import { useRef } from 'react'
import type { CSSProperties, KeyboardEvent } from 'react'
import './SegmentedControl.css'

/**
 * The range picker from the monitor and alerts panels (`.seg`) — 1h / 24h / 7d /
 * 30d, Open / All.
 *
 * Two upgrades on the legacy version, which was a row of `<button>`s toggling an
 * `.on` class:
 *   - the selection is a sliding thumb, so switching range shows which direction
 *     you moved instead of one background blinking off and another on;
 *   - it's a real radiogroup with roving tabindex and arrow-key navigation. The
 *     old one put every segment in the tab order and gave keyboard users no way
 *     to change range without four Tab presses.
 */

export interface Segment<T extends string> {
  value: T
  label: string
  /** Tooltip, for a label too short to be self-explanatory. */
  title?: string
}

export interface SegmentedControlProps<T extends string> {
  segments: Segment<T>[]
  value: T
  onChange: (value: T) => void
  /** Names the group for screen readers — "Time range", "Alert filter". */
  label: string
  size?: 'sm' | 'md'
}

function SegmentedControl<T extends string>({
  segments,
  value,
  onChange,
  label,
  size = 'sm',
}: SegmentedControlProps<T>) {
  const ref = useRef<HTMLDivElement>(null)
  const index = Math.max(
    0,
    segments.findIndex((s) => s.value === value),
  )

  const move = (to: number) => {
    const next = (to + segments.length) % segments.length
    onChange(segments[next].value)
    // Focus follows selection in a radiogroup, so arrow keys keep working
    // without a second Tab.
    ref.current?.querySelectorAll<HTMLButtonElement>('.ui-seg-btn')[next]?.focus()
  }

  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    switch (e.key) {
      case 'ArrowRight':
      case 'ArrowDown':
        e.preventDefault()
        move(index + 1)
        break
      case 'ArrowLeft':
      case 'ArrowUp':
        e.preventDefault()
        move(index - 1)
        break
      case 'Home':
        e.preventDefault()
        move(0)
        break
      case 'End':
        e.preventDefault()
        move(segments.length - 1)
        break
      default:
    }
  }

  return (
    <div
      ref={ref}
      className={`ui-seg ui-seg--${size}`}
      role="radiogroup"
      aria-label={label}
      onKeyDown={onKeyDown}
      style={{ '--ui-seg-n': segments.length, '--ui-seg-i': index } as CSSProperties}
    >
      <span className="ui-seg-thumb" aria-hidden="true" />
      {segments.map((segment) => {
        const selected = segment.value === value
        return (
          <button
            key={segment.value}
            type="button"
            className="ui-seg-btn"
            role="radio"
            aria-checked={selected}
            // Roving tabindex: one stop for the whole group.
            tabIndex={selected ? 0 : -1}
            title={segment.title}
            onClick={() => onChange(segment.value)}
          >
            {segment.label}
          </button>
        )
      })}
    </div>
  )
}

export default SegmentedControl
