"""M8 后台 Review 管线（docs/01 §M8；接口 docs/03 §3.5；实现规格 docs/05 §7.4）。

Learning Loop 的"评估+沉淀+再部署"：
- nightly：prediction_error 降序消费反馈样本 → 校准维护（相位切换/EMA）→
  LLM 复盘改写 coefficients.yaml（token 预算硬护栏，超限降级纯统计）→ 温度重算
- weekly：校准边界重建、温度全量重算、技能归档、成长汇报数据（记忆 diff + 环比）
"""
