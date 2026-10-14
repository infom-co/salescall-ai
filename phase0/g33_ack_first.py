"""G-33：AIが、相手が話し終えたらまず受けの言葉を言い、返事を待たずに報告する形（受けの言葉が先。DESIGN.md 6-15 の案）で、
最初の声までの間が目安（0.7〜1.0秒。2026-10-14 ユーザー）に入るか。崩れ・報告の正しさ・受けの言葉の選び方は落ちないか。

ユーザー：「相手が相づち以外で何か言った際、AIが考える間に『はい』などの相づちで時間を稼いだらいい。ただし『はい』だらけにならない
ように。相手の話次第では『承知しました』『ありがとうございます』『そうですね』…などで対応するのも良い。判定を無理に早めすぎない」。
比べる形（同じ台本。考える量はどれも0）：
- C_待つ報告：G-30 の判定0.4秒の形（報告の返事を待ってから話す。プログラムが受けの言葉を付ける）
- N_受けの言葉が先・判定0.4秒：報告を「返事を待たない」宣言にし、AIが自分で受けの言葉を言う。言い直しの頭はつなぎの言葉
- N_受けの言葉が先・判定は既定：同じく、話し終わりの判定は既定（速めすぎない形で目安に入るか）
相手の台詞に、「えーとですね、」のあと 0.7秒あけて「3人です。」を入れる（考えている相手には「はい」と相づちを打つ決まり）。
見ること：
- 最初の声までの間（声の終わりから）／続き（言い直し・次の質問）の声までの間
- 受けの言葉の中身（場面に合うか・続けて同じでないか）
- 言い直しの値が別の耳で聞こえたか（2通りの聞き方のどちらか）
- 報告の正しさ（答えを報告しなかったターン、値が正しく取れたか）
"""
from __future__ import annotations

import asyncio
import json
import statistics as st

from common import (base_parser, describe_error, drop_partial, make_client, pick_key, read_wav_pcm16_mono, require_python,
                    save_partial, save_result, say)
from convo import AUDIO_DIR, CUT_IN_S, run_call
from flowctl import normalize_value
from g22_opening import BUSINESS, W
from g30_latency import VARIANTS as G30_VARIANTS
from g30_latency import keep_call
from instruction import VOICE_TYPES
from jtext import compare_reading
from listen import hear_feeds, in_any
from stitch_gap import callee_speech, join_with_pause

FAST = G30_VARIANTS["C_考える量0＋判定0.4秒"]
VARIANTS = {
    "C_待つ報告": {**FAST},
    "N_受けの言葉が先・判定0.4秒": {**FAST, "ack_first": True},
    "N_受けの言葉が先・判定は既定": {"thinking_budget": 0, "ack_first": True},
}
PAUSE_WAV = "Puck_pause700_three.wav"
ANSWERS = ("Puck_yes_reserved.wav", "Puck_datetime_pm.wav", "Puck_affirm.wav", PAUSE_WAV)
FOLLOWUP_WAIT_S = 10.0


def turn_parts(t: dict) -> dict:
    """1ターンの声を、最初の声（受けの言葉）と、報告の返事のあとの続きに分ける（声の始まりの時刻と、AIの文字）。"""
    ev = t.get("timeline") or []
    sent = [e["at_s"] for e in ev if e["kind"] == "tool_response_sent"]
    starts = [e for e in ev if e["kind"] == "audio_start"]
    cont = [e["at_s"] for e in starts if sent and e["at_s"] > sent[0] and e.get("chunk", 0) > 0]
    said = t.get("said_events") or []
    first_text = "".join(e["text"] for e in said if not cont or e["at_s"] < cont[0])
    return {"first_audio_s": t.get("first_audio_after_s"), "cont_audio_s": cont[0] if cont else None, "first_text": first_text}


def measures(call: dict) -> dict:
    turns = []
    for t in call["turns"]:
        c = t.get("callee")
        if not (isinstance(c, dict) and c.get("wav") in ANSWERS) or t.get("first_audio_after_s") is None:
            continue
        tail = callee_speech(c["wav"])[2]
        p = turn_parts(t)
        turns.append({"callee": c["wav"], "cut_in": t["first_audio_after_s"] < CUT_IN_S,
                      "gap_first_s": round(p["first_audio_s"] + tail, 3),
                      "gap_cont_s": round(p["cont_audio_s"] + tail, 3) if p["cont_audio_s"] is not None else None,
                      "first_text": p["first_text"][:30], "said": (t.get("intended") or "")[:60],
                      "reported": any(e.get("name") in ("record_answer", "confirm_field") for e in (t.get("tool_events") or []))})
    it = call["items"]
    ok = (it["Q1"]["condition"] == "はい" and it["Q2"]["value"] == "15日の午後2時" and it["Q3"]["value"] == "3人"
          and all(it[q]["state"] in ("confirmed", "accepted") for q in ("Q1", "Q2", "Q3")))
    value_turns = [t for t in turns if t["callee"] in ("Puck_datetime_pm.wav", PAUSE_WAV) and not t["cut_in"]]
    return {"turns_measured": turns,
            "checks": {"items_ok": ok, "skipped": sum(1 for t in value_turns if not t["reported"])}}


async def value_heard(client, call: dict) -> list[dict]:
    """言い直しの値が、別の耳で聞こえたか（2通りの聞き方のどちらか）。G-27 と同じ見方。"""
    out = []
    for t in call["turns"]:
        asked = [e for e in (t.get("tool_events") or []) if e.get("reply") == "confirm_required"]
        if not (asked and t.get("audio_file")):
            continue
        q = next(x for x in BUSINESS.questions if x.id == asked[-1]["args"].get("question_id"))
        value = normalize_value(q.type, asked[-1]["args"].get("value") or asked[-1]["args"].get("correction"))
        if not value:
            continue
        pcm, rate = read_wav_pcm16_mono(AUDIO_DIR / t["audio_file"])
        heard = await hear_feeds(client, pcm, rate)
        out.append({"value": value, "heard": heard, "heard_any": in_any(heard, lambda v: compare_reading(value) in compare_reading(v))})
    return out


def summarize(rows: list[dict]) -> dict:
    s = {}
    for name in VARIANTS:
        rs = [r for r in rows if r.get("variant") == name and "checks" in r]
        ts = [t for r in rs for t in r["turns_measured"] if not t["cut_in"] and t["callee"] != PAUSE_WAV]

        def q(xs):
            xs = sorted(x for x in xs if x is not None)
            return {"n": len(xs), "median": round(st.median(xs), 2), "p90": round(xs[int(len(xs) * 0.9)], 2)} if xs else None
        vh = [v for r in rs for v in r.get("value_heard", [])]
        firsts = [t["first_text"] for r in rs for t in r["turns_measured"] if not t["cut_in"]]
        s[name] = {"calls": len(rs), "items_ok": sum(r["checks"]["items_ok"] for r in rs), "skipped": sum(r["checks"]["skipped"] for r in rs),
                   "gap_first": q(t["gap_first_s"] for t in ts), "gap_cont": q(t["gap_cont_s"] for t in ts),
                   "value_missed_in_all": sum(1 for v in vh if not v["heard_any"]), "readbacks": len(vh),
                   "pause_cut_in": sum(1 for r in rs for t in r["turns_measured"] if t["callee"] == PAUSE_WAV and t["cut_in"]),
                   "first_words": sorted({f[:12] for f in firsts if f})}
    return s


async def run(args) -> dict:
    join_with_pause("Puck_filler_eto.wav", 0.7, "Puck_three_people.wav", PAUSE_WAV)
    if args.dry_run:
        from flowctl import tool_declarations
        from instruction import build
        from live_util import build_config
        for kw in VARIANTS.values():
            v, text = build(BUSINESS, "S2", opening_mode="recorded", ack_first=kw.get("ack_first", False))
            build_config(voice=v, system_instruction=text, tools=tool_declarations(BUSINESS.questions, non_blocking=kw.get("ack_first", False)),
                         thinking_budget=kw.get("thinking_budget"), speech_end=kw.get("speech_end"))
        return {"dry_run": True, "variants": list(VARIANTS)}
    client = make_client(pick_key(args))
    callee = [W("yes_reserved"), W("datetime_pm"), W("affirm"), {"wav": PAUSE_WAV}, W("affirm")]
    rows = []
    for rep in range(args.repeat):
        for vt in args.type or VOICE_TYPES:
            for name, kw in VARIANTS.items():
                try:
                    call = await run_call(client, args.model, BUSINESS, vt, callee, opening_mode="recorded", use_ear=False,
                                          save_audio="all", audio_label=f"G33_{name}_{rep}", followup_wait_s=FOLLOWUP_WAIT_S, **kw)
                except Exception as exc:
                    call, row = None, {"rep": rep, "type": vt, "variant": name, "error": describe_error(exc)}
                if call is not None:
                    row = keep_call({"rep": rep, "type": vt, "variant": name}, call, measures)
                    try:
                        row["value_heard"] = await value_heard(client, call)
                    except Exception as exc:
                        row["value_heard"], row["hear_error"] = [], describe_error(exc)
                rows.append(row)
                tm = row.get("turns_measured", [])
                say(f"[{rep}] {vt} {name} 項目={row.get('checks', {}).get('items_ok')} 最初の声={[t['gap_first_s'] for t in tm]} "
                    f"続き={[t['gap_cont_s'] for t in tm]} 受け={[t['first_text'][:10] for t in tm]} {row.get('error', '')}")
                save_partial("G-33", {"model": args.model, "summary": summarize(rows), "rows": rows})
                await asyncio.sleep(args.pause)
    summary = summarize(rows)
    say("まとめ：", json.dumps(summary, ensure_ascii=False))
    return {"model": args.model, "variants": VARIANTS, "summary": summary, "rows": rows}


def main() -> None:
    require_python()
    p = base_parser(__doc__)
    p.add_argument("--repeat", type=int, default=2)
    p.add_argument("--pause", type=float, default=3.0)
    p.add_argument("--type", choices=sorted(VOICE_TYPES), action="append")
    args = p.parse_args()
    save_result("G-33", asyncio.run(run(args)))
    drop_partial("G-33")


if __name__ == "__main__":
    main()
