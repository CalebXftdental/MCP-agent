/**
 * The signed-in principal.
 *
 * `/dashboard/me` returns 401 when there is no session, which is a normal state
 * and not an error — the hook reports `status: 'anonymous'` for it and keeps
 * `error` for genuine failures (gateway down, 500). Callers can then show a
 * sign-in prompt without inspecting status codes.
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiError, getMe, logout as postLogout, type Me } from '../lib/api'

export type SessionStatus = 'loading' | 'signed-in' | 'anonymous' | 'error'

/** `'offline'`: the request never got a response at all (`ApiError.isOffline`)
 *  — the gateway process itself is unreachable. `'unexpected'`: it responded,
 *  just not with something usable (a 404, a 500, a malformed body) — a real
 *  problem, but a different one, and worth telling apart from "go start the
 *  gateway" since that advice would be wrong here. Null while there's no
 *  error to describe. */
export type SessionErrorKind = 'offline' | 'unexpected' | null

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
  errorKind: SessionErrorKind
  /** True once, right after a call elsewhere in the app 401'd a session that
   *  this hook still believed was live — as opposed to a plain never-signed-in
   *  landing. The sign-in view uses this to explain *why* it's showing up
   *  instead of just appearing silently. Clears on the next successful sign-in. */
  sessionExpired: boolean
  refresh: () => void
  signOut: () => Promise<void>
}

export function useSession(): Session {
  const [status, setStatus] = useState<SessionStatus>('loading')
  const [me, setMe] = useState<Me | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [errorKind, setErrorKind] = useState<SessionErrorKind>(null)
  const [sessionExpired, setSessionExpired] = useState(false)
  const [nonce, setNonce] = useState(0)

  const statusRef = useRef(status)
  useEffect(() => {
    statusRef.current = status
  }, [status])

  const refresh = useCallback(() => setNonce((n) => n + 1), [])

  // Any request anywhere in the app noticing a 401 (lib/api.ts's `request()`)
  // means the cookie died mid-session — re-check here so the UI drops back to
  // sign-in on its own. Gated on `statusRef` (not `status`, to dodge the stale
  // closure a plain dependency would need re-subscribing for) so this can only
  // ever fire the *first* 401 after a real sign-in: once `refresh()` lands on
  // 'anonymous' below, further events are ignored until the next successful
  // login flips it back to 'signed-in' — otherwise a burst of parallel calls
  // failing together would re-trigger this repeatedly for no reason.
  const expiredPendingRef = useRef(false)
  useEffect(() => {
    function onExpired() {
      if (statusRef.current !== 'signed-in') return
      expiredPendingRef.current = true
      refresh()
    }
    window.addEventListener('gov:session-expired', onExpired)
    return () => window.removeEventListener('gov:session-expired', onExpired)
  }, [refresh])

  useEffect(() => {
    let live = true

    getMe()
      .then((result) => {
        if (!live) return
        setMe(result)
        setError(null)
        setErrorKind(null)
        setSessionExpired(false)
        setStatus('signed-in')
      })
      .catch((cause: unknown) => {
        if (!live) return
        setMe(null)
        if (cause instanceof ApiError && cause.isUnauthenticated) {
          setError(null)
          setErrorKind(null)
          setStatus('anonymous')
          if (expiredPendingRef.current) {
            expiredPendingRef.current = false
            setSessionExpired(true)
          }
        } else {
          setError(cause instanceof Error ? cause.message : 'Could not reach the gateway')
          setErrorKind(cause instanceof ApiError && cause.isOffline ? 'offline' : 'unexpected')
          setStatus('error')
        }
      })

    // Guards against a resolved fetch writing state after the component is gone,
    // and against an in-flight refresh() overwriting a newer one.
    return () => {
      live = false
    }
  }, [nonce])

  const signOut = useCallback(async () => {
    try {
      await postLogout()
    } finally {
      // Even if the POST fails the local session is finished as far as the UI is
      // concerned; leaving it "signed in" would be a lie.
      setMe(null)
      setErrorKind(null)
      setSessionExpired(false)
      setStatus('anonymous')
      // A deliberate sign-out, unlike a mid-session expiry, gets a clean
      // canonical URL — there's no "back to where you were" to preserve the
      // way there is for an expiry, since the user just asked to leave.
      window.history.replaceState(null, '', '/login')
    }
  }, [])

  return {
    status,
    me,
    name: me?.name ?? null,
    role: me?.role ?? null,
    isAdmin: me?.role === 'admin',
    error,
    errorKind,
    sessionExpired,
    refresh,
    signOut,
  }
}
