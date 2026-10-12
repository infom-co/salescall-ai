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
    return 0.0 if len(idx) == 0 else (idx[-1] - idx[0] + 1) / rate
