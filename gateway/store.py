"""SQLite ledger of paid runs: what we quoted, what the upstream charged, how it settled."""

from __future__ import annotations

import asyncio
import os
import sqlite3
import time
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    invoice_id TEXT,
    tool TEXT NOT NULL,
    quote_rlusd TEXT NOT NULL,
    quote_estimated INTEGER NOT NULL,
    upstream_run_id TEXT,
    upstream_status TEXT,
    upstream_cost_usd TEXT,
    settlement_tx TEXT,
    payer TEXT,
    job_token TEXT,
    state TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS runs_invoice ON runs (invoice_id);
CREATE TABLE IF NOT EXISTS invoices (
    id TEXT PRIMARY KEY,
    requirements TEXT NOT NULL,
    binding TEXT NOT NULL,
    target TEXT NOT NULL,
    expires_at REAL NOT NULL,
    state TEXT NOT NULL DEFAULT 'quoted',
    payment_hash TEXT,
    run_id TEXT UNIQUE,
    response_status INTEGER,
    response_body BLOB,
    response_headers TEXT,
    settlement TEXT,
    reconciliation TEXT,
    updated_at REAL NOT NULL
);
"""

COLUMNS = (
    "invoice_id",
    "tool",
    "quote_rlusd",
    "quote_estimated",
    "upstream_run_id",
    "upstream_status",
    "upstream_cost_usd",
    "settlement_tx",
    "payer",
    "job_token",
    "state",
)


class RunLedger:
    def __init__(self, path: str) -> None:
        if path != ":memory:":
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._lock = asyncio.Lock()

    async def _run(self, sql: str, params: tuple[Any, ...]) -> list[sqlite3.Row]:
        return (await self.transaction([(sql, params)]))[0]

    async def transaction(
        self, statements: list[tuple[str, tuple[Any, ...]]]
    ) -> list[list[sqlite3.Row]]:
        async with self._lock:
            task = asyncio.create_task(asyncio.to_thread(self._execute, statements))
            cancelled = False
            while True:
                try:
                    result = await asyncio.shield(task)
                    break
                except asyncio.CancelledError:
                    # Repeated cancellation must not cancel the worker future or
                    # release this lock before its real thread commits/rolls back.
                    cancelled = True
                    if task.done():
                        result = task.result()
                        break
            if cancelled:
                raise asyncio.CancelledError
            return result

    def _execute(self, statements: list[tuple[str, tuple[Any, ...]]]) -> list[list[sqlite3.Row]]:
        with self._conn:
            return [self._conn.execute(sql, params).fetchall() for sql, params in statements]

    async def create(self, run_id: str, **fields: Any) -> None:
        now = time.time()
        values = {name: fields.get(name) for name in COLUMNS}
        values["state"] = values["state"] or "started"
        values["quote_estimated"] = int(bool(values["quote_estimated"]))
        names = ", ".join(("id", *COLUMNS, "created_at", "updated_at"))
        marks = ", ".join("?" for _ in range(len(COLUMNS) + 3))
        await self._run(
            f"INSERT INTO runs ({names}) VALUES ({marks})",
            (run_id, *(values[name] for name in COLUMNS), now, now),
        )

    async def update(self, run_id: str, **fields: Any) -> None:
        unknown = set(fields) - set(COLUMNS)
        if unknown:
            raise ValueError(f"unknown run fields: {sorted(unknown)}")
        if not fields:
            return
        assignments = ", ".join(f"{name} = ?" for name in fields)
        await self._run(
            f"UPDATE runs SET {assignments}, updated_at = ? WHERE id = ?",
            (*fields.values(), time.time(), run_id),
        )

    async def get(self, run_id: str) -> dict[str, Any] | None:
        rows = await self._run("SELECT * FROM runs WHERE id = ?", (run_id,))
        return dict(rows[0]) if rows else None

    async def under_quoted(self, limit: int = 100) -> list[dict[str, Any]]:
        """Settled runs where the upstream charged more than the buyer paid."""
        rows = await self._run(
            "SELECT * FROM runs WHERE upstream_cost_usd IS NOT NULL "
            "AND CAST(upstream_cost_usd AS REAL) > CAST(quote_rlusd AS REAL) "
            "ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        return [dict(row) for row in rows]

    def close(self) -> None:
        self._conn.close()
