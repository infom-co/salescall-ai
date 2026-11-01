"""G-28：AIに見せる言葉が英語か日本語かで、AIが答えの報告を飛ばす誤りが変わるか（2026-10-13・ユーザー指摘「こちらが英語を使っていませんか？」）。

G-25（AIに見せる言葉に英語の名前があった時点のコード）：録音の名乗りの形・間なしで、相手が値を答えたターンの44%（34中15）で、
AIが答えを報告せずに自分で言い直した。G-26（日本語にしたあと）：同じ形で 12中0、ほかの形も合わせて 48中0。
⇒ 原因が「英語の名前」なのか、「時間がたって Google の側のふるまいが変わった」だけなのかを分けるため、同じ時間帯に、
英語の名前の形（EN）と日本語の名前の形（JA）を、1本ずつ交互に動かす。ほかの条件はそろえる（録音の名乗りの形・間なし・同じ台本）。
EN の形は、流れの制御の「AIに見せる言葉の表」（flowctl の *_LABEL）を、この試験の中だけ、中の名前（英字）そのままに置き換えて作る。
依頼主の選択肢も、日本語にする前の形（yes・no・answered）にする。表を元に戻してから、次の通話へ進む。
"""
from __future__ import annotations

import asyncio
import json

import flowctl
from common import base_parser, describe_error, drop_partial, make_client, pick_key, require_python, save_partial, save_result, say
from convo import run_call
from flowctl import Question
from g26_report_skip import CALLEE, judge
from instruction import VOICE_TYPES, Business

_TABLES = ("ARG_LABEL", "SYSTEM_LABEL", "CONFIRM_LABEL", "END_LABEL", "REPLY_KEY_LABEL", "RESULT_LABEL")


def business(lang: str) -> Business:
    yes_no, answered = (("yes", "no"), ("answered",)) if lang == "EN" else (("はい", "いいえ"), ("答えた",))
    return Business(company="株式会社アオバ物流", caller="佐藤", purpose="ご予約の確認", purpose_line="ご予約の確認でお電話いたしました。",
                    questions=[Question("Q1", "ご予約はされていますでしょうか。", "yes_no", conditions=yes_no),
                               Question("Q2", "ご希望の日時をお聞かせください。", "datetime", conditions=answered),
                               Question("Q3", "ご来店の人数をお聞かせください。", "number", conditions=answered)])


class Labels:
    """AIに見せる言葉の表を、この試験の中だけ英字（中の名前そのまま）にする。抜けるときに必ず元へ戻す。"""

    def __init__(self, lang: str):
        self.lang, self.saved = lang, {}

    def __enter__(self):
        if self.lang == "EN":
            for name in _TABLES:
                table = getattr(flowctl, name)
                self.saved[name] = dict(table)
                table.clear()
                table.update({k: k for k in self.saved[name]})
            self._rebuild()
        return self

    def __exit__(self, *exc):
        for name, original in self.saved.items():
            table = getattr(flowctl, name)
            table.clear()
            table.update(original)
        if self.saved:
            self._rebuild()

    @staticmethod
    def _rebuild():
        flowctl._ARG_FROM_LABEL.clear()
        flowctl._ARG_FROM_LABEL.update({v: k for k, v in flowctl.ARG_LABEL.items()})
        for key, table in (("condition_id", flowctl.SYSTEM_LABEL), ("result", flowctl.CONFIRM_LABEL), ("reason", flowctl.END_LABEL)):
            flowctl._FROM_LABEL[key] = {v: k for k, v in table.items()}


def summarize(rows: list[dict]) -> dict:
    out = {}
    for lang in ("EN", "JA"):
        rs = [r for r in rows if r["lang"] == lang and "checks" in r]
        out[lang] = {"calls": len(rs), "answer_turns": sum(r["checks"]["answer_turns"] for r in rs),
                     "skipped": sum(r["checks"]["skipped"] for r in rs), "added_month": sum(r["checks"]["added_month"] for r in rs),
                     "items_ok": sum(r["checks"]["items_ok"] for r in rs), "bad_args": sum(r["checks"]["bad_args"] for r in rs)}
    return out


async def run(args) -> dict:
    if args.dry_run:
        from instruction import build
        with Labels("EN"):
            en = build(business("EN"), "S2", opening_mode="recorded")[1]
        ja = build(business("JA"), "S2", opening_mode="recorded")[1]
        return {"dry_run": True, "en_has_condition_id": "condition_id" in en, "ja_has_condition_id": "condition_id" in ja,
                "restored": flowctl.ARG_LABEL["condition_id"]}
    client = make_client(pick_key(args))
    rows = []
    for rep in range(args.repeat):
        for vt in args.type or VOICE_TYPES:
            for lang in ("EN", "JA"):  # 1本ずつ交互に（時間の影響を偏らせない）
                try:
                    with Labels(lang):
                        call = await run_call(client, args.model, business(lang), vt, CALLEE["recorded"], opening_mode="recorded",
                                              use_ear=False, save_audio="none")
                    row = {"rep": rep, "type": vt, "lang": lang, "checks": judge(call), **call}
                except Exception as exc:
                    row = {"rep": rep, "type": vt, "lang": lang, "error": describe_error(exc)}
                rows.append(row)
                c = row.get("checks", {})
                say(f"[{rep}] {vt} {lang} 報告しなかった={c.get('skipped')}/{c.get('answer_turns')} 月を足した={c.get('added_month')} "
                    f"項目={c.get('items_ok')} {row.get('error', '')}")
                save_partial("G-28", {"model": args.model, "summary": summarize(rows), "rows": rows})
                await asyncio.sleep(args.pause)
    summary = summarize(rows)
    say("まとめ：", json.dumps(summary, ensure_ascii=False))
    return {"model": args.model, "summary": summary, "rows": rows}


def main() -> None:
    require_python()
    p = base_parser(__doc__)
    p.add_argument("--repeat", type=int, default=2)
    p.add_argument("--pause", type=float, default=3.0)
    p.add_argument("--type", choices=sorted(VOICE_TYPES), action="append")
    args = p.parse_args()
    save_result("G-28", asyncio.run(run(args)))
    drop_partial("G-28")


if __name__ == "__main__":
    main()
