"""
On-prem preprocessing layer for the Open Dental MCP server.

All modules in this package follow the database safety contract:
- Open Dental's database is read-only. Use sql_safety.assert_select_only()
  on any SQL routed to _query_database.
- The OD image share is read-only. Use Path.read_bytes(), never open(..., "w"),
  never os.remove / shutil.move / shutil.copy on share paths.
- The only writable state lives in live/OpenDentalMCP/data/.
- No schema changes against OD's DB. Ever.

The contract is enforced by tests in tests/test_safety_contract.py.
"""
