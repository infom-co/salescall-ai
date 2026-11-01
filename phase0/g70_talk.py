"""G-70：前の流れとつながらない返事（会話になっていない）を見分けられるか（前の話の答え meaning_judge.PRIOR をプログラムが比べる＝
flowctl.off_talk・TALK_WHY。判定に会話になっているかを答えさせる軸 FITS は、✅ 2026-10-31 の夜 ユーザー「(a) OK」で外した）。

きっかけ（✅ 2026-10-31 ユーザー）：「『差し控えさせていただきます』だけだと『利用しない』になるのは当然。この部分だけの判断ならAIがあっています。
前の分との流れの回答として会話になっていないだけです。それを分かるようにしないと！」・同じ返事を前の流れとつながる場合・つながらない場合に
並べた組の案に「これだね。Aは会話になっていない。A1は日本語として少しおかしいけど、利用しないと受け取れるということだよ」。
同じ返事を、前の流れとつながる場合（会話になっている＝言葉の意味で受け取る）と、つながらない場合（会話になっていない＝言葉の意味の答えを
「念のため…でよろしかったでしょうか？」で確かめる・答えが無ければ聞き直す。✅ 2026-10-31 ユーザー「(イ)」＝お客様が考えを変えた場合もある）に並べる。
前の流れは、答えの段の判定に渡す「その前に相手が言ったこと」（flowctl.Flow.judge_context の answer_words）と同じ形で渡す。
正解はここに書いた（作らせたモデルの正解ではない）。A の組は 2026-10-31 にご確認済み・B〜F も同じ日にご確認済み（「(a) OK」）。
G の組は、分かれ目の向こう側（同じ日にご確認済み。受け取っても、1回確かめても正しい）。判定の線より下の読み（確かでない）は、外れと分けて数える。
回帰と並べて流さない（判定の鍵を取り合う）。
"""
from __future__ import annotations

import json
import time

from common import base_parser, load_secret, require_python, save_result, say
from flowctl import Question
from g53_meaning import ASK_AGAIN, flow_act, g48_questions
from meaning_judge import OTHER_JUDGES, PRIOR_READERS, OtherJudgeError, judge_other, other_client, prior_asked, read_prior_first, sure_line

# (印, その前にお客様が言ったこと, 質問の番号, 返事, 受け取ってよい読み, 言い切りはどちらでもよいか, 会話になっているか, ご確認)
CASES = [
    ("A1", "当日は電車で伺います。", "G6", "差し控えさせていただきます", ("利用しない",), True, True, True),
    ("A2", "車で伺うので、駐車場を使わせてください。", "G6", "差し控えさせていただきます", ("利用しない",), False, False, True),
    ("B1", "今の業者さんとは来月で解約します。", "G3", "はい、ございます。", ("ある",), False, True, True),
    ("B2", "どこの業者とも契約したことはありません。", "G3", "はい、ございます。", ("ある",), False, False, True),
    ("C1", "リースの期間がもうすぐ終わるんです。", "G13", "そうです。", ("はい",), False, True, True),
    ("C2", "複合機は去年買い取りました。", "G13", "そうです。", ("はい",), False, False, True),
    ("D1", None, "G3", "差し控えさせていただきます", ("refused",), False, True, True),
    ("D2", "契約の話なら喜んでしますよ。", "G3", "差し控えさせていただきます", ("refused",), False, False, True),
    ("E1", "先週、予約の電話をしました。", "G31", "はい。", ("している",), False, True, True),
    ("E2", "まだどこにも予約していないんです。", "G31", "はい、しています。", ("している",), False, False, True),
    ("F1", "その日は空いています。", "G9", "参加します。", ("参加する",), False, True, True),
    ("F2", "その日は出張でおりません。", "G9", "はい、参加します。", ("参加する",), False, False, True),
    # 分かれ目の向こう側（2026-10-31 の昼に足した・同じ日にご確認済み「(a) OK」）：前に言ったことを変えると言って答えた（会話になっている）・いまの言葉が
    #    前に話したことを指している（前に話したことで答えを補う）。受け取っても、1回確かめても正しい（✅ 同じ日の夜 ユーザー：「『それでは、駐車場を
    #    (利用される)(利用されない)ということでよろしいでしょうか？』のような確認を入れる形で良いのではないでしょうか」）
    ("G1", "車で伺うので、駐車場を使わせてください。", "G6", "やっぱり電車で行くことにしたので、利用しません", ("利用しない",), True, True, True),
    ("G2", "駐車場を使わせてください。", "G6", "先ほど申し上げたとおりです", ("利用する",), True, True, True),
]


def wants(q: Question, accept: tuple, either: bool, talk: bool) -> set[str]:
    """流れの制御がすべきこと（g53_meaning.flow_wants と同じ決め方）：会話になっていなければ、言葉の意味の答え（選択肢）を念のため確かめるか、
    聞き直す（答えが選択肢でなければ、聞き直すだけ）。"""
    if not talk:
        return {ASK_AGAIN} | {f"念のため確かめる：{a}" for a in accept if a in q.conditions}
    return set().union(*({f"受け取る：{a}", f"念のため確かめる：{a}"} if either else {f"受け取る：{a}"} for a in accept))


def prior_only(args, names, qmap, clients) -> None:
    """前の話の答えだけを、前の話のある組で問い合わせる（線より下の選び＝prior_raw と確からしさも残す）。正解の前の話の答えは、会話になっていない
    組では、いまの言葉の答えと違う選択肢（前の話と逆）・会話になっている組では、受け取る読みと同じか「決まらない」。"""
    from meaning_judge import _send_other, other_text, prior_request, read_prior
    rows = []
    for mark, before, qid, text, accept, either, talk, checked in CASES:
        if not before or (args.only and mark not in args.only):
            continue
        q = qmap[qid]
        for rep in range(args.repeat):
            row = {"mark": mark, "before": before, "qid": qid, "talk": talk, "accept": list(accept), "rep": rep}
            for n in names:
                try:
                    got = read_prior(q, other_text(n, _send_other(clients[n], n, prior_request(n, q, before))))
                except OtherJudgeError as exc:
                    row[f"{n}誤り"] = str(exc)[:200]
                    continue
                row[f"{n}前の話"], row[f"{n}前の話の確からしさ"], row[f"{n}前の話の選び（線より下も）"] = got
            rows.append(row)
            say(mark, before, {n: (row.get(f"{n}前の話の選び（線より下も）"), row.get(f"{n}前の話の確からしさ")) for n in names})
    save_result(f"G-70prior_only{args.tag}", {"rows": rows})


def main() -> None:
    require_python()
    p = base_parser(__doc__)
    p.add_argument("--judge", action="append", choices=tuple(OTHER_JUDGES), help="測る判定（いくつでも。既定は d1-ultra と Jev）")
    p.add_argument("--repeat", type=int, default=3)
    p.add_argument("--tag", default="")
    p.add_argument("--only", action="append", help="測る組の印だけ（いくつでも。例 G1）")
    p.add_argument("--reps", type=int, nargs="*", help="測る回り（0 から。--only と使う＝前の測りで抜けた回り）")
    p.add_argument("--prior-only", action="store_true", help="前の話の答えの問い合わせだけを送る（線を決めるため。線より下の選びも残す）")
    args = p.parse_args()
    names = args.judge or ["d1 Ultra（Liquid）", "Jev 1.14"]
    qmap = {q.id: q for q in g48_questions()}
    clients = {n: other_client(n, load_secret(OTHER_JUDGES[n]["key"], args.env), budget=args.budget_yen) for n in names}
    if args.prior_only:
        return prior_only(args, names, qmap, clients)
    rows = []
    for mark, before, qid, text, accept, either, talk, checked in CASES:
        if args.only and mark not in args.only:
            continue
        q = qmap[qid]
        for rep in (args.reps if args.reps is not None and args.only else range(args.repeat)):
            row = {"mark": mark, "before": before, "qid": qid, "question": q.text, "text": text, "accept": list(accept), "either": either,
                   "talk": talk, "checked": checked, "rep": rep}
            # 前の話の答えは、組と同じく決めた順（PRIOR_READERS＝d1-ultra ⇒ Jev）の1か所で読み、どの判定の読みにも同じものを添える（JudgePanel.ask）
            shared = (read_prior_first({n: clients[n] for n in PRIOR_READERS if n in clients}, q, before) if prior_asked(q, None, before)
                      else (None, None, None))
            row["前の話の答え（組の1か所）"] = list(shared)
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
                got.prior, got.prior_p, got.prior_raw = shared
                row[n] = str(got)
                row |= {f"{n}{k}": getattr(got, a, None) for k, a in (("確からしさ", "sure"), ("言い切り", "firm"), ("言い切りの確からしさ", "firm_p"),
                                                                     ("前の話", "prior"), ("前の話の確からしさ", "prior_p"),
                                                                     ("前の話の選び（線より下も）", "prior_raw"))}
                act = flow_act({"set": "C"}, q, str(got), got.firm, got.prior)
                row[f"{n}動き"], row[f"{n}正しい"] = act, act in wants(q, accept, either, talk)
                # 答えの確からしさが判定の線より下の読みは、本番では使わず次の判定へ回す（確かでない）＝外れとは分けて数える（✅ 同じ日の夜
                # ユーザー（D2 について）：「そもそも、会話が成り立っていないですよね。そりゃ、判断できないんじゃないですか。」）
                row[f"{n}確かでない"] = got.sure is not None and got.sure < sure_line(n, q)
            rows.append(row)
            say(mark, text, {n: (row.get(n), row.get(f"{n}確からしさ"), row.get(f"{n}前の話"), row.get(f"{n}正しい")) for n in names})
    summary = {}
    for n in names:
        sure_rows = [r for r in rows if n in r and not r.get(f"{n}確かでない")]
        summary[f"{n}・確かな読み（線から上）の正しい動き"] = f"{sum(1 for r in sure_rows if r[f'{n}正しい'])}/{len(sure_rows)}"
        summary[f"{n}・確かでない（線より下＝次の判定へ）"] = sum(1 for r in rows if r.get(f"{n}確かでない"))
        for part, pick in (("会話になっていない組", lambda r: not r["talk"]), ("会話になっている組", lambda r: r["talk"]),
                           ("ご確認済み", lambda r: r["checked"]), ("案（ご確認待ち）", lambda r: not r["checked"])):
            got = [r for r in rows if pick(r) and n in r]
            summary[f"{n}・{part}"] = f"{sum(1 for r in got if r[f'{n}正しい'])}/{len(got)}"
    say("数え：", json.dumps(summary, ensure_ascii=False))
    save_result(f"G-70{args.tag}", {"cases": [list(c) for c in CASES], "summary": summary, "rows": rows})


if __name__ == "__main__":
    main()
