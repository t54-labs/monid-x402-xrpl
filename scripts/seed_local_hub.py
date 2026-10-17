"""Write this gateway into a LOCAL XRPL AI Hub database, for previewing the Hub UI.

    HUB_DATABASE_URL=postgresql://postgres:password@127.0.0.1:5440/x402_xrpl \\
        uv run python -m scripts.seed_local_hub

Reads the running gateway's /.well-known/x402 (every page) and upserts one Merchant
(the payTo address, named after the gateway, with its logo) and every tool as a
Resource, including the catalog labels the Hub's provider-grouping branch reads
(category, subcategory, upstreamProvider, externalId, and upstreamLogoUrl once that
column exists). Refuses any database that is not on a loopback host.
"""

from __future__ import annotations

import base64
import json
import os
import sys
from urllib.parse import urlparse

import httpx
import psycopg

GATEWAY = os.environ.get("GATEWAY_URL", "http://localhost:8000").rstrip("/")
DATABASE_URL = os.environ.get("HUB_DATABASE_URL", "")
CATEGORY = os.environ.get("HUB_MERCHANT_CATEGORY", "Data & Analytics")


def fetch_catalog() -> tuple[dict, list[dict]]:
    first = httpx.get(f"{GATEWAY}/.well-known/x402", timeout=30).json()
    resources = list(first["resources"])
    url = first.get("next")
    while url:
        page = httpx.get(url, timeout=30).json()
        resources.extend(page["resources"])
        url = page.get("next")
    return first, resources


def pay_to(tool_url: str) -> tuple[str, str]:
    """The payTo and asset code the gateway actually challenges with."""
    response = httpx.post(tool_url, json={}, timeout=30)
    if response.status_code != 402:
        raise SystemExit(f"gateway probe returned HTTP {response.status_code}; is it configured?")
    req = json.loads(base64.b64decode(response.headers["PAYMENT-REQUIRED"]))["accepts"][0]
    return req["payTo"], req["asset"]


def main() -> int:
    host = urlparse(DATABASE_URL).hostname
    if host not in ("127.0.0.1", "localhost", "::1"):
        print("HUB_DATABASE_URL must point at a local database")
        return 2
    meta, resources = fetch_catalog()
    address, asset = pay_to(resources[0]["url"])
    rows = [
        (
            address,
            r["url"],
            r["name"][:500],
            (r.get("description") or "")[:4000],
            r["price"]["amount"],
            asset,
            r["price"]["issuer"],
            r["price"]["network"],
            r.get("category"),
            r.get("subcategory"),
            r.get("upstreamProvider"),
            r.get("externalId"),
            r.get("upstreamLogoUrl"),
        )
        for r in resources
        if r.get("price")
    ]
    with psycopg.connect(DATABASE_URL) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO "Merchant" (address, name, description, website, "logoUrl", category,
                                    "categorySource", "categorizedAt", "createdAt", "updatedAt")
            VALUES (%s, %s, %s, %s, %s, %s, 'manual', now(), now(), now())
            ON CONFLICT (address) DO UPDATE SET name = EXCLUDED.name, description = EXCLUDED.description,
                website = EXCLUDED.website, "logoUrl" = EXCLUDED."logoUrl", category = EXCLUDED.category,
                "updatedAt" = now()
            """,
            (address, meta["name"], meta["description"], GATEWAY, meta.get("logoUrl"), CATEGORY),
        )
        cur.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'Resource' AND column_name = 'upstreamLogoUrl'"
        )
        has_logo_column = cur.fetchone() is not None
        if not has_logo_column:
            rows = [row[:-1] for row in rows]
        logo_column = ', "upstreamLogoUrl"' if has_logo_column else ""
        logo_value = ", %s" if has_logo_column else ""
        logo_update = ', "upstreamLogoUrl" = EXCLUDED."upstreamLogoUrl"' if has_logo_column else ""
        cur.executemany(
            f"""
            INSERT INTO "Resource" (id, "merchantAddr", url, name, description, "priceAmount", "priceAsset",
                                    "priceAssetIssuer", schema, network, category, subcategory,
                                    "upstreamProvider", "externalId"{logo_column}, "isDiscovered", "isActive",
                                    "createdAt", "updatedAt")
            VALUES (gen_random_uuid(), %s, %s, %s, %s, %s, %s, %s, 'x402', %s, %s, %s, %s, %s{logo_value},
                    false, true, now(), now())
            ON CONFLICT ("merchantAddr", url) DO UPDATE SET name = EXCLUDED.name,
                description = EXCLUDED.description, "priceAmount" = EXCLUDED."priceAmount",
                category = EXCLUDED.category, subcategory = EXCLUDED.subcategory,
                "upstreamProvider" = EXCLUDED."upstreamProvider", "externalId" = EXCLUDED."externalId"
                {logo_update}, "isActive" = true, "updatedAt" = now()
            """,
            rows,
        )
    logos = "with provider logos" if has_logo_column else "(Hub has no upstreamLogoUrl column yet)"
    print(f"seeded merchant {meta['name']} ({address}) with {len(rows)} tools {logos}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
