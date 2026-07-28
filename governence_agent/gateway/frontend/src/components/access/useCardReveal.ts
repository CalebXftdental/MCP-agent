import { useCallback, useEffect, useRef, useState } from 'react'
import type { CSSProperties } from 'react'

/** Rough ceiling for the overlay's height — used only to decide which side of
 *  the card has room, not to constrain the overlay itself (it's never clipped,
 *  by design). Generous on purpose: guessing a little too tall just flips the
 *  placement a bit earlier than strictly necessary, which is a far cheaper
 *  mistake than guessing too short and clipping against the viewport edge. */
const ESTIMATED_OVERLAY_HEIGHT = 170
const GAP = 6

function placementFor(rect: DOMRect): CSSProperties {
  const spaceBelow = window.innerHeight - rect.bottom
  const openAbove = spaceBelow < ESTIMATED_OVERLAY_HEIGHT + GAP && rect.top > spaceBelow
  return {
    position: 'fixed',
    left: rect.left,
    width: rect.width,
    ...(openAbove ? { bottom: window.innerHeight - rect.top + GAP } : { top: rect.bottom + GAP }),
  }
}

/**
 * Measures a card on hover/focus and returns fixed-position styling for a
 * portal-rendered detail overlay.
 *
 * This has to be a portal rather than an absolutely-positioned child: Card's
 * base style sets `overflow:hidden` (needed elsewhere, to crop image/table
 * content to its rounded corners), and every ancestor Card up the tree clips a
 * `position:absolute` descendant to its own box — first the card itself, then
 * whatever Card wraps the grid it lives in, and the same problem resurfaces at
 * the page's own scroll container after that. A portal sidesteps the whole
 * class of bug: it renders outside every ancestor's box entirely, so nothing
 * can clip it and it can never collide with content below it in normal flow.
 *
 * Shared by every tool-card variant so the placement/dismissal logic can't
 * drift between them.
 */
export function useCardReveal<T extends HTMLElement>() {
  const ref = useRef<T>(null)
  const [style, setStyle] = useState<CSSProperties | null>(null)

  const show = useCallback(() => {
    const el = ref.current
    if (!el) return
    setStyle(placementFor(el.getBoundingClientRect()))
  }, [])

  const hide = useCallback(() => setStyle(null), [])

  // A fixed-position snapshot goes stale the moment the page scrolls or
  // resizes — dismiss rather than track it live, which is the same trade a
  // native title tooltip makes. Capture phase so this fires for a scroll on
  // ANY ancestor (e.g. the shell's own scroll region), not just the window:
  // scroll events don't bubble, but they do reach capturing listeners on
  // every element above their target.
  useEffect(() => {
    if (!style) return
    window.addEventListener('scroll', hide, true)
    window.addEventListener('resize', hide)
    return () => {
      window.removeEventListener('scroll', hide, true)
      window.removeEventListener('resize', hide)
    }
  }, [style, hide])

  return { ref, style, show, hide }
}
