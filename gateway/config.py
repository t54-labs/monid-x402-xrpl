from __future__ import annotations

from decimal import Decimal, InvalidOperation
from functools import cached_property

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

RLUSD_CURRENCY_HEX = "524C555344000000000000000000000000000000"
RLUSD_MAINNET_ISSUER = "rMxCKbEDwqr76QuheSUMdEGf4B9xJ8m5De"
RLUSD_TESTNET_ISSUER = "rQhWct2fv4Vc4KRjRgMrxa8xPN9Zx9iLKV"
RLUSD_ISSUERS_BY_NETWORK = {
    "xrpl:0": RLUSD_MAINNET_ISSUER,
    "xrpl:1": RLUSD_TESTNET_ISSUER,
}
T54_FACILITATOR_SOURCE_TAG = 804681468

# Tools whose state lives in our single upstream workspace (inboxes, phone numbers,
# machines, files, browser sessions) would be shared by every buyer, and tools billed
# by wall-clock time cannot be quoted up front. Both stay off until per-buyer
# ownership exists.
DEFAULT_EXCLUDED_PROVIDERS = (
    "agentmail",
    "agentline",
    "saperly",
    "smolmachine",
    "sfs",
    "recall",
    "x402.browserbase.com",
)
# Same reasons, for single endpoints of otherwise stateless providers ("provider#/path",
# shell-style wildcards allowed).
DEFAULT_EXCLUDED_ENDPOINTS = (
    "blockrun.ai#/api/v1/phone/*",
    "blockrun.ai#/api/v1/voice/*",
    "tinyfish#/browser-sessions/*",
    "tinyfish#/browser-profiles/*",
)


def _csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _decimal(value: str, name: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{name} must be a decimal string") from exc
    if parsed < 0:
        raise ValueError(f"{name} must not be negative")
    return parsed


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
        hide_input_in_errors=True,
    )

    LOG_LEVEL: str = "INFO"
    PUBLIC_BASE_URL: str = "http://localhost:8000"
    DATABASE_PATH: str = "data/gateway.sqlite3"

    # Brand shown to directories and buyers. Like "Heurist Inference Router", the
    # upstream's name plus what the gateway does.
    GATEWAY_BRAND_NAME: str = "Monid Tool Router"
    GATEWAY_DESCRIPTION: str = (
        "Tool Router — Powered by Monid. Pay-per-call data, search, social, commerce, "
        "finance and generative AI tools for agents, settled in RLUSD on the XRP Ledger."
    )
    # Merchant icon for directories, and where the upstream keeps its provider logos.
    GATEWAY_LOGO_URL: str = "https://monid.ai/favicon.svg"
    UPSTREAM_LOGO_BASE_URL: str = "https://monid.ai/logos"
    # Strip the upstream platform's name from public tool text (for a white-label brand).
    HIDE_UPSTREAM_BRAND: bool = False
    UPSTREAM_BRAND_TERMS: str = "Monid"

    # Upstream (Monid). The catalog is public; running a tool needs the key.
    MONID_API_BASE_URL: str = "https://api.monid.ai"
    MONID_API_KEY: str = Field(default="", repr=False)
    MONID_REQUEST_TIMEOUT_SECONDS: float = Field(default=120.0, gt=0)
    MONID_MIN_BALANCE_USD: str = "5"
    MONID_BALANCE_CHECK_SECONDS: float = Field(default=60.0, gt=0)

    CATALOG_SNAPSHOT_PATH: str = "data/catalog.json"
    CATALOG_REFRESH_SECONDS: float = Field(default=3600.0, gt=0)
    CATALOG_PAGE_SIZE: int = Field(default=200, ge=1, le=1000)
    EXCLUDED_PROVIDERS: str = ",".join(DEFAULT_EXCLUDED_PROVIDERS)
    EXCLUDED_ENDPOINTS: str = ",".join(DEFAULT_EXCLUDED_ENDPOINTS)

    # Pricing: Monid cost (USD) -> RLUSD 1:1, times (1 + markup), rounded up.
    PRICE_MARKUP: str = "0"
    PRICE_QUANTUM_RLUSD: str = "0.0001"
    MIN_PRICE_RLUSD: str = "0.001"
    # Units assumed when a variable-priced tool's request does not state its size.
    DEFAULT_RESULT_UNITS: int = Field(default=10, ge=1)
    DEFAULT_METERED_UNITS: int = Field(default=10, ge=1)

    # x402 on XRPL. RLUSD only; cross-currency payments are refused.
    X402_PAYMENT_ENABLED: bool = True
    X402_XRPL_NETWORK: str = "xrpl:1"
    X402_XRPL_PAYTO_ADDRESS: str = ""
    X402_XRPL_FACILITATOR_URL: str = ""
    X402_XRPL_ASSET: str = RLUSD_CURRENCY_HEX
    X402_XRPL_ISSUER: str = RLUSD_TESTNET_ISSUER
    X402_SOURCE_TAG: int | None = T54_FACILITATOR_SOURCE_TAG
    X402_MAX_TIMEOUT_SECONDS: int = Field(default=600, ge=60, le=3600)

    # Async upstream runs: how long the paid request waits before handing back a job.
    RUN_WAIT_SECONDS: float = Field(default=240.0, ge=0)
    RUN_POLL_INTERVAL_SECONDS: float = Field(default=3.0, gt=0)
    MAX_CONCURRENT_RUNS: int = Field(default=32, ge=1)

    @field_validator("PRICE_MARKUP", "PRICE_QUANTUM_RLUSD", "MIN_PRICE_RLUSD", "MONID_MIN_BALANCE_USD")
    @classmethod
    def _validate_decimal(cls, value: str, info) -> str:
        _decimal(value, info.field_name)
        return value

    @field_validator("PRICE_QUANTUM_RLUSD")
    @classmethod
    def _validate_quantum(cls, value: str) -> str:
        if Decimal(value) <= 0:
            raise ValueError("PRICE_QUANTUM_RLUSD must be positive")
        return value

    @cached_property
    def markup(self) -> Decimal:
        return Decimal(self.PRICE_MARKUP)

    @cached_property
    def price_quantum(self) -> Decimal:
        return Decimal(self.PRICE_QUANTUM_RLUSD)

    @cached_property
    def min_price(self) -> Decimal:
        return Decimal(self.MIN_PRICE_RLUSD)

    @cached_property
    def min_balance_usd(self) -> Decimal:
        return Decimal(self.MONID_MIN_BALANCE_USD)

    @cached_property
    def excluded_providers(self) -> frozenset[str]:
        return frozenset(_csv(self.EXCLUDED_PROVIDERS))

    @cached_property
    def excluded_endpoints(self) -> frozenset[str]:
        return frozenset(_csv(self.EXCLUDED_ENDPOINTS))

    @cached_property
    def upstream_brand_terms(self) -> tuple[str, ...]:
        return _csv(self.UPSTREAM_BRAND_TERMS)

    @property
    def brand_name(self) -> str:
        return self.GATEWAY_BRAND_NAME.strip()

    @property
    def configuration_problems(self) -> list[str]:
        """Reasons the paid routes must stay closed. Empty means ready."""
        problems: list[str] = []
        if not self.brand_name:
            problems.append("GATEWAY_BRAND_NAME is not set")
        if not self.MONID_API_KEY.strip():
            problems.append("MONID_API_KEY is not set")
        if not self.X402_PAYMENT_ENABLED:
            return problems
        if not self.X402_XRPL_PAYTO_ADDRESS.strip():
            problems.append("X402_XRPL_PAYTO_ADDRESS is not set")
        if not self.X402_XRPL_FACILITATOR_URL.strip():
            problems.append("X402_XRPL_FACILITATOR_URL is not set")
        if self.X402_XRPL_ASSET.upper() != RLUSD_CURRENCY_HEX:
            problems.append("X402_XRPL_ASSET must be the RLUSD currency code")
        expected_issuer = RLUSD_ISSUERS_BY_NETWORK.get(self.X402_XRPL_NETWORK)
        if expected_issuer is not None and self.X402_XRPL_ISSUER != expected_issuer:
            problems.append(f"X402_XRPL_ISSUER does not match the RLUSD issuer for {self.X402_XRPL_NETWORK}")
        return problems
