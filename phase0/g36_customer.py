"""G-36：お客役を Leda・Sadachbia にした通話で、考えている相手への「はい」の2つの形と、聞き返しの直し（2026-10-14 ユーザー指示）を確かめる。

ユーザー（G-34 の通話全体を聞いて）：
- 「S2やS4の『えーとですね』の後の『はい』が、物欲しそうな、期待しているような『はい』に聞こえるので、相づち風の『はい』でいい。L4 は問題なし」
- 「相手役があまりにも機械っぽい声で臨場感がないので、Leda と Sadachbia に入れ替わりでお客役をやらせたら？話し方もお客っぽいものに」
- 「『失礼いたしました。』の後、『かしこまりました』はおかしい」⇒ お詫びのあとの言い直しの頭を「改めまして、」にした（案）
- 「『いつでも』『なるべく早く』は『即』＝よい答え。『-』はありえない」⇒ 日時の答えとして受け付ける
2回目（同じ日・ユーザーの続きの決定を入れたあと）に足した場面：
- 言い直し：相手が自分の言い間違いを直す ⇒「承知しました。」＋「それでは、16日の午後2時で、よろしいでしょうか？」
  （訂正＝こちらの聞き違い ⇒「失礼いたしました。」＋「改めまして、…大丈夫でしょうか？」）
- 先に答えた：日時の質問に「3人です。」⇒ プログラムが聞き直し、相手は「15日の午後2時でお願いします。」と答え直す
- 人数に別の答え：人数の質問に「家族で行こうと思ってます。」⇒ 数の型の形の確かめで聞き直し、相手は「3人です。」と答え直す
相手役は、聞き返されたら次の台詞へ進まず、答え直す（台詞の again。⛔ 1回目：台本の相手が聞き返しのあとも次の台詞へ進み、
「はい、そうです」「3人です」が日時の答えとして報告された＝試験の作りの誤り）。
比べる形（考えている相手への「はい」）：
- A_言い方を足す：AIが自分で「はい。」と言う。指示に「軽い相づちとして、落ち着いた声で、語尾を上げずに（期待するような言い方にしない）」を足した
- B_録音の相づち：AIは声を出さずに「相手が考えている」と報告し（返事を待たない宣言・話させない返事）、プログラムが相づち風に読んだ「はい。」の
  録音を流す（この報告が、相手が黙っている秒数＝5秒の数え始めにもなる）
お客役の声：会話のAIが Sadachbia（S2・S4）なら Leda、Leda（L4）なら Sadachbia（convo.callee_voice_for）。
見ること：
- 考えている相手に：A＝AIが言ったこと／B＝報告したか・黙っていたか・答えに対して誤って「考えている」と報告しなかったか
- 長い間（相手が6秒黙る）：AIが黙って待てたか
- 値と項目の状態（訂正＝16日・いつでも＝「いつでも大丈夫」が取れたか）・差し戻し・相づち・最初の声の間
- 聞き比べの声：長い間・訂正・いつでも の通話をつないだもの（ユーザーが「はい」の言い方・「改めまして、」・お客役の声を聞く）
"""
from __future__ import annotations

import asyncio
import json
import statistics as st
from pathlib import Path

from common import base_parser, describe_error, drop_partial, make_client, pick_key, require_python, save_partial, save_result, say
from convo import CUT_IN_S, CALLEE_FOR, callee_voice_for, run_call
from flowctl import GOT, normalize_value
from g22_opening import BUSINESS, W
from g30_latency import keep_call
from g34_ack import FAST, FOLLOWUP_WAIT_S, LONG_SILENCE_S
from instruction import VOICE_TYPES
from stitch_gap import callee_speech, join_with_pause, played_ack, stitch_call

VARIANTS = {"A_言い方を足す": {"thinking_op": False}, "B_録音の相づち": {"thinking_op": True}}
THINKING = ("pause700_datetime", "pause1500_three", "filler_eto", "unclear")  # 考えている相手の台詞
ANSWERS = ("datetime_pm", "three_people", "correct_16", "itsudemo", "restate_16", "kazoku")
WANT = {"考える間": ("15日の午後2時", "3人"), "長い間": ("15日の午後2時", "3人"), "訂正": ("16日の午後2時", "3人"),
        "いつでも": ("いつでも大丈夫", "3人"), "聞き取りにくい": ("15日の午後2時", "3人"), "言い直し": ("16日の午後2時", "3人"),
        "先に答えた": ("15日の午後2時", "3人"), "人数に別の答え": ("15日の午後2時", "3人")}
# 値を直されたあとに、プログラムが流すはずの相づち（言い直しの場面の見どころ）
WANT_AFTER = {"訂正": "shitsurei", "言い直し": "shouchi"}


def A(name: str, again: str | None = None) -> dict:
    """答えの台詞。聞き返されたら、again（無ければ同じ台詞）で答え直す。"""
    return {**W(name), "again": W(again or name)["wav"]}


def scenes() -> dict[str, list]:
    for v in CALLEE_FOR.values():
        join_with_pause(f"{v}_filler_eto.wav", 0.7, f"{v}_datetime_pm.wav", f"{v}_pause700_datetime.wav")
        join_with_pause(f"{v}_filler_eto.wav", 1.5, f"{v}_three_people.wav", f"{v}_pause1500_three.wav")
    return {
        "考える間": [W("yes_reserved"), W("pause700_datetime"), W("affirm"), W("pause1500_three"), W("affirm")],
        "長い間": [W("yes_reserved"), W("datetime_pm"), W("affirm"), W("filler_eto"), {"silence_s": LONG_SILENCE_S},
                  W("three_people"), W("affirm")],
        "訂正": [W("yes_reserved"), A("datetime_pm"), A("correct_16"), W("affirm"), A("three_people"), W("affirm")],
        "いつでも": [W("yes_reserved"), A("itsudemo"), W("affirm"), A("three_people"), W("affirm")],
        "聞き取りにくい": [W("yes_reserved"), W("unclear"), W("datetime_pm"), W("affirm"), W("three_people"), W("affirm")],
        "言い直し": [W("yes_reserved"), A("datetime_pm"), A("restate_16"), W("affirm"), A("three_people"), W("affirm")],
        "先に答えた": [W("yes_reserved"), A("three_people", "datetime_pm"), W("affirm"), A("three_people"), W("affirm")],
        "人数に別の答え": [W("yes_reserved"), A("datetime_pm"), W("affirm"), A("kazoku", "three_people"), W("affirm")],
    }


def utt(wav: str | None) -> str:
    """声のファイル名から台詞の名前（「Leda_filler_eto.wav」⇒「filler_eto」）。"""
    return wav.split("_", 1)[1].removesuffix(".wav") if wav and "_" in wav else (wav or "")


def turn_measures(call: dict) -> list[dict]:
    out = []
    for t in call["turns"]:
        c = t.get("callee") or {}
        said = (t.get("intended") or "").strip()
        names = [e["name"] for e in t.get("tool_events") or []]
        row = {"callee": utt(c.get("wav")) or ("followup" if "followup_of" in c else f"silence{c.get('silence_s')}"),
               "said": said[:60], "reports": names, "replies": [e.get("reply") for e in t.get("tool_events") or []],
               "spoke": bool(t.get("audio_file") or said), "thinking_report": "report_thinking" in names}
        if "followup_of" in c:
            row["followup_of"] = utt(c["followup_of"].get("wav")) if isinstance(c["followup_of"], dict) else None
            row["followup_reason"] = c.get("reason")
        if "wav" in c and t.get("first_audio_after_s") is not None:
            tail = callee_speech(c["wav"])[2]
            row.update({"cut_in": t["first_audio_after_s"] < CUT_IN_S, "ai_at_s": round(t["first_audio_after_s"] + tail, 3)})
        ack = played_ack(t)
        if ack is not None:
            row.update({"ack": ack["ack"], "ack_at_s": round(ack["at_s"], 3)})
        if "ai_at_s" in row or "ack_at_s" in row:
            row["first_heard_s"] = row.get("ack_at_s", row.get("ai_at_s"))
        out.append(row)
    return out


def judge(call: dict, scene: str) -> dict:
    it = call["items"]
    q2, q3 = WANT[scene]
    return {"q1_ok": it["Q1"]["condition"] == "はい" and it["Q1"]["state"] in GOT,
            "q2_ok": normalize_value("datetime", it["Q2"]["value"]) == q2 and it["Q2"]["state"] == "confirmed",
            "q3_ok": normalize_value("number", it["Q3"]["value"]) == q3 and it["Q3"]["state"] == "confirmed",
            "rejected": sum(1 for e in call["flow_log"] if e["reply"]["result"] == "rejected"),
            "ask_backs": sum(1 for e in call["flow_log"] if e["reply"]["result"] == "ask_back"),
            "final_phase": call["final_phase"]}


def after_ack(call: dict) -> str | None:
    """値を直された報告（違う値を言われた・相手が言い直した）のあとに流した相づち。"""
    return next((e["reply"].get("ack") for e in call["flow_log"]
                 if e["op"] == "confirm_field" and e["reply"]["result"] == "confirm_required" and e["reply"].get("after")), None)


def measures(call: dict, scene: str) -> dict:
    return {"turns_measured": turn_measures(call), "checks": {**judge(call, scene), "after_ack": after_ack(call)},
            "acks": [e["reply"].get("ack") for e in call["flow_log"] if e["reply"].get("ack")],
            "says": [e["reply"].get("say") for e in call["flow_log"] if e["reply"].get("say")]}


def summarize(rows: list[dict]) -> dict:
    def q(xs):
        xs = sorted(x for x in xs if x is not None)
        return {"n": len(xs), "median": round(st.median(xs), 2), "p90": round(xs[min(len(xs) - 1, int(len(xs) * 0.9))], 2)} if xs else None
    s = {}
    for name in VARIANTS:
        rs = [r for r in rows if r.get("variant") == name and "checks" in r]
        ts = [t for r in rs for t in r["turns_measured"]]
        thinking = [t for t in ts if t["callee"] in THINKING]
        s[name] = {
            "calls": len(rs), "errors": sum(1 for r in rows if r.get("variant") == name and "error" in r),
            "values_ok": sum(r["checks"]["q2_ok"] + r["checks"]["q3_ok"] for r in rs), "values": 2 * len(rs),
            "by_scene": {sc: f'{sum(r["checks"]["q2_ok"] and r["checks"]["q3_ok"] for r in rs if r["scene"] == sc)}/'
                             f'{sum(1 for r in rs if r["scene"] == sc)}' for sc in WANT if any(r["scene"] == sc for r in rs)},
            "after_ack_ok": {sc: f'{sum(r["checks"].get("after_ack") == a for r in rs if r["scene"] == sc)}/'
                                 f'{sum(1 for r in rs if r["scene"] == sc)}' for sc, a in WANT_AFTER.items() if any(r["scene"] == sc for r in rs)},
            "rejected": sum(r["checks"]["rejected"] for r in rs), "ask_backs": sum(r["checks"]["ask_backs"] for r in rs),
            "thinking_turns": len(thinking), "thinking_reported": sum(t["thinking_report"] for t in thinking),
            "thinking_spoke": sum(t["spoke"] for t in thinking),
            "thinking_said": sorted({t["said"][:16] for t in thinking if t["said"]}),
            "thinking_report_on_answer": sum(1 for t in ts if t["thinking_report"] and t["callee"] in ANSWERS),
            "spoke_in_silence": sum(1 for t in ts if t["callee"].startswith("silence") and t["spoke"]),
            "first_heard": q(t.get("first_heard_s") for t in ts if t["callee"] in ANSWERS + ("affirm", "yes_reserved") and not t.get("cut_in")),
        }
    return s


def make_listening(result: Path, out: Path) -> list[dict]:
    """長い間と、聞き返し・言い直しの場面の通話をつないだ聞き比べの声（声の型ごとに1本。決まりどおりに進んだ通話を先に選ぶ）。
    長い間は形ごと（考えている相手への「はい」を聞き比べる）、ほかの場面は、結果にある最初の形だけ。"""
    d = json.loads(result.read_text(encoding="utf-8"))
    out.mkdir(parents=True, exist_ok=True)
    rows = sorted((r for r in d["rows"] if "turns" in r), key=lambda r: not (r["checks"]["q2_ok"] and r["checks"]["q3_ok"]))
    made, n = [], 0
    present = [v for v in VARIANTS if any(r["variant"] == v for r in rows)]
    for scene in ("長い間", "訂正", "言い直し", "いつでも", "先に答えた", "人数に別の答え"):
        for name in present if scene == "長い間" else present[-1:]:
            for vt in VOICE_TYPES:
                for r in rows:
                    if (r["variant"], r["type"], r["scene"]) != (name, vt, scene):
                        continue
                    n += 1
                    path = out / f"{n:02d}_{scene}_{name.split('_')[0]}_{vt}_お客{callee_voice_for(vt)}.wav"
                    plan = stitch_call(r, vt, path)
                    if plan is None:
                        n -= 1
                        continue
                    made.append({"file": path.name, "heard_first": [p["heard_first"] for p in plan], "says": r.get("says")})
                    break
    return made


async def run(args) -> dict:
    plan = scenes()
    if args.dry_run:
        from make_callee_samples import verified
        need = {f"{v}_{utt(x[k])}.wav" for v in CALLEE_FOR.values() for xs in plan.values() for x in xs for k in ("wav", "again") if k in x}
        return {"dry_run": True, "missing": sorted(w for w in need if not verified(w) and "pause" not in w and "unclear" not in w)}
    client = make_client(pick_key(args))
    rows = []
    for rep in range(args.repeat):
        for vt in args.type or VOICE_TYPES:
            for scene in args.scene or tuple(plan):
                for name, kw in VARIANTS.items():
                    if args.variant and name not in args.variant:
                        continue
                    try:
                        call = await run_call(client, args.model, BUSINESS, vt, plan[scene], opening_mode="recorded", use_ear=False,
                                              save_audio="all", audio_label=f"G36_{name}_{scene}_{rep}", followup_wait_s=FOLLOWUP_WAIT_S,
                                              callee_voice="swap", **FAST, **kw)
                    except Exception as exc:
                        call, row = None, {"rep": rep, "type": vt, "scene": scene, "variant": name, "error": describe_error(exc)}
                    if call is not None:
                        row = keep_call({"rep": rep, "type": vt, "scene": scene, "variant": name}, call, lambda c: measures(c, scene))
                    rows.append(row)
                    tm = row.get("turns_measured", [])
                    say(f"[{rep}] {vt} {scene} {name} 値={row.get('checks')} 相づち={row.get('acks')} "
                        f"考える相手に={[(t['said'][:10], t['thinking_report']) for t in tm if t['callee'] in THINKING]} {row.get('error', '')}")
                    save_partial("G-36", {"model": args.model, "summary": summarize(rows), "rows": rows})
                    await asyncio.sleep(args.pause)
    summary = summarize(rows)
    say("まとめ：", json.dumps(summary, ensure_ascii=False))
    return {"model": args.model, "variant": FAST, "summary": summary, "rows": rows}


def main() -> None:
    require_python()
    p = base_parser(__doc__)
    p.add_argument("--repeat", type=int, default=1)
    p.add_argument("--pause", type=float, default=3.0)
    p.add_argument("--type", choices=sorted(VOICE_TYPES), action="append")
    p.add_argument("--scene", choices=tuple(WANT), action="append")
    p.add_argument("--variant", choices=tuple(VARIANTS), action="append", help="流す形（無ければ両方）")
    p.add_argument("--listen", type=Path, help="この結果から、聞き比べの声を作る（キーを使わない）")
    p.add_argument("--out", type=Path)
    args = p.parse_args()
    if args.listen:
        for m in make_listening(args.listen, args.out):
            print(json.dumps(m, ensure_ascii=False))
        return
    if args.dry_run:  # ⛔ 2026-10-14：下見の結果を保存し、途中の結果のファイルまで消していた（流れている G-36 の途中の結果）
        print(json.dumps(asyncio.run(run(args)), ensure_ascii=False))
        return
    save_result("G-36", asyncio.run(run(args)))
    drop_partial("G-36")


if __name__ == "__main__":
    main()
