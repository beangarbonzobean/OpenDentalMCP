# OpenDental + DEXIS MCP (monorepo)

This repository holds the **canonical application code** for:

- **OpenDental MCP** — `live/OpenDentalMCP/` → deploy to **`C:\OpenDentalMCP`**
- **DEXIS monitor + MCP** — `live/DEXISMonitor/` → deploy to **`C:\DEXISMonitor`**

Documentation lives under **[`docs/`](docs/README.md)**.

## Remote edit → push → deploy

1. **Clone** this repo where you work (or edit on GitHub/GitLab).
2. **Change code** under `live/OpenDentalMCP/` and/or `live/DEXISMonitor/`.
3. **Commit and push** to your remote.
4. **On the Windows server:** `git pull` (or copy files from your machine), then copy updated files into `C:\OpenDentalMCP` / `C:\DEXISMonitor`, **`pip install -r requirements.txt`** if dependencies changed, and **restart** the relevant Windows services.

Git does **not** replace files on `C:\` automatically; the copy step is required unless you script it.

## Configuration and secrets

| Item | In Git? |
|------|--------|
| `config.example.json` in each `live/.../` folder | Yes — copy to `config.json` per machine |
| `config.json` | **No** — gitignored; keep on each server |
| `.env` | **No** — use `env_template_for_server.txt` (OpenDental) |
| TLS (`*.pem`, `*.crt`, `*.key`) | **No** |

## Resolved repo hygiene

- **Single code location:** `live/` only (old duplicate deployment folders were removed).
- **Docs:** Sanitized hostnames and keys; deployment guides updated for this repo layout.
- **`.gitignore`:** Ignores secrets, certs, logs, and per-machine `config.json` under `live/`.

## First-time Git on this machine

```powershell
cd C:\Users\Administrator\Desktop\Cursor\OpenDentalMCP
git init
git add .
git status   # confirm no .env or config.json under live/
git commit -m "Initial commit: OpenDental + DEXIS MCP"
```

Then add `origin` and push.
