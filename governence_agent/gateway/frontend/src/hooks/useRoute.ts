/**
 * Path-based tab routing — `/monitor`, not `/#/monitor`.
 *
 * This needs the backend's cooperation to work: a hard refresh or a shared
 * link on `/monitor` sends a real HTTP request for `/monitor`, so something
 * server-side has to answer it with the app instead of a 404. That's
 * `pages._catch_all` (gateway/backend/__init__.py) — registered last, after
 * every real route, so it only ever catches a path nothing else claimed.
 *
 * (Hash routing avoided needing that entirely, which used to be the whole
 * reason for it — plus a second one, now gone: the dev server used to proxy
 * `/dashboard` straight to the gateway's legacy login page, so a path-based
 * route under `/dashboard` would have been answered by Python instead of
 * reaching this SPA. Neither excuse applies anymore.)
 *
 * A route may carry one optional extra path segment — `/home/<conversationId>`
 * — exposed as `param`. `resolveRoute` already ignores anything past the first
 * `/` when resolving the tab itself, so an unknown/malformed param can never
 * point the router at the wrong tab; it can only fail to mean anything to the
 * page that reads it.
 */

import { useCallback, useEffect, useMemo, useState } from 'react'
import { isKnownPath, resolveRoute, type RouteKey } from '../pages/routes'

export interface Router {
  key: RouteKey
  /** The segment after the tab, if any — e.g. `home` for `/home/home`.
   *  Percent-decoded. Null on a bare `/home`. */
  param: string | null
  /** True when the current path isn't a real destination at all — a typo, a
   *  stale link, anything not in the registry — as opposed to `key` having
   *  quietly defaulted to Home because that's what an unrecognised path
   *  always resolves to. `App.tsx` renders `NotFoundPage` instead of `key`'s
   *  page when this is set, rather than pretending nothing's wrong. */
  notFound: boolean
  navigate: (key: RouteKey, param?: string) => void
  /** For anchors, so tabs are real links: middle-click and copy-link work. */
  href: (key: RouteKey, param?: string) => string
}

function path(key: RouteKey, param?: string): string {
  return param ? `/${key}/${encodeURIComponent(param)}` : `/${key}`
}

export function useRoute(role: string | null): Router {
  const [raw, setRaw] = useState(() => location.pathname)

  useEffect(() => {
    // Fires on Back/Forward. `navigate` below updates state itself for a
    // programmatic move — `pushState` triggers no event of its own, unlike
    // assigning `location.hash` (which fired `hashchange`) used to.
    const onPopState = () => setRaw(location.pathname)
    addEventListener('popstate', onPopState)
    return () => removeEventListener('popstate', onPopState)
  }, [])

  const navigate = useCallback((key: RouteKey, param?: string) => {
    const next = path(key, param)
    history.pushState(null, '', next)
    setRaw(next)
  }, [])

  const href = useCallback((key: RouteKey, param?: string) => path(key, param), [])

  // Resolved every render rather than stored: `role` arrives after the session
  // loads, and an admin deep link must become reachable at that point instead of
  // staying stuck on the Home fallback it resolved to while role was null.
  const key = resolveRoute(raw, role)
  const notFound = useMemo(() => !isKnownPath(raw), [raw])

  const param = useMemo(() => {
    const stripped = raw.replace(/^\/+/, '')
    const slash = stripped.indexOf('/')
    if (slash === -1) return null
    const rest = stripped.slice(slash + 1)
    if (!rest) return null
    try {
      return decodeURIComponent(rest)
    } catch {
      return null
    }
  }, [raw])

  return { key, param, notFound, navigate, href }
}
