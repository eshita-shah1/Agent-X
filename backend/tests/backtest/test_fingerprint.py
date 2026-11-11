"""Canonical serialization and the three fingerprints (correction B2: the result fingerprint covers every output)."""

import dataclasses
import subprocess
import sys
from datetime import datetime
from decimal import Decimal

import pytest

from app.backtest import (
    BacktestRequest,
    BenchmarkStatus,
    CostModel,
    InstrumentData,
    canonical,
    config_fingerprint,
    digest,
    result_fingerprint,
    run_backtest,
)
from app.strategies import ORBConfig
from app.strategies.selection import Candidate, PinnedPolicy, RegimeConfig
from tests.backtest import CANDIDATE, D, NEXT_DAY, POLICY, THIRD_DAY, bar, scripted, session

BACKEND = __import__("pathlib").Path(__file__).resolve().parents[2]


def req(**kw):
    base = dict(instrument_ids=("X",), start=NEXT_DAY, end=THIRD_DAY, policy=POLICY, capital=D(100000), warmup_sessions=0)
    base.update(kw)
    return BacktestRequest(**base)


def test_canonical_form():
    assert canonical(D("1.50")) == canonical(D("1.5")) == "1.5" and canonical(D("100000.00")) == "100000"
    assert canonical(D("-0.00")) == "0" and canonical(0.1) == "0.1"
    assert canonical(datetime.fromisoformat("2026-11-03T09:15:00+05:30")) == "2026-11-03T03:45:00Z"
    with pytest.raises(ValueError):
        canonical(datetime(2026, 11, 3, 9, 15))
    with pytest.raises(TypeError):
        canonical(object())
    assert digest({"b": 1, "a": [D("2.0")]}) == digest({"a": [D(2)], "b": 1})


def test_config_fingerprint_is_stable_and_sensitive():
    base = config_fingerprint(req())
    assert base == config_fingerprint(req()) == config_fingerprint(req(capital=D("100000.00")))
    variants = [
        req(capital=D(100001)),
        req(end=NEXT_DAY),
        req(warmup_sessions=1),
        req(cost_model=CostModel(slippage_bps=D(1))),
        req(instrument_ids=("Y",)),
        req(policy=PinnedPolicy(Candidate("s-2", "ORB", CANDIDATE.config))),
        req(policy=PinnedPolicy(Candidate("s-1", "ORB", ORBConfig(range_minutes=30)))),
        req(policy=PinnedPolicy(CANDIDATE, RegimeConfig(trend_min=26.0))),
    ]
    prints = [config_fingerprint(v) for v in variants]
    assert base not in prints and len(set(prints)) == len(prints)


def backtest(bars, **kw):
    return run_backtest(req(end=NEXT_DAY, **kw), {"X": InstrumentData("X", tuple(bars))}, strategy_factory=scripted({"10:00": None}))


def test_data_fingerprint_covers_prices_and_source_references():
    bars = session(NEXT_DAY)
    base = backtest(bars).data_fingerprint
    assert backtest(bars).data_fingerprint == base
    changed_price = bars[:10] + [bar(NEXT_DAY, "10:05", "100.01")] + bars[11:]
    changed_ref = bars[:10] + [bar(NEXT_DAY, "10:05", 100, source_ref=(42, 2))] + bars[11:]
    assert len({base, backtest(changed_price).data_fingerprint, backtest(changed_ref).data_fingerprint}) == 3


def test_result_fingerprint_covers_every_output():
    result = backtest(session(NEXT_DAY, overrides={"10:30": (100, 100.5, 98.5, 99.5)}))
    (inst,) = result.instruments
    assert result_fingerprint(result.instruments) == result.result_fingerprint
    t, e, p = inst.trades[0], inst.events[0], inst.equity[5]
    mutations = {
        "trades": dataclasses.replace(inst, trades=(dataclasses.replace(t, exit_price=t.exit_price + 1),)),
        "equity": dataclasses.replace(inst, equity=inst.equity[:5] + (dataclasses.replace(p, cash=p.cash + 1),) + inst.equity[6:]),
        "selection": dataclasses.replace(inst, selection_decisions=({**inst.selection_decisions[0], "reason": "X"},)),
        "events": dataclasses.replace(inst, events=(dataclasses.replace(e, config_id="other"),) + inst.events[1:]),
        "data_quality": dataclasses.replace(inst, data_quality=dataclasses.replace(inst.data_quality, missing_bars=1)),
        "benchmark": dataclasses.replace(inst, benchmark=dataclasses.replace(inst.benchmark, status=BenchmarkStatus.LIMITED)),
        "benchmark_limitations": dataclasses.replace(inst, benchmark=dataclasses.replace(inst.benchmark, limitations=("MISSING_BARS",))),
        "summary": dataclasses.replace(inst, summary=dataclasses.replace(inst.summary, entry_signals_ignored=9)),
    }
    prints = {name: result_fingerprint((m,)) for name, m in mutations.items()}
    assert result.result_fingerprint not in prints.values() and len(set(prints.values())) == len(prints)


SCRIPT = """
import sys; sys.path.insert(0, '.')
from tests.backtest.test_properties import full_run
r = full_run()
print(r.config_fingerprint, r.data_fingerprint, r.result_fingerprint)
"""


def test_fingerprints_do_not_depend_on_the_hash_seed_or_process():
    outputs = set()
    for seed in ("0", "1", "12345"):
        env = {**__import__("os").environ, "PYTHONHASHSEED": seed}
        out = subprocess.run([sys.executable, "-c", SCRIPT], cwd=BACKEND, env=env, capture_output=True, text=True, check=True)
        outputs.add(out.stdout.strip())
    assert len(outputs) == 1 and len(next(iter(outputs)).split()) == 3


def test_results_hold_decimals_not_floats():
    result = backtest(session(NEXT_DAY))
    assert type(result.capital) is Decimal

    def walk(value, path):
        if isinstance(value, float):
            raise AssertionError(f"float at {path}")
        if dataclasses.is_dataclass(value):
            for f in dataclasses.fields(value):
                walk(getattr(value, f.name), f"{path}.{f.name}")
        elif isinstance(value, tuple):
            for i, v in enumerate(value):
                walk(v, f"{path}[{i}]")

    for inst in result.instruments:  # selection explanations carry float64 indicator values by design; money never does
        walk(dataclasses.replace(inst, selection_decisions=()), "instrument")


# ---- time-zone hardening (Phase 3 finalization) -------------------------------------------------------------------


def _in_zone(bars, zone):
    from zoneinfo import ZoneInfo

    return [dataclasses.replace(b, start_ts=b.start_ts.astimezone(ZoneInfo(zone))) for b in bars]


def test_equal_instants_in_any_zone_give_identical_outcomes_and_fingerprints():
    from tests.backtest.test_properties import BARS, REQUEST

    results = [
        run_backtest(REQUEST, {"X": InstrumentData("X", tuple(_in_zone(BARS, zone)))})
        for zone in ("UTC", "Asia/Kolkata", "America/New_York", "Pacific/Kiritimati")
    ]
    assert len({(r.data_fingerprint, r.result_fingerprint) for r in results}) == 1
    assert all(r.instruments == results[0].instruments for r in results)  # trades, equity, events, decisions, benchmark
    inst = results[0].instruments[0]
    assert inst.trades and all(t.entry_bar_start.utcoffset().total_seconds() == 0 for t in inst.trades)
    d = inst.selection_decisions[0]
    assert d["checkpoint"].endswith("T04:00:00Z") and d["regime"]["input_bar_start"].endswith("Z")  # 09:30 / 09:25 IST


def test_bars_are_stored_in_utc_but_keep_their_ist_session():
    b = bar(NEXT_DAY, "09:15", 100)
    assert b.start_ts.utcoffset().total_seconds() == 0 and b.start_ts.hour == 3 and b.start_ts.minute == 45
    assert b.session_date == NEXT_DAY


def test_naive_or_offsetless_timestamps_are_rejected():
    import datetime as dt

    from app.backtest import BacktestBar

    class NoOffset(dt.tzinfo):
        def utcoffset(self, _):
            return None

    for ts in (dt.datetime(2026, 11, 3, 9, 15), dt.datetime(2026, 11, 3, 9, 15, tzinfo=NoOffset())):
        with pytest.raises(ValueError, match="timezone-aware"):
            BacktestBar(ts, NEXT_DAY, D(1), D(1), D(1), D(1), 1)
