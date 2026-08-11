import type { ReactNode } from 'react'
import { Badge, DateTimePicker, Field, Input, Spinner, Switch } from '../ui'
import type { WorkflowApprovalGate, WorkflowInputRequirement, WorkflowPreflight } from '../../lib/api'
import './WorkflowInputFields.css'

/**
 * The dynamic, per-workflow part of the run/schedule form — everything below
 * "which workflow" and above the submit button. Shared by WorkflowsPage (run
 * now) and AutomationsPage (save a schedule) so both read the same set of
 * fields for a given template rather than maintaining two copies.
 *
 * Two things aren't backend-declared and are special-cased by template id:
 * the PDF-packet toggle (only `customer_email_draft` reads it) and which
 * approval-gate toggle maps to which input key. Everything else — which
 * fields appear at all, their labels, and whether they're required in
 * non-sample mode — comes straight from the preflight response.
 */

const PACKET_TEMPLATES = new Set(['customer_email_draft'])

/** The 5 built-in templates (gateway/workflows.py's `TEMPLATES`) are the only
 *  ones whose runners special-case `sample: true` to swap in local test data
 *  — a published "My Workflow" graph has no such concept (its interpreter
 *  runs whatever the trigger/step bindings say, always), so showing the
 *  toggle there would offer a switch that silently does nothing. */
const SAMPLE_AWARE_TEMPLATES = new Set([
  'customer_360_report',
  'shipment_exception_report',
  'vendor_ap_summary',
  'customer_email_draft',
  'weekly_executive_brief',
])

function approvalInputKey(gateId: string): string {
  return gateId === 'send_approval' ? 'request_send_approval' : 'request_approval'
}

function stringValue(values: Record<string, unknown>, name: string): string {
  const v = values[name]
  return typeof v === 'string' ? v : v == null ? '' : String(v)
}

export interface SampleToggleProps {
  templateId: string
  values: Record<string, unknown>
  setValue: (name: string, value: unknown) => void
}

/** The "Use sample data" switch, split out of the fields block so a page can
 *  place it in its Card's header `actions` (top-right, next to the title)
 *  instead of stacked above the fields — no description text, to keep that
 *  header row compact. Renders nothing for a template `SAMPLE_AWARE_TEMPLATES`
 *  doesn't cover, same rule `WorkflowInputFields` itself uses. */
export function SampleToggle({ templateId, values, setValue }: SampleToggleProps) {
  if (!SAMPLE_AWARE_TEMPLATES.has(templateId)) return null
  return (
    <Switch checked={values.sample !== false} onChange={(v) => setValue('sample', v)} label="Use sample data" size="sm" />
  )
}

/** A field a backend only ever treats as a calendar day (`start_date`,
 *  `end_date`) — the one naming convention every workflow's declared inputs
 *  actually follows, so this is enough to route it to the date picker
 *  instead of a plain text box without a backend-side schema flag. */
function isDateField(name: string): boolean {
  return name.endsWith('_date')
}

export interface WorkflowInputFieldsProps {
  templateId: string
  requiredInputs: WorkflowInputRequirement[]
  approvalGates: WorkflowApprovalGate[]
  values: Record<string, unknown>
  setValue: (name: string, value: unknown) => void
  /** Rendered as a trailing item in the same row as the input fields (e.g.
   *  the page's own "Run workflow" button) — so a short field list and its
   *  submit action read as one compact line instead of a full-width form
   *  block stacked above a separate button row. */
  actions?: ReactNode
}

export function WorkflowInputFields({
  templateId,
  requiredInputs,
  approvalGates,
  values,
  setValue,
  actions,
}: WorkflowInputFieldsProps) {
  const sample = values.sample !== false
  const sampleAware = SAMPLE_AWARE_TEMPLATES.has(templateId)
  // Sample mode only means something for the 5 hardcoded templates, whose
  // runners actually swap in local test data server-side when sample=true --
  // there, the placeholder previewing that sample value is honest. A "My
  // Workflow" graph has no such swap (see SAMPLE_AWARE_TEMPLATES' own note),
  // so its fields are always plain "type the real value" inputs: required
  // whenever not explicitly marked optional, hinted by their own label
  // rather than a sample value that will never actually get used.
  const placeholderFor = (input: WorkflowInputRequirement) =>
    sampleAware ? (sample ? input.sampleDefault ?? '' : input.label) : input.label

  return (
    <div className="wf-fields">
      {(requiredInputs.length > 0 || actions) && (
        <div className="wf-fields-row">
          {requiredInputs.map((input) => (
            <div key={input.name} className="wf-field-cell">
              <Field label={input.label} required={sampleAware ? !sample && !!input.requiredWhenSampleFalse : !input.optional}>
                {(fieldProps) =>
                  isDateField(input.name) ? (
                    <DateTimePicker
                      {...fieldProps}
                      mode="date"
                      placeholder={placeholderFor(input)}
                      value={stringValue(values, input.name)}
                      onChange={(v) => setValue(input.name, v)}
                    />
                  ) : (
                    <Input
                      {...fieldProps}
                      placeholder={placeholderFor(input)}
                      value={stringValue(values, input.name)}
                      onChange={(e) => setValue(input.name, e.target.value)}
                    />
                  )
                }
              </Field>
            </div>
          ))}
          {actions && <div className="wf-fields-actions">{actions}</div>}
        </div>
      )}

      {(PACKET_TEMPLATES.has(templateId) || approvalGates.length > 0) && (
        <div className="wf-fields-toggles">
          {PACKET_TEMPLATES.has(templateId) && (
            <Switch
              checked={values.include_packet !== false}
              onChange={(v) => setValue('include_packet', v)}
              label="Include a PDF packet"
              description="Attach a formatted summary alongside the draft."
              size="sm"
            />
          )}
          {approvalGates.map((gate) => (
            <Switch
              key={gate.id}
              checked={values[approvalInputKey(gate.id)] === true}
              onChange={(v) => setValue(approvalInputKey(gate.id), v)}
              label={gate.label}
              description="Pause here for an admin's sign-off before this counts as done."
              size="sm"
            />
          ))}
        </div>
      )}
    </div>
  )
}

export interface WorkflowReadinessProps {
  preflight: WorkflowPreflight | null
  checking: boolean
  error: string | null
}

/** A plain-English "would this actually work right now" strip, replacing the
 *  legacy panel's pill wall of Blockers/Warnings/missingCategories.
 *
 *  The "not ready" case renders nothing here on purpose: blockers now surface
 *  as a `WorkflowAlertBadge` next to the section's own title (hover/focus
 *  reveals them one per line) instead of a red block in the form body — the
 *  page is responsible for mounting that badge from the same `preflight`. */
export function WorkflowReadiness({ preflight, checking, error }: WorkflowReadinessProps) {
  if (error) return <p className="wf-readiness wf-readiness-error">⚠ {error}</p>
  if (!preflight) {
    return checking ? (
      <p className="wf-readiness wf-readiness-checking">
        <Spinner size="sm" label={null} /> Checking what this needs…
      </p>
    ) : null
  }
  if (!preflight.ready) return null
  return (
    <div className="wf-readiness wf-readiness-ok">
      <Badge tone="ok" dot>
        Ready to run
      </Badge>
      {preflight.warnings.map((w, i) => (
        <p key={i} className="wf-readiness-note">
          {w}
        </p>
      ))}
    </div>
  )
}
