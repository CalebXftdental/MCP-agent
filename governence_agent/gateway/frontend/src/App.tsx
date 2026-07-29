import AppShell from './components/AppShell'
import GatewayErrorPage from './components/GatewayErrorPage'
import SessionBoot from './components/SessionBoot'
import { ToastProvider } from './components/ui'
import { useRoute } from './hooks/useRoute'
import { useSession } from './hooks/useSession'
import { pageFor } from './pages'
import LoginPage from './pages/LoginPage'
import NotFoundPage from './pages/NotFoundPage'
import { ROUTES } from './pages/routes'

/**
 * Composition root: session, routing, and the toast host, then whichever of
 * five top-level views the current state calls for. Only the last one is
 * actually the workspace — everything before it is a standalone screen with
 * no sidebar, because none of them have anything a workspace nav would help
 * with:
 *
 *   - `'loading'` → `SessionBoot`. Every fresh visit is `'loading'` for at
 *     least one round trip to `/dashboard/me`; defaulting that state to
 *     "assume signed in" (what this used to do) meant the dashboard chrome
 *     rendered first and was yanked away the instant the check resolved —
 *     a flash of the wrong app instead of an unknown one.
 *   - `'error'` → `GatewayErrorPage`. The workspace has nothing useful to
 *     offer when the gateway itself is the problem.
 *   - `'anonymous'` → `LoginPage`. A sign-in form doesn't want a sidebar
 *     around it advertising panels there's no session to open yet.
 *   - signed in, but `router.notFound` → `NotFoundPage`. A wrong path is its
 *     own dead end regardless of session state; the button back to Home on
 *     that page is the way out, not a nav item next to it implying there's
 *     something to browse to instead.
 *   - otherwise → `AppShell`, wrapping the routed page — the only case where
 *     the workspace layout is actually the right frame.
 *
 * ToastProvider stays outermost so `useToast()` works from any branch.
 * Routing takes the role, because an admin deep link has to resolve to Home
 * for a non-admin rather than rendering a panel whose every request will
 * come back 403.
 */
function App() {
  const session = useSession()
  const router = useRoute(session.role)
  const Page = pageFor(router.key)

  return (
    <ToastProvider>
      {session.status === 'loading' ? (
        <SessionBoot />
      ) : session.status === 'error' ? (
        <GatewayErrorPage
          message={session.error ?? 'The gateway did not respond.'}
          errorKind={session.errorKind}
          onRetry={session.refresh}
        />
      ) : session.status === 'anonymous' ? (
        <LoginPage session={session} />
      ) : router.notFound ? (
        <NotFoundPage path={location.pathname} navigate={router.navigate} />
      ) : (
        <AppShell session={session} router={router}>
          <Page
            route={ROUTES[router.key]}
            session={session}
            navigate={router.navigate}
            routeParam={router.param}
          />
        </AppShell>
      )}
    </ToastProvider>
  )
}

export default App
