# DEXIS MCP Functionality Verification

**Date:** 2026-05-08  
**Purpose:** Verify DEXIS MCP server connectivity and basic functionality after 6 weeks of no focused commits

## Background

The DEXIS Monitor MCP has been in deployed state with the last focused commit 6 weeks ago (2026-03-27). The log file shows no recent activity, which could indicate either:
1. No x-ray activity during this period
2. Service not running or connection issues
3. Configuration issues

This test verifies basic functionality by testing core MCP tools against the live DEXIS_DATA database.

## Test Script

**File:** `test_dexis_connection.py`

This script runs 5 verification tests:

1. **Database Connection** - Verifies SQL Server connectivity to `(local)\DEXIS_DATA`
2. **List Tables** - Tests schema discovery functionality
3. **Search Patient** - Tests patient search (searches for "Young" per test patient convention)
4. **Recent X-rays** - Tests x-ray retrieval (fetches last 10 x-rays)
5. **X-ray Statistics** - Tests aggregate queries (counts by image type)

## Running the Test

```bash
# From the DEXISMonitor directory
python test_dexis_connection.py
```

The test will:
- Log to both console and `dexis_connection_test.log`
- Display detailed progress for each test
- Provide a summary of passed/failed tests
- Return exit code 0 if all tests pass, 1 if any fail

## Expected Results

**All tests should pass** if:
- SQL Server is running
- DEXIS_DATA instance is accessible
- Windows authentication is configured
- Database contains patient and x-ray data

**Common Failure Modes:**

- **Connection fails**: SQL Server not running or DEXIS_DATA instance not found
- **Authentication fails**: Windows user doesn't have database permissions
- **No data returned**: Database is empty or tables have different schema
- **Import errors**: Missing dependencies (pyodbc)

## Configuration

The test script will load configuration from:
1. `config.json` (production)
2. `config.test.json` (test database - currently configured for `DEXIS_TEST`)

Default connection if no config found:
- Server: `(local)\DEXIS_DATA`
- Database: `DEXIS`
- Authentication: Windows (Trusted Connection)

## Next Steps After Verification

**If tests pass:**
- Monitor log file for recent activity
- Verify MCP HTTP server is running (default port 8843)
- Test via Claude MCP client connection

**If tests fail:**
- Check SQL Server status
- Verify database permissions
- Review connection string in config
- Check DEXIS installation and database name

## Service Information

**NSSM Service Name:** DEXISMonitor (if configured)  
**Log Location:** Check configured log_file in config.json  
**Default Port:** 8843 (HTTP)

## Related Documentation

- `mcp_tools.py` - Available MCP tool functions
- `dexis_db_query.py` - Database connection and query logic
- `config.example.json` - Configuration template
