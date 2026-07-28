import { useEffect, useRef } from 'react'
import type { ReactNode } from 'react'
import { usePresence } from './motion'
import './Modal.css'

/**
 * A centered overlay dialog — for a focused, single task (create/edit a
 * record) rather than Drawer's "detail alongside the list it came from."
 * Where Drawer slides in from the edge and keeps whatever's behind it
 * legible, Modal dims and centers, which is the right call exactly when
 * there's nothing behind it worth keeping in view — a create form has no
 * "the row this refers to" to stay anchored to.
 *
 * Shares Drawer's accessibility mechanics rather than reinventing them: Tab
 * trapped inside while open, focus restored to the opener on close, Escape
 * and backdrop-click both dismiss, background scroll locked, stays mounted
 * through its exit animation via `usePresence`.
 */

export interface ModalProps {
  open: boolean
  onClose: () => void
  eyebrow?: ReactNode
  title: ReactNode
  footer?: ReactNode
  /** Any CSS length. Defaults to a comfortable single-column form width. */
  width?: string
  children: ReactNode
}

const FOCUSABLE =
  'a[href], button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex]:not([tabindex="-1"])'

function Modal({ open, onClose, eyebrow, title, footer, width = '30rem', children }: ModalProps) {
  const { mounted, state } = usePresence(open, 200)
  const panelRef = useRef<HTMLElement>(null)
  const openerRef = useRef<Element | null>(null)

  // Same rationale as Drawer: `mounted` becomes true one commit after `open`
  // does, so this effect must depend on it too or it captures a null panel.
  useEffect(() => {
    if (!open || !mounted) return

    openerRef.current = document.activeElement
    const panelAtOpen = panelRef.current
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
        e.preventDefault()
        panel.focus()
        return
      }

      const first = items[0]
      const last = items[items.length - 1]
      const active = document.activeElement
      if (e.shiftKey && (active === first || active === panel)) {
        e.preventDefault()
        last.focus()
      } else if (!e.shiftKey && active === last) {
        e.preventDefault()
        first.focus()
      }
    }

    document.addEventListener('keydown', onKeyDown)

    const previousOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'

    return () => {
      document.removeEventListener('keydown', onKeyDown)
      document.body.style.overflow = previousOverflow
      if (panelAtOpen?.contains(document.activeElement)) {
        ;(openerRef.current as HTMLElement | null)?.focus?.()
      }
    }
  }, [open, mounted, onClose])

  if (!mounted) return null

  return (
    <div className="ui-modal-scrim" data-state={state} onClick={onClose}>
      <section
        ref={panelRef}
        className="ui-modal"
        style={{ width }}
        role="dialog"
        aria-modal="true"
        aria-label={typeof title === 'string' ? title : undefined}
        tabIndex={-1}
        onClick={(e) => e.stopPropagation()}
      >
        <header className="ui-modal-head">
          <div className="ui-modal-headings">
            {eyebrow && <p className="ui-modal-eyebrow ui-eyebrow">{eyebrow}</p>}
            <h2 className="ui-modal-title">{title}</h2>
          </div>
          <button type="button" className="ui-modal-close" onClick={onClose} aria-label="Close">
            <span aria-hidden="true">✕</span>
          </button>
        </header>

        <div className="ui-modal-body">{children}</div>
        {footer && <footer className="ui-modal-foot">{footer}</footer>}
      </section>
    </div>
  )
}

export default Modal
