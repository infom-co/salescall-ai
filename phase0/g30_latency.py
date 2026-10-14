"""G-30：会話の間（相手が話し終えてから、AIの声が出始めるまで）を縮める手立てを比べる（DESIGN.md 6-15。2026-10-14 ユーザー指示）。

ユーザー：「5秒とかは問題外。0.5秒くらいに改善できないのかを検討した方がいい」。いまの間は、答えを報告してから話したターンで中央 2.15秒
（声で送った G-22・G-29）。内訳は、話し終わりの判定 約0.65秒＋AIが考えて報告を決める 約0.65秒＋報告の返事のあと話し始める 約0.85秒。
比べる形（同じ台本・同じ回数。形の順は1本ずつ回す）：
- A_いまの形：考える量は自動、話し終わりの判定も既定
- B_考える量0
- C_考える量0＋判定0.4秒：話し終わりの感度を高く、無音を待つ長さ 400ミリ秒
- D_考える量0＋判定0.2秒：同じく 200ミリ秒
見ること：
- 間（相手の声の終わりから、AIの声の始まりまで。相手役の声のあとの無音を足して、声の終わりから数える）
- 割り込み：「えーとですね、」のあと 0.9秒あけて「3人です。」と言う相手に、AIが言い終わる前に話し出したか
- 正しさ：答えを報告しなかったターン、項目が正しく取れたか（考える量0や速い判定で、報告の正しさが落ちないか）
⚠️ 作業環境の中の値（電話の経路の遅れは入っていない）。声は全部残す（間をつないだ声を、ユーザーに聞いていただく）。
"""
from __future__ import annotations

import asyncio
import json
import statistics as st

import numpy as np

from common import base_parser, describe_error, drop_partial, make_client, pick_key, read_wav_pcm16_mono, require_python, save_partial
from common import save_result, say, write_wav
from convo import AUDIO_DIR, run_call
from g22_opening import BUSINESS, W
from instruction import VOICE_TYPES
from make_callee_samples import verified
from stitch_gap import callee_speech

CALLEE_DIR = AUDIO_DIR.parent / "callee"
PAUSE_S = 0.9
VARIANTS = {
    "A_いまの形": {},
    "B_考える量0": {"thinking_budget": 0},
    "C_考える量0＋判定0.4秒": {"thinking_budget": 0, "speech_end": {"sensitivity": "HIGH", "silence_ms": 400}},
    "D_考える量0＋判定0.2秒": {"thinking_budget": 0, "speech_end": {"sensitivity": "HIGH", "silence_ms": 200}},
}
PAUSE_WAV = "Puck_pause_three.wav"  # 「えーとですね、」＋無音＋「3人です。」（確かめに通った2つの声をつないだもの）
CALLEE = [W("yes_reserved"), W("datetime_pm"), W("affirm"), {"wav": PAUSE_WAV}, W("affirm")]
ANSWERS = ("Puck_datetime_pm.wav", PAUSE_WAV)  # 値を答える発話（報告を飛ばしたかを数える）


def make_pause_clip() -> None:
    """「えーとですね、」＋ PAUSE_S 秒の無音＋「3人です。」。どちらも別の耳の確かめに通った声だけを使う。"""
    for w in ("Puck_filler_eto.wav", "Puck_three_people.wav"):
        assert verified(w), f"{w} は確かめに通っていない"
    a, ra, ta = callee_speech("Puck_filler_eto.wav")
    b, rb, _ = callee_speech("Puck_three_people.wav")
    assert ra == rb, "2つの声の速さ（標本の数）が違う"
    xa = np.frombuffer(a, dtype=np.int16)[: -int(ta * ra) or None]  # 「えーとですね、」の声のあとの無音を切る
    gap = np.zeros(int(PAUSE_S * ra), dtype=np.int16)
    write_wav(CALLEE_DIR / PAUSE_WAV, np.concatenate([xa, gap, np.frombuffer(b, dtype=np.int16)]).tobytes(), ra)


def turn_measures(call: dict) -> list[dict]:
    out = []
    for t in call["turns"]:
        c = t.get("callee")
        if not isinstance(c, dict) or t.get("first_audio_after_s") is None:
            continue
        tail = callee_speech(c["wav"])[2]
        tools = [e["at_s"] for e in (t.get("tool_events") or []) if e.get("at_s") is not None]
        tool_first = bool(tools) and min(tools) < t["first_audio_after_s"]
        out.append({"index": t["index"], "callee": c["wav"], "gap_s": round(t["first_audio_after_s"] + tail, 3),
                    "tool_first": tool_first,
                    "to_tool_s": round(min(tools) + tail, 3) if tool_first else None,
                    "tool_to_voice_s": round(t["first_audio_after_s"] - min(tools), 3) if tool_first else None,
                    # 割り込み：相手の声を送り終える前に、AIの声ができていた（受け取りを始めた直後に、もう声があった）
                    "cut_in": t["first_audio_after_s"] < 0.05,
                    "reported": any(e.get("name") == "record_answer" for e in (t.get("tool_events") or [])),
                    "said": (t.get("intended") or "")[:60]})
    return out


def judge(call: dict, turns: list[dict]) -> dict:
    it = call["items"]
    ok = (it["Q1"]["condition"] == "はい" and it["Q2"]["value"] == "15日の午後2時" and it["Q3"]["value"] == "3人"
          and all(it[q]["state"] in ("confirmed", "accepted") for q in ("Q1", "Q2", "Q3")))
    answer_turns = [t for t in turns if t["callee"] in ANSWERS]
    return {"items_ok": ok, "skipped": sum(1 for t in answer_turns if not t["reported"]),
            "pause_cut_in": any(t["cut_in"] for t in turns if t["callee"] == PAUSE_WAV)}


def summarize(rows: list[dict]) -> dict:
    s = {}
    for name in VARIANTS:
        rs = [r for r in rows if r.get("variant") == name and "checks" in r]
        ts = [t for r in rs for t in r["turns_measured"] if not t["cut_in"]]

        def q(xs):
            xs = sorted(xs)
            return {"n": len(xs), "median": round(st.median(xs), 2), "p90": round(xs[int(len(xs) * 0.9)], 2), "max": round(xs[-1], 2)} if xs else None
        s[name] = {"calls": len(rs), "items_ok": sum(r["checks"]["items_ok"] for r in rs),
                   "skipped": sum(r["checks"]["skipped"] for r in rs), "pause_cut_in": sum(r["checks"]["pause_cut_in"] for r in rs),
                   "gap_tool": q([t["gap_s"] for t in ts if t["tool_first"]]),
                   "gap_no_tool": q([t["gap_s"] for t in ts if not t["tool_first"]]),
                   "to_tool": q([t["to_tool_s"] for t in ts if t["tool_first"]]),
                   "tool_to_voice": q([t["tool_to_voice_s"] for t in ts if t["tool_first"]])}
    return s


async def run(args) -> dict:
    make_pause_clip()
    if args.dry_run:
        from instruction import build
        from live_util import build_config
        from flowctl import tool_declarations
        for kw in VARIANTS.values():
            v, text = build(BUSINESS, "S2", opening_mode="recorded")
            build_config(voice=v, system_instruction=text, tools=tool_declarations(BUSINESS.questions),
                         thinking_budget=kw.get("thinking_budget"), speech_end=kw.get("speech_end"))
        pcm, rate = read_wav_pcm16_mono(CALLEE_DIR / PAUSE_WAV)
        return {"dry_run": True, "variants": list(VARIANTS), "pause_clip_s": round(len(pcm) / 2 / rate, 2)}
    client = make_client(pick_key(args))
    rows = []
    for rep in range(args.repeat):
        for vt in args.type or VOICE_TYPES:
            for name, kw in VARIANTS.items():
                try:
                    call = await run_call(client, args.model, BUSINESS, vt, CALLEE, opening_mode="recorded", use_ear=False,
                                          save_audio="all", audio_label=f"G30_{name}_{rep}", **kw)
                    tm = turn_measures(call)
                    row = {"rep": rep, "type": vt, "variant": name, "turns_measured": tm, "checks": judge(call, tm), **call}
                except Exception as exc:
                    row = {"rep": rep, "type": vt, "variant": name, "error": describe_error(exc)}
                rows.append(row)
                c, tm = row.get("checks", {}), row.get("turns_measured", [])
                say(f"[{rep}] {vt} {name} 項目={c.get('items_ok')} 飛ばし={c.get('skipped')} 割り込み={c.get('pause_cut_in')} "
                    f"間={[t['gap_s'] for t in tm]} {row.get('error', '')}")
                save_partial("G-30", {"model": args.model, "summary": summarize(rows), "rows": rows})
                await asyncio.sleep(args.pause)
    summary = summarize(rows)
    say("まとめ：", json.dumps(summary, ensure_ascii=False))
    return {"model": args.model, "variants": VARIANTS, "pause_s": PAUSE_S, "summary": summary, "rows": rows}


def main() -> None:
    require_python()
    p = base_parser(__doc__)
    p.add_argument("--repeat", type=int, default=2)
    p.add_argument("--pause", type=float, default=3.0)
    p.add_argument("--type", choices=sorted(VOICE_TYPES), action="append")
    args = p.parse_args()
    save_result("G-30", asyncio.run(run(args)))
    drop_partial("G-30")


if __name__ == "__main__":
    main()
