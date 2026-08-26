import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Badge,
  Button,
  Card,
  DataTable,
  EmptyState,
  PageShell,
  type BadgeTone,
  type Column,
} from '../components/ui'
import { getEmailSends, type EmailSend } from '../lib/api'
import { formatRelative, formatWhen } from '../lib/format'
import type { PageProps } from './types'
import './SendQueuePage.css'

/**
 * Send Queue — ports `renderSends()`: a read-only view of this principal's
 * approval-gated email sends. There is no draft form here — email drafts
 * are created elsewhere (the Assistant/Playground calling `draft_email`);
 * this tab only shows what already has an approved approval tied to it and
 * is sitting in `send_email`'s queue, exactly like Calendar's own connector
 * queue table.
 */

function sendStatusTone(status: string): BadgeTone {
  return status === 'sent' ? 'ok' : 'warn'
}

function SendQueuePage(_props: PageProps) {
  const [sends, setSends] = useState<EmailSend[]>([])
  const [state, setState] = useState<'loading' | 'ready' | 'error'>('loading')
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(() => {
    let live = true
    setState('loading')

    getEmailSends()
      .then((result) => {
        if (!live) return
        setSends(result.sends ?? [])
        setError(null)
        setState('ready')
      })
      .catch((cause: unknown) => {
        if (!live) return
        const message = cause instanceof Error ? cause.message : 'Could not load the send queue.'
        setError(message)
        setState('error')
      })

    return () => {
      live = false
    }
  }, [])

  useEffect(() => load(), [load])

  const columns = useMemo<Column<EmailSend>[]>(
    () => [
      {
        key: 'email',
        header: 'Email',
        render: (s) => (
          <>
            <div>{s.subject || 'Email'}</div>
            <div className="sendqueue-subtext ui-mono">
              {s.sendId} · draft {s.draftArtifactId}
            </div>
            {s.message && <div className="sendqueue-subtext">{s.message}</div>}
          </>
        ),
      },
      {
        key: 'status',
        header: 'Status',
        width: '9rem',
        render: (s) => (
          <Badge tone={sendStatusTone(s.status)} dot>
            {s.status}
          </Badge>
        ),
      },
      { key: 'provider', header: 'Provider', width: '7rem' },
      { key: 'approvalId', header: 'Approval', mono: true, width: '9rem' },
      {
        key: 'recipients',
        header: 'Recipients',
        render: (s) => [...s.to, ...s.cc].join(', ') || '—',
      },
      {
        key: 'createdAt',
        header: 'Created',
        width: '9rem',
        muted: true,
        nowrap: true,
        render: (s) => <span title={formatWhen(s.createdAt)}>{formatRelative(s.createdAt)}</span>,
      },
    ],
    [],
  )

  return (
    <PageShell className="sendqueue">
      <Card
        title="Email send queue"
        description="Approved drafts land here for connector-backed delivery. Local mode queues without external delivery."
        flush
        actions={
          <Button variant="quiet" size="sm" onClick={load} loading={state === 'loading'}>
            Refresh
          </Button>
        }
      >
        <DataTable
          columns={columns}
          rows={sends}
          rowKey={(s) => s.sendId}
          loading={state === 'loading'}
          skeletonRows={4}
          caption="Queued email sends"
          empty={
            state === 'error' ? (
              <EmptyState
                title="Couldn't load the send queue"
                description={error ?? undefined}
                action={
                  <Button size="sm" variant="ghost" onClick={load}>
                    Try again
                  </Button>
                }
              />
            ) : (
              <EmptyState title="No queued sends yet" description="Approved email drafts appear here once queued." />
            )
          }
        />
      </Card>
    </PageShell>
  )
}

export default SendQueuePage
