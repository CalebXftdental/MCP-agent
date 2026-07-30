import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  AddButton,
  Badge,
  Button,
  Card,
  Dropdown,
  EmptyState,
  Field,
  Input,
  Modal,
  Spinner,
  useToast,
  type DropdownOption,
} from '../components/ui'
import { SampleToggle, WorkflowInputFields, WorkflowReadiness } from '../components/workflow/WorkflowInputFields'
import WorkflowAlertBadge from '../components/workflow/WorkflowAlertBadge'
import InfoHoverIcon from '../components/workflow/InfoHoverIcon'
import StepModal from '../components/workflow/StepModal'
import StepFlow from '../components/workflow/StepFlow'
import type { NodeSource, TriggerInput } from '../components/workflow/StepConfigFields'
import { useWorkflowInputs } from '../hooks/useWorkflowInputs'
import {
  ApiError,
  addWorkflowGraphVersion,
  createWorkflowGraph,
  deleteWorkflowGraph,
  getWorkflowGraph,
  getWorkflowGraphCatalog,
  listWorkflowGraphs,
  publishWorkflowGraph,
  runWorkflow,
  setWorkflowTemplateStatus,
  validateWorkflowGraph,
  type GraphEdge,
  type GraphNode,
  type WorkflowGraph,
  type WorkflowGraphCatalogTool,
  type WorkflowGraphValidation,
  type WorkflowRunResult,
} from '../lib/api'
import type { PageProps } from './types'
import './MyWorkflowsPage.css'

/**
 * My Workflow — replaces `renderMyWorkflows()`'s free 2D drag/dot-connect
 * canvas with a linear step list: trigger at the top, then an ordered stack
 * of step cards, each configured inline.
 *
 * The canvas gave a user-buildable workflow arbitrary graph shape (branches,
 * fan-out, a step's input wired from any node by dragging a line). That's a
 * dataflow-graph mental model — genuinely powerful, but a specialized skill
 * most non-technical builders don't have. Every real workflow this feature
 * is used for in practice is a straight sequence ("pull data → summarize →
 * draft → approve → send"), so this trades branching for "add a step, fill
 * in a small form, reorder with two arrows" — Zapier's shape, not
 * Node-RED's. The backend's graph model is untouched: a linear chain is
 * simply the degenerate case where each step's edge is exactly "the step
 * before it," generated here rather than hand-wired.
 */

function newStepId(): string {
  return `step_${Math.random().toString(36).slice(2, 9)}`
}

function makeToolStep(tool: WorkflowGraphCatalogTool): GraphNode {
  return { nodeId: newStepId(), kind: 'tool_call', title: tool.canonical, tool: tool.canonical, config: {}, inputBindings: {} }
}

function makeApprovalStep(): GraphNode {
  return {
    nodeId: newStepId(),
    kind: 'approval_gate',
    title: 'Approval gate',
    tool: '',
    config: { reason: '', risk_level: 'medium' },
    inputBindings: {},
  }
}

function makeAiStep(): GraphNode {
  return {
    nodeId: newStepId(),
    kind: 'llm_transform',
    title: 'AI step',
    tool: '',
    config: { kind: 'summarize', instruction: '' },
    inputBindings: {},
  }
}

/** Every step's edge is just "the step before it" — sufficient for a linear
 *  chain (see module note above): it satisfies the backend's single-trigger,
 *  no-cycle, everything-reachable checks trivially, and a step's own input
 *  binding (which can point further back than its immediate predecessor)
 *  doesn't need its own edge for the topological sort to still place it
 *  correctly. */
function chainEdges(nodes: GraphNode[]): GraphEdge[] {
  const edges: GraphEdge[] = []
  for (let i = 1; i < nodes.length; i++) {
    edges.push({ edgeId: `e_${nodes[i - 1].nodeId}_${nodes[i].nodeId}`, sourceNodeId: nodes[i - 1].nodeId, targetNodeId: nodes[i].nodeId })
  }
  return edges
}

/** Orders a loaded graph's nodes by dependency (Kahn's algorithm), so a
 *  workflow saved by this same linear builder round-trips in the order it
 *  was authored. Falls back to the stored array order on a cycle — shouldn't
 *  happen for anything that passed `validate_graph`, but a saved order beats
 *  a blank page either way. */
function topoOrder(nodes: GraphNode[], edges: GraphEdge[]): GraphNode[] {
  const byId = new Map(nodes.map((n) => [n.nodeId, n]))
  const inDeg = new Map(nodes.map((n) => [n.nodeId, 0]))
  const out = new Map<string, string[]>(nodes.map((n) => [n.nodeId, []]))
  for (const e of edges) {
    if (!byId.has(e.sourceNodeId) || !byId.has(e.targetNodeId)) continue
    out.get(e.sourceNodeId)!.push(e.targetNodeId)
    inDeg.set(e.targetNodeId, (inDeg.get(e.targetNodeId) ?? 0) + 1)
  }
  const remaining = new Map(inDeg)
  const queue = nodes.filter((n) => (inDeg.get(n.nodeId) ?? 0) === 0).map((n) => n.nodeId)
  const order: string[] = []
  while (queue.length) {
    const id = queue.shift()!
    order.push(id)
    for (const t of out.get(id) ?? []) {
      remaining.set(t, (remaining.get(t) ?? 0) - 1)
      if (remaining.get(t) === 0) queue.push(t)
    }
  }
  return order.length === nodes.length ? order.map((id) => byId.get(id)!) : nodes
}

function MyWorkflowsPage({ session }: PageProps) {
  const toast = useToast()

  const [graphs, setGraphs] = useState<WorkflowGraph[]>([])
  const [graphsLoading, setGraphsLoading] = useState(true)
  const [catalog, setCatalog] = useState<WorkflowGraphCatalogTool[]>([])

  const [graphId, setGraphId] = useState<string | null>(null)
  const [displayName, setDisplayName] = useState('Untitled workflow')
  const [description, setDescription] = useState('')
  const [status, setStatus] = useState('draft')
  const [publishedVersion, setPublishedVersion] = useState(0)

  const [triggerInputs, setTriggerInputs] = useState<TriggerInput[]>([])
  const [steps, setSteps] = useState<GraphNode[]>([])
  const [stepModalOpen, setStepModalOpen] = useState(false)
  /** Node id being edited, or null when the modal is adding a new step. */
  const [editingStepId, setEditingStepId] = useState<string | null>(null)
  const [deleteConfirmOpen, setDeleteConfirmOpen] = useState(false)
  const [deletingGraph, setDeletingGraph] = useState(false)

  const [saving, setSaving] = useState(false)
  const [validating, setValidating] = useState(false)
  const [publishing, setPublishing] = useState(false)
  const [validation, setValidation] = useState<WorkflowGraphValidation | null>(null)

  const [runPhase, setRunPhase] = useState<'idle' | 'running' | 'done' | 'error'>('idle')
  const [runResult, setRunResult] = useState<WorkflowRunResult | null>(null)
  const [runError, setRunError] = useState<string | null>(null)
  const runInputs = useWorkflowInputs(status === 'active' && graphId ? graphId : null)
  // Same "title only, nothing else" first-check treatment WorkflowsPage uses.
  const runFirstCheckPending = runInputs.checking && !runInputs.preflight && !runInputs.error

  const catalogByTool = useMemo(() => Object.fromEntries(catalog.map((t) => [t.canonical, t])), [catalog])

  const loadGraphs = useCallback(() => {
    setGraphsLoading(true)
    listWorkflowGraphs()
      .then((r) => {
        setGraphs(r.graphs)
        setGraphsLoading(false)
      })
      .catch(() => setGraphsLoading(false))
  }, [])

  useEffect(loadGraphs, [loadGraphs])
  useEffect(() => {
    getWorkflowGraphCatalog()
      .then((r) => setCatalog(r.tools))
      .catch(() => setCatalog([]))
  }, [])

  const resetToNew = useCallback(() => {
    setGraphId(null)
    setDisplayName('Untitled workflow')
    setDescription('')
    setStatus('draft')
    setPublishedVersion(0)
    setTriggerInputs([])
    setSteps([])
    setValidation(null)
    setRunPhase('idle')
    setRunResult(null)
    setDeleteConfirmOpen(false)
  }, [])

  const openGraph = useCallback(
    async (id: string) => {
      if (!id) {
        resetToNew()
        return
      }
      try {
        const g = await getWorkflowGraph(id)
        const allNodes = g.nodes ?? []
        const trigger = allNodes.find((n) => n.kind === 'trigger')
        const rest = topoOrder(allNodes, g.edges ?? []).filter((n) => n.kind !== 'trigger')
        setGraphId(g.graphId)
        setDisplayName(g.displayName)
        setDescription(g.description)
        setStatus(g.status)
        setPublishedVersion(g.publishedVersion)
        setTriggerInputs((trigger?.config.inputs as TriggerInput[] | undefined) ?? [])
        setSteps(rest)
        setValidation(null)
        setRunPhase('idle')
        setRunResult(null)
        setDeleteConfirmOpen(false)
      } catch {
        toast.error('Could not load that workflow.')
      }
    },
    [resetToNew, toast],
  )

  /** Blank step of the chosen kind, for `StepModal`'s picker phase. Node-id
   *  generation stays here so the modal never invents ids of its own. */
  const makeStep = useCallback(
    (kind: string): GraphNode | null => {
      if (kind === 'approval_gate') return makeApprovalStep()
      if (kind === 'llm_transform') return makeAiStep()
      return catalogByTool[kind] ? makeToolStep(catalogByTool[kind]) : null
    },
    [catalogByTool],
  )

  const openAddStep = useCallback(() => {
    setEditingStepId(null)
    setStepModalOpen(true)
  }, [])

  const openEditStep = useCallback((nodeId: string) => {
    setEditingStepId(nodeId)
    setStepModalOpen(true)
  }, [])

  /** Upsert by node id: an edited step replaces itself in place (keeping its
   *  position in the chain), a brand-new one appends. */
  const commitStep = useCallback((step: GraphNode) => {
    setSteps((prev) => (prev.some((s) => s.nodeId === step.nodeId) ? prev.map((s) => (s.nodeId === step.nodeId ? step : s)) : [...prev, step]))
  }, [])

  const removeStep = useCallback((nodeId: string) => {
    setSteps((prev) => prev.filter((s) => s.nodeId !== nodeId))
  }, [])

  const moveStep = useCallback((nodeId: string, dir: -1 | 1) => {
    setSteps((prev) => {
      const i = prev.findIndex((s) => s.nodeId === nodeId)
      const j = i + dir
      if (i < 0 || j < 0 || j >= prev.length) return prev
      const next = [...prev]
      ;[next[i], next[j]] = [next[j], next[i]]
      return next
    })
  }, [])

  const insertApprovalBefore = useCallback((nodeId: string) => {
    setSteps((prev) => {
      const i = prev.findIndex((s) => s.nodeId === nodeId)
      if (i < 0) return prev
      const next = [...prev]
      next.splice(i, 0, makeApprovalStep())
      return next
    })
  }, [])

  const addTriggerInput = useCallback(() => setTriggerInputs((prev) => [...prev, { name: '', label: '' }]), [])
  const updateTriggerInput = useCallback((index: number, patch: Partial<TriggerInput>) => {
    setTriggerInputs((prev) => prev.map((t, i) => (i === index ? { ...t, ...patch } : t)))
  }, [])
  const removeTriggerInput = useCallback((index: number) => {
    setTriggerInputs((prev) => prev.filter((_, i) => i !== index))
  }, [])

  /** Outputs of every step before position `index` — the only ones a step at
   *  that position may bind to, since the interpreter walks the chain in
   *  order. For a not-yet-added step, `index` is `steps.length` (everything
   *  is "before" it). */
  const sourcesBefore = useCallback(
    (index: number): NodeSource[] =>
      steps.slice(0, index).flatMap((s) => {
        if (s.kind === 'tool_call') {
          const meta = catalogByTool[s.tool]
          return (meta?.outputFields ?? []).map((f) => ({ nodeId: s.nodeId, path: f, label: `${s.title || s.tool} → ${f}` }))
        }
        if (s.kind === 'llm_transform') return [{ nodeId: s.nodeId, path: 'text', label: `${s.title || 'AI step'} → text` }]
        return []
      }),
    [steps, catalogByTool],
  )

  const editingStep = useMemo(() => steps.find((s) => s.nodeId === editingStepId) ?? null, [steps, editingStepId])
  const modalNodeSources = useMemo(() => {
    const index = editingStepId ? steps.findIndex((s) => s.nodeId === editingStepId) : steps.length
    return sourcesBefore(index < 0 ? steps.length : index)
  }, [editingStepId, steps, sourcesBefore])

  const buildGraph = useCallback((): { nodes: GraphNode[]; edges: GraphEdge[] } => {
    const trigger: GraphNode = {
      nodeId: 'trigger',
      kind: 'trigger',
      title: 'Trigger',
      tool: '',
      config: { inputs: triggerInputs.filter((t) => t.name.trim()) },
      inputBindings: {},
    }
    const nodes = [trigger, ...steps]
    return { nodes, edges: chainEdges(nodes) }
  }, [triggerInputs, steps])

  /** Persists whatever is CURRENTLY on screen (steps, trigger inputs, name,
   *  description) as a new version — the one place both `save` and `publish`
   *  actually write to the backend, so neither can drift from what the
   *  editor shows. `publish` used to call `publishWorkflowGraph(graphId)`
   *  directly, which republishes whatever version was last explicitly
   *  saved — if you removed a step and hit Publish without an explicit save
   *  first, the removal was silently discarded and the OLD version (step
   *  still in it) got published; reopening the workflow then made the
   *  "deleted" step look like it had come back. Routing publish through
   *  this same persist step closes that gap. */
  const persist = useCallback(async (): Promise<WorkflowGraph | null> => {
    const { nodes, edges } = buildGraph()
    try {
      const result = graphId
        ? await addWorkflowGraphVersion(graphId, { display_name: displayName, description, nodes, edges })
        : await createWorkflowGraph({ display_name: displayName, description, nodes, edges })
      setGraphId(result.graphId)
      setStatus(result.status)
      setPublishedVersion(result.publishedVersion)
      setValidation(null)
      return result
    } catch (cause) {
      toast.error(cause instanceof ApiError ? cause.message : 'Could not save that workflow.')
      return null
    }
  }, [graphId, displayName, description, buildGraph, toast])

  const save = useCallback(async () => {
    setSaving(true)
    const wasNew = !graphId
    const result = await persist()
    setSaving(false)
    if (result) {
      toast.success(wasNew ? 'Saved as a new draft' : 'New version saved')
      loadGraphs()
    }
  }, [graphId, persist, loadGraphs, toast])

  const validate = useCallback(async () => {
    if (!graphId) {
      toast.warn('Save the workflow first')
      return
    }
    const { nodes, edges } = buildGraph()
    setValidating(true)
    try {
      setValidation(await validateWorkflowGraph(graphId, nodes, edges))
    } catch (cause) {
      toast.error(cause instanceof ApiError ? cause.message : 'Could not check this workflow.')
    } finally {
      setValidating(false)
    }
  }, [graphId, buildGraph, toast])

  const publish = useCallback(async () => {
    setPublishing(true)
    try {
      const saved = await persist()
      if (!saved) return
      const result = await publishWorkflowGraph(saved.graphId)
      setStatus(result.status)
      setPublishedVersion(result.publishedVersion)
      toast.success('Workflow published')
      loadGraphs()
    } catch (cause) {
      toast.error(cause instanceof ApiError ? cause.message : 'Could not publish that workflow.')
    } finally {
      setPublishing(false)
    }
  }, [persist, loadGraphs, toast])

  const toggleStatus = useCallback(async () => {
    if (!graphId) return
    const action = status === 'active' ? 'disable' : 'enable'
    try {
      await setWorkflowTemplateStatus(graphId, action)
      setStatus(action === 'disable' ? 'disabled' : 'active')
      toast.success(action === 'disable' ? 'Workflow disabled' : 'Workflow enabled')
    } catch (cause) {
      toast.error(cause instanceof ApiError ? cause.message : 'Could not update that workflow.')
    }
  }, [graphId, status, toast])

  const confirmDeleteGraph = useCallback(async () => {
    if (!graphId) return
    setDeletingGraph(true)
    try {
      await deleteWorkflowGraph(graphId)
      toast.success('Workflow deleted')
      setDeleteConfirmOpen(false)
      resetToNew()
      loadGraphs()
    } catch (cause) {
      toast.error(cause instanceof ApiError ? cause.message : 'Could not delete that workflow.')
    } finally {
      setDeletingGraph(false)
    }
  }, [graphId, resetToNew, loadGraphs, toast])

  const run = useCallback(async () => {
    if (!graphId) return
    setRunPhase('running')
    setRunError(null)
    try {
      const result = await runWorkflow(graphId, runInputs.values)
      setRunResult(result)
      setRunPhase('done')
    } catch (cause) {
      setRunError(cause instanceof ApiError ? cause.message : 'Could not run that workflow.')
      setRunPhase('error')
    }
  }, [graphId, runInputs.values])

  const graphOptions = useMemo<DropdownOption[]>(
    () => graphs.map((g) => ({ value: g.graphId, label: `${g.displayName} (${g.status})` })),
    [graphs],
  )

  return (
    <div className="my-workflows">
      <Card
        title={
          <span className="workflows-run-title">
            My Workflow
            <InfoHoverIcon
              label="About My Workflow"
              text="Build a custom automation by connecting governed tools, approval gates, and AI steps — no code required."
            />
            <Badge tone={status === 'active' ? 'ok' : status === 'disabled' ? 'neutral' : 'warn'} subtle>
              {status}
            </Badge>
            {validation &&
              (validation.ready ? (
                <Badge tone="ok" dot>
                  Looks good
                </Badge>
              ) : (
                <WorkflowAlertBadge blockers={validation.blockers} />
              ))}
          </span>
        }
        actions={
          <div className="mw-header-actions">
            {session.isAdmin && graphId && (status === 'active' || status === 'disabled') && (
              <Button size="sm" variant="ghost" onClick={toggleStatus}>
                {status === 'active' ? 'Disable' : 'Enable'}
              </Button>
            )}
            {graphId && (
              <Button size="sm" variant="danger" onClick={() => setDeleteConfirmOpen(true)}>
                Delete
              </Button>
            )}
            <Button size="sm" variant="ghost" onClick={resetToNew}>
              New workflow
            </Button>
            <Button size="sm" variant="ghost" onClick={validate} loading={validating} disabled={!graphId}>
              Check
            </Button>
            <Button size="sm" variant="ghost" onClick={save} loading={saving}>
              Save
            </Button>
            <Button size="sm" onClick={publish} loading={publishing}>
              Publish
            </Button>
          </div>
        }
      >
        <div className="mw-identity-row">
          <div className="mw-identity-cell">
            <Field label="Open an existing workflow">
              {(fp) => (
                <Dropdown
                  {...fp}
                  value={graphId ?? ''}
                  onChange={openGraph}
                  placeholder={graphsLoading ? 'Loading…' : '-- new workflow --'}
                  options={graphOptions}
                />
              )}
            </Field>
          </div>
          <div className="mw-identity-cell">
            <Field label="Name">{(fp) => <Input {...fp} value={displayName} onChange={(e) => setDisplayName(e.target.value)} />}</Field>
          </div>
          <div className="mw-identity-cell">
            <Field label="Description" hint="Optional">
              {(fp) => <Input {...fp} value={description} onChange={(e) => setDescription(e.target.value)} />}
            </Field>
          </div>
        </div>

        {publishedVersion > 0 && <p className="mw-version-note">published v{publishedVersion}</p>}
      </Card>

      <Card
        title="What this workflow needs"
        description="Named inputs someone fills in before running this workflow — shown as an ordinary form, the same as any other workflow."
      >
        <div className="mw-trigger-inputs">
          {triggerInputs.map((t, i) => (
            <div className="mw-trigger-input-row" key={i}>
              <Input placeholder="key, e.g. customer_id" value={t.name} onChange={(e) => updateTriggerInput(i, { name: e.target.value })} />
              <Input placeholder="Label shown to whoever runs it" value={t.label} onChange={(e) => updateTriggerInput(i, { label: e.target.value })} />
              <Button size="sm" variant="ghost" onClick={() => removeTriggerInput(i)} aria-label="Remove input">
                ✕
              </Button>
            </div>
          ))}
          <Button size="sm" variant="ghost" onClick={addTriggerInput}>
            + Add an input
          </Button>
        </div>
      </Card>

      <Card
        title="Steps"
        description="Runs top to bottom. Hover a step to edit, reorder, or remove it."
        actions={<AddButton onClick={openAddStep} label="Add a step" size="sm" />}
      >
        {steps.length === 0 ? (
          <EmptyState
            title="No steps yet"
            description="Add one to get started."
            action={
              <Button size="sm" variant="ghost" onClick={openAddStep}>
                + Add a step
              </Button>
            }
          />
        ) : (
          <StepFlow
            steps={steps}
            catalogByTool={catalogByTool}
            triggerInputLabels={triggerInputs.filter((t) => t.name.trim()).map((t) => t.label || t.name)}
            onEdit={openEditStep}
            onMove={moveStep}
            onRemove={removeStep}
            onInsertApprovalBefore={insertApprovalBefore}
          />
        )}
      </Card>

      <StepModal
        open={stepModalOpen}
        onClose={() => setStepModalOpen(false)}
        catalog={catalog}
        triggerInputs={triggerInputs}
        nodeSources={modalNodeSources}
        editing={editingStep}
        makeStep={makeStep}
        onCommit={commitStep}
      />

      <Modal
        open={deleteConfirmOpen}
        onClose={() => setDeleteConfirmOpen(false)}
        eyebrow="My Workflow"
        title={`Delete ${displayName}?`}
        footer={
          <>
            <Button variant="ghost" onClick={() => setDeleteConfirmOpen(false)} disabled={deletingGraph}>
              Cancel
            </Button>
            <Button variant="danger" onClick={confirmDeleteGraph} loading={deletingGraph}>
              Delete
            </Button>
          </>
        }
      >
        <p className="mw-delete-modal-lead">
          This can't be undone.
          {status === 'active'
            ? ' Any automation scheduled against it will start failing, and it disappears from the Workflows catalog immediately.'
            : ' Every saved version goes with it.'}
        </p>
      </Modal>

      {status === 'active' && graphId && (
        <Card
          title={
            <span className="workflows-run-title">
              Run this workflow
              {runInputs.preflight && !runInputs.preflight.ready && <WorkflowAlertBadge blockers={runInputs.preflight.blockers} />}
            </span>
          }
          description={
            runFirstCheckPending ? undefined : "Runs alongside every other workflow — you'll see it in the Workflows tab's Recent runs too."
          }
          actions={!runFirstCheckPending ? <SampleToggle templateId={graphId} values={runInputs.values} setValue={runInputs.setValue} /> : undefined}
        >
          {runFirstCheckPending ? (
            <Spinner center label="Checking what this workflow needs…" />
          ) : (
            <div className="workflows-run-form">
              <WorkflowInputFields
                templateId={graphId}
                requiredInputs={runInputs.preflight?.requiredInputs ?? []}
                approvalGates={runInputs.preflight?.approvalGates ?? []}
                values={runInputs.values}
                setValue={runInputs.setValue}
                actions={
                  <Button onClick={run} loading={runPhase === 'running'} disabled={!!runInputs.preflight && !runInputs.preflight.ready}>
                    Run workflow
                  </Button>
                }
              />
              <WorkflowReadiness preflight={runInputs.preflight} checking={runInputs.checking} error={runInputs.error} />
              {runPhase === 'error' && <p className="workflows-run-error">⚠ {runError}</p>}
              {runPhase === 'done' && runResult && (
                <div className="workflows-run-result">
                  <div className="workflows-run-result-head">
                    <Badge tone={runResult.status === 'completed' ? 'ok' : 'warn'} dot>
                      {runResult.status.replace(/_/g, ' ')}
                    </Badge>
                    <span className="ui-mono workflows-run-result-id">{runResult.runId}</span>
                  </div>
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
      )}
    </div>
  )
}

export default MyWorkflowsPage
