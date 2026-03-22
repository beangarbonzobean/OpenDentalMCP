# Fix Port Conflict Between DEXIS and OpenDental MCP Servers
# Changes OpenDental MCP Server to use port 8444 instead of 8443

param(
    [string]$OpenDentalServiceName = "OpenDentalMCPServer",
    [int]$NewPort = 8444,
    [string]$NSSMPath = "C:\Program Files\nssm\nssm.exe"
)

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "Fix MCP Server Port Conflict" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# Check if running as Administrator
$isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "ERROR: This script must be run as Administrator!" -ForegroundColor Red
    Write-Host "Right-click PowerShell and select 'Run as Administrator'" -ForegroundColor Yellow
    exit 1
}

# Check current services
Write-Host "Step 1: Checking current services..." -ForegroundColor Cyan
$dexisService = Get-Service -Name "DEXISMCPHTTPServer" -ErrorAction SilentlyContinue
$opendentalService = Get-Service -Name $OpenDentalServiceName -ErrorAction SilentlyContinue

if ($dexisService) {
    Write-Host "  [OK] DEXIS MCP Server found: $($dexisService.Status)" -ForegroundColor Green
} else {
    Write-Host "  [WARN] DEXIS MCP Server not found" -ForegroundColor Yellow
}

if ($opendentalService) {
    Write-Host "  [OK] OpenDental MCP Server found: $($opendentalService.Status)" -ForegroundColor Green
} else {
    Write-Host "  [ERROR] OpenDental MCP Server not found: $OpenDentalServiceName" -ForegroundColor Red
    Write-Host "  Please check the service name and try again" -ForegroundColor Yellow
    exit 1
}

Write-Host ""

# Check NSSM
if (-not (Test-Path $NSSMPath)) {
    Write-Host "[ERROR] NSSM not found at: $NSSMPath" -ForegroundColor Red
    Write-Host "NSSM is required to update the service configuration" -ForegroundColor Yellow
    exit 1
}

# Stop OpenDental service
Write-Host "Step 2: Stopping OpenDental MCP Server..." -ForegroundColor Cyan
if ($opendentalService.Status -eq 'Running') {
    try {
        Stop-Service -Name $OpenDentalServiceName -Force
        Start-Sleep -Seconds 2
        Write-Host "  [OK] Service stopped" -ForegroundColor Green
    } catch {
        Write-Host "  [ERROR] Failed to stop service: $_" -ForegroundColor Red
        exit 1
    }
} else {
    Write-Host "  [OK] Service already stopped" -ForegroundColor Green
}

Write-Host ""

# Get current service configuration
Write-Host "Step 3: Getting current service configuration..." -ForegroundColor Cyan
try {
    $currentAppParams = & $NSSMPath get $OpenDentalServiceName AppParameters
    Write-Host "  Current parameters: $currentAppParams" -ForegroundColor White
    
    # Extract script path and current port
    if ($currentAppParams -match '"(.*?)"\s+(\d+)') {
        $scriptPath = $matches[1]
        $currentPort = [int]$matches[2]
        Write-Host "  Script path: $scriptPath" -ForegroundColor Gray
        Write-Host "  Current port: $currentPort" -ForegroundColor Gray
        
        if ($currentPort -eq $NewPort) {
            Write-Host "  [INFO] Service already configured for port $NewPort" -ForegroundColor Yellow
            Write-Host "  No changes needed!" -ForegroundColor Green
        } else {
            # Update port
            Write-Host ""
            Write-Host "Step 4: Updating service to use port $NewPort..." -ForegroundColor Cyan
            $newAppParams = "`"$scriptPath`" $NewPort"
            & $NSSMPath set $OpenDentalServiceName AppParameters $newAppParams
            Write-Host "  [OK] Service updated to use port $NewPort" -ForegroundColor Green
        }
    } else {
        Write-Host "  [WARN] Could not parse current parameters" -ForegroundColor Yellow
        Write-Host "  Attempting to update anyway..." -ForegroundColor Yellow
        
        # Try to get script path from AppDirectory and common script names
        $appDir = & $NSSMPath get $OpenDentalServiceName AppDirectory
        $possibleScripts = @("mcp_server_http.py", "opendental_mcp_server.py", "server.py")
        
        $scriptPath = $null
        foreach ($script in $possibleScripts) {
            $testPath = Join-Path $appDir $script
            if (Test-Path $testPath) {
                $scriptPath = $testPath
                break
            }
        }
        
        if ($scriptPath) {
            $newAppParams = "`"$scriptPath`" $NewPort"
            & $NSSMPath set $OpenDentalServiceName AppParameters $newAppParams
            Write-Host "  [OK] Service updated to use port $NewPort" -ForegroundColor Green
        } else {
            Write-Host "  [ERROR] Could not find script path" -ForegroundColor Red
            Write-Host "  Please update manually using NSSM GUI or command line" -ForegroundColor Yellow
            exit 1
        }
    }
} catch {
    Write-Host "  [ERROR] Failed to update service: $_" -ForegroundColor Red
    exit 1
}

Write-Host ""

# Verify configuration
Write-Host "Step 5: Verifying configuration..." -ForegroundColor Cyan
try {
    $updatedParams = & $NSSMPath get $OpenDentalServiceName AppParameters
    Write-Host "  Updated parameters: $updatedParams" -ForegroundColor White
    
    if ($updatedParams -match "$NewPort") {
        Write-Host "  [OK] Configuration verified" -ForegroundColor Green
    } else {
        Write-Host "  [WARN] Configuration may not have updated correctly" -ForegroundColor Yellow
    }
} catch {
    Write-Host "  [WARN] Could not verify configuration" -ForegroundColor Yellow
}

Write-Host ""

# Start service
Write-Host "Step 6: Starting OpenDental MCP Server..." -ForegroundColor Cyan
try {
    Start-Service -Name $OpenDentalServiceName
    Start-Sleep -Seconds 2
    $status = (Get-Service -Name $OpenDentalServiceName).Status
    if ($status -eq 'Running') {
        Write-Host "  [OK] Service started successfully" -ForegroundColor Green
    } else {
        Write-Host "  [WARN] Service status: $status" -ForegroundColor Yellow
    }
} catch {
    Write-Host "  [ERROR] Failed to start service: $_" -ForegroundColor Red
    Write-Host "  Check logs for details" -ForegroundColor Yellow
}

Write-Host ""

# Summary
Write-Host "========================================" -ForegroundColor Green
Write-Host "Port Conflict Fixed!" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Green
Write-Host ""
Write-Host "Service Configuration:" -ForegroundColor Cyan
Write-Host "  DEXIS MCP Server: Port 8443" -ForegroundColor White
Write-Host "  OpenDental MCP Server: Port $NewPort" -ForegroundColor White
Write-Host ""

# Check if Cloudflare tunnel config exists
$cloudflareConfig = "C:\cloudflared\config.yml"
if (Test-Path $cloudflareConfig) {
    Write-Host "IMPORTANT: Update Cloudflare Tunnel Config" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "Update C:\cloudflared\config.yml to include:" -ForegroundColor White
    Write-Host "  - hostname: opendental-mcp.yourdomain.com" -ForegroundColor Gray
    Write-Host "    service: https://localhost:$NewPort" -ForegroundColor Gray
    Write-Host ""
    Write-Host "Then restart the Cloudflare tunnel service" -ForegroundColor White
    Write-Host ""
}

Write-Host "Test the servers:" -ForegroundColor Cyan
Write-Host "  DEXIS: Invoke-WebRequest -Uri 'https://localhost:8443/health' -SkipCertificateCheck" -ForegroundColor White
Write-Host "  OpenDental: Invoke-WebRequest -Uri 'https://localhost:$NewPort/health' -SkipCertificateCheck" -ForegroundColor White
Write-Host ""

Write-Host "Check service status:" -ForegroundColor Cyan
Write-Host "  Get-Service DEXISMCPHTTPServer, $OpenDentalServiceName" -ForegroundColor White
Write-Host ""

