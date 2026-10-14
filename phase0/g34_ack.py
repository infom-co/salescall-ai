"""G-34：AIの報告が届いた瞬間に、プログラムが録音の相づちを流す形（DESIGN.md 6-15）と、考えている相手への「はい。」の決まりを、
通話の形で測る。

✅ 2026-10-14 ユーザー（聞き比べのあと）：
- 報告の時刻に「はい、」を流す形は「明らかにこちらの方が自然な間。話の流れもいい感じ」⇒ 採る。
- 「基本は『はい、』で大丈夫。違和感のある会話や、『はい』が続くようなら、別の相づちもあった方がいい」⇒ どの場面でどれを流すかは
  流れの制御（flowctl.Flow._ack）の1か所が決める。
- 考えている相手（「えーとですね、」で止まる人）には短い「はい。」。「さらに間が空くようなら、『いかがされましたか？』
  『どうなされましたか？』などを入れるといい」⇒ その一言は電話接続側が流す（AIは時間を数えられない）。⏳ 秒数はユーザーに確かめる。
話し終わりの判定は 0.4秒・考える量0（G-30 の C の形。ユーザー：判定を無理に早めすぎない）。
場面（相手の台詞。どれも別の耳の確かめに通った声）：
- ふつう：答え・言い直しへの「はい」がふつうに続く
- 考える間：「えーとですね、」のあと 0.7秒・1.5秒あけて答える（相づち「はい。」だけで待てるか。割り込んだら、相手の続きに返事をするか）
- 長い間：「えーとですね、」のあと、相手が 6秒黙る（AIが「はい。」のあと黙って待てるか。黙っている間にAIが話し出したら、
  電話接続側の一言とぶつかる）
- 訂正：言い直した値を相手が直す（「いえ、16日の午後2時です。」）⇒ 相づち「失礼いたしました。」
- 聞き取りにくい：「えっと、じゅう、の……」（聞き返しなら相づち「申し訳ございません、」。考えている相手として「はい。」で待つこともありうる）
見ること：
- 相手に聞こえる最初の声（相づち、または相づちが流れないときはAIの声）の、声の終わりからの間（目安 0.7〜1.0秒。ユーザー）
- 相づちのあと、AIの声を待たせた長さ（相づちが終わるまでAIの声を止める）と、相づちとAIの声の間
- 報告より先にAIが話し始めたターン（相づちは流さない）
- AIが自分で「はい」から話し始めて、相づちと重なったか
- 考えている相手に言ったこと（「はい。」だけか）・黙っている間に話したか
- 値と項目の状態・報告の飛ばし・差し戻し
- 言い直しの声を別の耳で聞いて、値と頭の受けの言葉（「承知いたしました。」など）が聞こえたか（2通りの聞き方のどちらか）。
  ⚠️ 相づちの録音を作ったとき、「承知しました」が崩れやすかった（S4 で3回中2回「そうしました」）。AIが自分で言う受けの言葉にも起きうる
"""
from __future__ import annotations

import asyncio
import json
import statistics as st
from pathlib import Path

from common import (base_parser, describe_error, drop_partial, make_client, pick_key, read_wav_pcm16_mono, require_python,
                    save_partial, save_result, say)
from convo import AUDIO_DIR, CUT_IN_S, run_call
from g22_opening import BUSINESS, W
from g30_latency import VARIANTS as G30_VARIANTS
from g30_latency import keep_call
from instruction import VOICE_TYPES
from jtext import compare_reading
from listen import hear_feeds, in_any
from stitch_gap import _speech_only, ack_wav, callee_speech, join_with_pause, played_ack, stitch_call

FAST = G30_VARIANTS["C_考える量0＋判定0.4秒"]
FOLLOWUP_WAIT_S = 10.0
LONG_SILENCE_S = 6.0
JOIN_S = 0.05  # 相づちが終わってからAIの声を流すまでの最小の間（電話接続側が守る順）


def scenes() -> dict[str, list]:
    p07, p15 = "Puck_pause700_datetime.wav", "Puck_pause1500_three.wav"
    join_with_pause("Puck_filler_eto.wav", 0.7, "Puck_datetime_pm.wav", p07)
    join_with_pause("Puck_filler_eto.wav", 1.5, "Puck_three_people.wav", p15)
    return {
        "ふつう": [W("yes_reserved"), W("datetime_pm"), W("affirm"), W("three_people"), W("affirm")],
        "考える間": [W("yes_reserved"), {"wav": p07}, W("affirm"), {"wav": p15}, W("affirm")],
        "長い間": [W("yes_reserved"), W("datetime_pm"), W("affirm"), W("filler_eto"), {"silence_s": LONG_SILENCE_S},
                  W("three_people"), W("affirm")],
        "訂正": [W("yes_reserved"), W("datetime_pm"), W("correct_16"), W("affirm"), W("three_people"), W("affirm")],
        "聞き取りにくい": [W("yes_reserved"), W("unclear"), W("datetime_pm"), W("affirm"), W("three_people"), W("affirm")],
    }


WANT = {"ふつう": ("15日の午後2時", "3人"), "考える間": ("15日の午後2時", "3人"), "長い間": ("15日の午後2時", "3人"),
        "訂正": ("16日の午後2時", "3人"), "聞き取りにくい": ("15日の午後2時", "3人")}
THINKING_WAVS = ("Puck_pause700_datetime.wav", "Puck_pause1500_three.wav", "Puck_filler_eto.wav", "Puck_unclear.wav")


def ack_seconds(voice_type: str, ack: str) -> float | None:
    path = ack_wav(voice_type, ack)
    if not path.exists():
        return None
    x, rate = _speech_only(path)
    return len(x) / rate


def turn_measures(call: dict, voice_type: str) -> list[dict]:
    out = []
    for t in call["turns"]:
        c = t.get("callee")
        if not isinstance(c, dict):
            continue
        said = (t.get("intended") or "").strip()
        row = {"callee": c.get("wav") or ("followup" if "followup_of" in c else f"silence{c.get('silence_s')}"),
               "said": said[:60], "reports": [e["name"] for e in t.get("tool_events") or []],
               "replies": [e["reply"] for e in t.get("tool_events") or []]}
        spoke = bool(t.get("audio_file") or said)
        if "silence_s" in c:
            row["spoke_in_silence"] = spoke
            out.append(row)
            continue
        if "followup_of" in c:
            # 続きを待った行：相手の台詞に続きが無い（「えーとですね、」だけ）なら、ここで話したのは、相手が黙っている間に話したこと
            of = c["followup_of"].get("wav") if isinstance(c["followup_of"], dict) else None
            row.update({"followup_reason": c.get("reason"), "followup_of": of, "spoke": spoke})
            if of == "Puck_filler_eto.wav":
                row["spoke_in_silence"] = spoke
            out.append(row)
            continue
        if "wav" not in c or t.get("first_audio_after_s") is None:
            out.append(row)
            continue
        tail = callee_speech(c["wav"])[2]
        ai_at = t["first_audio_after_s"] + tail
        row.update({"cut_in": t["first_audio_after_s"] < CUT_IN_S, "ai_at_s": round(ai_at, 3)})
        first_tool = next((e for e in t.get("tool_events") or []), None)
        row["spoke_before_report"] = bool(first_tool and first_tool.get("at_s") is not None and t["first_audio_after_s"] <= first_tool["at_s"])
        ack = played_ack(t)
        if ack is not None:
            secs = ack_seconds(voice_type, ack["ack"])
            row.update({"ack": ack["ack"], "ack_at_s": round(ack["at_s"], 3), "ack_s": round(secs, 3) if secs else None})
            if secs is not None:
                start = max(ai_at, ack["at_s"] + secs + JOIN_S)
                row.update({"ai_held_s": round(start - ai_at, 3), "ack_to_ai_s": round(start - ack["at_s"] - secs, 3)})
            row["ai_starts_with_hai"] = said.startswith("はい")
        row["first_heard_s"] = row.get("ack_at_s", row["ai_at_s"])
        out.append(row)
    return out


def judge(call: dict, scene: str) -> dict:
    it = call["items"]
    q2, q3 = WANT[scene]
    return {"q1_ok": it["Q1"]["condition"] == "はい" and it["Q1"]["state"] == "accepted",
            "q2_ok": it["Q2"]["value"] == q2 and it["Q2"]["state"] == "confirmed",
            "q3_ok": it["Q3"]["value"] == q3 and it["Q3"]["state"] == "confirmed",
            "rejected": sum(1 for e in call["flow_log"] if e["reply"]["result"] == "rejected"),
            "final_phase": call["final_phase"]}


def measures(call: dict, voice_type: str, scene: str) -> dict:
    return {"turns_measured": turn_measures(call, voice_type), "checks": judge(call, scene),
            "acks": [e["reply"].get("ack") for e in call["flow_log"] if e["reply"].get("ack")]}


def summarize(rows: list[dict]) -> dict:
    def q(xs):
        xs = sorted(x for x in xs if x is not None)
        return {"n": len(xs), "median": round(st.median(xs), 2), "p90": round(xs[min(len(xs) - 1, int(len(xs) * 0.9))], 2),
                "max": round(xs[-1], 2)} if xs else None
    s = {}
    for scene in scenes_names():
        rs = [r for r in rows if r.get("scene") == scene and "checks" in r]
        ts = [t for r in rs for t in r["turns_measured"]]
        normal = [t for t in ts if "ai_at_s" in t and not t.get("cut_in") and t["callee"] not in THINKING_WAVS]
        s[scene] = {
            "calls": len(rs), "errors": sum(1 for r in rows if r.get("scene") == scene and "error" in r),
            "values_ok": sum(r["checks"]["q2_ok"] + r["checks"]["q3_ok"] for r in rs), "values": 2 * len(rs),
            "q1_ok": sum(r["checks"]["q1_ok"] for r in rs), "rejected": sum(r["checks"]["rejected"] for r in rs),
            "first_heard": q(t["first_heard_s"] for t in normal), "ai_voice": q(t["ai_at_s"] for t in normal),
            "ai_held": q(t.get("ai_held_s") for t in normal), "ack_to_ai": q(t.get("ack_to_ai_s") for t in normal),
            "no_ack_turns": sum(1 for t in normal if "ack" not in t),
            "spoke_before_report": sum(1 for t in normal if t.get("spoke_before_report")),
            "ai_starts_with_hai": sum(1 for t in normal if t.get("ai_starts_with_hai")),
            "thinking_said": sorted({t["said"][:20] or "（黙って待った）" for t in ts if t["callee"] in THINKING_WAVS}),
            "thinking_waited": sum(1 for t in ts if t.get("followup_reason") == "waited"),
            "thinking_cut_in": sum(1 for t in ts if t["callee"] in THINKING_WAVS and t.get("cut_in")),
            "spoke_in_silence": sum(1 for t in ts if t.get("spoke_in_silence")),
            "acks": [r["acks"] for r in rs][:6],
            "readbacks": sum(len(r.get("readback_heard", [])) for r in rs),
            "readback_value_missed": sum(1 for r in rs for b in r.get("readback_heard", []) if not b["value_heard"]),
            "readback_lead_missed": sorted(b["say"].split("。")[0] for r in rs for b in r.get("readback_heard", []) if not b["lead_heard"]),
        }
    return s


async def readback_heard(client, call: dict) -> list[dict]:
    """言い直しの声を別の耳で聞く：言わせた文の値と、頭の受けの言葉が聞こえたか（2通りの聞き方のどちらか。G-27 と同じ見方）。"""
    out = []
    for t in call["turns"]:
        asked = [e for e in t.get("tool_events") or [] if e.get("reply") == "confirm_required" and e.get("say")]
        if not (asked and t.get("audio_file")):
            continue
        said = asked[-1]["say"]
        lead = said.split("。")[0] + "。"
        value = (asked[-1]["args"].get("value") or asked[-1]["args"].get("correction") or "").strip()
        pcm, rate = read_wav_pcm16_mono(AUDIO_DIR / t["audio_file"])
        heard = await hear_feeds(client, pcm, rate)
        out.append({"say": said, "heard": heard,
                    "lead_heard": in_any(heard, lambda v: compare_reading(lead) in compare_reading(v)),
                    "value_heard": bool(value) and in_any(heard, lambda v: compare_reading(value) in compare_reading(v))})
    return out


def scenes_names() -> tuple[str, ...]:
    return tuple(WANT)


async def run(args) -> dict:
    plan = scenes()
    if args.dry_run:
        missing = [(vt, a) for vt in VOICE_TYPES for a in ("hai", "shouchi", "arigatou", "shitsurei", "moushiwake")
                   if not ack_wav(vt, a).exists()]
        return {"dry_run": True, "scenes": {k: [x.get("wav") or x for x in v] for k, v in plan.items()}, "ack_missing": missing}
    client = make_client(pick_key(args))
    rows = []
    for rep in range(args.repeat):
        for vt in args.type or VOICE_TYPES:
            for scene in args.scene or scenes_names():
                try:
                    call = await run_call(client, args.model, BUSINESS, vt, plan[scene], opening_mode="recorded", use_ear=False,
                                          save_audio="all", audio_label=f"G34_{scene}_{rep}", followup_wait_s=FOLLOWUP_WAIT_S, **FAST)
                except Exception as exc:
                    call, row = None, {"rep": rep, "type": vt, "scene": scene, "error": describe_error(exc)}
                if call is not None:
                    row = keep_call({"rep": rep, "type": vt, "scene": scene}, call, lambda c: measures(c, vt, scene))
                    try:
                        row["readback_heard"] = await readback_heard(client, call)
                    except Exception as exc:
                        row["readback_heard"], row["hear_error"] = [], describe_error(exc)
                rows.append(row)
                tm = row.get("turns_measured", [])
                say(f"[{rep}] {vt} {scene} 値={row.get('checks')} 相づち={row.get('acks')} "
                    f"最初の声={[t.get('first_heard_s') for t in tm if 'first_heard_s' in t]} "
                    f"考える相手に={[t['said'][:12] for t in tm if t['callee'] in THINKING_WAVS]} "
                    f"黙る間に話した={[t['spoke_in_silence'] for t in tm if 'spoke_in_silence' in t]} {row.get('error', '')}")
                save_partial("G-34", {"model": args.model, "summary": summarize(rows), "rows": rows})
                await asyncio.sleep(args.pause)
    summary = summarize(rows)
    say("まとめ：", json.dumps(summary, ensure_ascii=False))
    return {"model": args.model, "variant": FAST, "long_silence_s": LONG_SILENCE_S, "summary": summary, "rows": rows}


LISTEN_SCENES = ("ふつう", "訂正", "長い間", "聞き取りにくい")


def make_listening(result: Path, out: Path) -> list[dict]:
    """結果から、会話全体をつないだ聞き比べの声を作る（声の型ごとに、場面ごとに1本。つなげる通話の最初のもの）。
    相手が次に話し始めるまでの間は 0.6秒（作った値）。割り込みのあった通話は、本当の時刻が分からないので使わない（stitch_call）。"""
    d = json.loads(result.read_text(encoding="utf-8"))
    out.mkdir(parents=True, exist_ok=True)
    made, n = [], 0
    for vt in VOICE_TYPES:
        for scene in LISTEN_SCENES:
            for r in d["rows"]:
                if r.get("type") != vt or r.get("scene") != scene or "turns" not in r:
                    continue
                n += 1
                path = out / f"{n:02d}_{vt}_{scene}.wav"
                plan = stitch_call(r, vt, path)
                if plan is None:
                    n -= 1
                    continue
                made.append({"file": path.name, "acks": [p["heard_first"] for p in plan], "first_s": [p["first_s"] for p in plan],
                             "ai_held_s": [p["ai_held_s"] for p in plan], "said": [(t.get("intended") or "")[:30] for t in r["turns"]]})
                break
    return made


def main() -> None:
    require_python()
    p = base_parser(__doc__)
    p.add_argument("--listen", type=Path, help="この結果から、会話全体の聞き比べの声を作る（キーを使わない）")
    p.add_argument("--out", type=Path, help="--listen の置き場")
    p.add_argument("--repeat", type=int, default=2)
    p.add_argument("--pause", type=float, default=3.0)
    p.add_argument("--type", choices=sorted(VOICE_TYPES), action="append")
    p.add_argument("--scene", choices=scenes_names(), action="append")
    args = p.parse_args()
    if args.listen:
        for m in make_listening(args.listen, args.out):
            print(json.dumps(m, ensure_ascii=False))
        return
    save_result("G-34", asyncio.run(run(args)))
    drop_partial("G-34")


if __name__ == "__main__":
    main()
