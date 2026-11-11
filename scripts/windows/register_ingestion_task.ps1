<#
.SYNOPSIS
  Registers (or replaces) the Windows Task Scheduler task "AgentX-Intraday-Ingestion": every 5 minutes, Monday to
  Friday, 09:15-15:40 local time (the machine must be on India Standard Time), running ingest_intraday.ps1.

.NOTES
  Runs as the current user, only while logged on (no stored password). Remove with:
    Unregister-ScheduledTask -TaskName "AgentX-Intraday-Ingestion" -Confirm:$false
  The ingestion script itself refuses to run outside session hours, so a wrong trigger cannot ingest at night.
#>
$ErrorActionPreference = "Stop"
$name = "AgentX-Intraday-Ingestion"
if ((Get-TimeZone).Id -ne "India Standard Time") {
    throw "The machine clock is not on India Standard Time; adjust the trigger times before registering."
}
$script = Join-Path $PSScriptRoot "ingest_intraday.ps1"
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$script`"" `
    -WorkingDirectory (Split-Path $script)
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At "09:15"
# 09:15 + 6h25m = 15:40: the last runs pick up the 15:25-15:30 bar once it is finalised.
$trigger.Repetition = (New-ScheduledTaskTrigger -Once -At "09:15" `
    -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration (New-TimeSpan -Hours 6 -Minutes 25)).Repetition
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 4) `
    -StartWhenAvailable:$false -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries
Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings `
    -Description "Agent-X: Yahoo Finance intraday ingestion every 5 minutes during the NSE session (paper trading only)." `
    -Force | Out-Null
Get-ScheduledTask -TaskName $name | Get-ScheduledTaskInfo | Select-Object TaskName, NextRunTime
