/**
 * Per-user localStorage list, for saved asks and pinned answers.
 *
 * Namespaced by the signed-in principal, which the legacy UI also did — and for
 * a real reason found in production: localStorage is scoped to the ORIGIN, not
 * to the session, and is never cleared on logout. Without the namespace,
 * testing as one account and then signing in as another in the same browser
 * hands the second account the first one's saved prompts.
 *
 * The list is capped like the original (50 entries) so a long-lived browser
 * profile can't grow it without bound.
 */

import { useCallback, useEffect, useState } from 'react'

const MAX = 50

function keyFor(name: string, owner: string | null): string {
  return `${name}:${owner || 'anon'}`
}

function read<T>(key: string): T[] {
  try {
    const raw = localStorage.getItem(key)
    if (!raw) return []
    const parsed: unknown = JSON.parse(raw)
    return Array.isArray(parsed) ? (parsed as T[]) : []
  } catch {
    // Corrupt or unreadable: start clean rather than throwing on every render.
    return []
  }
}

function write<T>(key: string, list: T[]): void {
  try {
    localStorage.setItem(key, JSON.stringify(list.slice(0, MAX)))
  } catch {
    // Quota or private mode. The in-memory list still updates, so the current
    // session behaves normally and only persistence is lost.
  }
}

export interface StoredList<T> {
  items: T[]
  /** Prepends, so the newest is first. */
  add: (item: T) => void
  /** Removes everything matching the predicate. */
  remove: (match: (item: T) => boolean) => void
  clear: () => void
}

/**
 * @param name  storage key prefix, e.g. 'gov_quickasks'
 * @param owner signed-in principal's name; null while the session is loading,
 *              which reads the 'anon' bucket rather than guessing
 */
export function useStoredList<T>(name: string, owner: string | null): StoredList<T> {
  const key = keyFor(name, owner)
  const [items, setItems] = useState<T[]>(() => read<T>(key))

  // Re-read when the owner resolves: the first render happens before /dashboard/me
  // returns, so the initial state is the 'anon' bucket and has to be replaced
  // rather than merged.
  useEffect(() => {
    setItems(read<T>(key))
  }, [key])

  const commit = useCallback(
    (next: T[]) => {
      setItems(next)
      write(key, next)
    },
    [key],
  )

  const add = useCallback(
    (item: T) => commit([item, ...read<T>(key)].slice(0, MAX)),
    [commit, key],
  )

  const remove = useCallback(
    (match: (item: T) => boolean) => commit(read<T>(key).filter((i) => !match(i))),
    [commit, key],
  )

  const clear = useCallback(() => commit([]), [commit])

  return { items, add, remove, clear }
}
