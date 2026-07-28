import { useEffect, useState } from 'react'
import { Badge, Button, Card, useToast } from '../ui'
import {
  ApiError,
  getWorkflowSuggestions,
  launchWorkflowSuggestion,
  type WorkflowSuggestion,
} from '../../lib/api'
import './WorkflowSuggestions.css'

/**
 * "Run this as a workflow" — `addWorkflowSuggestions()`. Fetched per completed
 * turn (deterministic keyword match server-side, not an LLM call), and
 * best-effort: a failure here is swallowed exactly like the legacy `catch(e)
 * {return;}`, since the reply itself already answered the question and a
 * broken suggestion box shouldn't put an error in front of a good answer.
 */

export interface WorkflowSuggestionsProps {
  question: string
  reply: string
  navigate: (key: 'workflows' | 'files') => void
}

type LaunchState = 'idle' | 'launching' | 'launched'

function WorkflowSuggestions({ question, reply, navigate }: WorkflowSuggestionsProps) {
  const toast = useToast()
  const [suggestions, setSuggestions] = useState<WorkflowSuggestion[] | null>(null)
  const [launch, setLaunch] = useState<Record<string, LaunchState>>({})
  const [result, setResult] = useState<{
    runId: string
    status: string
    links: { filename: string; downloadUrl: string }[]
  } | null>(null)

  useEffect(() => {
    let live = true
    getWorkflowSuggestions(`${question}\n${reply}`, 3)
      .then((res) => {
        if (live) setSuggestions(res.suggestions ?? [])
      })
      .catch(() => {
        if (live) setSuggestions([])
      })
    return () => {
      live = false
    }
    // question/reply are the turn's final content and don't change after the
    // message settles, so this fetches exactly once per completed turn.
  }, [question, reply])

  if (!suggestions || suggestions.length === 0) return null

  const runLaunch = async (suggestion: WorkflowSuggestion) => {
    setLaunch((prev) => ({ ...prev, [suggestion.templateId]: 'launching' }))
    try {
      const res = await launchWorkflowSuggestion(suggestion.templateId, suggestion.inputs)
      const links = (res.artifacts ?? [])
        .filter((a): a is typeof a & { downloadUrl: string } => Boolean(a.downloadUrl))
        .map((a) => ({ filename: a.filename, downloadUrl: a.downloadUrl }))
      setResult({ runId: res.runId, status: res.status, links })
      setLaunch((prev) => ({ ...prev, [suggestion.templateId]: 'launched' }))
      toast.success('Workflow launched')
    } catch (cause) {
      setLaunch((prev) => ({ ...prev, [suggestion.templateId]: 'idle' }))
      toast.error(cause instanceof ApiError ? cause.message : 'Could not launch that workflow')
    }
  }

  return (
    <Card title="Run this as a workflow" className="wf-suggest-card">
      <p className="wf-suggest-desc">
        Create governed files, approvals, and audit-backed workflow runs from this
        request.
      </p>
      <div className="wf-suggest-grid">
        {suggestions.map((s) => {
          const state = launch[s.templateId] ?? 'idle'
          return (
            <Card key={s.templateId} className="wf-suggest-item">
              <div className="wf-suggest-item-head">
                <strong>{s.displayName}</strong>
                <Badge tone="ok">{s.outputTypes.join(', ') || 'workflow'}</Badge>
              </div>
              <p className="wf-suggest-reason">{s.reason}</p>
              <Button
                size="sm"
                variant="ghost"
                loading={state === 'launching'}
                disabled={state === 'launched'}
                onClick={() => runLaunch(s)}
              >
                {state === 'launched' ? 'Launched' : 'Launch'}
              </Button>
            </Card>
          )
        })}
      </div>

      {result && (
        <p className="wf-suggest-result">
          workflow {result.status}: <span className="ui-mono">{result.runId}</span>
          {result.links.map((link, i) => (
            <span key={link.downloadUrl}>
              {i === 0 ? ' · ' : ' | '}
              <a href={link.downloadUrl}>{link.filename}</a>
            </span>
          ))}
          {' · '}
          <Button variant="quiet" size="sm" onClick={() => navigate('workflows')}>
            Runs
          </Button>
          <Button variant="quiet" size="sm" onClick={() => navigate('files')}>
            Files
          </Button>
        </p>
      )}
    </Card>
  )
}

export default WorkflowSuggestions
