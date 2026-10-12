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


def speed_up(pcm16: bytes, rate: int, factor: float, frame_ms: float = 30.0, search_ms: float = 10.0) -> bytes:
    """声の高さを変えずに、話す速さだけを factor 倍にする（WSOLA という方式）。

    2026-10-12 ユーザーの評価「話すスピードが遅すぎる ⇒ 2倍速で自然に」への手当て。
    話し方の指示（プロンプト）だけでは 1.04〜1.36倍にしかならず、言葉を勝手に言い換えることもあった（G-3）。
    ⇒ 速さは、AIの声を相手に流す前の、この音の変換の1か所で決める（言葉は1文字も変わらない）。
    """
    if factor == 1.0:
        return pcm16
    x = _to_float(pcm16)
    n = int(rate * frame_ms / 1000)
    n += n % 2
    hop_out = n // 2
    hop_in = int(round(hop_out * factor))
    tol = int(rate * search_ms / 1000)
    win = np.hanning(n).astype(np.float32)
    out_len = int(len(x) / factor) + n
    y = np.zeros(out_len, dtype=np.float32)
    norm = np.zeros(out_len, dtype=np.float32)
    pos_in, pos_out = 0, 0
    prev_tail = None
    while pos_in + n + tol < len(x) and pos_out + n < out_len:
        if prev_tail is None:
            best = pos_in
        else:
            # 前の区間の続き（prev_tail）と最もよく重なる位置を、±tol の範囲で探す
            lo, hi = max(0, pos_in - tol), min(len(x) - n, pos_in + tol)
            seg = x[lo:hi + hop_out]
            if len(seg) < hop_out:
                break
            corr = np.correlate(seg, prev_tail, mode="valid")
            best = lo + int(np.argmax(corr))
        frame = x[best:best + n]
        if len(frame) < n:
            break
        y[pos_out:pos_out + n] += frame * win
        norm[pos_out:pos_out + n] += win
        prev_tail = x[best + hop_out:best + hop_out + hop_out]
        if len(prev_tail) < hop_out:
            break
        pos_in += hop_in
        pos_out += hop_out
    norm[norm < 1e-6] = 1.0
    y = (y / norm)[:pos_out + hop_out]
    return _to_pcm16(y)
