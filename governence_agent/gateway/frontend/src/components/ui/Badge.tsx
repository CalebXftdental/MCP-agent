import type { HTMLAttributes, ReactNode } from 'react'
import { severityTone, type BadgeTone } from './scales'
import './Badge.css'

/**
 * The status pill — 81 uses in the old dashboard — and the 4px severity stripe
 * that fronts each alert row.
 *
 * One deliberate difference from the legacy `.pill`: there, `.warn` was styled
 * with the error red and `.pending` carried the amber, so `denied` and
 * `critical` rendered identically to a genuine warning. Tones here are named for
 * meaning — `danger` is red, `warn` is amber — which does mean a legacy
 * `.pill.warn` maps to `tone="danger"`, not `tone="warn"`.
 */

export interface BadgeProps extends HTMLAttributes<HTMLSpanElement> {
  tone?: BadgeTone
  /** Leading dot. Carries the tone as shape, not only as colour. */
  dot?: boolean
  /** Expanding ring on the dot — for genuinely live state only. */
  pulse?: boolean
  /** Outline instead of tinted fill, for dense tables where fills stack up. */
  subtle?: boolean
  children?: ReactNode
}

export function Badge({
  tone = 'neutral',
  dot = false,
  pulse = false,
  subtle = false,
  children,
  className = '',
  ...rest
}: BadgeProps) {
  return (
    <span
      {...rest}
      className={`ui-badge ui-badge--${tone} ${subtle ? 'ui-badge--subtle' : ''} ${className}`}
    >
      {dot && (
        <span className={`ui-badge-dot ${pulse ? 'ui-badge-dot--pulse' : ''}`} aria-hidden="true" />
      )}
      {children}
    </span>
  )
}

export interface SeverityBarProps {
  severity: string
  /** Announced to screen readers, which can't see the stripe at all. */
  label?: string
}

export function SeverityBar({ severity, label }: SeverityBarProps) {
  return (
    <span
      className={`ui-sevbar ui-sevbar--${severityTone(severity)}`}
      role={label ? 'img' : 'presentation'}
      aria-label={label}
    />
  )
}

export default Badge
