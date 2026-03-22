#!/usr/bin/env python3
"""
Check Open Dental MCP Server Tools
Verifies that the server is exposing all expected tools including smart_query and query_database
"""

import requests
import json
import sys

def check_tools():
    """Check what tools are available from the MCP server"""
    try:
        # Make a tools/list request to the MCP server
        url = "https://localhost:8444/mcp"
        
        request_data = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/list",
            "params": {}
        }
        
        print("Connecting to OpenDental MCP server at https://localhost:8444...")
        # Disable SSL warnings
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        
        response = requests.post(
            url,
            json=request_data,
            verify=False,  # Skip SSL verification for self-signed cert
            timeout=10
        )
        
        response.raise_for_status()
        result = response.json()
        
        if "result" in result and "tools" in result["result"]:
            tools = result["result"]["tools"]
            tool_names = [tool["name"] for tool in tools]
            
            print(f"\n[OK] Server is responding")
            print(f"[OK] Total tools available: {len(tools)}")
            print(f"\nTool names:")
            for name in sorted(tool_names):
                print(f"  - {name}")
            
            # Check for specific tools
            print(f"\n--- Verification ---")
            has_smart_query = "smart_query" in tool_names
            has_query_database = "query_database" in tool_names
            
            if has_smart_query:
                print("[OK] smart_query tool is available")
            else:
                print("[FAIL] smart_query tool is NOT available")
            
            if has_query_database:
                print("[OK] query_database tool is available")
            else:
                print("[FAIL] query_database tool is NOT available")
            
            if has_smart_query and has_query_database:
                print(f"\n[SUCCESS] All expected tools are available!")
                return 0
            else:
                print(f"\n[FAILURE] Some expected tools are missing")
                return 1
        else:
            print("[ERROR] Unexpected response format")
            print(json.dumps(result, indent=2))
            return 1
            
    except requests.exceptions.SSLError as e:
        print(f"[ERROR] SSL Error: {e}")
        print("Note: This is expected with self-signed certificates")
        print("Trying with SSL verification disabled...")
        return check_tools()
    except requests.exceptions.RequestException as e:
        print(f"[ERROR] Connection error: {e}")
        print("\nMake sure the server is running:")
        print("  Get-Service -Name 'OpenDentalMCPServer'")
        return 1
    except Exception as e:
        print(f"[ERROR] Error: {e}")
        import traceback
        traceback.print_exc()
        return 1

if __name__ == "__main__":
    sys.exit(check_tools())

