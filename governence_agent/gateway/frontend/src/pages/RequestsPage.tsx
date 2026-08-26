import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Badge,
  Button,
  Card,
  DataTable,
  Drawer,
  EmptyState,
  KeyValue,
  PageShell,
  useToast,
  type BadgeTone,
  type Column,
} from '../components/ui'
import {
  ApiError,
  approveAdminRequest,
  denyAdminRequest,
  getAccessSuggestions,
  getAdminRequests,
  getCategoryCatalog,
  type AccessSuggestion,
  type AdminAccessRequest,
  type CategoryCatalogEntry,
} from '../lib/api'
import { describeRequest } from '../lib/describeRequest'
import { formatRelative, formatWhen } from '../lib/format'
import type { PageProps } from './types'
import './RequestsPage.css'

/**
 * Access Requests — ports `renderRequests()`: pending signup/access-request
 * decisions, plus the read-only "who keeps getting denied" suggestions list.
 *
 * Two changes from the legacy panel:
 *   - the Detail column now resolves category/tool ids through the catalog
 *     via `describeRequest` (shared with AccessPage's own request history),
 *     so an admin reads "Order lookups: Get order details" instead of
 *     "orders_read: get_order".
 *   - Deny opened a bare `confirm('Deny request?')`, easy to blow through by
 *     habit since Approve needs no confirmation at all. It's now a Drawer
 *     that shows exactly what's being denied — including, for a signup
 *     request, that the account gets disabled too.
 */

function kindTone(kind: string): BadgeTone {
  return kind === 'account' ? 'accent' : 'neutral'
}

function RequestsPage(_props: PageProps) {
  const toast = useToast()
  const [requests, setRequests] = useState<AdminAccessRequest[]>([])
  const [suggestions, setSuggestions] = useState<AccessSuggestion[]>([])
  const [categories, setCategories] = useState<CategoryCatalogEntry[]>([])
  const [state, setState] = useState<'loading' | 'ready' | 'error'>('loading')
  const [error, setError] = useState<string | null>(null)

  // Per-row Approve spinner. Deny has no busy state of its own here — its
  // button just opens the confirm drawer, which owns `submitting`.
  const [approving, setApproving] = useState<string | null>(null)
  const [denyTarget, setDenyTarget] = useState<AdminAccessRequest | null>(null)
  const [denying, setDenying] = useState(false)

  const load = useCallback(() => {
    let live = true
    setState('loading')

    Promise.all([
      getAdminRequests('pending'),
      getAccessSuggestions().catch(() => ({ suggestions: [] })),
      getCategoryCatalog().catch(() => ({ categories: [] })),
    ])
      .then(([reqs, sugg, catalog]) => {
        if (!live) return
        setRequests(reqs.requests ?? [])
        setSuggestions(sugg.suggestions ?? [])
        setCategories(catalog.categories ?? [])
        setError(null)
        setState('ready')
      })
      .catch((cause: unknown) => {
        if (!live) return
        setError(cause instanceof Error ? cause.message : 'Could not load access requests.')
        setState('error')
      })

    return () => {
      live = false
    }
  }, [])

  useEffect(() => load(), [load])

  const approve = useCallback(
    async (request: AdminAccessRequest) => {
      setApproving(request.id)
      try {
        await approveAdminRequest(request.id)
        toast.success(request.kind === 'workflow' ? 'Marked acknowledged' : 'Request approved')
        setRequests((prev) => prev.filter((r) => r.id !== request.id))
      } catch (cause) {
        toast.error(cause instanceof ApiError ? cause.message : 'Could not approve that request')
      } finally {
        setApproving(null)
      }
    },
    [toast],
  )

  const confirmDeny = useCallback(async () => {
    if (!denyTarget) return
    setDenying(true)
    try {
      await denyAdminRequest(denyTarget.id)
      toast.success(denyTarget.kind === 'workflow' ? 'Dismissed' : 'Request denied')
      setRequests((prev) => prev.filter((r) => r.id !== denyTarget.id))
      setDenyTarget(null)
    } catch (cause) {
      toast.error(cause instanceof ApiError ? cause.message : 'Could not deny that request')
    } finally {
      setDenying(false)
    }
  }, [denyTarget, toast])

  const requestColumns = useMemo<Column<AdminAccessRequest>[]>(
    () => [
      {
        key: 'kind',
        header: 'Kind',
        width: '7rem',
        render: (r) => (
          <Badge tone={kindTone(r.kind)} subtle>
            {r.kind}
          </Badge>
        ),
      },
      {
        key: 'who',
        header: 'Who',
        width: '10rem',
        render: (r) => r.username || r.consumer_id || '—',
      },
      {
        key: 'detail',
        header: 'Detail',
        render: (r) => (
          <>
            <div>{describeRequest(r, categories)}</div>
            {r.justification && <div className="requests-justification">{r.justification}</div>}
          </>
        ),
      },
      {
        key: 'when',
        header: 'Asked',
        width: '9rem',
        muted: true,
        nowrap: true,
        render: (r) => <span title={formatWhen(r.created_at)}>{formatRelative(r.created_at)}</span>,
      },
      {
        key: 'action',
        header: '',
        srHeader: 'Decide',
        width: '11rem',
        align: 'right',
        render: (r) => (
          <div className="requests-row-actions">
            <Button size="sm" onClick={() => approve(r)} loading={approving === r.id}>
              Approve
            </Button>
            <Button size="sm" variant="danger" onClick={() => setDenyTarget(r)} disabled={approving === r.id}>
              Deny
            </Button>
          </div>
        ),
      },
    ],
    [categories, approve, approving],
  )

  // Workflow requests ("I can't build this myself yet") are a DIFFERENT kind of
  // ask from an access/signup decision -- approving one doesn't grant anything,
  // it just marks the ask reviewed (admin_policy.py's kind=="workflow" branch),
  // so they get their own section/labels rather than living in the grant queue.
  const pendingOther = useMemo(() => requests.filter((r) => r.kind !== 'workflow'), [requests])
  const workflowRequests = useMemo(() => requests.filter((r) => r.kind === 'workflow'), [requests])

  const workflowColumns = useMemo<Column<AdminAccessRequest>[]>(
    () => [
      { key: 'who', header: 'Who', width: '10rem', render: (r) => r.username || r.consumer_id || '—' },
      { key: 'ask', header: 'What they asked for', render: (r) => r.justification || '—' },
      {
        key: 'when', header: 'Asked', width: '9rem', muted: true, nowrap: true,
        render: (r) => <span title={formatWhen(r.created_at)}>{formatRelative(r.created_at)}</span>,
      },
      {
        key: 'action', header: '', srHeader: 'Decide', width: '11rem', align: 'right',
        render: (r) => (
          <div className="requests-row-actions">
            <Button size="sm" onClick={() => approve(r)} loading={approving === r.id}>
              Acknowledge
            </Button>
            <Button size="sm" variant="danger" onClick={() => setDenyTarget(r)} disabled={approving === r.id}>
              Dismiss
            </Button>
          </div>
        ),
      },
    ],
    [approve, approving],
  )

  const suggestionColumns = useMemo<Column<AccessSuggestion>[]>(
    () => [
      { key: 'consumer', header: 'Consumer' },
      { key: 'tool', header: 'Tool attempted', mono: true },
      { key: 'attempts', header: 'Attempts', width: '7rem', numeric: true },
    ],
    [],
  )

  return (
    <PageShell className="requests">
      <Card
        title="Pending requests"
        description="Account signups and access asks — approve to grant them, deny to close them out."
        flush
        actions={
          <Button variant="quiet" size="sm" onClick={load} loading={state === 'loading'}>
            Refresh
          </Button>
        }
      >
        <DataTable
          columns={requestColumns}
          rows={pendingOther}
          rowKey={(r) => r.id}
          loading={state === 'loading'}
          skeletonRows={4}
          caption="Pending access and signup requests"
          empty={
            state === 'error' ? (
              <EmptyState
                title="Couldn't load requests"
                description={error ?? undefined}
                action={
                  <Button size="sm" variant="ghost" onClick={load}>
                    Try again
                  </Button>
                }
              />
            ) : (
              <EmptyState title="No pending requests" description="Nothing is waiting on a decision." />
            )
          }
        />
      </Card>

      <Card
        title="Workflow requests"
        description="Things people asked the assistant (or typed directly) that we can't build yet — a report needing custom logic, a saved/recurring version, or data we don't expose. Acknowledge to mark it seen; the actual build is separate work."
        flush
      >
        <DataTable
          columns={workflowColumns}
          rows={workflowRequests}
          rowKey={(r) => r.id}
          loading={state === 'loading'}
          skeletonRows={2}
          caption="Pending workflow/report requests"
          empty={<EmptyState title="No workflow requests" description="Nobody's hit a capability gap recently." compact />}
        />
      </Card>

      <Card
        title="Access suggestions"
        description="Aggregated from denied “not granted” attempts — repeat attempts likely mean a grant is missing."
        flush
      >
        <DataTable
          columns={suggestionColumns}
          rows={suggestions}
          rowKey={(s, i) => `${s.consumer}:${s.tool}:${i}`}
          loading={state === 'loading'}
          skeletonRows={3}
          dense
          caption="Tools repeatedly denied for missing grants"
          empty={<EmptyState title="No suggestions" description="Nothing's been denied for a missing grant recently." compact />}
        />
      </Card>

      <Drawer
        open={denyTarget != null}
        onClose={() => setDenyTarget(null)}
        eyebrow={denyTarget?.kind === 'workflow' ? 'Workflow request' : 'Access request'}
        title={denyTarget?.kind === 'workflow' ? 'Dismiss this request?' : 'Deny this request?'}
        footer={
          <>
            <Button variant="ghost" onClick={() => setDenyTarget(null)} disabled={denying}>
              Cancel
            </Button>
            <Button variant="danger" onClick={confirmDeny} loading={denying}>
              {denyTarget?.kind === 'workflow' ? 'Dismiss request' : 'Deny request'}
            </Button>
          </>
        }
      >
        {denyTarget && (
          <KeyValue
            items={
              denyTarget.kind === 'workflow'
                ? [
                    { label: 'Who', value: denyTarget.username || denyTarget.consumer_id },
                    { label: 'What they asked for', value: denyTarget.justification, wide: true },
                  ]
                : [
                    { label: 'Who', value: denyTarget.username || denyTarget.consumer_id },
                    { label: 'Kind', value: denyTarget.kind },
                    { label: 'Detail', value: describeRequest(denyTarget, categories), wide: true },
                    { label: 'Justification', value: denyTarget.justification, wide: true },
                    ...(denyTarget.kind === 'account'
                      ? [{ label: 'Note', value: 'Denying also disables this account.', wide: true }]
                      : []),
                  ]
            }
          />
        )}
      </Drawer>
    </PageShell>
  )
}

export default RequestsPage
