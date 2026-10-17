"""x402 RLUSD payment for tool routes: quote -> verify -> run upstream -> settle.

The buyer is charged only when the upstream run succeeded (or is still running when
we hand back a job). Verification happens before the run; settlement after it.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse, Response
from x402_xrpl.facilitator import AsyncFacilitatorClient, FacilitatorClientOptions
from x402_xrpl.server import require_payment
from x402_xrpl.types import PaymentPayload, PaymentRequired, PaymentRequirements, ResourceInfo

from .catalog import TOOL_PATH_PREFIX, CatalogStore, Tool
from .config import Settings
from .pricing import Quote, UnquotableError, base_quote, quote_tool
from .upstream import UpstreamClient

logger = logging.getLogger(__name__)

Handler = Callable[[Request], Awaitable[Response]]
PaymentMiddlewareFactory = Callable[..., Callable[[Request, Handler], Awaitable[Response]]]

# Handlers set this on request.state to ask for settlement of a verified payment.
SETTLE_FLAG = "x402_settle"


class InvoiceStore:
    """Binds an issued invoice to its requirements; guards against double execution."""

    def __init__(self, max_entries: int = 50_000) -> None:
        self._invoices: dict[str, tuple[float, Any]] = {}
        self._active: set[str] = set()
        self._max_entries = max_entries
        self._lock = asyncio.Lock()

    def _expire(self, now: float) -> None:
        for key in [k for k, (expires, _) in self._invoices.items() if expires <= now]:
            self._invoices.pop(key, None)
            self._active.discard(key)

    async def put(self, invoice_id: str, reqs: Any, *, ttl_seconds: int) -> None:
        async with self._lock:
            now = time.monotonic()
            self._expire(now)
            while len(self._invoices) >= self._max_entries:
                victim = next((k for k in self._invoices if k not in self._active), None)
                if victim is None:
                    break
                self._invoices.pop(victim, None)
            self._invoices[invoice_id] = (now + ttl_seconds, reqs)

    async def get(self, invoice_id: str) -> Any | None:
        async with self._lock:
            self._expire(time.monotonic())
            stored = self._invoices.get(invoice_id)
            return stored[1] if stored else None

    async def claim(self, invoice_id: str) -> bool:
        async with self._lock:
            if invoice_id not in self._invoices or invoice_id in self._active:
                return False
            self._active.add(invoice_id)
            return True

    async def release(self, invoice_id: str) -> None:
        async with self._lock:
            self._active.discard(invoice_id)

    async def consume(self, invoice_id: str) -> None:
        async with self._lock:
            self._invoices.pop(invoice_id, None)
            self._active.discard(invoice_id)


def _decode_signature(signature: str) -> dict[str, Any] | None:
    try:
        decoded = json.loads(base64.b64decode(signature))
    except (ValueError, TypeError):
        return None
    return decoded if isinstance(decoded, dict) else None


def _signature_matches(decoded: dict[str, Any], amount: str, settings: Settings) -> bool:
    accepted = decoded.get("accepted")
    if not isinstance(accepted, dict):
        return False
    extra = accepted.get("extra") or {}
    return (
        accepted.get("scheme") == "exact"
        and accepted.get("network") == settings.X402_XRPL_NETWORK
        and str(accepted.get("amount")) == amount
        and accepted.get("asset") == settings.X402_XRPL_ASSET
        and accepted.get("payTo") == settings.X402_XRPL_PAYTO_ADDRESS
        and extra.get("issuer") == settings.X402_XRPL_ISSUER
    )


def _invoice_id(decoded: dict[str, Any]) -> str | None:
    payload = decoded.get("payload")
    value = payload.get("invoiceId") if isinstance(payload, dict) else None
    return value if isinstance(value, str) and value else None


def _without_signature(request: Request) -> Request:
    scope = dict(request.scope)
    scope["headers"] = [
        (k, v) for k, v in request.scope.get("headers", []) if k.lower() != b"payment-signature"
    ]
    return Request(scope, request.receive)


def _b64(body: dict[str, Any]) -> str:
    return base64.b64encode(json.dumps(body, separators=(",", ":"), sort_keys=True).encode()).decode()


def _error(status: int, code: str, message: str, **extra: Any) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message, **extra}})


def _selected(payload: PaymentPayload, stored: Any) -> PaymentRequirements | None:
    accepted = payload.accepted
    for req in stored or []:
        if (
            isinstance(req, PaymentRequirements)
            and req.scheme == accepted.scheme
            and req.network == accepted.network
            and req.amount == accepted.amount
            and req.asset == accepted.asset
            and req.pay_to == accepted.pay_to
            and req.invoice_id() == accepted.invoice_id()
        ):
            return req
    return None


async def settle_payment(
    *,
    facilitator: Any,
    signature: str,
    invoices: InvoiceStore,
    resource_url: str,
    description: str,
) -> tuple[dict[str, Any] | None, Response | None]:
    """Settle a verified payment. Returns (settlement, None) or (None, error response)."""
    payload = PaymentPayload.from_dict(json.loads(base64.b64decode(signature)))
    invoice_id = payload.accepted.invoice_id()
    stored = await invoices.get(invoice_id) if invoice_id else None
    selected = _selected(payload, stored)
    if selected is None:
        return None, _error(409, "settlement_context_missing", "The invoice is no longer known.")
    try:
        response = await facilitator._client.post(
            "/settle",
            json={"paymentPayload": payload.to_dict(), "paymentRequirements": selected.to_dict()},
        )
        settlement = response.json()
    except (httpx.RequestError, ValueError):
        logger.error("settlement status unknown for invoice %s", invoice_id)
        return None, _error(409, "settlement_status_unknown", "Settlement status is unknown.")
    if response.status_code != 200 or not isinstance(settlement, dict):
        return None, _error(409, "settlement_status_unknown", "Settlement status is unknown.")
    if settlement.get("success") is not True:
        reason = str(settlement.get("errorReason") or "settle_failed")
        challenge = PaymentRequired(
            x402_version=2,
            error=f"settle_failed:{reason}",
            resource=ResourceInfo(url=resource_url, description=description, mime_type="application/json"),
            accepts=list(stored),
            extensions={},
        ).to_dict()
        return None, JSONResponse(
            status_code=402, content=challenge, headers={"PAYMENT-REQUIRED": _b64(challenge)}
        )
    await invoices.consume(invoice_id)
    return settlement, None


def tool_for(request: Request, catalogs: CatalogStore) -> Tool | None:
    path = request.scope.get("path", "")
    if not path.startswith(f"{TOOL_PATH_PREFIX}/"):
        return None
    return catalogs.catalog.by_route(path)


def register_payment_middleware(
    app: Any,
    settings: Settings,
    catalogs: CatalogStore,
    upstream: UpstreamClient,
    *,
    facilitator: Any | None = None,
    middleware_factory: PaymentMiddlewareFactory = require_payment,
) -> None:
    invoices = InvoiceStore()
    if facilitator is None and settings.X402_XRPL_FACILITATOR_URL:
        facilitator = AsyncFacilitatorClient(
            FacilitatorClientOptions(base_url=settings.X402_XRPL_FACILITATOR_URL)
        )
    app.state.x402_facilitator = facilitator
    app.state.x402_invoices = invoices

    @app.middleware("http")
    async def tool_payment(request: Request, call_next: Handler) -> Response:
        if request.method not in ("GET", "POST"):
            return await call_next(request)
        tool = tool_for(request, catalogs)
        if tool is None:
            return await call_next(request)
        if not settings.X402_PAYMENT_ENABLED:
            return await call_next(request)
        problems = settings.configuration_problems
        if problems or facilitator is None:
            return _error(503, "payment_not_configured", "The payment gateway is not configured yet.")

        # Directory crawlers probe with GET, an empty POST or POST {}: they get the
        # base price, and a GET never runs the tool.
        if request.method == "GET":
            request = _without_signature(request)
        signature = request.headers.get("PAYMENT-SIGNATURE")
        raw = await request.body() if request.method == "POST" else b""
        is_probe = False
        body: Any = None
        if raw.strip():
            try:
                body = json.loads(raw)
            except ValueError:
                return _error(400, "invalid_json", "The request body must be a JSON object.")
            if not isinstance(body, dict):
                return _error(400, "invalid_json", "The request body must be a JSON object.")
        elif signature:
            return _error(400, "missing_input", "A paid request must carry the tool input as JSON.")
        if not signature and not body:
            is_probe = True

        if not is_probe and not await upstream.balance_ok():
            return _error(503, "temporarily_unavailable", "This tool is temporarily unavailable.")

        try:
            quote: Quote = base_quote(tool, settings) if is_probe else quote_tool(tool, body, settings)
        except UnquotableError as exc:
            return _error(503, "tool_unpriceable", str(exc))

        decoded = _decode_signature(signature) if signature else None
        if signature and decoded is None:
            return _error(400, "invalid_payment_signature", "PAYMENT-SIGNATURE is not valid base64 JSON.")
        paid = decoded is not None and _signature_matches(decoded, quote.amount, settings)
        # A signature for a different amount (the input changed after quoting) is
        # treated as unpaid: the buyer gets a fresh quote for this input.
        payment_request = request if paid or not signature else _without_signature(request)
        invoice_id = _invoice_id(decoded) if paid and decoded else None
        if invoice_id is not None and not await invoices.claim(invoice_id):
            return _error(409, "payment_attempt_in_progress", "This invoice is already being processed.")

        resource_url = f"{settings.PUBLIC_BASE_URL.rstrip('/')}{tool.path}"
        description = f"{tool.name} — {settings.brand_name}"
        middleware = middleware_factory(
            path=None,  # this middleware is built for this one request only
            price=quote.amount,
            pay_to_address=settings.X402_XRPL_PAYTO_ADDRESS,
            network=settings.X402_XRPL_NETWORK,
            asset=settings.X402_XRPL_ASSET,
            issuer=settings.X402_XRPL_ISSUER,
            source_tag=settings.X402_SOURCE_TAG,
            max_timeout_seconds=settings.X402_MAX_TIMEOUT_SECONDS,
            resource=resource_url,
            description=description,
            extra={
                "crossCurrency": False,
                "discoveryProbe": is_probe,
                "pricing": {**quote.detail, "estimated": quote.estimated},
            },
            facilitator=facilitator,
            invoice_store=invoices,
            settle=False,
        )

        async def run_then_settle(verified: Request) -> Response:
            verified.state.x402_verified = True
            verified.state.x402_quote = quote
            verified.state.x402_invoice_id = invoice_id
            response = await call_next(verified)
            if not getattr(verified.state, SETTLE_FLAG, False) or not signature:
                return response
            settlement, failure = await settle_payment(
                facilitator=facilitator,
                signature=signature,
                invoices=invoices,
                resource_url=resource_url,
                description=description,
            )
            on_settled = getattr(verified.state, "x402_on_settled", None)
            if on_settled is not None:
                await on_settled(settlement)
            if failure is not None:
                return failure
            response.headers["PAYMENT-RESPONSE"] = _b64(settlement)
            return response

        try:
            return await middleware(payment_request, run_then_settle)
        finally:
            if invoice_id is not None:
                await invoices.release(invoice_id)


def new_job_token() -> str:
    return uuid.uuid4().hex + uuid.uuid4().hex
