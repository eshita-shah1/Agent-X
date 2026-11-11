"""Operational commands. Run from the backend directory:

    python -m app.ops.cli daily [--coverage-days 7] [--report-dir DIR]
    python -m app.ops.cli latency [--days 30]
    python -m app.ops.cli set-operator --email EMAIL (--on | --off)
    python -m app.ops.cli check-contract-note NOTE.json [--schedule SCHEDULE.json]
    python -m app.ops.cli staging-validate --staging-db NAME_staging --schedule SCHEDULE.json --approval-reference REF

Scheduling (owner decision B6): Agent-X never schedules itself (Phase 2 decision 5). `daily` is a one-shot, idempotent
command meant to be run by an external scheduler (cron, launchd, Windows Task Scheduler): see docs/OPERATIONS.md.
Exit codes of `daily`: 0 SUCCEEDED, 2 PARTIAL, 1 FAILED, 3 another ingestion job is already running.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import select

from app.core.clock import utc_now
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.db.enums import InstrumentType, JobType
from app.db.models import AuditEvent, Candle, DataIngestionJob, DataProvider, User
from app.db.session import make_engine, make_sessionmaker

TIMEFRAME = "5m"


# ---- daily ingestion (B6) --------------------------------------------------------------------------------------


def run_daily(factory, registry, settings: Settings, clock=utc_now, coverage_days: int = 7) -> tuple[int, dict]:
    """Seed check, one incremental ingestion job over every enabled equity, then a coverage and freshness report.

    Safe to re-run at any time: archiving is idempotent; a crashed earlier job is marked INTERRUPTED by Phase 2's
    heartbeat rule; already-archived ranges are not re-fetched beyond the restatement overlap. A successful run does
    NOT mean coverage is complete: missing intervals and unexplained absences are reported, never hidden."""
    from app.marketdata.jobs import JobAlreadyRunning, create_job, resolve_targets, run_job
    from app.marketdata.seed import seed_registry
    from app.marketdata.service import MarketDataService

    report: dict = {"started_at": clock().isoformat(), "provider": settings.marketdata_provider, "timeframe": TIMEFRAME}
    with factory() as s:
        report["seed"] = seed_registry(s, registry.descriptors())
        svc = MarketDataService(s, registry, settings, clock)
        targets = resolve_targets(svc, settings.marketdata_provider, None)
        try:
            job = create_job(
                s, job_type=JobType.INCREMENTAL, provider_code=settings.marketdata_provider, timeframe=TIMEFRAME,
                parameters={"instrument_ids": [t.id for t in targets], "source": "ops-daily"},
                user_id=None, settings=settings, clock=clock,
            )
        except JobAlreadyRunning as exc:
            report.update(status="SKIPPED_ALREADY_RUNNING", detail=str(exc))
            return 3, report
        job_id = job.id
    run_job(factory, registry, settings, job_id, clock=clock)
    with factory() as s:
        job = s.get(DataIngestionJob, job_id)
        report.update(job_id=str(job_id), status=job.status.value, error=job.error_message,
                      instruments=job.summary.get("instruments", []))
        svc = MarketDataService(s, registry, settings, clock)
        end = svc.calendar.to_local(clock()).date()
        start = end - timedelta(days=coverage_days)
        cov = []
        for inst in svc.list_instruments(instrument_type=InstrumentType.EQUITY, enabled=True):
            c = svc.coverage(instrument_id=inst.id, timeframe=TIMEFRAME, start_day=start, end_day=end, sample_limit=5)
            f = svc.freshness(instrument_id=inst.id, timeframe=TIMEFRAME)
            cov.append({
                "symbol": inst.symbol, "candles": c["candle_count"], "missing_intervals": c["missing_intervals"],
                "days_partial": c["days_partial"], "days_absent": c["days_absent"],
                "unexplained_absences": len(c["unexplained_absences"]),
                "full_coverage_of_requested_range": c["full_coverage_of_requested_range"], "freshness": f["status"],
            })
        report["coverage"] = {"start_day": start.isoformat(), "end_day": end.isoformat(), "instruments": cov}
        report["coverage_complete"] = all(x["full_coverage_of_requested_range"] for x in cov) if cov else False
    report["finished_at"] = clock().isoformat()
    code = {"SUCCEEDED": 0, "PARTIAL": 2}.get(report["status"], 1)
    return code, report


# ---- latency measurement (B7) ---------------------------------------------------------------------------------


def _pct(values: list[float], p: float) -> float:
    s = sorted(values)
    return s[min(len(s) - 1, max(0, round(p / 100 * (len(s) - 1))))]


def measure_latency(factory, provider_code: str, days: int, clock=utc_now) -> dict:
    """Distribution of `observed_at - bar_end` (seconds) over canonical archived bars: how long after a bar ends it
    became available to Agent-X. This measures Agent-X's operational availability (provider latency + finalisation
    margin + ingestion cadence), not Yahoo's publication latency alone; the Phase 2 probe measures the latter."""
    since = clock() - timedelta(days=days)
    with factory() as s:
        rows = s.execute(
            select(Candle.observed_at, Candle.bar_end_ts)
            .join(DataProvider, DataProvider.id == Candle.provider_id)
            .where(DataProvider.code == provider_code, Candle.is_canonical.is_(True), Candle.bar_end_ts >= since)
        ).all()
    lags = [(o - e).total_seconds() for o, e in rows]
    if not lags:
        return {"provider": provider_code, "days": days, "bars": 0, "note": "no archived bars in the window"}
    return {
        "provider": provider_code, "days": days, "bars": len(lags),
        "lag_seconds": {
            "min": min(lags), "p50": _pct(lags, 50), "p90": _pct(lags, 90), "p99": _pct(lags, 99), "max": max(lags),
            "mean": round(statistics.fmean(lags), 1),
        },
        "note": "observed_at - bar_end; see docs/OPERATIONS.md for how to choose EXECUTION_MAX_PRICE_AGE_SECONDS",
    }


# ---- operators ---------------------------------------------------------------------------------------------


def set_operator(factory, email: str, on: bool) -> dict:
    """Grant or revoke operator rights. Server-side only (never exposed through the API); audited."""
    with factory() as s:
        user = s.scalar(select(User).where(User.email == email.strip().lower()))
        if user is None:
            raise SystemExit(f"no user with email {email!r}")
        before = user.is_operator
        user.is_operator = on
        s.add(AuditEvent(user_id=user.id, event_type="operator.granted" if on else "operator.revoked",
                         details={"before": before, "after": on, "via": "ops-cli"}))
        s.commit()
        return {"email": user.email, "is_operator": on, "changed": before != on}


# ---- entry point --------------------------------------------------------------------------------------------


def _ctx(settings: Settings | None = None):
    from app.marketdata.providers.registry import build_default_registry

    settings = settings or get_settings()
    engine = make_engine(settings.database_url, settings.db_connect_timeout_seconds)
    return settings, make_sessionmaker(engine), build_default_registry()


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    ap = argparse.ArgumentParser(prog="python -m app.ops.cli")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("daily", help="one-shot incremental ingestion + coverage report (for an external scheduler)")
    d.add_argument("--coverage-days", type=int, default=7)
    d.add_argument("--report-dir", type=Path, default=None)
    lat = sub.add_parser("latency", help="availability latency of archived bars")
    lat.add_argument("--days", type=int, default=30)
    op = sub.add_parser("set-operator")
    op.add_argument("--email", required=True)
    g = op.add_mutually_exclusive_group(required=True)
    g.add_argument("--on", action="store_true")
    g.add_argument("--off", action="store_true")
    cn = sub.add_parser("check-contract-note")
    cn.add_argument("note", type=Path)
    cn.add_argument("--schedule", type=Path, default=None)
    st = sub.add_parser("staging-validate")
    st.add_argument("--staging-db", required=True)
    st.add_argument("--schedule", type=Path, required=True)
    st.add_argument("--approval-reference", required=True)
    st.add_argument("--participation", default=None)
    args = ap.parse_args(argv)

    if args.cmd == "daily":
        settings, factory, registry = _ctx()
        code, report = run_daily(factory, registry, settings, coverage_days=args.coverage_days)
        text = json.dumps(report, indent=2, default=str)
        if args.report_dir:
            args.report_dir.mkdir(parents=True, exist_ok=True)
            (args.report_dir / f"daily_{datetime.now().strftime('%Y%m%dT%H%M%S')}.json").write_text(text, encoding="utf-8")
        print(text)
        return code
    if args.cmd == "latency":
        settings, factory, _ = _ctx()
        print(json.dumps(measure_latency(factory, settings.marketdata_provider, args.days), indent=2))
        return 0
    if args.cmd == "set-operator":
        _, factory, _ = _ctx()
        print(json.dumps(set_operator(factory, args.email, args.on)))
        return 0
    if args.cmd == "check-contract-note":
        from app.exchange.contract_notes import evaluate_contract_note
        from app.exchange.costs import load_cost_schedule

        note = json.loads(args.note.read_text(encoding="utf-8"))
        schedule_path = args.schedule or (args.note.parent / note["schedule"])
        report = evaluate_contract_note(load_cost_schedule(schedule_path), note)
        print(json.dumps(report.as_dict(), indent=2))
        return 0 if report.ok else 1
    if args.cmd == "staging-validate":
        from app.ops.staging import staging_main

        return staging_main(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
