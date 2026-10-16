"""質問の「聞くこと」（「ご希望の日時」など）を、質問の文から決める（取込のとき。人は確かめない。G-43 で測る）。

使う所：聞き返しの言い方（phrases.ask_again）の「恐れ入ります。（聞くこと）をもう一度お聞かせいただいてもよろしいでしょうか？」と、
最後の1回の「お声が聞き取れませんでしたので（聞くこと）をもう一度だけお聞かせいただいてもよろしいでしょうか？」。
空なら「恐れ入ります。もう一度お聞かせいただいてもよろしいでしょうか？」の形になる（聞き返しはできる＝止まらない）。
✅ 2026-10-16 ユーザー：「人は基本的に介在しないと何度も言っているよね！」⇒ 取込のプレビューで人が確かめる形（CL-1.md 4-10 の前の案）はやめる。

決め方（units.decide と同じ形）：
- by_rule：質問の文が「（聞くこと）を ＋ 聞く言葉（お聞かせ・お伺い・教えて・お知らせ・お尋ね・頂戴・確認させて）」の形なら、
  を の前の言葉そのもの（質問の文の中の文字だけ。AIを使わない＝言葉を作り出さない）。
- by_model：それ以外の形（「何名様でお越しになりますでしょうか？」など）は、文字のモデルに答えさせる。
- decide：決まりで決まればそれ。決まらなければ、2つのモデルの答えが同じで、確かめ（check）に通るときだけ。どれでもなければ空。
"""
from __future__ import annotations

import json
import re
import unicodedata

# 聞く言葉（を のあとの最初の動詞）。⚠️「Xを聞いたことがありますか」「Xをご存じですか」「資料をお送りしても」「ご確認いただけますか」は
# 聞く質問ではない ⇒ 形まで決める：聞く＋せる（お聞かせ）／お＋聞く＋為る（お聞きする）／伺う／教える／知る＋せる（お知らせ）／
# 尋ねる／頂戴＋為る／確認＋為る＋せる（確認させていただく）
_BREAK_POS2 = {"接続助詞", "係助詞", "終助詞", "副助詞"}
_BREAK_CASE = {"が", "に", "へ", "で", "から", "より"}  # の・と（「お名前と部署」）は名前の中に入る


def _is_tell(ws: list, i: int) -> bool:
    """ws[i] から始まる言葉が「聞く言葉」か。"""
    lem = lambda k: ws[k].feature.lemma if 0 <= k < len(ws) else ""  # noqa: E731
    w = ws[i]
    if w.feature.pos1 == "動詞":
        if lem(i) in ("伺う", "教える", "尋ねる"):
            return True
        if lem(i) == "聞く":
            return lem(i + 1) == "せる" or (ws[i - 1].surface == "お" and lem(i + 1) == "為る")
        if lem(i) == "知る":
            return lem(i + 1) == "せる"
        return False
    if lem(i) == "頂戴":
        return lem(i + 1) == "為る"
    if lem(i) == "確認":
        return lem(i + 1) == "為る" and lem(i + 2) == "せる"
    return False


def by_rule(question: str) -> str:
    """「（聞くこと）を ＋ 聞く言葉」の形なら、を の前の名詞のまとまり（文の頭か、区切りのあとから）。それ以外・形が崩れるときは空。
    区切り＝「、」・副詞・接続の助詞（「差し支えなければ」の「ば」）・「は」「も」・格の助詞「が」「に」「で」など（「の」「と」は
    名前の中に入る＝「担当の方のお名前と部署」）・名詞にかからない動詞（「お使いになっている」の「て」のあと）。"""
    from jtext import words
    ws = list(words(unicodedata.normalize("NFKC", question)))
    for k, w in enumerate(ws):
        if not (w.surface == "を" and w.feature.pos1 == "助詞"):
            continue
        j = next((j for j in range(k + 1, len(ws)) if ws[j].feature.pos1 == "動詞" or ws[j].feature.lemma in ("頂戴", "確認")
                  or ws[j].surface == "を"), None)
        if j is None or ws[j].surface == "を" or not _is_tell(ws, j):
            continue
        start = next((b + 1 for b in range(k - 1, -1, -1) if _is_break(ws, b)), 0)
        topic = "".join(x.surface for x in ws[start:k])
        return topic if check(topic) else ""
    return ""


def _is_break(ws: list, b: int) -> bool:
    f = ws[b].feature
    if f.pos1 in ("補助記号", "副詞", "感動詞", "接続詞"):
        return True
    if f.pos1 == "助詞":
        return f.pos2 in _BREAK_POS2 or ws[b].surface in _BREAK_CASE
    if f.pos1 in ("動詞", "助動詞", "形容詞"):  # 名詞にかかる動詞（「つきやすい時間帯」）は名前の中、そうでなければ区切り
        return not (b + 1 < len(ws) and ws[b + 1].feature.pos1 in ("名詞", "接尾辞"))
    return False


def check(topic: str) -> bool:
    """聞き返しの言い方に入れられる形か（名詞で終わる・問いかけや聞く言葉を含まない・長すぎない）。"""
    from jtext import words
    t = topic.strip()
    if not t or len(t) > 20 or re.search(r"[？?。、！!「」]", t):
        return False
    ws = list(words(t))
    if not ws or ws[-1].feature.pos1 not in ("名詞", "接尾辞") or ws[0].feature.pos1 not in ("接頭辞", "名詞", "代名詞", "形状詞"):
        return False
    return not any(_is_tell(ws, i) for i in range(len(ws)))


PROMPT = (
    "電話でお客様に聞く質問の一覧です。お客様の答えが聞き取れなかったとき、次の言い方で聞き返します。\n"
    "「恐れ入ります。（　）をもう一度お聞かせいただいてもよろしいでしょうか？」\n"
    "質問ごとに、（　）に入れる「聞いていることの名前」を topic に書いてください。\n"
    "決まり：\n"
    "- 名詞の言い方にします（例：「何名様でお越しになりますでしょうか？」⇒「ご来店の人数」）。\n"
    "- できるだけ質問の文の言葉を使い、質問に無い意味を足しません。お客様に向ける丁寧な言い方（「ご」「お」）にします。\n"
    "- 自然な名前にならないとき（はい・いいえで答える質問など）は、topic を空にします。\n"
    "質問（番号：質問の文）：\n"
)


def _schema():
    from google.genai import types

    item = types.Schema(type=types.Type.OBJECT, properties={
        "no": types.Schema(type=types.Type.INTEGER, description="質問の番号"),
        "topic": types.Schema(type=types.Type.STRING, description="聞いていることの名前。自然な名前にならなければ空"),
    }, required=["no", "topic"])
    return types.Schema(type=types.Type.OBJECT, properties={"items": types.Schema(type=types.Type.ARRAY, items=item)},
                        required=["items"])


def by_model(client, model: str, questions: list[str]) -> list[str]:
    """1回の問い合わせでまとめて答えさせる（取込は業務の版ごとに1回）。questions と同じ並びの topic（そろえたもの）を返す。"""
    from google.genai import types

    body = "\n".join(f"{n}：{q}" for n, q in enumerate(questions, 1))
    r = client.models.generate_content(
        model=model, contents=PROMPT + body,
        config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=_schema(), temperature=0))
    got = {x.get("no"): x.get("topic", "") for x in json.loads(r.text).get("items", [])}
    return [clean(got.get(n, "")) for n in range(1, len(questions) + 1)]


def clean(topic: str | None) -> str:
    return unicodedata.normalize("NFKC", (topic or "").strip().strip("「」『』"))


def decide(question: str, model_topics: list[str]) -> str:
    """聞くことを1つに決める（取込のとき。人は確かめない）。(1) 決まり (2) モデル2つの答えが同じで、確かめに通るとき (3) 空。"""
    rule = by_rule(question)
    if rule:
        return rule
    if len(model_topics) < 2 or not model_topics[0] or any(t != model_topics[0] for t in model_topics[1:]):
        return ""
    return model_topics[0] if check(model_topics[0]) else ""
