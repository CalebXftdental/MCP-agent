import type { ReactNode } from 'react'
import './Chip.css'

/**
 * The pill-shaped action from the assistant's quick-ask row (`.qa-chip`). Unlike
 * Badge, this one is pressable.
 *
 * The legacy version nested its delete `<button>` inside the chip button, which
 * is invalid HTML and made the ✕ unreachable by keyboard. Here the chip and its
 * remove control are siblings inside a wrapper, so both are tabbable.
 */

export interface ChipProps {
  children: ReactNode
  onClick?: () => void
  /** Renders a ✕ affordance. Omit for a plain chip. */
  onRemove?: () => void
  /** What the ✕ removes, for the screen-reader label. */
  removeLabel?: string
  icon?: ReactNode
  selected?: boolean
  disabled?: boolean
  title?: string
  /** `sm` for dense rows of many chips — suggestion lists, filter bars. */
  size?: 'sm' | 'md'
  /** Caps the chip's width; the label ellipsises past it. Any CSS length. Use
   *  when the text is arbitrary length and the row must stay scannable. */
  maxWidth?: string
}

function Chip({
  children,
  onClick,
  onRemove,
  removeLabel,
  icon,
  selected = false,
  disabled = false,
  title,
  size = 'md',
  maxWidth,
}: ChipProps) {
  return (
    <span
      className={`ui-chip-wrap ui-chip-wrap--${size} ${onRemove ? 'ui-chip-wrap--removable' : ''}`}
      style={maxWidth ? { maxWidth } : undefined}
    >
      <button
        type="button"
        className="ui-chip"
        onClick={onClick}
        disabled={disabled}
        title={title}
        aria-pressed={onClick && selected ? true : undefined}
        data-selected={selected || undefined}
      >
        {icon && (
          <span className="ui-chip-icon" aria-hidden="true">
            {icon}
          </span>
        )}
        <span className="ui-chip-label">{children}</span>
      </button>
      {onRemove && (
        <button
          type="button"
          className="ui-chip-remove"
          onClick={onRemove}
          disabled={disabled}
          aria-label={removeLabel ?? 'Remove'}
        >
          <span aria-hidden="true">✕</span>
        </button>
      )}
    </span>
  )
}

export default Chip
