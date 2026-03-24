#!/usr/bin/env python3
"""
Open Dental MCP Server (stdio version)
Provides MCP API access to Open Dental REST API for AI agents
Compatible with Claude Desktop and other stdio-based MCP clients
"""

import json
import sys
import os
import logging
from typing import Any, Dict, List, Optional
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent))

from mcp_tools import OpenDentalMCPTools
try:
    from mcp_tools_optimized import OptimizedOpenDentalMCPTools, get_discovery_tools
except ImportError:
    OptimizedOpenDentalMCPTools = None

    def get_discovery_tools():
        return []

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('opendental_mcp.log'),
        logging.StreamHandler(sys.stderr)
    ]
)
logger = logging.getLogger(__name__)


class OpenDentalMCPServer:
    """MCP Server for Open Dental API"""
    
    def __init__(self, use_optimized: bool = True):
        if use_optimized and OptimizedOpenDentalMCPTools is not None:
            self.tools = OptimizedOpenDentalMCPTools()
        else:
            self.tools = OpenDentalMCPTools()
        self.request_id = None
        
    def send_response(self, result: Any = None, error: Optional[Dict] = None):
        """Send JSON-RPC response"""
        # Ensure id is never None - use 0 as default if not set
        response_id = self.request_id if self.request_id is not None else 0
        
        response = {
            "jsonrpc": "2.0",
            "id": response_id
        }
        
        if error:
            response["error"] = error
        else:
            response["result"] = result
            
        print(json.dumps(response), flush=True)
        
    def handle_request(self, request: Dict[str, Any]):
        """Handle incoming JSON-RPC request"""
        try:
            self.request_id = request.get("id")
            method = request.get("method")
            params = request.get("params", {})
            
            logger.debug(f"Received request: {method} with params: {params}")
            
            # Handle initialize method (required for Claude Desktop)
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
                        "message": f"Method not found: {method}"
                    }
                )
                
        except Exception as e:
            logger.error(f"Error handling request: {e}", exc_info=True)
            self.send_response(
                error={
                    "code": -32603,
                    "message": f"Internal error: {str(e)}"
                }
            )
    
    def handle_initialize(self, params: Dict):
        """Handle initialize request"""
        self.send_response({
            "protocolVersion": "2024-11-05",
            "capabilities": {
                "tools": {}
            },
            "serverInfo": {
                "name": "opendental-mcp",
                "version": "1.0.0"
            }
        })
    
    def handle_tools_list(self):
        """List available tools"""
        # Get base tools
        tools = self.tools.list_tools()
        
        # Add discovery tools
        discovery_tools = get_discovery_tools()
        tools.extend(discovery_tools)
        
        self.send_response({"tools": tools})
    
    def handle_tool_call(self, params: Dict):
        """Call a tool"""
        tool_name = params.get("name")
        arguments = params.get("arguments", {})
        
        try:
            # Handle discovery tools
            if tool_name == "list_tool_categories":
                if isinstance(self.tools, OptimizedOpenDentalMCPTools):
                    result = self.tools.list_tool_categories()
                else:
                    result = {"error": "Optimized tools not enabled"}
            elif tool_name == "search_tools":
                if isinstance(self.tools, OptimizedOpenDentalMCPTools):
                    query = arguments.get("query", "")
                    result = self.tools.search_tools(query)
                else:
                    result = {"error": "Optimized tools not enabled"}
            elif tool_name == "get_tool_suggestions":
                if isinstance(self.tools, OptimizedOpenDentalMCPTools):
                    intent = arguments.get("intent", "")
                    result = self.tools.get_tool_suggestions(intent)
                else:
                    result = {"error": "Optimized tools not enabled"}
            elif tool_name == "get_cache_stats":
                if isinstance(self.tools, OptimizedOpenDentalMCPTools):
                    result = self.tools.get_cache_stats()
                else:
                    result = {"error": "Optimized tools not enabled"}
            elif tool_name == "clear_cache":
                if isinstance(self.tools, OptimizedOpenDentalMCPTools):
                    self.tools.clear_cache()
                    result = {"success": True, "message": "Cache cleared"}
                else:
                    result = {"error": "Optimized tools not enabled"}
            else:
                # Call regular tool
                result = self.tools.call_tool(tool_name, arguments)
            
            self.send_response({
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(result, indent=2)
                    }
                ]
            })
        except Exception as e:
            logger.error(f"Error calling tool {tool_name}: {e}", exc_info=True)
            self.send_response(
                error={
                    "code": -32603,
                    "message": f"Tool execution error: {str(e)}"
                }
            )


def main():
    """Main entry point"""
    server = OpenDentalMCPServer()
    
    logger.info("Open Dental MCP Server starting (stdio mode)...")
    
    # Read from stdin line by line
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
            
        try:
            request = json.loads(line)
            server.handle_request(request)
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON: {e}")
            continue
        except Exception as e:
            logger.error(f"Unexpected error: {e}", exc_info=True)
            continue


if __name__ == "__main__":
    main()

