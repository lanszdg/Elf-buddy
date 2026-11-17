# Elf Buddy 数据模型

版本：v0.1（2026-10-10）｜早期设计基线。实际存储见 [README](../README.md)。

类型定义使用 TypeScript 描述（协议中立）；实现侧 elf-shell 用 TS，elf-brain 用 Python（pydantic 镜像定义）。所有时间为 Unix 毫秒。所有 id 为 ULID。

存储分布：
- **event.db**（SQLite + sqlite-vec）：L0 情景流、开口决策、内容库（本地、append-only 优先）
- **Graphiti**（FalkorDB 嵌入式）：L1 原子事实、L2 兴趣图谱（时态边）
- **profile**：L3 画像槽位（MVP 存 SQLite 表，Honcho 方案后置，见架构 M4 许可风险）
- **文件**：人设/立场/技能 Markdown（`%APPDATA%/ElfBuddy/persona/`、`skills/`）
- **digest.db**（SQLite）：晨报/精灵日记/成长汇报（正文 + offered_ts 投递状态，offered_ts NULL=未主动给出）

---

## 1. 情景流（L0）

### 1.1 Event —— 一切事件的根类型

所有交互、反馈、感知、决策事件 append-only 写入，是 Learning Loop 的唯一数据源。

```typescript
type EventType =
  // 感知事件（M2 上报）
  | 'state.changed'          // 用户状态机变更
  // 呈现与交互事件（M1 上报）
  | 'speak.shown'            // 通道 L1-L3 的内容已展示
  | 'speak.opened'           // 用户点开查看（L2 被点击 / L3 展开）
  | 'speak.consumed'         // 消费完成（看完/听完）
  | 'speak.dwell'            // 停留时长/回看（附 dwell_ms）
  | 'speak.followup'         // 用户主动追问（"还有呢？"）
  | 'speak.dismissed'        // 秒关/无视超时
  | 'speak.shared_to_elf'    // 用户反向分享内容给精灵（最强正信号）
  | 'user.message'           // 用户主动发起对话
  | 'user.correction'        // 用户纠偏（"我现在还是喜欢长的"）
  | 'user.mute'              // "现在别烦我"（附 duration）
  // 决策事件（M3 产生）
  | 'decision.made'          // 时机引擎每次通道决策（含评分快照）
  // 系统事件
  | 'content.ingested'       // 内容入库
  | 'speak.digest'           // 晨报/成长汇报已由精灵主动说出（payload: {digest_id, kind, date, week}；不进反馈学习/命中率窗口）
  | 'review.ran';            // Review 管线运行记录

interface Event<T = unknown> {
  id: string;
  type: EventType;
  ts: number;
  payload: T;                // 按 type 见下文各 payload
  // 溯源：该事件由哪条内容/哪次决策引发（可空）
  content_id?: string;
  decision_id?: string;
}
```

### 1.2 关键 payload

```typescript
interface StateChangedPayload {
  from: UserState; to: UserState;
  signals: {                 // 触发依据快照，供复盘
    idle_sec: number;
    foreground_app?: string; // 进程名，如 'Code.exe'（不存窗口标题原文，隐私）
    foreground_kind?: AppKind; // 'ide'|'browser'|'meeting'|'doc'|'media'|'other'
    meeting_detected: boolean;
    screen_locked: boolean;
  };
}

type UserState = 'DEEP_WORK' | 'BUSY' | 'TRANSITION' | 'IDLE' | 'AWAY' | 'DND';

interface DwellPayload { dwell_ms: number; completed: boolean; }
interface FollowupPayload { text: string; }       // 用户追问原文
interface CorrectionPayload { text: string; applied_fact_ids: string[]; }
interface MutePayload { duration_ms: number; }
```

---

## 2. 兴趣图谱（L1/L2，Graphiti）

Graphiti 原生提供 episode 溯源与时态边（valid_at/invalid_at，失效归档而非覆盖）。以下自定义属性叠加在节点/边上。

### 2.1 InterestNode —— 兴趣节点

```typescript
type InterestLevel = 'domain' | 'topic' | 'entity';
// 例：domain=独立游戏 → topic=像素风RPG → entity=《星露谷物语》

interface InterestNodeProps {
  name: string;
  level: InterestLevel;
  temperature: number;        // 0-1，温度 = 近14天交互频率的指数衰减函数，Review 管线每日重算
  first_seen_ts: number;
  last_active_ts: number;
  source: 'explicit' | 'inferred'; // 用户明说 vs 系统推断（隐性知识）
  confidence: number;         // 0-1，inferred 节点专用；explicit 恒为 1
}
```

### 2.2 InterestEdge —— 图谱边（含时态）

```typescript
interface InterestEdgeProps {
  relation: 'contains' | 'related_to' | 'chasing' | 'paused' | 'dropped';
  // chasing=正在追（会完结：剧集/赛季）；paused=温度冷却；dropped=用户明确放弃
  valid_from: number;
  valid_to?: number;          // 失效时间（Graphiti 时态，失效归档可查历史）
  confidence: number;
  source_event_ids: string[]; // episode 溯源
  note?: string;              // 一句可解释的话："你3月时说在追第二季"
}
```

### 2.3 MemoryFact —— 原子事实（非兴趣类）

```typescript
interface MemoryFact {
  id: string;
  subject: string;            // 'user' | 'elf'
  predicate: string;          // 'dislike_cilantro' | 'owns_camera' | ...
  object: string;
  valid_from: number; valid_to?: number;
  confidence: number; source_event_ids: string[];
  created_by: 'extractor' | 'user_correction';
}
```

---

## 3. 消费画像（L3）

```typescript
interface UserProfile {
  // 消费偏好槽位：全部带置信度+来源+更新时间； inferred 槽位可被 user.correction 改写
  slots: Record<string, ProfileSlot>;
  // 时机偏好（Review 管线从开口反馈统计而来）
  timing: TimingPreference;
  updated_ts: number;
}

interface ProfileSlot {
  key: string;                // 'length_pref'|'tone_pref'|'medium_pref'|'hot_ratio_ok'
  value: string;              // 'short'|'snarky'|'video'|...
  confidence: number;
  source: 'explicit' | 'inferred' | 'corrected';
  evidence_event_ids: string[];
  updated_ts: number;
}

interface TimingPreference {
  // 状态×时段的命中率矩阵，Review 管线维护；MVP 可为空走默认系数
  buckets: Record<string, {   // key: `${weekday}:${hour}` 粒度可再粗
    attempts: number; hits: number;
  }>;
  quiet_hours: { start: string; end: string }; // 默认 23:00-08:00，可配置
}
```

---

## 4. 关系账户

```typescript
interface RelationshipAccount {
  level: number;              // 亲密度等级（只增不减，成长不回退铁律）
  xp: number;                 // 累计正向互动经验
  // 信任记账（滑动窗口，近50次开口）
  window: { decision_id: string; hit: boolean; ts: number }[];
  hit_rate: number;           // window 内命中率，Review 管线重算
  quiet_period_until?: number;// 安静期截止时间（连续未命中触发）
  daily_l3_quota: number;     // 当前 L3 日配额（冷启动=3，随 hit_rate 在 [1,6] 浮动）
  l3_unlocked: boolean;       // 开口权限=成长解锁项；冷启动=false（前两周仅 L1/L2）
}
```

---

## 5. 内容域

### 5.1 ContentItem —— 内容库条目

```typescript
interface ContentItem {
  id: string;
  source: { adapter: string; url: string; title: string; published_ts: number; };
  kind: 'interest' | 'hot';   // 兴趣 / 热点（热点受 ≤30% 配额约束）
  body_ref: string;           // 正文存储位置（content.db 内 row id）
  tags: {
    domains: string[]; entities: string[];   // 打标产出，对齐兴趣图谱节点名
    quality: number;          // 0-1
    sentiment: 'snarky' | 'neutral' | 'deep' | 'listicle';
    time_sensitivity: 'breaking' | 'scheduled' | 'evergreen';
    scheduled_at?: number;    // scheduled 类（直播/发布会）的定点时间
  };
  embedding_id: string;       // sqlite-vec 行 id
  ingested_ts: number;
}
```

### 5.2 CandidateEntry —— 想说清单（时机引擎输入队列）

```typescript
interface CandidateEntry {
  id: string;
  content_id: string;
  content_value: number;      // 兴趣温度×匹配度（M7 精排分），0-1
  match_reason: string;       // LLM 精排生成的可解释理由："给你看这个是因为你上周…"
  urgency: {                  // 时效曲线
    type: 'breaking' | 'scheduled' | 'evergreen';
    decay_half_life_h: number;// 价值半衰期；evergreen 很长
    not_after?: number;       // 过期作废时间
  };
  freshness_ok: boolean;      // 用户是否疑似已知（检测到其在浏览相关内容则置 false 作废）
  status: 'queued' | 'held_l1' | 'shown' | 'expired' | 'sent_to_digest';
  narration?: Narration;      // 人格化讲述，首次展示前生成并缓存
  created_ts: number;
}

interface Narration {
  hook: string;               // L3 开场白："歇会儿？有个事你肯定想听——"
  script_short: string;       // ≤90秒播报稿/气泡短文
  script_deep?: string;       // 深度版（周末/晚间）
  stance_check_passed: boolean; // 立场记忆库一致性校验结果
}
```

### 5.3 SpeakDecision —— 开口决策记录（Learning Loop 核心数据）

```typescript
type Channel = 'L3' | 'L2' | 'L1' | 'L0';

interface SpeakDecision {
  id: string;
  candidate_id: string;
  channel: Channel;
  score_snapshot: {           // 白盒评分快照，可解释+可复盘
    content_value: number;
    state_factor: number;
    timing_factor: number;
    disturbance_cost: number;  // 今日已开口次数的递增惩罚
    trust_factor: number;
    final_score: number;
    thresholds: Record<Channel, number>;
  };
  user_state_at_decision: UserState;
  suppression_check: {        // 硬规则校验记录
    busy: boolean; quiet_hours: boolean; muted: boolean;
    consecutive_ignores: number; quota_left: number;
  };
  // 反馈与误差（RPE 原则：学习只发生在"意外"时，误差大的样本信息量最大）
  // 由反馈闭环从后续 speak.* 事件回填；pending = feedback 字段为空
  feedback?: {
    outcome: 'hit' | 'miss';        // 粗判，供关系账户命中率窗口（§4）使用
    expected_value: number;         // 0-1，决策时查归一化映射写入（见下"expected_value 归一化映射"）
    mapping_version: number;        // 决策时所用映射版本，供复盘映射演进前后的误差变化
    actual_value: number;           // 0-1，反馈信号梯度映射（见下表）
    prediction_error: number;       // |expected_value - actual_value|
    resolved_by_event_ids: string[];// 溯源：判定依据的 speak.* 事件
    resolved_ts: number;
  };
  decided_ts: number;
}
```

**actual_value 映射**（取反馈窗口内最强信号；L2 通道按点击与否映射 0/1）：

| 反馈信号 | actual_value |
|---|---|
| 跳过/无视超时（speak.dismissed） | 0 |
| 点开未看完（opened 无 consumed） | 0.25 |
| 看完/听完（speak.consumed） | 0.5 |
| 停留久/回看（speak.dwell 超阈值） | 0.75 |
| 主动追问（speak.followup） | 0.9 |
| 反向分享给精灵（speak.shared_to_elf） | 1.0 |

**expected_value 归一化映射（两阶段维护）**：

`final_score` 的原始量纲随系数学习持续漂移，不能直接当概率用；须经映射转为 0-1 接受概率估计（本质=决策分数的校准曲线）。映射参数存储于 `timing/coefficients.yaml` 的 `normalization` 节，时机引擎决策时加载查表，Review 管线负责维护：

```typescript
interface ExpectedValueMapping {
  version: number;               // 递增，Review 每次更新 +1；决策时写入 feedback.mapping_version
  phase: 'cold_start' | 'steady';
  min_samples: number;           // 切换稳态的最小已回填样本量，默认 100
  anchors: { score: number; expected: number }[];  // 冷启动：锚点分段线性插值（白盒，可手工调整）
  quantile_bins?: {              // 稳态：等频分箱校准（仅 phase=steady 有效）
    bin_edges: number[];         // K+1 个分位点，按 final_score 历史分布等频切分（K=10）
    bin_accept_rate: number[];   // K 个箱的实测接受率（箱内样本 actual_value 均值，EMA α=0.3 平滑）
    sample_count: number;        // 本映射基于的累计样本量
  };
  updated_ts: number;
}
```

| | 冷启动（cold_start） | 稳态（steady） |
|---|---|---|
| 触发条件 | 默认起点 | Review nightly 检测：已回填 feedback 的决策数 ≥ `min_samples`（100）时切换 phase |
| 方法 | 默认锚点表分段线性插值（初始经验值见下，可手工调） | 等频分箱校准：按历史 `final_score` 分布切 10 箱，箱内 `actual_value` 均值=该箱接受率 |
| 更新方 | 人工（改 coefficients.yaml） | Review 管线自动；箱接受率用 EMA（α=0.3）跨晚平滑，防单晚噪声跳变 |
| 防退化 | — | 单调约束：`bin_accept_rate` 强制非降；违反说明样本稀疏，保留上期值 |

冷启动默认锚点表（`final_score` 量纲的经验估计，系数学习后会漂移，故稳态以实测校准为准）：

| final_score | ≤-0.5 | 0 | 0.3 | 0.6 | 1.0 | ≥1.5 |
|---|---|---|---|---|---|---|
| expected | 0.05 | 0.15 | 0.40 | 0.65 | 0.85 | 0.95 |

**时序语义（不可变）**：`expected_value` 与 `mapping_version` 在**决策时**写入，历史记录永不回写——`prediction_error` 永远反映"当时的预测 vs 实际"，映射演进不污染历史误差（与数据红线 append-only 一致）。

两阶段的完整计算步骤（插值公式、切箱、EMA、单调化、数值示例）见 [04-algorithms.md](04-algorithms.md) §1。

**学习消费方式（Review 管线）**：样本按 `prediction_error` 降序消费，而非全量平均——GEPA 的少量样本额度优先给高误差样本：
- **假阳性**（expected 高 × actual 低 = 高估了内容/时机）→ 下调对应系数（content_value 相关因子 / 该状态×时段的 timing_factor）
- **假阴性**（expected 低 × actual 高 = 低估）→ 上调对应系数，且是**发现新兴趣/新偏好的最强信号**，触发兴趣图谱抽取复核
- 低误差样本仅作统计加固（更新分位数映射、验证系数收敛），不驱动系数变更

---

## 6. 成长与人格

### 6.1 SkillFile —— 技能文件（Markdown，agentskills.io 标准 frontmatter）

```yaml
---
name: timing.weekday-evening-video
kind: timing | topic_selection | narration_style
version: 3
score: 0.82                  # Curator 打分；低于阈值归档
created_by: review-pipeline
evidence_events: 134          # 支撑样本量
---
# 内容：自然语言策略 + 结构化参数（如：周四 20-23 点视频类 state_factor ×1.3）
```

### 6.2 PersonaFile —— 人设与立场

```typescript
interface Persona {
  soul_md: string;            // 性格/口癖/自我兴趣倾向（精灵有自己的偏好）
  stance_store: StanceEntry[];// 立场记忆库：精灵发表过的看法
}
interface StanceEntry {
  topic_entity: string; stance: string; said_ts: number;
  source_decision_id?: string;
}
```

### 6.3 JournalEntry / WeeklyReport —— 日记与成长汇报

```typescript
interface JournalEntry {      // 精灵日记：每日一条，Review 管线生成
  date: string; text: string; // 精灵第一人称
  related_event_ids: string[];
  offered: boolean;           // 是否已主动给用户看（不坐页面等用户点）
}
interface WeeklyReport {      // 成长汇报 = 记忆 diff 的人格化
  week: string;
  learned: { kind: 'new_interest' | 'profile_update' | 'timing' | 'skill';
             summary: string; detail_ref?: string; }[];
  text: string;               // "这周我发现了3件事：…"
}
```

投递语义（2026-10-17 实装，docs/05 §7.5）：存 digest.db（kind=morning/journal/weekly）；
晨报只认当天（隔夜不补说）、成长汇报 7 天内优先于晨报、日记不单独打扰（随次日晨报连带 offered）；
由精灵在 TRANSITION/IDLE 黄金窗口主动说（WS type=digest），推送成功落 `speak.digest` 事件。

---

## 7. 配置

```typescript
interface ElfConfig {
  hotspot_enabled: boolean;          // 热点开关，默认 true
  hotspot_max_ratio: number;         // 默认 0.3，硬上限
  liveness: 'quiet' | 'normal' | 'lively'; // onboarding 粗调初始人格
  quiet_hours: { start: string; end: string };
  llm: { strong_model: string; cheap_model: string; provider_keys_ref: string };
  privacy: { screen_capture: false };      // screenpipe 类感知默认关闭
}
```

## 8. 数据红线

1. **只做加法**：`RelationshipAccount.level/xp`、兴趣节点、技能文件均不允许回退清零；纠错走"新事实使旧事实失效"（valid_to 归档），历史可查。
2. **隐私**：不存前台窗口标题原文（只存进程名与分类）；屏幕捕获默认禁用；全部数据本地存储。
3. **append-only**：event.db 只允许插入；任何"更新"以新事件表达。
