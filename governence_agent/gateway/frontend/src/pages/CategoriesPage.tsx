import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  AddButton,
  Badge,
  Button,
  Card,
  Chip,
  DataTable,
  Drawer,
  Dropdown,
  EmptyState,
  Field,
  Input,
  Modal,
  Switch,
  useToast,
  type Column,
  type DropdownOption,
} from '../components/ui'
import { humanize } from '../components/access/toolCardShared'
import {
  ApiError,
  deleteCategory,
  getAdminCatalog,
  getCategoryCatalog,
  listCategories,
  upsertCategory,
  type AdminCatalog,
  type AdminCategory,
} from '../lib/api'
import type { PageProps } from './types'
import './CategoriesPage.css'

/**
 * Categories — ports `renderCategories()`: one row per data-domain template
 * (backend + tools + classification levels), plus create/edit.
 *
 * The legacy panel was a single raw-JSON `<textarea>` — "Edit" just
 * reserialized a row's dict back into the box for hand-editing, and the
 * only guidance on valid values was one muted sentence listing them
 * ("levels: PUBLIC, INTERNAL, PII, SENSITIVE") above the box. Someone who
 * isn't comfortable hand-writing JSON has no way to use this tab at all.
 *
 * Same underlying model and the same single upsert endpoint either way
 * (`POST /admin/categories` creates AND edits — there's no separate PATCH,
 * so "Save" in the edit drawer re-sends the whole record with its existing
 * id, exactly like re-editing the textarea did) — only the input surface
 * changes:
 *   - Backend is a `Dropdown` sourced from the real backend list, not a
 *     value you had to already know to type correctly.
 *   - Tools is a `Switch` ("every tool on this backend, including ones
 *     added later") plus a `Chip` grid for picking specific ones by name —
 *     replacing a bare `tools: "*"` or `tools: [...]` array you'd otherwise
 *     have to get exactly right, including knowing `"*"` was even an
 *     option. Tool names are humanized and, where a description exists
 *     (from any category that already references that tool, via
 *     `getCategoryCatalog`), shown on hover — the admin catalog itself
 *     only has raw canonical names like `get_customer_orders`.
 *   - Levels is a `Chip` multi-select with a plain-English line per level
 *     (what PII/SENSITIVE/etc. actually mean) instead of four bare
 *     constants an admin has to already know.
 *
 * Entry point is the same `+` → `Modal` (create) / row → `Drawer` (manage)
 * split as Consumers, reusing `AddButton`/`Modal`/`Dropdown`/`Chip` as-is.
 * Also carried over from that page: closing the manage drawer never
 * refetches the list on its own — only a save or a delete does, right at
 * the point it succeeds — so opening a row just to look never re-renders
 * the table underneath it.
 */

const LEVEL_META: Record<string, { label: string; hint: string }> = {
  PUBLIC: { label: 'Public', hint: 'Safe for anyone — status messages, labels.' },
  INTERNAL: { label: 'Internal', hint: 'Business data — order numbers, dates, statuses.' },
  PII: { label: 'Personal data', hint: 'Names, emails, phone numbers, addresses.' },
  SENSITIVE: { label: 'Financial', hint: 'Totals, amounts, spend.' },
}

interface CategoryFieldsProps {
  idEditable: boolean
  id: string
  onIdChange: (v: string) => void
  displayName: string
  onDisplayNameChange: (v: string) => void
  backend: string
  onBackendChange: (v: string) => void
  backendOptions: DropdownOption[]
  toolsMode: 'all' | 'specific'
  onToolsModeChange: (mode: 'all' | 'specific') => void
  availableTools: string[]
  selectedTools: Set<string>
  onToggleTool: (name: string) => void
  toolDescriptions: Record<string, string>
  levels: string[]
  selectedLevels: Set<string>
  onToggleLevel: (level: string) => void
  dataDomains: string
  onDataDomainsChange: (v: string) => void
}

/** The shared body for both the create Modal and the edit Drawer — same
 *  fields either way, since both go through the same upsert endpoint. */
function CategoryFields({
  idEditable,
  id,
  onIdChange,
  displayName,
  onDisplayNameChange,
  backend,
  onBackendChange,
  backendOptions,
  toolsMode,
  onToolsModeChange,
  availableTools,
  selectedTools,
  onToggleTool,
  toolDescriptions,
  levels,
  selectedLevels,
  onToggleLevel,
  dataDomains,
  onDataDomainsChange,
}: CategoryFieldsProps) {
  return (
    <div className="categories-form">
      <Field
        label="ID"
        hint={idEditable ? 'Short and stable — used everywhere this category is referenced.' : "Can't change after creation — a different id would create a separate category."}
      >
        {(fieldProps) => (
          <Input {...fieldProps} value={id} disabled={!idEditable} onChange={(e) => onIdChange(e.target.value)} />
        )}
      </Field>

      <Field label="Display name">
        {(fieldProps) => (
          <Input {...fieldProps} value={displayName} onChange={(e) => onDisplayNameChange(e.target.value)} />
        )}
      </Field>

      <Field label="Backend" hint="Which data-domain server this category's tools belong to.">
        {(fieldProps) => (
          <Dropdown {...fieldProps} value={backend} onChange={onBackendChange} options={backendOptions} />
        )}
      </Field>

      <div className="categories-section">
        <p className="ui-eyebrow categories-section-label">Tools</p>
        <Switch
          checked={toolsMode === 'all'}
          onChange={(checked) => onToolsModeChange(checked ? 'all' : 'specific')}
          label="Grant every tool on this backend"
          description="Stays current automatically as new tools are added later."
        />
        {toolsMode === 'specific' &&
          (availableTools.length === 0 ? (
            <p className="categories-note">Pick a backend above to see its tools.</p>
          ) : (
            <div className="categories-chip-grid">
              {availableTools.map((name) => (
                <Chip
                  key={name}
                  selected={selectedTools.has(name)}
                  onClick={() => onToggleTool(name)}
                  title={toolDescriptions[name] ?? humanize(name)}
                  maxWidth="14rem"
                >
                  {humanize(name)}
                </Chip>
              ))}
            </div>
          ))}
      </div>

      <div className="categories-section">
        <p className="ui-eyebrow categories-section-label">Sensitivity levels unlocked</p>
        <p className="categories-note">
          Which kinds of data holders of this category see unredacted — anything not selected is masked or
          dropped.
        </p>
        <div className="categories-chip-grid">
          {levels.map((level) => (
            <Chip
              key={level}
              selected={selectedLevels.has(level)}
              onClick={() => onToggleLevel(level)}
              title={LEVEL_META[level]?.hint}
            >
              {LEVEL_META[level]?.label ?? level}
            </Chip>
          ))}
        </div>
      </div>

      <Field label="Data domains" hint="Optional notes on what this covers — documentation only, not enforced.">
        {(fieldProps) => (
          <Input
            {...fieldProps}
            placeholder="e.g. SOOrder, SOLine"
            value={dataDomains}
            onChange={(e) => onDataDomainsChange(e.target.value)}
          />
        )}
      </Field>
    </div>
  )
}

function CategoriesPage(_props: PageProps) {
  const toast = useToast()

  const [categories, setCategories] = useState<AdminCategory[]>([])
  const [catalog, setCatalog] = useState<AdminCatalog | null>(null)
  const [toolDescriptions, setToolDescriptions] = useState<Record<string, string>>({})
  const [state, setState] = useState<'loading' | 'ready' | 'error'>('loading')
  const [error, setError] = useState<string | null>(null)

  // ── create form ──────────────────────────────────────────────────────────
  const [createOpen, setCreateOpen] = useState(false)
  const [createId, setCreateId] = useState('')
  const [createDisplayName, setCreateDisplayName] = useState('')
  const [createBackend, setCreateBackend] = useState('')
  const [createToolsMode, setCreateToolsMode] = useState<'all' | 'specific'>('all')
  const [createSelectedTools, setCreateSelectedTools] = useState<Set<string>>(new Set())
  const [createSelectedLevels, setCreateSelectedLevels] = useState<Set<string>>(new Set())
  const [createDataDomains, setCreateDataDomains] = useState('')
  const [creating, setCreating] = useState(false)

  // ── manage drawer ────────────────────────────────────────────────────────
  const [manageId, setManageId] = useState<string | null>(null)
  const [editDisplayName, setEditDisplayName] = useState('')
  const [editBackend, setEditBackend] = useState('')
  const [editToolsMode, setEditToolsMode] = useState<'all' | 'specific'>('all')
  const [editSelectedTools, setEditSelectedTools] = useState<Set<string>>(new Set())
  const [editSelectedLevels, setEditSelectedLevels] = useState<Set<string>>(new Set())
  const [editDataDomains, setEditDataDomains] = useState('')
  const [savingEdit, setSavingEdit] = useState(false)
  const [pendingDelete, setPendingDelete] = useState(false)
  const [deleting, setDeleting] = useState(false)

  const load = useCallback(() => {
    let live = true
    setState('loading')

    Promise.all([listCategories(), getAdminCatalog(), getCategoryCatalog().catch(() => ({ categories: [] }))])
      .then(([cats, cat, selfCatalog]) => {
        if (!live) return
        setCategories(cats.categories ?? [])
        setCatalog(cat)
        const descriptions: Record<string, string> = {}
        for (const entry of selfCatalog.categories ?? []) {
          for (const tool of entry.tools) descriptions[tool.name] = tool.description
        }
        setToolDescriptions(descriptions)
        setError(null)
        setState('ready')
      })
      .catch((cause: unknown) => {
        if (!live) return
        setError(cause instanceof Error ? cause.message : 'Could not load categories.')
        setState('error')
      })

    return () => {
      live = false
    }
  }, [])

  useEffect(() => load(), [load])

  const backendOptions = useMemo<DropdownOption[]>(
    () => Object.keys(catalog?.backends ?? {}).map((b) => ({ value: b, label: b })),
    [catalog],
  )

  const manageTarget = useMemo(() => categories.find((c) => c.id === manageId) ?? null, [categories, manageId])

  const toggleCreateTool = useCallback((name: string) => {
    setCreateSelectedTools((prev) => {
      const next = new Set(prev)
      if (next.has(name)) next.delete(name)
      else next.add(name)
      return next
    })
  }, [])

  const toggleEditTool = useCallback((name: string) => {
    setEditSelectedTools((prev) => {
      const next = new Set(prev)
      if (next.has(name)) next.delete(name)
      else next.add(name)
      return next
    })
  }, [])

  const toggleCreateLevel = useCallback((level: string) => {
    setCreateSelectedLevels((prev) => {
      const next = new Set(prev)
      if (next.has(level)) next.delete(level)
      else next.add(level)
      return next
    })
  }, [])

  const toggleEditLevel = useCallback((level: string) => {
    setEditSelectedLevels((prev) => {
      const next = new Set(prev)
      if (next.has(level)) next.delete(level)
      else next.add(level)
      return next
    })
  }, [])

  // Tool ids are backend-specific — switching backend invalidates whatever
  // was picked for the previous one, so the selection is cleared rather than
  // silently carrying stale, wrong-backend tool names.
  const onCreateBackendChange = useCallback((backend: string) => {
    setCreateBackend(backend)
    setCreateSelectedTools(new Set())
  }, [])

  const onEditBackendChange = useCallback((backend: string) => {
    setEditBackend(backend)
    setEditSelectedTools(new Set())
  }, [])

  const closeCreateModal = useCallback(() => {
    setCreateOpen(false)
    setCreateId('')
    setCreateDisplayName('')
    setCreateBackend('')
    setCreateToolsMode('all')
    setCreateSelectedTools(new Set())
    setCreateSelectedLevels(new Set())
    setCreateDataDomains('')
  }, [])

  const submitCreate = useCallback(async () => {
    const id = createId.trim()
    if (!id) {
      toast.warn('ID is required')
      return
    }
    if (!createBackend) {
      toast.warn('Pick a backend')
      return
    }
    setCreating(true)
    try {
      await upsertCategory({
        id,
        display_name: createDisplayName.trim() || id,
        backend: createBackend,
        tools: createToolsMode === 'all' ? '*' : [...createSelectedTools],
        levels: [...createSelectedLevels],
        data_domains: createDataDomains
          .split(',')
          .map((s) => s.trim())
          .filter(Boolean),
      })
      toast.success('Category created')
      closeCreateModal()
      load()
    } catch (cause) {
      toast.error(cause instanceof ApiError ? cause.message : 'Could not create that category.')
    } finally {
      setCreating(false)
    }
  }, [createId, createBackend, createDisplayName, createToolsMode, createSelectedTools, createSelectedLevels, createDataDomains, toast, closeCreateModal, load])

  // Resets the drawer's own UI state only — never refetches. A save or a
  // delete reloads itself at the point it succeeds; merely opening a
  // category to look at it and closing again shouldn't re-render the table.
  const closeManage = useCallback(() => {
    setManageId(null)
    setPendingDelete(false)
  }, [])

  const openManage = useCallback((category: AdminCategory) => {
    setManageId(category.id)
    setPendingDelete(false)
    setEditDisplayName(category.display_name)
    setEditBackend(category.backend)
    if (category.tools === '*') {
      setEditToolsMode('all')
      setEditSelectedTools(new Set())
    } else {
      setEditToolsMode('specific')
      setEditSelectedTools(new Set(category.tools))
    }
    setEditSelectedLevels(new Set(category.levels))
    setEditDataDomains(category.data_domains.join(', '))
  }, [])

  const saveEdit = useCallback(async () => {
    if (!manageTarget) return
    setSavingEdit(true)
    try {
      await upsertCategory({
        id: manageTarget.id,
        display_name: editDisplayName.trim() || manageTarget.id,
        backend: editBackend,
        tools: editToolsMode === 'all' ? '*' : [...editSelectedTools],
        levels: [...editSelectedLevels],
        data_domains: editDataDomains
          .split(',')
          .map((s) => s.trim())
          .filter(Boolean),
      })
      toast.success('Category updated')
      load()
    } catch (cause) {
      toast.error(cause instanceof ApiError ? cause.message : 'Could not update that category.')
    } finally {
      setSavingEdit(false)
    }
  }, [manageTarget, editDisplayName, editBackend, editToolsMode, editSelectedTools, editSelectedLevels, editDataDomains, toast, load])

  const confirmDelete = useCallback(async () => {
    if (!manageTarget) return
    setDeleting(true)
    try {
      await deleteCategory(manageTarget.id)
      toast.success('Category deleted')
      load()
      closeManage()
    } catch (cause) {
      toast.error(cause instanceof ApiError ? cause.message : 'Could not delete that category.')
    } finally {
      setDeleting(false)
    }
  }, [manageTarget, toast, load, closeManage])

  const columns = useMemo<Column<AdminCategory>[]>(
    () => [
      {
        key: 'id',
        header: 'Category',
        render: (c) => (
          <>
            <div>{c.display_name || c.id}</div>
            <div className="categories-subtext ui-mono">{c.id}</div>
          </>
        ),
      },
      {
        key: 'backend',
        header: 'Backend',
        mono: true,
      },
      {
        key: 'tools',
        header: 'Tools',
        width: '8rem',
        render: (c) => (c.tools === '*' ? <Badge tone="accent" subtle>all</Badge> : `${c.tools.length} tool${c.tools.length === 1 ? '' : 's'}`),
      },
      {
        key: 'levels',
        header: 'Levels',
        render: (c) =>
          c.levels.length === 0 ? (
            <span className="categories-none">—</span>
          ) : (
            <div className="categories-chip-grid categories-chip-grid--tight">
              {c.levels.map((l) => (
                <Badge key={l} subtle>
                  {LEVEL_META[l]?.label ?? l}
                </Badge>
              ))}
            </div>
          ),
      },
      {
        key: 'actions',
        header: '',
        srHeader: 'Manage',
        width: '7rem',
        align: 'right',
        render: (c) => (
          <Button size="sm" variant="ghost" onClick={() => openManage(c)}>
            Manage
          </Button>
        ),
      },
    ],
    [openManage],
  )

  return (
    <div className="categories">
      <Card
        title="Categories"
        description="Data-domain templates — which backend, tools, and sensitivity levels each grants."
        flush
        actions={<AddButton onClick={() => setCreateOpen(true)} label="Add a new category" />}
      >
        {state === 'error' ? (
          <EmptyState
            title="Couldn't load categories"
            description={error ?? undefined}
            action={
              <Button size="sm" variant="ghost" onClick={load}>
                Try again
              </Button>
            }
          />
        ) : (
          <DataTable
            columns={columns}
            rows={categories}
            rowKey={(c) => c.id}
            loading={state === 'loading'}
            skeletonRows={5}
            caption="Categories — data-domain templates"
            empty={<EmptyState title="No categories yet" description="Add the first one with the + above." />}
          />
        )}
      </Card>

      <Drawer
        open={manageId != null}
        onClose={closeManage}
        eyebrow="Category"
        title={manageTarget?.display_name || manageTarget?.id || ''}
        monoTitle={!manageTarget?.display_name}
        footer={
          pendingDelete ? (
            <>
              <Button variant="ghost" onClick={() => setPendingDelete(false)} disabled={deleting}>
                Cancel
              </Button>
              <Button variant="danger" onClick={confirmDelete} loading={deleting}>
                Delete
              </Button>
            </>
          ) : (
            <>
              <Button variant="danger" size="sm" onClick={() => setPendingDelete(true)}>
                Delete
              </Button>
              <Button onClick={saveEdit} loading={savingEdit}>
                Save changes
              </Button>
            </>
          )
        }
      >
        {manageTarget &&
          (pendingDelete ? (
            <p className="categories-confirm-msg">
              Delete this category permanently? Any consumer or department still listing "{manageTarget.id}" keeps
              the id but stops getting any grant from it — this doesn't clean those up.
            </p>
          ) : (
            <CategoryFields
              idEditable={false}
              id={manageTarget.id}
              onIdChange={() => {}}
              displayName={editDisplayName}
              onDisplayNameChange={setEditDisplayName}
              backend={editBackend}
              onBackendChange={onEditBackendChange}
              backendOptions={backendOptions}
              toolsMode={editToolsMode}
              onToolsModeChange={setEditToolsMode}
              availableTools={catalog?.backends[editBackend] ?? []}
              selectedTools={editSelectedTools}
              onToggleTool={toggleEditTool}
              toolDescriptions={toolDescriptions}
              levels={catalog?.levels ?? []}
              selectedLevels={editSelectedLevels}
              onToggleLevel={toggleEditLevel}
              dataDomains={editDataDomains}
              onDataDomainsChange={setEditDataDomains}
            />
          ))}
      </Drawer>

      <Modal
        open={createOpen}
        onClose={closeCreateModal}
        eyebrow="Category"
        title="New category"
        footer={
          <>
            <Button variant="ghost" onClick={closeCreateModal} disabled={creating}>
              Cancel
            </Button>
            <Button onClick={submitCreate} loading={creating}>
              Create category
            </Button>
          </>
        }
      >
        <CategoryFields
          idEditable
          id={createId}
          onIdChange={setCreateId}
          displayName={createDisplayName}
          onDisplayNameChange={setCreateDisplayName}
          backend={createBackend}
          onBackendChange={onCreateBackendChange}
          backendOptions={backendOptions}
          toolsMode={createToolsMode}
          onToolsModeChange={setCreateToolsMode}
          availableTools={catalog?.backends[createBackend] ?? []}
          selectedTools={createSelectedTools}
          onToggleTool={toggleCreateTool}
          toolDescriptions={toolDescriptions}
          levels={catalog?.levels ?? []}
          selectedLevels={createSelectedLevels}
          onToggleLevel={toggleCreateLevel}
          dataDomains={createDataDomains}
          onDataDomainsChange={setCreateDataDomains}
        />
      </Modal>
    </div>
  )
}

export default CategoriesPage
