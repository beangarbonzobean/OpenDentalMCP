"""Utilization dashboard.

Tracks pre-paid AI capacity (Claude Max session window + weekly caps,
Anthropic API extra-usage spend) and local-GPU/Ollama state, plus the
overnight OCR pipeline rate. The goal is to keep the user from leaving
quota unused — pre-paid capacity that resets unused is wasted spend.

Mounts under /utilization on the main MCP server.

Phase 1: dashboard only (this file).
Phase 2 (separate package): inference router that prefers idle pre-paid
capacity over billable APIs.
"""

from utilization_dashboard.routes import utilization_bp

__all__ = ["utilization_bp"]
