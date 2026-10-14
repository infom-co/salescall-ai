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
from convo import AUDIO_DIR, CUT_IN_S

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


def join_with_pause(first_wav: str, pause_s: float, second_wav: str, out_wav: str) -> None:
    """相手役の声を2つ、間をあけてつなぐ（言葉の途中で間をあける相手。G-30・G-31）。1つ目の声のあとの無音は切ってから、pause_s をあける。
    どちらも、別の耳の確かめに通った声だけを使う。"""
    from make_callee_samples import verified
    for w in (first_wav, second_wav):
        assert verified(w), f"{w} は確かめに通っていない"
    a, ra, ta = callee_speech(first_wav)
    b, rb, _ = callee_speech(second_wav)
    assert ra == rb, "2つの声の速さ（標本の数）が違う"
    xa = np.frombuffer(a, dtype=np.int16)
    xa = xa[: len(xa) - int(ta * ra)]
    gap = np.zeros(int(pause_s * ra), dtype=np.int16)
    write_wav(CALLEE_DIR / out_wav, np.concatenate([xa, gap, np.frombuffer(b, dtype=np.int16)]).tobytes(), ra)


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


def _speech_only(wav_path: Path) -> tuple[np.ndarray, int]:
    """声の前後の無音を切った声。"""
    pcm, rate = read_wav_pcm16_mono(wav_path)
    runs = voice_runs(voiced(pcm, rate), 0.15)
    x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    if runs:
        x = x[int(runs[0][0] * rate): int(runs[-1][1] * rate)]
    return x, rate


def stitch_with_ack(callee_wav: str, ai_file: str, ack_wav: Path, ack_at_s: float, ai_at_s: float, out: Path) -> float:
    """相手の声 → ack_at_s 秒（声の終わりから）で短い相づちの録音 → AIの声、をつなぐ（DESIGN.md 6-15 の案）。
    AIの声は ai_at_s 秒（声の終わりから。試験で測った値）で始まる。相づちと重なるときは、相づちが終わるまで待たせる（プログラムが流す順を
    決める形）。返り値＝AIの声が実際に始まった時刻（声の終わりから）。"""
    pcm, rate, tail = callee_speech(callee_wav)
    ai_pcm, ai_rate = read_wav_pcm16_mono(AUDIO_DIR / ai_file)
    a = _to_rate(pcm, rate, ai_rate)
    a = a[: max(0, len(a) - int(tail * ai_rate))]
    ack, ack_rate = _speech_only(ack_wav)
    ack = _to_rate(ack.astype(np.int16).tobytes(), ack_rate, ai_rate)
    ack_len = len(ack) / ai_rate
    ai_start = max(ai_at_s, ack_at_s + ack_len + 0.05)
    lead = np.zeros(int(0.3 * ai_rate), dtype=np.float32)
    gap1 = np.zeros(int(ack_at_s * ai_rate), dtype=np.float32)
    gap2 = np.zeros(int((ai_start - ack_at_s - ack_len) * ai_rate), dtype=np.float32)
    b = np.frombuffer(ai_pcm, dtype=np.int16).astype(np.float32)
    y = np.concatenate([lead, a, gap1, ack, gap2, b]).clip(-32768, 32767).astype(np.int16)
    write_wav(out, y.tobytes(), ai_rate)
    return ai_start


def played_ack(turn: dict) -> dict | None:
    """そのターンで、電話接続側が相づちを流したか（流したなら、どの相づちを、報告が届いた時刻＝声の終わりから何秒で）。
    流すのは、受け付けた最初の報告のときだけで、AIの声がまだ始まっていないときだけ（話している最中に重ねない）。"""
    c = turn.get("callee")
    if not isinstance(c, dict) or "wav" not in c:
        return None
    first = next((e for e in turn.get("tool_events") or [] if e.get("ack")), None)
    if first is None or first.get("at_s") is None:
        return None
    ai_at = turn.get("first_audio_after_s")
    if ai_at is not None and ai_at <= first["at_s"]:
        return None
    return {"ack": first["ack"], "at_s": first["at_s"] + callee_speech(c["wav"])[2]}


def ack_wav(voice_type: str, ack: str) -> Path:
    return AUDIO_DIR.parent / "ack" / f"{voice_type}_{ack}.wav"


def stitch_call(call: dict, voice_type: str, out: Path, reply_gap_s: float = 0.6, join_s: float = 0.05) -> list[dict] | None:
    """通話1本を、相手の声 → (相づち) → AIの声 の順に、試験で測った時刻でつなぐ（会話全体を聞いて、「はい」が多すぎないか・
    相づちの選び方が場面に合うかを、ユーザーが確かめるため）。相手が次に話し始めるまでの間は reply_gap_s（作った値）。
    相手の言葉の途中でAIが話し出したターン（割り込み）や、待っている間にAIが話したターンは、本当の時刻が分からないので、
    その通話は作らない（None）。AIが黙って待ったターン（声も報告も無い）は、相手の声だけを置く。
    返り値＝ターンごとの、相手に聞こえた最初の言葉と時刻。"""
    rate = 24000
    parts, plan = [np.zeros(int(0.3 * rate), dtype=np.float32)], []
    for t in call["turns"]:
        c = t.get("callee")
        if isinstance(c, dict) and "followup_of" in c:
            if t.get("audio_file"):
                return None
            continue  # 続きを待ったが、AIは話さなかった
        if isinstance(c, dict) and "silence_s" in c:
            if t.get("audio_file"):
                return None  # 黙っている間にAIが話した：時刻が分からない
            parts.append(np.zeros(int(c["silence_s"] * rate), dtype=np.float32))
            continue
        if not (isinstance(c, dict) and "wav" in c):
            return None
        pcm, prate, tail = callee_speech(c["wav"])
        a = _to_rate(pcm, prate, rate)
        from flowctl import SILENT_OPS
        if t.get("first_audio_after_s") is None and not t.get("audio_file") \
                and all(e.get("name") in SILENT_OPS for e in t.get("tool_events") or []):
            parts.append(a[: max(0, len(a) - int(tail * rate))])  # AIは黙って待った（相手が考えていることの報告だけ＝形 B）
            ack = played_ack(t)
            if ack is not None:  # 相づちの録音（形 B）を、報告が届いた時刻に置く
                x, r = _speech_only(ack_wav(voice_type, ack["ack"]))
                x = _to_rate(x.astype(np.int16).tobytes(), r, rate)
                parts += [np.zeros(int(ack["at_s"] * rate), dtype=np.float32), x]
                plan.append({"heard_first": ack["ack"], "first_s": round(ack["at_s"], 2), "ai_s": None, "ai_held_s": 0.0})
            continue
        if not t.get("audio_file") or t.get("first_audio_after_s") is None or t["first_audio_after_s"] < CUT_IN_S:
            return None
        parts.append(a[: max(0, len(a) - int(tail * rate))])
        ai_pcm, ai_rate = read_wav_pcm16_mono(AUDIO_DIR / t["audio_file"])
        assert ai_rate == rate
        ai_at = t["first_audio_after_s"] + tail
        ack = played_ack(t)
        if ack is not None:
            x, r = _speech_only(ack_wav(voice_type, ack["ack"]))
            x = _to_rate(x.astype(np.int16).tobytes(), r, rate)
            start = max(ai_at, ack["at_s"] + len(x) / rate + join_s)
            parts += [np.zeros(int(ack["at_s"] * rate), dtype=np.float32), x,
                      np.zeros(int((start - ack["at_s"] - len(x) / rate) * rate), dtype=np.float32)]
            plan.append({"heard_first": ack["ack"], "first_s": round(ack["at_s"], 2), "ai_s": round(start, 2),
                         "ai_held_s": round(start - ai_at, 2)})
        else:
            parts.append(np.zeros(int(ai_at * rate), dtype=np.float32))
            plan.append({"heard_first": (t.get("intended") or "")[:8], "first_s": round(ai_at, 2), "ai_s": round(ai_at, 2),
                         "ai_held_s": 0.0})
        parts += [np.frombuffer(ai_pcm, dtype=np.int16).astype(np.float32), np.zeros(int(reply_gap_s * rate), dtype=np.float32)]
    write_wav(out, np.concatenate(parts).clip(-32768, 32767).astype(np.int16).tobytes(), rate)
    return plan


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
