import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Badge,
  BarList,
  Button,
  Card,
  CodeBlock,
  DataTable,
  Donut,
  Drawer,
  EmptyState,
  Field,
  Heatmap,
  Input,
  KeyValue,
  Legend,
  SegmentedControl,
  Skeleton,
  Sparkline,
  StackedBars,
  Stat,
  StatGrid,
  Switch,
  useToast,
  type BadgeTone,
  type Column,
  type Segment,
} from '../components/ui'
import {
  ApiError,
  MONITOR_RANGE_HOURS,
  exportAuditWindow,
  getAdminCalls,
  getOverview,
  getPolicyChanges,
  type AdminCall,
  type MonitorRange,
  type Overview,
  type PolicyChange,
} from '../lib/api'
import { formatLatency, formatRelative, formatWhen } from '../lib/format'
import type { PageProps } from './types'
import './MonitorPage.css'

/**
 * Monitor — ports `renderMonitor()`: the live security overview (KPIs,
 * volume/authorization/department/tool/user/heatmap charts), the recent
 * tool-call feed, and the policy-change log.
 *
 * What changed and why:
 *   - The legacy panel rendered "Recent tool calls" and "Policy changes" as
 *     two full `<table>`s with no height limit — 200 and 200 rows stacked
 *     straight into page flow, so the tab grew to several screens tall and
 *     the charts above scrolled out of reach. Both tables now sit in a
 *     `DataTable` with a bounded `maxHeight` and a sticky header: the page's
 *     height is fixed, the list scrolls inside its own card.
 *   - Two chart shapes the kit didn't have yet, ported straight from the
 *     legacy `_stacked()` and `_heatmap()` SVG builders as real components
 *     (`StackedBars`, `Heatmap`) — reusable anywhere else a volume-over-time
 *     or weekday×hour view comes up, not one-off markup for this page.
 *   - Everything else (KPIs, donuts, bar lists) reuses `Stat`/`Donut`/
 *     `BarList`/`Sparkline` as built — this page is most of what they were
 *     built for.
 *   - The calls table gets a client-side search + status filter the legacy
 *     version didn't have; with 200 rows in view at once that's the
 *     difference between scanning and scrolling.
 *   - Range and the two tables load independently, matching the legacy
 *     panel's behavior (switching 1h/24h/7d/30d only recomputes the charts)
 *     — but each section now has its own loading/error state instead of one
 *     all-or-nothing gate, so a slow table doesn't hold the charts hostage.
 */

const RANGES: Segment<MonitorRange>[] = [
  { value: '1h', label: '1h' },
  { value: '24h', label: '24h' },
  { value: '7d', label: '7d' },
  { value: '30d', label: '30d' },
]

const STATUS_FILTERS: Segment<'all' | 'ok' | 'denied' | 'error'>[] = [
  { value: 'all', label: 'All' },
  { value: 'ok', label: 'Authorized' },
  { value: 'denied', label: 'Denied' },
  { value: 'error', label: 'Error' },
]

const WEEKDAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']

function callStatusTone(status: string): BadgeTone {
  if (status === 'ok') return 'ok'
  if (status === 'denied') return 'danger'
  if (status === 'error') return 'warn'
  return 'neutral'
}

type LoadState = 'loading' | 'ready' | 'error'

function MonitorPage(_props: PageProps) {
  const toast = useToast()

  const [range, setRange] = useState<MonitorRange>('24h')
  const [overview, setOverview] = useState<Overview | null>(null)
  const [overviewState, setOverviewState] = useState<LoadState>('loading')
  const [overviewError, setOverviewError] = useState<string | null>(null)

  const [calls, setCalls] = useState<AdminCall[]>([])
  const [callsState, setCallsState] = useState<LoadState>('loading')
  const [callsError, setCallsError] = useState<string | null>(null)

  const [changes, setChanges] = useState<PolicyChange[]>([])
  const [changesState, setChangesState] = useState<LoadState>('loading')
  const [changesError, setChangesError] = useState<string | null>(null)

  const [search, setSearch] = useState('')
  const [statusFilter, setStatusFilter] = useState<'all' | 'ok' | 'denied' | 'error'>('all')
  const [suspiciousOnly, setSuspiciousOnly] = useState(false)
  const [selectedCall, setSelectedCall] = useState<AdminCall | null>(null)

  const [exporting, setExporting] = useState(false)
  const [exportResult, setExportResult] = useState<{ filename: string; url: string; total: number } | null>(null)

  const loadOverview = useCallback((r: MonitorRange) => {
    let live = true
    setOverviewState('loading')

    getOverview(r)
      .then((o) => {
        if (!live) return
        setOverview(o)
        setOverviewError(null)
        setOverviewState('ready')
      })
      .catch((cause: unknown) => {
        if (!live) return
        setOverviewError(cause instanceof Error ? cause.message : 'Could not load the overview.')
        setOverviewState('error')
      })

    return () => {
      live = false
    }
  }, [])

  useEffect(() => loadOverview(range), [range, loadOverview])

  const loadCalls = useCallback(() => {
    let live = true
    setCallsState('loading')

    getAdminCalls(200)
      .then((result) => {
        if (!live) return
        setCalls(result.calls ?? [])
        setCallsError(null)
        setCallsState('ready')
      })
      .catch((cause: unknown) => {
        if (!live) return
        setCallsError(cause instanceof Error ? cause.message : 'Could not load recent calls.')
        setCallsState('error')
      })

    return () => {
      live = false
    }
  }, [])

  useEffect(() => loadCalls(), [loadCalls])

  const loadChanges = useCallback(() => {
    let live = true
    setChangesState('loading')

    getPolicyChanges()
      .then((result) => {
        if (!live) return
        setChanges(result.changes ?? [])
        setChangesError(null)
        setChangesState('ready')
      })
      .catch((cause: unknown) => {
        if (!live) return
        setChangesError(cause instanceof Error ? cause.message : 'Could not load policy changes.')
        setChangesState('error')
      })

    return () => {
      live = false
    }
  }, [])

  useEffect(() => loadChanges(), [loadChanges])

  const refreshAll = useCallback(() => {
    loadOverview(range)
    loadCalls()
    loadChanges()
  }, [range, loadOverview, loadCalls, loadChanges])

  const runExport = useCallback(async () => {
    setExporting(true)
    setExportResult(null)
    try {
      const result = await exportAuditWindow(MONITOR_RANGE_HOURS[range])
      setExportResult({
        filename: result.artifact.filename,
        url: result.artifact.downloadUrl,
        total: result.summary.total,
      })
      toast.success('Audit export created')
    } catch (cause) {
      toast.error(cause instanceof ApiError ? cause.message : 'Could not create the export.')
    } finally {
      setExporting(false)
    }
  }, [range, toast])

  const shownCalls = useMemo(() => {
    const q = search.trim().toLowerCase()
    return calls.filter((c) => {
      if (statusFilter !== 'all' && c.status !== statusFilter) return false
      if (suspiciousOnly && !c.suspicious) return false
      if (q && !c.consumer.toLowerCase().includes(q) && !c.tool.toLowerCase().includes(q)) return false
      return true
    })
  }, [calls, search, statusFilter, suspiciousOnly])

  const seriesTotals = useMemo(
    () => overview?.series.map((s) => s.ok + s.denied + s.error) ?? [],
    [overview],
  )

  const authSegments = useMemo(() => {
    const k = overview?.kpis
    if (!k) return []
    return [
      { label: 'Authorized', value: k.ok, color: 'var(--ui-ok)' },
      { label: 'Denied', value: k.denied, color: 'var(--ui-danger)' },
      { label: 'Error', value: k.error, color: 'var(--ui-warn)' },
    ]
  }, [overview])

  const volumeBuckets = useMemo(
    () =>
      overview?.series.map((s) => ({
        label: s.label,
        segments: [
          { key: 'ok', value: s.ok, color: 'var(--ui-ok)' },
          { key: 'denied', value: s.denied, color: 'var(--ui-danger)' },
          { key: 'error', value: s.error, color: 'var(--ui-warn)' },
        ],
      })) ?? [],
    [overview],
  )

  const deptSegments = useMemo(
    () => overview?.by_department.map((d) => ({ label: d.k, value: d.n })) ?? [],
    [overview],
  )

  const callColumns = useMemo<Column<AdminCall>[]>(
    () => [
      {
        key: 'when',
        header: 'When',
        width: '8.5rem',
        muted: true,
        nowrap: true,
        render: (c) => <span title={formatWhen(c.ts)}>{formatRelative(c.ts)}</span>,
      },
      { key: 'user', header: 'User', width: '9rem', render: (c) => c.consumer },
      { key: 'tool', header: 'Tool', mono: true, render: (c) => c.tool },
      {
        key: 'status',
        header: 'Status',
        width: '7rem',
        render: (c) => <Badge tone={callStatusTone(c.status)}>{c.status}</Badge>,
      },
      { key: 'rows', header: 'Rows', width: '5rem', numeric: true, render: (c) => (c.rows != null ? c.rows : '—') },
      {
        key: 'latency',
        header: 'Latency',
        width: '6rem',
        muted: true,
        numeric: true,
        render: (c) => formatLatency(c.latency_ms),
      },
      {
        key: 'safety',
        header: 'Safety',
        width: '8rem',
        render: (c) => (
          <Badge tone={c.suspicious ? 'danger' : 'ok'} dot title={(c.reasons || []).join('; ') || undefined}>
            {c.suspicious ? 'Suspicious' : 'Safe'}
          </Badge>
        ),
      },
    ],
    [],
  )

  const changeColumns = useMemo<Column<PolicyChange>[]>(
    () => [
      {
        key: 'when',
        header: 'When',
        width: '8.5rem',
        muted: true,
        nowrap: true,
        render: (c) => <span title={formatWhen(c.ts)}>{formatRelative(c.ts)}</span>,
      },
      { key: 'actor', header: 'Actor', width: '10rem', render: (c) => <b>{c.consumer}</b> },
      { key: 'change', header: 'Change', mono: true, render: (c) => c.tool },
      { key: 'detail', header: 'Detail', muted: true, render: (c) => c.detail || '—' },
    ],
    [],
  )

  const k = overview?.kpis

  return (
    <div className="monitor">
      <div className="monitor-toolbar">
        <div className="monitor-toolbar-controls">
          <SegmentedControl label="Time range" segments={RANGES} value={range} onChange={setRange} />
          <Button variant="quiet" size="sm" onClick={refreshAll}>
            Refresh
          </Button>
        </div>
      </div>

      {overviewState === 'error' ? (
        <Card title="Couldn't load the overview" accent="danger">
          <p className="monitor-error-body">{overviewError}</p>
          <Button variant="ghost" onClick={() => loadOverview(range)}>
            Try again
          </Button>
        </Card>
      ) : overviewState === 'loading' || !k ? (
        <StatGrid columns={4}>
          {Array.from({ length: 8 }, (_, i) => (
            <Skeleton key={i} index={i} height="4.5rem" />
          ))}
        </StatGrid>
      ) : (
        <StatGrid columns={4}>
          <Stat
            label="Calls"
            value={k.total}
            chart={seriesTotals.length >= 2 ? <Sparkline values={seriesTotals} /> : undefined}
          />
          <Stat label="Authorized" value={k.auth_rate} unit="%" tone="ok" decimals={1} />
          <Stat label="Denied" value={k.denied} tone={k.denied > 0 ? 'danger' : 'neutral'} />
          <Stat label="Suspicious" value={k.suspicious} tone={k.suspicious > 0 ? 'danger' : 'neutral'} />
          <Stat label="Sensitive access" value={k.sensitive} />
          <Stat label="Redactions" value={k.redactions} />
          <Stat label="Rows returned" value={k.rows} />
          <Stat label="Avg latency" value={k.avg_latency} unit="ms" decimals={1} />
        </StatGrid>
      )}

      {overviewState === 'ready' && overview && (
        <div className="monitor-charts">
          <Card className="monitor-card--span2" title="Call volume" description="authorized / denied / error">
            <StackedBars buckets={volumeBuckets} />
            <Legend segments={authSegments} />
          </Card>

          <Card title="Authorization" description="share allowed">
            <div className="monitor-donut-row">
              <Donut
                segments={authSegments}
                centerValue={`${k?.auth_rate ?? 0}%`}
                centerLabel="allowed"
                label="Authorization breakdown"
              />
              <Legend segments={authSegments} showPercent />
            </div>
          </Card>

          <Card title="By department" description="where demand is">
            {deptSegments.length === 0 ? (
              <EmptyState title="No data" compact />
            ) : (
              <div className="monitor-donut-row">
                <Donut segments={deptSegments} label="Calls by department" />
                <Legend segments={deptSegments} showPercent />
              </div>
            )}
          </Card>

          <Card title="Top tools" description="most-called governed tools">
            <BarList items={overview.by_tool.map((t) => ({ label: t.k, value: t.n }))} />
          </Card>

          <Card title="Busiest principals" description="calls per consumer">
            <BarList items={overview.by_user.map((u) => ({ label: u.k, value: u.n }))} />
          </Card>

          <Card className="monitor-card--full" title="Activity by hour" description="off-hours access stands out">
            <Heatmap
              grid={overview.heatmap.grid}
              rowLabels={WEEKDAYS}
              max={overview.heatmap.max}
              colLabel={(c) => (c % 3 === 0 ? String(c) : '')}
              cellLabel={(v, r, c) => `${WEEKDAYS[r]} ${c}:00 — ${v} call${v === 1 ? '' : 's'}`}
            />
          </Card>
        </div>
      )}

      <Card
        title="Recent tool calls"
        description="Click a row for full detail."
        flush
        actions={
          <>
            <Field label="Search" inline>
              {(props) => (
                <Input
                  {...props}
                  placeholder="Consumer or tool…"
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                  className="monitor-search"
                />
              )}
            </Field>
            <SegmentedControl label="Status filter" segments={STATUS_FILTERS} value={statusFilter} onChange={setStatusFilter} />
            <Switch checked={suspiciousOnly} onChange={setSuspiciousOnly} label="Suspicious only" size="sm" />
          </>
        }
        footer={
          <div className="monitor-export-row">
            <Button variant="ghost" size="sm" onClick={runExport} loading={exporting}>
              Export audit window
            </Button>
            {exportResult && (
              <span className="monitor-export-out">
                Audit artifact:{' '}
                <a href={exportResult.url} target="_blank" rel="noreferrer">
                  {exportResult.filename}
                </a>{' '}
                ({exportResult.total.toLocaleString()} events)
              </span>
            )}
          </div>
        }
      >
        <DataTable
          columns={callColumns}
          rows={shownCalls}
          rowKey={(c, i) => `${c.ts}-${c.tool}-${i}`}
          loading={callsState === 'loading'}
          skeletonRows={6}
          maxHeight="28rem"
          stickyHeader
          dense
          onRowClick={setSelectedCall}
          isRowActive={(c) => c === selectedCall}
          caption="Recent governed tool calls"
          footer={
            callsState === 'ready' && (
              <span className="monitor-tbl-count">
                Showing {shownCalls.length.toLocaleString()} of {calls.length.toLocaleString()} loaded
              </span>
            )
          }
          empty={
            callsState === 'error' ? (
              <EmptyState
                title="Couldn't load recent calls"
                description={callsError ?? undefined}
                action={
                  <Button size="sm" variant="ghost" onClick={loadCalls}>
                    Try again
                  </Button>
                }
              />
            ) : (
              <EmptyState title="No calls yet" />
            )
          }
        />
      </Card>

      <Card title="Policy changes" description="Logins, key rotations, and policy edits." flush>
        <DataTable
          columns={changeColumns}
          rows={changes}
          rowKey={(c, i) => `${c.ts}-${i}`}
          loading={changesState === 'loading'}
          skeletonRows={4}
          maxHeight="18rem"
          stickyHeader
          dense
          caption="Recent policy changes"
          empty={
            changesState === 'error' ? (
              <EmptyState
                title="Couldn't load policy changes"
                description={changesError ?? undefined}
                action={
                  <Button size="sm" variant="ghost" onClick={loadChanges}>
                    Try again
                  </Button>
                }
              />
            ) : (
              <EmptyState title="None yet" />
            )
          }
        />
      </Card>

      <Drawer
        open={selectedCall != null}
        onClose={() => setSelectedCall(null)}
        eyebrow="Call detail"
        title={selectedCall?.tool ?? ''}
        monoTitle
        meta={
          selectedCall && (
            <>
              <Badge tone={callStatusTone(selectedCall.status)}>{selectedCall.status}</Badge>
              <Badge tone={selectedCall.suspicious ? 'danger' : 'ok'} dot>
                {selectedCall.suspicious ? 'Suspicious' : 'Safe'}
              </Badge>
            </>
          )
        }
      >
        {selectedCall && (
          <div className="monitor-call-body">
            {selectedCall.suspicious && (selectedCall.reasons?.length ?? 0) > 0 && (
              <p className="monitor-call-reasons">{selectedCall.reasons!.join('; ')}</p>
            )}
            <KeyValue
              items={[
                { label: 'User', value: selectedCall.consumer },
                { label: 'When', value: formatWhen(selectedCall.ts) },
                { label: 'Rows returned', value: selectedCall.rows },
                { label: 'Latency', value: formatLatency(selectedCall.latency_ms) },
                { label: 'Customer', value: selectedCall.customer_id, mono: true },
                { label: 'Session', value: selectedCall.session_id, mono: true },
                { label: 'Request', value: selectedCall.request_id, mono: true },
                { label: 'Client IP', value: selectedCall.client_ip, mono: true },
              ]}
            />
            <CodeBlock label="Arguments" copyable>
              {selectedCall.args || '(none)'}
            </CodeBlock>
            {selectedCall.detail && (
              <KeyValue items={[{ label: 'Detail', value: selectedCall.detail, wide: true }]} />
            )}
          </div>
        )}
      </Drawer>
    </div>
  )
}

export default MonitorPage
