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
from .payment import SETTLE_FLAG, new_job_token, register_payment_middleware, tool_for
from .pricing import UnquotableError, base_quote
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

    kwargs: dict[str, Any] = {"facilitator": facilitator}
    if payment_middleware_factory is not None:
        kwargs["middleware_factory"] = payment_middleware_factory
    register_payment_middleware(app, settings, catalogs, upstream, **kwargs)
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
        tool = tool_for(request, catalogs)
        if tool is None:
            return JSONResponse(status_code=404, content={"error": {"code": "unknown_tool"}})
        if settings.X402_PAYMENT_ENABLED and not getattr(request.state, "x402_verified", False):
            return JSONResponse(status_code=402, content={"error": {"code": "payment_required"}})
        try:
            payload = await request.json()
        except ValueError:
            payload = {}

        run_id = uuid.uuid4().hex
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
                result = await upstream.run(tool.provider, tool.endpoint, payload)
                if not result.terminal and result.run_id:
                    result = await upstream.wait(result, settings.RUN_WAIT_SECONDS)
            except httpx.HTTPError:
                logger.exception("upstream run failed for %s", tool.external_id)
                await ledger.update(run_id, state="upstream_error")
                return JSONResponse(
                    status_code=502,
                    content={"error": {"code": "upstream_unavailable", "message": "Try again later."}},
                )

        await ledger.update(
            run_id,
            upstream_run_id=result.run_id,
            upstream_status=result.status,
            upstream_cost_usd=str(result.cost_usd) if result.cost_usd is not None else None,
        )

        async def record_settlement(settlement: dict[str, Any] | None) -> None:
            if settlement is None:
                await ledger.update(run_id, state="settle_failed")
                return
            await ledger.update(
                run_id,
                state="settled",
                settlement_tx=settlement.get("transaction"),
                payer=settlement.get("payer"),
            )

        request.state.x402_on_settled = record_settlement

        if not result.terminal and result.run_id is None:
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
        if (
            row is None
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
