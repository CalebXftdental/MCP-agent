import { useCallback, useEffect, useMemo, useState } from 'react'
import { Button, Modal } from '../ui'
import StepTypeTile from './StepTypeTile'
import StepConfigFields, { type NodeSource, type TriggerInput } from './StepConfigFields'
import type { GraphNode, WorkflowBinding, WorkflowGraphCatalogTool } from '../../lib/api'
import { stepIcon, stepKindLabel } from './stepMeta'
import './StepModal.css'

/**
 * Add-or-edit a step, in two phases: pick what the step does (hoverable
 * tiles, grouped like the legacy canvas's palette), then fill in its values.
 *
 * Editing an existing step opens straight at phase 2 — there's no "change
 * this step's type" path on purpose: a step's kind determines its entire
 * config shape, so switching it would silently discard every binding. Delete
 * and re-add instead, which is explicit about losing that work.
 *
 * The draft step is held locally and only handed to `onCommit` on Save, so
 * cancelling out of the modal leaves the workflow untouched — an edit that
 * wrote straight through to page state would make Cancel a no-op and lose
 * the previous values with no way back.
 */

export interface StepModalProps {
  open: boolean
  onClose: () => void
  catalog: WorkflowGraphCatalogTool[]
  triggerInputs: TriggerInput[]
  /** Outputs available to the step being added/edited — the page computes
   *  this from the step's position in the chain. */
  nodeSources: NodeSource[]
  /** Non-null when editing; null when adding a brand-new step. */
  editing: GraphNode | null
  /** Builds a blank step of the chosen kind (`'approval_gate'`,
   *  `'llm_transform'`, or a tool's canonical name). The page owns this so
   *  node-id generation stays in one place. */
  makeStep: (kind: string) => GraphNode | null
  onCommit: (step: GraphNode) => void
}

function StepModal({ open, onClose, catalog, triggerInputs, nodeSources, editing, makeStep, onCommit }: StepModalProps) {
  const [draft, setDraft] = useState<GraphNode | null>(null)

  // Re-seed each time the modal opens: editing starts from a copy of the real
  // step, adding starts at the picker (null draft).
  useEffect(() => {
    if (open) setDraft(editing ? { ...editing, config: { ...editing.config }, inputBindings: { ...editing.inputBindings } } : null)
  }, [open, editing])

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

  const pickKind = useCallback(
    (kind: string) => {
      const step = makeStep(kind)
      if (step) setDraft(step)
    },
    [makeStep],
  )

  const setConfig = useCallback((config: Record<string, unknown>) => {
    setDraft((prev) => (prev ? { ...prev, config } : prev))
  }, [])

  const setBinding = useCallback((arg: string, binding: WorkflowBinding | null) => {
    setDraft((prev) => {
      if (!prev) return prev
      const inputBindings = { ...prev.inputBindings }
      if (binding) inputBindings[arg] = binding
      else delete inputBindings[arg]
      return { ...prev, inputBindings }
    })
  }, [])

  const commit = useCallback(() => {
    if (!draft) return
    onCommit(draft)
    onClose()
  }, [draft, onCommit, onClose])

  const draftTool = draft?.kind === 'tool_call' ? catalog.find((t) => t.canonical === draft.tool) : undefined

  return (
    <Modal
      open={open}
      onClose={onClose}
      eyebrow={editing ? 'Edit step' : 'Add a step'}
      title={draft ? stepKindLabel(draft.kind, draft.tool) : 'What should this step do?'}
      width="42rem"
      footer={
        draft && (
          <>
            {/* Back only exists while ADDING — an edit has no picker phase to
                return to (see the module note on why kind is immutable). */}
            {!editing && (
              <Button variant="ghost" onClick={() => setDraft(null)}>
                Back
              </Button>
            )}
            <Button variant="ghost" onClick={onClose}>
              Cancel
            </Button>
            <Button onClick={commit}>{editing ? 'Save step' : 'Add step'}</Button>
          </>
        )
      }
    >
      {!draft ? (
        <div className="step-picker">
          <div className="step-picker-group">
            <p className="step-picker-group-label">Flow control</p>
            <div className="step-picker-grid">
              <StepTypeTile
                icon="✅"
                title="Approval gate"
                description="Pause and require an admin's sign-off before continuing."
                onSelect={() => pickKind('approval_gate')}
              />
              <StepTypeTile
                icon="✨"
                title="AI step"
                description="Summarize, draft, classify, or extract from an earlier step's output."
                onSelect={() => pickKind('llm_transform')}
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
                    icon={stepIcon('tool_call', tool)}
                    title={tool.canonical}
                    description={tool.description}
                    locked={!tool.granted}
                    lockedReason={!tool.granted ? "You don't currently have access to this tool." : undefined}
                    onSelect={() => pickKind(tool.canonical)}
                  />
                ))}
              </div>
            </div>
          ))}
        </div>
      ) : (
        <StepConfigFields
          step={draft}
          tool={draftTool}
          triggerInputs={triggerInputs}
          nodeSources={nodeSources}
          onConfigChange={setConfig}
          onBind={setBinding}
        />
      )}
    </Modal>
  )
}

export default StepModal
