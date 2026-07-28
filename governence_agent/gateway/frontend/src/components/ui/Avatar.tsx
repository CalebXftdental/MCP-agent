import type { CSSProperties } from 'react'
import './Avatar.css'

/**
 * Initials disc for the sidebar identity block and any per-principal row.
 *
 * The tint is derived from the name, so the same user is the same colour on every
 * screen — a weak but real identity cue when several similar names appear in an
 * audit list. It is decoration, never the only identifier: the name is always
 * rendered next to it.
 */

export interface AvatarProps {
  /** Person, service account, or consumer name. */
  name: string
  size?: 'sm' | 'md' | 'lg'
  /** Small status dot on the corner — online, revoked, paused. */
  status?: 'ok' | 'warn' | 'danger' | 'idle'
  /** Names the status dot. Without it the dot is decorative. */
  statusLabel?: string
}

/** Up to two initials. Falls back to the first character for a single-token name
 *  like a service id, and to a dash for an empty one. */
function initials(name: string): string {
  const parts = name.trim().split(/[\s._-]+/).filter(Boolean)
  if (parts.length === 0) return '–'
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase()
  return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase()
}

/** Deterministic hue. Plain sum-of-codepoints — this only has to be stable and
 *  well spread, not cryptographic. */
function hue(name: string): number {
  let total = 0
  for (const char of name) total = (total + char.codePointAt(0)!) % 360
  return total
}

function Avatar({ name, size = 'md', status, statusLabel }: AvatarProps) {
  return (
    <span
      className={`ui-avatar ui-avatar--${size}`}
      // Saturation and lightness are fixed, so no generated hue can come out
      // shouting or unreadable against the ink derived from it.
      style={{ '--ui-avatar-hue': hue(name) } as CSSProperties}
      title={name}
    >
      <span aria-hidden="true">{initials(name)}</span>
      <span className="ui-sr-only">{name}</span>
      {status && (
        <span
          className={`ui-avatar-status ui-avatar-status--${status}`}
          role={statusLabel ? 'img' : 'presentation'}
          aria-label={statusLabel}
        />
      )}
    </span>
  )
}

export default Avatar
