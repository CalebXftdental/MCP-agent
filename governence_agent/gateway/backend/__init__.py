"""The gateway's HTTP/JSON API — everything a browser calls, one module per domain.

`register(app)` is the whole public surface: it wires every route onto the
Starlette app app.py builds. Handlers live in the sibling modules and know nothing
about routing.

This package is the API the gateway *serves*. Its outbound counterpart -- the MCP
client pool the gateway uses to *reach* backend servers -- is `mcp_clients.py` one
level up.

Two prefixes, one handler each. `/backend/<path>` is canonical — what the React
client in frontend/ calls. The bare legacy path is registered alongside it because
static/app.html still calls it. Retiring a legacy path is `legacy=False` on its
line; when every line is legacy=False, `/dashboard/*` belongs to the shell alone
and pages._dashboard can become a single catch-all, which is what React Router
needs.
"""
from __future__ import annotations

from starlette.staticfiles import StaticFiles

from . import (
    admin_policy,
    admin_security,
    approvals,
    artifacts,
    automations,
    chat,
    code_plans,
    knowledge,
    pages,
    sends,
    session,
    templates,
    workflow_api,
    workflow_graphs,
)
from .deps import _FRONTEND_DIST_DIR

API_PREFIX = "/backend"


def register(app) -> None:
    """Mount every HTTP route on `app`. Called once from app.py."""

    def api(path: str, handler, methods: list[str] | None = None, *, legacy: bool = True) -> None:
        """A JSON/data endpoint: under /backend, plus its legacy bare path."""
        kwargs = {} if methods is None else {"methods": methods}
        app.add_route(f"{API_PREFIX}{path}", handler, **kwargs)
        if legacy:
            app.add_route(path, handler, **kwargs)

    def page(path: str, handler, methods: list[str] | None = None) -> None:
        """An HTML page or static asset. Deliberately NOT mirrored under /backend:
        that prefix serves data only, so HTML coming back from it always means
        something went wrong and a client can treat it as an error."""
        kwargs = {} if methods is None else {"methods": methods}
        app.add_route(path, handler, **kwargs)

    # ── Pages and assets ──────────────────────────────────────────────────────
    page("/", pages._root)
    page("/dashboard", pages._dashboard)
    # All app-shell section routes serve the same SPA shell (session-gated); the
    # client renders the panel from the path. Admin panels are hidden for
    # non-admins and every admin API enforces the role server-side.
    for section in (
        "admin", "assistant", "access", "consumers", "requests", "categories",
        "department-admin", "whitelist", "monitor", "history", "alerts", "security",
        "activity", "developer", "home", "playground", "files", "workflows",
        "my_workflows", "automations", "knowledge", "sends", "calendar", "approvals",
        "templates", "code-plans", "agents",
    ):
        page(f"/dashboard/{section}", pages._dashboard)
    page("/dashboard/signup", pages._signup_page)
    page("/dashboard/chat", pages._chat_page)
    page("/dashboard/logo.png", pages._logo)
    page("/favicon.svg", pages._favicon)

    # The built React SPA's hashed JS/CSS (frontend/dist/assets, from `npm run
    # build` -- see DEPLOY.md). Absent in local dev until that build has been run
    # at least once; deps._APP_HTML falls back to the legacy shell in that case,
    # so there's nothing under /assets to serve either.
    _frontend_assets_dir = _FRONTEND_DIST_DIR / "assets"
    if _frontend_assets_dir.exists():
        app.mount("/assets", StaticFiles(directory=str(_frontend_assets_dir)), name="frontend-assets")

    # ── Health ────────────────────────────────────────────────────────────────
    # Keeps its bare path permanently: App Service and uptime probes point at it.
    api("/health", pages._health)

    # ── Session, self-service, catalogs ───────────────────────────────────────
    api("/dashboard/login", session._login, methods=["POST"])
    api("/dashboard/signup", session._signup, methods=["POST"])
    api("/dashboard/logout", session._logout, methods=["POST"])
    api("/dashboard/me", session._me)
    api("/dashboard/my-access", session._my_access)
    api("/dashboard/my-key/rotate", session._my_key_rotate, methods=["POST"])
    api("/dashboard/request-access", session._request_access, methods=["POST"])
    api("/dashboard/my-denials", session._my_denials)
    api("/dashboard/my-activity", session._my_activity)
    api("/dashboard/departments", session._departments)
    api("/dashboard/category-catalog", session._categories_catalog)
    api("/dashboard/try-tool", session._try_tool, methods=["POST"])

    # ── Chat ──────────────────────────────────────────────────────────────────
    api("/chat", chat._chat, methods=["POST"])
    api("/chat/stream", chat._chat_stream, methods=["POST"])
    api("/dashboard/chat-history", chat._chat_history)
    api("/dashboard/chat-history/{sid}", chat._chat_transcript)
    api("/dashboard/chat-history/{sid}/resume", chat._chat_resume, methods=["POST"])
    api("/dashboard/feedback", chat._chat_feedback, methods=["POST"])

    # ── Admin: audit, policy, principals ──────────────────────────────────────
    api("/admin/calls", admin_security._admin_calls)
    api("/admin/audit-export", admin_security._admin_audit_export, methods=["POST"])
    api("/admin/sessions", admin_security._admin_sessions)
    api("/admin/rate-limits", admin_security._admin_rate_limits)
    api("/admin/catalog", admin_policy._admin_catalog)
    api("/admin/consumers", admin_policy._admin_consumers, methods=["GET", "POST"])
    api("/admin/consumers/{cid}", admin_policy._admin_consumer_item, methods=["PATCH", "DELETE"])
    api("/admin/consumers/{cid}/rotate-key", admin_policy._admin_consumer_rotate, methods=["POST"])
    api("/admin/consumers/{cid}/profile", admin_security._admin_consumer_profile)
    api("/admin/categories", admin_policy._admin_categories, methods=["GET", "POST"])
    api("/admin/categories/{cid}", admin_policy._admin_category_item, methods=["DELETE"])
    api("/admin/departments", admin_policy._admin_departments, methods=["GET", "POST"])
    api("/admin/departments/{did}", admin_policy._admin_department_item, methods=["DELETE"])
    api("/admin/whitelist", admin_policy._admin_whitelist, methods=["GET", "PUT"])
    api("/admin/policy-changes", admin_policy._admin_policy_changes)
    api("/admin/requests", admin_policy._admin_requests)
    api("/admin/requests/{rid}/approve", admin_policy._admin_request_approve, methods=["POST"])
    api("/admin/requests/{rid}/deny", admin_policy._admin_request_deny, methods=["POST"])
    api("/admin/access-suggestions", admin_policy._admin_access_suggestions)
    api("/admin/agents", admin_policy._admin_agents, methods=["GET", "POST"])
    api("/admin/agents/{aid}", admin_policy._admin_agent_item, methods=["GET", "PATCH"])

    # ── Admin: security monitoring and break-glass ─────────────────────────────
    api("/admin/overview", admin_security._admin_overview)
    api("/admin/alerts", admin_security._admin_alerts)
    api("/admin/alerts/{aid}/{action}", admin_security._admin_alert_action, methods=["POST"])
    api("/admin/credential-hygiene", admin_security._admin_credential_hygiene)
    api("/admin/backends/health", admin_security._admin_backend_health)
    api("/admin/controls", admin_security._admin_controls, methods=["GET", "PUT"])

    # ── Workflows ─────────────────────────────────────────────────────────────
    api("/workflows", workflow_api._workflows)
    api("/workflows/{tid}", workflow_api._workflow_template)
    api("/workflows/{tid}/preflight", workflow_api._workflow_preflight, methods=["GET", "POST"])
    api("/workflows/{tid}/run", workflow_api._workflow_run_start, methods=["POST"])
    api("/workflow-suggestions", workflow_api._workflow_suggestions, methods=["POST"])
    api("/workflow-suggestions/launch", workflow_api._workflow_suggestion_launch, methods=["POST"])
    api("/workflow-runs", workflow_api._workflow_runs)
    api("/workflow-runs/{rid}", workflow_api._workflow_run_item)
    api("/workflow-runs/{rid}/export-evidence", workflow_api._workflow_run_export_evidence, methods=["POST"])
    api("/workflow-runs/{rid}/cancel", workflow_api._workflow_run_cancel, methods=["POST"])
    api("/workflow-runs/{rid}/resume", workflow_api._workflow_run_resume, methods=["POST"])
    api("/admin/workflow-health", workflow_api._admin_workflow_health)
    api("/admin/workflows/{tid}/{action}", workflow_api._admin_workflow_template_status, methods=["POST"])

    # ── Workflow graphs ───────────────────────────────────────────────────────
    api("/workflow-graphs", workflow_graphs._workflow_graphs, methods=["GET", "POST"])
    api("/workflow-graphs/{gid}", workflow_graphs._workflow_graph_item)
    api("/workflow-graphs/{gid}/versions", workflow_graphs._workflow_graph_versions, methods=["POST"])
    api("/workflow-graphs/{gid}/publish", workflow_graphs._workflow_graph_publish, methods=["POST"])
    api("/workflow-graphs/{gid}/validate", workflow_graphs._workflow_graph_validate, methods=["POST"])
    api("/dashboard/workflow-graph-catalog", workflow_graphs._workflow_graph_catalog)

    # ── Artifacts ─────────────────────────────────────────────────────────────
    api("/artifacts", artifacts._artifacts, methods=["GET", "POST"])
    api("/artifacts/{aid}", artifacts._artifact_metadata, methods=["GET", "DELETE"])
    api("/artifacts/{aid}/reviews", artifacts._artifact_reviews, methods=["GET", "POST"])
    api("/artifacts/{aid}/reviews/{rid}", artifacts._artifact_review_item)
    # Before the {action} route below: {action} would also match "comments".
    api("/artifacts/{aid}/reviews/{rid}/comments", artifacts._artifact_review_comments, methods=["POST"])
    api("/artifacts/{aid}/reviews/{rid}/{action}", artifacts._artifact_review_decision, methods=["POST"])
    api("/artifacts/{aid}/versions", artifacts._artifact_versions, methods=["GET", "POST"])
    api("/artifacts/{aid}/versions/{vid}/download", artifacts._artifact_version_download)
    api("/artifacts/{aid}/shares", artifacts._artifact_shares, methods=["GET", "POST"])
    api("/artifacts/{aid}/shares/{sid}/revoke", artifacts._artifact_share_revoke, methods=["POST"])
    api("/artifacts/{aid}/workbench", artifacts._artifact_workbench)
    api("/artifacts/{aid}/request-approval", artifacts._artifact_request_approval, methods=["POST"])
    api("/artifacts/{aid}/download", artifacts._artifact_download)
    api("/admin/artifacts/purge-expired", artifacts._admin_artifact_purge_expired, methods=["POST"])
    # Called by the ONLYOFFICE document server, not a browser. The callback URL was
    # already handed out by _artifact_workbench, so the bare path must stay
    # reachable for as long as any open document references it.
    api("/artifacts/{aid}/onlyoffice/callback", artifacts._artifact_onlyoffice_callback, methods=["POST"])

    # ── Approvals, automations, templates, knowledge, code, sends ─────────────
    api("/approvals", approvals._approvals, methods=["GET", "POST"])
    api("/approvals/{aid}/{action}", approvals._approval_decide, methods=["POST"])
    # Before the {aid} route below: {aid} would also match "run-due".
    api("/automations/run-due", automations._automation_run_due, methods=["POST"])
    api("/automations", automations._automations, methods=["GET", "POST"])
    api("/automations/{aid}", automations._automation_item, methods=["GET", "DELETE"])
    api("/templates", templates._templates, methods=["GET", "POST"])
    api("/templates/{tid}", templates._template_item, methods=["GET", "PATCH"])
    api("/templates/{tid}/versions", templates._template_versions, methods=["POST"])
    api("/templates/{tid}/disable", templates._template_disable, methods=["POST"])
    api("/knowledge/documents", knowledge._knowledge_documents, methods=["GET"])
    api("/knowledge/documents/{did}", knowledge._knowledge_document_item, methods=["GET", "DELETE"])
    api("/knowledge/search", knowledge._knowledge_search, methods=["POST"])
    api("/knowledge/answer", knowledge._knowledge_answer, methods=["POST"])
    api("/code-plans", code_plans._code_plans)
    api("/code-plans/{pid}", code_plans._code_plan_item)
    api("/code-plans/{pid}/request-approval", code_plans._code_plan_request_approval, methods=["POST"])
    api("/email-sends", sends._email_sends)
    api("/email-sends/{sid}", sends._email_send_item)
    api("/calendar-sends", sends._calendar_sends)
    api("/calendar-sends/{sid}", sends._calendar_send_item)
