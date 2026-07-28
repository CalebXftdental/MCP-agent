import { createPortal } from 'react-dom'
import { Badge, Card } from '../ui'
import type { ToolInfo } from '../../lib/api'
import { RISK_META, humanize } from './toolCardShared'
import { useCardReveal } from './useCardReveal'
import ToolDetailOverlay from './ToolDetailOverlay'
import './AccessCard.css'

/**
 * One granted tool, as its own card — replaces the flat `<ul>` of description
 * lines the legacy "Granted access" panel used.
 *
 * At rest a card shows only the humanized name and its sensitivity pill (in
 * the header, via Card's `actions` slot), so a whole backend's grant scans at
 * a glance instead of reading as a wall of sentences. The full description and
 * the raw MCP tool name reveal on hover or keyboard focus via a portal (see
 * useCardReveal) without changing the card's own box size.
 *
 * A screen reader can't hover, so the full description and tool name are ALSO
 * always present as plain (non-portal, `ui-sr-only`) text inside the card.
 */

export interface AccessCardProps {
  tool: ToolInfo
}

function AccessCard({ tool }: AccessCardProps) {
  const risk = tool.risk ? RISK_META[tool.risk] : undefined
  const { ref, style, show, hide } = useCardReveal<HTMLDivElement>()

  const detailText = [tool.description, `MCP method: ${tool.name}.`, tool.approvalRequired ? 'Needs approval.' : '']
    .filter(Boolean)
    .join(' ')

  return (
    <div ref={ref} className="access-card-wrap" onMouseEnter={show} onMouseLeave={hide}>
      <Card
        className="access-card"
        title={humanize(tool.name)}
        tabIndex={0}
        onFocus={show}
        onBlur={hide}
        actions={
          risk && (
            <Badge tone={risk.tone} subtle>
              {risk.label}
            </Badge>
          )
        }
      >
        <span className="ui-sr-only">{detailText}</span>
      </Card>

      {style && createPortal(<ToolDetailOverlay tool={tool} style={style} />, document.body)}
    </div>
  )
}

export default AccessCard
