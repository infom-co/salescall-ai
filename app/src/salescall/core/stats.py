"""数え方の道具（二項分布）。持ち主 CL-2。

偶然のぶれを見込む計算は、ここの1か所（誤判定率の上の限度＝core/measure.py、応答率の低下＝core/answer_rate.py が使う）。
"""

from __future__ import annotations

import math


def at_most(k: int, n: int, p: float) -> float:
    """1本ごとに確率 p で起きることが、n 本のうち k 本以下しか起きない確率。"""
    if not 0 <= k <= n:
        raise ValueError("本数が合いません")
    if p <= 0:
        return 1.0
    if p >= 1:
        return 1.0 if k >= n else 0.0
    logs = [math.lgamma(n + 1) - math.lgamma(i + 1) - math.lgamma(n - i + 1) + i * math.log(p) + (n - i) * math.log1p(-p)
            for i in range(k + 1)]
    top = max(logs)
    return min(1.0, math.exp(top) * sum(math.exp(x - top) for x in logs))


def upper_bound(k: int, n: int, confidence: float = 0.95) -> float:
    """n 本のうち k 本だったとき、本当の割合がこの値より大きい見込みは (1 − confidence) 以下（片側の正確な上の限度＝
    Clopper-Pearson）。例：60本で0本 ⇒ 約0.049。"""
    if not 0 <= k <= n or n == 0:
        raise ValueError("本数が合いません")
    if k == n:
        return 1.0
    alpha = 1 - confidence
    lo, hi = k / n, 1.0
    for _ in range(200):
        mid = (lo + hi) / 2
        if at_most(k, n, mid) > alpha:
            lo = mid
        else:
            hi = mid
    return hi
