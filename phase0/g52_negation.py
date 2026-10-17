"""G-52：相手の言葉の打ち消しと、AIが選んだ選択肢の意味の言葉の打ち消しが食い違えば確かめる決まり（下の negation_conflict）が、正しい答えを
止めないか（文字だけで測る。2026-10-17）。⛔ 結果：作らせた答え756個のうち189個（25%）で正しい答えを止めた ⇒ 採らない（流れの制御には入れない）。

きっかけ：G-14 の残りの誤り「してないです」を、会話AIの聞き取りが「捨てないです」と書き、AIは「はい」（ご予約はされている）を選んだ（4回中2回）。
聞き違えた文字にも打ち消し「ない」が残っている ⇒ 相手の言葉に打ち消しがあるのに、選んだ選択肢の意味の言葉に打ち消しが無ければ、そのまま渡さずに
確かめる（G-14 の報告 951回に当てると、働いたのは48回で48回ともAIが誤って選んだ回＝正しい答えを止めた回0。新しく拾うのは「捨てないです」の2回）。
⚠️ G-14 の試験集は「ご予約はされていますか」の質問だけで、打ち消しの形で受け入れる言い方（「問題ないです」「構いません」）が入っていない。
- 材料：G-48 の31問。選択肢ごとに、その意味の一言の答えを、文字のモデル2つ（3.8 Flash・3.7 Flash）に作らせる。打ち消しの形で受け入れる言い方・
  打ち消しを使わずに断る言い方を、必ず混ぜさせる。意味の言葉は G-48 の結果から取込と同じ決め方で決める（meanings.decide）。
- 数えること：(1) AIが正しい選択肢を選んだとして、決まりが働いてしまう答え（要らない確かめ＝正しい答えを止める）(2) AIが別の選択肢を選んだとして、
  決まりが拾える答え（どれだけ守れるか）。⛔ 一番こわいのは (1) が多いこと＝何度も確かめて相手に不快感を与える。
"""
from __future__ import annotations

import json
from collections import Counter

from common import KeyRing, base_parser, describe_error, drop_partial, require_python, save_partial, save_result, say
from flowctl import Question
from g23_names import find_models
from g48_meanings import QUESTIONS
from meanings import clean, decide, negated

MODELS = ("3.8 Flash", "3.7 Flash")


def negation_conflict(q: Question, cond: str | None, said: str | None) -> bool:
    """試した決まり（採らなかった）：相手の言葉に打ち消しがあるのに、選んだ選択肢の意味の言葉に打ち消しが無い。向きは片方だけ。"""
    words = q.meanings.get(cond) if cond else None
    return bool(words and said and negated(said) and not negated(words))
PROMPT = (
    "電話でお客様に、選択肢のある質問をしました。お客様が、ある選択肢の意味で答えるときの、自然な一言の答えを作ってください。\n"
    "質問の番号ごと・選択肢ごとに、answers に6つ書きます。決まり：\n"
    "- 本当の電話で、お客様が言いそうな、短い話し言葉にします（「はい」「いいえ」で始めないものも混ぜます）。\n"
    "- 6つのうち2つ以上は、打ち消しの形（「〜ない」「〜ません」「〜なく」）を使う言い方にします。その選択肢の意味が受け入れ・認める側なら、"
    "打ち消しの形で受け入れる言い方（「問題ないです」「構いません」「差し支えありません」「なくても大丈夫です」など、質問に合うもの）にします。\n"
    "- その選択肢の意味だけを答えます。ほかの選択肢の意味に取れる言い方・どちらとも取れる言い方は書きません。\n"
    "質問（番号：質問の文／選択肢）：\n"
)


def _schema():
    from google.genai import types

    item = types.Schema(type=types.Type.OBJECT, properties={
        "no": types.Schema(type=types.Type.INTEGER), "choice": types.Schema(type=types.Type.STRING),
        "answers": types.Schema(type=types.Type.ARRAY, items=types.Schema(type=types.Type.STRING))}, required=["no", "choice", "answers"])
    return types.Schema(type=types.Type.OBJECT, properties={"items": types.Schema(type=types.Type.ARRAY, items=item)}, required=["items"])


def generate(client, model: str) -> list[dict]:
    from google.genai import types

    body = "\n".join(f"{n}：{q}／{'・'.join(c)}" for n, (q, c) in enumerate(QUESTIONS, 1))
    r = client.models.generate_content(model=model, contents=PROMPT + body, config=types.GenerateContentConfig(
        response_mime_type="application/json", response_schema=_schema(), temperature=1.0))
    out = [{} for _ in QUESTIONS]
    for x in json.loads(r.text).get("items", []):
        n = x.get("no")
        if isinstance(n, int) and 1 <= n <= len(QUESTIONS) and clean(x.get("choice")) in QUESTIONS[n - 1][1]:
            out[n - 1].setdefault(clean(x.get("choice")), []).extend(a for a in x.get("answers") or [] if isinstance(a, str) and a.strip())
    return out


def meanings_g48() -> list[dict]:
    """G-48 の保存した答えから、取込と同じ決め方で、質問ごとの意味の言葉を決める。"""
    from pathlib import Path

    files = sorted((Path(__file__).parent / "results").glob("G-48_*.json"))
    d = json.loads(files[-1].read_text(encoding="utf-8"))
    rows = {k: {(q, tuple(c)): w for (q, c), w in zip(d["questions"], v)} for k, v in d["rows"].items()}
    return [decide(q, c, [rows[k].get((q, tuple(c)), {}) for k in MODELS if k in rows]) for q, c in QUESTIONS]


def tally(answers: dict, means: list[dict]) -> dict:
    s = Counter()
    stops, catches = [], []
    for k, per_q in answers.items():
        for n, ((qtext, conds), got) in enumerate(zip(QUESTIONS, per_q)):
            q = Question(f"G{n + 1}", qtext, "yes_no" if set(conds) == {"はい", "いいえ"} else "choice", conditions=conds, meanings=means[n])
            for cond, xs in got.items():
                for a in xs:
                    s["答え"] += 1
                    if negation_conflict(q, cond, a):          # AIが正しい選択肢を選んだのに働く＝要らない確かめ
                        s["⛔正しい答えを止めた"] += 1
                        stops.append(f"{qtext}／{a} ⇒ {cond}（{means[n].get(cond)}）")
                    for other in conds:
                        if other == cond:
                            continue
                        s["別の選択肢を選んだ形"] += 1
                        if negation_conflict(q, other, a):
                            s["そのうち拾えた"] += 1
                            catches.append(f"{a} ⇒ {other}")
    out = {**s, "止めた例": stops, "拾えた例（はじめの20）": catches[:20]}
    say({k: v for k, v in out.items() if not isinstance(v, list)})
    for x in stops:
        say("  止めた：", x)
    return out


def run(args) -> dict:
    ring = KeyRing(args)
    ids, _ = find_models(ring.client())
    answers, errors = {}, {}
    for k in MODELS:
        if k not in ids:
            errors[k] = "モデルが見つからない"
            continue
        try:
            answers[k], used = ring.call(ids[k], lambda c: generate(c, ids[k]))
            say(k, f"キー {ring.keys[used].label}", sum(len(x) for g in answers[k] for x in g.values()), "個の答え")
        except Exception as exc:
            errors[k] = describe_error(exc)
            say(k, "誤りで止まりました：", str(errors[k].get("message", ""))[:200])
        save_partial("G-52", {"answers": answers, "errors": errors})
    means = meanings_g48()
    return {"model_ids": ids, "questions": QUESTIONS, "meanings": means, "answers": answers, "errors": errors, "summary": tally(answers, means)}


def rescore(path: str) -> dict:
    d = json.loads(open(path, encoding="utf-8").read())
    means = meanings_g48()
    return {**d, "rescored_from": path, "meanings": means, "summary": tally(d["answers"], means)}


def main() -> None:
    require_python()
    p = base_parser(__doc__)
    p.add_argument("--rescore", help="保存した答えに、いまの決まりを当て直す（問い合わせはしない）")
    args = p.parse_args()
    save_result("G-52", rescore(args.rescore) if args.rescore else run(args))
    drop_partial("G-52")


if __name__ == "__main__":
    main()
