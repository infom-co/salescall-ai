"""録音を流している間の、相手の声の見張り（DESIGN.md 6-13 の案「録音を流している間の仕組み」。フェーズ0の試作）。

相手の声の大きさだけで、声のかたまり（話し始めから話し終わりまで）を見る。文字にする処理は待たない。決めることは3つ：
- 短い声（short_s より短く終わった）＝相づち。最初の質問より前なら、録音を流し続け、声は捨てる（名乗り・用件の間の相づちは答えではない）
- 最初の質問の録音が流れ始めてからの短い声は、流し切ったあとに答えとしてAIへ送る（「はい」は質問への答えでありうる）
- 長い声（short_s を超えて続いた）＝話し始め。その時点で録音を止め、どこまで相手に届いたか（文の途中で止めた文は「まだ」）と、
  話し始めからの声をAIへ渡す
⚠️ 本物の電話の雑音・こだまで、この見張りが誤って動かないかは、ここでは確かめられない（フェーズ0 の R・H）。
⚠️ short_s などの数は、本物の人の相づちの長さで決める（作った相手役の声では「はい。」0.2〜0.3秒・話し始め1.1秒以上）。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

FRAME_S = 0.02


def voiced(pcm16: bytes, rate: int, floor_db: float = -40.0) -> list[bool]:
    """20ミリ秒ごとに、声があるか（音の大きさが floor_db を超えるか。基準は音の最大＝0 dB）。"""
    x = np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32768.0
    n = max(1, int(rate * FRAME_S))
    fr = x[: len(x) // n * n].reshape(-1, n)
    db = 20 * np.log10(np.sqrt((fr ** 2).mean(axis=1)) + 1e-9)
    return [bool(v) for v in db > floor_db]


def voice_runs(frames: list[bool], merge_gap_s: float) -> list[tuple[float, float]]:
    """声のかたまり（始まり・終わりの秒）。merge_gap_s より短い切れ目は、1つのかたまりにつなぐ。"""
    runs: list[list[float]] = []
    for i, v in enumerate(frames):
        if not v:
            continue
        t = i * FRAME_S
        if runs and t - runs[-1][1] <= merge_gap_s:
            runs[-1][1] = t + FRAME_S
        else:
            runs.append([t, t + FRAME_S])
    return [(a, b) for a, b in runs]


@dataclass
class Decision:
    stopped: bool                 # 録音を止めたか
    stop_at_s: float | None       # 止めた時刻（録音の始まりから）
    done: list[str]               # 相手に届いた文
    rest: list[str]               # まだ届いていない文（文の途中で止めた文を含む）
    send_from_s: float | None     # AIへ送る声の始まり（録音の始まりから。None なら送る声は無い）
    dropped: list[tuple[float, float]] = field(default_factory=list)  # 捨てた相づち（始まり・終わり）


def watch(parts: list[tuple[str, float]], callee_pcm: bytes, rate: int, onset_s: float, *, short_s: float,
          merge_gap_s: float = 0.15, floor_db: float = -40.0) -> Decision:
    """parts＝録音の文と長さ（秒）。最後の文が最初の質問。相手の声は、録音の始まりから onset_s 秒のところで始まるとする。"""
    q_start = sum(d for _, d in parts[:-1])
    total = sum(d for _, d in parts)
    dropped: list[tuple[float, float]] = []
    send_from = None
    for a, b in voice_runs(voiced(callee_pcm, rate, floor_db), merge_gap_s):
        start, end = onset_s + a, onset_s + b
        if start >= total:
            break                                   # 流し終えたあとの声は、ふつうの会話（この見張りの外）
        if end - start > short_s:                   # 長い声＝話し始め ⇒ short_s たったところで止める
            stop = start + short_s
            if stop >= total:
                break                               # 止める前に流し終わった
            done, rest, t = [], [], 0.0
            for text, d in parts:
                (done if t + d <= stop else rest).append(text)
                t += d
            return Decision(True, stop, done, rest, start, dropped)
        if start >= q_start:
            send_from = start if send_from is None else send_from  # 最初の質問が流れ始めてからの短い声＝答えでありうる
        else:
            dropped.append((start, end))            # 名乗り・用件の間の相づち ⇒ 捨てる
    return Decision(False, None, [t for t, _ in parts], [], send_from, dropped)


def part_lengths(texts: list[str], morae_per_s: float) -> list[tuple[str, float]]:
    """録音の文ごとの長さの見積もり（音の数 ÷ 話す速さ）。フェーズ0では録音を作っていないので、AIの話す速さから見積もる。"""
    from jtext import morae
    return [(t, morae(t) / morae_per_s) for t in texts]
