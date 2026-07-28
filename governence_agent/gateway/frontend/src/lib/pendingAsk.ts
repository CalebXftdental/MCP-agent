/**
 * The Home → Assistant handoff.
 *
 * Replaces the legacy `let PENDING_ASK = null`, which lived in module scope and
 * so was lost on any real navigation or refresh — the ask silently vanished if
 * the Assistant panel took a moment to mount.
 *
 * sessionStorage instead: it survives a reload and a hard navigation, is scoped
 * to the browser tab, and is cleared by `take()` on first read so an ask is
 * delivered exactly once. Two tabs never share a pending ask.
 *
 * Not namespaced per user, unlike the saved-asks lists: this value lives for a
 * single navigation, and `take()` empties it well before any account switch.
 */

const KEY = 'gov_pending_ask'

/** Queue an ask for whichever page consumes it next. */
export function setPendingAsk(text: string): void {
  const trimmed = text.trim()
  if (!trimmed) return
  try {
    sessionStorage.setItem(KEY, trimmed)
  } catch {
    // Private-mode quota failures are not worth surfacing: losing a queued ask
    // means the Assistant opens empty, which is recoverable by retyping.
  }
}

/** Read and clear. Returns null when nothing is queued. */
export function takePendingAsk(): string | null {
  try {
    const value = sessionStorage.getItem(KEY)
    if (value) sessionStorage.removeItem(KEY)
    return value || null
  } catch {
    return null
  }
}
