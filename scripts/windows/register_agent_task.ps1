<#
.SYNOPSIS
  Registers (or replaces) the Windows Task Scheduler task "AgentX-Agent-Cycle": every 5 minutes, Monday to Friday,
  09:17-15:45 local time (the machine must be on India Standard Time), running agent_cycle.ps1 two minutes after
  each "AgentX-Intraday-Ingestion" run.

.NOTES
  Registering this task is an owner decision; it is not done by any setup step. Runs as the current user, only while
  logged on (no stored password). Remove with:
    Unregister-ScheduledTask -TaskName "AgentX-Agent-Cycle" -Confirm:$false
  The cycle script itself refuses to run outside session hours.
#>
$ErrorActionPreference = "Stop"
$name = "AgentX-Agent-Cycle"
if ((Get-TimeZone).Id -ne "India Standard Time") {
    throw "The machine clock is not on India Standard Time; adjust the trigger times before registering."
}
$script = Join-Path $PSScriptRoot "agent_cycle.ps1"
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$script`"" `
    -WorkingDirectory (Split-Path $script)
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At "09:17"
# 09:17 + 6h28m = 15:45: the last runs book the fills of the 15:25-15:30 bar once ingestion has archived it.
$trigger.Repetition = (New-ScheduledTaskTrigger -Once -At "09:17" `
    -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration (New-TimeSpan -Hours 6 -Minutes 28)).Repetition
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 4) `
    -StartWhenAvailable:$false -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries
Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings `
    -Description "Agent-X: one agent cycle every 5 minutes during the NSE session (paper trading only)." `
    -Force | Out-Null
Get-ScheduledTask -TaskName $name | Get-ScheduledTaskInfo | Select-Object TaskName, NextRunTime
