# Open Dental MCP Server — deployment checklist

## What's included

Use the **`live/OpenDentalMCP/`** folder from this repository as the source tree (deploy to e.g. `C:\OpenDentalMCP`).

## Installation steps

1. **Copy** `live/OpenDentalMCP/` to your server (e.g. `C:\OpenDentalMCP`).

2. **On the server**, open PowerShell as Administrator.

3. **Navigate to the install folder:**
   ```powershell
   cd C:\OpenDentalMCP
   ```

4. **Install Python dependencies:**
   ```powershell
   pip install -r requirements.txt
   ```

5. **Configure** `config.json` from `config.example.json` and create **`.env`** (not in Git).

6. **Test the server manually** (optional):
   ```powershell
   python mcp_server_http.py
   ```
   Press Ctrl+C to stop.

7. **Install as Windows Service:**
   ```powershell
   .\install_service.ps1
   ```

8. **Verify service is running:**
   ```powershell
   .\check_service_status.ps1
   ```

## Prerequisites on Server

- Python 3.7+ installed
- NSSM installed (can install via: winget install NSSM.NSSM)
- Network access to:
  - Open Dental database (YOUR_OPENDENTAL_DB_HOST)
  - Open Dental AtoZ folder (\\YOUR_FILE_SERVER\OpenDentImages)
  - Open Dental REST API

## Configuration

The .env file contains all configuration. Verify these settings on the server:

- OPENDENTAL_DB_SERVER=YOUR_OPENDENTAL_DB_HOST (should be accessible from server)
- OPENDENTAL_ATOZ_PATH=\\YOUR_FILE_SERVER\OpenDentImages (should be accessible from server)
- OPENDENTAL_DEVELOPER_KEY and OPENDENTAL_CUSTOMER_KEY (should be correct)

## Documentation

See [DEPLOY_TO_SERVER.md](./DEPLOY_TO_SERVER.md) for detailed deployment instructions.

## Support

If you encounter issues:
1. Check service logs: C:\OpenDentalMCP\service_stderr.log
2. Verify network connectivity to YOUR_OPENDENTAL_DB_HOST
3. Test database connection: python test_db_connection.py
