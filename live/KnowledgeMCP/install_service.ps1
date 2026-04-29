$ServiceName = "KnowledgeMCPServer"
$ScriptName = "mcp_server_http.py"
$InstallPath = Split-Path -Parent $MyInvocation.MyCommand.Path
$PythonExe = Join-Path $InstallPath ".venv\Scripts\python.exe"
$ScriptPath = Join-Path $InstallPath $ScriptName
$LogPath = Join-Path $InstallPath "service_stdout.log"
$ErrPath = Join-Path $InstallPath "service_stderr.log"
$NSSM = "C:\Program Files\nssm\nssm.exe"

if (-not (Test-Path $NSSM)) {
    Write-Error "NSSM not found at $NSSM"
    exit 1
}

if (-not (Test-Path $PythonExe)) {
    Write-Host "Creating venv..."
    python -m venv (Join-Path $InstallPath ".venv")
    & $PythonExe -m pip install --upgrade pip
    & $PythonExe -m pip install -r (Join-Path $InstallPath "requirements.txt")
}

& $NSSM install $ServiceName $PythonExe $ScriptPath
& $NSSM set $ServiceName AppDirectory $InstallPath
& $NSSM set $ServiceName Start SERVICE_AUTO_START
& $NSSM set $ServiceName AppStdout $LogPath
& $NSSM set $ServiceName AppStderr $ErrPath
& $NSSM set $ServiceName AppRotateFiles 1
& $NSSM set $ServiceName AppRotateOnline 1
& $NSSM set $ServiceName AppRotateSeconds 86400
& $NSSM set $ServiceName AppRotateBytes 10485760

$envFile = Join-Path $InstallPath ".env"
if (Test-Path $envFile) {
    $envVars = Get-Content $envFile | Where-Object { $_ -match '=' -and $_ -notmatch '^\s*#' }
    foreach ($line in $envVars) {
        & $NSSM set $ServiceName AppEnvironmentExtra +$line
    }
}

& $NSSM start $ServiceName
Write-Host "Service $ServiceName installed and started."
Write-Host "Token file: $(Join-Path $InstallPath '.mcp_token')"
