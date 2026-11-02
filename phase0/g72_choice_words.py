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
KEEP = "keep"   # 目的につなげて伺う言葉（決め方が違う＝meanings.decide_keep。✅ 2026-11-03 ユーザー (2)「OK」）
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
    """測りの質問の1か所（g53_meaning.question_map の、取込の言葉を付ける前の表）の選択肢の質問：文 -> (選択肢, 事実を伺う質問か, 意味の言葉)。
    同じ文は最初のもの。"""
    from g53_meaning import question_map
    out = {}
    for q in question_map(words=False).values():
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
    # 22の質問をまとめて答えさせる長い問い合わせ（⛔ 2026-11-03：180秒で 504 DEADLINE_EXCEEDED が続いた）＝この道具だけ待つ長さを延ばす
    ring.TIMEOUT_S = 600.0
    ids = ring.model_ids()
    k, new = known(), new_questions()
    rows: dict = {"meaning": {}, "facts": {}, **{kind: {} for kind in KINDS + (KEEP,)}}
    errors: dict = {}
    prev: dict = {}   # 前の結果の選択肢ごとの言葉：種類 → モデル → {質問の文: {選択肢: 言葉}}
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
            old_targets = [t for t, _ in old.get("targets", [])]
            for kind, by in old.get("rows", {}).items():
                if kind in redo:
                    continue
                if kind in KINDS + (KEEP,):
                    # 選択肢ごとの言葉は、前の結果の質問の並び（targets）に合わせた並び ⇒ 質問の文で合わせて持つ（⛔ 2026-11-03：前の結果のあとで質問が
                    # 足され、番号で合わせて止まった）。今の並びに無い質問・前に無い質問は、足りない分だけ問い合わせる（ask_words）
                    for m, v in by.items():
                        if v is not None and len(v) == len(old_targets):
                            prev.setdefault(kind, {})[m] = dict(zip(old_targets, v))
                else:
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
    def ask_words(kind: str, m: str):
        """選択肢ごとの言葉（今の質問の並びに合わせた並び）：前の結果にある質問はそのまま、足りない質問だけを1回の問い合わせで聞く。"""
        have = prev.get(kind, {}).get(m, {})
        missing = [(t, c) for t, c in targets if t not in have]
        got = {}
        if missing:
            r = ask(kind, m, lambda cl, m=m, kind=kind, missing=missing: by_model(cl, ids[m], missing, kind), kind)
            # 問い合わせが失敗しても、前の結果の言葉は残す（⛔ 2026-11-03：失敗で None を返し、前の言葉ごと消えた結果を保存した）
            got = dict(zip([t for t, _ in missing], r)) if r is not None else {}
            say(kind, m, f"足りない質問 {len(missing)} 個を問い合わせた（前の結果の {len(targets) - len(missing)} 個はそのまま）")
        return [have.get(t, got.get(t, {})) for t, _ in targets]

    for kind in KINDS:
        for m in models:
            rows[kind][m] = ask_words(kind, m)
            save_partial("G-72", {"rows": rows, "errors": errors, "targets": targets})
    words = {kind: {t: decide(t, c, [(rows[kind].get(m) or [{}] * len(targets))[i] for m in models], kind, meaning_by[t])
                    for i, (t, c) in enumerate(targets)} for kind in KINDS}
    from meanings import decide_keep
    for m in models:
        rows[KEEP][m] = ask_words(KEEP, m)
        save_partial("G-72", {"rows": rows, "errors": errors, "targets": targets})
    kept = {t: decide_keep(t, c, [(rows[KEEP].get(m) or [{}] * len(targets))[i] for m in models]) for i, (t, c) in enumerate(targets)}
    words[KEEP] = {t: k for t, (_, k) in kept.items()}
    words["away"] = {t: list(a) for t, (a, _) in kept.items()}
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
            for kind in KINDS + (KEEP,):
                got = {m: (rows[kind].get(m) or [{}] * len(targets))[i].get(ch, "") for m in models}
                say(f"   {ch}（{kind}）：", " ／ ".join(f"{m}「{w}」{'' if w and check(w, t, ch, kind) else '✗'}" for m, w in got.items()),
                    "⇒", f"「{words[kind][t].get(ch, '')}」" if ch in words[kind][t] else "決まらない")


def tally(targets, words) -> dict:
    out = {}
    out["away"] = sum(len(words.get("away", {}).get(t, [])) for t, _ in targets)
    for kind in KINDS + (KEEP,):
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
    got = run(args)
    if got["errors"]:
        # 誤りのあった流しは結果として保存しない（測りの道具は、いちばん新しい結果から選択肢ごとの言葉を読む＝g53_meaning.imported。⛔ 2026-11-03：
        # 全部の問い合わせが時間切れの流しを保存し、ご確認いただいた言葉が無い結果が「新しい結果」になった）。途中の記録に残し、--resume で続ける
        save_partial("G-72", {k: got[k] for k in ("rows", "errors", "targets")})
        say("誤りがあったので結果として保存しません（途中の記録に残しました。--resume で続けます）：", list(got["errors"])[:6])
        raise SystemExit(1)
    save_result("G-72", got)
    drop_partial("G-72")


if __name__ == "__main__":
    main()
