/**
 * Key → page component, for all 25 tabs.
 *
 * There is no separate Assistant tab — Home's composer/thread IS the governed
 * assistant, so that entry was retired from the registry entirely rather than
 * kept as a second, redundant door to the same conversation (see routes.ts).
 *
 * Ported pages are listed explicitly; everything else resolves to
 * PlaceholderPage, which links through to the still-live legacy panel. Porting a
 * tab is therefore one import and one line in PAGES — no routing, sidebar, or
 * shell changes.
 *
 * `Partial<Record<...>>` on purpose: an exhaustive Record would force 25 entries
 * and turn every unported tab into a stub file that exists only to satisfy the
 * type.
 */

import type { ComponentType } from 'react'
import AccessPage from './AccessPage'
import AlertsPage from './AlertsPage'
import ApprovalsPage from './ApprovalsPage'
import CalendarPage from './CalendarPage'
import CategoriesPage from './CategoriesPage'
import ConsumersPage from './ConsumersPage'
import DepartmentsPage from './DepartmentsPage'
import DeveloperPage from './DeveloperPage'
import HistoryPage from './HistoryPage'
import HomePage from './HomePage'
import PlaceholderPage from './PlaceholderPage'
import RequestsPage from './RequestsPage'
import SecurityPage from './SecurityPage'
import SendQueuePage from './SendQueuePage'
import WhitelistPage from './WhitelistPage'
import type { RouteKey } from './routes'
import type { PageProps } from './types'

const PAGES: Partial<Record<RouteKey, ComponentType<PageProps>>> = {
  home: HomePage,
  access: AccessPage,
  history: HistoryPage,
  requests: RequestsPage,
  approvals: ApprovalsPage,
  security: SecurityPage,
  alerts: AlertsPage,
  whitelist: WhitelistPage,
  consumers: ConsumersPage,
  categories: CategoriesPage,
  'department-admin': DepartmentsPage,
  developer: DeveloperPage,
  calendar: CalendarPage,
  sends: SendQueuePage,
}

export function pageFor(key: RouteKey): ComponentType<PageProps> {
  return PAGES[key] ?? PlaceholderPage
}

/** Whether a tab has a real implementation — the sidebar dims the ones that
 *  don't, so it's obvious which parts of the console have moved over. */
export function isPorted(key: RouteKey): boolean {
  return key in PAGES
}

export { default as AccessPage } from './AccessPage'
export { default as AlertsPage } from './AlertsPage'
export { default as ApprovalsPage } from './ApprovalsPage'
export { default as CalendarPage } from './CalendarPage'
export { default as CategoriesPage } from './CategoriesPage'
export { default as ConsumersPage } from './ConsumersPage'
export { default as DepartmentsPage } from './DepartmentsPage'
export { default as DeveloperPage } from './DeveloperPage'
export { default as HistoryPage } from './HistoryPage'
export { default as HomePage } from './HomePage'
export { default as PlaceholderPage } from './PlaceholderPage'
export { default as RequestsPage } from './RequestsPage'
export { default as SecurityPage } from './SecurityPage'
export { default as SendQueuePage } from './SendQueuePage'
export { default as WhitelistPage } from './WhitelistPage'
export * from './routes'
export type { PageProps } from './types'
