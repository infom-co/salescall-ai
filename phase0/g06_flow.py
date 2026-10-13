"""G-6・G-7・G-16（の入口）：AIが操作を呼び、プログラムの返事に従って進むか／文字起こしが取れるか。

DESIGN.md 6-1・6-2・6-11 の考え方（会話の位置はプログラムが持つ）が、Gemini Live で成り立つかを確かめる。
流れの制御は flowctl、指示文は instruction、通話の動かし方は convo（どれも1か所）を通す。

台本（相手の発話は文字で送る）：
  Q1「ご予約はされていますか」（はい／いいえ・確かめない型）⇒「はい、予約しています」
  Q2「ご希望の日時をお聞かせください」（日時・確かめる型）⇒ 聞き取りにくい答え ⇒ 聞き返し ⇒「15日の2時です」⇒ 復唱 ⇒「はい」
確かめること：
  (1) record_answer の質問IDが、いまの質問と一致する
  (2) Q2 は、復唱の結果（confirm_field）が届くまで、プログラムが次へ進めない（I-18）
  (3) 聞き取りにくい答えで report_unclear を呼び、決まった言い方で1回だけ聞き返す（6-11）
  (4) 復唱の言葉が、AIの発話の文字起こしに出る（G-7）
"""
from __future__ import annotations

import asyncio

from common import base_parser, describe_error, make_client, pick_key, require_python, save_result, say
from convo import run_call
from flowctl import Question, tool_declarations
from instruction import VOICE_TYPES, Business, build
from live_util import build_config

# 試験の業務設定（名前は「」でくくらずに書く。くくるのは instruction.py だけ）
BUSINESS = Business(
    company="アオバ物流", caller="佐藤", purpose="ご予約の確認",
    questions=[Question("Q1", "ご予約はされていますか。", "yes_no", conditions=("はい", "いいえ")),
               Question("Q2", "ご希望の日時をお聞かせください。", "datetime", conditions=("答えた",))],
)
CALLEE = ["はい、もしもし。", "はい、予約しています。", "えっと、じゅう……の……", "15日の午後2時です。", "はい、そうです。"]


async def run(args) -> dict:
    if args.dry_run:
        voice, text = build(BUSINESS, args.voice_type)
        build_config(voice=voice, system_instruction=text, tools=tool_declarations(BUSINESS.questions))
        return {"dry_run": True, "instruction": text}
    client = make_client(pick_key(args))
    call = await run_call(client, args.model, BUSINESS, args.voice_type, CALLEE, thinking_budget=args.thinking_budget)
    for t in call["turns"]:
        say(f"相手：{t['callee']} ／ AI（別の耳）：{t.get('ear', '')} ／ 判定={t['check']['kind']}")
    checks = {"per_turn": per_turn_checks(call["turns"])}
    checks["all_turns_ok"] = all(c["ok"] for c in checks["per_turn"])
    checks["ask_back_phrase_count"] = "".join(t.get("ear", "") for t in call["turns"]).count("聞き取りにくかった")
    checks["ask_back_at_most_once"] = checks["ask_back_phrase_count"] <= 1
    return {"model": args.model, "voice_type": args.voice_type, "checks": checks, **call}


# 相手の発話ごとに、そのターンの中で起きるべきこと（言葉はどれか1つが出ればよい）
EXPECT = [
    {"callee": CALLEE[0], "tool": None, "say_any": ["予約"]},
    {"callee": CALLEE[1], "tool": "record_answer", "say_any": ["日時", "日にち", "ご希望"]},
    {"callee": CALLEE[2], "tool": "report_unclear", "say_any": ["聞き取りにくかった"]},
    {"callee": CALLEE[3], "tool": "record_answer", "say_any": ["15日"], "must_not_tool": "confirm_field"},  # 言い直して、返事を待つ
    {"callee": CALLEE[4], "tool": "confirm_field", "say_any": ["ありがとう", "失礼"]},
]


def per_turn_checks(turns: list[dict]) -> list[dict]:
    out = []
    for i, exp in enumerate(EXPECT):
        if i >= len(turns):
            out.append({"callee": exp["callee"], "ok": False, "why": "このターンまで進まなかった"})
            continue
        t = turns[i]
        names = [e["name"] for e in t["tool_events"]]
        tool_ok = (not names) if exp["tool"] is None else (exp["tool"] in names)
        # 相手の返事が届く前の確かめの報告は、差し戻されていれば「起きていない」のと同じ（受け付けたかで見る）
        # ⛔ 2026-10-13：通話の記録に無い "events" を見ていたため、この確かめが一度も働いていなかった ⇒ tool_events（中の名前）で見る
        accepted = [e["name"] for e in t["tool_events"] if e.get("reply") != "rejected"]
        if exp.get("must_not_tool") and exp["must_not_tool"] in accepted:
            tool_ok = False
        said = t.get("ear", "")  # 口から出た声（別の耳）で確かめる。AI自身の文字起こしは使わない（G-7）
        say_ok = any(w in said for w in exp["say_any"])
        out.append({"callee": exp["callee"], "ok": tool_ok and say_ok, "tool_ok": tool_ok, "say_ok": say_ok,
                    "expected_tool": exp["tool"], "got_tools": names, "expected_words": exp["say_any"], "said": said})
    return out


def main() -> None:
    require_python()
    p = base_parser(__doc__)
    p.add_argument("--voice-type", choices=sorted(VOICE_TYPES), default="L4")
    p.add_argument("--thinking-budget", type=int, default=None, help="None＝既定（自動）／0＝考えない")
    args = p.parse_args()
    try:
        result = {"ok": True, "thinking_budget": args.thinking_budget, **asyncio.run(run(args))}
    except Exception as exc:
        result = {"ok": False, "error": describe_error(exc)}
    save_result("G-06", result)


if __name__ == "__main__":
    main()
