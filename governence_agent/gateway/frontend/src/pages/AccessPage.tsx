import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Avatar,
  Badge,
  Button,
  Card,
  DataTable,
  EmptyState,
  KeyValue,
  PageShell,
  SecretKey,
  Skeleton,
  useToast,
  type BadgeTone,
  type Column,
} from '../components/ui'
import AccessCard from '../components/access/AccessCard'
import RequestPicker from '../components/access/RequestPicker'
import {
  ApiError,
  getCategoryCatalog,
  getMyAccess,
  getMyDenials,
  requestAccess,
  rotateMyKey,
  type AccessRequestRecord,
  type CategoryCatalogEntry,
  type MyAccess,
  type MyDenial,
} from '../lib/api'
import { describeRequest } from '../lib/describeRequest'
import type { PageProps } from './types'
import './AccessPage.css'

/**
 * My Access — ports `renderAccess()`: this account's role/status, its resolved
 * grant per backend, recent denials with one-click requests, the self-service
 * request-access picker, and the API key.
 *
 * The one thing rebuilt rather than carried over verbatim is the account
 * header. The legacy version put status/role/type/department in one row of
 * identical pills — the actual ask here ("clearly state the role") got lost in
 * a wall of same-weight badges. Role now gets its own labeled slot in a
 * KeyValue grid, separate from status, so it reads as the answer to "what is
 * this account allowed to do" rather than one tag among five.
 */

function statusTone(status: string): BadgeTone {
  if (status === 'active') return 'ok'
  if (status === 'pending') return 'warn'
  return 'danger'
}

function requestStatusTone(status: string): BadgeTone {
  if (status === 'approved') return 'ok'
  if (status === 'pending') return 'warn'
  return 'danger'
}

const EMPTY_ACCESS: MyAccess = {
  name: '',
  full_name: '',
  department: '',
  status: '',
  role: '',
  type: '',
  categories: [],
  effective_categories: [],
  has_key: false,
  access: {},
  requests: [],
}

function AccessPage(_props: PageProps) {
  const toast = useToast()

  const [account, setAccount] = useState<MyAccess>(EMPTY_ACCESS)
  const [categories, setCategories] = useState<CategoryCatalogEntry[]>([])
  const [denials, setDenials] = useState<MyDenial[]>([])
  const [state, setState] = useState<'loading' | 'ready' | 'error'>('loading')
  const [error, setError] = useState<string | null>(null)

  const [picked, setPicked] = useState<Record<string, Set<string>>>({})
  const [justification, setJustification] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [minting, setMinting] = useState(false)
  const [mintedKey, setMintedKey] = useState<string | null>(null)

  const load = useCallback(() => {
    let live = true
    setState('loading')

    Promise.all([getMyAccess(), getCategoryCatalog().catch(() => ({ categories: [] }))])
      .then(([access, catalog]) => {
        if (!live) return
        setAccount(access)
        setCategories(catalog.categories ?? [])
        setError(null)
        setState('ready')
      })
      .catch((cause: unknown) => {
        if (!live) return
        setError(cause instanceof Error ? cause.message : 'Could not load your access.')
        setState('error')
      })

    // Best-effort and independent of the load above, matching the legacy
    // panel: a denials failure shouldn't block the rest of the page, and an
    // empty result just means the card doesn't render.
    getMyDenials()
      .then((result) => {
        if (live) setDenials(result.denials ?? [])
      })
      .catch(() => {
        if (live) setDenials([])
      })

    return () => {
      live = false
    }
  }, [])

  useEffect(() => load(), [load])

  const backendLabel = useMemo(() => {
    const labels: Record<string, string> = {}
    for (const category of categories) labels[category.backend] = category.display_name
    return labels
  }, [categories])

  const accessEntries = useMemo(
    () =>
      Object.entries(account.access).sort(([a], [b]) =>
        (backendLabel[a] ?? a).localeCompare(backendLabel[b] ?? b),
      ),
    [account.access, backendLabel],
  )

  const myAccessRequests = useMemo(
    () => account.requests.filter((r) => r.kind === 'access'),
    [account.requests],
  )

  // Tools already asked for in a not-yet-decided request, per backend — greyed
  // out (and marked "(pending)") in the picker so a second click can't queue
  // the same ask twice.
  const pendingByBackend = useMemo(() => {
    const pending: Record<string, Set<string>> = {}
    for (const request of myAccessRequests) {
      if (request.status !== 'pending' || !request.selections) continue
      for (const selection of request.selections) {
        const set = (pending[selection.backend] ??= new Set())
        for (const tool of selection.tools) set.add(tool)
      }
    }
    return pending
  }, [myAccessRequests])

  const grantedToolNames = useCallback(
    (backend: string) => new Set((account.access[backend]?.tools ?? []).map((t) => t.name)),
    [account.access],
  )

  const availableByCategory = useMemo(
    () =>
      categories
        .map((category) => ({
          category,
          available: category.tools.filter((t) => !grantedToolNames(category.backend).has(t.name)),
        }))
        .filter((entry) => entry.available.length > 0),
    [categories, grantedToolNames],
  )

  const toggleTool = useCallback((categoryId: string, toolName: string) => {
    setPicked((prev) => {
      const next = { ...prev }
      const set = new Set(next[categoryId] ?? [])
      if (set.has(toolName)) set.delete(toolName)
      else set.add(toolName)
      next[categoryId] = set
      return next
    })
  }, [])

  const submitRequest = useCallback(async () => {
    const selections = Object.entries(picked)
      .map(([category, tools]) => ({ category, tools: [...tools] }))
      .filter((s) => s.tools.length > 0)
    if (selections.length === 0) {
      toast.warn('Pick at least one item')
      return
    }
    setSubmitting(true)
    try {
      await requestAccess(selections, justification)
      toast.success('Request submitted')
      setPicked({})
      setJustification('')
      load()
    } catch (cause) {
      toast.error(cause instanceof ApiError ? cause.message : 'Could not submit that request')
    } finally {
      setSubmitting(false)
    }
  }, [picked, justification, toast, load])

  const requestFromDenial = useCallback(
    async (denial: MyDenial) => {
      if (!denial.category) return
      try {
        await requestAccess([{ category: denial.category, tools: [denial.canonical] }], 'Requested from a denied attempt')
        toast.success('Request submitted')
        load()
      } catch (cause) {
        toast.error(cause instanceof ApiError ? cause.message : 'Could not submit that request')
      }
    },
    [toast, load],
  )

  const mintKey = useCallback(async () => {
    setMinting(true)
    try {
      const result = await rotateMyKey()
      setMintedKey(result.api_key)
      setAccount((prev) => ({ ...prev, has_key: true }))
      toast.success('Key minted — copy it now')
    } catch (cause) {
      toast.error(cause instanceof ApiError ? cause.message : 'Could not mint a key')
    } finally {
      setMinting(false)
    }
  }, [toast])

  const denialColumns = useMemo<Column<MyDenial>[]>(
    () => [
      {
        key: 'what',
        header: 'What you tried to do',
        render: (d) => (
          <>
            <div>{d.description}</div>
            <div className="ui-mono access-denial-tool">{d.tool}</div>
          </>
        ),
      },
      { key: 'attempts', header: 'Attempts', width: '6rem', numeric: true },
      {
        key: 'action',
        header: '',
        srHeader: 'Request access',
        width: '9rem',
        align: 'right',
        render: (d) =>
          d.category ? (
            <Button size="sm" onClick={() => requestFromDenial(d)}>
              Request access
            </Button>
          ) : (
            <span className="access-denial-nocat">no category</span>
          ),
      },
    ],
    [requestFromDenial],
  )

  const requestColumns = useMemo<Column<AccessRequestRecord>[]>(
    () => [
      { key: 'what', header: 'Requested', render: (r) => describeRequest(r, categories) },
      {
        key: 'status',
        header: 'Status',
        width: '7rem',
        render: (r) => <Badge tone={requestStatusTone(r.status)}>{r.status}</Badge>,
      },
      {
        key: 'justification',
        header: 'Justification',
        muted: true,
        render: (r) => r.justification || '—',
      },
    ],
    [categories],
  )

  if (state === 'loading') {
    return (
      <PageShell className="access">
        <Skeleton height="6rem" index={0} />
        <Skeleton height="8rem" index={1} />
        <Skeleton height="10rem" index={2} />
      </PageShell>
    )
  }

  if (state === 'error') {
    return (
      <PageShell className="access">
        <Card title="Couldn't load your access" accent="danger">
          <p className="access-error-body">{error}</p>
          <Button variant="ghost" onClick={load}>
            Try again
          </Button>
        </Card>
      </PageShell>
    )
  }

  const displayName = account.full_name || account.name

  return (
    <PageShell className="access">
      {/* Account and the API key are both short, identity-shaped cards — paired
          side by side on a wide screen rather than each sitting full-width
          with mostly empty space to the right. */}
      <div className="access-top-row">
        <Card title="Account" description="Your session identity and standing.">
          <div className="access-stack">
            <div className="access-identity">
              <Avatar name={displayName} size="lg" />
              <div className="access-identity-text">
                <p className="access-identity-name">{displayName}</p>
                {account.full_name && <p className="access-identity-username">@{account.name}</p>}
              </div>
              <Badge tone={statusTone(account.status)} dot>
                {account.status}
              </Badge>
            </div>

            <KeyValue
              columns={2}
              items={[
                { label: 'Role', value: <Badge tone="accent">{account.role}</Badge> },
                { label: 'Account type', value: account.type },
                ...(account.department ? [{ label: 'Department', value: account.department }] : []),
              ]}
            />
          </div>
        </Card>

        <Card
          title="API key"
          description={`${account.has_key ? 'A key is issued.' : 'No key yet.'} Minting shows it once — store it safely; it replaces any previous key.`}
        >
          <div className="access-stack">
            <Button onClick={mintKey} disabled={account.status !== 'active'} loading={minting}>
              {account.has_key ? 'Rotate key' : 'Mint key'}
            </Button>
            {mintedKey && (
              <SecretKey
                value={mintedKey}
                label="API key"
                hint="Copy it now — store it safely. Minting again replaces this key."
              />
            )}
          </div>
        </Card>
      </div>

      <Card
        title="Granted access"
        description={`What you can currently do${account.department ? ' (incl. via your department)' : ''}.`}
      >
        {accessEntries.length === 0 ? (
          <EmptyState title="No access granted yet" />
        ) : (
          <div className="access-groups">
            {accessEntries.map(([backend, grant]) => (
              // Open by default -- collapsing is an option, not the starting
              // state; nothing that was visible before this should now be
              // hidden until someone explicitly closes a section.
              <details className="access-group access-disclosure" key={backend} open>
                <summary className="access-group-head">
                  <h3>{backendLabel[backend] ?? backend}</h3>
                  <div className="access-group-levels">
                    {grant.levels.map((level) => (
                      <Badge key={level} tone="info" subtle>
                        {level}
                      </Badge>
                    ))}
                  </div>
                </summary>
                {grant.tools.length > 0 ? (
                  <div className="access-tool-grid">
                    {grant.tools.map((tool) => (
                      <AccessCard key={tool.name} tool={tool} />
                    ))}
                  </div>
                ) : (
                  <p className="access-group-empty">no tools</p>
                )}
              </details>
            ))}
          </div>
        )}
      </Card>

      {denials.length > 0 && (
        <Card
          title="Recent denials"
          description="Calls blocked because you don't have the grant yet — request it in one click."
          flush
        >
          <DataTable columns={denialColumns} rows={denials} rowKey={(d) => d.tool} dense />
        </Card>
      )}

      <Card
        title="Request more access"
        description="Open a category to pick the specific things you need."
      >
        <div className="access-stack">
          {availableByCategory.length === 0 ? (
            <EmptyState title="You already have every available tool" compact />
          ) : (
            <RequestPicker
              availableByCategory={availableByCategory}
              pendingByBackend={pendingByBackend}
              picked={picked}
              onToggleTool={toggleTool}
              justification={justification}
              onJustificationChange={setJustification}
              onSubmit={submitRequest}
              submitting={submitting}
              submitDisabled={account.status !== 'active'}
            />
          )}

          <DataTable
            columns={requestColumns}
            rows={myAccessRequests}
            rowKey={(r) => r.id}
            dense
            empty={<EmptyState title="No requests yet" compact />}
          />
        </div>
      </Card>
    </PageShell>
  )
}

export default AccessPage
