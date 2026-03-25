#!/usr/bin/env python3
"""
DEXIS MCP Server (stdio version)
Provides AI assistants with access to the DEXIS database via Model Context Protocol.
Compatible with Claude Desktop and other stdio-based MCP clients.
"""

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent))

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
    get_xray_statistics,
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("dexis_mcp.log"),
        logging.StreamHandler(sys.stderr),
    ],
)
logger = logging.getLogger(__name__)


def load_config() -> Dict:
    """Load configuration from MCP_CONFIG_FILE or config.json."""
    config_file = os.getenv("MCP_CONFIG_FILE", "config.json")
    try:
        with open(config_file, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except FileNotFoundError:
        logger.warning("%s not found. Using defaults.", config_file)
        return {
            "database": {
                "server": "(local)\\DEXIS_DATA",
                "database": "DEXIS",
                "use_windows_auth": True,
            }
        }


config = load_config()


class DexisMCPServer:
    """MCP Server for DEXIS database tools."""

    def __init__(self):
        self.request_id = None

    def send_response(self, result: Any = None, error: Optional[Dict] = None):
        """Build JSON-RPC 2.0 response dict; print as JSON to stdout with flush=True."""
        response_id = self.request_id if self.request_id is not None else 0
        response: Dict[str, Any] = {"jsonrpc": "2.0", "id": response_id}
        if error:
            response["error"] = error
        else:
            response["result"] = result
        print(json.dumps(response), flush=True)

    def handle_request(self, request: Dict[str, Any]):
        """Dispatch on method: initialize, tools/list, tools/call, ping."""
        try:
            self.request_id = request.get("id")
            method = request.get("method")
            params = request.get("params", {})

            logger.debug("Received request: %s with params: %s", method, params)

            if method == "initialize":
                self.handle_initialize(params)
            elif method == "tools/list":
                self.handle_tools_list()
            elif method == "tools/call":
                self.handle_tool_call(params)
            elif method == "ping":
                self.send_response({"status": "pong"})
            else:
                self.send_response(
                    error={
                        "code": -32601,
                        "message": f"Method not found: {method}",
                    }
                )
        except Exception as e:
            logger.error("Error handling request: %s", e, exc_info=True)
            self.send_response(
                error={"code": -32603, "message": f"Internal error: {str(e)}"}
            )

    def handle_initialize(self, params: Dict):
        """Return protocolVersion, capabilities, serverInfo."""
        self.send_response(
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "dexis-mcp", "version": "1.0.0"},
            }
        )

    def handle_tools_list(self):
        """Return tools as plain dicts (name, description, inputSchema)."""
        tools = [
            {
                "name": "list_tables",
                "description": "List all tables in the DEXIS database",
                "inputSchema": {"type": "object", "properties": {}, "required": []},
            },
            {
                "name": "describe_table",
                "description": "Get column names, types, and structure for a table",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "table_name": {
                            "type": "string",
                            "description": "Name of the table to describe",
                        }
                    },
                    "required": ["table_name"],
                },
            },
            {
                "name": "list_columns",
                "description": "Get all columns for a specific table",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "table_name": {
                            "type": "string",
                            "description": "Name of the table",
                        }
                    },
                    "required": ["table_name"],
                },
            },
            {
                "name": "execute_query",
                "description": "Execute read-only SELECT query with safety checks",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "SQL SELECT query to execute",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Maximum number of rows to return (default: 1000)",
                            "default": 1000,
                        },
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "search_patient",
                "description": "Find patients by name (first, last, or full name)",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Patient name to search for",
                        }
                    },
                    "required": ["name"],
                },
            },
            {
                "name": "get_patient_xrays",
                "description": "Get all x-rays for a patient ID or name",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "patient_id": {
                            "type": "integer",
                            "description": "Patient PersonID",
                        },
                        "patient_name": {
                            "type": "string",
                            "description": "Patient name (first, last, or full)",
                        },
                    },
                    "required": [],
                },
            },
            {
                "name": "get_xrays_by_date",
                "description": "Get x-rays taken on a specific date",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "target_date": {
                            "type": "string",
                            "description": "Date in format 'YYYY-MM-DD'",
                        }
                    },
                    "required": ["target_date"],
                },
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
                            "enum": [
                                "Periapical",
                                "Bitewing",
                                "Panoramic",
                                "Intraoral Photo",
                                "Extraoral Photo",
                            ],
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Maximum number of results (default: 100)",
                            "default": 100,
                        },
                    },
                    "required": ["xray_type"],
                },
            },
            {
                "name": "get_xray_info",
                "description": "Get detailed info for a specific x-ray by VisualID",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "visual_id": {
                            "type": "integer",
                            "description": "VisualID of the x-ray",
                        }
                    },
                    "required": ["visual_id"],
                },
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
                            "default": 50,
                        }
                    },
                    "required": [],
                },
            },
            {
                "name": "search_xrays",
                "description": "Advanced search with multiple filters (date range, type, patient, etc.)",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "patient_name": {
                            "type": "string",
                            "description": "Patient name filter",
                        },
                        "xray_type": {
                            "type": "string",
                            "description": "X-ray type filter",
                            "enum": [
                                "Periapical",
                                "Bitewing",
                                "Panoramic",
                                "Intraoral Photo",
                                "Extraoral Photo",
                            ],
                        },
                        "start_date": {
                            "type": "string",
                            "description": "Start date (YYYY-MM-DD)",
                        },
                        "end_date": {
                            "type": "string",
                            "description": "End date (YYYY-MM-DD)",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Maximum number of results (default: 100)",
                            "default": 100,
                        },
                    },
                    "required": [],
                },
            },
            {
                "name": "get_xray_statistics",
                "description": "Get statistics (counts by type, date ranges, etc.)",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "start_date": {
                            "type": "string",
                            "description": "Start date (YYYY-MM-DD)",
                        },
                        "end_date": {
                            "type": "string",
                            "description": "End date (YYYY-MM-DD)",
                        },
                    },
                    "required": [],
                },
            },
        ]
        self.send_response({"tools": tools})

    def handle_tool_call(self, params: Dict):
        """Dispatch to mcp_tools; wrap result in MCP content or return tool error."""
        tool_name = params.get("name")
        arguments = params.get("arguments", {})

        try:
            if tool_name == "list_tables":
                result = list_tables(config)
            elif tool_name == "describe_table":
                result = describe_table(arguments.get("table_name"), config)
            elif tool_name == "list_columns":
                result = list_columns(arguments.get("table_name"), config)
            elif tool_name == "execute_query":
                result = execute_query(
                    arguments.get("query"),
                    config,
                    arguments.get("limit", 1000),
                )
            elif tool_name == "search_patient":
                result = search_patient(arguments.get("name"), config)
            elif tool_name == "get_patient_xrays":
                result = get_patient_xrays(
                    arguments.get("patient_id"),
                    arguments.get("patient_name"),
                    config,
                )
            elif tool_name == "get_xrays_by_date":
                result = get_xrays_by_date(arguments.get("target_date"), config)
            elif tool_name == "get_xrays_by_type":
                result = get_xrays_by_type(
                    arguments.get("xray_type"),
                    config,
                    arguments.get("limit", 100),
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
                    config,
                )
            elif tool_name == "get_xray_statistics":
                result = get_xray_statistics(
                    arguments.get("start_date"),
                    arguments.get("end_date"),
                    config,
                )
            else:
                self.send_response(
                    error={
                        "code": -32601,
                        "message": f"Unknown tool: {tool_name}",
                    }
                )
                return

            if isinstance(result, str):
                text = result
            else:
                text = json.dumps(result, indent=2, default=str)

            self.send_response(
                {
                    "content": [
                        {
                            "type": "text",
                            "text": text,
                        }
                    ]
                }
            )
        except Exception as e:
            logger.error("Error calling tool %s: %s", tool_name, e, exc_info=True)
            self.send_response(
                error={
                    "code": -32603,
                    "message": f"Tool execution error: {str(e)}",
                }
            )


def main():
    server = DexisMCPServer()
    logger.info("DEXIS MCP Server starting (stdio mode)...")

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
            server.handle_request(request)
        except json.JSONDecodeError as e:
            logger.error("Invalid JSON: %s", e)
        except Exception as e:
            logger.error("Unexpected error: %s", e, exc_info=True)


if __name__ == "__main__":
    main()
