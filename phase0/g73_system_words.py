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
from flowctl import HEAR_OK, UNDECIDED, callback_question, hear_choices

# 作り手（判定の顔ぶれ＝meaning_judge.members(JUDGE2) と 3.5 Flash Lite に入れない）。✅ 2026-11-03 日本時間2時41分10秒 ユーザー「はい」＝
# 2人目の作り手を 3.7 Flash（Google の混雑で答えなかった）から Claude Haiku 5.5 に替える（Sonnet は判定に入っているので作り手にしない。
# 1回 約0.4円・上限1円＝--budget-yen 1）
MODELS = ("3.8 Flash", "Claude Haiku 5.5")
# 種類のそろわない場面を問い合わせ直す回数。混雑（503）・接続の切れの待ちと問い合わせ直しは、キーを回す所の1か所（common.call_with_retry）だけ
# ⛔ 2026-11-02：ここにも混雑の問い合わせ直し（8回）を足し、中の3回と重ねて1回の作りで最大32回を使いうる形にした＝2本のキーの1日の20回を、
#    3.7 Flash は1つも答えないまま使い切った（混雑で答えなかった問い合わせも、1日の回数に数えられていた様子）
SCENE_TRIES = 3
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
# お呼びする段（判定の項目は両方の段で読む＝flowctl.SIDE_ITEMS）：電話に出た方が、呼ぶ一言・確かめる一言のあとに言う言葉（組 HEAR_CALL＝G-59 で測る）
CALLING_SCENES = {False: "電話に出た方に、会社名と名前を名乗ってから、", True: "電話に出た方に、お呼びした方ご本人かを確かめるため、"}


def _hear_kinds(calling: bool) -> dict:
    kinds = {k: v for k, v in hear_choices(calling).items() if k != UNDECIDED}
    kinds[HEAR_OK] += "。⚠️ 紛らわしい言い方にしてください（「聞こえてますよ」「もしもし、はいどうぞ」のように、声や電話の話が入っていても、こちらの声は届いている言葉）"
    return kinds


def calling_questions() -> dict[bool, str]:
    """お呼びする段の2つの一言（{確かめる一言か: 判定に見せる質問の文}）。お呼びする段の測り（G-59）が判定に見せる質問と同じ1か所（question_of）。"""
    import g59_calling_judge as g
    _, f, _ = g.flows()
    return {asked: g.question_of(f, asked).text for asked in CALLING_SCENES}


PROMPT = (
    "電話の営業の会話で、こちらが次の質問をしたあとに、お客様が言う一言を作ってください。種類ごとに、{n}つずつ、違う言い方で書いてください。\n"
    "決まり：電話で実際に言いそうな、短い話し言葉にします。種類の名前そのもの（「点を言った」「分かった」など）は入れません。"
    "同じ言い方を繰り返しません。答えは、場面の番号の欄の中の、種類の名前の欄に入れます（その種類の言葉だけを入れる）。\n"
)


def _scenes() -> list[dict]:
    """言葉を作らせる場面の一覧（唯一の場所）：N1〜N3 の質問（場面の文つき）と、こちらの声が届いていない様子を読む答えの段の3つの質問・
    お呼びする段の2つの一言。"""
    out = [{"set": s["set"], "question": s["question"], "text": f"{s['scene']}、こちらが「{s['question']}」と伺った", "kinds": s["kinds"]}
           for s in _point_sets()]
    out += [{"set": "HEAR", "question": q, "text": f"こちらが「{q}」と伺った", "kinds": _hear_kinds(False)} for q in HEAR_QUESTIONS]
    out += [{"set": "HEAR_CALL", "question": q, "asked": asked, "text": f"{CALLING_SCENES[asked]}こちらが「{q}」と伺った", "kinds": _hear_kinds(True)}
            for asked, q in calling_questions().items()]
    return out


def _request(scenes: list[dict]) -> tuple[str, dict]:
    """作らせる文と答えの形（JSON の形）の唯一の場所：場面ごと・種類ごとの欄に分けた形で答えさせる（同じ種類を2回返して、ほかの種類が抜ける形を
    作らない＝⛔ 2026-11-02：項目の並びの形では、3.8 Flash が聞こえ方の場面の1つで「届いていない様子」を8個・「そういう様子は無い」を0個と
    2回続けて返し、8個のうち4個は届いている言葉だった）。"""
    words = {"type": "array", "items": {"type": "string"}}
    # additionalProperties＝ほかの欄を足させない（Anthropic の strict の答えの形に要る。Gemini の形にする所＝_gemini_schema は読まない）
    props = {str(i): {"type": "object", "properties": {k: words for k in sc["kinds"]}, "required": list(sc["kinds"]), "additionalProperties": False}
             for i, sc in enumerate(scenes, 1)}
    body = "\n".join(f"{i}：{sc['text']}。種類：" + "／".join(f"{k}＝{v}" for k, v in sc["kinds"].items()) for i, sc in enumerate(scenes, 1))
    return (PROMPT.format(n=PER_KIND) + "場面（番号：場面と質問・種類＝その意味）：\n" + body,
            {"type": "object", "properties": props, "required": list(props), "additionalProperties": False})


def _gemini_schema(js: dict):
    """答えの形（JSON の形）を Gemini の形に（_request の1か所から作る）。"""
    from google.genai import types
    kind = {"object": types.Type.OBJECT, "array": types.Type.ARRAY, "string": types.Type.STRING}[js["type"]]
    return types.Schema(type=kind, **({"properties": {k: _gemini_schema(v) for k, v in js["properties"].items()}, "required": js["required"]}
                                      if "properties" in js else {}), **({"items": _gemini_schema(js["items"])} if "items" in js else {}))


def _ask(client, model: str, scenes: list[dict]) -> list[dict]:
    """Gemini（3.8 Flash）に作らせる。"""
    from google.genai import types
    text, js = _request(scenes)
    r = client.models.generate_content(model=model, contents=text,
                                       config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=_gemini_schema(js),
                                                                          temperature=0.7))
    return _words(json.loads(r.text), scenes, model)


def _ask_anthropic(client, model: str, scenes: list[dict]) -> list[dict]:
    """Anthropic（Claude Haiku 5.5）に作らせる：同じ文と答えの形（_request）を、答えの形を指定した道具の呼び出しで返させる。料金は判定と同じ
    帳簿（meaning_judge.SPEND・上限は --budget-yen）。"""
    from meaning_judge import SPEND
    text, js = _request(scenes)
    SPEND.check()
    # ⚠️ Claude Haiku 5.5 は temperature などを送ると断る（判定の問い合わせの中身 meaning_judge.other_request に書いてある＝2026-10-18。
    #    ⛔ 2026-11-03：読まずに temperature を付け、問い合わせる前に断られた）。strict＝答えの形を守らせる（判定と同じ）
    msg = client.messages.create(model=model, max_tokens=8000, messages=[{"role": "user", "content": text}],
                                 tools=[{"name": "words", "description": "場面ごと・種類ごとに作った言葉", "input_schema": js, "strict": True}],
                                 tool_choice={"type": "tool", "name": "words"})
    SPEND.add(model, msg.usage)
    got = next((b.input for b in msg.content if getattr(b, "type", "") == "tool_use"), None)
    if not isinstance(got, dict):
        raise ValueError(f"{model} が答えの形で返さなかった（{getattr(msg, 'stop_reason', '')}）")
    return _words(got, scenes, model)


def _words(got: dict, scenes: list[dict], model: str) -> list[dict]:
    """答え（場面の番号 ⇒ 種類 ⇒ 言葉の並び）を、言葉の行に（読み取りの唯一の場所）。"""
    out = []
    for i, sc in enumerate(scenes, 1):
        for k in sc["kinds"]:
            for w in [w.strip() for w in ((got.get(str(i)) or {}).get(k) or []) if isinstance(w, str) and w.strip()]:
                out.append({"set": sc["set"], "question": sc["question"], "kind": k, "text": w, "maker": model,
                            "truth": UNDECIDED if k == UNSURE else k, **({"asked": sc["asked"]} if "asked" in sc else {})})
    return out


def _kept(words: list[dict], scenes: list[dict]) -> tuple[list[dict], list[dict]]:
    """そろった場面の言葉と、そろわない場面を分ける唯一の場所：場面は、どの種類にも PER_KIND 個以上あるときだけ残し、種類ごとに初めの
    PER_KIND 個を使う。そろわない場面は言葉を1つも使わない（種類の付け違いが混ざりうる＝正解を手で付け替えない）。作らせていない種類の言葉は捨てる。"""
    kept, missing = [], []
    for sc in scenes:
        by = {k: [w for w in words if w["question"] == sc["question"] and w["kind"] == k] for k in sc["kinds"]}
        if all(len(v) >= PER_KIND for v in by.values()):
            kept += [w for v in by.values() for w in v[:PER_KIND]]
        else:
            missing.append(sc)
    return kept, missing


def _anthropic(args) -> dict:
    """Anthropic の作り手（OTHER_JUDGES の名前 ⇒ モデルID）と、問い合わせる口。判定と同じ鍵（ANTHROPIC_JUDGE_KEY）・同じ料金の帳簿。"""
    from common import LEDGERS, load_secret
    from meaning_judge import OTHER_JUDGES, SPEND
    names = [m for m in _makers(args) if OTHER_JUDGES.get(m, {}).get("maker") == "anthropic"]
    if not names:
        return {}
    import anthropic
    client = anthropic.Anthropic(api_key=load_secret(OTHER_JUDGES[names[0]]["key"], args.env), max_retries=2, timeout=120)
    SPEND.limit = args.budget_yen
    if SPEND not in LEDGERS:
        LEDGERS.append(SPEND)
    return {m: (OTHER_JUDGES[m]["model"], lambda sc, m=m: _ask_anthropic(client, OTHER_JUDGES[m]["model"], sc)) for m in names}


def _makers(args) -> tuple[str, ...]:
    """この流しで問い合わせる作り手（--makers。既定は全部）。"""
    return tuple(getattr(args, "makers", None) or MODELS)


def run(args) -> dict:
    ring = KeyRing(args)
    others = _anthropic(args)
    ids = {**ring.model_ids(), **{m: mid for m, (mid, _) in others.items()}}
    scenes = _scenes()
    rows: dict[str, list[dict]] = {}
    errors: dict = {}
    if args.resume:   # 途中で止まった流しの続き：そろった場面は、もう一度は問い合わせない（前の形の rows＝{points|hear: {モデル: 言葉}} も読む）
        from common import HERE
        part = HERE / "results" / "G-73_partial.json"
        if part.exists():
            old = json.loads(part.read_text(encoding="utf-8")).get("rows", {})
            for key, by in old.items():
                if isinstance(by, dict):
                    for m, v in by.items():
                        rows.setdefault(m, []).extend(v)
                else:
                    rows.setdefault(key, []).extend(by)
    for m in MODELS:
        if m not in _makers(args):   # この流しで問い合わせない作り手も、足りない場面は「そろっていない」として残す（有料の測りの前の止め＝require_complete）
            rows[m], missing = _kept(rows.get(m, []), scenes)
            if missing:
                errors[m] = "この流しでは問い合わせていない（--makers）。そろわない場面：" + "・".join(sc["question"] for sc in missing)
            continue
        if m not in ids:
            errors[m] = "モデルが見つからない"
            continue
        kept, missing = _kept(rows.get(m, []), scenes)
        rows[m] = kept
        if not missing:
            continue
        for _ in range(SCENE_TRIES):
            try:
                if m in others:
                    got, used = others[m][1](missing), None
                else:
                    got, used = ring.call(ids[m], lambda c, sc=missing: _ask(c, ids[m], sc))
            except Exception as exc:   # 混雑・接続の切れは ring.call の中で待って問い合わせ直したあと（上限は次のキーへ回したあと）
                errors[m] = describe_error(exc)
                say(m, "誤りで止まりました：", str(errors[m].get("message", ""))[:200])
                break
            more, missing = _kept(got, missing)
            rows[m] += more
            errors.pop(m, None)
            say(m, f"{len(more)}個" + (f"（キー {ring.keys[used].label}）" if used is not None else ""),
                *(["そろわない場面：" + "・".join(sc["question"][:12] for sc in missing)] if missing else []))
            if not missing:
                break
            errors[m] = "そろわない場面：" + "・".join(sc["question"] for sc in missing)
        save_partial("G-73", {"rows": rows, "errors": errors})
    seen, uniq = set(), []
    for m in MODELS:
        for w in rows.get(m, []):   # 同じ質問・同じ言葉は1つ（作ったモデルが違っても）
            if (w["question"], w["text"]) not in seen:
                seen.add((w["question"], w["text"]))
                uniq.append(w)
    return {"model_ids": {m: ids.get(m) for m in MODELS}, "scenes": scenes, "words": uniq, "errors": errors,
            "count": {s: sum(1 for w in uniq if w["set"] == s) for s in ("N1", "N2", "N3", "HEAR", "HEAR_CALL")}}


def latest() -> dict:
    """測りの道具（G-53・G-59）が試しの言葉を読む唯一の口：いちばん新しい結果の {"path", "words", "errors"}（無ければ空）。errors があれば、
    そろっていない（作れなかったモデル・場面がある）＝測りの道具は、ユーザーがその形で測ると選んだときだけ使う（g53/g59 の --g73-partial）。"""
    from common import HERE
    files = sorted((HERE / "results").glob("G-73_2*.json"))
    if not files:
        return {"path": None, "words": [], "errors": {}}
    d = json.loads(files[-1].read_text(encoding="utf-8"))
    return {"path": files[-1].name, "words": d["words"], "errors": d.get("errors") or {}}


def require_complete(args, what: str, stop: bool = True) -> None:
    """有料の測りの前に：試しの言葉がそろっていなければ止める（⛔ 2026-11-02：作れなかった分を残したまま、いちばん新しい結果を黙って使いうる）。
    stop が偽（組み立てだけ・数えるだけ）なら、そろっているかを出すだけ。"""
    got = latest()
    say(f"試しの言葉（G-73）：{got['path']}・{len(got['words'])}個", *([f"そろっていない：{got['errors']}"] if got["errors"] else []))
    if stop and got["errors"] and not getattr(args, "g73_partial", False):
        raise SystemExit(f"{what}：試しの言葉がそろっていません（--g73-partial を付けるのは、ユーザーがこの形で測ると選んだときだけ）")


def main() -> None:
    require_python()
    p = base_parser(__doc__)
    p.add_argument("--resume", action="store_true", help="途中で止まった流しの続き（そろった場面は問い合わせない）")
    p.add_argument("--makers", nargs="+", choices=MODELS, help="問い合わせる作り手だけ（既定は全部。混んでいる作り手を外して流すとき）")
    p.set_defaults(budget_yen=1.0)   # ✅ 2026-11-03 日本時間2時41分10秒 ユーザー「はい」＝Haiku 5.5 で作る（約0.4円・上限1円）
    args = p.parse_args()
    out = run(args)
    save_result("G-73", out)
    if not out["errors"]:   # 止まった問い合わせがあれば、途中の記録を残す（--resume で、そろわない場面だけ流し直す）
        drop_partial("G-73")


if __name__ == "__main__":
    main()
