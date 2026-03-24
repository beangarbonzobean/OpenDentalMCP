"""
MCP Server for DEXIS Database Access
Provides AI assistants with access to DEXIS database via Model Context Protocol.
"""

import asyncio
import json
import logging
import os
import sys
from typing import Any, Dict, Optional

# Try to import MCP SDK
try:
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import Tool, TextContent
    MCP_AVAILABLE = True
except ImportError:
    # Fallback: Use basic stdio implementation if MCP SDK not available
    MCP_AVAILABLE = False
    Server = None
    stdio_server = None
    Tool = None
    TextContent = None

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
        logging.FileHandler('mcp_server.log'),
        logging.StreamHandler(sys.stderr)
    ]
)
logger = logging.getLogger(__name__)


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


# Load config once
config = load_config()


# Define MCP tools
def get_tools():
    """Get list of MCP tools."""
    if not MCP_AVAILABLE:
        return []
    
    return [
        # Schema Discovery Tools
        Tool(
            name="list_tables",
            description="List all tables in the DEXIS database",
            inputSchema={
                "type": "object",
                "properties": {},
                "required": []
            }
        ),
        Tool(
            name="describe_table",
            description="Get column names, types, and structure for a table",
            inputSchema={
                "type": "object",
                "properties": {
                    "table_name": {
                        "type": "string",
                        "description": "Name of the table to describe"
                    }
                },
                "required": ["table_name"]
            }
        ),
        Tool(
            name="list_columns",
            description="Get all columns for a specific table",
            inputSchema={
                "type": "object",
                "properties": {
                    "table_name": {
                        "type": "string",
                        "description": "Name of the table"
                    }
                },
                "required": ["table_name"]
            }
        ),
        
        # Query Execution Tools
        Tool(
            name="execute_query",
            description="Execute read-only SELECT query with safety checks",
            inputSchema={
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
        ),
        
        # Pre-built Query Tools
        Tool(
            name="search_patient",
            description="Find patients by name (first, last, or full name)",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Patient name to search for"
                    }
                },
                "required": ["name"]
            }
        ),
        Tool(
            name="get_patient_xrays",
            description="Get all x-rays for a patient ID or name",
            inputSchema={
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
        ),
        Tool(
            name="get_xrays_by_date",
            description="Get x-rays taken on a specific date",
            inputSchema={
                "type": "object",
                "properties": {
                    "target_date": {
                        "type": "string",
                        "description": "Date in format 'YYYY-MM-DD'"
                    }
                },
                "required": ["target_date"]
            }
        ),
        Tool(
            name="get_xrays_by_type",
            description="Get x-rays by type (Periapical, Bitewing, Panoramic, etc.)",
            inputSchema={
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
        ),
        Tool(
            name="get_xray_info",
            description="Get detailed info for a specific x-ray by VisualID",
            inputSchema={
                "type": "object",
                "properties": {
                    "visual_id": {
                        "type": "integer",
                        "description": "VisualID of the x-ray"
                    }
                },
                "required": ["visual_id"]
            }
        ),
        Tool(
            name="get_recent_xrays",
            description="Get most recent x-rays",
            inputSchema={
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
        ),
        Tool(
            name="search_xrays",
            description="Advanced search with multiple filters (date range, type, patient, etc.)",
            inputSchema={
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
        ),
        Tool(
            name="get_xray_statistics",
            description="Get statistics (counts by type, date ranges, etc.)",
            inputSchema={
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
        )
    ]


# Tool handler functions
async def handle_tool_call(tool_name: str, arguments: Dict[str, Any]):
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
        
        # Return in MCP format if available, otherwise return raw result
        if MCP_AVAILABLE and TextContent:
            return [TextContent(type="text", text=result)]
        else:
            return result
        
    except Exception as e:
        logger.error(f"Error handling tool {tool_name}: {e}", exc_info=True)
        error_result = json.dumps({"error": str(e)})
        
        # Return in MCP format if available, otherwise return raw result
        if MCP_AVAILABLE and TextContent:
            return [TextContent(type="text", text=error_result)]
        else:
            return error_result


# Main server implementation
async def main():
    """Main MCP server entry point."""
    if not MCP_AVAILABLE:
        # Fallback: Basic stdio implementation
        logger.error("MCP SDK not available. Please install: pip install mcp")
        logger.info("Attempting basic stdio implementation...")
        
        # Simple stdio handler
        async def handle_stdio():
            while True:
                try:
                    line = await asyncio.get_event_loop().run_in_executor(
                        None, sys.stdin.readline
                    )
                    if not line:
                        break
                    
                    # Parse JSON-RPC request
                    try:
                        request = json.loads(line.strip())
                        if request.get("method") == "tools/call":
                            tool_name = request["params"]["name"]
                            arguments = request["params"].get("arguments", {})
                            
                            # Handle tool call
                            results = await handle_tool_call(tool_name, arguments)
                            
                            # Format results for JSON-RPC response
                            if isinstance(results, str):
                                # Raw result string, wrap in TextContent format
                                content = [{"type": "text", "text": results}]
                            else:
                                # Already in MCP format (list of TextContent)
                                content = [{"type": "text", "text": r.text} if hasattr(r, "text") else r for r in results]
                            
                            # Send response
                            response = {
                                "jsonrpc": "2.0",
                                "id": request.get("id"),
                                "result": {
                                    "content": content
                                }
                            }
                            print(json.dumps(response), flush=True)
                    except json.JSONDecodeError:
                        continue
                    except Exception as e:
                        logger.error(f"Error processing request: {e}")
                        error_response = {
                            "jsonrpc": "2.0",
                            "id": request.get("id") if 'request' in locals() else None,
                            "error": {
                                "code": -32603,
                                "message": str(e)
                            }
                        }
                        print(json.dumps(error_response), flush=True)
                except Exception as e:
                    logger.error(f"Error in stdio handler: {e}")
                    break
        
        await handle_stdio()
        return
    
    # Use MCP SDK
    server = Server("dexis-mcp-server")
    
    # Register tools
    @server.list_tools()
    async def list_tools():
        return get_tools()
    
    @server.call_tool()
    async def call_tool(name: str, arguments: Dict[str, Any]):
        return await handle_tool_call(name, arguments)
    
    # Run server with stdio transport
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options()
        )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Server stopped by user")
    except Exception as e:
        logger.error(f"Server error: {e}", exc_info=True)
        sys.exit(1)

