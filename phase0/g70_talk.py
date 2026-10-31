"""G-70：答えの段の判定が、前の流れとつながらない返事（会話になっていない）を見分けられるか（meaning_judge.FITS・flowctl.TALK_WHY）。

きっかけ（✅ 2026-10-31 ユーザー）：「『差し控えさせていただきます』だけだと『利用しない』になるのは当然。この部分だけの判断ならAIがあっています。
前の分との流れの回答として会話になっていないだけです。それを分かるようにしないと！」・同じ返事を前の流れとつながる場合・つながらない場合に
並べた組の案に「これだね。Aは会話になっていない。A1は日本語として少しおかしいけど、利用しないと受け取れるということだよ」。
同じ返事を、前の流れとつながる場合（会話になっている＝言葉の意味で受け取る）と、つながらない場合（会話になっていない＝聞き直す）に並べる。
前の流れは、答えの段の判定に渡す「その前に相手が言ったこと」（flowctl.Flow.judge_context の answer_words）と同じ形で渡す。
正解はここに書いた（作らせたモデルの正解ではない）。A の組はご確認済み・B〜F は案（⏳ ご確認待ち）。回帰と並べて流さない（判定の鍵を取り合う）。
"""
from __future__ import annotations

import json
import time

from common import base_parser, load_secret, require_python, save_result, say
from g53_meaning import ASK_AGAIN, flow_act, g48_questions
from meaning_judge import OTHER_JUDGES, OtherJudgeError, judge_other, other_client

# (印, その前にお客様が言ったこと, 質問の番号, 返事, 受け取ってよい読み, 言い切りはどちらでもよいか, 会話になっているか, ご確認)
CASES = [
    ("A1", "当日は電車で伺います。", "G6", "差し控えさせていただきます", ("利用しない",), True, True, True),
    ("A2", "車で伺うので、駐車場を使わせてください。", "G6", "差し控えさせていただきます", (), False, False, True),
    ("B1", "今の業者さんとは来月で解約します。", "G3", "はい、ございます。", ("ある",), False, True, False),
    ("B2", "どこの業者とも契約したことはありません。", "G3", "はい、ございます。", (), False, False, False),
    ("C1", "リースの期間がもうすぐ終わるんです。", "G13", "そうです。", ("はい",), False, True, False),
    ("C2", "複合機は去年買い取りました。", "G13", "そうです。", (), False, False, False),
    ("D1", None, "G3", "差し控えさせていただきます", ("refused",), False, True, False),
    ("D2", "契約の話なら喜んでしますよ。", "G3", "差し控えさせていただきます", (), False, False, False),
    ("E1", "先週、予約の電話をしました。", "G31", "はい。", ("している",), False, True, False),
    ("E2", "まだどこにも予約していないんです。", "G31", "はい、しています。", (), False, False, False),
    ("F1", "その日は空いています。", "G9", "参加します。", ("参加する",), False, True, False),
    ("F2", "その日は出張でおりません。", "G9", "はい、参加します。", (), False, False, False),
]


def wants(accept: tuple, either: bool, talk: bool) -> set[str]:
    """流れの制御がすべきこと（g53_meaning.flow_wants と同じ決め方）：会話になっていなければ聞き直すだけ。"""
    if not talk:
        return {ASK_AGAIN}
    return set().union(*({f"受け取る：{a}", f"念のため確かめる：{a}"} if either else {f"受け取る：{a}"} for a in accept))


def main() -> None:
    require_python()
    p = base_parser(__doc__)
    p.add_argument("--judge", action="append", choices=tuple(OTHER_JUDGES), help="測る判定（いくつでも。既定は d1-ultra と Jev）")
    p.add_argument("--repeat", type=int, default=3)
    p.add_argument("--tag", default="")
    args = p.parse_args()
    names = args.judge or ["d1 Ultra（Liquid）", "Jev 1.14"]
    qmap = {q.id: q for q in g48_questions()}
    clients = {n: other_client(n, load_secret(OTHER_JUDGES[n]["key"], args.env), budget=args.budget_yen) for n in names}
    rows = []
    for mark, before, qid, text, accept, either, talk, checked in CASES:
        q = qmap[qid]
        for rep in range(args.repeat):
            row = {"mark": mark, "before": before, "qid": qid, "question": q.text, "text": text, "accept": list(accept), "either": either,
                   "talk": talk, "checked": checked, "rep": rep}
            for n in names:
                t = time.monotonic()
                try:
                    got = judge_other(clients[n], n, q, text, None, before)
                except OtherJudgeError as exc:
                    row[f"{n}誤り"] = str(exc)[:200]
                    continue
                row[f"{n}秒"] = round(time.monotonic() - t, 3)
                if got is None:
                    continue
                row[n] = str(got)
                row |= {f"{n}{k}": getattr(got, a, None) for k, a in (("確からしさ", "sure"), ("言い切り", "firm"), ("言い切りの確からしさ", "firm_p"),
                                                                     ("会話", "fits"), ("会話の確からしさ", "fits_p"))}
                act = flow_act({"set": "C"}, q, str(got), got.firm, got.fits)
                row[f"{n}動き"], row[f"{n}正しい"] = act, act in wants(accept, either, talk)
            rows.append(row)
            say(mark, text, {n: (row.get(n), row.get(f"{n}会話の確からしさ"), row.get(f"{n}正しい")) for n in names})
    summary = {}
    for n in names:
        for part, pick in (("会話になっていない組", lambda r: not r["talk"]), ("会話になっている組", lambda r: r["talk"]),
                           ("ご確認済み（A）", lambda r: r["checked"])):
            got = [r for r in rows if pick(r) and n in r]
            summary[f"{n}・{part}"] = f"{sum(1 for r in got if r[f'{n}正しい'])}/{len(got)}"
    say("数え：", json.dumps(summary, ensure_ascii=False))
    save_result(f"G-70{args.tag}", {"cases": [list(c) for c in CASES], "summary": summary, "rows": rows})


if __name__ == "__main__":
    main()
