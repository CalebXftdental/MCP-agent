import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { PointerEvent as ReactPointerEvent } from 'react'
import WorkflowNode from './WorkflowNode'
import { canvasSize, edgePath, inPortPoint, nodePosition, outPortPoint, type Point } from './graphGeometry'
import {
  bindingFromPort,
  bindingSourceNodeId,
  inputSlotsFor,
  outputPinsFor,
  ungatedSendNodeIds,
  wouldCreateCycle,
  type CatalogIndex,
} from './graphModel'
import type { GraphNode, WorkflowBinding } from '../../lib/api'
import './WorkflowCanvas.css'

/**
 * The My Workflow canvas: draggable step cards joined by curves you draw
 * yourself, from a step's output port to a later step's input port.
 *
 * A drawn curve IS an input binding (see graphModel's note) — there is no
 * separate "edges" structure being maintained alongside, so a line on screen
 * and the data the server executes can't disagree.
 *
 * Both interactions (dragging a node, dragging a connection) run off window
 * pointer listeners installed only while a gesture is active, rather than
 * `setPointerCapture` on the grabbed element. Capture would retarget every
 * subsequent event to the element the gesture started on, which is exactly
 * wrong for connecting: the drop target is whatever port is under the cursor
 * at the END of the gesture, so it's resolved with `elementFromPoint` instead.
 */

export interface WorkflowCanvasProps {
  nodes: GraphNode[]
  catalog: CatalogIndex
  selectedId: string | null
  onSelect: (nodeId: string | null) => void
  onMoveNode: (nodeId: string, position: Point) => void
  onConnect: (targetNodeId: string, arg: string, binding: WorkflowBinding) => void
  onDisconnect: (targetNodeId: string, arg: string) => void
  onEditNode: (nodeId: string) => void
  onDeleteNode: (nodeId: string) => void
  /** Surfaced as a toast by the page — the canvas itself stays presentational. */
  onRejectConnection: (reason: string) => void
}

interface DragState {
  nodeId: string
  startClient: Point
  startPos: Point
  moved: boolean
}

interface ConnectState {
  /** The node the wire comes OUT of — the same whether the gesture started at
   *  that output or by grabbing the far end of an existing wire. */
  sourceNodeId: string
  pin: string
  from: Point
  cursor: Point
  startClient: Point
  moved: boolean
  /** Set when the gesture began on an already-wired input: which connection is
   *  being picked up. It is not removed until the gesture ENDS, so a rejected
   *  or abandoned drag leaves the original wire untouched. */
  detach: { nodeId: string; arg: string } | null
}

const DRAG_THRESHOLD = 3

function WorkflowCanvas({
  nodes,
  catalog,
  selectedId,
  onSelect,
  onMoveNode,
  onConnect,
  onDisconnect,
  onEditNode,
  onDeleteNode,
  onRejectConnection,
}: WorkflowCanvasProps) {
  const surfaceRef = useRef<HTMLDivElement>(null)
  const dragRef = useRef<DragState | null>(null)
  const connectRef = useRef<ConnectState | null>(null)
  const [connect, setConnect] = useState<ConnectState | null>(null)
  const [dragging, setDragging] = useState(false)

  const byId = useMemo(() => new Map(nodes.map((n) => [n.nodeId, n])), [nodes])
  const ungated = useMemo(() => ungatedSendNodeIds(nodes, catalog), [nodes, catalog])
  const size = useMemo(() => canvasSize(nodes, catalog, ungated), [nodes, catalog, ungated])

  /** Client coords -> canvas coords. The surface element scrolls with its
   *  content, so its own rect already accounts for scroll offset. */
  const toCanvas = useCallback((clientX: number, clientY: number): Point => {
    const rect = surfaceRef.current?.getBoundingClientRect()
    return { x: clientX - (rect?.left ?? 0), y: clientY - (rect?.top ?? 0) }
  }, [])

  const onPointerDownNode = useCallback(
    (e: ReactPointerEvent, nodeId: string) => {
      // Ports and the hover action buttons opt out, so grabbing one of those
      // doesn't also start sliding the card around.
      if ((e.target as HTMLElement).closest('[data-no-drag]')) return
      if (e.button !== 0) return
      const node = byId.get(nodeId)
      if (!node) return
      dragRef.current = {
        nodeId,
        startClient: { x: e.clientX, y: e.clientY },
        startPos: nodePosition(node),
        moved: false,
      }
      e.preventDefault()
    },
    [byId],
  )

  const beginConnect = useCallback(
    (e: ReactPointerEvent, sourceNode: GraphNode, pin: string, detach: ConnectState['detach']) => {
      const index = outputPinsFor(sourceNode, catalog).indexOf(pin)
      const from = outPortPoint(sourceNode, Math.max(0, index))
      const state: ConnectState = {
        sourceNodeId: sourceNode.nodeId,
        pin,
        from,
        cursor: from,
        startClient: { x: e.clientX, y: e.clientY },
        moved: false,
        detach,
      }
      connectRef.current = state
      setConnect(state)
      e.preventDefault()
      e.stopPropagation()
    },
    [catalog],
  )

  const onPointerDownOutPort = useCallback(
    (e: ReactPointerEvent, nodeId: string, pin: string) => {
      if (e.button !== 0) return
      const node = byId.get(nodeId)
      if (node) beginConnect(e, node, pin, null)
    },
    [byId, beginConnect],
  )

  /** Grabbing a wired input picks that wire's end up: it follows the cursor from
   *  its original source, lands on whatever input it's dropped on, and is
   *  removed if dropped anywhere else. An unwired input has nothing to pick up,
   *  so a press there does nothing (it stays a drop target). */
  const onPointerDownInPort = useCallback(
    (e: ReactPointerEvent, nodeId: string, arg: string) => {
      if (e.button !== 0) return
      const binding = byId.get(nodeId)?.inputBindings?.[arg]
      if (!binding || binding.source === 'literal') return
      const sourceId = bindingSourceNodeId(binding)
      const sourceNode = sourceId ? byId.get(sourceId) : undefined
      if (!sourceNode) return
      beginConnect(e, sourceNode, binding.path, { nodeId, arg })
    },
    [byId, beginConnect],
  )

  useEffect(() => {
    const onMove = (e: PointerEvent) => {
      const drag = dragRef.current
      if (drag) {
        const dx = e.clientX - drag.startClient.x
        const dy = e.clientY - drag.startClient.y
        if (!drag.moved && Math.abs(dx) < DRAG_THRESHOLD && Math.abs(dy) < DRAG_THRESHOLD) return
        if (!drag.moved) {
          drag.moved = true
          setDragging(true)
        }
        // Clamped at 0 so a card can't be dragged out past the top/left edge
        // into space the scroll container can never reach.
        onMoveNode(drag.nodeId, {
          x: Math.max(0, drag.startPos.x + dx),
          y: Math.max(0, drag.startPos.y + dy),
        })
        return
      }
      const pending = connectRef.current
      if (pending) {
        const moved =
          pending.moved ||
          Math.abs(e.clientX - pending.startClient.x) >= DRAG_THRESHOLD ||
          Math.abs(e.clientY - pending.startClient.y) >= DRAG_THRESHOLD
        const next = { ...pending, moved, cursor: toCanvas(e.clientX, e.clientY) }
        connectRef.current = next
        setConnect(next)
      }
    }

    /** Everything about a connection gesture is decided here, at the end — the
     *  graph is not touched while the wire is in flight. */
    const finishConnect = (pending: ConnectState, e: PointerEvent) => {
      const { detach } = pending
      // A press that never moved is a click on a dot: leave the graph alone.
      if (!pending.moved) return

      const portEl = (document.elementFromPoint(e.clientX, e.clientY) as HTMLElement | null)?.closest<HTMLElement>(
        '[data-in-port]',
      )
      const targetNodeId = portEl?.dataset.nodeId
      const arg = portEl?.dataset.inPort

      if (!targetNodeId || !arg) {
        // Dropped into empty space. For a wire that was picked up that means
        // "disconnect"; for a new one, nothing was ever created.
        if (detach) onDisconnect(detach.nodeId, detach.arg)
        return
      }
      // Dropped back where it started.
      if (detach && targetNodeId === detach.nodeId && arg === detach.arg) return

      if (targetNodeId === pending.sourceNodeId) {
        onRejectConnection("A step can't feed into itself.")
        return
      }
      // The wire being moved is still in `nodes`, so the cycle check has to run
      // against the graph as it will be once that wire is gone — otherwise
      // reversing a connection reads as a loop that won't actually exist.
      const effective = detach
        ? nodes.map((n) =>
            n.nodeId === detach.nodeId
              ? {
                  ...n,
                  inputBindings: Object.fromEntries(
                    Object.entries(n.inputBindings ?? {}).filter(([key]) => key !== detach.arg),
                  ),
                }
              : n,
          )
        : nodes
      if (wouldCreateCycle(effective, pending.sourceNodeId, targetNodeId)) {
        onRejectConnection('That connection would create a loop.')
        return
      }

      const sourceNode = byId.get(pending.sourceNodeId)
      if (!sourceNode) return
      if (detach) onDisconnect(detach.nodeId, detach.arg)
      onConnect(targetNodeId, arg, bindingFromPort(sourceNode, pending.pin))
    }

    const onUp = (e: PointerEvent) => {
      const drag = dragRef.current
      if (drag) {
        // A press that never moved is a click: select the card.
        if (!drag.moved) onSelect(drag.nodeId)
        dragRef.current = null
        setDragging(false)
      }

      const pending = connectRef.current
      if (pending) {
        connectRef.current = null
        setConnect(null)
        finishConnect(pending, e)
      }
    }

    window.addEventListener('pointermove', onMove)
    window.addEventListener('pointerup', onUp)
    window.addEventListener('pointercancel', onUp)
    return () => {
      window.removeEventListener('pointermove', onMove)
      window.removeEventListener('pointerup', onUp)
      window.removeEventListener('pointercancel', onUp)
    }
  }, [nodes, byId, onMoveNode, onSelect, onConnect, onDisconnect, onRejectConnection, toCanvas])

  /** One curve per bound input, computed from the same positions the nodes
   *  render at — so lines track the card live while it's being dragged. */
  const curves = useMemo(() => {
    // The wire currently in the user's hand is drawn as the rubber band
    // instead, so it must not also be drawn in place.
    const held = connect?.moved && connect.detach ? `${connect.detach.nodeId}:${connect.detach.arg}` : null
    const out: { key: string; d: string }[] = []
    for (const node of nodes) {
      const slots = inputSlotsFor(node, catalog)
      slots.forEach((slot, slotIndex) => {
        const key = `${node.nodeId}:${slot.name}`
        if (key === held) return
        const binding = node.inputBindings?.[slot.name]
        if (!binding || binding.source === 'literal') return
        const sourceId = bindingSourceNodeId(binding)
        const sourceNode = sourceId ? byId.get(sourceId) : undefined
        if (!sourceNode) return
        const pinIndex = outputPinsFor(sourceNode, catalog).indexOf(binding.path)
        if (pinIndex < 0) return
        out.push({ key, d: edgePath(outPortPoint(sourceNode, pinIndex), inPortPoint(node, slotIndex)) })
      })
    }
    return out
  }, [nodes, byId, catalog, connect])

  return (
    <div
      className="wfc-wrap"
      data-dragging={dragging || undefined}
      data-connecting={connect != null || undefined}
    >
      <div
        ref={surfaceRef}
        className="wfc-surface"
        style={{ width: size.width, height: size.height }}
        onPointerDown={(e) => {
          // A press on empty canvas clears the selection.
          if (e.target === e.currentTarget) onSelect(null)
        }}
      >
        <svg className="wfc-edges" width={size.width} height={size.height} aria-hidden="true">
          {curves.map((c) => (
            <path key={c.key} d={c.d} className="wfc-edge" />
          ))}
          {connect && <path d={edgePath(connect.from, connect.cursor)} className="wfc-edge wfc-edge--pending" />}
        </svg>

        {nodes.map((node) => (
          <WorkflowNode
            key={node.nodeId}
            node={node}
            catalog={catalog}
            tool={node.kind === 'tool_call' ? catalog[node.tool] : undefined}
            selected={node.nodeId === selectedId}
            needsGate={ungated.has(node.nodeId)}
            connecting={connect != null && connect.sourceNodeId !== node.nodeId}
            onPointerDownNode={onPointerDownNode}
            onPointerDownOutPort={onPointerDownOutPort}
            onPointerDownInPort={onPointerDownInPort}
            onEdit={onEditNode}
            onDelete={onDeleteNode}
          />
        ))}
      </div>
    </div>
  )
}

export default WorkflowCanvas
