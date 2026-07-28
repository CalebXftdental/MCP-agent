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
 * When the legacy UI is retired and the `/dashboard` proxy entry comes out, this
 * hook is the one place to swap for React Router — everything else navigates
 * through `navigate(key)`.
 */

import { useCallback, useEffect, useState } from 'react'
import { DEFAULT_ROUTE, resolveRoute, type RouteKey } from '../pages/routes'

function hashKey(): string {
  return location.hash.replace(/^#\/?/, '')
}

export interface Router {
  key: RouteKey
  navigate: (key: RouteKey) => void
  /** For anchors, so tabs are real links: middle-click and copy-link work. */
  href: (key: RouteKey) => string
}

export function useRoute(role: string | null): Router {
  const [raw, setRaw] = useState(hashKey)

  useEffect(() => {
    const onHashChange = () => setRaw(hashKey())
    addEventListener('hashchange', onHashChange)
    return () => removeEventListener('hashchange', onHashChange)
  }, [])

  const navigate = useCallback((key: RouteKey) => {
    // Assigning the hash pushes a history entry, so Back returns to the previous
    // tab. The hashchange listener above updates state; setting it here as well
    // would double-render.
    location.hash = `#/${key}`
  }, [])

  const href = useCallback((key: RouteKey) => `#/${key}`, [])

  // Resolved every render rather than stored: `role` arrives after the session
  // loads, and an admin deep link must become reachable at that point instead of
  // staying stuck on the Home fallback it resolved to while role was null.
  const key = raw ? resolveRoute(raw, role) : DEFAULT_ROUTE

  return { key, navigate, href }
}
