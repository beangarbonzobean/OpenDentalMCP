# DEXIS Services Status Verification Script
# Checks NSSM service status, log paths, and service health
# Run this script to diagnose service issues

param(
    [string]$InstallPath = "C:\DEXISMonitor"
)

$ErrorActionPreference = "Continue"

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "DEXIS Services Status Check" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# Check if running as Administrator
$isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "WARNING: Not running as Administrator. Some checks may fail." -ForegroundColor Yellow
    Write-Host ""
}

# Check if NSSM is installed
$nssmPath = "C:\Program Files\nssm\nssm.exe"
if (-not (Test-Path $nssmPath)) {
    Write-Host "ERROR: NSSM not found at $nssmPath" -ForegroundColor Red
    Write-Host "Services may not be installed correctly." -ForegroundColor Yellow
    exit 1
}

Write-Host "✓ NSSM found at: $nssmPath" -ForegroundColor Green
Write-Host ""

# Function to check a single service
function Check-DexisService {
    param(
        [string]$ServiceName,
        [string]$DisplayName
    )

    Write-Host "----------------------------------------" -ForegroundColor Cyan
    Write-Host "Checking: $DisplayName" -ForegroundColor Cyan
    Write-Host "----------------------------------------" -ForegroundColor Cyan

    # Check if service exists
    $service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    if (-not $service) {
        Write-Host "✗ Service '$ServiceName' NOT FOUND" -ForegroundColor Red
        Write-Host "  Run install_all_services.ps1 to install services" -ForegroundColor Yellow
        Write-Host ""
        return $false
    }

    Write-Host "✓ Service exists: $ServiceName" -ForegroundColor Green
    Write-Host "  Display Name: $($service.DisplayName)" -ForegroundColor Gray
    Write-Host "  Status: $($service.Status)" -ForegroundColor $(if ($service.Status -eq 'Running') { 'Green' } else { 'Yellow' })
    Write-Host "  Start Type: $($service.StartType)" -ForegroundColor Gray
    Write-Host ""

    # Get NSSM configuration
    Write-Host "NSSM Configuration:" -ForegroundColor White

    # Application path
    $appPath = & $nssmPath get $ServiceName Application
    Write-Host "  Application: $appPath" -ForegroundColor Gray

    # Application parameters
    $appParams = & $nssmPath get $ServiceName AppParameters
    Write-Host "  Parameters: $appParams" -ForegroundColor Gray

    # Working directory
    $appDir = & $nssmPath get $ServiceName AppDirectory
    Write-Host "  Directory: $appDir" -ForegroundColor Gray

    # Log file paths
    $stdoutPath = & $nssmPath get $ServiceName AppStdout
    $stderrPath = & $nssmPath get $ServiceName AppStderr
    Write-Host "  Stdout Log: $stdoutPath" -ForegroundColor Gray
    Write-Host "  Stderr Log: $stderrPath" -ForegroundColor Gray
    Write-Host ""

    # Check if log files exist and show recent entries
    Write-Host "Log Files:" -ForegroundColor White

    # Check stdout log
    if (Test-Path $stdoutPath) {
        $stdoutSize = (Get-Item $stdoutPath).Length
        $stdoutModified = (Get-Item $stdoutPath).LastWriteTime
        Write-Host "  ✓ Stdout log exists: $stdoutSize bytes" -ForegroundColor Green
        Write-Host "    Last modified: $stdoutModified" -ForegroundColor Gray

        # Show last few lines
        $lastLines = Get-Content $stdoutPath -Tail 5 -ErrorAction SilentlyContinue
        if ($lastLines) {
            Write-Host "    Last 5 lines:" -ForegroundColor Gray
            foreach ($line in $lastLines) {
                Write-Host "      $line" -ForegroundColor DarkGray
            }
        } else {
            Write-Host "    (Log file is empty or unreadable)" -ForegroundColor Yellow
        }
    } else {
        Write-Host "  ✗ Stdout log NOT FOUND: $stdoutPath" -ForegroundColor Yellow
        Write-Host "    Service may not have started yet or log path is misconfigured" -ForegroundColor Gray
    }
    Write-Host ""

    # Check stderr log
    if (Test-Path $stderrPath) {
        $stderrSize = (Get-Item $stderrPath).Length
        $stderrModified = (Get-Item $stderrPath).LastWriteTime
        if ($stderrSize -gt 0) {
            Write-Host "  ! Stderr log has content: $stderrSize bytes" -ForegroundColor Yellow
            Write-Host "    Last modified: $stderrModified" -ForegroundColor Gray

            # Show last few lines
            $lastLines = Get-Content $stderrPath -Tail 10 -ErrorAction SilentlyContinue
            if ($lastLines) {
                Write-Host "    Last 10 lines (errors/warnings):" -ForegroundColor Yellow
                foreach ($line in $lastLines) {
                    Write-Host "      $line" -ForegroundColor Red
                }
            }
        } else {
            Write-Host "  ✓ Stderr log is empty (no errors)" -ForegroundColor Green
            Write-Host "    Last modified: $stderrModified" -ForegroundColor Gray
        }
    } else {
        Write-Host "  ✗ Stderr log NOT FOUND: $stderrPath" -ForegroundColor Yellow
        Write-Host "    Service may not have started yet or log path is misconfigured" -ForegroundColor Gray
    }
    Write-Host ""

    # Check application-specific log files (Python logging)
    Write-Host "Application Logs:" -ForegroundColor White

    $appLogMap = @{
        "DEXISXRayMonitor" = "xray_monitor.log"
        "DEXISMCPHTTPServer" = "mcp_server_http.log"
    }

    if ($appLogMap.ContainsKey($ServiceName)) {
        $appLogFile = Join-Path $InstallPath $appLogMap[$ServiceName]
        if (Test-Path $appLogFile) {
            $appLogSize = (Get-Item $appLogFile).Length
            $appLogModified = (Get-Item $appLogFile).LastWriteTime
            Write-Host "  ✓ Application log exists: $appLogFile" -ForegroundColor Green
            Write-Host "    Size: $appLogSize bytes" -ForegroundColor Gray
            Write-Host "    Last modified: $appLogModified" -ForegroundColor Gray

            # Show last few lines
            $lastLines = Get-Content $appLogFile -Tail 5 -ErrorAction SilentlyContinue
            if ($lastLines) {
                Write-Host "    Last 5 lines:" -ForegroundColor Gray
                foreach ($line in $lastLines) {
                    Write-Host "      $line" -ForegroundColor DarkGray
                }
            }
        } else {
            Write-Host "  ✗ Application log NOT FOUND: $appLogFile" -ForegroundColor Yellow
        }
    }

    Write-Host ""

    # Service health summary
    if ($service.Status -eq 'Running') {
        Write-Host "✓ Service Status: HEALTHY" -ForegroundColor Green
        return $true
    } else {
        Write-Host "✗ Service Status: NOT RUNNING" -ForegroundColor Red
        Write-Host "  To start: Start-Service $ServiceName" -ForegroundColor Yellow
        return $false
    }
}

# Check both services
$xrayMonitorOk = Check-DexisService -ServiceName "DEXISXRayMonitor" -DisplayName "DEXIS X-Ray Monitor"
Write-Host ""

$mcpServerOk = Check-DexisService -ServiceName "DEXISMCPHTTPServer" -DisplayName "DEXIS MCP HTTP Server"
Write-Host ""

# Final summary
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "Summary" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan

$allOk = $xrayMonitorOk -and $mcpServerOk

if ($allOk) {
    Write-Host "✓ All services are running" -ForegroundColor Green
} else {
    Write-Host "✗ Some services are not running" -ForegroundColor Red
    Write-Host ""
    Write-Host "To start all services:" -ForegroundColor Yellow
    if (-not $xrayMonitorOk) {
        Write-Host "  Start-Service DEXISXRayMonitor" -ForegroundColor White
    }
    if (-not $mcpServerOk) {
        Write-Host "  Start-Service DEXISMCPHTTPServer" -ForegroundColor White
    }
    Write-Host ""
    Write-Host "To enable auto-start:" -ForegroundColor Yellow
    Write-Host "  Set-Service DEXISXRayMonitor -StartupType Automatic" -ForegroundColor White
    Write-Host "  Set-Service DEXISMCPHTTPServer -StartupType Automatic" -ForegroundColor White
}

Write-Host ""
Write-Host "For more detailed diagnostics:" -ForegroundColor Cyan
Write-Host "  Get-Service DEXIS* | Format-List *" -ForegroundColor White
Write-Host "  Get-EventLog -LogName Application -Source DEXIS* -Newest 10" -ForegroundColor White
Write-Host ""
