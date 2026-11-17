"""expected_value 稳态等频分箱校准（docs/04 §1.2 建箱/§1.3 维护节奏，纯函数可单测）。

- 建箱/重建（weekly/相位切换时）：全量样本等频切 K=10 箱，边界取相邻分箱样本 score 均值
- EMA 更新（nightly，仅 steady）：边界不动，用新样本重算 rate_raw → EMA α=0.3
- 稀疏保护：箱样本数 <5 → 保留上期；首期向相邻有效箱借均值
- 单调化：向右传播（rate[k] < rate[k-1] → rate[k] = rate[k-1]）
"""

K_BINS = 10
EMA_ALPHA = 0.3
MIN_BIN_SAMPLES = 5
_EPS = 0.01  # 首末箱边界外扩（yaml 安全的有限值，代替 ±∞）


def _monotonize(rates: list[float]) -> list[float]:
    out = list(rates)
    for k in range(1, len(out)):
        if out[k] < out[k - 1]:
            out[k] = out[k - 1]
    return out


def _raw_rates(samples: list[tuple[float, float]], edges: list[float]) -> tuple[list[float | None], list[int]]:
    """各箱接受率与样本数；空箱 rate=None。"""
    counts = [0] * K_BINS
    sums = [0.0] * K_BINS
    for s, a in samples:
        k = locate_bin(edges, s)
        counts[k] += 1
        sums[k] += a
    rates = [sums[k] / counts[k] if counts[k] > 0 else None for k in range(K_BINS)]
    return rates, counts


def locate_bin(edges: list[float], s: float) -> int:
    """决策时查箱：edges[k] <= s < edges[k+1]，越界归首/末箱。"""
    if s < edges[0]:
        return 0
    for k in range(K_BINS):
        if edges[k] <= s < edges[k + 1]:
            return k
    return K_BINS - 1


def _finalize(raw: list[float | None], counts: list[int], prev_rates: list[float] | None) -> list[float]:
    """EMA → 稀疏保护 → 单调化（docs/04 §1.2 步骤 4-6）。"""
    rates: list[float] = [0.0] * K_BINS
    for k in range(K_BINS):
        if counts[k] >= MIN_BIN_SAMPLES and raw[k] is not None:
            # EMA（有上期映射时）；首期 rate = rate_raw
            rates[k] = (
                EMA_ALPHA * raw[k] + (1 - EMA_ALPHA) * prev_rates[k] if prev_rates else raw[k]
            )
        elif prev_rates is not None:
            rates[k] = prev_rates[k]  # 稀疏保护：保留上期
        else:
            rates[k] = -1.0  # 首期稀疏箱：占位，稍后向相邻箱借均值
    if prev_rates is None:
        valid_mean = [r for r, c in zip(rates, counts) if r >= 0 and c >= MIN_BIN_SAMPLES]
        fallback = sum(valid_mean) / len(valid_mean) if valid_mean else 0.5
        for k in range(K_BINS):
            if rates[k] < 0:
                neighbors = [
                    rates[j]
                    for j in (k - 1, k + 1)
                    if 0 <= j < K_BINS and 0 <= rates[j]
                ]
                rates[k] = sum(neighbors) / len(neighbors) if neighbors else fallback
    return [round(r, 4) for r in _monotonize(rates)]


def build_bins(samples: list[tuple[float, float]], prev: dict | None = None) -> dict:
    """全量建箱/重建（docs/04 §1.2 步骤 1-6）。samples=[(score, actual)]，按 score 等频切箱。"""
    ss = sorted(samples, key=lambda x: x[0])
    m = max(1, len(ss) // K_BINS)
    edges = [ss[0][0] - _EPS]
    for k in range(1, K_BINS):
        i = min(k * m, len(ss) - 2)  # 保证 i+1 合法
        edges.append((ss[i][0] + ss[i + 1][0]) / 2)
    edges.append(ss[-1][0] + _EPS)
    raw, counts = _raw_rates(ss, edges)
    rates = _finalize(raw, counts, prev["accept_rate"] if prev else None)
    return {
        "edges": [round(e, 4) for e in edges],
        "accept_rate": rates,
        "bin_counts": counts,
    }


def update_rates(new_samples: list[tuple[float, float]], bins: dict) -> dict:
    """nightly EMA（docs/04 §1.3）：边界不动，用当日新回填样本重算 rate_raw → 步骤 4-6。"""
    edges = [float(e) for e in bins["edges"]]
    raw, counts = _raw_rates(new_samples, edges)
    rates = _finalize(raw, counts, [float(r) for r in bins["accept_rate"]])
    return {"edges": bins["edges"], "accept_rate": rates, "bin_counts": counts}
