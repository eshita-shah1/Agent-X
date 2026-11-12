"""PostgreSQL backup, verification, isolated restore check and retention (Phase 8). Operator tooling; never automatic.

  backup         pg_dump -Fc of DB_NAME to <dir>/agentx_<db>_<UTC stamp>[_<label>].dump plus a JSON manifest: size,
                 SHA-256, pg_restore --list TOC counts, the alembic revision, and row counts and an md5 of every row
                 (ordered by id) of the tables that matter (users, accounts, orders, fills, positions, ledger, risk,
                 market data, agent and journal tables).
  verify         the file's SHA-256 equals the manifest's and pg_restore can read its table of contents.
  restore-check  restores a verified backup into a DISPOSABLE database whose name must end in `_restore_check` (and is
                 none of DB_NAME, DB_TEST_NAME or a staging database), then compares the alembic revision, row counts and
                 row md5s with the manifest. The source database is never written. The target is provisioned by an
                 administrator (scripts/recovery/provision_restore_check.sql); the application role cannot create it.
  prune          retention: keeps the newest N backups and any younger than D days; dry run unless --apply.

Credentials come from settings and are passed to the PostgreSQL tools through the PGPASSWORD environment variable of the
child process only; they are never printed, logged or written to a manifest.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import create_engine, text

from app.core.config import Settings

RESTORE_SUFFIX = "_restore_check"
SNAPSHOT_TABLES = (
    "users", "virtual_accounts", "audit_events", "orders", "fills", "positions", "cash_ledger", "risk_settings",
    "risk_state", "risk_decisions", "protective_exit_requirements", "corporate_action_adjustments", "dividend_credits",
    "instruments", "candles", "adjusted_candles", "corporate_actions", "data_providers", "data_ingestion_jobs",
    "data_ingestion_batches", "backtest_runs", "agent_instances", "agent_config_versions", "agent_cycles",
    "agent_decisions", "agent_events", "agent_instrument_states", "journal_entries", "journal_tags",
    "journal_reflections", "trade_explanations",
)
WINDOWS_PG_BIN = Path(r"C:\Program Files\PostgreSQL\18\bin")


class BackupError(RuntimeError):
    pass


def pg_tool(name: str) -> str:
    """pg_dump / pg_restore: PG_BIN_DIR, then PATH, then the default Windows install location."""
    exe = name + (".exe" if os.name == "nt" else "")
    configured = os.environ.get("PG_BIN_DIR")
    for candidate in ([Path(configured) / exe] if configured else []) + [shutil.which(name)] + [WINDOWS_PG_BIN / exe]:
        if candidate and Path(candidate).exists():
            return str(candidate)
    raise BackupError(f"{name} not found: set PG_BIN_DIR or put the PostgreSQL client tools on PATH")


def _env(settings: Settings) -> dict:
    env = dict(os.environ)
    env["PGPASSWORD"] = settings.db_password.get_secret_value()
    return env


def _conn_args(settings: Settings, database: str) -> list[str]:
    return ["-h", settings.db_host, "-p", str(settings.db_port), "-U", settings.db_user, "-d", database]


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def snapshot(url) -> dict:
    """Alembic revision plus row count and md5 of every row (ordered by id/primary key) per table. Read-only."""
    engine = create_engine(url)
    out: dict = {"tables": {}}
    try:
        with engine.connect() as c:
            out["alembic_version"] = c.execute(text("SELECT version_num FROM alembic_version")).scalar()
            for t in SNAPSHOT_TABLES:
                if not c.execute(text("SELECT to_regclass(:t)"), {"t": t}).scalar():
                    continue
                n = c.execute(text(f"SELECT count(*) FROM {t}")).scalar()
                md5 = c.execute(text(f"SELECT md5(coalesce(string_agg(x::text, '|' ORDER BY x.id), '')) FROM {t} x")).scalar()
                out["tables"][t] = {"rows": n, "md5": md5}
    finally:
        engine.dispose()
    return out


def toc(path: Path) -> dict:
    r = subprocess.run([pg_tool("pg_restore"), "--list", str(path)], capture_output=True, text=True)
    if r.returncode != 0:
        raise BackupError(f"pg_restore --list failed: {r.stderr.strip()[:500]}")
    entries = [line for line in r.stdout.splitlines() if line and not line.startswith(";")]
    data = sorted(line.split(" TABLE DATA public ")[1].split()[0] for line in entries if " TABLE DATA public " in line)
    return {"toc_entries": len(entries), "table_data_entries": len(data), "tables_with_data": data}


def manifest_path(dump: Path) -> Path:
    return dump.with_suffix(".manifest.json")


def create_backup(settings: Settings, out_dir: Path, label: str = "", now: datetime | None = None) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    safe_label = "".join(ch for ch in label if ch.isalnum() or ch in "-_")[:40]
    dump = out_dir / f"agentx_{settings.db_name}_{stamp}{'_' + safe_label if safe_label else ''}.dump"
    before = snapshot(settings.database_url)
    r = subprocess.run([pg_tool("pg_dump"), *_conn_args(settings, settings.db_name), "-Fc", "-f", str(dump)],
                       env=_env(settings), capture_output=True, text=True)
    if r.returncode != 0:
        dump.unlink(missing_ok=True)
        raise BackupError(f"pg_dump failed: {r.stderr.strip()[:500]}")
    contents = toc(dump)
    if "alembic_version" not in contents["tables_with_data"]:
        raise BackupError("the dump has no alembic_version data: not a complete Agent-X backup")
    manifest = {
        "file": dump.name, "database": settings.db_name, "created_at": stamp, "bytes": dump.stat().st_size,
        "sha256": sha256_file(dump), "format": "pg_dump custom (-Fc), full database", **contents,
        "alembic_version": before["alembic_version"], "snapshot": before["tables"],
        "snapshot_note": "taken immediately before pg_dump; a busy database can change in between (verify on a quiet one)",
    }
    manifest_path(dump).write_text(json.dumps(manifest, indent=1))
    return manifest


def verify_backup(dump: Path) -> dict:
    mpath = manifest_path(dump)
    if not dump.exists() or not mpath.exists():
        raise BackupError(f"missing backup or manifest for {dump.name}")
    manifest = json.loads(mpath.read_text())
    actual = sha256_file(dump)
    contents = toc(dump)
    problems = []
    if actual != manifest["sha256"]:
        problems.append("SHA-256 does not match the manifest (the file changed or is corrupt)")
    if contents["table_data_entries"] != manifest["table_data_entries"]:
        problems.append("the table of contents differs from the manifest")
    return {"file": dump.name, "sha256": actual, "sha256_matches": actual == manifest["sha256"], **contents,
            "ok": not problems, "problems": problems}


def restore_target_problem(settings: Settings, target: str) -> str | None:
    if not target.endswith(RESTORE_SUFFIX):
        return f"refusing: the restore target must be a disposable database named *{RESTORE_SUFFIX}"
    if target in (settings.db_name, settings.db_test_name) or "staging" in target:
        return "refusing: the restore target must not be the main, test or a staging database"
    return None


@dataclass
class RestoreReport:
    target: str
    alembic_version: str | None
    expected_alembic_version: str | None
    tables_compared: int
    mismatches: dict

    @property
    def ok(self) -> bool:
        return not self.mismatches and self.alembic_version == self.expected_alembic_version


def restore_check(settings: Settings, dump: Path, target: str) -> RestoreReport:
    problem = restore_target_problem(settings, target)
    if problem:
        raise BackupError(problem)
    verified = verify_backup(dump)
    if not verified["ok"]:
        raise BackupError("backup verification failed: " + "; ".join(verified["problems"]))
    manifest = json.loads(manifest_path(dump).read_text())
    r = subprocess.run([pg_tool("pg_restore"), *_conn_args(settings, target), "--clean", "--if-exists", "--no-owner",
                        "--no-privileges", "--single-transaction", "--exit-on-error", str(dump)],
                       env=_env(settings), capture_output=True, text=True)
    if r.returncode != 0:
        raise BackupError(f"pg_restore into {target} failed: {r.stderr.strip()[:500]}")
    restored = snapshot(settings._url(target))
    mismatches = {t: {"backup": v, "restored": restored["tables"].get(t)}
                  for t, v in manifest["snapshot"].items() if restored["tables"].get(t) != v}
    return RestoreReport(target, restored["alembic_version"], manifest["alembic_version"], len(manifest["snapshot"]),
                         mismatches)


def prune(out_dir: Path, keep_last: int, keep_days: int, apply: bool = False, now: datetime | None = None) -> dict:
    """Retention: keep the newest `keep_last` backups and every backup younger than `keep_days`."""
    if keep_last < 1:
        raise BackupError("keep_last must be at least 1")
    now = now or datetime.now(UTC)
    dumps = sorted(out_dir.glob("agentx_*.dump"), key=lambda p: p.stat().st_mtime, reverse=True)
    cutoff = now - timedelta(days=keep_days)
    remove = [p for i, p in enumerate(dumps) if i >= keep_last
              and datetime.fromtimestamp(p.stat().st_mtime, UTC) < cutoff]
    if apply:
        for p in remove:
            p.unlink()
            manifest_path(p).unlink(missing_ok=True)
    return {"kept": [p.name for p in dumps if p not in remove], "removed" if apply else "would_remove": [p.name for p in remove]}
