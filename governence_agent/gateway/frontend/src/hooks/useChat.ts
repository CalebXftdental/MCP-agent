/**
 * The governed assistant conversation. Ports the message-handling half of
 * `renderAssistant()` in static/app.html — everything except the DOM building,
 * which components now own.
 *
 * One behavioural difference from the legacy `send()`: there, `retry` called
 * `send(q)` again, which appended an entirely new user bubble with the same
 * text right below the failed answer — so retrying a failed ask left two
 * identical questions in the log, back to back. Here `retry` replaces the
 * failed turn in place instead of appending a duplicate. Everything else —
 * streaming updates, tool tags, follow-ups, workflow suggestions, feedback,
 * save-to-pinned, export — is unchanged in behavior.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  ApiError,
  getChatTranscript,
  resumeChatHistory,
  sendChatFeedback,
  type ChatResumeResult,
  type ChatToolCall,
} from '../lib/api'
import { streamOrChat, type ChatKind } from '../lib/chatStream'
import { useConversationId } from './useConversationId'

export type MessageStatus = 'tools' | 'answer' | 'done' | 'error'

export interface ChatMessage {
  id: string
  role: 'user' | 'bot'
  text: string
  status: MessageStatus
  /** Set once the turn finishes, for the tool-usage tag under a bot reply. */
  toolsUsed?: string[]
  /** Same turn's tool calls, WITH their args — toolsUsed above is names-only
   *  (all ChatMessageBubble needs); this is for callers that need to act on
   *  what a specific call actually did, e.g. MyWorkflowsPage animating a
   *  propose_graph call's proposed nodes/edges once the turn completes. */
  toolCalls?: ChatToolCall[]
  /** The question this reply answers — Retry and the workflow-suggestion
   *  fetch both need it, and a bot message doesn't otherwise carry it. */
  question?: string
  feedback?: 'up' | 'down'
  errorMessage?: string
}

/** Client-only id — never sent to the server, just a React key and a target for
 *  in-place updates while a turn streams. */
function messageId(): string {
  return `m-${Math.random().toString(36).slice(2)}${Date.now().toString(36)}`
}

/** The three canned follow-ups shown after every reply, from `LLM_ACTIONS`. */
export const LLM_ACTIONS = ['Summarize that', 'Explain simply', 'Show as a table']

/**
 * Keyword heuristic for reply-specific follow-ups — `suggestFollowups()`,
 * unchanged. Deliberately not an LLM call: these appear the instant the answer
 * does, and a second model round-trip just to guess a follow-up would be a
 * worse trade than four regexes.
 */
export function suggestFollowups(question: string, reply: string): string[] {
  const text = `${question} ${reply}`.toLowerCase()
  const suggestions: string[] = []
  if (/customer|client|account/.test(text)) suggestions.push('Show their recent orders')
  if (/\border|orders\b/.test(text)) suggestions.push('Show the line items')
  if (/invoice|overdue|balance|owe/.test(text)) suggestions.push('Which invoices are overdue?')
  if (/spend|total|revenue|sales/.test(text)) suggestions.push('Break it down by month')
  if (suggestions.length === 0) suggestions.push('Summarize that', 'Show more detail')
  return suggestions.slice(0, 3)
}

/** LLM_ACTIONS first, then the heuristic ones, deduplicated, capped at 5 —
 *  exactly `[...LLM_ACTIONS, ...suggestFollowups(q,reply)].filter(dedupe).slice(0,5)`. */
export function followupsFor(question: string, reply: string): string[] {
  const combined = [...LLM_ACTIONS, ...suggestFollowups(question, reply)]
  return combined.filter((item, i) => combined.indexOf(item) === i).slice(0, 5)
}

export interface UseChatResult {
  messages: ChatMessage[]
  /** True while a turn is in flight — disables the composer's send control. */
  busy: boolean
  /** Loading the transcript for a resumed conversation id (page load, not a
   *  fresh 'New conversation'). */
  loadingHistory: boolean
  conversationId: string
  send: (text: string) => void
  /** Re-runs a failed turn's question in place. */
  retry: (messageId: string) => void
  rate: (messageId: string, rating: 'up' | 'down') => void
  newConversation: () => void
  /** The full transcript as plain text, for the Export action. Null until at
   *  least one turn has completed. */
  exportText: () => string | null
  /** Switches the active thread to an existing conversation, opened from a
   *  past-conversations list (Home's rail or History's preview) — the one
   *  path both go through, so opening a conversation behaves identically no
   *  matter where it was clicked. Resolves with the id actually opened (which
   *  differs from `sessionId` when the requested session was closed and got
   *  cloned); the caller updates the URL to match. Rejects on a failed/unknown
   *  id — the caller decides how to surface that. */
  openConversation: (sessionId: string) => Promise<ChatResumeResult>
}

/** `kind` picks which governed loop/session-id namespace a turn goes through
 *  — 'home' (default, unchanged) is the general assistant; 'workflow' is the
 *  "My Workflow" builder's own copilot (see chatStream.ts's ChatKind). Both
 *  reuse every other mechanic here (streaming, retry, history, feedback)
 *  unchanged — only which endpoint/session bucket a turn lands in differs. */
export function useChat(owner: string | null, kind: ChatKind = 'home'): UseChatResult {
  const conv = useConversationId(owner, kind === 'workflow' ? 'workflow' : '')
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [busy, setBusy] = useState(false)
  const [loadingHistory, setLoadingHistory] = useState(false)
  // Guards a stream callback from writing into a message that a later retry or
  // a conversation switch has already superseded.
  const activeTurn = useRef<string | null>(null)

  // Always current, read (not depended on) by the history-load effect below —
  // see the effect's comment for why it must key on generation alone.
  const convRef = useRef(conv)
  convRef.current = conv

  const patch = useCallback((id: string, fields: Partial<ChatMessage>) => {
    setMessages((list) => list.map((m) => (m.id === id ? { ...m, ...fields } : m)))
  }, [])

  // Load history once per THREAD (mount, owner resolving, or an explicit
  // `newConversation()`) — keyed on `generation`, not `conv.id`, so a
  // mid-conversation server rename never re-fires this and overwrites the
  // messages already on screen with an empty transcript for the new id. Skips
  // the request entirely for a freshly-minted id: it cannot have history yet,
  // and hitting the endpoint anyway just to get a 404 was wasted latency in
  // the legacy panel.
  useEffect(() => {
    if (!convRef.current.isResumed) {
      setMessages([])
      return
    }

    let live = true
    setLoadingHistory(true)

    getChatTranscript(convRef.current.id)
      .then((transcript) => {
        if (!live) return
        // A live turn stamps `question` on the bot message the instant it's
        // created (see `send`) — a resumed transcript has to reconstruct the
        // same thing from the preceding user turn, or every historical reply
        // silently loses its follow-up chips, its workflow-suggestion card,
        // and Retry (which bails with no `question` to resend).
        let lastQuestion = ''
        const loaded: ChatMessage[] = (transcript.messages ?? []).map((m) => {
          if (m.role === 'user') {
            lastQuestion = m.content
            return { id: messageId(), role: 'user', text: m.content, status: 'done' }
          }
          return {
            id: messageId(),
            role: 'bot',
            text: m.content,
            status: 'done',
            toolsUsed: m.tools_used ?? undefined,
            question: lastQuestion || undefined,
          }
        })
        setMessages(loaded)
      })
      .catch((cause: unknown) => {
        // A 404 just means this id has no history yet, despite being marked
        // resumed (e.g. the stored id was never actually sent). Anything else
        // is swallowed too: the legacy panel treated a failed history load as
        // "start fresh" as well, and there's nothing actionable to show here.
        if (!live) return
        if (!(cause instanceof ApiError) || cause.status !== 404) {
          console.warn('chat history load failed', cause)
        }
      })
      .finally(() => {
        if (live) setLoadingHistory(false)
      })

    return () => {
      live = false
    }
  }, [conv.generation])

  const runTurn = useCallback(
    (question: string, botId: string) => {
      const turnToken = botId
      activeTurn.current = turnToken

      setBusy(true)
      streamOrChat(question, conv.id, (update) => {
        if (activeTurn.current !== turnToken) return
        patch(botId, {
          text: update.text,
          status: update.phase,
          toolsUsed: update.tools.length ? update.tools : undefined,
        })
      }, kind)
        .then((result) => {
          if (activeTurn.current !== turnToken) return
          if (result.conversationId !== conv.id) conv.set(result.conversationId)
          const toolNames = result.tools.map((t: ChatToolCall) => t.tool)
          patch(botId, {
            text: result.reply || '(no reply)',
            status: 'done',
            toolsUsed: toolNames.length ? toolNames : undefined,
            toolCalls: result.tools.length ? result.tools : undefined,
          })
        })
        .catch((cause: unknown) => {
          if (activeTurn.current !== turnToken) return
          patch(botId, {
            status: 'error',
            errorMessage: cause instanceof Error ? cause.message : 'Something went wrong',
          })
        })
        .finally(() => {
          if (activeTurn.current === turnToken) {
            activeTurn.current = null
            setBusy(false)
          }
        })
    },
    [conv, patch, kind],
  )

  const send = useCallback(
    (text: string) => {
      const question = text.trim()
      if (!question || busy) return

      const userId = messageId()
      const botId = messageId()
      setMessages((list) => [
        ...list,
        { id: userId, role: 'user', text: question, status: 'done' },
        { id: botId, role: 'bot', text: '', status: 'tools', question },
      ])
      runTurn(question, botId)
    },
    [busy, runTurn],
  )

  const retry = useCallback(
    (id: string) => {
      if (busy) return
      const target = messages.find((m) => m.id === id)
      if (!target?.question) return
      patch(id, { text: '', status: 'tools', errorMessage: undefined, feedback: undefined })
      runTurn(target.question, id)
    },
    [busy, messages, patch, runTurn],
  )

  const rate = useCallback(
    (id: string, rating: 'up' | 'down') => {
      patch(id, { feedback: rating })
      sendChatFeedback(conv.id, rating).catch(() => {
        // Best-effort, matching the legacy toast-only failure handling; the
        // optimistic UI state above already reflects the click.
      })
    },
    [conv.id, patch],
  )

  const newConversation = useCallback(() => {
    conv.reset()
    setMessages([])
  }, [conv])

  const openConversation = useCallback(
    async (sessionId: string): Promise<ChatResumeResult> => {
      const result = await resumeChatHistory(sessionId)
      // Bumps generation, so the history-loading effect above fetches and
      // renders this conversation's transcript — same mechanism a page
      // reload uses to restore the current thread, just pointed at a
      // different id.
      conv.open(result.conversation_id)
      return result
    },
    [conv],
  )

  const exportText = useCallback((): string | null => {
    if (messages.length === 0) return null
    return messages
      .filter((m) => m.text)
      .map((m) => `${m.role === 'user' ? 'You' : 'Assistant'}: ${m.text}`)
      .join('\n\n')
  }, [messages])

  return useMemo(
    () => ({
      messages,
      busy,
      loadingHistory,
      conversationId: conv.id,
      send,
      retry,
      rate,
      newConversation,
      exportText,
      openConversation,
    }),
    [messages, busy, loadingHistory, conv.id, send, retry, rate, newConversation, exportText, openConversation],
  )
}
