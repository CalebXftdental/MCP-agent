import { useCallback, useState } from 'react'
import Button from './Button'
import Chip from './Chip'
import CidrInput from './CidrInput'
import Field, { Textarea } from './Field'
import { useToast } from './toast-context'
import { validateCidr } from '../../lib/cidr'
import './CidrListEditor.css'

/**
 * A typed-entry + removable-chip editor for a list of CIDRs/IPs — the add
 * row and tray `WhitelistPage` first built for the global allowlist, pulled
 * out so any per-principal `ip_allowlist` field (Consumers' create form and
 * manage drawer) can use the same validated, chip-based entry instead of a
 * bare textarea or a second hand-rolled copy of this logic.
 *
 * Fully controlled (`value`/`onChange`) and save-agnostic — it has no
 * dirty/saved distinction or confirm step of its own, since "does adding an
 * entry here lock anyone out" differs by caller (the global list's first
 * entry gates every request; a consumer's own list only ever narrows that
 * one consumer). Callers that need that framing (see `WhitelistPage`) diff
 * `value` against their own last-saved copy and layer a confirm step around
 * this component; callers that don't (Consumers) just fold `value` into
 * whatever save action already exists for the rest of that form.
 *
 * "Paste multiple" is a second, textarea-based entry path alongside the
 * one-at-a-time `CidrInput` row — for pasting a block of IPs someone handed
 * you (a NAT gateway's public IP list, a security team's CIDR block) instead
 * of retyping each into four octet boxes. Splits on commas/whitespace/
 * newlines so it tolerates however the source formatted it, validates each
 * entry the same way the single-entry row does, and reports what happened
 * (added vs. skipped) rather than silently dropping anything invalid.
 */

export interface CidrListEditorProps {
  value: string[]
  onChange: (next: string[]) => void
  emptyHint?: string
  disabled?: boolean
}

function CidrListEditor({
  value,
  onChange,
  emptyHint = 'No entries — every IP is allowed.',
  disabled = false,
}: CidrListEditorProps) {
  const toast = useToast()

  const [inputValue, setInputValue] = useState('')
  const [inputError, setInputError] = useState<string | null>(null)

  const [bulkOpen, setBulkOpen] = useState(false)
  const [bulkText, setBulkText] = useState('')

  const addEntry = useCallback(() => {
    const entry = inputValue.trim()
    const validationError = validateCidr(entry)
    if (validationError) {
      setInputError(validationError)
      return
    }
    if (value.some((v) => v.toLowerCase() === entry.toLowerCase())) {
      setInputError('Already on the list')
      return
    }
    onChange([...value, entry])
    setInputValue('')
    setInputError(null)
  }, [inputValue, value, onChange])

  const removeEntry = useCallback(
    (cidr: string) => {
      onChange(value.filter((v) => v !== cidr))
    },
    [value, onChange],
  )

  const addBulk = useCallback(() => {
    const candidates = bulkText.split(/[\s,]+/).map((c) => c.trim()).filter(Boolean)
    if (candidates.length === 0) return

    const seen = new Set(value.map((v) => v.toLowerCase()))
    const added: string[] = []
    const skipped: string[] = []
    for (const candidate of candidates) {
      if (validateCidr(candidate) || seen.has(candidate.toLowerCase())) {
        skipped.push(candidate)
        continue
      }
      seen.add(candidate.toLowerCase())
      added.push(candidate)
    }

    if (added.length > 0) onChange([...value, ...added])
    if (skipped.length > 0) {
      toast.warn(
        added.length > 0
          ? `Added ${added.length}, skipped ${skipped.length} (invalid or already on the list): ${skipped.join(', ')}`
          : `Skipped ${skipped.length} (invalid or already on the list): ${skipped.join(', ')}`,
        { durationMs: null },
      )
    } else {
      toast.success(`Added ${added.length}`)
    }
    setBulkText('')
    if (added.length > 0 || skipped.length === 0) setBulkOpen(false)
  }, [bulkText, value, onChange, toast])

  return (
    <div className="ui-cidrlist">
      <Field label="Add a CIDR or IP" hint="Leave the prefix blank for a single address — press Enter or Add." error={inputError ?? undefined}>
        {(fieldProps) => (
          <div className="ui-cidrlist-add-row">
            <CidrInput
              {...fieldProps}
              value={inputValue}
              onChange={(next) => {
                setInputValue(next)
                setInputError(null)
              }}
              onSubmit={addEntry}
              disabled={disabled}
            />
            <Button size="sm" onClick={addEntry} disabled={disabled}>
              Add
            </Button>
            <Button size="sm" variant="ghost" onClick={() => setBulkOpen((open) => !open)} disabled={disabled}>
              Paste multiple
            </Button>
          </div>
        )}
      </Field>

      {bulkOpen && (
        <Field
          label="Paste a list"
          hint="One per line, or separated by commas/spaces — each is validated the same way as the single-entry row above."
        >
          {(fieldProps) => (
            <div className="ui-cidrlist-bulk">
              <Textarea
                {...fieldProps}
                mono
                rows={4}
                placeholder={'203.0.113.5\n198.51.100.0/24, 192.0.2.10'}
                value={bulkText}
                onChange={(e) => setBulkText(e.target.value)}
                disabled={disabled}
              />
              <div className="ui-cidrlist-bulk-actions">
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => {
                    setBulkOpen(false)
                    setBulkText('')
                  }}
                  disabled={disabled}
                >
                  Cancel
                </Button>
                <Button size="sm" onClick={addBulk} disabled={disabled || !bulkText.trim()}>
                  Add all
                </Button>
              </div>
            </div>
          )}
        </Field>
      )}

      <div className="ui-cidrlist-tray">
        {value.length === 0 ? (
          <p className="ui-cidrlist-empty">{emptyHint}</p>
        ) : (
          <div className="ui-cidrlist-chips">
            {value.map((cidr) => (
              <span className="ui-cidrlist-chip" key={cidr}>
                <Chip size="sm" onRemove={disabled ? undefined : () => removeEntry(cidr)} removeLabel={`Remove ${cidr}`}>
                  <span className="ui-mono">{cidr}</span>
                </Chip>
              </span>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

export default CidrListEditor
