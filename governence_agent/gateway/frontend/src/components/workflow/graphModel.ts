import type { FilterCondition, GraphEdge, GraphNode, WorkflowBinding, WorkflowGraphCatalogTool } from '../../lib/api'

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
  if (node.kind === 'filter') return [{ name: 'input', label: 'List to filter', required: true }]
  if (node.kind === 'loop') return [{ name: 'input', label: 'List to repeat over', required: true }]
  return []
}

/** The node ids each loop owns, and the reverse lookup. A body node is a normal
 *  node that happens to run once per row -- ownership is what the server checks
 *  (dominance, single entry, no escaping output), so the canvas has to know it
 *  too or it will draw and save edges the server rejects. */
export function loopBodyOwners(nodes: GraphNode[]): Map<string, string> {
  const owners = new Map<string, string>()
  for (const n of nodes) {
    if (n.kind !== 'loop') continue
    for (const bid of (n.config.body as string[] | undefined) ?? []) {
      if (!owners.has(bid)) owners.set(bid, n.nodeId)
    }
  }
  return owners
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
  if (node.kind === 'loop') {
    // Only the loop's AGGREGATE output may be consumed downstream -- a body
    // node's own output is per-row and has no meaning outside the loop, which
    // the server enforces as `loop_body_output_escapes`.
    return ['results', 'artifactIds', 'itemCount', 'iterations', 'succeeded', 'failed', 'errors', 'truncated', 'truncatedReason']
  }
  if (node.kind === 'filter') {
    return ['matched', 'unmatched', 'matchedCount', 'totalMatchCount', 'totalCount', 'matchLimitReached', 'matchedTable', 'unmatchedTable']
  }
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

/** Does this argument actually have a value — a wire, a trigger reference, or
 *  a literal that was actually typed into (not just "Type a value" selected
 *  and left blank)? Distinct from `bindingSourceNodeId(...) != null`, which
 *  only answers "is there a WIRE" — a filled-in literal has no source node
 *  but is still a set value, and treating it as unset (the canvas's original
 *  behavior) kept the required-field asterisk lit after someone had already
 *  filled the field in. */
export function isArgFilled(binding: WorkflowBinding | undefined): boolean {
  if (!binding) return false
  if (binding.source === 'literal') return binding.value !== undefined && binding.value !== null && String(binding.value).trim() !== ''
  return true
}

/** Required input slots with no value at all yet — a build-time gap
 *  `validate_graph` does not check server-side (see workflow_graph_store.py),
 *  so surfacing it live here is the only warning a builder gets before a run
 *  silently resolves the missing arg to nothing. */
export function missingRequiredArgs(node: GraphNode, catalog: CatalogIndex): InputSlot[] {
  return inputSlotsFor(node, catalog).filter((slot) => slot.required && !isArgFilled(node.inputBindings?.[slot.name]))
}

export function nodesWithMissingRequired(nodes: GraphNode[], catalog: CatalogIndex): Set<string> {
  return new Set(nodes.filter((n) => missingRequiredArgs(n, catalog).length > 0).map((n) => n.nodeId))
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
  const owners = loopBodyOwners(nodes)
  const edges: GraphEdge[] = []

  for (const node of nodes) {
    if (node.kind === 'trigger') continue
    const owner = owners.get(node.nodeId)
    const sources = new Set<string>()
    for (const binding of Object.values(node.inputBindings ?? {})) {
      const sourceId = bindingSourceNodeId(binding)
      // A binding pointing at a node that has since been deleted must not
      // become a dangling edge -- the server rejects the whole graph for one.
      if (sourceId && sourceId !== node.nodeId && byId.has(sourceId)) sources.add(sourceId)
    }
    if (owner) {
      // A body node's fallback source is its LOOP, never the trigger. A node
      // wired only to `loop_item` has no node-source binding at all, so the
      // trigger fallback below would have given it an edge straight from the
      // trigger -- which is exactly the shape the server rejects as
      // `loop_body_not_dominated`, since it means there is a way into the body
      // that bypasses the loop.
      sources.delete(trigger?.nodeId ?? '')
      if (sources.size === 0) sources.add(owner)
    } else if (sources.size === 0 && trigger) {
      sources.add(trigger.nodeId)
    }
    for (const sourceId of sources) {
      edges.push({ edgeId: nextEdgeId(), sourceNodeId: sourceId, targetNodeId: node.nodeId })
    }
  }
  return edges
}

// ── Trigger input naming ────────────────────────────────────────────────────
//
// A declared trigger input has a `label` (what whoever runs the workflow
// sees) and a `name` (the wire key `{source:'trigger', path}` bindings and
// the run form's values dict actually use). The builder UI only ever shows
// the label -- `name` is derived from it automatically -- so these two things
// can never again land in the wrong box the way "Territory Win-Back Flag"'s
// did (name="Toronto", label="city": harmless by accident since both sides
// used the same wrong string consistently, but a landmine for anyone reading
// the graph's raw JSON later).

/** `"Days since last order"` -> `"days_since_last_order"`. Falls back to
 *  `"input"` for a label that slugifies to nothing (e.g. all punctuation). */
export function slugifyTriggerInputName(label: string): string {
  const slug = label
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '_')
    .replace(/^_+|_+$/g, '')
  return slug || 'input'
}

/** `base` deduped against `taken` (every OTHER trigger input's current name)
 *  by appending `_2`, `_3`, ... -- two inputs labeled "City" and "city!" both
 *  slugify to `city` and still need distinct wire keys. */
export function uniqueTriggerInputName(base: string, taken: Set<string>): string {
  if (!taken.has(base)) return base
  let n = 2
  while (taken.has(`${base}_${n}`)) n += 1
  return `${base}_${n}`
}

function renameTriggerRefInBinding(binding: WorkflowBinding | undefined, oldName: string, newName: string): WorkflowBinding | undefined {
  if (binding && binding.source === 'trigger' && binding.path === oldName) return { source: 'trigger', path: newName }
  return binding
}

/** `FilterCondition`'s leaf `value` can itself be a `WorkflowBinding` (the
 *  filter editor reuses `BindingRow` for it) -- walk the same `all`/`any`/leaf
 *  recursion `_evaluate_condition_tree` uses server-side so a rename doesn't
 *  miss a trigger reference buried in a filter step's conditions. */
function renameTriggerRefInCondition(cond: FilterCondition, oldName: string, newName: string): FilterCondition {
  if ('all' in cond) return { all: cond.all.map((c) => renameTriggerRefInCondition(c, oldName, newName)) }
  if ('any' in cond) return { any: cond.any.map((c) => renameTriggerRefInCondition(c, oldName, newName)) }
  const value = cond.value as WorkflowBinding | undefined
  if (value && typeof value === 'object' && 'source' in value) {
    return { ...cond, value: renameTriggerRefInBinding(value, oldName, newName) }
  }
  return cond
}

/** Renaming a trigger input's wire key must update every place that key was
 *  already referenced, or the rename silently orphans a binding (the exact
 *  failure mode a raw two-textbox name/label editor invited). Called on every
 *  edit of a trigger input's label, so a step wired to it before the rename
 *  stays wired after. */
export function renameTriggerInputEverywhere(nodes: GraphNode[], oldName: string, newName: string): GraphNode[] {
  if (!oldName || oldName === newName) return nodes
  return nodes.map((n) => {
    const inputBindings = Object.fromEntries(
      Object.entries(n.inputBindings ?? {}).map(([k, b]) => [k, renameTriggerRefInBinding(b, oldName, newName) ?? b]),
    )
    let config = n.config
    if (n.kind === 'filter' && config.conditions) {
      config = { ...config, conditions: renameTriggerRefInCondition(config.conditions as FilterCondition, oldName, newName) }
    }
    return { ...n, inputBindings, config }
  })
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

/** Node-level half of "can this wire land here" — self-loop and cycle, the
 *  same two rejections `WorkflowCanvas`'s drop handler already enforces
 *  after the fact. Exposed separately so the canvas can also ask it BEFORE
 *  the drop, while a connection is still being dragged, to only light up
 *  ports that would actually be accepted. */
export function canConnectNodes(nodes: GraphNode[], sourceNodeId: string, targetNodeId: string): boolean {
  if (sourceNodeId === targetNodeId) return false
  return !wouldCreateCycle(nodes, sourceNodeId, targetNodeId)
}

/** Slot-level half of "can this wire land here" — mirrors `validate_graph`'s
 *  one binding-kind rule (workflow_graph_store.py): an `llm_transform`'s
 *  `input_text` may be sourced from a `tool_call`/`llm_transform` node (or the
 *  trigger, which never reaches this check — `bindingFromPort` gives it a
 *  `trigger` source, not a `node` one), but never from an `approval_gate`'s
 *  output. Every other slot/source combination has no server-side kind
 *  restriction. */
export function isBindingKindAllowed(
  targetNode: GraphNode,
  slot: InputSlot,
  sourceNode: GraphNode,
  nodes: GraphNode[] = [],
): boolean {
  if (targetNode.kind === 'llm_transform' && slot.name === 'input_text' && sourceNode.kind === 'approval_gate') {
    return false
  }
  // A body node's output is per-row; only its loop's aggregate output means
  // anything outside (server: `loop_body_output_escapes`). Two body nodes in the
  // SAME loop may of course wire to each other — that is the normal shape, and
  // the interpreter resolves it to the current iteration's copy.
  if (nodes.length) {
    const owners = loopBodyOwners(nodes)
    const sourceOwner = owners.get(sourceNode.nodeId)
    if (sourceOwner && owners.get(targetNode.nodeId) !== sourceOwner) return false
  }
  return true
}

export function nodeLabel(node: GraphNode, catalog: CatalogIndex): string {
  if (node.kind === 'trigger') return 'Trigger'
  if (node.title) return node.title
  if (node.kind === 'approval_gate') return 'Approval gate'
  if (node.kind === 'llm_transform') return `AI: ${String(node.config.kind ?? 'summarize')}`
  if (node.kind === 'filter') return 'Filter'
  if (node.kind === 'loop') return 'For each'
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
