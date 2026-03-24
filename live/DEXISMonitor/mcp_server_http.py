"""
HTTP-based MCP Server for DEXIS Database Access
Provides AI assistants with access to DEXIS database via HTTP REST API.
"""

import json
import logging
import os
import sys
from flask import Flask, request, jsonify
from flask_cors import CORS
from typing import Any, Dict, Optional

from mcp_tools import (
    list_tables,
    describe_table,
    list_columns,
    execute_query,
    search_patient,
    get_patient_xrays,
    get_xrays_by_date,
    get_xrays_by_type,
    get_xray_info,
    get_recent_xrays,
    search_xrays,
    get_xray_statistics
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('mcp_server_http.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Create Flask app
app = Flask(__name__)
CORS(app)  # Enable CORS for cross-origin requests

# Load config
def load_config() -> Dict:
    """Load configuration from MCP_CONFIG_FILE or config.json."""
    config_file = os.getenv("MCP_CONFIG_FILE", "config.json")
    try:
        with open(config_file, 'r', encoding='utf-8-sig') as f:
            return json.load(f)
    except FileNotFoundError:
        logger.warning("%s not found. Using defaults.", config_file)
        return {
            "database": {
                "server": "(local)\\DEXIS_DATA",
                "database": "DEXIS",
                "use_windows_auth": True
            }
        }

config = load_config()


# Tool definitions
TOOLS = [
    {
        "name": "list_tables",
        "description": "List all tables in the DEXIS database",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": []
        }
    },
    {
        "name": "describe_table",
        "description": "Get column names, types, and structure for a table",
        "inputSchema": {
            "type": "object",
            "properties": {
                "table_name": {
                    "type": "string",
                    "description": "Name of the table to describe"
                }
            },
            "required": ["table_name"]
        }
    },
    {
        "name": "list_columns",
        "description": "Get all columns for a specific table",
        "inputSchema": {
            "type": "object",
            "properties": {
                "table_name": {
                    "type": "string",
                    "description": "Name of the table"
                }
            },
            "required": ["table_name"]
        }
    },
    {
        "name": "execute_query",
        "description": "Execute read-only SELECT query with safety checks",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "SQL SELECT query to execute"
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of rows to return (default: 1000)",
                    "default": 1000
                }
            },
            "required": ["query"]
        }
    },
    {
        "name": "search_patient",
        "description": "Find patients by name (first, last, or full name)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Patient name to search for"
                }
            },
            "required": ["name"]
        }
    },
    {
        "name": "get_patient_xrays",
        "description": "Get all x-rays for a patient ID or name",
        "inputSchema": {
            "type": "object",
            "properties": {
                "patient_id": {
                    "type": "integer",
                    "description": "Patient PersonID"
                },
                "patient_name": {
                    "type": "string",
                    "description": "Patient name (first, last, or full)"
                }
            },
            "required": []
        }
    },
    {
        "name": "get_xrays_by_date",
        "description": "Get x-rays taken on a specific date",
        "inputSchema": {
            "type": "object",
            "properties": {
                "target_date": {
                    "type": "string",
                    "description": "Date in format 'YYYY-MM-DD'"
                }
            },
            "required": ["target_date"]
        }
    },
    {
        "name": "get_xrays_by_type",
        "description": "Get x-rays by type (Periapical, Bitewing, Panoramic, etc.)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "xray_type": {
                    "type": "string",
                    "description": "Type of x-ray: Periapical, Bitewing, Panoramic, Intraoral Photo, Extraoral Photo",
                    "enum": ["Periapical", "Bitewing", "Panoramic", "Intraoral Photo", "Extraoral Photo"]
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of results (default: 100)",
                    "default": 100
                }
            },
            "required": ["xray_type"]
        }
    },
    {
        "name": "get_xray_info",
        "description": "Get detailed info for a specific x-ray by VisualID",
        "inputSchema": {
            "type": "object",
            "properties": {
                "visual_id": {
                    "type": "integer",
                    "description": "VisualID of the x-ray"
                }
            },
            "required": ["visual_id"]
        }
    },
    {
        "name": "get_recent_xrays",
        "description": "Get most recent x-rays",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of x-rays to return (default: 50)",
                    "default": 50
                }
            },
            "required": []
        }
    },
    {
        "name": "search_xrays",
        "description": "Advanced search with multiple filters (date range, type, patient, etc.)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "patient_name": {
                    "type": "string",
                    "description": "Patient name filter"
                },
                "xray_type": {
                    "type": "string",
                    "description": "X-ray type filter",
                    "enum": ["Periapical", "Bitewing", "Panoramic", "Intraoral Photo", "Extraoral Photo"]
                },
                "start_date": {
                    "type": "string",
                    "description": "Start date (YYYY-MM-DD)"
                },
                "end_date": {
                    "type": "string",
                    "description": "End date (YYYY-MM-DD)"
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of results (default: 100)",
                    "default": 100
                }
            },
            "required": []
        }
    },
    {
        "name": "get_xray_statistics",
        "description": "Get statistics (counts by type, date ranges, etc.)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "start_date": {
                    "type": "string",
                    "description": "Start date (YYYY-MM-DD)"
                },
                "end_date": {
                    "type": "string",
                    "description": "End date (YYYY-MM-DD)"
                }
            },
            "required": []
        }
    }
]


def handle_tool_call(tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    """Handle tool calls and return results."""
    try:
        logger.info(f"Handling tool call: {tool_name} with arguments: {arguments}")
        
        # Schema Discovery Tools
        if tool_name == "list_tables":
            result = list_tables(config)
        elif tool_name == "describe_table":
            result = describe_table(arguments.get("table_name"), config)
        elif tool_name == "list_columns":
            result = list_columns(arguments.get("table_name"), config)
        
        # Query Execution Tools
        elif tool_name == "execute_query":
            result = execute_query(
                arguments.get("query"),
                config,
                arguments.get("limit", 1000)
            )
        
        # Pre-built Query Tools
        elif tool_name == "search_patient":
            result = search_patient(arguments.get("name"), config)
        elif tool_name == "get_patient_xrays":
            result = get_patient_xrays(
                arguments.get("patient_id"),
                arguments.get("patient_name"),
                config
            )
        elif tool_name == "get_xrays_by_date":
            result = get_xrays_by_date(arguments.get("target_date"), config)
        elif tool_name == "get_xrays_by_type":
            result = get_xrays_by_type(
                arguments.get("xray_type"),
                config,
                arguments.get("limit", 100)
            )
        elif tool_name == "get_xray_info":
            result = get_xray_info(arguments.get("visual_id"), config)
        elif tool_name == "get_recent_xrays":
            result = get_recent_xrays(arguments.get("limit", 50), config)
        elif tool_name == "search_xrays":
            result = search_xrays(
                arguments.get("patient_name"),
                arguments.get("xray_type"),
                arguments.get("start_date"),
                arguments.get("end_date"),
                arguments.get("limit", 100),
                config
            )
        elif tool_name == "get_xray_statistics":
            result = get_xray_statistics(
                arguments.get("start_date"),
                arguments.get("end_date"),
                config
            )
        else:
            result = json.dumps({"error": f"Unknown tool: {tool_name}"})
        
        logger.info(f"Tool {tool_name} completed successfully")
        return {"success": True, "result": json.loads(result)}
        
    except Exception as e:
        logger.error(f"Error handling tool {tool_name}: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


# API Routes

@app.route('/', methods=['GET'])
def root():
    """Root endpoint with server information."""
    return jsonify({
        "service": "DEXIS MCP Server",
        "status": "running",
        "version": "1.0.0",
        "endpoints": {
            "health": "/health",
            "tools": "/tools",
            "mcp": "/mcp"
        },
        "documentation": "See HTTP_MCP_SETUP.md for API documentation"
    }), 200


@app.route('/health', methods=['GET'])
def health():
    """Health check endpoint."""
    return jsonify({"status": "ok", "service": "DEXIS MCP Server"}), 200


@app.route('/tools', methods=['GET'])
def get_tools():
    """Get list of available tools."""
    return jsonify({"tools": TOOLS}), 200


@app.route('/tools/<tool_name>', methods=['GET'])
def get_tool_info(tool_name: str):
    """Get information about a specific tool."""
    tool = next((t for t in TOOLS if t["name"] == tool_name), None)
    if tool:
        return jsonify(tool), 200
    return jsonify({"error": f"Tool '{tool_name}' not found"}), 404


@app.route('/tools/<tool_name>/call', methods=['POST'])
def call_tool(tool_name: str):
    """Call a tool with arguments."""
    try:
        data = request.get_json()
        arguments = data.get("arguments", {}) if data else {}
        
        result = handle_tool_call(tool_name, arguments)
        
        if result.get("success"):
            return jsonify({
                "success": True,
                "tool": tool_name,
                "result": result.get("result")
            }), 200
        else:
            return jsonify({
                "success": False,
                "tool": tool_name,
                "error": result.get("error")
            }), 500
            
    except Exception as e:
        logger.error(f"Error calling tool {tool_name}: {e}", exc_info=True)
        return jsonify({
            "success": False,
            "tool": tool_name,
            "error": str(e)
        }), 500


@app.route('/mcp', methods=['GET', 'POST', 'OPTIONS'])
def mcp_endpoint():
    """MCP protocol endpoint for compatibility."""
    # Log all requests (both to file and console)
    print(f"[MCP] {request.method} request from {request.remote_addr}")
    logger.info(f"MCP endpoint called: {request.method} from {request.remote_addr}")
    logger.info(f"Headers: {dict(request.headers)}")
    print(f"[MCP] Headers: {dict(request.headers)}")
    
    # Handle OPTIONS requests (CORS preflight; Claude/browser clients send Accept + MCP headers)
    if request.method == 'OPTIONS':
        logger.info("Handling OPTIONS request")
        response = jsonify({})
        response.headers.add('Access-Control-Allow-Origin', '*')
        response.headers.add(
            'Access-Control-Allow-Headers',
            'Content-Type, Authorization, Accept, Mcp-Session-Id, Mcp-Protocol-Version, Last-Event-ID',
        )
        response.headers.add('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        return response, 204
    
    # Handle GET requests (for browser testing)
    if request.method == 'GET':
        logger.info("Handling GET request")
        return jsonify({
            "service": "DEXIS MCP Server",
            "endpoint": "/mcp",
            "description": "MCP protocol endpoint",
            "methods": ["POST"],
            "usage": "Send POST requests with JSON-RPC format",
            "example": {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/list",
                "params": {}
            }
        }), 200
    
    # Handle POST requests (actual MCP protocol)
    try:
        data = request.get_json()
        print(f"[MCP] POST request data: {json.dumps(data, indent=2)}")
        logger.info(f"POST request data: {json.dumps(data, indent=2)}")
        
        if not data:
            return jsonify({
                "jsonrpc": "2.0",
                "id": None,
                "error": {
                    "code": -32700,
                    "message": "Parse error: Invalid JSON"
                }
            }), 400
        
        method = data.get("method", "")
        params = data.get("params", {})
        
        logger.info(f"Processing method: {method}")

        # MCP lifecycle notifications (JSON-RPC notification; no id / empty response)
        if isinstance(method, str) and method.startswith("notifications/"):
            logger.info(f"MCP notification acknowledged: {method}")
            return "", 204
        
        # Handle initialization request
        if method == "initialize" or method == "initialize/":
            logger.info("Handling initialize request")
            response = {
                "jsonrpc": "2.0",
                "id": data.get("id"),
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {
                        "tools": {}
                    },
                    "serverInfo": {
                        "name": "dexis-mcp-server",
                        "version": "1.0.0"
                    }
                }
            }
            logger.info(f"Initialize response: {json.dumps(response, indent=2)}")
            return jsonify(response), 200
        
        if method == "tools/list" or method == "tools/list/":
            logger.info("Handling tools/list request")
            response = {
                "jsonrpc": "2.0",
                "id": data.get("id"),
                "result": {
                    "tools": TOOLS
                }
            }
            logger.info(f"Tools list response: {len(TOOLS)} tools")
            return jsonify(response), 200
        
        elif method == "tools/call":
            tool_name = params.get("name", "")
            arguments = params.get("arguments", {})
            
            result = handle_tool_call(tool_name, arguments)
            
            if result.get("success"):
                return jsonify({
                    "jsonrpc": "2.0",
                    "id": data.get("id"),
                    "result": {
                        "content": [
                            {
                                "type": "text",
                                "text": json.dumps(result.get("result"), indent=2)
                            }
                        ]
                    }
                }), 200
            else:
                return jsonify({
                    "jsonrpc": "2.0",
                    "id": data.get("id"),
                    "error": {
                        "code": -32603,
                        "message": result.get("error")
                    }
                }), 500

        # Streamable HTTP clients (e.g. Claude MCP) send ping; HTTP 404 is reserved for "session not found"
        elif method == "ping":
            return jsonify({
                "jsonrpc": "2.0",
                "id": data.get("id"),
                "result": {"status": "pong"},
            }), 200
        
        else:
            # Use HTTP 200 for JSON-RPC errors — 404 is interpreted as session terminated by streamable clients
            return jsonify({
                "jsonrpc": "2.0",
                "id": data.get("id"),
                "error": {
                    "code": -32601,
                    "message": f"Method '{method}' not found"
                }
            }), 200
            
    except Exception as e:
        logger.error(f"Error in MCP endpoint: {e}", exc_info=True)
        return jsonify({
            "jsonrpc": "2.0",
            "id": request.get_json().get("id") if request.is_json else None,
            "error": {
                "code": -32603,
                "message": str(e)
            }
        }), 500


if __name__ == "__main__":
    # Get host/port from environment first, then command line, then defaults
    port = int(os.getenv("MCP_HTTP_PORT", sys.argv[1] if len(sys.argv) > 1 else 8443))
    host = os.getenv("MCP_HTTP_HOST", sys.argv[2] if len(sys.argv) > 2 else "0.0.0.0")
    
    # HTTPS can be explicitly disabled to keep parity with HTTP-only deployments.
    use_https = os.getenv("MCP_USE_HTTPS", "true").lower() == "true"

    # Check for SSL certificates
    cert_file = 'server.crt'
    key_file = 'server.key'
    
    if use_https and os.path.exists(cert_file) and os.path.exists(key_file):
        ssl_context = (cert_file, key_file)
        protocol = "https"
        logger.info(f"Starting DEXIS MCP HTTPS Server on {host}:{port}")
        logger.info(f"Server URL: https://localhost:{port}")
        logger.info(f"MCP endpoint: https://localhost:{port}/mcp")
        logger.info(f"Tools endpoint: https://localhost:{port}/tools")
        logger.info("Using SSL certificates: server.crt, server.key")
    else:
        ssl_context = None
        protocol = "http"
        if use_https:
            logger.warning("SSL certificates not found! Server will run on HTTP (not secure)")
            logger.warning("To enable HTTPS, create SSL certificates: server.crt and server.key")
        else:
            logger.info("MCP_USE_HTTPS=false. Starting DEXIS MCP over HTTP.")
        logger.info(f"Starting DEXIS MCP HTTP Server on {host}:{port}")
        logger.info(f"Server URL: http://localhost:{port}")
        logger.info(f"MCP endpoint: http://localhost:{port}/mcp")
        logger.info(f"Tools endpoint: http://localhost:{port}/tools")
    
    app.run(host=host, port=port, debug=False, ssl_context=ssl_context)

