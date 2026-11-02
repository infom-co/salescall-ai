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
# 選択肢ごとの、目的につなげて伺う言葉（flowctl.Question.keep_words・away・phrases.CONFIRM の「言い切っていない答えのあと（目的につなげて伺う）」）。事実を
# 伺う質問ではない質問だけ。✅ 2026-11-03 ユーザー：「そもそも、導入の方向で進んでいるのに、「それでは、今月中のご導入は見送りとさせていただきますね？」だと、
# わざわざ、獲得できそうなお客様を失いかねない確認ですよね？そんな返答は営業トークとしてありえないんじゃないですか？」・(2)「OK」（目的から遠ざかる向きの
# 答えは、その向きで「させていただきますね？」とは言わず、目的につながる形で伺う）・(3)の答え「「それでは、来月以降にご導入ということでよろしいでしょうか？」」
# ⚠️ 例は試しの質問・試しの言葉と重ねない（判定と同じく、作らせるモデルも例の形を探す）
# ⛔ 2026-11-03（G-72・Haiku 5.5 が作った）：時期の例「来月のお申し込み」⇒「再来月以降に…」を置き、「今月中のご導入」の「いいえ」に2回とも「再来月以降の
#    ご導入」と写した（正しくは来月以降）・「お折り返し」「お差し上げ」と敬語を重ねた ⇒ 時期の例を外して意味で書き、へりくだる言葉に「お」を付けない決まりを足した
KEEP_PROMPT = (
    "電話で、こちらのご案内やお申し込みを進めるために、お客様に聞く選択肢のある質問の一覧です。お客様の答えが少しあいまいで、答えの向きが、こちらの"
    "目的（お客様にご利用・お申し込みいただく・こちらの申し出を受けていただく）から遠ざかる向きのとき、その向きのまま確かめると、お客様を目的から"
    "遠ざける一言になります。そこで、目的につながる形で、次の言い方で伺います。\n"
    "「それでは（　）でよろしいでしょうか？」\n"
    "質問の選択肢ごとに、お客様がその選択肢を答えたときに（　）に入れる言葉を words に書いてください。\n"
    "決まり：\n"
    "- 目的から遠ざかる向きの選択肢だけに書きます。目的に進む向きの選択肢と、目的に関わらない選び（お支払いの方法・時間帯・来られる手段・お席など）は、"
    "words を空にします。お客様のご様子やご都合を伺うだけの質問（来られる手段・ご人数・ご来店の時間帯など）も、目的に関わらないので空にします。\n"
    "- 打ち消しの問い（「〜ではございませんか？」「〜はございませんか？」）の「はい」は、打ち消しのほうの意味です"
    "（「お困りではございませんか？」の「はい」＝お困りではない）。向きは、その意味で決めます。\n"
    "- お客様の答えの向きは変えません（断りや見送りを、受けたことにしません）。そのうえで、目的への道を閉じない言い方にします。質問が時期を限って"
    "聞いているなら、その時期のすぐあとに同じことをする形にします（限った時期の名前を、その次の時期の名前に替える）。こちらの申し出を受けない答えなら、"
    "必要になったときにする形にします。\n"
    "- 「差し上げる」「伺う」「いただく」など、もともとへりくだる言葉には「お」「ご」を付けません。質問の言葉に無い「お」「ご」を足しません。\n"
    "- 質問に無い約束・日時・金額・別の品物を足しません。\n"
    "- 「でよろしいでしょうか？」にそのまま続けて読める形で、「ということ」で終えます。\n"
    "- 丁寧な言い方にします（「ご」「お」を付けるなど）。「、」（読点）を入れません。「？」で終えません。\n"
    "- 例：「カタログをお送りしてもよろしいでしょうか？」の「はい」⇒ 空、「いいえ」⇒「カタログは必要になりましたらお送りするということ」／"
    "「お届けは午前と午後のどちらがご都合よろしいでしょうか？」の「午前」「午後」⇒ どちらも空\n"
    "質問（番号：質問の文／選択肢）：\n"
)
# 言葉の種類（唯一の場所）：意味の言葉（判定に見せる選択肢の名前・言い直し）・いまの話の向きを伺う言葉・こちらがすることの言葉。作り方・確かめ・決め方は同じ1つ
PROMPTS = {"meaning": PROMPT, "doubt": DOUBT_PROMPT, "do": DO_PROMPT, "keep": KEEP_PROMPT}


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
    return _read_items(json.loads(r.text).get("items", []), questions)


def _read_items(items: list, questions: list[tuple[str, tuple]]) -> list[dict]:
    """答えの items を、質問ごとの {選択肢: 言葉} にそろえる（読み取りの唯一の場所＝Gemini も Anthropic も）。"""
    out = [{} for _ in questions]
    for x in items or []:
        n = x.get("no") if isinstance(x, dict) else None
        if isinstance(n, int) and 1 <= n <= len(questions) and clean(x.get("choice")) in questions[n - 1][1]:
            out[n - 1][clean(x.get("choice"))] = clean(x.get("words"))
    return out


def by_model_anthropic(client, model: str, questions: list[tuple[str, tuple]], kind: str = "meaning") -> list[dict]:
    """by_model と同じ文（PROMPTS）・同じ読み取りを、Anthropic（Claude Haiku 5.5）に、答えの形を指定した道具の呼び出しで返させる（作り手の
    代わり＝Gemini の Flash が混んでいる間。✅ 2026-11-03 ユーザー：「3.8 Flash の分の一部の場面もHaikuで作らせたらいいんじゃない？何度も言わせるな！」・
    「Haikuでもいいと言いましたよね。」）。料金は判定と同じ帳簿（meaning_judge.SPEND）。⚠️ temperature は送らない（Haiku 5.5 は断る）。"""
    from meaning_judge import SPEND

    body = "\n".join(f"{n}：{q}／{'・'.join(c)}" for n, (q, c) in enumerate(questions, 1))
    item = {"type": "object", "properties": {"no": {"type": "integer"}, "choice": {"type": "string"}, "words": {"type": "string"}},
            "required": ["no", "choice", "words"], "additionalProperties": False}
    schema = {"type": "object", "properties": {"items": {"type": "array", "items": item}}, "required": ["items"], "additionalProperties": False}
    SPEND.check()
    msg = client.messages.create(model=model, max_tokens=8000, messages=[{"role": "user", "content": PROMPTS[kind] + body}],
                                 tools=[{"name": "words", "description": "選択肢ごとの言葉", "input_schema": schema, "strict": True}],
                                 tool_choice={"type": "tool", "name": "words"})
    SPEND.add(model, msg.usage)
    got = next((b.input for b in msg.content if getattr(b, "type", "") == "tool_use"), None)
    if not isinstance(got, dict):
        raise ValueError(f"{model} が答えの形で返さなかった（{getattr(msg, 'stop_reason', '')}）")
    return _read_items(got.get("items", []), questions)


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
    if kind == "keep" and (not w.endswith("ということ") or re.search(r"させて|させていただ", w)):
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


def decide_keep(question: str, choices: tuple, model_words: list[dict]) -> tuple[tuple, dict]:
    """目的から遠ざかる向きの選択肢（away）と、目的につなげて伺う言葉（keep_words）を決める（取込のとき）。遠ざかる向きかは、どちらか1つのモデルでも
    言葉を書けば入れる（安全側＝その選択肢には「それでは…させていただきますね？」を使わない）。言葉は、2つ以上のモデルがどちらも書き、どちらも確かめに
    通るときだけ、最初のモデルの言葉（決まらなければ、押しも引きもしない今までの言い直しに戻る）。"""
    away, out = [], {}
    for c in choices:
        got = [m.get(c, "") for m in model_words]
        if any(got):
            away.append(c)
        if len(got) >= 2 and all(g and check(g, question, c, "keep") for g in got):
            out[c] = got[0]
    return tuple(away), out


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
