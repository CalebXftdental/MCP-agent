import { useCallback, useMemo, useState } from 'react'
import type { CSSProperties } from 'react'
import {
  BarList,
  Badge,
  Button,
  Card,
  Dropdown,
  EmptyState,
  Field,
  Input,
  PageShell,
  Textarea,
  TypingDots,
  useToast,
  type DropdownOption,
} from '../components/ui'
import MessageActions from '../components/chat/MessageActions'
// Reused for its `.chat-msg-body`/`.chat-msg-tool-line`/`.chat-msg-error`/
// `.chat-inline-chart` rules — a rendered reply looks the same here as in
// chat, right down to table/code-block styling, without copying those rules.
import '../components/chat/ChatMessageBubble.css'
import { useStoredList } from '../hooks/useStoredList'
import { streamOrChat } from '../lib/chatStream'
import { renderMarkdown, chartFromTable } from '../lib/markdown'
import { sendChatFeedback } from '../lib/api'
import type { PageProps } from './types'
import './PlaygroundPage.css'

/**
 * AI Playground — ports `renderPlayground()` / `AI_TASKS`: a curated set of
 * guided tasks, each filling a prompt template from a small form, run
 * through the same governed assistant the chat uses (`streamOrChat`, not a
 * new streaming implementation).
 *
 * Legacy stacked a grid of task tiles above a form that appeared underneath
 * whichever tile was clicked, pushing the page down every time. This is two
 * columns instead: tiles stay put on the left, the form and result live in a
 * fixed right-hand workspace — picking a different task never moves the
 * picker itself.
 *
 * The result panel reuses `MessageActions` (the same action row a chat
 * reply gets) wholesale rather than reimplementing legacy's smaller
 * Copy/Save/CSV-only set — Retry, the Chart toggle, and thumbs feedback come
 * along for free and now behave identically everywhere a governed reply
 * shows up in this app, not just in chat. Save writes to the same
 * `gov_savedans` list History's own "Saved answers" card reads, not a
 * parallel store.
 */

interface AiTaskField {
  k: string
  label: string
  ph?: string
  type?: 'select' | 'textarea'
  options?: string[]
}

interface AiTask {
  id: string
  icon: string
  title: string
  desc: string
  fields: AiTaskField[]
  tmpl: string
}

const AI_TASKS: AiTask[] = [
  {
    id: 'draft',
    icon: '✉️',
    title: 'Draft a message',
    desc: "A polished email or reminder, written from the account's data.",
    fields: [
      { k: 'who', label: 'Customer', ph: 'name, email, or customer id' },
      { k: 'about', label: 'About', ph: 'e.g. their overdue invoice' },
      { k: 'kind', label: 'Type', type: 'select', options: ['email', 'payment reminder', 'follow-up', 'thank-you note'] },
      { k: 'tone', label: 'Tone', type: 'select', options: ['friendly', 'formal', 'firm'] },
    ],
    tmpl: 'Draft a {tone} {kind} to {who} about {about}. Use only real details from their account; keep it concise and professional.',
  },
  {
    id: 'summary',
    icon: '📋',
    title: 'Summarize an account',
    desc: 'A quick 360° summary of a customer.',
    fields: [{ k: 'who', label: 'Customer', ph: 'name, email, or customer id' }],
    tmpl: 'Give me a concise summary of {who}: profile, recent orders, total spend, and any open invoices.',
  },
  {
    id: 'insights',
    icon: '💡',
    title: 'Get insights',
    desc: 'Surface anything notable or unusual.',
    fields: [{ k: 'who', label: 'Customer or topic', ph: 'e.g. Acme Dental, or "overdue invoices"' }],
    tmpl: 'Look at {who} and highlight anything notable, unusual, or worth my attention. Be specific and cite the numbers.',
  },
  {
    id: 'review',
    icon: '📊',
    title: 'Account review',
    desc: 'A review with key metrics and a short narrative.',
    fields: [{ k: 'who', label: 'Customer', ph: 'name, email, or customer id' }],
    tmpl: 'Generate an account review for {who}: key metrics, recent orders, open invoices, and a short narrative summary as a table where useful.',
  },
  {
    id: 'compare',
    icon: '⚖️',
    title: 'Compare two customers',
    desc: 'A side-by-side of two accounts.',
    fields: [
      { k: 'a', label: 'Customer A', ph: 'name, email, or id' },
      { k: 'b', label: 'Customer B', ph: 'name, email, or id' },
    ],
    tmpl: 'Compare {a} and {b}: profile, recent order volume, total spend, and open invoices. Present the comparison as a table.',
  },
  {
    id: 'justify',
    icon: '🔑',
    title: 'Draft an access request',
    desc: 'A business justification you can paste into a request.',
    fields: [
      { k: 'what', label: 'Access you need', ph: 'e.g. finance invoices' },
      { k: 'why', label: 'Why you need it', ph: 'e.g. month-end reconciliation' },
    ],
    tmpl:
      'Write a short, professional business justification (2-3 sentences) for requesting access to {what}, because I need to {why}. Output only the justification text — do not call any tools.',
  },
  {
    id: 'translate',
    icon: '🌐',
    title: 'Translate text',
    desc: 'Translate any text into another language.',
    fields: [
      {
        k: 'lang',
        label: 'Into',
        type: 'select',
        options: ['Spanish', 'French', 'German', 'Chinese (Simplified)', 'Portuguese', 'Japanese', 'English'],
      },
      { k: 'text', label: 'Text to translate', type: 'textarea', ph: 'Paste the text here' },
    ],
    tmpl:
      'Translate the following into {lang}, preserving meaning and tone. Output only the translation — do not call any tools:\n\n{text}',
  },
]

type RunStatus = 'idle' | 'tools' | 'answer' | 'done' | 'error'

function PlaygroundPage({ session }: PageProps) {
  const toast = useToast()
  const savedAnswers = useStoredList<{ text: string; ts: number }>('gov_savedans', session.name)

  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null)
  const [fieldValues, setFieldValues] = useState<Record<string, string>>({})
  const [extra, setExtra] = useState('')

  const [runStatus, setRunStatus] = useState<RunStatus>('idle')
  const [replyText, setReplyText] = useState('')
  const [toolsUsed, setToolsUsed] = useState<string[]>([])
  const [errorMessage, setErrorMessage] = useState<string | null>(null)
  const [conversationId, setConversationId] = useState<string | null>(null)
  const [lastPrompt, setLastPrompt] = useState<string | null>(null)
  const [feedback, setFeedback] = useState<'up' | 'down' | undefined>(undefined)
  const [chartOpen, setChartOpen] = useState(false)

  const selectedTask = useMemo(() => AI_TASKS.find((t) => t.id === selectedTaskId) ?? null, [selectedTaskId])

  const selectTask = useCallback((task: AiTask) => {
    setSelectedTaskId(task.id)
    const defaults: Record<string, string> = {}
    for (const f of task.fields) if (f.type === 'select' && f.options) defaults[f.k] = f.options[0]
    setFieldValues(defaults)
    setExtra('')
    setRunStatus('idle')
    setReplyText('')
    setErrorMessage(null)
    setLastPrompt(null)
    setFeedback(undefined)
    setChartOpen(false)
  }, [])

  const setFieldValue = useCallback((k: string, v: string) => {
    setFieldValues((prev) => ({ ...prev, [k]: v }))
  }, [])

  const runTask = useCallback(async (prompt: string) => {
    setLastPrompt(prompt)
    setRunStatus('tools')
    setReplyText('')
    setToolsUsed([])
    setErrorMessage(null)
    setFeedback(undefined)
    setChartOpen(false)

    const conv = `pg-${Math.random().toString(36).slice(2)}`
    setConversationId(conv)

    try {
      await streamOrChat(prompt, conv, (update) => {
        setRunStatus(update.phase)
        setReplyText(update.text)
        if (update.tools.length) setToolsUsed(update.tools)
      })
      setRunStatus('done')
    } catch (cause) {
      setErrorMessage(cause instanceof Error ? cause.message : 'Could not run that task.')
      setRunStatus('error')
    }
  }, [])

  const submitTask = useCallback(() => {
    if (!selectedTask) return
    for (const f of selectedTask.fields) {
      if (f.type !== 'select' && !(fieldValues[f.k] || '').trim()) {
        toast.warn(`Please fill in ${f.label}`)
        return
      }
    }
    let prompt = selectedTask.tmpl.replace(/\{(\w+)\}/g, (m, k: string) => fieldValues[k] || m)
    const extraTrimmed = extra.trim()
    if (extraTrimmed) prompt += `\n\nAdditional instructions from the user: ${extraTrimmed}`
    runTask(prompt)
  }, [selectedTask, fieldValues, extra, runTask, toast])

  const retry = useCallback(() => {
    if (lastPrompt) runTask(lastPrompt)
  }, [lastPrompt, runTask])

  const save = useCallback(() => {
    savedAnswers.add({ text: replyText, ts: Date.now() })
    toast.success('Saved to your answers')
  }, [replyText, savedAnswers, toast])

  const rate = useCallback(
    async (rating: 'up' | 'down') => {
      setFeedback(rating)
      if (!conversationId) return
      try {
        await sendChatFeedback(conversationId, rating)
      } catch {
        toast.error('Could not record that feedback.')
      }
    },
    [conversationId, toast],
  )

  const { nodes, tables } = useMemo(() => renderMarkdown(replyText), [replyText])
  const chartItems = useMemo(() => (tables[0] ? chartFromTable(tables[0]) : []), [tables])
  const running = runStatus === 'tools' || runStatus === 'answer'

  return (
    <PageShell className="playground" layout="bare">
      <Card
        className="playground-tasks"
        title="AI tasks"
        description="Pick a task, fill in a couple of details, and the assistant does it for you — governed and redacted just like the chat."
      >
        <div className="playground-task-list">
          {AI_TASKS.map((task, i) => (
            <button
              key={task.id}
              type="button"
              className="playground-task-btn"
              data-selected={task.id === selectedTaskId || undefined}
              style={{ '--ui-i': i } as CSSProperties}
              onClick={() => task.id !== selectedTaskId && selectTask(task)}
            >
              <span className="playground-task-icon" aria-hidden="true">
                {task.icon}
              </span>
              <span className="playground-task-text">
                <span className="playground-task-title">{task.title}</span>
                <span className="playground-task-desc">{task.desc}</span>
              </span>
            </button>
          ))}
        </div>
      </Card>

      <div className="playground-workspace">
        {!selectedTask ? (
          <Card className="playground-empty">
            <EmptyState
              title="Pick a task to get started"
              description="Choose one on the left — you'll fill in a couple of details, and the assistant does the rest."
            />
          </Card>
        ) : (
          <>
            <Card
              key={selectedTask.id}
              className="playground-form-card"
              title={
                <span className="playground-form-title">
                  <span aria-hidden="true">{selectedTask.icon}</span> {selectedTask.title}
                </span>
              }
              description={selectedTask.desc}
            >
              <div className="playground-form">
                <div className="playground-field-grid">
                  {selectedTask.fields.map((f) =>
                    f.type === 'select' ? (
                      <Field key={f.k} label={f.label}>
                        {(fieldProps) => (
                          <Dropdown
                            {...fieldProps}
                            value={fieldValues[f.k] ?? ''}
                            onChange={(v) => setFieldValue(f.k, v)}
                            options={(f.options ?? []).map((o): DropdownOption => ({ value: o, label: o }))}
                          />
                        )}
                      </Field>
                    ) : f.type === 'textarea' ? (
                      <div key={f.k} className="playground-field-wide">
                        <Field label={f.label}>
                          {(fieldProps) => (
                            <Textarea
                              {...fieldProps}
                              mono={false}
                              rows={4}
                              placeholder={f.ph}
                              value={fieldValues[f.k] ?? ''}
                              onChange={(e) => setFieldValue(f.k, e.target.value)}
                            />
                          )}
                        </Field>
                      </div>
                    ) : (
                      <Field key={f.k} label={f.label}>
                        {(fieldProps) => (
                          <Input
                            {...fieldProps}
                            placeholder={f.ph}
                            value={fieldValues[f.k] ?? ''}
                            onChange={(e) => setFieldValue(f.k, e.target.value)}
                          />
                        )}
                      </Field>
                    ),
                  )}
                </div>

                <Field label="Extra instructions" hint="Optional — how should it be analyzed?">
                  {(fieldProps) => (
                    <Textarea
                      {...fieldProps}
                      mono={false}
                      rows={2}
                      placeholder="e.g. focus on the last 3 months · flag anything over $10k · keep it to bullet points"
                      value={extra}
                      onChange={(e) => setExtra(e.target.value)}
                    />
                  )}
                </Field>

                <div className="playground-form-actions">
                  <Button onClick={submitTask} loading={running}>
                    Run task
                  </Button>
                </div>
              </div>
            </Card>

            {runStatus !== 'idle' && (
              <Card key={conversationId} className="playground-result">
                <div className="playground-result-head">
                  <h3 className="playground-result-title">Result</h3>
                  <Badge tone="warn">AI draft — review before use</Badge>
                </div>

                {runStatus === 'tools' && !replyText ? (
                  toolsUsed.length ? (
                    <p className="chat-msg-tool-line">using: {toolsUsed.join(', ')}…</p>
                  ) : (
                    <TypingDots />
                  )
                ) : runStatus === 'error' ? (
                  <p className="chat-msg-error">⚠ {errorMessage}</p>
                ) : (
                  <div className="chat-msg-body">{nodes}</div>
                )}

                {runStatus === 'done' && (
                  <>
                    {chartOpen && chartItems.length > 0 && (
                      <div className="chat-inline-chart">
                        <BarList items={chartItems.map((i) => ({ label: i.k, value: i.n }))} />
                      </div>
                    )}
                    <MessageActions
                      reply={replyText}
                      tables={tables}
                      onRetry={retry}
                      onSave={save}
                      feedback={feedback}
                      onRate={rate}
                      chartOpen={chartOpen}
                      onToggleChart={() => setChartOpen((v) => !v)}
                    />
                    {toolsUsed.length > 0 && (
                      <p className="chat-msg-tool-line chat-msg-tool-line--done">used: {toolsUsed.join(', ')}</p>
                    )}
                  </>
                )}
              </Card>
            )}
          </>
        )}
      </div>
    </PageShell>
  )
}

export default PlaygroundPage
