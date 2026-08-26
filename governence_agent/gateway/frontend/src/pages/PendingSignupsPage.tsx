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
  type Column,
} from '../components/ui'
import { ApiError, approveAdminRequest, denyAdminRequest, getAdminRequests, type AdminAccessRequest } from '../lib/api'
import { formatRelative, formatWhen } from '../lib/format'
import type { PageProps } from './types'
import './RequestsPage.css'

/**
 * Pending Signups — a dedicated approval queue for self-service dashboard
 * signups (kind="account" requests), separate from the general Access
 * Requests page even though both call the same `/admin/requests*` endpoints
 * and admin_policy.py's `_admin_request_approve`/`_admin_request_deny`
 * already branch on kind="account" to flip the consumer pending -> active
 * (approve) or pending -> disabled (deny). Kept apart so a new-hire waiting
 * to get in isn't buried in the same queue as routine tool-access asks.
 */

function PendingSignupsPage(_props: PageProps) {
  const toast = useToast()
  const [requests, setRequests] = useState<AdminAccessRequest[]>([])
  const [state, setState] = useState<'loading' | 'ready' | 'error'>('loading')
  const [error, setError] = useState<string | null>(null)

  const [approving, setApproving] = useState<string | null>(null)
  const [denyTarget, setDenyTarget] = useState<AdminAccessRequest | null>(null)
  const [denying, setDenying] = useState(false)

  const load = useCallback(() => {
    let live = true
    setState('loading')
    getAdminRequests('pending')
      .then((result) => {
        if (!live) return
        setRequests((result.requests ?? []).filter((r) => r.kind === 'account'))
        setError(null)
        setState('ready')
      })
      .catch((cause: unknown) => {
        if (!live) return
        setError(cause instanceof Error ? cause.message : 'Could not load pending signups.')
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
        toast.success('Account approved')
        setRequests((prev) => prev.filter((r) => r.id !== request.id))
      } catch (cause) {
        toast.error(cause instanceof ApiError ? cause.message : 'Could not approve that signup')
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
      toast.success('Signup denied')
      setRequests((prev) => prev.filter((r) => r.id !== denyTarget.id))
      setDenyTarget(null)
    } catch (cause) {
      toast.error(cause instanceof ApiError ? cause.message : 'Could not deny that signup')
    } finally {
      setDenying(false)
    }
  }, [denyTarget, toast])

  const columns = useMemo<Column<AdminAccessRequest>[]>(
    () => [
      { key: 'who', header: 'Name / username', width: '12rem', render: (r) => r.username || r.consumer_id || '—' },
      { key: 'email', header: 'Email', render: (r) => r.email || '—' },
      {
        key: 'department',
        header: 'Department',
        width: '10rem',
        render: (r) => <Badge tone="neutral" subtle>{r.department || '—'}</Badge>,
      },
      {
        key: 'when',
        header: 'Signed up',
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
    [approve, approving],
  )

  return (
    <PageShell className="requests">
      <Card
        title="Pending signups"
        description="New accounts that verified their email and are waiting on a decision — approve to let them sign in, deny to disable the account."
        flush
        actions={
          <Button variant="quiet" size="sm" onClick={load} loading={state === 'loading'}>
            Refresh
          </Button>
        }
      >
        <DataTable
          columns={columns}
          rows={requests}
          rowKey={(r) => r.id}
          loading={state === 'loading'}
          skeletonRows={4}
          caption="Pending signup approvals"
          empty={
            state === 'error' ? (
              <EmptyState
                title="Couldn't load pending signups"
                description={error ?? undefined}
                action={
                  <Button size="sm" variant="ghost" onClick={load}>
                    Try again
                  </Button>
                }
              />
            ) : (
              <EmptyState title="No pending signups" description="Nobody's waiting on account approval." />
            )
          }
        />
      </Card>

      <Drawer
        open={denyTarget != null}
        onClose={() => setDenyTarget(null)}
        eyebrow="Pending signup"
        title="Deny this signup?"
        footer={
          <>
            <Button variant="ghost" onClick={() => setDenyTarget(null)} disabled={denying}>
              Cancel
            </Button>
            <Button variant="danger" onClick={confirmDeny} loading={denying}>
              Deny signup
            </Button>
          </>
        }
      >
        {denyTarget && (
          <KeyValue
            items={[
              { label: 'Name / username', value: denyTarget.username || denyTarget.consumer_id },
              { label: 'Email', value: denyTarget.email },
              { label: 'Department', value: denyTarget.department },
              { label: 'Note', value: 'Denying also disables this account.', wide: true },
            ]}
          />
        )}
      </Drawer>
    </PageShell>
  )
}

export default PendingSignupsPage
