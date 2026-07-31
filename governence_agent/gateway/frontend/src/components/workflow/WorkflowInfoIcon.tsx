import { createPortal } from 'react-dom'
import { useIconReveal } from './useIconReveal'
import type { WorkflowTemplate } from '../../lib/api'
import './WorkflowCatalogTile.css'
import './WorkflowInfoIcon.css'

/**
 * A small circular "i" beside a section title — hover or focus reveals the
 * selected workflow's description/outputs/disabled-reason via the same
 * portal-overlay pattern the catalog tiles use (`useIconReveal`, a fixed-width
 * variant of `useCardReveal`), so that detail doesn't have to sit as
 * permanent text under the title once a workflow is picked. Reuses the
 * tile's own overlay styling (`.wf-tile-overlay*` from WorkflowCatalogTile.css)
 * rather than a second copy.
 */

export interface WorkflowInfoIconProps {
  template: WorkflowTemplate
}

function WorkflowInfoIcon({ template }: WorkflowInfoIconProps) {
  const { ref, style, show, hide } = useIconReveal<HTMLSpanElement>()
  const disabled = template.status !== 'active'

  return (
    <span ref={ref} className="wf-info-wrap" onMouseEnter={show} onMouseLeave={hide}>
      <button
        type="button"
        className="wf-info-btn"
        aria-label={`About ${template.displayName}`}
        onFocus={show}
        onBlur={hide}
      >
        i
      </button>

      {style &&
        createPortal(
          <div className="wf-tile-overlay" style={style} aria-hidden="true">
            <p className="wf-tile-overlay-desc">{template.description}</p>
            <p className="wf-tile-overlay-outputs">Outputs: {template.outputTypes.length ? template.outputTypes.join(', ') : 'none'}</p>
            {disabled && template.disabledReason && <p className="wf-tile-overlay-reason">Disabled: {template.disabledReason}</p>}
          </div>,
          document.body,
        )}
    </span>
  )
}

export default WorkflowInfoIcon
