"""答えの意味の2つ目の判定（DESIGN.md 6-10 の (6)。G-53）：相手の答えの言葉が、質問のどの選択肢に当たるかを、会話のAIとは別の文字のAIが
決める。会話のAIの選んだ答えと食い違えば、そのまま受け取らずに確かめる（流れの制御 Flow.on_meaning）。耳の聞き違いを別の耳で確かめる
（6-10 の (5)）のと同じ形で、意味の取り違えを、別の判定で確かめる。

⛔ 2026-10-17 ユーザー：「精度は100％にする仕組みを作らないとだめですよ。間違えるのが当たり前でスルーしてるだろ！」
   ⇒ 記録の通話（1,688本）で、会話のAIが意味を取り違えたまま、流れの制御が確かめずに受け取った答えが12回あった（「まだ決めてないです」⇒
   「どちらでも」5回・「どちらでも大丈夫です」⇒「カード」など5回・「いや、これからなんです」「家まだです」⇒「わからない」2回）。
   どれも聞き取りは正しく、別の耳では拾えない（私は「別の耳では直せません」と書いて、そのまま流していた）。
- 判定に渡すのは、質問の文・選べるもの（選択肢と、その意味の言葉＝取込で決めたもの・どちらでも・わからない・答えたくない）・相手の言葉だけ。
  会話のAIの選んだ答えは渡さない（引きずられないように＝独立した判定）。
- 「決められない」（受け入れにも断りにも取れる・言い切っていない・答えになっていない）も選べる。その答えは確かめる。
"""
from __future__ import annotations

import json

from flowctl import ANY, UNDECIDED, label
SYSTEM_DESC = {ANY: "どれでもよいと、こちらに任された（「どちらでも」「どれでも」）",
               "unknown": "わからない・覚えていない・まだ決めていない（選ぶ前の状態）",
               "refused": "答えたくないと、答えを拒んだ"}
UNDECIDED_DESC = "受け入れにも断りにも取れて、この言葉だけでは決められない（「大丈夫です」「結構です」だけなど）・言い切っていない（「たぶん」「かもしれない」）・質問への答えになっていない"

PROMPT = (
    "電話でお客様に質問をして、お客様が答えました。お客様の答えの言葉が、次の選べるもののどれに当たるかを、1つだけ選んでください。\n"
    "決まり：\n"
    "- 答えの言葉の意味で選びます。言葉の頭の「はい」「いいえ」だけで決めません（「いいえ、もう済ませてあります」は、済ませた側）。\n"
    "- 打ち消しを重ねた言い方は、意味で読みます（「していないわけではない」は、している側）。\n"
    "- 「〜ございませんでしょうか？」のような打ち消しの問いへの「はい」「いいえ」は、選べるものの説明で、どちらの意味かを決めます。\n"
    # ⚠️ 例は、試験の言葉（G-14・G-52）と同じ言い方にしない（同じだと、試験で測った正しさが高く出る）
    "- 迷ったら「" + UNDECIDED + "」を選びます。\n"
    "質問：「{question}」\n"
    "選べるもの（名前：説明）：\n{choices}\n"
    "お客様の答え：「{text}」\n"
)


def options(q) -> list[tuple[str, str]]:
    """選べるもの（AIに見せる名前、説明）。並びは Question.all_conditions と同じ＋最後に「決められない」。"""
    out = []
    for c in q.all_conditions:
        out.append((label(c), SYSTEM_DESC.get(c) or q.meanings.get(c) or f"「{c}」と答えた"))
    return out + [(UNDECIDED, UNDECIDED_DESC)]


def prompt(q, text: str) -> str:
    return PROMPT.format(question=q.text, text=text, choices="\n".join(f"- {n}：{d}" for n, d in options(q)))


def _config(q):
    from google.genai import types

    names = [n for n, _ in options(q)]
    schema = types.Schema(type=types.Type.OBJECT, properties={"choice": types.Schema(type=types.Type.STRING, enum=names)}, required=["choice"])
    # ⚠️ 考える量（thinking_budget）は指定しない：3.5 Flash Lite は指定すると断る（400）。3.8 Flash は0にしても約3.3秒（2026-10-17 に測った）
    return types.GenerateContentConfig(response_mime_type="application/json", response_schema=schema, temperature=0)


def _read(q, text: str | None) -> str | None:
    """判定の返事から、中の名前（選択肢・any・unknown・refused）。「決められない」は UNDECIDED のまま。読めなければ None。"""
    try:
        name = json.loads(text or "{}").get("choice")
    except ValueError:
        return None
    return UNDECIDED if name == UNDECIDED else q.condition_of(name)


def judge(client, model: str, q, text: str) -> str | None:
    """相手の言葉 text が、質問 q のどれに当たるか（中の名前か UNDECIDED。読めなければ None）。"""
    r = client.models.generate_content(model=model, contents=prompt(q, text), config=_config(q))
    return _read(q, r.text)


async def ajudge(client, model: str, q, text: str) -> str | None:
    """judge と同じ（通話の中で、会話のAIの報告を待つ間に走らせる形）。"""
    r = await client.aio.models.generate_content(model=model, contents=prompt(q, text), config=_config(q))
    return _read(q, r.text)


JUDGE_MODELS = ("3.5 Flash Lite", "3.8 Flash")   # 判定に使う文字のモデル（表示名。g23_names.find_models でIDを引く）


def make_judge(args, model_name: str):
    """通話の中で使う形：async (質問, 相手の言葉) -> 中の名前か UNDECIDED。キーは会話と同じく、上限・止められたキーを飛ばす
    （common.KeyRing.acall）。どの試験も、これを run_call(judge=…) に渡す（作り方を試験ごとに書かない）。"""
    from common import KeyRing
    from g23_names import find_models

    ring = KeyRing(args)
    model = find_models(ring.client())[0][model_name]

    async def run(q, text):
        got, _ = await ring.acall(model, lambda client: ajudge(client, model, q, text))
        return got
    return run
