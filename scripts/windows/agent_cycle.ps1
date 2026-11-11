<#
.SYNOPSIS
  One Agent-X agent cycle (Phase 5): every configured agent, once, then exit. Paper trading only.

.DESCRIPTION
  Meant to be started every 5 minutes by Windows Task Scheduler, two minutes after each ingestion run (see
  register_agent_task.ps1). Agent-X never schedules itself (decision D8); this script only wraps the CLI:
      python -m app.agent.cli cycle
  * Exits immediately (code 0, logged as SKIPPED) outside 09:17-15:45 IST on weekdays.
  * Never starts ingestion: fresh bars arrive only through the separately scheduled ingestion task.
  * Uses the repository's .env. It never sets EXECUTION_* variables: with execution disabled (the development default)
    agents run SIGNALS_ONLY and no order is ever submitted.
  * A second copy started while one is running is harmless: each agent is cycled by at most one worker at a time
    (PostgreSQL advisory lock); the other skips it.
  * Appends one line per run to <repo>\data\agent.log. Exit code: 0 all cycles succeeded or were skipped, 1 a cycle failed.
#>
param(
    [switch]$IgnoreMarketHours  # for a manual test run only
)
$ErrorActionPreference = "Stop"
$repo = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$backend = Join-Path $repo "backend"
$python = Join-Path $repo ".venv\Scripts\python.exe"
$dataDir = Join-Path $repo "data"
$log = Join-Path $dataDir "agent.log"
New-Item -ItemType Directory -Force -Path $dataDir | Out-Null

$ist = [System.TimeZoneInfo]::FindSystemTimeZoneById("India Standard Time")
$now = [System.TimeZoneInfo]::ConvertTimeFromUtc([DateTime]::UtcNow, $ist)
$stamp = $now.ToString("yyyy-MM-dd HH:mm:ss") + " IST"
$minutes = $now.Hour * 60 + $now.Minute
$weekday = $now.DayOfWeek -ne [DayOfWeek]::Saturday -and $now.DayOfWeek -ne [DayOfWeek]::Sunday
$inSession = $weekday -and $minutes -ge (9 * 60 + 17) -and $minutes -le (15 * 60 + 45)

if (-not $inSession -and -not $IgnoreMarketHours) {
    Add-Content -Path $log -Value "$stamp SKIPPED outside NSE session hours" -Encoding utf8
    exit 0
}

Push-Location $backend
try {
    $out = & $python -m app.agent.cli cycle 2>&1
    $code = $LASTEXITCODE
} finally {
    Pop-Location
}
$status = switch ($code) { 0 { "SUCCEEDED" } 1 { "CYCLE_FAILED" } default { "EXIT_$code" } }
Add-Content -Path $log -Value "$stamp $status exit=$code" -Encoding utf8
if ($code -ne 0) {
    ($out | Select-Object -Last 30) | ForEach-Object { Add-Content -Path $log -Value "    $_" -Encoding utf8 }
}
exit $code
