"""Approved universe, futures restrictions and architectural boundaries (static checks)."""

import pathlib
import re

from app.db.enums import InstrumentType
from app.marketdata.registry import ALL_ENTRIES, APPROVED_EQUITIES, FUTURES_PLACEHOLDERS

APP = pathlib.Path(__file__).resolve().parents[2] / "app"
APPROVED = ["RELIANCE", "HDFCBANK", "ICICIBANK", "SBIN", "INFY", "TCS", "LT", "BHARTIARTL", "ITC", "AXISBANK"]


def test_exactly_the_ten_approved_equities_in_order():
    assert [e.symbol for e in APPROVED_EQUITIES] == APPROVED
    assert all(e.instrument_type is InstrumentType.EQUITY and e.enabled for e in APPROVED_EQUITIES)


def test_yahoo_symbols_are_explicit_mappings_not_identities():
    for e in APPROVED_EQUITIES:
        assert e.provider_symbols == {"yahoo": f"{e.symbol}.NS"}
        assert e.provider_symbols["yahoo"] != e.symbol


def test_futures_are_disabled_placeholders_without_provider_symbols():
    assert [e.symbol for e in FUTURES_PLACEHOLDERS] == ["NIFTY", "BANKNIFTY"]
    for e in FUTURES_PLACEHOLDERS:
        assert e.instrument_type is InstrumentType.FUTURE and not e.enabled
        assert e.provider_symbols == {}  # index levels are never substituted for futures prices
        assert e.disabled_reason and "contract-specific" in e.disabled_reason


def test_no_unapproved_instruments_are_registered():
    assert len(ALL_ENTRIES) == 12


def test_only_the_yahoo_adapter_imports_yfinance():
    offenders = []
    for path in APP.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if re.search(r"^\s*(import|from)\s+yfinance\b", text, re.M) and path.name not in ("yahoo.py", "probe.py"):
            offenders.append(str(path.relative_to(APP)))
    assert offenders == []


def test_pandas_never_leaks_past_the_provider_adapter():
    """Provider responses become RawBar objects inside the adapter; nothing downstream sees a DataFrame."""
    for path in (APP / "marketdata").rglob("*.py"):
        if path.name == "yahoo.py":
            continue
        assert not re.search(r"^\s*(import|from)\s+pandas\b", path.read_text(encoding="utf-8"), re.M), path.name


def test_market_data_does_not_depend_on_trading_logic():
    forbidden = re.compile(r"app\.(strategies|risk|exchange|agent|backtest|benchmark|llm|portfolio)\b")
    for path in (APP / "marketdata").rglob("*.py"):
        assert not forbidden.search(path.read_text(encoding="utf-8")), path.name


def test_no_scheduler_or_trading_agent_is_started_by_the_market_data_package():
    for path in APP.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"^\s*(import|from)\s+(langgraph|apscheduler)\b", text, re.M), str(path.relative_to(APP))


def test_no_synthetic_data_generators_in_application_code():
    for path in APP.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"random\.(random|uniform|gauss|normal)|np\.random|ffill|interpolate\(|fillna\(", text), path.name
