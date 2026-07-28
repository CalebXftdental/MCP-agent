import { useMemo, useState } from 'react'
import { BarList, Chip, TypingDots, useToast } from '../ui'
import { renderMarkdown, chartFromTable, type MarkdownTable } from '../../lib/markdown'
import { useStoredList } from '../../hooks/useStoredList'
import { followupsFor, type ChatMessage } from '../../hooks/useChat'
import MessageActions from './MessageActions'
import WorkflowSuggestions from './WorkflowSuggestions'
import './ChatMessageBubble.css'

/**
 * One turn's bubble, plus everything that hangs off a completed bot reply:
 * the tool-usage tag, action bar, inline bar chart, and workflow suggestions.
 * Ports the `add()` / `thinking()` / tool-tag rendering from `renderAssistant()`.
 */

export interface ChatMessageBubbleProps {
  message: ChatMessage
  owner: string | null
  onRetry: (id: string) => void
  onRate: (id: string, rating: 'up' | 'down') => void
  /** A follow-up chip or a canned action was clicked — sends it as the next
   *  turn's question. */
  onFollowup: (text: string) => void
  navigate: (key: 'workflows' | 'files') => void
}

function ChatMessageBubble({
  message,
  owner,
  onRetry,
  onRate,
  onFollowup,
  navigate,
}: ChatMessageBubbleProps) {
  const toast = useToast()
  const savedAnswers = useStoredList<{ text: string; ts: number }>('gov_savedans', owner)
  const [chartOpen, setChartOpen] = useState(false)

  const { nodes, tables } = useMemo(
    () => renderMarkdown(message.role === 'bot' ? message.text : ''),
    [message.role, message.text],
  )

  if (message.role === 'user') {
    return (
      <div className="chat-msg chat-msg--me">
        <p className="chat-msg-text">{message.text}</p>
      </div>
    )
  }

  const isPending = message.status === 'tools' || message.status === 'answer'
  const isError = message.status === 'error'
  const isDone = message.status === 'done'

  const save = () => {
    savedAnswers.add({ text: message.text, ts: Date.now() })
    toast.success('Saved to your answers')
  }

  return (
    <div className="chat-msg chat-msg--bot">
      {isPending && message.status === 'tools' && !message.text ? (
        message.toolsUsed?.length ? (
          <p className="chat-msg-tool-line">using: {message.toolsUsed.join(', ')}…</p>
        ) : (
          <TypingDots />
        )
      ) : isError ? (
        <p className="chat-msg-error">⚠ {message.errorMessage}</p>
      ) : (
        <div className="chat-msg-body">{nodes}</div>
      )}

      {isDone && (
        <>
          {chartOpen && tables[0] && <InlineChart table={tables[0]} />}
          <MessageActions
            reply={message.text}
            tables={tables}
            onRetry={() => onRetry(message.id)}
            onSave={save}
            feedback={message.feedback}
            onRate={(rating) => onRate(message.id, rating)}
            chartOpen={chartOpen}
            onToggleChart={() => setChartOpen((v) => !v)}
          />
          {message.toolsUsed && message.toolsUsed.length > 0 && (
            <p className="chat-msg-tool-line chat-msg-tool-line--done">
              used: {message.toolsUsed.join(', ')}
            </p>
          )}
        </>
      )}

      {isError && (
        <div className="msg-actions">
          <button type="button" onClick={() => onRetry(message.id)}>
            ↻ Retry
          </button>
        </div>
      )}

      {isDone && message.question && (
        <>
          <FollowupChips question={message.question} reply={message.text} onPick={onFollowup} />
          <WorkflowSuggestions question={message.question} reply={message.text} navigate={navigate} />
        </>
      )}
    </div>
  )
}

/** Canned actions plus reply-specific suggestions — `addFollowups()`. Clicking
 *  one sends it as the next question, same as clicking a quick ask. */
function FollowupChips({
  question,
  reply,
  onPick,
}: {
  question: string
  reply: string
  onPick: (text: string) => void
}) {
  const chips = useMemo(() => followupsFor(question, reply), [question, reply])
  return (
    <div className="chat-followups">
      {chips.map((chip) => (
        <Chip key={chip} size="sm" onClick={() => onPick(chip)}>
          {chip}
        </Chip>
      ))}
    </div>
  )
}

/** The reply's bar chart toggle — `chartFromTable()` + the legacy `_hbars()`
 *  rendering, reusing the kit's BarList instead of hand-built markup. */
function InlineChart({ table }: { table: MarkdownTable }) {
  const items = useMemo(() => chartFromTable(table), [table])
  if (items.length === 0) return null
  return (
    <div className="chat-inline-chart">
      <BarList items={items.map((i) => ({ label: i.k, value: i.n }))} />
    </div>
  )
}

export default ChatMessageBubble
