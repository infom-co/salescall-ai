"""G-49：「大丈夫です」「結構です」「いいです」「平気です」だけの答えの意味を、前の質問から、人を介さずに決められるかを測る（meanings.py の
by_model_replies・decide_replies。2026-10-17）。

✅ 2026-10-17 ユーザー：「『結構です。』だけでは判断できない。人でも前の質問や会話から判断する言葉です」
⇒ 取込のときに、質問ごとに、これらの言葉だけの答えがどの選択肢の意味になるかを、文字のモデル2つが質問の文から決める。決まれば、通話の中では
確かめずにその選択肢で受け取る（例：「パンフレットをお送りしてもよろしいでしょうか？」に「結構です」＝お送りしない）。決まらなければ、
通話の中で、AIの受け取った意味を確かめる（G-14）。
- 材料：G-48 と同じ 30問 × 4つの言葉＝120組。
- 正解（TRUTH）：CL-1 が決めた、受け取ってよい答えの組（None＝決めない）。だれが聞いても一つに決まる組だけに選択肢を足した。書いていない組は
  None だけ（決めないのが正しい）。⛔ 一番こわいのは「決めすぎ」（正解に無い選択肢に決めた）＝相手に確かめずに違う答えが渡りうる。
1日の上限（Flash は各20回）に収まるよう、1回の問い合わせで全部を見させる。上限に当たったら次のキーで続ける（common.KeyRing）。
"""
from __future__ import annotations

import itertools
import json

from common import KeyRing, base_parser, describe_error, drop_partial, require_python, save_partial, save_result, say
from flowctl import AMBIGUOUS_WORDS
from g23_names import find_models
from g48_meanings import QUESTIONS
from meanings import by_model_replies, decide_replies

MODELS = ("3.8 Flash", "3.7 Flash", "3.6 Flash")
N = None
# (質問の番号＝1から) ⇒ {言葉: 受け取ってよい答えの組}。書いていない言葉・質問は {None}（決めないのが正しい）
TRUTH = {
    6: {"結構": {N, "利用しない"}},                              # 駐車場はご利用になりますでしょうか？
    7: {"結構": {N, "いいえ"}},                                  # 折り返しのお電話を差し上げてもよろしいでしょうか？
    8: {"結構": {N, "いいえ"}},                                  # パンフレットをお送りしてもよろしいでしょうか？
    9: {"結構": {N, "参加しない"}},                              # 説明会にはご参加いただけますでしょうか？
    10: {w: {N, "変更なし"} for w in AMBIGUOUS_WORDS},           # 変更はございませんでしょうか？（変更なし・変更あり）
    11: {w: {N, "はい"} for w in AMBIGUOUS_WORDS},               # 同じ問いで、はい＝変更なし
    15: {"結構": {N, "いいえ"}},                                 # 今月中のご導入をお考えでしょうか？
    26: {w: {N, "はい"} for w in AMBIGUOUS_WORDS},               # ご不明な点はございませんでしょうか？（はい＝無い）
    27: {"大丈夫": {N, "はい"}, "平気": {N, "はい"}},              # お急ぎではございませんか？（はい＝急いでいない）
    28: {"大丈夫": {N, "はい"}, "結構": {N, "はい"}, "平気": {N, "はい"}},  # 当日のキャンセルはございませんか？
}


def ok(n: int, word: str, got: str | None) -> bool:
    return got in TRUTH.get(n, {}).get(word, {N})


def tally(rows: dict) -> dict:
    out = {}
    def count(get) -> dict:
        t = {"決めた（正しい）": 0, "決めない（正しい）": 0, "決めない（決めてよかった）": 0, "⛔決めすぎ": 0, "⛔決めすぎの例": []}
        for n, (q, cs) in enumerate(QUESTIONS, 1):
            for w in AMBIGUOUS_WORDS:
                g = get(n)
                c = g.get(w)
                if not ok(n, w, c):
                    t["⛔決めすぎ"] += 1
                    t["⛔決めすぎの例"].append(f"{q}／{w}です ⇒ {c}")
                elif c is None:
                    t["決めない（正しい）" if TRUTH.get(n, {}).get(w, {N}) == {N} else "決めない（決めてよかった）"] += 1
                else:
                    t["決めた（正しい）"] += 1
        return t
    for k, got in rows.items():
        out[k] = count(lambda n, got=got: got[n - 1])
    for a, b in itertools.combinations(rows, 2):  # decide_replies そのもの
        out[f"{a}×{b}"] = count(lambda n, a=a, b=b: decide_replies(QUESTIONS[n - 1][1], [rows[a][n - 1], rows[b][n - 1]]))
    return out


def show(rows: dict) -> None:
    for n, (q, cs) in enumerate(QUESTIONS, 1):
        got = {k: v[n - 1] for k, v in rows.items()}
        marks = " ".join(f"{w}={'/'.join(str(g.get(w) or '-') for g in got.values())}{'' if all(ok(n, w, g.get(w)) for g in got.values()) else '⛔'}"
                         for w in AMBIGUOUS_WORDS)
        say(f"{n}：{q}（{'・'.join(cs)}） {marks}")


def run(args) -> dict:
    ring = KeyRing(args)
    ids, _ = find_models(ring.client())
    rows, errors = {}, {}
    for k in MODELS:
        if k not in ids:
            errors[k] = "モデルが見つからない"
            continue
        try:
            rows[k], used = ring.call(ids[k], lambda c: by_model_replies(c, ids[k], QUESTIONS))
            say(k, f"キー {ring.keys[used].label}")
        except Exception as exc:
            errors[k] = describe_error(exc)
            say(k, "誤りで止まりました：", str(errors[k].get("message", ""))[:200])
        save_partial("G-49", {"rows": rows, "errors": errors})
    show(rows)
    summary = tally(rows)
    for k, v in summary.items():
        say(k, {x: y for x, y in v.items() if x != "⛔決めすぎの例"}, v["⛔決めすぎの例"])
    return {"model_ids": ids, "questions": QUESTIONS, "rows": rows, "errors": errors, "summary": summary}


def rescore(path: str) -> dict:
    d = json.loads(open(path, encoding="utf-8").read())
    show(d["rows"])
    summary = tally(d["rows"])
    for k, v in summary.items():
        say(k, {x: y for x, y in v.items() if x != "⛔決めすぎの例"}, v["⛔決めすぎの例"])
    return {**d, "rescored_from": path, "summary": summary}


def main() -> None:
    require_python()
    p = base_parser(__doc__)
    p.add_argument("--rescore", help="保存した結果を、いまの正解（TRUTH）と決め方で数え直す（問い合わせはしない）")
    args = p.parse_args()
    save_result("G-49", rescore(args.rescore) if args.rescore else run(args))
    drop_partial("G-49")


if __name__ == "__main__":
    main()
