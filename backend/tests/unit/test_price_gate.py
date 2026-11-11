"""Execution-price gate (decision D4): every acceptance requirement fails closed."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.db.enums import PriceBasis
from app.exchange.pricing import (
    DisabledExecutionPriceSource,
    ExecutionBar,
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
    kind=QuoteKind.LATEST_BAR_CLOSE,
    source_ref="candle:1",
    volume=1000,
)
POLICY = ExecutionPricePolicy(
    mode="verified_next_bar_open",
    approved_bases=frozenset({PriceBasis.AS_TRADED_VERIFIED}),
    approved_providers=frozenset({"approved"}),
    max_age=timedelta(minutes=15),
)


class FixedSource:
    name = "fixed"
    enabled = True

    def __init__(self, quote, bars=()):
        self.quote = quote
        self.bars = list(bars)

    def latest(self, instrument_id):
        return self.quote

    def bars_from(self, instrument_id, start):
        return [b for b in self.bars if b.bar_start >= start]


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
    empty = ExecutionPricePolicy("verified_next_bar_open", frozenset(), frozenset(), timedelta(minutes=15))
    assert gate(GOOD, empty).check(1).rejection is PriceRejection.UNAPPROVED_BASIS


def test_check_reports_provenance():
    d = gate(GOOD).check(1).as_dict()
    assert d["ok"] is True
    assert d["quote"]["provider_code"] == "approved"
    assert d["quote"]["price_basis"] == "AS_TRADED_VERIFIED"
    assert d["quote"]["source_ref"] == "candle:1"


# ---- fill bars (next-bar-open model) ------------------------------------------------------------------

ACCEPTED = NOW - timedelta(minutes=12)


def bar(minutes_after_accept: int, **over) -> ExecutionBar:
    start = ACCEPTED + timedelta(minutes=minutes_after_accept)
    fields = dict(
        instrument_id=1, bar_start=start, bar_end=start + timedelta(minutes=5), session_date=start.date(),
        open=Decimal("100"), high=Decimal("101"), low=Decimal("99"), close=Decimal("100.5"), volume=1000,
        observed_at=start + timedelta(minutes=5, seconds=30), provider_code="approved",
        price_basis=PriceBasis.AS_TRADED_VERIFIED, is_final=True, source_ref=f"candle:{minutes_after_accept}",
    )
    fields.update(over)
    return ExecutionBar(**fields)


def bars_gate(*bars):
    return ExecutionPriceGate(FixedSource(GOOD, bars), POLICY, lambda: NOW)


def test_fill_bars_are_returned_oldest_first_when_all_eligible():
    result = bars_gate(bar(0), bar(5)).fill_bars(1, ACCEPTED)
    assert [b.source_ref for b in result.bars] == ["candle:0", "candle:5"] and result.blocked_by is None


def test_disabled_source_yields_no_fill_bars():
    result = ExecutionPriceGate(DisabledExecutionPriceSource(), POLICY, lambda: NOW).fill_bars(1, ACCEPTED)
    assert result.bars == [] and result.blocked_by is PriceRejection.SOURCE_DISABLED


@pytest.mark.parametrize(
    "bad,expected",
    [
        (dict(price_basis=PriceBasis.UNVERIFIED_PROVIDER_NATIVE), PriceRejection.UNVERIFIED_BASIS),
        (dict(provider_code="other"), PriceRejection.UNAPPROVED_PROVIDER),
        (dict(is_final=False), PriceRejection.NOT_FINAL),
        (dict(observed_at=NOW + timedelta(seconds=1)), PriceRejection.FUTURE_TIMESTAMP),
        (dict(low=Decimal("0")), PriceRejection.NON_POSITIVE_PRICE),
    ],
)
def test_an_ineligible_bar_stops_iteration_and_is_never_skipped(bad, expected):
    # A later eligible bar must not be used to "skip over" an ineligible one.
    result = bars_gate(bar(0, **bad), bar(5)).fill_bars(1, ACCEPTED)
    assert result.bars == [] and result.blocked_by is expected


def test_a_bar_that_started_before_acceptance_is_refused():
    class Leaky(FixedSource):
        def bars_from(self, instrument_id, start):
            return self.bars  # ignores `start`: a buggy source

    g = ExecutionPriceGate(Leaky(GOOD, [bar(-5)]), POLICY, lambda: NOW)
    result = g.fill_bars(1, ACCEPTED)
    assert result.bars == [] and result.blocked_by is PriceRejection.FUTURE_TIMESTAMP


def test_fill_bars_are_not_subject_to_the_reference_staleness_rule():
    # The order was accepted on fresh data; the bar right after acceptance is the correct fill bar even if
    # processing happens later. (A market order is still limited to the immediately next bar by the exchange.)
    old_accept = NOW - timedelta(hours=2)
    g = ExecutionPriceGate(FixedSource(GOOD, [bar(0, bar_start=old_accept, bar_end=old_accept + timedelta(minutes=5),
                                                       observed_at=old_accept + timedelta(minutes=6))]), POLICY, lambda: NOW)
    assert len(g.fill_bars(1, old_accept).bars) == 1


# ---- simulation-only mode (D4 simulation decision) ------------------------------------------------------

SIM = replace(POLICY, mode="simulation_provider_native", approved_bases=frozenset())
NATIVE = replace(GOOD, price_basis=PriceBasis.UNVERIFIED_PROVIDER_NATIVE)


def test_simulation_mode_accepts_only_provider_native_data():
    assert gate(NATIVE, SIM).check(1).ok
    verified = gate(GOOD, SIM).check(1)  # a verified label is NOT accepted in simulation mode: modes never mix
    assert verified.rejection is PriceRejection.BASIS_NOT_SIMULATION


def test_verified_mode_never_accepts_provider_native_data():
    assert gate(NATIVE, POLICY).check(1).rejection is PriceRejection.UNVERIFIED_BASIS


@pytest.mark.parametrize(
    "bad,expected",
    [
        (dict(as_of=NOW - timedelta(minutes=15, seconds=1)), PriceRejection.STALE),
        (dict(is_final=False), PriceRejection.NOT_FINAL),
        (dict(provider_code="other"), PriceRejection.UNAPPROVED_PROVIDER),
        (dict(observed_at=NOW + timedelta(seconds=1)), PriceRejection.FUTURE_TIMESTAMP),
        (dict(price=Decimal("0")), PriceRejection.NON_POSITIVE_PRICE),
        (dict(open=Decimal("100"), low=Decimal("101"), high=Decimal("102")), PriceRejection.INVALID_OHLC),
        (dict(volume=-1), PriceRejection.INVALID_VOLUME),
    ],
)
def test_simulation_mode_keeps_every_other_rule(bad, expected):
    assert gate(replace(NATIVE, **bad), SIM).check(1).rejection is expected


def test_simulation_fill_bars_reject_zero_volume_and_verified_labels():
    sim = lambda *bars: ExecutionPriceGate(FixedSource(NATIVE, bars), SIM, lambda: NOW)  # noqa: E731
    native = dict(price_basis=PriceBasis.UNVERIFIED_PROVIDER_NATIVE)
    assert len(sim(bar(0, **native)).fill_bars(1, ACCEPTED).bars) == 1
    assert sim(bar(0, volume=0, **native)).fill_bars(1, ACCEPTED).blocked_by is PriceRejection.ZERO_VOLUME
    assert sim(bar(0)).fill_bars(1, ACCEPTED).blocked_by is PriceRejection.BASIS_NOT_SIMULATION


def test_a_misconfigured_source_reports_config_invalid():
    src = DisabledExecutionPriceSource("EXECUTION_APPROVAL_REFERENCE is required", config_invalid=True)
    check = ExecutionPriceGate(src, SIM, lambda: NOW).check(1)
    assert check.rejection is PriceRejection.CONFIG_INVALID and "APPROVAL_REFERENCE" in check.detail
