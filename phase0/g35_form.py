"""G-35：値の形の確かめと、そのあとの流れ（DESIGN.md 6-2。✅ 2026-10-14 ユーザー承認「差し戻した後の仕組みの設計をしっかりと」）を、
通話の形で確かめる。

決まり：日付・時刻・日時の型の値が、別の型の値（「3人」「はい」）になっていたら、値は記録せずに聞き返す（必須は3回まで。言い方は段ごと。
DESIGN.md 6-11）。使い切っていたら「-」にして次へ。必須なら、締めの前の念のための確認でもう1回だけ聞く。AIに選ばせず、流れの制御が決める。
✅ 2026-10-14 ユーザー：「いつでも」「なるべく早く」は「即」＝よい答え（「-」はありえない）⇒ 日時の答えとして受け付ける。
場面（相手の台詞。台詞の順は、決まりどおりに進んだときの順）：
- 先に答えた：日時を聞いたのに「3人です。」⇒ 聞き直し ⇒「15日の午後2時でお願いします。」⇒ 確かめ ⇒ 人数「3人です。」
- 日時を決めない答え：「いつでも大丈夫です。」⇒ 答えとして言い直して確かめる ⇒「はい、そうです。」⇒ 人数「3人です。」
見ること：
- 日時の項目に、日時でない値が「取れた」として入った回（0 でなければならない）
- 聞き直しが起きたか・AIが聞き直しの言い方のとおりに言ったか・同じ項目で聞き直しが2回以上起きなかったか
- AIが報告せずに自分で対応したターン（「日時をお伺いしております」など）
- 値と項目の状態・差し戻し
"""
from __future__ import annotations

import asyncio
import json

from common import base_parser, describe_error, drop_partial, make_client, pick_key, require_python, save_partial, save_result, say
from convo import run_call
from flowctl import GOT, form_problem
from g22_opening import BUSINESS, W
from g30_latency import keep_call
from g34_ack import FAST, FOLLOWUP_WAIT_S
from instruction import VOICE_TYPES

SCENES = {
    "先に答えた": [W("yes_reserved"), W("three_people"), W("datetime_pm"), W("affirm"), W("three_people"), W("affirm")],
    "日時を決めない答え": [W("yes_reserved"), W("itsudemo"), W("affirm"), W("three_people"), W("affirm")],
}
WANT = {"先に答えた": {"Q2": ("confirmed", "15日の午後2時"), "Q3": ("confirmed", "3人")},
        "日時を決めない答え": {"Q2": ("confirmed", "いつでも大丈夫"), "Q3": ("confirmed", "3人")}}


def measures(call: dict, scene: str) -> dict:
    it = call["items"]
    reasks = [e for e in call["flow_log"] if e["reply"]["result"] == "ask_back" and e["reply"].get("ask_reason") == "form"]
    reask_turns = [t for t in call["turns"] if any(e.get("reply") == "ask_back" and e.get("say") for e in t.get("tool_events") or [])]
    want = WANT[scene]
    return {
        "checks": {
            "wrong_value_got": sum(1 for q in ("Q2",) if it[q]["state"] in GOT and form_problem("datetime", it[q]["value"])),
            "q2_ok": (it["Q2"]["state"], it["Q2"]["value"]) == want["Q2"],
            "q3_ok": (it["Q3"]["state"], it["Q3"]["value"]) == want["Q3"],
            "reasks": len(reasks),
            "reask_said_as_told": [next(e["say"] for e in t["tool_events"] if e.get("say"))[:8] in (t.get("intended") or "")
                                   for t in reask_turns],
            "rejected": sum(1 for e in call["flow_log"] if e["reply"]["result"] == "rejected"),
            "final_phase": call["final_phase"],
        },
        "said": [((t.get("callee") or {}).get("wav", "")[5:-4], (t.get("intended") or "")[:50],
                  [e.get("reply") for e in t.get("tool_events") or []]) for t in call["turns"]],
        "export": call["export"],
    }


def summarize(rows: list[dict]) -> dict:
    s = {}
    for scene in SCENES:
        rs = [r for r in rows if r.get("scene") == scene and "checks" in r]
        s[scene] = {"calls": len(rs), "errors": sum(1 for r in rows if r.get("scene") == scene and "error" in r),
                    "wrong_value_got": sum(r["checks"]["wrong_value_got"] for r in rs),
                    "q2_ok": sum(r["checks"]["q2_ok"] for r in rs), "q3_ok": sum(r["checks"]["q3_ok"] for r in rs),
                    "reask_counts": [r["checks"]["reasks"] for r in rs],
                    "reask_said_as_told": [x for r in rs for x in r["checks"]["reask_said_as_told"]],
                    "rejected": sum(r["checks"]["rejected"] for r in rs)}
    return s


async def run(args) -> dict:
    if args.dry_run:
        from make_callee_samples import verified
        return {"dry_run": True, "missing": sorted({x["wav"] for v in SCENES.values() for x in v if not verified(x["wav"])})}
    client = make_client(pick_key(args))
    rows = []
    for rep in range(args.repeat):
        for vt in args.type or VOICE_TYPES:
            for scene, callee in SCENES.items():
                try:
                    call = await run_call(client, args.model, BUSINESS, vt, callee, opening_mode="recorded", use_ear=False,
                                          save_audio="all", audio_label=f"G35_{scene}_{rep}", followup_wait_s=FOLLOWUP_WAIT_S, **FAST)
                except Exception as exc:
                    call, row = None, {"rep": rep, "type": vt, "scene": scene, "error": describe_error(exc)}
                if call is not None:
                    row = keep_call({"rep": rep, "type": vt, "scene": scene}, call, lambda c: measures(c, scene))
                rows.append(row)
                say(f"[{rep}] {vt} {scene} 確かめ={row.get('checks')} 書き出し={row.get('export')} {row.get('error', '')}")
                save_partial("G-35", {"model": args.model, "summary": summarize(rows), "rows": rows})
                await asyncio.sleep(args.pause)
    summary = summarize(rows)
    say("まとめ：", json.dumps(summary, ensure_ascii=False))
    return {"model": args.model, "variant": FAST, "summary": summary, "rows": rows}


def main() -> None:
    require_python()
    p = base_parser(__doc__)
    p.add_argument("--repeat", type=int, default=2)
    p.add_argument("--pause", type=float, default=3.0)
    p.add_argument("--type", choices=sorted(VOICE_TYPES), action="append")
    args = p.parse_args()
    save_result("G-35", asyncio.run(run(args)))
    drop_partial("G-35")


if __name__ == "__main__":
    main()
