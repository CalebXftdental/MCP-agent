"""mcp-code -- read-only opencode planning and repository review backend."""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(os.getenv("CODE_ENV_FILE") or ".env.local")

_CORE_DIR = str((Path(__file__).parent.parent / "governance_core").resolve())
if _CORE_DIR not in sys.path:
    sys.path.insert(0, _CORE_DIR)

import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse

import code_plan_store


def _allowed_hosts() -> list[str]:
    raw = (os.getenv("CODE_ALLOWED_HOSTS") or "").strip()
    hosts = ["localhost", "localhost:*", "127.0.0.1", "127.0.0.1:*"]
    for h in raw.split(","):
        h = h.strip()
        if h:
            hosts.extend((h, f"{h}:*"))
    return hosts


mcp = FastMCP(
    "frontier-mcp-code",
    stateless_http=True,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=_allowed_hosts(),
        allowed_origins=["http://localhost", "http://localhost:*", "http://127.0.0.1", "http://127.0.0.1:*"],
    ),
)


def _repo_root() -> Path:
    configured = os.getenv("CODE_AUTOMATION_REPO_ROOT")
    if configured:
        return Path(configured).resolve()
    return Path(__file__).parent.parent.resolve()


def _safe_rel(path: Path, root: Path) -> str | None:
    try:
        rel = path.resolve().relative_to(root)
    except ValueError:
        return None
    return str(rel).replace("\\", "/")


def _repo_files(limit: int = 400) -> list[str]:
    root = _repo_root()
    skip_dirs = {".git", ".venv", "__pycache__", ".pytest_cache", "node_modules", ".tmp"}
    files: list[str] = []
    if not root.exists():
        return files
    for p in root.rglob("*"):
        if len(files) >= limit:
            break
        if any(part in skip_dirs for part in p.parts):
            continue
        if p.is_file():
            rel = _safe_rel(p, root)
            if rel:
                files.append(rel)
    return sorted(files)


def _keywords(text: str) -> set[str]:
    words = re.findall(r"[a-zA-Z][a-zA-Z0-9_]{2,}", text.lower())
    stop = {"the", "and", "for", "with", "into", "from", "that", "this", "workflow", "report", "agent", "assistant", "need", "needs", "please"}
    return {w for w in words if w not in stop}


def _rank_files(request: str, focus: str = "") -> list[str]:
    files = _repo_files()
    words = _keywords(request + " " + focus)
    weighted = []
    hints = {
        "workflow": ["gateway/workflows.py", "governance_core/workflow_models.py", "gateway/static/app.html"],
        "template": ["governance_core/template_store.py", "governance_core/template_models.py", "gateway/static/app.html"],
        "approval": ["governance_core/approval_store.py", "governance_core/approval_models.py", "gateway/app.py"],
        "office": ["mcp-office/app.py", "governance_core/artifact_store.py"],
        "email": ["mcp-email/app.py", "governance_core/email_send_store.py"],
        "calendar": ["mcp-calendar/app.py", "governance_core/calendar_send_store.py"],
        "knowledge": ["mcp-knowledge/app.py", "governance_core/knowledge_store.py"],
        "rag": ["mcp-knowledge/app.py", "governance_core/knowledge_store.py"],
        "policy": ["governance_core/policy/manifest.py", "governance_core/policy/categories.py"],
    }
    for f in files:
        score = 0
        low = f.lower()
        for w in words:
            if w in low:
                score += 3
        for key, paths in hints.items():
            if key in words and f in paths:
                score += 7
        if f.startswith("_smoke/"):
            score += 1
        if score:
            weighted.append((score, f))
    weighted.sort(key=lambda item: (-item[0], item[1]))
    chosen = [f for _score, f in weighted[:12]]
    for default in ("gateway/workflows.py", "gateway/app.py", "gateway/static/app.html", "governance_core/policy/manifest.py", "governance_core/policy/categories.py"):
        if default in files and default not in chosen:
            chosen.insert(0, default)
    return chosen[:12]


def _commands(plan_type: str) -> list[str]:
    base = [
        r".venv\Scripts\python.exe -m pytest _smoke/test_policy.py",
        r".venv\Scripts\python.exe -m pytest _smoke/test_categories.py",
        "git diff --check",
    ]
    if plan_type == "repo_review":
        return [r'rg -n "TODO|FIXME|approval_required|BackendError" gateway governance_core mcp-*', *base]
    if plan_type == "template_generation":
        return [r".venv\Scripts\python.exe _smoke/test_templates.py", *base]
    return [r".venv\Scripts\python.exe _smoke/test_workflow_customer360.py", *base]


def _risk(request: str, fallback: str = "medium") -> str:
    text = request.lower()
    if any(w in text for w in ("delete", "send", "external", "credential", "secret", "payment", "production", "bash", "write")):
        return "high"
    if any(w in text for w in ("ui", "dashboard", "workflow", "approval", "policy", "connector")):
        return "medium"
    return fallback if fallback in {"low", "medium", "high"} else "medium"


def _result(record) -> str:
    data = record.public_dict()
    data.update({
        "source": "code",
        "status": "success",
        "message": "Read-only code plan captured. Any edit/write/bash execution must be requested and approved separately.",
    })
    return json.dumps(data)


@mcp.tool()
async def opencode_plan_change(owner: str, request: str, target_area: str = "", risk_level: str = "medium") -> str:
    """Create a read-only implementation plan for a requested codebase change."""
    files = _rank_files(request, target_area)
    risk = _risk(request + " " + target_area, risk_level)
    summary = f"Plan for {target_area or 'the governed assistant'}: inspect policy, gateway wrapper, dashboard route, MCP backend, and smoke tests before any edits."
    findings = [
        "This plan is advisory only and does not modify files.",
        "Implementation should add policy manifest/category coverage before exposing tools in the UI.",
        "Any code edits, dependency installs, or shell automation should be routed through a separate approval step.",
    ]
    record = code_plan_store.create_plan(
        owner=owner,
        request=request,
        plan_type="change_plan",
        risk_level=risk,
        summary=summary,
        proposed_files=files,
        proposed_commands=_commands("change_plan"),
        findings=findings,
    )
    return _result(record)


@mcp.tool()
async def opencode_review_repo(owner: str, focus: str = "", paths: list[str] | None = None) -> str:
    """Create a read-only repository review plan and checklist."""
    chosen = [p for p in (paths or []) if isinstance(p, str) and p.strip()]
    if not chosen:
        chosen = _rank_files(focus or "governance gateway policy approval workflow", focus)
    summary = f"Review plan for {focus or 'governance assistant repository'} with emphasis on policy bypasses, approval gates, persistence, and smoke coverage."
    findings = [
        "Check that model-provided owner fields are replaced by authenticated principal context.",
        "Check external actions for approval_required behavior before connector queueing.",
        "Check durable stores for owner filtering and reload behavior.",
        "Check UI routes against server-side authorization; navigation hiding is not sufficient by itself.",
    ]
    record = code_plan_store.create_plan(
        owner=owner,
        request=focus or "Repository governance review",
        plan_type="repo_review",
        risk_level="medium",
        summary=summary,
        proposed_files=chosen[:16],
        proposed_commands=_commands("repo_review"),
        findings=findings,
    )
    return _result(record)


@mcp.tool()
async def opencode_generate_template(owner: str, goal: str, template_type: str = "workflow") -> str:
    """Generate a governed workflow/template draft without writing code."""
    ttype = (template_type or "workflow").strip().lower()
    draft = {
        "goal": goal,
        "inputs": ["business_owner", "data_scope", "reviewer", "output_format"],
        "steps": [
            {"name": "resolve_scope", "tool": "minierp_accounts_find_customer", "approval": False},
            {"name": "collect_context", "tool": "knowledge_search_knowledge", "approval": False},
            {"name": "generate_artifact", "tool": "office_create_word_report", "approval": False},
            {"name": "request_review", "tool": "artifact_request_approval", "approval": True},
        ],
        "outputs": ["audit_record", "office_artifact", "approval_packet"],
        "guardrails": ["least privilege categories", "owner-scoped artifacts", "approval before external side effects"],
    }
    files = _rank_files(goal + " template workflow", ttype)
    summary = f"Generated a {ttype} template draft for: {goal}"
    record = code_plan_store.create_plan(
        owner=owner,
        request=goal,
        plan_type="template_generation",
        risk_level=_risk(goal, "medium"),
        summary=summary,
        proposed_files=files,
        proposed_commands=_commands("template_generation"),
        findings=["Template draft is not installed automatically; admin review should create or version it through the template registry."],
        template_draft=draft,
    )
    return _result(record)


async def _health(_request):
    return JSONResponse({"ok": True, "service": "mcp-code", "mode": "read_only"})


app = mcp.streamable_http_app()
app.add_route("/health", _health)


if __name__ == "__main__":
    port = int(os.getenv("PORT") or os.getenv("CODE_PORT") or "8070")
    print(f"[mcp-code] Starting on 0.0.0.0:{port} (path /mcp, read-only)")
    uvicorn.run(app, host="0.0.0.0", port=port)
