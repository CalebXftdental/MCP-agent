import { Button, Card, EmptyState } from './ui'
import type { SessionErrorKind } from '../hooks/useSession'
import StatusPage from './StatusPage'
import './GatewayErrorPage.css'

/**
 * Rendered by App.tsx in place of everything else for `session.status ===
 * 'error'` — a generic standalone screen, not nested inside `AppShell` (see
 * `StatusPage`'s own docstring for why): the workspace layout has nothing
 * useful to offer when the gateway itself is the problem, so it shouldn't be
 * on screen pretending otherwise.
 *
 * Split on `errorKind` rather than one generic "can't reach it" message: an
 * offline gateway (nothing answered at all) and a gateway that answered with
 * something wrong (a 404, a 500, a stale build) are different problems with
 * different fixes, and telling someone to go run `python app.py` when the
 * process is already running and just returned a bad status is actively
 * misleading.
 */

function PlugIcon() {
  return (
    <svg viewBox="0 0 20 20" width="18" height="18" aria-hidden="true">
      <path
        d="M7 2.5v3M13 2.5v3M6 5.5h8v3a4 4 0 0 1-4 4v0a4 4 0 0 1-4-4v-3ZM10 12.5v5"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  )
}

function AlertIcon() {
  return (
    <svg viewBox="0 0 20 20" width="18" height="18" aria-hidden="true">
      <path
        d="M10 2.5 18 17H2L10 2.5Z"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinejoin="round"
      />
      <path d="M10 8v3.2" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
      <circle cx="10" cy="14" r="0.9" fill="currentColor" />
    </svg>
  )
}

export interface GatewayErrorPageProps {
  message: string
  errorKind: SessionErrorKind
  onRetry: () => void
}

function GatewayErrorPage({ message, errorKind, onRetry }: GatewayErrorPageProps) {
  const offline = errorKind === 'offline'
  return (
    <StatusPage>
      <Card accent="danger">
        <EmptyState
          icon={offline ? <PlugIcon /> : <AlertIcon />}
          title={offline ? "Can't reach the gateway" : 'Something went wrong'}
          description={
            offline ? (
              <>
                {message} Start it with <code className="ui-mono">python app.py</code> in{' '}
                <code className="ui-mono">gateway/</code>, then retry. The indicator in the
                bottom-right corner reports what it can see.
              </>
            ) : (
              <>The gateway responded, but with an error: {message}. Try again in a moment.</>
            )
          }
          action={
            <Button variant="ghost" onClick={onRetry}>
              Try again
            </Button>
          }
        />
      </Card>
    </StatusPage>
  )
}

export default GatewayErrorPage
