"""Idempotent, insert-only seeding of the approved instrument registry and provider rows.

Existing rows are never modified: if someone has disabled an instrument, re-seeding does not re-enable it.
Declared (documented/unknown) capabilities are inserted only when missing, so probe evidence is never overwritten.
"""

from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.enums import TradingStatus, VerificationStatus
from app.db.models import DataProvider, Instrument, InstrumentProviderSymbol, ProviderCapability
from app.marketdata.providers.base import ProviderDescriptor
from app.marketdata.registry import ALL_ENTRIES, CURRENCY, EXCHANGE, EXCHANGE_TIMEZONE


def seed_registry(session: Session, descriptors: Iterable[ProviderDescriptor]) -> dict[str, int]:
    counts = {"providers_added": 0, "instruments_added": 0, "symbol_mappings_added": 0, "capabilities_added": 0}
    providers: dict[str, DataProvider] = {}
    for d in descriptors:
        row = session.scalar(select(DataProvider).where(DataProvider.code == d.code))
        if row is None:
            row = DataProvider(code=d.code, name=d.name, notes=d.notes or None)
            session.add(row)
            session.flush()
            counts["providers_added"] += 1
        providers[d.code] = row
        have = {c.capability for c in session.scalars(select(ProviderCapability).where(ProviderCapability.provider_id == row.id))}
        for declared in d.declared:
            if declared.capability not in have:
                session.add(
                    ProviderCapability(provider_id=row.id, capability=declared.capability, status=declared.status, note=declared.note or None, evidence={})
                )
                counts["capabilities_added"] += 1

    for e in ALL_ENTRIES:
        inst = session.scalar(
            select(Instrument).where(Instrument.exchange == EXCHANGE, Instrument.symbol == e.symbol, Instrument.instrument_type == e.instrument_type)
        )
        if inst is None:
            inst = Instrument(
                symbol=e.symbol, exchange=EXCHANGE, instrument_type=e.instrument_type, name=e.name, currency=CURRENCY,
                timezone=EXCHANGE_TIMEZONE,
                trading_status=TradingStatus.ENABLED if e.enabled else TradingStatus.DISABLED,
                disabled_reason=e.disabled_reason,
            )
            session.add(inst)
            session.flush()
            counts["instruments_added"] += 1
        for code, provider_symbol in e.provider_symbols.items():
            provider = providers.get(code)
            if provider is None:
                continue
            exists = session.scalar(
                select(InstrumentProviderSymbol).where(
                    InstrumentProviderSymbol.instrument_id == inst.id, InstrumentProviderSymbol.provider_id == provider.id
                )
            )
            if exists is None:
                session.add(
                    InstrumentProviderSymbol(
                        instrument_id=inst.id, provider_id=provider.id, provider_symbol=provider_symbol,
                        verification_status=VerificationStatus.UNVERIFIED,
                    )
                )
                counts["symbol_mappings_added"] += 1
    session.commit()
    return counts
