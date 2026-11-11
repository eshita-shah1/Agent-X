<#
.SYNOPSIS
  One intraday ingestion run for Agent-X (B6/B7): Yahoo Finance, every enabled equity, finalised bars only.

.DESCRIPTION
  Meant to be started every 5 minutes by Windows Task Scheduler during the NSE session (see
  register_ingestion_task.ps1). Agent-X never schedules itself (Phase 2 decision 5); this script only wraps the
  existing operator CLI:  python -m app.ops.cli daily --coverage-days 1 --report-dir <repo>\data\ingestion_reports

  * Exits immediately (code 0, logged as SKIPPED) outside 09:15-15:40 IST on weekdays, so a misconfigured trigger
    cannot ingest at night. Holidays are not known authoritatively: on a closed day Yahoo simply returns no new bars.
  * Uses the repository's .env (DB_NAME etc.). It never sets EXECUTION_* variables.
  * Never creates, rewrites or back-dates candles: the CLI archives what Yahoo returns, with observed_at = now.
  * Appends one line per run to <repo>\data\ingestion.log; each run's JSON report goes to data\ingestion_reports.
  * Exit code is the CLI's: 0 SUCCEEDED, 2 PARTIAL, 1 FAILED, 3 another ingestion job is running.
#>
param(
    [switch]$IgnoreMarketHours  # for a manual test run only
)
$ErrorActionPreference = "Stop"
$repo = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$backend = Join-Path $repo "backend"
$python = Join-Path $repo ".venv\Scripts\python.exe"
$dataDir = Join-Path $repo "data"
$log = Join-Path $dataDir "ingestion.log"
New-Item -ItemType Directory -Force -Path (Join-Path $dataDir "ingestion_reports") | Out-Null

$ist = [System.TimeZoneInfo]::FindSystemTimeZoneById("India Standard Time")
$now = [System.TimeZoneInfo]::ConvertTimeFromUtc([DateTime]::UtcNow, $ist)
$stamp = $now.ToString("yyyy-MM-dd HH:mm:ss") + " IST"
$minutes = $now.Hour * 60 + $now.Minute
$weekday = $now.DayOfWeek -ne [DayOfWeek]::Saturday -and $now.DayOfWeek -ne [DayOfWeek]::Sunday
$inSession = $weekday -and $minutes -ge (9 * 60 + 15) -and $minutes -le (15 * 60 + 40)

if (-not $inSession -and -not $IgnoreMarketHours) {
    Add-Content -Path $log -Value "$stamp SKIPPED outside NSE session hours" -Encoding utf8
    exit 0
}

Push-Location $backend
try {
    $out = & $python -m app.ops.cli daily --coverage-days 1 --report-dir (Join-Path $dataDir "ingestion_reports") 2>&1
    $code = $LASTEXITCODE
} finally {
    Pop-Location
}
$status = switch ($code) { 0 { "SUCCEEDED" } 2 { "PARTIAL" } 1 { "FAILED" } 3 { "OVERLAP_REFUSED" } default { "EXIT_$code" } }
Add-Content -Path $log -Value "$stamp $status exit=$code" -Encoding utf8
if ($code -ne 0) {
    # keep the tail of the output for diagnosis (no secrets are printed by the CLI)
    ($out | Select-Object -Last 20) | ForEach-Object { Add-Content -Path $log -Value "    $_" -Encoding utf8 }
}
exit $code
