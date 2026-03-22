# Check Open Dental MCP Server Service Status

$ServiceName = "OpenDentalMCPServer"
$InstallPath = "C:\OpenDentalMCP"

Write-Host "Open Dental MCP Server Status" -ForegroundColor Cyan
Write-Host ("=" * 50) -ForegroundColor Cyan

# Check service status
$service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($service) {
    $statusColor = if ($service.Status -eq "Running") { "Green" } else { "Red" }
    Write-Host "Service Status: " -NoNewline
    Write-Host $service.Status -ForegroundColor $statusColor
    Write-Host "Service Name: $($service.Name)" -ForegroundColor Cyan
    Write-Host "Display Name: $($service.DisplayName)" -ForegroundColor Cyan
    Write-Host "Start Type: $($service.StartType)" -ForegroundColor Cyan
} else {
    Write-Host "Service not found!" -ForegroundColor Red
}

Write-Host ""

# Check installation directory
if (Test-Path $InstallPath) {
    Write-Host "Installation Path: $InstallPath" -ForegroundColor Green
    $files = Get-ChildItem -Path $InstallPath -File
    Write-Host "Files: $($files.Count)" -ForegroundColor Cyan
} else {
    Write-Host "Installation directory not found: $InstallPath" -ForegroundColor Red
}

Write-Host ""

# Port from .env (MCP_HTTP_PORT), default 8444 to match config.example / DEXIS coexistence
$httpPort = "8444"
$envFile = Join-Path $InstallPath ".env"
if (Test-Path $envFile) {
    Get-Content $envFile | ForEach-Object {
        if ($_ -match '^\s*MCP_HTTP_PORT=(.+)$') { $httpPort = $matches[1].Trim() }
    }
}

# Check if HTTP server is responding
try {
    $response = Invoke-WebRequest -Uri "https://localhost:$httpPort/health" -SkipCertificateCheck -TimeoutSec 2 -ErrorAction Stop
    Write-Host "HTTP Server: " -NoNewline
    Write-Host "Running" -ForegroundColor Green
    Write-Host "Health Check: $($response.StatusCode)" -ForegroundColor Cyan
} catch {
    Write-Host "HTTP Server: " -NoNewline
    Write-Host "Not responding" -ForegroundColor Red
}

Write-Host ""

# Show recent logs
$logFile = Join-Path $InstallPath "opendental_mcp_http.log"
if (Test-Path $logFile) {
    Write-Host "Recent Log Entries:" -ForegroundColor Cyan
    Get-Content $logFile -Tail 5 | ForEach-Object {
        Write-Host "  $_" -ForegroundColor Gray
    }
} else {
    Write-Host "Log file not found: $logFile" -ForegroundColor Yellow
}

Write-Host ""

