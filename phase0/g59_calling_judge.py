"""G-59：目的の人を呼んでいる段の意味の判定が、取り次ぎ・代わった方・名乗ったご本人を見分けるか（DESIGN.md 4-3・Flow._target_reported）。
⛔ 2026-10-19 G-57 の本番の形：「お電話代わりました。岡田です。」を、いまの判定は「代わった人が話し始めた」と読み、流れの制御は名乗った方に
   「「おかだ」様でいらっしゃいますでしょうか？」と聞き直した（2つ目の判定は「ご本人」）。判定の説明に、名乗った名前がお呼びした方なら
   「ご本人」を足して、同じ言葉で測る。正解はここに書いた（作らせたモデルの正解ではない）。"""
from __future__ import annotations

import asyncio
import json

from common import base_parser, require_python, save_result, say
from meaning_judge import NoJudgment, add_judge_args, make_judges

# (相手の言葉, 受け取ってよい読み)。「岡田ですね。」は、聞き取りが途中で切れた形（G-57：続きの「少々お待ちください。」が次の発話に入った）
LINES = [
    ("お電話代わりました。岡田です。", ["ご本人"]),
    ("お電話代わりました、営業部の岡田です。", ["ご本人"]),
    ("はい、岡田です。", ["ご本人"]),
    ("岡田は私ですが。", ["ご本人"]),
    ("お電話代わりました。", ["代わった人が話し始めた"]),
    ("お電話代わりました。山本です。", ["代わった人が話し始めた", "決められない"]),
    ("岡田ですね。少々お待ちください。", ["取り次ぎを待つ"]),
    ("少々お待ちください。", ["取り次ぎを待つ"]),
    ("岡田は本日休みをいただいております。", ["不在"]),
    ("岡田ですね。", ["決められない", "取り次ぎを待つ"]),
]
# その前に相手が言ったこと（取り次いだ方の言葉）と、そのあとの言葉 ⇒ 受け取ってよい読み（Flow.judge_context の before）
# ✅ 2026-10-19 ユーザー：「受付が『それでは岡田にお繋ぎします。』のような後に、『はい、お電話代わりました。』と言われれば、『ご本人』になりますよね」
TO_TARGET, TO_OTHER = "岡田ですね。少々お待ちください。", "担当の者に代わりますので、少々お待ちください。"
LINES_AFTER = [
    ("はい、お電話代わりました。", ["ご本人"], TO_TARGET),
    ("お電話代わりました。", ["ご本人"], TO_TARGET),
    ("はい、お電話代わりました。", ["ご本人"], "それでは岡田にお繋ぎします。"),
    ("お電話代わりました。岡田です。", ["ご本人"], TO_TARGET),
    ("お電話代わりました。山本です。", ["代わった人が話し始めた", "決められない"], TO_TARGET),
    ("はい、お電話代わりました。", ["代わった人が話し始めた", "決められない"], TO_OTHER),
    ("お電話代わりました。", ["代わった人が話し始めた", "決められない"], "岡田は外出しておりますので、別の者に代わります。"),
    ("岡田は本日休みをいただいております。", ["不在"], TO_TARGET),
    # ⚠️ 2026-10-19 ユーザー（2回目）：「取り次いだ後に、『岡田は取り込んでおり、対応できません』ともう一度受付が言うこともある」
    ("岡田は取り込んでおりまして、対応できかねます。", ["不在"], TO_TARGET),
    ("申し訳ございません、岡田はただいま電話に出られないとのことです。", ["不在"], TO_TARGET),
    ("お待たせいたしました。岡田は会議中でして、本日は難しいようです。", ["不在"], TO_TARGET),
    ("お待たせいたしました。", ["決められない", "代わった人が話し始めた"], TO_TARGET),
]


async def run(args) -> dict:
    from flowctl import Flow, Question, label
    from instruction import Target
    target = Target(name="岡田", reading="おかだ", role="営業部部長")
    from g22_opening import BUSINESS
    import dataclasses
    b = dataclasses.replace(BUSINESS, target=target)
    f = Flow(b.questions, opening=b.replay_parts(), target=b.replay_parts(), play_recordings=True)
    q, asked, _ = f.judge_context()
    judges = make_judges(args)
    rows = []
    for text, ok, before in [(t, o, None) for t, o in LINES] + LINES_AFTER:
        for rep in range(args.repeat):
            row = {"text": text, "accept": ok, "before": before, "rep": rep}
            for k, j in judges.items():
                if j is not None:
                    try:
                        got = await j(q, text, asked, before=before)
                    except NoJudgment as exc:  # 答えにならなかった（Jev の確からしさが線より下・止まっている）：どれも選ばなかったと数える
                        got, row[f"{k}_tried"] = None, exc.tried
                    row[k] = label(got) if got else None
                    if got is not None and got.by:
                        row[f"{k}_by"], row[f"{k}_sure"] = got.by, got.sure
            row["ok"] = all(row.get(k) in ok for k in judges if judges[k] is not None)
            rows.append(row)
            say(f"（前：{before}）" if before else "", text, {k: row.get(k) for k in judges}, "OK" if row["ok"] else "×")
    summary = {k: f"{sum(1 for r in rows if r.get(k) in r['accept'])}/{len(rows)}" for k in judges if judges[k] is not None}
    summary |= {f"{k}（前の言葉つき）": f"{sum(1 for r in rows if r['before'] and r.get(k) in r['accept'])}/{sum(1 for r in rows if r['before'])}"
                for k in judges if judges[k] is not None}
    say("数え：", json.dumps(summary, ensure_ascii=False))
    return {"question": q.text, "summary": summary, "rows": rows}


def main() -> None:
    require_python()
    p = base_parser(__doc__)
    add_judge_args(p)
    p.add_argument("--repeat", type=int, default=3)
    p.add_argument("--tag", default="")
    args = p.parse_args()
    save_result(f"G-59{args.tag}", asyncio.run(run(args)))


if __name__ == "__main__":
    main()
