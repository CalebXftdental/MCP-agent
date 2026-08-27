import { useCallback, useEffect, useMemo, useState } from 'react'
import { Badge, Button, Card, EmptyState, Field, Input, PageShell, Skeleton, useToast } from '../components/ui'
import WorkflowCatalogTile from '../components/workflow/WorkflowCatalogTile'
import {
  ApiError,
  getCategoryCatalog,
  getMyAccess,
  getWorkflowSearch,
  listWorkflows,
  requestAccess,
  type CategoryCatalogEntry,
  type MyAccess,
  type WorkflowTemplate,
} from '../lib/api'
import type { PageProps } from './types'
import './WorkflowStorePage.css'

/**
 * Workflow Store — the discovery-and-request-access-facing catalog view,
 * separate from Workflows (the builder-facing "run what I already have"
 * view). Same `WorkflowCatalogTile` grid, but every tile also carries an
 * access verdict: templates the viewer can already run render normally,
 * ones they can't render greyed with a "Request access" action that fires
 * the same `requestAccess()` the My Access request picker uses — no need to
 * open that whole picker UI since the template already names exactly which
 * categories (and, in turn, which tools) are missing.
 *
 * Search is instant client-side substring filtering by default; "Smart
 * search" is an explicit opt-in that re-ranks the SAME full list via the
 * reranker-backed `/dashboard/workflow-search` endpoint instead of filtering
 * it, for a paraphrased query that doesn't share literal keywords with any
 * template's name/description. It fails soft to a substring order server-side
 * if the reranker isn't configured — surfaced here as a toast, not a crash.
 */

const MIN_SMART_QUERY_LEN = 2

function WorkflowStorePage({}: PageProps) {
  const toast = useToast()

  const [templates, setTemplates] = useState<WorkflowTemplate[]>([])
  const [myAccess, setMyAccess] = useState<MyAccess | null>(null)
  const [categoryCatalog, setCategoryCatalog] = useState<CategoryCatalogEntry[]>([])
  const [loadState, setLoadState] = useState<'loading' | 'ready' | 'error'>('loading')

  const [search, setSearch] = useState('')
  const [smartOrder, setSmartOrder] = useState<string[] | null>(null)
  const [smartBusy, setSmartBusy] = useState(false)

  const [requestBusyId, setRequestBusyId] = useState<string | null>(null)
  const [requestedIds, setRequestedIds] = useState<Set<string>>(new Set())

  const load = useCallback(() => {
    setLoadState('loading')
    Promise.all([listWorkflows(), getMyAccess(), getCategoryCatalog()])
      .then(([workflowsResult, access, catalog]) => {
        setTemplates(workflowsResult.workflows)
        setMyAccess(access)
        setCategoryCatalog(catalog.categories)
        setLoadState('ready')
      })
      .catch(() => setLoadState('error'))
  }, [])

  useEffect(load, [load])

  const categoryById = useMemo(() => {
    const map = new Map<string, CategoryCatalogEntry>()
    for (const c of categoryCatalog) map.set(c.id, c)
    return map
  }, [categoryCatalog])

  const effectiveCategories = useMemo(() => new Set(myAccess?.effective_categories ?? []), [myAccess])

  // Admin is an unconditional bypass on the backend (policy/resolve.py's
  // EffectiveGrant(all_tools=True)), not a literal grant of every category --
  // `/dashboard/my-access` correctly returns `effective_categories: []` for an
  // admin (nothing to enumerate), which would otherwise make every template
  // look "missing everything." Never gate on category membership for admin.
  const isAdmin = myAccess?.role === 'admin'

  const missingFor = useCallback(
    (template: WorkflowTemplate) => (isAdmin ? [] : template.requiredCategories.filter((c) => !effectiveCategories.has(c))),
    [isAdmin, effectiveCategories],
  )

  /** One clause per missing category, each naming its actual tools (or, for a
   *  backend-less gateway permission like "files", its data domains) rather
   *  than just the bare category id — "Requires access to: get_sales_price,
   *  get_gl_period_summary (category: Finance); ... (category: ...)". */
  const accessNoteFor = useCallback(
    (missing: string[]) => {
      if (!missing.length) return ''
      const clauses = missing.map((id) => {
        const cat = categoryById.get(id)
        const label = cat?.display_name ?? id
        const toolNames = (cat?.tools ?? []).map((t) => t.name)
        const domains = cat?.data_domains ?? []
        const what = toolNames.length ? toolNames.join(', ') : domains.length ? domains.join(', ') : label
        return `${what} (category: ${label})`
      })
      return `Requires access to: ${clauses.join('; ')}`
    },
    [categoryById],
  )

  const requestForTemplate = useCallback(
    async (template: WorkflowTemplate) => {
      const missing = missingFor(template)
      if (!missing.length) return
      setRequestBusyId(template.templateId)
      try {
        const selections = missing.map((id) => ({
          category: id,
          tools: (categoryById.get(id)?.tools ?? []).map((t) => t.name),
        }))
        await requestAccess(selections, 'Requested from Workflow Store')
        toast.success(`Access requested for ${template.displayName}`, {
          detail: 'An admin needs to approve it — check My Access for status.',
        })
        setRequestedIds((prev) => new Set(prev).add(template.templateId))
      } catch (cause) {
        toast.error(cause instanceof ApiError ? cause.message : 'Could not submit that access request.')
      } finally {
        setRequestBusyId(null)
      }
    },
    [missingFor, categoryById, toast],
  )

  const runSmartSearch = useCallback(async () => {
    const q = search.trim()
    if (q.length < MIN_SMART_QUERY_LEN) return
    setSmartBusy(true)
    try {
      const result = await getWorkflowSearch(q)
      setSmartOrder(result.templateIds)
      if (!result.usedReranker) {
        toast.warn('Smart search fell back to keyword matching — the semantic reranker is unavailable right now.')
      }
    } catch (cause) {
      toast.error(cause instanceof ApiError ? cause.message : 'Smart search failed.')
    } finally {
      setSmartBusy(false)
    }
  }, [search, toast])

  // Typing invalidates whatever order Smart Search produced for the OLD query
  // -- fall back to plain instant substring filtering until it's re-run.
  const onSearchChange = (value: string) => {
    setSearch(value)
    setSmartOrder(null)
  }

  const visibleTemplates = useMemo(() => {
    if (smartOrder) {
      const rank = new Map(smartOrder.map((id, i) => [id, i]))
      return [...templates].sort(
        (a, b) => (rank.get(a.templateId) ?? Infinity) - (rank.get(b.templateId) ?? Infinity),
      )
    }
    const q = search.trim().toLowerCase()
    if (!q) return templates
    return templates.filter((t) => `${t.displayName} ${t.description}`.toLowerCase().includes(q))
  }, [templates, smartOrder, search])

  return (
    <PageShell className="wf-store">
      <Card
        title="Workflow Store"
        description="Every published workflow in one place — see what you already have access to, and request the rest."
        flush
        actions={
          <div className="wf-store-toolbar">
            <Field label="Search" inline>
              {(fieldProps) => (
                <Input
                  {...fieldProps}
                  placeholder="Search workflows…"
                  value={search}
                  onChange={(e) => onSearchChange(e.target.value)}
                  className="wf-store-search"
                />
              )}
            </Field>
            <Button
              variant="ghost"
              size="sm"
              loading={smartBusy}
              disabled={search.trim().length < MIN_SMART_QUERY_LEN}
              onClick={runSmartSearch}
            >
              Smart search
            </Button>
            {smartOrder && (
              <Badge tone="info" subtle>
                Smart search active
              </Badge>
            )}
          </div>
        }
      >
        {loadState === 'loading' ? (
          <div className="wf-store-skeleton">
            {Array.from({ length: 8 }, (_, i) => (
              <Skeleton key={i} height="7.5rem" radius="var(--ui-r-md)" index={i} />
            ))}
          </div>
        ) : loadState === 'error' ? (
          <EmptyState
            title="Couldn't load the Workflow Store"
            action={
              <Button size="sm" variant="ghost" onClick={load}>
                Try again
              </Button>
            }
          />
        ) : visibleTemplates.length === 0 ? (
          <EmptyState
            title="No workflows match your search"
            description="Try a different term, or clear the search box."
            compact
          />
        ) : (
          <div className="wf-store-catalog">
            {visibleTemplates.map((t) => {
              const missing = missingFor(t)
              const blocked = missing.length > 0
              const requested = requestedIds.has(t.templateId)
              return (
                <WorkflowCatalogTile
                  key={t.templateId}
                  template={t}
                  selected={false}
                  onSelect={() => {}}
                  accessBlocked={blocked}
                  accessNote={blocked ? accessNoteFor(missing) : undefined}
                  adminActions={
                    blocked ? (
                      <Button
                        size="sm"
                        variant="ghost"
                        loading={requestBusyId === t.templateId}
                        disabled={requested}
                        onClick={() => requestForTemplate(t)}
                      >
                        {requested ? 'Requested' : 'Request access'}
                      </Button>
                    ) : undefined
                  }
                />
              )
            })}
          </div>
        )}
      </Card>
    </PageShell>
  )
}

export default WorkflowStorePage
