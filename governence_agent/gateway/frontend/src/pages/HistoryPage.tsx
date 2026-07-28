import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Badge,
  Button,
  Card,
  DataTable,
  EmptyState,
  SegmentedControl,
  Stat,
  StatGrid,
  useToast,
  type Column,
} from '../components/ui'
import { useStoredList } from '../hooks/useStoredList'
import {
  ApiError,
  getMyActivity,
  type ActivitySummary,
  type GovernedCall,
  type RateLimitHeadroom,
} from '../lib/api'
import { formatLatency, formatRelative, formatWhen } from '../lib/format'
import type { PageProps } from './types'
import './HistoryPage.css'

/**
 * History — what you've already done: your governed calls, and the answers you
 * pinned from Home.
 *
 * Both panels were on Home, where they competed with the composer for the first
 * screen and were capped at six rows to fit. Here they get the whole page, so:
 *
 *   - the summary the endpoint already returns becomes a stat strip instead of
 *     being thrown away,
 *   - all 200 calls the endpoint returns are shown, in a scrollable dense table
 *     with a sticky header, rather than the first six,
 *   - each call gets its rows and latency, which Home had no width for,
 *   - a status filter, because "what got denied" is the question this page is
 *     actually opened to answer.
 *
 * The legacy `My Activity` tab (rate-limit headroom, redaction detail) was
 * retired once this page covered the same ground — no second door to the
 * same data.
 */

interface SavedAnswer {
  text: string
  /** Epoch ms — also the identity, matching the legacy `removeAnswer(ts)`. */
  ts: number
}

/** Longest preview the legacy saved-answer row showed. */
const PREVIEW_CHARS = 180

type Filter = 'all' | 'ok' | 'denied' | 'error'

const FILTERS: { value: Filter; label: string; title: string }[] = [
  { value: 'all', label: 'All', title: 'Every governed call' },
  { value: 'ok', label: 'Allowed', title: 'Calls the policy allowed' },
  { value: 'denied', label: 'Denied', title: 'Calls the policy refused' },
  { value: 'error', label: 'Errors', title: 'Calls that reached a backend and failed' },
]

/** ok stays green; denied is red, not the legacy amber-labelled-`warn` red; a
 *  hard error is amber rather than the legacy plain grey, which made failures
 *  the least visible status in the list. */
function statusTone(status: string): 'ok' | 'danger' | 'warn' | 'neutral' {
  if (status === 'ok') return 'ok'
  if (status === 'denied') return 'danger'
  if (status === 'error') return 'warn'
  return 'neutral'
}

const EMPTY_SUMMARY: ActivitySummary = {
  total: 0,
  ok: 0,
  denied: 0,
  error: 0,
  redactions: 0,
  rows: 0,
}

function HistoryPage({ session, navigate }: PageProps) {
  const toast = useToast()
  const savedAnswers = useStoredList<SavedAnswer>('gov_savedans', session.name)

  const [calls, setCalls] = useState<GovernedCall[]>([])
  const [summary, setSummary] = useState<ActivitySummary>(EMPTY_SUMMARY)
  const [rateLimit, setRateLimit] = useState<RateLimitHeadroom | null>(null)
  const [state, setState] = useState<'loading' | 'ready' | 'error'>('loading')
  const [error, setError] = useState<string | null>(null)
  const [filter, setFilter] = useState<Filter>('all')

  const load = useCallback(() => {
    let live = true
    setState('loading')

    getMyActivity()
      .then((result) => {
        if (!live) return
        setCalls(result.calls ?? [])
        setSummary(result.summary ?? EMPTY_SUMMARY)
        setRateLimit(result.rate_limit ?? null)
        setError(null)
        setState('ready')
      })
      .catch((cause: unknown) => {
        if (!live) return
        setCalls([])
        setSummary(EMPTY_SUMMARY)
        setRateLimit(null)
        setError(
          cause instanceof ApiError && cause.isUnauthenticated
            ? 'Your session expired — sign in again to see your history.'
            : cause instanceof Error
              ? cause.message
              : 'Could not load your history.',
        )
        setState('error')
      })

    return () => {
      live = false
    }
  }, [])

  // Keyed on the principal: after a sign-in or account switch the previous
  // user's calls must not linger on screen.
  useEffect(() => load(), [load, session.name])

  const shown = useMemo(
    () => (filter === 'all' ? calls : calls.filter((call) => call.status === filter)),
    [calls, filter],
  )

  const copyAnswer = useCallback(
    async (answer: SavedAnswer) => {
      try {
        await navigator.clipboard.writeText(answer.text)
        toast.success('Copied')
      } catch {
        // navigator.clipboard is absent on insecure origins, so this path is real.
        toast.error('Could not copy — your browser blocked clipboard access.')
      }
    },
    [toast],
  )

  const columns = useMemo<Column<GovernedCall>[]>(
    () => [
      {
        key: 'ts',
        header: 'When',
        width: '9rem',
        nowrap: true,
        muted: true,
        render: (call) => <span title={formatWhen(call.ts)}>{formatRelative(call.ts)}</span>,
      },
      { key: 'tool', header: 'Tool', mono: true },
      {
        key: 'status',
        header: 'Status',
        width: '7rem',
        render: (call) => (
          <Badge tone={statusTone(call.status)} dot>
            {call.status}
          </Badge>
        ),
      },
      {
        key: 'rows',
        header: 'Rows',
        width: '5rem',
        numeric: true,
        // Denied calls never reached a backend, so they have no row count — an
        // em dash, not a 0, which would read as "returned nothing".
        render: (call) => (call.rows == null ? '—' : call.rows.toLocaleString()),
      },
      {
        key: 'latency_ms',
        header: 'Took',
        width: '5.5rem',
        numeric: true,
        muted: true,
        render: (call) => formatLatency(call.latency_ms),
      },
    ],
    [],
  )

  return (
    <div className="history">
      <StatGrid columns={4}>
        <Stat
          label="Governed calls"
          value={summary.total}
          detail={
            rateLimit
              ? `${rateLimit.used.toLocaleString()} of ${rateLimit.limit.toLocaleString()} this hour`
              : undefined
          }
        />
        <Stat label="Denied" value={summary.denied} tone={summary.denied > 0 ? 'danger' : 'neutral'} />
        <Stat label="Redactions" value={summary.redactions} />
        <Stat label="Rows returned" value={summary.rows} />
      </StatGrid>

      <div className="history-columns">
        <Card
          title="Recent activity"
          description="Every governed call this session has made, newest first."
          flush
          actions={
            <>
              <SegmentedControl
                label="Filter by status"
                segments={FILTERS}
                value={filter}
                onChange={setFilter}
              />
              <Button variant="quiet" size="sm" onClick={load} loading={state === 'loading'}>
                Refresh
              </Button>
            </>
          }
          footer={
            state === 'ready' && calls.length > 0 ? (
              <>
                <span>
                  {filter === 'all'
                    ? `${calls.length.toLocaleString()} calls`
                    : `${shown.length.toLocaleString()} of ${calls.length.toLocaleString()} calls`}
                </span>
                {/* The endpoint caps at 200 server-side, so say so rather than
                    letting the list look complete when it isn't. */}
                {calls.length >= 200 && <span>most recent 200</span>}
              </>
            ) : undefined
          }
        >
          <DataTable
            columns={columns}
            rows={shown}
            rowKey={(call, i) => `${call.ts}-${call.tool}-${i}`}
            loading={state === 'loading'}
            skeletonRows={8}
            caption="Your governed tool calls"
            dense
            stickyHeader
            maxHeight="30rem"
            empty={
              state === 'error' ? (
                <EmptyState
                  title="Couldn’t load your history"
                  description={error ?? undefined}
                  action={
                    <Button size="sm" variant="ghost" onClick={load}>
                      Try again
                    </Button>
                  }
                />
              ) : filter !== 'all' ? (
                <EmptyState
                  title={`No ${filter === 'ok' ? 'allowed' : filter} calls`}
                  description="Nothing in this session matches that status."
                  action={
                    <Button size="sm" variant="ghost" onClick={() => setFilter('all')}>
                      Show all
                    </Button>
                  }
                />
              ) : (
                <EmptyState
                  title="No activity yet"
                  description="Ask something on Home to get started."
                  action={
                    <Button size="sm" onClick={() => navigate('home')}>
                      Go to Home
                    </Button>
                  }
                />
              )
            }
          />
        </Card>

        <div className="history-aside">
          <Card
            title="Saved answers"
            description="Pinned from Home"
            actions={
              savedAnswers.items.length > 1 ? (
                <Button variant="quiet" size="sm" onClick={savedAnswers.clear}>
                  Clear all
                </Button>
              ) : undefined
            }
          >
            {savedAnswers.items.length === 0 ? (
              <EmptyState
                title="No saved answers yet"
                description="Pin one with ☆ on Home."
              />
            ) : (
              <ul className="history-saved">
                {savedAnswers.items.map((answer) => (
                  <li className="history-saved-row" key={answer.ts}>
                    <p className="history-saved-text" title={answer.text}>
                      {answer.text.slice(0, PREVIEW_CHARS)}
                      {answer.text.length > PREVIEW_CHARS ? '…' : ''}
                    </p>
                    <div className="history-saved-foot">
                      {/* Saved-answer timestamps are epoch MS from Date.now(),
                          unlike audit timestamps — hence the /1000. */}
                      <span className="history-saved-when" title={formatWhen(answer.ts / 1000)}>
                        {formatRelative(answer.ts / 1000)}
                      </span>
                      <span className="history-saved-actions">
                        <Button variant="ghost" size="sm" onClick={() => copyAnswer(answer)}>
                          Copy
                        </Button>
                        <Button
                          variant="danger"
                          size="sm"
                          onClick={() => savedAnswers.remove((saved) => saved.ts === answer.ts)}
                          aria-label="Remove saved answer"
                        >
                          ✕
                        </Button>
                      </span>
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </Card>

          <Card title="Past conversations" description="Not ported to this console yet.">
            <p className="history-note">
              Your assistant conversations — each closes and gets summarized after
              1h idle — are still on the current console.
            </p>
            <Button
              variant="ghost"
              size="sm"
              onClick={() => {
                location.href = '/dashboard/history'
              }}
            >
              Open conversations →
            </Button>
          </Card>
        </div>
      </div>
    </div>
  )
}

export default HistoryPage
