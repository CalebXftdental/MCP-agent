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
  PageShell,
  SegmentedControl,
  Spinner,
  Textarea,
  useToast,
  type DropdownOption,
} from '../components/ui'
import { SampleToggle, WorkflowInputFields, WorkflowReadiness } from '../components/workflow/WorkflowInputFields'
import WorkflowAlertBadge from '../components/workflow/WorkflowAlertBadge'
import InfoHoverIcon from '../components/workflow/InfoHoverIcon'
import StepModal from '../components/workflow/StepModal'
import StepFlow from '../components/workflow/StepFlow'
import WorkflowCanvas from '../components/workflow/WorkflowCanvas'
import WorkflowCanvasLegend from '../components/workflow/WorkflowCanvasLegend'
import { autoArrange, nextNodePosition, withLayout, type Point } from '../components/workflow/graphGeometry'
import {
  TRIGGER_NODE_ID,
  derivedEdges,
  nodeLabel,
  outputPinsFor,
  renameTriggerInputEverywhere,
  slugifyTriggerInputName,
  uniqueTriggerInputName,
  wouldCreateCycle,
} from '../components/workflow/graphModel'
import type { NodeSource, TriggerInput } from '../components/workflow/StepConfigFields'
// The copilot panel below reuses the SAME chat building blocks Home's
// assistant uses (ChatLog/Composer, useChat/useComposer) — same streaming,
// retry, and feedback behavior, just pointed at the "My Workflow" copilot's
// own session/system-prompt via useChat's `kind: 'workflow'` (see useChat.ts,
// orchestrator.py's WORKFLOW_COPILOT_SYSTEM_PROMPT) instead of building a
// second, parallel chat UI from scratch.
import ChatLog from '../components/chat/ChatLog'
import Composer from '../components/chat/Composer'
import { useChat } from '../hooks/useChat'
import { useComposer } from '../hooks/useComposer'
import { useStoredList } from '../hooks/useStoredList'
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
  requestWorkflow,
  runWorkflow,
  setWorkflowTemplateStatus,
  validateWorkflowGraph,
  type GraphEdge,
  type GraphNode,
  type WorkflowBinding,
  type WorkflowGraph,
  type WorkflowGraphCatalogTool,
  type WorkflowGraphValidation,
  type WorkflowRunResult,
} from '../lib/api'
import type { PageProps } from './types'
import './MyWorkflowsPage.css'

/**
 * My Workflow — two views over the same node list, switched with a toggle:
 *
 * - **Step**: trigger at the top, then an ordered stack of step cards
 *   reordered with two arrows — Zapier's shape. A step's edge is always "the
 *   step right before it" (`chainEdges`), generated here rather than
 *   hand-wired, trading branching (a specialized skill most non-technical
 *   builders don't have) for "add a step, fill in a small form."
 * - **Canvas**: step cards dragged anywhere and wired by drawing a curve from
 *   one card's output dot to another's input dot — Node-RED's shape, ported
 *   from the legacy free-positioned builder. Edges are DERIVED from every
 *   node's `inputBindings` (`derivedEdges`), never hand-maintained, so a line
 *   on screen and what the interpreter executes can't disagree.
 *
 * Both views edit the exact same `steps`/`triggerInputs` state and the exact
 * same backend graph model (`GraphNode.inputBindings` is the one source of
 * truth for what feeds what on either view — `edges` is just a derived
 * execution order, resolved independently by each view's own strategy).
 * Switching the toggle never touches saved data; only the next
 * Save/Check/Publish writes edges in the active view's shape.
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

function makeFilterStep(): GraphNode {
  return {
    nodeId: newStepId(),
    kind: 'filter',
    title: 'Filter',
    tool: '',
    config: { conditions: { all: [] } },
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

function MyWorkflowsPage({ session, navigate }: PageProps) {
  const toast = useToast()

  // The copilot's own conversation — kind: 'workflow' keeps it a completely
  // separate session/history bucket and system prompt from Home's assistant
  // (useChat.ts, orchestrator.py's WORKFLOW_COPILOT_SYSTEM_PROMPT), even
  // though it's the exact same hook/components.
  const copilotChat = useChat(session.name, 'workflow')
  const copilotAsks = useStoredList<string>('gov_quickasks_workflow', session.name)
  const copilotComposer = useComposer({ chat: copilotChat, toast, savedAsks: copilotAsks })

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
  /** The trigger card's own canvas position. Kept apart from `steps` because
   *  the trigger isn't a step — it's synthesized into the saved graph by
   *  `triggerNode` below — but it still has to be draggable in Canvas view. */
  const [triggerPos, setTriggerPos] = useState<Point>({ x: 40, y: 36 })
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null)
  /** Which of the two views is active — a display preference, not part of
   *  the saved graph, so it's persisted in localStorage rather than round
   *  -tripped through the backend (same pattern as `useCollapsed`'s sidebar
   *  state). Defaults to Step so nobody's workflow changes look on upgrade. */
  const [viewMode, setViewMode] = useState<'step' | 'canvas'>(() => {
    try {
      return localStorage.getItem('mw:viewMode') === 'canvas' ? 'canvas' : 'step'
    } catch {
      return 'step'
    }
  })
  const changeViewMode = useCallback((mode: 'step' | 'canvas') => {
    setViewMode(mode)
    try {
      localStorage.setItem('mw:viewMode', mode)
    } catch {
      // Private-mode quota failure: the in-memory toggle still works for this tab.
    }
  }, [])
  const [stepModalOpen, setStepModalOpen] = useState(false)
  /** Node id being edited, or null when the modal is adding a new step. */
  const [editingStepId, setEditingStepId] = useState<string | null>(null)
  const [deleteConfirmOpen, setDeleteConfirmOpen] = useState(false)
  const [deletingGraph, setDeletingGraph] = useState(false)

  const [saving, setSaving] = useState(false)
  const [validating, setValidating] = useState(false)
  const [publishing, setPublishing] = useState(false)
  const [validation, setValidation] = useState<WorkflowGraphValidation | null>(null)

  // "Can't build this yet" — a way to leave a request without needing to model
  // it as steps at all. Lands in the SAME queue as the assistant's own
  // submit_workflow_request tool (see RequestsPage's "Workflow requests"
  // section) — same store, same admin review, regardless of which door it
  // came through.
  const [requestText, setRequestText] = useState('')
  const [submittingRequest, setSubmittingRequest] = useState(false)

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

  // The copilot's propose_graph tool (app.py) writes a draft straight into the
  // same store this dropdown reads from — refetch whenever it finishes a turn
  // so a freshly-proposed draft shows up without the user hunting for a
  // refresh button. Harmless to over-fire (e.g. on mount, or a turn that
  // proposed nothing) — listWorkflowGraphs() is a cheap idempotent read.
  useEffect(() => {
    if (!copilotChat.busy) loadGraphs()
  }, [copilotChat.busy, loadGraphs])

  const resetToNew = useCallback(() => {
    setGraphId(null)
    setDisplayName('Untitled workflow')
    setDescription('')
    setStatus('draft')
    setPublishedVersion(0)
    setTriggerInputs([])
    setSteps([])
    setTriggerPos({ x: 40, y: 36 })
    setSelectedNodeId(null)
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
        // withLayout fills in positions for a graph saved before this page had
        // a Canvas view (Step-only saves never wrote one) — without it every
        // card would land at 0,0 in one unreadable pile if Canvas is opened.
        const loadedNodes = withLayout(g.nodes ?? [])
        const trigger = loadedNodes.find((n) => n.kind === 'trigger')
        const rest = topoOrder(loadedNodes, g.edges ?? []).filter((n) => n.kind !== 'trigger')
        setGraphId(g.graphId)
        setDisplayName(g.displayName)
        setDescription(g.description)
        setStatus(g.status)
        setPublishedVersion(g.publishedVersion)
        // A hand-built graph always writes {name, label} pairs (addTriggerInput
        // below), but a graph authored another way (e.g. the copilot's
        // propose_graph, which was never told this exact shape) can omit or
        // mistype either field -- normalize here, at the one place untrusted
        // graph data enters this page's state, instead of every place that
        // later assumes `name`/`label` are strings.
        const rawTriggerInputs = (trigger?.config.inputs as Partial<TriggerInput>[] | undefined) ?? []
        setTriggerInputs(rawTriggerInputs.map((t) => ({ name: String(t?.name ?? ''), label: String(t?.label ?? '') })))
        setTriggerPos({ x: trigger?.position?.x ?? 40, y: trigger?.position?.y ?? 36 })
        setSteps(rest)
        setSelectedNodeId(null)
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
      if (kind === 'filter') return makeFilterStep()
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
   *  position in the chain), a brand-new one lands at the next free spot on
   *  the canvas — harmless in Step view, which ignores `position`. */
  const commitStep = useCallback((step: GraphNode) => {
    setSteps((prev) => {
      if (prev.some((s) => s.nodeId === step.nodeId)) return prev.map((s) => (s.nodeId === step.nodeId ? step : s))
      return [...prev, { ...step, position: nextNodePosition(prev) }]
    })
    setSelectedNodeId(step.nodeId)
  }, [])

  /** Removing a step also drops every binding that pointed AT it — a binding
   *  naming a node that no longer exists would otherwise become a dangling
   *  reference the interpreter silently resolves to nothing, rather than
   *  being cleaned up here at edit time. */
  const removeStep = useCallback((nodeId: string) => {
    setSteps((prev) =>
      prev
        .filter((s) => s.nodeId !== nodeId)
        .map((s) => {
          const kept = Object.entries(s.inputBindings ?? {}).filter(
            ([, b]) => !(b.source === 'node' && b.node_id === nodeId),
          )
          return kept.length === Object.keys(s.inputBindings ?? {}).length ? s : { ...s, inputBindings: Object.fromEntries(kept) }
        }),
    )
    setSelectedNodeId((cur) => (cur === nodeId ? null : cur))
  }, [])

  /** Canvas-only: dragging a card. The trigger isn't in `steps`, so its own
   *  position is tracked separately. */
  const moveNode = useCallback((nodeId: string, position: Point) => {
    if (nodeId === TRIGGER_NODE_ID) {
      setTriggerPos(position)
      return
    }
    setSteps((prev) => prev.map((s) => (s.nodeId === nodeId ? { ...s, position } : s)))
  }, [])

  /** Canvas-only: a line drawn on the canvas — stored as the target step's
   *  input binding, which is the single source of truth for connections. */
  const connectNodes = useCallback((targetNodeId: string, arg: string, binding: WorkflowBinding) => {
    setSteps((prev) => prev.map((s) => (s.nodeId === targetNodeId ? { ...s, inputBindings: { ...s.inputBindings, [arg]: binding } } : s)))
  }, [])

  /** Canvas-only: a line pulled off its input port. The binding is dropped
   *  entirely rather than replaced with a literal — an argument with no
   *  binding falls back to the step's own `config` value server-side, which
   *  is what it had before anything was wired to it. */
  const disconnectNode = useCallback((targetNodeId: string, arg: string) => {
    setSteps((prev) =>
      prev.map((s) => {
        if (s.nodeId !== targetNodeId || !s.inputBindings?.[arg]) return s
        const { [arg]: _dropped, ...rest } = s.inputBindings
        return { ...s, inputBindings: rest }
      }),
    )
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
  /** The only edit surface for a trigger input: its label. The wire `name`
   *  bindings actually resolve against is derived from that label and never
   *  hand-typed, so it can't land in the wrong box the way a raw two-textbox
   *  editor let "Territory Win-Back Flag" happen (name="Toronto",
   *  label="city"). Renaming an already-referenced input cascades the new
   *  name into every step (and filter condition) that binds to it, so a
   *  wired step never silently orphans mid-edit. */
  const renameTriggerInput = useCallback(
    (index: number, label: string) => {
      const current = triggerInputs[index]
      if (!current) return
      const others = new Set(triggerInputs.filter((_, i) => i !== index).map((t) => t.name).filter(Boolean))
      const nextName = label.trim() ? uniqueTriggerInputName(slugifyTriggerInputName(label), others) : current.name
      if (current.name && nextName !== current.name) {
        setSteps((prev) => renameTriggerInputEverywhere(prev, current.name, nextName))
      }
      setTriggerInputs((prev) => prev.map((t, i) => (i === index ? { name: nextName, label } : t)))
    },
    [triggerInputs],
  )
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
        if (s.kind === 'filter') {
          return ['matched', 'unmatched', 'matchedCount', 'totalMatchCount', 'totalCount', 'matchLimitReached', 'matchedTable', 'unmatchedTable'].map((f) => ({
            nodeId: s.nodeId,
            path: f,
            label: `${s.title || 'Filter'} → ${f}`,
          }))
        }
        return []
      }),
    [steps, catalogByTool],
  )

  /** The trigger as a real node, so Canvas view can drag it and give it one
   *  output port per declared input. Synthesized rather than stored in
   *  `steps` because it isn't a step — `buildGraph` sends exactly this. */
  const triggerNode = useMemo<GraphNode>(
    () => ({
      nodeId: TRIGGER_NODE_ID,
      kind: 'trigger',
      title: 'Trigger',
      tool: '',
      config: { inputs: triggerInputs.filter((t) => (t.name ?? '').trim()) },
      inputBindings: {},
      position: triggerPos,
    }),
    [triggerInputs, triggerPos],
  )

  const allNodes = useMemo(() => [triggerNode, ...steps], [triggerNode, steps])

  /** Canvas-only: "no, redo this layout properly" — recomputes every node's
   *  position from scratch (unlike `withLayout`, which only fills gaps),
   *  for a graph whose positions are a hand-dragged mess or a bad guess from
   *  whatever authored it without ever seeing the canvas (e.g. the copilot's
   *  propose_graph). */
  const autoArrangeCanvas = useCallback(() => {
    const laidOut = autoArrange(allNodes)
    const trigger = laidOut.find((n) => n.nodeId === TRIGGER_NODE_ID)
    if (trigger) setTriggerPos({ x: trigger.position?.x ?? 40, y: trigger.position?.y ?? 36 })
    setSteps(laidOut.filter((n) => n.nodeId !== TRIGGER_NODE_ID))
  }, [allNodes])

  const editingStep = useMemo(() => steps.find((s) => s.nodeId === editingStepId) ?? null, [steps, editingStepId])

  /** Outputs the step being added/edited may bind to.
   *  - Step view: only steps strictly before it in the list (`sourcesBefore`)
   *    — unchanged from today, since the interpreter walks that same array.
   *  - Canvas view: any node that wouldn't create a cycle with it — position
   *    on a canvas isn't sequence, so reachability is the real constraint,
   *    the same one `wouldCreateCycle` enforces for a dragged line. */
  const modalNodeSources = useMemo<NodeSource[]>(() => {
    if (viewMode === 'canvas') {
      const targetId = editingStepId
      return steps
        .filter((s) => s.nodeId !== targetId && !(targetId && wouldCreateCycle(allNodes, s.nodeId, targetId)))
        .flatMap((s) =>
          outputPinsFor(s, catalogByTool).map((pin) => ({
            nodeId: s.nodeId,
            path: pin,
            label: `${nodeLabel(s, catalogByTool)} → ${pin}`,
          })),
        )
    }
    const index = editingStepId ? steps.findIndex((s) => s.nodeId === editingStepId) : steps.length
    return sourcesBefore(index < 0 ? steps.length : index)
  }, [viewMode, editingStepId, steps, allNodes, catalogByTool, sourcesBefore])

  /** `edges` is the one thing the two views compute differently: Step view
   *  keeps its existing strict chain (`chainEdges`, unchanged), Canvas view
   *  derives them from the real bindings (`derivedEdges`). Either way
   *  `nodes` is the same `allNodes`, and `inputBindings` — the actual data
   *  flow — never depends on which one is active. */
  const buildGraph = useCallback(
    (): { nodes: GraphNode[]; edges: GraphEdge[] } => ({
      nodes: allNodes,
      edges: viewMode === 'canvas' ? derivedEdges(allNodes) : chainEdges(allNodes),
    }),
    [viewMode, allNodes],
  )

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

  const submitRequest = useCallback(async () => {
    const text = requestText.trim()
    if (!text) return
    setSubmittingRequest(true)
    try {
      await requestWorkflow(text)
      toast.success("Sent — we'll follow up once it's built.")
      setRequestText('')
    } catch (cause) {
      toast.error(cause instanceof ApiError ? cause.message : 'Could not send that request.')
    } finally {
      setSubmittingRequest(false)
    }
  }, [requestText, toast])

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
      // Publishing can change what the run form's own preflight declares
      // (e.g. a newly added trigger input) without changing this graph's id
      // or the run form's draft values -- neither of which the preflight
      // fetch below is otherwise keyed on, so without this the "Run this
      // workflow" panel keeps showing the PREVIOUSLY published version's
      // fields until the workflow is reselected or the page reloads.
      runInputs.refresh()
    } catch (cause) {
      toast.error(cause instanceof ApiError ? cause.message : 'Could not publish that workflow.')
    } finally {
      setPublishing(false)
    }
  }, [persist, loadGraphs, toast, runInputs])

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
    <PageShell className="my-workflows">
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
              <Input placeholder="e.g. City" value={t.label} onChange={(e) => renameTriggerInput(i, e.target.value)} />
              {t.name && <span className="mw-trigger-input-key ui-mono">{t.name}</span>}
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
        description={
          viewMode === 'canvas'
            ? "Drag cards to arrange them. Drag from a step's right-hand dot into another step's left-hand dot to feed its output in. Hover a card to edit or remove it."
            : 'Runs top to bottom. Hover a step to edit, reorder, or remove it.'
        }
        actions={
          <div className="mw-header-actions">
            <SegmentedControl
              segments={[
                { value: 'step', label: 'Step' },
                { value: 'canvas', label: 'Canvas' },
              ]}
              value={viewMode}
              onChange={changeViewMode}
              label="Workflow editor view"
              size="sm"
            />
            {viewMode === 'canvas' && steps.length > 0 && (
              <Button size="sm" variant="ghost" onClick={autoArrangeCanvas}>
                Auto-arrange
              </Button>
            )}
            <AddButton onClick={openAddStep} label="Add a step" size="sm" />
          </div>
        }
        flush={viewMode === 'canvas'}
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
        ) : viewMode === 'canvas' ? (
          <>
            <WorkflowCanvasLegend />
            <WorkflowCanvas
              nodes={allNodes}
              catalog={catalogByTool}
              selectedId={selectedNodeId}
              onSelect={setSelectedNodeId}
              onMoveNode={moveNode}
              onConnect={connectNodes}
              onDisconnect={disconnectNode}
              onEditNode={openEditStep}
              onDeleteNode={removeStep}
              onRejectConnection={(reason) => toast.warn(reason)}
            />
          </>
        ) : (
          <StepFlow
            steps={steps}
            catalogByTool={catalogByTool}
            triggerInputLabels={triggerInputs.filter((t) => (t.label ?? '').trim()).map((t) => t.label)}
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

      <Card
        title="Ask the workflow copilot"
        description="Describe what you want — e.g. a win-back report — and it'll ask what it needs, check what data is actually available, and either build it right here, or put together a draft workflow for you to review in the dropdown below, or tell you plainly if it can't do either yet."
        actions={
          <Button variant="ghost" size="sm" onClick={copilotChat.newConversation}>
            New conversation
          </Button>
        }
        flush
      >
        <div className="mw-copilot">
          <ChatLog
            messages={copilotChat.messages}
            loadingHistory={copilotChat.loadingHistory}
            owner={session.name}
            onRetry={copilotChat.retry}
            onRate={copilotChat.rate}
            onFollowup={copilotComposer.ask}
            navigate={navigate}
            showWorkflowSuggestions={false}
            showFollowupChips={false}
          />
          <div className="mw-copilot-composer">
            <Composer
              composer={copilotComposer}
              busy={copilotChat.busy}
              idPrefix="mw-copilot-ask"
              placeholder="e.g. Build me a report of customers who need a win-back"
              autoFocus={false}
            />
          </div>
        </div>
      </Card>

      <Card
        title="Can't build what you need here?"
        description="Or just tell us directly without chatting — either way it goes straight to the team that builds these."
      >
        <div className="mw-request-form">
          <Field label="What do you need?" hint="Be as specific as you can — what should trigger it, what should it check, what should come out.">
            {(fp) => (
              <Textarea
                {...fp}
                mono={false}
                rows={3}
                placeholder="e.g. Flag customers who haven't reordered in longer than their usual pattern, and aren't closed accounts — email me a weekly list."
                value={requestText}
                onChange={(e) => setRequestText(e.target.value)}
              />
            )}
          </Field>
          <div className="mw-request-form-actions">
            <Button onClick={submitRequest} loading={submittingRequest} disabled={!requestText.trim()}>
              Send request
            </Button>
          </div>
        </div>
      </Card>
    </PageShell>
  )
}

export default MyWorkflowsPage
