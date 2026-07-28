/**
 * Key → page component, for all 26 tabs.
 *
 * Ported pages are listed explicitly; everything else resolves to
 * PlaceholderPage, which links through to the still-live legacy panel. Porting a
 * tab is therefore one import and one line in PAGES — no routing, sidebar, or
 * shell changes.
 *
 * `Partial<Record<...>>` on purpose: an exhaustive Record would force 26 entries
 * and turn every unported tab into a stub file that exists only to satisfy the
 * type.
 */

import type { ComponentType } from 'react'
import HistoryPage from './HistoryPage'
import HomePage from './HomePage'
import PlaceholderPage from './PlaceholderPage'
import type { RouteKey } from './routes'
import type { PageProps } from './types'

const PAGES: Partial<Record<RouteKey, ComponentType<PageProps>>> = {
  home: HomePage,
  history: HistoryPage,
}

export function pageFor(key: RouteKey): ComponentType<PageProps> {
  return PAGES[key] ?? PlaceholderPage
}

/** Whether a tab has a real implementation — the sidebar dims the ones that
 *  don't, so it's obvious which parts of the console have moved over. */
export function isPorted(key: RouteKey): boolean {
  return key in PAGES
}

export { default as HistoryPage } from './HistoryPage'
export { default as HomePage } from './HomePage'
export { default as PlaceholderPage } from './PlaceholderPage'
export * from './routes'
export type { PageProps } from './types'
