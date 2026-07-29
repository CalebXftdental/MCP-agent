import type { CSSProperties } from 'react'
import { CATEGORICAL } from './scales'
import EmptyState from './EmptyState'
import './StackedBars.css'

/**
 * A time-bucketed stacked column chart — Monitor's "call volume" (authorized
 * / denied / error per hour or day). Replaces the legacy `_stacked()` raw-SVG
 * builder.
 *
 * Every segment in a bucket is a flex item sized by `flexGrow: value`, so the
 * stack's proportions are exact regardless of the bucket's absolute total —
 * no per-pixel math the way the old SVG version needed. The bucket's overall
 * height is the one thing set by inline style, scaled against the tallest
 * bucket in the series.
 */

export interface StackedBarSegment {
  key: string
  value: number
  color?: string
}

export interface StackedBarBucket {
  /** Axis label — an hour ("14:00") or a date ("7/29"). */
  label: string
  segments: StackedBarSegment[]
}

export interface StackedBarsProps {
  buckets: StackedBarBucket[]
  height?: number
  /** Total above each bar. Off for a bucket with nothing in it. */
  showTotal?: boolean
  emptyLabel?: string
}

function StackedBars({ buckets, height = 160, showTotal = true, emptyLabel = 'No calls in this window' }: StackedBarsProps) {
  const totals = buckets.map((b) => b.segments.reduce((sum, seg) => sum + seg.value, 0))
  const max = Math.max(1, ...totals)
  const hasData = totals.some((t) => t > 0)

  if (buckets.length === 0 || !hasData) {
    return <EmptyState title={emptyLabel} compact />
  }

  // At most ~8 axis labels, so a 30-day range doesn't print 30 overlapping dates.
  const labelStep = Math.max(1, Math.ceil(buckets.length / 8))

  return (
    <div className="ui-stackbars">
      <div className="ui-stackbars-plot" style={{ height }}>
        {buckets.map((bucket, i) => {
          const total = totals[i]
          return (
            <div
              className="ui-stackbars-col"
              key={`${bucket.label}-${i}`}
              style={{ '--ui-i': i } as CSSProperties}
              title={`${bucket.label}: ${total.toLocaleString()}`}
            >
              {showTotal && total > 0 && <span className="ui-stackbars-total">{total}</span>}
              <div className="ui-stackbars-track" style={{ height: `${(total / max) * 100}%` }}>
                {bucket.segments.map(
                  (seg, si) =>
                    seg.value > 0 && (
                      <div
                        key={seg.key}
                        className="ui-stackbars-seg"
                        style={{ flexGrow: seg.value, background: seg.color ?? CATEGORICAL[si % CATEGORICAL.length] }}
                      />
                    ),
                )}
              </div>
            </div>
          )
        })}
      </div>
      <div className="ui-stackbars-labels">
        {buckets.map((bucket, i) => (
          <span className="ui-stackbars-label" key={`lab-${bucket.label}-${i}`}>
            {i % labelStep === 0 ? bucket.label : ''}
          </span>
        ))}
      </div>
    </div>
  )
}

export default StackedBars
