"""声を少しだけ高く・軽くする加工（話す速さは変えない）。⏳ 案（DESIGN.md 6-13）：ユーザーが聞いて決める。

✅ 2026-10-15 ユーザー：「録音だけでなく、AIでも、この声（若々しい声）が普通に出るようにしないと別人になってしまう」
⛔ 話し方の指示では変わらなかった：G-39・G-40 で8通りの指示（「若々しい声で」「トーンを高めに」「入社2年目くらいの…」など）を試し、
   会話の中のAIの声は24本とも「おっさんっぽい」（ユーザー）。表現が一番良かったのは、何も足していない、いまの指示（G-39 の09・G-40 の10）。
⇒ 声の名前（Sadachbia。✅ ユーザーが男性の声を全て試して選んだ）と指示はそのままにして、音そのものを、電話接続側で加工する案。
   録音にも、AIの声にも、同じ加工をかける（1人の人物に聞こえるように）。
やり方：声を ratio 倍の速さで流したのと同じ音にして（高さと声の響きが ratio 倍に上がる。短くなる）、そのあと、波形の形をつなぎ直して、
   長さを元に戻す（WSOLA。高さと響きはそのまま、速さだけ元に戻る）。
⚠️ 加工には、区切り1つ分（約 0.02秒）の遅れが要る。音が不自然にならないか・本物の電話の経路で聞こえ方が変わらないかは、人の耳と R・H で確かめる。
"""
from __future__ import annotations

import numpy as np

FRAME_S = 0.03     # つなぎ直す区切りの長さ
SEARCH_S = 0.008   # つなぎ目を合わせるために、前後に探す幅


def _resample(x: np.ndarray, n_out: int) -> np.ndarray:
    """線形の補間で長さを変える（ratio が 1 に近いので、これで足りる）。"""
    return np.interp(np.linspace(0, len(x) - 1, n_out), np.arange(len(x)), x)


def stretch(x: np.ndarray, factor: float, rate: int) -> np.ndarray:
    """高さを変えずに、長さを factor 倍にする（WSOLA）。"""
    n = int(rate * FRAME_S)
    hop_out = n // 2
    hop_in = hop_out / factor
    tol = int(rate * SEARCH_S)
    win = np.hanning(n)
    n_frames = int((len(x) - n - 2 * tol) / hop_in)
    if n_frames <= 1:
        return x.copy()
    out = np.zeros(n_frames * hop_out + n)
    norm = np.zeros_like(out)
    prev = None  # 前の区切りを自然に続けた先（つなぎ目の手本）
    for k in range(n_frames):
        center = int(round(k * hop_in)) + tol
        if prev is None:
            start = center
        else:
            lo, hi = center - tol, center + tol
            seg = x[lo: hi + n]
            corr = np.correlate(seg, prev, mode="valid")
            start = lo + int(np.argmax(corr))
        frame = x[start: start + n]
        out[k * hop_out: k * hop_out + n] += frame * win
        norm[k * hop_out: k * hop_out + n] += win
        prev = x[start + hop_out: start + hop_out + n]
        if len(prev) < n:
            break
    norm[norm < 1e-6] = 1.0
    return out / norm


def shift(pcm16: bytes, rate: int, ratio: float) -> bytes:
    """声の高さと響きを ratio 倍にする（長さは変えない）。ratio=1.0 ならそのまま返す。"""
    if abs(ratio - 1.0) < 1e-6:
        return pcm16
    x = np.frombuffer(pcm16, dtype="<i2").astype(np.float64)
    faster = _resample(x, int(round(len(x) / ratio)))
    y = stretch(faster, ratio, rate)
    y = _resample(y, len(x)) if abs(len(y) - len(x)) > rate * 0.05 else y
    return np.clip(y, -32768, 32767).astype("<i2").tobytes()
