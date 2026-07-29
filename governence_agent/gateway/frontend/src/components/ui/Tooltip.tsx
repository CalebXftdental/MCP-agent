import { cloneElement, useCallback, useEffect, useId, useRef, useState } from 'react'
import type { CSSProperties, ReactElement, ReactNode } from 'react'
import { createPortal } from 'react-dom'
import './Tooltip.css'

/**
 * A hover/focus label for a trigger that doesn't carry its own visible text —
 * built for the sidebar's collapsed icon rail, where a nav item's name
 * disappears and the native `title` attribute is the only fallback (slow to
 * appear, unstyled, and invisible to a screen reader unless something else
 * also names the element).
 *
 * Portal-rendered at a measured fixed position, the same approach `Dropdown`
 * and `DateTimePicker` use for their own floating panels: the sidebar's nav
 * list scrolls (`overflow-y: auto`), so a plain `position: absolute` bubble
 * would get clipped the moment the trigger neared the scroll boundary. A
 * stale position after a scroll/resize is handled by closing rather than
 * re-tracking, same trade-off as those.
 *
 * `children` is cloned, not wrapped in an extra element — a nav item relies
 * on being a direct flex child of its list, and a wrapping `<span>` would
 * break that. The trade-off: the child must not already set
 * onMouseEnter/onMouseLeave/onFocus/onBlur/onClick or a ref, since this
 * replaces rather than chains them.
 */

export type TooltipSide = 'top' | 'right' | 'bottom' | 'left'

export interface TooltipProps {
  /** Falsy (empty string, null) never opens — lets a caller pass a
   *  conditionally-empty label without a separate `disabled` check. */
  label: ReactNode
  children: ReactElement
  side?: TooltipSide
  disabled?: boolean
}

const SHOW_DELAY_MS = 300
const GAP_PX = 10

function place(rect: DOMRect, side: TooltipSide): CSSProperties {
  switch (side) {
    case 'left':
      return { top: rect.top + rect.height / 2, left: rect.left - GAP_PX }
    case 'top':
      return { top: rect.top - GAP_PX, left: rect.left + rect.width / 2 }
    case 'bottom':
      return { top: rect.bottom + GAP_PX, left: rect.left + rect.width / 2 }
    case 'right':
    default:
      return { top: rect.top + rect.height / 2, left: rect.right + GAP_PX }
  }
}

function Tooltip({ label, children, side = 'right', disabled = false }: TooltipProps) {
  const [open, setOpen] = useState(false)
  const [rect, setRect] = useState<DOMRect | null>(null)
  const triggerRef = useRef<HTMLElement | null>(null)
  const timerRef = useRef<ReturnType<typeof setTimeout>>(undefined)
  const tooltipId = useId()

  const hide = useCallback(() => {
    clearTimeout(timerRef.current)
    setOpen(false)
  }, [])

  const show = useCallback(() => {
    if (disabled || !label) return
    clearTimeout(timerRef.current)
    timerRef.current = setTimeout(() => {
      const node = triggerRef.current
      if (node) setRect(node.getBoundingClientRect())
      setOpen(true)
    }, SHOW_DELAY_MS)
  }, [disabled, label])

  useEffect(() => () => clearTimeout(timerRef.current), [])

  useEffect(() => {
    if (!open) return
    window.addEventListener('scroll', hide, true)
    window.addEventListener('resize', hide)
    return () => {
      window.removeEventListener('scroll', hide, true)
      window.removeEventListener('resize', hide)
    }
  }, [open, hide])

  const trigger = cloneElement(children, {
    ref: (node: HTMLElement | null) => {
      triggerRef.current = node
    },
    onMouseEnter: show,
    onMouseLeave: hide,
    onFocus: show,
    onBlur: hide,
    onClick: hide,
    'aria-describedby': open ? tooltipId : undefined,
  } as Record<string, unknown>)

  return (
    <>
      {trigger}
      {open &&
        rect &&
        createPortal(
          <div id={tooltipId} role="tooltip" className={`ui-tooltip ui-tooltip--${side}`} style={place(rect, side)}>
            {label}
          </div>,
          document.body,
        )}
    </>
  )
}

export default Tooltip
