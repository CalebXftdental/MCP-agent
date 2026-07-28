import type { CSSProperties } from 'react'
import EmptyState from './EmptyState'
import './BarList.css'

/**
 * Ranked horizontal bars — "top tools", "top users". Replaces `_hbars()`.
 *
 * Bars are scaled against the largest value, not the total, which is right for a
 * "top N" list: the point is relative rank, and a top-10 slice of a long tail
 * would otherwise render as ten near-empty tracks.
 */

export interface BarItem {
  label: string
  value: number
  /** Overrides the accent, for a categorical series. */
  color?: string
  /** Tooltip. Defaults to the label, which is usually truncated. */
  title?: string
}

export interface BarListProps {
  items: BarItem[]
  /** Trim to the top N by value. */
  limit?: number
  /** Formats the trailing number. */
  format?: (value: number) => string
  emptyLabel?: string
}

function BarList({ items, limit, format = (v) => v.toLocaleString(), emptyLabel = 'No data yet' }: BarListProps) {
  if (items.length === 0) return <EmptyState title={emptyLabel} compact />

  const ranked = [...items].sort((a, b) => b.value - a.value).slice(0, limit ?? items.length)
  const max = Math.max(1, ...ranked.map((i) => i.value))

  return (
    <ul className="ui-bars">
      {ranked.map((item, i) => (
        <li className="ui-bar" key={`${item.label}-${i}`}>
          <span className="ui-bar-label ui-truncate" title={item.title ?? item.label}>
            {item.label}
          </span>
          <span className="ui-bar-track">
            <span
              className="ui-bar-fill"
              style={
                {
                  '--ui-bar-pct': `${((item.value / max) * 100).toFixed(1)}%`,
                  '--ui-i': i,
                  background: item.color,
                } as CSSProperties
              }
            />
          </span>
          <span className="ui-bar-value">{format(item.value)}</span>
        </li>
      ))}
    </ul>
  )
}

export default BarList
