import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Button,
  Card,
  CodeBlock,
  Dropdown,
  EmptyState,
  Field,
  Input,
  SecretKey,
  Skeleton,
  Textarea,
  useToast,
  type DropdownOption,
} from '../components/ui'
import { humanize } from '../components/access/toolCardShared'
import { ApiError, getMyAccess, rotateMyKey, tryTool, type MyAccess, type ToolInfo } from '../lib/api'
import type { PageProps } from './types'
import './DeveloperPage.css'

/**
 * Developer — ports `renderDeveloper()`: the API key, an MCP connection
 * snippet, and a playground to run one governed tool call as yourself.
 *
 * The legacy version crammed all three into hand-rolled `<pre>` blocks and a
 * cramped inline `.grid`, with the raw canonical tool name (`get_customer_
 * orders`) as the only label in a bare `<select>` and its description
 * squeezed into one muted line underneath. Here each concern gets its own
 * Card with real breathing room, the snippet and the result both use the
 * kit's own `CodeBlock` (copy button built in, instead of a second
 * hand-wired "Copy snippet" button), the key uses the same masked
 * `SecretKey` reveal Access already does, and the tool picker is a
 * `Dropdown` with the humanized name as the label and the raw name +
 * description shown together below it, not one or the other.
 */

const CONNECTION_SNIPPET = `# Governance MCP endpoint
${location.origin}/mcp

# Authenticate with your API key (mint one below):
Authorization: Bearer <YOUR_API_KEY>`

interface PlaygroundTool {
  backend: string
  tool: ToolInfo
}

function DeveloperPage({ navigate }: PageProps) {
  const toast = useToast()

  const [account, setAccount] = useState<MyAccess | null>(null)
  const [state, setState] = useState<'loading' | 'ready' | 'error'>('loading')
  const [error, setError] = useState<string | null>(null)

  const [minting, setMinting] = useState(false)
  const [mintedKey, setMintedKey] = useState<string | null>(null)

  const [selectedTool, setSelectedTool] = useState('')
  const [customerId, setCustomerId] = useState('')
  const [argsText, setArgsText] = useState('{}')
  const [argsError, setArgsError] = useState<string | null>(null)
  const [running, setRunning] = useState(false)
  const [runError, setRunError] = useState<string | null>(null)
  const [result, setResult] = useState<string | null>(null)

  const load = useCallback(() => {
    let live = true
    setState('loading')

    getMyAccess()
      .then((access) => {
        if (!live) return
        setAccount(access)
        setError(null)
        setState('ready')
      })
      .catch((cause: unknown) => {
        if (!live) return
        setError(cause instanceof Error ? cause.message : 'Could not load your access.')
        setState('error')
      })

    return () => {
      live = false
    }
  }, [])

  useEffect(() => load(), [load])

  const tools = useMemo<PlaygroundTool[]>(() => {
    if (!account) return []
    const out: PlaygroundTool[] = []
    for (const [backend, grant] of Object.entries(account.access)) {
      for (const tool of grant.tools) out.push({ backend, tool })
    }
    return out
  }, [account])

  const toolOptions = useMemo<DropdownOption[]>(
    () => tools.map(({ tool }) => ({ value: tool.name, label: humanize(tool.name) })),
    [tools],
  )

  const activeTool = useMemo(() => tools.find((t) => t.tool.name === selectedTool) ?? null, [tools, selectedTool])

  // Default to the first granted tool once the list arrives, rather than
  // leaving the picker empty until the user opens it themselves.
  useEffect(() => {
    if (!selectedTool && tools.length > 0) setSelectedTool(tools[0].tool.name)
  }, [tools, selectedTool])

  const mintKey = useCallback(async () => {
    setMinting(true)
    try {
      const minted = await rotateMyKey()
      setMintedKey(minted.api_key)
      setAccount((prev) => (prev ? { ...prev, has_key: true } : prev))
      toast.success('Key minted — copy it now')
    } catch (cause) {
      toast.error(cause instanceof ApiError ? cause.message : 'Could not mint a key')
    } finally {
      setMinting(false)
    }
  }, [toast])

  const runTool = useCallback(async () => {
    if (!activeTool) return
    let args: Record<string, unknown>
    try {
      const parsed: unknown = argsText.trim() ? JSON.parse(argsText) : {}
      if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
        setArgsError('Arguments must be a JSON object')
        return
      }
      args = parsed as Record<string, unknown>
      setArgsError(null)
    } catch {
      setArgsError('Arguments must be valid JSON')
      return
    }

    setRunning(true)
    setRunError(null)
    setResult(null)
    try {
      const response = await tryTool(activeTool.tool.name, args, customerId)
      setResult(JSON.stringify(response.result, null, 2))
    } catch (cause) {
      setRunError(cause instanceof ApiError ? cause.message : 'Could not run that tool.')
    } finally {
      setRunning(false)
    }
  }, [activeTool, argsText, customerId])

  if (state === 'loading') {
    return (
      <div className="developer">
        <Skeleton height="8rem" index={0} />
        <Skeleton height="6rem" index={1} />
        <Skeleton height="14rem" index={2} />
      </div>
    )
  }

  if (state === 'error') {
    return (
      <div className="developer">
        <Card title="Couldn't load your access" accent="danger">
          <p className="developer-error-body">{error}</p>
          <Button variant="ghost" onClick={load}>
            Try again
          </Button>
        </Card>
      </div>
    )
  }

  return (
    <div className="developer">
      <Card
        title="API key"
        description={`${account?.has_key ? 'A key is issued.' : 'No key yet.'} Minting shows it once — store it safely; it replaces any previous key.`}
      >
        <div className="developer-stack">
          <div>
            <Button onClick={mintKey} disabled={account?.status !== 'active'} loading={minting}>
              {account?.has_key ? 'Rotate key' : 'Mint key'}
            </Button>
          </div>
          {mintedKey && (
            <SecretKey
              value={mintedKey}
              label="API key"
              hint="Copy it now — store it safely. Minting again replaces this key."
            />
          )}
        </div>
      </Card>

      <Card title="Connect an MCP client" description="Point any MCP client at the gateway with your key as a bearer token.">
        <CodeBlock label="Connection snippet" copyable wrap>
          {CONNECTION_SNIPPET}
        </CodeBlock>
      </Card>

      <Card
        title="Tool playground"
        description="Run a governed tool as yourself — full policy, scope, and redaction apply, exactly as an agent would see it."
      >
        {tools.length === 0 ? (
          <EmptyState
            title="You have no granted tools yet"
            description="Request access on My Access first."
            action={
              <Button size="sm" onClick={() => navigate('access')}>
                Go to My Access
              </Button>
            }
          />
        ) : (
          <div className="developer-stack">
            <Field label="Tool">
              {(fieldProps) => (
                <Dropdown {...fieldProps} value={selectedTool} onChange={setSelectedTool} options={toolOptions} />
              )}
            </Field>

            {activeTool && (
              <p className="developer-tool-desc">
                <span className="ui-mono">{activeTool.tool.name}</span> — {activeTool.tool.description}
              </p>
            )}

            <Field label="Customer ID" hint="Only if this tool is account-scoped.">
              {(fieldProps) => (
                <Input {...fieldProps} placeholder="optional" value={customerId} onChange={(e) => setCustomerId(e.target.value)} />
              )}
            </Field>

            <Field label="Arguments (JSON)" error={argsError ?? undefined}>
              {(fieldProps) => (
                <Textarea
                  {...fieldProps}
                  mono
                  rows={4}
                  value={argsText}
                  onChange={(e) => {
                    setArgsText(e.target.value)
                    setArgsError(null)
                  }}
                />
              )}
            </Field>

            <div>
              <Button onClick={runTool} loading={running}>
                Run tool
              </Button>
            </div>

            {runError && <p className="developer-run-error">{runError}</p>}

            {result && (
              <CodeBlock label="Result (governed & redacted for you)" copyable maxHeight="20rem">
                {result}
              </CodeBlock>
            )}
          </div>
        )}
      </Card>
    </div>
  )
}

export default DeveloperPage
