import asyncio
from contextvars import ContextVar
from types import SimpleNamespace

import pytest

from gateway import deadline as deadline_module

from .test_app import FakeFacilitator, FakeUpstream, build, decode_header, sign


@pytest.fixture
def upstream():
    return FakeUpstream()


@pytest.fixture
def controlled_deadline(monkeypatch):
    """Expire the real asyncio timeout only after the target phase is reached.

    Context propagation follows the request through Starlette's child tasks.
    Patching this module's asyncio reference leaves test watchdogs untouched.
    """
    current = ContextVar("request_deadline")

    def timeout(seconds):
        deadline = asyncio.timeout(None)
        current.set(deadline)
        return deadline

    def expire():
        current.get().reschedule(asyncio.get_running_loop().time())

    monkeypatch.setattr(deadline_module, "asyncio", SimpleNamespace(
        timeout=timeout, Semaphore=asyncio.Semaphore,
    ))
    return expire


@pytest.mark.parametrize("phase", ["balance", "verify", "execution", "settlement", "poll"])
async def test_deadline_keeps_payment_retries_closed(
    tmp_path, upstream, monkeypatch, phase, controlled_deadline,
):
    facilitator = FakeFacilitator()
    app, client = build(tmp_path, upstream, facilitator, REQUEST_DEADLINE_SECONDS=0.08)
    challenge = await client.post("/t/exa/search", json={"query": "x"})
    signature = sign(decode_header(challenge.headers["PAYMENT-REQUIRED"]))
    headers = {"PAYMENT-SIGNATURE": signature}
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def hang(*args, **kwargs):
        entered.set()
        try:
            controlled_deadline()
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    original_post = facilitator.post
    original_run = app.state.upstream.run
    original_balance = app.state.upstream.balance_ok
    if phase == "balance":
        monkeypatch.setattr(app.state.upstream, "balance_ok", hang)
    elif phase in ("verify", "settlement"):

        async def post(path, json):
            if path == ("/verify" if phase == "verify" else "/settle"):
                facilitator.calls.append(path)
                await hang()
            return await original_post(path, json)

        monkeypatch.setattr(facilitator, "post", post)
    elif phase == "execution":

        async def run(*args, **kwargs):
            upstream.runs.append({"started": True})
            await hang()

        monkeypatch.setattr(app.state.upstream, "run", run)
    else:
        upstream.run_response = (202, {"runId": "KNOWN_JOB", "status": "READY"})
        monkeypatch.setattr(app.state.upstream, "get_run", hang)

    response = await asyncio.wait_for(
        client.post("/t/exa/search", json={"query": "x"}, headers=headers),
        timeout=10,
    )
    assert entered.is_set() and cancelled.is_set()
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "request_deadline_exceeded"
    row = app.state.ledger._conn.execute("SELECT state FROM invoices").fetchone()
    assert (
        row[0]
        == {
            "balance": "quoted",
            "verify": "quoted",
            "execution": "executing",
            "poll": "executing",
            "settlement": "settling",
        }[phase]
    )
    if phase == "poll":
        assert app.state.ledger._conn.execute("SELECT upstream_run_id FROM runs").fetchone()[0] == "KNOWN_JOB"

    monkeypatch.setattr(facilitator, "post", original_post)
    monkeypatch.setattr(app.state.upstream, "run", original_run)
    monkeypatch.setattr(app.state.upstream, "balance_ok", original_balance)
    if phase in ("execution", "settlement", "poll"):
        before_runs = len(upstream.runs)
        before_settles = facilitator.calls.count("/settle")
        # A fresh process must preserve the same refusal to repeat side effects.
        app.state.ledger.close()
        await client.aclose()
        app, client = build(tmp_path, upstream, facilitator)
        retry = await client.post("/t/exa/search", json={"query": "x"}, headers=headers)
        assert retry.status_code == 409
        assert len(upstream.runs) == before_runs
        assert facilitator.calls.count("/settle") == before_settles
    else:
        retry = await client.post("/t/exa/search", json={"query": "x"}, headers=headers)
        assert retry.status_code == 200
    app.state.ledger.close()
    await client.aclose()


async def test_overload_is_rejected_before_invoice_claim(
    tmp_path, upstream, monkeypatch, controlled_deadline,
):
    facilitator = FakeFacilitator()
    app, client = build(tmp_path, upstream, facilitator, MAX_CONCURRENT_RUNS=1, REQUEST_DEADLINE_SECONDS=0.3)
    signatures = []
    for query in ("one", "two"):
        r = await client.post("/t/exa/search", json={"query": query})
        signatures.append(sign(decode_header(r.headers["PAYMENT-REQUIRED"])))
    entered = asyncio.Event()
    release = asyncio.Event()

    async def hang(*args, **kwargs):
        entered.set()
        await release.wait()
        controlled_deadline()
        await asyncio.Event().wait()

    monkeypatch.setattr(app.state.upstream, "run", hang)
    first = asyncio.create_task(
        client.post("/t/exa/search", json={"query": "one"}, headers={"PAYMENT-SIGNATURE": signatures[0]})
    )
    try:
        await asyncio.wait_for(entered.wait(), 10)
        second = await client.post(
            "/t/exa/search", json={"query": "two"}, headers={"PAYMENT-SIGNATURE": signatures[1]}
        )
        assert second.status_code == 503 and second.json()["error"]["code"] == "gateway_busy"
        assert facilitator.calls == ["/verify"]
        states = sorted(r[0] for r in app.state.ledger._conn.execute("SELECT state FROM invoices"))
        assert states == ["executing", "quoted"]
        release.set()
        assert (await asyncio.wait_for(first, 10)).status_code == 409
    finally:
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
    app.state.ledger.close()
    await client.aclose()


async def test_job_poll_timeout_never_reexecutes_or_resettles(
    tmp_path, upstream, monkeypatch, controlled_deadline,
):
    from .test_app import paid_post

    facilitator = FakeFacilitator()
    app, client = build(tmp_path, upstream, facilitator, RUN_WAIT_SECONDS=0, REQUEST_DEADLINE_SECONDS=0.08)
    upstream.run_response = (202, {"runId": "KNOWN_JOB", "status": "READY"})
    response = await paid_post(client, "/t/exa/search", {"query": "async"})
    assert response.status_code == 202
    job = response.json()

    async def hang(*args, **kwargs):
        controlled_deadline()
        await asyncio.Event().wait()

    monkeypatch.setattr(app.state.upstream, "get_run", hang)
    response = await asyncio.wait_for(
        client.get("/jobs/" + job["jobId"], headers={"X-Job-Token": job["jobToken"]}), 10
    )
    assert response.status_code == 504
    assert len(upstream.runs) == 1 and facilitator.calls == ["/verify", "/settle"]
    assert app.state.ledger._conn.execute("SELECT state FROM invoices").fetchone()[0] == "settled"
    app.state.ledger.close()
    await client.aclose()


@pytest.mark.parametrize("signed,status", [(False, 504), (True, 409)])
async def test_configured_deadline_cancels_request_without_test_clock(signed, status):
    entered = asyncio.Event()
    cancelled = asyncio.Event()
    messages = []

    async def app(scope, receive, send):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(message):
        messages.append(message)

    middleware = deadline_module.RequestDeadlineMiddleware(app, seconds=0, concurrency=1)
    scope = {"type": "http", "path": "/t/example", "method": "POST",
             "headers": [(b"payment-signature", b"test")] if signed else []}
    # A zero timeout fires on the next event-loop turn, after app enters its wait.
    # There is no short wall-clock allowance for SQLite or request preparation.
    await asyncio.wait_for(middleware(scope, receive, send), 10)
    assert entered.is_set() and cancelled.is_set()
    assert messages[0]["status"] == status
    assert b"request_deadline_exceeded" in messages[1]["body"]
    assert not middleware.slots.locked()
