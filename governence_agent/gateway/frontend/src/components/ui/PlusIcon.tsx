/**
 * A "+" drawn as two exactly-centered SVG strokes, not a text glyph.
 *
 * A font's "+" character is rarely centered within its own em box — ascent/
 * descent and glyph-specific side bearings vary by font — so scaling it via
 * `transform: scale()` (the hover "pop" this kit uses on icon buttons)
 * re-rasterizes the glyph at a new effective size, and browsers can shift
 * its ink by a device pixel relative to its box when they do. On a small
 * circular button that one-pixel shift is proportionally large and reads as
 * the icon "drifting" mid-hover. SVG geometry has no font metrics to drift:
 * the two strokes cross at the exact center of the viewBox at any scale.
 */

export interface PlusIconProps {
  /** Any CSS length. Defaults to filling its font-size, like a text glyph would. */
  size?: number | string
}

export function PlusIcon({ size = '1em' }: PlusIconProps) {
  return (
    <svg viewBox="0 0 24 24" width={size} height={size} aria-hidden="true" focusable="false">
      <path d="M12 5v14M5 12h14" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" />
    </svg>
  )
}

export default PlusIcon
