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
# 選択肢ごとの、いまの話の向きを伺う言葉（flowctl.Question.doubt_words・phrases.CONFIRM の「いまの話の向きを伺う」）。事実を伺う質問ではない質問だけ。
# ✅ 2026-11-01 日本時間22時31分 ユーザー：「「もういいです。」のパターンでも、「パンフレットはお送りしない方がいいということでしょうか？」の方がよくないですか？」
# （例の質問は、測る質問に入れないもの＝G-48 の決まり）
DOUBT_PROMPT = (
    "電話でお客様に聞く、選択肢のある質問の一覧です。お客様が「もういいです。」のように、いまの質問への答えとも、この電話を断る言葉とも取れる"
    "ことを言ったとき、まず、いまの質問への答えの向きを、次の言い方で伺います。\n"
    "「（　）ということでしょうか？」\n"
    "質問の選択肢ごとに、お客様がその選択肢を答えたという意味を、お客様のお気持ち・ご予定として（　）に入れる言葉で words に書いてください。\n"
    "決まり：\n"
    "- こちらの申し出（お送りする・お電話する・ご訪問する など）を受けるかの質問は、「〜した方がいい」「〜しない方がいい」で終えます。\n"
    "- お客様のご予定・お選び・ご様子を伺う質問は、お客様のご予定・お選びとして言い切る形にします（「〜をご利用になる」「〜にはお越しにならない」"
    "「〜がいい」）。こちらが勧めているように聞こえる「〜方がいい」にはしません。\n"
    "- 打ち消しの問い（「〜ではございませんか？」「〜はございませんか？」）の「はい」は、打ち消しのほうの意味です"
    "（「お困りではございませんか？」の「はい」＝お困りではない）。\n"
    "- 「ということ」「こと」は書きません（あとに続きます）。\n"
    "- 質問の言葉をできるだけそのまま使います。質問に無い意味を足しません。\n"
    "- お客様に向ける丁寧な言い方にします（「ご」「お」を付けるなど）。\n"
    "- 「、」（読点）を入れません。「？」で終えません。\n"
    "- 例：「カタログをお送りしてもよろしいでしょうか？」の「はい」⇒「カタログをお送りした方がいい」、「いいえ」⇒「カタログはお送りしない方がいい」／"
    "「送迎バスはご利用になりますでしょうか？」の「利用しない」⇒「送迎バスはご利用にならない」／"
    "「お届けは午前と午後のどちらがご都合よろしいでしょうか？」の「午前」⇒「お届けは午前がいい」\n"
    "質問（番号：質問の文／選択肢）：\n"
)
# 選択肢ごとの、こちらがすること（お客様がされること）の言葉（flowctl.Question.do_words・phrases.CONFIRM の「言い切っていない答えのあと（こちらがすること）」）。
# 事実を伺う質問ではない質問だけ。✅ 2026-11-01 日本時間20時56分 ユーザー：「…「それではパンフレットをお送りさせていただきますね？」の方がいいんじゃないですか？」・
# 11-02 日本時間1時22分「「はい、それではパンフレットの送付は控えさせていただきますね？」の方がいいんじゃないですか！」
DO_PROMPT = (
    "電話でお客様に聞く、選択肢のある質問の一覧です。お客様の答えが少しあいまいでも、向きが決まったとき（「うーん、送ってもらおうかな…」など）は、"
    "その答えを受けて、こちらがすること（またはお客様がされること）を、次の言い方で伝えて確かめます。\n"
    "「それでは（　）させていただきますね？」\n"
    "質問の選択肢ごとに、お客様がその選択肢を答えたときに（　）に入れる言葉を words に書いてください。\n"
    "決まり：\n"
    "- 「させていただきますね？」にそのまま続けて読める形にします。「させていただ」は書きません（あとに続きます）。\n"
    "- こちらの申し出（お送りする・お電話する・ご訪問する など）を受けるかの質問：受ける向きは、こちらがすること（「〜をお送り」「〜のお電話を差し上げ」）、"
    "断る向きは「〜は控え」（こちらがしない）にします。\n"
    "- お客様のご予定・お選び・ご様子を伺う質問：お客様のお答えを承る「〜と」の形にします（「〜はカードと」「〜はご利用なしと」「〜はご欠席と」）。"
    "「控え」は使いません（こちらが控える意味になります）。\n"
    "- 打ち消しの問い（「〜ではございませんか？」「〜はございませんか？」）の「はい」は、打ち消しのほうの意味です"
    "（「お困りではございませんか？」の「はい」＝お困りではない）。\n"
    "- 「させていただきますね？」に続けたときに、へりくだりの言い方が重ならない形にします（「差し上げ」「お伺い」などのあとに続けない）。\n"
    "- お客様の言ったことより強くしません（考えている・迷っているという答えを、決めたことにしない）。\n"
    "- 質問の言葉をできるだけそのまま使います。質問に無いこと（約束・日時・金額など）を足しません。\n"
    "- 丁寧な言い方にします（「ご」「お」を付けるなど）。\n"
    "- 「、」（読点）を入れません。「？」で終えません。\n"
    "- 例：「カタログをお送りしてもよろしいでしょうか？」の「はい」⇒「カタログをお送り」、「いいえ」⇒「カタログの送付は控え」／"
    "「担当者からお見積もりのお電話を差し上げてもよろしいでしょうか？」の「はい」⇒「担当者からお見積もりのお電話を」／"
    "「来月のお申し込みをお考えでしょうか？」の「はい」⇒「来月のお申し込みをご検討中と」／"
    "「送迎バスはご利用になりますでしょうか？」の「利用する」⇒「送迎バスはご利用と」、「利用しない」⇒「送迎バスはご利用なしと」／"
    "「お届けは午前と午後のどちらがご都合よろしいでしょうか？」の「午前」⇒「お届けは午前と」\n"
    "質問（番号：質問の文／選択肢）：\n"
)
# 言葉の種類（唯一の場所）：意味の言葉（判定に見せる選択肢の名前・言い直し）・いまの話の向きを伺う言葉・こちらがすることの言葉。作り方・確かめ・決め方は同じ1つ
PROMPTS = {"meaning": PROMPT, "doubt": DOUBT_PROMPT, "do": DO_PROMPT}


def _schema():
    from google.genai import types

    item = types.Schema(type=types.Type.OBJECT, properties={
        "no": types.Schema(type=types.Type.INTEGER, description="質問の番号"),
        "choice": types.Schema(type=types.Type.STRING, description="選択肢（書いてあるとおり）"),
        "words": types.Schema(type=types.Type.STRING, description="（　）に入れる言葉"),
    }, required=["no", "choice", "words"])
    return types.Schema(type=types.Type.OBJECT, properties={"items": types.Schema(type=types.Type.ARRAY, items=item)},
                        required=["items"])


def by_model(client, model: str, questions: list[tuple[str, tuple]], kind: str = "meaning") -> list[dict]:
    """questions＝[(質問の文, 選択肢の並び)]。1回の問い合わせでまとめて答えさせ、質問ごとに {選択肢: 言葉}（そろえたもの）を返す。kind＝言葉の種類（PROMPTS）。"""
    from google.genai import types

    body = "\n".join(f"{n}：{q}／{'・'.join(c)}" for n, (q, c) in enumerate(questions, 1))
    r = client.models.generate_content(
        model=model, contents=PROMPTS[kind] + body,
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


def check(words: str, question: str, choice: str, kind: str = "meaning") -> bool:
    """言い方の（言葉）に入れられる形か：空でない・長すぎない・区切りや問いかけを含まない・返事の言葉だけでない・質問か選択肢の中の言葉（名詞）を
    1つは使っている（質問に無いことを作り出していない目安）。種類ごとに、続く言葉と重ならない形か：意味の言葉＝「（言葉）でよろしいでしょうか？」／
    いまの話の向きを伺う言葉＝「〜方がいい」で終わり、「ということ」を含まない（「（言葉）ということでしょうか？」）／こちらがすることの言葉＝「させて」を
    含まず、言い切りの語尾で終わらない（「それでは（言葉）させていただきますね？」）。"""
    from jtext import words as split

    w = words.strip()
    # 長さの上限は質問の長さに合わせる（⛔ 2026-11-02 G-72：30字までの決まりで、長い質問「担当者から詳しいご案内のお電話を差し上げてもよろしいでしょうか？」の
    # 意味の言葉が、2つのモデルとも向きは正しいのに決まらなかった）。質問より10字を超えて長い言葉は、質問に無いことを足した目安
    if not w or len(w) > max(30, len(question) + 10) or re.search(r"[、。，．？?！!「」『』]", w) or _BARE.fullmatch(w):
        return False
    if re.search(r"(?:でしょうか|ですか|ますか|よろしい)$", w):
        return False
    if kind == "doubt" and ("ということ" in w or w.endswith("こと")):
        return False
    if kind == "do" and (re.search(r"させて|させ$", w) or re.search(r"(?:ます|です|ね|ください)$", w)):
        return False
    base = unicodedata.normalize("NFKC", question + choice)
    nouns = [x.surface for x in split(w) if x.feature.pos1 == "名詞" and len(x.surface) >= 2]
    return any(n in base for n in nouns)


# 打ち消しの言葉（「されていない」「なし」「不要」「お送りしなくて」「ございません」）。意味の言葉の向き（認める側か打ち消す側か）を比べる
# 「控え」＝こちらの申し出を断る向き（「パンフレットの送付は控え」）・「欠席」「見送り」＝お客様の選びの断る向き（こちらがすることの言葉＝「説明会はご欠席と」）
_NEGATION = re.compile(r"ない|なく|なし|無し|不要|ません|しない|ず$|控え|欠席|見送り")


def negated(words: str) -> bool:
    return bool(_NEGATION.search(words))


REPLY_PROMPT = (
    "電話でお客様に、選択肢のある質問をしたとき、お客様が「{words}」のどれかだけを答えることがあります。"
    "これらの言葉は、前の質問によって、受け入れの意味にも、断りの意味にもなります。\n"
    "質問ごとに、それぞれの言葉が、その質問への答えとして、選択肢のどれの意味になるかを choice に書いてください。\n"
    "決まり：\n"
    "- 質問の文から、だれが聞いても一つに決まるときだけ、選択肢を書きます。\n"
    "- 受け入れとも断りとも取れるとき・選択肢を選んでいないときは、choice を空にします。迷ったら空にします。\n"
    "- 例：「ご案内のメールをお送りしてもよろしいでしょうか？」（送る・送らない）の「結構です」⇒「送らない」（お断りの意味）、"
    "「大丈夫です」⇒ 空（「送ってよい」とも「要らない」とも取れる）\n"
    "質問（番号：質問の文／選択肢）：\n"
)


def _reply_schema():
    from google.genai import types

    item = types.Schema(type=types.Type.OBJECT, properties={
        "no": types.Schema(type=types.Type.INTEGER, description="質問の番号"),
        "word": types.Schema(type=types.Type.STRING, description="お客様の言葉（書いてあるとおり）"),
        "choice": types.Schema(type=types.Type.STRING, description="選択肢（書いてあるとおり）。決まらなければ空"),
    }, required=["no", "word", "choice"])
    return types.Schema(type=types.Type.OBJECT, properties={"items": types.Schema(type=types.Type.ARRAY, items=item)},
                        required=["items"])


def by_model_replies(client, model: str, questions: list[tuple[str, tuple]]) -> list[dict]:
    """受け入れにも断りにも取れる言葉（flowctl.AMBIGUOUS_WORDS）だけの答えが、質問ごとにどの選択肢の意味になるか。
    1回の問い合わせでまとめて答えさせ、質問ごとに {言葉: 選択肢}（決まらない言葉は入れない）を返す。"""
    from google.genai import types

    from flowctl import AMBIGUOUS_WORDS

    said = {w: f"{w}です" for w in AMBIGUOUS_WORDS}  # お客様の言葉の形（「結構です」）
    body = "\n".join(f"{n}：{q}／{'・'.join(c)}" for n, (q, c) in enumerate(questions, 1))
    r = client.models.generate_content(
        model=model, contents=REPLY_PROMPT.format(words="」「".join(said.values())) + body,
        config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=_reply_schema(), temperature=0))
    out = [{} for _ in questions]
    for x in json.loads(r.text).get("items", []):
        n, word, choice = x.get("no"), clean(x.get("word")), clean(x.get("choice"))
        key = next((w for w in AMBIGUOUS_WORDS if word in (w, said[w])), None)
        if isinstance(n, int) and 1 <= n <= len(questions) and key and choice in questions[n - 1][1]:
            out[n - 1][key] = choice
    return out


def decide_replies(choices: tuple, model_replies: list[dict]) -> dict:
    """言葉ごとに、2つ以上のモデルが同じ選択肢を答えたときだけ、その選択肢（取込のとき。人は確かめない）。決まらない言葉は入れない
    （通話の中で、AIの受け取った意味を確かめる＝flowctl）。⚠️ 誤って決めると、相手に確かめずに違う答えが渡る＝決めない側に倒す。"""
    from flowctl import AMBIGUOUS_WORDS

    out = {}
    for w in AMBIGUOUS_WORDS:
        got = [m.get(w) for m in model_replies]
        if len(got) >= 2 and got[0] in choices and all(g == got[0] for g in got[1:]):
            out[w] = got[0]
    return out


def decide(question: str, choices: tuple, model_words: list[dict], kind: str = "meaning", meanings: dict | None = None) -> dict:
    """意味の言葉を決める（取込のとき。人は確かめない）。選択肢ごとに、2つ以上のモデルの言葉がどれも確かめに通り、打ち消しの有無が
    そろうときだけ、最初のモデルの言葉を使う。⚠️ 一番こわい誤りは向きの取り違え（「はい」に「されていない」）＝相手が認めると逆の答えが渡る。
    向きがモデルどうしで食い違えば決めない。⛔ G-48 の1回目：言葉が文字まで同じときだけにすると、51個中9個が「初めて」と「初めてということ」、
    「ご参加いただけない」と「ご参加されない」のような、意味の同じ言い違いで決まらなかった（どれも意味の取り違えは0）。
    kind＝言葉の種類（PROMPTS）。⛔ 2026-11-02 G-72：いまの話の向きを伺う言葉・こちらがすることの言葉で、2つのモデルがそろって向きを取り違えた（打ち消しの問い「お急ぎではございませんか？」の
    「はい」＝お急ぎではない、を「お急ぎと」）＝同じ種類の言葉どうしの比べでは止められない ⇒ 意味の言葉（meanings。もう決まったもの）がある選択肢は、
    向きがそれとそろうときだけ使う（決まらなければ、言い直しの今までの形に戻る＝安全側）。"""
    out = {}
    for c in choices:
        got = [m.get(c, "") for m in model_words]
        same = {negated(g) for g in got} | ({negated(meanings[c])} if meanings and meanings.get(c) else set())
        if len(got) >= 2 and all(g and check(g, question, c, kind) for g in got) and len(same) == 1:
            out[c] = got[0]
    return out
