import { useCallback, useState } from 'react'
import { useToast } from '../ui'
import { chartFromTable, tableToCsv, type MarkdownTable } from '../../lib/markdown'
import { downloadText } from '../../lib/download'
import './MessageActions.css'

/**
 * The action row under a completed bot reply — `actionsBar()` from
 * static/app.html: copy, CSV/chart (only when the reply contains a table),
 * retry, save to pinned answers, and thumbs feedback.
 *
 * The legacy version read its table back out of the live DOM
 * (`p.querySelector('.mdtable')`) to build the CSV and chart. This takes the
 * parsed `MarkdownTable[]` the renderer already produced instead — the first
 * table in the reply, matching the legacy behavior of acting on whichever table
 * `querySelector` found first.
 */

export interface MessageActionsProps {
  reply: string
  tables: MarkdownTable[]
  onRetry: () => void
  onSave: () => void
  feedback?: 'up' | 'down'
  onRate: (rating: 'up' | 'down') => void
  /** Toggled inline by the Chart button; owned by the parent so the chart can
   *  render as a sibling block, matching where the legacy version inserted it. */
  chartOpen: boolean
  onToggleChart: () => void
}

function MessageActions({
  reply,
  tables,
  onRetry,
  onSave,
  feedback,
  onRate,
  chartOpen,
  onToggleChart,
}: MessageActionsProps) {
  const toast = useToast()
  const [copied, setCopied] = useState(false)
  const table = tables[0]

  const copy = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(reply)
      setCopied(true)
      setTimeout(() => setCopied(false), 1600)
    } catch {
      toast.error('Could not copy — your browser blocked clipboard access.')
    }
  }, [reply, toast])

  const exportCsv = useCallback(() => {
    if (!table) return
    downloadText('result.csv', tableToCsv(table), 'text/csv')
  }, [table])

  const toggleChart = useCallback(() => {
    if (!table) return
    if (!chartOpen && chartFromTable(table).length === 0) {
      toast.warn('No numeric column to chart')
      return
    }
    onToggleChart()
  }, [chartOpen, onToggleChart, table, toast])

  return (
    <div className="msg-actions">
      <button type="button" onClick={copy}>
        {copied ? '✓ Copied' : '⧉ Copy'}
      </button>
      {table && (
        <>
          <button type="button" onClick={exportCsv}>
            ⬇ CSV
          </button>
          <button type="button" onClick={toggleChart}>
            📊 Chart
          </button>
        </>
      )}
      <button type="button" onClick={onRetry}>
        ↻ Retry
      </button>
      <button type="button" onClick={onSave}>
        ☆ Save
      </button>
      <button
        type="button"
        className={feedback === 'up' ? 'is-on' : ''}
        title="Helpful"
        onClick={() => onRate('up')}
      >
        👍
      </button>
      <button
        type="button"
        className={feedback === 'down' ? 'is-on' : ''}
        title="Not helpful"
        onClick={() => onRate('down')}
      >
        👎
      </button>
    </div>
  )
}

export default MessageActions
