import { useCallback, useEffect, useId, useRef, useState } from 'react'
import type { CSSProperties, KeyboardEvent } from 'react'
import { createPortal } from 'react-dom'
import './Dropdown.css'

/**
 * A styled stand-in for `<select>` (`Field.tsx`'s `Select`), for the two
 * things a native select can't do: look the same across browsers, and take
 * the kit's colour tokens all the way down into the open menu (Chromium's
 * own popup menu ignores CSS entirely on some platforms, which is exactly
 * the "why does this look different from everything else" the redesign
 * complaint was about).
 *
 * The option list is portal-rendered at a measured fixed position — the same
 * approach `useCardReveal` uses for tool-card overlays, for the same reason:
 * a Modal's body clips overflow, so an absolutely-positioned child menu would
 * get cropped the moment the modal is short enough to scroll. A stale
 * position after a scroll/resize is handled by closing rather than
 * re-tracking, again mirroring `useCardReveal` — a closed menu the user
 * re-opens is a far cheaper mistake than a menu floating over the wrong
 * control.
 */

export interface DropdownOption {
  value: string
  label: string
}

export interface DropdownProps {
  value: string
  onChange: (value: string) => void
  options: DropdownOption[]
  placeholder?: string
  disabled?: boolean
  /** Placed on the trigger button, so a `Field` label's `htmlFor` focuses it. */
  id?: string
  'aria-describedby'?: string
  'aria-invalid'?: true
}

function Dropdown({
  value,
  onChange,
  options,
  placeholder = 'Select…',
  disabled = false,
  id,
  'aria-describedby': describedBy,
  'aria-invalid': invalid,
}: DropdownProps) {
  const [open, setOpen] = useState(false)
  const [highlight, setHighlight] = useState(0)
  const [rect, setRect] = useState<{ top: number; left: number; width: number } | null>(null)
  const triggerRef = useRef<HTMLButtonElement>(null)
  const listRef = useRef<HTMLUListElement>(null)
  const listId = useId()

  const selectedIndex = Math.max(0, options.findIndex((o) => o.value === value))
  const current = options.find((o) => o.value === value)

  const close = useCallback(() => setOpen(false), [])

  const openMenu = useCallback(() => {
    if (disabled || options.length === 0) return
    const r = triggerRef.current?.getBoundingClientRect()
    if (r) setRect({ top: r.bottom + 6, left: r.left, width: r.width })
    setHighlight(selectedIndex)
    setOpen(true)
  }, [disabled, options.length, selectedIndex])

  const choose = useCallback(
    (index: number) => {
      const opt = options[index]
      if (!opt) return
      onChange(opt.value)
      close()
      triggerRef.current?.focus()
    },
    [options, onChange, close],
  )

  // A fixed-position snapshot goes stale the instant the page scrolls or
  // resizes — closing (not re-measuring) is the same trade `useCardReveal`
  // makes for its portal overlay. Scrolling is capture-phase so it catches a
  // scroll on any ancestor, not just the window — but that also catches
  // scrolling the menu's OWN option list (it's `overflow-y: auto` and
  // usually taller than its `max-height`), which must NOT close the menu
  // the user is actively scrolling through.
  useEffect(() => {
    if (!open) return

    const onScroll = (e: Event) => {
      const target = e.target
      if (target instanceof Node && listRef.current?.contains(target)) return
      close()
    }
    const onResize = () => close()
    const onPointerDown = (e: PointerEvent) => {
      const target = e.target as Node
      if (triggerRef.current?.contains(target) || listRef.current?.contains(target)) return
      close()
    }

    window.addEventListener('scroll', onScroll, true)
    window.addEventListener('resize', onResize)
    document.addEventListener('pointerdown', onPointerDown)
    return () => {
      window.removeEventListener('scroll', onScroll, true)
      window.removeEventListener('resize', onResize)
      document.removeEventListener('pointerdown', onPointerDown)
    }
  }, [open, close])

  const onTriggerKeyDown = (e: KeyboardEvent<HTMLButtonElement>) => {
    if (disabled) return
    if (!open) {
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp' || e.key === 'Enter' || e.key === ' ') {
        e.preventDefault()
        openMenu()
      }
      return
    }
    switch (e.key) {
      case 'ArrowDown':
        e.preventDefault()
        setHighlight((h) => Math.min(options.length - 1, h + 1))
        break
      case 'ArrowUp':
        e.preventDefault()
        setHighlight((h) => Math.max(0, h - 1))
        break
      case 'Home':
        e.preventDefault()
        setHighlight(0)
        break
      case 'End':
        e.preventDefault()
        setHighlight(options.length - 1)
        break
      case 'Enter':
      case ' ':
        e.preventDefault()
        choose(highlight)
        break
      case 'Escape':
        e.preventDefault()
        close()
        break
      case 'Tab':
        close()
        break
      default:
    }
  }

  return (
    <>
      <button
        ref={triggerRef}
        type="button"
        id={id}
        className="ui-dd-trigger"
        disabled={disabled}
        onClick={() => (open ? close() : openMenu())}
        onKeyDown={onTriggerKeyDown}
        role="combobox"
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={open ? listId : undefined}
        aria-describedby={describedBy}
        aria-invalid={invalid}
      >
        <span className={`ui-dd-value ${current ? '' : 'ui-dd-value--placeholder'}`}>
          {current?.label ?? placeholder}
        </span>
        <span className={`ui-dd-chevron ${open ? 'ui-dd-chevron--open' : ''}`} aria-hidden="true" />
      </button>

      {open &&
        rect &&
        createPortal(
          <ul
            ref={listRef}
            id={listId}
            className="ui-dd-menu"
            role="listbox"
            aria-label={typeof placeholder === 'string' ? placeholder : undefined}
            style={{ top: rect.top, left: rect.left, width: rect.width } as CSSProperties}
          >
            {options.map((opt, i) => (
              <li key={opt.value}>
                <button
                  type="button"
                  className="ui-dd-option"
                  role="option"
                  aria-selected={opt.value === value}
                  data-highlighted={i === highlight || undefined}
                  onMouseEnter={() => setHighlight(i)}
                  onClick={() => choose(i)}
                >
                  {opt.label}
                  {opt.value === value && (
                    <span className="ui-dd-check" aria-hidden="true">
                      ✓
                    </span>
                  )}
                </button>
              </li>
            ))}
          </ul>,
          document.body,
        )}
    </>
  )
}

export default Dropdown
