"""Create private, consistent SQLite snapshots and rehearse upgrades on copies."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path


def read_only(path):
    return sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=5)


def file_hash(path):
    with Path(path).open("rb") as stream:
        digest = hashlib.sha256()
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
        return digest.hexdigest()


def quote_identifier(name):
    return '"' + name.replace('"', '""') + '"'


def inventory(connection):
    tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    result = {}
    for table in ("runs", "invoices"):
        if table not in tables:
            continue
        columns = [r[1] for r in connection.execute("PRAGMA table_info(" + table + ")")]
        states = dict(connection.execute("SELECT state, COUNT(*) FROM " + table + " GROUP BY state"))
        result[table] = {"columns": columns, "rows": sum(states.values()), "states": states}
    return result


def data_hash(connection, table, columns):
    digest = hashlib.sha256()
    query = (
        "SELECT "
        + ",".join(map(quote_identifier, columns))
        + " FROM "
        + quote_identifier(table)
        + " ORDER BY id"
    )
    for row in connection.execute(query):
        encoded = json.dumps(
            list(row), default=lambda v: {"bytes": v.hex()}, separators=(",", ":"), ensure_ascii=True
        ).encode()
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def check_integrity(connection):
    if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
        raise ValueError("SQLite integrity check failed")


def backup(source, destination):
    destination = Path(destination)
    # Creating a new directory prevents accidental replacement of a live ledger.
    destination.mkdir(mode=0o700, parents=False, exist_ok=False)
    try:
        target = destination / "gateway.sqlite3"
        fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        deadline = time.monotonic() + 120

        def progress(status, remaining, total):
            if time.monotonic() > deadline:
                raise TimeoutError("Snapshot deadline exceeded")

        with read_only(source) as origin, sqlite3.connect(target) as copied:
            origin.backup(copied, pages=256, progress=progress, sleep=0.05)
            check_integrity(copied)
            tables = inventory(copied)
        manifest = {"format": 1, "sha256": file_hash(target), "tables": tables, "created_at": time.time()}
        fd = os.open(destination / "manifest.json", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(manifest, stream, indent=2)
        return manifest
    except BaseException:
        shutil.rmtree(destination)
        raise


def verify_snapshot(directory):
    directory = Path(directory)
    manifest = json.loads((directory / "manifest.json").read_text())
    if manifest.get("format") != 1 or file_hash(directory / "gateway.sqlite3") != manifest["sha256"]:
        raise ValueError("Snapshot checksum does not match its manifest")
    with read_only(directory / "gateway.sqlite3") as db:
        check_integrity(db)
        if inventory(db) != manifest["tables"]:
            raise ValueError("Snapshot inventory does not match its manifest")
    return manifest


def rehearse(directory):
    from gateway.store import RunLedger

    manifest = verify_snapshot(directory)
    original = Path(directory) / "gateway.sqlite3"
    tables = manifest["tables"]
    if "runs" not in tables or (tables["runs"]["rows"] and "invoices" not in tables):
        raise ValueError("Ledger requires a reviewed legacy-invoice migration")
    with tempfile.TemporaryDirectory(prefix="monid-restore-") as scratch:
        restored = Path(scratch) / "restored"
        backup(original, restored)
        path = restored / "gateway.sqlite3"
        with read_only(original) as before:
            fingerprints = {
                table: data_hash(before, table, detail["columns"]) for table, detail in tables.items()
            }
        ledger = RunLedger(str(path))
        ledger.close()
        # Confirm required columns, including when CREATE IF NOT EXISTS could
        # otherwise hide an incompatible pre-existing table.
        expected = RunLedger(":memory:")
        required = inventory(expected._conn)
        expected.close()
        with read_only(path) as after:
            check_integrity(after)
            actual = inventory(after)
            for table, detail in required.items():
                if not set(detail["columns"]) <= set(actual.get(table, {}).get("columns", [])):
                    raise ValueError("Restored ledger is missing required columns")
            for table, detail in tables.items():
                if data_hash(after, table, detail["columns"]) != fingerprints[table]:
                    raise ValueError("Upgrade changed existing ledger data")
        return {
            "compatible": True,
            "tables": actual,
            "unresolved_invoices": sum(
                tables.get("invoices", {}).get("states", {}).get(s, 0)
                for s in ("executing", "settling", "settlement_unknown", "ready")
            ),
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="operation", required=True)
    create = sub.add_parser("backup")
    create.add_argument("--source", required=True)
    create.add_argument("--destination", required=True)
    for name in ("verify", "rehearse"):
        command = sub.add_parser(name)
        command.add_argument("--snapshot", required=True)
    args = parser.parse_args()
    try:
        if args.operation == "backup":
            backup(args.source, args.destination)
            print("Consistent private snapshot created and verified.")
        elif args.operation == "verify":
            verify_snapshot(args.snapshot)
            print("Snapshot checksum, inventory and integrity verified.")
        else:
            report = rehearse(args.snapshot)
            print(
                json.dumps(
                    {"compatible": report["compatible"], "unresolved_invoices": report["unresolved_invoices"]}
                )
            )
    except Exception:
        # Do not dump database rows, tokens, paths, or arbitrary SQLite errors.
        raise SystemExit("Ledger operation failed; source unchanged. Inspect privately.") from None


if __name__ == "__main__":
    main()
