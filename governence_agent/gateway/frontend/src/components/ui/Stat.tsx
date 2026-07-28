import type { ReactNode } from 'react'
import { useCountUp } from './motion'
import './Stat.css'

/**
 * The KPI tile (`.kpi` / `.lab` / `.val` / `.kpi.alert`), 37 uses across the
 * monitor and activity panels.
 *
 * Numeric values count up when they CHANGE, not on first paint — these panels
 * poll, and a tile that animates 0 → 4,182 every refresh is noise. First render
 * and every unchanged poll land straight on the number.
 *
 * Values are grouped with `toLocaleString()`. The old UI printed raw integers,
 * so a row count read `1284502`.
 */

export type StatTone = 'neutral' | 'ok' | 'warn' | 'danger'

export interface StatProps {
  label: ReactNode
  /** A number animates and gets grouped; a string is rendered verbatim. */
  value: number | string
  /** Trailing unit, set smaller and muted — `%`, `ms`, `rows`. */
  unit?: ReactNode
  /** Colours the label and value. `danger` is the old `.kpi.alert`. */
  tone?: StatTone
  /** Trend line, usually a <Sparkline />. */
  chart?: ReactNode
  /** Secondary line under the value — a comparison or qualifier. */
  detail?: ReactNode
  /** Long-form explanation on hover. */
  title?: string
  /** Skip the count-up (a stat that only ever renders once). */
  still?: boolean
  /** Decimal places to keep. */
  decimals?: number
  onClick?: () => void
}

export function Stat({
  label,
  value,
  unit,
  tone = 'neutral',
  chart,
  detail,
  title,
  still = false,
  decimals = 0,
  onClick,
}: StatProps) {
  const numeric = typeof value === 'number'
  const shown = useCountUp(numeric ? value : 0, { disabled: still || !numeric, decimals })
  const text = numeric
    ? shown.toLocaleString(undefined, {
        minimumFractionDigits: decimals,
        maximumFractionDigits: decimals,
      })
    : value

  const body = (
    <>
      <span className="ui-stat-label ui-eyebrow">{label}</span>
      <span className="ui-stat-value">
        {text}
        {unit && <small className="ui-stat-unit">{unit}</small>}
      </span>
      {detail && <span className="ui-stat-detail">{detail}</span>}
      {chart && <span className="ui-stat-chart">{chart}</span>}
    </>
  )

  const className = `ui-stat ui-stat--${tone}`

  return onClick ? (
    <button type="button" className={`${className} ui-stat--clickable`} title={title} onClick={onClick}>
      {body}
    </button>
  ) : (
    <div className={className} title={title}>
      {body}
    </div>
  )
}

export interface StatGridProps {
  children: ReactNode
  /** Target columns on a wide screen. Tiles reflow to 2 and then 1 below that,
   *  which the legacy `repeat(4,1fr)` did not — eight KPIs became eight
   *  unreadable slivers on a laptop. */
  columns?: 2 | 3 | 4
}

export function StatGrid({ children, columns = 4 }: StatGridProps) {
  return <div className={`ui-stat-grid ui-stat-grid--${columns}`}>{children}</div>
}

export default Stat
