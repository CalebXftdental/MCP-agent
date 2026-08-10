import type { PointerEvent as ReactPointerEvent } from 'react'
import { Badge } from '../ui'
import type { GraphNode, WorkflowGraphCatalogTool } from '../../lib/api'
import {
  BODY_H,
  HEAD_H,
  NODE_W,
  PORT_GAP,
  PORT_TOP,
  WARN_H,
  nodeHeight,
  nodePosition,
  portRowCount,
  portsBandHeight,
} from './graphGeometry'
import {
  bindingSourceNodeId,
  inputSlotsFor,
  isArgFilled,
  nodeLabel,
  outputPinsFor,
  type CatalogIndex,
  type InputSlot,
} from './graphModel'
import { stepIcon, stepSummary } from './stepMeta'

/**
 * One card on the My Workflow canvas, in fixed bands: a title row, a one-line
 * summary of what the step is set to, then one row per port — inputs on the
 * left, outputs on the right — and a warning strip if the step needs a gate.
 *
 * Each band's height comes from graphGeometry rather than from CSS, because the
 * port dots are placed by arithmetic in that same module. If a band could grow
 * to fit its text, the dots would slide out from under their own labels (and
 * away from the curves aiming at them), which is exactly what happened when
 * the labels were free-floating over the summary.
 *
 * Positioned with `transform: translate3d` rather than `left`/`top` — during a
 * drag this changes every pointer move, and a transform is composited without
 * re-running layout for the whole canvas, which is the difference between a
 * drag that tracks the cursor and one that stutters behind it.
 */

const RISK_TONE: Record<string, 'danger' | 'warn' | 'neutral' | 'ok'> = {
  send: 'danger',
  write: 'warn',
  export: 'warn',
  read_sensitive: 'neutral',
  read_low: 'ok',
}

export interface WorkflowNodeProps {
  node: GraphNode
  catalog: CatalogIndex
  tool: WorkflowGraphCatalogTool | undefined
  selected: boolean
  /** Reachable by a send-risk path with no approval gate — outlined in red,
   *  mirroring what the server's own validation would reject. */
  needsGate: boolean
  /** Required input slots this node has no value for yet — outlined in red,
   *  same treatment as `needsGate`, stacked alongside it if both apply. */
  missingRequired: InputSlot[]
  /** While a connection is being dragged: which of THIS node's input slots
   *  would actually be accepted if dropped on now (self-loop/cycle/binding-
   *  kind already ruled out) — undefined/empty when none are, so a wire in
   *  flight only lights up ports that would really connect. */
  dropTargetArgs: Set<string> | undefined
  onPointerDownNode: (e: ReactPointerEvent, nodeId: string) => void
  onPointerDownOutPort: (e: ReactPointerEvent, nodeId: string, pin: string) => void
  /** Grabbing a wired input picks that connection's end up so it can be moved
   *  elsewhere or dropped into space to remove it. */
  onPointerDownInPort: (e: ReactPointerEvent, nodeId: string, arg: string) => void
  onEdit: (nodeId: string) => void
  onDelete: (nodeId: string) => void
}

function WorkflowNode({
  node,
  catalog,
  tool,
  selected,
  needsGate,
  missingRequired,
  dropTargetArgs,
  onPointerDownNode,
  onPointerDownOutPort,
  onPointerDownInPort,
  onEdit,
  onDelete,
}: WorkflowNodeProps) {
  const { x, y } = nodePosition(node)
  const inputs = inputSlotsFor(node, catalog)
  const outputs = outputPinsFor(node, catalog)
  const isTrigger = node.kind === 'trigger'
  const risk = tool?.riskLevel
  const summary = isTrigger ? null : stepSummary(node, tool)
  const triggerSummary = outputs.length ? `Asks for: ${outputs.join(', ')}` : 'Takes no inputs'
  const rows = portRowCount(node, catalog)
  const label = nodeLabel(node, catalog)

  return (
    <div
      className="wfc-node"
      data-selected={selected || undefined}
      data-needs-gate={needsGate || undefined}
      data-missing-required={missingRequired.length > 0 || undefined}
      data-kind={node.kind}
      style={{
        transform: `translate3d(${x}px, ${y}px, 0)`,
        width: NODE_W,
        height: nodeHeight(node, catalog, (needsGate ? 1 : 0) + (missingRequired.length > 0 ? 1 : 0)),
      }}
      onPointerDown={(e) => onPointerDownNode(e, node.nodeId)}
    >
      <div className="wfc-node-head" style={{ height: HEAD_H }}>
        <span className="wfc-node-icon" aria-hidden="true">
          {isTrigger ? '▶' : stepIcon(node.kind, tool)}
        </span>
        <span className="wfc-node-title" title={label}>
          {label}
        </span>
        {risk && (
          <span className="wfc-node-head-right">
            <Badge tone={RISK_TONE[risk] ?? 'neutral'} subtle>
              {risk.replace(/_/g, ' ')}
            </Badge>
          </span>
        )}
      </div>

      {/* What the step is actually SET TO, not its type again — the type is
          already the title. `stepSummary` returns null when nothing's
          configured yet, which is worth saying explicitly. */}
      <div
        className="wfc-node-body"
        style={{ height: BODY_H }}
        data-unset={!isTrigger && summary == null ? '' : undefined}
      >
        {isTrigger ? triggerSummary : (summary ?? 'Not set up yet — open to fill in its values')}
      </div>

      {rows > 0 && (
        <div className="wfc-node-ports" style={{ height: portsBandHeight(rows) }}>
          {inputs.map((slot, i) => {
            const binding = node.inputBindings?.[slot.name]
            // "Wired" (has a line to another node) drives the dot's filled
            // look and whether grabbing it detaches a connection. "Filled"
            // (wired OR a literal that was actually typed in) is what the
            // required-slot asterisk should track instead -- a filled-in
            // literal has no wire, but it isn't missing a value either.
            const wired = bindingSourceNodeId(binding) != null
            const filled = isArgFilled(binding)
            return (
              <div
                key={slot.name}
                className="wfc-port-row wfc-port-row--in"
                style={{ top: PORT_TOP + i * PORT_GAP, height: PORT_GAP }}
              >
                <button
                  type="button"
                  className="wfc-port wfc-port--in"
                  data-in-port={slot.name}
                  data-node-id={node.nodeId}
                  data-bound={wired || undefined}
                  data-droppable={dropTargetArgs?.has(slot.name) || undefined}
                  data-no-drag
                  title={
                    wired
                      ? `${slot.label} — drag this dot away to disconnect`
                      : `${slot.label}${slot.required ? ' (required)' : ''}`
                  }
                  aria-label={`Input ${slot.name}`}
                  tabIndex={-1}
                  onPointerDown={(e) => onPointerDownInPort(e, node.nodeId, slot.name)}
                />
                <span className="wfc-port-label" data-bound={wired || undefined}>
                  {slot.name}
                  {slot.required && !filled ? <i aria-hidden="true"> *</i> : null}
                </span>
              </div>
            )
          })}

          {outputs.map((pin, i) => (
            <div
              key={pin}
              className="wfc-port-row wfc-port-row--out"
              style={{ top: PORT_TOP + i * PORT_GAP, height: PORT_GAP }}
            >
              <span className="wfc-port-label">{pin}</span>
              <button
                type="button"
                className="wfc-port wfc-port--out"
                data-no-drag
                title={`${pin} — drag to an input to connect`}
                aria-label={`Output ${pin}, drag to connect`}
                tabIndex={-1}
                onPointerDown={(e) => onPointerDownOutPort(e, node.nodeId, pin)}
              />
            </div>
          ))}
        </div>
      )}

      {needsGate && (
        <div className="wfc-node-warn" style={{ height: WARN_H }}>
          Needs an approval gate before this
        </div>
      )}

      {missingRequired.length > 0 && (
        <div className="wfc-node-warn" style={{ height: WARN_H }} title={`Missing: ${missingRequired.map((s) => s.label).join(', ')}`}>
          Missing required value: {missingRequired.map((s) => s.label).join(', ')}
        </div>
      )}

      {/* Hover/focus actions. Not in the header flow so they never displace the
          title, and `data-no-drag` so grabbing one doesn't start a node drag.
          The trigger has neither: its inputs are edited in the "What this
          workflow needs" card, and a graph must always have exactly one
          trigger, so there is nothing to delete. */}
      {!isTrigger && (
        <div className="wfc-node-actions" data-no-drag>
          <button
            type="button"
            className="wfc-node-btn"
            title="Edit this step"
            aria-label={`Edit ${label}`}
            onClick={() => onEdit(node.nodeId)}
          >
            <svg viewBox="0 0 24 24" width="12" height="12" aria-hidden="true">
              <path
                d="M4 20h4L20 8l-4-4L4 16v4z"
                fill="none"
                stroke="currentColor"
                strokeWidth="2"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            </svg>
          </button>
          <button
            type="button"
            className="wfc-node-btn wfc-node-btn--danger"
            title="Delete this step"
            aria-label={`Delete ${label}`}
            onClick={() => onDelete(node.nodeId)}
          >
            <svg viewBox="0 0 24 24" width="12" height="12" aria-hidden="true">
              <path d="M6 6l12 12M18 6L6 18" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" />
            </svg>
          </button>
        </div>
      )}

      <span className="ui-sr-only">
        {label}. {inputs.length} input{inputs.length === 1 ? '' : 's'}, {outputs.length} output
        {outputs.length === 1 ? '' : 's'}.
        {needsGate ? ' Needs an approval gate before it.' : ''}
        {missingRequired.length > 0 ? ` Missing required value: ${missingRequired.map((s) => s.label).join(', ')}.` : ''}
      </span>
    </div>
  )
}

export default WorkflowNode
