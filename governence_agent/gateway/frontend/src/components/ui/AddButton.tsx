import PlusIcon from './PlusIcon'
import './AddButton.css'

/**
 * The circular "+" trigger for "add a new X" — first built for Consumers,
 * now shared so a second admin list (Categories, and whatever comes after
 * it) gets the exact same affordance instead of a re-styled one-off button.
 * Icon is `PlusIcon` (SVG, not a text glyph) and the circle itself never
 * scales on hover — both fixed after an earlier version of this button
 * visibly "drifted" the plus sign on hover.
 */

export interface AddButtonProps {
  onClick: () => void
  /** Accessible name and native tooltip — say what it adds, e.g. "Add a new consumer". */
  label: string
  disabled?: boolean
  size?: 'sm' | 'md'
}

function AddButton({ onClick, label, disabled = false, size = 'md' }: AddButtonProps) {
  return (
    <button
      type="button"
      className={`ui-add-btn ui-add-btn--${size}`}
      onClick={onClick}
      disabled={disabled}
      aria-label={label}
      title={label}
    >
      <PlusIcon size={size === 'sm' ? '0.85rem' : '1.05rem'} />
    </button>
  )
}

export default AddButton
