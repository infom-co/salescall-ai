"""取込のときの名前の洗い出し（Intent.md 6-8）の試作。トークスクリプトの文から、名前（固有名詞）と読みを出す唯一の場所。

出し方（どれもここに置く。G-42 で比べて decide の形を決めた）：
- by_tokenizer：日本語を単語に分ける処理（fugashi＋unidic-lite。AIを使わない）。固有名詞の印が付いた単語と、その読み
- by_model：文字のモデル（Gemini の Flash など）に、決まった指示（PROMPT）と決まった答えの形（_schema）で出させる。
  台本をまとめて1回で答えさせる（by_model_scripts。取込は業務の版ごとに1回）
- given_reading：台本の文に書いてある読みがな（「東海林（しょうじ）」）を、プログラムが読む
- quote_by_model：比べるためだけの別の形。AIに、文へ直接「」を付けさせる（G-23。使わない）
✅ 2026-10-16 ユーザー：「人は基本的に介在しないと何度も言っているよね！」⇒ 取込のプレビューで人が確かめる形（前の Intent.md 6-8）は
   やめる。読みが1つに決まらないものは、AIかプログラムが「決まらない」と分かることを確かめ、決まらない名前の扱いは決まりで決める。
"""
from __future__ import annotations

import json
import unicodedata

PROMPT = (
    "電話をかけるときのトークスクリプト（台本）が、いくつかあります。台本ごとに、文の中の固有名詞（会社名・団体名・人名・商品名・"
    "サービス名・地名・建物名・店名）を、すべて抜き出してください。台本どうしは別のものです（ほかの台本を手がかりにしない）。\n"
    "決まり：\n"
    "- 「株式会社」「様」「店」「営業部」などの言葉は含めず、名前の部分だけを書きます（例：「株式会社アオバ物流」⇒「アオバ物流」）。\n"
    "- 普通の言葉（予約・資料・本社・事務所・担当 など）は含めません。固有名詞が無ければ、その台本の名前は無しにします。\n"
    "- 読みは、ひらがなで書きます。英字や記号を含む名前も、声に出すときの読みを書きます。\n"
    "- 文の中に名前の読みがな（名前のすぐあとのかっこの中のかな など）が書いてあれば、それを読みにします。\n"
    "- 読みが文から1つに決まらないもの（人名や地名で、読み方が何通りもあるもの。濁る・濁らないの違いも含む）は、uncertain を true にし、"
    "考えられる読みを candidates に並べます。文の中に読みを決める手がかり（地域・よく知られた会社名・読みがな など）があれば、それを使って"
    " reading を決め、uncertain は false にします。\n"
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
        "script": types.Schema(type=types.Type.INTEGER, description="台本の番号"),
        "surface": types.Schema(type=types.Type.STRING, description="文の中の書き方（名前の部分だけ）"),
        "reading": types.Schema(type=types.Type.STRING, description="読み（ひらがな）"),
        "kind": types.Schema(type=types.Type.STRING, enum=KINDS),
        "uncertain": types.Schema(type=types.Type.BOOLEAN, description="読みが1つに決まらない"),
        "candidates": types.Schema(type=types.Type.ARRAY, items=types.Schema(type=types.Type.STRING), description="考えられる読み"),
    }, required=["script", "surface", "reading", "kind", "uncertain"])
    return types.Schema(type=types.Type.OBJECT, properties={"names": types.Schema(type=types.Type.ARRAY, items=item)},
                        required=["names"])


def by_model_scripts(client, model: str, scripts: list[str]) -> list[list[dict]]:
    """台本（文をつないだもの）をまとめて1回で答えさせる。scripts と同じ並びの、名前の一覧を返す。"""
    from google.genai import types

    body = "\n".join(f"台本{n}：{t}" for n, t in enumerate(scripts, 1))
    r = client.models.generate_content(
        model=model, contents=PROMPT + "台本：\n" + body,
        config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=_schema(), temperature=0))
    out = [[] for _ in scripts]
    for x in json.loads(r.text).get("names", []):
        n = x.get("script")
        if isinstance(n, int) and 1 <= n <= len(scripts):
            out[n - 1].append(x)
    return out


def by_model(client, model: str, script: str) -> list[dict]:
    return by_model_scripts(client, model, [script])[0]


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


def quote(name: str) -> str:
    """名前を「」でくくる唯一の場所（instruction.quote_in_text・Business・流れの制御の名前の読みの確かめが使う）。"""
    return f"「{name}」"


# 会社・法人の種類（法人格）と読み。自社名は書いてあるまま名乗り（前に付く・後ろに付く・株式会社でない会社も）、名前の印（「」）と読みへの
# 置き換えは、種類を除いた名前の部分だけにする（G-17：「株式会社「アオバ物流」」の形で、名前の聞き違いが 12回中4回 → 13回中2回に減った）。
# ⛔ 2026-10-21 CL-2 の 4-121 で見つけた誤り：名乗りが、いつも「株式会社」＋自社名だった（「大和ハウス工業」は「株式会社大和ハウス工業」と
#    名乗り、合同会社・お店の名前でも株式会社と名乗っていた）。長いものを先に当てる（「医療法人社団」は「医療法人」より先）
LEGAL_FORMS = {"株式会社": "かぶしきがいしゃ", "有限会社": "ゆうげんがいしゃ", "合同会社": "ごうどうがいしゃ", "合資会社": "ごうしがいしゃ",
               "合名会社": "ごうめいがいしゃ", "一般社団法人": "いっぱんしゃだんほうじん", "一般財団法人": "いっぱんざいだんほうじん",
               "公益社団法人": "こうえきしゃだんほうじん", "公益財団法人": "こうえきざいだんほうじん",
               "特定非営利活動法人": "とくていひえいりかつどうほうじん", "医療法人社団": "いりょうほうじんしゃだん",
               "医療法人財団": "いりょうほうじんざいだん", "医療法人": "いりょうほうじん", "社会福祉法人": "しゃかいふくしほうじん",
               "学校法人": "がっこうほうじん", "税理士法人": "ぜいりしほうじん", "弁護士法人": "べんごしほうじん",
               "司法書士法人": "しほうしょしほうじん", "行政書士法人": "ぎょうせいしょしほうじん",
               "社会保険労務士法人": "しゃかいほけんろうむしほうじん", "監査法人": "かんさほうじん"}
# 略した書き方（「(株)」「㈱」など）は、声に出すと読み違えるので、正式な書き方に直してから名乗る
LEGAL_FORM_SHORT = {"(株)": "株式会社", "㈱": "株式会社", "(有)": "有限会社", "㈲": "有限会社", "(同)": "合同会社", "(資)": "合資会社",
                    "(名)": "合名会社", "(一社)": "一般社団法人", "(一財)": "一般財団法人", "(公社)": "公益社団法人", "(公財)": "公益財団法人",
                    "(医)": "医療法人", "(福)": "社会福祉法人", "(学)": "学校法人", "(税)": "税理士法人", "(弁)": "弁護士法人"}


def legal_form_split(company: str) -> tuple[str, str, str]:
    """自社名を（前に付く会社の種類, 名前の部分, 後ろに付く会社の種類）に分ける唯一の場所（Business._said・読みの探し方が使う）。
    種類が無ければ ("", 自社名, "")。略した書き方は正式な書き方に直す。"""
    s = unicodedata.normalize("NFKC", company).strip()
    for short, full in LEGAL_FORM_SHORT.items():
        s = s.replace(unicodedata.normalize("NFKC", short), full)
    forms = sorted(LEGAL_FORMS, key=len, reverse=True)
    head = next((f for f in forms if s.startswith(f) and len(s) > len(f)), "")
    rest = s[len(head):].strip()
    tail = next((f for f in forms if rest.endswith(f) and len(rest) > len(f)), "")
    return head, rest[:len(rest) - len(tail)].strip(), tail


def legal_form_reading_off(reading: str, company: str) -> str:
    """名前の部分の読み：自社名の読みに会社の種類の読みが入っていたら外す（読みは名前の部分に当てる＝Business._said）。"""
    head, _, tail = legal_form_split(company)
    r = reading.strip()
    if head and r.startswith(LEGAL_FORMS[head]):
        r = r[len(LEGAL_FORMS[head]):]
    if tail and r.endswith(LEGAL_FORMS[tail]):
        r = r[:len(r) - len(LEGAL_FORMS[tail])]
    return r


def given_reading(text: str, surface: str) -> str:
    """台本の文の中で、名前のすぐあとに書いてある読みがな（「東海林（しょうじ）」「角田(つのだ)」）。無ければ空。"""
    import re

    t = unicodedata.normalize("NFKC", text)
    m = re.search(re.escape(unicodedata.normalize("NFKC", surface)) + r"\s*\(\s*([ぁ-んァ-ヶー]+)\s*\)", t)
    return hira(m.group(1)) if m else ""


def hira(s: str) -> str:
    """カタカナをひらがなに（空白は外す）。"""
    return "".join(chr(ord(c) - 0x60) if "ァ" <= c <= "ヶ" else c for c in (s or "") if not c.isspace())


def unvoiced(s: str) -> str:
    """ひらがなの濁点・半濁点を外した形（「かどだ」⇒「かとた」、「なかじま」⇒「なかしま」）。読みを比べるときだけに使う。
    会話のAIは、同じ漢字の名前の読みの濁りの違いを聞き分けられない（G-45：「なかしま」と直されて「なかじま」と報告／
    G-46：「カドタ」と直されて「かどだ」と報告）ので、AIの聞いた読みと読みの候補を、この形で比べる（flowctl.next_reading）。"""
    out = unicodedata.normalize("NFD", hira(s))
    return unicodedata.normalize("NFC", "".join(c for c in out if c not in "\u3099\u309a"))


def decide(text: str, model_runs: list[list[dict]]) -> list[dict]:
    """名前の一覧と読みを決める（取込のとき。人は確かめない）。model_runs＝別々の文字のモデルの答え（by_model の戻り値）。
    名前：どちらかのモデルが出したもの（G-42：見落とし0・余分0。両方が出したものだけにすると1つ見落とした）。単語分けは使わない
          （G-42：見落とし17・読みの誤り8）。書き方の粒の違う名前（「兵庫県三田市」と「三田市」）は両方残す（くくるのは長いほうが先）。
    読み：(1) 台本に書いてある読みがな（given_reading）(2) 2つのモデルの読みが同じで、どちらも「決まらない」としていないとき
          (3) それ以外は空＝読みが決まらない（candidates に候補）。G-42：2つのモデルを合わせると、読みを誤って決めたのは0、
          文から読みが決まらない6つ（中島・山崎・角田・河野・新垣・場所の手がかりの無い三田）は6つとも「決まらない」になった。
    戻り値：[{"surface", "reading"（空＝決まらない）, "source"（"台本の読みがな"・"モデル2つ"・""）, "candidates", "likely"（決まらないとき、
    まず言ってみる読み）}]"""
    from jtext import compare_reading

    rd = lambda s: compare_reading(s or "").replace(" ", "")  # noqa: E731
    found: dict[str, list[dict]] = {}
    for rows in model_runs:
        seen = set()
        for x in rows:
            key = norm_surface(x.get("surface", ""))
            if key and key not in seen:
                seen.add(key)
                found.setdefault(key, []).append(x)
    out = []
    for key, hits in found.items():
        surface = hits[0].get("surface", key)
        given = given_reading(text, surface)
        if given:
            out.append({"surface": surface, "reading": given, "source": "台本の読みがな", "candidates": []})
            continue
        sure = len(hits) == len(model_runs) >= 2 and not any(x.get("uncertain") for x in hits) and \
            all(x.get("reading") and rd(x["reading"]) == rd(hits[0]["reading"]) for x in hits)
        if sure:
            out.append({"surface": surface, "reading": hira(hits[0]["reading"]), "source": "モデル2つ", "candidates": []})
            continue
        cands = []
        for x in hits:
            for c in [x.get("reading", "")] + list(x.get("candidates") or []):
                if c and rd(c) not in {rd(y) for y in cands}:
                    cands.append(hira(c))
        # likely＝会話の中で確かめるときに、まず言ってみる読み（2つのモデルの reading が同じならそれ、違えば先のモデルの reading）。
        # ✅ 2026-10-16 ユーザー：「なかじまとなかしまなどは人が見ても判断できません。会話の中で失礼のないように語るか、場合によっては
        #    聞き出す」⇒ 流れの制御の名前の読みの確かめ（flowctl.reading_question）で、この読みを言って相手に確かめていただく
        likely = hira(hits[0].get("reading", "")) if hits[0].get("reading") else (cands[0] if cands else "")
        out.append({"surface": surface, "reading": "", "source": "", "candidates": cands, "likely": likely})
    return out
