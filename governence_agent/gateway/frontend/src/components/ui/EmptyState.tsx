import type { ReactNode } from 'react'
import './EmptyState.css'

/**
 * The "nothing here yet" state, 69 uses in the old dashboard.
 *
 * Those strings were the best copy in the legacy UI — "No activity yet — ask
 * something above to get started" tells you what to do next, where a bare "No
 * data." doesn't. The `action` slot exists to make that the easy path: say what
 * is missing, then offer the control that fixes it.
 */

export interface EmptyStateProps {
  /** One line, sentence case, no trailing period. */
  title: ReactNode
  /** What to do about it. */
  description?: ReactNode
  /** Decorative glyph. Skipped by assistive tech. */
  icon?: ReactNode
  action?: ReactNode
  /** Fits inside a table cell or a small card — drops padding and the icon. */
  compact?: boolean
}

function EmptyState({ title, description, icon, action, compact = false }: EmptyStateProps) {
  return (
    <div className={`ui-empty ${compact ? 'ui-empty--compact' : ''}`}>
      {icon && !compact && (
        <div className="ui-empty-icon" aria-hidden="true">
          {icon}
        </div>
      )}
      <p className="ui-empty-title">{title}</p>
      {description && <p className="ui-empty-desc">{description}</p>}
      {action && <div className="ui-empty-action">{action}</div>}
    </div>
  )
}

export default EmptyState
