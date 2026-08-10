import { Dropdown, Field, Input, Switch, Textarea, type DropdownOption } from '../ui'
import type { GraphNode, WorkflowBinding, WorkflowGraphCatalogTool } from '../../lib/api'
import './StepConfigFields.css'

/** A tool arg's raw JSON-Schema shape, as exposed by `/dashboard/workflow-graph-catalog` —
 *  enough to pick a sane input widget and a fallback label/hint when the
 *  manifest didn't supply a human `description`. */
export interface ArgSchema {
  type?: string
  description?: string
  enum?: unknown[]
}

/** `customer_id` -> `Customer id` — used only when the tool's manifest gives
 *  no `description` for an argument, so the field still reads as words
 *  instead of a raw JSON key. */
function humanizeArgName(name: string): string {
  const spaced = name.replace(/_/g, ' ').trim()
  return spaced ? spaced[0].toUpperCase() + spaced.slice(1) : name
}

/**
 * One step's configuration form — the whole "fill in this step's values"
 * surface, for every step kind (tool call, approval gate, AI step).
 *
 * Lives here rather than inline in MyWorkflowsPage because it's now rendered
 * in the add/edit modal (`StepModal`'s second phase) rather than expanded
 * permanently inside each node of the flow: the flow shows a compact node,
 * and configuring it is a deliberate "open this step" action.
 */

export interface TriggerInput {
  name: string
  label: string
}

/** An earlier step's output, offered as a value source for a later step. */
export interface NodeSource {
  nodeId: string
  path: string
  label: string
}

function bindingSource(binding: WorkflowBinding | undefined): 'literal' | 'trigger' | 'node' {
  return binding?.source ?? 'literal'
}

interface BindingRowProps {
  argLabel: string
  /** Standing guidance shown under the field — the raw arg name and its type,
   *  always visible, so the exact parameter being set is never just a guess
   *  from the (possibly humanized, possibly reused-elsewhere) label above it. */
  hint?: string
  required?: boolean
  binding: WorkflowBinding | undefined
  /** Present for a tool arg (drives which literal-value widget renders);
   *  absent for the one hardcoded slot (`llm_transform.input_text`), which
   *  always gets the plain text box it already had. */
  schema?: ArgSchema
  triggerInputs: TriggerInput[]
  nodeSources: NodeSource[]
  onChange: (binding: WorkflowBinding | null) => void
}

/** The literal-value control itself, switched on the arg's declared JSON-Schema
 *  type instead of one `<Input>` for everything — a boolean/enum arg otherwise
 *  looked identical to a freeform string one, with nothing hinting what values
 *  were even valid. */
function LiteralValueControl({
  schema,
  value,
  onChange,
}: {
  schema: ArgSchema | undefined
  value: unknown
  onChange: (value: unknown) => void
}) {
  if (schema?.enum && schema.enum.length > 0) {
    const options: DropdownOption[] = schema.enum.map((v) => ({ value: String(v), label: String(v) }))
    return (
      <Dropdown
        value={value !== undefined && value !== null ? String(value) : ''}
        onChange={(v) => onChange(schema.enum!.find((e) => String(e) === v) ?? v)}
        options={options}
        placeholder="Choose…"
      />
    )
  }
  if (schema?.type === 'boolean') {
    return <Switch size="sm" checked={value === true} onChange={onChange} label={value === true ? 'On' : 'Off'} />
  }
  if (schema?.type === 'integer' || schema?.type === 'number') {
    return (
      <Input
        type="number"
        step={schema.type === 'integer' ? 1 : 'any'}
        value={value === undefined || value === null ? '' : String(value)}
        onChange={(e) => onChange(e.target.value === '' ? '' : Number(e.target.value))}
        placeholder="value"
      />
    )
  }
  return (
    <Input value={value !== undefined && value !== null ? String(value) : ''} onChange={(e) => onChange(e.target.value)} placeholder="value" />
  )
}

/** One step argument's value source — typed literally, taken from this
 *  workflow's own declared input, or taken from an earlier step's output.
 *  Mirrors `gateway/workflow_graph_interpreter.py`'s `_resolve_binding`
 *  exactly: those are the only three sources it understands. */
function BindingRow({ argLabel, hint, required, binding, schema, triggerInputs, nodeSources, onChange }: BindingRowProps) {
  const source = bindingSource(binding)
  const sourceOptions: DropdownOption[] = [
    { value: 'literal', label: 'Type a value' },
    ...(triggerInputs.length ? [{ value: 'trigger', label: "From this workflow's input" }] : []),
    ...(nodeSources.length ? [{ value: 'node', label: 'From an earlier step' }] : []),
  ]

  return (
    <Field label={argLabel} required={required} hint={hint}>
      {() => (
        <div className="step-binding-controls">
          <Dropdown
            value={source}
            onChange={(v) => {
              if (v === 'literal') onChange({ source: 'literal', value: schema?.type === 'boolean' ? false : '' })
              else if (v === 'trigger' && triggerInputs[0]) onChange({ source: 'trigger', path: triggerInputs[0].name })
              else if (v === 'node' && nodeSources[0]) onChange({ source: 'node', node_id: nodeSources[0].nodeId, path: nodeSources[0].path })
            }}
            options={sourceOptions}
          />
          {source === 'literal' && (
            <LiteralValueControl
              schema={schema}
              value={binding && binding.source === 'literal' ? binding.value : undefined}
              onChange={(value) => onChange({ source: 'literal', value })}
            />
          )}
          {source === 'trigger' && (
            <Dropdown
              value={binding && binding.source === 'trigger' ? binding.path : ''}
              onChange={(path) => onChange({ source: 'trigger', path })}
              options={triggerInputs.map((t) => ({ value: t.name, label: t.label || t.name }))}
            />
          )}
          {source === 'node' && (
            <Dropdown
              value={binding && binding.source === 'node' ? `${binding.node_id}::${binding.path}` : ''}
              onChange={(v) => {
                const [nodeId, path] = v.split('::')
                onChange({ source: 'node', node_id: nodeId, path })
              }}
              options={nodeSources.map((s) => ({ value: `${s.nodeId}::${s.path}`, label: s.label }))}
            />
          )}
        </div>
      )}
    </Field>
  )
}

export interface StepConfigFieldsProps {
  step: GraphNode
  tool: WorkflowGraphCatalogTool | undefined
  triggerInputs: TriggerInput[]
  /** Outputs of the steps that come BEFORE this one — the only ones it may
   *  bind to, since the interpreter walks the chain in order. */
  nodeSources: NodeSource[]
  onConfigChange: (config: Record<string, unknown>) => void
  onBind: (arg: string, binding: WorkflowBinding | null) => void
}

function StepConfigFields({ step, tool, triggerInputs, nodeSources, onConfigChange, onBind }: StepConfigFieldsProps) {
  if (step.kind === 'tool_call') {
    const properties = tool?.parameters?.properties ?? {}
    const required = new Set(tool?.parameters?.required ?? [])
    const argNames = Object.keys(properties)
    if (!tool) return <p className="step-config-empty">This tool is no longer available.</p>
    return (
      <div className="step-config">
        <p className="step-config-lead">{tool.description}</p>
        {argNames.length === 0 ? (
          <p className="step-config-empty">This tool takes no inputs — nothing to configure.</p>
        ) : (
          argNames.map((arg) => {
            const schema = properties[arg]
            const typeLabel = schema?.enum?.length ? 'choice' : schema?.type || 'text'
            return (
            <BindingRow
              key={arg}
              argLabel={schema?.description || humanizeArgName(arg)}
              hint={`${arg} · ${typeLabel}`}
              schema={schema}
              required={required.has(arg)}
              binding={step.inputBindings[arg]}
              triggerInputs={triggerInputs}
              nodeSources={nodeSources}
              onChange={(b) => onBind(arg, b)}
            />
            )
          })
        )}
      </div>
    )
  }

  if (step.kind === 'approval_gate') {
    return (
      <div className="step-config">
        <p className="step-config-lead">Everything after this step waits until an admin signs off.</p>
        <Field label="Reason" hint="Shown to whoever reviews it.">
          {(fp) => (
            <Textarea
              {...fp}
              mono={false}
              rows={2}
              value={String(step.config.reason ?? '')}
              onChange={(e) => onConfigChange({ ...step.config, reason: e.target.value })}
            />
          )}
        </Field>
        <Field label="Risk level">
          {(fp) => (
            <Dropdown
              {...fp}
              value={String(step.config.risk_level ?? 'medium')}
              onChange={(v) => onConfigChange({ ...step.config, risk_level: v })}
              options={[
                { value: 'medium', label: 'Medium' },
                { value: 'high', label: 'High' },
              ]}
            />
          )}
        </Field>
      </div>
    )
  }

  return (
    <div className="step-config">
      <p className="step-config-lead">Runs text from an earlier step through the governed assistant.</p>
      <Field label="What should it do?">
        {(fp) => (
          <Dropdown
            {...fp}
            value={String(step.config.kind ?? 'summarize')}
            onChange={(v) => onConfigChange({ ...step.config, kind: v })}
            options={[
              { value: 'summarize', label: 'Summarize' },
              { value: 'draft_reply', label: 'Draft a reply' },
              { value: 'classify', label: 'Classify' },
              { value: 'extract', label: 'Extract' },
            ]}
          />
        )}
      </Field>
      <Field label="Instruction" hint="Optional — extra direction for the assistant.">
        {(fp) => (
          <Textarea
            {...fp}
            mono={false}
            rows={2}
            value={String(step.config.instruction ?? '')}
            onChange={(e) => onConfigChange({ ...step.config, instruction: e.target.value })}
          />
        )}
      </Field>
      <BindingRow
        argLabel="Text to work from"
        binding={step.inputBindings.input_text}
        triggerInputs={triggerInputs}
        nodeSources={nodeSources}
        onChange={(b) => onBind('input_text', b)}
      />
    </div>
  )
}

export default StepConfigFields
