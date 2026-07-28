import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  AddButton,
  Badge,
  Button,
  Card,
  DataTable,
  Drawer,
  EmptyState,
  Field,
  Input,
  Modal,
  useToast,
  type Column,
} from '../components/ui'
import CategoryPicker from '../components/access/CategoryPicker'
import {
  ApiError,
  deleteDepartment,
  getCategoryCatalog,
  listDepartments,
  upsertDepartment,
  type CategoryCatalogEntry,
  type Department,
} from '../lib/api'
import type { PageProps } from './types'
import './DepartmentsPage.css'

/**
 * Departments — ports `renderDepartments()`: a department is just a named
 * set of categories, so this is the same raw-JSON-textarea-to-real-form
 * redesign as Categories, over a much smaller record (id, display name,
 * categories — no backend/tools/levels of its own).
 *
 * Same `CategoryPicker` Consumers and Categories already use for "pick some
 * categories" — a department picking its categories is the identical
 * control, not a different one that happens to look similar. Same
 * `AddButton` → `Modal` (create) / row → `Drawer` (manage) split, and the
 * same discipline about the manage drawer never reloading the list on its
 * own — only a save or delete does, right where it succeeds.
 *
 * One thing worth carrying over precisely: `POST /admin/departments`
 * actually validates its `categories` list server-side (unlike categories'
 * own tools/levels, which are accepted unvalidated) — an unknown category
 * id is rejected with a 400 naming it. The picker only ever offers real
 * category ids, so that path shouldn't normally trigger, but the error
 * message is still surfaced verbatim via toast rather than assumed away.
 */

function DepartmentsPage(_props: PageProps) {
  const toast = useToast()

  const [departments, setDepartments] = useState<Department[]>([])
  const [categories, setCategories] = useState<CategoryCatalogEntry[]>([])
  const [state, setState] = useState<'loading' | 'ready' | 'error'>('loading')
  const [error, setError] = useState<string | null>(null)

  // ── create form ──────────────────────────────────────────────────────────
  const [createOpen, setCreateOpen] = useState(false)
  const [createId, setCreateId] = useState('')
  const [createDisplayName, setCreateDisplayName] = useState('')
  const [createCategories, setCreateCategories] = useState<Set<string>>(new Set())
  const [creating, setCreating] = useState(false)

  // ── manage drawer ────────────────────────────────────────────────────────
  const [manageId, setManageId] = useState<string | null>(null)
  const [editDisplayName, setEditDisplayName] = useState('')
  const [editCategories, setEditCategories] = useState<Set<string>>(new Set())
  const [savingEdit, setSavingEdit] = useState(false)
  const [pendingDelete, setPendingDelete] = useState(false)
  const [deleting, setDeleting] = useState(false)

  const load = useCallback(() => {
    let live = true
    setState('loading')

    Promise.all([listDepartments(), getCategoryCatalog().catch(() => ({ categories: [] }))])
      .then(([deps, cats]) => {
        if (!live) return
        setDepartments(deps.departments ?? [])
        setCategories(cats.categories ?? [])
        setError(null)
        setState('ready')
      })
      .catch((cause: unknown) => {
        if (!live) return
        setError(cause instanceof Error ? cause.message : 'Could not load departments.')
        setState('error')
      })

    return () => {
      live = false
    }
  }, [])

  useEffect(() => load(), [load])

  const categoryLabel = useMemo(() => {
    const labels: Record<string, string> = {}
    for (const c of categories) labels[c.id] = c.display_name
    return labels
  }, [categories])

  const manageTarget = useMemo(() => departments.find((d) => d.id === manageId) ?? null, [departments, manageId])

  const toggleCreateCategory = useCallback((id: string) => {
    setCreateCategories((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }, [])

  const toggleEditCategory = useCallback((id: string) => {
    setEditCategories((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }, [])

  const closeCreateModal = useCallback(() => {
    setCreateOpen(false)
    setCreateId('')
    setCreateDisplayName('')
    setCreateCategories(new Set())
  }, [])

  const submitCreate = useCallback(async () => {
    const id = createId.trim()
    if (!id) {
      toast.warn('ID is required')
      return
    }
    setCreating(true)
    try {
      await upsertDepartment({
        id,
        display_name: createDisplayName.trim() || id,
        categories: [...createCategories],
      })
      toast.success('Department created')
      closeCreateModal()
      load()
    } catch (cause) {
      toast.error(cause instanceof ApiError ? cause.message : 'Could not create that department.')
    } finally {
      setCreating(false)
    }
  }, [createId, createDisplayName, createCategories, toast, closeCreateModal, load])

  // Resets the drawer's own UI state only — never refetches. A save or a
  // delete reloads itself at the point it succeeds, so just opening a
  // department to look and closing it again doesn't re-render the table.
  const closeManage = useCallback(() => {
    setManageId(null)
    setPendingDelete(false)
  }, [])

  const openManage = useCallback((department: Department) => {
    setManageId(department.id)
    setPendingDelete(false)
    setEditDisplayName(department.display_name)
    setEditCategories(new Set(department.categories))
  }, [])

  const saveEdit = useCallback(async () => {
    if (!manageTarget) return
    setSavingEdit(true)
    try {
      await upsertDepartment({
        id: manageTarget.id,
        display_name: editDisplayName.trim() || manageTarget.id,
        categories: [...editCategories],
      })
      toast.success('Department updated')
      load()
    } catch (cause) {
      toast.error(cause instanceof ApiError ? cause.message : 'Could not update that department.')
    } finally {
      setSavingEdit(false)
    }
  }, [manageTarget, editDisplayName, editCategories, toast, load])

  const confirmDelete = useCallback(async () => {
    if (!manageTarget) return
    setDeleting(true)
    try {
      await deleteDepartment(manageTarget.id)
      toast.success('Department deleted')
      load()
      closeManage()
    } catch (cause) {
      toast.error(cause instanceof ApiError ? cause.message : 'Could not delete that department.')
    } finally {
      setDeleting(false)
    }
  }, [manageTarget, toast, load, closeManage])

  const columns = useMemo<Column<Department>[]>(
    () => [
      {
        key: 'id',
        header: 'Department',
        render: (d) => (
          <>
            <div>{d.display_name || d.id}</div>
            <div className="departments-subtext ui-mono">{d.id}</div>
          </>
        ),
      },
      {
        key: 'categories',
        header: 'Categories',
        render: (d) =>
          d.categories.length === 0 ? (
            <span className="departments-none">—</span>
          ) : (
            <div className="departments-pill-grid">
              {d.categories.map((id) => (
                <Badge key={id} subtle>
                  {categoryLabel[id] ?? id}
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
        render: (d) => (
          <Button size="sm" variant="ghost" onClick={() => openManage(d)}>
            Manage
          </Button>
        ),
      },
    ],
    [categoryLabel, openManage],
  )

  return (
    <div className="departments">
      <Card
        title="Departments"
        description="Org-unit groupings of categories, used by self-signup. Editing a department's categories updates every member's access immediately."
        flush
        actions={<AddButton onClick={() => setCreateOpen(true)} label="Add a new department" />}
      >
        {state === 'error' ? (
          <EmptyState
            title="Couldn't load departments"
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
            rows={departments}
            rowKey={(d) => d.id}
            loading={state === 'loading'}
            skeletonRows={4}
            caption="Departments — org-unit groupings of categories"
            empty={<EmptyState title="No departments yet" description="Add the first one with the + above." />}
          />
        )}
      </Card>

      <Drawer
        open={manageId != null}
        onClose={closeManage}
        eyebrow="Department"
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
            <p className="departments-confirm-msg">
              Delete this department permanently? Members keep their status but will resolve as if they have no
              department until reassigned.
            </p>
          ) : (
            <div className="departments-form">
              <Field label="Display name">
                {(fieldProps) => (
                  <Input {...fieldProps} value={editDisplayName} onChange={(e) => setEditDisplayName(e.target.value)} />
                )}
              </Field>
              <Field label="Categories" hint="What members of this department can do — updates live for every member.">
                {() => (
                  <CategoryPicker categories={categories} selected={editCategories} onToggle={toggleEditCategory} />
                )}
              </Field>
            </div>
          ))}
      </Drawer>

      <Modal
        open={createOpen}
        onClose={closeCreateModal}
        eyebrow="Department"
        title="New department"
        footer={
          <>
            <Button variant="ghost" onClick={closeCreateModal} disabled={creating}>
              Cancel
            </Button>
            <Button onClick={submitCreate} loading={creating}>
              Create department
            </Button>
          </>
        }
      >
        <div className="departments-form">
          <Field label="ID" hint="Short and stable — used everywhere this department is referenced.">
            {(fieldProps) => <Input {...fieldProps} value={createId} onChange={(e) => setCreateId(e.target.value)} />}
          </Field>
          <Field label="Display name">
            {(fieldProps) => (
              <Input {...fieldProps} value={createDisplayName} onChange={(e) => setCreateDisplayName(e.target.value)} />
            )}
          </Field>
          <Field label="Categories" hint="What members of this department can do — updates live for every member.">
            {() => (
              <CategoryPicker categories={categories} selected={createCategories} onToggle={toggleCreateCategory} />
            )}
          </Field>
        </div>
      </Modal>
    </div>
  )
}

export default DepartmentsPage
