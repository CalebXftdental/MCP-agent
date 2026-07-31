import { createPortal } from 'react-dom'
import type { ReactNode } from 'react'
import { Badge } from '../ui'
import { useCardReveal } from '../access/useCardReveal'
import type { WorkflowTemplate } from '../../lib/api'
import './WorkflowCatalogTile.css'

/**
 * One workflow in the catalog grid — a compact tile at rest (icon, name,
 * status dot only), with the description/outputs/disabled-reason revealed on
 * hover or keyboard focus via the same portal-overlay pattern the Access
 * picker's tool cards use (`useCardReveal`) rather than showing everything at
 * once, which read as too much text per tile packed into a dense grid.
 *
 * The admin Disable/Enable action is deliberately NOT inside that hover
 * overlay: the overlay is `pointer-events: none` (informational-only, same
 * as ToolDetailOverlay) because it's portaled outside this tile's own DOM
 * subtree — moving the pointer onto it would fire this tile's `onMouseLeave`
 * and dismiss it before a click could land. It's a plain CSS-opacity reveal
 * on the tile itself instead, which has no such problem.
 */

const OUTPUT_ICONS: Record<string, string> = {
  xlsx: '📊',
  pptx: '📽️',
  docx: '📝',
  pdf: '📄',
  email_draft: '✉️',
  calendar_invite: '📅',
}

function templateIcon(outputTypes: string[]): string {
  for (const type of outputTypes) {
    const icon = OUTPUT_ICONS[type]
    if (icon) return icon
  }
  return '⚙️'
}

export interface WorkflowCatalogTileProps {
  template: WorkflowTemplate
  selected: boolean
  onSelect: (template: WorkflowTemplate) => void
  /** Rendered as a small always-there-on-hover affordance, outside the
   *  portaled detail overlay — see the module note above for why. */
  adminActions?: ReactNode
}

function WorkflowCatalogTile({ template, selected, onSelect, adminActions }: WorkflowCatalogTileProps) {
  const { ref, style, show, hide } = useCardReveal<HTMLDivElement>()
  const disabled = template.status !== 'active'

  const activate = () => {
    if (disabled) return
    onSelect(template)
  }

  return (
    <div ref={ref} className="wf-tile-wrap" onMouseEnter={show} onMouseLeave={hide}>
      <div
        className="wf-tile"
        role="button"
        tabIndex={0}
        aria-pressed={selected}
        aria-disabled={disabled || undefined}
        data-selected={selected || undefined}
        data-disabled={disabled || undefined}
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
          {templateIcon(template.outputTypes)}
        </span>
        <span className="wf-tile-title">{template.displayName}</span>
        <Badge tone={template.status === 'active' ? 'ok' : 'neutral'} subtle dot>
          {template.status}
        </Badge>
        <span className="ui-sr-only">
          {template.description} Outputs: {template.outputTypes.join(', ') || 'none'}.
          {disabled && template.disabledReason ? ` Disabled: ${template.disabledReason}.` : ''}
        </span>
      </div>

      {adminActions && <div className="wf-tile-admin">{adminActions}</div>}

      {style &&
        createPortal(
          <div className="wf-tile-overlay" style={style} aria-hidden="true">
            <p className="wf-tile-overlay-desc">{template.description}</p>
            <p className="wf-tile-overlay-outputs">Outputs: {template.outputTypes.length ? template.outputTypes.join(', ') : 'none'}</p>
            {disabled && template.disabledReason && <p className="wf-tile-overlay-reason">Disabled: {template.disabledReason}</p>}
          </div>,
          document.body,
        )}
    </div>
  )
}

export default WorkflowCatalogTile
