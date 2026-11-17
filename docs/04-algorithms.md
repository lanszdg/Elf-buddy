# Elf Buddy 算法细节

版本：v0.1（2026-10-10）｜配套：`02-data-model.md`、`03-interfaces.md`

收录各模块核心算法的分步计算流程，粒度到"可直接编码"。数据结构定义以 `02-data-model.md` 为准，本文不重复。

---

## §1 expected_value 概率校准（冷启动锚点插值 / 稳态等频分箱）

服务对象：时机引擎 `decide()` 中将 `final_score` 转为接受概率估计 `expected_value`（写入 `SpeakDecision.feedback`）。参数结构 `ExpectedValueMapping` 见数据模型 §5.3。

### 1.0 符号与输入

| 符号 | 含义 | 来源 |
|---|---|---|
| `s` | 单次决策的 `final_score`（原始分，量纲随系数漂移） | 时机引擎评分公式 |
| `A = [(s₁,e₁),…,(sₙ,eₙ)]` | 冷启动锚点表，按 `s` 升序（默认 n=6，见 1.1） | coefficients.yaml |
| `D` | 已回填 feedback 的历史决策样本集 `{ (sⱼ, aⱼ) }`，`aⱼ` = actual_value | event.db |
| `K` | 稳态分箱数，固定 10 | 约定 |
| `α` | EMA 平滑系数，固定 0.3 | 约定 |
| `mapping(s)` | 输出：`expected_value ∈ [0,1]` | 本算法 |

### 1.1 冷启动：锚点分段线性插值

**使用时机**：`phase = cold_start`（已回填样本数 < `min_samples`=100）。

**默认锚点表**（经验先验，人工可改 coefficients.yaml）：

| i | 1 | 2 | 3 | 4 | 5 | 6 |
|---|---|---|---|---|---|---|
| sᵢ | -0.5 | 0 | 0.3 | 0.6 | 1.0 | 1.5 |
| eᵢ | 0.05 | 0.15 | 0.40 | 0.65 | 0.85 | 0.95 |

**计算步骤**：

```
输入：s
1. 边界截断：
   - 若 s ≤ s₁ → 返回 e₁
   - 若 s ≥ sₙ → 返回 eₙ
2. 区间定位：找到 i 使 sᵢ ≤ s < sᵢ₊₁（锚点已升序，线性扫描或二分均可，n=6 无所谓）
3. 线性插值：
   t = (s - sᵢ) / (sᵢ₊₁ - sᵢ)
   expected_value = eᵢ + t × (eᵢ₊₁ - eᵢ)
```

**数值示例**：`s = 0.45` → 落在区间 [0.3, 0.6)：
```
t = (0.45 - 0.3) / (0.6 - 0.3) = 0.5
expected_value = 0.40 + 0.5 × (0.65 - 0.40) = 0.525
```

### 1.2 稳态：等频分箱校准

**切换条件**：Review nightly 检测 `|D| ≥ min_samples`（100）→ `phase` 置为 `steady`，首次建箱。

**建箱/重建步骤**（weekly 执行；nightly 只做接受率 EMA，见 1.3）：

```
输入：D（按 sⱼ 升序排序），K=10
1. 排序：D_sorted = sort(D, key=s)
2. 切箱：等频切分，每箱约 ⌊|D|/K⌋ 个样本
   - bin_edges[0]   = -∞（或 min(s) - ε）
   - bin_edges[K]   = +∞（或 max(s) + ε）
   - bin_edges[k]   = ( s_(k·m) + s_(k·m+1) ) / 2，其中 m = ⌊|D|/K⌋，k = 1..K-1
     （边界取相邻两分箱样本的 score 均值，保证任何历史样本唯一落箱）
3. 箱接受率：rate_raw[k] = mean{ aⱼ | sⱼ ∈ 第 k 箱 }
4. EMA 平滑（有上期映射时）：
   rate[k] = α × rate_raw[k] + (1-α) × rate_prev[k]，α = 0.3
   首期：rate[k] = rate_raw[k]
5. 稀疏保护：若第 k 箱样本数 < 5 → rate[k] = rate_prev[k]（保留上期；首期则向相邻箱借均值）
6. 单调化（向右传播）：
   for k = 2..K: 若 rate[k] < rate[k-1] → rate[k] = rate[k-1]
   （标准实现可换 PAVA 等张回归，MVP 用向右传播足够）
7. version += 1，写 updated_ts，持久化到 coefficients.yaml
```

**决策时查询**：

```
输入：s
1. 定位箱：找 k 使 bin_edges[k] ≤ s < bin_edges[k+1]（s 越界则归首/末箱）
2. 返回 bin_accept_rate[k]（分段常量；不插值——校准曲线的标准做法，
   连续化升级路径是 isotonic 回归，样本量 >1000 后再评估）
```

### 1.3 维护节奏汇总

| 动作 | 频率 | 内容 |
|---|---|---|
| phase 切换检查 | nightly | `|D|` 首次 ≥100 → cold_start → steady，首次建箱 |
| 接受率 EMA 更新 | nightly（仅 steady） | 用当日新回填样本重算 `rate_raw` → 步骤 4-6（边界不动） |
| 边界重建 | weekly（仅 steady） | 全量 D 重跑步骤 1-6（`final_score` 分布随系数学习漂移，边界需低频跟随） |
| 人工干预 | 任意 | 改 anchors / min_samples / α → 人工 version+1，下次决策生效 |

### 1.4 端到端数值示例（稳态）

设 `|D| = 120`，K=10，每箱 12 样本。第 3 箱：`bin_edges = [0.18, 0.31]`，本期 12 个样本的 `actual_value` 均值 = 0.25，上期 `rate_prev[3] = 0.30`：

```
rate_raw[3] = 0.25
EMA:        rate[3] = 0.3 × 0.25 + 0.7 × 0.30 = 0.285
单调化：     若 rate[2]（单调化后）= 0.30 > 0.285 → rate[3] 修正为 0.30
```

决策时 `s = 0.25` → 落入第 3 箱（0.18 ≤ 0.25 < 0.31）→ `expected_value = 0.30`。

反馈回来：用户秒关（`actual_value = 0`）→ `prediction_error = |0.30 - 0| = 0.30`。该样本进入高误差队列，优先驱动系数修正（消费规则见数据模型 §5.3"学习消费方式"）。

### 1.5 与 Learning Loop 的衔接

- **映射自身也是被学习对象**：低误差样本（预测准的）用于验证映射收敛——若连续两周低误差样本占比上升，说明系数与映射共同收敛；若高误差集中在某几个箱，优先重建边界而非全局调参。
- **防自指**：映射更新只使用 `feedback` 已回填的样本；`expected_value` 永远取决策时刻的映射版本（数据红线：历史不回写），因此映射演进不会改写历史 `prediction_error`。

---

## §2 匹配引擎：粗排 / 精排 / content_value 合成（自研核心③）

服务对象：内容管线每轮产出"想说清单"候选（`Scorecard`），供时机引擎 `decide()` 消费。原则：**白盒可解释**（每条推荐可回答"为什么"）、**成本分层**（embedding 粗排全量便宜，LLM 精排只碰 Top-K 且单次批量调用）。

### 2.0 输入输出与节奏

| 项 | 内容 |
|---|---|
| 输入 | `content_items`（已打标，含 embedding）、活跃兴趣节点集（P0=配置种子兴趣）、近 7 天 `speak.*` 反馈统计 |
| 输出 | ≤3 条候选入想说清单：每条带 `content_value`、`matched_node_id`、`matched_reason`（白盒理由） |
| 触发 | 每 30 min 一轮，或抓取批次新入库 ≥20 条时提前触发 |

### 2.1 粗排（向量召回 + 规则过滤，候选 ~200 → 20）

```
输入：候选池 C（近 48h 入库且未推荐过的 content_items）
1. 质量前置过滤：剔除 quality_score < 0.4 的条目（打标阶段已评）
2. 相关度计算：
   活跃兴趣节点集 I，节点 i 有描述向量 v_i 与温度 w_i（P0 全为 1）
   rel(c) = max_i [ cos(c.embedding, v_i) × w_i ]
   matched_node(c) = argmax_i（记录来源节点 = 白盒理由的原料）
3. 新鲜度惩罚：若 ∃ 已消费内容 c′ 使 cos(c.embedding, c′.embedding) ≥ 0.9
   → rel(c) ×= 0.3（用户大概率已知；P0 无消费历史则跳过本步）
4. 多样性约束：按 rel 降序取 Top 20，且每个兴趣节点最多入选 5 条（防单一兴趣霸榜）
输出：Top-20 候选，各带 rel、matched_node_id
```

> 偏差（05 §11.25）：实测 Top-K 纯按 rel 排，重抓的旧文会霸榜。实现改为
> `score = rel × (0.5 + 0.5 × freshness)`，freshness = 按 published_ts 的 τ=72h
> 指数衰减（缺发布时间退 fetched_ts；未来时间戳判废）。语义仍是"新鲜度调权"，
> 不属于新增排序维度。

### 2.2 精排（LLM 批量评分 + 理由生成，20 → ≤3）

**单次批量调用**（成本红线：每轮仅 1 次 LLM 调用，逐条调用禁止）：

```
prompt 结构：
  [系统] 你是用户的兴趣搭子的选题编辑。根据用户兴趣上下文给候选内容打分。
  [用户兴趣上下文]
    - 活跃兴趣 Top-10：{节点名, 温度, 最近相关反馈（被追问/被跳过的主题词）}
    - 消费偏好：{偏短内容 / 偏视频 / 近期晚间命中率高 …}（P0 可空）
  [候选] 20 条：{id, 标题, 摘要, 标签, quality_score}
  [要求] 返回 JSON 数组，每条：
    { id, relevance ∈ [0,1], reason: "一句话，必须点名与哪个兴趣/实体的关联" }
校验：
  - relevance 越界 → 钳制到 [0,1]；缺 id → 该条作废
  - reason 为空或未引用任何兴趣实体 → relevance ×= 0.5（可解释性强制，无理由即降权）
```

**入队规则**：`relevance ≥ 0.55` 且本轮名额 ≤3（按合成 `content_value` 降序）。理由 `reason` 落库 `content_items.matched_reason`——它是精灵开口时"给你看这个是因为……"的唯一来源（接口红线5：讲述锚定检索结果，禁止自由生成推荐理由）。

### 2.3 content_value 合成（写入 Scorecard）

```
content_value = (0.7 × relevance + 0.3 × quality_score) × time_decay × freshness

time_decay = exp(-Δt_hours / τ)
  τ:  news = 6h | thread/video = 24h | tutorial/docs = ∞（不衰减）
freshness = 粗排第 3 步的惩罚系数（1 或 0.3）
```

**数值示例**：某条 news，入库 3h，relevance=0.8，quality=0.7，无新鲜度惩罚：
```
base        = 0.7×0.8 + 0.3×0.7 = 0.77
time_decay  = exp(-3/6) ≈ 0.607
content_value = 0.77 × 0.607 × 1 ≈ 0.467
```
该值即时机评分公式 `speak_score = content_value × state_factor × timing_factor − …` 中的 `content_value` 因子，并与 §1 校准映射衔接（决策时经 `mapping(s)` 得 `expected_value`）。

### 2.4 与学习闭环的衔接

- 每条开口的反馈回填 `feedback.prediction_error`（§1）后：**假阴性样本（expected 低 × actual 高）是发现新兴趣的最强信号** → Review 管线触发兴趣图谱抽取复核，可能新增兴趣节点 → 下轮粗排的 `I` 集变化。匹配引擎与时机引擎共用同一条误差驱动回路，不另建学习通道。
- 反馈统计（近 7 天各主题词的追问/跳过）作为精排 prompt 的"用户兴趣上下文"原料——精排随反馈进化，但**不改变** §1 校准的独立性（精排影响候选是谁，校准影响预测准不准，两者解耦）。

### 2.5 P0 裁剪版

| 项 | P0 做法 | 完整版（P1+） |
|---|---|---|
| 兴趣源 | coefficients.yaml 手工种子兴趣 3-5 个，温度全 1 | Graphiti 时态图谱 + 温度衰减 |
| 相关度来源 | **LLM 打标直出 `interest_scores`**（每兴趣 0~1；无 key 降级为兴趣名 2-gram 关键词重合），替代 embedding——P0 兴趣节点少、日内容量小，一次打标调用顺带产出相关度，比引入 embedding 基础设施更省且更可解释 | embedding 入库时单次计算（本地 bge-small 或 API），缓存，余弦粗排 |
| 粗排 | rel = max(interest_scores)，新鲜度惩罚跳过（无消费历史） | 全量 |
| 精排 | 保留，候选 20→10，每轮 1 次调用；reason 强制锚定输入标题/摘要（接口红线⑤），LLM 失败降级 rel 直通 | 全量 |
| 反馈上下文 | 空 | 反馈统计注入 prompt |
