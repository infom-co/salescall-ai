"""相手の言葉の2つ目の判定（DESIGN.md 6-10 の (6)。G-53）：相手の言葉が何に当たるかを、会話のAIとは別の文字のAIが決める。
流れの制御（Flow.on_meaning）は、会話のAIの報告と比べて、食い違えばそのまま受け取らずに確かめる。耳の聞き違いを別の耳で確かめる
（6-10 の (5)）のと同じ形で、意味の取り違えを、別の判定で確かめる。1つの判定で、次の3つの場面を受け持つ（場面で選べるものが変わるだけ）：
- 質問への答え：質問の選択肢（選択肢の質問）／「答えた」（値を聞く質問）・どちらでも・わからない・答えたくない
- 言い直しへの返事（asked＝こちらが言い直した言葉）：選択肢の質問は、相手がどの意味を認めたか（「はい」でも、言い直した意味の選択肢）。
  値を聞く質問は「合っている」か「違う値を言われた」か
- 答えでない言葉（どの場面でも）：取り次ぎを待つように言われた・代わった人が話し始めた・決められない

⛔ 2026-10-17 ユーザー：「精度は100％にする仕組みを作らないとだめですよ。間違えるのが当たり前でスルーしてるだろ！」
   ⇒ 記録の通話で、会話のAIが意味を取り違えたまま、流れの制御が確かめずに受け取った答えが12回あった（「まだ決めてないです」⇒
   「どちらでも」5回・「どちらでも大丈夫です」⇒「カード」など5回・「いや、これからなんです」「家まだです」⇒「わからない」2回）。
   どれも聞き取りは正しく、別の耳では拾えない（私は「別の耳では直せません」と書いて、そのまま流していた）。
- 判定に渡すのは、質問の文・こちらが言い直した言葉（言い直しへの返事のとき）・選べるもの・相手の言葉だけ。
  会話のAIの選んだ答えは渡さない（引きずられないように＝独立した判定）。
"""
from __future__ import annotations

import json

from flowctl import ANY, CHOICE_TYPES, CONFIRM_LABEL, HANDOVER, HOLD, UNDECIDED, label

SYSTEM_DESC = {ANY: "どれでもよいと、こちらに任された（「どちらでも」「どれでも」）",
               "unknown": "わからない・覚えていない・まだ決めていない（選ぶ前の状態）",
               "refused": "答えたくないと、答えを拒んだ"}
KIND_DESC = {HOLD: "ほかの人に電話を代わる・取り次ぐので、待つように言った（答えではない）",
             HANDOVER: "電話を代わった別の人が、話し始めた（「お電話代わりました」など。答えではない）",
             UNDECIDED: "受け入れにも断りにも取れて、この言葉だけでは決められない（「大丈夫です」「結構です」だけなど）・言い切っていない"
                        "（「たぶん」「かもしれない」）・答えになっていない"}
CONFIRM_DESC = {"affirmed": "こちらが言い直した値で合っていると認めた", "corrected": "違う・直した（別の値を言った）"}

PROMPT = (
    "電話でお客様に質問をして、お客様が話しました。お客様の言葉が、次の選べるもののどれに当たるかを、1つだけ選んでください。\n"
    "決まり：\n"
    "- 言葉の意味で選びます。言葉の頭の「はい」「いいえ」だけで決めません（「いいえ、もう済ませてあります」は、済ませた側）。\n"
    "- 打ち消しを重ねた言い方は、意味で読みます（「していないわけではない」は、している側）。\n"
    "- 「〜ございませんでしょうか？」のような打ち消しの問いへの「はい」「いいえ」は、選べるものの説明で、どちらの意味かを決めます。\n"
    "- こちらが答えを言い直して確かめたあとの返事は、お客様が認めた意味で選びます（言い直した意味を「はい」と認めたら、その意味）。\n"
    # ⚠️ 例は、試験の言葉（G-14・G-52）と同じ言い方にしない（同じだと、試験で測った正しさが高く出る）
    "- 迷ったら「" + UNDECIDED + "」を選びます。\n"
    "質問：「{question}」\n"
    "{asked}"
    "選べるもの（名前：説明）：\n{choices}\n"
    "お客様の言葉：「{text}」\n"
)


def options(q, asked: str | None = None) -> list[tuple[str, str]]:
    """選べるもの（名前、説明）。選択肢の質問と、値を聞く質問の最初の答えは Question.all_conditions の並び。値を聞く質問の言い直しへの
    返事は「合っている」「違う値を言われた」（AIの報告 confirm_field と同じ名前）。最後に、答えでない言葉の3つ。"""
    if q.type in CHOICE_TYPES or asked is None:
        head = [(label(c), SYSTEM_DESC.get(c) or q.meanings.get(c) or f"「{c}」と答えた") for c in q.all_conditions]
    else:
        head = [(CONFIRM_LABEL[k], d) for k, d in CONFIRM_DESC.items()]
    return head + [(k, d) for k, d in KIND_DESC.items()]


def prompt(q, text: str, asked: str | None = None) -> str:
    said = f"こちらが言い直して確かめた言葉：「{asked}」\n" if asked else ""
    return PROMPT.format(question=q.text, asked=said, text=text, choices="\n".join(f"- {n}：{d}" for n, d in options(q, asked)))


def _config(q, asked: str | None):
    from google.genai import types

    names = [n for n, _ in options(q, asked)]
    schema = types.Schema(type=types.Type.OBJECT, properties={"choice": types.Schema(type=types.Type.STRING, enum=names)}, required=["choice"])
    # ⚠️ 考える量（thinking_budget）は指定しない：3.5 Flash Lite は指定すると断る（400）。3.8 Flash は0にしても約3.3秒（2026-10-17 に測った）
    return types.GenerateContentConfig(response_mime_type="application/json", response_schema=schema, temperature=0)


def read(q, text: str | None, asked: str | None = None) -> str | None:
    """判定の返事から、中の名前（選択肢・any・unknown・refused／affirmed・corrected／HOLD・HANDOVER・UNDECIDED）。読めなければ None。"""
    try:
        name = json.loads(text or "{}").get("choice")
    except ValueError:
        return None
    if name in KIND_DESC:
        return name
    if q.type not in CHOICE_TYPES and asked is not None:
        return next((k for k, v in CONFIRM_LABEL.items() if v == name and k in CONFIRM_DESC), None)
    return q.condition_of(name)


def judge(client, model: str, q, text: str, asked: str | None = None) -> str | None:
    """相手の言葉 text が、質問 q（asked＝こちらが言い直した言葉）のどれに当たるか。読めなければ None。"""
    r = client.models.generate_content(model=model, contents=prompt(q, text, asked), config=_config(q, asked))
    return read(q, r.text, asked)


async def ajudge(client, model: str, q, text: str, asked: str | None = None) -> str | None:
    """judge と同じ（通話の中で、会話のAIの報告を待つ間に走らせる形）。"""
    r = await client.aio.models.generate_content(model=model, contents=prompt(q, text, asked), config=_config(q, asked))
    return read(q, r.text, asked)


JUDGE_MODELS = ("3.5 Flash Lite", "3.6 Flash", "3.7 Flash", "3.8 Flash")   # 判定に使える文字のモデル（表示名。g23_names.find_models でIDを引く）


def make_judge(args, model_name: str):
    """通話の中で使う形：async (質問, 相手の言葉, こちらが言い直した言葉) -> 中の名前。キーは会話と同じく、上限・止められたキーを飛ばす
    （common.KeyRing.acall）。どの試験も、これを run_call(judge=…) に渡す（作り方を試験ごとに書かない）。"""
    from common import KeyRing
    from g23_names import find_models

    ring = KeyRing(args)
    model = find_models(ring.client())[0][model_name]

    async def run(q, text, asked=None):
        got, _ = await ring.acall(model, lambda client: ajudge(client, model, q, text, asked))
        return got
    return run
