import { useCallback, useEffect, useId, useMemo, useRef, useState } from 'react'
import type { CSSProperties } from 'react'
import { createPortal } from 'react-dom'
import Dropdown from './Dropdown'
import type { DropdownOption } from './Dropdown'
import './TimePicker.css'

/**
 * A time-of-day popover — the time half of `DateTimePicker`, standalone, for
 * a field that only ever needs a clock time and no calendar (a schedule's
 * "run at" time).
 *
 * Exists for exactly the reason `Dropdown` replaced `<select>` and
 * `DateTimePicker` replaced `<input type="datetime-local">`: a native
 * `<input type="time">` draws its own hour/minute/AM-PM popup in the
 * browser's colours, ignores every one of this kit's tokens, and looks
 * different on every platform. This reuses `Dropdown` for the three controls
 * rather than inventing a fourth kind of popover.
 *
 * `value`/`onChange` use the same "HH:mm" 24-hour shape a native
 * `<input type="time">` produces, so it drops in wherever that was used.
 */

export interface TimePickerProps {
  value: string
  onChange: (value: string) => void
  disabled?: boolean
  /** Placed on the trigger button, so a `Field` label's `htmlFor` focuses it. */
  id?: string
  'aria-describedby'?: string
  'aria-invalid'?: true
}

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

/** "HH:mm" → minutes since midnight. Defaults to 09:00 for an unparseable or
 *  empty value, matching `DateTimePicker`'s own "a fresh pick means 9am, not
 *  midnight" choice — midnight reads as "did this actually get set". */
function parseValue(value: string): { hour24: number; minute: number } {
  const [h, m] = (value ?? '').split(':').map(Number)
  if (Number.isNaN(h) || h == null) return { hour24: 9, minute: 0 }
  return { hour24: Math.min(23, Math.max(0, h)), minute: Number.isNaN(m) ? 0 : Math.min(59, Math.max(0, m)) }
}

function TimePicker({
  value,
  onChange,
  disabled = false,
  id,
  'aria-describedby': describedBy,
  'aria-invalid': invalid,
}: TimePickerProps) {
  const { hour24, minute } = useMemo(() => parseValue(value), [value])
  const [open, setOpen] = useState(false)
  const [rect, setRect] = useState<{ top: number; left: number } | null>(null)

  const triggerRef = useRef<HTMLButtonElement>(null)
  const panelRef = useRef<HTMLDivElement>(null)
  const panelId = useId()

  const close = useCallback(() => setOpen(false), [])

  const openPicker = useCallback(() => {
    if (disabled) return
    const r = triggerRef.current?.getBoundingClientRect()
    if (r) setRect({ top: r.bottom + 6, left: r.left })
    setOpen(true)
  }, [disabled])

  // Same dismissal contract as DateTimePicker, including the exception for
  // Dropdown's OWN portaled menu: it renders into <body> as a sibling of this
  // panel, so without recognising `.ui-dd-menu` as "inside", choosing an hour
  // would count as an outside click and close this panel before the click
  // landed on the option.
  useEffect(() => {
    if (!open) return

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

  const ampm = hour24 >= 12 ? 'PM' : 'AM'
  const hour12 = hour24 % 12 || 12

  // A minute off the 5-minute grid (set some other way) still needs to be a
  // selectable option, or Dropdown falls back to its placeholder.
  const minuteOptions = useMemo(() => {
    if (BASE_MINUTE_OPTIONS.some((o) => o.value === String(minute))) return BASE_MINUTE_OPTIONS
    return [...BASE_MINUTE_OPTIONS, { value: String(minute), label: pad(minute) }].sort((a, b) => Number(a.value) - Number(b.value))
  }, [minute])

  const update = useCallback(
    (nextHour12: number, nextMinute: number, nextAmpm: string) => {
      const h24 = (nextHour12 % 12) + (nextAmpm === 'PM' ? 12 : 0)
      onChange(`${pad(h24)}:${pad(nextMinute)}`)
    },
    [onChange],
  )

  const display = `${hour12}:${pad(minute)} ${ampm}`

  return (
    <>
      <button
        ref={triggerRef}
        type="button"
        id={id}
        className="ui-tp-trigger"
        disabled={disabled}
        onClick={() => (open ? close() : openPicker())}
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-controls={open ? panelId : undefined}
        aria-describedby={describedBy}
        aria-invalid={invalid}
      >
        <svg className="ui-tp-icon" viewBox="0 0 24 24" aria-hidden="true">
          <circle cx="12" cy="12" r="8.25" fill="none" stroke="currentColor" strokeWidth="1.6" />
          <path d="M12 7.5V12l3 1.9" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
        </svg>
        <span className="ui-tp-value">{display}</span>
      </button>

      {open &&
        rect &&
        createPortal(
          <div
            ref={panelRef}
            id={panelId}
            className="ui-tp-panel"
            role="dialog"
            aria-label="Choose a time"
            style={{ top: rect.top, left: rect.left } as CSSProperties}
          >
            <div className="ui-tp-row">
              <Dropdown value={String(hour12)} onChange={(v) => update(Number(v), minute, ampm)} options={HOUR_OPTIONS} />
              <span className="ui-tp-sep">:</span>
              <Dropdown value={String(minute)} onChange={(v) => update(hour12, Number(v), ampm)} options={minuteOptions} />
              <Dropdown value={ampm} onChange={(v) => update(hour12, minute, v)} options={AMPM_OPTIONS} />
            </div>
            <div className="ui-tp-footer">
              <button type="button" className="ui-tp-done" onClick={close}>
                Done
              </button>
            </div>
          </div>,
          document.body,
        )}
    </>
  )
}

export default TimePicker
