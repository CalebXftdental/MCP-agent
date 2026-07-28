import { useEffect, useRef } from 'react'
import { Skeleton } from '../ui'
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
}

/**
 * Scrollable turn history. Auto-scrolls to the newest content as a reply
 * streams in, matching `log.scrollTop = log.scrollHeight` after every DOM
 * write in the legacy version — but only while already at the bottom, so
 * scrolling up to reread an earlier answer isn't yanked back down by the next
 * streamed token.
 */
function ChatLog({ messages, loadingHistory, owner, onRetry, onRate, onFollowup, navigate }: ChatLogProps) {
  const scrollRef = useRef<HTMLDivElement>(null)
  const pinnedToBottom = useRef(true)

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
      {loadingHistory ? (
        <div className="chat-log-loading">
          <Skeleton height="2.4rem" width="60%" index={0} />
          <Skeleton height="3.6rem" width="85%" index={1} />
        </div>
      ) : (
        messages.map((message) => (
          <ChatMessageBubble
            key={message.id}
            message={message}
            owner={owner}
            onRetry={onRetry}
            onRate={onRate}
            onFollowup={onFollowup}
            navigate={navigate}
          />
        ))
      )}
    </div>
  )
}

export default ChatLog
