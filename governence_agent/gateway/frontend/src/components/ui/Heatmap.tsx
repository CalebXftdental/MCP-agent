import { Fragment } from 'react'
import type { CSSProperties } from 'react'
import EmptyState from './EmptyState'
import './Heatmap.css'

/**
 * A row × column intensity grid — built for Monitor's "activity by hour"
 * (weekday × hour), but generic: rows/columns are just labelled buckets.
 *
 * No component in the kit already did this (Donut/BarList/Sparkline are all
 * one-dimensional), so this is new. Intensity is `color-mix()` against a
 * fixed swatch rather than a second colour ramp — the same trick Field.css
 * and Skeleton.css already use for tinting, so this doesn't invent a new
 * pattern for "colour at N% strength."
 */

export interface HeatmapProps {
  /** `grid[row][col]` — every row must be the same length. */
  grid: number[][]
  rowLabels: string[]
  /** Header text for one column. Return '' to skip a label without losing
   *  the column's slot (e.g. showing only every 3rd hour). */
  colLabel?: (col: number) => string
  /** Tooltip + accessible text for one cell. Defaults to the bare count. */
  cellLabel?: (value: number, row: number, col: number) => string
  /** Overrides the peak used to scale intensity — otherwise the grid's own max. */
  max?: number
  color?: string
  emptyLabel?: string
}

function Heatmap({
  grid,
  rowLabels,
  colLabel,
  cellLabel,
  max,
  color = 'var(--ui-accent)',
  emptyLabel = 'No activity yet',
}: HeatmapProps) {
  const cols = grid[0]?.length ?? 0
  const peak = max ?? Math.max(1, ...grid.flat())

  // An all-zero grid still renders as a grid — every cell just stays at rest
  // colour, which reads as "nothing happened yet," not as a hidden chart. Only
  // a genuinely empty/malformed grid (no rows or no columns at all) falls back
  // to a placeholder, since there's nothing to draw a shape from.
  if (grid.length === 0 || cols === 0) {
    return <EmptyState title={emptyLabel} compact />
  }

  return (
    <div
      className="ui-heatmap"
      style={{ '--ui-heat-cols': cols } as CSSProperties}
      role="img"
      aria-label="Activity heatmap by row and column"
    >
      <span className="ui-heatmap-corner" aria-hidden="true" />
      {Array.from({ length: cols }, (_, c) => (
        <span className="ui-heatmap-colhead" key={`h-${c}`}>
          {colLabel?.(c) ?? ''}
        </span>
      ))}
      {grid.map((row, r) => (
        <Fragment key={rowLabels[r] ?? r}>
          <span className="ui-heatmap-rowhead">{rowLabels[r] ?? r}</span>
          {row.map((value, c) => {
            const pct = value ? 12 + 88 * (value / peak) : 0
            return (
              <span
                key={c}
                className="ui-heatmap-cell"
                style={
                  {
                    background: value ? `color-mix(in srgb, ${color} ${pct.toFixed(0)}%, transparent)` : undefined,
                  } as CSSProperties
                }
                title={cellLabel ? cellLabel(value, r, c) : `${value}`}
              />
            )
          })}
        </Fragment>
      ))}
    </div>
  )
}

export default Heatmap
