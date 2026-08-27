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

Also checks CONTENT drift across the three independently hand-written
descriptions every tool carries (backend @mcp.tool() docstring, manifest.py's
ToolPolicy.description, gateway @mcp.tool() docstring -- the ONLY one an LLM
caller ever sees, since the orchestrator only calls .list_tools() on the
gateway's own mcp object). Nothing enforced these stayed in sync before
2026-08-19, when two real cases were found: knowledge_search_knowledge's
gateway/manifest descriptions both said "local" documents after the backend
grew a direct-Azure/HTTP-proxy tier ahead of local fallback (stale fact, not
carried forward), and get_customer_order_recency's gateway docstring pointed
readers at "the underlying tool's own docstring" for a phone-number caveat that
didn't exist in that docstring at all -- the real caveat was three files deep,
in an analytics.py code comment, and the LLM can never chase a cross-file
pointer since it only ever sees the one string the gateway hands it. These
checks catch the two mechanical symptoms of that class of bug: a dead
"see ... docstring" pointer, and a safety/accuracy caveat present in
manifest.py's description but silently absent from the gateway's. They do NOT
catch every possible stale fact (e.g. a data-source rewrite with no caveat
keyword) -- that class still wants a human skim when a backend's own
docstring changes materially. See ToolPolicy.description's own field comment
in manifest.py for why that description is deliberately a DIFFERENT audience/
wording from the gateway's LLM-facing one (a non-technical self-service-UI
one-liner vs. LLM tool-selection guidance) -- these checks compare for dropped
substance, never for exact-text equality.
"""
from __future__ import annotations

import json
import re
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
    "get_field_catalog",
}

# The "Data Aggregation" tool family (STAGE2_PLAN.md SS10.2): pure local
# computation over numbers/rows the caller already has in hand -- no backend
# call, no _govern(...), nothing to redact or authorize, so a ToolPolicy would
# have no `backend`/`fields` to attach to. Documented there as deliberately
# having "no backend tag" (SS10.2's own wording), built 2026-08-18, but never
# added here until 2026-08-19 -- this script's Reverse #1 check was already
# correct and would have flagged them the moment they shipped; nobody ran it
# (or noticed the failure) at the time. Same rule as the meta tools above:
# add here only as a deliberate, visible decision.
KNOWN_GATEWAY_META_TOOLS |= {
    "calculate",
    "compute_stats",
    "percent_change",
    "group_stats",
}

# Home-chat bulk-result export (govern.py's _HOME_CHAT_MAX_RESULT_ROWS cap,
# added 2026-08-26): ungated itself, but NOT ungoverned data access -- it
# delegates to office_create_excel_report's own _govern("create_excel_report",
# ...) call internally, so a caller without export access is still denied
# there, one hop in. Ungated only so any principal can retrieve data THEY were
# already shown a capped view of, regardless of which category originally
# granted the underlying lookup. See gateway/app.py's own matching comment on
# its _UNGOVERNED_GATEWAY_TOOLS entry -- keep both in sync.
KNOWN_GATEWAY_META_TOOLS |= {
    "export_bulk_result_to_excel",
}

_LIST_TOOLS_SNIPPET = (
    "import asyncio, json\n"
    "import app as _mod\n"
    "async def _main():\n"
    "    tools = await _mod.mcp.list_tools()\n"
    "    print(json.dumps({t.name: t.description or '' for t in tools}))\n"
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


def _list_tools(directory: Path) -> tuple[dict[str, str] | None, str]:
    """Run `directory`'s app.py in a fresh subprocess and return its
    registered tools as {name: description}, or (None, error) if it couldn't
    be imported/run."""
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
        return json.loads(proc.stdout.strip().splitlines()[-1]), ""
    except Exception as exc:  # noqa: BLE001
        return None, f"could not parse output ({exc}): stdout={proc.stdout!r} stderr={proc.stderr[-500:]!r}"


# ── Content-drift heuristics ──────────────────────────────────────────────────
# Deliberately narrow and low-false-positive: a hard-fail suite is only useful
# if a failure always means "go fix this," never "go argue with the linter."

# An LLM caller only ever sees ONE string (the gateway's), so any description
# that tells the reader to go look at another tool's/layer's docstring is dead
# text -- there is no mechanism for the model to chase it. Catches the
# get_customer_order_recency case verbatim ("...see the underlying tool's own
# docstring") and any future rephrasing of the same mistake.
DEAD_REFERENCE_RE = re.compile(r"see\b[^.]{0,60}\b(underlying tool|own docstring)\b", re.IGNORECASE)

# Curated, not derived: each phrase below signals a safety/accuracy caveat
# (scope limits, data-quality caveats, non-attribution, entitlement gates) that
# a manifest.py author judged worth writing down. If it's worth telling a
# human via "My Access"/self-service UI, an LLM deciding whether/how to use the
# same tool needs the same substance -- not the same words (manifest.py's
# description is a deliberately different audience/wording, see
# ToolPolicy.description's field comment), just the same fact. Add a phrase
# here whenever a new caveat like this gets written into a manifest
# description -- that is what makes this check self-updating instead of a
# fixed snapshot of today's caveats.
CAVEAT_KEYWORDS = [
    "best-effort", "not verified", "not a verified", "not guaranteed",
    "cannot be attributed", "no due-date", "not live", "not customer-",
    "requires the", "entitlement", "gated",
]


def _dropped_caveats(manifest_desc: str, gateway_desc: str) -> list[str]:
    manifest_l, gateway_l = manifest_desc.lower(), gateway_desc.lower()
    return [kw for kw in CAVEAT_KEYWORDS if kw in manifest_l and kw not in gateway_l]


def main() -> int:
    dirs_to_check = sorted(set(BACKEND_DIRS.values())) + [GATEWAY_DIR]
    tools_by_dir: dict[str, dict[str, str]] = {}
    for d in dirs_to_check:
        descs, err = _list_tools(ROOT / d)
        check(f"{d}/app.py imports cleanly and lists its tools", descs is not None, err)
        tools_by_dir[d] = descs or {}

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
        extra = set(tools_by_dir.get(d, {})) - canonical_by_dir.get(d, set())
        check(f"{d}/app.py has no tools missing from the manifest", not extra, sorted(extra))

    # ── Content drift: the gateway description is the ONLY one an LLM caller
    # ever sees, so it must be self-contained (no dead cross-file pointers) and
    # must not have silently dropped a caveat manifest.py already flagged. ────
    for canonical, policy in sorted(manifest.TOOL_POLICIES.items()):
        gateway_name = manifest.namespaced(canonical)
        gateway_desc = gateway_tools.get(gateway_name, "")
        if not gateway_desc:
            continue  # already reported as a missing wrapper above
        check(f"{gateway_name}: description has no dead cross-file pointer",
              not DEAD_REFERENCE_RE.search(gateway_desc),
              f"gateway description: {gateway_desc!r} -- an LLM caller can't chase a "
              "'see the underlying tool's own docstring' pointer; inline the fact instead")
        dropped = _dropped_caveats(policy.description, gateway_desc)
        check(f"{gateway_name}: no manifest caveat silently dropped from the gateway description",
              not dropped,
              f"manifest.py's ToolPolicy.description mentions {dropped} but the gateway "
              f"description ({gateway_desc!r}) doesn't reflect it -- an LLM caller only ever "
              "sees the gateway string, so the caveat needs to be there too")

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
