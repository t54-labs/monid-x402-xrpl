"""Reviewed worst-case reservations, not a claim about unknown provider billing."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import stat
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .catalog import Tool
    from .config import Settings
    from .store import RunLedger

MAX_MICROS = 2**53 - 1
SPEND_SCHEMA_VERSION = 1
SPEND_SCHEMA = """
CREATE TABLE IF NOT EXISTS spend_budget (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    budget_id TEXT NOT NULL, account_id TEXT NOT NULL,
    ceiling_micros INTEGER NOT NULL CHECK (ceiling_micros > 0)
);
CREATE TABLE IF NOT EXISTS spend_reservations (
    invoice_id TEXT PRIMARY KEY, run_id TEXT NOT NULL UNIQUE,
    proof_hash TEXT NOT NULL UNIQUE, budget_id TEXT NOT NULL, account_id TEXT NOT NULL,
    binding TEXT NOT NULL, policy_digest TEXT NOT NULL,
    max_micros INTEGER NOT NULL CHECK (max_micros > 0),
    state TEXT NOT NULL CHECK (state IN ('reserved','dispatch_unknown','rejected_before_dispatch')),
    created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE TRIGGER IF NOT EXISTS spend_no_delete BEFORE DELETE ON spend_reservations
BEGIN SELECT RAISE(ABORT, 'spend reservations cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS spend_immutable BEFORE UPDATE OF
invoice_id,run_id,proof_hash,budget_id,account_id,binding,policy_digest,max_micros,created_at
ON spend_reservations BEGIN SELECT RAISE(ABORT, 'spend reservation identity is immutable'); END;
CREATE TRIGGER IF NOT EXISTS spend_no_reopen BEFORE UPDATE OF state ON spend_reservations
WHEN OLD.state != 'reserved' OR NEW.state NOT IN ('dispatch_unknown','rejected_before_dispatch')
BEGIN SELECT RAISE(ABORT, 'spend reservation cannot be reopened'); END;
CREATE TRIGGER IF NOT EXISTS spend_budget_no_reset BEFORE DELETE ON spend_budget
BEGIN SELECT RAISE(ABORT, 'spend budget cannot be reset'); END;
CREATE TRIGGER IF NOT EXISTS spend_budget_no_change BEFORE UPDATE ON spend_budget
BEGIN SELECT RAISE(ABORT, 'spend budget change requires reviewed migration'); END;
CREATE TRIGGER IF NOT EXISTS spend_guard_legacy_claim BEFORE UPDATE OF state ON invoices
WHEN NEW.state = 'executing' AND OLD.state = 'quoted' AND NOT EXISTS (
    SELECT 1 FROM spend_reservations WHERE invoice_id = NEW.id AND run_id = NEW.run_id
    AND state = 'reserved'
)
BEGIN SELECT RAISE(ABORT, 'durable spend reservation required'); END;
"""


class SpendDenied(ValueError):
    """Safe, fixed error text only; never include policy inputs or credential values."""


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def catalog_digest(tool: Tool) -> str:
    return digest(asdict(tool))


def unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise SpendDenied("duplicate policy field")
        result[key] = value
    return result


def financial_schema(connection):
    return {
        r[0]: r[1]
        for r in connection.execute(
            "SELECT name,sql FROM sqlite_master WHERE name GLOB 'spend_*' ORDER BY name"
        )
    }


def validate_schema(connection):
    with sqlite3.connect(":memory:") as expected:
        expected.executescript("CREATE TABLE invoices (id,state,run_id);" + SPEND_SCHEMA)
        if financial_schema(connection) != financial_schema(expected):
            raise SpendDenied("financial schema differs from reviewed migration")


def migrate(connection: sqlite3.Connection) -> None:
    # Version and definitions must be observed under the same write lock. A
    # concurrent first start may otherwise commit between these two reads.
    connection.execute("BEGIN IMMEDIATE")
    try:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version == SPEND_SCHEMA_VERSION:
            validate_schema(connection)
        elif version == 0:
            if financial_schema(connection):
                raise SpendDenied("unversioned financial tables require review")
            # executescript would implicitly commit our lock before executing.
            # These are constant, authored statements, not policy or user input.
            statement = ""
            for line in SPEND_SCHEMA.splitlines(keepends=True):
                statement += line
                if sqlite3.complete_statement(statement):
                    connection.execute(statement)
                    statement = ""
            if statement.strip():
                raise SpendDenied("incomplete financial migration")
            connection.execute(f"PRAGMA user_version={SPEND_SCHEMA_VERSION}")
        else:
            raise SpendDenied("unsupported financial schema version")
        connection.commit()
    except BaseException:
        if connection.in_transaction:
            connection.rollback()
        raise


def integer(value: Any, minimum: int = 1) -> bool:
    return type(value) is int and minimum <= value <= MAX_MICROS


def validate_rules(policy: dict, depth: int = 0) -> None:
    rules, required = policy["inputs"], policy["required"]
    if depth > 4 or not isinstance(rules, dict) or len(rules) > 64 or not isinstance(required, list):
        raise SpendDenied("invalid bounded input policy")
    if any(not isinstance(k, str) or not 1 <= len(k) <= 100 for k in rules):
        raise SpendDenied("invalid input name")
    if any(not isinstance(k, str) or k not in rules for k in required) or len(set(required)) != len(required):
        raise SpendDenied("invalid required inputs")
    for rule in rules.values():
        validate_rule(rule, depth)


def validate_rule(rule: dict, depth: int) -> None:
    if depth > 4 or not isinstance(rule, dict):
        raise SpendDenied("invalid input rule")
    kind = rule.get("type")
    if kind == "string":
        if (
            set(rule) != {"type", "max_length"}
            or not integer(rule["max_length"])
            or rule["max_length"] > 16384
        ):
            raise SpendDenied("unbounded string rule")
    elif kind == "integer":
        if (
            set(rule) != {"type", "minimum", "maximum"}
            or not integer(rule["minimum"], 0)
            or not integer(rule["maximum"], 0)
            or rule["minimum"] > rule["maximum"]
        ):
            raise SpendDenied("unbounded integer rule")
    elif kind == "array":
        if (
            set(rule) != {"type", "max_items", "items"}
            or not integer(rule["max_items"])
            or rule["max_items"] > 100
        ):
            raise SpendDenied("unbounded array rule")
        validate_rule(rule["items"], depth + 1)
    elif kind == "object":
        if set(rule) != {"type", "inputs", "required"}:
            raise SpendDenied("invalid object rule")
        validate_rules(rule, depth + 1)
    elif kind != "boolean" or set(rule) != {"type"}:
        raise SpendDenied("unsupported input policy; keep tool closed")


def matches(rule: dict, value: Any) -> bool:
    kind = rule["type"]
    if kind == "string":
        return isinstance(value, str) and len(value) <= rule["max_length"]
    if kind == "integer":
        return type(value) is int and rule["minimum"] <= value <= rule["maximum"]
    if kind == "boolean":
        return type(value) is bool
    if kind == "array":
        return (
            isinstance(value, list)
            and len(value) <= rule["max_items"]
            and all(matches(rule["items"], item) for item in value)
        )
    return (
        isinstance(value, dict)
        and not set(value) - set(rule["inputs"])
        and not set(rule["required"]) - set(value)
        and all(matches(rule["inputs"][key], item) for key, item in value.items())
    )


LEGACY_HISTORY = (
    "SELECT 1 FROM invoices i WHERE i.state != 'quoted' AND NOT EXISTS "
    "(SELECT 1 FROM spend_reservations s WHERE s.invoice_id=i.id) "
    "UNION ALL SELECT 1 FROM runs r WHERE NOT EXISTS "
    "(SELECT 1 FROM spend_reservations s WHERE s.run_id=r.id AND s.invoice_id=r.invoice_id) LIMIT 1"
)


@dataclass(frozen=True)
class Permit:
    budget_id: str
    account_id: str
    ceiling_micros: int
    max_micros: int
    policy_digest: str
    binding: str
    expires_at: float

    @property
    def quote_context(self) -> dict:
        return asdict(self)


class SpendGuard:
    def __init__(self, settings: Settings, ledger: RunLedger):
        self.settings, self.ledger = settings, ledger

    def policies(self) -> list[dict]:
        s = self.settings
        if (
            not integer(s.MONID_SPEND_BUDGET_MICROS)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", s.MONID_SPEND_BUDGET_ID)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", s.MONID_SPEND_ACCOUNT_ID)
            or not s.MONID_API_KEY
            or not s.MONID_TOOL_POLICIES_PATH
        ):
            raise SpendDenied("reviewed spend configuration required")
        path = Path(s.MONID_TOOL_POLICIES_PATH)
        if path.is_symlink():
            raise SpendDenied("policy symlink refused")
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as source:
                info = os.fstat(source.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > 1_048_576:
                    raise SpendDenied("bounded regular policy file required")
                raw = source.read(1_048_577)
            if len(raw) > 1_048_576:
                raise SpendDenied("policy size exceeded")
            value = json.loads(raw, object_pairs_hook=unique_pairs)
            if (
                set(value) != {"version", "tools"}
                or type(value["version"]) is not int
                or value["version"] != 1
            ):
                raise SpendDenied("unsupported policy document")
            policies = value["tools"]
            if not isinstance(policies, list) or not 0 < len(policies) <= 100:
                raise SpendDenied("reviewed tools required")
            seen = set()
            for p in policies:
                if not isinstance(p, dict) or set(p) != {
                    "provider",
                    "endpoint",
                    "catalog_digest",
                    "max_cost_micros",
                    "expires_at",
                    "inputs",
                    "required",
                    "review_reference",
                }:
                    raise SpendDenied("invalid tool policy")
                if not isinstance(p["provider"], str) or not isinstance(p["endpoint"], str):
                    raise SpendDenied("invalid tool identity")
                identity = (p["provider"], p["endpoint"])
                if identity in seen:
                    raise SpendDenied("duplicate tool policy")
                seen.add(identity)
                if (
                    not isinstance(p["catalog_digest"], str)
                    or not re.fullmatch("[a-f0-9]{64}", p["catalog_digest"])
                    or not integer(p["max_cost_micros"])
                    or p["max_cost_micros"] > s.MONID_SPEND_BUDGET_MICROS
                    or type(p["expires_at"]) not in (int, float)
                    or not 0 < p["expires_at"] <= 253402300799
                    or not math.isfinite(p["expires_at"])
                    or not isinstance(p["review_reference"], str)
                    or not 1 <= len(p["review_reference"].strip()) <= 500
                ):
                    raise SpendDenied("invalid reviewed maximum or provenance")
                validate_rules(p)
            return policies
        except (OSError, ValueError, TypeError, KeyError, RecursionError) as error:
            raise SpendDenied("reviewed spend policy unavailable or invalid") from error

    def permit(self, tool: Tool, body: dict, *, probe: bool = False) -> Permit:
        policy = next(
            (p for p in self.policies() if (p["provider"], p["endpoint"]) == (tool.provider, tool.endpoint)),
            None,
        )
        return self._permit(tool, body, policy, probe=probe)

    def _permit(self, tool: Tool, body: dict, policy: dict | None, *, probe: bool) -> Permit:
        s = self.settings
        try:
            current_digest = catalog_digest(tool)
        except (ValueError, TypeError, RecursionError) as error:
            raise SpendDenied("catalog cannot establish reviewed tool identity") from error
        if (
            policy is None
            or policy["expires_at"] <= time.time()
            or policy["catalog_digest"] != current_digest
        ):
            raise SpendDenied("tool policy missing expired or catalog changed")
        try:
            size = len(json.dumps(body, allow_nan=False).encode())
        except (ValueError, TypeError, RecursionError) as error:
            raise SpendDenied("bounded JSON required") from error
        if not isinstance(body, dict) or size > 16384:
            raise SpendDenied("bounded object input required")
        if not probe and not matches(
            {"type": "object", "inputs": policy["inputs"], "required": policy["required"]}, body
        ):
            raise SpendDenied("input outside reviewed policy")
        policy_hash = digest(policy)
        binding = digest(
            {
                "provider": tool.provider,
                "endpoint": tool.endpoint,
                "input": body,
                "policy": policy_hash,
                "budget_id": s.MONID_SPEND_BUDGET_ID,
                "account_id": s.MONID_SPEND_ACCOUNT_ID,
                "ceiling": s.MONID_SPEND_BUDGET_MICROS,
                "upstream_base": s.MONID_API_BASE_URL,
                "key_hash": hashlib.sha256(s.MONID_API_KEY.encode()).hexdigest(),
            }
        )
        return Permit(
            s.MONID_SPEND_BUDGET_ID,
            s.MONID_SPEND_ACCOUNT_ID,
            s.MONID_SPEND_BUDGET_MICROS,
            policy["max_cost_micros"],
            policy_hash,
            binding,
            policy["expires_at"],
        )

    async def reserve(
        self, invoice_id: str, payment_hash: str, blob: str, run_id: str, request_binding: str, permit: Permit
    ) -> None:
        if not isinstance(blob, str) or not re.fullmatch(r"(?:[a-fA-F0-9]{2}){1,12000}", blob):
            raise SpendDenied("invalid signed proof")
        proof = hashlib.sha256(bytes.fromhex(blob)).hexdigest()

        def operation(db):
            now = time.time()
            row = db.execute("SELECT * FROM invoices WHERE id=?", (invoice_id,)).fetchone()
            if (
                row is None
                or row["state"] != "quoted"
                or row["expires_at"] <= now
                or row["binding"] != request_binding
                or permit.expires_at <= now
            ):
                raise SpendDenied("invoice unavailable")
            stored = json.loads(row["requirements"])
            if not stored or stored[0].get("extra", {}).get("spend") != permit.quote_context:
                raise SpendDenied("quote spend policy mismatch")
            existing = db.execute("SELECT * FROM spend_budget WHERE singleton=1").fetchone()
            identity = (permit.budget_id, permit.account_id, permit.ceiling_micros)
            if existing and tuple(existing)[1:] != identity:
                raise SpendDenied("budget identity or ceiling change requires review")
            # An upgrade may contain historical irreversible work with no cost
            # reservation. It must be reconciled, never silently assigned zero.
            if db.execute(LEGACY_HISTORY).fetchone():
                raise SpendDenied("legacy financial history requires reconciliation")
            used = db.execute("SELECT coalesce(sum(max_micros),0) FROM spend_reservations").fetchone()[0]
            if used + permit.max_micros > permit.ceiling_micros:
                raise SpendDenied("spend budget exhausted")
            if existing is None:
                db.execute("INSERT INTO spend_budget VALUES (1,?,?,?)", identity)
            db.execute(
                "INSERT INTO spend_reservations VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    invoice_id,
                    run_id,
                    proof,
                    permit.budget_id,
                    permit.account_id,
                    permit.binding,
                    permit.policy_digest,
                    permit.max_micros,
                    "reserved",
                    now,
                    now,
                ),
            )
            db.execute(
                "UPDATE invoices SET state='executing',payment_hash=?,run_id=?,updated_at=? WHERE id=?",
                (payment_hash, run_id, now, invoice_id),
            )

        try:
            await self.ledger.atomic(operation)
        except sqlite3.IntegrityError as error:
            raise SpendDenied("duplicate or conflicting spend claim") from error

    async def dispatch(self, invoice_id: str, run_id: str, permit: Permit) -> None:
        def operation(db):
            row = db.execute(
                "UPDATE spend_reservations SET state='dispatch_unknown',updated_at=? "
                "WHERE invoice_id=? AND run_id=? AND binding=? AND state='reserved' RETURNING invoice_id",
                (time.time(), invoice_id, run_id, permit.binding),
            ).fetchone()
            if row is None or permit.expires_at <= time.time():
                raise SpendDenied("dispatch reservation unavailable or expired")

        await self.ledger.atomic(operation)

    async def reject_before_dispatch(self, invoice_id: str) -> None:
        await self.ledger._run(
            "UPDATE spend_reservations SET state='rejected_before_dispatch',updated_at=? "
            "WHERE invoice_id=? AND state='reserved'",
            (time.time(), invoice_id),
        )

    async def ready(self, tools) -> bool:
        try:
            policies = self.policies()
        except SpendDenied:
            return False
        # Public health must not reparse a 1 MiB policy for every catalog entry.
        # Each execution admission still obtains a fresh policy independently.
        listed = {(tool.provider, tool.endpoint): tool for tool in tools}
        valid = []
        for policy in policies:
            tool = listed.get((policy["provider"], policy["endpoint"]))
            if tool is None:
                continue
            try:
                valid.append(self._permit(tool, {}, policy, probe=True))
            except SpendDenied:
                pass
        if not valid:
            return False
        budget = await self.ledger._run("SELECT * FROM spend_budget", ())
        if budget and tuple(budget[0])[1:] != (
            self.settings.MONID_SPEND_BUDGET_ID,
            self.settings.MONID_SPEND_ACCOUNT_ID,
            self.settings.MONID_SPEND_BUDGET_MICROS,
        ):
            return False
        legacy = await self.ledger._run(LEGACY_HISTORY, ())
        if legacy:
            return False
        rows = await self.ledger._run(
            "SELECT coalesce(sum(max_micros),0) AS used FROM spend_reservations", ()
        )
        return rows[0]["used"] + min(p.max_micros for p in valid) <= self.settings.MONID_SPEND_BUDGET_MICROS
