/** Shared visual scales — the mappings from domain values to colour.
 *
 *  Separate from the components that render them so severity and category
 *  colour are decided in exactly one place: a table pill, a drawer stripe, and a
 *  donut wedge for the same alert must not disagree. (It also keeps Badge.tsx and
 *  Donut.tsx exporting components only, which is what React Fast Refresh needs.)
 */

export type BadgeTone = 'neutral' | 'accent' | 'ok' | 'warn' | 'danger' | 'info'

export type Severity = 'critical' | 'high' | 'medium' | 'low'

/** `critical` and `high` share red deliberately: both are act-now, and the
 *  severity label beside the pill carries the distinction. */
const SEVERITY_TONE: Record<Severity, BadgeTone> = {
  critical: 'danger',
  high: 'danger',
  medium: 'warn',
  low: 'neutral',
}

/** Tone for an alert severity. Unknown strings fall back to neutral rather than
 *  throwing — severities come from the backend and may gain values. */
export function severityTone(severity: string): BadgeTone {
  return SEVERITY_TONE[severity as Severity] ?? 'neutral'
}

/** Categorical ramp for breakdowns with no inherent meaning (calls by
 *  department, tools by volume). Starts with the brand teal, then hues picked to
 *  stay distinguishable in both themes while staying clear of the semantic
 *  red/amber/green — a department must never accidentally look like an error. */
export const CATEGORICAL = [
  'var(--ui-accent-500)',
  '#5b8def',
  '#a978d8',
  '#e08a5b',
  '#4bb3a5',
  '#8f9bb3',
]
