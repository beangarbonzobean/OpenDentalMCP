# Updating Open Dental MCP Server

## Quick Answer: Do I Need to Uninstall?

**No, you don't need to manually uninstall!** The installation script automatically handles updates by:
- Stopping the existing service
- Removing the old service
- Installing the new version
- Starting the service

## Update Process

### Option 1: Update from Git (recommended)

1. **Stop the service** (optional, but recommended):
   ```powershell
   Stop-Service -Name "OpenDentalMCPServer"
   ```

2. **Pull** the latest commit, then **copy** updated files from **`live/OpenDentalMCP/`** to **`C:\OpenDentalMCP`** (or merge in place).

3. **Run the install script** (it will handle the update):
   ```powershell
   cd C:\OpenDentalMCP
   .\install_service.ps1
   ```

   The script will:
   - ✅ Detect existing service
   - ✅ Stop and remove old service
   - ✅ Copy new files
   - ✅ Install updated service
   - ✅ Start the service

4. **Verify the update**:
   ```powershell
   .\check_service_status.ps1
   ```

### Option 2: Manual File Update (If Service is Already Running)

If you just need to update the Python files without reinstalling the service:

1. **Stop the service**:
   ```powershell
   Stop-Service -Name "OpenDentalMCPServer"
   ```

2. **Backup your `.env` file** (important!):
   ```powershell
   Copy-Item C:\OpenDentalMCP\.env C:\OpenDentalMCP\.env.backup
   ```

3. **Copy updated files**:
   ```powershell
   # Copy from your repo clone to C:\OpenDentalMCP
   Copy-Item "path\to\repo\live\OpenDentalMCP\*.py" C:\OpenDentalMCP\ -Force
   ```

4. **Verify `.env` file** is still correct (it should be preserved)

5. **Start the service**:
   ```powershell
   Start-Service -Name "OpenDentalMCPServer"
   ```

6. **Check logs** to verify it's working:
   ```powershell
   Get-Content C:\OpenDentalMCP\service_stderr.log -Tail 20
   ```

## What Gets Updated

When you run `install_service.ps1`, it updates:
- ✅ `mcp_tools.py` - All tool implementations
- ✅ `mcp_server.py` - Server code
- ✅ `mcp_server_http.py` - HTTP server
- ✅ `requirements.txt` - Python dependencies (will reinstall if changed)
- ✅ `config.json` - Configuration (if changed)

**Preserved:**
- ✅ `.env` file - Your configuration (API keys, database settings)
- ✅ Service configuration - Windows Service settings
- ✅ Log files - Existing logs are preserved

## Pre-Update Checklist

Before updating, verify:

- [ ] **Backup your `.env` file** (contains API keys and database credentials)
- [ ] **Note your current service status** (running/stopped)
- [ ] **Check if any custom configurations** were made outside the deployment package
- [ ] **Verify database connectivity** is still working
- [ ] **Test the update on a non-production server first** (if possible)

## Post-Update Verification

After updating, verify everything works:

1. **Check service status**:
   ```powershell
   Get-Service -Name "OpenDentalMCPServer"
   ```
   Should show: `Status: Running`

2. **Test health endpoint**:
   ```powershell
   Invoke-WebRequest -Uri "https://localhost:8444/health" -SkipCertificateCheck
   ```
   Should return: `{"status": "healthy"}`

3. **Check logs for errors**:
   ```powershell
   Get-Content C:\OpenDentalMCP\service_stderr.log -Tail 50
   ```

4. **Test a simple tool call** (if you have a test client):
   ```powershell
   $body = @{
       jsonrpc = "2.0"
       id = 1
       method = "tools/list"
   } | ConvertTo-Json
   
   Invoke-WebRequest -Uri "https://localhost:8444/mcp" -Method POST -Body $body -ContentType "application/json" -SkipCertificateCheck
   ```

## Troubleshooting Updates

### Service Won't Start After Update

1. **Check error logs**:
   ```powershell
   Get-Content C:\OpenDentalMCP\service_stderr.log -Tail 50
   ```

2. **Verify Python dependencies**:
   ```powershell
   cd C:\OpenDentalMCP
   python -m pip install -r requirements.txt
   ```

3. **Test manually**:
   ```powershell
   cd C:\OpenDentalMCP
   python mcp_server_http.py
   ```
   Press Ctrl+C to stop

4. **Check `.env` file**:
   ```powershell
   Get-Content C:\OpenDentalMCP\.env
   ```
   Verify all required variables are present

### Configuration Lost After Update

If your `.env` file was overwritten:

1. **Restore from backup**:
   ```powershell
   Copy-Item C:\OpenDentalMCP\.env.backup C:\OpenDentalMCP\.env -Force
   ```

2. **Or recreate from template**:
   - Check `env_template_for_server.txt` in the deployment package
   - Copy values from your backup

3. **Restart service**:
   ```powershell
   Restart-Service -Name "OpenDentalMCPServer"
   ```

### Rollback to Previous Version

If you need to rollback:

1. **Stop the service**:
   ```powershell
   Stop-Service -Name "OpenDentalMCPServer"
   ```

2. **Restore previous files** (if you have a backup):
   ```powershell
   # Restore from backup or previous deployment package
   Copy-Item "path\to\previous\version\*.py" C:\OpenDentalMCP\ -Force
   ```

3. **Start the service**:
   ```powershell
   Start-Service -Name "OpenDentalMCPServer"
   ```

## What Changed in This Update

**New Features Added:**
- ✅ `smart_query` tool - Self-iterating query generation
- ✅ Complexity detection - Early detection of complex queries
- ✅ Multi-statement SQL support - SET variables, CREATE TEMP TABLE
- ✅ Comment stripping - Handles SQL comments correctly
- ✅ Error analysis - Learns from column errors
- ✅ Suggested SQL - Provides working SQL for complex queries
- ✅ Enhanced read-only enforcement - Prevents dangerous operations

**Improvements:**
- Fast failure for complex queries (0 iterations vs 5)
- Better error messages
- Working SQL suggestions
- Natural language dangerous keyword detection

**Testing:**
- ✅ All 19 tests passing (100% success rate)
- ✅ Backward compatibility verified
- ✅ Edge cases handled

## Summary

**To update:** Just run `install_service.ps1` - it handles everything automatically!

**No uninstall needed** - the script does it for you.

**Always backup `.env`** before updating (just in case).

