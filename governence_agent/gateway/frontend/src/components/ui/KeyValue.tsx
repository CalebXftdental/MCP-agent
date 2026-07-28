import type { ReactNode } from 'react'
import './KeyValue.css'

/**
 * The two-column detail grid from the drawers (`.kv` / `.k` / `.v`).
 *
 * A real `<dl>`, not divs: the label/value pairing is then structural, so a
 * screen reader announces "Latency, 412ms" rather than reading two unrelated
 * runs of text.
 */

export interface KeyValueItem {
  label: ReactNode
  value: ReactNode
  /** Monospace value — ids, IPs, request keys. */
  mono?: boolean
  /** Full width, for something long like a reason string. */
  wide?: boolean
}

export interface KeyValueProps {
  items: KeyValueItem[]
  columns?: 1 | 2
  /** Rendered for null, undefined, and empty string. */
  placeholder?: ReactNode
}

function isBlank(value: ReactNode): boolean {
  return value == null || value === ''
}

function KeyValue({ items, columns = 2, placeholder = '—' }: KeyValueProps) {
  return (
    <dl className={`ui-kv ui-kv--${columns}`}>
      {items.map((item, i) => (
        // A <div> wrapper around each dt/dd pair is valid in HTML and is what
        // makes `wide` possible — grid can't span a dt and its dd together.
        <div className={`ui-kv-pair ${item.wide ? 'ui-kv-pair--wide' : ''}`} key={i}>
          <dt className="ui-kv-key ui-eyebrow">{item.label}</dt>
          <dd className={`ui-kv-value ${item.mono ? 'ui-mono' : ''}`}>
            {isBlank(item.value) ? <span className="ui-kv-blank">{placeholder}</span> : item.value}
          </dd>
        </div>
      ))}
    </dl>
  )
}

export default KeyValue
