"""Workflow catalog and in-memory run registry for the office-assistant scaffold.

This is deliberately small but real: the gateway owns execution because it already
has the governed `_govern` function and request context. This module owns stable
workflow metadata, run serialization, and payload shaping helpers.
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid
from dataclasses import replace
from pathlib import Path

_CORE_DIR = str((Path(__file__).parent.parent / "governance_core").resolve())
if _CORE_DIR not in sys.path:
    sys.path.insert(0, _CORE_DIR)

from workflow_models import WorkflowRun, WorkflowStep, WorkflowTemplate
import store_concurrency
import workflow_graph_store


TEMPLATES: dict[str, WorkflowTemplate] = {
    "customer_360_report": WorkflowTemplate(
        template_id="customer_360_report",
        display_name="Customer 360 Report",
        description="Generate a customer account report with XLSX data appendix and PPTX review deck.",
        required_categories=["accounts", "orders", "shipments", "office"],
        output_types=["xlsx", "pptx"],
        status="active",
    ),
    "shipment_exception_report": WorkflowTemplate(
        template_id="shipment_exception_report",
        display_name="Shipment Exception Report",
        description="Group shipment exceptions by customer and produce an operations-ready XLSX report.",
        required_categories=["orders", "shipments", "office"],
        output_types=["xlsx"],
        status="active",
    ),
    "vendor_ap_summary": WorkflowTemplate(
        template_id="vendor_ap_summary",
        display_name="Vendor AP Summary",
        description="Summarize vendor profile and AP invoice activity into finance-ready XLSX/DOCX artifacts.",
        required_categories=["finance", "office"],
        output_types=["xlsx", "docx"],
        status="active",
    ),
    "customer_email_draft": WorkflowTemplate(
        template_id="customer_email_draft",
        display_name="Customer Email Draft",
        description="Create a governed customer follow-up email draft, optionally with a PDF packet and approval request.",
        required_categories=["accounts", "orders", "shipments", "email_draft", "office"],
        output_types=["email_draft", "pdf"],
        status="active",
    ),
    "weekly_executive_brief": WorkflowTemplate(
        template_id="weekly_executive_brief",
        display_name="Weekly Executive Brief",
        description="Create a management-ready weekly business brief with top customers, risks, and action items.",
        required_categories=["analytics", "finance", "orders", "shipments", "office"],
        output_types=["xlsx", "pptx", "pdf"],
        status="active",
    ),
}

_RUNS: dict[str, WorkflowRun] = {}
_TEMPLATE_CONTROLS: dict[str, dict] = {}
_LOADED = False
_CONTROLS_LOADED = False

# Every mutation below goes through this guard (governance_core/store_concurrency.py).
# It is what makes a run record safe now that more than one thing can genuinely be
# in flight at once -- see this module's mutator section for the rules it enforces.
_GUARD = store_concurrency.StoreGuard("workflow-runs")

#: Re-exported so callers can batch a unit of work into one disk write:
#:
#:      with workflows.deferred_save():
#:          ...many add_step/complete_step calls...
#:
#: Without it, one node of a workflow costs two full rewrites of every run in the
#: file; a loop node's per-iteration steps would cost dozens.
def deferred_save():
    return _GUARD.deferred(_save)


def store_stats() -> dict:
    """Save counts, for tests and /admin observability -- proves deferral works."""
    return _GUARD.stats()


def _state_root() -> Path:
    configured = os.getenv("GOVERNANCE_STATE_DIR")
    if configured:
        return Path(configured).resolve()
    artifact_dir = os.getenv("GOVERNANCE_ARTIFACT_DIR")
    if artifact_dir:
        return (Path(artifact_dir).resolve().parent / "state").resolve()
    if Path("/home/data").exists():
        return Path("/home/data/governance-state").resolve()
    return Path("/tmp/governance-state").resolve()


def _store_file() -> Path:
    configured = os.getenv("GOVERNANCE_WORKFLOW_STORE_FILE")
    if configured:
        return Path(configured).resolve()
    return _state_root() / "workflow-runs.json"


def _template_controls_file() -> Path:
    configured = os.getenv("GOVERNANCE_WORKFLOW_TEMPLATE_CONTROLS_FILE")
    if configured:
        return Path(configured).resolve()
    return _state_root() / "workflow-template-controls.json"


def _step_to_record(s: WorkflowStep) -> dict:
    return {
        "step_id": s.step_id,
        "type": s.type,
        "status": s.status,
        "tool": s.tool,
        "title": s.title,
        "inputs": dict(s.inputs),
        "outputs": dict(s.outputs),
        "error": s.error,
    }


def _step_from_record(d: dict) -> WorkflowStep:
    return WorkflowStep(
        step_id=d["step_id"],
        type=d.get("type", "task"),
        status=d.get("status", "pending"),
        tool=d.get("tool", ""),
        title=d.get("title", ""),
        inputs=dict(d.get("inputs") or {}),
        outputs=dict(d.get("outputs") or {}),
        error=d.get("error"),
    )


def _run_to_record(r: WorkflowRun) -> dict:
    return {
        "run_id": r.run_id,
        "template_id": r.template_id,
        "requested_by": r.requested_by,
        "status": r.status,
        "inputs": dict(r.inputs),
        "steps": [_step_to_record(s) for s in r.steps],
        "artifact_ids": list(r.artifact_ids),
        "approval_ids": list(r.approval_ids),
        "created_at": r.created_at,
        "updated_at": r.updated_at,
        "error": r.error,
        "resumed_at": r.resumed_at,
    }


def _run_from_record(d: dict) -> WorkflowRun:
    return WorkflowRun(
        run_id=d["run_id"],
        template_id=d.get("template_id", ""),
        requested_by=d.get("requested_by", ""),
        status=d.get("status", "running"),
        inputs=dict(d.get("inputs") or {}),
        steps=[_step_from_record(s) for s in d.get("steps", [])],
        artifact_ids=list(d.get("artifact_ids") or []),
        approval_ids=list(d.get("approval_ids") or []),
        created_at=float(d.get("created_at") or 0.0),
        updated_at=float(d.get("updated_at") or 0.0),
        error=d.get("error"),
        resumed_at=d.get("resumed_at"),
    )


def _load() -> None:
    global _LOADED
    if _LOADED:
        return
    path = _store_file()
    _RUNS.clear()
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            for item in raw.get("runs", []):
                run = _run_from_record(item)
                _RUNS[run.run_id] = run
        except (OSError, ValueError, KeyError, TypeError):
            _RUNS.clear()
    _LOADED = True


def _save() -> None:
    # Inside a deferred_save() block this only marks the store dirty; the block
    # writes once on exit. Every run in the store is re-serialized on each write,
    # so the number of writes -- not their content -- is what costs.
    if _GUARD.defer_save():
        return
    payload = {
        "version": 1,
        "runs": [_run_to_record(r) for r in sorted(_RUNS.values(), key=lambda x: x.created_at)],
    }
    store_concurrency.atomic_write_text(_store_file(), json.dumps(payload, indent=2))


def reload_for_tests() -> None:
    """Clear process memory so smoke tests can prove workflow runs are file-backed."""
    global _LOADED, _CONTROLS_LOADED
    _LOADED = False
    _CONTROLS_LOADED = False
    _RUNS.clear()
    _TEMPLATE_CONTROLS.clear()


def _load_template_controls() -> None:
    global _CONTROLS_LOADED
    if _CONTROLS_LOADED:
        return
    path = _template_controls_file()
    _TEMPLATE_CONTROLS.clear()
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            for tid, control in dict(raw.get("templates") or {}).items():
                if tid in TEMPLATES:
                    _TEMPLATE_CONTROLS[tid] = dict(control or {})
        except (OSError, ValueError, TypeError):
            _TEMPLATE_CONTROLS.clear()
    _CONTROLS_LOADED = True


def _save_template_controls() -> None:
    payload = {"version": 1, "templates": _TEMPLATE_CONTROLS}
    store_concurrency.atomic_write_text(_template_controls_file(), json.dumps(payload, indent=2))


def _template_control(template_id: str) -> dict:
    _load_template_controls()
    return dict(_TEMPLATE_CONTROLS.get(template_id) or {})


def template_dict(t: WorkflowTemplate) -> dict:
    control = _template_control(t.template_id)
    return {
        "templateId": t.template_id,
        "displayName": t.display_name,
        "description": t.description,
        "requiredCategories": list(t.required_categories),
        "outputTypes": list(t.output_types),
        "status": control.get("status", t.status),
        "version": t.version,
        "disabledBy": control.get("disabled_by", ""),
        "disabledAt": control.get("disabled_at"),
        "disabledReason": control.get("reason", ""),
    }


def step_dict(s: WorkflowStep) -> dict:
    return {
        "stepId": s.step_id,
        "type": s.type,
        "status": s.status,
        "tool": s.tool,
        "title": s.title,
        "inputs": dict(s.inputs),
        "outputs": dict(s.outputs),
        "error": s.error,
    }


def run_dict(r: WorkflowRun) -> dict:
    return {
        "runId": r.run_id,
        "templateId": r.template_id,
        "requestedBy": r.requested_by,
        "status": r.status,
        "inputs": dict(r.inputs),
        "steps": [step_dict(s) for s in r.steps],
        "artifactIds": list(r.artifact_ids),
        "approvalIds": list(r.approval_ids),
        "createdAt": r.created_at,
        "updatedAt": r.updated_at,
        "error": r.error,
        "resumedAt": r.resumed_at,
    }


def _graph_template_dict(g) -> dict:
    """Project a WorkflowGraphDefinition into the SAME dict shape template_dict()
    produces, so /workflows, /workflow-runs, /admin/workflows/{tid}/{action}, and
    workflow_health() all work identically for a user-built graph -- none of those
    call sites need to know or care whether a template_id is hardcoded or graph-backed."""
    version = g.version_record(g.published_version) if g.published_version else g.version_record()
    return {
        "templateId": g.graph_id,
        "displayName": g.display_name,
        "description": g.description,
        "requiredCategories": workflow_graph_store.required_categories_for_version(version),
        "outputTypes": workflow_graph_store.output_types_for_version(version),
        "status": g.status,
        "version": g.published_version or g.current_version,
        "disabledBy": "",
        "disabledAt": None,
        "disabledReason": "",
    }


def list_templates() -> list[dict]:
    # A graph shows up here once published, regardless of active/disabled status --
    # same as the 5 hardcoded templates (disabling is a status flag, not a delete).
    return [template_dict(t) for t in TEMPLATES.values()] + \
           [_graph_template_dict(g) for g in workflow_graph_store.list_graphs() if g.published_version > 0]


def get_template(template_id: str) -> dict | None:
    t = TEMPLATES.get(template_id)
    if t is not None:
        return template_dict(t)
    g = workflow_graph_store.get_published_graph(template_id)
    return _graph_template_dict(g) if g is not None else None


def is_graph_backed(template_id: str) -> bool:
    return template_id not in TEMPLATES and workflow_graph_store.get_graph(template_id) is not None


def set_template_status(template_id: str, *, status: str, actor: str = "", reason: str = "") -> dict | None:
    if template_id in TEMPLATES:
        if status not in ("active", "disabled"):
            return None
        _load_template_controls()
        if status == "active":
            _TEMPLATE_CONTROLS.pop(template_id, None)
        else:
            _TEMPLATE_CONTROLS[template_id] = {
                "status": "disabled",
                "disabled_by": actor,
                "disabled_at": time.time(),
                "reason": reason,
            }
        _save_template_controls()
        return get_template(template_id)
    updated = workflow_graph_store.set_graph_status(template_id, status=status, actor=actor, reason=reason)
    return _graph_template_dict(updated) if updated is not None else None


def template_is_active(template_id: str) -> bool:
    template = get_template(template_id)
    return bool(template and template.get("status") == "active")


def _token_score(text: str, tokens: list[str]) -> int:
    lowered = (text or "").lower()
    return sum(1 for token in tokens if token in lowered)


def _extract_identifier(text: str, prefixes: list[str], fallback: str) -> str:
    import re
    for prefix in prefixes:
        pattern = rf"\b{re.escape(prefix)}[-_ ]?([A-Za-z0-9]{{2,20}})\b"
        match = re.search(pattern, text or "", flags=re.IGNORECASE)
        if match:
            raw = match.group(0).replace(" ", "-")
            return raw.upper()
    quoted = re.search(r"['\"]([^'\"]{2,40})['\"]", text or "")
    if quoted:
        return quoted.group(1).strip()
    return fallback


def workflow_suggestions(message: str, *, limit: int = 4) -> list[dict]:
    """Deterministically suggest active workflow templates for an assistant prompt."""
    text = (message or "").strip()
    if not text:
        return []
    candidates = [
        {
            "template_id": "customer_360_report",
            "tokens": ["customer", "account", "360", "review", "qbr", "deck", "presentation", "orders", "spend"],
            "reason": "Prepare a governed account review with spreadsheet/deck outputs.",
            "inputs": {"sample": True, "customer_id": _extract_identifier(text, ["customer", "cust"], "SAMPLE100"), "include_packet": True},
        },
        {
            "template_id": "shipment_exception_report",
            "tokens": ["shipment", "shipping", "delay", "delayed", "exception", "tracking", "carrier", "late"],
            "reason": "Turn shipment issues into an exception workbook.",
            "inputs": {"sample": True, "customer_id": _extract_identifier(text, ["customer", "cust"], "SAMPLE100")},
        },
        {
            "template_id": "vendor_ap_summary",
            "tokens": ["vendor", "ap", "payable", "invoice", "invoices", "bill", "bills", "payment"],
            "reason": "Create a vendor AP workbook and narrative summary.",
            "inputs": {"sample": True, "vendor_code": _extract_identifier(text, ["vendor", "vend"], "VEND100")},
        },
        {
            "template_id": "customer_email_draft",
            "tokens": ["email", "draft", "message", "follow-up", "follow up", "reply", "send"],
            "reason": "Draft a governed customer email with approval-ready delivery.",
            "inputs": {"sample": True, "customer_id": _extract_identifier(text, ["customer", "cust"], "SAMPLE100"), "recipient": "manager@example.com", "topic": "customer follow-up", "request_send_approval": True},
        },
        {
            "template_id": "weekly_executive_brief",
            "tokens": ["weekly", "executive", "brief", "leadership", "kpi", "summary", "packet"],
            "reason": "Build the weekly executive brief package with approval gate.",
            "inputs": {"sample": True, "include_packet": True, "request_approval": True},
        },
    ]
    out = []
    for candidate in candidates:
        template = get_template(candidate["template_id"])
        if not template or template.get("status") != "active":
            continue
        score = _token_score(text, candidate["tokens"])
        if score <= 0:
            continue
        out.append({
            "templateId": candidate["template_id"],
            "displayName": template.get("displayName", candidate["template_id"]),
            "score": score,
            "reason": candidate["reason"],
            "inputs": candidate["inputs"],
            "outputTypes": template.get("outputTypes", []),
        })
    out.sort(key=lambda item: (-item["score"], item["displayName"]))
    return out[:max(1, int(limit or 4))]

def new_run(template_id: str, requested_by: str, inputs: dict) -> WorkflowRun:
    _load()
    now = time.time()
    run = WorkflowRun(
        run_id="wr_" + uuid.uuid4().hex[:16],
        template_id=template_id,
        requested_by=requested_by,
        status="running",
        inputs=dict(inputs),
        created_at=now,
        updated_at=now,
    )
    _RUNS[run.run_id] = run
    _save()
    return run


# ── Mutators ──────────────────────────────────────────────────────────────────
#
# THE RULE, and the reason these look the way they do: a mutator applies its
# change to the CURRENTLY STORED record, never to the WorkflowRun object the
# caller passed in. Callers -- `_interpret` above all -- hold one `run` across
# many `await`s (every governed tool call, every LLM call), so by the time they
# write back, their copy can be arbitrarily stale. Writing it back wholesale is
# a lost update: the classic symptom was cancelling a running workflow and
# watching it finish anyway, because the interpreter's final
# `update_run(run, status="completed")` overwrote the cancel with a copy taken
# before it happened (concurrency_and_scale.md §3.2).
#
# The caller's object is still accepted and returned, so no call site changed;
# it is used for its `run_id` and as the fallback if the record has vanished.


def _current(run: WorkflowRun) -> WorkflowRun:
    return _RUNS.get(run.run_id) or run


def update_run(run: WorkflowRun, **changes) -> WorkflowRun:
    _load()
    with _GUARD.lock:
        current = _current(run)
        # `cancelled` and `failed` are sticky (store_concurrency.STICKY_STATUSES).
        # A cancel that lands while work is still in flight must survive whatever
        # that work writes when it finishes -- otherwise "cancel" is advisory at
        # best. Everything ELSE in the late write is still applied: the steps it
        # completed really did happen and belong in the record.
        if "status" in changes and store_concurrency.is_sticky(current.status) \
                and changes["status"] != current.status:
            changes.pop("status", None)
            changes.pop("error", None)
        changes.setdefault("updated_at", time.time())
        updated = replace(current, **changes)
        _RUNS[updated.run_id] = updated
        _save()
        return updated


def add_step(run: WorkflowRun, step: WorkflowStep) -> WorkflowRun:
    """Append to the stored run's steps, not to the caller's snapshot of them --
    otherwise two writers each append to their own copy and one list wins whole,
    silently dropping the other's step."""
    _load()
    with _GUARD.lock:
        return update_run(run, steps=[*_current(run).steps, step])


def complete_step(run: WorkflowRun, step_id: str, outputs: dict | None = None) -> WorkflowRun:
    _load()
    with _GUARD.lock:
        steps = [replace(s, status="completed", outputs=dict(outputs or {})) if s.step_id == step_id else s
                 for s in _current(run).steps]
        return update_run(run, steps=steps)


def fail_step(run: WorkflowRun, step_id: str, error: str) -> WorkflowRun:
    _load()
    with _GUARD.lock:
        steps = [replace(s, status="failed", error=error) if s.step_id == step_id else s
                 for s in _current(run).steps]
        return update_run(run, steps=steps, status="failed", error=error)


def workflow_health(now: float | None = None, *, stuck_after_sec: float = 1800) -> dict:
    """Summarize workflow run health for admin monitoring."""
    _load()
    now = now or time.time()
    runs = list(_RUNS.values())
    status_counts: dict[str, int] = {}
    by_template: dict[str, dict] = {}
    stuck = []
    durations = []
    terminal = {"completed", "failed", "cancelled"}
    for run in runs:
        status_counts[run.status] = status_counts.get(run.status, 0) + 1
        template = by_template.setdefault(run.template_id, {
            "templateId": run.template_id,
            "displayName": (get_template(run.template_id) or {}).get("displayName", run.template_id),
            "runs": 0,
            "completed": 0,
            "failed": 0,
            "cancelled": 0,
            "approvalRequired": 0,
            "running": 0,
            "artifacts": 0,
            "lastRunAt": None,
            "lastStatus": "never_run",
            "avgDurationSec": 0,
            "_durations": [],
        })
        template["runs"] += 1
        template["artifacts"] += len(run.artifact_ids)
        if run.status == "approval_required":
            template["approvalRequired"] += 1
        elif run.status in template:
            template[run.status] += 1
        if run.created_at and (template["lastRunAt"] is None or run.created_at > template["lastRunAt"]):
            template["lastRunAt"] = run.created_at
            template["lastStatus"] = run.status
        if run.status in terminal or run.status == "approval_required":
            duration = max(0.0, (run.updated_at or run.created_at) - run.created_at)
            durations.append(duration)
            template["_durations"].append(duration)
        if run.status == "running" and now - (run.updated_at or run.created_at) >= stuck_after_sec:
            stuck.append(run_dict(run))
    for tid, template in list(by_template.items()):
        ds = template.pop("_durations", [])
        template["avgDurationSec"] = round(sum(ds) / len(ds), 2) if ds else 0
        template["failureRate"] = round(template["failed"] / template["runs"] * 100, 1) if template["runs"] else 0
        template["artifactRate"] = round(template["artifacts"] / template["runs"], 2) if template["runs"] else 0
    for template in list_templates():
        by_template.setdefault(template["templateId"], {
            "templateId": template["templateId"],
            "displayName": template["displayName"],
            "runs": 0,
            "completed": 0,
            "failed": 0,
            "cancelled": 0,
            "approvalRequired": 0,
            "running": 0,
            "artifacts": 0,
            "lastRunAt": None,
            "lastStatus": "never_run",
            "avgDurationSec": 0,
            "failureRate": 0,
            "artifactRate": 0,
        })
    total = len(runs)
    completed = status_counts.get("completed", 0)
    failed = status_counts.get("failed", 0)
    approval_required = status_counts.get("approval_required", 0)
    health = "healthy"
    if stuck or (total and failed / total >= 0.25):
        health = "degraded"
    if total and failed / total >= 0.5:
        health = "unhealthy"
    return {
        "health": health,
        "totalRuns": total,
        "statusCounts": status_counts,
        "completedRuns": completed,
        "failedRuns": failed,
        "approvalRequiredRuns": approval_required,
        "failureRate": round(failed / total * 100, 1) if total else 0,
        "approvalRate": round(approval_required / total * 100, 1) if total else 0,
        "avgDurationSec": round(sum(durations) / len(durations), 2) if durations else 0,
        "stuckRuns": stuck,
        "templates": sorted(by_template.values(), key=lambda x: (x["displayName"].lower(), x["templateId"])),
    }

def list_runs(requested_by: str | None = None, limit: int = 100) -> list[dict]:
    _load()
    runs = list(_RUNS.values())
    if requested_by:
        runs = [r for r in runs if r.requested_by == requested_by]
    runs.sort(key=lambda r: r.created_at, reverse=True)
    return [run_dict(r) for r in runs[:limit]]


def get_run(run_id: str) -> dict | None:
    _load()
    run = _RUNS.get(run_id)
    return run_dict(run) if run else None


def sample_customer_payload(customer_id: str = "SAMPLE100") -> dict:
    return {
        "customer_id": customer_id,
        "overview": {
            "status": "success",
            "customerId": customer_id,
            "name": "Sample Dental Group",
            "primaryContact": {"name": "Jane Sample", "email": "jane.sample@example.com", "phone": "555-0100"},
            "profile": {"termsId": "NET30", "creditLimit": 50000, "customerCategory": "Dental Practice"},
        },
        "order_summary": {
            "status": "success",
            "customerId": customer_id,
            "orderCount": 4,
            "grandTotal": 12345.67,
            "averageOrderValue": 3086.42,
            "byStatus": {"Open": 1, "Completed": 3},
        },
        "orders": {
            "status": "success",
            "records": [
                {"orderNumber": "SO-1001", "status": "Completed", "date": "2026-06-01", "total": 4200.00},
                {"orderNumber": "SO-1002", "status": "Completed", "date": "2026-06-14", "total": 3050.50},
                {"orderNumber": "SO-1003", "status": "Open", "date": "2026-07-02", "total": 1995.17},
                {"orderNumber": "SO-1004", "status": "Completed", "date": "2026-07-12", "total": 3100.00},
            ],
        },
        "shipments": {
            "status": "success",
            "records": [
                {"orderNumber": "SO-1001", "shipmentStatus": "Delivered", "trackingNumber": "1Z-SAMPLE-1"},
                {"orderNumber": "SO-1002", "shipmentStatus": "Delivered", "trackingNumber": "1Z-SAMPLE-2"},
                {"orderNumber": "SO-1003", "shipmentStatus": "In Transit", "trackingNumber": "1Z-SAMPLE-3"},
            ],
        },
    }


def customer_360_tables(payload: dict) -> list[dict]:
    overview = payload.get("overview") or {}
    summary = payload.get("order_summary") or {}
    orders = (payload.get("orders") or {}).get("records") or []
    shipments = (payload.get("shipments") or {}).get("records") or []
    return [
        {
            "name": "Summary",
            "rows": [
                {"Metric": "Customer ID", "Value": payload.get("customer_id", "")},
                {"Metric": "Customer Name", "Value": overview.get("name") or overview.get("customerName") or ""},
                {"Metric": "Order Count", "Value": summary.get("orderCount", "")},
                {"Metric": "Grand Total", "Value": summary.get("grandTotal", "")},
                {"Metric": "Average Order Value", "Value": summary.get("averageOrderValue", "")},
            ],
        },
        {"name": "Orders", "rows": orders},
        {"name": "Shipments", "rows": shipments},
    ]




def customer_email_subject(payload: dict, topic: str = "") -> str:
    overview = payload.get("overview") or {}
    customer_name = overview.get("name") or overview.get("customerName") or payload.get("customer_id") or "Customer"
    topic = (topic or "account follow-up").strip()
    return f"{customer_name}: {topic}"


def customer_email_body(payload: dict, topic: str = "", tone: str = "professional") -> str:
    overview = payload.get("overview") or {}
    summary = payload.get("order_summary") or {}
    shipments = (payload.get("shipments") or {}).get("records") or []
    orders = (payload.get("orders") or {}).get("records") or []
    customer_name = overview.get("name") or overview.get("customerName") or payload.get("customer_id") or "the customer"
    open_shipments = [s for s in shipments if str(s.get("shipmentStatus") or s.get("status") or "").lower() not in ("delivered", "complete", "completed")]
    lines = [
        f"Hello,",
        "",
        f"Here is a {tone or 'professional'} follow-up draft for {customer_name} regarding {topic or 'the current account review'}.",
        "",
        f"- Orders reviewed: {summary.get('orderCount', len(orders))}",
        f"- Total spend in scope: {summary.get('grandTotal', 'n/a')}",
        f"- Open or in-transit shipments: {len(open_shipments)}",
        "",
        "Suggested next step: review the attached packet if included, then confirm the owner and timing for customer follow-up.",
        "",
        "Regards,",
        "Governed AI Office Assistant",
    ]
    return "\n".join(lines)

def customer_360_sections(payload: dict) -> list[dict]:
    overview = payload.get("overview") or {}
    summary = payload.get("order_summary") or {}
    shipments = (payload.get("shipments") or {}).get("records") or []
    open_shipments = [s for s in shipments if str(s.get("shipmentStatus") or s.get("status") or "").lower() not in ("delivered", "complete", "completed")]
    customer_name = overview.get("name") or overview.get("customerName") or payload.get("customer_id") or "Customer"
    return [
        {"heading": "Executive Summary", "bullets": [
            f"Customer: {customer_name}",
            f"Orders reviewed: {summary.get('orderCount', 'n/a')}",
            f"Total spend in scope: {summary.get('grandTotal', 'n/a')}",
        ]},
        {"heading": "Shipment Status", "bullets": [
            f"Shipment records reviewed: {len(shipments)}",
            f"Open or in-transit shipments: {len(open_shipments)}",
        ]},
        {"heading": "Recommended Next Steps", "bullets": [
            "Review open orders and shipment exceptions.",
            "Use the spreadsheet appendix for line-level follow-up.",
            "Route externally-facing communication through approval before sending.",
        ]},
    ]



def _money(value) -> float:
    try:
        return round(float(value or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def sample_shipment_payload(customer_id: str = "SAMPLE100") -> dict:
    return {
        "customer_id": customer_id,
        "shipments": {
            "status": "success",
            "records": [
                {"customerId": customer_id, "customerName": "Sample Dental Group", "orderNumber": "SO-1001", "shipmentStatus": "Delivered", "trackingNumber": "1Z-SAMPLE-1", "shipDate": "2026-07-08", "carrier": "UPS"},
                {"customerId": customer_id, "customerName": "Sample Dental Group", "orderNumber": "SO-1003", "shipmentStatus": "In Transit", "trackingNumber": "1Z-SAMPLE-3", "shipDate": "2026-07-19", "carrier": "UPS"},
                {"customerId": customer_id, "customerName": "Sample Dental Group", "orderNumber": "SO-1005", "shipmentStatus": "Exception", "trackingNumber": "1Z-SAMPLE-5", "shipDate": "2026-07-20", "carrier": "FedEx"},
                {"customerId": customer_id, "customerName": "Sample Dental Group", "orderNumber": "SO-1006", "shipmentStatus": "Delayed", "trackingNumber": "1Z-SAMPLE-6", "shipDate": "2026-07-21", "carrier": "DHL"},
            ],
        },
    }


def shipment_exception_tables(payload: dict) -> list[dict]:
    records = list((payload.get("shipments") or {}).get("records") or [])
    exceptions = [r for r in records if str(r.get("shipmentStatus") or r.get("status") or "").lower() not in ("delivered", "complete", "completed")]
    by_status: dict[str, int] = {}
    for row in exceptions:
        status = str(row.get("shipmentStatus") or row.get("status") or "Unknown")
        by_status[status] = by_status.get(status, 0) + 1
    return [
        {"name": "Summary", "rows": [
            {"Metric": "Customer ID", "Value": payload.get("customer_id", "")},
            {"Metric": "Shipments reviewed", "Value": len(records)},
            {"Metric": "Open exceptions", "Value": len(exceptions)},
        ]},
        {"name": "Exceptions", "rows": exceptions},
        {"name": "By Status", "rows": [{"Status": k, "Count": v} for k, v in sorted(by_status.items())]},
        {"name": "All Shipments", "rows": records},
    ]


def sample_vendor_payload(vendor_code: str = "VEND100") -> dict:
    return {
        "vendor_code": vendor_code,
        "vendor": {
            "status": "success",
            "vendorCode": vendor_code,
            "vendorName": "Sample Supply Co.",
            "vendorClassId": "SUPPLIES",
            "termsId": "NET30",
            "curyId": "USD",
            "paymentMethodId": "ACH",
            "vendor1099": False,
        },
        "ap_invoices": {
            "status": "success",
            "records": [
                {"invoiceNumber": "AP-1001", "docType": "Bill", "invoiceDate": "2026-06-20", "dueDate": "2026-07-20", "lineTotal": 1250.00, "taxTotal": 81.25, "paid": True},
                {"invoiceNumber": "AP-1002", "docType": "Bill", "invoiceDate": "2026-07-01", "dueDate": "2026-07-31", "lineTotal": 2480.40, "taxTotal": 161.23, "paid": False},
                {"invoiceNumber": "AP-1003", "docType": "Bill", "invoiceDate": "2026-07-10", "dueDate": "2026-08-09", "lineTotal": 775.50, "taxTotal": 50.41, "paid": False},
            ],
        },
    }


def vendor_ap_tables(payload: dict) -> list[dict]:
    vendor = payload.get("vendor") or {}
    invoices = list((payload.get("ap_invoices") or {}).get("records") or [])
    unpaid = [r for r in invoices if not r.get("paid")]
    total = sum(_money(r.get("lineTotal")) + _money(r.get("taxTotal")) for r in invoices)
    unpaid_total = sum(_money(r.get("lineTotal")) + _money(r.get("taxTotal")) for r in unpaid)
    return [
        {"name": "Summary", "rows": [
            {"Metric": "Vendor Code", "Value": payload.get("vendor_code", "")},
            {"Metric": "Vendor Name", "Value": vendor.get("vendorName") or vendor.get("name") or ""},
            {"Metric": "Terms", "Value": vendor.get("termsId", "")},
            {"Metric": "Invoice Count", "Value": len(invoices)},
            {"Metric": "Unpaid Count", "Value": len(unpaid)},
            {"Metric": "Total AP", "Value": round(total, 2)},
            {"Metric": "Unpaid AP", "Value": round(unpaid_total, 2)},
        ]},
        {"name": "AP Invoices", "rows": invoices},
        {"name": "Unpaid", "rows": unpaid},
    ]


def vendor_ap_sections(payload: dict) -> list[dict]:
    vendor = payload.get("vendor") or {}
    invoices = list((payload.get("ap_invoices") or {}).get("records") or [])
    unpaid = [r for r in invoices if not r.get("paid")]
    unpaid_total = sum(_money(r.get("lineTotal")) + _money(r.get("taxTotal")) for r in unpaid)
    return [
        {"heading": "Vendor Overview", "bullets": [
            f"Vendor: {vendor.get('vendorName') or vendor.get('name') or payload.get('vendor_code', 'Vendor')}",
            f"Terms: {vendor.get('termsId', 'n/a')}",
            f"Currency: {vendor.get('curyId', 'n/a')}",
        ]},
        {"heading": "AP Position", "bullets": [
            f"Invoices reviewed: {len(invoices)}",
            f"Unpaid invoices: {len(unpaid)}",
            f"Unpaid total: {round(unpaid_total, 2)}",
        ]},
        {"heading": "Recommended Follow-up", "bullets": [
            "Review unpaid invoices due in the next payment cycle.",
            "Confirm vendor terms and preferred payment method before release.",
            "Use the spreadsheet appendix for finance reconciliation.",
        ]},
    ]



def sample_executive_payload(start_date: str = "2026-07-15", end_date: str = "2026-07-22") -> dict:
    customers = [
        {"rank": 1, "customerId": "CUST-100", "name": "Sample Dental Group", "totalSpend": 42150.75, "orderCount": 18, "risk": "Shipment delays"},
        {"rank": 2, "customerId": "CUST-245", "name": "North Clinic Network", "totalSpend": 38200.00, "orderCount": 12, "risk": "Renewal review"},
        {"rank": 3, "customerId": "CUST-311", "name": "Prairie Ortho", "totalSpend": 24750.50, "orderCount": 9, "risk": "Open AP dispute"},
        {"rank": 4, "customerId": "CUST-420", "name": "Harbour Hygiene", "totalSpend": 19880.25, "orderCount": 7, "risk": "None"},
        {"rank": 5, "customerId": "CUST-511", "name": "Capital Endodontics", "totalSpend": 17440.10, "orderCount": 5, "risk": "Backorder watch"},
    ]
    exceptions = [
        {"Area": "Shipments", "Signal": "3 open exceptions", "Owner": "Operations", "Priority": "High"},
        {"Area": "Finance", "Signal": "2 unpaid vendor invoices above threshold", "Owner": "Finance", "Priority": "Medium"},
        {"Area": "Sales", "Signal": "2 renewal accounts need follow-up", "Owner": "Account Management", "Priority": "Medium"},
    ]
    return {
        "start_date": start_date,
        "end_date": end_date,
        "top_customers": {"status": "success", "records": customers, "totalSpend": round(sum(c["totalSpend"] for c in customers), 2)},
        "exceptions": exceptions,
        "kpis": {
            "Revenue in brief": round(sum(c["totalSpend"] for c in customers), 2),
            "Top customer count": len(customers),
            "Open executive risks": len(exceptions),
            "High priority risks": sum(1 for e in exceptions if e.get("Priority") == "High"),
        },
    }


def executive_brief_tables(payload: dict) -> list[dict]:
    top = list((payload.get("top_customers") or {}).get("records") or payload.get("top_customers") or [])
    exceptions = list(payload.get("exceptions") or [])
    kpis = payload.get("kpis") or {}
    return [
        {"name": "Executive KPIs", "rows": [{"Metric": k, "Value": v} for k, v in kpis.items()]},
        {"name": "Top Customers", "rows": top},
        {"name": "Risks and Actions", "rows": exceptions},
    ]


def executive_brief_sections(payload: dict) -> list[dict]:
    top = list((payload.get("top_customers") or {}).get("records") or payload.get("top_customers") or [])
    exceptions = list(payload.get("exceptions") or [])
    kpis = payload.get("kpis") or {}
    leader = top[0] if top else {}
    return [
        {"heading": "Weekly Executive Summary", "bullets": [
            f"Period: {payload.get('start_date', '')} to {payload.get('end_date', '')}",
            f"Revenue represented in top accounts: {kpis.get('Revenue in brief', 'n/a')}",
            f"Open executive risks: {kpis.get('Open executive risks', len(exceptions))}",
        ]},
        {"heading": "Top Account Signal", "bullets": [
            f"Top account: {leader.get('name', 'n/a')}",
            f"Spend: {leader.get('totalSpend', 'n/a')}",
            f"Watch item: {leader.get('risk', 'n/a')}",
        ]},
        {"heading": "Priority Actions", "bullets": [
            f"{e.get('Priority', 'Priority')}: {e.get('Area', 'Area')} - {e.get('Signal', '')} ({e.get('Owner', 'Unassigned')})" for e in exceptions[:5]
        ] or ["No priority actions captured."]},
    ]


def get_run_record(run_id: str) -> WorkflowRun | None:
    _load()
    return _RUNS.get(run_id)


def cancel_run(run_id: str, *, actor: str = "", reason: str = "") -> WorkflowRun | None:
    run = get_run_record(run_id)
    if not run:
        return None
    if run.status in ("completed", "failed", "cancelled"):
        return run
    steps = [replace(s, status="cancelled") if s.status in ("pending", "running") else s for s in run.steps]
    return update_run(run, status="cancelled", steps=steps, error=reason or f"cancelled by {actor}".strip())


def mark_approval_required(run_id: str, *, approval_id: str, artifact_ids: list[str] | None = None) -> WorkflowRun | None:
    run = get_run_record(run_id)
    if not run:
        return None
    approvals = [*run.approval_ids]
    if approval_id and approval_id not in approvals:
        approvals.append(approval_id)
    return update_run(run, status="approval_required", approval_ids=approvals, artifact_ids=list(artifact_ids or run.artifact_ids))


def resume_run(run_id: str, *, approval_id: str = "", actor: str = "") -> WorkflowRun | None:
    run = get_run_record(run_id)
    if not run:
        return None
    if run.status != "approval_required":
        return run
    steps = []
    for step in run.steps:
        if step.type == "approval" and step.status in ("pending", "running"):
            outputs = dict(step.outputs)
            if approval_id:
                outputs.setdefault("approvalId", approval_id)
            outputs["resumedBy"] = actor
            steps.append(replace(step, status="completed", outputs=outputs))
        else:
            steps.append(step)
    return update_run(run, status="completed", steps=steps, resumed_at=time.time())
