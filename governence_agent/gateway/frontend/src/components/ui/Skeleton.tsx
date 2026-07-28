import type { CSSProperties } from 'react'
import './Skeleton.css'

/**
 * Loading placeholders. The old dashboard wrote the literal string "Loading…"
 * into the target element, so every panel collapsed to one line and then jumped
 * to full height when data landed. A skeleton holds the eventual layout, so
 * nothing below it moves.
 */

export interface SkeletonProps {
  /** Any CSS length. Percentages work — the parent is the reference. */
  width?: string
  height?: string
  radius?: string
  /** Stagger index, so a column of skeletons ripples instead of blinking. */
  index?: number
  className?: string
}

export function Skeleton({
  width = '100%',
  height = '1rem',
  radius = 'var(--ui-r-xs)',
  index = 0,
  className = '',
}: SkeletonProps) {
  return (
    <span
      className={`ui-skeleton ${className}`}
      style={{ width, height, borderRadius: radius, '--ui-i': index } as CSSProperties}
      aria-hidden="true"
    />
  )
}

export interface SkeletonTextProps {
  lines?: number
  /** Width of the final line, which reads as a partial sentence. */
  lastLineWidth?: string
}

export function SkeletonText({ lines = 3, lastLineWidth = '62%' }: SkeletonTextProps) {
  return (
    <span className="ui-skeleton-text" role="status" aria-label="Loading">
      {Array.from({ length: lines }, (_, i) => (
        <Skeleton
          key={i}
          index={i}
          height="0.72rem"
          width={i === lines - 1 ? lastLineWidth : '100%'}
        />
      ))}
    </span>
  )
}

export default Skeleton
