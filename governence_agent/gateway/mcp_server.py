"""The FastMCP server instance, on its own so both planes can reach it.

app.py registers the @mcp.tool definitions on it and mounts its ASGI app; the
dashboard also needs it — backend/chat.py hands it to the orchestrator so the
assistant can call governed tools, and backend/workflow_graphs.py calls
mcp.list_tools() to build the graph editor's tool catalogue.

Keeping it here rather than in app.py is what stops that from being a circular
import (app.py imports backend, backend needs mcp).

Note the module name: `mcp_server`, not `mcp` — `mcp` is the installed SDK
package that FastMCP itself is imported from.
"""
from __future__ import annotations

import os

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings


# ── MCP server + transport security (same host-allowlist rationale as backend) ─

def _allowed_hosts() -> list[str]:
    raw = (os.getenv("GATEWAY_ALLOWED_HOSTS") or os.getenv("GOVERNANCE_ALLOWED_HOSTS") or "").strip()
    hosts = ["localhost", "localhost:*", "127.0.0.1", "127.0.0.1:*"]
    for h in raw.split(","):
        h = h.strip()
        if h:
            hosts.extend((h, f"{h}:*"))
    return hosts


_ALLOWED_HOSTS = _allowed_hosts()

mcp = FastMCP(
    "frontier-governance-gateway",
    stateless_http=True,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=_ALLOWED_HOSTS,
        allowed_origins=[f"https://{h}" for h in _ALLOWED_HOSTS if ":" not in h and not h.startswith(("localhost", "127."))]
        + ["http://localhost", "http://localhost:*", "http://127.0.0.1", "http://127.0.0.1:*"],
    ),
)
