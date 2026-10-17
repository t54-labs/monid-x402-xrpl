"""Client for the upstream run API (Monid), called with our own key."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx

from .config import Settings

TERMINAL_STATUSES = frozenset({"COMPLETED", "FAILED", "BLOCKED", "STOPPED", "TIMED_OUT"})


@dataclass(frozen=True)
class RunResult:
    http_status: int
    body: dict[str, Any]

    @property
    def run_id(self) -> str | None:
        value = self.body.get("runId")
        return value if isinstance(value, str) else None

    @property
    def status(self) -> str:
        return str(self.body.get("status") or "")

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def succeeded(self) -> bool:
        """Completed with a 2xx provider response and some output.

        Provider errors complete as data and the upstream bills them at zero, so the
        buyer must not be charged for them either.
        """
        provider = self.body.get("providerResponse") or {}
        provider_status = provider.get("httpStatus", self.http_status)
        return (
            self.status == "COMPLETED"
            and isinstance(provider_status, int)
            and 200 <= provider_status < 300
            and self.body.get("output") is not None
        )

    @property
    def cost_usd(self) -> Decimal | None:
        billing = self.body.get("billing") or {}
        for key in ("calculatedCost", "reportedCost"):
            cost = billing.get(key)
            if isinstance(cost, dict) and isinstance(cost.get("value"), int | float):
                if cost.get("unit") == "MICRO_DOLLAR":
                    return Decimal(cost["value"]) / Decimal(1_000_000)
                return Decimal(str(cost["value"]))
        return None


class UpstreamClient:
    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self._settings = settings
        self._client = client
        self._balance: tuple[float, Decimal | None] = (0.0, None)

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._settings.MONID_API_KEY}"}

    def _url(self, path: str) -> str:
        return f"{self._settings.MONID_API_BASE_URL.rstrip('/')}{path}"

    async def run(self, provider: str, endpoint: str, payload: Any) -> RunResult:
        response = await self._client.post(
            self._url("/v1/run"),
            headers=self._headers,
            json={"provider": provider, "endpoint": endpoint, "input": payload or {}},
            timeout=self._settings.MONID_REQUEST_TIMEOUT_SECONDS,
        )
        return RunResult(response.status_code, _json(response))

    async def get_run(self, run_id: str) -> RunResult:
        response = await self._client.get(
            self._url(f"/v1/runs/{run_id}"),
            headers=self._headers,
            timeout=self._settings.MONID_REQUEST_TIMEOUT_SECONDS,
        )
        return RunResult(response.status_code, _json(response))

    async def wait(self, result: RunResult, deadline_seconds: float) -> RunResult:
        """Poll an async run until it finishes or the deadline passes."""
        deadline = time.monotonic() + deadline_seconds
        while not result.terminal and result.run_id and time.monotonic() < deadline:
            await asyncio.sleep(self._settings.RUN_POLL_INTERVAL_SECONDS)
            result = await self.get_run(result.run_id)
        return result

    async def balance_ok(self) -> bool:
        """False when the prepaid upstream balance is below the floor (cached)."""
        checked_at, balance = self._balance
        if time.monotonic() - checked_at > self._settings.MONID_BALANCE_CHECK_SECONDS:
            balance = await self._fetch_balance()
            self._balance = (time.monotonic(), balance)
        # An unreadable balance does not block sales; the run itself will fail with 402.
        return balance is None or balance >= self._settings.min_balance_usd

    async def _fetch_balance(self) -> Decimal | None:
        try:
            response = await self._client.get(
                self._url("/v1/wallet/balance"), headers=self._headers, timeout=10
            )
            body = _json(response)
        except httpx.HTTPError:
            return None
        if response.status_code != 200:
            return None
        for key in ("balance", "available", "amount"):
            value = body.get(key)
            if isinstance(value, dict):
                value = value.get("value")
            if isinstance(value, int | float | str):
                try:
                    return Decimal(str(value))
                except ArithmeticError:
                    return None
        return None


def _json(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {"error": response.text[:500]}
    return body if isinstance(body, dict) else {"output": body}
