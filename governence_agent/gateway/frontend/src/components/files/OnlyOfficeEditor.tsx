import { useEffect, useId, useRef, useState } from 'react'
import './OnlyOfficeEditor.css'

/**
 * Embeds ONLYOFFICE's Document Server editor/viewer — ports the legacy
 * panel's `_loadOnlyofficeApi()` + `new DocsAPI.DocEditor(...)` pair as a real
 * component with a lifecycle React can own, instead of a page-level function
 * juggling a mutable `ooEditor` variable and manual `destroyEditor()` calls
 * on every navigation.
 *
 * Two things carried over deliberately from the legacy version, because they
 * were fixes for real ONLYOFFICE quirks, not arbitrary choices:
 *   - The API script is loaded once per Document Server origin and cached
 *     (module-level, not per-mount) — re-injecting the same `<script>` on
 *     every artifact inspected was wasteful and occasionally raced.
 *   - `DocEditor` OWNS its mount element once created (it rebuilds the DOM
 *     inside it), so the div it's given needs a stable id for the whole
 *     component's life — this uses `useId()` for that rather than a
 *     hardcoded string, so more than one editor could theoretically exist
 *     without colliding.
 */

export interface OnlyOfficeEditorProps {
  documentServerUrl: string
  /** Opaque — handed straight to `DocsAPI.DocEditor`, never read into typed
   *  fields (see `OnlyOfficeConfig` in lib/api.ts). */
  config: Record<string, unknown>
  /** Remounts the editor when this changes — a different artifact or a new
   *  version needs a fresh document loaded, not the same editor mutated. */
  instanceKey: string
  height?: string
}

interface DocEditorInstance {
  destroyEditor?: () => void
}

interface DocsApi {
  DocEditor: new (elementId: string, config: Record<string, unknown>) => DocEditorInstance
}

declare global {
  interface Window {
    DocsAPI?: DocsApi
  }
}

const scriptLoads = new Map<string, Promise<DocsApi>>()

function loadDocsApi(documentServerUrl: string): Promise<DocsApi> {
  const src = `${documentServerUrl.replace(/\/+$/, '')}/web-apps/apps/api/documents/api.js`
  const cached = scriptLoads.get(src)
  if (cached) return cached

  const promise = new Promise<DocsApi>((resolve, reject) => {
    const script = document.createElement('script')
    script.src = src
    script.onload = () => {
      if (window.DocsAPI) resolve(window.DocsAPI)
      else reject(new Error(`ONLYOFFICE Document Server at ${documentServerUrl} did not respond as expected`))
    }
    script.onerror = () => reject(new Error(`Could not reach ONLYOFFICE Document Server at ${documentServerUrl}`))
    document.head.appendChild(script)
  })
  scriptLoads.set(src, promise)
  return promise
}

function OnlyOfficeEditor({ documentServerUrl, config, instanceKey, height = '24rem' }: OnlyOfficeEditorProps) {
  const mountId = useId().replace(/[^a-zA-Z0-9_-]/g, '')
  const editorRef = useRef<DocEditorInstance | null>(null)
  const [state, setState] = useState<'loading' | 'ready' | 'error'>('loading')
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let live = true
    setState('loading')

    loadDocsApi(documentServerUrl)
      .then((DocsAPI) => {
        if (!live) return
        editorRef.current = new DocsAPI.DocEditor(mountId, config)
        setState('ready')
      })
      .catch((cause: unknown) => {
        if (!live) return
        setError(cause instanceof Error ? cause.message : 'Could not load the document editor.')
        setState('error')
      })

    return () => {
      live = false
      try {
        editorRef.current?.destroyEditor?.()
      } catch {
        // ONLYOFFICE occasionally throws tearing down an editor that never
        // finished initialising — never worth surfacing over the page it was
        // embedded in.
      }
      editorRef.current = null
    }
    // `config`/`mountId` deliberately excluded: this should remount only when
    // the caller says the underlying document changed (`instanceKey`) or the
    // server moved, not on every re-render of the parent that happens to
    // recreate the same config object.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [documentServerUrl, instanceKey])

  return (
    <div className="oo-editor" style={{ height }}>
      {state === 'error' ? (
        <p className="oo-editor-error">{error}</p>
      ) : (
        <>
          {state === 'loading' && <div className="oo-editor-loading">Loading document editor…</div>}
          <div id={mountId} className="oo-editor-mount" />
        </>
      )}
    </div>
  )
}

export default OnlyOfficeEditor
