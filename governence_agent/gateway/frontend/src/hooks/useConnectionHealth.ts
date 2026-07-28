import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiError, getBackendHealth, getHealth, getMe, type BackendHealth } from '../lib/api'

/**
 * Probes every connection the gateway depends on and normalises the results into
 * groups a UI can render without knowing any endpoint shapes.
 *
 * Three sources:
 *   GET /health                 — is the gateway process answering at all
 *   GET /dashboard/me           — does this browser hold a valid session
 *   GET /admin/backends/health  — live MCP probe per backend (admin-only)
 */

export type Severity = 'ok' | 'warn' | 'down' | 'idle' | 'checking' | 'blocked'

/** Worst-wins ordering for rolling a set of rows up to one badge. */
const RANK: Record<Severity, number> = {
  down: 5,
  warn: 4,
  blocked: 3,
  checking: 2,
  idle: 1,
  ok: 0,
}

export function worstOf(severities: Severity[]): Severity {
  return severities.reduce<Severity>((worst, s) => (RANK[s] > RANK[worst] ? s : worst), 'ok')
}

export interface ConnectionRow {
  id: string
  label: string
  status: Severity
  /** Short right-aligned detail: latency, role, reason. */
  detail?: string
  /** Full untruncated text, surfaced on hover. */
  title?: string
}

export interface ConnectionGroup {
  id: string
  label: string
  /** Sub-label under the group heading, e.g. the shared MCP URL. */
  note?: string
  rows: ConnectionRow[]
}

export interface ConnectionHealth {
  groups: ConnectionGroup[]
  worst: Severity
  /** One-line rollup for the collapsed badge. */
  summary: string
  checking: boolean
  lastChecked: Date | null
  refresh: () => void
}

const BACKEND_SEVERITY: Record<BackendHealth['status'], Severity> = {
  healthy: 'ok',
  degraded: 'warn',
  down: 'down',
  idle: 'idle',
}

function reasonFor(error: unknown): { detail: string; title?: string } {
  if (!(error instanceof ApiError)) return { detail: 'failed', title: String(error) }
  if (error.isOffline) return { detail: 'unreachable', title: error.message }
  if (error.isUnauthenticated) return { detail: 'signed out' }
  if (error.isForbidden) return { detail: 'admin only' }
  return { detail: `HTTP ${error.status}`, title: error.message }
}

/** Group backends by MCP URL — several policy domains can share one server. */
function groupBackends(backends: BackendHealth[]): ConnectionGroup[] {
  const byUrl = new Map<string, BackendHealth[]>()
  for (const b of backends) {
    const key = b.url ?? 'unconfigured'
    const bucket = byUrl.get(key)
    if (bucket) bucket.push(b)
    else byUrl.set(key, [b])
  }

  return [...byUrl.entries()].map(([url, group]) => ({
    id: `backend:${url}`,
    label: url === 'unconfigured' ? 'No URL configured' : url.replace(/^https?:\/\//, ''),
    note:
      group.length > 1
        ? `${group.length} policy domains on one server`
        : undefined,
    rows: group.map((b) => ({
      id: b.backend,
      label: b.backend,
      status: BACKEND_SEVERITY[b.status],
      detail:
        b.probe_latency_ms != null
          ? `${Math.round(b.probe_latency_ms)} ms`
          : b.probe_error
            ? 'no answer'
            : b.status,
      title: b.probe_error ?? b.last_error ?? undefined,
    })),
  }))
}

/**
 * @param pollMs re-probe on this interval; pass null to probe only on mount and
 *               on explicit refresh (e.g. only poll while a panel is open).
 */
export function useConnectionHealth(pollMs: number | null = null): ConnectionHealth {
  const [gateway, setGateway] = useState<ConnectionRow>({
    id: 'gateway',
    label: 'Gateway',
    status: 'checking',
  })
  const [session, setSession] = useState<ConnectionRow>({
    id: 'session',
    label: 'Session',
    status: 'checking',
  })
  const [backends, setBackends] = useState<ConnectionGroup[] | ConnectionRow>({
    id: 'backends',
    label: 'MCP backends',
    status: 'checking',
  })
  const [checking, setChecking] = useState(true)
  const [lastChecked, setLastChecked] = useState<Date | null>(null)

  // Survives unmount mid-flight so a late response can't setState on a dead component.
  const alive = useRef(true)
  useEffect(() => {
    alive.current = true
    return () => {
      alive.current = false
    }
  }, [])

  const refresh = useCallback(() => {
    setChecking(true)

    const probes = [
      getHealth()
        .then((h) => {
          if (alive.current) {
            setGateway({ id: 'gateway', label: 'Gateway', status: 'ok', detail: h.status })
          }
        })
        .catch((e) => {
          if (alive.current) {
            setGateway({ id: 'gateway', label: 'Gateway', status: 'down', ...reasonFor(e) })
          }
        }),

      getMe()
        .then((me) => {
          if (alive.current) {
            setSession({
              id: 'session',
              label: 'Session',
              status: 'ok',
              detail: `${me.name} · ${me.role}`,
            })
          }
        })
        .catch((e) => {
          if (alive.current) {
            const offline = e instanceof ApiError && e.isOffline
            setSession({
              id: 'session',
              label: 'Session',
              // Signed out is a normal state, not a failure of the connection.
              status: offline ? 'down' : 'blocked',
              ...reasonFor(e),
            })
          }
        }),

      getBackendHealth()
        .then((res) => {
          if (alive.current) setBackends(groupBackends(res.backends))
        })
        .catch((e) => {
          if (alive.current) {
            setBackends({
              id: 'backends',
              label: 'MCP backends',
              status: 'blocked',
              ...reasonFor(e),
            })
          }
        }),
    ]

    void Promise.allSettled(probes).then(() => {
      if (!alive.current) return
      setChecking(false)
      setLastChecked(new Date())
    })
  }, [])

  useEffect(refresh, [refresh])

  useEffect(() => {
    if (pollMs == null) return
    const id = window.setInterval(refresh, pollMs)
    return () => window.clearInterval(id)
  }, [pollMs, refresh])

  const backendGroups: ConnectionGroup[] = Array.isArray(backends)
    ? backends
    : [{ id: 'backends', label: 'MCP backends', rows: [backends] }]

  const groups: ConnectionGroup[] = [
    { id: 'gateway', label: 'Gateway', rows: [gateway, session] },
    ...backendGroups,
  ]

  const backendRows = backendGroups.flatMap((g) => g.rows)
  const worst = worstOf(groups.flatMap((g) => g.rows).map((r) => r.status))

  let summary: string
  if (gateway.status === 'down') {
    summary = 'Gateway offline'
  } else if (checking && !lastChecked) {
    summary = 'Checking…'
  } else {
    const down = backendRows.filter((r) => r.status === 'down').length
    const degraded = backendRows.filter((r) => r.status === 'warn').length
    if (down && degraded) summary = `${down} down · ${degraded} degraded`
    else if (down) summary = `${down} of ${backendRows.length} down`
    else if (degraded) summary = `${degraded} degraded`
    else if (!Array.isArray(backends)) summary = 'Gateway ok'
    else summary = `All ${backendRows.length} connected`
  }

  return { groups, worst, summary, checking, lastChecked, refresh }
}
