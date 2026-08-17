# Governed Office AI Assistant Expansion Plan

> **STALE — superseded by `STAGE2_PLAN.md`.** Written 2026-07-22, before artifacts,
> approvals, automation, the workflow-graph engine, knowledge, calendar-send,
> email-send, code-plans, and document-reviews all shipped (see `STAGE1_README.md`
> for the real current tool catalogue). Its "Current Platform Baseline" (§2) and
> "New MCP Backends" (§8) sections describe work that was already done differently
> or has since moved on — **do not plan against them.** The product-vision material
> (use cases §4, UX page layout §10, categories/risk taxonomy §7) is still reasonable
> background reading. For current MCP-scaling, write-tool, and LLM strategy, see
> `STAGE2_PLAN.md`.

**Project name:** Governed Office AI Assistant  
**Base system:** `governence_agent` governance gateway + MiniERP MCP platform  
**Date:** 2026-07-22  
**Goal:** Expand the current governed MiniERP assistant into a complete internal office-workflow platform that can safely retrieve business data, reason over it, generate office artifacts, coordinate approvals, and automate repeatable coworker workflows.

---

## 1. Executive Summary

The current platform already has the hardest part of an enterprise AI assistant: a governed tool plane. It has a gateway, MCP backends, category-based authorization, redaction, audit, user/admin access management, chat history, and a local/self-hosted LLM path.

The expansion should build on that foundation instead of becoming a generic chatbot. The product should become a **governed office workbench**:

> A coworker asks for a business outcome, the assistant retrieves authorized data, generates a report/deck/spreadsheet/email, asks for approval when required, and preserves a full audit trail.

Example target workflow:

> "Find customer ABC's last 12 months of orders, summarize shipment delays, create an Excel report, generate a PowerPoint for the account review, draft a follow-up email, and send it to my manager for approval."

The product differentiator is not only AI generation. It is the combination of:

- Governed access to operational data.
- Local/private LLM usage.
- Repeatable workflow execution.
- Finished Microsoft Office-compatible outputs.
- Human approval gates.
- Full policy, redaction, and audit coverage.

---

## 2. Current Platform Baseline

### 2.1 Existing Strengths

The repo already contains the core building blocks:

- `gateway/app.py`
  - Public governed gateway.
  - Dashboard routes.
  - Chat routes.
  - Admin routes.
  - Policy enforcement pipeline.
  - Audit logging.
  - Backend pause controls.
  - Access request handling.

- `gateway/orchestrator.py`
  - In-process chat orchestrator.
  - OpenAI-compatible local LLM support.
  - Tool-loop execution.
  - Qwen text-format tool-call parsing.
  - Grant-filtered tool specs.

- `governance_core/policy/manifest.py`
  - Canonical tool registry.
  - Backend ownership.
  - Account-scoped tool definitions.
  - Field classification.
  - Redaction metadata.

- `governance_core/policy/decision.py`
  - Deterministic PDP.
  - Tool allow/deny.
  - Scope verification.
  - Redaction plan generation.

- `governance_core/store/*`
  - File, local, and Cosmos-backed policy stores.
  - Consumers.
  - Categories.
  - Access requests.
  - Chat sessions.
  - Global controls.

- `mcp-minierp/app.py`
  - Consolidated MiniERP MCP backend.
  - Customers/accounts tools.
  - Orders tools.
  - Shipments tools.
  - Finance tools.
  - Resolver tools.
  - Composite/analytics tools.

- `gateway/static/*`
  - Current dashboard, admin, account, chat, signup pages.

### 2.2 Current Tool Surface

The current MiniERP surface already covers important office workflows:

- Customer lookup:
  - `find_customer`
  - `resolve_contact`
  - `get_customer_profile`
  - `get_contacts`
  - `get_addresses`
  - `get_customer_overview`

- Orders:
  - `get_customer_orders`
  - `get_customer_order_total`
  - `get_order_details`
  - `get_product_details_in_order`
  - `get_order_overview`
  - `get_customer_order_summary`

- Shipments:
  - `get_shipping_by_order`
  - `get_shipping_by_shipment`
  - `get_customer_shipment_status`

- Finance:
  - `get_invoice_details`
  - `get_vendor_details`
  - `get_vendor_ap_invoices`
  - `get_ap_invoice_details`
  - `get_po_order_status`
  - `get_gl_account_transactions`

- Analytics:
  - `get_product_sales`
  - `get_orders_by_product`
  - `get_customers_by_region`
  - `get_top_customers_by_spend`

### 2.3 Main Gap

The current assistant can answer questions and call governed tools, but it does not yet provide a full office workflow loop:

1. Collect user intent.
2. Retrieve governed data.
3. Transform data into tables/charts/narratives.
4. Generate durable files.
5. Preview/edit files.
6. Route outputs for review.
7. Send or store deliverables.
8. Re-run the workflow later.
9. Audit the full chain.

This gap is the expansion opportunity.

---

## 3. Product Vision

### 3.1 North Star

Build an internal AI assistant that turns business data into finished office work products.

The assistant should support:

- Natural language requests.
- Governed data retrieval.
- Office document generation.
- Spreadsheet generation and analysis.
- Presentation generation.
- PDF extraction and summarization.
- Email drafting.
- Approval workflows.
- Scheduled reports.
- Repeatable workflow templates.
- Role-specific assistant modes.
- Admin observability and control.

### 3.2 Product Positioning

Recommended positioning:

> A governed AI office assistant for safely turning company data into reports, decks, spreadsheets, drafts, and repeatable business workflows.

Avoid positioning it as:

- "Another chatbot."
- "A generic AI wrapper."
- "A simple MiniERP search page."

The platform should become the safe execution layer for office work.

### 3.3 Target Users

Primary user groups:

- Customer service representatives.
- Sales/account managers.
- Operations coordinators.
- Finance staff.
- Managers/executives.
- Internal analysts.
- IT/admin users.

Secondary user groups:

- Autonomous internal agents.
- Developers maintaining workflows.
- Compliance/security reviewers.

---

## 4. Core Use Cases

### 4.1 Customer Service

#### Customer Lookup and Response

User asks:

> "Customer Jane Smith called about order 12345. What is the status and what should I tell her?"

Assistant should:

1. Resolve customer by name/email/phone/order.
2. Retrieve order details.
3. Retrieve shipment/tracking details.
4. Summarize status in plain language.
5. Draft a customer-safe response.
6. Redact internal/finance fields if user lacks access.

Outputs:

- Chat answer.
- Optional email draft.
- Optional support note.

#### Escalation Brief

User asks:

> "Create an escalation summary for customer ABC's delayed shipments this month."

Assistant should:

1. Resolve customer.
2. Retrieve recent orders.
3. Retrieve shipment statuses.
4. Identify delayed/problem shipments.
5. Generate a one-page DOCX/PDF escalation brief.

Outputs:

- DOCX.
- PDF.
- Summary in chat.

### 4.2 Sales and Account Management

#### Customer Account Review

User asks:

> "Prepare a quarterly account review deck for customer ABC."

Assistant should:

1. Resolve customer.
2. Retrieve customer overview.
3. Retrieve order history.
4. Retrieve shipment performance.
5. Retrieve top products/orders.
6. Generate Excel data appendix.
7. Generate PowerPoint account review deck.
8. Include notes for talking points.

Outputs:

- PPTX deck.
- XLSX appendix.
- Executive summary.

#### Renewal or Business Review Prep

User asks:

> "Show me what changed for this customer since last quarter."

Assistant should:

1. Compare current quarter vs previous quarter.
2. Summarize spend changes.
3. Summarize order frequency changes.
4. Summarize delayed shipments or exceptions.
5. Suggest discussion points.

Outputs:

- Chat summary.
- Optional report/deck.

### 4.3 Finance

#### Invoice Packet

User asks:

> "Generate an invoice packet for invoice INV123."

Assistant should:

1. Retrieve invoice details.
2. Retrieve customer profile if permitted.
3. Retrieve related order/shipment if available.
4. Create a PDF or DOCX packet.
5. Mark sensitive financial fields according to policy.

Outputs:

- DOCX/PDF packet.
- Audit record.

#### Vendor Summary

User asks:

> "Create a vendor AP summary for vendor VEND001 for the last 90 days."

Assistant should:

1. Retrieve vendor profile.
2. Retrieve AP invoices.
3. Aggregate totals.
4. Generate XLSX.
5. Generate finance summary.

Outputs:

- XLSX report.
- Chat summary.

### 4.4 Operations

#### Shipment Exception Report

User asks:

> "Create a report of customers with delayed shipments this week."

Assistant should:

1. Query shipment/order data.
2. Identify exceptions.
3. Group by customer.
4. Generate XLSX.
5. Generate manager summary.

Outputs:

- XLSX exception report.
- Optional PPTX summary.

### 4.5 Management

#### Weekly Executive Brief

User asks:

> "Generate this week's customer and order performance brief."

Assistant should:

1. Pull top customers by spend.
2. Pull order trends.
3. Pull shipment exceptions.
4. Pull finance highlights if permitted.
5. Generate PPTX.
6. Generate PDF version.

Outputs:

- PPTX.
- PDF.
- Chat summary.

### 4.6 IT and Admin

#### Policy and Usage Monitoring

Admin asks:

> "Show unusual assistant activity in the last 24 hours."

Assistant should:

1. Query audit logs.
2. Highlight denials, bursts, errors, enumeration patterns.
3. Summarize affected users/agents.
4. Suggest remediation actions.

Outputs:

- Admin dashboard view.
- Chat summary.

#### opencode-Powered Internal Automation

Admin/developer asks:

> "Create a new workflow template for monthly customer reports."

Assistant should:

1. Use opencode in a controlled developer lane.
2. Generate/edit workflow template files.
3. Run tests.
4. Produce diff for review.

Outputs:

- Code/template changes.
- Test results.
- Review notes.

---

## 5. Target Architecture

### 5.1 High-Level Architecture

```text
Users / Agents
    |
    | dashboard chat, workflow UI, scheduled jobs, webhooks
    v
Governance Gateway
    |
    | auth, categories, PDP, redaction, rate limits, audit, controls
    v
Workflow Orchestrator
    |
    | plans, steps, approvals, artifacts, retries
    v
Governed MCP Tool Plane
    |
    |-- mcp-minierp      customer/order/shipment/finance data
    |-- mcp-office       docx/xlsx/pptx/pdf generation and manipulation
    |-- mcp-files        artifact storage, templates, uploads
    |-- mcp-email        draft/send/approval-gated email
    |-- mcp-calendar     meeting prep and scheduling
    |-- mcp-code         opencode-powered developer/admin automation
    |-- mcp-knowledge    internal docs/RAG/search
    |
    v
External/Internal Systems
    |
    | MiniERP, ONLYOFFICE, file storage, email, calendar, CRM, docs
```

### 5.2 Keep the Gateway as the Control Point

The current gateway should remain the single public governed entry point.

All new capabilities should register through:

- Manifest entries.
- Category grants.
- Field classifications.
- Redaction plans.
- Audit logging.
- Admin pause controls.
- Rate limits.

Avoid allowing workflow code or office-generation code to bypass the gateway for business data.

### 5.3 Add a Workflow Orchestrator

The workflow orchestrator should sit beside the current chat orchestrator.

Responsibilities:

- Convert user intent into a workflow plan.
- Execute deterministic workflow steps.
- Call governed MCP tools.
- Persist workflow runs.
- Track artifacts.
- Pause for human approval.
- Resume after approval.
- Handle retries/failures.
- Emit audit events.

Initial implementation can be simple Python code inside `gateway/`, then split later if needed.

Recommended files:

```text
gateway/
  workflows.py              workflow definitions and execution engine
  workflow_routes.py        dashboard/API routes
  artifact_routes.py        artifact download/preview routes

governance_core/
  workflow_models.py        WorkflowTemplate, WorkflowRun, WorkflowStep
  artifact_store.py         metadata model and storage interface
```

### 5.4 Add Dedicated MCP Backends

Recommended new backend directories:

```text
mcp-office/
  app.py
  builders/
    excel.py
    powerpoint.py
    word.py
    pdf.py
  templates/
    customer_account_review/
    shipment_exception_report/
    invoice_packet/

mcp-files/
  app.py
  store.py
  scan.py
  templates.py

mcp-email/
  app.py
  drafts.py
  send.py
  approvals.py

mcp-calendar/
  app.py
  meetings.py

mcp-code/
  app.py
  opencode_runner.py
```

The first production-quality backend should be `mcp-office`.

---

## 6. Core Platform Concepts

### 6.1 Artifact

An artifact is any durable output generated or uploaded through the assistant.

Examples:

- XLSX report.
- PPTX deck.
- DOCX brief.
- PDF packet.
- CSV export.
- Email draft.
- Uploaded source document.
- Extracted table.

Recommended metadata:

```json
{
  "id": "art_...",
  "ownerConsumerId": "c_...",
  "createdBy": "user|agent",
  "createdAt": "2026-07-22T00:00:00Z",
  "type": "xlsx|pptx|docx|pdf|csv|email_draft|json",
  "title": "Customer ABC Account Review",
  "path": "artifacts/2026/07/art_.../account-review.pptx",
  "sourceWorkflowRunId": "wr_...",
  "sourceToolCalls": ["audit_...", "audit_..."],
  "classification": ["INTERNAL", "PII", "SENSITIVE"],
  "retentionDays": 90,
  "status": "ready|failed|expired|pending_approval",
  "downloadCount": 0,
  "checksum": "sha256:..."
}
```

### 6.2 Workflow Template

A workflow template defines a repeatable business process.

Recommended metadata:

```json
{
  "id": "customer_account_review",
  "displayName": "Customer Account Review",
  "description": "Create an account review deck and spreadsheet appendix.",
  "version": 1,
  "status": "active|draft|disabled",
  "owner": "sales_ops",
  "requiredCategories": ["accounts", "orders", "shipments"],
  "optionalCategories": ["finance"],
  "requiredLevels": ["INTERNAL"],
  "outputTypes": ["pptx", "xlsx"],
  "approvalPolicy": {
    "required": false,
    "requiredWhenExternalSend": true,
    "approverRole": "manager"
  },
  "inputs": [
    {"name": "customer_query", "type": "string", "required": true},
    {"name": "period", "type": "date_range", "required": true}
  ],
  "steps": [
    {"id": "resolve_customer", "type": "tool_call"},
    {"id": "fetch_overview", "type": "tool_call"},
    {"id": "build_excel", "type": "artifact"},
    {"id": "build_deck", "type": "artifact"}
  ]
}
```

### 6.3 Workflow Run

A workflow run is one execution instance.

Recommended metadata:

```json
{
  "id": "wr_...",
  "templateId": "customer_account_review",
  "templateVersion": 1,
  "requestedBy": "c_...",
  "actorType": "user|agent",
  "status": "running|paused|approval_required|completed|failed|cancelled",
  "createdAt": "2026-07-22T00:00:00Z",
  "updatedAt": "2026-07-22T00:02:00Z",
  "inputs": {
    "customer_query": "ABC Dental",
    "period": "last_quarter"
  },
  "steps": [],
  "artifacts": [],
  "auditRefs": [],
  "error": null
}
```

### 6.4 Approval

Approvals are required when an action has business consequences.

Approval-required actions:

- Sending an external email.
- Exporting broad PII datasets.
- Exporting broad sensitive financial datasets.
- Running autonomous agent workflows.
- Deleting or overwriting files.
- Scheduling recurring workflows.
- Calling opencode with edit permissions.

Approval metadata:

```json
{
  "id": "appr_...",
  "workflowRunId": "wr_...",
  "requestedBy": "c_...",
  "approverConsumerId": "c_manager",
  "status": "pending|approved|denied|expired",
  "riskLevel": "low|medium|high",
  "reason": "External email send",
  "previewArtifactIds": ["art_..."],
  "createdAt": "...",
  "decidedAt": null
}
```

---

## 7. Governance Expansion

### 7.1 Extend Categories

Current categories are data-domain oriented:

- `accounts`
- `orders`
- `shipments`
- `finance`

Add office/action categories:

- `office`
  - Generate documents, spreadsheets, presentations, PDFs.
  - No external delivery.

- `files`
  - Upload, read, store, preview, download artifacts.

- `email_draft`
  - Draft internal/external email.
  - No send permission.

- `email_send_internal`
  - Send to approved internal domains.

- `email_send_external`
  - Send to external recipients.
  - Approval required by default.

- `calendar`
  - Read schedule, create meeting drafts.

- `workflow_runner`
  - Run approved workflow templates.

- `workflow_admin`
  - Create/edit workflow templates.

- `agent_admin`
  - Create or manage autonomous agents.

- `code_automation`
  - Use opencode-powered tools.
  - Approval required for edit/write/bash actions.

### 7.2 Extend Manifest Levels

Current field classification levels:

- `PUBLIC`
- `INTERNAL`
- `PII`
- `SENSITIVE`
- `PCI`

Recommended additions:

- `CONFIDENTIAL`
  - Internal strategy, contracts, HR, legal, management-only material.

- `CREDENTIAL`
  - API keys, tokens, passwords, connection strings.

- `EXPORT_CONTROLLED`
  - Broad exports or generated files containing multiple customers/vendors.

Do not add too many levels too early. If implementation needs to stay small, begin with existing levels and represent broad exports as tool-level risk flags.

### 7.3 Tool Risk Classification

Add risk metadata to manifest policies:

```python
risk="read_low|read_sensitive|export|send|write|code_exec"
approval_required=False
max_rows_without_approval=100
```

Risk categories:

- `read_low`
  - Single-entity lookup.

- `read_sensitive`
  - PII or financial data.

- `export`
  - File generation or multi-row dataset export.

- `send`
  - Email/calendar/action leaving the system.

- `write`
  - Creates or modifies data in a connected system.

- `code_exec`
  - opencode or shell-driven automation.

### 7.4 Output-Aware Governance

Generated files need governance too.

Rules:

- Artifact classification should be computed from source data classifications.
- If a PPTX includes SENSITIVE data, the PPTX artifact is SENSITIVE.
- If an XLSX includes PII, the XLSX artifact is PII.
- Downloads should be audited.
- External sends should require approval when artifact classification includes PII/SENSITIVE/CONFIDENTIAL.
- Artifact retention should depend on classification.

### 7.5 Data Minimization

Workflow templates should request the smallest data needed.

Examples:

- Customer account review should not pull GL transactions by default.
- Shipment exception report should not include contact phone/email unless needed.
- Executive brief should aggregate by customer without exposing line-level PII.

### 7.6 Human-in-the-Loop Policy

Use a consequence-based approval model:

No approval:

- Single customer lookup.
- Internal summary.
- Generate private draft.
- Generate file for self.

Approval may be required:

- Broad export.
- Sensitive finance export.
- Scheduled recurring workflow.
- Internal send to large distribution list.

Approval required:

- External email send.
- Agent-initiated external communication.
- Any writeback to ERP/CRM.
- opencode action with write/bash/edit permissions.
- Permission escalation requested by a user/agent.

---

## 8. New MCP Backends

## 8.1 `mcp-office`

### Purpose

Generate and manipulate Microsoft Office-compatible artifacts:

- XLSX.
- PPTX.
- DOCX.
- PDF.
- CSV.
- Fillable forms later.

### Technology Options

Initial implementation:

- `openpyxl` or `xlsxwriter` for XLSX.
- `python-pptx` for PPTX.
- `python-docx` for DOCX.
- `reportlab` or LibreOffice/ONLYOFFICE conversion for PDF.

Advanced implementation:

- ONLYOFFICE Document Builder for DOCX/XLSX/PPTX/PDF creation.
- ONLYOFFICE Docs embedded editor for preview/edit/collaboration.
- ONLYOFFICE DocSpace for document rooms and governed collaboration.
- ONLYOFFICE plugins/macros for in-editor AI actions.

### Initial Tools

#### `create_excel_report`

Signature:

```python
create_excel_report(
    title: str,
    tables: list,
    charts: list = [],
    filename: str = "",
    template_id: str = ""
) -> str
```

Responsibilities:

- Create XLSX file.
- Write one or more sheets.
- Apply formatting.
- Add filters.
- Freeze header rows.
- Add charts where requested.
- Store artifact metadata.
- Return artifact id, filename, download route, and classification.

#### `create_powerpoint_deck`

Signature:

```python
create_powerpoint_deck(
    title: str,
    sections: list,
    source_tables: list = [],
    template_id: str = "",
    filename: str = ""
) -> str
```

Responsibilities:

- Generate PPTX.
- Use approved brand template.
- Create title slide, summary slide, table slides, chart slides, recommendation slides.
- Store artifact metadata.

#### `create_word_report`

Signature:

```python
create_word_report(
    title: str,
    sections: list,
    tables: list = [],
    template_id: str = "",
    filename: str = ""
) -> str
```

Responsibilities:

- Generate DOCX.
- Apply headings.
- Insert tables.
- Insert executive summary.
- Store artifact metadata.

#### `create_pdf_packet`

Signature:

```python
create_pdf_packet(
    title: str,
    sections: list,
    source_artifact_ids: list = [],
    filename: str = ""
) -> str
```

Responsibilities:

- Generate PDF.
- Optionally combine generated DOCX/PPTX exports later.
- Store artifact metadata.

#### `convert_artifact`

Signature:

```python
convert_artifact(
    artifact_id: str,
    target_format: str
) -> str
```

Responsibilities:

- Convert DOCX/PPTX/XLSX to PDF.
- Convert CSV to XLSX.
- Convert supported docs to PDF.

#### `extract_tables_from_document`

Signature:

```python
extract_tables_from_document(
    artifact_id: str,
    pages: str = ""
) -> str
```

Responsibilities:

- Extract tables from PDFs/documents.
- Return structured JSON.
- Preserve source artifact references.

### Manifest Policies

Add canonical tools:

- `create_excel_report`
- `create_powerpoint_deck`
- `create_word_report`
- `create_pdf_packet`
- `convert_artifact`
- `extract_tables_from_document`

Suggested backend:

- `office`

Suggested fields:

- `artifactId`: `INTERNAL`
- `filename`: `INTERNAL`
- `downloadUrl`: `INTERNAL`
- `classification`: `INTERNAL`
- `sourceRowCount`: `SENSITIVE` if revealing volume is sensitive, otherwise `INTERNAL`

### Test Cases

- User without `office` category cannot generate artifacts.
- Artifact inherits PII classification from source data.
- Artifact inherits SENSITIVE classification from source data.
- Downloads are audited.
- Large export triggers approval.
- Generated XLSX opens successfully.
- Generated PPTX opens successfully.
- Generated DOCX opens successfully.

---

## 8.2 `mcp-files`

### Purpose

Manage uploaded and generated artifacts.

### Initial Tools

- `list_artifacts`
- `get_artifact_metadata`
- `delete_artifact`
- `create_template`
- `list_templates`
- `get_template`
- `upload_source_file`
- `download_artifact`

### Governance Requirements

- Users can see their own artifacts.
- Admins can see all artifacts.
- Shared artifacts require explicit permissions.
- Delete requires owner/admin.
- Download logs an audit event.
- Preview logs an audit event for sensitive artifacts.

### Storage Options

Development:

```text
governance_core/data/artifacts/
```

Production:

- Azure Blob Storage.
- Local network storage.
- ONLYOFFICE DocSpace storage.
- S3-compatible object storage.

Recommended artifact directory shape:

```text
artifacts/
  2026/
    07/
      art_abc123/
        metadata.json
        output.xlsx
        output.pdf
```

---

## 8.3 `mcp-email`

### Purpose

Draft and optionally send emails.

### Initial Tools

- `create_email_draft`
- `preview_email_draft`
- `send_email_draft`
- `list_email_drafts`

### Draft Signature

```python
create_email_draft(
    to: list,
    cc: list = [],
    subject: str = "",
    body_markdown: str = "",
    attachment_artifact_ids: list = []
) -> str
```

### Governance Requirements

- Drafting is lower risk.
- Internal send can be allowed by category.
- External send requires approval by default.
- Attachments inherit artifact classification.
- Block sending CREDENTIAL/PCI.
- Audit recipients, subject, attachment IDs, but avoid storing full body if it contains sensitive data.

### Implementation Notes

Start with draft-only mode. Do not send real email until approvals and audit are stable.

Potential future integrations:

- Outlook.
- Gmail.
- SMTP relay.
- Microsoft Graph.

---

## 8.4 `mcp-calendar`

### Purpose

Meeting preparation and scheduling.

### Initial Tools

- `create_meeting_brief`
- `draft_calendar_invite`
- `list_upcoming_meetings`

### Example Workflow

> "Prepare me for my 2 PM meeting with customer ABC."

Steps:

1. Read meeting metadata.
2. Resolve customer.
3. Retrieve recent orders/shipments.
4. Generate meeting brief.
5. Attach prior account review if available.

### Governance Requirements

- Calendar read permission separate from business-data permission.
- External invite creation requires approval.
- Meeting briefs with PII/SENSITIVE inherit classification.

---

## 8.5 `mcp-code`

### Purpose

Use opencode for internal developer/admin automation.

### Good Use Cases

- Generate new workflow templates.
- Update report/deck templates.
- Add tests.
- Refactor MCP tool code.
- Produce diffs for review.
- Explain codebase sections.

### Initial Tools

- `opencode_plan_change`
- `opencode_review_repo`
- `opencode_generate_template`

Avoid enabling direct edit/write/bash through normal users.

### Governance Requirements

- Admin/developer category only.
- Plan/review tools can be read-only.
- Edit/write/bash actions require approval.
- Tool outputs should include diff summary and changed files.
- Never allow arbitrary shell execution for non-admin workflows.

---

## 9. Workflow Templates

## 9.1 Workflow 1: Customer 360 Report

### User Prompt

> "Create a customer 360 report for ABC Dental for the last 12 months."

### Required Categories

- `accounts`
- `orders`
- `shipments`
- `office`
- `files`

Optional:

- `finance`

### Steps

1. Resolve customer.
   - Tool: `find_customer`
   - Input: customer query.
   - Output: selected `customerId`.

2. Fetch overview.
   - Tool: `get_customer_overview`
   - Input: `customerId`.

3. Fetch order summary.
   - Tool: `get_customer_order_summary`
   - Input: `customerId`, date range.

4. Fetch recent orders.
   - Tool: `get_customer_orders`
   - Input: `customerId`, date range.

5. Fetch shipment status.
   - Tool: `get_customer_shipment_status`
   - Input: `customerId`.

6. Generate spreadsheet.
   - Tool: `create_excel_report`
   - Sheets:
     - Summary.
     - Orders.
     - Shipment status.
     - Notes.

7. Generate presentation.
   - Tool: `create_powerpoint_deck`
   - Slides:
     - Title.
     - Executive summary.
     - Customer profile.
     - Order trend.
     - Shipment status.
     - Risks/opportunities.
     - Recommended next steps.

8. Return artifact cards in UI.

### Outputs

- XLSX.
- PPTX.
- Chat summary.

### Approval Rules

- No approval if generated for requester only.
- Approval required for external email send.
- Approval required if workflow includes finance fields and user lacks finance category.

### MVP Acceptance Criteria

- User can run workflow from UI.
- Assistant asks for disambiguation if multiple customers found.
- Generated XLSX opens.
- Generated PPTX opens.
- Artifacts are visible in "Files".
- Tool calls appear in audit.
- Artifact classification includes inherited PII/SENSITIVE.

---

## 9.2 Workflow 2: Shipment Exception Report

### User Prompt

> "Create a shipment exception report for this week."

### Required Categories

- `orders`
- `shipments`
- `office`
- `files`

### Steps

1. Query recent orders/shipments.
2. Identify delayed/problem shipments.
3. Group by customer.
4. Generate Excel report.
5. Generate management summary.

### Outputs

- XLSX.
- Optional PDF.
- Chat summary.

### Suggested XLSX Sheets

- Exceptions by customer.
- Orders impacted.
- Shipment details.
- Summary metrics.

### MVP Acceptance Criteria

- Produces a report with grouped shipment exceptions.
- Does not expose contact PII unless accounts category is also granted.
- Uses redacted values where necessary.

---

## 9.3 Workflow 3: Vendor AP Summary

### User Prompt

> "Create an AP summary for vendor VEND001 for the last 90 days."

### Required Categories

- `finance`
- `office`
- `files`

### Steps

1. Fetch vendor details.
2. Fetch AP invoices.
3. Aggregate totals.
4. Generate XLSX.
5. Generate DOCX/PDF summary.

### Outputs

- XLSX.
- DOCX or PDF.
- Chat summary.

### Approval Rules

- Approval required if sending externally.
- Approval required for broad export over threshold.

---

## 9.4 Workflow 4: Customer Email Draft

### User Prompt

> "Draft an email to customer ABC about order 12345 shipping status."

### Required Categories

- `orders`
- `shipments`
- `email_draft`

Optional:

- `accounts` for customer contact info.
- `email_send_external` for sending.

### Steps

1. Resolve order.
2. Retrieve shipment status.
3. Draft customer-safe email.
4. Preview draft.
5. If user requests send, create approval or send if policy allows.

### Outputs

- Email draft.

### Safety Rules

- Do not expose internal finance details.
- Do not include redacted fields.
- External send requires approval initially.

---

## 9.5 Workflow 5: Weekly Executive Brief

### User Prompt

> "Generate this week's executive brief."

### Required Categories

- `orders`
- `shipments`
- `office`
- `files`

Optional:

- `finance`
- `accounts`

### Steps

1. Pull top customers by spend.
2. Pull product/order trends.
3. Pull shipment exceptions.
4. Pull finance highlights if permitted.
5. Generate PPTX.
6. Generate PDF copy.

### Outputs

- PPTX.
- PDF.

### Approval Rules

- If scheduled or agent-generated, notify owner after completion.
- Broad sensitive data may require manager approval.

---

## 10. User Experience Plan

### 10.1 Navigation

Replace the current simple chat-first page with a workbench.

Recommended top-level sections:

- Assistant
- Workflows
- Files
- Templates
- My Access
- Admin

### 10.2 Assistant Page

Purpose:

- Natural language entry point.
- Artifact-generating chat.
- Tool-call transparency.

Features:

- Chat thread.
- Suggested workflow shortcuts.
- Artifact cards.
- Tool-call disclosure.
- "Continue as workflow" button.
- "Export answer" button.

Artifact card fields:

- File icon.
- Title.
- Type.
- Classification badge.
- Created time.
- Download.
- Preview.
- Send for approval.

### 10.3 Workflows Page

Purpose:

- Browse and run approved workflow templates.

Features:

- Workflow catalog.
- Category/risk badges.
- Required access display.
- Run form.
- Recent runs.
- Scheduled runs later.

Workflow card fields:

- Name.
- Description.
- Output types.
- Required categories.
- Approval policy.
- Last run.

### 10.4 Workflow Run Page

Purpose:

- Show step-by-step execution.

Features:

- Input summary.
- Step timeline.
- Current status.
- Tool calls used.
- Generated artifacts.
- Approval prompts.
- Error/retry controls.

Step statuses:

- Pending.
- Running.
- Completed.
- Failed.
- Skipped.
- Waiting for approval.

### 10.5 Files Page

Purpose:

- Manage generated and uploaded artifacts.

Features:

- Search.
- Filter by type.
- Filter by classification.
- Filter by workflow.
- Download.
- Preview.
- Share/request approval.
- Delete if allowed.

### 10.6 Templates Page

Purpose:

- Manage approved report/deck/document templates.

User features:

- View available templates.
- Preview template.
- Use template in workflow.

Admin features:

- Upload template.
- Version template.
- Disable template.
- Set required categories.

### 10.7 Admin Page

Current admin page should expand to include:

- Consumers.
- Categories.
- Access requests.
- Audit.
- Alerts.
- Backend health.
- Workflow runs.
- Agent identities.
- Artifact retention.
- Approval queue.
- Global controls.

Admin controls:

- Pause all agents.
- Pause specific backend.
- Pause external sends.
- Pause artifact downloads.
- Disable workflow template.
- Rotate agent key.

---

## 11. LLM and Agent Design

### 11.1 Model Strategy

Use local hosted LLM for:

- Internal chat.
- Tool planning.
- Summarization.
- Draft generation.
- Report narrative generation.
- Data explanation.

Use deterministic code for:

- Authorization.
- Redaction.
- Scope binding.
- Artifact classification.
- Workflow state transitions.
- Approval decisions.
- File storage paths.

### 11.2 Prompt Strategy

Create role-specific system prompts:

- `general_assistant`
- `customer_service_assistant`
- `sales_account_assistant`
- `finance_assistant`
- `operations_assistant`
- `admin_assistant`
- `workflow_planner`
- `artifact_writer`

Each prompt should specify:

- Allowed business domain.
- How to choose tools.
- What not to invent.
- How to handle redacted fields.
- When to ask for clarification.
- How to produce artifact-ready structured sections.

### 11.3 Planning vs Execution

Separate planning from execution:

- LLM may propose a plan.
- Workflow engine validates the plan.
- Workflow engine executes allowed steps.
- PDP enforces every tool call.

Do not let the LLM directly decide approval requirements.

### 11.4 Structured Outputs

For artifact generation, require the LLM to produce structured content:

```json
{
  "title": "Customer ABC Account Review",
  "summary": ["...", "..."],
  "sections": [
    {
      "heading": "Order Trends",
      "bullets": ["...", "..."],
      "tables": ["orders_summary"]
    }
  ],
  "charts": [
    {
      "type": "bar",
      "title": "Monthly Orders",
      "sourceTable": "monthly_orders"
    }
  ]
}
```

This makes Office generation more reliable than asking the LLM to write binary files.

### 11.5 Conversation Memory

Current chat history can support:

- Resume previous work.
- Reference prior generated artifacts.
- Continue a workflow.

Add memory boundaries:

- Never use previous sensitive data in a new context unless the same user still has access.
- Re-check authorization when resuming old workflows.
- Recompute artifact access at download time.

---

## 12. Data and Storage Design

### 12.1 Store Collections

Current store includes consumers, categories, access requests, config, audit, chat sessions.

Add:

- `workflowTemplates`
- `workflowRuns`
- `artifacts`
- `approvals`
- `templateVersions`
- `scheduledJobs`
- `agentProfiles`

### 12.2 `workflowTemplates`

Fields:

- `id`
- `displayName`
- `description`
- `version`
- `status`
- `owner`
- `requiredCategories`
- `optionalCategories`
- `risk`
- `approvalPolicy`
- `inputSchema`
- `steps`
- `outputTypes`
- `createdAt`
- `updatedAt`
- `createdBy`

### 12.3 `workflowRuns`

Fields:

- `id`
- `templateId`
- `templateVersion`
- `requestedBy`
- `actorConsumerId`
- `status`
- `inputs`
- `steps`
- `artifacts`
- `approvalIds`
- `auditRefs`
- `error`
- `createdAt`
- `updatedAt`
- `completedAt`

### 12.4 `artifacts`

Fields:

- `id`
- `ownerConsumerId`
- `workflowRunId`
- `type`
- `title`
- `filename`
- `storagePath`
- `mimeType`
- `classification`
- `sourceToolCalls`
- `sourceArtifactIds`
- `checksum`
- `sizeBytes`
- `retentionDays`
- `status`
- `createdAt`
- `expiresAt`

### 12.5 `approvals`

Fields:

- `id`
- `workflowRunId`
- `requestedBy`
- `approverConsumerId`
- `status`
- `riskLevel`
- `reason`
- `preview`
- `artifactIds`
- `createdAt`
- `expiresAt`
- `decidedAt`
- `decisionNote`

### 12.6 `agentProfiles`

Fields:

- `consumerId`
- `displayName`
- `ownerConsumerId`
- `purpose`
- `allowedWorkflowTemplates`
- `schedulePolicy`
- `approvalPolicy`
- `status`
- `lastRunAt`
- `createdAt`

---

## 13. Security and Compliance Requirements

### 13.1 Deny by Default

Every new tool, workflow, artifact action, and send action should be denied until explicitly granted.

### 13.2 Per-Action Audit

Audit the following:

- Workflow started.
- Workflow step completed.
- Tool call allowed/denied.
- Artifact created.
- Artifact previewed.
- Artifact downloaded.
- Artifact deleted.
- Email draft created.
- Email sent.
- Approval requested.
- Approval approved/denied.
- Agent run started/completed.
- Admin policy change.

### 13.3 Sensitive Output Handling

Rules:

- Do not include redacted values in generated files.
- Mark artifacts by inherited classification.
- Prevent external sends of PCI/CREDENTIAL.
- Require approval for external sends with PII/SENSITIVE.
- Consider watermarking sensitive PDFs/decks.

### 13.4 Agent Identity

Every autonomous workflow should run as an agent consumer:

- Own API key.
- Own categories.
- Owner human.
- Allowed templates.
- Rate limit.
- Audit identity.
- Revocation controls.

### 13.5 Revocation

Admin must be able to:

- Disable user.
- Disable agent.
- Rotate key.
- Pause backend.
- Pause all agents.
- Pause external sends.
- Disable workflow template.
- Expire artifact.

### 13.6 Row and Scope Safety

Continue current model:

- Customer IDs should be resolved through trusted tools.
- Account-scoped tools should bind/verify scope server-side.
- Do not let the LLM invent identifiers.
- Do not let workflow input bypass scope checks.

### 13.7 Prompt Injection Defense

For document upload and email/calendar integrations:

- Treat uploaded document text as untrusted.
- Never allow document content to override system instructions.
- Do not execute instructions found inside documents.
- Strip or quote user-provided content before passing to planner prompts.
- Log source document IDs used in outputs.

### 13.8 Exfiltration Detection

Extend existing analytics:

- Distinct customer IDs per user/agent.
- Broad exports per user/agent.
- Artifact downloads per user/agent.
- External send volume.
- Denial bursts.
- Repeated failed lookups.
- Large row counts.
- Agent activity outside schedule.

---

## 14. Implementation Roadmap

## Phase 0: Stabilize the Existing Foundation

### Objectives

- Confirm current gateway and MiniERP backend are stable.
- Ensure tests run locally.
- Document current architecture and runbook.

### Tasks

- Run smoke tests.
- Verify local LLM configuration path.
- Verify chat tool calls.
- Verify admin dashboard.
- Verify categories/access requests.
- Verify audit logging.
- Verify backend health checks.
- Document env vars.

### Deliverables

- Updated runbook.
- Known issues list.
- Baseline test result.

### Exit Criteria

- Existing chat works.
- Existing governed MiniERP tools work.
- Admin can grant/revoke access.
- Audit shows tool calls and denials.

---

## Phase 1: Artifact Foundation

### Objectives

- Add durable generated files.
- Add first office-generation backend.
- Make generated files visible in UI.

### Tasks

1. Create artifact model.
2. Add artifact store interface.
3. Implement local filesystem artifact storage.
4. Add artifact metadata persistence.
5. Add artifact routes:
   - `GET /artifacts`
   - `GET /artifacts/{id}`
   - `GET /artifacts/{id}/download`
   - `GET /artifacts/{id}/preview`
6. Add `mcp-office`.
7. Implement `create_excel_report`.
8. Implement `create_powerpoint_deck`.
9. Implement `create_word_report`.
10. Add manifest entries for office tools.
11. Add `office` and `files` categories.
12. Add UI artifact cards.
13. Add Files page.

### First File Types

- XLSX.
- PPTX.
- DOCX.

### Deliverables

- `mcp-office` backend.
- Artifact metadata model.
- Artifact storage.
- Files UI.
- Generated file download.

### Exit Criteria

- User can generate an XLSX from a simple data table.
- User can generate a PPTX from structured sections.
- User can download artifacts.
- Downloads are audited.
- User without `office` category is denied.

---

## Phase 2: Workflow Engine MVP

### Objectives

- Add repeatable workflow templates and workflow runs.
- Execute workflows step by step.
- Generate artifacts from MiniERP data.

### Tasks

1. Add workflow models.
2. Add workflow template registry.
3. Add workflow run store.
4. Implement workflow executor.
5. Implement step types:
   - `tool_call`
   - `llm_transform`
   - `artifact`
   - `approval_pause`
6. Add Workflows page.
7. Add workflow run detail page.
8. Add first three templates:
   - Customer 360 Report.
   - Shipment Exception Report.
   - Vendor AP Summary.
9. Add audit events for workflow lifecycle.
10. Add tests for workflow execution.

### Deliverables

- Workflow engine.
- Workflow catalog UI.
- Workflow run UI.
- Three real workflows.

### Exit Criteria

- User can run Customer 360 Report from UI.
- Workflow calls governed MiniERP tools.
- Workflow generates XLSX/PPTX.
- Step timeline shows progress.
- Failed step is visible.
- Audit connects workflow run to tool calls and artifacts.

---

## Phase 3: Approvals and Delivery

### Objectives

- Add human approval gates.
- Add email draft and send flow.
- Prevent risky actions from happening silently.

### Tasks

1. Add approval model/store.
2. Add approval queue UI.
3. Add approval API:
   - request.
   - approve.
   - deny.
   - expire.
4. Create `mcp-email` in draft-only mode.
5. Implement `create_email_draft`.
6. Implement `preview_email_draft`.
7. Add optional `send_email_draft` behind approval.
8. Add policy for internal vs external recipients.
9. Add attachment classification checks.
10. Add admin global control: pause external sends.

### Deliverables

- Approval queue.
- Email draft workflow.
- Approval-gated external send design.

### Exit Criteria

- Assistant can create an email draft.
- External sends require approval.
- Approval decision is audited.
- Sensitive artifact attachment triggers approval or block.

---

## Phase 4: ONLYOFFICE Integration

### Objectives

- Add rich document preview/edit/collaboration.
- Use ONLYOFFICE for polished office document generation and editing.

### Tasks

1. Decide integration mode:
   - ONLYOFFICE Docs embedded editor.
   - ONLYOFFICE Document Builder.
   - ONLYOFFICE DocSpace.
2. Add document preview route.
3. Add editor launch route.
4. Add template editing.
5. Add PDF conversion.
6. Add comments/review workflow.
7. Add form filling if useful.
8. Add plugin/macro experiments for in-editor AI actions.

### Deliverables

- Preview/edit generated office files in browser.
- Template versioning.
- PDF export path.

### Exit Criteria

- User can preview generated DOCX/XLSX/PPTX.
- User can edit or comment through ONLYOFFICE.
- Edited versions remain tied to artifact history.

---

## Phase 5: Autonomous Agents and Scheduling

### Objectives

- Support scheduled recurring workflows.
- Support agent-owned workflow execution.

### Tasks

1. Add scheduled job model.
2. Add agent profile model.
3. Add agent creation UI.
4. Add allowed workflow template list per agent.
5. Add schedule policy.
6. Add run notifications.
7. Add anomaly detection for agent behavior.
8. Add admin pause controls for scheduled jobs.

### Deliverables

- Weekly/monthly reports.
- Agent identity management.
- Agent run audit.

### Exit Criteria

- Admin can create an agent.
- Agent can run an approved workflow on schedule.
- Agent cannot call tools outside its categories/templates.
- Admin can pause/disable agent.

---

## Phase 6: Knowledge, Search, and Uploaded Documents

### Objectives

- Let users work with internal documents safely.
- Add RAG/search/document Q&A.

### Tasks

1. Add upload flow.
2. Add document parsing.
3. Add document chunking and indexing.
4. Add `mcp-knowledge`.
5. Add document Q&A.
6. Add citation output.
7. Add prompt-injection filters.
8. Add source artifact audit.

### Deliverables

- Upload PDF/DOCX.
- Ask questions over uploaded docs.
- Generate reports from uploaded docs plus MiniERP data.

### Exit Criteria

- User can upload a document.
- Assistant can summarize it with citations.
- Document content cannot override tool/policy instructions.

---

## Phase 7: opencode Developer/Admin Automation

### Objectives

- Use opencode to help maintain workflows, templates, tests, and internal code safely.

### Tasks

1. Add `mcp-code` read-only planning tools.
2. Add opencode config with strict permissions.
3. Add admin-only workflow template generation.
4. Add code review/diff generation.
5. Add approval for edit/write/bash.
6. Add audit for every opencode run.

### Deliverables

- Admin/developer AI automation lane.
- Safer workflow/template maintenance.

### Exit Criteria

- Admin can ask for a workflow template plan.
- opencode output is captured and audited.
- Edits require explicit approval.

---

## 15. Recommended MVP Scope

The MVP should be narrow but impressive.

### MVP Name

**Office Assistant MVP: Customer Report Builder**

### MVP Capabilities

1. User logs in.
2. User opens Assistant or Workflows.
3. User runs "Customer 360 Report".
4. Assistant resolves customer.
5. Assistant fetches governed MiniERP data.
6. Assistant generates:
   - XLSX account report.
   - PPTX account review deck.
7. User sees artifact cards.
8. User downloads files.
9. Admin sees audit trail.

### MVP Technical Tasks

- Add artifact model.
- Add local artifact store.
- Add `mcp-office`.
- Add Excel builder.
- Add PowerPoint builder.
- Add office manifest/category.
- Add workflow model.
- Add Customer 360 workflow.
- Add Files page.
- Add Workflows page.
- Add audit events.
- Add tests.

### MVP Non-Goals

- Real email sending.
- Calendar integration.
- Autonomous scheduled agents.
- Full ONLYOFFICE editor embedding.
- Broad RAG/document upload.
- ERP writeback.

---

## 16. Suggested Repository Structure

```text
governence_agent/
  gateway/
    app.py
    orchestrator.py
    workflows.py
    workflow_routes.py
    artifact_routes.py
    static/
      app.html
      chat.html
      workflows.html
      workflow-run.html
      files.html
      templates.html
      admin.html

  governance_core/
    workflow_models.py
    artifact_models.py
    approval_models.py
    artifact_store.py
    workflow_store.py
    policy/
      manifest.py
      decision.py
      redaction.py

  mcp-minierp/
    app.py
    sqlagent/

  mcp-office/
    app.py
    requirements.txt
    builders/
      excel.py
      powerpoint.py
      word.py
      pdf.py
    templates/
      customer_account_review/
        template.json
        deck_theme.json
      shipment_exception_report/
        template.json
      vendor_ap_summary/
        template.json

  mcp-files/
    app.py
    requirements.txt
    store.py

  mcp-email/
    app.py
    requirements.txt
    drafts.py

  _smoke/
    test_artifacts.py
    test_office_tools.py
    test_workflows.py
    test_approvals.py
```

---

## 17. API Design Sketch

### 17.1 Workflow Routes

```http
GET  /workflows
GET  /workflows/{template_id}
POST /workflows/{template_id}/run
GET  /workflow-runs
GET  /workflow-runs/{run_id}
POST /workflow-runs/{run_id}/cancel
POST /workflow-runs/{run_id}/resume
```

### 17.2 Artifact Routes

```http
GET    /artifacts
GET    /artifacts/{artifact_id}
GET    /artifacts/{artifact_id}/download
GET    /artifacts/{artifact_id}/preview
DELETE /artifacts/{artifact_id}
```

### 17.3 Approval Routes

```http
GET  /approvals
GET  /approvals/{approval_id}
POST /approvals/{approval_id}/approve
POST /approvals/{approval_id}/deny
```

### 17.4 Admin Routes

```http
GET  /admin/workflows
POST /admin/workflows
GET  /admin/workflow-runs
GET  /admin/artifacts
GET  /admin/agents
POST /admin/agents
POST /admin/controls/pause-external-sends
```

---

## 18. Testing Strategy

### 18.1 Unit Tests

Test:

- Artifact metadata classification inheritance.
- Workflow step state transitions.
- Approval policy decisions.
- Manifest category checks.
- Office builder output metadata.
- Prompt/planner JSON validation.

### 18.2 Integration Tests

Test:

- Customer 360 workflow happy path.
- Workflow denial when user lacks category.
- Artifact download audit.
- Large export approval trigger.
- External email approval trigger.
- Backend pause prevents workflow execution.

### 18.3 File Validation Tests

Test generated files:

- XLSX opens and contains expected sheets.
- PPTX opens and contains expected slide count.
- DOCX opens and contains expected headings.
- PDF exists and has nonzero pages.

Use libraries where possible:

- `openpyxl` for XLSX validation.
- `python-pptx` for PPTX validation.
- `python-docx` for DOCX validation.
- `pypdf` or rendering for PDF validation.

### 18.4 Security Tests

Test:

- Unauthorized user cannot run office tools.
- Unauthorized user cannot download another user's artifact.
- Redacted values are not present in generated artifacts.
- External send cannot attach blocked classifications.
- Disabled workflow cannot run.
- Disabled agent cannot run.
- Prompt injection in uploaded doc is ignored.

---

## 19. Operational Requirements

### 19.1 Observability

Dashboards should show:

- Workflow runs by status.
- Tool calls by backend.
- Artifact generation count.
- Download count.
- Approval queue age.
- Agent runs.
- Denials.
- Error rates.
- Slow tools.

### 19.2 Retention

Suggested defaults:

- PUBLIC/INTERNAL artifacts: 180 days.
- PII artifacts: 90 days.
- SENSITIVE artifacts: 60 days.
- CONFIDENTIAL artifacts: 30 days.
- Failed intermediate files: 7 days.

Make retention configurable by category.

### 19.3 Backup and Recovery

Must preserve:

- Workflow templates.
- Workflow run records.
- Artifact metadata.
- Approval records.
- Audit records.

Generated binary artifacts may have separate retention and backup rules.

### 19.4 Deployment

Current deployment runs gateway + MiniERP. Expansion will need additional processes:

- gateway.
- mcp-minierp.
- mcp-office.
- mcp-files if separate.
- mcp-email later.

For Azure App Service, consider:

- Multiple processes in startup script for MVP.
- Container App or separate App Services as system grows.
- Central env var management.
- Health checks per backend.

---

## 20. Risks and Mitigations

### Risk: The Assistant Becomes Too Broad

Mitigation:

- Start with three workflows.
- Add role-specific packs.
- Keep workflow templates explicit.

### Risk: Generated Files Leak Redacted Data

Mitigation:

- Use redacted governed tool outputs only.
- Inherit classification from source data.
- Scan generated artifact text for blocked fields in tests.
- Audit downloads.

### Risk: LLM Makes Poor Workflow Plans

Mitigation:

- Workflow engine validates plans.
- Prefer fixed workflow templates.
- Require structured LLM outputs.
- Ask clarification when identifiers are ambiguous.

### Risk: External Send Causes Business Harm

Mitigation:

- Draft-only first.
- Approval required for external send.
- Recipient domain policy.
- Attachment classification checks.

### Risk: Autonomous Agents Overreach

Mitigation:

- Each agent has identity and categories.
- Agents can only run approved templates.
- Admin pause controls.
- Schedule windows.
- Anomaly detection.

### Risk: Office Generation Quality Is Low

Mitigation:

- Use templates.
- Validate generated files.
- Add preview/edit via ONLYOFFICE.
- Keep first templates simple and polished.

### Risk: opencode Executes Unsafe Changes

Mitigation:

- Read-only planning first.
- Admin-only category.
- Approval for edits/bash.
- Diff review before applying.

---

## 21. Product Milestones

### Milestone 1: Artifact Engine

User can generate and download XLSX/PPTX/DOCX from structured data.

### Milestone 2: Customer Report Workflow

User can run a governed Customer 360 workflow and receive XLSX + PPTX outputs.

### Milestone 3: Workflow Workbench

User can browse workflows, run them, view step progress, and manage artifacts.

### Milestone 4: Approval-Gated Delivery

User can draft email with generated artifacts and route external send for approval.

### Milestone 5: ONLYOFFICE Preview/Edit

User can preview and edit generated artifacts in browser.

### Milestone 6: Scheduled Agents

Admin can create an agent that runs approved workflows on schedule.

### Milestone 7: Knowledge + Uploaded Docs

User can upload documents, ask questions, and generate combined reports.

---

## 22. Suggested First Sprint

### Sprint Goal

Create the first end-to-end artifact-generating workflow skeleton.

### Tasks

1. Add artifact model and local artifact store.
2. Add `mcp-office` folder.
3. Implement `create_excel_report`.
4. Implement `create_powerpoint_deck`.
5. Register office backend in gateway backend config.
6. Add manifest policies for office tools.
7. Add seeded `office` category.
8. Add basic Files page.
9. Add simple workflow model.
10. Implement Customer 360 workflow with hardcoded steps.
11. Add artifact cards to chat/workflow response.
12. Add smoke tests.

### Demo Script

1. Log in as a user with `accounts`, `orders`, `shipments`, `office`, `files`.
2. Run "Customer 360 Report".
3. Enter customer query.
4. Confirm selected customer if needed.
5. Watch workflow steps complete.
6. Download XLSX.
7. Download PPTX.
8. Open admin audit and show tool/artifact events.

---

## 23. Long-Term Direction

The long-term system should feel like an internal operating system for office work:

- Users ask for outcomes, not data pulls.
- Workflows are reusable and governed.
- Every output is traceable.
- Every agent has an identity.
- Every sensitive action has policy.
- Managers get finished briefs, not raw data.
- Coworkers can automate repetitive work without bypassing security.

The key architectural rule is simple:

> Expand the tool surface aggressively, but keep authorization, redaction, audit, approval, and artifact governance centralized.
