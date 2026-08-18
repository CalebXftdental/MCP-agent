"""In-RAM working memory for the "My Workflow" copilot -- discovered tool
fields/values, which modules (tools) it's decided to use, and a draft node
list, scoped to one chat session_id.

Deliberately NOT chat_log, NOT the policy store, NOT any persisted store:
gone on process restart and explicitly treated as stale after
GOVERNANCE_CHAT_IDLE_SEC (same cutoff chat_log's own idle sweep uses), so it
never silently survives past the conversation it was built for. This is
scratch bookkeeping for the model to accumulate a STRUCTURED plan without
re-deriving it from its own prior prose every turn -- see
update_workflow_plan in app.py. It is not a substitute for actually proposing
the workflow (propose_graph) once the plan is ready.

Single-process, in-memory dict: correct for Stage 1's "one App Service
running both processes" deployment (DEPLOY.md), not for multiple horizontally
-scaled instances -- a later turn landing on a different instance than an
earlier one would see an empty plan. Acceptable at this stage; would need a
shared cache (e.g. Redis) to survive a multi-instance deployment.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time

_IDLE_SEC = int(os.getenv("GOVERNANCE_CHAT_IDLE_SEC") or str(60 * 60))
_SWEEP_INTERVAL_SEC = int(os.getenv("GOVERNANCE_WORKFLOW_SCRATCHPAD_SWEEP_INTERVAL_SEC") or "300")

_PLANS: dict[str, dict] = {}


def _fresh() -> dict:
    return {"fieldsDiscovered": [], "modulesChosen": [], "draftNodes": [], "notes": "", "checks": [], "updatedAt": None}


def get_plan(session_id: str) -> dict:
    """Always returns a full plan dict, never None -- a session with nothing
    recorded yet (or gone stale) just gets the empty shape back."""
    entry = _PLANS.get(session_id)
    if entry is None:
        return _fresh()
    if entry.get("updatedAt") and (time.time() - entry["updatedAt"]) > _IDLE_SEC:
        _PLANS.pop(session_id, None)
        return _fresh()
    return dict(entry)


def update_plan(
    session_id: str, *,
    fields_discovered: list[dict] | None = None,
    modules_chosen: list[str] | None = None,
    draft_nodes: list[dict] | None = None,
    notes: str | None = None,
    checks: list[dict] | None = None,
) -> dict:
    """Upserts only the fields given (None = leave unchanged) and returns the
    FULL resulting plan -- callers never have to merge partial updates
    themselves, and the model always gets its complete current plan back.

    `checks` is written automatically by propose_graph (gateway/app.py) after
    every save attempt -- the structured validate_graph result (GraphCheck.
    public_dict()) for that attempt, [] on a clean save. It is NOT a
    model-settable argument on the update_workflow_plan tool; it exists so
    the copilot can recall "what's still outstanding on this graph" via a
    plain re-read (update_workflow_plan with every other arg omitted)
    without needing to call propose_graph again just to find out."""
    plan = get_plan(session_id)
    if fields_discovered is not None:
        plan["fieldsDiscovered"] = fields_discovered
    if modules_chosen is not None:
        plan["modulesChosen"] = modules_chosen
    if draft_nodes is not None:
        plan["draftNodes"] = draft_nodes
    if notes is not None:
        plan["notes"] = notes
    if checks is not None:
        plan["checks"] = checks
    plan["updatedAt"] = time.time()
    _PLANS[session_id] = plan
    return dict(plan)


def clear_plan(session_id: str) -> None:
    _PLANS.pop(session_id, None)


def sweep_stale_plans() -> int:
    """Evict every plan idle past _IDLE_SEC, regardless of whether anyone
    ever calls get_plan() for that exact session_id again. Without this, an
    abandoned session's plan sits in _PLANS until process restart --
    get_plan's own staleness check only fires on a later read for that same
    session, which may never come. Same shape as chat_log's own idle sweep;
    see scratchpad_sweep_loop below and backend/chat.py's _chat_sweep_loop.
    Returns the number evicted."""
    now = time.time()
    stale = [sid for sid, plan in _PLANS.items() if plan.get("updatedAt") and (now - plan["updatedAt"]) > _IDLE_SEC]
    for sid in stale:
        _PLANS.pop(sid, None)
    return len(stale)


async def scratchpad_sweep_loop() -> None:
    """Background guarantee: evict idle workflow-copilot plans on a timer,
    same pattern as backend/chat.py's _chat_sweep_loop (mirrors its idle-sweep
    shape, not its data -- this sweeps _PLANS, not ChatSession). Single-
    instance, in-process -- consistent with the rest of this app's Stage-1
    model (audit ring, rate limits, scope_store)."""
    while True:
        await asyncio.sleep(_SWEEP_INTERVAL_SEC)
        try:
            evicted = sweep_stale_plans()
            if evicted:
                print(f"[workflow-scratchpad-sweep] evicted {evicted} idle plan(s)", flush=True)
        except Exception as exc:  # the sweep must never crash the process
            print(f"[workflow-scratchpad-sweep] failed: {exc}", file=sys.stderr, flush=True)
