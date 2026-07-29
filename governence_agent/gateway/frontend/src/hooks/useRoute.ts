/**
 * Hash-based tab routing.
 *
 * Hash, not pathname, and deliberately so: the dev server proxies `/dashboard`
 * straight to the gateway (see vite.config.ts) because the legacy login page
 * still lives there, so a path-based route like `/dashboard/monitor` would be
 * answered by Python instead of reaching this SPA. `#/monitor` is unambiguous
 * today, needs no dev-server rewrite, and needs no history fallback in
 * production either.
 *
 * A route may carry one optional extra path segment — `#/home/<conversationId>`
 * — exposed as `param`. `resolveRoute` already ignores anything past the first
 * `/` when resolving the tab itself, so an unknown/malformed param can never
 * point the router at the wrong tab; it can only fail to mean anything to the
 * page that reads it.
 *
 * When the legacy UI is retired and the `/dashboard` proxy entry comes out, this
 * hook is the one place to swap for React Router — everything else navigates
 * through `navigate(key)`.
 */

import { useCallback, useEffect, useMemo, useState } from 'react'
import { DEFAULT_ROUTE, resolveRoute, type RouteKey } from '../pages/routes'

function hashKey(): string {
  return location.hash.replace(/^#\/?/, '')
}

export interface Router {
  key: RouteKey
  /** The segment after the tab, if any — e.g. `home` for `#/home/home`.
   *  Percent-decoded. Null when the hash is bare (`#/home`). */
  param: string | null
  navigate: (key: RouteKey, param?: string) => void
  /** For anchors, so tabs are real links: middle-click and copy-link work. */
  href: (key: RouteKey, param?: string) => string
}

export function useRoute(role: string | null): Router {
  const [raw, setRaw] = useState(hashKey)

  useEffect(() => {
    const onHashChange = () => setRaw(hashKey())
    addEventListener('hashchange', onHashChange)
    return () => removeEventListener('hashchange', onHashChange)
  }, [])

  const navigate = useCallback((key: RouteKey, param?: string) => {
    // Assigning the hash pushes a history entry, so Back returns to the previous
    // tab. The hashchange listener above updates state; setting it here as well
    // would double-render.
    location.hash = param ? `#/${key}/${encodeURIComponent(param)}` : `#/${key}`
  }, [])

  const href = useCallback(
    (key: RouteKey, param?: string) => (param ? `#/${key}/${encodeURIComponent(param)}` : `#/${key}`),
    [],
  )

  // Resolved every render rather than stored: `role` arrives after the session
  // loads, and an admin deep link must become reachable at that point instead of
  // staying stuck on the Home fallback it resolved to while role was null.
  const key = raw ? resolveRoute(raw, role) : DEFAULT_ROUTE

  const param = useMemo(() => {
    const slash = raw.indexOf('/')
    if (slash === -1) return null
    const rest = raw.slice(slash + 1)
    if (!rest) return null
    try {
      return decodeURIComponent(rest)
    } catch {
      return null
    }
  }, [raw])

  return { key, param, navigate, href }
}
