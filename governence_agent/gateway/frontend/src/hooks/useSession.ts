/**
 * The signed-in principal.
 *
 * `/dashboard/me` returns 401 when there is no session, which is a normal state
 * and not an error — the hook reports `status: 'anonymous'` for it and keeps
 * `error` for genuine failures (gateway down, 500). Callers can then show a
 * sign-in prompt without inspecting status codes.
 */

import { useCallback, useEffect, useState } from 'react'
import { ApiError, getMe, logout as postLogout, type Me } from '../lib/api'

export type SessionStatus = 'loading' | 'signed-in' | 'anonymous' | 'error'

export interface Session {
  status: SessionStatus
  me: Me | null
  /** Convenience: `me?.name ?? null`, the key the stored-list hooks namespace by. */
  name: string | null
  /** Convenience: `me?.role ?? null`. Null means "not known yet or not signed in",
   *  which every admin check must treat as non-admin. */
  role: string | null
  isAdmin: boolean
  error: string | null
  refresh: () => void
  signOut: () => Promise<void>
}

export function useSession(): Session {
  const [status, setStatus] = useState<SessionStatus>('loading')
  const [me, setMe] = useState<Me | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [nonce, setNonce] = useState(0)

  useEffect(() => {
    let live = true

    getMe()
      .then((result) => {
        if (!live) return
        setMe(result)
        setError(null)
        setStatus('signed-in')
      })
      .catch((cause: unknown) => {
        if (!live) return
        setMe(null)
        if (cause instanceof ApiError && cause.isUnauthenticated) {
          setError(null)
          setStatus('anonymous')
        } else {
          setError(cause instanceof Error ? cause.message : 'Could not reach the gateway')
          setStatus('error')
        }
      })

    // Guards against a resolved fetch writing state after the component is gone,
    // and against an in-flight refresh() overwriting a newer one.
    return () => {
      live = false
    }
  }, [nonce])

  const refresh = useCallback(() => setNonce((n) => n + 1), [])

  const signOut = useCallback(async () => {
    try {
      await postLogout()
    } finally {
      // Even if the POST fails the local session is finished as far as the UI is
      // concerned; leaving it "signed in" would be a lie.
      setMe(null)
      setStatus('anonymous')
    }
  }, [])

  return {
    status,
    me,
    name: me?.name ?? null,
    role: me?.role ?? null,
    isAdmin: me?.role === 'admin',
    error,
    refresh,
    signOut,
  }
}
