import AppShell from './components/AppShell'
import { ToastProvider } from './components/ui'
import { useRoute } from './hooks/useRoute'
import { useSession } from './hooks/useSession'
import { pageFor } from './pages'
import { ROUTES } from './pages/routes'

/**
 * Composition root: session, routing, and the toast host, then the shell around
 * whichever page the current tab resolves to.
 *
 * ToastProvider is outermost so `useToast()` works from the shell as well as
 * from pages. Routing takes the role, because an admin deep link has to resolve
 * to Home for a non-admin rather than rendering a panel whose every request will
 * come back 403.
 */
function App() {
  const session = useSession()
  const router = useRoute(session.role)
  const Page = pageFor(router.key)

  return (
    <ToastProvider>
      <AppShell session={session} router={router}>
        <Page route={ROUTES[router.key]} session={session} navigate={router.navigate} />
      </AppShell>
    </ToastProvider>
  )
}

export default App
