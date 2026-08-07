import { useEffect, useRef, useState } from 'react'
import type { MouseEvent, ReactNode } from 'react'
import { Avatar, Button, CollapseToggle, Tooltip } from './ui'
import ConnectionStatus from './ConnectionStatus'
import { useCollapsed } from '../hooks/useCollapsed'
import type { Session } from '../hooks/useSession'
import type { Router } from '../hooks/useRoute'
import { isPorted } from '../pages'
import { ROUTES, visibleNav, type RouteKey } from '../pages/routes'
import './AppShell.css'

/**
 * Sidebar + topbar around the routed page. The legacy `<aside>` / `.topbar` /
 * `#view` layout, driven by the route registry instead of a `buildNav()` that
 * wrote HTML strings.
 *
 * Only ever mounted signed in, on a real path — App.tsx routes `'loading'`,
 * `'error'`, `'anonymous'`, and an unrecognised path to their own standalone
 * screens before this ever renders, so nothing here needs to account for any
 * of those (no gateway-error state, no "not signed in" fallback — `session.me`
 * is always set here).
 *
 * Two things it adds. Tabs are real `<a href="/key">` anchors, so middle-click,
 * copy-link, and Back all work — a plain left click is the one case intercepted
 * (`NavItem`'s `onClick`), swapped for `router.navigate` so it becomes an
 * in-place transition instead of a full page reload; everything else falls
 * through to the browser's native handling of a normal link. And an unported
 * tab is dimmed with a small dot, so it is clear before clicking which parts
 * of the console have moved to React.
 */

export interface AppShellProps {
  session: Session
  router: Router
  children: ReactNode
}

function AppShell({ session, router, children }: AppShellProps) {
  const [navOpen, setNavOpen] = useState(false)
  const nav = useCollapsed('gov_nav_collapsed')
  const route = ROUTES[router.key]
  const groups = visibleNav(session.role)
  const mainRef = useRef<HTMLElement>(null)

  // Close the drawer nav on any tab change, and send focus to the new page so a
  // keyboard user isn't left at the top of a sidebar they just navigated from.
  useEffect(() => {
    setNavOpen(false)
    mainRef.current?.scrollTo({ top: 0 })
  }, [router.key])

  useEffect(() => {
    if (!navOpen) return
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setNavOpen(false)
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [navOpen])

  const me = session.me
  // Always set: App.tsx only mounts AppShell once `session.status ===
  // 'signed-in'`, which is exactly when `useSession` guarantees `me` is
  // populated. This is here purely to narrow the type for TS below.
  if (!me) return null

  return (
    <div className="shell" data-nav-open={navOpen || undefined} data-nav-collapsed={nav.collapsed || undefined}>
      <aside className="shell-side" id="shell-nav">
        <div className="shell-brand">
          {/* Hidden when collapsed — the wrapper (not the two elements
              individually) so the row's own justify-content can re-centre on
              just the toggle below without a hidden sibling still claiming
              space. */}
          <div className="shell-brand-mark">
            <img className="shell-logo" src="/frontier-logo.png" alt="Frontier MCP Workspace" />
            <span className="ui-eyebrow">Frontier MCP Workspace</span>
          </div>
          {/* Lives in the sidebar itself, not docked at its border — when
              collapsed it re-centres in the same narrow column the nav icons
              and avatar sit in below, reading as one more item in that rail
              rather than a control bolted onto the edge. */}
          <CollapseToggle
            collapsed={nav.collapsed}
            onToggle={nav.toggle}
            label="Sections"
            className="shell-collapse-toggle"
          />
        </div>

        <nav className="shell-nav" aria-label="Sections">
          {groups.map((group) => (
            <div className="shell-nav-group" key={group.label ?? 'top'}>
              {group.label && <p className="shell-nav-label ui-eyebrow">{group.label}</p>}
              {group.keys.map((key) => (
                <NavItem key={key} routeKey={key} router={router} collapsed={nav.collapsed} />
              ))}
            </div>
          ))}
        </nav>

        <div className="shell-who">
          <Avatar name={me.name} size="md" />
          <span className="shell-who-text">
            <span className="shell-who-name">{me.name}</span>
            <span className="shell-who-role">{me.role}</span>
          </span>
          <Button variant="ghost" size="sm" className="shell-who-signout" onClick={session.signOut}>
            Sign out
          </Button>
        </div>
      </aside>

      {/* Tap-to-dismiss for the mobile drawer. Hidden from assistive tech: the
          Escape handler above is the accessible way out. */}
      <div className="shell-scrim" onClick={() => setNavOpen(false)} aria-hidden="true" />

      <main className="shell-main" ref={mainRef}>
        <header className="shell-top">
          <Button
            className="shell-menu"
            variant="ghost"
            onClick={() => setNavOpen((open) => !open)}
            aria-expanded={navOpen}
            aria-controls="shell-nav"
            aria-label="Sections"
          >
            ☰
          </Button>
          <div className="shell-top-text">
            <h1 className="shell-top-title">{route.title}</h1>
            <p className="shell-top-desc">{route.description}</p>
          </div>
        </header>

        {children}
      </main>

      <ConnectionStatus corner="bottom-right" />
    </div>
  )
}

function NavItem({ routeKey, router, collapsed }: { routeKey: RouteKey; router: Router; collapsed: boolean }) {
  const route = ROUTES[routeKey]
  const active = router.key === routeKey
  const ported = isPorted(routeKey)

  // A real path now needs a real navigation to load (unlike a hash link,
  // which the browser never reloads for) — intercepted here so a plain click
  // stays an in-place tab switch. Anything asking for a new tab/window of its
  // own (a modifier key, or a middle click) is left alone; the href is a real
  // URL, so the browser's own handling of that already does the right thing.
  const onClick = (e: MouseEvent<HTMLAnchorElement>) => {
    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return
    e.preventDefault()
    router.navigate(routeKey)
  }

  const link = (
    <a
      className="shell-nav-item"
      href={router.href(routeKey)}
      onClick={onClick}
      data-active={active || undefined}
      data-unported={!ported || undefined}
      aria-current={active ? 'page' : undefined}
      // The label's already visible when expanded, so the native tooltip
      // would just be a redundant, slower-to-appear echo of it — it only
      // carries its own information (the description) there. Collapsed, the
      // custom Tooltip below takes over instead of stacking a second one.
      title={collapsed ? undefined : ported ? route.description : `${route.title} — not ported yet`}
    >
      <span className="shell-nav-icon" aria-hidden="true">
        {route.icon}
      </span>
      <span className="shell-nav-text">{route.title}</span>
      {!ported && (
        <>
          <span className="shell-nav-dot" aria-hidden="true" />
          <span className="ui-sr-only">(not ported yet)</span>
        </>
      )}
    </a>
  )

  return (
    <Tooltip label={ported ? route.title : `${route.title} (not ported yet)`} side="right" disabled={!collapsed}>
      {link}
    </Tooltip>
  )
}

export default AppShell
