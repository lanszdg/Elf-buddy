/**
 * 精灵形象渲染层抽象（docs/01 §6 M1 在场层）。
 * 当前实现：程序生成光团精灵（sprite.ts）；接口预留 Live2D/VRM 可插拔替代。
 * App.vue 只面向此接口，不感知渲染技术。
 */
export interface PetRenderer {
  /** 初始化渲染上下文（canvas 已挂载）。 */
  init(canvas: HTMLCanvasElement): Promise<void>
  /** 加载/切换形象：光团精灵传 JSON.stringify(SpriteConfig)。 */
  setModel(modelUrl: string): Promise<void>
  /** 视线跟随（canvas 内坐标）。 */
  lookAt(x: number, y: number): void
  /** 命中检测（canvas 内坐标）：仅精灵本体区域拦截点击，透明/光晕外沿放行。 */
  hitTest(x: number, y: number): boolean
  /** L1 静默持有提示：轻微发光（不跳不出声，等用户主动凑过来）。 */
  hint(): void
  /** L2 显眼化：吸引注意的动作（不发声）。 */
  attract(): void
  /** L3 开口期间的动作与口型；false 恢复常态。 */
  speak(on: boolean): void
  /** 用户点击宠物的即时反应。 */
  poke(): void
  /** skin-1 拖拽甩动：拖拽中每帧传入窗口位移（px），精灵据此倾侧/拉伸并弹簧回正。 */
  dragBy?(dx: number, dy: number): void
  /** drag-1 落地挤压：投掷落地/撞边触发（impact 0.1~0.5），衰减振荡一次。 */
  land?(impact: number): void
  /** 成长缩放（docs/05 §4.1：升级光团渐进变大，1=捏脸体型；不改用户选的 size 档）。 */
  setGrowth?(factor: number): void
  /** 情绪姿态（docs/03 §2.2 mood.set 下行）：驱动待机动作频率与表情参数。 */
  setMood?(mood: PetMood): void
  /** 瞬时表情（docs/05 §11.35 表情系统）：眉毛/嘴型/腮红/情绪符号贴纸，
   * 与 mood 姿态正交；durationMs 后自动回落 normal，默认 1500。 */
  setExpression?(expr: PetExpression, durationMs?: number): void
  /** 真实口型脉冲（docs/05 §8.4：TTS boundary 事件驱动，替代正弦模拟）。 */
  mouthPulse?(): void
  dispose(): void
}

/** docs/03 §2.2 五种情绪姿态。 */
export type PetMood = 'idle' | 'thinking' | 'excited' | 'sorry' | 'sleepy'

/** §11.35 瞬时表情：眉毛+嘴型+腮红+符号贴纸，事件驱动，定时回落。 */
export type PetExpression = 'normal' | 'happy' | 'pout' | 'angry' | 'lovely' | 'surprised' | 'shy'

/** 光团精灵捏脸参数（docs/05 §1.4）。 */
export interface SpriteConfig {
  /** 体色（hex）。 */
  color: string
  /** 眼型。 */
  eyes: 'round' | 'happy' | 'star'
  /** 装饰。 */
  accessory: 'none' | 'wings' | 'halo' | 'antenna'
  /** 体型。 */
  size: 's' | 'm' | 'l'
  /** §11.37 用户上传的皮肤图（dataURL）：设置后整图替代程序化光团本体；
   * 壳侧 localStorage 持有，不进 brain profile（防 base64 撑爆脑库）。 */
  skin?: string
}

export const DEFAULT_SPRITE: SpriteConfig = {
  color: '#7ec8ff',
  eyes: 'round',
  accessory: 'none',
  size: 'm',
}

/** onboarding 捏脸备选项。 */
export const SPRITE_OPTIONS = {
  colors: [
    { value: '#7ec8ff', name: '天空蓝' },
    { value: '#8ff0c8', name: '薄荷绿' },
    { value: '#ffb3c7', name: '杏粉' },
    { value: '#ffe08a', name: '暖黄' },
    { value: '#c9b0ff', name: '紫罗兰' },
    { value: '#ffc38f', name: '橙橘' },
  ],
  eyes: [
    { value: 'round', name: '圆圆眼' },
    { value: 'happy', name: '眯眯眼' },
    { value: 'star', name: '星星眼' },
  ] as const,
  accessories: [
    { value: 'none', name: '光溜溜' },
    { value: 'wings', name: '小翅膀' },
    { value: 'halo', name: '光环' },
    { value: 'antenna', name: '呆毛' },
  ] as const,
  sizes: [
    { value: 's', name: '小巧' },
    { value: 'm', name: '标准' },
    { value: 'l', name: '敦实' },
  ] as const,
}
