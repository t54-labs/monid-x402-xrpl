"""Turn a tool's upstream USD price and a request body into an RLUSD quote.

x402 `exact` needs one fixed amount before the work runs, but most upstream prices
depend on the request (results asked for, seconds of video, chosen model) and some on
the output. The quote reads every size the request states and falls back to
configured defaults for the rest. It is an estimate for variable prices; every run
records the upstream's real cost so under-quoted tools show up in reconciliation.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal
from typing import Any

from .catalog import Tool
from .config import Settings

# Request fields that state how many results a call returns.
RESULT_COUNT_KEYS = (
    "limit",
    "maxItems",
    "max_items",
    "maxResults",
    "max_results",
    "numResults",
    "num_results",
    "count",
    "num",
    "size",
    "pageSize",
    "page_size",
    "resultsLimit",
    "results_limit",
    "maxReviews",
    "max_reviews",
    "depth",
    "top_k",
)

# Request fields that state metered size, by billing unit.
UNIT_KEYS: dict[str, tuple[str, ...]] = {
    "second": ("duration", "seconds", "duration_seconds", "durationSeconds", "length"),
    "image": ("n", "num_images", "numImages", "image_count", "count"),
    "result": RESULT_COUNT_KEYS,
    "step": ("max_steps", "maxSteps"),
    "credit": (),
    "token": (),
}
TEXT_KEYS = ("text", "input", "prompt", "content")


class UnquotableError(ValueError):
    """The tool's price cannot be turned into a fixed quote."""


@dataclass(frozen=True)
class Quote:
    cost_usd: Decimal
    amount: str  # RLUSD value string sent as the x402 amount
    estimated: bool
    detail: dict[str, Any]


def _usd(price: Mapping[str, Any], key: str = "amount") -> Decimal:
    value = (price.get(key) or {}).get("value")
    if value is None:
        return Decimal(0)
    currency = (price.get(key) or {}).get("currency", "USD")
    if currency != "USD":
        raise UnquotableError(f"unsupported price currency {currency}")
    return Decimal(str(value))


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float) and value > 0:
        return math.ceil(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip()) or None
    return None


def _find(body: Any, keys: tuple[str, ...]) -> int | None:
    """First positive integer under any of `keys`, searching nested objects."""
    if not isinstance(body, Mapping):
        return None
    for key in keys:
        found = _positive_int(body.get(key))
        if found is not None:
            return found
    for value in body.values():
        if isinstance(value, Mapping):
            found = _find(value, keys)
            if found is not None:
                return found
    return None


class _Estimator:
    def __init__(self, body: Any, settings: Settings) -> None:
        self.body = body if isinstance(body, Mapping) else {}
        self.settings = settings
        self.estimated = False

    def result_count(self) -> int:
        found = _find(self.body, RESULT_COUNT_KEYS)
        if found is None:
            self.estimated = True
            return self.settings.DEFAULT_RESULT_UNITS
        return found

    def units(self, unit: str) -> int:
        if unit == "character":
            text = next((self.body[k] for k in TEXT_KEYS if isinstance(self.body.get(k), str)), None)
            if text is not None:
                return max(1, len(text))
        found = _find(self.body, UNIT_KEYS.get(unit, ()))
        if found is None:
            self.estimated = True
            return self.settings.DEFAULT_METERED_UNITS
        return found

    def matches(self, when: Mapping[str, Any]) -> bool:
        return all(self.body.get(key) == value for key, value in when.items())

    def price(self, price: Mapping[str, Any]) -> Decimal:
        kind = price.get("type")
        if kind == "PER_CALL":
            return _usd(price)
        if kind == "PER_RESULT":
            return _usd(price, "flatFee") + _usd(price) * self.result_count()
        if kind == "PER_UNIT":
            per = Decimal(str(price.get("per") or 1))
            units = Decimal(self.units(str(price.get("unit") or "")))
            # Proportional: "$100 per 1M characters" for 5,000 characters is $0.50.
            return _usd(price) * units / per
        if kind == "PER_UNIT_MATRIX":
            return self.price(self._variant(price))
        if kind == "TIERED":
            return self._tiered(price)
        raise UnquotableError(f"unsupported price type {kind}")

    def _variant(self, price: Mapping[str, Any]) -> Mapping[str, Any]:
        variants = [v for v in price.get("variants") or [] if isinstance(v.get("price"), Mapping)]
        if not variants:
            raise UnquotableError("price matrix has no variants")
        for variant in variants:
            if self.matches(variant.get("when") or {}):
                return variant["price"]
        # The request names no listed variant: quote the dearest so we never undercharge.
        self.estimated = True
        return max(variants, key=lambda v: _Estimator(self.body, self.settings).price(v["price"]))["price"]

    def _tiered(self, price: Mapping[str, Any]) -> Decimal:
        default = price.get("default")
        total = self.price(default) if isinstance(default, Mapping) else _usd(price)
        # Tiers counted from the output are read off the response after the run.
        # Tiers billed in the same unit are alternatives (720p seconds *or* 1080p
        # seconds), so each unit contributes its dearest tier; different units add up.
        measured: dict[tuple[Any, Any], Decimal] = {}
        for tier in price.get("tiers") or []:
            tier_price = tier.get("price")
            if not isinstance(tier_price, Mapping):
                continue
            when = tier.get("when")
            selector = tier.get("selector")
            if when is not None:
                if self.matches(when):
                    total += self.price(tier_price)
            elif selector is None:
                total += self.price(tier_price)  # unconditional fee, e.g. "Actor start"
            elif selector.get("offset"):
                continue  # overage beyond a free first unit; assume none
            else:
                unit = (tier_price.get("type"), tier_price.get("unit"))
                measured[unit] = max(measured.get(unit, Decimal(0)), self.price(tier_price))
        return total + sum(measured.values(), Decimal(0))


def to_rlusd(cost_usd: Decimal, settings: Settings) -> str:
    """Upstream USD cost -> RLUSD amount (1:1), with markup, rounded up, floored."""
    raw = cost_usd * (Decimal(1) + settings.markup)
    quantum = settings.price_quantum
    rounded = (raw / quantum).to_integral_value(rounding=ROUND_CEILING) * quantum
    amount = max(rounded, settings.min_price)
    return format(amount.normalize(), "f")


def quote_tool(tool: Tool, body: Any, settings: Settings) -> Quote:
    estimator = _Estimator(body, settings)
    cost = estimator.price(tool.price)
    return Quote(
        cost_usd=cost,
        amount=to_rlusd(cost, settings),
        estimated=estimator.estimated,
        detail={"upstreamPriceType": tool.price.get("type"), "costUsd": str(cost)},
    )


def base_quote(tool: Tool, settings: Settings) -> Quote:
    """Quote with no request body: the price a directory probe sees."""
    return quote_tool(tool, None, settings)
