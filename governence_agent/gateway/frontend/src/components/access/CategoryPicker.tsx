import { Chip } from '../ui'
import type { CategoryCatalogEntry } from '../../lib/api'
import './CategoryPicker.css'

/**
 * A row of toggleable chips over the real category catalog — first built
 * for Consumers' create/edit forms, reused as-is by Departments (a
 * department IS just a named set of categories, so picking its categories
 * is the identical control). Whole-category selection, unlike
 * `access/RequestPicker.tsx`'s per-tool picker: this is for admin surfaces
 * that grant a category wholesale, not the self-service request flow that
 * drills into individual tools within one.
 */

export interface CategoryPickerProps {
  categories: CategoryCatalogEntry[]
  selected: Set<string>
  onToggle: (id: string) => void
  emptyLabel?: string
}

function CategoryPicker({ categories, selected, onToggle, emptyLabel = 'No categories exist yet.' }: CategoryPickerProps) {
  if (categories.length === 0) return <p className="cat-picker-empty">{emptyLabel}</p>
  return (
    <div className="cat-picker-grid">
      {categories.map((c) => (
        <Chip key={c.id} selected={selected.has(c.id)} onClick={() => onToggle(c.id)} title={c.display_name}>
          {c.display_name}
        </Chip>
      ))}
    </div>
  )
}

export default CategoryPicker
