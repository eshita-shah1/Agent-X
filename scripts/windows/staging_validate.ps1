<#
.SYNOPSIS
  Runs the B9 staging validator twice on agentx_staging (without and with --participation 0.1) and saves the reports.

.DESCRIPTION
  Prerequisites: staging_copy.ps1 succeeded after a session of 5-minute ingestion, and
  $env:AGENTX_STAGING_DB_PASSWORD is set for this PowerShell session.
  Exit codes of each run: 0 passed, 1 a check failed, 2 no fresh scenario (order flow NOT exercised: not a pass).
  Reports: docs\evidence\<date>_staging_validate_{base,participation_0.1}.json plus a summary file with both exit codes,
  the schedule version and its SHA-256. No credentials are written. Development (agentx) is never touched.
#>
param(
    [string]$ApprovalReference = "STAGING-ONLY:AGENTX-MOCK-EXCHANGE-VALIDATION-001",
    [string]$Schedule = ""
)
# Windows PowerShell 5.1 turns a native tool's stderr (Alembic logs, pg_restore/psql notices) into a terminating error
# under "Stop". Native results are checked explicitly through $LASTEXITCODE below instead.
$ErrorActionPreference = "Continue"
$repo = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$backend = Join-Path $repo "backend"
$python = Join-Path $repo ".venv\Scripts\python.exe"
if (-not $Schedule) { $Schedule = Join-Path $repo "data\staging\cost_schedule.SYNTHETIC_STAGING_ONLY.json" }
if (-not (Test-Path $Schedule)) { throw "Schedule not found: $Schedule" }
if (-not $env:AGENTX_STAGING_DB_PASSWORD) {
    # Fallback: the git-ignored .env.staging (one line: AGENTX_STAGING_DB_PASSWORD=...). Never committed.
    $stagingEnv = Join-Path $repo ".env.staging"
    if (Test-Path $stagingEnv) {
        $line = Get-Content $stagingEnv | Where-Object { $_ -match "^AGENTX_STAGING_DB_PASSWORD=" } | Select-Object -First 1
        if ($line) { $env:AGENTX_STAGING_DB_PASSWORD = $line -replace "^AGENTX_STAGING_DB_PASSWORD=", "" }
    }
}
if (-not $env:AGENTX_STAGING_DB_PASSWORD) { throw "Set `$env:AGENTX_STAGING_DB_PASSWORD or create .env.staging first." }
$date = Get-Date -Format "yyyy-MM-dd"
$evidence = Join-Path $repo "docs\evidence"

$summary = [ordered]@{
    generated_at = (Get-Date).ToString("o"); staging_db = "agentx_staging"; approval_reference = $ApprovalReference
    schedule_file = (Split-Path $Schedule -Leaf); schedule_sha256 = (Get-FileHash $Schedule -Algorithm SHA256).Hash.ToLower()
    runs = [ordered]@{}
}
Push-Location $backend
try {
    $env:DB_NAME = "agentx_staging"; $env:DB_USER = "agentx_staging_app"; $env:DB_PASSWORD = $env:AGENTX_STAGING_DB_PASSWORD
    $fresh = & $python -m app.ops.cli freshness --min-run 8 2>$null
    $summary.freshness_exit_code = $LASTEXITCODE
    $fresh | Set-Content -Path (Join-Path $evidence "${date}_staging_freshness.json") -Encoding utf8
    foreach ($run in @(@{ name = "base"; extra = @() }, @{ name = "participation_0.1"; extra = @("--participation", "0.1") })) {
        $report = & $python -m app.ops.cli staging-validate --staging-db agentx_staging --schedule $Schedule `
            --approval-reference $ApprovalReference @($run.extra) 2>$null
        $code = $LASTEXITCODE
        $file = Join-Path $evidence ("${date}_staging_validate_" + $run.name + ".json")
        $report | Set-Content -Path $file -Encoding utf8
        $meaning = switch ($code) { 0 { "PASSED" } 1 { "FAILED" } 2 { "NOT_EXERCISED (no fresh scenario; not a pass)" } default { "ERROR" } }
        $summary.runs[$run.name] = [ordered]@{ exit_code = $code; meaning = $meaning; report = (Split-Path $file -Leaf) }
        Write-Host ("{0}: exit {1} = {2}" -f $run.name, $code, $meaning)
    }
    # Scripted API order lifecycle (step 8), only after both validator runs passed.
    if (@($summary.runs.Values | Where-Object { $_.exit_code -ne 0 }).Count -eq 0) {
        $report = & $python -m app.ops.cli staging-lifecycle --staging-db agentx_staging --schedule $Schedule `
            --approval-reference $ApprovalReference 2>$null
        $code = $LASTEXITCODE
        $file = Join-Path $evidence "${date}_staging_lifecycle.json"
        $report | Set-Content -Path $file -Encoding utf8
        $meaning = switch ($code) { 0 { "PASSED" } 1 { "FAILED" } 2 { "NOT_EXERCISED (no fresh scenario; not a pass)" } default { "ERROR" } }
        $summary.runs["lifecycle"] = [ordered]@{ exit_code = $code; meaning = $meaning; report = (Split-Path $file -Leaf) }
        Write-Host ("lifecycle: exit {0} = {1}" -f $code, $meaning)
    } else {
        $summary.runs["lifecycle"] = [ordered]@{ exit_code = $null; meaning = "NOT_RUN (a validator run did not pass)" }
    }
} finally {
    Remove-Item Env:DB_NAME, Env:DB_USER, Env:DB_PASSWORD -ErrorAction SilentlyContinue
    Pop-Location
}
$summary | ConvertTo-Json -Depth 5 | Set-Content -Path (Join-Path $evidence "${date}_staging_validate_summary.json") -Encoding utf8
$failed = @($summary.runs.Values | Where-Object { $_.exit_code -ne 0 }).Count
if ($failed) { exit 1 } else { exit 0 }
