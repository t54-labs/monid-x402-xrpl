"""Sync the live upstream catalog and report how every tool prices.

    uv run python -m scripts.catalog_report

Needs no API key. Writes the snapshot to CATALOG_SNAPSHOT_PATH as a side effect.
"""

from __future__ import annotations

import asyncio
import collections
from decimal import Decimal

import httpx

from gateway.catalog import CatalogStore
from gateway.config import Settings
from gateway.pricing import UnquotableError, base_quote


async def main() -> None:
    settings = Settings()
    async with httpx.AsyncClient(timeout=30) as client:
        catalog = await CatalogStore(settings, client).refresh()

    by_type: collections.Counter[str] = collections.Counter()
    estimated = 0
    unquotable: list[str] = []
    amounts: list[Decimal] = []
    for tool in catalog.tools:
        by_type[str(tool.price.get("type"))] += 1
        try:
            quote = base_quote(tool, settings)
        except UnquotableError as exc:
            unquotable.append(f"{tool.external_id}: {exc}")
            continue
        estimated += quote.estimated
        amounts.append(Decimal(quote.amount))

    amounts.sort()
    print(f"tools listed: {len(catalog.tools)} (synced {catalog.synced_at})")
    print(f"excluded providers: {', '.join(sorted(settings.excluded_providers))}")
    print(f"price types: {dict(by_type)}")
    print(f"base quote uses default sizes: {estimated}")
    print(f"unquotable: {len(unquotable)}")
    for line in unquotable[:20]:
        print(f"  {line}")
    if amounts:
        mid = amounts[len(amounts) // 2]
        print(f"base quote RLUSD: min {amounts[0]}  median {mid}  max {amounts[-1]}")
        at_floor = sum(1 for a in amounts if a == settings.min_price)
        print(f"at the {settings.min_price} RLUSD floor: {at_floor}")


if __name__ == "__main__":
    asyncio.run(main())
