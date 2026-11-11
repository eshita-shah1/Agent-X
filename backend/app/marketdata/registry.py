"""The approved instrument universe (Phase 0/2 decisions). Changing it needs owner approval.

Display symbols and provider symbols are kept apart on purpose: the Yahoo symbol for an NSE equity is the
trading symbol plus a '.NS' suffix, but that is a provider convention, not an identity.

Futures (NIFTY, BANKNIFTY) are registered as DISABLED placeholders only. They carry no provider symbol,
no contract identifier and no expiry: contract-specific data has not been verified, and index levels are
never substituted for futures prices.
"""

from dataclasses import dataclass, field

from app.db.enums import InstrumentType

EXCHANGE = "NSE"
CURRENCY = "INR"
EXCHANGE_TIMEZONE = "Asia/Kolkata"
FUTURES_DISABLED_REASON = (
    "Disabled until genuine contract-specific futures data is available and verified; "
    "index values are not futures prices and no synthetic futures data is permitted."
)


@dataclass(frozen=True)
class RegistryEntry:
    symbol: str
    name: str
    instrument_type: InstrumentType
    provider_symbols: dict[str, str] = field(default_factory=dict)  # provider code -> provider symbol
    enabled: bool = True
    disabled_reason: str | None = None


def _equity(symbol: str, name: str) -> RegistryEntry:
    return RegistryEntry(symbol, name, InstrumentType.EQUITY, {"yahoo": f"{symbol}.NS"})


APPROVED_EQUITIES: tuple[RegistryEntry, ...] = (
    _equity("RELIANCE", "Reliance Industries"),
    _equity("HDFCBANK", "HDFC Bank"),
    _equity("ICICIBANK", "ICICI Bank"),
    _equity("SBIN", "State Bank of India"),
    _equity("INFY", "Infosys"),
    _equity("TCS", "Tata Consultancy Services"),
    _equity("LT", "Larsen & Toubro"),
    _equity("BHARTIARTL", "Bharti Airtel"),
    _equity("ITC", "ITC"),
    _equity("AXISBANK", "Axis Bank"),
)

FUTURES_PLACEHOLDERS: tuple[RegistryEntry, ...] = (
    RegistryEntry("NIFTY", "NIFTY index futures", InstrumentType.FUTURE, {}, False, FUTURES_DISABLED_REASON),
    RegistryEntry("BANKNIFTY", "BANKNIFTY index futures", InstrumentType.FUTURE, {}, False, FUTURES_DISABLED_REASON),
)

ALL_ENTRIES: tuple[RegistryEntry, ...] = APPROVED_EQUITIES + FUTURES_PLACEHOLDERS
