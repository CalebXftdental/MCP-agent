import { createPortal } from 'react-dom'
import { useIconReveal } from './useIconReveal'
import './WorkflowCatalogTile.css'
import './WorkflowInfoIcon.css'

/**
 * The generic form of `WorkflowInfoIcon` — a small circular "i" that reveals
 * arbitrary text on hover/focus, for a section explainer that would
 * otherwise sit as a permanent subtitle under a Card's title (e.g. "My
 * Workflow"'s one-time feature blurb). `WorkflowInfoIcon` stays a separate,
 * template-specific component (description + outputs + disabled reason,
 * several lines) rather than being rewritten on top of this one, since this
 * one is deliberately single-purpose: one plain string, nothing structured.
 */

export interface InfoHoverIconProps {
  text: string
  /** Accessible name for the icon button itself — say what it's about, e.g.
   *  "About My Workflow". */
  label?: string
}

function InfoHoverIcon({ text, label = 'More info' }: InfoHoverIconProps) {
  const { ref, style, show, hide } = useIconReveal<HTMLSpanElement>()

  return (
    <span ref={ref} className="wf-info-wrap" onMouseEnter={show} onMouseLeave={hide}>
      <button type="button" className="wf-info-btn" aria-label={label} onFocus={show} onBlur={hide}>
        i
      </button>

      {style &&
        createPortal(
          <div className="wf-tile-overlay" style={style} aria-hidden="true">
            <p className="wf-tile-overlay-desc">{text}</p>
          </div>,
          document.body,
        )}
    </span>
  )
}

export default InfoHoverIcon
