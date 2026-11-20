"""声の形を変える1か所（計画書 8-3「変換の処理を、接続モジュールと会話AIの間に1か所だけ置く」。docs/PHONE.md 2-2）。

声は、16bit・1ch・リトルエンディアンの PCM（バイト列）だけを扱う。
- サーバーへ送る形：`TO_SERVER_RATE`（16kHz）を `FRAME_MS`（20ms）ごと（⏳ CL-1 と合わせる＝CL-3 の連絡帳 4-2 の7）。
- 装置の形（48kHz など）との行き来は `resample` の1か所で変える。下げるときは、先に高い音を落としてから間引く（折り返しの雑音を防ぐ）。
Python だけで書く（PC の部品に足す部品を増やさない。1秒の声で数ミリ秒）。
"""

from __future__ import annotations

import array
import math
import sys

TO_SERVER_RATE = 16000
FRAME_MS = 20
_BIG = sys.byteorder == "big"


def samples(pcm: bytes) -> array.array:
    """PCM のバイト列 → 数の並び（16bit・リトルエンディアン）。端の1バイトは捨てる。"""
    a = array.array("h", pcm[: len(pcm) - len(pcm) % 2])
    if _BIG:
        a.byteswap()
    return a


def pcm_of(values: list[float] | array.array) -> bytes:
    """数の並び → PCM のバイト列（はみ出しは端でそろえる）。"""
    a = array.array("h", (max(-32768, min(32767, int(round(v)))) for v in values))
    if _BIG:
        a.byteswap()
    return a.tobytes()


def _kernel(cutoff: float, taps: int = 31) -> list[float]:
    """窓（ハミング）をかけた sinc の FIR の係数。cutoff は標本化の周波数に対する割合（0〜0.5）。"""
    m = taps // 2
    h = []
    for i in range(taps):
        k = i - m
        s = 2 * cutoff if k == 0 else math.sin(2 * math.pi * cutoff * k) / (math.pi * k)
        h.append(s * (0.54 - 0.46 * math.cos(2 * math.pi * i / (taps - 1))))
    total = sum(h)
    return [v / total for v in h]


def resample(pcm: bytes, src: int, dst: int) -> bytes:
    """標本化の周波数を src から dst へ変える（声の形を変える1か所）。
    下げるとき：出す点の所だけで、高い音を落とした値を計算する（全部の点で計算しない＝通話の声を遅らせない）。
    上げるとき：となりの2点の間をつなぐ（上げても新しい高い音は生まれない）。"""
    x = samples(pcm)
    if src == dst or not x:
        return pcm_of(x)
    n_out = int(len(x) * dst / src)
    out: list[float] = []
    if dst < src:
        h = _kernel(0.45 * dst / src)
        m = len(h) // 2
        n = len(x)
        for i in range(n_out):
            c = int(round(i * src / dst))
            lo, hi = max(0, c - m), min(n, c + m + 1)
            out.append(sum(h[j - c + m] * x[j] for j in range(lo, hi)))
        return pcm_of(out)
    for i in range(n_out):
        t = i * src / dst
        j = int(t)
        f = t - j
        a = x[j]
        b = x[j + 1] if j + 1 < len(x) else a
        out.append(a + (b - a) * f)
    return pcm_of(out)


class Framer:
    """届いた声を、決まった長さ（既定 20ms）の切れ目にそろえる（端は次に回す）。"""

    def __init__(self, rate: int = TO_SERVER_RATE, ms: int = FRAME_MS) -> None:
        self.size = rate * ms // 1000 * 2
        self._rest = b""

    def push(self, pcm: bytes) -> list[bytes]:
        buf = self._rest + pcm
        n = len(buf) // self.size
        self._rest = buf[n * self.size:]
        return [buf[i * self.size:(i + 1) * self.size] for i in range(n)]


def rms_of(pcm: bytes) -> float:
    """その切れ目の音の大きさ（二乗平均の平方根）。"""
    x = samples(pcm)
    return (sum(v * v for v in x) / len(x)) ** 0.5 if len(x) else 0.0
