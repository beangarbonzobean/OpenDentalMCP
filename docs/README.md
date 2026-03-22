# Documentation index

| Document | Purpose |
|----------|---------|
| [OPENDENTAL_MCP_OVERVIEW.md](./OPENDENTAL_MCP_OVERVIEW.md) | Features, tools, HTTP/stdio usage |
| [DEPLOY_TO_SERVER.md](./DEPLOY_TO_SERVER.md) | Deploy OpenDental MCP to `C:\OpenDentalMCP` (includes Git workflow) |
| [DEPLOYMENT_INSTRUCTIONS.md](./DEPLOYMENT_INSTRUCTIONS.md) | Short deployment checklist |
| [SERVICE_SETUP.md](./SERVICE_SETUP.md) | Windows service (NSSM) details |
| [UPDATE_INSTRUCTIONS.md](./UPDATE_INSTRUCTIONS.md) | Updating an existing install |
| [PORT_CONFLICT_FIX.md](./PORT_CONFLICT_FIX.md) | DEXIS vs OpenDental both on port 8443 |
| [scripts/fix_port_conflict.ps1](./scripts/fix_port_conflict.ps1) | Automates moving OpenDental MCP to port 8444 |

DEXIS stack (`live/DEXISMonitor/`): use the same pattern—copy to `C:\DEXISMonitor`, configure `config.json` from `config.example.json`, run `install_all_services.ps1` as documented in that folder’s scripts.
