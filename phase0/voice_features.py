"""声の性質を数で表す（同じ人の声に聞こえるかを比べるため。DESIGN.md 6-13）。

✅ 2026-10-15 ユーザー（S4 の「承知しました、」の候補を聞いて）：「02＞01＞04＞03。誠実で若々しい声でとてもよい。ただし、続く声が、
   おっさんのしゃがれた声で、年齢層や人物が違うように聞こえる。02の声に他が合わせられると一番良い」
⇒ 同じ声の名前（Sadachbia）でも、録るたびに声の高さ・かすれ具合が変わる。人が選んだ1本（基準の声）と比べるための数を出す。
- 高さ：声の出ている区間の基本の高さ（Hz）の中央。半音の差で比べる（12半音＝1オクターブ）。
- 澄み具合：声の周期の揃い方（0〜1。自己相関の山の高さ）の中央。かすれた声・しゃがれた声ほど低い。
- 明るさ：音の重心の高さ（Hz）の中央。こもった声ほど低い（⚠️ 言葉の音で大きく変わる。同じ文どうしでしか比べない）。
- 低く沈む割合：その録音の中央の高さより 7半音（約0.67倍）以上低い区間の割合。⚠️ 2026-10-15：ユーザーが「おっさんのしゃがれた声」と
  聞いた S4 の「続きまして…」の録音は 18%（100Hz より下の区間が41）、「誠実で若々しい」と聞いた候補2は 0%。声がしゃがれる・
  かすれるところ（きしむような低い声）で出やすい。⏳ 人の耳との対応は、まだ1本ずつしか確かめていない（G-38 で確かめる）。
⚠️ どれも、人の耳の「同じ人に聞こえるか」の代わりにはならない。人が聞く前に、明らかに違う録音を落とすための目安。
"""
from __future__ import annotations

import math

import numpy as np

FRAME_S = 0.03  # 比べる長さ（1つの区間）
HOP_S = 0.01
F0_MIN, F0_MAX = 70.0, 450.0
FLOOR_DB = -35.0  # 声の出ている区間とみなす大きさ（最も大きい区間から）
YIN_THRESHOLD = 0.15  # 周期のずれがこれより小さい最初の谷を、声の周期とする（1オクターブの取り違えを避ける）
APERIODIC_MAX = 0.35  # 周期のずれがこれより大きい区間は、高さを数えない（子音・息・かすれ）
LOW_ST = 7.0  # 中央の高さより、これだけ（半音）低い区間を「低く沈む」とする


def _yin(frame: np.ndarray, w: int, lag_lo: int, lag_hi: int) -> tuple[float, float]:
    """1つの区間の (周期の長さ, 周期のずれ 0〜1)。周期のずれ＝どれだけ周期が揃っていないか（小さいほど澄んだ声）。"""
    x = frame - frame.mean()
    e = np.concatenate([[0.0], np.cumsum(x ** 2)])
    m = 1 << int(np.ceil(np.log2(len(x) + w)))
    r = np.fft.irfft(np.conj(np.fft.rfft(x[:w], m)) * np.fft.rfft(x, m), m)[: lag_hi + 1]
    tau = np.arange(lag_hi + 1)
    d = e[w] + (e[tau + w] - e[tau]) - 2 * r
    d[0] = 0.0
    cum = np.cumsum(d[1:])
    cmnd = np.ones_like(d)
    cmnd[1:] = d[1:] * tau[1:] / np.where(cum > 0, cum, 1e-12)
    band = cmnd[lag_lo:lag_hi + 1]
    below = np.nonzero(band < YIN_THRESHOLD)[0]
    if len(below):
        i = int(below[0])
        while i + 1 < len(band) and band[i + 1] < band[i]:  # その谷の底まで進む
            i += 1
    else:
        i = int(np.argmin(band))
    return float(lag_lo + i), float(band[i])


def features(pcm16: bytes, rate: int) -> dict | None:
    """声の性質 {pitch_hz, pitch_spread_st, clarity, brightness_hz, low_ratio, frames}。声が無ければ None。
    clarity＝1−周期のずれ（中央）。brightness_hz＝音の重心の高さ（中央）。"""
    x = np.frombuffer(pcm16, dtype="<i2").astype(np.float64) / 32768.0
    lag_lo, lag_hi = int(rate / F0_MAX), int(rate / F0_MIN)
    w, hop = int(rate * FRAME_S), int(rate * HOP_S)
    n = w + lag_hi
    if len(x) < n:
        return None
    frames = np.lib.stride_tricks.sliding_window_view(x, n)[::hop]
    rms = np.sqrt((frames[:, :w] ** 2).mean(axis=1)) + 1e-12
    db = 20 * np.log10(rms / rms.max())
    freqs = np.fft.rfftfreq(w, 1 / rate)
    win = np.hanning(w)
    pitches, ap, bright = [], [], []
    for f in frames[db > FLOOR_DB]:
        lag, a = _yin(f, w, lag_lo, lag_hi)
        mag = np.abs(np.fft.rfft((f[:w] - f[:w].mean()) * win))
        bright.append(float((freqs * mag).sum() / (mag.sum() + 1e-12)))
        if a <= APERIODIC_MAX:
            pitches.append(rate / lag)
            ap.append(a)
    if len(pitches) < 5:
        return None
    st = 12 * np.log2(np.array(pitches) / np.median(pitches))
    return {"pitch_hz": round(float(np.median(pitches)), 1),
            "pitch_spread_st": round(float(np.percentile(st, 90) - np.percentile(st, 10)), 2),
            "clarity": round(1 - float(np.median(ap)), 3), "brightness_hz": round(float(np.median(bright))),
            "low_ratio": round(float((st <= -LOW_ST).mean()), 3), "frames": len(pitches)}


def unvoiced_spans(pcm16: bytes, rate: int, min_s: float = 0.03) -> list[tuple[float, float]]:
    """声帯の震えの無い区間（無音・息の音＝「っし」の「シ」など。周期のずれが APERIODIC_MAX より大きいか、小さすぎる音）の (始まり, 終わり) の秒。
    録音を継ぐ位置を決めるため（声の高さの継ぎ目が出ない所。g58_name_splice の4回目）。min_s より短い区間は除く。"""
    x = np.frombuffer(pcm16, dtype="<i2").astype(np.float64) / 32768.0
    lag_lo, lag_hi = int(rate / F0_MAX), int(rate / F0_MIN)
    w, hop = int(rate * FRAME_S), int(rate * HOP_S)
    n = w + lag_hi
    if len(x) < n:
        return []
    frames = np.lib.stride_tricks.sliding_window_view(x, n)[::hop]
    rms = np.sqrt((frames[:, :w] ** 2).mean(axis=1)) + 1e-12
    db = 20 * np.log10(rms / rms.max())
    voiceless = [d <= FLOOR_DB or _yin(f, w, lag_lo, lag_hi)[1] > APERIODIC_MAX for f, d in zip(frames, db)]
    spans, start = [], None
    for i, v in enumerate(voiceless + [False]):
        t = (i * hop + w / 2) / rate
        if v and start is None:
            start = t
        elif not v and start is not None:
            if t - start >= min_s:
                spans.append((round(start, 3), round(t, 3)))
            start = None
    return spans


def semitones(a_hz: float, b_hz: float) -> float:
    """a が b より何半音高いか。"""
    return 12 * math.log2(a_hz / b_hz)


def read_wav(path) -> tuple[bytes, int]:
    import wave
    with wave.open(str(path)) as w:
        assert w.getsampwidth() == 2 and w.getnchannels() == 1, path
        return w.readframes(w.getnframes()), w.getframerate()
