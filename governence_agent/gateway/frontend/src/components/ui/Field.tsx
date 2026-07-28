import { useId } from 'react'
import type {
  InputHTMLAttributes,
  ReactNode,
  SelectHTMLAttributes,
  TextareaHTMLAttributes,
} from 'react'
import './Field.css'

/**
 * Labelled form controls.
 *
 * The old UI wrapped controls in a bare `<label>` — which associates the label
 * correctly, but left hints and errors as unrelated siblings. A wrong value was
 * reported by a red `.err` div somewhere nearby that no screen reader connected
 * to the input. Field wires `htmlFor`, `aria-describedby` and `aria-invalid`, so
 * the error is read out when focus lands on the offending control.
 */

export interface FieldProps {
  label: ReactNode
  /** Receives the id to put on the control. */
  children: (props: {
    id: string
    'aria-describedby': string | undefined
    'aria-invalid': true | undefined
  }) => ReactNode
  /** Standing guidance. Always visible. */
  hint?: ReactNode
  /** Validation failure. Replaces the hint and turns the control red. */
  error?: ReactNode
  required?: boolean
  /** Side-by-side label and control, for dense filter bars. */
  inline?: boolean
}

export function Field({ label, children, hint, error, required, inline = false }: FieldProps) {
  const id = useId()
  const hintId = `${id}-hint`
  const errorId = `${id}-error`
  const describedBy = error ? errorId : hint ? hintId : undefined

  return (
    <div className={`ui-field ${inline ? 'ui-field--inline' : ''}`}>
      <label className="ui-field-label" htmlFor={id}>
        {label}
        {required && (
          <>
            <span className="ui-field-req" aria-hidden="true">
              *
            </span>
            <span className="ui-sr-only">(required)</span>
          </>
        )}
      </label>

      <div className="ui-field-control">
        {children({
          id,
          'aria-describedby': describedBy,
          'aria-invalid': error ? true : undefined,
        })}
      </div>

      {/* aria-live so a validation message that appears after submit is
          announced, not just silently painted. */}
      {error ? (
        <p className="ui-field-error" id={errorId} aria-live="polite">
          {error}
        </p>
      ) : (
        hint && (
          <p className="ui-field-hint" id={hintId}>
            {hint}
          </p>
        )
      )}
    </div>
  )
}

export type InputProps = InputHTMLAttributes<HTMLInputElement>

export function Input({ className = '', ...rest }: InputProps) {
  return <input {...rest} className={`ui-input ${className}`} />
}

export type SelectProps = SelectHTMLAttributes<HTMLSelectElement>

export function Select({ className = '', children, ...rest }: SelectProps) {
  return (
    <select {...rest} className={`ui-input ui-select ${className}`}>
      {children}
    </select>
  )
}

export interface TextareaProps extends TextareaHTMLAttributes<HTMLTextAreaElement> {
  /** Monospace, for JSON argument editors. On by default — every textarea in
   *  the old UI was monospace, because they all hold JSON or code. */
  mono?: boolean
}

export function Textarea({ className = '', mono = true, rows = 4, ...rest }: TextareaProps) {
  return (
    <textarea {...rest} rows={rows} className={`ui-input ui-textarea ${mono ? 'ui-mono' : ''} ${className}`} />
  )
}

export default Field
