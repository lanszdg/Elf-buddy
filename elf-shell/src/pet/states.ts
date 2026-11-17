/**
 * §11.37 壳侧状态动作契约（对比 Codex 桌宠 9 状态表后的落地版）。
 *
 * Codex 把 Agent 状态显式映射到固定动画（idle/running/waving/jumping/failed/waiting/review），
 * 好处是行为可预测、可文档化。Elf Buddy 原先动作触发散在 App.vue 各事件里，这里收敛成一张表：
 * 每个状态 = 动作原语（hop/hint）+ 可选瞬时表情 + 可选临时 mood 姿态。
 *
 * 分层原则：
 * - brain 下发的 mood.set 是「基准姿态」（baseMood），长期有效；
 * - 状态契约的 mood 是「临时姿态」，ms 后自动回落 baseMood，不与 brain 打架；
 * - working 是唯一持续性状态：enter 后到 exit 前一直保持 thinking，显式配对使用。
 */
import type { PetExpression, PetMood } from './renderer'

/** 壳侧任务状态（对照 Codex：idle/running→working/waiting/review/failed/jumping→greet）。 */
export type PetState =
  | 'idle' // 待机：无动作
  | 'working' // 干活中：追问/语音应答进行中（持续性，须 exitState 配对）
  | 'waiting' // 等用户：L1 静默持有
  | 'greet' // 招呼：L2/L3 内容到达
  | 'review' // 被审阅：卡片被读/追问（dwell 已有反馈通道，不打扰）
  | 'failed' // 受挫：被无视/被拒
  | 'celebrate' // 庆祝：升级/配置生效

export interface StateContract {
  /** 进入状态时触发的动作原语。 */
  act?: 'hop' | 'hint'
  /** 伴随瞬时表情（走 §11.35 表情系统，durationMs 后自动回落 normal）。 */
  expr?: PetExpression
  /** 临时 mood 姿态：value 生效 ms 秒后回落 baseMood；ms=0 表示持续到 exitState（仅 working 用）。 */
  mood?: { value: PetMood; ms: number }
}

export const STATE_CONTRACT: Record<PetState, StateContract> = {
  idle: {},
  working: { mood: { value: 'thinking', ms: 0 } },
  waiting: { act: 'hint' },
  greet: { act: 'hop', expr: 'happy', mood: { value: 'excited', ms: 4000 } },
  review: {},
  failed: { expr: 'pout', mood: { value: 'sorry', ms: 6000 } },
  celebrate: { act: 'hop', expr: 'happy', mood: { value: 'excited', ms: 5000 } },
}
