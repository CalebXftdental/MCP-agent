import { useCallback, useEffect, useMemo, useState } from 'react'
import type { KeyboardEvent } from 'react'
import {
  Badge,
  Button,
  Card,
  Chip,
  DataTable,
  DateTimePicker,
  Dropdown,
  EmptyState,
  Field,
  Input,
  Textarea,
  useToast,
  type BadgeTone,
  type Column,
  type DropdownOption,
} from '../components/ui'
import { ApiError, getCalendarSends, tryTool, type CalendarSend } from '../lib/api'
import { formatRelative, formatWhen } from '../lib/format'
import type { PageProps } from './types'
import './CalendarPage.css'

/**
 * Calendar — ports `renderCalendar()`: draft a .ics invite (governed through
 * the same tool playground `POST /dashboard/try-tool` the Developer tab
 * uses), plus the read-only queue of sends already approved and queued for
 * a connector.
 *
 * Three usability changes over the legacy form, all requested directly:
 *   - Attendees was one text input, comma-separated — a single typo-prone
 *     string with no feedback on what you'd actually typed. It's now an
 *     add-one-at-a-time row (mirroring the IP allowlist's chip tray): type a
 *     name, press Enter or Add, it lands as a removable `Chip`, validated as
 *     an email before it's allowed on the list.
 *   - The domain is a `Dropdown` defaulting to `@frontierdental.com` with
 *     `@venadogroup.com` as the other option, so the common case is picking
 *     a name and a company, not typing a full address. Typing something
 *     that already contains `@` is still taken as a complete address
 *     as-is — an external customer's email doesn't need to fight the
 *     dropdown to get in.
 *   - Start/End were plain text inputs with a placeholder like
 *     `2026-07-23T14:00:00-04:00` — a UTC-offset string someone has to
 *     hand-compute and keep in sync with DST. They're the kit's own
 *     `DateTimePicker` now (a calendar + time popover, not a native
 *     `datetime-local` input — a native one draws its own affordance
 *     outside this kit's styling entirely), paired with a `Timezone`
 *     Dropdown; the backend already accepts a naive local time alongside a
 *     separate IANA zone name (`mcp-calendar/app.py`'s floating-time path),
 *     so this is the more correct shape for what the tool always supported,
 *     not just a nicer-looking input.
 */

const DOMAIN_OPTIONS: DropdownOption[] = [
  { value: '@frontierdental.com', label: '@frontierdental.com' },
  { value: '@venadogroup.com', label: '@venadogroup.com' },
]

const TIMEZONE_OPTIONS: DropdownOption[] = [
  { value: 'America/Toronto', label: 'America/Toronto (ET)' },
  { value: 'America/New_York', label: 'America/New_York (ET)' },
  { value: 'America/Chicago', label: 'America/Chicago (CT)' },
  { value: 'America/Denver', label: 'America/Denver (MT)' },
  { value: 'America/Los_Angeles', label: 'America/Los_Angeles (PT)' },
  { value: 'UTC', label: 'UTC' },
]

const DEFAULT_DESCRIPTION = `Agenda:
- Review generated account packet
- Confirm follow-up owner
- Decide escalation path`

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/

function sendStatusTone(status: string): BadgeTone {
  return status === 'sent' ? 'ok' : 'warn'
}

interface CalendarDraftResult {
  status?: string
  message?: string
  errorCode?: string
  error?: string
  filename?: string
  draftId?: string
  artifactId?: string
  downloadUrl?: string
}

function CalendarPage({ navigate }: PageProps) {
  const toast = useToast()

  const [title, setTitle] = useState('')
  const [attendees, setAttendees] = useState<string[]>([])
  const [attendeeInput, setAttendeeInput] = useState('')
  const [attendeeDomain, setAttendeeDomain] = useState(DOMAIN_OPTIONS[0].value)
  const [attendeeError, setAttendeeError] = useState<string | null>(null)
  const [start, setStart] = useState('')
  const [end, setEnd] = useState('')
  const [timezone, setTimezone] = useState('America/Toronto')
  const [location, setLocation] = useState('')
  const [description, setDescription] = useState(DEFAULT_DESCRIPTION)

  const [drafting, setDrafting] = useState(false)
  const [draftResult, setDraftResult] = useState<CalendarDraftResult | null>(null)
  const [draftError, setDraftError] = useState<string | null>(null)

  const [sends, setSends] = useState<CalendarSend[]>([])
  const [queueState, setQueueState] = useState<'loading' | 'ready' | 'error'>('loading')
  const [queueError, setQueueError] = useState<string | null>(null)

  const loadQueue = useCallback(() => {
    let live = true
    setQueueState('loading')

    getCalendarSends()
      .then((result) => {
        if (!live) return
        setSends(result.sends ?? [])
        setQueueError(null)
        setQueueState('ready')
      })
      .catch((cause: unknown) => {
        if (!live) return
        setQueueError(cause instanceof Error ? cause.message : 'Could not load the calendar queue.')
        setQueueState('error')
      })

    return () => {
      live = false
    }
  }, [])

  useEffect(() => loadQueue(), [loadQueue])

  const addAttendee = useCallback(() => {
    const raw = attendeeInput.trim()
    if (!raw) {
      setAttendeeError('Enter an attendee')
      return
    }
    const email = raw.includes('@') ? raw : `${raw}${attendeeDomain}`
    if (!EMAIL_RE.test(email)) {
      setAttendeeError('Not a valid email address')
      return
    }
    if (attendees.some((a) => a.toLowerCase() === email.toLowerCase())) {
      setAttendeeError('Already added')
      return
    }
    setAttendees((prev) => [...prev, email])
    setAttendeeInput('')
    setAttendeeError(null)
  }, [attendeeInput, attendeeDomain, attendees])

  const onAttendeeKeyDown = useCallback(
    (e: KeyboardEvent<HTMLInputElement>) => {
      if (e.key !== 'Enter') return
      e.preventDefault()
      addAttendee()
    },
    [addAttendee],
  )

  const removeAttendee = useCallback((email: string) => {
    setAttendees((prev) => prev.filter((a) => a !== email))
  }, [])

  const submitDraft = useCallback(async () => {
    if (attendees.length === 0) {
      toast.warn('Add at least one attendee')
      return
    }
    if (!start || !end) {
      toast.warn('Set a start and end time')
      return
    }
    if (end <= start) {
      toast.warn('End must be after start')
      return
    }

    setDrafting(true)
    setDraftError(null)
    setDraftResult(null)
    try {
      const response = await tryTool(
        'draft_calendar_invite',
        {
          title: title.trim() || 'Calendar invite',
          attendees,
          start: `${start}:00`,
          end: `${end}:00`,
          timezone_name: timezone,
          location: location.trim(),
          description,
          classification: ['INTERNAL', 'PII'],
        },
        '',
      )
      const res = (response.result ?? {}) as CalendarDraftResult
      if (res.status !== 'success') {
        throw new Error(res.message || res.errorCode || res.error || res.status || 'Draft failed')
      }
      setDraftResult(res)
      toast.success('Invite draft created')
      loadQueue()
    } catch (cause) {
      setDraftError(cause instanceof ApiError ? cause.message : cause instanceof Error ? cause.message : 'Could not create that invite draft.')
    } finally {
      setDrafting(false)
    }
  }, [attendees, start, end, title, timezone, location, description, toast, loadQueue])

  const columns = useMemo<Column<CalendarSend>[]>(
    () => [
      {
        key: 'invite',
        header: 'Invite',
        render: (s) => (
          <>
            <div>{s.title || 'Invite'}</div>
            <div className="calendar-subtext ui-mono">
              {s.sendId} · draft {s.draftArtifactId}
            </div>
            <div className="calendar-subtext">
              {s.start} - {s.end} {s.timezone}
            </div>
            {s.message && <div className="calendar-subtext">{s.message}</div>}
          </>
        ),
      },
      {
        key: 'status',
        header: 'Status',
        width: '9rem',
        render: (s) => (
          <Badge tone={sendStatusTone(s.status)} dot>
            {s.status}
          </Badge>
        ),
      },
      { key: 'provider', header: 'Provider', width: '7rem' },
      { key: 'approvalId', header: 'Approval', mono: true, width: '9rem' },
      {
        key: 'attendees',
        header: 'Attendees',
        render: (s) => s.attendees.join(', ') || '—',
      },
      {
        key: 'createdAt',
        header: 'Created',
        width: '9rem',
        muted: true,
        nowrap: true,
        render: (s) => <span title={formatWhen(s.createdAt)}>{formatRelative(s.createdAt)}</span>,
      },
    ],
    [],
  )

  return (
    <div className="calendar">
      <Card
        title="Calendar invites"
        description="Create .ics invite drafts, route them for approval, then queue external calendar creation for Outlook/Google/Graph adapters."
      >
        <div className="calendar-stack">
          <Field label="Title">
            {(fieldProps) => (
              <Input {...fieldProps} placeholder="Customer renewal review" value={title} onChange={(e) => setTitle(e.target.value)} />
            )}
          </Field>

          <Field label="Attendees" hint="Pick a name and a domain, or type a full address — press Enter or Add." error={attendeeError ?? undefined}>
            {(fieldProps) => (
              <div className="calendar-attendee-row">
                <Input
                  {...fieldProps}
                  placeholder="manager"
                  value={attendeeInput}
                  onChange={(e) => {
                    setAttendeeInput(e.target.value)
                    setAttendeeError(null)
                  }}
                  onKeyDown={onAttendeeKeyDown}
                />
                <Dropdown value={attendeeDomain} onChange={setAttendeeDomain} options={DOMAIN_OPTIONS} />
                <Button size="sm" onClick={addAttendee}>
                  Add
                </Button>
              </div>
            )}
          </Field>

          {attendees.length > 0 && (
            <div className="calendar-chips">
              {attendees.map((email) => (
                <span className="calendar-chip" key={email}>
                  <Chip size="sm" onRemove={() => removeAttendee(email)} removeLabel={`Remove ${email}`}>
                    {email}
                  </Chip>
                </span>
              ))}
            </div>
          )}

          <div className="calendar-form-grid">
            <Field label="Start" hint="Your local time — paired with the timezone below.">
              {(fieldProps) => <DateTimePicker {...fieldProps} value={start} onChange={setStart} />}
            </Field>
            <Field label="End">
              {(fieldProps) => <DateTimePicker {...fieldProps} value={end} onChange={setEnd} />}
            </Field>
            <Field label="Timezone">
              {(fieldProps) => (
                <Dropdown {...fieldProps} value={timezone} onChange={setTimezone} options={TIMEZONE_OPTIONS} />
              )}
            </Field>
            <Field label="Location">
              {(fieldProps) => (
                <Input {...fieldProps} placeholder="Teams / Boardroom" value={location} onChange={(e) => setLocation(e.target.value)} />
              )}
            </Field>
          </div>

          <Field label="Description">
            {(fieldProps) => (
              <Textarea {...fieldProps} mono={false} rows={5} value={description} onChange={(e) => setDescription(e.target.value)} />
            )}
          </Field>

          <div className="calendar-form-actions">
            <Button onClick={submitDraft} loading={drafting}>
              Create invite draft
            </Button>
            <Button variant="ghost" onClick={() => navigate('files')}>
              View files
            </Button>
          </div>

          {draftError && <p className="calendar-draft-error">{draftError}</p>}

          {draftResult && (
            <Card className="calendar-draft-card" accent="ok">
              <p className="calendar-draft-filename">{draftResult.filename}</p>
              <p className="calendar-draft-id ui-mono">{draftResult.draftId || draftResult.artifactId}</p>
              <div className="calendar-form-actions">
                {draftResult.downloadUrl && (
                  <a href={draftResult.downloadUrl} className="calendar-download-link">
                    Download .ics
                  </a>
                )}
                <Button size="sm" variant="ghost" onClick={() => navigate('files')}>
                  Inspect in Files
                </Button>
              </div>
              <p className="calendar-draft-note">
                Request approval from Files, then queue creation with the approved approval id.
              </p>
            </Card>
          )}
        </div>
      </Card>

      <Card
        title="Calendar connector queue"
        description="Approved invites waiting for (or already handed to) a calendar connector."
        flush
        actions={
          <Button variant="quiet" size="sm" onClick={loadQueue} loading={queueState === 'loading'}>
            Refresh
          </Button>
        }
      >
        <DataTable
          columns={columns}
          rows={sends}
          rowKey={(s) => s.sendId}
          loading={queueState === 'loading'}
          skeletonRows={4}
          caption="Queued calendar invite sends"
          empty={
            queueState === 'error' ? (
              <EmptyState
                title="Couldn't load the queue"
                description={queueError ?? undefined}
                action={
                  <Button size="sm" variant="ghost" onClick={loadQueue}>
                    Try again
                  </Button>
                }
              />
            ) : (
              <EmptyState title="No queued calendar sends yet" description="Drafts appear here once approved and queued." />
            )
          }
        />
      </Card>
    </div>
  )
}

export default CalendarPage
