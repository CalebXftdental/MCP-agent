import { useCallback, useEffect, useRef, useState } from 'react'
import { Button, CollapseToggle, useToast } from '../components/ui'
import ChatLog from '../components/chat/ChatLog'
import Composer from '../components/chat/Composer'
import ConversationRail from '../components/chat/ConversationRail'
import QuickAsks from '../components/chat/QuickAsks'
import { useChat } from '../hooks/useChat'
import { useCollapsed } from '../hooks/useCollapsed'
import { useComposer } from '../hooks/useComposer'
import { useConversationHistory } from '../hooks/useConversationHistory'
import { useStoredList } from '../hooks/useStoredList'
import { ApiError } from '../lib/api'
import { downloadText } from '../lib/download'
import { firstName } from '../lib/format'
import { takePendingAsk } from '../lib/pendingAsk'
import type { PageProps } from './types'
import './HomePage.css'

/**
 * Home — Claude-style: a centred greeting and composer until the first
 * message, then the composer drops to a pinned bottom bar above a scrolling
 * conversation, with an Export / New conversation bar above the log. The chat
 * itself (streaming, tool tags, retry, save, feedback, follow-ups, workflow
 * suggestions) is the same governed assistant as the legacy
 * `renderAssistant()` panel — there's no separate Assistant tab; this page
 * replaces it entirely, so there is exactly one place to have this
 * conversation, not two.
 *
 * A persistent left rail lists past conversations (open and closed),
 * matching claude.ai's own layout — previously the only way to reach an old
 * conversation was a link out to the legacy console's history page, which
 * this replaces. Opening one, whether from this rail or from History's own
 * preview list, goes through the exact same path: `navigate('home', sessionId)`
 * sets `/home/<id>` in the URL, and the effect below is the one place that
 * turns that id into an actually-open conversation (`chat.openConversation`,
 * backend/chat.py's `_chat_resume` — a closed session gets cloned into a
 * fresh open one rather than reopened, so the URL is corrected to the clone's
 * id once that happens).
 *
 * The composer (`components/chat/Composer.tsx`) and the quick-ask picker
 * (`components/chat/QuickAsks.tsx`) are pulled out into their own components
 * on general principle (state and markup shouldn't live jammed together in a
 * page file), not because anything else mounts them today.
 */

function greeting(now: Date): string {
  const hour = now.getHours()
  if (hour < 12) return 'Good morning'
  if (hour < 18) return 'Good afternoon'
  return 'Good evening'
}

/** Cycled through as a typewriter effect in the hero composer's placeholder —
 *  a plain-language taste of what to ask, in the same spirit as the {blank}
 *  templates in QuickAsks but written out so they read naturally as they
 *  type. */
const PLACEHOLDER_EXAMPLES = [
  'e.g. Show recent orders for Acme Dental',
  'e.g. Find the customer jane@acmedental.com',
  'e.g. What did Acme Dental spend this year?',
  'e.g. Show overdue invoices for Acme Dental',
  'e.g. Look up invoice INV-2024-0192',
  'e.g. Find customers in the Pacific Northwest',
]

function HomePage({ session, navigate, routeParam }: PageProps) {
  const toast = useToast()
  const chat = useChat(session.name)
  const savedAsks = useStoredList<string>('gov_quickasks', session.name)
  const composer = useComposer({ chat, toast, savedAsks })
  const conversations = useConversationHistory(session.name)
  const railCollapse = useCollapsed('gov_home_rail_collapsed')

  const inThread = chat.messages.length > 0 || chat.loadingHistory

  // A quick ask or a chip elsewhere in the app queued a question (setPendingAsk)
  // and sent the user here. `takePendingAsk` clears the value on read, so `ask`
  // reappearing in this
  // array on every render (it closes over `chat`, a new object each render) is
  // harmless: every call after the first genuinely queued one is a no-op
  // against an already-empty queue. Destructured rather than `composer.ask`
  // directly so the effect depends on the (stable) function alone, not the
  // (unmemoized, every-render-new) composer object it hangs off.
  const { ask } = composer
  useEffect(() => {
    const queued = takePendingAsk()
    if (queued) ask(queued)
  }, [ask])

  // Opens the conversation named in the URL (`/home/<id>`), whether it got
  // there from this page's own rail or from History's preview list. Guarded
  // by a ref (not just the `routeParam !== conversationId` check) so React
  // 18 Strict Mode's double-invoked effects can't fire `resumeChatHistory`
  // twice for the same id — the second call, if the first happened to
  // target a CLOSED session, would clone it a second time.
  const [openingId, setOpeningId] = useState<string | null>(null)
  const openedForRef = useRef<string | null>(null)
  useEffect(() => {
    if (!routeParam || routeParam === chat.conversationId || openedForRef.current === routeParam) return
    openedForRef.current = routeParam
    setOpeningId(routeParam)
    chat
      .openConversation(routeParam)
      .then((result) => {
        if (result.cloned) toast.warn('That conversation had closed — continuing as a new one')
        // A clone opens under a NEW id; point the URL at it so a reload (or
        // a copied link) lands on the conversation actually now open, not
        // the closed one that was asked for.
        if (result.conversation_id !== routeParam) navigate('home', result.conversation_id)
      })
      .catch((cause: unknown) => {
        toast.error(cause instanceof ApiError ? cause.message : 'Could not open that conversation.')
        navigate('home')
      })
      .finally(() => setOpeningId(null))
  }, [routeParam, chat, navigate, toast])

  // The rail's own list goes stale the moment the active conversation
  // changes (a fresh id becoming real on its first reply, or switching to a
  // different past conversation) — cheap enough to just refetch rather than
  // patch it in client-side.
  const { reload: reloadConversations } = conversations
  const conversationIdForReload = chat.conversationId
  useEffect(() => {
    reloadConversations()
    // Deliberately NOT keying this on `reloadConversations` itself (a new
    // function every render) — only a genuine id change should refetch.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [conversationIdForReload])

  const openConversationFromRail = useCallback(
    (sessionId: string) => navigate('home', sessionId),
    [navigate],
  )

  const startNewConversation = useCallback(() => {
    chat.newConversation()
    navigate('home')
  }, [chat, navigate])

  const exportConversation = useCallback(() => {
    const text = chat.exportText()
    if (!text) {
      toast.warn('Nothing to export yet')
      return
    }
    downloadText('conversation.txt', text)
  }, [chat, toast])

  const mainContent = inThread ? (
    <div className="home home--thread">
      {/* Hidden along with the composer while a resumed conversation's
          history is still loading — same reasoning: the page doesn't yet
          know if this is a real thread or an empty one about to fall back
          to the hero, and Export/New conversation on a thread that might not
          exist would just flash. */}
      {!chat.loadingHistory && (
        <div className="home-thread-bar">
          <Button variant="ghost" size="sm" onClick={exportConversation}>
            Export
          </Button>
          <Button variant="ghost" size="sm" onClick={startNewConversation}>
            + New conversation
          </Button>
        </div>
      )}

      <ChatLog
        messages={chat.messages}
        loadingHistory={chat.loadingHistory}
        owner={session.name}
        onRetry={chat.retry}
        onRate={chat.rate}
        onFollowup={composer.ask}
        navigate={navigate}
      />

      {/* Hidden while a resumed conversation's history is still loading — the
          page doesn't yet know whether this is a real thread or an empty one
          that's about to fall back to the hero, so showing (and then possibly
          re-hiding) the composer here would just flash. */}
      {!chat.loadingHistory && (
        <div className="home-thread-composer">
          <Composer composer={composer} busy={chat.busy} idPrefix="home-ask" />
        </div>
      )}
    </div>
  ) : (
    <div className="home">
      <section className="home-hero">
        <h1 className="home-greeting">
          <span className="home-mark" aria-hidden="true">
            <svg viewBox="0 0 24 24" width="24" height="24">
              <path
                d="M12 2v20M2 12h20M4.9 4.9l14.2 14.2M19.1 4.9L4.9 19.1"
                stroke="currentColor"
                strokeWidth="2.2"
                strokeLinecap="round"
                fill="none"
              />
            </svg>
          </span>
          {greeting(new Date())}, {firstName(session.name)}
        </h1>
        <p className="home-sub">
          Ask about customers, orders, shipments, and invoices — in plain language.
        </p>

        <Composer
          composer={composer}
          busy={chat.busy}
          idPrefix="home-ask"
          typewriterExamples={PLACEHOLDER_EXAMPLES}
        />

        {/* Directly under the composer, because clicking a chip puts its text
            INTO the composer — any gap between the two hides the effect of the
            click. */}
        <div className="home-suggest">
          <QuickAsks owner={session.name} onPick={composer.applyTemplate} center />
        </div>

        {/* Recent activity and pinned answers now live on their own tab. */}
        <p className="home-elsewhere">
          <Button variant="quiet" size="sm" onClick={() => navigate('history')}>
            Recent activity &amp; saved answers →
          </Button>
        </p>
      </section>
    </div>
  )

  return (
    <div className="home-shell">
      {/* The clipping window onto the rail: THIS animates (width), while
          `.home-rail` itself keeps a fixed width so its contents never
          reflow/squeeze mid-collapse — same technique as the app shell's
          own sidebar collapse. */}
      <div className="home-rail-outer" data-collapsed={railCollapse.collapsed || undefined}>
        <aside className="home-rail">
          <div className="home-rail-head">
            <span className="ui-eyebrow">Conversations</span>
            <Button variant="ghost" size="sm" onClick={startNewConversation}>
              + New
            </Button>
          </div>
          <div className="home-rail-scroll">
            <ConversationRail
              sessions={conversations.sessions}
              state={conversations.state}
              activeId={chat.conversationId}
              onSelect={openConversationFromRail}
              openingId={openingId}
              dense
            />
          </div>
        </aside>
      </div>

      {/* Docked to the rail's edge, not inside it (see CollapseToggle's own
          comment) — a sibling of the clipping wrapper above, so it's never
          subject to that wrapper's `overflow: hidden`. Shown regardless of
          hero/thread state, so the rail can always be brought back. */}
      <CollapseToggle
        collapsed={railCollapse.collapsed}
        onToggle={railCollapse.toggle}
        label="Conversations"
        className="home-rail-toggle"
      />

      <div className="home-main">{mainContent}</div>
    </div>
  )
}

export default HomePage
