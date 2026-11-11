"""Execution-price gate (decision D4): every acceptance requirement fails closed."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.db.enums import PriceBasis
from app.exchange.pricing import (
    DisabledExecutionPriceSource,
    ExecutionPriceGate,
    ExecutionPricePolicy,
    ExecutionQuote,
    PriceRejection,
    QuoteKind,
)

NOW = datetime(2026, 1, 5, 5, 0, tzinfo=UTC)
GOOD = ExecutionQuote(
    instrument_id=1,
    price=Decimal("100"),
    as_of=NOW - timedelta(minutes=5),
    observed_at=NOW - timedelta(minutes=4),
    provider_code="approved",
    price_basis=PriceBasis.AS_TRADED_VERIFIED,
    is_final=True,
    kind=QuoteKind.CANDLE_CLOSE,
    source_ref="candle:1",
    volume=1000,
)
POLICY = ExecutionPricePolicy(
    approved_bases=frozenset({PriceBasis.AS_TRADED_VERIFIED}),
    approved_providers=frozenset({"approved"}),
    max_age=timedelta(minutes=15),
)


class FixedSource:
    name = "fixed"
    enabled = True

    def __init__(self, quote):
        self.quote = quote

    def latest(self, instrument_id):
        return self.quote


def gate(quote, policy=POLICY):
    return ExecutionPriceGate(FixedSource(quote), policy, lambda: NOW)


def test_eligible_quote_passes():
    check = gate(GOOD).check(1)
    assert check.ok and check.quote == GOOD and check.rejection is None


def test_disabled_source_blocks_everything():
    check = ExecutionPriceGate(DisabledExecutionPriceSource(), POLICY, lambda: NOW).check(1)
    assert not check.ok and check.rejection is PriceRejection.SOURCE_DISABLED
    assert "D4" in check.detail


@pytest.mark.parametrize(
    "quote,expected",
    [
        (None, PriceRejection.NO_DATA),
        (replace(GOOD, instrument_id=2), PriceRejection.INSTRUMENT_MISMATCH),
        (replace(GOOD, as_of=GOOD.as_of.replace(tzinfo=None)), PriceRejection.NAIVE_TIMESTAMP),
        (replace(GOOD, price=Decimal("0")), PriceRejection.NON_POSITIVE_PRICE),
        (replace(GOOD, is_final=False), PriceRejection.NOT_FINAL),
        (replace(GOOD, as_of=NOW + timedelta(seconds=1)), PriceRejection.FUTURE_TIMESTAMP),
        (replace(GOOD, observed_at=NOW + timedelta(seconds=1)), PriceRejection.FUTURE_TIMESTAMP),
        (replace(GOOD, as_of=NOW - timedelta(minutes=15, seconds=1)), PriceRejection.STALE),
        (replace(GOOD, price_basis=PriceBasis.UNVERIFIED_PROVIDER_NATIVE), PriceRejection.UNVERIFIED_BASIS),
        (replace(GOOD, price_basis=PriceBasis.SPLIT_ADJUSTED_VERIFIED), PriceRejection.UNAPPROVED_BASIS),
        (replace(GOOD, provider_code="other"), PriceRejection.UNAPPROVED_PROVIDER),
    ],
)
def test_each_requirement_is_enforced(quote, expected):
    check = gate(quote).check(1)
    assert not check.ok
    assert check.rejection is expected


def test_freshness_boundary_is_inclusive():
    assert gate(replace(GOOD, as_of=NOW - timedelta(minutes=15))).check(1).ok


def test_unverified_basis_is_rejected_even_if_a_policy_lists_it():
    lax = replace(POLICY, approved_bases=frozenset({PriceBasis.UNVERIFIED_PROVIDER_NATIVE}))
    check = gate(replace(GOOD, price_basis=PriceBasis.UNVERIFIED_PROVIDER_NATIVE), lax).check(1)
    assert check.rejection is PriceRejection.UNVERIFIED_BASIS


def test_empty_policy_approves_nothing():
    empty = ExecutionPricePolicy(frozenset(), frozenset(), timedelta(minutes=15))
    assert gate(GOOD, empty).check(1).rejection is PriceRejection.UNAPPROVED_BASIS


def test_check_reports_provenance():
    d = gate(GOOD).check(1).as_dict()
    assert d["ok"] is True
    assert d["quote"]["provider_code"] == "approved"
    assert d["quote"]["price_basis"] == "AS_TRADED_VERIFIED"
    assert d["quote"]["source_ref"] == "candle:1"
