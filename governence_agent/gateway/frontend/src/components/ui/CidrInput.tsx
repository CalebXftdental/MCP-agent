import { useRef } from 'react'
import type { ChangeEvent, KeyboardEvent } from 'react'
import './CidrInput.css'

/**
 * A single IPv4 address/CIDR entered as four octet boxes plus a prefix box,
 * dot- and slash-separated — the classic router-admin-panel entry widget,
 * rather than one freeform text field. Typing three digits, a `.`, or a
 * value that can't take another digit without exceeding 255 all advance to
 * the next box; Backspace on an empty box steps back into the previous one.
 *
 * Fully controlled, like `Input`: `value` is the whole canonical string
 * ("10.0.0.0/8" or a bare "203.0.113.5"), so the caller's existing
 * validate-on-submit logic doesn't need to know this is five boxes instead
 * of one.
 */

export interface CidrInputProps {
  value: string
  onChange: (value: string) => void
  /** Enter anywhere in the group — mirrors a plain input's submit-on-Enter. */
  onSubmit?: () => void
  disabled?: boolean
  /** Placed on the first octet box, so a `Field` label's `htmlFor` focuses it. */
  id?: string
  'aria-describedby'?: string
  'aria-invalid'?: true
}

function splitValue(value: string): { octets: string[]; prefix: string } {
  const [addr, prefix = ''] = value.split('/')
  const parts = addr.split('.')
  return { octets: [0, 1, 2, 3].map((i) => parts[i] ?? ''), prefix }
}

function joinValue(octets: string[], prefix: string): string {
  const addr = octets.join('.')
  return prefix ? `${addr}/${prefix}` : addr
}

function CidrInput({
  value,
  onChange,
  onSubmit,
  disabled = false,
  id,
  'aria-describedby': describedBy,
  'aria-invalid': invalid,
}: CidrInputProps) {
  const { octets, prefix } = splitValue(value)
  const boxes = useRef<Array<HTMLInputElement | null>>([])

  const focusBox = (index: number) => {
    const el = boxes.current[index]
    el?.focus()
    el?.select()
  }

  const handleOctetChange = (index: number) => (e: ChangeEvent<HTMLInputElement>) => {
    const digits = e.target.value.replace(/\D/g, '').slice(0, 3)
    const clamped = digits === '' ? '' : String(Math.min(255, Number(digits)))
    const next = [...octets]
    next[index] = clamped
    onChange(joinValue(next, prefix))
    // Auto-advance once the box unambiguously can't take another digit
    // (already 3 digits, or 2 digits that would exceed 255 with a third).
    if (index < 3 && (clamped.length === 3 || (clamped.length === 2 && Number(clamped) > 25))) {
      focusBox(index + 1)
    }
  }

  const handleOctetKeyDown = (index: number) => (e: KeyboardEvent<HTMLInputElement>) => {
    const el = e.currentTarget
    if (e.key === '.' || e.key === ' ') {
      e.preventDefault()
      focusBox(index < 3 ? index + 1 : 4)
    } else if (e.key === '/') {
      e.preventDefault()
      focusBox(4)
    } else if (e.key === 'Backspace' && el.value === '' && index > 0) {
      e.preventDefault()
      focusBox(index - 1)
    } else if (e.key === 'ArrowLeft' && el.selectionStart === 0 && index > 0) {
      e.preventDefault()
      focusBox(index - 1)
    } else if (e.key === 'ArrowRight' && el.selectionStart === el.value.length && index < 3) {
      e.preventDefault()
      focusBox(index + 1)
    } else if (e.key === 'Enter') {
      onSubmit?.()
    }
  }

  const handlePrefixChange = (e: ChangeEvent<HTMLInputElement>) => {
    const digits = e.target.value.replace(/\D/g, '').slice(0, 2)
    const clamped = digits === '' ? '' : String(Math.min(32, Number(digits)))
    onChange(joinValue(octets, clamped))
  }

  const handlePrefixKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Backspace' && e.currentTarget.value === '') {
      e.preventDefault()
      focusBox(3)
    } else if (e.key === 'Enter') {
      onSubmit?.()
    }
  }

  return (
    <div className="ui-cidr" data-disabled={disabled || undefined} role="group" aria-label="IP address and prefix">
      {octets.map((octet, i) => (
        <span className="ui-cidr-seg" key={i}>
          {i > 0 && (
            <span className="ui-cidr-dot" aria-hidden="true">
              .
            </span>
          )}
          <input
            ref={(el) => {
              boxes.current[i] = el
            }}
            id={i === 0 ? id : undefined}
            className="ui-cidr-box"
            type="text"
            inputMode="numeric"
            autoComplete="off"
            maxLength={3}
            value={octet}
            disabled={disabled}
            onChange={handleOctetChange(i)}
            onKeyDown={handleOctetKeyDown(i)}
            onFocus={(e) => e.currentTarget.select()}
            aria-label={`Address, octet ${i + 1} of 4`}
            aria-describedby={i === 0 ? describedBy : undefined}
            aria-invalid={i === 0 ? invalid : undefined}
          />
        </span>
      ))}
      <span className="ui-cidr-slash" aria-hidden="true">
        /
      </span>
      <input
        ref={(el) => {
          boxes.current[4] = el
        }}
        className="ui-cidr-box ui-cidr-box--prefix"
        type="text"
        inputMode="numeric"
        autoComplete="off"
        maxLength={2}
        placeholder="32"
        value={prefix}
        disabled={disabled}
        onChange={handlePrefixChange}
        onKeyDown={handlePrefixKeyDown}
        onFocus={(e) => e.currentTarget.select()}
        aria-label="Prefix length, 0 to 32 — leave blank for a single address"
      />
    </div>
  )
}

export default CidrInput
