"""Persist a probe report's findings: capability statuses with evidence, and per-symbol verification.

Kept apart from probe.py so the probe itself stays free of any database dependency.
"""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.enums import VerificationStatus
from app.db.models import DataProvider, InstrumentProviderSymbol, ProviderCapability
from app.marketdata.probe import derive_capabilities, derive_price_basis_observations
from app.marketdata.types import Capability, CapabilityStatus


def record_probe_report(session: Session, report: dict[str, Any]) -> dict[str, int]:
    provider = session.scalar(select(DataProvider).where(DataProvider.code == report["provider"]))
    if provider is None:
        raise ValueError(f"provider {report['provider']!r} is not registered; run the seed command first")
    now = datetime.now(UTC)
    caps = derive_capabilities(report)
    basis = derive_price_basis_observations(report)
    updated = 0
    for name, found in caps.items():
        evidence = dict(found["evidence"])
        if name in ("HISTORICAL_INTRADAY_CANDLES", "HISTORICAL_DAILY_CANDLES"):
            evidence["price_basis_observations"] = basis
        row = session.scalar(
            select(ProviderCapability).where(ProviderCapability.provider_id == provider.id, ProviderCapability.capability == Capability(name))
        )
        if row is None:
            row = ProviderCapability(provider_id=provider.id, capability=Capability(name), status=CapabilityStatus.UNKNOWN)
            session.add(row)
        row.status, row.note, row.evidence, row.probe_id, row.determined_at = CapabilityStatus(found["status"]), found.get("note"), evidence, report["probe_id"], now
        updated += 1

    verified = 0
    mappings = {
        m.provider_symbol: m
        for m in session.scalars(select(InstrumentProviderSymbol).where(InstrumentProviderSymbol.provider_id == provider.id))
    }
    for s in report["symbols"]:
        m = mappings.get(s["provider_symbol"])
        if m is None:
            continue
        ok = bool(s.get("five_minute", {}).get("success")) and s["five_minute"].get("returned_candles", 0) > 0
        transient = (not ok) and all(a.get("error_kind") in ("RATE_LIMITED", "UNAVAILABLE") for a in s.get("five_minute_attempts", [{}]))
        if transient:
            continue  # a network problem proves nothing about the symbol
        m.verification_status = VerificationStatus.VERIFIED if ok else VerificationStatus.FAILED
        m.verified_at = now
        m.verification_evidence = {
            "probe_id": report["probe_id"],
            "returned_candles": s.get("five_minute", {}).get("returned_candles"),
            "exchange_timezone_observed": s.get("request_meta", {}).get("index_tz"),
            "metadata": s.get("metadata"),
        }
        verified += 1
    session.commit()
    return {"capabilities_updated": updated, "symbols_verified": verified}
