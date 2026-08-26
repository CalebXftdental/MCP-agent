import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  AddButton,
  Badge,
  Button,
  Card,
  CodeBlock,
  DataTable,
  Drawer,
  Dropdown,
  EmptyState,
  Field,
  Input,
  KeyValue,
  Modal,
  PageShell,
  Skeleton,
  Textarea,
  useToast,
  type BadgeTone,
  type Column,
  type DropdownOption,
} from '../components/ui'
import {
  ApiError,
  addTemplateVersion,
  createTemplate,
  disableTemplate,
  getTemplate,
  listTemplates,
  type Template,
  type TemplateVersionRecord,
} from '../lib/api'
import { formatRelative, formatWhen } from '../lib/format'
import type { PageProps } from './types'
import './TemplatesPage.css'

/**
 * Templates — ports `renderTemplates()`: a versioned library of reusable
 * report/deck/email/calendar/prompt/workflow templates.
 *
 * The legacy panel edited every type through one "Content JSON" `<textarea>`
 * — its only concession to type was swapping which starter JSON blob it
 * pre-filled on `<select>` change, and "Add version" was a native `prompt()`
 * around the same raw JSON. Someone who isn't comfortable hand-writing JSON
 * (and knowing, for a workflow template, that it specifically needs
 * `goal`/`inputs`/`steps`/`outputs` keys) has no way to use this tab.
 *
 * Each template type now gets fields shaped for what it actually is —
 * Subject/Body for email, Title/Location/Duration/Description for calendar,
 * a plain Prompt-text box, Goal/Inputs/Steps/Outputs (one item per line) for
 * workflow, and a list of Heading+Bullets sections for the three document
 * types — via `TemplateContentFields`, shared between the create Modal and
 * the "Add version" step of the manage Drawer so both go through the exact
 * same editor. `draftFromContent`/`contentFromDraft` round-trip that shape
 * to/from the plain JSON object the API actually stores, preserving any
 * keys the structured editor doesn't model (e.g. a document template using
 * `tables`/`layout` instead of `sections`) rather than silently dropping
 * them on the next save.
 *
 * Same `+` → `Modal` (create) / row → `Drawer` (manage) split as Consumers/
 * Categories/Departments, and the same discipline about the manage drawer
 * never reloading the list on its own — only creating, versioning, or
 * disabling does, right at the point it succeeds.
 */

const TEMPLATE_TYPE_OPTIONS: DropdownOption[] = [
  { value: 'excel', label: 'Excel workbook' },
  { value: 'powerpoint', label: 'PowerPoint deck' },
  { value: 'word', label: 'Word document' },
  { value: 'email', label: 'Email' },
  { value: 'calendar', label: 'Calendar invite' },
  { value: 'workflow', label: 'Workflow' },
  { value: 'prompt', label: 'Prompt' },
  { value: 'generic', label: 'Generic document' },
]

const TYPE_LABEL: Record<string, string> = Object.fromEntries(TEMPLATE_TYPE_OPTIONS.map((o) => [o.value, o.label]))

function isSectionsType(type: string): boolean {
  return type === 'excel' || type === 'powerpoint' || type === 'word' || type === 'generic'
}

function statusTone(status: string): BadgeTone {
  if (status === 'active') return 'ok'
  if (status === 'disabled') return 'danger'
  return 'warn'
}

interface SectionDraft {
  heading: string
  /** One bullet per line while editing — split into an array on submit. */
  bullets: string
}

interface ContentDraft {
  goal: string
  inputs: string
  steps: string
  outputs: string
  sections: SectionDraft[]
  subject: string
  body: string
  calTitle: string
  calLocation: string
  calDuration: string
  calDescription: string
  promptText: string
  /** Any content keys the structured editor for this type doesn't model
   *  (e.g. `tables`/`layout` on a document template) — carried through
   *  untouched rather than dropped the next time this template is saved. */
  extra: Record<string, unknown>
}

function emptyDraft(): ContentDraft {
  return {
    goal: '',
    inputs: '',
    steps: '',
    outputs: '',
    sections: [],
    subject: '',
    body: '',
    calTitle: '',
    calLocation: '',
    calDuration: '',
    calDescription: '',
    promptText: '',
    extra: {},
  }
}

function defaultDraftForType(type: string): ContentDraft {
  const d = emptyDraft()
  if (isSectionsType(type)) {
    d.sections = [{ heading: 'Executive Summary', bullets: 'Current position\nRisks\nNext actions' }]
  } else if (type === 'workflow') {
    d.goal = 'Generate a monthly customer report'
    d.inputs = 'customer_id\ndate_range'
    d.steps = 'get_customer_overview\ncreate_excel_report'
    d.outputs = 'xlsx'
  } else if (type === 'email') {
    d.subject = 'Follow-up'
    d.body = 'Hello,\n\nPlease review the attached packet.'
  } else if (type === 'calendar') {
    d.calDuration = '30'
  }
  return d
}

function linesOf(value: unknown): string {
  return Array.isArray(value) ? value.filter((v) => typeof v === 'string').join('\n') : ''
}

function draftFromContent(type: string, content: Record<string, unknown>): ContentDraft {
  const d = emptyDraft()
  const rest = { ...content }

  if (isSectionsType(type)) {
    const sections = Array.isArray(content.sections) ? content.sections : []
    d.sections = sections.map((s) => {
      const section = (s ?? {}) as Record<string, unknown>
      return { heading: String(section.heading ?? ''), bullets: linesOf(section.bullets) }
    })
    delete rest.sections
  } else if (type === 'workflow') {
    d.goal = String(content.goal ?? '')
    d.inputs = linesOf(content.inputs)
    const steps = Array.isArray(content.steps) ? content.steps : []
    d.steps = steps
      .map((s) => (typeof s === 'string' ? s : String((s as Record<string, unknown> | null)?.tool ?? '')))
      .filter(Boolean)
      .join('\n')
    d.outputs = linesOf(content.outputs)
    delete rest.goal
    delete rest.inputs
    delete rest.steps
    delete rest.outputs
  } else if (type === 'email') {
    d.subject = String(content.subject ?? '')
    d.body = String(content.bodyMarkdown ?? '')
    delete rest.subject
    delete rest.bodyMarkdown
  } else if (type === 'calendar') {
    d.calTitle = String(content.title ?? '')
    d.calLocation = String(content.location ?? '')
    d.calDuration = content.durationMinutes != null ? String(content.durationMinutes) : ''
    d.calDescription = String(content.description ?? '')
    delete rest.title
    delete rest.location
    delete rest.durationMinutes
    delete rest.description
  } else if (type === 'prompt') {
    d.promptText = String(content.text ?? '')
    delete rest.text
  }

  d.extra = rest
  return d
}

function splitLines(value: string): string[] {
  return value
    .split('\n')
    .map((s) => s.trim())
    .filter(Boolean)
}

function contentFromDraft(type: string, draft: ContentDraft): Record<string, unknown> {
  if (isSectionsType(type)) {
    return {
      ...draft.extra,
      sections: draft.sections.map((s) => ({ heading: s.heading.trim(), bullets: splitLines(s.bullets) })),
    }
  }
  if (type === 'workflow') {
    return {
      ...draft.extra,
      goal: draft.goal.trim(),
      inputs: splitLines(draft.inputs),
      steps: splitLines(draft.steps).map((tool) => ({ tool })),
      outputs: splitLines(draft.outputs),
    }
  }
  if (type === 'email') {
    return { ...draft.extra, subject: draft.subject.trim(), bodyMarkdown: draft.body }
  }
  if (type === 'calendar') {
    return {
      ...draft.extra,
      title: draft.calTitle.trim(),
      location: draft.calLocation.trim(),
      durationMinutes: draft.calDuration.trim() ? Number(draft.calDuration) : undefined,
      description: draft.calDescription,
    }
  }
  if (type === 'prompt') {
    return { ...draft.extra, text: draft.promptText }
  }
  return { ...draft.extra }
}

/** Mirrors `template_store._validate` so a missing required piece is caught
 *  here, in plain language, instead of surfacing as a raw 400 from the API. */
function validateDraft(type: string, draft: ContentDraft): string | null {
  if (type === 'workflow') {
    if (!draft.goal.trim()) return 'Goal is required for a workflow template.'
    if (!draft.inputs.trim()) return 'Add at least one input.'
    if (!draft.steps.trim()) return 'Add at least one step.'
    if (!draft.outputs.trim()) return 'Add at least one output.'
  }
  if (isSectionsType(type) && draft.sections.length === 0) {
    return 'Add at least one section.'
  }
  return null
}

interface SectionsEditorProps {
  sections: SectionDraft[]
  onChange: (sections: SectionDraft[]) => void
}

function SectionsEditor({ sections, onChange }: SectionsEditorProps) {
  const update = (i: number, patch: Partial<SectionDraft>) =>
    onChange(sections.map((s, idx) => (idx === i ? { ...s, ...patch } : s)))
  const remove = (i: number) => onChange(sections.filter((_, idx) => idx !== i))
  const add = () => onChange([...sections, { heading: '', bullets: '' }])

  return (
    <div className="templates-sections">
      {sections.map((s, i) => (
        <div className="templates-section" key={i}>
          <div className="templates-section-row">
            <Field label={`Section ${i + 1} heading`}>
              {(fieldProps) => (
                <Input {...fieldProps} value={s.heading} onChange={(e) => update(i, { heading: e.target.value })} />
              )}
            </Field>
            <Button variant="ghost" size="sm" onClick={() => remove(i)}>
              Remove
            </Button>
          </div>
          <Field label="Bullets" hint="One per line.">
            {(fieldProps) => (
              <Textarea {...fieldProps} mono={false} rows={3} value={s.bullets} onChange={(e) => update(i, { bullets: e.target.value })} />
            )}
          </Field>
        </div>
      ))}
      <Button variant="ghost" size="sm" onClick={add}>
        + Add section
      </Button>
    </div>
  )
}

interface TemplateContentFieldsProps {
  type: string
  draft: ContentDraft
  onChange: (draft: ContentDraft) => void
}

/** The type-conditional editor — shared by the create Modal and the manage
 *  Drawer's "Add version" step, since both produce the same content shape. */
function TemplateContentFields({ type, draft, onChange }: TemplateContentFieldsProps) {
  const set = <K extends keyof ContentDraft>(k: K, v: ContentDraft[K]) => onChange({ ...draft, [k]: v })

  if (isSectionsType(type)) {
    return <SectionsEditor sections={draft.sections} onChange={(sections) => set('sections', sections)} />
  }

  if (type === 'workflow') {
    return (
      <div className="templates-content-fields">
        <Field label="Goal" hint="One sentence — what this workflow produces.">
          {(fieldProps) => <Input {...fieldProps} value={draft.goal} onChange={(e) => set('goal', e.target.value)} />}
        </Field>
        <Field label="Inputs" hint="One per line — field names the workflow needs, e.g. customer_id">
          {(fieldProps) => (
            <Textarea {...fieldProps} mono={false} rows={3} value={draft.inputs} onChange={(e) => set('inputs', e.target.value)} />
          )}
        </Field>
        <Field label="Steps" hint="One per line — governed tools to call, in order, e.g. get_customer_overview">
          {(fieldProps) => (
            <Textarea {...fieldProps} mono={false} rows={3} value={draft.steps} onChange={(e) => set('steps', e.target.value)} />
          )}
        </Field>
        <Field label="Outputs" hint="One per line — output formats, e.g. xlsx">
          {(fieldProps) => (
            <Textarea {...fieldProps} mono={false} rows={2} value={draft.outputs} onChange={(e) => set('outputs', e.target.value)} />
          )}
        </Field>
      </div>
    )
  }

  if (type === 'email') {
    return (
      <div className="templates-content-fields">
        <Field label="Subject">
          {(fieldProps) => <Input {...fieldProps} value={draft.subject} onChange={(e) => set('subject', e.target.value)} />}
        </Field>
        <Field label="Body">
          {(fieldProps) => (
            <Textarea {...fieldProps} mono={false} rows={6} value={draft.body} onChange={(e) => set('body', e.target.value)} />
          )}
        </Field>
      </div>
    )
  }

  if (type === 'calendar') {
    return (
      <div className="templates-content-fields">
        <div className="templates-field-grid">
          <Field label="Title">
            {(fieldProps) => <Input {...fieldProps} value={draft.calTitle} onChange={(e) => set('calTitle', e.target.value)} />}
          </Field>
          <Field label="Default location">
            {(fieldProps) => (
              <Input {...fieldProps} placeholder="Teams / Boardroom" value={draft.calLocation} onChange={(e) => set('calLocation', e.target.value)} />
            )}
          </Field>
          <Field label="Duration (minutes)">
            {(fieldProps) => (
              <Input {...fieldProps} type="number" min={0} value={draft.calDuration} onChange={(e) => set('calDuration', e.target.value)} />
            )}
          </Field>
        </div>
        <Field label="Description">
          {(fieldProps) => (
            <Textarea {...fieldProps} mono={false} rows={4} value={draft.calDescription} onChange={(e) => set('calDescription', e.target.value)} />
          )}
        </Field>
      </div>
    )
  }

  // prompt
  return (
    <div className="templates-content-fields">
      <Field label="Prompt text" hint="The reusable instruction or prompt body.">
        {(fieldProps) => (
          <Textarea {...fieldProps} mono={false} rows={7} value={draft.promptText} onChange={(e) => set('promptText', e.target.value)} />
        )}
      </Field>
    </div>
  )
}

/** Read-only counterpart to `TemplateContentFields` — the same shape,
 *  rendered as plain text/lists instead of form controls. */
function TemplateContentView({ type, content }: { type: string; content: Record<string, unknown> }) {
  const draft = useMemo(() => draftFromContent(type, content), [type, content])
  const extraKeys = Object.keys(draft.extra)

  return (
    <div className="templates-content-view">
      {isSectionsType(type) &&
        (draft.sections.length === 0 ? (
          <p className="templates-note">No sections yet.</p>
        ) : (
          draft.sections.map((s, i) => (
            <div className="templates-view-section" key={i}>
              <h4 className="templates-view-heading">{s.heading || 'Untitled section'}</h4>
              {s.bullets.trim() ? (
                <ul className="templates-view-bullets">
                  {splitLines(s.bullets).map((b, bi) => (
                    <li key={bi}>{b}</li>
                  ))}
                </ul>
              ) : (
                <p className="templates-note">No bullets.</p>
              )}
            </div>
          ))
        ))}

      {type === 'workflow' && (
        <>
          <p className="templates-view-line">
            <strong>Goal:</strong> {draft.goal || '—'}
          </p>
          <p className="templates-view-line">
            <strong>Inputs:</strong> {splitLines(draft.inputs).join(', ') || '—'}
          </p>
          <p className="templates-view-line">
            <strong>Steps:</strong>
          </p>
          <ol className="templates-view-bullets">
            {splitLines(draft.steps).map((s, i) => (
              <li key={i} className="ui-mono">
                {s}
              </li>
            ))}
          </ol>
          <p className="templates-view-line">
            <strong>Outputs:</strong> {splitLines(draft.outputs).join(', ') || '—'}
          </p>
        </>
      )}

      {type === 'email' && (
        <>
          <p className="templates-view-line">
            <strong>Subject:</strong> {draft.subject || '—'}
          </p>
          <p className="templates-view-body">{draft.body || '—'}</p>
        </>
      )}

      {type === 'calendar' && (
        <KeyValue
          columns={2}
          items={[
            { label: 'Title', value: draft.calTitle },
            { label: 'Location', value: draft.calLocation },
            { label: 'Duration', value: draft.calDuration ? `${draft.calDuration} min` : undefined },
            { label: 'Description', value: draft.calDescription, wide: true },
          ]}
        />
      )}

      {type === 'prompt' && <p className="templates-view-body">{draft.promptText || '—'}</p>}

      {extraKeys.length > 0 && (
        <div className="templates-extra">
          <p className="ui-eyebrow templates-section-label">Additional data</p>
          <CodeBlock>{JSON.stringify(draft.extra, null, 2)}</CodeBlock>
        </div>
      )}
    </div>
  )
}

function TemplatesPage({ session }: PageProps) {
  const toast = useToast()

  const [templates, setTemplates] = useState<Template[]>([])
  const [state, setState] = useState<'loading' | 'ready' | 'error'>('loading')
  const [error, setError] = useState<string | null>(null)

  // ── create modal ─────────────────────────────────────────────────────────
  const [createOpen, setCreateOpen] = useState(false)
  const [createName, setCreateName] = useState('')
  const [createType, setCreateType] = useState<string>('generic')
  const [createDescription, setCreateDescription] = useState('')
  const [createTags, setCreateTags] = useState('')
  const [createWorkflows, setCreateWorkflows] = useState('')
  const [createContentDraft, setCreateContentDraft] = useState<ContentDraft>(() => defaultDraftForType('generic'))
  const [creating, setCreating] = useState(false)

  // ── manage drawer ────────────────────────────────────────────────────────
  const [manageId, setManageId] = useState<string | null>(null)
  const [manageDetail, setManageDetail] = useState<Template | null>(null)
  const [detailState, setDetailState] = useState<'loading' | 'ready' | 'error'>('loading')
  const [detailError, setDetailError] = useState<string | null>(null)

  const [addVersionOpen, setAddVersionOpen] = useState(false)
  const [versionContentDraft, setVersionContentDraft] = useState<ContentDraft>(emptyDraft)
  const [versionNotes, setVersionNotes] = useState('')
  const [savingVersion, setSavingVersion] = useState(false)

  const [pendingDisable, setPendingDisable] = useState(false)
  const [disabling, setDisabling] = useState(false)

  const load = useCallback(() => {
    let live = true
    setState('loading')

    listTemplates(session.isAdmin)
      .then((result) => {
        if (!live) return
        setTemplates(result.templates ?? [])
        setError(null)
        setState('ready')
      })
      .catch((cause: unknown) => {
        if (!live) return
        setError(cause instanceof Error ? cause.message : 'Could not load templates.')
        setState('error')
      })

    return () => {
      live = false
    }
  }, [session.isAdmin])

  useEffect(() => load(), [load])

  const onCreateTypeChange = useCallback((type: string) => {
    setCreateType(type)
    setCreateContentDraft(defaultDraftForType(type))
  }, [])

  const closeCreateModal = useCallback(() => {
    setCreateOpen(false)
    setCreateName('')
    setCreateType('generic')
    setCreateDescription('')
    setCreateTags('')
    setCreateWorkflows('')
    setCreateContentDraft(defaultDraftForType('generic'))
  }, [])

  const submitCreate = useCallback(async () => {
    const name = createName.trim()
    if (!name) {
      toast.warn('Name is required')
      return
    }
    const validationError = validateDraft(createType, createContentDraft)
    if (validationError) {
      toast.warn(validationError)
      return
    }
    setCreating(true)
    try {
      await createTemplate({
        display_name: name,
        template_type: createType,
        content: contentFromDraft(createType, createContentDraft),
        description: createDescription.trim(),
        tags: createTags.split(',').map((s) => s.trim()).filter(Boolean),
        allowed_workflow_ids: createWorkflows.split(',').map((s) => s.trim()).filter(Boolean),
        notes: 'Initial version',
      })
      toast.success('Template created')
      closeCreateModal()
      load()
    } catch (cause) {
      toast.error(cause instanceof ApiError ? cause.message : 'Could not create that template.')
    } finally {
      setCreating(false)
    }
  }, [createName, createType, createContentDraft, createDescription, createTags, createWorkflows, toast, closeCreateModal, load])

  // Resets the drawer's own UI state only — never refetches. Creating,
  // versioning, or disabling reloads itself at the point it succeeds, so
  // opening a template just to look and closing it again doesn't re-render
  // the table underneath it.
  const closeManage = useCallback(() => {
    setManageId(null)
    setAddVersionOpen(false)
    setPendingDisable(false)
  }, [])

  const openManage = useCallback((template: Template) => {
    setManageId(template.templateId)
    setManageDetail(null)
    setDetailState('loading')
    setAddVersionOpen(false)
    setPendingDisable(false)

    getTemplate(template.templateId)
      .then((detail) => {
        setManageDetail(detail)
        setDetailError(null)
        setDetailState('ready')
      })
      .catch((cause: unknown) => {
        setDetailError(cause instanceof Error ? cause.message : 'Could not load this template.')
        setDetailState('error')
      })
  }, [])

  const openAddVersion = useCallback(() => {
    if (!manageDetail) return
    const latest = manageDetail.versions?.find((v) => v.version === manageDetail.currentVersion)
    setVersionContentDraft(draftFromContent(manageDetail.templateType, latest?.content ?? manageDetail.content ?? {}))
    setVersionNotes('')
    setAddVersionOpen(true)
  }, [manageDetail])

  const submitVersion = useCallback(async () => {
    if (!manageDetail) return
    const validationError = validateDraft(manageDetail.templateType, versionContentDraft)
    if (validationError) {
      toast.warn(validationError)
      return
    }
    setSavingVersion(true)
    try {
      const content = contentFromDraft(manageDetail.templateType, versionContentDraft)
      const updated = await addTemplateVersion(manageDetail.templateId, content, versionNotes.trim())
      setManageDetail(updated)
      setAddVersionOpen(false)
      toast.success('Version added')
      load()
    } catch (cause) {
      toast.error(cause instanceof ApiError ? cause.message : 'Could not add that version.')
    } finally {
      setSavingVersion(false)
    }
  }, [manageDetail, versionContentDraft, versionNotes, toast, load])

  const confirmDisable = useCallback(async () => {
    if (!manageDetail) return
    setDisabling(true)
    try {
      const updated = await disableTemplate(manageDetail.templateId)
      setManageDetail(updated)
      setPendingDisable(false)
      toast.success('Template disabled')
      load()
    } catch (cause) {
      toast.error(cause instanceof ApiError ? cause.message : 'Could not disable that template.')
    } finally {
      setDisabling(false)
    }
  }, [manageDetail, toast, load])

  const columns = useMemo<Column<Template>[]>(
    () => [
      {
        key: 'name',
        header: 'Template',
        render: (t) => (
          <>
            <div>{t.displayName}</div>
            <div className="templates-subtext ui-mono">{t.templateId}</div>
            {t.description && <div className="templates-subtext">{t.description}</div>}
          </>
        ),
      },
      {
        key: 'type',
        header: 'Type',
        width: '9.5rem',
        render: (t) => <Badge subtle>{TYPE_LABEL[t.templateType] ?? t.templateType}</Badge>,
      },
      {
        key: 'status',
        header: 'Status',
        width: '7rem',
        render: (t) => (
          <Badge tone={statusTone(t.status)} dot>
            {t.status}
          </Badge>
        ),
      },
      { key: 'version', header: 'Version', width: '6rem', muted: true, render: (t) => `v${t.currentVersion}` },
      {
        key: 'tags',
        header: 'Tags',
        render: (t) =>
          t.tags.length === 0 ? (
            <span className="templates-none">—</span>
          ) : (
            <div className="templates-pill-grid">
              {t.tags.map((tag) => (
                <Badge key={tag} subtle>
                  {tag}
                </Badge>
              ))}
            </div>
          ),
      },
      {
        key: 'actions',
        header: '',
        srHeader: 'Inspect',
        width: '7rem',
        align: 'right',
        render: (t) => (
          <Button size="sm" variant="ghost" onClick={() => openManage(t)}>
            Inspect
          </Button>
        ),
      },
    ],
    [openManage],
  )

  const versionColumns = useMemo<Column<TemplateVersionRecord>[]>(
    () => [
      { key: 'version', header: 'Version', width: '5rem', render: (v) => `v${v.version}` },
      { key: 'createdBy', header: 'By', width: '9rem' },
      {
        key: 'createdAt',
        header: 'When',
        width: '9rem',
        muted: true,
        nowrap: true,
        render: (v) => <span title={formatWhen(v.createdAt)}>{formatRelative(v.createdAt)}</span>,
      },
      { key: 'notes', header: 'Notes', muted: true, render: (v) => v.notes || '—' },
    ],
    [],
  )

  const currentContent = manageDetail
    ? (manageDetail.versions?.find((v) => v.version === manageDetail.currentVersion)?.content ?? manageDetail.content ?? {})
    : {}

  return (
    <PageShell className="templates">
      <Card
        title="Templates"
        description="Versioned reusable templates for governed office workflows and generated artifacts."
        flush
        actions={session.isAdmin ? <AddButton onClick={() => setCreateOpen(true)} label="Add a new template" /> : undefined}
      >
        {state === 'error' ? (
          <EmptyState
            title="Couldn't load templates"
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
            rows={templates}
            rowKey={(t) => t.templateId}
            loading={state === 'loading'}
            skeletonRows={5}
            caption="Reusable templates"
            empty={
              <EmptyState
                title="No templates yet"
                description={session.isAdmin ? 'Add the first one with the + above.' : 'None have been created yet.'}
              />
            }
          />
        )}
      </Card>

      <Drawer
        open={manageId != null}
        onClose={closeManage}
        eyebrow="Template"
        title={manageDetail?.displayName || manageId || ''}
        monoTitle={!manageDetail?.displayName}
        footer={
          pendingDisable ? (
            <>
              <Button variant="ghost" onClick={() => setPendingDisable(false)} disabled={disabling}>
                Cancel
              </Button>
              <Button variant="danger" onClick={confirmDisable} loading={disabling}>
                Disable
              </Button>
            </>
          ) : addVersionOpen ? (
            <>
              <Button variant="ghost" onClick={() => setAddVersionOpen(false)} disabled={savingVersion}>
                Cancel
              </Button>
              <Button onClick={submitVersion} loading={savingVersion}>
                Save new version
              </Button>
            </>
          ) : session.isAdmin && manageDetail ? (
            <>
              <Button
                variant="danger"
                size="sm"
                onClick={() => setPendingDisable(true)}
                disabled={manageDetail.status === 'disabled'}
              >
                {manageDetail.status === 'disabled' ? 'Already disabled' : 'Disable'}
              </Button>
              <Button size="sm" onClick={openAddVersion}>
                Add version
              </Button>
            </>
          ) : undefined
        }
      >
        {detailState === 'loading' ? (
          <Skeleton height="12rem" />
        ) : detailState === 'error' ? (
          <p className="templates-note">{detailError}</p>
        ) : !manageDetail ? null : pendingDisable ? (
          <p className="templates-confirm-msg">
            Disable this template? It stops showing up for anyone without admin access, but its versions and
            history stay intact.
          </p>
        ) : addVersionOpen ? (
          <div className="templates-drawer-body">
            <p className="templates-note">
              Editing content for the current type — {TYPE_LABEL[manageDetail.templateType] ?? manageDetail.templateType}.
              The type itself can't change.
            </p>
            <TemplateContentFields type={manageDetail.templateType} draft={versionContentDraft} onChange={setVersionContentDraft} />
            <Field label="What changed" hint="Optional note for this version.">
              {(fieldProps) => <Input {...fieldProps} value={versionNotes} onChange={(e) => setVersionNotes(e.target.value)} />}
            </Field>
          </div>
        ) : (
          <div className="templates-drawer-body">
            <div className="templates-section-block">
              <p className="ui-eyebrow templates-section-label">Details</p>
              <KeyValue
                columns={2}
                items={[
                  { label: 'Type', value: TYPE_LABEL[manageDetail.templateType] ?? manageDetail.templateType },
                  {
                    label: 'Status',
                    value: (
                      <Badge tone={statusTone(manageDetail.status)} dot>
                        {manageDetail.status}
                      </Badge>
                    ),
                  },
                  { label: 'Version', value: `v${manageDetail.currentVersion}` },
                  { label: 'Owner', value: manageDetail.owner || 'system' },
                  { label: 'Description', value: manageDetail.description, wide: true },
                  { label: 'Tags', value: manageDetail.tags.join(', '), wide: true },
                ]}
              />
            </div>

            <div className="templates-section-block">
              <p className="ui-eyebrow templates-section-label">Content</p>
              <TemplateContentView type={manageDetail.templateType} content={currentContent} />
            </div>

            <div className="templates-section-block">
              <p className="ui-eyebrow templates-section-label">Version history</p>
              <DataTable
                columns={versionColumns}
                rows={manageDetail.versions ?? []}
                rowKey={(v) => String(v.version)}
                dense
                caption="Version history"
                empty={<EmptyState title="No versions" compact />}
              />
            </div>
          </div>
        )}
      </Drawer>

      <Modal
        open={createOpen}
        onClose={closeCreateModal}
        eyebrow="Template"
        title="New template"
        footer={
          <>
            <Button variant="ghost" onClick={closeCreateModal} disabled={creating}>
              Cancel
            </Button>
            <Button onClick={submitCreate} loading={creating}>
              Create template
            </Button>
          </>
        }
      >
        <div className="templates-form">
          <Field label="Name">
            {(fieldProps) => (
              <Input {...fieldProps} placeholder="Quarterly Account Review" value={createName} onChange={(e) => setCreateName(e.target.value)} />
            )}
          </Field>

          <Field label="Type">
            {(fieldProps) => (
              <Dropdown {...fieldProps} value={createType} onChange={onCreateTypeChange} options={TEMPLATE_TYPE_OPTIONS} />
            )}
          </Field>

          <Field label="Description">
            {(fieldProps) => (
              <Input
                {...fieldProps}
                placeholder="Template purpose and intended use"
                value={createDescription}
                onChange={(e) => setCreateDescription(e.target.value)}
              />
            )}
          </Field>

          <Field label="Tags" hint="Comma-separated, e.g. customer, qbr">
            {(fieldProps) => (
              <Input {...fieldProps} placeholder="customer, qbr" value={createTags} onChange={(e) => setCreateTags(e.target.value)} />
            )}
          </Field>

          <Field label="Allowed workflows" hint="Comma-separated workflow template ids this may be used from.">
            {(fieldProps) => (
              <Input
                {...fieldProps}
                placeholder="customer_360_report"
                value={createWorkflows}
                onChange={(e) => setCreateWorkflows(e.target.value)}
              />
            )}
          </Field>

          <div className="templates-content-divider">
            <p className="ui-eyebrow templates-section-label">Content</p>
            <TemplateContentFields type={createType} draft={createContentDraft} onChange={setCreateContentDraft} />
          </div>
        </div>
      </Modal>
    </PageShell>
  )
}

export default TemplatesPage
