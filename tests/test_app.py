from __future__ import annotations

import base64
import json
from typing import Any

import httpx
import pytest

from gateway.app import create_app
from gateway.catalog import save_snapshot
from gateway.config import RLUSD_CURRENCY_HEX, RLUSD_TESTNET_ISSUER

from .conftest import PAY_TO, endpoint, make_settings, per_call

ENDPOINTS = [
    endpoint("exa", "/search", per_call(0.007), displayName="Exa Search API"),
    endpoint(
        "dataforseo",
        "/serp",
        {"type": "PER_RESULT", "amount": {"value": 0.001, "currency": "USD"}},
    ),
    endpoint("apify", "/actor", per_call(0.003)),
]


class FakeFacilitator:
    def __init__(self, *, verify_ok: bool = True, settle_ok: bool = True) -> None:
        self.calls: list[str] = []
        self.verify_ok = verify_ok
        self.settle_ok = settle_ok
        self._client = self

    async def post(self, path: str, json: dict[str, Any]) -> httpx.Response:  # noqa: A002
        self.calls.append(path)
        if path == "/verify":
            return httpx.Response(200, json={"isValid": self.verify_ok, "payer": "rBuyer"})
        if path == "/settle":
            return httpx.Response(
                200,
                json={
                    "success": self.settle_ok,
                    "transaction": "ABC123" if self.settle_ok else "",
                    "network": "xrpl:1",
                    "payer": "rBuyer",
                    **({} if self.settle_ok else {"errorReason": "insufficient_funds"}),
                },
            )
        return httpx.Response(404)


class FakeUpstream:
    def __init__(self) -> None:
        self.runs: list[dict[str, Any]] = []
        self.run_response: tuple[int, dict[str, Any]] = (200, completed())
        self.poll_responses: list[dict[str, Any]] = []
        self.balance = 100.0

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer test-key"
        if request.url.path == "/v1/run":
            self.runs.append(json.loads(request.content))
            status, body = self.run_response
            return httpx.Response(status, json=body)
        if request.url.path.startswith("/v1/runs/"):
            body = self.poll_responses.pop(0) if self.poll_responses else completed()
            return httpx.Response(200, json=body)
        if request.url.path == "/v1/wallet/balance":
            return httpx.Response(200, json={"balance": {"value": self.balance, "currency": "USD"}})
        return httpx.Response(404)


def completed(output: Any = None, *, http_status: int = 200, cost_micro: int = 7000) -> dict[str, Any]:
    return {
        "runId": "RUN1",
        "status": "COMPLETED",
        "output": {"results": [1, 2]} if output is None and http_status == 200 else output,
        "providerResponse": {"httpStatus": http_status},
        "billing": {"calculatedCost": {"value": cost_micro, "unit": "MICRO_DOLLAR", "currency": "USD"}},
        "hints": {"next": "monid run ..."},
        "billedUnits": 1,
    }


@pytest.fixture
def upstream() -> FakeUpstream:
    return FakeUpstream()


@pytest.fixture
def facilitator() -> FakeFacilitator:
    return FakeFacilitator()


def build(tmp_path, upstream, facilitator, **overrides):
    settings = make_settings(tmp_path, **overrides)
    save_snapshot(settings.CATALOG_SNAPSHOT_PATH, ENDPOINTS, [], "2026-10-17T00:00:00Z")
    upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream.handler))
    app = create_app(settings, http_client=upstream_client, facilitator=facilitator, sync_catalog=False)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://tools.example")
    return app, client


def decode_header(value: str) -> dict[str, Any]:
    return json.loads(base64.b64decode(value))


def sign(challenge: dict[str, Any]) -> str:
    accepted = challenge["accepts"][0]
    envelope = {
        "x402Version": 2,
        "accepted": accepted,
        "payload": {"invoiceId": accepted["extra"]["invoiceId"], "signedTxBlob": "DEADBEEF"},
    }
    return base64.b64encode(json.dumps(envelope).encode()).decode()


async def paid_post(client, path: str, body: dict[str, Any]) -> httpx.Response:
    challenge = await client.post(path, json=body)
    assert challenge.status_code == 402
    signature = sign(decode_header(challenge.headers["PAYMENT-REQUIRED"]))
    return await client.post(path, json=body, headers={"PAYMENT-SIGNATURE": signature})


async def test_probe_gets_rlusd_challenge_with_source_tag(tmp_path, upstream, facilitator):
    _, client = build(tmp_path, upstream, facilitator)
    response = await client.post("/t/exa/search")
    assert response.status_code == 402
    challenge = decode_header(response.headers["PAYMENT-REQUIRED"])
    [req] = challenge["accepts"]
    assert req["asset"] == RLUSD_CURRENCY_HEX
    assert req["amount"] == "0.007"
    assert req["payTo"] == PAY_TO
    assert req["network"] == "xrpl:1"
    assert req["extra"]["issuer"] == RLUSD_TESTNET_ISSUER
    assert req["extra"]["sourceTag"] == 804681468
    assert req["extra"]["crossCurrency"] is False
    assert challenge["resource"]["url"] == "https://tools.example/t/exa/search"
    assert "Monid Tool Router" in challenge["resource"]["description"]
    assert upstream.runs == []


async def test_get_probe_never_runs_the_tool(tmp_path, upstream, facilitator):
    _, client = build(tmp_path, upstream, facilitator)
    challenge = await client.post("/t/exa/search", json={"query": "x"})
    signature = sign(decode_header(challenge.headers["PAYMENT-REQUIRED"]))
    response = await client.get("/t/exa/search", headers={"PAYMENT-SIGNATURE": signature})
    assert response.status_code == 402
    assert facilitator.calls == [] and upstream.runs == []


async def test_quote_follows_request_size(tmp_path, upstream, facilitator):
    _, client = build(tmp_path, upstream, facilitator)
    response = await client.post("/t/dataforseo/serp", json={"limit": 30})
    req = decode_header(response.headers["PAYMENT-REQUIRED"])["accepts"][0]
    assert req["amount"] == "0.03"


async def test_paid_run_settles_after_success(tmp_path, upstream, facilitator):
    app, client = build(tmp_path, upstream, facilitator)
    response = await paid_post(client, "/t/exa/search", {"query": "xrpl"})
    assert response.status_code == 200
    assert response.json()["output"] == {"results": [1, 2]}
    assert "billing" not in response.json() and "hints" not in response.json()
    assert decode_header(response.headers["PAYMENT-RESPONSE"])["transaction"] == "ABC123"
    assert facilitator.calls == ["/verify", "/settle"]
    assert upstream.runs == [{"provider": "exa", "endpoint": "/search", "input": {"query": "xrpl"}}]
    rows = app.state.ledger._conn.execute("SELECT * FROM runs").fetchall()
    assert rows[0]["state"] == "settled"
    assert rows[0]["upstream_cost_usd"] == "0.007"
    assert rows[0]["settlement_tx"] == "ABC123"


async def test_failed_upstream_run_is_not_charged(tmp_path, upstream, facilitator):
    app, client = build(tmp_path, upstream, facilitator)
    upstream.run_response = (404, completed(None, http_status=404, cost_micro=0))
    response = await paid_post(client, "/t/exa/search", {"query": "nothing"})
    assert response.status_code == 404
    assert "PAYMENT-RESPONSE" not in response.headers
    assert facilitator.calls == ["/verify"]
    row = app.state.ledger._conn.execute("SELECT state FROM runs").fetchone()
    assert row["state"] == "failed_unbilled"


async def test_rejected_upstream_request_is_not_charged(tmp_path, upstream, facilitator):
    app, client = build(tmp_path, upstream, facilitator)
    upstream.run_response = (400, {"code": 400, "message": "query is required"})
    response = await paid_post(client, "/t/exa/search", {"q": "wrong field"})
    assert response.status_code == 400
    assert response.json()["error"]["message"] == "query is required"
    assert "PAYMENT-RESPONSE" not in response.headers
    assert facilitator.calls == ["/verify"]
    row = app.state.ledger._conn.execute("SELECT state FROM runs").fetchone()
    assert row["state"] == "rejected_unbilled"


async def test_failed_settlement_withholds_output(tmp_path, upstream):
    facilitator = FakeFacilitator(settle_ok=False)
    app, client = build(tmp_path, upstream, facilitator)
    response = await paid_post(client, "/t/exa/search", {"query": "xrpl"})
    assert response.status_code == 402
    assert "output" not in response.json()
    row = app.state.ledger._conn.execute("SELECT state FROM runs").fetchone()
    assert row["state"] == "settle_failed"


async def test_signature_for_another_input_gets_a_fresh_quote(tmp_path, upstream, facilitator):
    _, client = build(tmp_path, upstream, facilitator)
    challenge = await client.post("/t/dataforseo/serp", json={"limit": 1})
    signature = sign(decode_header(challenge.headers["PAYMENT-REQUIRED"]))
    response = await client.post(
        "/t/dataforseo/serp", json={"limit": 100}, headers={"PAYMENT-SIGNATURE": signature}
    )
    assert response.status_code == 402
    assert decode_header(response.headers["PAYMENT-REQUIRED"])["accepts"][0]["amount"] == "0.1"
    assert facilitator.calls == []
    assert upstream.runs == []


async def test_async_run_is_awaited_then_settled(tmp_path, upstream, facilitator):
    _, client = build(tmp_path, upstream, facilitator)
    upstream.run_response = (202, {"runId": "RUN1", "status": "READY"})
    upstream.poll_responses = [{"runId": "RUN1", "status": "RUNNING"}, completed()]
    response = await paid_post(client, "/t/apify/actor", {"searchTerms": ["ai"]})
    assert response.status_code == 200
    assert response.json()["status"] == "COMPLETED"
    assert facilitator.calls == ["/verify", "/settle"]


async def test_long_async_run_returns_a_pollable_job(tmp_path, upstream, facilitator):
    _, client = build(tmp_path, upstream, facilitator, RUN_WAIT_SECONDS=0)
    upstream.run_response = (202, {"runId": "RUN1", "status": "READY"})
    response = await paid_post(client, "/t/apify/actor", {"searchTerms": ["ai"]})
    assert response.status_code == 202
    job = response.json()
    assert facilitator.calls == ["/verify", "/settle"]

    denied = await client.get(f"/jobs/{job['jobId']}", headers={"X-Job-Token": "wrong"})
    assert denied.status_code == 404
    done = await client.get(f"/jobs/{job['jobId']}", headers={"X-Job-Token": job["jobToken"]})
    assert done.status_code == 200
    assert done.json()["output"] == {"results": [1, 2]}


async def test_low_upstream_balance_stops_sales_before_payment(tmp_path, upstream, facilitator):
    upstream.balance = 1.0
    _, client = build(tmp_path, upstream, facilitator)
    response = await client.post("/t/exa/search", json={"query": "x"})
    assert response.status_code == 503
    # Directory probes (GET, empty POST, POST {}) still see the price.
    assert (await client.get("/t/exa/search")).status_code == 402
    assert (await client.post("/t/exa/search")).status_code == 402
    assert (await client.post("/t/exa/search", json={})).status_code == 402


async def test_paid_routes_stay_closed_until_configured(tmp_path, upstream, facilitator):
    _, client = build(tmp_path, upstream, facilitator, X402_XRPL_PAYTO_ADDRESS="")
    response = await client.post("/t/exa/search")
    assert response.status_code == 503
    health = (await client.get("/health")).json()
    assert health["paymentsReady"] is False
    assert "X402_XRPL_PAYTO_ADDRESS is not set" in health["problems"]


async def test_unknown_tool_is_404_not_402(tmp_path, upstream, facilitator):
    _, client = build(tmp_path, upstream, facilitator)
    assert (await client.post("/t/exa/nope")).status_code == 404


async def test_discovery_pages_through_the_catalog(tmp_path, upstream, facilitator):
    _, client = build(tmp_path, upstream, facilitator, CATALOG_PAGE_SIZE=2)
    first = (await client.get("/.well-known/x402")).json()
    assert first["name"] == "Monid Tool Router"
    assert first["total"] == 3 and first["pages"] == 2
    assert first["next"] == "https://tools.example/.well-known/x402?page=2"
    entry = first["resources"][0]
    assert entry["url"].startswith("https://tools.example/t/")
    assert set(entry) >= {
        "name",
        "description",
        "externalId",
        "category",
        "subcategory",
        "upstreamProvider",
        "price",
    }
    second = (await client.get("/.well-known/x402", params={"page": 2})).json()
    assert second["next"] is None and len(second["resources"]) == 1


async def test_catalog_search(tmp_path, upstream, facilitator):
    _, client = build(tmp_path, upstream, facilitator)
    found = (await client.get("/catalog", params={"q": "exa search"})).json()
    assert found["total"] == 1
    assert found["items"][0]["name"] == "Exa Search API"
