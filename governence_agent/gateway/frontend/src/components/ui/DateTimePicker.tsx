import { useCallback, useEffect, useId, useMemo, useRef, useState } from 'react'
import type { CSSProperties } from 'react'
import { createPortal } from 'react-dom'
import Dropdown from './Dropdown'
import type { DropdownOption } from './Dropdown'
import './DateTimePicker.css'

/**
 * A calendar + time popover, styled and animated like the rest of the kit —
 * built for the same reason `Dropdown` replaced native `<select>`: a native
 * `datetime-local` input's calendar/clock affordance is drawn entirely by
 * the browser/OS, ignores every one of this kit's colour tokens, and looks
 * different on every platform. This draws its own month grid and reuses
 * `Dropdown` for the hour/minute/AM-PM controls rather than inventing a
 * third kind of popover.
 *
 * `value`/`onChange` use the same "YYYY-MM-DDTHH:mm" shape a native
 * `datetime-local` input produces, so it drops in wherever that was used.
 *
 * The popover stays open across a day click AND a time change — closing
 * only on Done, Escape, or an outside click — so setting both takes one
 * open, not a re-open per field. The outside-click check explicitly treats
 * a click inside `Dropdown`'s OWN portaled menu as "still inside": both
 * this panel and a `Dropdown`'s option list render into `document.body` as
 * siblings, so without that exception, picking an hour would look like a
 * click outside THIS popover and close the whole thing before the click
 * even reached the option.
 */

export interface DateTimePickerProps {
  value: string
  onChange: (value: string) => void
  placeholder?: string
  disabled?: boolean
  /** `'date'` drops the hour/minute/AM-PM row entirely and reads/writes a
   *  bare `"YYYY-MM-DD"` (no `T...` suffix) — for a field a backend only
   *  ever treats as a calendar day (a report's start/end date), where
   *  offering a time would just be one more control that does nothing.
   *  Defaults to `'datetime'`, the original "YYYY-MM-DDTHH:mm" behavior. */
  mode?: 'date' | 'datetime'
  /** Placed on the trigger button, so a `Field` label's `htmlFor` focuses it. */
  id?: string
  'aria-describedby'?: string
  'aria-invalid'?: true
}

const WEEKDAYS = ['Su', 'Mo', 'Tu', 'We', 'Th', 'Fr', 'Sa']

const HOUR_OPTIONS: DropdownOption[] = Array.from({ length: 12 }, (_, i) => ({
  value: String(i + 1),
  label: String(i + 1),
}))

const BASE_MINUTE_OPTIONS: DropdownOption[] = Array.from({ length: 12 }, (_, i) => {
  const m = i * 5
  return { value: String(m), label: String(m).padStart(2, '0') }
})

const AMPM_OPTIONS: DropdownOption[] = [
  { value: 'AM', label: 'AM' },
  { value: 'PM', label: 'PM' },
]

function pad(n: number): string {
  return String(n).padStart(2, '0')
}

function parseValue(value: string): Date | null {
  if (!value) return null
  const [datePart, timePart] = value.split('T')
  const [y, m, d] = (datePart ?? '').split('-').map(Number)
  if (!y || !m || !d) return null
  const [hh, mm] = (timePart ?? '00:00').split(':').map(Number)
  return new Date(y, m - 1, d, hh || 0, mm || 0)
}

function formatValue(date: Date, mode: 'date' | 'datetime'): string {
  const datePart = `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`
  return mode === 'date' ? datePart : `${datePart}T${pad(date.getHours())}:${pad(date.getMinutes())}`
}

function sameDay(a: Date, b: Date): boolean {
  return a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate()
}

/** 42 cells (6 full weeks) so every month renders the same panel height,
 *  including the leading/trailing days from adjacent months. */
function buildGrid(viewMonth: Date): Date[] {
  const year = viewMonth.getFullYear()
  const month = viewMonth.getMonth()
  const firstWeekday = new Date(year, month, 1).getDay()
  const start = new Date(year, month, 1 - firstWeekday)
  return Array.from({ length: 42 }, (_, i) => new Date(start.getFullYear(), start.getMonth(), start.getDate() + i))
}

function DateTimePicker({
  value,
  onChange,
  placeholder,
  disabled = false,
  mode = 'datetime',
  id,
  'aria-describedby': describedBy,
  'aria-invalid': invalid,
}: DateTimePickerProps) {
  const selected = useMemo(() => parseValue(value), [value])
  const today = useMemo(() => new Date(), [])

  const [open, setOpen] = useState(false)
  const [viewMonth, setViewMonth] = useState(() => {
    const base = selected ?? today
    return new Date(base.getFullYear(), base.getMonth(), 1)
  })
  const [rect, setRect] = useState<{ top: number; left: number } | null>(null)

  const triggerRef = useRef<HTMLButtonElement>(null)
  const panelRef = useRef<HTMLDivElement>(null)
  const panelId = useId()

  const close = useCallback(() => setOpen(false), [])

  const openPicker = useCallback(() => {
    if (disabled) return
    const base = selected ?? today
    setViewMonth(new Date(base.getFullYear(), base.getMonth(), 1))
    const r = triggerRef.current?.getBoundingClientRect()
    if (r) setRect({ top: r.bottom + 6, left: r.left })
    setOpen(true)
  }, [disabled, selected, today])

  useEffect(() => {
    if (!open) return

    // Capture-phase scroll catches a scroll on any ancestor, including the
    // hour/minute Dropdowns' own option lists (they're `overflow-y: auto`
    // and usually taller than their `max-height`) — scrolling through one of
    // those must not close this whole panel out from under it. Their menu is
    // portaled to <body> too, so it's never a DOM descendant of panelRef;
    // recognized as "inside" by class instead, same as the pointerdown check
    // below.
    const onScroll = (e: Event) => {
      const target = e.target
      if (target instanceof Node && panelRef.current?.contains(target)) return
      if (target instanceof Element && target.closest('.ui-dd-menu')) return
      close()
    }
    const onResize = () => close()
    const onPointerDown = (e: PointerEvent) => {
      const target = e.target as Element
      if (triggerRef.current?.contains(target) || panelRef.current?.contains(target)) return
      if (target.closest?.('.ui-dd-menu')) return
      close()
    }
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        close()
        triggerRef.current?.focus()
      }
    }

    window.addEventListener('scroll', onScroll, true)
    window.addEventListener('resize', onResize)
    document.addEventListener('pointerdown', onPointerDown)
    document.addEventListener('keydown', onKeyDown)
    return () => {
      window.removeEventListener('scroll', onScroll, true)
      window.removeEventListener('resize', onResize)
      document.removeEventListener('pointerdown', onPointerDown)
      document.removeEventListener('keydown', onKeyDown)
    }
  }, [open, close])

  const grid = useMemo(() => buildGrid(viewMonth), [viewMonth])

  const selectDay = useCallback(
    (day: Date) => {
      // A fresh pick with no time set yet defaults to 9am, not midnight —
      // midnight reads as "did this actually get set" more than a real time.
      // Irrelevant in 'date' mode (formatValue drops the time entirely), but
      // harmless to still compute.
      const base = selected ?? new Date(day.getFullYear(), day.getMonth(), day.getDate(), 9, 0)
      onChange(formatValue(new Date(day.getFullYear(), day.getMonth(), day.getDate(), base.getHours(), base.getMinutes()), mode))
      if (mode === 'date') close()
    },
    [selected, onChange, mode, close],
  )

  const hour24 = selected ? selected.getHours() : 9
  const minute = selected ? selected.getMinutes() : 0
  const ampm = hour24 >= 12 ? 'PM' : 'AM'
  const hour12 = hour24 % 12 || 12

  // A minute not already on the 5-minute grid (set some other way) still
  // needs to be a selectable option, or the Dropdown would fall back to
  // showing its placeholder instead of the real value.
  const minuteOptions = useMemo(() => {
    if (BASE_MINUTE_OPTIONS.some((o) => o.value === String(minute))) return BASE_MINUTE_OPTIONS
    return [...BASE_MINUTE_OPTIONS, { value: String(minute), label: pad(minute) }].sort((a, b) => Number(a.value) - Number(b.value))
  }, [minute])

  const updateTime = useCallback(
    (nextHour12: number, nextMinute: number, nextAmpm: string) => {
      const base = selected ?? today
      const h24 = (nextHour12 % 12) + (nextAmpm === 'PM' ? 12 : 0)
      onChange(formatValue(new Date(base.getFullYear(), base.getMonth(), base.getDate(), h24, nextMinute), mode))
    },
    [selected, today, onChange, mode],
  )

  const jumpToToday = useCallback(() => {
    setViewMonth(new Date(today.getFullYear(), today.getMonth(), 1))
    selectDay(today)
  }, [today, selectDay])

  const display = selected
    ? mode === 'date'
      ? selected.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' })
      : selected.toLocaleString(undefined, { month: 'short', day: 'numeric', year: 'numeric', hour: 'numeric', minute: '2-digit' })
    : null

  return (
    <>
      <button
        ref={triggerRef}
        type="button"
        id={id}
        className="ui-dtp-trigger"
        disabled={disabled}
        onClick={() => (open ? close() : openPicker())}
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-controls={open ? panelId : undefined}
        aria-describedby={describedBy}
        aria-invalid={invalid}
      >
        <svg className="ui-dtp-icon" viewBox="0 0 24 24" aria-hidden="true">
          <rect x="3.5" y="5" width="17" height="15" rx="2.5" fill="none" stroke="currentColor" strokeWidth="1.6" />
          <path d="M3.5 9.5h17M8 3v3.4M16 3v3.4" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
        </svg>
        <span className={`ui-dtp-value ${display ? '' : 'ui-dtp-value--placeholder'}`}>
          {display ?? placeholder ?? (mode === 'date' ? 'Pick a date' : 'Pick a date & time')}
        </span>
      </button>

      {open &&
        rect &&
        createPortal(
          <div
            ref={panelRef}
            id={panelId}
            className="ui-dtp-panel"
            role="dialog"
            aria-label={mode === 'date' ? 'Choose a date' : 'Choose a date and time'}
            style={{ top: rect.top, left: rect.left } as CSSProperties}
          >
            <div className="ui-dtp-header">
              <button
                type="button"
                className="ui-dtp-nav"
                aria-label="Previous month"
                onClick={() => setViewMonth((m) => new Date(m.getFullYear(), m.getMonth() - 1, 1))}
              >
                ‹
              </button>
              <span className="ui-dtp-month">
                {viewMonth.toLocaleDateString(undefined, { month: 'long', year: 'numeric' })}
              </span>
              <button
                type="button"
                className="ui-dtp-nav"
                aria-label="Next month"
                onClick={() => setViewMonth((m) => new Date(m.getFullYear(), m.getMonth() + 1, 1))}
              >
                ›
              </button>
            </div>

            <div className="ui-dtp-weekdays">
              {WEEKDAYS.map((w) => (
                <span key={w}>{w}</span>
              ))}
            </div>

            <div className="ui-dtp-grid">
              {grid.map((day) => {
                const outside = day.getMonth() !== viewMonth.getMonth()
                const isSelected = selected != null && sameDay(day, selected)
                const isToday = sameDay(day, today)
                return (
                  <button
                    type="button"
                    key={day.toISOString()}
                    className="ui-dtp-day"
                    data-outside={outside || undefined}
                    data-selected={isSelected || undefined}
                    data-today={isToday || undefined}
                    onClick={() => selectDay(day)}
                  >
                    {day.getDate()}
                  </button>
                )
              })}
            </div>

            {mode === 'datetime' && (
              <div className="ui-dtp-time">
                <Dropdown value={String(hour12)} onChange={(v) => updateTime(Number(v), minute, ampm)} options={HOUR_OPTIONS} />
                <span className="ui-dtp-time-sep">:</span>
                <Dropdown value={String(minute)} onChange={(v) => updateTime(hour12, Number(v), ampm)} options={minuteOptions} />
                <Dropdown value={ampm} onChange={(v) => updateTime(hour12, minute, v)} options={AMPM_OPTIONS} />
              </div>
            )}

            <div className="ui-dtp-footer">
              <button type="button" className="ui-dtp-text-btn" onClick={() => onChange('')}>
                Clear
              </button>
              <button type="button" className="ui-dtp-text-btn ui-dtp-text-btn--accent" onClick={jumpToToday}>
                Today
              </button>
              <button type="button" className="ui-dtp-done" onClick={close}>
                Done
              </button>
            </div>
          </div>,
          document.body,
        )}
    </>
  )
}

export default DateTimePicker
