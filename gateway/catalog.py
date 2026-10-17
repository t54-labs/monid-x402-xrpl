"""The tool catalog, synced from Monid's public (keyless) endpoint listing."""

from __future__ import annotations

import asyncio
import fnmatch
import json
import logging
import os
import re
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import httpx

from .config import Settings

logger = logging.getLogger(__name__)

TOOL_PATH_PREFIX = "/t"
CATALOG_PAGE_LIMIT = 50  # the public listing serves at most 50 per page


@dataclass(frozen=True)
class Tool:
    provider: str
    endpoint: str
    name: str
    description: str
    provider_name: str
    price: Mapping[str, Any]
    categories: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()

    @property
    def external_id(self) -> str:
        return f"{self.provider}#{self.endpoint}"

    @property
    def route_key(self) -> str:
        """The decoded request path this tool is served at."""
        return f"{TOOL_PATH_PREFIX}/{self.provider}{self.endpoint}"

    @property
    def path(self) -> str:
        """The same path, percent-encoded for use in a URL."""
        return quote(self.route_key, safe="/:._-~")


@dataclass
class Catalog:
    tools: list[Tool] = field(default_factory=list)
    taxonomy: list[Mapping[str, Any]] = field(default_factory=list)
    synced_at: str | None = None
    logos: dict[str, str] = field(default_factory=dict)  # provider slug -> logo URL

    def __post_init__(self) -> None:
        self._index = {tool.route_key: tool for tool in self.tools}
        self._parents = {item["id"]: item for item in self.taxonomy if item.get("parents")}

    def by_route(self, decoded_path: str) -> Tool | None:
        return self._index.get(decoded_path)

    def labels(self, tool: Tool) -> dict[str, str | None]:
        """Reserved directory fields: top category, subcategory, upstream provider."""
        subcategory = next(
            (c for c in tool.categories if self._parents.get(c, {}).get("kind") == "category"),
            None,
        )
        provider_item = next(
            (c for c in tool.categories if self._parents.get(c, {}).get("kind") == "provider"),
            None,
        )
        anchor = self._parents.get(subcategory or provider_item or "", {})
        parents = anchor.get("parents") or []
        return {
            "category": parents[0] if parents else None,
            "subcategory": subcategory,
            "upstreamProvider": tool.provider_name,
            "upstreamLogoUrl": self.logos.get(tool.provider),
        }


def _redact(text: str, terms: Iterable[str]) -> str:
    for term in terms:
        text = re.sub(rf"\b{re.escape(term)}('s)?\b", "", text, flags=re.IGNORECASE)
    return re.sub(r"\s{2,}", " ", text).strip()


def build_catalog(
    endpoints: Iterable[Mapping[str, Any]],
    taxonomy: Iterable[Mapping[str, Any]],
    settings: Settings,
    *,
    synced_at: str | None = None,
    logos: Mapping[str, str] | None = None,
) -> Catalog:
    terms = settings.upstream_brand_terms if settings.HIDE_UPSTREAM_BRAND else ()
    tools: dict[str, Tool] = {}
    for item in endpoints:
        provider = str(item.get("provider") or "").strip()
        endpoint = str(item.get("endpoint") or "").strip()
        price = item.get("price")
        if not provider or not endpoint.startswith("/") or not isinstance(price, Mapping):
            continue
        if provider in settings.excluded_providers:
            continue
        external_id = f"{provider}#{endpoint}"
        if any(fnmatch.fnmatchcase(external_id, pattern) for pattern in settings.excluded_endpoints):
            continue
        tool = Tool(
            provider=provider,
            endpoint=endpoint,
            name=_redact(str(item.get("displayName") or endpoint), terms),
            description=_redact(str(item.get("displayDescription") or ""), terms),
            provider_name=_redact(str(item.get("providerDisplayName") or provider), terms),
            price=price,
            categories=tuple(item.get("categories") or ()),
            tags=tuple(item.get("tags") or ()),
        )
        tools[tool.route_key] = tool
    ordered = sorted(tools.values(), key=lambda t: (t.provider, t.endpoint))
    return Catalog(tools=ordered, taxonomy=list(taxonomy), synced_at=synced_at, logos=dict(logos or {}))


async def fetch_upstream_listing(
    client: httpx.AsyncClient,
    base_url: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Page through the public endpoint listing; returns (endpoints, taxonomy)."""
    base = base_url.rstrip("/")
    endpoints: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        params: dict[str, Any] = {"limit": CATALOG_PAGE_LIMIT}
        if cursor:
            params["cursor"] = cursor
        response = await client.get(f"{base}/public/v1/endpoints", params=params)
        response.raise_for_status()
        page = response.json()
        items = page.get("items") or []
        endpoints.extend(items)
        cursor = page.get("cursor")
        if not cursor or not items:
            break
    response = await client.get(f"{base}/public/v1/categories")
    response.raise_for_status()
    taxonomy = response.json().get("items") or []
    return endpoints, taxonomy


LOGO_EXTENSIONS = ("svg", "png", "jpg", "jpeg", "webp")


def logo_candidates(provider: str, taxonomy: Iterable[Mapping[str, Any]], base_url: str) -> list[str]:
    """Where the upstream site keeps a provider's logo: by slug or catalog id, two folders."""
    item_id = next(
        (i.get("id") for i in taxonomy if i.get("kind") == "provider" and i.get("provider") == provider),
        None,
    )
    slugs = dict.fromkeys(s for s in (provider.replace(".", "-"), item_id) if s)
    base = base_url.rstrip("/")
    return [
        f"{base}/{folder}/{slug}.{ext}"
        for folder in ("providers", "brands")
        for slug in slugs
        for ext in LOGO_EXTENSIONS
    ]


async def resolve_logos(
    client: httpx.AsyncClient,
    providers: Iterable[str],
    taxonomy: Iterable[Mapping[str, Any]],
    base_url: str,
    known: Mapping[str, str],
) -> dict[str, str]:
    """Find each provider's logo once; providers without one get the directory's initial tile."""
    taxonomy = list(taxonomy)
    found = {p: u for p, u in known.items()}
    semaphore = asyncio.Semaphore(8)

    async def probe(provider: str) -> None:
        for url in logo_candidates(provider, taxonomy, base_url):
            async with semaphore:
                try:
                    response = await client.head(url, timeout=10, follow_redirects=True)
                except httpx.HTTPError:
                    continue
            if response.status_code == 200 and response.headers.get("content-type", "").startswith("image/"):
                found[provider] = url
                return

    await asyncio.gather(*(probe(p) for p in set(providers) if p not in found))
    return found


def save_snapshot(
    path: str,
    endpoints: list[dict[str, Any]],
    taxonomy: list[dict[str, Any]],
    synced_at: str,
    logos: Mapping[str, str] | None = None,
) -> None:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=directory, delete=False, encoding="utf-8") as tmp:
        json.dump(
            {"syncedAt": synced_at, "endpoints": endpoints, "taxonomy": taxonomy, "logos": dict(logos or {})},
            tmp,
        )
        temp_name = tmp.name
    os.replace(temp_name, path)


def load_snapshot(path: str, settings: Settings) -> Catalog | None:
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return None
    return build_catalog(
        raw.get("endpoints") or [],
        raw.get("taxonomy") or [],
        settings,
        synced_at=raw.get("syncedAt"),
        logos=raw.get("logos") or {},
    )


class CatalogStore:
    """Holds the live catalog and refreshes it in the background."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self._settings = settings
        self._client = client
        self.catalog = load_snapshot(settings.CATALOG_SNAPSHOT_PATH, settings) or Catalog()

    async def refresh(self) -> Catalog:
        from datetime import UTC, datetime

        endpoints, taxonomy = await fetch_upstream_listing(self._client, self._settings.MONID_API_BASE_URL)
        synced_at = datetime.now(UTC).isoformat()
        logos = await resolve_logos(
            self._client,
            (str(e.get("provider") or "") for e in endpoints if e.get("provider")),
            taxonomy,
            self._settings.UPSTREAM_LOGO_BASE_URL,
            self.catalog.logos,
        )
        catalog = build_catalog(endpoints, taxonomy, self._settings, synced_at=synced_at, logos=logos)
        if not catalog.tools:
            raise RuntimeError("upstream listing returned no usable tools")
        await asyncio.to_thread(
            save_snapshot, self._settings.CATALOG_SNAPSHOT_PATH, endpoints, taxonomy, synced_at, logos
        )
        self.catalog = catalog
        logger.info("catalog synced: %d tools", len(catalog.tools))
        return catalog

    async def run_forever(self) -> None:
        while True:
            try:
                await self.refresh()
            except Exception:  # keep serving the last good catalog
                logger.exception("catalog refresh failed; keeping %d tools", len(self.catalog.tools))
            await asyncio.sleep(self._settings.CATALOG_REFRESH_SECONDS)
