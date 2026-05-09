# DEXIS Services Diagnostics

This document provides guidance on diagnosing and troubleshooting DEXIS MCP services.

## Quick Service Verification

Run the diagnostic script to check service status and logs:

```powershell
.\verify_services.ps1
```

This script will:
- ✓ Verify NSSM installation
- ✓ Check service status (running/stopped)
- ✓ Display NSSM configuration (paths, logs)
- ✓ Show recent log entries
- ✓ Identify common issues

## Installed Services

The DEXIS MCP installation creates two Windows services:

### 1. DEXISXRayMonitor
- **Purpose**: Monitors DEXIS Imaging Suite folder for new x-ray images
- **Script**: `xray_monitor.py`
- **NSSM Logs**:
  - `C:\DEXISMonitor\DEXISXRayMonitor_output.log` (stdout)
  - `C:\DEXISMonitor\DEXISXRayMonitor_error.log` (stderr)
- **Application Log**: `C:\DEXISMonitor\xray_monitor.log`

### 2. DEXISMCPHTTPServer
- **Purpose**: HTTP-based MCP server for DEXIS database access
- **Script**: `mcp_server_http.py`
- **NSSM Logs**:
  - `C:\DEXISMonitor\DEXISMCPHTTPServer_output.log` (stdout)
  - `C:\DEXISMonitor\DEXISMCPHTTPServer_error.log` (stderr)
- **Application Log**: `C:\DEXISMonitor\mcp_server_http.log`

## Common Issues

### Empty Logs
**Symptom**: Log files are empty or don't exist

**Possible Causes**:
1. **Service not running**: Check service status with `Get-Service DEXIS*`
2. **Service never started**: Check Windows Event Log for startup errors
3. **Log path misconfigured**: Verify NSSM configuration with `nssm get <ServiceName> AppStdout`
4. **Permissions issue**: Service may not have write access to log directory

**Resolution**:
```powershell
# Check service status
Get-Service DEXISXRayMonitor
Get-Service DEXISMCPHTTPServer

# Check NSSM configuration
nssm get DEXISXRayMonitor AppStdout
nssm get DEXISXRayMonitor AppStderr

# Check Windows Event Log for errors
Get-EventLog -LogName Application -Source "DEXISXRayMonitor" -Newest 10
```

### Service Not Running
**Symptom**: Service shows as "Stopped" status

**Resolution**:
```powershell
# Start the service
Start-Service DEXISXRayMonitor
Start-Service DEXISMCPHTTPServer

# Enable auto-start on boot
Set-Service DEXISXRayMonitor -StartupType Automatic
Set-Service DEXISMCPHTTPServer -StartupType Automatic

# Verify status
Get-Service DEXIS*
```

### Service Crashes or Stops Unexpectedly
**Symptom**: Service starts but stops shortly after

**Troubleshooting Steps**:
1. Check stderr log for Python errors
2. Verify Python is installed and accessible
3. Check dependencies: `pip list`
4. Verify config.json exists and is valid
5. Check database connectivity

```powershell
# View stderr log (contains Python errors)
Get-Content C:\DEXISMonitor\DEXISXRayMonitor_error.log -Tail 20

# Test Python manually
cd C:\DEXISMonitor
python xray_monitor.py
# (Press Ctrl+C to stop)
```

### Log Rotation Not Working
**Symptom**: Log files grow indefinitely

**NSSM Log Rotation Settings**:
```powershell
# Check rotation settings
nssm get DEXISXRayMonitor AppRotateFiles      # Should be 1 (enabled)
nssm get DEXISXRayMonitor AppRotateOnline     # Should be 1 (rotate while running)
nssm get DEXISXRayMonitor AppRotateSeconds    # Should be 86400 (daily)
nssm get DEXISXRayMonitor AppRotateBytes      # Should be 10485760 (10MB)
```

## Manual Service Commands

### Check Status
```powershell
# Quick status check
Get-Service DEXIS*

# Detailed information
Get-Service DEXISXRayMonitor | Format-List *
```

### Start/Stop Services
```powershell
# Start services
Start-Service DEXISXRayMonitor
Start-Service DEXISMCPHTTPServer

# Stop services
Stop-Service DEXISXRayMonitor
Stop-Service DEXISMCPHTTPServer

# Restart services
Restart-Service DEXISXRayMonitor
Restart-Service DEXISMCPHTTPServer
```

### View Logs
```powershell
# View last 20 lines of stdout
Get-Content C:\DEXISMonitor\DEXISXRayMonitor_output.log -Tail 20

# View last 20 lines of stderr (errors)
Get-Content C:\DEXISMonitor\DEXISXRayMonitor_error.log -Tail 20

# View application log
Get-Content C:\DEXISMonitor\xray_monitor.log -Tail 20

# Follow log in real-time (like tail -f)
Get-Content C:\DEXISMonitor\xray_monitor.log -Wait -Tail 20
```

### NSSM Commands
```powershell
# Service status via NSSM
nssm status DEXISXRayMonitor

# View all NSSM configuration
nssm dump DEXISXRayMonitor

# Edit service (opens GUI)
nssm edit DEXISXRayMonitor

# Reinstall service (if configuration is broken)
nssm remove DEXISXRayMonitor confirm
# Then run install_all_services.ps1
```

## Log File Locations

All logs are stored in `C:\DEXISMonitor\`:

| Service | NSSM Stdout | NSSM Stderr | Application Log |
|---------|-------------|-------------|-----------------|
| DEXISXRayMonitor | `DEXISXRayMonitor_output.log` | `DEXISXRayMonitor_error.log` | `xray_monitor.log` |
| DEXISMCPHTTPServer | `DEXISMCPHTTPServer_output.log` | `DEXISMCPHTTPServer_error.log` | `mcp_server_http.log` |

**Note**: NSSM captures stdout/stderr, while Python's logging framework writes to application-specific log files. Both should be checked when troubleshooting.

## Verification Checklist

When diagnosing service issues, check:

- [ ] NSSM is installed at `C:\Program Files\nssm\nssm.exe`
- [ ] Services exist in Windows Services (services.msc)
- [ ] Services are running (Status = "Running")
- [ ] Services are set to auto-start (Start Type = "Automatic")
- [ ] Log files exist and are being written to
- [ ] No errors in stderr logs
- [ ] Application logs show recent activity
- [ ] Python is installed and accessible
- [ ] Required Python packages are installed (`pip list`)
- [ ] config.json exists and is valid JSON
- [ ] Database connection works (test manually)

## Getting Help

If services are not working after following this guide:

1. Run `.\verify_services.ps1` and save the output
2. Check stderr logs for Python errors
3. Test the Python scripts manually (see "Service Crashes" section above)
4. Check Windows Event Viewer for system-level errors
5. Verify all prerequisites are installed (Python, dependencies, NSSM)
