# Agent-X — Backup, Restore and Migration Safety (Phase 8)

Tooling: `python -m app.ops.cli backup | verify-backup | restore-check | prune-backups` (`app/ops/backup.py`). Run from
`backend/` (or in the container: `docker compose -f docker-compose.prod.yml run --rm backup`). Nothing runs on a
schedule unless an operator registers one.

## 1. Backup

```powershell
..\.venv\Scripts\python.exe -m app.ops.cli backup --label pre-0014        # default dir: <repo>/data/backups (git-ignored)
```

- **Format:** `pg_dump -Fc` of the whole `DB_NAME` database: schema, triggers, functions and data.
- **Name:** `agentx_<database>_<UTC yyyymmddThhmmssZ>[_<label>].dump`, with a manifest beside it:
  `agentx_<database>_<stamp>[_<label>].manifest.json`.
- **Manifest:**
  - file size and **SHA-256**;
  - the `pg_restore --list` counts (TOC entries and tables with data);
  - the **alembic revision**;
  - for 31 tables, the row count and an **md5 of every row** ordered by id. The tables cover users and accounts,
    audit events, orders, fills, positions, the cash ledger, risk records, instruments and candles, corporate actions,
    ingestion, backtests, agent records, journal entries and explanations.
- **Refusals:** a dump with no `alembic_version` data is refused.
- Credentials reach `pg_dump` only through the child process's `PGPASSWORD` and are never printed or written down.
- On a busy database the snapshot is taken just before `pg_dump` and can differ slightly; verify on a quiet database (no
  ingestion or agent cycle running) when you need exact equality.

Tools: `pg_dump` and `pg_restore` must match the server's major version (18). The CLI uses `PG_BIN_DIR`, then `PATH`,
then `C:\Program Files\PostgreSQL\18\bin`. The API image includes the PostgreSQL client.

## 2. Verify

```powershell
..\.venv\Scripts\python.exe -m app.ops.cli verify-backup ..\data\backups\agentx_agentx_20261112T070000Z_pre-0014.dump
```

Recomputes the SHA-256 and re-reads the table of contents. Exit 0 = intact; exit 1 = the file changed, is corrupt or its
TOC differs. Verify before every restore and after copying a backup off the machine.

## 3. Isolated restore check (never over a real database)

1. Once, as the PostgreSQL administrator: `psql -h 127.0.0.1 -U postgres -d postgres -f scripts/recovery/provision_restore_check.sql`.
   This creates an empty, disposable `agentx_restore_check` owned by `agentx_app`; the app role gets no CREATEDB.
2. Run the check:

   ```powershell
   ..\.venv\Scripts\python.exe -m app.ops.cli restore-check <backup.dump> --target agentx_restore_check
   ```

   It refuses any target not named `*_restore_check`, and refuses `DB_NAME`, `DB_TEST_NAME` and anything containing
   `staging`. It verifies the backup, then runs `pg_restore --clean --if-exists --no-owner --single-transaction
   --exit-on-error` into the target. Finally it compares the alembic revision and every table's row count and row md5
   with the manifest. Exit 0 = identical.
3. Then: `DROP DATABASE agentx_restore_check;` (as the administrator), or keep it for the next check (`--clean` reuses
   it).

## 4. Real restore (disaster recovery; owner decision)

Restoring over a real database discards everything written after the backup.
1. Stop the API and the agent worker.
2. Take a backup of the current state anyway.
3. Restore into an empty database: `pg_restore --clean --if-exists --no-owner -d <db> <dump>`.
4. Run the restore check against the manifest, then `alembic current` and `/health/ready`.

Never test this procedure on development or staging; use §3.

## 5. Retention

```powershell
..\.venv\Scripts\python.exe -m app.ops.cli prune-backups --keep-last 14 --keep-days 30           # dry run: lists what it would remove
..\.venv\Scripts\python.exe -m app.ops.cli prune-backups --keep-last 14 --keep-days 30 --apply
```

Policy (recommended defaults):
- Keep the newest 14 backups and every backup younger than 30 days.
- Keep every pre-migration backup until the next migration has been verified.
- Copy at least one verified backup per week off the machine.

Backups contain personal data (emails, password hashes) and trading records: store them encrypted at rest with
access restricted to operators.

## 6. Migration safety

| Safeguard | How |
|---|---|
| Right database | every command-line alembic run prints its target. With `ENVIRONMENT=staging` or `production` it refuses unless `AGENTX_MIGRATION_TARGET=<that database>` (tested) |
| Backup first | the documented order is `backup` → `verify-backup` → migrate → compare; see the evidence files for 0011–0013 |
| Single head | `alembic heads` must show one head; CI checks it |
| Drift | `alembic check` (CI), and `test_migrations_round_trip_and_match_the_models`: 0001 → head → base → head with no model difference |
| Data-preserving downgrades | 0003, 0009, 0012 and 0013 refuse to downgrade while their records exist. 0014 drops only a session column and an index |
| Readiness | `/health/ready` is 503 until the schema is at the code's head: new code is never served on an old schema |
| No auto-migration | the API never migrates on start-up. `migrate` is a separate one-shot container |

Upgrade path for the development database, an owner action (it is at `0013`; this code needs `0014`):
```powershell
cd backend
..\.venv\Scripts\python.exe -m app.ops.cli backup --label pre-0014
..\.venv\Scripts\python.exe -m app.ops.cli verify-backup ..\data\backups\<file>.dump
..\.venv\Scripts\python.exe -m alembic upgrade 0014
..\.venv\Scripts\python.exe -m alembic check
```
Until then, authenticated requests from this code fail against the `0013` database (the `users.sessions_revoked_at`
column is missing), and `/health/ready` reports `unavailable`.
