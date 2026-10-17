from decimal import Decimal

import pytest

from gateway.catalog import build_catalog
from gateway.pricing import UnquotableError, quote_tool, to_rlusd

from .conftest import endpoint, make_settings, per_call


def tool(settings, price):
    return build_catalog([endpoint("p", "/x", price)], [], settings).tools[0]


def usd(value):
    return {"value": value, "currency": "USD"}


def test_per_call_is_cost_rounded_up_to_quantum(settings):
    quote = quote_tool(tool(settings, per_call(0.00123)), {}, settings)
    assert quote.cost_usd == Decimal("0.00123")
    assert quote.amount == "0.0013"
    assert quote.estimated is False


def test_minimum_price_applies_to_cheap_and_free_tools(settings):
    assert quote_tool(tool(settings, per_call(0.0002)), {}, settings).amount == "0.001"
    assert quote_tool(tool(settings, per_call(0)), {}, settings).amount == "0.001"


def test_markup_is_applied_before_rounding(tmp_path):
    settings = make_settings(tmp_path, PRICE_MARKUP="0.5")
    assert to_rlusd(Decimal("0.01"), settings) == "0.015"


def test_per_result_uses_requested_count_and_flat_fee(settings):
    price = {"type": "PER_RESULT", "amount": usd(0.001), "flatFee": usd(0.01)}
    quote = quote_tool(tool(settings, price), {"maxItems": 50}, settings)
    assert quote.cost_usd == Decimal("0.06")
    assert quote.estimated is False


def test_per_result_falls_back_to_default_count(settings):
    price = {"type": "PER_RESULT", "amount": usd(0.002)}
    quote = quote_tool(tool(settings, price), {"query": "x"}, settings)
    assert quote.cost_usd == Decimal("0.02")  # DEFAULT_RESULT_UNITS = 10
    assert quote.estimated is True


def test_per_unit_is_proportional(settings):
    price = {"type": "PER_UNIT", "amount": usd(0.12), "per": 60, "unit": "second"}
    assert quote_tool(tool(settings, price), {"duration": 90}, settings).cost_usd == Decimal("0.18")
    per_million = {"type": "PER_UNIT", "amount": usd(100), "per": 1_000_000, "unit": "character"}
    quote = quote_tool(tool(settings, per_million), {"text": "x" * 5000}, settings)
    assert quote.cost_usd == Decimal("0.5")


def test_per_unit_characters_count_text_length(settings):
    price = {"type": "PER_UNIT", "amount": usd(0.0001), "per": 1, "unit": "character"}
    quote = quote_tool(tool(settings, price), {"text": "hello"}, settings)
    assert quote.cost_usd == Decimal("0.0005")


def test_matrix_picks_the_variant_the_request_names(settings):
    price = {
        "type": "PER_UNIT_MATRIX",
        "amount": usd(0.042),
        "selectors": [{"key": "resolution", "in": "body"}],
        "variants": [
            {
                "when": {"resolution": "720p"},
                "price": {"type": "PER_UNIT", "amount": usd(0.04), "per": 1, "unit": "second"},
            },
            {
                "when": {"resolution": "1080p"},
                "price": {"type": "PER_UNIT", "amount": usd(0.07), "per": 1, "unit": "second"},
            },
        ],
    }
    t = tool(settings, price)
    assert quote_tool(t, {"resolution": "720p", "duration": 5}, settings).cost_usd == Decimal("0.20")
    # Unnamed variant: quote the dearest so the buyer never pays less than our cost.
    fallback = quote_tool(t, {"duration": 5}, settings)
    assert fallback.cost_usd == Decimal("0.35")
    assert fallback.estimated is True


def test_tiered_adds_matching_and_unconditional_tiers(settings):
    price = {
        "type": "TIERED",
        "amount": usd(0.001),
        "default": per_call(0.001),
        "tiers": [
            {"label": "JSON", "when": {"json": True}, "price": per_call(0.004)},
            {"label": "Start", "price": per_call(0.0005)},
            {
                "label": "Pages beyond the first",
                "selector": {"key": "numPages", "in": "output", "offset": 1},
                "price": {"type": "PER_RESULT", "amount": usd(0.001)},
            },
            {
                "label": "Reviews",
                "selector": {"key": "reviewCount", "in": "output"},
                "price": {"type": "PER_RESULT", "amount": usd(0.001)},
            },
        ],
    }
    t = tool(settings, price)
    assert quote_tool(t, {"json": True, "limit": 3}, settings).cost_usd == Decimal("0.0085")
    assert quote_tool(t, {"limit": 3}, settings).cost_usd == Decimal("0.0045")


def test_unknown_price_type_is_unquotable(settings):
    with pytest.raises(UnquotableError):
        quote_tool(tool(settings, {"type": "AUCTION"}), {}, settings)


def test_tiered_output_alternatives_in_one_unit_take_the_dearest(settings):
    def seconds(key, value):
        return {
            "selector": {"key": key, "in": "output"},
            "price": {"type": "PER_UNIT", "amount": usd(value), "per": 1, "unit": "second"},
        }

    price = {
        "type": "TIERED",
        "amount": usd(0),
        "tiers": [seconds("billed_seconds.720p", 0.084), seconds("billed_seconds.1080p", 0.112)],
    }
    quote = quote_tool(tool(settings, price), {"duration": 5}, settings)
    assert quote.cost_usd == Decimal("0.56")
