import { useEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { Avatar, Button, Card, CollapseToggle, Tooltip } from './ui'
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
 * Two things it adds. Tabs are real `<a href="#/key">` anchors, so middle-click,
 * copy-link, and Back all work — the old ones were `<a>` with a click handler and
 * `preventDefault()`. And an unported tab is dimmed with a small dot, so it is
 * clear before clicking which parts of the console have moved to React.
 */

export interface AppShellProps {
  session: Session
  router: Router
  children: ReactNode
}

/** Shown instead of the page when there is no session. There is no React login
 *  form yet, so this hands off to the one that exists rather than pretending. */
function SignedOut() {
  return (
    <div className="shell-gate">
      <Card
        title="Sign in to continue"
        description="This console needs a governance session."
        actions={
          <Button
            onClick={() => {
              location.href = '/dashboard'
            }}
          >
            Go to sign in →
          </Button>
        }
      >
        <p className="shell-gate-body">
          Sessions are an <code>HttpOnly</code> cookie set by the gateway, so
          signing in happens on its own page. You'll come back here afterwards.
        </p>
      </Card>
    </div>
  )
}

function Unreachable({ message, onRetry }: { message: string; onRetry: () => void }) {
  return (
    <div className="shell-gate">
      <Card
        title="Can't reach the gateway"
        accent="danger"
        actions={
          <Button variant="ghost" onClick={onRetry}>
            Try again
          </Button>
        }
      >
        <p className="shell-gate-body">{message}</p>
        <p className="shell-gate-body">
          Start it with <code>python app.py</code> in <code>gateway/</code>, then retry.
          The indicator in the bottom-right corner reports what it can see.
        </p>
      </Card>
    </div>
  )
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

  return (
    <div className="shell" data-nav-open={navOpen || undefined} data-nav-collapsed={nav.collapsed || undefined}>
      <aside className="shell-side" id="shell-nav">
        <div className="shell-brand">
          {/* Hidden when collapsed — the wrapper (not the two elements
              individually) so the row's own justify-content can re-centre on
              just the toggle below without a hidden sibling still claiming
              space. */}
          <div className="shell-brand-mark">
            <img className="shell-logo" src="/frontier-logo.png" alt="Frontier Dental" />
            <span className="ui-eyebrow">MCP WoORKSPACE</span>
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
          {session.me ? (
            <>
              <Avatar name={session.me.name} size="md" />
              <span className="shell-who-text">
                <span className="shell-who-name">{session.me.name}</span>
                <span className="shell-who-role">{session.me.role}</span>
              </span>
              <Button variant="ghost" size="sm" className="shell-who-signout" onClick={session.signOut}>
                Sign out
              </Button>
            </>
          ) : (
            <span className="shell-who-role">
              {session.status === 'loading' ? 'Checking session…' : 'Not signed in'}
            </span>
          )}
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

        {session.status === 'anonymous' ? (
          <SignedOut />
        ) : session.status === 'error' ? (
          <Unreachable
            message={session.error ?? 'The gateway did not respond.'}
            onRetry={session.refresh}
          />
        ) : (
          children
        )}
      </main>

      <ConnectionStatus corner="bottom-right" />
    </div>
  )
}

function NavItem({ routeKey, router, collapsed }: { routeKey: RouteKey; router: Router; collapsed: boolean }) {
  const route = ROUTES[routeKey]
  const active = router.key === routeKey
  const ported = isPorted(routeKey)

  const link = (
    <a
      className="shell-nav-item"
      href={router.href(routeKey)}
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
