# Fixing Port Conflict Between MCP Servers

## Problem

Both DEXIS and OpenDental MCP servers are trying to use port 8443, causing a conflict.

## Solution

Change the OpenDental MCP server to use port 8444 (or any other available port).

## Quick Fix

Run the automated fix script:

```powershell
# On your Windows server, as Administrator
.\fix_port_conflict.ps1
```

This script will:
1. Stop the OpenDental MCP Server service
2. Update it to use port 8444
3. Restart the service
4. Verify the configuration

## Manual Fix

If you prefer to fix it manually:

### Step 1: Stop OpenDental Service

```powershell
Stop-Service OpenDentalMCPServer
```

### Step 2: Update Service Configuration

Using NSSM:

```powershell
# Get current configuration
C:\Program Files\nssm\nssm.exe get OpenDentalMCPServer AppParameters

# Update to use port 8444
# Replace [SCRIPT_PATH] with actual path to your OpenDental MCP server script
C:\Program Files\nssm\nssm.exe set OpenDentalMCPServer AppParameters "[SCRIPT_PATH] 8444"
```

**Example:**
```powershell
C:\Program Files\nssm\nssm.exe set OpenDentalMCPServer AppParameters "C:\OpenDentalMonitor\mcp_server_http.py 8444"
```

### Step 3: Start Service

```powershell
Start-Service OpenDentalMCPServer
```

### Step 4: Verify

```powershell
# Check service status
Get-Service OpenDentalMCPServer

# Test the server
Invoke-WebRequest -Uri "https://localhost:8444/health" -SkipCertificateCheck
```

## Update Cloudflare Tunnel Config

If you're using Cloudflare Tunnel, update `C:\cloudflared\config.yml`:

```yaml
tunnel: mcp-servers
credentials-file: C:\cloudflared\[TUNNEL_ID].json

ingress:
  # DEXIS MCP Server
  - hostname: dexis-mcp.yourdomain.com
    service: https://localhost:8443
  
  # OpenDental MCP Server
  - hostname: opendental-mcp.yourdomain.com
    service: https://localhost:8444
  
  # Catch-all (must be last)
  - service: http_status:404
```

Then restart the Cloudflare tunnel:

```powershell
Restart-Service CloudflareTunnel
```

## Verify Both Servers Are Working

```powershell
# Test DEXIS MCP Server (port 8443)
Invoke-WebRequest -Uri "https://localhost:8443/health" -SkipCertificateCheck

# Test OpenDental MCP Server (port 8444)
Invoke-WebRequest -Uri "https://localhost:8444/health" -SkipCertificateCheck
```

Both should return `{"status": "healthy", ...}`

## Service Names

- **DEXIS MCP Server**: `DEXISMCPHTTPServer` (port 8443)
- **OpenDental MCP Server**: `OpenDentalMCPServer` (port 8444)

## Check Port Usage

To see what's using each port:

```powershell
# Check port 8443
Get-NetTCPConnection -LocalPort 8443 -ErrorAction SilentlyContinue | Select-Object LocalAddress, LocalPort, State, OwningProcess

# Check port 8444
Get-NetTCPConnection -LocalPort 8444 -ErrorAction SilentlyContinue | Select-Object LocalAddress, LocalPort, State, OwningProcess
```

## Troubleshooting

### Service Won't Start

1. **Check logs:**
   ```powershell
   Get-Content C:\OpenDentalMonitor\OpenDentalMCPServer_error.log -Tail 50
   ```

2. **Test manually:**
   ```powershell
   cd C:\OpenDentalMonitor
   python mcp_server_http.py 8444
   ```

3. **Check if port is in use:**
   ```powershell
   Get-NetTCPConnection -LocalPort 8444 -ErrorAction SilentlyContinue
   ```

### Port Still in Use

If port 8444 is already in use, choose a different port (e.g., 8445, 8446):

```powershell
# Update to use port 8445
C:\Program Files\nssm\nssm.exe set OpenDentalMCPServer AppParameters "[SCRIPT_PATH] 8445"
```

### Service Configuration Not Updating

1. **Use NSSM GUI:**
   ```powershell
   C:\Program Files\nssm\nssm.exe edit OpenDentalMCPServer
   ```
   - Go to "Application" tab
   - Update "Arguments" field to include the port number
   - Click "Install service" or "Edit service"

2. **Or reinstall the service:**
   ```powershell
   # Stop and remove
   Stop-Service OpenDentalMCPServer
   C:\Program Files\nssm\nssm.exe remove OpenDentalMCPServer confirm
   
   # Reinstall with correct port
   C:\Program Files\nssm\nssm.exe install OpenDentalMCPServer python.exe
   C:\Program Files\nssm\nssm.exe set OpenDentalMCPServer AppParameters "C:\OpenDentalMonitor\mcp_server_http.py 8444"
   # ... (set other parameters)
   ```

## Summary

**Quick Fix:**
```powershell
.\fix_port_conflict.ps1
```

**Manual Fix:**
1. Stop service: `Stop-Service OpenDentalMCPServer`
2. Update port: `nssm set OpenDentalMCPServer AppParameters "[SCRIPT] 8444"`
3. Start service: `Start-Service OpenDentalMCPServer`
4. Update Cloudflare config if needed
5. Test both servers

**Final Configuration:**
- DEXIS: Port 8443
- OpenDental: Port 8444

