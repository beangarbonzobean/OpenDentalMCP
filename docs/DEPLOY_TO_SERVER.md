# Deploy Open Dental MCP Server to Server Computer

## Overview

This guide will help you deploy the Open Dental MCP Server to your server computer (not the current development machine).

## Prerequisites on Server

Before deploying, ensure your server has:

1. **Python 3.7+** installed
2. **NSSM** (Non-Sucking Service Manager) - can be installed via winget
3. **Network access** to:
   - Open Dental database (`YOUR_OPENDENTAL_DB_HOST`)
   - Open Dental AtoZ folder (`\\YOUR_FILE_SERVER\OpenDentImages`)
   - Open Dental REST API

## Deployment Methods

### Method 1: From this Git repository (recommended)

1. **Clone** this repository on the machine where you edit code (or on the server).

2. **Copy** everything under **`live/OpenDentalMCP/`** to **`C:\OpenDentalMCP`** on the target server (merge/replace files as needed).

3. **`config.json`** is not committed (see `config.example.json`). On first setup, copy `config.example.json` to `config.json` and adjust. If the server already has a working `config.json`, keep it when updating code.

4. **`.env`** is not in Git. Create it on the server from **`env_template_for_server.txt`** or your password manager.

5. **TLS:** Deploy `cert.pem` / `key.pem` (or your real certs) outside of Git.

6. **On the server**, open PowerShell as Administrator:
   ```powershell
   cd C:\OpenDentalMCP
   pip install -r requirements.txt
   .\install_service.ps1
   ```

**Ongoing updates:** Pull the latest commit, copy updated files from `live/OpenDentalMCP/` to `C:\OpenDentalMCP`, then restart the Windows service (see [UPDATE_INSTRUCTIONS.md](./UPDATE_INSTRUCTIONS.md)).

### Method 2: Network share

1. Share your working tree or a zip of `live/OpenDentalMCP`.
2. On the server, copy into `C:\OpenDentalMCP` and continue from step 3 in Method 1.

### Method 3: Remote deployment script

If you use a custom `deploy_to_server.ps1`, run it per your internal process.

## Files to Deploy

Copy these files/folders to your server:

### Required Files:
- `mcp_server_http.py` - HTTP MCP server
- `mcp_tools.py` - MCP tools implementation
- `requirements.txt` - Python dependencies
- `.env` - Configuration file (with all your settings)
- `install_service.ps1` - Service installation script
- `check_service_status.ps1` - Service status checker
- `uninstall_service.ps1` - Service uninstaller

### Optional Files:
- `mcp_server.py` - Stdio MCP server (if needed)
- `config.json` - Copy from `config.example.json` locally (not in Git)
- `OPENDENTAL_MCP_OVERVIEW.md` - Documentation (in `docs/`)
- `SERVICE_SETUP.md` - Service setup guide

## Server Configuration

### Step 1: Install Prerequisites on Server

**Install Python:**
```powershell
# Check if Python is installed
python --version

# If not installed, install via winget
winget install Python.Python.3.11
```

**Install NSSM:**
```powershell
winget install NSSM.NSSM
```

### Step 2: Verify Network Access

On the server, verify access to:

**Database:**
```powershell
# Test MySQL connection (if you have mysql client)
mysql -h YOUR_OPENDENTAL_DB_HOST -u root -e "SELECT 1"
```

**AtoZ Folder:**
```powershell
# Test network share access
Test-Path "\\YOUR_FILE_SERVER\OpenDentImages"
```

**Open Dental API:**
```powershell
# Test API connectivity
Invoke-WebRequest -Uri "https://api.opendental.com/api/v1/providers" -Headers @{"Authorization"="ODFHIR YOUR_KEY/YOUR_KEY"}
```

### Step 3: Configure .env File on Server

Make sure the `.env` file on the server has the correct paths:

```env
# Open Dental API Configuration
OPENDENTAL_API_URL=https://api.opendental.com/api/v1
OPENDENTAL_DEVELOPER_KEY=YOUR_OPENDENTAL_DEVELOPER_KEY
OPENDENTAL_CUSTOMER_KEY=YOUR_OPENDENTAL_CUSTOMER_KEY

# Database Configuration
OPENDENTAL_DB_TYPE=mysql
OPENDENTAL_DB_SERVER=YOUR_OPENDENTAL_DB_HOST
OPENDENTAL_DB_DATABASE=opendental
OPENDENTAL_DB_USERNAME=root
OPENDENTAL_DB_PASSWORD=
OPENDENTAL_DB_USE_WINDOWS_AUTH=false

# AtoZ Path (network share)
OPENDENTAL_ATOZ_PATH=\\YOUR_FILE_SERVER\OpenDentImages

# MCP Server Configuration (use 8444 if DEXIS MCP uses 8443 on the same host)
MCP_HTTP_PORT=8444
MCP_HTTP_HOST=0.0.0.0
MCP_USE_HTTPS=true
```

**Important:** Verify that:
- Database server name is correct (`YOUR_OPENDENTAL_DB_HOST`)
- AtoZ path is accessible from server (`\\YOUR_FILE_SERVER\OpenDentImages`)
- API keys are correct

### Step 4: Install Service on Server

1. **Open PowerShell as Administrator** on the server

2. **Navigate to deployment directory:**
   ```powershell
   cd C:\OpenDentalMCP
   ```

3. **Install Python dependencies:**
   ```powershell
   pip install -r requirements.txt
   ```

4. **Test the server manually first:**
   ```powershell
   python mcp_server_http.py
   ```
   - Press Ctrl+C to stop after verifying it starts

5. **Install as Windows Service:**
   ```powershell
   .\install_service.ps1
   ```

6. **Verify service is running:**
   ```powershell
   .\check_service_status.ps1
   ```

## Post-Installation Verification

### Check Service Status
```powershell
Get-Service -Name "OpenDentalMCPServer"
```

### Test Health Endpoint
```powershell
Invoke-WebRequest -Uri "https://localhost:8444/health" -SkipCertificateCheck
```

### Check Service Logs
```powershell
# View recent logs
Get-Content C:\OpenDentalMCP\service_stdout.log -Tail 50
Get-Content C:\OpenDentalMCP\service_stderr.log -Tail 50
```

## Troubleshooting

### Service Won't Start

1. **Check logs:**
   ```powershell
   Get-Content C:\OpenDentalMCP\service_stderr.log -Tail 50
   ```

2. **Verify environment variables:**
   ```powershell
   Get-Content C:\OpenDentalMCP\.env
   ```

3. **Test database connection:**
   ```powershell
   cd C:\OpenDentalMCP
   python test_db_connection.py
   ```

### Network Share Not Accessible

If the AtoZ network share isn't accessible:

1. **Verify network connectivity:**
   ```powershell
   Test-Connection YOUR_OPENDENTAL_DB_HOST
   ```

2. **Check share permissions:**
   - Ensure the service account has access to `\\YOUR_FILE_SERVER\OpenDentImages`
   - The service runs as the SYSTEM account by default

3. **Map network drive (if needed):**
   - You may need to configure the service to use a mapped drive
   - Or use UNC path directly (recommended)

### Database Connection Issues

1. **Test MySQL connection:**
   ```powershell
   # If mysql client is installed
   mysql -h YOUR_OPENDENTAL_DB_HOST -u root -e "SELECT 1"
   ```

2. **Verify firewall:**
   - Ensure MySQL port (3306) is open between server and `YOUR_OPENDENTAL_DB_HOST`

3. **Check credentials:**
   - Verify username/password in `.env` file

## Service Management on Server

### Start Service
```powershell
Start-Service -Name "OpenDentalMCPServer"
```

### Stop Service
```powershell
Stop-Service -Name "OpenDentalMCPServer"
```

### Restart Service
```powershell
Restart-Service -Name "OpenDentalMCPServer"
```

### View Service Status
```powershell
.\check_service_status.ps1
```

## Accessing MCP Server from Other Machines

Once installed on the server, other machines can access the MCP server at:

- **HTTPS:** `https://SERVER_IP:8444/mcp` (or the port in your `.env`)
- **Health Check:** `https://SERVER_IP:8444/health`

**Note:** Ensure Windows Firewall allows that port. If both DEXIS and OpenDental MCP run on one machine, avoid both using **8443** — see [PORT_CONFLICT_FIX.md](./PORT_CONFLICT_FIX.md).

## Updating the Service

To update the service on the server:

1. **Stop the service:**
   ```powershell
   Stop-Service -Name "OpenDentalMCPServer"
   ```

2. **Copy updated files** to `C:\OpenDentalMCP`

3. **Update Python dependencies** (if needed):
   ```powershell
   pip install -r requirements.txt --upgrade
   ```

4. **Restart the service:**
   ```powershell
   Start-Service -Name "OpenDentalMCPServer"
   ```

## Security Considerations

1. **Firewall:** Only open the MCP port (e.g. 8444) if you need external access
2. **SSL Certificate:** The service uses a self-signed certificate by default
3. **Service Account:** Service runs as SYSTEM account (has full system access)
4. **Database Credentials:** Stored in `.env` file - ensure file permissions are secure

## Next Steps

After successful deployment:

1. ✅ Verify service is running
2. ✅ Test health endpoint
3. ✅ Test MCP endpoint
4. ✅ Configure your AI agents to use the server's MCP endpoint
5. ✅ Monitor logs for any issues

