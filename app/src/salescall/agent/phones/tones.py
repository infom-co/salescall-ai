"""電話の音を聞き分ける1か所（docs/PHONE.md 2-3・2-4）：呼び出しの音（お客様の電話が鳴っている）と話し中の音。

- 1つの切れ目（20ms）ごとに、決まった高さ（`TONES` の周波数）の音が、全体の音のどれだけを占めるかを見る（Goertzel の計算。
  1つの高さだけを見る、軽い計算）。占める割合が `SHARE` より多ければ「音が鳴っている切れ目」。
- 鳴る・休むの長さの並び（`Cadence`）が、表の型（鳴る秒・休む秒）に、ゆとり `SLACK` の内で2回続けて合えば、その音と決める。
- 表の値は日本の標準の音（400Hz。呼び出し＝1秒鳴って2秒休む・話し中＝0.5秒ずつ）。⚠️ Rakuten のネットワークの音が同じかは分からない。
  確かめ R-6 で本物を録り、表の値をそれに合わせて決める（推し量って決めない）。決められないときは None＝分からない（決めつけない）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from salescall.agent.phones.audio import FRAME_MS, samples


@dataclass(frozen=True)
class Tone:
    hz: float
    on_s: float
    off_s: float


# ⏳ R-6 で本物の音に合わせる（いまは日本の標準の値）
TONES: dict[str, Tone] = {
    "ringback": Tone(400.0, 1.0, 2.0),   # 呼び出しの音
    "busy": Tone(400.0, 0.5, 0.5),       # 話し中の音
}
SHARE = 0.5      # その高さの音が、切れ目の全体の音のうち、これより多く占めれば「鳴っている」
FLOOR = 200.0    # 切れ目の音の大きさ（二乗平均の平方根）がこれより小さければ「休み」（静けさで割合を出さない）
SLACK = 0.3      # 鳴る・休むの長さのゆとり（割合）
REPEAT = 2       # 型に合う鳴る・休むが、続けてこの回数そろえば決める


def share(pcm: bytes, rate: int, hz: float) -> float:
    """その切れ目で、hz の音が全体の音のどれだけを占めるか（0〜1）。音が小さすぎれば 0。"""
    x = samples(pcm)
    n = len(x)
    if n == 0:
        return 0.0
    energy = sum(v * v for v in x)
    if math.sqrt(energy / n) < FLOOR:
        return 0.0
    w = 2 * math.pi * hz / rate
    coeff = 2 * math.cos(w)
    s1 = s2 = 0.0
    for v in x:
        s0 = v + coeff * s1 - s2
        s2, s1 = s1, s0
    power = s1 * s1 + s2 * s2 - coeff * s1 * s2   # その高さの強さ（Goertzel）
    return min(1.0, 2 * power / (n * energy))


class Cadence:
    """切れ目を1つずつ受け取り、呼び出しの音・話し中の音と決まったら、その名前を返す（決まるまで None）。"""

    def __init__(self, rate: int, ms: int = FRAME_MS) -> None:
        self.rate = rate
        self.step = ms / 1000
        self._on = False
        self._run = 0.0
        self._runs: list[tuple[bool, float]] = []   # （鳴っていたか、長さ）の並び

    def push(self, pcm: bytes) -> str | None:
        hz = {t.hz for t in TONES.values()}
        on = any(share(pcm, self.rate, f) >= SHARE for f in hz)
        if on == self._on:
            self._run += self.step
            return None
        self._runs.append((self._on, self._run))
        self._on, self._run = on, self.step
        return self.decide()

    def decide(self) -> str | None:
        runs = [r for r in self._runs if r[1] > 0]
        for name, t in TONES.items():
            pairs = 0
            for (a_on, a_len), (b_on, b_len) in zip(runs, runs[1:]):
                if a_on and not b_on and _near(a_len, t.on_s) and _near(b_len, t.off_s):
                    pairs += 1
                    if pairs >= REPEAT:
                        return name
                elif a_on:
                    pairs = 0
        return None


def _near(got: float, want: float) -> bool:
    return abs(got - want) <= want * SLACK
