import { createPortal } from 'react-dom'
import { useCardReveal } from '../access/useCardReveal'
import './WorkflowCatalogTile.css'

/**
 * One choice in the "Add a step" picker — a tile at rest (icon + name only),
 * full description revealed on hover/focus via the same portal-overlay
 * pattern `WorkflowCatalogTile` uses (`useCardReveal` — this tile is
 * card-sized, not icon-sized, so the plain hook's "overlay width matches the
 * trigger" behavior is correct here, unlike the tiny `WorkflowInfoIcon`/
 * `WorkflowAlertBadge`, which needed `useIconReveal` instead).
 *
 * A locked tile (a tool the builder doesn't have access to) stays visible
 * rather than disappearing — seeing what exists and why it's unavailable is
 * more informative than a tool silently not being an option at all — but
 * isn't selectable.
 */

export interface StepTypeTileProps {
  icon: string
  title: string
  description: string
  locked?: boolean
  lockedReason?: string
  onSelect: () => void
}

function StepTypeTile({ icon, title, description, locked = false, lockedReason, onSelect }: StepTypeTileProps) {
  const { ref, style, show, hide } = useCardReveal<HTMLDivElement>()

  const activate = () => {
    if (locked) return
    onSelect()
  }

  return (
    <div ref={ref} className="wf-tile-wrap" onMouseEnter={show} onMouseLeave={hide}>
      <div
        className="wf-tile"
        role="button"
        tabIndex={0}
        aria-disabled={locked || undefined}
        data-disabled={locked || undefined}
        onClick={activate}
        onKeyDown={(e) => {
          if (e.key !== 'Enter' && e.key !== ' ') return
          e.preventDefault()
          activate()
        }}
        onFocus={show}
        onBlur={hide}
      >
        <span className="wf-tile-icon" aria-hidden="true">
          {icon}
        </span>
        <span className="wf-tile-title">{title}</span>
        <span className="ui-sr-only">
          {description}
          {locked && lockedReason ? ` ${lockedReason}` : ''}
        </span>
      </div>

      {style &&
        createPortal(
          <div className="wf-tile-overlay" style={style} aria-hidden="true">
            <p className="wf-tile-overlay-desc">{description}</p>
            {locked && lockedReason && <p className="wf-tile-overlay-reason">{lockedReason}</p>}
          </div>,
          document.body,
        )}
    </div>
  )
}

export default StepTypeTile
