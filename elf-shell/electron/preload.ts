import { contextBridge, ipcRenderer } from 'electron'

contextBridge.exposeInMainWorld('elfShell', {
  /** §9.1：brain 一次性 token（主进程生成；开发直连 brain 时 brain 不校验） */
  token: ipcRenderer.sendSync('elf-token') as string,
  /** hover 宠物/气泡时切换可交互；离开恢复穿透 */
  setInteractive: (on: boolean) => ipcRenderer.send('set-interactive', on),
  /** speak.* 等反馈事件上行 */
  sendEvent: (type: string, payload: Record<string, unknown>) =>
    ipcRenderer.send('shell-event', type, payload),
  /** 首次引导模式：放大居中窗口并取消穿透；完成恢复桌宠形态 */
  setOnboarding: (on: boolean) => ipcRenderer.send('set-onboarding', on),
  /** 拖动桌宠：窗口按屏幕坐标增量移动 */
  dragBy: (dx: number, dy: number) => ipcRenderer.send('drag-by', dx, dy),
  /** drag-1 投掷释放：快甩松手，主进程接管惯性运动（含边缘反弹） */
  dragRelease: (vx: number, vy: number) => ipcRenderer.send('drag-release', vx, vy),
  /** drag-1 落地/撞边冲击回叫 */
  onPetImpact: (cb: (impact: number) => void) =>
    ipcRenderer.on('pet-impact', (_e, impact) => cb(impact)),
  /** 退出应用（设置面板「退出」按钮；无边框窗口没有系统关闭按钮） */
  quit: () => ipcRenderer.send('elf-quit'),
})
