/**
 * Persisted collapse state for a side panel (the main nav sidebar, Home's
 * conversation rail) — localStorage-backed, so a user's preference survives a
 * reload, matching how claude.ai remembers whether its own sidebar is open.
 */

import { useCallback, useState } from 'react'

export interface UseCollapsedResult {
  collapsed: boolean
  toggle: () => void
}

export function useCollapsed(storageKey: string, defaultCollapsed = false): UseCollapsedResult {
  const [collapsed, setCollapsed] = useState(() => {
    try {
      const stored = localStorage.getItem(storageKey)
      return stored === null ? defaultCollapsed : stored === '1'
    } catch {
      return defaultCollapsed
    }
  })

  const toggle = useCallback(() => {
    setCollapsed((prev) => {
      const next = !prev
      try {
        localStorage.setItem(storageKey, next ? '1' : '0')
      } catch {
        // Private-mode quota failure: the in-memory toggle still works for this tab.
      }
      return next
    })
  }, [storageKey])

  return { collapsed, toggle }
}
