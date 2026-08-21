"""Shared, process-cached lookup for a governed tool's real outputSchema.

Used by two call sites that must never disagree about which tools are
"typed" (have a real, named-field outputSchema) vs not:
  - get_field_catalog (app.py) -- the workflow copilot's free field-shape
    lookup, so it doesn't need a live data call just to learn field names.
  - _govern's build-mode gating (govern.py) -- decides whether a bulk data
    call during workflow authoring is a genuine deliberate fetch (tool
    already has a known schema, so any real call to it is presumably on
    purpose) or still needs a small forced sample first (no schema yet, so
    the model may be probing for shape via real data, same as before
    get_field_catalog existed).

One cache, one "is this schema real" rule, kept in its own module so neither
app.py nor govern.py has to import the other.
"""
from __future__ import annotations

import mcp_clients

_PRIMITIVE_JSON_TYPES = {"string", "number", "integer", "boolean", "null"}

_CACHE: dict[str, dict | None] = {}


def is_real_schema(schema: dict | None) -> bool:
    """FastMCP auto-generates a trivial {"result": {"type": "string"}}-style
    wrapper schema for every tool whose return annotation is a bare scalar
    (e.g. a still-untyped `-> str` tool) -- confirmed by direct inspection,
    not documented anywhere. That's not a real field catalog; treat it the
    same as no schema at all rather than reporting a fake "result" field."""
    if not schema:
        return False
    props = schema.get("properties") or {}
    if set(props.keys()) == {"result"} and isinstance(props.get("result"), dict):
        return props["result"].get("type") not in _PRIMITIVE_JSON_TYPES
    return True


async def get_output_schema(backend: str, tool: str) -> dict | None:
    """Real outputSchema for `tool` on `backend`, or None if it doesn't have
    a usable one. Cached for the life of this process -- a tool's schema
    only changes on a backend redeploy, which restarts this gateway too."""
    if tool not in _CACHE:
        try:
            schema = await mcp_clients.get_tool_schema(backend, tool)
        except mcp_clients.BackendError:
            schema = None
        _CACHE[tool] = schema if is_real_schema(schema) else None
    return _CACHE[tool]
