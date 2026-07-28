import type { BadgeTone } from '../ui'

/** Risk level (from the policy manifest) -> how it reads on a card. Shared by
 *  every tool-card variant — read-only (AccessCard) and pickable
 *  (PickableAccessCard) — so a label/tone can't drift between the two. */
export const RISK_META: Record<string, { label: string; tone: BadgeTone }> = {
  read_low: { label: 'Read', tone: 'neutral' },
  read_sensitive: { label: 'Sensitive data', tone: 'info' },
  export: { label: 'Export', tone: 'warn' },
  send: { label: 'Sends out', tone: 'warn' },
  write: { label: 'Write', tone: 'danger' },
}

/** "get_customer_order_summary" -> "Get Customer Order Summary". Tool names
 *  are already namespaced/descriptive in this codebase, so a straight
 *  underscore-to-title-case pass reads naturally without a lookup table. */
export function humanize(name: string): string {
  return name
    .split('_')
    .filter(Boolean)
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
    .join(' ')
}
