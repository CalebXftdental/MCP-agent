import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Badge,
  Button,
  Card,
  DataTable,
  Drawer,
  EmptyState,
  KeyValue,
  SecretKey,
  SegmentedControl,
  SeverityBar,
  severityTone,
  useToast,
  type BadgeTone,
  type Column,
  type Segment,
} from '../components/ui'
import {
  ApiError,
  actOnAlert,
  getAlerts,
  rotateConsumerKey,
  setConsumerStatus,
  type Alert,
} from '../lib/api'
import { formatRelative, formatWhen } from '../lib/format'
import type { PageProps } from './types'
import './AlertsPage.css'

/**
 * Alerts — ports `renderAlerts()`: one flat table of incidents, each already
 * one (principal × signal-type) bucket rolled up server-side — there is no
 * further per-principal grouping to do client-side, the description's
 * "grouped per principal" describes the server's roll-up, not a nested view.
 *
 * Two changes from the legacy panel:
 *   - The severity stripe was a raw inline-styled div; it's `SeverityBar`
 *     here, the same stripe Badge.tsx already exports for exactly this.
 *   - "Rotate key" from the containment drawer used to fire the request and
 *     throw the returned key away — the toast just said "Key rotated", never
 *     showing it, so the only way to get the new value was to sign in AS
 *     that principal. It's shown once via `SecretKey` now, the same fix
 *     already applied to Security's own hygiene-table rotate action.
 *   - Legacy's `confirm()` before Disable/Rotate is now the drawer swapping
 *     to a confirm sub-view in place — no browser dialog, no second Drawer
 *     stacked on the first.
 */

const FILTERS: Segment<'open' | 'all'>[] = [
  { value: 'open', label: 'Open', title: 'Only unresolved incidents' },
  { value: 'all', label: 'All', title: 'Every incident in the last 7 days' },
]

function alertStatusTone(status: string): BadgeTone {
  if (status === 'open') return 'warn'
  if (status === 'resolved') return 'ok'
  if (status === 'acknowledged') return 'info'
  return 'neutral'
}

type ContainAction = 'disable' | 'rotate'

function AlertsPage(_props: PageProps) {
  const toast = useToast()

  const [alerts, setAlerts] = useState<Alert[]>([])
  const [state, setState] = useState<'loading' | 'ready' | 'error'>('loading')
  const [error, setError] = useState<string | null>(null)
  const [filter, setFilter] = useState<'open' | 'all'>('open')

  // Per-id Ack/Resolve spinner — keyed so one row's action doesn't disable
  // the whole table.
  const [acting, setActing] = useState<Record<string, 'ack' | 'resolve'>>({})

  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [containConfirm, setContainConfirm] = useState<ContainAction | null>(null)
  const [containSubmitting, setContainSubmitting] = useState(false)
  const [mintedKey, setMintedKey] = useState<string | null>(null)

  const load = useCallback(() => {
    let live = true
    setState('loading')

    getAlerts()
      .then((result) => {
        if (!live) return
        setAlerts(result.alerts ?? [])
        setError(null)
        setState('ready')
      })
      .catch((cause: unknown) => {
        if (!live) return
        setError(cause instanceof Error ? cause.message : 'Could not load alerts.')
        setState('error')
      })

    return () => {
      live = false
    }
  }, [])

  useEffect(() => load(), [load])

  const shown = useMemo(
    () => (filter === 'all' ? alerts : alerts.filter((a) => a.status === 'open')),
    [alerts, filter],
  )

  // Derived from the live list rather than held as its own object, so an
  // Ack/Resolve/contain action that reloads `alerts` updates what the open
  // drawer shows instead of going stale the moment the list refreshes.
  const selectedAlert = useMemo(() => alerts.find((a) => a.id === selectedId) ?? null, [alerts, selectedId])

  const closeDrawer = useCallback(() => {
    setSelectedId(null)
    setContainConfirm(null)
    setMintedKey(null)
    load()
  }, [load])

  const runAlertAction = useCallback(
    async (id: string, action: 'ack' | 'resolve') => {
      setActing((prev) => ({ ...prev, [id]: action }))
      try {
        await actOnAlert(id, action)
        toast.success(action === 'ack' ? 'Acknowledged' : 'Resolved')
        load()
        return true
      } catch (cause) {
        toast.error(cause instanceof ApiError ? cause.message : 'Could not update that incident.')
        return false
      } finally {
        setActing((prev) => {
          const next = { ...prev }
          delete next[id]
          return next
        })
      }
    },
    [toast, load],
  )

  const handleDrawerAction = useCallback(
    async (action: 'ack' | 'resolve') => {
      if (!selectedAlert) return
      if (await runAlertAction(selectedAlert.id, action)) closeDrawer()
    },
    [selectedAlert, runAlertAction, closeDrawer],
  )

  const openContainConfirm = useCallback((action: ContainAction) => {
    setMintedKey(null)
    setContainConfirm(action)
  }, [])

  const confirmContain = useCallback(async () => {
    if (!selectedAlert?.consumer_id || !containConfirm) return
    setContainSubmitting(true)
    try {
      if (containConfirm === 'disable') {
        await setConsumerStatus(selectedAlert.consumer_id, 'disabled')
        await actOnAlert(selectedAlert.id, 'resolve').catch(() => {})
        toast.success('Principal disabled · incident resolved')
        closeDrawer()
      } else {
        const result = await rotateConsumerKey(selectedAlert.consumer_id)
        await actOnAlert(selectedAlert.id, 'resolve').catch(() => {})
        toast.success('Key rotated · incident resolved')
        setMintedKey(result.api_key)
      }
    } catch (cause) {
      toast.error(cause instanceof ApiError ? cause.message : 'Could not complete that action.')
    } finally {
      setContainSubmitting(false)
    }
  }, [selectedAlert, containConfirm, toast, closeDrawer])

  const columns = useMemo<Column<Alert>[]>(
    () => [
      {
        key: 'severity',
        header: 'Severity',
        width: '7.5rem',
        render: (a) => (
          <span className="alerts-severity">
            <SeverityBar severity={a.severity} />
            <Badge tone={severityTone(a.severity)}>{a.severity}</Badge>
          </span>
        ),
      },
      { key: 'principal', header: 'Principal', width: '10rem', render: (a) => <b>{a.consumer}</b> },
      {
        key: 'signal',
        header: 'Signal',
        render: (a) => (
          <>
            <div>{a.reason}</div>
            <div className="alerts-type ui-mono">{a.type}</div>
          </>
        ),
      },
      { key: 'events', header: 'Events', width: '5.5rem', numeric: true },
      {
        key: 'last_seen',
        header: 'Last seen',
        width: '9rem',
        muted: true,
        nowrap: true,
        render: (a) => <span title={formatWhen(a.last_ts)}>{formatRelative(a.last_ts)}</span>,
      },
      {
        key: 'status',
        header: 'Status',
        width: '8rem',
        render: (a) => (
          <Badge tone={alertStatusTone(a.status)} dot>
            {a.status}
          </Badge>
        ),
      },
      {
        key: 'actions',
        header: '',
        srHeader: 'Decide',
        width: '11rem',
        align: 'right',
        render: (a) =>
          a.status === 'resolved' ? (
            <span className="alerts-handled">{a.handled_by || '—'}</span>
          ) : (
            <div className="alerts-row-actions">
              <Button
                size="sm"
                variant="ghost"
                onClick={(e) => {
                  e.stopPropagation()
                  runAlertAction(a.id, 'ack')
                }}
                loading={acting[a.id] === 'ack'}
                disabled={acting[a.id] === 'resolve'}
              >
                Ack
              </Button>
              <Button
                size="sm"
                onClick={(e) => {
                  e.stopPropagation()
                  runAlertAction(a.id, 'resolve')
                }}
                loading={acting[a.id] === 'resolve'}
                disabled={acting[a.id] === 'ack'}
              >
                Resolve
              </Button>
            </div>
          ),
      },
    ],
    [acting, runAlertAction],
  )

  return (
    <div className="alerts">
      <Card
        title="Security incidents"
        description="Flagged behavior grouped per principal — enumeration, denial bursts, call bursts, repeated errors."
        flush
        actions={
          <>
            <SegmentedControl label="Incident filter" segments={FILTERS} value={filter} onChange={setFilter} />
            <Button variant="quiet" size="sm" onClick={load} loading={state === 'loading'}>
              Refresh
            </Button>
          </>
        }
      >
        <DataTable
          columns={columns}
          rows={shown}
          rowKey={(a) => a.id}
          loading={state === 'loading'}
          skeletonRows={5}
          onRowClick={(a) => setSelectedId(a.id)}
          isRowActive={(a) => a.id === selectedId}
          caption="Security incidents grouped per principal"
          empty={
            state === 'error' ? (
              <EmptyState
                title="Couldn't load alerts"
                description={error ?? undefined}
                action={
                  <Button size="sm" variant="ghost" onClick={load}>
                    Try again
                  </Button>
                }
              />
            ) : (
              <EmptyState
                title={filter === 'open' ? 'No open incidents' : 'No incidents'}
                description="Nothing has been flagged in the last 7 days."
              />
            )
          }
        />
      </Card>

      <Drawer
        open={selectedId != null}
        onClose={closeDrawer}
        eyebrow={selectedAlert ? `Incident · ${selectedAlert.severity}` : 'Incident'}
        title={selectedAlert?.consumer ?? ''}
        footer={
          mintedKey ? (
            <Button onClick={closeDrawer}>Done</Button>
          ) : containConfirm ? (
            <>
              <Button variant="ghost" onClick={() => setContainConfirm(null)} disabled={containSubmitting}>
                Cancel
              </Button>
              <Button
                variant={containConfirm === 'disable' ? 'danger' : 'primary'}
                onClick={confirmContain}
                loading={containSubmitting}
              >
                {containConfirm === 'disable' ? 'Disable principal' : 'Rotate key'}
              </Button>
            </>
          ) : selectedAlert && selectedAlert.status !== 'resolved' ? (
            <>
              <Button
                variant="ghost"
                onClick={() => handleDrawerAction('ack')}
                loading={acting[selectedAlert.id] === 'ack'}
              >
                Acknowledge
              </Button>
              <Button onClick={() => handleDrawerAction('resolve')} loading={acting[selectedAlert.id] === 'resolve'}>
                Resolve
              </Button>
            </>
          ) : undefined
        }
      >
        {selectedAlert &&
          (mintedKey ? (
            <div className="alerts-decision-body">
              <SecretKey
                value={mintedKey}
                label="New API key"
                hint="Copy it now — store it safely. This is the only time it's shown."
              />
            </div>
          ) : containConfirm ? (
            <div className="alerts-decision-body">
              <p className="alerts-confirm-msg">
                {containConfirm === 'disable'
                  ? 'Disable this principal? It will be blocked at the auth edge immediately.'
                  : "Rotate this principal's key? Its current key stops working at once."}
              </p>
              <p className="alerts-note">This also resolves the incident.</p>
            </div>
          ) : (
            <div className="alerts-decision-body">
              <p className="alerts-reason">{selectedAlert.reason}</p>

              <KeyValue
                items={[
                  { label: 'Signal', value: selectedAlert.type },
                  { label: 'Events', value: selectedAlert.events },
                  { label: 'First seen', value: formatWhen(selectedAlert.first_ts) },
                  { label: 'Last seen', value: formatWhen(selectedAlert.last_ts) },
                  { label: 'Status', value: selectedAlert.status },
                  { label: 'Handled by', value: selectedAlert.handled_by },
                ]}
              />

              <div>
                <p className="ui-eyebrow alerts-subsection-label">Tools involved</p>
                {selectedAlert.tools.length === 0 ? (
                  <p className="alerts-note">—</p>
                ) : (
                  <div className="alerts-tools">
                    {selectedAlert.tools.map((t) => (
                      <Badge key={t} subtle>
                        {t}
                      </Badge>
                    ))}
                  </div>
                )}
              </div>

              <div>
                <p className="ui-eyebrow alerts-subsection-label">Contain</p>
                {selectedAlert.consumer_id ? (
                  <>
                    <div className="alerts-contain-actions">
                      <Button variant="danger" size="sm" onClick={() => openContainConfirm('disable')}>
                        Disable principal
                      </Button>
                      <Button variant="ghost" size="sm" onClick={() => openContainConfirm('rotate')}>
                        Rotate key
                      </Button>
                    </div>
                    <p className="alerts-note">
                      Disable blocks this principal at the auth edge immediately; either action also resolves the
                      incident.
                    </p>
                  </>
                ) : (
                  <p className="alerts-note">No linked account found for this principal — contain manually.</p>
                )}
              </div>
            </div>
          ))}
      </Drawer>
    </div>
  )
}

export default AlertsPage
