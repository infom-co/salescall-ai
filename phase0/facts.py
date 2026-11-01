"""質問が「お客様に事実を伺う質問か」を、質問の文から決める（取込のとき。人は確かめない。G-71 で測る）。

✅ 2026-11-01 ユーザー：(a)「OK」＝質問ごとに「事実を伺う質問か」を1か所に持ち、事実を伺う質問では「どちらでも」を選べるものから外し
（「お任せします」「どちらでも構いません」は答えになっていない＝聞き直す）、控える・断る返事は「答えたくない」として扱う。
⛔ 2026-10-31 の夜（G-53nofits）：判定への説明では「『どちらでも』は事実を聞く質問には無い」としながら、事実を伺う質問（従業員数・使っている回線）
にも「どちらでも」を選べるものとして出し、正解の表も「お任せします」系の言葉を「どちらでも」にしていた（説明と選べるものの食い違い）。
説明の1文では効かなかった（d1-ultra は9つのうち8つを「どちらでも」と読んだ）。同じ区別が、「ご予約はされていますでしょうか？」への
「差し控えさせていただきます」を「予約していない」と読む外れにも出ていた（申し出への断りと、事実の質問に答えるのを控えることを分けていない）。

使う所：flowctl.Question.asks_fact（選べるものの1か所＝Question.all_conditions・判定の説明の1か所＝meaning_judge.system_desc）。

決め方（topics.decide・meanings.decide と同じ形）：文字のモデル2つに、1回の問い合わせでまとめて答えさせる（by_model）。2つの答えがそろえばそれ。
そろわない・答えが無いときは、事実を伺う質問とする。⚠️ どちらに倒しても誤りは残りうる：事実を伺う質問を希望の質問と取り違えると、
「お任せします」を「どちらでも」として受け取り依頼者に意味の無い値が渡る・「差し控え」を断る側の答えと読みうる。希望・申し出の質問を事実を伺う
質問と取り違えると、「どちらでも」は聞き直すだけで済むが、申し出を控える返事（「遠慮しておきます」）を「答えたくない」と読みうる ⇒ 誤った値の
重いほう（事実を伺う質問の取り違え）を避ける側に倒す（G-71 でそろわなかったのは「ご不明な点はございませんでしょうか？」の1つ＝事実を伺う質問）。
"""
from __future__ import annotations

import json

PROMPT = (
    "電話でお客様に聞く質問の一覧です。質問ごとに、お客様に事実を伺う質問かを fact に答えてください。\n"
    "決まり：\n"
    "- 事実を伺う質問（true）：お客様がもうしたこと・持っているもの・使っているもの・いまの状態・数など、お客様の希望や選びでは変わらないことを"
    "伺う質問。\n"
    "- 事実を伺う質問ではない（false）：お客様の希望・都合・選びを伺う質問と、こちらの申し出（お送りする・お電話する など）を受けるかを伺う質問。\n"
    "- 質問の文の意味だけで決めます。\n"
    "質問（番号：質問の文）：\n"
)


def _schema():
    from google.genai import types

    item = types.Schema(type=types.Type.OBJECT, properties={
        "no": types.Schema(type=types.Type.INTEGER, description="質問の番号"),
        "fact": types.Schema(type=types.Type.BOOLEAN, description="お客様に事実を伺う質問か"),
    }, required=["no", "fact"])
    return types.Schema(type=types.Type.OBJECT, properties={"items": types.Schema(type=types.Type.ARRAY, items=item)},
                        required=["items"])


def by_model(client, model: str, questions: list[str]) -> list[bool | None]:
    """1回の問い合わせでまとめて答えさせる（取込は業務の版ごとに1回）。questions と同じ並びの答え（無ければ None）を返す。"""
    from google.genai import types

    body = "\n".join(f"{n}：{q}" for n, q in enumerate(questions, 1))
    r = client.models.generate_content(
        model=model, contents=PROMPT + body,
        config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=_schema(), temperature=0))
    got = {x.get("no"): x.get("fact") for x in json.loads(r.text).get("items", [])}
    return [got.get(n) if isinstance(got.get(n), bool) else None for n in range(1, len(questions) + 1)]


def decide(model_facts: list[bool | None]) -> bool:
    """事実を伺う質問かを1つに決める（取込のとき。人は確かめない）。2つ以上のモデルの答えがそろえばそれ、そろわなければ事実を伺う質問。"""
    if len(model_facts) >= 2 and all(f is not None for f in model_facts) and len(set(model_facts)) == 1:
        return model_facts[0]
    return True
