"""流れの制御（フェーズ0の試作。DESIGN.md 4-4・6-1・6-2・6-5・6-11 をそのまま形にしたもの）。

会話の位置・項目の状態・聞き返しと確かめの回数は、ここだけが持つ（Q6・Q7）。AIは操作を呼ぶだけで、
ここが確かめて「受け付けた」と返したものだけが記録になる（6-2）。G-6・G-16・G-13・G-5 の確かめは、すべてここを通す。

項目の状態（4-4 の名前だけを使う）：empty / heard / unclear / checking / confirmed / accepted / dont_know / unresolved / refused
すべての質問に、システムが2つの選択肢を足す（業務ごとに書き忘れない。I-19）：
  "unknown"（相手が「わからない」と答えた）⇒ dont_know。「取れた」には入らない。依頼主へは「-」。念のための確認はしない
    ⛔ 2026-10-13 ユーザー決定（CL-2 から依頼）：必須の質問が「わからない」なら、業務は完了ではない。前は accepted（＝取れた）に
       していたので、ほかがそろうと完了になっていた。⇒ 専用の状態にした（「取れた」は状態だけで決まる。条件を見なくてよい）
  "refused"（相手が答えを拒んだ）⇒ refused。依頼主へは「✕」。二度と聞かない
  ✅ 2026-10-13 ユーザー確定（DESIGN.md 6-2・4-4・7-1b）。設計の抜け（拒否を報告する操作が無い／「わからない」と
     答えた項目を念のため聞き直すことになっていた）を埋めたもの。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from names import hira, quote, unvoiced
from phrases import AI_ANSWER, AI_ASKED_AGAIN, AMPM_ASK, CALLBACK_ASK, CLOSE_DECLINED, CLOSING, LEAD_IN, NAME_READING_ASK
from phrases import SILENCE_PROMPT, SILENCE_PROMPT_IDS, UNKNOWN_ANSWER
from phrases import ask_again as ask_again_phrase
from phrases import confirm as confirm_phrase, restate_words
from phrases import lead_in as lead_in_phrase
from units import supply_unit

# 型の一覧（Q19）：その場で言い直して確かめる型。DESIGN.md 4-4
CONFIRM_TYPES = {"date", "time", "datetime", "number", "amount", "name", "phone", "email", "address"}
# 選択肢で答える型：依頼主へは、相手の言葉ではなく、宣言された選択肢を渡す（7-1b）
CHOICE_TYPES = {"yes_no", "choice"}
# 型の一覧（Q19）の全部。確かめない型は、選択肢の型と、自由な意見（free_text。2026-10-13 CL-2 の依頼で名前を決めた）
TYPES = CONFIRM_TYPES | CHOICE_TYPES | {"free_text"}
SYSTEM_CONDITIONS = ("unknown", "refused")
# 選択肢の質問（choice）だけにシステムが足す答え：相手がこちらに任せた（「どちらでも大丈夫です」）。✅ 2026-10-17 ユーザー：答えとして受ける
# （「いつでも」と同じ＝こちら次第のよい答え。「わからない」にしない）。⛔ G-14 のいまの形：AIは「どちらでも大丈夫です」を「わからない」3本・
# 「カード」2本・「答えたくない」1本と、合う選択肢が無いので分けた。はい／いいえの質問には足さない
ANY = "any"
STATES = ("empty", "heard", "unclear", "checking", "confirmed", "accepted", "dont_know", "unresolved", "refused")
# 値の形をそろえる（型の一覧 Q19 の決まり）：確かめる型は、言い直しに使う値なので、言い終わりの「です」「。」などは値に入れない
# ⛔「ね」「よ」「で」などの1文字は外さない（名前の終わり＝「あかね」などを削るため）
# 確かめない型（選択肢・自由な意見）は、相手の言葉のまま（自由な意見の「よかったです」を「よかった」に変えない）
_TAIL = re.compile(r"(?:です|でございます|になります)?[。、．！？!?\s]*$")
# 日時・数・金額の値の後ろの、頼む言葉（「15日の午後2時でお願いします」の「でお願いします」）も値に入れない。
# ⛔ G-46：AIが相手の言葉をそのまま値にして報告し、言い直しが「15日の午後2時でお願いしますということでよろしいでしょうか？」になった
#    （これまでの試験の値 約1,600個のうち1回）。⚠️「いつでも大丈夫です」の「大丈夫」は外さない（言い直しの形をユーザーに聞いていただいた）
_REQUEST_TAIL = re.compile(r"(?<=\S)で(?:お願い(?:します|いたします|致します|できますか|できますでしょうか|したいです))$")
# 空白に意味の無い型（日付・時刻・日時・数・金額）は、値の中の空白も外す。
# ⛔ 2026-10-14 G-34：相手の声を文字にしたもの（「15 日の午後 2 時でお願いします。」）を、AIがそのまま写して報告し、
#    値が「15 日の午後 2 時」になった（依頼主へ渡す値に空白が入る）。名前・住所・メールは、空白に意味がありうるので外さない
NO_SPACE_TYPES = {"date", "time", "datetime", "number", "amount"}
_SPACE = re.compile(r"[\s\u3000]+")
# 同じ型の値の漢数字は、数を数える言葉の前にあるものだけ、数字に直す（「十五日の午後二時」⇒「15日の午後2時」・「三人」⇒「3人」）。
# ✅ 2026-10-14 ユーザー承認：「日時と数の答えに限り、『日・時・分・人・名』などの前の漢数字を数字に直す」。⛔ G-36：AIが値を漢数字で
#    報告した（1回目・2回目に1本ずつ）＝意味は正しいが、依頼主へ渡す値の書き方がそろわない。数える言葉の前にない漢数字（「一緒」「一度」）は
#    直さない（「番」も数える言葉に入れない：「一番早い日」）。「十分」は「時」のすぐあとのときだけ直す（「2時十分」は時刻。「十分です」は「足りる」の意味）
_KANJI_DIGIT = {"〇": 0, "零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_KANJI_UNIT = {"十": 10, "百": 100, "千": 1000}
_KANJI_NUMBER = re.compile(r"[〇零一二三四五六七八九十百千万]+(?=[日時分秒月年週人名歳才個枚台件回円階泊杯本部号かヶケ])")


def _kanji_int(s: str) -> int:
    """漢数字の並びを数にする（「十五」15・「二十」20・「三万五千」35000・「二〇二六」2026）。"""
    total = section = num = 0
    for ch in s:
        if ch in _KANJI_DIGIT:
            num = num * 10 + _KANJI_DIGIT[ch]
        elif ch in _KANJI_UNIT:
            section += (num or 1) * _KANJI_UNIT[ch]
            num = 0
        else:  # 万
            total += ((section + num) or 1) * 10000
            section = num = 0
    return total + section + num


def _kanji_to_digits(v: str) -> str:
    def repl(m: re.Match) -> str:
        if m.group() == "十" and v[m.end()] == "分" and (m.start() == 0 or v[m.start() - 1] != "時"):
            return m.group()
        return str(_kanji_int(m.group()))
    return _KANJI_NUMBER.sub(repl, v)


def normalize_value(qtype: str, value: str | None, unit: str = "") -> str | None:
    """値の形をそろえる。unit は数の質問の単位（Question.unit。取込で決めたもの）で、数の型の数字だけの値に補う（units.supply_unit）。
    ✅ 2026-10-16 ユーザー：「入れる方向でOK」（G-37：相手の「3人です」を AI が「3 です」と聞き、言い直しが「3で大丈夫でしょうか？」に
    なった）。補った値が、言い直しと依頼主へ渡す値の両方になる。⚠️ 金額の型には補わない（数字だけの「50」は、相手の「50万」の「万」が
    聞き落とされた形でもありうる＝「50円」と言い直すと桁を誤る）。"""
    if value is None:
        return None
    v = value.strip()
    if qtype in CONFIRM_TYPES:
        v = _TAIL.sub("", v).strip()
        if qtype in DATE_TIME_TYPES | NUMBER_TYPES:
            v = _REQUEST_TAIL.sub("", v).strip()
    if qtype in NO_SPACE_TYPES:
        v = _SPACE.sub("", v)
        if qtype == "number":
            v = supply_unit(v, unit)
        v = _kanji_to_digits(v)
    return v or None
GOT = ("confirmed", "accepted")  # 「取れた」（4-4）
TAKEN = GOT + ("dont_know", "refused")  # AIの報告を受け取った状態（確かめが遅れて届いたら当てる。Flow._late）

# 値の形の確かめ（型の一覧の決まり。DESIGN.md 6-2。✅ 2026-10-14 ユーザー承認）：日付・時刻・日時の型の値に、日付・時刻の言葉が無く、
# 別の型の値（数「3人」・返事の言葉「はい」・こちらの名前「答えなかった」）になっているときだけ止める。
# ⛔ G-34：AIが答えを報告せずに言い直したあと、次の質問の答え「3人」が日時の答えとして確かめまで済んだ。
# ✅ 2026-10-14 ユーザー：「『いつでも』『なるべく早く』は『即』ということなので、営業としては良いこと。日時よりも良い。こちら次第と
#    いうこと。『-』はありえない」⇒ 日時を決めない答えは、日時の質問の答えとして受け付ける（止めるのは、別の型の値だけ）。
# これまでの試験の値に当てて、取り違え 25回中25回を止め、正しい答え 672回中0回を止める（form_rule_eval.py。2026-10-14 の当て直し）。
# 数・金額の型は、数字か数の言葉が1つも無い値だけ止める（form_problem。✅ 2026-10-14 ユーザー承認）。名前などの型には当てない
DATE_TIME_TYPES = {"date", "time", "datetime"}
NUMBER_TYPES = {"number", "amount"}
DATE_TIME_WORDS = ("日", "時", "分", "月", "曜", "午前", "午後", "今日", "明日", "あした", "あす", "明後日", "あさって", "来週", "今週",
                   "再来週", "週", "朝", "昼", "夕方", "夕刻", "晩", "夜", "半", "正午", "年", "明け", "頃", "ごろ", "以降", "旬", "末", "初め", "中")


_NUMERAL = re.compile(r"[0-9０-９〇一二三四五六七八九十百千万]")
# 受け入れにも断りにも取れる言葉（DESIGN.md 6-10 の (3)。システム共通の一覧はここだけ＝曖昧な答えの見分け・返事の言葉だけの値・取込の
# 「曖昧な言葉の意味」の全部が、この並びを使う）
AMBIGUOUS_WORDS = ("大丈夫", "結構", "いい", "平気")
# ⛔ 2026-10-18 に外したもの：取込で意味を決める言葉の「はい」「いいえ」（REPLY_WORDS）。頭の言葉の確かめ（reply_lead）のためだけに足したもので、
#    その確かめを外したので使う所が無くなった（はい・いいえの意味は、意味の判定が選択肢の意味の言葉で読む）
_REPLY_ONLY = re.compile(rf"(?:はい|ええ|うん|いいえ|いえ|いや|そう|です|違います|ちがいます|{'|'.join(AMBIGUOUS_WORDS)}|ございます|わかりません|"
                         r"わからない|分かりません|分からない|[、。，．！？!?\s])+")


# 数の言葉（かな）。⚠️ 短すぎる言葉（「ひと」「やっ」「いつ」「とお」）は入れない：「ひとまず」「やっぱり」「いつでも」「そのとおり」に当たる
_NUMBER_WORDS = re.compile(r"ひとり|ひとつ|ふたり|ふたつ|みっつ|よっつ|よにん|いつつ|むっつ|ななつ|やっつ|ここのつ|ゼロ|"
                           r"(?:いち|に|さん|ご|ろく|しち|なな|はち|きゅう|く|じゅう)(?:にん|めい)|数名|数人|何名|何人")


def form_problem(qtype: str, value: str | None) -> str | None:
    """値の形が型に合わない理由（合っていれば None）。値の形をそろえた（normalize_value）あとの値に当てる。
    答えが質問に合うかは、ここ1か所で決める（✅ 2026-10-14 ユーザー承認。AIには聞いたとおりの値で報告させる）。"""
    if not value:
        return None
    if qtype in NUMBER_TYPES:
        # ✅ 2026-10-14 ユーザー承認：数の型の値には、数字か数の言葉が1つは入っている。⛔ G-35：人数の質問に「いつでも大丈夫」が答えとして
        #    入り、確かめの段まで進んだ。これまでの人数の値 460回：正しい答え（「3人」「3名様」「1人」など）は1回も止めず、数になっていない値
        #    （「はい」「いつでも大丈夫です」「本人です。はい、そうです。」など）は全部止める（form_rule_eval.py）。
        #    ⚠️ 人数の質問に入った日時（「15日の午後2時」）は止められない（数の型は単位を持たない。何日間かを聞く質問なら正しい答え）
        if _NUMERAL.search(value) or _NUMBER_WORDS.search(value):
            return None
        return "数がありません"
    if qtype in CONFIRM_TYPES - DATE_TIME_TYPES:
        # 名前・電話番号・メール・住所：返事の言葉だけの値は、答えの値ではない。⛔ 2026-10-17 の見直しで見つけた漏れ：名前を聞いて
        #    「結構です」「はい」とだけ言われると、「結構ということでよろしいでしょうか？」と言い直していた（日時・数は止めていた）
        if _REPLY_ONLY.fullmatch(value):
            return "答えの値ではなく、返事の言葉です"
        return "相手の答えではなく、こちらの名前です" if value in _LABELS else None
    if qtype not in DATE_TIME_TYPES or any(w in value for w in DATE_TIME_WORDS):
        return None
    if _NUMERAL.search(value):
        return "日付も時刻も無く、数だけがあります"
    if _REPLY_ONLY.fullmatch(value):
        return "日時ではなく、返事の言葉です"
    if value in _LABELS:
        return "相手の答えではなく、こちらの名前です"
    return None  # 「いつでも」「なるべく早く」「そちらのご都合で」など：日時を決めない答え（こちら次第＝営業にはよい答え）


# 値の中の数の書き違い（✅ 2026-10-19 ユーザー：「『ご時』なんて、漢字の組み合わせの日本語ないでしょ！『ご時』と表示されてる時点でおかしいと
# 判断しないと」）。⛔ 2026-10-19 G-62：AIは声では「夕方の5時頃」と言ったのに、報告の値は「夕方のご時頃」で、確認済みになった（会話AIは声で
# 考えるので、報告の文字に、同じ音の別の書き方を書くことがある）。値の形の確かめ（form_problem）は「時」「夕方」があるので通していた。
# ⇒ 数かどうかは、単語分けの辞書が決める（jtext.words の数詞。「夕方のご時頃」の「ご」を、辞書は「五」の数と読む）。もとの字が漢数字かゼロの
#    数（決まった数）が、かなで書かれていたら、書き間違い＝AIに差し戻して書き直させる（お客様は正しく答えているので、聞き返さない）。
#    「何時でも」の何・「いくつか」の幾は決まった数ではないので当たらない。「ご人数」「お時間」の「ご」「お」は、辞書が頭に付く言葉と読む。
#    これまでの試験の報告の値 20,777個（210種類）に当てて、当たったのは「夕方のご時頃」だけ（PHASE0 G-62）
_KANA = re.compile(r"[ぁ-ゖァ-ヺー]")
_DIGIT_LEMMA = re.compile(r"[〇零一二三四五六七八九十百千万億兆]+|ゼロ")


def kana_number(qtype: str, value: str | None) -> str | None:
    """日時・数・金額の型の値の中で、決まった数がかなで書かれている語（「夕方のご時頃」の「ご」）。無ければ None。"""
    if not value or qtype not in NO_SPACE_TYPES:
        return None
    from jtext import words
    for w in words(value):
        f = w.feature
        if getattr(f, "pos2", "") == "数詞" and _KANA.search(w.surface) and _DIGIT_LEMMA.fullmatch(str(getattr(f, "lemma", "")).split("-")[0]):
            return w.surface
    return None


def kana_number_note(value: str, bad: str) -> str:
    """数の書き違いの差し戻しの言葉（記録と訂正の両方で使う）。"""
    return f"値「{value}」の「{bad}」は、数がかなで書かれていて、日本語の言葉になっていません。数は数字で書いて（例：「5時」）、もう一度報告してください。"


# 午前・午後（✅ 2026-10-15 ユーザー：AIからプログラムへ移す。「例えば、1時と言われた場合に、『午前と午後、どちらになりますでしょうか？』は、
# 深夜営業でもしていない限りおかしい。依頼者ごとに『営業時間』を入れる形にして、その時間外の時間だと、午前と午後の確認はしない。会社の場合は
# 9:00〜19:00 がデフォルト、店舗や深夜営業店などは営業時間を入れる形式」）。⛔ G-37：AIが「32 です」を時刻と思い込み、報告せずに自分で
# 午前・午後を聞き、相手の言っていない「3時の午後」を報告した。
# ⇒ 時刻（1〜12時）に午前・午後を表す言葉が無いとき：午前と午後のどちらか一方だけが営業時間に入れば、その側を値に補う（言い直しで相手が
#    確かめられる）。両方とも入る・どちらも入らないときだけ、プログラムが1回だけ聞く（AMPM_ASK）。答え（「午後です」）は、前の値に入れる
AMPM_TYPES = {"time", "datetime"}
DEFAULT_BUSINESS_HOURS = (9, 19)  # 会社の初めの値（9:00〜19:00）。依頼主ごとの設定（店舗・深夜営業は (18, 3) のように日をまたいでよい）
AMPM_WORDS = re.compile(r"午前|午後|朝|昼|夕方|夕刻|夜|晩|深夜|夜中|明け方|正午")
_HOUR = re.compile(r"(?<!\d)(\d{1,2})時")


def _in_hours(hour: int, hours: tuple[int, int]) -> bool:
    open_, close = hours
    return open_ <= hour <= close if open_ <= close else (hour >= open_ or hour <= close)


def fill_ampm(value: str | None, hours: tuple[int, int] = DEFAULT_BUSINESS_HOURS) -> tuple[str | None, bool]:
    """時刻の値に午前・午後が無いとき、営業時間から補う。返り値＝(値, 聞く必要があるか)。"""
    if not value or AMPM_WORDS.search(value) or not (m := _HOUR.search(value)):
        return value, False
    h = int(m.group(1))
    if h == 0 or h > 12:
        return value, False
    am, pm = (0, 12) if h == 12 else (h, h + 12)
    am_in, pm_in = _in_hours(am, hours), _in_hours(pm, hours)
    if am_in == pm_in:
        return value, True
    if h == 12 and pm_in:
        return value, False  # 「12時」は昼の12時（「午後12時」とは言わない）
    return value[:m.start()] + ("午前" if am_in else "午後") + value[m.start():], False


def merge_ampm(pending: str | None, answer: str | None) -> str | None:
    """午前・午後を聞いたあとの答え（「午後です」⇒「午後」）を、前の値（「15日の10時」）に入れる。答えに時刻があれば、答えのほうを使う。"""
    if not pending or not answer or _HOUR.search(answer) or not (w := AMPM_WORDS.search(answer)):
        return answer
    m = _HOUR.search(pending)
    return pending[:m.start()] + w.group() + pending[m.start():] if m else answer


def question_play(questions: list["Question"], qid: str) -> dict:
    """その質問を録音で流すときの1つ（電話接続側が流すもの）。業務の質問＝依頼主の単位の録音（つなぎの言葉＋質問の文）、かけ直しで足す質問＝
    契約者の単位の録音（phrases.RECORDED の callback_〜。どの業務でも同じ文）。読みの確かめは録音にしない（相手ごとの名前を含む＝AIが言う）。"""
    q = next(x for x in questions if x.id == qid)
    if q.callback:
        return {"kind": "recorded", "id": f"callback_{q.callback}", "text": q.text}
    text, lead = spoken_question(questions, qid)
    return {"kind": "question", "id": q.id, "text": text, "lead": lead}


def spoken_question(questions: list["Question"], qid: str) -> tuple[str, str]:
    """録音で流す質問の文（つなぎの言葉＋質問の文）と、そのつなぎの言葉。つなぎの言葉は質問の順番で決める（2問目「それでは」・3問目
    「続きまして」・4問目「では」…）。最初の質問は名乗りと一緒に流すので、つなぎの言葉は付けない。
    ✅ 2026-10-15 ユーザー（聞き比べ）：つなぎの言葉と質問を1本で録音したほうが「しいて言えば自然」⇒ 1本の録音にする。順番で決めておけば、
    依頼主ごとの録音は質問1つにつき1本のまま（流れの制御・録音を作る道具の両方が、ここを使う）"""
    q = next(x for x in questions if x.id == qid)
    if q.reading_of or q.callback:  # プログラムが足す質問（読みの確かめ・かけ直し）：つなぎの言葉は付けない
        return q.text, ""
    # ⛔ 見直しで見つけた漏れ：読みの確かめを挟むと、そのあとの質問のつなぎの言葉（＝録音の名前）が1つずれた（「それでは」⇒「続きまして」）
    i = [x.id for x in questions if not x.reading_of and not x.callback].index(qid)
    if i == 0:
        return q.text, ""
    lead = LEAD_IN["次の質問"][(i - 1) % len(LEAD_IN["次の質問"])]
    return lead + q.text, lead


def other_value(qtype: str, pending: str | None, given: str | None) -> str | None:
    """「合っている」の報告に添えられた値が、待っている値と違う意味の値なら、その値（同じ・意味の無い値なら None）。
    返事の言葉だけ（「はい」「はいそう」）・形の合わない値（日時に「3人」）は、意味の無い値として無視する。数の型は、数字が同じなら同じ値
    （「3人」と「3名」）。これまでの試験の「合っている」588回のうち、違う意味の値は2回だけ（どちらも、AIが訂正を報告しなかった回）。"""
    if not given or _REPLY_ONLY.fullmatch(given) or form_problem(qtype, given):
        return None
    if qtype in NUMBER_TYPES:
        a, b = re.sub(r"\D", "", pending or ""), re.sub(r"\D", "", given)
        return given if b and a != b else None
    return given if given != pending else None

# ── AIに見せる言葉（唯一の場所）。⛔ 2026-10-13 ユーザー指摘「こちらが英語を使っていませんか？」：指示文・操作の宣言・操作への返事に、
#    英語の名前（condition_id・unknown・callee_hurry・confirm_required など）が入っていた。AIが英語で自分の動きを声に出した回があった。
#    ⇒ AIに見せる名前は、すべて日本語にする。中の名前（英字）との置き換えは、AIの入口（Flow.handle）と操作の宣言（tool_declarations）
#    で、この表だけを使って行う。⚠️ 操作の名前（record_answer など）だけは、道具の決まりで英字のまま。
#    依頼主が決める選択肢（「はい」「いいえ」など）は、依頼主が書いたまま（日本語）で、置き換えない。
#    ⛔ 2026-10-13 G-26・G-28：項目名「相手の返事」を、AIが「与えの返事」「合い手の返事」と書き違えた（日本語の形の confirm_field 60回中4回。
#    英字の「result」は 82回中0回。「答えの種類」は 89回中0回）。⇒ 「答えの種類」と同じ形の「返事の種類」にした。
#    書き違えは名前を変えても0にはならない前提で、宣言に無い項目名・選択肢は、すべて差し戻す（Flow.apply の _check_args）。
ARG_LABEL = {"question_id": "質問", "condition_id": "答えの種類", "value": "値", "result": "返事の種類", "correction": "訂正された値",
             "reason": "理由", "part": "流し直す部分", "party": "相手", "why": "かけ直しの理由"}
SYSTEM_LABEL = {"unknown": "わからない", "refused": "答えたくない"}
ANY_LABEL = "どちらでも"
# 意味の2つ目の判定（meaning_judge。DESIGN.md 6-10 の (6)）が、どの選択肢とも決められないと答えたときの名前（受け入れにも断りにも取れる・言い切っていない・答えになっていない）
UNDECIDED = "決められない"
# 意味の判定が、答えではないと決める言葉（どの場面でも）：相手が取り次ぐので待つように言った／代わった別の人が話し始めた（DESIGN.md 6-8）
HOLD = "取り次ぎを待つ"
HANDOVER = "代わった人が話し始めた"
# 確かめの名前（Flow.on_check）：別の耳の文字（聞き取り。6-10 の (5)）・意味の判定（6-10 の (6)。judge2＝後ろで走らせる2つ目の判定）
CHECKS = ("ear", "judge", "judge2", "asks")  # asks＝呼んでいる段の、こちらに尋ねているかの問い合わせ（Flow.asks_context）
# 意味の判定の受け持ち（会社を分けて同時に出す2つ。早く届いたほうで決め、遅れて届いたほうも同じ決まりで当てる。meaning_judge.JudgePanel）
JUDGMENTS = ("judge", "judge2")
HOLD_READINGS = JUDGMENTS + ("asks",)  # 「取り次ぎを待つ」を数える読み（Flow._hold_now・取り次ぎの区切り。尋ねているかの問い合わせの「取り次ぎを待つ」も）
CONFIRM_LABEL = {"affirmed": "合っている", "corrected": "違う値を言われた", "no_answer": "答えなかった"}
# 値を直されたあとの言い直し（✅ 2026-10-14 ユーザー）：(場面＝phrases.CONFIRM の鍵, 頭の言葉＝phrases.LEAD_IN の鍵, 流す相づち)
# - こちらの聞き違い（相手に「違う」と言われた）：「失礼いたしました。改めまして、16日の午後2時で、大丈夫でしょうか？」
# - 相手の言い間違い（相手が自分で言い直した・答えを変えた）：「承知しました。それでは、16日の午後2時で、よろしいでしょうか？」
# - AIが訂正を報告せずに自分で言い直し、相手が「はい」と言った（G-36）：「はい、念のため、16日の午後2時と、おっしゃいましたでしょうか。」
AFTER = {"misheard": ("聞き違いのあと", "お詫びのあとの言い直し", "shitsurei"),
         # 同じ項目を2度聞き違えた（名前の読みの3つ目の確かめ）：「申し訳ございません、」（重いほう）＋「改めまして…」
         "misheard_again": ("聞き違いのあと", "お詫びのあとの言い直し", "moushiwake_heavy"),
         "restated": ("言い直しのあと", "言い直しを受けて", "shouchi"),
         "unreported": ("聞き取りにくかったあと", "念のための言い直し", "hai"),
         # 受け入れにも断りにも取れる答え（「大丈夫です」）の意味を、AIが受け取ったほうで確かめる：
         # 「はい、」＋「念のためご予約はお済みということでよろしいでしょうか？」（DESIGN.md 6-10 の (3)）
         "ambiguous": ("曖昧な答えのあと", "念のための言い直し", "hai"),
         # 言い切っていない最初の答え（「たぶん予約してます」）：「はい、」＋「念のためご予約はお済みということでよろしかったでしょうか？」
         # （✅ 2026-10-18 ユーザー：「この場合、こちらの方が自然」。その前の案「…というご回答で…」は採らなかった）
         "hedged": ("言い切っていない答えのあと", "念のための言い直し", "hai")}
HEDGE_WHY = "言い切っていない答え"  # 確かめる理由の名前（言い直しの言い方を、曖昧な言葉の確かめと分ける。Flow._check_again）
# 値を直されたとき、こちらの聞き違いか相手の言い間違いかは、プログラムが相手の言葉（会話AIの聞き取りの文字）から決める。
# ✅ 2026-10-14 ユーザー決定（3つの案のうち「(a) プログラムが相手の言葉で見分ける」）。⛔ G-36 の2回目・3回目：AIに選ばせると、
#    12本中12本「相手が言い直した」を選んだ（相手が「いや、16日って言ったんですけど。」と言っても）。
# 相手が自分の言い間違い・答えの変更を言う言葉があれば相手の言い間違い、無ければ（文字がまだ届いていないときも）こちらの聞き違い＝お詫び。
# G-34〜G-36 の 30回に当てて、決めたとおりに分かれる（「あ、すみません、間違えました。」6回／「いや、16日って言ったんですけど。」6回・
# 「いえ、16日の午後2時です。」18回）
SELF_CORRECTION = re.compile(r"間違|まちが|やっぱ|変更|変え")


# 名前の読みの確かめへの返事が「違う」で始まるか（「いや、中島です。」「いえ、ナカシマです」「違います」）。「いえいえ」は打ち消しではない
# （「いえいえ、それで大丈夫です」）。⛔ G-45：相手の「いや、中島です。」を、AIが「合っている」と報告した（AIには同じ名前に聞こえる）
# ⛔ 見直しで直した：前の「違」「ちが」は「違いません」「違いないです」（＝合っている）にも当たった
DENIAL = re.compile(r"^(?:いや|いえ(?!いえ)|(?:違|ちが)(?:います|う|って|いますけど))")
# 認める言葉で始まる返事（「はい」「ええ」「そうです」「大丈夫です」「合ってます」「それで」）。名前の読みの確かめで、相手が認める言葉を
# 言わずに名前だけを言い直したとき（「ナカシマです。」）は、直されたとして扱う（相手は認めるときは「はい」と言う）
AFFIRM = re.compile(r"^(?:はい|ええ|うん|そう|大丈夫|合って|あって|それで|結構|いえいえ)")


def correction_kind(callee_said: str) -> str:
    """値を直されたときの直され方（AFTER の鍵）。"""
    return "restated" if SELF_CORRECTION.search(callee_said or "") else "misheard"


# 受け入れにも断りにも取れる答え（DESIGN.md 6-10 の (3)。システム共通の一覧はここだけ）。相手の言葉が、これ「だけ」のとき
# （頭のつなぎの声「あ」「えーと」と、語尾の「よ」を除く）。「はい、大丈夫です」「いえ、結構です」のように、認める・打ち消す言葉が
# 付いていれば当たらない（その言葉で決まる）。⛔ G-14（6つの声の 24本）：AIは「大丈夫です」を6本とも「はい」、「いいです」を「はい」5本・
# 「いいえ」1本、「結構です」を「いいえ」2本と決め、4本は通話を断られたとして終えた。
# ⚠️ 選択肢の質問の最初の答えにだけ当てる。言い直しへの返事（「…でよろしいでしょうか？」に「大丈夫です」）は、認める返事（AFFIRM）
_FILLER = r"(?:あ+|あの+|えー*と?|え+|うーん|ん+|まあ)[、。\s]*"
AMBIGUOUS = re.compile(rf"(?:{_FILLER})*(?:(?:{'|'.join(AMBIGUOUS_WORDS)})(?:です|っす)?よ?[、。！!\s]*)+")
# 言い切っていない答え（「家族がしたかもしれないです」「たぶんカードです」）も、AIが選んだ選択肢のまま渡さず、同じように意味を確かめる。
# ⛔ G-14（いまの形・v2）：「家族がしたかもしれないです」を、AIは 12本中11本「はい」と決めた（正しくは「わからない」か、確かめる）。
# ⚠️「と思います」は入れない（「カードがいいと思います」のように、言い切りの柔らかい言い方にも使う）
UNCERTAIN = re.compile(r"かもしれ|かも[。、！!\s]*$|かもね|たぶん|多分|おそらく|恐らく")


def ambiguous_word(text: str | None) -> str | None:
    """相手の言葉が、受け入れにも断りにも取れる言葉だけなら、その言葉（AMBIGUOUS_WORDS の1つ。2種類以上を言ったら None）。
    ✅ 2026-10-17 ユーザー：「『結構です。』だけでは判断できない。人でも前の質問や会話から判断する言葉です」⇒ 意味は、前の質問で決まれば
    その意味（Question.reply_meanings。取込で決める）、決まらなければ確かめる。"""
    t = (text or "").strip()
    if not AMBIGUOUS.fullmatch(t):
        return None
    said = {w for w in AMBIGUOUS_WORDS if w in t}
    return said.pop() if len(said) == 1 else None



# 頭の返事の言葉の言い方の違い（伸ばし方を含む：「ええ」と「えー」・「いや」と「いやあ」。2026-10-17：AmiVoice はこの書き方で返す）
_LEAD_READING = ((re.compile(r"^(?:いいえ|いえ|いや)[ーあぁ]*"), "いいえ"), (re.compile(r"^(?:はい|え[えー])[ーえぇ]*"), "はい"))
# 答えの前の言いよどみ（読みにそろえたあと。長いものから先に当てる）。「えー」だけは返事の言葉（「ええ」）にもなるので入れない
# 「あ」「あー」「あっ」は、返事の言葉の前のときだけ（「ありがとう」の「あ」は言いよどみではない）
_HEAD_FILLER = re.compile(r"^(?:(?:えーっと|えっと|えーと|ええと|あのー|あのう|あの|そのー|その|うーん|んー|まあ|(?:あー|あっ|あ)(?=はい|いいえ|いえ|いや|ええ|えー))"
                          r"[、。,.\s]*)+")


def heard_alike(a: str, b: str) -> bool:
    """2つの耳の文字が同じことを言っているか（G-15）。読み（ひらがな）にそろえ、句読点・書き方の違い（「家まだ」と「いえ、まだ」）と、頭の返事の
    言葉の言い方の違い（「いえ」「いいえ」「いや」・「はい」「ええ」）と、答えの前の言いよどみ（「あ」「えっと」「あの」など）は見ない。
    それ以外の違い（「捨てないです」と「してないです」）は食い違い。
    【2026-10-17】言いよどみ：AmiVoice は答えの前の息の音を「あ」と書く（「あはい。予約しています。」）。記録の通話に当てると、見ない形で
    AmiVoice の要らない確かめは 104 → 66、拾える誤りは同じ（ほかの耳は変わらない）。
    G-14 の報告 600回ほどに当てると、AIが正しく選んだ回で食い違ったのは 563回中34回（6%）・AIが誤った回は2回とも食い違った。
    ⛔ 通話の中で測ると（G-15 の続き。Gemini の書き起こしのモデルを別の耳にした）、食い違いで確かめた14回のうち13回は正しい答えへの要らない確かめ
    （152本の8.5%）で、拾えた誤りは0（残った誤り「してないです」は、2つの耳が両方とも「捨てないです」と聞いた）⇒ 既定では使わない
    （convo.run_call の ear_check は既定で切る。別の耳の文字が届かなければ比べない＝この決まりは働かない）。会話AIと独立した別の耳が
    見つかったら、耳を入れ替えて測り直す。"""
    from jtext import compare_reading

    def norm(s: str) -> str:
        t = _HEAD_FILLER.sub("", compare_reading(s))
        for pat, w in _LEAD_READING:
            t = pat.sub(w, t)
        return t
    return norm(a) == norm(b)


# 選択肢の名前が返事の言葉だけのもの（「はい」「いいえ」）。この名前のままでは言い直せない（「はいでよろしいでしょうか？」）
_BARE_REPLY = re.compile(r"はい|いいえ|いえ|ええ|うん|いや")


def choices_note(q: "Question") -> str:
    """選択肢の意味（取込で決めた意味の言葉。Question.meanings）を、会話のAIへ知らせる一文（無ければ空）。依頼主へ渡る選択肢がどの意味かを、
    会話のAI・意味の2つ目の判定（meaning_judge）・言い直し（choice_words）で同じにする（この1か所から、質問を知らせる所すべてに添える）。
    ⛔ 2026-10-17 に見つけた穴：会話のAIには選択肢の名前しか見せていなかった＝打ち消しの問い（「ご不明な点はございませんでしょうか？」）の
       「はい」の向きを、AIが自分で決めていた（意味の言葉は「はい」＝ご不明な点はないということ。G-53）。"""
    if q.type not in CHOICE_TYPES or not q.meanings:
        return ""
    return f"{q.id} の選択肢の意味：" + "／".join(f"「{c}」＝{q.meanings[c]}" for c in q.conditions if q.meanings.get(c)) + "。"


def choice_words(q: "Question", cond: str | None) -> str | None:
    """選択肢の答えを言い直すときの言葉（「（言葉）でよろしいでしょうか？」の（言葉）。phrases.confirm の meaning）。取込で決めた意味の言葉
    （Question.meanings。「ご予約はお済みということ」）、無ければ選択肢の名前（「カード」）。名前が返事の言葉だけなら、言い直せないので None。
    ⛔ 2026-10-17 に見つけた穴：選択肢の型を聞き返したあとの言い直しが「はいとおっしゃいましたでしょうか？」になっていた"""
    if cond == ANY:  # 任された答え：「念のためどちらでも構わないということでよろしいでしょうか？」
        return "どちらでも構わない"
    w = q.meanings.get(cond) or cond or ""
    return None if not w or _BARE_REPLY.fullmatch(w) else w
# 終わりを求める理由（request_end）。「通話を断られた」＝発信の拒否（営業お断り・もうかけないで）＝発信禁止の申告（Intent.md 原則14。
# DESIGN.md 6-2 の断りの3つの上の段）。「相手が怒っている」＝怒り・苦情＝弁明や約束をせずに終え、人へ知らせる印（9-4・7-1 の順7）。
# ⛔ 2026-10-18 の見直しで見つけた漏れ：前は「通話を断られた」で締めるだけで、発信禁止の申告をどこにも残していなかった（原則14 の
#    「その場で発信禁止に登録」に届かない）。怒り・苦情を報告する手立ても無かった
END_LABEL = {"done": "質問が終わった", "callee_hurry": "相手が急いでいる", "callee_refused_call": "通話を断られた",
             "callee_complaint": "相手が怒っている", "not_transferred": "取り次いでいただけなかった"}
# 目的の人に繋がらずに終わった理由（会話の報告の not_reached。Flow.not_reached の1か所で決める）。✅ 2026-10-22 ユーザー決定（CL-2 の 4-130）：
# 「『営業拒否』：もうかけない。『事情があり、取り次げなかった』：日を改めて1回。『約束した時刻のかけ直し』：約束した時間と同じ」
# ⇒ 営業のお断り＝通話を断られた（発信の拒否。ここには入れない）／お呼びした方が出られない（戻る時刻を聞いた＝かけ直しの申し出）＝absent／
#    それ以外の事情で取り次いでいただけなかった＝not_transferred（時刻が取れたかは通話の事実 callback が持つ＝約束の時刻は CL-2 が決める）
NOT_REACHED_LABEL = {"absent": "不在", "not_transferred": "取り次いでいただけなかった"}
# 断られたときの締め（CLOSE_DECLINED）で終える理由。念のための確認はしない（DESIGN.md 6-5）
DECLINED_ENDS = ("callee_hurry", "callee_refused_call", "callee_complaint")
REPLY_KEY_LABEL = {"result": "結果", "message": "次にすること", "question_id": "質問", "question": "質問の文", "value": "値", "say": "言い方"}
RESULT_LABEL = {"next": "次の質問", "confirm_required": "言い直して確かめる", "rejected": "差し戻し", "ask_back": "聞き返す",
                "recheck": "念のための確認", "end": "締め", "busy_ask": "引き止めの一言", "replayed": "録音で流し直した",
                "hold": "取り次ぎを待つ", "noted": "受け付けた", "confirm_target": "目的の人か確かめる", "call_target": "目的の人をお呼びし直す", "ai_question": "エーアイか聞かれた",
                "restate": "言い直す", "wait": "最後まで聞く"}
# 名乗りの流し直し（DESIGN.md 6-13。✅ 2026-10-13 ユーザー：名乗り直しも録音で流す「間が大丈夫ならOK」）：AIが選ぶ部分の名前
# 名乗り＝「どちら様ですか」／用件＝「何のご用件ですか」／最初から＝聞き取れなかった様子（「もう一度」「はい？」）／代わった方へ＝別の人に代わった
# エーアイか聞かれた＝「AIですか？」「人間ですか？」への決まった言い方（instruction.Business の ai_answer。Flow._ai_question）。
# 質問に戻る＝その言い方に、相手が問題は無いと答えた（いまの位置へ戻る。Flow._resume）。
# ⛔ 2026-10-19 G-57：AIに自分で答えさせると、3本とも答えたあと黙り、いまの質問に戻らなかった ⇒ 決まった言い方はプログラムが渡し、戻るのもプログラム
# 答えられない質問＝相手の質問に、こちらが答えを持っていない（⛔ 2026-10-22 G-57 tc19：「分からない質問」という名前では、AIは番号の出どころ・
# キャンセル料の問いの6本中4本で「用件」を選んだ＝聞き取れなかった問いとも読める名前だった）（業務設定の想定問答にも、名乗り・用件にも無い）＝決まった一言の録音
# （phrases.UNKNOWN_ANSWER。Flow._answer）。想定問答の答えは、業務設定の問いの名前で選ぶ（ANSWER_PREFIX ＋ 問いの名前）
PART_LABEL = {"intro": "名乗り", "purpose": "用件", "all": "最初から", "handover": "代わった方へ", "ai": "エーアイか聞かれた",
              "resume": "質問に戻る", "unknown": "答えられない質問"}
# 想定問答の問いの名前に使えない名前（AIが流し直しで選ぶ部分の、中の名前と日本語の名前。AIの報告は日本語の名前を中の名前に読み替えて
# から問いの名前を探すので、同じ名前の問いには届かない＝Flow.__init__ が断る。CL-2 の業務設定の確かめ ANSWER_RESERVED も、この一覧に合わせる）。
# ⛔ 2026-10-22 の見直しで見つけた漏れ：CL-2 は中の名前だけを断っていた＝問いの名前が「用件」だと、用件の録音が流れて答えが流れない
RESERVED_PART_NAMES = frozenset(PART_LABEL) | frozenset(PART_LABEL.values())
ANSWER_PREFIX = "answer_"  # 想定問答の答えの、録音の部分の名前の頭（instruction.Business.replay_parts と同じ。録音のファイル名になるので「:」を使わない＝Windows）
OPENING_ORDER = ("greeting", "intro", "purpose")  # 名乗りの録音の部分の順（instruction.Business.replay_parts の鍵）
# 相手の種別（DESIGN.md 4-3。report_party）：電話に出た相手が何か（AIに見せる名前なので英字を使わない＝FAX は「ファクス」）。受付・目的の人は、目的の人のある業務だけでAIに見せる
# （「誰でも」の業務は、最初の相手が目的の人＝報告しなくてよい）。人ではない相手（留守番電話・自動音声の案内・FAX）には話さずに終え、
# 番号違いは決まった締めで終える（Intent.md 9-4）
PARTY_LABEL = {"reception": "受付の方", "target": "目的の人", "voicemail": "留守番電話", "ivr": "自動音声の案内", "fax": "ファクスの音",
               "wrong_party": "番号違い"}
NOT_A_PERSON = ("voicemail", "ivr", "fax")
PERSON_PARTIES = ("reception", "target")
# かけ直しの申し出（DESIGN.md 6-2 の request_callback。CL-2 の依頼・✅ 2026-10-14 ユーザー「相手が別の番号を言ったら、基本はその番号へかける」）：
# 理由は3つだけ。理由ごとに、システムが足す質問（CALLBACK_ASK の役目の順）。足した質問は C1（時刻）・C2（番号）で、どの業務でも同じ番号
CALLBACK_LABEL = {"absent": "不在", "other_number": "別の番号へ", "later": "あとで"}
CALLBACK_PLAN = {"absent": ("back_time",), "other_number": ("number", "reachable_time"), "later": ("later_time",)}
CALLBACK_IDS = {"time": "C1", "number": "C2"}
# 目的の人を呼んでいる段（calling）の、意味の判定の「質問」（呼ぶ一言）の選択肢と意味の言葉（DESIGN.md 1-1：AIの報告「目的の人」を、
# 独立した判定とそろったときだけ受け取る。取り次ぎ＝HOLD・代わった人＝HANDOVER は、判定のほうの答えでない言葉）
TARGET_IS_THERE, TARGET_IS_AWAY, TARGET_IS_NOT = "ご本人", "不在", "別の方"
# 電話に出た方が、こちらに尋ねているか（呼んでいる段だけ、目的の人の判定とは別の問い合わせ＝確かめの名前 "asks"。電話接続側が同時に出す）。
# ✅ 2026-10-22 ユーザー：「相手が喋っていても、それが質問だった場合の対処は？」。⛔ G-57 tc14：「岡田ですね。（間）失礼ですが、どちら様でしょうか？」を、
#    AIは「目的の人」と報告し、判定は「ご本人」「取り次ぎを待つ」と分かれ、質問に答える前に「〜様でいらっしゃいますでしょうか？」と確かめた（3本とも）。
# ⛔ 目的の人の判定の選択肢に「こちらに聞いている」を足す形は取り下げ（G-59 10-22）：質問は全部読めたが、ほかの言葉の読み方まで変わり、
#    取り次ぎのあとの「お待たせいたしました。」を 3.5 Flash Lite が10回中3回・Sonnet が10回中1回「ご本人」と読んだ（足す前は0回）＝別の方に繋ぐ向き
ASKS_YES, ASKS_NO = "尋ねている", "尋ねていない"
CONFIRM_PARTS = ("target_confirm", "target_reconfirm")  # 目的の人かを確かめる一言（名乗りの部分）：確かめ・はっきりしない返事への聞き返し
ASKS_MEANINGS = {ASKS_YES: "電話に出た方が、こちらに尋ねている（どちら様か・ご用件か・どちらの〇〇か など、こちらが答えることを聞いた）",
                 ASKS_NO: "こちらに何も尋ねていない（名乗った・取り次ぐ・不在を伝えた・「はい」「いいえ」と答えた・名前をくり返して確かめた など）"}
# ご本人の意味（判定の決まりの1か所。meaning_judge.KIND_DESC の「代わった人」は、ここに当たるときは選ばない）。
# ✅ 2026-10-19 ユーザー：「受付が『それでは岡田にお繋ぎします。』のような後に、『はい、お電話代わりました。』と言われれば、『ご本人』になりますよね」
#    ⛔ 直す前は、判定に、その前の取り次いだ方の言葉を渡しておらず、名乗らない「はい、お電話代わりました。」を「代わった人」と読んでいた（G-62）
# ⚠️ 2026-10-19 ユーザー（2回目）：「取り次いだ後に、『岡田は取り込んでおり、対応できません』ともう一度受付が言うこともあるので、取次後は
#    必ずしも、本人とは限らないので注意、見直しが必要」⇒ ご本人とするのは、取り次いだあとに「電話を代わった方」が出たときだけ。出られないと
#    伝える言葉は不在。取り次いだ方がまた出たのか、代わった方が出たのか分からない言葉は、ご本人と決めない（確かめる）
TARGET_MEANINGS = {TARGET_IS_THERE: "お呼びした方ご本人が電話に出ているということ（電話を代わった方が、お呼びした方の名前を名乗ったときも・"
                                    "その前に取り次いだ方が、お呼びした方に取り次いだ（お名前を確かめて待つように言った・お繋ぎしますと言った）あとに、"
                                    "電話を代わった方が「お電話代わりました」のように出たときも）。取り次いだあとでも、お呼びした方が出られないと伝える言葉"
                                    "（取り込んでいる・席を外しているなど）はこちらではなく、取り次いだ方がまた出たのか、代わった方が出たのか分からない言葉"
                                    "（「お待たせいたしました」だけなど）もこちらではない。お呼びした方がいないので別の者に代わると言ったあとに"
                                    "代わった方が出たとき・お呼びした方と違う名前を名乗ったときも、こちらではない",
                   TARGET_IS_AWAY: "お呼びした方はいらっしゃらない・電話に出られないということ（取り次いだあとに、取り次いだ方がまた出て、そう伝えたときも）"}
# 目的の人かを確かめたあと（確かめる一言＝はい／いいえの問い）の選択肢と意味の言葉（Flow.judge_context が、確かめへの返事を待つ間の質問にする）。
# ⛔ 2026-10-21 G-59：確かめたあとも呼ぶ一言の選択肢（ご本人・不在）だけを見せていたので、「いいえ」に当たる選択肢が無く、「いえ、違います。」を
#    2社とも「不在」と読み（3回中3回）、3.5 Flash Lite は「いいえ、私は山田と申します。」を「ご本人」と読んだ（3回中2〜3回）
CONFIRM_MEANINGS = {TARGET_IS_THERE: "電話に出ている方が、お呼びした方ご本人だということ（「はい」「そうです」「私です」と答えた）",
                    TARGET_IS_NOT: "電話に出ている方は、お呼びした方ではないということ（「いいえ」「違います」と答えた・違うお名前を名乗った）",
                    TARGET_IS_AWAY: TARGET_MEANINGS[TARGET_IS_AWAY]}
# hang_up＝話さずに切る（電話接続側が読む）・silent＝AIに話させない返事（取り次ぎを待つ間など。handle_call）
INTERNAL_KEYS = {"ack", "ask_n", "ask_reason", "ask_last", "after", "play", "speak", "hang_up", "silent", "sorry", "again", "away", "renamed"}  # 中で使うだけの項目（電話接続側・数え方が読む）。AIへの返事には入れない
# こちらが使う名前（AIに見せる日本語の名前）。値の欄に入っていたら、相手の答えではない（form_problem）
_LABELS = (set(SYSTEM_LABEL.values()) | set(CONFIRM_LABEL.values()) | set(END_LABEL.values()) | set(PARTY_LABEL.values())
           | set(CALLBACK_LABEL.values()) | {"答えた"})
_FROM_LABEL = {"condition_id": {**{v: k for k, v in SYSTEM_LABEL.items()}, ANY_LABEL: ANY}, "result": {v: k for k, v in CONFIRM_LABEL.items()},
               "reason": {v: k for k, v in END_LABEL.items()}, "part": {v: k for k, v in PART_LABEL.items()},
               "party": {v: k for k, v in PARTY_LABEL.items()}, "why": {v: k for k, v in CALLBACK_LABEL.items()}}
_ARG_FROM_LABEL = {v: k for k, v in ARG_LABEL.items()}


def unsure_counts(kind: str, value: str) -> str | None:
    """目的の人を呼んでいる段で、確からしさが線より下の読み（value。代わりの Jev）を、どう数えるか（唯一の決まり＝Flow.on_unsure・G-59 の数え方）。
    弱い読みは、繋ぐのを止める側にだけ使う：目的の人の判定で「ご本人」でなければ「決められない」＝繋ぐのを止める印（Flow._doubted。判定の
    読みとは別に持つ＝ほかの判定と食い違いを作らない）／尋ねているかの問い合わせで「尋ねている」（目的の人かを決めずに、聞かれたことに
    答える側）。ほかは数えない（None）。
    ⛔ 2026-10-22 G-59：Jev は質問の言葉の「尋ねている」を、確からしさ 0.79〜0.92 で15回中9回返した（線より下で捨てると、質問に答えずに
       目的の人かを確かめる。質問ではない言葉に、弱い「尋ねている」は0回）"""
    if kind in JUDGMENTS:
        return UNDECIDED if value != TARGET_IS_THERE else None
    if kind == "asks":
        return ASKS_YES if value == ASKS_YES else None
    return None


def label(cond: str | None) -> str | None:
    """選択肢の中の名前 → AIに見せる名前・依頼主へ渡す名前（業務の選択肢はそのまま）。"""
    return ANY_LABEL if cond == ANY else SYSTEM_LABEL.get(cond) or CONFIRM_LABEL.get(cond, cond)
# AIが呼べる操作（6-2）と、その項目（中の名前）：(必ず要る項目, 無くてもよい項目)。操作の宣言（tool_declarations）と、
# 受け取ったときの確かめ（Flow._check_args）は、この表だけを使う（宣言と確かめが食い違わないように）
OP_ARGS = {"record_answer": (("question_id", "condition_id"), ("value",)),
           "report_unclear": (("question_id",), ()),
           "confirm_field": (("question_id", "result"), ("correction",)),
           "request_end": (("reason",), ()),
           "replay_opening": (("part",), ()),
           "report_party": (("party",), ()),
           "request_callback": (("why",), ())}
# ✅ 2026-10-14 ユーザー：考えている相手への「はい」は形 A（AIが自分で言う）。⇒ 比べた形 B（AIは黙って「相手が考えている」と報告し、
#    相づちはプログラムが録音で流す。G-36）の操作は外した（B は、答えを考えている途中と読み違えて黙った回があった）
OPS = tuple(OP_ARGS)
# 選択肢が決まっている項目（中の名前 → その表）。答えの種類（condition_id）は質問ごとに違うので、record_answer の中で確かめる
_ENUM_ARGS = {"result": CONFIRM_LABEL, "reason": END_LABEL, "part": PART_LABEL, "party": PARTY_LABEL, "why": CALLBACK_LABEL}


@dataclass
class Limits:
    """回数の上限（システム共通の設定の値。8章の「厳しいほう」）。DESIGN.md 6-11"""
    # その場の聞き返し（聞き取れない・答えが質問に合わない）：必須の項目は2回まで（最初の質問と、締めの前の念のための確認1回のほかに）。
    # ✅ 2026-10-14 ユーザー：「『聞き返しは1項目1回まで』は大事なポイントは3回までOK」「必須の大事な部分の『分からない』は、極力なくすべき。
    #    安易に『-』にしたらダメ」「いたるところで聞き返すのは相手に不快感を与える」⇒ 必須でない項目は、その場では聞き返さない（これまでどおり）
    # ✅ 2026-10-14 ユーザー（続き）：「必須の項目を聞く回数が、最初の質問のほかに最大3回（その場2回＋締めの前の念のための確認1回）、
    #    依頼者ごとに変更できる設定にしておいた方がいい」⇒ 初めの値は2回。依頼主ごとの設定（CL-2 の設定の表）で変えられる
    ask_back: int = 2
    recheck: int = 1           # 締めの前の念のための確認：1項目1回まで（その項目を聞く最後の1回。言い方は「…もう一度だけ…」）
    after_correction: int = 1  # 訂正のあとの復唱：1回まで
    # 名前の読みの確かめで言う読みの数（まず言ってみる読みを含めて）。候補が3つある名前（「角田」＝つのだ・かくた・かどた）で、AIが相手の
    # 言った読みを聞き分けられない回（G-46：名前だけで「カドタです。」と直されると、AIの報告は使えないことが多い）も、相手に順に確かめて
    # いただけば正しい読みに着く。✅ 2026-10-14 ユーザー「大事なポイントは3回までOK」の範囲（最初の確かめ＋2回）
    reading_tries: int = 3
    replays: int = 2           # 名乗りの流し直し：1通話2回まで（同じ相手に何度も同じ録音を流さない。使い切ったらAIが短く答える）
    # 確かめ（別の耳・意味の判定）を待つ長さ：AIの報告が届いてから、この秒まで（待つ間は、電話接続側が「はい、」の相づちを先に流す）。
    # ✅ 2026-10-18 ユーザー：「『はい、』の相づちがあって2秒程度なら大丈夫」。間に合わなければ、AIの報告のまま受け取って先へ進み、
    # 遅れて届いた確かめを同じ決まりで当てる（食い違えば「-」にして締めの前にもう一度聞く＝Flow.on_check）。最後まで届かなかった確かめは、
    # 通話後の検証（6-12）でやり直す（Flow.unchecked）。止まらない
    hold_s: float = 2.0


@dataclass
class Question:
    id: str
    text: str
    type: str                   # 型の一覧の名前（"yes_no" "choice" "datetime" など）
    required: bool = True
    conditions: tuple = ()      # 業務が宣言した選択肢（依頼主が書いたまま。システムの2つは足さなくてよい）
    topic: str = ""             # 聞くこと（「ご希望の日時」など。聞き返しの言い方に使う。業務設定。無ければ言わずに聞き返す）
    unit: str = ""              # 数の型の単位（「人」など）。取込で質問の文から決める（units。人は確かめない）。空なら補わない
    # 名前の読みの確かめ（reading_question が作る、プログラムが足す質問）：reading_of＝読みが決まらない名前の表記（「山崎」）、
    # prefill＝まず言ってみる読み（ひらがな。names.decide の likely）。この質問は、相手に聞かずに言い直しの形で始まる
    reading_of: str = ""
    prefill: str = ""
    readings: tuple = ()        # 考えられる読み（names.decide の candidates。まず言ってみる読みを含む）。AIが聞き分けられないときに使う
    reading_kind: str = "法人"  # 確かめの言い方（phrases.NAME_READING_ASK の鍵＝発信対象の区分。法人／個人）
    # かけ直しの申し出で、システムが足した質問の役目（CALLBACK_ASK の鍵。callback_question が作る）。答えは依頼主へ渡す項目に入れず、
    # 通話の事実（Flow.facts の callback）に残す。録音は無いので AIが言う（つなぎの言葉も付けない）
    callback: str = ""
    # 選択肢の型の、選択肢ごとの意味の言葉（選択肢 → 「ご予約はお済みということ」）。答えを言い直して確かめるときに使う（choice_words）。
    # 取込で文字のモデルが決める（人は確かめない）。無い選択肢は名前のまま言い直す（返事の言葉だけの名前は言い直せない）
    meanings: dict = field(default_factory=dict)
    # 受け入れにも断りにも取れる言葉（AMBIGUOUS_WORDS）だけの答えが、この質問ではどの選択肢の意味になるか（言葉 → 選択肢。例：
    # 「パンフレットをお送りしてもよろしいでしょうか？」の「結構」→「いいえ」）。取込で文字のモデル2つが質問の文から決める（meanings.decide_replies。
    # G-49）。無い言葉は、通話の中で意味を確かめる。✅ 2026-10-17 ユーザー：「人でも前の質問や会話から判断する言葉です」
    reply_meanings: dict = field(default_factory=dict)

    def __post_init__(self):
        assert self.type in TYPES, f"{self.id} の型「{self.type}」は、型の一覧にありません（I-16）"
        assert not self.callback or self.callback in CALLBACK_ASK, f"{self.id}：かけ直しの質問の役目「{self.callback}」はありません"
        assert not self.reading_of or (self.type == "name" and _KANA.fullmatch(self.prefill or "")), \
            f"{self.id}：名前の読みの確かめは、名前の型で、まず言ってみる読み（ひらがな）が要ります"
        clash = set(self.conditions) & (set(SYSTEM_CONDITIONS) | set(SYSTEM_LABEL.values()) | {ANY, ANY_LABEL})
        assert not clash, f"{self.id} の選択肢が、システムの選択肢と同じ名前です：{clash}"
        assert not self.meanings or (self.type in CHOICE_TYPES and set(self.meanings) <= set(self.conditions)), \
            f"{self.id}：意味の言葉は、選択肢の型の、宣言した選択肢にだけ付けられます"
        assert not self.reply_meanings or (self.type in CHOICE_TYPES and set(self.reply_meanings) <= set(AMBIGUOUS_WORDS)
                                           and set(self.reply_meanings.values()) <= set(self.conditions)), \
            f"{self.id}：曖昧な言葉の意味は、選択肢の型の、宣言した選択肢にだけ決められます"

    @property
    def all_conditions(self) -> tuple:
        return tuple(self.conditions) + ((ANY,) if self.type == "choice" else ()) + SYSTEM_CONDITIONS

    @property
    def labels(self) -> tuple:
        """AIに見せる選択肢の名前（業務の選択肢はそのまま、システムが足すものは日本語の名前）。all_conditions と同じ並び。"""
        return tuple(label(c) for c in self.all_conditions)

    def reply_choice(self, word: str | None) -> str | None:
        """受け入れにも断りにも取れる言葉（AMBIGUOUS_WORDS の1つ）だけの答えが、この質問でどの選択肢の意味になるか（決まらなければ None。
        唯一の場所）。取込で決めたもの（reply_meanings）。"""
        return self.reply_meanings.get(word)

    def condition_of(self, name: str | None) -> str | None:
        """選択肢の名前（AIに見せる名前）から、中の名前（無ければ None）。"""
        return next((c for c in self.all_conditions if label(c) == name), None)

    @property
    def confirm(self) -> bool:
        return self.type in CONFIRM_TYPES


_KANA = re.compile(r"[ぁ-ゖー]+")
# 相手からの質問（文の終わりが「？」か、問いかけの言い終わり）。⚠️「はい？」（聞き返し）も当たるが、次の質問へ進む所（_advance）でしか見ないので、
# 当たっても、AIが自分で次の質問を言う（録音を流さない）だけ
_CALLEE_ASKS = re.compile(r"(?:[？?]|(?:です|ます|でしょう|ません|んです|ない|でした|ました)か[。]?)$")


def next_reading(heard: str | None, readings: tuple | list, tried: list[str]) -> str | None:
    """相手が名前の読みを直したときに、次に確かめる読みを決める唯一の場所（flowctl.reading_question）。
    heard＝AIが聞いたと報告した読み（ひらがな。無ければ None）、readings＝考えられる読み（names.decide の candidates）、tried＝もう言った読み。
    会話のAIは、同じ漢字の名前の読みの濁りの違いを聞き分けられない（G-45：「いや、ナカシマです。」に19回中15回「合っている」・3回「なかじま」／
    G-46：「いや、カドタです。」を「かどだ」と報告）。確かめるのは相手（こちらの声は、ひらがなの読みで正しく届く）なので、
    ここは「次にどの読みで聞くか」だけを決める：
    (1) AIの聞いた読みが、まだ言っていない候補なら、それ
    (2) 候補には無いが、濁りを外すと、まだ言っていない候補と同じなら、その候補（「かどだ」⇒「かどた」。2つ以上なら、濁りの違いが少ないほう）
    (3) 候補のどれとも違う読みなら、AIの聞いた読み（相手が候補に無い読みを言った）。ただし、言った読みと濁りだけ違う読みは、
        まず言ってみる読みのほかに候補があるときは使わない（AIが、こちらの言った読みを濁りだけ違えて聞いた形）。ほかに候補が無いときだけ使う（「やまざき」⇒「やまさき」）
    (4) それ以外（読みが無い・言った読みと同じ）は、まだ言っていない候補の最初
    どれも無ければ None（＝確かめをやめて、名前を使わずにお呼びする）。"""
    untried = [r for r in readings if r not in tried]
    if heard and heard not in tried:
        if heard in untried:
            return heard
        same = [r for r in untried if unvoiced(r) == unvoiced(heard)]
        if same:
            return min(same, key=lambda r: sum(x != y for x, y in zip(r, heard)))
        if len(readings) <= 1 or all(unvoiced(t) != unvoiced(heard) for t in tried):
            return heard
    return untried[0] if untried else None


CALLBACK_TOPIC = {"back_time": "お戻りになるお時間", "later_time": "ご都合のよろしいお時間", "number": "お電話番号",
                  "reachable_time": "お電話に出られるお時間"}


def callback_question(role: str) -> "Question":
    """かけ直しの申し出で、システムが足す質問（役目＝CALLBACK_ASK の鍵）を作る唯一の場所。時刻は日時の型、番号は電話番号の型（どちらも
    言い直して確かめる型）。出られる時間は必須にしない（✅ 2026-10-14 ユーザー：分からないと言われたら、それでよい）。"""
    number = role == "number"
    return Question(CALLBACK_IDS["number" if number else "time"], CALLBACK_ASK[role], "phone" if number else "datetime",
                    required=role != "reachable_time", conditions=("答えた",), topic=CALLBACK_TOPIC[role], callback=role)


def reading_question(qid: str, surface: str, likely: str, readings: tuple | list = (), kind: str = "法人") -> "Question":
    """読みが決まらない相手の名前（「山崎」＝やまざき／やまさき）を、会話の中で確かめる、プログラムが足す質問（DESIGN.md 6-2）。
    ✅ 2026-10-16 ユーザー：「なかじまとなかしまなどは人が見ても判断できません。それは会話の中で失礼のないように語るか、場合によっては
       聞き出す」。⇒ 新しい流れは作らず、ほかの値と同じ「言い直して確かめる」流れ（4-4）にのせる：まず言ってみる読みで
       「お名前の読みの方は「なかじま」様でよろしかったでしょうか？」（個人なら「お名前の方は…」。kind＝発信対象の区分）と聞き、
       相手が認めれば確認済み、「いや、なかしまです」なら、録音「失礼いたしました。」＋「改めましてお名前の読みの方は「なかしま」様で
       よろしかったでしょうか？」で確かめる。
       確かめられなければ、名前を使わずに「ご担当者様」のようにお呼びする（失礼のないように語る）。
    必須にしない（聞き返して相手を煩わせない。確かめられなければ名前を使わないだけ）。
    readings＝考えられる読み。⛔ G-45：相手が「いや、ナカシマです。」と直したのに、AIは「中島」と聞いて、訂正の読みを直す前と同じ
    「なかじま」で報告した（漢字が同じ名前の読みの違いを、AIは聞き分けられない）。⇒ 相手が「違う」と言ったのに同じ読みが報告されたら、
    考えられる読みのうち、まだ言っていない読みで確かめる（_op_confirm_field）。"""
    likely = hira(likely)
    alts = tuple(dict.fromkeys([likely] + [hira(r) for r in readings if r]))
    return Question(qid, NAME_READING_ASK[kind].format(v=quote(likely)), "name", required=False, conditions=("答えた",),
                    topic="お名前のお読み", reading_of=surface, prefill=likely, readings=alts, reading_kind=kind)


@dataclass
class Item:
    state: str = "empty"
    value: str | None = None
    condition: str | None = None
    ask_backs: int = 0
    rechecks: int = 0
    corrections: int = 0
    history: list = field(default_factory=list)
    pending_value: str | None = None  # 午前・午後を聞いている値（答えが届いたら、そこへ入れる）
    ampm_asked: bool = False          # 午前・午後を聞いたか（1項目1回まで）
    ambiguous_asked: bool = False     # 曖昧な答えの意味を確かめたか（そのあとの「結構です」は、確かめへの返事として受け取る）
    tried_readings: list = field(default_factory=list)  # 名前の読みの確かめで、これまでに言った読み（同じ読みを2度言わない）
    hedged: str | None = None         # 言い直しへの返事が言い切っていなかった（その返事の言葉）：依頼主へは別の欄にお客様の言葉を渡す（Flow.hedged_words）

    def set(self, state: str, why: str) -> None:
        assert state in STATES, state
        self.history.append((self.state, state, why))
        self.state = state


class Flow:
    def __init__(self, questions: list[Question], limits: Limits | None = None, confirm=confirm_phrase,
                 busy_ask: str | None = None, lead_ins: bool = True, play_recordings: bool = False,
                 business_hours: tuple[int, int] = DEFAULT_BUSINESS_HOURS, opening: dict | None = None,
                 voicemail_message: str | None = None, target: dict | None = None, answers: dict | None = None):
        """confirm：言い直しの言い方を作る関数 (値, 場面, 何回目) → 文（ふだんは phrases.confirm。言い方を比べる試験 G-25 だけが別のものを渡す）。
        busy_ask：相手が「忙しい」と言ったときに1回だけ言う一言（業務設定。None なら丁寧に終える＝既定。Intent.md 9-4）。
        target：目的の人のある業務の、名乗りの部分の文（instruction.Business.replay_parts。呼ぶ一言 target・確かめる一言 target_confirm を含む）。
        あれば、目的の人に繋がるまでは calling の段で、最初の質問を渡さない（DESIGN.md 4-3）。None＝「誰でも」（最初に出た人が目的の人）。"""
        self.confirm = confirm
        self.busy_ask = busy_ask
        # 留守番電話に残す伝言（業務設定。Intent.md 9-4・18-2 の14。None なら残さずに切る＝安全側）
        self.voicemail_message = voicemail_message
        # 想定問答（業務設定。Intent.md 5-3 (2)「AIが答えてよいこと」）：問いの名前 → 答えの文。答えは録音（名乗りを録音で流す通話）か、
        # AIがその文のとおりに言う（AIが話す通話）。AIは自分の言葉では答えない（_answer）
        self.answers: dict[str, str] = dict(answers or {})
        if clash := sorted(set(self.answers) & RESERVED_PART_NAMES):
            raise ValueError(f"想定問答の問いの名前 {clash} は、名乗りなどの録音の部分の名前と同じです（使えない名前＝RESERVED_PART_NAMES）")
        self.answered: dict[str, int] = {}  # 答えた回数（部分ごと。同じ答えは2回まで）
        self.party: str | None = None     # いまの相手の種別（PARTY_LABEL の中の名前。報告が無ければ None＝不明。DESIGN.md 4-3）
        self.callback_why: str | None = None  # かけ直しの申し出の理由（CALLBACK_LABEL の中の名前。1通話1回まで）
        self.callback_ids: list[str] = []      # かけ直しで足した質問（目的の人かを確かめに戻ったあと、続きから聞く＝_callback）
        self.busy_asked = False
        self.confirm_counts: dict[str, int] = {}   # 言い直しの場面ごとの回数（同じ言い方が続かないよう、順番に回す）
        self.awaiting_say: str | None = None       # いま返事を待っている言い直しの、言い方（差し戻しのときも同じ言い方を使う）
        # lead_ins：プログラムの返事のあとに話す文の頭に、受けの言葉を置くか（DESIGN.md 6-11。✅ 2026-10-13 ユーザー承認＝既定で置く。
        # 置かない形は、過去の比べの試験（G-25・G-27 の W0）を同じ意味で動かすためだけに残す）
        self.lead_ins = lead_ins
        # play_recordings：次の質問（つなぎの言葉＋質問の文）と締めの言葉を、電話接続側が録音で流し、AIには話させない（DESIGN.md 6-13。
        # ✅ 2026-10-15 ユーザー：よく使う言葉は契約者の単位で、トークスクリプト（質問の文）は依頼主の単位で録音する。G-37 で確かめる）
        self.play_recordings = play_recordings
        self.business_hours = business_hours  # 営業時間（午前・午後を補う・聞くかを決める。依頼主ごとの設定）
        self.lead_counts: dict[str, int] = {}
        self.ack_count = 0               # 次の質問の前に流す相づちを、順番に回す（_ack）
        # AIが答えを報告せずに言い直し、相手に値を直された：(質問, 直され方＝AFTER の鍵)。その答えの報告で、直され方に合う言い直しにする
        self.unreported_correction: tuple[str, str] | None = None
        self.callee_said = ""            # いまの相手の発話の文字（会話AIの聞き取り。届いた分だけ。on_heard）
        # 会話AIの、相手の話し始め・話し終わりの知らせ（on_activity）：いまの発話で話し終わりが来たか・その位置・いま相手が話しているか。
        # 相手の声の終わりの位置（on_voice。電話接続側の声の見張り）。位置は、会話AIへ送った音の上の秒（words_complete）
        self.callee_ended = False
        self.callee_end_at: float | None = None
        self.callee_speaking = False
        self.callee_voice_end: float | None = None
        self.midway = False              # いま受けている報告は、相手の言葉が出揃う前に届いた（handle の midway。電話接続側が付ける）
        # いまの相手の発話の確かめ（on_check。CHECKS の名前 → 別の耳の文字／意味の判定の中の名前。届いていないものは入らない）。
        # いまの相手の言葉のものだけ（check_said：その確かめが見た相手の言葉。None＝発話まるごと。言葉が増えたら、前の言葉の判定は外す）
        self.check: dict[str, str] = {}
        self.check_said: dict[str, str | None] = {}
        self.checks_used: tuple = ()     # この通話で使う確かめ（use_checks。届かなかったものを、通話後の検証へ回すため＝unchecked）
        # 受け取った項目の、受け取ったときの材料（相手の発話の番号・会話AIの聞き取り・受け取った答え・確かめ）。遅れて届いた確かめを、
        # 同じ決まり（_compare）で当てるため（_late）。受け取るたびに書き換える（その項目を聞き直したら、新しい発話のもの）
        self.taken: dict[str, dict] = {}
        # 相手が取り次いでいる最中か（意味の判定が HOLD と決め、AIに黙って待たせた。次の相手の発話で解く）。電話接続側は、この間、
        # 相手が黙っている秒を数える見張り（6-7・6-15 のつなぎの一言・張り替え）を止める（相手はいま電話口にいない。DESIGN.md 6-8）
        self.holding = False
        self.nudged_turn = -1            # 報告を促す知らせを出した相手の発話の番号（unreported_note。1つの発話に1回まで）
        # いちばん最近「合っている」で確かめが済んだ項目と、そのときの流れの位置（同じ相手の発話への報告を、AIが「違う値を言われた」に
        # 直したとき、その確かめを取り消して戻すため。_reopen）
        self.last_affirmed: dict | None = None
        self.questions = {q.id: q for q in questions}
        self.order = [q.id for q in questions]
        self.business_ids = list(self.order)  # 業務の質問（かけ直しの質問の途中で目的の人が出たら、ここから聞く＝_target_back）
        self.limits = limits or Limits()
        self.items = {q.id: Item() for q in questions}
        self.current: str | None = self.order[0]
        self.phase = "asking"            # asking → rechecking → closing
        self.recheck_queue: list[str] = []
        self.awaiting_confirm: str | None = None
        self.callee_turns = 0
        self.confirm_requested_at: int | None = None
        # 通話の事実（種類 → 起きた順の記録。_note だけが書く）。業務の判定（DESIGN.md 7-1。CL-2 の judge_task）へ渡す、会話の側が決めるもの：
        # do_not_call＝発信禁止の申告（原則14）／complaint＝怒り・苦情（人へ知らせる印）／end＝通話を終えた理由。どれも根拠の発話つき
        self.facts: dict[str, list[dict]] = {}
        self.end_reason: str | None = None   # 締めた理由（END_LABEL の中の名前。_close だけが書く）
        self.log: list[dict] = []        # 呼ばれた操作と返事（確かめの記録）
        # 名乗りの録音の文（部分 → 文。instruction.Business.replay_parts）。名乗りを録音で流す形のときだけ（無ければ流し直しの操作は無い）
        self.opening = opening or {}
        self.replays = 0
        # 「AIですか？」と聞かれた回数と、決まった言い方（「何か問題でもございましたでしょうか？」）への相手の返事を待っているか（_ai_question）
        self.ai_asks = 0
        self.ai_said = ""  # エーアイかと聞かれて、いま返事を待っている言い方（_ai_question。_now が見せる）
        self.ai_pending = False
        # 名乗りの録音のうち、相手にまだ届いていない部分（on_opening_cut。録音が最後まで流れたら空）
        self.opening_unheard: list[str] = []
        # 目的の人（DESIGN.md 4-3）：繋がるまでは calling の段（いまの質問は無い）。名乗りの終わりは呼ぶ一言なので、用件はまだ届いていない
        self.target = target
        # 呼んでいる段は、電話口の方ごとに数える（DESIGN.md 4-3 の区間）：speaker_from＝いま電話口にいる方が話し始めた相手の発話の番号
        # （取り次ぎの判定のあとの発話・代わった方の発話から）。呼ぶ一言・確かめる一言は、流したときの相手の発話の番号を残し、その方に流した
        # 数を数える（呼ぶ一言は1人に2回まで・確かめる一言は1人に1回）。⛔ 2026-10-21 の見直し：通話ごとに数えていたので、受付の方に1回
        # 確かめると、あとで代わった方を確かめられず、回数を使い切ったあとは判定に関係なく繋いでいた（不在・取り次ぎと読んでいても）
        self.speaker_from = 0
        self.target_called: list[int] = [0] if target else []  # 呼ぶ一言を流したときの相手の発話の番号（名乗りの終わりの1回から）
        self.target_confirmed: list[int] = []                  # 確かめる一言を流したときの相手の発話の番号（その次の発話が、確かめへの返事）
        self.target_sorry: list[int] = []                      # お呼びし直すときにお詫びした番号（同じ方に1回まで＝_call_target）
        self.confirm_open = False                              # 確かめへの返事を待っている（お呼びし直したら消す＝_now）
        self.greeted_at: int | None = None                     # 判定を待つ間にあいさつを流したときの番号（hold_play。その方には繋いだときに流さない）
        # 呼んだあとに取り次がれた（受付の方の報告・取り次ぎを待つ・代わった人）＝いま電話口にいる方は、まだ名乗りを聞いていない（その方に
        # 名乗りを流し直したら消す＝_call_target）
        self.passed_on = False
        # 呼んでいる段の、相手の発話ごとの言葉（出揃ったもの。判定に「その前に相手が言ったこと」として渡す＝judge_context）と判定（遅れて
        # 届いた分も。出揃った言葉のものだけ。電話口の方の「ご本人」の読みを数える＝_target_readings）
        self.calling_words: dict[int, str] = {}
        self.calling_judged: dict[int, dict[str, str]] = {}
        # 呼んでいる段の、こちらに尋ねているかの読み（発話の番号ごと＝on_check の asks）と、取り次ぎの判定が区切りを動かす前の区切り（尋ねて
        # いた発話なら戻す＝尋ねた方は、こちらの答えを待っている）
        self.calling_asks: dict[int, str] = {}
        self.hold_moved: dict[int, tuple[int, bool]] = {}
        # 繋ぐのを止める弱い読み（確からしさが線より下の、「ご本人」ではない読み＝on_unsure）：いまの言葉の分（受け持ち → 見た言葉）と、
        # 終わった発話の分（発話の番号 → 受け持ち）。判定の読み（check・calling_judged）には入れない（_doubted）
        self.doubt: dict[str, str | None] = {}
        self.calling_doubted: dict[int, set[str]] = {}
        # 呼んでいる段の判定の質問（judge_context）：呼ぶ一言と、確かめる一言（確かめへの返事を待つ間は、こちらが実際に聞いた問いを質問にする）
        # 確かめる一言は2つ（CONFIRM_PARTS：確かめ・はっきりしない返事への聞き返し）。返事を待っている方を confirm_part に持つ
        asked_parts = (("T1", "target"),) + tuple((f"T{i + 2}", part) for i, part in enumerate(CONFIRM_PARTS)) if target else ()
        self.confirm_part = CONFIRM_PARTS[0]
        self.calling_question = (Question("T1", target["target"], "yes_no", required=False, conditions=tuple(TARGET_MEANINGS),
                                          meanings=dict(TARGET_MEANINGS)) if target else None)
        self.confirm_questions = {part: Question(qid, target[part], "yes_no", required=False, conditions=tuple(CONFIRM_MEANINGS),
                                                 meanings=dict(CONFIRM_MEANINGS)) for qid, part in asked_parts[1:]}
        # 呼んでいる段の、こちらに尋ねているかの問い合わせ（asks_context。質問の文は、こちらが最後に聞いたこと＝呼ぶ一言か確かめる一言）
        self.asks_questions = {qid: Question(qid, target[part], "yes_no", required=False, conditions=tuple(ASKS_MEANINGS), meanings=dict(ASKS_MEANINGS))
                               for qid, part in asked_parts}
        self.target_claimed = False  # いまの目的の人の確かめが、AIの「目的の人」の報告から来たか（_now が「まだ受け付けていない」と伝える）
        if target:
            self.phase, self.current = "calling", None
            self.opening_unheard = ["purpose"] if target.get("purpose") else []
        elif self.questions[self.current].reading_of:
            self._enter_reading(self.current)

    # ── 名乗りの録音の途中で相手が話し始めて、録音を止めた（電話接続側の見張りが止めた。DESIGN.md 6-13）
    def on_opening_cut(self, done: list[str]) -> None:
        """届いた文（done）から、まだ届いていない名乗りの部分を覚える。流し直しのとき、AIが選んだ部分に足して流す（_op_replay_opening）。
        ⛔ G-50：用件が届く前に「どちら様ですか」と聞かれると、AIは「名乗り」だけを選んだ（11本中11本）＝用件が一度も届かないまま
        質問へ進む。どこまで届いたかはプログラムが知っているので、足すのはプログラム（AIの選び方に任せない）。"""
        self.opening_unheard = [k for k in OPENING_ORDER if self.opening.get(k) and self.opening[k] not in done]

    # ── 相手の発話が届いた（本番では、相手の音声の発話の検出で数える）
    def on_callee(self) -> None:
        if self.phase == "calling":
            self._keep_calling_turn()
        self.doubt = {}
        self.callee_turns += 1
        self.callee_said = ""
        self.callee_ended, self.callee_end_at, self.callee_voice_end = False, None, None
        self.check, self.check_said = {}, {}
        self.holding = False

    def _keep_calling_turn(self) -> None:
        """いまの発話の言葉と、その言葉の判定（判定のあとに相手が話し続けた分は除く）を、呼んでいる段の記録に残す（唯一の場所）：発話が
        終わったとき（on_callee）と、その発話でかけ直しへ移るとき（_callback。⛔ 2026-10-22 の見直しで見つけた漏れ：移ったあとの on_callee は
        呼んでいる段ではないので、かけ直しを決めた発話の「不在」の読みが残らず、かけ直しの質問の途中で目的の人かを確かめに戻ったあと、
        違う方に岡田様をお呼びし直していた）"""
        if not self.callee_said.strip():
            return
        self.calling_words[self.callee_turns] = self.callee_said.strip()
        done = {k: v for k, v in self.check.items() if k in JUDGMENTS and self.check_said.get(k) in (None, self.callee_said)}
        if done:
            self.calling_judged.setdefault(self.callee_turns, {}).update(done)
        if doubted := {k for k, said in self.doubt.items() if said in (None, self.callee_said)}:
            self.calling_doubted.setdefault(self.callee_turns, set()).update(doubted)

    # ── 相手の同じ発話を、張り替えた新しいつなぎへ送り直す（DESIGN.md 6-7）：相手は話していないので数えず、文字だけを受け取り直す。
    #    ⛔ 2026-10-17 に見つけた穴：送り直しで on_callee を呼ばないまま、新しいつなぎの聞き取りを足していた（同じ言葉が2回たまる）
    def on_resend(self) -> None:
        self.callee_said = ""
        self.doubt = {}
        self.callee_ended = self.callee_speaking = False  # 知らせは、新しいつなぎから届き直す（位置も、新しいつなぎの音の上で数え直す）
        self.callee_end_at = self.callee_voice_end = None
        self.check, self.check_said = {}, {}

    # ── 相手の言葉の文字が届いた（会話AIの聞き取り。値を直されたときの直され方を、プログラムが決めるのに使う）
    def on_heard(self, text: str) -> None:
        self.callee_said += text
        for kind in [k for k, said in self.check_said.items() if said is not None and said != self.callee_said]:
            del self.check[kind], self.check_said[kind]  # 前の言葉の判定（相手が話し続けた）。いまの言葉で判定し直したものだけを使う
        for kind in [k for k, said in self.doubt.items() if said is not None and said != self.callee_said]:
            del self.doubt[kind]

    # ── 会話AIが、相手の話し始め（speaking=True）・話し終わり（False）を知らせた（Live の voice_activity。電話接続側が受ける）。
    #    at_s＝その位置（会話AIへ送った音の上の秒。audio_offset）
    def on_activity(self, speaking: bool, at_s: float | None = None) -> None:
        self.callee_speaking = speaking
        if not speaking:
            self.callee_ended, self.callee_end_at = True, at_s

    # ── 相手の声が、会話AIへ送った音の上で at_s 秒まで続いた（電話接続側の声の見張り。相手がまた話せば、後ろへずれる）。
    #    None＝声の終わりを見張らない（話し始め・話し終わりの知らせが来ないつなぎだと、電話接続側が決めた）
    def on_voice(self, at_s: float | None) -> None:
        self.callee_voice_end = at_s

    @property
    def words_complete(self) -> bool:
        """相手の言葉の文字が出揃っているか：いまの発話で、会話AIが話し終わりを知らせ、その位置が相手の声の終わり（on_voice）より後で、
        そのあと相手がまた話し始めていない。会話AIの聞き取りは、話し終わりの知らせの直前（同じ瞬間）に、その区切りの文字を渡す（G-60）。
        話し終わりの位置は、その区切りの最後の声から1.0〜1.4秒あと（G-60 の45つなぎで毎回同じ値）＝声の終わりより前の位置の話し終わりは、
        途中の区切り（文と文の間）。⛔ 2026-10-19 G-57：会話AIが遅れ、「岡田ですね。」の話し終わりが、声を送り終えて3.8秒あとに届いた
        （「少々お待ちください。」は送り終えていたのに、まだ聞き取っていない）＝位置を見ずに「話し終わり」だけで決めると、途中の言葉で判定する。
        DESIGN.md 6-10。
        ⛔ 2026-10-19 G-57 の本番の形：声の終わりから決めた秒数だけ続きの文字を待つ形では、受付の方の「岡田ですね。」「少々お待ちください。」
           （間0.6秒）の「岡田ですね。」だけで判定した。文字の届き方は、相手の話し方と会話AIの混み具合で変わる（G-34：考えている相手の
           「ええとですね。」「3人です。」は、声の終わりのあとに0.7秒あいて届いた）⇒ 秒数ではなく、会話AIの区切りの知らせで決める。"""
        if self.callee_voice_end is None and not self.callee_ended and not self.callee_speaking:
            return True  # 声を見張っていない（文字で送った相手の言葉）・知らせも来ていない：届いた言葉が全部
        return (self.callee_ended and not self.callee_speaking
                and (self.callee_voice_end is None or self.callee_end_at is None or self.callee_end_at >= self.callee_voice_end))

    @property
    def ear_said(self) -> str | None:
        return self.check.get("ear")

    @property
    def meaning_said(self) -> str | None:
        """いまの相手の言葉の意味（意味の判定）：届いた判定がそろって選んだもの（まだ1つなら、その判定＝早く届いたほうで決める）。
        届いた判定どうしが食い違えば「決められない」（どちらにも決めない＝確かめる側）。届いていなければ None。
        ⛔ 2026-10-19 の見直し：その場の判定（judge）だけを見ていた＝もう1つの判定が先に届いても、遅いほうを待っていた（G-57 の9回目：
           その場の判定 328回のうち58回が2秒超）"""
        got = [self.check[k] for k in JUDGMENTS if k in self.check]
        return (got[0] if all(g == got[0] for g in got) else UNDECIDED) if got else None

    @property
    def judging(self) -> bool:
        """この通話で意味の判定を使うか（use_checks）。"""
        return any(k in self.checks_used for k in JUDGMENTS)

    # ── この通話で使う確かめ（電話接続側が、通話の始めに1回呼ぶ。CHECKS の名前）
    def use_checks(self, *kinds: str) -> None:
        assert all(k in CHECKS for k in kinds), kinds
        self.checks_used = tuple(kinds)

    # ── 確かめが届いた（唯一の入口）：別の耳（会話AIとは別の音声認識。DESIGN.md 6-10 の (5)。G-15）の文字、または意味の判定（会話AIとは
    #    別の文字のAI。meaning_judge。6-10 の (6)。G-53）の答え（中の名前・HOLD・HANDOVER・UNDECIDED）。turn＝どの相手の発話の確かめか
    #    （on_callee で数えた番号。省けば、いまの発話）。落ちた・時間切れは呼ばない（届いていない＝unchecked）。
    #    その発話への報告で、もう受け取った項目があれば、同じ決まりで当てる（_late）
    def on_check(self, kind: str, value: str | None, turn: int | None = None, said: str | None = None) -> None:
        """said：その確かめが見た相手の言葉（意味の判定。None＝発話まるごと＝別の耳）。いまの言葉と違えば（判定のあとに相手が話し続けた）、
        いまの発話の確かめにも、その言葉より後の言葉で受け取った項目にも当てない（同じ言葉で受け取った項目には当てる）。"""
        assert kind in CHECKS, kind
        turn = self.callee_turns if turn is None else turn
        if value is None:
            return
        current = turn == self.callee_turns and (said is None or said == self.callee_said)
        if current:
            self.check[kind], self.check_said[kind] = value, said
        if self.phase == "calling" and kind in JUDGMENTS and turn < self.callee_turns and said in (None, self.calling_words.get(turn)):
            # 終わった発話の判定が遅れて届いた：その方の「ご本人」の読みとして数える（_target_readings。⛔ 2026-10-21 の見直し：いまの発話の判定
            # だけを数えていたので、判定が遅れて目的の人かを確かめると、確かめの間に届いた前の発話の判定を使わず、また確かめていた）
            self.calling_judged.setdefault(turn, {})[kind] = value
        if self.phase == "calling" and kind == "asks" and (current or turn < self.callee_turns):
            self.calling_asks[turn] = value
            if value == ASKS_YES and turn == self.callee_turns and turn in self.hold_moved:
                # 取り次ぎと読まれた発話が、こちらへの質問だった（「岡田ですね。失礼ですが、どちら様でしょうか？」）：尋ねた方は電話口を
                # 離れず、こちらの答えを待っている ⇒ 区切りを戻す（次の発話も同じ方。いまの発話のうちだけ＝次の方が話し始めたら戻さない）
                self.speaker_from, self.passed_on = self.hold_moved.pop(turn)
        if (self.phase == "calling" and ((kind in JUDGMENTS and value in (HOLD, HANDOVER)) or (kind in HOLD_READINGS and value == HOLD))
                and (current or turn < self.callee_turns) and not (value == HOLD and self.calling_asks.get(turn) == ASKS_YES)):
            # 取り次がれた：このあと出る目的の人は、名乗りを聞いていない別の方。前の発話の判定が遅れて届いても同じ（その方が取り次いだ事実は
            # 変わらない）。⛔ 2026-10-19 G-62：受付の方の「少々お待ちください。」の判定が、代わった方が話し始めたあとに届き、取り次がれたことが
            # 残らず、代わった岡田様に、あいさつと名乗りを流さずに用件から始めた。こちらに尋ねた発話の「取り次ぎ」は、区切りを動かさない（上）
            before = (self.speaker_from, self.passed_on)
            self._new_speaker(turn, after=value == HOLD)
            if value == HOLD and self.speaker_from != before[0]:
                self.hold_moved.setdefault(turn, before)
        if self.phase != "calling" and kind in JUDGMENTS and (c := (self.facts.get("connect") or [None])[-1]) and c["turn"] == turn:
            # 繋いだ発話の判定が、繋いだあとに届いた：通話の事実に残す（通話後の検証が、判定なしで繋いだ通話を確かめる。DESIGN.md 6-12）
            c.setdefault("late", {})[kind] = value
        for qid, t in list(self.taken.items()):
            if t["turn"] == turn and kind not in t["check"] and (said is None or said == t["said"]):
                t["check"][kind] = value
                self._late(qid)

    def on_unsure(self, kind: str, value: str, turn: int | None = None, said: str | None = None) -> None:
        """判定の受け持ちが、確からしさが線より下の読み（value）しか返さなかった（meaning_judge.NoJudgment の unsure＝代わりの Jev）。
        目的の人を呼んでいる段（繋ぐのは取り消せない）では、「ご本人」ではない読みだけを「決められない」として数える（弱い読みは、繋ぐのを
        止める側にだけ使い、繋ぐ側には使わない）。答えの段では数えない（届かなかった確かめ＝通話後の検証。答えは遅れて届いた判定でも当て直せる）。
        ⛔ 2026-10-21 G-57 tc11（確かめにいいえ・L4）：中身を見ずに「決められない」と数えたので、確かめへの「はい、そうです。」を Jev が
           「ご本人」（確からしさ 0.94）と読んだのに繋がず、岡田様にお呼びし直した
        ⛔ 2026-10-21 G-57 tc10（確かめにいいえ・L4）：最初の方の「はい、お電話変わりました。」（前の言葉なし）を、AIは決まりに反して「目的の人」と
           報告し、3.5 Flash Lite も「ご本人」と読んだ。もう1つの受け持ちは Sonnet が止まっていて、代わりの Jev は「代わった人が話し始めた」と
           読んだが確からしさ 0.89 で線より下＝捨てて「答えなし」と同じに数え、読みが2つそろったとして別の方（山田様）に繋いだ"""
        if self.phase != "calling" or unsure_counts(kind, value) is None:
            return
        if kind in JUDGMENTS:
            # 繋ぐのを止める印だけ（判定の読みには入れない＝ほかの判定と食い違いを作らない＝_doubted）。⛔ 2026-10-22 G-57 tc15：受付の方の
            # 「岡田ですね。（間）少々お待ちください。」を、Sonnet は「取り次ぎを待つ」、代わりの Jev も弱く「取り次ぎを待つ」と読んだのに、
            # 弱い読みを「決められない」として判定に入れていたので2つが食い違い、黙って待たずに、受付の方へ確かめる一言を流した
            turn = self.callee_turns if turn is None else turn
            if turn == self.callee_turns and (said is None or said == self.callee_said):
                self.doubt[kind] = said
            elif turn < self.callee_turns and said in (None, self.calling_words.get(turn)):
                self.calling_doubted.setdefault(turn, set()).add(kind)
            return
        self.on_check(kind, unsure_counts(kind, value), turn, said)

    def judge_context(self) -> tuple["Question | None", str | None, str | None]:
        """意味の判定に渡すもの（相手の発話の始めに決める）：いまの質問と、こちらが言い直して確かめた言葉（言い直しの返事を待っているとき）と、
        その前に相手が言ったこと。目的の人を呼んでいる段（calling）では、呼ぶ一言を質問にし（目的の人かを確かめたあとは、その言い方を添える）、
        その段の相手の言葉（直前の3つまで）を添える（取り次いだ方の言葉で、代わった方が誰かが決まる＝TARGET_MEANINGS）。
        ⛔ 2026-10-19 G-62：その前の言葉を渡しておらず、受付の方「岡田ですね。少々お待ちください。」のあとの「はい、お電話代わりました。」を、
           判定の片方が「代わった人」と読んだ（ユーザー：「『ご本人』になりますよね」）
        ⛔ 2026-10-18 の見直しで見つけた漏れ：いまの質問が無い段では判定が走らず、取り次ぎ（「少々お待ちください」）を見分ける独立した確かめも、
           AIの「目的の人」の報告を確かめる手立ても無かった"""
        if self.phase == "calling":
            # 確かめへの返事を待つ間は、確かめる一言（「〇〇様でいらっしゃいますでしょうか？」）を質問にする。⛔ 2026-10-21 G-59：呼ぶ一言（「…様は
            # いらっしゃいますでしょうか？」）を質問にし、確かめる一言を「言い直して確かめた言葉」として添えていたので、3.5 Flash Lite は
            # 「お電話代わりました。担当の山田です。」のあとの「いいえ、私は山田と申します。」を3回中2回「ご本人」と読んだ（決まりの文を足す形は、
            # 判定ごとに誤りの出方が入れ替わるだけだった＝Jev が「いえ、違います。」を「ご本人」と読んだ）
            q = self.confirm_questions[self.confirm_part] if self.asked_target is not None and self.confirm_open else self.calling_question
            return q, None, "／".join(list(self.calling_words.values())[-3:]) or None
        q = self.questions.get(self.current) if self.current else None
        return q, (self.awaiting_say if self.awaiting_confirm is not None else None), None

    def asks_context(self) -> tuple["Question", str | None] | None:
        """呼んでいる段の、こちらに尋ねているかの問い合わせ（電話接続側が、目的の人の判定と同時に出す。確かめの名前 "asks"）：(質問, その前に
        相手が言ったこと)。呼んでいる段でなければ None（答えの段の質問は、AIが答える＝これまでどおり）。"""
        if self.phase != "calling":
            return None
        q, _, before = self.judge_context()
        return self.asks_questions[q.id], before

    # ── AIのターンが、声は出したのに報告なしで終わった・呼んでいる段で決めずに待たせたまま終わった（電話接続側が呼ぶ）：報告を促す知らせ
    #    （出さないなら None）。
    def unreported_note(self) -> str | None:
        """いまの質問の答えだと、意味の判定が決めた相手の言葉（on_check の judge が選択肢・どちらでも・わからない・答えたくないのどれか）に、
        AIが報告しなかったときだけ、1つの相手の発話に1回まで。知らせには、いまの質問の文を入れない（報告することだけ）。
        この知らせへのAIの声は、報告より前のものは相手に流さない（電話接続側）。
        ⛔ 2026-10-17：記録の通話で、相手の「はい。」「はい、先週しました。」（雑音）のあと、AIが短く声を出しただけで報告せずに終えた回が34回。
           プログラムは気づく手立てを持っていなかった（PHASE0「答えの報告が抜けたとき」の案）。
        ⛔ 2026-10-17 の1回目：判定を見ずに、相手の言葉があれば出し、知らせに「いまの質問は…」を入れた ⇒ 取り次ぎ（「代わりますので少々
           お待ちください」）のあとにも出て、声の型 S4 の3本中2本で、代わった方にAIが自分で名乗って質問した・流し直しを頼まなかった
           （6-7 の G-19 と同じ形：AIへの文字の合図が会話を乱す）。答えの3本では、AIは3本とも黙って正しく報告した。"""
        if self.phase == "calling":
            # 目的の人を呼んでいる段：AIの報告を、相手の言葉が出揃う前のものとして決めずに待たせた（_wait_for_words）のに、言葉が出揃っても
            # AIが報告し直さない ⇒ 出揃った言葉で報告し直すように知らせる（1つの相手の発話に1回まで）。
            # ⛔ 2026-10-21 G-57 tc10（判定を止めた形・L4）：AIが言葉の途中で報告し、待たせたあと報告し直さなかったので、その方に何も流れず
            #    （「お電話代わりました。担当の山田です。」に岡田様をお呼びし直さなかった）、3回続けて止まった通話もあった
            # 確かめる一言への返事に、AIが報告しなかった（⛔ G-57 tc11：157回中3回。2回は繋がったと思い込んで自分で名乗り始めた）も同じ
            # 黙って待たせた（受付の方の報告・取り次ぎ）あとに、その言葉を、届いた判定が「不在」と読んだ ⇒ かけ直しへ（⛔ 2026-10-22 ユーザー：
            # 「受付の人が再度、やはり本人は不在やでられないといった報告の場合も黙っているということですか？」＝判定が遅れると、黙ったままだった）
            last = self.log[-1] if self.log else None
            now = last is not None and last["callee_turns"] == self.callee_turns
            waited = now and last["reply"].get("result") == "wait"
            quiet = now and last["reply"].get("result") in ("noted", "hold")
            replied = (self.asked_target is not None and self.confirm_open and self.callee_turns == self.asked_target_turn + 1 and not now)
            if not self.words_complete or self.nudged_turn == self.callee_turns:
                return None
            said = self.callee_said.strip()[:40]
            if quiet and self._asking_now():
                self.nudged_turn = self.callee_turns
                return f"［プログラムからのお知らせ］{self._asking_note()}このお知らせには、声で返事をしません。"
            away_told = now and last["reply"].get("away")  # かけ直しへ進むように伝えた（_away）
            if away_told and self._hold_now():
                # 取り次ぎの読みが、あとから届いた：かけ直しは、代わった方の言葉を聞いてから（⛔ 2026-10-22 G-57 tc22：取り次ぎの読みが4.6秒で届いた）
                self.nudged_turn = self.callee_turns
                return (f"［プログラムからのお知らせ］相手の言葉（「{said}」）は、取り次ぎを待つ言葉でもありました（意味の判定）。何も言わずに、"
                        "代わった方が話し始めるのを待ちます（かけ直しの申し出は、代わった方の言葉を聞いてからです）。このお知らせには、声で返事をしません。")
            if away_told and not any(x["op"] == "request_callback" for x in self.log if x["callee_turns"] == self.callee_turns):
                # かけ直しへ進むように伝えたのに、AIが進まずにターンを終えた（⛔ 2026-10-22 G-57 tc22：代わった方の番でかけ直しへ進まず、次の言葉で
                # 進んだので、相手がもう言った戻る時刻を聞き直し、話がずれた）
                self.nudged_turn = self.callee_turns
                return f"［プログラムからのお知らせ］{self._away_text()}このお知らせには、声で返事をしません。"
            if quiet and self._away_now() and not self._hold_now():  # 取り次ぎの最中は知らせない（代わった方が話したら、その言葉で決める）
                self.nudged_turn = self.callee_turns
                return (f"［プログラムからのお知らせ］相手の言葉（「{said}」）は、お呼びした方がいま電話に出られないという意味です（意味の判定）。"
                        f"何も言わずに、request_callback を、聞いた言葉に合う{ARG_LABEL['why']}で呼びます（断られたときは request_end）。"
                        "このお知らせには、声で返事をしません。")
            if not (waited or replied):
                return None
            self.nudged_turn = self.callee_turns
            what = "は、目的の人かを聞いた返事です" if replied else "は、最後まで届きました"
            again = "" if replied else "もう一度"
            return (f"［プログラムからのお知らせ］相手の言葉（「{said}」）{what}。何も言わずに、この言葉で、{again}報告してください"
                    f"（{self._report_hint()}）。"
                    "まだ目的の人に繋いでいません。このお知らせには、声で返事をしません。")
        if self.phase == "closing" or self.current is None or self.awaiting_confirm is not None:
            return None
        q = self.questions[self.current]
        if self.meaning_said not in q.all_conditions or self.nudged_turn == self.callee_turns:
            return None
        self.nudged_turn = self.callee_turns
        return (f"［プログラムからのお知らせ］相手の言葉（「{self.callee_said.strip()[:40]}」）は、{q.id} への答えです。"
                "何も言わずに、record_answer で報告してください。このお知らせには、声で返事をしません。")

    def _late(self, qid: str) -> None:
        """受け取ったあとに届いた確かめを、受け取ったときの材料に当てる（決まりは _compare の1か所）。食い違えば、受け取った答えを依頼主へ
        渡さない：「-」にして、締めの前の念のための確認でもう一度聞く（まだ聞けるとき）。確かめずに渡った誤りを、確かめの遅れで通さないため。"""
        it, q, t = self.items[qid], self.questions[qid], self.taken[qid]
        if it.state not in TAKEN:
            return
        why, _ = self._compare(q, t["said"], t["reported"], t["check"], policy=False, confirming=t["confirming"])
        if not why:
            if t["confirming"] and not q.reading_of and not it.hedged and Flow._hedged(t["said"], t["check"]):
                it.hedged = (t["said"] or "").strip()[:40]
                it.set(it.state, f"言い切っていない返事だった（「{it.hedged}」・受け取ったあとに届いた確かめ）")
            return
        it.set("unresolved", f"{why}（受け取ったあとに届いた確かめ）・受け取った答え（「{label(t['reported'])}」）を渡さない")
        self.taken.pop(qid)
        if (self.phase == "rechecking" and q.required and it.rechecks < self.limits.recheck and qid != self.current
                and qid not in self.recheck_queue):
            self.recheck_queue.append(qid)

    def unchecked(self) -> dict[str, list[str]]:
        """受け取った項目のうち、使う確かめ（use_checks）が通話の終わりまで届かなかったもの（項目 → 届かなかった確かめ）。
        通話後の検証（DESIGN.md 6-12）が、録音からやり直す。やり直せなければ「-」に落とす（確かめていない答えを、取れたとして渡さない）。"""
        # 会話AIの聞き取りが届かなかった発話は、別の耳の文字が届いても比べられない（届かなかったのと同じ）
        return {qid: miss for qid, t in self.taken.items()
                if self.items[qid].state in TAKEN
                and (miss := [k for k in self.checks_used if k not in t["check"] or (k == "ear" and not t["said"])])}

    # ── この操作の返事を作る前に、確かめ（use_checks）を待つか：答えをそのまま受け取りうる報告だけ（DESIGN.md 1章）。
    #    確かめない型の答え・「わからない」「答えたくない」・自由な意見の答え／言い直しへの返事／代わった方への名乗りの流し直し（取り次ぎを待つ
    #    間に流さない）。値を聞く型の最初の答えは、言い直して確かめるので待たない（その返事で待つ）
    def wants_checks(self, name: str, args: dict | None = None) -> bool:
        a = self._args_in(dict(args or {}))
        if name == "replay_opening":
            return a.get("part") == "handover"
        if name == "report_party":
            # 呼んでいる段の「目的の人」「受付の方」の報告は、判定に当ててから決める（_calling_reported。⛔ 2026-10-21 の見直し：受付の方の報告は
            # 判定を待たずに黙って待たせていたので、「席を外しております」「私が岡田です」を受付の方と報告されると、そのまま黙っていた）
            return self.phase == "calling" and a.get("party") in PERSON_PARTIES
        if name == "request_callback":
            return self.phase == "calling"  # 呼んでいる段のかけ直しの申し出も、同じ1か所で判定に当ててから決める（⛔ G-57 tc24）
        if name == "request_end" and a.get("reason") == "not_transferred":
            return self.phase == "calling"  # 取り次いでいただけなかった：取り次ぎの読みがあれば黙って待つ（_not_transferred）
        q = self.questions.get(self.current) if self.current else None
        if q is None:
            return False
        if name == "record_answer":
            return self.awaiting_confirm is None and (q.type in CHOICE_TYPES or not q.confirm or a.get("condition_id") in SYSTEM_CONDITIONS)
        return name == "confirm_field" and self.awaiting_confirm is not None

    # ── 取り消せない報告の判定を待つ間に流す録音（電話接続側が、「はい、」の相づちの代わりに流し、流し終わるまで待つ長さを延ばす）。無ければ None。
    def hold_play(self, name: str, args: dict | None = None) -> dict | None:
        """目的の人に繋ぐかを決める報告（binding）で、電話口の方がまだ名乗りを聞いていない新しい方（取り次がれた方・AIが「代わった方」と報告した
        方）なら、あいさつ「お忙しいところ恐れ入ります。」の録音（名乗りの部分 greeting）。その方が誰でも、新しい方には必ず流す言葉なので、判定を
        待つ時間に流しても無駄にならない（繋いだら名乗り・用件から・お呼びし直すなら名乗りから・確かめるなら確かめる一言から続く）。
        流したら、その方はあいさつを聞いた（greeted_at。_connect・_call_target は流さない）。
        ✅ 2026-10-22 ユーザー：「名乗らない方（取り次ぎのあとの『はい、お電話代わりました。』）の後には、録音で、『お忙しいところ、失礼いたします。』
           を入れておいて、その間に判定を待てばいいんじゃないですか？」（言い方は、承認済みのあいさつ＝phrases.GREETING。⛔ G-57：判定は2つとも
           正しく「ご本人」と読んだが2秒に間に合わず（2.9〜4.2秒）、決まりどおり1回確かめていた）"""
        a = self._args_in(dict(args or {}))
        if name == "request_callback" or not self.binding(name, a) or not self.opening.get("greeting") or self._greeted():
            return None  # かけ直しの申し出（相手は目的の人ではない側）には、あいさつを流さない
        handover = name == "replay_opening" and a.get("part") == "handover"
        if not (self.passed_on or (handover and self.asked_target is None)):
            return None
        self.greeted_at = self.callee_turns
        return {"kind": "opening", "part": "greeting", "text": self.opening["greeting"]}

    # ── この報告で決めることは、取り消せないか（電話接続側が、報告を待たせる間に判定を全部待つか＝早く届いたほうで決めないか）。
    #    目的の人に繋ぐ（呼んでいる段の「目的の人」の報告・代わった方への名乗りの流し直し）は、繋いだら戻せない（名乗り・用件・質問が流れる）。
    #    呼んでいる段のかけ直しの申し出も同じ（受け取ると、目的の人に繋がないまま戻る時刻などを聞いて締める。G-57 tc24）。
    #    答えの報告は、遅れて届いた判定で当て直せる（Flow._late）ので、早く届いたほうで決める
    #    （2026-10-19 G-62。⛔ 訂正：最初に例に挙げた「はい、お電話代わりました。」は、受付の方が岡田様に取り次いだあとなので「ご本人」が
    #     正しかった＝ユーザー。食い違いの真因は、判定に前の言葉を渡していなかったこと＝judge_context。全部を待つ決まりは残す）
    def binding(self, name: str, args: dict | None = None) -> bool:
        a = self._args_in(dict(args or {}))
        return self.phase == "calling" and ((name == "report_party" and a.get("party") == "target")
                                            or (name == "replay_opening" and a.get("part") == "handover")
                                            or name == "request_callback")

    # ── AIの操作を受ける唯一の入口（AIに見せる言葉＝日本語）。ここで1回だけ中の名前に置き換え、返事を日本語に戻す。
    #    相づち（ack）は電話接続側が流すもので、AIには見せない（AIの返事から外す）
    def handle(self, fc, midway: bool = False) -> dict:
        """midway：この報告は、相手の言葉が出揃う前に届いた（AIが相手の言葉の間で報告した。電話接続側が、届いた時点の words_complete で付ける）。"""
        self.midway = midway
        try:
            return self._reply_out(self.apply(fc.name, self._args_in(dict(fc.args or {}))))
        finally:
            self.midway = False

    # ── handle と同じ。返事を「話させない」形（SILENT）で返すかも返す（電話接続側が録音を流すときは、AIに話させない）
    def handle_call(self, fc, midway: bool = False) -> tuple[dict, bool]:
        reply = self.handle(fc, midway)
        last = self.log[-1]["reply"]
        # 話させない返事：録音を流す（AIは続けて話さない）・話さずに切る・取り次ぎを待つ（⛔ 2026-10-19 G-57：受付の方の「少々お待ちください」の
        # 報告に、話させる形で返していたので、3本中2本でAIが話した＝相手の言葉の真似・ありもしない用件「マンションの共用部修繕工事の…」を7秒）
        return reply, (bool(last.get("play")) and not last.get("speak")) or bool(last.get("hang_up")) or bool(last.get("silent"))

    # ── 中の名前での入口（handle と試験は、ここを通す）。記録（log）は中の名前で残す
    def apply(self, name: str, args: dict) -> dict:
        before = {qid: it.state for qid, it in self.items.items()}
        if name == "replay_opening" and args.get("part") in self.answers:
            args = {**args, "part": ANSWER_PREFIX + args["part"]}  # 想定問答の問いの名前 ⇒ 録音の部分の名前
        if name not in OPS:
            reply = self._reject(f"その操作はありません（使える操作は {'・'.join(OPS)}）。")
        elif why := self._check_args(name, args, {ANSWER_PREFIX + k for k in self.answers}):
            reply = self._reject(why)
        elif self._asking_now() and not (name == "request_end" or (name == "replay_opening" and args.get("part") != "handover")):
            # 相手がこちらに尋ねている（呼んでいる段の、別の問い合わせ asks）：聞かれたことに答えるのが先（唯一の場所。目的の人・受付の方の
            # 報告・代わった方・答えの報告・かけ直しのどれでも）。⛔ 2026-10-22 G-57 tc18：「この番号はどちらでお知りになりましたか？」に、AIが
            # 答えの報告（record_answer）を呼び、「何も言わずに待ちます」と返したので、質問に答えなかった（3本中2本）
            reply = self._reject(self._asking_note())
        else:
            reply = getattr(self, f"_op_{name}")(args)
        for qid, it in self.items.items():
            # 名前の読みは、認めたか直されたかを相手の言葉でプログラムが決める（✅ 2026-10-14 ユーザー決定 (a)。_reading_denied）＝確かめに当てない
            if it.state in TAKEN and before[qid] != it.state and not self.questions[qid].reading_of:
                # 受け取ったときの材料を残す（遅れて届いた確かめを、同じ決まりで当てる＝_late／届かなかった確かめを数える＝unchecked）。
                # 言い直しへの返事で確かめた項目は、値を聞く型なら「合っている」が受け取った答え（選択肢の型は選択肢）
                q, confirming = self.questions[qid], it.state == "confirmed"
                self.taken[qid] = {"turn": self.callee_turns, "said": self.callee_said, "check": dict(self.check), "confirming": confirming,
                                   "reported": "affirmed" if confirming and q.type not in CHOICE_TYPES else it.condition}
        if self.callback_why:
            # かけ直しの申し出の答え（取れたものだけ。取れていなければ None）。操作のたびに書き直す（締めの前に相手が切っても、取れた分は残る）
            got = {k: self.answer_of(qid) if qid in self.items and self.items[qid].state in GOT else None for k, qid in CALLBACK_IDS.items()}
            self.facts["callback"][-1].update(got)
        if not (name == "replay_opening" and args.get("part") in ("ai", "resume")):
            self.ai_pending = False  # 決まった言い方への返事を待つのは、その次の報告まで（答え・ほかの報告が来たら、もう待っていない）
        reply["ack"] = self._ack(name, args, reply)
        self.log.append({"op": name, "args": args, "reply": reply, "callee_turns": self.callee_turns})
        return reply

    def _ack(self, name: str, args: dict, reply: dict) -> str | None:
        """報告が届いた瞬間に、電話接続側が流す相づち（phrases.ACK の中の名前。流さないなら None）。DESIGN.md 6-15。
        ✅ 2026-10-14 ユーザー：基本は「はい、」。違和感のある場面や「はい」が続くときは、別の相づち。
        - 言い直し（AIの言葉は「ありがとうございます。」などの受けの言葉から始まる）・締め・引き止めの一言 ⇒「はい、」
        - 次の質問・念のための確認 ⇒「承知しました、」「ありがとうございます、」を順番に（言い直しの「はい、」と交互になり、
          「はい」が続かない。AIの言葉は「それでは、」などのつなぎの言葉から始まるので、受けの言葉が2つ重ならない）
        - 相手が「わからない」「答えたくない」と答えた・通話を断られた ⇒「承知しました、」（お礼は場面に合わない）
        - 聞き返しても聞き取れず、先へ進む ⇒「はい、」
        - 言い直した値が違っていた ⇒「失礼いたしました。」／聞き取れなかった ⇒「申し訳ございません、」（お詫びの決まり。6-11）
          「申し訳ございません、」は2本：1回目の聞き返し＝軽いお詫び（moushiwake）／同じ項目を2回目以降に聞く・最後の1回＝重いお詫び
          （moushiwake_heavy）。✅ 2026-10-16 ユーザー：「02だと軽薄に聞こえる」場面（こちらの誤り・クレーム・かけ直し）は重いほう
        - 差し戻し ⇒ 流さない（AIはまだ受け付けられていない報告を出し直す）"""
        result = reply["result"]
        if result in ("rejected", "hold", "wait", "confirm_target"):
            # 差し戻し・取り次ぎを待つ（相手はいま聞いていない）・相手の言葉の途中・目的の人かを確かめる（どの報告から来ても同じ＝確かめる
            # 一言だけ。聞き返しにもお詫びは重ねない＝✅ 2026-10-22 ユーザーの言い方「おかだ様でお間違えなかったでしょうか？」）
            return None
        if result == "ask_back":
            # 1回目の「答えが質問に合わない」は「はい、」（続けてAIが「恐れ入ります。（聞くこと）を、もう一度…」）。
            # 聞き取れなかったとき・2回目からは、申し訳なさそうに読んだ「申し訳ございません、」（✅ 2026-10-14 ユーザー）
            # 最後の1回（「お声が聞き取れませんでしたので、…もう一度だけ…」）は、何回目でも「申し訳ございません、」
            first_form = reply.get("ask_reason") == "form" and reply.get("ask_n") == 1 and not reply.get("ask_last")
            # 午前・午後を聞く：お詫びではなく「はい、」（続けてAIが「恐れ入ります、午前と午後、どちらになりますでしょうか？」）
            if first_form or reply.get("ask_reason") == "ampm":
                return "hai"
            return "moushiwake" if reply.get("ask_n") == 1 and not reply.get("ask_last") else "moushiwake_heavy"
        if result == "confirm_required":
            return AFTER[reply["after"]][2] if reply.get("after") else "hai"  # 何のあとの言い直しかは、言い直しを作る所（_ask_confirm）が決めたもの
        if name == "record_answer" and args.get("condition_id") in SYSTEM_CONDITIONS:
            return "shouchi"
        if name == "request_callback" and result == "next":
            # 「本人は出かけております」「あとでかけて」に「ありがとうございます、」は合わない。代わった方へは、録音のあいさつ・名乗りから始まる
            # ので流さない（_callback の renamed）
            return None if reply.get("renamed") else "shouchi"
        if result == "call_target":
            # 目的の人をお呼びし直す（_call_target）：確かめに「いいえ」と言われたあとは「失礼いたしました。」。まだ確かめていない方（違うお名前を
            # 名乗った方）には、録音のあいさつから始まるので流さない
            return "shitsurei" if reply.get("sorry") else None
        if name == "report_party":
            return None  # 留守番電話・番号違い・受付に「承知しました」などは合わない（番号違いの締めの頭に「失礼いたしました。」を流すと、締めの「失礼」と重なる）
        if name == "replay_opening" and args.get("part") == "unknown":
            return "moushiwake"  # 「申し訳ございません、」＋「私の方では分かりかねます。」（phrases.UNKNOWN_ANSWER）
        if name == "replay_opening" and str(args.get("part")).startswith(ANSWER_PREFIX):
            return "hai"  # 想定問答の答え：聞かれたことに答える頭は「はい、」
        if name == "replay_opening":
            # 名乗りの流し直しの頭は「失礼いたしました。」（6-13）。代わった方へは、録音のあいさつから始まるので相づちは流さない
            # 「AIですか？」への言い方（「何か問題でもございましたでしょうか？」）の頭に、お詫びは重ねない。質問に戻るときは「失礼いたしました。」
            return None if args.get("part") in ("handover", "ai") else "shitsurei"
        if name == "report_unclear":
            return "hai"  # 聞き返しを使い切って先へ進む：聞き取れていないので、「承知しました」「ありがとうございます」は合わない
        if result == "recheck":
            return "moushiwake_heavy"  # 締めの前の念のための確認（最後の1回）：重いお詫び＋「お声が聞き取れませんでしたので…もう一度だけ…」
        if result == "next":
            self.ack_count += 1
            return ("shouchi", "arigatou")[(self.ack_count - 1) % 2]
        if result == "end" and args.get("reason") == "callee_refused_call":
            return "shouchi"
        if result == "end" and args.get("reason") == "callee_complaint":
            # 怒り・苦情：締めの言い方そのもの（「お忙しいところ申し訳ございませんでした。…」）がお詫び。頭に「承知しました、」「はい、」を
            # 流すと、相手の怒りを軽く受け流したように聞こえ、「申し訳ございません、」を流すとお詫びが2回続く ⇒ 流さない
            return None
        return "hai"

    @staticmethod
    def _check_args(name: str, args: dict, answer_parts: set = frozenset()) -> str | None:
        """宣言（OP_ARGS）に合わない報告は、記録を書き換える前に、どこが違うかを言って差し戻す。合っていれば None。
        ⛔ 2026-10-13 G-26・G-28：AIが項目名を書き違えた報告（「与えの返事」）を、そのまま受け取っていた。返事の種類が無いので
           「相手が答えなかった」とされ、相手が「はい」と認めた値が「-」になった（4回。どれも黙って起きた）。
           ⇒ 宣言に無い項目名・要る項目の抜け・選択肢に無い値・文字でない値は、すべて差し戻す。"""
        required, optional = OP_ARGS[name]
        usable = "・".join(ARG_LABEL[k] for k in required + optional)
        for k, v in args.items():
            if k not in required + optional:
                return f"「{k}」という項目は、{name} にはありません（使える項目は {usable}）。"
            if v is not None and not isinstance(v, str):
                return f"「{ARG_LABEL[k]}」は、文字で入れてください。"
        for k in required:
            if not (args.get(k) or "").strip():
                return f"{name} には「{ARG_LABEL[k]}」が要ります（使える項目は {usable}）。"
        for k, table in _ENUM_ARGS.items():
            if k in args and args[k] not in table and not (k == "part" and args[k] in answer_parts):
                return f"{ARG_LABEL[k]}「{table.get(args[k], args[k])}」は選べません（選べるのは {'、'.join(table.values())}）。"
        return None

    @staticmethod
    def _args_in(args: dict) -> dict:
        out = {}
        for k, v in args.items():
            key = _ARG_FROM_LABEL.get(k, k)
            out[key] = _FROM_LABEL.get(key, {}).get(v, v)
        return out

    @staticmethod
    def _reply_out(reply: dict) -> dict:
        return {REPLY_KEY_LABEL.get(k, k): (RESULT_LABEL.get(v, v) if k == "result" else v) for k, v in reply.items()
                if k not in INTERNAL_KEYS}

    # ── 操作
    def _op_record_answer(self, a: dict) -> dict:
        qid = a.get("question_id")
        if self.awaiting_confirm is not None:
            # 2026-10-13 G-16：言い直した値に相手が「はい」と答えたのに、AIが record_answer を呼び、言い直しが繰り返された
            # 2026-10-13 G-16 の2回目：Q2 の言い直しの返事を待っている間に、Q3 の答えとして報告された（同じ扱いにする）
            return self._reject("いまは record_answer では受け付けません。")
        if qid != self.current:
            return self._reject(f"{qid} は、いまの質問ではありません。")
        q, it = self.questions[qid], self.items[qid]
        if (held := self._not_an_answer()):
            return held  # 答えではない言葉への報告は、値の形・曖昧の決まりより先に止める（取り次ぎの間に聞き返さない）
        cond = a.get("condition_id")
        if cond not in q.all_conditions:
            shown = label(cond)
            return self._reject(f"{ARG_LABEL['condition_id']}「{shown}」は、この質問の選択肢にありません（選べるのは {'、'.join(q.labels)}）。")
        value = normalize_value(q.type, a.get("value"), q.unit)
        if cond not in SYSTEM_CONDITIONS and (bad := kana_number(q.type, value)):
            return self._reject(kana_number_note(value, bad))  # お客様は正しく答えている＝AIの書き間違いなので、聞き返さずに書き直させる
        if cond not in SYSTEM_CONDITIONS and (why := form_problem(q.type, value)):
            # 値を聞く質問に「結構です」だけ：答えの値ではないので聞き返す（そのあとにまた言われたら、断りとして締めてよい＝_op_request_end）
            it.ambiguous_asked = it.ambiguous_asked or bool(ambiguous_word(self.callee_said or value))
            return self._bad_form(qid, value, why)
        if value is None and q.type not in CHOICE_TYPES and cond not in SYSTEM_CONDITIONS:
            # ⛔ 2026-10-13 G-25 で見つけた漏れ：値の要る質問で値の無い答えを受け付け、言い直しに「Noneで、よろしいでしょうか。」を
            #    入れていた（確かめない型なら、依頼主へ値の代わりに条件の名前が渡っていた）
            return self._reject(f"答えの値がありません。相手が答えた値を「{ARG_LABEL['value']}」に入れて、もう一度報告してください。")
        if cond not in SYSTEM_CONDITIONS and q.type in AMPM_TYPES:
            value, it.pending_value = merge_ampm(it.pending_value, value), None
            value, ask = fill_ampm(value, self.business_hours)
            if ask and not it.ampm_asked:
                return self._ask_ampm(qid, value)
        if cond in SYSTEM_CONDITIONS and (taken := self._ambiguous_only(f"「{SYSTEM_LABEL[cond]}」")):
            # 相手の言葉が「大丈夫です」「結構です」だけなのに、AIが「わからない」「答えたくない」を選んだ：その言葉だけで決めない
            # ⛔ G-14 の続き：日時の質問への「大丈夫です」を、AIは 6本中2本「わからない」にした（聞き返さずに「-」になる）
            return taken
        it.condition, it.value = cond, value
        said = self.callee_said or value
        if cond in SYSTEM_CONDITIONS:
            why, back = self._checks(q, cond, policy=False)
            if why and q.required and q.type in CHOICE_TYPES and back in q.conditions + (ANY,):
                # ⛔ 2026-10-17：「いや、これからなんです」「家まだです」（聞き取りは正しい）を、AIが「わからない」と報告し、確かめずに受け取っていた
                return self._check_again(qid, why, cond, back)
            if why and q.required and not (back == cond and it.ask_backs):
                # 言い直す選択肢が無い（聞き取りが食い違う・値を聞く型で答えがあったと判定した）：質問から聞き直す（6-11 の回数の中で）。
                # 「わからない」と判定の「決められない」の食い違い（back＝報告のまま）は、最初の答えだけ（2回目の「わからない」は受け取る＝
                # 本当にわからない方に、何度も聞かない）
                return self._ask_again(qid, "unclear", f"{why}（AIは「{label(cond)}」と受け取った）")
            return self._take_system(qid, cond)
        after = self.unreported_correction[1] if self.unreported_correction and self.unreported_correction[0] == qid else None
        self.unreported_correction = None
        # 前の質問から意味が決まる曖昧な言葉（取込で決めた。G-49）：その選択肢を採る（AIの選んだ選択肢と違えば、こちらを採る）
        decided = q.reply_choice(ambiguous_word(said)) if q.type in CHOICE_TYPES else None
        if decided:
            cond = it.condition = decided
        why, back = self._checks(q, cond, policy=not decided, words=said) if q.type in CHOICE_TYPES else (None, None)
        if why:
            # 必須でない項目は、聞き返さない決まり（6-11）に合わせて確かめず「-」
            if not q.required:
                it.set("unresolved", f"{why}（「{said[:20]}」）・必須でないので確かめない")
                return self._advance("「-」にします。")
            return self._check_again(qid, why, cond, back)
        if decided:
            it.set("heard", f"「{said[:20]}」は、この質問では「{cond}」の意味（取込で質問の文から決めた）")
        else:
            it.set("heard", "聞き取れた")
        if q.confirm or it.ask_backs or self.phase == "rechecking" or after:
            return self._ask_confirm(qid, after=after)
        it.set("accepted", "曖昧な言葉だが、前の質問から意味が決まる" if decided else "確かめない型で、はっきり聞き取れた")
        return self._advance("受け付けました。")

    # ── 確かめ（DESIGN.md 1章・6-10）を決める唯一の場所：最初の答えの報告にも、言い直しへの返事の報告にも、遅れて届いた確かめにも、これを当てる
    @staticmethod
    def _compare(q: "Question", said: str, reported: str | None, check: dict, policy: bool = True,
                 confirming: bool = False, words: str | None = None) -> tuple[str | None, str | None]:
        """会話AIの報告（reported。選択肢の型は選んだ選択肢・値を聞く型の言い直しへの返事は affirmed／corrected）を、独立した確かめに当てる：
        (1) 別の耳の文字（check["ear"]。聞き取り。6-10 の (5)。会話AIの聞き取り said と、読みにそろえて比べる）
        (2) 曖昧な答えの決まり（✅ ユーザー確定：「結構です」「大丈夫です」だけ・言い切っていない答えは、その言葉だけで決めない。当てる言葉は
            words＝会話AIの聞き取り、届いていなければAIの報告の値）。「結構です」だけの決まりは policy のときだけ（言い直しへの返事の「大丈夫です」は、
            言い直した意味を認める返事。取込で意味が決まった言葉も当てない）。言い切っていない答え（「たぶん」「かもしれない」）は、答えを選んだ報告と
            「合っている」の報告のすべてに当てる
        (3) 意味の判定（check の JUDGMENTS。届いたものすべて。意味。6-10 の (6)。meaning_judge）
        返り値＝(確かめる理由, 言い直す答え)。理由が None ＝そろった（受け取ってよい）。届いていない確かめは比べない
        （遅れて届けば Flow.on_check が同じ決まりで当てる。最後まで届かなければ通話後の検証＝Flow.unchecked）。
        理由は、聞き取り・決まり・意味の判定の順に書く（判定が増やした確かめを、記録から数えられるように）。
        ⛔ 2026-10-18 に外したもの：頭の言葉（「はい」「いいえ」「いや」）と選んだ答えの食い違いの確かめ（reply_lead）。記録の通話で働いた13回は
           13回とも意味の判定も食い違いを出していた（判定が受け持つ）。打ち消しを混ぜた答え（G-52 の B）では、頭の言葉の付いた82個のうち
           21個で、頭の言葉が正しい答えと逆を指していた（「ご不明な点はございませんでしょうか？」への「はい、ちょっと分からない点があります」）。
           その21個を、判定は19個正しく読んだ＝判定の劣化版なので、外して1つにした。"""
        ear = check.get("ear")
        if bool(said) and ear is not None and not heard_alike(said, ear):
            return f"別の耳の文字（「{ear[:20]}」）と食い違う", reported
        text = (words or said or "").strip()
        if policy and AMBIGUOUS.fullmatch(text):
            return "曖昧な答え", reported
        if reported not in SYSTEM_CONDITIONS and not confirming and UNCERTAIN.search(text):
            # 言い切っていない答えは、その言葉だけで決めない（✅ ユーザー確定）。最初の答えだけに当てる（言い直しへの返事は、言い直した
            # 意味に寄っていれば、その答えに「曖昧」の印を付けて受け取る＝Flow._hedged。✅ 2026-10-18 ユーザー：「yes,but 曖昧」）。
            # 「わからない」「答えたくない」の報告には当てない（「たぶん分からないです」は、そのまま「わからない」）
            return HEDGE_WHY, reported
        for kind in JUDGMENTS:
            back = Flow._meaning_disagrees(q, reported, check.get(kind), confirming)
            if back:
                return f"意味の判定（「{label(check[kind])}」）と食い違う", back
        if reported not in SYSTEM_CONDITIONS and not confirming and Flow._judged_unsure(check):
            return f"{HEDGE_WHY}（意味の判定が、言い切っていないと読んだ）", reported
        return None, None

    @staticmethod
    def _judged_unsure(check: dict) -> bool:
        """意味の判定が、寄っている答えを選んだうえで、言い切っていないと読んだ（meaning_judge.Reading.firm）。"""
        return any(getattr(check.get(k), "firm", True) is False and check.get(k) not in (UNDECIDED, HOLD, HANDOVER)
                   for k in JUDGMENTS)

    @staticmethod
    def _hedged(said: str | None, check: dict) -> bool:
        """言い直しへの返事が、言い直した答えに寄っているが言い切っていない（「〜はずですが」「〜くらいかなとは思いますが」）：
        確かめがそろって受け取るときに、依頼主へ渡す値に「曖昧」の印を付ける（もう一度は聞かない）。言い切っていない答えの決まり（UNCERTAIN）と
        意味の判定の、どちらかが言い切っていないと読めば付ける（印は値を変えない＝誤った値は渡らない）。
        ✅ 2026-10-18 ユーザー：「『急ぎと言えば急ぎですが』…『5万円くらいかなとは思いますが』なんかは、『yes,but 曖昧』のような返答にしておけば
           いいんじゃないですか？」⇒ それまでは、もう1回言い直し、それでも言い切らなければ「-」（寄っている向きが依頼主へ届かなかった）"""
        return bool(UNCERTAIN.search(said or "")) or Flow._judged_unsure(check)

    @staticmethod
    def _meaning_disagrees(q: "Question", reported: str | None, meaning: str | None, confirming: bool = False) -> str | None:
        """意味の判定（meaning）が、会話のAIの報告（reported）と食い違えば、言い直して確かめる答え（判定が選択肢か「どちらでも」を選んだなら
        判定のほう、ほかは会話のAIのほう）。食い違わない・判定が無い ⇒ None。
        - どちらも選択肢を選んでいない（わからない・答えたくない・決められない・取り次ぎ）ときは、食い違いとしない（言い直す答えが無い）
        - 値を聞く型の言い直しへの返事（confirming）：「合っている」と報告したのに、判定が「合っている」と読まなかったときだけ
          （「違う値を言われた」は、直された値をもう一度言い直して確かめるので、食い違っても誤った値は渡らない）"""
        if meaning is None or meaning == reported:
            return None
        if confirming and q.type not in CHOICE_TYPES:
            return reported if reported == "affirmed" else None
        named = meaning in q.conditions or meaning == ANY
        if not named and reported in SYSTEM_CONDITIONS:
            # 「わからない」の報告に、判定が「決められない」（どの選択肢かは決まらないが、わからないとも言っていない）：食い違い＝聞き直す。
            # ⛔ 2026-10-19 G-55：「10名未満ではないです」（従業員数の3択）を、AIは「わからない」と報告し、2つの判定は「決められない」と読んだのに、
            #    確かめずに「わからない」で受け取った（相手は答えの一部を言っていた＝依頼主へ「-」）。G-55 で、AIの「わからない」57回のうち、
            #    判定が「決められない」だったのは2回（これと、本当にわからない1回＝聞き直しが1回増えるだけ）。「答えたくない」には当てない
            #    （二度と聞かない決まり）
            return reported if reported == "unknown" and meaning == UNDECIDED and q.type in CHOICE_TYPES else None
        return meaning if named else reported

    def _checks(self, q: "Question", reported: str | None, policy: bool = True, confirming: bool = False,
                words: str | None = None) -> tuple[str | None, str | None]:
        """いまの相手の発話の確かめ（self.check）で、_compare を当てる。"""
        return self._compare(q, self.callee_said, reported, self.check, policy, confirming, words)

    def _not_an_answer(self) -> dict | None:
        """意味の判定が、相手の言葉は答えではないと決めた（DESIGN.md 6-8）：答え・言い直しへの返事としては受け取らない（唯一の場所。
        答えの報告・言い直しへの返事の報告・代わった方への名乗りの流し直しが使う）。当たらなければ None。
        - 取り次ぎを待つように言われた（HOLD）：相手はいま聞いていないので、AIは黙って待つ（差し戻しではない＝張り替えない。相づちも流さない）。
          代わった方が話し始めたら、名乗りを流し直す（その時機は、判定が HANDOVER と決めた発話で、プログラムが受け付ける）
        - 代わった別の人が話し始めた（HANDOVER）：答えではないので差し戻し、名乗りを流し直してもらう
        ⛔ 2026-10-17 G-50：取り次ぎの場面で、代わった方への名乗りの録音を、相手の「少々お待ちください」の直後に流した通話が約4割
           （代わった方には届かない）。流す時機をAIに任せていた。"""
        said = (self.callee_said or "").strip()[:30]
        then = (f"代わった方が話し始めたら、replay_opening を{ARG_LABEL['part']}「{PART_LABEL['handover']}」で呼びます。" if self.opening
                else "代わった方が話し始めたら、名乗りと用件を短く伝えてから、いまの質問を聞きます。")
        if self.meaning_said == HOLD:
            return self._hold()
        if self.meaning_said == HANDOVER:
            return self._reject(f"相手の言葉（「{said}」）は、代わった方が話し始めた言葉で、答えではありません。" + then.replace("代わった方が話し始めたら、", ""))
        return None

    def _hold(self) -> dict:
        """取り次ぎを待つように言われた：相手はいま聞いていないので、AIは黙って待つ（唯一の返事。差し戻しではない＝張り替えない。相づちも
        流さない）。代わった方が話し始めたら、名乗りを流し直す（その時機は、判定が HANDOVER と決めた発話で、プログラムが受け付ける）。"""
        said = (self.callee_said or "").strip()[:30]
        then = (f"代わった方が話し始めたら、replay_opening を{ARG_LABEL['part']}「{PART_LABEL['handover']}」で呼びます。" if self.opening
                else "代わった方が話し始めたら、名乗りと用件を短く伝えてから、いまの質問を聞きます。")
        self.holding = True
        return {"result": "hold", "silent": True, "message": f"相手は、ほかの人に取り次いでいます（「{said}」）。答えではありません。何も言わずに、"
                                             f"代わった方が話し始めるのを待ってください。{then}"}

    def _confirm_again(self, qid: str, why: str, reported: str | None, back: str | None) -> dict:
        """言い直しへの返事の確かめがそろわない：もう1回だけ言い直して確かめる（訂正のあとの復唱と同じ回数＝after_correction。選択肢の型は、
        判定が選択肢を選んでいればその意味で）。使い切った・必須でない・言い直す選択肢が無い ⇒「-」（必須なら締めの前の念のための確認で1回）。"""
        it, q = self.items[qid], self.questions[qid]
        self.awaiting_confirm = None
        if q.required and it.corrections < self.limits.after_correction and (q.type not in CHOICE_TYPES or back in q.conditions + (ANY,)):
            it.corrections += 1
            return self._check_again(qid, why, reported, back)
        it.set("unresolved", f"{why}（言い直しへの返事「{(self.callee_said or '')[:20]}」・AIは「{label(reported)}」と受け取った）・確かめがそろわない")
        return self._advance("「-」にします。")

    def _check_again(self, qid: str, why: str, cond: str | None, back: str | None) -> dict:
        """確かめがそろわなかった答えを、そのまま受け取らずに言い直して確かめる（選択肢の型は back の意味で＝判定が選択肢を選んでいれば判定のほう）。"""
        it, q = self.items[qid], self.questions[qid]
        it.set("heard", f"{why}（「{(self.callee_said or '')[:20]}」）・AIは「{label(cond)}」と受け取った")
        if q.type in CHOICE_TYPES and back is not None:
            it.condition = back
        it.ambiguous_asked = True
        # 言い方は、確かめるきっかけ（言い切っていない答えの決まり・判定との食い違い）ではなく、お客様の言葉が言い切っているかで決める
        # （⛔ 2026-10-18 に見つけた漏れ：AIが「わからない」と報告し、判定が「していない」と読んだ「たぶんしてないと思います」は、
        #    食い違いがきっかけなので、言い切っていない答えの言い方にならなかった）
        hedged = why.startswith(HEDGE_WHY) or Flow._hedged(self.callee_said, self.check)
        return self._ask_confirm(qid, after="hedged" if hedged else "ambiguous")

    def _take_system(self, qid: str, cond: str) -> dict:
        """相手が「わからない」「答えたくない」と答えた（答えの報告と、言い直しへの返事の、どちらからも来る）。"""
        it = self.items[qid]
        it.condition = cond
        if cond == "refused":
            it.set("refused", "相手が答えを拒んだ")
            return self._advance("受け付けました（答えたくない）。この質問は二度としません。")
        it.set("dont_know", "相手が「わからない」と答えた")
        return self._advance("受け付けました（わからない）。")

    def _op_report_unclear(self, a: dict) -> dict:
        qid = a.get("question_id")
        if self.awaiting_confirm is not None and self.awaiting_confirm == qid and self.questions[qid].reading_of:
            # 名前の読みの確かめは、相手の答えの言い直しではなく、こちらから聞く質問（相手はまだ何も答えていない）。その返事が聞き取れない
            # ときは、ほかの質問と同じく聞き取れないとして扱う（必須でないので聞き返さず、名前を使わずにお呼びする）。⛔ 試験で見つけた漏れ：
            # 言い直しの返事待ちとして report_unclear を差し戻し続け、聞き取れない相手で流れが進まなかった（NeverStuckTest）
            self.awaiting_confirm = None
            return self._ask_again(qid, "unclear", "聞き取れない・曖昧")
        if self.awaiting_confirm is not None:
            return self._reject("いまは report_unclear では受け付けません。")
        if taken := self._ambiguous_only("聞き取れない"):
            return taken
        if qid != self.current:
            return self._reject(f"{qid} は、いまの質問ではありません。")
        q, it = self.questions[qid], self.items[qid]
        return self._ask_again(qid, "unclear", "聞き取れない・曖昧")

    def _op_confirm_field(self, a: dict) -> dict:
        qid = a.get("question_id")
        if self.awaiting_confirm is None and qid == self.current:
            # ⛔ 2026-10-14 G-34（2回）：AIが答えを報告せずに自分で言い直し、相手の返事を confirm_field で報告してきた。前の差し戻しは
            #    「相手が答えたら record_answer で」としか言わず、相手がもう答えているのに AIが待つと読めた。1回は報告し直さず、次の質問の
            #    答え「3人」が日時の答えとして記録され、確かめまで済んだ。⇒ 答えがまだ報告されていないことと、報告する値を、はっきり言う
            v = normalize_value(self.questions[qid].type, a.get("correction"), self.questions[qid].unit)
            self.unreported_correction = (qid, correction_kind(self.callee_said)) if a.get("result") == "corrected" else None
            shown = f"（「{v}」）" if v else ""
            return self._reject(f"{qid} の答えは、まだ record_answer で報告されていません。相手が答えた値{shown}を、いま record_answer で"
                                "報告してください。言い直しの言い方は、プログラムの返事のとおりにします。")
        if self.awaiting_confirm != qid and a.get("result") == "corrected" and self._affirmed_in_this_turn(qid):
            # ⛔ 2026-10-14 G-36 の4回目：相手が「あ、すみません、間違えました。16日の…」と言い始めた「あ」の時点で、AIが「合っている」と
            #    報告し、0.7秒後に「違う値を言われた（16日）」と報告し直した。制御は先の報告で次の質問へ進んでいたので、あとの報告を
            #    差し戻し、古い値（15日）で確かめが済んだ。⇒ 同じ相手の発話への報告を、AIが「違う値を言われた」に直したら、あとの報告を採る
            #    （先の確かめを取り消し、流れの位置も戻してから、ふつうの訂正として扱う）
            self._reopen()
        if self.awaiting_confirm != qid:
            return self._reject(f"いまは、{qid} の言い直しを求めていません。")
        if self.callee_turns <= (self.confirm_requested_at or 0):  # I-24：相手の返事が届く前の報告は差し戻す
            return self._reject("相手の返事をまだ聞いていません。言い直したら、相手が返事をするまで待ってから報告してください。")
        if (held := self._not_an_answer()):
            return held
        it = self.items[qid]
        result = a.get("result")
        correction = normalize_value(self.questions[qid].type, a.get("correction"), self.questions[qid].unit)
        q = self.questions[qid]
        if q.reading_of and result == "affirmed" and self._reading_denied(q):
            # 読みの違いは AIには聞き分けられない（G-45）ので、認めたか直されたかは、相手の言葉でプログラムが決める
            # （値を直されたときの直され方を相手の言葉で決めるのと同じ。✅ 2026-10-14 ユーザー決定 (a)）
            result, correction = "corrected", None
            it.set(it.state, f"相手が「{self.callee_said[:20]}」と直したのに、AIは「合っている」と報告した ⇒ 直されたとして扱う")
        if q.reading_of and correction:
            correction = hira(re.sub(r"[\s様さん。、]", "", correction))
            if result == "corrected" and not _KANA.fullmatch(correction):
                return self._reject(f"「{q.reading_of}」の読みは、相手が言ったとおりの音を、ひらがなだけで"
                                    f"{ARG_LABEL['correction']}に入れて、もう一度報告してください（「{correction}」は使えません）。")
        if q.reading_of and result == "corrected":
            # ⛔ G-46 で試して外したもの：AIの報告が使えないときに、候補を並べて AI に選ばせる段。名前だけで「カドタです。」と直された場面で、
            #    並べる順を「かくた・かどた」にすると5本中1本、逆にすると5本中4本＝AIは聞いて選ばず、並びの先を取る。候補を見せずに
            #    聞こえた音を書かせても5本中1本（「つのだ」3回・「たばた」1回）。⇒ 次に確かめる読みは next_reading が決め、
            #    外れていれば相手が直してくれる（下の reading_tries まで）
            # 次に確かめる読みは、next_reading の1か所が決める（AIは濁りの違いを聞き分けられない＝G-45・G-46）。
            # 無ければ確かめをやめて、名前を使わずにお呼びする
            heard, correction = correction, next_reading(correction, q.readings, it.tried_readings)
            if correction is None:
                self.awaiting_confirm = None
                it.set("unresolved", "相手が読みを直したが、ほかの読みの候補が無い")
                return self._advance("「-」にします。" + self._reading_note(qid))
            if correction != heard:
                it.set(it.state, f"AIの聞いた読み「{heard or '（無し）'}」は使わず、候補の「{correction}」で確かめる（濁りの違いは聞き分けられない）")
        if q.type in CHOICE_TYPES and result == "corrected" and correction:
            # 選択肢の質問の訂正は、選択肢の名前で受ける。⛔ 2026-10-17 に見つけた穴：訂正の言葉（「まだです」）を値に入れるだけで、選択肢は
            #    前のまま（「はい」）だった＝相手が打ち消したのに、依頼主へ「はい」が渡る形だった（依頼主へ渡すのは選択肢＝export）
            chosen = q.condition_of(correction)
            if chosen is None:
                return self._reject(f"{qid} は選択肢の質問です。{ARG_LABEL['correction']}には、相手の答えに合う選択肢の名前"
                                    f"（{'、'.join(q.labels)}）を入れて、もう一度報告してください。")
            if chosen in SYSTEM_CONDITIONS:
                why, back = self._checks(q, chosen, policy=False, confirming=True)
                if why:
                    return self._confirm_again(qid, why, chosen, back)
                self.awaiting_confirm = None
                return self._take_system(qid, chosen)
            correction = chosen
        if result == "corrected" and correction is None:  # 訂正の値の無い報告は、記録を書き換える前に差し戻す（上と同じ漏れ）
            return self._reject(f"訂正された値がありません。相手が言い直した値を「{ARG_LABEL['correction']}」に入れて、もう一度報告してください。")
        if result == "corrected" and not q.reading_of and (bad := kana_number(q.type, correction)):
            return self._reject(kana_number_note(correction, bad))  # 言い直しの返事待ちのまま、書き直させる
        # 名前の読みの訂正は、候補から選んだ読み（next_reading）なので、値の形の確かめに当てない（「井伊（いい）」を返事の言葉と取り違えない）
        if result == "corrected" and not q.reading_of and (why := form_problem(self.questions[qid].type, correction)):
            self.awaiting_confirm = None  # 言い直しの返事待ちをやめて、質問から聞き直す（6-2）
            return self._bad_form(qid, correction, why)
        if result in ("corrected", "affirmed") and correction and self.questions[qid].type in AMPM_TYPES:
            correction, ask = fill_ampm(correction, self.business_hours)
            if ask and result == "corrected" and not it.ampm_asked:
                self.awaiting_confirm = None
                return self._ask_ampm(qid, correction)
        self.awaiting_confirm = None
        # 選択肢の型は、言い直すのが選択肢の意味の言葉なので、添えられた値で別の値を言い直す形（G-36）には当てない
        if result == "affirmed" and q.type not in CHOICE_TYPES and (other := other_value(q.type, it.value, correction)):
            # ⛔ 2026-10-14 G-36：相手が値を直したのに、AIが訂正を報告せずに自分で新しい値を言い直し、相手の「はい」を「合っている」と
            #    報告した（訂正された値の欄には新しい値）。制御は、待っていた古い値への返事として受け取り、古い値で確かめを済ませた
            #    （日時＝15日のまま。相手は16日と言っていた）。⇒ 古い値で済ませず、添えられた値を、プログラムの言い方で念のため言い直して
            #    確かめる（相手にもう一度「はい」をもらう。違う値が確かめ済みになるよりよい）
            if it.corrections < self.limits.after_correction:
                it.corrections += 1
                it.value = other
                it.set("heard", "AIが報告せずに言い直した値に、相手が肯定した（念のため言い直す）")
                return self._ask_confirm(qid, after="unreported")
            it.set("unresolved", "訂正のあとも合わない")
            return self._advance("「-」にします。")
        if result == "affirmed" and not q.reading_of:
            # 言い直しへの返事も、最初の答えと同じ確かめに当てる（DESIGN.md 1章）。名前の読みは、相手の言葉でプログラムが決める（_reading_denied）
            reported = it.condition if q.type in CHOICE_TYPES else "affirmed"
            why, back = self._checks(q, reported, policy=False, confirming=True)
            if why:
                return self._confirm_again(qid, why, reported, back)
        if result == "affirmed":
            if not q.reading_of and Flow._hedged(self.callee_said, self.check):
                it.hedged = (self.callee_said or "").strip()[:40]
            it.set("confirmed", f"相手が肯定した（言い切っていない：「{it.hedged}」）" if it.hedged else "相手が肯定した")
            self.last_affirmed = {"qid": qid, "callee_turns": self.callee_turns, "phase": self.phase, "current": self.current,
                                  "recheck_queue": list(self.recheck_queue), "confirm_requested_at": self.confirm_requested_at,
                                  "awaiting_say": self.awaiting_say}
            return self._advance("確認できました。" + self._reading_note(qid))
        more = (len(it.tried_readings) < self.limits.reading_tries) if q.reading_of else (it.corrections < self.limits.after_correction)
        if result == "corrected" and more:
            it.corrections += 1
            it.value = correction
            if q.type in CHOICE_TYPES:
                it.condition = correction
            kind = correction_kind(self.callee_said)
            if kind == "misheard" and it.corrections >= 2:
                kind = "misheard_again"  # 同じ項目を2度聞き違えた＝こちらの誤りが重なった ⇒ 重いお詫び（✅ 2026-10-16 ユーザー：こちらの誤りは 04）
            it.set("heard", "相手が訂正した（こちらの聞き違い）" if kind.startswith("misheard") else "相手が訂正した（相手の言い間違い）")
            return self._ask_confirm(qid, after=kind)
        it.set("unresolved", "訂正のあとも合わない" if result == "corrected" else "相手が答えなかった")
        return self._advance("「-」にします。" + self._reading_note(qid))

    def _reading_denied(self, q: "Question") -> bool:
        """名前の読みの確かめへの相手の返事（会話AIの聞き取りの文字）が、直す返事か：打ち消しの言葉で始まる（「いや、中島です。」）、
        または認める言葉で始まらずに名前を言い直している（「ナカシマです。」）。文字が届いていなければ、AIの報告のまま。"""
        said = re.sub(r"^[\s、。]+", "", self.callee_said or "")
        if not said:
            return False
        if DENIAL.match(said):
            return True
        named = q.reading_of in said or any(r in hira(said) for r in q.readings)
        return named and not AFFIRM.match(said)

    def _reading_note(self, qid: str) -> str:
        """名前の読みの確かめが済んだ（または確かめられなかった）ときに、AIへ伝えること。それ以外の質問では空。"""
        q, it = self.questions[qid], self.items[qid]
        if not q.reading_of:
            return ""
        if it.state == "confirmed":
            return f"このあと「{q.reading_of}」様は「{it.value}」様とお呼びします。"
        # 確かめられなかった：読み違えたままお呼びしない（✅ 2026-10-16 ユーザー：「失礼のないように語る」）
        return f"「{q.reading_of}」様の読みは確かめられませんでした。このあとは、お名前を使わずに「ご担当者様」のようにお呼びします。"

    def readings(self) -> dict[str, str]:
        """会話の中で相手に確かめていただいた名前の読み（表記 ⇒ ひらがな）。依頼主へ渡す値・次にかけるときに使う。"""
        return {q.reading_of: self.items[q.id].value for q in self.questions.values()
                if q.reading_of and self.items[q.id].state == "confirmed"}

    def _affirmed_in_this_turn(self, qid: str) -> bool:
        """qid の確かめが、いまの相手の発話への報告で済んだばかりか（そのあと相手がまだ話していない）。"""
        la = self.last_affirmed
        return bool(la) and la["qid"] == qid and la["callee_turns"] == self.callee_turns and self.items[qid].state == "confirmed"

    def _reopen(self) -> None:
        """「合っている」で済んだ確かめを取り消し、言い直しの返事を待つ位置に戻す（_affirmed_in_this_turn のときだけ）。"""
        la, self.last_affirmed = self.last_affirmed, None
        self.items[la["qid"]].set("checking", "同じ相手の発話への報告を、AIが「違う値を言われた」に直した（先の確かめを取り消す）")
        self.phase, self.current, self.recheck_queue = la["phase"], la["current"], la["recheck_queue"]
        self.awaiting_confirm, self.confirm_requested_at, self.awaiting_say = la["qid"], la["confirm_requested_at"], la["awaiting_say"]

    def _op_replay_opening(self, a: dict) -> dict:
        """名乗りを録音で流し直す（DESIGN.md 6-13。✅ 2026-10-13 ユーザー：「名乗り直しも録音で流す」）。AIは部分を選ぶだけで、自分では言わない。
        ⛔ G-22・G-29：AIが自分で名乗り直すと、社名が正しく聞こえない（24本中、2通りの耳の両方で正しく聞こえたのは3本）。
        流すもの：相づち「失礼いたしました。」（_ack）→ 選んだ部分の録音 → いまの質問の録音（答えを待っている質問があれば）。
        言い直しの返事待ちのときは、録音のあとにAIが言い直しをもう一度言う（その言い方は値で変わるので録音にできない）。"""
        part = a.get("part")
        if part == "unknown" or part.startswith(ANSWER_PREFIX):
            return self._answer(part)
        if part == "ai":
            return self._ai_question()
        if part == "resume":
            return self._resume()
        if part == "handover" and self.phase == "calling":
            # 代わった方が目的の人かは、目的の人の報告と同じ1か所で決める（⛔ 2026-10-19 G-57 の間を長くした場面：代わった方の「お電話
            # 変わりました。岡田です。」を判定が「ご本人」と読んだのに、名乗りの流し直しの頼みでは判定を見ずに、目的の人かを聞き直した）。
            # 名乗りの録音が無い通話（AIが話す形）でも同じ（⛔ 2026-10-21 の見直しで見つけた漏れ：決まりの文は「代わった方へ」で呼ぶように
            # 言うのに、録音が無い通話ではその部分を宣言せず、呼ばれても差し戻していた＝代わった方の道が止まる）
            return self._calling_reported("handover")
        if not self.opening:
            return self._reject("この通話では、名乗りの録音はありません。聞かれたことに、自分で短く答えてください。")
        if part == "handover" and self.meaning_said == HOLD:
            return self._not_an_answer()
        if self.replays >= self.limits.replays:
            return self._reject(f"名乗りの録音は、もう{self.replays}回流しました（同じ録音を何度も流しません）。聞かれたことに、自分の言葉で短く答えてください。")
        keys = {"intro": ["intro"], "purpose": ["purpose"], "all": ["intro", "purpose"], "handover": ["greeting", "intro", "purpose"]}[part]
        # まだ届いていない名乗り・用件も一緒に流す（on_opening_cut）。あいさつは足さない（頭にお詫びの相づちが流れるので、重ねない）。
        # 目的の人に繋がる前（calling）は、用件は聞かれたときだけ流す（DESIGN.md 4-3：目的の人ではない方には、聞かれたら用件を言う）
        calling = self.phase == "calling"
        if calling and part == "all":
            keys = ["intro"]
        keys = [k for k in OPENING_ORDER if self.opening.get(k)
                and (k in keys or (k != "greeting" and k in self.opening_unheard and not (calling and k == "purpose")))]
        if not keys:
            return self._reject(f"「{PART_LABEL[part]}」の録音はありません。聞かれたことに、自分で短く答えてください。")
        self.replays += 1
        self.opening_unheard = [k for k in self.opening_unheard if k not in keys]
        play = [{"kind": "opening", "part": k, "text": self.opening[k]} for k in keys]
        return self._continue(play, "".join(self.opening[k] for k in keys))

    def _answer(self, part: str) -> dict:
        """相手の質問に、決まった答えで答える（唯一の場所。呼んでいる段でも、答えの段でも）：想定問答の答え（業務設定。Intent.md 5-3 (2)）か、
        答えを持っていない問いへの一言（「申し訳ございません、」＋「私の方では分かりかねます。」＝phrases.UNKNOWN_ANSWER）。AIは選ぶだけで、
        自分の言葉では答えない（作る・約束する・名前を言い崩すのを防ぐ）。録音で流す通話は録音、AIが話す通話はその文のとおりに言わせる。
        答えたあとは、いまの位置へ戻る（_continue：呼んでいる段なら、その方にまだ2回呼んでいなければもう一度お呼びする・答えの段なら
        いまの質問）。同じ答えは1通話に2回まで。
        ✅ 2026-10-22 ユーザー：「想定の質問ならそれでいいけど、録音や想定にない質問だった場合は？」「必ずどんな営業トークにも出てくるパターンは
           システム側に録音しておいた方がよくない？」⛔ G-57 tc18：AIは「この番号はどちらでお知りになりましたか？」に答えず（3本中2本）、
           自分の言葉で答えた回は、目的の人の名前をAIの声で言った"""
        unknown = part == "unknown"
        name = part[len(ANSWER_PREFIX):]
        text = UNKNOWN_ANSWER if unknown else self.answers.get(name)
        if text is None:
            return self._reject(f"「{name}」の答えはありません（{self._answer_choices()}）。")
        self.answered[part] = self.answered.get(part, 0) + 1
        if self.answered[part] > 2:
            return self._reject("同じ答えを、もう2回流しました（同じ答えを何度も流しません）。何も言わずに、相手が話すのを待ちます。")
        self._note("answered", part=part)
        if unknown and self.play_recordings:
            return self._continue([{"kind": "recorded", "id": "unknown", "text": text}], text)
        if not unknown and self.opening and self.opening.get(part):
            return self._continue([{"kind": "opening", "part": part, "text": text}], text)
        return {"result": "replayed", "say": text, "speak": True,
                "message": f"「{text}」とだけ言います（言葉を足しません）。そのあと、{self._now()}"}

    def _answer_choices(self) -> str:
        """相手の質問に答える部分の一覧（AIへの知らせの文。_asking_note・_answer の1か所）。"""
        faq = "".join(f"・「{n}」" for n in self.answers)
        return (f"誰からの電話か＝「{PART_LABEL['intro']}」・何の用件か・こちらとの関係・営業の電話か＝「{PART_LABEL['purpose']}」"
                + (f"・想定問答にある問い{faq}" if faq else "") + f"・それ以外の問い（こちらが答えを持っていない問い。番号をどこで知ったか・"
                f"金額・社内のことなど。用件では答えません）＝「{PART_LABEL['unknown']}」")

    def _continue(self, play: list, shown: str) -> dict:
        """録音（名乗りの流し直し・お詫びの相づち）を流したあと、いまの位置へ戻る返事（唯一の場所。_op_replay_opening・_resume）。
        いまの位置＝目的の人を呼んでいる段（もう一度だけ呼ぶ）／言い直しの返事待ち（AIが言い直しをもう一度言う）／答えを待っている質問
        （その質問の録音）／聞いている質問が無い（相手が話すのを待つ）。shown＝流した録音の文（空なら、流したのはお詫びの相づちだけ）。"""
        done = f"「{shown}」は、プログラムが録音で流しました（相手にもう聞こえています）。" if shown else ""
        if self.phase == "calling":
            # 目的の人に繋がる前：名乗り・用件のあとに、もう一度だけ呼ぶ一言（DESIGN.md 4-3）。同じ方に3回目は呼ばない（同じお願いを繰り返さない）
            if self._calls_to_speaker() < 2:
                self.target_called.append(self.callee_turns)
                play.append({"kind": "opening", "part": "target", "text": self.opening["target"]})
                done = f"「{shown}{self.opening['target']}」は、プログラムが録音で流しました（相手にもう聞こえています）。"
            return {"result": "replayed", "play": play, "silent": True, "message": f"{done}{self._now()}"}
        if self.awaiting_confirm is not None:
            # 言い方（say）も渡す：AIの声が外れたかを見張る（offscript。渡した言い方のあるターンだけを見張る）
            return {"result": "replayed", "play": play, "speak": True, "say": self.awaiting_say,
                    "message": f"{done}続けて、言い直し「{self.awaiting_say}」をもう一度言って、相手の返事を待ってください（前に言葉を足しません）。"}
        q = self.questions.get(self.current) if self.current else None
        if q is not None and self.play_recordings and not q.reading_of:
            play.append(question_play(list(self.questions.values()), q.id))
            return {"result": "replayed", "play": play,
                    "message": f"「{shown}{play[-1]['text']}」は、プログラムが録音で流しました（相手にもう聞こえています）。何も言わずに、"
                               "相手の答えを待ってください。相手が答えたら record_answer で報告します。"}
        if q is not None:
            return {"result": "replayed", "play": play, "speak": True,
                    "message": f"{done}続けて、いまの質問 {q.id}「{q.text}」を聞いてください（名乗りは繰り返しません）。"}
        # 聞いている質問が無い（質問が全部済んで締めの最中など）。⛔ 2026-10-17 の見直しで見つけた漏れ：前は、どの場面でも
        #    「相手の答えを待って record_answer で報告」と返していた＝答える質問が無いのに、報告を求めていた
        return {"result": "replayed", "play": play, "silent": True,
                "message": f"{done}何も言わずに、相手が話すのを待ちます。相手が話したら、それに答えます（名乗りは繰り返しません）。"}

    def _ai_question(self) -> dict:
        """「AIですか？」「機械ですか？」「人間ですか？」と聞かれたとき（Intent.md 9-4。唯一の場所）。
        ✅ 2026-10-19 ユーザー：AIだとは伝えない ⇒ 決まった言い方「何か問題でもございましたでしょうか？」（業務設定 ai_answer。無ければ
           phrases.AI_ANSWER）を流し、相手の返事を待つ。打ち消しの「いえ、」は付けない。相手が問題は無いと答えたら、お詫びの相づちのあと、
           いまの位置へ戻る（_resume）。
        重ねて聞かれた（2回目）：✅ 2026-10-19 ユーザー：終えるのは「あまりにも失礼だし、AIだと認めているのと同じ」⇒ 終えずに、決まった言い方
           （phrases.AI_ASKED_AGAIN＝不安にさせたお詫びと、最後まで正確に案内する約束。録音）を言って、いまの位置へ戻る（_continue）。
        3回目から：✅ 2026-10-19 ユーザー：「2回目だろうと、3回目だろうと、相手に不信感を与えることに変わりないでしょ」⇒ 何回聞かれても終えない。
           「いかがなさいましたか？」と聞いて、1回目と同じく相手の返事を待つ（DESIGN.md 6-2）。4回目からは「どうなさいましたか？」と交互に
           （phrases.SILENCE_PROMPT。考えている相手に聞く一言と同じ、承認済みの言い方。同じ言い方を続けない）。
        ⛔ 前の形（3回目は丁寧に終える）は、この決定でやめた。その前の形（「はいエーアイの音声でお電話しております。」のあと、いまの質問の
           録音）も、10-19 の決定でやめた。"""
        self.ai_asks += 1
        self._note("ai_question")
        if self.ai_asks == 2:
            self.ai_pending = False
            # ⛔ 2026-10-19 G-57（三度聞かれる）：3回目に、AIが操作を呼ばずに、この言い方をまねて自分で答えた（6本中6本）⇒ 毎回呼ぶことを添える
            again = f"また聞かれたら、自分では答えずに、もう一度 replay_opening を「{PART_LABEL['ai']}」で呼びます。"
            if self.play_recordings:
                r = self._continue([{"kind": "recorded", "id": "ai_again", "text": AI_ASKED_AGAIN}], AI_ASKED_AGAIN)
                return {**r, "message": f"{r['message']}{again}"}
            r = self._continue([], "")
            return {**r, "say": AI_ASKED_AGAIN, "speak": True,
                    "message": f"「{AI_ASKED_AGAIN}」とだけ言い（言葉を足しません）、続けて、{r['message']}{again}"}
        self.ai_pending = True
        if self.ai_asks == 1:
            text = self.opening.get("ai") or AI_ANSWER
            play = {"kind": "opening", "part": "ai", "text": text}
        else:
            k = (self.ai_asks - 3) % len(SILENCE_PROMPT)
            text = SILENCE_PROMPT[k]
            play = {"kind": "recorded", "id": SILENCE_PROMPT_IDS[k], "text": text}
        self.ai_said = text
        then = self._now()
        if self.play_recordings:
            return {"result": "ai_question", "play": [play],
                    "message": f"「{text}」は、プログラムが録音で流しました（相手にもう聞こえています）。{then}"}
        return {"result": "ai_question", "say": text, "message": f"「{text}」とだけ言います（言葉を足しません）。そのあと、{then}"}

    def _resume(self) -> dict:
        """決まった言い方（「何か問題でもございましたでしょうか？」）に、相手が問題は無いと答えた（「いえ」「別に」「機械みたいだったので」）：
        お詫びの相づち「失礼いたしました。」（_ack）のあと、いまの位置へ戻る（_continue）。"""
        if not self.ai_pending:
            return self._reject(f"「{PART_LABEL['resume']}」は、エーアイかと聞かれて、決まった言い方を流したあとの返事にだけ使います。")
        self.ai_pending = False
        return self._continue([], "")

    def _op_report_party(self, a: dict) -> dict:
        """相手の種別の報告（DESIGN.md 4-3・6-2）。人ではない相手（留守番電話・自動音声の案内・FAX）には話さずに切る（留守番電話は、業務設定の
        伝言があれば残してから切る）。番号違いは、断られたときと同じ決まった締めで終える（業務の判定は範囲外＝7-1 の順2）。受付・目的の人は
        記録して続ける（目的の人に繋がってから質問を始める流れは、目的の人のある業務の流れ）。
        ⛔ 2026-10-18 の見直しで見つけた漏れ：相手の種別を報告する手立てが無く、留守番電話の案内・自動音声にも、AIは質問を続けるか、
           自分で何かを言うしかなかった（Intent.md 9-4 の「伝言を残すか残さないかを設定で選ぶ」に届かない）"""
        return self._party(a["party"], by="ai")

    # ── 音・回線の信号で、相手の種別が分かった（DESIGN.md 4-3 の優先順位の1。電話接続側が、会話AIに聞かせる前に呼ぶ）。
    #    ⛔ 2026-10-19 G-57：会話AIは、ファクスの答える音を聞いても3本とも何も報告せずに黙った（audio.is_fax_tone で見分ける）
    # ── AIの声が、渡した言い方から外れた（電話接続側の見張りが止めた。DESIGN.md 6-7。offscript.off_script）
    def expected_say(self, since: int = 0) -> str | None:
        """いまAIに言わせている決まった言い方：since 番目より後の記録のうち、いちばん新しい返事の「言い方」（無ければ None＝見張らない）。
        AIが自由に答えるターン（相手の質問への答えなど）は、渡した言い方が無いので見張らない。"""
        return self.log[-1]["reply"].get("say") if len(self.log) > since else None

    def on_off_script(self, expected: str, heard: str = "content") -> dict:
        """外れた言葉を止めたあと：新しいつなぎに、相手に聞こえていたもの（heard＝offscript.SpeechGate.heard の kind）に合わせた言い方を言わせる
        （何も聞こえていない＝渡した言い方のまま／受けの言葉だけ＝「それでは、…」／外れた中身まで＝「失礼いたしました。改めまして、…」。
        人が言い間違えて、少し間を置いて言い直すのと同じ形。offscript.restate。お詫びを録音にしない理由も同じ所）。✅ 2026-10-19 ユーザー：「違和感のない形にしないと、AIや機械だと勘繰られますよ。
        その設計が必要でしょう」。流れの位置（言い直しの返事待ちなど）は変えない（言う中身は同じ）。"""
        from offscript import restate
        say = restate(expected, heard)
        stopped = {"none": "相手に流す前に止めました（相手には何も聞こえていません）", "opener": "受けの言葉のあとで止めました",
                   "content": "途中で止めました"}[heard]
        reply = {"result": "restate", "ack": None, "say": say,
                 "message": f"［プログラムからのお知らせ］さきほどの言葉は、渡した言い方と違ったので、プログラムが{stopped}。"
                            f"「{say}」とだけ言って（言葉を足しません）、相手の返事を待ちます。"}
        self.log.append({"op": "signal", "args": {"off_script": expected, "heard": heard}, "reply": reply, "callee_turns": self.callee_turns})
        return reply

    def on_signal(self, party: str) -> dict:
        assert party in NOT_A_PERSON, party
        reply = self._party(party, by="signal")
        self.log.append({"op": "signal", "args": {"party": party}, "reply": reply, "callee_turns": self.callee_turns})
        return reply

    def _party(self, party: str, by: str) -> dict:
        """相手の種別が分かったときの扱いの唯一の場所（AIの報告 report_party と、音・回線の信号 on_signal の両方）。by＝どちらで分かったか。"""
        self.party = party
        self._note("party", party=party, by=by)
        if party == "voicemail" and self.voicemail_message:
            # ✅ 2026-10-19 ユーザー：「依頼者ごとに設定で選べる形にして、必要なら録音をしましょう」⇒ 伝言は依頼主の単位の録音（名乗りの部分 voicemail）
            #    依頼主の録音が無い（名乗りを録音で流さない通話）ときは、AIが設定の文のまま言う
            return self._close("voicemail", text=(self.voicemail_message, "voicemail"), part=True, asked="voicemail" not in self.opening)
        if party in NOT_A_PERSON:
            return self._hang_up(party)
        if party == "wrong_party":
            return self._close("wrong_party")
        if self.phase == "calling":
            return self._calling_reported(party)
        if self.callback_why and self.target and party == "target":
            return self._target_back()
        if self.callback_why and party == "reception":
            return self._hold()  # かけ直しの質問の途中で、取り次がれた・待つように言われた：答えではないので黙って待つ
        if party == "target" and self.target and self.opening and all(it.state == "empty" for it in self.items.values()):
            # 繋いだあと、まだ何も答えていないうちに、また目的の人が出た＝前の「目的の人」は取り次いだ方だった（AIが早く報告した）か、
            # 人が替わった ⇒ 代わった方への流し直しと同じ（あいさつ・名乗り・用件・いまの質問）。⛔ 2026-10-19 G-57 の4回目：会話AIの聞き取りが
            # 「岡田ですね。」で切れ（続きの「少々お待ちください。」は次の発話に入った）、AIが受付の方を目的の人と報告して用件と質問を流し、
            # 代わった岡田様には名乗りも用件も流れなかった（その場の判定なしで流した回）
            return self._op_replay_opening({"part": "handover"})
        return {"result": "noted", "message": f"受け付けました。{self._now()}"}

    def _calling_reported(self, ai: str, why: str | None = None) -> dict:
        """呼んでいる段で、電話口の方が誰かを決める唯一の場所（DESIGN.md 4-3。AIの報告を、独立した判定に当てる＝1-1 の決まりを目的の人にも）。
        ai＝AIの読み：target（report_party「目的の人」）／handover（replay_opening「代わった方へ」＝代わった方が名乗らない・違うお名前を
        名乗った・確かめに「いいえ」と答えた）／reception（report_party「受付の方」＝お取り次ぎいただく・待つように言われた）／
        away（request_callback＝お呼びした方はいま出られない。why＝AIが選んだかけ直しの理由）。
        決める順（⛔ 2026-10-21 ユーザー：「二つ以上揃わなかった場合に関しても正しく設計できているんですか？」で見直した。前は、確かめを
        使い切ると判定に関係なく繋ぎ（不在・取り次ぎと読んでいても）、確かめは通話ごとに数え、確かめに「いいえ」と言われたときの道が無く、
        判定が遅れると同じ方を2回確かめ、受付の方の報告は判定に当てていなかった）：
        (1) 相手の言葉が出揃っていない（判定を使う通話）⇒ 決めずに黙って待つ（_wait_for_words）
        (1-b) 相手がこちらに尋ねている（別の問い合わせ asks）⇒ 目的の人かはまだ決めず、聞かれたことに録音で答えるよう返す（不在・取り次ぎより先）
        (2) 届いた判定のどれか1つでも取り次ぎ ⇒ 黙って待つ（次に話す方は、新しい方）／どれか1つでも不在 ⇒ かけ直しの申し出を求める
            （この順。AIの読みより判定を先に見る。弱い読み＝確からしさが線より下は、ここには使わない＝繋ぐのを止めるだけ＝_doubted）
        (3) 繋ぐ（取り消せない＝_connect）：
            - 判定を使わない通話で、AIが目的の人と報告した
            - AIの読みが「目的の人ではない」でなく（受付の方・確かめたあとの「代わった方」＝「いいえ」ではなく）、いまの言葉の判定が食い違わず
              （「ご本人」か、まだ届いていない）、この方の「ご本人」の読みが2つ以上（読み＝AIの「目的の人」の報告・この方の発話を「ご本人」と
              読んだ判定の1つずつ。遅れて届いた前の発話の判定も、同じ方なら数える）
            - この方に確かめたあとで、AIが目的の人と報告し、確かめのあとの発話の判定が1つも届いていない（判定が止まっている）＝確かめへの
              返事をAIの読みだけで受け取る（確かめる前の「決められない」「代わった人」は、確かめた理由なので数えない。不在と読まれていたら
              繋がない）。通話の事実に読みを残し、通話後の検証が確かめる（DESIGN.md 6-12）
        (4) 繋がない：
            - 受付の方 ⇒ 黙って待つ（お取り次ぎ）。ただし、この方にまだ確かめておらず、判定は「ご本人」と読んだ ⇒ 確かめる
            - この方にまだ確かめていない ⇒ 目的の人かを確かめる（1人に1回＝_ask_target）。判定が「代わった人」（違う方）で、AIも
              「代わった方」と言う ⇒ 確かめずに、名乗ってから目的の人をお呼びする（_call_target）
            - この方に確かめたあと（「はい」とそろわなかった）⇒ 目的の人をお呼びし直す（_call_target。お詫びから）
        (5) AIの「お呼びした方はいま出られない」（away）は、(2) の不在の読み（いまの言葉・前の言葉のどれか1つ）があれば受け取る。無ければ：
            - いまの言葉の判定が1つも届いていない（判定が止まっている・判定を使わない通話）⇒ AIの読みだけで受け取り、通話の事実に残す
            - 判定が「ご本人」で、理由が「あとで」「別の番号へ」⇒ 受け取る（電話口のご本人が、いまは話せない・別の番号へと言った）
            - ほか（判定は「別の方」「代わった人」「決められない」、または「ご本人」なのに理由が「不在」）⇒ 判定と食い違う。AIの読みを
              「この方は目的の人ではない」として (3)(4) で決める（お呼びし直す・確かめる＝間違えても、相手がまた不在を伝えれば決め直せる側。
              間違えてかけ直しへ進むと、目的の人に繋がらないまま締める）。⛔ 2026-10-22 G-57 tc24（確かめにいいえ・L4）：確かめへの
              「いいえ、私は山田と申します。」を、判定は2つとも「別の方」と読んだのに、AIのかけ直しの申し出（不在）を判定に当てずに受け取り、
              山田様に戻る時刻を聞き、そのあと代わって出た岡田様にも「何時ごろお戻りに…」を流した"""
        j = self.meaning_said if self.words_complete else None
        self.target_claimed = ai == "target"
        if self.judging and j is None and (self.midway or not self.words_complete):
            return self._wait_for_words()
        # こちらに尋ねているときは、ここへ来る前に答える道へ回す（apply の入口＝1か所。不在・取り次ぎより先。⛔ 2026-10-22 G-59：「岡田ですね。
        # 失礼ですが、どちら様でしょうか？」を、Sonnet は3回とも「取り次ぎを待つ」と読んだ＝取り次ぎを先に見ると、質問に答えずに黙って待つ）
        if self._hold_now():
            # 届いた判定のどれか1つでも「取り次ぎを待つ」：黙って待つ（不在より先＝「岡田は外出しておりますので、別の者に代わります。少々
            # お待ちください。」は、取り次ぎの最中に話さない）。間違えて待っても、相手がまた話せば決め直せる（取り消せる）が、間違えて確かめると、
            # 取り次ぎの最中の方に話しかけ、話し手の区切りもずれる。⛔ 2026-10-22 G-57 tc15：判定がそろったときだけ待っていたので、1つが
            # 「取り次ぎを待つ」・もう1つが別の読みのとき、受付の方へ確かめる一言を流した（区切りは取り次ぎで次の発話へ動き、確かめは誰にも
            # 数えられず、知らせの文が「None」になった）
            return self._hold()
        if self._away_now():
            return self._away(why)
        if ai == "handover":
            self._new_speaker(self.callee_turns, after=False)  # 代わった方が話し始めた（確かめを聞いた方の返事なら、替わった印にしない）
        asked = self.asked_target is not None
        read, answered, away = self._target_readings()
        doubt = self._doubted()
        if away and j != TARGET_IS_THERE:
            # 前の言葉で、お呼びした方が出られないと言われていた（この方でも、取り次いだ前の方でも。判定が遅れて届いた分も）：いまの言葉が
            # 「ご本人」と読まれない限り、お呼びし直さずに、かけ直しへ
            return self._away(why)
        if ai == "away":
            if j is None or (j == TARGET_IS_THERE and why != "absent"):
                # 判定が届いていない（AIの読みだけ＝通話の事実に残し、通話後の検証が確かめる）／電話口のご本人が、いまは話せないと言った
                return self._callback(why, {"ai"} | (read if j == TARGET_IS_THERE else set()))
            ai = "not_target"  # 判定と食い違う：AIの読みを「この方は目的の人ではない」として決める（上の (5)）
        if ai == "target":
            read |= {"ai"}
        if ai == "target" and not self.judging:
            return self._connect(read)
        # AIの読みが「目的の人ではない」：受付の方／確かめたあとの「代わった方」（「いいえ」）。判定が「ご本人」とそろっても繋がない
        against = ai in ("reception", "not_target") or (ai == "handover" and asked)
        # 確かめへの返事が、まだ届いていない（AIが、確かめを流したあとに同じ発話でまた報告した）
        waiting = asked and self.confirm_open and self.callee_turns <= self.asked_target_turn
        # 確かめたあとの返事の判定が1つも届いていない（判定が止まっている）：確かめへの「はい」を、AIの読みだけで受け取る。確かめる前の判定
        # （決められない・代わった人）は、確かめた理由なので数えない。不在は、いつ届いても繋がない
        alone = asked and not waiting and ai == "target" and not answered and not away
        if not against and not doubt and j in (TARGET_IS_THERE, None) and (len(read) >= 2 or alone):
            return self._connect(read)
        if waiting:
            return self._reject("目的の人かを聞いた返事が、まだ届いていません。")
        if ai == "reception":
            if not asked and j == TARGET_IS_THERE:
                # AIは受付の方と言い、いまの言葉の判定は「ご本人」と読んだ：繋がずに確かめる（前の発話の読みでは決めない＝いまの言葉は
                # 「お待ちください」かもしれない）
                return self._ask_target(new_person=False)
            # 受付の方：目的の人は、このあと代わって出る別の方（次の発話から新しい方＝取り次ぎの判定と同じ区切り。判定が届かないときの手がかり）。
            # ⛔ 2026-10-21 の見直し（判定を止めた形 G-57 tc8down）：区切りを動かさなかったので、山田様に確かめたことが、取り次がれて出た岡田様にも
            #    「確かめ済み」と数えられ、岡田様をAIの読みだけで繋いだ（確かめていない方を、AIの読みだけで繋ぐ道）。お取り次ぎを待つ間は話させない
            self._new_speaker(self.callee_turns, after=True)
            self.passed_on = True
            return {"result": "noted", "silent": True, "message": f"受け付けました。{self._now()}"}
        if not asked and ai in ("handover", "not_target") and j == HANDOVER:
            self.passed_on = True
            return self._call_target(sorry=False)
        if not asked:
            return self._ask_target(new_person=ai == "handover" or j in (HANDOVER, UNDECIDED) or doubt)
        # 確かめへの返事が、はっきりしない（AIは「はい」と読んだが判定はそう読まない・AIは「いいえ」と読んだが判定は「はい」と読んだ）：
        # お呼びし直さず（本当はご本人かもしれない）、お詫びのあと1回だけ聞き返す。聞き返しへの返事も同じなら、AIが2回とも「はい」と読み、
        # 判定が「別の方」（いいえ）と読んでいなければ繋ぐ。⛔ 2026-10-22 ユーザー：「はいと揃わなかったけど、岡田様だった場合…また、岡田様を
        # 呼ぶんですか？」＝前は、はっきりしない返事でも「失礼いたしました。…いらっしゃいますでしょうか？」とお呼びし直していた
        unclear = self.confirm_open and ((ai == "target" and (j != TARGET_IS_THERE or doubt))
                                         or (ai in ("handover", "not_target") and j == TARGET_IS_THERE))
        if unclear and self._confirms_to_speaker() < 2:
            return self._ask_target(new_person=False, again=True)
        if unclear and ai == "target" and j != TARGET_IS_NOT:
            return self._connect(read)
        # 確かめたあとのAIの「代わった方」は、「いいえ」とも、新しい方が出たとも取れる：判定が「別の方」（いいえ）と読んだとき以外は、新しい方
        # かもしれないので名乗りから流す（確かめへの返事のときだけ。同じ方なら名乗りが重なるだけ。⛔ G-57 tc11（判定を止めた形・名乗らない方）：確かめが取り次ぎの最中に
        # 流れ、そのあと出た岡田様の「はい、お電話代わりました。」を「いいえ」と同じに扱って、名乗りを流さないまま質問へ進んだ）
        return self._call_target(sorry=True, maybe_new=ai == "handover" and j != TARGET_IS_NOT and self.confirm_open)

    def _away_now(self) -> bool:
        """いまの相手の言葉（出揃ったもの）を、届いた判定のどれか1つでも「不在」と読んだか。「不在」も「別の方」「代わった人」も繋がない読みで、
        違いは「目的の人をもう一度お呼びするか」だけ ⇒ 1つでも「不在」なら、お呼びし直さずにかけ直しへ（✅ 2026-10-22 ユーザー：「違う名乗りの
        方が、岡田はいないと言っても、また、岡田様を呼ぶんですか？」。⛔ G-59：「いいえ、山田と申します。岡田は本日おりません。」を、Sonnet と
        Jev は「別の方」、3.5 Flash Lite は「不在」と読んだ＝1つの言葉に2つの意味。決まりの文を書き足すと、判定の間で誤りが入れ替わるだけだった）"""
        return self.words_complete and TARGET_IS_AWAY in (self.check.get(k) for k in JUDGMENTS)

    def _hold_now(self) -> bool:
        """いまの相手の言葉（出揃ったもの）を、届いた読みのどれか1つでも「取り次ぎを待つ」と読んだか（呼んでいる段の、黙って待つ側）。
        読み＝目的の人の判定と、尋ねているかの問い合わせ（判定の仕組みは、どの問いにも「取り次ぎを待つ」を選択肢として添える＝meaning_judge.KIND_DESC）。
        ⛔ 2026-10-22 G-57 tc21：受付の方の「岡田は外出しておりますので、別の者に代わります。少々お待ちください。」（不在と取り次ぎの2つの意味）を、
           目的の人の判定は2つとも「不在」と読み、取り次ぎの最中に、かけ直しの質問へ進んだ（3本中2本）。尋ねているかの問い合わせは「取り次ぎを待つ」と
           読んでいた（G-59：この問い合わせの「取り次ぎを待つ」は、答えた335回で、取り次ぎではない言葉に0回）"""
        return self.words_complete and HOLD in (self.check.get(k) for k in HOLD_READINGS)

    def _asking_now(self) -> bool:
        """いまの相手の言葉（出揃ったもの）で、電話に出た方がこちらに尋ねているか（別の問い合わせ asks。聞かれたことに答えるのが先）。"""
        return self.words_complete and self.check.get("asks") == ASKS_YES

    def _asking_note(self) -> str:
        """電話に出た方が、こちらに質問している：目的の人かはまだ決めず（確かめも用件も流さない）、聞かれたことに録音で答える（唯一の言い方）。
        ✅ 2026-10-22 ユーザー：「相手が喋っていても、それが質問だった場合の対処は？」"""
        return (f"相手の言葉（「{(self.callee_said or '').strip()[:30]}」）は、こちらへの質問です（意味の判定）。目的の人かはまだ決めません"
                f"（確かめも用件も流しません）。自分の言葉では答えずに、聞かれたことに合う replay_opening で答えます（{self._answer_choices()}・"
                f"どちらの〇〇か・聞き取れなかった様子＝「{PART_LABEL['all']}」）。聞かれていなければ、もう一度報告してください。")

    def _away(self, why: str | None = None) -> dict:
        """お呼びした方がいま電話に出られない：繋がず（名乗り・用件・質問を流さない）、お呼びし直しもしない。かけ直しの申し出を、AIが聞いた
        言葉に合う理由で（AIは言葉の中身を聞いている＝ご本人が「今は手が離せない」なら「あとで」）。断られたときは終える（唯一の場所）。
        why：AIがかけ直しを申し出ていれば、その理由（request_callback）＝そのまま受け取る。申し出ていなければ、申し出るように返す。
        ⛔ 2026-10-22 ユーザー：「AIが何と報告していてもの中に、重要な報告はないんですか？」＝前は理由を「不在」に決めつけていた"""
        if why:
            return self._callback(why, {"ai"} | self._target_readings()[2])
        if any(x["reply"].get("away") and x["callee_turns"] < self.callee_turns for x in self.log):
            # 前の発話でかけ直しへ進むように伝えたのに、AIはまた別の報告をした：プログラムが「不在」として進める（同じ発話の報告し直しは待つ＝
            # 「別の者に代わります。少々お待ちください。」の取り次ぎの最中に、かけ直しの質問を流さない）。判定が不在と読んだ＝お呼びした方は
            # いらっしゃらない・電話に出られない。理由が「別の番号へ」なら、AIはその操作を呼んでいる）。⛔ 2026-10-22 G-57 tc25（不在で別の方に
            # 代わる・L4）：伝えたあともAIは「代わった方へ」「目的の人」を4回くり返し、代わった山田様に自分の声で「失礼いたしました。
            # 奥田様はいらっしゃいますでしょうか？」と言い、かけ直しの質問は山田様が戻る時刻を言ったあとに流れた（AIが従うかに頼っていた）
            return self._callback("absent", self._target_readings()[2],
                                  prefix=f"お呼びした方は、いま電話に出られません（「{self._away_words().strip()[:30]}」・意味の判定）。"
                                         f"かけ直しの申し出（{CALLBACK_LABEL['absent']}）として、プログラムが進めました。")
        # away：かけ直しへ進むように伝えた印（AIが進まずにターンを終えたら、1回だけ知らせる＝unreported_note）
        return self._reject(self._away_text()) | {"away": True}

    def _away_words(self) -> str:
        """「不在」と読まれた相手の言葉（いちばん新しいもの）。⛔ 2026-10-22 G-57 tc24（不在で別の方に代わる・S4）：いまの言葉を引いていたので、
        受付の方の「岡田は外出しておりますので、別の者に代わります。」のあとに代わった山田様の「お電話代わりました。山田です。」を、
        「お呼びした方がいま電話に出られないという意味です」とAIに伝えていた"""
        if self._away_now():
            return self.callee_said or ""
        turns = [t for t, v in self.calling_judged.items() if TARGET_IS_AWAY in v.values()]
        return self.calling_words.get(max(turns), "") if turns else (self.callee_said or "")

    def _away_text(self) -> str:
        """お呼びした方がいま電話に出られないときに、AIへ伝える文（_away・unreported_note の1か所）。"""
        return (f"相手の言葉（「{self._away_words().strip()[:30]}」）は、お呼びした方がいま電話に出られないという意味です（意味の判定）。"
                "名乗り・用件・質問は流さず、お呼びし直しもしません。request_callback を、聞いた言葉に合う"
                f"{ARG_LABEL['why']}で呼びます（お呼びした方がいらっしゃらない＝「{CALLBACK_LABEL['absent']}」・ご本人が今は話せない＝"
                f"「{CALLBACK_LABEL['later']}」・別の番号にかけてほしい＝「{CALLBACK_LABEL['other_number']}」）。営業をお断りされた・"
                f"もうかけないでと言われたときは、request_end を「{END_LABEL['callee_refused_call']}」で呼びます。")

    def _confirms_to_speaker(self) -> int:
        """いま電話口にいる方に、確かめる一言を流した回数（聞き返しを含む）。"""
        return sum(c >= self.speaker_from for c in self.target_confirmed)

    def _greeted(self) -> bool:
        """いま電話口にいる方に、判定を待つ間にあいさつを流したか（hold_play）。"""
        return self.greeted_at is not None and self.greeted_at >= self.speaker_from

    def _new_speaker(self, turn: int, after: bool, sure: bool = False) -> None:
        """呼んでいる段で、電話口の方が替わった（唯一の場所）：取り次ぎの判定（after＝その次の発話から）・代わった人の判定とAIの「代わった方」
        の報告（その発話から）。替わった方は、まだ名乗りを聞いていない（passed_on）。確かめる一言を聞いた方が、そのあとに言った言葉は、
        替わった印にしない（「いいえ、山本です」＝その方は目的の人ではない、という返事。同じ方にもう一度確かめず、名乗りも流し直さない）。
        前の発話の判定が遅れて届いても、区切りは前へ戻さない。sure：替わったことが分かっている（かけ直しの質問の途中で、AIが「目的の人」と
        報告した＝_target_back。前の方に確かめていても、替わった印にする）。"""
        start = turn + 1 if after else turn
        if start <= self.speaker_from or (not after and not sure and any(self.speaker_from <= c < turn for c in self.target_confirmed)):
            return
        self.speaker_from = start
        self.passed_on = True  # いま電話口にいる方（取り次ぎのあとなら、このあと出る方）は、まだ名乗りを聞いていない

    def _calls_to_speaker(self) -> int:
        """いま電話口にいる方に、呼ぶ一言を流した回数。"""
        return sum(c >= self.speaker_from for c in self.target_called)

    @property
    def asked_target(self) -> str | None:
        """いま電話口にいる方に、目的の人かを確かめたときの言い方（確かめていなければ None。その返事の判定に添える＝judge_context）。"""
        if self.phase == "calling" and any(c >= self.speaker_from for c in self.target_confirmed):
            return self.target[self.confirm_part]
        return None

    @property
    def asked_target_turn(self) -> int:
        """最後に確かめる一言を流したときの、相手の発話の番号（その次の発話が、確かめへの返事）。"""
        return self.target_confirmed[-1] if self.target_confirmed else -1

    def _target_readings(self) -> tuple[set[str], bool, set[str]]:
        """いま電話口にいる方の発話（区切り speaker_from から）の、出揃った言葉の判定（遅れて届いた前の発話の判定も。いまの発話は、出揃って
        いれば）から：(その方を「ご本人」と読んだ判定の名前, 確かめる一言のあとの発話の判定が1つでも届いたか, 不在と読んだ判定の名前)。
        不在の読みだけは、呼んでいる段の全部の発話から（電話口の方が誰かではなく、お呼びした方の事実＝受付の方が「岡田は外出しております
        ので、別の者に代わります。」と言ったら、代わった方にも岡田様をお呼びし直さない。⛔ 2026-10-22 G-59：その言葉を Sonnet は3回とも
        「不在」と読んだが、代わった方の「お電話代わりました。山田です。」は、前の言葉を渡しても3回とも「代わった人」と読んだ）。"""
        every = dict(self.calling_judged)
        if self.words_complete:
            every[self.callee_turns] = {k: self.check[k] for k in JUDGMENTS if k in self.check}
        seen = {t: v for t, v in every.items() if t >= self.speaker_from}
        after = self.asked_target_turn if self.asked_target is not None else self.callee_turns
        # こちらに尋ねた発話の「ご本人」は数えない（答えを待つ発話で、誰かを決める材料にしない。⛔ 2026-10-22 G-59：3.5 Flash Lite は
        # 「岡田ですね。失礼ですが、どちら様でしょうか？」を3回とも「ご本人」と読んだ＝尋ねているかが先なので、その場では繋がないが、読みが
        # 残ると、答えたあとのAIの読み違いと2つそろう）。不在の読みは残す（お呼びした方の事実）
        asking = {t for t, v in self.calling_asks.items() if v == ASKS_YES}
        return ({k for t, v in seen.items() if t not in asking for k, x in v.items() if x == TARGET_IS_THERE},
                any(v for t, v in seen.items() if t > after), {k for v in every.values() for k, x in v.items() if x == TARGET_IS_AWAY})

    def _doubted(self) -> bool:
        """いま電話口にいる方に、繋ぐのを止める弱い読みがあるか（on_unsure）：いまの言葉（出揃ったもの）の分と、確かめたあとの発話の分
        （遅れて届いた分も）。確かめる前の弱い読みは、確かめた理由なので数えない（_target_readings の「確かめのあと」と同じ区切り）。"""
        after = self.asked_target_turn if self.asked_target is not None else self.callee_turns - 1
        late = any(kinds for t, kinds in self.calling_doubted.items() if t > after and t >= self.speaker_from and t < self.callee_turns)
        return late or (self.words_complete and bool(self.doubt))

    def _wait_for_words(self) -> dict:
        """AIの報告を、相手の言葉が出揃う前のものとして決めない：AIを黙って待たせ、相手の続きの言葉を最後まで聞いてから、もう一度報告させる
        （次の報告は、出揃った言葉の判定と当てる）。取り次ぎ（hold）とは違い、相手が電話口を離れたとは決めない（黙る間の見張りは止めない）。"""
        # ⛔ 2026-10-22 G-57 tc21（判定を止めた形）：「岡田ですね。（間）失礼ですが、どちら様でしょうか？」の途中でAIが報告し、待たせたあと、
        #    3本とも質問に答えずに「目的の人」と報告し直した（判定が止まっていると、こちらへの質問かを見分けられるのはAIだけ）
        return {"result": "wait", "silent": True,
                "message": f"相手の方は、まだ話している途中でした。何も言わずに、相手の言葉を最後まで聞いてから、もう一度報告してください（{self._report_hint()}）。"}

    def _report_hint(self) -> str:
        """呼んでいる段で、出揃った言葉で報告し直すときの手がかり（知らせの文の1か所：待たせたとき・報告し直さなかったとき）。"""
        return f"目的の人か・取り次ぎか・いらっしゃらないか。名乗り・用件などを聞かれたなら、それに合う replay_opening で答えます（{self._answer_choices()}）"

    def _connect(self, read: set[str]) -> dict:
        """目的の人に繋がった（DESIGN.md 4-3）：その方にまだ届いていない名乗り・用件を流してから、最初の質問を渡す（唯一の場所）。
        取り次がれて代わった方（passed_on）には、あいさつ・名乗り・用件の全部（呼んだときの名乗りは、取り次いだ方にしか届いていない）。
        最初に出た方が目的の人なら、まだ届いていない分だけ（用件。録音の途中で止めて届いていない名乗りがあれば、それも）。
        録音で流す形では名乗り・用件を録音で流し、最初の質問は _advance が決める（録音か、AIが言うか）。AIが話す形では、AIが言う。
        read＝繋いだ読み（「ai」＝AIの報告・判定の名前）。通話の事実（connect）に残し、繋いだあとに届いた判定も足す（on_check。通話後の検証が、
        判定なしで繋いだ通話を確かめる＝DESIGN.md 6-12）"""
        self._note("connect", read=sorted(read), judging=self.judging)
        if self.callback_why:
            # かけ直しの質問の途中で、目的の人に繋がった（_target_back）：かけ直しは要らない（聞いた答えは通話の事実に残したまま。締めはふつう）
            self.facts["callback"][-1]["connected"] = True
            self.callback_why, self.callback_ids = None, []
        keys = [k for k in OPENING_ORDER if self.target.get(k) and (self.passed_on or k in self.opening_unheard)
                and not (k == "greeting" and self._greeted())]  # 判定を待つ間に流したあいさつは、もう流さない（hold_play）
        self.phase, self.opening_unheard, self.passed_on = "asking", [], False
        texts = "".join(self.target[k] for k in keys)
        if not keys:
            return self._advance("目的の人に繋がりました。", handled=True)
        if not self.opening:
            return self._advance(f"目的の人に繋がりました。まず「{texts}」と言ってから、", handled=True)
        r = self._advance(f"目的の人に繋がりました。「{texts}」は、プログラムが録音で流しました（相手にもう聞こえています）。続けて、", handled=True)
        if "play" not in r:
            r["speak"] = True  # 最初の質問は AIが言う（読みの確かめ・相手の質問に答えてから聞くとき）：録音のあとに話させる
        r["play"] = [{"kind": "opening", "part": k, "text": self.opening[k]} for k in keys] + r.get("play", [])
        return r

    def _op_request_callback(self, a: dict) -> dict:
        """かけ直しの申し出（DESIGN.md 6-2）。理由ごとに、システムが足す質問（callback_question）を、ふつうの質問と同じ流れ（record_answer・
        言い直して確かめる・聞き返し）で聞き、取れたら締める（ふつうの締め）。残りの業務の質問は聞かない（目的の人ではない・いまは話せない相手）。
        聞いた番号へかけるか・いつかけるかは、門と次の動き（CL-2）が決める：会話の側は、聞いたとおりに通話の事実へ残すだけ。
        言い直しの返事待ちだった業務の質問は、確かめずに終わる（「-」）。念のための確認はしない（相手は話せる状態ではない）。"""
        if self.phase == "closing":
            return self._reject("通話はもう締めに入っています。")
        if self.phase == "calling":
            # 呼んでいる段：AIの「お呼びした方はいま出られない」という読み＝電話口の方が誰かを決める1か所で、判定に当ててから受け取る
            # （取り次ぎの最中は待つ・不在の読みがあれば受け取る・判定と食い違えばお呼びし直すか確かめる）。⛔ 2026-10-22 G-57 tc24（確かめに
            # いいえ・L4）：ここで判定を見ずに受け取っていたので、「いいえ、私は山田と申します。」（判定は2つとも「別の方」）でかけ直しへ進んだ
            return self._calling_reported("away", why=a["why"])
        if self.callback_why:
            return self._reject(f"かけ直しの申し出（{CALLBACK_LABEL[self.callback_why]}）は、もう受け付けています。")
        return self._callback(a["why"], {"ai"})  # 繋いだあと：目的の人ご本人の申し出（電話口の方が誰かは、もう決まっている）

    def _callback(self, why: str, read: set[str], prefix: str = "") -> dict:
        """かけ直しの質問を始める（唯一の場所）。read＝受け取った読み（「ai」＝AIの申し出・不在／ご本人と読んだ判定の名前。「ai」が無ければ
        プログラムが進めた）を通話の事実に残す（AIの読みだけで受け取ったかを、通話後の検証が見る＝DESIGN.md 6-12。繋ぐときの connect と同じ形）。
        目的の人かを確かめに戻ったあと（_target_back）に、同じ理由でまた申し出られたら、聞いた分は残したまま続きから聞く。
        電話口の方が、まだこちらの名乗りを聞いていない方（取り次がれて代わった方）なら、先にあいさつ・名乗りを流す（Intent.md 9-4「相手が
        替わったら短く名乗り直す」。_call_target と同じ決まり）。⛔ 2026-10-22 の見直しで見つけた漏れ：代わった山田様に、名乗らずに
        「承知しました、恐れ入りますが何時ごろお戻りに…」を流していた"""
        intro = [k for k in ("greeting", "intro") if self.phase == "calling" and self.passed_on and self.target.get(k)
                 and not (k == "greeting" and self._greeted())]
        if self.phase == "calling":
            self._keep_calling_turn()  # かけ直しを決めた発話の読み（不在）を残す：確かめに戻ったとき（_target_back）も、お呼びし直さない
            self.passed_on = False if intro else self.passed_on
        if why == self.callback_why and self.callback_ids:
            self._note("callback_resume", why=why, read=sorted(read))
            self.order = list(self.callback_ids)
        else:
            self.callback_why = why
            self._note("callback", why=why, read=sorted(read))
            qs = [callback_question(r) for r in CALLBACK_PLAN[why]]
            for q in qs:
                self.questions[q.id], self.items[q.id] = q, Item()
            self.order = self.callback_ids = [q.id for q in qs]
        self.phase, self.recheck_queue = "asking", []
        self.awaiting_confirm = self.awaiting_say = None
        if not intro:
            return self._advance(prefix, handled=True)
        texts = "".join(self.target[k] for k in intro)
        if not self.opening:
            return self._advance(f"{prefix}電話口の方は、まだこちらの名乗りを聞いていません。まず「{texts}」と言ってから、", handled=True) | {"renamed": True}
        r = self._advance(f"{prefix}電話口の方は、まだこちらの名乗りを聞いていません。「{texts}」は、プログラムが録音で流しました（相手にもう"
                          "聞こえています）。続けて、", handled=True)
        if "play" not in r:
            r["speak"] = True
        r["play"] = [{"kind": "opening", "part": k, "text": self.opening[k]} for k in intro] + r.get("play", [])
        return r | {"renamed": True}

    def _target_back(self) -> dict:
        """かけ直しの質問の途中で、AIが「目的の人」と報告した（お呼びした方が戻って電話口に出た・AIの聞き違い）：呼んでいる段に戻り、
        その方に目的の人かを1回確かめる（唯一の場所。判定はかけ直しの質問の答えとして読んでいて、電話口の方が誰かは読んでいない＝
        確かめの返事を、呼んでいる段の決め方 _calling_reported で決める）。「はい」なら繋ぎ（あいさつ・名乗り・用件・最初の質問）、
        違えば（前に不在と読まれているので）かけ直しの質問の続きへ戻る（_callback）。聞いたかけ直しの答えは通話の事実に残したまま。
        ⛔ 2026-10-22 G-57 tc24（確かめにいいえ・L4）：かけ直しの質問の途中で代わって出た岡田様の「お電話代わりました。岡田です。」に、
           あいさつ・名乗り・用件のあと「恐れ入りますが何時ごろお戻りになられますでしょうか？」を流し、業務の質問を1つも聞かずに締めた"""
        self._note("target_back", why=self.callback_why)
        self.order, self.phase, self.current, self.recheck_queue = list(self.business_ids), "calling", None, []
        self.awaiting_confirm = self.awaiting_say = None
        self._new_speaker(self.callee_turns, after=False, sure=True)
        return self._ask_target(new_person=True)

    def _hang_up(self, reason: str) -> dict:
        """話さずに切る唯一の場所（人ではない相手）。締めの言葉も相づちも流さない。"""
        self.current, self.phase, self.end_reason = None, "closing", reason
        self._note("end", reason=reason)
        return {"result": "end", "hang_up": True,
                "message": f"相手は{PARTY_LABEL[reason]}です（人ではありません）。何も言わずに終えます（言葉を言いません）。"}

    def _ask_target(self, new_person: bool, again: bool = False) -> dict:
        """電話口の方が目的の人か、読みがそろわない：目的の人かを確かめる（DESIGN.md 4-3。言い方は phrases.target_confirm）。1人に1回
        （_calling_reported が、まだ確かめていない方にだけ呼ぶ）。録音で流す通話では、呼ぶ一言と同じく録音（名乗りの部分 target_confirm）を
        流す。⛔ 2026-10-19 G-62：AIに言わせていたので、名前を言い崩した（「小田原までいらっしゃいましたでしょうか？」）＝DESIGN.md 4-3
        「名前をAIに言わせる所を残さない」に、コードが届いていなかった。認めたら report_party（目的の人）＝_connect が、あいさつ・名乗り・
        用件・最初の質問を流す。
        new_person：新しい方が出た印がある（判定が「代わった人」「決められない」と読んだ・AIが「代わった方」と報告した）＝取り次がれた
        （passed_on。繋いだら、あいさつ・名乗りから全部）。判定が届かずに確かめるとき（AIは「目的の人」と報告し、新しい方の印は無い）は付けない。
        ⛔ 2026-10-19 G-62：いつも付けていたので、判定が遅れて最初に出たご本人に確かめたとき、繋いだあとに名乗りをもう一度流した"""
        if new_person:
            self.passed_on = True
        self.target_confirmed.append(self.callee_turns)
        self.confirm_open = True
        self.confirm_part = CONFIRM_PARTS[1] if again else CONFIRM_PARTS[0]  # 聞き返しは、確かめとは別の言い方（phrases.target_reconfirm）
        text = self.asked_target
        then = self._now()
        head = {"result": "confirm_target", "again": again}  # again：はっきりしない返事への聞き返し
        if self.play_recordings and self.opening.get(self.confirm_part):
            return head | {"play": [{"kind": "opening", "part": self.confirm_part, "text": text}],
                           "message": f"電話に出た方に「{text}」と聞く録音を、プログラムが流しました（相手にもう聞こえています）。何も言わずに、{then}"}
        return head | {"say": text, "message": f"電話に出た方に「{text}」とだけ聞きます（言葉を足しません）。そのあと、{then}"}

    def _call_target(self, sorry: bool, maybe_new: bool = False) -> dict:
        """電話口の方は目的の人ではない（確かめに「いいえ」と答えた・違うお名前を名乗った・確かめへの返事が「はい」とそろわなかった）：
        目的の人をお呼びし直す（DESIGN.md 4-3「受付・別の人 ⇒ 決まった言い方で取り次ぎを頼む」。唯一の場所）。
        - sorry：確かめたあと（頭にお詫びの相づち「失礼いたしました。」＝_ack）
        - その方にまだ名乗っていない（取り次がれて代わった方＝passed_on）⇒ あいさつ・名乗りから（DESIGN.md 4-3「相手が替わったら短く名乗り
          直す」）。流したら、その方は名乗りを聞いた方（passed_on を消す。用件はまだ＝繋いだら用件から）
        - maybe_new：電話口の方が替わったかもしれない（確かめたあとのAIの「代わった方」）⇒ 名乗りから
        - 呼ぶ一言は、その方に2回まで（同じお願いを繰り返さない）。流すものが無ければ、お詫びだけで相手の言葉を待つ
        ⛔ 2026-10-21 の見直しで見つけた漏れ：確かめに「いいえ、山本です。」と言われたときの道が無く、AIは番号違いで締めた（G-62）・同じ方に
           もう一度確かめた・回数を使い切って繋いだ"""
        self.confirm_open = False  # 確かめへの返事は、もう待っていない（_now）
        if sorry and any(c >= self.speaker_from for c in self.target_sorry):
            sorry = False  # お詫びは、同じ方に1回まで
        if sorry:
            self.target_sorry.append(self.callee_turns)
        keys = [k for k in ("greeting", "intro") if (self.passed_on or maybe_new) and self.target.get(k)
                and not (k == "greeting" and self._greeted())]
        if keys:
            self.passed_on = False
            self.opening_unheard = [k for k in OPENING_ORDER if k == "purpose" and self.target.get(k)]
        if self._calls_to_speaker() < 2:
            keys.append("target")
            self.target_called.append(self.callee_turns)
        text = "".join(self.target[k] for k in keys)
        head = {"result": "call_target", "sorry": sorry}
        what = "相手の方は、目的の人ではありませんでした（目的の人だと、はっきりしませんでした）。"
        if not keys:
            return head | {"silent": True, "message": f"{what}お詫びの相づちだけを流しました。何も言わずに、{self._now()}"}
        if self.opening:
            return head | {"silent": True, "play": [{"kind": "opening", "part": k, "text": self.opening[k]} for k in keys],
                           "message": f"{what}「{text}」は、プログラムが録音で流しました（相手にもう聞こえています）。何も言わずに、{self._now()}"}
        return head | {"say": text, "message": f"{what}「{text}」とだけ言います（言葉を足しません）。そのあと、{self._now()}"}

    def _op_request_end(self, a: dict) -> dict:
        reason = a.get("reason")
        if reason == "done" and self.phase == "calling":
            return self._reject("まだ目的の人に繋がっていません（質問を始めていません）。")
        if reason == "not_transferred":
            return self._not_transferred()
        if reason in ("callee_hurry", "callee_refused_call") and (taken := self._ambiguous_only("通話を断られた")):
            # 相手の言葉が「結構です」のような言葉だけ：その言葉だけで、通話を断られたとは決めない（_ambiguous_only）
            return taken
        if reason == "callee_hurry" and self.busy_ask and not self.busy_asked:
            # 業務設定で「引き止める」を選んだとき：1回だけ、決まった一言で聞く。2回目の「急いでいる」は終える
            self.busy_asked = True
            return {"result": "busy_ask", "say": self.busy_ask,
                    "message": f"「{self.busy_ask}」と1回だけ聞いてください。相手が応じたら、いまの質問から続けます。"
                               f"応じなければ、もう一度 request_end を{ARG_LABEL['reason']}「{END_LABEL['callee_hurry']}」で呼んでください。"}
        if reason in DECLINED_ENDS:
            if reason == "callee_refused_call":
                self._note("do_not_call")
            elif reason == "callee_complaint":
                self._note("complaint")
            return self._close(reason)
        if self.current is not None:
            # いま聞いている質問がある間は、終えられない（まだ聞いていない・言い直しの返事待ち・念のための確認の最中）
            # ⛔ 2026-10-13 の試験で見つけた漏れ：「まだ聞いていない項目」だけを数えていたため、言い直しの返事待ちや
            #    念のための確認の最中に「終わり」と報告されると、確認を飛ばして通話を終えていた
            return self._reject("まだ終えられません。")
        return self._advance("")

    def _not_transferred(self) -> dict:
        """受付の方が、営業のお断りではない事情で、目的の人に取り次げないと言った（「担当が分からないのでお繋ぎできません」「個人宛のお電話は
        お繋ぎしておりません」など。戻る時刻・かけ直しの話は無い）：お礼の締めで終える（唯一の場所）。日を改めてかけ直すかは CL-2 が決める
        （✅ 2026-10-22 ユーザー決定「『事情があり、取り次げなかった』：日を改めて1回」＝CL-2 の 4-130）。営業のお断り（「営業のお電話は
        お繋ぎできない決まりです」も）は、通話を断られた（発信の拒否＝もうかけない）。お呼びした方がいま出られない（「席を外しております」）は、
        かけ直しの申し出（戻る時刻を聞く）。呼んでいる段だけ：言葉の途中なら待ち、取り次ぎの読みがあれば黙って待つ（_calling_reported と同じ順）。"""
        if self.phase != "calling":
            return self._reject("目的の人には、もう繋がっています（取り次ぎの話ではありません）。")
        if self.judging and self.meaning_said is None and (self.midway or not self.words_complete):
            return self._wait_for_words()
        if self._hold_now():
            return self._hold()
        self._note("not_transferred")
        return self._close("not_transferred")

    @property
    def not_reached(self) -> str | None:
        """目的の人のある通話が、目的の人に繋がらずに終わった（いま終わったら）ときの理由（NOT_REACHED_LABEL の中の名前）。会話の報告の
        not_reached に入れる唯一の場所（電話接続側が、通話の終わりに読む＝相手が切った通話も）。繋がった・目的の人の無い業務・人と話して
        いない（留守番電話・自動音声・ファクス・だれも話さない）・番号違い・通話を断られた（発信の拒否）なら None。
        かけ直しの申し出で終えた ⇒ 不在（戻る時刻が取れたかは、通話の事実 callback が持つ）／それ以外（取り次いでいただけなかった・
        相手が急いでいる・怒っている・呼んでいる段で切られた）⇒ 取り次いでいただけなかった。
        ⛔ 2026-10-22 CL-2 の 4-130：試作に not_reached の理由を報告する所が無かった（理由が無いと、判定は「理由が無い」で判定待ちになる）"""
        if not self.target or self.facts.get("connect") or self.callee_turns == 0:
            return None
        if self.party in NOT_A_PERSON or self.party == "wrong_party" or self.facts.get("do_not_call"):
            return None
        return "absent" if self.callback_why else "not_transferred"

    # ── 内部
    def _ambiguous_only(self, ai_said: str) -> dict | None:
        """相手の言葉が「結構です」「大丈夫です」のような言葉だけなのに、AIが答えとしてでなく「通話を断られた」「聞き取れない」と報告した
        ときの扱い（唯一の場所）。その言葉だけで決めず、いまの質問の答えとして受け取る。当たらなければ None（AIの報告のまま）。
        - 選択肢の質問：前の質問から意味が決まれば、その選択肢（Question.reply_meanings）。決まらなければ、AIに選択肢を選んで報告し直させ、
          意味を確かめる（_op_record_answer）
        - 値を聞く質問：答えの値ではないので、聞き返す（答えが質問に合わないときと同じ。_bad_form）
        - 自由な意見：その言葉のまま、答えとして報告し直させる
        確かめた・聞き返したあとにまた言われたら（ambiguous_asked）、AIの報告のまま（2度目は、前の会話から断りと分かる）。
        ✅ 2026-10-17 ユーザー：「『結構です。』だけでは判断できない。人でも前の質問や会話から判断する言葉です」
        ⛔ G-14：相手の「結構です。」（「ご予約はされていますでしょうか？」への答え）を、AIは 30本中4本で通話の断りとして締めた"""
        q = self.questions.get(self.current) if self.current else None
        said = (self.callee_said or "").strip()
        word = ambiguous_word(said)
        if q is None or not word or self.phase != "asking" or self.awaiting_confirm is not None or self.items[q.id].ambiguous_asked:
            return None
        if q.type in CHOICE_TYPES and q.reply_choice(word):
            return self._op_record_answer({"question_id": q.id, "condition_id": q.reply_choice(word), "value": said})
        if q.type in CONFIRM_TYPES:
            self.items[q.id].ambiguous_asked = True
            return self._bad_form(q.id, said, "答えの値ではなく、返事の言葉です")
        # 差し戻すのは1回だけ（AIが報告し直さずに同じ報告を繰り返しても、流れが止まらないように＝2度目はAIの報告のまま）
        self.items[q.id].ambiguous_asked = True
        # 選ぶのは業務の選択肢から（「わからない」「答えたくない」も、その言葉だけでは決めない）
        how = (f"選択肢（{'、'.join(label(c) for c in q.all_conditions if c not in SYSTEM_CONDITIONS)}）から近いものを選んで"
               if q.type in CHOICE_TYPES else "聞いたとおりの言葉を値に入れて")
        # ⛔ G-14：前の文（「…報告してください。プログラムが意味を確かめます。」）では、6本中1本でAIが報告せずに、自分の言葉で
        #    「かしこまりました。念のための確認でございますが、ご予約はお済みでしょうか。」と聞き直した ⇒ 何も言わずに、すぐ報告させる
        return self._reject(f"相手の「{said[:20]}」だけでは、{ai_said}とは決められません（前の質問によって意味が変わる言葉です）。"
                            f"相手にはまだ何も言わずに、いまの質問 {q.id} の答えとして、{how}、すぐ record_answer で報告してください。"
                            "確かめ・聞き返しの言い方は、プログラムが返事で渡します（自分で聞き直してはいけません）。")

    def _bad_form(self, qid: str, value: str, why: str) -> dict:
        """答えの値の形が質問の型に合わない（DESIGN.md 6-2）。値は記録せず、聞き返し（_ask_again）へ。"""
        q = self.questions[qid]
        return self._ask_again(qid, "form", f"値の形が合わない（{value}：{why}）",
                               f"{q.id}「{q.text}」の答えとして「{value}」が報告されましたが、{why}。")

    def _ask_ampm(self, qid: str, value: str) -> dict:
        """時刻の午前・午後を、1回だけ聞く（fill_ampm で決められなかったとき）。答えは、次の答えの報告で前の値に入れる（merge_ampm）。"""
        it = self.items[qid]
        it.ampm_asked, it.pending_value = True, value
        it.set("unclear", f"午前か午後かが分からない（{value}。営業時間 {self.business_hours[0]}〜{self.business_hours[1]}時）")
        return {"result": "ask_back", "question_id": qid, "say": AMPM_ASK, "ask_n": 0, "ask_reason": "ampm",
                "message": f"「{value}」の午前・午後が分かりません。「{AMPM_ASK}」と聞いて、相手の答えを待ってください。"
                           "相手が答えたら、聞いたとおりの値で record_answer で報告します。"}

    def _ask_again(self, qid: str, reason: str, why: str, shown: str = "") -> dict:
        """聞き返しの唯一の場所（DESIGN.md 6-11。聞き取れない・曖昧＝"unclear"／答えが質問に合わない＝"form"）。AIに選ばせず、ここで
        決める：必須の項目は、その場で3回まで聞き返す（言い方は段ごとに phrases.ask_again）。使い切っていたら・必須でない・念のための確認の
        最中なら、「-」にして次へ（必須なら、締めの前の念のための確認で1回）。⇒ どの場合も、AIへの返事に次にすることが必ず入る。"""
        q, it = self.questions[qid], self.items[qid]
        if self.phase == "asking" and q.required and it.ask_backs < self.limits.ask_back:
            it.ask_backs += 1
            it.set("unclear", f"{why}・聞き返す（{it.ask_backs}回目）")
            # 最後の1回か：その場の回数を使い切り、締めの前の念のための確認も無いとき（依頼主の設定で確認を0回にしたとき）
            last = it.ask_backs == self.limits.ask_back and self.limits.recheck == 0
            say = ask_again_phrase(it.ask_backs, reason, q.topic, last=last)
            return {"result": "ask_back", "question_id": q.id, "say": say, "ask_n": it.ask_backs, "ask_reason": reason, "ask_last": last,
                    "message": f"{shown}「{say}」と聞き返して、相手の答えを待ってください。相手が答えたら record_answer で報告します。"}
        it.set("unresolved", f"{why}・聞き返しを使い切った" if self.phase == "asking" else f"{why}・念のための確認でも聞けなかった")
        return self._advance(f"{shown}「-」にします。" + self._reading_note(qid))

    def _enter_reading(self, qid: str, prefix: str = "") -> dict:
        """名前の読みの確かめを始める（reading_question）。まず言ってみる読みを値にして、言い直しの返事待ちにする。言い方は質問の文。"""
        q, it = self.questions[qid], self.items[qid]
        it.condition, it.value = q.conditions[0], q.prefill
        it.tried_readings.append(q.prefill)
        it.set("checking", f"読みが決まらない名前「{q.reading_of}」を、まず言ってみる読みで確かめる")
        self.awaiting_confirm, self.confirm_requested_at, self.awaiting_say = qid, self.callee_turns, q.text
        return {"result": "confirm_required", "question_id": qid, "value": q.prefill, "say": q.text, "after": None,
                "message": f"{prefix}次は、お名前の読みの確認です。「{q.text}」と言って、相手の返事を待ってください。{self._confirm_how(qid)}"}

    def _ask_confirm(self, qid: str, after: str | None = None) -> dict:
        """言い直して確かめる。after＝値を直されたあとか（AFTER の鍵。場面の言い方・頭の言葉・流す相づちを、その表の1か所から選ぶ）。
        ⛔ 2026-10-14 ユーザー：「失礼いたしました。」のあとの「かしこまりました」はおかしい。こちらの聞き違いなら「失礼いたしました。
        改めまして、…大丈夫でしょうか？」、相手の言い間違いなら「承知しました。それでは、…よろしいでしょうか？」。
        選択肢の型は、選んだ選択肢の意味の言葉で言い直す（choice_words。「念のためご予約はお済みということでよろしいでしょうか？」）。"""
        it, q = self.items[qid], self.questions[qid]
        choice = q.type in CHOICE_TYPES
        words = choice_words(q, it.condition) if choice else None
        if choice and words is None:
            # 選択肢の名前が返事の言葉だけ（「はい」「いいえ」）で、意味の言葉が取込で決まらなかった＝言い直せない（止めずに進める）。
            # 曖昧な答えは、言い直さずに聞き返す（聞き返しの回数と言い方は _ask_again の1か所）。それ以外は聞き取れた答えのまま受け付ける
            if after in ("ambiguous", "hedged"):
                return self._ask_again(qid, "unclear", "曖昧な答え（言い直す言葉が無い）")
            it.set("accepted", "選択肢の型で、言い直す言葉が無い（聞き取れた答えのまま受け付ける）")
            return self._advance("受け付けました。")
        it.set("checking", "言い直して確かめる")
        self.awaiting_confirm = qid
        self.confirm_requested_at = self.callee_turns
        # 場面：聞き返した・念のための確認・訂正のあとは「聞き取りにくかったあと」、それ以外は「ふつう」（phrases.CONFIRM）
        situation = AFTER[after][0] if after else (
            "聞き取りにくかったあと" if (it.ask_backs or it.corrections or self.phase == "rechecking") else "ふつう")
        n = self.confirm_counts.get(situation, 0)
        self.confirm_counts[situation] = n + 1
        # 言い直す値：値の無い選択肢の型（はい／いいえなど。聞き返したあとは復唱する＝6-11）は、選んだ選択肢で言い直す。
        # ⛔ 2026-10-14 に見つけた穴：値だけを渡していたため、「Noneで、よろしいでしょうか。」という文を作っていた
        shown = restate_words(words, self.callee_said) if choice else (it.value or label(it.condition))
        if q.reading_of:
            shown = quote(it.value)
            it.tried_readings.append(it.value)
        lead = AFTER[after][1] if after else "言い直し"
        # 名前の読みは、質問と同じ言い方で、直していただいた読みを言う（「改めましてお名前の読みの方は「なかしま」様でよろしかったでしょうか？」）
        if q.reading_of:
            phrase = NAME_READING_ASK[q.reading_kind].format(v=shown)
        elif choice:
            phrase = confirm_phrase(shown, situation, n, meaning=True)
        else:
            phrase = self.confirm(shown, situation, n)
        say = self.awaiting_say = self._lead(lead) + phrase
        return {"result": "confirm_required", "value": shown, "say": say, "after": after,
                "message": f"言い直して確かめます。「{say}」と言って、相手の返事を待ってください。" + (self._confirm_how(qid) if choice else "")}

    def _confirm_how(self, qid: str) -> str:
        """言い直しへの相手の返事の報告の仕方（唯一の場所。言い直しを始めるとき・差し戻すとき＝_now の両方が使う）。質問の種類で、
        訂正された値に入れるものが違う：名前の読み＝聞いたとおりの音のひらがな／選択肢＝選択肢の名前／値＝直したあとの値の全部。
        ⛔ 2026-10-17 の見直しで見つけた漏れ：差し戻しのとき（_now）は、選択肢の質問でも「違う値を…訂正された値で」とだけ伝えていた"""
        q = self.questions[qid]
        ok, ng, corr = CONFIRM_LABEL["affirmed"], CONFIRM_LABEL["corrected"], ARG_LABEL["correction"]
        if q.reading_of:
            return (f"相手が認めたら confirm_field を{ARG_LABEL['result']}「{ok}」で、違う読みを言われたら「{ng}」で報告し、{corr}には"
                    "相手が言ったとおりの音をひらがなで入れます（漢字を思い浮かべて、知っている読みに直さない）。")
        if q.type in CHOICE_TYPES:
            return (f"相手が認めたら confirm_field を{ARG_LABEL['result']}「{ok}」で、違うと言われたら「{ng}」で報告し、{corr}には"
                    f"相手の答えに合う選択肢の名前（{'、'.join(q.labels)}）を入れます。")
        return f"相手が肯定したら confirm_field を{ARG_LABEL['result']}「{ok}」で、違う値を言ったら「{ng}」と{corr}で報告してください。"

    def _remaining(self) -> list[str]:
        return [qid for qid in self.order if self.items[qid].state == "empty"]

    def _callee_question(self) -> str | None:
        """いまの相手の発話の中の、相手からの質問（無ければ None）。相手の言葉の文字（会話AIの聞き取り）で、文の終わりが問いかけのもの。
        ⛔ G-47：相手が「はい、予約しています。当日はどなたが担当されるんですか？」と答えたのに、プログラムはすぐ次の質問の録音を流し、
           AIには「何も言わずに待って」と伝えた＝相手の質問が10本中10本、聞き流された。"""
        for sent in re.findall(r"[^。！!？?]+[。！!？?]?", self.callee_said or ""):
            t = sent.strip()
            if _CALLEE_ASKS.search(t):
                return t
        return None

    def _advance(self, prefix: str, handled: bool = False) -> dict:
        # 相手が質問をしていたら、録音は流さずに、AIがまず短く答えてから次へ進む（録音で流すと、相手の質問にかぶせることになる）。
        # handled＝相手の言葉そのものを、いまの操作が受けた（かけ直しの申し出「あとでかけてもらえますか？」・目的の人の名乗り）＝答えに添えた
        # 質問ではないので、答えさせない。⛔ 2026-10-19 G-57 の2回目：かけ直しの申し出の「〜もらえますか？」を相手からの質問として扱い、
        #    AIに「まず答えて」と渡したので、AIが「私の方では折り返しのお電話はお約束できかねます」と言い、足す質問も録音でなくAIが言った（6本中2本）
        asked = None if handled else self._callee_question()
        if asked:
            prefix += (f"相手が質問をしています（「{asked[:40]}」）。まず、答えられる範囲で短く答えます（分からないことは作らず、約束もしません）。"
                       "そのあと続けて、")
        rest = self._remaining()
        if self.phase == "asking" and rest:
            self.current = rest[0]
            q = self.questions[self.current]
            if q.reading_of:
                return self._enter_reading(q.id, prefix)
            if self.play_recordings and asked:
                # 録音を流す形でも、この質問だけは AIが言う。つなぎの言葉は、録音で流すときと同じ並び（spoken_question）から取る
                # （受けの言葉の数え方と混ぜると、同じ「それでは」が続きうる）
                text, lead = spoken_question(list(self.questions.values()), q.id)
                return {"result": "next", "question_id": q.id, "question": q.text,
                        "message": f"{prefix}次の質問は {q.id}「{text}」です。" + (f"「{lead}」から話し始めてください。" if lead else "")
                                   + choices_note(q)}
            if self.play_recordings:
                # かけ直しで足す質問も録音（⛔ 2026-10-19 G-57：AIに言わせると、3本中1本で「私の方では折り返しのお電話ができかねますので」と
                # 足し、もう1本でも「かしこまりました。」を足した＝決まった文は録音にする決まり（6-13）に戻した）
                play = question_play(list(self.questions.values()), q.id)
                text = play["text"]
                return {"result": "next", "question_id": q.id, "question": q.text, "play": [play],
                        "message": f"{prefix}次の質問 {q.id}「{text}」は、プログラムが録音で流しました（相手にもう聞こえています）。"
                                   "何も言わずに、相手の答えを待ってください。相手が答えたら record_answer で報告します。" + choices_note(q)}
            return {"result": "next", "question_id": q.id, "question": q.text,
                    "message": f"{prefix}次の質問は {q.id}「{q.text}」です。" + ("" if q.callback else self._lead_note("次の質問"))
                               + choices_note(q)}
        if self.phase == "asking":
            # 締めの前に、「-」の必須項目を1回だけ念のため確かめる（6-11）。急いでいる相手・かけ直しを頼んだ相手にはしない
            self.recheck_queue = [] if self.callback_why else [qid for qid in self.order
                                  if self.items[qid].state == "unresolved" and self.questions[qid].required
                                  and self.items[qid].rechecks < self.limits.recheck]
            self.phase = "rechecking"
        if self.phase == "rechecking" and self.recheck_queue and not self.hurried:
            self.current = self.recheck_queue.pop(0)
            self.items[self.current].rechecks += 1
            q = self.questions[self.current]
            # 締めの前の念のための確認＝その項目を聞く最後の1回（✅ 2026-10-14 ユーザー：最初の質問のほかに最大3回＝その場2回＋この1回）。
            # 言い方は聞き返しと同じ表（phrases.ask_again）の、最後の1回の言い方。相づちは「申し訳ございません、」（_ack）
            say = ask_again_phrase(self.items[self.current].ask_backs + 1, "unclear", q.topic, last=True)
            return {"result": "recheck", "question_id": q.id, "question": q.text, "say": say,
                    "message": f"{prefix}念のための確認です。{q.id} を、もう1回だけ「{say}」と聞いて、相手の答えを待ってください。"
                               "相手が答えたら record_answer で報告します。"}
        return self._close("callback" if self.callback_why else "done", prefix=f"{prefix}質問は終わりです。", asked=bool(asked))

    @property
    def hurried(self) -> bool:
        """相手が急いでいる・通話を断られた・怒っている、で締めたか（念のための確認をしない・締めは断られたときの言い方）。"""
        return self.end_reason in DECLINED_ENDS

    def _note(self, kind: str, **extra) -> None:
        """通話の事実を残す唯一の場所（self.facts）。根拠の発話＝相手の発話の番号（on_callee で数えた番号）と、その文字（会話AIの聞き取り）。
        Intent.md 9-5「各項目には、根拠になった発話（時刻つき）を保存する」・11-1「拒否・同意の記録（根拠の発話）」。時刻は電話接続側が
        発話の番号に付ける（試作では通話の記録の turns）。"""
        self.facts.setdefault(kind, []).append({"turn": self.callee_turns, "said": (self.callee_said or "").strip()[:80], **extra})

    def _close(self, reason: str, prefix: str = "", asked: bool = False, text: tuple[str, str] | None = None, part: bool = False) -> dict:
        """締め（DESIGN.md 6-14）を作る唯一の場所。毎回同じ文なので、言い方は決まった1つ（ふつう＝CLOSING・急いでいる／断られた／怒っている
        ＝CLOSE_DECLINED）。reason＝締めた理由（END_LABEL の中の名前。通話の事実の end に残す）。
        録音で流す形なら録音を流してAIは黙る（6-13：毎回同じ文の発話は録音）。AIが話す形と、相手の質問にAIが答えてから締めるときは、
        AIがその言い方のまま言う。⛔ 2026-10-17 の見直しで見つけた漏れ：ふつうの締めは、録音で流す形のときしか言い方を渡しておらず、
        AIが話す形では「締めの言葉を言って」とだけ伝えていた＝AIが自分で文を作り、「お気軽にお問い合わせください」「お気をつけて
        お越しくださいませ」（命令の形）を足した（G-45）。断られたときの締めは録音にしていなかった（「お忙しいところ」はAIの声で崩れやすい）。
        part：text の録音が依頼主の単位（名乗りの部分。留守番電話の伝言）。無ければ契約者の単位（締めの録音）。"""
        text, rid = text or ((CLOSING, "closing") if reason in ("done", "callback", "not_transferred") else (CLOSE_DECLINED, "closing_declined"))
        self.current, self.phase, self.end_reason = None, "closing", reason
        self._note("end", reason=reason)
        if self.play_recordings and not asked:
            item = {"kind": "opening", "part": rid, "text": text} if part else {"kind": "closing", "id": rid, "text": text}
            return {"result": "end", "play": [item],
                    "message": f"{prefix}締めの言葉「{text}」は、プログラムが録音で流しました。何も言わずに終えます。"}
        return {"result": "end", "say": text,
                "message": f"{prefix}「{text}」と言って終えてください。この言い方だけを使い、言葉を足しません"
                           "（またお電話します、などの約束もしません）。"}

    def _lead(self, kind: str) -> str:
        """受けの言葉（lead_ins が無効なら空）。種類ごとに順番に回す。"""
        if not self.lead_ins:
            return ""
        n = self.lead_counts.get(kind, 0)
        self.lead_counts[kind] = n + 1
        return lead_in_phrase(kind, n)

    def _lead_note(self, kind: str) -> str:
        lead = self._lead(kind)
        return f"「{lead}」から話し始めてください。" if lead else ""

    def _reject(self, why: str) -> dict:
        """差し戻す唯一の場所。理由が何であっても、いま求めていること（_now）を必ず添える。"""
        return {"result": "rejected", "message": f"{why}{self._now()}"}

    def _now(self) -> str:
        """いま求めていること（AIが次にすることを迷わないため）。
        ⛔ 2026-10-13 G-16 の2回目：Q2 の言い直しの返事を待っている間に Q3 を報告され、「いまの質問は Q2 です」とだけ返したため、
           AIは Q2 を最初から聞き直した（言い直しの返事を待っていることが、伝わっていなかった）。"""
        if self.ai_pending:
            return (f"いまは、エーアイかと聞かれて流した「{self.ai_said}」への、相手の返事を待っています。何も言わずに"
                    f"待ちます。相手が、問題は無いと言ったら（「いえ」「別に」「機械みたいだったので」など）、replay_opening を「{PART_LABEL['resume']}」"
                    f"で呼びます。重ねて聞かれたら（「本当に人ですか」など）、もう一度「{PART_LABEL['ai']}」で呼びます。相手がいまの質問に答えたら、"
                    "その答えを報告します。")
        if self.awaiting_confirm is not None:
            say = self.awaiting_say
            how = self._confirm_how(self.awaiting_confirm)
            if self.callee_turns > (self.confirm_requested_at or 0):
                # ⛔ G-46：報告の種類の名前を書き違えて差し戻されたとき、ここが「相手がまだ答えていなければ、もう一度聞いて」としか言わず、
                #    相手はもう「角田です。」と答えていたのに、AIは同じ確かめをもう一度声に出した（相手は同じことを2回聞かれる）。
                #    ⇒ 相手の返事が届いているなら、聞き直さずに、その返事を報告し直してもらう
                heard = f"（「{self.callee_said[:30]}」）" if self.callee_said else ""
                return (f"いまは {self.awaiting_confirm} の言い直し「{say}」への、相手の返事{heard}がもう届いています。"
                        f"もう一度聞かずに、何も言わないで、その返事を報告し直してください。{how}")
            return (f"いまは {self.awaiting_confirm} の言い直し「{say}」への、相手の返事を待っています。{how}"
                    f"相手がまだ答えていなければ、「{say}」と、もう一度聞いてください。")
        if self.end_reason in NOT_A_PERSON:
            return "通話は終わりです。何も言いません。"
        if self.phase == "calling" and self.asked_target and self.confirm_open and self.callee_turns <= self.asked_target_turn + 1:
            # ⛔ 2026-10-19 G-57（目的の人_本人）：確かめる一言を録音で流したあと、相手の「はい、そうです。」を、AIが最初の質問の答えとして
            #    報告し、差し戻しの「お取り次ぎいただくのを待っています」では、確かめへの返事を待っていることが伝わらなかった（3本中2本）
            # ⛔ 2026-10-21 G-57（目的の人_本人・取り次ぎ）：AIが先に「目的の人」と報告し、判定がそろわずに確かめたとき、AIは繋がったと思い込み、
            #    「はい、そうです。」のあと最初の質問へ進んだ（4本。名乗らない方＝AIが先に報告していないときは8本とも正しく報告した）
            held = f"（さきほどの「{PARTY_LABEL['target']}」の報告は、まだ受け付けていません）" if self.target_claimed else ""
            return (f"いまは、まだ目的の人に繋いでいません{held}。電話に出た方に「{self.asked_target}」と聞いて、返事を待っています。その方が認めたら（「はい」「そうです」など）、もう一度 report_party を"
                    f"「{PARTY_LABEL['target']}」で呼びます（名乗り・用件・最初の質問は、プログラムが流します）。まだ最初の質問はしていないので、"
                    "用件を話したり、record_answer で報告したりしません。目的の人ではなかったら（「いいえ」・違うお名前を名乗ったなど）、replay_opening を"
                    f"「{PART_LABEL['handover']}」で呼びます（お呼びし直す言い方は、プログラムが流します。番号違いではありません）。席を外している・"
                    f"出られないと言われたら、request_callback を「{CALLBACK_LABEL['absent']}」で呼びます。")
        if self.phase == "calling":
            return (f"いまは、目的の人（「{self.target['target']}」）にお取り次ぎいただくのを待っています。電話に出た方が目的の人だと分かったら、"
                    f"report_party を「{PARTY_LABEL['target']}」で呼びます（用件と最初の質問は、プログラムが返事で渡します）。"
                    "お取り次ぎいただく間は、何も言わずに待ちます。")
        if self.current is None:
            return "質問は終わりです。締めの言葉を言って終えてください。"
        q = self.questions[self.current]
        # ⛔ 2026-10-13 G-28：AIが答えを報告せずに言い直し、差し戻されたあと、「いまの質問は Q2 です」としか伝わらず、
        #    報告しないまま言い直しを繰り返した。⇒ いますることの操作まで書く
        return (f"いまの質問は {q.id}「{q.text}」です。相手が答えたら record_answer で報告してください"
                f"（聞き取れなかったら report_unclear）。")

    # ── 依頼主へ渡す書き方（7-1b。Q20 の唯一の場所）
    def answer_of(self, qid: str) -> str | None:
        """取れた項目の答え（依頼主へ渡すもの・張り替えのときにAIへ渡す「聞けたこと」の両方。唯一の場所）。選択肢の型は、宣言された選択肢
        （相手の言葉そのものではない。任された答えは「どちらでも」）、それ以外は値。"""
        it = self.items[qid]
        if self.questions[qid].type in CHOICE_TYPES or it.value in (None, ""):
            return label(it.condition)
        return it.value

    def export(self, unknown_mark: str = "-", refused_mark: str = "✕") -> dict:
        """依頼主へ渡す値（DESIGN.md 7-1b）。言い切っていない返事で受け取った値も、値はそのまま（お客様の言葉は hedged_words の別の欄）。"""
        out = {}
        for qid, it in self.items.items():
            if self.questions[qid].callback:
                continue  # かけ直しの申し出の答えは、通話の事実（facts の callback）で渡す
            if it.state == "refused":
                out[qid] = refused_mark
            elif it.state in GOT:
                out[qid] = self.answer_of(qid)
            else:
                out[qid] = unknown_mark
        return out


    def hedged_words(self) -> dict:
        """依頼主へ渡す別の欄：言い切っていない返事で受け取った項目の、お客様の言葉そのまま（DESIGN.md 7-1b）。
        ✅ 2026-10-18 ユーザー：案C（「ご予約」の欄は「していない」・別の欄にお客様の言葉「していないはずですが…」）が分かりやすい"""
        return {qid: it.hedged for qid, it in self.items.items() if it.state in GOT and it.hedged and not self.questions[qid].callback}


def tool_declarations(questions: list[Question], replay: bool = False, target: bool = False, answers: tuple = ()):
    """AIが呼べる操作（6-2）の宣言を作る唯一の場所。条件の選択肢は、質問の宣言＋システムの2つ。
    target：目的の人のある業務か（相手の種別に、受付・目的の人を足す。DESIGN.md 4-3）。
    ⛔ 2026-10-14 G-33：答えの報告を「返事を待たない」宣言にする形は採らない（AIが受けの言葉を先に言わなかった）。"""
    from google.genai import types

    def s(enum=None, desc=None):
        return types.Schema(type=types.Type.STRING, enum=enum, description=desc)

    # かけ直しの申し出で足す質問（C1・C2）は、どの業務でも報告できるように、初めから宣言に入れる
    added = [callback_question(r) for r in ("back_time", "number")]
    qids = [q.id for q in questions] + [q.id for q in added]
    conds = sorted({c for q in questions + added for c in q.labels})
    A = ARG_LABEL

    def fd(name, desc, props, required):
        # 項目と「必ず要る項目」は、受け取ったときの確かめと同じ表（OP_ARGS）から作る
        req, opt = OP_ARGS[name]
        assert set(props) == {A[k] for k in req + opt} and required == [A[k] for k in req], name
        return types.FunctionDeclaration(name=name, description=desc,
                                         parameters=types.Schema(type=types.Type.OBJECT, properties=props, required=required))

    return [types.Tool(function_declarations=[
        fd("record_answer", "相手の答えを、いまの質問の選択肢に分類して報告する。"
           f"相手が「わからない」と答えたら「{SYSTEM_LABEL['unknown']}」、答えを拒んだら「{SYSTEM_LABEL['refused']}」、"
           f"選択肢の質問で、どれでもよいとこちらに任されたら（「どちらでも」「どれでも」）「{ANY_LABEL}」（まだ決めていないなら「{SYSTEM_LABEL['unknown']}」）",
           {A["question_id"]: s(qids), A["condition_id"]: s(conds), A["value"]: s(desc="聞き取った値（相手の言葉のとおり）")},
           [A["question_id"], A["condition_id"]]),
        fd("report_unclear", "相手の答えが聞き取れない・曖昧だったことを報告する",
           {A["question_id"]: s(qids)}, [A["question_id"]]),
        fd("confirm_field", "言い直した値への、相手の返事を報告する（相手が返事をしてから呼ぶ）",
           {A["question_id"]: s(qids), A["result"]: s(list(CONFIRM_LABEL.values())), A["correction"]: s(desc="直したあとの値（全部）。選択肢の質問では、相手の答えに合う選択肢の名前")},
           [A["question_id"], A["result"]]),
        # 「エーアイか聞かれた」「質問に戻る」は、名乗りの録音が無い通話でも使う（決まった言い方とそのあとは流れの制御が決める＝Flow._ai_question）
        # 「代わった方へ」は、目的の人のある業務では録音が無くても使う（目的の人を呼んでいる段で、代わった方を知らせる＝Flow._calling_reported）
        fd("replay_opening", ("名乗りや用件を聞き返されたとき、自分では言わずに、録音で流し直してもらう（どちら様＝"
                              f"「{PART_LABEL['intro']}」、ご用件＝「{PART_LABEL['purpose']}」、聞き取れなかった様子＝「{PART_LABEL['all']}」、"
                              f"別の人に代わった＝「{PART_LABEL['handover']}」）。" if replay else
                              f"目的の人をお呼びしているときに、別の人に代わった＝「{PART_LABEL['handover']}」。" if target else "")
           + f"エーアイか・人間かと聞かれた＝「{PART_LABEL['ai']}」（何と言うかはプログラムが決めます）。その言い方に、相手が問題は無いと答えた"
           f"＝「{PART_LABEL['resume']}」。相手からそのほかの質問をされた＝想定問答にある問いならその名前"
           + "".join(f"（「{n}」）" for n in answers) + f"、無ければ「{PART_LABEL['unknown']}」（こちらが答えを持っていない問い。用件では答えず、"
           "自分の言葉でも答えません）",
           {A["part"]: s([v for k, v in PART_LABEL.items() if replay or k in ("ai", "resume", "unknown") or (target and k == "handover")]
                         + list(answers))},
           [A["part"]]),
        fd("report_party", "電話に出たのが人ではないとき（留守番電話の案内・自動音声の案内・ファクスの音）や、番号違い（かけた先とは別の人・別の会社）"
           "だったときに知らせる" + ("。受付の方が出た・目的の人に代わったときも知らせる" if target else ""),
           {A["party"]: s([PARTY_LABEL[k] for k in (PERSON_PARTIES if target else ()) + NOT_A_PERSON + ("wrong_party",)])}, [A["party"]]),
        fd("request_callback", "相手が、かけ直してほしいと言ったときに呼ぶ。話すべき方がいない・出かけている＝"
           f"「{CALLBACK_LABEL['absent']}」、別の電話番号へかけてほしい＝「{CALLBACK_LABEL['other_number']}」、"
           f"いまは都合が悪いのであとでかけてほしい＝「{CALLBACK_LABEL['later']}」",
           {A["why"]: s(list(CALLBACK_LABEL.values()))}, [A["why"]]),
        fd("request_end", f"電話を終えたいときに呼ぶ。相手が急いでいる・切ろうとしているなら「{END_LABEL['callee_hurry']}」、"
           f"通話自体を断られた（「営業はお断りしています」「もうかけないで」など）なら「{END_LABEL['callee_refused_call']}」、"
           f"相手が怒っている・苦情を言っているなら「{END_LABEL['callee_complaint']}」、受付の方が営業のお断りではない事情で取り次げない"
           f"と言ったなら「{END_LABEL['not_transferred']}」、質問が全部終わったなら「{END_LABEL['done']}」",
           {A["reason"]: s(list(END_LABEL.values()))}, [A["reason"]]),
    ])]
