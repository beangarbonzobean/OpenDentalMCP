#!/usr/bin/env python3
"""
Open Dental MCP Server (HTTP version)
Provides MCP API access to Open Dental REST API for AI agents via HTTP/HTTPS
Compatible with HTTP-based MCP clients
"""

import json
import sys
import logging
from typing import Any, Dict, List, Optional
from pathlib import Path
from flask import Flask, request, jsonify
from flask_cors import CORS
import ssl
import os

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent))

from mcp_tools import OpenDentalMCPTools
from np_tracker_routes import np_tracker_bp

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)  # Enable CORS for cross-origin requests

tools = OpenDentalMCPTools()

# New Patient Tracker blueprint — LAN-only browser dashboard. The blueprint
# enforces RFC-1918 source-IP gating internally; see np_tracker_routes.py.
app.register_blueprint(np_tracker_bp)


@app.route('/health', methods=['GET'])
def health():
    """Health check endpoint"""
    return jsonify({
        "status": "healthy",
        "service": "opendental-mcp-http",
        "version": "1.0.0"
    })


@app.route('/mcp', methods=['GET', 'POST', 'OPTIONS'])
def handle_mcp_request():
    """Handle MCP JSON-RPC requests (POST). GET/OPTIONS for client probes and CORS."""
    if request.method == 'OPTIONS':
        r = jsonify({})
        r.headers.add('Access-Control-Allow-Origin', '*')
        r.headers.add('Access-Control-Allow-Headers', 'Content-Type, Authorization, Accept, Mcp-Session-Id')
        r.headers.add('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        return r, 204

    if request.method == 'GET':
        # Avoid 405 when clients probe /mcp (e.g. SSE fallback); RPC is POST-only here.
        return jsonify({
            "service": "opendental-mcp-http",
            "endpoint": "/mcp",
            "usage": "Send JSON-RPC POST with Content-Type: application/json",
            "example": {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        }), 200

    try:
        data = request.get_json()
        if not data:
            return jsonify({
                "jsonrpc": "2.0",
                "error": {"code": -32700, "message": "Parse error"},
                "id": None
            }), 400
        
        method = data.get("method")
        params = data.get("params", {})
        request_id = data.get("id")
        
        logger.debug(f"Received request: {method} with params: {params}")

        # MCP notifications (no response body required; Cursor sends notifications/initialized)
        if isinstance(method, str) and method.startswith("notifications/"):
            logger.info(f"MCP notification acknowledged: {method}")
            return "", 204
        
        # Handle initialize method
        if method == "initialize":
            return jsonify({
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {
                        "tools": {}
                    },
                    "serverInfo": {
                        "name": "opendental-mcp",
                        "version": "1.0.0"
                    }
                }
            })
        
        # Handle tools/list
        elif method == "tools/list":
            tools_list = tools.list_tools()
            return jsonify({
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"tools": tools_list}
            })
        
        # Handle tools/call
        elif method == "tools/call":
            tool_name = params.get("name")
            arguments = params.get("arguments", {})
            
            try:
                result = tools.call_tool(tool_name, arguments)
                # Opt-in rich content: if a tool returns a dict with "_mcp_content",
                # pass that array through as MCP content blocks (supports image/document
                # blocks). Otherwise fall back to the default text-wrap behavior.
                if isinstance(result, dict) and "_mcp_content" in result:
                    content = result["_mcp_content"]
                else:
                    content = [
                        {
                            "type": "text",
                            "text": json.dumps(result, indent=2)
                        }
                    ]
                return jsonify({
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {"content": content}
                })
            except Exception as e:
                logger.error(f"Error calling tool {tool_name}: {e}", exc_info=True)
                return jsonify({
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {
                        "code": -32603,
                        "message": f"Tool execution error: {str(e)}"
                    }
                }), 500
        
        # Handle ping
        elif method == "ping":
            return jsonify({
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"status": "pong"}
            })
        
        else:
            return jsonify({
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {
                    "code": -32601,
                    "message": f"Method not found: {method}"
                }
            }), 404
            
    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)
        return jsonify({
            "jsonrpc": "2.0",
            "error": {
                "code": -32603,
                "message": f"Internal error: {str(e)}"
            },
            "id": request.get_json().get("id") if request.is_json else None
        }), 500


def create_ssl_context():
    """Create SSL context for HTTPS"""
    cert_file = Path(__file__).parent / "cert.pem"
    key_file = Path(__file__).parent / "key.pem"
    
    if cert_file.exists() and key_file.exists():
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert_file, key_file)
        return context
    return None


def main():
    """Main entry point"""
    port = int(os.getenv("MCP_HTTP_PORT", "8444"))
    host = os.getenv("MCP_HTTP_HOST", "0.0.0.0")
    use_https = os.getenv("MCP_USE_HTTPS", "true").lower() == "true"
    
    logger.info(f"Open Dental MCP HTTP Server starting on {host}:{port} (HTTPS: {use_https})...")
    
    ssl_context = create_ssl_context() if use_https else None
    
    if use_https and not ssl_context:
        logger.warning("HTTPS requested but certificates not found. Generating self-signed certificates...")
        # Generate self-signed certificates
        from cryptography import x509
        from cryptography.x509.oid import NameOID
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from datetime import datetime, timedelta
        
        # Generate private key
        private_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048
        )
        
        # Create certificate
        subject = issuer = x509.Name([
            x509.NameAttribute(NameOID.COUNTRY_NAME, "US"),
            x509.NameAttribute(NameOID.STATE_OR_PROVINCE_NAME, "CA"),
            x509.NameAttribute(NameOID.LOCALITY_NAME, "Huntington Beach"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Dental AI Agent"),
            x509.NameAttribute(NameOID.COMMON_NAME, "localhost"),
        ])
        
        cert = x509.CertificateBuilder().subject_name(
            subject
        ).issuer_name(
            issuer
        ).public_key(
            private_key.public_key()
        ).serial_number(
            x509.random_serial_number()
        ).not_valid_before(
            datetime.utcnow()
        ).not_valid_after(
            datetime.utcnow() + timedelta(days=365)
        ).add_extension(
            x509.SubjectAlternativeName([
                x509.DNSName("localhost"),
            ]),
            critical=False,
        ).sign(private_key, hashes.SHA256())
        
        # Save certificate and key
        cert_file = Path(__file__).parent / "cert.pem"
        key_file = Path(__file__).parent / "key.pem"
        
        cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        key_file.write_bytes(private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption()
        ))
        
        logger.info("Self-signed certificates generated.")
        ssl_context = create_ssl_context()
    
    app.run(host=host, port=port, ssl_context=ssl_context, debug=False)


if __name__ == "__main__":
    main()

