"""Synthetic cost schedules for tests. NOT real NSE, SEBI, tax or broker rates; every expected value in tests that
use these fixtures is a formula check, not evidence of compatibility with any broker's contract notes."""

import copy
import json
from datetime import date
from pathlib import Path

from app.db.enums import Product
from app.exchange.costs import CostModel, CostSchedule, load_cost_schedule, parse_cost_schedule

FIXTURE_SCHEDULE_PATH = Path(__file__).parent / "fixtures" / "cost_schedule_synthetic.json"
FIXTURE_DATE = date(2026, 1, 5)


def fixture_schedule_dict() -> dict:
    return json.loads(FIXTURE_SCHEDULE_PATH.read_text())


def schedule_from(d: dict) -> CostSchedule:
    return parse_cost_schedule(json.dumps(d).encode())


def fixture_costs(product: Product = Product.DELIVERY, on: date = FIXTURE_DATE, participation=None) -> CostModel:
    return load_cost_schedule(FIXTURE_SCHEDULE_PATH).resolve(product, on, participation)


def modified(**changes) -> dict:
    """A copy of the fixture schedule with rate fields changed by rate_id: modified(**{"T-DP-D": {"value": "15"}})."""
    d = copy.deepcopy(fixture_schedule_dict())
    for r in d["rates"]:
        if r["rate_id"] in changes:
            r.update(changes[r["rate_id"]])
    return d
