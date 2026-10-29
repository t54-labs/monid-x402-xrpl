"""Full gateway/SDK/local stand-in integration over ASGI, without TCP or XRPL."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, Request
from x402_xrpl.facilitator import AsyncFacilitatorClient, FacilitatorClientOptions

from gateway.app import create_app
from gateway.catalog import save_snapshot
from scripts import fake_monid

from .conftest import make_settings
from .test_app import ENDPOINTS, decode_header, paid_post, sign, write_synthetic_policies


class CountingTransport(httpx.ASGITransport):
    def __init__(self, app):
        super().__init__(app=app)
        self.calls = []

    async def handle_async_request(self, request):
        self.calls.append(request.url.path)
        return await super().handle_async_request(request)


@pytest.fixture
async def local_gateway(tmp_path, monkeypatch):
    settings = make_settings(tmp_path, MONID_SPEND_BUDGET_MICROS=10_000_000,
                             MONID_SPEND_BUDGET_ID="synthetic-budget",
                             MONID_SPEND_ACCOUNT_ID="synthetic-account",
                             MONID_TOOL_POLICIES_PATH=str(tmp_path / "synthetic-policies.json"))
    write_synthetic_policies(settings)
    save_snapshot(settings.CATALOG_SNAPSHOT_PATH, ENDPOINTS, [], "synthetic-snapshot")
    monkeypatch.setenv("FAKE_MONID_KEY", "test-key")
    monkeypatch.setenv("CATALOG", settings.CATALOG_SNAPSHOT_PATH)
    for name in ("_runs", "_polls", "_snapshot"):
        monkeypatch.setattr(fake_monid, name, {})
    upstream_transport = CountingTransport(fake_monid.app)
    upstream_client = httpx.AsyncClient(transport=upstream_transport)
    facilitator_app = FastAPI()
    decision = SimpleNamespace(success=True)

    @facilitator_app.post("/verify")
    async def verify(request: Request):
        body = await request.json()
        assert body["paymentPayload"]["accepted"] == body["paymentRequirements"]
        return {"isValid": True, "payer": "rSyntheticBuyer"}

    @facilitator_app.post("/settle")
    async def settle(request: Request):
        body = await request.json()
        return {
            "success": decision.success,
            "network": body["paymentRequirements"]["network"],
            "transaction": "SYNTHETIC_TX" if decision.success else "",
            "payer": "rSyntheticBuyer",
            **(
                {}
                if decision.success
                else {"errorReason": "insufficient_funds", "settlementAttempted": False}
            ),
        }

    facilitator_transport = CountingTransport(facilitator_app)
    facilitator = AsyncFacilitatorClient(FacilitatorClientOptions(base_url="https://facilitator.example"))
    await facilitator._client.aclose()
    facilitator._client = httpx.AsyncClient(
        transport=facilitator_transport, base_url="https://facilitator.example"
    )
    app = create_app(settings, http_client=upstream_client, facilitator=facilitator, sync_catalog=False)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=settings.PUBLIC_BASE_URL)
    try:
        async with app.router.lifespan_context(app):
            yield SimpleNamespace(
                app=app,
                client=client,
                settings=settings,
                decision=decision,
                upstream=upstream_transport,
                facilitator=facilitator_transport,
            )
    finally:
        await client.aclose()
        await upstream_client.aclose()
        await facilitator._client.aclose()


@pytest.mark.parametrize(
    "query, wait, expected", [("xrpl", 1, 200), ("fail", 1, 404), ("slow", 0, 202), ("slow", 1, 200)]
)
async def test_full_local_routes_sdk_and_standin(local_gateway, query, wait, expected):
    local = local_gateway
    local.settings.RUN_WAIT_SECONDS = wait
    health = await local.client.get("/health")
    assert health.status_code == 200 and health.json()["paymentsReady"] is True
    response = await paid_post(local.client, "/t/exa/search", {"query": query})
    assert response.status_code == expected
    assert "billing" not in response.json() and "hints" not in response.json()
    assert local.upstream.calls.count("/v1/run") == 1
    assert local.facilitator.calls == (["/verify"] if query == "fail" else ["/verify", "/settle"])
    if expected == 202:
        body = response.json()
        for _ in range(2):
            poll = await local.client.get(body["poll"], headers={"X-Job-Token": body["jobToken"]})
        assert poll.status_code == 200 and poll.json()["status"] == "COMPLETED"
        assert local.upstream.calls.count("/v1/run") == 1
    row = local.app.state.ledger._conn.execute("SELECT * FROM runs").fetchone()
    assert row["state"] == ("failed_unbilled" if query == "fail" else "settled")


async def test_default_unknown_settlement_recovers_only_after_operator_decision(local_gateway):
    local = local_gateway
    local.decision.success = False
    challenge = await local.client.post("/t/exa/search", json={"query": "xrpl"})
    headers = {"PAYMENT-SIGNATURE": sign(challenge.json())}
    for _ in range(2):
        response = await local.client.post("/t/exa/search", json={"query": "xrpl"}, headers=headers)
        assert response.status_code == 409 and "output" not in response.json()
    assert local.upstream.calls.count("/v1/run") == 1
    assert local.facilitator.calls == ["/verify", "/settle"]
    invoice_id = decode_header(headers["PAYMENT-SIGNATURE"])["payload"]["invoiceId"]
    # Synthetic control-plane decision; production operators must stop all processes
    # and establish the external evidence as described in the recovery runbook.
    await local.app.state.x402_invoices.reconcile(
        invoice_id, evidence="synthetic-no-submission-proof", settlement=None
    )
    local.decision.success = True
    response = await local.client.post("/t/exa/search", json={"query": "xrpl"}, headers=headers)
    assert response.status_code == 200 and response.json()["output"]["echo"] == {"query": "xrpl"}
    assert local.facilitator.calls == ["/verify", "/settle", "/settle"]
    assert local.upstream.calls.count("/v1/run") == 1
