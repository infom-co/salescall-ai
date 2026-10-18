"""音の変換（フェーズ0の確認で使う唯一の場所。DESIGN.md 8-3・6-10 の (1) の試作）。

- サンプリングレートの変換（窓付き sinc の低域通過フィルタ＋補間）
- 電話の音質にする（8kHz に落とす＝電話の帯域だけにする）
"""
from __future__ import annotations

import numpy as np


def _to_float(pcm16: bytes) -> np.ndarray:
    return np.frombuffer(pcm16, dtype="<i2").astype(np.float32) / 32768.0


def _to_pcm16(x: np.ndarray) -> bytes:
    return (np.clip(x, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()


def _lowpass(x: np.ndarray, cutoff_ratio: float, taps: int = 129) -> np.ndarray:
    """cutoff_ratio＝残す上限の周波数 ÷ 元のサンプリングレート（0〜0.5）。"""
    n = np.arange(taps) - (taps - 1) / 2
    h = 2 * cutoff_ratio * np.sinc(2 * cutoff_ratio * n) * np.hamming(taps)
    h /= h.sum()
    return np.convolve(x, h, mode="same")


def resample(pcm16: bytes, src_rate: int, dst_rate: int) -> bytes:
    if src_rate == dst_rate:
        return pcm16
    x = _to_float(pcm16)
    if dst_rate < src_rate:  # 下げるときは、先に高い音を落として折り返しを防ぐ
        x = _lowpass(x, 0.45 * dst_rate / src_rate)
    n_out = int(round(len(x) * dst_rate / src_rate))
    t_src = np.arange(len(x)) / src_rate
    t_dst = np.arange(n_out) / dst_rate
    return _to_pcm16(np.interp(t_dst, t_src, x))


def phone_quality(pcm16: bytes, rate: int, out_rate: int = 16000) -> bytes:
    """電話の音質にする：8kHz に落としてから、会話AIへ送る形（既定 16kHz）に戻す。"""
    narrow = resample(pcm16, rate, 8000)
    return resample(narrow, 8000, out_rate)


def duration_s(pcm16: bytes, rate: int) -> float:
    return len(pcm16) / 2 / rate



def voiced_seconds(pcm16: bytes, rate: int, threshold: float = 0.02) -> float:
    """前後の無音を除いた、声のある区間の長さ（秒）。話す速さを測るときに使う（前後の無音で速さが変わって見えないように）。"""
    x = np.abs(_to_float(pcm16))
    idx = np.flatnonzero(x > threshold)
    return 0.0 if len(idx) == 0 else float((idx[-1] - idx[0] + 1) / rate)  # 記録（JSON）に書けるよう、ふつうの数にする


# ファクスの音（ITU-T T.30）：かけた側の CNG（1100Hz）、受けた側の CED（2100Hz）と V.21 の信号（1650Hz・1850Hz）。人の声は、1つの高さの
# 音だけが0.5秒も続くことはないので、帯域の中の力のほとんどがこの高さに集まる区間の長さで見分ける（DESIGN.md 4-3 の優先順位の1＝音・回線の
# 信号で分かるものは、会話AIに聞かせる前にプログラムが決める）。
# ⛔ 2026-10-19 G-57：会話AIは、ファクスの答える音を聞いても3本とも何も報告せず、黙った（種別の報告に任せると、つなぎの一言を流し続ける）
FAX_TONES_HZ = (1100.0, 2100.0, 1650.0, 1850.0)
_TONE_FRAME_S = 0.04  # 40ミリ秒（25Hz きざみ。⛔ 20ミリ秒では50Hz きざみで、窓のにじみが隣のきざみへ逃げ、割合が半分ほどに見えた）
_TONE_SHARE = 0.7     # 1つの枠の、電話の帯域の力のうち、ファクスの高さ（±60Hz）に集まる割合
_TONE_MIN_S = 0.5     # その枠が続く長さ（CNG は0.5秒・CED は2.6〜4秒・V.21 の旗は1秒ほど）


def fax_tone_seconds(pcm16: bytes, rate: int) -> float:
    """声の中で、ファクスの音が切れずに続いた、いちばん長い区間の秒（無ければ0）。"""
    x = _to_float(pcm16)
    n = int(_TONE_FRAME_S * rate)
    if len(x) < n:
        return 0.0
    frames = x[: len(x) // n * n].reshape(-1, n) * np.hanning(n)
    spec = np.abs(np.fft.rfft(frames, axis=1)) ** 2
    freqs = np.fft.rfftfreq(n, 1 / rate)
    band = (freqs >= 300) & (freqs <= 3400)
    near = np.zeros_like(freqs, dtype=bool)
    for f in FAX_TONES_HZ:
        near |= np.abs(freqs - f) <= 60
    total = spec[:, band].sum(axis=1)
    share = np.where(total > 1e-6, spec[:, near & band].sum(axis=1) / np.maximum(total, 1e-12), 0.0)
    loud = total > 1e-3 * n  # 無音の枠は数えない
    best = run = 0
    for hit in (share >= _TONE_SHARE) & loud:
        run = run + 1 if hit else 0
        best = max(best, run)
    return best * _TONE_FRAME_S


def is_fax_tone(pcm16: bytes, rate: int) -> bool:
    return fax_tone_seconds(pcm16, rate) >= _TONE_MIN_S
