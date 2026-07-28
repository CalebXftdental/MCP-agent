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

type JsonBody = Record<string, unknown> | unknown[]

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
  if (!res.ok) throw new ApiError(res.status, payload)
  return payload as T
}

export const api = {
  get: <T>(path: string, init?: RequestInit) => request<T>('GET', path, undefined, init),
  post: <T>(path: string, body?: JsonBody, init?: RequestInit) =>
    request<T>('POST', path, body, init),
  patch: <T>(path: string, body?: JsonBody, init?: RequestInit) =>
    request<T>('PATCH', path, body, init),
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

/** One resolved tool call the assistant made this turn (orchestrator.py:326). */
export interface ChatToolCall {
  tool: string
  args: Record<string, unknown>
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

export interface LoginResult extends Me {
  ok: boolean
}

/** Sets the session cookie on success; 401 on bad credentials, 429 when locked out. */
export const login = (username: string, password: string) =>
  api.post<LoginResult>('/dashboard/login', { username, password })

export const logout = () => api.post<{ ok: boolean }>('/dashboard/logout')
