# Install All DEXIS Services on Server
# Installs both X-Ray Monitor and MCP HTTP Server as Windows Services
# Run this script on the target Windows server as Administrator

param(
    [string]$InstallPath = "C:\DEXISMonitor",
    [int]$MCPPort = 8443
)

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "DEXIS Services Installer" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "This will install:" -ForegroundColor White
Write-Host "  1. DEXIS X-Ray Monitor (24/7 monitoring)" -ForegroundColor Yellow
Write-Host "  2. DEXIS MCP HTTP Server (24/7 API access)" -ForegroundColor Yellow
Write-Host ""

# Check if running as Administrator
$isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "ERROR: This script must be run as Administrator!" -ForegroundColor Red
    Write-Host "Right-click PowerShell and select 'Run as Administrator'" -ForegroundColor Yellow
    exit 1
}

# Check if NSSM is installed
$nssmPath = "C:\Program Files\nssm\nssm.exe"
if (-not (Test-Path $nssmPath)) {
    Write-Host "NSSM (Non-Sucking Service Manager) not found." -ForegroundColor Yellow
    Write-Host "Downloading NSSM..." -ForegroundColor Cyan
    
    # Create temp directory
    $tempDir = "$env:TEMP\nssm"
    if (-not (Test-Path $tempDir)) {
        New-Item -ItemType Directory -Path $tempDir -Force | Out-Null
    }
    
    # Download NSSM
    $nssmUrl = "https://nssm.cc/release/nssm-2.24.zip"
    $nssmZip = "$tempDir\nssm.zip"
    $nssmExtract = "$tempDir\nssm"
    
    try {
        Write-Host "Downloading NSSM from $nssmUrl..." -ForegroundColor Cyan
        Invoke-WebRequest -Uri $nssmUrl -OutFile $nssmZip -UseBasicParsing
        
        Write-Host "Extracting NSSM..." -ForegroundColor Cyan
        Expand-Archive -Path $nssmZip -DestinationPath $nssmExtract -Force
        
        # Find the correct architecture (win64 or win32)
        $nssmExe = Get-ChildItem -Path $nssmExtract -Recurse -Filter "nssm.exe" | Select-Object -First 1
        
        if ($nssmExe) {
            # Copy to Program Files
            $nssmDir = "C:\Program Files\nssm"
            if (-not (Test-Path $nssmDir)) {
                New-Item -ItemType Directory -Path $nssmDir -Force | Out-Null
            }
            Copy-Item $nssmExe.FullName -Destination $nssmPath -Force
            Write-Host "NSSM installed successfully!" -ForegroundColor Green
        } else {
            Write-Host "ERROR: Could not find nssm.exe in downloaded files" -ForegroundColor Red
            exit 1
        }
    } catch {
        Write-Host "ERROR: Failed to download/install NSSM: $_" -ForegroundColor Red
        Write-Host "Please download NSSM manually from: https://nssm.cc/download" -ForegroundColor Yellow
        Write-Host "Extract it to: C:\Program Files\nssm\" -ForegroundColor Yellow
        exit 1
    }
}

# Get current script directory
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

# Determine installation path
if (-not (Test-Path $InstallPath)) {
    Write-Host "Creating installation directory: $InstallPath" -ForegroundColor Cyan
    New-Item -ItemType Directory -Path $InstallPath -Force | Out-Null
}

# Copy all files to installation directory
Write-Host ""
Write-Host "Copying files to $InstallPath..." -ForegroundColor Cyan
$filesToCopy = @(
    "xray_monitor.py",
    "mcp_server_http.py",
    "mcp_tools.py",
    "mcp_server.py",
    "dexis_db_query.py",
    "tooth_number_converter.py",
    "config.json",
    "requirements.txt",
    "server.crt",
    "server.key"
)

foreach ($file in $filesToCopy) {
    $sourcePath = Join-Path $scriptDir $file
    if (Test-Path $sourcePath) {
        Copy-Item $sourcePath -Destination $InstallPath -Force
        Write-Host "  Copied: $file" -ForegroundColor Gray
    } else {
        Write-Host "  WARNING: $file not found (will skip)" -ForegroundColor Yellow
    }
}

# Find Python executable
Write-Host ""
Write-Host "Finding Python installation..." -ForegroundColor Cyan
$pythonExe = $null

# Try common Python locations
$pythonPaths = @(
    "C:\Python*\python.exe",
    "C:\Program Files\Python*\python.exe",
    "C:\Program Files (x86)\Python*\python.exe",
    "$env:LOCALAPPDATA\Programs\Python\Python*\python.exe",
    "$env:ProgramFiles\Python*\python.exe"
)

foreach ($path in $pythonPaths) {
    $found = Get-ChildItem -Path $path -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($found) {
        $pythonExe = $found.FullName
        break
    }
}

# Try python command
if (-not $pythonExe) {
    try {
        $pythonVersion = python --version 2>&1
        if ($LASTEXITCODE -eq 0) {
            $pythonExe = (Get-Command python).Source
        }
    } catch {
        # Python not in PATH
    }
}

if (-not $pythonExe) {
    Write-Host "ERROR: Python not found!" -ForegroundColor Red
    Write-Host "Please install Python 3.7+ and try again." -ForegroundColor Yellow
    exit 1
}

Write-Host "Found Python: $pythonExe" -ForegroundColor Green

# Install Python dependencies
Write-Host ""
Write-Host "Installing Python dependencies..." -ForegroundColor Cyan
$requirementsFile = Join-Path $InstallPath "requirements.txt"
if (Test-Path $requirementsFile) {
    & $pythonExe -m pip install -r $requirementsFile --quiet
    if ($LASTEXITCODE -eq 0) {
        Write-Host "Dependencies installed successfully!" -ForegroundColor Green
    } else {
        Write-Host "WARNING: Some dependencies may not have installed correctly" -ForegroundColor Yellow
    }
} else {
    Write-Host "WARNING: requirements.txt not found" -ForegroundColor Yellow
}

# Function to install a service
function Install-Service {
    param(
        [string]$ServiceName,
        [string]$DisplayName,
        [string]$Description,
        [string]$ScriptPath,
        [string]$Arguments = ""
    )
    
    # Check if service already exists
    $existingService = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    if ($existingService) {
        Write-Host "Service '$ServiceName' already exists." -ForegroundColor Yellow
        $response = Read-Host "Do you want to remove and reinstall it? (Y/N)"
        if ($response -eq 'Y' -or $response -eq 'y') {
            Write-Host "Removing existing service..." -ForegroundColor Cyan
            if ($existingService.Status -eq 'Running') {
                Stop-Service -Name $ServiceName -Force
            }
            & $nssmPath remove $ServiceName confirm
            Start-Sleep -Seconds 2
        } else {
            Write-Host "Skipping service '$ServiceName'..." -ForegroundColor Yellow
            return $false
        }
    }
    
    # Install service using NSSM
    Write-Host "Installing service: $DisplayName..." -ForegroundColor Cyan
    
    & $nssmPath install $ServiceName $pythonExe
    if ($Arguments) {
        & $nssmPath set $ServiceName AppParameters "`"$ScriptPath`" $Arguments"
    } else {
        & $nssmPath set $ServiceName AppParameters "`"$ScriptPath`""
    }
    & $nssmPath set $ServiceName DisplayName $DisplayName
    & $nssmPath set $ServiceName Description $Description
    & $nssmPath set $ServiceName Start SERVICE_AUTO_START
    & $nssmPath set $ServiceName AppDirectory $InstallPath
    & $nssmPath set $ServiceName AppStdout (Join-Path $InstallPath "$ServiceName_output.log")
    & $nssmPath set $ServiceName AppStderr (Join-Path $InstallPath "$ServiceName_error.log")
    & $nssmPath set $ServiceName AppRotateFiles 1
    & $nssmPath set $ServiceName AppRotateOnline 1
    & $nssmPath set $ServiceName AppRotateSeconds 86400
    & $nssmPath set $ServiceName AppRotateBytes 10485760
    
    Write-Host "Service '$DisplayName' installed successfully!" -ForegroundColor Green
    return $true
}

# Install X-Ray Monitor Service
Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "Installing X-Ray Monitor Service" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
$monitorScript = Join-Path $InstallPath "xray_monitor.py"
$monitorInstalled = Install-Service `
    -ServiceName "DEXISXRayMonitor" `
    -DisplayName "DEXIS X-Ray Monitor" `
    -Description "Monitors DEXIS Imaging Suite for new x-ray images and sends notifications" `
    -ScriptPath $monitorScript

# Install MCP HTTP Server Service
Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "Installing MCP HTTP Server Service" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
$mcpScript = Join-Path $InstallPath "mcp_server_http.py"
$mcpInstalled = Install-Service `
    -ServiceName "DEXISMCPHTTPServer" `
    -DisplayName "DEXIS MCP HTTP Server" `
    -Description "HTTP-based MCP server for DEXIS database access via AI agents" `
    -ScriptPath $mcpScript `
    -Arguments $MCPPort

# Summary
Write-Host ""
Write-Host "========================================" -ForegroundColor Green
Write-Host "Installation Complete!" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Green
Write-Host ""

if ($monitorInstalled) {
    Write-Host "X-Ray Monitor Service:" -ForegroundColor Cyan
    Write-Host "  Service Name: DEXISXRayMonitor" -ForegroundColor White
    Write-Host "  Display Name: DEXIS X-Ray Monitor" -ForegroundColor White
    Write-Host "  Status: Installed" -ForegroundColor Green
    Write-Host ""
}

if ($mcpInstalled) {
    Write-Host "MCP HTTP Server Service:" -ForegroundColor Cyan
    Write-Host "  Service Name: DEXISMCPHTTPServer" -ForegroundColor White
    Write-Host "  Display Name: DEXIS MCP HTTP Server" -ForegroundColor White
    Write-Host "  Port: $MCPPort" -ForegroundColor White
    Write-Host "  URL: https://localhost:$MCPPort/mcp" -ForegroundColor White
    Write-Host "  Status: Installed" -ForegroundColor Green
    Write-Host ""
}

Write-Host "Installation Path: $InstallPath" -ForegroundColor White
Write-Host ""

Write-Host "IMPORTANT: Before starting the services:" -ForegroundColor Yellow
Write-Host "1. Edit config.json in $InstallPath" -ForegroundColor White
Write-Host "   - Set database credentials" -ForegroundColor Gray
Write-Host "   - Configure notification settings" -ForegroundColor Gray
Write-Host ""
Write-Host "2. Test services manually first:" -ForegroundColor White
Write-Host "   cd $InstallPath" -ForegroundColor Gray
Write-Host "   python xray_monitor.py" -ForegroundColor Gray
Write-Host "   (Press Ctrl+C after testing)" -ForegroundColor Gray
Write-Host ""
Write-Host "   python mcp_server_http.py $MCPPort" -ForegroundColor Gray
Write-Host "   (Press Ctrl+C after testing)" -ForegroundColor Gray
Write-Host ""

Write-Host "To start the services:" -ForegroundColor Cyan
if ($monitorInstalled) {
    Write-Host "  Start-Service DEXISXRayMonitor" -ForegroundColor Yellow
}
if ($mcpInstalled) {
    Write-Host "  Start-Service DEXISMCPHTTPServer" -ForegroundColor Yellow
}
Write-Host ""
Write-Host "Or use Services.msc to start/stop the services" -ForegroundColor Cyan
Write-Host ""

Write-Host "To check service status:" -ForegroundColor Cyan
if ($monitorInstalled) {
    Write-Host "  Get-Service DEXISXRayMonitor" -ForegroundColor Yellow
}
if ($mcpInstalled) {
    Write-Host "  Get-Service DEXISMCPHTTPServer" -ForegroundColor Yellow
}
Write-Host ""

Write-Host "To view logs:" -ForegroundColor Cyan
if ($monitorInstalled) {
    Write-Host "  Get-Content $InstallPath\xray_monitor.log -Tail 20" -ForegroundColor Yellow
}
if ($mcpInstalled) {
    Write-Host "  Get-Content $InstallPath\mcp_server_http.log -Tail 20" -ForegroundColor Yellow
}
Write-Host ""

