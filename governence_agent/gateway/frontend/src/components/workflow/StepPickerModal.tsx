import { useMemo } from 'react'
import { Modal } from '../ui'
import StepTypeTile from './StepTypeTile'
import type { WorkflowGraphCatalogTool } from '../../lib/api'
import './StepPickerModal.css'

/**
 * The "Add a step" overlay — replaces the flat Dropdown of every tool name
 * with hoverable tiles grouped the way the legacy canvas's palette was
 * (flow control first, then one group per backend), so picking a step is
 * browsing a small catalog rather than reading a long alphabetical list.
 */

const RISK_ICONS: Record<string, string> = {
  send: '✉️',
  write: '📝',
  export: '📊',
  read_sensitive: '🔍',
  read_low: '🔎',
}

function toolIcon(tool: WorkflowGraphCatalogTool): string {
  return (tool.riskLevel && RISK_ICONS[tool.riskLevel]) || '🔧'
}

export interface StepPickerModalProps {
  open: boolean
  onClose: () => void
  catalog: WorkflowGraphCatalogTool[]
  onPick: (kind: string) => void
}

function StepPickerModal({ open, onClose, catalog, onPick }: StepPickerModalProps) {
  const groups = useMemo(() => {
    const byBackend = new Map<string, WorkflowGraphCatalogTool[]>()
    for (const tool of catalog) {
      const key = tool.backend ?? 'other'
      const list = byBackend.get(key)
      if (list) list.push(tool)
      else byBackend.set(key, [tool])
    }
    return [...byBackend.entries()].sort(([a], [b]) => a.localeCompare(b))
  }, [catalog])

  const pick = (kind: string) => {
    onPick(kind)
    onClose()
  }

  return (
    <Modal open={open} onClose={onClose} eyebrow="Add a step" title="What should this step do?" width="42rem">
      <div className="step-picker">
        <div className="step-picker-group">
          <p className="step-picker-group-label">Flow control</p>
          <div className="step-picker-grid">
            <StepTypeTile
              icon="✅"
              title="Approval gate"
              description="Pause and require an admin's sign-off before continuing."
              onSelect={() => pick('approval_gate')}
            />
            <StepTypeTile
              icon="✨"
              title="AI step"
              description="Summarize, draft, classify, or extract from an earlier step's output."
              onSelect={() => pick('llm_transform')}
            />
          </div>
        </div>

        {groups.map(([backend, tools]) => (
          <div className="step-picker-group" key={backend}>
            <p className="step-picker-group-label">{backend}</p>
            <div className="step-picker-grid">
              {tools.map((tool) => (
                <StepTypeTile
                  key={tool.canonical}
                  icon={toolIcon(tool)}
                  title={tool.canonical}
                  description={tool.description}
                  locked={!tool.granted}
                  lockedReason={!tool.granted ? "You don't currently have access to this tool." : undefined}
                  onSelect={() => pick(tool.canonical)}
                />
              ))}
            </div>
          </div>
        ))}
      </div>
    </Modal>
  )
}

export default StepPickerModal
