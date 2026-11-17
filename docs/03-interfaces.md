# Elf Buddy 接口定义

版本：v0.2（2026-10-17 实装回写；基线 v0.1 2026-10-10）｜配套：`01-architecture.md`、`02-data-model.md`

两个进程：`elf-shell`（Electron/TS）与 `elf-brain`（Python/FastAPI）。
通信（2026-10-17 实装）：上行（shell→brain）= 本地 HTTP `http://127.0.0.1:8787/v1/*`；下行（brain→shell）= WebSocket `ws://127.0.0.1:8787/v1/ws/shell`；语音通道 `ws://127.0.0.1:8787/v1/ws/voice`。
brain 只绑定 loopback；启动时交换一次性 token（shell 拉起 brain 时经环境变量 `ELF_TOKEN` 传入），防本机其他进程伪造事件。HTTP 走 `X-Elf-Token` 头，WS 走 `?token=` 查询参数（浏览器 WS 无法自定义请求头）；生产壳未带 token → HTTP 401 / WS 以 4401 关闭；`ELF_TOKEN` 未注入时不校验（开发直连免 token）。非 `/v1` 路径一律 404（V1Guard ASGI 中间件统一守卫，见 docs/05 §9.1）。

约定：请求/响应体均为 JSON；类型名引用 `02-data-model.md`；错误统一 `{ "error": { "code": string, "message": string } }`。

---

## 1. shell → brain（HTTP 上行）

### 1.1 事件上报（感知 + 交互，共用一个入口）

```
POST /v1/events
Body: { "type": string, "ts": number, "source"?: string, "payload": object }   // 单条（实装）
200: { "ok": true }
```

- shell 上报（实装事件名）：
  - `perception.idle_change` `{idle_sec}` / `perception.foreground_change` `{app, app_class}` / `perception.screen_lock` `{locked}` → 感知状态机（窗口标题原文不出壳，只给进程名与分类；会议态由 brain 侧按 app 名判定 Zoom/Teams/腾讯会议）
  - `speak.shown` `{decision_id}` / `.opened` / `.consumed` / `.dwell` `{decision_id, dwell_ms}` / `.dismissed` / `.feedback_explicit` `{decision_id, value: 'up'|'down', ...}` → 反馈学习闭环记账
  - `user.correction` `{text}` → 纠偏流程（对话走 `/v1/chat`，不经事件入口）
- 语义分叉：`speak.shown` 只作 ack（把 decision_id 从下行重发队列移除），**不进反馈结算**；其余 `speak.*` 才结算反馈信号。
- `speak.shown` 由 shell 在内容实际渲染后上报（不是 brain 下发即算），保证记账基于真实曝光。

### 1.2 对话（用户主动说话/凑过来）

```
POST /v1/chat
Body: { "text": string, "content_id"?: string, "decision_id"?: string }   // 实装
200: { "reply_text": string }
```

- 双触发：①L1 揭示卡片后壳内追问带 `content_id` → 回答锚定该卡片标题/摘要（红线⑤，答不出明说没提到）；②用户气泡主动说话（`content_id` 空）→ 闲聊模式走 SOUL.md 人格。
- 追问统一计 0.9 反馈信号进 `on_speak_feedback`；LLM 不可用时返回降级文案（不崩）。
- 语音音频不走本接口（v0.1 的 `audio_stream`/CosyVoice2 未实装）：实装为壳侧系统 `speechSynthesis` 本地朗读，见 §2.5 语音通道。

### 1.3 形象配置（实装；文生图形象包未实装）

形象不走向量生成，改为**程序生成的精灵 + 用户捏脸参数**（docs/05 §1.1）：

```
GET  /v1/profile            → { "exists": boolean, "elf_name"?, "sprite_config"?, "interests"?, "feed_urls"? }
POST /v1/profile            ← { "elf_name": string, "sprite_config": { "color", "eyes", "accessory", "size" }, "interests": [{ "name", "desc" }], "feed_urls"?: string[] }
     200: { "ok": true }     // 落 profile.json/interests.json/sources.json，后台立即跑一轮内容管线
```

- `sprite_config` 由壳侧精灵渲染器直接消费（`setModel` 重绘），换装后清 `elf_auto_accessory` 标记。
- v0.1 的 `POST /v1/persona/avatar-pack`（文生图立绘+表情差分）**未实装**：体积/成本/延迟均不适配桌宠常驻形态（偏差已回写 docs/05）。

### 1.4 配置与纠偏

```
POST /v1/correction                  ← { "text": string }   // "我现在还是喜欢长的"
     200: { "applied_fact_ids": string[] }                  // 槽位 upsert + 旧事实 valid_to 归档，永不删除
GET  /v1/profile/slots               → [{ slot, value, ... }]  // 当前消费偏好快照（成长面板/调试）
```

- v0.1 的 `GET/PUT /v1/config` **未实装**：热点开关/活泼度/quiet_hours 由 `timing/coefficients.yaml` 承载（Review 管线学习），设置面板只改档案（名字/外观/兴趣/内容源），不直接暴露系数。

### 1.5 成长与家（1% 场景，"精灵的家"页面数据）

```
GET /v1/journal?date=2026-10-10      → JournalEntry[]       // 日记
GET /v1/weekly-report?week=2026-W41  → WeeklyReport         // 成长汇报
GET /v1/relationship                 → RelationshipAccount  // 亲密度/命中率/配额（只读）
GET /v1/interests?include_dormant=true → InterestNode[]     // 兴趣图谱快照（供好奇时刻"你都知道我什么"，由精灵讲述，不做图鉴页）
GET /v1/growth/panel                 → GrowthPanel          // 成长面板聚合（双击精灵一次拿全，docs/05 §4.2）
```

```typescript
interface GrowthPanel {               // 2026-10-17 实装
  account: RelationshipAccount;       // level/xp/xp_next_level/hit_rate/daily_l3_quota
  interests: InterestNode[];          // 全量（含 dormant），shell 按温度排序截断 top8
  facts_total: number;                // 生效记忆条数（memory_facts 归档不计）
  facts_recent_7d: number;            // 近 7 天新学会（含归档 diff 语义）
}
```

### 1.6 其它实装端点（docs/05 §7 补充）

```
POST /v1/digest/morning              → { ok, kind, title, text }   // 手动触发晨报（调试/验收）
POST /v1/review/nightly              → ReviewReport                 // 手动触发夜间复盘
POST /v1/review/weekly               → WeeklyReport                 // 手动触发生成周报
GET  /v1/review/latest               → 最近一次复盘摘要
POST /v1/reset                       → { ok: true }                 // 清空重来：关库→删全部数据→brain 退出，壳自动重启进引导
```

---

## 2. brain → shell（WebSocket 下行）

连接（实装）：`ws://127.0.0.1:8787/v1/ws/shell?token=…`（壳启动后建立，断线 3s 后重连）。消息信封：

```typescript
{ "type": WsType, "id"?: string, "payload": unknown }
```

### 2.1 SpeakCommand —— 开口/显眼指令（时机引擎 → 在场层）

```typescript
// 实装形态（2026-10-17）——注意 type/channel 在顶层，content 字段在 payload 内
{
  "type": "speak",
  "id": string,                     // = decision_id（下行重发去重键）
  "channel": "L0" | "L1" | "L2" | "L3",
  "payload": {
    "content_id": string,
    "title": string,
    "reason": string,               // 白盒理由（红线⑤：来自精排落库）
    "summary": string,
    "url": string,
    "cover"?: string,               // 封面图（RSS enclosure / og:image，可空）
    "narration"?: Narration         // L3 必带：hook + script_short + stance_check_passed
  },
  "ttl_ms"?: number | null          // L3=30000；L2/L1 为 null
}
```

通道语义：L3 气泡+可选语音（黄金窗口，日配额）；L2 仅动作（gesture 吸引，不出声）；L1 点击唤出的持有池卡片；L0 静默持有（不渲染）。

shell 必须按真实发生回报：`speak.shown` / `speak.opened` / `speak.consumed` / `speak.dwell` / `speak.dismissed`。
**幂等重发**（红线3，已实装）：brain 对每条 speak 入 pending 队列，渲染后上报 `speak.shown{decision_id}` 才 ack 出队；壳重连时 `resend_pending()` 重发未 ack 指令（600s 过期丢弃），壳侧按 `decision_id` 去重（Set，上限 200）。

### 2.2 表情/状态变更

```typescript
{ "type": "mood.set", "payload": { "mood": "idle" | "thinking" | "excited" | "sorry" | "sleepy" } }
// 典型用法：连续未命中进入安静期 → 'sorry' 姿态 + 降低待机动作频率

// §11.35 瞬时表情（与 mood 姿态正交叠加；改脸上部件，duration_ms 后壳侧自动回落 normal）
{ "type": "expression.set",
  "payload": { "expr": "normal" | "happy" | "pout" | "angry" | "lovely" | "surprised" | "shy",
               "duration_ms": number } }  // duration_ms 默认 1500
// 典型用法：👍 → lovely / 👎 → pout / 追问·久读 → happy / 戳 → pout / 连戳≥3 → angry
```

### 2.3 系统通知

```typescript
{ "type": "avatar.updated", "payload": { "pack_path": string } }        // §1.3 完成
{ "type": "brain.status", "payload": { "state": "ready" | "degraded", "llm_ok": boolean, "rsshub_ok": boolean } }
{ "type": "review.done", "payload": { "summary": string } }             // 夜间 Review 完成（晨报/汇报已就绪）

// §11.35 内心 OS：静默独白（os_loop 每 ~45min 一条；cheap 生成失败用本地兜底）
{ "type": "os.say", "payload": { "text": string } }                      // 壳侧虚线斜体气泡 8s 自消
```

### 2.4 DigestCommand —— 晨报/成长汇报主动说（docs/05 §7.5）

```typescript
{ "type": "digest", "id": string,
  "payload": { "kind": "morning" | "weekly", "title": string, "text": string } }
```

与 SpeakCommand 的区别：**不进反馈学习**（无 👍👎/追问/consumed，不占命中率窗口）；
shell 气泡 60s 自消、仅"知道啦"关闭；brain 在 TRANSITION/IDLE 黄金窗口**优先于内容推荐**推送
（一次状态变化只说一件事），quiet_hours 拒推；推送成功落 `speak.digest` 事件并 mark offered。

### 2.5 语音通道 ws/voice（docs/05 §8.4，实装）

```
连接：ws://127.0.0.1:8787/v1/ws/voice?token=…     // 壳侧 🎤 开启时建立，关闭即断开
上行：二进制帧 = PCM int16 小端 / 16kHz / 单声道（壳侧 AudioContext 降采样）
下行：{ "type": "voice.*", "payload": …, "text"?: string }
```

| 事件 | 方向 | 说明 |
|---|---|---|
| `voice.wake` | 下 | KWS 命中唤醒词（= 精灵名字，音素由 pypinyin 逐字生成） |
| `voice.asr_final` | 下 | 指令窗内端点检出后的最终文本（空串=误唤/没听清） |
| `voice.say` | 下 | 回复文本 `{text, heard}`，壳侧 `speechSynthesis` 朗读 + boundary 驱动口型 |
| `voice.error` | 下 | 会话开启失败（模型缺失/下载失败），壳侧提示并退出聆听态 |

- 隐私红线：麦克风音频只走 127.0.0.1 环回，ASR 全本地（sherpa-onnx zipformer-zh-14M int8），不出机。
- `voice.asr_final` 复用 `/v1/chat` 的后端 `chat_reply`：10min 内有说过的卡片 → 追问锚定；否则闲聊（soul 人格，不进反馈学习）。

---

## 3. elf-brain 内部接口（模块边界，Python 包级）

包结构即模块边界；跨包只许经下列接口，禁止互相 import 内部实现。

```
brain/
  perception/    # 状态机（消费 state.changed 事件）
  timing/        # M3 时机引擎
  memory/        # M4 记忆底座（Graphiti 适配、画像、人格/立场、技能）
  feedback/      # M5 反馈学习闭环
  content/       # M6 内容管线（适配器/打标/讲述）
  match/         # M7 匹配引擎
  review/        # M8 后台管线
```

### 3.1 perception → timing

```python
class StateSource(Protocol):
    def current(self) -> UserState: ...
    def on_change(self, cb: Callable[[StateChangedPayload], None]) -> None: ...
```

### 3.2 timing 核心

```python
class TimingEngine(Protocol):
    def enqueue(self, candidate: CandidateEntry) -> None: ...
        # content 管线调用；入想说清单
    def on_state_change(self, s: StateChangedPayload) -> None: ...
        # 触发重评；命中阈值则发 SpeakCommand
    def on_feedback(self, e: Event) -> None: ...
        # speak.* 事件回填 feedback（outcome/actual_value/prediction_error，见数据模型§5.3）；更新关系账户窗口
    def decide(self, now: int) -> SpeakDecision: ...
        # 纯函数式评分（score = content_value × state_factor × timing_factor
        #                - disturbance_cost × trust_factor），可单测、可解释
```

系数配置（`state_factor` / `timing_factor` / 各通道阈值 / 配额曲线 / `normalization` 归一化映射）集中存放 `timing/coefficients.yaml`，**Review 管线只改这个文件，不改代码**。

### 3.3 memory 对外接口

```python
class MemoryStore(Protocol):
    # 读（决策用）
    def interest_temperature(self, entities: list[str]) -> dict[str, float]: ...
    def profile(self) -> UserProfile: ...
    def stance_check(self, entity: str, proposed: str) -> bool: ...   # 讲述生成前立场校验
    # 写（事件驱动）
    def ingest_episode(self, e: Event, valence: Literal['pos','neg','neutral']) -> None: ...
        # 反馈闭环调用：带符号 episode 进 Graphiti
    def apply_correction(self, text: str) -> list[str]: ...           # 返回失效的 fact_ids
```

### 3.4 content / match

```python
class ContentPipeline(Protocol):
    def run_ingest(self) -> int: ...        # 定时：适配器抓取→打标→入库，返回新条目数
    def make_candidates(self) -> list[CandidateEntry]: ...
        # 热点 ≤30% 配额在此强制执行；调 match 引擎算 content_value + match_reason

class MatchEngine(Protocol):
    def rank(self, items: list[ContentItem]) -> list[CandidateEntry]: ...
        # embedding 粗排（温度加权）→ LLM 精排 top-K（产出 match_reason）
```

### 3.5 review 管线

```python
class ReviewPipeline(Protocol):
    def nightly(self) -> ReviewReport: ...
        # 反馈分析（样本按 prediction_error 降序消费，见数据模型§5.3）→改写 coefficients.yaml；
        # 记忆巩固（去重/过期 valid_to）；
        # 温度重算；生成晨报+日记
    def weekly(self) -> WeeklyReport: ...
        # 成长汇报（记忆 diff 人格化）；技能复盘（Curator 打分/归档）；
        # 命中率环比、token 成本报表
```

调度：APScheduler（进程内 cron）；`nightly` 于 quiet_hours 开始后运行。**成本护栏：单次 Review 的 LLM 调用预算硬上限，超限自动降级为仅统计不调模型**——这是小龙虾烧 token 的教训落到代码里。

---

## 4. 外部依赖接口

| 依赖 | 接入方式 | 用途 | 降级策略 |
|---|---|---|---|
| RSS 直连（默认 8 源） | HTTP GET feed URL（36氪/少数派/爱范儿/极客公园/InfoQ/Solidot/博客园/开源中国，2026-10-17 实测 200） | 内容抓取；用户自选源 `sources.json` 优先 | 单源失败跳过；全部失败→当日无新内容，精灵明说"今天没什么值得说的" |
| og:image 补抓 | HTTP GET 候选文章页（仅精排 top≤10） | 封面图（RSS enclosure/media:* 缺失时） | 抓不到则无封面，卡片降级纯文字 |
| LLM API | 统一封装 `llm.complete(model_tier, prompt, json_schema?)`（OpenAI 兼容） | 打标/抽取(cheap=deepseek-chat)、讲述/精排/复盘(strong) | strong 不可用→cheap 兜底；全不可用→仅静默持有，不主动开口（余额不足 402 时全链路降级不崩） |
| sherpa-onnx（**brain 侧**） | 本地库：KWS zipformer-wenetspeech int8（~5MB）+ ASR zipformer-zh-14M int8（~25MB），模型首用从 ModelScope 镜像下载 | 唤醒词 + 端侧流式 ASR（音频只在环回，不出机） | 模型缺失/加载失败→`voice.error`，壳侧退回纯文字气泡 |
| 系统 TTS（`speechSynthesis`） | 壳侧 Web Speech API | 朗读 `voice.say`（boundary 事件驱动口型） | 无可用音色→只显示字幕，不阻塞对话 |
| 文生图 API | —（v0.1 设想，**未实装**） | — | 形象改为程序生成精灵 + 捏脸参数（§1.3） |

## 5. 接口红线

1. shell 不直接访问任何数据库与 Graphiti——一切经 brain API（保证单一数据入口，记忆可审计）。
2. `decision.made` 事件必须先于对应 `SpeakCommand` 落库（决策可复盘，甩锅可查）。
3. 所有下行指令幂等：shell 重连后 brain 重发未 ack 的 `SpeakCommand`，shell 按 `decision_id` 去重。
4. 任何接口不得返回用户原始隐私数据（窗口标题原文、屏幕内容）——这类数据不出 perception 包。
5. 讲述生成必须锚定已入库内容：`Narration` 与生成的任何推荐文案必须引用有效 `content_id`（检索在前、生成在后），禁止 LLM 凭记忆自由生成内容推荐——Muse Feed 的教训：无内容库约束的生成式推荐会泛泛而谈甚至编造，且不相关推荐直接损害用户对记忆的信任（nanoMuse 逆向论文/remio 评测，2026-09/10）。
