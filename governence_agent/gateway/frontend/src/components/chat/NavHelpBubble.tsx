import { useEffect, useRef, useState } from 'react'
import { Button, TypingDots } from '../ui'
import { ApiError, sendNavHelp, type NavHelpTurn } from '../../lib/api'
import { ROUTES, type RouteKey } from '../../pages/routes'
import type { Router } from '../../hooks/useRoute'
import './NavHelpBubble.css'

/**
 * Floating "where do I find X" helper, mounted once in AppShell so it
 * persists across every tab except Home (which already IS a full assistant —
 * see routes.ts's comment on the home route).
 *
 * Deliberately separate from Home's governed assistant (backend/nav_help.py):
 * no MCP tools, no knowledge base, no chat_log session on the server. This
 * component holds the only copy of its own history and replays the last few
 * turns with each question, the same way a stateless turn always has.
 */

export interface NavHelpBubbleProps {
  router: Router
}

interface NavHelpMessage {
  id: number
  role: 'user' | 'assistant'
  content: string
  navigate: RouteKey | null
  error?: boolean
}

const MAX_HISTORY_TURNS = 6

function isRouteKey(value: string | null): value is RouteKey {
  return value !== null && Object.hasOwn(ROUTES, value)
}

function NavHelpBubble({ router }: NavHelpBubbleProps) {
  const [open, setOpen] = useState(false)
  const [messages, setMessages] = useState<NavHelpMessage[]>([])
  const [draft, setDraft] = useState('')
  const [busy, setBusy] = useState(false)
  const nextId = useRef(0)
  const scrollRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight })
  }, [messages, busy])

  // Home already IS the full assistant (routes.ts) — a second chat surface
  // there would just be two doors to two different, half-overlapping things.
  if (router.key === 'home') return null

  const ask = async (text: string) => {
    const question = text.trim()
    if (!question || busy) return
    setDraft('')
    const history: NavHelpTurn[] = messages
      .slice(-MAX_HISTORY_TURNS)
      .map((m) => ({ role: m.role, content: m.content }))
    setMessages((prev) => [...prev, { id: nextId.current++, role: 'user', content: question, navigate: null }])
    setBusy(true)
    try {
      const result = await sendNavHelp(question, history)
      const navigate = isRouteKey(result.navigate) ? result.navigate : null
      setMessages((prev) => [
        ...prev,
        { id: nextId.current++, role: 'assistant', content: result.reply, navigate },
      ])
    } catch (cause) {
      const message = cause instanceof ApiError ? cause.message : 'Could not reach the navigation helper.'
      setMessages((prev) => [
        ...prev,
        { id: nextId.current++, role: 'assistant', content: message, navigate: null, error: true },
      ])
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="nhb" data-open={open || undefined}>
      {open && (
        <div className="nhb-panel" role="dialog" aria-label="Where do I find that?">
          <header className="nhb-panel-head">
            <h2>Where do I find…</h2>
            <button type="button" className="nhb-close" onClick={() => setOpen(false)} aria-label="Close">
              ✕
            </button>
          </header>

          <div className="nhb-scroll" ref={scrollRef}>
            {messages.length === 0 && (
              <p className="nhb-empty">
                Ask where something lives in this console — I only know navigation, not your data.
              </p>
            )}
            {messages.map((m) => (
              <div key={m.id} className={`nhb-msg nhb-msg--${m.role}${m.error ? ' nhb-msg--error' : ''}`}>
                <p>{m.content}</p>
                {m.navigate && (
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => {
                      router.navigate(m.navigate as RouteKey)
                      setOpen(false)
                    }}
                  >
                    Go to {ROUTES[m.navigate].title} →
                  </Button>
                )}
              </div>
            ))}
            {busy && <TypingDots label="Navigation helper is replying" />}
          </div>

          <form
            className="nhb-composer"
            onSubmit={(e) => {
              e.preventDefault()
              ask(draft)
            }}
          >
            <label className="ui-sr-only" htmlFor="nhb-input">
              Ask where to find something
            </label>
            <input
              id="nhb-input"
              className="nhb-input"
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              placeholder="e.g. where do I approve a send?"
              autoComplete="off"
              autoFocus
            />
            <Button type="submit" size="sm" disabled={draft.trim().length === 0 || busy}>
              Ask
            </Button>
          </form>
        </div>
      )}

      <button
        type="button"
        className="nhb-badge"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        aria-label={open ? 'Close navigation helper' : 'Open navigation helper'}
      >
        🧭
      </button>
    </div>
  )
}

export default NavHelpBubble
