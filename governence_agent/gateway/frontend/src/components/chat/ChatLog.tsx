import { useEffect, useRef } from 'react'
import { Skeleton, usePresence } from '../ui'
import type { ChatMessage } from '../../hooks/useChat'
import ChatMessageBubble from './ChatMessageBubble'
import './ChatLog.css'

export interface ChatLogProps {
  messages: ChatMessage[]
  loadingHistory: boolean
  owner: string | null
  onRetry: (id: string) => void
  onRate: (id: string, rating: 'up' | 'down') => void
  onFollowup: (text: string) => void
  navigate: (key: 'workflows' | 'files') => void
  /** See ChatMessageBubble — defaults true (Home's existing behavior). */
  showWorkflowSuggestions?: boolean
  /** See ChatMessageBubble — defaults true (Home's existing behavior). */
  showFollowupChips?: boolean
}

/** How long the skeleton takes to fade out once history has loaded. */
const SKELETON_EXIT_MS = 200

/**
 * Scrollable turn history. Auto-scrolls to the newest content as a reply
 * streams in, matching `log.scrollTop = log.scrollHeight` after every DOM
 * write in the legacy version — but only while already at the bottom, so
 * scrolling up to reread an earlier answer isn't yanked back down by the next
 * streamed token.
 *
 * The loading skeleton crossfades out rather than being swapped for real
 * content in the same frame: it's absolutely positioned over the log while
 * `usePresence` keeps it mounted for its exit transition, so it fades away on
 * top of the messages (which are rendering — and fading in themselves —
 * underneath) instead of popping out of existence the instant history arrives.
 */
function ChatLog({
  messages,
  loadingHistory,
  owner,
  onRetry,
  onRate,
  onFollowup,
  navigate,
  showWorkflowSuggestions,
  showFollowupChips,
}: ChatLogProps) {
  const scrollRef = useRef<HTMLDivElement>(null)
  const pinnedToBottom = useRef(true)
  const { mounted: skeletonMounted, state: skeletonState } = usePresence(loadingHistory, SKELETON_EXIT_MS)

  useEffect(() => {
    const el = scrollRef.current
    if (el && pinnedToBottom.current) el.scrollTop = el.scrollHeight
  })

  const onScroll = () => {
    const el = scrollRef.current
    if (!el) return
    pinnedToBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 48
  }

  return (
    <div className="chat-log" ref={scrollRef} onScroll={onScroll}>
      {messages.map((message) => (
        <ChatMessageBubble
          key={message.id}
          message={message}
          owner={owner}
          onRetry={onRetry}
          onRate={onRate}
          onFollowup={onFollowup}
          navigate={navigate}
          showWorkflowSuggestions={showWorkflowSuggestions}
          showFollowupChips={showFollowupChips}
        />
      ))}

      {skeletonMounted && (
        <div className="chat-log-loading" data-state={skeletonState}>
          <Skeleton height="2.4rem" width="60%" index={0} />
          <Skeleton height="3.6rem" width="85%" index={1} />
        </div>
      )}
    </div>
  )
}

export default ChatLog
