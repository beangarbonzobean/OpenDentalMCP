"""Autonomous Claude usage refresh.

Hits the same JSON API the dashboard's bookmarklet uses
(/api/organizations/<uuid>/usage on claude.ai) using saved session cookies,
then POSTs the result to /utilization/api/claude-snapshot-json.

No browser involved — this is a plain HTTP call with a cookie jar. Schedule
on a cron (e.g. every 10-15 min via Windows Task Scheduler) and the
dashboard's Claude panels stay current without anyone clicking anything.

ONE-TIME SETUP
==============

1. Make sure you're logged into claude.ai in any Chrome profile.
2. Install the "Cookie-Editor" extension (or any equivalent that exports
   cookies as JSON).
3. On any claude.ai page, open Cookie-Editor, click "Export → JSON" and
   save the result to:
       live/OpenDentalMCP/data/claude_cookies.json
4. Test:
       .venv\\Scripts\\python.exe scripts\\refresh_claude_usage.py
   Should print {"ok": true, "ts": "...", ...}.
5. Schedule via Task Scheduler — every 10-15 min. The script is fast
   (~1 second per run) and quiet (no log spam unless an error occurs).

When cookies expire (Anthropic rotates session tokens periodically — usually
weeks, not days), the script returns 401-ish errors. Re-export from
Cookie-Editor and overwrite the file. No restart needed.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Optional

try:
    import requests  # noqa: F401  ensure available
except ImportError:
    print("FATAL: `requests` not installed. Run: .venv\\Scripts\\pip install requests",
          file=sys.stderr)
    sys.exit(2)
import requests


_REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_COOKIES = _REPO_ROOT / "data" / "claude_cookies.json"

DASHBOARD_URL = os.environ.get(
    "UTILIZATION_DASHBOARD_URL",
    "http://127.0.0.1:9766/utilization/api/claude-snapshot-json",
)
COOKIES_PATH = Path(os.environ.get("CLAUDE_COOKIES_PATH", DEFAULT_COOKIES))
USER_AGENT = os.environ.get(
    "CLAUDE_USAGE_UA",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
)


def load_cookies(path: Path) -> dict[str, str]:
    """Load cookies from a JSON file. Supports two common formats:

    1. Cookie-Editor / EditThisCookie array form:
         [{"name": ..., "value": ..., "domain": ".claude.ai", ...}, ...]
    2. Flat dict form:
         {"sessionKey": "...", "lastActiveOrg": "..."}
    """
    if not path.exists():
        raise FileNotFoundError(f"cookies file not found at {path}")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, list):
        out: dict[str, str] = {}
        for c in raw:
            domain = c.get("domain", "")
            if "claude.ai" not in domain:
                continue
            if "name" in c and "value" in c:
                out[c["name"]] = c["value"]
        return out
    if isinstance(raw, dict):
        return {k: str(v) for k, v in raw.items()}
    raise ValueError(f"unsupported cookies file format: {type(raw).__name__}")


def fetch_claude_payload(session: requests.Session) -> dict:
    """Hit the org list, find claude_max org, fetch usage + prepaid credits."""
    orgs = session.get("https://claude.ai/api/organizations", timeout=10)
    orgs.raise_for_status()
    org_list = orgs.json()
    org = next(
        (o for o in org_list if "claude_max" in (o.get("capabilities") or [])),
        None,
    )
    if not org:
        raise RuntimeError(
            "No org with claude_max capability — cookies may be for the wrong "
            "account, or the org no longer has Max."
        )
    org_id = org["uuid"]

    usage = session.get(
        f"https://claude.ai/api/organizations/{org_id}/usage", timeout=10,
    )
    usage.raise_for_status()
    usage_payload = usage.json()

    prepaid: Optional[dict] = None
    try:
        pc = session.get(
            f"https://claude.ai/api/organizations/{org_id}/prepaid/credits",
            timeout=10,
        )
        if pc.ok:
            prepaid = pc.json()
    except requests.RequestException:
        pass

    return {"usage": usage_payload, "prepaid_credits": prepaid, "org_uuid": org_id}


def submit(payload: dict) -> dict:
    res = requests.post(DASHBOARD_URL, json=payload, timeout=10)
    res.raise_for_status()
    return res.json()


def main() -> int:
    try:
        cookies = load_cookies(COOKIES_PATH)
    except (FileNotFoundError, ValueError) as e:
        print(f"FATAL: {e}", file=sys.stderr)
        return 2

    if not cookies:
        print(
            f"FATAL: no claude.ai cookies in {COOKIES_PATH} — "
            "did the export include the right domain?",
            file=sys.stderr,
        )
        return 2

    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    for name, value in cookies.items():
        session.cookies.set(name, value, domain=".claude.ai")

    try:
        payload = fetch_claude_payload(session)
    except requests.HTTPError as e:
        status = e.response.status_code if e.response else "?"
        print(
            f"FATAL: claude.ai returned HTTP {status}. "
            "Cookies may have expired — re-export from the browser.",
            file=sys.stderr,
        )
        return 1
    except requests.RequestException as e:
        print(f"FATAL: claude.ai request failed: {e}", file=sys.stderr)
        return 1
    except RuntimeError as e:
        print(f"FATAL: {e}", file=sys.stderr)
        return 1

    # Strip org_uuid before sending — the dashboard endpoint doesn't expect it.
    org_uuid = payload.pop("org_uuid", None)

    try:
        res = submit(payload)
    except requests.RequestException as e:
        print(f"FATAL: dashboard POST failed: {e}", file=sys.stderr)
        return 1

    print(json.dumps({"ok": res.get("ok"), "ts": res.get("ts"),
                      "org_uuid": org_uuid, "fields": res.get("fields_parsed")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
