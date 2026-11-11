"""Operational commands for market data. Run from the backend directory:

    python -m app.marketdata.cli seed                 # register the approved instruments + provider (idempotent)
    python -m app.marketdata.cli probe --persist      # opt-in Yahoo probe (network); stores capability evidence
    python -m app.marketdata.cli backfill [--days 60] [--symbols RELIANCE,TCS]
    python -m app.marketdata.cli incremental [--symbols ...]
    python -m app.marketdata.cli coverage [--days 60]
    python -m app.marketdata.cli status

Nothing here starts on application start-up; ingestion is always an explicit, operator-initiated run.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.enums import InstrumentType, JobType
from app.db.models import DataIngestionJob, Instrument
from app.db.session import make_engine, make_sessionmaker
from app.marketdata import probe as probe_module
from app.marketdata.jobs import JobAlreadyRunning, create_job, resolve_targets, run_job
from app.marketdata.probe_store import record_probe_report
from app.marketdata.providers.registry import build_default_registry
from app.marketdata.seed import seed_registry
from app.marketdata.service import MarketDataService, utcnow

TIMEFRAME = "5m"


def _ctx():
    settings = get_settings()
    engine = make_engine(settings.database_url, settings.db_connect_timeout_seconds)
    return settings, engine, make_sessionmaker(engine), build_default_registry()


def _targets(svc: MarketDataService, provider: str, symbols: str | None):
    ids = None
    if symbols:
        wanted = [s.strip().upper() for s in symbols.split(",")]
        by_symbol = {i.symbol: i for i in svc.list_instruments(instrument_type=InstrumentType.EQUITY)}
        unknown = [s for s in wanted if s not in by_symbol]
        if unknown:
            raise SystemExit(f"unknown equity symbol(s): {unknown}")
        ids = [by_symbol[s].id for s in wanted]
    return resolve_targets(svc, provider, ids)


def cmd_seed(_args) -> int:
    settings, engine, factory, registry = _ctx()
    with factory() as s:
        print(json.dumps(seed_registry(s, registry.descriptors()), indent=2))
    return 0


def cmd_probe(args) -> int:
    argv = ["--days", str(args.days)]
    if args.symbols:
        argv += ["--symbols", args.symbols]
    rc = probe_module.main(argv)
    if args.persist:
        reports = sorted((Path(__file__).resolve().parents[3] / "data" / "probe_reports").glob("probe_*.json"))
        report = json.loads(reports[-1].read_text(encoding="utf-8"))
        settings, engine, factory, registry = _ctx()
        with factory() as s:
            print("persisted:", record_probe_report(s, report))
    return rc


def _run(args, job_type: JobType) -> int:
    settings, engine, factory, registry = _ctx()
    with factory() as s:
        svc = MarketDataService(s, registry, settings)
        targets = _targets(svc, settings.marketdata_provider, args.symbols)
        try:
            job = create_job(
                s, job_type=job_type, provider_code=settings.marketdata_provider, timeframe=TIMEFRAME,
                parameters={"instrument_ids": [t.id for t in targets], "days": getattr(args, "days", 60), "source": "cli"},
                user_id=None, settings=settings,
            )
        except JobAlreadyRunning as exc:
            print(f"refused: {exc}")
            return 2
        job_id = job.id
    print(f"job {job_id}: {job_type.value} for {len(targets)} instrument(s) ...", flush=True)
    run_job(factory, registry, settings, job_id)
    with factory() as s:
        job = s.get(DataIngestionJob, job_id)
        print(f"status: {job.status.value}")
        for r in job.summary.get("instruments", []):
            print(
                f"  {r.get('symbol', r['instrument_id']):11} {r['status']:9} received={r.get('rows_received', '-'):<5} "
                f"inserted={r.get('candles_inserted', '-'):<5} dup={r.get('duplicates_identical', '-'):<5} "
                f"restated={r.get('restatements', '-'):<3} quarantined={r.get('quarantined', '-'):<4} "
                f"non_final={r.get('non_final_dropped', '-'):<3} {r.get('error_kind') or ''}"
            )
        return 0 if job.status.value in ("SUCCEEDED", "PARTIAL") else 1


def cmd_coverage(args) -> int:
    settings, engine, factory, registry = _ctx()
    with factory() as s:
        svc = MarketDataService(s, registry, settings)
        end = utcnow().astimezone(svc.calendar.to_local(utcnow()).tzinfo).date()
        start = end - timedelta(days=args.days)
        for inst in svc.list_instruments(instrument_type=InstrumentType.EQUITY, enabled=True):
            c = svc.coverage(instrument_id=inst.id, timeframe=TIMEFRAME, start_day=start, end_day=end, sample_limit=5)
            f = svc.freshness(instrument_id=inst.id, timeframe=TIMEFRAME)
            print(
                f"{inst.symbol:11} candles={c['candle_count']:<5} first={str(c['first_candle_start'])[:16]} "
                f"missing={c['missing_intervals']:<4} partial_days={c['days_partial']:<3} absent={c['days_absent']} "
                f"unexplained={len(c['unexplained_absences'])} full={c['full_coverage_of_requested_range']} fresh={f['status']}"
            )
    return 0


def cmd_status(_args) -> int:
    settings, engine, factory, registry = _ctx()
    with factory() as s:
        svc = MarketDataService(s, registry, settings)
        h = svc.provider_health(settings.marketdata_provider)
        print(f"provider {settings.marketdata_provider}: {h.status.value} ({h.reason})")
        print(json.dumps(svc.thresholds_report(), indent=2, default=str))
        jobs = s.scalars(select(DataIngestionJob).order_by(DataIngestionJob.created_at.desc()).limit(5)).all()
        for j in jobs:
            print(f"job {j.id} {j.job_type.value} {j.status.value} created={j.created_at:%Y-%m-%d %H:%M}")
    return 0


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    ap = argparse.ArgumentParser(prog="python -m app.marketdata.cli")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("seed")
    p = sub.add_parser("probe")
    p.add_argument("--days", type=int, default=60)
    p.add_argument("--symbols")
    p.add_argument("--persist", action="store_true")
    for name in ("backfill", "incremental"):
        q = sub.add_parser(name)
        q.add_argument("--symbols")
        if name == "backfill":
            q.add_argument("--days", type=int, default=60)
    c = sub.add_parser("coverage")
    c.add_argument("--days", type=int, default=60)
    sub.add_parser("status")
    args = ap.parse_args(argv)
    handlers = {
        "seed": cmd_seed, "probe": cmd_probe, "coverage": cmd_coverage, "status": cmd_status,
        "backfill": lambda a: _run(a, JobType.BACKFILL), "incremental": lambda a: _run(a, JobType.INCREMENTAL),
    }
    return handlers[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
