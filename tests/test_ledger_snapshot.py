import asyncio
import sqlite3

import pytest

from gateway.store import RunLedger
from scripts.ledger_snapshot import backup, rehearse, verify_snapshot


def test_wal_snapshot_preserves_unknown_state_tokens_and_extra_columns(tmp_path):
    source = tmp_path / "live.sqlite3"
    ledger = RunLedger(str(source))
    ledger._conn.execute("PRAGMA journal_mode=WAL")
    ledger._conn.execute("ALTER TABLE runs ADD COLUMN extra_column TEXT")
    asyncio.run(
        ledger.create(
            "run", tool="example#/tool", quote_rlusd="1", job_token="private-token", state="execution_unknown"
        )
    )
    ledger._conn.execute(
        "INSERT INTO invoices (id, requirements, binding, target, expires_at,state,updated_at) "
        "VALUES ('invoice','[]','binding','[]',0,'executing',0)"
    )
    ledger._conn.commit()
    destination = tmp_path / "snapshot"
    manifest = backup(source, destination)
    assert manifest["tables"]["runs"]["rows"] == 1
    assert (destination / "gateway.sqlite3").stat().st_mode & 0o777 == 0o600
    assert (destination / "manifest.json").stat().st_mode & 0o777 == 0o600
    assert "private-token" not in (destination / "manifest.json").read_text()
    report = rehearse(destination)
    assert report["compatible"] and report["unresolved_invoices"] == 1
    assert ledger._conn.execute("SELECT job_token FROM runs").fetchone()[0] == "private-token"
    assert "extra_column" in report["tables"]["runs"]["columns"]
    with pytest.raises(FileExistsError):
        backup(source, destination)
    ledger.close()


def test_tampered_snapshot_is_rejected(tmp_path):
    ledger = RunLedger(str(tmp_path / "source"))
    ledger.close()
    backup(tmp_path / "source", tmp_path / "snapshot")
    with (tmp_path / "snapshot" / "gateway.sqlite3").open("ab") as f:
        f.write(b"tampered")
    with pytest.raises(ValueError, match="checksum"):
        verify_snapshot(tmp_path / "snapshot")


def test_missing_source_does_not_create_a_database(tmp_path):
    with pytest.raises(sqlite3.OperationalError):
        backup(tmp_path / "missing", tmp_path / "snapshot")
    assert not (tmp_path / "missing").exists() and not (tmp_path / "snapshot").exists()


def test_legacy_runs_without_invoices_require_review(tmp_path):
    source = tmp_path / "legacy"
    ledger = RunLedger(str(source))
    asyncio.run(ledger.create("old", tool="example#/tool", quote_rlusd="1"))
    ledger._conn.execute("DROP TABLE invoices")
    ledger._conn.commit()
    ledger.close()
    backup(source, tmp_path / "snapshot")
    with pytest.raises(ValueError, match="legacy"):
        rehearse(tmp_path / "snapshot")
