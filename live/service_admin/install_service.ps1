# Install Service Admin as an NSSM service
# Run as Administrator from PowerShell:
#   .\install_service.ps1

$ServiceName = "ServiceAdmin"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$PythonExe = "C:\Users\Administrator\Desktop\Cursor\OpenDentalMCP\live\OpenDentalMCP\.venv\Scripts\python.exe"
$ScriptPath = Join-Path $ScriptDir "service_admin.py"
$Nssm = "C:\Program Files\nssm\nssm.exe"

Write-Host "Installing $ServiceName..." -ForegroundColor Cyan
Write-Host "  Python: $PythonExe"
Write-Host "  Script: $ScriptPath"
Write-Host "  WorkDir: $ScriptDir"
Write-Host "  NSSM: $Nssm"

# Remove existing service if present
$existing = sc.exe query $ServiceName 2>&1
if ($LASTEXITCODE -eq 0) {
    Write-Host "Removing existing $ServiceName service..." -ForegroundColor Yellow
    & $Nssm stop $ServiceName 2>$null
    & $Nssm remove $ServiceName confirm
}

# Install
& $Nssm install $ServiceName $PythonExe $ScriptPath
& $Nssm set $ServiceName AppDirectory $ScriptDir
& $Nssm set $ServiceName DisplayName "MCP Service Admin"
& $Nssm set $ServiceName Description "HTTP admin endpoint for managing MCP NSSM services"
& $Nssm set $ServiceName Start SERVICE_AUTO_START

# Logging
& $Nssm set $ServiceName AppStdout (Join-Path $ScriptDir "admin_stdout.log")
& $Nssm set $ServiceName AppStderr (Join-Path $ScriptDir "admin_stderr.log")
& $Nssm set $ServiceName AppStdoutCreationDisposition 4
& $Nssm set $ServiceName AppStderrCreationDisposition 4
& $Nssm set $ServiceName AppRotateFiles 1
& $Nssm set $ServiceName AppRotateOnline 1
& $Nssm set $ServiceName AppRotateBytes 1048576

# Start it
Write-Host "Starting $ServiceName..." -ForegroundColor Cyan
& $Nssm start $ServiceName

Start-Sleep -Seconds 2

# Show status
sc.exe query $ServiceName

# Show the token
Write-Host ""
Write-Host "Admin token:" -ForegroundColor Green
$tokenFile = Join-Path $ScriptDir ".admin_token"
if (Test-Path $tokenFile) {
    Get-Content $tokenFile
} else {
    Write-Host "(Token will be generated on first run - check admin_stderr.log)" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "Test with:" -ForegroundColor Cyan
Write-Host "  curl http://192.168.127.49:9800/health"
