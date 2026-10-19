from __future__ import annotations

import asyncio
import contextlib
import threading

from gateway.store import RunLedger


async def test_repeated_cancellation_keeps_connection_locked_until_commit(tmp_path, monkeypatch):
    ledger = RunLedger(str(tmp_path / "ledger.sqlite3"))
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    execute = ledger._execute

    def delayed(statements):
        entered.set()
        assert release.wait(timeout=3)
        try:
            return execute(statements)
        finally:
            finished.set()

    monkeypatch.setattr(ledger, "_execute", delayed)
    task = asyncio.create_task(ledger._run("UPDATE invoices SET state = 'executing'", ()))
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert ledger._lock.locked(), "A live SQLite worker must retain exclusive connection ownership"
    finally:
        release.set()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        assert await asyncio.to_thread(finished.wait, 3)
        ledger.close()
