import './ProgressBar.css'

/** A determinate progress bar with an optional step trail — for a pipeline
 *  that reports its own stage (upload → extract → chunk → embed → index, see
 *  KnowledgePage's ingest progress) rather than a plain "loading" spinner. */

export interface ProgressStep {
  key: string
  label: string
}

export interface ProgressBarProps {
  /** 0–100. Clamped. */
  percent: number
  tone?: 'accent' | 'danger'
  label?: string
  sublabel?: string
  /** When given, renders as a labeled trail instead of a plain track — the
   *  step at/after `activeIndex` is highlighted as current, everything before
   *  it as complete. */
  steps?: ProgressStep[]
  activeIndex?: number
}

export function ProgressBar({
  percent,
  tone = 'accent',
  label,
  sublabel,
  steps,
  activeIndex = -1,
}: ProgressBarProps) {
  const clamped = Math.max(0, Math.min(100, percent))
  return (
    <div className="ui-progress" role="progressbar" aria-valuenow={clamped} aria-valuemin={0} aria-valuemax={100}>
      {(label || sublabel) && (
        <div className="ui-progress-labels">
          {label && <span className="ui-progress-label">{label}</span>}
          {sublabel && <span className="ui-progress-sublabel">{sublabel}</span>}
        </div>
      )}
      <div className={`ui-progress-track ui-progress-track--${tone}`}>
        <div
          className={`ui-progress-fill ${tone === 'accent' && clamped < 100 ? 'ui-progress-fill--active' : ''}`}
          style={{ width: `${clamped}%` }}
        />
      </div>
      {steps && steps.length > 0 && (
        <ol className="ui-progress-steps">
          {steps.map((step, i) => (
            <li
              key={step.key}
              className={
                'ui-progress-step' +
                (i < activeIndex ? ' ui-progress-step--done' : '') +
                (i === activeIndex ? ' ui-progress-step--active' : '')
              }
            >
              {step.label}
            </li>
          ))}
        </ol>
      )}
    </div>
  )
}

export default ProgressBar
