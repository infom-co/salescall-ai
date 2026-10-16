"""数の質問の「単位」を決めて、数字だけの答えに補う（DESIGN.md 6-2 の値の形の確かめの続き。G-41 で測る）。

✅ 2026-10-16 ユーザー：「入れる方向でOKだが、人は基本的に介在しないと何度も言っているよね！AIかプログラムが判定して、精度よく単位を
   入れられるのかを確かめる必要があるね。会話で何を確認したいのかを確認して、正しい単位を補える精度高い仕組みが必要です」
⛔ G-37：相手の「3人です」を会話AIが「3 です」と聞き、値を「3」と報告した ⇒ 言い直しが「3で大丈夫でしょうか？」になった。
   相手が数字だけで答える（「3です」）ことも、ふつうにある。

決め方（取込のとき、業務設定の版ごとに1回。人は確かめない）：
- by_rule：質問の文の「何名」「何台」「人数」「台数」のような、数える言葉から決める（AIを使わない）。数える言葉の一覧（COUNTERS）は
  日本語の数え方の言葉で、業務ごとに足すものではない。
- by_model：文字のモデルに、質問が何を数えているか（counted）と、数字のすぐあとに付ける言葉（unit）を答えさせる。1つに決まらない
  （広さ＝平米・畳・坪、期間＝日・か月・年 など）ときは空にさせる。
- decide：2つを合わせる（どう合わせるかは G-41 の数で決める）。決まらなければ空＝単位を補わない（いまと同じ形のまま）。
使い方（会話のとき）：supply_unit が、数字だけの値（「3」「2か3」「3くらい」）に単位を補う。単位がもう付いている値（「3人」「3名様」
「大人2人と子ども1人」「ふたり」）は変えない。補った値が、言い直し（「3人で大丈夫でしょうか？」）と依頼主へ渡す値（「3人」）の両方になる。
"""
from __future__ import annotations

import json
import re

# 数える言葉（助数詞）。長いものから当てる（「何年目」を「年」にしない）。業務ごとに足す一覧ではない（日本語の数え方の言葉）
COUNTERS = sorted({
    "人", "名", "名様", "人分", "名分", "名様分", "人前", "台", "台分", "個", "つ", "本", "枚", "冊", "匹", "頭", "羽", "件", "回", "回目", "度",
    "泊", "日", "日間", "週", "週間", "か月", "ヶ月", "カ月", "ヵ月", "ケ月", "年", "年間", "年目", "年生", "年式", "歳", "才",
    "時間", "分", "秒", "部屋", "室", "階", "階建て", "棟", "軒", "戸", "社", "店", "店舗", "部", "通", "着", "足", "杯", "箱",
    "ケース", "セット", "袋", "缶", "株", "口", "坪", "番", "番目", "席", "世帯", "区画", "食", "点", "種類", "品", "皿", "錠", "包",
    "束", "機", "両", "校", "園", "か所", "ヶ所", "カ所", "箇所", "回線", "便", "kg", "キロ", "メートル", "km",
}, key=len, reverse=True)
_COUNTER = "|".join(re.escape(c) for c in COUNTERS)
_ASK = re.compile(rf"何\s*({_COUNTER})")              # 「何名様」「何台」「何年目」「何か月」
_COUNT_OF = re.compile(rf"({_COUNTER})数")            # 「人数」「台数」「店舗数」「部屋数」（「参加者数」「従業員数」は当たらない）
_HONORIFIC = re.compile(r"様")                         # 「名様」⇒「名」・「名様分」⇒「名分」（依頼主へ渡す値に「様」は付けない）
# 同じ意味の書き方（「何名様」と「人数」が1つの質問に両方あっても、2つの単位とは数えない）
SAME = [{"人", "名"}, {"人分", "名分"}, {"か月", "ヶ月", "カ月", "ヵ月", "ケ月"}, {"か所", "ヶ所", "カ所", "箇所"}, {"歳", "才"},
        {"部屋", "室"}, {"店", "店舗"}]


def same_unit(a: str, b: str) -> bool:
    return a == b or any(a in g and b in g for g in SAME)


def clean_unit(unit: str | None) -> str:
    """単位をそろえる。おかしな形（数字・句読点を含む、長すぎる）は空にする。"""
    u = re.sub(r"\s", "", unit or "")
    u = _HONORIFIC.sub("", u)
    if not u or len(u) > 5 or re.search(r"[0-9０-９、。，．？?！!「」]", u):
        return ""
    return u


def by_rule(question: str) -> str:
    """質問の文の数える言葉から決める。「何名」の形を先に使い、無ければ「人数」の形を使う。見つからない・意味の違う単位が2つ以上
    ある（「何泊何日」）ときは空。"""
    asked = [u for u in (clean_unit(m.group(1)) for m in _ASK.finditer(question)) if u]
    counted = [u for u in (clean_unit(m.group(1)) for m in _COUNT_OF.finditer(question)) if u]
    found = asked + counted
    if not found or any(not same_unit(found[0], u) for u in found[1:]):
        return ""
    return found[0]


PROMPT = (
    "電話でお客様に聞く質問の一覧です。どの質問も、答えが数になる質問です。質問ごとに、次の2つを答えてください。\n"
    "(1) counted：その質問で、何の数を確かめたいのか（例：「ご来店の人数をお伺いしてもよろしいでしょうか？」⇒「来店する人」）。\n"
    "(2) unit：お客様が数字だけで答えたとき（例：「3です」）、言い直しで数字のすぐあとに付ける、数える言葉（例：「人」⇒「3人」）。\n"
    "決まり：\n"
    "- 質問の文に数える言葉（「何名」「何台」「何泊」など）があれば、その言葉を使います。\n"
    "- 質問の文に無くても、数えるものから数える言葉が1つに決まるなら、電話でふつうに使う言葉を書きます（例：「従業員数」⇒「人」）。\n"
    "- 数える言葉が2つ以上考えられて1つに決まらないとき（広さ＝平米・畳・坪、期間＝日・か月・年 など）や、"
    "数える言葉が2つ要る質問（「何泊何日」）は、unit を空にし、reason にわけを書きます。推測で決めないでください。\n"
    "- 「約」「くらい」など数字の前後の言葉や、「様」は付けません。数字も書きません。\n"
    "質問（番号：業務の目的／質問の文）：\n"
)


def _schema():
    from google.genai import types

    item = types.Schema(type=types.Type.OBJECT, properties={
        "no": types.Schema(type=types.Type.INTEGER, description="質問の番号"),
        "counted": types.Schema(type=types.Type.STRING, description="何の数を確かめたいのか"),
        "unit": types.Schema(type=types.Type.STRING, description="数字のすぐあとに付ける数える言葉。決まらなければ空"),
        "reason": types.Schema(type=types.Type.STRING, description="unit を空にしたわけ"),
    }, required=["no", "counted", "unit"])
    return types.Schema(type=types.Type.OBJECT, properties={"items": types.Schema(type=types.Type.ARRAY, items=item)},
                        required=["items"])


def by_model(client, model: str, questions: list[tuple[str, str]]) -> list[dict]:
    """questions＝[(業務の目的, 質問の文)]。1回の問い合わせでまとめて答えさせる（取込は業務の版ごとに1回）。
    戻り値は questions と同じ並びの [{"counted", "unit"(そろえたもの), "raw_unit", "reason"}]。答えの無い質問は unit を空にする。"""
    from google.genai import types

    body = "\n".join(f"{n}：{purpose}／{q}" for n, (purpose, q) in enumerate(questions, 1))
    r = client.models.generate_content(
        model=model, contents=PROMPT + body,
        config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=_schema(), temperature=0))
    got = {x.get("no"): x for x in json.loads(r.text).get("items", [])}
    out = []
    for n in range(1, len(questions) + 1):
        x = got.get(n, {})
        out.append({"counted": x.get("counted", ""), "raw_unit": x.get("unit", ""), "unit": clean_unit(x.get("unit")),
                    "reason": x.get("reason", "")})
    return out


# 数字だけの値：数（数字・漢数字）が、区切り（「、」「か」「から」「〜」）でつながっているだけで、前後に「約」「くらい」などがあってもよい
_NUM = r"[0-9０-９〇一二三四五六七八九十百千万]+"
_BARE = re.compile(rf"^(?P<pre>約|およそ|だいたい|大体)?(?P<nums>{_NUM}(?:(?:、|,|，|・|〜|～|-|から|か){_NUM})*)"
                   r"(?P<post>くらい|ぐらい|ほど|程度|前後|以上|以下|未満|位)?$")


def supply_unit(value: str | None, unit: str) -> str | None:
    """数字だけの値に単位を補う（「3」⇒「3人」・「2か3」⇒「2か3人」・「3くらい」⇒「3人くらい」）。単位が空・値に単位が
    もう付いている（数字のあとに数字でも区切りでもない文字がある）ときは、そのまま返す。"""
    if not value or not unit:
        return value
    m = _BARE.match(value)
    if not m:
        return value
    return f"{m['pre'] or ''}{m['nums']}{unit}{m['post'] or ''}"



def agree(a: str, b: str) -> str:
    """2つのモデルの答えを合わせる。同じ意味ならその単位。片方が「つ」（どんな物にも使える数え方）で、もう片方に数える言葉があれば、
    数える言葉のほう（「いくつ」の質問で「台」と「つ」に分かれる・「おいくつ」で「歳」と「つ」に分かれる＝G-41）。それ以外は空。"""
    if a and b and same_unit(a, b):
        return a
    if a == "つ" and b:
        return b
    if b == "つ" and a:
        return a
    return ""


def decide(question: str, model_units: list[str]) -> str:
    """単位を1つに決める（取込のとき。人は確かめない）。model_units＝別々の文字のモデルの答え（clean_unit をとおしたもの）。
    (1) 質問の文の数える言葉（by_rule）で決まれば、それを使う（G-41：76問中60問がここで決まり、誤りは0）。
    (2) 決まらなければ、2つのモデルの答えが合うとき（agree）だけ使う。1つでも空・食い違いなら空。
    空のときは単位を補わない（いまと同じ「3で大丈夫でしょうか？」のまま）。
    G-41：(2) を1つのモデルだけにすると、3.7 Flash・3.5 Flash Lite で誤りが出た（「おいくつ」⇒「つ」・「通塾の時間」⇒「分」）。
    2つのモデルを合わせると、試した21の組のどれでも誤りは0（補わないのが0〜2問）。"""
    rule = by_rule(question)
    if rule:
        return rule
    if len(model_units) < 2:
        return ""
    unit = model_units[0]
    for other in model_units[1:]:
        unit = agree(unit, other)
        if not unit:
            return ""
    return unit
