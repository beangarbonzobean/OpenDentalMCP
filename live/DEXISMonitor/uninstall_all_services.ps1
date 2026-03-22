# Uninstall All DEXIS Services
# Removes both X-Ray Monitor and MCP HTTP Server Windows Services
# Run this script as Administrator

param(
    [switch]$Force
)

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "DEXIS Services Uninstaller" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "This will remove:" -ForegroundColor White
Write-Host "  1. DEXIS X-Ray Monitor Service" -ForegroundColor Yellow
Write-Host "  2. DEXIS MCP HTTP Server Service" -ForegroundColor Yellow
Write-Host ""

# Check if running as Administrator
$isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "ERROR: This script must be run as Administrator!" -ForegroundColor Red
    Write-Host "Right-click PowerShell and select 'Run as Administrator'" -ForegroundColor Yellow
    exit 1
}

$nssmPath = "C:\Program Files\nssm\nssm.exe"

# Function to remove a service
function Remove-Service {
    param(
        [string]$ServiceName,
        [string]$DisplayName
    )
    
    $service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    if (-not $service) {
        Write-Host "Service '$DisplayName' not found. Skipping..." -ForegroundColor Yellow
        return
    }
    
    # Check if service is running
    if ($service.Status -eq 'Running') {
        Write-Host "Stopping service: $DisplayName..." -ForegroundColor Cyan
        Stop-Service -Name $ServiceName -Force
        Start-Sleep -Seconds 2
    }
    
    # Remove service using NSSM
    if (Test-Path $nssmPath) {
        Write-Host "Removing service: $DisplayName..." -ForegroundColor Cyan
        & $nssmPath remove $ServiceName confirm
        Start-Sleep -Seconds 2
        
        # Verify removal
        $service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
        if (-not $service) {
            Write-Host "Service '$DisplayName' removed successfully!" -ForegroundColor Green
        } else {
            Write-Host "WARNING: Service '$DisplayName' may not have been removed completely" -ForegroundColor Yellow
        }
    } else {
        Write-Host "NSSM not found. Trying to remove service using sc.exe..." -ForegroundColor Yellow
        sc.exe delete $ServiceName
    }
}

# Remove X-Ray Monitor Service
Write-Host "Removing X-Ray Monitor Service..." -ForegroundColor Cyan
Remove-Service -ServiceName "DEXISXRayMonitor" -DisplayName "DEXIS X-Ray Monitor"

Write-Host ""

# Remove MCP HTTP Server Service
Write-Host "Removing MCP HTTP Server Service..." -ForegroundColor Cyan
Remove-Service -ServiceName "DEXISMCPHTTPServer" -DisplayName "DEXIS MCP HTTP Server"

Write-Host ""
Write-Host "========================================" -ForegroundColor Green
Write-Host "Uninstallation Complete!" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Green
Write-Host ""
Write-Host "Note: Installation files in C:\DEXISMonitor are not removed." -ForegroundColor Yellow
Write-Host "If you want to remove them, delete the folder manually." -ForegroundColor Yellow
Write-Host ""

