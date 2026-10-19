"""Durable invoice state; irreversible work is never unlocked by a retry or TTL."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Any

from fastapi.responses import Response
from x402_xrpl.types import PaymentRequirements

from .store import RunLedger


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class InvoiceStore:
    def __init__(self, ledger: RunLedger) -> None:
        self.ledger = ledger

    async def put(
        self, invoice_id: str, reqs: Any, *, ttl_seconds: int, binding: str, target: list[str]
    ) -> None:
        now = time.time()
        await self.ledger.transaction(
            [
                ("DELETE FROM invoices WHERE state = 'quoted' AND expires_at <= ?", (now,)),
                # Only unused quotes can be evicted. Execution tombstones never expire.
                (
                    "DELETE FROM invoices WHERE id IN (SELECT id FROM invoices WHERE state = 'quoted' "
                    "ORDER BY updated_at DESC LIMIT -1 OFFSET 49999)",
                    (),
                ),
                (
                    "INSERT INTO invoices (id, requirements, binding, target, expires_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        invoice_id,
                        json.dumps([r.to_dict() for r in reqs]),
                        binding,
                        json.dumps(target),
                        now + ttl_seconds,
                        now,
                    ),
                ),
            ]
        )

    async def record(self, invoice_id: str) -> dict[str, Any] | None:
        rows = await self.ledger._run("SELECT * FROM invoices WHERE id = ?", (invoice_id,))
        return dict(rows[0]) if rows else None

    async def get(self, invoice_id: str) -> list[PaymentRequirements] | None:
        row = await self.record(invoice_id)
        if row is None or (row["state"] == "quoted" and row["expires_at"] <= time.time()):
            return None
        return [PaymentRequirements.from_dict(req) for req in json.loads(row["requirements"])]

    async def begin(self, invoice_id: str, payment_hash: str, run_id: str) -> bool:
        rows = await self.ledger._run(
            "UPDATE invoices SET state = 'executing', payment_hash = ?, run_id = ?, updated_at = ? "
            "WHERE id = ? AND state = 'quoted' AND expires_at > ? RETURNING id",
            (payment_hash, run_id, time.time(), invoice_id, time.time()),
        )
        return bool(rows)

    async def save_response(self, invoice_id: str, response: Response, *, billable: bool) -> None:
        await self.ledger._run(
            "UPDATE invoices SET state = ?, response_status = ?, response_body = ?, response_headers = ?, "
            "updated_at = ? WHERE id = ? AND state = 'executing'",
            (
                "ready" if billable else "unbilled",
                response.status_code,
                response.body,
                json.dumps({"content-type": response.headers.get("content-type", "application/json")}),
                time.time(),
                invoice_id,
            ),
        )

    async def start_settlement(self, invoice_id: str) -> bool:
        return bool(
            await self.ledger._run(
                "UPDATE invoices SET state = 'settling', updated_at = ? "
                "WHERE id = ? AND state = 'ready' RETURNING id",
                (time.time(), invoice_id),
            )
        )

    async def settlement_result(
        self, invoice_id: str, *, state: str, settlement: dict[str, Any] | None = None
    ) -> None:
        await self.ledger.transaction(
            [
                (
                    "UPDATE invoices SET state = ?, settlement = ?, updated_at = ? WHERE id = ?",
                    (state, json.dumps(settlement) if settlement else None, time.time(), invoice_id),
                ),
                (
                    "UPDATE runs SET state = ?, settlement_tx = ?, payer = ?, updated_at = ? "
                    "WHERE id = (SELECT run_id FROM invoices WHERE id = ?)",
                    (
                        "settled" if state == "settled" else "settle_failed" if state == "ready" else state,
                        (settlement or {}).get("transaction"),
                        (settlement or {}).get("payer"),
                        time.time(),
                        invoice_id,
                    ),
                ),
            ]
        )

    async def reconcile(self, invoice_id: str, *, evidence: str, settlement: dict[str, Any] | None) -> None:
        """Offline operator decision after checking facilitator and validated chain data.

        None explicitly means proven NOT submitted/settled. This method never
        unlocks execution and never makes an external request.
        """
        if not evidence.strip():
            raise ValueError("An evidence reference is required")
        reqs = await self.get(invoice_id)
        if not reqs:
            raise ValueError("Unknown invoice")
        if settlement is not None and (
            settlement.get("success") is not True
            or not settlement.get("transaction")
            or not settlement.get("payer")
            or settlement.get("network") != reqs[0].network
        ):
            raise ValueError("Confirmed settlement requires transaction, payer and the invoice network")
        audit = json.dumps(
            {
                "id": uuid.uuid4().hex,
                "evidence": evidence,
                "at": time.time(),
                "outcome": "settled" if settlement else "not_settled",
            }
        )
        state = "settled" if settlement else "ready"
        results = await self.ledger.transaction(
            [
                (
                    "UPDATE invoices SET state = ?, settlement = ?, reconciliation = ?, updated_at = ? "
                    "WHERE id = ? AND state IN ('settling', 'settlement_unknown') "
                    "AND response_body IS NOT NULL RETURNING id",
                    (state, json.dumps(settlement) if settlement else None, audit, time.time(), invoice_id),
                ),
                (
                    "UPDATE runs SET state = ?, settlement_tx = ?, payer = ?, updated_at = ? "
                    "WHERE id = (SELECT run_id FROM invoices WHERE id = ? AND reconciliation = ?)",
                    (
                        "settled" if settlement else "settle_failed",
                        (settlement or {}).get("transaction"),
                        (settlement or {}).get("payer"),
                        time.time(),
                        invoice_id,
                        audit,
                    ),
                ),
            ]
        )
        if not results[0]:
            raise ValueError("Only an unknown settlement with a saved response can be reconciled")

    @staticmethod
    def response(row: dict[str, Any]) -> Response:
        headers = json.loads(row["response_headers"])
        if row["state"] == "settled":
            import base64

            headers["PAYMENT-RESPONSE"] = base64.b64encode(row["settlement"].encode()).decode()
        return Response(content=row["response_body"], status_code=row["response_status"], headers=headers)


class BoundInvoiceStore:
    """Request-local SDK adapter; never put request binding in shared mutable state."""

    def __init__(self, invoices: InvoiceStore, binding: str, target: list[str]) -> None:
        self.invoices = invoices
        self.binding = binding
        self.target = target

    async def put(self, invoice_id: str, reqs: Any, *, ttl_seconds: int) -> None:
        await self.invoices.put(
            invoice_id,
            reqs,
            ttl_seconds=ttl_seconds,
            binding=self.binding,
            target=self.target,
        )

    async def get(self, invoice_id: str) -> Any:
        return await self.invoices.get(invoice_id)
