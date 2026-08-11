import type { GraphNode, WorkflowBinding, WorkflowGraphCatalogTool } from '../../lib/api'
import { isArgFilled } from './graphModel'

/** Display helpers shared by the flow nodes and the add/edit modal, so a step
 *  reads the same in both places. */

const RISK_ICONS: Record<string, string> = {
  send: '✉️',
  write: '📝',
  export: '📊',
  read_sensitive: '🔍',
  read_low: '🔎',
}

export function stepKindLabel(kind: string, tool: string): string {
  if (kind === 'approval_gate') return 'Approval gate'
  if (kind === 'llm_transform') return 'AI step'
  if (kind === 'filter') return 'Filter'
  return tool || 'Step'
}

export function stepIcon(kind: string, tool: WorkflowGraphCatalogTool | undefined): string {
  if (kind === 'approval_gate') return '✅'
  if (kind === 'llm_transform') return '✨'
  if (kind === 'filter') return '🧮'
  return (tool?.riskLevel && RISK_ICONS[tool.riskLevel]) || '🔧'
}

const AI_ACTION_LABELS: Record<string, string> = {
  summarize: 'Summarize',
  draft_reply: 'Draft a reply',
  classify: 'Classify',
  extract: 'Extract',
}

function bindingSummary(binding: WorkflowBinding | undefined): string | null {
  if (!binding) return null
  if (binding.source === 'literal') {
    const value = String(binding.value ?? '').trim()
    return value ? `"${value}"` : null
  }
  if (binding.source === 'trigger') return `input: ${binding.path}`
  return `from an earlier step`
}

/** A one-line "what is this step actually set to" summary for the collapsed
 *  flow node — so the flow is readable without opening every step. Returns
 *  null when there's nothing configured worth showing, and the node falls
 *  back to a "needs setting up" hint instead. */
export function stepSummary(step: GraphNode, tool: WorkflowGraphCatalogTool | undefined): string | null {
  if (step.kind === 'approval_gate') {
    const reason = String(step.config.reason ?? '').trim()
    const risk = String(step.config.risk_level ?? 'medium')
    return reason ? `${reason} · ${risk} risk` : `${risk} risk`
  }

  if (step.kind === 'llm_transform') {
    const action = AI_ACTION_LABELS[String(step.config.kind ?? 'summarize')] ?? 'Summarize'
    const from = bindingSummary(step.inputBindings.input_text)
    return from ? `${action} · ${from}` : action
  }

  if (step.kind === 'filter') {
    const conditions = (step.config.conditions as { all?: unknown[] } | undefined)?.all ?? []
    const count = conditions.length
    const from = bindingSummary(step.inputBindings.input)
    const limit = bindingSummary(step.config.match_limit as WorkflowBinding | undefined)
    let rule = count === 0 ? 'No conditions yet' : `${count} condition${count === 1 ? '' : 's'}`
    if (limit) rule += ` · up to ${limit}`
    return from ? `${rule} · ${from}` : rule
  }

  const argNames = Object.keys(tool?.parameters?.properties ?? {})
  if (argNames.length === 0) return 'No inputs needed'
  const set = argNames.filter((a) => isArgFilled(step.inputBindings[a])).length
  return set === 0 ? null : `${set} of ${argNames.length} input${argNames.length === 1 ? '' : 's'} set`
}
