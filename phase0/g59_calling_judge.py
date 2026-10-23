"""G-59：目的の人を呼んでいる段の意味の判定が、取り次ぎ・代わった方・名乗ったご本人を見分けるか（DESIGN.md 4-3・Flow._calling_reported）。
⛔ 2026-10-19 G-57 の本番の形：「お電話代わりました。岡田です。」を、いまの判定は「代わった人が話し始めた」と読み、流れの制御は名乗った方に
   「「おかだ」様でいらっしゃいますでしょうか？」と聞き直した（2つ目の判定は「ご本人」）。判定の説明に、名乗った名前がお呼びした方なら
   「ご本人」を足して、同じ言葉で測る。正解はここに書いた（作らせたモデルの正解ではない）。"""
from __future__ import annotations

import asyncio
import json

from common import base_parser, require_python, save_result, say
from meaning_judge import NoJudgment, add_judge_args, make_judges

# 質問の言葉の、目的の人の判定で受け取ってよい読み（下の LINES の注を参照）
ON_QUESTION = ["取り次ぎを待つ", "代わった人が話し始めた", "決められない", "わからない"]
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
    # こちらへの質問（✅ 2026-10-22 ユーザー：「相手が喋っていても、それが質問だった場合の対処は？」）。質問かは別の問い合わせ（ASKING・下）が
    # 読む。目的の人の判定は「ご本人」でも「不在」でもなければよい（ご本人＝この方の読みとして残り、答えたあと、AIの読みとそろえば繋ぐ側・
    # 不在＝答えたあと、かけ直しへ進む。取り次ぎ・代わった人は、尋ねた発話なら区切りを動かさないか、尋ねた方を新しい方として名乗りから流すだけ）
    ("岡田ですね。失礼ですが、どちら様でしょうか？", ON_QUESTION),
    ("どういったご用件でしょうか？", ON_QUESTION),
    ("どちらの岡田でしょうか？", ON_QUESTION),
    ("あいにく岡田は外出しております。失礼ですが、どちら様でしょうか？", ["不在"]),
    # 不在を伝えてから、別の方に取り次ぐ（流れの制御は「取り次ぎを待つ」が1つでもあれば黙って待つ＝どちらでもよい）
    (AWAY_TO_OTHER := "岡田は外出しておりますので、別の者に代わります。少々お待ちください。", ["不在", "取り次ぎを待つ"]),
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
    # ✅ 2026-10-21 ユーザー：「担当の者に代わりますの後に、はい、お電話変わりましたと出れば、ほぼ『本人』でしょう」（「代わった人」でも正しい）
    ("はい、お電話代わりました。", ["ご本人", "代わった人が話し始めた", "決められない"], TO_OTHER),
    ("お電話代わりました。", ["代わった人が話し始めた", "決められない"], "岡田は外出しておりますので、別の者に代わります。"),
    ("岡田は本日休みをいただいております。", ["不在"], TO_TARGET),
    # ⚠️ 2026-10-19 ユーザー（2回目）：「取り次いだ後に、『岡田は取り込んでおり、対応できません』ともう一度受付が言うこともある」
    ("岡田は取り込んでおりまして、対応できかねます。", ["不在"], TO_TARGET),
    ("申し訳ございません、岡田はただいま電話に出られないとのことです。", ["不在"], TO_TARGET),
    ("お待たせいたしました。岡田は会議中でして、本日は難しいようです。", ["不在"], TO_TARGET),
    ("お待たせいたしました。", ["決められない", "代わった人が話し始めた"], TO_TARGET),
    # 違うお名前の方が出て、お呼びした方がいないとも言った（✅ 2026-10-22 ユーザー：「違う名乗りの方が、岡田はいないと言っても、また、岡田様を
    # 呼ぶんですか？」）⇒ 不在（答え）を選ぶ。「代わった人」を選ぶと、流れの制御は岡田様をお呼びし直す
    ("お電話代わりました。担当の山田です。岡田は本日不在にしております。", ["不在"], TO_OTHER),
    ("お電話代わりました、山田です。あいにく岡田は外出しておりまして。", ["不在"], None),
    # 受付の方が不在を伝えてから代わった別の方：お呼びした方はいない（前の言葉）⇒ 不在（かけ直しの申し出へ）。「代わった人」を選ぶと、
    # 流れの制御は岡田様をお呼びし直す（⚠️ 2026-10-22 の見直しで見つけた形。不在の読みは、電話口の方ごとに数えている）
    ("お電話代わりました。山田です。", ["不在"], AWAY_TO_OTHER),
]
# 目的の人かを確かめたあとの返事（判定に、確かめた言い方を添える＝Flow.judge_context の asked）⇒ 受け取ってよい読み。
# 2026-10-21 ユーザー：「二つ以上揃わなかった場合に関しても正しく設計できているんですか？」で足した（確かめに「いいえ」と答えた方を
# 「ご本人」と読めば、AIの読み違いと重なったときに別の方に繋ぐ＝Flow._calling_reported）
NOT_TARGET = ["別の方", "代わった人が話し始めた", "決められない"]  # どれでも繋がない（Flow._calling_reported）。⛔ 不在は誤り（かけ直しへ進む）
LINES_CONFIRMED = [
    ("はい、そうです。", ["ご本人"], None),
    ("はい、私です。", ["ご本人"], None),
    ("はい、そうです。", ["ご本人"], TO_TARGET),
    ("いいえ、私は山田と申します。", NOT_TARGET, None),
    ("いいえ、私は山田と申します。", NOT_TARGET, "お電話代わりました。担当の山田です。"),
    ("いえ、違います。", NOT_TARGET, None),
    ("いえ、受付の者です。少々お待ちください。", ["取り次ぎを待つ"], None),
    ("いえ、岡田は席を外しております。", ["不在"], None),
    ("いいえ、山田と申します。岡田は本日おりません。", ["不在"], None),
    ("えっと、どちら様でしょうか？", NOT_TARGET + ON_QUESTION, None),
]
# こちらに尋ねている言葉（呼んでいる段の、別の問い合わせ Flow.asks_context が「尋ねている」と読むべきもの。ほかの言葉は全部「尋ねていない」）。
# ⛔ 2026-10-22：目的の人の判定の選択肢に「こちらに聞いている」を足す形は、質問は読めたが「お待たせいたしました。」を Sonnet が3回とも
#    「ご本人」と読むなど、ほかの言葉の読み方まで変わった（3.5 Flash Lite 103/111・Sonnet 102/111）＝取り下げて、別の問い合わせにした
ASKING = {"岡田ですね。失礼ですが、どちら様でしょうか？", "どういったご用件でしょうか？", "どちらの岡田でしょうか？",
          "あいにく岡田は外出しております。失礼ですが、どちら様でしょうか？", "えっと、どちら様でしょうか？"}


def cases(confirm: str) -> list[tuple[str, list[str], str | None, str | None]]:
    """測る言葉の全部：(相手の言葉, 受け取ってよい読み, その前に相手が言ったこと, 確かめた言い方)。"""
    return ([(t, o, None, None) for t, o in LINES] + [(t, o, b, None) for t, o, b in LINES_AFTER]
            + [(t, o, b, confirm) for t, o, b in LINES_CONFIRMED])


async def run(args) -> dict:
    from flowctl import ASKS_YES, Flow, Question, label, unsure_counts
    from instruction import Target
    target = Target(name="岡田", reading="おかだ", role="営業部部長")
    from g22_opening import BUSINESS
    import dataclasses
    b = dataclasses.replace(BUSINESS, target=target)
    f = Flow(b.questions, opening=b.replay_parts(), target=b.replay_parts(), play_recordings=True)
    judges = {k: j for k, j in make_judges(args).items() if k != "grounder"}  # 測るのは意味の判定だけ（中身の判定は g63_grounding）
    rows = []
    for text, ok, before, asked in cases(b.target.confirm()):
        q = f.confirm_questions["target_confirm"] if asked else f.calling_question  # 確かめたあとの返事は、確かめる一言を質問にする（Flow.judge_context）
        for rep in range(args.repeat):
            for j in {j.panel for j in judges.values() if j is not None}:
                j.reset()  # 言葉ごとに測る（前の言葉の時間切れで、この言葉の問い合わせを飛ばさない）
            want = text in ASKING
            row = {"text": text, "accept": ok, "asks_accept": want, "before": before, "asked": asked, "rep": rep}
            async def one(k, j, col, question):
                try:
                    got = await j(question, text, None, before=before)
                except NoJudgment as exc:  # 答えにならなかった（Jev の確からしさが線より下・止まっている）：どれも選ばなかったと数える
                    row[f"{col}_tried"] = exc.tried
                    # 確からしさが線より下の読みは、本番と同じに数える（flowctl.unsure_counts＝Flow.on_unsure と同じ決まり）
                    unsure = next((t["unsure"] for t in exc.tried if "unsure" in t), None)
                    counted = unsure_counts("judge" if col == k else "asks", unsure) if unsure is not None else None
                    row[col] = label(counted) if counted else None
                    return
                row[col] = label(got) if got else None
                if got is not None and got.by:
                    row[f"{col}_by"], row[f"{col}_sure"] = got.by, got.sure
            # 目的の人の判定と、こちらに尋ねているかの問い合わせ（本番と同じに、同じ言葉・同じ前の言葉で同時に出す＝convo.check_handler）
            await asyncio.gather(*(one(k, j, col, question) for k, j in judges.items() if j is not None
                                   for col, question in ((k, q), (f"{k}_asks", f.asks_questions[q.id]))))
            # 尋ねているかは「尋ねている」だけが流れを変える（Flow._asking_now）＝ほかの読みは全部「尋ねていない」と数える
            row["ok"] = all(row.get(k) in ok and (row.get(f"{k}_asks") == ASKS_YES) == want for k in judges if judges[k] is not None)
            rows.append(row)
            say(f"（前：{before}）" if before else "", text, {k: (row.get(k), row.get(f"{k}_asks")) for k in judges}, "OK" if row["ok"] else "×")
    summary = {k: f"{sum(1 for r in rows if r.get(k) in r['accept'])}/{len(rows)}" for k in judges if judges[k] is not None}
    summary |= {f"{k}（尋ねているか）": f"{sum(1 for r in rows if (r.get(f'{k}_asks') == ASKS_YES) == r['asks_accept'])}/{len(rows)}"
                for k in judges if judges[k] is not None}
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
