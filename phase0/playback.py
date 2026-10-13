"""定型の音声（録音）を流す側の決まり（フェーズ0の試作。DESIGN.md 6-13 の案）。電話接続モジュールの中の1か所に置く。

- 名乗りの録音は、文ごとの録音（Clip）を順に流す。どの文まで相手に届いたかを、プログラムが正確に知るため。
- 流している間も、相手の声を見張る（barge_in_at）：
  - 「はい」などの短い相づち（MIN_SPEECH_S 未満）は、止めない。日本の電話では、名乗りの途中に「はい」と言う人が多い。
  - それより長く話し始めたら、止める。止めた時刻から、届いた文と、まだの文が決まる（split_played）。
  - 流している間の相手の声は、会話AIへ送らずに取っておく。止めたら、「どこまで届いたか」をAIに知らせてから
    （instruction.barge_in_note）、話し始めからの声をAIへ送る。最後まで流れたら、相づちの声は捨てる。
⚠️ 限界：実際の電話では、こちらの録音の声が相手の側から戻ってくる（エコー）。それを相手の声と取り違えないかは、
   電話の接続（フェーズ1、R-4）で確かめる。ここでは、相手の声だけが入る形で決まりを確かめる。
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

FRAME_S = 0.02          # 声があるかを見る区切り（20ミリ秒）
ENERGY = 0.02           # この大きさを超える区切りを「声がある」とみなす（最大振幅を1とした実効値）
MAX_GAP_S = 0.25        # 声の切れ目がこれより短ければ、続いた声とみなす（「どちら、様ですか」の息継ぎ）
MIN_SPEECH_S = 0.8      # これより長く続いた声を「話し始めた」とみなす。短いものは相づちとして止めない（設定の値）


@dataclass
class Clip:
    text: str
    seconds: float


def speech_frames(pcm16: bytes, rate: int) -> np.ndarray:
    x = np.frombuffer(pcm16, dtype="<i2").astype(np.float32) / 32768.0
    n = int(rate * FRAME_S)
    k = len(x) // n
    if k == 0:
        return np.zeros(0, dtype=bool)
    frames = x[: k * n].reshape(k, n)
    return np.sqrt((frames ** 2).mean(axis=1)) > ENERGY


def barge_in_at(callee_pcm16: bytes, rate: int, *, min_speech_s: float = MIN_SPEECH_S) -> float | None:
    """相手の声（録音を流し始めた時刻を0とする）の中で、「話し始めた」とみなす時刻（秒）を返す。無ければ None。
    返すのは、続いた声が min_speech_s に届いた時刻（＝止める時刻）。話し始めの時刻は speech_start_before で求める。"""
    voiced = speech_frames(callee_pcm16, rate)
    run_start, last_voice, gap = None, None, int(MAX_GAP_S / FRAME_S)
    for i, v in enumerate(voiced):
        if v:
            if run_start is None or (last_voice is not None and i - last_voice > gap):
                run_start = i
            last_voice = i
            if (i - run_start + 1) * FRAME_S >= min_speech_s:
                return round((i + 1) * FRAME_S, 3)
    return None


def speech_start_before(callee_pcm16: bytes, rate: int, stop_s: float) -> float:
    """止めた時刻の直前の、話し始めの時刻（AIへは、ここからの相手の声を送る。話し始めを切り落とさないため）。"""
    voiced = speech_frames(callee_pcm16, rate)
    i = min(int(stop_s / FRAME_S), len(voiced)) - 1
    gap = int(MAX_GAP_S / FRAME_S)
    start = i
    silent = 0
    while i >= 0:
        if voiced[i]:
            start, silent = i, 0
        else:
            silent += 1
            if silent > gap:
                break
        i -= 1
    return round(max(0, start) * FRAME_S, 3)


def split_played(clips: list[Clip], stop_s: float | None) -> tuple[list[str], list[str]]:
    """止めた時刻で、相手に届いた文（最後まで流れた文）と、まだ届いていない文に分ける。
    途中で止めた文は「まだ」に入れる（言い切っていない文を、届いたことにしない）。"""
    if stop_s is None:
        return [c.text for c in clips], []
    t, done, rest = 0.0, [], []
    for c in clips:
        if t + c.seconds <= stop_s:
            done.append(c.text)
        else:
            rest.append(c.text)
        t += c.seconds
    return done, rest
