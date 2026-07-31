import type { GraphEdge, GraphNode, WorkflowBinding, WorkflowGraphCatalogTool } from '../../lib/api'

/**
 * Shared graph logic for the My Workflow canvas — the parts that are pure
 * data, kept out of the components that draw them.
 *
 * The load-bearing rule (carried over from the legacy canvas): `edges` is
 * DERIVED from every node's `inputBindings`, never hand-edited. A connection
 * on the canvas IS a binding, so there is exactly one source of truth for
 * "what's wired to what" rather than two structures to keep in sync. Edges
 * are regenerated at save/validate/draw time by `derivedEdges`.
 */

export const TRIGGER_NODE_ID = 'trigger'

export type CatalogIndex = Record<string, WorkflowGraphCatalogTool | undefined>

/** A declared input someone can wire INTO (a node's left-hand ports). */
export interface InputSlot {
  name: string
  label: string
  required?: boolean
}

/** `owner` is injected server-side from the authenticated run owner
 *  (workflow_graph_interpreter.py overrides whatever a binding says), so
 *  offering it as a wirable input would be a port that does nothing. */
const HIDDEN_TOOL_ARGS = new Set(['owner'])

export function inputSlotsFor(node: GraphNode, catalog: CatalogIndex): InputSlot[] {
  if (node.kind === 'tool_call') {
    const meta = catalog[node.tool]
    const props = meta?.parameters?.properties ?? {}
    const required = new Set(meta?.parameters?.required ?? [])
    return Object.keys(props)
      .filter((k) => !HIDDEN_TOOL_ARGS.has(k))
      .map((k) => ({ name: k, label: props[k]?.description || k, required: required.has(k) }))
  }
  if (node.kind === 'llm_transform') return [{ name: 'input_text', label: 'Text to work from', required: true }]
  return []
}

/** Values a node produces, which can be wired onward (right-hand ports). For
 *  the trigger that's the workflow's own declared inputs — dragging from one
 *  of those produces a `trigger`-source binding, not a `node` one (see
 *  `bindingFromPort`). */
export function outputPinsFor(node: GraphNode, catalog: CatalogIndex): string[] {
  if (node.kind === 'trigger') {
    const declared = (node.config.inputs as { name?: string }[] | undefined) ?? []
    return declared.map((i) => i.name ?? '').filter(Boolean)
  }
  if (node.kind === 'tool_call') return catalog[node.tool]?.outputFields ?? []
  if (node.kind === 'approval_gate') return ['approvalId']
  if (node.kind === 'llm_transform') return ['text']
  return []
}

/** The binding a dragged connection becomes. A trigger source must emit
 *  `{source:'trigger'}` — the interpreter resolves a `node` source by
 *  searching the RUN's steps, and the trigger is never added as a step, so a
 *  `{source:'node', node_id:'trigger'}` binding would silently resolve to
 *  null at run time. */
export function bindingFromPort(sourceNode: GraphNode, pin: string): WorkflowBinding {
  if (sourceNode.kind === 'trigger') return { source: 'trigger', path: pin }
  return { source: 'node', node_id: sourceNode.nodeId, path: pin }
}

/** Which node a binding draws its line FROM, or null for a literal (which has
 *  no line — it's a typed value, not a connection). */
export function bindingSourceNodeId(binding: WorkflowBinding | undefined): string | null {
  if (!binding) return null
  if (binding.source === 'node') return binding.node_id
  if (binding.source === 'trigger') return TRIGGER_NODE_ID
  return null
}

let edgeSeq = 0
function nextEdgeId(): string {
  edgeSeq += 1
  return `e_${edgeSeq}`
}

/**
 * One edge per (target node, bound argument) whose source is another node —
 * this is what gets POSTed and what the cycle check walks.
 *
 * A node whose every argument is a literal has no data dependency at all,
 * but still has to be ordered and reachable or the server's `validate_graph`
 * reports it as an orphan ("nodes not reachable from the trigger"). Those get
 * an implicit trigger -> node edge, matching the legacy builder.
 */
export function derivedEdges(nodes: GraphNode[]): GraphEdge[] {
  const trigger = nodes.find((n) => n.kind === 'trigger')
  const byId = new Set(nodes.map((n) => n.nodeId))
  const edges: GraphEdge[] = []

  for (const node of nodes) {
    if (node.kind === 'trigger') continue
    const sources = new Set<string>()
    for (const binding of Object.values(node.inputBindings ?? {})) {
      const sourceId = bindingSourceNodeId(binding)
      // A binding pointing at a node that has since been deleted must not
      // become a dangling edge -- the server rejects the whole graph for one.
      if (sourceId && sourceId !== node.nodeId && byId.has(sourceId)) sources.add(sourceId)
    }
    if (sources.size === 0 && trigger) sources.add(trigger.nodeId)
    for (const sourceId of sources) {
      edges.push({ edgeId: nextEdgeId(), sourceNodeId: sourceId, targetNodeId: node.nodeId })
    }
  }
  return edges
}

/** Would wiring `fromId -> toId` create a cycle? True iff `toId` can already
 *  reach `fromId`. Checked before a connection is accepted so the canvas
 *  never builds a graph the server would reject outright. */
export function wouldCreateCycle(nodes: GraphNode[], fromId: string, toId: string): boolean {
  if (fromId === toId) return true
  const out = new Map<string, string[]>()
  for (const e of derivedEdges(nodes)) {
    const list = out.get(e.sourceNodeId)
    if (list) list.push(e.targetNodeId)
    else out.set(e.sourceNodeId, [e.targetNodeId])
  }
  const seen = new Set([toId])
  const queue = [toId]
  while (queue.length) {
    const cur = queue.pop()!
    for (const next of out.get(cur) ?? []) {
      if (next === fromId) return true
      if (!seen.has(next)) {
        seen.add(next)
        queue.push(next)
      }
    }
  }
  return false
}

export function nodeLabel(node: GraphNode, catalog: CatalogIndex): string {
  if (node.kind === 'trigger') return 'Trigger'
  if (node.title) return node.title
  if (node.kind === 'approval_gate') return 'Approval gate'
  if (node.kind === 'llm_transform') return `AI: ${String(node.config.kind ?? 'summarize')}`
  return catalog[node.tool]?.canonical ?? node.tool
}

/**
 * The set of nodes reachable from the trigger WITHOUT passing through an
 * approval gate — a direct port of `workflow_graph_store.validate_graph`'s
 * `_reachable_from(trigger, exclude_kinds={"approval_gate"})`.
 *
 * Deleting the gate nodes from the graph severs every path that ran through
 * one, so whatever is still reachable is exactly what runs ungated.
 */
function reachableWithoutGates(nodes: GraphNode[]): Set<string> {
  const trigger = nodes.find((n) => n.kind === 'trigger')
  if (!trigger) return new Set()

  const gated = new Set(nodes.filter((n) => n.kind === 'approval_gate').map((n) => n.nodeId))
  const out = new Map<string, string[]>()
  for (const e of derivedEdges(nodes)) {
    if (gated.has(e.sourceNodeId) || gated.has(e.targetNodeId)) continue
    const list = out.get(e.sourceNodeId)
    if (list) list.push(e.targetNodeId)
    else out.set(e.sourceNodeId, [e.targetNodeId])
  }

  const seen = new Set([trigger.nodeId])
  const queue = [trigger.nodeId]
  while (queue.length) {
    const cur = queue.pop()!
    for (const next of out.get(cur) ?? []) {
      if (!seen.has(next)) {
        seen.add(next)
        queue.push(next)
      }
    }
  }
  return seen
}

/** Which nodes call a send-risk tool reachable without an approval gate — the
 *  exact rule `validate_graph` rejects a graph for, surfaced live on the
 *  canvas so it shows up while building rather than only on save. Computed for
 *  the whole graph at once because the reachability walk is shared. */
export function ungatedSendNodeIds(nodes: GraphNode[], catalog: CatalogIndex): Set<string> {
  const sendNodes = nodes.filter((n) => n.kind === 'tool_call' && catalog[n.tool]?.riskLevel === 'send')
  if (sendNodes.length === 0) return new Set()
  const reachable = reachableWithoutGates(nodes)
  return new Set(sendNodes.filter((n) => reachable.has(n.nodeId)).map((n) => n.nodeId))
}
