"""Navigation-help chatbot: a small, independent assistant that only answers
"where do I find X" questions about this console's own pages/tabs.

Deliberately separate from backend/chat.py's governed assistant -- no MCP
tools are passed to the model, no knowledge_store/knowledge MCP server is
touched, and no chat_log session is created. It cannot call a governed tool,
search or answer from any knowledge base, or persist a transcript; the worst
a bad answer can do is point at the wrong tab. Same LLM client and the same
llm_broker admission lanes as chat.py, though -- see gateway/llm_broker.py.
"""
from __future__ import annotations

from starlette.responses import JSONResponse

import llm_broker
import orchestrator

from .deps import _session, _unauthorized

# Mirrors frontend/src/pages/routes.ts's ROUTES table (title/description/admin
# only -- icons and sidebar grouping don't matter here). Kept in sync by hand;
# this is UI copy, not an access grant, so a stale entry here can make the
# navigation helper describe or point at a page wrong, but it can't widen what
# anyone can actually open -- resolveRoute() and each admin endpoint's own
# check are still the real gate, same as before this module existed.
_ROUTE_TABLE: list[tuple[str, str, str, bool]] = [
    # (key, title, description, admin_only)
    ("home", "Home", "Ask about customers, orders, shipments, and invoices, in plain language.", False),
    ("playground", "AI Playground", "Guided AI tasks: draft messages, summarize accounts, surface insights.", False),
    ("workflows", "Workflows", "Run governed office workflows that turn data into reports, decks, and files.", False),
    ("automations", "Automations", "Recurring governed workflows with deterministic local due-run execution.", False),
    ("my_workflows", "My Workflow", "Build a custom automation by connecting governed tools, approval gates, and AI steps -- no code required.", False),
    ("knowledge", "Knowledge", "Local document ingestion, search, and citation-backed answers.", False),
    ("files", "Files", "Generated artifacts, classifications, and downloads.", False),
    ("templates", "Templates", "Reusable report, deck, email, calendar, prompt, and workflow templates.", False),
    ("sends", "Send Queue", "Approval-gated email delivery queue and connector handoff status.", False),
    ("calendar", "Calendar", "Draft invite artifacts and approval-gated calendar connector queue.", False),
    ("access", "My Access", "Your status, granted data domains, and API key.", False),
    ("developer", "Developer", "Your API key, an MCP connection snippet, and a playground to test governed tools.", False),
    ("history", "History", "Your recent governed calls and the answers you pinned from Home.", False),
    ("consumers", "Consumers", "Principals (people & agents), their categories, and API keys.", True),
    ("categories", "Categories", "Data-domain templates: which backend, tools, and sensitivity levels each grants.", True),
    ("department-admin", "Departments", "Org-unit groupings of categories, used by self-signup.", True),
    ("requests", "Access Requests", "Approve signups and access asks; review denied-attempt suggestions.", True),
    ("approvals", "Approvals", "External sends and other high-impact actions, waiting for an admin decision.", True),
    ("agents", "Agents", "Autonomous agent identities with allowed workflow templates and schedule constraints.", True),
    ("code-plans", "Code Plans", "Read-only opencode planning, repository reviews, and workflow template drafts.", True),
    ("monitor", "Monitor", "Live security overview: call volume, authorization, sensitive access, and every governed tool call.", True),
    ("alerts", "Alerts", "Security incidents: enumeration, denial bursts, and anomalies grouped per principal for triage.", True),
    ("security", "Security", "Backend health and API-key hygiene: dormant keys, unused grants, unrotated credentials.", True),
    ("whitelist", "IP Allowlist", "Global CIDR allowlist enforced at the MCP edge.", True),
]

_VALID_KEYS = {row[0] for row in _ROUTE_TABLE}
_ADMIN_ONLY = {row[0] for row in _ROUTE_TABLE if row[3]}


def _visible(role: str | None) -> list[tuple[str, str, str, bool]]:
    """Same rule as frontend routes.ts's `visibleNav`: every row unless it's
    admin-only and this session isn't. This -- not the client -- is the one
    place that decides which pages the bot may even mention."""
    return [row for row in _ROUTE_TABLE if not row[3] or role == "admin"]


def _nav_kb_text(role: str | None) -> str:
    return "\n".join(f"- {key}: {title} -- {description}" for key, title, description, _ in _visible(role))


_SYSTEM_PROMPT_TEMPLATE = (
    "You are the navigation helper for the Frontier MCP Workspace console. "
    "You ONLY help the signed-in user find which page/tab has what they need. "
    "You have no access to business data (customers, orders, invoices, documents) "
    "and can't take any action yourself -- if asked for that, say so plainly and "
    "point at the page where they can do it, or at Home for data questions.\n\n"
    "Pages this user can open, one per line as `key: Title -- description`:\n"
    "{nav_kb}\n\n"
    "When you're confident which single page answers the question, end your "
    "reply on its own final line as exactly `NAVIGATE: <key>`, using one of the "
    "keys listed above verbatim. Omit that line if no single page fits, or "
    "you're unsure."
)

_MAX_HISTORY_TURNS = 6


def _clean_history(raw) -> list[dict]:
    """Client-supplied turns only -- this endpoint keeps no server-side session
    (no chat_log, unlike /chat), so a conversational bubble round-trips its own
    recent turns each request. Trusted only as chat context, never as anything
    that affects access: a forged turn can only confuse the reply, the same as
    a forged `history` payload to any other stateless chat call would."""
    if not isinstance(raw, list):
        return []
    out = []
    for turn in raw[-_MAX_HISTORY_TURNS:]:
        if not isinstance(turn, dict):
            continue
        role = turn.get("role")
        content = str(turn.get("content") or "").strip()
        if role in ("user", "assistant") and content:
            out.append({"role": role, "content": content})
    return out


def _extract_navigate(content: str, role: str | None) -> tuple[str, str | None]:
    """Pulls a trailing `NAVIGATE: <key>` line off the reply. Re-checks the
    role gate here too (not just at prompt-build time in `_nav_kb_text`) so a
    key surviving from earlier history can never point a non-admin session at
    an admin-only tab."""
    lines = content.splitlines()
    if not lines:
        return content, None
    last = lines[-1].strip()
    if not last.upper().startswith("NAVIGATE:"):
        return content, None
    key = last.split(":", 1)[1].strip().lower()
    reply = "\n".join(lines[:-1]).strip()
    if key in _VALID_KEYS and not (key in _ADMIN_ONLY and role != "admin"):
        return reply, key
    return reply, None


def _busy_payload(busy: llm_broker.LLMBusy) -> dict:
    return {"error": str(busy), "code": "assistant_busy", "lane": busy.lane, "queued": busy.queued, "retryable": True}


async def _nav_help(request):
    claims = _session(request)
    if not claims:
        return _unauthorized()
    try:
        body = await request.json()
    except Exception:
        body = {}
    message = str(body.get("message") or "").strip()
    if not message:
        return JSONResponse({"error": "message is required"}, status_code=400)

    llm_complete = orchestrator.default_llm_complete()
    if llm_complete is None:
        return JSONResponse({"reply": "The navigation helper isn't configured yet.", "navigate": None, "configured": False})
    # Idempotent -- default_llm_complete() already returns an adapted callable;
    # this only matters for a test injecting a plain sync fake, same as
    # orchestrator.run_chat's identical line.
    llm_complete = llm_broker.adapt_complete(llm_complete)

    role = claims.get("role")
    messages: list[dict] = [{"role": "system", "content": _SYSTEM_PROMPT_TEMPLATE.format(nav_kb=_nav_kb_text(role))}]
    messages.extend(_clean_history(body.get("history")))
    messages.append({"role": "user", "content": message})

    try:
        msg = await llm_complete(messages, [], lane=llm_broker.INTERACTIVE, user=claims.get("name", ""))
    except llm_broker.LLMBusy as busy:
        return JSONResponse(_busy_payload(busy), status_code=503, headers={"Retry-After": "10"})
    except Exception as exc:  # noqa: BLE001 -- surfaced as a readable error, not a bare 500
        return JSONResponse({"error": f"navigation helper failed: {exc}"}, status_code=502)

    content = (getattr(msg, "content", "") or "").strip()
    reply, navigate = _extract_navigate(content, role)
    return JSONResponse({"reply": reply, "navigate": navigate, "configured": True})
