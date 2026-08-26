import { useCallback, useEffect, useMemo, useState } from 'react'
import type { CSSProperties } from 'react'
import {
  Badge,
  Button,
  Card,
  Chip,
  DataTable,
  Drawer,
  Dropdown,
  EmptyState,
  Field,
  KeyValue,
  PageShell,
  SecretKey,
  Skeleton,
  Switch,
  useToast,
  type BadgeTone,
  type Column,
  type DropdownOption,
} from '../components/ui'
import {
  ApiError,
  getAdminControls,
  getBackendHealth,
  getCategoryCatalog,
  getCredentialHygiene,
  listConsumers,
  rotateConsumerKey,
  setAdminControls,
  setConsumerStatus,
  type AdminControls,
  type BackendHealth,
  type CategoryCatalogEntry,
  type Consumer,
  type CredentialHygieneConsumer,
} from '../lib/api'
import { formatRelative, formatWhen } from '../lib/format'
import type { PageProps } from './types'
import './SecurityPage.css'

/**
 * Security — ports `renderSecurity()`: break-glass containment, the backend
 * health board, and API-key hygiene, in the same three-card order as the
 * legacy panel.
 *
 * What changed and why:
 *   - Break-glass was a plain checkbox list with one immediate "Apply
 *     controls" button and zero confirmation — the copy already warns
 *     "takes effect immediately" but the UI had no friction to match. Toggles
 *     here only edit a local draft; a slide-in bar appears once the draft
 *     differs from what's actually applied, and Apply opens a Drawer that
 *     spells out the exact diff (what's newly blocked, what's being resumed)
 *     before it goes live. Turning things back off is the same flow, not a
 *     separate "safe" path — the diff view makes that obvious at a glance.
 *   - Each per-backend pause switch sits next to that backend's live health
 *     dot (from the same `/admin/backends/health` the board below already
 *     fetches), so blocking one is an informed decision, not a bare name in
 *     a checkbox list. A backend paused by an admin also gets flagged on its
 *     health card below, so a deliberate block never reads as an outage.
 *   - A third lever, pause specific consumers, sits alongside the other two
 *     in the same draft/diff/Apply flow -- containment for one named agent
 *     or user without reaching for the global agent switch or a whole
 *     backend. Distinct from disabling a consumer on the Consumers page:
 *     that revokes the credential/login outright, this only pauses traffic
 *     and is meant to be flipped back off once the incident's understood.
 *   - A fourth, narrower still: pause specific categories for ONE consumer.
 *     This is deliberately NOT the same field as that consumer's permanent
 *     `categories` grant on the Consumers page -- it's a temporary overlay
 *     enforced in `_govern`, so flipping it back off restores exactly what
 *     was there before, with no need to remember/reconstruct the original
 *     grant the way editing `categories` directly would require.
 *   - The hygiene table was read-only in the legacy panel — no rotate/revoke
 *     of a dormant or overdue key without leaving for the Consumers tab.
 *     Both actions now live on the row, through the same confirm-then-act
 *     Drawer pattern as Requests/Approvals.
 */

function backendTone(status: BackendHealth['status']): BadgeTone {
  if (status === 'healthy') return 'ok'
  if (status === 'degraded') return 'warn'
  if (status === 'down') return 'danger'
  return 'neutral'
}

function backendAccent(status: BackendHealth['status']): 'none' | 'ok' | 'warn' | 'danger' {
  if (status === 'healthy') return 'ok'
  if (status === 'degraded') return 'warn'
  if (status === 'down') return 'danger'
  return 'none'
}

type RowActionKind = 'rotate' | 'disable' | 'reactivate'

function SecurityPage(_props: PageProps) {
  const toast = useToast()

  const [controls, setControls] = useState<AdminControls | null>(null)
  const [backendNames, setBackendNames] = useState<string[]>([])
  const [health, setHealth] = useState<BackendHealth[]>([])
  const [hygiene, setHygiene] = useState<CredentialHygieneConsumer[]>([])
  const [allConsumers, setAllConsumers] = useState<Consumer[]>([])
  const [categories, setCategories] = useState<CategoryCatalogEntry[]>([])
  const [state, setState] = useState<'loading' | 'ready' | 'error'>('loading')
  const [error, setError] = useState<string | null>(null)

  // Local edits to the break-glass switches. Nothing here reaches the server
  // until Apply is confirmed — flipping a switch is free to reconsider.
  const [pausedAgentsDraft, setPausedAgentsDraft] = useState(false)
  const [pausedBackendsDraft, setPausedBackendsDraft] = useState<Set<string>>(new Set())
  const [pausedConsumersDraft, setPausedConsumersDraft] = useState<Set<string>>(new Set())
  const [pausedCategoriesDraft, setPausedCategoriesDraft] = useState<Record<string, string[]>>({})
  const [categoryEditConsumerId, setCategoryEditConsumerId] = useState('')
  const [confirmOpen, setConfirmOpen] = useState(false)
  const [applying, setApplying] = useState(false)

  const [rowAction, setRowAction] = useState<{ consumer: CredentialHygieneConsumer; action: RowActionKind } | null>(
    null,
  )
  const [rowSubmitting, setRowSubmitting] = useState(false)
  const [mintedKey, setMintedKey] = useState<string | null>(null)

  const load = useCallback(() => {
    let live = true
    setState('loading')

    Promise.all([
      getAdminControls(),
      getBackendHealth(),
      getCredentialHygiene(),
      getCategoryCatalog().catch(() => ({ categories: [] })),
      listConsumers().catch(() => ({ consumers: [] })),
    ])
      .then(([ctrl, bh, ch, cats, cs]) => {
        if (!live) return
        setControls(ctrl.controls)
        setBackendNames(ctrl.backends ?? [])
        setPausedAgentsDraft(ctrl.controls.paused_agents)
        setPausedBackendsDraft(new Set(ctrl.controls.paused_backends ?? []))
        setPausedConsumersDraft(new Set(ctrl.controls.paused_consumers ?? []))
        setPausedCategoriesDraft({ ...(ctrl.controls.paused_categories ?? {}) })
        setHealth(bh.backends ?? [])
        setHygiene(ch.consumers ?? [])
        setCategories(cats.categories ?? [])
        setAllConsumers(cs.consumers ?? [])
        setError(null)
        setState('ready')
      })
      .catch((cause: unknown) => {
        if (!live) return
        setError(cause instanceof Error ? cause.message : 'Could not load security data.')
        setState('error')
      })

    return () => {
      live = false
    }
  }, [])

  useEffect(() => load(), [load])

  const healthByBackend = useMemo(() => {
    const map: Record<string, BackendHealth> = {}
    for (const b of health) map[b.backend] = b
    return map
  }, [health])

  const appliedPausedBackends = useMemo(() => controls?.paused_backends ?? [], [controls])
  const appliedPausedConsumers = useMemo(() => controls?.paused_consumers ?? [], [controls])
  const appliedPausedCategories = useMemo(() => controls?.paused_categories ?? {}, [controls])
  const consumerName = useMemo(() => {
    const names: Record<string, string> = {}
    for (const c of allConsumers) names[c.consumer_id] = c.name
    return names
  }, [allConsumers])
  const categoryLabel = useMemo(() => {
    const labels: Record<string, string> = {}
    for (const c of categories) labels[c.id] = c.display_name
    return labels
  }, [categories])
  const consumerOptions = useMemo<DropdownOption[]>(
    () => [
      { value: '', label: 'Choose a consumer…' },
      ...allConsumers.map((c) => ({ value: c.consumer_id, label: `${c.name} (${c.type})` })),
    ],
    [allConsumers],
  )
  const agentsChanged = controls ? pausedAgentsDraft !== controls.paused_agents : false
  const addedBackends = useMemo(
    () => [...pausedBackendsDraft].filter((b) => !appliedPausedBackends.includes(b)),
    [pausedBackendsDraft, appliedPausedBackends],
  )
  const removedBackends = useMemo(
    () => appliedPausedBackends.filter((b) => !pausedBackendsDraft.has(b)),
    [appliedPausedBackends, pausedBackendsDraft],
  )
  const addedConsumers = useMemo(
    () => [...pausedConsumersDraft].filter((id) => !appliedPausedConsumers.includes(id)),
    [pausedConsumersDraft, appliedPausedConsumers],
  )
  const removedConsumers = useMemo(
    () => appliedPausedConsumers.filter((id) => !pausedConsumersDraft.has(id)),
    [appliedPausedConsumers, pausedConsumersDraft],
  )

  // Category pauses are keyed by consumer, each holding a list of category
  // ids — flattened to (consumerId, categoryId) pairs so the diff/dirty logic
  // below can treat them the same way as the flat backend/consumer lists.
  const flattenCategoryPairs = useCallback(
    (map: Record<string, string[]>) =>
      Object.entries(map).flatMap(([consumerId, categoryIds]) => categoryIds.map((categoryId) => ({ consumerId, categoryId }))),
    [],
  )
  const pairKey = useCallback((p: { consumerId: string; categoryId: string }) => `${p.consumerId} ${p.categoryId}`, [])
  const appliedCategoryPairs = useMemo(() => flattenCategoryPairs(appliedPausedCategories), [appliedPausedCategories, flattenCategoryPairs])
  const draftCategoryPairs = useMemo(() => flattenCategoryPairs(pausedCategoriesDraft), [pausedCategoriesDraft, flattenCategoryPairs])
  const appliedCategoryPairKeys = useMemo(() => new Set(appliedCategoryPairs.map(pairKey)), [appliedCategoryPairs, pairKey])
  const draftCategoryPairKeys = useMemo(() => new Set(draftCategoryPairs.map(pairKey)), [draftCategoryPairs, pairKey])
  const addedCategoryPairs = useMemo(
    () => draftCategoryPairs.filter((p) => !appliedCategoryPairKeys.has(pairKey(p))),
    [draftCategoryPairs, appliedCategoryPairKeys, pairKey],
  )
  const removedCategoryPairs = useMemo(
    () => appliedCategoryPairs.filter((p) => !draftCategoryPairKeys.has(pairKey(p))),
    [appliedCategoryPairs, draftCategoryPairKeys, pairKey],
  )

  const dirty =
    agentsChanged || addedBackends.length > 0 || removedBackends.length > 0 ||
    addedConsumers.length > 0 || removedConsumers.length > 0 ||
    addedCategoryPairs.length > 0 || removedCategoryPairs.length > 0
  const containmentActive =
    (controls?.paused_agents ?? false) || appliedPausedBackends.length > 0 || appliedPausedConsumers.length > 0 ||
    appliedCategoryPairs.length > 0
  const escalating =
    (agentsChanged && pausedAgentsDraft) || addedBackends.length > 0 || addedConsumers.length > 0 ||
    addedCategoryPairs.length > 0

  const toggleBackendDraft = useCallback((name: string) => {
    setPausedBackendsDraft((prev) => {
      const next = new Set(prev)
      if (next.has(name)) next.delete(name)
      else next.add(name)
      return next
    })
  }, [])

  const toggleConsumerDraft = useCallback((consumerId: string) => {
    setPausedConsumersDraft((prev) => {
      const next = new Set(prev)
      if (next.has(consumerId)) next.delete(consumerId)
      else next.add(consumerId)
      return next
    })
  }, [])

  const toggleCategoryDraft = useCallback((consumerId: string, categoryId: string) => {
    setPausedCategoriesDraft((prev) => {
      const current = new Set(prev[consumerId] ?? [])
      if (current.has(categoryId)) current.delete(categoryId)
      else current.add(categoryId)
      const next = { ...prev }
      if (current.size === 0) delete next[consumerId]
      else next[consumerId] = [...current]
      return next
    })
  }, [])

  const toggleAllAgentsDraft = useCallback(
    (paused: boolean) => {
      setPausedAgentsDraft(paused)
      setPausedBackendsDraft(paused ? new Set(backendNames) : new Set())
    },
    [backendNames],
  )

  const discardDraft = useCallback(() => {
    if (!controls) return
    setPausedAgentsDraft(controls.paused_agents)
    setPausedBackendsDraft(new Set(controls.paused_backends ?? []))
    setPausedConsumersDraft(new Set(controls.paused_consumers ?? []))
    setPausedCategoriesDraft({ ...(controls.paused_categories ?? {}) })
  }, [controls])

  const applyControls = useCallback(async () => {
    setApplying(true)
    try {
      const result = await setAdminControls({
        paused_agents: pausedAgentsDraft,
        paused_backends: [...pausedBackendsDraft],
        paused_consumers: [...pausedConsumersDraft],
        paused_categories: pausedCategoriesDraft,
      })
      setControls(result.controls)
      setPausedAgentsDraft(result.controls.paused_agents)
      setPausedBackendsDraft(new Set(result.controls.paused_backends ?? []))
      setPausedConsumersDraft(new Set(result.controls.paused_consumers ?? []))
      setPausedCategoriesDraft({ ...(result.controls.paused_categories ?? {}) })
      toast.success('Controls applied')
      setConfirmOpen(false)
    } catch (cause) {
      toast.error(cause instanceof ApiError ? cause.message : 'Could not apply controls.')
    } finally {
      setApplying(false)
    }
  }, [pausedAgentsDraft, pausedBackendsDraft, pausedConsumersDraft, pausedCategoriesDraft, toast])

  const openRowAction = useCallback((consumer: CredentialHygieneConsumer, action: RowActionKind) => {
    setMintedKey(null)
    setRowAction({ consumer, action })
  }, [])

  const closeRowAction = useCallback(() => {
    setRowAction(null)
    setMintedKey(null)
    load()
  }, [load])

  const confirmRowAction = useCallback(async () => {
    if (!rowAction) return
    setRowSubmitting(true)
    try {
      if (rowAction.action === 'rotate') {
        const result = await rotateConsumerKey(rowAction.consumer.consumer_id)
        setMintedKey(result.api_key)
        toast.success('Key rotated')
      } else {
        await setConsumerStatus(rowAction.consumer.consumer_id, rowAction.action === 'disable' ? 'disabled' : 'active')
        toast.success(rowAction.action === 'disable' ? 'Consumer disabled' : 'Consumer reactivated')
        setRowAction(null)
        load()
      }
    } catch (cause) {
      toast.error(cause instanceof ApiError ? cause.message : 'Could not complete that action.')
    } finally {
      setRowSubmitting(false)
    }
  }, [rowAction, toast, load])

  const hygieneColumns = useMemo<Column<CredentialHygieneConsumer>[]>(
    () => [
      {
        key: 'principal',
        header: 'Principal',
        render: (c) => (
          <>
            <div>{c.name}</div>
            <div className="security-role">{c.role}</div>
          </>
        ),
      },
      { key: 'type', header: 'Type', width: '6rem', render: (c) => <Badge subtle>{c.type}</Badge> },
      {
        key: 'status',
        header: 'Status',
        width: '7rem',
        render: (c) => (
          <>
            <Badge tone={c.status === 'active' ? 'ok' : 'danger'} dot>
              {c.status}
            </Badge>
            {appliedPausedConsumers.includes(c.consumer_id) && (
              <div>
                <Badge tone="warn" subtle>
                  paused
                </Badge>
              </div>
            )}
          </>
        ),
      },
      {
        key: 'last_used',
        header: 'Last used',
        width: '9rem',
        muted: true,
        nowrap: true,
        render: (c) =>
          c.last_used ? <span title={formatWhen(c.last_used)}>{formatRelative(c.last_used)}</span> : 'never',
      },
      {
        key: 'key_age',
        header: 'Key age',
        width: '6rem',
        muted: true,
        render: (c) => (c.key_age_days != null ? `${Math.round(c.key_age_days)}d` : '—'),
      },
      {
        key: 'flags',
        header: 'Flags',
        render: (c) =>
          c.flags.length === 0 ? (
            <Badge tone="ok" subtle>
              ok
            </Badge>
          ) : (
            <div className="security-flags">
              {c.flags.map((f) => (
                <Badge key={f} tone="danger" subtle>
                  {f}
                </Badge>
              ))}
            </div>
          ),
      },
      {
        key: 'actions',
        header: '',
        srHeader: 'Actions',
        width: '12rem',
        align: 'right',
        render: (c) => (
          <div className="security-row-actions">
            <Button size="sm" variant="ghost" onClick={() => openRowAction(c, 'rotate')}>
              Rotate key
            </Button>
            <Button
              size="sm"
              variant={c.status === 'active' ? 'danger' : 'ghost'}
              onClick={() => openRowAction(c, c.status === 'active' ? 'disable' : 'reactivate')}
            >
              {c.status === 'active' ? 'Disable' : 'Reactivate'}
            </Button>
          </div>
        ),
      },
    ],
    [openRowAction, appliedPausedConsumers],
  )

  if (state === 'loading') {
    return (
      <PageShell className="security">
        <Skeleton height="9rem" index={0} />
        <Skeleton height="12rem" index={1} />
        <Skeleton height="14rem" index={2} />
      </PageShell>
    )
  }

  if (state === 'error') {
    return (
      <PageShell className="security">
        <Card title="Couldn't load security data" accent="danger">
          <p className="security-error-body">{error}</p>
          <Button variant="ghost" onClick={load}>
            Try again
          </Button>
        </Card>
      </PageShell>
    )
  }

  return (
    <PageShell className="security">
      <Card
        title="Break-glass controls"
        description="Incident containment enforced on every governed call — takes effect immediately."
        actions={
          <Badge tone={containmentActive ? 'warn' : 'ok'} dot pulse={!containmentActive}>
            {containmentActive ? 'containment active' : 'normal'}
          </Badge>
        }
      >
        <div className="security-stack">
          <Switch
            checked={pausedAgentsDraft}
            onChange={toggleAllAgentsDraft}
            label="Pause all agents"
            description="Blocks every API-key (agent) caller, on every backend. Humans and admins are unaffected."
            tone="danger"
          />

          <div className="security-subsection">
            <p className="ui-eyebrow security-subsection-label">Pause specific backends</p>
            <div className="security-backend-grid">
              {backendNames.map((name, i) => {
                const h = healthByBackend[name]
                return (
                  <div className="security-backend-row" key={name} style={{ '--ui-i': i } as CSSProperties}>
                    <span className="security-backend-name">
                      {h && (
                        <Badge tone={backendTone(h.status)} dot pulse={h.status === 'healthy'} subtle>
                          {h.status}
                        </Badge>
                      )}
                      <span className="ui-mono">{name}</span>
                    </span>
                    <Switch
                      size="sm"
                      checked={pausedBackendsDraft.has(name)}
                      onChange={() => toggleBackendDraft(name)}
                      label={
                        <Badge
                          tone={pausedBackendsDraft.has(name) ? 'danger' : 'ok'}
                          dot
                          subtle
                        >
                          {pausedBackendsDraft.has(name) ? 'Blocked' : 'Allowed'}
                        </Badge>
                      }
                      tone="danger"
                    />
                  </div>
                )
              })}
            </div>
          </div>

          <div className="security-subsection">
            <p className="ui-eyebrow security-subsection-label">Pause specific consumers</p>
            <p className="security-subsection-hint">
              Blocks one consumer everywhere — agent or human — regardless of the switches above. Independent of
              disabling a consumer on the Consumers page: this is instantly reversible and meant for containment,
              not credential revocation.
            </p>
            {allConsumers.length === 0 ? (
              <p className="security-note">No consumers yet.</p>
            ) : (
              <div className="security-consumer-grid">
                {allConsumers.map((c) => (
                  <Chip
                    key={c.consumer_id}
                    selected={pausedConsumersDraft.has(c.consumer_id)}
                    onClick={() => toggleConsumerDraft(c.consumer_id)}
                    title={`${c.name} — ${c.type}`}
                  >
                    {c.name}
                  </Chip>
                ))}
              </div>
            )}
          </div>

          <div className="security-subsection">
            <p className="ui-eyebrow security-subsection-label">Pause specific categories for a consumer</p>
            <p className="security-subsection-hint">
              Temporarily narrows one consumer's access to just these categories' tools, without touching their
              permanent category grant on the Consumers page.
            </p>
            <Field label="Consumer">
              {(fieldProps) => (
                <Dropdown {...fieldProps} value={categoryEditConsumerId} onChange={setCategoryEditConsumerId} options={consumerOptions} />
              )}
            </Field>
            {categoryEditConsumerId && (
              <div className="security-consumer-grid">
                {categories.map((cat) => (
                  <Chip
                    key={cat.id}
                    selected={(pausedCategoriesDraft[categoryEditConsumerId] ?? []).includes(cat.id)}
                    onClick={() => toggleCategoryDraft(categoryEditConsumerId, cat.id)}
                    title={cat.display_name}
                  >
                    {cat.display_name}
                  </Chip>
                ))}
              </div>
            )}
            {Object.keys(pausedCategoriesDraft).length > 0 && (
              <div className="security-paused-categories-summary">
                {Object.entries(pausedCategoriesDraft).map(([consumerId, categoryIds]) => (
                  <p className="security-note" key={consumerId}>
                    <strong>{consumerName[consumerId] ?? consumerId}:</strong>{' '}
                    {categoryIds.map((id) => categoryLabel[id] ?? id).join(', ')}
                  </p>
                ))}
              </div>
            )}
          </div>

          {dirty && (
            <div className="security-dirty-bar">
              <span className="security-dirty-text">Unsaved containment changes</span>
              <div className="security-dirty-actions">
                <Button variant="ghost" size="sm" onClick={discardDraft}>
                  Discard
                </Button>
                <Button size="sm" variant={escalating ? 'danger' : 'primary'} onClick={() => setConfirmOpen(true)}>
                  Apply controls
                </Button>
              </div>
            </div>
          )}
        </div>
      </Card>

      <Card
        title="Backend health"
        description="Live liveness probe per data-domain backend, combined with the recent-traffic error rate."
        actions={
          <Button variant="quiet" size="sm" onClick={load}>
            Refresh
          </Button>
        }
      >
        {health.length === 0 ? (
          <EmptyState title="No backends" />
        ) : (
          <div className="security-health-grid">
            {health.map((b, i) => {
              const paused = appliedPausedBackends.includes(b.backend)
              return (
                <Card
                  key={b.backend}
                  className="security-health-card"
                  style={{ '--ui-i': i } as CSSProperties}
                  accent={paused ? 'warn' : backendAccent(b.status)}
                  title={<span className="ui-mono">{b.backend}</span>}
                  actions={
                    <Badge tone={backendTone(b.status)} dot pulse={b.status === 'healthy'} subtle>
                      {b.status}
                    </Badge>
                  }
                >
                  {paused && <p className="security-health-paused">Paused by admin — not an outage.</p>}
                  <p className="security-health-line">
                    {b.probe_ok === false
                      ? `Probe failed: ${b.probe_error || 'unreachable'}`
                      : b.probe_latency_ms != null
                        ? `Probe ${Math.round(b.probe_latency_ms)}ms`
                        : 'Not probed'}
                  </p>
                  <p className="security-health-line security-health-line--muted">
                    {b.calls.toLocaleString()} calls · {(b.error_rate * 100).toFixed(0)}% errors · last ok{' '}
                    {b.last_ok ? formatRelative(b.last_ok) : '—'}
                  </p>
                </Card>
              )
            })}
          </div>
        )}
      </Card>

      <Card
        title="API-key hygiene"
        description="Dormant, never-used, or overdue-for-rotation keys — derived from call history and rotation audit events."
        flush
      >
        <DataTable
          columns={hygieneColumns}
          rows={hygiene}
          rowKey={(c) => c.consumer_id}
          caption="API-key consumers and their hygiene flags"
          empty={<EmptyState title="No API-key consumers" />}
        />
      </Card>

      <Drawer
        open={confirmOpen}
        onClose={() => setConfirmOpen(false)}
        eyebrow="Break-glass"
        title="Apply containment changes?"
        footer={
          <>
            <Button variant="ghost" onClick={() => setConfirmOpen(false)} disabled={applying}>
              Cancel
            </Button>
            <Button variant={escalating ? 'danger' : 'primary'} onClick={applyControls} loading={applying}>
              Apply
            </Button>
          </>
        }
      >
        <div className="security-decision-body">
          {agentsChanged && (
            <p className="security-diff-row">
              <Badge tone={pausedAgentsDraft ? 'danger' : 'ok'} dot>
                {pausedAgentsDraft ? 'Pausing' : 'Resuming'}
              </Badge>
              all agents (API-key callers)
            </p>
          )}
          {addedBackends.map((name) => (
            <p className="security-diff-row" key={`add-${name}`}>
              <Badge tone="danger" dot>
                Blocking
              </Badge>
              <span className="ui-mono">{name}</span>
            </p>
          ))}
          {removedBackends.map((name) => (
            <p className="security-diff-row" key={`rm-${name}`}>
              <Badge tone="ok" dot>
                Resuming
              </Badge>
              <span className="ui-mono">{name}</span>
            </p>
          ))}
          {addedConsumers.map((id) => (
            <p className="security-diff-row" key={`add-consumer-${id}`}>
              <Badge tone="danger" dot>
                Pausing
              </Badge>
              {consumerName[id] ?? id}
            </p>
          ))}
          {removedConsumers.map((id) => (
            <p className="security-diff-row" key={`rm-consumer-${id}`}>
              <Badge tone="ok" dot>
                Resuming
              </Badge>
              {consumerName[id] ?? id}
            </p>
          ))}
          {addedCategoryPairs.map((p) => (
            <p className="security-diff-row" key={`add-cat-${pairKey(p)}`}>
              <Badge tone="danger" dot>
                Pausing
              </Badge>
              {categoryLabel[p.categoryId] ?? p.categoryId} for {consumerName[p.consumerId] ?? p.consumerId}
            </p>
          ))}
          {removedCategoryPairs.map((p) => (
            <p className="security-diff-row" key={`rm-cat-${pairKey(p)}`}>
              <Badge tone="ok" dot>
                Resuming
              </Badge>
              {categoryLabel[p.categoryId] ?? p.categoryId} for {consumerName[p.consumerId] ?? p.consumerId}
            </p>
          ))}
          <p className="security-note">This takes effect on the very next governed call — no rollout delay.</p>
        </div>
      </Drawer>

      <Drawer
        open={rowAction != null}
        onClose={closeRowAction}
        eyebrow="API-key hygiene"
        title={
          rowAction?.action === 'rotate'
            ? mintedKey
              ? 'Key rotated'
              : 'Rotate this key?'
            : rowAction?.action === 'disable'
              ? 'Disable this consumer?'
              : 'Reactivate this consumer?'
        }
        footer={
          mintedKey ? (
            <Button onClick={closeRowAction}>Done</Button>
          ) : (
            <>
              <Button variant="ghost" onClick={() => setRowAction(null)} disabled={rowSubmitting}>
                Cancel
              </Button>
              <Button
                variant={rowAction?.action === 'reactivate' ? 'primary' : 'danger'}
                onClick={confirmRowAction}
                loading={rowSubmitting}
              >
                {rowAction?.action === 'rotate' ? 'Rotate key' : rowAction?.action === 'disable' ? 'Disable' : 'Reactivate'}
              </Button>
            </>
          )
        }
      >
        {rowAction && (
          <div className="security-decision-body">
            <KeyValue
              items={[
                { label: 'Principal', value: rowAction.consumer.name },
                { label: 'Role', value: rowAction.consumer.role },
                { label: 'Type', value: rowAction.consumer.type },
                { label: 'Flags', value: rowAction.consumer.flags.join(', ') || 'none', wide: true },
              ]}
            />
            {rowAction.action === 'rotate' && !mintedKey && (
              <p className="security-note">The current key stops working the moment this completes.</p>
            )}
            {rowAction.action === 'disable' && (
              <p className="security-note">
                Every governed call from this consumer will be rejected until reactivated.
              </p>
            )}
            {mintedKey && (
              <SecretKey
                value={mintedKey}
                label="New API key"
                hint="Copy it now — store it safely. This is the only time it's shown."
              />
            )}
          </div>
        )}
      </Drawer>
    </PageShell>
  )
}

export default SecurityPage
