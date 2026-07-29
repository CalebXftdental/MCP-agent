import type { CSSProperties } from 'react'
import { Badge, EmptyState, Skeleton } from '../ui'
import type { ChatSessionSummary } from '../../lib/api'
import { formatRelative, formatWhen } from '../../lib/format'
import './ConversationRail.css'

/**
 * A past-conversations list — one preview row per session, newest first
 * (server-sorted). Shared by Home's sidebar (`dense`, always visible, "like
 * what Claude does") and History's "Past conversations" card (the same list,
 * full-width) — one component so opening a conversation looks and behaves
 * identically no matter which page it was clicked from.
 *
 * Selecting a row doesn't open the conversation itself — `onSelect` just
 * reports the click. Both callers wire it to `navigate('home', sessionId)`,
 * so the actual resume-and-load happens in exactly one place (Home's
 * `routeParam` effect), which is what makes "opened from the rail" and
 * "opened from History" the same code path rather than two similar ones.
 */

export interface ConversationRailProps {
  sessions: ChatSessionSummary[]
  state: 'loading' | 'ready' | 'error'
  /** Highlights the session currently open on Home. */
  activeId?: string | null
  onSelect: (sessionId: string) => void
  /** The session a resume request is in flight for — disables that one row
   *  without freezing the whole list. */
  openingId?: string | null
  emptyTitle?: string
  emptyDescription?: string
  /** Tighter rows for the fixed-width sidebar; the History card uses the
   *  roomier default. */
  dense?: boolean
}

function preview(session: ChatSessionSummary): string {
  if (session.summary) return session.summary
  return session.status === 'open' ? 'Just started…' : 'No summary yet'
}

function ConversationRail({
  sessions,
  state,
  activeId,
  onSelect,
  openingId,
  emptyTitle = 'No conversations yet',
  emptyDescription = 'Ask the assistant something to start one.',
  dense = false,
}: ConversationRailProps) {
  if (state === 'loading') {
    return (
      <div className="conv-rail-list">
        {Array.from({ length: 4 }, (_, i) => (
          <Skeleton key={i} index={i} height={dense ? '2.7rem' : '3.6rem'} />
        ))}
      </div>
    )
  }

  if (state === 'error') {
    return <EmptyState title="Couldn't load conversations" compact={dense} />
  }

  if (sessions.length === 0) {
    return <EmptyState title={emptyTitle} description={emptyDescription} compact={dense} />
  }

  return (
    <ul className={`conv-rail-list ${dense ? 'conv-rail-list--dense' : ''}`}>
      {sessions.map((session, i) => (
        <li key={session.session_id}>
          <button
            type="button"
            className="conv-rail-row"
            data-active={session.session_id === activeId || undefined}
            style={{ '--ui-i': i } as CSSProperties}
            onClick={() => onSelect(session.session_id)}
            disabled={openingId === session.session_id}
            title={formatWhen(session.last_active_at)}
          >
            <span className="conv-rail-row-top">
              <span className="conv-rail-when">{formatRelative(session.last_active_at)}</span>
              {session.status === 'open' && (
                <Badge tone="ok" dot pulse subtle>
                  live
                </Badge>
              )}
            </span>
            <span className="conv-rail-preview">{preview(session)}</span>
          </button>
        </li>
      ))}
    </ul>
  )
}

export default ConversationRail
