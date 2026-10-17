import httpx

from gateway.catalog import (
    build_catalog,
    fetch_upstream_listing,
    load_snapshot,
    resolve_logos,
    save_snapshot,
)

from .conftest import endpoint, per_call

TAXONOMY = [
    {"id": "search", "displayName": "Search & SEO", "kind": "top", "children": ["web-search", "exa"]},
    {"id": "web-search", "displayName": "Web Search", "kind": "category", "parents": ["search"]},
    {"id": "exa", "displayName": "Exa", "kind": "provider", "provider": "exa", "parents": ["search"]},
]


def test_excluded_providers_and_endpoints_are_dropped(tmp_path):
    from .conftest import make_settings

    settings = make_settings(tmp_path, EXCLUDED_ENDPOINTS="exa#/contents,tinyfish#/browser-*")
    catalog = build_catalog(
        [
            endpoint("exa", "/search", per_call(0.007)),
            endpoint("exa", "/contents", per_call(0.001)),
            endpoint("agentmail", "/inboxes", per_call(0.01)),
            endpoint("tinyfish", "/browser-sessions/provision", per_call(0.01)),
        ],
        TAXONOMY,
        settings,
    )
    assert [t.external_id for t in catalog.tools] == ["exa#/search"]


def test_upstream_brand_is_removed_when_hiding_is_on(tmp_path):
    from .conftest import make_settings

    settings = make_settings(tmp_path, HIDE_UPSTREAM_BRAND=True)
    catalog = build_catalog(
        [
            endpoint(
                "magichour",
                "/gif",
                per_call(0.01),
                displayDescription="AI media from Magic Hour. Monid offers its AI GIF Generator.",
            )
        ],
        [],
        settings,
    )
    assert "monid" not in catalog.tools[0].description.lower()
    assert catalog.tools[0].description.startswith("AI media from Magic Hour.")


def test_brand_is_kept_by_default(settings):
    catalog = build_catalog(
        [endpoint("magichour", "/gif", per_call(0), displayDescription="Monid's own FS")], [], settings
    )
    assert catalog.tools[0].description == "Monid's own FS"


def test_routes_and_reserved_labels(settings):
    catalog = build_catalog(
        [
            endpoint("defillama", "/coins/chart/{coins}", per_call(0.001)),
            endpoint("exa", "/search", per_call(0.007), categories=["exa", "web-search"]),
        ],
        TAXONOMY,
        settings,
    )
    chart = catalog.by_route("/t/defillama/coins/chart/{coins}")
    assert chart is not None
    assert chart.path == "/t/defillama/coins/chart/%7Bcoins%7D"
    exa = catalog.by_route("/t/exa/search")
    assert catalog.labels(exa) == {
        "category": "search",
        "subcategory": "web-search",
        "upstreamProvider": "Exa",
        "upstreamLogoUrl": None,
    }


async def test_listing_follows_cursor_pages():
    pages = {
        None: {"items": [endpoint("exa", "/search", per_call(0.007))], "cursor": "c2", "total": 2},
        "c2": {"items": [endpoint("exa", "/contents", per_call(0.001))], "cursor": None, "total": 2},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/public/v1/endpoints":
            return httpx.Response(200, json=pages[request.url.params.get("cursor")])
        if request.url.path == "/public/v1/categories":
            return httpx.Response(200, json={"items": TAXONOMY})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        endpoints, taxonomy = await fetch_upstream_listing(client, "https://api.example")
    assert [e["endpoint"] for e in endpoints] == ["/search", "/contents"]
    assert taxonomy == TAXONOMY


def test_snapshot_round_trip(settings, tmp_path):
    path = str(tmp_path / "snap" / "catalog.json")
    save_snapshot(path, [endpoint("exa", "/search", per_call(0.007))], TAXONOMY, "2026-10-17T00:00:00Z")
    catalog = load_snapshot(path, settings)
    assert catalog is not None
    assert catalog.synced_at == "2026-10-17T00:00:00Z"
    assert len(catalog.tools) == 1


async def test_logos_are_found_by_slug_or_catalog_id_and_cached():
    taxonomy = TAXONOMY + [
        {"id": "strale", "kind": "provider", "provider": "api.strale.io", "parents": ["data"]},
    ]
    present = {
        "https://cdn.example/logos/providers/exa.png",
        "https://cdn.example/logos/brands/strale.png",
    }
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if str(request.url) in present:
            return httpx.Response(200, headers={"content-type": "image/png"})
        return httpx.Response(404, headers={"content-type": "text/html"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        logos = await resolve_logos(
            client,
            ["exa", "api.strale.io", "nologo", "cached"],
            taxonomy,
            "https://cdn.example/logos",
            {"cached": "https://cdn.example/logos/providers/cached.svg"},
        )
    assert logos == {
        "exa": "https://cdn.example/logos/providers/exa.png",
        "api.strale.io": "https://cdn.example/logos/brands/strale.png",
        "cached": "https://cdn.example/logos/providers/cached.svg",
    }
    assert not any("cached" in url for url in seen)
