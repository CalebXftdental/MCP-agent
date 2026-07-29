import type { ReactNode } from 'react'
import './StatusPage.css'

export interface StatusPageProps {
  children: ReactNode
}

/**
 * Full-viewport, centered shell for a screen that isn't really part of the
 * workspace and shouldn't pretend to be by sitting inside its sidebar/topbar
 * chrome — "can't reach the gateway," "page not found." Used by
 * `GatewayErrorPage` and `NotFoundPage`, both rendered as top-level siblings
 * of `AppShell` in App.tsx rather than in its content slot, the same reason
 * `LoginPage` is: a sidebar advertising panels that can't currently be
 * reached (because the gateway's down, or because the path was wrong) is a
 * worse frame for the message than no sidebar at all.
 *
 * Shares its small brand mark with `LoginPage`/`SessionBoot` so every one of
 * the app's non-workspace screens reads as one family instead of each
 * improvising its own look.
 */
function StatusPage({ children }: StatusPageProps) {
  return (
    <div className="status-page">
      <img className="status-page-mark" src="/frontier-mark.png" alt="" />
      <div className="status-page-body">{children}</div>
    </div>
  )
}

export default StatusPage
