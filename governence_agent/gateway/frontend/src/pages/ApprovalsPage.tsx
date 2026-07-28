import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Badge,
  Button,
  Card,
  DataTable,
  Drawer,
  EmptyState,
  Field,
  KeyValue,
  Textarea,
  severityTone,
  useToast,
  type BadgeTone,
  type Column,
} from '../components/ui'
import { ApiError, apiUrl, decideApproval, getApprovals, type Approval } from '../lib/api'
import { formatRelative, formatWhen } from '../lib/format'
import type { PageProps } from './types'
import './ApprovalsPage.css'

/**
 * Approvals — ports `renderApprovals()`: the single generic queue every
 * high-impact action (external sends, workflow steps, anything gated behind
 * `approval_store`) lands in once it needs a human decision.
 *
 * The legacy panel asked for a decision note through a bare `window.prompt`,
 * which blocks the whole tab and can't show what's actually being decided
 * alongside the note. Both Approve and Deny now open a Drawer instead: the
 * full record (reason, requester, risk, artifacts) stays visible while the
 * note is typed, and the two actions are visually distinct (ok vs. danger)
 * rather than one dialog with the choice baked into which button you clicked
 * before it appeared.
 */

type DecisionAction = 'approve' | 'deny'

function statusTone(status: string): BadgeTone {
  if (status === 'approved') return 'ok'
  if (status === 'denied') return 'danger'
  if (status === 'pending') return 'warn'
  return 'neutral'
}

function ApprovalsPage(_props: PageProps) {
  const toast = useToast()
  const [approvals, setApprovals] = useState<Approval[]>([])
  const [state, setState] = useState<'loading' | 'ready' | 'error'>('loading')
  const [error, setError] = useState<string | null>(null)

  const [decision, setDecision] = useState<{ approval: Approval; action: DecisionAction } | null>(null)
  const [note, setNote] = useState('')
  const [submitting, setSubmitting] = useState(false)

  const load = useCallback(() => {
    let live = true
    setState('loading')

    getApprovals()
      .then((result) => {
        if (!live) return
        setApprovals(result.approvals ?? [])
        setError(null)
        setState('ready')
      })
      .catch((cause: unknown) => {
        if (!live) return
        setError(cause instanceof Error ? cause.message : 'Could not load the approval queue.')
        setState('error')
      })

    return () => {
      live = false
    }
  }, [])

  useEffect(() => load(), [load])

  const openDecision = useCallback((approval: Approval, action: DecisionAction) => {
    setNote('')
    setDecision({ approval, action })
  }, [])

  const confirmDecision = useCallback(async () => {
    if (!decision) return
    const { approval, action } = decision
    setSubmitting(true)
    try {
      await decideApproval(approval.approvalId, action, note.trim())
      toast.success(action === 'approve' ? 'Approved' : 'Denied')
      setDecision(null)
      load()
    } catch (cause) {
      toast.error(cause instanceof ApiError ? cause.message : `Could not ${action} that item.`)
    } finally {
      setSubmitting(false)
    }
  }, [decision, note, toast, load])

  const columns = useMemo<Column<Approval>[]>(
    () => [
      {
        key: 'action',
        header: 'Action',
        render: (a) => (
          <>
            <div>{a.reason}</div>
            <div className="ui-mono approvals-id">
              {a.approvalId}
              {a.workflowRunId && ` · ${a.workflowRunId}`}
            </div>
            {a.decisionNote && <div className="approvals-note">Note: {a.decisionNote}</div>}
          </>
        ),
      },
      { key: 'requester', header: 'Requester', width: '9rem', render: (a) => a.requestedBy },
      {
        key: 'risk',
        header: 'Risk',
        width: '7rem',
        render: (a) => (
          <div className="approvals-stack">
            <Badge tone={severityTone(a.riskLevel)} subtle>
              {a.riskLevel}
            </Badge>
            <Badge tone={statusTone(a.status)} dot>
              {a.status}
            </Badge>
          </div>
        ),
      },
      {
        key: 'artifacts',
        header: 'Artifacts',
        render: (a) =>
          a.artifactIds.length === 0 ? (
            '—'
          ) : (
            <div className="approvals-stack">
              {a.artifactIds.map((id) => (
                <a key={id} href={apiUrl(`/artifacts/${encodeURIComponent(id)}/download`)}>
                  {id}
                </a>
              ))}
            </div>
          ),
      },
      {
        key: 'created',
        header: 'Created',
        width: '9rem',
        muted: true,
        nowrap: true,
        render: (a) => <span title={formatWhen(a.createdAt)}>{formatRelative(a.createdAt)}</span>,
      },
      {
        key: 'decide',
        header: '',
        srHeader: 'Decide',
        width: '11rem',
        align: 'right',
        render: (a) =>
          a.status === 'pending' ? (
            <div className="approvals-row-actions">
              <Button size="sm" onClick={() => openDecision(a, 'approve')}>
                Approve
              </Button>
              <Button size="sm" variant="danger" onClick={() => openDecision(a, 'deny')}>
                Deny
              </Button>
            </div>
          ) : (
            <span className="approvals-decided-by">{a.approver || '—'}</span>
          ),
      },
    ],
    [openDecision],
  )

  return (
    <div className="approvals">
      <Card
        title="Approval queue"
        description="External sends and other high-impact actions wait here until you decide."
        flush
        actions={
          <Button variant="quiet" size="sm" onClick={load} loading={state === 'loading'}>
            Refresh
          </Button>
        }
      >
        <DataTable
          columns={columns}
          rows={approvals}
          rowKey={(a) => a.approvalId}
          loading={state === 'loading'}
          skeletonRows={5}
          caption="High-impact actions awaiting or given an admin decision"
          empty={
            state === 'error' ? (
              <EmptyState
                title="Couldn't load approvals"
                description={error ?? undefined}
                action={
                  <Button size="sm" variant="ghost" onClick={load}>
                    Try again
                  </Button>
                }
              />
            ) : (
              <EmptyState title="No approvals yet" description="Nothing has needed a decision yet." />
            )
          }
        />
      </Card>

      <Drawer
        open={decision != null}
        onClose={() => setDecision(null)}
        eyebrow="Approval"
        title={decision?.action === 'deny' ? 'Deny this action?' : 'Approve this action?'}
        footer={
          <>
            <Button variant="ghost" onClick={() => setDecision(null)} disabled={submitting}>
              Cancel
            </Button>
            <Button
              variant={decision?.action === 'deny' ? 'danger' : 'primary'}
              onClick={confirmDecision}
              loading={submitting}
            >
              {decision?.action === 'deny' ? 'Deny' : 'Approve'}
            </Button>
          </>
        }
      >
        {decision && (
          <div className="approvals-decision-body">
            <KeyValue
              items={[
                { label: 'Reason', value: decision.approval.reason, wide: true },
                { label: 'Requester', value: decision.approval.requestedBy },
                { label: 'Risk', value: decision.approval.riskLevel },
                ...(decision.approval.workflowRunId
                  ? [{ label: 'Workflow run', value: decision.approval.workflowRunId, mono: true, wide: true }]
                  : []),
              ]}
            />
            <Field label="Note" hint="Optional — visible to whoever looks at this decision later.">
              {(fieldProps) => (
                <Textarea
                  {...fieldProps}
                  mono={false}
                  rows={3}
                  placeholder="optional"
                  value={note}
                  onChange={(e) => setNote(e.target.value)}
                />
              )}
            </Field>
          </div>
        )}
      </Drawer>
    </div>
  )
}

export default ApprovalsPage
