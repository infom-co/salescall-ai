"""G-73：ご承認いただけなかったときのシステムの質問（N1・N2・N3）への答えの言葉と、こちらの声が届いていない様子の言葉（HEAR）を、
無料の枠の2つのモデル（3.8 Flash・3.7 Flash＝判定に使わないモデル）で作り、ユーザーにお見せする（2026-11-02）。

✅ 2026-11-02 日本時間5時37分・5時56分 ユーザー：ご納得いただけなかった点を伺い、資料でご説明し、「ほかにご納得いただけない点はございますでしょうか？」
   →「だいたい分かりました」ならお礼で次へ・「なるほど」ならご納得を伺う（N1〜N3。流れの制御 Flow._point_step）。
✅ 2026-11-02 日本時間21時16分 ユーザー：「(ア) OK、お勧めの形で進めてください」＝こちらの声が届いていない様子を判定AIで読む（判定の項目 HEAR）。
✅ 2026-11-02 日本時間6時40分 ユーザー：「測るのは、すべてが整理できてからでしょ！」＝ここでは作るだけ。言葉を見ていただいてから、測りは1回にまとめる。
- 正解：作らせた種類（N1〜N3 は選択肢の名前・「どちらとも取れない」＝決められない／HEAR は判定の項目の選べるもの）。
- ⛔ 自分で作った言葉を判定すると正しさが高く出るので、作るモデルは判定の顔ぶれ（d1-ultra・Jev・Sonnet・3.5 Flash Lite）に入れない。
- 言葉の種類ごとに、1つのモデルへ1回の問い合わせでまとめて答えさせる（0円）。上限に当たったら次のキーで続ける（common.KeyRing）。
"""
from __future__ import annotations

import json

from common import KeyRing, base_parser, describe_error, drop_partial, require_python, save_partial, save_result, say
from flowctl import HEAR_LOST, HEAR_OK, UNDECIDED, callback_question, hear_choices

MODELS = ("3.8 Flash", "3.7 Flash")
PER_KIND = 4   # 種類ごとに作らせる言葉の数
EXPLAINED = "料金は月額3,000円で、初月は無料となっております。"   # 前の段でご説明した内容（試しの業務。ApprovalTest と同じ）
UNSURE = "どちらとも取れない"   # 決められない（UNDECIDED）の種類の名前


def _point_sets() -> list[dict]:
    """N1〜N3：質問の文・選択肢ごとの意味（流れの制御の _ROLE_MEANINGS）と、決められない一言。"""
    from flowctl import _ROLE_MEANINGS
    out = []
    for role, scene in (("not_agreed", "こちらがご説明した内容のご承認を伺ったところ、お客様が「いいえ」と答えたので"),
                        ("other_point", f"お客様の分からなかった点を、こちらが「{EXPLAINED}」とご説明したあとで"),
                        ("agreed_now", f"お客様の分からなかった点を「{EXPLAINED}」とご説明し、お客様が「なるほど」とだけ言ったので")):
        q = callback_question(role)
        kinds = dict(_ROLE_MEANINGS[role])
        kinds[UNSURE] = "受け入れにも断りにも取れて、その言葉だけでは、どれか決められない一言（「なるほど」「うーん」など、相づちや考えている言葉）"
        out.append({"set": q.id, "question": q.text, "scene": scene, "kinds": kinds})
    return out


# こちらの声が届いていない様子：答えの段のいろいろな質問のあとに、お客様が言う言葉（そういう様子の無い、紛らわしい言葉も作らせる）
HEAR_QUESTIONS = ("パンフレットをお送りしてもよろしいでしょうか？", "ご予約はされていますでしょうか？", "ご希望の日時をお聞かせいただけますでしょうか？")


def _hear_set() -> dict:
    kinds = {k: v for k, v in hear_choices(False).items() if k != UNDECIDED}
    kinds[HEAR_OK] += "。⚠️ 紛らわしい言い方にしてください（「聞こえてますよ」「もしもし、はいどうぞ」のように、声や電話の話が入っていても、こちらの声は届いている言葉）"
    return {"set": "HEAR", "questions": HEAR_QUESTIONS, "kinds": kinds}


PROMPT = (
    "電話の営業の会話で、こちらが次の質問をしたあとに、お客様が言う一言を作ってください。種類ごとに、{n}つずつ、違う言い方で書いてください。\n"
    "決まり：電話で実際に言いそうな、短い話し言葉にします。種類の名前そのもの（「点を言った」「分かった」など）は入れません。"
    "同じ言い方を繰り返しません。\n"
)


def _complete(words: list[dict], expected: list[tuple[str, str]]) -> list[dict]:
    """作らせた言葉がそろっているかを確かめる唯一の場所：場面（質問）ごとに、どの種類もちょうど PER_KIND 個（作るモデルが同じ場面・同じ種類を
    2回返し、ほかの種類が抜けると、言葉の正解（作らせた種類）が違う＝⛔ 2026-11-02 の1回目：3.8 Flash が「ご予約はされていますでしょうか？」の
    「届いていない様子」を8個返し、うち4個は「そちらの声聞こえてます」のような、届いている言葉だった）。そろわなければ ValueError（その問い合わせを
    誤りとして残し、--resume で問い合わせ直す）。正解を手で付け替えない。"""
    from collections import Counter
    got = Counter((w["question"], w["kind"]) for w in words)
    bad = [f"{q[:20]}・{k}：{got.get((q, k), 0)}個" for q, k in expected if got.get((q, k), 0) != PER_KIND]
    bad += [f"{q[:20]}・{k}：作らせていない種類" for q, k in got if (q, k) not in expected]
    if bad:
        raise ValueError("言葉がそろっていない（" + "／".join(bad) + "）")
    return words


def _ask_points(client, model: str, sets: list[dict]) -> list[dict]:
    from google.genai import types
    body = "\n".join(f"{i}：{s['scene']}、こちらが「{s['question']}」と伺った。種類：" + "／".join(f"{k}＝{v}" for k, v in s["kinds"].items())
                     for i, s in enumerate(sets, 1))
    item = types.Schema(type=types.Type.OBJECT, properties={
        "no": types.Schema(type=types.Type.INTEGER), "kind": types.Schema(type=types.Type.STRING),
        "words": types.Schema(type=types.Type.ARRAY, items=types.Schema(type=types.Type.STRING))}, required=["no", "kind", "words"])
    schema = types.Schema(type=types.Type.OBJECT, properties={"items": types.Schema(type=types.Type.ARRAY, items=item)}, required=["items"])
    r = client.models.generate_content(model=model, contents=PROMPT.format(n=PER_KIND) + "場面（番号：場面と質問・種類＝その意味）：\n" + body,
                                       config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=schema,
                                                                          temperature=0.7))
    out = []
    for x in json.loads(r.text).get("items", []):
        n, kind = x.get("no"), x.get("kind")
        if isinstance(n, int) and 1 <= n <= len(sets) and kind in sets[n - 1]["kinds"]:
            for w in [w.strip() for w in x.get("words", []) if isinstance(w, str) and w.strip()]:
                out.append({"set": sets[n - 1]["set"], "question": sets[n - 1]["question"], "kind": kind, "text": w, "maker": model,
                            "truth": UNDECIDED if kind == UNSURE else kind})
    return _complete(out, _point_pairs(sets))


def _point_pairs(sets: list[dict]) -> list[tuple[str, str]]:
    return [(s["question"], k) for s in sets for k in s["kinds"]]


def _hear_pairs(s: dict) -> list[tuple[str, str]]:
    return [(q, k) for q in s["questions"] for k in s["kinds"]]


def _ask_hear(client, model: str, s: dict) -> list[dict]:
    from google.genai import types
    body = "\n".join(f"{i}：こちらが「{q}」と伺った" for i, q in enumerate(s["questions"], 1))
    kinds = "／".join(f"{k}＝{v}" for k, v in s["kinds"].items())
    item = types.Schema(type=types.Type.OBJECT, properties={
        "no": types.Schema(type=types.Type.INTEGER), "kind": types.Schema(type=types.Type.STRING, enum=list(s["kinds"])),
        "words": types.Schema(type=types.Type.ARRAY, items=types.Schema(type=types.Type.STRING))}, required=["no", "kind", "words"])
    schema = types.Schema(type=types.Type.OBJECT, properties={"items": types.Schema(type=types.Type.ARRAY, items=item)}, required=["items"])
    r = client.models.generate_content(model=model, contents=PROMPT.format(n=PER_KIND) + f"種類（どの場面にも）：{kinds}\n場面：\n" + body,
                                       config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=schema,
                                                                          temperature=0.7))
    out = []
    for x in json.loads(r.text).get("items", []):
        n, kind = x.get("no"), x.get("kind")
        if isinstance(n, int) and 1 <= n <= len(s["questions"]) and kind in s["kinds"]:
            for w in [w.strip() for w in x.get("words", []) if isinstance(w, str) and w.strip()]:
                out.append({"set": "HEAR", "question": s["questions"][n - 1], "kind": kind, "text": w, "maker": model, "truth": kind})
    return _complete(out, _hear_pairs(s))


def run(args) -> dict:
    ring = KeyRing(args)
    ids = ring.model_ids()
    points, hear = _point_sets(), _hear_set()
    rows: dict = {"points": {}, "hear": {}}
    errors: dict = {}
    if args.resume:   # 途中で止まった流しの続き：答えのあった問い合わせは、もう一度は送らない
        import glob
        from common import HERE
        part = sorted(glob.glob(str(HERE / "results" / "G-73_partial.json")))
        if part:
            pairs = {"points": _point_pairs(points), "hear": _hear_pairs(hear)}
            for key, by in json.loads(open(part[-1], encoding="utf-8").read()).get("rows", {}).items():
                for m, v in by.items():
                    try:   # 残っていた分も同じ確かめを通す（そろっていない分は問い合わせ直す）
                        rows.setdefault(key, {})[m] = _complete(v, pairs[key])
                    except ValueError as exc:
                        say(key, m, "途中の記録を使わず、問い合わせ直します：", str(exc)[:200])
    for m in MODELS:
        if m not in ids:
            errors[m] = "モデルが見つからない"
            continue
        for key, fn in (("points", lambda c, m=m: _ask_points(c, ids[m], points)), ("hear", lambda c, m=m: _ask_hear(c, ids[m], hear))):
            if rows[key].get(m):
                continue
            try:
                got, used = ring.call(ids[m], fn)
                rows[key][m] = got
                say(key, m, f"{len(got)}個（キー {ring.keys[used].label}）")
            except Exception as exc:
                errors[f"{key}・{m}"] = describe_error(exc)
                say(key, m, "誤りで止まりました：", str(errors[f"{key}・{m}"].get("message", ""))[:200])
            save_partial("G-73", {"rows": rows, "errors": errors})
    words = [w for by in rows.values() for got in by.values() for w in got]
    seen, uniq = set(), []
    for w in words:   # 同じ質問・同じ言葉は1つ（作ったモデルが違っても）
        if (w["question"], w["text"]) not in seen:
            seen.add((w["question"], w["text"]))
            uniq.append(w)
    return {"model_ids": {m: ids.get(m) for m in MODELS}, "points": points, "hear": hear, "words": uniq, "errors": errors,
            "count": {s: sum(1 for w in uniq if w["set"] == s) for s in ("N1", "N2", "N3", "HEAR")}}


def main() -> None:
    require_python()
    p = base_parser(__doc__)
    p.add_argument("--resume", action="store_true", help="途中で止まった流しの続き（答えのあった問い合わせは送らない）")
    args = p.parse_args()
    out = run(args)
    save_result("G-73", out)
    if not out["errors"]:   # 止まった問い合わせがあれば、途中の記録を残す（--resume で、止まった分だけ流し直す）
        drop_partial("G-73")


if __name__ == "__main__":
    main()
