"""取込のときの名前の洗い出し（Intent.md 6-8）の試作。トークスクリプトの文から、名前（固有名詞）の候補と読みを出す唯一の場所。

出し方は2つあり、どちらもここに置く（G-23 で比べる）：
- by_tokenizer：日本語を単語に分ける処理（fugashi＋unidic-lite。AIを使わない）。固有名詞の印が付いた単語と、その読み
- by_model：文字のモデル（Gemini の Flash など）に、決まった指示（PROMPT）と決まった答えの形（_schema）で出させる
- quote_by_model：比べるためだけの別の形。AIに、文へ直接「」を付けさせる（QUOTE_PROMPT。G-23 で、ほかの文字を変えないかを見る）
どちらの結果も「候補」であり、取込のプレビューで人が確かめる（Intent.md 6-8）。読みが1つに決まらないものは uncertain にする。
"""
from __future__ import annotations

import json
import unicodedata

PROMPT = (
    "次の文は、電話をかけるときのトークスクリプトです。文の中の固有名詞（会社名・団体名・人名・商品名・サービス名・地名・建物名・店名）を、"
    "すべて抜き出してください。\n"
    "決まり：\n"
    "- 「株式会社」「様」「店」「営業部」などの言葉は含めず、名前の部分だけを書きます（例：「株式会社アオバ物流」⇒「アオバ物流」）。\n"
    "- 普通の言葉（予約・資料・本社・事務所・担当 など）は含めません。固有名詞が無ければ、空の一覧にします。\n"
    "- 読みは、ひらがなで書きます。英字や記号を含む名前も、声に出すときの読みを書きます。\n"
    "- 読みが文から1つに決まらないもの（人名や地名で、読み方が何通りもあるもの）は、uncertain を true にし、"
    "考えられる読みを candidates に並べます。文の中に読みを決める手がかり（地域など）があれば、それを使って reading を決めます。\n"
    "文：\n"
)
# 比べるための別の形：AIに、文へ直接「」を付けさせる（設計の形は、名前の一覧を出させて、プログラムが instruction.quote_in_text で付ける）
QUOTE_PROMPT = (
    "次の文は、電話をかけるときのトークスクリプトです。文の中の固有名詞（会社名・団体名・人名・商品名・サービス名・地名・建物名・店名）を、"
    "「」でくくってください。\n"
    "決まり：\n"
    "- 「」を付けること以外は、1文字も変えないでください（文字・句読点・空白・数字・全角と半角・漢字とかなの書き方も、そのまま）。\n"
    "- 「株式会社」「様」「店」「営業部」などの言葉は、かっこの外に置きます（例：株式会社アオバ物流の ⇒ 株式会社「アオバ物流」の）。\n"
    "- もともと「」『』でくくられている所は、そのままにします。固有名詞が無ければ、文をそのまま返します。\n"
    "文：\n"
)
KINDS = ["会社名", "団体名", "人名", "商品名", "サービス名", "地名", "建物名", "店名", "その他"]


def _schema():
    from google.genai import types

    item = types.Schema(type=types.Type.OBJECT, properties={
        "surface": types.Schema(type=types.Type.STRING, description="文の中の書き方（名前の部分だけ）"),
        "reading": types.Schema(type=types.Type.STRING, description="読み（ひらがな）"),
        "kind": types.Schema(type=types.Type.STRING, enum=KINDS),
        "uncertain": types.Schema(type=types.Type.BOOLEAN, description="読みが1つに決まらない"),
        "candidates": types.Schema(type=types.Type.ARRAY, items=types.Schema(type=types.Type.STRING), description="考えられる読み"),
    }, required=["surface", "reading", "kind", "uncertain"])
    return types.Schema(type=types.Type.OBJECT, properties={"names": types.Schema(type=types.Type.ARRAY, items=item)},
                        required=["names"])


def by_model(client, model: str, script: str) -> list[dict]:
    from google.genai import types

    r = client.models.generate_content(
        model=model, contents=PROMPT + script,
        config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=_schema(), temperature=0))
    return json.loads(r.text).get("names", [])


def quote_by_model(client, model: str, script: str) -> str:
    from google.genai import types

    schema = types.Schema(type=types.Type.OBJECT, properties={"text": types.Schema(type=types.Type.STRING)}, required=["text"])
    r = client.models.generate_content(
        model=model, contents=QUOTE_PROMPT + script,
        config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=schema, temperature=0))
    return json.loads(r.text).get("text", "")


def by_tokenizer(script: str) -> list[dict]:
    from jtext import reading, words

    out = []
    for w in words(script):
        f = w.feature
        if f.pos1 == "名詞" and f.pos2 == "固有名詞":
            kana = getattr(f, "kana", None) or ""
            out.append({"surface": w.surface, "reading": reading(kana) if kana else "", "kind": f.pos3 or "その他",
                        "uncertain": False, "candidates": []})
    return out


def norm_surface(s: str) -> str:
    s = unicodedata.normalize("NFKC", s)
    for w in ("株式会社", "様", "さま", "『", "』", "「", "」", " ", "　"):
        s = s.replace(w, "")
    return s
