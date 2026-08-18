import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { ChangeEvent as ReactChangeEvent, KeyboardEvent as ReactKeyboardEvent } from 'react'
import {
  Badge,
  Button,
  Card,
  Chip,
  DataTable,
  Drawer,
  Dropdown,
  EmptyState,
  Field,
  Input,
  KeyValue,
  Skeleton,
  Switch,
  TypingDots,
  useToast,
  type Column,
  type DropdownOption,
} from '../components/ui'
import {
  ApiError,
  answerFromKnowledge,
  deleteKnowledgeDocument,
  deleteMyDocument,
  getKnowledgeDocument,
  listKnowledgeDocuments,
  listMyDocuments,
  searchKnowledge,
  uploadMyDocument,
  type KnowledgeAnswer,
  type KnowledgeDocument,
  type KnowledgeSearchHit,
  type MyDocument,
} from '../lib/api'
import { formatRelative, formatWhen } from '../lib/format'
import type { PageProps } from './types'
import './KnowledgePage.css'

/**
 * Knowledge — ports `renderKnowledge()`: search and citation-backed answers
 * over the shared knowledge base, plus a real personal-document tier.
 *
 * The legacy panel's "Ingest text" / "Ingest file" forms called
 * `POST /knowledge/documents`, a route that was never mounted —
 * `backend/knowledge.py` is read-only BY DESIGN for the shared company tier
 * (new company documents arrive through AraTestEnvBE's own ingestion pipeline,
 * never through this gateway). Those buttons 405'd since before this port
 * existed and were dropped rather than carried forward.
 *
 * "My documents" below is a genuinely different, separate tier
 * (digest_persoanl_kb.md): private per-owner upload, backed by its own
 * `/knowledge/mine*` routes and its own `personal_knowledge` category grant —
 * not the same read-only path as "Indexed documents", and never visible to
 * anyone but the uploader (no admin bypass, unlike "All principals" below).
 *
 * What's real and ported for the company tier: Ask (answer, grounded with
 * citations, or a raw search over passages) and the indexed-document list
 * (view detail, delete). A citation or a search hit opens straight into that
 * document's detail via the same drawer the list uses, and the drawer's own
 * "Ask about this document" scopes the Ask card back to it.
 */

const LIMIT_OPTIONS: DropdownOption[] = [
  { value: '3', label: '3 results' },
  { value: '5', label: '5 results' },
  { value: '10', label: '10 results' },
]

type AskMode = 'answer' | 'search'
type ManageState = 'loading' | 'ready' | 'error'
type ManageSubView = 'view' | 'delete'

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

function KnowledgePage({ session }: PageProps) {
  const toast = useToast()
  const askSectionRef = useRef<HTMLDivElement>(null)

  // ── document list ────────────────────────────────────────────────────────
  const [documents, setDocuments] = useState<KnowledgeDocument[]>([])
  const [docsState, setDocsState] = useState<'loading' | 'ready' | 'error'>('loading')
  const [docsError, setDocsError] = useState<string | null>(null)
  const [docFilter, setDocFilter] = useState('')
  const [showAll, setShowAll] = useState(false)

  // ── ask panel ─────────────────────────────────────────────────────────────
  const [question, setQuestion] = useState('')
  const [scopeId, setScopeId] = useState('')
  const [limit, setLimit] = useState(5)
  const [asking, setAsking] = useState<AskMode | null>(null)
  const [askMode, setAskMode] = useState<AskMode | null>(null)
  const [askError, setAskError] = useState<string | null>(null)
  const [answer, setAnswer] = useState<KnowledgeAnswer | null>(null)
  const [hits, setHits] = useState<KnowledgeSearchHit[] | null>(null)

  // ── my documents (personal tier) ─────────────────────────────────────────
  const [myDocs, setMyDocs] = useState<MyDocument[]>([])
  const [myDocsState, setMyDocsState] = useState<'loading' | 'ready' | 'error' | 'unavailable'>('loading')
  const [myDocsError, setMyDocsError] = useState<string | null>(null)
  const [uploading, setUploading] = useState(false)
  const [pendingDeleteId, setPendingDeleteId] = useState<string | null>(null)
  const [deletingMine, setDeletingMine] = useState(false)
  const fileInputRef = useRef<HTMLInputElement>(null)

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

  const onFileSelected = useCallback(
    async (e: ReactChangeEvent<HTMLInputElement>) => {
      const file = e.target.files?.[0]
      e.target.value = '' // allow re-selecting the same file next time
      if (!file) return
      setUploading(true)
      try {
        await uploadMyDocument(file)
        toast.success(`Uploaded "${file.name}"`)
        loadMine()
      } catch (cause) {
        toast.error(cause instanceof ApiError ? cause.message : 'Could not upload that file.')
      } finally {
        setUploading(false)
      }
    },
    [toast, loadMine],
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

  const myDocColumns = useMemo<Column<MyDocument>[]>(
    () => [
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
    ],
    [pendingDeleteId, deletingMine, confirmDeleteMine],
  )

  // ── manage drawer ─────────────────────────────────────────────────────────
  const [manageId, setManageId] = useState<string | null>(null)
  const [manageDoc, setManageDoc] = useState<KnowledgeDocument | null>(null)
  const [manageState, setManageState] = useState<ManageState>('ready')
  const [manageError, setManageError] = useState<string | null>(null)
  const [subView, setSubView] = useState<ManageSubView>('view')
  const [deleting, setDeleting] = useState(false)

  const load = useCallback(() => {
    let live = true
    setDocsState('loading')

    listKnowledgeDocuments(session.isAdmin && showAll)
      .then((result) => {
        if (!live) return
        setDocuments(result.documents ?? [])
        setDocsError(null)
        setDocsState('ready')
      })
      .catch((cause: unknown) => {
        if (!live) return
        setDocsError(cause instanceof Error ? cause.message : 'Could not load documents.')
        setDocsState('error')
      })

    return () => {
      live = false
    }
  }, [session.isAdmin, showAll])

  useEffect(() => load(), [load])

  const scopeOptions = useMemo<DropdownOption[]>(
    () => [
      { value: '', label: 'All documents' },
      ...documents.map((d) => ({ value: d.documentId, label: d.title || d.filename })),
    ],
    [documents],
  )

  const shownDocs = useMemo(() => {
    const q = docFilter.trim().toLowerCase()
    if (!q) return documents
    return documents.filter((d) => d.title.toLowerCase().includes(q) || d.filename.toLowerCase().includes(q))
  }, [documents, docFilter])

  // ── manage drawer ─────────────────────────────────────────────────────────

  const closeManage = useCallback(() => {
    setManageId(null)
    setManageDoc(null)
    setSubView('view')
  }, [])

  function openManage(doc: KnowledgeDocument) {
    setManageId(doc.documentId)
    setManageDoc(doc)
    setManageState('ready')
    setManageError(null)
    setSubView('view')
  }

  const openManageById = useCallback(
    (documentId: string) => {
      const existing = documents.find((d) => d.documentId === documentId)
      if (existing) {
        openManage(existing)
        return
      }
      // A citation or search hit can point at a document this owner's default
      // list view doesn't currently include (e.g. filtered by the search text
      // above, or — for an admin with "All principals" off — owned by someone
      // else). Fetched on demand rather than forcing every citation click to
      // first flip the toggle and hunt for the row.
      setManageId(documentId)
      setManageDoc(null)
      setManageError(null)
      setSubView('view')
      setManageState('loading')
      getKnowledgeDocument(documentId)
        .then((result) => {
          setManageDoc(result.document)
          setManageState('ready')
        })
        .catch((cause: unknown) => {
          setManageError(cause instanceof ApiError ? cause.message : 'Could not load that document.')
          setManageState('error')
        })
    },
    [documents],
  )

  const confirmDelete = useCallback(async () => {
    if (!manageId) return
    setDeleting(true)
    try {
      await deleteKnowledgeDocument(manageId)
      toast.success('Document deleted')
      closeManage()
      load()
    } catch (cause) {
      toast.error(cause instanceof ApiError ? cause.message : 'Could not delete that document.')
    } finally {
      setDeleting(false)
    }
  }, [manageId, toast, closeManage, load])

  const askAboutDoc = useCallback((doc: KnowledgeDocument) => {
    setScopeId(doc.documentId)
    setManageId(null)
    setManageDoc(null)
    setSubView('view')
    requestAnimationFrame(() => {
      askSectionRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' })
    })
  }, [])

  // ── ask ───────────────────────────────────────────────────────────────────

  const runAnswer = useCallback(async () => {
    const q = question.trim()
    if (!q) {
      toast.warn('Ask a question first')
      return
    }
    setAsking('answer')
    setAskError(null)
    try {
      const result = await answerFromKnowledge(q, limit, scopeId)
      setAnswer(result)
      setHits(null)
      setAskMode('answer')
    } catch (cause) {
      setAskError(cause instanceof ApiError ? cause.message : 'Could not get an answer.')
      setAnswer(null)
      setHits(null)
      setAskMode(null)
    } finally {
      setAsking(null)
    }
  }, [question, limit, scopeId, toast])

  const runSearch = useCallback(async () => {
    const q = question.trim()
    if (!q) {
      toast.warn('Ask a question first')
      return
    }
    setAsking('search')
    setAskError(null)
    try {
      const result = await searchKnowledge(q, limit, scopeId)
      setHits(result)
      setAnswer(null)
      setAskMode('search')
    } catch (cause) {
      setAskError(cause instanceof ApiError ? cause.message : 'Could not search your documents.')
      setAnswer(null)
      setHits(null)
      setAskMode(null)
    } finally {
      setAsking(null)
    }
  }, [question, limit, scopeId, toast])

  const onQuestionKeyDown = (e: ReactKeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter' && asking == null) {
      e.preventDefault()
      runAnswer()
    }
  }

  const docColumns = useMemo<Column<KnowledgeDocument>[]>(
    () => [
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
      {
        key: 'type',
        header: 'Type',
        width: '7rem',
        render: (d) => <Badge subtle>{d.sourceType}</Badge>,
      },
      { key: 'chunks', header: 'Chunks', width: '5.5rem', numeric: true, render: (d) => d.chunkCount },
      {
        key: 'classification',
        header: 'Classification',
        width: '11rem',
        render: (d) => (
          <div className="knowledge-pills">
            {d.classification.map((c) => (
              <Badge key={c} subtle>
                {c}
              </Badge>
            ))}
          </div>
        ),
      },
      {
        key: 'created',
        header: 'Created',
        width: '8.5rem',
        muted: true,
        nowrap: true,
        render: (d) => <span title={formatWhen(d.createdAt)}>{formatRelative(d.createdAt)}</span>,
      },
    ],
    [],
  )

  const hitColumns = useMemo<Column<KnowledgeSearchHit>[]>(
    () => [
      {
        key: 'source',
        header: 'Source',
        width: '13rem',
        render: (h) => (
          <div className="knowledge-doc-cell">
            <p className="knowledge-doc-title">{h.documentTitle || h.documentId}</p>
            <p className="knowledge-doc-filename ui-mono">#{h.chunkId}</p>
          </div>
        ),
      },
      { key: 'score', header: 'Score', width: '5rem', numeric: true, render: (h) => h.score.toFixed(3) },
      { key: 'snippet', header: 'Snippet', render: (h) => h.text },
    ],
    [],
  )

  return (
    <div className="knowledge">
      <div ref={askSectionRef}>
        <Card
          title="Ask your documents"
          description="Answers are grounded in citations from what's already indexed — nothing beyond what's on file."
        >
          <div className="knowledge-ask-form">
            <Field label="Question">
              {(props) => (
                <Input
                  {...props}
                  placeholder="What's our refund policy for enterprise customers?"
                  value={question}
                  onChange={(e) => setQuestion(e.target.value)}
                  onKeyDown={onQuestionKeyDown}
                />
              )}
            </Field>

            <div className="knowledge-ask-row">
              <Field label="Search within" inline>
                {(props) => <Dropdown {...props} value={scopeId} onChange={setScopeId} options={scopeOptions} />}
              </Field>
              <Field label="Results" inline>
                {(props) => (
                  <Dropdown
                    {...props}
                    value={String(limit)}
                    onChange={(v) => setLimit(Number(v))}
                    options={LIMIT_OPTIONS}
                  />
                )}
              </Field>
              <div className="knowledge-ask-actions">
                <Button
                  variant="ghost"
                  onClick={runSearch}
                  loading={asking === 'search'}
                  disabled={asking != null && asking !== 'search'}
                >
                  Search only
                </Button>
                <Button onClick={runAnswer} loading={asking === 'answer'} disabled={asking != null && asking !== 'answer'}>
                  Get answer
                </Button>
              </div>
            </div>
          </div>

          <div className="knowledge-ask-results">
            {asking ? (
              <div className="knowledge-thinking">
                <TypingDots label={asking === 'answer' ? 'Finding an answer' : 'Searching'} />
                <span>{asking === 'answer' ? 'Reading your documents…' : 'Searching your documents…'}</span>
              </div>
            ) : askError ? (
              <EmptyState title="Couldn't get an answer" description={askError} compact />
            ) : askMode === 'answer' && answer ? (
              <div className="knowledge-answer">
                <p className="knowledge-answer-text">{answer.answer}</p>
                {answer.citations.length > 0 && (
                  <div className="knowledge-citations">
                    {answer.citations.map((c, i) => (
                      <Chip
                        key={`${c.documentId}-${c.chunkId}-${i}`}
                        size="sm"
                        title={`Score ${c.score.toFixed(2)}`}
                        onClick={() => openManageById(c.documentId)}
                      >
                        {c.documentTitle || c.documentId}
                      </Chip>
                    ))}
                  </div>
                )}
              </div>
            ) : askMode === 'search' && hits ? (
              hits.length === 0 ? (
                <EmptyState
                  title="No matches"
                  description="Try a different question, or clear the document scope above."
                  compact
                />
              ) : (
                <DataTable
                  columns={hitColumns}
                  rows={hits}
                  rowKey={(h, i) => `${h.chunkId}-${i}`}
                  dense
                  caption="Search results"
                  onRowClick={(h) => openManageById(h.documentId)}
                />
              )
            ) : (
              <EmptyState
                title="Ask anything about your indexed documents"
                description={
                  documents.length === 0
                    ? 'Nothing is indexed yet — check back once your organization\'s ingestion pipeline has added documents.'
                    : '"Get answer" gives a grounded summary with citations; "Search only" shows the raw matching passages.'
                }
                compact
              />
            )}
          </div>
        </Card>
      </div>

      {myDocsState !== 'unavailable' && (
        <Card
          title="My documents"
          description="Private to you — not visible to anyone else, including admins. Upload PDFs, Word/Excel/PowerPoint, or plain text."
          actions={
            <>
              <input
                ref={fileInputRef}
                type="file"
                onChange={onFileSelected}
                style={{ display: 'none' }}
                accept=".pdf,.docx,.pptx,.xlsx,.txt,.md,.csv,.json,.log"
              />
              <Button size="sm" onClick={onPickFile} loading={uploading}>
                Upload document
              </Button>
            </>
          }
        >
          {myDocsState === 'error' ? (
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
              maxHeight="min(40vh, 22rem)"
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
      )}

      <Card
        title="Indexed documents"
        description="What's currently searchable. New documents arrive through your organization's ingestion pipeline."
        actions={
          <div className="knowledge-toolbar">
            <Field label="Search" inline>
              {(props) => (
                <Input
                  {...props}
                  placeholder="Title or filename…"
                  value={docFilter}
                  onChange={(e) => setDocFilter(e.target.value)}
                  className="knowledge-search"
                />
              )}
            </Field>
            {session.isAdmin && (
              <Switch checked={showAll} onChange={setShowAll} label="All principals" size="sm" />
            )}
            <Button variant="quiet" size="sm" onClick={load} loading={docsState === 'loading'}>
              Refresh
            </Button>
          </div>
        }
      >
        {docsState === 'error' ? (
          <EmptyState
            title="Couldn't load documents"
            description={docsError ?? undefined}
            action={
              <Button size="sm" variant="ghost" onClick={load}>
                Try again
              </Button>
            }
          />
        ) : (
          <DataTable
            columns={docColumns}
            rows={shownDocs}
            rowKey={(d) => d.documentId}
            loading={docsState === 'loading'}
            onRowClick={openManage}
            isRowActive={(d) => d.documentId === manageId}
            caption="Indexed documents"
            maxHeight="min(60vh, 34rem)"
            stickyHeader
            empty={
              <EmptyState
                title={documents.length === 0 ? 'No documents indexed yet' : 'No documents match'}
                description={
                  documents.length === 0
                    ? "Documents are added through your organization's ingestion pipeline."
                    : 'Try a different search.'
                }
                compact
              />
            }
          />
        )}
      </Card>

      <Drawer
        open={manageId != null}
        onClose={closeManage}
        eyebrow="Document"
        title={manageDoc?.title || manageDoc?.filename || ''}
        meta={
          manageDoc && (
            <>
              <Badge subtle>{manageDoc.sourceType}</Badge>
              {manageDoc.classification.map((c) => (
                <Badge key={c} subtle>
                  {c}
                </Badge>
              ))}
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
                Delete document
              </Button>
            </>
          ) : manageDoc ? (
            <>
              {(manageDoc.owner === session.name || session.isAdmin) && (
                <Button variant="danger" size="sm" onClick={() => setSubView('delete')}>
                  Delete
                </Button>
              )}
              <Button size="sm" onClick={() => askAboutDoc(manageDoc)}>
                Ask about this document
              </Button>
            </>
          ) : undefined
        }
      >
        {manageState === 'loading' ? (
          <Skeleton height="14rem" />
        ) : manageState === 'error' ? (
          <p className="knowledge-note">{manageError}</p>
        ) : !manageDoc ? null : subView === 'delete' ? (
          <p className="knowledge-confirm-msg">
            Delete "{manageDoc.title || manageDoc.filename}"? This removes it — and every indexed chunk — from
            search and answers, and can't be undone.
          </p>
        ) : (
          <div className="knowledge-drawer-body">
            <KeyValue
              columns={2}
              items={[
                { label: 'Owner', value: manageDoc.owner },
                { label: 'Filename', value: manageDoc.filename, mono: true },
                { label: 'Source type', value: manageDoc.sourceType },
                { label: 'Chunks', value: String(manageDoc.chunkCount) },
                { label: 'Checksum', value: manageDoc.checksum, mono: true },
                { label: 'Created', value: formatWhen(manageDoc.createdAt) },
                { label: 'Updated', value: formatWhen(manageDoc.updatedAt) },
              ]}
            />
            {Object.keys(manageDoc.metadata).length > 0 && (
              <div className="knowledge-section-block">
                <p className="ui-eyebrow knowledge-section-label">Metadata</p>
                <KeyValue
                  columns={2}
                  items={Object.entries(manageDoc.metadata).map(([k, v]) => ({
                    label: humanizeKey(k),
                    value: formatSignalValue(v),
                  }))}
                />
              </div>
            )}
          </div>
        )}
      </Drawer>
    </div>
  )
}

export default KnowledgePage
