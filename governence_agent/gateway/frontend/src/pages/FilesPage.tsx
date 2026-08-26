import { useCallback, useEffect, useMemo, useState } from 'react'
import type { CSSProperties, KeyboardEvent } from 'react'
import {
  AddButton,
  Badge,
  Button,
  Card,
  Chip,
  DataTable,
  Drawer,
  Dropdown,
  EmptyState,
  Field,
  FileTypeIcon,
  Input,
  KeyValue,
  Modal,
  PageShell,
  SegmentedControl,
  Skeleton,
  Switch,
  Textarea,
  useToast,
  type BadgeTone,
  type Column,
  type DropdownOption,
  type Segment,
} from '../components/ui'
import OnlyOfficeEditor from '../components/files/OnlyOfficeEditor'
import {
  ApiError,
  addArtifactVersion,
  createArtifact,
  createArtifactShare,
  deleteArtifact,
  getArtifactWorkbench,
  listArtifacts,
  requestArtifactApproval,
  revokeArtifactShare,
  type Artifact,
  type ArtifactShare,
  type ArtifactVersionRecord,
  type ArtifactWorkbench,
} from '../lib/api'
import { formatBytes, formatRelative, formatWhen } from '../lib/format'
import type { PageProps } from './types'
import './FilesPage.css'

/**
 * Files — ports `renderFiles()`: generated/uploaded artifacts, their
 * classifications, and downloads.
 *
 * The legacy panel was a flat, unbounded `<table>` (every artifact the server
 * returned, no cap on the page's own height) plus an inline detail block that
 * replaced itself into the same page rather than overlaying it. Changes here:
 *
 *   - A card grid instead of a table — files aren't rows of structured fields
 *     the way audit calls or consumers are, they're objects with a name, a
 *     type, and a size, which reads better as a small icon-fronted tile than
 *     as table columns. `FileTypeIcon` is new (no icon system existed before)
 *     — a colour-coded page glyph monogrammed by type, the same idea Drive
 *     or Office use so the grid reads at a glance without a thumbnail render.
 *   - The grid scrolls inside a bounded height rather than growing the whole
 *     page — a library of a few hundred files stays a fixed, comfortable
 *     viewport-worth rather than a scroll of unknown length.
 *   - Search + type filter, which the legacy version had neither of.
 *   - The inline detail block is a Drawer, matching every other manage view
 *     in this console — Download / New version / Share / Request approval /
 *     Delete swap into the footer the same way Templates' Disable/Add-version
 *     do.
 *   - The ONLYOFFICE viewer/editor opens in a large Modal, not embedded
 *     inline in the (27rem-wide) drawer — a spreadsheet or slide deck isn't
 *     meaningfully readable at drawer width, and the whole point of the
 *     bounded-viewport work on this page was to stop cramming things into a
 *     space too small to actually use. The drawer just gets a summary line
 *     and an "Open" button; `OnlyOfficeEditor` (new) owns the actual
 *     `DocsAPI.DocEditor` lifecycle — script loading (cached per Document
 *     Server origin, matching the legacy `_loadOnlyofficeApi`), mount, and
 *     `destroyEditor()` on unmount.
 *
 * Scoped OUT of this port: the document-review thread (comments, approve/
 * request-changes). That's a distinct, much larger surface (effectively a
 * commenting system) — worth its own pass rather than folding into "view
 * your files." Read-only Approvals (+ requesting one) stayed in since it's
 * small and directly about file governance.
 */

const CLASSIFICATIONS = ['PUBLIC', 'INTERNAL', 'PII', 'SENSITIVE']

const RISK_OPTIONS: DropdownOption[] = [
  { value: 'low', label: 'Low' },
  { value: 'medium', label: 'Medium' },
  { value: 'high', label: 'High' },
]

const MAX_UPLOAD_BYTES = 25 * 1024 * 1024

type UploadMode = 'file' | 'text'
type ManageSubView = 'view' | 'version' | 'share' | 'approval' | 'delete'

function fileToBase64(file: File): Promise<{ base64: string; mimeType: string }> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => {
      const result = reader.result as string
      const comma = result.indexOf(',')
      resolve({ base64: comma === -1 ? result : result.slice(comma + 1), mimeType: file.type || 'application/octet-stream' })
    }
    reader.onerror = () => reject(reader.error ?? new Error('Could not read that file'))
    reader.readAsDataURL(file)
  })
}

function humanizeKey(key: string): string {
  const spaced = key.replace(/([a-z0-9])([A-Z])/g, '$1 $2')
  return spaced.charAt(0).toUpperCase() + spaced.slice(1)
}

function formatSignalValue(value: unknown): string {
  if (value == null || value === '') return '—'
  if (Array.isArray(value)) return value.length === 0 ? '—' : value.map(String).join(', ')
  if (typeof value === 'object') return JSON.stringify(value)
  if (typeof value === 'boolean') return value ? 'Yes' : 'No'
  return String(value)
}

/** ONLYOFFICE's config is opaque (see `OnlyOfficeConfig` in lib/api.ts) — this
 *  is the one field the drawer's own copy needs, read defensively since
 *  nothing guarantees the nested shape beyond what ONLYOFFICE's API docs say
 *  today. */
function editorMode(config: Record<string, unknown>): 'edit' | 'view' {
  const editorConfig = config.editorConfig
  if (editorConfig && typeof editorConfig === 'object' && (editorConfig as Record<string, unknown>).mode === 'edit') {
    return 'edit'
  }
  return 'view'
}

function statusTone(status: string): BadgeTone {
  if (status === 'active') return 'ok'
  if (status === 'expired' || status === 'revoked' || status === 'denied') return 'danger'
  if (status === 'pending' || status === 'open') return 'warn'
  return 'neutral'
}

/** Renders identically to `Button` (same classes) so a plain download link
 *  sits in a footer or action row indistinguishably from a real button —
 *  `Button` itself has no polymorphic `as`, and a download needs a real
 *  `<a href download>`, not a click handler. */
function DownloadLink({
  href,
  variant = 'ghost',
  size = 'sm',
}: {
  href: string
  variant?: 'primary' | 'ghost'
  size?: 'sm' | 'md'
}) {
  return (
    <a
      href={href}
      className={`ui-btn ui-btn--${variant} ui-btn--${size}`}
      onClick={(e) => e.stopPropagation()}
    >
      <span className="ui-btn-content">Download</span>
    </a>
  )
}

function DownloadIcon() {
  return (
    <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
      <path
        d="M8 2v7.5m0 0L5 6.5m3 3 3-3M3 12.5h10"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  )
}

interface FileCardProps {
  artifact: Artifact
  index: number
  onOpen: () => void
}

function FileCard({ artifact, index, onOpen }: FileCardProps) {
  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault()
      onOpen()
    }
  }

  return (
    <div
      className="files-card"
      style={{ '--ui-i': index } as CSSProperties}
      role="button"
      tabIndex={0}
      onClick={onOpen}
      onKeyDown={onKeyDown}
    >
      <a
        className="files-card-download"
        href={artifact.downloadUrl}
        title="Download"
        aria-label={`Download ${artifact.title}`}
        onClick={(e) => e.stopPropagation()}
      >
        <DownloadIcon />
      </a>

      <FileTypeIcon type={artifact.type} size={38} />

      <div className="files-card-body">
        <p className="files-card-title" title={artifact.title}>
          {artifact.title || artifact.filename}
        </p>
        <p className="files-card-filename ui-mono" title={artifact.filename}>
          {artifact.filename}
        </p>
      </div>

      {artifact.classification.length > 0 && (
        <div className="files-card-pills">
          {artifact.classification.slice(0, 3).map((c) => (
            <Badge key={c} subtle>
              {c}
            </Badge>
          ))}
        </div>
      )}

      <div className="files-card-foot">
        <span>{formatBytes(artifact.sizeBytes)}</span>
        <span title={formatWhen(artifact.createdAt)}>{formatRelative(artifact.createdAt)}</span>
      </div>

      {artifact.shared && (
        <Badge tone="info" subtle className="files-card-shared">
          shared
        </Badge>
      )}
    </div>
  )
}

function FilesPage({ session }: PageProps) {
  const toast = useToast()

  const [artifacts, setArtifacts] = useState<Artifact[]>([])
  const [state, setState] = useState<'loading' | 'ready' | 'error'>('loading')
  const [error, setError] = useState<string | null>(null)

  const [search, setSearch] = useState('')
  const [typeFilter, setTypeFilter] = useState('all')

  // ── upload modal ─────────────────────────────────────────────────────────
  const [uploadOpen, setUploadOpen] = useState(false)
  const [uploadMode, setUploadMode] = useState<UploadMode>('file')
  const [uploadTitle, setUploadTitle] = useState('')
  const [uploadClassification, setUploadClassification] = useState('INTERNAL')
  const [uploadFile, setUploadFile] = useState<File | null>(null)
  const [uploadText, setUploadText] = useState('')
  const [uploading, setUploading] = useState(false)

  // ── manage drawer ────────────────────────────────────────────────────────
  const [manageId, setManageId] = useState<string | null>(null)
  const [workbench, setWorkbench] = useState<ArtifactWorkbench | null>(null)
  const [workbenchState, setWorkbenchState] = useState<'loading' | 'ready' | 'error'>('loading')
  const [workbenchError, setWorkbenchError] = useState<string | null>(null)
  const [subView, setSubView] = useState<ManageSubView>('view')
  const [editorOpen, setEditorOpen] = useState(false)

  const [versionMode, setVersionMode] = useState<UploadMode>('file')
  const [versionFile, setVersionFile] = useState<File | null>(null)
  const [versionText, setVersionText] = useState('')
  const [versionNote, setVersionNote] = useState('')
  const [savingVersion, setSavingVersion] = useState(false)

  const [shareWith, setShareWith] = useState('')
  const [shareAllowDownload, setShareAllowDownload] = useState(false)
  const [sharing, setSharing] = useState(false)

  const [approvalReason, setApprovalReason] = useState('')
  const [approvalRisk, setApprovalRisk] = useState('medium')
  const [requestingApproval, setRequestingApproval] = useState(false)

  const [deleting, setDeleting] = useState(false)

  const load = useCallback(() => {
    let live = true
    setState('loading')

    listArtifacts(session.isAdmin)
      .then((result) => {
        if (!live) return
        setArtifacts(result.artifacts ?? [])
        setError(null)
        setState('ready')
      })
      .catch((cause: unknown) => {
        if (!live) return
        setError(cause instanceof Error ? cause.message : 'Could not load files.')
        setState('error')
      })

    return () => {
      live = false
    }
  }, [session.isAdmin])

  useEffect(() => load(), [load])

  const typeOptions = useMemo<DropdownOption[]>(() => {
    const seen = new Set<string>()
    for (const a of artifacts) seen.add(a.type)
    return [
      { value: 'all', label: 'All types' },
      ...[...seen].sort().map((t) => ({ value: t, label: t.replace(/_/g, ' ') })),
    ]
  }, [artifacts])

  const shown = useMemo(() => {
    const q = search.trim().toLowerCase()
    return artifacts.filter((a) => {
      if (typeFilter !== 'all' && a.type !== typeFilter) return false
      if (q && !a.title.toLowerCase().includes(q) && !a.filename.toLowerCase().includes(q)) return false
      return true
    })
  }, [artifacts, search, typeFilter])

  // ── upload ───────────────────────────────────────────────────────────────

  const closeUpload = useCallback(() => {
    setUploadOpen(false)
    setUploadMode('file')
    setUploadTitle('')
    setUploadClassification('INTERNAL')
    setUploadFile(null)
    setUploadText('')
  }, [])

  const submitUpload = useCallback(async () => {
    if (uploadMode === 'file' && !uploadFile) {
      toast.warn('Choose a file first')
      return
    }
    if (uploadMode === 'text' && !uploadText.trim()) {
      toast.warn('Paste some text first')
      return
    }
    if (uploadFile && uploadFile.size > MAX_UPLOAD_BYTES) {
      toast.warn('That file is over the 25MB limit')
      return
    }
    setUploading(true)
    try {
      const base = {
        title: uploadTitle.trim() || undefined,
        classification: [uploadClassification],
      }
      const created = uploadFile
        ? await createArtifact({
            ...base,
            filename: uploadFile.name,
            ...(await fileToBase64(uploadFile)),
          })
        : await createArtifact({ ...base, text: uploadText })
      toast.success('File uploaded')
      closeUpload()
      load()
      openManage(created)
    } catch (cause) {
      toast.error(cause instanceof ApiError ? cause.message : 'Could not upload that file.')
    } finally {
      setUploading(false)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [uploadMode, uploadFile, uploadText, uploadTitle, uploadClassification, toast, closeUpload, load])

  // ── manage drawer ────────────────────────────────────────────────────────

  const closeManage = useCallback(() => {
    setManageId(null)
    setSubView('view')
    setEditorOpen(false)
  }, [])

  const loadWorkbench = useCallback((id: string) => {
    setWorkbenchState('loading')
    getArtifactWorkbench(id)
      .then((result) => {
        setWorkbench(result)
        setWorkbenchError(null)
        setWorkbenchState('ready')
      })
      .catch((cause: unknown) => {
        setWorkbenchError(cause instanceof Error ? cause.message : 'Could not load this file.')
        setWorkbenchState('error')
      })
  }, [])

  function openManage(artifact: Artifact) {
    setManageId(artifact.artifactId)
    setWorkbench(null)
    setSubView('view')
    setEditorOpen(false)
    loadWorkbench(artifact.artifactId)
  }

  const openVersion = useCallback(() => {
    setVersionMode('file')
    setVersionFile(null)
    setVersionText('')
    setVersionNote('')
    setSubView('version')
  }, [])

  const submitVersion = useCallback(async () => {
    if (!manageId) return
    if (versionMode === 'file' && !versionFile) {
      toast.warn('Choose a file first')
      return
    }
    if (versionMode === 'text' && !versionText.trim()) {
      toast.warn('Paste some text first')
      return
    }
    if (versionFile && versionFile.size > MAX_UPLOAD_BYTES) {
      toast.warn('That file is over the 25MB limit')
      return
    }
    setSavingVersion(true)
    try {
      const input = versionFile
        ? { filename: versionFile.name, note: versionNote.trim(), ...(await fileToBase64(versionFile)) }
        : { text: versionText, note: versionNote.trim() }
      await addArtifactVersion(manageId, input)
      toast.success('New version added')
      setSubView('view')
      loadWorkbench(manageId)
      load()
    } catch (cause) {
      toast.error(cause instanceof ApiError ? cause.message : 'Could not add that version.')
    } finally {
      setSavingVersion(false)
    }
  }, [manageId, versionMode, versionFile, versionText, versionNote, toast, loadWorkbench, load])

  const openShare = useCallback(() => {
    setShareWith('')
    setShareAllowDownload(false)
    setSubView('share')
  }, [])

  const submitShare = useCallback(async () => {
    if (!manageId) return
    const who = shareWith.trim()
    if (!who) {
      toast.warn('Enter who to share with')
      return
    }
    setSharing(true)
    try {
      await createArtifactShare(manageId, {
        shared_with: who,
        permissions: shareAllowDownload ? ['view', 'download'] : ['view'],
      })
      toast.success(`Shared with ${who}`)
      setSubView('view')
      loadWorkbench(manageId)
    } catch (cause) {
      toast.error(cause instanceof ApiError ? cause.message : 'Could not share that file.')
    } finally {
      setSharing(false)
    }
  }, [manageId, shareWith, shareAllowDownload, toast, loadWorkbench])

  const revokeShare = useCallback(
    async (share: ArtifactShare) => {
      if (!manageId) return
      try {
        await revokeArtifactShare(manageId, share.shareId)
        toast.success(`Revoked ${share.sharedWith}'s access`)
        loadWorkbench(manageId)
      } catch (cause) {
        toast.error(cause instanceof ApiError ? cause.message : 'Could not revoke that share.')
      }
    },
    [manageId, toast, loadWorkbench],
  )

  const openApproval = useCallback(() => {
    setApprovalReason('')
    setApprovalRisk('medium')
    setSubView('approval')
  }, [])

  const submitApproval = useCallback(async () => {
    if (!manageId) return
    if (!approvalReason.trim()) {
      toast.warn('Say why this needs approval')
      return
    }
    setRequestingApproval(true)
    try {
      await requestArtifactApproval(manageId, approvalReason.trim(), approvalRisk)
      toast.success('Approval requested')
      setSubView('view')
      loadWorkbench(manageId)
    } catch (cause) {
      toast.error(cause instanceof ApiError ? cause.message : 'Could not request approval.')
    } finally {
      setRequestingApproval(false)
    }
  }, [manageId, approvalReason, approvalRisk, toast, loadWorkbench])

  const confirmDelete = useCallback(async () => {
    if (!manageId) return
    setDeleting(true)
    try {
      await deleteArtifact(manageId)
      toast.success('File deleted')
      closeManage()
      load()
    } catch (cause) {
      toast.error(cause instanceof ApiError ? cause.message : 'Could not delete that file.')
    } finally {
      setDeleting(false)
    }
  }, [manageId, toast, closeManage, load])

  const versionColumns = useMemo<Column<ArtifactVersionRecord>[]>(
    () => [
      { key: 'version', header: 'Version', width: '5rem', render: (v) => `v${v.versionNumber}` },
      { key: 'size', header: 'Size', width: '6rem', muted: true, render: (v) => formatBytes(v.sizeBytes) },
      { key: 'by', header: 'By', width: '9rem', render: (v) => v.createdBy },
      {
        key: 'when',
        header: 'When',
        width: '9rem',
        muted: true,
        nowrap: true,
        render: (v) => <span title={formatWhen(v.createdAt)}>{formatRelative(v.createdAt)}</span>,
      },
      { key: 'note', header: 'Note', muted: true, render: (v) => v.note || '—' },
      {
        key: 'actions',
        header: '',
        srHeader: 'Download',
        width: '6rem',
        align: 'right',
        render: (v) => <DownloadLink href={v.downloadUrl} />,
      },
    ],
    [],
  )

  const shareColumns = useMemo<Column<ArtifactShare>[]>(
    () => [
      { key: 'who', header: 'Shared with', render: (s) => s.sharedWith },
      {
        key: 'permissions',
        header: 'Can',
        render: (s) => s.permissions.join(', '),
      },
      {
        key: 'status',
        header: 'Status',
        width: '7rem',
        render: (s) => (
          <Badge tone={statusTone(s.status)} dot>
            {s.status}
          </Badge>
        ),
      },
      {
        key: 'actions',
        header: '',
        srHeader: 'Revoke',
        width: '6rem',
        align: 'right',
        render: (s) =>
          s.status === 'active' ? (
            <Button size="sm" variant="ghost" onClick={() => revokeShare(s)}>
              Revoke
            </Button>
          ) : (
            '—'
          ),
      },
    ],
    [revokeShare],
  )

  const artifact = workbench?.artifact

  return (
    <PageShell className="files">
      <Card
        title="Files"
        description="Generated artifacts, classifications, and downloads."
        actions={
          <div className="files-toolbar">
            <Field label="Search" inline>
              {(props) => (
                <Input
                  {...props}
                  placeholder="Title or filename…"
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                  className="files-search"
                />
              )}
            </Field>
            <Field label="Type" inline>
              {(props) => <Dropdown {...props} value={typeFilter} onChange={setTypeFilter} options={typeOptions} />}
            </Field>
            <Button variant="quiet" size="sm" onClick={load} loading={state === 'loading'}>
              Refresh
            </Button>
            <AddButton onClick={() => setUploadOpen(true)} label="Upload a file" />
          </div>
        }
      >
        {state === 'error' ? (
          <EmptyState
            title="Couldn't load your files"
            description={error ?? undefined}
            action={
              <Button size="sm" variant="ghost" onClick={load}>
                Try again
              </Button>
            }
          />
        ) : state === 'loading' ? (
          <div className="files-grid-wrap">
            <div className="files-grid">
              {Array.from({ length: 8 }, (_, i) => (
                <Skeleton key={i} index={i} height="9.5rem" radius="var(--ui-r-md)" />
              ))}
            </div>
          </div>
        ) : shown.length === 0 ? (
          <EmptyState
            title={artifacts.length === 0 ? 'No files yet' : 'No files match'}
            description={
              artifacts.length === 0
                ? 'Upload your first one with the + above.'
                : 'Try a different search or type filter.'
            }
            action={
              artifacts.length === 0 ? (
                <Button size="sm" onClick={() => setUploadOpen(true)}>
                  Upload a file
                </Button>
              ) : (
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => {
                    setSearch('')
                    setTypeFilter('all')
                  }}
                >
                  Clear filters
                </Button>
              )
            }
          />
        ) : (
          <div className="files-grid-wrap">
            <div className="files-grid">
              {shown.map((a, i) => (
                <FileCard key={a.artifactId} artifact={a} index={i} onOpen={() => openManage(a)} />
              ))}
            </div>
          </div>
        )}
      </Card>

      <Drawer
        open={manageId != null}
        onClose={closeManage}
        eyebrow="File"
        title={artifact?.title || manageId || ''}
        meta={
          artifact && (
            <>
              <Badge tone={statusTone(artifact.status)} dot>
                {artifact.status}
              </Badge>
              {artifact.classification.map((c) => (
                <Badge key={c} subtle>
                  {c}
                </Badge>
              ))}
              {artifact.shared && (
                <Badge tone="info" subtle>
                  shared with you
                </Badge>
              )}
            </>
          )
        }
        footer={
          subView === 'delete' ? (
            <>
              <Button variant="ghost" onClick={() => setSubView('view')} disabled={deleting}>
                Cancel
              </Button>
              <Button variant="danger" onClick={confirmDelete} loading={deleting}>
                Delete file
              </Button>
            </>
          ) : subView === 'version' ? (
            <>
              <Button variant="ghost" onClick={() => setSubView('view')} disabled={savingVersion}>
                Cancel
              </Button>
              <Button onClick={submitVersion} loading={savingVersion}>
                Save new version
              </Button>
            </>
          ) : subView === 'share' ? (
            <>
              <Button variant="ghost" onClick={() => setSubView('view')} disabled={sharing}>
                Cancel
              </Button>
              <Button onClick={submitShare} loading={sharing}>
                Share
              </Button>
            </>
          ) : subView === 'approval' ? (
            <>
              <Button variant="ghost" onClick={() => setSubView('view')} disabled={requestingApproval}>
                Cancel
              </Button>
              <Button onClick={submitApproval} loading={requestingApproval}>
                Request approval
              </Button>
            </>
          ) : artifact ? (
            <>
              <Button variant="danger" size="sm" onClick={() => setSubView('delete')}>
                Delete
              </Button>
              <Button variant="ghost" size="sm" onClick={openApproval}>
                Request approval
              </Button>
              <Button variant="ghost" size="sm" onClick={openShare}>
                Share
              </Button>
              <Button size="sm" onClick={openVersion}>
                New version
              </Button>
              <DownloadLink href={artifact.downloadUrl} variant="primary" />
            </>
          ) : undefined
        }
      >
        {workbenchState === 'loading' ? (
          <Skeleton height="16rem" />
        ) : workbenchState === 'error' ? (
          <p className="files-note">{workbenchError}</p>
        ) : !workbench || !artifact ? null : subView === 'delete' ? (
          <p className="files-confirm-msg">
            Delete "{artifact.title}"? This removes every version and can't be undone.
          </p>
        ) : subView === 'version' ? (
          <div className="files-drawer-body">
            <SegmentedControl
              label="Version source"
              segments={
                [
                  { value: 'file', label: 'Upload a file' },
                  { value: 'text', label: 'Paste text' },
                ] as Segment<UploadMode>[]
              }
              value={versionMode}
              onChange={setVersionMode}
            />
            {versionMode === 'file' ? (
              <Field label="Replacement file" hint="Up to 25MB.">
                {(props) => (
                  <input
                    {...props}
                    type="file"
                    className="ui-input files-file-input"
                    onChange={(e) => setVersionFile(e.target.files?.[0] ?? null)}
                  />
                )}
              </Field>
            ) : (
              <Field label="Text content">
                {(props) => <Textarea {...props} rows={8} value={versionText} onChange={(e) => setVersionText(e.target.value)} />}
              </Field>
            )}
            <Field label="What changed" hint="Optional note for this version.">
              {(props) => <Input {...props} value={versionNote} onChange={(e) => setVersionNote(e.target.value)} />}
            </Field>
          </div>
        ) : subView === 'share' ? (
          <div className="files-drawer-body">
            <Field label="Share with" hint="Username of the person to share this file with.">
              {(props) => <Input {...props} placeholder="jane" value={shareWith} onChange={(e) => setShareWith(e.target.value)} />}
            </Field>
            <Switch
              checked={shareAllowDownload}
              onChange={setShareAllowDownload}
              label="Allow download"
              description="Without this, they can view its details but not pull the file itself."
            />
          </div>
        ) : subView === 'approval' ? (
          <div className="files-drawer-body">
            <Field label="Reason" hint="Why does this file need an admin decision?">
              {(props) => (
                <Textarea {...props} mono={false} rows={4} value={approvalReason} onChange={(e) => setApprovalReason(e.target.value)} />
              )}
            </Field>
            <Field label="Risk level">
              {(props) => <Dropdown {...props} value={approvalRisk} onChange={setApprovalRisk} options={RISK_OPTIONS} />}
            </Field>
          </div>
        ) : (
          <div className="files-drawer-body">
            <div className="files-section-block">
              <p className="ui-eyebrow files-section-label">Preview</p>
              <div className="files-preview-head">
                <FileTypeIcon type={artifact.type} size={34} />
                <span className="ui-mono files-preview-filename">{workbench.preview.filename}</span>
              </div>
              {workbench.preview.error ? (
                <p className="files-note">{workbench.preview.error}</p>
              ) : workbench.preview.summary.length === 0 ? (
                <p className="files-note">No preview available.</p>
              ) : (
                <ul className="files-summary-list">
                  {workbench.preview.summary.map((line, i) => (
                    <li key={i}>{line}</li>
                  ))}
                </ul>
              )}
              {Object.keys(workbench.preview.signals).length > 0 && (
                <KeyValue
                  columns={2}
                  items={Object.entries(workbench.preview.signals).map(([k, v]) => ({
                    label: humanizeKey(k),
                    value: formatSignalValue(v),
                  }))}
                />
              )}
            </div>

            <div className="files-section-block">
              <p className="ui-eyebrow files-section-label">Document editor</p>
              {workbench.onlyoffice?.enabled ? (
                <>
                  <p className="files-note">
                    Powered by ONLYOFFICE —{' '}
                    {editorMode(workbench.onlyoffice.config) === 'edit'
                      ? 'edits made here save as a new version automatically.'
                      : 'opens as a read-only preview.'}
                  </p>
                  <Button size="sm" onClick={() => setEditorOpen(true)}>
                    {editorMode(workbench.onlyoffice.config) === 'edit' ? 'Open document editor' : 'Preview document'}
                  </Button>
                </>
              ) : (
                <p className="files-note">No embedded preview is available for this file type.</p>
              )}
            </div>

            <div className="files-section-block">
              <p className="ui-eyebrow files-section-label">Governance</p>
              <KeyValue
                columns={2}
                items={[
                  { label: 'Owner', value: artifact.owner },
                  { label: 'Size', value: formatBytes(artifact.sizeBytes) },
                  { label: 'Checksum', value: artifact.checksum, mono: true },
                  { label: 'Version', value: `v${artifact.currentVersion} of ${artifact.versionCount}` },
                  { label: 'Created', value: formatWhen(artifact.createdAt) },
                  { label: 'Expires', value: artifact.expiresAt ? formatWhen(artifact.expiresAt) : 'Never' },
                  { label: 'Retention', value: artifact.retentionDays ? `${artifact.retentionDays} days` : '—' },
                  { label: 'Source workflow run', value: artifact.sourceWorkflowRunId, mono: true },
                ]}
              />
              {artifact.sourceToolCalls.length > 0 && (
                <div className="files-chip-row">
                  {artifact.sourceToolCalls.map((tool, i) => (
                    <Chip key={`${tool}-${i}`} size="sm">
                      {tool}
                    </Chip>
                  ))}
                </div>
              )}
            </div>

            <div className="files-section-block">
              <p className="ui-eyebrow files-section-label">Versions</p>
              <DataTable
                columns={versionColumns}
                rows={workbench.versions}
                rowKey={(v) => v.versionId}
                dense
                caption="Version history"
                empty={<EmptyState title="No versions" compact />}
              />
            </div>

            {(artifact.owner === session.name || session.isAdmin) && (
              <div className="files-section-block">
                <p className="ui-eyebrow files-section-label">Shares</p>
                <DataTable
                  columns={shareColumns}
                  rows={workbench.shares}
                  rowKey={(s) => s.shareId}
                  dense
                  caption="Shares"
                  empty={<EmptyState title="Not shared with anyone" compact />}
                />
              </div>
            )}

            <div className="files-section-block">
              <p className="ui-eyebrow files-section-label">Approvals</p>
              {workbench.approvals.length === 0 ? (
                <p className="files-note">None requested.</p>
              ) : (
                <ul className="files-approval-list">
                  {workbench.approvals.map((ap) => (
                    <li key={ap.approvalId} className="files-approval-row">
                      <Badge tone={statusTone(ap.status)} dot>
                        {ap.status}
                      </Badge>
                      <span>{ap.reason}</span>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </div>
        )}
      </Drawer>

      {/* Its own Modal, not embedded inline in the drawer above — a document
          editor needs real width to be usable, which a 27rem-wide side panel
          can't give it. Sized against the viewport, not a fixed rem value,
          so it stays a "reasonable size of viewport" on both a laptop and an
          ultrawide monitor. */}
      <Modal
        open={editorOpen}
        onClose={() => setEditorOpen(false)}
        eyebrow="Document editor"
        title={artifact?.title || ''}
        width="min(94vw, 88rem)"
      >
        {artifact && workbench?.onlyoffice?.enabled && (
          <OnlyOfficeEditor
            documentServerUrl={workbench.onlyoffice.documentServerUrl}
            config={workbench.onlyoffice.config}
            instanceKey={`${artifact.artifactId}:${artifact.currentVersion}`}
            height="min(80vh, 56rem)"
          />
        )}
      </Modal>

      <Modal
        open={uploadOpen}
        onClose={closeUpload}
        eyebrow="File"
        title="Upload a file"
        footer={
          <>
            <Button variant="ghost" onClick={closeUpload} disabled={uploading}>
              Cancel
            </Button>
            <Button onClick={submitUpload} loading={uploading}>
              Upload
            </Button>
          </>
        }
      >
        <div className="files-form">
          <Field label="Title" hint="Defaults to the filename if left blank.">
            {(props) => <Input {...props} placeholder="Q3 customer report" value={uploadTitle} onChange={(e) => setUploadTitle(e.target.value)} />}
          </Field>

          <Field label="Classification">
            {(props) => (
              <Dropdown
                {...props}
                value={uploadClassification}
                onChange={setUploadClassification}
                options={CLASSIFICATIONS.map((c) => ({ value: c, label: c }))}
              />
            )}
          </Field>

          <SegmentedControl
            label="Source"
            segments={
              [
                { value: 'file', label: 'Upload a file' },
                { value: 'text', label: 'Paste text' },
              ] as Segment<UploadMode>[]
            }
            value={uploadMode}
            onChange={setUploadMode}
          />

          {uploadMode === 'file' ? (
            <Field label="File" hint="Up to 25MB.">
              {(props) => (
                <input
                  {...props}
                  type="file"
                  className="files-file-input"
                  onChange={(e) => setUploadFile(e.target.files?.[0] ?? null)}
                />
              )}
            </Field>
          ) : (
            <Field label="Text content">
              {(props) => <Textarea {...props} rows={8} value={uploadText} onChange={(e) => setUploadText(e.target.value)} />}
            </Field>
          )}
        </div>
      </Modal>
    </PageShell>
  )
}

export default FilesPage
