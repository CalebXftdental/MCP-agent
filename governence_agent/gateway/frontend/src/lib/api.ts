/**
 * The single entry point for every call to the governance gateway.
 *
 * Paths here are written WITHOUT the API prefix — `/dashboard/me`, not
 * `/backend/dashboard/me`. VITE_API_BASE_URL supplies the prefix, so moving or
 * renaming it is an .env change rather than a sweep through call sites.
 *
 * Auth is the gov_session cookie: HttpOnly, SameSite=Strict, Secure
 * (gateway/app.py:910). Nothing here attaches it — the browser does, and only
 * because these requests are same-origin. That is why the dev server proxies
 * rather than calling port 8020 directly.
 */

const BASE = (import.meta.env.VITE_API_BASE_URL ?? '').replace(/\/+$/, '')

/** Prefixes a path the same way `api.get`/`api.post` do, for the one caller
 *  that needs a raw `fetch` instead of the JSON wrapper — SSE streaming can't
 *  go through `request()`, which always awaits and parses a full body. */
export function apiUrl(path: string): string {
  return `${BASE}${path}`
}

/** The gateway's error shape: every failure returns `{"error": "..."}`. */
interface GatewayError {
  error?: string
}

export class ApiError extends Error {
  readonly status: number
  readonly body: unknown

  constructor(status: number, body: unknown) {
    const detail = (body as GatewayError | null)?.error
    super(typeof detail === 'string' ? detail : `Gateway request failed (${status})`)
    this.name = 'ApiError'
    this.status = status
    this.body = body
  }

  /** No session, or it expired — the caller should show the login screen. */
  get isUnauthenticated(): boolean {
    return this.status === 401
  }

  /** Signed in, but this principal lacks the role or grant. */
  get isForbidden(): boolean {
    return this.status === 403
  }

  /** The gateway was unreachable, so the request never got a status. */
  get isOffline(): boolean {
    return this.status === 0
  }

  /** Layer 1 or the login lockout rejected the caller for rate. */
  get isRateLimited(): boolean {
    return this.status === 429
  }
}

// `object`, not `Record<string, unknown>`: a declared interface without an index
// signature (AdminControls, CreateConsumerInput, ...) isn't structurally
// assignable to Record<string, unknown> even when every property matches, so
// api.post/put callers passing a named request type would fail to type-check.
// Only ever reaches JSON.stringify() below, so nothing here needs keyed access.
type JsonBody = object

async function readBody(res: Response): Promise<unknown> {
  if (res.status === 204) return null
  const contentType = res.headers.get('content-type') ?? ''
  if (!contentType.includes('application/json')) return res.text()
  try {
    return await res.json()
  } catch {
    // A JSON content-type with an unparseable body: treat as empty rather than
    // masking the real HTTP status with a parse error.
    return null
  }
}

async function request<T>(
  method: string,
  path: string,
  body?: JsonBody,
  init?: RequestInit,
): Promise<T> {
  let res: Response
  try {
    res = await fetch(`${BASE}${path}`, {
      ...init,
      method,
      // Same-origin today; 'include' also covers a future split-host setup
      // without touching call sites.
      credentials: 'include',
      headers: {
        Accept: 'application/json',
        ...(body === undefined ? {} : { 'Content-Type': 'application/json' }),
        ...init?.headers,
      },
      body: body === undefined ? init?.body : JSON.stringify(body),
    })
  } catch {
    // fetch() rejects only on transport failure, which in practice means the
    // gateway is not running or the proxy target is wrong.
    throw new ApiError(0, {
      error: `No response from ${BASE || 'the gateway'}${path}. Is the gateway running on port 8020?`,
    })
  }

  const payload = await readBody(res)
  if (!res.ok) {
    // A single choke point for "the session died mid-use" (cookie expired,
    // revoked, or the server restarted with a new signing secret) — every
    // call goes through here, so this is the one place that can notice
    // without every page threading its own 401 handling. `useSession` listens
    // and re-checks itself only while it currently believes it's signed in,
    // so this can't loop against the normal 401 a signed-out `getMe()` gets.
    if (res.status === 401) window.dispatchEvent(new Event('gov:session-expired'))
    throw new ApiError(res.status, payload)
  }
  return payload as T
}

export const api = {
  get: <T>(path: string, init?: RequestInit) => request<T>('GET', path, undefined, init),
  post: <T>(path: string, body?: JsonBody, init?: RequestInit) =>
    request<T>('POST', path, body, init),
  patch: <T>(path: string, body?: JsonBody, init?: RequestInit) =>
    request<T>('PATCH', path, body, init),
  put: <T>(path: string, body?: JsonBody, init?: RequestInit) => request<T>('PUT', path, body, init),
  del: <T>(path: string, init?: RequestInit) => request<T>('DELETE', path, undefined, init),
}

// ── Typed endpoints ───────────────────────────────────────────────────────────
// Add these as panels get ported, so response shapes live next to the client
// rather than being re-guessed at each call site.

export interface Health {
  status: string
  service: string
}

export interface Me {
  name: string
  role: string
}

/** Open probe — no session required (gateway/app.py:3503). */
export const getHealth = () => api.get<Health>('/health')

/** Current session's principal; throws a 401 ApiError when not signed in. */
export const getMe = () => api.get<Me>('/dashboard/me')

/** One MCP backend as the gateway sees it (gateway/analytics.py:445). */
export interface BackendHealth {
  backend: string
  url: string | null
  status: 'healthy' | 'degraded' | 'down' | 'idle'
  calls: number
  errors: number
  error_rate: number
  last_ok: number | null
  last_error_ts: number | null
  last_error: string | null
  /** null when the caller asked for ?probe=0 (traffic signal only). */
  probe_ok: boolean | null
  probe_latency_ms: number | null
  probe_error: string | null
}

/**
 * Live MCP liveness probes plus the recent-traffic error signal, per backend.
 * Admin-only (403 otherwise). Probes are deduped by URL server-side and the
 * result is cached, so polling this is cheap.
 */
export const getBackendHealth = () =>
  api.get<{ backends: BackendHealth[] }>('/admin/backends/health')

/** One audited governed call, as the audit ring buffer records it
 *  (governance_core/audit.py), after safety.classify() adds the two flags. */
export interface GovernedCall {
  /** Unix SECONDS, from `time.time()` — not milliseconds. */
  ts: number
  consumer: string
  tool: string
  /** 'ok' | 'denied' | 'error' — widened, because the backend may add more and
   *  an unknown status must render as itself rather than crash the row. */
  status: string
  /** Records the (redacted) result carried — the exfiltration signal. Absent on
   *  denied records, which never reached a backend (audit.py:337). */
  rows?: number | null
  latency_ms: number | null
  /** Free text; the denial reason on a denied record. A `redacted:` prefix means
   *  fields were dropped for this caller. */
  detail: string | null
  /** Added by safety.classify() on the way out, not stored in the audit record. */
  suspicious?: boolean
  reasons?: string[]
}

export interface ActivitySummary {
  total: number
  ok: number
  denied: number
  error: number
  redactions: number
  rows: number
}

/** Layer-1 hourly budget for this principal (governance_core/edge.py:129).
 *  Null when they have not made a governed call this window. */
export interface RateLimitHeadroom {
  consumer: string
  used: number
  limit: number
  window_resets_in_sec: number
}

export interface MyActivity {
  /** Most recent first, capped at 200 server-side. */
  calls: GovernedCall[]
  summary: ActivitySummary
  rate_limit: RateLimitHeadroom | null
}

/** This caller's own governed calls (backend/session.py:284). Session-scoped —
 *  never another principal's, so it needs no admin role. */
export const getMyActivity = () => api.get<MyActivity>('/dashboard/my-activity')

// ── Chat ──────────────────────────────────────────────────────────────────
// The governed assistant. /chat and /chat/stream answer the same turn through
// the same PEP path (tool calls resolve + audit under the signed-in principal);
// streaming is the primary path, /chat is both the SSE fallback and how a
// plain caller (no EventSource support) would use this API.

/** One resolved tool call the assistant made this turn (orchestrator.py's
 *  `used` list). `result` is the tool's own raw JSON-string return value —
 *  e.g. propose_graph's result carries the real graphId it just created,
 *  which the copilot's own reply text may not restate verbatim. */
export interface ChatToolCall {
  tool: string
  args: Record<string, unknown>
  result?: string
}

export interface ChatTurnResult {
  reply: string
  tool_calls: ChatToolCall[]
  conversation_id: string
  /** False only when no chat model is configured server-side — a deploy-time
   *  state, not a per-turn failure (orchestrator.py:294). */
  configured?: boolean
}

/** Non-streaming turn: waits for the full reply. Used as the fallback when
 *  streaming fails, and by anything that doesn't need incremental text. */
export const sendChat = (message: string, conversationId: string) =>
  api.post<ChatTurnResult>('/chat', { message, conversation_id: conversationId })

/** The "My Workflow" builder's own copilot -- a separate governed conversation
 *  (own session_id namespace, own system prompt: orchestrator.py's
 *  WORKFLOW_COPILOT_SYSTEM_PROMPT) from the general assistant above, but the
 *  exact same turn shape and the exact same chat_log session/history storage. */
export const sendWorkflowChat = (message: string, conversationId: string) =>
  api.post<ChatTurnResult>('/workflow-chat', { message, conversation_id: conversationId })

/** SSE event shapes from /chat/stream (backend/chat.py:90-107). `tool_calls` on
 *  `done` carries the same shape as ChatTurnResult's, confirmed live. */
export type ChatStreamEvent =
  | { type: 'meta'; conversation_id: string }
  | { type: 'delta'; text: string }
  | { type: 'replace'; text: string }
  | { type: 'tools'; tools: string[] }
  | { type: 'done'; tool_calls: ChatToolCall[]; configured?: boolean }
  | { type: 'error'; message: string }

export interface ChatSessionSummary {
  session_id: string
  created_at: number
  last_active_at: number
  status: 'open' | 'closed'
  message_count: number
  summary: string | null
}

export const getChatHistoryList = () => api.get<{ sessions: ChatSessionSummary[] }>('/dashboard/chat-history')

export interface ChatTranscriptMessage {
  role: 'user' | 'assistant'
  content: string
  ts: number
  tools_used?: string[] | null
}

export interface ChatTranscript {
  session_id: string
  status: 'open' | 'closed'
  created_at: number
  last_active_at: number
  closed_at: number | null
  summary: string | null
  messages: ChatTranscriptMessage[]
}

/** Loads a conversation's messages, e.g. to resume it after a reload. 404s (as
 *  an ApiError) when the id is unknown — a brand-new conversation, not an error. */
export const getChatTranscript = (sessionId: string) =>
  api.get<ChatTranscript>(`/dashboard/chat-history/${encodeURIComponent(sessionId)}`)

export interface ChatResumeResult {
  conversation_id: string
  /** True when the requested session was already closed, so the server cloned
   *  it into a fresh open session pre-seeded with its messages rather than
   *  reopening the closed id — `conversation_id` is that NEW id, not the one
   *  requested. */
  cloned: boolean
}

/** "Continue this conversation" from a past-conversation list. An open session
 *  resumes under its own id (`cloned: false`); a closed one is cloned into a
 *  fresh open session (backend/chat.py's `_chat_resume` — the server never
 *  reopens a closed id). 404s (as an ApiError) when the id is unknown. */
export const resumeChatHistory = (sessionId: string) =>
  api.post<ChatResumeResult>(`/dashboard/chat-history/${encodeURIComponent(sessionId)}/resume`)

export const sendChatFeedback = (conversationId: string, rating: 'up' | 'down') =>
  api.post<{ ok: boolean }>('/dashboard/feedback', { conversation_id: conversationId, rating })

export interface WorkflowSuggestion {
  templateId: string
  displayName: string
  reason: string
  outputTypes: string[]
  inputs: Record<string, unknown>
  score: number
}

/** Deterministic (non-LLM) keyword match from the turn's text to workflow
 *  templates — offered as "run this as a workflow" under a reply. */
export const getWorkflowSuggestions = (text: string, limit = 3) =>
  api.post<{ suggestions: WorkflowSuggestion[] }>('/workflow-suggestions', { message: text, limit })

export interface WorkflowArtifactRef {
  artifactId: string
  filename: string
  downloadUrl?: string
}

export interface WorkflowLaunchResult {
  runId: string
  status: string
  artifacts?: WorkflowArtifactRef[]
}

export const launchWorkflowSuggestion = (templateId: string, inputs: Record<string, unknown>) =>
  api.post<WorkflowLaunchResult>('/workflow-suggestions/launch', { template_id: templateId, inputs })

// ── My Access ─────────────────────────────────────────────────────────────────
// This principal's own resolved grant, the self-service request-access catalog,
// and the API key used to call the gateway as this principal outside the browser.

/** Plain-English view of a tool (backend/deps.py:_tool_info) — never the raw
 *  canonical name a normal user has no reason to parse. */
export interface ToolInfo {
  name: string
  description: string
  risk: string | null
  approvalRequired: boolean
}

/** What this principal can do on one backend: the tools, and the redaction
 *  levels that unlock which fields on those tools come back unmasked. */
export interface AccessGrant {
  tools: ToolInfo[]
  levels: string[]
}

export interface AccessSelection {
  category: string
  backend: string
  tools: string[]
}

/** A self-service request. `selections` is what this UI produces; `categories`
 *  and `backend` are older/other request shapes the same list endpoint can
 *  return (signup requests, etc.) — present in the type so a record missing
 *  `selections` doesn't silently type-check as one that has it. */
export interface AccessRequestRecord {
  id: string
  kind: string
  selections?: AccessSelection[]
  categories?: string[]
  backend?: string
  justification: string
  status: 'pending' | 'approved' | 'denied' | string
  created_at: number
}

export interface MyAccess {
  name: string
  /** Empty string, not null, when unset — the gateway always returns a string. */
  full_name: string
  department: string
  status: string
  role: string
  type: string
  categories: string[]
  effective_categories: string[]
  has_key: boolean
  /** Keyed by backend id. Empty object while `status !== 'active'`. */
  access: Record<string, AccessGrant>
  requests: AccessRequestRecord[]
}

export const getMyAccess = () => api.get<MyAccess>('/dashboard/my-access')

export interface CategoryCatalogEntry {
  id: string
  display_name: string
  backend: string
  tools: ToolInfo[]
  /** Gateway-level permissions (files/workflow_runner/...) have no MCP tools of
   *  their own — this is the only explanation the picker has for what holding
   *  one of those actually grants. */
  data_domains: string[]
}

/** The full category catalog — every user, not just admins (it's what the
 *  request-access picker groups by). */
export const getCategoryCatalog = () =>
  api.get<{ categories: CategoryCatalogEntry[] }>('/dashboard/category-catalog')

/** Mints a fresh key, replacing any previous one. Shown once — the gateway
 *  never returns an existing key, only its hash. */
export const rotateMyKey = () => api.post<{ api_key: string }>('/dashboard/my-key/rotate')

export interface TryToolResult {
  /** Namespaced (`<backend>_<canonical>`), unlike the plain canonical name
   *  the request took as `tool`. */
  tool: string
  /** Already governed and redacted exactly as this principal's own grant
   *  would produce it — this is not a preview or a dry run. */
  result: unknown
}

/** Runs one governed tool call as the signed-in principal — full PDP, scope,
 *  and redaction apply, and it's audited like any other call. 400s (as an
 *  ApiError) for an unknown tool or non-object `args`; 403 if the account
 *  isn't active yet. */
export const tryTool = (tool: string, args: Record<string, unknown>, customerId: string) =>
  api.post<TryToolResult>('/dashboard/try-tool', { tool, args, customer_id: customerId })

/** One queued calendar invite send (calendar_send_models.CalendarSendRecord).
 *  Created by `send_calendar_invite` once a drafted .ics has an approved
 *  approval tied to it — drafting alone (`draft_calendar_invite`, via
 *  `tryTool`) never reaches this queue. */
export interface CalendarSend {
  sendId: string
  owner: string
  draftArtifactId: string
  approvalId: string
  status: 'sent' | 'queued_for_connector' | 'approval_required' | string
  provider: string
  title: string
  /** Naive local wall-clock ISO string, paired with `timezone` — not UTC. */
  start: string
  end: string
  timezone: string
  attendees: string[]
  location: string
  message: string
  createdAt: number
  updatedAt: number
  sentAt: number | null
}

/** This principal's own queued calendar sends (session-scoped — never
 *  another principal's, matching the same shape as email/other send
 *  queues). */
export const getCalendarSends = () => api.get<{ sends: CalendarSend[] }>('/calendar-sends')

/** One queued email send (email_send_models.EmailSendRecord) — same
 *  approval-gated shape as `CalendarSend`, reached the same way: a drafted
 *  email needs an approved approval tied to it before `send_email` queues
 *  it here. */
export interface EmailSend {
  sendId: string
  owner: string
  draftArtifactId: string
  approvalId: string
  status: 'sent' | 'queued_for_connector' | 'approval_required' | string
  provider: string
  to: string[]
  cc: string[]
  subject: string
  attachmentArtifactIds: string[]
  message: string
  createdAt: number
  updatedAt: number
  sentAt: number | null
}

/** This principal's own queued email sends (session-scoped). */
export const getEmailSends = () => api.get<{ sends: EmailSend[] }>('/email-sends')

// ── Files (artifacts) ───────────────────────────────────────────────────────
// Generated/uploaded artifacts (governance_core/artifact_models.py) — reports,
// drafts, uploads, anything produced by or fed into a governed workflow.
// Everyone sees their own plus anything actively shared with them; an admin
// with `?all=1` sees everyone's (backend/artifacts.py's `_artifacts`).

export interface Artifact {
  artifactId: string
  owner: string
  title: string
  filename: string
  /** Free-form — `word`/`excel`/`powerpoint`/`pdf`/`email_draft`/
   *  `calendar_invite`/`json`/`text`/anything else a workflow produced. */
  type: string
  mimeType: string
  classification: string[]
  sourceWorkflowRunId: string | null
  sourceToolCalls: string[]
  sourceArtifactIds: string[]
  checksum: string
  sizeBytes: number
  status: string
  createdAt: number
  expiresAt: number | null
  retentionDays: number | null
  currentVersion: number
  versionCount: number
  latestVersionId: string
  downloadUrl: string
  workbenchUrl: string
  /** Computed server-side (`_artifact_public_for`), not stored on the record:
   *  true when this caller sees it via an active share rather than owning it. */
  shared?: boolean
  expired?: boolean
}

/** `all` (admin-only; a non-admin's request for it is silently ignored
 *  server-side) lists every principal's artifacts instead of just this
 *  caller's own + shared-with-them. Capped at 100 server-side. */
export const listArtifacts = (all = false) => api.get<{ artifacts: Artifact[] }>(`/artifacts${all ? '?all=1' : ''}`)

export interface CreateArtifactInput {
  title?: string
  filename?: string
  /** Exactly one of `text`/`content_base64` — plain text is the "paste
   *  instead of upload" path, auto-named `<title>.txt` server-side if
   *  `filename` is omitted. */
  text?: string
  content_base64?: string
  mime_type?: string
  artifact_type?: string
  classification?: string[]
  retention_days?: number
}

/** Requires the `files` category unless admin. 25MB cap server-side (base64
 *  decoded size). Returns the created artifact, 201. */
export const createArtifact = (input: CreateArtifactInput) => api.post<Artifact>('/artifacts', input)

export const getArtifact = (id: string) => api.get<Artifact>(`/artifacts/${encodeURIComponent(id)}`)

/** Owner or admin only. */
export const deleteArtifact = (id: string) =>
  api.del<{ ok: boolean; artifactId: string }>(`/artifacts/${encodeURIComponent(id)}`)

/** Type-specific facts pulled from the file itself — sheet/slide counts,
 *  attendee lists, table detection — never raw file bytes. `signals` shape
 *  depends entirely on `kind` (== the artifact's `type`); read defensively. */
export interface ArtifactPreview {
  kind: string
  filename: string
  summary: string[]
  packageParts: string[]
  signals: Record<string, unknown>
  error?: string
}

export interface ArtifactVersionRecord {
  artifactId: string
  versionId: string
  versionNumber: number
  filename: string
  mimeType: string
  checksum: string
  sizeBytes: number
  createdBy: string
  createdAt: number
  note: string
  downloadUrl: string
}

export interface ArtifactShare {
  shareId: string
  artifactId: string
  owner: string
  sharedWith: string
  permissions: string[]
  createdBy: string
  status: 'active' | 'revoked' | string
  createdAt: number
  expiresAt: number | null
  revokedAt: number | null
  revokedBy: string | null
}

/** One approval request tied to this artifact — the same shape
 *  `getApprovals()` returns, filtered server-side to this artifact's id. */
export interface ArtifactApproval {
  approvalId: string
  requestedBy: string
  reason: string
  status: 'pending' | 'approved' | 'denied' | string
  riskLevel: string
  artifactIds: string[]
  workflowRunId: string
  approver: string
  decisionNote: string
  createdAt: number
  decidedAt: number | null
}

/** Present only when `GOVERNANCE_ONLYOFFICE_DOCUMENT_SERVER_URL` is
 *  configured server-side AND the artifact's type is one ONLYOFFICE can open
 *  (word/excel/powerpoint/pdf). `config` is opaque on purpose — it's handed
 *  verbatim to `new DocsAPI.DocEditor(mountId, config)`, never read into or
 *  reconstructed from typed fields, so a new key ONLYOFFICE's API adds still
 *  reaches it untouched. */
export interface OnlyOfficeConfig {
  enabled: true
  documentServerUrl: string
  documentType: 'word' | 'cell' | 'slide' | 'pdf' | string
  config: Record<string, unknown>
}

/** The artifact's full detail view — everything the manage drawer needs in
 *  one call. `shares` comes back empty for a caller who isn't the owner/admin
 *  (backend/artifacts.py only populates it for them). Document review
 *  threads (comments, approve/request-changes) aren't modeled here — this
 *  console doesn't render that workflow, only the ONLYOFFICE viewer/editor
 *  itself, so `reviews`' shape is left untyped rather than guessed at. */
export interface ArtifactWorkbench {
  artifact: Artifact
  preview: ArtifactPreview
  approvals: ArtifactApproval[]
  shares: ArtifactShare[]
  versions: ArtifactVersionRecord[]
  reviews: unknown[]
  onlyoffice: OnlyOfficeConfig | null
}

export const getArtifactWorkbench = (id: string) => api.get<ArtifactWorkbench>(`/artifacts/${encodeURIComponent(id)}/workbench`)

export interface AddArtifactVersionInput {
  filename?: string
  text?: string
  content_base64?: string
  mime_type?: string
  note?: string
}

/** Owner or admin only. */
export const addArtifactVersion = (id: string, input: AddArtifactVersionInput) =>
  api.post<{ artifact: Artifact; version: ArtifactVersionRecord }>(`/artifacts/${encodeURIComponent(id)}/versions`, input)

/** Owner or admin only. Grants `view` by default; add `'download'` to let the
 *  recipient pull the file itself, not just see its metadata/preview. */
export const createArtifactShare = (
  id: string,
  input: { shared_with: string; permissions?: string[]; expires_in_days?: number },
) => api.post<ArtifactShare>(`/artifacts/${encodeURIComponent(id)}/shares`, input)

export const revokeArtifactShare = (id: string, shareId: string) =>
  api.post<ArtifactShare>(`/artifacts/${encodeURIComponent(id)}/shares/${encodeURIComponent(shareId)}/revoke`)

export const requestArtifactApproval = (id: string, reason: string, riskLevel = 'medium') =>
  api.post<ArtifactApproval>(`/artifacts/${encodeURIComponent(id)}/request-approval`, { reason, risk_level: riskLevel })

// ── Templates ────────────────────────────────────────────────────────────────
// Versioned reusable templates for governed office workflows and generated
// artifacts. Readable by anyone signed in; creating, versioning, and
// disabling are admin-only server-side (backend/templates.py) — the page
// gates those actions on `session.isAdmin` the same way the legacy panel
// checked `ME.role === 'admin'`, purely so the UI doesn't offer a door that
// won't open. `content`'s shape depends on `templateType`: workflow requires
// `goal`/`inputs`/`steps`/`outputs` to all be PRESENT (any value, even
// empty); excel/powerpoint/word require at least one of `sections`/`tables`/
// `layout` to be truthy (governance_core/template_store.py's `_validate`).
// email/calendar/prompt/generic have no server-enforced shape.

export type TemplateType = 'excel' | 'powerpoint' | 'word' | 'email' | 'calendar' | 'workflow' | 'prompt' | 'generic'

export interface TemplateVersionRecord {
  version: number
  createdBy: string
  createdAt: number
  notes: string
  /** Present when the caller asked for content (list calls omit it). */
  content?: Record<string, unknown>
}

export interface Template {
  templateId: string
  displayName: string
  templateType: TemplateType | string
  status: 'active' | 'draft' | 'disabled' | string
  owner: string
  description: string
  classification: string[]
  tags: string[]
  allowedWorkflowIds: string[]
  currentVersion: number
  createdAt: number
  updatedAt: number
  /** Notes on the current version, hoisted up for the list view — the full
   *  version history only comes back when `versions` is requested. */
  latestNotes: string
  versions?: TemplateVersionRecord[]
  /** Only present on a single-template fetch that asked for content AND
   *  didn't also ask for versions; when versions are included, read the
   *  current version's own `content` instead. */
  content?: Record<string, unknown>
}

/** Admin sees disabled templates too when `includeDisabled` is set; everyone
 *  else only ever sees non-disabled ones regardless of the flag. */
export const listTemplates = (includeDisabled = false) =>
  api.get<{ templates: Template[] }>(`/templates${includeDisabled ? '?include_disabled=1' : ''}`)

/** Always includes full version history + each version's content — there is
 *  no "just the latest" fetch for a single template. */
export const getTemplate = (id: string) => api.get<Template>(`/templates/${encodeURIComponent(id)}?content=1`)

export interface CreateTemplateInput {
  display_name: string
  template_type: string
  content: Record<string, unknown>
  description?: string
  classification?: string[]
  tags?: string[]
  allowed_workflow_ids?: string[]
  notes?: string
}

/** 400s (as an ApiError) if `content` fails `_validate` for the chosen type —
 *  the message names exactly which required key is missing. */
export const createTemplate = (input: CreateTemplateInput) => api.post<Template>('/templates', input)

/** Adds a new version (bumps `currentVersion`); the template's type can't
 *  change, so `content` is re-validated against whatever type it already is. */
export const addTemplateVersion = (id: string, content: Record<string, unknown>, notes: string) =>
  api.post<Template>(`/templates/${encodeURIComponent(id)}/versions`, { content, notes })

export const disableTemplate = (id: string) => api.post<Template>(`/templates/${encodeURIComponent(id)}/disable`)

/** Submits a self-service grant request. 400s (as an ApiError) if every
 *  selected tool is already held. */
export const requestAccess = (selections: { category: string; tools: string[] }[], justification: string) =>
  api.post<{ ok: boolean; id: string }>('/dashboard/request-access', { selections, justification })

/** One tool this principal has been denied, aggregated across recent attempts,
 *  with enough context to file a one-click request for it. */
export interface MyDenial {
  tool: string
  canonical: string
  description: string
  backend: string | null
  category: string | null
  category_name: string | null
  risk: string | null
  attempts: number
  last_ts: number
}

export const getMyDenials = () => api.get<{ denials: MyDenial[] }>('/dashboard/my-denials')

/** Logs "I can't build this myself yet" as kind="workflow" in the SAME queue as
 *  access requests (admin_policy.py's _admin_requests) -- the assistant's
 *  submit_workflow_request tool lands in the identical queue, so an admin sees
 *  both a chat-originated ask and one typed directly here in one place. */
export const requestWorkflow = (description: string) =>
  api.post<{ ok: boolean; id: string }>('/dashboard/request-workflow', { description })

// ── Admin: access requests & approvals ─────────────────────────────────────
// Two independent admin queues (admin_policy.py, approvals.py): self-service
// access/signup requests, and the generic high-impact-action approval queue
// that external sends and workflow steps feed into.

/** One pending or decided access/signup request, as an admin sees it — wider
 *  than `AccessRequestRecord` (which is a caller's view of their own), since
 *  an admin also needs to know who asked and, once decided, who decided.
 *  `tools`/`justification` cover the oldest bare-backend request shape
 *  (admin_policy.py's undiscriminated `else` branch) alongside the current
 *  `selections` one. */
export interface AdminAccessRequest {
  id: string
  kind: string
  consumer_id?: string
  username?: string
  selections?: AccessSelection[]
  categories?: string[]
  backend?: string
  tools?: string[]
  justification: string
  status: 'pending' | 'approved' | 'denied' | string
  created_at: number
  decided_by?: string
  decided_at?: number | null
}

/** Admin-only; `status` is an exact match, not a filter menu — the panel only
 *  ever asks for `pending`. */
export const getAdminRequests = (status = 'pending') =>
  api.get<{ requests: AdminAccessRequest[] }>(`/admin/requests?status=${encodeURIComponent(status)}`)

/** Grants the request's selections/categories. 409s (as an ApiError) when the
 *  consumer is gone or the request has nothing valid left to grant — the
 *  caller should surface that message rather than a generic failure. */
export const approveAdminRequest = (id: string) =>
  api.post<{ ok: boolean }>(`/admin/requests/${encodeURIComponent(id)}/approve`, {})

/** Marks the request denied; also disables the consumer for a signup-kind
 *  request. */
export const denyAdminRequest = (id: string) =>
  api.post<{ ok: boolean }>(`/admin/requests/${encodeURIComponent(id)}/deny`, {})

/** One (consumer, tool) pair aggregated from recent "not granted" denials —
 *  a hint that a grant is missing, not a request anyone made. */
export interface AccessSuggestion {
  consumer: string
  tool: string
  attempts: number
}

export const getAccessSuggestions = () =>
  api.get<{ suggestions: AccessSuggestion[] }>('/admin/access-suggestions')

/** One high-impact action waiting on (or already given) an admin decision —
 *  an external send, a workflow step, anything gated behind `approval_store`.
 *  Field names come through camelCase already (`ApprovalRecord.public_dict`),
 *  unlike the rest of this file's snake_case payloads. */
export interface Approval {
  approvalId: string
  requestedBy: string
  reason: string
  status: 'pending' | 'approved' | 'denied' | string
  riskLevel: string
  artifactIds: string[]
  workflowRunId: string
  approver: string
  decisionNote: string
  /** Unix seconds. */
  createdAt: number
  decidedAt: number | null
}

/** Admin-only. Always the full queue, decided included — there is no
 *  pending/decided toggle in this panel, matching the legacy one. */
export const getApprovals = () => api.get<{ approvals: Approval[] }>('/approvals?all=1&include_decided=1')

/** Approves or denies one approval, with an optional note. Returns the
 *  updated record. When the approval is tied to a workflow run this also
 *  updates that run's matching step server-side — nothing this UI needs to
 *  special-case. */
export const decideApproval = (id: string, action: 'approve' | 'deny', note: string) =>
  api.post<Approval>(`/approvals/${encodeURIComponent(id)}/${action}`, { note })

// ── Admin: break-glass controls & credential hygiene ───────────────────────
// Two more admin-only views, both enforced/computed server-side: the global
// containment switches every governed call passes through (govern.py), and a
// derived report over which API-key consumers are dormant, never used, or
// overdue for rotation.

/** The four independent kill switches `_govern()` checks on every call.
 *  `paused_agents` blocks every agent (API-key) caller everywhere; humans and
 *  admins are unaffected. `paused_backends` blocks calls to those named
 *  backends for EVERYONE, agent or human. `paused_consumers` blocks one or
 *  more specific consumers (by `consumer_id`), agent or human, regardless of
 *  the other two switches -- a lighter, instantly-reversible containment
 *  lever distinct from `setConsumerStatus('disabled')`, which revokes the
 *  consumer's credential/login entirely rather than just pausing it.
 *  `paused_categories` is narrower still: `{consumer_id: [category_id, ...]}`
 *  temporarily blocks just those categories' tools for that one consumer,
 *  without touching their permanent `categories` grant (Consumers page). */
export interface AdminControls {
  paused_agents: boolean
  paused_backends: string[]
  paused_consumers: string[]
  paused_categories: Record<string, string[]>
}

/** `backends` is every known backend name (`sorted(manifest.backends())`),
 *  regardless of pause state — the full checklist for the "block this MCP"
 *  UI, in the same order `getBackendHealth` returns them. */
export const getAdminControls = () =>
  api.get<{ controls: AdminControls; backends: string[] }>('/admin/controls')

/** Applies all three switches together; takes effect on the very next
 *  governed call. 409s (as an ApiError) when the policy store is read-only
 *  (no persistence configured for this deployment). */
export const setAdminControls = (controls: AdminControls) =>
  api.put<{ ok: boolean; controls: AdminControls }>('/admin/controls', controls)

/** One API-key consumer's hygiene read, as `analytics.credential_hygiene()`
 *  computes it: `flags` is 0-2 of "never used" / "dormant &gt;30d" (mutually
 *  exclusive) and "rotation unknown" / "key &gt;90d old" (also mutually
 *  exclusive) — never more than one from each pair. */
export interface CredentialHygieneConsumer {
  consumer_id: string
  name: string
  role: string
  type: string
  status: 'active' | 'disabled' | string
  /** Days since the key was last rotated (or created, if never rotated). */
  key_age_days: number | null
  /** Days since this consumer's last governed call. */
  idle_days: number | null
  last_used: number | null
  last_rotation: number | null
  flags: string[]
}

/** Admin-only. Pre-sorted worst-first (most flags, then longest idle) by the
 *  server, so the UI can render it as-is. */
export const getCredentialHygiene = () =>
  api.get<{ consumers: CredentialHygieneConsumer[] }>('/admin/credential-hygiene')

/** Mints a fresh key for this consumer and invalidates the old one
 *  immediately. Returned once — the caller must show it now or it's gone. */
export const rotateConsumerKey = (consumerId: string) =>
  api.post<{ api_key: string }>(`/admin/consumers/${encodeURIComponent(consumerId)}/rotate-key`)

/** Disables or reactivates a consumer. Disabling is enforced at the auth
 *  edge — every governed call from this consumer is rejected until
 *  reactivated. Returns the updated record, same as `updateConsumer`. */
export const setConsumerStatus = (consumerId: string, status: 'active' | 'disabled') =>
  api.patch<{ consumer: Consumer }>(`/admin/consumers/${encodeURIComponent(consumerId)}`, { status })

// ── Admin: consumers ────────────────────────────────────────────────────────
// Principals — people and agents — plus the categories and department that
// decide what they can call. A consumer's EFFECTIVE grant is its own
// `categories` unioned live with its department's current categories
// (governance_core/policy/resolve.py) — editing a department later reaches
// every member automatically, with nothing to keep in sync per-consumer.
// `effective_backends` below is that union already resolved to backend names,
// not a list of category ids.

/** One principal, as the admin list/CRUD sees it (`_consumer_public`,
 *  backend/deps.py). `categories` is what THIS record holds directly;
 *  `department` is the other, additive half of its grant (see module note
 *  above) — a consumer can have both at once. */

/** Per-backend fine-tuning on top of the categories union (policy/resolve.py):
 *  `grantTools`/`grantLevels` add, `denyTools`/`denyLevels` always win. Keyed
 *  by backend, not category — two categories that share a backend (e.g.
 *  `email_send_external`/`email_send_internal`, both backend `email`) share
 *  the SAME deny list, so denying a tool here removes it regardless of which
 *  held category would otherwise have granted it. */
export interface ConsumerOverrides {
  [backend: string]: {
    grantTools?: string[]
    denyTools?: string[]
    grantLevels?: string[]
    denyLevels?: string[]
  }
}

export interface Consumer {
  consumer_id: string
  name: string
  full_name: string
  department: string
  status: 'active' | 'disabled' | string
  type: string
  role: string
  categories: string[]
  /** Backend names (or `["*"]` for "every tool"), already resolved from
   *  categories ∪ department — not category ids. */
  effective_backends: string[]
  rate_limit_per_hour: number | null
  ip_allowlist: string[]
  overrides: ConsumerOverrides
  allowed_levels: string[]
  has_key: boolean
  has_login: boolean
}

export const listConsumers = () => api.get<{ consumers: Consumer[] }>('/admin/consumers')

export interface CreateConsumerInput {
  name: string
  role: string
  type: string
  /** Directly-granted category ids — independent of `department`. */
  categories: string[]
  department?: string
  rate_limit_per_hour?: number | null
  /** Restricts this one consumer's /mcp calls to these CIDRs/IPs, on top of
   *  whatever the global allowlist already permits. Empty/omitted = no
   *  restriction beyond the global list. */
  ip_allowlist?: string[]
  /** Excludes specific tools from an otherwise-granted category (see
   *  ConsumerOverrides) — e.g. hold `orders` but deny `cancel_order`. */
  overrides?: ConsumerOverrides
  /** Only needed if this consumer should also be able to sign into the
   *  console itself, separately from its API key. */
  password?: string | null
}

/** Creates the account and mints its first API key in the same call — there
 *  is no separate "issue a key" step. 409s (as an ApiError) if the id (which
 *  defaults to `name`) is already taken. */
export const createConsumer = (input: CreateConsumerInput) =>
  api.post<{ consumer: Consumer; api_key: string }>('/admin/consumers', input)

export interface UpdateConsumerInput {
  status?: 'active' | 'disabled'
  role?: string
  type?: string
  categories?: string[]
  department?: string
  rate_limit_per_hour?: number | null
  full_name?: string
  ip_allowlist?: string[]
  overrides?: ConsumerOverrides
}

/** Partial update — only the fields present in `patch` change. */
export const updateConsumer = (consumerId: string, patch: UpdateConsumerInput) =>
  api.patch<{ consumer: Consumer }>(`/admin/consumers/${encodeURIComponent(consumerId)}`, patch)

export const deleteConsumer = (consumerId: string) =>
  api.del<{ ok: boolean }>(`/admin/consumers/${encodeURIComponent(consumerId)}`)

export interface ConsumerProfile {
  name: string
  full_name: string
  department: string
  role: string
  type: string
  status: string
  /** Active safety-heuristic flags for this principal right now — empty
   *  means clean, not "not computed." */
  reasons: string[]
  totals: { total: number; last_24h: number; last_7d: number; denied: number; error: number }
  distinct_customers: number
  rows_returned: number
  sensitive_calls: number
  redactions: number
  rate_limit_per_hour: number | null
  last_used: number | null
  last_key_rotation: number | null
  /** Tools this consumer is granted but has never actually called — least-
   *  privilege candidates to revoke. */
  unused_grants: string[]
  ips: string[]
}

export const getConsumerProfile = (consumerId: string) =>
  api.get<ConsumerProfile>(`/admin/consumers/${encodeURIComponent(consumerId)}/profile`)

/** A department is just a named, reusable set of categories
 *  (governance_core/departments.py) — picking one for a consumer adds its
 *  CURRENT categories to that consumer's grant, live, not as a one-time copy. */
export interface Department {
  id: string
  display_name: string
  categories: string[]
}

export const listDepartments = () => api.get<{ departments: Department[] }>('/admin/departments')

/** Creates or fully replaces a department (upsert by `id`, same shape as
 *  `upsertCategory` below). Unlike categories, this DOES validate its
 *  `categories` list server-side — an unknown category id is rejected
 *  (400), not silently accepted. */
export const upsertDepartment = (input: Department) =>
  api.post<{ ok: boolean; id: string }>('/admin/departments', input)

/** No existence check server-side — deleting an unknown id is a silent
 *  no-op 200. Members keep their `department` id but resolve as if they had
 *  none until reassigned; nothing reassigns them automatically. */
export const deleteDepartment = (id: string) =>
  api.del<{ ok: boolean }>(`/admin/departments/${encodeURIComponent(id)}`)

// ── Admin: categories ────────────────────────────────────────────────────────
// A category is a data-domain template: one backend, which of that backend's
// tools it grants ("*" for all, tracked live as new tools appear, or an
// explicit list), and which classification levels come through unredacted.
// `/admin/categories` POST is a plain upsert keyed on `id` — there is no
// separate update endpoint; "editing" a category is re-POSTing the whole
// record with the same id.

/** The admin-only sibling of `/dashboard/category-catalog` — raw ids and
 *  names only (no descriptions), but it's where the valid backend list and
 *  the fixed classification levels actually come from. */
export interface AdminCatalog {
  /** Canonical tool names per backend — needs `humanize()` for display. */
  backends: Record<string, string[]>
  /** Always the 4 in use today: PUBLIC, INTERNAL, PII, SENSITIVE. */
  levels: string[]
  categories: string[]
  departments: string[]
  roles: string[]
  types: string[]
}

export const getAdminCatalog = () => api.get<AdminCatalog>('/admin/catalog')

export interface AdminCategory {
  id: string
  display_name: string
  backend: string
  /** Literal `"*"` means every tool `tools_for_backend(backend)` returns —
   *  resolved live, not expanded/stored, so it grants tools added later too. */
  tools: '*' | string[]
  levels: string[]
  /** Free-form, documentation only — never enforced. */
  data_domains: string[]
}

export const listCategories = () => api.get<{ categories: AdminCategory[] }>('/admin/categories')

/** Creates or fully replaces a category (upsert by `id`). 400s (as an
 *  ApiError) if `id`/`backend` is missing or `backend` isn't a real one —
 *  everything else (tool names, levels, data_domains) is accepted
 *  unvalidated server-side. */
export const upsertCategory = (input: AdminCategory) =>
  api.post<{ ok: boolean; id: string }>('/admin/categories', input)

/** No existence check server-side — deleting an unknown id is a silent
 *  no-op 200. Consumers/departments that still list this id keep the id but
 *  stop getting any grant from it; nothing cleans that up automatically. */
export const deleteCategory = (id: string) => api.del<{ ok: boolean }>(`/admin/categories/${encodeURIComponent(id)}`)

// ── Admin: alerts ───────────────────────────────────────────────────────────
// One incident per (principal, signal type), rolled up server-side from
// safety.classify()'s live heuristics — enumeration, denial bursts, call
// bursts, repeated tool errors on one backend. `status` is reconstructed from
// audit policy-change events, not stored on the record itself.

export interface Alert {
  id: string
  consumer: string
  consumer_id: string | null
  /** 'enumeration' | 'burst' | 'denials' | 'errors' | 'other' — widened,
   *  since a new signal type should still render rather than crash. */
  type: string
  severity: 'critical' | 'high' | 'medium' | 'low' | string
  events: number
  first_ts: number
  last_ts: number
  reason: string
  tools: string[]
  status: 'open' | 'acknowledged' | 'resolved' | string
  handled_by: string | null
  handled_at: number | null
}

/** Admin-only. The whole 7-day window, unfiltered and unpaginated — the
 *  caller does its own Open/All filtering. Cached ~8s server-side. */
export const getAlerts = () => api.get<{ alerts: Alert[] }>('/admin/alerts')

/** Acknowledges or resolves one incident. Neither needs a request body;
 *  resolving is also a side effect of the two `setConsumerStatus`/
 *  `rotateConsumerKey` containment actions, called separately from there. */
export const actOnAlert = (id: string, action: 'ack' | 'resolve') =>
  api.post<{ ok: boolean; id: string; status: string }>(`/admin/alerts/${encodeURIComponent(id)}/${action}`, {})

// ── Admin: monitor ──────────────────────────────────────────────────────────
// Live security overview (analytics.overview()), the raw recent-calls feed
// that overview is computed from, and the durable policy-change log (signups,
// key rotations, category/department/control edits) — all read-only.

export type MonitorRange = '1h' | '24h' | '7d' | '30d'

/** Hours to hand `exportAuditWindow` for the currently selected range — the
 *  export always covers "since now minus this many hours," same as the KPIs
 *  above it. */
export const MONITOR_RANGE_HOURS: Record<MonitorRange, number> = { '1h': 1, '24h': 24, '7d': 168, '30d': 720 }

export interface OverviewKpis {
  total: number
  ok: number
  denied: number
  error: number
  suspicious: number
  redactions: number
  sensitive: number
  rows: number
  avg_latency: number
  auth_rate: number
  denied_rate: number
}

/** One time bucket of `overview.series` — hourly buckets under a 24h range,
 *  daily otherwise (analytics.py's `_series`). */
export interface OverviewSeriesBucket {
  ok: number
  denied: number
  error: number
  label: string
}

/** A generic (label, count) pair — `by_department`/`by_tool`/`by_user` all
 *  share this shape; the latter two are already trimmed to the top 8 server-side. */
export interface OverviewCount {
  k: string
  n: number
}

export interface OverviewHeatmap {
  /** `grid[weekday][hour]`, weekday 0 = Monday. */
  grid: number[][]
  max: number
}

export interface Overview {
  range_sec: number
  kpis: OverviewKpis
  series: OverviewSeriesBucket[]
  by_department: OverviewCount[]
  by_tool: OverviewCount[]
  by_user: OverviewCount[]
  heatmap: OverviewHeatmap
}

/** Admin-only. Cached ~15s server-side, so switching range and switching back
 *  within that window is free. */
export const getOverview = (range: MonitorRange) => api.get<Overview>(`/admin/overview?range=${range}`)

/** One audited call, as the admin monitor sees it — wider than `GovernedCall`
 *  (a caller's view of its own activity): includes the fields only a full
 *  investigation needs (governance_core/audit.py's `log_call`). */
export interface AdminCall {
  ts: number
  consumer: string
  tool: string
  status: string
  rows?: number | null
  latency_ms: number | null
  detail: string | null
  suspicious?: boolean
  reasons?: string[]
  customer_id: string | null
  session_id: string | null
  request_id: string | null
  client_ip: string | null
  /** Free-text summary of the call's arguments, already redacted server-side. */
  args: string
}

/** Admin sees every principal's calls; capped at `limit` (most recent first),
 *  same 1000-event ring buffer the KPIs above read from. */
export const getAdminCalls = (limit = 200) => api.get<{ calls: AdminCall[] }>(`/admin/calls?limit=${limit}`)

/** One durable policy-change event — a signup, key rotation, or admin edit
 *  (governance_core/audit.py's `log_policy_change`). `tool` here is really
 *  `"<action> <target>"`, not a governed tool name. */
export interface PolicyChange {
  ts: number
  consumer: string
  tool: string
  detail: string | null
}

export const getPolicyChanges = () => api.get<{ changes: PolicyChange[] }>('/admin/policy-changes')

export interface AuditExportArtifact {
  artifactId: string
  filename: string
  downloadUrl: string
}

export interface AuditExportResult {
  artifact: AuditExportArtifact
  summary: { total: number; byType: Record<string, number>; suspicious: number }
  filters: { hours: number; type: string; consumer: string; limit: number }
}

/** Snapshots the last `hours` of audit events (every type, not just calls)
 *  into a downloadable artifact. 5000-record cap matches the legacy panel's
 *  default. */
export const exportAuditWindow = (hours: number, limit = 5000) =>
  api.post<AuditExportResult>('/admin/audit-export', { hours, type: 'all', limit })

// ── Admin: IP allowlist ─────────────────────────────────────────────────────
// One global CIDR list, enforced only on `/mcp` traffic (edge.py) — the
// dashboard itself authenticates by session cookie and is unaffected. An
// EMPTY list means every IP is allowed; it is not a locked-down default.
// There is no per-entry metadata (no label, no added-by/when) and no
// server-side format validation — a malformed entry is silently accepted and
// simply never matches anything at enforcement time.

/** Whole-list read. */
export const getWhitelist = () => api.get<{ whitelist: string[] }>('/admin/whitelist')

/** Whole-list replace. 409s (as an ApiError) when the policy store is
 *  read-only. Returns the saved list, so the caller can trust the response
 *  over its own draft. */
export const setWhitelist = (cidrs: string[]) =>
  api.put<{ ok: boolean; whitelist: string[] }>('/admin/whitelist', { cidrs })

export interface LoginResult extends Me {
  ok: boolean
}

/** Sets the session cookie on success; 401 on bad credentials, 429 when locked out
 *  (`ApiError.isRateLimited`) after too many failures for that username+IP. */
export const login = (username: string, password: string) =>
  api.post<LoginResult>('/dashboard/login', { username, password })

export const logout = () => api.post<{ ok: boolean }>('/dashboard/logout')

export interface Department {
  id: string
  display_name: string
  categories: string[]
}

/** Public — no session required, so the signup form can populate its
 *  department picker before the visitor has an account. */
export const getDepartments = () => api.get<{ departments: Department[] }>('/dashboard/departments')

export interface SignupResult {
  ok: boolean
  status: string
  name: string
}

/** Creates an active account and signs it in immediately (no email
 *  verification, no admin approval gate on the account itself) — sets the
 *  session cookie the same as `login`. 400 for a missing field or an unknown
 *  department, 409 for a taken username or a read-only policy store. */
export const signup = (input: { full_name: string; username: string; password: string; department: string }) =>
  api.post<SignupResult>('/dashboard/signup', input)

// ── Knowledge base ───────────────────────────────────────────────────────────
// Read-only end to end (governance_core/policy/manifest.py: "Deliberately no
// ingest_knowledge_text/ingest_knowledge_file entries"). New documents arrive
// through AraTestEnvBE's own ingestion pipeline into the shared knowledge
// base, never through this gateway — so there is no upload/ingest client here,
// only list/view/delete of what's already indexed, plus search and
// citation-backed answers.

export interface KnowledgeDocument {
  documentId: string
  owner: string
  title: string
  filename: string
  sourceType: string
  classification: string[]
  createdAt: number
  updatedAt: number
  chunkCount: number
  checksum: string
  metadata: Record<string, unknown>
}

/** `all` only takes effect for an admin caller — a non-admin passing it gets
 *  silently scoped back to their own documents (backend/knowledge.py). */
export const listKnowledgeDocuments = (all = false) =>
  api.get<{ documents: KnowledgeDocument[] }>(`/knowledge/documents${all ? '?all=1' : ''}`)

export const getKnowledgeDocument = (id: string) =>
  api.get<{ document: KnowledgeDocument }>(`/knowledge/documents/${encodeURIComponent(id)}`)

/** 404s — not 403 — for someone else's document if you're not an admin; the
 *  backend won't even confirm it exists. */
export const deleteKnowledgeDocument = (id: string) =>
  api.del<{ ok: boolean }>(`/knowledge/documents/${encodeURIComponent(id)}`)

export interface KnowledgeSearchHit {
  chunkId: string
  documentId: string
  ordinal: number
  text: string
  score: number
  documentTitle: string
  filename: string
  classification: string[]
}

/** The live mcp-knowledge backend (Azure AI Search in production) wraps hits
 *  as `{results: [...]}`; the local dev fallback store returns a bare array.
 *  Normalised here so the page never has to guess which one answered. */
export const searchKnowledge = async (
  query: string,
  limit = 5,
  documentId = '',
): Promise<KnowledgeSearchHit[]> => {
  const body: Record<string, unknown> = { query, limit }
  if (documentId) body.document_id = documentId
  const result = await api.post<{ results?: KnowledgeSearchHit[] } | KnowledgeSearchHit[]>(
    '/knowledge/search',
    body,
  )
  return Array.isArray(result) ? result : (result.results ?? [])
}

export interface KnowledgeCitation {
  documentId: string
  chunkId: string
  documentTitle: string
  score: number
}

export interface KnowledgeAnswer {
  answer: string
  citations: KnowledgeCitation[]
}

export const answerFromKnowledge = (query: string, limit = 5, documentId = '') => {
  const body: Record<string, unknown> = { query, limit }
  if (documentId) body.document_id = documentId
  return api.post<KnowledgeAnswer>('/knowledge/answer', body)
}

// ── Personal knowledge tier ──────────────────────────────────────────────────
// Private to the uploading user, no admin bypass — separate MCP tools and a
// separate category grant from the company tier above (governance_core/
// policy/categories.py's "personal_knowledge"). A 403 here most likely means
// the signed-in user simply hasn't been granted that category yet, not a bug.

/** Same shape as KnowledgeDocument — a personal doc has no extra fields. */
export type MyDocument = KnowledgeDocument

export const listMyDocuments = () => api.get<{ documents: MyDocument[] }>('/knowledge/mine')

/** Multipart upload — deliberately bypasses api.post's JSON body path (passing
 *  `body: undefined` up front so `request()` never sets a JSON Content-Type,
 *  letting the browser set the multipart boundary itself). */
export const uploadMyDocument = (file: File, title?: string) => {
  const form = new FormData()
  form.append('file', file, file.name)
  if (title) form.append('title', title)
  return api.post<{ document: MyDocument }>('/knowledge/mine', undefined, { body: form })
}

export const deleteMyDocument = (id: string) =>
  api.del<{ documentId: string; deleted: boolean }>(`/knowledge/mine/${encodeURIComponent(id)}`)

export const searchMyDocuments = async (
  query: string,
  limit = 5,
  documentId = '',
): Promise<KnowledgeSearchHit[]> => {
  const body: Record<string, unknown> = { query, limit }
  if (documentId) body.document_id = documentId
  const result = await api.post<{ results?: KnowledgeSearchHit[] }>('/knowledge/mine/search', body)
  return result.results ?? []
}

// ── Workflows ────────────────────────────────────────────────────────────────
// A "workflow" is either one of the 5 hardcoded templates (gateway/workflows.py)
// or a published user-built graph (see Workflow graphs, below) — both project
// into this same WorkflowTemplate shape server-side, so one client type and one
// set of run/preflight/run-history endpoints cover both.

export interface WorkflowTemplate {
  templateId: string
  displayName: string
  description: string
  requiredCategories: string[]
  outputTypes: string[]
  status: 'active' | 'disabled' | string
  version: number
  disabledBy?: string
  disabledAt?: number | null
  disabledReason?: string
}

export const listWorkflows = () => api.get<{ workflows: WorkflowTemplate[] }>('/workflows')

/** One input a template declares as user-fillable (backend/workflow_api.py's
 *  `_workflow_input_requirements`) — a graph-backed template's trigger node
 *  carries the same shape. This is what makes the run/schedule form show only
 *  the fields a given workflow actually uses, instead of one static form with
 *  every field for every workflow. */
export interface WorkflowInputRequirement {
  name: string
  label: string
  sampleDefault?: string
  /** Only enforced once `sample` is false — every field is optional in sample
   *  mode. Read only for the 5 hardcoded templates; a graph-backed workflow's
   *  trigger inputs have no sample concept at all, so `optional` is what
   *  gates them instead (see `_workflow_preflight_for`'s `is_graph` branch). */
  requiredWhenSampleFalse?: boolean
  optional?: boolean
}

/** An optional approval pause this template supports, and whether the current
 *  draft inputs have it turned on — echoes back what was just sent, not an
 *  independent toggle the preflight call itself can flip. */
export interface WorkflowApprovalGate {
  id: string
  label: string
  enabled: boolean
}

export interface WorkflowConnector {
  category: string
  backend: string
  configured: boolean
  paused?: boolean
  url?: string
  error?: string
}

/** A dry run of "would this actually work right now" — missing access,
 *  missing required inputs, paused backends — rendered as plain sentences
 *  rather than the pill wall the legacy panel used. */
export interface WorkflowPreflight {
  ready: boolean
  templateId: string
  requiredCategories: string[]
  effectiveCategories: string[]
  legacyAllowAll: boolean
  pausedBackends: string[]
  missingCategories: string[]
  requiredInputs: WorkflowInputRequirement[]
  missingInputs: string[]
  approvalGates: WorkflowApprovalGate[]
  connectors: WorkflowConnector[]
  blockers: string[]
  warnings: string[]
}

/** `inputs` is sent as the flat request body (not wrapped) — the gateway
 *  accepts either shape but every other caller (run, automations) uses flat,
 *  so preflight matches for a single mental model of "what a workflow's
 *  inputs look like on the wire." */
export const getWorkflowPreflight = (templateId: string, inputs: Record<string, unknown>) =>
  api.post<WorkflowPreflight>(`/workflows/${encodeURIComponent(templateId)}/preflight`, inputs)

export interface WorkflowRunArtifact {
  artifactId: string
  filename: string
  classification?: string[]
  downloadUrl?: string
}

export interface WorkflowRunStep {
  stepId: string
  type: string
  status: string
  tool: string
  title: string
  inputs: Record<string, unknown>
  outputs: Record<string, unknown>
  error: string | null
}

export interface WorkflowRun {
  runId: string
  templateId: string
  requestedBy: string
  status: string
  inputs: Record<string, unknown>
  steps: WorkflowRunStep[]
  artifactIds: string[]
  approvalIds: string[]
  createdAt: number
  updatedAt: number
  error: string | null
  resumedAt: number | null
}

export interface WorkflowRunResult extends WorkflowRun {
  artifacts?: WorkflowRunArtifact[]
  approval?: { approvalId: string }
}

export const runWorkflow = (templateId: string, inputs: Record<string, unknown>) =>
  api.post<WorkflowRunResult>(`/workflows/${encodeURIComponent(templateId)}/run`, inputs)

/** `all` (admin-only; ignored for anyone else) lists every principal's runs
 *  instead of just this caller's own. */
export const listWorkflowRuns = (all = false) =>
  api.get<{ runs: WorkflowRun[] }>(`/workflow-runs${all ? '?all=1' : ''}`)

export interface WorkflowTimelineEvent {
  kind: 'step' | 'audit' | 'policy' | 'artifact' | 'approval' | string
  ts: number | null
  status: string | null
  title: string
  tool: string
  detail: string
}

export const getWorkflowRun = (runId: string, timeline = false) =>
  api.get<WorkflowRun & { timeline?: WorkflowTimelineEvent[] }>(
    `/workflow-runs/${encodeURIComponent(runId)}${timeline ? '?timeline=1' : ''}`,
  )

export const cancelWorkflowRun = (runId: string, reason: string) =>
  api.post<WorkflowRun>(`/workflow-runs/${encodeURIComponent(runId)}/cancel`, { reason })

/** Resumes a run paused on an approval gate once that approval has been
 *  decided. 409s (as an ApiError) if it's still pending or was denied. */
export const resumeWorkflowRun = (runId: string, approvalId: string) =>
  api.post<WorkflowRun>(`/workflow-runs/${encodeURIComponent(runId)}/resume`, { approval_id: approvalId })

export const exportWorkflowRunEvidence = (runId: string) =>
  api.post<{ artifact: WorkflowRunArtifact; timelineEvents: number }>(
    `/workflow-runs/${encodeURIComponent(runId)}/export-evidence`,
  )

export interface WorkflowHealthTemplate {
  templateId: string
  displayName: string
  runs: number
  completed: number
  failed: number
  approvalRequired: number
  lastStatus: string
  failureRate: number
  artifactRate: number
  avgDurationSec: number
}

export interface WorkflowHealth {
  health: 'healthy' | 'degraded' | 'unhealthy'
  totalRuns: number
  failedRuns: number
  approvalRequiredRuns: number
  failureRate: number
  approvalRate: number
  avgDurationSec: number
  stuckRuns: WorkflowRun[]
  templates: WorkflowHealthTemplate[]
}

/** Admin-only — run-quality KPIs derived from persisted workflow runs. */
export const getWorkflowHealth = () => api.get<WorkflowHealth>('/admin/workflow-health')

/** Admin-only. Disabling asks for a reason (audited); enabling doesn't. */
export const setWorkflowTemplateStatus = (
  templateId: string,
  action: 'enable' | 'disable',
  reason = '',
) => api.post<WorkflowTemplate>(`/admin/workflows/${encodeURIComponent(templateId)}/${action}`, { reason })

// ── Automations ──────────────────────────────────────────────────────────────
// A schedule that re-runs a published, active workflow on an interval — the
// same `/workflows/{id}/run` execution path, just triggered by time instead
// of a click.

export interface Automation {
  automationId: string
  owner: string
  templateId: string
  actorType: 'user' | 'agent'
  agentId: string
  displayName: string
  inputs: Record<string, unknown>
  intervalSec: number
  status: 'active' | 'disabled' | string
  createdAt: number
  updatedAt: number
  nextRunAt: number
  lastRunAt: number | null
  lastRunId: string
  lastStatus: string
  runCount: number
}

/** Admin sees every principal's schedules by default; a non-admin only ever
 *  sees their own regardless. */
export const listAutomations = () => api.get<{ automations: Automation[] }>('/automations')

export interface CreateAutomationInput {
  template_id: string
  display_name: string
  inputs: Record<string, unknown>
  /** Seconds between runs — the wire unit; the UI works in a plain
   *  Daily/Weekly/Custom schedule and converts before sending. */
  interval_sec: number
  next_run_at?: number
}

export const createAutomation = (input: CreateAutomationInput) =>
  api.post<Automation>('/automations', input)

export const deleteAutomation = (automationId: string) =>
  api.del<{ ok: boolean }>(`/automations/${encodeURIComponent(automationId)}`)

/** Admin-only — runs every automation currently due. Local/manual trigger for
 *  a deployment without its own external scheduler calling this on a timer. */
export const runDueAutomations = () =>
  api.post<{ ran: number; results: Array<{ automationId: string; status: string }> }>('/automations/run-due', {})

// ── Workflow graphs ("My Workflow") ─────────────────────────────────────────
// A user-buildable workflow: an ordered chain of steps (tool calls, an AI
// step, an approval gate) wired trigger -> step -> step -> .... Node/edge
// naming matches the backend's GraphNode/GraphEdge wire shape exactly
// (governance_core/workflow_graph_models.py) so a saved graph round-trips
// without translation.

/** Where one step's input value comes from: a literal typed value, the
 *  trigger's own raw inputs by key, or an earlier node's output by key
 *  (gateway/workflow_graph_interpreter.py's `_resolve_binding`). */
export type WorkflowBinding =
  | { source: 'literal'; value: unknown }
  | { source: 'trigger'; path: string }
  | { source: 'node'; node_id: string; path: string }

export type GraphNodeKind = 'trigger' | 'tool_call' | 'approval_gate' | 'llm_transform' | 'filter'

/** One leaf test in a filter node's condition tree, or a nested `all`/`any`
 *  group of them — mirrors `workflow_graph_store.FILTER_OPS` and the recursive
 *  shape `workflow_graph_interpreter.py::_evaluate_condition_tree` walks. */
export type FilterCondition =
  | { all: FilterCondition[] }
  | { any: FilterCondition[] }
  | { field: string; op: string; value: unknown }

export interface GraphNode {
  nodeId: string
  kind: GraphNodeKind
  title: string
  /** Canonical tool name; set iff `kind === 'tool_call'`. */
  tool: string
  /** Literal, non-bindable settings — an approval's reason/risk level, an AI
   *  step's instruction, a trigger's declared input list. */
  config: Record<string, unknown>
  inputBindings: Record<string, WorkflowBinding>
  /** UI-only; the interpreter ignores this. Unused by the linear builder
   *  (kept only so a graph built here still opens if ever inspected by a
   *  hypothetical future canvas view). */
  position?: { x?: number; y?: number }
}

export interface GraphEdge {
  edgeId: string
  sourceNodeId: string
  targetNodeId: string
}

export interface WorkflowGraph {
  graphId: string
  displayName: string
  description: string
  owner: string
  status: 'draft' | 'active' | 'disabled' | string
  publishedVersion: number
  currentVersion: number
  createdAt: number
  updatedAt: number
  /** Present on create, on a single-graph fetch, and on add-version — absent
   *  on the list endpoint. */
  nodes?: GraphNode[]
  edges?: GraphEdge[]
}

/** Admin sees every owner's graphs by default; a non-admin only ever sees
 *  their own. List entries never include `nodes`/`edges` — open one to edit. */
export const listWorkflowGraphs = () => api.get<{ graphs: WorkflowGraph[] }>('/workflow-graphs?all=1')

export const getWorkflowGraph = (graphId: string) =>
  api.get<WorkflowGraph>(`/workflow-graphs/${encodeURIComponent(graphId)}`)

/** Owner or admin only. Hard delete — there is no "deleted" status, the
 *  graph is simply gone from `listWorkflowGraphs`/`getWorkflowGraph`
 *  afterward. An automation or past run that still names this graph's id as
 *  its template id keeps that id; nothing reaches back to clean those up. */
export const deleteWorkflowGraph = (graphId: string) =>
  api.del<{ ok: boolean }>(`/workflow-graphs/${encodeURIComponent(graphId)}`)

export interface SaveWorkflowGraphInput {
  display_name: string
  description?: string
  nodes: GraphNode[]
  edges: GraphEdge[]
  notes?: string
}

/** Creates a brand-new draft (version 1). 400s (as an ApiError) with a
 *  human-readable reason if the graph fails build-time validation (a tool
 *  the owner can't reach, a send-risk step with no approval gate before it,
 *  etc.) — the message is exactly what `validateWorkflowGraph` would have
 *  reported. */
export const createWorkflowGraph = (input: SaveWorkflowGraphInput) =>
  api.post<WorkflowGraph>('/workflow-graphs', input)

/** Adds a new version to an existing graph (bumps `currentVersion`); does not
 *  publish it. Same validation as create. */
export const addWorkflowGraphVersion = (graphId: string, input: SaveWorkflowGraphInput) =>
  api.post<WorkflowGraph>(`/workflow-graphs/${encodeURIComponent(graphId)}/versions`, input)

/** Publishes a version (defaults to the current one), making the graph a
 *  runnable workflow under `graphId` as its template id. Response never
 *  includes `nodes`/`edges`. */
export const publishWorkflowGraph = (graphId: string, version?: number) =>
  api.post<WorkflowGraph>(`/workflow-graphs/${encodeURIComponent(graphId)}/publish`, version ? { version } : {})

export interface WorkflowGraphValidation {
  ready: boolean
  blockers: string[]
  warnings: string[]
}

/** Dry-run validation against a draft `nodes`/`edges` before saving — same
 *  checks `createWorkflowGraph`/`addWorkflowGraphVersion` enforce, but never
 *  writes anything. */
export const validateWorkflowGraph = (graphId: string, nodes: GraphNode[], edges: GraphEdge[]) =>
  api.post<WorkflowGraphValidation>(`/workflow-graphs/${encodeURIComponent(graphId)}/validate`, { nodes, edges })

/** One tool on the builder's palette, scoped to the caller's own grant
 *  (`granted: false` greys it out — a UX hint, not the security boundary;
 *  the live governed call is what actually enforces access). */
export interface WorkflowGraphCatalogTool {
  name: string
  canonical: string
  description: string
  riskLevel: string | null
  approvalRequired: boolean
  backend: string | null
  granted: boolean
  parameters: {
    type: 'object'
    properties?: Record<string, { type?: string; description?: string; enum?: unknown[] }>
    required?: string[]
  }
  outputFields: string[]
}

export const getWorkflowGraphCatalog = () =>
  api.get<{ tools: WorkflowGraphCatalogTool[] }>('/dashboard/workflow-graph-catalog')
