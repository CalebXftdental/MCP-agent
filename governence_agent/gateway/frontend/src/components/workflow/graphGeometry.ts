import type { GraphNode } from '../../lib/api'
import { derivedEdges, inputSlotsFor, outputPinsFor, type CatalogIndex } from './graphModel'

/**
 * Canvas geometry — where a node sits, where its ports sit, and the path of
 * the curve between two ports.
 *
 * Every position here is computed arithmetically from a node's own `position`
 * plus a port's index. The legacy canvas instead found each port's real DOM
 * element and called `getBoundingClientRect()` on it, correcting for the
 * scroll container's offset. That works, but it means edges can only be drawn
 * AFTER the nodes have been laid out by the browser, which in React is a
 * render-order dependency that shows up as edges lagging a frame behind the
 * node during a drag. Deterministic math has no such coupling: nodes and
 * edges are rendered from the same numbers in the same commit.
 *
 * The price of that choice is that these numbers must describe the card's REAL
 * rendered box, so every band of a node has a fixed height here and
 * WorkflowNode applies those same constants inline. Nothing about a node's
 * height may depend on how its text happens to wrap.
 */

export const NODE_W = 212

/**
 * The card's own 1px border. Absolutely-positioned children — which is every
 * port — are placed relative to the PADDING box, i.e. inside that border, so
 * each port centre is shifted by it. One pixel, but it is the difference
 * between a curve that lands on the dot and one that lands beside it.
 */
export const NODE_BORDER = 1

export const PORT_SIZE = 12
export const PORT_R = PORT_SIZE / 2

/**
 * Fixed band heights, top to bottom: title row, summary, port rows, warning.
 * BODY_H holds two clamped lines of --ui-fs-2xs (11px at line-height 1.3) plus
 * the band's own 0.3rem vertical padding — if it were any shorter the second
 * line would be sliced through the middle rather than ellipsised.
 */
export const HEAD_H = 30
export const BODY_H = 40
export const WARN_H = 21
export const PORT_GAP = 20
const PORTS_PAD_TOP = 7
const PORTS_PAD_BOTTOM = 9

/** Centre of the first port row, relative to the node's padding box top. */
export const PORT_TOP = HEAD_H + BODY_H + PORTS_PAD_TOP + PORT_GAP / 2

export interface Point {
  x: number
  y: number
}

export function nodePosition(node: GraphNode): Point {
  return { x: node.position?.x ?? 0, y: node.position?.y ?? 0 }
}

/** Port rows a node shows — inputs on the left, outputs on the right, sharing
 *  rows so a node is as tall as its busier side. Zero means no port band at
 *  all (and so no divider) rather than an empty one. */
export function portRowCount(node: GraphNode, catalog: CatalogIndex): number {
  return Math.max(inputSlotsFor(node, catalog).length, outputPinsFor(node, catalog).length)
}

export function portsBandHeight(rows: number): number {
  return rows > 0 ? PORTS_PAD_TOP + rows * PORT_GAP + PORTS_PAD_BOTTOM : 0
}

export function nodeHeight(node: GraphNode, catalog: CatalogIndex, needsGate = false): number {
  const bands = HEAD_H + BODY_H + portsBandHeight(portRowCount(node, catalog)) + (needsGate ? WARN_H : 0)
  return bands + NODE_BORDER * 2
}

function portY(node: GraphNode, index: number): number {
  return nodePosition(node).y + NODE_BORDER + PORT_TOP + index * PORT_GAP
}

/** Centre of an input port — on the node's left edge. */
export function inPortPoint(node: GraphNode, index: number): Point {
  return { x: nodePosition(node).x + NODE_BORDER, y: portY(node, index) }
}

/** Centre of an output port — on the node's right edge. */
export function outPortPoint(node: GraphNode, index: number): Point {
  return { x: nodePosition(node).x + NODE_W - NODE_BORDER, y: portY(node, index) }
}

/** A horizontal-tangent cubic bezier, so lines leave a port going right and
 *  arrive going right — reads as flow direction even when the target sits to
 *  the left of its source. */
export function edgePath(from: Point, to: Point): string {
  const dx = Math.max(40, Math.abs(to.x - from.x) / 2)
  return `M${from.x},${from.y} C${from.x + dx},${from.y} ${to.x - dx},${to.y} ${to.x},${to.y}`
}

/** Canvas extent — enough room for every node plus space to drag into. Takes
 *  the ungated set because a warning band makes a card taller. */
export function canvasSize(
  nodes: GraphNode[],
  catalog: CatalogIndex,
  needsGate?: Set<string>,
): { width: number; height: number } {
  let maxX = 0
  let maxY = 0
  for (const node of nodes) {
    const { x, y } = nodePosition(node)
    maxX = Math.max(maxX, x + NODE_W)
    maxY = Math.max(maxY, y + nodeHeight(node, catalog, needsGate?.has(node.nodeId)))
  }
  return { width: Math.max(900, maxX + 260), height: Math.max(420, maxY + 160) }
}

const COL_W = 262
const ROW_H = 172
const ORIGIN: Point = { x: 40, y: 36 }

/** Where a newly added step should land: to the right of everything already
 *  placed, wrapping to a new row rather than running off the canvas. */
export function nextNodePosition(nodes: GraphNode[]): Point {
  const placed = nodes.filter((n) => n.kind !== 'trigger')
  const column = 1 + (placed.length % 3)
  const row = Math.floor(placed.length / 3)
  return { x: ORIGIN.x + column * COL_W, y: ORIGIN.y + row * ROW_H }
}

/**
 * Fills in positions for nodes that don't have one, laid out left-to-right in
 * dependency order.
 *
 * Needed because a graph saved by the previous linear step-list builder has
 * no positions at all (it never had a canvas to place anything on) — without
 * this, opening one of those would stack every node at (0,0) in a single
 * unreadable pile. Nodes that already carry a position keep it untouched.
 */
export function withLayout(nodes: GraphNode[]): GraphNode[] {
  const hasPosition = (n: GraphNode) => (n.position?.x ?? 0) !== 0 || (n.position?.y ?? 0) !== 0
  if (nodes.every(hasPosition)) return nodes

  // Longest-path depth per node, so a step always sits right of what feeds it.
  const depth = new Map<string, number>()
  const edges = derivedEdges(nodes)
  for (const n of nodes) depth.set(n.nodeId, n.kind === 'trigger' ? 0 : 1)
  // |nodes| passes is enough to settle longest paths in a DAG.
  for (let pass = 0; pass < nodes.length; pass++) {
    let changed = false
    for (const e of edges) {
      const next = (depth.get(e.sourceNodeId) ?? 0) + 1
      if (next > (depth.get(e.targetNodeId) ?? 0)) {
        depth.set(e.targetNodeId, next)
        changed = true
      }
    }
    if (!changed) break
  }

  const usedRows = new Map<number, number>()
  return nodes.map((node) => {
    if (hasPosition(node)) return node
    const column = depth.get(node.nodeId) ?? 0
    const row = usedRows.get(column) ?? 0
    usedRows.set(column, row + 1)
    return { ...node, position: { x: ORIGIN.x + column * COL_W, y: ORIGIN.y + row * ROW_H } }
  })
}
