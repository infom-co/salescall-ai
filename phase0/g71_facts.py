"""G-71：質問が「お客様に事実を伺う質問か」を、人を介さずに決められるかを測る（facts.py。2026-11-01）。

✅ 2026-11-01 ユーザー：(a)「OK」＝質問ごとに「事実を伺う質問か」を1か所に持ち、事実を伺う質問では「どちらでも」を選べるものから外す・控える／
断る返事は「答えたくない」。決めるのは取込のとき（選択肢の意味の言葉＝G-48・聞くこと＝G-43 と同じく、文字のモデル2つ。人は確かめない）。
- 材料：答えの段の測り（G-53）が使う質問の全部（g53_meaning の質問の1か所）。
- 見ること：(1) モデルどうしで答えがそろうか（decide はそろったときだけ使い、そろわなければ事実を伺う質問） (2) ⛔ 取り違え（希望・申し出の
  質問を事実と決めた／事実の質問を希望と決めた）を CL-1 が1つずつ見る（WRONG に書いて数え直す）。
1日の上限に収まるよう、1回の問い合わせで全部を見させる。上限に当たったら次のキーで続ける（common.KeyRing）。
"""
from __future__ import annotations

from common import KeyRing, base_parser, describe_error, require_python, save_partial, save_result, say
from facts import by_model, decide

MODELS = ("3.8 Flash", "3.7 Flash")
# ⛔ 取り違え（CL-1 が1つずつ見て書く）：質問の文
WRONG: set[str] = set()


def questions() -> list[str]:
    """測る質問の文（答えの段の測りと同じ1か所＝g53_meaning の質問。同じ文は1回）。"""
    from g53_meaning import question_map
    return list(dict.fromkeys(q.text for q in question_map(facts=False).values()))


def tally(qs: list[str], rows: dict) -> dict:
    got = [decide([rows[k][i] for k in rows]) for i in range(len(qs))]
    agree = sum(1 for i in range(len(qs)) if len({rows[k][i] for k in rows}) == 1 and rows[next(iter(rows))][i] is not None)
    return {"そろった": agree, "そろわない（事実を伺う質問とする）": len(qs) - agree, "事実を伺う質問": sum(got),
            "⛔取り違え": sum(1 for q, g in zip(qs, got) if q in WRONG)}


def run(args) -> dict:
    ring = KeyRing(args)
    ids = ring.model_ids()
    qs = questions()
    rows, errors = {}, {}
    for k in MODELS:
        if k not in ids:
            errors[k] = "モデルが見つからない"
            continue
        try:
            rows[k], used = ring.call(ids[k], lambda c: by_model(c, ids[k], qs))
            say(k, f"キー {ring.keys[used].label}")
        except Exception as exc:
            errors[k] = describe_error(exc)
            say(k, "誤りで止まりました：", str(errors[k].get("message", ""))[:200])
        save_partial("G-71", {"questions": qs, "rows": rows, "errors": errors})
    for i, q in enumerate(qs):
        say(f"{'事実' if decide([rows[k][i] for k in rows]) else '希望・申し出'}：{q}", {k: rows[k][i] for k in rows})
    summary = tally(qs, rows) if rows else {}
    say("数え：", summary)
    return {"model_ids": ids, "questions": qs, "rows": rows, "errors": errors, "summary": summary}


def main() -> None:
    require_python()
    args = base_parser(__doc__).parse_args()
    save_result("G-71", run(args))


if __name__ == "__main__":
    main()
