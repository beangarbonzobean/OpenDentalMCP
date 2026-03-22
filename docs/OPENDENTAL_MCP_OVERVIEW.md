# Open Dental MCP Server

MCP (Model Context Protocol) server for Open Dental REST API access. Provides AI agents with access to Open Dental practice management data.

## Features

- **MCP Protocol Support**: Both stdio and HTTP/HTTPS modes
- **Comprehensive API Access**: All Open Dental REST API endpoints
- **Pre-built Tools**: Common queries (search patients, appointments, etc.)
- **Windows Service**: Runs 24/7 as Windows Service
- **Secure**: HTTPS with self-signed certificates
- **Remote Management**: PowerShell scripts for deployment

## Installation

### Prerequisites

1. **Python 3.7+** installed
2. **NSSM** (Non-Sucking Service Manager) - Download from https://nssm.cc/download
3. **Open Dental API Keys**:
   - `OPENDENTAL_DEVELOPER_KEY`
   - `OPENDENTAL_CUSTOMER_KEY`
   - `OPENDENTAL_API_URL` (optional, defaults to https://api.opendental.com/api/v1)

### Quick Install

1. **Install Python dependencies**:
   ```powershell
   pip install -r requirements.txt
   ```

2. **Configure environment variables**:
   Create a `.env` file in the `opendental-mcp` directory:
   ```env
   OPENDENTAL_API_URL=https://api.opendental.com/api/v1
   OPENDENTAL_DEVELOPER_KEY=your_developer_key
   OPENDENTAL_CUSTOMER_KEY=your_customer_key
   MCP_HTTP_PORT=8444
   MCP_HTTP_HOST=0.0.0.0
   MCP_USE_HTTPS=true
   ```

3. **Install as Windows Service**:
   ```powershell
   .\install_service.ps1
   ```

## Usage

### Stdio Mode (for Claude Desktop)

Run directly:
```bash
python mcp_server.py
```

### HTTP Mode (for other AI agents)

Run directly:
```bash
python mcp_server_http.py
```

Or access via service:
- HTTPS: `https://localhost:8444/mcp`
- Health check: `https://localhost:8444/health`

## Available Tools

### Resource Discovery
- `list_resources` - List all available Open Dental API resources

### Patient Tools
- `get_patient` - Get patient by ID
- `search_patients` - Search patients by name, phone, email, etc.

### Appointment Tools
- `get_appointment` - Get appointment by ID
- `search_appointments` - Search appointments by patient, date, status

### Provider Tools
- `get_provider` - Get provider by ID
- `list_providers` - List all providers

### Laboratory Tools
- `get_laboratory` - Get laboratory by ID
- `list_laboratories` - List all laboratories

### Lab Case Tools
- `get_lab_cases` - Get lab cases for a patient

### Document Tools
- `get_document` - Get document by ID
- `get_patient_documents` - Get documents for a patient

### Procedure Tools
- `get_procedure_codes` - List procedure code definitions

### Statistics
- `get_statistics` - Get practice statistics (patient count, appointment count, etc.)

## Service Management

### Start Service
```powershell
Start-Service OpenDentalMCPServer
```

### Stop Service
```powershell
Stop-Service OpenDentalMCPServer
```

### Check Status
```powershell
Get-Service OpenDentalMCPServer
```

### View Logs
```powershell
Get-Content C:\OpenDentalMCP\opendental_mcp_http.log -Tail 20
Get-Content C:\OpenDentalMCP\service_stderr.log -Tail 20
```

## Configuration

Edit `config.json` or set environment variables:

- `OPENDENTAL_API_URL` - Open Dental API base URL
- `OPENDENTAL_DEVELOPER_KEY` - Developer API key
- `OPENDENTAL_CUSTOMER_KEY` - Customer API key
- `MCP_HTTP_PORT` - HTTP server port (default: 8444; use 8443 only if no port conflict with DEXIS MCP)
- `MCP_HTTP_HOST` - HTTP server host (default: 0.0.0.0)
- `MCP_USE_HTTPS` - Use HTTPS (default: true)

## Testing

### Test HTTP Server
```powershell
# Health check
Invoke-WebRequest -Uri "https://localhost:8444/health" -SkipCertificateCheck

# List tools
$body = @{
    jsonrpc = "2.0"
    id = 1
    method = "tools/list"
    params = @{}
} | ConvertTo-Json

Invoke-WebRequest -Uri "https://localhost:8444/mcp" -Method POST -Body $body -ContentType "application/json" -SkipCertificateCheck
```

## Troubleshooting

### Service won't start
1. Check logs: `C:\OpenDentalMCP\service_stderr.log`
2. Verify Python path: `Get-Command python`
3. Verify API keys are set in environment variables
4. Check port 8444 is not in use: `netstat -ano | findstr :8444`

### API errors
1. Verify API keys are correct
2. Check API URL is correct
3. Check network connectivity to Open Dental API
4. Review logs for detailed error messages

### Certificate errors
- Self-signed certificates are generated automatically
- For production, replace `cert.pem` and `key.pem` with proper certificates

## Architecture

Similar to DEXIS MCP Server:
- **mcp_server.py** - Stdio-based server (for Claude Desktop)
- **mcp_server_http.py** - HTTP-based server (for other AI agents)
- **mcp_tools.py** - Tool implementations
- **config.json** - Configuration file
- **install_service.ps1** - Service installation script

## Next Steps

1. Test all endpoints
2. Add more specialized tools as needed
3. Set up monitoring/alerting
4. Configure proper SSL certificates for production

