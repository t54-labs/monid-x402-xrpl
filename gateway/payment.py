"""x402 RLUSD payment for tool routes: quote -> verify -> run upstream -> settle.

The buyer is charged only when the upstream run succeeded (or is still running when
we hand back a job). Verification happens before the run; settlement after it.
"""

from __future__ import annotations

import base64
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Any
from urllib.parse import quote as urlquote

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse, Response
from x402_xrpl.facilitator import AsyncFacilitatorClient, FacilitatorClientOptions
from x402_xrpl.server import require_payment
from x402_xrpl.types import PaymentPayload, PaymentRequired, PaymentRequirements, ResourceInfo

from .catalog import TOOL_PATH_PREFIX, CatalogStore, Tool
from .config import Settings
from .invoices import BoundInvoiceStore, InvoiceStore, digest
from .pricing import Quote, UnquotableError, base_quote, quote_tool
from .upstream import UpstreamClient

logger = logging.getLogger(__name__)

Handler = Callable[[Request], Awaitable[Response]]
PaymentMiddlewareFactory = Callable[..., Callable[[Request, Handler], Awaitable[Response]]]

# Handlers set this on request.state to ask for settlement of a verified payment.
SETTLE_FLAG = "x402_settle"


def _decode_signature(signature: str) -> dict[str, Any] | None:
    try:
        decoded = json.loads(base64.b64decode(signature))
    except (ValueError, TypeError):
        return None
    return decoded if isinstance(decoded, dict) else None


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
    return next((req for req in stored or [] if req.to_dict() == payload.accepted.to_dict()), None)


async def settle_payment(
    *,
    facilitator: Any,
    signature: str,
    invoices: InvoiceStore,
    resource_url: str,
    description: str,
    allow_unsubmitted_retry: bool = False,
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
    if (
        response.status_code != 200
        or not isinstance(settlement, dict)
        or type(settlement.get("success")) is not bool
    ):
        return None, _error(409, "settlement_status_unknown", "Settlement status is unknown.")
    if settlement.get("network") != selected.network:
        return None, _error(409, "settlement_status_unknown", "Settlement network is unknown.")
    if settlement.get("success") is not True:
        reason = str(settlement.get("errorReason") or "settle_failed")
        # These are the SDK's legacy ambiguous submission/validation outcomes.
        # Only a documented pre-submission rejection is automatically retryable.
        if (
            not allow_unsubmitted_retry
            or settlement.get("settlementAttempted") is not False
            or reason != "insufficient_funds"
            or settlement.get("transaction")
        ):
            return None, _error(409, "settlement_status_unknown", "Settlement status is unknown.")
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
    if not isinstance(settlement.get("transaction"), str) or not settlement["transaction"]:
        return None, _error(409, "settlement_status_unknown", "Settlement transaction is unknown.")
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
    invoices = InvoiceStore(app.state.ledger)
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
        if tool is None and not (
            request.method == "POST"
            and request.scope.get("path", "").startswith(f"{TOOL_PATH_PREFIX}/")
            and request.headers.get("PAYMENT-SIGNATURE")
        ):
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

        # Price alone cannot bind an invoice: most tools have a flat price.
        binding = digest(["POST", tool.provider, tool.endpoint, body or {}]) if tool else None
        decoded = _decode_signature(signature) if signature else None
        payload = None
        row = None
        selected = None
        if signature:
            try:
                payload = PaymentPayload.from_dict(decoded) if decoded is not None else None
                invoice_id = payload.payload.get("invoiceId") if payload else None
                if (
                    not payload
                    or payload.x402_version != 2
                    or not isinstance(invoice_id, str)
                    or not invoice_id
                    or invoice_id != payload.accepted.invoice_id()
                ):
                    raise ValueError("invalid invoice")
            except (ValueError, TypeError, KeyError, AttributeError):
                return _error(400, "invalid_payment_signature", "Invalid payment envelope.")
            row = await invoices.record(invoice_id)
            if row is None:
                return _error(409, "unknown_invoice", "Obtain a new quote; this invoice is not known.")
            if row["state"] != "quoted":
                # Recovery is tied to the original route/target, independently of
                # catalog removal, exclusion or replacement after execution.
                provider, endpoint = json.loads(row["target"])
                route = f"{TOOL_PATH_PREFIX}/{provider}{endpoint}"
                if request.scope["path"] != route:
                    return _error(409, "invoice_request_mismatch", "The invoice belongs to another route.")
                binding = digest(["POST", provider, endpoint, body or {}])
            elif tool is None:
                return _error(404, "unknown_tool", "This tool is no longer available.")
            if row["binding"] != binding:
                if row["state"] != "quoted":
                    return _error(409, "invoice_request_mismatch", "The invoice belongs to another request.")
                # Preserve fresh-quote behavior for changed input, including flat prices.
                signature = None
                payload = None
                row = None
                request = _without_signature(request)
            elif (selected := _selected(payload, await invoices.get(invoice_id))) is None:
                return _error(409, "invoice_expired_or_mismatched", "Obtain a new quote.")
            elif row["state"] != "quoted":
                if row["payment_hash"] != digest(payload.to_dict()):
                    return _error(409, "invoice_payment_mismatch", "Use the original verified payment.")
                return await deliver_or_settle(row, signature, urlquote(route, safe="/:._-~"))

        if not is_probe and not await upstream.balance_ok():
            return _error(503, "temporarily_unavailable", "This tool is temporarily unavailable.")
        if row is not None:
            # Stored requirements survive repricing and balance depletion.
            detail = dict((selected.extra or {}).get("pricing") or {})
            quote = Quote(
                cost_usd=Decimal(selected.amount),
                amount=selected.amount,
                estimated=bool(detail.get("estimated")),
                detail=detail,
            )
        else:
            try:
                quote = base_quote(tool, settings) if is_probe else quote_tool(tool, body, settings)
            except UnquotableError as exc:
                return _error(503, "tool_unpriceable", str(exc))
        invoice_id = row["id"] if row else None
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
            invoice_store=BoundInvoiceStore(invoices, binding, [tool.provider, tool.endpoint]),
            settle=False,
        )

        async def run_then_settle(verified: Request) -> Response:
            run_id = uuid.uuid4().hex
            if invoice_id is None or not await invoices.begin(invoice_id, digest(payload.to_dict()), run_id):
                return _error(409, "payment_attempt_in_progress", "The invoice was claimed or expired.")
            verified.state.x402_verified = True
            verified.state.x402_quote = quote
            verified.state.x402_invoice_id = invoice_id
            verified.state.x402_run_id = run_id
            verified.state.x402_tool = tool  # execute the exact catalog snapshot we bound
            response = await call_next(verified)
            if getattr(verified.state, "x402_execution_unknown", False):
                return response  # permanent execution tombstone; never reopen it
            # Starlette call_next returns a streaming response. Fully persist the
            # redacted public response before any settlement can be submitted.
            content = b"".join([chunk async for chunk in response.body_iterator])
            saved = Response(
                content=content, status_code=response.status_code, headers=dict(response.headers)
            )
            billable = getattr(verified.state, SETTLE_FLAG, False)
            await invoices.save_response(invoice_id, saved, billable=billable)
            return await deliver_or_settle(await invoices.record(invoice_id), signature, tool.path)

        return await middleware(request, run_then_settle)

    async def deliver_or_settle(row: dict[str, Any], signature: str, path: str) -> Response:
        if row["state"] in ("settled", "unbilled"):
            return invoices.response(row)
        if row["state"] in ("settling", "settlement_unknown"):
            return _error(409, "settlement_status_unknown", "Reconciliation is required before retrying.")
        if row["state"] != "ready":
            return _error(
                409, "execution_status_unknown", "Execution is in progress or requires reconciliation."
            )
        if not await invoices.start_settlement(row["id"]):
            return _error(409, "payment_attempt_in_progress", "Settlement is already being processed.")
        # A crash/cancellation from here leaves 'settling' durably closed. A
        # positive no-submission decision is required before another /settle.
        settlement, failure = await settle_payment(
            facilitator=facilitator,
            signature=signature,
            invoices=invoices,
            resource_url=f"{settings.PUBLIC_BASE_URL.rstrip('/')}{path}",
            description=settings.brand_name,
            allow_unsubmitted_retry=settings.X402_ALLOW_UNSUBMITTED_RETRY,
        )
        if failure is not None:
            state = "ready" if failure.status_code == 402 else "settlement_unknown"
            await invoices.settlement_result(row["id"], state=state)
            return failure
        await invoices.settlement_result(row["id"], state="settled", settlement=settlement)
        return invoices.response(await invoices.record(row["id"]))


def new_job_token() -> str:
    return uuid.uuid4().hex + uuid.uuid4().hex
