import { useCallback, useState } from 'react'
import { Chip } from '../ui'
import { humanize, RISK_META } from './toolCardShared'
import type { CategoryCatalogEntry, ConsumerOverrides } from '../../lib/api'
import './CategoryAccessPicker.css'

/**
 * `CategoryPicker` grants a category wholesale — this is the same row of
 * category chips, but each one also has an expand control that drops down
 * to that category's individual tools (same `CategoryCatalogEntry.tools`
 * `RequestPicker` uses for the self-service request flow), each a small
 * toggle chip of its own. Deselecting a tool doesn't remove the category —
 * it writes that tool into `overrides[category.backend].denyTools`
 * (policy/resolve.py: deny always wins over the category's own grant), so
 * "hold this category but not that one tool" is expressible without a
 * separate category just for the carve-out.
 *
 * `overrides` is keyed by BACKEND, not category (that's the real data
 * model, not a UI simplification) — so if two categories on this consumer
 * share a backend (e.g. `email_send_external`/`email_send_internal`, both
 * backend `email`), denying a tool from either one's expanded view removes
 * it for both. The note under an expanded category's tools flags this
 * whenever another SELECTED category shares its backend, rather than
 * letting that collision surprise whoever's editing this later.
 */

export interface CategoryAccessPickerProps {
  categories: CategoryCatalogEntry[]
  selected: Set<string>
  onToggle: (id: string) => void
  overrides: ConsumerOverrides
  onToggleTool: (backend: string, toolName: string) => void
  emptyLabel?: string
}

function CategoryAccessPicker({
  categories,
  selected,
  onToggle,
  overrides,
  onToggleTool,
  emptyLabel = 'No categories exist yet.',
}: CategoryAccessPickerProps) {
  const [expanded, setExpanded] = useState<Set<string>>(new Set())

  const toggleExpanded = useCallback((id: string) => {
    setExpanded((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }, [])

  if (categories.length === 0) return <p className="cat-picker-empty">{emptyLabel}</p>

  return (
    <div className="cap-list">
      {categories.map((c) => {
        const isExpanded = expanded.has(c.id)
        const denied = new Set(overrides[c.backend]?.denyTools ?? [])
        const sharesBackend = categories.some(
          (other) => other.id !== c.id && other.backend === c.backend && selected.has(other.id),
        )
        return (
          <div className="cap-row" key={c.id}>
            <div className="cap-row-head">
              <Chip selected={selected.has(c.id)} onClick={() => onToggle(c.id)} title={c.display_name}>
                {c.display_name}
              </Chip>
              {c.tools.length > 0 && (
                <button
                  type="button"
                  className="cap-expand"
                  onClick={() => toggleExpanded(c.id)}
                  aria-expanded={isExpanded}
                  title={isExpanded ? 'Hide tools' : `${c.tools.length} tool${c.tools.length === 1 ? '' : 's'} — click to view`}
                >
                  <span className="cap-expand-count">{c.tools.length}</span>
                  <span className={`cap-chevron ${isExpanded ? 'cap-chevron--open' : ''}`} aria-hidden="true" />
                  <span className="ui-sr-only">{isExpanded ? `Hide ${c.display_name}'s tools` : `Show ${c.display_name}'s tools`}</span>
                </button>
              )}
            </div>
            {isExpanded && (
              <div className="cap-tools">
                {sharesBackend && (
                  <p className="cap-tools-note">
                    Shares the <span className="ui-mono">{c.backend}</span> backend with another category held on
                    this consumer — deselecting a tool here removes it for both.
                  </p>
                )}
                <div className="cap-tools-grid">
                  {c.tools.map((tool) => {
                    const granted = !denied.has(tool.name)
                    const risk = tool.risk ? RISK_META[tool.risk] : undefined
                    const tooltip = [tool.description, risk?.label].filter(Boolean).join(' — ')
                    return (
                      <Chip
                        key={tool.name}
                        size="sm"
                        selected={granted}
                        onClick={() => onToggleTool(c.backend, tool.name)}
                        title={tooltip || tool.name}
                      >
                        {humanize(tool.name)}
                      </Chip>
                    )
                  })}
                </div>
              </div>
            )}
          </div>
        )
      })}
    </div>
  )
}

export default CategoryAccessPicker
