"""G-72：選択肢ごとの「いまの話の向きを伺う言葉」と「こちらがすることの言葉」を、取込と同じ形（meanings.by_model・check・decide）で作る（2026-11-02）。

✅ 2026-11-01 日本時間22時31分 ユーザー：「「もういいです。」のパターンでも、「パンフレットはお送りしない方がいいということでしょうか？」の方がよくないですか？」
✅ 2026-11-01 日本時間20時56分・23時59分 ユーザー：「…「それではパンフレットをお送りさせていただきますね？」の方がいいんじゃないですか？と言いましたよね。」
✅ 2026-11-02 日本時間1時22分 ユーザー：「「はい、それではパンフレットの送付は控えさせていただきますね？」の方がいいんじゃないですか！」
✅ 2026-11-01 日本時間23時04分 ユーザー：(b)「取込の意味の言葉を確かめる試し：仮（デフォルト）で「詳しいご案内のお電話を…」に替えてよい。」
- 材料：答えの段の測りの質問の1か所（g53_meaning.question_map）の選択肢の質問と、電話を終える言葉の試しの表（end_words）の質問。
  表にしか無い質問（詳しいご案内のお電話・ご訪問）は、意味の言葉（meaning）と事実を伺う質問か（facts.decide）も作る。保存してある測りの質問の
  意味の言葉は作り直さない（保存した答えの正解が、その言葉と文に結びついている）。
- 作る言葉：事実を伺う質問ではない質問だけ（事実を伺う質問は「それでは（意味の言葉）ということで（依頼者がすること）…」の形で、この2つは使わない）。
- 見ること：(1) 2つのモデルの言葉がどちらも確かめに通り、向き（打ち消しか）がそろうか（decide はそのときだけ使う）(2) ⛔ 向きの取り違え・
  質問に無いことを足した言葉を CL-1 が1つずつ見る（BAD に書いて数え直す）。作った言葉はユーザーに全部お見せする。
無料の枠（Gemini の Flash）で、言葉の種類ごとに1回の問い合わせでまとめて答えさせる（0円）。上限に当たったら次のキーで続ける（common.KeyRing）。
"""
from __future__ import annotations

import json

from common import KeyRing, base_parser, describe_error, drop_partial, require_python, save_partial, save_result, say
from flowctl import CHOICE_TYPES

MODELS = ("3.8 Flash", "3.7 Flash")
KINDS = ("doubt", "do")
# 作り手が混んでいる間の代わり（--stand-in。G-73 と同じ考え）。✅ 2026-11-03 ユーザー：「3.8 Flash の分の一部の場面もHaikuで作らせたらいいんじゃない？
# 何度も言わせるな！」・同じ朝（G-72 で Gemini の Flash 2つが時間切れ 504 を続けたのに待ち続けた）「Haikuでもいいと言いましたよね。」
STAND_INS = {"3.8 Flash": "Claude Haiku 5.5", "3.7 Flash": "Claude Haiku 5.5"}
# 目的から遠ざかる向きの選択肢（決め方が違う＝meanings.decide_away。✅ 2026-11-03 ユーザー (2)「OK」）。⛔ 2026-11-03 日本時間8時10分 ユーザー：「今の一言や案は
# 全くダメです。あなたはチェックしたのですか？いい加減な提案をするな！！」＝それまでは「目的につなげて伺う言葉」（keep）を作らせていた ⇒ 向きだけ
AWAY = "away"
# ⛔ 取り違え・質問に無いことを足した言葉（CL-1 が1つずつ見て書く）：(言葉の種類, 質問の文, 選択肢, 言葉)
BAD: set[tuple[str, str, str, str]] = {
    # 1回目（作らせる文の決まりが申し出の形だけだった・向きをモデルどうしでしか比べていなかった）
    ("do", "お急ぎではございませんか？", "はい", "お急ぎと"), ("do", "お急ぎではございませんか？", "いいえ", "お急ぎは控え"),
    ("do", "当日のキャンセルはございませんか？", "いいえ", "当日のキャンセルは控え"),
    ("do", "駐車場はご利用になりますでしょうか？", "利用しない", "駐車場のご利用は控え"),
    ("do", "説明会にはご参加いただけますでしょうか？", "参加しない", "説明会へのご参加は控え"),
    ("do", "今月中のご導入をお考えでしょうか？", "いいえ", "今月中のご導入は控え"),
    ("doubt", "当日はお車でお越しになりますでしょうか？", "はい", "当日はお車でお越しになった方がいい"),
    # 2回目（へりくだりが重なる・お客様の言ったことより強い）
    ("do", "担当者から折り返しのお電話を差し上げてもよろしいでしょうか？", "はい", "担当者から折り返しのお電話を差し上げ"),
    ("do", "担当者から詳しいご案内のお電話を差し上げてもよろしいでしょうか？", "はい", "担当者から詳しいご案内のお電話を差し上げ"),
    ("do", "今月中のご導入をお考えでしょうか？", "はい", "今月中のご導入と"),
    # 4回目（Haiku 5.5・言葉の鍵を文と選択肢の組にして G11 を作った）：「させていただきますね？」に続かない
    ("do", "ご予約の日時に変更はございませんでしょうか？", "はい", "ご予約の日時に変更はございません"),
    # ✅ 2026-11-03 日本時間8時10分 ユーザー：(7)「当日のキャンセルはございませんか？」⇒「「それでは予定通りの方向で進めさせていただきますね？」」・(6)「お急ぎでは
    # ございませんか？」⇒「「○○日以降なら十分対応できますが、いつ頃がよろしいでしょうか？」のように確かめるのがいいでしょう。」＝お客様の答えを言い直して
    # 決めつける形（「キャンセルはなしと」「お急ぎではないと」）は使わない。同じ形の言葉（ご予約の日時の「変更なしと」・お急ぎの「いいえ」）も同じ（目的は同じ）。
    # その答えのあとの一言は、業務設定（追って伺う質問・こちらがすることの言葉）
    ("do", "当日のキャンセルはございませんか？", "はい", "当日のキャンセルはなしと"),
    ("do", "ご予約の日時に変更はございませんでしょうか？", "変更なし", "ご予約の日時に変更なしと"),
    ("do", "お急ぎではございませんか？", "はい", "お急ぎではないと"), ("do", "お急ぎではございませんか？", "いいえ", "お急ぎと"),
    # 5回目（言葉を文の終わりまで持つ形・Haiku 5.5）：予定どおりの向きの決まりと例を足しても、キャンセルはないを「〜ないということで承ります」と決めつけた（(7) のご判断と違う）
    ("do", "当日のキャンセルはございませんか？", "はい", "当日のキャンセルはないということで承ります"),
}


def known() -> dict[str, tuple[str, tuple, bool, dict]]:
    """測りの質問の1か所（g53_meaning.question_map の、取込の言葉を付ける前の表）の選択肢の質問：言葉の鍵（文と選択肢の組＝g53_meaning.word_key）->
    (文, 選択肢, 事実を伺う質問か, 意味の言葉)。⛔ 2026-11-03 の見直しで見つけた：文だけで1つにしていたので、同じ文で選択肢の違う質問（G10「変更なし・
    変更あり」と G11「はい・いいえ」）の G11 に、言葉が1つも付いていなかった（遠ざかる向きは G10 の「変更あり」＝G11 の選択肢に無い名前）。"""
    from g53_meaning import question_map, word_key
    out = {}
    for q in question_map(words=False).values():
        if q.type in CHOICE_TYPES:
            out.setdefault(word_key(q.text, q.conditions), (q.text, tuple(q.conditions), q.asks_fact, dict(q.meanings)))
    return out


def new_questions() -> list[tuple[str, tuple]]:
    """意味の言葉と事実を伺う質問かも、ここで作る質問：(文, 選択肢)。表（end_words）にしか無い質問と、文を直した測りの質問（g53_meaning.renamed。✅ 2026-11-03 ユーザー：「「折り返し」だとすぐだと思われ断られる可能性が高いので、「また」に変更するように言ったよね！」）。"""
    from end_words import CHOICES
    from g53_meaning import renamed, word_key
    k = known()
    return [(t, c) for t, c in CHOICES.items() if word_key(t, c) not in k] + renamed()


def run(args) -> dict:
    from facts import by_model as facts_by_model, decide as facts_decide
    from g53_meaning import word_key
    from meanings import by_model, decide, decide_away

    ring = KeyRing(args)
    # 22の質問をまとめて答えさせる長い問い合わせ（⛔ 2026-11-03：180秒で 504 DEADLINE_EXCEEDED が続いた）＝この道具だけ待つ長さを延ばす
    ring.TIMEOUT_S = 600.0
    stand = _stand_in(args)   # 作り手の欄 ⇒ 代わりに問い合わせる関数（--stand-in のときだけ）
    ids = {} if stand else ring.model_ids()
    k, new = known(), new_questions()
    rows: dict = {kind: {} for kind in ("meaning", "facts") + KINDS + (AWAY,)}
    errors: dict = {}
    # 前の結果の言葉：種類 → モデル → {言葉の鍵: 答え}（言葉の鍵＝文と選択肢の組。意味の言葉・事実を伺う質問かは、その質問の答え）
    prev: dict = {}
    if args.resume:
        # 途中で止まった流し（通信が切れた・時間切れ）の続き・前の結果の続き：答えのあった問い合わせは、もう一度は送らない（同じ問い合わせを2回しない）。
        # --redo の種類だけは送り直す（作らせる文の決まりを直したとき）
        import glob
        from common import HERE
        # 途中の記録は、質問の並び（targets）を持つときだけ使う（⛔ 2026-11-03：言葉を問い合わせる前に止まった途中の記録には、前に決まった言葉も
        # 並びも無く、それを拾って、ご確認いただいた言葉まで作り直しかけた）。無ければ前の結果
        part = [p for p in sorted(glob.glob(str(HERE / "results" / "G-72_partial.json")))
                if json.loads(open(p, encoding="utf-8").read()).get("targets")] or sorted(glob.glob(str(HERE / "results" / "G-72_2*.json")))
        redo = set(filter(None, (args.redo or "").split(",")))
        if part:
            old = json.loads(open(part[-1], encoding="utf-8").read())
            # 答えは、その流しの質問の並びに合わせた並び ⇒ 言葉の鍵で合わせて持つ（⛔ 2026-11-03：前の結果のあとで質問が足され、番号で合わせて止まった）。
            # 今の並びに無い質問・前に無い質問は、足りない分だけ問い合わせる（ask_each）
            lists = {"meaning": old.get("new_questions", []), "facts": old.get("new_questions", [])}
            for kind, by in old.get("rows", {}).items():
                if kind in redo or kind not in rows:
                    continue
                order = [word_key(t, c) for t, c in lists.get(kind, old.get("targets", []))]
                for m, v in by.items():
                    if v is not None and len(v) == len(order):
                        prev.setdefault(kind, {})[m] = dict(zip(order, v))

    def ask_each(kind: str, m: str, items: list[tuple[str, tuple]], fn) -> list:
        """答え（items の並びに合わせた並び）：前の結果にある質問はそのまま、足りない質問だけを1回の問い合わせで聞く（fn(足りない質問)）。問い合わせが
        失敗しても、前の結果の答えは残す（⛔ 2026-11-03：失敗で None を返し、前の言葉ごと消えた結果を保存した）。"""
        have = prev.get(kind, {}).get(m, {})
        missing = [x for x in items if word_key(x[0], x[1]) not in have]
        got = {}
        if missing:
            label = f"{kind}・{m}"
            try:
                if stand:
                    r = stand[m](missing, kind)
                    say(kind, m, f"の欄を {STAND_INS[m]} が作った")
                else:
                    r, used = ring.call(ids[m], lambda cl: fn(cl, missing))
                    say(kind, m, f"キー {ring.keys[used].label}")
            except Exception as exc:
                errors[label] = describe_error(exc)
                say(kind, m, "誤りで止まりました：", str(errors[label].get("message", ""))[:200])
                r = None
            got = dict(zip([word_key(x[0], x[1]) for x in missing], r)) if r is not None else {}
            say(kind, m, f"足りない質問 {len(missing)} 個を問い合わせた（前の結果の {len(items) - len(missing)} 個はそのまま）")
        return [have.get(word_key(x[0], x[1]), got.get(word_key(x[0], x[1]))) for x in items]

    models = list(MODELS) if stand else [m for m in MODELS if m in ids]
    for m in MODELS:
        if m not in ids and not stand:
            errors[m] = "モデルが見つからない"
    # 意味の言葉が無い質問（表にしか無い質問・文を直した質問）：事実を伺う質問か・意味の言葉（取込と同じ決め方）
    for m in models:
        rows["facts"][m] = ask_each("facts", m, new, lambda cl, xs, m=m: facts_by_model(cl, ids[m], [t for t, _ in xs]))
        rows["meaning"][m] = ask_each("meaning", m, new, lambda cl, xs, m=m: by_model(cl, ids[m], xs, "meaning"))
        save_partial("G-72", {"rows": rows, "errors": errors, "new_questions": new})
    fact_of = {t: facts_decide([(rows["facts"].get(m) or [None] * len(new))[i] for m in models]) for i, (t, _) in enumerate(new)}
    meaning_of = {word_key(t, c): decide(t, c, [(rows["meaning"].get(m) or [{}] * len(new))[i] or {} for m in models])
                  for i, (t, c) in enumerate(new)}
    redone = {word_key(t, c) for t, c in new}
    questions = [(t, c, fact_of[t] if key in redone else f, meaning_of[key] if key in redone else m)
                 for key, (t, c, f, m) in k.items()]
    questions += [(t, c, fact_of[t], meaning_of[word_key(t, c)]) for t, c in new if word_key(t, c) not in k]
    targets = [(t, c) for t, c, f, _ in questions if not f]   # 事実を伺う質問ではない質問だけ
    meaning_by = {word_key(t, c): m for t, c, _, m in questions}   # 向きを比べる・遠ざかる向きを読ませる意味の言葉

    for kind in KINDS + (AWAY,):
        # 遠ざかる向きは、選択肢ごとに意味の言葉を添えて読ませる（meanings._body。⛔ 2026-11-03：名前だけで、打ち消しの問いの「はい」を逆に読んだ）
        items = [(t, c, meaning_by[word_key(t, c)]) if kind == AWAY else (t, c) for t, c in targets]
        for m in models:
            rows[kind][m] = ask_each(kind, m, items, lambda cl, xs, m=m, kind=kind: by_model(cl, ids[m], xs, kind))
            save_partial("G-72", {"rows": rows, "errors": errors, "targets": targets, "new_questions": new})
    words = {kind: {word_key(t, c): decide(t, c, [(rows[kind].get(m) or [{}] * len(targets))[i] or {} for m in models], kind,
                                            meaning_by[word_key(t, c)])
                    for i, (t, c) in enumerate(targets)} for kind in KINDS}
    # CL-1 が見て誤りとした言葉（BAD）は、流れの制御に渡さない（⛔ 2026-11-03：数えるだけで、決まった言葉として渡していた）
    for kind in KINDS:
        for (t, c) in targets:
            by = words[kind][word_key(t, c)]
            for ch in [ch for ch, w in by.items() if (kind, t, ch, w) in BAD]:
                del by[ch]
    words[AWAY] = {word_key(t, c): list(decide_away(c, [(rows[AWAY].get(m) or [{}] * len(targets))[i] or {} for m in models]))
                   for i, (t, c) in enumerate(targets)}
    show(targets, rows, words, models)
    summary = tally(targets, words)
    say("数え：", summary)
    return {"model_ids": ids, "new_questions": new, "facts": fact_of, "meanings": meaning_of, "targets": targets,
            "rows": rows, "words": words, "errors": errors, "summary": summary}


def _stand_in(args) -> dict:
    """--stand-in のとき、作り手の欄ごとの代わりの問い合わせ（同じ文・同じ読み取り＝meanings.by_model_anthropic）。判定と同じ鍵・同じ料金の帳簿
    （上限は --budget-yen）。"""
    if not getattr(args, "stand_in", False):
        return {}
    import anthropic
    from common import LEDGERS, load_secret
    from meaning_judge import OTHER_JUDGES, SPEND
    from facts import by_model_anthropic as facts_anthropic
    from meanings import by_model_anthropic
    client = anthropic.Anthropic(api_key=load_secret(OTHER_JUDGES["Claude Haiku 5.5"]["key"], args.env), max_retries=2, timeout=300)
    SPEND.limit = args.budget_yen
    if SPEND not in LEDGERS:
        LEDGERS.append(SPEND)
    def make(sub):
        model = OTHER_JUDGES[sub]["model"]
        return lambda qs, kind: (facts_anthropic(client, model, [q[0] for q in qs]) if kind == "facts" else by_model_anthropic(client, model, qs, kind))
    return {m: make(sub) for m, sub in STAND_INS.items()}


def show(targets, rows, words, models) -> None:
    from g53_meaning import word_key
    from meanings import check
    for i, (t, c) in enumerate(targets):
        key = word_key(t, c)
        say(f"{i + 1}：{t}（{'・'.join(c)}）")
        for ch in c:
            for kind in KINDS + (AWAY,):
                got = {m: ((rows[kind].get(m) or [{}] * len(targets))[i] or {}).get(ch, "") for m in models}
                done = ("遠ざかる" if ch in words[AWAY][key] else "遠ざからない") if kind == AWAY else \
                    f"「{words[kind][key][ch]}」" if ch in words[kind][key] else "決まらない"
                say(f"   {ch}（{kind}）：", " ／ ".join(f"{m}「{w}」{'' if w and check(w, t, ch, kind) else '✗'}" for m, w in got.items()), "⇒", done)


def tally(targets, words) -> dict:
    from g53_meaning import word_key
    out = {"away": sum(len(words.get(AWAY, {}).get(word_key(t, c), [])) for t, c in targets)}
    for kind in KINDS:
        n = sum(len(c) for _, c in targets)
        got = sum(len(words[kind][word_key(t, c)]) for t, c in targets)
        out[kind] = {"選択肢": n, "決まった": got, "決まらない": n - got,
                     "⛔取り違え": sum(1 for t, c in targets for ch in c if (kind, t, ch, words[kind][word_key(t, c)].get(ch, "")) in BAD)}
    return out


def main() -> None:
    require_python()
    p = base_parser(__doc__)
    p.add_argument("--resume", action="store_true", help="途中で止まった流し・前の結果の続き（答えのあった問い合わせは送らない）")
    p.add_argument("--redo", help="--resume のとき、送り直す言葉の種類（doubt,do,away など。作らせる文の決まりを直したとき）")
    p.add_argument("--stand-in", action="store_true", help="Gemini の Flash 2つの欄を、代わりの作り手（STAND_INS＝Claude Haiku 5.5）が作る（混んでいる間）")
    args = p.parse_args()
    got = run(args)
    if got["errors"]:
        # 誤りのあった流しは結果として保存しない（測りの道具は、いちばん新しい結果から選択肢ごとの言葉を読む＝g53_meaning.imported。⛔ 2026-11-03：
        # 全部の問い合わせが時間切れの流しを保存し、ご確認いただいた言葉が無い結果が「新しい結果」になった）。途中の記録に残し、--resume で続ける
        save_partial("G-72", {k: got[k] for k in ("rows", "errors", "targets", "new_questions")})
        say("誤りがあったので結果として保存しません（途中の記録に残しました。--resume で続けます）：", list(got["errors"])[:6])
        raise SystemExit(1)
    save_result("G-72", got)
    drop_partial("G-72")


if __name__ == "__main__":
    main()
