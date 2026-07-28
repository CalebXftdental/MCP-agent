import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  AddButton,
  Badge,
  Button,
  Card,
  DataTable,
  Drawer,
  Dropdown,
  EmptyState,
  Field,
  Input,
  KeyValue,
  Modal,
  SecretKey,
  Skeleton,
  useToast,
  type BadgeTone,
  type Column,
  type DropdownOption,
} from '../components/ui'
import CategoryPicker from '../components/access/CategoryPicker'
import {
  ApiError,
  createConsumer,
  deleteConsumer,
  getCategoryCatalog,
  getConsumerProfile,
  listConsumers,
  listDepartments,
  rotateConsumerKey,
  setConsumerStatus,
  updateConsumer,
  type CategoryCatalogEntry,
  type Consumer,
  type ConsumerProfile,
  type Department,
} from '../lib/api'
import { formatRelative, formatWhen } from '../lib/format'
import type { PageProps } from './types'
import './ConsumersPage.css'

/**
 * Consumers — ports `renderConsumers()`: every principal (person or agent),
 * a create form, and per-consumer management.
 *
 * The create form's categories field was a single free-text input whose only
 * hint was its OWN placeholder text — every known category id joined with
 * commas, e.g. `orders,accounts,finance,...`. That string reads exactly like
 * example input, so typing it verbatim was one keystroke away from an easy
 * mistake, and nothing checked what you typed against real category ids
 * either way. Editing an existing consumer's categories was worse: a native
 * `prompt()` box pre-filled with the same comma string. Both are replaced
 * here by the same `CategoryPicker` — a row of toggleable chips sourced from
 * the real catalog, so what's selectable and what's selected are the same
 * thing to look at.
 *
 * The other half of a consumer's grant — its department, which contributes
 * that department's CURRENT categories live (governance_core/policy/
 * resolve.py) — had no field anywhere in the legacy create form and no way
 * to change it after signup short of a raw PATCH no button ever sent. It's
 * a first-class field here, in both the create form and the manage drawer,
 * with copy that says plainly that it's additive with the categories below
 * rather than an alternative to them.
 *
 * Every other row action (Profile, Rotate, Categories, Delete — four
 * buttons crowding one row) is consolidated into a single "Manage" drawer:
 * usage/risk stats, the access editor above, and rotate/disable/delete
 * behind their own confirm step, all in one place per consumer instead of
 * scattered across the row and two kinds of browser dialog.
 *
 * The create form itself was also the first thing on the page, permanently
 * expanded above a list most visits don't need to touch. It's now a `Modal`
 * — a centered overlay rather than Drawer's edge panel, since a create form
 * has no "row it came from" worth keeping visible behind it — opened from a
 * circular "+" beside the list's own title, and laid out as one field per
 * row instead of the legacy's cramped multi-column grid. Its Role/Type/
 * Department selects (and the manage drawer's own Department select) use
 * the new `Dropdown` in place of a native `<select>`, which every browser
 * themes its OWN way and none of them take this kit's colour tokens.
 */

function statusTone(status: string): BadgeTone {
  return status === 'active' ? 'ok' : 'danger'
}

const ROLE_OPTIONS: DropdownOption[] = [
  { value: 'user', label: 'user' },
  { value: 'admin', label: 'admin' },
]

const TYPE_OPTIONS: DropdownOption[] = [
  { value: 'agent', label: 'agent' },
  { value: 'user', label: 'user' },
]

type PendingAction = 'rotate' | 'disable' | 'reactivate' | 'delete'

function ConsumersPage(_props: PageProps) {
  const toast = useToast()

  const [consumers, setConsumers] = useState<Consumer[]>([])
  const [categories, setCategories] = useState<CategoryCatalogEntry[]>([])
  const [departments, setDepartments] = useState<Department[]>([])
  const [state, setState] = useState<'loading' | 'ready' | 'error'>('loading')
  const [error, setError] = useState<string | null>(null)

  // ── create form ──────────────────────────────────────────────────────────
  const [createOpen, setCreateOpen] = useState(false)
  const [newName, setNewName] = useState('')
  const [newRole, setNewRole] = useState('user')
  const [newType, setNewType] = useState('agent')
  const [newRate, setNewRate] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [newDepartment, setNewDepartment] = useState('')
  const [newCategories, setNewCategories] = useState<Set<string>>(new Set())
  const [creating, setCreating] = useState(false)
  const [mintedKey, setMintedKey] = useState<{ name: string; key: string } | null>(null)

  // ── manage drawer ────────────────────────────────────────────────────────
  const [manageId, setManageId] = useState<string | null>(null)
  const [profile, setProfile] = useState<ConsumerProfile | null>(null)
  const [profileState, setProfileState] = useState<'loading' | 'ready' | 'error'>('loading')
  const [profileError, setProfileError] = useState<string | null>(null)

  const [editDepartment, setEditDepartment] = useState('')
  const [editCategories, setEditCategories] = useState<Set<string>>(new Set())
  const [editRate, setEditRate] = useState('')
  const [savingAccess, setSavingAccess] = useState(false)

  const [pendingAction, setPendingAction] = useState<PendingAction | null>(null)
  const [actionSubmitting, setActionSubmitting] = useState(false)
  const [rotatedKey, setRotatedKey] = useState<string | null>(null)

  const load = useCallback(() => {
    let live = true
    setState('loading')

    Promise.all([
      listConsumers(),
      getCategoryCatalog().catch(() => ({ categories: [] })),
      listDepartments().catch(() => ({ departments: [] })),
    ])
      .then(([cs, cats, deps]) => {
        if (!live) return
        setConsumers(cs.consumers ?? [])
        setCategories(cats.categories ?? [])
        setDepartments(deps.departments ?? [])
        setError(null)
        setState('ready')
      })
      .catch((cause: unknown) => {
        if (!live) return
        setError(cause instanceof Error ? cause.message : 'Could not load consumers.')
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

  const departmentLabel = useMemo(() => {
    const labels: Record<string, string> = {}
    for (const d of departments) labels[d.id] = d.display_name
    return labels
  }, [departments])

  const manageTarget = useMemo(() => consumers.find((c) => c.consumer_id === manageId) ?? null, [consumers, manageId])

  const departmentOptions = useMemo<DropdownOption[]>(
    () => [{ value: '', label: 'No department' }, ...departments.map((d) => ({ value: d.id, label: d.display_name }))],
    [departments],
  )

  const toggleNewCategory = useCallback((id: string) => {
    setNewCategories((prev) => {
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
    setMintedKey(null)
    setNewName('')
    setNewRole('user')
    setNewType('agent')
    setNewRate('')
    setNewPassword('')
    setNewDepartment('')
    setNewCategories(new Set())
  }, [])

  const submitCreate = useCallback(async () => {
    const name = newName.trim()
    if (!name) {
      toast.warn('Name is required')
      return
    }
    setCreating(true)
    try {
      const result = await createConsumer({
        name,
        role: newRole,
        type: newType,
        categories: [...newCategories],
        department: newDepartment || undefined,
        rate_limit_per_hour: newRate.trim() ? Number(newRate) : null,
        password: newPassword || null,
      })
      setMintedKey({ name: result.consumer.name, key: result.api_key })
      setNewName('')
      setNewRate('')
      setNewPassword('')
      setNewDepartment('')
      setNewCategories(new Set())
      toast.success('Consumer created')
      load()
    } catch (cause) {
      toast.error(cause instanceof ApiError ? cause.message : 'Could not create that consumer.')
    } finally {
      setCreating(false)
    }
  }, [newName, newRole, newType, newCategories, newDepartment, newRate, newPassword, toast, load])

  const openManage = useCallback((consumer: Consumer) => {
    setManageId(consumer.consumer_id)
    setEditDepartment(consumer.department || '')
    setEditCategories(new Set(consumer.categories))
    setEditRate(consumer.rate_limit_per_hour != null ? String(consumer.rate_limit_per_hour) : '')
    setPendingAction(null)
    setRotatedKey(null)
    setProfile(null)
    setProfileState('loading')

    getConsumerProfile(consumer.consumer_id)
      .then((result) => {
        setProfile(result)
        setProfileError(null)
        setProfileState('ready')
      })
      .catch((cause: unknown) => {
        setProfileError(cause instanceof Error ? cause.message : 'Could not load this consumer’s profile.')
        setProfileState('error')
      })
  }, [])

  // Resets the drawer's own UI state only — it does NOT refetch the list.
  // Nothing here changes what the table shows (viewing the profile, backing
  // out of a pending confirm, or dismissing a rotated key are all no-ops for
  // the row itself), so closing shouldn't force every row to re-render. Each
  // action that DOES change something the table displays (disable/
  // reactivate, delete, saving access) reloads itself at the point it
  // succeeds, rather than leaving it to whoever closes the drawer next.
  const closeManage = useCallback(() => {
    setManageId(null)
    setPendingAction(null)
    setRotatedKey(null)
  }, [])

  const saveAccess = useCallback(async () => {
    if (!manageTarget) return
    setSavingAccess(true)
    try {
      await updateConsumer(manageTarget.consumer_id, {
        department: editDepartment,
        categories: [...editCategories],
        rate_limit_per_hour: editRate.trim() ? Number(editRate) : null,
      })
      toast.success('Access updated')
      load()
    } catch (cause) {
      toast.error(cause instanceof ApiError ? cause.message : 'Could not update access.')
    } finally {
      setSavingAccess(false)
    }
  }, [manageTarget, editDepartment, editCategories, editRate, toast, load])

  const confirmPendingAction = useCallback(async () => {
    if (!manageTarget || !pendingAction) return
    setActionSubmitting(true)
    try {
      if (pendingAction === 'rotate') {
        const result = await rotateConsumerKey(manageTarget.consumer_id)
        setRotatedKey(result.api_key)
        toast.success('Key rotated')
      } else if (pendingAction === 'delete') {
        await deleteConsumer(manageTarget.consumer_id)
        toast.success('Consumer deleted')
        // The row is gone — this is the one close path that DOES need a
        // reload, so it's done explicitly here rather than inside closeManage.
        load()
        closeManage()
      } else {
        await setConsumerStatus(manageTarget.consumer_id, pendingAction === 'disable' ? 'disabled' : 'active')
        toast.success(pendingAction === 'disable' ? 'Consumer disabled' : 'Consumer reactivated')
        setPendingAction(null)
        load()
      }
    } catch (cause) {
      toast.error(cause instanceof ApiError ? cause.message : 'Could not complete that action.')
    } finally {
      setActionSubmitting(false)
    }
  }, [manageTarget, pendingAction, toast, load, closeManage])

  const columns = useMemo<Column<Consumer>[]>(
    () => [
      {
        key: 'name',
        header: 'Name',
        render: (c) => (
          <>
            <div>{c.full_name || c.name}</div>
            <div className="consumers-subtext">
              {c.name}
              {c.department && ` · ${departmentLabel[c.department] ?? c.department}`}
            </div>
          </>
        ),
      },
      {
        key: 'categories',
        header: 'Categories',
        render: (c) => (
          <>
            {c.categories.length === 0 && !c.department ? (
              <span className="consumers-none">—</span>
            ) : (
              <div className="consumers-cat-pills">
                {c.categories.map((id) => (
                  <Badge key={id} subtle>
                    {categoryLabel[id] ?? id}
                  </Badge>
                ))}
              </div>
            )}
            {c.department && (
              <div className="consumers-subtext">+ via {departmentLabel[c.department] ?? c.department}</div>
            )}
          </>
        ),
      },
      {
        key: 'role',
        header: 'Role',
        width: '6rem',
        render: (c) => <Badge subtle>{c.role}</Badge>,
      },
      {
        key: 'type',
        header: 'Type',
        width: '6rem',
        render: (c) => <Badge subtle>{c.type}</Badge>,
      },
      {
        key: 'rate',
        header: 'Rate / hr',
        width: '6rem',
        muted: true,
        render: (c) => c.rate_limit_per_hour ?? 'default',
      },
      {
        key: 'status',
        header: 'Status',
        width: '7rem',
        render: (c) => (
          <Badge tone={statusTone(c.status)} dot>
            {c.status}
          </Badge>
        ),
      },
      {
        key: 'credential',
        header: 'Credential',
        width: '7rem',
        muted: true,
        render: (c) => (c.has_key ? 'API key' : c.has_login ? 'Login' : '—'),
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
    [categoryLabel, departmentLabel, openManage],
  )

  return (
    <div className="consumers">
      <Card
        title="Consumers"
        description="Every principal — person or agent — that can call the gateway."
        flush
        actions={<AddButton onClick={() => setCreateOpen(true)} label="Add a new consumer" />}
      >
        {state === 'error' ? (
          <EmptyState
            title="Couldn't load consumers"
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
            rows={consumers}
            rowKey={(c) => c.consumer_id}
            loading={state === 'loading'}
            skeletonRows={5}
            caption="Consumers — principals that can call the gateway"
            empty={<EmptyState title="No consumers yet" description="Add the first one with the + above." />}
          />
        )}
      </Card>

      <Drawer
        open={manageId != null}
        onClose={closeManage}
        eyebrow="Consumer"
        title={manageTarget?.full_name || manageTarget?.name || ''}
        monoTitle={!manageTarget?.full_name}
        meta={
          manageTarget && (
            <>
              {profile && profile.reasons.length > 0 ? (
                <Badge tone="danger" dot>
                  {profile.reasons.length} active flag{profile.reasons.length === 1 ? '' : 's'}
                </Badge>
              ) : profileState === 'ready' ? (
                <Badge tone="ok" dot>
                  no active flags
                </Badge>
              ) : null}
              <Badge subtle>{manageTarget.role}</Badge>
              <Badge subtle>{manageTarget.type}</Badge>
              <Badge tone={statusTone(manageTarget.status)} dot>
                {manageTarget.status}
              </Badge>
            </>
          )
        }
        footer={
          pendingAction ? (
            <>
              <Button variant="ghost" onClick={() => setPendingAction(null)} disabled={actionSubmitting}>
                Cancel
              </Button>
              <Button
                variant={pendingAction === 'reactivate' ? 'primary' : 'danger'}
                onClick={confirmPendingAction}
                loading={actionSubmitting}
              >
                {pendingAction === 'rotate'
                  ? 'Rotate key'
                  : pendingAction === 'disable'
                    ? 'Disable'
                    : pendingAction === 'reactivate'
                      ? 'Reactivate'
                      : 'Delete'}
              </Button>
            </>
          ) : rotatedKey ? (
            <Button onClick={closeManage}>Done</Button>
          ) : (
            manageTarget && (
              <>
                <Button variant="ghost" size="sm" onClick={() => setPendingAction('rotate')}>
                  Rotate key
                </Button>
                {manageTarget.status === 'active' ? (
                  <Button variant="danger" size="sm" onClick={() => setPendingAction('disable')}>
                    Disable
                  </Button>
                ) : (
                  <Button size="sm" onClick={() => setPendingAction('reactivate')}>
                    Reactivate
                  </Button>
                )}
                <Button variant="danger" size="sm" onClick={() => setPendingAction('delete')}>
                  Delete
                </Button>
              </>
            )
          )
        }
      >
        {manageTarget &&
          (rotatedKey ? (
            <SecretKey
              value={rotatedKey}
              label="New API key"
              hint="Copy it now — store it safely. The old key stopped working immediately."
            />
          ) : pendingAction ? (
            <p className="consumers-confirm-msg">
              {pendingAction === 'rotate' &&
                "Rotate this consumer's key? Its current key stops working at once."}
              {pendingAction === 'disable' && 'Disable this consumer? It will be blocked at the auth edge immediately.'}
              {pendingAction === 'reactivate' && 'Reactivate this consumer? It will be able to call the gateway again.'}
              {pendingAction === 'delete' && 'Delete this consumer permanently? This cannot be undone.'}
            </p>
          ) : (
            <div className="consumers-drawer-body">
              <div className="consumers-section">
                <p className="ui-eyebrow consumers-section-label">Access</p>
                <Field label="Department" hint="Adds that department's current categories on top of the ones below.">
                  {(fieldProps) => (
                    <Dropdown {...fieldProps} value={editDepartment} onChange={setEditDepartment} options={departmentOptions} />
                  )}
                </Field>
                <Field label="Categories" hint="Granted directly, independent of the department above.">
                  {() => (
                    <CategoryPicker categories={categories} selected={editCategories} onToggle={toggleEditCategory} />
                  )}
                </Field>
                <Field label="Rate limit / hr" hint="Blank = default limit.">
                  {(fieldProps) => (
                    <Input
                      {...fieldProps}
                      type="number"
                      min={0}
                      placeholder="default"
                      value={editRate}
                      onChange={(e) => setEditRate(e.target.value)}
                    />
                  )}
                </Field>
                <Button size="sm" onClick={saveAccess} loading={savingAccess}>
                  Save access
                </Button>
              </div>

              <div className="consumers-section">
                <p className="ui-eyebrow consumers-section-label">Usage &amp; risk</p>
                {profileState === 'loading' ? (
                  <Skeleton height="8rem" />
                ) : profileState === 'error' ? (
                  <p className="consumers-note">{profileError}</p>
                ) : (
                  profile && (
                    <>
                      {profile.reasons.length > 0 && (
                        <p className="consumers-flags">{profile.reasons.join(' · ')}</p>
                      )}
                      <KeyValue
                        columns={2}
                        items={[
                          { label: 'Calls (total)', value: profile.totals.total },
                          { label: 'Last 24h / 7d', value: `${profile.totals.last_24h} / ${profile.totals.last_7d}` },
                          { label: 'Denied', value: profile.totals.denied },
                          { label: 'Errors', value: profile.totals.error },
                          { label: 'Distinct customers', value: profile.distinct_customers },
                          { label: 'Rows returned', value: profile.rows_returned },
                          { label: 'Sensitive calls', value: profile.sensitive_calls },
                          { label: 'Redactions', value: profile.redactions },
                          {
                            label: 'Last used',
                            value: profile.last_used ? (
                              <span title={formatWhen(profile.last_used)}>{formatRelative(profile.last_used)}</span>
                            ) : (
                              'never'
                            ),
                          },
                          {
                            label: 'Key age',
                            value: profile.last_key_rotation
                              ? `${Math.round((Date.now() / 1000 - profile.last_key_rotation) / 86400)}d`
                              : 'unknown',
                          },
                        ]}
                      />

                      <p className="ui-eyebrow consumers-section-label">
                        Least privilege — granted but never used ({profile.unused_grants.length})
                      </p>
                      {profile.unused_grants.length === 0 ? (
                        <p className="consumers-note">Every granted tool has been used.</p>
                      ) : (
                        <div className="consumers-cat-pills">
                          {profile.unused_grants.map((tool) => (
                            <Badge key={tool} subtle>
                              <span className="ui-mono">{tool}</span>
                            </Badge>
                          ))}
                        </div>
                      )}

                      {profile.ips.length > 0 && (
                        <>
                          <p className="ui-eyebrow consumers-section-label">Source IPs</p>
                          <div className="consumers-cat-pills">
                            {profile.ips.map((ip) => (
                              <Badge key={ip} subtle>
                                <span className="ui-mono">{ip}</span>
                              </Badge>
                            ))}
                          </div>
                        </>
                      )}
                    </>
                  )
                )}
              </div>
            </div>
          ))}
      </Drawer>

      <Modal
        open={createOpen}
        onClose={closeCreateModal}
        eyebrow="Consumer"
        title={mintedKey ? 'Consumer created' : 'New consumer'}
        footer={
          mintedKey ? (
            <Button onClick={closeCreateModal}>Done</Button>
          ) : (
            <>
              <Button variant="ghost" onClick={closeCreateModal} disabled={creating}>
                Cancel
              </Button>
              <Button onClick={submitCreate} loading={creating}>
                Create &amp; issue key
              </Button>
            </>
          )
        }
      >
        {mintedKey ? (
          <SecretKey
            value={mintedKey.key}
            label={`API key for ${mintedKey.name}`}
            hint="Copy it now — store it safely. Shown once."
          />
        ) : (
          <div className="consumers-form">
            <Field label="Name" required>
              {(fieldProps) => <Input {...fieldProps} value={newName} onChange={(e) => setNewName(e.target.value)} />}
            </Field>

            <Field label="Role">
              {(fieldProps) => <Dropdown {...fieldProps} value={newRole} onChange={setNewRole} options={ROLE_OPTIONS} />}
            </Field>

            <Field label="Type">
              {(fieldProps) => <Dropdown {...fieldProps} value={newType} onChange={setNewType} options={TYPE_OPTIONS} />}
            </Field>

            <Field label="Department" hint="Adds that department's current categories on top of the ones below.">
              {(fieldProps) => (
                <Dropdown {...fieldProps} value={newDepartment} onChange={setNewDepartment} options={departmentOptions} />
              )}
            </Field>

            <Field label="Categories" hint="Granted directly to this consumer, independent of any department above.">
              {() => <CategoryPicker categories={categories} selected={newCategories} onToggle={toggleNewCategory} />}
            </Field>

            <Field label="Rate limit / hr" hint="Blank = default limit.">
              {(fieldProps) => (
                <Input
                  {...fieldProps}
                  type="number"
                  min={0}
                  placeholder="default"
                  value={newRate}
                  onChange={(e) => setNewRate(e.target.value)}
                />
              )}
            </Field>

            <Field label="Login password" hint="Only if this consumer signs into the console itself.">
              {(fieldProps) => (
                <Input
                  {...fieldProps}
                  type="password"
                  placeholder="optional"
                  value={newPassword}
                  onChange={(e) => setNewPassword(e.target.value)}
                />
              )}
            </Field>
          </div>
        )}
      </Modal>
    </div>
  )
}

export default ConsumersPage
