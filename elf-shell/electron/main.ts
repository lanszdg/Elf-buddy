/**
 * Electron 主进程（docs/03 §1：壳的职责=呈现+感知+执行，不做决策）。
 * 透明无边框常驻窗 + 默认点击穿透 + 空闲检测上行。
 *
 * 分发形态：生产模式下壳托管大脑——启动时 spawn 打包好的 elf-brain.exe
 * （试用者机器零 Python 依赖），退出时回收子进程。
 */
import { app, BrowserWindow, globalShortcut, ipcMain, net, powerMonitor, protocol, screen, shell } from 'electron'
import { spawn, execFile, ChildProcess } from 'node:child_process'
import { randomBytes } from 'node:crypto'
import path from 'node:path'
import { pathToFileURL } from 'node:url'
import fs from 'node:fs'
import { interestTools, isInterestTool, type InterestTool } from '../src/interest/toolRoutes'

// §9.1：brain 全部接口走 /v1 + 一次性 token（本进程生成，拉起 brain 时经环境变量传入；
// renderer 经 preload 拿到同一 token）。开发模式 brain 由 start.ps1 单独启动，不校验。
const BRAIN_TOKEN = process.env.ELF_TOKEN || randomBytes(24).toString('hex')
const BRAIN_EVENTS = 'http://127.0.0.1:8787/v1/events'
const isDev = !!process.env.VITE_DEV_SERVER_URL
// 桌宠窗口尺寸（渲染层布局按此比例：底部 38% 精灵，上方 62% 卡片）
const PET_W = 400
const PET_H = 600
// dev 日志管道：renderer console + 主进程关键事件落盘到项目内。用户经 start.ps1 弹窗跑壳时
// 输出不在 IDE 终端，事后排查全靠这个文件；生产 isDev=false 恒不写。
const DEV_LOG = isDev ? path.join(__dirname, '..', 'dev-renderer.log') : null
function devLog(msg: string): void {
  if (!DEV_LOG) return
  try {
    fs.appendFileSync(DEV_LOG, `[${new Date().toISOString()}] ${msg}\n`)
  } catch {
    // 日志管道失败不阻断运行
  }
}
let win: BrowserWindow | null = null
let interestWindow: BrowserWindow | null = null
let brainProc: ChildProcess | null = null
let quitting = false

// 单实例锁（docs/05 §11.32）：重复启动不能再起新实例——第二个实例会再拉一个 brain 撞 8787 端口必死，
// 它的 renderer 连不上脑 → 用户看到精灵「自己退了」。抢不到锁直接退；第二次启动则聚焦已有窗口。
const gotSingleInstanceLock = app.requestSingleInstanceLock()
if (!gotSingleInstanceLock) {
  app.quit()
} else {
  app.on('second-instance', () => {
    if (win) {
      if (win.isMinimized()) win.restore()
      win.show()
      win.focus()
    }
  })
}

// 数据目录唯一化：Electron 默认按 package.json 的 name（elf-shell）建 userData，
// 与 electron-builder 的 productName（Elf Buddy）不一致，历史版本因此把数据写成了两处。
// 这里显式钉死在 %APPDATA%\Elf Buddy，Electron 缓存与 brain 数据都落在同一棵树里。
// ELF_USER_DATA 供开发态把数据引到项目内（Trae 沙箱禁止写 %APPDATA% 时仍能起壳）。
const USER_DATA = process.env.ELF_USER_DATA
  ? path.resolve(process.env.ELF_USER_DATA)
  : path.join(app.getPath('appData'), 'Elf Buddy')
const LEGACY_USER_DATA = path.join(app.getPath('appData'), 'elf-shell')
app.setPath('userData', USER_DATA)

/** 旧目录数据搬家：只补缺，不覆盖、不删除（用户可随时回退）。 */
function migrateLegacyData(): void {
  const legacy = path.join(LEGACY_USER_DATA, 'data')
  const target = path.join(USER_DATA, 'data')
  if (!fs.existsSync(legacy)) return
  fs.mkdirSync(target, { recursive: true })
  let moved = 0
  for (const name of fs.readdirSync(legacy)) {
    const dst = path.join(target, name)
    if (fs.existsSync(dst)) continue // 新目录已有的一律不动
    try {
      fs.cpSync(path.join(legacy, name), dst, { recursive: true, force: false })
      moved += 1
    } catch (e) {
      console.warn('[migrate] skipped', name, e)
    }
  }
  if (moved) console.log(`[migrate] ${moved} 项从 ${legacy} 迁到 ${target}`)
}

// 自定义协议 elf://model/... → Live2D 模型目录（开发=项目 models/，生产=resources/models/）
// renderer 以 fetch 加载模型；特权注册必须在 app ready 前
protocol.registerSchemesAsPrivileged([
  { scheme: 'elf', privileges: { standard: true, secure: true, supportFetchAPI: true, stream: true } },
])

function modelsDir(): string {
  return isDev ? path.join(__dirname, '../models') : path.join(process.resourcesPath, 'models')
}

/** 生产模式：启动大脑子进程，数据目录=userData/data（可写） */
function startBrain() {
  if (isDev) return // 开发模式 brain 由 start.ps1 单独启动
  const brainExe = path.join(process.resourcesPath, 'brain', 'elf-brain.exe')
  if (!fs.existsSync(brainExe)) {
    console.error('[brain] not found:', brainExe)
    return
  }
  brainProc = spawn(brainExe, [], {
    env: {
      ...process.env,
      ELF_DATA_DIR: path.join(app.getPath('userData'), 'data'),
      ELF_TOKEN: BRAIN_TOKEN,
    },
    windowsHide: true,
  })
  brainProc.stdout?.on('data', (d) => console.log('[brain]', d.toString().trim()))
  brainProc.stderr?.on('data', (d) => console.error('[brain]', d.toString().trim()))
  brainProc.on('exit', (code) => {
    console.log('[brain] exited:', code)
    brainProc = null
    // §5.2 清空重来：brain 抹数据后以 0 退出 → 自动拉起（renderer 同步 reload 回引导）
    if (code === 0 && !quitting) setTimeout(startBrain, 1000)
  })
}

function stopBrain() {
  brainProc?.kill()
  brainProc = null
}

function showInterests(section: InterestTool) {
  const display = screen.getDisplayNearestPoint(screen.getCursorScreenPoint())
  const tool = interestTools[section]
  const width = Math.min(tool.width, display.workArea.width - 32)
  const height = Math.min(tool.height, display.workArea.height - 32)
  const bounds = {
    width,
    height,
    x: Math.round(display.workArea.x + (display.workArea.width - width) / 2),
    y: Math.round(display.workArea.y + (display.workArea.height - height) / 2),
  }
  if (interestWindow && !interestWindow.isDestroyed()) {
    if (interestWindow.isMinimized()) interestWindow.restore()
    if (!interestWindow.isMaximized()) interestWindow.setBounds(bounds)
    interestWindow.show()
    interestWindow.focus()
    interestWindow.webContents.send('interest-section', section)
    return
  }
  const active = new BrowserWindow({
    ...bounds, minWidth: Math.min(500, width), minHeight: Math.min(420, height),
    title: tool.name + ' · Elf Buddy', backgroundColor: '#f7f4ef', autoHideMenuBar: true,
    webPreferences: { preload: path.join(__dirname, 'preload.js'), sandbox: true, contextIsolation: true },
  })
  interestWindow = active
  active.once('closed', () => { if (interestWindow === active) interestWindow = null })
  active.webContents.setWindowOpenHandler(() => ({ action: 'deny' }))
  active.webContents.on('will-navigate', (event) => event.preventDefault())
  const query = { view: 'interests', section }
  if (process.env.VITE_DEV_SERVER_URL) {
    const url = new URL(process.env.VITE_DEV_SERVER_URL)
    for (const [key, value] of Object.entries(query)) url.searchParams.set(key, value)
    void active.loadURL(url.href)
  } else {
    void active.loadFile(path.join(__dirname, '../dist/index.html'), { query })
  }
}

function createWindow() {
  // 卡片要放封面+标题+口播稿+操作区，260×320 里必然挤成小字堆——
  // 放大到 400×600：上半 62% 给卡片，下半留精灵本体（仍守「气泡不遮脸」）
  const W = PET_W
  const H = PET_H
  const MARGIN = 24
  // 默认落位工作区右下角（workArea 已排除任务栏），桌宠不挡主工作区
  const area = screen.getPrimaryDisplay().workArea
  win = new BrowserWindow({
    width: W,
    height: H,
    x: area.x + area.width - W - MARGIN,
    y: area.y + area.height - H - MARGIN,
    transparent: true,
    frame: false,
    alwaysOnTop: true,
    skipTaskbar: true,
    hasShadow: false,
    resizable: false,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      // 桌宠窗常年无焦点：关掉后台节流，否则 rAF 被压 → 呼吸/眨眼等动画停帧
      backgroundThrottling: false,
    },
  })
  // 默认穿透（不打断用户工作）；渲染进程 hover 宠物/气泡时切回可交互
  win.setIgnoreMouseEvents(true, { forward: true })
  // Windows 透明窗的 WS_EX_TOPMOST 有运行时丢失场景（层级被全屏/其他置顶窗压掉后
  // 窗口看似"消失"，实测 GWL_EXSTYLE=0x80020 缺 0x8）。构造选项之外显式断言一次，
  // 并在 set-interactive / set-onboarding 等窗口属性变更点后统一 re-assert。
  win.setAlwaysOnTop(true)
  if (isDev) {
    // renderer 的报错/日志转发到主进程终端（透明窗崩溃排查）+ 落盘 dev-renderer.log
    win.webContents.on('console-message', (_e, _level, message) => {
      console.log('[renderer]', message)
      devLog(`[renderer] ${message}`)
    })
  }
  if (process.env.VITE_DEV_SERVER_URL) {
    win.loadURL(process.env.VITE_DEV_SERVER_URL)
  } else {
    win.loadFile(path.join(__dirname, '../dist/index.html'))
  }
}

/** 上行事件（docs/03 §2.1）。失败静默丢弃，不阻塞壳。 */
async function postEvent(type: string, payload: Record<string, unknown>) {
  try {
    await fetch(BRAIN_EVENTS, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Elf-Token': BRAIN_TOKEN },
      body: JSON.stringify({ type, ts: Date.now(), source: 'shell', payload }),
    })
  } catch { /* brain 未就绪时静默 */ }
}

/** WS_EX_TOPMOST 丢失 + DWM 停合 双重看护（docs/05 §11.32 的补充兜底）：
 *  ① 全屏放映（PowerPoint 幻灯片）、UAC 提权、某些独占式全屏游戏会把透明窗的置顶打掉；
 *  ② 同一类全屏进出还会让 DWM 不再把透明窗合成上屏——窗口句柄、TOPMOST、渲染进程全正常，
 *     屏幕上就是没有内容（已实测：GDI 截屏窗口区域只见下层窗）。
 *  事件点 re-assert 覆盖不到这两种外部击落，改为 15s 周期幂等补钉：补置顶 + ±1px 交替位移。
 *  1px 真实位移可踢醒 DWM 重新合成（等价手动 SetWindowPos + SWP_FRAMECHANGED 的验证结论）。 */
/** DWM 补钉：补置顶 + ±1px 交替位移。1px 真实位移可踢醒 DWM 重新合成透明窗。 */
let nudged = false
function kickDwm() {
  if (win && !win.isDestroyed()) {
    win.setAlwaysOnTop(true)
    const [x, y] = win.getPosition()
    win.setPosition(x + (nudged ? -1 : 1), y)
    nudged = !nudged
  }
}

function startTopmostWatchdog() {
  setInterval(kickDwm, 15000)
}

/** 空闲检测（docs/03 §3.1）：5s 轮询系统空闲秒数。 */
function startIdleLoop() {
  let last = -1
  setInterval(() => {
    const idle = powerMonitor.getSystemIdleTime()
    // 只在状态语义变化时上报（活跃→间隙→空闲），节流
    const bucket = idle < 10 ? 0 : idle < 180 ? 1 : 2
    if (bucket !== last) {
      last = bucket
      postEvent('perception.idle_change', { idle_sec: idle })
    }
  }, 5000)
}

/**
 * 前台应用采集（docs/01 §M2）：5s 轮询前台窗口进程名 → 泛化类别上行。
 * 零依赖方案：spawn powershell Add-Type 内联 Win32 API（避免 active-win 的 node-gyp 原生编译）。
 * 隐私红线：进程名在壳侧映射为类别后即丢弃，窗口标题原文不出壳、不取。
 */
const APP_CLASS: Record<string, string> = {
  code: 'code', cursor: 'code', devenv: 'code', idea64: 'idea',
  winword: 'word', excel: 'excel', wps: 'wpp', wpp: 'wpp', et: 'wpp',
  notion: 'notion', feishu: 'feishu', dingtalk: 'dingtalk',
  zoom: 'zoom', teams: 'teams', 'ms-teams': 'teams',
  wemeetapp: 'tencent meeting', voov: 'voov',
  chrome: 'browser', msedge: 'browser', firefox: 'browser',
}

// PS 5.1 对 -Command 内嵌引号的命令行解析有 bug（\" 会漏进 C# 源码），
// 一律走 -EncodedCommand（base64 UTF-16LE）彻底绕开引号转义
const FOREGROUND_PS = [
  `Add-Type -MemberDefinition '[DllImport("user32.dll")] public static extern System.IntPtr GetForegroundWindow(); [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(System.IntPtr h, out uint p);' -Name Fg -Namespace W`,
  '$h=[W.Fg]::GetForegroundWindow(); $p=0',
  '[W.Fg]::GetWindowThreadProcessId($h,[ref]$p) | Out-Null',
  '(Get-Process -Id $p).ProcessName',
].join('; ')

/** 截图/录屏工具：拉起时会把全屏覆盖层顶到最前，DWM 随即停掉透明桌宠窗的合成
 *  （屏幕上直接没有内容，用户视角="精灵没了"）。进程白名单见下方检测。 */
const CAPTURE_PROCS = new Set([
  'screenclippinghost', 'snippingtool', 'video.ui', 'sharex', // 系统截图/录屏
  'qq', 'wechat', 'weixin', // 热键截图时覆盖层挂在这俩进程下（日常聊天误触发仅一次无害补钉）
])

function startForegroundLoop() {
  let lastClass = ''
  let lastProc = ''
  const encoded = Buffer.from(FOREGROUND_PS, 'utf16le').toString('base64')
  setInterval(() => {
    execFile(
      'powershell',
      ['-NoProfile', '-NonInteractive', '-EncodedCommand', encoded],
      { timeout: 4000, windowsHide: true },
      (err, stdout) => {
        if (err) return // 采集失败静默，不打断
        const proc = stdout.trim().toLowerCase()
        if (!proc) return
        // 截图工具进/出前台瞬间立即踢 DWM（不等 15s 看门狗），精灵"消失"秒回
        const capturing = CAPTURE_PROCS.has(proc)
        if (capturing !== CAPTURE_PROCS.has(lastProc)) kickDwm()
        lastProc = proc
        const appClass = APP_CLASS[proc] ?? 'other'
        if (appClass !== lastClass) {
          lastClass = appClass
          postEvent('perception.foreground_change', { app: proc, app_class: appClass })
        }
      }
    )
  }, 5000)
}

/** 锁屏/解锁（docs/03 §3.1）：locked=false 让状态机退出 AWAY。 */
function startScreenLockWatch() {
  powerMonitor.on('lock-screen', () => postEvent('perception.screen_lock', { locked: true }))
  powerMonitor.on('unlock-screen', () => postEvent('perception.screen_lock', { locked: false }))
}

app.whenReady().then(() => {
  // 模型资源协议：elf://model/<相对路径>
  protocol.handle('elf', (req) => {
    const rel = decodeURIComponent(req.url.replace(/^elf:\/\/model\/?/, ''))
    return net.fetch(pathToFileURL(path.join(modelsDir(), rel)).toString())
  })
  ipcMain.on('elf-token', (e) => {
    e.returnValue = BRAIN_TOKEN // preload 同步取 token（§9.1）
  })
  if (!isDev) migrateLegacyData() // 旧 elf-shell 目录数据一次搬家（开发态由 start.ps1 管数据目录）
  startBrain()
  createWindow()
  globalShortcut.register('CommandOrControl+Shift+Space', () => {
    if (!win || win.isDestroyed()) return
    win.show()
    win.focus()
    win.webContents.send('companion-open')
  })
  ipcMain.on('interest-open', (event, section: unknown) => {
    if (event.sender === win?.webContents && isInterestTool(section)) showInterests(section)
  })
  ipcMain.handle('interest-external', async (event, value: string) => {
    if (event.sender !== interestWindow?.webContents && event.sender !== win?.webContents) throw new Error('Invalid window')
    const url = new URL(value)
    if (!['https:', 'http:'].includes(url.protocol) || url.username || url.password) throw new Error('Invalid URL')
    await shell.openExternal(url.href)
  })
  ipcMain.on('interest-avatar', (event, value: string) => {
    if (event.sender === interestWindow?.webContents && typeof value === 'string' && value.length <= 650000) {
      win?.webContents.send('interest-avatar-changed', value)
    }
  })
  startIdleLoop()
  startForegroundLoop()
  startScreenLockWatch()
  startTopmostWatchdog()
  ipcMain.on('set-interactive', (_e, on: boolean) => {
    win?.setIgnoreMouseEvents(!on, { forward: true })
    // 穿透切换会动窗口 exstyle，随后 re-assert 置顶，防 WS_EX_TOPMOST 丢失
    win?.setAlwaysOnTop(true)
  })
  ipcMain.on('shell-event', (_e, type: string, payload: Record<string, unknown>) => {
    postEvent(type, payload)
  })
  // 设置面板「退出」：无边框桌宠窗口没有系统关闭按钮，由渲染层显式请求
  ipcMain.on('elf-quit', () => {
    console.log('[quit] 主进程收到 elf-quit')
    devLog('[quit] 主进程收到 elf-quit')
    app.quit()
  })
  let tossTimer: NodeJS.Timeout | null = null
  // 拖动桌宠：按屏幕坐标增量移动窗口
  ipcMain.on('drag-by', (_e, dx: number, dy: number) => {
    if (!win) return
    if (tossTimer) {
      clearInterval(tossTimer)
      tossTimer = null
    }
    const [x, y] = win.getPosition()
    win.setPosition(Math.round(x + dx), Math.round(y + dy))
  })
  // drag-1 投掷惯性：renderer 快甩松手把速度（px/s）交过来，主进程 60fps 积分运动——
  // 撞工作区边缘反弹（阻尼 0.55），速度衰减到阈值停住，全程回叫冲击给精灵做落地挤压
  ipcMain.on('drag-release', (_e, vx: number, vy: number) => {
    if (!win) return
    const speed0 = Math.hypot(vx, vy)
    if (speed0 < 240) return // 慢松手就是普通放下， skin-1 弹簧回正已够
    if (tossTimer) clearInterval(tossTimer)
    const area = screen.getDisplayMatching(win.getBounds()).workArea
    const [w, h] = win.getSize()
    let [x, y] = win.getPosition()
    const impactOf = (s: number) => Math.min(0.5, Math.max(0.1, s / 1500))
    tossTimer = setInterval(() => {
      if (!win) {
        if (tossTimer) clearInterval(tossTimer)
        tossTimer = null
        return
      }
      vx *= 0.985
      vy *= 0.985
      let nx = x + (vx * 16) / 1000
      let ny = y + (vy * 16) / 1000
      let hitSpeed = 0
      if (nx < area.x) {
        nx = area.x
        hitSpeed = Math.abs(vx)
        vx = Math.abs(vx) * 0.55
      } else if (nx + w > area.x + area.width) {
        nx = area.x + area.width - w
        hitSpeed = Math.abs(vx)
        vx = -Math.abs(vx) * 0.55
      }
      if (ny < area.y) {
        ny = area.y
        hitSpeed = Math.max(hitSpeed, Math.abs(vy))
        vy = Math.abs(vy) * 0.55
      } else if (ny + h > area.y + area.height) {
        ny = area.y + area.height - h
        hitSpeed = Math.max(hitSpeed, Math.abs(vy))
        vy = -Math.abs(vy) * 0.55
      }
      if (hitSpeed > 0) win.webContents.send('pet-impact', impactOf(hitSpeed))
      x = nx
      y = ny
      win.setPosition(Math.round(x), Math.round(y))
      if (Math.hypot(vx, vy) < 30) {
        if (tossTimer) clearInterval(tossTimer)
        tossTimer = null
        win.webContents.send('pet-impact', impactOf(speed0)) // 落地一击按出手力度结算
      }
    }, 16)
  })
  // 首次引导：窗口临时放大居中并取消穿透；完成后恢复桌宠形态
  ipcMain.on('set-onboarding', (_e, on: boolean) => {
    if (!win) return
    if (on) {
      win.setIgnoreMouseEvents(false)
      const area = screen.getPrimaryDisplay().workArea
      const W = 520
      const H = 640
      win.setBounds({
        x: Math.round(area.x + (area.width - W) / 2),
        y: Math.round(area.y + (area.height - H) / 2),
        width: W,
        height: H,
      })
    } else {
      const W = PET_W
      const H = PET_H
      const MARGIN = 24
      const area = screen.getPrimaryDisplay().workArea
      win.setBounds({
        x: area.x + area.width - W - MARGIN,
        y: area.y + area.height - H - MARGIN,
        width: W,
        height: H,
      })
      win.setIgnoreMouseEvents(true, { forward: true })
    }
    // setBounds/穿透切换都可能重置窗口层级，恢复桌宠形态后 re-assert 置顶
    win.setAlwaysOnTop(true)
  })
})

app.on('will-quit', () => {
  quitting = true
  globalShortcut.unregisterAll()
  stopBrain()
})
app.on('window-all-closed', () => app.quit())
