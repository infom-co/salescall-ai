"""取込のとき文字のAIに、質問（台本）ごとの答えの並びを、答えの形を決めて1回でまとめて答えさせる問い合わせの唯一の場所（Gemini・Anthropic とも）。

⛔ 2026-11-05 まで、答えの形の指定・問い合わせ・答えの取り出しを、決め方ごとに別々に書いていた（facts・unclear・meanings の by_model・by_model_anthropic・
by_model_replies・topics・units・names の写し。同じ形を足すたびに写しが増える＝一本化の漏れ。帳面 CL-1.md 1 の一本化の残り）。
決め方ごとに持つのは、問いの文・答えの欄（名前・型・説明）・読み取り（並べ直し・そろえ方）だけ。

- 送り先は部品の作りで分ける（呼ぶ側は分けない）：Anthropic の部品（messages.create がある）＝答えの形を指定した道具の呼び出し（strict。欄は全部
  欠かせない・欄の説明は送らない＝前の形のまま・temperature は送らない＝Haiku 5.5 は断る・料金は判定と同じ帳簿 meaning_judge.SPEND）／
  Gemini の部品（models.generate_content）＝答えの形（JSON の型。欄の説明つき）と temperature 0。
- 欄の型：「integer」「boolean」「string」・("enum", 並び)＝決まった言葉のどれか・("array", "string")＝文字の並び。
"""
from __future__ import annotations

import json

Field = tuple   # (型, 説明 または None)


def _gemini_schema(key: str, fields: dict[str, Field], required: list[str]):
    from google.genai import types

    base = {"integer": types.Type.INTEGER, "boolean": types.Type.BOOLEAN, "string": types.Type.STRING}

    def one(kind, desc):
        kw = {"description": desc} if desc else {}
        if isinstance(kind, tuple) and kind[0] == "enum":
            return types.Schema(type=types.Type.STRING, enum=list(kind[1]), **kw)
        if isinstance(kind, tuple) and kind[0] == "array":
            return types.Schema(type=types.Type.ARRAY, items=types.Schema(type=base[kind[1]]), **kw)
        return types.Schema(type=base[kind], **kw)
    item = types.Schema(type=types.Type.OBJECT, properties={n: one(*f) for n, f in fields.items()}, required=required)
    return types.Schema(type=types.Type.OBJECT, properties={key: types.Schema(type=types.Type.ARRAY, items=item)}, required=[key])


def _anthropic_schema(key: str, fields: dict[str, Field]) -> dict:
    def one(kind):
        if isinstance(kind, tuple) and kind[0] == "enum":
            return {"type": "string", "enum": list(kind[1])}
        if isinstance(kind, tuple) and kind[0] == "array":
            return {"type": "array", "items": {"type": kind[1]}}
        return {"type": kind}
    item = {"type": "object", "properties": {n: one(f[0]) for n, f in fields.items()}, "required": list(fields), "additionalProperties": False}
    return {"type": "object", "properties": {key: {"type": "array", "items": item}}, "required": [key], "additionalProperties": False}


def ask(client, model: str, prompt: str, fields: dict[str, Field], *, required: list[str] | None = None, key: str = "items",
        tool: tuple[str, str] = ("items", "質問ごとの答え"), max_tokens: int = 8000, think: bool = False) -> list:
    """prompt を1回送り、答えの並び（key の中身。読み取り・並べ直しは呼ぶ側）を返す。fields＝{欄の名前: (型, 説明)}（並びのまま送る）。
    required＝Gemini の答えの形で欠かせない欄（無ければ全部。Anthropic は strict なので全部）。tool＝Anthropic の道具の (名前, 説明)。
    think＝Anthropic を考える形で聞く（考える段を入れ、道具を使わず返事の形を決めて JSON で返させる＝判定の「考える Sonnet」と同じ送り方
    meaning_judge.OTHER_JUDGES。名前を指定した道具の呼び出しは考える段を飛ばすため）。2つの文字のAIがそろわなかった文だけを聞く所（import_ai.decide_since）。
    Anthropic が答えの形で返さなければ誤りを上げる（呼ぶ側＝import_ai._each が、そのモデルの答えは無いとして決める）。"""
    if hasattr(client, "messages"):
        from meaning_judge import SPEND

        SPEND.check()
        if think:
            msg = client.messages.create(model=model, max_tokens=max_tokens, messages=[{"role": "user", "content": prompt}], thinking={"type": "adaptive"},
                                         output_config={"effort": "low", "format": {"type": "json_schema", "schema": _anthropic_schema(key, fields)}})
            SPEND.add(model, msg.usage)
            text = next((b.text for b in msg.content if getattr(b, "type", "") == "text"), None)
            got = json.loads(text) if text else None
            if not isinstance(got, dict):
                raise ValueError(f"{model} が答えの形で返さなかった（{getattr(msg, 'stop_reason', '')}）")
            return got.get(key, []) or []
        msg = client.messages.create(model=model, max_tokens=max_tokens, messages=[{"role": "user", "content": prompt}],
                                     tools=[{"name": tool[0], "description": tool[1], "input_schema": _anthropic_schema(key, fields), "strict": True}],
                                     tool_choice={"type": "tool", "name": tool[0]})
        SPEND.add(model, msg.usage)
        got = next((b.input for b in msg.content if getattr(b, "type", "") == "tool_use"), None)
        if not isinstance(got, dict):
            raise ValueError(f"{model} が答えの形で返さなかった（{getattr(msg, 'stop_reason', '')}）")
        return got.get(key, []) or []
    from google.genai import types

    r = client.models.generate_content(
        model=model, contents=prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=_gemini_schema(key, fields, required or list(fields)),
                                           temperature=0))
    return json.loads(r.text).get(key, []) or []


def ask_flags(client, model: str, prompt: str, texts: list[str], field: str, desc: str, tool: tuple[str, str]) -> list[bool | None]:
    """質問の文ごとに真か偽かを、1回の問い合わせでまとめて答えさせる唯一の場所（事実を伺う質問か＝facts・お呼びする方がいらっしゃるかの一文か＝target_asks・
    ご案内の方法を伺う質問か＝ways）。送る文は prompt＋「番号：質問の文」の並び、答えの欄は「no」（質問の番号）と field（desc の真偽）。texts と同じ並びの答え
    （無い・形の違うものは None）を返す。⛔ 2026-11-05 まで、決め方ごとに同じ聞き方と読み取りを写していた（facts・target_asks の by_model と _read_items）。"""
    body = "\n".join(f"{n}：{t}" for n, t in enumerate(texts, 1))
    items = ask(client, model, prompt + body, {"no": ("integer", "質問の番号"), field: ("boolean", desc)}, tool=tool)
    got = {x.get("no"): x.get(field) for x in items or [] if isinstance(x, dict)}
    return [got.get(n) if isinstance(got.get(n), bool) else None for n in range(1, len(texts) + 1)]


def agreed(answers: list, otherwise):
    """2つ以上のモデルの答え answers がそろえばその答え、そろわない・答えが無い・1つだけなら otherwise（倒す側）＝取込で決めることの合わせ方の唯一の場所
    （facts.decide・target_asks.decide・ways.decide）。"""
    if len(answers) >= 2 and all(a is not None for a in answers) and len(set(answers)) == 1:
        return answers[0]
    return otherwise
