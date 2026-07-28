import type { CategoryCatalogEntry } from './api'

/** The bits of an access/signup request `describeRequest` needs — both the
 *  caller's own `AccessRequestRecord` and the admin-facing
 *  `AdminAccessRequest` satisfy this. */
export interface DescribableRequest {
  selections?: { category: string; tools: string[] }[]
  categories?: string[]
  backend?: string
}

/**
 * One request's line in plain English, resolved against the category
 * catalog rather than left as raw ids — mirrors the legacy `reqRowLabel()`,
 * including its fallback chain for request shapes the current UI doesn't
 * produce itself (signup-era `categories`/`backend` requests).
 */
export function describeRequest(request: DescribableRequest, categories: CategoryCatalogEntry[]): string {
  if (request.selections?.length) {
    return request.selections
      .map((selection) => {
        const category = categories.find((c) => c.id === selection.category)
        return selection.tools
          .map((name) => category?.tools.find((t) => t.name === name)?.description ?? name)
          .join(', ')
      })
      .join(' | ')
  }
  if (request.categories?.length) {
    return request.categories
      .map((id) => categories.find((c) => c.id === id)?.display_name ?? id)
      .join(', ')
  }
  return `backend: ${request.backend || '—'}`
}
