import { Button, Card } from '../components/ui'
import type { PageProps } from './types'
import './PlaceholderPage.css'

/**
 * Stands in for a tab that exists in the registry but hasn't been ported from
 * `static/app.html` yet.
 *
 * It links to the live legacy panel rather than apologising, because the legacy
 * panel still works: every bare route stays registered alongside its `/backend`
 * twin (gateway/backend/__init__.py), so `/dashboard/<key>` is a working page for
 * as long as the port takes. Sending someone to a dead end would be the only
 * real failure here.
 */

function PlaceholderPage({ route }: PageProps) {
  return (
    <div className="stub">
      <Card
        title={
          <span className="stub-title">
            <span className="stub-icon" aria-hidden="true">
              {route.icon}
            </span>
            {route.title}
          </span>
        }
        description={route.description}
        actions={
          <Button
            variant="ghost"
            size="sm"
            // A full page load, not a tab change: this leaves the React app for
            // the legacy one. Same origin, so the session cookie rides along.
            onClick={() => {
              location.href = `/dashboard/${route.key}`
            }}
          >
            Open the current version →
          </Button>
        }
      >
        <p className="stub-body">
          Not ported to the new console yet. The existing panel is still live and
          fully functional — the link above opens it.
        </p>
      </Card>
    </div>
  )
}

export default PlaceholderPage
