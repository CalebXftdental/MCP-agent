/**
 * The signed-in principal's past assistant conversations — open and closed,
 * newest-first (already sorted server-side). Shared by Home's conversation
 * rail and History's "Past conversations" preview, so both list the exact
 * same data through the exact same fetch/loading/error handling rather than
 * two independent copies drifting apart.
 */

import { useCallback, useEffect, useState } from 'react'
import { getChatHistoryList, type ChatSessionSummary } from '../lib/api'

export interface UseConversationHistoryResult {
  sessions: ChatSessionSummary[]
  state: 'loading' | 'ready' | 'error'
  reload: () => void
}

export function useConversationHistory(owner: string | null): UseConversationHistoryResult {
  const [sessions, setSessions] = useState<ChatSessionSummary[]>([])
  const [state, setState] = useState<'loading' | 'ready' | 'error'>('loading')

  const load = useCallback(() => {
    let live = true
    setState('loading')

    getChatHistoryList()
      .then((result) => {
        if (!live) return
        setSessions(result.sessions ?? [])
        setState('ready')
      })
      .catch(() => {
        if (!live) return
        setState('error')
      })

    return () => {
      live = false
    }
  }, [])

  // Keyed on the principal too: after a sign-in/account switch the previous
  // user's conversations must not linger on screen.
  useEffect(() => load(), [load, owner])

  return { sessions, state, reload: load }
}
