"""G-47：こちら側の、読みが決まらない人の名前を、AIが声に出さずに言い換えるか（instruction.unread_rule。2026-10-16）。

きっかけ：取込で読みが決まらない名前のうち、こちら側の人の名前（名乗る担当者のほかの人。例：当日の担当「新垣」＝あらがき／しんがき）は、
相手に聞いても分からない。AIへの指示で「声に出さずに『担当の者』のように言い換える」と渡している（DESIGN.md 6-2）が、守るかを測っていない。
- 業務：G-22 の予約の確認。AIに渡す目的に「当日は担当の新垣がご案内します」を入れる（名乗りと質問は録音で流す形）。
- 相手：「はい、予約しています。当日はどなたが担当されるんですか？」（答えと一緒に、当日の担当を聞く）→「そうなんですね。」→ 日時 → はい。
- 比べる：決まりあり（unread_names に「新垣」）／決まりなし（比べる元。AIが名前を口にするかどうか自体を見る）。
- 見ること：AIの言葉（AI自身の文字）に「新垣」・その読み（あらがき・しんがき）が出たか／相手の問いに答えたか（「担当」と言ったか）／
  Q1・Q2 が取れたか。
⚠️ AI自身の文字は、声と違う文字で書かれることがある（G-5）。名前を言ったかは、文字に「新垣」か読みが出たかで数える（言い換えたかは文字で見る）。
"""
from __future__ import annotations

import asyncio
import json
import re

from common import base_parser, describe_error, drop_partial, make_client, pick_key, require_python, save_partial, save_result, say
from convo import run_call
from flowctl import GOT, normalize_value
from g22_opening import BUSINESS as BASE, W
from g30_latency import keep_call
from g34_ack import FAST, FOLLOWUP_WAIT_S
from instruction import Business

NAME = {"surface": "新垣", "reading": "", "source": "", "candidates": ["あらがき", "しんがき"], "likely": "あらがき"}
SAID_NAME = re.compile(r"新垣|あらがき|しんがき|アラガキ|シンガキ")


def business(rule: bool) -> Business:
    return Business(company=BASE.company, caller=BASE.caller, purpose="ご予約の確認（当日は、担当の新垣がご案内します）",
                    purpose_line=BASE.purpose_line, questions=list(BASE.questions), names=[NAME],
                    unread_names=["新垣"] if rule else [])


SCENES = {"決まりあり": business(True), "決まりなし": business(False)}
# ⚠️ 2026-10-22 の4回目から：答えに添えられた質問に答えたあとは、相手の返事を待ってから次の質問の録音を流す（flowctl.Flow._advance の
# after_answer）⇒ 相手の台詞に返事（「そうなんですね。」）を足した
LINES = [W("ask_staff"), W("i_see"), W("datetime_pm"), W("affirm")]


def measures(call: dict) -> dict:
    it = call["items"]
    said = [(t.get("intended") or "").strip() for t in call["turns"]]
    return {"checks": {"said_name": any(SAID_NAME.search(x) for x in said),
                       "answered": any("担当" in x for x in said),
                       "q1_ok": it["Q1"]["condition"] == "はい" and it["Q1"]["state"] in GOT,
                       "q2_ok": normalize_value("datetime", it["Q2"]["value"]) == "15日の午後2時" and it["Q2"]["state"] == "confirmed"},
            "said": said}


async def run(args) -> dict:
    client = make_client(pick_key(args))
    rows = []
    for rep in range(args.repeat):
        for scene, biz in SCENES.items():
            try:
                call = await run_call(client, args.model, biz, "S4", LINES, opening_mode="recorded", use_ear=False,
                                      save_audio="all", audio_label=f"G47_{scene}_{rep}", followup_wait_s=FOLLOWUP_WAIT_S,
                                      callee_voice="swap", play_recordings=True, **FAST)
                row = keep_call({"rep": rep, "scene": scene}, call, measures)
            except Exception as exc:
                row = {"rep": rep, "scene": scene, "error": describe_error(exc)}
            rows.append(row)
            say(f"[{rep}] {scene}：{row.get('checks')} AIの言葉={[s[:50] for s in row.get('said', [])]} {row.get('error', '')}")
            save_partial("G-47", {"rows": rows})
            await asyncio.sleep(args.pause)
    out = {s: {k: f"{sum(1 for r in rows if r['scene'] == s and r.get('checks', {}).get(k))}/"
                  f"{sum(1 for r in rows if r['scene'] == s and 'checks' in r)}" for k in ("said_name", "answered", "q1_ok", "q2_ok")}
           for s in SCENES}
    say("数え：", json.dumps(out, ensure_ascii=False))
    return {"model": args.model, "summary": out, "rows": rows}


def main() -> None:
    require_python()
    p = base_parser(__doc__)
    p.add_argument("--repeat", type=int, default=5)
    p.add_argument("--pause", type=float, default=3.0)
    args = p.parse_args()
    save_result("G-47", asyncio.run(run(args)))
    drop_partial("G-47")


if __name__ == "__main__":
    main()
