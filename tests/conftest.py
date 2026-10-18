from __future__ import annotations

import pytest

from gateway.config import RLUSD_TESTNET_ISSUER, Settings

PAY_TO = "rPEPPER7kfTD9w2To4CQk6UCfuHM9c6GDY"


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    """The unit suite must never contact Monid, a facilitator, or any network."""
    import socket

    def blocked(*args, **kwargs):
        raise AssertionError("Outbound network is disabled in unit tests")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)

    # Defend against accidentally constructing Settings() with the developer's .env.
    monkeypatch.setitem(Settings.model_config, "env_file", None)


def make_settings(tmp_path, **overrides) -> Settings:
    values = {
        "GATEWAY_BRAND_NAME": "Monid Tool Router",
        "PUBLIC_BASE_URL": "https://tools.example",
        "DATABASE_PATH": str(tmp_path / "gateway.sqlite3"),
        "CATALOG_SNAPSHOT_PATH": str(tmp_path / "catalog.json"),
        "MONID_API_KEY": "test-key",
        "X402_XRPL_NETWORK": "xrpl:1",
        "X402_XRPL_ISSUER": RLUSD_TESTNET_ISSUER,
        "X402_XRPL_PAYTO_ADDRESS": PAY_TO,
        "X402_XRPL_FACILITATOR_URL": "https://facilitator.example",
        "RUN_POLL_INTERVAL_SECONDS": 0.01,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


@pytest.fixture
def settings(tmp_path) -> Settings:
    return make_settings(tmp_path)


def endpoint(provider: str, path: str, price: dict, **extra) -> dict:
    return {
        "provider": provider,
        "endpoint": path,
        "displayName": extra.pop("displayName", f"{provider} {path}"),
        "displayDescription": extra.pop("displayDescription", "A tool."),
        "providerDisplayName": extra.pop("providerDisplayName", provider.title()),
        "price": price,
        "categories": extra.pop("categories", [provider]),
        "tags": [],
        **extra,
    }


def per_call(value: float) -> dict:
    return {"type": "PER_CALL", "amount": {"value": value, "currency": "USD"}}
