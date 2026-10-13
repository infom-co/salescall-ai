"""G-26：相手が値を答えたのに、AIが報告（record_answer）をせずに自分で言い直してしまう誤りは、何で起きるか（2026-10-13）。

G-25（録音の名乗りの形・相手の発話を間なしで送る）で、相手が値を答えたターンの44%（34中15）で、AIが報告せずに
「15日の午後2時ですね」と自分で言い直した。相手の「はい、そうです」のあとで報告し、プログラムが決まりどおり言い直しを求めるので、
同じ確認を2回して、会話が1つずれた。言っていない「10月」を足した回もあった。G-16 の3回目・4回目（AIが名乗る形・別の耳を
会話の途中で動かす＝約8秒の間がある）では 60中0。
⇒ 違いは2つ：(1) 名乗りの形（AIが名乗る／録音）、(2) 相手が話し始めるまでの間（なし／8秒）。2×2で分ける。
本物の電話では、相手はAIが話し終えて1秒ほどで話す＝「間なし」に近い。
見ること：相手が値を答えたターン（「15日の午後2時です。」「3人です。」）で、AIが record_answer を呼んだか。値に、相手が言っていない
「月」などを足していないか。別の耳は使わない（このターンの声の中身は見ない。操作を呼んだかだけを見る）。
"""
from __future__ import annotations

import asyncio
import json

from common import base_parser, describe_error, drop_partial, make_client, pick_key, require_python, save_partial, save_result, say
from convo import run_call
from g25_after_tool import BUSINESS
from instruction import VOICE_TYPES

ANSWERS = ("15日の午後2時です。", "3人です。")  # 相手が値を答える発話
CALLEE = {"ai": ["はい、もしもし。", "はい、予約しています。", "15日の午後2時です。", "はい、そうです。", "3人です。", "はい、そうです。"],
          "recorded": ["はい、予約しています。", "15日の午後2時です。", "はい、そうです。", "3人です。", "はい、そうです。"]}
CELLS = [("ai", 0.0), ("ai", 8.0), ("recorded", 0.0), ("recorded", 8.0)]


def judge(call: dict) -> dict:
    turns = [t for t in call["turns"] if t["callee"] in ANSWERS]
    reported = [any(e["name"] == "record_answer" for e in t["tool_events"]) for t in turns]
    values = [e["args"].get("value") for t in call["turns"] for e in t["tool_events"] if e["name"] == "record_answer"]
    return {"answer_turns": len(turns), "reported": sum(reported), "skipped": len(turns) - sum(reported),
            "added_month": sum(1 for v in values if v and "月" in v), "values": values,
            "items_ok": call["items"]["Q2"]["value"] == "15日の午後2時" and call["items"]["Q3"]["value"] == "3人"
            and call["items"]["Q2"]["state"] == "confirmed" and call["items"]["Q3"]["state"] == "confirmed"}


def summarize(rows: list[dict]) -> dict:
    out = {}
    for mode, gap in CELLS:
        rs = [r for r in rows if r["mode"] == mode and r["gap_s"] == gap and "checks" in r]
        out[f"{mode}_間{gap:g}秒"] = {"calls": len(rs), "answer_turns": sum(r["checks"]["answer_turns"] for r in rs),
                                     "skipped": sum(r["checks"]["skipped"] for r in rs),
                                     "added_month": sum(r["checks"]["added_month"] for r in rs),
                                     "items_ok": sum(r["checks"]["items_ok"] for r in rs)}
    return out


async def run(args) -> dict:
    if args.dry_run:
        return {"dry_run": True, "cells": CELLS}
    client = make_client(pick_key(args))
    rows = []
    for rep in range(args.repeat):
        for vt in args.type or VOICE_TYPES:
            for mode, gap in CELLS:  # 4つの組み合わせを1本ずつ回す（時刻の影響を偏らせない）
                try:
                    call = await run_call(client, args.model, BUSINESS, vt, CALLEE[mode], opening_mode=mode, use_ear=False,
                                          save_audio="none", gap_s=gap)
                    row = {"rep": rep, "type": vt, "mode": mode, "gap_s": gap, "checks": judge(call), **call}
                except Exception as exc:
                    row = {"rep": rep, "type": vt, "mode": mode, "gap_s": gap, "error": describe_error(exc)}
                rows.append(row)
                c = row.get("checks", {})
                say(f"[{rep}] {vt} {mode} 間{gap:g}秒 報告しなかった={c.get('skipped')}/{c.get('answer_turns')} 月を足した={c.get('added_month')} "
                    f"項目={c.get('items_ok')} {row.get('error', '')}")
                save_partial("G-26", {"model": args.model, "summary": summarize(rows), "rows": rows})
                await asyncio.sleep(args.pause)
    summary = summarize(rows)
    say("まとめ：", json.dumps(summary, ensure_ascii=False))
    return {"model": args.model, "cells": CELLS, "summary": summary, "rows": rows}


def main() -> None:
    require_python()
    p = base_parser(__doc__)
    p.add_argument("--repeat", type=int, default=2)
    p.add_argument("--pause", type=float, default=3.0)
    p.add_argument("--type", choices=sorted(VOICE_TYPES), action="append")
    args = p.parse_args()
    save_result("G-26", asyncio.run(run(args)))
    drop_partial("G-26")


if __name__ == "__main__":
    main()
