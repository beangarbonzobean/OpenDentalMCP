# Wrapper for the intake auto-filing processor.
#
# Invoked by Windows Task Scheduler every 10 minutes (or whatever cadence
# fits the practice's scan workflow).
#
# Reads from the watch folder defined by INTAKE_WATCH_FOLDER (or
# --watch arg), classifies and matches each new PDF, and either auto-files
# high-confidence matches into OD or queues them for staff review in the
# review UI.
#
# To inspect the latest run:
#   Get-Content "C:\Users\Administrator\Desktop\Cursor\OpenDentalMCP\live\OpenDentalMCP\logs\intake_processor.log" -Tail 100

$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'

$Root = 'C:\Users\Administrator\Desktop\Cursor\OpenDentalMCP\live\OpenDentalMCP'
Set-Location $Root

# Pull ANTHROPIC_API_KEY from Machine env (extractor + classifier need it).
$apiKey = [Environment]::GetEnvironmentVariable('ANTHROPIC_API_KEY', 'Machine')

$env:MCP_CONFIG_FILE              = 'config.prod.json'
# Watch folder: scanner dumps end-of-day batch PDFs here.
if (-not $env:INTAKE_WATCH_FOLDER)        { $env:INTAKE_WATCH_FOLDER        = '\\SERVER12\ShareFolder\Scans' }
# SHADOW MODE: threshold > 1.0 means nothing ever auto-files. Every candidate
# is queued for review so we can compare our suggestions against what front
# desk actually filed during the day. Lower this (e.g. to 0.95) once we've
# seen the AI agree with staff for several days running.
if (-not $env:INTAKE_AUTO_FILE_THRESHOLD) { $env:INTAKE_AUTO_FILE_THRESHOLD = '1.5' }

# Use the local VLM for the OCR step (free) and Haiku for the structured
# extraction + classification (more accurate for JSON output).
$env:OCR_BACKEND                  = 'local'
$env:LOCAL_VLM_BASE_URL           = 'http://192.168.127.78:11434'
$env:LOCAL_VLM_PRIMARY            = 'glm-ocr:q8_0'
$env:LOCAL_VLM_FALLBACK           = 'qwen3.5:9b'
$env:LOCAL_VLM_DPI                = '150'
$env:LOCAL_VLM_HAIKU_PAGE_FALLBACK = 'true'
if ($apiKey) { $env:ANTHROPIC_API_KEY = $apiKey }

if (-not (Test-Path $env:INTAKE_WATCH_FOLDER)) {
    New-Item -ItemType Directory -Force -Path $env:INTAKE_WATCH_FOLDER | Out-Null
}

$timestamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
Write-Output "[$timestamp] Starting intake processor: watch=$env:INTAKE_WATCH_FOLDER threshold=$env:INTAKE_AUTO_FILE_THRESHOLD"
$start = Get-Date

& "$Root\.venv\Scripts\python.exe" "$Root\scripts\intake_processor.py" `
    --watch=$env:INTAKE_WATCH_FOLDER `
    --auto-file-threshold=$env:INTAKE_AUTO_FILE_THRESHOLD `
    --log-level=INFO

$elapsed = (Get-Date) - $start
$timestamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
Write-Output "[$timestamp] Intake processor done in $($elapsed.TotalSeconds.ToString('F1'))s"
