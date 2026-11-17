/**
 * brain 接口客户端（docs/03 §0；docs/05 §9.1）。
 * 全部接口走 /v1 前缀 + 一次性 token（壳主进程生成，经 preload 暴露给 renderer）。
 * 开发模式 brain 未注入 ELF_TOKEN 时 token 会被忽略（brain 不校验）。
 */
const TOKEN = window.elfShell?.token ?? ''

export const BRAIN = 'http://127.0.0.1:8787/v1'

/** 统一带上 token 的 fetch（body/headers 原样透传）。 */
export function brainFetch(path: string, init: RequestInit = {}): Promise<Response> {
  return fetch(`${BRAIN}${path}`, {
    ...init,
    headers: { ...(init.headers as Record<string, string> | undefined), 'X-Elf-Token': TOKEN },
  })
}

/** WS 下行通道（浏览器 WS 不能自定义头，token 走查询参数）。 */
export function brainWs(path: string): WebSocket {
  const query = TOKEN ? `?token=${encodeURIComponent(TOKEN)}` : ''
  return new WebSocket(`ws://127.0.0.1:8787/v1${path}${query}`)
}

/** §11.36 脑侧自选表情：LLM 按回复语气挑（normal/happy/pout/angry/lovely/surprised/shy）。 */
export type BrainMood =
  | 'normal'
  | 'happy'
  | 'pout'
  | 'angry'
  | 'lovely'
  | 'surprised'
  | 'shy'

export interface ChatReply {
  reply_text: string
  mood?: BrainMood
}

export interface PokeReply {
  kind: 'speak' | 'chat' | 'noop'
  reply_text?: string
  mood?: BrainMood
}
