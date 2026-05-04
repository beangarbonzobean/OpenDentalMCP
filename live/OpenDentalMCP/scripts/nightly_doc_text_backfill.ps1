# Nightly OCR backfill for the OD document-text cache.
#
# Invoked by Windows Task Scheduler at 21:00 every night (clinic closes 20:00).
# Runs the local-VLM OCR backend (qwen2.5vl -> qwen3.5 fallback -> Haiku page
# fallback) against the next batch of uncached OD documents.
#
# Read-only against OD's database and the OD image share. Writes only to
# live/OpenDentalMCP/data/ (cache + lock) and live/OpenDentalMCP/logs/.
#
# To inspect last run:
#   Get-Content "C:\Users\Administrator\Desktop\Cursor\OpenDentalMCP\live\OpenDentalMCP\logs\document_text_rebuild.log" -Tail 100

$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'

$Root = 'C:\Users\Administrator\Desktop\Cursor\OpenDentalMCP\live\OpenDentalMCP'
Set-Location $Root

# Pull ANTHROPIC_API_KEY from Machine env (the Task Scheduler context may not
# have it inherited if the task user differs).
$apiKey = [Environment]::GetEnvironmentVariable('ANTHROPIC_API_KEY', 'Machine')

# Backend selection and limits.
$env:MCP_CONFIG_FILE                = 'config.prod.json'
$env:DOC_TEXT_SKIP_CATEGORIES       = '179,180,181,463,467'
$env:OCR_BACKEND                    = 'local'
$env:LOCAL_VLM_BASE_URL             = 'http://192.168.127.78:11434'
$env:LOCAL_VLM_PRIMARY              = 'qwen2.5vl:7b'
$env:LOCAL_VLM_FALLBACK             = 'qwen3.5:9b'
$env:LOCAL_VLM_DPI                  = '150'
$env:LOCAL_VLM_HAIKU_PAGE_FALLBACK  = 'true'  # rescue pages that crash both local models
if ($apiKey) { $env:ANTHROPIC_API_KEY = $apiKey }

# Per-run caps. With workers=4 + OLLAMA_NUM_PARALLEL=4 on the GPU host we
# get ~3-4x throughput vs sequential. Wall-clock budget ~9 hours (21:00 -> 06:00).
#   sequential: ~10s/doc -> ~3,200 docs/9h
#   parallel-4: ~3s/doc effective -> ~10,000 docs/9h
# We cap at 8,000 to leave buffer for multi-page outliers and unforeseen slowdowns.
$maxDocs    = 8000
$maxSpend   = 5.00   # Haiku page-fallback cost ceiling per night
$workers    = 4

$timestamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
Write-Output "[$timestamp] Starting nightly backfill: max_docs=$maxDocs max_spend=`$$maxSpend workers=$workers"
$start = Get-Date

& "$Root\.venv\Scripts\python.exe" "$Root\scripts\rebuild_document_text_index.py" `
    --max-docs=$maxDocs `
    --max-spend=$maxSpend `
    --workers=$workers `
    --log-level=INFO

$elapsed = (Get-Date) - $start
$timestamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
Write-Output "[$timestamp] Nightly backfill done in $($elapsed.TotalMinutes.ToString('F1')) min"
