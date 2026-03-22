# Fix .env file on server for Open Dental MCP Server
# Interactive script to create or update .env file

$ErrorActionPreference = "Stop"

$ServiceName = "OpenDentalMCPServer"
$InstallPath = "C:\OpenDentalMCP"
$envFile = Join-Path $InstallPath ".env"

Write-Host "Open Dental MCP Server - .env Configuration Fix" -ForegroundColor Cyan
Write-Host ("=" * 60) -ForegroundColor Cyan
Write-Host ""

# Check if .env file already exists
if (Test-Path $envFile) {
    Write-Host "Existing .env file found at: $envFile" -ForegroundColor Yellow
    $overwrite = Read-Host "Overwrite existing .env file? (y/N)"
    if ($overwrite -ne "y" -and $overwrite -ne "Y") {
        Write-Host "Keeping existing .env file." -ForegroundColor Green
        exit 0
    }
}

Write-Host ""
Write-Host "Enter configuration values (press Enter to use defaults):" -ForegroundColor Yellow
Write-Host ""

# Open Dental API Configuration
$apiUrl = Read-Host "Open Dental API URL [https://api.opendental.com/api/v1]"
if ([string]::IsNullOrWhiteSpace($apiUrl)) { $apiUrl = "https://api.opendental.com/api/v1" }

$devKey = Read-Host "Developer Key"
if ([string]::IsNullOrWhiteSpace($devKey)) { $devKey = "" }

$custKey = Read-Host "Customer Key"
if ([string]::IsNullOrWhiteSpace($custKey)) { $custKey = "" }

# Database Configuration
Write-Host ""
Write-Host "Database Configuration:" -ForegroundColor Cyan
$dbType = Read-Host "Database Type (mysql/sqlserver) [mysql]"
if ([string]::IsNullOrWhiteSpace($dbType)) { $dbType = "mysql" }

$dbServer = Read-Host "Database Server [YOUR_OPENDENTAL_DB_HOST]"
if ([string]::IsNullOrWhiteSpace($dbServer)) { $dbServer = "YOUR_OPENDENTAL_DB_HOST" }

$dbDatabase = Read-Host "Database Name [opendental]"
if ([string]::IsNullOrWhiteSpace($dbDatabase)) { $dbDatabase = "opendental" }

$dbUsername = Read-Host "Database Username [root]"
if ([string]::IsNullOrWhiteSpace($dbUsername)) { $dbUsername = "root" }

$dbPassword = Read-Host "Database Password []" -AsSecureString
$dbPasswordPlain = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($dbPassword))

$useWindowsAuth = Read-Host "Use Windows Authentication? (true/false) [false]"
if ([string]::IsNullOrWhiteSpace($useWindowsAuth)) { $useWindowsAuth = "false" }

# AtoZ Path
Write-Host ""
Write-Host "AtoZ Path Configuration:" -ForegroundColor Cyan
$atozPath = Read-Host "AtoZ Path [\\YOUR_FILE_SERVER\OpenDentImages]"
if ([string]::IsNullOrWhiteSpace($atozPath)) { $atozPath = "\\YOUR_FILE_SERVER\OpenDentImages" }

# MCP Server Configuration
Write-Host ""
Write-Host "MCP Server Configuration:" -ForegroundColor Cyan
$httpPort = Read-Host "HTTP Port [8444]"
if ([string]::IsNullOrWhiteSpace($httpPort)) { $httpPort = "8444" }

$httpHost = Read-Host "HTTP Host [0.0.0.0]"
if ([string]::IsNullOrWhiteSpace($httpHost)) { $httpHost = "0.0.0.0" }

$useHttps = Read-Host "Use HTTPS? (true/false) [true]"
if ([string]::IsNullOrWhiteSpace($useHttps)) { $useHttps = "true" }

# Create .env file content
$envContent = @"
# Open Dental API Configuration
OPENDENTAL_API_URL=$apiUrl
OPENDENTAL_DEVELOPER_KEY=$devKey
OPENDENTAL_CUSTOMER_KEY=$custKey

# Database Configuration
OPENDENTAL_DB_TYPE=$dbType
OPENDENTAL_DB_SERVER=$dbServer
OPENDENTAL_DB_DATABASE=$dbDatabase
OPENDENTAL_DB_USERNAME=$dbUsername
OPENDENTAL_DB_PASSWORD=$dbPasswordPlain
OPENDENTAL_DB_USE_WINDOWS_AUTH=$useWindowsAuth

# AtoZ Path
OPENDENTAL_ATOZ_PATH=$atozPath

# MCP Server Configuration
MCP_HTTP_PORT=$httpPort
MCP_HTTP_HOST=$httpHost
MCP_USE_HTTPS=$useHttps
"@

# Write .env file
Write-Host ""
Write-Host "Creating .env file..." -ForegroundColor Yellow
$envContent | Out-File -FilePath $envFile -Encoding ASCII
Write-Host "[OK] .env file created at: $envFile" -ForegroundColor Green

# Update service with environment variables
Write-Host ""
Write-Host "Updating service with environment variables..." -ForegroundColor Yellow

# Find NSSM
$nssmPath = "C:\Program Files\nssm\nssm.exe"
if (-not (Test-Path $nssmPath)) {
    $wingetNssm = Get-ChildItem -Path "$env:LOCALAPPDATA\Microsoft\WinGet\Packages" -Filter "nssm.exe" -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty FullName
    if ($wingetNssm -and (Test-Path $wingetNssm)) {
        $nssmPath = $wingetNssm
    } else {
        Write-Host "[ERROR] NSSM not found. Cannot update service." -ForegroundColor Red
        exit 1
    }
}

# Load environment variables from .env
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

# Clear existing environment variables first (NSSM limitation - we need to set them all)
# Set all environment variables
foreach ($envVar in $envVars) {
    & $nssmPath set $ServiceName AppEnvironmentExtra $envVar
}

Write-Host "[OK] Loaded $($envVars.Count) environment variables into service" -ForegroundColor Green

# Restart service
Write-Host ""
Write-Host "Restarting service..." -ForegroundColor Yellow
Restart-Service -Name $ServiceName -Force
Start-Sleep -Seconds 2

$service = Get-Service -Name $ServiceName
if ($service.Status -eq "Running") {
    Write-Host "[OK] Service restarted successfully!" -ForegroundColor Green
    Write-Host ""
    Write-Host "Configuration complete!" -ForegroundColor Green
    Write-Host "Service: $ServiceName" -ForegroundColor Cyan
    Write-Host "Status: $($service.Status)" -ForegroundColor Cyan
    Write-Host "HTTP Server: https://localhost:$httpPort" -ForegroundColor Cyan
} else {
    Write-Host "[ERROR] Service not running. Status: $($service.Status)" -ForegroundColor Red
    Write-Host "Check logs at: $InstallPath\service_stderr.log" -ForegroundColor Yellow
}

