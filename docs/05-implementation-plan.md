# Elf Buddy 完整实现规划

> 单一全量清单，不分期。排序=依赖顺序，不是阶段划分。
> 基线：链路骨架已通（感知→决策→下行→反馈落库），LLM 打标/精排已接 DeepSeek，引导页可走完。

## 0. 目标形态

桌面右下角常驻一个**光团小精灵**（程序生成，非人形立绘）：用户在场时安静陪伴，间隙时叼来内容卡片开口讲话；形象是用户自己捏的，成长是用户看得见的。验收一句话：**试用者双击 exe，3 分钟内捏出自己的小精灵，并在当天收到第一条"说得有道理"的内容推荐。**

## 1. 形象系统（光团精灵 SpriteRenderer）

程序绘制（PixiJS Graphics），零外部模型、零版权风险、捏脸即改参数。

| # | 内容 | 实现要点 | 验收 |
|---|------|---------|------|
| 1.1 | 光团本体 | 径向渐变发光体（additive 外发光 + 内核），呼吸=缩放正弦（周期≈3s），重心在底部 | 肉眼可见"在呼吸" |
| 1.2 | 五官 | 双眼（瞳孔随鼠标偏移=lookAt）、眨眼（随机 2-6s 眼睑闭合 120ms）、嘴（speak 时开合摆动） | 眨眼自然不机械 |
| 1.3 | 动作原语 | idle 呼吸 / attract 跳两下（L2）/ speak 身体前倾+口型（L3）/ poke 挤压回弹+眯眼 / sleep 变暗慢呼吸（长时间无交互） | 四通道各有专属动作 |
| 1.4 | 参数化捏脸 | 体色（6 色板）、眼型（圆眼/眯眯眼/星形眼）、装饰（无/小翅膀/光环/呆毛）、体型（小/中/大） | 改参数即时生效 |
| 1.5 | 形象预览 | onboarding 内实时渲染预览，捏完即所见 | 引导页可见活体精灵 |
| 1.6 | 渲染层抽象 | PetRenderer 接口不变，sprite.ts 为当前实现；Live2D/VRM 为可插拔替代 | App.vue 不感知具体实现 |

## 2. 互动系统

| # | 内容 | 实现要点 | 验收 |
|---|------|---------|------|
| 2.1 | 气泡布局修正 | 精灵固定窗体下 1/3，气泡在上方展开，永不遮脸 | 截图验证无重叠 |
| 2.2 | 内容卡片（已有） | 理由+标题+摘要+看看原文/待会 | 已通，保持 |
| 2.3 | 快捷反馈 👍👎 | 卡片上加两个按钮，落 `speak.feedback_explicit`（正/负），进 prediction_error 学习 | 点👎后同类内容降权可查 |
| 2.4 | 追问对话 | 卡片底部输入框：用户打字追问 → brain 用该 content_id 正文摘要+LLM 生成回答（锚定内容，接口红线⑤）→ 精灵口型+气泡回答 | 问"这篇讲了啥"得到忠于原文的回答 |
| 2.5 | L1 点击展开 | L1 静默持有时精灵轻微发光提示，点击→升级气泡 | TODO P0.1 清零 |
| 2.6 | 戳一戳回应 | poke 时随机俏皮话气泡（本地模板，不烧 token） | 点它有反应、不重复 |

## 3. 内容与时机（补全）

| # | 内容 | 实现要点 | 验收 |
|---|------|---------|------|
| 3.1 | 前台应用采集 ✅ | 同 §6.1/6.2（2026-10-17 实装：壳侧 5s 轮询前台进程→泛化类别上行；会议名单→BUSY 零打扰） | 单测 teams→BUSY；活体四类事件 200 |
| 3.2 | 三层驱动完整版 ✅ | 新模块 app/timing/drive.py（ThreeLayerDrive）：①状态驱动=状态机进 TRANSITION/IDLE 即重评；②周期巡逻 5min 兜底（BUSY/AWAY/DND 不消费指纹，状态+清单指纹去重——OpenClaw 心跳降本）；③价值自触发=管线新产出最佳候选当前状态 speak_score≥L2 门槛即时评估上浮。三层共用 try_speak 路径（digest 优先/quiet/非活跃/壳离线憋着）；Meta ISM 去重静默：decision.made 即"已说过"持久记录，重启 lifespan rebuild | 单测 10/10（重建/去重/巡逻指纹/高低分自触发）；高分内容管线完成即评估，不等 30min 周期或状态变化 |
| 3.3 | cheap 层提速 ✅ | 2026-10-17 实装：①cheap 模型 deepseek-v4-pro→deepseek-chat（实测 4.6s/20条 vs 65.1s/20条，14 倍差；v4-pro 推理级不适合批量打标）；②40条/批+Semaphore(2) 双路并发（实测 5 路触限流 67.8s，2 路 7.8s/双批）；③max_tokens 显式 8000（默认 4096 截断 40 条结构化输出）；④超时随批量缩放 max(20,n)；⑤失败只做一轮二分缩批且两半并发，仍败降级关键词启发式——有界降级不烧等待。健康窗口全管线 ≈20-25s（抓取 1.7-2.5s/82条 + 打标 ~8s + 精排+封面 ~10s）✓<30s。偏差：精排也由强模型改 chat（≤30字锚定理由质量实测无差，强模型保留给 /chat）；DeepSeek 长生成吞吐日间振荡剧烈（健康 4.6s/20条 ↔ 降级 28.8s/10条），客户端调参无解，降级窗口全管线 ~105s 闭环不破（关键词 fallback 仍产出 cv 0.5-0.8 想说清单+封面） | 健康窗口 <30s 达标；降级路径想说清单质量可用 |
| 3.4 | 内容源扩展 ✅ | 2026-10-17 实装：RSS 3→8 源全 curl 实测 200（36kr/sspai/ifanr/geekpark/infoq.cn/solidot/cnblogs/oschina；知乎 403 反爬、机器之心 /rss→HTML 假 feed 均弃用）；httpx follow_redirects=True+浏览器 UA（302 feed 原拿到 HTML）；抓取 8 源并发 asyncio.gather。封面：RSS enclosure/media:content/media:thumbnail 直出，缺则仅对精排候选（≤10条）并发补抓 og:image（timeout 6s，不抓全量）→ SpeakCommand.payload.cover → 卡片顶图出血样式。引导页"它从哪里看世界"区块：8 源 chips 默认全选可关+自定义 RSS（http 校验≤5个）→ POST /profile.feed_urls → brain 落 data/sources.json（用户数据与 config 分层，空则不写用默认）+ pipeline.reload_sources() | 引导页可选源；想说 3 条含封面 2 条实测 |

## 4. 成长可视化

| # | 内容 | 实现要点 | 验收 |
|---|------|---------|------|
| 4.1 | 亲密度等级 | opened/👍/对话 累计经验值，升级时光团变大一点+解锁装饰 | 点卡片能看到进度条动 ✅(2026-10-17，卡片 XP 细条 showBubble 时刷新；升级=壳侧 localStorage 比对 level→attract+hint 庆祝+每级+3% setGrowth 渐进变大；Lv.3/5/7 解锁翅膀/光环/呆毛，仅 accessory=none 时自动装备不覆盖捏脸) |
| 4.2 | 成长面板 | 右键/双击精灵 → 小面板：等级、已学兴趣画像（兴趣分条形图）、记忆条数 | 用户能说出"它懂我啥" ✅(2026-10-17，双击开面板（e.detail≥2 抑制戳一戳冲突，右键留 §5.2）：Lv徽章+XP条+命中率/L3配额+兴趣温度 top8 条形图（explicit/inferred 双色、非 active 淡显）+记忆条数+解锁状态；数据走新聚合端点 GET /growth/panel 一次拿全) |
| 4.3 | 记忆结晶 | 高 prediction_error 样本 Review 后写回兴趣画像（学习闭环已通，此处做可视呈现） | 画像随反馈变化 ✅(2026-10-17，面板"记忆结晶"区=facts_total（新增 count_facts()，归档不计）+近7天新学会 N 条（复用 facts_since）；兴趣条形图随👍👎温度实时变化——7.2 闭环的可视出口) |

## 5. 工程收尾

| # | 内容 | 验收 |
|---|------|------|
| 5.1 | 清理 Live2D 残留 | models/ 只剩 pixi.min.js，包体回落 ✅(2026-10-10) |
| 5.2 | 设置面板 ✅ | 2026-10-17 实装：壳侧 components/Settings.vue，精灵右键唤出（与成长面板互斥）；GET /profile 扩展返回全量（elf_name/sprite_config/interests/feed_urls）回填面板；保存 POST /profile（名字 maxlength 12 + 捏脸 chip 与 onboarding 同源 + 兴趣 textarea 一行一个「名字\|描述」需≥2 条）→ 壳侧 spriteCfg 同步 + setModel 重绘 + 清 elf_auto_accessory（用户重捏脸后不再被自动装饰覆盖）；清空重来二次确认 → POST /reset：关库 → 删 event.db/graph.db/profile.db/persona.db/digest.db/relationship.json/interests.json/sources.json/profile.json/coefficients.yaml + reports/（含 -wal/-shm 旁文件），保留 .env（LLM key）/SOUL.md（手改人格）/voice/（30MB 模型）→ os._exit(0)；生产壳检测退出码 0 自动重启 brain（quitting 标志防应用退出时误拉），renderer 2.5s 后 reload 回引导。语音开关暂留右下角 🎤（localStorage 持久），迁入面板后置；2026-10-17 补：面板新增「退出」按钮（无边框桌宠窗口无系统关闭入口）→ preload `quit()` → IPC `elf-quit` → `app.quit()` | 活体实测：GET /profile 返回小芽+2 兴趣 ✓；POST /reset 响应 ok → brain 退出 → 数据目录仅剩 voice/.env/SOUL.md，reports/ 与 event.db 均已清空 ✓ |
| 5.3 | 便携版打包 | 用 scripts/build.ps1 生成可执行文件。手工验收覆盖后端启动、令牌鉴权、首次引导、RSS 推送和语音模型下载。 |
| 5.4 | 文档回写 ✅ | 2026-10-17 完成：**03-interfaces 升 v0.2**——头部端口 7878→8787 + `/v1` 前缀 + token 双通道（X-Elf-Token / ?token=）落地说明；§1.1 Event 单条形态与实装事件名（perception.idle_change/foreground_change/screen_lock、speak.shown/opened/consumed/dwell/dismissed/feedback_explicit、user.correction）；§1.2 /chat 实装 body（text/content_id/decision_id）+ 双触发语义，audio_stream/CosyVoice2 标注未实装改系统 TTS；§1.3 形象包 → 形象配置（/profile + sprite_config 四字段，avatar-pack 标注未实装）；§1.4 /config 标注未实装改由 coefficients.yaml + /profile 承载，补 /profile/slots；新增 §1.6 其它实装端点（digest/morning、review/nightly、weekly、latest、reset）；§2 连接地址改 /v1/ws/shell，SpeakCommand 按实装重写（顶层 type/id/channel L0–L3 + payload.content_id/title/reason/summary/url/cover/narration + ttl_ms，含幂等重发 ack 语义）；新增 §2.5 ws/voice 语音通道事件表（voice.wake/asr_final/say/error）；§4 依赖表按实装重写（RSS 直连 8 源 + og:image、LLM OpenAI 兼容、sherpa-onnx 改 brain 侧、CosyVoice2→系统 speechSynthesis、文生图未实装）。**01-architecture**——架构总图与 M1 表 Live2D→程序生成精灵（sprite_config 捏脸、setModel 重绘），语音标注 ASR 在 brain 侧/TTS 系统级及偏差原因；§5 进程与部署补 portable、数据目录 userData/data、8787+/v1+token；§6 落地顺序表 P0–P3 全部标注已完成 | 文档与代码双向一致检查通过（端点 18 个、事件名、端口、字段逐一核对） |

## 6. 感知补全（M2 采集端）

| # | 内容 | 实现要点 | 验收 |
|---|------|---------|------|
| 6.1 | 前台应用采集 ✅ | ~~壳侧轮询 active-win~~→零依赖方案：壳侧 5s spawn powershell Add-Type 内联 Win32（GetForegroundWindow→ProcessName，-EncodedCommand 绕开 PS5.1 引号 bug），进程名映射泛化类别后上行 `perception.foreground_change{app,app_class}`，标题原文不取不出壳——隐私红线 | 状态机随前台 app 切换（单测 teams→BUSY/code→DEEP_WORK；node execFile 活体采集实测通过） |
| 6.2 | 会议/锁屏检测 ✅ | 会议进程名单（zoom/teams/tencent meeting/voov）→ BUSY；powerMonitor lock/unlock-screen → `perception.screen_lock{locked}` → AWAY/解锁脱出（状态机补 unlock 分支，原实现只进不出）。偏差：麦克风占用未实现（Windows 无轻量 API，进程名单已覆盖主场景） | 单测 9/9（锁屏→AWAY、AWAY 中 idle 不脱出、解锁重估、unlock 不打断 DND） |
| 6.3 | 反馈梯度采集端 ✅ | consumed（20s 未关自动报）✅、dwell（悬停卡片累计 ≥8s 才关 → speak.dwell{dwell_ms,completed} 替代 dismissed——挂着无视≠停留久，以悬停而非展示时长判定）、followup（§2.4 追问即上报）✅、shared_to_elf（拖动链接给精灵，后置） | 前四档 actual_value 均有真实事件来源；shared_to_elf 留后置 |

## 7. 记忆与学习底座（M4/M5/M8 全量）

依赖序：7.1→7.2→7.3→7.4（Review 消费前三者的数据）。

| # | 内容 | 实现要点 | 验收 |
|---|------|---------|------|
| 7.1 | 关系账户 | RelationshipAccount（level/xp/命中率窗口近50次/安静期/配额浮动 [1,6]）；连续2次无视→当日禁L3；信任记账进评分 trust_factor | 连点2次"待会"后当日不再被 L3 打扰 |
| 7.2 | 兴趣图谱（Graphiti） | ~~FalkorDB 嵌入式~~→**SQLite 承载 Graphiti 语义**（2026-10-10 验证：falkordblite 全版本无 win wheel、安装实测 "not supported on the 'win32' platform"；Neo4j=JVM 服务器无法入便携包）；InterestNode（温度=近14天指数衰减，半衰7天，explicit 地板0.3）+时态边（valid_to归档，paused 自动开闭）+MemoryFact；反馈事件带符号 episode 写入（neg×1.5保护性）；温度进 content_value | 兴趣温度随👍👎可见变化（成长面板呈现） ✅(2026-10-10，/interests 实测) |
| 7.3 | 消费画像+纠偏 | ProfileSlot（length/tone/medium 偏好）；POST /correction "我现在还是喜欢长的" → 旧事实 valid_to 归档（只做加法红线） | 纠偏后推荐长度倾向可查地改变 ✅(2026-10-17，/correction 三次纠偏实测：4 槽位落库、long 归档/short 生效、精排 prompt 注入偏好) |
| 7.4 | Review 管线 | APScheduler nightly（quiet_hours 后 23:15）：prediction_error 降序消费→改写 coefficients.yaml；稳态校准切换（样本≥100，等频分箱+EMA α=0.3+单调化，04 §1.2）；**token 预算硬上限，超限降级纯统计**；weekly（周日 23:20）：边界重建/温度重算/技能归档（库结构与归档纪律在位，生成侧随 §8）/成长汇报数据 | 命中率环比提升可查；单次 Review 成本有硬护栏 ✅(2026-10-17，单测全过+活体 nightly：LLM 复盘 8521/20000 token、state_factor.idle 0.7→0.65 自动落盘保注释、命中率环比入报告) |
| 7.5 | 晨报/日记/成长汇报 | L0 内容流入晨报；精灵日记第一人称；周报=记忆 diff 人格化（"这周我发现了3件事"）——由精灵主动说，不做页面。WS 新类型 type=digest（不进反馈学习、无👍👎/追问，防污染命中率窗口）；pending/offered 语义：weekly 7 天内优先、morning 只认当天（隔夜不补说）、日记随次日晨报连带 offered；TRANSITION/IDLE 黄金窗口一次只说一件事（digest 优先于内容推荐）；quiet_hours 拒推 | 早晨收到晨报气泡；周末收到成长汇报 ✅(2026-10-17，单测 14/14+活体：启动补生成晨报消费昨夜复盘、nightly 钩子生成第一人称日记、TRANSITION 钩子 WS 推送+speak.digest 落库+offered 标记、weekly 钩子生成 8 条 learned 成长汇报待说；speak.digest 事件与 WS digest 消息已补录 docs/02 §1.1/§6.3、docs/03 §2.4) |

## 8. 人格化讲述与语音（M1/M6 表达层）

| # | 内容 | 实现要点 | 验收 |
|---|------|---------|------|
| 8.1 | Narration 生成 ✅ | 2026-10-17 实装：新模块 app/speak/narration.py（NarrationService）。L3 决策后推送前生成（L1/L2 不生成省 token，03 §2.1：L3 必带 narration），hook（≤20字开场白）+script_short（150-220字口语稿）+stance_check_passed；生成前按内容 entities 检索立场库注入 prompt（保持一致或点明转变原因），产出 stances 入库；缓存进 CandidateEntry（item["narration"]）；shell 气泡显 hook 斜体蓝字+script 替代干巴 summary（6 行空间）。失败/无 key → None 回退 summary 闭环不破。偏差：强模型→cheap（v4-pro 65s 不可用于 L3 实时路径，chat ~5s；L3 日配额≤6 成本可忽略；强模型保留给 /chat——与 §3.3 精排同一偏差逻辑）；script_deep 未实现（P1 周末/晚间场景再补） | 单测 18/18（生成/立场注入/容错降级）；活体验证待 LLM 余额恢复 |
| 8.2 | 人格/立场底座 ✅ | 2026-10-17 实装：app/growth/persona.py 扩为 PersonaService。SOUL.md 首次落盘默认副本（性格/口癖/自身兴趣倾向三段——偏爱动手做的胜过概念炒作、反感标题党；与用户重合→共鸣不重合→碰撞），用户可编辑、每次读取回盘免重启（同 coefficients 哲学）；StanceEntry 立场库 SQLite persona.db（topic_entity/stance/said_ts/source_decision_id），只做加法不删改，stances_for 双向模糊匹配（"诺兰"命中"导演诺兰的新片"）按入库序最新优先 | 单测覆盖：默认落盘/空实体跳过/模糊命中/多立场共存/最新优先 |
| 8.3 | mood.set 下行 ✅ | 2026-10-17 实装：brain 派生情绪（quiet_hours→sleepy；安静期/连拒≥2→sorry；👍/dwell/追问/shared→excited 180s 瞬态），变化才推（去重）；反馈统一入口 on_speak_feedback（/events 与 /chat 共用）结算后重估，patrol_loop 5min 顺路重估（quiet_hours 边界是时间驱动），壳（重）连即同步当前情绪。壳侧 MOOD_PARAMS 五档参数化：呼吸周期/幅度、眨眼间隔、眼睑下垂（sorry 0.45 半耷拉/sleepy 0.8 近全闭）、光晕基准、thinking 轻颤 wobble；未知 mood 值忽略防脏数据 | 活体冒烟：连拒→sorry、👍→excited 日志+WS 下行 ✓；被连拒肉眼可见变蔫（呼吸慢 4.5s+幅度 0.6+眼睑半垂+光晕变暗） |
| 8.4 | 语音对话 ✅ | 2026-10-17 实装：新模块 app/voice/（wakeword.py 唤醒词音素生成 + service.py 会话状态机）；WS /ws/voice（壳上行 PCM int16 16k 单声道，下行 voice.wake/asr_final/say/error）；KWS sherpa-onnx zipformer-wenetspeech int8（3.3M，~5MB）自定义唤醒词——pypinyin 把 elf_name 逐字拆「声母+带调韵母」逐 token 对 tokens.txt 校验（不在表内生成期即报错，防静默失效），keywords.txt 随 profile 名每次开会话重建；唤醒后 ASR 指令窗 10s 上限+端点检出（1.2s 尾随静音），ASR 文本 → 10min 内有说过卡片按追问锚定（同 /chat 计 0.9 信号）否则闲聊模式（soul 人格 cheap 模型，不进反馈学习），回复 voice.say 下行 → 壳 speechSynthesis 朗读 + boundary 事件 mouthPulse 真实口型（600ms 无脉冲回退正弦，不哑巴）；壳侧 src/voice.ts 麦克风采集降采样 16k 环回上行，🎤 开关右下角（localStorage 持久，§5.2 设置面板就绪后迁入），字幕气泡显「我在听/你：/小芽：」。模型不入安装包（portable 红线）：ensure_models 缺啥下啥（ModelScope 镜像 pkufool KWS + ZhaoChaoqun ASR，~30MB 总量）落 data/voice/。偏差：①ASR 推理在脑侧非壳侧（sherpa-onnx 无合适 shell 绑定；音频只走 127.0.0.1 环回不出机，隐私等价）②CosyVoice2→系统 speechSynthesis（PyTorch 级体积破红线）③LipSync=boundary 事件驱动非 RMS 音量级④ASR 模型 paraformer-bilingual(int8 226MB)→zipformer-zh-14M(int8 25MB)——中文专用体积减 9 倍，中英混说识别率下降（语音场景主要是中文指令）⑤GitHub release 本机不可达，模型源改 ModelScope 镜像 | 活体：WS 会话开启/静音噪声 3s 无误唤/断连干净 ✓；/chat 闲聊无 key 降级「我在呢，你说。」✓；唤醒+ASR 中文识别准确率待真声实测（本机 SAPI 合成被沙箱策略拦截）；shell 构建 exit=0 |

## 9. 接口对齐（03 全量）

| # | 内容 | 验收 |
|---|------|------|
| 9.1 | /v1 前缀+一次性 token ✅ | 2026-10-17 实装：brain 侧 V1Guard（ASGI 中间件，零侵入 86 个既有路由）——非 /v1 路径直接 404；token 校验通过后 `scope["path"]=path[3:]` 剥前缀交内层。HTTP 走 X-Elf-Token 头，WS 走 ?token= 查询参数（浏览器 WS 无法自定义头），OPTIONS 预检放行（V1Guard 在 CORS 外层，拦了预检会卡死跨域）。`ELF_TOKEN` 未注入时不校验（开发 start.ps1 直连 brain 免 token 便于 curl）。壳侧链路：主进程 randomBytes(24) 生成 → spawn brain 时 env 传入 → ipcMain.on('elf-token') 同步 IPC → preload sendSync 暴露 `window.elfShell.token` → renderer 经 src/brain.ts 的 brainFetch/brainWs 统一注入（App.vue 8 处 fetch 与 voice.ts WS 全量切换） | 探针实测：`/v1/profile` 无 token → 401 ✓、带 token → 200 ✓、`/profile`（无前缀）→ 404 ✓、`/v1/events` 带 token → 200 ✓、`/v1/ws/shell` 无 token → 握手被拒（WebSocketDisconnect）✓ |
| 9.2 | /v1/chat（追问通道后端）✅ | 2026-10-17 实装：POST /chat（ChatRequest: text/content_id/decision_id）→ chat_reply() 统一后端。双触发同源：①L1 揭示卡片后壳内追问带 content_id → 回答锚定原卡片正文（红线⑤，提示词只喂该条内容，答不出回「这里面好像没提到这个，看看原文？」）②用户气泡主动发起（content_id 空）→ 闲聊模式走 soul 人格。语音通道 voice.asr_final 复用同一 chat_reply（10min 内有说过卡片即按追问锚定，否则闲聊）；追问统一计 0.9 反馈信号进 on_speak_feedback | 无 key 降级不崩（fallback 文案）✓；语音追问与文本追问走同一路径 ✓ |
| 9.3 | config/correction/journal/weekly-report/relationship/interests 接口族 ✅ | 2026-10-17 实装：brain 共 18 个端点全部经 /v1 暴露——/events、/chat、/ws/shell、/ws/voice、GET/POST /profile、/profile/slots、/relationship、/interests、/growth/panel、/correction、/review/nightly、/review/weekly、/review/latest、/journal、/weekly-report、/digest/morning、/reset。壳侧只渲染不计算（红线1）：设置面板（GET/POST /profile、POST /reset）、成长面板（/relationship、/growth/panel）、纠错（/correction）、聊天（/chat）全部走 brainFetch，无本地业务状态 | 设置面板活体：GET /profile 回填 → POST /profile 保存 ✓；成长面板/纠错接口经统一客户端调用 ✓ |
| 9.4 | WS 幂等重发 ✅ | 2026-10-17 实装：WsManager 加 pending 队列（decision_id → (下发时刻, SpeakCommand)），push speak 即入队；壳实际渲染后上报 `speak.shown{decision_id}` = ack 出队（post_event 单列分支，不走 speak.* 反馈结算——曝光确认≠反馈信号）；壳（重）连时 resend_pending() 重发，PENDING_TTL=600s 过期丢弃（陈旧卡片不骚扰）。壳侧 seenSpeak Set 按 decision_id 去重（上限 200 条删最旧），重连不重复弹卡 | 探针实测：push speak → `pending=['spk_probe']`；非 speak 指令不入队；ack 后 → pending 空；resend 不再下发 ✓ |

## 10. 总验收（试用者视角）

1. 双击 exe → 引导页捏出**自己的**光团精灵（颜色/眼睛/装饰）→ 落到右下角呼吸眨眼
2. 鼠标划过它眼神跟随，点它会回应
3. 间隙时它跳出来讲内容，理由说得出"跟你哪个兴趣相关"
4. 👍👎 点完，后面的推荐肉眼可见更准
5. 成长面板里能看到"它越来越懂我"

## 11. 后续实现变更

| 模块 | 实现变更 |
| --- | --- |
| 内容与提醒 | RSS 排序纳入发布时间和新鲜度衰减。主动询问资讯时按需刷新内容池。对话可设置本地提醒，并可在成长面板查看最新入库条目。 |
| 桌面交互 | 戳一戳优先请求后端，有本地文案兜底。程序动画增加短时表情与待机小动作。Electron 使用单实例锁避免多个后端争用同一端口。 |
| 会话与记忆 | 文本与语音共享短期对话上下文。事实抽取写入 SQLite，用户纠偏可归档旧事实。分支故事选择经独立 API 推进。 |
| 复盘 | 周报生成的候选经验通过反馈样本回放检查后再入库。晨报、回访和成长汇报使用独立投递状态。 |
| 音频与皮肤 | 语音会话支持唤醒后的连续交互。在线朗读失败时回退系统朗读。图片皮肤可选用 ISNet 本机抠图。 |

当前公开版本的启动、配置、数据流和第三方资源说明见 [README](../README.md)。
