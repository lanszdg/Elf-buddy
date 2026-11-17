import { brainFetch } from '../brain'

export type Kind = 'practice' | 'photo' | 'pick' | 'event' | 'research' | 'handoff' | 'watch' | 'release'
export interface Item {
  id: string
  kind: Kind
  title: string
  data: Record<string, unknown>
  created: number
  due: number | null
  done: boolean
}

export async function interestApi<T>(path: string, method = 'GET', body?: unknown): Promise<T> {
  const response = await brainFetch('/interest' + path, {
    method,
    headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  if (!response.ok) {
    const info = await response.json().catch(() => null)
    throw new Error(typeof info?.detail === 'string' ? info.detail : '请求失败（' + response.status + '）')
  }
  return response.status === 204 ? undefined as T : await response.json() as T
}

export function saveInterest(kind: Kind, title: string, data: Item['data'], due?: number): Promise<Item> {
  return interestApi('/items', 'POST', { kind, title, data, due })
}

export function httpUrl(value: unknown): string {
  if (typeof value !== 'string' || !value) return ''
  try {
    const url = new URL(value.trim())
    if ((url.protocol === 'https:' || url.protocol === 'http:') && !url.username && !url.password) return url.href
  } catch { /* 无法打开的不形成链接 */ }
  return ''
}

export function dateText(seconds: number | null | undefined): string {
  return typeof seconds === 'number' ? new Date(seconds * 1000).toLocaleString('zh-CN', {
    month: 'long', day: 'numeric', hour: '2-digit', minute: '2-digit',
  }) : ''
}

export function download(name: string, text: string, mime = 'text/plain;charset=utf-8') {
  const url = URL.createObjectURL(new Blob([text], { type: mime }))
  const link = document.createElement('a')
  link.href = url
  link.download = name
  link.click()
  window.setTimeout(() => URL.revokeObjectURL(url), 1000)
}

export function defaultDue(minutes = 30): string {
  const date = new Date(Date.now() + minutes * 60_000)
  return new Date(date.getTime() - date.getTimezoneOffset() * 60_000).toISOString().slice(0, 16)
}
