"""
HTTP-based MCP Server for KnowledgeMCP
Exposes Claude Code memory files and skills over MCP so any client
(Claude.ai, Cowork, API/SDK, other machines) can read and write them.
"""

import json
import os
import sys
import logging
import secrets
from pathlib import Path
from functools import wraps
from typing import Any, Dict, List

from flask import Flask, request, jsonify
from flask_cors import CORS

sys.path.insert(0, str(Path(__file__).parent))

from mcp_tools import KnowledgeTools

# --- Logging ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("mcp_server_http.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

# --- Config ---
CONFIG_PATH = Path(__file__).parent / "config.prod.json"
if not CONFIG_PATH.exists():
    CONFIG_PATH = Path(__file__).parent / "config.example.json"
CONFIG: Dict[str, Any] = {}
if CONFIG_PATH.exists():
    CONFIG = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))

MEMORY_DIR = os.environ.get("KNOWLEDGE_MEMORY_DIR") or CONFIG.get("memory_dir", "")
SKILL_ROOTS = CONFIG.get("skill_roots", [])
env_roots = os.environ.get("KNOWLEDGE_SKILL_ROOTS")
if env_roots:
    SKILL_ROOTS = [p.strip() for p in env_roots.split(os.pathsep) if p.strip()]

if not MEMORY_DIR:
    raise RuntimeError(
        "memory_dir not configured. Set it in config.prod.json or env KNOWLEDGE_MEMORY_DIR."
    )

logger.info(f"Memory dir: {MEMORY_DIR}")
logger.info(f"Skill roots: {SKILL_ROOTS}")

tools = KnowledgeTools(memory_dir=MEMORY_DIR, skill_roots=SKILL_ROOTS)

# --- Flask App ---
app = Flask(__name__)
ALLOWED_ORIGINS = [
    "https://opendental-mcp.huntingtonbeachdentalcenter.com",
    "https://dexis-mcp.huntingtonbeachdentalcenter.com",
    "https://knowledge-mcp.huntingtonbeachdentalcenter.com",
]
CORS(app, origins=ALLOWED_ORIGINS, supports_credentials=True)

# --- Bearer Token Auth ---
TOKEN_FILE = Path(__file__).parent / ".mcp_token"


def load_or_create_token() -> str:
    token = os.environ.get("MCP_API_TOKEN")
    if token:
        return token
    if TOKEN_FILE.exists():
        return TOKEN_FILE.read_text().strip()
    token = secrets.token_urlsafe(48)
    TOKEN_FILE.write_text(token)
    logger.info(f"Generated new MCP API token -> {TOKEN_FILE}")
    return token


API_TOKEN = load_or_create_token()


def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if request.method == "OPTIONS":
            return f(*args, **kwargs)
        provided = None
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            provided = auth_header[len("Bearer "):]
        else:
            provided = request.args.get("token")
        if not provided:
            logger.warning(f"Auth missing from {request.remote_addr}")
            return jsonify({"error": "Authorization required"}), 401
        if not secrets.compare_digest(provided, API_TOKEN):
            logger.warning(f"Invalid token from {request.remote_addr}")
            return jsonify({"error": "Invalid token"}), 403
        return f(*args, **kwargs)

    return decorated


# --- Routes ---
@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "healthy",
        "service": "knowledge-mcp",
        "version": "1.0.0",
        "memory_dir": MEMORY_DIR,
        "skill_roots": [str(p) for p in SKILL_ROOTS],
    })


@app.route("/mcp", methods=["GET", "POST", "OPTIONS"])
@require_auth
def handle_mcp_request():
    if request.method == "OPTIONS":
        r = jsonify({})
        r.headers.add(
            "Access-Control-Allow-Headers",
            "Content-Type, Authorization, Accept, Mcp-Session-Id",
        )
        r.headers.add("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        return r, 204

    if request.method == "GET":
        return jsonify({
            "service": "knowledge-mcp",
            "endpoint": "/mcp",
            "usage": "Send JSON-RPC POST with Content-Type: application/json",
        }), 200

    # --- POST: JSON-RPC ---
    data = None
    try:
        data = request.get_json()
        if not data:
            return jsonify({
                "jsonrpc": "2.0",
                "error": {"code": -32700, "message": "Parse error"},
                "id": None,
            }), 400

        method = data.get("method")
        params = data.get("params", {})
        request_id = data.get("id")

        if isinstance(method, str) and method.startswith("notifications/"):
            return "", 204

        if method == "initialize":
            return jsonify({
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {
                        "name": "knowledge-mcp",
                        "version": "1.0.0",
                    },
                },
            })

        if method == "tools/list":
            return jsonify({
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"tools": tools.list_tools()},
            })

        if method == "tools/call":
            tool_name = params.get("name")
            arguments = params.get("arguments", {}) or {}
            try:
                result = tools.call_tool(tool_name, arguments)
                return jsonify({
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "content": [{
                            "type": "text",
                            "text": json.dumps(result, indent=2, default=str),
                        }]
                    },
                })
            except Exception as e:
                logger.error(f"Tool error {tool_name}: {e}", exc_info=True)
                return jsonify({
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32603, "message": str(e)},
                }), 500

        if method == "ping":
            return jsonify({
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"status": "pong"},
            })

        return jsonify({
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        }), 200

    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)
        return jsonify({
            "jsonrpc": "2.0",
            "error": {"code": -32603, "message": str(e)},
            "id": data.get("id") if data else None,
        }), 500


def main():
    port = int(os.getenv("MCP_HTTP_PORT", "8446"))
    host = os.getenv("MCP_HTTP_HOST", "0.0.0.0")
    logger.info(f"Starting knowledge-mcp on {host}:{port}")
    app.run(host=host, port=port, debug=False)


if __name__ == "__main__":
    main()
