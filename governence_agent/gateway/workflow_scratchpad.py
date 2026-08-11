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

import os
import time

_IDLE_SEC = int(os.getenv("GOVERNANCE_CHAT_IDLE_SEC") or str(60 * 60))

_PLANS: dict[str, dict] = {}


def _fresh() -> dict:
    return {"fieldsDiscovered": [], "modulesChosen": [], "draftNodes": [], "notes": "", "updatedAt": None}


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
) -> dict:
    """Upserts only the fields given (None = leave unchanged) and returns the
    FULL resulting plan -- callers never have to merge partial updates
    themselves, and the model always gets its complete current plan back."""
    plan = get_plan(session_id)
    if fields_discovered is not None:
        plan["fieldsDiscovered"] = fields_discovered
    if modules_chosen is not None:
        plan["modulesChosen"] = modules_chosen
    if draft_nodes is not None:
        plan["draftNodes"] = draft_nodes
    if notes is not None:
        plan["notes"] = notes
    plan["updatedAt"] = time.time()
    _PLANS[session_id] = plan
    return dict(plan)


def clear_plan(session_id: str) -> None:
    _PLANS.pop(session_id, None)
