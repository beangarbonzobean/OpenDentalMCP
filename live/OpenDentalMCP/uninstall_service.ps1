# Uninstall Open Dental MCP Server Windows Service

$ErrorActionPreference = "Stop"

$ServiceName = "OpenDentalMCPServer"
$InstallPath = "C:\OpenDentalMCP"
$nssmPath = "C:\Program Files\nssm\nssm.exe"

Write-Host "Uninstalling Open Dental MCP Server..." -ForegroundColor Yellow

# Check if service exists
$service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if (-not $service) {
    Write-Host "Service '$ServiceName' not found. Nothing to uninstall." -ForegroundColor Yellow
    exit 0
}

# Stop service if running
if ($service.Status -eq "Running") {
    Write-Host "Stopping service..." -ForegroundColor Yellow
    Stop-Service -Name $ServiceName -Force
    Start-Sleep -Seconds 2
}

# Remove service
if (Test-Path $nssmPath) {
    Write-Host "Removing service..." -ForegroundColor Yellow
    & $nssmPath remove $ServiceName confirm
    Start-Sleep -Seconds 2
} else {
    Write-Host "NSSM not found. Removing service via PowerShell..." -ForegroundColor Yellow
    Remove-Service -Name $ServiceName -ErrorAction SilentlyContinue
}

# Optionally remove installation directory
$removeFiles = Read-Host "Remove installation directory ($InstallPath)? (y/N)"
if ($removeFiles -eq "y" -or $removeFiles -eq "Y") {
    if (Test-Path $InstallPath) {
        Remove-Item -Path $InstallPath -Recurse -Force
        Write-Host "Removed installation directory." -ForegroundColor Green
    }
}

Write-Host "[OK] Service uninstalled successfully!" -ForegroundColor Green
