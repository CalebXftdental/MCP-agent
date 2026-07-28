/**
 * Streaming client for the governed assistant. Ports `streamOrChat()` from
 * static/app.html line-for-line in behavior: try SSE first, call `onUpdate` as
 * text arrives, and fall back to the plain `/chat` endpoint on ANY failure —
 * a dropped connection, a non-stream content-type, a mid-stream `error` event.
 *
 * This bypasses the `api.*` JSON wrapper deliberately: `request()` in api.ts
 * always awaits a full parsed body, which is exactly what streaming can't do.
 * Raw `fetch` + a manual reader is the one place in this app that talks to the
 * gateway directly.
 */

import { apiUrl, sendChat, type ChatStreamEvent, type ChatToolCall } from './api'

export interface StreamUpdate {
  text: string
  tools: string[]
  phase: 'tools' | 'answer'
}

export interface StreamResult {
  reply: string
  tools: ChatToolCall[]
  /** The server may rename the conversation (e.g. the caller's id had gone
   *  idle and been recycled) — always use this one going forward. */
  conversationId: string
}

/** Parses one `data: {...}\n\n` frame's JSON, matching the legacy client's
 *  `JSON.parse(dl.slice(5).trim())`. Returns null on a malformed frame instead
 *  of throwing, so one bad frame doesn't abort an otherwise-good stream. */
function parseFrame(block: string): ChatStreamEvent | null {
  const line = block.split('\n').find((l) => l.startsWith('data:'))
  if (!line) return null
  try {
    return JSON.parse(line.slice(5).trim()) as ChatStreamEvent
  } catch {
    return null
  }
}

async function streamChat(
  message: string,
  conversationId: string,
  onUpdate: (update: StreamUpdate) => void,
): Promise<StreamResult> {
  const res = await fetch(apiUrl('/chat/stream'), {
    method: 'POST',
    credentials: 'include',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ message, conversation_id: conversationId }),
  })

  const contentType = res.headers.get('content-type') ?? ''
  if (!res.ok || !res.body || !contentType.includes('text/event-stream')) {
    throw new Error('stream unavailable')
  }

  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let text = ''
  let tools: string[] = []
  let toolCalls: ChatToolCall[] = []
  let conv = conversationId
  let started = false

  for (;;) {
    const { value, done } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })

    let boundary: number
    while ((boundary = buffer.indexOf('\n\n')) >= 0) {
      const block = buffer.slice(0, boundary)
      buffer = buffer.slice(boundary + 2)
      const event = parseFrame(block)
      if (!event) continue

      switch (event.type) {
        case 'meta':
          conv = event.conversation_id
          break
        case 'delta':
          started = true
          text += event.text || ''
          onUpdate({ text, tools, phase: 'answer' })
          break
        case 'replace':
          text = event.text || ''
          onUpdate({ text, tools, phase: 'answer' })
          break
        case 'tools':
          tools = [...tools, ...(event.tools || [])]
          if (!started) onUpdate({ text: '', tools, phase: 'tools' })
          break
        case 'done':
          if (event.tool_calls?.length) {
            toolCalls = event.tool_calls
            tools = toolCalls.map((t) => t.tool)
          }
          break
        case 'error':
          throw new Error(event.message || 'stream error')
      }
    }
  }

  return { reply: text, tools: toolCalls, conversationId: conv }
}

/**
 * Streams a turn, calling `onUpdate` as text arrives; on any failure (network,
 * bad content-type, a mid-stream error frame) retries once through the plain
 * `/chat` endpoint, which records the turn itself. Never rejects for a
 * reachable gateway — the caller sees either a reply or a thrown ApiError from
 * the fallback (session expired, rate limited, etc).
 */
export async function streamOrChat(
  message: string,
  conversationId: string,
  onUpdate: (update: StreamUpdate) => void,
): Promise<StreamResult> {
  try {
    return await streamChat(message, conversationId, onUpdate)
  } catch {
    const result = await sendChat(message, conversationId)
    return {
      reply: result.reply || '',
      tools: result.tool_calls ?? [],
      conversationId: result.conversation_id || conversationId,
    }
  }
}
