"""Offline settlement reconciliation. Never loads .env or contacts an external service."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from gateway.invoices import InvoiceStore
from gateway.store import RunLedger


async def reconcile(args: argparse.Namespace) -> None:
    if not Path(args.database).is_file():
        raise ValueError("Database must already exist")
    if args.outcome == "settled" and (not args.transaction or not args.payer):
        raise ValueError("Settled outcome requires --transaction and --payer")
    ledger = RunLedger(args.database)
    try:
        invoices = InvoiceStore(ledger)
        reqs = await invoices.get(args.invoice)
        if not reqs:
            raise ValueError("Unknown invoice")
        settlement = (
            None
            if args.outcome == "not-settled"
            else {
                "success": True,
                "transaction": args.transaction,
                "payer": args.payer,
                "network": reqs[0].network,
            }
        )
        await invoices.reconcile(args.invoice, evidence=args.evidence, settlement=settlement)
    finally:
        ledger.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True)
    parser.add_argument("--invoice", required=True)
    parser.add_argument("--outcome", choices=("settled", "not-settled"), required=True)
    parser.add_argument("--evidence", required=True, help="Reference to verified facilitator/chain evidence")
    parser.add_argument("--transaction")
    parser.add_argument("--payer")
    parser.add_argument(
        "--gateway-stopped",
        action="store_true",
        required=True,
        help="Confirm all gateway processes using this database have stopped",
    )
    args = parser.parse_args()
    try:
        asyncio.run(reconcile(args))
    except ValueError as exc:
        parser.error(str(exc))
    print("Settlement decision recorded; execution remains locked.")


if __name__ == "__main__":
    main()
