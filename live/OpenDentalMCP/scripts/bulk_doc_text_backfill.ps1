# Bulk OCR backfill for the OD document-text cache.
#
# This is the long-running counterpart to nightly_doc_text_backfill.ps1.
# Run this manually when you want to chew through the historical backlog as
# fast as possible, e.g., kicked off Friday evening to run through the weekend.
#
# Differences from the nightly wrapper:
#   - No per-run document cap (max-docs=999999)
#   - Higher Haiku page-fallback budget ($30 vs $5)
#   - Logs to a separate bulk log file so you can distinguish bulk runs from
#     the nightly cadence
#   - Uses the SAME lock file as the nightly task. If a nightly run is still
#     in flight when you start a bulk, the bulk will exit cleanly with
#     halted_reason='locked' — no overlap risk.
#
# Read-only against OD's database and the OD image share. Writes only to
# live/OpenDentalMCP/data/ (cache + lock) and live/OpenDentalMCP/logs/.
#
# Typical use:
#   PowerShell> & 'C:\Users\Administrator\Desktop\Cursor\OpenDentalMCP\live\OpenDentalMCP\scripts\bulk_doc_text_backfill.ps1'
#   ...wait several hours / overnight / weekend...
#
# Or via Task Scheduler in the foreground (recommended) so it survives logoff:
#   Register-ScheduledTask -TaskName 'OpenDentalMCP_BulkBackfill' \
#       -Action (New-ScheduledTaskAction -Execute powershell.exe \
#                  -Argument "-NoProfile -ExecutionPolicy Bypass -File '<path-to-this-script>'") \
#       -Principal (New-ScheduledTaskPrincipal -UserId SYSTEM -RunLevel Highest)
#   Start-ScheduledTask -TaskName 'OpenDentalMCP_BulkBackfill'
#
# Tail the log to monitor:
#   Get-Content "C:\Users\Administrator\Desktop\Cursor\OpenDentalMCP\live\OpenDentalMCP\logs\document_text_rebuild.log" -Wait

$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'

$Root = 'C:\Users\Administrator\Desktop\Cursor\OpenDentalMCP\live\OpenDentalMCP'
Set-Location $Root

$apiKey = [Environment]::GetEnvironmentVariable('ANTHROPIC_API_KEY', 'Machine')

$env:MCP_CONFIG_FILE                = 'config.prod.json'
$env:DOC_TEXT_SKIP_CATEGORIES       = '179,180,181,463,467'
$env:OCR_BACKEND                    = 'local'
$env:LOCAL_VLM_BASE_URL             = 'http://192.168.127.78:11434'
$env:LOCAL_VLM_PRIMARY              = 'glm-ocr:q8_0'
$env:LOCAL_VLM_FALLBACK             = 'qwen3.5:9b'
$env:LOCAL_VLM_DPI                  = '150'
$env:LOCAL_VLM_HAIKU_PAGE_FALLBACK  = 'true'
if ($apiKey) { $env:ANTHROPIC_API_KEY = $apiKey }

# Effectively no doc cap; the underlying iter_documents will simply run out
# of new docs eventually. The Haiku-fallback budget caps cost ceiling.
$maxDocs    = 999999
$maxSpend   = 30.00   # bulk run can rescue ~3000 problem pages via Haiku before halting

$timestamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
Write-Output "[$timestamp] Starting BULK backfill: max_docs=$maxDocs max_spend=`$$maxSpend"
Write-Output "[$timestamp] Bulk runs share the lock with the nightly task. Safe to interrupt with Ctrl+C — the cache is committed per-doc."
$start = Get-Date

& "$Root\.venv\Scripts\python.exe" "$Root\scripts\rebuild_document_text_index.py" `
    --max-docs=$maxDocs `
    --max-spend=$maxSpend `
    --log-level=INFO

$elapsed = (Get-Date) - $start
$timestamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
Write-Output "[$timestamp] Bulk backfill done in $($elapsed.TotalHours.ToString('F2')) hours"
