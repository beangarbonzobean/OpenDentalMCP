# Install Task Scheduler entry for the daily manager-brief refresh.
#
# Run once (as Administrator). Creates a task that fires
# refresh_manager_brief.ps1 every morning at 06:30.
#
# To uninstall:
#   Unregister-ScheduledTask -TaskName 'UtilizationDashboard-ManagerBriefRefresh' -Confirm:$false
# To run manually:
#   Start-ScheduledTask  -TaskName 'UtilizationDashboard-ManagerBriefRefresh'
# To inspect:
#   Get-ScheduledTask    -TaskName 'UtilizationDashboard-ManagerBriefRefresh' | Get-ScheduledTaskInfo

$ErrorActionPreference = 'Stop'

$Root = 'C:\Users\Administrator\Desktop\Cursor\OpenDentalMCP\live\OpenDentalMCP'
$Script = Join-Path $Root 'scripts\refresh_manager_brief.ps1'
$TaskName = 'UtilizationDashboard-ManagerBriefRefresh'

if (-not (Test-Path $Script)) {
    throw "Refresh script not found at $Script"
}

# Action: run PowerShell with the script
$action = New-ScheduledTaskAction `
    -Execute 'powershell.exe' `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$Script`""

# Trigger: daily at 06:30 (clinic opens 08:00; brief ready 90 min ahead)
$trigger = New-ScheduledTaskTrigger -Daily -At '06:30'

# Settings: don't run on battery, allow if missed, give 10 min wall-clock
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
    -RestartCount 2 `
    -RestartInterval (New-TimeSpan -Minutes 5)

# Run as the current user (interactive token so the venv is on PATH)
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType S4U

# Register (replace if exists)
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description 'Daily Opus refresh of the utilization dashboard manager brief.'

Write-Output "Installed scheduled task: $TaskName"
Write-Output "  next run: $((Get-ScheduledTaskInfo -TaskName $TaskName).NextRunTime)"
Write-Output ""
Write-Output 'Run it manually right now to verify:'
Write-Output "  Start-ScheduledTask -TaskName '$TaskName'"
