# Production code (canonical)

These folders track what runs on the server disks listed below. **Edit here in Git**, then deploy to the machine paths when you are ready.

| Folder | Deploy to |
|--------|-----------|
| `OpenDentalMCP/` | `C:\OpenDentalMCP\` |
| `DEXISMonitor/` | `C:\DEXISMonitor\` |

**Last imported from disk:** 2025-03-22 (update when you re-sync).

## Configuration

- **`config.json`** is **not** committed (see `.gitignore`). Templates are:
  - `OpenDentalMCP/config.example.json`
  - `DEXISMonitor/config.example.json`
- On a new server, copy the matching example to `config.json` and fill in real values.
- **`.env`** (OpenDental) and **TLS files** are not in Git; use `env_template_for_server.txt` and your cert process.

## Re-import from a server (PowerShell)

Use this to refresh the repo from live installs (still excludes secrets, certs, and logs):

```powershell
$repo = 'C:\Path\To\OpenDentalMCP'
$liveRoot = Join-Path $repo 'live'

function ShouldSkipFile($name) {
  if ($name -match '^\.env') { return $true }
  if ($name -match '\.(pem|crt|key)$') { return $true }
  if ($name -match '\.log$') { return $true }
  if ($name -eq 'config.json') { return $true }
  return $false
}

function Copy-LiveFlat($src, $destDir) {
  New-Item -ItemType Directory -Force -Path $destDir | Out-Null
  Get-ChildItem -LiteralPath $src -File -Force | ForEach-Object {
    if (ShouldSkipFile $_.Name) { return }
    Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $destDir $_.Name) -Force
  }
}

Copy-LiveFlat 'C:\OpenDentalMCP' (Join-Path $liveRoot 'OpenDentalMCP')
Copy-LiveFlat 'C:\DEXISMonitor' (Join-Path $liveRoot 'DEXISMonitor')
```

After importing, recreate **`config.example.json`** if new settings appeared on disk (or merge by hand). Update the **Last imported** date above.
