/**
 * 光团小精灵渲染实现（docs/05 §1：程序生成，零外部模型，捏脸=改参数）。
 *
 * 只需 pixi.min.js（UMD 版，同 live2d 方案的加载原因）。
 * 不透明外套使用本地 Canvas 纹理，五官、装饰与肢体留在可调关节上。
 * 动画（呼吸/眨眼/跳跃/口型/睡眠）由 ticker 状态机驱动。
 * 内部坐标系按 R=50 固定单位绘制，布局时整体缩放——resize 无需重建几何。
 */
import type { PetExpression, PetMood, PetRenderer } from './renderer'
import { DEFAULT_SPRITE, type SpriteConfig } from './renderer'
import { cutoutSkin, segmentLimbs, type SkinLimb } from './skinCutout'
import { DEFAULT_DAMPING, DEFAULT_STIFFNESS, stepSpring } from './spring'
import { DragPose, type PartKind } from './dragPose'
import { ROUND_AVATAR, type Avatar } from './avatar'
import { BODY_HEIGHT, BODY_ORIGIN_Y, BODY_WIDTH, FACE_EYE_Y, drawCompanionCoat, mixColor } from './companionArt'

/* eslint-disable @typescript-eslint/no-explicit-any */
declare global {
  interface Window {
    PIXI?: any
  }
}

let pixiPromise: Promise<any> | null = null

function loadPixi(): Promise<any> {
  pixiPromise ??= new Promise((resolve, reject) => {
    if (window.PIXI) return resolve(window.PIXI)
    const s = document.createElement('script')
    s.src = 'elf://model/pixi.min.js'
    s.onload = () => (window.PIXI ? resolve(window.PIXI) : reject(new Error('PIXI 未就位')))
    s.onerror = () => reject(new Error('脚本加载失败: pixi.min.js'))
    document.head.appendChild(s)
  })
  return pixiPromise
}

const R = 50 // 内部单位半径
const FIGURE_BOX = 200
const FOOT_PADDING = 30
const EYE_GAP = 13
const DARK = 0x242b39
const SIZE_FACTOR: Record<SpriteConfig['size'], number> = { s: 0.85, m: 1, l: 1.15 }
/** skin-3 嘴线位置（图高比例）：胸像/正比大头的嘴基本都在中线附近，以下半幅下拉模拟开口。 */
const SKIN_JAW_FRAC = 0.5
const SKIN_JAW_DROP = 9 // 开口最大下拉（内部单位，R=50 基准）

/** §8.3 情绪姿态参数（docs/03 §2.2）：驱动待机动作频率与表情。
 * breathPeriod 呼吸周期(s)；breathAmp 呼吸幅度倍率；blinkEvery 眨眼间隔区间(ms)；
 * eyeDroop 眼睑下垂量(0=全开 1=全闭)；glowBase 光晕基准 alpha；wobble 思考轻颤。 */
const MOOD_PARAMS: Record<
  PetMood,
  {
    breathPeriod: number
    breathAmp: number
    blinkEvery: [number, number]
    eyeDroop: number
    glowBase: number
    wobble: boolean
  }
> = {
  idle: { breathPeriod: 3, breathAmp: 1, blinkEvery: [2000, 4000], eyeDroop: 0, glowBase: 0.3, wobble: false },
  thinking: { breathPeriod: 2.4, breathAmp: 1, blinkEvery: [1800, 3200], eyeDroop: 0.15, glowBase: 0.34, wobble: true },
  excited: { breathPeriod: 1.8, breathAmp: 1.5, blinkEvery: [1200, 2400], eyeDroop: 0, glowBase: 0.42, wobble: false },
  sorry: { breathPeriod: 4.5, breathAmp: 0.6, blinkEvery: [3500, 6000], eyeDroop: 0.45, glowBase: 0.2, wobble: false },
  sleepy: { breathPeriod: 6, breathAmp: 0.7, blinkEvery: [6000, 9000], eyeDroop: 0.8, glowBase: 0.16, wobble: false },
}

/** §11.35 瞬时表情参数表：眉毛/嘴型/腮红/贴纸/眼睛眯合/身体倾角，与 mood 姿态正交叠加。
 * brow：none 无眉 | arch 弯月 | v 八字(委屈/害羞) | iv 倒八(怒/哼) | raise 高挑(惊讶)；
 * mouth：smile 微笑 | grin 大笑 | frown 下撇(怒) | pout 嘟嘴(赌气) | o 惊讶 | small 小笑；
 * sticker：none | anger💢 | heart❤ | note🎵 | burst 放射线 | sweat💧 | puff💨；
 * squint 眼睛纵向倍率（<1 眯眼，>1 略睁大）；tilt 哼气时身体倾角（rad，±=左右扭头）。 */
const EXPR_PARAMS: Record<
  PetExpression,
  {
    brow: 'none' | 'arch' | 'v' | 'iv' | 'raise'
    mouth: 'smile' | 'grin' | 'frown' | 'pout' | 'o' | 'small'
    blush: number
    sticker: 'none' | 'anger' | 'heart' | 'note' | 'burst' | 'sweat' | 'puff'
    squint: number
    tilt: number
  }
> = {
  normal: { brow: 'none', mouth: 'smile', blush: 0, sticker: 'none', squint: 1, tilt: 0 },
  happy: { brow: 'arch', mouth: 'grin', blush: 0.4, sticker: 'note', squint: 0.45, tilt: 0 },
  pout: { brow: 'iv', mouth: 'pout', blush: 0.6, sticker: 'puff', squint: 0.55, tilt: 0.08 }, // 气鼓鼓：倒八眉+鼓腮+嘟嘴+喷气，身体别过去（哼！）
  angry: { brow: 'iv', mouth: 'frown', blush: 0, sticker: 'anger', squint: 0.7, tilt: -0.08 },
  lovely: { brow: 'arch', mouth: 'grin', blush: 0.55, sticker: 'heart', squint: 0.45, tilt: 0 },
  surprised: { brow: 'raise', mouth: 'o', blush: 0, sticker: 'burst', squint: 1.1, tilt: 0 },
  shy: { brow: 'v', mouth: 'small', blush: 0.6, sticker: 'sweat', squint: 0.75, tilt: 0 },
}

export class SpritePetRenderer implements PetRenderer {
  private PIXI: any = null
  private app: any = null
  private root: any = null // 锚点=精灵底部中心，内部坐标 R=50
  private body: any = null // 呼吸/挤压缩放置于此
  private glow: any = null
  private eyes: any[] = []
  private pupils: any[] = [] // round 眼型的瞳孔（视线偏移）
  private mouth: any = null
  private brows: any[] = [] // §11.35 眉毛（normal 隐藏）
  private blushes: any[] = [] // §11.35 腮红
  private sticker: any = null // §11.35 情绪符号贴纸
  private accessory: any = null
  private cfg: SpriteConfig = DEFAULT_SPRITE
  private radialTexture: any = null
  private coatTexture: any = null
  private growth = 1 // 成长缩放（§4.1 升级变大；setModel 重建后仍生效）
  private skinTex: any = null // §11.37 用户上传皮肤的 PIXI 纹理（加载失败回落光团）
  private skinTexUrl: string | null = null // 抠图后的 dataURL（skin-2 肢体切分输入）
  private skinSprite: any = null // 皮肤精灵（命中检测要量它的实际显示尺寸）
  // skin-2 四肢子精灵：angle/vel=弹簧摆动状态，side=-1 左/+1 右，phase=待机相位错开
  private limbs: Array<{ sp: any; kind: PartKind; side: number; angle: number; vel: number; phase: number }> = []
  private skinJaw: any = null // skin-3 嘴线以下半幅精灵：speaking 时下拉产生开口
  private skinJawRestY = 0 // skinJaw 贴嘴线的静止 y（speak(false) 复位用）

  // 动画状态
  private speaking = false
  private blinkAt = 0 // 下次眨眼时刻（ms）
  private blinkStart = -1
  private hopStart = -1
  private pokeStart = -1
  private hintStart = -1
  private mouthOpen = 0
  private mouthEnergy = 0 // §8.4：boundary 事件脉冲能量（真实口型，衰减 ~160ms）
  private lastPulseAt = 0
  private prevNow = 0
  private look = { x: 0, y: 0 } // 视线目标（归一化 -1..1）
  private eyeOffset = { x: 0, y: 0 } // 平滑后的实际偏移
  private lastInteract = performance.now() // 与 tick 同基准
  private mood: PetMood = 'idle'
  private expr: PetExpression = 'normal' // §11.35 当前瞬时表情
  private exprStart = -1 // 表情开始时刻（贴纸弹入动画基准）
  private exprEnd = 0 // 表情回落时刻
  private blushTarget = 0 // 腮红目标透明度（tick 内平滑趋近）
  private tiltTarget = 0 // 表情身体倾角目标（哼气扭头）
  private tiltCur = 0 // 平滑后的实际倾角
  private idleAct: 'stretch' | 'look' | 'nod' | 'sway' = 'stretch' // §11.35 随机小动作
  private idleActStart = -1
  private idleAt = 0 // 下次小动作候选时刻
  private readonly dragPose = new DragPose()
  // drag-1 落地挤压：投掷落地/撞边时的衰减振荡形变（landT<0 空闲，0~1 播放中）
  private landT = -1
  private landImpact = 0
  private walkStart = -1

  async init(canvas: HTMLCanvasElement): Promise<void> {
    this.PIXI = await loadPixi()
    this.app = new this.PIXI.Application({
      view: canvas,
      transparent: true,
      antialias: true,
      autoDensity: true,
      resolution: window.devicePixelRatio || 1,
      resizeTo: canvas.parentElement ?? window,
    })
    this.radialTexture = this.makeRadialTexture()
    this.app.ticker.add(this.tick)
    window.addEventListener('resize', this.layout)
  }

  async setModel(configJson: string): Promise<void> {
    if (!this.app) return
    try {
      this.cfg = { ...DEFAULT_SPRITE, ...JSON.parse(configJson) }
    } catch {
      this.cfg = DEFAULT_SPRITE
    }
    if (this.cfg.skin) await this.loadSkinTexture(this.cfg.skin) // §11.37
    if (this.root) {
      this.app.stage.removeChild(this.root)
      this.root.destroy({ children: true })
    }
    this.coatTexture?.destroy(true)
    this.coatTexture = null
    await this.build()
    this.layout()
    this.touch()
    // 注意：tick 内全部用 performance.now()，这里必须同基准（曾误用 Date.now() 导致永不眨眼）
    this.blinkAt = performance.now() + 1500 + Math.random() * 2500
  }

  lookAt(x: number, y: number): void {
    if (!this.root) return
    const w = this.app.renderer.width / this.app.renderer.resolution
    const h = this.app.renderer.height / this.app.renderer.resolution
    // 视线相对精灵中心的归一化偏移
    this.look.x = Math.max(-1, Math.min(1, (x - this.root.x) / (w * 0.35)))
    this.look.y = Math.max(-1, Math.min(1, (y - (this.root.y + FACE_EYE_Y * this.root.scale.y)) / (h * 0.35)))
    this.touch()
  }

  hitTest(x: number, y: number): boolean {
    if (!this.root) return false
    // §11.37 皮肤模式：按图片实际显示矩形命中（含 6px 宽容），光团模式沿用圆形
    if (this.skinSprite) {
      const s = this.root.scale.x
      const hw = (this.skinSprite.width * s) / 2
      const hh = this.skinSprite.height * s
      const dx = x - this.root.x
      const dy = y - this.root.y
      return Math.abs(dx) <= hw + 6 && dy <= 6 && dy >= -hh - 6
    }
    const s = this.root.scale.x
    const avatar = this.cfg.avatar
    const scaleX = s * this.body.scale.x
    const scaleY = s * this.body.scale.y
    const dx = (x - this.root.x - this.body.x * s) / scaleX
    const dy = (y - this.root.y - this.body.y * s) / scaleY
    if ((dx / 49) ** 2 + ((dy + 46) / 46) ** 2 <= 1) return true
    const earHeight = 39 * (avatar?.ears ?? 0.69)
    return earHeight > 4 && dy < -71 && dy >= -75 - earHeight && Math.abs(Math.abs(dx) - 26) < 13
  }

  attract(): void {
    this.touch()
    if (this.hopStart < 0) this.hopStart = performance.now()
  }

  hint(): void {
    this.touch()
    if (this.hintStart < 0) this.hintStart = performance.now()
  }

  speak(on: boolean): void {
    this.touch()
    this.speaking = on
    if (!on) {
      this.mouthOpen = 0
      this.mouthEnergy = 0
      if (this.skinJaw) this.skinJaw.y = this.skinJawRestY
      this.drawMouth()
    }
  }

  /** §8.4 真实口型：TTS boundary（词/句边界）事件脉冲；600ms 无脉冲回退正弦兜底。 */
  mouthPulse(): void {
    this.mouthEnergy = 1
    this.lastPulseAt = performance.now()
    if (this.speaking) this.touch()
  }

  walk(): void {
    this.touch()
    this.walkStart = performance.now()
  }

  poke(): void {
    this.touch()
    this.pokeStart = performance.now()
  }

  grab(point: number): void {
    this.dragPose.start(point)
    this.idleActStart = -1
    this.hopStart = -1
    this.landT = -1
    this.touch()
  }

  dragVelocity(vx: number, vy: number): void {
    this.dragPose.move(vx, vy)
  }

  releaseGrab(vx = 0, vy = 0): void {
    this.dragPose.release(vx, vy)
    this.touch()
  }

  /** drag-1 落地挤压：投掷落地/撞边触发，impact 0.1~0.5 决定形变幅度。 */
  land(impact: number): void {
    this.landImpact = Math.max(0.1, Math.min(0.5, impact))
    this.landT = 0
    for (const lb of this.limbs) {
      const direction = lb.kind === 'arm' ? -lb.side : lb.side
      lb.vel += direction * this.landImpact * (lb.kind === 'ear' ? 20 : 13)
    }
    this.touch()
  }

  /** §8.3 mood.set 下行：切换情绪姿态参数（未知值忽略，防脏数据打崩渲染）。 */
  setMood(mood: PetMood): void {
    if (mood in MOOD_PARAMS && mood !== this.mood) {
      this.mood = mood
      this.blinkAt = Math.min(this.blinkAt, performance.now() + MOOD_PARAMS[mood].blinkEvery[0])
    }
  }

  /** §11.35 expression.set 下行（壳内事件也可直调）：瞬时表情，durationMs 后自动回落 normal。 */
  setExpression(expr: PetExpression, durationMs = 1500): void {
    if (!(expr in EXPR_PARAMS)) return // 未知值忽略，防脏数据
    this.touch()
    this.exprStart = performance.now()
    this.exprEnd = expr === 'normal' ? 0 : this.exprStart + durationMs
    this.applyExpr(expr)
  }

  dispose(): void {
    this.app?.ticker.remove(this.tick)
    this.coatTexture?.destroy(true)
    this.coatTexture = null
    window.removeEventListener('resize', this.layout)
    this.app?.destroy(true)
    this.app = null
  }

  // ---- 构建 ----

  /** §11.37 预载用户皮肤图：先本地抠人物（skin-1，失败静默回落原图）→ Image → PIXI.Texture。 */
  private async loadSkinTexture(url: string): Promise<void> {
    let texUrl = url
    try {
      texUrl = await cutoutSkin(url)
    } catch (e) {
      console.warn('[skin] 抠图失败，回落原图', e)
    }
    this.skinTexUrl = texUrl
    return new Promise((resolve) => {
      const img = new Image()
      img.onload = () => {
        this.skinTex = this.PIXI.Texture.from(img)
        resolve()
      }
      img.onerror = () => {
        this.skinTex = null
        resolve()
      }
      img.src = texUrl
    })
  }

  /** 白色径向渐变纹理（亮核→透明），tint 后即任意颜色发光体。 */
  private makeRadialTexture(): any {
    const c = document.createElement('canvas')
    c.width = c.height = 128
    const ctx = c.getContext('2d')!
    const g = ctx.createRadialGradient(64, 64, 0, 64, 64, 64)
    g.addColorStop(0, 'rgba(255,255,255,1)')
    g.addColorStop(0.55, 'rgba(255,255,255,0.55)')
    g.addColorStop(1, 'rgba(255,255,255,0)')
    ctx.fillStyle = g
    ctx.fillRect(0, 0, 128, 128)
    return this.PIXI.Texture.from(c)
  }

  private makeGlowSprite(tint: number, size: number, alpha: number, additive = true): any {
    const s = new this.PIXI.Sprite(this.radialTexture)
    s.anchor.set(0.5)
    s.tint = tint
    s.width = s.height = size
    s.alpha = alpha
    if (additive) s.blendMode = this.PIXI.BLEND_MODES.ADD
    return s
  }

  private async build(): Promise<void> {
    const P = this.PIXI
    const color = /^#[a-f\d]{6}$/i.test(this.cfg.color) ? parseInt(this.cfg.color.slice(1), 16) : 0x7ec8ff
    this.eyes = []
    this.pupils = []
    this.skinSprite = null
    this.skinJaw = null
    this.limbs = []

    this.root = new P.Container()
    this.body = new P.Container()
    this.root.addChild(this.body)
    this.app.stage.addChild(this.root)

    // §11.37 用户皮肤：整图替代程序化光团（纹理加载失败自动回落光团分支）
    if (this.cfg.skin && this.skinTex) {
      await this.buildSkin()
      this.applyExpr(this.expr) // 重建后同步当前表情（皮肤模式只落身体倾角）
      return
    }

    // 翅膀（身体后层）
    if (this.cfg.accessory === 'wings') {
      for (const side of [-1, 1]) {
        const w = new P.Graphics()
        w.lineStyle(1.5, 0x515364).beginFill(0xf6e9cd)
        w.drawEllipse(0, 0, 17, 9).endFill()
        w.x = side * 43
        w.y = -30
        w.rotation = side * -0.55
        w.name = `wing${side}`
        this.body.addChild(w)
      }
    }

    this.addGroundShadow()
    const avatar = this.cfg.avatar ?? { ...ROUND_AVATAR, ears: 0.69, tail: 0.36 }
    this.buildAvatarParts(color, avatar)

    this.coatTexture = P.Texture.from(drawCompanionCoat(color))
    const coat = new P.Sprite(this.coatTexture)
    coat.anchor.set(0.5, 0)
    coat.y = BODY_ORIGIN_Y
    coat.width = BODY_WIDTH
    coat.height = BODY_HEIGHT
    this.body.addChild(coat)
    for (const limb of this.limbs) {
      if (limb.kind === 'arm') this.body.addChild(limb.sp)
    }

    const clasp = new P.Graphics()
    clasp.lineStyle(1.2, 0x745446).beginFill(0xe9b980)
      .moveTo(1, -19).quadraticCurveTo(10, -19, 12, -12)
      .quadraticCurveTo(5, -10, 1, -19).endFill()
    clasp.lineStyle(1, 0x865744).moveTo(1, -19).lineTo(8, -14.5)
    this.body.addChild(clasp)

    // 呆毛（本体之上、五官之下）
    if (this.cfg.accessory === 'antenna') {
      const a = new P.Graphics()
      a.lineStyle(3, DARK, 1)
      a.moveTo(0, -R * 1.82)
      a.quadraticCurveTo(4, -R * 2.35, 16, -R * 2.5)
      const tip = this.makeGlowSprite(color, 26, 0.9)
      tip.x = 16
      tip.y = -R * 2.5
      a.addChild(tip)
      a.name = 'antenna'
      this.body.addChild(a)
    }

    this.buildFace()

    // 光环（最上层，浮在头顶）
    if (this.cfg.accessory === 'halo') {
      const h = new P.Graphics()
      h.lineStyle(4.5, 0xffd66b, 1)
      h.drawEllipse(0, 0, 18, 7)
      const hg = this.makeGlowSprite(0xffd66b, 44, 0.35)
      h.addChild(hg)
      h.y = -R * 2.25
      h.rotation = -0.08
      h.name = 'halo'
      this.body.addChild(h)
    }
    this.accessory = this.body.children.find((c: any) => c.name)
    this.applyExpr(this.expr) // 重建后把当前表情重绘到新部件上
  }

  /** §11.37 皮肤模式构建：用户图片归一到光团相当的视觉体量（底部中心对齐站立点），
   * 脚下垫一圈柔光保住"桌宠"光感。身体级动画（呼吸浮沉/戳挤/跳跃/睡眠变暗/倾角）全部保留；
   * 五官级部件不存在，对应 tick 分支对空数组/空引用天然无操作。
   * skin-2：抠图 mask 列投影切出四肢，各为子精灵挂肩/髋枢轴，弹簧摆动见 tick。 */
  private async buildSkin(): Promise<void> {
    const P = this.PIXI
    // 面部引用清空：皮肤模式没有五官/贴纸/腮红（applyExpr 里它们对空引用安全）
    this.eyes = []
    this.pupils = []
    this.brows = []
    this.blushes = []
    this.mouth = null
    this.sticker = null
    this.accessory = null
    this.addGroundShadow()
    const sp = new P.Sprite(this.skinTex)
    sp.anchor.set(0.5, 1) // 底部中心=root 原点（站立点），与光团本体的贴地逻辑一致
    const boxH = R * 3.4 // 与光团"核心+头顶"相当的高度预算
    const boxW = R * 2.6
    const k = Math.min(boxH / sp.height, boxW / sp.width)
    sp.scale.set(k)
    this.body.addChild(sp)
    this.skinSprite = sp

    // skin-3 说话嘴型：整图本体不再渲染，沿嘴线（图高 SKIN_JAW_FRAC 处）用纹理帧切成上下两半。
    // 上半 anchor=底贴嘴线、下半 anchor=顶贴嘴线；说话时下半下移 → 嘴部像素被拉开 = 开口。
    // sp 保留作命中检测/皮肤模式标记（renderable=false，不参与绘制）。
    {
      const W = this.skinTex.width
      const H = this.skinTex.height
      const linePx = Math.round(H * SKIN_JAW_FRAC)
      const lineY = -k * (H - linePx) // 嘴线 body 纵坐标：切线以下有 (H-linePx) 像素，乘缩放得站立点上方距离
      sp.renderable = false
      const mkHalf = (y0: number, hh: number, anchorY: number) => {
        const t = new P.Texture(this.skinTex.baseTexture, new P.Rectangle(0, y0, W, hh))
        const s = new P.Sprite(t)
        s.anchor.set(0.5, anchorY)
        s.scale.set(k)
        s.position.set(0, lineY)
        this.body.addChild(s)
        return s
      }
      mkHalf(0, linePx, 1) // 上半：底边贴嘴线
      this.skinJaw = mkHalf(linePx, H - linePx, 0) // 下半：顶边贴嘴线，可下拉
      this.skinJawRestY = lineY
    }

    // skin-2 肢体切分：失败/切不出都静默回落 skin-1 整图活化（既有兜底哲学）
    if (!this.skinTexUrl) return
    try {
      const segs = await segmentLimbs(this.skinTexUrl)
      const iw = this.skinTex.width
      const ih = this.skinTex.height
      for (const seg of segs) this.attachLimb(seg, k, iw, ih)
    } catch (e) {
      console.warn('[skin] 肢体切分失败，回落整图', e)
    }
  }

  private addGroundShadow() {
    this.glow = this.makeGlowSprite(0x232830, 114, 0.16, false)
    this.glow.height = 21
    this.glow.y = 0
    this.root.addChildAt(this.glow, 0)
  }

  private buildAvatarParts(color: number, avatar: Avatar) {
    const add = (kind: PartKind, side: number, x: number, y: number, length: number, thickness: number) => {
      if (length <= 0) return
      const part = new this.PIXI.Graphics()
      const ear = kind === 'ear'
      const edge = mixColor(color, 0x283047, 0.73)
      if (ear) {
        const half = thickness / 2
        part.lineStyle(1.7, edge).beginFill(mixColor(color, 0xf8ebd9, 0.32))
          .moveTo(-half * 0.65, 1)
          .bezierCurveTo(-half * 1.05, -length * 0.58, -5, -length * 0.93, 2, -length)
          .bezierCurveTo(half * 1.25, -length * 0.78, half * 1.35, -length * 0.35, half * 0.72, 1)
          .closePath().endFill()
        if (length > 12) {
          part.lineStyle(0).beginFill(mixColor(0xd88b8c, color, 0.22))
            .drawEllipse(1.7, -length * 0.45, thickness * 0.23, length * 0.29).endFill()
        }
      } else {
        part.lineStyle(1.5, edge).beginFill(mixColor(color, 0x515477, kind === 'tail' ? 0.31 : 0.22))
          .drawRoundedRect(-thickness / 2, -thickness / 3, thickness, length + thickness / 3, thickness / 2).endFill()
        part.lineStyle(0).beginFill(0xffe6cf, 0.33)
          .drawEllipse(-1, length * 0.72, thickness * 0.28, thickness * 0.2).endFill()
      }
      part.position.set(x, y)
      part.rotation = side * (ear ? 0.21 : kind === 'tail' ? -1.08 : kind === 'arm' ? -0.37 : -0.12)
      this.body.addChild(part)
      this.limbs.push({ sp: part, kind, side, angle: part.rotation, vel: 0, phase: side === 1 ? Math.PI : 0 })
    }
    for (const side of [-1, 1]) {
      const earLength = avatar.kind === 'long-ears' ? 41 : 29
      add('ear', side, side * 23, -73, earLength * avatar.ears * (side === -1 ? 0.9 : 1), 16)
      add('arm', side, side * 37, -32, 24 * avatar.arms, 12)
      add('leg', side, side * 17, -13, 26 * avatar.legs, 16)
    }
    add('tail', 1, 32, -23, 29 * avatar.tail, 12)
  }

  /** Attach an imported image limb at the shoulder or hip pivot. */
  private attachLimb(seg: SkinLimb, k: number, imgW: number, imgH: number): void {
    const P = this.PIXI
    const lb = new P.Sprite(P.Texture.from(seg.canvas))
    const avatar = this.cfg.avatar
    const correction = seg.kind.startsWith('arm') ? avatar?.shoulder : avatar?.hip
    const pivotY = Math.max(seg.oy, Math.min(seg.oy + seg.canvas.height,
      seg.pivotY + (correction ?? 0) * imgH))
    lb.anchor.set(
      Math.min(1, Math.max(0, (seg.pivotX - seg.ox) / seg.canvas.width)),
      Math.min(1, Math.max(0, (pivotY - seg.oy) / seg.canvas.height))
    )
    lb.position.set((seg.pivotX - imgW * 0.5) * k, (pivotY - imgH) * k)
    lb.scale.set(k)
    this.body.addChild(lb) // 后加 = 画在躯干底图之上
    this.limbs.push({
      sp: lb,
      kind: seg.kind.startsWith('arm') ? 'arm' : 'leg',
      side: seg.kind.endsWith('L') ? -1 : 1,
      angle: 0,
      vel: 0,
      phase: Math.random() * Math.PI * 2,
    })
  }

  private buildFace(): void {
    const P = this.PIXI
    const eyeY = FACE_EYE_Y
    const eyeSpread = this.cfg.avatar?.eyes ?? 1
    for (const side of [-1, 1]) {
      const box = new P.Container()
      box.x = side * EYE_GAP * eyeSpread
      box.y = eyeY
      const g = new P.Graphics()
      if (this.cfg.eyes === 'round') {
        const pupil = new P.Graphics()
        pupil.beginFill(DARK).drawEllipse(0, 1, 4.8, 6.2).endFill()
        pupil.beginFill(0xfffcf4).drawCircle(-1.3, -1.1, 1.55).endFill()
        pupil.beginFill(0xeebaa1, 0.55).drawCircle(1.3, 3.1, 0.64).endFill()
        g.addChild(pupil)
        this.pupils.push(pupil)
      } else if (this.cfg.eyes === 'happy') {
        // 眯眯眼 ^^：上弯弧线
        g.lineStyle(3.5, DARK, 1)
        g.arc(0, 3, 7.5, Math.PI * 1.08, Math.PI * 1.92)
      } else {
        // 星形眼 ✦
        g.beginFill(DARK)
        const pts: number[] = []
        for (let i = 0; i < 8; i++) {
          const r = i % 2 === 0 ? 7.5 : 2.8
          const a = (i * Math.PI) / 4 - Math.PI / 2
          pts.push(Math.cos(a) * r, Math.sin(a) * r)
        }
        g.drawPolygon(pts).endFill()
        g.beginFill(0xffffff).drawCircle(1.8, -2.5, 1.6).endFill()
      }
      box.addChild(g)
      this.body.addChild(box)
      this.eyes.push(box)
    }
    this.mouth = new P.Graphics()
    this.mouth.y = -34
    this.body.addChild(this.mouth)

    // §11.35 表情部件：眉毛（默认清空隐藏）/腮红/情绪符号贴纸
    this.brows = []
    for (const side of [-1, 1]) {
      const b = new P.Graphics()
      b.x = side * EYE_GAP * eyeSpread
      b.y = FACE_EYE_Y - 14
      this.body.addChild(b)
      this.brows.push(b)
    }
    this.blushes = []
    for (const side of [-1, 1]) {
      const bl = this.makeGlowSprite(0xd96c79, 14, 0, false)
      bl.height = 8
      bl.x = side * 23
      bl.y = -38
      this.body.addChild(bl)
      this.blushes.push(bl)
    }
    this.sticker = new P.Container()
    this.sticker.x = R * 0.85
    this.sticker.y = -R * 2.1
    this.sticker.visible = false
    this.body.addChild(this.sticker)
    this.drawMouth()
  }

  private drawMouth(): void {
    const m = this.mouth
    if (!m) return
    m.clear()
    if (this.mouthOpen >= 0.08) {
      // 开口（说话，优先级高于表情嘴型）
      m.beginFill(DARK)
      m.drawEllipse(0, 1, 6.5, 2 + 7 * this.mouthOpen)
      m.endFill()
      return
    }
    // §11.35：闭嘴形态随瞬时表情变化
    const shape = EXPR_PARAMS[this.expr].mouth
    if (shape === 'grin') {
      // 大笑：填充半圆
      m.beginFill(DARK)
      m.arc(0, -3, 8, Math.PI * 0.12, Math.PI * 0.88)
      m.closePath()
      m.endFill()
    } else if (shape === 'frown') {
      // 下撇（生气，怒嘴：短而厚，末端上挑一点）
      m.lineStyle(3.6, DARK, 1)
      m.arc(0, 4.5, 6.5, Math.PI * 1.15, Math.PI * 1.85)
    } else if (shape === 'pout') {
      // 赌气嘟嘴：小而厚的下撇+下唇微鼓（配合倒八眉+鼓腮读作"哼"）
      m.lineStyle(3.6, DARK, 1)
      m.arc(0, 2.2, 4.4, Math.PI * 1.12, Math.PI * 1.88)
      m.lineStyle(2.2, DARK, 0.55)
      m.arc(0, 4.6, 3.4, Math.PI * 1.2, Math.PI * 1.8)
    } else if (shape === 'o') {
      // 惊讶 O 嘴
      m.beginFill(DARK)
      m.drawEllipse(0, 0, 4.2, 5.2)
      m.endFill()
    } else if (shape === 'small') {
      // 害羞小微笑
      m.lineStyle(2.6, DARK, 1)
      m.arc(0, -2, 5, Math.PI * 0.2, Math.PI * 0.8)
    } else {
      // 闭合微笑（默认）
      m.lineStyle({ width: 2.1, color: DARK, cap: this.PIXI.LINE_CAP.ROUND })
      m.moveTo(-5, -2).quadraticCurveTo(0, 4, 5, -2)
    }
  }

  // ---- 动画状态机（ticker） ----

  /** §11.35：把表情参数落到部件上（眉毛/嘴型/贴纸重绘 + 腮红目标值）。 */
  private applyExpr(expr: PetExpression): void {
    this.expr = expr
    this.blushTarget = EXPR_PARAMS[expr].blush
    this.tiltTarget = EXPR_PARAMS[expr].tilt
    this.drawBrows()
    this.drawSticker()
    if (!this.speaking) this.drawMouth()
  }

  /** 眉毛：每种表情一对眉形；normal 无眉——光团脸平日靠眼睛传情。
   * 实心渐变条（粗短四边形），比细线圆眼更像"眉"；k 按左右镜像。 */
  private drawBrows(): void {
    const type = EXPR_PARAMS[this.expr].brow
    for (let i = 0; i < this.brows.length; i++) {
      const g = this.brows[i]
      const side = i === 0 ? -1 : 1
      const k = -side // 左眉 k=1（内侧朝 +x），右眉镜像
      g.clear()
      g.rotation = 0
      if (type === 'none') continue
      if (type === 'arch') {
        g.lineStyle(3, DARK, 1)
        g.arc(0, 2, 7, Math.PI * 1.15, Math.PI * 1.85) // 弯月
        g.y = -R - 29
        continue
      }
      // 实心四边形条：外端细、内端粗；iv 内低外高（怒/哼），v/raise 内高外低
      const hi = type === 'iv'
      const xo = -8 * k,
        xi = 7 * k
      const pts = hi
        ? [xo, -1.3, xo, 1.3, xi, 4.6, xi, 0.6] // 倒八：内侧粗且压向眼睛
        : [xo, 0.6, xo, 4.2, xi, -2, xi, 1] // 八字/高挑：内侧细且扬起
      g.beginFill(DARK)
      g.drawPolygon(pts)
      g.endFill()
      g.y = type === 'iv' ? -R - 25.5 : type === 'v' ? -R - 27.5 : -R - 33
    }
  }

  /** 情绪符号贴纸：程序手绘漫画情绪符号（💢/❤/🎵/放射线/💧/💨），挂在头侧上方。 */
  private drawSticker(): void {
    const s = this.sticker
    if (!s) return
    for (const c of s.removeChildren()) c.destroy()
    const type = EXPR_PARAMS[this.expr].sticker
    s.visible = type !== 'none'
    if (!s.visible) return
    const P = this.PIXI
    const g = new P.Graphics()
    if (type === 'anger') {
      // 💢 四瓣怒气符号
      for (let i = 0; i < 4; i++) {
        const a = (i * Math.PI) / 2 + Math.PI / 4
        const tx = Math.cos(a) * 12
        const ty = Math.sin(a) * 12
        const px = Math.cos(a + Math.PI / 2) * 3.6
        const py = Math.sin(a + Math.PI / 2) * 3.6
        const bx = Math.cos(a) * 3
        const by = Math.sin(a) * 3
        g.beginFill(0xff5566)
        g.drawPolygon([bx + px, by + py, tx, ty, bx - px, by - py])
        g.endFill()
      }
    } else if (type === 'heart') {
      g.beginFill(0xff6b8a)
      g.drawCircle(-3.2, -2.2, 4)
      g.drawCircle(3.2, -2.2, 4)
      g.drawPolygon([-6.7, -1, 6.7, -1, 0, 7.5])
      g.endFill()
      g.beginFill(0xffffff, 0.85)
      g.drawCircle(-3, -3.4, 1.3)
      g.endFill()
    } else if (type === 'note') {
      g.beginFill(0xffd66b)
      g.drawEllipse(-2.5, 6, 4.4, 3.2) // 符头
      g.endFill()
      g.lineStyle(2.6, 0xffd66b, 1)
      g.moveTo(1.6, 6)
      g.lineTo(1.6, -8.5) // 符杆
      g.moveTo(1.6, -8.5)
      g.quadraticCurveTo(6.5, -7.5, 7, -2) // 符尾
    } else if (type === 'burst') {
      // 惊讶放射线（漫画三短线）
      g.lineStyle(3, 0xffe08a, 1)
      for (const a of [-Math.PI / 2 - 0.55, -Math.PI / 2, -Math.PI / 2 + 0.55]) {
        g.moveTo(Math.cos(a) * 4.5, Math.sin(a) * 4.5)
        g.lineTo(Math.cos(a) * 12, Math.sin(a) * 12)
      }
    } else if (type === 'sweat') {
      // 💧 汗滴
      g.beginFill(0x9fd8ff)
      g.drawCircle(0, 3, 5)
      g.drawPolygon([-4.7, 1.2, 4.7, 1.2, 0, -9])
      g.endFill()
      g.beginFill(0xffffff, 0.8)
      g.drawCircle(-1.6, 2, 1.4)
      g.endFill()
    } else {
      // puff 💨 哼气小云：实心蓬松白云（大小叠加圆）+ 三颗渐小飘点，尾巴朝外
      const WHITE = 0xffffff
      g.beginFill(WHITE, 0.95)
      g.drawCircle(-2.5, 2.6, 4.6)
      g.drawCircle(3, 3, 3.6)
      g.drawCircle(0.6, -2, 4)
      g.drawCircle(-4.5, -0.5, 3)
      g.endFill()
      g.beginFill(WHITE, 0.7)
      g.drawCircle(8.5, -2, 1.9)
      g.drawCircle(11.5, -4.2, 1.4)
      g.drawCircle(14.2, -6, 1)
      g.endFill()
    }
    s.addChild(g)
  }

  private tick = (): void => {
    if (!this.root || !this.body) return
    const now = performance.now()
    const dt = this.prevNow > 0 ? (now - this.prevNow) / 1000 : 0.016
    this.prevNow = now
    const t = now / 1000
    const mp = MOOD_PARAMS[this.mood]

    // Keep the coat opaque, including when the character dozes off.
    const sleeping = now - this.lastInteract > 120_000
    const period = sleeping ? 6 : mp.breathPeriod

    // 呼吸（果冻感：X/Y 反相 + 轻微浮沉，幅度要肉眼可见；§8.3 情绪倍率）
    const br = Math.sin((t * 2 * Math.PI) / period)
    let sx = 1 + 0.045 * mp.breathAmp * br
    let sy = 1 - 0.05 * mp.breathAmp * br
    let yOff = 2.2 * mp.breathAmp * Math.cos((t * 2 * Math.PI) / period) // 吸气略上浮

    // 跳跃（attract）：两连跳 + 落地挤压
    if (this.hopStart >= 0) {
      const p = (now - this.hopStart) / 780 // 两跳共 780ms
      if (p >= 1) {
        this.hopStart = -1
      } else {
        const ph = p < 0.55 ? p / 0.55 : (p - 0.55) / 0.45
        const amp = p < 0.55 ? 30 : 16
        yOff -= Math.abs(Math.sin(ph * Math.PI)) * amp // 叠加在呼吸浮沉上
        const land = Math.abs(ph * Math.PI - Math.PI) > Math.PI * 0.82 // 接近落地
        if (land) {
          sx *= 1.12
          sy *= 0.85
        }
      }
    }

    // 戳一戳：挤压回弹
    if (this.pokeStart >= 0) {
      const p = (now - this.pokeStart) / 420
      if (p >= 1) {
        this.pokeStart = -1
      } else {
        const s = Math.sin(p * Math.PI)
        sx *= 1 + 0.28 * s
        sy *= 1 - 0.3 * s
      }
    }

    // §11.35 随机小动作库：待机 25s 后每 30~90s 自发一个（灵气来源），说话/交互时不触发
    let actDroop = 0
    let actGlow = 0
    if (
      this.idleActStart < 0 &&
      !this.dragPose.held &&
      !this.speaking &&
      now - this.lastInteract > 25_000 &&
      now > this.idleAt
    ) {
      // 快睡着（>90s 无交互）时更容易打瞌睡
      const drowsy = now - this.lastInteract > 90_000
      const kinds: Array<'stretch' | 'look' | 'nod' | 'sway'> = drowsy
        ? ['nod', 'nod', 'sway']
        : ['stretch', 'look', 'sway', 'nod']
      this.idleAct = kinds[(Math.random() * kinds.length) | 0]
      this.idleActStart = now
    }
    // 表情身体倾角（哼气扭头）：平滑趋近目标，叠加在小动作之上
    this.tiltCur += (this.tiltTarget - this.tiltCur) * 0.12
    this.dragPose.step(dt)
    const drag = this.dragPose
    this.body.rotation = this.tiltCur + drag.tilt
    // Arms, legs and ears have different targets before the spring step.
    const avatar = this.cfg.avatar
    const rig = !this.skinSprite
    const walking = this.walkStart < 0 ? 0 : (now - this.walkStart) / 1500
    if (walking >= 1) this.walkStart = -1
    const step = walking > 0 && walking < 1 ? Math.sin(walking * Math.PI * 8) * 0.57 : 0
    if (this.limbs.length) {
      const sdt = Math.min(dt, 0.033)
      for (const lb of this.limbs) {
        // skin-2 摆幅：±0.16~0.18 rad（≈10°）肉眼可辨；再小真机无感（用户实测反馈）
        const sway = Math.sin(t * 1.9 + lb.phase) * (lb.kind === 'arm' ? 0.16 : 0.08)
        const flop = drag.angle(lb.kind, lb.side, t, lb.phase)
        const talk =
          lb.kind === 'arm' && this.speaking
            ? Math.sin(t * 11 + lb.phase) * 0.28 * (0.3 + this.mouthOpen)
            : 0
        const rest = rig
          ? lb.side * (lb.kind === 'ear' ? 0.21 : lb.kind === 'tail' ? -1.08 : lb.kind === 'arm' ? -0.37 : -0.12)
          : lb.side * (lb.kind === 'arm' ? 0.18 : 0.08)
        const kick = rig && lb.kind === 'leg' && this.hopStart >= 0 ? 0.4 * Math.sin(t * 15 + lb.phase) : 0
        const stride = lb.kind === 'leg' ? lb.side * step : lb.kind === 'arm' ? lb.side * -step * 0.6 : 0
        const target = rest + sway + flop + talk + kick + stride
        stepSpring(lb, target, avatar?.stiffness ?? DEFAULT_STIFFNESS, avatar?.damping ?? DEFAULT_DAMPING, sdt)
        lb.sp.rotation = lb.angle
      }
    }
    if (this.idleActStart >= 0) {
      const dur = { stretch: 1500, look: 1700, nod: 2100, sway: 2600 }[this.idleAct]
      const p = (now - this.idleActStart) / dur
      if (p >= 1) {
        this.idleActStart = -1
        this.idleAt = now + 30_000 + Math.random() * 60_000
      } else if (this.idleAct === 'stretch') {
        // 伸懒腰：拔高→回落，光晕随之一亮
        const k = Math.sin(Math.min(p / 0.72, 1) * Math.PI)
        sx *= 1 - 0.1 * k
        sy *= 1 + 0.16 * k
        yOff -= 6 * k
        actGlow = 0.15 * k
      } else if (this.idleAct === 'look') {
        // 张望：左右看两趟（直接写视线目标，鼠标一移动即被 lookAt 接管）
        this.look.x = Math.sin(p * Math.PI * 4) * 0.85 * (1 - p * 0.4)
      } else if (this.idleAct === 'nod') {
        // 瞌睡点头：眼睑渐垂 + 脑袋点两下
        actDroop = 0.5 * Math.max(0, Math.sin(p * Math.PI))
        yOff += Math.max(0, Math.sin(p * Math.PI * 2)) * 5
      } else {
        // sway 摇摆：身体轻轻左右晃
        this.body.rotation = this.tiltCur + drag.tilt + Math.sin(p * Math.PI * 3) * 0.07 * (1 - p)
      }
    }

    sx *= 1 - drag.lift * 0.10
    sy *= 1 + drag.lift * 0.19 + Math.abs(drag.y) * 0.055
    yOff -= drag.lift * 21

    // drag-1 落地挤压：衰减振荡（先扁后回弹两下），与拖拽拉伸共用 scale 管线
    if (this.landT >= 0) {
      this.landT += dt / 0.45
      if (this.landT >= 1) {
        this.landT = -1
      } else {
        const osc = Math.sin(this.landT * Math.PI * 2.4) * (1 - this.landT)
        sx *= 1 + this.landImpact * 0.7 * osc
        sy *= 1 - this.landImpact * osc
      }
    }

    if (rig && avatar) {
      sx *= avatar.width
      sy *= avatar.height
    }
    this.body.scale.set(sx, sy)
    this.body.y = yOff
    this.glow.alpha = (0.11 + 0.18 * (mp.glowBase + actGlow) + 0.025 * Math.sin(t * 2 * Math.PI / period)) * (1 - drag.lift * 0.72)

    // §11.36 戳一戳灵气：挤压回弹的同时荡出一圈光晕涟漪
    if (this.pokeStart >= 0) {
      const pp = (now - this.pokeStart) / 420
      if (pp < 1) this.glow.alpha += 0.055 * Math.sin(pp * Math.PI)
    }

    // L1 持有提示：1.8s 内三次衰减光脉冲（docs/05 §2.5）
    if (this.hintStart >= 0) {
      const p = (now - this.hintStart) / 1800
      if (p >= 1) {
        this.hintStart = -1
      } else {
        const pulse = Math.sin(p * Math.PI) * Math.abs(Math.sin(p * 3 * Math.PI))
        this.glow.alpha += 0.09 * pulse
        this.body.y -= 3.8 * pulse
      }
    }

    // 说话：身体前倾 + 口型（§8.4：boundary 脉冲驱动真实口型；不发 boundary 的语音回退正弦）
    this.body.x = (mp.wobble ? Math.sin(t * 7) * 2.5 : 0) +
      (walking > 0 && walking < 1 ? Math.sin(walking * Math.PI) * 34 : 0)
    if (this.speaking) {
      this.mouthEnergy *= Math.exp(-dt / 0.16)
      this.mouthOpen =
        now - this.lastPulseAt < 600
          ? Math.max(this.mouthEnergy, 0.12)
          : Math.abs(Math.sin(t * 9)) // 兜底：无 boundary 事件的系统语音
      if (this.skinSprite) {
        // skin-1 整图活化：无部件嘴型，用口型脉冲驱动身体弹性形变（上下轻颤模仿说话）
        const p = this.mouthOpen
        this.body.scale.set(sx * (1 - 0.02 * p), sy * (1 + 0.04 * p))
        // skin-3 嘴线以下半幅下拉 → 嘴部像素拉开 = 开口
        if (this.skinJaw) this.skinJaw.y = this.skinJawRestY + p * SKIN_JAW_DROP
      } else {
        this.body.scale.set(sx * 1.04, sy * 1.04)
      }
      this.drawMouth()
    }

    // 眨眼 + 眼睑下垂（§8.3 情绪表情：sorry 半耷拉、sleepy 近全闭）
    if (this.blinkStart < 0 && now > this.blinkAt) this.blinkStart = now
    let blinkK = 1
    if (this.blinkStart >= 0) {
      const p = (now - this.blinkStart) / 240
      if (p >= 1) {
        this.blinkStart = -1
        const [bMin, bMax] = mp.blinkEvery
        this.blinkAt = now + bMin + Math.random() * (bMax - bMin)
      } else {
        blinkK = 1 - 0.92 * Math.sin(p * Math.PI)
      }
    }
    // §11.35 表情眯合：squint<1 眯眼（开心/委屈），>1 略睁大（惊讶）；actDroop=瞌睡点头垂眼
    const sq = EXPR_PARAMS[this.expr].squint
    const sleepEyes = sleeping ? 0.65 : 1
    this.eyes.forEach((e) =>
      e.scale.set(sq > 1 ? sq : 1, (1 - mp.eyeDroop) * (1 - actDroop) * blinkK * Math.min(sq, 1) * sleepEyes)
    )

    // §11.35 表情生命周期：到时回落 normal；腮红平滑趋近目标；贴纸弹入+浮动
    if (this.expr !== 'normal' && now > this.exprEnd) this.applyExpr('normal')
    for (const b of this.blushes) b.alpha += (this.blushTarget - b.alpha) * 0.15
    if (this.sticker?.visible) {
      const p = this.exprStart >= 0 ? Math.min(1, (now - this.exprStart) / 240) : 1
      let sc = 1 + 2.7 * Math.pow(p - 1, 3) + 1.7 * Math.pow(p - 1, 2) // easeOutBack 弹入
      if (this.expr === 'lovely') sc *= 1 + 0.1 * Math.max(0, Math.sin(t * 6)) // 爱心心跳
      this.sticker.scale.set(Math.max(0.01, sc))
      this.sticker.y = -R * 2.1 + (p >= 1 ? Math.sin(t * 3) * 3 : 0)
      this.sticker.x = R * 0.85 + (this.expr === 'angry' ? Math.sin(t * 35) * 1.6 : 0)
      this.sticker.rotation =
        this.expr === 'angry' ? Math.sin(t * 30) * 0.08 : Math.sin(t * 2.2) * 0.06
    }

    // 视线：瞳孔/眼眶平滑跟随
    this.eyeOffset.x += (this.look.x * 3.2 - this.eyeOffset.x) * 0.12
    this.eyeOffset.y += (this.look.y * 2.2 - this.eyeOffset.y) * 0.12
    for (const pupil of this.pupils) {
      pupil.x = this.eyeOffset.x
      pupil.y = this.eyeOffset.y
    }
    if (this.pupils.length === 0) {
      // 非 round 眼型：整个眼眶轻微偏移
      for (const e of this.eyes) {
        e.x = (e.x < 0 ? -1 : 1) * EYE_GAP * (avatar?.eyes ?? 1) + this.eyeOffset.x * 0.6
        e.y = FACE_EYE_Y + this.eyeOffset.y * 0.6
      }
    }
    this.root.rotation = this.eyeOffset.x * 0.012

    // 装饰微动
    if (this.accessory) {
      if (this.accessory.name === 'halo') {
        this.accessory.y = -R * 2.25 + Math.sin(t * 2.2) * 3
        this.accessory.rotation = -0.08 + Math.sin(t * 1.3) * 0.06
      } else if (this.accessory.name === 'antenna') {
        this.accessory.rotation = Math.sin(t * 2) * 0.12
      }
    }
    const wingL = this.body.children.find((c: any) => c.name === 'wing-1')
    const wingR = this.body.children.find((c: any) => c.name === 'wing1')
    if (wingL && wingR) {
      const flap = Math.sin(t * 5) * 0.18
      wingL.rotation = 0.55 + flap
      wingR.rotation = -0.55 - flap
    }
  }

  private touch(): void {
    this.lastInteract = performance.now()
  }

  /** 成长缩放：升级时光团渐进变大（§4.1），钳制 [0.5, 1.5] 防异常输入。 */
  setGrowth(factor: number): void {
    this.growth = Math.max(0.5, Math.min(1.5, factor))
    this.layout()
  }

  private layout = (): void => {
    if (!this.app || !this.root) return
    const w = this.app.renderer.width / this.app.renderer.resolution
    const h = this.app.renderer.height / this.app.renderer.resolution
    const usableHeight = Math.max(0, h - FOOT_PADDING)
    const avatar = this.cfg.avatar
    const ears = (avatar?.kind === 'long-ears' ? 41 : 29) * (avatar?.ears ?? 0.69)
    const accessoryHeight = this.cfg.accessory === 'antenna' ? 134 : this.cfg.accessory === 'halo' ? 123 : 0
    const coatHeight = Math.max(88, 73 + ears, accessoryHeight) * (avatar?.height ?? 1)
    const poseHeight = this.skinSprite?.height ?? coatHeight
    const preferredScale = (w * 0.47 / FIGURE_BOX) * SIZE_FACTOR[this.cfg.size] * this.growth
    const s = Math.min(preferredScale, usableHeight / (poseHeight + 45))
    this.root.scale.set(s)
    this.root.x = w / 2
    this.root.y = h - FOOT_PADDING
  }
}
