"""選択肢の質問（はい／いいえ・選択肢）の、選択肢ごとの「意味の言葉」を決める（取込のとき。人は確かめない。G-48 で測る）。

使う所：答えを言い直して確かめるとき（flowctl.choice_words → phrases.confirm の meaning）。
- 受け入れにも断りにも取れる答え（「大丈夫です」「結構です」。DESIGN.md 6-10 の (3)）のあと：「念のため（意味の言葉）でよろしいでしょうか？」
- 聞き返したあとの言い直し（6-11）：「ありがとうございます。（意味の言葉）でよろしかったでしょうか？」
例：「ご予約はされていますでしょうか？」の「はい」⇒「ご予約はお済みということ」、「いいえ」⇒「ご予約はまだということ」。
⛔ 2026-10-17 に見つけた穴：選択肢の名前のまま言い直していたので「はいとおっしゃいましたでしょうか？」になっていた。
✅ 2026-10-16 ユーザー：「人は基本的に介在しないと何度も言っているよね！」⇒ 取込のプレビューで人が確かめる形にはしない。

決め方（topics.decide と同じ形）：文字のモデル2つに、1回の問い合わせでまとめて答えさせる（by_model）。プログラムの確かめ（check）に通り、
2つのモデルの言葉が同じときだけ使う（decide）。決まらない選択肢は空＝選択肢の名前で言い直す（返事の言葉だけの名前は言い直せないので、
言い直さずに聞き返す＝flowctl._ask_confirm。止まらない）。⚠️ 意味を取り違えた言葉（「はい」に「まだ」の意味）は、相手が認めると逆の答えが
渡る＝誤りを0にすることを一番にする（G-48 で1つずつ見る）。
"""
from __future__ import annotations

import json
import re
import unicodedata

PROMPT = (
    "電話でお客様に聞く、選択肢のある質問の一覧です。お客様の答えがあいまいだったとき（「大丈夫です」「結構です」など）や、"
    "聞き返したあとに、受け取った答えの意味を、次の言い方で確かめます。\n"
    "「（　）でよろしいでしょうか？」\n"
    "質問の選択肢ごとに、お客様がその選択肢を答えたという意味を、（　）に入れる言葉で words に書いてください。\n"
    "決まり：\n"
    "- 質問の言葉をできるだけそのまま使い、言い切る形にします。質問に無い意味を足しません。\n"
    "- お客様に向ける丁寧な言い方にします（「ご」「お」を付けるなど）。\n"
    "- 「、」（読点）を入れません。「？」で終えません。\n"
    "- 「はい」「いいえ」のような返事の言葉だけにしません。\n"
    "- 例：「お届けは午前と午後のどちらがご都合よろしいでしょうか？」の「午前」⇒「お届けは午前」／"
    "「資料はもうお手元に届いていますでしょうか？」の「はい」⇒「資料はもうお手元に届いているということ」、"
    "「いいえ」⇒「資料はまだお手元に届いていないということ」\n"
    "質問（番号：質問の文／選択肢）：\n"
)


def _schema():
    from google.genai import types

    item = types.Schema(type=types.Type.OBJECT, properties={
        "no": types.Schema(type=types.Type.INTEGER, description="質問の番号"),
        "choice": types.Schema(type=types.Type.STRING, description="選択肢（書いてあるとおり）"),
        "words": types.Schema(type=types.Type.STRING, description="（　）に入れる言葉"),
    }, required=["no", "choice", "words"])
    return types.Schema(type=types.Type.OBJECT, properties={"items": types.Schema(type=types.Type.ARRAY, items=item)},
                        required=["items"])


def by_model(client, model: str, questions: list[tuple[str, tuple]]) -> list[dict]:
    """questions＝[(質問の文, 選択肢の並び)]。1回の問い合わせでまとめて答えさせ、質問ごとに {選択肢: 言葉}（そろえたもの）を返す。"""
    from google.genai import types

    body = "\n".join(f"{n}：{q}／{'・'.join(c)}" for n, (q, c) in enumerate(questions, 1))
    r = client.models.generate_content(
        model=model, contents=PROMPT + body,
        config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=_schema(), temperature=0))
    out = [{} for _ in questions]
    for x in json.loads(r.text).get("items", []):
        n = x.get("no")
        if isinstance(n, int) and 1 <= n <= len(questions) and clean(x.get("choice")) in questions[n - 1][1]:
            out[n - 1][clean(x.get("choice"))] = clean(x.get("words"))
    return out


def clean(s: str | None) -> str:
    return unicodedata.normalize("NFKC", (s or "").strip().strip("「」『』")).rstrip("。")


_BARE = re.compile(r"(?:はい|いいえ|いえ|ええ|うん|いや)")


def check(words: str, question: str, choice: str) -> bool:
    """言い直しの「（言葉）でよろしいでしょうか？」に入れられる形か：空でない・長すぎない・区切りや問いかけを含まない・返事の言葉だけでない・
    質問か選択肢の中の言葉（名詞）を1つは使っている（質問に無いことを作り出していない目安）。"""
    from jtext import words as split

    w = words.strip()
    if not w or len(w) > 30 or re.search(r"[、。，．？?！!「」『』]", w) or _BARE.fullmatch(w):
        return False
    if re.search(r"(?:でしょうか|ですか|ますか|よろしい)$", w):
        return False
    base = unicodedata.normalize("NFKC", question + choice)
    nouns = [x.surface for x in split(w) if x.feature.pos1 == "名詞" and len(x.surface) >= 2]
    return any(n in base for n in nouns)


# 打ち消しの言葉（「されていない」「なし」「不要」「お送りしなくて」「ございません」）。意味の言葉の向き（認める側か打ち消す側か）を比べる
_NEGATION = re.compile(r"ない|なく|なし|無し|不要|ません|しない|ず$")


def negated(words: str) -> bool:
    return bool(_NEGATION.search(words))


def decide(question: str, choices: tuple, model_words: list[dict]) -> dict:
    """意味の言葉を決める（取込のとき。人は確かめない）。選択肢ごとに、2つ以上のモデルの言葉がどれも確かめに通り、打ち消しの有無が
    そろうときだけ、最初のモデルの言葉を使う。⚠️ 一番こわい誤りは向きの取り違え（「はい」に「されていない」）＝相手が認めると逆の答えが渡る。
    向きがモデルどうしで食い違えば決めない。⛔ G-48 の1回目：言葉が文字まで同じときだけにすると、51個中9個が「初めて」と「初めてということ」、
    「ご参加いただけない」と「ご参加されない」のような、意味の同じ言い違いで決まらなかった（どれも意味の取り違えは0）。"""
    out = {}
    for c in choices:
        got = [m.get(c, "") for m in model_words]
        if len(got) >= 2 and all(g and check(g, question, c) for g in got) and len({negated(g) for g in got}) == 1:
            out[c] = got[0]
    return out
