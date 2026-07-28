import { useEffect, useRef } from 'react'
import type { ReactNode } from 'react'
import { usePresence } from './motion'
import './Drawer.css'

/**
 * The right-hand detail panel — call detail, alert detail, artifact detail.
 *
 * The legacy version was one shared `<aside id="drawer">` that `openDrawer(html)`
 * filled with an interpolated string. It looked right and behaved like a div:
 * `aria-hidden="true"` was set once in the markup and never cleared, so the
 * contents were hidden from screen readers even while open; nothing moved focus
 * in or out; Escape did nothing; and the page behind it kept scrolling.
 *
 * This one:
 *   - traps Tab inside while open and returns focus to the opener on close,
 *   - closes on Escape and on backdrop click,
 *   - locks background scroll,
 *   - stays mounted for its slide-out, via usePresence.
 *
 * Content is children, so the caller renders React instead of building HTML
 * strings — which also removes the manual `esc()` on every interpolated value.
 */

export interface DrawerProps {
  open: boolean
  onClose: () => void
  /** Small uppercase label above the title — "Call detail", "Incident". */
  eyebrow?: ReactNode
  title: ReactNode
  /** Monospace title, for ids and tool names. */
  monoTitle?: boolean
  /** Row under the header — status badges, actions. */
  meta?: ReactNode
  /** Pinned action strip at the bottom. */
  footer?: ReactNode
  width?: string
  children: ReactNode
}

const FOCUSABLE =
  'a[href], button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex]:not([tabindex="-1"])'

function Drawer({
  open,
  onClose,
  eyebrow,
  title,
  monoTitle = false,
  meta,
  footer,
  width = '27rem',
  children,
}: DrawerProps) {
  const { mounted, state } = usePresence(open, 260)
  const panelRef = useRef<HTMLElement>(null)
  const openerRef = useRef<Element | null>(null)

  // `mounted` is a dependency because the panel does not exist on the render
  // where `open` first flips true — usePresence mounts it one commit later, and
  // an effect that ran before that would capture a null ref and silently trap
  // nothing.
  useEffect(() => {
    if (!open || !mounted) return

    openerRef.current = document.activeElement
    // Captured for the cleanup: by then React may have detached the ref, and the
    // focus-restore check below needs the node that was actually open.
    const panelAtOpen = panelRef.current
    // Focus the panel itself, not its first control: landing on a Close button
    // means Enter dismisses what you just opened.
    panelAtOpen?.focus()

    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.stopPropagation()
        onClose()
        return
      }
      const panel = panelRef.current
      if (e.key !== 'Tab' || !panel) return

      const items = [...panel.querySelectorAll<HTMLElement>(FOCUSABLE)].filter(
        (el) => el.offsetParent !== null,
      )
      if (items.length === 0) {
        // Nothing tabbable inside: keep focus on the panel rather than letting
        // Tab escape to the page behind the scrim.
        e.preventDefault()
        panel.focus()
        return
      }

      const first = items[0]
      const last = items[items.length - 1]
      const active = document.activeElement
      // Shift+Tab from the panel itself wraps to the end, so the first Tab press
      // after opening can reach the footer actions directly.
      if (e.shiftKey && (active === first || active === panel)) {
        e.preventDefault()
        last.focus()
      } else if (!e.shiftKey && active === last) {
        e.preventDefault()
        first.focus()
      }
    }

    document.addEventListener('keydown', onKeyDown)

    // Restore rather than clear: the app shell may set its own overflow.
    const previousOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'

    return () => {
      document.removeEventListener('keydown', onKeyDown)
      document.body.style.overflow = previousOverflow
      // Only pull focus back if it's still inside the drawer — if something else
      // took it in the meantime, stealing it back is worse than leaving it.
      if (panelAtOpen?.contains(document.activeElement)) {
        ;(openerRef.current as HTMLElement | null)?.focus?.()
      }
    }
  }, [open, mounted, onClose])

  if (!mounted) return null

  return (
    <>
      <div className="ui-drawer-scrim" data-state={state} onClick={onClose} aria-hidden="true" />
      <aside
        ref={panelRef}
        className="ui-drawer"
        data-state={state}
        style={{ width }}
        role="dialog"
        aria-modal="true"
        aria-label={typeof title === 'string' ? title : undefined}
        tabIndex={-1}
      >
        <header className="ui-drawer-head">
          <div className="ui-drawer-headings">
            {eyebrow && <p className="ui-drawer-eyebrow ui-eyebrow">{eyebrow}</p>}
            <h2 className={`ui-drawer-title ${monoTitle ? 'ui-mono' : ''}`}>{title}</h2>
          </div>
          <button type="button" className="ui-drawer-close" onClick={onClose} aria-label="Close">
            <span aria-hidden="true">✕</span>
          </button>
        </header>

        {meta && <div className="ui-drawer-meta">{meta}</div>}
        <div className="ui-drawer-body">{children}</div>
        {footer && <footer className="ui-drawer-foot">{footer}</footer>}
      </aside>
    </>
  )
}

export default Drawer
