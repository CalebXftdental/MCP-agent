import type { ButtonHTMLAttributes, ReactNode } from 'react'
import './Button.css'

/**
 * The four button shapes the old UI actually used — `button`, `.ghost`,
 * `.danger`, and a bare-text variant hand-rolled per panel — plus a real
 * loading state.
 *
 * Loading reserves the label's width instead of swapping the text for a
 * spinner, so a row of buttons doesn't jump when one starts working. That
 * shifting was visible on every Approve/Deny table in the old dashboard.
 */

export type ButtonVariant = 'primary' | 'ghost' | 'danger' | 'quiet'
export type ButtonSize = 'sm' | 'md'

export interface ButtonProps extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, 'className'> {
  variant?: ButtonVariant
  size?: ButtonSize
  /** Shows a spinner and blocks clicks. Keeps the button's width. */
  loading?: boolean
  /** Leading glyph or icon. Hidden from assistive tech — label it in children. */
  icon?: ReactNode
  /** Fills its container. */
  block?: boolean
  children?: ReactNode
  className?: string
}

function Button({
  variant = 'primary',
  size = 'md',
  loading = false,
  icon,
  block = false,
  disabled,
  type = 'button',
  children,
  className = '',
  ...rest
}: ButtonProps) {
  return (
    <button
      {...rest}
      type={type}
      className={`ui-btn ui-btn--${variant} ui-btn--${size} ${block ? 'ui-btn--block' : ''} ${className}`}
      disabled={disabled || loading}
      data-loading={loading || undefined}
      aria-busy={loading || undefined}
    >
      {loading && <span className="ui-btn-spinner" aria-hidden="true" />}
      <span className="ui-btn-content">
        {icon && (
          <span className="ui-btn-icon" aria-hidden="true">
            {icon}
          </span>
        )}
        {children}
      </span>
    </button>
  )
}

export default Button
