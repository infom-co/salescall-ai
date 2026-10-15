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
# 相づちの録音が終わってから、AIの声を流すまでの間（電話接続側が守る順。DESIGN.md 6-15）。
# ✅ 2026-10-14 ユーザー（G-36 を聞いて）：「『ありがとうございます。』と『続きまして』の間が短すぎる。0.6秒くらいは欲しい。『承知致しました。』と
#    『それでは』の間も同様」⇒ 0.05秒から 0.6秒にした
ACK_TO_AI_S = 0.6
# 声の終わりのあとに残す、弱い語尾の長さの上限と、その音量の下限（20·log10(実効値)）。
# ⛔ 2026-10-14 ユーザー（G-36 を聞いて）：「お客役の Sadachbia は会話の途中が何か所か途切れている」⇒ 声の終わりを見つける決まり（voice_runs）が、
#    そのあと 0.1〜0.2秒続く弱い語尾（「です」の「す」など。40〜49）を声に数えず、つなぐときに切っていた。聞き比べの声と、間をあけた相手の声
#    （join_with_pause）の作りだけの誤り（試験で測った間は、どれも声の終わりから数えているので変わらない）
TAIL_KEEP_MAX_S = 0.5
# 録音どうしをつなぐ間（つなぎの言葉の録音 → 質問の文の録音。G-37）。⏳ ユーザーに聞いていただいて決める
RECORDING_JOIN_S = 0.1  # ⏳ 2026-10-15 G-37 の1回目は 0.2秒（ユーザー：「それでは、ご希望の…」が特に不自然）
TAIL_KEEP_DB = 25.0


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


def kept_tail(pcm: bytes, rate: int, tail: float) -> float:
    """声の終わりのあとに残す長さ（秒）：弱い語尾が続く間（50ミリ秒ごとの音量が TAIL_KEEP_DB 以上の間。TAIL_KEEP_MAX_S まで）。"""
    x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    start, frame = len(x) - int(tail * rate), int(0.05 * rate)
    kept = 0
    while kept < int(TAIL_KEEP_MAX_S * rate) and start + kept + frame <= len(x):
        seg = x[start + kept: start + kept + frame]
        if 20 * np.log10(np.sqrt(np.mean(seg ** 2)) + 1e-9) < TAIL_KEEP_DB:
            break
        kept += frame
    return kept / rate


def callee_audio(wav: str, rate_out: int) -> tuple[np.ndarray, float]:
    """つなぐための相手役の声（rate_out の速さ。声のあとの無音は切り、弱い語尾は残す）と、声の終わりのあとに残した長さ（秒）。
    あとに続ける間は、声の終わりから数えた時刻から、この長さを引く。"""
    pcm, rate, tail = callee_speech(wav)
    keep = kept_tail(pcm, rate, tail)
    a = _to_rate(pcm, rate, rate_out)
    return a[: max(0, len(a) - int((tail - keep) * rate_out))], keep


def join_with_pause(first_wav: str, pause_s: float, second_wav: str, out_wav: str) -> None:
    """相手役の声を2つ、間をあけてつなぐ（言葉の途中で間をあける相手。G-30・G-31）。1つ目の声のあとの無音は切ってから、pause_s をあける。
    どちらも、別の耳の確かめに通った声だけを使う。"""
    from make_callee_samples import verified
    for w in (first_wav, second_wav):
        assert verified(w), f"{w} は確かめに通っていない"
    a, ra, ta = callee_speech(first_wav)
    b, rb, _ = callee_speech(second_wav)
    assert ra == rb, "2つの声の速さ（標本の数）が違う"
    keep = kept_tail(a, ra, ta)
    xa = np.frombuffer(a, dtype=np.int16)
    xa = xa[: len(xa) - int((ta - keep) * ra)]
    gap = np.zeros(int(max(0.0, pause_s - keep) * ra), dtype=np.int16)  # 間は声の終わりから数える
    write_wav(CALLEE_DIR / out_wav, np.concatenate([xa, gap, np.frombuffer(b, dtype=np.int16)]).tobytes(), ra)


def stitch(callee_wav: str, ai_file: str, gap_s: float, out: Path) -> None:
    ai_pcm, ai_rate = read_wav_pcm16_mono(AUDIO_DIR / ai_file)
    a, keep = callee_audio(callee_wav, ai_rate)            # 声のあとの無音を切る（弱い語尾は残す）
    gap = np.zeros(int(max(0.0, gap_s - keep) * ai_rate), dtype=np.float32)
    lead = np.zeros(int(0.3 * ai_rate), dtype=np.float32)
    b = np.frombuffer(ai_pcm, dtype=np.int16).astype(np.float32)
    y = np.concatenate([lead, a, gap, b]).clip(-32768, 32767).astype(np.int16)
    write_wav(out, y.tobytes(), ai_rate)


def _speech_only(wav_path: Path) -> tuple[np.ndarray, int]:
    """声の前後の無音を切った声（終わりの弱い語尾は残す＝kept_tail）。"""
    pcm, rate = read_wav_pcm16_mono(wav_path)
    runs = voice_runs(voiced(pcm, rate), 0.15)
    x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    if runs:
        tail = len(x) / rate - runs[-1][1]
        x = x[int(runs[0][0] * rate): int((runs[-1][1] + kept_tail(pcm, rate, tail)) * rate)]
    return x, rate


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


def played_wav(voice_type: str, item: dict) -> Path:
    """電話接続側が流す録音のファイル（流れの制御の返事の play の1つ）。締めは契約者の単位（results/ack）、質問（つなぎの言葉と1本）は
    依頼主の単位（results/script）。DESIGN.md 6-13。kind "lead"（つなぎの言葉だけの録音）は、G-37 の1回目・2回目の記録を聞き直すときだけ"""
    from phrases import LEAD_RECORDED
    if item["kind"] == "lead":
        return ack_wav(voice_type, LEAD_RECORDED[item["text"]])
    if item["kind"] == "closing":
        return ack_wav(voice_type, "closing")
    name = f"{item['id']}_{LEAD_RECORDED[item['lead']]}" if item.get("lead") else item["id"]
    return AUDIO_DIR.parent / "script" / f"{voice_type}_{name}.wav"


def _rec(path: Path, rate: int) -> np.ndarray:
    x, r = _speech_only(path)
    return _to_rate(x.astype(np.int16).tobytes(), r, rate)


def _lead_sounds(voice_type: str, callee_voice: str, rate: int, ack: dict | None, main_at: float, keep: float, join_s: float,
                 filler_s: float | None) -> tuple[list[tuple[float, np.ndarray, str]], float]:
    """相手の声のあと、主の声（AIの声・流す録音）より前に、電話接続側が流す声と、主の声を始めてよい時刻（声の終わりから）。
    filler_s（つなぎの一言の案。DESIGN.md 6-15。⏳ ユーザーが聞いて決める）：相手が話し終えてから filler_s 秒たっても何も流れていなければ
    「はい、」（そのあとの相づちが「はい、」なら重ねない）。相づちのあと filler_s 秒たっても主の声が始まらなければ「少々お待ちいただいても
    よろしいでしょうか。」を流し、相手の「はい。」のあとに主の声を始める。None なら、相づちだけ（これまでどおり）。"""
    out, now = [], keep

    def put(at: float, x: np.ndarray, what: str) -> None:
        nonlocal now
        at = max(at, now)
        out.append((at, x, what))
        now = at + len(x) / rate

    filled = filler_s is not None and (ack is None or ack["at_s"] > filler_s) and main_at > filler_s
    if filled:
        put(filler_s, _rec(ack_wav(voice_type, "hai"), rate), "つなぎ：はい、")
    if ack is not None and not (filled and ack["ack"] == "hai"):
        put(ack["at_s"] if not filled else max(ack["at_s"], now + 0.1), _rec(ack_wav(voice_type, ack["ack"]), rate), ack["ack"])
    if not out:
        return out, main_at
    if filler_s is not None and main_at > now + filler_s:
        put(now + filler_s, _rec(ack_wav(voice_type, "wait"), rate), "つなぎ：少々お待ちいただいてもよろしいでしょうか。")
        x, _ = callee_audio(f"{callee_voice}_backchannel.wav", rate)
        put(now + 0.4, x, "相手：はい。")  # 問いかけの一言なので、相手は「はい」と答える（6-15：この「はい」は会話AIに送らない）
    return out, max(main_at, now + join_s)


def _place(parts: list, rate: int, keep: float, sounds: list) -> float:
    """声の終わりから数えた時刻で、声を並べる。返り値＝並べ終わった時刻。"""
    now = keep
    for at, x, _ in sounds:
        parts += [np.zeros(int(max(0.0, at - now) * rate), dtype=np.float32), x]
        now = max(at, now) + len(x) / rate
    return now


def stitch_call(call: dict, voice_type: str, out: Path, reply_gap_s: float = 0.6, join_s: float = ACK_TO_AI_S,
                filler_s: float | None = None) -> list[dict] | None:
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
        tail = callee_speech(c["wav"])[2]
        a, keep = callee_audio(c["wav"], rate)  # 時刻はどれも声の終わりから数える。残した語尾の長さ（keep）だけ、あとの間を縮める
        if not t.get("audio_file") and t.get("first_audio_after_s") is None and not t.get("tool_events"):
            parts.append(a)  # AIは黙って待った（声も報告も無い）：相手の声だけ
            continue
        callee_voice = c["wav"].split("_", 1)[0]
        play = next((e for e in t.get("tool_events") or [] if e.get("play")), None)
        if play is not None:
            # 電話接続側が録音を流したターン（次の質問・締め。G-37）：相づち → 0.6秒 → 録音（つなぎの言葉 → 質問の文）。
            # AIは話さない決まり。話していても、電話接続側はその声を流さない（plan に印を残す）
            parts.append(a)
            ack = played_ack(t)
            sounds, start = _lead_sounds(voice_type, callee_voice, rate, ack, max(play["at_s"] + tail, keep), keep, join_s, filler_s)
            now = _place(parts, rate, keep, sounds)
            first_s = sounds[0][0] if sounds else start
            for item in play["play"]:
                x = _rec(played_wav(voice_type, item), rate)
                parts += [np.zeros(int(max(0.0, start - now) * rate), dtype=np.float32), x]
                now = max(start, now) + len(x) / rate
                start = now + RECORDING_JOIN_S
            parts.append(np.zeros(int(reply_gap_s * rate), dtype=np.float32))
            plan.append({"heard_first": sounds[0][2] if sounds else play["play"][0]["text"], "first_s": round(first_s, 2), "ai_s": None,
                         "ai_held_s": 0.0, "played": [i["text"] for i in play["play"]], "ai_spoke_on_play": bool(t.get("audio_file")),
                         "fillers": [w for _, _, w in sounds if w.startswith("つなぎ")]})
            continue
        if not t.get("audio_file") or t.get("first_audio_after_s") is None or t["first_audio_after_s"] < CUT_IN_S:
            return None
        parts.append(a)
        ai_pcm, ai_rate = read_wav_pcm16_mono(AUDIO_DIR / t["audio_file"])
        assert ai_rate == rate
        ai_at = t["first_audio_after_s"] + tail
        ack = played_ack(t)
        sounds, start = _lead_sounds(voice_type, callee_voice, rate, ack, ai_at, keep, join_s, filler_s)
        start = max(start, keep)
        now = _place(parts, rate, keep, sounds)
        parts.append(np.zeros(int(max(0.0, start - now) * rate), dtype=np.float32))
        plan.append({"heard_first": sounds[0][2] if sounds else (t.get("intended") or "")[:8],
                     "first_s": round(sounds[0][0] if sounds else start, 2), "ai_s": round(start, 2), "ai_held_s": round(start - ai_at, 2),
                     "fillers": [w for _, _, w in sounds if w.startswith("つなぎ")]})
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
