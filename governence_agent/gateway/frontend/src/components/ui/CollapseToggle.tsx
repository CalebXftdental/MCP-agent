import './CollapseToggle.css'

/**
 * The button that opens/closes a side panel (the main nav sidebar, Home's
 * conversation rail) — the "sidebar" glyph most apps use for this (VS Code,
 * Notion, Linear, Slack…), a rectangle with a divider near one edge, rather
 * than a chevron.
 *
 * Visually it belongs to the panel it toggles, docked right at its edge — but
 * it is NOT a descendant of the panel's own collapsing box. That box clips
 * its contents as it shrinks (so the panel's fixed-width content doesn't
 * reflow mid-collapse), and a button clipped along with it would make itself
 * unreachable the instant it did its job. Each caller instead renders this as
 * a sibling of the collapsing box, positioned absolutely and slid to the
 * panel's current edge in CSS (see AppShell.css / HomePage.css) — same
 * transition, so it visually rides along with the edge without ever being
 * clipped by it.
 */

export interface CollapseToggleProps {
  collapsed: boolean
  onToggle: () => void
  /** Announced to screen readers and shown as the hover title — "Sections",
   *  "Conversations". */
  label: string
  className?: string
}

function CollapseToggle({ collapsed, onToggle, label, className = '' }: CollapseToggleProps) {
  return (
    <button
      type="button"
      className={`ui-collapse-toggle ${className}`}
      onClick={onToggle}
      aria-expanded={!collapsed}
      aria-label={collapsed ? `Show ${label}` : `Hide ${label}`}
      title={collapsed ? `Show ${label}` : `Hide ${label}`}
    >
      <svg className="ui-collapse-toggle-icon" viewBox="0 0 16 16" width="15" height="15" aria-hidden="true">
        <rect x="1.5" y="2.5" width="13" height="11" rx="1.6" fill="none" stroke="currentColor" strokeWidth="1.3" />
        <line x1="6.4" y1="2.5" x2="6.4" y2="13.5" stroke="currentColor" strokeWidth="1.3" />
      </svg>
    </button>
  )
}

export default CollapseToggle
