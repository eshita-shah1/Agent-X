"""B2 golden tests: every contract note in tests/golden/contract_notes must be reproduced exactly by the production
cost model. Synthetic notes prove the harness; real broker notes (when supplied) prove a schedule."""

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.exchange.contract_notes import evaluate_contract_note
from app.exchange.costs import load_cost_schedule
from tests.cost_fixtures import modified, schedule_from

GOLDEN = Path(__file__).resolve().parents[1] / "golden" / "contract_notes"
NOTES = sorted(GOLDEN.glob("*.json"))


@pytest.mark.parametrize("path", NOTES, ids=[p.stem for p in NOTES])
def test_contract_note_is_reproduced_exactly(path):
    note = json.loads(path.read_text(encoding="utf-8"))
    schedule = load_cost_schedule((path.parent / note["schedule"]).resolve())
    report = evaluate_contract_note(schedule, note)
    mismatches = [c for c in report.components if not c.ok]
    assert not mismatches, [(c.charge, str(c.expected), str(c.computed)) for c in mismatches]
    assert all(c.expected is not None for c in report.components), "every charge must be stated on the note"


def test_at_least_one_note_exists_and_synthetic_notes_are_labelled():
    assert NOTES
    for p in NOTES:
        note = json.loads(p.read_text(encoding="utf-8"))
        assert isinstance(note.get("synthetic"), bool) and note.get("source")


def _note(trades, expected=None):
    return {"contract_note_id": "T", "trade_date": "2026-01-05", "product": "DELIVERY", "synthetic": True,
            "trades": trades, "expected": expected or {}}


def test_dp_charge_once_per_scrip_per_day_and_mismatches_are_reported():
    sched = schedule_from(modified(**{"T-DP-D": {"value": "15"}}))
    trades = [
        {"order_ref": "S1", "instrument": "A", "side": "SELL", "quantity": 1, "price": "100"},
        {"order_ref": "S2", "instrument": "A", "side": "SELL", "quantity": 1, "price": "100"},
        {"order_ref": "S3", "instrument": "B", "side": "SELL", "quantity": 1, "price": "100"},
    ]
    r = evaluate_contract_note(sched, _note(trades, {"DP_CHARGE": "30"}))
    dp = next(c for c in r.components if c.charge == "DP_CHARGE")
    assert dp.computed == Decimal("30") and dp.ok  # A once + B once
    wrong = evaluate_contract_note(sched, _note(trades, {"DP_CHARGE": "45"}))
    assert not wrong.ok and not next(c for c in wrong.components if c.charge == "DP_CHARGE").ok


def test_contract_note_level_rounding_uses_the_session_stage():
    d = modified()
    for r in d["rounding"]:
        if r["charge_type"] == "STT":
            r.update(stage="SESSION", quantum="1", mode="HALF_UP")
    sched = schedule_from(d)
    trades = [{"order_ref": f"S{i}", "instrument": "A", "side": "SELL", "quantity": 1, "price": p}
              for i, p in enumerate(["1000", "600", "200", "1300"])]  # STT raw 2.5 + 1.5 + 0.5 + 3.25 = 7.75
    r = evaluate_contract_note(sched, _note(trades))
    assert next(c for c in r.components if c.charge == "STT").computed == Decimal("8")


def test_per_order_brokerage_cap_spans_the_order_s_trades():
    sched = schedule_from(modified())
    trades = [{"order_ref": "B1", "instrument": "A", "side": "BUY", "quantity": 100, "price": "150"},
              {"order_ref": "B1", "instrument": "A", "side": "BUY", "quantity": 100, "price": "150"}]
    r = evaluate_contract_note(sched, _note(trades))
    assert next(c for c in r.components if c.charge == "BROKERAGE").computed == Decimal("20")  # 15 + 15 capped at 20
