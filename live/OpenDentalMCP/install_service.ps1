# Install Open Dental MCP Server as Windows Service
# Uses NSSM (Non-Sucking Service Manager)

$ErrorActionPreference = "Stop"

$ServiceName = "OpenDentalMCPServer"
$ServiceDisplayName = "Open Dental MCP Server"
$ServiceDescription = "MCP Server for Open Dental API access"
$InstallPath = "C:\OpenDentalMCP"
$PythonPath = (Get-Command python).Source
$ScriptPath = Join-Path $InstallPath "mcp_server_http.py"

Write-Host "Installing Open Dental MCP Server as Windows Service..." -ForegroundColor Green

# Check if NSSM is available
$nssmPath = "C:\Program Files\nssm\nssm.exe"
if (-not (Test-Path $nssmPath)) {
    # Try to find NSSM in WinGet packages
    $wingetNssm = Get-ChildItem -Path "$env:LOCALAPPDATA\Microsoft\WinGet\Packages" -Filter "nssm.exe" -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty FullName
    if ($wingetNssm -and (Test-Path $wingetNssm)) {
        $nssmPath = $wingetNssm
        Write-Host "Found NSSM at: $nssmPath" -ForegroundColor Green
    } else {
        Write-Host "NSSM not found. Please install NSSM first." -ForegroundColor Red
        Write-Host "Download from: https://nssm.cc/download" -ForegroundColor Yellow
        Write-Host "Or install via: winget install NSSM.NSSM" -ForegroundColor Yellow
        exit 1
    }
}

# Create installation directory
if (-not (Test-Path $InstallPath)) {
    New-Item -ItemType Directory -Path $InstallPath -Force | Out-Null
    Write-Host "Created installation directory: $InstallPath" -ForegroundColor Green
}

# Copy files to installation directory
$SourcePath = Split-Path -Parent $MyInvocation.MyCommand.Path
Copy-Item "$SourcePath\*.py" $InstallPath -Force
Copy-Item "$SourcePath\requirements.txt" $InstallPath -Force
if (Test-Path "$SourcePath\config.json") {
    Copy-Item "$SourcePath\config.json" $InstallPath -Force
}

# Copy .env file from parent directory (where it's typically located)
$ParentEnvFile = Join-Path (Split-Path -Parent $SourcePath) ".env"
if (Test-Path $ParentEnvFile) {
    Write-Host "Copying .env file from parent directory..." -ForegroundColor Yellow
    Copy-Item $ParentEnvFile (Join-Path $InstallPath ".env") -Force
    Write-Host "[OK] .env file copied" -ForegroundColor Green
} else {
    Write-Host "[WARNING] .env file not found at: $ParentEnvFile" -ForegroundColor Yellow
    Write-Host "You may need to create a .env file in $InstallPath" -ForegroundColor Yellow
}

# Install Python dependencies
Write-Host "Installing Python dependencies..." -ForegroundColor Yellow
& $PythonPath -m pip install -r (Join-Path $InstallPath "requirements.txt")

# Check if service already exists
$existingService = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($existingService) {
    Write-Host "Service already exists. Removing old service..." -ForegroundColor Yellow
    & $nssmPath stop $ServiceName
    & $nssmPath remove $ServiceName confirm
    Start-Sleep -Seconds 2
}

# Install service
Write-Host "Installing service..." -ForegroundColor Yellow
& $nssmPath install $ServiceName $PythonPath "$ScriptPath"

# Configure service
& $nssmPath set $ServiceName DisplayName $ServiceDisplayName
& $nssmPath set $ServiceName Description $ServiceDescription
& $nssmPath set $ServiceName AppDirectory $InstallPath
& $nssmPath set $ServiceName Start SERVICE_AUTO_START
& $nssmPath set $ServiceName AppStdout (Join-Path $InstallPath "service_stdout.log")
& $nssmPath set $ServiceName AppStderr (Join-Path $InstallPath "service_stderr.log")
& $nssmPath set $ServiceName AppRotateFiles 1
& $nssmPath set $ServiceName AppRotateOnline 1
& $nssmPath set $ServiceName AppRotateSeconds 86400
& $nssmPath set $ServiceName AppRotateBytes 10485760

# Set environment variables from .env if it exists
$envFile = Join-Path $InstallPath ".env"
if (Test-Path $envFile) {
    Write-Host "Loading environment variables from .env..." -ForegroundColor Yellow
    $envVars = @()
    Get-Content $envFile | ForEach-Object {
        if ($_ -match '^\s*([^#][^=]+)=(.*)$') {
            $key = $matches[1].Trim()
            $value = $matches[2].Trim()
            # Remove quotes if present
            if ($value -match '^"(.*)"$' -or $value -match "^'(.*)'$") {
                $value = $matches[1]
            }
            $envVars += "$key=$value"
        }
    }
    
    # Set all environment variables at once (NSSM supports multiple AppEnvironmentExtra)
    foreach ($envVar in $envVars) {
        & $nssmPath set $ServiceName AppEnvironmentExtra $envVar
    }
    Write-Host "[OK] Loaded $($envVars.Count) environment variables" -ForegroundColor Green
} else {
    Write-Host "[WARNING] .env file not found. Service will use system environment variables." -ForegroundColor Yellow
}

$mcpPort = "8444"
if (Test-Path $envFile) {
    Get-Content $envFile | ForEach-Object {
        if ($_ -match '^\s*MCP_HTTP_PORT=(.+)$') { $mcpPort = $matches[1].Trim() }
    }
}

# Start service
Write-Host "Starting service..." -ForegroundColor Yellow
Start-Service $ServiceName

# Wait a moment and check status
Start-Sleep -Seconds 2
$service = Get-Service -Name $ServiceName
if ($service.Status -eq "Running") {
    Write-Host "[OK] Service installed and started successfully!" -ForegroundColor Green
    Write-Host "Service Name: $ServiceName" -ForegroundColor Cyan
    Write-Host "Installation Path: $InstallPath" -ForegroundColor Cyan
    Write-Host "HTTP Server: https://localhost:$mcpPort" -ForegroundColor Cyan
} else {
    Write-Host "[ERROR] Service installed but not running. Status: $($service.Status)" -ForegroundColor Red
    Write-Host "Check logs at: $InstallPath\service_stderr.log" -ForegroundColor Yellow
}

