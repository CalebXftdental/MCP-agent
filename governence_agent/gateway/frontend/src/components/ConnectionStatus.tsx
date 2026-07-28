import { useEffect, useId, useRef, useState } from 'react'
import { useConnectionHealth, type ConnectionGroup, type Severity } from '../hooks/useConnectionHealth'
import './ConnectionStatus.css'

/**
 * Corner-mounted connection indicator that opens into a panel, browser-extension
 * style. Self-contained: mount it once anywhere and it probes on its own.
 *
 *   <ConnectionStatus />
 *   <ConnectionStatus position="top-right" pollMs={15000} />
 */

export type Corner = 'bottom-right' | 'bottom-left' | 'top-right' | 'top-left'

export interface ConnectionStatusProps {
  corner?: Corner
  /** Re-probe interval while the panel is open. Closed panels never poll. */
  pollMs?: number
  /** Start expanded — useful in a debug view. */
  defaultOpen?: boolean
}

const DOT_LABEL: Record<Severity, string> = {
  ok: 'connected',
  warn: 'degraded',
  down: 'down',
  idle: 'idle',
  checking: 'checking',
  blocked: 'unavailable',
}

function Dot({ status }: { status: Severity }) {
  return <span className={`cx-dot cx-dot--${status}`} role="presentation" />
}

function Group({ group }: { group: ConnectionGroup }) {
  return (
    <section className="cx-group">
      <header>
        <h3>{group.label}</h3>
        {group.note && <p className="cx-note">{group.note}</p>}
      </header>
      <ul>
        {group.rows.map((row) => (
          <li key={row.id} title={row.title}>
            <Dot status={row.status} />
            <span className="cx-row-label">{row.label}</span>
            <span className={`cx-row-detail cx-row-detail--${row.status}`}>
              {row.detail ?? DOT_LABEL[row.status]}
            </span>
          </li>
        ))}
      </ul>
    </section>
  )
}

function ConnectionStatus({
  corner = 'bottom-right',
  pollMs = 30_000,
  defaultOpen = false,
}: ConnectionStatusProps) {
  const [open, setOpen] = useState(defaultOpen)
  const panelId = useId()
  const rootRef = useRef<HTMLDivElement>(null)
  const triggerRef = useRef<HTMLButtonElement>(null)

  // Only poll while the panel is visible; a closed badge costs one probe on mount.
  const { groups, worst, summary, checking, lastChecked, refresh } = useConnectionHealth(
    open ? pollMs : null,
  )

  // Dismiss on outside click or Escape, returning focus to the trigger.
  useEffect(() => {
    if (!open) return

    const close = () => {
      setOpen(false)
      triggerRef.current?.focus()
    }
    const onPointerDown = (e: PointerEvent) => {
      if (!rootRef.current?.contains(e.target as Node)) setOpen(false)
    }
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') close()
    }

    document.addEventListener('pointerdown', onPointerDown)
    document.addEventListener('keydown', onKeyDown)
    return () => {
      document.removeEventListener('pointerdown', onPointerDown)
      document.removeEventListener('keydown', onKeyDown)
    }
  }, [open])

  return (
    <div ref={rootRef} className={`cx cx--${corner}`} data-open={open}>
      {open && (
        <div className="cx-panel" id={panelId} role="dialog" aria-label="Connection status">
          <header className="cx-panel-head">
            <h2>Connections</h2>
            <button
              type="button"
              className="cx-refresh"
              onClick={refresh}
              disabled={checking}
              aria-label="Re-check connections"
            >
              {checking ? 'Checking…' : 'Refresh'}
            </button>
          </header>

          <div className="cx-scroll">
            {groups.map((group) => (
              <Group key={group.id} group={group} />
            ))}
          </div>

          <footer className="cx-panel-foot">
            {lastChecked
              ? `Checked ${lastChecked.toLocaleTimeString()}`
              : 'Probing…'}
            {open && pollMs ? ` · re-checks every ${Math.round(pollMs / 1000)}s` : ''}
          </footer>
        </div>
      )}

      <button
        ref={triggerRef}
        type="button"
        className="cx-badge"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        aria-controls={open ? panelId : undefined}
      >
        <Dot status={worst} />
        <span className="cx-badge-text">{summary}</span>
      </button>
    </div>
  )
}

export default ConnectionStatus
