from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .catalog import CatalogStore, Tool
from .config import Settings
from .deadline import RequestDeadlineMiddleware
from .payment import SETTLE_FLAG, new_job_token, register_payment_middleware, tool_for
from .pricing import UnquotableError, base_quote
from .spend import SpendDenied, SpendGuard
from .store import RunLedger
from .upstream import RunResult, UpstreamClient

logger = logging.getLogger(__name__)

# Upstream response fields that are safe to pass to buyers. Everything else
# (billing, hints with upstream CLI commands, provider metadata) stays private.
PUBLIC_RUN_FIELDS = ("status", "output", "billedUnits", "createdAt", "completedAt")


def _public_run(result: RunResult) -> dict[str, Any]:
    body = {key: result.body[key] for key in PUBLIC_RUN_FIELDS if key in result.body}
    provider = result.body.get("providerResponse") or {}
    if provider.get("error") is not None:
        body["error"] = provider["error"]
    return body


def _tool_entry(tool: Tool, settings: Settings, labels: dict[str, str | None]) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "url": f"{settings.PUBLIC_BASE_URL.rstrip('/')}{tool.path}",
        "method": "POST",
        "name": tool.name,
        "description": tool.description,
        "externalId": tool.external_id,
        **labels,
    }
    try:
        quote = base_quote(tool, settings)
        entry["price"] = {
            "amount": quote.amount,
            "asset": "RLUSD",
            "issuer": settings.X402_XRPL_ISSUER,
            "network": settings.X402_XRPL_NETWORK,
            "variable": tool.price.get("type") != "PER_CALL",
        }
    except UnquotableError:
        entry["price"] = None
    return entry


def create_app(
    settings: Settings | None = None,
    *,
    http_client: httpx.AsyncClient | None = None,
    facilitator: Any | None = None,
    payment_middleware_factory: Any | None = None,
    sync_catalog: bool = True,
) -> FastAPI:
    settings = settings or Settings()
    client = http_client or httpx.AsyncClient(timeout=30)
    catalogs = CatalogStore(settings, client)
    upstream = UpstreamClient(settings, client)
    ledger = RunLedger(settings.DATABASE_PATH)
    run_slots = asyncio.Semaphore(settings.MAX_CONCURRENT_RUNS)

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        task = asyncio.create_task(catalogs.run_forever()) if sync_catalog else None
        problems = settings.configuration_problems
        if not settings.X402_PAYMENT_ENABLED:
            problems = [*problems, "payment_enforcement_disabled"]
        if problems:
            logger.warning("paid routes are closed: %s", "; ".join(problems))
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
            ledger.close()
            if http_client is None:
                await client.aclose()

    app = FastAPI(title=settings.brand_name or "Tool Gateway", lifespan=lifespan)
    app.state.settings = settings
    app.state.catalogs = catalogs
    app.state.upstream = upstream
    app.state.ledger = ledger
    app.state.spend = SpendGuard(settings, ledger)

    kwargs: dict[str, Any] = {"facilitator": facilitator}
    if payment_middleware_factory is not None:
        kwargs["middleware_factory"] = payment_middleware_factory
    register_payment_middleware(app, settings, catalogs, upstream, **kwargs)
    app.add_middleware(
        RequestDeadlineMiddleware, seconds=settings.REQUEST_DEADLINE_SECONDS,
        concurrency=settings.MAX_CONCURRENT_RUNS,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["PAYMENT-REQUIRED", "PAYMENT-RESPONSE"],
    )

    @app.get("/health")
    async def health() -> dict[str, Any]:
        problems = settings.configuration_problems
        if not settings.X402_PAYMENT_ENABLED:
            problems = [*problems, "payment_enforcement_disabled"]
        if not await app.state.spend.ready(catalogs.catalog.tools):
            problems = [*problems, "reviewed_spend_policy_or_budget_unavailable"]
        return {
            "ok": True,
            "paymentsReady": not problems,
            "problems": problems,
            "tools": len(catalogs.catalog.tools),
            "catalogSyncedAt": catalogs.catalog.synced_at,
        }

    @app.get("/.well-known/x402")
    async def discovery(page: int = Query(1, ge=1)) -> dict[str, Any]:
        catalog = catalogs.catalog
        size = settings.CATALOG_PAGE_SIZE
        tools = catalog.tools[(page - 1) * size : page * size]
        pages = max(1, -(-len(catalog.tools) // size))
        base = settings.PUBLIC_BASE_URL.rstrip("/")
        return {
            "x402Version": 2,
            "name": settings.brand_name,
            "description": settings.GATEWAY_DESCRIPTION,
            "logoUrl": settings.GATEWAY_LOGO_URL or None,
            "version": catalog.synced_at,
            "total": len(catalog.tools),
            "page": page,
            "pages": pages,
            "next": f"{base}/.well-known/x402?page={page + 1}" if page < pages else None,
            "resources": [_tool_entry(t, settings, catalog.labels(t)) for t in tools],
        }

    @app.get("/catalog")
    async def search_catalog(
        q: str = "",
        provider: str = "",
        category: str = "",
        page: int = Query(1, ge=1),
        size: int = Query(50, ge=1, le=200),
    ) -> dict[str, Any]:
        catalog = catalogs.catalog
        needle = q.strip().lower()
        matches = [
            t
            for t in catalog.tools
            if (not provider or t.provider == provider)
            and (not category or category in t.categories)
            and (not needle or needle in f"{t.name} {t.description} {t.provider_name}".lower())
        ]
        chunk = matches[(page - 1) * size : page * size]
        return {
            "total": len(matches),
            "page": page,
            "items": [_tool_entry(t, settings, catalog.labels(t)) for t in chunk],
        }

    @app.post("/t/{tool_path:path}")
    async def run_tool(tool_path: str, request: Request) -> JSONResponse:
        tool = getattr(request.state, "x402_tool", None) or tool_for(request, catalogs)
        if tool is None:
            return JSONResponse(status_code=404, content={"error": {"code": "unknown_tool"}})
        if settings.X402_PAYMENT_ENABLED and not getattr(request.state, "x402_verified", False):
            return JSONResponse(status_code=402, content={"error": {"code": "payment_required"}})
        # Payment-disabled compatibility mode must not become a spend bypass.
        permit = getattr(request.state, "spend_permit", None)
        if permit is None or not getattr(request.state, "x402_verified", False):
            return JSONResponse(status_code=503, content={"error": {"code": "spend_not_configured"}})
        try:
            payload = await request.json()
        except ValueError:
            payload = {}

        run_id = getattr(request.state, "x402_run_id", None) or uuid.uuid4().hex
        quote = getattr(request.state, "x402_quote", None)
        await ledger.create(
            run_id,
            invoice_id=getattr(request.state, "x402_invoice_id", None),
            tool=tool.external_id,
            quote_rlusd=quote.amount if quote else "0",
            quote_estimated=quote.estimated if quote else False,
        )

        async with run_slots:
            try:
                current = catalogs.catalog.by_route(request.scope["path"])
                if current is None or app.state.spend.permit(current, payload) != permit:
                    raise SpendDenied("queued policy changed")
                await app.state.spend.dispatch(request.state.x402_invoice_id, run_id, permit)
                # The commit yields to other tasks. Recheck after it, with no
                # further await before UpstreamClient.run constructs its URL,
                # headers and JSON. A rejection keeps dispatch intent occupied.
                current = catalogs.catalog.by_route(request.scope["path"])
                if current is None or app.state.spend.permit(current, payload) != permit:
                    raise SpendDenied("policy changed while committing dispatch")
            except SpendDenied:
                await app.state.spend.reject_before_dispatch(request.state.x402_invoice_id)
                return JSONResponse(
                    status_code=503, content={"error": {"code": "spend_pre_dispatch_rejected"}}
                )
            try:
                result = await upstream.run(tool.provider, tool.endpoint, payload)
                # An accepted async job must be recoverable even if polling times
                # out or this process dies while waiting for it.
                await ledger.update(
                    run_id, upstream_run_id=result.run_id, upstream_status=result.status,
                )
                if not result.terminal and result.run_id:
                    try:
                        result = await upstream.wait(result, settings.RUN_WAIT_SECONDS)
                    except httpx.HTTPError:
                        # Known accepted work: return the original durable job,
                        # rather than discarding it and starting another run.
                        logger.warning("upstream poll failed for run %s", run_id)
            except httpx.HTTPError:
                logger.warning("upstream execution status unknown for run %s", run_id)
                request.state.x402_execution_unknown = True
                await ledger.update(run_id, state="execution_unknown")
                return JSONResponse(
                    status_code=409,
                    content={"error": {
                        "code": "execution_status_unknown", "message": "Reconciliation required.",
                    }},
                )

        await ledger.update(
            run_id,
            upstream_run_id=result.run_id,
            upstream_status=result.status,
            upstream_cost_usd=str(result.cost_usd) if result.cost_usd is not None else None,
        )

        if not result.terminal and result.run_id is None:
            if not 400 <= result.http_status < 500:
                request.state.x402_execution_unknown = True
                await ledger.update(run_id, state="execution_unknown")
                return JSONResponse(status_code=409, content={"error": {"code": "execution_status_unknown"}})
            # Rejected before any run started (bad input, auth, balance): nothing was
            # done upstream, so nothing is charged.
            await ledger.update(run_id, state="rejected_unbilled")
            status = result.http_status if 400 <= result.http_status < 500 else 502
            message = result.body.get("message") if status < 500 else "The tool could not be started."
            return JSONResponse(
                status_code=status,
                content={"error": {"code": "upstream_rejected", "message": message}},
            )

        if result.terminal:
            if not result.succeeded:
                # Not charged: the upstream bills failed and empty runs at zero.
                await ledger.update(run_id, state="failed_unbilled")
                status = result.http_status if 400 <= result.http_status < 600 else 502
                return JSONResponse(status_code=status, content=_public_run(result))
            setattr(request.state, SETTLE_FLAG, True)
            return JSONResponse(status_code=200, content=_public_run(result))

        # Still running at the deadline: the work is committed upstream, so settle
        # and hand back a job the buyer can poll for free.
        token = new_job_token()
        await ledger.update(run_id, job_token=token, state="running")
        setattr(request.state, SETTLE_FLAG, True)
        base = settings.PUBLIC_BASE_URL.rstrip("/")
        return JSONResponse(
            status_code=202,
            content={
                "status": result.status or "RUNNING",
                "jobId": run_id,
                "jobToken": token,
                "poll": f"{base}/jobs/{run_id}",
            },
        )

    @app.get("/jobs/{job_id}")
    async def get_job(job_id: str, request: Request) -> JSONResponse:
        token = request.headers.get("X-Job-Token") or request.query_params.get("token") or ""
        row = await ledger.get(job_id)
        invoice = (
            await app.state.x402_invoices.record(row["invoice_id"])
            if row and row.get("invoice_id") else None
        )
        if (
            row is None
            or (row.get("invoice_id") and (
                invoice["state"] != "settled" if invoice is not None else row["state"] != "settled"
            ))
            or not row.get("job_token")
            or not hmac.compare_digest(row["job_token"], token)
            or not row.get("upstream_run_id")
        ):
            return JSONResponse(status_code=404, content={"error": {"code": "unknown_job"}})
        try:
            result = await upstream.get_run(row["upstream_run_id"])
        except httpx.HTTPError:
            return JSONResponse(status_code=502, content={"error": {"code": "upstream_unavailable"}})
        if result.terminal:
            await ledger.update(
                job_id,
                upstream_status=result.status,
                upstream_cost_usd=str(result.cost_usd) if result.cost_usd is not None else None,
            )
        return JSONResponse(status_code=200, content={"jobId": job_id, **_public_run(result)})

    return app
