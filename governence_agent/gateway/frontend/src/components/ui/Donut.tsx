import type { ReactNode } from 'react'
import { CATEGORICAL } from './scales'
import './Donut.css'

/**
 * Proportion ring plus its legend — authorized/denied/error, calls by
 * department. Replaces `_donut()` and `_legend()`.
 *
 * Geometry note: `pathLength={100}` renormalises the circle so every
 * `strokeDasharray` below is literally a percentage, which is what removes the
 * `2πr` arithmetic the legacy version carried around.
 */

export interface DonutSegment {
  label: string
  value: number
  /** Any CSS colour. Falls back to the shared categorical ramp by index. */
  color?: string
}

export interface DonutProps {
  segments: DonutSegment[]
  size?: number
  thickness?: number
  /** Big number in the middle. */
  centerValue?: ReactNode
  /** Caption under it. */
  centerLabel?: ReactNode
  /** Describes the breakdown. Required when no legend is rendered alongside. */
  label?: string
}

export function Donut({
  segments,
  size = 108,
  thickness = 13,
  centerValue,
  centerLabel,
  label,
}: DonutProps) {
  const total = segments.reduce((sum, s) => sum + s.value, 0)
  const radius = (size - thickness) / 2
  const center = size / 2

  let offset = 0

  return (
    <div className="ui-donut" style={{ width: size, height: size }}>
      <svg
        className="ui-donut-svg"
        viewBox={`0 0 ${size} ${size}`}
        width={size}
        height={size}
        role={label ? 'img' : 'presentation'}
        aria-label={label}
      >
        {/* Rotated so the first segment starts at 12 o'clock, where a reader
            expects a proportion ring to begin. */}
        <g transform={`rotate(-90 ${center} ${center})`}>
          <circle
            className="ui-donut-track"
            cx={center}
            cy={center}
            r={radius}
            fill="none"
            strokeWidth={thickness}
          />
          {total > 0 &&
            segments.map((segment, i) => {
              const pct = (segment.value / total) * 100
              const dash = `${pct} ${100 - pct}`
              const thisOffset = offset
              offset += pct
              return (
                <circle
                  key={segment.label}
                  className="ui-donut-seg"
                  cx={center}
                  cy={center}
                  r={radius}
                  fill="none"
                  stroke={segment.color ?? CATEGORICAL[i % CATEGORICAL.length]}
                  strokeWidth={thickness}
                  strokeLinecap="butt"
                  pathLength={100}
                  strokeDasharray={dash}
                  strokeDashoffset={-thisOffset}
                />
              )
            })}
        </g>
      </svg>

      {(centerValue != null || centerLabel != null) && (
        <div className="ui-donut-center">
          {centerValue != null && <span className="ui-donut-value">{centerValue}</span>}
          {centerLabel != null && <span className="ui-donut-label">{centerLabel}</span>}
        </div>
      )}
    </div>
  )
}

export interface LegendProps {
  segments: DonutSegment[]
  /** Adds each segment's share of the total. */
  showPercent?: boolean
  format?: (value: number) => string
}

export function Legend({ segments, showPercent = false, format = (v) => v.toLocaleString() }: LegendProps) {
  const total = segments.reduce((sum, s) => sum + s.value, 0) || 1

  return (
    <ul className="ui-legend">
      {segments.map((segment, i) => (
        <li className="ui-legend-row" key={segment.label}>
          <span
            className="ui-legend-swatch"
            style={{ background: segment.color ?? CATEGORICAL[i % CATEGORICAL.length] }}
            aria-hidden="true"
          />
          <span className="ui-legend-name ui-truncate" title={segment.label}>
            {segment.label}
          </span>
          <span className="ui-legend-value">
            {format(segment.value)}
            {showPercent && (
              <span className="ui-legend-pct">
                {' '}
                {((segment.value / total) * 100).toFixed(0)}%
              </span>
            )}
          </span>
        </li>
      ))}
    </ul>
  )
}

export default Donut
