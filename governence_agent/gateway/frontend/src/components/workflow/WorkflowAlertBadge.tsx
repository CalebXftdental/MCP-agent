import { createPortal } from 'react-dom'
import { useIconReveal } from './useIconReveal'
import './WorkflowCatalogTile.css'
import './WorkflowAlertBadge.css'

/**
 * A small circular red count, for a section title — replaces the old
 * always-visible "Needs attention before this can run" block + bulleted
 * blocker list that used to sit in the form body. Hovering or focusing it
 * reveals every blocker, one per line, via `useIconReveal` (a fixed-width
 * variant of the catalog tiles' `useCardReveal` overlay — a plain
 * `useCardReveal` sizes the overlay to the TRIGGER's own width, which for a
 * ~17px circular badge means every word wraps onto its own line), so the
 * count itself stays a single compact glyph next to the title rather than a
 * wall of red text.
 *
 * Renders nothing when there's nothing to flag — a page decides whether to
 * mount this at all based on `preflight.ready`, so an empty `blockers` array
 * here would only happen from a caller wiring mistake, not a real state.
 */

export interface WorkflowAlertBadgeProps {
  blockers: string[]
}

function WorkflowAlertBadge({ blockers }: WorkflowAlertBadgeProps) {
  const { ref, style, show, hide } = useIconReveal<HTMLSpanElement>()
  if (blockers.length === 0) return null

  return (
    <span ref={ref} className="wf-alert-wrap" onMouseEnter={show} onMouseLeave={hide}>
      <button
        type="button"
        className="wf-alert-btn"
        aria-label={`${blockers.length} thing${blockers.length === 1 ? '' : 's'} need attention before this can run`}
        onFocus={show}
        onBlur={hide}
      >
        {blockers.length}
      </button>

      {style &&
        createPortal(
          <div className="wf-tile-overlay wf-alert-overlay" style={style} aria-hidden="true">
            {blockers.map((b, i) => (
              <p key={i} className="wf-alert-overlay-line">
                {b}
              </p>
            ))}
          </div>,
          document.body,
        )}
    </span>
  )
}

export default WorkflowAlertBadge
