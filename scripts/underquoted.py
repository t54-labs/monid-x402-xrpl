"""List paid runs where the upstream charged more than the buyer paid.

    uv run python -m scripts.underquoted [limit]

Variable-priced tools are quoted from the request before they run; this is the
check that those estimates hold. Tools that show up repeatedly need better defaults
(DEFAULT_RESULT_UNITS / DEFAULT_METERED_UNITS) or an exclusion.
"""

from __future__ import annotations

import asyncio
import sys

from gateway.config import Settings
from gateway.store import RunLedger


async def main(limit: int) -> None:
    ledger = RunLedger(Settings().DATABASE_PATH)
    rows = await ledger.under_quoted(limit)
    if not rows:
        print("no under-quoted runs")
    for row in rows:
        print(
            f"{row['tool']}: quoted {row['quote_rlusd']} RLUSD, upstream {row['upstream_cost_usd']} USD"
            f"{' (estimated quote)' if row['quote_estimated'] else ''} [{row['state']}]"
        )
    ledger.close()


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 100))
