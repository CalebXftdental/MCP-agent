import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { CSSProperties } from 'react'
import { createPortal } from 'react-dom'
import { Button, Chip, Field, Textarea, useReducedMotion, useToast } from '../ui'
import type { CategoryCatalogEntry, ToolInfo } from '../../lib/api'
import { humanize } from './toolCardShared'
import PickableAccessCard from './PickableAccessCard'
import './RequestPicker.css'

/**
 * The self-service part of "Request more access": a row of category pills
 * (title + how many tools are still available there), one shared panel below
 * that shows whichever pill is open, and a running tray of what's been picked
 * so far — replacing the old per-category `<details>` accordion, which put a
 * full checkbox list under every category at once whether it was relevant or
 * not.
 *
 * Picking a card doesn't just flip a boolean: a small ghost of the card flies
 * from where it was clicked to the tray (`FlightGhost`, a measured-rect portal
 * in the same spirit as useCardReveal's overlay) so the "this moved down
 * there" relationship is visible, not just inferred from two lists changing at
 * once. Skipped entirely under `prefers-reduced-motion` — the pick itself is
 * still instant either way.
 */

type CategoryEntry = { category: CategoryCatalogEntry; available: ToolInfo[] }

export interface RequestPickerProps {
  availableByCategory: CategoryEntry[]
  pendingByBackend: Record<string, Set<string>>
  picked: Record<string, Set<string>>
  onToggleTool: (categoryId: string, toolName: string) => void
  justification: string
  onJustificationChange: (value: string) => void
  onSubmit: () => void
  submitting: boolean
  submitDisabled: boolean
  /** One-time cap on how many tools can go into a single request. */
  cap?: number
}

// Matches --ui-dur-4 in theme.css (the kit's "entrance" duration) — the flight
// is dressed as an entrance, so it uses the same timing.
const FLIGHT_MS = 420

interface Flight {
  id: number
  label: string
  start: DOMRect
  end: DOMRect
}

function FlightGhost({ label, start, end }: { label: string; start: DOMRect; end: DOMRect }) {
  const [arrived, setArrived] = useState(false)

  useEffect(() => {
    const frame = requestAnimationFrame(() => setArrived(true))
    return () => cancelAnimationFrame(frame)
  }, [])

  const dx = end.left + end.width / 2 - (start.left + start.width / 2)
  const dy = end.top - start.top

  const style: CSSProperties = {
    position: 'fixed',
    left: start.left,
    top: start.top,
    width: start.width,
    transform: arrived ? `translate(${dx}px, ${dy}px) scale(0.4)` : 'translate(0, 0) scale(1)',
    opacity: arrived ? 0 : 1,
  }

  return createPortal(
    <div className="access-flight-ghost" style={style} aria-hidden="true">
      {label}
    </div>,
    document.body,
  )
}

function RequestPicker({
  availableByCategory,
  pendingByBackend,
  picked,
  onToggleTool,
  justification,
  onJustificationChange,
  onSubmit,
  submitting,
  submitDisabled,
  cap = 7,
}: RequestPickerProps) {
  const toast = useToast()
  const reducedMotion = useReducedMotion()
  const [activeCategoryId, setActiveCategoryId] = useState<string | null>(null)
  const trayRef = useRef<HTMLDivElement>(null)
  const [flights, setFlights] = useState<Flight[]>([])
  const flightIdRef = useRef(0)

  const activeEntry = useMemo(
    () => availableByCategory.find((entry) => entry.category.id === activeCategoryId) ?? null,
    [availableByCategory, activeCategoryId],
  )

  // A category can empty out from under the open panel (its last tool just got
  // requested, then `load()` refreshes `availableByCategory`) — fall back to
  // the picker's rest state instead of showing a stale empty panel.
  useEffect(() => {
    if (activeCategoryId && !activeEntry) setActiveCategoryId(null)
  }, [activeCategoryId, activeEntry])

  const totalPicked = useMemo(
    () => Object.values(picked).reduce((sum, set) => sum + set.size, 0),
    [picked],
  )

  const selectedTools = useMemo(() => {
    const out: { categoryId: string; tool: ToolInfo }[] = []
    for (const { category, available } of availableByCategory) {
      const set = picked[category.id]
      if (!set || set.size === 0) continue
      for (const tool of available) {
        if (set.has(tool.name)) out.push({ categoryId: category.id, tool })
      }
    }
    return out
  }, [availableByCategory, picked])

  const launchFlight = useCallback(
    (cardEl: HTMLDivElement, label: string) => {
      if (reducedMotion) return
      const tray = trayRef.current
      if (!tray) return
      const start = cardEl.getBoundingClientRect()
      const end = tray.getBoundingClientRect()
      const id = ++flightIdRef.current
      setFlights((prev) => [...prev, { id, label, start, end }])
      window.setTimeout(() => {
        setFlights((prev) => prev.filter((f) => f.id !== id))
      }, FLIGHT_MS + 40)
    },
    [reducedMotion],
  )

  const handleToggle = useCallback(
    (categoryId: string, tool: ToolInfo, cardEl: HTMLDivElement) => {
      const already = picked[categoryId]?.has(tool.name) ?? false
      if (!already && totalPicked >= cap) {
        toast.warn(`You can request up to ${cap} things at a time — drop one first.`)
        return
      }
      if (!already) launchFlight(cardEl, humanize(tool.name))
      onToggleTool(categoryId, tool.name)
    },
    [picked, totalPicked, cap, launchFlight, onToggleTool, toast],
  )

  return (
    <div className="req-picker">
      <div className="req-pill-row">
        {availableByCategory.map(({ category, available }) => (
          <Chip
            key={category.id}
            selected={activeCategoryId === category.id}
            onClick={() => setActiveCategoryId((cur) => (cur === category.id ? null : category.id))}
          >
            <span className="req-pill-content">
              {category.display_name}
              <span className="req-pill-count">{available.length}</span>
            </span>
          </Chip>
        ))}
      </div>

      <div className="req-panel">
        {activeEntry ? (
          // A fixed 4-up grid, not the granted-access grid's auto-fit — here
          // the card width should stay constant regardless of how many tools
          // a category has (one tool shouldn't stretch to fill the whole
          // row), so this gets its own rule instead of reusing
          // `.access-tool-grid`.
          <div className="req-tool-grid" key={activeEntry.category.id}>
            {activeEntry.available.map((tool) => {
              const pendingTools = pendingByBackend[activeEntry.category.backend] ?? new Set<string>()
              const pickedTools = picked[activeEntry.category.id] ?? new Set<string>()
              const isSelected = pickedTools.has(tool.name)
              return (
                <PickableAccessCard
                  key={tool.name}
                  tool={tool}
                  selected={isSelected}
                  pending={pendingTools.has(tool.name)}
                  atCap={!isSelected && totalPicked >= cap}
                  onToggle={(t, el) => handleToggle(activeEntry.category.id, t, el)}
                />
              )
            })}
          </div>
        ) : (
          <p className="req-panel-hint">Choose a category above to see what you can request.</p>
        )}
      </div>

      <div className="req-tray" ref={trayRef}>
        <div className="req-tray-head">
          <span className="ui-eyebrow">Selected to request</span>
          <span className={`req-tray-count ${totalPicked >= cap ? 'req-tray-count--full' : ''}`}>
            {totalPicked} / {cap}
          </span>
        </div>
        {selectedTools.length === 0 ? (
          <p className="req-tray-empty">Nothing selected yet — pick a card above.</p>
        ) : (
          <div className="req-tray-chips">
            {selectedTools.map(({ categoryId, tool }) => (
              <span className="req-tray-chip" key={`${categoryId}:${tool.name}`}>
                <Chip
                  size="sm"
                  onRemove={() => onToggleTool(categoryId, tool.name)}
                  removeLabel={`Remove ${humanize(tool.name)} from the request`}
                >
                  {humanize(tool.name)}
                </Chip>
              </span>
            ))}
          </div>
        )}
      </div>

      {/* Collapsed by default — an always-open textarea for an optional field
          competed with the picking flow above for visual weight; tucked behind
          a summary, it's there when needed and invisible when it isn't. */}
      <details className="req-note access-disclosure">
        <summary className="ui-eyebrow">Add a note (optional)</summary>
        <Field label="Why do you need this?" hint="Optional — helps whoever reviews it.">
          {(fieldProps) => (
            <Textarea
              {...fieldProps}
              mono={false}
              rows={2}
              placeholder="optional"
              value={justification}
              onChange={(e) => onJustificationChange(e.target.value)}
            />
          )}
        </Field>
      </details>

      <div className="access-submit-row">
        <Button onClick={onSubmit} disabled={submitDisabled} loading={submitting}>
          Request access{totalPicked > 0 ? ` (${totalPicked})` : ''}
        </Button>
      </div>

      {flights.map((flight) => (
        <FlightGhost key={flight.id} label={flight.label} start={flight.start} end={flight.end} />
      ))}
    </div>
  )
}

export default RequestPicker
