# Open Dental MCP Server - Windows Service Setup Guide

## Overview

This guide will help you install the Open Dental MCP Server as a Windows Service so it can run 24/7.

## Prerequisites

✅ **NSSM** - Already installed via winget  
✅ **Python** - Already installed  
✅ **Database Configuration** - Already configured in `.env`  
✅ **Open Dental API Keys** - Already configured in `.env`

## Installation Steps

### Step 1: Run PowerShell as Administrator

**IMPORTANT:** You must run PowerShell as Administrator to install Windows Services.

1. Press `Windows Key + X`
2. Select **"Windows PowerShell (Admin)"** or **"Terminal (Admin)"**
3. Click **"Yes"** when prompted by User Account Control

### Step 2: Navigate to the MCP Directory

```powershell
cd "C:\Users\frontofc\Desktop\Cursor\Dental AI Agent\DentalAIAgent\opendental-mcp"
```

### Step 3: Run the Installation Script

```powershell
.\install_service.ps1
```

The script will:
1. ✅ Find NSSM (already installed)
2. ✅ Create installation directory: `C:\OpenDentalMCP`
3. ✅ Copy all Python files and `.env` configuration
4. ✅ Install Python dependencies
5. ✅ Install the Windows Service
6. ✅ Configure environment variables from `.env`
7. ✅ Start the service

### Step 4: Verify Installation

After installation, check the service status:

```powershell
.\check_service_status.ps1
```

Or manually check:

```powershell
Get-Service -Name "OpenDentalMCPServer"
```

## Service Details

- **Service Name:** `OpenDentalMCPServer`
- **Display Name:** `Open Dental MCP Server`
- **Installation Path:** `C:\OpenDentalMCP`
- **HTTP Server:** `https://localhost:8444`
- **Health Check:** `https://localhost:8444/health`
- **MCP Endpoint:** `https://localhost:8444/mcp`

## Service Management

### Check Service Status

```powershell
Get-Service -Name "OpenDentalMCPServer"
```

### Start Service

```powershell
Start-Service -Name "OpenDentalMCPServer"
```

### Stop Service

```powershell
Stop-Service -Name "OpenDentalMCPServer"
```

### Restart Service

```powershell
Restart-Service -Name "OpenDentalMCPServer"
```

### View Service Logs

```powershell
# Standard output
Get-Content C:\OpenDentalMCP\service_stdout.log -Tail 50

# Error output
Get-Content C:\OpenDentalMCP\service_stderr.log -Tail 50

# Application log
Get-Content C:\OpenDentalMCP\opendental_mcp_http.log -Tail 50
```

## Uninstall Service

If you need to remove the service:

```powershell
.\uninstall_service.ps1
```

## Troubleshooting

### Service Won't Start

1. **Check logs:**
   ```powershell
   Get-Content C:\OpenDentalMCP\service_stderr.log -Tail 50
   ```

2. **Check if port 8444 is in use:**
   ```powershell
   netstat -ano | findstr :8444
   ```

3. **Verify environment variables:**
   ```powershell
   Get-Content C:\OpenDentalMCP\.env
   ```

4. **Test the server manually:**
   ```powershell
   cd C:\OpenDentalMCP
   python mcp_server_http.py
   ```

### Service Stops Unexpectedly

1. Check Windows Event Viewer:
   - Open Event Viewer
   - Navigate to: Windows Logs → Application
   - Look for errors related to `OpenDentalMCPServer`

2. Check service logs (see above)

3. Verify database connection:
   ```powershell
   cd C:\OpenDentalMCP
   python test_db_connection.py
   ```

### Port Already in Use

If port 8444 is already in use, you can change it:

1. Edit `C:\OpenDentalMCP\.env`:
   ```env
   MCP_HTTP_PORT=8444
   ```

2. Restart the service:
   ```powershell
   Restart-Service -Name "OpenDentalMCPServer"
   ```

### Environment Variables Not Loading

The service loads environment variables from `C:\OpenDentalMCP\.env`. If variables aren't loading:

1. Verify the `.env` file exists:
   ```powershell
   Test-Path C:\OpenDentalMCP\.env
   ```

2. Check the file format (no spaces around `=`, no quotes unless needed):
   ```env
   OPENDENTAL_DEVELOPER_KEY=your_key
   OPENDENTAL_DB_TYPE=mysql
   ```

3. Restart the service after editing `.env`:
   ```powershell
   Restart-Service -Name "OpenDentalMCPServer"
   ```

## Testing the Service

### Health Check

```powershell
Invoke-WebRequest -Uri "https://localhost:8444/health" -SkipCertificateCheck
```

### Test MCP Endpoint

```powershell
$body = @{
    jsonrpc = "2.0"
    id = 1
    method = "tools/list"
} | ConvertTo-Json

Invoke-WebRequest -Uri "https://localhost:8444/mcp" -Method POST -Body $body -ContentType "application/json" -SkipCertificateCheck
```

## Service Configuration

The service is configured with:
- **Auto-start:** Service starts automatically on boot
- **Log rotation:** Logs rotate daily or when they reach 10MB
- **Working directory:** `C:\OpenDentalMCP`
- **Python path:** Automatically detected

## Next Steps

Once the service is running:

1. ✅ Test the health endpoint
2. ✅ Test the MCP endpoint
3. ✅ Configure your AI agent to use `https://localhost:8444/mcp`
4. ✅ Monitor logs for any issues

## Support

If you encounter issues:
1. Check the logs first
2. Verify all prerequisites are installed
3. Ensure you ran PowerShell as Administrator
4. Check that the database connection is working

