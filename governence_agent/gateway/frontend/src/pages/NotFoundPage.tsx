import { Button, EmptyState } from '../components/ui'
import StatusPage from '../components/StatusPage'
import type { RouteKey } from './routes'
import './NotFoundPage.css'

/**
 * Rendered by App.tsx in place of everything else when the current path
 * isn't a real destination at all (`router.notFound`, from useRoute.ts) — a
 * typo, a stale link, or a tab that's since been retired. Previously this
 * case was indistinguishable from actually navigating to Home: `resolveRoute`
 * always returns *some* valid key, so an unrecognised path silently landed on
 * Home with nothing to say a link didn't go where it claimed to.
 *
 * A standalone screen (`StatusPage`, same as `GatewayErrorPage`), not nested
 * inside `AppShell`'s sidebar — the "Back to Home" button below is the one
 * and only way out, not a nav item alongside it, since a workspace sidebar
 * offering panels next to a page that says "this doesn't exist" is a mixed
 * message about whether anything's actually wrong.
 */

function CompassIcon() {
  return (
    <svg viewBox="0 0 40 40" width="34" height="34" aria-hidden="true">
      <circle cx="20" cy="20" r="17" fill="none" stroke="currentColor" strokeWidth="1.6" />
      <path
        d="M26 14 17 18l-3 9 9-4 3-9Z"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinejoin="round"
      />
      <circle cx="20" cy="20" r="1.6" fill="currentColor" />
    </svg>
  )
}

interface NotFoundPageProps {
  path: string
  navigate: (key: RouteKey) => void
}

function NotFoundPage({ path, navigate }: NotFoundPageProps) {
  return (
    <StatusPage>
      <p className="not-found-digits" aria-hidden="true">
        404
      </p>
      <EmptyState
        icon={<CompassIcon />}
        title="We couldn't find that page"
        description={
          <>
            There's nothing at <code className="ui-mono">{path}</code>. It may have moved, or the
            link might be out of date.
          </>
        }
        action={
          <Button onClick={() => navigate('home')} block>
            Back to Home
          </Button>
        }
      />
    </StatusPage>
  )
}

export default NotFoundPage
