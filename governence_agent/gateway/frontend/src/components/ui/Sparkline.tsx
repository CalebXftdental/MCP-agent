import { useId } from 'react'
import './Sparkline.css'

/**
 * Inline trend line for a Stat. Replaces the legacy `_spark()` string builder.
 *
 * Three things it adds: a gradient area under the line so the shape reads at
 * 26px tall, an emphasised endpoint marking "now" (the value the number beside
 * it shows), and a left-to-right draw-in.
 *
 * `preserveAspectRatio="none"` stretches the viewBox to the box, which is what
 * makes a 64×26 sparkline work at any width — `vector-effect: non-scaling-stroke`
 * keeps the stroke from stretching with it.
 */

export interface SparklineProps {
  values: number[]
  /** Any CSS colour. Defaults to the accent. */
  color?: string
  width?: number
  height?: number
  /** Gradient area under the line. */
  fill?: boolean
  /** Dot on the final point. */
  showEnd?: boolean
  /** Describes the trend for screen readers. Omit inside a labelled Stat, where
   *  the number already carries the meaning. */
  label?: string
}

function Sparkline({
  values,
  color = 'var(--ui-accent)',
  width = 64,
  height = 26,
  fill = true,
  showEnd = true,
  label,
}: SparklineProps) {
  const gradientId = useId()

  // One point can't make a line, and a flat series has no range to normalise by.
  if (values.length < 2) return null

  const min = Math.min(...values)
  const max = Math.max(...values)
  const span = max - min || 1
  const stepX = width / (values.length - 1)
  // 1.5px inset top and bottom so the stroke isn't clipped at the extremes.
  const y = (v: number) => height - 1.5 - ((v - min) / span) * (height - 3)

  const points = values.map((v, i) => `${(i * stepX).toFixed(2)},${y(v).toFixed(2)}`)
  const line = points.join(' ')
  const area = `0,${height} ${line} ${width},${height}`
  const [lastX, lastY] = points[points.length - 1].split(',')

  return (
    <svg
      className="ui-spark"
      viewBox={`0 0 ${width} ${height}`}
      width={width}
      height={height}
      preserveAspectRatio="none"
      role={label ? 'img' : 'presentation'}
      aria-label={label}
      aria-hidden={label ? undefined : true}
    >
      {fill && (
        <>
          <defs>
            <linearGradient id={gradientId} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={color} stopOpacity="0.3" />
              <stop offset="100%" stopColor={color} stopOpacity="0" />
            </linearGradient>
          </defs>
          <polygon className="ui-spark-area" points={area} fill={`url(#${gradientId})`} />
        </>
      )}
      <polyline
        className="ui-spark-line"
        points={line}
        fill="none"
        stroke={color}
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
        vectorEffect="non-scaling-stroke"
        pathLength={1}
      />
      {showEnd && (
        <circle
          className="ui-spark-end"
          cx={lastX}
          cy={lastY}
          r="1.6"
          fill={color}
          vectorEffect="non-scaling-stroke"
        />
      )}
    </svg>
  )
}

export default Sparkline
