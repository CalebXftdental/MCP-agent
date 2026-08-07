import { useCallback, useEffect, useMemo, useState } from 'react'
import { Badge, Button, Card, CidrListEditor, Drawer, EmptyState, Skeleton, useToast } from '../components/ui'
import { ApiError, getWhitelist, setWhitelist } from '../lib/api'
import type { PageProps } from './types'
import './WhitelistPage.css'

/**
 * IP Allowlist — ports `renderWhitelist()`: a global CIDR list enforced only
 * on `/mcp` traffic. The legacy panel was one bare `&lt;textarea&gt;` (one CIDR
 * per line) and a single Save button, with no format feedback at all — any
 * string reached the PUT body verbatim, and the backend accepts anything
 * too, silently no-op'ing a malformed entry instead of rejecting it.
 *
 * What changed and why:
 *   - Entries are now typed one at a time into a real input, validated
 *     client-side (`lib/cidr.ts`) before they're allowed onto the list, and
 *     shown as removable chips — mirroring the tray pattern RequestPicker
 *     already uses for "things you've added, that you can take back off."
 *     There's still nothing stopping a syntactically-valid-but-wrong CIDR,
 *     but a flat typo no longer reaches Save.
 *   - An empty list means "allow every IP" — the safe default, not a
 *     lockout — so the scary direction is the FIRST entry (enforcement
 *     switches on for every other caller the moment it saves) and the
 *     confirm step says so explicitly, the same way Security's break-glass
 *     Apply diff does. Removing the last entry back to allow-all gets a
 *     lighter heads-up rather than the same red treatment.
 */

function WhitelistPage(_props: PageProps) {
  const toast = useToast()

  const [saved, setSaved] = useState<string[]>([])
  const [draft, setDraft] = useState<string[]>([])
  const [state, setState] = useState<'loading' | 'ready' | 'error'>('loading')
  const [error, setError] = useState<string | null>(null)

  const [confirmOpen, setConfirmOpen] = useState(false)
  const [saving, setSaving] = useState(false)

  const load = useCallback(() => {
    let live = true
    setState('loading')

    getWhitelist()
      .then((result) => {
        if (!live) return
        const list = result.whitelist ?? []
        setSaved(list)
        setDraft(list)
        setError(null)
        setState('ready')
      })
      .catch((cause: unknown) => {
        if (!live) return
        setError(cause instanceof Error ? cause.message : 'Could not load the allowlist.')
        setState('error')
      })

    return () => {
      live = false
    }
  }, [])

  useEffect(() => load(), [load])

  const added = useMemo(() => draft.filter((d) => !saved.includes(d)), [draft, saved])
  const removed = useMemo(() => saved.filter((d) => !draft.includes(d)), [saved, draft])
  const dirty = added.length > 0 || removed.length > 0
  const activating = saved.length === 0 && draft.length > 0
  const deactivating = saved.length > 0 && draft.length === 0

  const discard = useCallback(() => {
    setDraft(saved)
  }, [saved])

  const doSave = useCallback(async () => {
    setSaving(true)
    try {
      const result = await setWhitelist(draft)
      const list = result.whitelist ?? []
      setSaved(list)
      setDraft(list)
      toast.success('Allowlist saved')
      setConfirmOpen(false)
    } catch (cause) {
      toast.error(cause instanceof ApiError ? cause.message : 'Could not save the allowlist.')
    } finally {
      setSaving(false)
    }
  }, [draft, toast])

  return (
    <div className="whitelist">
      <Card
        title="IP Allowlist"
        description="Global CIDR allowlist enforced on every request -- MCP tool calls, the login page, and the dashboard alike."
        actions={
          state === 'ready' ? (
            <Badge tone={saved.length > 0 ? 'warn' : 'neutral'} dot>
              {saved.length > 0 ? `restricted · ${saved.length} range${saved.length === 1 ? '' : 's'}` : 'allow all'}
            </Badge>
          ) : undefined
        }
      >
        {state === 'loading' ? (
          <Skeleton height="9rem" />
        ) : state === 'error' ? (
          <EmptyState
            title="Couldn't load the allowlist"
            description={error ?? undefined}
            action={
              <Button size="sm" variant="ghost" onClick={load}>
                Try again
              </Button>
            }
          />
        ) : (
          <div className="whitelist-stack">
            <div className="whitelist-tray-head">
              <span className="ui-eyebrow">Current list</span>
              <span className="whitelist-tray-count">
                {draft.length === 0 ? 'allow all' : `${draft.length} ${draft.length === 1 ? 'entry' : 'entries'}`}
              </span>
            </div>

            <CidrListEditor value={draft} onChange={setDraft} emptyHint="No entries — every IP can reach the MCP edge." />

            {dirty && (
              <div className="whitelist-dirty-bar">
                <span className="whitelist-dirty-text">Unsaved allowlist changes</span>
                <div className="whitelist-dirty-actions">
                  <Button variant="ghost" size="sm" onClick={discard}>
                    Discard
                  </Button>
                  <Button size="sm" variant={activating ? 'danger' : 'primary'} onClick={() => setConfirmOpen(true)}>
                    Save changes
                  </Button>
                </div>
              </div>
            )}
          </div>
        )}
      </Card>

      <Drawer
        open={confirmOpen}
        onClose={() => setConfirmOpen(false)}
        eyebrow="IP allowlist"
        title="Save these changes?"
        footer={
          <>
            <Button variant="ghost" onClick={() => setConfirmOpen(false)} disabled={saving}>
              Cancel
            </Button>
            <Button variant={activating ? 'danger' : 'primary'} onClick={doSave} loading={saving}>
              Save
            </Button>
          </>
        }
      >
        <div className="whitelist-decision-body">
          {activating && (
            <p className="whitelist-warning whitelist-warning--danger">
              This is the first entry — every IP not listed here will be blocked the moment this saves,
              including the login page and this dashboard. Make sure your own IP is in the list before saving.
            </p>
          )}
          {deactivating && (
            <p className="whitelist-warning">
              Removing the last entry returns to allow-all — every IP will be able to reach the login page,
              dashboard, and /mcp.
            </p>
          )}

          {added.length > 0 && (
            <div>
              <p className="ui-eyebrow whitelist-diff-label">Adding</p>
              <div className="whitelist-chips">
                {added.map((cidr) => (
                  <Badge key={cidr} tone="warn" subtle>
                    {cidr}
                  </Badge>
                ))}
              </div>
            </div>
          )}

          {removed.length > 0 && (
            <div>
              <p className="ui-eyebrow whitelist-diff-label">Removing</p>
              <div className="whitelist-chips">
                {removed.map((cidr) => (
                  <Badge key={cidr} tone="ok" subtle>
                    {cidr}
                  </Badge>
                ))}
              </div>
            </div>
          )}

          <p className="whitelist-note">Takes effect on the next request — no rollout delay.</p>
        </div>
      </Drawer>
    </div>
  )
}

export default WhitelistPage
