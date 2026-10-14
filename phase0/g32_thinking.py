"""G-32：相手が「えーとですね、」のあと間をあけたとき、AIが「ごゆっくりどうぞ」と割り込む件。決まりを「何も言わずに待つ」に変えると、
割り込みが消えるか（DESIGN.md 6-15）。

G-31：割り込み（AIが間の中で話し出す）は、話し終わりの判定が既定の形で間1.0秒から、判定0.4秒の形で間0.7秒から起きた。言ったのは
決まりどおりの「ごゆっくりどうぞ。」。本物の電話では、相手の続きの言葉にかぶる。
比べる形（同じ台本。間は 0.7・1.0・1.5秒）：
- A_ごゆっくり：判定は既定、いまの決まり
- C_ごゆっくり：考える量0＋判定0.4秒、いまの決まり
- C_黙って待つ：考える量0＋判定0.4秒、決まりを「何も言わずに、相手が話し終えるまで待つ」に変える
見ること：割り込み（間の中でAIが声を出したか・言ったこと）、割り込んだあと相手の続きに返事をしたか（10秒まで。無音を送りながら待つ）、
値が正しく取れたか、ふつうのターンの間（判定を速くした効き目が、決まりを変えても残るか）。
"""
from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager

import instruction
from common import base_parser, describe_error, drop_partial, make_client, pick_key, require_python, save_partial, save_result, say
from convo import run_call
from g22_opening import BUSINESS, W
from g30_latency import VARIANTS as G30_VARIANTS
from g30_latency import turn_measures
from g31_pause import FOLLOWUP_WAIT_S, clips, judge, measure
from instruction import VOICE_TYPES

PAUSES = (0.7, 1.0, 1.5)
WAIT_SILENTLY = ("相手が考えている（「えー」「えーと」「その」など、まだ答え終わっていない）ときは、何も言わずに、相手が話し終えるまで"
                 "待ちます。何も報告しません。質問を聞き直してはいけません。")
VARIANTS = {
    "A_ごゆっくり": {"kw": G30_VARIANTS["A_いまの形"], "rule": None},
    "C_ごゆっくり": {"kw": G30_VARIANTS["C_考える量0＋判定0.4秒"], "rule": None},
    "C_黙って待つ": {"kw": G30_VARIANTS["C_考える量0＋判定0.4秒"], "rule": WAIT_SILENTLY},
}


@contextmanager
def thinking_rule(rule: str | None):
    """考えている相手への決まり（instruction.THINKING_RULE）を、この試験の中だけ差し替える。抜けるときに必ず元へ戻す。"""
    saved = instruction.THINKING_RULE
    if rule is not None:
        instruction.THINKING_RULE = rule
    try:
        yield
    finally:
        instruction.THINKING_RULE = saved


def summarize(rows: list[dict]) -> dict:
    s = {}
    for name in VARIANTS:
        rs = [r for r in rows if r.get("variant") == name and "checks" in r]
        pts = [t for r in rs for t in r["pause_turns"]]
        gaps = sorted(t["gap_s"] for r in rs for t in r["turns_measured"] if t["tool_first"] and not t["cut_in"]
                      and not t["callee"].startswith("Puck_pause"))
        s[name] = {"calls": len(rs), "pause_turns": len(pts), "cut_in": sum(t["cut_in"] for t in pts),
                   "said_in_pause": sorted({t["said_in_pause"] for t in pts if t["said_in_pause"]}),
                   "no_reply_after_cut_in": sum(1 for t in pts if t["cut_in"] and not t["followup_replied"]),
                   "values_ok": sum(r["checks"]["q2_ok"] + r["checks"]["q3_ok"] for r in rs), "values": 2 * len(rs),
                   "normal_gap_median": gaps[len(gaps) // 2] if gaps else None,
                   "normal_gap_p90": gaps[int(len(gaps) * 0.9)] if gaps else None}
        for p in PAUSES:
            pp = [t for r in rs if r["pause"] == p for t in r["pause_turns"]]
            s[name][f"cut_in_間{p}秒"] = f"{sum(t['cut_in'] for t in pp)}/{len(pp)}"
    return s


async def run(args) -> dict:
    plans = {p: clips(p) for p in PAUSES}
    if args.dry_run:
        with thinking_rule(WAIT_SILENTLY):
            text = instruction.build(BUSINESS, "S2", opening_mode="recorded")[1]
        return {"dry_run": True, "rule_swapped": WAIT_SILENTLY in text, "restored": instruction.THINKING_RULE != WAIT_SILENTLY}
    client = make_client(pick_key(args))
    rows = []
    for rep in range(args.repeat):
        for vt in args.type or VOICE_TYPES:
            for p in PAUSES:
                q2, q3 = plans[p]
                callee = [W("yes_reserved"), {"wav": q2}, W("affirm"), {"wav": q3}, W("affirm")]
                for name, v in VARIANTS.items():
                    try:
                        with thinking_rule(v["rule"]):
                            call = await run_call(client, args.model, BUSINESS, vt, callee, opening_mode="recorded", use_ear=False,
                                                  save_audio="all", audio_label=f"G32_{name}_{p}_{rep}",
                                                  followup_wait_s=FOLLOWUP_WAIT_S, **v["kw"])
                        row = {"rep": rep, "type": vt, "variant": name, "pause": p, "pause_turns": measure(call, (q2, q3)),
                               "turns_measured": turn_measures(call), "checks": judge(call), **call}
                    except Exception as exc:
                        row = {"rep": rep, "type": vt, "variant": name, "pause": p, "error": describe_error(exc)}
                    rows.append(row)
                    pt = row.get("pause_turns", [])
                    say(f"[{rep}] {vt} {name} 間{p}秒 割り込み={[t['cut_in'] for t in pt]} 返事={[t['followup_replied'] for t in pt]} "
                        f"値={row.get('checks')} 言ったこと={[t['said_in_pause'] for t in pt]} {row.get('error', '')}")
                    save_partial("G-32", {"model": args.model, "summary": summarize(rows), "rows": rows})
                    await asyncio.sleep(args.pause)
    summary = summarize(rows)
    say("まとめ：", json.dumps(summary, ensure_ascii=False))
    return {"model": args.model, "variants": {k: {"kw": v["kw"], "rule": v["rule"]} for k, v in VARIANTS.items()},
            "pauses": PAUSES, "summary": summary, "rows": rows}


def main() -> None:
    require_python()
    p = base_parser(__doc__)
    p.add_argument("--repeat", type=int, default=1)
    p.add_argument("--pause", type=float, default=3.0)
    p.add_argument("--type", choices=sorted(VOICE_TYPES), action="append")
    args = p.parse_args()
    save_result("G-32", asyncio.run(run(args)))
    drop_partial("G-32")


if __name__ == "__main__":
    main()
