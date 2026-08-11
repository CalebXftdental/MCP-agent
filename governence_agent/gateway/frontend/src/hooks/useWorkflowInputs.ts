import { useCallback, useEffect, useRef, useState } from 'react'
import { getWorkflowPreflight, type WorkflowPreflight } from '../lib/api'

/**
 * Drives the "what does this workflow actually need from me" form shared by
 * the Workflows (run now) and Automations (schedule) pages.
 *
 * The legacy panel showed one static form with every field every workflow
 * might ever use (customer id, vendor code, recipient, topic, two dates, a
 * row limit) regardless of which workflow was selected. The preflight
 * endpoint already returns exactly the fields a given template declares
 * (`requiredInputs`) — this hook just calls it on a debounce every time the
 * template or draft values change, so the caller can render only those
 * fields and a live, plain-English readiness read instead of a fixed grid.
 */

const DEBOUNCE_MS = 300

export interface WorkflowInputsState {
  values: Record<string, unknown>
  setValue: (name: string, value: unknown) => void
  reset: () => void
  /** Re-runs the preflight check against the same `templateId`/`values` —
   *  for when the workflow itself just changed server-side (e.g. a "My
   *  Workflow" graph was just published with a newly declared trigger
   *  input) rather than the user's selection or draft values. Publishing
   *  doesn't change `templateId` (same graph id) or `values`, so without
   *  this the fetch effect below never re-fires and the run form keeps
   *  showing whatever fields the PREVIOUSLY published version declared. */
  refresh: () => void
  preflight: WorkflowPreflight | null
  checking: boolean
  error: string | null
}

export function useWorkflowInputs(templateId: string | null): WorkflowInputsState {
  const [values, setValues] = useState<Record<string, unknown>>({ sample: true })
  const [preflight, setPreflight] = useState<WorkflowPreflight | null>(null)
  const [checking, setChecking] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [refreshTick, setRefreshTick] = useState(0)
  const requestSeq = useRef(0)

  const reset = useCallback(() => {
    setValues({ sample: true })
    setPreflight(null)
    setError(null)
  }, [])

  const refresh = useCallback(() => setRefreshTick((n) => n + 1), [])

  // Switching templates starts over rather than carrying stale field values
  // (a customer id typed for one workflow has no business surviving into a
  // vendor-code workflow's form).
  useEffect(() => reset(), [templateId, reset])

  useEffect(() => {
    if (!templateId) return
    const seq = ++requestSeq.current
    setChecking(true)
    const timer = setTimeout(() => {
      getWorkflowPreflight(templateId, values)
        .then((result) => {
          if (requestSeq.current !== seq) return
          setPreflight(result)
          setError(null)
        })
        .catch((cause: unknown) => {
          if (requestSeq.current !== seq) return
          setPreflight(null)
          setError(cause instanceof Error ? cause.message : 'Could not check what this workflow needs.')
        })
        .finally(() => {
          if (requestSeq.current === seq) setChecking(false)
        })
    }, DEBOUNCE_MS)
    return () => clearTimeout(timer)
    // eslint-disable-next-line react-hooks/exhaustive-deps -- values is the intentional trigger; templateId re-runs via reset() above; refreshTick is a manual re-trigger only
  }, [templateId, values, refreshTick])

  const setValue = useCallback((name: string, value: unknown) => {
    setValues((prev) => ({ ...prev, [name]: value }))
  }, [])

  return { values, setValue, reset, refresh, preflight, checking, error }
}
