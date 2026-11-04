"""質問が「ほかに分からない点が無いかを伺う質問か」と、その「無い」にあたる選択肢を、質問の文と選択肢から決める（取込のとき。人は確かめない）。

✅ 2026-11-04 日本時間20時27分 ユーザー：「(1) トークスクリプトの「ご不明な点は…」の重ね聞き：「この質問は、ほかに分からない点が無いかを伺う質問か」を
決めます。（ご不明な点は無い）として扱い、重ねて伺いません。OK」（19時26分のご質問「お客様「だいたい分かりました。」の後に、こちら「ありがとうございます。
それではご不明な点はございますでしょうか？」となぜ、再度、不明点を確認するんですか？」）。

使う所：flowctl.Question.unclear_none（ご承認いただけなかったときの手順で「ほかにご納得いただけない点は…」に「分かった」と答えていただいたら、その質問は
伺わずに、この答えにする＝flowctl.Flow._no_doubts_step の1か所）。

決め方（facts.decide と同じ形）：文字のモデル2つに、1回の問い合わせでまとめて答えさせる（by_model）。2つの答えがそろい、「無い」にあたる選択肢がその
質問の選択肢にあるときだけ決まる。そろわない・答えが無いときは空（伺う）。⚠️ どちらに倒しても誤りは残りうる：ほかの質問を、分からない点が無いかを伺う
質問と取り違えると、伺うべき答えを伺わずに決めてしまう（依頼者へ誤った答えが渡る）。取り違えなければ、重ねて伺うだけ（お客様には同じことを2回伺うが、
答えは正しい）⇒ 誤った答えの重いほう（伺わずに決める）を避ける側に倒す。
"""
from __future__ import annotations

import json

PROMPT = (
    "電話でお客様に聞く質問の一覧です。質問ごとに、次の2つを答えてください。\n"
    "- asks：この質問が、お客様に、分からない点・気になる点・確かめたいことがほかに無いかを伺う質問か（true／false）。\n"
    "- none：asks が true のときは、選択肢のうち「分からない点は無い」という意味になる選択肢の名前を、選択肢に書かれたとおりに1つ書きます。"
    "asks が false のときは空の文字を書きます。\n"
    "決まり：\n"
    "- 質問の文の意味だけで決めます。\n"
    "- 「無いでしょうか」と打ち消しの形で伺う質問では、「はい」が「分からない点は無い」の意味になります。\n"
    "質問（番号：質問の文／選択肢）：\n"
)


def _body(questions: list[tuple[str, tuple]]) -> str:
    return "\n".join(f"{n}：{text}／選択肢：{'・'.join(conds)}" for n, (text, conds) in enumerate(questions, 1))


def _schema():
    from google.genai import types

    item = types.Schema(type=types.Type.OBJECT, properties={
        "no": types.Schema(type=types.Type.INTEGER, description="質問の番号"),
        "asks": types.Schema(type=types.Type.BOOLEAN, description="ほかに分からない点が無いかを伺う質問か"),
        "none": types.Schema(type=types.Type.STRING, description="「分からない点は無い」にあたる選択肢の名前（無ければ空）"),
    }, required=["no", "asks", "none"])
    return types.Schema(type=types.Type.OBJECT, properties={"items": types.Schema(type=types.Type.ARRAY, items=item)},
                        required=["items"])


def by_model(client, model: str, questions: list[tuple[str, tuple]]) -> list[dict | None]:
    """1回の問い合わせでまとめて答えさせる（取込は業務の版ごとに1回）。questions（(質問の文, 選択肢の並び)）と同じ並びの答え（無ければ None）を返す。"""
    from google.genai import types

    r = client.models.generate_content(
        model=model, contents=PROMPT + _body(questions),
        config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=_schema(), temperature=0))
    return _read_items(json.loads(r.text).get("items", []), len(questions))


def _read_items(items: list, n: int) -> list[dict | None]:
    """答えの items を、質問と同じ並び（1〜n 番）の {"asks", "none"}（無い・形の違うものは None）にそろえる（読み取りの唯一の場所＝Gemini も Anthropic も）。"""
    got = {x.get("no"): x for x in items or [] if isinstance(x, dict)}
    out = []
    for i in range(1, n + 1):
        x = got.get(i)
        ok = x is not None and isinstance(x.get("asks"), bool) and isinstance(x.get("none"), str)
        out.append({"asks": x["asks"], "none": x["none"]} if ok else None)
    return out


def by_model_anthropic(client, model: str, questions: list[tuple[str, tuple]]) -> list[dict | None]:
    """by_model と同じ文（PROMPT）・同じ読み取りを、Anthropic（Claude Haiku 5.5）に、答えの形を指定した道具の呼び出しで返させる（facts.by_model_anthropic と
    同じ作り）。料金は判定と同じ帳簿（meaning_judge.SPEND）。⚠️ temperature は送らない（Haiku 5.5 は断る）。"""
    from meaning_judge import SPEND

    item = {"type": "object", "properties": {"no": {"type": "integer"}, "asks": {"type": "boolean"}, "none": {"type": "string"}},
            "required": ["no", "asks", "none"], "additionalProperties": False}
    schema = {"type": "object", "properties": {"items": {"type": "array", "items": item}}, "required": ["items"], "additionalProperties": False}
    SPEND.check()
    msg = client.messages.create(model=model, max_tokens=4000, messages=[{"role": "user", "content": PROMPT + _body(questions)}],
                                 tools=[{"name": "unclear", "description": "質問ごとの、ほかに分からない点が無いかを伺う質問か",
                                         "input_schema": schema, "strict": True}],
                                 tool_choice={"type": "tool", "name": "unclear"})
    SPEND.add(model, msg.usage)
    got = next((b.input for b in msg.content if getattr(b, "type", "") == "tool_use"), None)
    if not isinstance(got, dict):
        raise ValueError(f"{model} が答えの形で返さなかった（{getattr(msg, 'stop_reason', '')}）")
    return _read_items(got.get("items", []), len(questions))


def decide(conditions: tuple, model_rows: list[dict | None]) -> str:
    """「無い」にあたる選択肢を1つに決める（取込のとき。人は確かめない）：2つ以上のモデルの答えがそろって「伺う質問」で、同じ選択肢を挙げ、その選択肢が
    質問の選択肢にあるときだけ、その名前。それ以外は空（伺う）。"""
    if len(model_rows) < 2 or any(r is None or not r["asks"] for r in model_rows):
        return ""
    nones = {r["none"] for r in model_rows}
    return next(iter(nones)) if len(nones) == 1 and next(iter(nones)) in conditions else ""
