/** Display formatting shared by panels. Kept out of components so a timestamp
 *  reads the same in a table, a drawer, and a tooltip. */

/** Audit timestamps are unix SECONDS, not milliseconds — a raw `new Date(ts)`
 *  puts every governed call in January 1970. */
export function formatWhen(ts: number | null | undefined): string {
  if (!ts) return '—'
  return new Date(ts * 1000).toLocaleString()
}

const UNITS: [limit: number, secs: number, name: Intl.RelativeTimeFormatUnit][] = [
  [60, 1, 'second'],
  [3600, 60, 'minute'],
  [86_400, 3600, 'hour'],
  [604_800, 86_400, 'day'],
  [2_629_800, 604_800, 'week'],
  [31_557_600, 2_629_800, 'month'],
  [Infinity, 31_557_600, 'year'],
]

/**
 * "3 minutes ago". Relative time is easier to scan in a recent-activity list
 * than a full locale timestamp; pair it with `formatWhen` in a `title` so the
 * exact moment is still one hover away.
 */
export function formatRelative(ts: number | null | undefined, now = Date.now()): string {
  if (!ts) return '—'
  const deltaSec = (ts * 1000 - now) / 1000
  const magnitude = Math.abs(deltaSec)
  if (magnitude < 45) return 'just now'

  const [, secs, unit] = UNITS.find(([limit]) => magnitude < limit)!
  const formatter = new Intl.RelativeTimeFormat(undefined, { numeric: 'auto' })
  return formatter.format(Math.round(deltaSec / secs), unit)
}

/** Milliseconds as the dashboard shows them: whole numbers, never "412.7ms". */
export function formatLatency(ms: number | null | undefined): string {
  return ms == null ? '—' : `${Math.round(ms)}ms`
}

/** First name, title-cased, from a principal name that may be an email, a
 *  dotted login, or a service id. Mirrors the legacy `renderHome` greeting. */
export function firstName(name: string | null | undefined): string {
  const head = (name || 'there').split(/[.@\s]/)[0]
  return head.charAt(0).toUpperCase() + head.slice(1)
}

/** "482 B", "3.4 KB", "12 MB" — the legacy Files panel only ever showed
 *  KB-rounded, so a small artifact read as "0 KB" and a large one as an
 *  unbroken six-digit number. One decimal below 10 of a unit, none above. */
export function formatBytes(bytes: number | null | undefined): string {
  if (bytes == null) return '—'
  if (bytes < 1024) return `${bytes} B`
  const units = ['KB', 'MB', 'GB']
  let value = bytes / 1024
  let i = 0
  while (value >= 1024 && i < units.length - 1) {
    value /= 1024
    i++
  }
  return `${value.toFixed(value < 10 ? 1 : 0)} ${units[i]}`
}
