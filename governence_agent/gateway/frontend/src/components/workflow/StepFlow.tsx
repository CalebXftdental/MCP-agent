import { Badge } from '../ui'
import { stepIcon, stepKindLabel, stepSummary } from './stepMeta'
import type { GraphNode, WorkflowGraphCatalogTool } from '../../lib/api'
import './StepFlow.css'

/**
 * The workflow as a top-to-bottom flow of connected nodes — the legacy
 * canvas's visual "these steps feed into each other" reading, without its
 * free-positioned drag/dot-connect editing model (see MyWorkflowsPage's own
 * module note on why the linear chain replaced the graph canvas).
 *
 * Each node is collapsed to icon + name + a one-line summary of what it's set
 * to (`stepSummary`); configuring it is an explicit "open this step" action
 * via the pencil that appears on hover/focus, not a permanently-expanded form
 * per node. Reorder and remove sit in the same hover group.
 *
 * The hover controls are plain CSS opacity on a DOM child (not a portal), so
 * unlike the tile-detail overlays elsewhere in this folder they stay
 * clickable — moving the pointer onto them keeps it inside the node.
 */

export interface StepFlowProps {
  steps: GraphNode[]
  catalogByTool: Record<string, WorkflowGraphCatalogTool>
  /** Names of this workflow's declared inputs, for the trigger node's summary. */
  triggerInputLabels: string[]
  onEdit: (nodeId: string) => void
  onMove: (nodeId: string, dir: -1 | 1) => void
  onRemove: (nodeId: string) => void
  onInsertApprovalBefore: (nodeId: string) => void
}

function StepFlow({
  steps,
  catalogByTool,
  triggerInputLabels,
  onEdit,
  onMove,
  onRemove,
  onInsertApprovalBefore,
}: StepFlowProps) {
  return (
    <div className="step-flow">
      <div className="step-flow-node step-flow-node--trigger">
        <span className="step-flow-icon" aria-hidden="true">
          ▶
        </span>
        <span className="step-flow-text">
          <span className="step-flow-title">When this workflow runs</span>
          <span className="step-flow-summary">
            {triggerInputLabels.length
              ? `Asks for: ${triggerInputLabels.join(', ')}`
              : 'Takes no inputs'}
          </span>
        </span>
      </div>

      {steps.map((step, i) => {
        const tool = step.kind === 'tool_call' ? catalogByTool[step.tool] : undefined
        const needsGate = tool?.riskLevel === 'send' && !steps.slice(0, i).some((s) => s.kind === 'approval_gate')
        const summary = stepSummary(step, tool)
        const unknownTool = step.kind === 'tool_call' && !tool

        return (
          <div key={step.nodeId} className="step-flow-item">
            <span className="step-flow-connector" aria-hidden="true" />

            <div className="step-flow-node" data-needs-gate={needsGate || undefined}>
              <span className="step-flow-index">{i + 1}</span>
              <span className="step-flow-icon" aria-hidden="true">
                {stepIcon(step.kind, tool)}
              </span>
              <span className="step-flow-text">
                <span className="step-flow-title">{stepKindLabel(step.kind, step.tool)}</span>
                {unknownTool ? (
                  <span className="step-flow-summary step-flow-summary--warn">This tool is no longer available</span>
                ) : summary ? (
                  <span className="step-flow-summary">{summary}</span>
                ) : (
                  <span className="step-flow-summary step-flow-summary--muted">Not set up yet — open it to fill in its values</span>
                )}
              </span>

              {needsGate && (
                <Badge tone="danger" subtle>
                  Needs approval before it
                </Badge>
              )}

              <span className="step-flow-controls">
                <button type="button" className="step-flow-btn" onClick={() => onEdit(step.nodeId)} aria-label={`Edit ${stepKindLabel(step.kind, step.tool)}`} title="Edit">
                  ✎
                </button>
                <button type="button" className="step-flow-btn" disabled={i === 0} onClick={() => onMove(step.nodeId, -1)} aria-label="Move up" title="Move up">
                  ↑
                </button>
                <button
                  type="button"
                  className="step-flow-btn"
                  disabled={i === steps.length - 1}
                  onClick={() => onMove(step.nodeId, 1)}
                  aria-label="Move down"
                  title="Move down"
                >
                  ↓
                </button>
                <button
                  type="button"
                  className="step-flow-btn step-flow-btn--danger"
                  onClick={() => onRemove(step.nodeId)}
                  aria-label={`Remove ${stepKindLabel(step.kind, step.tool)}`}
                  title="Remove"
                >
                  ✕
                </button>
              </span>
            </div>

            {needsGate && (
              <button type="button" className="step-flow-gate-fix" onClick={() => onInsertApprovalBefore(step.nodeId)}>
                + Insert an approval gate before this step
              </button>
            )}
          </div>
        )
      })}
    </div>
  )
}

export default StepFlow
