<script setup lang="ts">
/**
 * 桌宠渲染（docs/03 §1：四通道动作原语）。
 * 光团小精灵（pet/sprite.ts，渲染层抽象预留 Live2D/VRM）+ 首次引导 + 内容卡片气泡。
 */
import { computed, nextTick, onMounted, onUnmounted, ref } from 'vue'
import Onboarding from './components/Onboarding.vue'
import Settings, { type SettingsData } from './components/Settings.vue'
import { SpritePetRenderer } from './pet/sprite'
import { DEFAULT_SPRITE, type PetExpression, type PetMood, type SpriteConfig } from './pet/renderer'
import { STATE_CONTRACT, type PetState } from './pet/states'
import { brainFetch, brainWs, type BrainMood } from './brain'
import { VoiceClient } from './voice'

interface SpeakCommand {
  type: 'speak'
  id: string
  channel: 'L0' | 'L1' | 'L2' | 'L3'
  payload: {
    content_id: string
    title: string
    reason: string
    summary: string
    url: string
    cover?: string // 封面图（§3.4：RSS enclosure/og:image，可空）
    // 人格化讲述（§8.1：L3 必带；hook 开场白+script_short 口语稿；立场库校验过一致性）
    narration?: { hook: string; script_short: string; stance_check_passed: boolean }
  }
  ttl_ms?: number | null
}

/** 晨报/成长汇报（docs/05 §7.5：精灵主动说；不进反馈学习，无👍👎/追问）。 */
interface DigestCommand {
  type: 'digest'
  id: string
  payload: { kind: 'morning' | 'weekly'; title: string; text: string }
}

/** story-1 互动剧一幕（brain 推送 story.scene；选择走 POST /story/choose 推进）。 */
interface StoryCommand {
  type: 'story.scene'
  id: string // story_id
  payload: {
    story_id: string
    beat: number
    total_beats: number
    title: string
    scene: string
    choices: string[]
    is_end: boolean
  }
}

/** 情绪姿态下行（docs/03 §2.2 mood.set；docs/05 §8.3）。 */
interface MoodCommand {
  type: 'mood.set'
  payload: { mood: 'idle' | 'thinking' | 'excited' | 'sorry' | 'sleepy' }
}

/** §11.35 瞬时表情下行（docs/03 §2.2 扩展）：改脸上部件（眉/嘴/腮红/贴纸），duration_ms 后回落。 */
interface ExpressionCommand {
  type: 'expression.set'
  payload: { expr: PetExpression; duration_ms?: number }
}

/** §11.35 内心 OS 下行：精灵静默时的独白气泡（斜体虚线，与俏皮话区分）。 */
interface OsCommand {
  type: 'os.say'
  payload: { text: string }
}

/** 关系账户快照（GET /relationship；§4.1 等级/XP 进度条数据源）。 */
interface Relationship {
  level: number
  xp: number
  xp_next_level: number
  hit_rate: number
  daily_l3_quota: number
  in_quiet_period: boolean
  l3_unlocked: boolean
}

/** 兴趣节点快照（GET /growth/panel → interests[]；§4.2 画像条形图数据源）。 */
interface InterestSnapshot {
  name: string
  temperature: number // 0~1（半衰 7 天指数衰减）
  status: 'active' | 'paused' | 'dormant'
  source: 'explicit' | 'inferred'
  episode_count: number
}

/** 成长面板聚合数据（GET /growth/panel，一次拿全）。 */
interface GrowthPanel {
  account: Relationship
  interests: InterestSnapshot[]
  facts_total: number
  facts_recent_7d: number
  taste_profile?: string // P1-5 品味档案卡：「我眼中的你」
}

/** 热榜条目（GET /trending，docs/05 §11.34-C：最新入库 10 条，点击看原文）。 */
interface TrendingItem {
  content_id: string
  title: string
  source: string
  age: string
  url: string
}

/** 等级装饰解锁表（§4.1：到达等级且用户未自选装饰时自动装备，不覆盖捏脸）。 */
const UNLOCKS = [
  { level: 3, acc: 'wings', label: '小翅膀' },
  { level: 5, acc: 'halo', label: '光环' },
  { level: 7, acc: 'antenna', label: '呆毛' },
] as const

type Phase = 'loading' | 'onboarding' | 'pet'
const phase = ref<Phase>('loading')
const bubble = ref<SpeakCommand | null>(null)
const digest = ref<DigestCommand | null>(null)
const story = ref<StoryCommand | null>(null) // story-1 互动剧：当前一幕分支卡片
const storyChoosing = ref(false)
const panel = ref<GrowthPanel | null>(null) // 成长面板（双击精灵打开）
const settings = ref<SettingsData | null>(null) // 设置面板（右键精灵打开；§5.2）
const rel = ref<Relationship | null>(null) // 卡片 XP 细条 + 升级比对
let spriteCfg: SpriteConfig = DEFAULT_SPRITE // 用户捏脸（自动装备装饰时合并，不覆盖）
const canvasRef = ref<HTMLCanvasElement | null>(null)
const reply = ref('')
const chatInput = ref('')
const chatSending = ref(false)
const quip = ref<string | null>(null)
const osLine = ref<string | null>(null) // §11.35 内心 OS 气泡（脑侧 os.say 下行驱动）
let ws: WebSocket | null = null
let bubbleTimer: number | null = null
let consumedTimer: number | null = null
let bubbleShownAt = 0 // 卡片弹出时刻（dwell_ms 采集）
let bubbleHoverMs = 0 // 卡片累计悬停时长（停留久=真在读，区别于挂着无视）
let bubbleHoverStart = 0
let digestTimer: number | null = null
let quipTimer: number | null = null
let pendingL2: SpeakCommand | null = null
let pendingL1: SpeakCommand | null = null
const pet = new SpritePetRenderer()

// ---- §11.37 状态动作契约：baseMood=brain 长期基准；状态临时 mood 到时回落基准 ----
let baseMood: PetMood = 'idle'
let moodTimer: number | null = null

/** 状态进入：按 STATE_CONTRACT 触发动作原语 + 瞬时表情 + 临时 mood（ms>0 时自动回落基准）。 */
function enterState(state: PetState) {
  const c = STATE_CONTRACT[state]
  if (c.act === 'hop') pet.attract()
  if (c.act === 'hint') pet.hint()
  if (c.expr) pet.setExpression(c.expr, 2000)
  if (c.mood) {
    if (moodTimer) {
      clearTimeout(moodTimer)
      moodTimer = null
    }
    pet.setMood?.(c.mood.value)
    if (c.mood.ms > 0) moodTimer = window.setTimeout(() => pet.setMood?.(baseMood), c.mood.ms)
  }
}

/** working 是持续性状态（ms=0）：事件结束显式退出，姿态回落 brain 基准。 */
function exitState(state: PetState) {
  const c = STATE_CONTRACT[state]
  if (c.mood && c.mood.ms === 0) {
    if (moodTimer) {
      clearTimeout(moodTimer)
      moodTimer = null
    }
    pet.setMood?.(baseMood)
  }
}

// ---- 语音对话（§8.4：喊名字 → 它答应并开口；开关持久化在 localStorage，UI 在设置面板 §11.28） ----
const voiceOn = ref(false)

// ---- 钻地-探头（静默收纳）：无交互时精灵沉到窗口底边之下，鼠标探入底部热区或
// 新消息到达时冒头。纯渲染层 transform，不动窗口位置（docs/03 §1：壳只做呈现） ----
const burrowed = ref(false)
const BURROW_IDLE_MS = 60_000 // 距上次本体交互多久后下沉
const BURROW_HOTBAND = 40 // 沉底时窗口底部可触发冒头的像素带（对应 .peek 土堆位置）
let lastPetActivity = Date.now()
let burrowLoopStarted = false

/** 冒头：升回地面并重置静默计时。任何消息下行与用户靠近都应调用。 */
function riseUp() {
  lastPetActivity = Date.now()
  burrowed.value = false
}

/** 本体被交互（命中/按下/点击）：刷新静默计时。 */
function touchPet() {
  lastPetActivity = Date.now()
}

/** 静默计时器：无气泡/面板/晨报/语音/待点 L2 且超过阈值 → 下沉。10s 一拍。 */
function startBurrowLoop() {
  if (burrowLoopStarted) return
  burrowLoopStarted = true
  setInterval(() => {
    if (
      !burrowed.value &&
      !bubble.value &&
      !panel.value &&
      !digest.value &&
      !story.value &&
      !settings.value &&
      !pendingL2 &&
      !voiceOn.value &&
      !dragging &&
      Date.now() - lastPetActivity >= BURROW_IDLE_MS
    ) {
      burrowed.value = true
    }
  }, 10_000)
}
const elfName = ref('小芽') // 真实名字（profile.elf_name），字幕不再写死
const voiceLine = ref<string | null>(null) // 聆听/应答字幕（复用 quip 样式，独立通道）
let voiceLineTimer: number | null = null
const voice = new VoiceClient({
  onWake() {
    showVoiceLine('我在听…')
  },
  onHeard(text) {
    if (text) showVoiceLine(`你：${text}`)
    else showVoiceLine('没听清，再叫我一次？', 2500)
    enterState('working') // §11.37 契约：听懂→思考姿态
  },
  onSay(text) {
    exitState('working') // §11.37：开口应答=思考结束，姿态回落基准
    // 字幕停留时长跟朗读时长走（≈5字/秒，下限 3s）
    showVoiceLine(`${elfName.value}：${text}`, Math.max(3000, text.length * 220))
  },
  onConverse() {
    showVoiceLine('接着说吧，我在听…', 12000) // 续聊窗：不用再喊名字
  },
  onIdle() {
    voiceLine.value = null // 续聊窗超时，回到待唤醒
  },
  onError(msg) {
    voiceOn.value = false
    localStorage.setItem('elf_voice_on', '0')
    showQuip(`语音开不了：${msg}`)
  },
})
voice.setMouth(pet)

function showVoiceLine(text: string, ms = 60000) {
  voiceLine.value = text
  if (voiceLineTimer) clearTimeout(voiceLineTimer)
  voiceLineTimer = window.setTimeout(() => (voiceLine.value = null), ms)
}

async function toggleVoice() {
  if (voiceOn.value) {
    voiceOn.value = false
    localStorage.setItem('elf_voice_on', '0')
    voice.stop()
    voiceLine.value = null
    return
  }
  try {
    await voice.start()
    voiceOn.value = true
    localStorage.setItem('elf_voice_on', '1')
    showVoiceLine('叫我的名字试试～', 5000)
  } catch (e) {
    showQuip(`语音开不了：${e instanceof Error ? e.message : e}`)
  }
}

/** 戳一戳俏皮话（本地模板，不烧 token；docs/05 §2.6）。 */
const QUIPS = [
  '干嘛干嘛？',
  '戳我算你眼光好',
  '我在盯着你感兴趣的世界呢',
  '再戳一下试试？',
  '有事说事，没事……也可以戳',
  '今天也在努力懂你呢',
  '哎呀，手痒了？',
  '轻点轻点，光团要戳散了',
  '（往旁边挪了挪，又没真挪）',
  '这手感，上瘾了吧',
]

/** §11.36 模型配置生效报喜池。 */
const EFFECT_QUIPS = ['新脑子接上了！', '感觉变聪明了！', '配置生效，灵气满满！']

/** §11.36 单机戳随机小情绪池（连戳 streak 仍走赌气/生气逻辑）。 */
const POKE_REACTIONS: { expr: PetExpression; weight: number }[] = [
  { expr: 'pout', weight: 3 },
  { expr: 'lovely', weight: 2 },
  { expr: 'surprised', weight: 2 },
  { expr: 'happy', weight: 2 },
  { expr: 'shy', weight: 1 },
]

function pickPokeExpr(): PetExpression {
  const total = POKE_REACTIONS.reduce((s, r) => s + r.weight, 0)
  let roll = Math.random() * total
  for (const r of POKE_REACTIONS) {
    roll -= r.weight
    if (roll <= 0) return r.expr
  }
  return 'pout'
}

/** §11.36 脑侧 mood → 壳侧表情（未知值回落 null，调用方给兜底）。 */
function coerceExpr(mood: string | undefined): PetExpression | null {
  return mood && ['normal', 'happy', 'pout', 'angry', 'lovely', 'surprised', 'shy'].includes(mood)
    ? (mood as PetExpression)
    : null
}

// ---- 启动：等 brain 就绪 → 查档案 → 引导或正常 ----
async function waitBrain(): Promise<Response | null> {
  for (let i = 0; i < 40; i++) {
    try {
      return await brainFetch('/profile')
    } catch {
      await new Promise((r) => setTimeout(r, 2000)) // brain 冷启动约 20s
    }
  }
  return null
}

async function boot() {
  const r = await waitBrain()
  const profile = r ? await r.json().catch(() => ({ exists: false })) : { exists: false }
  if (profile.exists) {
    elfName.value = profile.elf_name || '小芽'
    phase.value = 'pet'
    await initPet(profile.sprite_config ?? DEFAULT_SPRITE)
  } else {
    phase.value = 'onboarding'
  }
}

/** 捏脸合并自动装备装饰（§4.1：用户自选装饰优先，永不覆盖捏脸）；§11.37 皮肤从 localStorage 注入。 */
function mergedConfig(): SpriteConfig {
  const auto = localStorage.getItem('elf_auto_accessory') as SpriteConfig['accessory'] | null
  const skin = localStorage.getItem('elf_skin')
  return {
    ...spriteCfg,
    accessory: spriteCfg.accessory !== 'none' ? spriteCfg.accessory : (auto ?? 'none'),
    skin: skin ?? undefined,
  }
}

async function initPet(cfg: SpriteConfig) {
  spriteCfg = cfg
  await nextTick() // 等 canvas 挂载
  if (!canvasRef.value) return
  await pet.init(canvasRef.value)
  await pet.setModel(JSON.stringify(mergedConfig()))
  refreshRelationship(false) // 恢复成长缩放+等级基线（开机不庆祝）
  startBurrowLoop() // 钻地静默收纳（引导完成后才有精灵本体，这里起最稳）
}

async function onOnboarded(profile: { elf_name: string; sprite_config?: SpriteConfig }) {
  window.elfShell.setOnboarding(false)
  elfName.value = profile.elf_name || '小芽'
  phase.value = 'pet'
  await initPet(profile.sprite_config ?? DEFAULT_SPRITE)
}

// ---- 下行：SpeakCommand 四通道（docs/03 §2.4） ----
// §9.4 幂等重发：brain 重连后重发未 ack 的 SpeakCommand，壳按 decision_id 去重
const seenSpeak = new Set<string>()

function connect() {
  ws = brainWs('/ws/shell')
  ws.onmessage = (e) => {
    const cmd = JSON.parse(e.data) as SpeakCommand | DigestCommand | StoryCommand | MoodCommand | ExpressionCommand | OsCommand
    if (cmd.type === 'mood.set') {
      baseMood = cmd.payload.mood // §11.37：更新基准姿态（状态临时 mood 到期回落到它）
      pet.setMood?.(cmd.payload.mood) // §8.3：情绪姿态下行（被连拒变蔫/👍后兴奋）
      return
    }
    if (cmd.type === 'expression.set') {
      pet.setExpression(cmd.payload.expr, cmd.payload.duration_ms) // §11.35：脑侧驱动表情
      return
    }
    if (cmd.type === 'os.say') {
      showOs(cmd.payload.text) // §11.35：内心 OS 气泡（不与内容卡片/俏皮话互斥，最轻量）
      return
    }
    if (cmd.type === 'digest') {
      showDigest(cmd)
      return
    }
    if (cmd.type === 'story.scene') {
      showStory(cmd) // story-1 互动剧：新一幕（开局/推进/完结）
      return
    }
    if (cmd.type !== 'speak' || seenSpeak.has(cmd.id)) return
    seenSpeak.add(cmd.id)
    if (seenSpeak.size > 200) seenSpeak.delete(seenSpeak.values().next().value as string)
    if (cmd.channel === 'L3') showBubble(cmd)
    else if (cmd.channel === 'L2') attract(cmd)
    else if (cmd.channel === 'L1') holdL1(cmd)
    // L0 纯记录，无表现
  }
  ws.onclose = () => setTimeout(connect, 3000) // brain 重启后自动重连
}

/** 晨报/成长汇报：内容卡片让位（不记 dismissed，不进反馈学习）；60s 自消。 */
function showDigest(cmd: DigestCommand) {
  riseUp()
  if (bubble.value) dismiss(true)
  if (story.value) closeStory()
  digest.value = cmd
  pet.speak(true)
  if (digestTimer) clearTimeout(digestTimer)
  digestTimer = window.setTimeout(closeDigest, 60000)
}

function closeDigest() {
  digest.value = null
  pet.speak(false)
}

// ---- story-1 互动剧：一幕一分支卡片，点选推进，完结自动写回脑侧记忆 ----
/** 新一幕到达：内容卡片/digest 让位；不自动关（用户自己读，节奏自己掌握）。 */
function showStory(cmd: StoryCommand) {
  riseUp()
  if (bubble.value) dismiss(true)
  if (digest.value) closeDigest()
  story.value = cmd
  pet.speak(true)
}

/** 点选分支 → 推进下一幕；脑侧经 story.scene 推送回新一幕，这里只负责出声与表情。 */
async function chooseStory(choice: string) {
  const cur = story.value
  if (!cur || storyChoosing.value) return
  storyChoosing.value = true
  enterState('working')
  try {
    const r = await brainFetch('/story/choose', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ story_id: cur.payload.story_id, choice }),
    })
    const data = await r.json()
    if (data.error) {
      showQuip(data.error.message ?? '这幕演砸了，要不再开一局？')
      story.value = null
      pet.speak(false)
      return
    }
    // 下一幕由 story.scene 推送更新卡片；这里把这一幕念出来
    voice.speak(data.reply_text ?? '')
    pet.setExpression(coerceExpr(data.mood) ?? 'happy', 2000)
  } catch {
    showQuip('脑子不在线，故事先停在这～')
  } finally {
    exitState('working')
    storyChoosing.value = false
  }
}

/** 不玩了：只收卡片不删局（30 分钟不推进脑侧自动作废）。 */
function closeStory() {
  story.value = null
  pet.speak(false)
}

function showBubble(cmd: SpeakCommand) {
  riseUp()
  enterState('greet') // §11.37 契约：L3 到达 = 跳+开心脸（开口前先招个呼）
  if (digest.value) closeDigest() // 内容卡片与 digest 互斥（digest 不进反馈学习）
  if (story.value) closeStory() // 与互动剧卡片互斥
  if (panel.value) closePanel() // 卡片与成长面板互斥
  refreshRelationship() // 顺手刷新卡片 XP 细条（不阻塞，§4.1"点卡片能看到进度条动"）
  bubble.value = cmd
  // §9.4：实际渲染后回报曝光（=ack，brain 从未 ack 队列移除；不进反馈结算）
  nextTick(() => window.elfShell.sendEvent('speak.shown', { decision_id: cmd.id }))
  bubbleShownAt = Date.now()
  bubbleHoverMs = 0
  bubbleHoverStart = 0
  reply.value = ''
  chatInput.value = ''
  pet.speak(true)
  if (bubbleTimer) clearTimeout(bubbleTimer)
  bubbleTimer = window.setTimeout(() => dismiss(false), cmd.ttl_ms ?? 30000)
  // 反馈梯度：卡片展示满 20s 未关闭 → 读完信号（docs/02 §5.3，actual 0.5）
  if (consumedTimer) clearTimeout(consumedTimer)
  consumedTimer = window.setTimeout(() => {
    if (bubble.value?.id === cmd.id) {
      window.elfShell.sendEvent('speak.consumed', { decision_id: cmd.id })
    }
  }, 20000)
}

/** L1 静默持有：光脉冲提示，用户点击时才展开（docs/05 §2.5）。 */
function holdL1(cmd: SpeakCommand) {
  riseUp() // 有新消息=冒头（弱提示沉底时看不见光脉冲）
  pendingL1 = cmd
  enterState('waiting') // §11.37 契约：L1 静默持有 = hint 光脉冲
}

function attract(cmd: SpeakCommand) {
  riseUp()
  enterState('greet') // §11.37 契约：L2 到达 = 跳+开心脸（显眼化，不发声）
  pendingL2 = cmd // 用户点击宠物 → 升级为气泡展示
  showQuip('有新发现，点我看看') // 不点不知道怎么开，L2 需要可点开提示
  // 过期不候：不点击 120s 后自动放弃，否则 pendingL2 永驻会把钻地守卫永久锁死
  const expired = cmd
  setTimeout(() => {
    if (pendingL2 === expired) pendingL2 = null
  }, 120_000)
}

/**
 * 穿透窗下的交互切换（Electron Windows 行为）：
 * setIgnoreMouseEvents(true, {forward:true}) 时，renderer 只收到 mousemove，
 * 不触发 :hover / mouseenter——必须用 mousemove 自行判定。
 * 命中区域=气泡 + 精灵本体圆形（hitTest），其余区域穿透给下层窗口，
 * 避免挡住 IDE 等应用的按钮。
 */
let interactive = false
let dragging = false
let lastScreen = { x: 0, y: 0 }
let dragDist = 0
// drag-1 投掷测速：拖拽期间 EMA 平滑速度（px/s），松手交给主进程做惯性运动
let dragVel = { x: 0, y: 0 }
let lastDragT = 0

function updateInteractivity(e: MouseEvent) {
  // 拖动中：持续移动窗口并保持可交互（防止指针移出本体后穿透中断拖动）
  if (dragging) {
    const dx = e.screenX - lastScreen.x
    const dy = e.screenY - lastScreen.y
    if (dx || dy) {
      const now = performance.now()
      const dt = Math.max(1, now - lastDragT)
      lastDragT = now
      // EMA 平滑：瞬时抖动不直接把投掷速度拉飞
      dragVel.x = 0.6 * dragVel.x + 0.4 * ((dx / dt) * 1000)
      dragVel.y = 0.6 * dragVel.y + 0.4 * ((dy / dt) * 1000)
      dragDist += Math.abs(dx) + Math.abs(dy)
      lastScreen = { x: e.screenX, y: e.screenY }
      window.elfShell.dragBy(dx, dy)
      pet.dragBy?.(dx, dy) // skin-1 拖拽甩动：速度透传给渲染层
    }
    return
  }
  const rect = canvasRef.value?.getBoundingClientRect()
  const lx = e.clientX - (rect?.left ?? 0)
  const ly = e.clientY - (rect?.top ?? 0)
  // 钻地中：本体已沉，跳过视线跟随与命中判定；底部热区命中即冒头（穿透窗下
  // mousemove 覆盖整个窗口，用户把鼠标探向右下角 → 精灵噗地冒出来）
  if (burrowed.value) {
    // 底部热区（.peek 土堆所在横带）：命中即冒头，且让窗口在该带恢复可交互——
    // 用户能看见土堆、光标变正常、可点击召唤。其余区域保持穿透，不挡下层应用。
    // 注意必须用 e.clientY（viewport 原点=窗口左上）：canvas 在 .pet 容器内会随
    // 下沉位移，rect.top 跟着走，ly 凭空少 216px，热区在数学上永远够不到
    const near = e.clientY >= window.innerHeight - BURROW_HOTBAND
    if (near) riseUp()
    if (near !== interactive) {
      interactive = near
      window.elfShell.setInteractive(near)
    }
    return
  }
  if (rect && ly >= 0) pet.lookAt(lx, ly)
  const el = document.elementFromPoint(e.clientX, e.clientY) as HTMLElement | null
  const onBubble = !!el?.closest('.bubble, .voice-line') // 语音控件同享交互区
  // dwell 采集：悬停卡片=在读（穿透窗下 mousemove 进出各触发一次，停车阅读全程计入）
  if (bubble.value) {
    if (onBubble && bubbleHoverStart === 0) bubbleHoverStart = Date.now()
    else if (!onBubble && bubbleHoverStart > 0) {
      bubbleHoverMs += Date.now() - bubbleHoverStart
      bubbleHoverStart = 0
    }
  }
  const hit = onBubble || (rect != null && pet.hitTest(lx, ly))
  // 注意：悬停不重置静默计时——鼠标常停在右下角本体上，否则钻地永远等不到 60s
  if (hit !== interactive) {
    interactive = hit
    window.elfShell.setInteractive(hit)
  }
}

function onPetMouseDown(e: MouseEvent) {
  dragging = true
  touchPet()
  dragDist = 0
  dragVel = { x: 0, y: 0 }
  lastScreen = { x: e.screenX, y: e.screenY }
  lastDragT = performance.now()
  window.addEventListener('mouseup', onDragEnd, { once: true })
}

function onDragEnd() {
  dragging = false
  // drag-1 快甩松手 → 主进程接管窗口惯性；慢松手走 skin-1 原有弹簧回正
  const speed = Math.hypot(dragVel.x, dragVel.y)
  if (speed > 240) {
    window.elfShell.dragRelease(dragVel.x, dragVel.y)
    tossGlide(dragVel.x, dragVel.y) // 飞行途中持续甩动（与拖拽共用一套弹簧）
  }
}

/** drag-1 惯性飞行中的挥舞驱动：镜像主进程同款衰减（0.985/16ms），只驱动形变不管位置。 */
function tossGlide(vx: number, vy: number) {
  let last = performance.now()
  const step = (now: number) => {
    if (dragging) return // 用户半路又抓住了：交给拖拽管线
    const dt = Math.min(64, now - last)
    last = now
    const f = Math.pow(0.985, dt / 16)
    vx *= f
    vy *= f
    if (Math.hypot(vx, vy) < 30) return // 与主进程同阈值，落地交给落地挤压
    pet.dragBy?.((vx * dt) / 1000, (vy * dt) / 1000)
    requestAnimationFrame(step)
  }
  requestAnimationFrame(step)
}

// ---- 戳一戳表情（§11.35：3s 窗口内连戳 → 赌气；≥3 连戳 → 生气） ----
const pokeTimes: number[] = []

function onPetClick(e: MouseEvent) {
  if (e.detail >= 2) return // 双击的第二次 click 交给 openPanel，不重复戳一戳
  if (dragDist > 6) return // 拖拽落点不触发戳一戳
  if (panel.value) {
    closePanel() // 面板开着时单击精灵=关闭
    return
  }
  pet.poke()
  // §11.35 连戳表情：每次戳都赌气一下；3s 内戳满 3 次升级成生气（streak 彩蛋见 dismiss）
  const now = Date.now()
  pokeTimes.push(now)
  while (pokeTimes.length && now - pokeTimes[0] > 3_000) pokeTimes.shift()
  pet.setExpression(pokeTimes.length >= 3 ? 'angry' : pickPokeExpr(), 1500)
  if (pendingL2) {
    showBubble(pendingL2)
    pendingL2 = null
  } else if (pendingL1) {
    showBubble(pendingL1) // L1 点击展开（docs/05 §2.5）
    pendingL1 = null
  } else if (!bubble.value) {
    void pokeBrain() // §11.34-A：无内容时戳一戳 → 问脑（有推内容/闲聊），失败本地兜底
  }
}

// ---- 戳一戳接脑（docs/05 §11.34-A） ----
let pokeAskedAt = 0 // 上次问脑时刻：高频连戳（<15s）只给本地俏皮话，不烧 token
async function pokeBrain() {
  const now = Date.now()
  if (now - pokeAskedAt < 15_000) {
    pet.setExpression('pout', 1500) // §11.35：问太勤，脑不理 → 本地兜底也带情绪
    showQuip()
    return
  }
  pokeAskedAt = now
  try {
    const r = await brainFetch('/poke', { method: 'POST' })
    if (!r.ok) {
      showQuip()
      return
    }
    const data = await r.json()
    if (data.kind === 'chat' && data.reply_text) {
      showQuip(data.reply_text, 6000) // 脑的接话比本地俏皮话长，给足读完时间
      const expr = coerceExpr(data.mood)
      if (expr) pet.setExpression(expr, 2000) // §11.36：被戳的接话 → 脸随语气
    } else {
      showQuip() // speak=内容气泡走 WS 下行；noop=脑侧节流中 → 本地兜底
    }
  } catch {
    showQuip() // 脑不在线 → 本地兜底
  }
}

// ---- 成长可视化（docs/05 §4：等级进度条/升级庆祝/成长面板） ----
/** 面板兴趣画像：按温度降序取 top8。 */
const topInterests = computed(() =>
  [...(panel.value?.interests ?? [])].sort((a, b) => b.temperature - a.temperature).slice(0, 8),
)
/** XP 进度百分比（面板与卡片细条共用算法）。 */
function xpPercent(a: Relationship | null | undefined): number {
  if (!a || a.xp_next_level <= 0) return 0
  return Math.min(100, Math.round((a.xp / a.xp_next_level) * 100))
}
const panelXpPct = computed(() => xpPercent(panel.value?.account))
const relXpPct = computed(() => xpPercent(rel.value))

/** 拉关系账户刷新 XP 条；比对 localStorage 检测升级（brain 无升级事件，壳侧比对）。 */
async function refreshRelationship(celebrate = true) {
  try {
    const r = await brainFetch('/relationship')
    if (!r.ok) return
    const data = (await r.json()) as Relationship
    const prev = Number(localStorage.getItem('elf_last_level') ?? '0')
    rel.value = data
    pet.setGrowth?.(1 + (data.level - 1) * 0.03) // 每级 +3% 渐进变大
    if (celebrate && prev > 0 && data.level > prev) onLevelUp(data.level)
    localStorage.setItem('elf_last_level', String(data.level))
  } catch {
    /* brain 不在线时静默 */
  }
}

/** 升级庆祝（§11.37 契约：celebrate = 跳+开心脸+兴奋几秒）；到达解锁档且用户未自选装饰 → 自动装备。 */
function onLevelUp(level: number) {
  enterState('celebrate')
  const unlock = UNLOCKS.find((u) => u.level === level)
  if (unlock && spriteCfg.accessory === 'none') {
    localStorage.setItem('elf_auto_accessory', unlock.acc)
    pet.setModel(JSON.stringify(mergedConfig()))
    showQuip(`我升级啦，还长出了${unlock.label}！`)
  } else {
    showQuip(`我升级啦！Lv.${level}`)
  }
}

/** 双击精灵 → 成长面板（§4.2）；与卡片/digest/俏皮话互斥。 */
async function openPanel() {
  if (dragDist > 6) return // 拖拽落点不开面板
  if (panel.value) {
    closePanel()
    return
  }
  if (bubble.value) dismiss(true) // 让位不算无视
  if (digest.value) closeDigest()
  quip.value = null
  osLine.value = null
  try {
    const r = await brainFetch('/growth/panel')
    if (!r.ok) return
    panel.value = (await r.json()) as GrowthPanel
    void loadTrending() // §11.34-C：热榜区异步填充，不阻塞面板打开
  } catch {
    showQuip('脑子不在线，待会再看～')
  }
}

function closePanel() {
  panel.value = null
  trending.value = null
}

// ---- 今天的新鲜事（docs/05 §11.34-C：成长面板热榜区） ----
const trending = ref<TrendingItem[] | null>(null) // null=加载中/未打开；[]=脑无内容
async function loadTrending() {
  try {
    const r = await brainFetch('/trending')
    if (!r.ok) return
    trending.value = (await r.json()) as TrendingItem[]
  } catch {
    trending.value = [] // 脑不在线 → 热榜区显示"还在蹲"，不打扰面板其他内容
  }
}

/** 点热榜条目看原文（全部真实入库内容，红线⑤）。 */
function openTrend(t: TrendingItem) {
  if (t.url) window.open(t.url)
}

// ---- 设置面板（§5.2：右键精灵唤出；改名字/捏脸/兴趣/清空重来） ----
async function openSettings() {
  if (settings.value) {
    settings.value = null
    return
  }
  if (bubble.value) dismiss(true) // 让位不算无视
  if (digest.value) closeDigest()
  if (panel.value) closePanel()
  quip.value = null
  osLine.value = null
  try {
    const r = await brainFetch('/profile')
    if (!r.ok) return
    const p = await r.json()
    if (!p.exists) return
    settings.value = {
      elf_name: p.elf_name ?? '',
      sprite_config: {
        ...DEFAULT_SPRITE,
        ...(p.sprite_config ?? {}),
        skin: localStorage.getItem('elf_skin') ?? undefined, // §11.37 皮肤壳侧持有
      },
      interests: p.interests ?? [],
      feed_urls: p.feed_urls ?? [],
    }
  } catch {
    showQuip('脑子不在线，待会再改～')
  }
}

/** 保存设置：POST /profile 全量覆盖；捏脸即时重建；改名随下次语音会话生效。
 * §11.36 模型配置：改了 LLM 配置则追加 POST /config/llm，生效瞬间小精灵跳+发光+报喜。 */
async function onSettingsSave(data: SettingsData) {
  settings.value = null
  const { skin, ...spriteRest } = data.sprite_config // §11.37 皮肤壳侧持有，不进脑库
  if (skin) localStorage.setItem('elf_skin', skin)
  else localStorage.removeItem('elf_skin')
  spriteCfg = spriteRest
  elfName.value = data.elf_name || '小芽'
  if (data.sprite_config.accessory !== 'none') localStorage.removeItem('elf_auto_accessory')
  await pet.setModel(JSON.stringify(mergedConfig()))
  const body = { ...data, sprite_config: spriteRest }
  try {
    await brainFetch('/profile', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
  } catch {
    showQuip('没存上，脑子不在线')
    return
  }
  if (data.llm) {
    try {
      const r = await brainFetch('/config/llm', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data.llm),
      })
      if (r.ok && (await r.json()).changed?.length) {
        // §11.37 契约：配置生效 = celebrate（跳+光脉冲+开心脸+报喜）
        enterState('celebrate')
        showQuip(EFFECT_QUIPS[Math.floor(Math.random() * EFFECT_QUIPS.length)], 4000)
        return
      }
    } catch {
      showQuip('模型配置没生效，再试试？')
      return
    }
  }
  showQuip('记住啦')
}

/** 纠偏：一句话直连 /correction（事实归档 + 槽位 upsert，不触发全量保存）。 */
async function onSettingsCorrect(text: string) {
  try {
    const r = await brainFetch('/correction', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text }),
    })
    const d = await r.json()
    showQuip((d.applied_fact_ids?.length ?? 0) > 0 ? '记住这个改变了' : '收到，我再观察观察')
  } catch {
    showQuip('没记上，脑子不在线')
  }
}

/** 退出应用（无边框窗口无系统关闭按钮，设置面板显式退出）。 */
function onSettingsQuit() {
  console.log('[quit] 按钮 → elfShell.quit()')
  window.elfShell.quit()
}

/** 清空重来：brain 抹数据后退出（生产壳自动拉起）；renderer 稍后 reload 回引导。 */
async function onSettingsReset() {
  settings.value = null
  localStorage.removeItem('elf_skin') // §11.37 清空重来 → 皮肤一并回默认光团
  try {
    await brainFetch('/reset', { method: 'POST' })
  } catch {
    /* brain 可能已开始退出，忽略 */
  }
  showQuip('清空啦，马上重新认识你～')
  voice.stop() // 语音会话持有旧模型状态，一并收掉
  setTimeout(() => location.reload(), 2500)
}

/** 戳一戳俏皮话小气泡：默认 3s 自动消失；脑的闲聊接话传 durationMs=6000 给足读完时间。 */
function showQuip(text?: string, durationMs = 3000) {
  quip.value = text ?? QUIPS[Math.floor(Math.random() * QUIPS.length)]
  if (quipTimer) clearTimeout(quipTimer)
  quipTimer = window.setTimeout(() => (quip.value = null), durationMs)
}

/** §11.35 内心 OS 气泡：静默独白，8s 自消；不与内容卡片/俏皮话互斥（最轻量通道）。 */
let osTimer: number | null = null
function showOs(text: string) {
  if (!text) return
  osLine.value = text
  if (osTimer) clearTimeout(osTimer)
  osTimer = window.setTimeout(() => (osLine.value = null), 8000)
}

/** 👍👎 快捷反馈（docs/05 §2.3）：显式信号最高优先级，即时应答增强"被听懂"感。 */
function feedback(positive: boolean) {
  if (!bubble.value) return
  window.elfShell.sendEvent('speak.feedback_explicit', {
    decision_id: bubble.value.id,
    positive,
  })
  dismiss(true) // 已显式表态，不再补发 dismissed
  pet.setExpression(positive ? 'lovely' : 'pout', 1800) // §11.35：被夸→爱心眼，被否→委屈
  showQuip(positive ? '记住了，下次多讲这类' : '收到，以后少讲这种')
}

/** 追问对话（docs/05 §2.4）：回答锚定 content_id，检索在前生成在后。 */
async function ask() {
  const text = chatInput.value.trim()
  if (!text || !bubble.value || chatSending.value) return
  chatSending.value = true
  enterState('working') // §11.37 契约：追问进行中 = 思考姿态（回复到达/失败都退出）
  try {
    const r = await brainFetch('/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        text,
        content_id: bubble.value.payload.content_id,
        decision_id: bubble.value.id,
      }),
    })
    const data = await r.json()
    reply.value = data.reply_text ?? '……'
    // §11.36：脸跟着话的语气走（脑侧 LLM 挑 mood），不再一律 happy
    pet.setExpression(coerceExpr(data.mood) ?? 'happy', 2000)
  } catch {
    reply.value = '我现在脑子不在线，待会再问我～'
    pet.setExpression('pout', 2000) // §11.35：答不上来 → 委屈抱歉脸（pout 重设计后适用）
  } finally {
    exitState('working') // §11.37：姿态回落 brain 基准
    chatSending.value = false
    chatInput.value = ''
  }
}

/** RSS 摘要常带 HTML 标签，卡片只显示纯文本。 */
function plain(html: string): string {
  return html.replace(/<[^>]*>/g, '').replace(/\s+/g, ' ').trim()
}

function open() {
  if (!bubble.value) return
  window.elfShell.sendEvent('speak.opened', { decision_id: bubble.value.id })
  if (bubble.value.payload.url) window.open(bubble.value.payload.url)
  dismiss(true)
}

function dismiss(opened: boolean) {
  if (bubble.value && !opened) {
    // 反馈梯度（docs/02 §5.3）：悬停读 ≥8s 才关 → dwell(0.75) 替代 dismissed(0)
    // 挂着无视 ≠ 停留久——以悬停时长而非展示时长判定；max-wins 自动盖过 consumed(0.5)
    if (bubbleHoverStart > 0) {
      bubbleHoverMs += Date.now() - bubbleHoverStart
      bubbleHoverStart = 0
    }
    if (bubbleHoverMs >= 8000) {
      window.elfShell.sendEvent('speak.dwell', {
        decision_id: bubble.value.id,
        dwell_ms: Date.now() - bubbleShownAt,
        completed: true,
      })
    } else {
      window.elfShell.sendEvent('speak.dismissed', { decision_id: bubble.value.id })
      // §11.37 契约：被无视 = 受挫（赌气+变蔫一会）；同日累计 5 次 → 彩蛋发火（计数清零）
      enterState('failed')
      const today = new Date().toDateString()
      const rec = localStorage.getItem('elf_ignore_streak')?.split('|')
      const streak = rec && rec[0] === today ? Number(rec[1]) || 0 : 0
      if (streak + 1 >= 5) {
        localStorage.removeItem('elf_ignore_streak')
        pet.setExpression('angry', 2500)
        showQuip('今天第 5 次无视我了！我要闹了（bushi', 5000)
      } else {
        localStorage.setItem('elf_ignore_streak', `${today}|${streak + 1}`)
        if (Math.random() < 0.25) showQuip('又不理我…')
      }
    }
    bubbleHoverMs = 0
  }
  if (consumedTimer) {
    clearTimeout(consumedTimer)
    consumedTimer = null
  }
  bubble.value = null
  pet.speak(false)
}

onMounted(() => {
  boot()
  connect()
  window.addEventListener('mousemove', updateInteractivity)
  window.elfShell.onPetImpact((impact) => pet.land?.(impact)) // drag-1 落地/撞边挤压
  if (localStorage.getItem('elf_voice_on') === '1') toggleVoice() // 恢复语音开关（§8.4）
})
onUnmounted(() => {
  ws?.close()
  voice.stop()
  pet.dispose()
  window.removeEventListener('mousemove', updateInteractivity)
})
</script>

<template>
  <Onboarding v-if="phase === 'onboarding'" @done="onOnboarded" />
  <div v-else class="stage">
    <div v-if="settings" class="bubble settings-bubble">
      <Settings :initial="settings" :voice-on="voiceOn" @save="onSettingsSave"
        @correct="onSettingsCorrect" @reset="onSettingsReset" @quit="onSettingsQuit"
        @close="settings = null" @voice-toggle="toggleVoice" />
    </div>
    <div v-else-if="panel" class="bubble panel">
      <button class="close-btn light" title="收起" @click="closePanel">
        <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor"
          stroke-width="2" stroke-linecap="round">
          <path d="M6 6l12 12M18 6L6 18" />
        </svg>
      </button>
      <div class="panel-head">
        <span class="lv-badge">Lv.{{ panel.account.level }}</span>
        <div class="xp-bar"><div class="xp-fill" :style="{ width: panelXpPct + '%' }"></div></div>
        <span class="xp-num">{{ panel.account.xp }} / {{ panel.account.xp_next_level }}</span>
      </div>
      <div class="panel-scroll">
        <p class="panel-meta">
          命中率 {{ Math.round(panel.account.hit_rate * 100) }}% · 今日 L3 配额
          {{ panel.account.daily_l3_quota }}
        </p>
        <div class="panel-sec">它懂你的方向</div>
        <div
          v-for="it in topInterests"
          :key="it.name"
          class="interest-row"
          :class="{ dim: it.status !== 'active' }"
        >
          <span class="interest-name" :title="it.name">{{ it.name }}</span>
          <div class="interest-bar">
            <div
              class="interest-fill"
              :class="it.source"
              :style="{ width: Math.round(it.temperature * 100) + '%' }"
            ></div>
          </div>
          <span class="interest-temp">{{ Math.round(it.temperature * 100) }}</span>
        </div>
        <p v-if="!topInterests.length" class="panel-meta">还在观察你的兴趣……</p>
        <div class="panel-sec">记忆结晶</div>
        <div class="panel-card">
          记住 <b>{{ panel.facts_total }}</b> 条 · 近 7 天新学会
          <b>{{ panel.facts_recent_7d }}</b> 条
        </div>
        <div class="panel-sec">我眼中的你</div>
        <div v-if="panel.taste_profile" class="panel-card taste-card">
          {{ panel.taste_profile }}
        </div>
        <p v-else class="panel-meta">还在慢慢认识你……</p>
        <div class="panel-sec">今天的新鲜事</div>
        <div v-if="trending && trending.length" class="trend-list">
          <div
            v-for="(t, i) in trending"
            :key="t.content_id"
            class="trend-row"
            :title="t.title"
            @click="openTrend(t)"
          >
            <span class="trend-rank">{{ i + 1 }}</span>
            <span class="trend-title">{{ t.title }}</span>
            <span class="trend-meta">{{ t.source }} · {{ t.age }}</span>
          </div>
        </div>
        <p v-else-if="trending" class="panel-meta">还在蹲新鲜内容，待会再来看看～</p>
        <div class="panel-sec">成长解锁</div>
        <div class="unlock-row">
          <span
            v-for="u in UNLOCKS"
            :key="u.level"
            class="unlock"
            :class="{ got: panel.account.level >= u.level }"
            >Lv.{{ u.level }} {{ u.label }}</span
          >
        </div>
      </div>
    </div>
    <div v-else-if="bubble" class="bubble card">
      <div v-if="bubble.payload.cover" class="hero">
        <img class="hero-img" :src="bubble.payload.cover" alt="" />
        <div class="hero-shade"></div>
        <span class="reason-pill">{{ bubble.payload.reason }}</span>
      </div>
      <div v-else class="card-head">
        <span class="reason-pill light">{{ bubble.payload.reason }}</span>
      </div>
      <button
        class="close-btn"
        :class="{ light: !bubble.payload.cover }"
        title="收起"
        @click="dismiss(false)"
      >
        <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor"
          stroke-width="2" stroke-linecap="round">
          <path d="M6 6l12 12M18 6L6 18" />
        </svg>
      </button>
      <div class="card-body">
        <p v-if="bubble.payload.narration" class="hook">{{ bubble.payload.narration.hook }}</p>
        <h2 class="title">{{ bubble.payload.title }}</h2>
        <p v-if="bubble.payload.narration" class="summary narration-script">
          {{ bubble.payload.narration.script_short }}
        </p>
        <p v-else-if="bubble.payload.summary" class="summary">
          {{ plain(bubble.payload.summary) }}
        </p>
        <div v-if="reply" class="reply">{{ reply }}</div>
      </div>
      <div class="card-foot">
        <div class="actions">
          <button class="primary" @click="open">看看原文</button>
          <button class="ghost" @click="dismiss(false)">待会</button>
          <button class="vote" title="这类多讲点" @click="feedback(true)">
            <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor"
              stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
              <path d="M7 22V11l5-9a2.5 2.5 0 0 1 2.4 3.2L13 11h5.6a2.4 2.4 0 0 1 2.3 3.1l-2 6.4A2.4 2.4 0 0 1 16.6 22H7z" />
              <path d="M7 22H3V11h4" />
            </svg>
          </button>
          <button class="vote" title="这类少讲点" @click="feedback(false)">
            <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor"
              stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"
              style="transform: rotate(180deg)">
              <path d="M7 22V11l5-9a2.5 2.5 0 0 1 2.4 3.2L13 11h5.6a2.4 2.4 0 0 1 2.3 3.1l-2 6.4A2.4 2.4 0 0 1 16.6 22H7z" />
              <path d="M7 22H3V11h4" />
            </svg>
          </button>
        </div>
        <div class="ask-row">
          <input
            v-model="chatInput"
            class="ask-input"
            placeholder="追问这条…"
            :disabled="chatSending"
            @keyup.enter="ask"
          />
          <button class="ask-send" :disabled="chatSending || !chatInput.trim()" @click="ask">
            {{ chatSending ? '…' : '问' }}
          </button>
        </div>
        <div
          v-if="rel"
          class="xp-strip"
          :title="`Lv.${rel.level} · 经验 ${rel.xp}/${rel.xp_next_level}`"
        >
          <div class="xp-strip-fill" :style="{ width: relXpPct + '%' }"></div>
        </div>
      </div>
    </div>
    <div v-else-if="digest" class="bubble digest-bubble">
      <button class="close-btn light" title="收起" @click="closeDigest">
        <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor"
          stroke-width="2" stroke-linecap="round">
          <path d="M6 6l12 12M18 6L6 18" />
        </svg>
      </button>
      <div class="card-head">
        <span class="kicker">{{ digest.payload.kind === 'weekly' ? '成长汇报' : '早安日报' }}</span>
      </div>
      <div class="card-body">
        <h2 class="title">{{ digest.payload.title }}</h2>
        <div class="digest-text">{{ digest.payload.text }}</div>
      </div>
      <div class="card-foot">
        <div class="actions"><button class="primary" @click="closeDigest">知道啦</button></div>
      </div>
    </div>
    <div v-else-if="story" class="bubble digest-bubble story-bubble">
      <button class="close-btn light" title="不玩了" @click="closeStory">
        <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor"
          stroke-width="2" stroke-linecap="round">
          <path d="M6 6l12 12M18 6L6 18" />
        </svg>
      </button>
      <div class="card-head">
        <span class="kicker">互动剧 · 第 {{ story.payload.beat }}/{{ story.payload.total_beats }} 幕 · 《{{ story.payload.title }}》</span>
      </div>
      <div class="card-body">
        <div class="story-text">{{ story.payload.scene }}</div>
      </div>
      <div class="card-foot">
        <div v-if="!story.payload.is_end" class="actions story-actions">
          <button
            v-for="c in story.payload.choices"
            :key="c"
            class="primary"
            :disabled="storyChoosing"
            @click="chooseStory(c)"
          >{{ c }}</button>
          <button class="ghost" :disabled="storyChoosing" @click="closeStory">不玩了</button>
        </div>
        <div v-else class="actions">
          <span class="story-end">— 完 —</span>
          <button class="primary" @click="closeStory">落幕</button>
        </div>
      </div>
    </div>
    <div v-else-if="quip" class="quip">{{ quip }}</div>
    <div v-if="osLine" class="os">{{ osLine }}</div>
    <div v-if="voiceLine" class="voice-line">{{ voiceLine }}</div>
    <div
      class="pet"
      :class="{ burrowed }"
      @mousedown="onPetMouseDown"
      @click="onPetClick"
      @dblclick="openPanel"
      @contextmenu.prevent="openSettings"
    >
      <canvas ref="canvasRef" class="pet-canvas" />
    </div>
    <!-- 钻地哨兵：本体沉底后完全隐形，用户找不到召回入口（窗口底边在任务栏上方
         约 72px，鼠标到屏幕角落在任务栏上触发不到窗口）。沉底时露一个小土堆+眼睛，
         指示位置；鼠标探入底部热区或点击土堆即冒头 -->
    <div v-show="burrowed" class="peek"><i /><i /></div>
  </div>
</template>

<style scoped>
.stage {
  position: relative;
  width: 100%;
  height: 100%;
}
/* 精灵只占底部 38%，气泡在上方展开——永不遮脸（docs/05 §2.1） */
.pet {
  position: absolute;
  bottom: 0;
  left: 0;
  width: 100%;
  height: 38%;
  cursor: pointer;
  /* 钻地-探头：冒头带回弹，下沉稍缓（躲回去的动作要轻） */
  transition: transform 0.55s cubic-bezier(0.34, 1.45, 0.5, 1);
  will-change: transform;
}
/* 沉到窗口底边之下（留 12px 头顶弹性：模型顶边有呆毛/耳尖时会露出小尖尖） */
.pet.burrowed {
  transform: translateY(calc(100% - 12px));
  transition-duration: 0.8s;
  cursor: default;
}
.pet-canvas {
  width: 100%;
  height: 100%;
  display: block;
  /* 琥珀环境光环（暗色主题）：叠加自身光晕出「头像橙环」效果 */
  filter: drop-shadow(0 0 22px rgba(255, 150, 50, 0.3));
}
/* 钻地哨兵：小土堆+两只眼睛，沉底时指示精灵位置（呼吸微光引鼠标探入热区） */
.peek {
  position: absolute;
  bottom: 1px;
  left: 50%;
  transform: translateX(-50%);
  display: flex;
  justify-content: center;
  gap: 9px;
  width: 56px;
  height: 16px;
  padding-top: 5px;
  border-radius: 14px 14px 4px 4px;
  background: rgba(34, 30, 26, 0.62);
  box-shadow: 0 -1px 6px rgba(0, 0, 0, 0.25);
  pointer-events: none;
  animation: peek-breathe 2.4s ease-in-out infinite;
}
.peek i {
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: #ffe9d0;
  box-shadow: 0 0 6px 1px rgba(255, 175, 90, 0.9);
}
@keyframes peek-breathe {
  0%,
  100% {
    opacity: 0.7;
  }
  50% {
    opacity: 1;
  }
}

/* ---------- 卡片容器（内容卡/成长面板/晨报/设置共用） ---------- */
.bubble {
  position: absolute;
  top: 10px;
  left: 12px;
  right: 12px;
  display: flex;
  flex-direction: column;
  max-height: calc(62% - 22px);
  border-radius: var(--elf-radius);
  background: rgba(26, 20, 14, 0.94);
  border: 1px solid rgba(255, 190, 120, 0.14);
  box-shadow:
    0 26px 50px -22px rgba(0, 0, 0, 0.7),
    0 4px 12px rgba(0, 0, 0, 0.45);
  backdrop-filter: blur(12px);
  overflow: hidden;
  color: var(--elf-ink);
  z-index: 10;
  animation: card-in 0.22s cubic-bezier(0.2, 0.9, 0.3, 1.05);
}
@keyframes card-in {
  from {
    opacity: 0;
    transform: translateY(10px) scale(0.98);
  }
  to {
    opacity: 1;
    transform: none;
  }
}
/* 设置面板自带 头/滚动体/底部操作区 三段，外框只负责圆角与高度 */
.settings-bubble {
  padding: 0;
}
.settings-bubble > * {
  flex: 1 1 auto;
  min-height: 0;
}

/* 封面：只做视觉锚点，理由压在其上 */
.hero {
  position: relative;
  flex: none;
  height: 86px;
  overflow: hidden;
  background: linear-gradient(135deg, #2e2317, #211910);
}
.hero-img {
  display: block;
  width: 100%;
  height: 100%;
  object-fit: cover;
}
.hero-shade {
  position: absolute;
  inset: 0;
  background: linear-gradient(
    180deg,
    rgba(22, 13, 6, 0.2) 0%,
    rgba(22, 13, 6, 0.02) 40%,
    rgba(22, 13, 6, 0.68) 100%
  );
}
.reason-pill {
  position: absolute;
  left: 14px;
  bottom: 10px;
  max-width: calc(100% - 64px);
  padding: 4px 10px;
  border-radius: 999px;
  font-size: 11.5px;
  font-weight: 600;
  color: #fff;
  background: rgba(15, 20, 36, 0.34);
  border: 1px solid rgba(255, 255, 255, 0.28);
  backdrop-filter: blur(8px);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.reason-pill.light {
  position: static;
  display: inline-block;
  max-width: calc(100% - 40px);
  color: var(--elf-accent);
  background: rgba(255, 154, 61, 0.12);
  border-color: rgba(255, 154, 61, 0.26);
  backdrop-filter: none;
}
.card-head {
  flex: none;
  padding: 16px 16px 0;
}
/* 无封面时的分类标签（晨报/成长汇报） */
.kicker {
  display: inline-block;
  padding: 4px 10px;
  border-radius: 999px;
  font-size: 11.5px;
  font-weight: 600;
  color: #ffb35c;
  background: rgba(255, 154, 61, 0.13);
  border: 1px solid rgba(255, 154, 61, 0.32);
}

/* 收起按钮：无边框窗口没有别的收起入口 */
.close-btn {
  position: absolute;
  top: 10px;
  right: 10px;
  z-index: 3;
  width: 26px;
  height: 26px;
  display: grid;
  place-items: center;
  padding: 0;
  border: none;
  border-radius: 50%;
  background: rgba(15, 20, 36, 0.34);
  color: #fff;
  cursor: pointer;
  backdrop-filter: blur(8px);
  transition:
    background 0.15s,
    transform 0.15s;
}
.close-btn:hover {
  background: rgba(15, 20, 36, 0.55);
  transform: scale(1.06);
}
.close-btn.light {
  background: rgba(255, 255, 255, 0.08);
  color: var(--elf-ink-3);
  backdrop-filter: none;
}
.close-btn.light:hover {
  background: rgba(255, 255, 255, 0.16);
  color: var(--elf-ink);
}

/* ---------- 卡片正文 ---------- */
.card-body {
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
  padding: 12px 16px;
}
.hook {
  margin: 0 0 7px;
  font-size: 12.5px;
  line-height: 1.5;
  font-weight: 600;
  color: var(--elf-accent);
  display: -webkit-box;
  -webkit-line-clamp: 1;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.title {
  margin: 0;
  font-size: 16px;
  line-height: 1.45;
  font-weight: 700;
  color: var(--elf-ink);
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.summary {
  margin: 8px 0 0;
  font-size: 13px;
  line-height: 1.68;
  color: var(--elf-ink-2);
  display: -webkit-box;
  -webkit-line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
/* §8.1 人格化讲述：口播稿替代干巴 summary */
.narration-script {
  -webkit-line-clamp: 4;
}
.reply {
  margin-top: 10px;
  padding: 10px 12px;
  border-radius: 12px;
  background: linear-gradient(180deg, rgba(255, 154, 61, 0.12), rgba(255, 125, 31, 0.06));
  border: 1px solid rgba(255, 154, 61, 0.24);
  font-size: 12.5px;
  line-height: 1.62;
  color: #f2e2cb;
}
.digest-text {
  margin-top: 10px;
  font-size: 13px;
  line-height: 1.75;
  color: var(--elf-ink-2);
  white-space: pre-line;
}

/* ---------- story-1 互动剧卡片 ---------- */
.story-text {
  font-size: 13.5px;
  line-height: 1.85;
  color: var(--elf-ink-2);
  white-space: pre-line;
}
/* 分支选择纵向排列：选项是剧情分支，横排挤且容易误触 */
.story-actions {
  flex-direction: column;
  align-items: stretch;
}
.story-actions .primary {
  width: 100%;
}
.story-end {
  flex: 1;
  text-align: center;
  color: var(--elf-ink-3, rgba(255, 244, 224, 0.45));
  font-size: 12.5px;
  letter-spacing: 0.3em;
}

/* ---------- 卡片底部操作区（固定，不随正文滚动） ---------- */
.card-foot {
  flex: none;
  padding: 10px 14px 12px;
  border-top: 1px solid var(--elf-line);
  background: linear-gradient(180deg, rgba(33, 26, 19, 0.3), #211a13);
}
.actions {
  display: flex;
  align-items: center;
  gap: 8px;
}
.actions .primary {
  flex: 1;
  height: 34px;
  border: none;
  border-radius: 11px;
  background: linear-gradient(180deg, #ffb35c, #ff7d1f);
  color: #2b1403;
  font-size: 13px;
  font-weight: 600;
  letter-spacing: 0.02em;
  cursor: pointer;
  box-shadow: 0 6px 14px -6px rgba(255, 125, 31, 0.55);
  transition:
    filter 0.15s,
    transform 0.1s;
}
.actions .primary:hover {
  filter: brightness(1.06);
}
.actions .primary:active {
  transform: translateY(1px);
}
.actions .ghost {
  height: 34px;
  padding: 0 15px;
  border-radius: 11px;
  border: 1px solid var(--elf-line-strong);
  background: rgba(255, 255, 255, 0.06);
  color: var(--elf-ink-2);
  font-size: 12.5px;
  cursor: pointer;
  transition:
    background 0.15s,
    color 0.15s;
}
.actions .ghost:hover {
  background: rgba(255, 255, 255, 0.12);
  color: var(--elf-ink);
}
.vote {
  flex: none;
  width: 34px;
  height: 34px;
  display: grid;
  place-items: center;
  padding: 0;
  border-radius: 11px;
  border: 1px solid var(--elf-line-strong);
  background: rgba(255, 255, 255, 0.06);
  color: var(--elf-ink-3);
  cursor: pointer;
  transition: all 0.15s;
}
.vote:hover {
  color: var(--elf-accent);
  border-color: rgba(255, 154, 61, 0.45);
  background: rgba(255, 154, 61, 0.1);
}
.ask-row {
  display: flex;
  gap: 8px;
  margin-top: 8px;
}
.ask-input {
  flex: 1;
  min-width: 0;
  height: 32px;
  padding: 0 11px;
  border-radius: 10px;
  border: 1px solid var(--elf-line-strong);
  background: rgba(255, 255, 255, 0.05);
  font-size: 12.5px;
  color: var(--elf-ink);
  outline: none;
  transition:
    border-color 0.15s,
    box-shadow 0.15s;
}
.ask-input::placeholder {
  color: var(--elf-ink-3);
}
.ask-input:focus {
  border-color: rgba(255, 154, 61, 0.6);
  box-shadow: 0 0 0 3px rgba(255, 154, 61, 0.16);
}
.ask-input:disabled {
  background: rgba(255, 255, 255, 0.03);
}
.ask-send {
  flex: none;
  width: 48px;
  height: 32px;
  border: none;
  border-radius: 10px;
  background: rgba(255, 154, 61, 0.14);
  color: var(--elf-accent);
  font-size: 12.5px;
  font-weight: 600;
  cursor: pointer;
  transition: background 0.15s;
}
.ask-send:hover:not(:disabled) {
  background: rgba(255, 154, 61, 0.24);
}
.ask-send:disabled {
  color: var(--elf-ink-3);
  background: rgba(255, 255, 255, 0.05);
  cursor: default;
}
/* 卡片 XP 细条（§4.1：点卡片能看到进度条动） */
.xp-strip {
  margin-top: 9px;
  height: 4px;
  border-radius: 999px;
  background: rgba(255, 244, 230, 0.08);
  overflow: hidden;
}
.xp-strip-fill {
  height: 100%;
  border-radius: 999px;
  background: linear-gradient(90deg, #ffc531, #ff7d1f);
  box-shadow: 0 0 8px rgba(255, 140, 40, 0.55);
  transition: width 0.4s ease;
}

/* ---------- 成长面板（§4.2） ---------- */
.panel-head {
  flex: none;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 16px 16px 10px;
}
.lv-badge {
  background: linear-gradient(180deg, #ffb35c, #ff7d1f);
  color: #2b1403;
  border-radius: 8px;
  padding: 3px 9px;
  font-size: 12px;
  font-weight: 700;
  letter-spacing: 0.02em;
  white-space: nowrap;
  box-shadow: 0 4px 10px -5px rgba(255, 125, 31, 0.7);
}
.xp-bar {
  flex: 1;
  height: 7px;
  background: rgba(255, 244, 230, 0.08);
  border-radius: 999px;
  overflow: hidden;
}
.xp-fill {
  height: 100%;
  border-radius: 999px;
  background: linear-gradient(90deg, #ffc531, #ff7d1f);
  box-shadow: 0 0 8px rgba(255, 140, 40, 0.55);
  transition: width 0.4s ease;
}
.xp-num {
  font-size: 11.5px;
  color: var(--elf-ink-3);
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}
.panel-scroll {
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
  padding: 0 16px 16px;
}
.panel-meta {
  margin: 0;
  font-size: 12px;
  line-height: 1.6;
  color: var(--elf-ink-3);
}
.panel-sec {
  display: flex;
  align-items: center;
  gap: 7px;
  margin: 16px 0 8px;
  font-size: 11.5px;
  font-weight: 700;
  letter-spacing: 0.06em;
  color: var(--elf-ink-2);
}
/* 节标题前的橙色小方块标记（暗色徽章卡片风的节奏点） */
.panel-sec::before {
  content: '';
  width: 8px;
  height: 8px;
  border-radius: 3px;
  background: var(--elf-badge);
  box-shadow: 0 0 8px rgba(255, 150, 40, 0.6);
}
.interest-row {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 9px;
}
.interest-row.dim {
  opacity: 0.42;
}
.interest-name {
  width: 88px;
  font-size: 12.5px;
  color: var(--elf-ink);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.interest-bar {
  flex: 1;
  height: 6px;
  background: rgba(255, 244, 230, 0.08);
  border-radius: 999px;
  overflow: hidden;
}
.interest-fill {
  height: 100%;
  border-radius: 999px;
  background: linear-gradient(90deg, #ffc531, #ff9a3d);
}
.interest-fill.inferred {
  background: linear-gradient(90deg, #6fd8bf, #4fc3ae);
}
.interest-temp {
  width: 26px;
  text-align: right;
  font-size: 11.5px;
  color: var(--elf-ink-3);
  font-variant-numeric: tabular-nums;
}
.panel-card {
  margin-top: 2px;
  padding: 11px 13px;
  border-radius: 12px;
  background: rgba(255, 244, 230, 0.05);
  border: 1px solid var(--elf-line);
  font-size: 12.5px;
  line-height: 1.6;
  color: var(--elf-ink-2);
}

/* P1-5 品味档案卡：像小精灵手写的便签 */
.taste-card {
  background: linear-gradient(135deg, rgba(255, 170, 60, 0.13) 0%, rgba(255, 120, 40, 0.05) 100%);
  border-color: rgba(255, 170, 80, 0.28);
  font-style: italic;
  color: #ecd9c0;
}
.panel-card b {
  color: var(--elf-accent);
  font-size: 14px;
  font-weight: 700;
}
.trend-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.trend-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 10px;
  border-radius: 10px;
  background: rgba(255, 244, 230, 0.05);
  border: 1px solid var(--elf-line);
  cursor: pointer;
  transition: border-color 0.15s;
}
.trend-row:hover {
  border-color: var(--elf-accent);
}
.trend-rank {
  flex: none;
  width: 18px;
  height: 18px;
  display: grid;
  place-items: center;
  border-radius: 6px;
  background: var(--elf-badge);
  color: #3a2004;
  font-size: 11px;
  font-weight: 800;
  font-variant-numeric: tabular-nums;
  box-shadow: 0 2px 6px -2px rgba(255, 125, 31, 0.6);
}
.trend-title {
  flex: 1 1 auto;
  min-width: 0;
  font-size: 12.5px;
  color: var(--elf-ink);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.trend-meta {
  flex: none;
  max-width: 42%; /* §11.36：来源栏限宽省略——feed 标题再长也挤不爆标题（残字修复） */
  overflow: hidden;
  text-overflow: ellipsis;
  font-size: 11px;
  color: var(--elf-ink-3);
  white-space: nowrap;
}
.unlock-row {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.unlock {
  padding: 4px 9px;
  border-radius: 999px;
  border: 1px solid var(--elf-line-strong);
  background: rgba(255, 255, 255, 0.05);
  font-size: 11.5px;
  color: var(--elf-ink-3);
}
.unlock.got {
  color: var(--elf-accent);
  border-color: rgba(255, 154, 61, 0.45);
  background: rgba(255, 154, 61, 0.1);
}

/* ---------- 悬浮提示 / 语音字幕 ---------- */
.quip,
.os,
.voice-line {
  position: absolute;
  left: 50%;
  bottom: 33%;
  transform: translateX(-50%);
  max-width: 300px;
  padding: 9px 14px;
  border-radius: 14px;
  font-size: 12.5px;
  line-height: 1.5;
  text-align: center;
  z-index: 9;
  animation: pop-in 0.18s ease-out;
}
.quip {
  background: rgba(28, 22, 16, 0.94);
  color: var(--elf-ink-2);
  box-shadow:
    0 12px 26px -14px rgba(0, 0, 0, 0.7),
    0 0 0 1px rgba(255, 190, 120, 0.12);
}
/* §11.35 内心 OS：半透明斜体虚线框，一眼区分"心里话"和"对你说的话" */
.os {
  background: rgba(28, 22, 16, 0.78);
  border: 1.5px dashed rgba(255, 154, 61, 0.5);
  color: rgba(240, 220, 195, 0.8);
  font-style: italic;
}
.voice-line {
  background: rgba(28, 22, 16, 0.94);
  border: 1px solid rgba(255, 154, 61, 0.28);
  color: #f0dfc8;
}
@keyframes pop-in {
  from {
    opacity: 0;
    transform: translateX(-50%) translateY(6px) scale(0.97);
  }
  to {
    opacity: 1;
    transform: translateX(-50%) translateY(0) scale(1);
  }
}
/* §8.4/§11.28 语音开关已迁入设置面板（Settings.vue「语音对话」），主界面不再悬浮麦克风 */
</style>
