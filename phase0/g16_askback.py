"""G-16：聞き返し・復唱・念のための確認・拒否・わからない・急いでいる相手が、決まり（DESIGN.md 4-4・6-11）どおりに動くか。

台本（相手の発話は文字）を場面ごとに用意し、通話の動かし方（convo）で1本ずつ動かす。確かめるのは2つの面：
- プログラムの面：項目の最後の状態・依頼主へ渡す書き方（flowctl の記録）が、場面の答えと一致するか
- 声の面：決まった言い方（聞き返し・念のため）を、決まった回数だけ言ったか。拒否された質問を、二度と口にしていないか
  （AIの声は別の耳で文字にしたもので見る。AI自身の文字起こしは使わない）
⚠️ 限界：相手の発話は文字で送っている（決まりの確かめのため）。実際の声での聞き取りは G-14 で確かめる。
"""
from __future__ import annotations

import asyncio
import json

from common import base_parser, describe_error, make_client, pick_key, require_python, save_result, say
from convo import run_call
from turn_check import bad_kinds
from flowctl import GOT, Question, tool_declarations
from instruction import ASK_BACK, VOICE_TYPES, Business, build
from live_util import build_config

BUSINESS = Business(
    company="アオバ物流", caller="佐藤", purpose="ご予約の確認",
    questions=[Question("Q1", "ご予約はされていますか。", "yes_no", conditions=("はい", "いいえ")),
               Question("Q2", "ご希望の日時をお聞かせください。", "datetime", conditions=("答えた",)),
               Question("Q3", "ご来店の人数をお聞かせください。", "number", conditions=("答えた",))],
)
# 「答えたが聞き取れない」。⛔「えっと、じゅう……の……」は、考えている途中とも取れる（2026-10-13 の2回目で、AIは
#   「ごゆっくりどうぞ」と待った。それは決まりどおり）⇒ はっきり「答えたが聞き取れない」と分かる形にした
UNCLEAR = "［雑音で、ほとんど聞き取れない］……にち……じ……"
# 場面：相手の発話（順番どおり）と、答え（最後の状態・依頼主へ渡す書き方・言い方の回数・口にしてはいけない言葉）
SCENES = {
    "A_聞き返し1回": {
        "callee": ["はい、もしもし。", "はい、予約しています。", UNCLEAR, "15日の午後2時です。", "はい、そうです。",
                   "3人です。", "はい、そうです。"],
        "export": {"Q1": "はい", "Q2": "15日の午後2時", "Q3": "3人"}, "ask_back": 1, "recheck": 0},
    "B_2回聞けない→念のため": {
        # 2回目も「答えたが聞き取れない」。⛔「えー、その……」（考えている）は聞き取れない答えではない（G-16 の最初の版の誤り）
        "callee": ["はい、もしもし。", "はい、予約しています。", UNCLEAR, "［また雑音で、聞き取れない］……の……ご……", "3人です。", "はい、そうです。",
                   "15日の午後2時です。", "はい、そうです。"],
        "export": {"Q1": "はい", "Q2": "15日の午後2時", "Q3": "3人"}, "ask_back": 1, "recheck": 1},
    "C_拒否": {
        "callee": ["はい、もしもし。", "はい、予約しています。", "15日の午後2時です。", "はい、そうです。",
                   "人数は、ちょっとお答えできません。"],
        "export": {"Q1": "はい", "Q2": "15日の午後2時", "Q3": "✕"}, "ask_back": 0, "recheck": 0,
        # 拒否のあとに、人数を聞き直していないか（「人数は結構です」のような言い方は数えない）
        "must_not_say_after": {"from_index": 4, "words": ["何名", "何人", "人数をお", "人数を教"]}},
    "D_急いでいる": {
        "callee": ["はい、もしもし。", "はい、予約しています。", UNCLEAR, "すみません、いま急いでいるので、これで失礼します。"],
        "export": {"Q1": "はい", "Q2": "-", "Q3": "-"}, "ask_back": 1, "recheck": 0},
    "E_訂正": {
        "callee": ["はい、もしもし。", "はい、予約しています。", "15日の午後2時です。", "いえ、16日です。午後2時で合っています。",
                   "はい、そうです。", "3人です。", "はい、そうです。"],
        "export": {"Q1": "はい", "Q2": "16日の午後2時", "Q3": "3人"}, "ask_back": 0, "recheck": 0},
    "G_考えている": {
        # 相手が考えているときは、何も報告せず待つ。質問を聞き直さない（聞き返しに数えない）
        "callee": ["はい、もしもし。", "はい、予約しています。", "えー、その……", "15日の午後2時です。", "はい、そうです。",
                   "3人です。", "はい、そうです。"],
        "export": {"Q1": "はい", "Q2": "15日の午後2時", "Q3": "3人"}, "ask_back": 0, "recheck": 0,
        "must_not_say_after": {"from_index": 2, "until_index": 3, "words": ["お聞かせ", "教えて", "日時を"]}},
    "F_わからない": {
        "callee": ["はい、もしもし。", "はい、予約しています。", "15日の午後2時です。", "はい、そうです。",
                   "人数は、まだ決まっていなくて、わからないです。"],
        "export": {"Q1": "はい", "Q2": "15日の午後2時", "Q3": "-"}, "ask_back": 0, "recheck": 0},
}


def judge_scene(name: str, scene: dict, call: dict) -> dict:
    said = [t.get("ear", "") for t in call["turns"]]
    alltext = "".join(said)
    got = call["export"]
    exp = scene["export"]
    def value_ok(qid):
        e, it = exp[qid], call["items"][qid]
        if e in ("-", "✕"):
            return got.get(qid) == e
        if e == "はい":  # はい／いいえは、分類（条件）で見る
            return it["condition"] == "はい" and it["state"] in GOT
        # 値は言い回しが揺れるので、数字と「午前／午後」を含むかで見る
        keys = [k for k in ("15", "16", "2", "3", "午後") if k in e]
        return it["state"] in GOT and all(k in (it["value"] or "") for k in keys)
    # 聞き返し・念のための確認の回数は、プログラムの記録で数える（言い方は、AIが復唱でも「念のため」と言うので、声では数えない）。
    # 声で確かめるのは、プログラムが求めたそのターンで、決まった言い方をしたか。
    def said_in_turns_with(result, word):
        hits = []
        for t in call["turns"]:
            if any(e.get("reply") == result for e in t["tool_events"]):
                hits.append(word in t.get("ear", ""))
        return hits
    checks = {
        "export_ok": all(value_ok(q) for q in exp),
        "ask_back_said": alltext.count(ASK_BACK[:6]),            # 「聞き取りにく」（決まった言い方なので、声で数えられる）
        "program_ask_back": sum(1 for e in call["flow_log"] if e["reply"].get("result") == "ask_back"),
        "program_recheck": sum(1 for e in call["flow_log"] if e["reply"].get("result") == "recheck"),
        "ask_back_phrase_in_turn": said_in_turns_with("ask_back", ASK_BACK[:6]),
        "recheck_phrase_in_turn": said_in_turns_with("recheck", "念のため"),
        "bad_turns": bad_kinds(call["turns"]),
        "rejected_ops": [e for e in call["flow_log"] if e["reply"].get("result") == "rejected"],
        "closed": call["final_phase"] == "closing",
    }
    checks["ask_back_ok"] = (checks["ask_back_said"] == scene["ask_back"] == checks["program_ask_back"]
                             and all(checks["ask_back_phrase_in_turn"]))
    checks["recheck_ok"] = checks["program_recheck"] == scene["recheck"] and all(checks["recheck_phrase_in_turn"])
    if "must_not_say_after" in scene:
        m = scene["must_not_say_after"]
        later = "".join(said[m["from_index"]:m.get("until_index")])
        checks["not_asked_again"] = not any(w in later for w in m["words"])
    checks["passed"] = all(checks[k] for k in ("export_ok", "ask_back_ok", "recheck_ok", "closed")) \
        and checks.get("not_asked_again", True) and not checks["bad_turns"]
    return checks


async def run(args) -> dict:
    if args.dry_run:
        for t in VOICE_TYPES:
            v, text = build(BUSINESS, t)
            build_config(voice=v, system_instruction=text, tools=tool_declarations(BUSINESS.questions))
        return {"dry_run": True}
    client = make_client(pick_key(args))
    rows = []
    for rep in range(args.repeat):
        for name, scene in SCENES.items():
            if args.scene and name not in args.scene:
                continue
            for vt in args.type or VOICE_TYPES:
                try:
                    call = await run_call(client, args.model, BUSINESS, vt, scene["callee"], save_audio=args.save_audio,
                                          audio_label=f"G16_{name}_{rep}")
                    row = {"rep": rep, "scene": name, "type": vt, "checks": judge_scene(name, scene, call), **call}
                except Exception as exc:
                    row = {"rep": rep, "scene": name, "type": vt, "error": describe_error(exc)}
                rows.append(row)
                c = row.get("checks", {})
                say(f"[{rep}] {name} {vt} 合格={c.get('passed')} 渡す値={row.get('export')} 聞き返し={c.get('ask_back_said')} "
                    f"念のため（プログラム）={c.get('program_recheck')} 差し戻し={len(c.get('rejected_ops', []))} {row.get('error', '')}")
                await asyncio.sleep(args.pause)
    summary = {"runs": len(rows), "passed": sum(1 for r in rows if r.get("checks", {}).get("passed")),
               "by_scene": {n: [r.get("checks", {}).get("passed") for r in rows if r["scene"] == n] for n in SCENES}}
    say("まとめ：", json.dumps(summary, ensure_ascii=False))
    return {"model": args.model, "summary": summary, "rows": rows}


def main() -> None:
    require_python()
    p = base_parser(__doc__)
    p.add_argument("--repeat", type=int, default=1)
    p.add_argument("--pause", type=float, default=4.0)
    p.add_argument("--type", choices=sorted(VOICE_TYPES), action="append")
    p.add_argument("--scene", choices=sorted(SCENES), action="append")
    p.add_argument("--save-audio", choices=("bad", "all", "none"), default="bad", help="AIの声を残すターン（convo.run_call）")
    args = p.parse_args()
    save_result("G-16", asyncio.run(run(args)))


if __name__ == "__main__":
    main()
