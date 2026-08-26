import type { HTMLAttributes, ReactNode } from 'react'
import './PageShell.css'

/**
 * The routed page's outermost box: content measure, page inset, and the
 * vertical rhythm between top-level cards.
 *
 * Every page stylesheet used to open with the same eight lines — flex column,
 * `gap: 1rem`, `padding: 1.5rem 1.75rem 3rem`, a `max-width`, and a 640px
 * padding override. Twenty-two copies, which is how the console ended up with
 * four different content widths (60 / 72 / 80 / 84rem): each copy chose its
 * own, so switching tabs resized the column.
 *
 * Named `PageShell`, not `Page`, because `PageProps` in `pages/types.ts` is
 * already the contract every routed page implements (route, session, navigate).
 * Two different `Page*` types imported into the same file would be a trap.
 *
 * `width` is deliberately two options rather than a free rem value, so the
 * vocabulary can't drift again. If a page seems to need a third, that's a
 * conversation about the page, not a new number.
 *
 * Keep the page's own class as `className` — it's still the ancestor selector
 * for everything inside (`.access-top-row`, `.monitor-toolbar`, …), and page
 * CSS loads after this file, so a page can still override what it must.
 */

export interface PageShellProps extends HTMLAttributes<HTMLDivElement> {
  /** Content measure. `default` (80rem) for data pages; `narrow` (60rem) for a
   *  page that is genuinely one column of prose or form fields. */
  width?: 'default' | 'narrow'
  /** `stack` is the standard column-of-cards rhythm. `bare` drops it for a page
   *  that owns its own `display` (a grid, a centred hero) in its own stylesheet. */
  layout?: 'stack' | 'bare'
  children?: ReactNode
}

export function PageShell({
  width = 'default',
  layout = 'stack',
  className = '',
  children,
  ...rest
}: PageShellProps) {
  const classes = [
    'ui-page',
    width === 'narrow' ? 'ui-page--narrow' : '',
    layout === 'stack' ? 'ui-page--stack' : '',
    className,
  ]
    .filter(Boolean)
    .join(' ')

  return (
    <div {...rest} className={classes}>
      {children}
    </div>
  )
}

export default PageShell
