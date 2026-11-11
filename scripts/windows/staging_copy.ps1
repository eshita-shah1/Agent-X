<#
.SYNOPSIS
  Copies the market-data archive from agentx into the provisioned agentx_staging database and verifies it.

.DESCRIPTION
  Prerequisite: an administrator ran scripts\staging\provision_staging.sql (role agentx_staging_app owning
  agentx_staging). Set the staging password for this PowerShell session only:
      $env:AGENTX_STAGING_DB_PASSWORD = Read-Host -AsSecureString ... (or plain Read-Host)
  Steps (stops on the first failure):
    1. verify the staging role has no elevated attributes and the database is agentx_staging and empty;
    2. alembic upgrade head on agentx_staging, as agentx_staging_app (process-only environment overrides);
    3. pg_dump --data-only of the archive tables only from agentx (read-only; no users, accounts, orders, fills,
       backtests or audit events are copied);
    4. pg_restore into agentx_staging in a single transaction;
    5. compare row counts and an MD5 of every copied table's rows between source and copy;
    6. write the evidence to docs\evidence\<date>_staging_copy.json (no credentials).
  agentx is only read; agentx_test is never touched.
#>
param(
    [switch]$Refresh  # reload the archive into an already-migrated, otherwise empty staging copy (e.g. after a session)
)
# Windows PowerShell 5.1 turns a native tool's stderr (Alembic logs, pg_restore/psql notices) into a terminating error
# under "Stop". Native results are checked explicitly through $LASTEXITCODE below instead.
$ErrorActionPreference = "Continue"
$repo = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$backend = Join-Path $repo "backend"
$python = Join-Path $repo ".venv\Scripts\python.exe"
$pgBin = "C:\Program Files\PostgreSQL\18\bin"
$stagingDb = "agentx_staging"; $stagingRole = "agentx_staging_app"
$tables = @("data_providers", "instruments", "instrument_provider_symbols", "provider_capabilities",
    "provider_health_state", "data_ingestion_jobs", "data_ingestion_batches", "data_quality_issues",
    "data_restatement_events", "exchange_calendar_days", "candles", "adjusted_candles", "corporate_actions")

if (-not $env:AGENTX_STAGING_DB_PASSWORD) {
    # Fallback: the git-ignored .env.staging (one line: AGENTX_STAGING_DB_PASSWORD=...). Never committed.
    $stagingEnv = Join-Path $repo ".env.staging"
    if (Test-Path $stagingEnv) {
        $line = Get-Content $stagingEnv | Where-Object { $_ -match "^AGENTX_STAGING_DB_PASSWORD=" } | Select-Object -First 1
        if ($line) { $env:AGENTX_STAGING_DB_PASSWORD = $line -replace "^AGENTX_STAGING_DB_PASSWORD=", "" }
    }
}
if (-not $env:AGENTX_STAGING_DB_PASSWORD) { throw "Set `$env:AGENTX_STAGING_DB_PASSWORD or create .env.staging first." }
$envFile = Get-Content (Join-Path $repo ".env")
function EnvValue($k) { ($envFile | Where-Object { $_ -match "^$k=" } | Select-Object -First 1) -replace "^$k=", "" }
$srcUser = EnvValue "DB_USER"; $srcDb = EnvValue "DB_NAME"; $srcPass = EnvValue "DB_PASSWORD"
$dbHost = EnvValue "DB_HOST"; $port = EnvValue "DB_PORT"
if ($srcDb -ne "agentx") { throw "Source DB_NAME in .env is '$srcDb', expected 'agentx'." }

function Q($db, $user, $pass, $sql) {
    $env:PGPASSWORD = $pass
    try { $r = & "$pgBin\psql.exe" -h $dbHost -p $port -U $user -d $db -v ON_ERROR_STOP=1 -Atc $sql; if ($LASTEXITCODE) { throw "psql failed on $db" }; $r }
    finally { Remove-Item Env:PGPASSWORD -ErrorAction SilentlyContinue }
}
function S($sql) { Q $stagingDb $stagingRole $env:AGENTX_STAGING_DB_PASSWORD $sql }
function Src($sql) { Q $srcDb $srcUser $srcPass $sql }

# 1. role and target checks
$attrs = S "select current_database()||'|'||current_user||'|'||rolsuper||'|'||rolcreatedb||'|'||rolcreaterole from pg_roles where rolname = current_user"
if ($attrs -ne "agentx_staging|agentx_staging_app|false|false|false") { throw "Unexpected staging identity/attributes: $attrs" }
$existing = S "select count(*) from information_schema.tables where table_schema='public'"
if ([int]$existing -ne 0 -and -not $Refresh) { throw "agentx_staging is not empty ($existing tables). Use -Refresh to reload the archive, or tear it down and re-provision." }
if ($Refresh -and [int]$existing -ne 0) {
    # Reload the archive only while staging holds no user or trading record (nothing the validator created is lost).
    $userRows = S "select (select count(*) from users)+(select count(*) from virtual_accounts)+(select count(*) from orders)+(select count(*) from fills)+(select count(*) from positions)+(select count(*) from cash_ledger)+(select count(*) from risk_decisions)+(select count(*) from protective_exit_requirements)+(select count(*) from backtest_runs)+(select count(*) from audit_events)"
    if ([int]$userRows -ne 0) { throw "Refusing -Refresh: staging already holds $userRows user/trading rows (validator records). Tear down and re-provision instead." }
    # TRUNCATE does not fire the archive's row triggers; CASCADE reaches only tables verified empty above.
    S ("TRUNCATE " + ($tables -join ", ") + " RESTART IDENTITY CASCADE") | Out-Null
    Write-Host "Archive tables in agentx_staging truncated for refresh (no user or trading rows existed)."
}
$userJobs = Src "select count(*) from data_ingestion_jobs where requested_by_user_id is not null"
if ([int]$userJobs -ne 0) { throw "$userJobs ingestion job(s) reference a development user; users are not copied. Refusing (no data is altered)." }

# 2. schema at head on the empty copy (process-only overrides; .env untouched)
Push-Location $backend
try {
    $env:DB_NAME = $stagingDb; $env:DB_USER = $stagingRole; $env:DB_PASSWORD = $env:AGENTX_STAGING_DB_PASSWORD
    & $python -m alembic upgrade head; if ($LASTEXITCODE) { throw "alembic upgrade failed" }
    $rev = (& $python -m alembic current 2>$null | Select-Object -Last 1)
} finally {
    Remove-Item Env:DB_NAME, Env:DB_USER, Env:DB_PASSWORD -ErrorAction SilentlyContinue
    Pop-Location
}

# 3. dump the archive tables only (read-only on agentx)
$stamp = Get-Date -Format "yyyyMMddTHHmmss"
$dumpDir = Join-Path $repo "data\staging"; New-Item -ItemType Directory -Force -Path $dumpDir | Out-Null
$dump = Join-Path $dumpDir "agentx_archive_$stamp.dump"
$tArgs = @(); foreach ($t in $tables) { $tArgs += @("-t", $t) }
$env:PGPASSWORD = $srcPass
& "$pgBin\pg_dump.exe" -h $dbHost -p $port -U $srcUser -d $srcDb -Fc --data-only @tArgs -f $dump
$dumpCode = $LASTEXITCODE; Remove-Item Env:PGPASSWORD
if ($dumpCode) { throw "pg_dump failed" }

# 4. restore into the copy in one transaction
$env:PGPASSWORD = $env:AGENTX_STAGING_DB_PASSWORD
& "$pgBin\pg_restore.exe" -h $dbHost -p $port -U $stagingRole -d $stagingDb --data-only --single-transaction --exit-on-error $dump
$restoreCode = $LASTEXITCODE; Remove-Item Env:PGPASSWORD
if ($restoreCode) { throw "pg_restore failed (the single transaction was rolled back)" }

# 5. verify counts and checksums table by table
$result = [ordered]@{}; $allMatch = $true
foreach ($t in $tables) {
    $sql = "select count(*)||'|'||md5(coalesce(string_agg(x::text, E'\n' order by x::text), '')) from $t x"
    $a = Src $sql; $b = S $sql
    $match = $a -eq $b; if (-not $match) { $allMatch = $false }
    $p = $a.Split("|"); $result[$t] = [ordered]@{ rows = [int]$p[0]; md5 = $p[1]; copy = $b; match = $match }
}
$leaks = S "select (select count(*) from users)+(select count(*) from virtual_accounts)+(select count(*) from orders)+(select count(*) from fills)+(select count(*) from backtest_runs)+(select count(*) from audit_events)"

# 6. evidence
$evidence = [ordered]@{
    generated_at = (Get-Date).ToString("o"); source_db = $srcDb; staging_db = $stagingDb; staging_role = $stagingRole
    staging_role_attributes = "rolsuper=false rolcreatedb=false rolcreaterole=false"
    staging_alembic_revision = $rev; dump_file = (Split-Path $dump -Leaf)
    dump_sha256 = (Get-FileHash $dump -Algorithm SHA256).Hash.ToLower()
    non_archive_rows_in_copy = [int]$leaks; all_tables_match = $allMatch; tables = $result
}
$out = Join-Path $repo ("docs\evidence\" + (Get-Date -Format "yyyy-MM-dd") + "_staging_copy.json")
$evidence | ConvertTo-Json -Depth 5 | Set-Content -Path $out -Encoding utf8
Write-Host "Evidence: $out"
if (-not $allMatch -or [int]$leaks -ne 0) { throw "Verification failed: see $out" }
Write-Host "Staging copy verified: every table matches the source; no user or trading rows copied."
