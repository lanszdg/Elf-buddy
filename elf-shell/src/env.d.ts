/// <reference types="vite/client" />

interface ElfShell {
  /** §9.1：brain 一次性 token（主进程生成，HTTP 头/WS 查询参数用） */
  token: string
  setInteractive(on: boolean): void
  sendEvent(type: string, payload: Record<string, unknown>): void
  setOnboarding(on: boolean): void
  dragBy(dx: number, dy: number): void
  /** drag-1 投掷释放：快甩松手时把速度（px/s）交给主进程做惯性运动 */
  dragRelease(vx: number, vy: number): void
  /** drag-1 落地/撞边冲击订阅：主进程惯性运动期间回叫（impact 0.1~0.5） */
  onPetImpact(cb: (impact: number) => void): void
  quit(): void
  openInterests(section: string): void
  onCompanionOpen(callback: () => void): () => void
  openInterestLink(url: string): Promise<void>
  applyAvatar(value: string): void
  onAvatar(callback: (value: string) => void): () => void
  onInterestSection(callback: (section: string) => void): () => void
}

interface Window {
  elfShell: ElfShell
}

declare module '*.vue' {
  import type { DefineComponent } from 'vue'
  const component: DefineComponent<{}, {}, any>
  export default component
}
