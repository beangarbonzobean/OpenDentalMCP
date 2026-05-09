"""Standalone Flask runner for the utilization dashboard.

For dev / off-hours testing without restarting the production
mcp_server_http.py service. Mounts only this blueprint, on its own port.

Usage:
    python -m utilization_dashboard.standalone [--port 9766] [--host 0.0.0.0]
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# Make sibling packages importable when run as a module from live/OpenDentalMCP
_PKG_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PKG_DIR))

from flask import Flask
from flask_cors import CORS  # type: ignore[import-not-found]

from utilization_dashboard import utilization_bp


def main() -> int:
    p = argparse.ArgumentParser(description="Run the utilization dashboard standalone.")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=9766)
    p.add_argument("--debug", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    app = Flask(__name__)
    CORS(app)
    app.register_blueprint(utilization_bp)
    print(f"utilization dashboard: http://{args.host}:{args.port}/utilization/")
    app.run(host=args.host, port=args.port, debug=args.debug, use_reloader=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
