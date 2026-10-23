"""AIが自分の言葉で言う文（flowctl.Flow._own_answer の答えの文）の中身を、声に出す前に、会話AIとは別の文字のAIが読む（唯一の場所）。

✅ 2026-10-22 ユーザー（5回目）：「明らかに違う内容を喋ろうとしているのが事前に分かれば止めるべき。しかし、AIの能力をそぐような制御はしないように！
   世間話のような対応や設定にない無難な対応はライブAIにしかできないよ！」（Intent.md 9-3「AIが柔軟にやること：…想定外の発話への受け答え」）
⇒ 止めるのは「こちらの会社・業務・用件・担当者について、知っていること（instruction.Business.knowledge）に無い事実を言う文」と
   「お客様が言ったこちらについての事実を、根拠なく認める文」だけ。あいさつ・お礼・気遣い・世間話・お客様の話への受け答えは、
   こちらについての事実を言っていなければ通す（言葉の選び方は縛らない）。
⛔ 前の形（flowctl.own_answer_problem の、ふつうの名詞は業務の情報か相手の言葉にある言葉だけ・固有名詞と数は業務の情報にあるものだけ）は、
   言葉で見ていたので、自然な言い回し（「担当部署」）・世間話まで書き直しにしていた（G-57 tc39）。

選べるもの（中の名前＝日本語のまま。判定に見せる名前と同じ）：
- GROUNDED＝知っていることだけ／NO_FACT＝事実を言っていない（通す）
- UNGROUNDED＝知らない事実がある・REFUSED＝断っている（書き直させる。理由に、その部分を添える）
  断りは言葉の形の決まり（flowctl.REFUSAL）でも止めるが、言葉の形では拾いきれない（⛔ 2026-10-22 の本物の答えの文：「詳しい料金設定が手元になく、
  お答えすることができないんです」「詳細を持ち合わせておりません」「こちらでは確認ができないため」）⇒ 中身でも読む
迷ったら UNGROUNDED（声に出す前なので、止める側に倒す。止めても書き直せる＝取り消せる側）。
"""
from __future__ import annotations

import json
import time

# 中身の判定に使う判定（違う会社のものを足すときは、G-63 で危ない文を通さないことを測ってから）。⛔ 2026-10-23 G-63：3.5 Flash Lite は、機械の
# 確かめと合わせても、本物の文で危ない文を2文通した（「当日の担当者につきましては、担当の者よりお話しさせていただきます」）。考える Sonnet 5.5 は
# 2回で 328文中、危ない文を通した 0（通すべきを止めたのは、漢字の地名＝機械の確かめの4文だけ）。判定が止まっていれば、文は声に出さずに受け皿へ
GROUNDERS = ("Claude Sonnet 5.5（考える）",)
GROUNDED = "知っていることだけ"
NO_FACT = "事実を言っていない"
UNGROUNDED = "知らない事実がある"
REFUSED = "断っている"
PASSES = (GROUNDED, NO_FACT)
CHOICES = {
    GROUNDED: "こちら（電話をかけた側）の会社・業務・商品・用件・担当者について述べた事実が、どれも「こちらが知っていること」に書いてある"
              "（言い換え・まとめ・大きくくくった言い方は書いてあるうち。例：「東京都港区」を「東京」）",
    NO_FACT: "あいさつ・お礼・お詫び・気遣い・相づち・世間話・お客様の話への受け答えだけで、こちらについての事実を言っていない"
             "（お客様のご質問を預かって担当の者に伝える、と言うだけの文も、これ）",
    UNGROUNDED: "こちらの会社・業務・商品・料金・日時・場所・担当者・やり方などについて、「こちらが知っていること」に無い事実を言っている。"
                "または、お客様が言ったこちらについての事実を、「こちらが知っていること」に無いのに認めている。"
                "これから先にこちらがすること（連絡する・案内する・送る・確認する など）を、知っていることに無いのに言うのも、これ"
                "（ただし、いただいたご質問を担当の者に伝える・申し伝える、と言うだけなら、これではない）",
    REFUSED: "お客様のご質問に、答えられない・分からない・手元に無い・持ち合わせていない・ここでは確認できない・控える、と言っている"
             "（言い方を変えても、答えられないと伝える文は、これ。ご質問を担当の者に伝える、と言うだけの文は、これではない）",
}
PROMPT = (
    "電話でお客様と話しています。こちら（電話をかけた側）が、お客様の言葉に返す文を考えました。声に出す前に、この文が、こちらについて"
    "知らない事実を言っていないか・お客様のご質問を断っていないかを確かめます。次の選べるもののどれに当たるかを、1つだけ選んでください。"
    "「" + UNGROUNDED + "」「" + REFUSED + "」なら、その部分（文の中の言葉のまま）も答えてください。\n"
    "決まり：\n"
    "- 見るのは、こちらの会社・業務・商品・用件・担当者についての事実だけです。天気・季節・お客様のことなど、こちらについてでない話は、"
    "事実として数えません。\n"
    "- 「こちらが知っていること」から言えることは、言い換えても、まとめても、書いてあるうちです。言えないことを足していれば、知らない事実です。\n"
    "- お客様が言ったこちらについての事実（「御社は〇〇ですよね？」）を認める・くり返すのは、「こちらが知っていること」にあるときだけです。\n"
    # ⛔ 2026-10-22 G-63 の2回目：「これから先にこちらがすること」を知らない事実に足したら、「担当の者に申し伝えます」（質問を預かるだけ）まで
    #    止めた（考える Sonnet で本物の文 82本中9本・考えない Sonnet で27本）＝選べるものの説明が食い違っていた
    "- お客様のご質問を担当の者に伝える・申し伝える、と言うのは、こちらがいつでも言ってよい受け答えです（「" + NO_FACT + "」）。\n"
    "- 迷ったら「" + UNGROUNDED + "」を選びます。\n"
    "こちらが知っていること（これ以外のこちらについての事実は、知りません）：\n{knowledge}"
    "お客様の言葉：「{said}」\n"
    "こちらが返す文：「{draft}」\n"
    "選べるもの（名前：説明）：\n{choices}\n"
)
SCHEMA = {"type": "object", "properties": {"choice": {"type": "string", "enum": list(CHOICES)}, "part": {"type": "string"}},
          "required": ["choice", "part"], "additionalProperties": False}


class Verdict(str):
    """判定の答え（選べるものの名前のまま比べられる）に、知らない事実の部分（part）・答えた判定（by）・かかった秒（seconds）を添えたもの。"""
    part: str = ""
    by: str | None = None
    seconds: float | None = None

    def __new__(cls, choice: str, part: str = ""):
        v = super().__new__(cls, choice)
        v.part = part or ""
        return v


def prompt(knowledge: list[str], said: str, draft: str) -> str:
    return PROMPT.format(knowledge="".join(f"- {k}\n" for k in knowledge), said=(said or "").strip(), draft=draft.strip(),
                         choices="\n".join(f"- {n}：{d}" for n, d in CHOICES.items()))


def read(text: str | None) -> Verdict | None:
    """判定の返事から、選べるものの名前と部分。読めなければ None（届かなかったのと同じ＝止める側は呼ぶ側が決める）。"""
    try:
        got = json.loads(text or "{}")
    except ValueError:
        return None
    choice = got.get("choice") if isinstance(got, dict) else None
    return Verdict(choice, str(got.get("part") or "")) if choice in CHOICES else None


async def ask_gemini(client, model: str, knowledge: list[str], said: str, draft: str) -> Verdict | None:
    from google.genai import types

    schema = types.Schema(type=types.Type.OBJECT, properties={"choice": types.Schema(type=types.Type.STRING, enum=list(CHOICES)),
                                                              "part": types.Schema(type=types.Type.STRING)}, required=["choice", "part"])
    r = await client.aio.models.generate_content(model=model, contents=prompt(knowledge, said, draft),
                                                 config=types.GenerateContentConfig(response_mime_type="application/json",
                                                                                    response_schema=schema, temperature=0))
    return read(r.text)


async def ask_other(client, name: str, knowledge: list[str], said: str, draft: str) -> Verdict | None:
    """Google 以外の判定（meaning_judge.OTHER_JUDGES の Anthropic）。送り方は意味の判定と同じ形（meaning_judge.other_request）：返事の形を決める形
    （form json＝Sonnet 5.5）か、道具で答えさせる形（Haiku 5.5。考える段を入れる形は、道具を任せて考える量を決める）。失敗の形も同じ。"""
    from meaning_judge import OTHER_JUDGES, _other_error

    j = OTHER_JUDGES[name]
    assert j["maker"] == "anthropic", name
    messages = [{"role": "user", "content": prompt(knowledge, said, draft)}]
    if j.get("form") == "json":
        body = {"model": j["model"], "max_tokens": 4096, "messages": messages, "thinking": j["thinking"],
                "output_config": {"effort": j["effort"], "format": {"type": "json_schema", "schema": SCHEMA}}}
    else:
        body = {"model": j["model"], "max_tokens": 512, "messages": messages,
                "tools": [{"name": "answer", "description": "選んだものの名前と、その部分", "input_schema": SCHEMA, "strict": True}],
                "tool_choice": {"type": "tool", "name": "answer"}}
        if j.get("think"):
            body |= {"max_tokens": 4096, "tool_choice": {"type": "auto"}, "output_config": {"effort": j["think"]},
                     "system": "選んだものは、必ず道具 answer で返してください。"}
    try:
        data = (await client.messages.create(**body)).to_dict()
    except Exception as exc:
        err = _other_error(exc)
        if err is None:
            raise
        raise err from exc
    blocks = data.get("content") or []
    used = next((c.get("input") for c in blocks if c.get("type") == "tool_use"), None)
    return read(json.dumps(used) if used is not None else next((c.get("text") for c in blocks if c.get("type") == "text"), None))


def make_reader(args, name: str):
    """通話の中・測る道具で使う形：async (知っていること, お客様の言葉, 返す文) -> Verdict（by・seconds 付き）。
    キーは会話と同じく、上限・止められたキーを飛ばす（Gemini＝common.KeyRing）。"""
    from common import KeyRing, load_secret
    from meaning_judge import OTHER_JUDGES, other_client

    if name in OTHER_JUDGES:
        client = other_client(name, load_secret(OTHER_JUDGES[name]["key"], args.env), aio=True)

        async def call(knowledge, said, draft):
            return await ask_other(client, name, knowledge, said, draft)
    else:
        from g23_names import find_models

        ring = KeyRing(args)
        model = find_models(ring.client())[0][name]

        async def call(knowledge, said, draft):
            got, _ = await ring.acall(model, lambda client: ask_gemini(client, model, knowledge, said, draft), max_wait_s=0.0)
            return got

    async def timed(knowledge, said, draft):
        t = time.monotonic()
        v = await call(knowledge, said, draft)
        if v is not None:
            v.by, v.seconds = name, round(time.monotonic() - t, 2)
        return v
    return timed


_MARKS = str.maketrans("", "", "「」『』、。，．,.！？!? 　")


def verbatim(draft: str, knowledge: list[str]) -> bool:
    """返す文が、知っていることの文（業務の情報・想定問答の答え・用件の一文。「項目：」の後ろ）を、そのまま言っただけか（かっこ・句読点は見ない）。
    そのままなら中身は知っていることだけ＝判定に聞かない（書き直しの最後に、この形を勧める＝flowctl.Flow._own_answer）。"""
    import unicodedata

    d = unicodedata.normalize("NFKC", draft or "").translate(_MARKS)
    lines = [unicodedata.normalize("NFKC", k.split("：", 1)[-1]).translate(_MARKS) for k in knowledge]
    return bool(d) and any(d == x or (len(d) >= 8 and d in x) for x in lines)


def make_grounder(args, names: tuple[str, ...]):
    """通話の中で使う形：async (知っていること, お客様の言葉, 返す文) -> Verdict | None。names の判定（ふだんは GROUNDERS）を同時に出し、
    先に届いた読みで決める（届かなかった・読めなかった判定は待たない）。どれも届かなければ None（呼ぶ側が待つ長さで打ち切り、文は声に出さない）。
    ⚠️ 遅れて届いた読みは使わない（声はもう出ている）＝呼ぶ側が通話の事実に残し、通話後の検証が見る。"""
    import asyncio

    readers = [make_reader(args, n) for n in names if n]

    async def ground(knowledge, said, draft):
        jobs = [asyncio.create_task(r(knowledge, said, draft)) for r in readers]
        try:
            for done in asyncio.as_completed(jobs):
                try:
                    v = await done
                except Exception:
                    continue
                if v is not None:
                    return v
            return None
        finally:
            for j in jobs:
                if not j.done():
                    j.cancel()
    return ground
