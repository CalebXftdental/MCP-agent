import { useId, useState } from 'react'
import type { ReactNode } from 'react'
import { Button, Chip } from '../ui'
import { useStoredList } from '../../hooks/useStoredList'
import './QuickAsks.css'

/**
 * The quick-ask chip picker — `QUICK_STARTERS` plus this user's saved asks,
 * collapsed to one row with "+N more" until pressed. Home's only current
 * consumer, but kept as its own component (like `Composer`) rather than
 * inlined in the page.
 *
 * Owns its own `useStoredList` (saved asks live under one storage key per
 * user, `gov_quickasks`) rather than taking it as a prop, so any future
 * consumer can drop it in without wiring the list through first.
 */

interface QuickAskGroup {
  label: string
  items: string[]
}

/** The built-in starters, verbatim from `QUICK_STARTERS`. */
const STARTERS: QuickAskGroup[] = [
  {
    label: 'Customers',
    items: [
      'Find the customer {name or email}',
      'Show a 360° overview of customer {customer id}',
      'Find customers in {region}',
    ],
  },
  {
    label: 'Orders',
    items: [
      'Show recent orders for {customer}',
      'Get details for order {order number}',
      'What did {customer} spend this year?',
    ],
  },
  {
    label: 'Invoices & finance',
    items: ['Show overdue invoices for {customer}', 'Look up invoice {invoice number}'],
  },
]

/** Chips shown before "+N more" is pressed. Five fits one row at the
 *  composer's width without wrapping on a laptop. */
const COLLAPSED = 5

/** Renders a `{blank}` template as plain text runs plus a small boxed span per
 *  blank — Slack's inline-code treatment for a fill-in field, so it reads as
 *  a field rather than literal curly braces. The braces themselves are the
 *  delimiter and aren't shown. */
function renderTemplate(text: string): ReactNode {
  return text.split(/(\{[^}]+\})/g).map((part, i) => {
    const blank = /^\{([^}]+)\}$/.exec(part)
    return blank ? (
      <span className="qa-chip-blank" key={i}>
        {blank[1]}
      </span>
    ) : (
      part
    )
  })
}

export interface QuickAsksProps {
  owner: string | null
  onPick: (template: string) => void
  /** Trailing line under the expanded list. */
  note?: string
  /** Centers the collapsed row — for Home's hero, where the composer above it
   *  is also centered. The expanded grouped list stays left-aligned either
   *  way: group labels over centered chips read as captions floating free of
   *  their group. */
  center?: boolean
}

function QuickAsks({
  owner,
  onPick,
  note = 'Save your own with ★, or type your own above',
  center = false,
}: QuickAsksProps) {
  const savedAsks = useStoredList<string>('gov_quickasks', owner)
  const [showAll, setShowAll] = useState(false)
  const panelId = useId()

  const savedSet = new Set(savedAsks.items)
  const starters = STARTERS.flatMap((group) => group.items)
  // Saved asks first: they are this user's own, so they earn the visible row.
  const collapsed = [...savedAsks.items, ...starters].slice(0, COLLAPSED)
  const hidden = savedAsks.items.length + starters.length - collapsed.length

  const chipFor = (item: string) => {
    const saved = savedSet.has(item)
    return (
      <Chip
        key={item}
        size="sm"
        maxWidth="20rem"
        onClick={() => onPick(item)}
        onRemove={saved ? () => savedAsks.remove((entry) => entry === item) : undefined}
        removeLabel={`Remove saved ask: ${item}`}
        icon={saved ? '★' : undefined}
        title={item}
      >
        {renderTemplate(item)}
      </Chip>
    )
  }

  return (
    <div className="qa-block">
      {/* The collapsed row gives way to the toggle when expanded, rather than
          staying above the grouped list — otherwise the first five chips
          appear twice. */}
      <div className={`qa-chips ${center ? 'qa-chips--center' : ''}`}>
        {!showAll && collapsed.map(chipFor)}
        {(hidden > 0 || showAll) && (
          <Button
            variant="quiet"
            size="sm"
            onClick={() => setShowAll((open) => !open)}
            aria-expanded={showAll}
            aria-controls={panelId}
          >
            {showAll ? 'Show fewer' : `+${hidden} more`}
          </Button>
        )}
      </div>

      {/* Always in the DOM, `hidden` when collapsed: aria-controls has to
          point at something that exists, and a disclosure whose target only
          appears after the press announces nothing on the way in. */}
      <div className="qa-groups" id={panelId} hidden={!showAll}>
        {savedAsks.items.length > 0 && (
          <div className="qa-group">
            <p className="ui-eyebrow">★ Your saved</p>
            <div className="qa-chips">{savedAsks.items.map(chipFor)}</div>
          </div>
        )}

        {STARTERS.map((group) => (
          <div className="qa-group" key={group.label}>
            <p className="ui-eyebrow">{group.label}</p>
            <div className="qa-chips">{group.items.map(chipFor)}</div>
          </div>
        ))}

        <p className="qa-note">{note}</p>
      </div>
    </div>
  )
}

export default QuickAsks
