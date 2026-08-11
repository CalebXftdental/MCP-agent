import { Component } from 'react'
import type { ErrorInfo, ReactNode } from 'react'
import { Button, Card, EmptyState } from './ui'

export interface PageErrorBoundaryProps {
  children: ReactNode
}

interface PageErrorBoundaryState {
  error: Error | null
}

/**
 * Catches a render crash in the routed page content so it shows a recoverable
 * message instead of taking the whole app down. Without ANY boundary
 * (App.tsx had none before this), an uncaught render error unmounts the
 * entire React tree, leaving #root empty -- which in dark mode (theme.css's
 * `--ui-bg: #0f1418`) reads as the whole screen going solid black, with no
 * on-screen trace of what happened or why.
 *
 * Scoped to just the page slot (App.tsx wraps `<Page>`, not the whole app),
 * so the sidebar/topbar in AppShell survive a crash on one page -- there's
 * still a way to navigate off whatever broke instead of a dead end.
 */
class PageErrorBoundary extends Component<PageErrorBoundaryProps, PageErrorBoundaryState> {
  state: PageErrorBoundaryState = { error: null }

  static getDerivedStateFromError(error: Error) {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // eslint-disable-next-line no-console
    console.error('Page crashed:', error, info.componentStack)
  }

  render() {
    const { error } = this.state
    if (!error) return this.props.children
    return (
      <Card accent="danger">
        <EmptyState
          title="Something went wrong on this page"
          description={
            <>
              {error.message || 'An unexpected error occurred.'} Reloading usually fixes it — if it keeps
              happening on the same page, let the team know what you clicked right before it broke.
            </>
          }
          action={
            <Button variant="ghost" onClick={() => window.location.reload()}>
              Reload
            </Button>
          }
        />
      </Card>
    )
  }
}

export default PageErrorBoundary
