"""G-31：相手が言葉の途中で間をあけたとき、AIが割り込むか（話し終わりの判定の速さ × 間の長さ）。DESIGN.md 6-15。

G-30：「えーとですね、」のあと約1秒あけて「3人です。」と言う相手に、判定が既定の形を含むどの形でも、AIは間の中で「ごゆっくりどうぞ。」
と言った（指示文どおり）。本物の電話では、相手が続きを話し始めたところに、AIの声がかぶる。話し終わりの判定を速くすると、間を縮められる
（G-30）が、割り込みは増えうる。⇒ 間の長さを変えて、どの判定の速さで、どの長さの間から割り込むかを数える。
比べる形：A_いまの形（判定は既定）／C_判定0.4秒／D_判定0.2秒（C・D は考える量0。G-30 と同じ）。
相手：Q2「えーとですね、」＋間＋「15日の午後2時でお願いします。」、Q3「えーとですね、」＋間＋「3人です。」。間は1本の通話の中で同じ長さ。
見ること（間のある答えのターンごと）：
- 割り込み：相手が言い終える前に、AIが話し出した（相手の声を送り終えた直後に、AIの声がもうできていた）
- 割り込んだときに言ったこと（「ごゆっくりどうぞ」か、別のことか）
- 割り込んだあと、相手の続きの言葉に返事をしたか（10秒まで待つ。しなければ、相手は黙って待たされた）
- 値が正しく取れたか
"""
from __future__ import annotations

import asyncio
import json

from common import base_parser, describe_error, drop_partial, make_client, pick_key, require_python, save_partial, save_result, say
from convo import CUT_IN_S, run_call
from g22_opening import BUSINESS, W
from g30_latency import VARIANTS as G30_VARIANTS
from g30_latency import OLD_THINKING_RULE, keep_call, thinking_rule
from instruction import VOICE_TYPES
from stitch_gap import join_with_pause

PAUSES = (0.4, 0.7, 1.0, 1.5)
VARIANTS = {k: v for k, v in G30_VARIANTS.items() if k in ("A_いまの形", "C_考える量0＋判定0.4秒", "D_考える量0＋判定0.2秒")}
FOLLOWUP_WAIT_S = 10.0


def clips(pause: float) -> tuple[str, str]:
    q2, q3 = f"Puck_pause{int(pause * 1000)}_datetime.wav", f"Puck_pause{int(pause * 1000)}_three.wav"
    join_with_pause("Puck_filler_eto.wav", pause, "Puck_datetime_pm.wav", q2)
    join_with_pause("Puck_filler_eto.wav", pause, "Puck_three_people.wav", q3)
    return q2, q3


def measure(call: dict, pause_wavs: tuple[str, str]) -> list[dict]:
    """間のある答えのターンごとの結果。"""
    out = []
    turns = call["turns"]
    for k, t in enumerate(turns):
        c = t.get("callee")
        if not (isinstance(c, dict) and c.get("wav") in pause_wavs):
            continue
        cut = t.get("first_audio_after_s") is not None and t["first_audio_after_s"] < CUT_IN_S
        follow = turns[k + 1] if k + 1 < len(turns) and isinstance(turns[k + 1].get("callee"), dict) \
            and turns[k + 1]["callee"].get("followup_of") == c else None
        out.append({"wav": c["wav"], "cut_in": cut, "said_in_pause": (t.get("intended") or "")[:40] if cut else None,
                    "followup_replied": (follow is not None and not follow.get("timed_out") and bool(follow.get("intended") or follow.get("tool_events")))
                    if cut else None,
                    "followup_said": (follow.get("intended") or "")[:40] if follow else None})
    return out


def judge(call: dict) -> dict:
    it = call["items"]
    return {"q2_ok": it["Q2"]["value"] == "15日の午後2時" and it["Q2"]["state"] == "confirmed",
            "q3_ok": it["Q3"]["value"] == "3人" and it["Q3"]["state"] == "confirmed"}


def summarize(rows: list[dict]) -> dict:
    s = {}
    for name in VARIANTS:
        for p in PAUSES:
            rs = [r for r in rows if r.get("variant") == name and r.get("pause") == p and "checks" in r]
            ts = [t for r in rs for t in r["pause_turns"]]
            s[f"{name}／間{p}秒"] = {"turns": len(ts), "cut_in": sum(t["cut_in"] for t in ts),
                                    "no_reply_after_cut_in": sum(1 for t in ts if t["cut_in"] and not t["followup_replied"]),
                                    "values_ok": sum(r["checks"]["q2_ok"] + r["checks"]["q3_ok"] for r in rs), "values": 2 * len(rs)}
    return s


async def run(args) -> dict:
    plans = {p: clips(p) for p in PAUSES}
    if args.dry_run:
        return {"dry_run": True, "variants": list(VARIANTS), "clips": plans}
    client = make_client(pick_key(args))
    rows = []
    for rep in range(args.repeat):
        for vt in args.type or VOICE_TYPES:
            for p in PAUSES:
                q2, q3 = plans[p]
                callee = [W("yes_reserved"), {"wav": q2}, W("affirm"), {"wav": q3}, W("affirm")]
                for name, kw in VARIANTS.items():
                    try:
                        with thinking_rule(OLD_THINKING_RULE):  # G-31 は前の決まりで測った（g30_latency）
                            call = await run_call(client, args.model, BUSINESS, vt, callee, opening_mode="recorded", use_ear=False,
                                                  save_audio="all", audio_label=f"G31_{name}_{p}_{rep}", followup_wait_s=FOLLOWUP_WAIT_S,
                                                  **kw)
                    except Exception as exc:
                        call, row = None, {"rep": rep, "type": vt, "variant": name, "pause": p, "error": describe_error(exc)}
                    if call is not None:
                        row = keep_call({"rep": rep, "type": vt, "variant": name, "pause": p}, call,
                                        lambda c: {"pause_turns": measure(c, (q2, q3)), "checks": judge(c)})
                    rows.append(row)
                    pt = row.get("pause_turns", [])
                    say(f"[{rep}] {vt} {name} 間{p}秒 割り込み={[t['cut_in'] for t in pt]} 返事={[t['followup_replied'] for t in pt]} "
                        f"値={row.get('checks')} 言ったこと={[t['said_in_pause'] for t in pt]} {row.get('error', '')}")
                    save_partial("G-31", {"model": args.model, "summary": summarize(rows), "rows": rows})
                    await asyncio.sleep(args.pause)
    summary = summarize(rows)
    say("まとめ：", json.dumps(summary, ensure_ascii=False))
    return {"model": args.model, "variants": VARIANTS, "pauses": PAUSES, "summary": summary, "rows": rows}


def main() -> None:
    require_python()
    p = base_parser(__doc__)
    p.add_argument("--repeat", type=int, default=1)
    p.add_argument("--pause", type=float, default=3.0)
    p.add_argument("--type", choices=sorted(VOICE_TYPES), action="append")
    args = p.parse_args()
    save_result("G-31", asyncio.run(run(args)))
    drop_partial("G-31")


if __name__ == "__main__":
    main()
