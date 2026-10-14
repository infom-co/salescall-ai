"""間を聞いて確かめるための声を作る（DESIGN.md 6-15。ユーザーが聞いて、間に違和感がないかを決める）。

相手役の声（声のあとの無音は切る）→ 間（無音）→ AIの声、を1つの声のファイルにつなぐ。間は、試験で測った値（声の終わりから AIの声の
始まりまで）か、指定した値。試験の数字を変えるものではなく、聞き比べのためだけに使う。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from barge import voice_runs, voiced
from common import read_wav_pcm16_mono, write_wav
from convo import AUDIO_DIR

CALLEE_DIR = AUDIO_DIR.parent / "callee"


def _to_rate(pcm16: bytes, rate: int, target: int) -> np.ndarray:
    x = np.frombuffer(pcm16, dtype=np.int16).astype(np.float32)
    if rate == target:
        return x
    n = int(len(x) * target / rate)
    return np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x)


def callee_speech(wav: str) -> tuple[bytes, int, float]:
    """相手役の声と、声のあとの無音の長さ（秒）。"""
    pcm, rate = read_wav_pcm16_mono(CALLEE_DIR / wav)
    runs = voice_runs(voiced(pcm, rate), 0.15)
    end = runs[-1][1] if runs else len(pcm) / 2 / rate
    return pcm, rate, len(pcm) / 2 / rate - end


def stitch(callee_wav: str, ai_file: str, gap_s: float, out: Path) -> None:
    pcm, rate, tail = callee_speech(callee_wav)
    ai_pcm, ai_rate = read_wav_pcm16_mono(AUDIO_DIR / ai_file)
    a = _to_rate(pcm, rate, ai_rate)
    a = a[: max(0, len(a) - int(tail * ai_rate))]          # 声のあとの無音を切る
    gap = np.zeros(int(max(0.0, gap_s) * ai_rate), dtype=np.float32)
    lead = np.zeros(int(0.3 * ai_rate), dtype=np.float32)
    b = np.frombuffer(ai_pcm, dtype=np.int16).astype(np.float32)
    y = np.concatenate([lead, a, gap, b]).clip(-32768, 32767).astype(np.int16)
    write_wav(out, y.tobytes(), ai_rate)


def measured_gap(turn: dict) -> float | None:
    """試験で測った間（声の終わりから AIの声の始まりまで）。"""
    c = turn.get("callee")
    if not isinstance(c, dict) or turn.get("first_audio_after_s") is None:
        return None
    return turn["first_audio_after_s"] + callee_speech(c["wav"])[2]


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("result")
    p.add_argument("--row", type=int, required=True)
    p.add_argument("--turn", type=int, required=True)
    p.add_argument("--gaps", type=float, nargs="*", default=[], help="測った間のほかに作る間（秒）")
    p.add_argument("--out", required=True)
    a = p.parse_args()
    d = json.loads(Path(a.result).read_text())
    t = d["rows"][a.row]["turns"][a.turn]
    m = measured_gap(t)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    for g, label in [(m, "測った間")] + [(g, "指定") for g in a.gaps]:
        name = out / f"間{g:.1f}秒（{label}）_{t['callee']['wav'].removesuffix('.wav')}.wav"
        stitch(t["callee"]["wav"], t["audio_file"], g, name)
        print(name.name, "| AI:", (t.get("intended") or "")[:40])


if __name__ == "__main__":
    main()
