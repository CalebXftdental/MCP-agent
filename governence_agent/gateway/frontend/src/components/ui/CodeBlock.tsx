import { useCallback, useEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import './CodeBlock.css'

/**
 * Preformatted payloads (the `<pre class="mono">` blocks in the call drawer) and
 * the credential display (`.key`).
 *
 * SecretKey is masked until revealed, which the legacy `.key` was not — it
 * printed a freshly rotated consumer key straight into the page, so the
 * credential sat in plain view of anyone near the screen and in any screenshot
 * of the panel. Copy works without revealing.
 */

export interface CodeBlockProps {
  children: string
  /** Small label above the block — "Arguments", "Response". */
  label?: ReactNode
  /** Adds a copy button. */
  copyable?: boolean
  /** Wrap long lines instead of scrolling horizontally. */
  wrap?: boolean
  /** Any CSS length. Scrolls beyond it. */
  maxHeight?: string
}

/** Clipboard write, reported back as a boolean. `navigator.clipboard` is absent
 *  on insecure origins, so the failure path is real and has to be visible. */
async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text)
    return true
  } catch {
    return false
  }
}

function useCopy() {
  const [state, setState] = useState<'idle' | 'copied' | 'failed'>('idle')
  const timer = useRef<ReturnType<typeof setTimeout>>(undefined)

  useEffect(() => () => clearTimeout(timer.current), [])

  const copy = useCallback(async (text: string) => {
    setState((await copyText(text)) ? 'copied' : 'failed')
    clearTimeout(timer.current)
    timer.current = setTimeout(() => setState('idle'), 1800)
  }, [])

  return { state, copy }
}

export function CodeBlock({ children, label, copyable = false, wrap = false, maxHeight }: CodeBlockProps) {
  const { state, copy } = useCopy()

  return (
    <div className="ui-code">
      {(label || copyable) && (
        <div className="ui-code-head">
          {label && <span className="ui-eyebrow">{label}</span>}
          {copyable && (
            <button type="button" className="ui-code-copy" onClick={() => copy(children)}>
              {state === 'copied' ? 'Copied' : state === 'failed' ? 'Copy failed' : 'Copy'}
            </button>
          )}
        </div>
      )}
      <pre className={`ui-code-pre ${wrap ? 'ui-code-pre--wrap' : ''}`} style={{ maxHeight }}>
        {children}
      </pre>
      {/* Announces the result of a copy, which is otherwise a purely visual
          label change on the button. */}
      <span className="ui-sr-only" role="status" aria-live="polite">
        {state === 'copied' ? 'Copied to clipboard' : state === 'failed' ? 'Copy failed' : ''}
      </span>
    </div>
  )
}

export interface SecretKeyProps {
  value: string
  label?: ReactNode
  /** Shown under the key — what it grants, when it expires. */
  hint?: ReactNode
}

export function SecretKey({ value, label = 'API key', hint }: SecretKeyProps) {
  const [revealed, setRevealed] = useState(false)
  const { state, copy } = useCopy()

  // Last four characters stay visible, which is enough to tell two keys apart
  // without exposing either.
  const masked = value.length > 4 ? `${'•'.repeat(Math.min(28, value.length - 4))}${value.slice(-4)}` : '••••'

  return (
    <div className="ui-secret">
      <div className="ui-code-head">
        <span className="ui-eyebrow">{label}</span>
        <div className="ui-secret-actions">
          <button type="button" className="ui-code-copy" onClick={() => setRevealed((v) => !v)}>
            {revealed ? 'Hide' : 'Reveal'}
          </button>
          <button type="button" className="ui-code-copy" onClick={() => copy(value)}>
            {state === 'copied' ? 'Copied' : state === 'failed' ? 'Copy failed' : 'Copy'}
          </button>
        </div>
      </div>
      <code className="ui-secret-value">{revealed ? value : masked}</code>
      {hint && <p className="ui-secret-hint">{hint}</p>}
      <span className="ui-sr-only" role="status" aria-live="polite">
        {state === 'copied' ? 'Key copied to clipboard' : state === 'failed' ? 'Copy failed' : ''}
      </span>
    </div>
  )
}

export default CodeBlock
