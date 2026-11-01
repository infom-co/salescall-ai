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
}


def known() -> dict[str, tuple[tuple, bool, dict]]:
    """測りの質問の1か所（g53_meaning.question_map）の選択肢の質問：文 -> (選択肢, 事実を伺う質問か, 意味の言葉)。同じ文は最初のもの。"""
    from g53_meaning import question_map
    out = {}
    for q in question_map().values():
        if q.type in CHOICE_TYPES and q.text not in out:
            out[q.text] = (tuple(q.conditions), q.asks_fact, dict(q.meanings))
    return out


def new_questions() -> list[tuple[str, tuple]]:
    """表（end_words）にしか無い質問：(文, 選択肢)。"""
    from end_words import CHOICES
    k = known()
    return [(t, c) for t, c in CHOICES.items() if t not in k]


def run(args) -> dict:
    from facts import by_model as facts_by_model, decide as facts_decide
    from meanings import by_model, decide

    ring = KeyRing(args)
    ids = ring.model_ids()
    k, new = known(), new_questions()
    rows: dict = {"meaning": {}, "facts": {}, **{kind: {} for kind in KINDS}}
    errors: dict = {}
    if args.resume:
        # 途中で止まった流し（通信が切れた・時間切れ）の続き・前の結果の続き：答えのあった問い合わせは、もう一度は送らない（同じ問い合わせを2回しない）。
        # --redo の種類だけは送り直す（作らせる文の決まりを直したとき）
        import glob
        from common import HERE
        part = sorted(glob.glob(str(HERE / "results" / "G-72_partial.json"))) or sorted(glob.glob(str(HERE / "results" / "G-72_2*.json")))
        redo = set(filter(None, (args.redo or "").split(",")))
        if part:
            for kind, by in json.loads(open(part[-1], encoding="utf-8").read()).get("rows", {}).items():
                if kind not in redo:
                    rows.setdefault(kind, {}).update({m: v for m, v in by.items() if v is not None})

    def ask(label: str, model: str, fn, kind: str):
        if rows[kind].get(model) is not None:
            return rows[kind][model]
        try:
            got, used = ring.call(ids[model], fn)
            say(label, model, f"キー {ring.keys[used].label}")
            return got
        except Exception as exc:
            errors[f"{label}・{model}"] = describe_error(exc)
            say(label, model, "誤りで止まりました：", str(errors[f"{label}・{model}"].get("message", ""))[:200])
            return None

    models = [m for m in MODELS if m in ids]
    for m in MODELS:
        if m not in ids:
            errors[m] = "モデルが見つからない"
    # 表にしか無い質問：事実を伺う質問か・意味の言葉（取込と同じ決め方）
    for m in models:
        rows["facts"][m] = ask("事実を伺う質問か", m, lambda c, m=m: facts_by_model(c, ids[m], [t for t, _ in new]), "facts")
        rows["meaning"][m] = ask("意味の言葉", m, lambda c, m=m: by_model(c, ids[m], new, "meaning"), "meaning")
        save_partial("G-72", {"rows": rows, "errors": errors})
    fact_of = {t: facts_decide([(rows["facts"].get(m) or [None] * len(new))[i] for m in models]) for i, (t, _) in enumerate(new)}
    meaning_of = {t: decide(t, c, [(rows["meaning"].get(m) or [{}] * len(new))[i] for m in models]) for i, (t, c) in enumerate(new)}
    questions = [(t, c, f, m) for t, (c, f, m) in k.items()] + [(t, c, fact_of[t], meaning_of[t]) for t, c in new]
    targets = [(t, c) for t, c, f, _ in questions if not f]   # 事実を伺う質問ではない質問だけ
    meaning_by = {t: m for t, _, _, m in questions}           # 向きを比べる意味の言葉（meanings.decide の meanings）
    for kind in KINDS:
        for m in models:
            rows[kind][m] = ask(kind, m, lambda c, m=m, kind=kind: by_model(c, ids[m], targets, kind), kind)
            save_partial("G-72", {"rows": rows, "errors": errors, "targets": targets})
    words = {kind: {t: decide(t, c, [(rows[kind].get(m) or [{}] * len(targets))[i] for m in models], kind, meaning_by[t])
                    for i, (t, c) in enumerate(targets)} for kind in KINDS}
    show(targets, rows, words, models)
    summary = tally(targets, words)
    say("数え：", summary)
    return {"model_ids": ids, "new_questions": new, "facts": fact_of, "meanings": meaning_of, "targets": targets,
            "rows": rows, "words": words, "errors": errors, "summary": summary}


def show(targets, rows, words, models) -> None:
    from meanings import check
    for i, (t, c) in enumerate(targets):
        say(f"{i + 1}：{t}")
        for ch in c:
            for kind in KINDS:
                got = {m: (rows[kind].get(m) or [{}] * len(targets))[i].get(ch, "") for m in models}
                say(f"   {ch}（{kind}）：", " ／ ".join(f"{m}「{w}」{'' if w and check(w, t, ch, kind) else '✗'}" for m, w in got.items()),
                    "⇒", f"「{words[kind][t].get(ch, '')}」" if ch in words[kind][t] else "決まらない")


def tally(targets, words) -> dict:
    out = {}
    for kind in KINDS:
        n = sum(len(c) for _, c in targets)
        got = sum(len(words[kind][t]) for t, _ in targets)
        out[kind] = {"選択肢": n, "決まった": got, "決まらない": n - got,
                     "⛔取り違え": sum(1 for t, c in targets for ch in c if (kind, t, ch, words[kind][t].get(ch, "")) in BAD)}
    return out


def main() -> None:
    require_python()
    p = base_parser(__doc__)
    p.add_argument("--resume", action="store_true", help="途中で止まった流し・前の結果の続き（答えのあった問い合わせは送らない）")
    p.add_argument("--redo", help="--resume のとき、送り直す言葉の種類（doubt,do など。作らせる文の決まりを直したとき）")
    args = p.parse_args()
    save_result("G-72", run(args))
    drop_partial("G-72")


if __name__ == "__main__":
    main()
