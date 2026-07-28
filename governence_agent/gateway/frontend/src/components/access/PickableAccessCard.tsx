import { createPortal } from 'react-dom'
import { Badge, Card } from '../ui'
import type { ToolInfo } from '../../lib/api'
import { RISK_META, humanize } from './toolCardShared'
import { useCardReveal } from './useCardReveal'
import ToolDetailOverlay from './ToolDetailOverlay'
import './PickableAccessCard.css'

/**
 * The clickable counterpart to AccessCard, for the "Request more access"
 * picker: same at-a-glance title + risk pill + hover/focus detail reveal
 * (shared via useCardReveal/ToolDetailOverlay), but the whole card is a toggle
 * button instead of read-only.
 *
 * Selecting one hands its own DOM node up to the caller (`onToggle`) rather
 * than reaching into any tray/animation concern itself — this component only
 * knows "pick" vs "not picked"; the request picker decides what a selection
 * looks like once it leaves here (the fly-to-tray animation measures the same
 * node this hands back).
 */

export interface PickableAccessCardProps {
  tool: ToolInfo
  selected: boolean
  /** Already asked for in a not-yet-decided request — shown, not pickable. */
  pending?: boolean
  /** The request cap was hit and this tool isn't one of the already-selected
   *  ones, so picking it is blocked until something else is dropped. */
  atCap?: boolean
  onToggle: (tool: ToolInfo, cardEl: HTMLDivElement) => void
}

function PickableAccessCard({ tool, selected, pending = false, atCap = false, onToggle }: PickableAccessCardProps) {
  const { ref, style, show, hide } = useCardReveal<HTMLDivElement>()
  const risk = tool.risk ? RISK_META[tool.risk] : undefined
  const disabled = pending || (atCap && !selected)

  const activate = () => {
    if (disabled || !ref.current) return
    onToggle(tool, ref.current)
  }

  const note = pending ? 'Already requested — pending review.' : atCap && !selected ? 'Drop another pick first — request cap reached.' : undefined

  const detailText = [tool.description, `MCP method: ${tool.name}.`, note ?? (tool.approvalRequired ? 'Needs approval.' : '')]
    .filter(Boolean)
    .join(' ')

  return (
    <div ref={ref} className="pick-card-wrap" onMouseEnter={show} onMouseLeave={hide}>
      <Card
        className="pick-card"
        title={humanize(tool.name)}
        tabIndex={0}
        role="button"
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
        actions={
          <span className="pick-card-actions">
            {risk && (
              <Badge tone={risk.tone} subtle>
                {risk.label}
              </Badge>
            )}
            <span className="pick-card-check" aria-hidden="true">
              ✓
            </span>
          </span>
        }
      >
        <span className="ui-sr-only">{detailText}</span>
      </Card>

      {style && createPortal(<ToolDetailOverlay tool={tool} style={style} note={note} />, document.body)}
    </div>
  )
}

export default PickableAccessCard
