"""Tool-registration consistency check across the three places a tool must be
wired for it to be actually reachable end-to-end:

  1. The physical backend's own MCP server (mcp-minierp/mcp-office/...) --
     the tool must exist as a real @mcp.tool() there, or nothing implements it.
  2. governance_core/policy/manifest.py -- the tool must have a ToolPolicy
     (backend tag, risk tier, field classifications), or it has no governance
     and no chat surface can be authorized to call it.
  3. gateway/app.py -- the tool must have a namespaced @mcp.tool() wrapper
     that calls _govern(canonical, ...), or no LLM caller (Home chat, the
     workflow copilot, or any external MCP client hitting the gateway) can
     ever reach it, no matter what #1/#2 say.

This is the check that would have caught, before it shipped instead of after,
the 2026-08-17 gap where get_po_line_items/get_ar_payment_history/
get_ap_payment_history/get_gl_period_summary/get_invoice_line_items/
get_bill_line_items/get_customer_invoice_history/get_item_movement_history
were fully implemented in mcp-minierp and fully classified in manifest.py, but
had no gateway/app.py wrapper -- so they were grantable in "My Access" and
callable from the admin Try-Tool playground (which calls _govern directly,
bypassing the gateway's own MCP surface), but invisible to Home chat, the
workflow copilot, and any real MCP client. Run this after adding, renaming, or
moving any tool.

Each physical backend is queried via a fresh subprocess with its own cwd (not
an in-process import) for two reasons: every backend's entrypoint is literally
named app.py, so importing more than one in the same process would just return
the first cached module; and each backend's own relative ".env.local" load
(see e.g. mcp-minierp/app.py) only resolves correctly from its own directory,
exactly like when it's actually deployed.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(ROOT))

from governance_core.policy import manifest  # noqa: E402

# manifest.py's `backend` tag is a logical domain, not a physical process --
# minierp_orders/accounts/shipments/finance/analytics are all served by the one
# consolidated mcp-minierp process. Add an entry here whenever a new physical
# backend or a new logical tag on an existing one is introduced.
BACKEND_DIRS: dict[str, str] = {
    "minierp_orders": "mcp-minierp",
    "minierp_accounts": "mcp-minierp",
    "minierp_shipments": "mcp-minierp",
    "minierp_finance": "mcp-minierp",
    "minierp_analytics": "mcp-minierp",
    "office": "mcp-office",
    "email": "mcp-email",
    "calendar": "mcp-calendar",
    "knowledge": "mcp-knowledge",
    "code": "mcp-code",
}
GATEWAY_DIR = "gateway"

# Gateway tools with no backend/manifest entry by design (workflow-authoring
# meta tools -- scratchpad reads/writes and draft-graph proposals, not data
# lookups; see gateway/app.py's own docstrings on each). Adding a new one here
# is a deliberate, visible decision, not a silent exemption -- anything NOT in
# this set must resolve through manifest.canonical() or the check below fails.
KNOWN_GATEWAY_META_TOOLS = {
    "submit_workflow_request",
    "update_workflow_plan",
    "list_my_workflows",
    "get_my_workflow",
    "propose_graph",
}

_LIST_TOOLS_SNIPPET = (
    "import asyncio, json\n"
    "import app as _mod\n"
    "async def _main():\n"
    "    tools = await _mod.mcp.list_tools()\n"
    "    print(json.dumps(sorted(t.name for t in tools)))\n"
    "asyncio.run(_main())\n"
)

PASS, FAIL = 0, 0


def check(name, cond, detail=None) -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}", str(detail)[:600] if detail is not None else "")


def _list_tools(directory: Path) -> tuple[set[str] | None, str]:
    """Run `directory`'s app.py in a fresh subprocess and return its
    registered tool names, or (None, error) if it couldn't be imported/run."""
    try:
        proc = subprocess.run(
            [sys.executable, "-c", _LIST_TOOLS_SNIPPET],
            cwd=str(directory), capture_output=True, text=True, timeout=60,
        )
    except subprocess.TimeoutExpired as exc:
        return None, f"timed out: {exc}"
    if proc.returncode != 0:
        return None, proc.stderr[-2000:]
    try:
        return set(json.loads(proc.stdout.strip().splitlines()[-1])), ""
    except Exception as exc:  # noqa: BLE001
        return None, f"could not parse output ({exc}): stdout={proc.stdout!r} stderr={proc.stderr[-500:]!r}"


def main() -> int:
    dirs_to_check = sorted(set(BACKEND_DIRS.values())) + [GATEWAY_DIR]
    tools_by_dir: dict[str, set[str]] = {}
    for d in dirs_to_check:
        names, err = _list_tools(ROOT / d)
        check(f"{d}/app.py imports cleanly and lists its tools", names is not None, err)
        tools_by_dir[d] = names or set()

    gateway_tools = tools_by_dir[GATEWAY_DIR]

    # ── Forward: every manifest tool is implemented in its backend AND has a
    # gateway wrapper. This second half is exactly the check that was missing
    # on 2026-08-17. ──────────────────────────────────────────────────────────
    for canonical, policy in sorted(manifest.TOOL_POLICIES.items()):
        backend_dir = BACKEND_DIRS.get(policy.backend)
        if backend_dir is None:
            check(f"{canonical}: manifest backend tag {policy.backend!r} maps to a known physical directory",
                  False, "add it to BACKEND_DIRS in this script")
            continue
        check(f"{canonical}: implemented in {backend_dir}/app.py", canonical in tools_by_dir[backend_dir])
        expected_gateway_name = manifest.namespaced(canonical)
        check(f"{canonical}: has a gateway wrapper ({expected_gateway_name})",
              expected_gateway_name in gateway_tools,
              "not found among gateway/app.py's registered tools -- no LLM caller can reach this tool")

    # ── Reverse #1: every gateway tool either is a known meta-tool, or resolves
    # to a real manifest entry backed by an actual backend implementation.
    # Catches an orphaned gateway wrapper (governance bypassed entirely) or one
    # calling a tool the backend never registered (would fail at call time). ──
    for name in sorted(gateway_tools):
        if name in KNOWN_GATEWAY_META_TOOLS:
            continue
        canon = manifest.canonical(name)
        check(f"gateway tool {name}: resolves to a manifest entry", canon is not None,
              "add a ToolPolicy for it, or add it to KNOWN_GATEWAY_META_TOOLS if deliberately ungoverned")
        if canon is None:
            continue
        policy = manifest.TOOL_POLICIES[canon]
        backend_dir = BACKEND_DIRS.get(policy.backend)
        check(f"gateway tool {name}: backend {policy.backend!r} actually implements {canon}",
              backend_dir is not None and canon in tools_by_dir.get(backend_dir, set()),
              "gateway wrapper calls a tool the backend never registered")

    # ── Reverse #2: every tool a physical backend implements is governed by the
    # manifest. An implemented-but-ungoverned tool can't be reached by any
    # grant (so it's not a security hole today) but is exactly the kind of
    # silent drift this script exists to catch before it becomes one. ────────
    canonical_by_dir: dict[str, set[str]] = {}
    for canonical, policy in manifest.TOOL_POLICIES.items():
        d = BACKEND_DIRS.get(policy.backend)
        if d is not None:
            canonical_by_dir.setdefault(d, set()).add(canonical)
    for d in sorted(set(BACKEND_DIRS.values())):
        extra = tools_by_dir.get(d, set()) - canonical_by_dir.get(d, set())
        check(f"{d}/app.py has no tools missing from the manifest", not extra, sorted(extra))

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
