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
  Spinner,
  Textarea,
  useToast,
  type DropdownOption,
} from '../components/ui'
import { SampleToggle, WorkflowInputFields, WorkflowReadiness } from '../components/workflow/WorkflowInputFields'
import WorkflowAlertBadge from '../components/workflow/WorkflowAlertBadge'
import InfoHoverIcon from '../components/workflow/InfoHoverIcon'
import StepPickerModal from '../components/workflow/StepPickerModal'
import { useWorkflowInputs } from '../hooks/useWorkflowInputs'
import {
  ApiError,
  addWorkflowGraphVersion,
  createWorkflowGraph,
  getWorkflowGraph,
  getWorkflowGraphCatalog,
  listWorkflowGraphs,
  publishWorkflowGraph,
  runWorkflow,
  setWorkflowTemplateStatus,
  validateWorkflowGraph,
  type GraphEdge,
  type GraphNode,
  type GraphNodeKind,
  type WorkflowBinding,
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

type TriggerInput = { name: string; label: string }

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

interface NodeSource {
  nodeId: string
  path: string
  label: string
}

type BindingSource = 'literal' | 'trigger' | 'node'

function bindingSource(binding: WorkflowBinding | undefined): BindingSource {
  return binding?.source ?? 'literal'
}

interface BindingRowProps {
  argLabel: string
  required?: boolean
  binding: WorkflowBinding | undefined
  triggerInputs: TriggerInput[]
  nodeSources: NodeSource[]
  onChange: (binding: WorkflowBinding | null) => void
}

/** One step argument's value source — typed literally, taken from this
 *  workflow's own declared input, or taken from an earlier step's output.
 *  Mirrors `gateway/workflow_graph_interpreter.py`'s `_resolve_binding`
 *  exactly: those are the only three sources it understands. */
function BindingRow({ argLabel, required, binding, triggerInputs, nodeSources, onChange }: BindingRowProps) {
  const source = bindingSource(binding)
  const sourceOptions: DropdownOption[] = [
    { value: 'literal', label: 'Type a value' },
    ...(triggerInputs.length ? [{ value: 'trigger', label: "From this workflow's input" }] : []),
    ...(nodeSources.length ? [{ value: 'node', label: 'From an earlier step' }] : []),
  ]

  return (
    <div className="mw-binding-row">
      <span className="mw-binding-label">
        {argLabel}
        {required && <span className="mw-binding-req">*</span>}
      </span>
      <Dropdown
        value={source}
        onChange={(v) => {
          if (v === 'literal') onChange({ source: 'literal', value: '' })
          else if (v === 'trigger' && triggerInputs[0]) onChange({ source: 'trigger', path: triggerInputs[0].name })
          else if (v === 'node' && nodeSources[0]) onChange({ source: 'node', node_id: nodeSources[0].nodeId, path: nodeSources[0].path })
        }}
        options={sourceOptions}
      />
      {source === 'literal' && (
        <Input
          value={binding && binding.source === 'literal' ? String(binding.value ?? '') : ''}
          onChange={(e) => onChange({ source: 'literal', value: e.target.value })}
          placeholder="value"
        />
      )}
      {source === 'trigger' && (
        <Dropdown
          value={binding && binding.source === 'trigger' ? binding.path : ''}
          onChange={(path) => onChange({ source: 'trigger', path })}
          options={triggerInputs.map((t) => ({ value: t.name, label: t.label || t.name }))}
        />
      )}
      {source === 'node' && (
        <Dropdown
          value={binding && binding.source === 'node' ? `${binding.node_id}::${binding.path}` : ''}
          onChange={(v) => {
            const [nodeId, path] = v.split('::')
            onChange({ source: 'node', node_id: nodeId, path })
          }}
          options={nodeSources.map((s) => ({ value: `${s.nodeId}::${s.path}`, label: s.label }))}
        />
      )}
    </div>
  )
}

interface ToolStepFieldsProps {
  step: GraphNode
  tool: WorkflowGraphCatalogTool | undefined
  triggerInputs: TriggerInput[]
  nodeSources: NodeSource[]
  onBind: (arg: string, binding: WorkflowBinding | null) => void
}

function ToolStepFields({ step, tool, triggerInputs, nodeSources, onBind }: ToolStepFieldsProps) {
  const properties = tool?.parameters?.properties ?? {}
  const required = new Set(tool?.parameters?.required ?? [])
  const argNames = Object.keys(properties)
  if (!tool) return <p className="mw-step-empty">This tool is no longer available.</p>
  if (argNames.length === 0) return <p className="mw-step-empty">This tool takes no inputs.</p>
  return (
    <div className="mw-step-bindings">
      {argNames.map((arg) => (
        <BindingRow
          key={arg}
          argLabel={properties[arg]?.description || arg}
          required={required.has(arg)}
          binding={step.inputBindings[arg]}
          triggerInputs={triggerInputs}
          nodeSources={nodeSources}
          onChange={(b) => onBind(arg, b)}
        />
      ))}
    </div>
  )
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
  const [stepPickerOpen, setStepPickerOpen] = useState(false)

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
      } catch {
        toast.error('Could not load that workflow.')
      }
    },
    [resetToNew, toast],
  )

  const addStep = useCallback(
    (kind: string) => {
      if (!kind) return
      const step =
        kind === 'approval_gate' ? makeApprovalStep() : kind === 'llm_transform' ? makeAiStep() : catalogByTool[kind] ? makeToolStep(catalogByTool[kind]) : null
      if (!step) return
      setSteps((prev) => [...prev, step])
    },
    [catalogByTool],
  )

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

  const updateStepConfig = useCallback((nodeId: string, config: Record<string, unknown>) => {
    setSteps((prev) => prev.map((s) => (s.nodeId === nodeId ? { ...s, config } : s)))
  }, [])

  const setBinding = useCallback((nodeId: string, arg: string, binding: WorkflowBinding | null) => {
    setSteps((prev) =>
      prev.map((s) => {
        if (s.nodeId !== nodeId) return s
        const inputBindings = { ...s.inputBindings }
        if (binding) inputBindings[arg] = binding
        else delete inputBindings[arg]
        return { ...s, inputBindings }
      }),
    )
  }, [])

  const addTriggerInput = useCallback(() => setTriggerInputs((prev) => [...prev, { name: '', label: '' }]), [])
  const updateTriggerInput = useCallback((index: number, patch: Partial<TriggerInput>) => {
    setTriggerInputs((prev) => prev.map((t, i) => (i === index ? { ...t, ...patch } : t)))
  }, [])
  const removeTriggerInput = useCallback((index: number) => {
    setTriggerInputs((prev) => prev.filter((_, i) => i !== index))
  }, [])

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

  const save = useCallback(async () => {
    const { nodes, edges } = buildGraph()
    setSaving(true)
    try {
      const result = graphId
        ? await addWorkflowGraphVersion(graphId, { display_name: displayName, description, nodes, edges })
        : await createWorkflowGraph({ display_name: displayName, description, nodes, edges })
      setGraphId(result.graphId)
      setStatus(result.status)
      setPublishedVersion(result.publishedVersion)
      setValidation(null)
      toast.success(graphId ? 'New version saved' : 'Saved as a new draft')
      loadGraphs()
    } catch (cause) {
      toast.error(cause instanceof ApiError ? cause.message : 'Could not save that workflow.')
    } finally {
      setSaving(false)
    }
  }, [graphId, displayName, description, buildGraph, loadGraphs, toast])

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
    if (!graphId) {
      toast.warn('Save the workflow first')
      return
    }
    setPublishing(true)
    try {
      const result = await publishWorkflowGraph(graphId)
      setStatus(result.status)
      setPublishedVersion(result.publishedVersion)
      toast.success('Workflow published')
      loadGraphs()
    } catch (cause) {
      toast.error(cause instanceof ApiError ? cause.message : 'Could not publish that workflow.')
    } finally {
      setPublishing(false)
    }
  }, [graphId, loadGraphs, toast])

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

  const stepKindTitle = (kind: GraphNodeKind, tool: string) =>
    kind === 'approval_gate' ? 'Approval gate' : kind === 'llm_transform' ? 'AI step' : tool

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
            <Button size="sm" variant="ghost" onClick={resetToNew}>
              New workflow
            </Button>
            <Button size="sm" variant="ghost" onClick={validate} loading={validating} disabled={!graphId}>
              Check
            </Button>
            <Button size="sm" variant="ghost" onClick={save} loading={saving}>
              {graphId ? 'Save new version' : 'Save as draft'}
            </Button>
            <Button size="sm" onClick={publish} loading={publishing} disabled={!graphId}>
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
        description="Runs top to bottom. Add a step, fill in a small form, reorder with the arrows."
        actions={<AddButton onClick={() => setStepPickerOpen(true)} label="Add a step" size="sm" />}
      >
        {steps.length === 0 ? (
          <EmptyState
            title="No steps yet"
            description="Add one to get started."
            action={
              <Button size="sm" variant="ghost" onClick={() => setStepPickerOpen(true)}>
                + Add a step
              </Button>
            }
          />
        ) : (
          <div className="mw-steps">
            {steps.map((step, i) => {
              const tool = step.kind === 'tool_call' ? catalogByTool[step.tool] : undefined
              const needsGate = tool?.riskLevel === 'send' && !steps.slice(0, i).some((s) => s.kind === 'approval_gate')
              const sources = sourcesBefore(i)
              return (
                <div key={step.nodeId} className="mw-step" data-needs-gate={needsGate || undefined}>
                  <div className="mw-step-head">
                    <span className="mw-step-index">{i + 2}</span>
                    <span className="mw-step-title">{stepKindTitle(step.kind, tool?.canonical ?? step.tool)}</span>
                    <div className="mw-step-move">
                      <button type="button" className="mw-step-btn" disabled={i === 0} onClick={() => moveStep(step.nodeId, -1)} aria-label="Move up">
                        ↑
                      </button>
                      <button
                        type="button"
                        className="mw-step-btn"
                        disabled={i === steps.length - 1}
                        onClick={() => moveStep(step.nodeId, 1)}
                        aria-label="Move down"
                      >
                        ↓
                      </button>
                      <button type="button" className="mw-step-btn mw-step-btn-danger" onClick={() => removeStep(step.nodeId)} aria-label="Remove step">
                        ✕
                      </button>
                    </div>
                  </div>

                  {needsGate && (
                    <div className="mw-step-warning">
                      <span>This step sends something externally — add an approval gate before it.</span>
                      <Button size="sm" variant="ghost" onClick={() => insertApprovalBefore(step.nodeId)}>
                        + Insert approval before
                      </Button>
                    </div>
                  )}

                  {step.kind === 'tool_call' && (
                    <ToolStepFields step={step} tool={tool} triggerInputs={triggerInputs} nodeSources={sources} onBind={(arg, b) => setBinding(step.nodeId, arg, b)} />
                  )}

                  {step.kind === 'approval_gate' && (
                    <div className="mw-step-fields">
                      <Field label="Reason">
                        {(fp) => (
                          <Textarea
                            {...fp}
                            mono={false}
                            rows={2}
                            value={String(step.config.reason ?? '')}
                            onChange={(e) => updateStepConfig(step.nodeId, { ...step.config, reason: e.target.value })}
                          />
                        )}
                      </Field>
                      <Field label="Risk level">
                        {(fp) => (
                          <Dropdown
                            {...fp}
                            value={String(step.config.risk_level ?? 'medium')}
                            onChange={(v) => updateStepConfig(step.nodeId, { ...step.config, risk_level: v })}
                            options={[
                              { value: 'medium', label: 'Medium' },
                              { value: 'high', label: 'High' },
                            ]}
                          />
                        )}
                      </Field>
                    </div>
                  )}

                  {step.kind === 'llm_transform' && (
                    <div className="mw-step-fields">
                      <Field label="AI action">
                        {(fp) => (
                          <Dropdown
                            {...fp}
                            value={String(step.config.kind ?? 'summarize')}
                            onChange={(v) => updateStepConfig(step.nodeId, { ...step.config, kind: v })}
                            options={[
                              { value: 'summarize', label: 'Summarize' },
                              { value: 'draft_reply', label: 'Draft a reply' },
                              { value: 'classify', label: 'Classify' },
                              { value: 'extract', label: 'Extract' },
                            ]}
                          />
                        )}
                      </Field>
                      <Field label="Instruction">
                        {(fp) => (
                          <Textarea
                            {...fp}
                            mono={false}
                            rows={2}
                            value={String(step.config.instruction ?? '')}
                            onChange={(e) => updateStepConfig(step.nodeId, { ...step.config, instruction: e.target.value })}
                          />
                        )}
                      </Field>
                      <BindingRow
                        argLabel="Text to work from"
                        binding={step.inputBindings.input_text}
                        triggerInputs={triggerInputs}
                        nodeSources={sources}
                        onChange={(b) => setBinding(step.nodeId, 'input_text', b)}
                      />
                    </div>
                  )}
                </div>
              )
            })}
          </div>
        )}
      </Card>

      <StepPickerModal
        open={stepPickerOpen}
        onClose={() => setStepPickerOpen(false)}
        catalog={catalog}
        onPick={addStep}
      />

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
