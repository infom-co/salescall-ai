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
# 息の音（「し」「ち」の息）の音の重心の下限。⛔ 2026-10-19 G-58：周期が無いだけで探すと、「様は｜いら」の間のかすれた声（音の重心 1,400〜2,600Hz）
# を息の音と取り違えた（本物の息の音は 5,400〜7,300Hz）
NOISE_MIN_HZ = 4000.0
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


def unvoiced_spans(pcm16: bytes, rate: int, min_s: float = 0.03, noise_only: bool = False) -> list[tuple[float, float]]:
    """声帯の震えの無い区間（無音・息の音＝「っし」の「シ」など。周期のずれが APERIODIC_MAX より大きいか、小さすぎる音）の (始まり, 終わり) の秒。
    録音を継ぐ位置を決めるため（声の高さの継ぎ目が出ない所。g58_name_splice の4回目）。min_s より短い区間は除く。
    noise_only：無音は数えず、音のある息の音（周期のずれが大きく、音の重心が NOISE_MIN_HZ より高い音）だけ（name_clips.seam_cut。
    ⛔ 無音を数えると、「様で」のあとの間＝言葉の間で切っていた）。"""
    x = np.frombuffer(pcm16, dtype="<i2").astype(np.float64) / 32768.0
    lag_lo, lag_hi = int(rate / F0_MAX), int(rate / F0_MIN)
    w, hop = int(rate * FRAME_S), int(rate * HOP_S)
    n = w + lag_hi
    if len(x) < n:
        return []
    frames = np.lib.stride_tricks.sliding_window_view(x, n)[::hop]
    rms = np.sqrt((frames[:, :w] ** 2).mean(axis=1)) + 1e-12
    db = 20 * np.log10(rms / rms.max())
    if noise_only:
        freqs, win = np.fft.rfftfreq(w, 1 / rate), np.hanning(w)

        def bright(f):
            mag = np.abs(np.fft.rfft((f[:w] - f[:w].mean()) * win))
            return float((freqs * mag).sum() / (mag.sum() + 1e-12))
        voiceless = [d > FLOOR_DB and _yin(f, w, lag_lo, lag_hi)[1] > APERIODIC_MAX and bright(f) >= NOISE_MIN_HZ for f, d in zip(frames, db)]
    else:
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


def pitch_at(pcm16: bytes, rate: int, t: float, max_aperiodic: float = APERIODIC_MAX) -> float | None:
    """t 秒から始まる1つの区間（FRAME_S と、周期を探す長さ）の声の高さ（Hz）。周期が揃わない（子音・息・無音）なら None。
    録音を継ぐ所の前後の高さを比べるため（g69_vowel_seam。features は区間5つ以上の中央なので、継ぎ目の1点には使えない）。
    max_aperiodic：周期のずれの上限（既定は APERIODIC_MAX。かすれた母音の1周期の長さを取るときだけ、緩くする）。"""
    x = np.frombuffer(pcm16, dtype="<i2").astype(np.float64) / 32768.0
    lag_lo, lag_hi = int(rate / F0_MAX), int(rate / F0_MIN)
    w, i = int(rate * FRAME_S), int(t * rate)
    f = x[max(0, i):max(0, i) + w + lag_hi]
    if i < 0 or len(f) < w + lag_hi or np.sqrt((f[:w] ** 2).mean()) < 1e-4:
        return None
    lag, a = _yin(f, w, lag_lo, lag_hi)
    return rate / lag if a <= max_aperiodic else None


def pitch_near(pcm16: bytes, rate: int, start: int, period: int, periods: int = 2) -> tuple[float | None, float, float]:
    """start 標本から periods 周期の区間（周期を探す長さ＝period の 1.4倍ぶん長い）の (高さ Hz、周期のずれ、大きさ dB)。
    録音を継ぐ所の、つなぎ目に接する数周期だけを読む（g69_vowel_seam）。周期は period の 0.7〜1.4倍の中だけで探す（1オクターブの
    取り違えをしない）。周期のずれが APERIODIC_MAX を超える（波がそろわない）なら、高さは None。
    ⛔ 2026-10-28：つなぎ目の高さを pitch_at（FRAME_S＋周期を探す長さ＝約44ミリ秒）で読んでいた＝声の高さが動いている所では、つなぎ目の
       高さにならない（G-69 の3回目の S2 の「の」：名前の部分の声は 44ミリ秒の間に 216Hz から 198Hz へ下がっていて、区間の頭の高さを読み
       「高さの差 0.0」と数えた。2周期で読むと 1.22半音＝ユーザー ✕「重なって、音量も違和感」）。"""
    lo, hi = int(period * 0.7), int(period * 1.4)
    w = periods * period
    seg = pcm16[max(0, start) * 2:(start + w + hi) * 2]
    x = np.frombuffer(seg, dtype="<i2").astype(np.float64) / 32768.0
    if start < 0 or len(x) < w + hi or not x.std():
        return None, 1.0, -999.0
    lag, a = _yin(x, w, lo, hi)
    return (rate / lag if a <= APERIODIC_MAX else None), a, loudness_db(seg)


def loudness_db(pcm16: bytes) -> float:
    """声の大きさ（全体の2乗平均の dB。もとの音の数のまま＝比べるのは、同じ録音の仕組みで作った録音どうし）。"""
    x = np.frombuffer(pcm16, dtype="<i2").astype(np.float64)
    return float(20 * np.log10(np.sqrt((x ** 2).mean()) + 1e-9)) if len(x) else -999.0


def semitones(a_hz: float, b_hz: float) -> float:
    """a が b より何半音高いか。"""
    return 12 * math.log2(a_hz / b_hz)


def read_wav(path) -> tuple[bytes, int]:
    import wave
    with wave.open(str(path)) as w:
        assert w.getsampwidth() == 2 and w.getnchannels() == 1, path
        return w.readframes(w.getnframes()), w.getframerate()
