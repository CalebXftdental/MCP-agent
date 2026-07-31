import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Badge,
  Button,
  Card,
  DataTable,
  Dropdown,
  EmptyState,
  Field,
  Input,
  Modal,
  SegmentedControl,
  useToast,
  type Column,
  type DropdownOption,
} from '../components/ui'
import { SampleToggle, WorkflowInputFields, WorkflowReadiness } from '../components/workflow/WorkflowInputFields'
import WorkflowAlertBadge from '../components/workflow/WorkflowAlertBadge'
import { useWorkflowInputs } from '../hooks/useWorkflowInputs'
import {
  ApiError,
  createAutomation,
  deleteAutomation,
  listAutomations,
  listWorkflows,
  type Automation,
  type WorkflowTemplate,
} from '../lib/api'
import { formatRelative, formatWhen } from '../lib/format'
import type { PageProps } from './types'
import './AutomationsPage.css'

/**
 * Automations — ports `renderAutomations()`: schedule a workflow to re-run on
 * its own.
 *
 * The legacy panel's biggest non-technical-hostile spot: scheduling meant
 * hand-typing a JSON blob into an "Inputs JSON" textarea, plus a raw
 * "interval minutes" number. Both are gone here — the same dynamic
 * `WorkflowInputFields` the Workflows tab uses fills `inputs` from labelled
 * fields (JSON-serialized only on the wire, never shown), and the schedule
 * is Daily/Weekly/Custom plus a time-of-day picker instead of a number of
 * minutes.
 */

type ScheduleKind = 'daily' | 'weekly' | 'custom'

function nextRunAtFor(kind: ScheduleKind, timeOfDay: string): number {
  const now = new Date()
  if (kind === 'custom') return Math.floor(now.getTime() / 1000)
  const [hh, mm] = timeOfDay.split(':').map((n) => Number(n) || 0)
  const next = new Date(now)
  next.setHours(hh, mm, 0, 0)
  if (next.getTime() <= now.getTime()) next.setDate(next.getDate() + (kind === 'weekly' ? 7 : 1))
  return Math.floor(next.getTime() / 1000)
}

function intervalSecFor(kind: ScheduleKind, customHours: string): number {
  if (kind === 'daily') return 86400
  if (kind === 'weekly') return 604800
  return Math.max(1, Math.round(Number(customHours) || 24)) * 3600
}

function scheduleSentence(a: Automation): string {
  const hours = a.intervalSec / 3600
  let cadence: string
  if (a.intervalSec === 86400) cadence = 'Runs daily'
  else if (a.intervalSec === 604800) cadence = 'Runs weekly'
  else if (hours < 24) cadence = `Runs every ${hours} hour${hours === 1 ? '' : 's'}`
  else cadence = `Runs every ${Math.round(hours / 24)} days`
  return `${cadence} · next ${formatRelative(a.nextRunAt)}`
}

function AutomationsPage({ session }: PageProps) {
  const toast = useToast()

  const [templates, setTemplates] = useState<WorkflowTemplate[]>([])
  const [automations, setAutomations] = useState<Automation[]>([])
  const [listState, setListState] = useState<'loading' | 'ready' | 'error'>('loading')

  const [templateId, setTemplateId] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [scheduleKind, setScheduleKind] = useState<ScheduleKind>('daily')
  const [timeOfDay, setTimeOfDay] = useState('06:00')
  const [customHours, setCustomHours] = useState('24')
  const [creating, setCreating] = useState(false)

  const [deleteTarget, setDeleteTarget] = useState<Automation | null>(null)
  const [deleting, setDeleting] = useState(false)

  const { values, setValue, preflight, checking, error: preflightError } = useWorkflowInputs(templateId || null)

  const load = useCallback(() => {
    setListState('loading')
    Promise.all([listWorkflows(), listAutomations()])
      .then(([w, a]) => {
        const active = w.workflows.filter((t) => t.status === 'active')
        setTemplates(active)
        setAutomations(a.automations)
        setListState('ready')
      })
      .catch(() => setListState('error'))
  }, [])

  useEffect(load, [load])

  const selectTemplate = useCallback((id: string) => {
    setTemplateId(id)
  }, [])

  useEffect(() => {
    const t = templates.find((x) => x.templateId === templateId)
    if (t) setDisplayName(t.displayName)
  }, [templateId, templates])

  const create = useCallback(async () => {
    if (!templateId) return
    setCreating(true)
    try {
      await createAutomation({
        template_id: templateId,
        display_name: displayName.trim() || templateId,
        inputs: values,
        interval_sec: intervalSecFor(scheduleKind, customHours),
        next_run_at: nextRunAtFor(scheduleKind, timeOfDay),
      })
      toast.success('Automation scheduled')
      setTemplateId('')
      setDisplayName('')
      load()
    } catch (cause) {
      toast.error(cause instanceof ApiError ? cause.message : 'Could not schedule that automation.')
    } finally {
      setCreating(false)
    }
  }, [templateId, displayName, values, scheduleKind, customHours, timeOfDay, load, toast])

  const confirmDelete = useCallback(async () => {
    if (!deleteTarget) return
    setDeleting(true)
    try {
      await deleteAutomation(deleteTarget.automationId)
      toast.success('Automation deleted')
      setDeleteTarget(null)
      load()
    } catch (cause) {
      toast.error(cause instanceof ApiError ? cause.message : 'Could not delete that automation.')
    } finally {
      setDeleting(false)
    }
  }, [deleteTarget, load, toast])

  const templateOptions = useMemo<DropdownOption[]>(
    () => templates.map((t) => ({ value: t.templateId, label: t.displayName })),
    [templates],
  )

  const templateDisplayName = useCallback(
    (id: string) => templates.find((t) => t.templateId === id)?.displayName ?? id,
    [templates],
  )

  const columns = useMemo<Column<Automation>[]>(
    () => [
      {
        key: 'name',
        header: 'Automation',
        render: (a) => (
          <>
            <div>{a.displayName}</div>
            <div className="automations-sub">{templateDisplayName(a.templateId)}</div>
          </>
        ),
      },
      {
        key: 'schedule',
        header: 'Schedule',
        render: (a) => scheduleSentence(a),
      },
      {
        key: 'last',
        header: 'Last run',
        width: '11rem',
        render: (a) =>
          a.lastRunAt ? (
            <>
              <Badge tone={a.lastStatus === 'completed' ? 'ok' : a.lastStatus === 'failed' ? 'danger' : 'neutral'} subtle>
                {a.lastStatus.replace(/_/g, ' ')}
              </Badge>
              <div className="automations-sub" title={formatWhen(a.lastRunAt)}>
                {formatRelative(a.lastRunAt)}
              </div>
            </>
          ) : (
            <span className="automations-sub">Never run yet</span>
          ),
      },
      {
        key: 'actions',
        header: '',
        srHeader: 'Actions',
        width: '6rem',
        align: 'right',
        render: (a) => (
          <Button size="sm" variant="danger" onClick={() => setDeleteTarget(a)}>
            Delete
          </Button>
        ),
      },
    ],
    [templateDisplayName],
  )

  return (
    <div className="automations">
      <Card
        title={
          <span className="workflows-run-title">
            Schedule an automation
            {preflight && !preflight.ready && <WorkflowAlertBadge blockers={preflight.blockers} />}
          </span>
        }
        description="Pick a workflow, fill in what it needs, and choose how often it should run on its own."
        actions={templateId ? <SampleToggle templateId={templateId} values={values} setValue={setValue} /> : undefined}
      >
        <div className="automations-form">
          <Field label="Workflow">
            {(fieldProps) => (
              <Dropdown
                {...fieldProps}
                value={templateId}
                onChange={selectTemplate}
                options={templateOptions}
                placeholder="Choose a workflow…"
              />
            )}
          </Field>

          {templateId && (
            <>
              <Field label="Name" hint="Shown in the schedule list below.">
                {(fieldProps) => (
                  <Input {...fieldProps} value={displayName} onChange={(e) => setDisplayName(e.target.value)} />
                )}
              </Field>

              <WorkflowInputFields
                templateId={templateId}
                requiredInputs={preflight?.requiredInputs ?? []}
                approvalGates={preflight?.approvalGates ?? []}
                values={values}
                setValue={setValue}
              />

              <WorkflowReadiness preflight={preflight} checking={checking} error={preflightError} />

              <div className="automations-schedule">
                <SegmentedControl
                  label="How often"
                  size="md"
                  value={scheduleKind}
                  onChange={setScheduleKind}
                  segments={[
                    { value: 'daily', label: 'Daily' },
                    { value: 'weekly', label: 'Weekly' },
                    { value: 'custom', label: 'Custom' },
                  ]}
                />
                {scheduleKind !== 'custom' ? (
                  <Field label="At" inline>
                    {(fieldProps) => (
                      <Input
                        {...fieldProps}
                        type="time"
                        value={timeOfDay}
                        onChange={(e) => setTimeOfDay(e.target.value)}
                      />
                    )}
                  </Field>
                ) : (
                  <Field label="Every" inline hint="hours">
                    {(fieldProps) => (
                      <Input
                        {...fieldProps}
                        type="number"
                        min={1}
                        value={customHours}
                        onChange={(e) => setCustomHours(e.target.value)}
                      />
                    )}
                  </Field>
                )}
              </div>

              <div className="automations-form-actions">
                <Button onClick={create} loading={creating} disabled={!!preflight && !preflight.ready}>
                  Schedule automation
                </Button>
              </div>
            </>
          )}
        </div>
      </Card>

      <Card
        title="Schedules"
        flush
        actions={
          <Button variant="quiet" size="sm" onClick={load} loading={listState === 'loading'}>
            Refresh
          </Button>
        }
      >
        <DataTable
          columns={columns}
          rows={automations}
          rowKey={(a) => a.automationId}
          loading={listState === 'loading'}
          caption="Scheduled workflow automations"
          empty={
            listState === 'error' ? (
              <EmptyState
                title="Couldn't load automations"
                action={
                  <Button size="sm" variant="ghost" onClick={load}>
                    Try again
                  </Button>
                }
              />
            ) : (
              <EmptyState title="No automations yet" description="Schedule one above to see it here." />
            )
          }
        />
      </Card>

      <Modal
        open={deleteTarget != null}
        onClose={() => setDeleteTarget(null)}
        eyebrow="Automation"
        title={`Delete ${deleteTarget?.displayName ?? ''}?`}
        footer={
          <>
            <Button variant="ghost" onClick={() => setDeleteTarget(null)} disabled={deleting}>
              Cancel
            </Button>
            <Button variant="danger" onClick={confirmDelete} loading={deleting}>
              Delete
            </Button>
          </>
        }
      >
        <p className="automations-modal-lead">This stops future scheduled runs — past runs and their files stay put.</p>
      </Modal>

      {!session.isAdmin && (
        <p className="automations-scope-note">You're seeing your own automations. An admin sees everyone's.</p>
      )}
    </div>
  )
}

export default AutomationsPage
