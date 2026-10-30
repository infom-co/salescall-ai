"""G-68：取り次ぎで待たせたあとに話した方が、待たせた方が戻ったのか、別の方に代わったのかを、意味の判定で読めるか（Flow.passed_on の手前）。

きっかけ（2026-10-25 日本時間の早朝・J9a）：取り次ぎで待ったあとに話した方には、誰でもあいさつ・名乗りを流し直している（受付の方が「少々お待ち
ください」と言った時点で、次の発話から新しい方と区切る＝Flow._new_speaker）。J9a で流し直した10本のうち7本は「お待たせいたしました。」で
戻った方（取り込んでいて出られない3・代わりにお話をお聞きします4）＝待たせた方と同じ方と見られ、名乗りはもう聞いている。Intent.md 9-4 の
決まりは「相手が替わったら短く名乗り直す」＝替わっていなければ要らない。
案：戻ってきた方の最初の発話を、目的の人の判定・尋ねているかとは別の問い合わせで読み、2つの判定がそろって「待たせた方が戻った」と読んだ
ときだけ、区切りを元へ戻す（Flow.hold_moved。尋ねていた発話の区切りを戻すのと同じ仕組み）。届かない・そろわないときは今までどおり新しい方。
⇒ 数えるのは (1) 別の方の言葉を、2つそろって「戻った」と読んだ回（害の側＝名乗りを聞いていない方に名乗らない。0 でなければ入れない）
   (2) 戻った方の言葉を、2つそろって「戻った」と読めた回（効き目の側）。どちらとも言えない言葉は、どう読んでもよい（数えない）。
正解はここに書いた（作らせたモデルの正解ではない）。回帰と並べて流さない（判定の鍵を取り合う＝CL-1.md 0-2）。
"""
from __future__ import annotations

import asyncio
import json

from common import base_parser, require_python, save_result, say
from meaning_judge import NoJudgment, add_judge_args, make_judges

from flowctl import RETURN_MEANINGS as MEANINGS, RETURNED_OTHER as OTHER, RETURNED_SAME as SAME  # 説明の文の正本（G-68 で測った文）
HOLD_TARGET, HOLD_OTHER = "岡田ですね。少々お待ちください。", "担当の者に代わりますので、少々お待ちください。"
ANY = None  # どちらとも言えない（数えない）
# (待たせた方の言葉, 戻ってきた方の言葉, 正解)
CASES = [
    (HOLD_TARGET, "お待たせいたしました。申し訳ございません、岡田は取り込んでおりまして、ただいま電話に出られないとのことです。", SAME),
    (HOLD_TARGET, "お待たせいたしました。岡田はただいま席を外しておりまして。", SAME),
    (HOLD_TARGET, "お待たせしました。岡田、本日はお休みをいただいておりました。", SAME),
    (HOLD_TARGET, "お待たせいたしました。あいにく岡田は会議中でした。", SAME),
    (HOLD_TARGET, "すみません、お待たせしました。岡田に確認したんですが、今日はもう戻らないそうです。", SAME),
    (HOLD_TARGET, "お待たせいたしました。岡田は外出しておりまして、戻りは夕方になるとのことです。", SAME),
    (HOLD_OTHER, "お待たせいたしました。担当の者があいにく席を外しておりまして。", SAME),
    (HOLD_TARGET, "お待たせいたしました。岡田はおりませんので、私がご用件をお伺いしてもよろしいでしょうか。", SAME),
    (HOLD_TARGET, "お電話代わりました。担当の山田です。あいにく岡田は本日不在にしております。", OTHER),
    (HOLD_TARGET, "お電話代わりました、山田です。岡田は外出しておりまして。", OTHER),
    (HOLD_TARGET, "はい、お電話代わりました。", OTHER),
    (HOLD_TARGET, "お電話代わりました。岡田です。", OTHER),
    (HOLD_TARGET, "お待たせいたしました。お電話代わりました、営業部の田中です。", OTHER),
    (HOLD_TARGET, "もしもし、代わりました。同じ部署の佐々木ですが、岡田は今日お休みです。", OTHER),
    (HOLD_TARGET, "お待たせいたしました、岡田です。", OTHER),
    (HOLD_OTHER, "お電話代わりました。担当の者ですが。", OTHER),
    (HOLD_TARGET, "お待たせいたしました。", ANY),
    (HOLD_TARGET, "お待たせいたしました。岡田はおりませんので、代わりにお話をお聞きします。", ANY),
]


def question():
    """測る問い合わせの質問（測る所 run と、送る文の指紋 prompts_fingerprint の1か所）。"""
    from flowctl import Question
    from phrases import target_call
    return Question("R1", target_call("法人", "おかだ", "営業部部長"), "yes_no", required=False, conditions=tuple(MEANINGS), meanings=dict(MEANINGS))


def prompts_fingerprint() -> tuple[int, str]:
    """判定に送る文（CASES の全部）の長さと指紋（0円）。⛔ 2026-10-31：答えの段の説明の文（1か所）を直したとき、この文も変わったのに
    確かめていなかった（指紋の道具が無かった）＝測った文と同じかを、ここで確かめる"""
    import hashlib
    from meaning_judge import prompt_parts
    q = question()
    s = "".join("".join(prompt_parts(q, text, None, before)) for before, text, _ in CASES)
    return len(s), hashlib.sha256(s.encode()).hexdigest()[:16]


async def run(args) -> dict:
    q = question()
    judges = {k: j for k, j in make_judges(args).items() if k != "grounder" and j is not None}
    rows = []
    for before, text, want in CASES:
        for rep in range(args.repeat):
            for j in {j.panel for j in judges.values()}:
                j.reset()
            row = {"before": before, "text": text, "want": want, "rep": rep}

            async def one(k, j):
                try:
                    got = await j(q, text, None, before=before)
                except NoJudgment as exc:
                    row[k], row[f"{k}_tried"] = None, exc.tried
                    return
                row[k] = str(got) if got else None
                if got is not None and got.by:
                    row[f"{k}_by"], row[f"{k}_sure"] = got.by, got.sure
            await asyncio.gather(*(one(k, j) for k, j in judges.items()))
            row["both_same"] = all(row.get(k) == SAME for k in judges)
            rows.append(row)
            say(text[:40], {k: row.get(k) for k in judges}, "そろって戻った" if row["both_same"] else "")
    harm = [r for r in rows if r["want"] == OTHER and r["both_same"]]
    gain = [r for r in rows if r["want"] == SAME and r["both_same"]]
    summary = {"別の方をそろって戻ったと読んだ（害）": f"{len(harm)}/{sum(1 for r in rows if r['want'] == OTHER)}",
               "戻った方をそろって戻ったと読んだ（効き目）": f"{len(gain)}/{sum(1 for r in rows if r['want'] == SAME)}"}
    summary |= {f"{k}・正しい（どちらとも言えない言葉を除く）": f"{sum(1 for r in rows if r['want'] and r.get(k) == r['want'])}/"
                f"{sum(1 for r in rows if r['want'])}" for k in judges}
    say("数え：", json.dumps(summary, ensure_ascii=False))
    for r in harm:
        say("⛔ 害：", r["text"], {k: r.get(k) for k in judges})
    return {"meanings": MEANINGS, "summary": summary, "rows": rows}


def main() -> None:
    require_python()
    p = base_parser(__doc__)
    add_judge_args(p)
    p.add_argument("--repeat", type=int, default=2)
    p.add_argument("--tag", default="")
    p.add_argument("--fingerprint", action="store_true", help="測らずに、判定に送る文の長さと指紋だけを出す（測った文と同じかを確かめる）")
    args = p.parse_args()
    if args.fingerprint:
        say("判定に送る文：%d 字・指紋 %s" % prompts_fingerprint())
        return
    save_result(f"G-68{args.tag}", asyncio.run(run(args)))


if __name__ == "__main__":
    main()
