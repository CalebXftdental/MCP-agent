import type { ReactNode } from 'react'
import './Switch.css'

/**
 * A boolean toggle rendered as a single row: label (+ optional description)
 * on the left, a sliding track on the right. First needed by the Security
 * page's break-glass controls, which the legacy UI rendered as bare
 * `&lt;input type=checkbox&gt;`s — fine for a settings form, but "block this MCP"
 * reads as a more consequential action than a checkbox communicates.
 *
 * The whole row is one `<button role="switch">`, not a `<label>` wrapping a
 * separate control: a `<label>` only forwards clicks to a native form
 * control nested inside it, not to an arbitrary button, so wrapping one
 * around a styled button+track would leave the label text unclickable.
 * One focusable element also means one Tab stop and one thing a screen
 * reader announces per row, not two.
 */

export interface SwitchProps {
  checked: boolean
  onChange: (checked: boolean) => void
  label: ReactNode
  /** Second line under the label, e.g. what flipping it actually does. */
  description?: ReactNode
  disabled?: boolean
  /** Track colour when on. `danger` for a switch that blocks or pauses
   *  something — the same red a Badge would use for that state. */
  tone?: 'accent' | 'danger'
  /** `sm` for a dense list of many switches (per-backend rows). */
  size?: 'sm' | 'md'
}

function Switch({
  checked,
  onChange,
  label,
  description,
  disabled = false,
  tone = 'accent',
  size = 'md',
}: SwitchProps) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      className={`ui-switch-row ui-switch-row--${size}`}
      data-on={checked || undefined}
      disabled={disabled}
      onClick={() => onChange(!checked)}
    >
      <span className="ui-switch-text">
        <span className="ui-switch-label">{label}</span>
        {description && <span className="ui-switch-desc">{description}</span>}
      </span>
      <span className={`ui-switch ui-switch--${tone}`} aria-hidden="true">
        <span className="ui-switch-thumb" />
      </span>
    </button>
  )
}

export default Switch
