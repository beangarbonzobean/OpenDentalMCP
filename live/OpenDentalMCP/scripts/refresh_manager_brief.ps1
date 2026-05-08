# Daily manager-brief refresh.
#
# Runs at a scheduled time (e.g. 06:30) so the brief is fresh when the user
# logs in for the day. Posts to the dashboard's manager refresh endpoint,
# which dispatches Opus via the inference router and stores the brief.
#
# Cost per run: ~$0 against pre-paid Max quota (no API spend) per Opus.
# Latency: 30-90s typical.
#
# Logs are appended to:
#   live/OpenDentalMCP/logs/manager_brief_refresh.log
#
# Schedule via Task Scheduler — see scripts/install_manager_brief_cron.ps1

$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'

$Root = 'C:\Users\Administrator\Desktop\Cursor\OpenDentalMCP\live\OpenDentalMCP'
$Url  = $env:UTILIZATION_DASHBOARD_REFRESH_URL
if (-not $Url) {
    $Url = 'http://127.0.0.1:9766/utilization/api/manager/refresh'
}
$LogDir  = Join-Path $Root 'logs'
$LogFile = Join-Path $LogDir 'manager_brief_refresh.log'

if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir | Out-Null }

$ts = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
$start = Get-Date

try {
    $resp = Invoke-RestMethod -Method POST -Uri $Url -TimeoutSec 480
    $elapsed = [int]((Get-Date) - $start).TotalSeconds
    $line = "[$ts] OK in ${elapsed}s — model=$($resp.model) projects=$($resp.projects_in_bundle.Count) chars=$($resp.bundle_chars)"
    Add-Content -Path $LogFile -Value $line
    Write-Output $line
}
catch {
    $elapsed = [int]((Get-Date) - $start).TotalSeconds
    $msg = $_.Exception.Message
    $line = "[$ts] FAIL after ${elapsed}s — $msg"
    Add-Content -Path $LogFile -Value $line
    Write-Output $line
    exit 1
}
