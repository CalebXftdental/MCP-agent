import type { HTMLAttributes, ReactNode } from 'react'
import './Card.css'

/**
 * The panel container used 92 times in the old dashboard, and the section
 * heading that precedes groups of them.
 *
 * `title` / `description` / `actions` replace the `<div class="row"
 * style="justify-content:space-between">` that was hand-written above almost
 * every card — that inline style appeared often enough to be a de facto
 * component already.
 */

export interface CardProps extends Omit<HTMLAttributes<HTMLElement>, 'title'> {
  title?: ReactNode
  description?: ReactNode
  /** Right-aligned controls in the header. */
  actions?: ReactNode
  /** Separated strip along the bottom. */
  footer?: ReactNode
  /** Drops body padding — for a card whose whole body is a table. */
  flush?: boolean
  /** Hover affordance. Use only when the whole card is clickable. */
  interactive?: boolean
  /** Left edge stripe, for state that must read before the text does. */
  accent?: 'none' | 'accent' | 'ok' | 'warn' | 'danger' | 'info'
  children?: ReactNode
}

export function Card({
  title,
  description,
  actions,
  footer,
  flush = false,
  interactive = false,
  accent = 'none',
  children,
  className = '',
  ...rest
}: CardProps) {
  const head = title || description || actions

  return (
    <section
      {...rest}
      className={`ui-card ${interactive ? 'ui-card--interactive' : ''} ${accent !== 'none' ? `ui-card--edge ui-card--edge-${accent}` : ''} ${className}`}
    >
      {head && (
        <header className="ui-card-head">
          <div className="ui-card-headings">
            {title && <h2 className="ui-card-title">{title}</h2>}
            {description && <p className="ui-card-desc">{description}</p>}
          </div>
          {actions && <div className="ui-card-actions">{actions}</div>}
        </header>
      )}
      {children != null && (
        <div className={`ui-card-body ${flush ? 'ui-card-body--flush' : ''}`}>{children}</div>
      )}
      {footer && <footer className="ui-card-foot">{footer}</footer>}
    </section>
  )
}

export interface SectionHeaderProps {
  title: ReactNode
  description?: ReactNode
  actions?: ReactNode
  /** Heading level. Pick by document structure, not by size. */
  level?: 2 | 3
}

export function SectionHeader({ title, description, actions, level = 2 }: SectionHeaderProps) {
  const Heading = level === 2 ? 'h2' : 'h3'
  return (
    <div className="ui-section-head">
      <div className="ui-section-headings">
        <Heading className="ui-section-title">{title}</Heading>
        {description && <p className="ui-section-desc">{description}</p>}
      </div>
      {actions && <div className="ui-section-actions">{actions}</div>}
    </div>
  )
}

export default Card
