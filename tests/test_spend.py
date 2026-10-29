"""Synthetic spend admission, crash safety and financial-state preservation."""

from __future__ import annotations

import asyncio
import base64
import json
import sqlite3
import threading
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from gateway.config import Settings
from gateway.spend import SpendDenied, SpendGuard
from gateway.store import SCHEMA, RunLedger
from scripts.ledger_snapshot import backup, rehearse, verify_snapshot

from .test_app import FakeFacilitator, FakeUpstream, build, decode_header, paid_post, sign

BODY = {"query": "synthetic"}
ROUTE = "/t/exa/search"


def allocations(app):
    return [dict(r) for r in app.state.ledger._conn.execute("SELECT * FROM spend_reservations")]


async def quote(app, client, body=None):
    body = BODY if body is None else body
    response = await client.post(ROUTE, json=body)
    assert response.status_code == 402
    envelope = decode_header(sign(response.json()))
    invoice = envelope["payload"]["invoiceId"]
    row = await app.state.x402_invoices.record(invoice)
    permit = app.state.spend.permit(app.state.catalogs.catalog.by_route(ROUTE), body)
    return envelope, (
        invoice,
        "synthetic-proof-hash",
        envelope["payload"]["signedTxBlob"],
        "run-" + invoice,
        row["binding"],
        permit,
    )


def headers(envelope):
    return {"PAYMENT-SIGNATURE": base64.b64encode(json.dumps(envelope).encode()).decode()}


@pytest.mark.parametrize("value", [True, False, -1, 1.5, "1e6", "-1", "1.0", 2**53, "9007199254740992"])
def test_invalid_budget_never_coerces(value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, MONID_SPEND_BUDGET_MICROS=value)


def test_environment_budget_parsing_and_closed_default(monkeypatch):
    assert Settings(_env_file=None).MONID_SPEND_BUDGET_MICROS == 0
    monkeypatch.setenv("MONID_SPEND_BUDGET_MICROS", "100000")
    assert Settings(_env_file=None).MONID_SPEND_BUDGET_MICROS == 100_000


@pytest.mark.parametrize(
    "override",
    [
        {"MONID_SPEND_BUDGET_MICROS": 0},
        {"MONID_SPEND_BUDGET_ID": ""},
        {"MONID_SPEND_ACCOUNT_ID": ""},
        {"MONID_TOOL_POLICIES_PATH": ""},
        {"X402_PAYMENT_ENABLED": False},
    ],
)
async def test_default_denial_before_verification_or_execution(tmp_path, override):
    upstream, facilitator = FakeUpstream(), FakeFacilitator()
    app, client = build(tmp_path, upstream, facilitator, **override)
    response = await client.post(ROUTE, json=BODY)
    assert response.status_code == 503
    assert not (await client.get("/health")).json()["paymentsReady"]
    assert upstream.runs == [] and facilitator.calls == [] and allocations(app) == []


@pytest.mark.parametrize(
    "body",
    [
        {"query": "x" * 1001},
        {"unknown": 1},
        {"limit": True},
        {"limit": 101},
        {"searchTerms": ["a"] * 6},
        {"options": {"unexpected": 1}},
        {"options": {"a": -1}},
    ],
)
async def test_bounded_native_input_rejected_before_dispatch(tmp_path, body):
    upstream = FakeUpstream()
    app, client = build(tmp_path, upstream, FakeFacilitator())
    assert (await client.post(ROUTE, json=body)).status_code == 503
    assert upstream.runs == [] and allocations(app) == []


@pytest.mark.parametrize(
    "invalid",
    [
        '{"version":1,"version":1,"tools":[]}',
        "[]",
        '{"version":1,"tools":[]}',
    ],
)
async def test_malformed_policy_fails_closed(tmp_path, invalid):
    app, client = build(tmp_path, FakeUpstream(), FakeFacilitator())
    Path(app.state.spend.settings.MONID_TOOL_POLICIES_PATH).write_text(invalid)
    assert (await client.post(ROUTE, json=BODY)).status_code == 503


async def test_exact_boundary_concurrent_connections_and_replay(tmp_path):
    upstream = FakeUpstream()
    app, client = build(tmp_path, upstream, FakeFacilitator(), MONID_SPEND_BUDGET_MICROS=100_000)
    first, claim1 = await quote(app, client)
    second, claim2 = await quote(app, client)
    connection = RunLedger(app.state.spend.settings.DATABASE_PATH)
    other = SpendGuard(app.state.spend.settings, connection)
    try:
        outcomes = await asyncio.gather(
            app.state.spend.reserve(*claim1), other.reserve(*claim2), return_exceptions=True
        )
        assert sum(v is None for v in outcomes) == 1
        assert sum(isinstance(v, SpendDenied) for v in outcomes) == 1
        assert sum(r["max_micros"] for r in allocations(app)) == 100_000
        assert not await app.state.spend.ready(app.state.catalogs.catalog.tools)
        winner = claim1 if outcomes[0] is None else claim2
        with pytest.raises(SpendDenied):
            await other.reserve(*winner)
        # A crash after claim but before dispatch does not let an HTTP retry run it.
        envelope = first if outcomes[0] is None else second
        assert (await client.post(ROUTE, json=BODY, headers=headers(envelope))).status_code == 409
        assert upstream.runs == []
    finally:
        connection.close()


async def test_same_signed_proof_on_different_invoice_is_atomic_denial(tmp_path):
    app, client = build(tmp_path, FakeUpstream(), FakeFacilitator())
    _, one = await quote(app, client)
    _, two = await quote(app, client)
    await app.state.spend.reserve(*one)
    two = (*two[:2], one[2].lower(), *two[3:])
    with pytest.raises(SpendDenied, match="conflicting"):
        await app.state.spend.reserve(*two)
    assert len(allocations(app)) == 1
    assert (await app.state.x402_invoices.record(two[0]))["state"] == "quoted"


@pytest.mark.parametrize("changed", ["policy", "account", "key", "base", "expiry"])
async def test_change_during_verification_rejects_before_run_and_retains_reservation(tmp_path, changed):
    upstream, facilitator = FakeUpstream(), FakeFacilitator()
    app, client = build(tmp_path, upstream, facilitator)
    envelope, _ = await quote(app, client)
    post = facilitator.post

    async def verifying(path, json):
        result = await post(path, json)
        if path == "/verify":
            settings = app.state.spend.settings
            if changed in {"policy", "expiry"}:
                policy_path = Path(settings.MONID_TOOL_POLICIES_PATH)
                import json as encoding

                policy = encoding.loads(policy_path.read_text())
                for tool_policy in policy["tools"]:
                    if tool_policy["provider"] == "exa":
                        tool_policy["expires_at" if changed == "expiry" else "max_cost_micros"] = 1
                policy_path.write_text(encoding.dumps(policy))
            else:
                setattr(
                    settings,
                    {
                        "account": "MONID_SPEND_ACCOUNT_ID",
                        "key": "MONID_API_KEY",
                        "base": "MONID_API_BASE_URL",
                    }[changed],
                    "changed-synthetic",
                )
        return result

    facilitator.post = verifying
    result = await client.post(ROUTE, json=BODY, headers=headers(envelope))
    assert result.status_code == 503
    assert upstream.runs == [] and facilitator.calls == ["/verify"]
    assert allocations(app)[0]["state"] == "rejected_before_dispatch"
    assert allocations(app)[0]["max_micros"] == 100_000


async def test_queued_policy_removal_rejects_before_dispatch(tmp_path):
    import inspect

    upstream = FakeUpstream()
    app, client = build(tmp_path, upstream, FakeFacilitator(), MAX_CONCURRENT_RUNS=1)
    # Current ingress rejects excess concurrency before claims. Hold the inner
    # slot explicitly to exercise its defensive revalidation if work ever queues.
    handler = next(r.endpoint for r in app.routes if r.name == "run_tool")
    slots = inspect.getclosurevars(handler).nonlocals["run_slots"]
    envelope, _ = await quote(app, client)
    await slots.acquire()
    task = asyncio.create_task(client.post(ROUTE, json=BODY, headers=headers(envelope)))
    try:
        for _ in range(200):
            if allocations(app):
                break
            await asyncio.sleep(0.005)
        assert len(allocations(app)) == 1
        Path(app.state.spend.settings.MONID_TOOL_POLICIES_PATH).unlink()
    finally:
        slots.release()
    assert (await task).status_code == 503
    assert allocations(app)[0]["state"] == "rejected_before_dispatch"
    assert upstream.runs == []


@pytest.mark.parametrize("outcome", ["timeout", "unknown", "success", "rejected", "settlement_unknown"])
async def test_irreversible_or_uncertain_outcome_never_refunds(tmp_path, outcome):
    upstream = FakeUpstream()
    facilitator = FakeFacilitator(settle_ok=outcome != "settlement_unknown")
    app, client = build(
        tmp_path, upstream, facilitator, MONID_SPEND_BUDGET_MICROS=100_000, X402_ALLOW_UNSUBMITTED_RETRY=False
    )
    if outcome == "timeout":

        async def timeout(*args):
            upstream.runs.append({"synthetic_attempt": True})
            raise httpx.ReadTimeout("synthetic timeout")

        app.state.upstream.run = timeout
    elif outcome == "unknown":
        upstream.run_response = (502, {})
    elif outcome == "rejected":
        upstream.run_response = (400, {"message": "synthetic rejection"})
    envelope, _ = await quote(app, client)
    response = await client.post(ROUTE, json=BODY, headers=headers(envelope))
    assert response.status_code in {200, 400, 409, 502}
    assert len(upstream.runs) == 1
    assert [(r["state"], r["max_micros"]) for r in allocations(app)] == [("dispatch_unknown", 100_000)]
    await client.post(ROUTE, json=BODY, headers=headers(envelope))
    assert len(upstream.runs) == 1
    assert not await app.state.spend.ready(app.state.catalogs.catalog.tools)
    # New quote cannot reuse the occupied lifetime budget, even after a restart.
    restarted, client2 = build(tmp_path, upstream, FakeFacilitator(), MONID_SPEND_BUDGET_MICROS=100_000)
    await paid_post(client2, ROUTE, {"query": "another"})
    assert len(upstream.runs) == 1 and len(allocations(restarted)) == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("MONID_SPEND_BUDGET_ID", "new-budget"),
        ("MONID_SPEND_ACCOUNT_ID", "new-account"),
        ("MONID_SPEND_BUDGET_MICROS", 20_000_000),
    ],
)
async def test_restart_cannot_reset_budget_identity_or_ceiling(tmp_path, field, value):
    upstream = FakeUpstream()
    app, client = build(tmp_path, upstream, FakeFacilitator())
    assert (await paid_post(client, ROUTE, BODY)).status_code == 200
    app2, client2 = build(tmp_path, upstream, FakeFacilitator(), **{field: value})
    assert not await app2.state.spend.ready(app2.state.catalogs.catalog.tools)
    assert (await paid_post(client2, ROUTE, BODY)).status_code == 409
    assert len(upstream.runs) == 1 and allocations(app2) == allocations(app)


async def test_additive_migration_and_unknown_legacy_work_stays_closed(tmp_path):
    db_path = tmp_path / "gateway.sqlite3"
    with sqlite3.connect(db_path) as db:
        db.executescript(SCHEMA)
        db.execute(
            "INSERT INTO runs (id,tool,quote_rlusd,quote_estimated,state,created_at,updated_at) "
            "VALUES ('legacy','synthetic','1',0,'execution_unknown',1,1)"
        )
        db.execute(
            "INSERT INTO invoices (id,requirements,binding,target,expires_at,state,updated_at) "
            "VALUES ('legacy','[]','synthetic','[]',1,'settlement_unknown',1)"
        )
        before = {t: db.execute(f"SELECT * FROM {t}").fetchall() for t in ("runs", "invoices")}
    upstream = FakeUpstream()
    app, client = build(tmp_path, upstream, FakeFacilitator())
    for table, rows in before.items():
        assert [tuple(r) for r in app.state.ledger._conn.execute(f"SELECT * FROM {table}")] == rows
    assert not await app.state.spend.ready(app.state.catalogs.catalog.tools)
    assert (await paid_post(client, ROUTE, BODY)).status_code == 409
    assert upstream.runs == [] and allocations(app) == []


async def test_uninvoiced_legacy_run_alone_blocks_new_spend(tmp_path):
    app, client = build(tmp_path, FakeUpstream(), FakeFacilitator())
    await app.state.ledger.create("legacy", tool="synthetic", quote_rlusd="1")
    assert (await paid_post(client, ROUTE, BODY)).status_code == 409
    assert allocations(app) == []


async def test_old_claim_code_and_destructive_financial_updates_are_rejected(tmp_path):
    app, client = build(tmp_path, FakeUpstream(), FakeFacilitator())
    _, claim = await quote(app, client)
    with pytest.raises(sqlite3.IntegrityError, match="reservation required"):
        await app.state.x402_invoices.begin(claim[0], "hash", claim[3])
    await app.state.spend.reserve(*claim)
    await app.state.spend.dispatch(claim[0], claim[3], claim[-1])
    for sql in (
        "DELETE FROM spend_reservations",
        "UPDATE spend_reservations SET max_micros=1",
        "UPDATE spend_reservations SET state='reserved'",
        "DELETE FROM spend_budget",
        "UPDATE spend_budget SET ceiling_micros=9000000",
    ):
        with pytest.raises(sqlite3.IntegrityError):
            await app.state.ledger._run(sql, ())
    assert allocations(app)[0]["max_micros"] == 100_000


def test_unknown_schema_version_refuses_before_any_database_mutation(tmp_path):
    path = tmp_path / "unknown.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version=99")
    before = path.read_bytes()
    with pytest.raises(SpendDenied, match="schema"):
        RunLedger(str(path))
    assert path.read_bytes() == before


async def test_cancelled_atomic_claim_completes_before_releasing_connection(tmp_path):
    app, client = build(tmp_path, FakeUpstream(), FakeFacilitator())
    _, claim = await quote(app, client)
    ledger = app.state.ledger
    entered, release = threading.Event(), threading.Event()
    original = ledger._atomic

    def delayed(operation):
        entered.set()
        assert release.wait(3)
        return original(operation)

    ledger._atomic = delayed
    task = asyncio.create_task(app.state.spend.reserve(*claim))
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert ledger._lock.locked()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert allocations(app)[0]["state"] == "reserved"
    assert (await app.state.x402_invoices.record(claim[0]))["state"] == "executing"


async def test_snapshot_restore_retains_budget_reservations_and_replay_guards(tmp_path):
    app, client = build(tmp_path, FakeUpstream(), FakeFacilitator())
    assert (await paid_post(client, ROUTE, BODY)).status_code == 200
    path = Path(app.state.spend.settings.DATABASE_PATH)
    before = path.read_bytes()
    destination = tmp_path / "snapshot"
    backup(path, destination)
    manifest = verify_snapshot(destination)
    assert manifest["schema_version"] == 1
    assert manifest["tables"]["spend_reservations"]["rows"] == 1
    assert rehearse(destination)["compatible"]
    assert path.read_bytes() == before
    restored = RunLedger(str(destination / "gateway.sqlite3"))
    assert [dict(r) for r in restored._conn.execute("SELECT * FROM spend_reservations")] == allocations(app)
    with pytest.raises(sqlite3.IntegrityError):
        await restored._run("DELETE FROM spend_reservations", ())
    restored.close()


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TRIGGER spend_no_delete",
        "ALTER TABLE spend_reservations ADD COLUMN unreviewed TEXT",
    ],
)
def test_changed_financial_schema_refuses_startup_without_repairing_it(tmp_path, sql):
    path = tmp_path / "schema.sqlite3"
    ledger = RunLedger(str(path))
    ledger._conn.execute(sql)
    ledger._conn.commit()
    ledger.close()
    before = path.read_bytes()
    with pytest.raises(SpendDenied, match="schema differs"):
        RunLedger(str(path))
    assert path.read_bytes() == before


async def test_injected_transaction_failure_rolls_back_all_three_financial_writes(tmp_path):
    app, client = build(tmp_path, FakeUpstream(), FakeFacilitator())
    _, claim = await quote(app, client)
    await app.state.ledger._run(
        "CREATE TRIGGER synthetic_failure BEFORE UPDATE ON invoices "
        "BEGIN SELECT RAISE(ABORT, 'synthetic crash'); END",
        (),
    )
    with pytest.raises(SpendDenied):
        await app.state.spend.reserve(*claim)
    assert allocations(app) == []
    assert not app.state.ledger._conn.execute("SELECT * FROM spend_budget").fetchall()
    assert (await app.state.x402_invoices.record(claim[0]))["state"] == "quoted"


async def test_request_deadline_after_dispatch_retains_budget(tmp_path):
    upstream = FakeUpstream()
    app, client = build(tmp_path, upstream, FakeFacilitator(), REQUEST_DEADLINE_SECONDS=0.1)
    started = asyncio.Event()

    async def delayed(*args):
        upstream.runs.append({"synthetic_attempt": True})
        started.set()
        await asyncio.sleep(10)

    app.state.upstream.run = delayed
    envelope, _ = await quote(app, client)
    result = await client.post(ROUTE, json=BODY, headers=headers(envelope))
    assert started.is_set() and result.status_code == 409
    assert allocations(app)[0]["state"] == "dispatch_unknown"
    assert (await client.post(ROUTE, json=BODY, headers=headers(envelope))).status_code == 409
    assert len(upstream.runs) == 1


@pytest.mark.parametrize("kind", ["symlink", "oversize", "fifo", "invalid_bound", "expired", "unbounded"])
async def test_unsafe_policy_sources_and_bounds_deny_without_io_calls(tmp_path, kind):
    import os

    upstream, facilitator = FakeUpstream(), FakeFacilitator()
    app, client = build(tmp_path, upstream, facilitator)
    path = Path(app.state.spend.settings.MONID_TOOL_POLICIES_PATH)
    policy = json.loads(path.read_text())
    if kind == "symlink":
        target = path.with_suffix(".target")
        path.rename(target)
        path.symlink_to(target)
    elif kind == "oversize":
        path.write_bytes(b" " * 1_048_577)
    elif kind == "fifo":
        path.unlink()
        os.mkfifo(path)
    else:
        for rule in policy["tools"]:
            if kind == "invalid_bound":
                rule["max_cost_micros"] = True
            elif kind == "expired":
                rule["expires_at"] = 1
            else:
                rule["inputs"]["anything"] = {"type": "array"}
        path.write_text(json.dumps(policy))
    assert (await client.post(ROUTE, json=BODY)).status_code == 503
    assert allocations(app) == [] and upstream.runs == [] and facilitator.calls == []


async def test_competing_processes_cannot_exceed_single_reservation_budget(tmp_path):
    import os
    import subprocess
    import sys

    app, client = build(tmp_path, FakeUpstream(), FakeFacilitator(), MONID_SPEND_BUDGET_MICROS=100_000)
    _, first = await quote(app, client)
    _, second = await quote(app, client)
    source = Path(__file__).resolve().parents[1]
    program = r"""
import asyncio, json, socket, sys
from pathlib import Path

def blocked(*args, **kwargs):
    raise AssertionError("Network disabled")
socket.socket.connect = blocked
socket.socket.connect_ex = blocked
socket.getaddrinfo = blocked
from gateway.config import Settings
from gateway.spend import Permit, SpendDenied, SpendGuard
from gateway.store import RunLedger

async def main():
    payload = json.loads(Path(sys.argv[1]).read_text())
    ledger = RunLedger(payload["database"])
    # reserve consumes only the bound immutable permit, not environment credentials.
    guard = SpendGuard(Settings(_env_file=None), ledger)
    claim = payload["claim"]
    claim[-1] = Permit(**claim[-1])
    sys.stdout.write("ready\n")
    sys.stdout.flush()
    sys.stdin.readline()
    try:
        await guard.reserve(*claim)
        print("claimed")
    except SpendDenied:
        print("denied")
    ledger.close()
asyncio.run(main())
"""
    processes = []
    env = {"PATH": os.defpath, "PYTHONPATH": str(source), "PYTHONDONTWRITEBYTECODE": "1"}
    try:
        for index, claim in enumerate((first, second)):
            request = tmp_path / f"synthetic-claim-{index}.json"
            request.write_text(
                json.dumps(
                    {
                        "database": app.state.spend.settings.DATABASE_PATH,
                        "claim": [*claim[:-1], claim[-1].quote_context],
                    }
                )
            )
            process = subprocess.Popen(
                [sys.executable, "-c", program, str(request)],
                cwd=tmp_path,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            processes.append(process)
        # Both databases are open before either process starts its claim.
        for process in processes:
            assert await asyncio.wait_for(asyncio.to_thread(process.stdout.readline), 5) == "ready\n"
        for process in processes:
            process.stdin.write("go\n")
            process.stdin.flush()
        outcomes = []
        for process in processes:
            output, error = await asyncio.to_thread(process.communicate, timeout=5)
            assert process.returncode == 0, error
            outcomes.append(output.strip())
        assert sorted(outcomes) == ["claimed", "denied"]
        assert sum(row["max_micros"] for row in allocations(app)) == 100_000
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)


def test_machine_readable_native_schema_contract_matches_exact_runtime():
    ledger = RunLedger(":memory:")
    try:
        contract = json.loads((Path(__file__).parents[1] / "docs/financial-schema-v1.json").read_text())
        assert contract["native_schema_version"] == ledger._conn.execute("PRAGMA user_version").fetchone()[0]
        assert contract["sqlite_schema"] == {
            r[0]: r[1]
            for r in ledger._conn.execute(
                "SELECT name,sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY name"
            )
        }
    finally:
        ledger.close()


def test_historical_snapshot_rehearsal_only_migrates_scratch_copy(tmp_path):
    source = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(source) as db:
        db.executescript(SCHEMA)
        db.execute(
            "INSERT INTO invoices (id,requirements,binding,target,expires_at,state,updated_at) "
            "VALUES ('legacy','[]','synthetic','[]',1,'settlement_unknown',1)"
        )
    before = source.read_bytes()
    snapshot = tmp_path / "legacy-snapshot"
    backup(source, snapshot)
    manifest_path = snapshot / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["format"] = 1
    del manifest["schema_version"]
    manifest_path.write_text(json.dumps(manifest))
    result = rehearse(snapshot)
    assert result["compatible"] and result["unresolved_invoices"] == 1
    assert result["tables"]["spend_reservations"]["rows"] == 0
    assert source.read_bytes() == before
    with sqlite3.connect(snapshot / "gateway.sqlite3") as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 0


@pytest.mark.parametrize("change", ["removed", "expired", "maximum", "key", "base", "account", "catalog"])
async def test_revocation_during_dispatch_commit_cannot_submit(tmp_path, change):
    from gateway.catalog import Catalog

    upstream, facilitator = FakeUpstream(), FakeFacilitator()
    app, client = build(tmp_path, upstream, facilitator)
    envelope, _ = await quote(app, client)
    original = app.state.spend.dispatch

    async def changing(*args):
        await original(*args)
        settings = app.state.spend.settings
        path = Path(settings.MONID_TOOL_POLICIES_PATH)
        if change == "removed":
            path.unlink()
        elif change in {"expired", "maximum"}:
            policy = json.loads(path.read_text())
            for item in policy["tools"]:
                item["expires_at" if change == "expired" else "max_cost_micros"] = 1
            path.write_text(json.dumps(policy))
        elif change == "catalog":
            app.state.catalogs.catalog = Catalog(tools=[])
        else:
            setattr(
                settings,
                {"key": "MONID_API_KEY", "base": "MONID_API_BASE_URL", "account": "MONID_SPEND_ACCOUNT_ID"}[
                    change
                ],
                "changed-synthetic",
            )

    app.state.spend.dispatch = changing
    result = await client.post(ROUTE, json=BODY, headers=headers(envelope))
    assert result.status_code == 503
    assert upstream.runs == [] and facilitator.calls == ["/verify"]
    assert allocations(app)[0]["state"] == "dispatch_unknown"
    assert allocations(app)[0]["max_micros"] == 100_000
    # Repeat cannot refund or create another provider submission/reservation.
    assert (await client.post(ROUTE, json=BODY, headers=headers(envelope))).status_code == 503
    assert len(allocations(app)) == 1 and upstream.runs == []


async def test_health_loads_one_policy_snapshot_for_large_catalog(tmp_path):
    from dataclasses import replace

    app, _ = build(tmp_path, FakeUpstream(), FakeFacilitator())
    guard = app.state.spend
    original = guard.policies
    count = 0

    def counting():
        nonlocal count
        count += 1
        return original()

    guard.policies = counting
    tools = app.state.catalogs.catalog.tools
    tools = tools + [replace(tools[0], endpoint=f"/unreviewed-{n}") for n in range(1000)]
    assert await guard.ready(tools)
    assert count == 1


@pytest.mark.parametrize("expiry", [10**400, -(10**400), 253402300800, float("inf"), float("nan")])
async def test_unsupported_expiry_returns_closed_response_not_exception(tmp_path, expiry):
    app, client = build(tmp_path, FakeUpstream(), FakeFacilitator())
    path = Path(app.state.spend.settings.MONID_TOOL_POLICIES_PATH)
    policy = json.loads(path.read_text())
    for item in policy["tools"]:
        item["expires_at"] = expiry
    path.write_text(json.dumps(policy))
    result = await client.get("/health")
    assert result.status_code == 200 and not result.json()["paymentsReady"]
    assert (await client.post(ROUTE, json=BODY)).status_code == 503
    assert allocations(app) == []


def test_migration_version_and_definitions_use_one_write_transaction(tmp_path):
    from gateway.spend import migrate

    path = tmp_path / "racing-migration.sqlite3"
    first = sqlite3.connect(path)
    second = sqlite3.connect(path)
    first.executescript(SCHEMA)

    class InterleavedStartup:
        def execute(self, sql, *args):
            result = first.execute(sql, *args)
            if sql == "PRAGMA user_version" and not first.in_transaction:
                # Old implementation sees 0, then the other startup commits v1
                # before it reads financial_schema and incorrectly refuses it.
                migrate(second)
            return result

        def __getattr__(self, name):
            return getattr(first, name)

    try:
        migrate(InterleavedStartup())
        migrate(second)
        assert first.execute("PRAGMA user_version").fetchone() == (1,)
        assert second.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    finally:
        first.close()
        second.close()


def test_migration_failure_rolls_back_all_new_schema(tmp_path):
    from gateway.spend import financial_schema, migrate

    path = tmp_path / "failed-migration.sqlite3"
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    before = list(db.execute("SELECT name,sql FROM sqlite_master ORDER BY name"))

    class FailingMigration:
        def execute(self, sql, *args):
            if "CREATE TRIGGER" in sql:
                raise RuntimeError("synthetic migration failure")
            return db.execute(sql, *args)

        def __getattr__(self, name):
            return getattr(db, name)

    try:
        with pytest.raises(RuntimeError, match="synthetic"):
            migrate(FailingMigration())
        assert not financial_schema(db)
        assert list(db.execute("SELECT name,sql FROM sqlite_master ORDER BY name")) == before
        assert db.execute("PRAGMA user_version").fetchone() == (0,)
    finally:
        db.close()


async def test_nonfinite_catalog_identity_remains_closed(tmp_path):
    from dataclasses import replace

    from gateway.catalog import Catalog

    app, client = build(tmp_path, FakeUpstream(), FakeFacilitator())
    tool = app.state.catalogs.catalog.by_route(ROUTE)
    app.state.catalogs.catalog = Catalog(tools=[replace(tool, price={"amount": float("nan")})])
    assert not (await client.get("/health")).json()["paymentsReady"]
    assert (await client.post(ROUTE, json=BODY)).status_code == 503
    assert allocations(app) == []


async def test_maximum_integer_budget_remains_exact_through_sqlite(tmp_path):
    maximum = 2**53 - 1
    upstream = FakeUpstream()
    app, client = build(tmp_path, upstream, FakeFacilitator(), MONID_SPEND_BUDGET_MICROS=maximum)
    path = Path(app.state.spend.settings.MONID_TOOL_POLICIES_PATH)
    policy = json.loads(path.read_text())
    for item in policy["tools"]:
        item["max_cost_micros"] = maximum
    path.write_text(json.dumps(policy))
    assert (await paid_post(client, ROUTE, BODY)).status_code == 200
    assert (await paid_post(client, ROUTE, {"query": "another"})).status_code == 409
    assert len(upstream.runs) == 1
    assert (
        app.state.ledger._conn.execute("SELECT sum(max_micros) FROM spend_reservations").fetchone()[0]
        == maximum
    )
