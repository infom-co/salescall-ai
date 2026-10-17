"""G-48：選択肢の質問の「意味の言葉」を、人を介さずに決められるかを測る（meanings.py。2026-10-17）。

きっかけ：G-14 で、受け入れにも断りにも取れる答え（「大丈夫です」「結構です」）を、AIが24本中21本で「はい」「いいえ」に決めた。
⇒ 流れの制御が、AIの受け取った意味で「念のため（意味の言葉）でよろしいでしょうか？」と確かめる（DESIGN.md 6-10 の (3)）。その（意味の言葉）を、
取込のときに文字のモデルが決める。あわせて、選択肢の型を聞き返したあとの言い直し（「はいとおっしゃいましたでしょうか？」になっていた）にも使う。
- 材料：はい／いいえの質問・選択肢の質問 30問（2回目に、打ち消しの問いなど5問を足した）（QUESTIONS）。選択肢が「はい・いいえ」のもの・言葉になっているもの（「ある・ない」）・
  打ち消しの問い（「変更はございませんでしょうか？」）・お願いの問い（「お送りしてもよろしいでしょうか？」）・選択肢が3つのものを混ぜた。
  例に使った2問（プロンプトの中）は入れない。
- 見ること：(1) 確かめ（check）に通るか (2) モデルどうしで言葉が同じになるか（decide はそれだけを使う） (3) ⛔ 意味を取り違えていないか
  （相手が認めると逆の答えが渡る）。(3) は言葉を1つずつ書き出して CL-1 が見る（BAD に書いて数え直す）。
1日の上限（Flash は各20回）に収まるよう、1回の問い合わせで全部を見させる。上限に当たったら次のキーで続ける（common.KeyRing）。
"""
from __future__ import annotations

import itertools
import json

from common import KeyRing, base_parser, describe_error, drop_partial, require_python, save_partial, save_result, say
from g23_names import find_models
from meanings import by_model, check, decide

QUESTIONS = [
    ("ご予約はされていますでしょうか？", ("はい", "いいえ")),
    ("資料はお手元に届いていますでしょうか？", ("はい", "いいえ")),
    ("現在ほかの業者様とのご契約はございますでしょうか？", ("ある", "ない")),
    ("ご来店は初めてでしょうか？", ("初めて", "2回目以降")),
    ("当日はお車でお越しになりますでしょうか？", ("はい", "いいえ")),
    ("駐車場はご利用になりますでしょうか？", ("利用する", "利用しない")),
    ("担当者から折り返しのお電話を差し上げてもよろしいでしょうか？", ("はい", "いいえ")),
    ("パンフレットをお送りしてもよろしいでしょうか？", ("はい", "いいえ")),
    ("説明会にはご参加いただけますでしょうか？", ("参加する", "参加しない")),
    ("ご予約の日時に変更はございませんでしょうか？", ("変更なし", "変更あり")),
    ("ご予約の日時に変更はございませんでしょうか？", ("はい", "いいえ")),   # 打ち消しの問いに「はい／いいえ」（はい＝変更なし）
    ("予約日は決まっていますか？", ("決まっている", "決まっていない")),     # Intent.md 6-3 の例
    ("複合機は現在リースでご利用でしょうか？", ("はい", "いいえ")),
    ("前回のご案内はご覧いただけましたでしょうか？", ("はい", "いいえ")),
    ("今月中のご導入をお考えでしょうか？", ("はい", "いいえ")),
    ("お支払いはカードと振込のどちらがよろしいでしょうか？", ("カード", "振込")),
    ("ご連絡はお電話とメールのどちらがよろしいでしょうか？", ("電話", "メール")),
    ("ご訪問は平日と土日のどちらがよろしいでしょうか？", ("平日", "土日")),
    ("ご希望のプランはスタンダードとプレミアムのどちらでしょうか？", ("スタンダード", "プレミアム")),
    ("お打ち合わせはオンラインとご訪問のどちらがよろしいでしょうか？", ("オンライン", "訪問")),
    ("お部屋は禁煙と喫煙のどちらをご希望でしょうか？", ("禁煙", "喫煙")),
    ("従業員数は10名未満と10名から50名と50名以上のどれにあたりますでしょうか？", ("10名未満", "10名から50名", "50名以上")),
    ("お使いの回線は光回線とモバイル回線のどちらでしょうか？", ("光回線", "モバイル回線")),
    ("お見積もりは郵送とメールのどちらでお送りすればよろしいでしょうか？", ("郵送", "メール")),
    ("お席はカウンターとテーブルのどちらがよろしいでしょうか？", ("カウンター", "テーブル")),
    # 2回目に足した：打ち消しの問いに「はい／いいえ」（「はい」が打ち消し側になる）・在席・同行
    ("ご不明な点はございませんでしょうか？", ("はい", "いいえ")),
    ("お急ぎではございませんか？", ("はい", "いいえ")),
    ("当日のキャンセルはございませんか？", ("はい", "いいえ")),
    ("ご担当者様は本日ご在席でしょうか？", ("はい", "いいえ")),
    ("お子様もご一緒でしょうか？", ("はい", "いいえ")),
    # 3回目に足した：G-14 の Q5（選択肢を言葉で書いた、はい／いいえの質問）
    ("ご予約はされていますでしょうか？", ("している", "していない")),
]
MODELS = ("3.8 Flash", "3.7 Flash", "3.6 Flash")
# ⛔ 意味を取り違えた言葉（CL-1 が1つずつ見て書く）：(質問の番号＝1から, 選択肢, 言葉)
BAD: set[tuple[int, str, str]] = set()


def tally(rows: dict) -> dict:
    """モデルごと・組み合わせごとに：確かめに通った／空・通らない／⛔取り違え（BAD）を、選択肢の数で数える。"""
    out = {}
    for k, got in rows.items():
        t = {"通った": 0, "空・通らない": 0, "⛔取り違え": 0}
        for n, ((q, cs), g) in enumerate(zip(QUESTIONS, got), 1):
            for c in cs:
                w = g.get(c, "")
                if (n, c, w) in BAD:
                    t["⛔取り違え"] += 1
                t["通った" if w and check(w, q, c) else "空・通らない"] += 1
        out[k] = t
    for a, b in itertools.combinations(rows, 2):  # meanings.decide そのもの（2つのモデルの言葉が同じときだけ）
        t = {"決まった": 0, "決まらない": 0, "⛔取り違え": 0}
        for n, ((q, cs), x, y) in enumerate(zip(QUESTIONS, rows[a], rows[b]), 1):
            got = decide(q, cs, [x, y])
            for c in cs:
                t["決まった" if c in got else "決まらない"] += 1
                t["⛔取り違え"] += (n, c, got.get(c, "")) in BAD
        out[f"{a}×{b}"] = t
    return out


def show(rows: dict) -> None:
    for n, (q, cs) in enumerate(QUESTIONS, 1):
        say(f"{n}：{q}")
        for c in cs:
            ws = {k: v[n - 1].get(c, "") for k, v in rows.items()}
            say(f"   {c}：", " ／ ".join(f"{k}「{w}」{'' if w and check(w, q, c) else '✗'}" for k, w in ws.items()))


def run(args) -> dict:
    ring = KeyRing(args)
    ids, _ = find_models(ring.client())
    rows, errors = {}, {}
    for k in MODELS:
        if k not in ids:
            errors[k] = "モデルが見つからない"
            continue
        try:
            rows[k], used = ring.call(ids[k], lambda c: by_model(c, ids[k], QUESTIONS))
            say(k, f"キー {ring.keys[used].label}")
        except Exception as exc:
            errors[k] = describe_error(exc)
            say(k, "誤りで止まりました：", str(errors[k].get("message", ""))[:200])
        save_partial("G-48", {"rows": rows, "errors": errors})
    show(rows)
    summary = tally(rows)
    for k, v in summary.items():
        say(k, v)
    return {"model_ids": ids, "questions": QUESTIONS, "rows": rows, "errors": errors, "summary": summary}


def rescore(path: str) -> dict:
    d = json.loads(open(path, encoding="utf-8").read())
    show(d["rows"])
    summary = tally(d["rows"])
    for k, v in summary.items():
        say(k, v)
    return {**d, "rescored_from": path, "summary": summary}


def main() -> None:
    require_python()
    p = base_parser(__doc__)
    p.add_argument("--rescore", help="保存した結果を、いまの確かめ（check）と取り違えの一覧（BAD）で数え直す（問い合わせはしない）")
    args = p.parse_args()
    save_result("G-48", rescore(args.rescore) if args.rescore else run(args))
    drop_partial("G-48")


if __name__ == "__main__":
    main()
