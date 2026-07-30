import type { CSSProperties } from 'react'
import { useCardReveal } from '../access/useCardReveal'

/** Wide enough to read a full sentence without wrapping every word onto its
 *  own line, narrow enough to still read as a popover, not a panel. */
const OVERLAY_WIDTH = 260

/**
 * `useCardReveal` sizes its overlay to match the TRIGGER's own bounding-box
 * width — correct for a card-sized trigger (that's what it was built for:
 * AccessCard/PickableAccessCard), but wrong for a small icon like
 * `WorkflowInfoIcon`/`WorkflowAlertBadge`: a ~17px-wide circular button
 * produces a ~17px-wide overlay, which wraps every single word onto its own
 * line instead of reading as a sentence.
 *
 * This keeps `useCardReveal`'s positioning/dismissal logic (scroll/resize/
 * outside-click all still apply) and only overrides the resulting width to a
 * fixed, readable size — clamped so the overlay never runs off the right
 * edge of the viewport regardless of how close to the edge the icon sits.
 */
export function useIconReveal<T extends HTMLElement>() {
  const { ref, style, show, hide } = useCardReveal<T>()

  const fixedStyle: CSSProperties | null = style
    ? {
        ...style,
        width: OVERLAY_WIDTH,
        left: Math.max(8, Math.min(Number(style.left) || 0, window.innerWidth - OVERLAY_WIDTH - 8)),
      }
    : null

  return { ref, style: fixedStyle, show, hide }
}
