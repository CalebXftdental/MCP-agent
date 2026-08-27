import { useCallback, useEffect, useRef, useState } from 'react'
import type { ChangeEvent as ReactChangeEvent } from 'react'
import {
  Badge,
  Button,
  Card,
  DataTable,
  EmptyState,
  PageShell,
  ProgressBar,
  useToast,
  type Column,
  type ProgressStep,
} from '../components/ui'
import {
  ALLOWED_UPLOAD_ACCEPT,
  ApiError,
  deleteMyDocument,
  getUploadProgress,
  isUploadableFilename,
  listMyDocuments,
  uploadMyDocument,
  type IngestProgress,
  type IngestStage,
  type MyDocument,
} from '../lib/api'
import { formatRelative, formatWhen } from '../lib/format'
import type { PageProps } from './types'
import './KnowledgePage.css'

/**
 * Knowledge — a single tier now: private per-owner PDF upload and management
 * (digest_persoanl_kb.md), backed by its own `/knowledge/mine*` routes and its
 * own `personal_knowledge` category grant. Never visible to anyone but the
 * uploader — no admin bypass.
 *
 * The former "Ask your documents" card (search/answer over the shared
 * company-wide knowledge base) and "Indexed documents" list were dropped:
 * asking questions over indexed knowledge is already covered by the home
 * chatbot (search_knowledge/answer_from_knowledge, and the personal-tier
 * equivalents, are exposed there as MCP tools), so a duplicate Q&A surface
 * here was redundant. This page's job is upload + manage, not ask.
 */

// Order and labels mirror governance_core/ingest_progress.py's STAGE_ORDER —
// what personal_knowledge_store.ingest_document actually does, in order.
// Percent is a display estimate, not measured: extraction (Document
// Intelligence, PDFs especially) and embedding are the slow stages, so they
// get the widest bands rather than an even split across five steps.
const INGEST_STEPS: ProgressStep[] = [
  { key: 'uploaded', label: 'Uploaded' },
  { key: 'extracting', label: 'Extracting text' },
  { key: 'chunking', label: 'Splitting into chunks' },
  { key: 'embedding', label: 'Generating embeddings' },
  { key: 'indexing', label: 'Indexing' },
  { key: 'done', label: 'Done' },
]
const INGEST_PERCENT: Record<IngestStage, number> = {
  uploaded: 8,
  extracting: 32,
  chunking: 52,
  embedding: 78,
  indexing: 92,
  done: 100,
  error: 100,
}
const INGEST_LABEL: Record<IngestStage, string> = {
  uploaded: 'Uploaded — queued for processing…',
  extracting: 'Extracting text…',
  chunking: 'Splitting into chunks…',
  embedding: 'Generating embeddings…',
  indexing: 'Indexing…',
  done: 'Done',
  error: 'Upload failed',
}
// How often to poll while a job is in flight. Extraction/embedding are the
// slow stages (seconds, not milliseconds), so sub-second polling would only
// spend requests without the bar visibly moving any faster.
const INGEST_POLL_MS = 800

function KnowledgePage({}: PageProps) {
  const toast = useToast()

  // ── my documents (personal tier) ─────────────────────────────────────────
  const [myDocs, setMyDocs] = useState<MyDocument[]>([])
  const [myDocsState, setMyDocsState] = useState<'loading' | 'ready' | 'error' | 'unavailable'>('loading')
  const [myDocsError, setMyDocsError] = useState<string | null>(null)
  const [uploading, setUploading] = useState(false)
  const [uploadProgress, setUploadProgress] = useState<IngestProgress | null>(null)
  const [pendingDeleteId, setPendingDeleteId] = useState<string | null>(null)
  const [deletingMine, setDeletingMine] = useState(false)
  const fileInputRef = useRef<HTMLInputElement>(null)
  const pollTimerRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const loadMine = useCallback(() => {
    let live = true
    setMyDocsState('loading')
    listMyDocuments()
      .then((result) => {
        if (!live) return
        setMyDocs(result.documents ?? [])
        setMyDocsError(null)
        setMyDocsState('ready')
      })
      .catch((cause: unknown) => {
        if (!live) return
        // A 403 here almost always just means this consumer hasn't been
        // granted the `personal_knowledge` category yet, not a real error —
        // show a plain "not available" state instead of an error banner.
        if (cause instanceof ApiError && cause.isForbidden) {
          setMyDocsState('unavailable')
          return
        }
        setMyDocsError(cause instanceof Error ? cause.message : 'Could not load your documents.')
        setMyDocsState('error')
      })
    return () => {
      live = false
    }
  }, [])

  useEffect(() => loadMine(), [loadMine])

  const onPickFile = () => fileInputRef.current?.click()

  const mountedRef = useRef(true)
  useEffect(
    () => () => {
      mountedRef.current = false
      if (pollTimerRef.current != null) clearInterval(pollTimerRef.current)
    },
    [],
  )

  const stopPolling = useCallback(() => {
    if (pollTimerRef.current != null) {
      clearInterval(pollTimerRef.current)
      pollTimerRef.current = null
    }
  }, [])

  /** Polls the ingest job every INGEST_POLL_MS until it reaches a terminal
   *  stage (done/error) — see governance_core/ingest_progress.py. */
  const pollUploadProgress = useCallback(
    (jobId: string, filename: string) => {
      stopPolling()
      pollTimerRef.current = setInterval(async () => {
        let result: IngestProgress
        try {
          result = await getUploadProgress(jobId)
        } catch (cause) {
          stopPolling()
          if (!mountedRef.current) return
          setUploading(false)
          setUploadProgress(null)
          toast.error(
            cause instanceof ApiError ? cause.message : `Lost track of "${filename}" — check your documents list.`,
          )
          return
        }
        if (!mountedRef.current) return
        setUploadProgress(result)
        if (result.stage !== 'done' && result.stage !== 'error') return

        stopPolling()
        setUploading(false)
        if (result.stage === 'done') {
          toast.success(`Uploaded "${filename}"`)
          loadMine()
        } else {
          toast.error(result.message || `Could not upload "${filename}".`)
        }
        // Leave the finished/failed bar visible briefly rather than snapping
        // it away the instant the last stage lands.
        setTimeout(() => {
          if (mountedRef.current) setUploadProgress(null)
        }, 1600)
      }, INGEST_POLL_MS)
    },
    [stopPolling, toast, loadMine],
  )

  const onFileSelected = useCallback(
    async (e: ReactChangeEvent<HTMLInputElement>) => {
      const file = e.target.files?.[0]
      e.target.value = '' // allow re-selecting the same file next time
      if (!file) return
      if (!isUploadableFilename(file.name)) {
        toast.error(`"${file.name}" isn't a PDF — only .pdf uploads are supported right now.`)
        return
      }
      setUploading(true)
      setUploadProgress({ jobId: '', stage: 'uploaded', message: '', documentId: '', filename: file.name })
      try {
        const { jobId } = await uploadMyDocument(file)
        pollUploadProgress(jobId, file.name)
      } catch (cause) {
        setUploading(false)
        setUploadProgress(null)
        toast.error(cause instanceof ApiError ? cause.message : 'Could not upload that file.')
      }
    },
    [toast, pollUploadProgress],
  )

  const confirmDeleteMine = useCallback(async () => {
    if (!pendingDeleteId) return
    setDeletingMine(true)
    try {
      await deleteMyDocument(pendingDeleteId)
      toast.success('Document deleted')
      setPendingDeleteId(null)
      loadMine()
    } catch (cause) {
      toast.error(cause instanceof ApiError ? cause.message : 'Could not delete that document.')
    } finally {
      setDeletingMine(false)
    }
  }, [pendingDeleteId, toast, loadMine])

  const myDocColumns: Column<MyDocument>[] = [
    {
      key: 'title',
      header: 'Title',
      render: (d) => (
        <div className="knowledge-doc-cell">
          <p className="knowledge-doc-title">{d.title || d.filename}</p>
          <p className="knowledge-doc-filename ui-mono">{d.filename}</p>
        </div>
      ),
    },
    { key: 'type', header: 'Type', width: '7rem', render: (d) => <Badge subtle>{d.sourceType}</Badge> },
    { key: 'chunks', header: 'Chunks', width: '5.5rem', numeric: true, render: (d) => d.chunkCount },
    {
      key: 'created',
      header: 'Uploaded',
      width: '8.5rem',
      muted: true,
      nowrap: true,
      render: (d) => <span title={formatWhen(d.createdAt)}>{formatRelative(d.createdAt)}</span>,
    },
    {
      key: 'actions',
      header: '',
      width: '9rem',
      render: (d) =>
        pendingDeleteId === d.documentId ? (
          <div className="knowledge-inline-confirm">
            <Button variant="danger" size="sm" onClick={confirmDeleteMine} loading={deletingMine}>
              Confirm
            </Button>
            <Button variant="ghost" size="sm" onClick={() => setPendingDeleteId(null)} disabled={deletingMine}>
              Cancel
            </Button>
          </div>
        ) : (
          <Button variant="ghost" size="sm" onClick={() => setPendingDeleteId(d.documentId)}>
            Delete
          </Button>
        ),
    },
  ]

  return (
    <PageShell className="knowledge">
      <Card
        title="My documents"
        description="Private to you — not visible to anyone else, including admins. PDF only for now — more file types are coming. Ask about these from chat."
        actions={
          myDocsState === 'unavailable' ? undefined : (
            <>
              <input
                ref={fileInputRef}
                type="file"
                onChange={onFileSelected}
                style={{ display: 'none' }}
                accept={ALLOWED_UPLOAD_ACCEPT}
              />
              <Button size="sm" onClick={onPickFile} loading={uploading} disabled={uploadProgress != null && !uploading}>
                Upload document
              </Button>
            </>
          )
        }
      >
        {uploadProgress && (
          <div className="knowledge-upload-progress">
            <ProgressBar
              percent={INGEST_PERCENT[uploadProgress.stage]}
              tone={uploadProgress.stage === 'error' ? 'danger' : 'accent'}
              label={
                uploadProgress.stage === 'error' && uploadProgress.message
                  ? uploadProgress.message
                  : INGEST_LABEL[uploadProgress.stage]
              }
              sublabel={uploadProgress.filename}
              steps={uploadProgress.stage === 'error' ? undefined : INGEST_STEPS}
              activeIndex={INGEST_STEPS.findIndex((s) => s.key === uploadProgress.stage)}
            />
          </div>
        )}
        {myDocsState === 'unavailable' ? (
          <EmptyState
            title="Personal document upload isn't enabled for your account yet"
            description="Ask an admin to grant the Personal Knowledge category, then refresh this page."
            compact
          />
        ) : myDocsState === 'error' ? (
          <EmptyState
            title="Couldn't load your documents"
            description={myDocsError ?? undefined}
            action={
              <Button size="sm" variant="ghost" onClick={loadMine}>
                Try again
              </Button>
            }
          />
        ) : (
          <DataTable
            columns={myDocColumns}
            rows={myDocs}
            rowKey={(d) => d.documentId}
            loading={myDocsState === 'loading'}
            caption="My documents"
            maxHeight="min(60vh, 34rem)"
            empty={
              <EmptyState
                title="No documents uploaded yet"
                description="Upload a document to search and ask about it from chat, using search_my_documents/answer_from_my_documents."
                compact
              />
            }
          />
        )}
      </Card>
    </PageShell>
  )
}

export default KnowledgePage
