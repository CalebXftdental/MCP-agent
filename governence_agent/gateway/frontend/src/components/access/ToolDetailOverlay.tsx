import type { CSSProperties } from 'react'
import { Badge } from '../ui'
import type { ToolInfo } from '../../lib/api'
import './ToolDetailOverlay.css'

/**
 * The hover/focus reveal portal content — chrome shared by every tool-card
 * variant. Positioning comes from the caller (`useCardReveal`'s measured
 * `style`); this only supplies what's inside it. `aria-hidden`: a screen
 * reader can't hover, so the full description already lives as plain
 * `ui-sr-only` text inside the triggering card — this is a sighted-only
 * duplicate of that same content.
 */

export interface ToolDetailOverlayProps {
  tool: ToolInfo
  style: CSSProperties
  /** Extra line for a state the description alone doesn't cover, e.g. "already
   *  requested — pending review" in the request picker. */
  note?: string
}

function ToolDetailOverlay({ tool, style, note }: ToolDetailOverlayProps) {
  return (
    <div className="access-card-overlay" style={style} aria-hidden="true">
      <p className="access-card-desc">{tool.description}</p>
      <p className="access-card-method">
        <span className="ui-eyebrow">MCP method</span>
        <code>{tool.name}</code>
      </p>
      {tool.approvalRequired && (
        <Badge tone="warn" dot>
          Needs approval
        </Badge>
      )}
      {note && <p className="access-card-note">{note}</p>}
    </div>
  )
}

export default ToolDetailOverlay
