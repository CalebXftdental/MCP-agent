import './Spinner.css'

/** Indeterminate progress. Two shapes, one for each thing the old UI used:
 *  a ring for "this panel is loading", dots for "the assistant is thinking". */

export interface SpinnerProps {
  size?: 'sm' | 'md' | 'lg'
  /** Announced while busy. Pass null only when a visible label already says it. */
  label?: string | null
  /** Centres itself in the available space with breathing room. */
  center?: boolean
}

export function Spinner({ size = 'md', label = 'Loading…', center = false }: SpinnerProps) {
  return (
    <span
      className={`ui-spinner-wrap ${center ? 'ui-spinner-wrap--center' : ''}`}
      role="status"
      aria-live="polite"
    >
      <span className={`ui-spinner ui-spinner--${size}`} aria-hidden="true" />
      {label && <span className="ui-sr-only">{label}</span>}
    </span>
  )
}

export interface TypingDotsProps {
  label?: string
}

/** The assistant's three-dot pending state, keyframes carried over from the
 *  legacy `.typing` rule so the chat rhythm is unchanged. */
export function TypingDots({ label = 'Assistant is replying' }: TypingDotsProps) {
  return (
    <span className="ui-typing" role="status" aria-live="polite">
      <i aria-hidden="true" />
      <i aria-hidden="true" />
      <i aria-hidden="true" />
      <span className="ui-sr-only">{label}</span>
    </span>
  )
}

export default Spinner
