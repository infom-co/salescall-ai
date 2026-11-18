"""G-45：読みが決まらない名前を、会話の中で確かめられるか（flowctl.reading_question。2026-10-16 ユーザー指示）。

✅ ユーザー：「日本語の曖昧な漢字、例えばなかじまとなかしまなどは人が見ても判断できません。それは会話の中で失礼のないように語るか、
   場合によっては聞き出すというような手法がいいでしょう。例えば、『お名前はやまざき様でよろしかったでしょうか』や『お名前を確認させて
   いただきたいのですが、ヤマザキ様とお呼びしたらよろしいでしょうか？』などというような聞き方です」
- 業務：Q1（予約）→ N1（読みの確かめ。「中島」をまず「なかじま」で確かめる）→ Q2（日時）。名乗りと最初の質問は録音で流す形。
- 場面：「そのまま」＝相手が「はい、そうです。」と認める（読み＝なかじま）／「読みを直す」＝相手が「いや、ナカシマです。」と直し、
  言い直しにも「はい、そうです。」（読み＝なかしま）／「名前だけ言い直す」＝打ち消しの言葉を言わずに「ナカシマです。」。
- 見ること：(1) N1 が確認済みで、読みが場面どおりか (2) AIが確かめの言い方を言ったか（名前を言い違えていないかは、AIの声の文字で見る）
  (3) 直す場面で、AIが訂正の読みを ひらがなで報告できたか（漢字で報告すると差し戻す＝差し戻しの数） (4) Q1・Q2 も取れたか。
相手の声の材料：make_callee_samples の correct_nakashima（Leda）。会話のAIは S4（お客役は Leda）。
"""
from __future__ import annotations

import asyncio
import json

from common import base_parser, describe_error, drop_partial, make_client, pick_key, require_python, save_partial, save_result, say
from convo import run_call
from flowctl import GOT, Question, normalize_value, reading_question
from g22_opening import BUSINESS as BASE, W
from g30_latency import keep_call
from g34_ack import FAST, FOLLOWUP_WAIT_S
from instruction import Business

BUSINESS = Business(company=BASE.company, caller=BASE.caller, purpose=BASE.purpose, purpose_line=BASE.purpose_line,
                    questions=[BASE.questions[0], reading_question("N1", "中島", "なかじま", ["なかじま", "なかしま"]),
                               Question("Q2", BASE.questions[1].text, "datetime", conditions=("答えた",), topic=BASE.questions[1].topic)])
SCENES = {"そのまま": ([W("yes_reserved"), W("affirm"), W("datetime_pm"), W("affirm")], "なかじま"),
          "読みを直す": ([W("yes_reserved"), W("correct_nakashima"), W("affirm"), W("datetime_pm"), W("affirm")], "なかしま"),
          "名前だけ言い直す": ([W("yes_reserved"), W("name_only_nakashima"), W("affirm"), W("datetime_pm"), W("affirm")], "なかしま")}


def measures(call: dict, want: str) -> dict:
    it = call["items"]
    log = call["flow_log"]
    n1 = [e for e in log if e["args"].get("question_id") == "N1"]
    said = [(t.get("intended") or "").strip() for t in call["turns"]]
    return {"checks": {"q1_ok": it["Q1"]["condition"] == "はい" and it["Q1"]["state"] in GOT,
                       "n1_ok": it["N1"]["state"] == "confirmed" and it["N1"]["value"] == want,
                       "n1": (it["N1"]["state"], it["N1"]["value"]),
                       "q2_ok": normalize_value("datetime", it["Q2"]["value"]) == "15日の午後2時" and it["Q2"]["state"] == "confirmed",
                       "n1_rejected": sum(1 for e in n1 if e["reply"]["result"] == "rejected"),
                       "rejected": sum(1 for e in log if e["reply"]["result"] == "rejected")},
            "n1_reports": [(e["op"], e["args"], e["reply"]["result"]) for e in n1],
            "said": said, "says": [e["reply"].get("say") for e in log if e["reply"].get("say")],
            "acks": [e["reply"].get("ack") for e in log if e["reply"].get("ack")]}


async def run(args) -> dict:
    client = make_client(pick_key(args))
    rows = []
    for rep in range(args.repeat):
        for scene, (lines, want) in SCENES.items():
            try:
                call = await run_call(client, args.model, BUSINESS, "S4", lines, opening_mode="recorded", use_ear=False,
                                      save_audio="all", audio_label=f"G45_{scene}_{rep}", followup_wait_s=FOLLOWUP_WAIT_S,
                                      callee_voice="swap", **FAST)
                row = keep_call({"rep": rep, "scene": scene}, call, lambda c: measures(c, want))
            except Exception as exc:
                row = {"rep": rep, "scene": scene, "error": describe_error(exc)}
            rows.append(row)
            say(f"[{rep}] {scene}：{row.get('checks')} AIの声={[s[:40] for s in row.get('said', [])]} {row.get('error', '')}")
            save_partial("G-45", {"rows": rows})
            await asyncio.sleep(args.pause)
    ok = {s: f"{sum(1 for r in rows if r['scene'] == s and r.get('checks', {}).get('n1_ok'))}/{sum(1 for r in rows if r['scene'] == s)}"
          for s in SCENES}
    say("読みの確かめが場面どおりに済んだ：", json.dumps(ok, ensure_ascii=False))
    return {"model": args.model, "summary": ok, "rows": rows}


def main() -> None:
    require_python()
    p = base_parser(__doc__)
    p.add_argument("--repeat", type=int, default=3)
    p.add_argument("--pause", type=float, default=3.0)
    args = p.parse_args()
    save_result("G-45", asyncio.run(run(args)))
    drop_partial("G-45")


if __name__ == "__main__":
    main()
