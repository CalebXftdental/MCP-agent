import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Badge,
  Button,
  Card,
  DataTable,
  Drawer,
  EmptyState,
  Field,
  Modal,
  Spinner,
  Textarea,
  useToast,
  type BadgeTone,
  type Column,
} from '../components/ui'
import { SampleToggle, WorkflowInputFields, WorkflowReadiness } from '../components/workflow/WorkflowInputFields'
import WorkflowCatalogTile from '../components/workflow/WorkflowCatalogTile'
import WorkflowInfoIcon from '../components/workflow/WorkflowInfoIcon'
import WorkflowAlertBadge from '../components/workflow/WorkflowAlertBadge'
import { useWorkflowInputs } from '../hooks/useWorkflowInputs'
import {
  ApiError,
  cancelWorkflowRun,
  exportWorkflowRunEvidence,
  getWorkflowHealth,
  getWorkflowRun,
  listWorkflowRuns,
  listWorkflows,
  resumeWorkflowRun,
  runWorkflow,
  setWorkflowTemplateStatus,
  type WorkflowHealth,
  type WorkflowRun,
  type WorkflowRunResult,
  type WorkflowTemplate,
} from '../lib/api'
import { formatRelative, formatWhen } from '../lib/format'
import type { PageProps } from './types'
import './WorkflowsPage.css'

/**
 * Workflows — ports `renderWorkflows()`: pick a governed office workflow, run
 * it, and see recent runs.
 *
 * Three deliberate departures from the legacy panel, all aimed at the same
 * "not intuitive for non-technical people" complaint:
 *   - The run form only shows the fields THIS workflow declares
 *     (`useWorkflowInputs`/`WorkflowInputFields`), not one static grid of
 *     every field any workflow might ever use.
 *   - Readiness checks itself automatically as the form changes and reads as
 *     plain sentences (`WorkflowReadiness`), not a separate "Check readiness"
 *     button and a wall of Blockers/Warnings pills.
 *   - Resume no longer asks for an approval id via `window.prompt` — the
 *     endpoint already resolves the run's own pending approval when none is
 *     given, so a plain "Resume" button is both simpler and correct. Disable
 *     and Cancel use a Modal instead of `prompt`/`confirm` for the same
 *     "don't block the tab on a native dialog" reason the Approvals page
 *     redesign already established.
 */

type RunPhase = 'idle' | 'running' | 'done' | 'error'

function statusTone(status: string): BadgeTone {
  if (status === 'completed') return 'ok'
  if (status === 'failed' || status === 'cancelled') return 'danger'
  if (status === 'approval_required') return 'warn'
  return 'neutral'
}

function WorkflowsPage({ session }: PageProps) {
  const toast = useToast()

  const [templates, setTemplates] = useState<WorkflowTemplate[]>([])
  const [templatesState, setTemplatesState] = useState<'loading' | 'ready' | 'error'>('loading')
  const [selectedId, setSelectedId] = useState<string | null>(null)

  const [runs, setRuns] = useState<WorkflowRun[]>([])
  const [runsState, setRunsState] = useState<'loading' | 'ready' | 'error'>('loading')

  const [health, setHealth] = useState<WorkflowHealth | null>(null)

  const [runPhase, setRunPhase] = useState<RunPhase>('idle')
  const [runResult, setRunResult] = useState<WorkflowRunResult | null>(null)
  const [runError, setRunError] = useState<string | null>(null)

  const [detail, setDetail] = useState<(WorkflowRun & { timeline?: unknown[] }) | null>(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const [busyRunId, setBusyRunId] = useState<string | null>(null)

  const [disableTarget, setDisableTarget] = useState<WorkflowTemplate | null>(null)
  const [disableReason, setDisableReason] = useState('')
  const [cancelTarget, setCancelTarget] = useState<WorkflowRun | null>(null)
  const [decisionBusy, setDecisionBusy] = useState(false)

  const { values, setValue, preflight, checking, error: preflightError } = useWorkflowInputs(selectedId)
  // True only for the FIRST readiness check right after picking a workflow —
  // once preflight (or an error) lands, later re-checks as fields change must
  // not hide the form again, or it'd vanish on every keystroke.
  const firstCheckPending = !!selectedId && checking && !preflight && !preflightError

  const loadTemplates = useCallback(() => {
    setTemplatesState('loading')
    listWorkflows()
      .then((result) => {
        setTemplates(result.workflows)
        setTemplatesState('ready')
      })
      .catch(() => setTemplatesState('error'))
  }, [])

  const loadRuns = useCallback(() => {
    setRunsState('loading')
    listWorkflowRuns(session.isAdmin)
      .then((result) => {
        setRuns(result.runs)
        setRunsState('ready')
      })
      .catch(() => setRunsState('error'))
  }, [session.isAdmin])

  const loadHealth = useCallback(() => {
    if (!session.isAdmin) return
    getWorkflowHealth()
      .then(setHealth)
      .catch(() => setHealth(null))
  }, [session.isAdmin])

  useEffect(loadTemplates, [loadTemplates])
  useEffect(loadRuns, [loadRuns])
  useEffect(loadHealth, [loadHealth])

  const selectedTemplate = useMemo(
    () => templates.find((t) => t.templateId === selectedId) ?? null,
    [templates, selectedId],
  )

  const selectTemplate = useCallback((template: WorkflowTemplate) => {
    if (template.status !== 'active') return
    setSelectedId(template.templateId)
    setRunPhase('idle')
    setRunResult(null)
    setRunError(null)
  }, [])

  const run = useCallback(async () => {
    if (!selectedId) return
    setRunPhase('running')
    setRunError(null)
    try {
      const result = await runWorkflow(selectedId, values)
      setRunResult(result)
      setRunPhase('done')
      loadRuns()
    } catch (cause) {
      setRunError(cause instanceof ApiError ? cause.message : 'Could not run that workflow.')
      setRunPhase('error')
    }
  }, [selectedId, values, loadRuns])

  const openDetail = useCallback(async (r: WorkflowRun) => {
    setDetailLoading(true)
    setDetail({ ...r })
    try {
      const full = await getWorkflowRun(r.runId, true)
      setDetail(full)
    } catch {
      toast.error('Could not load that run.')
      setDetail(null)
    } finally {
      setDetailLoading(false)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const doResume = useCallback(
    async (runId: string) => {
      setBusyRunId(runId)
      try {
        await resumeWorkflowRun(runId, '')
        toast.success('Workflow resumed')
        loadRuns()
        if (detail?.runId === runId) openDetail(await getWorkflowRun(runId))
      } catch (cause) {
        toast.error(cause instanceof ApiError ? cause.message : 'Could not resume that run.')
      } finally {
        setBusyRunId(null)
      }
    },
    [loadRuns, detail, openDetail, toast],
  )

  const confirmCancel = useCallback(async () => {
    if (!cancelTarget) return
    setDecisionBusy(true)
    try {
      await cancelWorkflowRun(cancelTarget.runId, 'Cancelled from the Workflows tab')
      toast.success('Workflow cancelled')
      setCancelTarget(null)
      loadRuns()
    } catch (cause) {
      toast.error(cause instanceof ApiError ? cause.message : 'Could not cancel that run.')
    } finally {
      setDecisionBusy(false)
    }
  }, [cancelTarget, loadRuns, toast])

  const exportEvidence = useCallback(
    async (runId: string) => {
      try {
        const result = await exportWorkflowRunEvidence(runId)
        toast.success(`Evidence exported: ${result.artifact.filename}`)
      } catch (cause) {
        toast.error(cause instanceof ApiError ? cause.message : 'Could not export evidence.')
      }
    },
    [toast],
  )

  const confirmDisable = useCallback(async () => {
    if (!disableTarget) return
    setDecisionBusy(true)
    try {
      await setWorkflowTemplateStatus(disableTarget.templateId, 'disable', disableReason.trim())
      toast.success('Workflow disabled')
      setDisableTarget(null)
      loadTemplates()
    } catch (cause) {
      toast.error(cause instanceof ApiError ? cause.message : 'Could not disable that workflow.')
    } finally {
      setDecisionBusy(false)
    }
  }, [disableTarget, disableReason, loadTemplates, toast])

  const enableTemplate = useCallback(
    async (template: WorkflowTemplate) => {
      try {
        await setWorkflowTemplateStatus(template.templateId, 'enable')
        toast.success('Workflow enabled')
        loadTemplates()
      } catch (cause) {
        toast.error(cause instanceof ApiError ? cause.message : 'Could not enable that workflow.')
      }
    },
    [loadTemplates, toast],
  )

  const templateDisplayName = useCallback(
    (templateId: string) => templates.find((t) => t.templateId === templateId)?.displayName ?? templateId,
    [templates],
  )

  const runColumns = useMemo<Column<WorkflowRun>[]>(
    () => [
      {
        key: 'workflow',
        header: 'Workflow',
        render: (r) => (
          <>
            <div>{templateDisplayName(r.templateId)}</div>
            <div className="ui-mono wf-run-id">{r.runId}</div>
          </>
        ),
      },
      {
        key: 'status',
        header: 'Status',
        width: '10rem',
        render: (r) => (
          <Badge tone={statusTone(r.status)} dot>
            {r.status.replace(/_/g, ' ')}
          </Badge>
        ),
      },
      {
        key: 'artifacts',
        header: 'Artifacts',
        render: (r) => (r.artifactIds.length ? `${r.artifactIds.length} file${r.artifactIds.length > 1 ? 's' : ''}` : '—'),
      },
      {
        key: 'created',
        header: 'Started',
        width: '9rem',
        muted: true,
        nowrap: true,
        render: (r) => <span title={formatWhen(r.createdAt)}>{formatRelative(r.createdAt)}</span>,
      },
      {
        key: 'actions',
        header: '',
        srHeader: 'Actions',
        width: '13rem',
        align: 'right',
        render: (r) => (
          <div className="wf-run-actions" onClick={(e) => e.stopPropagation()}>
            {r.status === 'approval_required' && (
              <Button size="sm" loading={busyRunId === r.runId} onClick={() => doResume(r.runId)}>
                Resume
              </Button>
            )}
            {(r.status === 'running' || r.status === 'approval_required') && (
              <Button size="sm" variant="danger" onClick={() => setCancelTarget(r)}>
                Cancel
              </Button>
            )}
          </div>
        ),
      },
    ],
    [templateDisplayName, busyRunId, doResume],
  )

  return (
    <div className="workflows">
      {session.isAdmin && health && (
        <Card className="workflows-health" title="Workflow health" description="Run quality across every workflow, derived from persisted runs.">
          <div className="workflows-health-kpis">
            <div className="workflows-kpi">
              <span className="workflows-kpi-label">Total runs</span>
              <span className="workflows-kpi-value">{health.totalRuns}</span>
            </div>
            <div className="workflows-kpi">
              <span className="workflows-kpi-label">Failed</span>
              <span className="workflows-kpi-value">{health.failedRuns}</span>
            </div>
            <div className="workflows-kpi">
              <span className="workflows-kpi-label">Approval gate</span>
              <span className="workflows-kpi-value">{health.approvalRate}%</span>
            </div>
            <div className="workflows-kpi">
              <span className="workflows-kpi-label">Stuck</span>
              <span className="workflows-kpi-value">{health.stuckRuns.length}</span>
            </div>
            <Badge tone={health.health === 'healthy' ? 'ok' : health.health === 'degraded' ? 'warn' : 'danger'} dot>
              {health.health}
            </Badge>
          </div>
        </Card>
      )}

      <Card
        title="Workflow catalog"
        description="Executable office workflows that turn governed data into files, review packets, and workbench artifacts."
        flush
      >
        {templatesState === 'loading' ? (
          <div className="workflows-catalog-loading">Loading workflows…</div>
        ) : templatesState === 'error' ? (
          <EmptyState
            title="Couldn't load the workflow catalog"
            action={
              <Button size="sm" variant="ghost" onClick={loadTemplates}>
                Try again
              </Button>
            }
          />
        ) : (
          <div className="workflows-catalog">
            {templates.map((t) => (
              <WorkflowCatalogTile
                key={t.templateId}
                template={t}
                selected={t.templateId === selectedId}
                onSelect={selectTemplate}
                adminActions={
                  session.isAdmin ? (
                    t.status === 'active' ? (
                      <Button size="sm" variant="ghost" onClick={() => setDisableTarget(t)}>
                        Disable
                      </Button>
                    ) : (
                      <Button size="sm" variant="ghost" onClick={() => enableTemplate(t)}>
                        Enable
                      </Button>
                    )
                  ) : undefined
                }
              />
            ))}
          </div>
        )}
      </Card>

      <Card
        title={
          <span className="workflows-run-title">
            Run a workflow
            {selectedTemplate && !firstCheckPending && <WorkflowInfoIcon template={selectedTemplate} />}
            {preflight && !preflight.ready && <WorkflowAlertBadge blockers={preflight.blockers} />}
          </span>
        }
        description={!selectedTemplate ? 'Pick a workflow above to get started.' : undefined}
        actions={
          selectedTemplate && !firstCheckPending ? (
            <SampleToggle templateId={selectedTemplate.templateId} values={values} setValue={setValue} />
          ) : undefined
        }
      >
        {!selectedTemplate ? (
          <EmptyState title="No workflow selected" description="Choose one from the catalog above." compact />
        ) : firstCheckPending ? (
          <Spinner center label="Checking what this workflow needs…" />
        ) : (
          <div className="workflows-run-form">
            <WorkflowInputFields
              templateId={selectedTemplate.templateId}
              requiredInputs={preflight?.requiredInputs ?? []}
              approvalGates={preflight?.approvalGates ?? []}
              values={values}
              setValue={setValue}
              actions={
                <Button onClick={run} loading={runPhase === 'running'} disabled={!!preflight && !preflight.ready}>
                  Run workflow
                </Button>
              }
            />

            <WorkflowReadiness preflight={preflight} checking={checking} error={preflightError} />

            {runPhase === 'error' && <p className="workflows-run-error">⚠ {runError}</p>}

            {runPhase === 'done' && runResult && (
              <div className="workflows-run-result">
                <div className="workflows-run-result-head">
                  <Badge tone={statusTone(runResult.status)} dot>
                    {runResult.status.replace(/_/g, ' ')}
                  </Badge>
                  <span className="ui-mono workflows-run-result-id">{runResult.runId}</span>
                </div>
                {runResult.approval && (
                  <p className="workflows-run-result-note">
                    An admin needs to approve this before it's done — you'll see it move in Recent runs below.
                  </p>
                )}
                {!!runResult.artifacts?.length && (
                  <div className="workflows-run-result-artifacts">
                    {runResult.artifacts.map((a) => (
                      <a key={a.artifactId} href={a.downloadUrl} className="workflows-run-result-artifact">
                        {a.filename}
                      </a>
                    ))}
                  </div>
                )}
              </div>
            )}
          </div>
        )}
      </Card>

      <Card
        title="Recent workflow runs"
        flush
        actions={
          <Button variant="quiet" size="sm" onClick={loadRuns} loading={runsState === 'loading'}>
            Refresh
          </Button>
        }
      >
        <DataTable
          columns={runColumns}
          rows={runs}
          rowKey={(r) => r.runId}
          loading={runsState === 'loading'}
          onRowClick={openDetail}
          caption="Recent workflow runs"
          empty={
            runsState === 'error' ? (
              <EmptyState
                title="Couldn't load recent runs"
                action={
                  <Button size="sm" variant="ghost" onClick={loadRuns}>
                    Try again
                  </Button>
                }
              />
            ) : (
              <EmptyState title="No runs yet" description="Run a workflow above to see it here." />
            )
          }
        />
      </Card>

      <Drawer
        open={detail != null}
        onClose={() => setDetail(null)}
        eyebrow="Workflow run"
        title={detail ? templateDisplayName(detail.templateId) : ''}
        monoTitle={false}
        footer={
          detail && (
            <>
              <Button variant="ghost" onClick={() => exportEvidence(detail.runId)}>
                Export evidence
              </Button>
              {detail.status === 'approval_required' && (
                <Button loading={busyRunId === detail.runId} onClick={() => doResume(detail.runId)}>
                  Resume
                </Button>
              )}
            </>
          )
        }
      >
        {detail && (
          <div className="workflows-detail">
            <div className="workflows-detail-head">
              <Badge tone={statusTone(detail.status)} dot>
                {detail.status.replace(/_/g, ' ')}
              </Badge>
              <span className="ui-mono">{detail.runId}</span>
            </div>
            <p className="workflows-detail-meta">
              Requested by {detail.requestedBy} · {formatWhen(detail.createdAt)}
            </p>
            {detail.error && <p className="workflows-detail-error">⚠ {detail.error}</p>}

            <h3 className="workflows-detail-subhead">Steps</h3>
            {detailLoading ? (
              <p className="workflows-detail-loading">Loading…</p>
            ) : detail.steps.length === 0 ? (
              <p className="workflows-detail-loading">No steps recorded.</p>
            ) : (
              <ul className="workflows-detail-steps">
                {detail.steps.map((s) => (
                  <li key={s.stepId} className="workflows-detail-step">
                    <div className="workflows-detail-step-head">
                      <span>{s.title || s.stepId}</span>
                      <Badge tone={statusTone(s.status)} subtle>
                        {s.status}
                      </Badge>
                    </div>
                    {s.error && <p className="workflows-detail-step-error">{s.error}</p>}
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}
      </Drawer>

      <Modal
        open={disableTarget != null}
        onClose={() => setDisableTarget(null)}
        eyebrow="Workflow"
        title={`Disable ${disableTarget?.displayName ?? ''}?`}
        footer={
          <>
            <Button variant="ghost" onClick={() => setDisableTarget(null)} disabled={decisionBusy}>
              Cancel
            </Button>
            <Button variant="danger" onClick={confirmDisable} loading={decisionBusy} disabled={!disableReason.trim()}>
              Disable
            </Button>
          </>
        }
      >
        <p className="workflows-modal-lead">Nobody will be able to run this workflow until it's re-enabled.</p>
        <Field label="Reason" required>
          {(fieldProps) => (
            <Textarea
              {...fieldProps}
              mono={false}
              rows={3}
              placeholder="Why is this being disabled?"
              value={disableReason}
              onChange={(e) => setDisableReason(e.target.value)}
            />
          )}
        </Field>
      </Modal>

      <Modal
        open={cancelTarget != null}
        onClose={() => setCancelTarget(null)}
        eyebrow="Workflow run"
        title="Cancel this run?"
        footer={
          <>
            <Button variant="ghost" onClick={() => setCancelTarget(null)} disabled={decisionBusy}>
              Keep it running
            </Button>
            <Button variant="danger" onClick={confirmCancel} loading={decisionBusy}>
              Cancel run
            </Button>
          </>
        }
      >
        <p className="workflows-modal-lead">
          This stops <span className="ui-mono">{cancelTarget?.runId}</span> — any steps already completed keep
          their artifacts, but nothing further will run.
        </p>
      </Modal>
    </div>
  )
}

export default WorkflowsPage
