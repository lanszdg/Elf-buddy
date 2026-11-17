"""Review 管线（docs/03 §3.5）。P0 零学习，仅占位钩子——结构落位，不执行。

P1 接入：prediction_error 降序消费（docs/02 §5.3）→ 改写 coefficients.yaml；
校准映射稳态切换（docs/04 §1.2）。
"""


async def nightly() -> None:  # pragma: no cover - P0 不调度
    raise NotImplementedError("P1 实现")
