/**
 * The active conversation id, replacing `getActiveConv()`/`setActiveConv()`.
 *
 * sessionStorage, not a bare variable: it must survive a hard refresh — the
 * server keeps a conversation alive for `GOVERNANCE_CHAT_IDLE_SEC` (default 1h),
 * much longer than a SPA session, so losing the id on reload would orphan an
 * otherwise-live conversation.
 *
 * Namespaced by the signed-in principal, matching the legacy fix for a real bug
 * (2026-07): sessionStorage is scoped to the browser TAB, not to who's logged
 * in, and is never cleared on logout. Testing as account A and then signing
 * into account B in the same tab would otherwise hand B the SAME
 * conversation_id A was using — both users' messages landing in one session,
 * which on Cosmos surfaced as a spurious "forbidden" from a cross-partition,
 * owner-unknown lookup matching either user's same-id document.
 */

import { useCallback, useEffect, useState } from 'react'

const KEY = 'gov_active_conv'

/** `namespace` separates unrelated conversation threads that would otherwise
 *  collide on the same per-owner storage key — e.g. the "My Workflow" copilot
 *  (namespace 'workflow') keeping its own active-conversation id and history
 *  bucket distinct from the general Home assistant (namespace '', the
 *  original key shape, kept as-is so no existing session gets orphaned on
 *  deploy). Both still go through the exact same chat_log session storage on
 *  the server — only the id (and therefore which bucket it lands in) differs. */
function storageKey(owner: string | null, namespace: string): string {
  return namespace ? `${KEY}:${namespace}:${owner || 'anon'}` : `${KEY}:${owner || 'anon'}`
}

function fresh(namespace: string): string {
  const prefix = namespace ? `${namespace}-chat` : 'chat'
  return `${prefix}-${Math.random().toString(36).slice(2)}`
}

export interface ConversationId {
  id: string
  /** True when `id` came from storage (a reload of an in-progress
   *  conversation) rather than being freshly minted. Callers use this to skip
   *  fetching history for a conversation that can't have any yet — the legacy
   *  panel fetched unconditionally and ate a 404 round trip on every brand-new
   *  tab, which this avoids. */
  isResumed: boolean
  /**
   * Bumped only when the THREAD changes — mount, the owner resolving, and
   * `reset()`. NOT bumped by `set()`. A history-loading effect should key off
   * this instead of `id`: a mid-conversation server rename (the requested id
   * had gone idle and the server handed back a fresh one) changes `id` without
   * starting a new thread, and refetching history at that point would replace
   * the messages already on screen with the server's (empty, for a just-issued
   * id) transcript — wiping the conversation the rename happened in the middle
   * of.
   */
  generation: number
  /** Adopt a server-issued id — the stream/response may rename the caller's id.
   *  Does not affect `generation`; see above. */
  set: (id: string) => void
  /** Abandon this conversation and start a new one. */
  reset: () => void
  /** Switch the active thread to an EXISTING conversation — opening one from a
   *  past-conversations list, as opposed to `set`'s "the current thread got
   *  renamed." Unlike `set`, this bumps `generation`, so the history-loading
   *  effect in `useChat` fires and replaces the messages on screen with that
   *  conversation's transcript. */
  open: (id: string) => void
}

export function useConversationId(owner: string | null, namespace = ''): ConversationId {
  const key = storageKey(owner, namespace)
  const [id, setId] = useState<string>(() => {
    try {
      return sessionStorage.getItem(key) || fresh(namespace)
    } catch {
      return fresh(namespace)
    }
  })
  const [isResumed, setIsResumed] = useState<boolean>(() => {
    try {
      return Boolean(sessionStorage.getItem(key))
    } catch {
      return false
    }
  })
  const [generation, setGeneration] = useState(0)

  // Re-derive when the owner resolves: the first render happens before
  // /dashboard/me returns, so the initial id is under the 'anon' bucket and has
  // to be replaced once the real principal is known, not merged with it.
  useEffect(() => {
    try {
      const existing = sessionStorage.getItem(key)
      if (existing) {
        setId(existing)
        setIsResumed(true)
      } else {
        const next = fresh(namespace)
        sessionStorage.setItem(key, next)
        setId(next)
        setIsResumed(false)
      }
    } catch {
      setId(fresh(namespace))
      setIsResumed(false)
    }
    setGeneration((g) => g + 1)
  }, [key, namespace])

  const set = useCallback(
    (next: string) => {
      setId(next)
      try {
        sessionStorage.setItem(key, next)
      } catch {
        // Private-mode quota failure: the in-memory id still works for this tab.
      }
      // Deliberately no setIsResumed/setGeneration — see the `generation` doc.
    },
    [key],
  )

  const reset = useCallback(() => {
    const next = fresh(namespace)
    setId(next)
    setIsResumed(false)
    try {
      sessionStorage.setItem(key, next)
    } catch {
      // Private-mode quota failure: the in-memory id still works for this tab.
    }
    setGeneration((g) => g + 1)
  }, [key, namespace])

  const open = useCallback(
    (next: string) => {
      setId(next)
      setIsResumed(true)
      try {
        sessionStorage.setItem(key, next)
      } catch {
        // Private-mode quota failure: the in-memory id still works for this tab.
      }
      setGeneration((g) => g + 1)
    },
    [key],
  )

  return { id, isResumed, generation, set, reset, open }
}
