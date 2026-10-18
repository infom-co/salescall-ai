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
- 選択肢は、**意味の言葉を名前にして**見せる（取込で決めた Question.meanings。無ければ選択肢の名前）。
  ⛔ 2026-10-18 G-53 の2回目：選択肢の名前（「はい」「いいえ」）で見せていたら、「念のためご予約の日時に変更がおありということでよろしいでしょうか？」
     への「違います」を「いいえ」（＝変更がおあり）と読んだ（2回）＝言い直しを認めていないのに、認めたと読む向き（誤った答えが渡る向き）。
     「違います」と、名前の「いいえ」を、言葉で結びつけた。
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
    "- 「〜ございませんでしょうか？」のような打ち消しの問いへの「はい」「いいえ」は、選べるものの名前（意味）で、どちらの意味かを決めます。\n"
    "- こちらが答えを言い直して確かめたあとの返事は、お客様が認めた意味で選びます（言い直した意味を「はい」と認めたら、その意味）。\n"
    # ⚠️ 例は、試験の言葉（G-14・G-52）と同じ言い方にしない（同じだと、試験で測った正しさが高く出る）
    "{hedge}"
    "- 迷ったら「" + UNDECIDED + "」を選びます。\n"
    "質問：「{question}」\n"
    "{asked}"
    "選べるもの（名前：説明）：\n{choices}\n"
    "お客様の言葉：「{text}」\n"
)


# ⏳ 試している決まり（G-53 の3回目で測ってから決める）：言い切っていない返事は「決められない」。⛔ G-53 の2回目：言い直しへの返事 1,356個のうち
#    15個で、認めていない返事を「認めた」と読み、うち13個に言い切っていない目印があった（「メールのほうが助かりますが…」「していないはずですが…」）。
#    ⚠️ 「〜なんですが」は、はっきりしたお願いの前置きにも使う（「メールでお願いしたいんですが」）＝正しい答えを止めないかも測る
HEDGE_RULE = ("- 言い切っていない返事（「〜ですが…」で言いさす・「〜かな」「〜かね」・「〜はず」・「〜くらい」・「できれば〜」）は、"
              "認めたとも直したとも決めず「" + UNDECIDED + "」を選びます。\n")
USE_HEDGE_RULE = False  # 測るとき（g53_meaning --hedge）だけ True にする


def options(q, asked: str | None = None) -> list[tuple[str, str, str]]:
    """選べるもの（判定に見せる名前、説明、中の名前）。選択肢の質問と、値を聞く質問の最初の答えは Question.all_conditions の並び
    （宣言した選択肢は、意味の言葉を名前にする。無い・ほかの名前とかぶるときは選択肢の名前）。値を聞く質問の言い直しへの返事は
    「合っている」「違う値を言われた」（AIの報告 confirm_field と同じ名前）。最後に、答えでない言葉の3つ。"""
    if q.type in CHOICE_TYPES or asked is None:
        taken = {label(c) for c in q.all_conditions if c not in q.conditions} | set(KIND_DESC)
        head = []
        for c in q.all_conditions:
            if c in q.conditions:
                name = q.meanings.get(c)
                if not name or name in taken:
                    name = label(c)
                taken.add(name)
                head.append((name, "この意味の答え" if name != label(c) else f"「{c}」と答えた", c))
            else:
                head.append((label(c), SYSTEM_DESC[c], c))
    else:
        head = [(CONFIRM_LABEL[k], d, k) for k, d in CONFIRM_DESC.items()]
    return head + [(k, d, k) for k, d in KIND_DESC.items()]


def prompt(q, text: str, asked: str | None = None) -> str:
    said = f"こちらが言い直して確かめた言葉：「{asked}」\n" if asked else ""
    return PROMPT.format(question=q.text, asked=said, text=text, hedge=HEDGE_RULE if USE_HEDGE_RULE else "",
                         choices="\n".join(f"- {n}：{d}" for n, d, _ in options(q, asked)))


def _config(q, asked: str | None):
    from google.genai import types

    names = [n for n, _, _ in options(q, asked)]
    schema = types.Schema(type=types.Type.OBJECT, properties={"choice": types.Schema(type=types.Type.STRING, enum=names)}, required=["choice"])
    # ⚠️ 考える量（thinking_budget）は指定しない：3.5 Flash Lite は指定すると断る（400）。3.8 Flash は0にしても約3.3秒（2026-10-17 に測った）
    return types.GenerateContentConfig(response_mime_type="application/json", response_schema=schema, temperature=0)


def read(q, text: str | None, asked: str | None = None) -> str | None:
    """判定の返事から、中の名前（選択肢・any・unknown・refused／affirmed・corrected／HOLD・HANDOVER・UNDECIDED）。読めなければ None。"""
    try:
        name = json.loads(text or "{}").get("choice")
    except (ValueError, AttributeError):
        return None
    return next((c for n, _, c in options(q, asked) if n == name), None)


def _schema(q, asked: str | None) -> dict:
    """選べるものの名前だけを返させる形（Gemini は _config・ほかの会社はこの形をそのまま渡す）。"""
    return {"type": "object", "properties": {"choice": {"type": "string", "enum": [n for n, _, _ in options(q, asked)]}},
            "required": ["choice"], "additionalProperties": False}


def judge(client, model: str, q, text: str, asked: str | None = None) -> str | None:
    """相手の言葉 text が、質問 q（asked＝こちらが言い直した言葉）のどれに当たるか。読めなければ None。"""
    r = client.models.generate_content(model=model, contents=prompt(q, text, asked), config=_config(q, asked))
    return read(q, r.text, asked)


async def ajudge(client, model: str, q, text: str, asked: str | None = None) -> str | None:
    """judge と同じ（通話の中で、会話のAIの報告を待つ間に走らせる形）。"""
    r = await client.aio.models.generate_content(model=model, contents=prompt(q, text, asked), config=_config(q, asked))
    return read(q, r.text, asked)


JUDGE_MODELS = ("3.5 Flash Lite", "3.6 Flash", "3.7 Flash", "3.8 Flash")   # 判定に使える文字のモデル（表示名。g23_names.find_models でIDを引く）

# ── Google 以外の判定（DESIGN.md 1-3 の (2)・選んだ根拠は docs/JUDGE_SETUP.md）。同じ問い合わせの文・同じ選べるもの・同じ読み方（read）で、
#    送り方だけが会社ごとに違う。
#    ✅ 2026-10-18 ユーザー：「ジェミニより精度が悪いんだったら意味がないでしょ。ジェミニよりほぼ確実に精度がいいものを抜粋しないと時間の無駄」
#       「最新モデルで評価しないとダメだよ」⇒ 各社の最新の、速くて安いモデルのうち、公開の数字で 3.5 Flash Lite を上回るものだけ
#    ⚠️ キーの名前は ANTHROPIC_API_KEY にしない（この作業の場所そのものが読む名前と同じになる）
OTHER_JUDGES = {
    "Claude Haiku 5.5": {"maker": "anthropic", "model": "claude-haiku-5-5", "key": "ANTHROPIC_JUDGE_KEY", "host": "api.anthropic.com"},
    "GPT-6 Luna": {"maker": "openai", "model": "gpt-6-luna", "key": "OPENAI_JUDGE_KEY", "host": "api.openai.com"},
}
OPENAI_EFFORT = "low"  # 考える量（速さの公開の数字は low で約1.5秒。高くすると十数秒〜）


def other_request(name: str, q, text: str, asked: str | None, key: str) -> tuple[str, dict, dict]:
    """Google 以外の判定への問い合わせ（宛先・見出し・中身）。"""
    j, p = OTHER_JUDGES[name], prompt(q, text, asked)
    if j["maker"] == "anthropic":
        return (f"https://{j['host']}/v1/messages", {"x-api-key": key, "anthropic-version": "2023-06-01"},
                {"model": j["model"], "max_tokens": 64, "temperature": 0, "messages": [{"role": "user", "content": p}],
                 "tools": [{"name": "answer", "description": "お客様の言葉に当たるものの名前", "input_schema": _schema(q, asked)}],
                 "tool_choice": {"type": "tool", "name": "answer"}})
    return (f"https://{j['host']}/v1/responses", {"authorization": f"Bearer {key}"},
            {"model": j["model"], "input": p, "reasoning": {"effort": OPENAI_EFFORT}, "max_output_tokens": 2000,
             "text": {"format": {"type": "json_schema", "name": "answer", "schema": _schema(q, asked), "strict": True}}})


def other_text(name: str, data: dict) -> str | None:
    """Google 以外の判定の返事から、read に渡す文字（{"choice": 名前}）。"""
    if OTHER_JUDGES[name]["maker"] == "anthropic":
        got = next((c.get("input") for c in data.get("content") or [] if c.get("type") == "tool_use"), None)
        return json.dumps(got, ensure_ascii=False) if got else None
    return next((c.get("text") for o in data.get("output") or [] for c in o.get("content") or [] if c.get("type") == "output_text"), None)


def judge_other(http, name: str, q, text: str, asked: str | None = None, *, key: str) -> str | None:
    """judge と同じ（Google 以外の判定。http は httpx.Client）。上限・混雑は呼ぶ側がやり直す（httpx.HTTPStatusError）。"""
    url, head, body = other_request(name, q, text, asked, key)
    r = http.post(url, headers=head, json=body)
    r.raise_for_status()
    return read(q, other_text(name, r.json()), asked)


async def ajudge_other(http, name: str, q, text: str, asked: str | None = None, *, key: str) -> str | None:
    """judge_other と同じ（通話の中の形。http は httpx.AsyncClient）。"""
    url, head, body = other_request(name, q, text, asked, key)
    r = await http.post(url, headers=head, json=body)
    r.raise_for_status()
    return read(q, other_text(name, r.json()), asked)


def make_judge(args, model_name: str):
    """通話の中で使う形：async (質問, 相手の言葉, こちらが言い直した言葉) -> 中の名前。キーは会話と同じく、上限・止められたキーを飛ばす
    （common.KeyRing.acall）。どの試験も、これを run_call(judge=…) に渡す（作り方を試験ごとに書かない）。"""
    from common import KeyRing, load_secret
    from g23_names import find_models

    if model_name in OTHER_JUDGES:
        import httpx

        key, http = load_secret(OTHER_JUDGES[model_name]["key"], args.env), httpx.AsyncClient(timeout=30)

        async def other(q, text, asked=None):
            return await ajudge_other(http, model_name, q, text, asked, key=key)
        return other
    ring = KeyRing(args)
    model = find_models(ring.client())[0][model_name]

    async def run(q, text, asked=None):
        got, _ = await ring.acall(model, lambda client: ajudge(client, model, q, text, asked))
        return got
    return run
