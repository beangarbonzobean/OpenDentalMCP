# DEXIS Monitor MCP Server

DEXIS image-monitor MCP server that exposes patient x-ray queries to AI agents via the Model Context Protocol. Connects to the DEXIS_DATA SQL Server database and provides read-only access to patient records and x-ray images.

## Overview

This MCP server provides AI assistants (like Claude Desktop) with tools to query the DEXIS dental imaging database. All queries are **read-only** for safety.

## Available Tools

### Schema Discovery

#### `list_tables`
List all tables in the DEXIS database.

**Parameters:** None

**Returns:** JSON array of tables with schema and table names.

#### `describe_table`
Get column names, types, and structure for a specific table.

**Parameters:**
- `table_name` (string, required): Name of the table to describe

**Returns:** JSON array of column definitions including data types, nullable status, defaults, and constraints.

#### `list_columns`
Get all column names for a specific table.

**Parameters:**
- `table_name` (string, required): Name of the table

**Returns:** JSON array of column names.

---

### Query Execution

#### `execute_query`
Execute a read-only SELECT query with safety validation.

**Parameters:**
- `query` (string, required): SQL SELECT query to execute
- `limit` (integer, optional): Maximum number of rows to return (default: 1000, max: 1000)

**Safety:** Queries are validated to ensure they only contain SELECT statements. Write operations (INSERT, UPDATE, DELETE, etc.) are blocked.

**Returns:** JSON array of result rows.

---

### Patient Search

#### `search_patient`
Find patients by name (supports first name, last name, or full name search).

**Parameters:**
- `name` (string, required): Patient name to search for (partial matches supported)

**Returns:** JSON array of matching patients with:
- `PersonID`: Unique patient identifier
- `FirstName`: Patient first name
- `LastName`: Patient last name
- `PatientName`: Full formatted name

---

### X-ray Query Tools

#### `get_patient_xrays`
Get all x-rays for a specific patient.

**Parameters:** (at least one required)
- `patient_id` (integer, optional): Patient PersonID
- `patient_name` (string, optional): Patient name (first, last, or full)

**Returns:** JSON array of x-rays with:
- `ImageID`: VisualID of the x-ray
- `ImageDate`: Date taken (YYYY-MM-DD)
- `ImageTime`: Time taken (HH:MM:SS)
- `ImageType`: Type classification (Periapical, Bitewing, Panoramic, etc.)
- `ToothNumber`: Associated tooth numbers
- `StudyRecordID`: Study identifier
- `StudyDate`: Full study timestamp
- `PatientID`: Patient PersonID (when searching by name)

#### `get_xrays_by_date`
Get all x-rays taken on a specific date.

**Parameters:**
- `target_date` (string, required): Date in format 'YYYY-MM-DD'

**Returns:** JSON array of x-rays with patient information included.

#### `get_xrays_by_type`
Get x-rays filtered by imaging type.

**Parameters:**
- `xray_type` (string, required): One of:
  - `Periapical`
  - `Bitewing`
  - `Panoramic`
  - `Intraoral Photo`
  - `Extraoral Photo`
- `limit` (integer, optional): Maximum number of results (default: 100)

**Returns:** JSON array of x-rays matching the specified type.

#### `get_xray_info`
Get detailed information for a specific x-ray.

**Parameters:**
- `visual_id` (integer, required): VisualID of the x-ray

**Returns:** JSON object with complete x-ray metadata.

#### `get_recent_xrays`
Get the most recently taken x-rays.

**Parameters:**
- `limit` (integer, optional): Maximum number of x-rays to return (default: 50)

**Returns:** JSON array of recent x-rays sorted by creation date (newest first).

#### `search_xrays`
Advanced search with multiple optional filters.

**Parameters:** (all optional, combine as needed)
- `patient_name` (string): Filter by patient name
- `xray_type` (string): Filter by x-ray type (Periapical, Bitewing, Panoramic, etc.)
- `start_date` (string): Start date filter (YYYY-MM-DD)
- `end_date` (string): End date filter (YYYY-MM-DD)
- `limit` (integer): Maximum number of results (default: 100)

**Returns:** JSON array of x-rays matching all specified filters.

---

### Statistics

#### `get_xray_statistics`
Get aggregate statistics on x-ray counts by type.

**Parameters:** (all optional)
- `start_date` (string): Start date for statistics window (YYYY-MM-DD)
- `end_date` (string): End date for statistics window (YYYY-MM-DD)

**Returns:** JSON array of x-ray counts grouped by image type, ordered by frequency.

---

## Configuration

The server reads configuration from `config.json` (or the path specified in `MCP_CONFIG_FILE` environment variable).

Example configuration:
```json
{
  "database": {
    "server": "(local)\\DEXIS_DATA",
    "database": "DEXIS",
    "use_windows_auth": true
  }
}
```

## Deployment

This MCP server is deployed as an NSSM Windows service. See `install_all_services.ps1` for installation details.

## Logging

Logs are written to `dexis_mcp.log` in the working directory.
