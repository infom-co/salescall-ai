"""G-43：質問の「聞くこと」を、人を介さずに決められるかを測る（topics.py。2026-10-16 ユーザー指示）。

✅ ユーザー：「（名前の読みと聞くことを）人を介さない形で進めてください」
- 使う所：聞き返しの「恐れ入ります。（聞くこと）をもう一度お聞かせいただいてもよろしいでしょうか？」と、最後の1回の
  「お声が聞き取れませんでしたので（聞くこと）をもう一度だけお聞かせいただいてもよろしいでしょうか？」（phrases.ask_again）。
- 材料：答えの型のちがう質問 40問（QUESTIONS）。「（聞くこと）を＋聞く言葉」の形・そうでない形（「何名様で…」「…は可能でしょうか」）・
  聞く言葉に見えて違う形（「資料をお送りしても」「ご確認いただけますでしょうか」「聞いたことは」）を混ぜた。
- 正解の形：ok＝聞き返しに入れて自然な言い方（どれか1つに当たればよい）。"" を含む質問は、空（聞くことを言わずに聞き返す）でもよい。
- 数え方：正しい／空にした（"" を含まない質問で空＝聞き返しはできるが、何を聞くかを言えない＝安全な取りこぼし）／
  ⛔誤り（ok に無い言い方を入れた＝不自然な・意味の違う聞き返しになりうる）。⛔ 誤りを0にすることを一番にする。
  ⚠️ ok に無い言い方でも自然なものはありうる。誤りは1つずつ書き出して見る（数だけで決めない）。
1日の上限（Flash は各20回）に収まるよう、1回の問い合わせで全部の質問を見させる。上限に当たったら次のキーで続ける（common.KeyRing）。
"""
from __future__ import annotations

import itertools
import json
import time

from common import base_parser, call_with_retry, describe_error, drop_partial, make_client, pick_key, require_python, save_partial, \
    save_result, say
from g23_names import find_models
from topics import by_model, by_rule, check, decide

# (質問の文, 正しい言い方（"" は空でもよい）)
QUESTIONS = [
    ("ご希望の日時をお聞かせいただけますでしょうか？", ["ご希望の日時"]),
    ("ご来店の人数をお伺いしてもよろしいでしょうか？", ["ご来店の人数"]),
    ("ご予約はされていますでしょうか？", ["", "ご予約の有無", "ご予約のご状況"]),
    ("何名様でお越しになりますでしょうか？", ["", "ご来店の人数", "お越しになる人数", "ご来店人数", "人数", "お越しの人数", "ご人数"]),
    ("ご宿泊は何泊のご予定でしょうか？", ["", "ご宿泊の日数", "ご宿泊数", "ご宿泊の泊数", "ご宿泊のご予定", "ご宿泊日数"]),
    ("ご担当者様のお名前をお伺いしてもよろしいでしょうか？", ["ご担当者様のお名前"]),
    ("お電話番号を教えていただけますでしょうか？", ["お電話番号"]),
    ("ご住所をお伺いしてもよろしいでしょうか？", ["ご住所"]),
    ("メールアドレスをお教えいただけますでしょうか？", ["メールアドレス"]),
    ("ご予算はおいくらくらいでしょうか？", ["", "ご予算"]),
    ("現在お使いの複合機は何台でしょうか？", ["", "複合機の台数", "お使いの複合機の台数", "現在お使いの複合機の台数", "ご使用の複合機の台数"]),
    ("従業員数をお伺いしてもよろしいでしょうか？", ["従業員数"]),
    ("ご都合のよろしい日時はございますでしょうか？", ["", "ご都合のよろしい日時", "ご都合のよい日時", "ご都合のよろしい日"]),
    ("折り返しのご連絡先をお伺いできますでしょうか？", ["折り返しのご連絡先"]),
    ("お子様の学年を教えていただけますでしょうか？", ["お子様の学年"]),
    ("体験授業へのご参加は可能でしょうか？", ["", "体験授業へのご参加", "体験授業へのご参加の可否", "ご参加の可否"]),
    ("いつ頃のご入居をお考えでしょうか？", ["", "ご入居の時期", "ご入居のご予定", "ご入居時期", "ご入居のご希望時期", "ご入居予定の時期"]),
    ("駐車場は何台分必要でしょうか？", ["", "必要な駐車場の台数", "駐車場の台数", "駐車場の必要台数", "必要な駐車場の台数分", "駐車場の必要な台数"]),
    ("今お使いの回線の会社を教えていただけますでしょうか？", ["今お使いの回線の会社"]),
    ("資料をお送りしてもよろしいでしょうか？", ["", "資料の送付", "資料の送付の可否", "資料のご送付"]),  # 「資料をもう一度お聞かせ」は誤り
    ("次回のご来店のご予定はございますでしょうか？", ["", "次回のご来店のご予定"]),
    ("お車の走行距離をお伺いしてもよろしいでしょうか？", ["お車の走行距離"]),
    ("お名前を頂戴してもよろしいでしょうか？", ["お名前"]),
    ("お名前をフルネームでお伺いしてもよろしいでしょうか？", ["お名前", "フルネームのお名前", "お名前のフルネーム"]),
    ("差し支えなければご年齢をお伺いしてもよろしいでしょうか？", ["ご年齢"]),
    ("今回のご相談の内容を簡単にお聞かせいただけますでしょうか？", ["今回のご相談の内容", "ご相談の内容"]),
    ("ご決裁はどなたがされますでしょうか？", ["", "ご決裁者", "ご決裁者様", "ご決裁される方", "ご決裁をされる方", "決裁者"]),
    ("現在のご契約は何年目でしょうか？", ["", "現在のご契約の年数", "ご契約の年数", "ご契約年数", "ご契約の年目"]),
    ("ご利用中のサービスの月額をお伺いしてもよろしいでしょうか？", ["ご利用中のサービスの月額"]),
    ("ご検討の時期はいつ頃でしょうか？", ["", "ご検討の時期", "ご検討時期"]),
    ("ほかの会社とも比べていらっしゃいますでしょうか？", ["", "ほかの会社との比較", "他社との比較", "他社とのご比較", "ほかの会社とのご比較"]),
    ("ご連絡のつきやすい時間帯をお知らせいただけますでしょうか？", ["ご連絡のつきやすい時間帯"]),
    ("ご希望のお部屋の広さをお伺いしてもよろしいでしょうか？", ["ご希望のお部屋の広さ"]),
    ("来週のご都合はいかがでしょうか？", ["", "来週のご都合"]),
    ("ご住所の郵便番号から教えていただけますでしょうか？", ["", "ご住所の郵便番号", "郵便番号"]),
    ("担当の方のお名前と部署をお伺いしてもよろしいでしょうか？", ["担当の方のお名前と部署"]),
    ("お支払い方法はカードと振込のどちらがよろしいでしょうか？", ["", "お支払い方法", "ご希望のお支払い方法"]),
    ("ご注文の数をお伺いしてもよろしいでしょうか？", ["ご注文の数"]),
    ("ご契約の内容をご確認いただけますでしょうか？", [""]),              # お客様に確かめていただくお願い＝聞く質問ではない
    ("このサービスを聞いたことはございますでしょうか？", ["", "このサービスのご存知の有無"]),  # 「サービスをもう一度お聞かせ」は誤り
    ("お使いになっている回線を教えていただけますでしょうか？", ["お使いになっている回線", "お使いの回線", "ご利用の回線"]),
]
MODELS = {"3.8 Flash": 2, "3.7 Flash": 2, "3.6 Flash": 2, "3.5 Flash Lite": 2}


def judge(ok: list[str], topic: str) -> str:
    if not topic:
        return "正しい" if "" in ok else "空にした"
    return "正しい" if topic in ok else "誤り"


def tally(topics: list[str]) -> dict:
    out = {"正しい": 0, "空にした": 0, "誤り": 0, "誤りの例": [], "空の例": []}
    for (q, ok), t in zip(QUESTIONS, topics):
        j = judge(ok, t)
        out[j] += 1
        if j == "誤り":
            out["誤りの例"].append(f"{q} ⇒ 「{t}」")
        elif j == "空にした":
            out["空の例"].append(q)
    return out


def compare(raw: dict) -> dict:
    runs = {k: v["rows"] for k, v in raw.items() if "rows" in v}
    rule = [by_rule(q) for q, _ in QUESTIONS]
    out = {"決まりだけ": tally(rule)}
    for k, v in runs.items():
        out[f"{k}だけ（確かめあり）"] = tally([t if check(t) else "" for t in v])
        out[f"決まり→{k}（確かめあり）"] = tally([r or (t if check(t) else "") for r, t in zip(rule, v)])
    for a, b in itertools.combinations(runs, 2):  # topics.decide そのもの
        out[f"決まり→{a}×{b}"] = tally([decide(q, [x, y]) for (q, _), x, y in zip(QUESTIONS, runs[a], runs[b])])
    return out


def show(cmp: dict, examples: bool = True) -> None:
    for k, v in cmp.items():
        say(k, f"正しい {v['正しい']}／空 {v['空にした']}／⛔誤り {v['誤り']}", v["誤りの例"] if examples else "")


def run(args) -> dict:
    out = {"questions": [{"question": q, "ok": ok, "rule": by_rule(q)} for q, ok in QUESTIONS]}
    if args.dry_run:
        out["compare"] = compare({})
        show(out["compare"])
        return out
    client = make_client(pick_key(args))
    ids, flash_names = find_models(client)
    out["model_ids"], out["flash_display_names"] = ids, flash_names
    plan = [(k, rep) for k, n in MODELS.items() if k in ids and (not args.model_key or k in args.model_key) for rep in range(n)]
    raw = {}
    for k, rep in sorted(plan, key=lambda x: x[1]):
        name = f"{k}#{rep}"
        try:
            got, retries = call_with_retry(lambda: by_model(client, ids[k], [q for q, _ in QUESTIONS]))
            raw[name] = {"rows": got, "retries": retries}
            t = tally([x if check(x) else "" for x in got])
            say(name, f"正しい {t['正しい']}／空 {t['空にした']}／⛔誤り {t['誤り']}", t["誤りの例"])
        except Exception as exc:
            raw[name] = {"error": describe_error(exc)}
            msg = raw[name]["error"].get("message", "")
            say(name, "誤りで止まりました：", msg[:200])
            if "RESOURCE_EXHAUSTED" in msg or "429" in str(raw[name]["error"].get("code", "")):
                say("⛔ 上限に当たったので、ここで止めます（やり直さない）")
                break
        save_partial("G-43", {"raw": raw})
        time.sleep(args.pause)
    out["raw"] = raw
    out["compare"] = compare(raw)
    show(out["compare"], examples=False)
    return out


def rescore(path: str) -> dict:
    d = json.loads(open(path, encoding="utf-8").read())
    out = {"rescored_from": path, "questions": [{"question": q, "ok": ok, "rule": by_rule(q)} for q, ok in QUESTIONS],
           "model_ids": d.get("model_ids"), "raw": d["raw"], "compare": compare(d["raw"])}
    show(out["compare"], examples=False)
    return out


def main() -> None:
    require_python()
    p = base_parser(__doc__)
    p.add_argument("--pause", type=float, default=15.0)
    p.add_argument("--model-key", choices=list(MODELS), action="append")
    p.add_argument("--rescore", help="保存した結果を、いまの決め方と正解で数え直す（問い合わせはしない）")
    args = p.parse_args()
    save_result("G-43", rescore(args.rescore) if args.rescore else run(args))
    drop_partial("G-43")


if __name__ == "__main__":
    main()
