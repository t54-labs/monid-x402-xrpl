from __future__ import annotations

import pytest

from .test_app import FakeFacilitator, FakeUpstream, build, decode_header, sign


@pytest.fixture
def upstream():
    return FakeUpstream()


async def signed_request(client, path="/t/exa/search", body=None):
    body = body or {"query": "xrpl"}
    challenge = await client.post(path, json=body)
    assert challenge.status_code == 402
    return {"PAYMENT-SIGNATURE": sign(decode_header(challenge.headers["PAYMENT-REQUIRED"]))}


async def test_failed_settlement_retry_runs_upstream_once(tmp_path, upstream):
    facilitator = FakeFacilitator(settle_ok=False)
    app, client = build(tmp_path, upstream, facilitator)
    headers = await signed_request(client)
    first = await client.post("/t/exa/search", json={"query": "xrpl"}, headers=headers)
    second = await client.post("/t/exa/search", json={"query": "xrpl"}, headers=headers)
    assert first.status_code == second.status_code == 402
    assert len(upstream.runs) == 1
    facilitator.settle_ok = True
    third = await client.post("/t/exa/search", json={"query": "xrpl"}, headers=headers)
    assert third.status_code == 200
    assert third.json()["output"] == {"results": [1, 2]}
    assert len(upstream.runs) == 1
    assert app.state.ledger._conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1


async def test_quote_and_failed_settlement_survive_restart(tmp_path, upstream):
    facilitator = FakeFacilitator(settle_ok=False)
    app, client = build(tmp_path, upstream, facilitator)
    headers = await signed_request(client)
    app.state.ledger.close()
    app, client = build(tmp_path, upstream, facilitator)
    assert (await client.post("/t/exa/search", json={"query": "xrpl"}, headers=headers)).status_code == 402
    app.state.ledger.close()
    app, client = build(tmp_path, upstream, facilitator)

    # Settlement retries must not depend on balance or current catalog pricing.
    async def no_balance_call():
        raise AssertionError("A settlement retry must not call the upstream balance API")

    app.state.upstream.balance_ok = no_balance_call
    app.state.catalogs.catalog.tools[0].price["amount"]["value"] = 99
    facilitator.settle_ok = True
    done = await client.post("/t/exa/search", json={"query": "xrpl"}, headers=headers)
    replay = await client.post("/t/exa/search", json={"query": "xrpl"}, headers=headers)
    assert done.status_code == replay.status_code == 200
    assert done.content == replay.content
    assert done.headers["PAYMENT-RESPONSE"] == replay.headers["PAYMENT-RESPONSE"]
    assert facilitator.calls == ["/verify", "/settle", "/settle"]
    assert len(upstream.runs) == 1


@pytest.mark.parametrize(
    "outcome",
    [
        "timeout",
        "http500",
        "bad_json",
        "list",
        "missing_success",
        "wrong_network",
        "missing_tx",
        "submit_failed:timeout",
        "submit_missing_tx_hash",
        "validation_failed:timeout",
        "no_submission_evidence",
        "attempted",
    ],
)
async def test_unknown_settlement_never_resubmits_after_restart(tmp_path, upstream, outcome):
    import httpx

    facilitator = FakeFacilitator()
    original_post = facilitator.post

    async def post(path, json):
        if path != "/settle":
            return await original_post(path, json)
        facilitator.calls.append(path)
        if outcome == "timeout":
            raise httpx.ReadTimeout("synthetic timeout")
        if outcome == "http500":
            return httpx.Response(500, json={"success": False, "settlementAttempted": False})
        if outcome == "bad_json":
            return httpx.Response(200, text="not json")
        if outcome == "list":
            return httpx.Response(200, json=[])
        if outcome == "missing_success":
            return httpx.Response(200, json={"network": "xrpl:1"})
        if outcome == "wrong_network":
            return httpx.Response(200, json={"success": True, "transaction": "ABC", "network": "xrpl:0"})
        if outcome == "missing_tx":
            return httpx.Response(200, json={"success": True, "network": "xrpl:1"})
        return httpx.Response(
            200,
            json={
                "success": False,
                "network": "xrpl:1",
                "errorReason": "insufficient_funds"
                if outcome in ("no_submission_evidence", "attempted")
                else outcome,
                **(
                    {}
                    if outcome == "no_submission_evidence"
                    else {"settlementAttempted": outcome == "attempted"}
                ),
            },
        )

    facilitator.post = post
    app, client = build(tmp_path, upstream, facilitator)
    headers = await signed_request(client)
    response = await client.post("/t/exa/search", json={"query": "xrpl"}, headers=headers)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "settlement_status_unknown"
    assert "output" not in response.json()
    assert app.state.ledger._conn.execute("SELECT state FROM invoices").fetchone()[0] == "settlement_unknown"
    app.state.ledger.close()
    _, client = build(tmp_path, upstream, facilitator)
    response = await client.post("/t/exa/search", json={"query": "xrpl"}, headers=headers)
    assert response.status_code == 409
    assert facilitator.calls == ["/verify", "/settle"]
    assert len(upstream.runs) == 1


@pytest.mark.parametrize("phase", ["before_post", "after_post", "after_response", "after_settlement"])
async def test_crash_windows_fail_closed_or_resume_only_settlement(tmp_path, upstream, phase, monkeypatch):
    facilitator = FakeFacilitator()
    app, client = build(tmp_path, upstream, facilitator)
    invoices = app.state.x402_invoices
    headers = await signed_request(client)

    async def crash(*args, **kwargs):
        raise RuntimeError("simulated process crash")

    if phase == "before_post":
        monkeypatch.setattr(app.state.ledger, "create", crash)
    elif phase == "after_post":
        monkeypatch.setattr(invoices, "save_response", crash)
    elif phase == "after_response":
        monkeypatch.setattr(invoices, "start_settlement", crash)
    else:
        monkeypatch.setattr(invoices, "settlement_result", crash)
    with pytest.raises(RuntimeError, match="simulated process crash"):
        await client.post("/t/exa/search", json={"query": "xrpl"}, headers=headers)
    app.state.ledger.close()
    _, client = build(tmp_path, upstream, facilitator)
    response = await client.post("/t/exa/search", json={"query": "xrpl"}, headers=headers)
    assert response.status_code == (200 if phase == "after_response" else 409)
    assert len(upstream.runs) == (0 if phase == "before_post" else 1)
    assert facilitator.calls.count("/settle") == (1 if phase in ("after_response", "after_settlement") else 0)


async def test_upstream_post_timeout_never_reexecutes(tmp_path, upstream, monkeypatch):
    import httpx

    facilitator = FakeFacilitator()
    app, client = build(tmp_path, upstream, facilitator)
    headers = await signed_request(client)

    async def timed_out(*args):
        upstream.runs.append({"accepted_but_response_lost": True})
        raise httpx.ReadTimeout("synthetic timeout")

    monkeypatch.setattr(app.state.upstream, "run", timed_out)
    assert (await client.post("/t/exa/search", json={"query": "xrpl"}, headers=headers)).status_code == 409
    app.state.ledger.close()
    _, client = build(tmp_path, upstream, facilitator)
    assert (await client.post("/t/exa/search", json={"query": "xrpl"}, headers=headers)).status_code == 409
    assert facilitator.calls == ["/verify"] and len(upstream.runs) == 1


async def test_concurrent_instances_claim_one_execution(tmp_path, upstream):
    import asyncio

    facilitator = FakeFacilitator()
    app1, client1 = build(tmp_path, upstream, facilitator)
    _, client2 = build(tmp_path, upstream, facilitator)
    headers = await signed_request(client1)
    original_post = facilitator.post
    both_verifying = asyncio.Event()
    verified = 0

    async def post(path, json):
        nonlocal verified
        if path == "/verify":
            verified += 1
            if verified == 2:
                both_verifying.set()
            await both_verifying.wait()
        return await original_post(path, json)

    facilitator.post = post
    responses = await asyncio.wait_for(
        asyncio.gather(
            client1.post("/t/exa/search", json={"query": "xrpl"}, headers=headers),
            client2.post("/t/exa/search", json={"query": "xrpl"}, headers=headers),
        ),
        timeout=3,
    )
    assert sorted(r.status_code for r in responses) == [200, 409]
    assert len(upstream.runs) == 1
    assert app1.state.ledger._conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
    assert facilitator.calls.count("/settle") == 1


async def test_concurrent_settlement_retry_submits_once(tmp_path, upstream):
    import asyncio

    facilitator = FakeFacilitator(settle_ok=False)
    _, client1 = build(tmp_path, upstream, facilitator)
    headers = await signed_request(client1)
    assert (await client1.post("/t/exa/search", json={"query": "xrpl"}, headers=headers)).status_code == 402
    _, client2 = build(tmp_path, upstream, facilitator)
    entered = asyncio.Event()
    release = asyncio.Event()
    original_post = facilitator.post
    facilitator.settle_ok = True

    async def post(path, json):
        if path == "/settle":
            entered.set()
            await release.wait()
        return await original_post(path, json)

    facilitator.post = post
    task = asyncio.create_task(client1.post("/t/exa/search", json={"query": "xrpl"}, headers=headers))
    await asyncio.wait_for(entered.wait(), timeout=3)
    other = await client2.post("/t/exa/search", json={"query": "xrpl"}, headers=headers)
    assert other.status_code == 409
    release.set()
    assert (await task).status_code == 200
    assert facilitator.calls.count("/settle") == 2  # first failed attempt plus one retry
    assert len(upstream.runs) == 1


@pytest.mark.parametrize("phase", ["execution", "settlement"])
async def test_cancellation_does_not_unlock_invoice(tmp_path, upstream, monkeypatch, phase):
    import asyncio
    import contextlib

    facilitator = FakeFacilitator()
    app, client = build(tmp_path, upstream, facilitator)
    headers = await signed_request(client)
    entered = asyncio.Event()

    async def hang(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    if phase == "execution":
        monkeypatch.setattr(app.state.upstream, "run", hang)
    else:
        original_post = facilitator.post

        async def post(path, json):
            if path == "/settle":
                facilitator.calls.append(path)
                return await hang()
            return await original_post(path, json)

        facilitator.post = post
    task = asyncio.create_task(client.post("/t/exa/search", json={"query": "xrpl"}, headers=headers))
    await asyncio.wait_for(entered.wait(), timeout=3)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    app.state.ledger.close()
    _, client = build(tmp_path, upstream, facilitator)
    result = await client.post("/t/exa/search", json={"query": "xrpl"}, headers=headers)
    assert result.status_code == 409
    assert result.json()["error"]["code"] == (
        "execution_status_unknown" if phase == "execution" else "settlement_status_unknown"
    )
    assert facilitator.calls.count("/settle") == (0 if phase == "execution" else 1)


async def test_async_job_survives_failure_restart_and_poll_timeout(tmp_path, upstream, monkeypatch):
    import httpx

    facilitator = FakeFacilitator(settle_ok=False)
    app, client = build(tmp_path, upstream, facilitator)
    upstream.run_response = (202, {"runId": "RUN1", "status": "RUNNING"})

    async def poll_timeout(*args):
        row = app.state.ledger._conn.execute("SELECT upstream_run_id FROM runs").fetchone()
        assert row[0] == "RUN1"  # committed before the first poll
        raise httpx.ReadTimeout("synthetic poll timeout")

    monkeypatch.setattr(app.state.upstream, "wait", poll_timeout)
    headers = await signed_request(client, "/t/apify/actor", {"searchTerms": ["ai"]})
    result = await client.post("/t/apify/actor", json={"searchTerms": ["ai"]}, headers=headers)
    assert result.status_code == 402 and "jobToken" not in result.json()
    row = dict(app.state.ledger._conn.execute("SELECT * FROM runs").fetchone())
    assert (
        await client.get(f"/jobs/{row['id']}", headers={"X-Job-Token": row["job_token"]})
    ).status_code == 404
    app.state.ledger.close()
    _, client = build(tmp_path, upstream, facilitator)
    facilitator.settle_ok = True
    result = await client.post("/t/apify/actor", json={"searchTerms": ["ai"]}, headers=headers)
    assert result.status_code == 202
    assert result.json()["jobId"] == row["id"] and result.json()["jobToken"] == row["job_token"]
    polled = await client.get(result.json()["poll"], headers={"X-Job-Token": row["job_token"]})
    assert polled.status_code == 200 and "output" in polled.json()
    assert len(upstream.runs) == 1


@pytest.mark.parametrize("mutation", ["body", "endpoint", "provider", "payment", "issuer", "invoice"])
async def test_replay_cannot_change_bound_request_or_payment(tmp_path, upstream, mutation):
    from dataclasses import replace

    from gateway.catalog import Catalog

    facilitator = FakeFacilitator(settle_ok=False)
    app, client = build(tmp_path, upstream, facilitator)
    tool = app.state.catalogs.catalog.by_route("/t/exa/search")
    app.state.catalogs.catalog = Catalog(
        tools=[tool, replace(tool, endpoint="/other"), replace(tool, provider="other")]
    )
    headers = await signed_request(client)
    assert (await client.post("/t/exa/search", json={"query": "xrpl"}, headers=headers)).status_code == 402
    path, body = "/t/exa/search", {"query": "xrpl"}
    if mutation == "body":
        body = {"query": "different"}
    elif mutation == "endpoint":
        path = "/t/exa/other"
    elif mutation == "provider":
        path = "/t/other/search"
    else:
        import base64
        import json

        envelope = decode_header(headers["PAYMENT-SIGNATURE"])
        if mutation == "payment":
            envelope["payload"]["signedTxBlob"] = "FORGED"
        elif mutation == "issuer":
            envelope["accepted"]["extra"]["issuer"] = "rWrongIssuer"
        else:
            envelope["payload"]["invoiceId"] = "another-invoice"
        headers = {"PAYMENT-SIGNATURE": base64.b64encode(json.dumps(envelope).encode()).decode()}
    result = await client.post(path, json=body, headers=headers)
    assert result.status_code in (400, 409)
    assert "output" not in result.json()
    assert facilitator.calls == ["/verify", "/settle"] and len(upstream.runs) == 1


async def test_flat_price_input_change_and_probe_issue_new_bound_quote(tmp_path, upstream):
    facilitator = FakeFacilitator()
    _, client = build(tmp_path, upstream, facilitator)
    headers = await signed_request(client)
    changed = await client.post("/t/exa/search", json={"query": "different"}, headers=headers)
    assert changed.status_code == 402
    assert facilitator.calls == [] and upstream.runs == []
    # Even an unsigned discovery probe cannot authorize arbitrary flat-price input.
    probe = await client.get("/t/exa/search")
    headers = {"PAYMENT-SIGNATURE": sign(probe.json())}
    assert (await client.post("/t/exa/search", json={"query": "xrpl"}, headers=headers)).status_code == 402
    assert upstream.runs == []


async def test_canonical_json_replay_returns_cached_output(tmp_path, upstream):
    facilitator = FakeFacilitator()
    _, client = build(tmp_path, upstream, facilitator)
    body = {"query": "xrpl", "options": {"a": 1, "b": 2}}
    headers = await signed_request(client, body=body)
    first = await client.post("/t/exa/search", json=body, headers=headers)
    replay = await client.post(
        "/t/exa/search",
        content='{"options":{"b":2,"a":1}, "query":"xrpl"}',
        headers={**headers, "content-type": "application/json"},
    )
    assert first.status_code == replay.status_code == 200
    assert first.content == replay.content
    assert len(upstream.runs) == 1 and facilitator.calls == ["/verify", "/settle"]


async def test_expiry_cannot_unlock_active_or_finished_execution(tmp_path, upstream, monkeypatch):
    import asyncio

    facilitator = FakeFacilitator()
    app, client = build(tmp_path, upstream, facilitator)
    headers = await signed_request(client)
    entered, release = asyncio.Event(), asyncio.Event()
    original_run = app.state.upstream.run

    async def run(*args):
        entered.set()
        await release.wait()
        return await original_run(*args)

    monkeypatch.setattr(app.state.upstream, "run", run)
    task = asyncio.create_task(client.post("/t/exa/search", json={"query": "xrpl"}, headers=headers))
    await asyncio.wait_for(entered.wait(), timeout=3)
    await app.state.ledger._run("UPDATE invoices SET expires_at = 0", ())
    await signed_request(client)  # quote cleanup must leave the execution tombstone
    assert (await client.post("/t/exa/search", json={"query": "xrpl"}, headers=headers)).status_code == 409
    release.set()
    assert (await task).status_code == 200
    assert (await client.post("/t/exa/search", json={"query": "xrpl"}, headers=headers)).status_code == 200
    assert len(upstream.runs) == 1


async def test_expired_unused_quote_does_not_execute(tmp_path, upstream):
    facilitator = FakeFacilitator()
    app, client = build(tmp_path, upstream, facilitator)
    headers = await signed_request(client)
    await app.state.ledger._run("UPDATE invoices SET expires_at = 0", ())
    assert (await client.post("/t/exa/search", json={"query": "xrpl"}, headers=headers)).status_code == 409
    assert facilitator.calls == [] and upstream.runs == []


async def test_verify_failure_can_retry_before_any_execution(tmp_path, upstream):
    facilitator = FakeFacilitator(verify_ok=False)
    _, client = build(tmp_path, upstream, facilitator)
    headers = await signed_request(client)
    assert (await client.post("/t/exa/search", json={"query": "xrpl"}, headers=headers)).status_code == 402
    assert upstream.runs == []
    facilitator.verify_ok = True
    assert (await client.post("/t/exa/search", json={"query": "xrpl"}, headers=headers)).status_code == 200
    assert len(upstream.runs) == 1


@pytest.mark.parametrize("settled", [True, False])
async def test_explicit_reconciliation_only_resolves_settlement(tmp_path, upstream, settled):
    import httpx

    facilitator = FakeFacilitator()
    original_post = facilitator.post

    async def post(path, json):
        if path == "/settle":
            facilitator.calls.append(path)
            raise httpx.ReadTimeout("synthetic timeout")
        return await original_post(path, json)

    facilitator.post = post
    app, client = build(tmp_path, upstream, facilitator)
    headers = await signed_request(client)
    assert (await client.post("/t/exa/search", json={"query": "xrpl"}, headers=headers)).status_code == 409
    invoice_id = decode_header(headers["PAYMENT-SIGNATURE"])["payload"]["invoiceId"]
    settlement = {"success": True, "transaction": "CONFIRMED", "payer": "rBuyer", "network": "xrpl:1"}
    with pytest.raises(ValueError, match="evidence"):
        await app.state.x402_invoices.reconcile(invoice_id, evidence="", settlement=settlement)
    await app.state.x402_invoices.reconcile(
        invoice_id, evidence="synthetic-test-evidence", settlement=settlement if settled else None
    )
    facilitator.post = original_post
    response = await client.post("/t/exa/search", json={"query": "xrpl"}, headers=headers)
    assert response.status_code == 200 and len(upstream.runs) == 1
    assert facilitator.calls.count("/settle") == (1 if settled else 2)
    with pytest.raises(ValueError, match="Only an unknown settlement"):
        await app.state.x402_invoices.reconcile(invoice_id, evidence="cannot-reset", settlement=None)
    assert app.state.ledger._conn.execute("SELECT state FROM runs").fetchone()[0] == "settled"


async def test_catalog_refresh_cannot_change_verified_execution_target(tmp_path, upstream):
    from dataclasses import replace

    from gateway.catalog import Catalog

    facilitator = FakeFacilitator()
    app, client = build(tmp_path, upstream, facilitator)
    headers = await signed_request(client)
    original_post = facilitator.post
    original = app.state.catalogs.catalog.by_route("/t/exa/search")

    async def post(path, json):
        if path == "/verify":
            # Simulate a catalog refresh during an external verification await.
            app.state.catalogs.catalog = Catalog(tools=[])
        return await original_post(path, json)

    facilitator.post = post
    response = await client.post("/t/exa/search", json={"query": "xrpl"}, headers=headers)
    assert response.status_code == 200
    assert upstream.runs[0]["provider"] == original.provider
    assert upstream.runs[0]["endpoint"] == original.endpoint
    app.state.catalogs.catalog = Catalog(tools=[replace(original, price={"type": "UNQUOTABLE"})])
    assert (await client.post("/t/exa/search", json={"query": "xrpl"}, headers=headers)).status_code == 200
    assert len(upstream.runs) == 1


@pytest.mark.parametrize("run_id", [None, "", "   "])
async def test_no_usable_run_id_is_not_a_billable_job(tmp_path, upstream, run_id):
    facilitator = FakeFacilitator()
    _, client = build(tmp_path, upstream, facilitator)
    upstream.run_response = (200, {"status": "RUNNING", "runId": run_id})
    headers = await signed_request(client)
    for _ in range(2):
        response = await client.post("/t/exa/search", json={"query": "xrpl"}, headers=headers)
        assert response.status_code == 409
        assert "jobToken" not in response.json()
    assert facilitator.calls == ["/verify"] and len(upstream.runs) == 1


async def test_failed_upstream_result_is_durably_unbilled(tmp_path, upstream):
    from .test_app import completed

    facilitator = FakeFacilitator()
    app, client = build(tmp_path, upstream, facilitator)
    headers = await signed_request(client)
    upstream.run_response = (404, completed(None, http_status=404, cost_micro=0))
    first = await client.post("/t/exa/search", json={"query": "xrpl"}, headers=headers)
    app.state.ledger.close()
    _, client = build(tmp_path, upstream, facilitator)
    again = await client.post("/t/exa/search", json={"query": "xrpl"}, headers=headers)
    assert first.status_code == again.status_code == 404 and first.content == again.content
    assert facilitator.calls == ["/verify"] and len(upstream.runs) == 1


async def test_legacy_unpersisted_invoice_is_rejected(tmp_path, upstream):
    facilitator = FakeFacilitator()
    app, client = build(tmp_path, upstream, facilitator)
    headers = await signed_request(client)
    invoice_id = decode_header(headers["PAYMENT-SIGNATURE"])["payload"]["invoiceId"]
    await app.state.ledger.create(
        "legacy", invoice_id=invoice_id, tool="exa#/search", quote_rlusd="0.007", state="settle_failed"
    )
    # Model upgrade: old runs exist, but the invoice was only in the old process's memory.
    await app.state.ledger._run("DELETE FROM invoices", ())
    response = await client.post("/t/exa/search", json={"query": "xrpl"}, headers=headers)
    assert response.status_code == 409 and response.json()["error"]["code"] == "unknown_invoice"
    assert facilitator.calls == [] and upstream.runs == []


async def test_reconciliation_cli_changes_only_saved_unknown_settlement(tmp_path, upstream, monkeypatch):
    import sys

    from scripts.reconcile_invoice import main

    facilitator = FakeFacilitator(settle_ok=False)
    app, client = build(tmp_path, upstream, facilitator)
    headers = await signed_request(client)
    await client.post("/t/exa/search", json={"query": "xrpl"}, headers=headers)
    invoice_id = decode_header(headers["PAYMENT-SIGNATURE"])["payload"]["invoiceId"]
    await app.state.ledger._run("UPDATE invoices SET state = 'settlement_unknown'", ())
    app.state.ledger.close()
    argv = [
        "reconcile_invoice",
        "--database",
        str(tmp_path / "gateway.sqlite3"),
        "--invoice",
        invoice_id,
        "--outcome",
        "settled",
        "--transaction",
        "CONFIRMED",
        "--payer",
        "rBuyer",
        "--evidence",
        "synthetic-incident-reference",
        "--gateway-stopped",
    ]
    monkeypatch.setattr(sys, "argv", argv)
    # Run the synchronous command in a worker because it owns its own asyncio loop.
    import asyncio

    await asyncio.to_thread(main)
    app, client = build(tmp_path, upstream, facilitator)
    response = await client.post("/t/exa/search", json={"query": "xrpl"}, headers=headers)
    assert response.status_code == 200 and len(upstream.runs) == 1
    settlement = decode_header(response.headers["PAYMENT-RESPONSE"])
    assert settlement["transaction"] == "CONFIRMED"
    assert (
        "synthetic-incident-reference"
        in app.state.ledger._conn.execute("SELECT reconciliation FROM invoices").fetchone()[0]
    )


async def test_reconciliation_cannot_reopen_unknown_execution(tmp_path, upstream):
    facilitator = FakeFacilitator()
    app, client = build(tmp_path, upstream, facilitator)
    headers = await signed_request(client)
    invoice_id = decode_header(headers["PAYMENT-SIGNATURE"])["payload"]["invoiceId"]
    assert await app.state.x402_invoices.begin(invoice_id, "synthetic-hash", "synthetic-run")
    with pytest.raises(ValueError, match="Only an unknown settlement"):
        await app.state.x402_invoices.reconcile(invoice_id, evidence="synthetic-evidence", settlement=None)
    assert (await app.state.x402_invoices.record(invoice_id))["state"] == "executing"


@pytest.mark.parametrize("state, expected", [("settled", 200), ("settle_failed", 404), ("running", 404)])
async def test_legacy_job_polling_respects_recorded_settlement(tmp_path, upstream, state, expected):
    facilitator = FakeFacilitator()
    app, client = build(tmp_path, upstream, facilitator)
    await app.state.ledger.create(
        "legacy-job",
        invoice_id="legacy-invoice",
        tool="exa#/search",
        quote_rlusd="0.007",
        state=state,
        upstream_run_id="RUN1",
        job_token="test-token",
    )
    response = await client.get("/jobs/legacy-job", headers={"X-Job-Token": "test-token"})
    assert response.status_code == expected
    assert facilitator.calls == [] and upstream.runs == []


@pytest.mark.parametrize("initial_settled", [True, False])
async def test_delisted_tool_can_recover_saved_result(tmp_path, upstream, initial_settled):
    from gateway.catalog import Catalog

    facilitator = FakeFacilitator(settle_ok=initial_settled)
    app, client = build(tmp_path, upstream, facilitator)
    headers = await signed_request(client)
    first = await client.post("/t/exa/search", json={"query": "xrpl"}, headers=headers)
    assert first.status_code == (200 if initial_settled else 402)
    app.state.ledger.close()
    app, client = build(tmp_path, upstream, facilitator)
    app.state.catalogs.catalog = Catalog(tools=[])
    facilitator.settle_ok = True
    response = await client.post("/t/exa/search", json={"query": "xrpl"}, headers=headers)
    assert response.status_code == 200 and response.json()["output"] == {"results": [1, 2]}
    assert len(upstream.runs) == 1
    assert (await client.post("/t/exa/search", json={"query": "xrpl"})).status_code == 404


@pytest.mark.parametrize(
    "poll_kind", ["http404", "http500", "malformed", "different_id", "terminal_without_id"]
)
async def test_async_poll_error_preserves_original_accepted_job(tmp_path, upstream, monkeypatch, poll_kind):
    from gateway.upstream import RunResult

    facilitator = FakeFacilitator()
    app, client = build(tmp_path, upstream, facilitator)
    upstream.run_response = (202, {"runId": "ACCEPTED-JOB", "status": "RUNNING"})

    async def poll(run_id):
        assert run_id == "ACCEPTED-JOB"
        if poll_kind.startswith("http"):
            return RunResult(int(poll_kind[4:]), {"message": "poll failed"})
        if poll_kind == "malformed":
            return RunResult(200, {"message": "not a run"})
        if poll_kind == "different_id":
            return RunResult(200, {"runId": "ANOTHER-JOB", "status": "COMPLETED", "output": "wrong job"})
        return RunResult(200, {"status": "COMPLETED", "output": {"done": True}})

    monkeypatch.setattr(app.state.upstream, "get_run", poll)
    headers = await signed_request(client)
    response = await client.post("/t/exa/search", json={"query": "xrpl"}, headers=headers)
    assert response.status_code == (200 if poll_kind == "terminal_without_id" else 202)
    assert "wrong job" not in response.text
    row = app.state.ledger._conn.execute("SELECT * FROM runs").fetchone()
    assert row["upstream_run_id"] == "ACCEPTED-JOB" and row["state"] == "settled"
    assert facilitator.calls == ["/verify", "/settle"] and len(upstream.runs) == 1


async def test_settlement_and_run_ledger_commit_atomically(tmp_path, upstream):
    import sqlite3

    facilitator = FakeFacilitator()
    app, client = build(tmp_path, upstream, facilitator)
    headers = await signed_request(client)
    await app.state.ledger._run(
        "CREATE TRIGGER fail_settled BEFORE UPDATE ON runs WHEN NEW.state = 'settled' "
        "BEGIN SELECT RAISE(ABORT, 'synthetic commit failure'); END",
        (),
    )
    with pytest.raises(sqlite3.IntegrityError, match="synthetic commit failure"):
        await client.post("/t/exa/search", json={"query": "xrpl"}, headers=headers)
    assert app.state.ledger._conn.execute("SELECT state FROM invoices").fetchone()[0] == "settling"
    app.state.ledger.close()
    _, client = build(tmp_path, upstream, facilitator)
    response = await client.post("/t/exa/search", json={"query": "xrpl"}, headers=headers)
    assert response.status_code == 409 and len(upstream.runs) == 1
    assert facilitator.calls == ["/verify", "/settle"]


async def test_production_default_reconciles_even_claimed_non_submission(tmp_path, upstream):
    from .conftest import make_settings

    assert make_settings(tmp_path).X402_ALLOW_UNSUBMITTED_RETRY is False
    facilitator = FakeFacilitator(settle_ok=False)
    app, client = build(tmp_path, upstream, facilitator, X402_ALLOW_UNSUBMITTED_RETRY=False)
    headers = await signed_request(client)
    first = await client.post("/t/exa/search", json={"query": "xrpl"}, headers=headers)
    assert first.status_code == 409 and "output" not in first.json()
    app.state.ledger.close()
    _, client = build(tmp_path, upstream, facilitator, X402_ALLOW_UNSUBMITTED_RETRY=False)
    replay = await client.post("/t/exa/search", json={"query": "xrpl"}, headers=headers)
    assert replay.status_code == 409
    assert len(upstream.runs) == 1 and facilitator.calls == ["/verify", "/settle"]


@pytest.mark.parametrize(
    "reason",
    [
        "verify_failed:invalid_payload",
        "submit_failed:timeout",
        "submit_missing_tx_hash",
        "submit_engine_result:tecUNFUNDED_PAYMENT:unfunded",
        "validation_failed:validation_timeout",
        "tx_failed:tecUNFUNDED_PAYMENT",
    ],
)
async def test_available_facilitator_failure_shapes_are_closed(tmp_path, upstream, reason):
    import httpx

    facilitator = FakeFacilitator()
    original = facilitator.post

    async def post(path, json):
        if path == "/settle":
            facilitator.calls.append(path)
            return httpx.Response(
                200,
                json={
                    "success": False,
                    "errorReason": reason,
                    "transaction": "",
                    "network": "xrpl:1",
                    "payer": "rBuyer",
                },
            )
        return await original(path, json)

    facilitator.post = post
    # Even an explicitly enabled future contract cannot reinterpret these legacy shapes.
    _, client = build(tmp_path, upstream, facilitator, X402_ALLOW_UNSUBMITTED_RETRY=True)
    headers = await signed_request(client)
    for _ in range(2):
        response = await client.post("/t/exa/search", json={"query": "xrpl"}, headers=headers)
        assert response.status_code == 409 and "output" not in response.json()
    assert len(upstream.runs) == 1 and facilitator.calls == ["/verify", "/settle"]


@pytest.mark.parametrize("state", ["quoted", "executing", "ready", "unbilled", "settled"])
async def test_reconciliation_refuses_known_or_unexecuted_states(tmp_path, upstream, state):
    facilitator = FakeFacilitator()
    app, client = build(tmp_path, upstream, facilitator)
    headers = await signed_request(client)
    paid = await client.post("/t/exa/search", json={"query": "xrpl"}, headers=headers)
    assert paid.status_code == 200
    invoice_id = decode_header(headers["PAYMENT-SIGNATURE"])["payload"]["invoiceId"]
    await app.state.ledger._run("UPDATE invoices SET state = ? WHERE id = ?", (state, invoice_id))
    before_invoice = await app.state.x402_invoices.record(invoice_id)
    before_run = dict(app.state.ledger._conn.execute("SELECT * FROM runs").fetchone())
    with pytest.raises(ValueError, match="Only an unknown settlement"):
        await app.state.x402_invoices.reconcile(invoice_id, evidence="synthetic-proof", settlement=None)
    assert await app.state.x402_invoices.record(invoice_id) == before_invoice
    assert dict(app.state.ledger._conn.execute("SELECT * FROM runs").fetchone()) == before_run
    assert facilitator.calls == ["/verify", "/settle"] and len(upstream.runs) == 1


@pytest.mark.parametrize(
    "field, value",
    [
        ("amount", "999"),
        ("network", "xrpl:0"),
        ("asset", "XRP"),
        ("payTo", "rAnotherAddress"),
        ("maxTimeoutSeconds", 100),
        ("sourceTag", 999),
        ("issuer", "rWrongIssuer"),
    ],
)
async def test_issued_requirements_cannot_be_changed_before_execution(tmp_path, upstream, field, value):
    import base64
    import json

    facilitator = FakeFacilitator()
    _, client = build(tmp_path, upstream, facilitator)
    original = await signed_request(client)
    envelope = decode_header(original["PAYMENT-SIGNATURE"])
    requirements = envelope["accepted"]
    (requirements["extra"] if field in ("sourceTag", "issuer") else requirements)[field] = value
    forged = base64.b64encode(json.dumps(envelope).encode()).decode()
    response = await client.post(
        "/t/exa/search", json={"query": "xrpl"}, headers={"PAYMENT-SIGNATURE": forged}
    )
    assert response.status_code == 409
    assert facilitator.calls == [] and upstream.runs == []
    # A forged attempt cannot consume or pin the legitimate quote.
    assert (await client.post("/t/exa/search", json={"query": "xrpl"}, headers=original)).status_code == 200
    assert len(upstream.runs) == 1


@pytest.mark.parametrize("bad", ["not-base64-json", "bnVsbA==", "W10=", "e30="])
async def test_malformed_payment_never_claims_execution(tmp_path, upstream, bad):
    facilitator = FakeFacilitator()
    _, client = build(tmp_path, upstream, facilitator)
    response = await client.post("/t/exa/search", json={"query": "xrpl"}, headers={"PAYMENT-SIGNATURE": bad})
    assert response.status_code == 400
    assert upstream.runs == [] and facilitator.calls == []
