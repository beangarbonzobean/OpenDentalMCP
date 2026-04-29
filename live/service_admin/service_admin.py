"""
Service Admin HTTP Endpoint
Lightweight admin server for managing NSSM-hosted MCP services.
Supports restart, status checks, and log tailing.

Usage:
    python service_admin.py

Auth:
    All requests require: Authorization: Bearer <token>
    Token is read from SERVICE_ADMIN_TOKEN env var or .admin_token file.

Endpoints:
    GET  /status/<service>     - Service status via sc.exe
    POST /restart/<service>    - Restart via nssm restart
    GET  /logs/<service>       - Tail last N lines of service stdout log
    GET  /health               - Health check (no auth required)
"""

import http.server
import json
import os
import subprocess
import secrets
import sys
from pathlib import Path
from urllib.parse import urlparse, parse_qs

PORT = int(os.environ.get("SERVICE_ADMIN_PORT", 9800))
NSSM = os.environ.get("NSSM_PATH", r"C:\Program Files\nssm\nssm.exe")

# --- Known services and their metadata ---
KNOWN_SERVICES = {
    "OpenDentalMCPServer": {
        "display": "Open Dental MCP",
        "dir": r"C:\Users\Administrator\Desktop\Cursor\OpenDentalMCP\live\OpenDentalMCP",
        "log": "service_stdout.log",
    },
    "DEXISMCPHTTPServer": {
        "display": "DEXIS MCP",
        "dir": r"C:\Users\Administrator\Desktop\Cursor\OpenDentalMCP\live\DEXISMonitor",
        "log": "service_stdout.log",
    },
    "KnowledgeMCPServer": {
        "display": "Knowledge MCP",
        "dir": r"C:\Users\Administrator\Desktop\Cursor\OpenDentalMCP\live\KnowledgeMCP",
        "log": "service_stdout.log",
    },
}

# --- Token management ---
TOKEN_FILE = Path(__file__).parent / ".admin_token"


def load_or_create_token() -> str:
    """Load token from env, file, or generate a new one."""
    # 1. Environment variable takes priority
    env_token = os.environ.get("SERVICE_ADMIN_TOKEN")
    if env_token:
        return env_token

    # 2. Read from file
    if TOKEN_FILE.exists():
        token = TOKEN_FILE.read_text().strip()
        if token:
            return token

    # 3. Generate and save
    token = secrets.token_urlsafe(32)
    TOKEN_FILE.write_text(token)
    print(f"Generated new admin token (saved to {TOKEN_FILE})")
    print(f"Token: {token}")
    return token


AUTH_TOKEN = load_or_create_token()


def run_command(cmd: list[str], timeout: int = 30) -> dict:
    """Run a subprocess and return stdout/stderr/returncode."""
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=True,
        )
        return {
            "returncode": result.returncode,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
        }
    except subprocess.TimeoutExpired:
        return {"returncode": -1, "stdout": "", "stderr": f"Command timed out after {timeout}s"}
    except Exception as e:
        return {"returncode": -1, "stdout": "", "stderr": str(e)}


def get_service_status(service_name: str) -> dict:
    """Query service status via sc.exe."""
    result = run_command(["sc.exe", "query", service_name])
    state = "UNKNOWN"
    if result["returncode"] == 0:
        for line in result["stdout"].splitlines():
            if "STATE" in line:
                # e.g. "        STATE              : 4  RUNNING"
                parts = line.strip().split()
                if len(parts) >= 4:
                    state = parts[-1]
                break
    return {"service": service_name, "state": state, "raw": result["stdout"]}


def restart_service(service_name: str) -> dict:
    """Restart service via nssm."""
    result = run_command([NSSM, "restart", service_name], timeout=45)
    # Verify it came back up
    status = get_service_status(service_name)
    return {
        "service": service_name,
        "action": "restart",
        "nssm_output": result["stdout"] or result["stderr"],
        "nssm_returncode": result["returncode"],
        "post_restart_state": status["state"],
    }


def tail_log(service_name: str, lines: int = 50) -> dict:
    """Read last N lines of a service's stdout log."""
    meta = KNOWN_SERVICES.get(service_name)
    if not meta:
        return {"error": f"Unknown service: {service_name}"}

    log_path = Path(meta["dir"]) / meta["log"]
    if not log_path.exists():
        return {"error": f"Log not found: {log_path}", "path": str(log_path)}

    try:
        all_lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        tail = all_lines[-lines:] if len(all_lines) > lines else all_lines
        return {
            "service": service_name,
            "log_path": str(log_path),
            "total_lines": len(all_lines),
            "showing_last": len(tail),
            "lines": tail,
        }
    except Exception as e:
        return {"error": str(e), "path": str(log_path)}


class AdminHandler(http.server.BaseHTTPRequestHandler):
    """HTTP handler for service admin operations."""

    def _send_json(self, data: dict, status: int = 200):
        body = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _check_auth(self) -> bool:
        """Verify bearer token. Returns True if authorized."""
        auth = self.headers.get("Authorization", "")
        if auth == f"Bearer {AUTH_TOKEN}":
            return True
        self._send_json({"error": "Unauthorized. Provide: Authorization: Bearer <token>"}, 401)
        return False

    def _parse_path(self):
        """Parse URL path into segments."""
        parsed = urlparse(self.path)
        segments = [s for s in parsed.path.strip("/").split("/") if s]
        params = parse_qs(parsed.query)
        return segments, params

    def do_OPTIONS(self):
        """Handle CORS preflight."""
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()

    def do_GET(self):
        segments, params = self._parse_path()

        # Health check - no auth required
        if segments == ["health"]:
            self._send_json({
                "status": "ok",
                "services": list(KNOWN_SERVICES.keys()),
                "port": PORT,
            })
            return

        if not self._check_auth():
            return

        if len(segments) == 2 and segments[0] == "status":
            service = segments[1]
            if service not in KNOWN_SERVICES:
                self._send_json({"error": f"Unknown service: {service}", "known": list(KNOWN_SERVICES.keys())}, 404)
                return
            self._send_json(get_service_status(service))
            return

        if len(segments) == 2 and segments[0] == "logs":
            service = segments[1]
            if service not in KNOWN_SERVICES:
                self._send_json({"error": f"Unknown service: {service}"}, 404)
                return
            lines = int(params.get("lines", [50])[0])
            self._send_json(tail_log(service, lines))
            return

        # List all services status
        if segments == ["status"]:
            statuses = {name: get_service_status(name) for name in KNOWN_SERVICES}
            self._send_json(statuses)
            return

        self._send_json({"error": "Not found", "endpoints": {
            "GET /health": "Health check (no auth)",
            "GET /status": "All services status",
            "GET /status/<service>": "Single service status",
            "POST /restart/<service>": "Restart a service",
            "GET /logs/<service>?lines=N": "Tail service log",
        }}, 404)

    def do_POST(self):
        if not self._check_auth():
            return

        segments, _ = self._parse_path()

        if len(segments) == 2 and segments[0] == "restart":
            service = segments[1]
            if service not in KNOWN_SERVICES:
                self._send_json({"error": f"Unknown service: {service}", "known": list(KNOWN_SERVICES.keys())}, 404)
                return
            result = restart_service(service)
            status_code = 200 if result["post_restart_state"] == "RUNNING" else 500
            self._send_json(result, status_code)
            return

        # --- TEMPORARY: Database restore utilities ---
        if segments == ["db", "list-backups"]:
            result = run_command(["cmd", "/c", "dir", "D:\\Backups\\", "/b", "/o-d"], timeout=10)
            self._send_json({"path": "D:\\Backups", "output": result["stdout"], "error": result["stderr"]})
            return

        if segments == ["db", "list-binlogs"]:
            result = run_command(["cmd", "/c", "dir", "D:\\mysql\\data\\binlog.*", "/b", "/o-d"], timeout=10)
            self._send_json({"path": "D:\\mysql\\data", "output": result["stdout"], "error": result["stderr"]})
            return

        if segments == ["db", "run-sql-file"]:
            # Read POST body for the file path
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode('utf-8') if content_length else '{}'
            data = json.loads(body)
            file_path = data.get("file")
            if not file_path:
                self._send_json({"error": "Provide 'file' in POST body"}, 400)
                return
            result = run_command(["cmd", "/c", f'mysql -u root opendental < "{file_path}"'], timeout=120)
            self._send_json({"action": "run-sql-file", "file": file_path, "result": result})
            return

        if segments == ["db", "exec"]:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode('utf-8') if content_length else '{}'
            data = json.loads(body)
            cmd = data.get("cmd")
            if not cmd:
                self._send_json({"error": "Provide 'cmd' in POST body"}, 400)
                return
            result = run_command(["cmd", "/c", cmd], timeout=120)
            self._send_json({"action": "exec", "cmd": cmd, "result": result})
            return
        # --- END TEMPORARY ---

        self._send_json({"error": "Not found. Use POST /restart/<service>"}, 404)

    def log_message(self, format, *args):
        """Override to include timestamp in logs."""
        sys.stderr.write(f"[service_admin] {self.address_string()} - {format % args}\n")


def main():
    print(f"Service Admin starting on port {PORT}")
    print(f"Auth token: {AUTH_TOKEN}")
    print(f"Known services: {', '.join(KNOWN_SERVICES.keys())}")
    print(f"Endpoints:")
    print(f"  GET  http://0.0.0.0:{PORT}/health")
    print(f"  GET  http://0.0.0.0:{PORT}/status")
    print(f"  GET  http://0.0.0.0:{PORT}/status/<service>")
    print(f"  POST http://0.0.0.0:{PORT}/restart/<service>")
    print(f"  GET  http://0.0.0.0:{PORT}/logs/<service>?lines=N")

    server = http.server.HTTPServer(("0.0.0.0", PORT), AdminHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.server_close()


if __name__ == "__main__":
    main()
