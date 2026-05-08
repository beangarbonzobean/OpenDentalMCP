# OpenDental MCP Server

Main MCP (Model Context Protocol) server providing AI agents with access to the Open Dental REST API. Hosts Flask blueprints for intake automation, OCR review, and new patient tracking. Runs nightly OCR backfill and periodic intake auto-filing jobs.

## Architecture

### MCP Server Core

**Service**: `OpenDentalMCPServer` (NSSM Windows service)  
**Runtime**: `C:\Users\Administrator\Desktop\Cursor\OpenDentalMCP\live\OpenDentalMCP`  
**Entrypoint**: `mcp_server_http.py`  
**Python**: `.venv\Scripts\python.exe`

**Endpoints**:
- **Public**: `https://opendental-mcp.huntingtonbeachdentalcenter.com/mcp`
- **LAN**: `http://192.168.127.49:8445`

The MCP server implements the JSON-RPC 2.0 protocol and exposes Open Dental API operations as MCP tools. See `mcp_tools.py` for the complete tool catalog (patient management, appointments, treatment plans, claims, documents, etc.).

### Flask Blueprints

All Flask blueprints are **LAN-only** (RFC-1918 source-IP gating) and serve browser-based dashboards for staff.

#### 1. New Patient Tracker (`/tracker`)

**Purpose**: Dashboard tracking which doctor examined each new patient  
**Routes**:
- `GET /tracker/` - static dashboard HTML
- `GET /tracker/api/new-patients` - JSON data wrapping `get_new_patient_exam_doctors()`
- `GET /tracker/healthz` - readiness probe

**Implementation**: `np_tracker_routes.py` + `new_patient_doctor_resolver.py`  
**UI**: `np_tracker_app/index.html`

#### 2. Intake Review (`/intake`)

**Purpose**: Review queue for batch-scan auto-filing — staff confirm, override, or reject AI-suggested patient matches and document categories  
**Routes**:
- `GET /intake/` - static review dashboard HTML
- `GET /intake/api/queue` - pending/queued/error items
- `GET /intake/api/item/<id>` - item details + audit log
- `GET /intake/api/item/<id>/pdf` - candidate PDF (extracted pages)
- `POST /intake/api/item/<id>/confirm` - file with suggested patient/category
- `POST /intake/api/item/<id>/override` - file with overridden patient/category
- `POST /intake/api/item/<id>/reject` - mark rejected (no OD write)
- `GET /intake/api/categories` - curated document taxonomy
- `GET /intake/api/patient-search` - proxy to OD patient search

**Implementation**: `intake_routes.py` + `preprocessing/intake/*`  
**UI**: `intake_app/index.html`  
**Watch folder**: `\\SERVER12\ShareFolder\Scans` (configurable via `INTAKE_WATCH_FOLDER`)  
**Auto-file threshold**: `1.5` (shadow mode — nothing auto-files; everything queues for review)

#### 3. OCR Review (`/ocr-review`)

**Purpose**: Review overnight OCR batches of historical OD documents — approve good OCR or flag bad OCR for re-run  
**Routes**:
- `GET /ocr-review/` - static dashboard HTML
- `GET /ocr-review/api/summary` - counts + cost stats
- `GET /ocr-review/api/queue` - list of recent OCR'd docs
- `GET /ocr-review/api/doc/<doc_num>` - full text + audit metadata
- `GET /ocr-review/api/doc/<doc_num>/pdf` - original PDF (re-rendered from share)
- `POST /ocr-review/api/doc/<doc_num>/approve` - mark Reviewed=1
- `POST /ocr-review/api/doc/<doc_num>/flag` - DELETE row (next backfill re-OCRs)
- `GET /ocr-review/healthz` - readiness probe

**Implementation**: `ocr_review_routes.py` + `preprocessing/document_text_cache.py`  
**UI**: `ocr_review_app/index.html`

### Nightly Jobs

#### OCR Backfill (21:00 nightly)

**Script**: `scripts/rebuild_document_text_index.py`  
**Scheduler**: Windows Task Scheduler → `scripts/nightly_doc_text_backfill.ps1`  
**Purpose**: OCR the next batch of uncached OD documents into the full-text search cache  
**Log**: `logs/document_text_rebuild.log`

**Backend**: Local VLM (Ollama on `LABCOMPUTER` / `192.168.127.78:11434`)
- **Primary**: `qwen2.5vl:7b` (150 DPI)
- **Fallback**: `qwen3.5:9b`
- **Page fallback**: Claude Haiku (for pages that crash both local models)

**Limits**:
- **Max docs**: 8,000/night (~3s/doc effective with 4 workers)
- **Max spend**: $5.00 (Haiku page-fallback cost ceiling)
- **Workers**: 4 (client-side concurrency)
- **Keep-alive**: 30s (unloads model after last call to free VRAM for staff apps)

**Wall-clock budget**: ~9 hours (21:00 → 06:00; clinic closes at 20:00)

#### Intake Auto-Filing (every 10 minutes)

**Script**: `scripts/intake_processor.py`  
**Scheduler**: Windows Task Scheduler → `scripts/intake_processor_run.ps1`  
**Purpose**: Classify and match new PDFs from watch folder; auto-file high-confidence matches or queue for staff review  
**Log**: `logs/intake_processor.log`

**Watch folder**: `\\SERVER12\ShareFolder\Scans` (scanner dumps batch PDFs here)  
**Auto-file threshold**: `1.5` (shadow mode — nothing auto-files; everything queues for review in `/intake`)

**OCR backend**: Same local VLM + Haiku stack as nightly backfill (qwen2.5vl primary so daytime feedback transfers to nightly archive OCR)

### Sibling MCP Services

The OpenDental MCP is one of three sibling MCP servers deployed on the same host:

#### 1. DEXIS MCP (`DEXISMCPHTTPServer`)

**Purpose**: X-ray access (search patients, retrieve X-ray metadata, fetch image files)  
**Runtime**: `live/DEXISMonitor`  
**Endpoint**: `https://dexis-mcp.huntingtonbeachdentalcenter.com/mcp`  
**Service**: `DEXISMCPHTTPServer` (NSSM)

#### 2. Knowledge MCP (`KnowledgeMCPServer`)

**Purpose**: Claude Code memory + skills (list/read/save memories, list/read skills)  
**Runtime**: `live/KnowledgeMCP`  
**Endpoint**: `https://knowledge-mcp.huntingtonbeachdentalcenter.com/mcp`  
**Service**: `KnowledgeMCPServer` (NSSM)  
**Port**: 8446

### Service Management

**Service Admin** (`ServiceAdmin`)  
**Purpose**: Remote management dashboard for Cowork (no more PowerShell relay)  
**Runtime**: `live/service_admin`  
**Endpoint**: `https://service-admin.huntingtonbeachdentalcenter.com`  
**LAN**: `http://192.168.127.49:9800`  
**Token file**: `live/service_admin/.admin_token`

**API**:
- `GET /status` - service status (running/stopped)
- `POST /restart` - restart a service by name
- `GET /logs` - tail recent log lines

**Usage** (from Chrome on Cowork):
```javascript
// Restart OpenDental MCP after code changes
fetch('https://service-admin.huntingtonbeachdentalcenter.com/restart/OpenDentalMCPServer', {
  method: 'POST',
  headers: { 'Authorization': 'Bearer YOUR_TOKEN_HERE' }
})
```

## Development Workflow

### Making Changes

1. **Edit code** in `live/OpenDentalMCP/`
2. **Restart service** via Service Admin or PowerShell:
   ```powershell
   nssm restart OpenDentalMCPServer
   ```
3. **Validate** via raw JSON-RPC (curl or Postman against `/mcp` endpoint)

**Important**: Code edits don't take effect until the NSSM service is restarted.

### Testing

**Test patient**: Ben Young (use as both test patient AND test employee)

**Run tests**:
```powershell
cd live/OpenDentalMCP
.venv\Scripts\python.exe -m pytest tests/ -v
```

**Coverage**:
- Unit tests for intake pipeline (`tests/test_intake_*.py`)
- Blueprint tests (`tests/test_intake_routes.py`, `tests/test_ocr_review_routes.py`)
- OCR/PDF tests (`tests/test_ocr_helper.py`, `tests/test_pdf_render.py`)
- Safety/validation tests (`tests/test_sql_safety.py`, `tests/test_preflight.py`)

### Code Conventions

See `mcp_tools.py` for the canonical pattern:

1. **Add schema** in `list_tools()`
2. **Dispatch** in `call_tool()`
3. **Add resource** in `_list_resources()`
4. **Implement `_xxx()` method** that returns `{"success": True/False, ...}` and catches its own exceptions

**API method validation**: `_make_request()` dispatches HTTP by method name only (GET/POST/PUT/DELETE). The old `ENDPOINT_METHODS` validation map has been removed.

**Delete helpers**: `_delete_allergy`, `_delete_claim_payment`, `_delete_insurance_subscription`, `_delete_treatment_plan_procedure` all use `_make_request("DELETE", ...)`.

## Configuration

**Config file**: `config.prod.json` (referenced via `MCP_CONFIG_FILE` env var)  
**Secrets**: ANTHROPIC_API_KEY from Machine env (inherited by Task Scheduler jobs)

**OCR backend selection** (env vars):
- `OCR_BACKEND=local` (Ollama) vs. `anthropic` (Haiku)
- `LOCAL_VLM_BASE_URL=http://192.168.127.78:11434`
- `LOCAL_VLM_PRIMARY=qwen2.5vl:7b`
- `LOCAL_VLM_FALLBACK=qwen3.5:9b`
- `LOCAL_VLM_DPI=150`
- `LOCAL_VLM_HAIKU_PAGE_FALLBACK=true`
- `LOCAL_VLM_KEEP_ALIVE=30s` (nightly) / `12h` (daytime)

**Intake settings**:
- `INTAKE_WATCH_FOLDER=\\SERVER12\ShareFolder\Scans`
- `INTAKE_AUTO_FILE_THRESHOLD=1.5` (shadow mode — set to 0.95 for production auto-filing)

**Document text index**:
- `DOC_TEXT_SKIP_CATEGORIES=179,180,181,463,467` (categories to skip in OCR backfill)

## Logs

**Service logs** (NSSM stdout/stderr):
- Not currently file-logged (rely on Windows Event Viewer or NSSM log config)

**Job logs**:
- `logs/document_text_rebuild.log` (OCR backfill)
- `logs/intake_processor.log` (intake auto-filing)

**Inspect recent runs**:
```powershell
Get-Content "C:\Users\Administrator\Desktop\Cursor\OpenDentalMCP\live\OpenDentalMCP\logs\document_text_rebuild.log" -Tail 100
Get-Content "C:\Users\Administrator\Desktop\Cursor\OpenDentalMCP\live\OpenDentalMCP\logs\intake_processor.log" -Tail 100
```

## Repository Structure

```
live/OpenDentalMCP/
├── mcp_server_http.py              # Flask app + MCP JSON-RPC handler
├── mcp_tools.py                    # OpenDentalMCPTools class (MCP tool catalog)
├── np_tracker_routes.py            # New Patient Tracker blueprint
├── intake_routes.py                # Intake Review blueprint
├── ocr_review_routes.py            # OCR Review blueprint
├── new_patient_doctor_resolver.py  # NP tracker business logic
├── preprocessing/                  # OCR + intake pipeline modules
│   ├── intake/                     # Intake auto-filing pipeline
│   │   ├── classifier.py           # Document classification
│   │   ├── extractor.py            # Structured data extraction
│   │   ├── filer.py                # OD document filing
│   │   ├── matcher.py              # Patient matching
│   │   ├── processor.py            # Orchestrator
│   │   ├── taxonomy.py             # Document category taxonomy
│   │   └── cache.py                # Review queue persistence
│   ├── document_text_cache.py      # OCR cache (SQLite)
│   ├── document_text_index.py      # Full-text search index
│   ├── ocr_helper.py               # OCR backend abstraction
│   ├── pdf_render.py               # PDF → PNG rendering
│   ├── path_resolver.py            # OD document share path resolution
│   └── ...
├── scripts/                        # CLI entry points for jobs
│   ├── rebuild_document_text_index.py
│   ├── intake_processor.py
│   ├── nightly_doc_text_backfill.ps1
│   ├── intake_processor_run.ps1
│   └── ...
├── tests/                          # pytest suite
├── np_tracker_app/                 # New Patient Tracker UI (static HTML/JS)
├── intake_app/                     # Intake Review UI (static HTML/JS)
├── ocr_review_app/                 # OCR Review UI (static HTML/JS)
├── data/                           # SQLite caches + lock files
├── logs/                           # Job logs (rotating)
├── config.prod.json                # API credentials + DB connection
└── .venv/                          # Python virtual environment
```

## Onboarding Checklist

- [ ] Verify NSSM service is running: `nssm status OpenDentalMCPServer`
- [ ] Test MCP endpoint: `curl https://opendental-mcp.huntingtonbeachdentalcenter.com/health`
- [ ] Browse to LAN dashboards: `http://192.168.127.49:8445/tracker`, `/intake`, `/ocr-review`
- [ ] Check Service Admin: `https://service-admin.huntingtonbeachdentalcenter.com/status`
- [ ] Inspect nightly OCR log: `Get-Content logs\document_text_rebuild.log -Tail 50`
- [ ] Inspect intake processor log: `Get-Content logs\intake_processor.log -Tail 50`
- [ ] Run test suite: `.venv\Scripts\python.exe -m pytest tests/ -v`
- [ ] Verify Ollama reachable: `curl http://192.168.127.78:11434/api/tags`
- [ ] Check sibling services: DEXIS MCP, Knowledge MCP (via Service Admin)

## Support

**Author**: Built for Huntington Beach Dental Center  
**MCP Protocol**: [Model Context Protocol 2024-11-05](https://modelcontextprotocol.io/)  
**Open Dental API**: [Open Dental FHIR REST API](https://opendental.com/site/apidocs.html)

For issues with the MCP server, check Service Admin logs first. For issues with nightly jobs, tail the job logs. For issues with Flask blueprints, check browser console + network tab (LAN-only access enforced at source-IP level).
