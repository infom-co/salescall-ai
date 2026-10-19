"""G-59：目的の人を呼んでいる段の意味の判定が、取り次ぎ・代わった方・名乗ったご本人を見分けるか（DESIGN.md 4-3・Flow._target_reported）。
⛔ 2026-10-19 G-57 の本番の形：「お電話代わりました。岡田です。」を、いまの判定は「代わった人が話し始めた」と読み、流れの制御は名乗った方に
   「「おかだ」様でいらっしゃいますでしょうか？」と聞き直した（2つ目の判定は「ご本人」）。判定の説明に、名乗った名前がお呼びした方なら
   「ご本人」を足して、同じ言葉で測る。正解はここに書いた（作らせたモデルの正解ではない）。"""
from __future__ import annotations

import asyncio
import json

from common import base_parser, require_python, save_result, say
from meaning_judge import add_judge_args, make_judges

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


async def run(args) -> dict:
    from flowctl import Flow, Question, label
    from instruction import Target
    target = Target(name="岡田", reading="おかだ", role="営業部部長")
    from g22_opening import BUSINESS
    import dataclasses
    b = dataclasses.replace(BUSINESS, target=target)
    f = Flow(b.questions, opening=b.replay_parts(), target=b.replay_parts(), play_recordings=True)
    q, asked = f.judge_context()
    judges = make_judges(args)
    rows = []
    for text, ok in LINES:
        for rep in range(args.repeat):
            row = {"text": text, "accept": ok, "rep": rep}
            for k, j in judges.items():
                if j is not None:
                    got = await j(q, text, asked)
                    row[k] = label(got) if got else None
            row["ok"] = all(row.get(k) in ok for k in judges if judges[k] is not None)
            rows.append(row)
            say(text, {k: row.get(k) for k in judges}, "OK" if row["ok"] else "×")
    summary = {k: f"{sum(1 for r in rows if r.get(k) in r['accept'])}/{len(rows)}" for k in judges if judges[k] is not None}
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
