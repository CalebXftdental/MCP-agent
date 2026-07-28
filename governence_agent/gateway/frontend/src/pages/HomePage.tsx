import { useCallback, useEffect, useRef, useState } from 'react'
import type { KeyboardEvent } from 'react'
import { Button, Chip, useToast } from '../components/ui'
import ChatLog from '../components/chat/ChatLog'
import { useChat } from '../hooks/useChat'
import { useStoredList } from '../hooks/useStoredList'
import { downloadText } from '../lib/download'
import { firstName } from '../lib/format'
import { takePendingAsk } from '../lib/pendingAsk'
import type { PageProps } from './types'
import './HomePage.css'

/**
 * Home — Claude-style: a centred greeting and composer until the first
 * message, then the composer drops to a pinned bottom bar above a scrolling
 * conversation. The chat itself (streaming, tool tags, retry, save, feedback,
 * follow-ups, workflow suggestions) is the same governed assistant as the
 * legacy `renderAssistant()` panel — ported in full, not re-imagined:
 *
 *   - `/chat/stream` first, falling back to `/chat` on any failure
 *     (`lib/chatStream.ts`, replacing `streamOrChat()`)
 *   - the same markdown subset, headings/lists/tables/code
 *     (`lib/markdown.tsx`, replacing `mdToHtml()`)
 *   - copy / CSV / chart / retry / save / thumbs on every reply
 *     (`components/chat/MessageActions.tsx`, replacing `actionsBar()`)
 *   - canned + reply-specific follow-up chips (`followupsFor()`, replacing
 *     `addFollowups()`)
 *   - "run this as a workflow" suggestions (`WorkflowSuggestions.tsx`,
 *     replacing `addWorkflowSuggestions()`)
 *   - resuming a reloaded conversation's transcript, export, and starting a new
 *     conversation
 *
 * Quick asks are the pre-chat empty state, same as before: click one to load it
 * into the composer (filling its first `{blank}` selection), or "+N more" for
 * the full grouped list. Once a conversation has messages, that empty-state UI
 * steps aside for the thread — starting over is one click on "New
 * conversation".
 */

interface QuickAskGroup {
  label: string
  items: string[]
}

/** The built-in starters, verbatim from `QUICK_STARTERS`. */
const STARTERS: QuickAskGroup[] = [
  {
    label: 'Customers',
    items: [
      'Find the customer {name or email}',
      'Show a 360° overview of customer {customer id}',
      'Find customers in {region}',
    ],
  },
  {
    label: 'Orders',
    items: [
      'Show recent orders for {customer}',
      'Get details for order {order number}',
      'What did {customer} spend this year?',
    ],
  },
  {
    label: 'Invoices & finance',
    items: ['Show overdue invoices for {customer}', 'Look up invoice {invoice number}'],
  },
]

/** Chips shown before "+N more" is pressed. Five fits one row at the composer's
 *  width without wrapping on a laptop. */
const COLLAPSED = 5

const PLACEHOLDER = /\{([^}]+)\}/

interface Blank {
  start: number
  end: number
  name: string
}

function firstBlank(text: string): Blank | null {
  const match = PLACEHOLDER.exec(text)
  if (!match || match.index === undefined) return null
  return { start: match.index, end: match.index + match[0].length, name: match[1] }
}

function greeting(now: Date): string {
  const hour = now.getHours()
  if (hour < 12) return 'Good morning'
  if (hour < 18) return 'Good afternoon'
  return 'Good evening'
}

function HomePage({ session, navigate }: PageProps) {
  const toast = useToast()
  const chat = useChat(session.name)

  const [draft, setDraft] = useState('')
  const composerRef = useRef<HTMLTextAreaElement>(null)
  // Bumped to ask the effect below to focus the composer and select its first
  // blank. A counter rather than a boolean, so two clicks on the same chip both
  // re-select instead of the second being a no-op.
  const [focusTick, setFocusTick] = useState(0)
  const [showAll, setShowAll] = useState(false)

  const savedAsks = useStoredList<string>('gov_quickasks', session.name)

  const inThread = chat.messages.length > 0 || chat.loadingHistory

  useEffect(() => {
    if (focusTick === 0) return
    const el = composerRef.current
    if (!el) return
    el.focus()
    const blank = firstBlank(el.value)
    if (blank) el.setSelectionRange(blank.start, blank.end)
    else el.setSelectionRange(el.value.length, el.value.length)
  }, [focusTick])

  // Grows with its content up to the max-height in CSS, then scrolls. Reset to
  // 'auto' first, or the height only ever ratchets upward.
  useEffect(() => {
    const el = composerRef.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = `${el.scrollHeight}px`
  }, [draft])

  const ask = useCallback(
    (text: string) => {
      const trimmed = text.trim()
      if (!trimmed) return

      const blank = firstBlank(trimmed)
      if (blank) {
        setDraft(trimmed)
        setFocusTick((n) => n + 1)
        toast.warn(`Fill in ${blank.name} before asking`)
        return
      }

      chat.send(trimmed)
      setDraft('')
    },
    [chat, toast],
  )

  // A quick ask or a chip elsewhere in the app queued a question (setPendingAsk)
  // and sent the user here — the legacy handoff target was the Assistant tab;
  // Home fills that role now, so it's the one consuming the queue.
  // `takePendingAsk` clears the value on read, so `ask` reappearing in this
  // array on every render (it closes over `chat`, which is a new object each
  // render) is harmless: every call after the first genuinely queued one is a
  // no-op against an already-empty queue.
  useEffect(() => {
    const queued = takePendingAsk()
    if (queued) ask(queued)
  }, [ask])

  const applyTemplate = useCallback((template: string) => {
    setDraft(template)
    setFocusTick((n) => n + 1)
  }, [])

  const saveDraft = useCallback(() => {
    const text = draft.trim()
    if (!text) {
      toast.warn('Type an ask first, then save it')
      return
    }
    savedAsks.add(text)
    toast.success('Saved to Quick asks')
  }, [draft, savedAsks, toast])

  const exportConversation = useCallback(() => {
    const text = chat.exportText()
    if (!text) {
      toast.warn('Nothing to export yet')
      return
    }
    downloadText('conversation.txt', text)
  }, [chat, toast])

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    // Enter asks, Shift+Enter is a newline — the convention for a multi-line
    // composer. The legacy single-line input had no way to write a two-line
    // question at all.
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      ask(draft)
    }
  }

  const savedSet = new Set(savedAsks.items)
  const starters = STARTERS.flatMap((group) => group.items)
  // Saved asks first: they are this user's own, so they earn the visible row.
  const collapsed = [...savedAsks.items, ...starters].slice(0, COLLAPSED)
  const hidden = savedAsks.items.length + starters.length - collapsed.length

  const chipFor = (item: string) => {
    const saved = savedSet.has(item)
    return (
      <Chip
        key={item}
        size="sm"
        maxWidth="20rem"
        onClick={() => applyTemplate(item)}
        onRemove={saved ? () => savedAsks.remove((entry) => entry === item) : undefined}
        removeLabel={`Remove saved ask: ${item}`}
        icon={saved ? '★' : undefined}
        title={item}
      >
        {item}
      </Chip>
    )
  }

  const pendingBlank = firstBlank(draft)

  const composer = (
    <form
      className="home-composer"
      onSubmit={(e) => {
        e.preventDefault()
        ask(draft)
      }}
    >
      <label className="ui-sr-only" htmlFor="home-ask">
        Ask the governed assistant
      </label>
      <textarea
        id="home-ask"
        ref={composerRef}
        className="home-composer-input"
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={onKeyDown}
        placeholder="e.g. Show recent orders for Acme Dental"
        rows={1}
        autoComplete="off"
        autoFocus
        spellCheck
      />

      <div className="home-composer-foot">
        <p className="home-composer-hint">
          {pendingBlank ? (
            <span className="home-composer-blank">
              Replace <code>{`{${pendingBlank.name}}`}</code> before asking
            </span>
          ) : (
            <>
              <kbd>Enter</kbd> to ask · <kbd>Shift</kbd>+<kbd>Enter</kbd> for a new line
            </>
          )}
        </p>
        <div className="home-composer-actions">
          <Button
            type="button"
            variant="ghost"
            size="sm"
            onClick={saveDraft}
            title="Save this ask to Quick asks"
            aria-label="Save this ask to Quick asks"
          >
            ★
          </Button>
          <Button type="submit" disabled={draft.trim().length === 0 || chat.busy} loading={chat.busy} icon="↑">
            Ask
          </Button>
        </div>
      </div>
    </form>
  )

  if (inThread) {
    return (
      <div className="home home--thread">
        <div className="home-thread-bar">
          <Button variant="ghost" size="sm" onClick={exportConversation}>
            Export
          </Button>
          <Button variant="ghost" size="sm" onClick={chat.newConversation}>
            + New conversation
          </Button>
        </div>

        <ChatLog
          messages={chat.messages}
          loadingHistory={chat.loadingHistory}
          owner={session.name}
          onRetry={chat.retry}
          onRate={chat.rate}
          onFollowup={ask}
          navigate={navigate}
        />

        <div className="home-thread-composer">{composer}</div>
      </div>
    )
  }

  return (
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

        {composer}

        {/* Directly under the composer, because clicking a chip puts its text
            INTO the composer — any gap between the two hides the effect of the
            click. One row by default; the grouped list is one press away. */}
        <div className="home-suggest">
          {/* The collapsed row gives way to the toggle when expanded, rather than
              staying above the grouped list — otherwise the first five chips
              appear twice. */}
          <div className="home-chips">
            {!showAll && collapsed.map(chipFor)}
            {(hidden > 0 || showAll) && (
              <Button
                variant="quiet"
                size="sm"
                onClick={() => setShowAll((open) => !open)}
                aria-expanded={showAll}
                aria-controls="home-all-asks"
              >
                {showAll ? 'Show fewer' : `+${hidden} more`}
              </Button>
            )}
          </div>

          {/* Always in the DOM, `hidden` when collapsed: aria-controls has to
              point at something that exists, and a disclosure whose target only
              appears after the press announces nothing on the way in. */}
          <div className="home-all-asks" id="home-all-asks" hidden={!showAll}>
            {savedAsks.items.length > 0 && (
              <div className="home-ask-group">
                <p className="ui-eyebrow">★ Your saved</p>
                <div className="home-chips">{savedAsks.items.map(chipFor)}</div>
              </div>
            )}

            {STARTERS.map((group) => (
              <div className="home-ask-group" key={group.label}>
                <p className="ui-eyebrow">{group.label}</p>
                <div className="home-chips">{group.items.map(chipFor)}</div>
              </div>
            ))}

            <p className="home-suggest-note">Save your own with ★, or type your own above</p>
          </div>
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
}

export default HomePage
