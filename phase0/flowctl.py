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
import unicodedata
import dataclasses
import functools
from dataclasses import dataclass, field

from names import hira, quote, unvoiced
from phrases import SENSE_NONE, sense_ask, AI_ASKED_AGAIN, AMPM_ASK, CALLBACK_ASK, CALLBACK_ASK_AGAIN, CALLBACK_ASK_ANSWERING, CALLBACK_ASK_CHOSEN, CALLBACK_ASK_WHICH, CALLBACK_ASK_WHICH_RECAP, CALLBACK_ASK_RESUMED, CLOSE_DECLINED, CLOSING, CLOSING_AFTER_DECLINE, LEAD_IN, NAME_READING_ASK
from phrases import CALLBACK_ASK_FORM, CALLBACK_ASK_REUSED, CALLBACK_ASK_OTHER_WAYS, CALLBACK_ASK_OTHER_WAYS_AGAIN, POINT_KINDS, RESUMED_FALLBACK_LEAD, recap_line
from phrases import APOLOGY_CUT_END, QUESTION_RESUME_LEAD, TOLD_AGAIN, TOLD_FOLLOW_UP, WAY_ANY_ANNOUNCE
from phrases import LEAD_PIECES, OTHERS_HEAD, back_fit, closes, drop_receipt, head_join, head_word, heads_said
from phrases import APOLOGY_FIRST, APOLOGY_SAME_DAY, OFFER_TAKEN, WISH_WAY_TOPIC, APOLOGY_PURPOSE, BACK_LINES, DEPARTED_ACK, HOLD_TIMEOUT, WAIT_REPLY, MOSHIMOSHI, NUMBER_AGAIN, OFFER_THANKS, OWN_FALLBACK, point_fallback, point_fallback_id, SILENCE_PROMPT, SILENCE_PROMPT_IDS, SUCCESSOR, SUCCESSOR_IDS, TALK_FALLBACK
from phrases import RECORDED, STANDARD_PARTS, ack_pause_s, ask_again as ask_again_phrase
from phrases import CHANGE_BY, CHANGE_BY_AGAIN, TENTATIVELY, confirm as confirm_phrase, restate_words
from phrases import ACK, OTHER_WISH_AGAIN, OTHER_WISH_AGAIN_PLAIN, OTHER_WISH_ASK, OTHER_WISH_ASK_PLAIN, OTHER_WAYS_ASK, OTHER_WAYS_ASK_AGAIN, OTHER_WAYS_OFFER, OTHER_WAYS_OFFER_AGAIN, OTHER_WAYS_WHICH, WISH_TOLD
from phrases import lead_in as lead_in_phrase
from phrases import HANDOFF_DO, OFFER_DO_END, WISH_DO, offer_words
from phrases import ESTIMATE_ASK, HESITATE_POINT, HOW_NOW
from units import supply_unit

# 型の一覧（Q19）：その場で言い直して確かめる型。DESIGN.md 4-4
# line_id＝LINE の ID（✅ 2026-11-03 日本時間20時48分 ユーザー：ほかの方法に LINE を言っていただいたら、その場で伺う＝Flow._wish_step）
# mobile＝携帯電話の番号（SMS が届く番号。✅ 2026-11-04 日本時間19時26分 ユーザー：SMS でのご招待を選ばれたら、携帯電話番号を伺っていなければ伺う＝Flow._line_how）
CONFIRM_TYPES = {"date", "time", "datetime", "number", "amount", "name", "phone", "email", "address", "line_id", "mobile"}
# 選択肢で答える型：依頼主へは、相手の言葉ではなく、宣言された選択肢を渡す（7-1b）
CHOICE_TYPES = {"yes_no", "choice"}
# 型の一覧（Q19）の全部。確かめない型は、選択肢の型と、自由な意見（free_text。2026-10-13 CL-2 の依頼で名前を決めた）
TYPES = CONFIRM_TYPES | CHOICE_TYPES | {"free_text"}
SYSTEM_CONDITIONS = ("unknown", "refused")
# 選択肢の質問（choice）だけにシステムが足す答え：相手がこちらに任せた（「どちらでも大丈夫です」）。✅ 2026-10-17 ユーザー：答えとして受ける
# （「いつでも」と同じ＝こちら次第のよい答え。「わからない」にしない）。⛔ G-14 のいまの形：AIは「どちらでも大丈夫です」を「わからない」3本・
# 「カード」2本・「答えたくない」1本と、合う選択肢が無いので分けた。はい／いいえの質問には足さない
ANY = "any"
# 選択肢の質問（choice）だけにシステムが足す答え：どの選択肢も要らない・どれもしないでほしい（「連絡自体控えてほしいです」「どちらもお断りします」）。
# はい／いいえの質問は「いいえ」がこの役なので足さない。業務の最終目的の質問なら、断る答え（Flow._goals_lost）＝その業務はかけない。そうでなければ
# 答えとして受け取って続ける（依頼者へは「どれも要らない」）。✅ 2026-11-02 日本時間4時16分 ユーザー：「連絡しないと最終目的を果たせないような流れ
# だったのなら営業拒否になるし、登録など別の手段が目的の営業であれば電話やメールは控えるという意味合いになるんじゃないですか。」・(a)「OK」。
# ⛔ それまでは「答えたくない」に入れるしかなく、最終目的の質問でも断られたと数えなかった（「答えたくない」は断る答えではない）
NONE = "none"
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
_KANJI_BIG = {"万": 10 ** 4, "億": 10 ** 8}
# 数を数える言葉の前の、漢数字を1つ以上含む数の並び（算用数字とのまじり「5万」「3万5千」も1つの数）。小数・桁区切りの途中（「1.5万」）と
# 「数十」「何十」のあとからは数に直さない（読み違えるより、お客様の言葉のまま）。⛔ 2026-11-08 日本時間5時台に見つけた：並びが漢数字だけで、
# 算用数字のあとの「万」だけを1つの数と読み、「5万円」を「510000円」・「50万円」を「5010000円」にしていた（10-22 から）
_NUM_CHARS = "0-9０-９〇零一二三四五六七八九十百千万億"
_KANJI_NUMBER = re.compile(rf"(?<![0-9０-９.,．，数何])[{_NUM_CHARS}]*[〇零一二三四五六七八九十百千万億][{_NUM_CHARS}]*"
                           r"(?=[日時分秒月年週人名歳才個枚台件回円階泊杯本部号かヶケ])")


def _kanji_text(s: str) -> str:
    """漢数字を含む1つの数の並びを、数字の書き方にする唯一の場所（「十五」15・「二十」20・「三万五千」「3万5千」35000・「5万」50000・
    「二〇二六」2026）。位の言葉（十・百・千・万・億）の無い漢数字の2つ以上の並びは位取りではない（「二三日」＝2〜3日。〇を含む「二〇二六」だけ
    位取り）：続いた2つの数は範囲（「2〜3」）、ほかは読めないのでそのまま残す（CL-2 の時刻の読み spoken_time._kanji_number の 10-19 の決まりと同じ。
    ⛔ 11-08 5時台まで「二三日」を「23日」と読んでいた）。"""
    s = unicodedata.normalize("NFKC", s)

    def digits_of(seg: str) -> list[int]:
        return [_KANJI_DIGIT[c] if c in _KANJI_DIGIT else int(c) for c in seg]

    def positional(seg: str) -> bool:  # 位の言葉の前の数字の並びを位取りで読めるか（漢数字2つ以上の並びは〇を含むときだけ）
        return sum(c in _KANJI_DIGIT for c in seg) < 2 or any(c in "〇零" for c in seg)

    total = section = 0
    seg = ""
    for ch in s:
        if ch in _KANJI_UNIT or ch in _KANJI_BIG:
            if not positional(seg):
                return s
            n = int("".join(map(str, digits_of(seg)))) if seg else None
            if ch in _KANJI_UNIT:
                section += (1 if n is None else n) * _KANJI_UNIT[ch]
            else:
                total += ((section + (n or 0)) or 1) * _KANJI_BIG[ch]
                section = 0
            seg = ""
        else:
            seg += ch
    if not positional(seg):
        d = digits_of(seg)
        return f"{d[0]}〜{d[1]}" if not total and not section and len(d) == 2 and d[1] == d[0] + 1 else s
    return str(total + section + (int("".join(map(str, digits_of(seg)))) if seg else 0))


def _kanji_to_digits(v: str) -> str:
    def repl(m: re.Match) -> str:
        if m.group() == "十" and v[m.end()] == "分" and (m.start() == 0 or v[m.start() - 1] != "時"):
            return m.group()
        return _kanji_text(m.group())
    return _KANJI_NUMBER.sub(repl, v)


# 待ってほしいと言われた長さ（「15分ほどお待ちいただけますか」「あと5分で会議が終わりますので」「1時間半」）。「十分ほど」の「十分」は長さ
# （「十分です」＝足りる、とは分ける：後ろに「ほど・くらい・ぐらい・程度・前後・で・お待ち」が続くときだけ）
# 時刻の「2時10分」は長さではない（「時」のすぐあとの分は数えない）
# 「1時間30分」は1つの長さ（⛔ 2026-10-24 の見直し（調べ役）：「1時間」と「30分」を別々に拾い、長いほうの60分で締めていた）
_WAIT_LEN = re.compile(r"(?<![時\d])(\d+)\s*時間(?:(半)|\s*(\d+)\s*分)?|(?<![時\d])(\d+)\s*分|(半)\s*時間")
_TEN_MINUTES = re.compile(r"(?<![〇一二三四五六七八九十百時])十分(?=\s*(?:ほど|くらい|ぐらい|程度|前後|で(?!す)|お待ち|待))")


def wait_seconds(text: str | None) -> int | None:
    """相手の言葉の中の、待ってほしい長さ（秒。無ければ None）。保留の待ちの長さを決める唯一の場所（Flow._hold）。いくつか言われたら長いほう。"""
    t = _TEN_MINUTES.sub("10分", unicodedata.normalize("NFKC", text or ""))
    t = _kanji_to_digits(t)
    found = []
    for m in _WAIT_LEN.finditer(t):
        if m.group(1):
            found.append(int(m.group(1)) * 3600 + (1800 if m.group(2) else int(m.group(3) or 0) * 60))
        elif m.group(4):
            found.append(int(m.group(4)) * 60)
        else:
            found.append(1800)
    return max(found) if found else None


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
    if qtype in ("email", "line_id"):
        v = re.sub(r"\s", "", unicodedata.normalize("NFKC", v))  # 「info @ example.co.jp」「ｉｎｆｏ＠…」⇒ 依頼主へ渡す形（半角・空白なし）
    if qtype in NO_SPACE_TYPES:
        v = _SPACE.sub("", v)
        if qtype == "number":
            v = supply_unit(v, unit)
        v = _kanji_to_digits(v)
    return v or None
GOT = ("confirmed", "accepted")  # 「取れた」（4-4）
TAKEN = GOT + ("dont_know", "refused")  # AIの報告を受け取った状態（受け取った時に決めた＝あとに届いた確かめは記録だけ。Flow._late_to_taken）

# 値の形の確かめ（型の一覧の決まり。DESIGN.md 6-2。✅ 2026-10-14 ユーザー承認）：日付・時刻・日時の型の値に、日付・時刻の言葉が無く、
# 別の型の値（数「3人」・返事の言葉「はい」・こちらの名前「答えなかった」）になっているときだけ止める。
# ⛔ G-34：AIが答えを報告せずに言い直したあと、次の質問の答え「3人」が日時の答えとして確かめまで済んだ。
# ✅ 2026-10-14 ユーザー：「『いつでも』『なるべく早く』は『即』ということなので、営業としては良いこと。日時よりも良い。こちら次第と
#    いうこと。『-』はありえない」⇒ 日時を決めない答えは、日時の質問の答えとして受け付ける（止めるのは、別の型の値だけ）。
# これまでの試験の値に当てて、取り違え 25回中25回を止め、正しい答え 672回中0回を止める（form_rule_eval.py。2026-10-14 の当て直し）。
# 数・金額の型は、数字か数の言葉が1つも無い値だけ止める（form_problem。✅ 2026-10-14 ユーザー承認）。名前などの型には当てない
DATE_TIME_TYPES = {"date", "time", "datetime"}
NUMBER_TYPES = {"number", "amount"}
# 値を伺う型のうち、最初の答えの「まだ決まっていない」に1回だけ目安を伺う型（唯一の組＝Flow._hesitation の estimate。日時・数・金額＝おおよその目安で
# 答えられる値。名前・連絡先は入れない）。✅ 2026-11-08 日本時間2時21分15秒 ユーザー「(1)〜(4)ともイでOK」の (1)
ESTIMATE_TYPES = DATE_TIME_TYPES | NUMBER_TYPES
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


_EMAIL = re.compile(r"[^@\s]+@[^@\s.]+(?:\.[^@\s.]+)+")  # メールアドレスの形（宛名@宛先。宛先はドットで区切る）
_LINE_ID = re.compile(r"[A-Za-z0-9._-]{4,20}")  # LINE の ID の形（form_problem）
# 番号の種類の決まり（上から順に最初に当たったもの。(頭の数字, けた数, 種類)。どれにも当たらない10けた（2けた目が0でない）は固定電話）。「この番号は、
# どの番号か」の正本は CL-2 の番号の正規化（app/src/salescall/core/phone.py の _RULES＝DESIGN.md 2章 Q1）で、これはその写し（会話の側は CL-2 の部品を
# 読み込まない）＝食い違えば試験が落ちる（NumberRulesMatchCl2Test）。⛔ 2026-11-05 日本時間2時05分37秒のご質問のあとの読み直しで見つけた：会話の側は
# 「0 で始まる10けたから」・「060・070・080・090 で始まる11けた」の別の決まりで見ていて、けたの足りない番号（「090-1234-567」）を、CL-2 ならかけられない
# 番号なのに電話番号として受け取り、携帯電話の番号ではないとして SMS の理由を添えて伺い直していた
NUMBER_RULES: tuple[tuple[str, int, str], ...] = (
    ("0120", 10, "toll_free"), ("0800", 11, "toll_free"), ("0570", 10, "navi_dial"), ("0180", 10, "premium"), ("0990", 10, "premium"),
    ("020", 11, "m2m"), ("050", 11, "ip"), ("060", 11, "fmc"), ("070", 11, "mobile"), ("080", 11, "mobile"), ("090", 11, "mobile"),
)
SMS_KINDS = frozenset({"mobile", "fmc"})   # SMS が届く番号（060・070・080・090 で始まる11けた＝✅ 11-04 19時26分のご承認の言い方）


def number_kind(value: str | None) -> str | None:
    """お客様が言った番号の種類（NUMBER_RULES の名前か "fixed"）。日本の番号の決まりに当てはまらなければ None（けたが合わない・0 で始まらない・数字の
    ほかの言葉がある）＝電話番号の形の確かめ（form_problem）・携帯電話の番号か（is_mobile）・種類の違う番号か（wrong_number_kind）の唯一の決まり。
    数字のあいだの区切り（ハイフン・空白・かっこ）は外す。国番号は「+81」と書いたときだけ（CL-2 と同じ）。"""
    if not value:
        return None
    text = unicodedata.normalize("NFKC", value).strip()
    compact = re.sub(r"[\s\-‐‑‒–—―−ー()（）.]", "", text)
    if compact.startswith("+81") and compact[3:].isdigit():
        rest = compact[3:]
        compact = "0" + (rest[1:] if rest.startswith("0") else rest)
    if not compact.isdigit() or not compact.startswith("0"):
        return None
    for prefix, length, kind in NUMBER_RULES:
        if compact.startswith(prefix):
            return kind if len(compact) == length else None
    return "fixed" if len(compact) == 10 and compact[1] != "0" else None


def is_mobile(value: str | None) -> bool:
    """携帯電話の番号（SMS が届く番号）か（唯一の決まり＝値の形の確かめ form_problem・伺い済みかを見る Flow._known_contact）。"""
    return number_kind(value) in SMS_KINDS


def wrong_number_kind(qtype: str, value: str | None) -> bool:
    """日本の電話番号としては合っているが、その質問が伺う番号の種類ではない（SMS でご招待する携帯電話の番号に、固定電話・フリーダイヤル・IP 電話の番号）か
    （唯一の決まり＝form_problem の理由の言葉・理由を添えて伺い直すか＝Flow._bad_form）。けたの足りない番号（聞き違い）・「はい」は当たらない＝ふつうの
    聞き返し。⛔ 2026-11-05 日本時間2時05分37秒のご質問のあとの読み直しで見つけた：どの形の違いにも「恐れ入りますがSMSは携帯電話の番号に…」を添え、2回目で
    聞き返さずにほかの方法へ進んでいた（携帯電話をお持ちで、けたが聞き取れなかっただけでも）"""
    return qtype == "mobile" and number_kind(value) not in (None, *SMS_KINDS)


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
    if qtype == "email" and not _EMAIL.fullmatch(re.sub(r"\s", "", unicodedata.normalize("NFKC", value))):
        # メールアドレスの形（〇〇@〇〇.〇〇）でない：⛔ 2026-10-22 G-57 tc34・tc35（メールで・S2）：相手は「info @ example.co.jp」と言い、
        # AIも声では言い直したのに、報告の値は「info@example」（宛先が途中まで）で、形の確かめが無かったので通っていた（Intent.md 9-5
        # 「相手の肯定と形式の検査がそろったときだけ確認済み」の形式の検査が、メールには入っていなかった）
        return "メールアドレスの形（英字などの宛名・@・ドットで区切った宛先）ではありません"
    if qtype == "line_id" and not _LINE_ID.fullmatch(re.sub(r"\s", "", unicodedata.normalize("NFKC", value))):
        # LINE の ID の形（LINE の決まり：英字の小文字・数字・ピリオド・下線・ハイフンの4〜20字。ID を設定していない方が言う、LINE に登録した電話番号の形も入る）
        return "LINE の ID の形（英字・数字・ピリオド・下線・ハイフンの4〜20字）ではありません"
    if qtype == "phone" and number_kind(value) is None:
        # 電話番号（ファクス番号も）の形：日本の番号の決まり（number_kind＝CL-2 の番号の正規化の写し）に当てはまる。⛔ 2026-11-04 日本時間21時台に見つけた：
        # 電話番号の型には形の確かめが無く、「ファックスで」のような値も言い直していた（Intent.md 9-5「相手の肯定と形式の検査がそろったときだけ確認済み」）。
        # ⛔ 11-05 2時05分37秒のご質問のあとの読み直しで見つけた：「0 で始まる10けたから」だけを見て、けたの合わない番号（CL-2 がかけられない）も通していた
        return "電話番号の形（0 で始まる、日本の番号のけた）ではありません"
    if qtype == "mobile" and not is_mobile(value):
        # SMS でご招待する携帯電話の番号：SMS が届かない番号（固定電話・フリーダイヤルなど）は、言い直さずに聞き直す（理由を添えるのは、電話番号としては
        # 合っている、その種類でない番号だけ＝wrong_number_kind・Flow._bad_form）
        if wrong_number_kind(qtype, value):
            return "電話番号ですが、携帯電話の番号（060・070・080・090 で始まる11けた）ではありません（SMS が届かない）"
        return "携帯電話の番号の形（060・070・080・090 で始まる11けた）ではありません"
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
# ✅ 2026-10-24 ユーザー（営業時間とは別に、業務ごとの「お客様指定の歯止め時間」ができたため）：「営業時間+歯止め時間内に、午前と午後の両方が
#    入っている場合は午前か午後を必ず『伺う』。営業時間+歯止め時間外がある時間は、時間内の時間を採用し、(24時間方式+午前午後で)『○○時ですね』と
#    復唱する。午前と午後を確かめたうえで、お客様の要望時間は、何時でも『かける』」「閉店ちょうどや開始ちょうどは時間内に含める」・どちらも外なら
#    伺う・12時台は「昼の12時」（夜中は「午前0時」）・復唱は「（日時）でよろしいでしょうか？」のまま言い方だけ（✅ CL-1 の提案「OK」）
# ⇒ 時刻（1〜12時）に午前・午後を表す言葉が無いとき：営業時間か歯止め時間の、どちらかに入れば時間内（端ちょうども入れる・分まで見る）。
#    片方だけが時間内なら、その側を値に補う（言い直しで相手が確かめられる＝spoken_clock）。両方とも時間内・どちらも時間外なら、プログラムが
#    1回だけ聞く（AMPM_ASK）。答え（「午後です」）は、前の値に入れる。⚠️ CL-2 の時刻の読み（spoken_time）と同じ決まり（CL-1.md 4-31）
AMPM_TYPES = {"time", "datetime"}
DEFAULT_BUSINESS_HOURS = (9, 19)  # お客様の営業時間の初めの値（会社 9:00〜19:00。業務ごとの設定 callee.business_hours。日をまたいでよい）
DEFAULT_DESIGNATED_LIMIT = (7, 22)  # お客様指定の歯止め時間の初めの値（7:00〜22:00。業務ごとの設定 callee.designated_limit）
AMPM_WORDS = re.compile(r"午前|午後|朝|昼|夕方|夕刻|夜|晩|深夜|夜中|明け方|正午")
_HOUR = re.compile(r"(?<!\d)(\d{1,2})時")
_CLOCK = re.compile(r"(?:(?P<part>午前|午後|朝|昼|夕方|夕刻|夜|晩|深夜|夜中|明け方)の?)?(?<!\d)(?P<h>\d{1,2})時(?:(?P<half>半)|(?P<min>\d{1,2})分)?")


def _minutes(r: tuple) -> tuple[int, int]:
    """時間の範囲を、0時からの分の (始まり, 終わり) に（(9, 19) の時・"09:00" の形のどちらでも）。"""
    def one(x):
        if isinstance(x, str):
            h, m = x.split(":")
            return int(h) * 60 + int(m)
        return int(round(x * 60))
    return one(r[0]), one(r[1])


def _inside(minute: int, r: tuple) -> bool:
    """その時刻が範囲に入るか（始まりちょうど・終わりちょうども入る。日をまたいでよい）。"""
    a, b = _minutes(r)
    return a <= minute <= b if a <= b else (minute >= a or minute <= b)


def fill_ampm(value: str | None, hours: tuple = DEFAULT_BUSINESS_HOURS, limit: tuple = DEFAULT_DESIGNATED_LIMIT) -> tuple[str | None, bool]:
    """時刻の値に午前・午後が無いとき、営業時間と歯止め時間から補う。返り値＝(値, 聞く必要があるか)。言い直しの言い方の形で報告された値は、
    もとの値に戻してから（plain_clock。値はいつも、もとの形で持つ）。"""
    value = plain_clock(value)
    if not value or AMPM_WORDS.search(value) or not (m := _CLOCK.search(value)):
        return value, False
    h = int(m.group("h"))
    if h == 0 or h > 12:
        return value, False
    minute = 30 if m.group("half") else int(m.group("min") or 0)
    am = (0 if h == 12 else h) * 60 + minute
    am_in, pm_in = (any(_inside(x, r) for r in (hours, limit)) for x in (am, am + 720))
    if am_in == pm_in:
        return value, True
    if h == 12:   # 「12時」：昼はそのまま（「午後12時」とは言わない）・夜中は「午前0時」
        return (value, False) if pm_in else (value[:m.start()] + "午前0" + value[m.start() + 2:], False)
    return value[:m.start()] + ("午前" if am_in else "午後") + value[m.start():], False


_CLOCK_TAIL = re.compile(r"(?:頃|ごろ|くらい|ぐらい|以降|以後|過ぎ|すぎ|までに|まで|前後|ちょうど|台)*")


def plain_clock(value: str | None) -> str | None:
    """言い直しの言い方の形（spoken_clock の形「15日の14時、午後2時」）になっている時刻の値を、もとの値（「15日の午後2時」）に戻す（唯一の場所。
    その形でなければ、そのまま）。値はいつも、もとの形で持つ（言い方は、そこから spoken_clock で作るだけ）。
    ⛔ 2026-10-24 G-57 J5（言い間違えの直し・L4）：AIが、こちらの言い直しの言い方をそのまま「訂正された値」として報告し、プログラムは
       別の値と取り違えて念のためもう一度確かめた。そのとき言い方の形にもう一度言い方を当てたので「15日の14時、14時、午後2時、2時」と崩れた"""
    ms = list(_CLOCK.finditer(value or ""))
    i = value.find("、", ms[0].end()) if len(ms) >= 2 else -1
    if i < 0:
        return value
    base = value[:ms[0].start()] + value[i + 1:]
    return base if _speak_clock(base) == value else value


def spoken_clock(value: str | None) -> str | None:
    """時刻の値を、言い直しで言う形にする（唯一の場所。値そのものは変えない。もう言い方の形になっている値は、そのまま＝plain_clock）。"""
    return _speak_clock(plain_clock(value))


def _speak_clock(value: str | None) -> str | None:
    """時刻の値を、言い直しで言う形にする（spoken_clock の中身）。午後は24時間＋午後の両方、午前は「午前○時」、昼の12時台は
    「昼の12時」（✅ 2026-10-24 ユーザー：「確認の言葉は、24時間方式（午前は、午前○○時、午後は○○(24時間)時+(午後○○時の両方)を使う」）。
    例：「15日の午後2時」⇒「15日の14時、午後2時」・「午後3時から5時の間」⇒「15時から17時、午後3時から5時の間」。午前か午後かが決まって
    いない時刻（午前・午後を聞いても決まらなかった）は、そのまま。"""
    ms = list(_CLOCK.finditer(value or ""))
    if not ms:
        return value
    times, half = [], None   # 0時からの分と、午前・午後（前の時刻から引き継ぐ＝「午後3時から5時」の5時）
    for m in ms:
        h, part = int(m.group("h")), m.group("part")
        minute = 30 if m.group("half") else int(m.group("min") or 0)
        if h > 23 or minute > 59 or (part and h > 12):
            return value
        if h >= 13:
            h24 = h
        elif part in ("午後", "夕方", "夕刻", "夜", "晩"):
            h24 = 0 if h == 12 and part in ("夜", "晩") else (h if h == 12 else h + 12)
        elif part in ("午前", "朝", "明け方"):
            h24 = h % 12
        elif part == "昼":
            h24 = 12 if h == 12 else h + 12
        elif part in ("深夜", "夜中"):
            h24 = 0 if h == 12 else (h if h <= 5 else h + 12)
        elif h == 12:
            h24 = 12   # 「12時」は昼の12時（fill_ampm の決まり）
        elif half is not None:
            h24 = h if half == "am" else h + 12
        else:
            return value   # 午前か午後かが決まっていない
        half = "am" if h24 < 12 else "pm"
        times.append(h24 * 60 + minute)
    tail = _CLOCK_TAIL.match(value, ms[-1].end()).group()
    head, rest = value[:ms[0].start()], value[ms[-1].end() + len(tail):]

    def clock(t: int, h: int) -> str:
        mm = t % 60
        return f"{h}時" + ("半" if mm == 30 else f"{mm}分" if mm else "")

    def span(render) -> str:
        out, pos = "", ms[0].start()
        for m, t, prev in zip(ms, times, [None] + times[:-1]):
            out += value[pos:m.start()] + render(t, prev)
            pos = m.end()
        return out + tail

    def twelve(t: int, prev: int | None) -> str:
        h24 = t // 60
        if h24 == 12:
            return "昼の" + clock(t, 12)
        word = "午前" if h24 < 12 else "午後"
        same = prev is not None and (prev // 60 < 12) == (h24 < 12) and prev // 60 != 12
        return ("" if same else word) + clock(t, h24 % 12)

    if all(t // 60 <= 12 for t in times):
        return head + span(twelve) + rest   # 午前と昼の12時台だけ＝24時間の形は言わない
    return head + span(lambda t, prev: clock(t, t // 60)) + "、" + span(twelve) + rest


WEEKDAY_WORDS = "月火水木金土日"
DAY_SLOT = "{最短の日}"   # 業務設定の質問の文・入る一言に書く、最短で対応できる日の場所（Flow が day_words で置き換える）


def day_words(earliest, today) -> str:
    """最短で対応できる日（業務設定の「3営業日以降」などを、CL-2 の暦＝営業日と祝日の1か所が日にちにしたもの）を、電話の日から見た言い方にする
    （唯一の場所。言い切っていない時期の答えのあとに伺う質問の「（{最短の日}なら対応できますが、）いつ頃がよろしいでしょうか？」＝Question.ask_if_hedged）。
    当日＝本日以降・翌日＝明日以降・翌々日＝明後日以降・同じ週＝今週の〇曜日以降・次の週の月曜日＝来週以降・次の週のほかの曜日＝来週の〇曜日以降・
    それより先は、同じ月なら〇日以降、違う月なら〇月〇日以降（週は月曜日から）。✅ 2026-11-03 ユーザー：「「○○日以降」は、「明日以降」「来週以降」
    などなどの言い方も考えられます。また、業務設定では「3営業日以降」などの設定になるので、それを自動で置き換える仕組みが必要です。」"""
    d = (earliest - today).days
    if d <= 0:
        return "本日以降"
    if d == 1:
        return "明日以降"
    if d == 2:
        return "明後日以降"
    weeks = (earliest - today.fromordinal(today.toordinal() - today.weekday())).days // 7   # 電話の日の週の月曜日から何週目か
    if weeks == 0:
        return f"今週の{WEEKDAY_WORDS[earliest.weekday()]}曜日以降"
    if weeks == 1:
        return "来週以降" if earliest.weekday() == 0 else f"来週の{WEEKDAY_WORDS[earliest.weekday()]}曜日以降"
    return f"{earliest.day}日以降" if (earliest.year, earliest.month) == (today.year, today.month) else f"{earliest.month}月{earliest.day}日以降"


def _day_variants() -> tuple:
    """日によって変わる言い方のうち、日付を言わないもの（本日・明日・明後日・今週の〇曜日・来週・来週の〇曜日）の一覧（day_words の1か所から作る＝
    どの曜日の電話でも、2週間先までに出る言い方）。録音で流す電話は、この言い方ごとに文を丸ごと録音する（✅ 2026-11-03 ユーザー：「(ア) お勧め：言い方ごとに
    文を丸ごと録音します。OK」）。⏳ 日付の言い方（〇日以降・〇月〇日以降）は、名前と同じつなぎ方で組む（まだ作っていない）"""
    import datetime as dt
    out = []
    for t in (dt.date(2026, 11, 2) + dt.timedelta(days=i) for i in range(7)):
        for d in range(14):
            w = day_words(t + dt.timedelta(days=d), t)
            if not re.match(r"\d", w) and w not in out:
                out.append(w)
    return tuple(out)


DAY_VARIANTS = _day_variants()


def day_recording_id(qid: str, words: str) -> str:
    """日によって変わる言い方の入った質問の、その言い方の録音の名前（つなぎの言葉の前に _d番号。make_ack_clips.script_table と同じ1か所）。"""
    return f"{qid}_d{DAY_VARIANTS.index(words) + 1}"


def merge_ampm(pending: str | None, answer: str | None) -> str | None:
    """午前・午後を聞いたあとの答え（「午後です」⇒「午後」）を、前の値（「15日の10時」）に入れる。答えに時刻があれば、答えのほうを使う。"""
    if not pending or not answer or _HOUR.search(answer) or not (w := AMPM_WORDS.search(answer)):
        return answer
    m = _HOUR.search(pending)
    return pending[:m.start()] + w.group() + pending[m.start():] if m else answer


def asks_wish(q: "Question") -> bool:
    """お客様のご希望・申し出から選んでいただく業務の質問か（唯一の決まり）。この質問だけ：
    - 迷っているお客様に選ぶ助けを伝える（判定の選べるもの「迷っている」＝meaning_judge.answer_kinds・流れの制御 Flow._hesitation・測りの数え
      g53_meaning.flow_act／flow_wants）
    - 決められない（まだ決めていない）のがお客様の最終の決定なら、その質問を保留で締める（Flow._confirm_again・_tell_due・録音を作る道具 make_ack_clips）
    入れない質問：事実を伺う質問（選ぶものではない）・名前の読みの確かめ・システムの質問（ほかのご希望 W1・ご承認の手順 N1〜N3・待つかかけ直すか H2・
    連絡先など＝それぞれの決まった道）・ご承認の質問（ご承認の手順の道）・ほかに分からない点が無いかを伺う質問（取込の unclear_none）。
    ✅ 2026-11-06 日本時間11時19分55秒 ユーザー：「(2) 「ちょっと今は決められないですね。」というのが最終のお客様の決定であれば、「承知しました。それでは
    パンフレットは保留とさせていただきますね。」とパンフレットは締めて、次へ」・2026-11-07 13時05分06秒・18時03分49秒・22時02分54秒（迷っているお客様＝
    業務の選択肢の選び方へのお言葉。お言葉に無い場面には当てない＝帳面 0-2。ご承認の質問への迷いは伺う＝帳面 1 の (11) の A-7）。
    ⛔ 2026-11-08 日本時間0時台（11-07 23時26分13秒のご質問の見直しの H-8）：選ぶ助けをする質問（システムの質問だけ除く）・保留で締める質問・最初の
    「まだ決めていない」に選ぶ助けをする質問を3か所で別々に決めていて、事実を伺う質問・ご承認の質問・ほかに分からない点の質問にも「どういった点で
    お迷いでしょうか？」と伺い、ほかに分からない点の質問を保留で締めえた
    システムの質問のうち、こちらの申し出（OFFER_ROLES＝詳しい者より改めて N4・締める前の申し出 N6）は入れる（業務の選ぶ質問と同じ理由：お客様は聞き取れる
    言葉で迷いを言っている＝伺い直しは誤り。決められないのが最終の決定なら、断られてはいないので保留でよいかを伺う）。⛔✅ 2026-11-09 日本時間6時33分57秒
    ユーザー：「…「うーん、どうですかね。」と返されたら、1回だけ伺い直します：こちら「はい、恐れ入ります。詳しい者からのお話のご希望をもう一度お聞かせ
    いただいてもよろしいでしょうか？」。：この質問はおかしいでしょ！…ちゃんと流れを考えたらわかるだろうが！」。申し出の方法を選んでいただく質問（WAY_ROLES）は
    迷いへの応じ方だけ同じ（hears_hesitation）で、保留は伺わない（決められなければ、それぞれの決まった道＝方法の件を申し伝える・ほかのご希望は無い）。入れない
    システムの質問は、それぞれの決まった道（急いでいる方への「また改めて」H1＝急いでいる方を引き止めない・はっきりしなければ丁寧に締める（11-01）／ご遠慮の
    確かめ D1＝お断りの向きの方に勧めない／待つか改めてか H2・かけ直し C7・約束の電話 P1・伝言 M1・お戻り C6＝この電話の段取りで、こちらの申し出から選ぶ
    ものではない／本心を伺う D2・ご承認の手順 N1〜N3＝それぞれの手順の道）"""
    if q.callback:
        return q.callback in OFFER_ROLES
    return not (q.asks_fact or q.reading_of or q.approval or q.unclear_none)


def hears_hesitation(q: "Question") -> bool:
    """迷っているお客様に応じる質問か（唯一の決まり＝判定の選べるもの「迷っている」meaning_judge.answer_kinds・流れの制御 Flow._hesitation・測りの数え
    g53_meaning.flow_act／flow_wants）：ご希望・申し出から選んでいただく業務の質問（asks_wish＝選ぶ助け・それでも決められなければ保留）と、説明した内容の
    ご承認の質問（approval＝ご承認いただけなかったときの手順と同じ道で、迷っている点を伺ってご説明する・保留では締めない＝Flow._point_step）。
    ✅ 2026-11-08 日本時間2時21分15秒 ユーザー「(1)〜(4)ともイでOK」の (4)（伺ったときの例：「この内容でご同意いただけますでしょうか？」への「うーん、同意する
    かどうか迷いますね。」）。⛔ 直す前：ご承認の質問には「迷っている」を出さず、「承知しました。続きまして…」と、ご承認を伺わないまま次の質問へ進んだ。
    申し出の方法をお客様に選んでいただくシステムの質問（WAY_ROLES＝ほかのご希望 W1・申し出の方法 N5・LINE の伺い方 C11）も、迷いへの応じ方は同じ（伺い直さずに
    選ぶ助け。✅ 2026-11-09 日本時間6時33分57秒のご指摘と同じ理由。⛔ 直す前は判定に「迷っている」を出さず、「うーん、どうしようかな。」に「はい、恐れ入ります。
    （聞くこと）をもう一度お聞かせいただいてもよろしいでしょうか？」と伺い直した）。選ぶ助けのあとも決められなければ、保留は伺わずにそれぞれの決まった道
    （asks_wish の説明）"""
    return asks_wish(q) or q.approval or q.callback in WAY_ROLES


def takes_for_now(q: "Question", condition: str | None) -> bool:
    """ほかに分からない点が無いかを伺う質問（取込の unclear_none）への「無い」か（唯一の決まり＝流れの制御 Flow._ask_confirm・_keep_toward_goal・_tell_due と
    測りの数え g53_meaning.flow_act・flow_wants）。言い切っていなければ（「今のところ質問はありません。」）確かめ直さずに受け取り、「何かわからないことが
    ございましたらいつでもご遠慮なくおっしゃってくださいね。」を添えてから次の質問へ。✅ 2026-11-07 日本時間13時05分06秒 ユーザー：「「が」があろうが
    なかろうが、 この言葉を添える方がいいと思います。その後に続きまして と入るのがスムーズでしょう。」"""
    return bool(q.unclear_none) and condition == q.unclear_none


# 値が確定でないことを表す言葉（approx_value の1か所）：値の前に付く（「だいたい15日」「約10名」）・値のすぐあとに付く（「15日あたり」「2時ごろ」「10名くらい」
# 「15日前後」「10名程度」「10名ほど」「15日をめどに」）。「以降」「まで」「過ぎ」「から」は、お客様が範囲を決めて言ったもの（確定でないのではない）なので入れない
APPROX_BEFORE = r"(?:だいたい|大体|おおよそ|およそ|約)"
APPROX_AFTER = r"を?(?:あたり|辺り|ごろ|頃|くらい|ぐらい|位|前後|程度|ほど|程|めど|目処|目途|目安)に?"
_APPROX_IN_VALUE = re.compile(rf"{APPROX_BEFORE}(?=[0-9０-９一二三四五六七八九十百])[\s、]*|{APPROX_AFTER}")
_NUMBER_WORD = r"[0-9０-９一二三四五六七八九十百千]+(?:日|時|分|名|人|件|円|万円|千円|万|千|個|台|本|回|週|週間|か月|ヶ月|月|年)?"


CUSTOMER_WORDS_MAX = 40   # 依頼者へ渡すお客様の言葉の、1つの発話の長さの上限（customer_said の1か所）


def customer_said(text: str | None) -> str:
    """依頼者へ渡すお客様の言葉（1つの発話）の形（唯一の場所＝言い切っていない印 Item.hedged・答えが動いた発話 Item.said＝Flow.customer_words）：
    前後の空白を外し、CUSTOMER_WORDS_MAX 字まで（簡潔に＝✅ 2026-11-08 日本時間3時48分03秒「お客様の言葉で簡潔に渡せるように！」）。"""
    return (text or "").strip()[:CUSTOMER_WORDS_MAX]


def approx_value(value: str | None, said: str | None) -> tuple[str, str] | None:
    """値を伺う質問の答えが確定でない言い方か（唯一の決まり＝言い直しを作る Flow._ask_confirm の頭・受ける1か所 Flow._tentative）。確定でなければ
    （値の芯, お客様の言い方の値）＝（「15日」, 「15日あたり」）、確定なら None。見るのは、値そのもの（会話AIが言葉のまま報告した＝道具の宣言「相手の言葉のとおり」）
    か、お客様の言葉の値のすぐ前・すぐあと（会話AIが「15日」とだけ報告した）。値がお客様の言葉に無い（会話AIが「午後2時」と書き直した）ときは、数のすぐあと
    （「2時ごろ」）を見る。ほかの所の言葉（「なるほど」の「ほど」など）は見ない。
    ✅ 2026-11-08 日本時間3時48分03秒 ユーザー：「お客様「15日あたりですかね。」のように「あたり」などの日にち確定でない言葉が入っている場合には、こちら
    「はい、それでは15日とさせていただきますね？」のように確定で確認するのはあいての意思に反します。」"""
    v = (value or "").strip()
    if not v:
        return None
    if _APPROX_IN_VALUE.search(v):
        return (_APPROX_IN_VALUE.sub("", v).strip(" 、。") or v), v
    s = said or ""
    i = s.find(v)
    if i < 0:
        m = re.search(rf"{_NUMBER_WORD}({APPROX_AFTER})", s)
        return (v, v + m.group(1)) if m else None
    before = re.search(rf"{APPROX_BEFORE}[\s、]*$", s[:i])
    after = re.compile(APPROX_AFTER).match(s, i + len(v))
    if before is None and after is None:
        return None
    return v, (before.group(0).rstrip(" 、") if before else "") + v + (after.group(0) if after else "")


def pending_recording_id(q: "Question") -> str:
    """保留でよいかを伺う一言（pending_line）の録音の名前（唯一の場所＝流れの制御と録音を作る道具 make_ack_clips.script_table）：業務の質問と、業務設定から
    文が決まる締める前の申し出（N6）は「質問の番号_pending」（依頼主の単位）。文が業務によらないシステムの質問（詳しい者からのお話 N4＝phrases.CALLBACK_PENDING）は
    共通の一言 callback_（役目）_pending。"""
    from phrases import CALLBACK_PENDING
    return f"callback_{q.callback}_pending" if q.callback in CALLBACK_PENDING else f"{q.id}_pending"


def pending_line(q: "Question") -> str:
    """決められないのがお客様の最終の決定のとき、その質問を締める一言（唯一の場所。Flow._tell_due）：「それでは（聞くこと）は保留とさせていただきますね。」。
    （聞くこと）は聞き返しと同じ取込の言葉（Question.topic）。無ければ「この件」（phrases.PENDING_THING）。文が業務によらないシステムの質問は phrases.CALLBACK_PENDING
    （詳しい者からのお話は「ご希望」を付けない＝2026-11-09 日本時間6時33分57秒の帳面の伺いの形）。"""
    from phrases import CALLBACK_PENDING, PENDING_LINE, PENDING_THING
    return PENDING_LINE.format(t=CALLBACK_PENDING.get(q.callback) or q.topic or PENDING_THING)


def tell_recording_id(q: "Question", cond: str) -> str:
    """その答えのあとに伝える一言（Question.tell_words）の録音の名前（唯一の場所＝流れの制御と録音を作る道具 make_ack_clips.script_table）：
    「質問の番号_tell選択肢の番号（1から）」。"""
    return f"{q.id}_tell{q.conditions.index(cond) + 1}"


def other_wish_recording_id(q: "Question", again: bool = False, which: bool = False) -> str:
    """選ぶ質問 q に「どれも要らない」と言われたとき、ほかのご希望を伺う一言（phrases.OTHER_WISH_ASK。q の選択肢が入る）の、依頼主の単位の録音の名前
    （唯一の場所。録音を作る make_ack_clips.script_table と、流す question_play が同じ名前を使う）。again＝伺っている途中で切れた電話の続きで、切れた所から
    伺う一言（phrases.OTHER_WISH_AGAIN）。which＝ご案内の方法の申し出に、どの方法かは言わずに受けていただいたときの1回の伺い直し（phrases.OTHER_WAYS_WHICH）。"""
    return f"{q.id}_other_which" if which else f"{q.id}_other_again" if again else f"{q.id}_other"


# 自分で答える質問の受け皿に使える、知っていることの文の見出し（唯一の表：作る所 instruction.Business.knowledge と、読む所 Flow.known_lines の両方が使う。
# ⛔ 2026-11-06 日本時間5時56分55秒のご質問のあとの読み直しで見つけた：見出しの言葉を2か所に直接書いていた＝片方だけ変えると、受け皿に使える文が黙って減る）
KNOWN_HEADS = {"info": "業務の情報", "answer": "想定問答の答え", "approval": "ご承認いただく内容の説明",
               # 選択肢ごとの良い点・気をつける点（Question.option_notes。業務の情報と同じく、そのまま声に出せる文）：迷っているお客様に選ぶ助けを伝える材料
               # （Flow._hesitation）。✅ 2026-11-07 日本時間13時05分06秒・18時03分49秒 ユーザー
               "option": "選択肢ごとの良い点・気をつける点"}


def approval_explanation(q: "Question | None") -> str | None:
    """ご承認いただく内容の説明の文（唯一の場所）：ご承認を伺う質問に入る一言（Question.intro＝トークスクリプトの説明）。無ければ None（説明は質問の文
    そのもの＝最初の質問は入る一言を持てない＝CL-2 の 4-185 の (4)）。使う所：知っていることの1行（instruction.Business.knowledge）・受け皿に選べる文
    （Flow.known_lines）・続きの電話の振り返りの一言（phrases.recap_line＝callback_question の recap）。"""
    return (q.intro.strip() or None) if q is not None and q.approval else None


def recap_recording_id(q: "Question") -> str:
    """ご承認の質問 q の振り返りの一言（phrases.recap_line。q の説明の文が入る）の、依頼主の単位の録音の名前（唯一の場所。作る make_ack_clips.script_table と
    流す question_play が同じ名前を使う）。"""
    return f"{q.id}_recap"


def question_play(questions: list["Question"], qid: str, before: str = "", again: bool = False, resumed: bool = False) -> dict:
    """その質問を録音で流すときの1つ（電話接続側が流すもの）。業務の質問＝依頼主の単位の録音（つなぎの言葉＋質問の文）、かけ直しで足す質問＝
    共通の一言の録音（phrases.RECORDED の callback_〜。どの業務でも同じ文）。読みの確かめは録音にしない（相手ごとの名前を含む＝AIが言う）。
    before・again・resumed は、つなぎの言葉を決める1か所（spoken_question）へ渡す。入る一言の頭のつなぎの言葉を替えた形は、入る一言の録音（番号だけ）とは別の録音
    （名前は「番号_intro」＋つなぎの言葉＝phrases.question_clip）。"""
    q = next(x for x in questions if x.id == qid)
    if q.callback in OPENING_ROLES:
        return {"kind": "opening", "part": q.callback, "text": q.text}  # 伝言の文・約束の電話の一言は依頼主の単位の録音
    if q.of or q.callback:
        # 業務の質問から文を作ったシステムの質問（ほかのご希望を伺う質問・振り返りの一言）＝業務の質問と同じ依頼主の単位の録音・ほかのシステムの質問＝共通の
        # 一言の録音。直前に流す一言 before（2回目・3回目からの戻る一言）に続ける形は phrases.back_fit の1か所。戻る一言のあと（again で before がある＝
        # Flow._continue）は、初めて伺ったときの前置きを言い直さない形（Question.plain＝B-5）。もう一度言ってほしいと頼まれた（before が無い）ときは、
        # お客様は聞き取れていないので伺った文のまま
        rid, text = q.plain if again and before and q.plain else (q.recording or f"callback_{q.callback}", q.text)
        rid, text = back_fit(rid, text, before)
        # 業務設定から文を作ったシステムの質問（改めてのお話の方法を選んでいただく一言＝BUSINESS_ROLES）も依頼主の単位の録音
        return {"kind": "question", "id": rid, "text": text, "lead": ""} if q.of or q.callback in BUSINESS_ROLES else {"kind": "recorded", "id": rid, "text": text}
    text, lead = spoken_question(questions, qid, before, again, resumed)
    # 日によって変わる言い方の入った質問は、その言い方の録音（Question.recording＝day_recording_id。名前の組み方は今と同じ「番号＋つなぎの言葉」）
    rid = q.recording or q.id
    if again and not lead:
        # 戻る一言（頭が「それでは」）のあとに伺い直す、頭の無い形：初めて伺う形と同じ文なら、その録音（はじめの質問）。違う文（つなぎの言葉・説明の文つき）なら
        # 別の録音（番号_again）。⛔ 2026-11-07 H7 を入れて見つけた：番号だけの名前にすると、説明の文つきのご承認の質問の録音（番号だけ）を上書きした
        base = question_play(questions, qid)
        return base if base["text"] == text else {"kind": "question", "id": f"{rid}_again", "text": text, "lead": ""}
    if q.intro and lead and not again and lead != q.intro and closes(before or "。"):
        # 入る一言（業務設定）の頭のつなぎの言葉を替えた形（relead）：入る一言の録音とは別の録音
        return {"kind": "question", "id": f"{rid}_intro", "text": text, "lead": head_word(lead)}
    return {"kind": "question", "id": rid, "text": text, "lead": lead}


# 次の項目の質問に入るときの頭（✅ 2026-11-06 日本時間12時54分30秒 ユーザー：「次の項目の質問に入る前は、すべて「続きまして」でいいんじゃないですか。」。
# ⛔ それまでは質問の順番で「それでは」「続きまして」「では」を回していた＝2026-10-13 の「短い受けの言葉を順番に回して置く」（G-27 の W1）から）
NEXT_LEAD = "続きまして"
# もう伺った質問を伺い直すときの頭（戻る一言・「失礼いたしました。」のあと・尋ねられて答えたあとに最終の確かめの向きが決まらない質問＝Flow._continue）。
# ✅ 2026-11-06 日本時間11時55分13秒 ユーザー「(2) イ」＝11時台のご報告の (イ)「進む向きが1つに決まらない選ぶ質問は (ウ) の形」＝「はい、それでは
# パンフレットをお送りしてもよろしいでしょうか？」。同じ理由（次の項目ではない＝「続きまして」は合わない）で、もう伺った質問へ戻るとき全部に当てる
AGAIN_LEAD = "それでは"


def restated_again(say: str, back: str = "") -> str:
    """もう伺った言い直し（言い直しの返事を待っている所）へ戻るときに、もう一度言う文（唯一の決まり＝戻る1か所 Flow._continue＝話を戻す・もう一度言ってほしいと
    頼まれた・待たずにお話を続けた）：伺ったときの頭の受けの言葉（前の答えを受けた「ありがとうございます。」など＝phrases.drop_receipt）は言い直さず、もう伺った
    質問へ戻るときと同じ頭「それでは」（AGAIN_LEAD）から。直前に流す戻る一言 back があれば、質問へ戻るときと同じ決まり（phrases.back_fit＝2回目の「それでは
    もう一度確認させていただきますね。」のあとは「それでは」を重ねない・3回目の「…お伺いさせていただきますが」とは1つの文＝返す文に back も入る）。
    ⛔ 2026-11-08 日本時間7時台（11-07 23時26分13秒のご質問の見直しの B-1）：伺ったときの文をそのまま言い直していた（「確認の途中でしたのでもう一度お伺い
    させていただきますがありがとうございます。明日の19時…」・「それではもう一度確認させていただきますね。それではパンフレットを…」）"""
    text = drop_receipt(say)
    return back_fit("", text if head_word(text) else AGAIN_LEAD + text, back)[1]
# その回に直前に流す一言の文の頭のつなぎの言葉と、次の質問の頭のつなぎの言葉が重なったときに替える言葉の順（✅ 同じ時「(1) ウ」＝11時台のご報告の
# 「(ウ) 次の質問の頭を「続きまして」にする」。「続きまして」と重なるときは、残りのつなぎの言葉から）
RELEAD_ORDER = ("続きまして", *(w for w in LEAD_IN["次の質問"] if w != "続きまして"))
# 同じ受けの言葉が続くときに替える相づち（唯一の表＝Flow._ack）：「承知しました。」が続けば「かしこまりました。」（✅ 2026-11-02 日本時間7時12分 ユーザー）・
# 「かしこまりました。」が続けば「承知しました。」（同じ理由。23時22分41秒のご質問の見直し H9）
SAME_RECEIPT = {"shouchi": "kashikomari", "kashikomari": "shouchi"}
# 受けの言葉の相づち（答えを受け取ったことを言う一言＝次の質問・申し伝える一言の頭。唯一の表＝Flow._ack の same_turn）
RECEIPTS = ("shouchi", "arigatou", "kashikomari")

# 文の頭のつなぎの言葉を読む head_word・heads_said は phrases の1か所（決まった文の質問を戻る一言に続ける phrases.back_fit も同じ2つで読む）


def relead(lead: str, before: str) -> str:
    """質問の頭（つなぎの言葉・前の答えで伺う質問の「それでは」・業務の入る一言）のつなぎの言葉が、その回に直前に流す一言 before の文の頭のつなぎの言葉と
    重なれば（同じ言葉・片方がもう片方の終わり＝「それでは」と「では」）、重ならない言葉（RELEAD_ORDER の順）に替える（唯一の場所＝spoken_question）。
    ✅ 2026-11-06 日本時間11時55分13秒 ユーザー「(1) ウ」（「承知しました。それではこの件は保留とさせていただきますね。それではお支払いは…」の重なりに、
    「次の質問の頭を「続きまして」にする」）。同じ理由が当たる場面すべて＝業務の「答えのあとに伝える一言」（頭のつなぎの言葉を業務設定の確かめで止めて
    いない）・業務の入る一言（頭の言葉を変えたい業務が書く＝CL-2 の business_settings）・続きの電話の始めの一言も、この1か所。
    重なった頭のあとに、まだ頭の言葉（質問の頭を組む部分 phrases.LEAD_PIECES＝「ほかに」など）が続くなら、替えずに外す（「それではお手元の資料をご覧ください。
    ほかにご不明な点は…」。⛔ 2026-11-08 日本時間8時台（11-07 23時26分13秒のご質問の見直しの B-8）まで「続きましてほかに…」＝11-07 4時40分35秒「ここで
    「続きまして」はおかしいでしょ。「ほかに」とかでしょ。」に反していた）。"""
    w = head_word(lead)
    said = heads_said(before) if w else set()
    def echoes(a: str) -> bool:
        return any(a.endswith(x) or x.endswith(a) for x in said)
    if not said or not echoes(w):
        return lead
    if lead[len(w):].startswith(tuple(LEAD_PIECES)):
        return lead[len(w):]
    alt = next((a for a in RELEAD_ORDER if not echoes(a)), None)
    return lead if alt is None else alt + lead[len(w):]


def final_toward(q: "Question") -> str | None:
    """まだ答えていない質問について尋ねられて答えたあと、お客様が相づちだけのとき（Flow._continue の final）、最終の確かめで伺う向き（唯一の決まり＝流れの
    制御と、録音を作る形 question_forms）：こちらが勧めた向き＝宣言した選択肢のうち目的から遠ざからないもの（進む向きの1か所 Question.toward）が1つだけのとき、
    その選択肢（11-03 の「最終目的に繋がるクロージングはマイナスの言葉で締めない」）。事実を伺う質問（お客様が言っていない事実を確かめる形になる）・
    選択肢の型でない質問・システムの質問（申し出 OFFER_ROLES は受けていただく答え）・進む向きが2つ以上ある質問は None（頭に「それでは」を付けて伺い直す＝AGAIN_LEAD）。
    ✅ 2026-11-06 日本時間11時55分13秒 ユーザー「(2) イ」（11時19分55秒「話はパンフレットをから移っていないので戻す必要はなく、あいてが迷っている最中の
    最終確認をして、パンフレットのシーンを締めないといけないんじゃないですか？」を、まだ答えていない質問にも当てる）"""
    if q.callback in OFFER_ROLES:
        # 申し出（詳しい者より改めて N4・締める前の申し出 N6）：こちらが勧める向きは申し出を受けていただく答え（こちらがすることの言葉 Question.do_words の
        # ある答え）＝最終の確かめは「はい、それでは詳しい者よりお話しさせていただきましょうか？」（Flow._final_check の final_do）。⛔ 2026-11-09 日本時間8時台：
        # システムの質問をすべて None にしていたので、迷っている方に選ぶ助けを伝えたあとの「なるほど。」に、最初の申し出をそのままくり返していた
        # （「はい、それでは詳しい者より改めてお話しさせていただいてもよろしいでしょうか？」＝本書 9-4「お客様が考えている」の「相づちだけなら最終の確かめ」・
        # 11-06 11時19分55秒・11-01「全く同じ質問を2度したら、機械だと思われてしまいませんか？」に反していた）
        return next((c for c in q.conditions if q.do_words.get(c)), None)
    if q.asks_fact or q.type not in CHOICE_TYPES or q.reading_of or q.callback or q.of:
        return None
    return q.toward[0] if len(q.toward) == 1 else None


def before_lines(questions: list["Question"]) -> list[str]:
    """業務の質問の直前に、同じ返事で流しうる業務の一言の文（唯一の一覧＝録音を作る形を決める question_forms）：業務の「答えのあとに伝える一言」（Question.tell_words）・方法を並べて伺ったあとの「どれでも」にこちらで方法をお伝えする一言（way_any_line。お電話でのご案内なら
    そのあと次の質問へ）。流れの制御は、質問の前に同じ返事で流す一言の文そのものを質問の頭を決める1か所（spoken_question の before）に渡す＝録音の形の一覧も
    文そのものを渡す。ほかに質問の前に流す決まった一言（戻る一言・ご不満な点…・申し伝える一言・お詫び）の頭にはつなぎの言葉が無い（試験
    test_lines_before_a_question_do_not_start_with_a_lead_word）。⛔ 2026-11-08 日本時間7時台（11-07 23時26分13秒のご質問の見直しの B-3）：前は一言の頭の
    つなぎの言葉だけ（「それでは」）を返し、録音の形の一覧がそれを前の言葉として渡していた＝spoken_question は「。」で終わらない前の言葉を3回目に戻る一言と
    つなぐ頭と読むので、入る一言のある質問の替えた形（（番号）_intro）が一覧に無く録音が無いまま流れ、頭が「では」の一言があれば流れない形を作っていた"""
    # 保留の一言（pending_line）は、伺ってお返事を待つ一言＝同じ返事で次の質問を流さない（Flow._ask_pending。✅ 2026-11-09 日本時間3時46分53秒）ので入れない
    lines = [t for q in questions for t in q.tell_words.values()]
    lines += [w[1] for q in questions if may_wish(q) and (w := way_any_line(q))]
    return list(dict.fromkeys(lines))


def question_forms(questions: list["Question"], qid: str) -> list[dict]:
    """業務の質問を録音で流しうる形（唯一の一覧＝録音を作る make_ack_clips.script_table。流す形は question_play の1か所が作る）：次の項目として初めて伺う形・
    直前に流しうる一言（before_lines）の頭と重なってつなぎの言葉を替えた形・もう伺った質問を伺い直す形（頭は AGAIN_LEAD＝Flow._continue。戻る一言のあと・
    「失礼いたしました。」のあと・尋ねられて答えたあと）・続きの電話で最初に伺う形（頭は QUESTION_RESUME_LEAD）。同じ録音の名前は1つ。"""
    q = next(x for x in questions if x.id == qid)
    forms = [question_play(questions, qid)] + [question_play(questions, qid, before=line) for line in before_lines(questions)]
    if not (q.reading_of or q.callback or q.of):
        forms += [question_play(questions, qid, again=True), question_play(questions, qid, resumed=True)]
        # 戻る一言のあとに伺い直す形（頭が戻る一言の頭と重なれば外す＝spoken_question の again）。✅ 2026-11-07 日本時間3時14分01秒 ユーザー「(4) H7」
        forms += [question_play(questions, qid, again=True, before=text) for _, text in BACK_LINES]
    return list({(p["id"], p.get("lead") or ""): p for p in forms}.values())


def resumed_call(earlier: dict | None, apology_call=None, number_again=None) -> bool:
    """続きの電話か（唯一の決まり＝流れの制御 Flow._resumed_call・名乗りの録音と AIへの指示文 instruction.Business.first_ask）：前の通話で取れた答えがある。
    お詫びの電話・番号を伺い直す電話は業務の質問を伺わないので除く。名乗り・お詫び・用件のすぐあとに伺う業務の質問の頭（spoken_question の resumed）を決める。"""
    return bool(earlier) and not apology_call and not number_again


def purpose_part(earlier: dict | None, apology, when: str, who: str | None = None) -> str:
    """名乗り（お詫び）のあとに流す用件の一文の、名乗りの部分の名前（唯一の決まり＝流れの制御 Flow._purpose_play・名乗りの録音の文
    instruction.Business.opening_parts）。前の電話でお話しした方への続きの電話は、用件の一文を前の電話とつなげる（phrases.PURPOSE_AGAIN）：
    - 前の電話で業務の質問を伺い始めていた（前の通話で取れた答えがあり、その答えを取った通話で繋いだ方＝who がこの方。who の無い前の形・目的のお客様のいない
      業務は、目的の方と読む）：「前回のお電話で途中になっておりました（用件）でお電話いたしました。」（cut）
    - 伺う前に終わっていた（取れた答えは無いが、前の電話のお詫びがある＝その電話でお話しした方）：「前回お伺いすることができませんでした（用件）…」（unasked）
    - 同じ日の電話は「先ほど」（お詫びと同じ when＝apology_when）。それ以外（初めての電話・前の電話でお話ししていない方）は今の用件の一文（purpose）
    ✅ 2026-11-09 日本時間9時48分09秒 ユーザー（お言葉は phrases.PURPOSE_AGAIN）"""
    if earlier:
        whos = {(e or {}).get("who") or "target" for e in earlier.values() if isinstance(e, dict)}
        return f"purpose_again_cut_{when}" if whos == {who or "target"} else "purpose"
    return f"purpose_again_unasked_{when}" if apology else "purpose"


def spoken_question(questions: list["Question"], qid: str, before: str = "", again: bool = False, resumed: bool = False) -> tuple[str, str]:
    """録音で流す質問の文（つなぎの言葉＋質問の文）と、そのつなぎの言葉（唯一の場所）。次の項目の質問に入るときは「続きまして」（NEXT_LEAD。
    ✅ 2026-11-06 日本時間12時54分30秒 ユーザー。⛔ それまでは質問の順番で「それでは」「続きまして」「では」を回していた）。最初の質問は名乗りと一緒に
    流すので、つなぎの言葉は付けない。
    ✅ 2026-10-15 ユーザー（聞き比べ）：つなぎの言葉と質問を1本で録音したほうが「しいて言えば自然」⇒ 1本の録音にする。順番で決めておけば、
    依頼主ごとの録音は質問1つにつき1本のまま（流れの制御・録音を作る道具の両方が、ここを使う）
    before＝その回に、この質問の直前に流す一言の文そのもの：その文の頭のつなぎの言葉と重なれば替える（relead。✅ 2026-11-06 日本時間11時55分13秒「(1) ウ」）。
    「。」で終わらない一言（3回目に戻る一言）なら、質問をその文に続ける。頭の言葉だけを渡さない（文として読む＝B-3。録音の形の一覧も before_lines の文を渡す）。
    again＝尋ねられて答えたあとに伺い直す形（頭は AGAIN_LEAD。入る一言はもう流したので付けない。✅ 同じ時「(2) イ」）。
    resumed＝続きの電話で、お詫び（＋用件の一文）のすぐあとに最初に伺う（次の項目のつなぎの言葉の代わりに QUESTION_RESUME_LEAD。✅ 2026-11-06 日本時間
    15時20分30秒 ユーザー「続きまして、も、それではも繋がりがおかしい」。前の答えで伺う質問も同じ。入る一言は業務の言葉のまま）。"""
    q = next(x for x in questions if x.id == qid)
    if q.reading_of or q.callback:  # プログラムが足す質問（読みの確かめ・かけ直し）：つなぎの言葉は付けない
        return q.text, ""
    # ⛔ 見直しで見つけた漏れ：読みの確かめを挟むと、そのあとの質問のつなぎの言葉（＝録音の名前）が1つずれた（「それでは」⇒「続きまして」）
    i = [x.id for x in questions if not x.reading_of and not x.callback].index(qid)
    others = others_head(q)
    if before and not closes(before):
        # 3回目から戻るときの一言（phrases.BACK_TO_MIDWAY「確認の途中でしたのでもう一度お伺いさせていただきますが」＝文を終えない）は、質問の頭として1本で
        # 録音する（✅ 2026-11-07 日本時間4時40分35秒 ユーザー「…「確認の途中でしたのでもう一度お伺いさせていただきますが、ご予約は～」」）
        # 頭と文をつなぐのは phrases.head_join の1か所（「が」で終わる頭のあとは「恐れ入りますが」を外す＝システムの質問の back_fit と同じ。⛔ K-3 の確かめで見つけた）
        return head_join(before + others, q.text), before + others
    if again:
        # 直前に流す一言（2回目に戻るときの「それではもう一度確認させていただきますね。」＝phrases.BACK_LINES）の頭が同じ「それでは」なら重ねない（頭を付けない）
        lead = ("" if AGAIN_LEAD in heads_said(before) else AGAIN_LEAD) + others
        return head_join(lead, q.text), lead
    if i == 0:
        return q.text, ""
    # この質問に入る一言（Question.intro＝業務設定）があれば、つなぎの言葉の代わりにそれを頭に。前の答えで伺う質問（ask_if。
    # 言い切っていないときだけ伺う質問も）は、お客様の答えを受けて伺うので「それでは」（「資料はお手元に届いていますでしょうか？」の「いいえ」⇒
    # 「それではお送りし直してもよろしいでしょうか？」）（✅ 2026-11-03 ユーザーの言い方：「それでは、日時変更なさいますか？それとも、
    # キャンセルなさいますか？」「それでは、いつでしたら、お話しできそうでしょうか？」。⛔ 見直しで見つけた：並びの順番の「続きまして」を付けていた＝
    # 業務設定の文が「それでは」で始まると「続きましてそれでは、…」「それではそれでは、…」。業務設定の文には頭のつなぎの言葉を書かない＝CL-2）。
    # 前の答えで伺う質問は、前の項目の続き（次の項目ではない）なので「続きまして」にしない（12時54分30秒の「次の項目の質問に入る前」）
    # 続きの電話のお詫びのすぐあと（resumed）は、前の答えで伺う質問も QUESTION_RESUME_LEAD：「それでは」はこの電話のお客様の答えを受ける言葉で、お詫びのあとには
    # 受ける答えが無い（15時20分30秒「続きまして、も、それではも繋がりがおかしい」と同じ理由。⛔ 23時22分41秒のご質問の見直し A6・H4：前の答えで伺う質問だけ
    # 「それでは」のまま流していた）。業務の入る一言は業務の言葉のまま
    if resumed and not q.intro:
        return head_join(QUESTION_RESUME_LEAD, q.text), QUESTION_RESUME_LEAD
    # ほかに分からない点が無いかを伺う質問は、次の項目ではなく、ほかの話のあとの「ほかに」（others_head。⛔ それまでは「続きまして」）
    if q.intro:
        lead = relead(q.intro, before)
    elif q.ask_if:
        lead = relead("それでは" + others, before)
    elif q.unclear_none:
        lead = others
    else:
        lead = relead(NEXT_LEAD, before)
    return head_join(lead, q.text), lead


def others_head(q: "Question") -> str:
    """ほかに分からない点が無いかを伺う質問（取込で決める Question.unclear_none）の文の前に付ける「ほかに」（phrases.OTHERS_HEAD。唯一の決まり＝spoken_question）：
    ほかの話のあとに伺う質問なので、次の項目のつなぎの言葉「続きまして」の代わりに「ほかに」、伺い直すときも「それではほかに…」（✅ 2026-11-07 日本時間4時40分35秒
    ユーザー：「（アドレスを確かめたあと）こちら「承知しました。続きましてご不明な点はございますでしょうか？」；ここで「続きまして」はおかしいでしょ。「ほかに」
    とかでしょ。」）。業務の文がもう「ほかに」と書いていれば足さない。はじめの質問と続きの電話の始めの頭（QUESTION_RESUME_LEAD）には付けない（spoken_question）。
    ⚠️ 取込の文字のAI2つがそろわず印が空の質問は、今までどおり「続きまして」（印が空＝分からない点を伺う質問かが決まっていない）"""
    return OTHERS_HEAD if q.unclear_none and not q.text.startswith((OTHERS_HEAD, "他に")) else ""


def other_value(qtype: str, pending: str | None, given: str | None) -> str | None:
    """「合っている」の報告に添えられた値が、待っている値と違う意味の値なら、その値（同じ・意味の無い値なら None）。
    返事の言葉だけ（「はい」「はいそう」）・形の合わない値（日時に「3人」）は、意味の無い値として無視する。数の型は、数字が同じなら同じ値
    （「3人」と「3名」）。これまでの試験の「合っている」588回のうち、違う意味の値は2回だけ（どちらも、AIが訂正を報告しなかった回）。"""
    if not given or _REPLY_ONLY.fullmatch(given) or form_problem(qtype, given):
        return None
    if qtype in AMPM_TYPES:
        # 時刻は、言い直しの言い方の形どうしで比べる（「明日の19時」と「明日の19時、午後7時」・「明日の午後7時」は同じ値）。違う値は、もとの形で返す
        return None if spoken_clock(given) == spoken_clock(pending) else plain_clock(given)
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
             "reason": "理由", "part": "流し直す部分", "party": "相手", "why": "かけ直しの理由", "offer": "相手の申し出", "answer": "答えの文",
             "fallback": "使えないときの一言", "until_why": "〇時までの理由", "cycle": "その日・頃を選んだ理由",
             "unknown_why": "わからない・答えたくない理由", "rest": "ほかに言われたこと", "terms": "申し出の条件",
             "added": "こちらから足した条件", "topic": "申し出に含める中身", "way": "申し出の方法"}
SYSTEM_LABEL = {"unknown": "わからない", "refused": "答えたくない"}
ANY_LABEL = "どちらでも"
NONE_LABEL = "どれも要らない"
# 意味の2つ目の判定（meaning_judge。DESIGN.md 6-10 の (6)）が、どの選択肢とも決められないと答えたときの名前（受け入れにも断りにも取れる・言い切っていない・答えになっていない）
UNDECIDED = "決められない"
# 意味の判定が、答えではないと決める言葉（どの場面でも）：相手が取り次ぐので待つように言った／代わった別の人が話し始めた（DESIGN.md 6-8）
HOLD = "取り次ぎを待つ"
HANDOVER = "代わった人が話し始めた"
# 答えの段だけの、待つ・かけ直すの読み（meaning_judge.ANSWER_KINDS。お呼びする段・尋ねているか・戻ったかの問い合わせには出さない）。どれも答えではない。
# ✅ 2026-11-05 日本時間23時19分06秒 ユーザー（会話2）：「電話がつながったまま、妻に確認を待ってと言っていらっしゃるなら、「はい、それではこのままお返事を
# 待っております。」で待ち、妻に確認するから、また、後からや別日程でかけ直してという意味であれば、「（はい、）承知しました。いつ頃おかけ直しさせて
# いただきましょうか？」のように一旦終えると良いでしょう。」・23時56分48秒「(2) …OK」「待つか、かけ直すかが言葉から決まらないとき：(ア)」（1回伺う）
# ＝取り次ぐ・代わる・理由を言わずに待つ（HOLD＝黙って待つ）と、確かめ・相談して戻る（HOLD_BACK＝「このままお返事を待っております。」）を分け、あとでかけ直す
# （LATER）と、どちらか決まらない（WAIT_OR_LATER＝1回伺う）を足した。読みを組み合わせるのは wait_reading の1か所
HOLD_BACK = "返事を待つ"
LATER = "あとでかけ直す"
# あとでかけ直すのうち、お客様のほうからあとで連絡すると申し出た（折り返しの申し出＝お礼の一言「お気遣いありがとうございます。…」のあと、こちらからかけ直す形で
# 伺う＝✅ 2026-10-22 ユーザー・2026-11-05 日本時間23時19分06秒の会話2）。組み合わせ（wait_reading）では LATER と同じ1つ。お礼を言うかは later_offer の1か所。
# ⛔ 2026-11-06 日本時間13時台の見直し（C9）で見つけた：誰からかけ直すかを判定が読まず、会話AIの申し出だけで決めていた＝報告が無い・遅れるとお礼が抜けた
LATER_THEIRS = "お客様から折り返す"
WAIT_OR_LATER = "待つかかけ直すか決められない"
# まだ答えを考えている途中（「うーん、どうしようかな…。」）：プログラムが「はい。」の相づち（phrases.ACK の aizuchi）を流して待つ（黙られたら電話接続側が5秒で
# 急かさない一言＝phrases.THINKING_PROMPT＝9-4）。✅ 2026-11-06 日本時間11時19分55秒 ユーザー「(1) ア」＝10時台に伺った (ア)「判定に「まだ考えている途中（答えていない）」を
# 足します。そう読んだら、会話AIが報告してもしなくても、プログラムが「はい。」の録音を流して待ちます」。⛔ それまでは会話AIが自分で「はい。」と言う形（10-14 の
# 形 A）だけで、録音で流す通話ではその声を流さず（確かめていない声は流さない＝9-4 の「範囲外の質問」）、促しの知らせで報告させて同じ確かめの一言をくり返した
THINKING = "考えている途中"
WAIT_KINDS = (HOLD, HOLD_BACK, LATER, LATER_THEIRS, WAIT_OR_LATER, THINKING)
# どれにするかを迷っている・決めかねていると言い終えた（「カードか振込か迷っています。」。答えではない＝待つ読みでもない）：選ぶ助けになることを伝えて促す
# （Flow._hesitation の1か所）。言い終えていない言いよどみ（「うーん、どうしようかな…。」）は考えている途中（THINKING）。✅ 2026-11-07 日本時間13時05分06秒
# ユーザー：「…のように促すのが一番いいんじゃないでしょうか。迷っていると言っているのに途中で話を終わらしたりどうなさいましたか と 急かすのは最悪な手ですよ。」
HESITATING = "迷っている"
# 直前にこちらが伺ったこと（質問・言い直しの確かめ）を、もう一度言ってほしいと頼まれた（「え、すみません、もう一度お願いできますか？」・「聞こえなかったです」）：
# プログラムが「失礼いたしました。」（phrases.ACK の shitsurei）のあと、伺ったことを流し直す（質問は頭に「それでは」＝AGAIN_LEAD。Flow._repeat_now の1か所）。
# 答えの段だけの読み（meaning_judge.ANSWER_KINDS）。✅ 2026-11-06 日本時間15時20分30秒 ユーザー「(1)…お勧め：判定の選べるものに「もう一度言ってほしい」を
# 足します。プログラムが「失礼いたしました。」＋「それでは（質問）」を流します。OK」。⛔ それまでは「？」の文なので、こちらに尋ねられた扱いになり、質問を
# 流し直せなかった（13時27分07秒のご質問の見直しで見つけた）
REPEAT = "もう一度言ってほしい"
# 判定の読みのうち、答えではないもの（決められない UNDECIDED のほか。唯一の並び＝会話になっていない返事 off_talk・言い切りの印 Flow._judged_unsure・
# 測りの数えの曖昧の印 g53_meaning.mark）。⛔ 2026-11-06 日本時間15時台に見つけた：同じ並びを3か所で手で書いていて、待つ読みを足したときに測りの側が
# 取り次ぎを待つ・代わった人のままだった
NOT_ANSWERS = (*WAIT_KINDS, REPEAT, HANDOVER, HESITATING)
# 判定の読みのうち、どの答えも選んでいないもの（唯一の組＝undecided・hesitates）：決められない（判定が言葉から決められない＝相づち・選ぶのを避けた・
# お客様の「決められない」も）と、わからない（わからない・まだ決めていない、と言った＝meaning_judge.SYSTEM_DESC）。お客様が「まだ決めていない」と
# 言ったかは、この組と理由の読みで決める1か所（Flow._not_deciding）。⛔ 2026-11-08 日本時間0時台（11-07 23時26分13秒のご質問の見直しの H-2）：
# 最初の答え・「どういった点で…」のあと・迷っているか・最終の確かめのあと、の4か所で組が違った（「届き先を決めかねていて」が読みしだいで3通りに分かれた）
UNDECIDED_READS = (UNDECIDED, "unknown")


def undecided(readings) -> bool:
    """届いた判定の読みがどれも、どの答えも選んでいない（UNDECIDED_READS）か（唯一の決まり＝流れの制御 Flow._hesitation・_confirm_again）。"""
    got = {str(r) for r in readings if r is not None}
    return bool(got) and got <= set(UNDECIDED_READS)


WAIT_NOW, CALL_LATER = "このまま待つ", "かけ直す"   # 決まらないときに1回伺う質問 H2 の選択肢（callback_question("wait_or_later")）
# H2 の3つ目の選択肢：待つのも、かけ直すのも要らず、お客様がそのままお話を続けた（伺う前のお話に答えた「あ、いや、料金のところが気になってたんです。」・
# 「あ、もう大丈夫です、続けてください。」）。伺う前の位置へ戻し、その言葉を戻した位置について読み直す（Flow._wait_continue の1か所）。⛔ 2026-11-08
# 日本時間7時台（11-07 23時26分13秒のご質問の見直しの A-5）：選べるものが2つだけで、判定は「決められない」と読み、「はい、それではこのままお返事を
# 待っております。」と待った（言われた点を捨てた＝本書 9-3「取得済みのことを重ねて聞かない」）
KEEP_ON = "待たずに続ける"
# H2 の答えの名前を、同じ意味の待つ・かけ直すの読みに読み替える表（唯一の表＝as_wait）。H2 を伺っている間、判定は H2 の答えの名前でも待つ読みの名前でも
# 読めるので、2つの名前を同じ意味に数える。⛔ 2026-11-06 日本時間23時22分41秒のご質問の見直し（W2）：判定の1つが「かけ直す」・もう1つが「お客様から
# 折り返す」と読むと、同じ意味なのに割れたとみて「このまま待つ」に決め、「はい、それではこのままお返事を待っております。」と逆の動きをした
WAIT_ANSWERS = {WAIT_NOW: HOLD_BACK, CALL_LATER: LATER}


def as_wait(reading, asking_wait: bool):
    """判定の読み reading を、待つ・かけ直すの読みの名前にそろえる（唯一の場所＝流れの制御 Flow._wait_check）：H2 を伺っている間（asking_wait）だけ、H2 の答えの
    名前を WAIT_ANSWERS で読み替える（ほかの質問の選択肢の名前は、たまたま同じ字でも読み替えない）。"""
    return WAIT_ANSWERS.get(str(reading), reading) if asking_wait and reading is not None else reading
# 確かめの名前（Flow.on_check）：別の耳の文字（聞き取り。6-10 の (5)）・意味の判定（6-10 の (6)。judge2＝後ろで走らせる2つ目の判定）
CHECKS = ("ear", "judge", "judge2", "asks", "asks2", "returned", "returned2", "judge@ahead", "judge2@ahead")  # asks・asks2＝呼んでいる段の、こちらに尋ねているかの問い合わせ
# （Flow.asks_context）・returned・returned2＝取り次ぎで待ったあとに話した方が、待たせた方か（Flow.returned_context）。前の話の答えは、判定の読みに添えて届く
# （Reading.prior。読み口は Flow._prior の1か所）。⛔ 2026-11-08 日本時間12時台まで、判定の組が答えられなかったときにも前の話の答えだけを単独で受け（"prior"）、
# 会話AIの報告と比べていた＝会話AIを判定に加えていた（12時11分40秒のご判断で外した）
# 意味の判定の受け持ち（会社を分けて同時に出す2つ。早く届いたほうで決め、遅れて届いたほうも同じ決まりで当てる。meaning_judge.JudgePanel）
JUDGMENTS = ("judge", "judge2")


def later_offer(check: dict, offer: str | None) -> str | None:
    """あとでかけ直すと読んだ言葉の、お客様の申し出（唯一の決まり＝流れの制御 Flow._wait_route）：お客様のほうから折り返すという申し出か（"callback"）は判定が読む。
    かけ直すと読んだ判定がそろってお客様から折り返す（LATER_THEIRS）と読めば申し出・そろってこちらから（LATER）と読めば申し出ではない（会話AIが申し出と報告
    しても）・割れたら、会話AIの申し出（offer）が判定の1つとそろったときだけ申し出（設計書 1-1＝会話AIの報告は、独立した確かめとそろったときだけ受け取る）。
    折り返しでない申し出（伝言・ほかの手段）は、会話AIの報告のまま（判定はかけ直しの向きだけを読む）。"""
    read = [check[k] for k in JUDGMENTS if check.get(k) in (LATER, LATER_THEIRS)]
    theirs = read.count(LATER_THEIRS)
    if offer not in (None, "callback") or not read:
        return offer
    if theirs == len(read):
        return "callback"
    return "callback" if offer == "callback" and theirs else None


def wait_reading(check: dict) -> str | None:
    """お客様の言葉を、待つ・かけ直すの読みとして組み合わせた答え（唯一の決まり＝流れの制御 Flow._wait_now・測りの数え g53_meaning.flow_act）。
    届いた判定の読みがどれも WAIT_KINDS のときだけ答える（答え・ほかの読みと食い違えば None＝答えの読みの決まり Flow._meaning_of が受け持つ）：
    そろえばその読み／待つ2つ（HOLD・HOLD_BACK）が食い違えば黙って待つ HOLD（どちらも電話をつないだまま待つ。「お返事を待っております」は確かめて戻る
    方だけに言う）／待つ読みとかけ直す読みが食い違う・どれかが決まらないと読めば WAIT_OR_LATER（言葉から決まらない＝1回伺う）。
    ⛔ 2026-11-06 日本時間5時56分55秒のご質問のあとの読み直しで見つけた：待つ・かけ直すの読みと、ほかの読み（決められない・答え・不在）に割れると None を返し、
    待つ道にも1回伺う道にも入らず、会話AIの報告の種類で3通りに分かれた（報告が無いと無言）。お呼びする段は、報告で受けたとき（Flow._hold_now＝どれか1つでも
    「取り次ぎを待つ」なら待つ）と報告の無いターン（ここ＝全部が待つときだけ）で決まりが違った ⇒ 割れても答える：取り次ぎを待つ読みがあれば（尋ねているかの
    問い合わせの読みも数える。かけ直す・決まらない読みが無ければ）「はい、」のあと黙って待つ HOLD（✅ 2026-11-06 日本時間2時28分41秒のお言葉・お呼びする段の
    「取り次ぎの最中にかけ直しの質問を流さない」）／それ以外の割れ（返事を待つ・かけ直す・決まらない読みと、ほかの読み）は言葉から決まらない＝WAIT_OR_LATER
    （1回伺う＝✅ 23時56分48秒「待つか、かけ直すかが言葉から決まらないとき：(ア)」）。待つ・かけ直すの読みが1つも無ければ None（答えの読みの決まりが受け持つ）。
    まだ考えている途中（THINKING）は、ほかの待つ読みがあればそちら（取り次ぎ・相談のために待つ・かけ直す、のほうが言葉の中身がはっきりしている）。ほかの待つ読みが
    無ければ、答え・決められないの読みと割れても考えている途中（「はい。」で待つ＝取り消せない動きをしない側。お客様が続けて話せば、その言葉で決め直す）。
    お客様から折り返す（LATER_THEIRS）は、あとでかけ直す（LATER）と同じ1つ（応じ方は同じかけ直し。お礼の一言を言うかは later_offer）。"""
    got = {LATER if check[k] == LATER_THEIRS else check[k] for k in JUDGMENTS if k in check}
    waits = got & set(WAIT_KINDS)
    if any(check.get(k) == HOLD for k in ASKS_CHECKS):
        waits.add(HOLD)   # 尋ねているかの問い合わせの「取り次ぎを待つ」（お呼びする段。判定の仕組みは、どの問いにも「取り次ぎを待つ」を添える）
    if not waits:
        return None
    if waits == {THINKING}:
        return THINKING
    waits.discard(THINKING)
    got.discard(THINKING)
    if got == waits and len(got) == 1:
        return next(iter(got))
    return HOLD if HOLD in waits and not waits & {LATER, WAIT_OR_LATER} else WAIT_OR_LATER



def asks_us(readings) -> bool:
    """答えの段で、お客様が、いまの質問・言い直しに答える代わりに、こちらに尋ねたか（唯一の決まり＝流れの制御 Flow._asked_now・測りの数え
    g53_meaning.flow_act）：届いた意味の判定の読みがどれも答えを選んでいない「決められない」（説明に「どの答えがよいかを、こちらの考えとして尋ねた」を含む＝
    meaning_judge）で、こちらへ尋ねた問いがある（項目 QUERY＝queries_us の1か所）。
    ✅ 2026-11-06 日本時間4時11分28秒 ユーザー：「送ってもらったほうがいいですかねは、「送ってもらったほうがいいですかね？」の相手への確認の意味と、
       「送ってもらったほうがいいですかね。」の自身への言い聞かせの意味合いがあります。」・4時23分33秒「お客様：送ってもらったほうがいいですかね？や、
       送ってもらったほうがいいですかねー？だった場合は、こちらは「そうですね、お読みになられるとより理解が深まるかと思います。」のような返答が自然です。」
    ✅ 2026-11-08 日本時間8時55分08秒 ユーザー「(イ) 判定に「こちらへ尋ねた問いがあるか」を1項目足し、1か所で読む」＝相手への確認か言い聞かせかは、
       言葉の形（「？」「。」）ではなく判定が読む（⛔ それまでは「？」で終わる文があるときだけ尋ねたとしていた＝聞き取りの文字が「。」で終わった問いを聞き流した）。
       判定が答えに寄っていると読んだ問いの形（頼み）は、答えの代わりではない（「それでは…させていただきますね？」の一言が、頼みへの答えを兼ねる＝11-01 3時45分）"""
    got = [r for r in readings if r is not None]
    return bool(got) and all(str(r) == UNDECIDED for r in got) and queries_us(got)


def queries_us(readings) -> bool:
    """お客様の言葉に、こちらへ尋ねた問いがあるか（唯一の決まり＝答えの代わりに尋ねた asks_us・答えに添えた質問 Flow._question_to_us。読む項目は
    query_choices）：届いた意味の判定の読みのうち、この項目を読んだものがそろって「尋ねた問いがある」。項目を読んでいない読み（届いていない・項目を足す前の
    記録）は数えず、1つも無ければ尋ねたとしない（言葉の形で補わない。判定を使わない通話の見方は Flow._question_to_us の中だけ）。割れたら尋ねたとしない
    （答えの段の読みは組の1つ＝割れるのは、判定の受け持ちが2つある通話だけ）。✅ 2026-11-08 日本時間8時55分08秒 ユーザー（query_choices に引いたお言葉）"""
    got = [q for r in readings if r is not None and (q := getattr(r, QUERY, None)) is not None]
    return bool(got) and all(q == QUERY_YES for q in got)


def hesitates(readings) -> bool:
    """答えの段で、お客様がどれにするかを迷っていると言い終えたか（唯一の決まり＝流れの制御 Flow._hesitation・測りの数え g53_meaning.flow_act）：届いた意味の判定の
    読みがどれも迷っている（HESITATING）・決められない（UNDECIDED）・わからない（まだ決めていない）で、迷っているが1つ以上（迷っていると言い終えた言葉は、どの答えも
    選んでいない＝決められない・まだ決めていないと読まれても同じ向き。割れたら、すぐ保留で締めずに選ぶ助けを先にする＝✅ 2026-11-07 日本時間13時05分06秒
    ユーザー「迷っていると言っているのに途中で話を終わらしたり…急かすのは最悪な手ですよ。」）。考えている途中（言いよどみ）と割れたら、待つ読みの1か所
    （wait_reading）が先に受け持つ（「はい。」で待つ＝お客様が続けて話せば決め直す）。"""
    got = {str(r) for r in readings if r is not None}
    return HESITATING in got and got <= {HESITATING, *UNDECIDED_READS}


def asked_text(said: str | None) -> str | None:
    """こちらへ尋ねた問いの文（会話AIへ渡す「お客様の質問」の文。唯一の場所＝Flow._asked_now・_question_to_us）：問いかけで終わる文（question_in の語尾の
    見方）があればその文、無ければ言葉そのもの。尋ねたかは判定の読みで決まっている（queries_us）＝ここは渡す文を選ぶだけ。"""
    return question_in(said) or (said or "").strip() or None


# 先に言われた答え（Flow.ahead）を、次に伺う質問について読み直す判定（電話接続側が、同じ発話の言葉で出す＝Flow.ahead_context。読みは Flow.on_check が
# その答えの確かめにだけ当てる＝いまの発話の判定・ほかの質問の材料には当てない）
AHEAD_SUFFIX = "@ahead"
AHEAD_CHECKS = tuple(k + AHEAD_SUFFIX for k in JUDGMENTS)
assert set(AHEAD_CHECKS) <= set(CHECKS)
# 尋ねているかの問い合わせの受け持ち（意味の判定の2つと同じ会社の2つ。どちらか1つでも「尋ねている」なら質問＝Flow._asking_now・calling_asks）。
# ⛔ 2026-10-25 J10b（名前を確かめてから名乗りを聞かれる・S4）：受け持ちが 3.5 Flash Lite の1つだけで、混んで 4.7秒かかり待つ長さに届かず、
#    「岡田ですね。（間）失礼ですが、どちら様でしょうか？」に、質問と分からないまま目的の人かを確かめる一言を流した（会社を分ける決まり＝CL-1.md 0-2
#    に当たっていなかった）。G-59 の記録：3.5 Flash Lite の外れはどれも答えが返らない側（質問を見落とす）・Sonnet 5.5 は 234回中 0 の外れ・
#    質問ではない言葉を「尋ねている」と読んだ回はどちらも 0 ＝ どちらか1つで質問とみなしても、質問でない言葉を質問と取る回は増えない
ASKS_CHECKS = ("asks", "asks2")
HOLD_READINGS = JUDGMENTS + ASKS_CHECKS  # 「取り次ぎを待つ」を数える読み（Flow._hold_now・取り次ぎの区切り。尋ねているかの問い合わせの「取り次ぎを待つ」も）
CONFIRM_LABEL = {"affirmed": "合っている", "corrected": "違う値を言われた", "no_answer": "答えなかった"}
# 値を直されたあとの言い直し（✅ 2026-10-14 ユーザー）：(場面＝phrases.CONFIRM の鍵, 頭の言葉＝phrases.LEAD_IN の鍵, 流す相づち)
# - こちらの聞き違い（相手に「違う」と言われた）：「失礼いたしました。改めまして、16日の午後2時で、大丈夫でしょうか？」
# - 相手の言い間違い（相手が自分で言い直した・答えを変えた）：「承知しました。それでは、16日の午後2時で、よろしいでしょうか？」
# - AIが訂正を報告せずに自分で言い直し、相手が「はい」と言った（G-36）：「はい、念のため、16日の午後2時と、おっしゃいましたでしょうか。」
AFTER = {"misheard": ("聞き違いのあと", "お詫びのあとの言い直し", "shitsurei"),
         # 同じ項目を2度聞き違えた（名前の読みの3つ目の確かめ）：「申し訳ございません、」（重いほう）＋「改めまして…」
         "misheard_again": ("聞き違いのあと", "お詫びのあとの言い直し", "moushiwake_heavy"),
         "restated": ("言い直しのあと", "言い直しを受けて", "shouchi"),
         # 言い直さない質問への、会話がつながらない答えの意味合いを尋ねる（Flow._check_again。phrases.SENSE_ASK）：「はい、」＋「それで結構ですとおっしゃったのは、
         # 電話で大丈夫という意味合いでしょうか？」（✅ 2026-11-05 日本時間23時56分48秒 ユーザー「ア」）
         "sense": ("会話がつながらない答えのあと", None, "hai"),
         "unreported": ("聞き取りにくかったあと", "念のための言い直し", "hai"),
         # 受け入れにも断りにも取れる答え（「大丈夫です」）の意味を、AIが受け取ったほうで確かめる：
         # 「はい、」＋「念のためご予約はお済みということでよろしいでしょうか？」（DESIGN.md 6-10 の (3)）
         "ambiguous": ("曖昧な答えのあと", "念のための言い直し", "hai"),
         # 言い切っていない最初の答え（「たぶん予約してます」）：「はい、」＋「念のためご予約はお済みということでよろしかったでしょうか？」
         # （✅ 2026-10-18 ユーザー：「この場合、こちらの方が自然」。その前の案「…というご回答で…」は採らなかった）
         "hedged": ("言い切っていない答えのあと", "念のための言い直し", "hai"),
         # 言い切っていないが向きの決まった最初の答えの確かめ（Flow._hedged_after の1か所が選ぶ。頭の言葉は付けない＝文が「それでは」で始まる）：
         # - 事実を伺う質問ではない質問（希望・申し出）：「はい、」＋「それではパンフレットをお送りさせていただきますね？」（選択肢ごとの Question.do_words。
         #   ✅ 2026-11-01 日本時間20時56分・23時59分 ユーザー）。言葉が無ければ上の hedged
         # - 事実を伺う質問（Question.asks_fact）：「はい、」＋「それではご予約はお済みということで承っておいて大丈夫でしょうか？」（意味の言葉＋
         #   phrases.client_does_words＝業務の「依頼者がすること」＝Flow.client_does があれば「（それ）させていただいて」・無ければ「承っておいて」。
         #   ✅ 2026-11-01 日本時間6時05分 ユーザー：「「それでは、ご予約はされているということで登録(記録)させていただいて大丈夫でしょうか？」のような形で、
         #   登録するので確認しているんだよと伝わる形の方がいいでしょう。」・6時27分「依頼主が最終的に「登録」することになるのであれば、我々は依頼主の
         #   作業代行としてお客様と対峙しているので、それに合わせた方がいいでしょう。」・2026-11-08 日本時間5時33分05秒「以前、「～で記録させていただいて」
         #   という言い方にしていましたが、「～で承っておいて」に変更しましょうか。こちらの方が自然だし汎用的です。」）
         "hedged_do": ("言い切っていない答えのあと（こちらがすること）", None, "hai"),
         "hedged_fact": ("言い切っていない答えのあと（事実を伺う質問）", None, "hai"),
         # - 進める向きで、こちらがすることの言葉が決まらなかった：「はい、」＋「それでは当日はお車でお越しになるということで承りますね？」（意味の言葉で承る）
         "hedged_noted": ("言い切っていない答えのあと（承る）", None, "hai"),
         # 迷っているお客様に尋ねられて答えたあとの、こちらがすることの最終の確かめ（Flow._final_check）：「はい、」＋「それではパンフレットをお送りさせて
         # いただきましょうか？」。✅ 2026-11-06 日本時間11時19分55秒 ユーザー：「…「はい、それではパンフレットをお送りさせていただきましょうか？」という方向性
         # ですよね。話はパンフレットをから移っていないので戻す必要はなく、あいてが迷っている最中の最終確認をして、パンフレットのシーンを締めないといけないんじゃ
         # ないですか？」
         "final_do": ("迷っている方への最終の確かめ（こちらがすること）", None, "hai"),
         # 言い直しを作らずに、決まった一言で伺ってお返事を待つ2つ（_ask_confirm を通らない＝場面・頭の言葉は無い。相づちだけこの表から＝_ack_choice）：
         # - how：言い直しへのお返事がまだ決めていない（判定が答えを選んでいない）ときの2回目「はい、」＋「いかがなさいますか？」（Flow._how_now）。✅ 2026-11-09
         #   日本時間3時06分51秒 ユーザー：「同じ質問を繰り返しているし、悩んでいるのに、この返答はおかしいですよね。「いかがなさいますか？」のような返答が妥当じゃ
         #   ないですか？」
         # - pending：決められないのがお客様の最終の決定のとき「承知しました。」＋「それでは（聞くこと）は保留ということでよろしいでしょうか？」（Flow._ask_pending）。
         #   ✅ 2026-11-09 日本時間3時46分53秒 ユーザー：「…「それではこの件は保留ということでよろしいでしょうか？」のように聞いてあげた方がいいんじゃないですか？
         #   この状況でこちらに決めつけられるのは心外じゃないでしょうか？」
         "how": (None, None, "hai"),
         "pending": (None, None, "shouchi"),
         # どちらの場合もありうると言われた答え（「どっちでもいいですよ。」）を受け取らずに、依頼者が備える答えの可能性を確かめる（Question.maybe があるときだけ＝
         # Flow._ask_confirm の1か所）：「はい、」＋「当日はお車でお越しになる可能性もあるということでよろしいでしょうか？」（頭の言葉は付けない）。⛔✅ 2026-11-09
         # 日本時間4時36分49秒 ユーザー：「「お車で来る可能性もあるということでよろしいでしょうか？」とか、「駐車場が必要な可能性もあるということでよろしいでしょうか？」
         # というような確認になるんじゃないんですか？」（⛔ 前は、はい／いいえの質問に「どちらでも」が無く、判定が「決められない」と読んで「はい、恐れ入ります。
         # もう一度お聞かせいただいてもよろしいでしょうか？」と聞き直していた）
         "maybe": ("どちらにもなりうる答えのあと", None, "hai"),
         # 可能性の確かめへのお返事がはっきりしない（判定が答えを選んでいない）ときの2回目（言い直しへの返事がそろわないときの1か所＝Flow._confirm_again から）：
         # 「はい、」＋「それでは当日はお車でお越しになる可能性もあるということでよろしいでしょうか？」（矛盾や言い直しの答えを最終的に確かめる「それでは…」＝settle と
         # 同じ形。⛔ 入れる途中、流れの制御に通して見つけた：必須の事実を伺う質問で「それでは当日はお車でお越しになるということで…」と可能性を外して言い切っていた）
         "maybe_settle": ("どちらにもなりうる答えの最終の確かめ", None, "hai"),
         # 申し出（詳しい者より改めて N4・締める前の申し出 N6）への会話が繋がらないお返事に、会話AIがひとこと応えたあとの伺い直し（Flow._offer_again の1か所）：
         # 「さようでございますか。」（会話AIのひとこと・使えなければ受け答えの録音）＋「それでは月曜日を避けて、詳しい者よりお話しさせていただくということで
         # よろしいでしょうか？」（頭の言葉・相づちは付けない＝会話AIのひとことから始まる。最初の申し出の「…てもよろしいでしょうか？」はくり返さない）。
         # ✅ 2026-11-09 日本時間7時57分50秒 ユーザー：「1.(a)（お勧め）会話AIがお客様の言葉にひとこと応えてから、申し出を伺い直す。例：「さようでございますか。
         # それでは月曜日を避けて、詳しい者よりお話しさせていただくということでよろしいでしょうか？」いただいてもと同じことを言わない」
         "offer_again": ("申し出の伺い直し", None, None),
         "offer_alt": ("申し出の代わりの伺い直し", None, None),
         # ⛔ 2026-11-03 日本時間8時10分 ユーザー：「今の一言や案は全くダメです。あなたはチェックしたのですか？いい加減な提案をするな！！」＝目的から遠ざかる
         #    向きの選択肢に、取込のAIが作った「それでは（目的につなげる言葉）でよろしいでしょうか？」（hedged_keep）を外した（9つの質問の案のうち、
         #    営業の電話として筋が通ったのは今月中のご導入の「来月以降のご導入をお考えということ」の1つだけ。「パンフレットは必要になりましたらお送りするということ」＝「必要になるかを誰が決めるんですか？お客様から
         #    連絡させるつもりですか？」）。目的につなげて伺うのは、業務設定の追って伺う質問（Question.ask_if_hedged）だけ
         # 矛盾した答え・言い直しの答えを最終的に確かめる（会話になっていない返事・前の話と逆の返事・いまの言葉だけでは決められず前の話の答えを
         # 確かめる＝Flow._check_again の TALK_WHY・PRIOR_WHY）：「はい、」＋「それでは駐車場はご利用になるということでよろしいでしょうか？」
         # ✅ 2026-10-31 の夜 ユーザー：「『それでは、駐車場を(利用される)(利用されない)ということでよろしいでしょうか？』のような確認を入れる形で
         # 良いのではないでしょうか」「『念のため…でよろしかったでしょうか？』は、確認するまでもないけど、最後に、復唱する場合であり、『それでは…
         # ということでよろしいでしょうか？』は矛盾や言い直しの答えを最終的に確認する場合です。」（言い切っていない答えは hedged のまま）
         "settle": ("言い直しのあと", "言い直しを受けて", "hai"),
         # 電話を終える言葉か判定でも決まらず、いまの話への返事として向きを確かめる（Flow._topic_first）：「はい、」＋「パンフレットはお送りしない方が
         # いいということでしょうか？」（頭の言葉は付けない＝None。✅ 2026-11-01 日本時間22時31分 ユーザー）
         "end_doubt": ("いまの話の向きを伺う", None, "hai"),
         # 値が確定でない答え（「15日あたり」＝approx_value）・値の言い直しへの「まだ決まっていない」を受ける2つの形（Flow._tentative の1か所が選ぶ。頭の言葉は
         # 付けない＝文が「それでは」で始まる）。✅ 2026-11-08 日本時間3時48分03秒 ユーザー：「…確定で確認するのはあいての意思に反します。⇒「承知しました。
         # それでは15日あたりでお考えということで承っておきますね。」にするか、「はい、それではひとまず15日とさせていただきますね？変更があった場合には（～までに
         # ご連絡いただけましたら）（～までであれば）ご対応可能です。」」
         # - 業務設定に変更の受付の期限（Question.change_by）がある：「はい、」＋「それではひとまず15日あたりとさせていただきますね？変更があった場合には
         #   前日までにご連絡いただけましたらご対応可能です。」（値はお客様の言い方のまま＝5時33分05秒「相手が「あたり」をつけたら、こちらも「あたり」を
         #   つけた方がいいでしょう。」・確定した値で登録する質問 Question.definite_value だけ「ひとまず15日と」。事実を伺う質問は「それではひとまず10名くらい
         #   ということで承っておいて大丈夫でしょうか？…」）
         "tentative_do": ("言い切っていない答えのあと（こちらがすること）", None, "hai"),
         "tentative_fact": ("言い切っていない答えのあと（事実を伺う質問）", None, "hai"),
         # - 無い：「承知しました。」＋「それでは15日あたりでお考えということで承っておきますね。」（問いではない＝お客様のお返事が直した値でなければ受け取る。
         #   事実を伺う質問は「それでは10名くらいということで承っておきますね。」）
         "noted": ("確定でない値を承る", None, "shouchi"),
         # - 言い直し（ひとまずの形も）へ「まだ決まっていない」と返された：「承知しました。」＋「それではまだ確定ではないと承っておきますね。」（値は言わない・
         #   質問の種類で変わらない・期限があれば変更の受付の一文を添える。✅ 2026-11-08 日本時間5時33分05秒 ユーザー (2)・(4)「イ」。⛔ 5時00分52秒に入れた
         #   「それではひとまず15日の予定でお考えだと承っておきますね。」を置き換えた。5時00分52秒「…まだ決まっていないと言われたのに、「それでは15日で
         #   お考えということで承っておきますね。」だと確定のように聞こえ、相手に話が伝わっていないと思われてしまいますよ。」）
         "noted_undecided": ("まだ決まっていない値を承る", None, "shouchi")}
# 確定でない値を受ける形（Flow._tentative が選んだもの＝_ask_confirm の頭で回し直さない）
TENTATIVE_AFTERS = ("tentative_do", "tentative_fact", "noted", "noted_undecided")
HEDGE_WHY = "言い切っていない答え"  # 確かめる理由の名前（言い直しの言い方を、曖昧な言葉の確かめと分ける。Flow._check_again）
# 決める時（電話接続側の待つ長さの中＝Flow.on_decided）までに、その発話に出した意味の判定の確かな読みが1つも届かなかった答え（確かめる理由の名前＝Flow._checks の
# 1か所）：受け取らずに、その場で確かめる（判定の「決められない」とは別の理由＝尋ねた・まだ決めていない・迷っているの決まりには読まれない）。
# ✅ 2026-11-07 日本時間8時57分04秒 ユーザー「はい、その形でいいですが…」（答えの段は2秒の中に1回だけ決め、あとから覆す道を外す・2秒を超えた読みは通話後の確かめ）・
# 3時14分01秒「後から判定が覆るということ自体が正しい設定ならないんじゃないですか」。⛔ それまでは受け取って、遅れて届いた食い違いで「-」にし、締めの前に伺い直した
MISSED_WHY = "待つ長さの中に意味の判定の確かな読みが届かなかった答え"
# 会話になっていない返事（確かめる理由の名前＝Flow._check_again）。✅ 2026-10-31 ユーザー：「この部分だけの判断ならAIがあっています。前の分との
#    流れの回答として会話になっていないだけです。それを分かるようにしないと！」・前に言ったことと逆の返事は「(イ)」（「念のため…でよろしかった
#    でしょうか？」と言い直して確かめる＝お客様が考えを変えた場合もある）⇒ 判定の読みに意味の答え（選択肢）があれば、その答えを言い切っていない
#    答えと同じ言い方で確かめ、答えが無ければ（決められない・答えたくない など）質問から聞き直す
#    ⇒ 同じ日の夜 ユーザー：言い方は「それでは…ということでよろしいでしょうか？」（矛盾や言い直しの答えを最終的に確かめる＝AFTER の settle）
TALK_WHY = "会話になっていない返事"


def off_talk(q: "Question", got: str | None, prior: str | None = None) -> bool:
    """会話になっていない返事か（唯一の決まり。流れの制御 Flow._judged_off_talk と、測りの数え g53_meaning.flow_act が使う）：その前に相手が
    言ったことだけから決まる答え（prior＝meaning_judge.PRIOR）と、いまの言葉の答え（got）が、どちらも宣言した選択肢で、違う（前の話と合わない＝
    プログラムが比べる。✅ 2026-10-31 の昼 ユーザー：「(a) 検査OK」）。待つように言った・代わった人が話し始めた、の読みは答えではないので当てない
    （待つ・名乗る、の道が受け持つ）。判定に会話になっているかを答えさせる軸（meaning_judge の FITS）は外した（✅ 同じ日の夜 ユーザー：「(a) OK」。
    G-53final で、正しく救った答えは0・壊した答えは d1-ultra 19・Jev 3）。"""
    if got in NOT_ANSWERS:
        return False
    return prior in q.conditions and got in q.conditions and prior != got


def before_words(q: "Question | None", said: "list[str]") -> str | None:
    """判定に渡す「その前に相手が言ったこと」（唯一の決まり＝流れの制御 Flow.judge_context（呼んでいる段・答えの段）・ahead_context と、測りの場面
    g73_system_words.answer_scenes）：その段の相手の言葉 said（古い順）の、直前の3つまで。業務の質問には、前の答えも前の話として渡す（前に言ったことと
    食い違う答えを見つける＝G-70・10-31「(a) 検査OK」）。呼んでいる段の質問（呼ぶ一言・確かめる一言）には、取り次いだ方の言葉を渡す（target_facets）。
    プログラムが前の答えをきっかけに足す質問（システムの質問＝Question.callback。ほかのご希望 W1・ご承認いただけなかったときの N1〜N3・待つかかけ直すかの
    H2・ご遠慮の確かめ・かけ直しの時刻など）には渡さない（その質問への言葉だけで読む）：それまでの言葉は、それぞれの質問について判定がもう読み、流れの制御は
    その読みで要るものが無いと決めて、この質問を伺う＝同じ言葉をこの質問の前の話の答えとしてもう一度読むと、同じ言葉に2つの決めができる（一本化の破れ）。
    きっかけは直前の言葉だけではない（ご承認の手順の N3「ご説明した内容でご納得いただけましたでしょうか？」は、ご承認のお断り・点・ご説明への返事・「なるほど」の
    続きで伺う＝ご説明でお考えが変わったかを伺うのに、前の話のお断りで「いいえ」と読めば、ご納得の「はい」を食い違いとして伺い直す）。きっかけの言葉の中に
    この質問の答えになる言葉があれば、先に言われた答えとして、この質問について1回読み直す（ahead_context＝前の話ではなく、いまの言葉として）。
    ⛔ 2026-11-07 日本時間11時台（11時29分43秒のお言葉で入れた (イ)）：G-53d1base で、W1 のきっかけの「どちらも要らないです。」（出した選択肢が合わない＝
       11-03 19時54分）を、前の話の答えを読む判定が W1 の「無い」と読み（100件中32件）、「郵便で送ってもらえますか。」のような正しい答えにも意味合いを尋ねていた"""
    if q is not None and q.callback:
        return None
    return "／".join(said[-3:]) or None


def restates(q: "Question") -> bool:
    """確かめがそろわない答え（前の話と食い違う・前の話で補う・言い切っていない・判定と食い違う）を、意味の言葉で言い直して確かめるか（唯一の決まり＝
    流れの制御 Flow._check_again・測りの数え g53_meaning.flow_act）。言い直さない質問（Question.firm_not_needed＝ほかのご希望 W1・伺い方 C11・ご承認
    いただけなかったときの N1〜N3。CL-1 の 11-03 のご判断の (4)）は、質問から聞き直す（「はい、」＋「恐れ入ります。（聞くこと）をもう一度…」）。
    ⛔ 2026-11-05 日本時間20時台に見つけた：測りの数えは、言い直さない質問の前の話との食い違いも「念のため確かめる」と数えていた（本番の動きと違う）"""
    return not getattr(q, "firm_not_needed", False)


# いまの言葉だけでは決められず、前の話の答えで念のため確かめる（確かめる理由の名前＝Flow._check_again。言い切っていない答えと同じ言い方）
PRIOR_WHY = "前の話の答え"
PRIOR_BACK_WHY = f"{PRIOR_WHY}（いまの言葉だけでは決められず、その前に相手が言ったことから決まる答えを確かめる）"   # Flow._prior_back で補う理由（_compare・_undecided_answer）
END_DOUBT_WHY = "電話を終える言葉か決まらなかった答え"   # 確かめる理由の名前（いまの話を先に伺う＝Flow._topic_first。向きを確かめる）


def prior_answer(q: "Question", got: str | None, prior: str | None) -> str | None:
    """いまの言葉だけでは決められない（判定が「決められない」と読んだ）とき、前の話の答え（宣言した選択肢）があれば、それを念のため確かめる答え
    （唯一の決まり。流れの制御 Flow._compare と、測りの数え g53_meaning.flow_act が使う）。✅ 2026-10-31 の昼 ユーザー：「(a) 検査OK」＝いまの
    言葉は前の話を見せずに読む（meaning_judge.main_before）ので、前の話を指す返事（前に言ったとおりだ、など）は、ここで前の話の答えを確かめる"""
    return prior if got == UNDECIDED and prior in q.conditions else None
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
    if q.type not in CHOICE_TYPES:
        return ""
    note = (f"{q.id} の選択肢の意味：" + "／".join(f"「{c}」＝{q.meanings[c]}" for c in q.conditions if q.meanings.get(c)) + "。") if q.meanings else ""
    return note


# 返事を待つ知らせの添え書き：待っているもの「ではない」ことを言われたときの場面（段ごと。どの操作かは指示文の1か所＝ここに操作の名前を書かない）
NOT_WAITED = {
    "asking": "答えではないこと（いまは話せない・急いでいる・いらっしゃらない・あとでかけてほしい・別の番号へ・相手からの質問・お断りなど）を"
              "言われたら、record_answer にせず、",
    # ⛔ 2026-10-25 J8a：目的のお客様の後任の方ご本人の「岡田は異動になりまして、今は私が担当しております。」を、S2・S4 とも AIが最初に
    #    「代わりの方」と報告した（J の前の 10-22 は9本とも「後任の方」）。名乗りの返事の知らせ（呼んでいる段の _now）が「目的の人だと
    #    分かったら『目的の人』で報告」だけで、指示文のほかの道が見えにくくなっていた＝J5 の答えの段と同じ形
    "calling": "目的の人ではないこと（受付の方の取り次ぎ・代わりの方・後任の方・いらっしゃらない・在籍していない・相手からの質問・お断りなど）を"
               "言われたら、",
}


def answer_wait(how: str = "相手が答えたら record_answer で報告します。", phase: str = "asking") -> str:
    """質問を流した（聞いた）あと、相手の返事を待つときの知らせの唯一の場所：答えの報告の仕方（how）と、待っているものではないことを言われたときの道
    （phase＝段：asking＝質問の答えを待つ・calling＝目的のお客様にお取り次ぎいただくのを待つ）。
    ⛔ 2026-10-24 J5：名乗りの返事が「相手が答えたら record_answer で報告します。」だけだったので、名乗りのすぐあとの「本人は今ちょっと
       出かけてるんですけど」「あとでかけてもらえますか」「別の番号に…」を、AIが最初の質問の答えとして報告した（不在 S4・L4・あとで S4・
       別の番号 S2）。J の前は名乗りのあとに返事が無く、指示文のとおり request_callback を呼んでいた＝直前の知らせが、指示文の道を狭めていた
    ⛔ 2026-10-24 J6（上の直しの誤り）：ここに「いまは話を進められない＝request_callback」と操作の名前まで書いたら、「今ちょっと忙しいんですけど」
       （指示文では、急いでいる＝request_end）まで、3つの声とも request_callback にした＝どの言葉にどの操作かの正本は指示文の1か所。
       ⇒ ここは場面を挙げて、指示文のとおりにすることだけを言う（操作の名前を書かない）"""
    return how + NOT_WAITED[phase] + "はじめの指示のとおりに、それに合う操作を呼びます。"


def played_note(text: str, head: str = "") -> str:
    """プログラムが録音で流した言葉を会話AIに知らせる文の唯一の書き方（会話AIへの指示 instruction.system_rules が目印にする「プログラムが録音で
    流しました」の言葉）。head＝その言葉の役目・相手（「締めの言葉」「次の質問 Q1」「電話に出た方への」）。続けて会話AIがすること（何も言わずに待つ・
    続けて言う）は、知らせる所が書く。⛔ 2026-11-05 まで約20か所で手で書き、「は、」と「を、」が混ざり、電話に出た方・受付の方への確かめの一言は
    「と聞く録音を、プログラムが流しました」＝指示が目印にする言葉が入っていなかった（Intent.md 19-0ax の ⏳ の一本化）"""
    return f"{head}「{text}」は、プログラムが録音で流しました（相手にもう聞こえています）。"


# played_note の文（と、そのあとの「何も言わずに、」「続けて、」）。録音を会話AIが言う形に替える所（Flow._spoken_all）が外す
PLAYED_SENTENCE = re.compile(r"[^。「]*" + re.escape(played_note("ZZTEXTZZ")).replace("ZZTEXTZZ", "[^」]*") + "(?:何も言わずに、|続けて、)?")


def say_note(text: str, head: str = "") -> str:
    """決まった言い方（返事の say）を会話AIに言わせる知らせの唯一の書き方。head＝その言葉の役目・相手（「電話に出た方に」「もう一度、言い直し」）。
    足しも削りもしない（G-22・G-51：締めでAIが約束の言葉を足した＝この決まりは、どの決まった言い方にも当たる）。続けて会話AIがすること
    （相手の返事を待つ など）は、知らせる所が書く。⛔ 2026-11-05 まで約20か所で「とだけ言います」「とだけ聞きます」「とだけ伺います」「と言って」
    「と聞き返して」「ともう一度だけ伺って」「と1回だけ聞いてください」「と言ってから」と、言い方が混ざっていた"""
    return f"{head}「{text}」とだけ言います（言葉を足しも削りもしません）。"


def choice_words(q: "Question", cond: str | None) -> str | None:
    """選択肢の答えを言い直すときの言葉（「（言葉）でよろしいでしょうか？」の（言葉）。phrases.confirm の meaning）。取込で決めた意味の言葉
    （Question.meanings。「ご予約はお済みということ」）、無ければ選択肢の名前（「カード」）。名前が返事の言葉だけなら、言い直せないので None。
    ⛔ 2026-10-17 に見つけた穴：選択肢の型を聞き返したあとの言い直しが「はいとおっしゃいましたでしょうか？」になっていた
    「どれも要らない」は、どの道でも言い直さない（_ask_confirm の頭の _keep_toward_goal が先に受け取る＝ここへは来ない。前の「念のためどちらもご不要と
    いうことでよろしかったでしょうか？」の行は 2026-11-03 の見直しで外した＝NoneOfThemTest.test_none_is_never_restated）"""
    if cond == ANY:  # 任された答え（言い切っていなければ「それではどちらでも構わないということで承りますね？」＝_hedged_after の hedged_noted）
        return "どちらでも構わない"
    w = q.meanings.get(cond) or cond or ""
    return None if not w or _BARE_REPLY.fullmatch(w) else w
# 終わりを求める理由（request_end）。「通話を断られた」＝発信の拒否（営業お断り・もうかけないで）＝発信禁止の申告（Intent.md 原則14。
# DESIGN.md 6-2 の断りの3つの上の段）。「相手が怒っている」＝怒り・苦情＝弁明や約束をせずに終え、人へ知らせる印（9-4・7-1 の順7）。
# ⛔ 2026-10-18 の見直しで見つけた漏れ：前は「通話を断られた」で締めるだけで、発信禁止の申告をどこにも残していなかった（原則14 の
#    「その場で発信禁止に登録」に届かない）。怒り・苦情を報告する手立ても無かった
END_LABEL = {"done": "質問が終わった", "callee_hurry": "相手が急いでいる", "callee_refused_call": "通話を断られた",
             "callee_complaint": "相手が怒っている", "not_transferred": "取り次いでいただけなかった", "transfer_unclear": "理由を言わずに取り次げないと言われた",
             "who_unclear": "お名前だけではどの方か分からないと言われた",
             "not_agreed": "ご納得いただけなかった点を、担当の者に申し伝えることにした"}
# 目的の人に繋がらずに終わった理由（会話の報告の not_reached。Flow.not_reached の1か所で決める）。✅ 2026-10-22 ユーザー決定（CL-2 の 4-130）：
# 「『営業拒否』：もうかけない。『事情があり、取り次げなかった』：日を改めて1回。『約束した時刻のかけ直し』：約束した時間と同じ」
# ⇒ 営業のお断り＝通話を断られた（発信の拒否。ここには入れない）／お呼びした方が出られない（戻る時刻を聞いた＝かけ直しの申し出）＝absent／
#    それ以外の事情で取り次いでいただけなかった＝not_transferred（時刻が取れたかは通話の事実 callback が持つ＝約束の時刻は CL-2 が決める）
# ✅ 2026-10-22 ユーザーの決定（相手の申し出）で足した理由：在籍していない（退職・異動。後任の方にも繋がらなかった・終える業務）／ほかの手段を
# 望まれた（メールで送って など。電話ではなく、その手段で）。どちらも、次にどうするかは CL-2 が決める
NOT_REACHED_LABEL = {"absent": "不在", "not_transferred": "取り次いでいただけなかった", "departed": "在籍していない",
                     "other_means": "ほかの手段を望まれた", "who_unclear": "お名前だけではどの方か分からなかった"}
# 最終目的の果たし方（業務設定＝CL-2。Flow の goal_mode）：any＝最終目的のどれか1つが果たせれば業務の目的が果たせる（1つ断られても、ほかが残れば
# 質問を続ける）／all＝全部が要る（1つ断られたら果たせない）。✅ 2026-11-01 日本時間22時31分 ユーザー：「片方だけでも目的が達成できるときは、
# 質問を続け、片方断られたら、目的は達成できない場合は、途中で締めるという形になるんではないでしょうか。」
GOAL_MODES = ("any", "all")
# 断られたときの締め（CLOSE_DECLINED）で終える理由。念のための確認はしない（DESIGN.md 6-5）
DECLINED_ENDS = ("callee_hurry", "callee_refused_call", "callee_complaint")
REPLY_KEY_LABEL = {"result": "結果", "message": "次にすること", "question_id": "質問", "question": "質問の文", "value": "値", "say": "言い方"}
RESULT_LABEL = {"next": "次の質問", "confirm_required": "言い直して確かめる", "rejected": "差し戻し", "ask_back": "聞き返す",
                "recheck": "念のための確認", "end": "締め", "busy_ask": "引き止めの一言", "replayed": "録音で流し直した",
                "hold": "取り次ぎを待つ", "noted": "受け付けた", "confirm_target": "目的の人か確かめる", "call_target": "目的の人をお呼びし直す", "ai_question": "エーアイか聞かれた",
                "restate": "言い直す", "wait": "最後まで聞く", "opening": "名乗り", "own_answer": "自分の言葉で答える", "callee_asked": "先に相手の質問に答える",
                "end_trouble": "こちらの不具合で終える"}
# 名乗りの流し直し（DESIGN.md 6-13。✅ 2026-10-13 ユーザー：名乗り直しも録音で流す「間が大丈夫ならOK」）：AIが選ぶ部分の名前
# 名乗り＝「どちら様ですか」／用件＝「何のご用件ですか」／最初から＝聞き取れなかった様子（「もう一度」「はい？」）／代わった方へ＝別の人に代わった
# エーアイか聞かれた＝「AIですか？」「人間ですか？」への決まった言い方（instruction.Business の ai_answer。Flow._ai_question）。
# 質問に戻る＝その言い方に、相手が問題は無いと答えた（いまの位置へ戻る。Flow._resume）。
# ⛔ 2026-10-19 G-57：AIに自分で答えさせると、3本とも答えたあと黙り、いまの質問に戻らなかった ⇒ 決まった言い方はプログラムが渡し、戻るのもプログラム
# 想定問答の答えは、業務設定の問いの名前で選ぶ（ANSWER_PREFIX ＋ 問いの名前）。録音の無い問い（想定問答にも、名乗り・用件にも無い）は、AIが
# 自分の言葉で答える（Flow._own_answer）：業務の情報（instruction.Business.info）にある問い＝自分で答える質問／業務の情報にも無い問い＝答えを
# 持っていない質問（作らずに、その旨を自分の言葉で伝える）。どちらも、相手からの質問として通話の事実に残す（依頼主へ渡す）。
# ⛔ 2026-10-22 の前の形（答えを持っていない問いには、どれも録音「申し訳ございません、」＋「私の方では分かりかねます。」）は取り下げた
#    （✅ ユーザー：「『只今、岡田は会議中ですので、折り返し電話するようにお伝えしましょうか？』のような質問に…『私の方では分かりかねます。』と
#    答えるんですか？…人以上の仕組みを作ることが私たちの目的ではないのですか？」）。⛔ G-57 tc19：「分からない質問」という名前では、AIは番号の
#    出どころ・キャンセル料の問いの6本中4本で「用件」を選んだ（聞き取れなかった問いとも読める名前だった）
PART_LABEL = {"intro": "名乗り", "purpose": "用件", "all": "最初から", "handover": "代わった方へ", "ai": "エーアイか聞かれた", "target": "お呼びした方",
              "resume": "質問に戻る", "own": "自分で答える質問", "no_answer": "答えを持っていない質問", "talk": "受け答え"}
# AIが自分の言葉で言う部分（Flow._own_answer の1か所）：質問への答え2つと、質問ではない言葉（世間話・お礼・気遣い・お客様の話）への受け答え。
# ✅ 2026-10-22 ユーザー（5回目）：「世間話のような対応や設定にない無難な対応はライブAIにしかできないよ！」（Intent.md 9-3「AIが柔軟にやること：
# …想定外の発話への受け答え」）⛔ 前は質問への答えの2つしか無く、録音で流す通話の答えの段では、世間話に返す道が無かった
OWN_PARTS = ("own", "no_answer", "talk")
# 答えの段で、いまの質問・言い直しに答える代わりにこちらへ尋ねられた問い（Flow._asked_now）に答えて、お客様の返事を待っている印（Flow.own_pending の値。
# 立てるのは Flow._await_reply の1か所）：お客様の返事が答えなら、質問に戻らずに答えとして受け取る（Flow._resume・_answer_in_reply）
ASKED_REPLY = "asked"
# 想定問答の問いの名前に使えない名前（AIが流し直しで選ぶ部分の、中の名前と日本語の名前。AIの報告は日本語の名前を中の名前に読み替えて
# から問いの名前を探すので、同じ名前の問いには届かない＝Flow.__init__ が断る。CL-2 の業務設定の確かめ ANSWER_RESERVED も、この一覧に合わせる）。
# ⛔ 2026-10-22 の見直しで見つけた漏れ：CL-2 は中の名前だけを断っていた＝問いの名前が「用件」だと、用件の録音が流れて答えが流れない
RESERVED_PART_NAMES = frozenset(PART_LABEL) | frozenset(PART_LABEL.values())
ANSWER_PREFIX = "answer_"  # 想定問答の答えの、録音の部分の名前の頭（instruction.Business.replay_parts と同じ。録音のファイル名になるので「:」を使わない＝Windows）
OPENING_ORDER = ("greeting", "intro", "purpose")  # 名乗りの録音の部分の順（instruction.Business.replay_parts の鍵）
# 相手の種別（DESIGN.md 4-3。report_party）：電話に出た相手が何か（AIに見せる名前なので英字を使わない＝FAX は「ファクス」）。受付・目的の人は、目的の人のある業務だけでAIに見せる
# （「誰でも」の業務は、最初の相手が目的の人＝報告しなくてよい）。人ではない相手（留守番電話・自動音声の案内・FAX）には話さずに終え、
# 番号違いは決まった締めで終える（Intent.md 9-4）
# 代わりの方＝お呼びした方の代わりに、電話口の方がご用件を伺うと申し出た（上司の方・同じ部署の方など）／後任の方＝お呼びした方は在籍しておらず、
# 電話口の方が後任・いまの担当だと言った（✅ 2026-10-22 ユーザー：代わりの方は「判断可能な人なら、話す。+業務ごとに選べる」・在籍していない
# ときは「基本は後任の方をお呼びする+業務ごとに選べる」。どちらも Flow._not_available の1か所が業務設定で決める）
PARTY_LABEL = {"reception": "受付の方", "target": "目的の人", "substitute": "代わりの方", "successor": "後任の方", "voicemail": "留守番電話",
               "ivr": "自動音声の案内", "fax": "ファクスの音", "wrong_party": "番号違い", "person": "電話に出た方（人）"}
NOT_A_PERSON = ("voicemail", "ivr", "fax")
# person＝電話に出た方の第一声で、人だと分かった（役はまだ分からない。CL-2 の call_report.PARTY_KINDS の person と同じ）。第一声を聞く段だけで使う
# （名乗りは、人と分かってから流す＝Flow._opening_when_ready）
# 通話の始め（✅ 2026-10-24 ユーザー：電話に出た方の第一声を待ってから名乗る＝CL-1.md 4-45・DESIGN.md 6-13）。⏳ どの秒も本物の電話で測って決める
FIRST_WORDS_SILENCE_S = 2.0   # 繋がってから第一声が無いまま（「もしもし」のあとも）この秒で、「もしもし」で確かめる
MOSHIMOSHI_TRIES = 2          # 「もしもし」は2回まで。そのあとも黙っていれば、何も言わずに切る（会話に至らなかった）
VOICEMAIL_WAIT_S = 8.0        # 第一声が留守番電話の案内と分かってから、黙って聞き続ける秒（案内のあとに人が出ることがある＝ユーザー）
# 締めた理由の全部（通話の事実 end の reason。Flow._end の1か所で確かめる。CL-2 の call_report.END_REASONS と同じ＝試験で比べる）：AIが選ぶもの
# （END_LABEL）と、流れの制御が決めるもの（人ではない相手・番号違い・かけ直しの申し出・ほかの手段・在籍していない・保留で待ちきった）。
# ⛔ 2026-10-24 の見直しで見つけた穴：保留で待ちきった締めを、いつも「取り次いでいただけなかった」で残していた＝目的のお客様に繋がった
#    あとの保留（「確認しますので少々お待ちください」）・目的のお客様のいない業務の保留でも、事実と違う理由になった。目的のお客様に
#    繋がらなかった理由は、締めた理由とは別に Flow.not_reached の1か所で決める（保留で待ちきった呼んでいる段は、取り次いでいただけなかった）
END_REASONS = (*END_LABEL, *NOT_A_PERSON, "wrong_party", "callback", "other_means", "departed", "hold_timeout", "goal_refused")
# こちらの不具合で電話を途中で終える理由（CL-2 の call.end_for_trouble の理由＝attempts.TROUBLES の3つ。CL-2 の連絡帳 4-156・4-158）。決めるのは
# Flow._trouble の1か所（DESIGN.md 6-8）。✅ 2026-10-24 ユーザー：甚大とは「プログラムやAIがこちらの意図しない相手がおかしいと思うような状況に
# 陥った場合全般や、回線が悪く、声がお互いや片方がほとんど聞き取れない場合など規定通りの会話が進められない状態」。締めた理由（END_REASONS）とは
# 別（終えるのも記録するのも CL-2 の電話の部品＝会話の報告の締めの理由には入れない）
TROUBLE_REASONS = ("ai_malfunction", "poor_audio", "cannot_proceed")
# 止まったAIの出来事（通話の報告の ai_trouble の what＝Flow.on_ai_trouble の1か所。CL-2 の call_report.AI_TROUBLE_WHATS と同じ＝試験で比べる）と、
# 別の耳の名前の頭（「別の耳（種類）」。CL-2 の errors.EAR_AI と同じ＝別の耳が止まったら緊急の道で再開しない、を名前で見分ける。CL-2 の 4-173 (b)・4-174 (b)）。
# 会話AIの名前と、緊急の道でない道の名前も、CL-2 の errors.CONVO_AI・errors.DIRECT と同じ（名前で、会話AIが止まった・緊急の道を出せない、を見分ける）
AI_TROUBLE_WHATS = ("止まった", "緊急の道で答えた", "おかしな動き")
REPORT_VERSION = 3   # 会話の報告の版（Flow.report。CL-2 の call_report.VERSION と同じ＝試験で比べる）
EAR_AI = "別の耳"
CONVO_AI = "会話AI"
DIRECT_ROUTE = "直接"


def ear_ai(kind: str) -> str:
    """別の耳の名前（「別の耳（種類）」）を作る唯一の場所：通話の途中で止まったとき（通話の報告の ai_trouble）と回復の確かめ（convo.check_ais）の
    両方がこれを使う＝止まった別の耳が答えるようになったかを、名前で突き合わせられる。"""
    return f"{EAR_AI}（{kind}）"

# 通話の事実の種類の全部（Flow._note の1か所で確かめる。CL-2 の call_report.FACT_KINDS と同じ＝試験で比べる。中身は CL-1.md 2-2）。
# out_of_scope（範囲外の求めで終えた）は、まだ書かない（実際にあるかを測ってから作る＝2-2 の「まだ作っていないもの」）
FACT_KINDS = ("do_not_call", "complaint", "out_of_scope", "party", "callback", "ai_question", "voicemail_message", "end", "connect", "answered",
              "target_back", "callback_resume", "not_transferred", "substitute", "departed", "callee_question", "talk", "hold_timeout",
              "end_unjudged", "goal_refused", "who_unclear", "not_agreed", "other_wish", "ended_on", "concern", "handoff",
              "close_offer", "offer_terms")   # end_unjudged：判定の答えが届かず、会話AIの読みで丁寧に締めた（Flow._end_late。通話のあとの確かめが読み直す）
# offer_terms：業務の申し出の質問の伺い直しに入れた条件・条件つきで受けていただいた条件（Flow._proposal_note の1か所。question・terms・added・topic。terms・topic は
# 伺い直したときから＝受けたかはその質問の答え・added は受けていただいたときだけ＝✅ 2026-11-12 まとめ役の (J2)。申し出のシステムの質問は handoff・close_offer に残す）
# goal_refused：最終目的の申し出を断られ、業務の目的が果たせなくなったので途中で締めた（Flow._goals_lost。その業務だけかけない＝CL-2）
# concern：本題に入ったあとの「もういいです。」に本心を伺い（「何か問題でもございましたでしょうか？」）、こちらの話し方・進め方・ご案内についての気になる点を
# 言っていただいた（Flow._concern_decided。question＝そのとき伺っていた業務の質問・said＝お客様の言葉。会話AIが応えた文は callee_question と同じ形＝
# answered・answer・drafts・checked＝書き手は _question_note の1か所。依頼者へ返す＝今後の改善。✅ 2026-11-08 日本時間2時21分15秒 ユーザー）
# not_agreed：説明した内容をご承認いただけず、ご納得いただけなかった点を伺った答え（question＝ご承認の質問・said＝お客様の言葉。Flow._follow_ups。依頼者へ返す）
# ended_on：ここで電話を終える答え（Question.ends_on）で、残りの業務の質問を伺わなかった（Flow.ended_on が通話の終わりの答えで決め、Flow.report が入れる。
# question＝止めた質問・answer＝その答え（日本語の名前）・skipped＝伺わなかった業務の質問。CL-2 の判定は skipped を「伺っていない（対象外）」と数える）
# other_wish：選ぶ質問に「どれも要らない」と言われ、ほかのご希望を伺った答え（Flow._wish_step。question＝その質問・wish＝ほかのご希望の選択肢の名前
# ・value＝お客様が言ったご希望・means＝ご連絡の方法（WISH_MEANS の鍵）・contact＝その方法の連絡先・outcome＝option（元の質問の選択肢を選び直した）／
# means（ご連絡の方法）／other（ほかのご希望）／none（無い）。依頼者へ返す）
# who_unclear：受付の方に、お名前だけではどの方か分からないと言われ、部署・フルネームでお呼びし直しても（recalled）・お伝えすることが無くて（発信対象データに
# フルネームが無い）、取り次いでいただけなかった（Flow._who_unclear。日を改めて同じお名前でかけても同じになる＝CL-2 の次の動きが読む）
PERSON_PARTIES = ("reception", "target")
STANDIN_PARTIES = ("substitute", "successor")  # 目的の人の代わりにお話しうる方（目的の人のある業務の、呼んでいる段だけ）
# かけ直しの申し出（DESIGN.md 6-2 の request_callback。CL-2 の依頼・✅ 2026-10-14 ユーザー「相手が別の番号を言ったら、基本はその番号へかける」）：
# 理由は3つだけ。理由ごとに、システムが足す質問（CALLBACK_ASK の役目の順）。足した質問は C1（時刻）・C2（番号）で、どの業務でも同じ番号
# ✅ 2026-10-22 ユーザーの決定で足した理由：長期の不在（出張など＝「戻る日時やおおよその帰社日を聞き、その時に折り返す」。休み・休職は 10-25 に
# お休みへ分けた）・在籍
# していない（退職・異動＝後任の方をお呼びするか終えるかは業務設定。Flow._departed）・ほかの手段（「メールで送って」＝「希望とメールアドレスを
# 記録して、お礼を言って締める」。どの手段かは相手の申し出 OFFER_LABEL）
# 「あとで」のかけ直しで「〇時まで」と言われたときの、いま電話を続けられない理由（CL-2 の call_report.UNTIL_WHYS。✅ 2026-10-23 ユーザー：「かけ直さないと
# いけない理由によるでしょ。3時までに別の番号にかけてなら、すぐでいいし、3時まで出られないなら、3時以降になるし、3時までに担当者へ聞いて折り返してなら、
# 確認後になるだろうし」＝CL-2 の 4-149。✅ 2026-10-24 ユーザー「進めてよい」）。ほかのかけ直しの理由は、理由そのものが決める（CL-2 の UNTIL_BY_REASON＝書かない）
UNTIL_LABEL = {"other_party": "別の番号・別の方へ", "unavailable": "それまでは出られない", "preparing": "それまでに確認・準備"}
# かけ直しの日・頃を、月単位の理由で選んだ（かけ直しの記録の cycle。✅ 2026-10-19 ユーザー：「月ぎめの業務の都合や振り込みなどの入金の間隔などの話の
# 場合は、月単位で考えた方がいい」＝CL-2 の 4-106。言葉だけでは、たまたまその頃になったのか分からないので、理由を言われたときだけ書く）
CYCLE_LABEL = {"month": "月単位の理由（月ぎめ・締め・振り込みや入金の間隔など）"}
# 「わからない」「答えたくない」の理由（項目の reason。業務の決まりが、理由×扱いで次の動きを決める＝CL-2 の business_rules の DONT_KNOW_REASONS・
# REFUSED_REASONS・4-37）。最後の1つは受け皿（理由を言わなかった）。AIが理由を選ばなければ受け皿にする
DONT_KNOW_LABEL = {"not_in_charge": "担当ではない", "undecided": "まだ決まっていない", "unknown_now": "いまは分からない",
                   "unspecified": "わからない理由を言わなかった"}
REFUSED_LABEL = {"policy": "決まりで言えない", "unwilling": "答えたくない理由を言わなかった"}
UNKNOWN_WHY_LABEL = {**DONT_KNOW_LABEL, **REFUSED_LABEL}
# 「わからない」の理由のうち、まだ決めていない（迷っている・決められない）のではなく、その理由の業務の決まり（分かる方・分かる時を伺う）で進むもの（唯一の組＝
# 迷っているお客様への応じ方 Flow._hesitation・最終の確かめのあとの受け取り Flow._confirm_again）：担当ではない（その方の決定ではない）・いまは分からない
WHY_ROUTED = ("not_in_charge", "unknown_now")
# 「〇時まで」「〇日まで」（いま電話を続けられない理由で、かけてよい時間・日が決まる言い方。理由を伺い直す・記録に until_why を書く唯一の
# 見分け。CL-2 の 4-149・4-155：「来月の5日までに」も「〇時まで」と同じ。「〇日以降」「〇日ごろ」は理由なしで読める）。
# 言葉の終わり（UNTIL_TAIL）は、CL-2 の時刻の読み（spoken_time._UNTIL_TAIL）と同じ並び（比べる試験。⏳ フェーズ1で spoken_time を呼ぶ1か所に）。
# ⛔ 2026-11-02 まで「より前」だけで、「3時前なら」を「〇時まで」と読まず、理由を伺わなかった（CL-2 は「3時まで」と同じ幅に読み、理由が無いので
#    確かめの要る時刻にした＝CL-2 の連絡帳 4-182 の (3)）。裸の「前」は時・時半のあとだけ：「3時前後」は「3時ごろ」（CL-2 も先に読み分ける）、
#    「3時10分前」は2時50分とも3時10分より前とも取れる（CL-2 は決めない）、「3日前」は日付ではない
UNTIL_TAIL = ("までに", "まで", "より前", "前")
_UNTIL = re.compile(rf"\d{{1,2}}時半?(?:{'|'.join(UNTIL_TAIL[:-1])}|前(?!後))|\d{{1,2}}(?:時\d{{1,2}}分|日)(?:{'|'.join(UNTIL_TAIL[:-1])})")
# ✅ 2026-10-25 日本時間の夕方 ユーザー：お休みは長期の不在から分ける（「本日は休み・出張 → 「いつごろお戻りのご予定かお分かりでしょうか？」
# 出張ならいいが、お休みだとおかしい⇒ 「次のご出社はいつかお分かりになりますでしょうか？」」）。違うのは伺う一言だけ（CALLBACK_PLAN）
CALLBACK_LABEL = {"absent": "不在", "day_off": "お休み", "long_absent": "長期の不在", "other_number": "別の番号へ", "later": "あとで",
                  "departed": "在籍していない", "other_means": "ほかの手段", "unavailable": "今はお電話に出られないご事情"}
# お呼びした方の事情で、いまはお話しできない理由（電話口の方ご自身の申し出ではない＝別の方に代わっても、代わりの方とお話しできないときに伺う
# のは、お呼びした方の戻り。唯一の場所＝Flow の呼んでいる段の3か所が読む。CL-2 の call_report.ABSENT_REASONS も同じ仲間）
# 今はお電話に出られないご事情（unavailable）＝理由を言わずに取り次げないと言われ、ご事情かを伺った一言に、はい・ご事情を答えられた（Flow._transfer_unclear）
AWAY_WHYS = ("absent", "day_off", "long_absent", "unavailable")
# 相手の申し出（request_callback の、無くてもよい項目）：折り返し・伝言（お呼びした方が出られないときの申し出）／メール・ファクス・郵送（ほかの手段で
# 送ってほしい）。✅ 2026-10-22 ユーザー：折り返し＝「お礼を言い、こちらからかけ直す形にして戻る時刻を聞く」・伝言＝「業務ごとに選べるようにし、
# 選んでいなければ (a)」（お礼を言って断り、戻る時刻を聞く）
OFFER_LABEL = {"callback": "折り返し", "message": "伝言", "email": "メール", "fax": "ファクス", "post": "郵送"}
# 受け答えの文が使えないときに流す一言（phrases.TALK_FALLBACK の録音。AIは言い方そのものを選ぶ。同じ言葉の別の調子は、名前に調子を添えて分ける）
TALK_LABEL = {k: f"{w}（{tone}）" if tone else w for k, (w, tone, _) in TALK_FALLBACK.items()}
TALK_CHOICES = "、".join(f"{TALK_LABEL[k]}＝{scene}" for k, (_, _, scene) in TALK_FALLBACK.items())  # AIに見せる、選び方の説明（1か所）
# ご説明できない点の種類（phrases.POINT_KINDS。答えを持っていない応え方で、ご説明する段のときだけ。AIが答えの文と一緒に選ぶ＝受け答えの一言と同じ形）
POINT_KIND_LABEL = {k: noun for k, (noun, _) in POINT_KINDS.items()}
POINT_KIND_CHOICES = ("「ご質問」＝分からない点・確かめたいことを言った、「ご要望」＝こうしてほしいという望みを言った、"
                      "「ご意見」＝お考え・感想を言った")
MEANS = ("email", "fax", "post")
CALLBACK_PLAN = {"absent": ("back_time",), "day_off": ("next_workday",), "long_absent": ("back_date",),
                 "other_number": ("number", "reachable_time"), "later": ("later_time",), "other_means": (), "unavailable": ("transfer_time",)}
MEANS_PLAN = {"email": ("email",), "fax": ("fax",), "post": ("address",)}  # ほかの手段で、聞くこと（手段の申し出が無ければ、聞かずに締める）
# システムが足す質問の番号（どの業務でも同じ）と、通話の事実（callback）に残すときの名前
CALLBACK_IDS = {"time": "C1", "number": "C2", "email": "C3", "fax": "C4", "address": "C5", "back_today": "C6", "again": "C7", "who": "C8",
                "when": "C9", "message": "M1", "promise_again": "P1",
                "refusal": "D1", "hurry": "H1", "not_agreed": "N1", "other_point": "N2", "agreed_now": "N3", "line": "C10", "other_wish": "W1",
                "line_how": "C11", "mobile": "C12", "wait_or_later": "H2", "concern": "D2", "handoff": "N4", "handoff_way": "N5",
                "close_offer": "N6"}
ROLE_KEY = {"back_time": "time", "later_time": "time", "back_date": "time", "next_workday": "time", "reachable_time": "time", "transfer_time": "time",
            "number": "number",
            "email": "email",
            "fax": "fax", "address": "address", "message": "message", "back_today": "back_today", "call_again": "again",
            "promise_again": "promise_again", "ask_who": "who", "ask_when": "when", "refusal": "refusal",
            "hurry_again": "hurry", "hurry_when": "time", "not_agreed": "not_agreed", "other_point": "other_point", "agreed_now": "agreed_now",
            "line": "line", "other_wish": "other_wish", "line_how": "line_how", "mobile": "mobile", "wait_or_later": "wait_or_later",
            "concern": "concern", "handoff": "handoff", "handoff_way": "handoff_way", "close_offer": "close_offer"}
# 必須の質問の「わからない」「答えたくない」の扱い（業務の決まり＝CL-2 の business_rules.HANDLINGS と同じ名前。Question.handlings の値）：
# 分かるお客様を聞く・分かる時を聞く（聞き足しの質問を1通話1回）・日を改める（会話は次の質問へ進む）・結論として終える（丁寧に締める）
HANDLINGS = ("ask_who", "ask_when", "retry", "conclude")
# かけ直しの記録（通話の事実 callback）の理由になりうる名前の唯一の一覧（CL-2 の call_report.CALLBACK_REASONS と同じ＝比べる試験。CL-2 の
# 連絡帳 4-188 の (d)：⛔ 2026-11-02 に unavailable を足したとき、一覧どうしを比べていなかったので CL-2 の側の抜けに気づけなかった）。
# CALLBACK_LABEL の「在籍していない」（departed）は理由にしない（通話の事実 departed＝Flow._departed。4-69 の 4-182 の (5)）・
# 伺い直し（分かる方・分かる時）は、伺い直しの記録の理由（質問の callback＝HANDLINGS の ask_who・ask_when）
CALLBACK_REASONS = (*(k for k in CALLBACK_LABEL if k != "departed"), "ask_who", "ask_when")
YES_NO_FACT = ("back_today", "again")   # かけ直しの記録に、はい／いいえ（真偽）で入れる答え（CL-2 の call_report.Callback の back_today・again）
# システムの質問の答えが、かけ直しの記録（通話の事実 callback）のどの項目に入るか（Flow._callback_fields の1か所。CL-2 の call_report が受ける
# 項目だけ）。約束の電話の一言（P1）は、取り次げなかった記録（not_transferred）の again に入るので、ここには無い。分かる時（C9）は時刻。
# ⛔ 2026-10-24 の見直し（調べ役）で見つけた穴：記録の項目を、聞いた質問の役目の名前から作っていた＝P1 が「promise_again」として入り、
#    CL-2 が報告ごと断った／はい・いいえの答えが取れていないと null を書き、CL-2 が報告ごと断った（相手が切った・「わからない」のとき）
CALLBACK_FIELD = {"C1": "time", "C2": "number", "C3": "email", "C4": "fax", "C5": "address", "C6": "back_today", "C7": "again",
                  "C8": "person", "C9": "time", "M1": "message"}
# 伺ったのに「はい」でなければ「いいえ」（よいと言われていない＝CL-2 の 4-135）と書く問い。ほかのはい／いいえは、取れたときだけ書く
# （「本日お戻りのご予定」の「わからない」は、戻らないとは言われていない）
NOT_YES_IS_NO = ("C7", "P1")
# 相手の申し出への応じ方（業務設定。instruction.Business の substitute_policy・message_policy・departed_policy・follow_up）。最初が既定（ユーザーの決定）
POLICIES = {"substitute": ("decision_maker", "target_only", "anyone"), "message": ("decline", "leave"), "departed": ("successor", "end"),
            # 本題の始まり（Flow._main_talk）：asked＝最初の業務の質問を流したあと（既定）／answered＝この電話で業務の答えを1つ以上いただいたあと。
            # 続きの電話はどちらでも本題。✅ 2026-11-08 日本時間3時15分04秒 ユーザー「(ア)業務ごとに選択できるようにしてください。」・3時48分03秒
            # 「(ア)既定は「最初の質問を流したあと」」
            "main_talk": ("asked", "answered")}
SUBSTITUTE_ID = "S1"  # 代わりの方に、判断いただけるお立場かを聞く質問（Flow._substitute）
# 目的の人を呼んでいる段（calling）の、意味の判定の「質問」（呼ぶ一言）の選択肢と意味の言葉（DESIGN.md 1-1：AIの報告「目的の人」を、
# 独立した判定とそろったときだけ受け取る。取り次ぎ＝HOLD・代わった人＝HANDOVER は、判定のほうの答えでない言葉）
TARGET_IS_THERE, TARGET_IS_AWAY, TARGET_IS_NOT = "ご本人", "不在", "別の方"
# 電話に出た方が、こちらに尋ねているか（呼んでいる段だけ、目的の人の判定とは別の問い合わせ＝確かめの名前 "asks"。電話接続側が同時に出す）。
# ✅ 2026-10-22 ユーザー：「相手が喋っていても、それが質問だった場合の対処は？」。⛔ G-57 tc14：「岡田ですね。（間）失礼ですが、どちら様でしょうか？」を、
#    AIは「目的の人」と報告し、判定は「ご本人」「取り次ぎを待つ」と分かれ、質問に答える前に「〜様でいらっしゃいますでしょうか？」と確かめた（3本とも）。
# ⛔ 目的の人の判定の選択肢に「こちらに聞いている」を足す形は取り下げ（G-59 10-22）：質問は全部読めたが、ほかの言葉の読み方まで変わり、
#    取り次ぎのあとの「お待たせいたしました。」を 3.5 Flash Lite が10回中3回・Sonnet が10回中1回「ご本人」と読んだ（足す前は0回）＝別の方に繋ぐ向き
ASKS_YES, ASKS_NO = "尋ねている", "尋ねていない"
# ほかのことを尋ねている（名乗り・用件・お呼びした方のほか）。⛔ 2026-10-25 J10b（番号の出どころを聞かれる・S4・L4）：「この番号はどちらでお知りに
#    なりましたか？」に、AIが用件の録音を選び、プログラムはそのまま流した（それまでの記録で 46本中5本。AIへの決まりは「番号をどこで知ったかは用件では
#    答えません」）＝AIが選んだ答え方を確かめる所が無かった ⇒ 何を聞かれたかも読み、名乗り・用件の録音で答えようとしたときに当てる（Flow._asked_other）
ASKS_OTHER = "ほかのことを尋ねている"
ASKED = (ASKS_YES, ASKS_OTHER)  # どちらも「こちらに尋ねている」（質問に答える道へ）
# 取り次ぎで待ったあとに話した方が、待たせた方が戻ったのか、別の方に代わったのか（その方の最初の発話だけ、2つの判定へ出す＝確かめの名前 returned・
# returned2）。⛔ 2026-10-25 J9a：取り次ぎで待ったあとに話した方には、誰でもあいさつ・名乗りを流し直していた（流し直した10本のうち7本は「お待たせ
# いたしました。」で戻った方＝名乗りはもう聞いている。Intent.md 9-4 の決まりは「相手が替わったら短く名乗り直す」）。2つがそろって「戻った」と読んだ
# ときだけ、お呼びした方に繋がない道（かけ直し・お立場・後任の方・お呼びし直し）の名乗りを省く（Flow._new_ears）。G-68：別の方の言葉を2つそろって
# 「戻った」と読んだ 0/24・戻った方の言葉をそろって読めた 24/24。⚠️ どちらとも言えない「お待たせいたしました。」だけも、そろって「戻った」と読んだ
# ＝ご本人が名乗らずに出た恐れがあるので、繋ぐ道（_connect）と確かめる一言（_ask_target）の名乗りは省かない
RETURNED_SAME, RETURNED_OTHER = "待たせた方が戻った", "別の方が出た"
RETURN_MEANINGS = {RETURNED_SAME: "前の言葉で取り次ぎのためにこちらを待たせた方が、また電話に出ている（「お待たせいたしました」と戻って、お呼びした"
                                  "方のことを伝えた・自分が代わりにお話を聞くと言った など。別の方に代わったと分かる言葉が無い）",
                   RETURNED_OTHER: "前の言葉を言った方とは別の方が、電話に出ている（「お電話代わりました」と出た・前の方と違うお名前を名乗った・"
                                   "お呼びした方ご本人が名乗った など）"}
RETURN_CHECKS = ("returned", "returned2")
CONFIRM_PARTS = ("target_confirm", "target_reconfirm")  # 目的の人かを確かめる一言（名乗りの部分）：確かめ・はっきりしない返事への聞き返し
# ⛔ 2026-10-25 J10b（不在で別の方に代わる・S2）：「岡田は外出しておりますので、別の者に代わります。少々お待ちください。」（不在と取り次ぎの
#    2つの意味）を、目的の人の判定は2つとも「不在」と読み（1つしか選べない）、この問いも2つとも「尋ねていない」と読んだ（説明に「取り次ぐ・
#    不在を伝えた」と書いていた）＝どの読みも「取り次ぎを待つ」にならず、取り次ぎの最中にかけ直しの質問を流した。⇒ 不在の事実は目的の人の判定、
#    取り次ぎはこの問い（判定の仕組みがどの問いにも添える「取り次ぎを待つ」）が受け持つように、ここから取り次ぎを外した（G-59 で測ってから）
ASKS_MEANINGS = {ASKS_YES: "電話に出た方が、こちらの名乗り・用件・お呼びした方のことを尋ねている（どちら様か・ご用件か・どういうご関係か・"
                           "営業の電話か・どちらの〇〇か など）",
                 ASKS_OTHER: "電話に出た方が、名乗り・用件・お呼びした方のほかのことを、こちらに尋ねている（この番号をどこで知ったか・会社の"
                             "所在地・料金・会社のこと など）",
                 ASKS_NO: "こちらに何も尋ねていない（名乗った・不在を伝えた・「はい」「いいえ」と答えた・名前をくり返して確かめた など）。"
                          "ほかの方に代わる・取り次ぐので待つように言ったときは、不在を伝えていても、こちらではない"}
# ご本人の意味（判定の決まりの1か所。meaning_judge.KIND_DESC の「代わった人」は、ここに当たるときは選ばない）。
# ✅ 2026-10-19 ユーザー：「受付が『それでは岡田にお繋ぎします。』のような後に、『はい、お電話代わりました。』と言われれば、『ご本人』になりますよね」
#    ⛔ 直す前は、判定に、その前の取り次いだ方の言葉を渡しておらず、名乗らない「はい、お電話代わりました。」を「代わった人」と読んでいた（G-62）
# ⚠️ 2026-10-19 ユーザー（2回目）：「取り次いだ後に、『岡田は取り込んでおり、対応できません』ともう一度受付が言うこともあるので、取次後は
#    必ずしも、本人とは限らないので注意、見直しが必要」⇒ ご本人とするのは、取り次いだあとに「電話を代わった方」が出たときだけ。出られないと
#    伝える言葉は不在。取り次いだ方がまた出たのか、代わった方が出たのか分からない言葉は、ご本人と決めない（確かめる）
# ⛔ 2026-10-25 G-59（2つ目の判定を Jev と2つの Sonnet の組にしたとき）：お名前ではなく役目でお呼びしたあと（「後任のご担当者様はいらっしゃいますで
#    しょうか？」）の「後任の者に代わりますので、少々お待ちください。」⇒「はい、お電話代わりました。」と「後任は田中になります。…」⇒「お電話代わり
#    ました。田中です。」を、考えない Sonnet 5.5 は3回とも「代わった人が話し始めた」と読んだ（考える形は3回とも「ご本人」）＝下の「お呼びした方が
#    いないので別の者に代わる」を、役目の方への取り次ぎにも当てていた ⇒ 役目でお呼びしたときの、お呼びした方を書いた
#    ⇒ 2026-10-30 から、話している方は誰かの「ご本人」の説明（下の target_facets）
# ── 目的のお客様の判定の物差し（Question.facets。判定は1回の問い合わせで項目ごとに1つずつ答え、流れの制御は facts_reading の1か所で組み合わせて読む）。
# ✅ 2026-10-30 ユーザー：「第1層：『前と同じ方』『前と違う方』／第2層：『前と違う方』の中に、『目的のご本人』『取次ぎの方』『代理の方』、『前と同じ方』の
#    中に『受付など』『ご本人』／第3層：『代理の方』の中に、『後任』『上司』『ご本人の代わりをできる代理』『代わりのできない代理』に分かれますよね。これを
#    判断させないと、誤るのは当然です。」「前回言ったことと同じことを繰り返していますよ。」（前回＝10-29「『別の方』と『後任』は別の軸ですよね！『別の方』の
#    中に『後任』『上司』『同僚』などがいることになるでしょ！」）
# ⛔ 前の形は、話している方を「ご本人・別の方・決められない」（＋後任の方か）だけで選ばせ、言葉が言っている役目（受付の方が名前を確かめている・取り次ぐ・
#    代わりに伺う）を選ぶ所が無かった＝「岡田ですね。」を「決められない」と読むのは当然だった（G-59 の 10-30 未明：考えない Haiku は6回とも「名前を確かめて
#    繰り返しているだけで、本人かどうかは決められない」）。10-29 深夜は「後任の方か」だけを足した＝教えていただいた形の1枝だけを継ぎ足した誤り
# 項目：(1) 話している方は前に話した方と同じか（第1層）(2) 話している方は誰か（第2層・第3層。1人の方は1つの役目にしか当たらないので1つの選び。言葉から
#    言える段までを選ぶ＝「代理の方（どれかは分からない）」「別の方（どなたかは分からない）」は、その段までしか言えないとき）(3) 電話のやり取り（取り次ぐので
#    待つ）(4) いまお呼びしている方の事情（不在・在籍していない）。1つの言葉に同時に入りうる別々の事実は項目を分ける（✅ 10-29「『不在』と『別の方』と
#    いうのは、そもそも別の評価軸でしょ」）。「ご本人」は、どの場面でも、はじめにお呼びした方（後任の方をお呼びしているときも前任の方）
SPEAKER, WHO, PHONE, ABOUT = "speaker", "who", "phone", "target"   # 返事の項目の名前（見出しは target_facets）
SPEAKER_SAME, SPEAKER_NEW = "前と同じ方", "前と違う方"
WHO_RECEPTION, WHO_OTHER = "受付・取次ぎの方", "別の方（どなたかは分からない）"
WHO_SUCCESSOR, WHO_BOSS, WHO_CAN, WHO_CANNOT, WHO_PROXY = ("代理の方（後任）", "代理の方（上司）", "代理の方（代わりに決められる）",
                                                         "代理の方（代わりに決められない）", "代理の方（どれかは分からない）")
PROXIES = (WHO_SUCCESSOR, WHO_BOSS, WHO_CAN, WHO_CANNOT, WHO_PROXY)
WHO_CHOICES = (TARGET_IS_THERE, WHO_RECEPTION, *PROXIES, WHO_OTHER, UNDECIDED)   # 第2層・第3層（どの場面でも同じ選べるもの。説明だけが場面で違う）
PHONE_NONE, ABOUT_NONE, ABOUT_DEPARTED = "何もない", "言っていない", "在籍していない"
# 説明と見分け方が使う言葉（中身はここに1回だけ書く。説明の中では名前で使う＝⛔ 2026-10-30：「『お電話代わりました』のように出た」
# 「ほかの人のこととして話した」「取り次いだ（名前を確かめて待つように言った・お繋ぎします）」を何か所でも中身ごと書き、片方だけ直して食い違わせた）
# ⛔ 2026-10-30 G-59（作り直した文の1回目の測り）：「違うお名前」が何と違うのかを書いていなかったので、Luna は取り次ぎのあとの
#    「お電話代わりました。山本です。」を「違う名前を名乗ってはいない」とご本人と読んだ（3回とも）⇒ 何と違うのかを、言葉の決まりの1か所に書く
# ⛔ 同じ日（2回目の測り）：「ほかの人として話した」の中身に「名前をくり返して確かめた」を入れていた＝名前と中身が合わず、Luna は「岡田ですね。」を
#    「名前を確かめただけでは、ご本人か受付の方か決められません」と読んだ（3回とも）⇒ 名前の確かめは「名前を確かめた」の1つにする
CHANGED, OTHER_PERSON, NAME_CHECK, RELAY_WORDS, FIRST_ANSWERER, OTHER_NAME = (
    "「代わって出た」", "「ほかの人として話した」", "「名前を確かめた」", "「取り次ぎの言葉」", "「はじめに出た方」", "「違うお名前」")
# 目的のお客様の判定に見せる、その前に相手が言ったことの行の見出しと、無いときの中身（Flow.judge_context が渡すもの＝呼んでいる段の相手の言葉
# の最後の3つまで。電話に出たときの第一声は呼ぶ一言より前なので入らない＝本番で無いのは、呼ぶ一言のあとに相手が初めて話した言葉だけ）。
# 言葉の決まりの「その前に相手が言ったこと」（facet_terms）と合わせて、渡すものについて本当のことだけを書く。
# ⛔ 2026-10-30 G-59（3つの段の形の1回目）：前の言葉の行が無いと、考える Haiku は「前に話した方がいない」と読んで前と同じ方かを「決められない」
#    にし（前の言葉の無い言葉の 22件中16件）、「はい、岡田です。」の話している方まで「決められない」にした
# ⛔ 同じ日：はじめは「電話に出た方が、…初めて話した言葉です」と書いた＝話した方を決めつけていた。Luna は「『お電話代わりました』は電話に出た方の
#    最初の言葉なので、代わったとは扱いません」と読み、前の言葉の無い「お電話代わりました、営業部の岡田です。」を前と同じ方と読んだ
# ⛔ 同じ日の読み直し4回目：「無し」と、前の方の見分け方の「（電話に出た方の）その言葉は見せていない」が食い違って読めた（前の言葉は在るのか）。
#    「相手が初めて話した言葉です。電話に出たときの言葉は見せていません」と直すと、Luna は前の言葉の無い「お電話代わりました。岡田です。」
#    「お電話代わりました。山本です。」を3回とも前と同じ方と読んだ（いまの言葉が電話に出たときの言葉だと読める）⇒ 無いことだけを書く
BEFORE = "その前に相手が言ったこと"
NO_BEFORE = "無し（こちらがはじめにお呼びしたあとに、お客様の言葉より前に相手が言った言葉は、ありません）"

# 電話を終える言葉か（どちらの段の判定も、今の問い合わせの中の別の項目として読む＝お呼びする段は項目 END・答えの段は答えとは別の項目）。
# ✅ 2026-11-01 日本時間17時04分 ユーザー：「(a) OK、(b) OK」（決めるのは判定AIだけ、会話AIの報告は判定を待つきっかけだけ）。
#    16時56分 ユーザー：「会話AIは正しい判断ができるの？できないから、別のAIを使ったんじゃないのか？」＝⛔ それまで、終わりの理由（通話を断られた・
#    急いでいる・怒っている・取り次げなかった）と、理由を言わずに取り次げないかは、会話AIの報告だけで決まっていた。
#    答えと別の項目にするのは、10-29 ユーザー：「『不在』と『別の方』というのは、そもそも別の評価軸でしょ。…両方並立は相手の発言次第では当然あり得る
#    じゃないですか！」（「結構です。もうかけてこないでください。」＝答えは送らない・電話を終える言葉は断った、の2つが1つの言葉に入る）。
#    分け方の中身はご決定のまま：「忙しい」とだけ言われたら急いでいる（10-13。かけ直してと言われたときだけ、かけ直しの申し出）・営業のお断り
#    （「営業のお電話はお繋ぎできない決まりです」も）／事情があって取り次げなかった（担当が分からない・個人宛は繋がない）を分ける（10-22）・
#    「結構です」「大丈夫です」だけで通話の断りとしない（10-17）。流れの制御での決め方は Flow._end_judged の1か所。
END = "end"
END_NONE, END_REFUSED, END_HURRY, END_ANGRY = "言っていない", "通話・営業を断った", "急いでいる・いまは話せない", "怒っている・苦情"
END_REASONED, END_UNCLEAR = "事情を言って取り次げない", "理由を言わずに取り次げない"
END_WHO = "どの方か分からず取り次げない"   # ✅ 2026-11-01 日本時間23時59分 ユーザー（お名前だけでは分からない＝部署・フルネームでお呼びし直す＝Flow._who_unclear）
END_REASON = {END_REFUSED: "callee_refused_call", END_HURRY: "callee_hurry", END_ANGRY: "callee_complaint",
              END_REASONED: "not_transferred", END_UNCLEAR: "transfer_unclear", END_WHO: "who_unclear"}   # 電話を終える読み ⇒ 終わりの理由（END_LABEL の中の名前）
END_HEADING = "電話を終える言葉か"
UNSURE_END = "unsure_end"   # 中だけの印：判定が割れた・確かでない読みに電話を終える寄りがある（会話AIが報告していなくても _end_unsure へ）


def end_choices(calling: bool) -> dict:
    """電話を終える言葉かの選べるもの（{中の名前: 説明}。唯一の場所＝お呼びする段の項目 target_facets・答えの段の項目 meaning_judge.answer_items）。
    取り次げない2つは、お呼びする段だけ（答えの段は、お呼びした方と話している）。説明は意味で書き、試しの言葉と同じ言い方の例は書かない。
    答えの段の2つの書き分け（お呼びする段の文は変えていない）：
    ✅ 2026-11-01 日本時間20時11分 ユーザー：「2.「かけてこないでください」（折り返しの申し出への返事）：その依頼者の最終目的が、担当者からの
       折り返しなら、断った＝もうかけないと同じ意味。…登録とかが目的なら、営業を拒否したのではなく、担当者からの折り返しを拒否しただけ。」
       （11-02 4時16分の (3) も同じ向き）＝申し出を断る言葉は答え。もうかけないになるかは最終目的で流れの制御が決める（Flow._goals_lost）。
       ⛔ 11-02 まで「前の質問の申し出を、短い言葉で断った」と言葉の形で書き、「断った」の「もうかけないでほしい」と重なって、
       お電話の申し出への「かけてこないでください。」を断ったと読んでいた（G-53end の F|E1）⇒ 申し出がお電話のときも、と意味で書いた。
    ✅ 2026-11-01 日本時間20時30分（20時56分に同じお言葉）ユーザー：「念のためお急ぎではないということでよろしかったでしょうか？
       いいえ、かなり急いでおります。…これは通常この流れなら、電話を終える言葉ではない可能性が高いでしょ。質問が資料を送る時期について
       確認している場合など。…質問の精度が悪いので、これだけでは判断できない。」＝用件の急ぎは、ご自分の時間の話ではない
       （20時11分の「はい、急いでます」は急いでいる、のまま）。⛔ 11-02 まで書いておらず、用件の急ぎを急いでいると読んでいた（G-53end）。"""
    none = ("いまの言葉は、この電話を続けられない・続けたくない、とは言っていない（答えた・尋ねた・あとでかけ直してほしいと頼んだ・"
            + ("お呼びした方の居場所や様子を伝えた" if calling
               else "前の質問でこちらが申し出たこと（お送りする・ご案内する・お電話する など）を断った・要らないと言った"
                    "（申し出がお電話なら、そのお電話を断る言葉も）") + "、だけのときも）")
    hurry = ("急いでいる・忙しいので、この電話をいまは続けられないと言った（何時ならよい・かけ直してほしい、とまでは言っていない"
             + ("" if calling else "。ご自分の時間のことではなく、用件のほうを急いでほしい、と言っただけなら、こちらではない") + "）")
    out = {END_NONE: none,
           END_REFUSED: "この電話そのもの、または営業の電話を断った（もうかけないでほしい・営業の電話は受けない・そういう電話はお繋ぎしない決まり、と言った）",
           END_HURRY: hurry,
           END_ANGRY: "怒っている・苦情を言っている（この電話やこちらの話し方を責めた・強い言葉で不満を言った）"}
    if calling:
        out |= {END_REASONED: "営業のお断りではない事情を言って、取り次げないと言った（担当が分からない・個人あての電話は繋がない など。"
                              "お呼びした方がいない・手が離せない、だけなら、こちらではない）",
                END_WHO: "こちらが伝えたお名前などでは、お呼びした方がどの方か分からないので、取り次げないと言った（同じお名前の方が何人かいる など）",
                END_UNCLEAR: "お呼びした方へはお繋ぎできない、とだけ言い、営業のお断りなのか、ほかの事情なのかを言っていない"}
    return out | {UNDECIDED: "この言葉からは、この電話を終えたいのかどうか決められない（断りとも、ほかの意味とも取れる など）"}


HEAR, HEAR_HEADING = "hear", "こちらの声が届いていない様子か"
HEAR_LOST, HEAR_OK = "届いていない様子", "そういう様子は無い"


def hear_choices(calling: bool) -> dict:
    """こちらの声が届いていない様子かの選べるもの（{中の名前: 説明}）。✅ 2026-11-02 日本時間21時16分 ユーザー：「(ア) OK、お勧めの形で進めてください」
    ＝お客様にこちらの声が届いていない様子を、言葉の形で拾わず、判定AIのいまの問い合わせに項目を1つ足して読む（使い道は切れ方の
    callee_cannot_hear＝Flow.cut の1か所。CL-2 の連絡帳 4-166・10-30 の管理者のご決定：声が片方に届いていなかった証拠があれば営業拒否にしない）。
    説明は意味で書き、試しの言葉と同じ言い方の例は書かない。どちらの段でも同じ文（calling は SIDE_ITEMS の形をそろえるため）。"""
    return {HEAR_LOST: "いまの言葉で、こちらの声がお客様に届いていない・聞き取れていない様子を表した（こちらの声が届いているかを確かめるように"
                       "呼びかけた・声が届かない、聞き取れないと伝えた など）",
            HEAR_OK: "声が届かない・聞き取れないといった様子は無い（答えた・尋ねた・ほかのことを話した、だけのときも）",
            UNDECIDED: "この言葉からは、こちらの声が届いているかどうか決められない"}


WHY, WHY_HEADING = "why", "わからない・答えたくない理由"
WHY_NOT_SAID = "わからない・答えたくないとは言っていない"


def why_choices(calling: bool) -> dict:
    """「わからない」「答えたくない」と言ったときの理由の選べるもの（{中の名前: 説明}。中の名前は理由の名前＝DONT_KNOW_LABEL・REFUSED_LABEL の値）。
    答えの段だけ（お呼びする段は {}＝side_items が飛ばす＝お呼びする段の文は変わらない）。読みを組み合わせるのは Flow._why_of の1か所。
    ✅ 2026-11-06 日本時間15時20分30秒 ユーザー「(3)…お勧め：判定に理由も読ませます。OK」（⛔ それまでは会話AIの報告の理由だけで決めていた＝業務によっては
    この理由で残りの質問を伺わずに締める＝11-01「会話AIは正しい判断ができるの？」と食い違い。13時27分07秒のご質問の見直しで見つけた）。
    説明は意味で書き、試しの言葉と同じ言い方の例は書かない。"""
    if calling:
        return {}
    d, r = DONT_KNOW_LABEL, REFUSED_LABEL
    return {d["not_in_charge"]: "分からないのは、ご自分の担当ではない・ご自分が決める立場ではないからだと言った（分かる方がほかにいる）",
            d["undecided"]: "まだ決まっていない・まだ決めていないので分からない、と言った",
            d["unknown_now"]: "いまは分からない（手元に無い・すぐには思い出せない など。あとなら分かる）、と言った",
            d["unspecified"]: "分からない、とだけ言い、どうして分からないのかを言っていない",
            r["policy"]: "会社などの決まりで、言えない・教えられない、と言った",
            r["unwilling"]: "答えたくない・言いたくない、とだけ言い、決まりで言えないとは言っていない",
            WHY_NOT_SAID: "分からない・答えたくない、とは言っていない（答えた・ほかのことを話した、だけのときも）",
            UNDECIDED: "この言葉からは、どの理由か決められない"}


QUERY, QUERY_HEADING = "query", "こちらへ尋ねた問いがあるか"
QUERY_YES, QUERY_NO = "こちらへ尋ねた問いがある", "こちらへ尋ねた問いは無い"


def query_choices(calling: bool) -> dict:
    """お客様の言葉に、こちらへ尋ねた問いがあるかの選べるもの（{中の名前: 説明}）。答えの段だけ（お呼びする段は {}＝お呼びする段の「尋ねているか」は
    別の問い合わせ asks が受け持つ＝お呼びする段の文は変わらない）。読みを組み合わせるのは queries_us の1か所（答えの代わりに尋ねた＝asks_us・
    答えに添えた質問＝Flow._question_to_us）。✅ 2026-11-08 日本時間8時55分08秒 ユーザー「(イ) 判定に「こちらへ尋ねた問いがあるか」を1項目足し、
    1か所で読む」（⛔ それまでは言葉の形で2か所が別々に決めていた＝答えの代わりに尋ねたは「？」の文だけ・答えに添えた質問は語尾の「ですか」など。
    ご納得の手順への相づちの「…ですか。」を質問として会話AIに答えさせ、「。」で終わった本当の質問を聞き流した）。
    説明は意味で書き、試しの言葉と同じ言い方の例は書かない。"""
    if calling:
        return {}
    return {QUERY_YES: "いまの言葉の中で、こちらの答えを求めて尋ねた（こちらの考え・こちらのこと・業務やご案内のことなどを、教えてほしいと問いかけた）。"
                       "文の終わりの形（「？」があるか、「。」で終わるか）ではなく、こちらの答えを求めているかで決める",
            QUERY_NO: "こちらに尋ねていない（答えた・こちらにしてほしいことを頼んだ・申し出た・相づちや納得を言った・自分に言い聞かせた・"
                      "もう一度言ってほしいと頼んだ、だけのときも。頼み・申し出は、問いの形で言っても尋ねたとしない）",
            UNDECIDED: "この言葉からは、こちらに尋ねたのかどうか決められない"}


# 答えとは別に、判定がお客様の言葉ごとに読む項目の唯一の表（{項目の名前: (見出し, 選べるものを作る関数)}。並びは問い合わせの文の並び）。
# 読むのは、お呼びする段の項目（target_facets）・答えの段の項目（meaning_judge.answer_items）・判定の決まりの文・Jev の問い・読み取り（Reading の
# 同じ名前の値）。⛔ 2026-11-02 まで「電話を終える言葉か」だけを7か所に写して持っていた＝項目を足すと、写し漏れで片方の段だけ読まなくなる
# 項目は、その段で読むものだけ選べるものを返す（その段で読まない項目は {}＝わからない・答えたくない理由・こちらへ尋ねた問いがあるかは答えの段だけ）
SIDE_ITEMS = {END: (END_HEADING, end_choices), HEAR: (HEAR_HEADING, hear_choices), WHY: (WHY_HEADING, why_choices),
              QUERY: (QUERY_HEADING, query_choices)}


def side_items(calling: bool) -> dict:
    """その段で答えとは別に読む項目の選べるもの（{項目の名前: {中の名前: 説明}}。SIDE_ITEMS の1か所から。その段で読まない項目は入れない）。"""
    return {k: c for k, (_, fn) in SIDE_ITEMS.items() if (c := fn(calling))}


@dataclass(frozen=True)
class Facet:
    """目的のお客様の判定の項目の1つ（Question.facets）：返事の項目の名前・見出し・選べるもの（{中の名前: 説明}）・見分け方。
    選べるものの説明には、その答えの中身だけを書く。2つの答えの境目のうち、説明どうしが補い合う形で決まらないものだけを、見分け方に1回だけ書く
    （⛔ 2026-10-30：同じ境目を両側の説明に書き、片方だけを直して食い違わせた・ほかの説明の条件を一部だけ写して行き過ぎた＝帳面 0-2 の21回目の失敗。
    試験 test_target_facets_say_each_condition_once が、説明どうしの同じ言い回しを見つけたら落ちる）。"""
    key: str
    heading: str
    choices: dict
    notes: tuple = ()


def called_words(seeks_successor: bool) -> tuple[str, str]:
    """判定に見せる、いまお呼びしている方の呼び方（唯一の場所）：(いまお呼びしている方, お呼びした方でもいまお呼びしている方でもない)。
    ふつうの場面では、いまお呼びしている方＝お呼びした方なので「お呼びした方」とだけ書く（⛔ 2026-10-30 まで、どの場面にも「お呼びした方でも、
    いまお呼びしている方でもなく」と書き、後任の方の場面だけの言い分けを、ふつうの場面にも見せていた）。"""
    if seeks_successor:
        return "いまお呼びしている方", "お呼びした方でも、いまお呼びしている方でもない"
    return "お呼びした方", "お呼びした方ではない"


def facet_terms(asked: bool, seeks_successor: bool) -> tuple[str, ...]:
    """説明と見分け方と決まりが使う言葉の中身（Question.terms＝判定の文の「言葉の決まり」と、Jev の問いの指示が、ここだけから作る）。場面＝
    確かめる一言への返事か（asked）・後任の方をお呼びしているか（seeks_successor）で、その場面で本当のことだけを書く。⚠️ 例は、試しの言葉
    （G-59）と同じ言い方にしない（試しに合わせた文になると、測りが本番の力を表さない＝試験 test_target_facets_say_each_condition_once が確かめる）。
    ⛔ 2026-10-30 の読み直し4回目：決まりに「その前に相手が言ったことは、いまの質問より前に言った言葉（いまの質問への答えではない）」と、どの場面
    にも書いていた＝呼ぶ一言への言葉の場面では、その前の言葉は呼ぶ一言のあとに言った言葉で、本当ではなかった（確かめる一言への返事の場面
    のために足した一文＝考える Haiku が、確かめるきっかけの「はい。」を確かめた問いへの答えと取り違えた）⇒ 確かめる一言の場面だけに書く"""
    P, _ = called_words(seeks_successor)
    return ((f"「{BEFORE}」＝こちらがはじめにお呼びしたあとに、お客様の言葉より前に、相手が言った言葉（いまの方が言ったとは限らない"
             + ("。こちらが確かめる問いを言う前の言葉は、その問いへの答えではない" if asked else "") + "）",
             f"{CHANGED}＝いまの言葉で「お電話代わりました」のように、電話を代わって話し始めた（前に相手が話した言葉が無くても）",
             f"{OTHER_PERSON}＝{P}のことを、自分ではない人のこととして話した・尋ねた（「〇〇はただいま手が空いておりません」「〇〇はどの部署の"
             "者でしょうか」など）",
             f"{NAME_CHECK}＝{P}の名前をくり返して確かめた（「〇〇でございますね」のように。ご本人は、自分の名前をこのようには確かめない）",
             f"{RELAY_WORDS}＝その前の言葉で、取り次いだ方が{P}に取り次ぐと言った（{NAME_CHECK}うえで待つように言った・その方につなぐと言った）",
             f"{FIRST_ANSWERER}＝はじめに電話に出て、{CHANGED}とは言っていない方",
             # ⛔ 2回目の測り：「{P}のお名前（その前の言葉で{P}だと言われたお名前も）ではないお名前」と書き、Luna は「山本です」を「その前の言葉で
             #    岡田様だと言われたお名前を名乗ったものではなく、違うお名前に当たりません」と読んだ（途中の括弧で文が切れた）
             (f"{OTHER_NAME}＝その前の言葉で{P}だと言われたお名前とは違うお名前" if seeks_successor else
              f"{OTHER_NAME}＝お呼びした方のお名前とは違うお名前（お呼びした方のお名前＝質問の言葉のお名前か、その前の言葉でお呼びした方だと"
              "言われたお名前）"))
            + (("「お呼びした方」「ご本人」＝はじめにお呼びした前任の方。「いまお呼びしている方」＝いまの質問でお呼びしている後任のご担当者様",)
               if seeks_successor else ()))


def target_facets(asked: bool = False, seeks_successor: bool = False) -> tuple[Facet, ...]:
    """目的のお客様の判定の物差し（Question.facets を作る唯一の場所。Flow._calling_questions がここから作る）：場面＝呼ぶ一言への言葉か、確かめる
    一言への返事か（asked）・後任の方をお呼びしているか（seeks_successor）から、項目と選べるものと見分け方を作る。後任の方の場面だけの文は、
    その場面のときだけ入れる（⛔ 2026-10-30 G-59：後任の方の場面のために「在籍していない」に足した一文を、考えない Sonnet が、ふつうの場面の
    「いえ、岡田は退職しまして、私が後任の田中です。」にも当てた＝退職を伝えてくださった方へのお詫びを流さない）。選べるものの名前は場面で変えない。
    説明の中の「」の言葉は facet_terms の言葉。"""
    P, NOT_P = called_words(seeks_successor)
    s = seeks_successor
    speaker = Facet(SPEAKER, "話している方は、前に話した方と同じか", {
        SPEAKER_SAME: f"いまの方は{CHANGED}のではなく、前の方と別の人だとも分からない（前の方がそのまま話しているときも、取り次いだ方が戻って"
                      "話しているかもしれないときも）",
        SPEAKER_NEW: f"いまの方が{CHANGED}・前の方と別の人だと分かる"}, (
        "前の方＝その前に相手が言ったことの最後の言葉を話した方。前に相手が話した言葉が無いときは、電話に出た方（電話に出たときの言葉は"
        "見せていない）",
        # ⛔ 2回目の測り：「別の人だと分かるのは、…ときと、…とき」と、ほかのときを閉じずに書き、Luna は後任の方の場面の「はい、私が後任の田中です。」
        #    を「前の方（岡田は先月退職いたしました、と言った方）とは別の人」と読んだ（3回とも）
        f"前の方と別の人だと分かるのは、次のときだけ：前の方が{OTHER_PERSON}か{NAME_CHECK}{P}を、いまの方が自分だと名乗った・答えた／前の方が"
        "自分のお名前を名乗っていて、いまの方がそれとは別のお名前を名乗った",
        "答えが変わっただけ（前に「はい」と言った方が、いまの質問に「いいえ」と答えた など）・取り次ぐと言われたことだけでは、前と違う方にしない",
        f"その前の言葉の中で{CHANGED}のは前の方で、いまの方のしるしではない"))
    if s:   # はじめにお呼びした方は在籍していない（後任の方をお呼びしている）
        himself = "はじめにお呼びした方ご本人（自分でその方の名前を名乗った）"
    elif asked:
        himself = "お呼びした方ご本人（確かめた問いに「はい」「そうです」「私です」と答えた・自分でお呼びした方の名前を名乗った）"
    else:
        himself = (f"お呼びした方ご本人（自分でお呼びした方の名前を名乗った・「私です」と答えた・取り次がれて{CHANGED}方は見分け方のとおり。お名前"
                   "ではなく役目（「部長様」など）だけでお呼びしたときは、その役目の方（取り次いだ方がその役目の方だと言った方も）。"
                   f"{RELAY_WORDS}のあとでも、{OTHER_PERSON}うえで出られないと伝えた方は、こちらではない）")
    reception = (f"{NOT_P}、電話を受けた方・取り次ぐ方（{FIRST_ANSWERER}が、確かめた問いに「いいえ」「違います」と答えた・"
                 f"{'' if s else OTHER_NAME + 'を名乗った・'}{OTHER_PERSON}・{NAME_CHECK}・受付の者だと言った・ほかの方に代わる・待つように言った）"
                 if asked else
                 f"{NOT_P}、電話を受けた方・取り次ぐ方（{FIRST_ANSWERER}で、{OTHER_PERSON}・{NAME_CHECK}・{'' if s else OTHER_NAME + 'を名乗った・'}待つように"
                 f"言った・ほかの方に代わると言った・受付の者だと言った。{CHANGED}方で、さらにほかの方へ取り次ぐと言った）")
    successor = ("代理の方のうち、退職・異動などでもう在籍していないお呼びした方の仕事を引き継いだ後任の方（自分で後任だと言った・退職や異動の"
                 "あとは自分が担当していると言った・その前の言葉で後任の方だと言われたお名前を名乗った"
                 + ("・後任のご担当者様かを確かめた問いに「はい」「そうです」「私です」と答えた" if s and asked else "")
                 + "。その日だけ代わりに受け持つ方は、こちらではない）")
    # ⛔ 2026-10-30 朝（5回目・6回目の読み直しのあと）：読み直しで挙がった理論上の穴（71件の試しにはほとんど無い言葉）を、ここと言葉の決まりに
    #    足していった（後任の方の場面の「ご本人」の言い分け・「はじめに出た方」の決め方・受付の方の説明を場面で1つの形・取り次がれて出た方の除く
    #    しるし・名前の確かめに尋ね返し・「打ち消した」）。考えない Sonnet の、流れの制御の動きが変わる外れは、足すたびに 0→1→2→3→3 と増えた
    #    （取り次ぎのあとの「お待たせいたしました。」は、この形で3回とも合い、足した形で3回とも外れた）⇒ 測った中でいちばん良かったこの形に戻した。
    #    挙がった穴と測った数は PHASE0 G-59（「まだ入れていない穴」）。足すときは、1つずつ足して、本番と同じ送り方で前と比べてから
    who = Facet(WHO, "話している方は誰か", {
        TARGET_IS_THERE: himself,
        WHO_RECEPTION: reception,
        WHO_SUCCESSOR: successor,
        WHO_BOSS: f"代理の方のうち、{'お呼びした方か、いまお呼びしている方' if s else 'お呼びした方'}の上司の方（自分で上司だと言った）",
        WHO_CAN: f"代理の方のうち、この件を{P}の代わりに決められる・判断できると言った方",
        WHO_CANNOT: f"代理の方のうち、{P}の代わりに出たが、この件は決められない・伝言や取り次ぎだけだと言った方",
        WHO_PROXY: f"{P}の代わりの方（代わりに出たと言った・代わりに用件を伺うと言った・その日は代わりに受け持つと言った）で、{'' if s else '後任・'}"
                   "上司か、この件を決められるかを、自分でも、その前の言葉でも言っていない",
        WHO_OTHER: f"{CHANGED}方{'（確かめへの返事では、その前の言葉で' + CHANGED + '方も）' if asked else ''}で、{NOT_P}と分かるが、取り次ぐ方か"
                   "代理の方かは分からない",
        UNDECIDED: f"{P}かどうか、この言葉からは決められない（"
                   + ("はいともいいえとも言っていない・こちらに聞き返しただけ）" if asked else
                      f"「はい」だけ・会社の名前だけ・「お待たせいたしました」だけ・{RELAY_WORDS}の無いまま{CHANGED}だけ・名前を出さずに、こちらの"
                      "名乗りや用件などを尋ねただけ）")}, (
        f"受付・取次ぎの方と別の方：取り次ぐと言われたあとに、{CHANGED}のではなく{P}の事情だけを伝えた方は、取り次いだ方が戻ったのか、別の方が"
        f"伝えたのか分からなくても、受付・取次ぎの方。{CHANGED}方が、{OTHER_NAME}を名乗っただけ・{P}の事情を伝えただけなら、別の方",
        # ⛔ 2026-10-30 G-59（作り直した文の1回目の測り）：作り直したときに、受付の方と決められないの境目（前は両側の説明に書いていた）を落とし、
        #    Luna は「岡田ですね。」を決められない・「どういったご用件でしょうか？」「お待たせいたしました。」を受付の方と読んだ（3回とも）
        f"受付・取次ぎの方と{UNDECIDED}：{P}のことを{OTHER_PERSON}方・{NAME_CHECK}方は、受付・取次ぎの方。「{UNDECIDED}」の説明の言葉だけの方は、{P}ご本人も"
        f"言う言葉なので、取り次ぐと言われたあとでも、前と同じ方でも、「{UNDECIDED}」",
        # ⛔ 2026-10-30 の読み直し：作り直したときに、受付の方の説明の「代理の方だとは言っていない」を落とし、「私が代わりにご用件をお伺いします」
        #    「今は私が担当しております」が受付の方と代理の方の両方に当たった。⛔ 読み直し4回目：ここに代理の方の言葉を単語で並べたので、受付の方が
        #    後任の方のことを話しただけの言葉も代理の方と読め（単語を探す読み方）、「無ければ」が代わって出た方（別の方）にも掛かって読めた
        #    ＝並べずに説明を指し、両方の説明に当たる方だけに限る。⛔ 作り直した文の1回目の測り：「言っていなければ受付・取次ぎの方」を、Luna は
        #    受付の方を選ぶ決まりとして読み、「どういったご用件でしょうか？」を「代理の方だという説明もない」から受付の方と読んだ ⇒ 選ぶ側は書かない
        "受付・取次ぎの方と代理の方：受付・取次ぎの方の説明にも当たる方は、代理の方の説明のどれかを自分のこととして言ったときだけ、代理の方"
        "（ほかの人が後任の方・上司だと話しただけは、自分のことではない）",
        # 取り次がれて出た方が誰かは、取り次いだ方が誰に取り次ぐ・代わると言ったかで決まる（1つの決まり。⛔ 2026-10-30 まで、ご本人・後任・上司・
        # どれかは分からないの4つの説明に分けて書き、「後任の者に代わります」のあとの方を、Luna が代理の方（どれかは分からない）とも読んだ）
        f"取り次がれて{CHANGED}方は、ほかの説明と見分け方より先に、取り次ぐと言った方が誰に代わると言ったかで決める：{RELAY_WORDS}⇒"
        f"{WHO_SUCCESSOR if s else TARGET_IS_THERE}"
        f"（{OTHER_NAME}を名乗った方は除く）／後任の方に代わる⇒" + WHO_SUCCESSOR + "／上司に代わる⇒" + WHO_BOSS + f"／{P}がいないので、ほかの者に代わる⇒"
        + WHO_PROXY + ("" if s else "／役目だけでお呼びしたときの、その役目の方に代わる⇒" + TARGET_IS_THERE),
        f"{OTHER_NAME}を名乗った方は、{P}ではない",
        # ⛔ 2026-10-30 G-59（作り直した文の1回目の測り）：「説明と見分け方のどれでも決まらないときだけ」と書き、決められないの説明に当たる言葉
        #    （「どういったご用件でしょうか？」「お待たせいたしました。」）でも、Luna は決められないを避けた
        f"「{UNDECIDED}」は、その説明に当たるときと、説明と見分け方のどれでも決まらないとき・2つ以上に当たるときに選ぶ"))
    phone = Facet(PHONE, "電話のやり取り", {
        # ⛔ 2026-10-30 G-59（作り直した文の1回目の測り）：説明から「いまの言葉で」を落とし（決まりにだけ書いた）、Luna は取り次ぎのあとの
        #    言葉を、その前の言葉の「少々お待ちください。」で「取り次ぎを待つ」と読んだ（5回）＝選べるものが、どの言葉のことかを説明に書く
        PHONE_NONE: "いまの言葉では、待つようには言っていない（電話を代わる・取り次ぐ・確かめるので待つように、とは言っていない）",
        HOLD: "いまの言葉で、待つように言った（「少々お待ちください」だけのときも・ほかの人に電話を代わる・取り次ぐので待つように言ったときも。お呼びした方に"
              "取り次ぐときも、ほかの者に代わるときも）"})
    about = Facet(ABOUT, f"{P}の事情", {
        ABOUT_NONE: f"いまの言葉では、{P}がいらっしゃるか・電話に出られるかを、何も言っていない（取り次ぐ・代わるので待つように、と言っただけのときも）",
        TARGET_IS_AWAY: f"いまの言葉で、{P}は、いまはいらっしゃらない・電話に出られない・いまはお話しできない、と伝えた（話している方がほかの方でも・取り次いだ方が"
                        f"また出てそう伝えたときも・{P}ご本人が、いまは手が離せないと言ったときも）",
        ABOUT_DEPARTED: ("いまの言葉で、後任の方はいない・後任の方ももういない、と伝えた（はじめにお呼びした方が在籍していないことは、もう分かっているので、"
                         "こちらにしない）" if s else
                         "いまの言葉で、お呼びした方は、退職・異動などで、もうその会社・部署にいない（担当が替わったときも）・そういう方はいない、と伝えた")}, (
        f"その日だけほかの方が代わりに受け持つときは、{ABOUT_DEPARTED}ではなく{TARGET_IS_AWAY}",))
    side = tuple(Facet(k, SIDE_ITEMS[k][0], c) for k, c in side_items(True).items())   # 電話を終える言葉か・こちらの声が届いていない様子か（お呼びする段で読む SIDE_ITEMS）
    return (speaker, who, phone, about, *side)


def facts_reading(facts: dict, seeks_successor: bool = False) -> tuple[str, bool, bool, bool]:
    """項目ごとの答え（facts＝{項目の名前: 中の名前}）⇒ (読み, 不在の印, 電話を代わった方の印, 後任の方の印)（唯一の決まり）。
    いまお呼びしている方（ふつうはお呼びした方ご本人・後任の方をお呼びしているとき＝seeks_successor は「代理の方（後任）」）なら「ご本人」の読み、
    決められない（後任の方をお呼びしているときの、はじめにお呼びした方ご本人も＝確かめる）なら「決められない」、ほかの方は「別の方」。
    在籍していないも、組み合わせた読みは不在（応じ方の違い＝後任の方・締め・かけ直しは、AIの理由で決める）。はじめの場面の後任の方の印は、
    話している方が「代理の方（後任）」のとき（Flow._successor_read が、AIの「後任の方」の報告とそろえる）。"""
    who = facts[WHO]
    sought = WHO_SUCCESSOR if seeks_successor else TARGET_IS_THERE
    # 後任の方をお呼びしているときの「代理の方（どれかは分からない）」は、後任の方かもしれない＝確かめる側（⛔「公認は田中に…」のあとの田中様を
    # 別の方とすると、AIの「代わった方」とそろって、確かめずに田中様へ後任の方をお呼びし直す＝J12b）
    unsure = (UNDECIDED, TARGET_IS_THERE, WHO_PROXY) if seeks_successor else (UNDECIDED,)
    person = TARGET_IS_THERE if who == sought else UNDECIDED if who in unsure else TARGET_IS_NOT
    new = facts[SPEAKER] == SPEAKER_NEW
    about = TARGET_IS_AWAY if facts[ABOUT] in (TARGET_IS_AWAY, ABOUT_DEPARTED) else ABOUT_NONE
    value, away, _ = target_reading(person, HOLD if facts[PHONE] == HOLD else HANDOVER if new else PHONE_NONE, about)
    return value, away, new, not seeks_successor and who == WHO_SUCCESSOR


def target_reading(who: str, phone: str, about: str) -> tuple[str, bool, bool]:
    """目的のお客様の判定の3つの事実 ⇒ 流れの制御の読み（唯一の組み方。facts_reading が項目ごとの答えから3つにして渡す）：(読み, 出られない・
    いまは話せないと伝えたか, 電話を代わった方か)。who＝いまお呼びしている方か（ご本人・別の方・決められない）・phone＝取り次ぎを待つ／電話を
    代わった方（HANDOVER）／何もない・about＝不在／言っていない。
    読みは、流れの制御が先に見る順（Flow._calling_reported）：取り次ぎを待つ（黙って待つ）＞ 不在（ご本人ではない方が、お呼びした方は出られないと
    伝えた＝お呼びし直さずに、戻りの時刻などを伺う）＞ 電話に出ている方（電話を代わった方で、別の方なら「代わった人が話し始めた」＝確かめずに
    お呼びし直す側。決められないなら「決められない」のまま＝確かめる側）。
    ご本人が「いまは話せない」と言ったときは、読みはご本人のまま（理由はAIが聞いた言葉で選ぶ＝_calling_reported の (5)。不在の読みにすると、
    AIが理由を「不在」と選んだときに、ご本人へ「何時ごろお戻りに…」を伺う）。
    読みに出ない事実は印で残す：不在＝取り次ぎを待つ言葉でも（次に代わって出る方は、お呼びした方の代わりの方＝_handed＝says_away）／ご本人が
    いまは話せない（繋がない＝says_busy）／電話を代わった方＝不在の言葉・ご本人・決められないでも（まだ名乗りを聞いていない方＝_new_speaker＝says_new）。
    ⛔ 2026-10-29 まで、電話を代わった方で「決められない」も「代わった人が話し始めた」にしていた（決められないことと、別の方だと分かったことを1つに
       混ぜていた）：後任の方をお呼びしたあと、「公認は田中に…」（聞き取りの誤り）のあとの「お電話代わりました。田中です。」を判定が「決められない」と
       読むと、AIの「代わった方」とそろって、確かめずに田中様へ後任の方をお呼びし直す（J12b と同じ外れ）。✅ 2026-10-19 ユーザー：取り次いだ方が
       また出たのか代わった方が出たのか分からない言葉は確かめる"""
    away, new = about == TARGET_IS_AWAY, phone == HANDOVER
    value = (HOLD if phone == HOLD else TARGET_IS_AWAY if away and who != TARGET_IS_THERE
             else HANDOVER if new and who == TARGET_IS_NOT else who)
    return value, away, new


def says_away(value) -> bool:
    """判定の読みが「お呼びした方は出られない」を含むか（読みそのものが不在か、ご本人ではない方の読みの不在の印＝target_reading）。"""
    return value == TARGET_IS_AWAY or (bool(getattr(value, "away", False)) and value != TARGET_IS_THERE)


def says_busy(value) -> bool:
    """判定の読みが「電話に出ているご本人が、いまは話せない」か（ご本人の読みに、不在の印＝target_reading）。"""
    return value == TARGET_IS_THERE and bool(getattr(value, "away", False))


def says_new(value) -> bool:
    """判定の読みが「電話を代わった方が話し始めた」を含むか（読みそのものか、電話を代わった方の印＝target_reading）。"""
    return value == HANDOVER or bool(getattr(value, "new", False))


def says_successor(value) -> bool:
    """判定の読みが、はじめの場面で「話している方は代理の方（後任）」を含むか（後任の方の印＝facts_reading。後任の方をお呼びしたあとは、読みそのもの）。"""
    return bool(getattr(value, "successor", False))


def says_proxy(value) -> bool:
    """判定の読みが「話している方は代理の方」（後任・上司・代わりに決められる・決められない・どれかは分からない＝PROXIES）か（項目ごとの答え
    ＝facts の「話している方は誰か」。項目ごとに答えない読みは、代理の方とは読んでいない）。Flow._proxy_read が、AIの「代わりの方」「後任の方」の
    報告とそろうかを見る。"""
    return (getattr(value, "facts", None) or {}).get(WHO) in PROXIES
# hang_up＝話さずに切る（電話接続側が読む）・silent＝AIに話させない返事（取り次ぎを待つ間など。handle_call）
INTERNAL_KEYS = {"ack", "ack_pause_s", "ask_n", "ask_reason", "ask_last", "after", "play", "speak", "hang_up", "silent", "sorry", "again", "away", "renamed",
                 "led", "thanks", "talked", "fallback", "quiet", "bridged", "hold_wait_s", "ahead_offer", "ahead_closed", "form_reason"}  # 中で使うだけの項目（電話接続側・数え方が読む）。AIへの返事には入れない
# こちらが使う名前（AIに見せる日本語の名前）。値の欄に入っていたら、相手の答えではない（form_problem）
_LABELS = (set(SYSTEM_LABEL.values()) | set(CONFIRM_LABEL.values()) | set(END_LABEL.values()) | set(PARTY_LABEL.values())
           | set(CALLBACK_LABEL.values()) | set(OFFER_LABEL.values()) | {"答えた"})
_FROM_LABEL = {"condition_id": {**{v: k for k, v in SYSTEM_LABEL.items()}, ANY_LABEL: ANY, NONE_LABEL: NONE}, "result": {v: k for k, v in CONFIRM_LABEL.items()},
               "reason": {v: k for k, v in END_LABEL.items()}, "part": {v: k for k, v in PART_LABEL.items()},
               "party": {v: k for k, v in PARTY_LABEL.items()}, "why": {v: k for k, v in CALLBACK_LABEL.items()},
               "offer": {v: k for k, v in OFFER_LABEL.items()}, "fallback": {v: k for k, v in {**TALK_LABEL, **POINT_KIND_LABEL}.items()},
               "until_why": {v: k for k, v in UNTIL_LABEL.items()}, "cycle": {v: k for k, v in CYCLE_LABEL.items()},
               "unknown_why": {v: k for k, v in UNKNOWN_WHY_LABEL.items()}}
_ARG_FROM_LABEL = {v: k for k, v in ARG_LABEL.items()}


def unsure_counts(kind: str, value: str, answer: bool = True) -> str | None:
    """目的の人を呼んでいる段で、確からしさが線より下の読み（value。代わりの Jev）を、どう数えるか（唯一の決まり＝Flow.on_unsure・G-59 の数え方）。
    answer＝答える側（尋ねている＝聞かれたことに録音で答える）にも数えてよい読みか。False は止める側だけの読み（組の答えが待つ長さに間に合わない
    ときの、裏で出した代わり＝convo.check_handler の hold。✅ 2026-10-30 ユーザー：「(1)(2)ともお勧めの形でOK」＝黙って待つ・確かめる側だけ）。
    ⛔ 2026-10-30 夜：裏の代わりも答える側に数えていた＝3.5 Flash Lite の「尋ねている」で、聞かれていない質問に答える側へ進みえた。弱い
       「尋ねている」を数えてよい根拠は、確からしさを返す判定の測りだけ（Jev・d1-ultra は、質問でない言葉に弱い「尋ねている」0回＝下・PHASE0
       G-59）。Flash Lite は、d1-ultra と Jev が2つとも確かでない言葉（裏で出す言葉）で、確かめへの「いいえ、私は山田と申します。」を3回中1回
       「尋ねている」と読み、質問の言葉では裏で出ることが無かった（質問は27回とも d1-ultra か Jev が確かに読む）＝答える側に数えると、救う0・誤る1
    弱い読みは、繋ぐのを止める側にだけ使う：目的の人の判定で「ご本人」でなければ「決められない」＝繋ぐのを止める印（Flow._doubted。判定の
    読みとは別に持つ＝ほかの判定と食い違いを作らない）／尋ねているかの問い合わせで「尋ねている」（目的の人かを決めずに、聞かれたことに
    答える側）。ほかは数えない（None）。
    ⛔ 2026-10-22 G-59：Jev は質問の言葉の「尋ねている」を、確からしさ 0.79〜0.92 で15回中9回返した（線より下で捨てると、質問に答えずに
       目的の人かを確かめる。質問ではない言葉に、弱い「尋ねている」は0回）
    目的の人の判定の弱い「取り次ぎを待つ」は、取り次ぎの読みとして数える（黙って待つ＝相手がまた話せば決め直せる側）。尋ねているかの問い合わせの
    弱い「取り次ぎを待つ」は数えない。⛔ 2026-10-22 G-57 tc27（不在で別の方に代わる・L4）：「岡田は外出しておりますので、別の者に代わります。
    少々お待ちください。」を、3.5 Flash Lite は止まっていて、Sonnet は「不在」だけ、Jev は弱く「取り次ぎを待つ」（0.59〜0.6）と読んだ＝捨てたので、
    取り次ぎの最中にかけ直しの質問を流した。G-59 の Jev の5回分：目的の人の判定の弱い「取り次ぎを待つ」は、取り次ぎの言葉に48回・ほかの言葉に
    0回／尋ねているかの問い合わせでは、ほかの言葉（「お待たせいたしました。」）に6回（0.37〜0.49）"""
    if kind in JUDGMENTS:
        return HOLD if value == HOLD else UNDECIDED if value != TARGET_IS_THERE else None
    if kind in ASKS_CHECKS and answer:
        return value if value in ASKED else None
    return None


def label(cond: str | None) -> str | None:
    """選択肢の中の名前 → AIに見せる名前・依頼主へ渡す名前（業務の選択肢はそのまま）。"""
    return ANY_LABEL if cond == ANY else NONE_LABEL if cond == NONE else SYSTEM_LABEL.get(cond) or CONFIRM_LABEL.get(cond, cond)


def condition(name: str) -> str:
    """選択肢の日本語の名前 → 中の名前（label の逆・AIの報告を読む所と同じ表 _FROM_LABEL。業務の選択肢はそのまま）。CL-2 の業務設定の
    伺う答え（ask_if の答え）は日本語の名前（「わからない」「どれも要らない」など）で来る＝会話へ渡すところ（フェーズ1のつなぎ）で、
    この1か所で置き換える（CL-2 の連絡帳 4-185 の (b)）。"""
    return _FROM_LABEL["condition_id"].get(name, name)
# AIが呼べる操作（6-2）と、その項目（中の名前）：(必ず要る項目, 無くてもよい項目)。操作の宣言（tool_declarations）と、
# 受け取ったときの確かめ（Flow._check_args）は、この表だけを使う（宣言と確かめが食い違わないように）
OP_ARGS = {"record_answer": (("question_id", "condition_id"), ("value", "until_why", "cycle", "unknown_why", "rest", "terms", "topic", "way")),
           "report_unclear": (("question_id",), ()),
           "confirm_field": (("question_id", "result"), ("correction", "unknown_why", "terms", "topic", "way")),
           "request_end": (("reason",), ()),
           "replay_opening": (("part",), ("answer", "fallback", "terms", "added", "topic", "way", "rest")),
           "report_party": (("party",), ()),
           "request_callback": (("why",), ("offer", "rest"))}
# ✅ 2026-10-14 ユーザー：考えている相手への「はい」は形 A（AIが自分で言う）。⇒ 比べた形 B（AIは黙って「相手が考えている」と報告し、
#    相づちはプログラムが録音で流す。G-36）の操作は外した（B は、答えを考えている途中と読み違えて黙った回があった）
OPS = tuple(OP_ARGS)
# 会話AIが、お客様の言葉を、いまの質問・言い直しへの答えとして報告する操作（答えに添えた質問を見る＝Flow._turn_done の took）
ANSWER_OPS = ("record_answer", "confirm_field")
# 報告に添える「ほかに言われたこと」（rest＝Flow._take_rest）の説明（操作の宣言の1か所。答えの報告とかけ直しの申し出に同じ文）
REST_DESC = ("この報告の答え・申し出のほかに、相手が同じ言葉の中で言った具体的なこと（日時・時刻・人数・お名前・電話番号・メールアドレス・ご住所・"
             "LINE の ID・ご連絡の方法など）を、相手が言ったとおりに書きます。無ければ書きません（こちらから次に伺うことの答えなら、伺わずに使います）")
# 質問へ戻る操作（replay_opening の resume）に添える「ほかに言われたこと」：先に言われたことに次の質問の答えは無かったが、まだ使っていないこと（_take_rest）
REST_KEEP_DESC = ("ほかに言われたことを伺うよう頼まれて、次の質問の答えが入っていなかったときだけ：ほかに言われたことのうち、このあと伺うことの答えに"
                  "なりそうなこと（電話番号など）を、相手が言ったとおりに書きます。無ければ書きません")
# 選択肢が決まっている項目（中の名前 → その表）。答えの種類（condition_id）は質問ごとに違うので、record_answer の中で確かめる
_ENUM_ARGS = {"result": CONFIRM_LABEL, "reason": END_LABEL, "part": PART_LABEL, "party": PARTY_LABEL, "why": CALLBACK_LABEL, "offer": OFFER_LABEL,
              "fallback": {**TALK_LABEL, **POINT_KIND_LABEL}, "until_why": UNTIL_LABEL, "cycle": CYCLE_LABEL, "unknown_why": UNKNOWN_WHY_LABEL}


def check_policies(policies: dict, message: str | None = None) -> None:
    """相手の申し出への応じ方（業務設定）の確かめ（唯一の場所。instruction.Business と Flow の両方が呼ぶ）。伝言を残す業務は、伝言の文が要る。"""
    for k, v in policies.items():
        if k in POLICIES and v not in POLICIES[k]:
            raise ValueError(f"相手の申し出への応じ方「{k}」に「{v}」は選べません（選べるのは {'・'.join(POLICIES[k])}）")
    if policies.get("message") == "leave" and not message:
        raise ValueError("伝言を残す業務（message_policy＝leave）には、伝言の文（message）が要ります")


def offers_rule(target: bool = True) -> str:
    """相手の申し出・お呼びした方の事情の報告の仕方（AIに見せる文の唯一の場所：指示文 instruction.system_rules・target_rule と、流れの制御の
    知らせ Flow._asking_note・_away_text・_report_hint）。✅ 2026-10-22 ユーザー：代わりの方・伝言・折り返し・在籍していない・ほかの手段・
    長期の不在。何と言うか（お礼・お詫び・聞くこと）は、プログラムが決まった録音で流す（AIは報告するだけ）。target：目的の人のある業務か
    （代わりの方・後任の方・在籍していない は、目的の人のある業務だけ）。"""
    C, O, A = CALLBACK_LABEL, OFFER_LABEL, ARG_LABEL
    # ⛔ 2026-10-25 日本時間の早朝（J9a）：目的の人の決まり（instruction.target_rule）と確かめを待つ間の知らせ（Flow._now）が、この分け方を
    #    例つきで書き直していて食い違っていた（「本日は休みです」＝不在 ⇒ その日は戻らない方に「何時ごろお戻りに…」・「出られない」＝不在 ⇒
    #    席にいて取り込んでいる方に「お戻り」を聞く）。同じ言葉でも回によって AIの選ぶ理由が割れた（取り込んでいて出られない：6本中3本ずつ）
    #    ⇒ 分け方はここだけに書き、ほかは「聞いた言葉に合う理由で」とだけ書く
    whys = [f"いらっしゃらない（席を外している・外出しているなど。その日のうちに戻る・戻るかが分からない）＝「{C['absent']}」",
            f"お休み（本日は休み・休暇・休職など。「今週いっぱい休み」も）＝「{C['day_off']}」",
            f"お休みではなく、出張などで、その日のうちには戻らない（「来週まで出張」も。休みとは言われず「今週いっぱいおりません」"
            f"だけのときも）＝「{C['long_absent']}」",
            f"いらっしゃるが、いまは話せない（取り込んでいる・手が離せないなど）・あとでかけてほしい＝「{C['later']}」",
            f"別の電話番号にかけてほしい＝「{C['other_number']}」"]
    if target:
        whys.append(f"退職・異動などでもういらっしゃらない＝「{C['departed']}」")
        # ✅ 2026-11-01 日本時間5時48分 ユーザー：「「はい」や事情を言われた → 理由のある取り次げない：かけ直しの申し出+取次ぎ可能な日時の確認の方がいいんじゃないですか？営業はお断りと言われた → 営業のお断り 戻る時刻を言われた → かけ直しの申し出」「確かめのあとの返事でもはっきりしないときは…：返事による」
        whys.append(f"理由を言わずに取り次げないと言われて、プログラムがお呼びした方のご事情かを伺ったあと、はい・ご事情を答えられた"
                    f"＝「{C['unavailable']}」（戻る時刻を言われたら「{C['absent']}」。伺ったあとだけ使います）")
    whys.append(f"メール・ファクス・郵送など、電話ではない手段で送ってほしい＝「{C['other_means']}」（メール・ファクス・郵送と言われたら、"
                f"その手段を{A['offer']}に付けます。「営業のご案内はメールでお願いしております」のように、電話でのご案内はお断りでも、"
                "ほかの手段を示されたらこちらです。通話を断られた、ではありません）")
    text = (f"相手と、いまは話を進められないときは request_callback を、聞いた言葉に合う{A['why']}で呼びます（{'・'.join(whys)}）。相手が"
            f"折り返し・伝言を申し出たら、{A['offer']}に「{O['callback']}」「{O['message']}」を、ほかの手段なら「{O['email']}」「{O['fax']}」"
            f"「{O['post']}」を付けます（どれでもなければ付けません）。")
    if target:
        text += (f"お呼びした方ではない方が、お呼びした方の代わりに、ご自身がご用件を伺うと言ったら（「〇〇はおりませんので、代わりに私がお聞きします」"
                 f"・上司の方・同じ部署の方など）report_party を「{PARTY_LABEL['substitute']}」、自分が後任・いまの担当だと言ったら"
                 f"「{PARTY_LABEL['successor']}」で呼びます。電話を別の方に代わる（「別の者に代わります」「少々お待ちください」）のは、お呼びした方が"
                 f"いないと言っていても、お取り次ぎです（「{PARTY_LABEL['reception']}」で呼び、何も言わずに待ちます）。")
    return text + "何と言うか（お礼・お詫び・聞くこと）は、プログラムが流します。自分では言いません。"


# 約束の言葉（かけ直す・連絡する。Intent.md 9-4「勝手に約束しない」・1-3 の取り消せない害）。AIが自分の言葉で答える文の確かめ（own_answer_problem）と、
# 場面の数え方（scenes.no_promise）の両方が、この1か所を使う
PROMISE = re.compile(r"改めて|折り返し|(?:お電話|ご連絡|連絡)(?:いたします|します|させていただきます|差し上げます)|担当(?:の者)?から")
# 断りの言い方（答えられない・分からない・控える）。AIの言葉にも録音にも使わない（✅ 2026-10-22 ユーザー（4回目）：「『そちらにつきましては
# この場ではお答えいたしかねます。』こういう回答をするから、機械っぽいとかAIぽいと思われるんでしょ！電話で営業する人がそんなことはまず言わない
# （契約がその時点で終わる）」）。答えを持っていない問いは、いただいた質問を担当の者に申し伝える形で受け止める（Flow._own_answer）。
# AIの答えの文の確かめ（own_answer_problem）と、場面の数え方（scenes.no_refusal）の両方が、この1か所を使う（refusal_in）
# 「〜かねます」は断り（「〜かねない」は断りではないので、後ろまで見る）
REFUSAL = re.compile(r"(?:いたし|致し|し|でき|出来|申し上げ|分かり|わかり|判り|存じ|受け|応じ|答え)かね(?:ます|る|て)|(?:お答え|ご回答|回答|お伝え|ご案内|"
                     r"申し上げ|ご説明|説明|お約束|約束)(?:すること)?(?:は|が|を)?(?:でき|出来)(?:ません|ない|かね)|(?:分かり|わかり|判り)ません|存じ(?:上げ)?ません|"
                     r"控えさせ|差し控え|お断り|ご遠慮(?:ください|願|いただ)")  # 「ご遠慮なく」は断りではない
# 答えられないことを、担当の者に申し伝える理由として言う文は、断りではない（同じ文の中で「〜ので／ため、…申し伝え」につながる）。
# ✅ 2026-10-23 ユーザーの言い方（答えを持っていない質問の一言）：「恐れ入ります。そのご質問は私では正確にお答えできませんので、担当の者に
#    申し伝えさせていただきますね。」⇒ 断りかどうかは、この1か所（refusal_in）で決める（録音にもAIの言葉にも場面の数え方にも同じに当てる）
_RELAY_REASON = re.compile(r"[^。！!？?]*?(?:ので|ため)[^。！!？?]*申し伝え")


def refusal_in(text: str | None) -> str | None:
    """文の中の断りの言い方（無ければ None）。申し伝える理由として言うもの（_RELAY_REASON）は断りに数えない。"""
    return next((m.group() for m in REFUSAL.finditer(text or "") if not _RELAY_REASON.match(text, m.end())), None)
# AIへの知らせの言葉（声に出してはいけない。⛔ G-57 tc31：返事の注意書き「名前は言い換える」を、AIが声に出した）
_ECHO = re.compile(r"プログラム|報告|操作|言い換え|声に出|答えの文|業務の情報|replay|record|request")
OWN_MAX_CHARS = 80  # 答えの文の長さの上限（短く1〜2文。長い答えは、そのぶん作る・外れる所が増える）
# 答えの文が確かめに通らないとき、理由を添えてAIに書き直させる回数（同じ相手の発話で）。尽きたら録音の一言（phrases.OWN_FALLBACK）。
# ✅ 2026-10-22 ユーザー（4回目）：断りの録音は使わない ⇒ 通らなかった文は、まずAIに直させる（書き直しの間、相手は待つ＝1回1秒ほど）
OWN_REWRITES = 2
UNCHECKED = "中身を確かめられませんでした"  # 答えの文の中身の判定が届かなかった（Flow._own_answer：書き直させずに受け皿へ）


def refusal_note(part: str) -> str:
    """答えの文に断りの言い方が入っていたときに、AIへ書き直しを頼む理由の文（唯一の書き方。言葉の形の決まり refusal_in と、判定AIの REFUSED の
    どちらで見つけても同じ文）。"""
    return f"お断りの言い方（「{part}」）が入っています。断らずに、いただいた質問を担当の者に申し伝える形にします"


def own_answer_problem(answer: str, names: list[str], may_follow_up: bool, verdict=None,
                       name_readings: dict[str, str] | None = None) -> str | None:
    """AIが自分の言葉で言う文（Flow._own_answer）を、声に出す前に確かめる唯一の場所。問題が無ければ None、あれば理由（日本語）。
    止めるのは「明らかに違う中身」だけ（✅ 2026-10-22 ユーザー（5回目）：「明らかに違う内容を喋ろうとしているのが事前に分かれば止めるべき。
    しかし、AIの能力をそぐような制御はしないように！世間話のような対応や設定にない無難な対応はライブAIにしかできないよ！」）：
    - 機械で（その場で・確かに）：名前（社名・担当者名・お呼びした方の名前と読み＝AIの声で崩れる。名前は録音だけ）・お客様への問いかけ（伺うことは
      プログラムが録音で流す。question_in）・読みの決まった名前
      （地名など。name_readings＝表記 -> 読み）を漢字のまま書いた（AIの声で崩れる。⛔ OWN-ANSWER-EARS：漢字の「港区」は 3本中1本・ひらがなは 6本中6本。
      中身の判定は言い換えとして通すので、機械で見る）・断りの言い方（REFUSAL）・
      許されていない約束（PROMISE。業務設定で許された、答えを持っていない問いへの「改めてご連絡」だけは使ってよい）・AIへの知らせの言葉・長すぎる
    - 中身で（verdict＝grounding の判定の読み）：こちらについて知らない事実を言う・お客様の言ったこちらの事実を根拠なく認める・断る
      （言葉の形では拾いきれない断り）。世間話・お礼・気遣いなど、こちらについての事実を言わない受け答えは通す
    verdict が None（判定の読みが届いていない）なら、機械の確かめだけ（None でも、声に出してよいという意味ではない＝呼ぶ側が受け皿へ回す。
    Flow._own_answer）。文字の幅（９と9）はそろえて比べる。
    ⛔ 前の形（ふつうの名詞は、業務の情報かお客様の言葉にある言葉か、中身を足さない言葉の一覧にあるものだけ）は撤去した：言葉で見ていたので、
       G-63 の文の一覧で、通すべき世間話・お礼・用件の答えを12文止め（「本当に暑い日が続いておりますね」「お食事中に大変失礼いたしました」）、
       作り話を2文通していた（「はい、土日も営業しております」・渋谷かと聞かれての「はい、さようでございます」）。
    ⛔ 判定の読みが無いときの足場（地名などの固有名詞と数は、知っていることにあるものだけ）も撤去した：G-63（2026-10-23）で、足場だけの
       判断は、危ない文を 82文中18文通した（「キャンセル料はかかりませんので、ご安心ください」「当日は担当の者がご案内いたします」）"""
    import unicodedata
    text = unicodedata.normalize("NFKC", (answer or "").strip())
    if not text:
        return "答えの文がありません"
    if len(text) > OWN_MAX_CHARS:
        return f"長すぎます（{len(text)}字。{OWN_MAX_CHARS}字まで）"
    if m := _ECHO.search(text):
        return f"こちらへの知らせの言葉（「{m.group()}」）が入っています"
    if hit := next((n for n in names if n and n in text), None):
        return f"名前（「{hit}」）が入っています（名前は声に出しません）"
    if hit := next((n for n in sorted(name_readings or {}, key=len, reverse=True) if n in text), None):
        return f"「{hit}」は、知っていることの書き方（「{name_readings[hit]}」）で書きます（漢字のままだと、声で読み違えます）"
    if hit := refusal_in(text):
        return refusal_note(hit)
    if not may_follow_up and (m := PROMISE.search(text)):
        # どうするかも添える（⛔ 2026-10-22 の4回目の流し G-57 tc36・範囲外の質問の S2：約束の言葉の名前だけを返したので、書き直すたびに
        # 「改めて折り返し」⇒「折り返し」⇒「後ほど」と、約束の言い方だけを替えた）
        return f"約束の言葉（「{m.group()}」）が入っています。連絡やかけ直しは約束せずに、いただいた質問を担当の者に申し伝える、までにします"
    if not may_follow_up and (asked := question_in(text, written=True)):
        # お客様に伺うことは、プログラムが録音で流す（Intent.md 9-3：次にどの質問へ進むか・取得済みのことを重ねて聞かないは、ルールが決める）。
        # ⛔ 2026-10-23 G-57 tc43（世間話・S4）：AIが受け答えの文に「それでは、ご予約はすでにされておりますでしょうか？」を入れ、そのあと
        #    プログラムも同じ質問の録音を流した＝2回聞いた（業務設定で許された「改めてご連絡…よろしいでしょうか？」の申し出は除く）
        return f"問いかけ（「{asked}」）が入っています。お客様に伺うことは、プログラムが流します（問いかけを入れずに書きます）"
    if verdict is not None:
        from grounding import DEFERRED, REFUSED, UNGROUNDED
        if verdict == DEFERRED:
            # 知っていることで答えられる問いを預かった（⛔ J9a：所在地を聞かれて「担当の者に申し伝えます」）：「自分で答える質問」で答え直させる
            known = f"（「{verdict.part}」）" if verdict.part else ""
            return (f"こちらが知っていること{known}で答えられるご質問です。担当の者に申し伝えるのではなく、replay_opening を"
                    f"「{PART_LABEL['own']}」で呼び、知っていることだけで答えます（答えのもとにした業務の情報の文も"
                    f"「{ARG_LABEL['fallback']}」で選びます）")
        if verdict == UNGROUNDED:
            return (f"こちらについて知らない事実（「{verdict.part or text}」）が入っています。知っていることだけで言うか、そのご質問を担当の者に"
                    "申し伝える文にします")
        if verdict == REFUSED:
            return refusal_note(verdict.part or text)
    return None


# 提案の言葉（ご都合の条件・申し出に含める中身）の長さの上限（✅ 2026-11-09 日本時間9時48分09秒 ユーザーの例「お金がかからないで利用できる範囲とお金がかかるのは
# どこからなのかを」＝33字が通る。⛔ 4fd9d79d までは20字＝条件だけの上限で、お客様の気にかかっていることに応える中身が入らなかった）
OFFER_TERMS_MAX = 40
# 会話AIが書く提案の言葉（申し出・方法を選んでいただく質問へのお話にひとこと応える番だけ。Flow._own_answer・_proposal_problem）：お客様の言ったご都合（terms）・
# こちらから足したご都合（added＝お客様は言っていないが、お話に合わせてこちらから足す条件。依頼者へは別の欄で渡す）・申し出に含める中身（topic）・方法（way）。
# ✅ 2026-11-12 日本時間14時58分17秒 ユーザー「アで！」（例の「夕方以降に」はお客様の「平日の昼間は外に出てる」に合わせてこちらから足した条件）
PROPOSAL_ARGS = ("terms", "added", "topic", "way")
PROPOSAL_TERMS = PROPOSAL_ARGS[:3]   # 通話の事実に残す提案の欄（申し出の通話の事実・offer_terms・other_wish。方法は各手順の way・means の欄）
OFFER_TERMS_ENDS = ("て", "で", "に")   # ご都合の条件：「それでは（条件）、（申し出の中身）…」に続く終わり
OFFER_TOPIC_ENDS = ("を", "て")         # 申し出に含める中身：「それでは（中身）（申し出の中身）…」に続く終わり（「〜を」「〜も含めて」）


def offer_terms_problem(terms: str, names: list[str], name_readings: dict[str, str] | None = None, kind: str = "terms") -> str | None:
    """会話AIが報告した提案の言葉（kind＝terms：ご都合の条件「月曜日を避けて」・topic：申し出に含める中身「料金の面も含めて」）の文の形を、声に出す前に確かめる
    唯一の場所。問題が無ければ None、あれば理由。区切りの記号の無い1つの言葉で、伺い直しの文（phrases.offer_words）に続く終わりか（条件＝「〜て」「〜で」「〜に」・
    中身＝「〜を」「〜て」）を見て、自分の言葉の機械の確かめ（own_answer_problem＝名前・読み・問いかけ・約束・断り・知らせの言葉）を同じように通す。中身の判定は
    Flow._proposal_problem（自分の言葉と同じ1か所）。"""
    import unicodedata
    text = unicodedata.normalize("NFKC", (terms or "").strip())
    if len(text) > OFFER_TERMS_MAX:
        return f"長すぎます（{len(text)}字。{OFFER_TERMS_MAX}字まで）"
    if any(c in text for c in "、。,.?!「」"):
        return "区切りの記号を入れずに、1つの言葉で書きます"
    if kind in ("terms", "added") and not text.endswith(OFFER_TERMS_ENDS):   # こちらから足した条件も、ご都合の条件と同じ形
        return (f"「それでは（条件）、（申し出の中身）ということでよろしいでしょうか？」に続くように、"
                f"{'・'.join('「〜' + e + '」' for e in OFFER_TERMS_ENDS)}で終えます（例「月曜日を避けて」「月曜日以外で」「午前中に」）")
    if kind == "topic" and not text.endswith(OFFER_TOPIC_ENDS):
        return ("「それでは（中身）（申し出の中身）ということでもよろしいでしょうか？」に続くように、"
                f"{'・'.join('「〜' + e + '」' for e in OFFER_TOPIC_ENDS)}で終えます（例「料金の面も含めて」「ご利用の流れを」）")
    return own_answer_problem(text, names, False, None, name_readings)


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
    # 声が聞き取れない（poor_audio）で電話を途中で終える：聞き返しを使い切っても（念のための確認でも）聞き取れないまま「-」にした回が、答えを
    # 1つも受け取らないまま、この回数続いたら（答えを1つ受け取ったら数え直す）。⏳ 初めの値＝本物の電話で測って決める（DESIGN.md 6-8。2回続けば、
    # お客様は「お電話が少し遠いようでして…」などの聞き返しを4回聞いている＝聞き続けて「-」にするより、お詫びして終え、復旧次第お詫びとともに
    # かけ直して続きから伺うほうが、お客様の負担が軽く、伺うべきことも伺える）
    trouble_unclear: int = 2

    def judge_wait_s(self, held_s: float = 0.0) -> float:
        """会話の側が判定を待つ長さ（唯一の決まり＝本番の電話接続側 convo.check_handler の待つ長さが切れる時・測りの数え g59_tally.waits）：言葉が出揃って
        から hold_s。判定を待つ間に録音（Flow.hold_play＝新しい方へのあいさつ「お忙しいところ恐れ入ります。」）を流すときは、その長さ held_s だけ延ばす
        （✅ 2026-10-22 ユーザー：「『お忙しいところ、失礼いたします。』を入れておいて、その間に判定を待てばいいんじゃないですか？」）。
        ⛔ 2026-11-06 日本時間13時台の見直し（判定の組の持ち場の A4）で見つけた：延ばす決まりを電話接続側だけが持ち、測りの数えは hold_s だけで数えていた"""
        return self.hold_s + held_s


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
    # 録音で流すときの共通の一言の録音の名前（phrases.RECORDED の鍵）。空なら役目から（callback_〜）。文を替えたシステムの質問だけが、その文の
    # 録音の名前を持つ（番号を伺い直す一言＝number_again_〜。⛔ 2026-10-24 の見直し（調べ役）：文だけを替え、録音の名前は callback_number の
    # ままだった＝流し直しで「恐れ入りますがお電話番号を…」の録音が流れるのに、記録とAIへの知らせは伺い直す一言だった）
    recording: str = ""
    # 業務の質問から文を作ったシステムの質問の、その業務の質問の番号（ほかのご希望を伺う W1＝選ぶ質問の選択肢・振り返りの一言＝ご承認の質問の説明の文）。
    # 文が業務ごとに変わるので、録音は依頼主の単位（question_play の1か所）。空なら共通の一言の録音
    of: str = ""
    # 戻る一言のあと（お話に応えて、もう伺ったこの質問へ戻るとき）に流す形（録音の名前, 文）：初めて伺ったときの前置き（続きの電話の「前回…切れてしまいました
    # ので」・番号を伺い直す「前回お伺いしたお電話番号に…」・方法をお伝えした一言に続ける形）は、もうお客様に届いているので言い直さず、同じ役目のそれだけで
    # 伺う形（作るのは callback_question・番号を伺い直す一言は Flow.__init__。使うのは question_play の1か所）。空なら初めて伺った形のまま。⛔ 2026-11-08
    # 日本時間7時台（11-07 23時26分13秒のご質問の見直しの B-5・B-6）：戻るたびに前置きを言い直し、3回目は前置きごと1つの文にした形の録音が無かった
    plain: tuple = ()
    # 選択肢の型の、選択肢ごとの意味の言葉（選択肢 → 「ご予約はお済みということ」）。答えを言い直して確かめるときに使う（choice_words）。
    # 取込で文字のモデルが決める（人は確かめない）。無い選択肢は名前のまま言い直す（返事の言葉だけの名前は言い直せない）
    meanings: dict = field(default_factory=dict)
    # 選択肢ごとの、いまの話の向きを伺う言葉（取込で文字のAI2つが作る＝CL-2。「いいえ」⇒「パンフレットはお送りしない方がいい」。Flow._topic_first・
    # 確かめの言い方「（言葉）ということでしょうか？」＝phrases.CONFIRM の「いまの話の向きを伺う」）。無ければ曖昧な答えと同じ確かめ
    doubt_words: dict = field(default_factory=dict)
    # 選択肢ごとの、こちらがすること（お客様がされること）の言葉（取込で文字のAI2つが作る＝CL-2・CL-1 が全部を見る。「はい」⇒「パンフレットをお送り」・
    # 「いいえ」⇒「パンフレットの送付は控え」・「カード」⇒「お支払いはカードと」。✅ 2026-11-02 日本時間1時22分 ユーザー：「「はい、それではパンフレットの
    # 送付は控えさせていただきますね？」の方がいいんじゃないですか！」）。事実を伺う質問ではない質問（希望・申し出）で、言い切っていないが
    # 向きの決まった最初の答えを「それでは（言葉）させていただきますね？」で確かめる（phrases.CONFIRM の「言い切っていない答えのあと（こちらがすること）」・
    # Flow._hedged_after）。判定に見せる意味の言葉（meanings）とは別の欄（意味の言葉を差し替えると判定に送る文が変わる＝10-31 日本時間5時37分にお伝えした形）。
    # 無い選択肢は、今までの「念のため（意味の言葉）でよろしかったでしょうか？」。✅ 2026-11-01 日本時間20時56分 ユーザー：「こちら：「念のためパンフレットを
    # お送りして大丈夫だということでよろしかったでしょうか？」よりも、「それではパンフレットをお送りさせていただきますね？」の方がいいんじゃないですか？」
    # 言葉は文の終わり（「させていただきます」「承ります」）まで持つ＝「それでは（言葉）ね？」（✅ 2026-11-03 ユーザー：(D) の案＝申し出・お好みの選び・ご意向
    # （「〜の方向で進めさせていただきます」）・ご様子（「〜ということで承ります」）の4つ）。業務設定の言葉があれば、それ（CL-2）
    do_words: dict = field(default_factory=dict)
    # 選択肢ごとの、その答えを受け取ったあとに伝える一言（業務設定＝依頼者のトークスクリプト。取込のAIは作らない）。言い切った答えでも言い切っていない答えでも、
    # 受け取ったら1回だけ言ってから次へ進む（Flow._advance の1か所）。言い切っていない答えは、確かめ直さずにこの一言へ進む（業務の次の段＝前の答えで伺う
    # 質問と同じ扱い）。✅ 2026-11-03 ユーザー：「「当日はお車でお越しということで承ります」や「○○駐車場をご利用いただけますのでどうぞご利用ください」
    # などでしょ！」・「車で来るなら駐車場の案内や指定などが目的になるんじゃないですか？」。録音の名前は tell_recording_id
    tell_words: dict = field(default_factory=dict)
    # 目的から遠ざかる向きの選択肢（取込で文字のAI2つが決める。どちらか1つでもそう読めば入れる＝安全側）。ここに入る選択肢の言い切っていない最初の答えは、
    # do_words の「それでは…させていただきますね？」を使わない（✅ 2026-11-03 ユーザー：「そもそも、導入の方向で進んでいるのに、「それでは、今月中のご導入は
    # 見送りとさせていただきますね？」だと、わざわざ、獲得できそうなお客様を失いかねない確認ですよね？」・(2)「OK」）。目的につなげて伺うのは、業務設定の
    # 追って伺う質問（ask_if_hedged＝依頼者のトークスクリプト。「それでは、日時変更なさいますか？それとも、キャンセルなさいますか？」）。追って伺う質問が
    # 無ければ、その向きの言葉で言い直さずに「承知しました。」で次へ（✅ 11-03 日本時間17時台「提案の「承知しました」で次に進むでもOK」。Flow._keep_toward_goal）
    away: tuple = ()
    # この質問に入る一言（業務設定＝依頼者のトークスクリプト。CL-2。「それでは予約番号の確認からさせていただきます。」）。前の質問のあと、つなぎの言葉
    # （それでは・続きまして）の代わりに、質問の録音の頭に1本で入れる（spoken_question の1か所）。空なら今のつなぎの言葉。最初の質問（名乗りと一緒に
    # 流す）には付けない。✅ 2026-11-01 日本時間6時05分 ユーザー：「この確認の後、次のステップとして、「予約番号の確認」や「資料の説明」…がある場合は、
    # 例えば、「それでは、予約番号の確認からさせていただきます。」のような次のステップに入るのが普通でしょう。」・6時27分「これは実際の企業と契約後の
    # トークスクリプトになります。今はこういったパターンになるという形式だけ作るステップです。」（どの質問のあとから入っても同じ一言なので、入る先の質問に持つ）
    intro: str = ""
    # 受け入れにも断りにも取れる言葉（AMBIGUOUS_WORDS）だけの答えが、この質問ではどの選択肢の意味になるか（言葉 → 選択肢。例：
    # 「パンフレットをお送りしてもよろしいでしょうか？」の「結構」→「いいえ」）。取込で文字のモデル2つが質問の文から決める（meanings.decide_replies。
    # G-49）。無い言葉は、通話の中で意味を確かめる。✅ 2026-10-17 ユーザー：「人でも前の質問や会話から判断する言葉です」
    reply_meanings: dict = field(default_factory=dict)
    # 「わからない」「答えたくない」の扱い（業務の決まり＝CL-2 の business_rules の「理由×扱い」。(状態, 理由) → HANDLINGS の1つ。必須の質問だけ）。
    # 状態は dont_know・refused、理由は DONT_KNOW_LABEL・REFUSED_LABEL の名前。「答えたくない」に使えるのは結論として終えるだけ（✕ は二度と聞かない）
    handlings: dict = field(default_factory=dict)
    # お客様に事実を伺う質問か（もうしたこと・持っているもの・使っているもの・いまの状態・数など、お客様の希望や選びで変わらないこと）。取込で文字の
    # モデル2つが質問の文から決める（facts.decide。そろわなければ事実を伺う質問）。真なら、選べるものに「どちらでも」を入れず（all_conditions の
    # 1か所＝会話AIにも判定にも出さない。「お任せします」は答えになっていない＝聞き直す）、控える・断る返事は「答えたくない」（meaning_judge.system_desc）。
    # ✅ 2026-11-01 ユーザー：(a)「OK」。⛔ 2026-10-31 の夜（G-53nofits）：判定の説明では「事実を聞く質問には無い」としながら、事実を伺う質問にも
    # 「どちらでも」を出していた（説明と選べるものの食い違い＝facts.py）
    asks_fact: bool = False
    # ご案内の方法（お電話・ご訪問・説明会・オンラインの面談 など）でご案内してよいかを伺う質問か（取込で文字のモデル2つが質問の文から決める＝ways.decide。
    # そろって「伺う質問」のときだけ真）と、業務でご案内できる方法（業務設定の「ご案内できる方法」を取込が入れる。お客様に言う名前のまま＝「メール」「LINE」
    # 「ZOOM」。どれもご連絡の方法の1つ＝means_in で WISH_MEANS に当たるもの）。はい／いいえの質問で真なら、言い切っていないお断りのあとのほかのご希望（W1）で、
    # 断られた方法を除いた方法を申し出る（_wish_text の1か所）。✅ 2026-11-05 日本時間22時28分36秒 ユーザー：「「はい、それではほかにご希望などはございます
    # でしょうか？」だと連絡手段を答える人はほぼいないと思うので、連絡ができないままで終わることになりますよ。」・23時19分06秒「案でOK　(2)ア」
    asks_way: bool = False
    offer_ways: tuple = ()
    # 意味合いを尋ねる一言の候補の言葉（選択肢 → お客様に言う言葉。言い直さない質問のうち、作る callback_question が入れるもの＝W1 は元の質問から
    # _wish_senses・LINE の伺い方 C11 は LINE_HOW_SENSES。入っていない選択肢・質問は尋ねずに聞き直す）
    senses: dict = field(default_factory=dict)
    # 依頼者の最終目的の申し出か（業務設定＝CL-2。業務ごとに複数ありうる）と、その申し出を断る答え（選択肢の名前。取込で決める）。断られて目的が
    # 果たせなくなったら、途中で締め、その業務だけかけない（Flow._goals_lost。✅ 2026-11-01 日本時間20時56分・22時31分 ユーザー）
    final_goal: bool = False
    declines: tuple = ()
    # 選択肢が、こちらが決めた意味の名前（ご本人・不在・尋ねている など）か。真なら、意味の判定にはその名前のまま見せ、意味の定義は説明に書く
    # （meaning_judge.options）。偽（お客様の選択肢の言葉＝はい・いいえ・している など）は、意味の言葉を名前にして見せる（言葉の頭で選ばせない）。
    # ⛔ 2026-10-29：意味の定義（長い文）を名前にしていたので、返事の形の指定にも長い文が並び、「ご本人」と答えるたびに長い文を書き出していた
    named: bool = False
    # 1つの言葉に同時に入りうる別々の事実を、項目ごとに答えさせる質問（目的のお客様の判定＝target_facets の形）。判定は項目ごとに1つずつ答え、
    # 読みは facts_reading の1か所で組み合わせる（meaning_judge.read）。空なら、選べるものの中から1つだけ選ばせる（ほかの質問）
    facets: tuple = ()
    # 項目ごとに答える質問だけ：説明と見分け方と決まりが使う言葉の中身（facet_terms。場面で変わる＝確かめる一言への返事か・後任の方の場面か）
    terms: tuple = ()
    # 目的のお客様の判定だけ：後任の方をお呼びしているか（facts_reading で、いまお呼びしている方＝「代理の方（後任）」）と、はじめにお呼びした
    # 言葉（後任の方をお呼びしたあと、判定に見せる＝「ご本人」は、どの場面でも、はじめにお呼びした方。Flow._calling_questions）
    seeks_successor: bool = False
    called: str = ""
    # 前の質問の答えで伺うかが決まる質問（業務設定＝CL-2。依頼者のトークスクリプト）：(前の質問の番号, その質問のどの答えのとき伺うか＝中の名前の並び。
    # 「わからない」は unknown)。初めの並びには入れず、その答えを受け取ったら前の質問のすぐあとに伺う（Flow._follow_ups の1か所）。空ならいつも伺う。
    # ✅ 2026-11-02 日本時間4時16分 ユーザー：「相手が資料を見ていなくても業務の進展ができるのであれば、そのまま進めたらいいと思いますし、確認しないと
    # 進められないのであれば、こちらから重要な事項を説明して、それの承認をもらう形になるんじゃないでしょうか。」（形だけ作る＝「今回は形だけ作ります：OK」）
    ask_if: tuple = ()
    # ask_if の答えが言い切っていないときだけ伺うか（業務設定＝依頼者のトークスクリプト・CL-2）。真なら、前の質問の言い切っていない最初の答えは、
    # 言い直して確かめずに「曖昧」の印を付けて受け取り、この質問で伺う（Flow._hedged_follow_up の1か所）。✅ 2026-11-03 ユーザー (3)「OK」：お困りごとに
    # 業務の手立てがある答えは、手立てを並べて伺う（「当日のキャンセルはございませんか？」への「都合が悪くなってしまいました」⇒「それでは、キャンセルに
    # なさいますか？それとも、日時を変更なさいますか？」。「日時変更ができるなら、それでおねがいしますとなる可能性があるからです」）・(4)「OK」：時期の答えが
    # 前向きなら「（{最短の日}なら対応できますが、）いつ頃がよろしいでしょうか？」（{最短の日}＝day_words）
    ask_if_hedged: bool = False
    # この質問の、ここで電話を終える答え（業務設定＝依頼者のトークスクリプトの「この答えなら、ここで終える」・CL-2。中の名前の並び。選択肢の型の、宣言した
    # 選択肢と「どちらでも」「どれも要らない」だけ）。この質問よりあとの業務の質問は、この質問の答えが取れて、終える答えでないときだけ伺う（決まりは gate_of の
    # 1か所）。聞き取れない・わからない・答えたくない・まだ伺っていないときも伺わない（先へ進むかが決まらない）。聞き取れないときは、必須の質問なので締めの前の
    # 念のための確認でもう1回伺う。わからないは聞き直さずに締め（10-13「わからない＝「-」で聞き直さない」）、続きの電話でもう一度伺う（earlier_kept）。答えたくないは
    # 二度と伺わない（10-13）。⛔ 2026-11-04 日本時間7時台の4回目のご質問の見直しで見つけた：この説明だけ「わからないも念のための確認でもう1回伺う」と読めた。終える答えなら残りは伺わずに締める（断りではない＝最終目的が断られたとはしない・伺わなかった質問は会話の報告の ended_on）。その答えで
    # 伺う質問（ask_if）は、その答えのトークスクリプトの続きなので伺う。「どれも要らない」には、終える答えに入っていても、ほかのご希望を1回伺う（W1。選び直した
    # 答えで決まり直す）。✅ 2026-11-04 日本時間2時10分54秒 ユーザー「OK」（「お電話がよいと言われたら、ここで終える」を、あとの質問ではなく、その質問に
    # 覚えさせる案）・1時25分39秒「（本人or後任or代理がいると聞き取れない）のに次に進むのは問題でしょ？」「…とほかのご希望を伺うように言っていますよね。」
    ends_on: tuple = ()
    # 説明した内容のご承認を伺う質問か（はい／いいえの型。説明の文は、この質問に入る一言＝intro か、質問の文そのもの。最初の質問は入る一言を持てない＝CL-2 の 4-185 の (4)）。2つ目の選択肢（ご承認いただけない）のときは、
    # 「ご納得いただけなかった点はどの部分になりますでしょうか？」を1回伺い（システムの質問 N1）、答えは通話の事実 not_agreed に残す（依頼者へ返す）。
    # 業務の最終目的の質問なら、伺ったあとで締める（その業務はかけない＝_goals_lost）。✅ 2026-11-02 日本時間4時36分 ユーザー：「(b) いいえ＝「ご納得いた
    # だけなかった点はどの部分になりますでしょうか？」のような確認が必要ではないですか？」
    approval: bool = False
    # 言い切っていなくても確かめずに受け取る質問（ご説明のあとの「ほかに…」「ご納得いただけましたでしょうか？」＝システムの質問だけ）。✅ 2026-11-02
    # 日本時間5時56分 ユーザー：「「だいたい分かりました」とかなら、「ありがとうございます。それでは、～」と次のステップでいい」（確かめると強引になる）
    firm_not_needed: bool = False
    # 必須でなくても、会話AIの報告と判定の読みが食い違えば、その場で1回だけ伺い直す質問（受け取る所 Flow._op_record_answer と聞き返しの1か所
    # Flow._ask_again が読む。聞き取れないだけなら聞き返さない＝必須でない質問のまま）：ご承認いただけなかったときの手順の N1〜N3＝見分けるのは判定の読み
    # （✅ 2026-11-02 日本時間6時21分 ユーザー：「契約を断ろうとしているのか、本当に分からないのかをしっかりと見分けないといけない」）。食い違えば、言い直さない
    # 質問の決まり（restates）どおり「はい、」＋「恐れ入ります。（聞くこと）をもう一度…」（本書 9-5）。⛔ 2026-11-06 日本時間5時56分55秒のご質問のあとの読み
    # 直しで見つけた：必須でないので、会話AIの「わからない」を判定と食い違っても受け取り、選んだ答えの食い違いは「-」にしていた（測りの数えは聞き直すと数えていた）
    asks_once: bool = False
    # ほかに分からない点が無いかを伺う質問か（「ご不明な点はございますでしょうか？」）と、その「無い」にあたる選択肢の名前（打ち消しの問い「ご不明な点は
    # ございませんでしょうか？」なら「はい」）。取込で文字のAI2つが質問の文と選択肢から決める（unclear.decide。そろわなければ空＝伺う）。空でなければ、ご承認
    # いただけなかったときの手順で「ほかにご納得いただけない点は…」（N2）に「分かった」と答えていただいたあとは伺わず、この答えにする（Flow._no_doubts_step の
    # 1か所）。✅ 2026-11-04 日本時間20時27分 ユーザー：「(1) トークスクリプトの「ご不明な点は…」の重ね聞き：「この質問は、ほかに分からない点が無いかを伺う
    # 質問か」を決めます。（ご不明な点は無い）として扱い、重ねて伺いません。OK」（19時26分のご質問「お客様「だいたい分かりました。」の後に、こちら「ありがとう
    # ございます。それではご不明な点はございますでしょうか？」となぜ、再度、不明点を確認するんですか？」＝9-3「取得済みのことを重ねて聞かない」）
    unclear_none: str = ""
    # ご案内の方法の申し出のあと、申し出た方法を並べて伺い直す質問か（callback_question の which で、方法が2つから＝way_any_line）。選べるものに「どちらでも」
    # （ANY）を出す（all_conditions の1か所）＝「どれでも」は答え（どちらでも＝10-17 の案A）。✅ 2026-11-07 日本時間3時14分01秒 ユーザー「(2) H3…イ」
    lists_ways: bool = False
    # 選択肢ごとの良い点・気をつける点（{選択肢の名前: その良い点・気をつける点の文}。業務設定＝取込＝CL-2。空なら無い）：迷っているお客様に、選ぶ助けになることを
    # 会話AIが確かめた自分の言葉で伝える材料（Flow._hesitation・知っていることの1行＝instruction.Business.knowledge）。無ければ「どういった点でお迷いでしょうか？」と
    # 1回伺う（phrases.HESITATE_POINT）。✅ 2026-11-07 日本時間13時05分06秒 ユーザー：「カード と振り込みのメリットデメリットを把握しておき 例えば「カードだと
    # カード会社のポイントがつくのでそういう点も 考慮されるといいかもしれませんね 」のように促すのが一番いいんじゃないでしょうか。」・18時03分49秒「(1) ア」
    option_notes: dict = field(default_factory=dict)
    # 値を伺う質問（日時・数・金額＝ESTIMATE_TYPES）の答えの変更を受け付ける期限（「前日」「3日前」「前週の金曜日」など。業務設定＝取込＝CL-2。空なら無い）：
    # 値が確定でない答え（「15日あたり」＝approx_value）・値の言い直しへの「まだ決まっていない」を、ひとまずその値にして変更の受付を添える形で受ける（Flow._tentative。
    # 「はい、それではひとまず15日あたりとさせていただきますね？変更があった場合には前日までにご連絡いただけましたらご対応可能です。」・言い直しへの「まだ決まって
    # いない」には「承知しました。それではまだ確定ではないと承っておきますね。」のあとに変更の受付の一文＝5時33分05秒）。空なら承っておく形
    # （「承知しました。それでは15日あたりでお考えということで承っておきますね。」）。✅ 2026-11-08 日本時間3時48分03秒 ユーザー：「…（～までにご連絡いただけましたら）
    # （～までであれば）ご対応可能です。」～は日付や曜日、前日など。」
    change_by: str = ""
    # 確定した値で登録する質問か（確定日付の登録が要る業務の質問。業務設定＝取込＝CL-2）：ひとまずの形（change_by）で、お客様の言い方の値（「15日あたり」）ではなく
    # 値の芯（「15日」）でひとまずとする（Flow._tentative）。✅ 2026-11-08 日本時間5時33分05秒 ユーザー：「お客様「15日あたりですかね。」：こちら「はい、それでは
    # ひとまず15日あたりとさせていただきますね？変更があった場合には前日までにご連絡いただけましたらご対応可能です。」相手が「あたり」をつけたら、こちらも「あたり」を
    # つけた方がいいでしょう。ただし、確定日付の登録が必要な業務の場合は、現状のままで大丈夫です。」。期限（change_by）が無い質問でも、承っておく形
    # （値「15日あたり」＝登録できない）ではなく、変更の受付の一文を付けないひとまずの形「はい、それではひとまず15日とさせていただきますね？」（✅ 6時04分57秒「イでOK」）
    definite_value: bool = False
    # お客様が「どちらの場合もありうる」と言ったとき（「どっちでもいいですよ」「当日次第ですね」）に確かめる (選択肢, 言葉)（依頼者が前もって備えなければならない
    # 答えと、その可能性を言う言葉。取込で文字のAI2つが質問の文と選択肢から決める＝maybes.decide。そろわなければ空）。あれば、選べるものに「どちらでも」を出し
    # （all_conditions）、判定がそう読んだら受け取らずに「はい、（言葉）可能性もあるということでよろしいでしょうか？」で確かめる（Flow._ask_confirm の maybe・
    # 言い方は phrases.MAYBE_ASK）。お返事の「はい」は、その選択肢を言い切っていない印とお客様の言葉で受け取る（依頼者へ「はい」とお客様の「どっちでもいい」）。
    # ⛔✅ 2026-11-09 日本時間4時36分49秒 ユーザー：「どっちでもいいですよ。に対して…こちら「はい、恐れ入ります。もう一度お聞かせいただいてもよろしいでしょうか？」
    # で本当に良いと思っているんですか？…「お車で来る可能性もあるということでよろしいでしょうか？」とか、「駐車場が必要な可能性もあるということでよろしいでしょうか？」
    # というような確認になるんじゃないんですか？大事なのは、最終的に依頼者は何を知りたいのかということですよね。」
    maybe: tuple = ()

    def __post_init__(self):
        assert self.type in TYPES, f"{self.id} の型「{self.type}」は、型の一覧にありません（I-16）"
        assert not self.ask_if or (len(self.ask_if) == 2 and self.ask_if[1]), f"{self.id}：伺う条件は（前の質問の番号, 答えの並び）です"
        assert not self.approval or (self.type == "yes_no" and len(self.conditions) == 2), f"{self.id}：ご承認を伺う質問は、はい／いいえの型です"
        assert not self.ends_on or (self.type in CHOICE_TYPES and not self.callback and self.required
                                    and set(self.ends_on) <= set(self.all_conditions) - set(SYSTEM_CONDITIONS)), \
            f"{self.id}：ここで終える答え（ends_on）は、必須の業務の選択肢の質問の、宣言した選択肢か「どちらでも」「どれも要らない」です"
        assert not self.unclear_none or (self.type in CHOICE_TYPES and not self.callback and self.unclear_none in self.conditions), \
            f"{self.id}：ほかに分からない点は無いにあたる答え（unclear_none）は、業務の選択肢の質問の、宣言した選択肢です"
        assert not self.maybe or (self.type in CHOICE_TYPES and not self.callback and len(self.maybe) == 2 and self.maybe[0] in self.conditions
                                  and self.maybe[1]), \
            f"{self.id}：どちらの場合もありうるときに確かめる答え（maybe）は、業務の選択肢の質問の (宣言した選択肢, 言葉) です"
        for (state, reason), kind in self.handlings.items():
            table = {"dont_know": DONT_KNOW_LABEL, "refused": REFUSED_LABEL}.get(state)
            assert table is not None and reason in table and kind in HANDLINGS, f"{self.id}：扱い {(state, reason)} → {kind} は使えません"
            assert state != "refused" or kind == "conclude", f"{self.id}：「答えたくない」に使える扱いは、結論として終えるだけです"
        assert not self.callback or self.callback in SYSTEM_ROLES, f"{self.id}：システムが足す質問の役目「{self.callback}」はありません"
        assert not self.reading_of or (self.type == "name" and _KANA.fullmatch(self.prefill or "")), \
            f"{self.id}：名前の読みの確かめは、名前の型で、まず言ってみる読み（ひらがな）が要ります"
        clash = set(self.conditions) & (set(SYSTEM_CONDITIONS) | set(SYSTEM_LABEL.values()) | {ANY, ANY_LABEL, NONE, NONE_LABEL})
        assert not clash, f"{self.id} の選択肢が、システムの選択肢と同じ名前です：{clash}"
        assert not self.meanings or (self.type in CHOICE_TYPES and set(self.meanings) <= set(self.conditions)), \
            f"{self.id}：意味の言葉は、選択肢の型の、宣言した選択肢にだけ付けられます"
        assert not self.reply_meanings or (self.type in CHOICE_TYPES and set(self.reply_meanings) <= set(AMBIGUOUS_WORDS)
                                           and set(self.reply_meanings.values()) <= set(self.conditions)), \
            f"{self.id}：曖昧な言葉の意味は、選択肢の型の、宣言した選択肢にだけ決められます"

    @property
    def offered(self) -> str | None:
        """申し出の質問なら、申し出を受けていただく答え（選択肢の名前）。違えば None（唯一の決まり＝受けていただいたお返事の相づち Flow._ack_choice・
        条件つきの受け Flow._terms_reported）：申し出のシステムの質問（OFFER_ROLES＝詳しい者より改めて N4・締める前の申し出 N6）・ご案内の方法でご案内して
        よいかを伺う質問（asks_way）・こちらがすることを申し出る業務の質問（はい／いいえの質問で、受ける答えのこちらがすることの言葉が「〜させていただきます」＝
        申し出の言葉を作る phrases.offer_words と同じ決まり）。ご承認・事実を伺う質問・名前の読みの確かめは申し出ではない。
        ✅ 2026-11-12 日本時間13時40分05秒 ユーザー：「(5) (ウ) は a：申し出を受けていただいたお返事には、必ず「ありがとうございます。」にする。」"""
        # 読むのは、選択肢ごとのこちらがすることの言葉の種類だけ（meanings.do_kind の1か所＝取込の決まりが種類ごとに決めた終わりの形）。申し出を受ける向きの
        # 言葉（「〜させていただきます」）の選択肢が1つだけある質問が申し出（申し出のシステムの質問の言葉も同じ形＝HANDOFF_DO・CloseOffer.do）。⛔ 5de3425e では
        # 質問の文の形（「〜てもよろしいでしょうか」）でも決めていた＝依頼者が書く文の形は決まっていないので、「パンフレットをお送りしましょうか？」を取りこぼした
        # （✅ 2026-11-12 まとめ役の (H4)）
        from meanings import do_kind
        offers = [c for c in self.conditions if do_kind(self.do_words.get(c, "")) == "offer"]
        return offers[0] if len(offers) == 1 else None

    @property
    def all_conditions(self) -> tuple:
        # 「どちらでも」「どれも要らない」は、業務の選ぶ質問で事実を伺わない質問だけ（システムの質問＝お立場・ご承認いただけなかったときの手順には足さない。
        # ⛔ 2026-11-02 に見つけた：お立場の質問 S1 にも「どちらでも」を出していた）。申し出た方法を並べて伺い直す質問（lists_ways）は「どちらでも」だけ
        # （「どれも要らない」は、その質問の断る答え＝WISH_NONE がある）
        # どちらの場合もありうるときに確かめる答えのある質問（maybe）も「どちらでも」だけ（はい／いいえ・事実を伺う質問でも出す＝「どちらでも」を答えとしては
        # 受け取らず、その可能性を確かめる＝Flow._ask_confirm の maybe。11-01 1時50分「お任せしますは答えになっていない」と食い違わない。✅ 11-09 4時36分49秒）
        business = self.type == "choice" and not self.asks_fact and not self.callback
        return tuple(self.conditions) + ((ANY, NONE) if business else (ANY,) if self.lists_ways or self.maybe else ()) + SYSTEM_CONDITIONS

    def offers(self, condition: str | None) -> bool:
        """condition が、この質問でお客様が選べる答え（宣言した選択肢と、この質問が出す「どちらでも」「どれも要らない」＝all_conditions のうち、わからない・
        答えたくないを除いたもの）か（唯一の決まり＝判定の読み・確かめる答えが、この質問の答えを名指ししているか。Flow._names_other・_undecided_answer・
        _op_record_answer の確かめ・_confirm_again が使う）。⛔ 2026-11-09 日本時間3時46分53秒のご質問のあとに見つけた：4か所が「宣言した選択肢か「どちらでも」
        「どれも要らない」か」を手で書き、事実を伺う質問（「どちらでも」を出さない＝11-01 1時50分）への判定の「どちらでも」を名指しと数え、「はい、念のため
        どちらでも構わないということでよろしいでしょうか？」と確かめていた"""
        return condition is not None and condition in self.all_conditions and condition not in SYSTEM_CONDITIONS

    @property
    def checks_now(self) -> bool:
        """確かめがそろわない答えを、その場で確かめる質問か（唯一の決まり＝必須の質問・その場で1回だけ伺い直す質問 asks_once。ほかは聞き返さず確かめない
        ＝6-11）：受け取る所 Flow._op_record_answer（そろわなければ確かめる／「-」）と、決める時に判定の確かな読みが無いか Flow._checks（その場で確かめる
        のはこの質問だけ。ほかは受け取って、届かなかった確かめ unchecked として通話後の確かめが録音でやり直す）。"""
        return self.required or self.asks_once

    def declined(self, condition: str | None) -> bool:
        """この答えがお断りか（唯一の決まり）：断る答え（取込で決める＝declines。システムの質問は _ROLE_DECLINES）・選ぶ質問の「どれも要らない」・
        説明した内容のご承認の質問の2つ目（ご承認いただけない）。最終目的が果たせなくなったか（Flow._goals_lost）・ご承認いただけなかったときの
        手順（Flow._point_step）・相づち（お断りにお礼は返さない＝Flow._ack）が、ここだけで決める。"""
        return condition is not None and (condition in self.declines or condition == NONE
                                          or (self.approval and len(self.conditions) > 1 and condition == self.conditions[1]))

    def turned_down(self, condition: str | None) -> bool:
        """この答えが、申し出のお断りか、目的から遠ざかる向きか（唯一の決まり）：お断り（declined）と、目的から遠ざかる向きの答え（away_answers）。
        受け取ったあとの相づち（「承知しました。」＝お礼は返さない＝Flow._ack）と、そのまま締めるときの締めの言葉（「はい、承知いたしました。お忙しい
        ところ…」＝Flow._close）が、ここだけで決める（Flow._turned_down_now）。✅ 2026-11-03 ユーザー：遠ざかる向きに寄った・言った答えは
        「承知しました。」で次へ・✅ 2026-11-02 日本時間6時33分 ユーザー：「…「はい、お忙しいところ…」だと唐突なので、こちら「はい、承知いたし
        ました。お忙しいところ…」の方がいいんじゃないでしょうか？」。⛔ 2026-11-04 日本時間7時台に見つけた：相づちは、言い切らない遠ざかる答えと
        お断りの答えだけを別々の目印で見て、言い切った遠ざかる答えには順番で「ありがとうございます。」を返し、締めは目的が果たせなくなったとき
        （goal_refused）だけ「承知いたしました。」にしていた（最終目的が2つの業務で、片方を受けていただいたあとの最後のお断りに「はい、お忙しい
        ところ…」）。"""
        return condition is not None and (self.declined(condition) or condition in self.away_answers)

    @property
    def away_answers(self) -> tuple:
        """目的から遠ざかる向きの選択肢（話の向きで決める所＝Flow._hedged_after・_ask_confirm・_keep_toward_goal・ほかのご希望を伺う文 callback_question
        は、away をじかに読まずにここを読む＝唯一の決まり）：取込で決めた away と、最終目的の質問のお断りの答え（declines）。最終目的を断る答えは、決まりと
        して目的から遠ざかる（CL-1 の 11-03 のご判断の (9)。⛔ 見直しで見つけた：取込の away に無いお断りは「それでは…お送りしなくてよいということで
        承りますね？」と遠ざかる向きの言葉で言い直し、認められると、ほかのご希望を伺わずに締めた）。説明した内容のご承認の質問の2つ目（ご承認いただけない）も、
        決まりとして目的から遠ざかる（その答えには、ご納得いただけなかった点を伺う手順がある＝Flow._point_step。✅ 2026-11-02 日本時間4時36分 ユーザー。
        ⛔ 2026-11-04 日本時間7時台の4回目のご質問の見直しで見つけた：言い切っていない「いいえ」を「それではご説明した内容ではご承認いただけないということで
        承りますね？」と遠ざかる向きの言葉で言い直していた）。最終目的でない質問の断る答えは入れない（その申し出を断っただけ＝お断りの相づち・締めは
        turned_down。✅ 2026-11-01 日本時間20時11分 ユーザー：「担当者からの折り返しを拒否しただけ」）。"""
        approval_no = (self.conditions[1],) if self.approval and len(self.conditions) > 1 else ()
        # 申し出の断る向きの言葉（「〜は控えさせていただきます」＝取込の決まり DO_PROMPT の断る向き・meanings.do_kind の decline）の答えも、取込の away に無くても
        # 遠ざかる向き（取込の away の決まり AWAY_PROMPT も「控える」を遠ざかるとする）。✅ 2026-11-12 まとめ役の (I2)。⛔ 952cea0b までは away が無いと
        # 「はい、それではお電話でのご案内は控えさせていただきますね？」と遠ざかる向きの言葉で言い直していた（11-03 の決まりに反する）
        from meanings import do_kind
        declined = tuple(c for c in self.conditions if do_kind(self.do_words.get(c, "")) == "decline")
        return tuple(dict.fromkeys((*self.away, *declined, *(self.declines if self.final_goal else ()), *approval_no)))

    @property
    def toward(self) -> tuple:
        """目的に進む向きの選択肢（唯一の決まり）：宣言した選択肢のうち、目的から遠ざかる向き（away_answers）でないもの。どちらとも取れる答え・食い違った答えを
        伺う向き（Flow._keep_toward_goal）と、まだ答えていない質問について尋ねられて答えたあとの最終の確かめの向き（final_toward）が、ここだけで決める
        （⛔ 2026-11-06 日本時間12時台、final_toward を入れる途中で見つけた：同じ問いを2か所で別々に書いていた）。"""
        return tuple(c for c in self.conditions if c not in self.away_answers and c != NONE)

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
_SO = ("そう", "さよう", "そっ")       # 相づちの「そうですか」の中身の言葉（指す言葉の「そう」。「さようでございますか」「そっか」も）
_ACK_HEADS = ("なるほど",)            # 相づちの頭に来る、受け止めの副詞（感動詞の「あ」「へえ」「はい」と同じく、問いの中身にしない）


def acknowledgment(sentence: str) -> bool:
    """「そうですか。」「あ、そうでしたか。」「なるほど、そうなんですか。」「さようでございますか。」のような相づちか（文の終わりが「か」でも、
    相手の話を受け止める言葉で、こちらへの問いかけではない）：感動詞・記号・受け止めの副詞を除くと、中身の言葉が「そう」だけで、そのあとは
    助動詞・助詞（「ございます」の「ござい」も）だけで「か」で終わる。「？」の付いた文は問い（語尾の上がりを聞き取った印）。
    ⛔ 2026-10-24 の見直しで見つけた誤り：語尾だけで見ていたので、「あ、そうでしたか。はい、予約しています。」の「そうでしたか」を相手からの
       質問と取り、AIに先に答えさせる形だった（Flow._advance の callee_asked）。中身のある問い（「大丈夫ですか」「本当ですか」「それは何ですか」）は今までどおり。"""
    if re.search(r"[？?]\s*$", sentence):
        return False
    from jtext import words
    ws = [w for w in words(sentence) if w.feature.pos1 not in ("感動詞", "補助記号") and w.surface not in _ACK_HEADS]
    return (len(ws) >= 2 and ws[0].surface in _SO and ws[-1].surface == "か"
            and all(w.feature.pos1 in ("助動詞", "助詞") or w.feature.pos2 == "非自立可能" for w in ws[1:]))


def question_in(text: str | None, written: bool = False) -> str | None:
    """文の中の問いかけの文（文の終わりが問いかけのもの。無ければ None）。判定を使わない通話の、相手の言葉の中の質問（Flow._question_to_us）・こちらへ尋ねた問いの渡す文（asked_text）と、AIの自分の言葉の中の
    問いかけ（own_answer_problem）を見分ける唯一の場所。written＝AIが書いた文（書き言葉なので、問いかけは「？」で終える＝「？」の文だけを見る。
    「さようでございましたか。」は相づち。聞き取りの文字は「？」が抜けうるので、語尾でも見る）。"""
    for sent in re.findall(r"[^。！!？?]+[。！!？?]?", text or ""):
        t = sent.strip()
        if re.search(r"[？?]$", t) if written else (_CALLEE_ASKS.search(t) and not acknowledgment(t)):
            return t
    return None


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


CALLBACK_TOPIC = {"back_time": "お戻りになるお時間", "later_time": "ご都合のよろしいお時間", "transfer_time": "お取り次ぎいただけそうな時期", "number": "お電話番号",
                  "reachable_time": "お電話に出られるお時間", "back_date": "お戻りのご予定", "next_workday": "次のご出社のご予定",
                  "email": "メールアドレス", "fax": "ファクス番号",
                  "address": "ご住所", "authority": "ご判断いただけるお立場か", "message": "ご伝言", "back_today": "本日のお戻りのご予定",
                  "call_again": "改めてのお電話", "promise_again": "改めてのお電話", "ask_who": "お分かりになる方", "ask_when": "お分かりになる時期",
                  "refusal": "今後のお電話", "hurry_again": "改めてのお電話", "hurry_when": "ご都合のよろしい日時", "not_agreed": "ご納得いただけなかった点",
                  "other_point": "ほかにご納得いただけない点", "agreed_now": "ご説明した内容へのご納得", "line": "LINEのID", "other_wish": "ほかのご希望",
                  "line_how": "LINEでのご連絡の方法", "mobile": "携帯電話の番号", "wait_or_later": "このままお待ちするか改めておかけ直しするか",
                  "concern": "お気づきの点", "handoff": "詳しい者からのお話のご希望", "handoff_way": "ご希望の方法",
                  "close_offer": ""}   # 締める前の申し出の聞くことは業務設定（CloseOffer.topic）
# ご承認の質問への迷いから始めた手順の1つ目（「どういった点でお迷いでしょうか？」＝callback_question の hesitating）の聞き直しの聞くこと
# （✅ 2026-11-08 日本時間2時21分15秒「(1)〜(4)ともイでOK」の (4)）
HESITATE_TOPIC = "お迷いの点"
# 文が依頼主の単位の録音のもの（伝言の文＝業務設定 message・約束の電話の一言＝お名前が入る＝instruction.Business.replay_parts の promise_again）
OPENING_ROLES = ("message", "promise_again")
# 文が業務の質問から決まる、依頼主の単位の録音のもの（ほかのご希望を伺う質問＝その質問の選択肢の名前が入る。録音の名前は other_wish_recording_id）
# 申し出の方法を選んでいただく質問（handoff_way＝業務設定の引き継ぎの方法・締める前の申し出の方法の名前が入る＝offer_ways。録音の名前は OFFER_WAY_RECORDING）と、
# 締める前の申し出（close_offer＝業務設定の文。録音の名前は CLOSE_OFFER_RECORDING）も
BUSINESS_ROLES = ("other_wish", "handoff_way", "close_offer")
OFFER_WAY_RECORDING = "offer_which"   # 申し出の方法を選んでいただく一言の依頼主の単位の録音（make_ack_clips.script_table・callback_question の1か所）
CLOSE_OFFER_RECORDING = "close_offer"   # 締める前の申し出の一言の依頼主の単位の録音（同じ2か所）
# 申し出の役目（通話の事実の名前も同じ＝Flow._handoff_step の1か所）：営業に引き継ぐ架電の詳しい者からのお話（N4）・そうでない架電の締める前の申し出（N6）
OFFER_ROLES = ("handoff", "close_offer")
# 申し出の方法をお客様に選んでいただくシステムの質問（迷っているお客様への応じ方の1か所＝hears_hesitation が入れる。保留は伺わない＝asks_wish には入れない）
WAY_ROLES = ("other_wish", "handoff_way", "line_how")
# 業務の質問によらず文が決まる役目の並び（唯一の並び＝会話AIの道具の宣言 tool_declarations が初めから並べる質問・SYSTEM_ROLES の元）。言い方の表ごとに
# 手で並べない（⛔ 2026-11-08 日本時間2時台：本心を伺う一言 D2＝CALLBACK_ASK_REUSED を足したとき、宣言の手の並びに無く、AIが答えを報告できない形だった。
# 10-24・11-03 にも同じ穴）
ASKED_ROLES = (*CALLBACK_ASK, *CALLBACK_ASK_REUSED, *OPENING_ROLES)
SYSTEM_ROLES = set(ASKED_ROLES) | set(BUSINESS_ROLES)  # システムが足す質問の役目
_ROLE_TYPE = {"number": "phone", "fax": "phone", "email": "email", "address": "address", "authority": "choice", "message": "yes_no", "ask_who": "name",
              "back_today": "yes_no", "call_again": "yes_no", "promise_again": "yes_no", "refusal": "yes_no", "hurry_again": "yes_no",
              "not_agreed": "choice", "other_point": "choice", "agreed_now": "yes_no", "line": "line_id", "other_wish": "choice",
              "line_how": "choice", "mobile": "mobile", "wait_or_later": "choice", "concern": "choice", "handoff": "yes_no", "handoff_way": "choice",
              "close_offer": "yes_no"}
# 代わりの方にお立場を伺った答えのうち、話を進めてよい答え（_substitute_decided の1か所）。✅ 2026-10-22 ユーザー（4回目）：問いは「ご判断いただける
# お立場の方ということで問題ございませんでしょうか？それともお取次ぎのみいただけるということでしたでしょうか？」の2つから選んでいただく形
AUTHORITY_YES = "判断できる"
# 方法を並べた伺い・申し出に、どの方法かは言わずに受けていただいた答えの選択肢の名前（唯一の名前）：LINE の伺い方（C11＝Flow._line_how）と、ご案内の方法の
# 申し出（W1＝_wish_choices・Flow._wish_step。✅ 2026-11-06 日本時間9時05分17秒）の両方。どちらも1回だけ、方法を並べて伺い直す
WAY_UNSAID = "方法は言っていない"
# LINE の伺い方（C11）を、繋がらないお返事のあとの LINE でない方法の提案で伺い直したときの答えの印（Flow._offer_answer の1か所。伺い方の選択肢＝ID・SMS・メールとは
# 重ならない名前。受けていただいたら、ご連絡の方法をその方法に決め直す＝Flow._way_switch。声には出さない）
WAY_SWITCH = "{way}に替える"
_ROLE_MEANINGS = {
    # 意味の言葉は、言い直しの言い方にもそのまま入る（choice_words：「念のため（意味の言葉）でよろしいでしょうか？」）ので、短く言い切る形にする
    "authority": {AUTHORITY_YES: "この件についてご判断いただけるお立場の方ということ", "取次ぎのみ": "お取次ぎのみいただけるということ"},
    "message": {"はい": "伝言を伝えていただけるということ", "いいえ": "伝言は伝えられないということ"},
    "back_today": {"はい": "本日お戻りのご予定があるということ", "いいえ": "本日はお戻りにならないということ"},
    "refusal": {"はい": "今後このようなお電話はご遠慮したほうがよいということ", "いいえ": "今後このようなお電話もご遠慮しなくてよいということ"},
    "hurry_again": {"はい": "また改めてお掛けしてよいということ", "いいえ": "改めてお掛けしないほうがよいということ"},
    # 待つか、かけ直すか（Flow._wait_or_later・_wait_or_later_decided）。✅ 2026-11-05 日本時間23時56分48秒 ユーザー「(ア)」
    "wait_or_later": {WAIT_NOW: "このまま電話をつないでお待ちしてよいということ", CALL_LATER: "改めておかけ直ししてよいということ",
                      KEEP_ON: "待つのも改めておかけ直しするのも要らず、そのままお話を続けたということ（その前のお話に答えた・もう大丈夫だと言った など）"},
    "call_again": {"はい": "後ほど改めてお掛けしてよいということ", "いいえ": "本日は改めてお掛けしないほうがよいということ"},
    "promise_again": {"はい": "後ほど改めてお掛けしてよいということ", "いいえ": "本日は改めてお掛けしないほうがよいということ"},
    # ご承認いただけなかったときの手順（Flow._point_step）：分からない点を聞いているのか、理由を挙げてお断りしようとしているのかを、判定の読みで見分ける
    # （回数では切らない）。✅ 2026-11-02 日本時間6時21分 ユーザー：「こちらで答えられるときは、1通話で2回まででなく、親身に答えないといけないんじゃないですか？契約を断ろうとしているのか、本当に分からないのかをしっかりと見分けないといけないですよ。本当に分からないだけなら、回数制限をしたら、せっかくとれる契約をみすみす逃すことになりますよ。」
    "not_agreed": {"点を言った": "分からない点・確かめたい点を言ったということ（ご説明すれば分かりうる）",
                   "断ろうとしている": "理由を挙げて、お断りしようとしているということ"},
    # 「ほかに…」（N2）と、続きの電話の振り返りの一言（同じ選択肢＝callback_question の recap）の両方で読むので、「ほかに」は質問の文に任せる（意味の言葉には入れない）
    # 「点は言っていない」（POINT_UNSAID）＝分からない点があるとだけ言った：プログラムが1回「ほかにご不明な点はどのようなことになりますでしょうか？」と伺う
    # （Flow._point_step。✅ 2026-11-06 日本時間9時05分17秒 ユーザー）
    "other_point": {"分かった": "ご納得いただけない点は無い・内容が分かったということ",
                    "分からない点がある": "分からない点・確かめたい点を言ったということ（ご説明すれば分かりうる）",
                    "点は言っていない": "分からない点・確かめたい点があると言ったが、どの点かは言っていないということ",
                    "断ろうとしている": "理由を挙げて、お断りしようとしているということ"},
    "agreed_now": {"はい": "ご説明した内容でご納得いただけたということ", "いいえ": "ご説明した内容ではご納得いただけなかったということ"},
    # LINE でのご連絡の連絡先の伺い方（Flow._line_how の1か所。選択肢の名前 → 伺う連絡先＝LINE_HOW）。✅ 2026-11-04 日本時間19時26分 ユーザー
    "line_how": {"ID": "LINEのIDを伝えるということ", "SMS": "SMSでLINEに招待してほしいということ", "メール": "メールでLINEに招待してほしいということ",
                 WAY_UNSAID: "どの方法かは言わずに、その形で対応してよいということ"},
    # 本題に入ったあとの「もういいです。」に本心を伺った一言（「何か問題でもございましたでしょうか？」＝Flow._concern_ask）への答え（Flow._concern_decided）。
    # ✅ 2026-11-08 日本時間2時21分15秒 ユーザー（お言葉は phrases.CALLBACK_ASK_REUSED）
    "concern": {"問題を言った": "こちらの話し方・進め方・ご案内の中身などに、気になる点・不満な点を言ったということ",
                "問題は無い": "特に問題は無い・気にしないでよいということ",
                "断ろうとしている": "これ以上は話したくない・電話を終えたいということ"},
    # 営業に引き継ぐ架電で、点を言っていただけないままご承認の手順を終えたときに伺う一言への答え（Flow._handoff_step。✅ 2026-11-09 日本時間4時36分49秒 ユーザー）
    "handoff": {"はい": "詳しい者から改めてお話ししてよいということ", "いいえ": "詳しい者からのお話は要らないということ"},
    # 営業に引き継がない架電で、同じ場面に伺う締める前の申し出（業務設定の文＝CloseOffer。Flow._handoff_step の同じ1か所。✅ 2026-11-09 日本時間6時33分57秒「(ア)…ｂ」）
    "close_offer": {"はい": "申し出のとおりにしてよいということ", "いいえ": "申し出は要らないということ"}}
POINT_UNSAID = "点は言っていない"   # 「ほかに…」（N2）への、分からない点があるとだけ言った答え（_ROLE_MEANINGS の other_point・Flow._point_step）
# システムの質問のお断りの答え（Question.declines。ご承認いただけなかったときの手順：お断りしようとしている・ご納得いただけない）
_ROLE_DECLINES = {"not_agreed": ("断ろうとしている",), "other_point": ("断ろうとしている",), "agreed_now": ("いいえ",), "concern": ("断ろうとしている",),
                  "handoff": ("いいえ",), "close_offer": ("いいえ",)}
# ご承認いただけなかったときの手順の段と、その段で伺うシステムの質問の役目（唯一の表＝Flow._point_step・
# 手順の質問の作り callback_question。⛔ 23時22分41秒のご質問の見直し A9：4か所が役目を手で並べていた）。
# ご説明する段（explain・explained）と申し伝え直す段（told）は、伺う質問が無い
POINT_STAGES = {"ask": "not_agreed", "other": "other_point", "confirm": "agreed_now"}
# 手順の質問の、ご納得いただけた答え（唯一の表＝Flow._point_step が手順を「ご納得」で終える）
POINT_AGREED = {"other_point": "分かった", "agreed_now": "はい"}
# 手順の質問の、ご説明する点を言った答え（唯一の表＝Flow._point_step がご説明の段へ進める・ご説明の前の言い足しを同じ回の点に足す）
POINT_SAID = {"not_agreed": "点を言った", "other_point": "分からない点がある"}
# 選ぶ質問の「どれも要らない」のあとに伺うほかのご希望（Flow._wish_step の1か所）：ご連絡の方法を言っていただいたら、その方法の連絡先をその場で伺う。
# ✅ 2026-11-03 日本時間20時48分 ユーザー：「「LINEならいいです」と回答いただけたら、LINEアドレスを伺うというステップを忘れないように。そうしないと、
# また、電話をかけて伺わないといけなくなりますよ。そうなると、また、繋がらなかったり、考えが変わったりして、無駄な対応やせっかくの顧客を失う可能性が
# 高まります。」。鍵 → (呼び名, その質問の選択肢に同じ方法があるかを見る言葉＝大文字にそろえて含むか, 連絡先を伺うシステムの質問の役目＝無ければ伺わない。
# お電話は、いまかけている番号がある)。申し伝える一言は phrases.WISH_TOLD の同じ鍵。方法の言葉を見分けるのは means_in の1か所。
# オンライン（ZOOM など）は、ご案内の URL をお送りするメールアドレスを伺う（✅ 2026-11-05 日本時間23時19分06秒 ユーザー「案でOK」＝22時49分58秒の案
# 「オンライン（ZOOMなど）は今の方法の一覧に無いので足し、ご案内の URL をお送りするメールアドレスを伺います」）。同じ連絡先を伺うほかの方法より後ろに置く
# （続きの電話の一言は、連絡先の役目を先に持つ方法が役目の一言を使い、後ろの方法は方法の名前の入った一言＝contact_again_key）
WISH_MEANS = {"line": ("LINE", ("LINE", "ライン"), "line_how"), "email": ("メール", ("メール", "MAIL"), "email"),
              "fax": ("ファクス", ("ファクス", "ファックス", "FAX"), "fax"), "post": ("郵送", ("郵送", "郵便"), "address"),
              "phone": ("お電話", ("電話", "TEL"), None),
              "online": ("オンライン", ("オンライン", "ZOOM", "ズーム", "TEAMS", "チームズ", "GOOGLE MEET", "WEB会議", "ウェブ会議", "ビデオ通話"), "email")}
WISH_OTHER, WISH_NONE = "ほかの希望を言った", "無い"
# 連絡先を伺う一言を、こちらで方法をお伝えした一言（phrases.WAY_ANY_ANNOUNCE）のあとに流す形にできる役目（ご連絡の方法の連絡先の役目＝WISH_MEANS から。
# 頭の「それでは」を重ねない＝callback_question の announced・録音 callback_（役目）_bare＝phrases.back_fit）
ANNOUNCED_ROLES = tuple(dict.fromkeys(r for _, _, r in WISH_MEANS.values() if r))
# LINE でのご連絡の連絡先の伺い方（システムの質問 line_how＝C11 の選択肢 → (通話の事実 other_wish の via, 伺う連絡先の役目)。唯一の場所＝Flow._line_how）：
# ID を伺う・SMS でご招待（携帯電話の番号）・メールでご招待（メールアドレス）。伺い済みの連絡先は伺わない（Flow._known_contact）。方法を言っていない
# （「はい」だけ）は1回だけ伺い直す（WAY_UNSAID）。✅ 2026-11-04 日本時間19時26分 ユーザー：「LINE IDでは友達追加できないことが多い。SMSかメールを
# 選ばれたら、もし、携帯電話番号やメールを伺っていなければここで確認。」
LINE_HOW = {"ID": ("id", "line"), "SMS": ("sms", "mobile"), "メール": ("email", "email")}
# LINE の伺い方（C11）の、意味合いを尋ねる一言の候補の言葉（Question.senses。言い方はご承認の「LINEのIDから登録かメールからご招待する方法」にそろえた＝
# 11-05 日本時間1時19分47秒・1時29分06秒）
LINE_HOW_SENSES = {"ID": "LINEのIDから登録で大丈夫", "SMS": "SMSからご招待で大丈夫", "メール": "メールからご招待で大丈夫"}
# システムの質問の、意味合いを尋ねる一言（phrases.sense_ask）の候補の言葉（お客様に言う言葉。役目ごと・callback_question が Question.senses に入れる唯一の表）。
# ご承認いただけなかったときの手順（✅ 2026-11-05 日本時間23時56分48秒「ア」＝当てるのは W1・LINE の伺い方・ご承認の手順）は、目的に進む向き（ご納得）の選択肢
# だけ：遠ざかる向き（断ろうとしている・ご説明した内容でご納得いただけなかった）は、その向きの言葉で言い直さない（11-03 のご判断＝今までどおり聞き直す）。
# 点を言った・分からない点がある・点は言っていないは、中身（どの点か）が要るので入れない。続きの電話の振り返りの一言とそのあとのどの点か（まだ点を1つも
# 伺っていない）は「ほかに」を外す（聞き直しの聞くことと同じ理由＝callback_question）
ROLE_SENSES = {"line_how": LINE_HOW_SENSES,
               "other_point": {POINT_AGREED["other_point"]: "ほかにご納得いただけない点は特に無い"},
               "agreed_now": {POINT_AGREED["agreed_now"]: "ご説明した内容でご納得いただけた"}}
# システムの質問の、受け入れにも断りにも取れる言葉（AMBIGUOUS_WORDS）だけの答えが、その質問でどの選択肢の意味になるか（Question.reply_meanings。業務の質問は
# 取込で質問の文から決める＝meanings.decide_replies。システムの質問は文が決まっているので役目ごとのこの表。callback_question が入れる唯一の表）。
# ✅ 2026-11-06 日本時間18時51分27秒 ユーザー：「こちら「はい、ほかにご納得いただけない点はございますでしょうか？」お客様「大丈夫です。」（「結構です。」でも
# 同じ形）こちら「ありがとうございます。続きまして（もしくは「次に」）ご訪問の日時のご希望はございますでしょうか？」」＝「ほかに…」（N2）への「大丈夫です」
# 「結構です」は「分かった」（ほかに点は無い）。同じ質問への同じ種類の言葉（「いいです」「平気です」）も同じ。⛔ 16時台に入れた形は、意味合いを尋ねる一言
# （ROLE_SENSES）で「大丈夫ですとおっしゃったのは…」と尋ねていた（10-17「前の質問で意味が決まればその意味」に当てていなかった）
# ✅ 2026-11-06 日本時間20時03分09秒 ユーザー：「「結構です。」だけは今のまま尋ねるか、お知らせください。OK」＝「ご説明した内容で…」（N3）への「大丈夫です」は
# 「はい」（ご納得いただけた）・「結構です」は今のまま意味合いを尋ねる（伺ったときの理由：ご納得の問いへの「結構です。」は「もう結構です」というお断りにも
# 聞こえる）。同じ理由が当たる「いいです」（「もういいです」）も尋ねる側・当たらない「平気です」は「大丈夫です」と同じ
ROLE_REPLIES = {"other_point": {w: POINT_AGREED["other_point"] for w in AMBIGUOUS_WORDS},
                "agreed_now": {w: POINT_AGREED["agreed_now"] for w in ("大丈夫", "平気")}}
# 続きの電話の始めのご説明の話し始めの言葉（「前回お伺いした（その点）の件ですが、」の頭。✅ 2026-11-04 日本時間19時26分 ユーザー「(A)…OK」＝Flow._point_note・
# 受け取る所 _explain_problem の1か所）
RESUMED_LEAD = "前回お伺いした"
# 続きの電話の始めに、連絡先を伺う前に切れたご連絡の方法の連絡先を伺い直す役目（resumed_step。WISH_MEANS の連絡先の役目）
CONTACT_ROLES = tuple(dict.fromkeys(r for _, _, r in WISH_MEANS.values() if r))
# ほかのご希望を伺った結果（通話の事実 other_wish の outcome と、前の通話で取れた答えの "wish" の値）：none＝無い・聞き取れない・わからない／option＝元の
# 質問の選択肢を選び直した／means＝ご連絡の方法（連絡先を伺う途中で切れたものも）／other＝ほかのご希望
# unresolved＝W1 に答えをいただけないまま終えた（迷ったあとも決められない・分からない・答えたくない）：断られたとはしない（Flow._goals_lost）。聞き取れないまま
# 締めの前の念のための確認も使い切ったときは、今までどおり元の答えのまま（none＝RemainingNineTest の (4)・⏳ 伺う候補）。
# ✅ 11-01「わからない・答えたくないは断られたとはしない」・11-05 4時48分13秒（もうかけないの印は付けない）。⛔ 2026-11-09 日本時間7時台まで「無い」と同じ none だった
WISH_OUTCOMES = ("none", "option", "means", "other", "unresolved")
# ご承認いただけなかったときの手順（ご納得いただけなかった点を伺う＝Flow._point_step）を、前の通話でどこまで伺えたか（前の通話で取れた答えの "point" の値＝
# earlier_point）：heard＝ご納得いただけなかった点を伺った（ご説明の途中で切れたものも）／agreed・declined・unresolved＝手順の終わり（Flow._point_end）。
# 無ければ、ご納得いただけなかった点の答えをいただく前に切れた＝続きの電話の始めに、切れた所から伺う（resumed_step。✅ 2026-11-04 日本時間18時16分 ユーザー「はい、OK」）
POINT_OUTCOMES = ("heard", "agreed", "declined", "unresolved", "told")
POINT_ENDS = ("agreed", "declined", "unresolved")   # そのうち手順の終わり（伺い終えた）


def earlier_point_open(point: str | None) -> bool:
    """前の通話の答えの手順の進み（earlier_point_of）で、ご承認いただけなかったときの手順をまだ伺い終えていないか（唯一の決まり＝続きの電話の始めの段
    resumed_step・報告の伺い終えていない Flow._unfinished）：手順の終わり（POINT_ENDS）でなければ伺い終えていない（点を伺う前に切れた＝無し・点は伺えた＝
    heard・申し伝える一言まで言って切れた＝told）。⛔ 2026-11-08 日本時間0時台に見つけた：報告の側は「無し・heard」だけを書き、told が抜けていた（始めの段を
    電話の始めに作っていた間は、この電話の手順の進みで答えていたので表に出なかった）"""
    return point not in POINT_ENDS
# 前の通話で取れた答え（Flow の earlier・instruction.Business.earlier。質問の番号 → この項目）の項目（唯一の一覧。CL-2 の conversation_io.earlier が渡す）：
# state・value・condition に加え、wish＝その答えに前の通話で伺ったほかのご希望の結果（earlier_wish。伺っていない・W1 の途中で切れたら無し）・point＝ご承認の
# 質問のお断りに前の通話で伺ったご納得いただけなかった点の進み（earlier_point。伺っていない・N1 の答えをいただく前に切れたら無し）・hedged＝
# 言い切っていない答えのお客様の言葉（依頼者へ渡した Flow.customer_words と同じ。言い切っていれば無し）。CL-1 の 11-03 のご判断の (1)：⛔ 見直しで見つけた＝
# 前の通話でご連絡の方法を言っていただいた最終目的の「どれも要らない」を、続きの通話で「目的が果たせない」として締めていた。point_said＝"point" が heard の
# とき、ご納得いただけなかった点を伺ったお客様の言葉（earlier_point_said。続きの電話の始めに、点を伺い直さずにご説明する＝✅ 2026-11-04 日本時間19時26分
# ユーザー「(A)…OK」）。wish_means＝"wish" が means で、その方法の連絡先を伺う前に切れたとき、その方法（earlier_wish_means。続きの電話の始めに連絡先を
# 伺う＝✅ 同じ 19時26分「(B)…修正：…前回、選択いただいたLINEでのご連絡の件で…」）
# ⇒ 【2026-11-05 CL-2 の連絡帳 4-212 の (B) と案2 で作り直した】wish・wish_means・wish_contact・wish_via・point・point_said を CL-2 と会話の側の2か所で読んでいた
# （CL-2 の call_report.OtherWish・NotAgreed の読み方と、こちらの earlier_*）。前の通話で伺えた連絡先の方法を wish_means（連絡先を伺う前に切れた方法＝連絡先の
# ある答えではいつも空）から読んでいて、続きの電話で、もう伺った連絡先を伺い直していた。⇒ 前の通話の答えには、その答えを取った通話の、その質問の最後の
# 事実そのもの（wish_fact＝通話の事実 other_wish の1つ・point_fact＝not_agreed の1つ。無ければ入れない）を渡していただき、読み方は会話の側の earlier_* の
# 1か所だけ（earlier_wish_of・earlier_means_of・earlier_point_of・earlier_said_of・earlier_records）
# "who"（CL-2 の 4-320＝4-290 の (5)・CL-1.md 4-106 の H6）：その答えを取った通話の、いちばん新しい繋いだ方（target・successor・substitute）。繋いだ事実の
#    無い通話の答えには無い・who の無い前の形は target と読む（読むのは CL-2 の CallReport.connected_who の1か所）。⏳ 続きの電話で判断いただける代わりの方に
#    「引き継がれている部分から」伺う（本書 9-4 の「お客様の申し出」）の見分けに使う＝読み方は CL-1.md 4-120
EARLIER_KEYS = ("state", "value", "condition", "hedged", "wish_fact", "point_fact", "who")


def earlier_fact(entry: dict | None, key: str) -> dict | None:
    """前の通話の答え（EARLIER_KEYS の形）から、その質問の最後の事実（key＝wish_fact・point_fact。無ければ None）を取り出す唯一の場所。"""
    f = (entry or {}).get(key)
    return f if isinstance(f, dict) else None


def earlier_wish_of(entry: dict | None) -> str | None:
    """前の通話で取れた答えに伺ったほかのご希望の結果（WISH_OUTCOMES の1つ。W1 の答えをいただく前に切れた・伺っていなければ None）＝earlier_wish の読み方。"""
    f = earlier_fact(entry, "wish_fact")
    return earlier_wish(f) if f is not None else None


def earlier_means_of(entry: dict | None) -> str | None:
    """前の通話で、ご連絡の方法までは伺えて、その連絡先を伺う前に切れた方法（無ければ None）＝earlier_wish_means の読み方。"""
    f = earlier_fact(entry, "wish_fact")
    return earlier_wish_means(f) if f is not None else None


def earlier_point_of(entry: dict | None) -> str | None:
    """前の通話でご承認いただけなかったときの手順をどこまで伺えたか（POINT_OUTCOMES の1つ。点の答えをいただく前に切れた・伺っていなければ None）＝
    earlier_point の読み方。"""
    f = earlier_fact(entry, "point_fact")
    return earlier_point(f) if f is not None else None


def earlier_said_of(entry: dict | None) -> str | None:
    """前の通話でご納得いただけなかった点を伺ったお客様の言葉（手順を伺い終えていれば None）＝earlier_point_said の読み方。"""
    f = earlier_fact(entry, "point_fact")
    return earlier_point_said(f) if f is not None else None


def earlier_wish(entry: dict) -> str | None:
    """通話の事実 other_wish の1つ（その質問に伺った最後のもの）から、ほかのご希望の結果（WISH_OUTCOMES の1つ。前の通話で取れた答えの "wish" に入れる値。
    W1 の答えをいただく前に切れたら None＝次の通話で伺う）を決める唯一の読み方：結果があればそれ・連絡先を伺う途中で切れた（ご連絡の方法を言って
    いただいた）なら means。流れの制御もこの読み方で決める（Flow._wish_outcome）。"""
    return entry.get("outcome") or ("means" if entry.get("means") else None)


def earlier_wish_means(entry: dict) -> str | None:
    """通話の事実 other_wish の1つ（その質問に伺った最後のもの）から、前の通話の答えの "wish_means"（ご連絡の方法までは伺えて、その連絡先を伺う前に
    切れた方法＝WISH_MEANS の鍵。無ければ None）を決める唯一の読み方（CL-2 の conversation_io.earlier が入れる）：結果が無い（伺い終えていない）・方法が
    あり、その方法に伺う連絡先がある・連絡先が無い。続きの電話の始めに、その連絡先を伺う（resumed_step）。"""
    means = entry.get("means")
    if entry.get("outcome") is not None or means not in WISH_MEANS or not WISH_MEANS[means][2] or entry.get("contact"):
        return None
    return means


def earlier_point(entry: dict) -> str:
    """通話の事実 not_agreed の1つ（その質問の最後のもの）から、ご承認いただけなかったときの手順の進み（POINT_OUTCOMES の1つ。前の通話で取れた答えの "point" に
    入れる値＝CL-2 の conversation_io.earlier）を決める唯一の読み方：手順の終わりの結果があればそれ・無ければ heard（ご納得いただけなかった点は伺った）。
    not_agreed の事実が1つも無い質問には "point" を入れない（ご納得いただけなかった点の答えをいただく前に切れた＝次の通話で伺う）。手順を終えたときは、点を
    伺っていなくても事実が残る（Flow._point_end）。手順の終わりが無く、ご説明できなかった（explained が False＝ご説明できない点の一言＝申し伝える一言まで
    言った）なら told（お客様のお返事の前に切れた＝続きの電話は、お詫びのあとに申し伝える一言を言い直す＝resumed_step。✅ 2026-11-06 日本時間9時05分17秒
    ユーザーの (b)。⛔ それまでは heard と読み、次の電話で同じ申し伝える一言をくり返した＝5時56分55秒のご質問の読み直し EN-D の指摘7）。"""
    return entry.get("outcome") or ("told" if entry.get("explained") is False else "heard")


def earlier_point_said(entry: dict) -> str | None:
    """通話の事実 not_agreed の1つ（その質問の最後のもの）から、前の通話の答えの "point_said"（ご納得いただけなかった点を伺ったお客様の言葉。無ければ点の値）を
    決める唯一の読み方（CL-2 の conversation_io.earlier が入れる）。手順を終えていれば（earlier_point が heard でない）無し＝続きの電話でご説明しない。"""
    if earlier_point(entry) != "heard":
        return None
    return (entry.get("said") or "").strip() or entry.get("point") or None


def means_in(text: str) -> list[str]:
    """文（選択肢の名前・業務設定のご案内できる方法の名前）に言葉の入っているご連絡の方法（WISH_MEANS の鍵。並びのまま）を見分ける唯一の場所（同じ方法か＝
    _wish_same_means・申し出る方法＝_offered_ways）。大文字にそろえて、方法の言葉が入っているかで見る。"""
    t = unicodedata.normalize("NFKC", text).upper()
    found = set()
    for w, k in sorted(((w, k) for k, (_, ws, _) in WISH_MEANS.items() for w in ws), key=lambda x: -len(x[0])):
        if w in t:
            # 長い言葉で見分けた所は、短い言葉で読み直さない（「オンライン」の中の「ライン」を LINE と読まない）。⛔ 2026-11-09 日本時間5時台（4時36分49秒の
            # 「オンライン説明」を入れる途中）に見つけた：「オンライン」を LINE とオンラインの2つに読み、ご案内の方法の申し出（_offered_ways）から落としていた
            found.add(k)
            t = t.replace(w, "\0")
    return [k for k in WISH_MEANS if k in found]


def contact_again_key(means: str) -> str:
    """ご連絡の方法 means の連絡先を伺う前に切れた電話の続きで、切れた所から伺う一言の鍵（phrases.CALLBACK_ASK_RESUMED・録音 callback_（鍵）_again の唯一の
    決め方）：その連絡先の役目を先に持つ方法は役目の名前（今までの一言「前回選択いただいたメールでのご連絡の件で…」）、同じ役目を後ろの方法も使うときは
    「役目_方法」（「前回選択いただいたオンラインでのご案内の件で…」。⛔ 役目の一言のままだと、オンラインを選んだ方に「メールでのご連絡の件で」と言う）。"""
    role = WISH_MEANS[means][2]
    owner = next(k for k, v in WISH_MEANS.items() if v[2] == role)
    return role if owner == means else f"{role}_{means}"


def wish_label(means: str) -> str:
    """ほかのご希望を伺う質問の、ご連絡の方法の選択肢の名前（「LINEでの連絡」）。"""
    return f"{WISH_MEANS[means][0]}での連絡"


def other_means(taken) -> dict:
    """業務の選択肢・方法に無いご連絡の方法を、選べるものに足す唯一の場所（ほかのご希望 W1＝_wish_choices・申し出の方法を選んでいただく N5＝callback_question の
    handoff_way）：{名前（wish_label）: 意味の言葉}。taken＝業務の選択肢・方法に同じ方法がある方法（足さない＝その選択肢・方法を選んだことになる）。選ばれたら、
    その方法の連絡先を伺い（_wish_contact）、担当の者に申し伝えて次へ（WISH_TOLD。業務に無い約束はしない）。✅ 2026-11-03 日本時間20時48分 ユーザー：「「LINEなら
    いいです」と回答いただけたら、LINEアドレスを伺うというステップを忘れないように。そうしないと、また、電話をかけて伺わないといけなくなりますよ。」⛔ 2026-11-09
    日本時間8時台（まとめ役の見直し (E)）まで N5 は業務の方法しか選べず、「オンラインしか無理なんですよ。」のあとも「訪問とお電話のどちらが…」と伺っていた"""
    taken = set(taken)
    return {wish_label(k): f"{WISH_MEANS[k][0]}での連絡を望んでいるということ" for k in WISH_MEANS if k not in taken}


def may_wish(q: "Question") -> bool:
    """この質問の答えのあとに、ほかのご希望を伺いうるか（唯一の決まり＝伺う所 Flow._wish_due・会話AIへの宣言 tool_declarations・録音の表
    make_ack_clips.script_table）：業務の選ぶ質問で「どれも要らない」のあるもの・最終目的の質問でお断りの答えのあるもの（はい／いいえも）。
    ご承認の質問（ご納得いただけなかった点を伺う＝Flow._point_step）・システムの質問・名前の読みの確かめは伺わない。"""
    if q.callback or q.approval or q.reading_of or q.type not in CHOICE_TYPES:
        return False
    return NONE in q.all_conditions or (q.final_goal and any(q.declined(c) for c in q.conditions))


def business_next(questions, qid: str, cond: str | None, hedged: bool) -> list["Question"]:
    """業務設定の前の答えで伺う質問（Question.ask_if）のうち、質問 qid の答え cond（言い切っていない答えか hedged）に当たるもの（トークスクリプトの順）。
    「業務の次の段がこの答えに当たるか」の唯一の決まり（流れの制御は Flow._business_next から・続きの電話の始めは wish_trigger から）。言い切っていない
    ときだけ伺う質問（ask_if_hedged）は、言い切っていない答えにだけ当たる。"""
    return [x for x in questions if x.ask_if and x.ask_if[0] == qid and cond in x.ask_if[1] and (hedged or not x.ask_if_hedged)]


def inserted(order, after: str, qid: str) -> list[str]:
    """並び order の質問 after のすぐあとに qid を入れた並び（唯一の書き方＝Flow._insert_after・follow_ups_in）。qid がもう並びにあれば、その位置から
    動かす。after が並びに無ければ、qid の今の位置（無ければ終わり）。"""
    rest = [x for x in order if x != qid]
    at = rest.index(after) + 1 if after in rest else (list(order).index(qid) if qid in order else len(rest))
    return [*rest[:at], qid, *rest[at:]]


def first_in(order, qid: str) -> list[str]:
    """並び order の先頭に qid を入れた並び（唯一の書き方＝Flow._put_first。続きの電話の始めに切れた所から伺う質問・ご承認の手順の「ほかに…」の先頭）。"""
    return [qid, *(x for x in order if x != qid)]


def follow_ups_in(order, questions, read) -> list[str]:
    """前の質問の答えで伺うかが決まる質問（Question.ask_if）を並びに入れた並び（唯一の決まり＝流れの制御 Flow._ask_if_follow_ups・名乗りのあとに最初に
    伺う質問 first_open）：まだ伺っていない（read の状態が empty）質問で、前の質問の答えが取れて（TAKEN）その答えに当たる（business_next）ものを、前の
    質問のすぐあとに入れる。同じ前の質問で伺う質問は、トークスクリプトの順に続けて入れる。read(qid) →（状態, 答えの中の名前, 言い切っていないか）。
    ⛔ 2026-11-02（CL-2 の 4-185 の (2)）まで、どれも前の質問のすぐあとに入れ、2つ以上あると逆の順になった。⛔ 2026-11-04 まで、名乗りのあとに最初に
    伺う質問（instruction.Business.first_question）は伺う条件を見ずに探していた（前の通話の答えで伺わない質問を、名乗りの録音の最後に流しうる）。"""
    questions, order, last = list(questions), list(order), {}
    for q in questions:
        if q.ask_if and q.id not in order and read(q.id)[0] == "empty":
            src = q.ask_if[0]
            state, cond, hedged = read(src)
            if state in TAKEN and q.id in {x.id for x in business_next(questions, src, cond, hedged)}:
                order = inserted(order, last.get(src, src), q.id)
                last[src] = q.id
    return order


def gate_of(questions, order, read) -> tuple[str, bool] | None:
    """先へ進むのを止めている業務の質問（並びの順で最初の1つ）と、止める理由（True＝ここで終える答え Question.ends_on・False＝答えが決まらない＝まだ
    伺っていない・聞き取れない・わからない・答えたくない）。無ければ None。唯一の決まり（伺わない質問 held・残りの質問 Flow._remaining・名乗りのあとに
    最初に伺う質問 first_open・会話の報告 Flow.ended_on が、ここだけで決める）。答えが決まらないうちに先へ進まないのは、その質問の答えで、そのあとの
    質問を伺うかが決まるため（✅ 2026-11-04 日本時間1時25分39秒 ユーザー：「（本人or後任or代理がいると聞き取れない）のに次に進むのは問題でしょ？」）。
    「どちらでも」「どれも要らない」はお客様の答え（終える答えに入っていなければ先へ進む）。"""
    by_id = {q.id: q for q in questions}
    for qid in order:
        q = by_id.get(qid)
        if q is None or not q.ends_on:
            continue
        state, cond, _ = read(qid)
        if state not in GOT:
            return qid, False
        if cond in q.ends_on:
            return qid, True
    return None


def held(questions, order, read) -> list[str]:
    """先へ進むのを止めている質問（gate_of）より並びであとの、業務の質問（伺わない質問。唯一の決まり）。その質問の答えで伺う質問（ask_if をたどると、
    止めている質問に着くもの）は、その答えのトークスクリプトの続きなので伺う（「お電話」なら、お電話のご都合のよい時間帯を伺って終える、など。
    ⛔ お呼びする方がいらっしゃるかは業務の質問ではない＝お呼びする段で、いらっしゃらなければ、いつならいらっしゃるかを伺う＝CALLBACK_PLAN。
    ✅ 2026-11-04 日本時間3時15分47秒・3時19分02秒 ユーザー）。システムの質問（ほかの
    ご希望・かけ直しの質問など）は止めない。"""
    g = gate_of(questions, order, read)
    if g is None:
        return []
    by_id, stop = {q.id: q for q in questions}, g[0]

    def branch(q) -> bool:
        seen = set()
        while q.ask_if and q.ask_if[0] not in seen:
            if q.ask_if[0] == stop:
                return True
            seen.add(q.ask_if[0])
            if (q := by_id.get(q.ask_if[0])) is None:
                return False
        return False
    after = list(order)[list(order).index(stop) + 1:]
    return [qid for qid in after if not by_id[qid].callback and not by_id[qid].reading_of and not branch(by_id[qid])]


def earlier_kept(questions, earlier: dict | None) -> dict:
    """前の通話の答え（CL-2 の conversation_io.earlier）のうち、この通話でもう伺わない答え（唯一の決まり＝Flow の earlier・first_open・resumed_step）：
    取れた答え（TAKEN）。ただし、ここで終える答えのある質問（Question.ends_on）の「わからない」は、もう一度伺う（その答えが決まらないと、そのあとの業務の
    質問を1つも伺えない＝続きの電話が何も伺わずに終わる＝お客様に要らない電話になる。CL-1 が 2026-11-04 に見つけた）。「答えたくない」は二度と伺わない
    （10-13 の決まりのまま）。"""
    by_id = {q.id: q for q in questions}
    return {qid: dict(v) for qid, v in (earlier or {}).items() if qid in by_id and (v or {}).get("state") in TAKEN
            and not (by_id[qid].ends_on and v["state"] == "dont_know")}


def earlier_read(earlier: dict | None, qid: str) -> tuple:
    """前の通話で取れた答え（instruction.Business.earlier・Flow の earlier。CL-2 の conversation_io.earlier）を、流れの制御と同じ形で読む唯一の場所：
    （状態, 答えの中の名前, 言い切っていないか）。取れた答えでなければ（"empty", None, False）。答えの名前は依頼者へ渡す日本語の名前＝中の名前に言い換える
    （condition。⛔ 2026-11-02 まで、そのまま入れていて、伺う条件・断る答えの見分けが「わからない」を unknown と見なかった＝CL-2 の連絡帳 4-179 の (b)）。"""
    v = (earlier or {}).get(qid) or {}
    if v.get("state") not in TAKEN:
        return "empty", None, False
    return v["state"], condition(v["condition"]) if v.get("condition") else None, earlier_hedged(v) is not None


def earlier_hedged(v: dict | None) -> str | None:
    """前の通話の答え1つ（EARLIER_KEYS の形）の、言い切っていない返事のお客様の言葉を読む唯一の決まり（電話の始めの決まり earlier_read・流れの制御の前の答えの
    写し Flow.__init__）：取れた答え（GOT）のときだけ。依頼者へ渡すお客様の言葉（Flow.customer_words）は「-」「✕」の答えにも添えるので、同じ欄で戻ってきても、
    取れていない答えを言い切っていない答えとして読まない（✅ 2026-11-08 日本時間3時48分03秒「お客様の言葉で簡潔に渡せるように！」で欄を広げたときに分けた）。"""
    v = v or {}
    return (v.get("hedged") or None) if v.get("state") in GOT else None


def first_open(questions, earlier: dict | None) -> "Question | None":
    """電話の始めに伺う業務の質問（前の通話で取れた答えの質問は飛ばす。無ければ None）。唯一の決まり＝流れの制御の始め Flow.__init__・名乗りのあとに
    最初に伺うこと instruction.Business.first_question：伺う条件の質問は、前の通話の答えで当たるときだけ（follow_ups_in）・先へ進むのを止める答えの
    あとの質問は伺わない（held）。続きの電話で切れた所から伺う手順（resumed_step）は、この前に決まる（システムの質問）。"""
    qs = [q for q in questions if not q.callback]
    read = functools.partial(earlier_read, earlier_kept(qs, earlier))
    order = follow_ups_in([q.id for q in qs if not q.ask_if], qs, read)
    hold, by_id = set(held(qs, order, read)), {q.id: q for q in qs}
    return next((by_id[qid] for qid in order if read(qid)[0] == "empty" and qid not in hold), None)


def wish_trigger(q: "Question", state: str, cond: str | None, hedged: bool, questions) -> bool:
    """この答え（状態 state・答え cond・言い切っていないか hedged）が、ほかのご希望を伺う答えか（伺ったか・いつ伺うかは見ない。唯一の決まり＝
    Flow._wish_trigger・続きの電話の始め resumed_step）：選ぶ質問の「どれも要らない」・最終目的の質問への言い切っていないお断り（業務設定に
    その答えのあとに伺う質問があれば、業務の言葉が先＝伺う答えではない）。"""
    if not may_wish(q) or state not in TAKEN or business_next(questions, q.id, cond, hedged):
        return False
    return cond == NONE or (q.final_goal and state == "accepted" and hedged and q.declined(cond))


def contact_role(means: str | None, via: str | None) -> str | None:
    """ご連絡の方法 means（と LINE の伺い方 via）で伺った連絡先が、どの連絡先の役目のものか（唯一の場所＝known_contact）：LINE は伺い方で決まる
    （ID＝line・SMS＝mobile・メール＝email）。お電話は連絡先が無い（None）。"""
    role = WISH_MEANS[means][2] if means in WISH_MEANS else None
    if role == "line_how":
        return next((r for v, r in LINE_HOW.values() if v == via), None)
    return role


# 業務の質問の答えのうち、連絡先の役目の連絡先として読める型（メールアドレス・携帯電話の番号・ご住所。known_contact の1か所）
_KNOWN_TYPES = {"email": ("email",), "mobile": ("phone", "mobile"), "address": ("address",)}


def known_contact(role: str, answers, records) -> "tuple[str, str | None] | None":
    """連絡先を伺う質問（役目 role）の答えが、この通話か前の通話でもう取れているか（取れていれば (連絡先, LINE の伺い方 via)＝伺わない。無ければ None）を
    決める唯一の決まり（流れの制御 Flow._known_contact・続きの電話の始めに伺う手順 resumed_step＝名乗りの録音の最後とAIへの指示文も）：
    - answers：質問の答え (質問, 状態, 値)。その質問の答え・メールアドレスはメールの型・携帯電話の番号は電話番号の型のうち携帯電話の番号のもの・ご住所は住所の型
    - records：ほかのご希望で伺った連絡先 {"means", "via", "contact"}（この通話の通話の事実 other_wish・前の通話の答えの wish_means・wish_via・wish_contact＝
      earlier_records）。LINE の伺い方（line_how）は、LINE の連絡先があれば（伺い方ごと）伺わない
    ✅ 2026-11-04 日本時間19時26分 ユーザー：「SMSかメールを選ばれたら、もし、携帯電話番号やメールを伺っていなければここで確認。」・9-3「取得済みのことを重ねて
    聞かない」。⛔ 2026-11-04 日本時間21時台に見つけた：決める所が2つあり（名乗りの組み立てと流れの制御）、食い違うと名乗りのあとで止まった。郵送のご住所・
    前の通話のほかのご希望の連絡先を見ていなかった"""
    records = [r for r in records if (r.get("contact") or "").strip()]
    if role == "line_how":
        r = next((r for r in reversed(records) if r.get("means") == "line"), None)
        return (r["contact"], r.get("via")) if r is not None else None
    did = CALLBACK_IDS[ROLE_KEY[role]]
    for q, state, value in answers:
        if state not in GOT or not value or not (q.id == did or q.type in _KNOWN_TYPES.get(role, ())):
            continue
        if role == "mobile" and not is_mobile(value):
            continue
        return value, None
    r = next((r for r in reversed(records) if contact_role(r.get("means"), r.get("via")) == role), None)
    return (r["contact"], None) if r is not None else None


def earlier_records(earlier: dict | None) -> list[dict]:
    """前の通話の答えにある、ほかのご希望で伺った連絡先（known_contact の records の形＝通話の事実 other_wish そのもの）：前の通話の答えの wish_fact のうち、
    連絡先のあるもの。⛔ 2026-11-05 CL-2 の連絡帳 4-212 の (B)：前は連絡先の方法を wish_means（連絡先を伺う前に切れた方法）から読み、連絡先のある答えでは
    いつも空＝もう伺った連絡先を伺い直していた。"""
    return [f for v in (earlier or {}).values() if (f := earlier_fact(v, "wish_fact")) is not None and (f.get("contact") or "").strip()]


def resumed_step(questions, earlier: dict | None) -> "tuple[str, Question] | None":
    """続きの電話の始めに、切れた所から伺う手順（(システムの質問の役目, 元の質問)。無ければ None）を決める唯一の場所（流れの制御の始め Flow.__init__・
    名乗りのあとに最初に伺う質問 opening_question）：前の通話で取れた答えのうち、そのあとに伺う手順を伺い終えていない答え（トークスクリプトの順で最初の1つ）。
    - ほかのご希望（other_wish）：前の通話の答えに "wish" が無い、ほかのご希望を伺う答え（wish_trigger）。✅ 2026-11-03 日本時間23時52分36秒 ユーザー「(ア)」
    - ご納得いただけなかった点（not_agreed）：前の通話の答えに "point" が無い、ご承認の質問のお断り（Flow._point_step と同じ決まり＝Question.declined）。
      ✅ 2026-11-04 日本時間18時16分 ユーザー「はい、OK」（ほかのご希望と同じく、お詫びのすぐあとに「前回お伺いした件でご納得いただけなかった点は…」）
    - ご連絡の方法の連絡先（WISH_MEANS の連絡先の役目＝CONTACT_ROLES）：前の通話の答えの "wish" が means で、"wish_means"（連絡先を伺う前に切れた方法）が
      ある。✅ 2026-11-04 日本時間19時26分 ユーザー（(B) のご修正）：「前回、選択いただいたLINEでのご連絡の件でLINEの連絡先を伺う前に切れてしまいましたので…」
    - ご説明（explain）：前の通話の答えの "point" が heard（ご納得いただけなかった点は伺えて、ご説明の途中で切れた）。点を伺い直さずに、お詫び（＋用件の一文）の
      すぐあとにAIがその点をご説明する（Flow._point_begin の said。システムの質問は無い＝名乗りの録音の最後に質問を流さない）。✅ 2026-11-04 日本時間19時26分
      ユーザー：「(A)…お勧めは、点を伺い直さずに、ご説明から始める形：OK」。点の言葉（"point_said"）が渡っていなければ、ご説明できる点が無いので、
      ご納得いただけなかった点（not_agreed）を伺い直す
    前の通話の答えの読み方は Flow の earlier と同じ（earlier_read の1か所）。続きの電話の途中では、どちらの手順も前の通話の答えから始めない（_wish_due・_point_step）。"""
    questions = list(questions)
    kept = earlier_kept(questions, earlier)
    answers = [(q, earlier_read(kept, q.id)[0], (kept.get(q.id) or {}).get("value")) for q in questions]
    for q in questions:
        state, cond, hedged = earlier_read(kept, q.id)
        if state not in TAKEN:
            continue
        entry = (earlier or {}).get(q.id) or {}
        if earlier_wish_of(entry) is None and wish_trigger(q, state, cond, hedged, questions):
            return "other_wish", q
        if ((means := earlier_means_of(entry)) in WISH_MEANS and (role := WISH_MEANS[means][2])
                and known_contact(role, answers, earlier_records(earlier)) is None):
            # ご連絡の方法までは伺えて、その連絡先を伺う前に切れた（earlier_wish_means）：切れた所から連絡先を伺う（その方法の連絡先の役目）。
            # もう取れている連絡先は伺わない（known_contact の1か所＝流れの制御の _wish_contact と同じ決まり）
            return role, q
        if q.approval and state in GOT and q.declined(cond) and earlier_point_open(point := earlier_point_of(entry)):
            if point == "told":
                # 申し伝える一言まで言って、お返事の前に切れた：お詫びのすぐあとに申し伝える一言を言い直す（伺う質問は無い＝Flow._sorry_lines）
                return "told", q
            if point == "heard" and (earlier_said_of(entry) or "").strip():
                return "explain", q
            # 点を伺う前に切れた・点は伺えたがその言葉が渡っていない（ご説明できる点が無い）：切れた所から点を伺う
            return "not_agreed", q
    return None


def told_kind(entry: dict | None) -> str:
    """前の電話で申し伝える一言まで言って切れた（resumed_step の told）ときの、申し伝える一言の種類（ご質問・ご要望・ご意見＝POINT_KINDS。無ければご質問）を
    前の通話の答えから読む唯一の場所（流れの制御 Flow._point_told・名乗りの録音の文 instruction.Business.opening_parts）"""
    f = earlier_fact(entry or {}, "point_fact") or {}
    return f.get("told") if f.get("told") in POINT_KINDS else "question"


# 予約証のお詫びの理由（CL-2 の task_next_actions.apology_reasons の CHECK＝0216 と同じ一覧。比べる試験）と、流す順（唯一の決まり＝apology_order）：前の電話で
# 言ってはいけないことを言ったお詫び（承認したお詫びの文＝CL-2 の apology.for_task の text）→ 前の電話が途中で切れたお詫び → 約束の時間にかけられなかったお詫び
# （前の電話で起きたことから、そのあとのことへ＝いちばん重いお詫びを先に）。⏳ 並べ方は伺う（帳面 4-126）
APOLOGY_REASONS = ("forbidden_speech", "cut_off", "missed_promise")


def apology_order(apology, approved: str | None = None) -> list[str]:
    """予約証のお詫びの理由を、流す順に並べる唯一の場所（お詫びの並び sorry_texts・流れの制御の始め Flow.__init__）。知らない理由と、承認したお詫びの文
    （approved＝instruction.Business.apology_text）の無い forbidden_speech は ValueError（黙って捨てない＝お詫びを流さないまま約束の用件へ進み、伝えたと
    記録されるのを防ぐ）。⛔ 2026-11-09 日本時間8時台（CL-2 の 4-350）まで、知らない理由を黙って捨てていた（Flow.__init__ の apology_due）"""
    unknown = sorted(set(apology) - set(APOLOGY_REASONS))
    if unknown:
        raise ValueError(f"知らないお詫びの理由です（会話の側の一覧は {APOLOGY_REASONS}）：{unknown}")
    if "forbidden_speech" in apology and not (approved or "").strip():
        raise ValueError("お詫びの理由 forbidden_speech には、承認したお詫びの文（apology_text＝CL-2 の apology.for_task の text）が要ります")
    return sorted(dict.fromkeys(apology), key=APOLOGY_REASONS.index)


def sorry_texts(apology, told: str | None, at_end: bool, when: str, follow_up: bool, approved: str | None = None) -> list[tuple[str, str]]:
    """続きの電話の始めに、名乗りのあと流すお詫びと申し伝える一言の並び（(録音の名前, 文)）を作る唯一の場所（流れの制御 Flow._sorry_lines・名乗りの録音の文
    instruction.Business.opening_parts＝会話AIへの「プログラムが流す録音」の知らせ）：予約証の理由ごとのお詫び（phrases.APOLOGY_FIRST）。前の電話で申し伝える
    一言まで言って切れた（told）なら、申し伝え直して締める電話（at_end）の途中で切れたお詫びは、お話の最後に切れたお詫び（APOLOGY_CUT_END。同じ日＝先ほど・
    あとの日＝前回＝when）と、申し伝える一言（TOLD_AGAIN・担当から連絡すると申し出てよい業務は TOLD_FOLLOW_UP）。✅ 2026-11-06 日本時間9時05分17秒 ユーザーの (b)。
    ⛔ 2026-11-08 日本時間8時台（(10) の H2 の残り）まで、名乗りの録音の文は別に並べていて、申し伝え直す電話でも途中で切れたお詫び＋用件の一文だった。
    言ってはいけないことを言ったお詫び（理由 forbidden_speech）は、承認したお詫びの文（approved）を、お詫びだけの電話と同じ流し方（録音の名前 "apology"＝
    承認したときに聞いていただいた、お詫びの案ごとの録音。共通の一言ではない）で流す。並べる順と理由の確かめは apology_order の1か所。✅ CL-2 の 4-350 の (2)"""
    out = [("apology", approved) if k == "forbidden_speech" else
           (f"apology_cut_end_{when}", APOLOGY_CUT_END[when]) if k == "cut_off" and at_end
           else (f"apology_{k}_same_day", APOLOGY_SAME_DAY[k]) if when == "same_day" and k in APOLOGY_SAME_DAY else (f"apology_{k}", APOLOGY_FIRST[k])
           for k in apology_order(apology, approved)]
    if told is not None:
        out.append(("told_follow_up", TOLD_FOLLOW_UP) if follow_up else (f"told_again_{told}_{when}", TOLD_AGAIN[f"{told}_{when}"]))
    return out


def opening_question(questions, earlier: dict | None) -> "Question | None":
    """名乗りのあとに最初に伺う質問（唯一の決まり＝流れの制御の始め Flow.__init__・名乗りの録音の最後とAIへの指示文 instruction.Business.first_question）：
    名前の読みの確かめ（前の通話で確かめていなければ＝first_open）が先・続きの電話で切れた所から伺う手順（resumed_step。ほかのご希望 W1・ご納得いただけ
    なかった点 N1 の again の形）はそのあと・それ以外は first_open。⛔ 2026-11-04 日本時間7時台の4回目のご質問の見直しで見つけた：名乗りの組み立てだけ読みの確かめを見ずにほかの
    ご希望から始め、名乗りの録音の最後に W1 を流しうった（流れの制御は R1 の答えを待つ）。"""
    questions = list(questions)
    first = first_open(questions, earlier)
    if first is not None and first.reading_of:
        return first
    if (step := resumed_step(questions, earlier)) is not None:
        role, q = step
        # ご説明から始める（explain）・申し伝え直す（told）：伺う質問は無い（AIが名乗りの録音のあとにご説明する・お詫びのあとに申し伝える一言を流す＝
        # Flow._opening_reply・_connect）。申し伝え直したあとは、前の通話で伺えなかった質問から（Flow の始めの並び）
        if role == "told":
            # ご承認が最終目的なら、申し伝え直したあとは締める（Flow._advance の not_agreed）＝伺う質問は無い
            return None if q.final_goal else first
        return None if role == "explain" else callback_question(role, of=q if role == "other_wish" else None, again=True,
                                                                means=earlier_means_of((earlier or {}).get(q.id)) if role in CONTACT_ROLES else None)
    return first


def _wish_picks(q: "Question") -> list[str]:
    """ほかのご希望を伺うとき、選び直していただける元の質問の選択肢（お断りの答えでないもの。お断りを選び直すのは「無い」と同じ）。"""
    return [c for c in q.conditions if not q.declined(c)]


def _wish_pick_label(q: "Question", c: str) -> str:
    """W1 の、元の質問の選択肢 c を選び直していただく選択肢の名前（唯一の場所＝選択肢を作る _wish_choices と、元の答えへ戻す wish_repick）：選ぶ質問の
    選択肢（電話・メール）はそのままの名前。返事の言葉だけの名前（はい／いいえ）は、W1 そのものが「はい、あります。」で答えられる問いなので、その名前に
    しない＝「やはり（意味の言葉）」、意味の言葉が無ければ「やはり「はい」」（CL-1 の 11-03 のご判断の (2)。⛔ 見直しで見つけた：「はい、あります。」を
    「はい」と報告すると、お断りが「パンフレットを送ってよい」に変わった）。"""
    if not _BARE_REPLY.fullmatch(c):
        return c
    m = q.meanings.get(c)
    return f"やはり{m}" if m else f"やはり「{c}」"


def wish_repick(q: "Question", cond: str | None) -> str | None:
    """W1 の答え cond が、元の質問 q の選択肢を選び直したものなら、その元の選択肢（無ければ None。W1 の答えを受ける Flow._wish_step が使う）。"""
    return next((c for c in _wish_picks(q) if _wish_pick_label(q, c) == cond), None)


def _wish_same_means(q: "Question", means: str) -> str | None:
    """元の質問の選び直せる選択肢のうち、ご連絡の方法 means（WISH_MEANS の鍵）と同じもの（無ければ None。唯一の場所）。同じ方法が選択肢にあれば、
    その方法のご希望は、その選択肢を選び直した答え（W1 の選択肢にその方法を出さない＝_wish_choices・その方法の答えの名前＝wish_option）。
    見るのは、W1 に出す選び直しの選択肢の名前（_wish_pick_label）：選ぶ質問は選択肢の名前（電話・メール）、はい／いいえは「やはり（意味の言葉）」。
    ⛔ 2026-11-05 日本時間20時台に見つけた：選択肢の名前（「はい」）だけを見ていたので、「担当者から詳しいご案内のお電話を差し上げてもよろしいでしょうか？」の
    W1 に「やはり…お電話を差し上げてよろしいということ」と「お電話での連絡」が並び、「やっぱり電話でいいです」の読みが2つに割れえた"""
    return next((c for c in _wish_picks(q) if means in means_in(_wish_pick_label(q, c))), None)


def _wish_senses(of: "Question") -> dict:
    """ほかのご希望（W1）の選択肢ごとの、意味合いを尋ねる一言の候補の言葉（お客様に言う言葉。W1 を作る callback_question が Question.senses に入れる唯一の
    場所）：元の質問 of の選び直し（選ぶ質問の名詞の選択肢は「（選択肢）で大丈夫」、ほかは元の質問の意味の言葉）・ご連絡の方法（「（方法）で大丈夫」）・
    無い（「ほかにご希望は特に無い」）。ほかの希望は中身が要るので入れない（尋ねずに質問から聞き直す）。"""
    out = {}
    for c in _wish_picks(of):
        m = of.meanings.get(c)
        words = f"{c}で大丈夫" if of.type == "choice" and _listable(c) else (m.removesuffix("ということ") if m else None)
        if words:
            out[_wish_pick_label(of, c)] = words
    out.update({wish_label(k): f"{WISH_MEANS[k][0]}で大丈夫" for k in WISH_MEANS if _wish_same_means(of, k) is None})
    out[WISH_NONE] = SENSE_NONE
    return out


def sense_words(q: "Question", c: str) -> str | None:
    """意味合いを尋ねる一言（phrases.sense_ask）の候補の言葉＝お客様に言う言葉（唯一の読み口。無ければ None＝尋ねずに質問から聞き直す）。言い直さない質問の
    意味の言葉は判定に見せる説明で、お客様に言う言葉ではない（CL-1 の 11-03 のご判断の (4)）ので使わず、作るときに入れたお客様に言う言葉（Question.senses）
    だけを引く：ほかのご希望 W1（_wish_senses）・LINE の伺い方 C11・ご承認の手順（ROLE_SENSES＝ご納得の向きだけ。ほかは今までどおり聞き直す）。"""
    return q.senses.get(c)


def asks_sense(q: "Question", c: str | None) -> bool:
    """言い直さない質問 q に、判定が選択肢 c と読んだ会話がつながらない答えをいただいたとき、聞き直さずに c で意味合いを尋ねるか（唯一の決まり＝流れの
    制御 Flow._check_again・測りの数え g53_meaning.flow_act）。✅ 2026-11-05 日本時間23時56分48秒 ユーザー「(1) 会話1：…候補の選び方：ア」"""
    return not restates(q) and c in q.conditions and sense_words(q, c) is not None


def wish_option(q: "Question", means: str) -> str:
    """元の質問 q の W1 で、ご連絡の方法 means のご希望にあたる選択肢の名前（元の質問に同じ方法の選択肢があれば、それを選び直した名前・無ければ
    「（方法）での連絡」）。W1 を伺っているときのほかの手段の申し出（request_callback）を、W1 の答えとして受ける所（Flow._wish_reported）が使う。"""
    same = _wish_same_means(q, means)
    return _wish_pick_label(q, same) if same is not None else wish_label(means)


def _wish_choices(q: "Question") -> tuple[tuple, dict]:
    """ほかのご希望を伺う質問（W1）の選択肢と意味の言葉（唯一の場所。会話AIへの意味の言葉も、判定の選べるもの＝meaning_judge.options も、ここから）：
    元の質問の選択肢（考え直して、そのどれかを選んだ。お断りの答えは除く。名前は _wish_pick_label）・ご連絡の方法（元の質問の選択肢に同じ方法が無い
    もの。同じ方法が選択肢にあれば、その選択肢を選んだことになる＝元の質問の答え）・ほかのご希望・無い。"""
    picks = _wish_picks(q)
    means = other_means(k for k in WISH_MEANS if _wish_same_means(q, k) is not None)

    def picked(c: str) -> str:
        # 選び直した答えの意味の言葉：選ぶ質問の選択肢も、はい／いいえ（名前は「やはり…」）も、元の質問を考え直したことを言う（W1 は「どれも要らない」・
        # お断りのあとに伺う問い）。⛔ 2026-11-05 日本時間20時台に、W1 の試しの言葉を作って見つけた：選ぶ質問の選択肢には意味の言葉（「ご連絡はお電話」）
        # だけを見せていたので、作り手が「電話はもう勘弁してほしい」のような電話を断る言葉を「電話」として作った（判定・会話AIが読む説明も同じ文）
        m = q.meanings.get(c) or (f"「{c}」を選んだということ" if _wish_pick_label(q, c) == c else f"「{c}」と答え直したということ")
        return f"元の質問を考え直して、{m if m.endswith('ということ') else m + 'ということ'}"
    conds = (*(_wish_pick_label(q, c) for c in picks), *means, WISH_OTHER, WISH_NONE)
    meanings = {**{_wish_pick_label(q, c): picked(c) for c in picks}, **means,
                WISH_OTHER: "上のどれでもない、ほかのご希望を言ったということ", WISH_NONE: "ほかに希望は無い・やはり要らないということ"}
    if len(way_offer(q) or ()) > 1:
        # ご案内の方法を2つから申し出たとき：受けていただいたが、どの方法かは言っていない（「はい、お願いします。」）＝1回だけ方法を並べて伺う（Flow._wish_step）。
        # 1つなら「はい」でその方法に決まる・申し出る方法が無ければ申し出ではない（並べて伺えない）。✅ 2026-11-06 日本時間9時05分17秒 ユーザー
        conds += (WAY_UNSAID,)
        meanings[WAY_UNSAID] = "申し出た方法でのご案内を受けると言ったが、どの方法かは言っていないということ"
    return conds, meanings


# 名詞の形でも、ありなしを述べる言葉（単語分けの辞書 unidic の見出し語。「変更なし」「同席あり」「予約済み」＝「（もの）が無い／ある／済んだ」と述べる言葉で、
# 「それでは（選択肢）のほかにご希望は…」に並べられない＝_listable）
_STATE_LEMMAS = ("有り", "無し", "済み")


def _listable(text: str) -> bool:
    """選択肢の名前が、「それでは（選択肢）のほかにご希望はございますでしょうか？」に並べられる名詞の言葉か（唯一の決まり。単語分けの辞書＝jtext.words の
    1か所で決める）：終わりの単語が名詞（名詞的な接尾辞「今月中」の「中」も）で、ありなしを述べる言葉（_STATE_LEMMAS）でない。「参加する」（動詞）・
    「変更なし」は並べない（CL-1 の 11-03 のご判断の (5)。⛔ 見直しで見つけた：「それでは参加するのほかに…」「それでは変更なしと変更ありのほかに…」）。"""
    from jtext import words
    ws = [w for w in words(text) if w.feature.pos1 not in ("補助記号", "空白")]
    if not ws:
        return False
    f = ws[-1].feature
    return (f.pos1 == "名詞" or (f.pos1 == "接尾辞" and f.pos2 == "名詞的")) and getattr(f, "lemma", "") not in _STATE_LEMMAS


def _offered_ways(q: "Question") -> list[str]:
    """ご案内の方法を伺う質問 q の W1 で申し出る方法の名前（業務設定の名前のまま・並びのまま。唯一の場所＝_wish_text）：ご連絡の方法に当たる名前（means_in が
    1つに決まるもの）のうち、断られた方法（元の質問の選び直しと同じ方法＝_wish_same_means）でないもの。同じ方法の名前が2つあれば初めのものだけ。
    方法に当たらない名前は言わない（その方法の連絡先を伺えない＝取込で確かめる＝CL-2）。"""
    out, seen = [], set()
    for name in q.offer_ways:
        ms = means_in(name)
        if len(ms) == 1 and ms[0] not in seen and _wish_same_means(q, ms[0]) is None:
            seen.add(ms[0])
            out.append(name)
    return out


def line_play(rid: str, text: str) -> dict:
    """返事のあとに流す一言の録音の種類を、録音の名前から決める唯一の場所（Flow._advance の業務の一言・保留の一言・お伝えする一言 way_any_line）：
    どの業務でも同じ一言（共通の一言の表 RECORDED にある）は recorded、業務ごとの一言は依頼主の単位の録音（question）。⛔ 2026-11-08 日本時間7時台
    （11-07 23時26分13秒のご質問の見直しの B-2）：お伝えする一言（（番号）_other_any）だけ recorded に決め打ちしていた"""
    return {"kind": "recorded", "id": rid, "text": text} if rid in RECORDED else {"kind": "question", "id": rid, "text": text, "lead": ""}


def way_any_line(q: "Question") -> "tuple[str, str] | None":
    """申し出た方法を並べて伺ったあと（W1 の which）に「どれでも」と言われたとき、業務設定で最初に並ぶ方法をこちらでお伝えする一言（唯一の場所＝流す
    Flow._advance・録音の表 make_ack_clips.script_table）：(依頼主の単位の録音の名前（番号）_other_any, 文)。並べて伺えない質問は None。
    ✅ 2026-11-07 日本時間3時14分01秒 ユーザー「(2) H3…イ」"""
    ways = way_offer(q) or []
    return (f"{q.id}_other_any", WAY_ANY_ANNOUNCE.format(w=ways[0])) if len(ways) > 1 else None


def way_offer(q: "Question") -> "list[str] | None":
    """元の質問 q の W1 が、ご案内の方法を申し出る形か（唯一の決まり＝文 _wish_text・選択肢 _wish_choices・伺い直しの質問 callback_question・録音の表
    make_ack_clips.script_table）：はい／いいえで伺うご案内の方法の質問（Question.asks_way）なら、申し出る方法の名前（_offered_ways。無ければ []＝連絡の方法
    だと分かる形で伺う）。申し出の形でなければ None（選ぶ質問は今までどおり選択肢を並べる）。"""
    return _offered_ways(q) if q.asks_way and q.type != "choice" else None


def _wish_text(q: "Question", again: bool = False, which: bool = False) -> str:
    """ほかのご希望を伺う文（唯一の場所＝W1 の質問 callback_question と、録音の表 make_ack_clips.script_table が同じ文を使う）：選ぶ質問で、選び直して
    いただける選択肢（目的から遠ざかる向き・最終目的のお断りの答えは並べない＝Question.away_answers）がどれも名詞の言葉（_listable）なら、並べる
    （「それではお電話とメールのほかにご希望はございますでしょうか？」）。並べられなければ「それではほかにご希望などはございますでしょうか？」（はい／いいえ
    と同じ。✅ 2026-11-03 日本時間21時38分の言い方）。again＝伺っている途中で切れた電話の続き（お詫びのすぐあと）で、切れた所から伺う一言（「前回お伺いした
    （聞くこと）の件で（選択肢）のほかに…」。並べるかは同じ決まり。✅ 2026-11-03 日本時間23時52分36秒 ユーザー「(ア)」）。"""
    # 聞くことの頭の「ご希望の」は言わない（「前回お伺いしたご希望のお支払い方法の件で…ほかにご希望は…」と、1つの文に「ご希望」を2回言わないため。
    # 20時48分のご承認の言い方を選択肢で伺う形にそろえたのと同じ理由＝phrases.OTHER_WISH_ASK の注）
    topic = q.topic.removeprefix("ご希望の")
    t = f"{topic}の" if topic else ""
    if (ways := way_offer(q)) is not None:
        # はい／いいえで伺う、ご案内の方法を伺う質問（Question.asks_way）：業務でご案内できる方法を申し出る（1つなら「メールで」・2つから「メールやLINE、ZOOMなどで」）。
        # 申し出る方法が無ければ、連絡の方法だと分かる形で伺う（✅ 2026-11-05 日本時間23時19分06秒 ユーザー「案でOK　(2)ア」）。選ぶ質問は今までどおり選択肢を並べる。
        # which＝申し出に、どの方法かは言わずに受けていただいたときの1回の伺い直し（「それではメールとLINEとZOOMのどちらがよろしいでしょうか？」＝Flow._wish_step）
        if which:
            return OTHER_WAYS_WHICH.format(w="と".join(ways))
        w = ways[0] if len(ways) == 1 else f"{ways[0]}や{'、'.join(ways[1:])}など" if ways else ""
        if again:
            return OTHER_WAYS_OFFER_AGAIN.format(t=t, w=w) if w else OTHER_WAYS_ASK_AGAIN.format(t=t)
        return OTHER_WAYS_OFFER.format(w=w) if w else OTHER_WAYS_ASK
    listed = [c for c in _wish_picks(q) if c not in q.away_answers] if q.type == "choice" else []
    listable = bool(listed) and all(map(_listable, listed))
    if again:
        return OTHER_WISH_AGAIN.format(t=t, c="と".join(listed)) if listable else OTHER_WISH_AGAIN_PLAIN.format(t=t)
    return OTHER_WISH_ASK.format(c="と".join(listed)) if listable else OTHER_WISH_ASK_PLAIN


def callback_question(role: str, text: str | None = None, nth: int = 0, of: "Question | None" = None, again: bool = False,
                      chosen: bool = False, without: str | None = None, recap: "Question | None" = None, means: str | None = None,
                      answering: bool = False, which: bool = False, after_recap: bool = False, announced: bool = False,
                      hesitating: bool = False, ways: tuple = (), about: str = "", do: str = "") -> "Question":
    """システムが足す質問（役目＝SYSTEM_ROLES の1つ）を作る唯一の場所：かけ直し・ほかの手段で聞くこと（時刻＝日時の型＝日にちだけの答えも
    受け取る・番号＝電話番号の型・メール・住所。どれも言い直して確かめる型）と、選んでいただくこと（代わりの方に、判断いただけるお立場か・
    お取次ぎのみか＝✅ 2026-10-22 ユーザー（4回目）の2つから選んでいただく問い／伝言を伝えていただけるか＝はい／いいえ）。
    出られる時間は必須にしない（✅ 2026-10-14 ユーザー：分からないと言われたら、それでよい）。選んでいただく問いも必須にしない（はっきりしなければ、
    話を進めない側＝お礼を言って戻る時刻を聞く）。text：伝言の文（依頼主の単位。業務設定の message）。
    of：ほかのご希望を伺う質問（other_wish）の元の質問（選ぶ質問で「どれも要らない」と答えていただいたもの。文と選択肢はその質問から＝_wish_choices）。
    ways：申し出の方法（offer_ways）。about：聞くこと（業務設定の締める前の申し出＝CloseOffer.topic）。do：締める前の申し出を受けていただいたときに、こちらが
    することの言葉（CloseOffer.do）。"""
    qtype = _ROLE_TYPE.get(role, "datetime")
    qid = SUBSTITUTE_ID if role == "authority" else CALLBACK_IDS[ROLE_KEY[role]]
    if role == "handoff_way":
        # 申し出（詳しい者からのお話 N4・締める前の申し出 N6）を受けていただいたあと、その方法（業務設定の方法 ways の名前のまま＝offer_ways）を選んでいただく一言
        # （申し出た方法を並べて伺う形と同じ phrases.OTHER_WAYS_WHICH。依頼主の単位の録音 OFFER_WAY_RECORDING）。必須（聞き取れなければ聞き返す）。
        # ✅ 2026-11-09 日本時間4時36分49秒 ユーザー：「それがOKなら、連絡手段として、「訪問、オンライン説明、お電話、セミナー参加」などを選択していただく方向で
        # 誘導する。」・6時33分57秒「(ア)…ｂ」（締める前の申し出も同じ1か所＝意味と聞くことは、どちらの申し出にも合う言葉）
        # 業務の方法に無いご連絡の方法も選べる（ほかのご希望と同じ1か所 other_means。業務の方法と同じ方法は足さない）
        extra = other_means(m for w in ways for m in means_in(w))
        return Question(qid, OTHER_WAYS_WHICH.format(w="と".join(ways)), qtype, required=True, conditions=(*ways, *extra),
                        meanings={**{w: f"ご希望の方法は「{w}」ということ" for w in ways}, **extra}, recording=OFFER_WAY_RECORDING,
                        topic=CALLBACK_TOPIC[role], callback=role, firm_not_needed=True)
    if role == "other_wish":
        conds, meanings = _wish_choices(of)
        # again＝伺っている途中で切れた電話の続きで、切れた所から伺う（Flow の始め＝_wish_begin。文と録音だけが違い、選択肢と答えの受け方は同じ）
        # 必須（聞き取れない・判定が食い違うと聞き返し、締めの前の念のための確認でもう1回）：取れないまま先へ進むと、ご連絡の方法を伺わずに終わり、最終目的
        # なら元の答え（どれも要らない）で締めて、その業務をかけない印が付く（取り消せない）＝伺い方（line_how）を必須にしたのと同じ理由（10-19 の決まり）。
        # ⛔ 2026-11-05 日本時間2時05分37秒のご質問のあとの読み直しで見つけた：必須にしておらず、「LINEなら。」に判定が食い違っただけで、その場で締めていた
        # which＝ご案内の方法の申し出に、どの方法かは言わずに受けていただいたときの1回の伺い直し（文と録音だけが違う）。申し出の W1 の聞き直しの聞くことは
        # 「ご案内の方法」（✅ 2026-11-06 日本時間9時05分17秒 ユーザー「(3)…OK」＝8時台のご報告のお勧めの形「聞き直しのときの言葉も、「ほかのご希望」ではなく
        # 「ご案内の方法」にします」）
        # 並べて伺う形だけ：「どれでも」は答え（どちらでも＝10-17 の案A）＝業務設定で最初に並ぶ方法でご案内する（Flow._wish_step）。✅ 2026-11-07
        # 日本時間3時14分01秒 ユーザー「(2) H3…イ」。⛔ それまでは選べるものに無く、「どれでも大丈夫ですよ。」も「方法は言っていない」として申し伝えていた
        return Question(qid, _wish_text(of, again, which), qtype, required=True, conditions=conds, meanings=meanings,
                        recording=other_wish_recording_id(of, again, which), topic="ご案内の方法" if way_offer(of) else CALLBACK_TOPIC[role],
                        callback=role, declines=(WISH_NONE,),
                        firm_not_needed=True, of=of.id, senses=_wish_senses(of),
                        lists_ways=which and way_any_line(of) is not None,
                        # 戻るときは「前回お伺いした件で」を言い直さない（Question.plain＝B-5）
                        plain=(other_wish_recording_id(of), _wish_text(of)) if again else ())
    # nth＝この通話で同じ質問を伺う何回目か（0から）：2回目からの言い方を順に回す（phrases.CALLBACK_ASK_AGAIN。録音は callback_（役目）_2 から）
    lines = (CALLBACK_ASK.get(role, ""), *CALLBACK_ASK_AGAIN.get(role, ()))   # 伝言の文などは言い方の表に無い（text で渡す）
    i = nth % len(lines)
    recording = f"callback_{role}_{i + 1}" if i else ""
    if role in CALLBACK_ASK_REUSED:
        # ほかの共通の一言の録音をそのまま流すシステムの質問（phrases.CALLBACK_ASK_REUSED＝本心を伺う一言 concern は「AIですか」に応える一言の録音 ai_answer）
        recording, line = CALLBACK_ASK_REUSED[role]
        lines, i = (line,), 0
    plain = ()   # 戻るときの形（Question.plain）：前置きのある形だけ
    if again:
        # 伺っている途中で切れた電話の続きで、切れた所から伺う一言（phrases.CALLBACK_ASK_RESUMED。共通の一言の録音 callback_（鍵）_again。選択肢と答えの受け方は同じ）。
        # ご連絡の方法の連絡先は、選んでいただいた方法 means で一言を選ぶ（contact_again_key。同じメールアドレスを伺うオンラインは「オンラインでのご案内の件で…」）
        key = contact_again_key(means) if means is not None and WISH_MEANS.get(means, (None, None, None))[2] == role else role
        lines, i, recording = (CALLBACK_ASK_RESUMED[key],), 0, f"callback_{key}_again"
        plain = (f"callback_{role}", CALLBACK_ASK[role])   # 戻るときは「前回…」の前置きを言い直さない（B-5）
    elif hesitating:
        # ご承認の質問への迷い（Flow._point_begin）：ご承認いただけなかったときの手順の1つ目（N1）を「どういった点でお迷いでしょうか？」で伺う（共通の一言の録音
        # hesitate_point＝迷っているお客様に伺う一言と同じ1本。選択肢と答えの受け方は N1 と同じ＝点を言った・断ろうとしている）。✅ 2026-11-08 日本時間
        # 2時21分15秒 ユーザー「(1)〜(4)ともイでOK」の (4)
        assert role == "not_agreed", role
        lines, i, recording = (HESITATE_POINT,), 0, "hesitate_point"
    elif recap is not None:
        # 続きの電話の始めに、ご納得いただけなかった点を言っていただけなかったとき、「ほかに…」（other_point）の代わりに流す振り返りの一言（phrases.recap_line。
        # ご承認の質問 recap の説明の文が入る＝依頼主の単位の録音 recap_recording_id）。選択肢と答えの受け方は「ほかに…」と同じ（Flow._point_step の1か所）。
        # ✅ 2026-11-05 日本時間4時16分52秒 ユーザー（お言葉は phrases.recap_line）
        assert role == "other_point" and approval_explanation(recap), (role, recap.id)
        lines, i, recording = (recap_line(approval_explanation(recap)),), 0, recap_recording_id(recap)
    elif answering and role in CALLBACK_ASK_ANSWERING:
        # 答えの段（お呼びする段でない）でかけ直しを申し出られたときの一言（phrases.CALLBACK_ASK_ANSWERING。共通の一言の録音 callback_（役目）_answering。
        # 理由にかかわらず＝✅ 2026-11-06 日本時間0時44分31秒 ユーザー「(イ) 答えの段のかけ直しすべてで大丈夫」）
        lines, i, recording = (CALLBACK_ASK_ANSWERING[role],), 0, f"callback_{role}_answering"
    elif which and role in CALLBACK_ASK_WHICH:
        # どの点かを言っていただけなかったときに1回伺う一言（phrases.CALLBACK_ASK_WHICH。共通の一言の録音 callback_（役目）_which。選択肢と答えの受け方は同じ＝
        # Flow._point_step）。✅ 2026-11-06 日本時間9時05分17秒 ユーザー。after_recap＝続きの電話の振り返りの一言への返事（まだ点を1つも伺っていない）：
        # 「ほかに」を外した一言（phrases.CALLBACK_ASK_WHICH_RECAP・録音 callback_（役目）_which_recap。✅ 10時12分44秒 ユーザー「(イ)…OK」）
        lines, i, recording = ((CALLBACK_ASK_WHICH_RECAP[role],), 0, f"callback_{role}_which_recap") if after_recap else \
            ((CALLBACK_ASK_WHICH[role],), 0, f"callback_{role}_which")
    elif announced and role in ANNOUNCED_ROLES:
        # こちらで方法をお伝えした一言（phrases.WAY_ANY_ANNOUNCE＝「それでは（方法）でご案内させていただきますね。」）のすぐあとの連絡先の一言：頭の「それでは」を
        # 重ねない（共通の一言の録音 callback_（役目）_bare）。✅ 2026-11-07 日本時間3時14分01秒 ユーザー「(2) H3…イ」
        # 名前と文は、戻る一言に続ける形と同じ1か所（phrases.back_fit＝callback_（役目）_bare）
        recording, bare = back_fit(f"callback_{role}", CALLBACK_ASK[role], WAY_ANY_ANNOUNCE)
        lines, i = (bare,), 0
        plain = (f"callback_{role}", CALLBACK_ASK[role])   # 戻るときは、お伝えした一言に続ける形ではなく、それだけで伺う形（B-5・B-6）
    elif chosen and role in CALLBACK_ASK_CHOSEN:
        # LINE の連絡先の伺い方（line_how）で選んでいただいたあとに伺う一言（phrases.CALLBACK_ASK_CHOSEN。共通の一言の録音 callback_（役目）_chosen）
        lines, i, recording = (CALLBACK_ASK_CHOSEN[role],), 0, f"callback_{role}_chosen"
    meanings = dict(_ROLE_MEANINGS.get(role, {}))
    if without is not None:
        # LINE の伺い方で選んでいただいた方法（without＝LINE_HOW の via）の連絡先が無かったときに、ほかの方法を1回だけ伺う一言（Flow._line_other_ways。
        # phrases.CALLBACK_ASK_OTHER_WAYS・2回目＝方法を言っていないときの伺い直しは CALLBACK_ASK_OTHER_WAYS_AGAIN。共通の一言の録音 callback_line_how_without_（via）・
        # その _2）。選択肢から、取れなかった方法を外す（会話AIも判定も、その方法は選べない）
        assert role == "line_how" and without in CALLBACK_ASK_OTHER_WAYS, (role, without)
        i = nth % 2
        lines = (CALLBACK_ASK_OTHER_WAYS[without], CALLBACK_ASK_OTHER_WAYS_AGAIN[without])
        recording = f"callback_{role}_without_{without}" + ("_2" if i else "")
        meanings = {c: m for c, m in meanings.items() if LINE_HOW.get(c, (None,))[0] != without}
    if qtype in CHOICE_TYPES:
        # 選んでいただく問いは必須にしない（上の説明）。ただし LINE の連絡先の伺い方（line_how）は、連絡先を伺う手順の一段＝連絡先の質問（必須）と同じく
        # 必須（聞き取れなければ聞き返す）。⛔ 2026-11-04 日本時間21時台に見つけた：必須にしておらず、1回聞き取れないだけで連絡先を伺わずに次へ進んだ
        # （11-03 20時48分「LINEアドレスを伺うというステップを忘れないように」）
        # 聞き直しの聞くこと：続きの電話の振り返りの一言（recap）と、その返事のあとの「どの点か」（after_recap）は、まだ点を1つも伺っていないので「ほかに」を外す
        # （✅ 2026-11-06 日本時間10時12分44秒 ユーザー「(イ)「ほかに」を外して…OK」と同じ理由。⛔ 15時台まで、聞き直しで「ほかにご納得いただけない点を…」と言った）
        # 意味合いを尋ねる一言の候補の言葉（ROLE_SENSES）も同じ理由で「ほかに」を外す
        first = recap is not None or after_recap
        topic = about or (HESITATE_TOPIC if hesitating else CALLBACK_TOPIC[role].removeprefix("ほかに") if first else CALLBACK_TOPIC[role])
        if role == "close_offer":
            recording = CLOSE_OFFER_RECORDING   # 文は業務設定（text）＝依頼主の単位の録音
        notes = {}
        if role == "handoff":
            # 詳しい者からのお話への迷いに選ぶ助けを伝える材料（業務の選ぶ質問の Question.option_notes と同じ形＝迷っているお客様への応じ方の1か所 Flow._hesitation）：
            # どの業務でも同じ良い点と、方法が2つ以上なら業務設定の方法から選んでいただけること（phrases.HANDOFF_NOTES）。✅ 2026-11-09 日本時間6時33分57秒のご指摘
            from phrases import HANDOFF_NOTES
            notes = {"はい": "。".join((*HANDOFF_NOTES[:2], *([HANDOFF_NOTES[2].format(w="・".join(ways))] if len(ways) > 1 else [])))}
        # 申し出（詳しい者より改めて N4・締める前の申し出 N6）を受けていただいたときに、こちらがすることの言葉（業務の選ぶ質問と同じ Question.do_words の「はい」＝
        # 業務の質問は取込の decide_do_words・N4 はどの業務でも同じ phrases.HANDOFF_DO・N6 は業務設定 CloseOffer.do＝取込の同じ1か所）。会話が繋がらないお返事の
        # あとの伺い直し「それでは（条件、）（こちらがすること）ということでよろしいでしょうか？」の申し出の中身（Flow._offer_again・phrases.offer_words）。
        # ✅ 2026-11-09 日本時間7時57分50秒 ユーザー（お言葉は Flow._offer_unlinked）
        does = {"はい": HANDOFF_DO} if role == "handoff" else {"はい": do} if role == "close_offer" and do else {}
        return Question(qid, text or lines[i], qtype, required=role == "line_how", conditions=tuple(meanings), recording=recording, option_notes=notes,
                        do_words=does,
                        meanings=meanings, topic=topic, callback=role, declines=_ROLE_DECLINES.get(role, ()),
                        senses={c: w.removeprefix("ほかに") if first else w for c, w in ROLE_SENSES.get(role, {}).items() if c in meanings},
                        # 受け入れにも断りにも取れる言葉の意味（ROLE_REPLIES）は、その質問の意味で決まる「ほかに…」・「ご説明した内容で…」そのものだけ（18時51分27秒・
                        # 20時03分09秒のお言葉のとおり）。振り返りの一言（質問ではない）・どの点かを伺う一言（何の点かを尋ねる）には当てない（⛔ 23時22分41秒のご質問の
                        # 見直し A2：当てていて、振り返りへの「結構です。」でご承認を「はい」で渡した）
                        reply_meanings={} if first or which else {w: c for w, c in ROLE_REPLIES.get(role, {}).items() if c in meanings},
                        firm_not_needed=role in (*POINT_STAGES.values(), "line_how", "wait_or_later", "concern", *OFFER_ROLES),
                        # その場で1回だけ伺い直す：ご承認いただけなかったときの手順（N1〜N3）と、詳しい者からの改めてのお話を伺う N4（ご承認を伺い終える所＝受けて
                        # いただけたかを取りこぼさない。11-02 6時21分「契約を断ろうとしているのか、本当に分からないのかをしっかりと見分けないといけない」と同じ理由。
                        # ⛔ 2026-11-09 日本時間5時台に N4 を入れた形は伺い直さずに次へ進んだ）
                        asks_once=role in (*POINT_STAGES.values(), *OFFER_ROLES),
                        of=recap.id if recap is not None else "", plain=plain)
    return Question(qid, lines[i], qtype, required=role not in ("reachable_time", "ask_who", "ask_when", "not_agreed"), conditions=("答えた",),
                    topic=CALLBACK_TOPIC[role], callback=role, recording=recording, plain=plain)


@dataclass(frozen=True)
class CloseOffer:
    """営業に引き継がない架電の、締める前の申し出（業務設定＝instruction.Business.close_offer・取込＝CL-2）：ご承認が最終目的のご承認の手順を点を言って
    いただけないまま終え、残りの質問が無いとき、締める前に伺う（営業に引き継ぐ架電の詳しい者からのお話と同じ1か所＝Flow._handoff_step）。
    ✅ 2026-11-09 日本時間6時33分57秒 ユーザー「・(ア) 営業に引き継がない架電で、ご承認が最終目的で、残りの質問が無いとき：ｂ」（ｂ＝締める前に別の一言を足す
    （例：資料を改めてお送りする申し出。ただし業務設定にある場合だけ））"""
    text: str          # 申し出の文（依頼主の単位の録音 CLOSE_OFFER_RECORDING。例「それでは改めて資料をお送りさせていただいてもよろしいでしょうか？」）
    topic: str = ""    # 聞くこと（聞き直し・保留でよいかを伺う一言に入る。例「資料のお送りのご希望」。無ければ聞き直しは聞くこと無し・保留は「この件」）
    ways: tuple = ()   # 方法（2つ以上なら選んでいただく・1つならその方法・無ければ方法も連絡先も伺わずに申し伝える。例 ("メール", "郵送")）
    # 申し出を受けていただいたときに、こちらがすることの言葉（取込＝業務の選ぶ質問の Question.do_words と同じ1か所 import_ai.decide_do_words が、申し出の文の
    # 「はい」に決める「〜させていただきます」＝meanings.DO_PROMPT の申し出の受ける向き。例「改めて資料をお送りさせていただきます」）。会話が繋がらないお返事の
    # あとの伺い直し「それでは（条件、）改めて資料をお送りさせていただくということでよろしいでしょうか？」の申し出の中身（Flow._offer_again）。要る（2つの文字の
    # AIがそろわず決まらなければ、締める前の申し出は作らない＝今のお礼の締め）。✅ 2026-11-09 日本時間7時57分50秒 ユーザー（お言葉は Flow._offer_unlinked）
    do: str = ""

    def __post_init__(self):
        if not self.do.endswith(OFFER_DO_END) or self.do == OFFER_DO_END:
            raise ValueError(f"締める前の申し出のこちらがすることの言葉（do）は「…{OFFER_DO_END}」の形で要ります（取込の decide_do_words）：{self.do!r}")


def offer_ways(handoff_ways: tuple = (), close_offer: "CloseOffer | None" = None) -> tuple:
    """申し出の方法（唯一の決まり）：営業に引き継ぐ架電は引き継ぎの方法、そうでなければ締める前の申し出の方法。"""
    return tuple(handoff_ways) or (tuple(close_offer.ways) if close_offer is not None else ())


def way_word_pairs(questions, handoff_ways: tuple = (), close_offer: "CloseOffer | None" = None) -> list[tuple[str, tuple]]:
    """方法を申し出の中身に入れる言い方を、取込で決める (こちらがすることの言葉, 方法の並び) の並び（唯一の並べ方＝取込が import_ai.decide_way_words に渡す）：
    申し出の質問（N4・N6）の言葉と申し出の方法（N5・C11 の提案も同じ言葉）・業務の申し出の質問の受ける答えの言葉とご案内できる方法・ほかのご希望（W1・
    その伺い方 C11）の「ご案内させていただきます」とご案内できる方法（_offered_ways）。同じ言葉は1つにまとめる。✅ 2026-11-12 まとめ役の (1)"""
    pairs: dict = {}

    def add(do, ways):
        if do and ways:
            got = pairs.setdefault(do, [])
            got += [w for w in ways if w not in got]
    for q in offer_questions(handoff_ways, close_offer):
        if q.offered is not None:
            add(q.do_words.get(q.offered), offer_ways(handoff_ways, close_offer))
    wish = []
    for q in questions:
        if q.offered is not None:
            add(q.do_words.get(q.offered), tuple(q.offer_ways))
        wish += [w for w in _offered_ways(q) if w not in wish]
    add(WISH_DO, wish)
    return [(d, tuple(w)) for d, w in pairs.items()]


def offer_questions(handoff_ways: tuple = (), close_offer: "CloseOffer | None" = None) -> list["Question"]:
    """業務設定で決まる申し出の質問（唯一の作り＝流れの制御 Flow._handoff_ask・会話AIへの宣言 tool_declarations・録音の表 make_ack_clips.script_table・
    知っていること instruction.Business.knowledge）：営業に引き継ぐ架電は詳しい者からのお話 N4（選ぶ助けの材料つき）、そうでなく業務設定にあれば締める前の
    申し出 N6、方法が2つ以上ならその方法を選んでいただく N5。"""
    asks = ([callback_question("handoff", ways=tuple(handoff_ways))] if handoff_ways else
            [callback_question("close_offer", close_offer.text, about=close_offer.topic, do=close_offer.do)] if close_offer is not None else [])
    ways = offer_ways(handoff_ways, close_offer)
    return asks + ([callback_question("handoff_way", ways=ways)] if len(ways) > 1 else [])


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
    pending_reasons: dict = dataclasses.field(default_factory=dict)  # その値と一緒に受け取った理由（until_why・cycle。午前・午後の答えで消さない）
    ampm_asked: bool = False          # 午前・午後を聞いたか（1項目1回まで）
    form_asked: bool = False          # 形の合わない答えに、理由を添えて伺い直したか（phrases.CALLBACK_ASK_FORM。1項目1回まで＝Flow._ask_again）
    form_failed: bool = False         # 理由を添えて伺い直しても形が合わなかった（その形の答えは無い＝Flow._line_other_ways がほかの方法を伺う）
    until_why: str | None = None      # 「〇時まで」の、いま電話を続けられない理由（UNTIL_LABEL の中の名前。かけ直しの記録の until_why）
    cycle: str | None = None          # かけ直しの日・頃を選んだ月単位の理由（CYCLE_LABEL の中の名前。かけ直しの記録の cycle）
    reason: str | None = None         # 「わからない」「答えたくない」の理由（DONT_KNOW_LABEL・REFUSED_LABEL の中の名前。CL-2 の項目の reason）
    until_asked: bool = False         # 理由が分からないので、時刻を伺い直したか（1項目1回まで）
    ambiguous_asked: bool = False     # 曖昧な答えの意味を確かめたか（そのあとの「結構です」は、確かめへの返事として受け取る）
    tried_readings: list = field(default_factory=list)  # 名前の読みの確かめで、これまでに言った読み（同じ読みを2度言わない）
    told: bool = False                # その答えのあとに伝える一言（Question.tell_words）を、もう言った
    played: bool = False              # この電話でこの質問をもう流した（伺った）：流し直すときの頭は「それでは」（AGAIN_LEAD）・初めては「続きまして」
                                      # （NEXT_LEAD）＝Flow._continue。立てるのは質問を流す所（_next_question・_opening_reply・_continue）
    final: bool = False               # 最終の確かめを伺った（Flow._final_check）：同じ確かめはもう言い直さない（Flow._confirm_again の1か所。直された答え・
                                      # お断りは今までどおり受け取る＝確かめの回数 corrections は使い切らせない）
    hedged: str | None = None         # 言い直しへの返事が言い切っていなかった（その返事の言葉）：依頼主へは別の欄にお客様の言葉を渡す（Flow.customer_words）
    tentative: str | None = None      # 値が確定でない答えを受ける形（Flow._tentative の1か所）：set＝「ひとまず…」を伺った／noted＝「…承っておきますね。」を言った
    approx: str | None = None         # 確定でない答えの、お客様の言い方の値（「15日あたり」＝approx_value。承っておく一言で、こちらで言い換えずに言う）
    said: list = field(default_factory=list)  # この質問の答えが動いた発話の、お客様の言葉（Flow._note_said の1か所。依頼者へは「-」「✕」の答えに添える＝customer_words）
    said_upto: int = 0                # history のどこまでを、お客様の言葉に当てたか（Flow._note_said）
    pending_asked: bool = False       # 保留にしてよいかを伺った（Flow._ask_pending の1か所。同じ伺いは2回しない＝Flow._pending_due）
    maybe_asked: str | None = None    # どちらの場合もありうる答えに、依頼者が備える答えの可能性を確かめた（Flow._ask_confirm の maybe の1か所）：pending＝お返事を
                                      # 待っている（はっきりしないお返事への2回目も可能性の確かめ＝maybe_settle）／done＝お返事をいただいた。確かめたあとの
                                      # 「どちらでも」は、その可能性がある答えとして受け取る（同じ確かめをくり返さない＝Flow._judged_reply）
    offer_again: bool = False         # 申し出（N4・N6）への会話が繋がらないお返事に、ひとこと応えてから申し出を伺い直した（Flow._offer_again の1か所。1項目1回）
    offer_terms: str | None = None    # その伺い直しに入れた、お客様の言葉にある申し出の受け方の条件（会話AIの報告で Flow._proposal_problem を通ったもの）
    offer_topic: str | None = None    # その伺い直しに入れた、申し出に含める中身（お客様が気にかけていることに応える＝「でも」の伺い直し）
    offer_stated: str | None = None   # 条件つきの受けの言い切りで、お伝えした方法（申し伝える一言の1回として数える＝Flow._told_said。Flow._taken_on_terms の1か所）
    offer_added: str | None = None    # その伺い直しに、こちらから足した条件（お客様の言っていないご都合。お客様の条件 offer_terms とは別の欄＝依頼者へ別に渡す）
    offer_way: str | None = None      # その伺い直しに入れた、ご希望の方法（業務の方法の名前。受けていただいたら方法を重ねて伺わない＝Flow._handoff_step）

    def set(self, state: str, why: str) -> None:
        assert state in STATES, state
        self.history.append((self.state, state, why))
        self.state = state



# AIへのプログラムからの知らせの枠の唯一の場所（声で返事をさせない知らせは quiet）。⛔ 2026-11-04 に見つけた穴：12か所で枠の言葉を手で書いて
# いた（片方だけ直すと、AIへの知らせの形が場面で食い違う）。知らせの文そのものは、ここへ移すときに1文字も変えていない
NOTICE_HEAD = "［プログラムからのお知らせ］"
NOTICE_QUIET = "このお知らせには、声で返事をしません。"
NOT_CONNECTED = "まだ目的の人に繋いでいません。"   # 呼んでいる段の知らせに添える一文（2か所の知らせで同じ）
NOT_INTRODUCED = "電話口の方は、まだこちらの名乗りを聞いていません。"   # 名乗りを流し直すときの知らせの頭（2か所で同じ）


def start_with(words: str) -> str:
    """AIへ、話し始めの言葉を渡す一文（唯一の書き方。質問に入る一言 Question.intro と、つなぎの言葉 _lead_note）。"""
    return f"「{words}」から話し始めてください。"


def notice(body: str, quiet: bool = False) -> str:
    """AIへのプログラムからの知らせの文（quiet＝声で返事をさせない知らせ）。"""
    return NOTICE_HEAD + body + (NOTICE_QUIET if quiet else "")


class Flow:
    def __init__(self, questions: list[Question], limits: Limits | None = None, confirm=confirm_phrase,
                 busy_ask: str | None = None, lead_ins: bool = True, play_recordings: bool = False, goal_mode: str = "any",
                 business_hours: tuple = DEFAULT_BUSINESS_HOURS, designated_limit: tuple = DEFAULT_DESIGNATED_LIMIT,
                 opening: dict | None = None,
                 voicemail_message: str | None = None, target: dict | None = None, answers: dict | None = None,
                 policies: dict | None = None, knowledge: list[str] | None = None, names: list[str] | None = None,
                 name_readings: dict[str, str] | None = None, judge_names: dict[str, str] | None = None,
                 promised: bool = False, apology: tuple = (), successor: bool = False,
                 voicemail_left: bool = False, earlier: dict | None = None, number_again: str | None = None, hold_wait_s: int = 300,
                 apology_call: str | None = None, apology_when: str = "later", client_does: str = "", apology_text: str | None = None,
                 earliest_day=None, today=None, handoff_ways: tuple = (), close_offer: "CloseOffer | None" = None,
                 way_words: dict | None = None):
        """confirm：言い直しの言い方を作る関数 (値, 場面, 何回目) → 文（ふだんは phrases.confirm。言い方を比べる試験 G-25 だけが別のものを渡す）。
        busy_ask：相手が「忙しい」と言ったときに1回だけ言う引き止めの一言（業務設定。録音で流す電話では業務の録音 busy。None なら引き止めず、
        締める前の伺い「それではまた改めてお掛けしてもよろしいでしょうか？」へ＝_hurry_ask。✅ 2026-11-01 日本時間20時56分。Intent.md 9-4）。
        target：目的の人のある業務の、名乗りの部分の文（instruction.Business.replay_parts。呼ぶ一言 target・確かめる一言 target_confirm を含む）。
        あれば、目的の人に繋がるまでは calling の段で、最初の質問を渡さない（DESIGN.md 4-3）。None＝「誰でも」（最初に出た人が目的の人）。
        policies：相手の申し出への応じ方（instruction.Business.policies。無い項目は既定＝POLICIES の最初・follow_up は False）。
        knowledge：こちらが知っていること（instruction.Business.knowledge）・names：声に出さない名前（社名・担当者名・お呼びした方の名前と読み。
        instruction.Business.spoken_names）・name_readings：読みの決まった名前の表記 -> 読み（instruction.Business.name_readings）＝AIが自分の言葉で言う文の
        確かめ（own_answer_problem・grounding）に使う。judge_names：判定に見せる名前の形（声の形 -> 書いてあるとおり＋読み。
        instruction.Business.judge_names）＝判定へ質問を渡す1か所（for_judge）で当てる。
        promised・apology・successor：この電話の門の予約証（CL-2 の gate.Claim）から＝お客様と約束したかけ直しの電話か（取り次げないと言われたら、
        締めの前に改めてお掛けしてよいかを伺う）・まずお詫びすること（理由の並び。目的の方に繋がったら最初に）・前任の方の後任のご担当者様を
        お呼びする電話か（呼ぶ一言は instruction.Target の successor が作る）。"""
        self.confirm = confirm
        self.busy_ask = busy_ask
        # 依頼者がすること（業務設定＝CL-2。登録・手配など。空なら無い）。事実を伺う質問の、言い切っていない最初の答えの確かめ「それでは（意味の言葉）という
        # ことで（依頼者がすること）させていただいて大丈夫でしょうか？」に入れる（AFTER の hedged_fact・tentative_fact）。空なら「…ということで承っておいて
        # 大丈夫でしょうか？」（言葉を作るのは phrases.client_does_words の1か所。✅ 2026-11-08 日本時間5時33分05秒「…「～で承っておいて」に変更しましょうか。」）
        self.client_does = client_does
        assert goal_mode in GOAL_MODES, goal_mode
        self.goal_mode = goal_mode   # 最終目的の果たし方（GOAL_MODES。業務設定＝CL-2）
        # 営業に引き継ぐ架電の、詳しい者からの改めてのお話の方法（業務設定＝instruction.Business.handoff_ways・取込＝CL-2。空なら営業に引き継がない架電）と、
        # その伺いの手順の印（Flow._handoff_step の1か所）
        self.handoff_ways: tuple = tuple(handoff_ways)
        # 営業に引き継がない架電の締める前の申し出（業務設定＝instruction.Business.close_offer・取込＝CL-2。None なら今のお礼の締め）と、申し出の方法（offer_ways の1か所）
        self.close_offer: CloseOffer | None = close_offer
        self.offer_ways: tuple = offer_ways(self.handoff_ways, close_offer)
        # 方法を申し出の中身に入れる言い方（業務設定＝取込で決めたもの。{こちらがすることの言葉: {方法: 言葉}}。読むのは _offer_words の1か所）
        self.way_words: dict = {d: dict(m) for d, m in (way_words or {}).items()}
        self.handoff: dict | None = None
        # 留守番電話に残す伝言（業務設定。Intent.md 9-4・18-2 の14。None なら残さずに切る＝安全側）
        self.voicemail_message = voicemail_message
        # この依頼者が、このお客様（同じ番号）の留守番電話に、もう伝言を残したか（電話の前に CL-2 の call_report.voicemail_left を読んで渡す。
        # ✅ 2026-10-19 ユーザー：「留守番電話に入れるのは最初の1回だけ…同じ依頼者のすべての一覧で、同じお客様（同じ番号）」＝CL-2 の 4-106）
        self.voicemail_left = voicemail_left
        self.voicemail_sending: str | None = None   # 流すように頼んだ、留守番電話の伝言（最後まで流れたら通話の事実 voicemail_message＝on_played）
        # 取り次ぎ・保留で待つ長さ（秒。業務設定。✅ 2026-10-24 ユーザー：「業務ごとに選べる：デフォルト、相手から長く待つ必要がある理由がなければ5分、
        # 理由があればそれまで待つ」）。相手が長さを言えば、その長さ（_hold の1か所で決め、返事の hold_wait_s で電話接続側へ渡す）
        self.hold_wait_s = hold_wait_s
        # 想定問答（業務設定。Intent.md 5-3 (2)「AIが答えてよいこと」）：問いの名前 → 答えの文。答えは録音（名乗りを録音で流す通話）か、
        # AIがその文のとおりに言う（AIが話す通話）。AIは自分の言葉では答えない（_answer）
        self.answers: dict[str, str] = dict(answers or {})
        if clash := sorted(set(self.answers) & RESERVED_PART_NAMES):
            raise ValueError(f"想定問答の問いの名前 {clash} は、名乗りなどの録音の部分の名前と同じです（使えない名前＝RESERVED_PART_NAMES）")
        self.answered: dict[str, int] = {}  # 答えた回数（部分ごと。同じ答えは2回まで）
        # 相手の申し出への応じ方（業務設定。✅ 2026-10-22 ユーザー：代わりの方・伝言・在籍していない は業務ごとに選べる。Flow._not_available）
        self.policies = {**{k: v[0] for k, v in POLICIES.items()}, "follow_up": False, **(policies or {})}
        self.message_text = (target or {}).get("message") or (opening or {}).get("message")  # 伝言の文（伝言を残す業務。依頼主の単位の録音）
        check_policies(self.policies, self.message_text)
        self.substitute_q: str | None = None  # 代わりの方に、判断いただけるお立場かを聞いている（その答えで決める＝_advance の入口）
        self.substitute_why = "absent"  # 代わりの方とお話しできなかったときに聞くかけ直しの理由（お呼びした方の事情＝不在・長期の不在）
        self.standin_wait = -1  # 代わりの方・後任の方の報告を、判定が届くまで待たせた相手の発話の番号（_calling_reported。同じ発話で2回は待たない）
        # 相手の質問・言葉に、AIが自分の言葉で答えた（_own_answer）：相手の返事のあとに「質問に戻る」で戻る（_resume）。同じ発話には1回だけ答える。
        # 値＝答えた部分（OWN_PARTS の中の名前。戻る相づちを分ける）か、答えの段で尋ねられた問い（_asked_now）に答えた「asked」か False。
        # 立てるのは _await_reply の1か所
        self.own_pending: str | bool = False
        self.reply_started = False  # いまの操作で、相手の返事を待ち始めたか（_await_reply。操作のあとに待つ印を消すかを決める＝_apply_one）
        # 返事を待ち始めた応え（答え・ご説明・受け答え・「AIですか」に応えた一言）の、お客様に聞こえた中身（返事の play・say・speak・silent と知らせ message）。
        # 待ち始める時に空にし（_await_reply）、その操作の返事ができた時に覚える（_apply_one）。会話AIが報告せずに声だけで答えた（judged_turn の voiced）ときは、
        # 何を言ったかを持たないので空のまま。もう一度言ってほしいと頼まれたら、これを「失礼いたしました。」のあとに言い直す（_repeat_now）
        self.reply_said: dict | None = None
        # 相手のお話・ご質問（世間話・お礼・気遣い・質問）に応えている：もう伺った質問（呼んでいる段なら、お呼びするお願い）へ戻る
        # ときは、話を戻す一言（phrases.BACK_LINES）から（_continue の1か所）。次の質問へ進めば、戻らないので消える（_advance）
        self.digressed = False
        # 同じ所（質問・言い直しの番号／お呼びするお願い＝"calling"）へ戻る一言を流した回数（_back_play の1か所。2回目・その次からは一言を替える＝H7）
        self.back_counts: dict[str, int] = {}
        self.own_turn = -1
        # 申し出を条件つきで受けていただいた答え・お返事の報告に添えた提案の言葉（相手の発話の番号, 質問, {terms, topic, way}）・業務に無い方法つきのお返事に、
        # ひとこと応えて提案で伺う番（相手の発話の番号）。_terms_reported・_taken_on_terms
        self.taken_terms: tuple | None = None
        self.own_tried: tuple[int, list] = (-1, [])  # 確かめに通らなかった答えの文（相手の発話の番号, [(文, 理由)]）。書き直させた回数を数える
        # 答えに添えられた相手の質問（_question_first）：答えへの返事は先に作っておき、AIがその質問に答えてから流す（取り出す1か所 _take_after_answer）。
        # (相手の質問, 返事, 返事を作った時の相手の発話の番号)
        self.after_answer: tuple[str, dict, int] | None = None
        # 答え方の操作を受けたとき、意味の判定のどれかが、いまの質問の答えとも読んだ相手の言葉（_keep_answer_reading）：
        # {"turn": 相手の発話の番号, "qid": 質問, "said": 言葉, "check": その言葉の確かめ}。AIは相手に応えたあと、いまの質問へ戻る前に、
        # その言葉の答えを報告できる（報告は、この言葉と確かめに当てる＝judged・_held_words）
        self.answer_held: dict | None = None
        self.judged: dict | None = None  # いま当てている報告が、前の発話の言葉の答え（answer_held）なら、その言葉（_apply_one の間だけ）
        self.question_answered_turn = -1  # 相手の質問に答えた相手の発話の番号（同じ発話の質問に、もう一度答えさせない＝_question_first）
        # 相手の言葉そのものを、いまの道が受けた発話の番号（かけ直しの申し出・目的の人の名乗りなど＝答えに添えた質問ではない。_advance の handled が立て、
        # 判定を使わない通話の言葉の形の見方 _question_to_us が読む）
        self.words_taken = -1
        self.unclear_turn = -1   # 会話AIが「聞き取れない」と報告した相手の発話の番号（聞き取れているかの1か所 _heard_clearly が読む。入口 _apply_one が立てる）
        # 答えの段で尋ねられた（_asked_now）問いへの答えの文が使えず、尋ねられる前の流れのとおりに進めると決めた相手の発話の番号（_asked_skip）
        self.asked_skip = -1
        self.knowledge = list(knowledge or [])
        # AIが自分の言葉で言う文の中身の判定の読み（grounding。電話接続側が on_grounded で渡す）：文 → 読み（相手の発話ごとに入れ替える）
        self.grounded: tuple[int, dict] = (-1, {})
        self.names = [n for n in (names or []) if n]
        self.name_readings = {k: v for k, v in (name_readings or {}).items() if k and v and k != v}
        self.judge_names = {k: v for k, v in (judge_names or {}).items() if k and v and k != v}
        self.party: str | None = None     # いまの相手の種別（PARTY_LABEL の中の名前。報告が無ければ None＝不明。DESIGN.md 4-3）
        self.callback_why: str | None = None  # かけ直しの申し出の理由（CALLBACK_LABEL の中の名前。1通話1回まで）
        self.callback_ids: list[str] = []      # かけ直しで足した質問（目的の人かを確かめに戻ったあと、続きから聞く＝_callback）
        self.back_turn: int | None = None      # かけ直しの質問の途中で、目的の人かを確かめに戻った発話の番号（_target_back。繋いだら None）
        self.callback_held = False             # かけ直しの質問を始めて（続きに戻って）から、取り次ぎがあったか（このあと新しい方が出うる＝_op_replay_opening）
        # お呼びした方が在籍しておらず、後任の方をお呼びしている（_to_successor）：呼ぶ一言・確かめる一言は後任の方の録音（共通の一言）。
        # target_from＝切り替えたあとの最初の相手の発話の番号（それより前の読み・回数は、前にお呼びした方のもの＝数えない）
        self.successor = False
        self.first_called = ""  # 後任の方へ切り替えたときの、はじめにお呼びした言葉（判定に見せる＝_calling_questions。次の電話の後任の方は空）
        self.target_from = 0
        self.system_parts: dict[str, str] = {}  # 名乗りの部分のうち、共通の一言の録音で流すもの（部分 → 録音の中の名前。_part_play）
        self.promised = promised
        # まだ言っていないお詫び（目的の方に繋がったら流す＝_connect）。流す順と知らない理由の確かめは apology_order の1か所（黙って捨てない）。承認したお詫びの文
        # （apology_text）は、約束の電話などの頭のお詫び（理由 forbidden_speech）の文（お詫びだけの電話の文は apology_call）
        self.apology_text = apology_text
        self.apology_due = apology_order(apology, apology_text)
        self.apology_reasons = tuple(self.apology_due)   # この電話のお詫びの理由（流したあとも残す＝用件の一文を前の電話とつなげるか purpose_part が読む）
        self.told_again: str | None = None   # 前の電話で申し伝える一言まで言って切れた：お詫びのあとに申し伝え直す種類（_point_told・_sorry_lines）
        self.close_after: str | None = None  # 締めの前に伺う一言のあとの、締めた理由（約束の電話で取り次げなかった＝_not_transferred）
        # 名乗りの部分のうち、別の名前の録音で流すもの（部分 → 業務の録音の部分の名前。_part_play）
        self.part_names: dict[str, str] = {}
        if successor:
            # 前任の方の後任のご担当者様をお呼びする電話：最初から後任の方が目的の人。呼ぶ一言は業務の録音の target_successor（お名前が入る。
            # instruction.Business.replay_parts）、確かめる一言・聞き返しは共通の一言の録音（_to_successor と同じ）。
            # ⛔ 2026-10-24 CL-2 の 4-152：1つの録音の組に同じ名前の部分は1つだけ＝ふつうの電話の呼ぶ一言（target）と同じ名前では録音できない
            self.successor = True
            self.system_parts.update({k: v for k, v in SUCCESSOR_IDS.items() if k != "target"})
            self.part_names["target"] = "target_successor"
            self.part_names["target_full"] = "target_successor_full"   # お名前だけでは分からないと言われたときの一言も、後任の方の形（_who_unclear）
        self.busy_asked = False
        self.confirm_counts: dict[str, int] = {}   # 言い直しの場面ごとの回数（同じ言い方が続かないよう、順番に回す）
        self.awaiting_say: str | None = None       # いま返事を待っている言い直しの、言い方（差し戻しのときも同じ言い方を使う）
        self.change_told: set[str] = set()         # この電話でお伝えした変更の受付の期限（Flow._change_line の1か所。2回目からは短い形）
        # 「わからない」のあとに足した聞き足しの質問（分かるお客様・分かる時）→ 元の質問（_take_system）。答えはかけ直しの記録に残す（noted＝残した分）
        self.follow_ups: dict[str, str] = {}
        # ご承認いただけなかったときの手順の状態（Flow._point_step の1か所だけが書く）：q＝ご承認の質問・stage＝ask（どの部分か伺っている）／explain
        # （AIがご説明する）／explained（ご説明が終わった）／other（ほかに…を伺っている）／confirm（ご納得いただけたかを伺っている）・round＝ご説明の回・
        # explained＝知っていることでご説明できたか・outcome＝agreed（ご納得）／declined（ご納得いただけない）／unresolved（ご説明できない・はっきりしない）
        self.point: dict | None = None
        # ご承認の質問ごとの、この通話でお客様にもう言った印（読み書きは _point_marks の1か所）：asked＝手順の一言ごとに伺った数（「ほかに…」の言い方を回す）・
        # anytime_said＝「ご不満な点が…」を言った
        self.point_marks: dict[str, dict] = {}
        # 選ぶ質問の「どれも要らない」のあとに、ほかのご希望を伺う段の状態（Flow._wish_step の1か所だけが書く）：q＝元の質問・stage＝ask（ほかのご希望を
        # 伺っている）／detail（ご連絡の方法の連絡先を伺っている）・detail＝連絡先を伺う質問の番号・means＝ご連絡の方法・outcome＝通話の事実 other_wish の
        # outcome（決まるまで None）・told＝申し伝える一言（phrases.WISH_TOLD の鍵。言ったら None）。wished＝この通話で伺った元の質問（1つの質問に1回）
        self.wish: dict | None = None
        self.wished: set[str] = set()
        # ご承認いただけなかったときの手順で、「ほかにご納得いただけない点は…」（N2）に「分かった」と答えていただいた（その質問の番号。無ければ None）：
        # ほかに分からない点が無いかを伺う業務の質問（Question.unclear_none）は、伺わずにその答えにする（Flow._no_doubts_step の1か所）
        self.no_doubts: str | None = None
        # 連絡先を伺う質問の番号 → ほかのご希望の前の答え（W1・LINE の伺い方 C11）の中にあった連絡先（Flow._wish_contact が置き、_advance_to が伺わずに言い直す）
        # 先に言われた答え（AIが報告に添えた「ほかに言われたこと」＝rest。_take_rest）：次に伺う質問へ進むとき、伺わずにその言葉の答えを報告してもらう
        # （_offer_ahead）。選ぶ質問は、その言葉を次の質問について読み直す判定（ahead_context）とそろったときだけ受け取る
        self.ahead: dict | None = None
        self.ahead_asked: dict | None = None
        self.follow_noted: set[str] = set()
        self.follow_asked: set[str] = set()       # この通話で伺った聞き足しの役目（ask_who・ask_when。1通話1回＝_take_system）
        # lead_ins：プログラムの返事のあとに話す文の頭に、受けの言葉を置くか（DESIGN.md 6-11。✅ 2026-10-13 ユーザー承認＝既定で置く。
        # 置かない形は、過去の比べの試験（G-25・G-27 の W0）を同じ意味で動かすためだけに残す）
        self.lead_ins = lead_ins
        # play_recordings：次の質問（つなぎの言葉＋質問の文）と締めの言葉を、電話接続側が録音で流し、AIには話させない（DESIGN.md 6-13。
        # ✅ 2026-10-15 ユーザー：よく使う言葉は共通の一言で、トークスクリプト（質問の文）は依頼主の単位で録音する。G-37 で確かめる）
        self.play_recordings = play_recordings
        self.business_hours = business_hours  # お客様の営業時間（午前・午後を補う・聞くかを決める。業務ごとの設定）
        self.designated_limit = designated_limit  # お客様指定の歯止め時間（営業時間と合わせて、午前・午後を決める。業務ごとの設定）
        self.lead_counts: dict[str, int] = {}
        self.ack_count = 0               # 次の質問の前に流す相づちを、順番に回す（_ack）
        self.last_ack: str | None = None # 最後に流した相づち（同じ「承知しました。」が続かないように＝_ack）
        # 先に言われた答えを伺う番（_offer_ahead）を開いた報告が、ふつうなら流した相づち（受け取った答えで選んだもの）と、その発話の番号。伺った報告は待ちの言葉
        # 「はい、」だけを流すので、同じ発話の最後の返事の頭に受けの言葉が要るとき、これを流す（Flow._ack の1か所）
        # （3つ目＝その報告で、お礼を返さない答えを受け取ったか＝_thanks_barred）
        self.ahead_ack: tuple[int, str | None, bool] | None = None
        # AIが答えを報告せずに言い直し、相手に値を直された：(質問, 直され方＝AFTER の鍵)。その答えの報告で、直され方に合う言い直しにする
        self.unreported_correction: tuple[str, str] | None = None
        # 判定は値を言ったと読んだのに会話AIが値を報告せず、値を報告し直させた：(質問, 発話の番号)。同じ発話で2回目なら質問から聞き直す（_judged_answer の所）
        self.value_asked_at: tuple[str, int] | None = None
        self.callee_said = ""            # いまの相手の発話の文字（会話AIの聞き取り。届いた分だけ。on_heard）
        # 会話AIの、相手の話し始め・話し終わりの知らせ（on_activity）：いまの発話で話し終わりが来たか・その位置・いま相手が話しているか。
        # 相手の声の終わりの位置（on_voice。電話接続側の声の見張り）。位置は、会話AIへ送った音の上の秒（words_complete）
        self.callee_ended = False
        self.callee_end_at: float | None = None
        self.callee_speaking = False
        self.callee_voice_end: float | None = None
        self._cut: dict | None = None    # 切れ方の材料のうち、電話が切れた瞬間の様子（on_line_end だけが書く。読むのは cut の1か所。会話の報告の cut）
        self.midway = False              # いま受けている報告は、相手の言葉が出揃う前に届いた（handle の midway。電話接続側が付ける）
        # いまの相手の発話の確かめ（on_check。CHECKS の名前 → 別の耳の文字／意味の判定の中の名前。届いていないものは入らない）。
        # いまの相手の言葉のものだけ（check_said：その確かめが見た相手の言葉。None＝発話まるごと。言葉が増えたら、前の言葉の判定は外す）
        self.check: dict[str, str] = {}
        self.check_said: dict[str, str | None] = {}
        self.checks_used: tuple = ()     # この通話で使う確かめ（use_checks。届かなかったものを、通話後の検証へ回すため＝unchecked）
        # 受け取った項目の、受け取ったときの材料（相手の発話の番号・会話AIの聞き取り・受け取った答え・確かめ）。受け取ったあとに届いた確かめは late に記録だけ
        # （_late_to_taken・届かなかった確かめ unchecked）。受け取るたびに書き換える（その項目を聞き直したら、新しい発話のもの）。いまの項目の記録だけを持つ
        # （同じ番号の質問の項目を作り直したら、前の回の記録は taken_before へ＝_new_item）。読むのは _material の1か所
        self.taken: dict[str, dict] = {}
        # 同じ番号の質問を伺い直す前の回の記録（質問の番号・その回の項目・材料。_new_item が移す）：その回の答えも依頼者へ渡りうる（ご承認の手順の点の記録など）
        # ＝届かなかった確かめ（unchecked）に「番号@発話の番号」で数え続け、遅れて届いた読みもその回の記録に残す（_late_to_taken）
        self.taken_before: list[tuple[str, "Item | None", dict]] = []
        # この通話で決め直した答え（_retake）の決め手：質問 → (決め手の答えの質問, その答えの項目)。決め直した答えは自分の材料を持たず、決め手の答えの
        # 材料（根拠の発話＝_evidence）。項目そのもので結ぶ＝同じ番号のシステムの質問を作り直した
        # （W1 をほかの質問に伺う・N2 を2回目に伺う）あとの答えには当たらない（決め直したときは、決め手の答えの材料はまだ書かれていない＝_record_taken はあと）
        self.rests_on: dict[str, tuple[str, "Item"]] = {}
        self.report_before: dict | None = None   # 受けている報告の前の項目の状態（apply の間だけ。_turned_down_now）
        # 相手が取り次いでいる最中か（意味の判定が HOLD と決め、AIに黙って待たせた。次の相手の発話で解く）。電話接続側は、この間、
        # 相手が黙っている秒を数える見張り（6-7・6-15 のつなぎの一言・張り替え）を止める（相手はいま電話口にいない。DESIGN.md 6-8）
        self.holding = False
        self.hold_wait: int | None = None  # いまの保留で待つ長さ（秒。_hold が決め、待ちきったら通話の事実 hold_timeout の waited_s に残す）
        self.nudged_turn = -1            # 報告を促す知らせを出した相手の発話の番号（unreported_note。1つの発話に1回まで）
        # こちらの不具合で電話を途中で終えた（_trouble の1か所）：理由と根拠（None＝終えていない）。聞き返しを使い切っても聞き取れないまま「-」に
        # なった必須の質問のうち、答えを受け取らないまま続いているもの（答えを1つ受け取ったら空）。張り替えて相手の発話を送り直した、相手の発話の
        # 番号（on_resend。同じ発話で2回目は張り替えない＝on_ai_failed）
        self.trouble: dict | None = None
        self.ai_trouble: list[dict] = []   # この通話で止まったAI・緊急の道で答えたこと（on_ai_trouble の1か所。通話の報告の ai_trouble）
        # 新しく1つ足したときの、通話の途中の知らせ先（CL-2 の通話ごとの口 call.ai_trouble。待たずに戻る。会話の側が通話の始めに入れる＝
        # convo.run_call の tell_ai_trouble）。⛔ 2026-11-02（CL-2 の連絡帳 4-171 の (a)）まで、通話の報告の終わりまで知らせていなかった＝
        # その間、同じ契約者のほかの回線が、止まったAIで新しい電話をかけていた
        self.tell_ai_trouble = None
        self.irr_checks: list[str] = []    # 取り消せない事実ごとの、2つ目の判定とそろったか（_note_irreversible の1か所。報告の irreversible_check）
        self.unclear_qids: set[str] = set()
        self.resent_turn: int | None = None
        # いちばん最近「合っている」で確かめが済んだ項目と、そのときの流れの位置（同じ相手の発話への報告を、AIが「違う値を言われた」に
        # 直したとき、その確かめを取り消して戻すため。_reopen）
        self.last_affirmed: dict | None = None
        # 最短で対応できる日（業務設定の「3営業日以降」などを CL-2 の暦＝営業日と祝日の1か所が日にちにしたもの）と電話の日：質問の文・入る一言の
        # {最短の日} を、電話の日から見た言い方（day_words の1か所）に置き換える。✅ 2026-11-03 ユーザー (4)：「業務設定では「3営業日以降」などの設定に
        # なるので、それを自動で置き換える仕組みが必要です。」・録音で流す電話は、言い方ごとに丸ごと録音した文を流す（(ア)「OK」＝Question.recording に
        # その言い方の録音の名前・day_recording_id）。⏳ 日付の言い方（〇日以降）は、名前と同じつなぎ方で組む（まだ作っていない＝止める）
        filled = []
        for q in questions:
            if DAY_SLOT in q.text or DAY_SLOT in q.intro:
                if earliest_day is None or today is None:
                    raise ValueError(f"質問 {q.id} の {DAY_SLOT} には、最短で対応できる日と電話の日が要ります（業務設定・CL-2 の暦）")
                w = day_words(earliest_day, today)
                if play_recordings and w not in DAY_VARIANTS:
                    raise ValueError(f"質問 {q.id} の {DAY_SLOT}「{w}」（日付の言い方）は、録音で流す電話ではまだ使えません（名前と同じつなぎ方は、まだ作っていない）")
                q = dataclasses.replace(q, text=q.text.replace(DAY_SLOT, w), intro=q.intro.replace(DAY_SLOT, w),
                                        recording=day_recording_id(q.id, w) if play_recordings else q.recording)
            filled.append(q)
        questions = filled
        self.questions = {q.id: q for q in questions}
        self.order = [q.id for q in questions if not q.ask_if]   # 前の答えで伺うかが決まる質問は、その答えのあとに入れる（_follow_ups）
        self.business_ids = list(self.order)  # 業務の質問（かけ直しの質問の途中で目的の人が出たら、ここから聞く＝_target_back）
        self.limits = limits or Limits()
        self.items = {q.id: Item() for q in questions}
        # 前の通話で取れた答え（instruction.Business.earlier。途中で切れた通話のかけ直しなど＝CL-2 の 4-77）：もう伺わずに続きから聞く。
        # この通話で取った答えではないので、依頼主へ渡す値（export）には入れない（記録は前の通話のもの＝CL-2 の行の項目の記録のまま）。どの答えを
        # もう伺わないかは earlier_kept の1か所（名乗りのあとに最初に伺う質問 first_open と同じ）
        self.earlier = earlier_kept(questions, earlier)
        self.earlier_all = dict(earlier or {})   # 前の通話の答えのまま（ほかのご希望で伺った連絡先を読む＝earlier_records。この通話で決め直しても変えない）
        for qid, v in self.earlier.items():
            it = self.items[qid]
            # 名前は依頼者へ渡す日本語の名前（CL-2 の conversation_io.earlier）＝中の名前へ言い換えて持つ（condition の1か所。⛔ 2026-11-02 まで
            # そのまま入れていて、伺う条件・断る答えの見分けが「わからない」を unknown と見なかった＝CL-2 の連絡帳 4-179 の (b)）
            it.value, it.condition = v.get("value"), condition(v["condition"]) if v.get("condition") else None
            # 言い切っていない答えのお客様の言葉（EARLIER_KEYS の hedged）：ほかのご希望を伺う答えか（_wish_due）・業務設定の言い切っていないときだけ伺う
            # 質問（_business_next）を、この通話で受け取った答えと同じ決まりで決める。前の通話でほかのご希望を伺った結果（wish）は _wish_outcome が読む。
            # 言い切っていない印は取れた答えのときだけ（earlier_hedged の1か所＝電話の始めの決まり earlier_read と同じ）
            it.hedged = earlier_hedged(v)
            it.set(v["state"], "前の通話で取れた答え（続きから聞く）")
            it.told = True   # その答えのあとに伝える一言は、前の通話のこと（この通話では言わない）
        self._ask_if_follow_ups()   # 前の通話で取れた答えで伺う質問も、並びに入れる
        # 最初に伺う質問：名乗りのあとに最初に伺うこと（instruction.Business.first_question）と同じ1か所（伺う条件・先へ進むのを止める答えも同じ決まり）
        self.current: str | None = getattr(first_open(questions, earlier), "id", None)
        self.phase = "asking"            # asking → rechecking → closing
        self.recheck_queue: list[str] = []
        self.awaiting_confirm: str | None = None
        # 言い直しの返事待ちで、「結構です」のような言葉だけを断り・聞き取れないと報告され、確かめへの返事として報告し直させた言い直し
        # （confirm_requested_at の値。_ambiguous_only。言い直しごとに1回）
        self.ambiguous_confirm_at: int | None = None
        self.callee_turns = 0
        self.confirm_requested_at: int | None = None
        self.unavailable_asked = False  # 理由を言わずに取り次げないと言われ、ご事情かを伺った（_transfer_unclear。1通話1回）
        self.full_called = False        # お名前だけでは分からないと言われ、部署・フルネームでお呼びし直した（_who_unclear。1通話1回）
        self.hear_leans: set[int] = set()     # 確かでなかった判定が「こちらの声が届いていない様子」と読んだ発話の番号（on_unsure。Flow.cut）
        self.end_leans: dict[int, set] = {}   # 発話ごとの、確かでなかった判定の「電話を終える言葉か」の読み（on_unsure。決まらないときの場面＝_end_unsure）
        self.end_due: str | None = None       # あとから届いた判定が、電話を終える言葉と読んだ（終わりの理由。次の報告か、報告の無いターンのあとに締める）
        self.refusal_asked = False            # お呼びした方ご本人に、今後このようなお電話をご遠慮したほうがよいかを伺った（_refusal_confirm。1通話1回）
        self.refusal_q: str | None = None     # その質問の番号（答えが決まったら _refusal_decided）
        # 本題に入ったあとの「もういいです。」に本心を伺った（_concern_ask。1通話1回）・その質問の番号（答えが決まったら _concern_decided）・気になる点を
        # 言っていただいた発話（会話AIが応える番＝_asked_now）
        self.concern_asked = False
        self.concern_q: str | None = None
        self.concern_turn: int | None = None
        self.end_used_turn: int | None = None  # 電話を終える言葉かの判定の読みを、もう当てた発話（同じ発話の読みを2回当てない）
        self.refusal_back: tuple | None = None  # ご遠慮の一言を伺う前の位置（いまの質問・言い直しの返事待ち）＝答えが決まったら、そこへ戻る
        self.check_for: str | None = None       # いまの発話の判定が、どの質問について読んだか（発話の始めの質問＝judge_context。_heard_answer）
        self.judging_now: dict[str, tuple] = {}   # いま読んでいる意味の判定（受け持ち → (発話の番号, 読んでいる言葉)。on_judging・on_judged。_end_pending）
        # 発話ごとに出した意味の判定（発話の番号 → 受け持ちの組。on_judging）。届かなかった確かめ（unchecked）は、その発話に出した判定だけを数える
        # （答えの段でその場の判定を使わない形＝meaning_judge.UNUSED_IN では、組だけを出す＝F0）
        self.judged_turns: dict[int, set[str]] = {}
        self.decided: dict[int, str] = {}     # 答えの段で、その発話の確かめで決めた（on_decided）：発話の番号 → 決めた言葉。このあとに届いた読みは記録だけ（_after_decision）
        self.late_checks: dict[int, dict] = {}   # 決めたあとに届いた確かめ（発話の番号 → 確かめの名前 → 読み）：答えにも返事にも当てない。通話後の材料（cut）だけ
        self.answer_leans: dict[int, set] = {}    # 発話ごとの、確かでなかった判定の答えの読み（答えの段。いまの話を先に伺うか＝_topic_first）
        self.hurry_asked = False                  # 急いでいる方に、また改めてお掛けしてよいかを伺った（_hurry_ask。1通話1回）
        self.hurry_q: str | None = None           # その質問の番号（答えが決まったら _hurry_decided）
        # 答えの段で、待つのか、かけ直すのか、お言葉から決まらないときに1回だけ伺う質問 H2（_wait_or_later）：その番号（答えが決まったら _wait_or_later_decided）・
        # 伺う前の位置（待つと決まったら、その質問へ戻って待つ）・伺ったか（1通話1回＝2回目は言葉どおり待つ）・伺った発話（その発話への遅れた報告を H2 の答えにしない＝_wait_route）
        self.wait_q: str | None = None
        self.wait_back: tuple | None = None
        self.wait_asked = False
        self.wait_turn: int | None = None
        self.thinking_turn: int | None = None   # まだ考えている途中と読んで「はい。」で待った発話（_thinking。同じ発話に相づちを重ねない）
        # 迷っているお客様への応じ方の印（{質問の番号: {"point": どういった点でお迷いかを伺った発話, "helped": 選ぶ助けを伝えた発話, "estimate": 目安を伺った発話}}。
        # 決まりは _hesitation の1か所）
        self.hesitating: dict[str, dict] = {}
        self.pending_wait: str | None = None    # 保留にしてよいかを伺い、お返事を待っている質問（Flow._ask_pending・_pending_reply）
        self.repeat_turn: int | None = None   # もう一度言ってほしいと読んで、伺ったことを流し直した発話（_repeat_now。同じ発話に流し直しを重ねない）
        self._asked_next: tuple[dict, dict | None] | None = None   # 次の質問を伺う返事とその中の質問の録音（_next_question。替えるのは _relead_next）
        self.end_doubt: int | None = None         # 電話を終える言葉か判定でも決まらず、いまの話への返事として受け取る発話（_topic_first。答えは向きを確かめる＝_checks）
        # 通話の事実（種類 → 起きた順の記録。_note だけが書く）。業務の判定（DESIGN.md 7-1。CL-2 の judge_task）へ渡す、会話の側が決めるもの：
        # do_not_call＝発信禁止の申告（原則14）／complaint＝怒り・苦情（人へ知らせる印）／end＝通話を終えた理由。どれも根拠の発話つき
        self.facts: dict[str, list[dict]] = {}
        self.end_reason: str | None = None   # 締めた理由（END_LABEL の中の名前。_close だけが書く）
        self.log: list[dict] = []        # 呼ばれた操作と返事（確かめの記録）
        # 名乗りの録音の文（部分 → 文。instruction.Business.replay_parts）。名乗りを録音で流す形のときだけ（無ければ流し直しの操作は無い）
        self.opening = dict(opening or {})  # 写しを持つ（後任の方へ切り替えると、呼ぶ一言・確かめる一言を書き換える＝_to_successor）
        # 業務が書かなかった標準の部分（「AIですか？」の答え）は、共通の一言の録音から流す（phrases.STANDARD_PARTS。どちらから流すかは _part_play の1か所。
        # ✅ 2026-11-02 管理者「システムで共通する名前や対応などは…システムで共通で録音を持っておく」＝CL-2 の 4-188）
        self.system_parts.update({k: rid for k, rid in STANDARD_PARTS.items() if k not in self.opening})
        # 業務で「引き止める」を選んだ業務の一言は、録音で流す電話では業務の録音（replay_parts の busy）。録音が無いまま流そうとしない（設定の誤り＝始めない）
        assert not (busy_ask and play_recordings and "busy" not in self.opening), "引き止めの一言（busy_ask）の録音の部分 busy が、名乗りの録音の部分にありません"
        self.replays = 0
        # 「AIですか？」と聞かれた回数と、決まった言い方（「何か問題でもございましたでしょうか？」）への相手の返事を待っているか（_ai_question）
        self.ai_asks = 0
        self.ai_said = ""  # エーアイかと聞かれて、いま返事を待っている言い方（_ai_question。_now が見せる）
        self.ai_pending = False
        # 名乗りの録音のうち、相手にまだ届いていない部分（on_opening_cut。録音が最後まで流れたら空）
        self.opening_unheard: list[str] = []
        # 目的の人（DESIGN.md 4-3）：繋がるまでは calling の段（いまの質問は無い）。名乗りの終わりは呼ぶ一言なので、用件はまだ届いていない
        self.target = dict(target) if target else target
        if self.successor:
            for d in {id(d): d for d in (self.target, self.opening) if d}.values():   # 在る入れ物だけ（名乗りの録音が無い通話＝AIが話す形でも作れる）
                # この電話の呼ぶ一言（流す録音は target_successor＝_part_play）と、確かめる一言・聞き返し（共通の一言の録音の文＝system_parts）
                d.update({k: v for k, v in SUCCESSOR.items() if k != "target"}, target=d["target_successor"])
                if d.get("target_successor_full"):
                    d["target_full"] = d["target_successor_full"]
                else:
                    d.pop("target_full", None)   # 後任の方の形が無ければ、お呼びし直さない（前任の方をフルネームでお呼びしない）
        if apology_call:
            # お詫びの電話には業務の用件を流さない（どの道でも＝名乗りの流し直し・まだ届いていない部分・代わりの方の申し出。「ご用件は？」には
            # お詫びの録音＝_op_replay_opening）。⛔ 2026-10-24 の見直し（調べ役）：繋いだときだけ用件を外していたので、ほかの道では業務の用件が流れた
            for d in {id(d): d for d in (self.target, self.opening) if d}.values():
                d.pop("purpose", None)
            if not target and not self.opening:
                # 目的のお客様のいない業務のお詫びは、名乗りの録音の終わりに流れる。AIが話す形には流す所が無い（本番は名乗りを録音で流す形だけ＝6-13。
                # ⛔ 2026-10-24 の見直し：どこからも流れないのに「伝えられた」になった）
                raise ValueError("目的のお客様のいない業務のお詫びの電話は、名乗りを録音で流す形だけです")
        # 呼んでいる段は、電話口の方ごとに数える（DESIGN.md 4-3 の区間）：speaker_from＝いま電話口にいる方が話し始めた相手の発話の番号
        # （取り次ぎの判定のあとの発話・代わった方の発話から）。呼ぶ一言・確かめる一言は、流したときの相手の発話の番号を残し、その方に流した
        # 数を数える（呼ぶ一言は1人に2回まで・確かめる一言は1人に1回）。⛔ 2026-10-21 の見直し：通話ごとに数えていたので、受付の方に1回
        # 確かめると、あとで代わった方を確かめられず、回数を使い切ったあとは判定に関係なく繋いでいた（不在・取り次ぎと読んでいても）
        self.speaker_from = 0
        self.target_called: list[int] = [0] if target else []  # 呼ぶ一言を流したときの相手の発話の番号（名乗りの終わりの1回から）
        # 通話の始めの、出た方の第一声を聞く段（on_connect から、名乗りを流すまで）・留守番電話と分かって聞き続けている・「もしもし」を流した回数
        self.answering = self.voicemail_waiting = False
        self.moshimoshi = 0
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
        # 答えの段の、相手の発話ごとの言葉（判定に「その前に相手が言ったこと」として渡す＝judge_context）。✅ 2026-10-31 ユーザー：「前の分との
        # 流れの回答として会話になっていないだけです。それを分かるようにしないと！」（「車で伺うので、駐車場を使わせてください」のあとの、駐車場の
        # 質問への「差し控えさせていただきます」は会話になっていない）⇒ 判定が、前の流れとつながるかを読めるように渡す（呼んでいる段と同じ形）
        self.answer_words: dict[int, str] = {}
        # いまの相手の発話が始まったときの段（on_callee の1か所で決める。答えの段の前の言葉に、呼んでいる段で話された言葉＝繋いだ発話などを入れない）
        self.turn_phase: str | None = None
        # 呼んでいる段の、こちらに尋ねているかの読み（発話の番号ごと＝on_check の asks）と、取り次ぎの判定が区切りを動かす前の区切り（尋ねて
        # いた発話なら戻す＝尋ねた方は、こちらの答えを待っている）
        self.calling_asks: dict[int, str] = {}
        self.other_rejected: int | None = None  # 名乗り・用件で答えようとしたのを差し戻した発話（_asked_other。1発話に1回）
        self.returned_same_at: int | None = None  # 待たせた方が戻ったと、2つの判定がそろって読んだ区切り（speaker_from。_new_ears）
        self.asks_read: dict[int, set[str]] = {}  # 呼んでいる段の発話ごとの、尋ねているかの読み（何を聞かれたか＝_asked_other）
        self.hold_moved: dict[int, tuple[int, bool]] = {}
        # 繋ぐのを止める弱い読み（確からしさが線より下の、「ご本人」ではない読み＝on_unsure）：いまの言葉の分（受け持ち → 見た言葉）と、
        # 終わった発話の分（発話の番号 → 受け持ち）。判定の読み（check・calling_judged）には入れない（_doubted）
        self.doubt: dict[str, str | None] = {}
        self.calling_doubted: dict[int, set[str]] = {}
        self.confirm_part = CONFIRM_PARTS[0]
        self._calling_questions()
        self.target_claimed = False  # いまの目的の人の確かめが、AIの「目的の人」の報告から来たか（_now が「まだ受け付けていない」と伝える）
        if target:
            self.phase, self.current = "calling", None
            self.opening_unheard = ["purpose"] if target.get("purpose") else []
        elif self.current is not None and self.questions[self.current].reading_of:
            self._enter_reading(self.current)
        # お詫びの電話（予約証の目的 apology。CL-2 の apology.for_task の文＝承認したときに聞いていただいた録音。✅ 2026-10-24 ユーザー「OK」）：
        # 業務の質問はしない。お呼びした方（目的のお客様のいる業務）に繋がったら、または名乗りのあと（いない業務）にお詫びの録音を流し、お客様の
        # お返事を聞いてから、お礼の締めで終える。伝えられたかは会話の報告の apology_delivered（Flow.apology_delivered）
        self.apology_call = apology_call
        self.apology_when = apology_when   # お詫びの文が、いつの電話のことか（same_day＝先ほど・later＝先日。CL-2 の apology の文の部分）
        assert apology_when in APOLOGY_PURPOSE, apology_when
        # お詫びを流すように頼んだか（apology_asked）・頼んだあと止まっていないか（apology_played。止まれば on_opening_cut が外す）・流し直したか
        self.apology_asked = self.apology_played = bool(apology_call) and not target   # いない業務は名乗りの録音の終わりに流れる
        self.apology_replayed = False
        # 最後まで流れた録音の文（電話接続側の知らせ＝on_played・on_opening_cut の届いた文）。お詫びを伝えられたか（apology_delivered）の唯一の材料
        self.heard_lines: set[str] = set()
        if apology_call:
            self.order, self.business_ids = [], []
            self.current = None
        # 番号を伺い直す電話（予約証の目的 ask_number_again。instruction.Business.number_again と同じ値）：電話に出た方に、名乗りのあと伺い直す一言
        # （phrases.NUMBER_AGAIN。名乗りの録音の終わり＝Business.first_ask）で番号を伺い、続けて出られる時間を伺って締める。答えはかけ直しの申し出
        # （別の番号）として通話の事実 callback に残す（CL-2 の 4-42：「ふつうの『かけ直しの申し出（別の番号）』として返せば、同じ流れで扱います」）
        self.number_again = number_again
        if number_again:
            assert number_again in NUMBER_AGAIN, number_again
            self.phase, self.callback_why = "asking", "other_number"
            # かけ直しの記録は、人の言葉が届いてから書く（_apply_one。⛔ 2026-10-24 の見直し：通話の初めに書いていたので、留守番電話・ファクス・
            # 無言で切られた電話まで、CL-2 は「人と話した」と読んだ＝かけ直しの約束・発信の拒否に誤った）
            qs = [callback_question(r) for r in CALLBACK_PLAN["other_number"]]
            # 戻るときは「前回お伺いしたお電話番号に…」を言い直さず、番号を伺う一言だけ（Question.plain＝B-5）
            qs[0] = dataclasses.replace(qs[0], text=NUMBER_AGAIN[number_again], recording=f"number_again_{number_again}",
                                        plain=(qs[0].recording or f"callback_{qs[0].callback}", qs[0].text))
            for q in qs:
                self._new_item(q)
            self.order = self.callback_ids = [q.id for q in qs]
            self.current = qs[0].id
        # ほかのご希望を伺っている途中で切れた電話の続き（前の通話の答えに "wish" が無い＝resumed_step）：お詫び（apology_due の「前回はお電話が途中で
        # 切れてしまい大変失礼いたしました。」）のすぐあとに、切れた所から「前回お伺いした（聞くこと）の件で（選択肢）のほかにご希望はございますでしょうか？」
        # と伺う（✅ 2026-11-03 日本時間23時52分36秒 ユーザー「(ア)」。伺い始めるのは _wish_begin の1か所）。目的のお客様のいる業務は、繋がってから
        # （いまの質問は繋がるまで無い＝_connect が並びの先頭から）。名乗りのあとに最初に伺うこと（instruction.Business.first_question）も、同じ
        # resumed_step で決まる（名乗りの録音・AIへの指示文と、流れの制御が同じ質問から始める）。ご承認いただけなかったときの手順の途中で切れた電話の続きも
        # 同じ1か所（前の通話の答えに "point" が無い＝_point_begin の again。✅ 2026-11-04 日本時間18時16分 ユーザー「はい、OK」）。作るのは、前の通話の答えの
        # お客様と話し始める時（_resume_cut の1か所）＝目的のお客様のいない業務は電話の始め（ここ）・いる業務はご本人か後任の方に繋がった時（_connect）
        self.resume_due = not (apology_call or number_again)
        if not target:
            self._resume_cut()

    def _calling_questions(self) -> None:
        """呼んでいる段の判定の質問（judge_context）を作る唯一の場所（始めと、後任の方へ切り替えたとき＝_to_successor）：呼ぶ一言と、確かめる一言
        （確かめへの返事を待つ間は、こちらが実際に聞いた問いを質問にする）。確かめる一言は2つ（CONFIRM_PARTS：確かめ・はっきりしない返事への
        聞き返し）。返事を待っている方を confirm_part に持つ。こちらに尋ねているかの問い合わせ（asks_context）の質問の文も、こちらが最後に聞いたこと
        ＝呼ぶ一言か確かめる一言。目的のお客様の判定は、後任の方をお呼びしたあとも同じ物差し（target_facets）で、「ご本人」は、はじめにお呼びした方
        （判定に、はじめにお呼びした言葉を見せる＝first_called。次の電話で初めから後任の方をお呼びするときは、呼ぶ一言にお名前が入っている）。"""
        target = self.target
        asked_parts = (("T1", "target"),) + tuple((f"T{i + 2}", part) for i, part in enumerate(CONFIRM_PARTS)) if target else ()
        scene = {"seeks_successor": self.successor, "called": self.first_called if self.successor else ""}
        self.calling_question = (Question("T1", target["target"], "yes_no", required=False, conditions=(TARGET_IS_THERE, TARGET_IS_NOT),
                                          named=True, facets=target_facets(False, self.successor), terms=facet_terms(False, self.successor),
                                          **scene) if target else None)
        self.confirm_questions = {part: Question(qid, target[part], "yes_no", required=False, conditions=(TARGET_IS_THERE, TARGET_IS_NOT),
                                                 named=True, facets=target_facets(True, self.successor),
                                                 terms=facet_terms(True, self.successor), **scene)
                                  for qid, part in asked_parts[1:]}
        self.asks_questions = {qid: Question(qid, target[part], "yes_no", required=False, conditions=tuple(ASKS_MEANINGS), meanings=dict(ASKS_MEANINGS),
                                        named=True) for qid, part in asked_parts}
        self.return_question = (Question("R1", target["target"], "yes_no", required=False, conditions=tuple(RETURN_MEANINGS),
                                         meanings=dict(RETURN_MEANINGS), named=True) if target else None)

    def _part_play(self, k: str) -> dict:
        """名乗りの部分 k を流す1つ（唯一の場所）：依頼主の単位の録音（名乗りの部分）か、共通の一言の録音（system_parts）＝後任の方へ切り替えた
        あとの呼ぶ一言・確かめる一言と、業務が書かなかった標準の部分（phrases.STANDARD_PARTS。文は共通の一言の表 RECORDED）。"""
        if k in self.system_parts:
            rid = self.system_parts[k]
            return {"kind": "recorded", "id": rid, "text": (self.target or {}).get(k) or RECORDED[rid]}
        k = self.part_names.get(k, k)
        return {"kind": "opening", "part": k, "text": self.opening.get(k) or self.target[k]}

    # ── 名乗りの録音の途中で相手が話し始めて、録音を止めた（電話接続側の見張りが止めた。DESIGN.md 6-13）
    def on_opening_cut(self, done: list[str]) -> None:
        """届いた文（done）から、まだ届いていない名乗りの部分を覚える。流し直しのとき、AIが選んだ部分に足して流す（_op_replay_opening）。
        ⛔ G-50：用件が届く前に「どちら様ですか」と聞かれると、AIは「名乗り」だけを選んだ（11本中11本）＝用件が一度も届かないまま
        質問へ進む。どこまで届いたかはプログラムが知っているので、足すのはプログラム（AIの選び方に任せない）。"""
        self.opening_unheard = [k for k in OPENING_ORDER if self.opening.get(k) and self.opening[k] not in done]
        self.on_played(done)
        if self.apology_asked and self.apology_call not in self.heard_lines:
            self.apology_played = False   # お詫びが届く前に止まった：お返事のあと、もう一度だけ流す（_apology_cut）

    # ── 録音を最後まで流し終えた（電話接続側が、流した録音の並びごとに呼ぶ。途中で止めたときは on_opening_cut）
    def on_played(self, done: list[str]) -> None:
        """最後まで届いた録音の文を覚える唯一の場所（お詫びを伝えられたか＝apology_delivered の材料）。⛔ 2026-10-24 の見直しで見つけた穴：
        お詫びの録音を流すように頼んだ瞬間に「伝えられた」としていた＝お詫びの途中で切られても、伝えたと報告した。電話に出た方の第一声と
        名乗りの録音の順は決めていないので、相手が話し始めたことからは推し量らない（どこまで届いたかは、流した電話接続側だけが知っている）。"""
        self.heard_lines.update(done)
        if self.voicemail_sending and self.voicemail_sending in done:   # 留守番電話の伝言を最後まで流した（_not_a_person）
            self.voicemail_sending = None
            self._note("voicemail_message")

    # ── 通話の始め：電話に出た方の第一声を聞いてから名乗る（✅ 2026-10-24 ユーザー。CL-1.md 4-45・DESIGN.md 6-13）。電話接続側が呼ぶ4つ
    def on_connect(self) -> dict:
        """電話が繋がった（電話接続側が1回だけ呼ぶ）：こちらは話さずに、出た方の第一声を聞く段に入る。第一声が終わったら on_first_words_end、
        第一声が無いまま silence_s 秒なら on_start_silence を呼ぶ。⛔ 2026-10-24 の見直しで見つけた設計の穴：繋がった瞬間に名乗りの録音を流す形で、
        留守番電話の案内の上に社名・お名前が流れて録音に残りうる（「人ではない相手には話さずに切る」と食い違う）・出た方の名乗りと重なった。
        この段のあとは、今までの始まり（名乗りが流れたあと）と同じ1本の流れ。"""
        if not self.opening:
            # AIが名乗る形（録音を使わない古い試し）は、繋がったらAIが名乗る（本番は名乗りを録音で流す形だけ＝DESIGN.md 6-13）
            raise ValueError("電話に出た方の第一声を待ってから名乗る形は、名乗りを録音で流す通話だけです")
        if self.callee_turns or any(x["op"] == "signal" and x["args"].get("connect") for x in self.log):
            return None   # 繋がったのは1通話に1回（2回目の知らせでは、聞く段に戻さない）
        self.answering = True
        if self.apology_call and not self.target:
            self.apology_asked = self.apology_played = False   # お詫びは、第一声のあとに流す名乗りの終わりに流れる（_opening_reply）
        self.target_called = []   # 呼ぶ一言は、第一声のあとに流す（_opening_reply。第一声への判定の読みは、呼ぶ一言への返事に数えない）
        self.speaker_from = 1     # 最初の電話口の方は、第一声（相手の発話の1つ目）から（第一声を「代わった人」と読まれても、替わった印にしない）
        return self._log_signal({"connect": True}, {
            "result": "wait", "silent": True, "silence_s": FIRST_WORDS_SILENCE_S,
            "message": "電話が繋がりました。何も言わずに、電話に出た方の最初の言葉を聞きます。最初の言葉を聞いたら、出た方が人か（受付の方・"
                       "目的の人など）・留守番電話・自動音声の案内かを report_party で知らせます（声は出しません）。"})

    def on_first_words_end(self) -> dict | None:
        """出た方の第一声の言葉が出揃った（電話接続側が、第一声を聞く段で、会話AIの話し終わりの知らせ on_activity(False) のたびに呼ぶ）：
        出た方が人と分かっていれば、名乗りの録音を流す（_opening_when_ready）。まだ分からなければ None（AIの報告を待つ）。"""
        return self._log_signal({"first_words_end": True}, self._opening_when_ready())

    def _opening_when_ready(self) -> dict | None:
        """名乗りを流す時を決める唯一の場所：第一声を聞く段で、出た方が人と分かり（AIの報告＝report_party）、その方の言葉が出揃った
        （words_complete）とき（✅ 2026-10-24 ユーザー「人なら、第一声が終わってから名乗ります。OK」）。人か分からないうちは流さない
        （留守番電話の案内も、文と文の間で言葉が途切れる＝言葉の終わりだけで流すと、案内の上に名乗りが流れて録音に残る）。どちらかがまだなら None。"""
        if (not self.answering or self.voicemail_waiting or self.phase == "closing" or self.party is None or self.party in NOT_A_PERSON
                or self.callee_turns == 0 or not self.words_complete):
            return None
        self.answering = False
        return self._opening_reply()

    def on_start_silence(self) -> dict | None:
        """繋がってから（「もしもし」のあとも）第一声が無いまま silence_s 秒（電話接続側が呼ぶ）：「もしもし」で、回線が繋がっているか・留守番電話では
        ないかを確かめる（✅ 2026-10-24 ユーザー）。MOSHIMOSHI_TRIES 回のあとも黙っていれば、何も言わずに切る（会話に至らなかった＝締めの記録は残さない）。"""
        if not self.answering or self.voicemail_waiting or self.callee_turns > 0:
            return None
        if self.moshimoshi >= MOSHIMOSHI_TRIES:
            self.answering = False
            return self._log_signal({"start_silence": True}, {"result": "end", "hang_up": True, "silent": True,
                                                              "message": "相手は黙ったままです。何も言わずに終えます。"})
        self.moshimoshi += 1
        item = {"kind": "recorded", "id": "moshimoshi", "text": MOSHIMOSHI}
        return self._log_signal({"start_silence": True}, {
            "result": "wait", "silence_s": FIRST_WORDS_SILENCE_S, "play": [item], "silent": True,
            "message": f"相手の声が聞こえません。{played_note(MOSHIMOSHI)}何も言わずに、相手の言葉を聞きます。"})

    def on_voicemail_wait_end(self) -> dict | None:
        """留守番電話の案内のあと、聞き続けても人が出なかった（電話接続側が voicemail_wait_s 秒のあとに呼ぶ）：留守番電話の扱い（伝言は業務設定で
        最初の1回だけ・無ければ話さずに切る＝_not_a_person）。人が出ていれば None。"""
        if not (self.answering and self.voicemail_waiting):
            return None
        return self._log_signal({"voicemail_wait_end": True}, self._not_a_person("voicemail"))

    def _party_ask(self) -> str:
        """第一声を聞く段で、AIに相手の種別を報告させる文（差し戻し・報告の抜けの知らせの1か所）。"""
        own = f"お呼びする方ご本人が名乗ったら「{PARTY_LABEL['target']}」、" if self.target and not self.number_again else ""
        return ("まだ名乗っていません（名乗りは、プログラムが録音で流します）。何も言わずに、report_party で、いまの言葉が人か（"
                f"{own}そのほかの人は「{PARTY_LABEL['person']}」）・{PARTY_LABEL['voicemail']}の案内・{PARTY_LABEL['ivr']}かを知らせてください。"
                + ("留守番電話の案内のあとに人が出たら、人と知らせます。" if self.voicemail_waiting else ""))

    @staticmethod
    def _prepend_played(r: dict, lines: list[dict]) -> dict:
        """次へ進む返事 r（_advance）の前に、流した録音 lines（名乗り・用件・お詫び・申し伝える一言など）を足す唯一の場所（繋がったとき _connect・
        続きの電話の始めの申し伝え直し _opening_reply）：r に録音が無い（AIが言う＝質問の録音の無い通話・読みの確かめ・相手の質問に答えてから聞く）なら、
        録音のあとに会話AIに話させる（speak）。⛔ 2026-11-06 日本時間13時台の見直しで見つけた：続きの電話の申し伝え直しの枝だけ話させる印が無く、
        録音のあと無言になった。
        足した録音がお詫び（申し伝える一言も＝共通の一言の録音 recorded）か、あいさつ・名乗りから始まるなら、相づちは重ねない（印 led＝_ack_choice。お礼・
        お詫びの一言から始まる返事＝_led・あいさつ・名乗りから始まる返事＝renamed と同じ決まり）。用件から始まるなら今までどおり（代わりの方がお立場に答えたあと
        「ありがとうございます、」＋用件）。⛔ 2026-11-06 日本時間23時22分41秒のご質問の見直し（H1）：印が無く、相づちを決める所が次の質問の種類で選び、続きの
        電話で目的のお客様に繋がったとき、最初に伺う一言がほかのご希望・連絡先なら「はい、前回はお電話が途中で切れてしまい…」「承知しました。前回は…」になった"""
        if "play" not in r:
            r["speak"] = True
        r["play"] = lines + r.get("play", [])
        if lines and (lines[0]["kind"] == "recorded" or lines[0].get("part") in ("greeting", "intro")):
            r["led"] = True
        return r

    def _opening_reply(self) -> dict:
        """第一声のあとに流す名乗り（唯一の場所。並びは instruction.Business.opening_parts と同じ＝試験で比べる）：あいさつ・名乗りのあと、
        目的の人のある業務は呼ぶ一言（第一声でお呼びする方ご本人が名乗ったなら確かめる一言＝✅ 2026-10-24 ユーザー「OK」）、ない業務は
        お詫び（前の電話の事情・お詫びの電話）・用件・最初の質問（番号を伺い直す電話は伺い直す一言）。"""
        lines = [self._part_play(k) if self.target else {"kind": "opening", "part": k, "text": self.opening[k]}
                 for k in ("greeting", "intro") if self.opening.get(k)]
        if self.target and not self.number_again:
            if self._first_words_named_target():
                r = self._ask_target(new_person=False)
                lines += r.get("play") or []
                say = r.get("say")
            else:
                self.target_called.append(self.callee_turns)
                lines.append(self._part_play("target"))
                say = None
            then = self._now()
        else:
            if self.apology_call:
                lines.append(self._apology_play())
                self.apology_asked = self.apology_played = True
            elif not self.number_again:
                told = self.told_again
                lines += self._sorry_lines()
                if told is not None:
                    # 申し伝える一言まで言って切れた続きの電話：お詫び・申し伝える一言のあとは、残りの質問か最後のあいさつ（次へ進む1か所 _advance）
                    texts = "".join(x["text"] for x in lines)
                    r = self._prepend_played(self._advance(f"{played_note(texts)}続けて、", handled=True), lines)
                    if r.get("result") != "end":
                        r["result"] = "opening"
                    # 残りの質問の頭も、続きの電話の最初の質問の頭（繋がったとき _connect と同じ1か所。⛔ 2026-11-06 日本時間23時22分41秒のご質問の
                    # 見直しの A5・H2：ここだけ続きの電話の印を渡さず「続きまして」だった）
                    return self._relead_next(r, [], resumed=self._resumed_call())
                if self.opening.get("purpose"):
                    lines.append(self._purpose_play())
            if self.current is not None:
                lines.append(question_play(list(self.questions.values()), self.current, "".join(x["text"] for x in lines),
                                           resumed=self._resumed_call()))
                self.items[self.current].played = True
            say, then = None, "相手の答えを待ってください。" + answer_wait() if self.current else "相手のお返事を待ちます。"
        texts = "".join(x["text"] for x in lines)
        if not self.target and self.current is None and self._explain_due():
            # 続きの電話の始めに、前の電話で伺ったご納得いただけなかった点をご説明する（伺う質問は無い＝opening_question）：録音のあとにAIが話す
            return {"result": "opening", "play": lines, "speak": True,
                    "message": f"{played_note(texts)}続けて、{self._point_note()}"}
        if say:
            # 確かめる一言の録音が無い（質問を録音で流さない通話）：名乗りの録音のあとに、AIが言う（_connect の最初の質問と同じ形）
            return {"result": "opening", "play": lines, "say": say, "speak": True,
                    "message": f"{played_note(texts)}続けて、{say_note(say)}そのあと、{then}"}
        return {"result": "opening", "play": lines, "silent": True,
                "message": f"{played_note(texts)}何も言わずに、{then}"}

    def _first_words_named_target(self) -> bool:
        """出た方の第一声で、お呼びする方ご本人が名乗ったか（「はい、岡田です」）：AIが「目的の人」と報告した・判定のどれかが「ご本人」と読んだ。
        どちらでも、流すのは確かめる一言（違っていても、確かめへの返事で決める＝_calling_reported）。"""
        return self.party == "target" or TARGET_IS_THERE in self._judged_now().values()

    # ── 相手の発話が届いた（本番では、相手の音声の発話の検出で数える）
    def on_callee(self) -> None:
        if (self.judging and self.end_reason is None and self.end_due is None and self.end_used_turn != self.callee_turns
                and (end := self._end_now()) in END_REASON):
            self.end_due = END_REASON[end]   # この発話の判定が電話を終える言葉と読んだのに、まだ当てていない（次の報告で締める＝_end_due_now）
        if self.phase == "calling":
            self._keep_calling_turn()
        elif self.turn_phase not in (None, "calling") and self.callee_said.strip():
            self.answer_words[self.callee_turns] = self.callee_said.strip()
        self.doubt, self.judging_now = {}, {}
        self.callee_turns += 1
        self.turn_phase = self.phase
        self.callee_said = ""
        self.callee_ended, self.callee_end_at, self.callee_voice_end = False, None, None
        self.check, self.check_said = {}, {}
        self.check_for = self.current   # この発話の判定は、いまの質問について読む（judge_context と同じ時点）
        self.holding = False

    def _judged_now(self) -> dict[str, str]:
        """いまの発話の言葉の判定（受け持ち → 読み。判定のあとに相手が話し続けた分は除く）。呼んでいる段の記録（_keep_calling_turn）と、
        繋いだ発話そのものの読み（通話の事実 connect の turn_reads＝_connect）の1か所。"""
        return {k: v for k, v in self.check.items() if k in JUDGMENTS and self.check_said.get(k) in (None, self.callee_said)}

    def _keep_calling_turn(self) -> None:
        """いまの発話の言葉と、その言葉の判定（判定のあとに相手が話し続けた分は除く）を、呼んでいる段の記録に残す（唯一の場所）：発話が
        終わったとき（on_callee）と、その発話でかけ直しへ移るとき（_callback。⛔ 2026-10-22 の見直しで見つけた漏れ：移ったあとの on_callee は
        呼んでいる段ではないので、かけ直しを決めた発話の「不在」の読みが残らず、かけ直しの質問の途中で目的の人かを確かめに戻ったあと、
        違う方に岡田様をお呼びし直していた）"""
        if not self.callee_said.strip():
            return
        self.calling_words[self.callee_turns] = self.callee_said.strip()
        if done := self._judged_now():
            self.calling_judged.setdefault(self.callee_turns, {}).update(done)
        if doubted := {k for k, said in self.doubt.items() if said in (None, self.callee_said)}:
            self.calling_doubted.setdefault(self.callee_turns, set()).update(doubted)

    # ── 相手の同じ発話を、張り替えた新しいつなぎへ送り直す（DESIGN.md 6-7）：相手は話していないので数えず、文字だけを受け取り直す。
    #    ⛔ 2026-10-17 に見つけた穴：送り直しで on_callee を呼ばないまま、新しいつなぎの聞き取りを足していた（同じ言葉が2回たまる）
    def on_resend(self) -> None:
        self.resent_turn = self.callee_turns   # この発話は張り替えた（張り替えたあとも戻らなければ、こちらの不具合で終える＝on_ai_failed）
        self.callee_said = ""
        self.doubt = {}
        self.callee_ended = self.callee_speaking = False  # 知らせは、新しいつなぎから届き直す（位置も、新しいつなぎの音の上で数え直す）
        self.callee_end_at = self.callee_voice_end = None
        self.check, self.check_said = {}, {}

    # ── 会話AIが、相手の発話に、声も受け付けた報告も返さなかった（黙った・報告がどれも差し戻されて声も無い・流さなかった声のあとも報告が無い）
    def on_ai_failed(self, why: str) -> dict:
        """張り替えるか、こちらの不具合で電話を終えるかを決める唯一の場所（DESIGN.md 6-7・6-8）：この発話でまだ張り替えていなければ張り替える
        （{"result": "swap"}＝電話接続側がつなぎを開き直し、相手の同じ発話を送り直す＝on_resend）。張り替えたあとも戻らなければ、会話AIの異常
        （ai_malfunction）で終える（_trouble）。why＝何が無かったか（記録に残す）。
        ⛔ 2026-10-28 の見直しで見つけた穴：張り替えは1つの発話に1回までで、張り替えたあとにまた黙ると、黙りの見張りを外して待つだけだった
           （試作は最大90秒）＝お客様には無言が続いた（Intent.md 9-4「会話AIが通話中に落ちた」の「相手を無言で放置しない」に届いていなかった）。
        通話の途中で会話AIのつなぎが切れたとき（live_util.is_session_drop）も、ここで決める。張り替えた印（resent_turn）は、張り替えると決めた
        ここで付ける（⛔ 2026-11-04 まで送り直し＝on_resend だけが付けていたので、送り直す前（流れの制御がまだその発話を受け取る前）に切れると、
        切れるたびに張り替え続けえた）。"""
        if self.resent_turn == self.callee_turns:
            return self._trouble("ai_malfunction", f"張り替えて相手の発話を送り直しても、会話AIの声も受け付けた報告も戻らない（{why}）")
        self.resent_turn = self.callee_turns
        self.on_ai_trouble({"model": CONVO_AI, "error": why, "glitch": True})   # 張り替えて続ける＝おかしな動き（止めずに警告。on_ai_trouble の1か所）
        return {"result": "swap", "why": why}

    def on_ai_unreachable(self, error: str, ai: str = CONVO_AI) -> dict:
        """通話で使うAIのつながり（ai＝会話AI か 別の耳。判定はつながりを持たない）を開けない（唯一の場所。開くのが止まったときも同じ＝開く所が
        判定と同じ「止まったとみなす秒」で打ち切る：live_util.open_session・ears.Ear）。電話の前（お客様の言葉がまだ1つも届いていない）なら、
        止まったAIとして残すだけ（{"result": "not_called"}＝電話接続側は電話をかけない。発信の門が1回で新しい電話を止める＝CL-2 の連絡帳
        4-179 の (a)・2章 Q58：止まったかを決めるのは会話の側の1か所。⛔ 2026-11-02 まで送っておらず、CL-2 が生の誤りを3回数えて止めていた。
        ✅ 2026-10-30 夜 ユーザー：「…その他に使う AI のどれかが止まったら、AI の
        不具合として回復するまで一時停止してください。」＝別の耳も同じ。⛔ 2026-11-04 まで別の耳を開けない誤りは、ここを通らずに通話の道具の
        外へ上がり、報告が作られなかった）。通話の途中：会話AI（張り替えのつながりを開けない）なら、甚大な問題として、お詫びして終える
        （_trouble。止まったAIにも数える）／別の耳なら、止まったAIとして残して電話は続ける（✅ 2026-10-31 管理者：「通話中は止めません。
        ただし、次の電話はかけず、確かめが通れば自動で再開します。」。別の耳は通話の始めに1回開くだけなので、いまの通話の道具では通らない）。"""
        if self.callee_turns == 0:
            self.on_ai_trouble({"model": ai, "error": error, "outage": True})
            return {"result": "not_called", "why": error}
        if ai != CONVO_AI:
            self.on_ai_trouble({"model": ai, "error": error, "outage": True})
            return {"result": "continue", "why": error}
        return self._trouble("ai_malfunction", f"会話AIのつながりを開けない（{error}）")

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
        return Flow._meaning_of(self.check)

    @staticmethod
    def _meaning_of(check: dict) -> str | None:
        got = [check[k] for k in JUDGMENTS if k in check]
        return (got[0] if all(g == got[0] for g in got) else UNDECIDED) if got else None

    # ── 答えの報告を当てる相手の言葉（唯一の場所）：ふだんは、いまの発話。相手に応えたあとで、その前の発話の答えを報告した
    #    （answer_held・_held_words）ときだけ、その発話の言葉と確かめ（judged）。値の形・曖昧の決まり・意味の判定・言い直しの言葉は、
    #    どちらでもこの2つから決める（次へ進むときの「相手が質問をしているか」は、いまの発話のまま＝_question_to_us）
    @property
    def heard_said(self) -> str:
        return self.judged["said"] if self.judged else self.callee_said

    @property
    def heard_check(self) -> dict:
        return self.judged["check"] if self.judged else self.check

    @property
    def judging(self) -> bool:
        """この通話で意味の判定を使うか（use_checks）。"""
        return any(k in self.checks_used for k in JUDGMENTS)

    # ── この通話で使う確かめ（電話接続側が、通話の始めに1回呼ぶ。CHECKS の名前）
    def use_checks(self, *kinds: str) -> None:
        assert all(k in CHECKS for k in kinds), kinds
        self.checks_used = tuple(kinds)

    # ── 意味の判定を出した・答えが返った（電話接続側が、判定を出すたびと、答え・確かでない読み・落ちたのどれで終わったときに呼ぶ）。
    #    まだ答えの返っていない判定があるか（_end_pending）を数える唯一の材料。turn・said＝読んでいる発話の番号と言葉
    def on_judging(self, kind: str, turn: int | None = None, said: str | None = None) -> None:
        if kind in JUDGMENTS:
            self.judging_now[kind] = (self.callee_turns if turn is None else turn, said)
            self.judged_turns.setdefault(self.callee_turns if turn is None else turn, set()).add(kind)

    def on_judged(self, kind: str, turn: int | None = None, said: str | None = None) -> None:
        if self.judging_now.get(kind) == (self.callee_turns if turn is None else turn, said):
            del self.judging_now[kind]

    # ── 電話接続側が、いまのお客様の発話の確かめを待つのをやめた（待つ長さが切れた・待つものがそろった。報告を受ける前・報告の無いターンに動く前＝
    #    convo.check_handler の handle・settle）：答えの段では、この発話はここまでに届いた確かめで決める（唯一の印）
    def on_decided(self, turn: int | None = None) -> None:
        """答えの段は、待つ長さ（2秒）の中で1回だけ決め、あとから覆さない（✅ 2026-11-07 日本時間8時57分04秒 ユーザー「はい、その形でいいですが…」＝d1-ultra と Jev を
        同時に出し、2秒の中に1回だけ決め、あとから覆す道を外す・2秒を超えた読みは通話後の確かめ。3時14分01秒「判定の優先で順位っていうのはそもそも決まっていますよね。
        そして2秒を超えた時にどうするのかということも決まっているはずですよね。後から判定が覆るということ自体が正しい設定ならないんじゃないですか。」）。
        決めたあとに届いたこの発話の確かめ（意味の判定・別の耳・前の話の答え）は記録だけ（on_check・on_unsure＝_after_decision の1か所）：受け取った答えには late として
        残り、届かなかった確かめ（unchecked＝通話後の確かめが録音でやり直す）に数える。決める時に判定の確かな読みが1つも無ければ、受け取らずにその場で確かめる
        （_checks の MISSED_WHY）。印を付けるのは、言葉が出揃った答えの段の発話だけ（出揃う前の報告は待たせて報告し直させる＝_wait_for_words・決めていない。お呼びする
        段は、遅れて届いた読みも、その方の読みとして数える決まりのまま＝9-4 の (5)）。お客様がこのあとに話し続けたら、その言葉で決め直す（言葉ごとに1回）。"""
        turn = self.callee_turns if turn is None else turn
        if turn == self.callee_turns and self.phase != "calling" and self.words_complete:
            self.decided[turn] = self.callee_said

    def _late_to_taken(self, turn: int, kind: str, value, said: str | None) -> None:
        """受け取った答え（taken）の発話の確かめが、受け取ったあとに届いた：答えには当てず、記録だけ（唯一の場所＝on_check）。受け取った時が、その答えを決めた時
        （on_decided と同じ決まり＝待つ長さの中で1回だけ決め、あとから覆さない）。記録は late（届かなかった確かめ unchecked に数え、通話後の確かめが録音でやり直す）。
        ⛔ 2026-11-08 日本時間9時台（F0）まで、ここで受け取ったときの材料に当て直し（_late）、食い違えば「-」にして締めの前に伺い直し、その答えで進めた手順の段
        （ご承認の手順・ほかのご希望）も戻していた＝あとから判定が覆る作り（11-07 3時14分01秒のお言葉）。
        同じ番号の質問を伺い直す前の回の記録（taken_before）にも残す（⛔ 2026-11-08 日本時間10時台に見つけた：次の回の答えで記録が上書きされると、前の回の
        発話の遅れた読みが残らなかった）"""
        for t in [*self.taken.values(), *(t for _, _, t in self.taken_before)]:
            if t["turn"] == turn and kind not in t["check"] and (said is None or said == t["said"]):
                t.setdefault("late", {})[kind] = value

    def _after_decision(self, turn: int, said: str | None) -> bool:
        """届いた確かめ（その発話の番号 turn・見た言葉 said＝None は発話まるごと＝別の耳）が、その発話を決めた（on_decided）あとのものか（唯一の決まり）。
        決めたあとにお客様が話し続け、その新しい言葉を見た判定（いまの発話・いまの言葉）だけは、次に決める材料（決めたあとではない）。"""
        d = self.decided.get(turn)
        return d is not None and not (turn == self.callee_turns and said is not None and said == self.callee_said != d)

    def _end_pending(self) -> bool:
        """いまの発話の言葉を読んでいる意味の判定のうち、まだ答えが返っていないものがある（混んでいる・止まりかけている。on_judging・on_judged）。"""
        return any(t == self.callee_turns and said in (None, self.callee_said) for t, said in self.judging_now.values())

    def waits_again(self, name: str | None, args: dict | None) -> bool:
        """電話接続側が、報告を受け取る前（・報告の無いターンのあとに締めるかを決める前）の待ちのあとで、もう一度だけ判定を全部待つか（最大で
        待つ長さ＝limits.hold_s。唯一の決まり）：電話を終えるかを決める所で、まだ答えの返っていない判定がある＝会話AIが終わりを報告した、
        または届いた判定の読みに電話を終える読み（確かでない寄りも）がある。締める・もうかけない・苦情は取り消せないので、届いた一部の読みでは
        決めない。✅ 2026-11-01 日本時間20時56分 ユーザー：(e)「(エ) の案でよいでしょうか。OK」（「もう一度だけ、最大2秒黙って待ちます。届けば、
        その読みで決めます」）。それでも届かなければ、会話AIの終わりの報告は _end_late、ほかは届いた読みで決める（_end_due_now）。"""
        if not self.judging or self.end_reason is not None or self.trouble is not None or not self._end_pending():
            return False
        if name == "request_end" and (args or {}).get("reason") != "done":
            return True
        return any(getattr(v, "end", None) in (*END_REASON, UNDECIDED) for v in self._judged_now().values()) or bool(self._end_lean())

    # ── 確かめが届いた（唯一の入口）：別の耳（会話AIとは別の音声認識。DESIGN.md 6-10 の (5)。G-15）の文字、または意味の判定（会話AIとは
    #    別の文字のAI。meaning_judge。6-10 の (6)。G-53）の答え（中の名前・HOLD・HANDOVER・UNDECIDED）。turn＝どの相手の発話の確かめか
    #    （on_callee で数えた番号。省けば、いまの発話）。落ちた・時間切れは呼ばない（届いていない＝unchecked）。
    #    その発話を決めたあと（on_decided）・受け取った項目の発話なら、答えには当てず記録だけ（_after_decision・_late_to_taken）
    def on_check(self, kind: str, value: str | None, turn: int | None = None, said: str | None = None) -> None:
        """said：その確かめが見た相手の言葉（意味の判定。None＝発話まるごと＝別の耳）。いまの言葉と違えば（判定のあとに相手が話し続けた）、
        いまの発話の確かめにも、その言葉より後の言葉で受け取った項目にも当てない（同じ言葉で受け取った項目には当てる）。"""
        assert kind in CHECKS, kind
        turn = self.callee_turns if turn is None else turn
        if value is None:
            return
        if kind in AHEAD_CHECKS:
            return self._ahead_check(kind[:-len(AHEAD_SUFFIX)], value, turn)
        if self._after_decision(turn, said):
            # その発話を決めた（on_decided）あとに届いた：答えにも返事にも当てず、記録だけ（受け取った答えには late＝届かなかった確かめ unchecked に数え、通話後の確かめが
            # 録音でやり直す・切れ方の材料 cut）。⛔ 2026-11-08 日本時間9時台（F0）まで、遅れて届いた読みで受け取った答えを当て直し（_late）、待つと応えたあとに伺い直した
            self.late_checks.setdefault(turn, {})[kind] = value
            self._late_to_taken(turn, kind, value, said)
            return
        current = turn == self.callee_turns and (said is None or said == self.callee_said)
        if current:
            self.check[kind], self.check_said[kind] = value, said
        elif (kind in JUDGMENTS and turn < self.callee_turns and self.end_reason is None and self.end_due is None
              and self.end_used_turn != turn and getattr(value, "end", None) in END_REASON):
            self.end_due = END_REASON[value.end]   # 前の発話の判定が、あとから届いて電話を終える言葉と読んだ（届いた時点で同じ決まりで当てる）
        if (self.phase == "calling" and kind in JUDGMENTS and self.target_from <= turn < self.callee_turns
                and said in (None, self.calling_words.get(turn))):
            # 終わった発話の判定が遅れて届いた：その方の「ご本人」の読みとして数える（_target_readings。⛔ 2026-10-21 の見直し：いまの発話の判定
            # だけを数えていたので、判定が遅れて目的の人かを確かめると、確かめの間に届いた前の発話の判定を使わず、また確かめていた）
            self.calling_judged.setdefault(turn, {})[kind] = value
        if (self.phase == "calling" and kind in RETURN_CHECKS and current and turn == self.speaker_from
                and sum(self.check.get(k) == RETURNED_SAME for k in RETURN_CHECKS) >= 2):
            self.returned_same_at = self.speaker_from   # 待たせた方が戻った（2つがそろった）：この方は名乗りを聞いている（_new_ears）
        if self.phase == "calling" and kind in ASKS_CHECKS and (current or turn < self.callee_turns):
            self.asks_read.setdefault(turn, set()).add(value)   # 何を聞かれたか（Flow._asked_other）
            if value in ASKED or self.calling_asks.get(turn) not in ASKED:   # どちらか1つでも「尋ねている」なら、その発話は質問
                self.calling_asks[turn] = ASKS_YES if value in ASKED else value
            if value in ASKED and turn == self.callee_turns and turn in self.hold_moved:
                # 取り次ぎと読まれた発話が、こちらへの質問だった（「岡田ですね。失礼ですが、どちら様でしょうか？」）：尋ねた方は電話口を
                # 離れず、こちらの答えを待っている ⇒ 区切りを戻す（次の発話も同じ方。いまの発話のうちだけ＝次の方が話し始めたら戻さない）
                self.speaker_from, self.passed_on = self.hold_moved.pop(turn)
        if (self.phase == "calling" and ((kind in JUDGMENTS and (value == HOLD or says_new(value))) or (kind in HOLD_READINGS and value == HOLD))
                and (current or turn < self.callee_turns) and not (value == HOLD and self.calling_asks.get(turn) == ASKS_YES)):
            # 取り次がれた：このあと出る目的の人は、名乗りを聞いていない別の方。前の発話の判定が遅れて届いても同じ（その方が取り次いだ事実は
            # 変わらない）。⛔ 2026-10-19 G-62：受付の方の「少々お待ちください。」の判定が、代わった方が話し始めたあとに届き、取り次がれたことが
            # 残らず、代わった岡田様に、あいさつと名乗りを流さずに用件から始めた。こちらに尋ねた発話の「取り次ぎ」は、区切りを動かさない（上）
            before = (self.speaker_from, self.passed_on)
            self._new_speaker(turn, after=value == HOLD)
            if value == HOLD and self.speaker_from != before[0]:
                self.hold_moved.setdefault(turn, before)
        if self.phase != "calling" and self.asking_reception and kind in JUDGMENTS and value == HOLD and current:
            self.callback_held = True  # かけ直しの質問の途中の取り次ぎ（AIが報告しなくても。_op_replay_opening・_party）
        if self.phase != "calling" and kind in JUDGMENTS and (c := (self.facts.get("connect") or [None])[-1]) and c["turn"] == turn:
            # 繋いだ発話の判定が、繋いだあとに届いた：通話の事実に残す（通話後の検証が、判定なしで繋いだ通話を確かめる。DESIGN.md 6-12）
            c.setdefault("late", {})[kind] = value
        self._late_to_taken(turn, kind, value, said)
        if (h := self.answer_held) is not None and h["turn"] == turn != self.callee_turns and kind not in h["check"] and said in (None, h["said"]):
            h["check"][kind] = value  # 覚えておいた言葉の確かめが、次の発話のあとに届いた：その言葉の答えの報告に当てる（_held_words）

    def on_unsure(self, kind: str, value: str, turn: int | None = None, said: str | None = None, answer: bool = True,
                  end: tuple = (), hear: tuple = ()) -> None:
        """判定の受け持ちが、確からしさが線より下の読み（value）しか返さなかった（meaning_judge.NoJudgment の unsure＝代わりの Jev）。
        end・hear＝確かでなかった読みの寄りの全部（電話を終える言葉か・こちらの声が届いていない様子か＝meaning_judge.leans の1か所。NoJudgment の
        end_lean・hear_lean。組が答えたときの Reading.end_lean・hear_lean と同じ集まり）。
        answer＝答える側にも数えてよい読みか（False＝止める側だけの読み＝裏で出した代わり。決まりは unsure_counts の1か所）。
        目的の人を呼んでいる段（繋ぐのは取り消せない）では、「ご本人」ではない読みだけを「決められない」として数える（弱い読みは、繋ぐのを
        止める側にだけ使い、繋ぐ側には使わない）。答えの段では数えない（届かなかった確かめ＝通話後の検証。答えは遅れて届いた判定でも当て直せる）。
        ⛔ 2026-10-21 G-57 tc11（確かめにいいえ・L4）：中身を見ずに「決められない」と数えたので、確かめへの「はい、そうです。」を Jev が
           「ご本人」（確からしさ 0.94）と読んだのに繋がず、岡田様にお呼びし直した
        ⛔ 2026-10-21 G-57 tc10（確かめにいいえ・L4）：最初の方の「はい、お電話変わりました。」（前の言葉なし）を、AIは決まりに反して「目的の人」と
           報告し、3.5 Flash Lite も「ご本人」と読んだ。もう1つの受け持ちは Sonnet が止まっていて、代わりの Jev は「代わった人が話し始めた」と
           読んだが確からしさ 0.89 で線より下＝捨てて「答えなし」と同じに数え、読みが2つそろったとして別の方（山田様）に繋いだ"""
        if kind in AHEAD_CHECKS:
            # 読み直した判定の確かでない読み：答える側の数え方（unsure_counts）で、先に言われた答えの確かめにだけ当てる
            return self.on_check(kind, unsure_counts(kind[:-len(AHEAD_SUFFIX)], value, answer), turn, said)
        if HEAR_LOST in hear and kind in JUDGMENTS:
            # 確かでなかった読みの「こちらの声が届いていない様子か」：切れ方の callee_cannot_hear にだけ使う（読み過ぎても取り消せる側＝Flow.cut）
            self.hear_leans.add(self.callee_turns if turn is None else turn)
        if self._after_decision(self.callee_turns if turn is None else turn, said):
            return   # その発話を決めた（on_decided）あとに届いた確かでない読み：答えにも返事にも当てない（切れ方の材料の寄りは上で残した）
        if (ends := set(end) & set(END_REASON)) and answer and kind in JUDGMENTS:
            # 確かでなかった読みの「電話を終える言葉か」：どちらの段でも、決まらないときの場面にだけ使う（止める側だけの読みは入れない＝_end_unsure）
            self.end_leans.setdefault(self.callee_turns if turn is None else turn, set()).update(ends)
        if (self.phase != "calling" and answer and kind in JUDGMENTS and value and turn in (None, self.callee_turns)
                and said in (None, self.callee_said)):
            # 確かでなかった読みの答え：受け取りには使わない（答えの段の決まり）。いまの話への返事とも取れるかだけに使う（_topic_first）
            self.answer_leans.setdefault(self.callee_turns, set()).add(value)
        if self.phase != "calling" or unsure_counts(kind, value, answer) is None:
            return
        if kind in JUDGMENTS and unsure_counts(kind, value, answer) == HOLD:
            # 弱い「取り次ぎを待つ」：取り次ぎの読みとして数える（黙って待つ・このあと出る方は新しい方＝on_check の1か所。unsure_counts）
            self.on_check(kind, HOLD, turn, said)
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
        self.on_check(kind, unsure_counts(kind, value, answer), turn, said)

    def judge_context(self) -> tuple["Question | None", str | None, str | None]:
        """意味の判定に渡すもの（相手の発話の始めに決める）：いまの質問と、こちらが言い直して確かめた言葉（言い直しの返事を待っているとき）と、
        その前に相手が言ったこと。目的の人を呼んでいる段（calling）では、呼ぶ一言を質問にし（目的の人かを確かめたあとは、その言い方を添える）、
        その段の相手の言葉（直前の3つまで）を添える（取り次いだ方の言葉で、代わった方が誰かが決まる＝target_facets）。答えの段も、その段の
        相手の言葉（直前の3つまで＝answer_words）を添える（前の話だけから決まる答えを判定が別に読み、いまの言葉の答えとプログラムが比べる＝
        meaning_judge.PRIOR・off_talk）。
        ⛔ 2026-10-31：答えの段では、その前の言葉を渡していなかった（「車で伺うので、駐車場を使わせてください」のあとの「差し控えさせて
           いただきます」を、質問とだけ並べて読む＝「利用しない」と読むのは当然で、会話になっていないことが分からない）
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
            return self.for_judge(q), None, before_words(q, list(self.calling_words.values()))
        if self.answering:
            return None, None, None   # 出た方の第一声（「はい、〇〇でございます」）は、まだ流していない最初の質問の答えではない
        q = self.questions.get(self.current) if self.current else None
        before = before_words(q, list(self.answer_words.values()))   # 答えの段のその前の言葉（直前の3つまで＝呼んでいる段と同じ・システムの質問には渡さない）
        return self.for_judge(q), (self._judge_text(self.awaiting_say) if self.awaiting_confirm is not None else None), before

    def for_judge(self, q: "Question | None") -> "Question | None":
        """判定に見せる質問（判定へ質問を渡す唯一の場所＝judge_context・asks_context・returned_context）：質問の文の名前を、声で読ませる形から
        判定に見せる形（書いてあるとおり＋読み＝instruction.Business.judge_names）にする。声で流す文・質問そのもの（選べるもの・項目）は変えない。"""
        if q is None:
            return None
        text, called = self._judge_text(q.text), self._judge_text(getattr(q, "called", ""))
        return q if (text, called) == (q.text, getattr(q, "called", "")) else dataclasses.replace(q, text=text, called=called)

    def _judge_text(self, text: str | None) -> str | None:
        for spoken in sorted(self.judge_names, key=len, reverse=True):   # 長い名前から先に（短い名前が長い名前の中に入っていても1回だけ）
            text = text.replace(spoken, self.judge_names[spoken]) if text else text
        return text

    def asks_context(self) -> tuple["Question", str | None] | None:
        """呼んでいる段の、こちらに尋ねているかの問い合わせ（電話接続側が、目的の人の判定と同時に出す。確かめの名前 "asks"）：(質問, その前に
        相手が言ったこと)。呼んでいる段でなければ None（答えの段の質問は、AIが答える＝これまでどおり）。"""
        if self.phase != "calling":
            return None
        q, _, before = self.judge_context()
        return self.for_judge(self.asks_questions[q.id]), before

    def returned_context(self) -> tuple["Question", str | None] | None:
        """取り次ぎで待ったあと、新しい方の区切りの最初の発話だけ：待たせた方が戻ったかの問い合わせ（電話接続側が、目的の人の判定と同時に2つの判定へ
        出す。確かめの名前 returned・returned2）：(質問, その前に相手が言ったこと)。それ以外は None（RETURN_MEANINGS）。"""
        if (self.phase != "calling" or not self.passed_on or self.return_question is None or self.callee_turns != self.speaker_from
                or self.callee_turns == 0):
            return None
        _, _, before = self.judge_context()
        return self.for_judge(self.return_question), before

    def _new_ears(self) -> bool:
        """電話口の方が、まだこちらの名乗りを聞いていないか（お呼びした方に繋がない道＝かけ直し・お立場・後任の方・お呼びし直し の1か所）：
        取り次ぎで待ったあとに話した方（passed_on）でも、待たせた方が戻ったと2つの判定がそろって読んだ方は、もう聞いている（RETURN_MEANINGS）。"""
        return self.passed_on and self.returned_same_at != self.speaker_from

    # ── AIのターンが、声は出したのに報告なしで終わった・呼んでいる段で決めずに待たせたまま終わった（電話接続側が呼ぶ）：報告を促す知らせ
    #    （出さないなら None）。
    def unreported_note(self, muted: bool = False) -> str | None:
        """いまの質問の答えだと、意味の判定が決めた相手の言葉（on_check の judge が選択肢・どちらでも・わからない・答えたくないのどれか）に、
        AIが報告しなかったときだけ、1つの相手の発話に1回まで。知らせには、いまの質問の文を入れない（報告することだけ）。
        この知らせへのAIの声は、報告より前のものは相手に流さない（電話接続側）。
        ⛔ 2026-10-17：記録の通話で、相手の「はい。」「はい、先週しました。」（雑音）のあと、AIが短く声を出しただけで報告せずに終えた回が34回。
           プログラムは気づく手立てを持っていなかった（PHASE0「答えの報告が抜けたとき」の案）。
        ⛔ 2026-10-17 の1回目：判定を見ずに、相手の言葉があれば出し、知らせに「いまの質問は…」を入れた ⇒ 取り次ぎ（「代わりますので少々
           お待ちください」）のあとにも出て、声の型 S4 の3本中2本で、代わった方にAIが自分で名乗って質問した・流し直しを頼まなかった
           （6-7 の G-19 と同じ形：AIへの文字の合図が会話を乱す）。答えの3本では、AIは3本とも黙って正しく報告した。
        muted＝このターンのAIの声を、電話接続側が流さなかった（speech_allowed）。受け付けた報告が無ければ、流していないことと、報告することを知らせる
        （止めただけでは、相手には無言が続く）。"""
        if self.answering:
            # 出た方の第一声を聞く段：言葉が出揃ったのに、AIが、出た方が人か・留守番電話か・自動音声の案内かを報告していない（名乗りは、その報告の
            # あとに流す＝_opening_when_ready。報告が無いと、相手には無言が続く）。1つの相手の発話に1回まで
            # 留守番電話の案内を聞き続けている間も、新しい言葉ごとに（案内のあとに人が出たかもしれない。⛔ 2026-10-24 G-57 J1：聞き続けている間は
            # 知らせず、案内のあとに出た方の言葉を、AIが人と報告しないまま通話が終わった）
            reported = any(x["op"] == "report_party" and x["callee_turns"] == self.callee_turns and x["reply"].get("result") != "rejected"
                           for x in self.log)
            if self.callee_turns == 0 or not self.words_complete or self.nudged_turn == self.callee_turns or reported:
                return None
            self.nudged_turn = self.callee_turns
            return notice(f"{self._unsent(muted)}相手の言葉（「{self.callee_said.strip()[:40]}」）を聞きました。"
                          f"{self._party_ask()}", quiet=True)
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
            unsent = self._unsent(muted)
            if unsent and not replied:
                # 操作を呼ばずに（または差し戻されたあとに）話した：その声は流していない＝相手には無言（⛔ 2026-10-22 G-57 tc31：所在地を聞かれた
                # L4 が、操作を呼ばずに業務の情報に無い地名で答えた。声を止めるだけでは相手が待ち続けるので、報告し直させる）
                self.nudged_turn = self.callee_turns
                if self._asking_now():
                    return notice(f"{unsent}{self._asking_note()}", quiet=True)
                return notice(f"{unsent}相手の言葉（「{said}」）への応じ方を、何も言わずに報告してください"
                              f"（{self._report_hint()}）。{NOT_CONNECTED}", quiet=True)
            if quiet and self._asking_now():
                self.nudged_turn = self.callee_turns
                return notice(self._asking_note(), quiet=True)
            away_told = now and last["reply"].get("away")  # かけ直しへ進むように伝えた（_away）
            if away_told and self._hold_now():
                # 取り次ぎの読みが、あとから届いた：かけ直しは、代わった方の言葉を聞いてから（⛔ 2026-10-22 G-57 tc22：取り次ぎの読みが4.6秒で届いた）
                self.nudged_turn = self.callee_turns
                return notice(f"相手の言葉（「{said}」）は、取り次ぎを待つ言葉でもありました（意味の判定）。何も言わずに、"
                              "代わった方が話し始めるのを待ちます（かけ直しの申し出は、代わった方の言葉を聞いてからです）。", quiet=True)
            if away_told and not any(x["op"] == "request_callback" for x in self.log if x["callee_turns"] == self.callee_turns):
                # かけ直しへ進むように伝えたのに、AIが進まずにターンを終えた（⛔ 2026-10-22 G-57 tc22：代わった方の番でかけ直しへ進まず、次の言葉で
                # 進んだので、相手がもう言った戻る時刻を聞き直し、話がずれた）
                self.nudged_turn = self.callee_turns
                return notice(self._away_text(), quiet=True)
            if quiet and self._away_now() and not self._hold_now():  # 取り次ぎの最中は知らせない（代わった方が話したら、その言葉で決める）
                self.nudged_turn = self.callee_turns
                return notice(f"{self._away_text()}何も言わずに報告します。", quiet=True)
            if not (waited or replied):
                return None
            self.nudged_turn = self.callee_turns
            what = "は、目的の人かを聞いた返事です" if replied else "は、最後まで届きました"
            again = "" if replied else "もう一度"
            return notice(f"{unsent}相手の言葉（「{said}」）{what}。何も言わずに、この言葉で、{again}報告してください"
                          f"（{self._report_hint()}）。{NOT_CONNECTED}", quiet=True)
        if self.phase == "closing" or self.nudged_turn == self.callee_turns:
            return None
        if unsent := self._unsent(muted):
            # 答えの段で、操作を呼ばずに（または差し戻されたあとに）話した：その声は流していない＝相手には無言 ⇒ いま求めていることで報告し直させる
            self.nudged_turn = self.callee_turns
            return notice(f"{unsent}{self._now()}", quiet=True)
        if self._asked_now() is not None and not any(x["callee_turns"] == self.callee_turns for x in self.log):
            # いまの質問・言い直しに答える代わりにこちらへ尋ねられたのに、会話AIが何も呼ばなかった（報告して差し戻された回は、その返事で伝えてある）。
            # ここに来るのは、声を流した（muted でない）ときだけ（流さなかった声は、上の _unsent の知らせ）＝会話AIが操作を呼ばずに声で答え、その声は
            # お客様に流れた（録音で流さない通話）：もう答えたので、答えさせる知らせは出さずに、お客様の返事を待つ（答えの文は確かめていないので、記録には
            # 答えたことだけを残す）。⛔ 2026-11-06 日本時間5時56分55秒のご質問のあとの読み直しで見つけた：知らせが「答える番」と言い、同じ答えを2回
            # 言わせうる形だった。声で答えたときの印は報告の無いターンの入口と同じ1か所（_asked_turn の voiced）
            self._asked_turn(voiced=True)
            return None
        if self.current is None or self.awaiting_confirm is not None:
            return None
        q = self.questions[self.current]
        if self.meaning_said not in q.all_conditions:
            return None
        self.nudged_turn = self.callee_turns
        return notice(f"相手の言葉（「{self.callee_said.strip()[:40]}」）は、{q.id} への答えです。"
                      "何も言わずに、record_answer で報告してください。", quiet=True)

    def _leave_open(self, head: str, rid: str) -> None:
        if self._offer_due():
            return   # この一言の代わりに申し出を伺う（営業に引き継ぐ架電の詳しい者からのお話・締める前の申し出＝_handoff_step。✅ 2026-11-09 日本時間4時36分49秒・6時33分57秒）
        """点を言っていただけないまま手順を終えたときに、次へ進む前に挟む一言（「ご不満な点がございましたらいつでもお申し出ください。」など）の印を立てる唯一の
        場所（head＝頭の相づち・rid＝一言の録音の名前。流すのは _advance の1か所）。このご承認の質問でもう言っていれば立てない（同じ一言を2回言わない）。
        ⛔ 2026-11-06 日本時間13時台の見直し（B2）：遅れた判定で取り消して段を戻す（_point_taken_back）と印だけ外し、言った印が無かったので、段が戻ったあとの
        答えでもう一度挟んだ"""
        if not self._point_marks()["anytime_said"]:
            self.point["open"] = (head, rid)

    def _point_marks(self) -> dict:
        """いまのご承認の質問（point の q）の、この通話でお客様にもう言った印を読む・書く唯一の場所（asked・anytime_said・n1＝Flow.__init__ の point_marks）。
        ご承認の質問ごとに通話で1つ＝同じご承認でまた手順を始めても（締めの前の念のための確認で伺い直した「いいえ」）、
        もう伺った一言・もう言った一言は言わない（本書 9-5 のご承認の手順「もうお客様に伺った一言は、取り消しでもう一度伺わない」）。⛔ 2026-11-06 日本時間
        23時22分41秒のご質問の見直し（ご承認の手順の持ち場 A1）：印を手順の記録 point に持っていたので、手順をやめると印も消え、伺い直したご承認の「いいえ」の
        あと「ご納得いただけなかった点は…」をもう一度伺い、「ご不満な点が…」も2回言った"""
        return self.point_marks.setdefault(self.point["q"], {"asked": {}, "anytime_said": False})

    def _retake(self, qid: str, cond: str, by: str, why: str) -> None:
        """この通話で、質問 qid の答えを cond に決め直す唯一の場所（ほかのご希望を伺って元の質問の選択肢を選び直していただいた＝_wish_step・ご承認いただけ
        なかったときの手順でご納得いただいた＝_point_end・ほかに分からない点は無いと伺った＝_no_doubts_step・遅れて届いた判定で取り消したご承認のお断りを、手順の
        ）。by＝決め手の答えの質問（W1・N1〜N3）。
        - 前の通話で取れた答えでも、この通話の答え（依頼者へ渡す＝export・customer_words）にする。言い切っていないかは、決め手の答えのお客様の言葉で決める
          （Flow._hedged の1か所。決め手の答えの材料＝まだ書いていなければ、いまの報告の言葉）。⛔ 2026-11-04 日本時間21時台に見つけた：決め手の質問（W1・
          N2・N3）は確かめずに受け取る質問なので印が付かず、「たぶん分かったと思います。」「たぶん送ってもらってもいいかもしれないです。」でも、言い切った
          答えとして渡っていた（19-0ba「お客様の言葉で付ける」から漏れていた）
        - その答えのあとに伝える一言は、まだ言っていない（_tell_due）。
        - 前の答えの発話の材料は外す（遅れて届いた確かめを、もう当てない）。決め直した答えは、決め手の答えの材料と確かめで決まる（rests_on。食い違えば
          根拠の発話＝_evidence）。決め手の答えの選択肢の名前（「分かった」・「やはり（意味の言葉）」）は元の質問の名前と違うので、
          決め手の発話の確かめを元の質問の答えに直接当てない。
        ⛔ 2026-11-04 日本時間18時台（18時16分のご承認を入れたあとの見直し）に見つけた：ご納得いただいた答えは「はい」にするだけで、続きの電話では依頼者へ
        渡らず、この通話でも前の「いいえ」の発話の判定が遅れて別の読みで届くと「-」に戻った。はい／いいえの質問の選び直しは、選び直した発話の材料を元の
        質問の答えに写していたので、同じ向きの確かめが遅れて届いても「-」になった。"""
        it = self.items[qid]
        m = self._material(by)   # 決め手の答えの材料（いまの報告で受けた答えなら、いまの言葉）
        said, check = m.get("said"), m.get("check", {})
        hedged = customer_said(said) or label(cond) if Flow._hedged(said, check) else None
        # その答えのあとに伝える一言は、違う答えに決め直したときだけ「まだ言っていない」（同じ答えに決め直した＝取り消した答えを手順のお返事で確かめた
        # ＝もう言ったかをそのまま）
        it.condition, it.value, it.hedged, it.told = cond, cond, hedged, it.told and it.condition == cond
        it.set("accepted", why)
        self.earlier.pop(qid, None)
        self.taken.pop(qid, None)
        self.rests_on[qid] = (by, self.items[by])

    def _evidence(self, qid: str) -> dict | None:
        """質問 qid の答えの根拠の発話（taken の形。無ければ None）を読む唯一の場所（ここで終える答えの根拠の発話＝ended_on・目的が果たせなくなった断り
        goal_refused）：材料の1か所 _material（いまの報告で受けた答えなら、いまその答えを受けている言葉＝前の発話の言葉で受けた答えなら、その発話）。この通話で
        決め直した答え（_retake）は、決め手の答えの材料（決め手の答えの項目のもの。その質問を伺い直したあとなら、前の回の記録＝taken_before）。
        ⛔ 2026-11-08 日本時間10時台に見つけた：記録（taken＝報告のあとに書く）だけを読んでいたので、報告の中で決める目的が果たせなくなった断りの根拠は、
        事実を残す所の既定（いまの発話）で埋まっていた＝覚えておいた前の発話の言葉で受けた答えなら、別の発話が根拠になった"""
        by, item = self.rests_on.get(qid, (None, None))
        if by is None:
            return self._material(qid) or None
        if self.items.get(by) is item:
            return self._material(by) or None
        return next((t for q, it, t in self.taken_before if q == by and it is item), None)

    def _record_taken(self, qid: str, words: dict | None, confirming: bool, reported: str | None) -> None:
        """受け取ったときの材料（唯一の書き方）：その発話の番号・言葉・確かめ（words＝前の発話の言葉の答えなら、その発話。無ければいまの発話）と、
        受け取った答え。受け取ったあとに届いた確かめは記録だけ（late＝_late_to_taken）・届かなかった確かめを数える（unchecked）。
        この通話で決め直した答え（_retake＝rests_on）には書かない：材料は決め手の答えのもの（_evidence）。
        ⛔ 2026-11-04 日本時間21時台（21時04分のご質問の読み直し）：この報告で状態が変わった決め直した答え（伺わずに決めた「ご不明な点は…」・前の通話の
           「いいえ」のご承認）にも、報告のあとでここを通って決め手でない材料を書いていた＝決め手の発話の判定が同じ読みで遅れて届くと、その材料と比べて
           食い違いとし、「-」にして念のための確認で伺った"""
        if qid in self.rests_on:
            return
        self.taken[qid] = {**self._words_now(words), "confirming": confirming, "reported": reported}

    def _record_received(self, before: dict, words: dict | None) -> None:
        """いまの報告・ターンで受け取った答え（before＝その前の項目の状態。_received_now と同じ決まり）の材料を残す唯一の場所（報告の入口 _apply_one）。
        名前の読みは、認めたか直されたかを相手の言葉でプログラムが決める（✅ 2026-10-14 ユーザー決定 (a)。_reading_denied）＝確かめに当てない。
        言い直しへの返事で確かめた項目は、値を聞く型なら「合っている」が受け取った答え（選択肢の型は選択肢）。前の発話の言葉の答え（words）は、その発話の
        番号・言葉・確かめ（遅れて届いたその発話の確かめも記録＝_late_to_taken）。"""
        for qid, it in self.items.items():
            if it.state not in TAKEN or before.get(qid) == it.state:
                continue
            if not self.questions[qid].reading_of:
                q, confirming = self.questions[qid], it.state == "confirmed"
                self._record_taken(qid, words, confirming, "affirmed" if confirming and q.type not in CHOICE_TYPES else it.condition)
            self.unclear_qids = set()   # 答えを1つ受け取った＝お声は聞き取れている（声が聞き取れないで終えるのは、聞き取れない質問が続いたときだけ）

    def _words_now(self, words: dict | None = None) -> dict:
        """答えを受け取った発話の材料（番号・言葉・確かめ）を作る唯一の場所（記録を書く _record_taken・記録の前に読む _material）：前の発話の言葉で受けた答え
        （words・操作の中なら judged）はその発話、無ければいまの発話。"""
        w = words or self.judged
        turn, said, check = (w["turn"], w["said"], w["check"]) if w else (self.callee_turns, self.callee_said, self.check)
        return {"turn": turn, "said": said, "check": dict(check)}

    def _material(self, qid: str) -> dict:
        """受け取った答えの材料（taken の形＝その発話の番号・言葉・確かめ。無ければ空）を読む唯一の場所（取り消せない事実のそろい _answer_check・繋いだ読み
        _substitute_decided・かけ直しの読み _hurry_decided・_wait_or_later_decided・覚えていないか _unknown_agreed・決め直す _retake・根拠の発話 _evidence）：
        いまの報告で受け取った答え（_received_now＝記録を書く _record_received と同じ決まり。記録を書くのは報告のあと）なら、いまその答えを受けている言葉
        （_words_now）、ほかは受け取りの記録（いまの項目のものだけ＝前の回は taken_before＝_new_item。前の通話の答えには記録が無い＝空）。
        ⛔ 2026-11-08 日本時間10時台（F0 の試験の書き直しのあとの確かめ）で見つけた：操作の中で決める5か所が記録（taken）を直接読んでいた＝いまの答えの判定が
        そろっていても「判定が無い」（急いでいる方への「いいえ」・ご遠慮の一言への「はい」の取り消せない事実のそろいが none＝CL-2 は録音の確かめまで保留／
        代わりの方と繋いだ読みが ['ai'] だけ）・同じ番号の質問の前の回の記録を、いまの答えの確かめとして読みえた。待つかかけ直すかの答えは、伺う前の質問の
        記録を読んでいた"""
        if any(q == qid for q, _ in self._received_now()):
            return self._words_now()
        return self.taken.get(qid) or {}

    def _new_item(self, q: "Question") -> None:
        """質問の項目を作る唯一の場所（システムの質問を足す・同じ番号の質問を伺い直す・前の通話の答えを外す）：同じ番号の前の回の受け取りの記録（taken）は、
        前の回の記録（taken_before）へ移す＝いまの答えの材料（_material）には前の回を読まない・前の回も届かなかった確かめ（unchecked）に数え続け、遅れて届いた
        読みもその回の記録に残す（_late_to_taken）。⛔ 2026-11-08 日本時間10時台（F0 の試験の書き直しの担当が見つけた）：14か所が項目を手で作り直し、5か所は
        前の回の記録を消し（遅れて届く確かめを新しい答えに当てないため＝F0 の前の決まり）、ほかは残していた＝ご説明のあとに「ほかに…」（N2）をもう一度伺うと、
        前の回の答え（依頼者へ渡る点の記録）が届かなかった確かめから消え、次の答えで上書きされて遅れた読みも残らなかった"""
        if (t := self.taken.pop(q.id, None)) is not None:
            self.taken_before.append((q.id, self.items.get(q.id), t))
        self.questions[q.id], self.items[q.id] = q, Item()

    def unchecked(self) -> dict[str, list[str]]:
        """受け取った項目のうち、使う確かめ（use_checks）が、その答えを決めた時までに届かなかったもの（項目 → 届かなかった確かめ。決めたあとに届いた読みも
        記録だけ＝届かなかったのと同じ）。通話後の検証（DESIGN.md 6-12）が、録音からやり直す。やり直せなければ「-」に落とす（確かめていない答えを、取れたとして
        渡さない）。同じ番号の質問を伺い直す前の回（taken_before）の受け取りは「番号@発話の番号」（その回の答えも依頼者へ渡りうる＝ご承認の手順の点の記録）。"""
        # 会話AIの聞き取りが届かなかった発話は、別の耳の文字が届いても比べられない（届かなかったのと同じ）。意味の判定は、その発話に出したもの
        # （judged_turns）だけを数える＝出していない受け持ちは届くはずが無い。1つも出していない発話は、使う判定の全部が届かなかった
        def due(k: str, turn: int) -> bool:
            return k not in JUDGMENTS or not self.judged_turns.get(turn) or k in self.judged_turns[turn]

        def miss(t: dict) -> list[str]:
            return [k for k in self.checks_used if due(k, t["turn"]) and (k not in t["check"] or (k == "ear" and not t["said"]))]
        now = {qid: m for qid, t in self.taken.items() if self.items[qid].state in TAKEN and (m := miss(t))}
        return now | {f"{qid}@{t['turn']}": m for qid, it, t in self.taken_before if it is not None and it.state in TAKEN and (m := miss(t))}

    # ── この操作の返事を作る前に、確かめ（use_checks）を待つか：答えをそのまま受け取りうる報告だけ（DESIGN.md 1章）。
    #    確かめない型の答え・「わからない」「答えたくない」・自由な意見の答え／言い直しへの返事／代わった方への名乗りの流し直し（取り次ぎを待つ
    #    間に流さない）。値を聞く型の最初の答えは、言い直して確かめるので待たない（その返事で待つ）
    def wants_checks(self, name: str, args: dict | None = None) -> bool:
        a = self._args_in(dict(args or {}))
        if self._may_ask_back() and (self._asked_blocks(name, a) or name == "replay_opening"):
            # 答えの段で「？」の言葉：尋ねられたか（_asked_now）は判定の読みで決まる＝尋ねられたときに止める報告（_asked_blocks の1か所）と、尋ねられたかで
            # 答えの文が使えないときの道が変わる答え方は、判定が届いてから当てる（判定の届く速さで、同じ言葉への動きを変えない）。⛔ 2026-11-06 日本時間
            # 5時56分55秒のご質問のあとの読み直しで見つけた：止める報告の一覧と待つ報告の一覧が別々で、「質問に戻る」・聞き取れない・値の質問の答えは
            # 待たなかった＝判定が遅れれば通り、先に届けば止まった
            return True
        if name == "replay_opening":
            # 代わった方への流し直し（繋ぐ）・相手の質問への答え方（いまの質問の答えも入っているかもしれない間＝_keep_answer_reading）・
            # 呼んでいる段で名乗り・用件の録音で答える（何を聞かれたかの読みで当てる＝_asked_other。読みが届く前に決めると当てられない）。
            # 尋ねられた問いに答えたあとの「質問に戻る」：お客様の返事が答えなら、戻らずに答えとして報告し直させる（_resume。判定の読みで決める）
            return (a.get("part") == "handover"
                    or (a.get("part") not in ("resume", "handover") and self._answer_may_come_first())
                    or (a.get("part") == "resume" and self.own_pending == ASKED_REPLY)
                    or self._opening_answer(a.get("part")))
        if name == "report_party":
            # 呼んでいる段の「目的の人」「受付の方」の報告は、判定に当ててから決める（_calling_reported。⛔ 2026-10-21 の見直し：受付の方の報告は
            # 判定を待たずに黙って待たせていたので、「席を外しております」「私が岡田です」を受付の方と報告されると、そのまま黙っていた）
            # 第一声を聞く段の報告は、判定を待たない（名乗りを流す前＝人かどうかだけ。待つと、名乗りが遅れる）
            return self.phase == "calling" and not self.answering and a.get("party") in PERSON_PARTIES + STANDIN_PARTIES
        if name == "request_callback":
            return self.phase == "calling" or self.judging  # 答えの段も、待つ・かけ直すの読み（wait_reading）を当てる。 呼んでいる段のかけ直しの申し出も、同じ1か所で判定に当ててから決める（⛔ G-57 tc24）
        if name == "request_end" and a.get("reason") not in (None, "done"):
            # 終わりの報告は、判定を待つきっかけ（電話を終える言葉かを決めるのは判定＝_end_judged。✅ 2026-11-01 日本時間17時04分 ユーザー：(a)「OK」）。
            # 判定を使わない通話の取り次いでいただけなかった：取り次ぎの読み（別の耳）があれば黙って待つ（_not_transferred）
            return self.judging or (a.get("reason") == "not_transferred" and self.phase == "calling")
        q = self.questions.get(self.current) if self.current else None
        if q is None:
            return False
        if name == "record_answer":
            # 前の発話の言葉の答え（_held_words）も待つ：いまの発話が答えを言い直していれば、新しいほうの言葉に当てる（判定が届いてから決める）
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
        standin = name == "report_party" and a.get("party") in STANDIN_PARTIES
        if name == "request_callback" or standin or not self.binding(name, a) or not self.opening.get("greeting") or self._greeted():
            return None  # かけ直し・代わりの方・後任の方の申し出（相手は目的の人ではない側）には、あいさつを流さない
        handover = name == "replay_opening" and a.get("part") == "handover"
        if not (self.passed_on or (handover and self.asked_target is None)):
            return None
        self.greeted_at = self.callee_turns
        return {"kind": "opening", "part": "greeting", "text": self.opening["greeting"]}

    # ── この報告で決めることは、取り消せないか（電話接続側が、報告を待たせる間に判定を全部待つか＝早く届いたほうで決めないか）。
    #    目的の人に繋ぐ（呼んでいる段の「目的の人」の報告・代わった方への名乗りの流し直し）は、繋いだら戻せない（名乗り・用件・質問が流れる）。
    #    呼んでいる段のかけ直しの申し出も同じ（受け取ると、目的の人に繋がないまま戻る時刻などを聞いて締める。G-57 tc24）。
    #    答えの報告は、組の1つの読み（d1-ultra 優先の順）が届いた時か待つ長さが切れた時に1回だけ決める（Flow.on_decided。あとから当て直さない）
    #    （2026-10-19 G-62。⛔ 訂正：最初に例に挙げた「はい、お電話代わりました。」は、受付の方が岡田様に取り次いだあとなので「ご本人」が
    #     正しかった＝ユーザー。食い違いの真因は、判定に前の言葉を渡していなかったこと＝judge_context。全部を待つ決まりは残す）
    def binding(self, name: str, args: dict | None = None) -> bool:
        a = self._args_in(dict(args or {}))
        return self.phase == "calling" and ((name == "report_party" and a.get("party") in ("target",) + STANDIN_PARTIES)
                                            or (name == "replay_opening" and a.get("part") == "handover")
                                            or name == "request_callback")

    def waits_every(self, name: str, args: dict | None = None) -> bool:
        """報告を待たせる間に、意味の判定を全部待つか（電話接続側。早く届いたほうで決めない）：取り消せない報告（binding）と、いまの質問の答えが
        入っているかもしれない間の答え方の操作（その言葉を覚えておくか・あとの答えの報告をどの確かめに当てるかは、判定が全部届いてから＝
        _keep_answer_reading）。"""
        a = self._args_in(dict(args or {}))
        return (self.binding(name, a) or (name == "replay_opening" and a.get("part") not in ("resume", "handover")
                                           and self._answer_may_come_first())
                or (name == "request_end" and a.get("reason") not in (None, "done") and self.judging))   # 電話を終えるか＝判定を全部待つ

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
        """中の名前の操作を1つ受ける。"""
        try:
            return self._apply_one(name, args)
        finally:
            self.report_before = None

    def _received_now(self) -> list:
        """受けている報告で受け取った答え（その報告で、取れた・わからない・答えたくないになった項目＝TAKEN の (番号, 項目)。唯一の場所）。相づち（_ack_choice）と
        締めの言葉（_close・_turned_down_now）は、届いた報告の名前（答えの報告・言い直しへの返事・聞き取れない）ではなく、ここで受け取った答えで選ぶ
        （⛔ 2026-11-04 日本時間7時台の4回目のご質問の見直しで見つけた：報告の名前で選んでいたので、言い直しへの返事で届いた「わからない」・聞き取れないの
        報告から曖昧な言葉の意味で決まった遠ざかる向きの答えに、別の相づちを返していた）。報告の外（報告を受けずに締める）は空。"""
        b = self.report_before
        return [] if b is None else [(qid, it) for qid, it in self.items.items() if it.state in TAKEN and b.get(qid) != it.state]

    def _thanks_barred(self, reply: dict) -> bool:
        """この報告で、お礼（「ありがとうございます。」）を返さない答えを受け取ったか（お断り・目的から遠ざかる向き＝turned_down・わからない・答えたくない。本書 9-7。
        同じ発話の2つの報告の受けの言葉を1つに選ぶ所＝Flow._ack の same_turn が読む）。"""
        return bool(reply.get("turned_down")) or any(it.state in ("dont_know", "refused") for _, it in self._received_now())

    def _turned_down_now(self) -> bool:
        """受けている報告で、お断り・目的から遠ざかる向きの答え（Question.turned_down）を受け取ったか（唯一の場所＝相づちの目印 _apply_one と、
        締めの言葉 _close）。報告の外（報告を受けずに締める）は偽。"""
        return any(it.state in GOT and self.questions[qid].turned_down(it.condition) for qid, it in self._received_now())

    def _hedged_next(self, reply: dict) -> bool:
        """次に伺う質問が、この報告で受け取った言い切っていない答え（印 hedged）のあとに伺う業務設定の質問（その答えの質問の ask_if）か（唯一の決まり＝
        相づちの「はい、」＋「それでは…」）。最初の答えでも言い直しへの返事でも同じ（⛔ 見直しで見つけた：最初の答えからだけ「はい、」で、値を伺う質問の
        言い直しへの言い切っていない返事から入ると、順番の「承知しました。」だった）。"""
        nq = self.questions.get(reply.get("question_id"))
        return nq is not None and bool(nq.ask_if) and any(qid == nq.ask_if[0] and it.hedged for qid, it in self._received_now())

    def _apply_one(self, name: str, args: dict) -> dict:
        before = {qid: it.state for qid, it in self.items.items()}
        self.report_before = before
        phase_before, answering = self.phase, self.answering
        words = None  # 前の発話の言葉の答えなら、その言葉（下の操作で決まる）
        answered = False  # この発話を、いまの質問・言い直しへの答えとして受けたか（答えの報告の操作を実際に受けたときだけ＝答えに添えた質問を見る _turn_done の took）
        held_was = self.answer_held   # この報告の前に覚えていた言葉（この報告で次の質問へ進み、先に言われた答えを覚え直したら残す＝下）
        run, run_args = name, args   # 実際に受ける操作（_wish_reported）。相づちもこの操作で選ぶ
        self.reply_started = False
        if name == "report_unclear":
            # 会話AIが、この発話を聞き取れないと言った（判定は文字だけを読むので、音の聞こえは会話AIと別の耳が決める＝_heard_clearly）。どの確かめより先に立てる
            # （迷っているお客様への応じ方 _hesitation が、聞き取れない言葉を選ぶ助けの番・保留にしない＝⛔ 11-07 23時26分13秒のご質問の見直しの H-3）
            self.unclear_turn = self.callee_turns
        if name == "replay_opening" and args.get("part") in self.answers:
            args = {**args, "part": ANSWER_PREFIX + args["part"]}  # 想定問答の問いの名前 ⇒ 録音の部分の名前
        if self.trouble is not None:
            reply = self._ended()   # こちらの不具合で電話を終えた（_trouble）：AIの報告は受けない（お詫びの言葉は電話接続側が流す）
        elif name not in OPS:
            reply = self._reject(f"その操作はありません（使える操作は {'・'.join(OPS)}）。")
        elif why := self._check_args(name, args, {ANSWER_PREFIX + k for k in self.answers}, set(self.known_lines())):
            reply = self._reject(why)
        elif self.answering and name != "report_party":
            # 出た方の第一声を聞いている（まだ名乗っていない）：相手の種別の報告のほかは受け付けない（名乗りは、第一声のあとにプログラムが流す）
            # ⛔ 2026-10-24 G-57 J1（留守番電話のあとに人が出る・S2）：前の文は「人ではない相手なら知らせる」だけで、人の知らせ方を書いて
            #    いなかった＝案内のあとに出た方の言葉に、AIは名乗りの流し直しを頼み、差し戻されたまま、人と知らせられなかった
            reply = self._reject(self._party_ask())
        elif self._asking_now() and not (name == "request_end" or (name == "replay_opening" and args.get("part") != "handover")
                                         or self._offer_while_asking(name, args)):
            # 相手がこちらに尋ねている（呼んでいる段の、別の問い合わせ asks）：聞かれたことに答えるのが先（唯一の場所。目的の人・受付の方の
            # 報告・代わった方・答えの報告・かけ直しのどれでも）。⛔ 2026-10-22 G-57 tc18：「この番号はどちらでお知りになりましたか？」に、AIが
            # 答えの報告（record_answer）を呼び、「何も言わずに待ちます」と返したので、質問に答えなかった（3本中2本）。
            # 相手の申し出（「折り返し電話するようにお伝えしましょうか？」「代わりに私が伺いましょうか？」）も問いの形なので、申し出の報告
            # （request_callback・代わりの方・後任の方）は止めない（✅ 2026-10-22 ユーザー：その問いに「分かりかねます」と答えるのは愚か）
            reply = self._reject(self._asking_note())
        elif name != "request_end" and (due := self._end_due_now()) is not None:
            reply = self._op_request_end({"reason": due, "judged": True})   # 会話AIの報告より、判定の「電話を終える言葉」を先に当てる（_end_judged）
        elif self._asked_now() is not None and self._asked_blocks(name, args):
            # 答えの段で、お客様が、いまの質問・言い直しに答える代わりにこちらへ尋ねた（asks_us の1か所）：報告は受け付けず、聞かれたことに答えるのが先
            # （呼んでいる段の「尋ねている」と同じ形。いま求めていること＝_now の _asked_note）。✅ 2026-11-06 日本時間4時23分33秒 ユーザー：「お客様：送って
            # もらったほうがいいですかね？や、送ってもらったほうがいいですかねー？だった場合は、こちらは「そうですね、お読みになられるとより理解が深まるかと
            # 思います。」のような返答が自然です。」⛔ それまでは、判定の「決められない」で言い直し・確かめの一言を繰り返していた
            # 迷っているお客様に選ぶ助けを伝える番（_hesitation の help）も同じ道で、言葉はその番に合わせる（迷っているのは問いではない）
            kind = self._asked_kind()
            what = ("どれにするか迷っている・まだ決めていない言葉" if kind == "help" else
                    "申し出への、はい・いいえではないお話" if kind == "offer" else "こちらへの問い")
            reply = self._reject(f"いまの相手の言葉は、答えではなく、{what}です（報告は受け付けません）。")
        elif (left := self._wait_left(name, args)) is not None:
            reply = left   # 待つかかけ直すかを伺っている間に、伺う前の話に答えた：伺う前の位置へ戻し、その言葉を戻した位置について読み直してから受ける
        else:
            # ほかのご希望を伺っているときの、ほかの手段の申し出は、W1 の答えとして受ける（_wish_reported の1か所。相づちも答えの報告と同じ決まり）。
            # 記録（log）には、AIが呼んだままの操作を残す
            run, run_args = self._wish_reported(name, args)
            # 前の発話の言葉の答え（_held_words）：この操作の間だけ、答えを当てる言葉をその発話にする（heard_said・heard_check）
            self.judged = self._held_words(run, run_args)
            prev_ahead = self._take_rest(run, run_args)
            try:
                reply = getattr(self, f"_op_{run}")(run_args)
            finally:
                words, self.judged = self.judged, None
            if reply["result"] == "rejected":
                self.ahead = prev_ahead
            answered = run in ANSWER_OPS
        # 返事のあとの決まり（受け取りの記録・待つ印・答えに添えた質問・取っておいた返事）は、報告の無いターンの入口 judged_turn と同じ1か所。答えに添えた質問を
        # 見るのは、会話AIがお客様の言葉を答えとして報告し、その報告を受けたとき（ANSWER_OPS。実際に受ける操作 run＝ほかのご希望の申し出も W1 の答え）。判定の
        # 電話を終える言葉が先に当たって締める道（_end_due_now）・差し戻しは、答えとして受けていない＝見ない（断り・苦情は弁明せずに締める＝本書 9-4）
        reply = self._turn_done(reply, before, words, took=answered, replay=name == "replay_opening")
        if name == "replay_opening" and args.get("part") not in ("resume", "handover") and reply["result"] != "rejected":
            self._mark_answered()  # この発話の相手の質問には、もう答えた（_question_first）
        if (name != "replay_opening" and reply["result"] != "rejected"
                and (self.answer_held is held_was or not (self.answer_held or {}).get("ahead"))):
            # 覚えておいた言葉（answer_held）は、AIがその答えを報告した（受け取ったか、確かめに入った）か、別の報告で先へ進んだら、もう使わない。
            # この報告で次の質問へ進み、先に言われた答えを覚え直した（_offer_ahead）ときは残す（⛔ 2026-11-04 日本時間21時台に流して見つけた：
            # 消していたので、次の報告がいまの発話の言葉に当たり、読み直しの判定も出なかった）
            self.answer_held = None
        # 先に言われた答えを、伺ったのと同じ発話のうちに受け取った（_offer_ahead）：伺った報告の相づちをもう流した＝重ねて流さない
        same_turn = words is not None and words.get("ahead") and words.get("offered") == self.callee_turns
        if words is not None and reply["result"] != "rejected" and not same_turn:
            # 前の言葉の答えを、相手の返事（「そうなんですね」）のあとに受け取った：その返事を「はい、」で受ける（答えへのお礼・承りは、もう遅い）
            reply["talked"] = True
        if self._turned_down_now():
            reply["turned_down"] = True   # この報告で、お断り・目的から遠ざかる向きの答えを受け取った（相づちにお礼を返さない＝_ack）
        # 先に言われたことを伺う（_offer_ahead）間は「はい、」だけを流す（そのあとに言い直しか次の質問。伺ったのと同じ発話のうちは「はい、」を重ねない）。
        # ✅ 2026-11-04 日本時間20時27分 ユーザー：「…伺わずに「abc_123でよろしいでしょうか？」と言い直して確かめます。OK」＝ご承認の文は
        # 「はい、ありがとうございます。abc_123でよろしいでしょうか？」（ふつうの言い直しと同じ「はい、」）。⛔ 21時台に流して見つけた：伺った報告の
        # 相づち（「承知しました。」「かしこまりました。」）を流してから言い直していた
        # 同じ発話の最後の返事の頭に受けの言葉が要るときは流す（Flow._ack の same_turn）：ご承認の手順の終わりのお礼（「ありがとうございます。それでは、～」＝11-02 5時56分。
        # ✅ 2026-11-05 日本時間2時25分10秒 ユーザー：「(a) お勧め：はっきり納得と言われたら、伺わずに「ありがとうございます。」で次へ進みます。…OK」＝ご説明の
        # あとの「それなら納得です。」を、ご納得の質問への先に言われた答えとして受け取ったとき）・申し出を受けていただいたお返事の受けの言葉（まとめ役の見直し (D)）も、
        # 伺った報告の「はい、」のあと（20時27分のご承認の文「はい、ありがとうございます。abc_123でよろしいでしょうか？」と同じ並び）。相づちを選ぶのは _ack の1か所のまま
        reply["ack"] = self._ack(run, run_args, reply, same_turn=bool(same_turn))
        if reply["ack"]:
            reply["ack_pause_s"] = ack_pause_s(reply["ack"])  # 相づちのあとの間（phrases.ACK_PAUSE_S の1か所。電話接続モジュールが守る）
        self._note_said(about=self._said_about())
        self.log.append({"op": name, "args": args, "reply": reply, "callee_turns": self.callee_turns, "phase_before": phase_before,
                         "phase": self.phase} | ({"words_turn": words["turn"]} if words else {}) | ({"answering": True} if answering else {}))
        return reply

    def _turn_done(self, reply: dict, before: dict, words: dict | None, took: bool, replay: bool = False) -> dict:
        """お客様の発話への返事ができたあとの決まり（唯一の場所＝報告の入口 _apply_one と、報告の無いターンの入口 judged_turn の両方が通る）：
        受け取りの記録（_record_received）・通話の事実の書き直し（番号を伺い直す電話・かけ直しの答え・分かる方・分かる時・約束の電話の一言）・応えた中身（reply_said）・
        返事を待つ印（own_pending・ai_pending）・答えに添えた質問（took＝この発話を答えとして受けた＝_question_first）・取っておいた返事（after_answer）。
        replay＝応える操作（replay_opening）＝答えに添えた質問は見ず、取っておいた返事も残す。
        ⛔ 2026-11-08 日本時間10時台（F0 の確かめ）：受け取りの記録が報告の入口の中だけだった。⛔ 11時台（J-5 の確かめ）：返事を待つ印を消すのも報告の入口の中
        だけだった＝報告の無いターンで答えを受け取ったあとも「相手の質問に自分の言葉で答えたところです」と会話AIに知らせ、取っておいた返事の番を伝えなかった"""
        self._record_received(before, words)
        if (self.number_again and not self.facts.get("callback") and self.callee_turns > 0 and self.party not in NOT_A_PERSON
                and self.party != "wrong_party"):
            self._note("callback", why="other_number", read=[])   # 番号を伺い直す電話：人の言葉が届いた（その発話が根拠）
        if self.callback_why and self.facts.get("callback"):
            # かけ直しの申し出の答え（Flow._callback_fields の1か所）。操作のたびに書き直す（締めの前に相手が切っても、取れた分は残る）
            self.facts["callback"][-1].update(self._callback_fields(self.callback_ids, self.callback_why))
        for fid in [f for f in self.follow_ups if f not in self.follow_noted and self.items[f].state in GOT
                    and self.questions[f].callback in CALLBACK_REASONS]:
            # 分かるお客様・分かる時の答え：かけ直しの記録（理由＝扱いの名前。項目は同じ1か所＝分かるお客様は person・分かる時は time）。
            # ⛔ 2026-11-03 に見つけた（CL-2 の 4-188 の (d) の比べと、記録を書く所の確かめ）：ご承認いただけなかったときの質問（N1〜N3。置く場所に
            #    同じ follow_ups を使う）の答えも、理由「not_agreed」などのかけ直しの記録に書いていた＝CL-2 の読み手は一覧に無い理由の報告を丸ごと断る
            self.follow_noted.add(fid)
            self._note("callback", why=self.questions[fid].callback, read=[], **self._callback_fields([fid], self.questions[fid].callback))
        if (p1 := CALLBACK_IDS["promise_again"]) in self.items and self._asked(p1) and self.facts.get("not_transferred"):
            # 約束の電話で取り次げなかったときの一言への答え（伺ったら、「はい」のときだけ true＝CL-2 の 4-135。⛔ 2026-10-24 の見直し：
            # 「わからない」・伺う途中で切れたときに書いていなかった＝CL-2 は「聞いていない」と読んだ）
            self.facts["not_transferred"][-1]["again"] = self._yes_no(p1)
        if reply["result"] != "rejected" and self.reply_started and (self.own_pending or self.ai_pending):
            # この操作で応えて、お客様の返事を待ち始めた：お客様に聞こえた中身を覚える（もう一度言ってほしいと頼まれたら言い直す＝_repeat_now）
            self.reply_said = {k: reply[k] for k in ("play", "say", "speak", "silent", "message") if reply.get(k)}
        if reply["result"] != "rejected" and not self.reply_started:
            # 決まった言い方・自分の言葉の答えへの返事を待つのは、その次の報告まで（答え・ほかの報告が来たら、もう待っていない）。この報告で応えて、
            # いま待ち始めた（_await_reply の1か所）ときは残す。差し戻した報告は、何も変えない（受け付けていない）。⛔ 2026-11-06 日本時間5時56分55秒の
            # ご質問のあとの読み直しで見つけた：待ち始める操作の一覧を、ここにもう1つ持っていた＝尋ねられた問いに想定問答の録音で答えた操作が一覧に無く、
            # 待つ印を消したので、お客様の返事を待たずに質問を流し直した
            self.ai_pending = self.own_pending = False
        if took and not replay:
            # 答えとして受けた言葉に、こちらへ尋ねた問いが添えてあれば、どの返事（次の質問・言い直し・聞き返し・締め）でも先に答える（唯一の場所）。待つ印を
            # 消したあとに見る（いま求めていることの知らせ＝_now が、前の答えへの返事待ちではなく、この質問に答える番を言う）
            reply = self._question_first(reply)
        if not replay and reply["result"] not in ("callee_asked", "rejected"):
            # 答えに添えられた質問に答えずに、AIが別の報告をした（次の質問の答えなど）：その報告で進む（先に作った返事は使わない）
            self.after_answer = None
        return reply

    def _question_first(self, reply: dict) -> dict:
        """答えとして受けた言葉への返事（次の質問・言い直し・聞き返し・締め）の前に、その言葉の中でこちらへ尋ねた問い（答えに添えた質問）に先に答えるか
        （唯一の場所＝返事のあとの1か所 _turn_done が、この発話を答えとして受けたときに呼ぶ）。答えるなら、返事は取っておき（after_answer）、会話AIが
        聞かれたことに合う答え方（replay_opening の1か所＝名乗り・用件・想定問答は録音、ほかは声に出す前に確かめた自分の言葉 _own_answer）で答えてから
        流す（_continue）。答えないのは：問いが無い（判定の項目「こちらへ尋ねた問いがあるか」の読み＝_question_to_us）・この発話の問いにはもう答えた
        （question_answered_turn）・尋ねられる前の流れで進めると決めた（asked_skip）・返事がもう応えている（差し戻し・答える番・ご説明）。頼み・申し出
        （「あとでかけてもらえますか？」）は、判定の項目が「尋ねた問いは無い」と読む（判定を使わない通話の言葉の形の見方だけ、道の印 words_taken で外す＝
        _question_to_us。⛔ 2026-10-19 G-57 の2回目：かけ直しの申し出の「〜もらえますか？」を相手からの質問として扱い、AIが「私の方では折り返しの
        お電話はお約束できかねます」と言った）。
        ⛔ 2026-11-08 日本時間9時台（J-5 の確かめ）で見つけた（前からある）：次へ進む返事（_advance_to）の中でしか見ていなかった＝言い直して確かめる答え（「15日の
        14時でお願いします。駐車場ってありますか？」）・言い切っていない答え・形の合わない答えに添えた質問を聞き流し、言い直し・聞き返しへ進んだ（G-47 と同じ害）。
        ⛔ G-47：相手が「はい、予約しています。当日はどなたが担当されるんですか？」と答えたのに、プログラムはすぐ次の質問の録音を流し、AIには「何も言わずに
        待って」と伝えた＝相手の質問が10本中10本、聞き流された。"""
        if (reply.get("result") in ("rejected", "callee_asked", "explain_point") or self.after_answer is not None or self.trouble is not None
                or self.callee_turns in (self.question_answered_turn, self.asked_skip)):
            # asked_skip＝この発話の問いへの答えの文が使えず、尋ねられる前の流れのとおりに進めると決めた（_asked_skip。答える番の決まり _asked_kind と同じ印）
            return reply
        asked = self._question_to_us()
        if not asked:
            return reply
        if not any(x["turn"] == self.callee_turns for x in self.facts.get("callee_question", [])):
            self._note("callee_question", answered=None)  # 同じ発話に報告が2回届いても、1つの質問は1つの記録
        self.after_answer = (asked, reply, self.callee_turns)
        return {"result": "callee_asked", "message": f"相手が質問をしています。次へ進む前に、まずその質問に答えます。{self._now()}"}

    def _take_after_answer(self) -> dict:
        """答えに添えた質問に答えたあと、取っておいた返事（_question_first）を取り出す唯一の場所（戻る1か所 _continue・締めの報告 _op_request_end）。
        取っておいた返事は、作った時の発話ではなく、いま流す時の発話（答えたあとのお客様の発話）で言う＝その返事を作った時に立てた「この発話で言った」印を、
        いまの発話に付け直す：言い直しを頼んだ（confirm_requested_at＝言い直しへの返事は、このあとの発話から）・考えている途中の「はい。」を流した
        （thinking_turn）・待つかかけ直すかを伺った（wait_turn）・もう一度言った（repeat_turn）・先に言われたことを伺った（answer_held の offered＝その答えの
        報告に相づちを重ねない）・ご承認の手順の相づち（point の thank・hai・gomottomo＝その返事の頭に流す）・迷っている方に伺った・選ぶ助けを伝えた（hesitating）。
        お客様が言った・読んだ印（聞き取れない・ご意見を言った・電話を終える言葉の読み など）は、お客様の発話のものなので付け直さない。
        取っておいた言い直しは、頭の受けの言葉（前の答えへの「ありがとうございます。」など）を外して「それでは」から＝もう伺ったことへ戻るときと同じ決まり
        （restated_again の1か所。✅ 2026-10-23 ユーザー：「そうなんですね。」のあとの「ありがとうございます、それでは…」は会話が繋がっていない）。
        ⛔ 2026-11-08 日本時間11時台（J-5 の確かめのあと、ご報告の例を流れの制御から出して見つけた）：作った時の発話の番号のままだったので、急いでいる方への
        伺いへの「はい、来週なら大丈夫です。」（来週＝先に言われた日時）に添えた質問に答えたあと、「はい、」のあとの「ありがとうございます。来週で…」にも
        「はい、」を重ねた。先に答える番の返事で、取っておいた返事のお礼「ありがとうございます。」を流していた（_ack_choice が印を先に読んだ）"""
        _, r, made = self.after_answer
        self.after_answer, now = None, self.callee_turns
        for name in ("confirm_requested_at", "thinking_turn", "wait_turn", "repeat_turn"):
            if getattr(self, name) == made:
                setattr(self, name, now)
        for marks, keys in ((self.answer_held, ("offered",)), (self.point, ("thank", "hai", "gomottomo")),
                            *((h, tuple(h)) for h in self.hesitating.values())):
            for k in keys if marks is not None else ():
                if marks.get(k) == made:
                    marks[k] = now
        if self.awaiting_confirm is not None and self.confirm_requested_at == now and r.get("say") == self.awaiting_say:
            say = restated_again(self.awaiting_say)
            r = {**r, "say": say, "message": r["message"].replace(self.awaiting_say, say)}
            self.awaiting_say = say
        return r

    def speech_allowed(self, since: int) -> bool:
        """AIが話した声を、相手に流してよいか（電話接続側が決める唯一の場所。since＝このターンの記録の始まり）。録音で流す通話では、AIの声で
        言ってよいのは、返事で渡した言い方（say＝確かめた答えの文・言い直し・聞き返し）か、話させる返事（speak）のあとだけ＝それ以外の声は流さない。
        録音で流すかは段で決まる：目的の人を呼んでいる段＝名乗りの録音（opening）、そのあと＝質問の録音（play_recordings）。
        ⛔ 2026-10-22 の4回目の流し（G-57 tc36）：答えの段では見ていなかった＝範囲外の質問の S2 は、答えの文が確かめに3回通らず断らない一言の録音が
           流れたあとに、自分の声で「…担当の者に申し伝えまして、改めてご連絡差し上げます。」（約束）と言い、答えに添えられた質問の S2・L4 は、
           録音の返事（「何も言わずに」）のあとに「…担当の者より後ほどご連絡させていただきます。」と言った（確かめていない約束が相手に流れた）
        ⛔ 2026-10-22 G-57 tc31（会社の所在地を聞かれる・L4）：AIが操作を呼ばずに自分から「弊社は東京目黒にございます。」（業務の情報は港区）と答え、
           続けて「申し訳ございません。岡田様は」と名前をAIの声で言った。⛔ G-57 tc25：AIが自分の声で「奥田様はいらっしゃいますでしょうか？」
        名乗りを録音で流さない通話（AIが話す形）は、AIが名乗るので流す。電話接続側は、ターンの途中でも声が届くたびに聞き、真になってからの声だけを
        流す（それより前の声は流さない）。流さなかった声があれば、unreported_note(muted=True) の知らせで答え直させる。"""
        if self.answering:
            return False   # 出た方の第一声を聞いている：まだ名乗っていないので、AIの声は流さない（名乗りはプログラムが流す）
        ops = self.log[since:]
        if since > 0 and self.log[since - 1]["reply"].get("result") == "restate" and self.log[since - 1]["callee_turns"] == self.callee_turns:
            # 外れた言葉を止めたあとの言い直し（on_off_script）：返事は止めたターンで作り、新しいつなぎのターンで言わせる＝そのターンの言い方
            # （⛔ 2026-10-22 の4回目の流し G-57 tc37：答えの段に広げたら、言い間違えの直しの3場面で、言い直しの声を流さなかった）
            ops = self.log[since - 1:]
        if ops and ops[0].get("answering"):
            # 第一声を聞く段に始まったターン：名乗りの録音のあとにAIに言わせる返事（確かめる一言の録音が無い形）のあとだけ（段の決まりより先）
            return any(x["reply"].get("say") or x["reply"].get("speak") for x in ops)
        start = ops[0].get("phase_before", self.phase) if ops else self.phase
        if not (self.opening if start == "calling" else self.play_recordings):
            return True
        # 呼んでいる段を出て、質問をAIが話す通話（質問の録音が無い）なら、そこからはAIが話す
        return any(x["reply"].get("say") or x["reply"].get("speak") or (x.get("phase", "calling") != "calling" and not self.play_recordings)
                   for x in ops)

    def _heard_unreported(self) -> bool:
        """相手が、報告されていないAIの言い直しを聞けたか（答えの報告の前の「違う値を言われた」の報告を差し戻すときに、言い直しを直された
        言い方にするかの唯一の材料）：最後に受け付けた操作のあとに相手が2回以上話した（答えと、報告されなかった言い直しへの返事）うえに、
        報告の無いAIの声が相手に流れる通話（speech_allowed の1か所。録音で流す通話では流さない）。
        ⛔ 2026-10-24 J11a（本当に人ですか L4）：相手の最初の答え「15日の午後2時でお願いします」を、AIが「違う値を言われた」で報告した。差し戻す
           ときに直された印を立てていたので、相手は何も直していないのに「改めまして…」と言い直した"""
        acted = next((x["callee_turns"] for x in reversed(self.log) if x["reply"].get("result") != "rejected"), 0)
        return self.callee_turns - acted >= 2 and self.speech_allowed(len(self.log))

    def _unsent(self, muted: bool) -> str:
        """流さなかった声の知らせの頭（unreported_note の1か所）。このターンの返事で、相手に何かが聞こえた（録音を流した・言い方を渡した・話させた）
        なら、言わない（相手は待たされていない。報告し直させると重なる）。
        ⛔ 2026-10-23 G-57 tc41（答えに添えられた質問・S2）：前は「受け付けた報告があれば言わない」で、相手に何も聞こえない返事（「先に相手の質問に
        答える番です」＝callee_asked）も数えていた。AIは操作を呼ばずに声で答え（流していない）、知らせが出ないまま、相手の「そうなんですね。」に
        「質問に戻る」を9回呼んだ（AIからは答えたつもり）"""
        # まだ考えている途中と読んで「はい。」で待っている（_thinking）のも、相手に聞こえた返事（報告し直させると、同じ確かめの一言をくり返す＝⛔ 2026-11-06 日本時間
        # 10時台に見つけた）
        heard = any(x["callee_turns"] == self.callee_turns and x["reply"].get("result") != "rejected"
                    and (x["reply"].get("play") or x["reply"].get("say") or x["reply"].get("speak") or x["reply"].get("result") == "thinking")
                    for x in self.log)
        return "さきほどのあなたの声は、相手に流していません（録音と、操作の返事で渡した言い方だけを流します）。" if muted and not heard else ""

    def _ack(self, name: str, args: dict, reply: dict, same_turn: bool = False) -> str | None:
        """流す相づち（唯一の入口）：場面で選んだ相づち（_ack_choice）が、前に流した相づちと同じ「承知しました。」なら「かしこまりました。」に替える
        （✅ 2026-11-02 日本時間7時12分 ユーザー：「「かしこまりました」も使えばいいんじゃないですか」＝「はい、あります。」→「承知しました。」・
        「いえ、登録は結構です。」→「承知しました。」と続いていた）。どの場面から来ても同じ（次の質問の順番・お断り・わからない・かけ直しの申し出）。
        同じ理由（同じ受けの言葉を続けない）で、「かしこまりました。」が続くときは「承知しました。」に替える（⛔ 2026-11-06 日本時間23時22分41秒のご質問の見直し
        H9：LINE の伺い方のあとの「かしこまりました。」に、待つかかけ直すかを伺う一言の頭の「かしこまりました。」が続いた）。"""
        ack = self._ack_choice(name, args, reply)
        carried, barred = self.ahead_ack[1:] if self.ahead_ack is not None and self.ahead_ack[0] == self.callee_turns else (None, False)
        if reply.get("ahead_closed"):
            # 先に言われたことに答えが無く、伺う番を閉じた（_resume）：伺う番を開いた報告の返事そのもの（次の質問・申し伝える一言）＝その報告の受けの言葉
            ack = carried if carried in RECEIPTS else None
        elif same_turn:
            # 先に言われた答えを、伺ったのと同じ発話のうちに受け取った（_offer_ahead）：伺った報告の「はい、」（待ちの言葉）はもう流した。返事の頭に受けの言葉が
            # 要るとき（この返事の決まりが受けの言葉＝次の質問・申し伝える一言など）だけ、受けの言葉を1つ流す。選ぶのは、その発話で受け取った答え全部（伺う番を
            # 開いた報告と、この報告。本書 9-7「相づちは受け取った答えで選ぶ」）：お礼を返さない答え（お断り・遠ざかる向き・わからない・答えたくない）があれば
            # 承り、どちらかの決まりがお礼（申し出を受けていただいた・ご承認の手順の終わりのお礼＝2時25分10秒の (a)）ならお礼、ほかは伺う番を開いた報告の受けの
            # 言葉（その報告に無ければこの返事のもの）。言い直し（言葉が「ありがとうございます。」などの受けの言葉から始まる＝「はい、」）・締め・お詫びは重ねない
            # （✅ 2026-11-04 日本時間20時27分「はい、ありがとうございます。abc_123でよろしいでしょうか？」）。⛔ 2026-11-09（まとめ役の見直し (D)）まで、同じ発話の
            # 返事の相づちを全部流さず（ご承認の手順の終わりのお礼だけ例外）、申し出を受けていただいた「はい、それなら。」のあと「はい、」＋「それでは恐れ入りますが
            # メールアドレスを…」でお礼が抜けた
            if ack not in RECEIPTS or reply["result"] == "end":
                ack = None
            elif barred or self._thanks_barred(reply):
                ack = "shouchi" if ack == "arigatou" else ack
            elif "arigatou" in (ack, carried):
                ack = "arigatou"
            elif carried in RECEIPTS:
                ack = carried
        if ack in SAME_RECEIPT and ack == self.last_ack:
            ack = SAME_RECEIPT[ack]
        if ack is not None:
            self.last_ack = ack
        return ack

    def _ack_choice(self, name: str, args: dict, reply: dict) -> str | None:
        """報告が届いた瞬間に、電話接続側が流す相づち（phrases.ACK の中の名前。流さないなら None）。DESIGN.md 6-15。
        ✅ 2026-10-14 ユーザー：基本は「はい、」。違和感のある場面や「はい」が続くときは、別の相づち。
        - 言い直し（AIの言葉は「ありがとうございます。」などの受けの言葉から始まる）・締め・引き止めの一言 ⇒「はい、」
        - 次の質問・念のための確認 ⇒「承知しました。」「ありがとうございます。」を順番に（言い直しの「はい、」と交互になり、
          「はい」が続かない。AIの言葉は「それでは、」などのつなぎの言葉から始まるので、受けの言葉が2つ重ならない）
        - 相手が「わからない」「答えたくない」と答えた・通話を断られた ⇒「承知しました。」（お礼は場面に合わない）
        - 聞き返しても聞き取れず、先へ進む ⇒「はい、」
        - 言い直した値が違っていた ⇒「失礼いたしました。」／聞き取れなかった ⇒「申し訳ございません、」（お詫びの決まり。6-11）
          「申し訳ございません、」は2本：1回目の聞き返し＝軽いお詫び（moushiwake）／同じ項目を2回目以降に聞く・最後の1回＝重いお詫び
          （moushiwake_heavy）。✅ 2026-10-16 ユーザー：「02だと軽薄に聞こえる」場面（こちらの誤り・クレーム・かけ直し）は重いほう
        - 差し戻し ⇒ 流さない（AIはまだ受け付けられていない報告を出し直す）"""
        result = reply["result"]
        if result == "callee_asked":
            # 答えに添えられた質問に先に答える番（_question_first）：相づちは流さない（答える操作の頭が「はい、」）。取っておいた返事の相づちの印（ご承認の手順の
            # お礼 など）は、その返事を流す時に読む（_take_after_answer が印を流す時の発話に付け直す）＝ここでは読まない（⛔ 2026-11-08 日本時間11時台：下の印を
            # 先に読み、先に答える番で「ありがとうございます。」を流し、答えたあとの次の質問の頭は「はい、」になっていた）
            return None
        if reply.get("again"):
            # 同じ発話にもう返事をしていた（報告の無いターンの入口 judged_turn のあとに、同じ言葉への報告が届いた＝_hold・_wait_route の again）：重ねない
            return None
        if reply.get("repeat"):
            # もう一度言ってほしいと頼まれ、伺ったことを流し直す（Flow._repeat_now）：「失礼いたしました。」＋「それでは（質問）」（✅ 2026-11-06 日本時間
            # 15時20分30秒 ユーザー「(1)…OK」）
            return "shitsurei"
        # ご承認の手順の相づちの印は、その印を立てた返事のもの＝取っておいた返事（after_answer）を流す前は、どの返事でも読まない（先に答える番・会話AIの答え。
        # ⛔ 2026-11-08 日本時間11時台：会話AIの答えの頭に、取っておいた返事のお礼「ありがとうございます。」を流していた）
        p = self.point if self.after_answer is None else None
        if p is not None and p.get("thank") == self.callee_turns and result != "end":
            # ご説明でご納得いただけた：「ありがとうございます。それでは、～」（✅ 2026-11-02 日本時間5時56分 ユーザー）
            p["thank"] = None
            return "arigatou"
        if p is not None and p.get("hai") == self.callee_turns and result != "end":
            p["hai"] = None
            return "hai"
        if p is not None and p.get("shouchi") == self.callee_turns and result != "end":
            # 点を言っていただけなかった N1 に、1回だけ「その中でも…」を伺う（_point_step）：「承知しました。」（✅ 2026-11-09 日本時間3時46分53秒）
            p["shouchi"] = None
            return "shouchi"
        if p is not None and p.get("gomottomo") == self.callee_turns and result != "end":
            # 続きの電話の始めに「覚えていない」と言われ、振り返りの一言を流す（_point_step の recap）：「ごもっともです。」（✅ 2026-11-05 日本時間4時16分52秒 ユーザー）
            p["gomottomo"] = None
            return "gomottomo"
        if reply.get("ahead_offer"):
            if (self.ahead_ack or (None,))[0] != self.callee_turns:
                # この発話で初めて伺う番を開いた：ふつうなら流した相づち（受け取った答えで選ぶ＝この下の決まりそのもの。本書 9-7）を取っておき、同じ発話の最後の
                # 返事の頭に受けの言葉が要るときに流す（Flow._ack）。同じ発話のうちに続けて伺う番を開いても（「LINEなら、IDは abc_123 です」）、最初の答えのもの
                self.ahead_ack = (self.callee_turns, self._ack_choice(name, args, {k: v for k, v in reply.items() if k != "ahead_offer"}),
                                  self._thanks_barred(reply))
            # 先に言われたことを伺う（_offer_ahead）間は「はい、」だけ（ご承認の手順の「はい、」＝上の hai も、ここで流したもの＝伺い直すときに重ねない。
            # ⛔ 2026-11-05 日本時間2時台、(a) を入れる途中で流して見つけた：ご説明のあとの「そうなんですね。」に、伺った報告の「はい、」と、ご納得の質問の前の
            # 「はい、」を2回流した）
            return "hai"
        if result == "hold":
            # 待つように言われた（取り次ぐ・代わる・理由を言わずに待つ・確かめて戻る。どの段・どの報告から来ても同じ＝Flow._hold の1か所）：「はい、」を
            # 返してから待つ（確かめて戻る方には、続けて「それではこのままお返事を待っております。」＝_hold の back）。✅ 2026-11-06 日本時間2時28分41秒
            # ユーザー：「お客様：ちょっと待ってもらえますか。も、黙って待つはおかしいと思いますよ。「はい、」のような相づちがないと失礼です。」
            # ⛔ それまでは「相手はいま聞いていない」として流さなかった（待つように言ったその時、お客様はまだ電話口でこちらの返事を聞いている）。
            # 同じ発話にもう待つと返していたら（_hold の again）、上で重ねない。考えている途中の「はい。」をもう流していたら（_hold の acked）、重ねない
            return None if reply.get("acked") else "hai"
        if result == "thinking":
            # まだ考えている途中（Flow._thinking）：「はい。」の相づち（落ち着いた声で、語尾を上げずに短く＝phrases.ACK の aizuchi）のあと待つ。会話AIが話す通話で、
            # 報告の無いターンに会話AIが自分で「はい。」と言い、その声が流れた（voiced＝judged_turn）なら重ねない。✅ 2026-11-06 日本時間11時19分55秒 ユーザー「(1) ア」
            return None if reply.get("voiced") else "aizuchi"
        if result == "hesitate":
            # 迷っていると言い終えたお客様に、どういった点でお迷いかを1回伺う（Flow._hesitate_point）：「はい、」＋「どういった点でお迷いでしょうか？」
            # （✅ 2026-11-07 日本時間18時03分49秒 ユーザー「(1) ア」）。会話AIの声がもう流れた（voiced）なら流さない（Flow._hesitate_now）
            return None if reply.get("voiced") else "hai"
        if result in ("rejected", "wait", "confirm_target", "end_trouble", "explain_point"):
            # 差し戻し・相手の言葉の途中・目的の人かを確かめる（どの報告から来ても同じ＝確かめる
            # 一言だけ。聞き返しにもお詫びは重ねない＝✅ 2026-10-22 ユーザーの言い方「おかだ様でお間違えなかったでしょうか？」）・
            # こちらの不具合で終える（お詫びの一言「恐れ入ります。…」から始まる＝相づちを重ねない）。答えに添えられた質問に先に答える番は、上の1行目
            return None
        if reply.get("point_open") and result in ("next", "end", "recheck"):
            # 点を言っていただけないまま手順を終えたあとに挟む一言（Flow._advance）の頭：「はい、」・覚えていないと言われたら「ごもっともです。」・この電話の
            # 言いたくない・どの部分か分からない＝「承知しました。」（決めるのは _point_step の leave_open の1か所）
            # （この一言は返事のいちばん先に流すので、返事の中身の頭の決まり（締めの led・録音の続きの quiet・受け答えのあとの talked・答え方の頭「はい、」）より
            # 先に読む。⛔ 2026-11-06 日本時間5時56分55秒のご質問のあとの読み直しで見つけた：「それは言いたくないです。ところで料金は…？」に「はい、料金は…。
            # ご不満な点が…」と、答えの決まりの「承知しました。」が抜けた。⛔ 13時台の見直し（B1）で見つけた：ご説明のあとの締めで、締めの led が先に読まれ、
            # 「ご不満な点が…」の頭の「はい、」が抜けた）
            return reply["point_open"]
        if reply.get("led"):
            return None  # 返事の録音が、お礼・お詫びの一言から始まる（申し出へのお礼・在籍していないときのお詫び＝_led）。重ねない
        if reply.get("quiet"):
            return None  # 流した録音の続き（相手はまだ話していない＝_resume）：頭に相づちを重ねない
        if reply.get("thanks"):
            # 代わりの方の申し出・伝言をお願いする・質問に答えたあとの返事：「ありがとうございます、」（その方がまだ名乗りを聞いていなければ、
            # 録音のあいさつ・名乗りから始まるので流さない）
            return None if reply.get("renamed") else "arigatou"
        if reply.get("talked"):
            return "hai"  # 受け答え（世間話・お礼への返事）のあとに、いまの位置へ戻る（_resume）
        if result == "ask_back":
            # 1回目の「答えが質問に合わない」は「はい、」（続けてAIが「恐れ入ります。（聞くこと）を、もう一度…」）。
            # 聞き取れなかったとき・2回目からは、申し訳なさそうに読んだ「申し訳ございません、」（✅ 2026-10-14 ユーザー）
            # 最後の1回（「お声が聞き取れませんでしたので、…もう一度だけ…」）は、何回目でも「申し訳ございません、」
            # 理由を添えて伺い直す一言（_ask_again の form_reason。「恐れ入りますがSMSは…」）も、何回目でも「はい、」（お詫びを重ねない）
            first_form = (reply.get("ask_reason") == "form" and reply.get("ask_n") == 1 and not reply.get("ask_last")) or reply.get("form_reason")
            # 午前・午後を聞く：お詫びではなく「はい、」（続けてAIが「恐れ入ります、午前と午後、どちらになりますでしょうか？」）
            if first_form or reply.get("ask_reason") == "ampm":
                return "hai"
            return "moushiwake" if reply.get("ask_n") == 1 and not reply.get("ask_last") else "moushiwake_heavy"
        if result == "confirm_required":
            return AFTER[reply["after"]][2] if reply.get("after") else "hai"  # 何のあとの言い直しかは、言い直しを作る所（_ask_confirm）が決めたもの
        if result in ("next", "end", "recheck") and any(self._offer_taken(qid) for qid, _ in self._received_now()):
            # 順番に回す相づち（下の next）も、お礼を言った番に合わせる＝次の順番は「承知しました。」（お礼を2回続けない。⛔ 入れる途中、例の会話で見つけた：
            # 申し出のお礼を数えに入れていなかったので、次の申し出でない質問にも「ありがとうございます。」が続き、そのあとの順番も前とずれた）
            self.ack_count += 2 - self.ack_count % 2
            return "arigatou"   # 申し出（Question.offered）を受けていただいた。✅ 2026-11-12 日本時間13時40分05秒 ユーザー「(5) (ウ) は a」
        if result == "next" and reply.get("sorry"):
            return "shitsurei"  # 確かめに「いいえ」と言われたあと、かけ直しの質問の続きへ戻る（_call_target）
        if result == "next" and reply.get("question_id") in (CALLBACK_IDS["refusal"], CALLBACK_IDS["concern"]):
            return "shitsurei"  # ご本人に、今後のお電話をご遠慮したほうがよいかを伺う（「失礼いたしました。」＋ phrases.CALLBACK_ASK["refusal"]＝✅ 11-01 17時04分の (b)）
        if result == "next" and reply.get("question_id") == CALLBACK_IDS["other_wish"]:
            return "hai"  # 「どれも要らない」のあと、ほかのご希望を伺う：「はい、」＋「それでは（選択肢）のほかにご希望はございますでしょうか？」（Flow._wish_step）
        if result == "next" and any(w is not None and w["stage"] == "detail" and w["outcome"] is None and reply.get("question_id") == w["detail"]
                                    for w in (self.wish, self.handoff)):
            return "shouchi"  # ご希望の方法（ほかのご希望・詳しい者からのお話）の連絡先・LINE の伺い方を伺う：「承知しました。」＋「それでは恐れ入りますがメールアドレスを…」（「承知しました。」が続けば「かしこまりました。」＋「それではLINEのIDを…」＝11-04 20時27分）（Flow._wish_step）
        if result == "next" and reply.get("question_id") == CALLBACK_IDS["wait_or_later"]:
            return "kashikomari"  # 待つか、かけ直すかを1回伺う：「かしこまりました。」＋「このままお待ちしていた方が…」（Flow._wait_or_later）
        if result == "next" and reply.get("question_id") == CALLBACK_IDS["hurry"]:
            # 急いでいる方に伺う：「はい、」＋「それではまた改めてお掛けしてもよろしいでしょうか？」（お礼・承りは合わない。まだ名乗りを聞いていない方へは、
            # 録音のあいさつ・名乗りから始まるので流さない＝_hurry_ask の renamed）
            return None if reply.get("renamed") else "hai"
        if (name == "request_callback" or reply.get("later_asked")) and result == "next":
            # 「本人は出かけております」「あとでかけて」に「ありがとうございます、」は合わない。代わった方へは、録音のあいさつ・名乗りから始まる
            # ので流さない（_callback の renamed）。判定があとでかけ直すと読んだかけ直し（_wait_route の later_asked）は、会話AIの報告がどの操作でも同じ
            return None if reply.get("renamed") else "shouchi"
        if result == "call_target":
            # 目的の人をお呼びし直す（_call_target）：確かめに「いいえ」と言われたあとは「失礼いたしました。」。まだ確かめていない方（違うお名前を
            # 名乗った方）には、録音のあいさつから始まるので流さない
            # どなたをお呼びかを聞かれて答える（_asked_target）は「はい、」
            return "shitsurei" if reply.get("sorry") else "hai" if reply.get("answering") and not reply.get("renamed") else None
        if name == "report_party":
            return None  # 留守番電話・番号違い・受付に「承知しました」などは合わない（番号違いの締めの頭に「失礼いたしました。」を流すと、締めの「失礼」と重なる）
        if name == "replay_opening" and args.get("part") in OWN_PARTS:
            # 相手の質問に、AIが自分の言葉で答える頭は「はい、」（AIの言葉は「そちらにつきましては」などの受けの言葉から。受け皿の、知っている
            # ことの文をAIが言うときも）。答えの文を使わず、録音の一言（「かしこまりました。…」）を流すときは、重ねない（⛔ 4回目の前は
            # 「申し訳ございません、」＝断りの頭だった）
            # ⛔ 2026-11-06 日本時間5時56分55秒のご質問のあとの読み直しで見つけた：録音で流すかで決めていた＝AIが話す形では「はい、恐れ入ります。…」になった
            return None if reply.get("fallback") and args.get("part") != "own" else "hai"
        if name == "replay_opening" and str(args.get("part")).startswith(ANSWER_PREFIX):
            return "hai"  # 想定問答の答え：聞かれたことに答える頭は「はい、」
        if name == "replay_opening":
            # 名乗りの流し直しの頭は「失礼いたしました。」（6-13）。代わった方へは、録音のあいさつから始まるので相づちは流さない
            # 「AIですか？」への言い方（「何か問題でもございましたでしょうか？」）の頭に、お詫びは重ねない。質問に戻るときは「失礼いたしました。」
            return None if args.get("part") in ("handover", "ai") else "shitsurei"
        # 返事の頭に流す決まった一言（Flow._advance）の相づちは、その一言の決まり：聞き取れないまま先へ進むとき・締めの前の念のための確認の前でも同じ
        # （CL-1 の 11-03 のご判断の (7)。⛔ 見直しで見つけた：下の2つが先にあり、「はい、LINEでのご連絡のご希望を…」・「申し訳ございません、そのご希望を…」）
        if (reply.get("wish_told") or reply.get("anytime_told")) and result in ("next", "end", "recheck"):
            # ほかのご希望を申し伝える一言（業務に無いことはお約束しない）・今のところ分からない点は無いと言われたときに添える一言（「承知しました。何かわからないことが
            # ございましたら…」＝言い切った「無い」と同じ受けの言葉のあとに添える。✅ 2026-11-07 日本時間13時05分06秒）の頭は「承知しました。」（Flow._advance）
            return "shouchi"
        if reply.get("told") and result in ("next", "end", "recheck"):
            return "hai"  # 受け取った答えのあとに業務の一言を伝える（「はい、○○駐車場をご利用いただけますので…」＝Flow._advance）
        if result == "recheck":
            # 締めの前の念のための確認（最後の1回）：重いお詫び＋「お声が聞き取れませんでしたので…もう一度だけ…」。聞き返しを使い切ったすぐあとに入るときも
            # （最後の質問・先へ進むかを決める質問＝gate_of）。⛔ 2026-11-04 まで、下の「聞き取れないまま先へ進む」の「はい、」が先に当たっていた
            return "moushiwake_heavy"
        follow = "".join(x.get("text", "") for x in reply.get("play") or []) + (reply.get("say") or "")
        if result == "end" and follow.startswith(CLOSING_AFTER_DECLINE[:len("承知いたしました。")]):
            # お断りのあとの締め（「承知いたしました。お忙しいところ…」）の頭は「はい、」（✅ 11-02 6時33分「はい、承知いたしました。お忙しいところお時間を
            # いただきまして…」）。下の「わからない」「答えたくない」の「承知しました。」を重ねない（⛔ 2026-11-06 日本時間13時台の見直しで見つけた：
            # 最終目的の W1 の「わからない」「答えたくない」で「承知しました。承知いたしました。お忙しいところ…」）
            return "hai"
        received = self._received_now()
        if any(it.state in ("dont_know", "refused") for _, it in received):
            # 相手が「わからない」「答えたくない」と答えた：「承知しました。」（お礼は場面に合わない）。答えの報告でも、言い直しへの返事でも、締めるときも同じ
            # （受け取った答えで選ぶ＝_received_now。締めの前の念のための確認は、上のお詫びが先）
            return "shouchi"
        b = self.report_before
        if (name != "report_unclear" and not received and result == "next" and b is not None
                and any(it.state == "unresolved" and b.get(qid) != "unresolved" for qid, it in self.items.items())):
            # 聞き取れてはいるが、答えをいただけないまま「-」にして先へ進む（判定が決められないと読んだ答えを伺い直したあと など）：「承知しました。」（お礼は場面に
            # 合わない＝上の「わからない」と同じ）。⛔ 2026-11-09 日本時間4時36分49秒の (e) を入れる途中、流れの制御に通して見つけた：詳しい者からのお話を伺い直しても
            # 「うーん…。」のときに、順番で「ありがとうございます。続きまして…」とお礼を返していた
            return "shouchi"
        if name == "report_unclear" and not received:
            return "hai"  # 聞き返しを使い切って先へ進む：聞き取れていないので、「承知しました」「ありがとうございます」は合わない（曖昧な言葉の意味で答えが決まったら、下の答えの決まり）
        if result == "next" and self._hedged_next(reply):
            return "hai"  # 言い切っていない答えのあとに、業務設定の追って伺う質問で伺う（「はい、」＋「それでは、キャンセルになさいますか？…」＝_hedged_next）
        if result == "next" and reply.get("turned_down"):
            # お断り・目的から遠ざかる向きの答え（Question.turned_down の1か所）を受け取って次へ進む：「承知しました。」（お礼は場面に合わない＝上の
            # 「通話を断られた」と同じ。⛔ 2026-11-02 日本時間6時台の見直しで見つけた：「いえ、登録は結構です。」「どちらも要らないです。」にも、順番で
            # 「ありがとうございます。」を返していた。⛔ 2026-11-04 日本時間7時台：言い切った遠ざかる答えにも、同じく返していた）
            return "shouchi"
        if result == "next":
            self.ack_count += 1
            return ("shouchi", "arigatou")[(self.ack_count - 1) % 2]
        # 締めの相づちは、締めた理由（判定が決めた理由＝end_reason）で選ぶ。⛔ 2026-11-01 夜の見直しで見つけた穴：会話AIが報告した理由で選んでいたので、
        # 判定が「怒っている」と決めても、会話AIの報告が「取り次いでいただけなかった」なら「はい、」を流していた
        if result == "end" and self.end_reason == "callee_refused_call":
            return "shouchi"
        if result == "end" and self.end_reason == "callee_complaint":
            # 怒り・苦情：締めの言い方そのもの（「お忙しいところ申し訳ございませんでした。…」）がお詫び。頭に「承知しました、」「はい、」を
            # 流すと、相手の怒りを軽く受け流したように聞こえ、「申し訳ございません、」を流すとお詫びが2回続く ⇒ 流さない
            return None
        return "hai"

    @staticmethod
    def _check_args(name: str, args: dict, answer_parts: set = frozenset(), known: set = frozenset()) -> str | None:
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
            if k in args and args[k] not in table and not (k == "part" and args[k] in answer_parts) and not (k == "fallback" and args[k] in known):
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
        if self.phase == "calling" and self.callback_why and qid in self.callback_ids:
            # かけ直しの質問の途中で目的の人かを確かめに戻ったあと（_target_back）、相手がかけ直しの質問に答えた（戻る時刻などを伝えた＝
            # 目的の人ではない）：お呼びし直さずに、かけ直しの質問の続きに戻して受け取る。⛔ 2026-10-22 G-57 tc28（不在で別の方に代わる・S4）：
            # 代わった山田様の「夕方の5時頃には戻ると思います」を、確かめへの返事を待っているとして差し戻した
            self._keep_calling_turn()
            self._note("callback_resume", why=self.callback_why, read=[])
            self.order, self.phase, self.recheck_queue, self.current = list(self.callback_ids), "asking", [], qid
        if qid != self.current:
            return self._not_current(qid)
        q, it = self.questions[qid], self.items[qid]
        if (held := self._not_an_answer()):
            return held  # 答えではない言葉への報告は、値の形・曖昧の決まりより先に止める（取り次ぎの間に聞き返さない）
        if it.maybe_asked == "pending":
            # ここへ来るのは、その質問への新しい答え（言い直しへのお返事を待っている間の報告は上で差し戻す）：可能性の確かめへのお返事を待つ印は外す（「-」にした
            # あと締めの前の念のための確認で伺い直した答えを、可能性の2回目の言い方で言い直さない＝_ask_confirm の maybe_settle）。「どっちでもいい」の印も外す
            # （新しい答えが言い切っていなければ、その言葉で付け直す。⛔ 入れる途中、流れの制御に通して見つけた：締めの前の確認の「はい、車で行きます。」に、
            # 依頼者へ前の「どっちでもいいですよ。」を渡していた）
            it.maybe_asked, it.hedged = "done", None
        cond = a.get("condition_id")
        if cond not in q.all_conditions:
            shown = label(cond)
            # 選択肢の意味も添える（choices_note の1か所。⛔ 2026-10-22 の4回目の流し G-57 tc36・上司と名乗る方の L4：2つから選んでいただく問いの
            # 答え「はい、問題ございません。」を「はい」で報告し、差し戻しには選択肢の名前しか無かったので、AIは「答えた」などを試したあと、
            # 問いを自分の声で3回言い直した）
            return self._reject(f"{ARG_LABEL['condition_id']}「{shown}」は、この質問の選択肢にありません（選べるのは {'、'.join(q.labels)}）。"
                                + choices_note(q))
        if (back := self._terms_reported(qid, cond, a)) is not None:
            return back   # 申し出に添えた条件の言葉の差し戻し（書き直し・業務に無い方法はひとこと応える番へ）
        reported = cond
        if (judged := self._judged_answer()) is not None and judged not in q.all_conditions:
            # 判定の確かな読みが、この質問の答え（選択肢・どちらでも・どれも要らない・わからない・答えたくない）に無い：答えを決められない読みと同じ（会話AIの報告へ戻して
            # 決めない＝会話AIは判定に加えない。⛔ 2026-11-08 日本時間12時台に見つけた：判定を入口で当てる直しの途中、読みが選択肢に無いと会話AIの答えで受け取っていた）
            judged = UNDECIDED
        if judged is not None and judged != cond:
            # 会話AIは判定に加えない（✅ 2026-11-08 日本時間12時11分40秒 ユーザー：「そもそも会話 AI がなぜ判定に加わっているんですか？会話 AI の判定は当てに
            # ならないというのは、以前から分かっていることだから判定 AI を入れたんですよね。」）：判定の組が確かに読んだ答え（決められない、も）が、会話AIの選んだ
            # 答えに代わる＝会話AIがどの答えを報告しても、お客様に聞こえる返事と記録される答えは同じ。値の文字は会話AIが渡す＝値を伺う型で、判定は値を言ったと
            # 読んだのに会話AIが値を報告しないときは、お客様には何も言わずに値を報告し直させる（同じ発話で2回目なら、質問から聞き直す）
            if q.type not in CHOICE_TYPES and cond in SYSTEM_CONDITIONS and judged not in (*SYSTEM_CONDITIONS, UNDECIDED):
                if self.value_asked_at != (qid, self.callee_turns):
                    self.value_asked_at = (qid, self.callee_turns)
                    return self._reject(f"相手は答えの値を言っています（意味の判定）。相手が言った値を「{ARG_LABEL['value']}」に入れて、{qid} の答えとして、"
                                        "すぐ record_answer で報告してください。")
                return self._ask_again(qid, self._ask_kind(), "意味の判定は値を言ったと読んだが、会話AIが値を報告しない")
            it.set(it.state, f"会話AIの報告「{label(cond)}」は使わず、意味の判定の読み「{label(judged)}」で決める")
            cond = judged
        answered = cond not in SYSTEM_CONDITIONS and cond != UNDECIDED
        value = normalize_value(q.type, a.get("value"), q.unit) if answered or cond == reported else None
        if answered and (bad := kana_number(q.type, value)):
            return self._reject(kana_number_note(value, bad))  # お客様は正しく答えている＝AIの書き間違いなので、聞き返さずに書き直させる
        if answered and (why := form_problem(q.type, value)):
            # 値を聞く質問に「結構です」だけ：答えの値ではないので聞き返す（そのあとにまた言われたら、断りとして締めてよい＝_op_request_end）
            it.ambiguous_asked = it.ambiguous_asked or bool(ambiguous_word(self.heard_said or value))
            return self._bad_form(qid, value, why)
        if value is None and q.type not in CHOICE_TYPES and answered:
            # ⛔ 2026-10-13 G-25 で見つけた漏れ：値の要る質問で値の無い答えを受け付け、言い直しに「Noneで、よろしいでしょうか。」を
            #    入れていた（確かめない型なら、依頼主へ値の代わりに条件の名前が渡っていた）
            return self._reject(f"答えの値がありません。相手が答えた値を「{ARG_LABEL['value']}」に入れて、もう一度報告してください。")
        # 時刻の答えと一緒に受け取る理由（月単位の理由・「〇時まで」の理由）。午前・午後を伺ったときは、前の報告の理由を引き継ぐ（⛔ 2026-10-24 の
        # 見直し：午前・午後の答えの報告には理由が無いので、理由が消え、承認済みの時刻の問いでもう一度伺っていた）
        reasons = {k: a.get(k) for k in ("cycle", "until_why")}
        if answered and q.type in AMPM_TYPES:
            if it.pending_value is not None:
                reasons = {k: v or it.pending_reasons.get(k) for k, v in reasons.items()}
            value, it.pending_value, it.pending_reasons = merge_ampm(it.pending_value, value), None, {}
            value, ask = fill_ampm(value, self.business_hours, self.designated_limit)
            if ask and not it.ampm_asked:
                it.pending_reasons = reasons
                return self._ask_ampm(qid, value)
        if CALLBACK_FIELD.get(qid) == "time":
            # かけ直しの時刻の質問（分かる時 C9 も）：月単位の理由でその日・頃を選んだか（⛔ 2026-10-24 の見直し：前は C1 だけで、C9 では捨てていた）
            it.cycle = reasons["cycle"] if answered else None
        if q.callback == "later_time" and answered:
            # 「あとで」のかけ直しの「〇時まで」：いま電話を続けられない理由で、かけてよい時間が決まる（CL-2 の 4-149）。理由が相手の言葉に無ければ、
            # 承認済みの時刻の問いで1回だけ伺い直す（✅ 2026-10-24 ユーザー「進めてよい」＝CL-1 の提案）。伺い直しても理由が無ければ、そのまま受け取る
            # （判定は「理由が分からない」＝通話の中で確かめが要った時刻として CL-2 が扱う）
            until = bool(_UNTIL.search(value or ""))
            it.until_why = reasons["until_why"] if until else None
            if until and not it.until_why and not it.until_asked:
                return self._ask_until(qid, value)
        if cond in SYSTEM_CONDITIONS and judged is None and (taken := self._ambiguous_only(f"「{SYSTEM_LABEL[cond]}」")):
            # 相手の言葉が「大丈夫です」「結構です」だけなのに、AIが「わからない」「答えたくない」を選んだ：その言葉だけで決めない（判定の組の確かな読みが
            # あれば、その読みで決まっている＝会話AIの報告は判定に加えない）
            # ⛔ G-14 の続き：日時の質問への「大丈夫です」を、AIは 6本中2本「わからない」にした（聞き返さずに「-」になる）
            return taken
        it.condition, it.value = (cond, value) if cond != UNDECIDED else (None, None)
        said = self.heard_said or value
        if self.end_doubt == self.callee_turns and self.judged is None and (way := self._answer_direction(q)) is not None:
            # 電話を終える言葉か判定でも決まらず、いまの話への返事として受け取る言葉（_topic_first）の答え：受け取る前に、判定が読んだ向きを確かめる
            # （必須かどうかに関わらず。確かめるのは答えの正しさではなく、電話を続けてよいか＝電話を断っていたなら、この確かめへの返事で分かる。
            # ✅ 2026-11-01 日本時間20時56分 ユーザー：「「パンフレットはお送りしない方がいいということでしょうか？」のような流れが普通じゃないですか？」）
            self.end_doubt = None
            return self._check_again(qid, f"{END_DOUBT_WHY}（判定の読み「{label(way)}」）", cond, way)
        if cond == UNDECIDED:
            if q.type in CHOICE_TYPES and (decided := q.reply_choice(ambiguous_word(said))):
                cond = it.condition = decided   # 前の質問から意味が決まる曖昧な言葉だけの答え（取込で決めた＝G-49・10-17）：判定が決められないと読んでも、その意味
            else:
                return self._undecided_answer(qid)
        if cond in SYSTEM_CONDITIONS:
            why, back = self._checks(q, cond, policy=False)
            if why and q.checks_now and q.type in CHOICE_TYPES and q.offers(back):
                # ⛔ 2026-10-17：「いや、これからなんです」「家まだです」（聞き取りは正しい）を、AIが「わからない」と報告し、確かめずに受け取っていた
                return self._check_again(qid, why, cond, back)
            if why and q.required and not (back == cond and it.ask_backs):
                # 言い直す選択肢が無い（別の耳の文字と食い違う・前の話と食い違う）：質問から聞き直す（6-11 の回数の中で）。言い直す答えが受け取る答えのまま
                # （back＝cond＝別の耳の文字との食い違い）は最初の答えだけ（2回目の「わからない」は受け取る＝本当にわからない方に、何度も聞かない）。聞き取れて
                # いる（_heard_clearly）なら、お電話のせいにせず「はい、」＋「恐れ入ります。（聞くこと）をもう一度…」（_ask_kind の1か所）。判定が「決められない」と
                # 読んだ答えは、この前の _undecided_answer が受け持つ（⛔ 2026-11-08 日本時間12時台まで、ここで会話AIの「わからない」と判定の「決められない」の
                # 食い違いを聞き直していた＝会話AIの報告で決まる道だった）
                return self._ask_again(qid, self._ask_kind(), f"{why}（受け取る答えは「{label(cond)}」）")
            return self._take_system(qid, cond, a.get("unknown_why"))
        after = self.unreported_correction[1] if self.unreported_correction and self.unreported_correction[0] == qid else None
        self.unreported_correction = None
        if cond == ANY and q.maybe:
            # どちらの場合もありうると言われた答え（「どっちでもいいですよ。」）：受け取らずに、依頼者が備える答えの可能性を確かめる（_ask_confirm の maybe の1か所。
            # 必須でない質問でも確かめる＝答えをまだ言っていただいていない。⛔✅ 2026-11-09 日本時間4時36分49秒 ユーザー）
            it.set("heard", "どちらの場合もありうると言われた（依頼者が備える答えの可能性を確かめる）")
            return self._ask_confirm(qid, after="maybe")
        # 前の質問から意味が決まる曖昧な言葉（取込で決めた。G-49）：その選択肢を採る（AIの選んだ選択肢と違えば、こちらを採る）
        decided = q.reply_choice(ambiguous_word(said)) if q.type in CHOICE_TYPES else None
        if decided:
            cond = it.condition = decided
        why, back = self._checks(q, cond, policy=not decided, words=said, decided=decided) if q.type in CHOICE_TYPES else (None, None)
        if why:
            # 必須でない項目は、聞き返さない決まり（6-11）に合わせて確かめず「-」（その場で1回だけ伺い直す質問＝Question.asks_once は除く）
            if not q.checks_now:
                it.set("unresolved", f"{why}（「{said[:20]}」）・必須でないので確かめない")
                return self._dash(qid)
            return self._check_again(qid, why, cond, back)
        if decided:
            it.set("heard", f"「{said[:20]}」は、この質問では「{cond}」の意味（取込で質問の文から決めた）")
        else:
            it.set("heard", "聞き取れた")
        if q.confirm and after is None and q.type not in CHOICE_TYPES and Flow._hedged(said, self.heard_check):
            # 値を伺う型の、言い切っていない最初の答え（「たぶん15日の午後2時がいいと思います」）：選択肢の型と同じ1か所で確かめ方を選ぶ（_hedged_after。
            # ✅ 2026-11-02 日本時間1時22分 ユーザー：「お支払いや日時なんかの希望を伺う選択にも、「それでは…させていただきますね？」の形でいいと
            # 言ったよね？」。⛔ 前は値を伺う型だけ、言い切っているかを見ずに「（値）でよろしいでしょうか？」で言い直していた）
            after = self._hedged_after(q, cond)
        # 聞き返したあと・締めの前の念のための確認の答えは、選択肢の型も言い直して確かめる（6-11）。言い切っていなくても確かめずに受け取る質問
        # （Question.firm_not_needed＝ほかのご希望を伺う W1・ご承認いただけなかったときの N1〜N3）は、どの段でも言い直さない（CL-1 の 11-03 のご判断の (4)。
        # ⛔ 見直しで見つけた：念のための確認の段で W1 を伺うと「はい、ありがとうございます。ほかに希望は無い・やはり要らないということで…」と言い直した）
        restate = (it.ask_backs or self.phase == "rechecking") and restates(q)
        if q.confirm or restate or after:
            return self._ask_confirm(qid, after=after)
        it.set("accepted", "曖昧な言葉だが、前の質問から意味が決まる" if decided else "確かめない型で、はっきり聞き取れた")
        return self._advance("受け付けました。")

    # ── 確かめ（DESIGN.md 1章・6-10）を決める唯一の場所：最初の答えの報告にも、言い直しへの返事の報告にも、遅れて届いた確かめにも、これを当てる
    @staticmethod
    def _compare(q: "Question", said: str, reported: str | None, check: dict, policy: bool = True,
                 confirming: bool = False, words: str | None = None, decided: str | None = None) -> tuple[str | None, str | None]:
        """受け取る答え（reported。判定を使う通話で判定の組の確かな読みがあれば、入口でその読みに置き換えた答え＝_judged_answer。無ければ会話AIの報告。
        選択肢の型は選んだ選択肢・値を聞く型の言い直しへの返事は affirmed／corrected）を、聞き取り・言葉の決まり・前の話に当てる（会話AIの報告と判定の読みは
        比べない＝会話AIは判定に加えない。✅ 2026-11-08 日本時間12時11分40秒）：
        (1) 別の耳の文字（check["ear"]。聞き取り。6-10 の (5)。会話AIの聞き取り said と、読みにそろえて比べる）
        (2) 曖昧な答えの決まり（✅ ユーザー確定：「結構です」「大丈夫です」だけ・言い切っていない答えは、その言葉だけで決めない。当てる言葉は
            words＝会話AIの聞き取り、届いていなければAIの報告の値）。「結構です」だけの決まりは policy のときだけ（言い直しへの返事の「大丈夫です」は、
            言い直した意味を認める返事。取込で意味が決まった言葉も当てない）。言い切っていない答え（「たぶん」「かもしれない」）は、答えを選んだ報告と
            「合っている」の報告のすべてに当てる
        (3) 意味の判定（check の JUDGMENTS）の読みのうち、答えそのもの以外：前の話と合わない返事（off_talk）・前の話から決まる答え（prior_answer）・
            取込で意味が決まった言葉と違う選択肢の読み（_names_other）・言い切っていないという読み（_judged_unsure）
        返り値＝(確かめる理由, 言い直す答え)。理由が None ＝受け取ってよい。届いていない確かめは比べない（決めたあとに届いた読みは記録だけ＝on_check の
        _after_decision。最後まで届かなければ通話後の検証＝Flow.unchecked）。
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
        if reported not in SYSTEM_CONDITIONS and not confirming and restates(q) and UNCERTAIN.search(text):
            # 言い切っていない答えは、その言葉だけで決めない（✅ ユーザー確定）。最初の答えだけに当てる（言い直しへの返事は、言い直した
            # 意味に寄っていれば、その答えに「曖昧」の印を付けて受け取る＝Flow._hedged。✅ 2026-10-18 ユーザー：「yes,but 曖昧」）。
            # 「わからない」「答えたくない」の報告には当てない（「たぶん分からないです」は、そのまま「わからない」）
            return HEDGE_WHY, reported
        if (off := Flow._judged_off_talk(q, check)) is not False:
            # 前の話と合わない返事：その読み（宣言した選択肢＝off_talk の決まり）を確かめる答えに（前に言ったことと逆の返事＝✅ ユーザー「(イ)」。
            # お客様が考えを変えた場合も、聞き違いの場合も、確かめへの返事で分かる）
            return f"{TALK_WHY}（意味の判定の読みが、その前に相手が言ったことから決まる答えと食い違う）", off
        if (back := Flow._prior_back(q, check)) is not None:
            return PRIOR_BACK_WHY, back
        for kind in JUDGMENTS:
            if decided and Flow._names_other(q, check.get(kind), decided):
                # 取込で意味が決まった言葉だけの答え（decided＝Question.reply_choice）：決めた選択肢と違う選択肢を判定が読んだときだけ、判定の読みで確かめる
                # （✅ 2026-10-17 ユーザー：「人でも前の質問や会話から判断する言葉です」＝前の質問で決まれば確かめずにその意味・✅ 2026-11-06 日本時間
                # 18時51分27秒：判定がお断りなどと読めば今までどおり確かめる）。⛔ 2026-11-07 日本時間9時台に見つけた：判定の「決められない」でも確かめていた。
                # 会話AIの報告と判定の読みは比べない（会話AIは判定に加えない＝答えは入口で判定の読みに置き換えてある＝_judged_answer）。⛔ 2026-11-08 日本時間
                # 12時台まで、ここで会話AIの報告と判定の読みを比べ（_meaning_disagrees）、食い違えば会話AIの選んだ答えで言い直していた
                return f"意味の判定（「{label(check[kind])}」）と食い違う", check[kind]
        if reported not in SYSTEM_CONDITIONS and not confirming and restates(q) and not decided and Flow._judged_unsure(check):
            return f"{HEDGE_WHY}（意味の判定が、言い切っていないと読んだ）", reported
        return None, None

    @staticmethod
    def _prior(check: dict) -> str | None:
        """前の話の答え（唯一の読み口）：判定の読みに添えて届いたもの（meaning_judge.JudgePanel.ask）。前の話の答えは、いまの言葉と関わりなく前の話と
        質問だけで決まる（どの判定の読みに添えても同じもの）。判定の組が答えられなかったときは使わない（会話AIの報告と比べない＝会話AIは判定に加えない。
        ⛔ 2026-11-08 日本時間12時台まで、そのときだけ単独で受け（確かめの "prior"）、会話AIの報告と比べていた）。"""
        return next((p for k in JUDGMENTS if (p := getattr(check.get(k), "prior", None)) is not None), None)

    @staticmethod
    def _judged_off_talk(q: "Question", check: dict) -> str | bool:
        """お客様の言葉が、前の話と合わない（会話になっていない）返事：意味の判定の読み（中の名前）が、前の話の答えと食い違う（off_talk の
        1か所）ときは、その読み。どの判定の読みも食い違わなければ False。
        待つように言った・代わった人が話し始めた、の読みは答えではないので当てない（待つ・名乗る、の道が受け持つ）。⛔ 2026-10-31 の昼（G-53）：
        「わかる者に代わりますので、少しお待ちいただけますか。」を、d1-ultra も Jev も「取り次ぎを待つ」と正しく読み、会話になっていないとも
        読んだ（そのころの FITS）＝待たずに聞き直す側へ回った。
        判定の組が答えられなかった（判定の読みが無い）ときは当てない（会話AIの報告を前の話の答えと比べない＝会話AIは判定に加えない。決める時に確かな読みが
        無ければ F0 の決まり＝_missed）。prior＝前の話の答え（Flow._prior の1か所）。"""
        prior = Flow._prior(check)
        return next((str(check[k]) for k in JUDGMENTS if check.get(k) is not None and off_talk(q, str(check[k]), prior)), False)

    @staticmethod
    def _prior_back(q: "Question", check: dict) -> str | None:
        """いまの言葉だけでは決められない（判定が確かに「決められない」と読んだ）言葉を、判定の読みに添えて届いた前の話の答えで補う答え（唯一の場所＝prior_answer の
        決まりを当てる。最初の答えの _compare と、判定が「決められない」と読んだ答えの _undecided_answer が通る）。前の話の答えも判定の組の読み（会話AIの報告では
        ない）。無ければ None。⛔ 2026-11-08 日本時間12時台に見つけた：判定の読みで決める直しの途中、「決められない」の答えを前の話の答えで補わずに聞き直していた"""
        return next((a for k in JUDGMENTS if check.get(k) is not None and (a := prior_answer(q, str(check[k]), Flow._prior(check))) is not None), None)

    @staticmethod
    def _judged_unsure(check: dict) -> bool:
        """意味の判定が、寄っている答えを選んだうえで、言い切っていないと読んだ（meaning_judge.Reading.firm）。"""
        return any(getattr(check.get(k), "firm", True) is False and check.get(k) not in (UNDECIDED, *NOT_ANSWERS)
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
    def _names_other(q: "Question", meaning: str | None, decided: str) -> bool:
        """判定の読み meaning が、取込で決めた意味 decided と違う選択肢（宣言した選択肢か「どちらでも」「どれも要らない」）を名指ししているか
        （唯一の決まり＝取込で意味が決まった言葉だけの答えを確かめるか。流れの制御 _compare と測りの数え g53_meaning.flow_act が使う）。
        「決められない」「わからない」や、同じ選択肢の言い切っていない読みは名指ししていない＝確かめない。"""
        return meaning is not None and meaning != decided and q.offers(meaning)

    def _checks(self, q: "Question", reported: str | None, policy: bool = True, confirming: bool = False,
                words: str | None = None, decided: str | None = None) -> tuple[str | None, str | None]:
        """答えの報告を当てる相手の言葉と確かめ（heard_said・heard_check＝ふだんは、いまの発話）で、_compare を当てる。そろったと出ても、決める時に判定の
        確かな読みが無ければ（_missed）、その場で確かめる質問（Question.checks_now）は受け取らずにその場で確かめる（MISSED_WHY）。必須でない質問は確かめない
        決まり（6-11）なので受け取り、届かなかった確かめ（unchecked）として通話後の確かめが録音でやり直す（⛔ 2026-11-08 日本時間10時台に流れの制御に通して
        見つけた：必須でない質問にも MISSED_WHY を返し、受け取る所の「必須でないので確かめない＝「-」」の道へ流れて、会話AIの答えを捨てていた）。"""
        why, back = self._compare(q, self.heard_said, reported, self.heard_check, policy, confirming, words, decided)
        if why is None and q.checks_now and self._missed(reported, decided):
            return MISSED_WHY, reported
        return why, back

    def _missed(self, reported: str | None, decided: str | None = None) -> bool:
        """決める時（電話接続側の待つ長さの中＝on_decided）に、その発話に出した意味の判定（judged_turns）の確かな読みが1つも無いか（唯一の決まり＝_checks）：
        組が確かに読めなかった（確かでない読みは答えに使わない＝on_unsure）・待つ長さに間に合わなかった。会話AIの報告だけでは受け取らない（✅ 2026-11-01
        日本時間16時56分 ユーザー「会話AIは正しい判断ができるの？できないから、別のAIを使ったんじゃないのか？」）＝その場で確かめる（✅ 2026-11-07 日本時間8時57分04秒
        の承認の形）。当てないもの：判定を出していない発話（出さない質問・判定を使わない通話＝届くはずが無い）・「わからない」「答えたくない」（依頼者へは「-」「✕」＝誤った
        値は渡らない・答えたくないは二度と伺わない。理由は届いた読みだけで決める＝_why_of）・取込で意味が決まった言葉だけの答え（decided＝前の質問で決まる＝10-17）。"""
        if not self.judging or reported in SYSTEM_CONDITIONS or decided:
            return False
        turn = self.judged["turn"] if self.judged else self.callee_turns
        return bool(self.judged_turns.get(turn, set()) & set(JUDGMENTS)) and not any(self.heard_check.get(k) is not None for k in JUDGMENTS)

    def _judged_answer(self) -> str | None:
        """答えの段で、いまの言葉（heard_said・heard_check）の答えを決める判定の組の読み（唯一の場所＝答えの報告 _op_record_answer・言い直しへの返事の報告
        _op_confirm_field）：その言葉への確かな読みが届いていれば、その読み（届いた判定どうしが食い違えば「決められない」＝_meaning_of）。
        届いていなければ None＝決める時に確かな読みが無い（F0 の決まり＝_missed：その場で確かめる質問は受け取らずにその場で確かめる）・判定を使わない通話（会話AIの
        通話の試験）は読みが届かないので None（届いた読みがあるかだけで決める＝通話の印 judging では分けない：印の有無で同じ読みの扱いを変えない）。会話AIの報告（選んだ答え・「合っている」「違う値を言われた」）は判定に加えない＝この読みがあれば、会話AIがどの答えを報告しても、
        この読みが答え（会話AIの報告は、判定を待つきっかけ・値の文字＝お客様ご本人の「はい」で確かめる・ほかに言われたこと、だけ）。
        ✅ 2026-11-08 日本時間12時11分40秒 ユーザー：「そもそも会話 AI がなぜ判定に加わっているんですか？会話 AI の判定は当てにならないというのは、以前から
        分かっていることだから判定 AI を入れたんですよね。それを判定 AI と一致する時ということにしたら精度が下がるに決まってるじゃないですか。」
        ⛔ それまでは、会話AIの報告を独立した確かめの1つとして判定の読みと比べ（10-18「そろったときだけ受け取る」を、判定AIを入れた理由に当てて読み直して
        いなかった）、判定が確かに読んでいても、会話AIの報告しだいで聞き返し・会話AIの選んだ答えで言い直し・会話AIの「答えたくない」で受け取っていた"""
        turn = self.judged["turn"] if self.judged else self.callee_turns
        if turn in self.calling_words:
            # 呼んでいる段の発話：判定は呼んでいる段の質問（目的のお客様か＝judge_context）として読んだ＝この質問の答えの読みではない（かけ直しの質問の途中で
            # 目的の人かを確かめに戻ったあとの答え＝_op_record_answer の頭）。⛔ 2026-11-08 日本時間12時台に見つけた：目的のお客様の判定の「不在」を答えの読みと
            # 取り違え、「夕方の5時頃には戻ると思います。」を聞き直していた
            return None
        return Flow._meaning_of(self.heard_check)

    def _undecided_answer(self, qid: str) -> dict:
        """判定の組が、お客様の言葉はどの答えも指していない（決められない）と確かに読んだ答えの報告（唯一の場所＝_op_record_answer）：受け取らず、会話AIの選んだ
        答えも言い直す候補にしない（会話AIは判定に加えない＝_judged_answer）。その場で確かめる質問（Question.checks_now）は、言い直す選択肢の型なら目的に進む向きの
        1か所（_keep_toward_goal＝進む向きが1つならその向きで伺い、決まらなければ質問から聞き直す＝11-03 の「まだどちらとも言っていない答え」の決まり）、言い直さ
        ない質問（W1・C11・N1〜N3）は聞き直しの1か所（_check_again）、値を伺う型は質問から聞き直す。必須でない質問は聞き返さない決まり（6-11）なので「-」。
        迷っている・まだ決めていない・考えている途中・待つ・こちらへの問いは、この前に、先に判定の読みで応じる1か所（_judged_first）と尋ねられて答える1か所が受け持つ。"""
        q, it = self.questions[qid], self.items[qid]
        why = f"意味の判定（「{UNDECIDED}」）：どの答えも指していない"
        said = (self.heard_said or "")[:20]
        if q.callback in POINT_STAGES.values():
            # ご承認いただけなかったときの手順の段（N1〜N3・振り返りの一言・どの点かを伺う一言）：判定の「決められない」は、手順の決まりの「どちらとも取れない」
            # （「そうでしたっけ。」「なるほど」）＝「わからない」として受け取り、手順の1か所（_point_step・頭の言葉は _unknown_agreed＝判定の読みで「はい、」）が進める
            # （N1・振り返りは押さずに手順を終える＝✅ 2026-11-05 日本時間4時48分13秒・N2 はご納得を伺う＝11-02 5時56分）。⛔ 2026-11-08 日本時間12時台まで、ここへは
            # 会話AIが「わからない」と報告したときだけ入り、会話AIが別の答えを報告すると判定との食い違いで聞き直していた（会話AIの報告で手順が変わる形）
            return self._take_system(qid, "unknown")
        if not q.checks_now:
            it.set("unresolved", f"{why}（「{said}」）・必須でないので確かめない")
            return self._dash(qid)
        if (back := Flow._prior_back(q, self.heard_check)) is not None:
            # 前の話を指す言葉（「先ほど申し上げたとおりです。」）：判定の読みに添えて届いた前の話の答えを「それでは…ということでよろしいでしょうか？」で確かめる
            # （_compare と同じ1か所＝_prior_back。✅ 2026-10-31 の昼「(a) 検査OK」・夜「『それでは…ということでよろしいでしょうか？』は矛盾や言い直しの答えを
            # 最終的に確認する場合です」）。言い直さない質問は、_check_again が質問から聞き直す
            return self._check_again(qid, PRIOR_BACK_WHY, None, back)
        named = {str(v) for k in JUDGMENTS if (v := self.heard_check.get(k)) is not None and q.offers(str(v))}
        if q.type in CHOICE_TYPES and len(named) == 1:
            # 判定どうしが割れ（_meaning_of の「決められない」）、選択肢を名指しした判定の読みが1つだけ：その判定の読みで確かめる（言い直す質問は「念のため…」・
            # 言い直さない質問は、その選択肢で意味合いを尋ねる＝_check_again の1か所）。候補は判定の読み（会話AIの選んだ答えではない）。⛔ 2026-11-08 日本時間
            # 12時台まで、ここでは会話AIの選んだ答えで確かめていた
            back = named.pop()
            return self._check_again(qid, f"意味の判定が割れた（「{label(back)}」と「{UNDECIDED}」など）", None, back)
        if q.type in CHOICE_TYPES and restates(q):
            it.set("heard", f"{why}（「{said}」）・会話AIの選んだ答えは言い直す候補にしない")
            it.ambiguous_asked = True
            return self._keep_toward_goal(qid, "ambiguous")
        if q.type in CHOICE_TYPES:
            return self._check_again(qid, why, None, None)
        return self._ask_again(qid, self._ask_kind(), why)

    def _not_an_answer(self) -> dict | None:
        """意味の判定が、相手の言葉は答えではないと決めた（DESIGN.md 6-8）：答え・言い直しへの返事としては受け取らない（唯一の場所。
        答えの報告・言い直しへの返事の報告・代わった方への名乗りの流し直しが使う）。当たらなければ None。
        - 取り次ぎを待つように言われた（HOLD）：「はい、」の相づちのあと、AIは黙って待つ（差し戻しではない＝張り替えない。相づちは _ack）。
          代わった方が話し始めたら、名乗りを流し直す（その時機は、判定が HANDOVER と決めた発話で、プログラムが受け付ける）
        - 代わった別の人が話し始めた（HANDOVER）：答えではないので差し戻し、名乗りを流し直してもらう
        ⛔ 2026-10-17 G-50：取り次ぎの場面で、代わった方への名乗りの録音を、相手の「少々お待ちください」の直後に流した通話が約4割
           （代わった方には届かない）。流す時機をAIに任せていた。
        見る言葉は、答えの報告を当てる言葉（heard_said・heard_check。ふだんは、いまの発話）。"""
        said = (self.heard_said or "").strip()[:30]
        then = (f"代わった方が話し始めたら、replay_opening を{ARG_LABEL['part']}「{PART_LABEL['handover']}」で呼びます。" if self.opening
                else "代わった方が話し始めたら、名乗りと用件を短く伝えてから、いまの質問を聞きます。")
        if (routed := self._judged_first()) is not None:
            return routed
        meaning = Flow._meaning_of(self.heard_check)
        if meaning == HANDOVER:
            if self.phase != "calling":
                self._who_took_over()   # 分かる方に代わったなら、元の質問へ（AIが「代わった方へ」を呼ばない通話でも＝相手が替わったと決まった所）
            return self._reject(f"相手の言葉（「{said}」）は、代わった方が話し始めた言葉で、答えではありません。" + then.replace("代わった方が話し始めたら、", ""))
        return None

    def _judged_first(self, voiced: bool = False) -> dict | None:
        """会話AIがどの操作で報告しても（答え・言い直しへの返事・聞き取れない・名乗りの流し直しのどの部分・電話口の方・電話を終える報告）・報告しなくても（報告の無い
        ターン judged_turn）、先に判定の読みで応じる唯一の場所（当たらなければ None）：待つ・かけ直す・考えている途中（wait_reading → 待つ読みの1か所 _wait_route）・
        もう一度言ってほしい（_repeat_now）。会話AIの報告は判定を待つきっかけだけ（✅ 2026-11-01 日本時間17時04分 ユーザー (a)「OK」）。voiced＝報告の無いターンに
        会話AIの声がお客様に流れたか（judged_turn）。応じるかだけを知る所は _judged_due（同じ決まり）。
        ⛔ 2026-11-06 日本時間13時台の見直し（C6・C7）で見つけた：答えの報告から入る所（_not_an_answer）だけが判定の読みで応じ、代わった方への報告は取り次ぎを待つ
        読みだけ、電話口の方の報告は何も当てずに「受け付けました」だった（前から）。⛔ 23時22分41秒のご質問の見直し（W1・W4・W8）で見つけた：名乗りの流し直しは
        「代わった方へ」の部分だけ・電話を終える報告は待つ読みだけ・報告の無いターンは自前の写しでここを通り、ほかの部分（質問に戻る・受け答え）で待つように言われても
        「話を戻させていただきますね。」を流し、もう一度言ってほしいと頼まれてもご遠慮の一言を伺っていた"""
        if (w := self._wait_now()) is not None and (routed := self._wait_route(w, self._wait_read())) is not None:
            return routed
        if self._kept_on():
            # 待つかかけ直すか（H2）を伺っている間に、お客様が待たずにお話を続けた（判定がそろって「待たずに続ける」）：伺う前の位置へ戻って、その言葉を読み直す
            return self._wait_continue()
        # 迷っている（_hesitation の1か所）：どういった点でお迷いかを伺う（何回迷っても保留で締めない＝22時02分54秒）・同じ発話にもう応じていれば重ねない。
        # 報告で来ても報告の無いターンでも同じ返事の1か所（_hesitate_now）。選ぶ助けを伝える番は、尋ねられて答える番の1か所（_asked_now）が受け持つ
        if self._hesitation() is not None and (r := self._hesitate_now(voiced)) is not None:
            return r
        return self._repeat_now(voiced)   # もう一度言ってほしいと頼まれた：「失礼いたしました。」のあと、伺ったことを流し直す

    def _judged_due(self) -> bool:
        """先に判定の読みで応じる1か所（_judged_first）が、いまの言葉に応じるか（唯一の決まり＝電話を終える言葉か判定で決まらないときに、決まらないときの場面
        （ご遠慮の一言・急いでいる方への伺い＝_end_unsure）にしないか＝報告の無いターン _end_due_now）：待つ・かけ直す・考えている途中（_wait_handles）・
        もう一度言ってほしい（_repeat_due）。はっきり電話を終える言葉と読んだとき（END_REASON）は、この決まりの外（今までどおり締める）。
        ✅ 本書 9-4「お客様が考えている」：悩んでいる最中の言葉（「うーん。」）は、電話を終える言葉か決まらないときのご遠慮の一言の場面ではない（11-01 22時31分）・
        待つように言われたら「はい、」のあと待つ（11-06 2時28分41秒「どの段・どの場面でも同じ」）・聞き返されたら流し直す（11-06 15時20分30秒）。⛔ 2026-11-06
        日本時間13時台の見直しで見つけた：電話を終える言葉かの項目が「決められない」だと、待つ・考えている途中の読みより先にご遠慮の一言を伺っていた（「はい」と
        言われると発信禁止）。⛔ 23時22分41秒のご質問の見直し（W4）：もう一度言ってほしいの読みが、この決まりに入っていなかった（前の名前 _waits_not_end）"""
        return self._wait_handles(self._wait_now()) or self._kept_on() or self._repeat_due() or self._hesitation() is not None

    def _thinking(self) -> dict:
        """お客様がまだ答えを考えている途中（判定の読み THINKING＝wait_reading）：「はい。」の相づち（_ack の1か所＝phrases.ACK の aizuchi。落ち着いた声で、
        語尾を上げずに短く）のあと、何も言わずに待つ（唯一の返事。言い直し・質問の返事待ちはそのまま＝お客様が続けて話せば、その言葉をいまの質問・言い直しへの
        返事として受ける）。黙られたら、電話接続側が5秒で「何か気になる点がございましたらお気軽にお聞かせくださいね。」・さらに5秒で「いかがなさいましたか？」
        （phrases.THINKING_PROMPT＝9-4。1つ目は ✅ 2026-11-07 日本時間18時03分49秒「(2) イ」で急かさない一言に替えた）。
        同じ発話にもう応じていたら（報告の無いターンの入口 judged_turn のあとに、同じ言葉への報告が届いた）、相づちを重ねない（again）。
        ✅ 2026-11-06 日本時間11時19分55秒 ユーザー「(1) ア」"""
        again = self.thinking_turn == self.callee_turns
        self.thinking_turn = self.callee_turns
        said = (self.callee_said or "").strip()[:30]
        reply = {"result": "thinking", "silent": True,
                 "message": f"相手の言葉（「{said}」）は、まだ答えを考えている途中の言葉です（意味の判定）。答えではないので報告しません。何も言わずに、"
                            f"相手が続けて話すのを待ってください。{self._now()}"}
        if again:
            reply["again"] = True
        return reply

    def _hesitation(self) -> str | None:
        """迷っているお客様への応じ方（唯一の決まり＝先に判定の読みで応じる1か所 _judged_first・_judged_due と、尋ねられて答える番 _asked_now）。当たらなければ None。
        - "help"：どれにするかを迷っていると言い終えた（hesitates）で、選択肢ごとの良い点・気をつける点（Question.option_notes）がある／どういった点でお迷いかを
          伺ったあとのお返事が答えではない（迷っている点を言った）⇒ 会話AIが、確かめた自分の言葉で、業務の情報の範囲で選ぶ助けを伝え、お返事を待つ（尋ねられて
          答える1か所と同じ道＝お返事が答えなら受け取る・相づちだけなら最終の確かめ＝_resume）
        - "point"：迷っていると言い終えたが、まだ「どういった点でお迷いでしょうか？」を伺っていなくて、選択肢ごとの良い点・気をつける点が無い・もう伝えた
          ⇒「はい、」＋その一言を1回（_hesitate_point。同じ問いを2度言わない＝11-01「全く同じ質問を2度したら、機械だと思われてしまいませんか？」）
        - "final"：選ぶ助けを伝えたあと（選ぶ助けがいちばん新しい応じ方）に、お客様が「まだ決められない」「まだ決めていない」と言った（_not_deciding）
          ⇒ 保留で締める（_hesitate_now → _undecided_final＝「承知しました。それではこの件は保留とさせていただきますね。」。最初の答えの段でも言い直しの段でも
          同じ。18時台のご報告の (イ) の文「それでも決められなければ保留で締める。」＝22時02分54秒「イでOK」）。相づち（「なるほど。」＝決められない・理由は
          言っていない）は当てない（今までどおり、お客様の言葉への報告の道＝質問に戻る・最終の確かめ＝11-06 11時19分55秒）
        - 何回迷っても保留で締めない（選ぶ助けを続ける）。保留で締めるのは、選ぶ助けのあとの「まだ決めていない」（上の final）と、言い切っていない答えの
          最終の確かめのあとも決められないとき（11-06 11時19分55秒の (2)。_confirm_again）だけ
        - 最初の答え・言い直しへの返事で「まだ決めていない」と言った（_not_deciding。理由の読みが割れた・無い「分からない」も）も、すぐ保留で締めず、同じ
          確かめもくり返さずに、迷っていると同じく先に選ぶ助けをする（まだ助けも伺いもしていないとき・最終の確かめの前だけ）。✅ 2026-11-08 日本時間
          2時21分15秒 ユーザー「(1)〜(4)ともイでOK」の (2)(3)（⛔ 0時台の J-2 で一度、理由の読みがそろって「まだ決まっていない」の最初の答えだけに戻して伺った）。
          当てるのは選択肢から選ぶ質問（CHOICE_TYPES＝はい／いいえ・選択肢）だけ（「まだ決めていない」が「どれにするか迷っている」と同じになるのは、選ぶ
          ものがあるときだけ＝金額・日時などを伺う質問への「まだ決まっていない」に「どういった点でお迷いでしょうか？」とは伺わない）。理由は判定が読む
          （_why_of）ので、判定を使う通話だけ（判定を使わない通話の理由は会話AIの報告＝ここからは見えない）
        - 当てる質問は、お客様のご希望・申し出から選んでいただく業務の質問だけ（asks_wish の1か所＝保留で締める質問と同じ）
        - 判定の読みが答えを選んでいない（undecided）だけの応じ方（伺ったあとの選ぶ助け・final・最初の「まだ決めていない」）は、電話を終える言葉に寄っている・
          聞き取れていない・こちらへの問い、のときは当てない（_plain_reply＝迷っていない答えと同じ道：ご遠慮の確かめ・聞き返し・先に答える）。判定が「迷っている」
          と読んだときは、言葉に迷っている中身がある（考えている途中と同じく、ご遠慮の確かめの場面にしない＝11-01 22時31分）。理由がそろって「担当ではない」
          「いまは分からない」も当てない（担当ではない＝その方の決定ではない・いまは分からない＝業務の決まりの「分かる時を伺う」か保留）
        ⛔ 2026-11-08 日本時間0時台（11-07 23時26分13秒のご質問の見直しの H-1・H-3）：選ぶ助けのあとの「まだ決められないです」を受け取る所が無く
        （a496d7e2 で外したまま）、最初の答えの段は「申し訳ございません、お電話が少し遠いようでして…」・言い直しの段は同じ確かめの一言をもう一度・理由の
        読みが割れると「お分かりになる方は…」だった／「どういった点で…」のあとの「もういいです。」・聞き取れない言葉も選ぶ助けの番にしていた
        ✅ 2026-11-07 日本時間13時05分06秒 ユーザー：「カード と振り込みのメリットデメリットを把握しておき 例えば「カードだと カード会社のポイントがつくので
        そういう点も 考慮されるといいかもしれませんね 」のように促すのが一番いいんじゃないでしょうか。迷っていると言っているのに途中で話を終わらしたり
        どうなさいましたか と 急かすのは最悪な手ですよ。」・18時03分49秒「(1) ア」・22時02分54秒「まだ迷っているのにどうして締めるんですか？超一流の営業マンが
        そんなとこで締めますか？お客様を見放したり、次にしたらそこで終わりますよ。イでOK、しかし、その後の迷いでも保留では締めない。」（⛔ 18時台に入れた形は、
        選ぶ助けのあとも迷っていれば保留で締めていた＝私が 11-06 の「決められないのが最終の決定なら保留」を迷っているにも当てた）"""
        if self.phase not in ("asking", "rechecking") or self.after_answer is not None:
            return None
        qid = self.awaiting_confirm or self.current
        if qid is None:
            return None
        q = self.questions[qid]
        # 値を伺う質問（日時・数・金額）の言い直し（ひとまずの形・承っておく一言も）への返事を待っている：「まだ決まっていない」は、確定で確かめ直さずに、確定で
        # ない値を受ける1か所へ（tentative＝_tentative。事実を伺う質問も同じ理由＝✅ 2026-11-08 日本時間3時48分03秒「確定で確認するのはあいての意思に反します」）
        restating_value = q.type in ESTIMATE_TYPES and self.awaiting_confirm == qid and not q.callback and self.items[qid].value is not None
        if not (hears_hesitation(q) or restating_value):
            return None
        h = self.hesitating.get(qid, {})
        point, helped, estimate, tentative = h.get("point"), h.get("helped"), h.get("estimate"), h.get("tentative")
        if self.callee_turns in (point, helped, estimate, tentative):
            # この発話にはもう応じた（同じ発話に遅れて届いた報告・判定）：応じた番のまま（重ねないのは返事の1か所 _hesitate_now。選ぶ助けを伝えた発話は、
            # 答えた印＝question_answered_turn も立っているので、それより先に見る）。⛔ 2026-11-08 日本時間0時台の見直しの H-4：伺ったばかりの発話を「伺った
            # あとのお返事」と読み、お返事を待たずに選ぶ助けを言わせた・選ぶ助けを伝えた発話への遅れた報告を答えとして受け取り、保留で締めた
            return {point: "point", estimate: "estimate", tentative: "tentative"}.get(self.callee_turns, "help")
        if self.callee_turns in (self.question_answered_turn, self.asked_skip) or self.check_for not in (None, qid):
            # この発話の判定の読みは、発話の始めの質問についての読み（check_for）：保留で締めて次の質問へ進んだあとに同じ発話の遅れた報告が来ても、次の質問の
            # 読みにしない（⛔ 同じ見直しの H-1 を直す途中、流れの制御に通して見つけた形）
            return None
        readings = [self.check.get(k) for k in JUDGMENTS]
        # 答えを選んでいず、迷っていない答えの道でもなく（_plain_reply）、理由がそろって「担当ではない」「いまは分からない」（WHY_ROUTED）でもない言葉（唯一の決まり）
        open_ = undecided(readings) and self._plain_reply(readings) and self._why_of("unknown") not in WHY_ROUTED
        # 最初の答えの「まだ決めていない」（まだ助けも伺いもしていない・最終の確かめの前・判定を使う通話。理由の読みが割れた・無い「分からない」も）
        first_undecided = (open_ and helped is None and point is None and estimate is None and self.judging and not self.items[qid].final
                           and self._not_deciding(readings))
        if restating_value and open_ and self._not_deciding(readings):
            # 値の言い直しへの「まだ決まっていない」：同じ言い直しをくり返さず（11-01「全く同じ質問を2度したら、機械だと思われてしまいませんか？」）、すぐ保留で
            # 締めず（お客様が言った値を捨てない）に、ひとまずの形か承っておく形で受ける（_tentative）。目安を伺ったあと（estimate）に言っていただいた値も同じ
            return "tentative"
        if not hears_hesitation(q):
            return None
        if q.approval:
            # 説明した内容のご承認の質問への迷い（「うーん、同意するかどうか迷いますね。」）と、最初の答えの「まだ決められないです。」：ご承認いただけなかったときの
            # 手順と同じ道（_hesitate_now →「わからない（まだ決まっていない）」で受け取り、手順の1か所 _point_step が「はい、」＋「どういった点でお迷いでしょうか？」を
            # 伺ってご説明する）。保留では締めない。手順を始めるのは、ご承認の質問ごとに1回（印 approval）＝そのあとの迷っている点のお返事・ご説明・「ほかに…」は
            # 手順の質問が受け持つ。✅ 2026-11-08 日本時間2時21分15秒 ユーザー「(1)〜(4)ともイでOK」の (4)・3時15分04秒「(ウ)お勧めの形でOK」（「まだ決められない」も
            # (4) と同じ道。選ぶ質問の (3) と同じ決まり＝理由の読みが割れた・無い「分からない」も）
            return "approval" if (hesitates(readings) or first_undecided) and "approval" not in h else None
        if hesitates(readings):
            return "point" if point is None and (not q.option_notes or helped is not None) else "help"
        if not open_:
            return None
        if helped is not None and (point is None or point < helped) or estimate is not None:
            # 選ぶ助け・目安を伺ったあと：まだ決めていない＝保留・相づちなどは今までどおり（目安を伺うのは1回だけ）
            return "final" if self._not_deciding(readings) else None
        if point is not None:
            return "help"   # どういった点でお迷いかを伺ったあとの、答えではないお返事（迷っている点）
        if not first_undecided:
            return None
        if q.type in CHOICE_TYPES:
            # まだ助けも伺いもしていない「まだ決めていない」（最初の答えも、言い直しへの返事も＝(イ)。理由の読みが割れた・無い「分からない」も。理由がそろって
            # 「担当ではない」「いまは分からない」は上で外した）：すぐ保留で締めず、同じ確かめもくり返さずに、先に選ぶ助け。最終の確かめ（Item.final）のあとも
            # 決められないのは、お客様の最終の決定＝保留（11-06 11時19分55秒の (2)。_confirm_again）。✅ 2026-11-08 日本時間2時21分15秒 ユーザー
            # 「(1)〜(4)ともイでOK」の (2)(3)（⛔ 0時台の J-2 で一度、理由の読みがそろって「まだ決まっていない」の最初の答えだけに戻して伺った）
            return "help" if q.option_notes else "point"
        if q.type in ESTIMATE_TYPES and self.awaiting_confirm is None:
            # 値を伺う質問（日時・数・金額）の最初の答えの「まだ決まっていない」：すぐ保留で締めずに、1回だけ「おおよその目安」を伺う（同じ (1)。一流の営業＝すぐ
            # 引かずに、決める手がかりを1つ伺う）。理由の読みが割れた・無い「分からない」も、選ぶ質問の (3) と同じ決まり（✅ 2026-11-08 日本時間3時15分04秒
            # ユーザー「(イ) 前者は、選ぶ質問の (3) と同じく、1回「おおよその目安だけでも…」を伺う形にします。：OK」）。言い直し（「それでは15日とさせていただきますね？」）
            # への返事は今までどおり（⏳ 前後を省かない例で伺い直す＝帳面 1 の (11)）
            return "estimate"
        return None

    def _not_deciding(self, readings) -> bool:
        """お客様が「まだ決めていない」「まだ決められない」と言ったか（唯一の決まり＝選ぶ助けのあとに保留で締める _hesitation の final）：届いた判定の読みが
        どの答えも選んでいない（undecided）で、わからない（わからない・まだ決めていない、と言った）と読んだ判定があるか、理由の読みがそろって「まだ決まって
        いない」。決められないとだけ読んだ言葉（相づち「なるほど。」・選ぶのを避けた）は入れない（判定の「決められない」は、判定が言葉から決められないこと＝
        meaning_judge の UNDECIDED）。"""
        return undecided(readings) and ("unknown" in {str(r) for r in readings if r is not None} or self._why_of("unknown") == "undecided")

    def _plain_reply(self, readings) -> bool:
        """答えを選んでいないお客様の言葉（undecided）を、迷っているお客様への応じ方（_hesitation の伺ったあとの選ぶ助け・final・最初の「まだ決めていない」）で
        読んでよいか（唯一の決まり）：電話を終える言葉に寄っていない（判定が電話を終える言葉と読んでいない・決まらない読みではない＝_end_due_now と同じ
        _end_unsure_now）・聞き取れている（_heard_clearly）・こちらへの問いではない（asks_us＝尋ねられて答える番 _asked_now が先に答える）。当たらなければ、
        迷っていない答えと同じ道（ご遠慮の確かめ・聞き返し・先に答える）。⛔ 2026-11-08 日本時間0時台（11-07 23時26分13秒のご質問の見直しの H-3）：
        「どういった点でお迷いでしょうか？」のあとの「もういいです。」にご遠慮の確かめをせず（_judged_due が飛ばした）、聞き取れない言葉にも選ぶ助けを
        言わせた"""
        end = self._end_now()
        return (end not in END_REASON and not self._end_unsure_now(end) and self._heard_clearly()
                and not asks_us(readings))

    def _heard_clearly(self) -> bool:
        """いまのお客様の言葉が聞き取れているか（唯一の決まり＝はっきり待つように言われたか _wait_clear・迷っているお客様への応じ方 _plain_reply）：会話AIが
        この発話を聞き取れないと報告していない（unclear_turn＝入口 _apply_one）・別の耳の文字が届いていれば、会話AIの聞き取りと食い違わない（確かめの (1) と
        同じ heard_alike。届いていない確かめは比べない＝_compare と同じ）。"""
        said, ear = self.callee_said or "", self.check.get("ear")
        return self.unclear_turn != self.callee_turns and (ear is None or heard_alike(said, ear))

    def _ask_kind(self) -> str:
        """意味が決まらない答え（判定の読みと会話AIの報告が割れた・曖昧な答えで言い直す言葉が無い・目的に進む向きが1つに決まらない）を質問から聞き直すときの
        聞き返しの種類を決める唯一の場所（_ask_again に渡す）：聞き取れている（_heard_clearly）なら、お電話のせいにせず "form"＝「はい、」＋「恐れ入ります。
        （聞くこと）をもう一度…」（本書 9-5 の (3)「お客様ははっきり話しているので、電話のせいにする聞き返しは合わない」＝G-14）・聞き取れていなければ
        "unclear"＝「申し訳ございません、」＋「お電話が少し遠いようでして…」。会話AIが聞き取れないと報告したとき（report_unclear）は、ここを通らない（聞き取りの
        問題）。⛔ 2026-11-08 日本時間0時台（K-13）は最初の答えの1か所だけ・11時台（F1 の設計で流れの制御に通して見つけた）に確かめ直しの1か所の2つも通した"""
        return "form" if self._heard_clearly() else "unclear"

    def _hesitate_now(self, voiced: bool = False) -> dict:
        """迷っているお客様（_hesitation が当たった）に応じる返事の唯一の場所（先に判定の読みで応じる1か所 _judged_first から＝会話AIの報告で来ても、報告の
        無いターン judged_turn でも同じ。お客様に聞こえる言葉も同じ）：
        - この発話にもう応じた（どういった点でお迷いかを伺った・選ぶ助けを伝えた＝同じ発話に遅れて届いた報告・判定）：重ねず、お返事を待つ（again）
        - voiced＝報告の無いターンに会話AIの声がもう流れた（会話AIが話す通話で、報告の代わりに声で応じた）：重ねず、応じた印だけ立てる（考えている途中の
          「はい。」・もう一度言ってほしいと同じ voiced の決まり）
        - point：「はい、」＋「どういった点でお迷いでしょうか？」（_hesitate_point）
        - help（選ぶ助けを伝える番）：None＝尋ねられて答える番の1か所が受け持つ（_asked_now。報告で来たら入口 _apply_one の差し戻し・報告の無いターンは
          _asked_turn。会話AIの答え＝自分の言葉の操作は、ここで止めない）
        ⛔ 2026-11-08 日本時間0時台（11-07 23時26分13秒のご質問の見直しの H-7・H-4）：会話AIの声のあとにも「はい、どういった点で…」を重ねた・同じ発話の
        遅れた報告で、伺ったばかりなのに選ぶ助けを言わせた"""
        kind = self._hesitation()
        h = self.hesitating.setdefault(self.awaiting_confirm or self.current, {})
        said = (self.callee_said or "").strip()[:30]
        head = f"相手の言葉（「{said}」）は、答えではなく、どれにするか迷っている・まだ決めていない言葉です（意味の判定。報告は要りません）。"
        if self.callee_turns in (h.get("point"), h.get("helped"), h.get("estimate"), h.get("tentative")):
            return {"result": "hesitate", "silent": True, "again": True,
                    "message": f"{head}この言葉には、もう応じました。何も言わずに、相手のお返事を待ってください。"}
        if kind == "tentative":
            # 値の言い直しへの「まだ決まっていない」：確定でない値を受ける1か所（_tentative＝ひとまずの形か承っておく形・承っておく一言のあとなら受け取る）。会話AIの
            # 声がもう流れた（報告の無いターン）なら重ねない（final と同じ＝答えは報告で当てる）。応じた印は発話ごと（同じ発話の遅れた報告で受け取らない）
            if voiced:
                return None
            h["tentative"] = self.callee_turns
            return self._tentative(self.awaiting_confirm, undecided=True)
        if kind == "final":
            # 選ぶ助けのあとも、まだ決めていない＝お客様の最終の決定：保留で締めて次へ（_undecided_final の1か所＝最終の確かめのあとと同じ）。会話AIの声がもう
            # 流れた（報告の無いターン）なら重ねない（何を言ったか分からない声に、保留の一言を重ねない＝答えは報告で当てる）
            if voiced:
                return None
            qid = self.awaiting_confirm or self.current
            self.awaiting_confirm = None
            return self._undecided_final(qid)
        if kind == "approval":
            # ご承認の質問への迷い：「わからない（まだ決まっていない）」で受け取り、ご承認いただけなかったときの手順を始める（_take_system の opens_point＝業務の
            # 決まりの結論・分かる方を伺う、は当てない＝迷っているのは結論ではない。手順の1か所 _point_step が伺う）。会話AIの声がもう流れた（報告の無いターン）
            # なら重ねない（final と同じ＝答えは報告で当てる）
            if voiced:
                return None
            qid = self.awaiting_confirm or self.current
            h["approval"] = self.callee_turns
            self.awaiting_confirm = None
            return self._take_system(qid, "unknown", "undecided", settled=True, opens_point=True)
        if kind not in ("point", "estimate"):
            return None
        if voiced:
            h[kind] = self.callee_turns
            return {"result": "hesitate", "silent": True, "voiced": True,
                    "message": f"{head}あなたの声が相手に流れました。何も言わずに、相手のお返事を待ってください。"}
        return self._hesitate_point(kind)

    def _asked_turn(self, voiced: bool) -> dict:
        """報告の無いターン（judged_turn）が、答えの段で尋ねられた・迷っているお客様に選ぶ助けを伝える番（_asked_now）だったときの返事（唯一の場所。報告で
        来たときは入口 _apply_one の差し戻しが同じ知らせ＝_now の _asked_note）：
        - voiced＝会話AIの声が流れた＝会話AIが声で答えた：答えさせる知らせは出さずに、お客様の返事を待つ（答えた印・選ぶ助けを伝えた印＝_await_reply の
          1か所）。依頼者へ渡すお客様の質問（_question_note）に残すのは、お客様の問いのときだけ（迷っている言葉は問いではない）
        - そうでなければ、会話AIに、聞かれたことに答える番・選ぶ助けを伝える番だと知らせる（会話AIが話す＝speak。相づちは会話AIの答えのほう）
        ⛔ 2026-11-08 日本時間0時台（11-07 23時26分13秒のご質問の見直しの A-6・H-7・H-11）：報告の無いターンは選ぶ助けの番を受け持たず、良い点のある業務では
        無言だった・会話AIの声のあとの印を立てる所（unreported_note）が、迷っている言葉を依頼者へ渡すお客様の質問に残した"""
        helping = self._hesitation() == "help"
        what = "どれにするか迷っている・まだ決めていない言葉" if helping else "こちらへの問い"
        head = f"いまの相手の言葉は、答えではなく、{what}です（報告は要りません）。"
        if voiced:
            self._question_note({"answered": True, "checked": None})   # お客様の問いのときだけ残る（_question_note の1か所）
            self._await_reply("own")
            return {"result": "callee_asked", "silent": True, "voiced": True,
                    "message": f"{head}あなたの声が相手に流れました（答えた）。何も言わずに、相手の返事を待ってください。"}
        return {"result": "callee_asked", "speak": True, "message": head + self._now()}

    def _hesitate_point(self, kind: str = "point") -> dict:
        """迷っている・まだ決めていないお客様に、1回だけ伺う返事（唯一の場所。_hesitation の point・estimate）：「はい、」（_ack_choice の hesitate）＋
        - point：選択肢ごとの良い点・気をつける点が無いとき「どういった点でお迷いでしょうか？」（共通の一言の録音 hesitate_point）。✅ 2026-11-07 日本時間
          18時03分49秒 ユーザー「(1) ア」
        - estimate：値を伺う質問（日時・数・金額）の最初の「まだ決まっていない」に「それではおおよその目安だけでもお伺いしてもよろしいでしょうか？」（共通の
          一言の録音 estimate_ask）。お答えが目安の値なら、ふつうの答え（言い切っていない答え）と同じ1か所で受け取る。それでも決まらなければ保留（final）。
          ✅ 2026-11-08 日本時間2時21分15秒 ユーザー「(1)〜(4)ともイでOK」の (1)
        録音で流せなければ会話AIに言わせる。伺った印は質問ごと（hesitating の point・estimate＝同じ発話に重ねない・2回伺わない）。"""
        qid = self.awaiting_confirm or self.current
        self.hesitating.setdefault(qid, {})[kind] = self.callee_turns
        said = (self.callee_said or "").strip()[:30]
        if kind == "estimate":
            rid, text = "estimate_ask", ESTIMATE_ASK
            asked = ("まだ決まっていないという言葉です（意味の判定）。答えではないので報告しません。おおよその目安を伺いました。相手のお返事を待ってください"
                     "（目安の値を言われたら、ふつうの答えと同じく報告してください）。")
        else:
            rid, text = "hesitate_point", HESITATE_POINT
            asked = ("どれにするか迷っている・まだ決めていないという言葉です（意味の判定）。答えではないので報告しません。どういった点でお迷いかを伺いました。"
                     "相手のお返事を待ってください（迷っている点を言われたら、選ぶ助けになることをお伝えする番を知らせます）。")
        reply = {"result": "hesitate", "message": f"相手の言葉（「{said}」）は、{asked}"}
        line = [{"kind": "recorded", "id": rid, "text": text}]
        if self._plays_recorded(line):
            return reply | {"play": line, "message": played_note(text) + reply["message"]}
        return reply | {"say": text, "message": say_note(text) + reply["message"]}

    def _repeat_now(self, voiced: bool = False) -> dict | None:
        """お客様に、直前にこちらが伺ったこと（いまの質問・言い直しの確かめ）をもう一度言ってほしいと頼まれた（判定の読みがそろって REPEAT＝_meaning_of）：
        「失礼いたしました。」（_ack_choice の repeat）のあと、伺ったことを戻る1か所（_continue）から流し直す返事（唯一の場所。答えの報告・言い直しへの返事の
        報告・聞き取れないの報告・名乗りの流し直し・報告の無いターン judged_turn の、どこから来ても同じ）。当たらなければ None。
        質問は頭に「それでは」（AGAIN_LEAD＝もう流した質問へ戻る形）・言い直しの返事待ちは言い直しをもう一度・システムの質問は同じ一言の録音。
        聞き返しの回数（ask_again）には数えない（こちらが聞き取れなかったのではない）。
        直前にこちらがお客様に応えて、お返事を待っている（お客様のご質問・お話に答えた・ご納得いただけなかった点をご説明した・「AIですか」に応えた＝
        own_pending・ai_pending）なら、もう一度言ってほしいのは、その答え・ご説明：「失礼いたしました。」のあと、応えた中身（reply_said）をそのまま言い直し、
        お返事を待ち続ける（何を言ったかを持っていない＝会話AIが声だけで応えたときは当てない＝_repeat_due）。そのほか、こちらが最後に言ったのが伺ったことで
        ないとき（お話がそれた digressed・答えに添えられた質問の続き after_answer・応える前の言葉の答えを持っている _held_now・まだご説明していない
        _explaining）は当てない。同じ発話にもう流し直していたら（報告の無いターンの入口のあとに、同じ言葉への報告が届いた）、重ねない（again）。
        voiced＝報告の無いターンに会話AIの声がもう流れた（会話AIが話す通話で、会話AIが自分で言い直した）：流し直しは重ねず、流し直した印だけ立てる（同じ言葉への
        遅れた報告でも流し直さない。⛔ 23時22分41秒のご質問の見直し（W8）：印を立てず、あとの報告で2回目を流した）。
        ✅ 2026-11-06 日本時間15時20分30秒 ユーザー「(1)…プログラムが「失礼いたしました。」＋「それでは（質問）」を流します。OK」"""
        if not self._repeat_due():
            return None
        if self.repeat_turn == self.callee_turns or voiced:
            self.repeat_turn = self.callee_turns
            if self.own_pending or self.ai_pending:
                self.reply_started = True   # お返事を待っている（応えた中身への返事を待つ印は、このあとも残す＝_apply_one）
            return {"result": "replayed", "silent": True, "again": True,
                    "message": f"この言葉には、もう言い直しました。何も言わずに、相手の答えを待ってください。{self._now()}"}
        self.repeat_turn = self.callee_turns
        if self.own_pending or self.ai_pending:
            # 直前にこちらが応えた（答え・ご説明・受け答え・「AIですか」に応えた一言）のをもう一度言ってほしい：「失礼いたしました。」のあと、応えた中身（reply_said＝
            # 確かめた文・録音）をそのまま言い直し、今までどおりお客様のお返事を待つ（待つ印は残す）。⛔ 2026-11-08 日本時間6時台（11-07 23時26分13秒のご質問の
            # 見直しの A-3）：ここでは伺ったことしか流し直さず、ご説明の直後の「もう一度お願いできますか？」には報告の無いターンは無言・会話AIの「質問に戻る」で
            # 「ほかにご納得いただけない点は…」へ進んだ
            said = self.reply_said
            self.reply_started = True
            return ({"result": "replayed", "repeat": True} | {k: said[k] for k in ("play", "say", "speak", "silent") if k in said}
                    | {"message": "相手に、直前のこちらの言葉をもう一度言ってほしいと頼まれました（「失礼いたしました。」はプログラムが流します）。"
                                  + said.get("message", "")})
        return self._continue([], "") | {"repeat": True}

    def _repeat_due(self) -> bool:
        """もう一度言ってほしいと頼まれ、直前にこちらが言ったことを言い直す場面か（唯一の決まり＝_repeat_now・_judged_due）：伺ったこと（いまの質問・言い直しの
        確かめ）か、お客様に応えてお返事を待っている中身（own_pending・ai_pending。何を言ったかを持っているとき＝reply_said。会話AIが報告せずに声だけで
        応えたときは持たない＝会話AIが言い直す）。お客様のお話がそれた・応える前の言葉の答えを持っている・ご説明をまだしていない、などで、こちらが最後に
        言ったのが伺ったことでも応えた中身でもないときは当てない。"""
        if self.phase == "calling" or Flow._meaning_of(self.heard_check) != REPEAT:
            return False
        if self.own_pending or self.ai_pending:
            return self.reply_said is not None
        return not ((self.awaiting_confirm is None and self.current is None) or self.after_answer is not None
                    or self.digressed or self._held_now() is not None or self._explaining())

    def _hold(self, back: bool = False) -> dict:
        """待つように言われた：「はい、」の相づち（_ack の1か所）のあと、AIは黙って待つ（唯一の返事。差し戻しではない＝張り替えない）。代わった方が
        話し始めたら、名乗りを流し直す（その時機は、判定が HANDOVER と決めた発話で、プログラムが受け付ける）。同じ発話にもう待つと返していたら
        （報告の無いターンの入口 judged_turn のあとに、同じ言葉への報告が届いた など）、相づちも一言も重ねない（again）。同じ発話に、考えている途中の「はい。」を
        もう流していたら（_thinking のあとに待つ読みが遅れて届いた）、相づちは重ねない（黙って待つなら何も流さない・確かめて戻る方には一言だけ＝acked）。
        ⛔ 2026-11-06 日本時間23時22分41秒のご質問の見直し（W7）：この決まりが報告の無いターンの道（_wait_rethought）にだけあり、報告から来ると「はい。」のあとに
        「はい、」を重ねた"""
        thought = self.thinking_turn == self.callee_turns   # この発話に、考えている途中の「はい。」をもう流した
        again = self.holding or (thought and not back)   # 待つ印は、お客様が新しく話すたびに下ろす（on_callee）＝立っていれば、この発話にもう待つと返した
        said = (self.callee_said or "").strip()[:30]
        # 相手が電話口を離れた理由は決めつけない：呼んでいる段はお取り次ぎ、繋がったあと・目的の人のいない業務は、確かめに行った・ほかの方に代わる
        # のどちらもある（⛔ 2026-10-24 の見直しで見つけた穴：どの場面でも「ほかの人に取り次いでいます」「代わった方が話し始めたら名乗りを流し直す」と
        # 伝えていた＝目的のお客様ご本人が確かめに離れて戻られても、AIは代わった方と思い込み、同じ方に名乗りを流し直させうる）
        away = ("相手は、お取り次ぎのために電話口を離れています" if self.phase == "calling" else
                "相手は、電話口を少し離れています（確かめに行かれた・ほかの方に代わる、のどちらもあります）")
        new_person = (f"replay_opening を{ARG_LABEL['part']}「{PART_LABEL['handover']}」で呼びます" if self.opening
                      else "名乗りと用件を短く伝えてから、いまの質問を聞きます")
        then = f"別の方に代わったら（「お電話代わりました」など）、{new_person}。同じ方が戻られたら、その方のお話から続けます。"
        self.holding = True
        if self.asking_reception and self.phase != "calling":
            self.callback_held = True  # かけ直し・お立場の質問の途中の取り次ぎ：このあと出る方は、新しい方かもしれない（_op_replay_opening）
        # 待つ長さ：業務設定（初め5分）。相手がそれより長く待つ理由（長さ）を言えば、その長さ（✅ 2026-10-24 ユーザー：「相手から長く待つ必要がある
        # 理由がなければ5分、理由があればそれまで待つ」＝短い長さ「1分ほど」は、5分より早く締める理由にしない。⛔ 2026-10-24 の見直し（調べ役）：
        # 言われた長さをそのまま使い、「1分ほど」で1分後に締めていた）。電話接続側は、この長さのあいだ黙って待ち（その間も、会話を進める流れから
        # 1秒より短い間ごとに call.beat を呼びながら＝DESIGN.md 6-8・CL-2 の 4-156）、誰も話さなければ on_hold_timeout を呼ぶ（相手がまた話せば、その言葉で決め直す）
        wait = self.hold_wait = max(wait_seconds(self.callee_said) or 0, self.hold_wait_s)
        reply = {"result": "hold", "silent": True, "hold_wait_s": wait,
                 "message": f"{away}（「{said}」）。答えではありません。何も言わずに、相手がまた話し始めるのを待ってください。{then}"}
        if again:
            reply["again"] = True
        elif back:
            # この電話の間に確かめて・相談して戻る方（HOLD_BACK）：「はい、」＋「それではこのままお返事を待っております。」（共通の一言の録音 wait_reply）を流してから
            # 待つ（待つ長さ・待ちきったときの一言は同じ。✅ 2026-11-05 日本時間23時19分06秒・23時56分48秒 ユーザー（会話2）「(2) …OK」）
            line = [{"kind": "recorded", "id": "wait_reply", "text": WAIT_REPLY}]
            if self._plays_recorded(line):
                reply |= {"play": line, "message": played_note(WAIT_REPLY) + reply["message"]}
            else:
                reply |= {"say": WAIT_REPLY, "silent": False, "message": say_note(WAIT_REPLY) + reply["message"]}
            reply["back"] = True
            if thought:
                reply["acked"] = True   # 「はい。」はもう流した：「それではこのままお返事を待っております。」だけ（_ack_choice）
        return reply

    # ── 取り次ぎ・保留のあと、待つ長さ（_hold の返事の hold_wait_s）のあいだ誰も話さなかった（電話接続側が呼ぶ）
    def on_hold_timeout(self) -> dict | None:
        """保留で待ちきった：決まった締め（phrases.HOLD_TIMEOUT＝✅ 2026-10-24 ユーザーの言い方「お返事がないようですので、また、あらためて、
        かけ直させていただきます。それでは、いったん失礼いたします。」）で終える（かけ直しは CL-2 の決まりのまま＝日を改めて1回）。
        締めた理由は「保留で待ちきった」（hold_timeout）。目的のお客様に繋がる前（呼んでいる段）の保留なら、
        繋がらなかった理由は取り次いでいただけなかった（Flow.not_reached の1か所。繋がったあと・目的のお客様のいない業務の保留は、繋がらなかった
        理由を持たない）。待った長さは通話の事実 hold_timeout（waited_s＝待った秒。根拠の発話＝保留を頼まれた発話）に残す（CL-1.md 4-42）。
        保留でなければ None（何もしない）。"""
        if not self.holding or self.phase == "closing":
            return None
        self._note("hold_timeout", waited_s=self.hold_wait)
        reply = self._close("hold_timeout", prefix="保留のまま、待つ長さが過ぎました。", text=(HOLD_TIMEOUT, "hold_timeout"))
        # 電話接続側からの知らせ（AIの操作ではない）も、操作の記録に残す（on_signal と同じ形。流した締めの録音を、ほかの返事と同じく扱う）
        return self._log_signal({"hold_timeout": True}, reply)

    def _final_check(self, qid: str, toward: str | None = None) -> dict:
        """迷っているお客様に、いまの言い直しについて尋ねられて答えたあと、お客様が相づちだけを返した（_resume の1か所）：同じ言い直しへは戻らず（話は移って
        いないので「話を戻させていただきますね。」も付けない）、最終の確かめで、その質問を締めにいく。こちらがすることの言葉のある質問は「はい、」＋「それでは
        パンフレットをお送りさせていただきましょうか？」（AFTER の final_do）、ほかは言い直しの答えを最終的に確かめる言い方（settle＝「それでは…ということで
        よろしいでしょうか？」）。最終の確かめなので、同じ確かめはもう言い直さない（印 Item.final）：そろわなければ _confirm_again が締める
        （決められないのがお客様の最終の決定なら保留＝asks_wish）。
        ✅ 2026-11-06 日本時間11時19分55秒 ユーザー：「「はい、それではパンフレットをお送りさせていただきましょうか？」という方向性ですよね。話はパンフレットを
        から移っていないので戻す必要はなく、あいてが迷っている最中の最終確認をして、パンフレットのシーンを締めないといけないんじゃないですか？」
        toward＝まだ答えていない質問について尋ねられて答えたあと（_continue の final）：こちらが勧めた向き（final_toward）で同じ最終の確かめ（✅ 2026-11-06
        日本時間11時55分13秒 ユーザー「(2) イ」。お断り・決められないは、言い直しの返事待ちと同じく、その答え・保留で受ける）。"""
        it, q = self.items[qid], self.questions[qid]
        if toward is not None:
            it.condition, it.value = toward, None
        # ⛔ 2026-11-06 日本時間12時台に見つけた：はじめは確かめの回数（corrections）を使い切らせて印にしていた＝最終の確かめへの、はっきり違う答え
        #    （「いえ、やっぱりやめておきます。」）を受け取る道も「訂正のあとも合わない」へ落ち、お断りを「-」にしていた
        it.final = True
        return self._ask_confirm(qid, after="final_do" if Flow._hedged_after(q, it.condition) == "hedged_do" else "settle")

    def _how_now(self, qid: str) -> dict:
        """言い直し（「はい、それではパンフレットをお送りさせていただきますね？」）へのお返事を、判定が答えを選んでいない（まだ決めていない＝「うーん、どうでしょう
        ね。」）と読んだときの2回目（唯一の場所＝_check_again の final・undecided）：同じ言い直しをくり返さず、「はい、」（AFTER の how）＋「いかがなさいますか？」
        （共通の一言の録音 how_now）で伺い、お返事を待つ。判定に見せる文は言い直した中身（awaiting_say）のまま＝判定は何のお返事かを分かって読む（選べば
        その答え・まだ決められなければ _confirm_again が保留を伺う）。✅ 2026-11-09 日本時間3時06分51秒 ユーザー：「同じ質問を繰り返しているし、悩んでいるのに、
        この返答はおかしいですよね。「いかがなさいますか？」のような返答が妥当じゃないですか？」"""
        self.items[qid].set("checking", "言い直しへのお返事がまだ決めていない：「いかがなさいますか？」で伺う（言い直した中身は同じ）")
        self.awaiting_confirm, self.confirm_requested_at = qid, self.callee_turns
        return self._fixed_ask(qid, [{"kind": "recorded", "id": "how_now", "text": HOW_NOW}], "how",
                               f"まだ決めていないお返事です（意味の判定）。いまの言い直し「{self.awaiting_say}」について、どうなさるかを伺いました。")

    def _fixed_ask(self, qid: str, line: list[dict], after: str, asked: str) -> dict:
        """言い直しを作らずに、決まった一言 line（録音1つ）で伺ってお返事を待つ返事（唯一の作り方＝_how_now・_ask_pending）：録音で流せる通話は録音、流せなければ
        会話AIに同じ文を言わせる。お返事の報告の仕方は言い直しと同じ（_confirm_how）。after＝相づちを選ぶ AFTER の鍵。"""
        text = "".join(p["text"] for p in line)
        reply = {"result": "confirm_required", "after": after,
                 "message": f"{asked}相手の返事を待ってください。" + self._confirm_how(qid)}
        if self._plays_recorded(line):
            return reply | {"play": line, "message": played_note(text) + reply["message"]}
        return reply | {"say": text, "message": say_note(text) + reply["message"]}

    def _pending_due(self, qid: str) -> bool:
        """決められないのがお客様の最終の決定で、その質問を保留にしてよいかを伺うか（唯一の決まり＝受け取る所 _take_system）：お客様のご希望・申し出を伺う質問
        （asks_wish）を「わからない」で受け取り、理由が担当ではない（その方の決定ではない）でなく、業務の決まりで分かる方・分かる時を続けて伺う（聞き足しの
        質問 follow_ups がこの質問を指す）のでもなく、まだ伺っていない。⛔ 2026-11-06 日本時間13時台の見直しで見つけた：理由と聞き足しを見ずに、「私の担当では
        ないので。」にも保留の一言を言っていた（この決まりは前は答えのあとの一言の1か所 _tell_due にあった）"""
        q, it = self.questions[qid], self.items[qid]
        return (it.state == "dont_know" and it.reason != "not_in_charge" and qid not in self.follow_ups.values() and not it.told
                and not it.pending_asked and asks_wish(q))

    def _ask_pending(self, qid: str) -> dict:
        """保留を言い渡さず伺う唯一の場所（_pending_due が当たった受け取り＝_take_system）：「承知しました。」（AFTER の pending）＋「それでは（聞くこと）は保留と
        いうことでよろしいでしょうか？」（pending_line・依頼主の単位の録音 pending_recording_id）で伺い、次の質問へ進まずにお返事を待つ。答えは「わからない
        （まだ決まっていない）」のまま（お返事の前に切れても保留）。判定に見せる文は、この伺いの文（awaiting_say）。お返事は _pending_reply の1か所。
        ✅ 2026-11-09 日本時間3時46分53秒 ユーザー：「お客様が決められないと言っているのは、悩んでいるのか、今決められないだけなのか、判断を拒んだわけではない
        ので、「それではこの件は保留ということでよろしいでしょうか？」のように聞いてあげた方がいいんじゃないですか？この状況でこちらに決めつけられるのは心外じゃ
        ないでしょうか？」（保留の一言を流す場面すべて＝最終の確かめのあと・選ぶ助けのあと・目安を伺ったあと・「いかがなさいますか？」のあと・最初の答えの
        「わからない」）"""
        q, it = self.questions[qid], self.items[qid]
        it.pending_asked = True
        text = pending_line(q)
        self.awaiting_confirm, self.confirm_requested_at, self.awaiting_say = qid, self.callee_turns, text
        self.pending_wait = qid
        return self._fixed_ask(qid, [line_play(pending_recording_id(q), text)], "pending",
                               f"まだ決められないというお返事です。{qid} を保留にしてよいかを伺いました。")

    def _pending_reply(self, qid: str, result: str | None, correction: str | None) -> dict:
        """保留を伺ったあとのお返事（唯一の場所＝_op_confirm_field の頭）：
        - 判定が「合っている」（保留でよい）と読んだ ⇒ 保留（わからない・まだ決まっていない）のまま次へ
        - 判定が元の質問の答え（「いや、やっぱり送ってください。」＝選択肢・値を言った）を読んだ ⇒ その答えを、ふつうの答えと同じ1か所（_op_record_answer）で受け取る
        - それ以外（はっきりしない・まだ決められない・答えたくない など）⇒ 同じ伺いをくり返さず、保留のまま次へ
        会話AIは判定に加えない（_judged_answer の1か所。判定を使わない通話＝会話AIの通話の試験だけ、会話AIの報告で同じ3つに分ける）。"""
        q, it = self.questions[qid], self.items[qid]
        self.pending_wait = self.awaiting_confirm = None
        judged = self._judged_answer()
        if judged is None and not self.judging:
            judged = "affirmed" if result == "affirmed" else ("corrected" if result == "corrected" else None)
            if judged == "corrected" and q.type in CHOICE_TYPES:
                judged = q.condition_of(correction)
        choice = q.type in CHOICE_TYPES
        if (choice and judged in q.conditions) or (not choice and judged == "corrected" and correction and q.conditions):
            it.condition, it.reason = None, None
            it.set("empty", f"保留を伺ったあと、お客様が答えを選んだ（「{customer_said(self.callee_said)}」）")
            args = {"question_id": qid, "condition_id": judged if choice else q.conditions[0]}
            return self._op_record_answer(args if choice else args | {"value": correction})
        why = "保留でよいと言われた" if judged == "affirmed" else f"はっきりしないお返事（「{customer_said(self.callee_said)}」）＝伺い直さず保留のまま"
        it.set("dont_know", f"保留を伺ったあと：{why}")
        if self.report_before is not None:
            # 保留のお返事は、この報告で受け取った答え（_received_now の1か所）＝相づちは「わからない」を受け取ったときと同じ「承知しました。」（続けば
            # 「かしこまりました。」）。状態は伺う前から「わからない」なので、報告の前の状態を「お返事待ち」として読ませる
            self.report_before = self.report_before | {qid: "pending"}
        return self._advance("受け付けました（保留）。")

    def _confirm_again(self, qid: str, why: str, reported: str | None, back: str | None) -> dict:
        """言い直しへの返事の確かめがそろわない：もう1回だけ言い直して確かめる（訂正のあとの復唱と同じ回数＝after_correction。選択肢の型は、
        判定が選択肢を選んでいればその意味で）。使い切った・必須でない・言い直す選択肢が無い ⇒「-」（必須なら締めの前の念のための確認で1回）。"""
        it, q = self.items[qid], self.questions[qid]
        self.awaiting_confirm = None
        if it.tentative == "noted":
            # 承っておく一言（問いではない）へのお返事は、確かめがそろわなくても言い直さない（_tentative の1か所。直した値は confirm_field の訂正で受ける）
            return self._tentative(qid)
        if it.offer_again:
            # 申し出の伺い直し（_offer_again）へのお返事もはっきりしない：もう伺い直さず、保留も伺わずに「承知しました。」で次へ・締め（伺い直しは1回）。
            # ✅ 2026-11-09 日本時間7時57分50秒のご判断を入れる帳面の伺い「また繋がらない・はっきりしない ⇒ 伺い直さず今の道」
            it.set("unresolved", f"{why}・申し出の伺い直しへのお返事もはっきりしない（伺い直さない）")
            return self._dash(qid)
        if why.startswith(TALK_WHY) and back is None:   # 会話になっていない返事で、確かめる答えが無ければ聞き直す（_check_again の1か所）
            return self._check_again(qid, why, reported, back)
        undecided_now = asks_wish(q) and undecided([self.heard_check.get(k) for k in JUDGMENTS])
        if undecided_now and self._why_of("unknown") in WHY_ROUTED:
            # 判定の読みがどれも答えを選んでいず、理由の読みがそろって「担当ではない」「いまは分からない」：まだ決めていないのではない＝言い直して確かめずに、
            # その理由の「わからない」として受け取る（最初の答えと同じ＝業務の決まりで分かる方・分かる時を伺う。WHY_ROUTED）。⛔ 2026-11-08 日本時間0時台
            # （11-07 23時26分13秒のご質問の見直しの H-1・H-2）：「上の者に聞かないと分からないです。」にも同じ確かめの一言をもう一度言い、そのあと理由を
            # 「まだ決まっていない」にして保留で締めていた
            return self._take_system(qid, "unknown")
        if q.required and not it.final and it.corrections < self.limits.after_correction and (q.type not in CHOICE_TYPES or q.offers(back)):
            it.corrections += 1
            # まだ決めていない（判定が答えを選んでいず、こちらへの問いでもない）なら、2回目は同じ一言でなく「いかがなさいますか？」（_how_now）。こちらへ尋ねた言葉
            # （asks_us）は、尋ねられて答える1か所か、答えられなければ尋ねられる前の流れのとおり（_asked_skip＝言い直しと同じ一言が応える）
            readings = [self.heard_check.get(k) for k in JUDGMENTS]
            return self._check_again(qid, why, reported, back, final=True, undecided=undecided_now and not asks_us(readings))
        if undecided_now:
            # 最終の確かめのあとも、判定の読みがどれも答えを選んでいない（お客様の最終の決定。迷っているは保留で締めない＝_hesitation・22時02分54秒）：「わからない
            # （まだ決まっていない）」として受け取り、答えのあとに伝える一言の1か所（_tell_due）から「それでは（聞くこと）は保留とさせていただきますね。」でその
            # 質問を締めて次へ（伺い直さない＝10-13「わからない＝「-」で聞き直さない」）
            return self._undecided_final(qid)
        it.set("unresolved", f"{why}（言い直しへの返事「{(self.callee_said or '')[:20]}」・AIは「{label(reported)}」と受け取った）・確かめがそろわない")
        return self._dash(qid)

    def _check_again(self, qid: str, why: str, cond: str | None, back: str | None, final: bool = False, undecided: bool = False) -> dict:
        """確かめがそろわなかった答えを、そのまま受け取らずに言い直して確かめる（選択肢の型は back の意味で＝判定が選択肢を選んでいれば判定のほう）。
        final＝言い直しへの返事がそろわず、もう1回だけ言い直す（2回目の確かめ＝_confirm_again）：
        - 希望・申し出の質問（こちらがすること・承ることを伝える一言＝_hedged_after の hedged_do・hedged_noted）：1回目と同じ一言で受ける（問いを繰り返す
          のではない）。✅ 2026-11-06 日本時間4時11分28秒 ユーザー：「…今回の流れの「送ってもらったほうがいいですかね。」だと言い聞かせの意味合いだと
          思われるので、はい、それではパンフレットをお送りさせていただきますね？で正解です。「はい、それではパンフレットをお送りして大丈夫だということで
          よろしいでしょうか？」でも間違いではありません。」（申し出は「それでは…させていただきますね？」の形＝11-01 20時56分・11-03 のご判断）
        - 問いの形（事実を伺う質問の「…承っておいて大丈夫でしょうか？」など）：同じ問いを続けて言わず、最終的に確かめる言い方（AFTER の settle）。
          ✅ 2026-11-01 ユーザー：「全く同じ質問を2度したら、機械だと思われてしまいませんか？」・2026-10-31 の夜 ユーザー：「『それでは…ということで
          よろしいでしょうか？』は矛盾や言い直しの答えを最終的に確認する場合です。」
        ⛔ 2026-11-06 日本時間3時台に、2回目の言い方を1回目と同じ決め方で選んでいた所を、全部 settle にした＝申し出の質問の決まった形を、ご判断で外した
           形に近い「…大丈夫だということでよろしいでしょうか？」に戻していた（4時11分28秒のお言葉で、申し出は1回目の一言のままにした）"""
        it, q = self.items[qid], self.questions[qid]
        if not restates(q):
            # 言い直さない質問（ほかのご希望 W1・伺い方 C11・ご承認いただけなかったときの N1〜N3）は、確かめがそろわなくても言い直さない（どの段でも＝CL-1 の
            # 11-03 のご判断の (4)。その意味の言葉は判定に見せる説明で、お客様に言う言葉ではない）＝答えが質問に合わないときと同じく、質問から聞き直す（6-11 の
            # 回数の中で。使い切れば締めの前の念のための確認でもう1回）。聞き取れてはいるので、お電話のせいにする聞き返しにせず、声が聞き取れない数にも入れない
            # （"form"＝「はい、」＋「恐れ入ります。（聞くこと）をもう一度…」。2026-10-17 の G-14「お客様ははっきり話しているので、電話のせいにする聞き返しは
            # 合わない」）。⛔ 2026-11-05 日本時間2時45分39秒のご指示で直す途中、流れの制御に通して見つけた：
            # 伺い方 C11 を必須にした 21時04分から、判定が食い違うと「念のためメールでLINEに招待してほしいということでよろしいでしょうか？」と言っていた
            if (h := self.answer_held) is not None and h.get("ahead") and h["qid"] == qid:
                # 先に言われた答え（質問はまだ流していない＝_offer_ahead）：その言葉に答えが無かったときと同じく、受け取らずに質問を流す（_resume の決まり）。
                # ⛔ 同じ流しで見つけた：まだ伺っていない質問に「お電話が少し遠いようでしてもう一度お聞かせ…」と聞き返すことになった
                it.condition = it.value = None
                it.set("empty", f"{why}・先に言われた答えなので受け取らず、質問を流す")
                self.answer_held = None
                return self._continue([], "")
            # 判定がいちばん近いと読んだ選択肢（back＝宣言した選択肢）があれば、聞き直さずに、その選択肢で意味合いを尋ねる（1つの答えに1回。候補の言葉が
            # 作れなければ聞き直す）。✅ 2026-11-05 日本時間23時19分06秒 ユーザー（会話1）・23時56分48秒「ア」（お言葉は phrases.SENSE_ASK）
            # 候補は判定自身が選んだ選択肢だけ（判定が決められない・待つと読んだときの back は会話AIの報告＝判定の読みではない。⛔ 入れる途中、流れの制御に
            # 通して見つけた：決められないと読んだ「それで結構です。」に、会話AIの「無い」で尋ねていた）
            judged = {str(v) for k in JUDGMENTS if (v := self.heard_check.get(k)) is not None}
            if back in judged and not it.ambiguous_asked and asks_sense(q, back):
                it.set("heard", f"{why}（「{(self.heard_said or '')[:20]}」）・判定のいちばん近い読み「{label(back)}」で意味合いを尋ねる")
                it.condition, it.ambiguous_asked = back, True
                return self._ask_confirm(qid, after="sense")
            return self._ask_again(qid, "form", why)
        # 会話になっていない返事（TALK_WHY）は、いつも確かめる答え（前の話と食い違う選択肢）があり、聞き違い・言い違いも確かめへの返事で分かる
        # （✅ 2026-10-30 ユーザー：「本当にお客様が『差し控えさせていただきます』なら会話になっていない。文字起こしが間違っている可能性も考えるべき」。判定に会話になっているかを答えさせていた
        # ころ＝meaning_judge の FITS は、答えの無い読みを質問から聞き直していた。✅ 2026-10-31 の夜 ユーザー「(a) OK」で FITS を外し、その枝も無くなった）
        it.set("heard", f"{why}（「{(self.heard_said or '')[:20]}」）・AIは「{label(cond)}」と受け取った")
        if q.type in CHOICE_TYPES and back is not None:
            it.condition = back
        it.ambiguous_asked = True
        # 言い方は、確かめるきっかけ（言い切っていない答えの決まり・判定との食い違い）ではなく、お客様の言葉が言い切っているかで決める
        # （⛔ 2026-10-18 に見つけた漏れ：AIが「わからない」と報告し、判定が「していない」と読んだ「たぶんしてないと思います」は、
        #    食い違いがきっかけなので、言い切っていない答えの言い方にならなかった）
        # 会話になっていない返事に意味の答えがあるとき・前の話の答えで補うときは「それでは…ということでよろしいでしょうか？」（矛盾や言い直しの答えを
        # 最終的に確かめる＝✅ 2026-10-31 の夜 ユーザー。その前の「(イ)」の「念のため…でよろしかったでしょうか？」は、確かめるまでもない復唱の言い方）
        if final and (told := Flow._hedged_after(q, it.condition)) in ("hedged_do", "hedged_noted"):
            # 2回目の確かめ：希望・申し出は、こちらがすること・承ることを伝える同じ一言で受ける。お客様がまだ決めていない（判定が答えを選んでいない＝undecided）
            # なら、同じ一言をくり返さず「いかがなさいますか？」（_how_now。✅ 2026-11-09 日本時間3時06分51秒 ユーザー）
            return self._how_now(qid) if undecided else self._ask_confirm(qid, after=told)
        if final or why.startswith((TALK_WHY, PRIOR_WHY)):   # 2回目の確かめ（問いの形）・矛盾した答え・前の話で補う答えを最終的に確かめる（AFTER の settle）
            return self._ask_confirm(qid, after="settle")
        if why.startswith(END_DOUBT_WHY) and q.type in CHOICE_TYPES and q.doubt_words.get(it.condition):
            return self._ask_confirm(qid, after="end_doubt")   # いまの話の向きを伺う（取込の言葉が無ければ、曖昧な答えと同じ確かめ）
        if self._terms_now(qid) and it.condition == q.offered:
            # 申し出を、こちらで応えられる条件つきで受けていただいた（_terms_reported）：言い切っていなくても伺い直さずに受け取り、言い切りの一言で次へ
            # （_taken_on_terms）。✅ 2026-11-12 日本時間13時40分05秒 ユーザー「(2)…伺い直さない」
            it.set("accepted", f"条件つきで申し出を受けていただいた（「{{customer_said(self.heard_said) or label(it.condition)}}」）：伺い直さずに受け取る")
            return self._advance("受け付けました。")
        hedged = why.startswith(HEDGE_WHY) or Flow._hedged(self.heard_said, self.heard_check)
        fq = self._hedged_follow_up(qid, it.condition) if hedged else None
        if hedged and (fq is not None or (q.type in CHOICE_TYPES and q.tell_words.get(it.condition))):
            # 言い切っていない答えのあとに伺う質問（Question.ask_if_hedged＝業務設定）がある：言い直さずに「曖昧」の印を付けて受け取り、その質問で伺う
            # （_advance → _follow_ups が前の質問のすぐあとに入れる。✅ 2026-11-03 ユーザー (3)(4)「OK」）
            it.hedged = customer_said(self.heard_said) or label(it.condition)
            it.set("accepted", f"言い切っていない答え（「{it.hedged}」）：言い直さずに受け取り、"
                   + (f"{fq.id}「{fq.text[:24]}」で伺う" if fq is not None else f"業務の一言「{q.tell_words[it.condition][:24]}」を伝える"))
            # 相づちは「はい、」（お客様の言葉を受けて「それでは…」と伺う。⛔ 2026-11-03 の見直しで見つけた：順番の「承知しました。」「ありがとうございます。」を
            # 流していた＝「たぶん行けなくなりそうです」に「ありがとうございます。」。ユーザーの言い方はどれも「はい、それでは…」）
            # （_hedged_next の1か所で決まる＝受け取った言い切っていない答えのあとに伺う質問）
            return self._advance("受け付けました。")
        return self._ask_confirm(qid, after=self._hedged_after(q, it.condition) if hedged else "ambiguous")

    def _hedged_follow_up(self, qid: str, cond: str | None) -> "Question | None":
        """言い切っていない答え（qid の cond）のあとに伺う質問（業務設定の前の答えで伺う質問＝Question.ask_if・まだ伺っていないもの。言い切っていない
        ときだけ伺う質問も、いつも伺う質問も）。唯一の場所（受け取る所と、並びに入れる _ask_if_follow_ups が同じ決まりを使う）。その答えのあとに業務の
        次の段があれば、言い直して確かめずにその段で伺う（✅ 2026-11-03 ユーザー：「車で来ることの確認だけが目的なら、間違いじゃないですが、通常は、
        車で来るなら駐車場の案内や指定などが目的になるんじゃないですか？」＝確かめの一言より、その答えで業務が次にすること）。"""
        return next((x for x in self._business_next(qid, cond, hedged=True) if x.id not in self.order and self.items[x.id].state == "empty"), None)

    def _business_next(self, qid: str, cond: str | None = None, hedged: bool | None = None) -> list["Question"]:
        """業務設定の前の答えで伺う質問（Question.ask_if）のうち、質問 qid の答え（cond・言い切っていない答えか hedged。省けば、いま受け取っている答え）に
        当たるもの（トークスクリプトの順）。「業務の次の段がこの答えに当たるか」の唯一の決まり＝並びに入れる _ask_if_follow_ups・言い切っていない答えの
        あとに伺う _hedged_follow_up・ほかのご希望より業務の言葉が先 _wish_due・伺い終えるまで締めない _goals_lost が、ここだけで決める（⛔ 見直しで
        見つけた：4か所に少しずつ違う写しがあった）。言い切っていないときだけ伺う質問（ask_if_hedged）は、言い切っていない答えにだけ当たる。"""
        it = self.items[qid]
        cond = it.condition if cond is None else cond
        hedged = bool(it.hedged) if hedged is None else hedged
        return business_next(self.questions.values(), qid, cond, hedged)

    @staticmethod
    def _hedged_after(q: "Question", cond: str | None) -> str:
        """言い切っていないが向きの決まった最初の答えの確かめ方（AFTER の鍵。唯一の場所）：事実を伺う質問なら hedged_fact（意味の言葉・値＋依頼者がすること）、
        そうでない質問（希望・申し出）は hedged_do（選択肢の型は、選んだ選択肢のこちらがすることの言葉＝Question.do_words が要る・値を伺う型＝日時・数など
        は「（値）とさせていただきます」）。選択肢の型で、こちらがすることの言葉が無い進める向きの答えと「どちらでも」は hedged_noted（意味の言葉で
        「それでは…で承りますね？」）。目的から遠ざかる向きの選択肢（Question.away_answers）・「どれも要らない」・名前の読みの確かめ・システムの答え・
        自由な答えは hedged（「念のため…でよろしかったでしょうか？」の形。ただし遠ざかる向きと「どれも要らない」は、_ask_confirm の頭の _keep_toward_goal が
        言い直さずに受け取る＝その形では言わない）。
        ✅ 2026-11-02 日本時間1時22分 ユーザー：「お支払いや日時なんかの希望を伺う選択にも、「それでは…させていただきますね？」の形でいいと言ったよね？」
        （10-31 日本時間6時05分の「違うのは、事実を伺う質問だけです」）"""
        if cond == ANY:
            # 選ぶのをこちらに任せた答え：任せたことを承る（「それでは、どちらでも構わないということで承りますね？」。✅ 2026-11-03 ユーザー：「「どちらでも」の
            # 答えでも、「念のため…でよろしかったでしょうか？」に戻るのはおかしいでしょ！」・11-02 のご判断「任せたことそのものを記録する。こちらがどれを
            # 選ぶかは、お客様に確かめることではない」）。どれも要らない（NONE）は、目的から遠ざかる向きと同じく言い直さない（_keep_toward_goal）
            return "hedged_noted"
        if q.reading_of or cond in SYSTEM_CONDITIONS or cond == NONE:
            return "hedged"
        if q.type in CHOICE_TYPES:
            # 目的から遠ざかる向きの選択肢（Question.away_answers）は「させていただきますね？」を使わない（✅ 2026-11-03 ユーザー (2)「OK」。目的につなげて
            # 伺うのは業務設定の追って伺う質問＝この前に _hedged_follow_up が選ぶ。無ければ言い直さずに受け取る＝_ask_confirm の頭の _keep_toward_goal）
            # 遠ざかる向きは、事実を伺う質問でも同じ（⛔ 見直しで見つけた：事実を伺う質問の形を先に選び、言い直さずに受け取る所が言い切っていない印を
            # 付けなかった＝依頼者へお客様の言葉が渡らず、最終目的ならほかのご希望を伺わずに締めた）
            if cond in q.away_answers:
                return "hedged"
            if q.asks_fact:
                return "hedged_fact"
            # 進める向きで、こちらがすることの言葉が決まらなかった：意味の言葉で承る（✅ 11-03 ユーザー：「…「念のため…でよろしかったでしょうか？」に戻りますは
            # おかしい。」）
            return "hedged_do" if q.do_words.get(cond) else "hedged_noted"
        if q.type in CONFIRM_TYPES:
            return "hedged_fact" if q.asks_fact else "hedged_do"
        return "hedged"

    def _tentative(self, qid: str, undecided: bool = False) -> dict:
        """値を伺う質問（日時・数・金額）で、お客様の答えが確定でないとき（値に「あたり」などが付いた＝approx_value・値の言い直しへの「まだ決まっていない」＝
        _hesitation の tentative＝undecided）に受ける唯一の場所。確定の言い直し（「それでは15日とさせていただきますね？」）も、同じ言い直しのくり返しもしない：
        - 言い直し（ひとまずの形も）へ「まだ決まっていない」と返された（undecided）：値を言わずに「まだ確定ではない」と承っておく（AFTER の noted_undecided＝
          「承知しました。それではまだ確定ではないと承っておきますね。」・期限があれば変更の受付の一文を添える。✅ 2026-11-08 日本時間5時33分05秒 ユーザー：(2)・(4)
          「イ」。⛔ 5時00分52秒のお言葉の「ひとまず15日の予定でお考えだと…」を置き換えた）。依頼者へ渡すお客様の言葉には、値を言った言葉（あれば）に続けて
          「まだ決まっていない」の言葉を残す（値だけでは、まだ決まっていないことが依頼者に伝わらない）
        - 確定でない値の答えで、業務設定に変更の受付の期限（Question.change_by）があって、まだ伺っていない：ひとまずその値にして、変更の受付を添えて伺う（AFTER の
          tentative_do・tentative_fact＝「はい、それではひとまず15日あたりとさせていただきますね？変更があった場合には前日までにご連絡いただけましたらご対応可能
          です。」）。値はお客様の言い方のまま（「あたり」を付けたらこちらも付ける）。確定した値で登録する質問（Question.definite_value）だけ値の芯（「15日」）。
          ✅ 2026-11-08 日本時間5時33分05秒 ユーザー：(1)「…相手が「あたり」をつけたら、こちらも「あたり」をつけた方がいいでしょう。ただし、確定日付の登録が必要な
          業務の場合は、現状のままで大丈夫です。」。確定した値で登録する質問は、期限が無くても、変更の受付の一文を付けずにひとまずの形（「はい、それではひとまず
          15日とさせていただきますね？」。✅ 6時04分57秒「イでOK」）。変更の受付の一文は、同じ期限をこの電話でもうお伝えしていれば短い形（_change_line）
        - 確定でない値の答えで、期限が無く、確定した値で登録する質問でもない：お客様の言い方の値のまま承っておく（AFTER の noted＝「承知しました。それでは15日あたりでお考えということで
          承っておきますね。」。問いではない）
        - 承っておく一言を言ったあと：お客様のお返事が直した値でなければ（はい・まだ決まっていない・答えなかった）、その値で受け取る（もう伺わない）
        受け取る値には、言い切っていない印とお客様の言葉を添える（Item.hedged＝依頼者へ・業務の次の段＝_hedged_follow_up と同じ「言い切っていない答え」）。
        ✅ 2026-11-08 日本時間3時48分03秒 ユーザー：「お客様「15日あたりですかね。」のように「あたり」などの日にち確定でない言葉が入っている場合には、こちら
        「はい、それでは15日とさせていただきますね？」のように確定で確認するのはあいての意思に反します。⇒「承知しました。それでは15日あたりでお考えということで
        承っておきますね。」にするか、「はい、それではひとまず15日とさせていただきますね？変更があった場合には（～までにご連絡いただけましたら）（～までであれば）
        ご対応可能です。」～は日付や曜日、前日など。」（2つのどちらにするかは、業務に変更の受付の期限があるか＝「ひとまず」の形は期限が要る。値の言い直しへの
        「まだ決まっていない」は、同じ理由＝確定で確かめ直すのはお客様の意思に反する、で同じ1か所に通す）"""
        it, q = self.items[qid], self.questions[qid]
        if it.tentative == "noted":
            self.awaiting_confirm = None
            it.set("confirmed", f"承っておく一言のあとのお返事（「{customer_said(self.callee_said)}」）：その値で受け取る（言い切っていない：「{it.hedged}」）")
            return self._advance("確認できました。")
        ap = approx_value(it.value, self.heard_said)
        if ap is not None:
            it.approx = ap[1]
        words = customer_said(self.heard_said)
        # お客様の言葉：値を言った言葉（確定でない値の答え）か、まだ決まっていないと言った言葉。両方あれば両方（同じ言葉は重ねない）
        it.hedged = "／".join(dict.fromkeys(w for w in (it.hedged, words if undecided or not it.hedged else None) if w)) or None
        if (q.change_by or q.definite_value) and it.tentative is None and not undecided:
            # ひとまずの形：期限のある質問と、確定した値で登録する質問（期限が無ければ変更の受付の一文は付けない＝_change_line。✅ 2026-11-08 日本時間
            # 6時04分57秒 ユーザー「イでOK」＝承っておく形では、依頼者へ登録できない値「15日あたり」が渡った）
            it.tentative = "set"
            if ap is not None:
                it.value = ap[0] if q.definite_value else ap[1]
            return self._ask_confirm(qid, after="tentative_fact" if q.asks_fact else "tentative_do")
        it.tentative = "noted"
        if not undecided:
            # 承っておく形はお客様の言い方の値で承る。まだ決まっていないと承る一言は値を言わない＝値は前の答えの値のまま（確定した値で登録する質問の
            # 芯の値をお客様の言い方の値に替えない。⛔ 11-08 5時台に流れの制御から例を出して見つけた：同じ1行で「15日」を「15日あたり」に替えていた）
            it.value = it.approx or it.value
        return self._ask_confirm(qid, after="noted_undecided" if undecided else "noted")

    def _undecided_final(self, qid: str) -> dict:
        """お客様の最終の決定が決められない（最終の確かめのあとも決められない＝_confirm_again。迷っているは保留で締めない＝_hesitation・22時02分54秒）：
        「わからない（まだ決まっていない）」として受け取る唯一の場所（保留の一言は _tell_due）。理由は、プログラムが決めた事実（まだ決まっていない）で、
        判定が読む「わからない・答えたくない理由」（_why_of）では上書きしない。✅ 2026-11-06 日本時間11時19分55秒 ユーザー「(2) 「ちょっと今は決められない
        ですね。」というのが最終のお客様の決定であれば、「承知しました。それではパンフレットは保留とさせていただきますね。」とパンフレットは締めて、次へ」。
        ⛔ 2026-11-07 日本時間18時台に見つけた：判定を使う通話では理由が判定の読み（無ければ「理由を言わなかった」）になり、業務の決まり（理由ごとの次の動き）が
        本番だけ変わりうる形だった"""
        return self._take_system(qid, "unknown", "undecided", settled=True)

    def _take_system(self, qid: str, cond: str, reason: str | None = None, settled: bool = False, opens_point: bool = False) -> dict:
        """相手が「わからない」「答えたくない」と答えた（答えの報告と、言い直しへの返事の、どちらからも来る）。reason＝会話AIが選んだ理由（判定を使う
        通話では使わない＝理由は判定が読む _why_of の1か所）。settled＝理由はプログラムが決めた事実（_undecided_final・_hesitate_now の approval）。
        opens_point＝ご承認の質問への迷い（_hesitate_now の approval）：ご承認いただけなかったときの手順を始める受け取り（_point_step が伺う）なので、業務の
        決まり（結論として締める・分かる方／分かる時を伺う）は当てない（迷っているのは結論ではない）。"""
        it = self.items[qid]
        it.condition = cond
        it.reason = reason if settled else self._why_of(cond, reason)
        kind = None if opens_point else self.questions[qid].handlings.get(("refused" if cond == "refused" else "dont_know", it.reason))
        if kind == "conclude":
            # 業務の決まりで、この答えが結論（「回答不可」など）：ほかの質問は伺わずに、丁寧に締める（CL-2 の 15-2-3 の (d)。結論の名前は CL-2 の判定が決める）
            it.set("refused" if cond == "refused" else "dont_know", "相手の答えが、業務の決まりで結論になった")
            return self._close("done", prefix="受け付けました。")
        role = {"ask_who": "ask_who", "ask_when": "ask_when"}.get(kind)
        if role and role not in self.follow_asked:
            # 分かるお客様・分かる時を、1通話1回だけ伺う（CL-2 の 15-2-3 の (a1)(a2)）。いまの質問のすぐあとに伺い、答えのあと残りの質問へ進む。
            # 伺ったかは印で数える（⛔ 2026-10-24 の見直し：質問の有る無しで数えていたので、分かる方に代わったあと（聞き足しの質問を外した）、
            # その方の「わからない」に、また分かる方を伺った）
            self.follow_asked.add(role)
            fq = callback_question(role)
            self._new_item(fq)
            at = self.order.index(qid) + 1 if qid in self.order else len(self.order)
            self.order = [*self.order[:at], fq.id, *self.order[at:]]
            self.follow_ups[fq.id] = qid
        if cond == "refused":
            it.set("refused", "相手が答えを拒んだ")
            return self._advance("受け付けました（答えたくない）。この質問は二度としません。")
        if opens_point:
            it.set("dont_know", "相手がご承認を迷っている（ご承認いただけなかったときの手順と同じ道で、迷っている点を伺う）")
            return self._advance("受け付けました（迷っている）。")
        it.set("dont_know", "相手が「わからない」と答えた")
        if self._pending_due(qid):
            return self._ask_pending(qid)   # 決められないのが最終の決定：保留を言い渡さず、保留でよいかを伺う（✅ 2026-11-09 日本時間3時46分53秒）
        return self._advance("受け付けました（わからない）。")

    def _why_of(self, cond: str, reason: str | None = None) -> str:
        """「わからない」「答えたくない」の理由（DONT_KNOW_LABEL・REFUSED_LABEL の鍵。唯一の決まり＝_take_system）：答えを当てる言葉（heard_check）への
        判定の読み（項目 WHY＝flowctl.why_choices）が、届いたものがそろって同じ理由で、その答えの理由の表にあれば、その理由。そろわない・届いていない・
        表に無い（わからないの答えに「決まりで言えない」など）・理由を言っていないと読んだなら、受け皿（理由を言わなかった＝業務の決まりで次の動きを
        変えない側。結論として締める・分かる方を伺う、を1つの読みでしない）。判定を使わない通話だけ、会話AIの選んだ理由。
        ✅ 2026-11-06 日本時間15時20分30秒 ユーザー「(3)…お勧め：判定に理由も読ませます。OK」（11-01 16時56分「会話AIは正しい判断ができるの？
        できないから、別のAIを使ったんじゃないのか？」と同じ理由）"""
        table = REFUSED_LABEL if cond == "refused" else DONT_KNOW_LABEL
        if not self.judging:
            return reason if reason in table else list(table)[-1]
        got = [getattr(self.heard_check.get(k), WHY, None) for k in JUDGMENTS]
        got = [g for g in got if g is not None]
        key = {v: k for k, v in table.items()}.get(got[0]) if got and all(g == got[0] for g in got) else None
        return key or list(table)[-1]

    def _op_report_unclear(self, a: dict) -> dict:
        qid = a.get("question_id")
        # 判定が、答えではない（待つ・かけ直す・考えている途中・代わった方）と読んでいれば、聞き返さずに答えの報告と同じ1か所で応じる（会話AIの報告は判定を
        # 待つきっかけだけ）。⛔ 2026-11-06 日本時間13時台の見直し（待つ持ち場の指摘8）：ここだけ読みを見ずに「申し訳ございません、お電話が少し遠いようで…」と
        # 聞き返した（「うーん、どうしようかな…。」「少し待ってください。」にも）
        if (held := self._not_an_answer()) is not None:
            return held
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
            return self._not_current(qid)
        q, it = self.questions[qid], self.items[qid]
        return self._ask_again(qid, "unclear", "聞き取れない・曖昧")

    def _op_confirm_field(self, a: dict) -> dict:
        qid = a.get("question_id")
        if self.awaiting_confirm is None and qid == self.current:
            # ⛔ 2026-10-14 G-34（2回）：AIが答えを報告せずに自分で言い直し、相手の返事を confirm_field で報告してきた。前の差し戻しは
            #    「相手が答えたら record_answer で」としか言わず、相手がもう答えているのに AIが待つと読めた。1回は報告し直さず、次の質問の
            #    答え「3人」が日時の答えとして記録され、確かめまで済んだ。⇒ 答えがまだ報告されていないことと、報告する値を、はっきり言う
            v = normalize_value(self.questions[qid].type, a.get("correction"), self.questions[qid].unit)
            heard = a.get("result") == "corrected" and self._heard_unreported()
            self.unreported_correction = (qid, correction_kind(self.callee_said)) if heard else None
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
        if self.after_answer is not None:
            # 言い直しは取っておいた返事の中（答えに添えられた質問に先に答える＝_question_first）：まだお客様に言っていないので、言い直しへの返事は無い
            return self._reject("言い直しは、まだ相手に言っていません（先に相手の質問に答え、そのあとプログラムが言い直します）。")
        if self.callee_turns <= (self.confirm_requested_at or 0):  # I-24：相手の返事が届く前の報告は差し戻す
            return self._reject("相手の返事をまだ聞いていません。言い直したら、相手が返事をするまで待ってから報告してください。")
        if (held := self._not_an_answer()):
            return held
        it = self.items[qid]
        result = a.get("result")
        correction = normalize_value(self.questions[qid].type, a.get("correction"), self.questions[qid].unit)
        if self.pending_wait == qid:
            return self._pending_reply(qid, result, correction)   # 保留にしてよいかを伺ったあとのお返事（言い直しへの返事の決まりは当てない）
        if (back := self._terms_reported(qid, it.condition if result == "affirmed" else None, a)) is not None:
            return back   # 申し出の言い直し・伺い直しへのお返事に添えた条件の言葉も、答えの報告と同じ入口・同じ確かめ（✅ 2026-11-12 まとめ役の (H1)）
        q = self.questions[qid]
        if not q.reading_of and (judged := self._judged_answer()) is not None:
            # 会話AIは判定に加えない（_judged_answer の1か所）：言い直しへの返事も、判定の組が確かに読んだ答えで決める（会話AIの「合っている」「違う値を言われた」・
            # 選んだ選択肢は使わない。直した値の文字だけは会話AIが渡す）。名前の読みは、相手の言葉でプログラムが決める（_reading_denied・G-45）ので当てない
            got = self._judged_reply(qid, judged, result, correction)
            if isinstance(got, dict):
                return got
            if got is not None:
                result, correction = got
        # 可能性の確かめ（_ask_confirm の maybe）へのお返事をいただいた：認めた＝その可能性がある答え（言い切っていない印のお客様の言葉のまま）・ほかの答え（「いや、
        # 電車で行きます。」）＝その質問への新しい答え（ふつうの答えを受け取る1か所 _op_record_answer＝言い切っていれば受け取って次へ・言い切っていなければ今までの
        # 確かめ。確かめたのはこちらの聞き取りではないので「失礼いたしました。改めまして…」と言い直さない）
        maybe_reply = it.maybe_asked == "pending" and result in ("affirmed", "corrected")
        if maybe_reply:
            it.maybe_asked = "done"
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
                return self._dash(qid)
            if correction != heard:
                it.set(it.state, f"AIの聞いた読み「{heard or '（無し）'}」は使わず、候補の「{correction}」で確かめる（濁りの違いは聞き分けられない）")
        if q.type in CHOICE_TYPES and result == "corrected" and correction:
            # 選択肢の質問の訂正は、選択肢の名前で受ける。⛔ 2026-10-17 に見つけた穴：訂正の言葉（「まだです」）を値に入れるだけで、選択肢は
            #    前のまま（「はい」）だった＝相手が打ち消したのに、依頼主へ「はい」が渡る形だった（依頼主へ渡すのは選択肢＝export）
            chosen = q.condition_of(correction)
            if chosen is None:
                return self._reject(f"{qid} は選択肢の質問です。{ARG_LABEL['correction']}には、相手の答えに合う選択肢の名前"
                                    f"（{'、'.join(q.labels)}）を入れて、もう一度報告してください。")
            if not restates(q) or maybe_reply:
                if maybe_reply:
                    it.hedged = None   # 「どっちでもいい」の印は外す（新しい答えが言い切っていなければ、その言葉で付け直す）
                # 言い直さない質問（意味合いを尋ねた答え＝_check_again の sense）：直していただいた答えは、言い直して確かめずに、ふつうの答えを受け取る1か所
                # （_op_record_answer）で受け取る（判定とそろえば受け取る・そろわなければ質問から聞き直す）。その質問の意味の言葉は判定に見せる説明で、お客様に
                # 言う言葉ではない（⛔ 入れる途中、流れの制御に通して見つけた：「改めましてほかに希望は無い・やはり要らないということで大丈夫でしょうか？」と
                # 言い直していた）。✅ 2026-11-05 日本時間23時19分06秒 ユーザー：「そうすると、お客様が「いえ、～という意味です。」のように返してくれるのが一般的」
                self.awaiting_confirm = None
                return self._op_record_answer({"question_id": qid, "condition_id": chosen})
            if chosen in SYSTEM_CONDITIONS:
                why, back = self._checks(q, chosen, policy=False, confirming=True)
                if why:
                    return self._confirm_again(qid, why, chosen, back)
                self.awaiting_confirm = None
                return self._take_system(qid, chosen, a.get("unknown_why"))
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
            correction, ask = fill_ampm(correction, self.business_hours, self.designated_limit)
            if ask and result == "corrected" and not it.ampm_asked:
                self.awaiting_confirm = None
                return self._ask_ampm(qid, correction)
        self.awaiting_confirm = None
        if it.tentative == "noted" and result != "corrected":
            # 承っておく一言（問いではない）へのお返事：直した値でなければ（はい・答えなかった）、その値で受け取る（_tentative の1か所）
            return self._tentative(qid)
        # 選択肢の型は、言い直すのが選択肢の意味の言葉なので、添えられた値で別の値を言い直す形（G-36）には当てない。名前の読みも当てない
        # （認めたか直されたかは相手の言葉でプログラムが決め＝_reading_denied、次に確かめる読みは next_reading の1か所が決める＝G-45：AIは濁りの
        # 違いを聞き分けられない。⛔ 2026-11-04 に見つけた穴：AIが「合っている」に添えた読みで確かめ直していた＝次の読みを決める所が2つ）
        if (result == "affirmed" and q.type not in CHOICE_TYPES and not q.reading_of
                and (other := other_value(q.type, it.value, correction))):
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
            return self._dash(qid)
        if result == "affirmed" and not q.reading_of:
            # 言い直しへの返事も、最初の答えと同じ確かめに当てる（DESIGN.md 1章）。名前の読みは、相手の言葉でプログラムが決める（_reading_denied）
            reported = it.condition if q.type in CHOICE_TYPES else "affirmed"
            why, back = self._checks(q, reported, policy=False, confirming=True)
            if why:
                return self._confirm_again(qid, why, reported, back)
        if result == "affirmed":
            if not q.reading_of and Flow._hedged(self.callee_said, self.check):
                it.hedged = customer_said(self.callee_said)
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
            if it.tentative:
                # 確定でない値を受ける形（_tentative）のあとに値を直された：直した値を新しい答えとして確かめる（確定でなければ、また受ける形を選ぶ＝_ask_confirm の頭）
                it.tentative = it.approx = it.hedged = None
            kind = correction_kind(self.callee_said)
            if kind == "misheard" and it.corrections >= 2:
                kind = "misheard_again"  # 同じ項目を2度聞き違えた＝こちらの誤りが重なった ⇒ 重いお詫び（✅ 2026-10-16 ユーザー：こちらの誤りは 04）
            it.set("heard", "相手が訂正した（こちらの聞き違い）" if kind.startswith("misheard") else "相手が訂正した（相手の言い間違い）")
            return self._ask_confirm(qid, after=kind)
        it.set("unresolved", "訂正のあとも合わない" if result == "corrected" else "相手が答えなかった")
        return self._dash(qid)

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

    def _not_current(self, qid: str) -> dict:
        """いまの質問ではない質問の番号で報告されたときの差し戻し（唯一の書き方。record_answer・report_unclear の2つの受け口）。"""
        return self._reject(f"{qid} は、いまの質問ではありません。")

    def _judged_reply(self, qid: str, judged: str, result: str | None, correction: str | None) -> "tuple[str, str | None] | dict | None":
        """言い直しへの返事を、判定の組が確かに読んだ答え（judged＝_judged_answer）で決める唯一の場所（_op_confirm_field の入口）。返り値＝会話AIの報告に代わる
        (result, correction)・そのまま返す返事・None（この質問の答えではない読み＝今までどおり）。会話AIの「合っている」「違う値を言われた」・選んだ選択肢は使わない
        （会話AIは判定に加えない＝11-08 12時11分40秒）。直した値の文字だけは会話AIが渡す：
        - 選択肢の型：判定の読みが言い直した選択肢なら合っている・ほかの答え（選択肢・どちらでも・どれも要らない・わからない・答えたくない）ならその答えに直された
        - 値を伺う型：判定が合っていると読めば合っている（会話AIが「違う値」と言っても直した値は使わない。会話AIが「合っている」に添えた別の値＝AIが自分で言い直した
          値＝G-36 は、今までどおり値の文字として念のため言い直す）・違う値を言われたと読めば直された（直した値は会話AIの報告から。会話AIが値を渡さなければ、
          お客様には何も言わずに報告し直させ、同じ発話で2回目なら質問から聞き直す）・わからない・答えたくないと読めばその答えで受け取る
        - 答えを選んでいない（決められない・ご希望を伺う質問＝asks_wish の「わからない」＝_confirm_again の undecided と同じ組）：言い直しへの返事がそろわないときの
          1か所（_confirm_again）：理由がそろって「担当ではない」「いまは分からない」ならその理由の「わからない」・もう1回だけ言い直す（言い直すのは、こちらが言い
          直した答え＝会話AIの報告ではない）・最終の確かめのあとなら「まだ決まっていない」で保留（_undecided_final）。ご希望を伺う質問でない（事実を伺う など）の
          「わからない」は答えそのもの＝「わからない」として受け取る（理由は判定が読む＝_why_of・業務の決まりで分かる方・分かる時を伺う）。⛔ 2026-11-08 日本時間
          12時台に見つけた：前の作りは、会話AIが「合っている」と報告すれば _confirm_again（最終の確かめのあとは理由「まだ決まっていない」で保留）・「わからない」と
          報告すれば「わからない」の答え（理由は判定の読み）で、会話AIの報告の置き方で道と業務の決まり（理由ごとの次の動き）が変わる形だった"""
        q, it = self.questions[qid], self.items[qid]
        choice = q.type in CHOICE_TYPES
        if judged not in ((it.condition, "affirmed", *q.all_conditions) if choice else ("affirmed", "corrected", *SYSTEM_CONDITIONS)):
            judged = UNDECIDED   # この質問の言い直しへの返事に無い読み＝答えを決められない読みと同じ（会話AIの報告へ戻して決めない）
        if judged == UNDECIDED or (judged == "unknown" and asks_wish(q)):
            why = f"意味の判定（「{label(judged)}」）：言い直しへの返事がどの答えも指していない"
            return self._confirm_again(qid, why, it.condition if choice else "affirmed", it.condition if choice else None)
        if choice:
            if judged == ANY and q.maybe:
                # 言い直しへのお返事が「どちらの場合もありうる」：可能性をもう確かめたなら、その可能性がある答えとして受け取る（同じ確かめをくり返さない・お客様の
                # 言葉を添える）。まだなら、ここで可能性を確かめる（_ask_confirm の maybe の1か所）
                if not it.maybe_asked:
                    it.set("heard", "言い直しへのお返事が、どちらの場合もありうる（依頼者が備える答えの可能性を確かめる）")
                    return self._ask_confirm(qid, after="maybe")
                it.condition = it.value = q.maybe[0]
                it.hedged = customer_said(self.callee_said) or it.hedged or ANY_LABEL
                it.maybe_asked = "done"
                return "affirmed", None
            if judged in (it.condition, "affirmed"):
                return "affirmed", None
            return ("corrected", label(judged)) if judged in q.all_conditions else None
        if judged == "affirmed":
            return "affirmed", correction if result == "affirmed" else None
        if judged in SYSTEM_CONDITIONS:
            self.awaiting_confirm = None
            return self._take_system(qid, judged)
        if judged != "corrected":
            return None
        if correction is not None and (result == "corrected" or other_value(q.type, it.value, correction)):
            return "corrected", correction
        if self.value_asked_at != (qid, self.callee_turns):
            self.value_asked_at = (qid, self.callee_turns)
            return self._reject(f"相手は言い直した値と違う値を言っています（意味の判定）。相手が言った値を「{ARG_LABEL['correction']}」に入れて、confirm_field を"
                                f"{ARG_LABEL['result']}「{CONFIRM_LABEL['corrected']}」で、すぐ報告してください。")
        self.awaiting_confirm = None
        return self._ask_again(qid, self._ask_kind(), "意味の判定は違う値を言ったと読んだが、会話AIが直した値を報告しない")

    def _dash(self, qid: str, shown: str = "") -> dict:
        """「-」にした項目（呼ぶ前に、記録の理由を付けて unresolved にする）から次へ進む唯一の場所。名前の読みの確かめなら、読みを確かめられ
        なかったことと、このあとはお名前を使わずにお呼びすることも伝える（_reading_note。⛔ 2026-11-04 に見つけた穴：6か所で別々に書き、
        読みの知らせを付ける所と付けない所があった）。shown＝頭に添える言葉（受け取った値など）。"""
        return self._advance(f"{shown}「-」にします。" + self._reading_note(qid))

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
        if self.end_reason is not None and la["phase"] != "closing":
            # その確かめで締めに進んでいた：締めを取り消す（締めた理由と、締めの記録を1つ。⛔ 2026-10-24 の見直し：段だけを戻していた＝締めた
            # 理由と締めの記録が残り、二つ目の締めで上書きしていた）
            self.facts["end"].pop()
            if not self.facts["end"]:
                del self.facts["end"]
            self.end_reason = None
        self.phase, self.current, self.recheck_queue = la["phase"], la["current"], la["recheck_queue"]
        self.awaiting_confirm, self.confirm_requested_at, self.awaiting_say = la["qid"], la["confirm_requested_at"], la["awaiting_say"]

    def _op_replay_opening(self, a: dict) -> dict:
        """名乗りを録音で流し直す（DESIGN.md 6-13。✅ 2026-10-13 ユーザー：「名乗り直しも録音で流す」）。AIは部分を選ぶだけで、自分では言わない。
        ⛔ G-22・G-29：AIが自分で名乗り直すと、社名が正しく聞こえない（24本中、2通りの耳の両方で正しく聞こえたのは3本）。
        流すもの：相づち「失礼いたしました。」（_ack）→ 選んだ部分の録音 → いまの質問の録音（答えを待っている質問があれば）。
        言い直しの返事待ちのときは、録音のあとにAIが言い直しをもう一度言う（その言い方は値で変わるので録音にできない）。"""
        part = a.get("part")
        if (why := self._explain_problem(a)) is not None:
            return self._reject(why)
        if (k := next((k for k in PROPOSAL_ARGS if (a.get(k) or "").strip()), None)) is not None and part != "talk":
            return self._reject(f"「{ARG_LABEL[k]}」は、「{PART_LABEL['talk']}」で申し出へのお話にひとこと応えるときだけ書きます。外して、もう一度呼んでください。")
        if self.phase != "calling" and (routed := self._judged_first()) is not None:
            # 答えの段：会話AIがどの部分を選んでも（質問に戻る・受け答え・自分の言葉で答える・代わった方へ…）、判定が待つ・かけ直す・考えている途中・もう一度
            # 言ってほしいと読んだ言葉なら、判定の読みで応じる1か所で応じる（お呼びする段は、電話口の方が誰かを決める1か所 _calling_reported が受け持つ）。
            # ⛔ 2026-11-06 日本時間13時台の見直し（C6）：「代わった方へ」だけに当てた。⛔ 23時22分41秒のご質問の見直し（W1）：ほかの部分では、待つように言われても
            # 「話を戻させていただきますね。」・受け答えを流していた
            return routed
        if part not in ("resume", "handover"):
            self._keep_answer_reading()
        asked = self._asked_now()   # 答えの段で、いまの質問・言い直しに答える代わりにこちらへ尋ねた（答えたあと、話はそれていない）
        if (part in OWN_PARTS or part.startswith(ANSWER_PREFIX)) and asked is None:
            # 相手のお話・ご質問に応える。名乗り・用件の流し直しは、お話の流れのうち＝戻る一言は要らない。「AIですか」のあとは、決まった言い方の
            # お詫び（「失礼いたしました。」）・お約束（「最後まで…正確にご案内させていただきます。」）が戻る一言を兼ねる（✅ 2026-10-19 ユーザーの流れ）。
            # いまの質問・言い直しについて尋ねられた問いに答えたあとは要らない（✅ 2026-11-06 日本時間11時19分55秒 ユーザー：「話はパンフレットをから移って
            # いないので戻す必要はなく、あいてが迷っている最中の最終確認をして、パンフレットのシーンを締めないといけないんじゃないですか？」＝_final_check。
            # ⛔ 10時台に私が戻る一言を付けた＝取り消した）
            self.digressed = True
        if part.startswith(ANSWER_PREFIX):
            return self._answer(part)
        # 申し出への会話が繋がらないお返事にひとこと応える番（_asked_kind の offer）は、受け答えが応え方そのもの（_own_answer → _offer_again）
        if part == "talk" and self._asked_kind() != "offer" and (asked is not None or (self.after_answer is not None and not (self.own_pending or self.ai_pending))):
            # 答えに添えられた相手の質問・いまの質問について尋ねられた問いに、まだ答えていない：受け答えでは答えたことにならない（⛔ 2026-10-23 G-57
            # tc42：AIが質問に答えないまま、相手の次の言葉に受け答えし、プログラムが次の質問へ進んだ＝相手の質問が答えられずに残った）。
            # 尋ねられた問い（この発話の問い＝_asked_now）は、前の発話で応えて返事を待っている印が残っていても同じ（⛔ 2026-11-06 日本時間5時56分55秒の
            # ご質問のあとの読み直しで見つけた：前の発話の印で受け答えが通り、使えないときの一言の無い受け答えが例外で止まった・受け答えだけで問いに
            # 答えたことになった）
            return self._reject(f"先に相手の質問に答えます。{self._now()}")
        if part in OWN_PARTS:
            return self._own_answer(part, a.get("answer"), a.get("fallback"), *(a.get(k) for k in PROPOSAL_ARGS))
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
        if part == "handover" and self.asking_reception and self.target and (self.callback_held or self._hold_read()):
            # かけ直しの質問のあとに取り次ぎがあって、代わった方が話し始めた：お呼びした方が戻ったのかもしれない（AIが「目的の人」と報告した
            # ときと同じ1か所＝_target_back で、1回確かめる）。⛔ 2026-10-22 G-57 tc27（かけ直しの途中で戻る・S2）：代わって出た岡田様の
            # 「お電話代わりました。岡田です。」を、AIは「代わった方」と報告し、あいさつ・名乗り・用件のあと「何時ごろお戻りに…」を流した。
            # 取り次ぎが無ければ、電話口の方は同じ方（かけ直しの質問を聞いた方）：確かめずに、下の流し直し。⛔ G-57 tc28（不在で別の方に
            # 代わる・S4）：かけ直しの質問を聞いた山田様の言葉の続きに、AIがまた「代わった方」と報告し、山田様に岡田様かを確かめた
            return self._hold() if self._hold_read() else self._target_back()
        if part == "handover":
            # 相手が替わった：分かる方に代わったなら、元の質問へ（流れの決まり＝名乗りの録音の有る無し・流し直しの回数より先に。⛔ 2026-10-24 の
            # 見直し：録音が無い・流し直しを使い切った通話では、ここまで来ずに差し戻し、代わった方の答えも「いまの質問ではない」で差し戻した）
            self._who_took_over()
            if not self.opening or self.replays >= self.limits.replays:
                return self._new_person_unrecorded()
        if self._opening_answer(part) and (other := self._asked_other()):
            return other
        if part == "target":
            return self._asked_target()
        if not self.opening:
            return self._reject("この通話では、名乗りの録音はありません。聞かれたことに、自分で短く答えてください。")
        if self.replays >= self.limits.replays:
            return self._reject(f"名乗りの録音は、もう{self.replays}回流しました（同じ録音を何度も流しません）。聞かれたことに、自分の言葉で短く答えてください。")
        keys = {"intro": ["intro"], "purpose": ["purpose"], "all": ["intro", "purpose"], "handover": ["greeting", "intro", "purpose"]}[part]
        # まだ届いていない名乗り・用件も一緒に流す（on_opening_cut）。あいさつは足さない（頭にお詫びの相づちが流れるので、重ねない）。
        # 目的の人に繋がる前（calling）は、用件は聞かれたときだけ流す（DESIGN.md 4-3：目的の人ではない方には、聞かれたら用件を言う）
        calling = self.phase == "calling"
        if calling and part == "all":
            keys = ["intro"]
        # お詫びの電話の「用件」は、お詫びそのもの（業務の用件は通話の初めに外してある）：用件を流す頼みには、お詫びの録音を流す。繋がる前（受付の方）
        # には、お詫びではなく用件の一言（「先日のお電話の件でご連絡いたしました。」✅ 2026-10-24 ユーザー「OK」。⛔ 前は流す録音が無く、
        # AIが自分の言葉で答える形だった）
        asked = bool(self.apology_call) and "purpose" in keys
        apology = ([] if not asked else [self._apology_play()] if not calling else
                   [{"kind": "recorded", "id": f"apology_purpose_{self.apology_when}", "text": APOLOGY_PURPOSE[self.apology_when]}])
        keys = [k for k in OPENING_ORDER if self.opening.get(k)
                and (k in keys or (k != "greeting" and k in self.opening_unheard and not (calling and k == "purpose")))]
        if not keys and not apology:
            return self._reject(f"「{PART_LABEL[part]}」の録音はありません。聞かれたことに、自分で短く答えてください。")
        self.replays += 1
        self.opening_unheard = [k for k in self.opening_unheard if k not in keys]
        play = [{"kind": "opening", "part": k, "text": self.opening[k]} for k in keys] + apology
        if asked and not calling:
            self.apology_asked = self.apology_played = True   # お詫びそのものを流した（用件の一言では、まだお詫びしていない）
        return self._continue(play, "".join(x["text"] for x in play))

    def _asked_target(self) -> dict:
        """受付の方に、どなたをお呼びしているかを聞かれた（「どちらの岡田でしょうか？」）：まだお伝えしていない部署・フルネームがあれば、それの入った
        呼ぶ一言（target_full＝お名前だけでは分からないと言われたときと同じ1つ・1通話1回）で、無ければいつもの呼ぶ一言で答える（お呼びし直すのは
        _call_target の1か所。頭は「はい、」）。✅ 2026-11-02 日本時間1時22分 ユーザー：(b)「OK」（伺い：「受付の方に「どちらの岡田でしょうか？」と
        聞かれたときも、(2) と同じ「営業部部長の「おかだたろう」様は…」で答えてよいでしょうか。」）"""
        if self.phase != "calling" or not self.target:
            return self._reject("いまはお呼びする段ではありません（お呼びした方には、もう繋がっています）。聞かれたことに合う答え方を選んでください。")
        if self.target.get("target_full") and not self.full_called:
            self.full_called = True
            return self._call_target(sorry=False, part="target_full") | {"answering": True}
        if self._calls_to_speaker() >= 2:
            return self._reject("お呼びする一言は、この方にもう2回お伝えしました（同じお願いを繰り返しません）。何も言わずに、受付の方の次のお言葉を待ってください。")
        return self._call_target(sorry=False) | {"answering": True}

    def _who_took_over(self) -> bool:
        """相手が替わった（代わった方が話し始めた）と決まったときの、分かるお客様の聞き足しの扱いの唯一の場所（AIの「代わった方へ」・判定の
        「代わった方」のどちらからも）：「分かる者に代わります」と取り次がれたなら、分かるお客様を伺った元の質問（答えがまだ取れていないもの）を、
        いま聞いている質問・言い直しの返事待ちによらず、代わった方に伺い直す（CL-2 の 15-2-3：「(a1) で取り次がれたら相手が替わる」）。
        分かるお客様の聞き足しはもう伺わない（取れていたお名前のかけ直しの記録は、言われたことの記録として残す。⛔ 最初は「繋がった」印 connected を
        付けたが、CL-2 ではその印は「目的のお客様に繋いだ」の意味で、繋いだ記録の無い報告を断る＝意味の違う印の流用。必要な質問が取れれば、CL-2 の
        判定は、かけ直しより先に「取れた」で終える）。当てはまらなければ False。
        ⛔ 2026-10-24 の見直し（調べ役）で見つけた穴：聞き足しを伺っている最中だけ戻していた＝お名前の言い直しの返事待ちのまま取り次がれると、
        代わった方に言い直しを言わせて止まった／お名前を伺い終えて次の質問へ進んでいたら、元の質問を伺わず、繋がった方へのかけ直しを約束させた"""
        took = [(fid, orig) for fid, orig in self.follow_ups.items()
                if self.questions[fid].callback == "ask_who" and self.items[orig].state not in GOT]
        for fid, orig in took:
            self.follow_ups.pop(fid)
            if self.awaiting_confirm == fid:
                self.awaiting_confirm = None
            # いま聞いていた別の質問（お名前を伺い終えて進んでいた）は、元の質問のあとに伺う
            back = [self.current] if self.current not in (None, fid, orig) and self.items[self.current].state not in GOT else []
            self.order = [*back, *[q for q in self.order if q not in (fid, orig, *back)]]
            del self.questions[fid], self.items[fid]
            # 元の質問は、この電話でもう流した＝代わった方への伺い直しも頭は「それでは」（Item.played を残す。⛔ 2026-11-06 日本時間13時台の見直しで見つけた：
            # 印を作り直すときに流した印も消し、代わった方に「…お電話いたしました。続きましてご予算は…」と次の項目の頭で伺っていた）
            self.items[orig] = Item(played=self.items[orig].played)
            self.current = orig
        return bool(took)

    def _new_person_unrecorded(self) -> dict:
        """代わった方に、名乗りの録音を流せない（録音の無い通話・流し直しを使い切った）：AIが短く名乗って用件を伝え、いまの質問を聞く。"""
        q = self.questions.get(self.current) if self.current else None
        ask = f"いまの質問 {q.id}「{q.text}」を聞いてください。" if q is not None else "相手の返事を待ってください。"
        if q is not None:
            self.items[q.id].played = True   # 会話AIが、この方にいまの質問を聞く（流した印＝Item.played）
        intro = (self.opening or self.target or {}).get("intro")
        name = f"「{intro}」と" if intro else ""
        return {"result": "replayed", "play": [], "speak": True,
                "message": f"代わった方には、まだ名乗っていません。{name}短く名乗り、用件を伝えてから、{ask}"}

    def _answer_may_come_first(self) -> bool:
        """いまの相手の言葉に、まだ答えをもらっていないいまの質問の答えが入っているかもしれない（_keep_answer_reading が判定を見る場面）。答え方の操作も、
        この間は判定を待つ（wants_checks。⛔ 2026-10-22 の4回目の流し G-57 tc37：AIが判定の届く前に答え方の操作を呼び、差し戻せなかった）。"""
        q = self.questions.get(self.current) if self.current else None
        # ご納得いただけなかった点のご説明の最中（_explaining）は、いまの質問（N1・「ほかに…」）の答えはもう伺ったあと（ご説明はその答えへの応え）
        return (self.phase in ("asking", "rechecking") and q is not None and not q.reading_of and self.awaiting_confirm is None
                and self.after_answer is None and self.items[q.id].state not in GOT and not self._explaining())

    def _keep_answer_reading(self) -> None:
        """相手の言葉に、いまの質問への答えが入っているかもしれないのに、AIが答えの報告ではなく答え方（replay_opening）を呼んだ（答えと質問を一緒に
        言われた・世間話をされた）：独立した意味の判定のどれかが、その言葉をいまの質問の答えと読んでいれば、その言葉と確かめを覚えておく
        （answer_held）。AIは相手に応えたあと、いまの質問へ戻る前に、その言葉の答えを報告できる（_return_note）。報告は、覚えておいた言葉と
        確かめに当てて、ふつうの答えと同じ1か所（_op_record_answer）で決める＝受け取るのは、AIが音声から報告した答えと独立した確かめがそろった
        ときだけ。AIには判定の読みを見せない。
        ⛔ 2026-10-22 の4回目の流し（G-57 tc36・答えに添えられた質問）：AIが先に「用件」の流し直しを選び、相手がもう答えた質問の録音がもう一度流れた
        ⛔ 2026-10-23 の失敗（2回）：その手当てとして、AIを差し戻して答えを先に報告させていた（_answer_first）。会話AIが世間話の「いや、」を
           「Ja.」と聞き取ると、その文字を読んだ判定2つがそろって「はい」と読み、AIは世間話に返す番を失って、判定の読みどおり「はい」と報告し、
           その報告が判定とそろったことになって依頼主へ渡る答えになった（G-57 tc44・tc45：世間話 L4 の2本とも。AIは音声を聞いて、正しく
           世間話と分かっていた）＝判定の読みからAIの報告を作ると、その報告は判定と独立でない（判定2つも、同じ聞き取りの文字を読んでいる）。
           お客様の世間話にも、何も返さなかった（✅ 2026-10-23 ユーザー：「お客様の気分を害する返答」）
        ⛔ 2026-10-23 の3回目（G-57 tc48・答えに添えられた質問の S2・S4）：その次の形＝判定がそろった読みを、答えたあと「念のため（意味の言葉）で
           よろしいでしょうか？」で確かめる形は、意味の言葉の無い選択肢（取込の2つのモデルの言葉がそろわなかった質問）・値を聞く質問では
           確かめが作れず、もう答えてもらった質問をもう一度流した（S2 は、相手が次に言った日時「15日の午後2時」を予約の質問の答えとして
           報告し、そのあとの答えが1つずつずれた）。
           判定がそろわない読みのときも同じだった ⇒ 確かめの文を作らずに、AIが聞いた答えを報告する形にした（どの型でも、判定が1つ食い違う
           ときも、ふつうの答えと同じ決まりで決まる）"""
        q = self.questions.get(self.current) if self.current else None
        if self._answer_may_come_first() and self._heard_answer(q):
            # 確かめはその発話の入れ物のまま持つ（この発話のうちに届く確かめも入る。次の発話では、入れ物が新しくなる＝on_callee）
            self.answer_held = {"turn": self.callee_turns, "qid": q.id, "said": self.callee_said, "check": self.check}

    def _heard_answer(self, q: "Question") -> bool:
        """いまの相手の言葉を、使っている意味の判定のどれかが、いまの質問の答え（選択肢・どちらでも・わからない・答えたくない）と読んだ
        （_keep_answer_reading の1か所。届いた判定だけを見る＝待つ間は判定を全部待つ＝waits_every）。答えを受け取るかは、ここでは決めない
        （AIの報告と確かめをそろえる _op_record_answer）。判定を使わない通話では読みが無いので、覚えない（いまの質問を聞く）。"""
        if self.check_for not in (None, q.id):
            return False   # いまの発話の判定は、ほかの質問について読んだもの（ご遠慮の一言への「いいえ」を、元の質問の「いいえ」と取り違えない）
        return any(self.check.get(k) in q.all_conditions for k in JUDGMENTS if k in self.checks_used)

    def _held_words(self, name: str, args: dict) -> dict | None:
        """この報告が、覚えておいた前の発話の言葉（answer_held）の答えなら、その言葉（_apply_one が、報告の間だけ judged にする）。
        答えを当てる言葉は、判定のどれかが、いまの質問の答えと読んだいちばん新しい言葉：いまの発話で報告した（相手に応える前・応えた直後）・
        いまの発話を判定が答えと読んだ（「あ、やっぱりまだでした」と言い直された）なら None＝いまの言葉のまま。
        言い直しへの返事として覚えた言葉（confirm＝待つかかけ直すかを伺っている間に、伺う前の言い直しに返事をした＝_wait_left）は、言い直しへの返事の報告
        （confirm_field）にだけ当てる。"""
        h = self.answer_held
        if name not in ("record_answer", "confirm_field") or h is None or (h["turn"] == self.callee_turns and not h.get("ahead")):
            return None
        if (name == "confirm_field") != bool(h.get("confirm")):
            return None
        if args.get("question_id") != h["qid"] or h["qid"] != self.current or self._heard_answer(self.questions[h["qid"]]):
            return None
        return h

    def _held_now(self) -> dict | None:
        """覚えておいた言葉（answer_held）が、いまの質問のまだ受け取っていない答えのものなら、その言葉（無ければ None）。"""
        h, q = self.answer_held, self.questions.get(self.current) if self.current else None
        return h if h is not None and q is not None and h["qid"] == q.id and self.items[q.id].state not in GOT else None

    def _take_rest(self, name: str, a: dict) -> dict | None:
        """報告に添えられた「ほかに言われたこと」（rest）を、先に言われた答えとして持つ唯一の場所（_apply_one。報告が断られたら前に戻す＝返り値）：
        その報告が当てる言葉（前の言葉の答えなら、その言葉＝judged）と一緒に持つ。先に言われた答えから報告して、ほかに残りが無ければ、もう使わない。
        ✅ 2026-11-04 日本時間20時27分 ユーザー：「(3) 方法と一緒に連絡先まで言われたとき（例：「LINEなら、IDは abc_123 です」）案：前の答えの中に
        その連絡先があれば、伺わずに「abc_123でよろしいでしょうか？」と言い直して確かめます。OK」＝9-3「取得済みのことを重ねて聞かない」と同じ理由が、
        どの質問にも当たる（10-19 の決まり）。⛔ 2026-11-04 日本時間21時台に見つけた：(3) を連絡先だけの別の道（W1・C11 の値の形を見る）で作っていて、
        かけ直しの「5時には戻ります」・ほかの手段の「…info@example.co.jp にお送りください」・別の番号の「…03-1234-5678 です」・業務の質問の
        「はい、予約しています。15日の午後2時でお願いしています。」の答えは、重ねて伺っていた。値の形の確かめも、ふつうの答えの確かめの写しで、
        「ファックスで」「資料を送ってもらえれば」を連絡先として言い直していた"""
        prev, rest, h = self.ahead, (a.get("rest") or "").strip(), self.judged
        if name == "replay_opening":
            # 質問へ戻る（resume）：先に言われたことを伺っていたときだけ。次の質問の答えは無かった＝まだ使っていないことが残っていれば持ち続け、
            # 無ければ、もう使わない（⛔ 21時台に流して見つけた：戻るたびに捨てていたので、「LINEなら、090-1234-5678 です」の番号を、伺い方を
            # 選んでいただいたあとにもう一度伺った＝20時27分のご承認「選んでいただいた方の連絡先として言い直す」から外れていた）
            h = self._held_now() if a.get("part") == "resume" else None
            # 申し出へのお話にひとこと応える受け答え（_asked_kind の offer）に添えた「ほかに言われたこと」（「オンラインしか無理なんですよ。」の方法など）は、
            # 申し出の条件にせず、このあと伺う質問（方法 N5 など）の先に言われた答えとして持つ（方法の決まりは業務の方法と同じ1か所のまま＝_offer_ahead・N5・
            # W1。9-3「取得済みのことを重ねて聞かない」）。⛔ 2026-11-09 日本時間8時台の見直しで見つけた：方法を条件「オンラインで」として伺い直し、そのあと
            # 「訪問とお電話のどちらがよろしいでしょうか？」と重ねて伺う形だった
            talked_offer = a.get("part") == "talk" and self._asked_kind() == "offer"
            if not talked_offer and (h is None or not h.get("ahead")):
                return prev
        if rest:
            turn, said = (h["turn"], h["said"]) if h is not None else (self.callee_turns, self.callee_said)
            self.ahead = {"turn": turn, "said": said, "rest": rest, "qid": None, "check": {}, "asked": False}
        elif h is not None and h.get("ahead"):
            self.ahead = None
        return prev

    def _offer_ahead(self, q: "Question", prefix: str) -> dict | None:
        """次に伺う質問 q へ進むとき、先に言われた答え（ahead）があれば、質問を流さずに、その言葉の答えを報告してもらう（唯一の場所＝_advance_to）。
        報告は、その言葉と、その言葉を q について読み直した確かめ（選ぶ質問＝ahead_context）に当て、ふつうの答えと同じ1か所（_op_record_answer）で
        決める（値を伺う質問は言い直して確かめる）。入っていなければ、AIが質問へ戻る操作を選び、プログラムが質問を流す（_resume）。答えの段で、まだ
        伺っていない質問だけ。"""
        a = self._ahead_now()
        if a is None or a["qid"] == q.id or self.items[q.id].state != "empty":
            return None
        self._hold_ahead(q.id)
        return {"result": "next", "question_id": q.id, "question": q.text, "message": f"{prefix}{self._return_note()}", "ahead_offer": True}

    def _hold_ahead(self, qid: str, back: str | None = None) -> None:
        """先に言われた答え（ahead）を、質問 qid について判定に読み直してもらい（ahead_context）、会話AIの報告をその言葉に当てる（_held_words）印を立てる唯一の場所
        （次に伺う質問へ進むとき＝_offer_ahead・待つかかけ直すかを伺っている間に、伺う前の話に答えた＝_wait_left）。back＝伺う前の話へ戻した言葉の種類（"answer"＝
        元の質問への答え・"confirm"＝元の言い直しへの返事）。"""
        a = self.ahead
        a.update(qid=qid, check={}, asked=False)
        self.answer_held = {"turn": a["turn"], "qid": qid, "said": a["said"], "check": a["check"], "ahead": True, "rest": a["rest"],
                            "offered": self.callee_turns, "back": back, "confirm": back == "confirm"}

    def _ahead_now(self) -> dict | None:
        """いま使える先に言われた答え（唯一の決まり＝質問を流さずに伺う所 _offer_ahead・もう伺った連絡先より先に使うか _wish_contact）：答えの段か、締めの前の
        念のための確認の段（その段で初めて伺う質問＝_offer_ahead は、まだ伺っていない質問だけ）。⛔ 2026-11-05 日本時間2時05分37秒のご質問のあとの読み直しで
        見つけた：2か所で別の条件にしていて、確認の段で伺ったほかのご希望（「メールなら、担当の b@… にお願いします。」）に、言われた b@ も、もう伺った a@ も
        使わずに「それでは恐れ入りますがメールアドレスを…」と伺った（0時31分の直しで起きた後戻り）"""
        return self.ahead if self.phase in ("asking", "rechecking") else None

    def ahead_context(self) -> tuple["Question", str | None, str | None, str] | None:
        """先に言われた答えを、次に伺う選ぶ質問について読み直す判定（電話接続側が、報告の返事を作ったあとに呼ぶ。確かめの名前 judge@ahead・judge2@ahead
        ＝AHEAD_CHECKS）：(質問, こちらが言い直して確かめた言葉, その前に相手が言ったこと, 読み直す相手の言葉)＝はじめの3つは、いまの発話の判定に渡すもの
        （judge_context）と同じ形。判定の読みは、伺ったときの発話の番号で返す（_ahead_check）。1つの先に言われた答えにつき1回だけ（2回目からは None）。
        値を伺う質問は言い直して確かめるので読み直さない（None）。ただし、伺う前の言い直しへの返事（待つかかけ直すかを伺っている間に返事をした＝_wait_left の
        confirm）は、その返事が言い直しの確かめそのものなので、どの型でも、言い直した言葉と一緒に読み直す。判定を使わない通話も None。"""
        a, h = self.ahead, self.answer_held
        if (a is None or h is None or not h.get("ahead") or a.get("qid") != h["qid"] or h["qid"] != self.current or a["asked"] or not self.judging
                or (self.questions[h["qid"]].type not in CHOICE_TYPES and not h.get("confirm"))):
            return None
        a["asked"], a["judge_turn"], self.ahead_asked = True, self.callee_turns, a
        q = self.questions[h["qid"]]
        before = before_words(q, [w for t, w in self.answer_words.items() if t < a["turn"]])
        return self.for_judge(q), (self._judge_text(self.awaiting_say) if h.get("confirm") else None), before, a["said"]

    def ahead_pending(self) -> bool:
        """読み直した判定が、まだ1つも届いていない（電話接続側が、先に言われた答えの報告を受け取る前に、待つ長さの中で待つ＝convo の judged）。"""
        a, h = self.ahead_asked, self.answer_held
        return (a is not None and h is not None and h.get("ahead") and h["qid"] == a["qid"] == self.current
                and not any(k in a["check"] for k in JUDGMENTS))

    def _ahead_check(self, kind: str, value: str, turn: int) -> None:
        """読み直した判定の読み（kind＝もとの判定の名前）を、先に言われた答えの確かめにだけ当てる（on_check の1か所。いまの発話の判定・ほかの質問の
        材料には当てない）。その答えをもう受け取っていれば、受け取った答えには当てず記録だけ（受け取った時に決めた＝_late_to_taken と同じ決まり）。"""
        a = self.ahead_asked
        if a is None or a.get("judge_turn") != turn or kind in a["check"]:
            return   # （turn＝読み直しを出したときの発話の番号＝ahead_context。前の読み直しの遅れた読みは当てない）
        a["check"][kind] = value
        t = self.taken.get(a["qid"])
        if t is not None and t["turn"] == a["turn"] and kind not in t["check"]:
            t.setdefault("late", {})[kind] = value

    def _return_note(self) -> str:
        """相手に応えたあと、いまの位置へ戻るときにすること（AIへの知らせの文。_own_then・_now・_continue の1か所）。覚えておいた言葉
        （answer_held）があれば、その言葉の答えの報告か、質問に戻るかを、AIが音声から選ぶ（判定の読みは見せない）。"""
        resume = f"replay_opening を「{PART_LABEL['resume']}」で呼びます"
        if self._held_now() is None:
            return f"{resume}。"
        q, h = self.questions[self.current], self._held_now()
        if h.get("back"):
            # 待つかかけ直すかを伺っている間に、待たずにお話を続けた（_wait_continue）：その言葉に、伺う前の質問の答え・言い直しへの返事が入っているか
            if h.get("confirm"):
                return (f"相手は、待たずにお話を続けました（「{h['rest']}」）。この言葉が、{q.id} の言い直し「{self.awaiting_say}」への返事なら、何も言わずに "
                        f"confirm_field で、聞いたとおりに報告します。{self._confirm_how(q.id)}返事でなければ、{resume}（言い直しは、プログラムが流します）。")
            return (f"相手は、待たずにお話を続けました（「{h['rest']}」）。この言葉に、伺う前の質問 {q.id}「{q.text}」への答えが入っていたなら、何も言わずに "
                    f"record_answer で、聞いた答えのとおりに報告します（「わからない」「答えたくない」も答えです）。入っていなければ、{resume}"
                    "（質問は、プログラムが流します）。" + choices_note(q))
        if h.get("ahead"):
            # 先に言われた答え（_offer_ahead）：次に伺う質問の答えが、こちらの質問の答えのほかに言われたことの中にあるか
            return (f"相手の言葉のうち、答えのほかに言われたこと（「{h['rest']}」）に、次に伺う質問 {q.id}「{q.text}」への答えが入っていたなら、"
                    f"何も言わずに record_answer で、聞いた答えのとおりに報告します（ほかにまだ言われたことがあれば、{ARG_LABEL['rest']}に書きます）。"
                    f"入っていなければ、{resume}（次の質問は、プログラムが流します。ほかに言われたことのうち、このあと伺うことの答えになりそうなことが"
                    f"残っていれば、{ARG_LABEL['rest']}に書きます）。" + choices_note(q))
        return (f"相手のさきほどの言葉（こちらが応える前の言葉）に、いまの質問 {q.id}「{q.text}」への答えも入っていたなら、何も言わずに "
                f"record_answer で、聞いた答えのとおりに報告します（「わからない」「答えたくない」も答えです）。入っていなければ、{resume}"
                + ("（いまの質問は、プログラムが録音で流します）。" if self.play_recordings else "。") + choices_note(q))

    def _answer(self, part: str) -> dict:
        """相手の質問に、想定問答の答え（業務設定。Intent.md 5-3 (2)。依頼主の単位の録音）で答える（唯一の場所。呼んでいる段でも、答えの段でも）。
        AIは問いの名前を選ぶだけ。録音で流す通話は録音、AIが話す通話はその文のとおりに言わせる。答えたあとは、いまの位置へ戻る（_continue：
        呼んでいる段なら、その方にまだ2回呼んでいなければもう一度お呼びする・答えの段ならいまの質問）。同じ答えは1通話に2回まで。
        想定問答に無い問いは、AIが業務の情報の範囲で自分の言葉で答える（_own_answer）。
        ⛔ G-57 tc18：AIは「この番号はどちらでお知りになりましたか？」に答えず（3本中2本）、自分の言葉で答えた回は、目的の人の名前をAIの声で言った"""
        name = part[len(ANSWER_PREFIX):]
        text = self.answers.get(name)
        if text is None:
            return self._reject(f"「{name}」の答えはありません（{self._answer_choices()}）。")
        self.answered[part] = self.answered.get(part, 0) + 1
        if self.answered[part] > 2:
            return self._reject("同じ答えを、もう2回流しました（同じ答えを何度も流しません）。何も言わずに、相手が話すのを待ちます。")
        self._note("answered", part=part)
        self._point_said("answer")
        if self._explaining() or self._asked_now() is not None:
            # ご納得いただけなかった点のご説明（想定問答の答え＝依頼者の資料）：流したあと、お客様の返事（「そうなんですね」）を聞いてから「はい、」で
            # 「ほかに…」を伺う（AIが自分の言葉でご説明したときと同じ＝_resume）。✅ 2026-11-02 日本時間5時56分 ユーザーの流れ。
            # 答えの段で尋ねられた問い（_asked_now）に想定問答の録音で答えたときも同じ：自分の言葉で答えたときと同じく、お客様の返事を待ってから戻る（✅ 2026-11-06
            # 日本時間4時23分33秒 ユーザーの流れ。⛔ 5時56分55秒のご質問のあとの読み直しで見つけた：録音で答えると、待たずに質問を流し直していた）
            self._await_reply("own")
            if self.opening and self.opening.get(part):
                return {"result": "replayed", "silent": True, "play": [{"kind": "opening", "part": part, "text": text}],
                        "message": f"{played_note(text)}何も言わずに、{self._own_then()}"}
            return {"result": "replayed", "say": text, "speak": True, "message": f"{say_note(text)}{self._own_then()}"}
        if (self.opening and self.opening.get(part)) or self.after_answer is not None:
            # 録音が無い通話で、答えに添えられた質問なら、AIがその文を先に言ってから次へ（_continue → _led）
            return self._continue([{"kind": "opening", "part": part, "text": text}], text)
        return {"result": "replayed", "say": text, "speak": True,
                "message": f"{say_note(text)}そのあと、{self._now()}"}

    def _own_answer(self, part: str, answer: str | None, fallback: str | None = None, terms: str | None = None, added: str | None = None,
                    topic: str | None = None, way: str | None = None) -> dict:
        """AIが自分の言葉で言う（唯一の場所。呼んでいる段でも、答えの段でも、答えに添えられた質問でも）：相手の質問に録音の答えが無い（名乗り・
        用件・想定問答のどれでもない）とき＝自分で答える質問（知っていることにある問い）・答えを持っていない質問（無い問い）と、質問ではない言葉
        （世間話・お礼・気遣い・お客様の話）への受け答え。✅ 2026-10-22 ユーザー：「人以上の仕組みを作ることが私たちの目的ではないのですか？」・
        （5回目）「AIの能力をそぐような制御はしないように！世間話のような対応や設定にない無難な対応はライブAIにしかできないよ！」
        - 答える文（answer）は、AIが声に出す前に操作の中に書き、プログラムが確かめる（own_answer_problem＝明らかに違う中身だけ。中身の判定＝grounding
          の読みは、電話接続側が on_grounded で先に渡す）。通れば、その文のとおりに言わせる（外れたら止める見張り offscript の対象）。通らなければ、
          理由を添えて書き直させる（同じ相手の発話で OWN_REWRITES 回まで。その間は声に出さない）。知っていることの文をそのまま言った文は、中身の
          判定に聞かない（grounding.verbatim）。中身の判定が届かなかった文は、声に出さず、書き直させずに受け皿へ（UNCHECKED）。
        - それでも通らなければ、問いの種類で受け皿を分ける（✅ 2026-10-22 ユーザー（5回目）：「お客様から見ても、それは答えられないだろうなと思う
          ような質問ならそれでよいが、それは答えられなかったらおかしいだろうと思われる内容や、世間話に近いような…では、別の対処が必要」）：
          答えを持っていない質問＝断らない一言の録音（phrases.OWN_FALLBACK）／自分で答える質問＝AIが答えの文と一緒に選んだ、答えのもとにした
          知っていることの文（fallback）をそのまま言わせる／受け答え＝AIが返す文と一緒に選んだ一言（fallback＝phrases.TALK_FALLBACK）の録音
          （✅ 2026-10-23 ユーザー：「（言葉を足さずに「はい、」で質問に戻るのは）お客様の気分を害する返答だということがわからないんですか？
          前回も言ったことが全く理解できていないんじゃないですか？」⛔ 前は、受け答えの受け皿で何も返さずに、いまの質問へ戻っていた）
        - 答えたあと、相手の返事を聞いてから「質問に戻る」でいまの位置へ戻る（_resume・_continue）
        - 相手の質問は通話の事実 callee_question に残す（依頼主へ渡す）。受け答えは talk に残す（依頼主へは渡さない）。同じ発話には1回だけ。
        - 申し出（N4・N6）への会話が繋がらないお返事にひとこと応える番（_asked_kind の offer）：受け答えのひとことを同じ確かめに通したあと（使えなければ受け答えの
          録音）、お返事を待たずに申し出を伺い直す（_offer_again）。提案の言葉（ご都合の条件 terms・申し出に含める中身 topic・ご希望の方法 way）は、この番のときだけ書く（声に出す前に
          自分の言葉と同じ確かめ＝_proposal_problem を通す。通らなければ書き直させ＝回数も自分の言葉と同じ、使い切ったら・中身を確かめられなかったら、お客様の
          言ったことに依らない形で伺い直す）。"""
        if self.own_turn == self.callee_turns:
            return self._reject("この言葉には、もう答えています（同じ言葉に2回答えません）。")
        offer = self._asked_kind() == "offer"
        proposal = {k: (v or "").strip() or None for k, v in zip(PROPOSAL_ARGS, (terms, added, topic, way))}
        given = [k for k in PROPOSAL_ARGS if proposal[k] is not None]
        if given and not offer:
            return self._reject(f"「{ARG_LABEL[given[0]]}」は、申し出へのお客様のお話にひとこと応える番だけに書きます（いまは書きません）。外して、もう一度呼んでください。")
        if offer and part != "talk":
            return self._reject(f"いまは、相手のお話にひとこと応える番です。replay_opening を「{PART_LABEL['talk']}」で呼びます。")
        rejected, proposal, dropped = self._proposal_checked(proposal, given, self.current, offer)   # dropped＝声に出さなかった提案の言葉と、その理由（受け答えの記録に残す）
        if rejected is not None:
            return rejected
        # 使えないときの一言（受け答え＝決まった一言・自分で答える質問＝答えのもとにした知っていることの文）。要るのは、答えの文が使えないとき
        # だけ（⛔ 2026-10-23 tc46：選んでいない報告をはじめから差し戻したので、AIが「答えの文」と「使えないときの一言」の片方を付け忘れるたびに
        # 相手を待たせた＝お礼の S4 で11回）
        kind = part
        pick = fallback if fallback is not None and not self._fallback_missing(kind, fallback) else None
        # 答えの段で、いまの質問・言い直しに答える代わりにこちらへ尋ねられた（_asked_now）：いまの質問についてのこちらの考えを、自分で答える質問として
        # 答える（答えのもとにした知っていることの文が無いこともある）。その答えの文が使えなければ、受け皿の一言ではなく、尋ねられる前の流れのとおりに進める（_asked_skip）
        # 迷っているお客様に選ぶ助けを伝える番（_hesitation の help）は、答えの文が使えなければ受け皿の一言（業務の情報に無いことは担当の者に申し伝える＝9-4 の
        # 「範囲外の質問」）。尋ねられる前の流れのとおりに進める（_asked_skip）のは、こちらの考えを尋ねられたとき（答えられなかったらおかしい問い）だけ
        asked = self._asked_kind() == "asked"
        if not (answer or "").strip():
            if pick is not None:
                # 一言だけを選んだ：その一言で応える（決まった言い方・知っていることの文そのまま＝確かめなくてよい。AIの選んだ言い方）
                self.own_turn = self.callee_turns
                if offer:
                    return self._offer_again(None, pick, {}, {"checked": "fallback"}, proposal, dropped)
                return self._own_fallback(kind, pick, None, {}, {"checked": "fallback"})
            return self._reject(f"言う文（自分の言葉で、短く1〜2文）を「{ARG_LABEL['answer']}」に書いて、もう一度呼んでください（声に出すのは、"
                                "プログラムの返事のあとです）。")
        from grounding import DEFERRED, GROUNDED, Verdict, verbatim
        answer = answer.strip()
        verdict = Verdict(GROUNDED) if verbatim(answer, self.knowledge) else self._grounded(answer)
        problem = own_answer_problem(answer, self.names, may_follow_up=self._may_follow_up(kind), verdict=verdict,
                                     name_readings=self.name_readings)
        tried = self.own_tried[1] if self.own_tried[0] == self.callee_turns else []
        if problem and len(tried) < OWN_REWRITES:
            # 書き直させる（返事の文には言い方の例を入れない。⛔ G-57 tc31：返事の注意書きをAIが声に出した）。自分で答える質問の最後の書き直しは、
            # 知っていることの文をそのまま使うように言う（そのままなら中身の判定に聞かない＝必ず通る）
            self.own_tried = (self.callee_turns, tried + [(answer, problem)])
            last = ("" if kind != "own" or asked or len(tried) + 1 < OWN_REWRITES else
                    "次で最後です。こちらが知っていることの文を、そのまま使って書きます。")
            need = "" if pick is not None or asked else (self._fallback_missing(kind, None) or "")
            return self._reject(f"この文は声に出しません（{problem}）。{last}{need}")
        if problem is None and verdict is None:
            # 中身を確かめられなかった（判定が待つ長さに届かなかった・止まっている・判定を使わない通話）：声に出さない。書き直しても判定は
            # 速くならないので、そのまま受け皿へ（✅ 2026-10-19 ユーザー：「明らかに違う内容を喋ろうとしているのが事前に分かるのであれば、
            # 止めてもOK」＝分からない文は流さない。⛔ G-63：判定の無いときの足場は、危ない文を 82文中18文通した）
            problem = UNCHECKED
        if problem and pick is None and not asked and (need := self._fallback_missing(kind, None)):
            # 使えない文で、使えないときの一言を選んでいない：選ばせる（書き直しには数えない。この1回だけ、相手をもう少し待たせる）
            return self._reject(f"この文は声に出しません（{problem}）。{need}")
        tried = tried + ([(answer, problem)] if problem else [])
        self.own_turn = self.callee_turns
        checked = {"checked": getattr(verdict, "by", None) or ("knowledge" if verdict is not None else None)}
        drafts = {"drafts": [{"text": t, "problem": p} for t, p in tried]} if tried else {}
        if problem and kind == "no_answer" and verdict == DEFERRED and (line := self._known_line_of(verdict.part)):
            # 知っていることで答えられる問いを、書き直しを使い切っても預かったまま：判定が答えのもとに挙げた知っていることの文をそのまま言う
            # （自分で答える質問の受け皿と同じ形。⛔ 2026-10-25 J9a：所在地を聞かれて「担当の者に申し伝えます」と答えた）
            kind, pick = "own", line
        if problem and asked and kind == "own" and pick is None:
            return self._asked_skip(problem, drafts, checked)   # 尋ねられた考えの答えが使えない：尋ねられる前の流れのとおりに進める
        if offer:
            return self._offer_again(None if problem else answer, pick if problem else None, drafts, checked, proposal, dropped)
        if problem:
            return self._own_fallback(kind, pick, problem, drafts, checked)
        # 続きの電話の始めのご説明（名乗りとお詫びの録音のすぐあと＝お客様はまだ話していない）：相づち「はい、」を付けない（quiet。受け皿の道と同じ）。
        # ⛔ 2026-11-06 日本時間5時56分55秒のご質問のあとの読み直しで見つけた：自分の言葉でご説明すると「はい、前回お伺いした料金の件ですが、…」になった
        lead = self._resumed_explaining()
        self._await_reply(kind)
        self._point_said(kind)
        if kind == "talk":
            self._note("talk", answer=answer, **drafts, **checked)
        else:
            self._question_note({"answered": kind == "own", "answer": answer, **drafts, **checked})
        return {"result": "own_answer", "say": answer, "message": f"{say_note(answer)}{self._own_then()}"} | ({"quiet": True} if lead else {})

    def _offer_again(self, said: str | None, pick: str | None, drafts: dict, checked: dict, proposal: dict, dropped: tuple | None = None) -> dict:
        """申し出（詳しい者より改めて N4・締める前の申し出 N6）への会話が繋がらないお返事に応える唯一の返事（_own_answer から。番は _asked_kind の offer＝
        _offer_unlinked）：(1) 会話AIのひとこと（said＝受け答えと同じ確かめを通った自分の言葉。使えなければ pick＝受け答えの録音 TALK_FALLBACK）、(2) 申し出を
        「それでは（条件、）（申し出の中身）ということでよろしいでしょうか？」で伺い直す（_ask_confirm の offer_again。値の入る言い直しと同じく会話AIが声に出す）。
        条件（terms）は申し出の通話の事実に残す（依頼者へ渡す）。伺い直しは1項目1回（Item.offer_again）で、このあとは聞き取れない・はっきりしないお返事にも
        伺い直さない（聞き直しの回数を使い切った扱い＝_ask_again・伺い直しへのお返事がそろわない＝_confirm_again）。
        ✅ 2026-11-09 日本時間7時57分50秒 ユーザー（お言葉は _offer_unlinked）"""
        qid = self.current
        q, it = self.questions[qid], self.items[qid]
        self.own_turn = self.callee_turns
        self._mark_answered()
        fb = TALK_FALLBACK[pick][0] if said is None else None
        self._note("talk", answer=said or fb, **({"fallback": pick} if said is None else {}), **drafts, **checked,
                   **({"terms_dropped": {"text": dropped[0], "problem": dropped[1]}} if dropped else {}))
        it.offer_again = True
        it.offer_terms, it.offer_added, it.offer_topic, it.offer_way = (proposal[k] for k in PROPOSAL_ARGS)
        if q.offered is None and it.offer_way is None:
            # 方法を選んでいただく質問（N5・C11・W1）の提案には、必ず業務の方法を入れる。会話AIの方法が使えなかったら（書き直しを使い切った・中身を確かめられ
            # なかった）、業務設定で最初に並ぶ方法（並べて伺ったあとの「どれでも」と同じ決まり＝_wish_step）
            it.offer_way = self._proposal_of(qid)[1][0]
        it.ask_backs = max(it.ask_backs, 1)
        it.condition, it.value = self._offer_answer(qid), None
        self._proposal_note(qid)   # お客様の条件は、伺い直したときに通話の事実へ（受けていただいても断られても。こちらから足した条件は受けていただいたとき）
        r = self._ask_confirm(qid, after=self._offer_after(qid))
        again, how = r["say"], self._confirm_how(qid)
        if said is not None:
            return r | {"say": said + again, "message": f"{say_note(said + again)}相手の返事を待ってください。{how}"}
        if self.play_recordings:
            return r | {"play": [{"kind": "recorded", "id": pick, "text": fb}], "speak": True,
                        "message": f"{played_note(fb)}続けて、{say_note(again)}相手の返事を待ってください。{how}"}
        return r | {"say": fb + again, "message": f"{say_note(fb + again)}相手の返事を待ってください。{how}"}

    def _proposal_checked(self, proposal: dict, given: list, qid: str | None, offering: bool) -> tuple:
        """会話AIが書いた提案の言葉（terms・topic・way）を声に出す前に確かめる唯一の場所（申し出へのお話にひとこと応える _own_answer・条件つきで受けて
        いただいた答えの報告 _terms_reported）：(差し戻しの返事か None, 使う提案, 声に出さなかった言葉と理由か None)。通らなければ書き直させ（自分の言葉と
        同じ回数＝同じ発話の書き直しの数え own_tried を一緒に使う）、使い切った・中身を確かめられなかった（判定は速くならない）なら、お客様の言ったことに
        依らない形（提案の言葉なし）で進める。offering＝ひとこと応える番の提案（_own_answer）：方法を選んでいただく質問の提案には方法が要る（書いていなければ
        書き直させる。使い切ったら業務設定で最初に並ぶ方法＝_offer_again）。"""
        need = offering and self.questions[qid].offered is None
        if (not given and not need) or (why := self._proposal_problem(proposal, qid, need)) is None:
            return None, proposal, None
        tried = self.own_tried[1] if self.own_tried[0] == self.callee_turns else []
        text = "／".join(proposal[k] for k in given)
        if why != UNCHECKED and len(tried) < OWN_REWRITES:
            self.own_tried = (self.callee_turns, tried + [(text, why)])
            return self._reject(f"申し出に添える言葉は声に出しません（{why}）。書き直して、もう一度呼んでください（無ければ書きません）。"), proposal, None
        return None, dict.fromkeys(PROPOSAL_ARGS), (text, why)

    def _terms_reported(self, qid: str, cond: str, a: dict) -> dict | None:
        """申し出（Question.offered）を受けていただいた答えの報告に添えた提案の言葉（ご都合の条件 terms・申し出に含める中身 topic・業務の方法 way）を受ける
        唯一の入口（_op_record_answer）。差し戻すときはその返事、受けたら None（受け取った答えのあと、言い切りの一言を _taken_on_terms が作る）：
        - 申し出を受けた答えでない・申し出の質問でない ⇒ 書かない（差し戻す）。方法は申し出のシステムの質問（N4・N6）だけ（ご案内の方法の申し出・業務の
          申し出の質問は、方法が質問そのもの＝別の方法はほかのご希望の道）
        - 業務に無いご連絡の方法 ⇒ こちらで応えられる条件ではない：受けた答えにせず、ひとこと応える番（_asked_kind の offer）にして「（方法）でのご希望も
          含めて…ということではいかがでしょうか？」の提案で伺う（(3) ア＝_offer_again）
        - ほかは伺い直しの提案と同じ確かめ（_proposal_checked）。
        ✅ 2026-11-12 日本時間13時40分05秒 ユーザー：「(2) お客様が、こちらで応えられる条件（業務の方法・ご都合の条件）つきで申し出を受けたら、伺い直さない。」"""
        proposal = {k: (a.get(k) or "").strip() or None for k in PROPOSAL_ARGS}
        given = [k for k in PROPOSAL_ARGS if proposal[k] is not None]
        self.taken_terms = None
        if not given:
            return None
        q, it = self.questions[qid], self.items[qid]
        if (accept := self._offer_answer(qid)) is None or cond != accept:
            return self._reject(f"「{ARG_LABEL[given[0]]}」は、申し出を受けていただいた答えにだけ書きます。外して、もう一度呼んでください。")
        if (w := proposal["way"]) is not None and (q.asks_way or q.callback in WAY_ROLES):
            return self._reject(f"「{ARG_LABEL['way']}」は、この質問には書きません（方法を伺う質問なので、別の方法は「{ARG_LABEL['rest']}」に、相手の言ったとおりに書きます）。")
        # 業務に無い方法つきでも、もう受けていただいた答えは伺い直さない（その方法はお約束せず、連絡先を伺って申し伝える＝_taken_on_terms・_handoff_way・
        # _offer_wish。提案で伺うのは会話が繋がらないお返事だけ＝_offer_unlinked の1か所。✅ 2026-11-12 まとめ役の (I1)）
        rejected, proposal, _ = self._proposal_checked(proposal, given, qid, False)
        if rejected is not None:
            return rejected
        if any(proposal.values()):
            self.taken_terms = (self.callee_turns, qid, proposal)
        return None

    def _terms_now(self, qid: str) -> bool:
        """この発話の答えの報告に、申し出を受けていただいた条件の言葉が添えてあるか（_terms_reported が受けたもの）。"""
        t = self.taken_terms
        return t is not None and t[0] == self.callee_turns and t[1] == qid

    def _taken_on_terms(self) -> str | None:
        """申し出を条件つきで受けていただいたときの言い切りの一言（唯一の場所＝次へ進む返事 _advance の頭）：受け取った答えが申し出を受けた答えなら、条件の
        言葉を項目と通話の事実に残し（申し出のシステムの質問は申し出の通話の事実＝方法はその段で連絡先へ・業務の質問は offer_terms）、「それでは…ますね。」
        （phrases.OFFER_TAKEN＝言い切っていない答えの一言と同じ文）を返す。次の質問・締めは、これを会話AIの声で先に言う（said_lead）。"""
        t = self.taken_terms
        if t is None:
            return None
        if t[0] != self.callee_turns:
            self.taken_terms = None
            return None
        _, qid, proposal = t
        q, it = self.questions[qid], self.items[qid]
        if not self._offer_taken(qid):
            return None
        self.taken_terms = None
        # 伺い直しに入れた条件（申し出・方法の提案の伺い直しを受けていただいたお返事）と合わせる（新しく言われたほうを使う）
        it.offer_terms, it.offer_added, it.offer_topic, it.offer_way = (proposal[k] or getattr(it, f"offer_{k}") for k in PROPOSAL_ARGS)
        # 言い切るのは、こちらで応えられる条件だけ（業務に無い方法はお約束しない＝言い切りに入れず、連絡先を伺って申し伝えるだけ。✅ 2026-11-12 まとめ役の (I1)）
        do, ways = self._proposal_of(qid)
        way = it.offer_way if it.offer_way in ways else None
        if not (it.offer_terms or it.offer_topic or way):
            return None
        if way:
            # 言い切りで方法をお伝えした＝申し伝える一言の1回として数える（11-07 3時14分01秒の (2) H3 と同じ1か所 told_said。その方法のまま終えるときだけ＝
            # _told_said）。手順がまだ無い業務の申し出の質問は、手順を始めるときに渡す（_offer_wish）。⛔✅ 2026-11-12 まとめ役の (J1)：「ありがとうございます。
            # それでは来週以降に、詳しい者がご訪問のうえお話しさせていただきますね。そのご希望を担当の者に申し伝えます。」と重ねていた
            it.offer_stated = way
            if (p := self._proc_of(qid)) is not None and p.get("told_said") is not True:
                p["told_said"] = way
        # 言い切りで言う条件は、お客様の言ったご都合だけ（こちらから足した条件は提案で言った。お客様が受けるときに言ったご都合は、それを確かめた言葉＝重ねると
        # 「夕方6時以降で、夕方以降に、…」になる。依頼者へは両方の欄で渡す＝_proposal_note）。⛔ 2026-11-12 日本時間14時58分17秒の「アで！」を入れる途中、
        # お返事の種類ごとの表で見つけた
        return OFFER_TAKEN.format(v=self._offer_words(do, it.offer_terms, it.offer_topic, way, polite=True))

    def _offer_wish(self) -> None:
        """業務の申し出の質問を、方法つきで受けていただいた（条件つきの受け・「（方法）でのご希望も含めて」の提案を受けていただいた）：その方法の連絡先を、
        ほかのご希望と同じ1か所で伺って申し伝える（_wish_means。11-03 20時48分の決まり）。申し出のシステムの質問（N4・N6）は、申し出の段の1か所
        （_handoff_step → _handoff_way）が同じことをする。唯一の場所＝次へ進む返事 _advance の頭。✅ 2026-11-12 まとめ役の (H2)"""
        for qid, it in self._received_now():
            q = self.questions[qid]
            if (q.callback not in OFFER_ROLES and it.state in GOT and it.condition == q.offered and it.offer_way is not None
                    and qid not in self.wished and (ms := means_in(it.offer_way))):
                self._wish_means(q, ms[0], told_said=it.offer_stated)

    def _offer_words(self, do: str, terms: str | None = None, topic: str | None = None, way: str | None = None, polite: bool = False) -> str:
        """申し出の言葉（phrases.offer_words）を作る流れの制御の唯一の入口（伺い直し・提案 _ask_confirm・条件つきの受けの言い切り _taken_on_terms）：方法を入れる
        言い方は、取込で決めたもの（業務設定 way_words）があればそれを渡す。✅ 2026-11-12 まとめ役の (1)"""
        return offer_words(do, terms, topic, way, polite, way_do=self.way_words.get(do, {}).get(way) if way else None)

    def _offer_ways_of(self, q: "Question") -> tuple:
        """申し出の質問 q で、こちらがお約束できる方法（業務の方法）の唯一の決まり：申し出のシステムの質問（N4・N6）は業務設定の申し出の方法（offer_ways）・
        業務の申し出の質問は業務設定のご案内できる方法（Question.offer_ways。無ければ無い＝どの方法も業務に無い方法＝お約束せず、ご希望として提案で伺い、
        受けていただいたら連絡先を伺って申し伝える）。ご案内の方法の申し出（asks_way）は方法そのものが質問なので、方法は書かない（_terms_reported）。
        ✅ 2026-11-12 まとめ役の (H2)"""
        return tuple(self.offer_ways) if q.callback in OFFER_ROLES else tuple(q.offer_ways)

    def _offer_parts(self, qid: str) -> tuple:
        """申し出の言葉（伺い直し・条件つきの受けの言い切り＝offer_words）に入れる (条件, 中身, 方法) を決める唯一の場所：業務に無いご連絡の方法（ご希望の
        方法）は、申し出の中身「（方法）でのご希望も含めて」にする（こちらがすることの中には入れない＝お約束しない）。✅ 2026-11-12 日本時間13時40分05秒 ユーザー「(3) (ア) はア」"""
        it = self.items[qid]
        # 条件は、お客様の言ったご都合のあとに、こちらから足したご都合（「月曜日を避けて、夕方以降に、…」）
        terms = "、".join(t for t in (it.offer_terms, it.offer_added) if t) or None
        if (w := it.offer_way) is not None and w not in self._proposal_of(qid)[1]:
            return terms, (it.offer_topic or "") + WISH_WAY_TOPIC.format(way=w), None
        return terms, it.offer_topic, it.offer_way

    def _proposal_of(self, qid: str) -> "tuple[str, tuple] | None":
        """会話が繋がらないお返事のあとに、提案で伺い直せる質問なら (こちらがすることの言葉, お約束できる方法)、違えば None（唯一の決まり＝応える番
        _offer_unlinked・伺い直し _offer_again・その言葉 _ask_confirm・言葉の部品 _offer_parts・提案の言葉の確かめ _proposal_problem・条件つきの受け
        _taken_on_terms）：
        - 申し出の質問（Question.offered）：受ける答えのこちらがすることの言葉と、業務の方法（_offer_ways_of）
        - 方法を選んでいただく質問（WAY_ROLES）：その手順（_way_proc）の言葉と方法。詳しい者からのお話の方法 N5・その LINE の伺い方 C11 は、申し出（N4・N6）の
          こちらがすることの言葉と業務設定の申し出の方法。ほかのご希望 W1・その伺い方 C11 は「ご案内させていただきます」（phrases.WISH_DO）と、ご案内できる
          方法（_offered_ways＝断られた方法でないもの）。LINE の伺い方は LINE を除く（LINE をやめる提案）。提案できる方法が無ければ None（今までどおり聞き直す）。
        ✅ 2026-11-12 日本時間14時58分17秒 ユーザー「アで！」"""
        q = self.questions[qid]
        if q.offered is not None:
            do = q.do_words.get(q.offered)
            return (do, self._offer_ways_of(q)) if do else None
        p = self._way_proc(q)
        if p is None:
            return None
        if p is self.handoff:
            oq = self.questions[CALLBACK_IDS[p["kind"]]]
            do, ways = oq.do_words.get(oq.offered or ""), tuple(self.offer_ways)
        else:
            # ほかのご希望の手順：申し出を方法つきで受けていただいた業務の申し出の質問（_offer_wish）なら、その申し出の言葉（「パンフレットをメールでお送り…」）。
            # ほかのご希望を伺った（W1）なら「ご案内させていただきます」
            oq = self.questions[p["q"]]
            taken = oq.offered is not None and self._offer_taken(oq.id)
            do, ways = (oq.do_words.get(oq.offered) if taken else WISH_DO), tuple(_offered_ways(oq))
        if q.callback == "line_how":
            ways = tuple(w for w in ways if "line" not in means_in(w))
        return (do, ways) if do and ways else None

    def _way_proc(self, q: "Question") -> dict | None:
        """方法を選んでいただく質問 q の手順の段の進み（詳しい者からのお話 self.handoff・ほかのご希望 self.wish）を決める唯一の場所。LINE の伺い方 C11 は、
        いま伺い方を伺っている手順。方法を選んでいただく質問でなければ None。"""
        if q.callback == "handoff_way":
            return self.handoff
        if q.callback == "other_wish":
            return self.wish
        if q.callback == "line_how":
            return next((p for p in (self.handoff, self.wish) if p is not None and p.get("detail") == q.id), None)
        return None

    def _offer_answer(self, qid: str) -> str | None:
        """「申し出・提案を受けていただいた」にあたる答え（唯一の決まり＝_offer_taken・伺い直し _offer_again・条件つきの受け _terms_reported）：申し出の質問は
        Question.offered。方法を選んでいただく質問は、提案で伺い直したときだけ、その方法の答え（N5＝方法の名前・W1＝その方法のご希望の選択肢＝wish_option・
        C11＝LINE をやめてその方法にする印 WAY_SWITCH）。ほかは None。"""
        q, it = self.questions[qid], self.items[qid]
        if q.offered is not None:
            return q.offered
        if not (it.offer_again and it.offer_way) or q.callback not in WAY_ROLES:
            return None
        if q.callback == "handoff_way":
            return it.offer_way
        if q.callback == "other_wish":
            return wish_option(self.questions[q.of], means_in(it.offer_way)[0])
        return WAY_SWITCH.format(way=it.offer_way)

    def _offer_taken(self, qid: str) -> bool:
        """申し出・提案を受けていただいたか（受け取った答えが _offer_answer。唯一の決まり＝お礼の相づち _ack_choice・言い切り _taken_on_terms・伺い方を替える
        _line_how）。"""
        a, it = self._offer_answer(qid), self.items[qid]
        return a is not None and it.state in GOT and it.condition == a

    def _offer_after(self, qid: str) -> str:
        """提案の伺い直しの終わりの唯一の決まり（_offer_again）：こちらから足したもの（条件 added・中身 topic＝業務に無い方法の「（方法）でのご希望も含めて」も・
        方法を選んでいただく質問の提案の方法）がある提案は「…ということではいかがでしょうか？」（offer_alt）、お客様の言ったことだけの提案は「…ということで
        よろしいでしょうか？」（offer_again）。✅ 2026-11-12 日本時間13時40分05秒の (1)・14時58分17秒「アで！」"""
        ours = self.items[qid].offer_added or self._offer_parts(qid)[1] or self.questions[qid].offered is None
        return "offer_alt" if ours else "offer_again"

    def _proposal_terms(self, qid: str) -> dict:
        """質問 qid の伺い直しの提案に入れた条件と中身のうち、いま通話の事実に残すもの（欄 PROPOSAL_TERMS の唯一の形）：お客様の言葉にある条件（terms）と、
        お客様が気にかけていることに応える中身（topic）は、受けていただいても断られても残す（お客様のご事情＝依頼者に役立つ）。こちらから足した条件（added）は、
        受けていただいたとき（_offer_taken）だけ（断られたら、こちらの提案にすぎない）。✅ 2026-11-12 まとめ役の (J2)"""
        it = self.items[qid]
        return {"terms": it.offer_terms, "added": it.offer_added if self._offer_taken(qid) else None, "topic": it.offer_topic}

    def _proposal_note(self, qid: str) -> None:
        """提案の条件と中身を通話の事実に残す唯一の場所（伺い直したとき _offer_again・受けていただいたとき _proposal_taken。何を残すかは _proposal_terms の
        1か所＝お客様の条件は伺い直したときから・こちらから足した条件は受けていただいたときだけ）。どの手順でも同じ：申し出の手順（N4・N6・その方法 N5・
        伺い方 C11）は申し出の通話の事実（handoff・close_offer）・ほかのご希望の手順（W1・その伺い方 C11）は other_wish（W1 の答えの前でまだ無ければ、
        答えを受けるときに _wish_step が同じ形で書く）・業務の申し出の質問は offer_terms（1つ）。✅ 2026-11-12 まとめ役の (J2)"""
        q = self.questions[qid]
        terms = self._proposal_terms(qid)
        p = self._way_proc(q) if q.callback in WAY_ROLES else None
        if q.callback in OFFER_ROLES or (p is not None and p is self.handoff):
            self._handoff_fact(**terms)
        elif p is not None:
            e = next((e for e in reversed(self.facts.get("other_wish", [])) if e["question"] == p["q"]), None)
            if e is not None:
                e.update(terms)
        elif q.callback not in WAY_ROLES and any(terms.values()):
            # 同じ質問に2つ作らない（受けていただいた答えを、あとで決め直しても1つ）
            e = next((e for e in self.facts.get("offer_terms", []) if e.get("question") == qid), None)
            if e is not None:
                e.update(self._proposal_terms(qid))
            else:
                self._note("offer_terms", question=qid, **self._proposal_terms(qid))

    def _proposal_taken(self) -> None:
        """申し出・提案を受けていただいた（_offer_taken）とき、伺い直しに入れた条件（お客様の条件・こちらから足した条件・中身）を必ず通話の事実に残す唯一の
        場所（次へ進む返事 _advance の頭。条件つきで受けていただいた言葉は、その前に _taken_on_terms が項目に合わせてある）。「はい」だけで受けていただいても残す。
        ⛔ 2026-11-12（961bd8d4 の報告の穴 (4)・まとめ役の (2)）：業務の申し出の質問は、お客様が条件を言い直したときだけ offer_terms に書き、伺い直しに入れた
        条件を「はい」だけで受けていただくと、依頼者へ渡らなかった"""
        for qid, _ in self._received_now():
            if self._offer_taken(qid) and any(self._proposal_terms(qid).values()):
                self._proposal_note(qid)

    def _way_switch(self, proc: dict, way: str) -> None:
        """LINE の伺い方（C11）への会話が繋がらないお返事のあとの、LINE でない方法の提案を受けていただいた（唯一の場所＝_line_how）：ご連絡の方法をその方法に
        決め直す。伺い方の段を外し（方法を言っていないときの伺い直し・ほかの方法の伺い直しの印も消す）、方法を選んでいただいた答え（N5・W1）をその方法に決め直して
        （_retake）、その方法の連絡先を、方法が決まったときと同じ1か所で伺う（詳しい者からのお話＝_handoff_way・ほかのご希望＝_wish_contact。お電話など連絡先の
        無い方法は申し伝える）。✅ 2026-11-12 日本時間14時58分17秒 ユーザー「アで！」"""
        hid = CALLBACK_IDS["line_how"]
        self._out_of_order(hid)
        for k in ("how_again", "without"):
            proc.pop(k, None)
        # 段は伺い方の前へ戻す（終えた手順を、締めの前の念のための確認で開き直さない＝_contact_reopen が開くのは連絡先の段だけ）
        proc.update(stage="way" if proc is self.handoff else "ask", detail=None)
        self._wish_via(None, proc)
        why = f"LINE の伺い方への繋がらないお返事のあとの提案（{way}）を受けていただいた"
        if proc is self.handoff:
            if (nid := CALLBACK_IDS["handoff_way"]) in self.items:
                self._retake(nid, way, hid, why)
            return self._handoff_way(way)
        q, wid, means = self.questions[proc["q"]], CALLBACK_IDS["other_wish"], means_in(way)[0]
        if wid in self.items:
            self._retake(wid, wish_option(q, means), hid, why)
        proc["means"] = means
        e = next((e for e in reversed(self.facts.get("other_wish", [])) if e["question"] == q.id), None)
        if e is not None:
            e.update(wish=wish_option(q, means), means=means, **self._proposal_terms(hid))
        if (role := WISH_MEANS[means][2]) is None:
            return self._wish_end("means", None)
        self._wish_contact(role, after=wid if wid in self.items else q.id)

    def _told_said(self, proc: dict) -> bool:
        """その手順（詳しい者からのお話 self.handoff・ほかのご希望 self.wish）で、申し伝える一言にあたる一言をもう言ったか（唯一の決まり＝手順の終わり
        _handoff_end・_wish_end）：told_said が真（申し伝える一言を言った＝連絡先の段を開き直したとき・並べて伺ったあとの「どれでも」にこちらで方法をお伝え
        した一言＝11-07 3時14分01秒の (2) H3）か、方法の名前（条件つきの受けの言い切りでお伝えした方法＝_taken_on_terms）でいまの方法と同じ。あとで方法が
        替わった（C11 で LINE をやめた）・決まらなかったら、言い切りはいまの方法を伝えていない＝申し伝える一言を言う。✅ 2026-11-12 まとめ役の (J1)"""
        t = proc.get("told_said")
        if not isinstance(t, str):
            return bool(t)
        if proc is self.handoff:
            return t == proc.get("way")
        ms = means_in(t)
        return len(ms) == 1 and ms[0] == proc.get("means")

    def _proc_of(self, qid: str) -> dict | None:
        """質問 qid がどの手順の段のものか（唯一の決まり＝言い切りを申し伝える一言の1回として数える _taken_on_terms）：申し出（N4・N6）は詳しい者からのお話の
        手順・方法を選んでいただく質問は _way_proc・業務の申し出の質問は、その質問のほかのご希望の手順（まだ無ければ None＝_offer_wish が始めるときに渡す）。"""
        q = self.questions[qid]
        if q.callback in OFFER_ROLES:
            return self.handoff
        if q.callback in WAY_ROLES:
            return self._way_proc(q)
        return self.wish if self.wish is not None and self.wish["q"] == qid else None

    def _way_unsaid(self, proc: dict) -> None:
        """LINE の伺い方（C11）への会話が繋がらないお返事（「LINEってあんまり見ないんですよね。」）のあとの、LINE でない方法の提案を受けていただけなかった
        （いいえ・また繋がらない・聞き取れない・方法を言っていない）ときの唯一の場所（_line_how）：お客様は LINE はあまり見ないとおっしゃったので、LINE の
        ご希望として申し伝えない。ご連絡の方法を決めずに、方法を言っていただけないときと同じ申し伝える一言（WISH_TOLD の way「ご案内の方法の件は…」）で続け、
        方法の答え（N5・W1）も方法を決めない（はっきりしない。伺い直しは1回＝並びから外して締めの前にも伺わない）。通話の事実の方法・伺い方も空にする。
        ⛔ 2026-11-12（まとめ役の (3)）：LINE の伺い方の「はっきりしない」の道（連絡先は無いまま LINE のご希望を申し伝える）に入り、「LINEでのご連絡のご希望を
        担当の者に申し伝えます。」と言っていた（お客様の言葉を受けていない）"""
        hid = CALLBACK_IDS["line_how"]
        for k in ("how_again", "without"):
            proc.pop(k, None)
        proc.update(stage="way" if proc is self.handoff else "ask", detail=None)
        self._wish_via(None, proc)
        why = "LINE の伺い方への繋がらないお返事のあとの提案を受けていただけなかった（ご連絡の方法は決めずに申し伝える）"
        mid = CALLBACK_IDS["handoff_way" if proc is self.handoff else "other_wish"]
        # 方法の答え（N5・W1）と、伺い方の答え（提案の印「（方法）に替える」・「方法は言っていない」）は、方法を決めない（「わからない」「答えたくない」は
        # お客様の答えのまま）。方法の答えは並びから外す（締めの前の念のための確認で伺わない）
        for xid in (mid, hid):
            if (x := self.items.get(xid)) is not None and x.state in GOT + ("unresolved",):
                x.condition, x.value = None, None
                x.set("unresolved", why)
        self._out_of_order(mid)
        if proc is self.handoff:
            proc["way"] = proc["means"] = None
            self._handoff_fact(way=None, means=None)
            return self._handoff_end("agreed")
        q = self.questions[proc["q"]]
        proc["means"], proc["told_as"] = None, "way"
        e = next((e for e in reversed(self.facts.get("other_wish", [])) if e["question"] == q.id), None)
        if e is not None:
            e.update(wish=WISH_OTHER, value=None, means=None)
        self._wish_end("other")

    def _proposal_problem(self, proposal: dict, qid: str, need_way: bool = False) -> str | None:
        """申し出の伺い直しに入れる、会話AIが書いた提案の言葉（ご都合の条件 terms・申し出に含める中身 topic・ご希望の方法 way）を声に出す前に確かめる唯一の場所
        （_own_answer）。問題が無ければ None、中身を確かめられなかったら UNCHECKED、ほかは理由。方法は業務の方法（offer_ways）の名前のときだけ。条件と中身は、文の形
        （offer_terms_problem）と、会話AIの自分の言葉と全く同じ中身の判定（grounding_jobs → on_grounded。業務の情報にある事実は言ってよい・知らない事実は止める）。
        ✅ 2026-11-09 日本時間9時48分09秒 ユーザー：「・(2) お客様：お金がかかるならいいです。今：さようでございますか。それでは詳しい者よりお話しさせていただくという
        ことでよろしいでしょうか？⇒今の返事は、お金がかかるならいいというお客様のお言葉を無視している。「かしこまりました。それではお金がかからないで利用できる範囲と
        お金がかかるのはどこからなのかを詳しい者よりお話しさせていただくということでもよろしいでしょうか？」のような感じで、お客様の要望を解決する提案を差し上げないと
        いけないのではないでしょうか？」「・(3) お客様：うちはオンラインしか無理なんですよ。：こちら：さようでございますか。それでは詳しい者よりオンラインでお話しさせて
        いただくということでよろしいでしょうか？：お客様がオンラインでと言われているのに、こちらがオンラインでということを無視していては、お客様にこちらの提案が
        伝わらないですよ。」⛔ 4fd9d79d まで、条件は「事実を言っていない」ものだけを通す別の決まりで（自分の言葉とは別の決まり）、料金・方法を受けられなかった。"""
        from grounding import UNGROUNDED
        ways = self._proposal_of(qid)[1]
        w = proposal["way"]
        if need_way and w not in ways:
            # 方法を選んでいただく質問（N5・C11・W1）の提案の方法は、その質問で業務がお約束できる方法だけ（業務に無い方法・断られた方法・LINE の伺い方の LINE は
            # 提案しない＝_proposal_of の1か所）。✅ 2026-11-12 日本時間14時58分17秒 ユーザー「アで！」
            return (f"{ARG_LABEL['way']}{'「' + w + '」は、ご提案できる方法ではありません' if w else 'がありません'}。相手のお話に合う方法を、業務の方法"
                    f"（{'・'.join(ways)}）から1つ選んで、その名前をそのまま書きます")
        if w is not None and w not in ways and not means_in(w):
            return (f"{ARG_LABEL['way']}「{w}」は業務の方法（{'・'.join(ways) or 'なし'}）でも、ご連絡の方法でもありません。書きません"
                    f"（「{ARG_LABEL['rest']}」に、相手の言ったとおりに書きます）")
        for k in ("terms", "added", "topic"):
            if (t := proposal[k]) is None:
                continue
            if (why := offer_terms_problem(t, self.names, self.name_readings, k)) is not None:
                return f"{ARG_LABEL[k]}「{t}」：{why}"
            v = self._grounded(t)
            if v is None:
                return UNCHECKED
            if v == UNGROUNDED:
                return (f"{ARG_LABEL[k]}「{t}」：こちらについて知らない事実（「{getattr(v, 'part', '') or t}」）が入っています。業務の情報にあることだけで書きます"
                        "（例：料金の決まりが業務の情報に無ければ「料金の面も含めて」）")
            if (why := own_answer_problem(t, self.names, False, verdict=v, name_readings=self.name_readings)) is not None:
                return f"{ARG_LABEL[k]}「{t}」：{why}"
        return None

    def _question_note(self, entry: dict) -> None:
        """相手の質問の記録（callee_question）に、答えを書く（答えに添えられた質問は、もう記録してある＝_advance。同じ発話の記録に書き足す）。唯一の書き手。
        本題で本心を伺った一言に言っていただいた気になる点（concern）に応えた文も、ここで書く。
        迷っているお客様に選ぶ助けを伝えた言葉（_hesitation の help）は問いではないので残さない（依頼者へ渡すお客様の質問にしない。⛔ 2026-11-08 日本時間
        0時台（11-07 23時26分13秒のご質問の見直しの H-11）：自分の言葉の答え・声で答えた印の所が、「カードか振込か迷っています。」を残していた）"""
        if self._hesitation() == "help":
            return
        # 本題で本心を伺った一言に言っていただいた気になる点（_concern_decided）は、お客様の質問ではない＝その事実 concern に書き足す
        kind = "concern" if self.concern_turn == self.callee_turns else "callee_question"
        same = [x for x in self.facts.get(kind, []) if x["turn"] == self.callee_turns]
        if same:
            same[-1].update(entry)
        else:
            self._note(kind, **entry)

    def _own_fallback(self, kind: str, pick: str | None, problem: str | None, drafts: dict, checked: dict) -> dict:
        """答えの文を使わずに、受け皿で応える返事（唯一の場所。_own_answer）：受け答え＝AIが選んだ一言の録音（phrases.TALK_FALLBACK。こちらに
        ついての事実を言わない決まった言い方）／自分で答える質問＝AIが選んだ、答えのもとにした知っていることの文そのまま／答えを持っていない
        質問＝断らない一言の録音（phrases.OWN_FALLBACK）。どれも確かめなくてよい。そのあと相手の返事を待つ（own_pending）。
        problem＝答えの文が使えなかった理由（None＝AIが一言だけを選んだ）。"""
        p = self.point
        # ご説明する段の、ご説明できないときの一言（_point_fallback の1か所。本題で本心を伺った一言に言っていただいた気になる点＝_asked_kind の concern にも同じ
        # 一言で申し伝える）と、続きの電話の始めのご説明か（_point_said で記録を作ると読めなくなるので先に読む）
        point_fb = self._point_fallback(pick) if self._explaining() or self._asked_kind() == "concern" else None
        lead = self._resumed_explaining()
        self._await_reply(kind)
        self._point_said(kind)
        if point_fb is not None and (e := self._point_fact()) is not None:
            e["told"] = pick if pick in POINT_KINDS else "question"   # 申し伝える一言の種類（このあと切れたら、続きの電話で同じ種類の件を申し伝え直す＝_point_told）
        why = f"答えの文は使いません（{problem}）。かわりに" if problem else ""
        if kind == "talk":
            said = TALK_FALLBACK[pick][0]
            play = {"kind": "recorded", "id": pick, "text": said}
            self._note("talk", answer=said, fallback=pick, **drafts, **checked)
        else:
            # ご納得いただけなかった点をご説明できない（お客様の言葉は質問ではない）：「その点は私では正確にご説明できませんので…」。続きの電話でのご説明（この電話
            # では点を伺っていない）は「前回お伺いした点は…」（「その点」が何も指さない）。続きの電話の始めの自分で答える受け皿（知っていることの文そのまま）は、
            # 録音「前回お伺いした件ですが、」のあとに言う（話し始めの言葉は、その点の短い言葉を会話AIが作るので受け皿では言えない）。⛔ 2026-11-05 日本時間
            # 2時05分37秒のご質問のあとの読み直しで見つけた：名乗りとお詫びのすぐあとに話し始めの言葉の無い文・何も指さない「その点は私では…」が流れた
            fb, fid = point_fb or (OWN_FALLBACK, "own_fallback")
            said, play = (pick, None) if kind == "own" else (fb, {"kind": "recorded", "id": fid, "text": fb})
            self._question_note({"answered": kind == "own", "answer": (RESUMED_FALLBACK_LEAD if lead and kind == "own" else "") + said, **drafts, **checked})
            if lead and kind == "own":
                pre = {"kind": "recorded", "id": "resumed_fallback_lead", "text": RESUMED_FALLBACK_LEAD}
                if self.play_recordings:
                    return {"result": "own_answer", "fallback": True, "play": [pre], "say": said, "speak": True, "quiet": True,
                            "message": f"{why}{played_note(RESUMED_FALLBACK_LEAD)}続けて、{say_note(said)}{self._own_then()}"}
                return {"result": "own_answer", "fallback": True, "say": RESUMED_FALLBACK_LEAD + said, "quiet": True,
                        "message": f"{why}{say_note(RESUMED_FALLBACK_LEAD + said)}{self._own_then()}"}
        if play is not None and self.play_recordings:
            return {"result": "own_answer", "fallback": True, "play": [play],
                    "message": f"{why}{played_note(said)}何も言わずに、{self._own_then()}"}
        return {"result": "own_answer", "fallback": True, "say": said,
                "message": f"{why}{say_note(said)}{self._own_then()}"}

    def _approval_explanation(self) -> str | None:
        """いまのご承認の手順の、ご承認いただく内容の説明の文（approval_explanation の1か所）。"""
        return approval_explanation(self.questions.get(self.point["q"]) if self.point else None)

    def _point_fallback(self, pick: str | None) -> tuple[str, str]:
        """ご説明する段で、ご説明できないときの一言（文, 録音の名前）の唯一の決まり（_own_fallback）：お客様の言ったことの種類（ご質問・ご要望・ご意見＝
        pick。会話AIが答えの文と一緒に選ぶ。選んでいなければご質問）で名前と返し方をそろえる（✅ 2026-11-05 日本時間3時33分11秒 ユーザーのご修正）。
        続きの電話の始めのご説明（この電話では点を伺っていない）は「前回お伺いした…」、この電話で伺った点は「その…」。続きの電話で点を言っていただけ
        なかったとき（お客様は点を言っていない）は、ご説明の段に入らないので、この一言は流れない（振り返りの一言＝_point_step の recap。✅ 2026-11-05
        日本時間3時49分00秒 ユーザー「…おかしくないですか？」・4時16分52秒「…人ではありえないような対応です。」）。"""
        kind, again = pick if pick in POINT_KINDS else "question", self._resumed_explaining()   # 前の電話で伺った点のご説明か（この電話で伺った点は「その…」）
        return point_fallback(kind, again), point_fallback_id(kind, again)

    def _may_follow_up(self, part: str | None) -> bool:
        """答えの文に「担当から改めてご連絡」を入れてよいか（業務設定で許された、答えを持っていない質問だけ。_own_answer・grounding_jobs の1か所）。"""
        return part == "no_answer" and bool(self.policies["follow_up"])

    def _own_then(self) -> str:
        return f"そのあと相手の返事を待ちます。{self._answer_in_reply()}返事があったら、{self._return_note()}"

    def _answer_in_reply(self) -> str:
        """相手の質問・お話に応えたあと、相手の次の言葉が、まだ待っている答えになりうるときの知らせ（_own_then・_now の1か所。なりえなければ空）：
        言い直しへの返事を待っているなら、言い直しへの返事として報告する／答えの段で尋ねられた問い（ASKED_REPLY）に答えたなら、いまの質問への答えとして
        報告する（どちらも質問に戻るのではない）。⛔ 2026-11-06 日本時間4時23分33秒のご指摘を入れる途中、作りを読んで見つけた：言い直しへの返事を待つ間に
        相手の問いに答えたあとの知らせは「質問に戻る」だけで、相手の「じゃあ、お願いします。」を会話AIが答えの報告（record_answer）で出すと差し戻され
        （言い直しの返事待ちの間は受け付けない）、相手を1往復待たせる形だった。⛔ 5時56分55秒のご質問のあとの読み直しで見つけた：いまの質問について
        尋ねられて答えたあとも「質問に戻る」だけで、そのとおりに動くと、答えたお客様に質問を流し直した"""
        if self.awaiting_confirm is not None:
            return f"いまも、言い直し「{self.awaiting_say}」への返事を待っています。相手の言葉がその返事なら、{self._confirm_how(self.awaiting_confirm)}"
        if self.own_pending == ASKED_REPLY and self.current is not None:
            q = self.questions[self.current]
            return (f"相手の返事が、いまの質問 {q.id}「{q.text}」への答えなら、質問には戻らずに、何も言わずに record_answer で、聞いた答えの"
                    "とおりに報告します。" + choices_note(q))
        return ""

    def _grounded(self, answer: str):
        """この相手の発話で、電話接続側が渡した、この文の中身の判定の読み（無ければ None＝判定を使わない通話・待つ長さに届かなかった）。"""
        turn, got = self.grounded
        return got.get(answer.strip()) if turn == self.callee_turns else None

    def grounding_jobs(self, name: str, args: dict | None = None) -> list[tuple[list[str], str, str]]:
        """電話接続側が、この操作を流れの制御に渡す前に、中身の判定（grounding）に聞く文の並び（唯一の場所。無ければ空）：(知っていること, お客様の言葉,
        確かめる文) の並び。電話接続側は、どれも同時に出して、届いた読みを1つずつ on_grounded へ渡す。
        - 会話AIが自分の言葉で言う文（答えの文）。機械の確かめで止まる文・知っていることの文をそのまま言った文・もう答えた発話は聞かない（判定の分の待ちを
          無駄にしない）。
        - 申し出へのお話にひとこと応える受け答えの提案の言葉（ご都合の条件 terms・申し出に含める中身 topic＝_offer_again の伺い直しに入れる言葉）。自分の言葉と
          全く同じ決まりで確かめる（_proposal_problem。✅ 2026-11-09 日本時間9時48分09秒＝「事実を言っていない」だけを通す別の決まりは外した）。形の合わない
          条件（offer_terms_problem）は聞かない。⛔ 2026-11-09 日本時間8時台（7時57分50秒のご判断を入れたあとの見直し）：条件の文字は形しか確かめず、
          「無料で」「明日までに」「オンラインで」も声に出しうる作りだった（許されていない約束・業務の方法に無い方法）"""
        a = self._args_in(dict(args or {}))
        if name in ("record_answer", "confirm_field"):   # 申し出を条件つきで受けていただいた答え・お返事の報告に添えた言葉（_terms_reported）
            return [(self.knowledge, self._answered_words(), t) for k in ("terms", "topic")
                    if (t := (a.get(k) or "").strip()) and self._grounded(t) is None and offer_terms_problem(t, self.names, self.name_readings, k) is None]
        if name != "replay_opening" or a.get("part") not in OWN_PARTS or self.own_turn == self.callee_turns:
            return []
        from grounding import verbatim
        jobs, answer = [], (a.get("answer") or "").strip()
        # 機械の確かめで止まる文（名前・断り・許されていない約束・読みで渡した地名の漢字・知らせの言葉・長すぎる）は、判定に聞かずに書き直させる
        # （⛔ 2026-10-23 G-57 tc40：約束を見ていなかったので、約束で止まる文まで判定を待ち、相手を約1.5秒余計に待たせた）
        if (answer and not verbatim(answer, self.knowledge) and self._grounded(answer) is None
                and own_answer_problem(answer, self.names, may_follow_up=self._may_follow_up(a.get("part")), name_readings=self.name_readings) is None):
            jobs.append((self.knowledge, self._answered_words(), answer))
        for k in ("terms", "added", "topic"):
            # 申し出の伺い直しに入れる提案の言葉（ご都合の条件・こちらから足した条件・申し出に含める中身）も、自分の言葉と同じ中身の判定に聞く（形の合わない言葉は聞かない）
            t = (a.get(k) or "").strip()
            if t and a.get("part") == "talk" and self._grounded(t) is None and offer_terms_problem(t, self.names, self.name_readings, k) is None:
                jobs.append((self.knowledge, self._answered_words(), t))
        return jobs

    def _answered_words(self) -> str:
        """AIの自分の言葉の文が応えるお客様の言葉（中身の判定へ渡す1か所＝grounding_jobs）：いまの発話。続きの電話の始めのご説明（前の電話で伺った点を、
        点を伺い直さずにご説明する＝_point_begin の said）は、その点のお客様の言葉（第一声ではない）。"""
        if self._resumed_explaining():
            return self.point["resumed"]
        return self.callee_said or ""

    def on_grounded(self, answer: str, verdict) -> None:
        """答えの文の中身の判定の読みが届いた（唯一の入口。電話接続側が、操作を渡す前に呼ぶ）。届かなかったときは呼ばない。"""
        turn, got = self.grounded
        if turn != self.callee_turns:
            got = {}
        got[answer.strip()] = verdict
        self.grounded = (self.callee_turns, got)

    def known_lines(self) -> list[str]:
        """自分で答える質問の受け皿に使える、知っていることの文（業務の情報・想定問答の答え。名前の読みに置き換えた、声に出す形）。AIは、答えのもとに
        した文を、答えの文と一緒に「使えないときの一言」で選ぶ（操作の宣言＝tool_declarations の known）。"""
        return [k.split("：", 1)[1] for k in self.knowledge if k.startswith(tuple(KNOWN_HEADS.values()))]

    def _opening_answer(self, part: str | None) -> bool:
        """呼んでいる段で、相手の言葉に名乗り・用件の録音で答える報告か（replay_opening の名乗り・用件・最初から）：何を聞かれたかの読みで当てる
        （_asked_other）報告の唯一の決まり。読みが届くまで報告を待たせる（wants_checks。先に届いた1つで決める）。
        ⛔ 2026-10-25 J12b（番号の出どころを聞かれる・3つの声とも）：AIは相手の言葉の終わりから約0.3秒で用件の録音を選び、何を聞かれたかの読みは
           1.4秒と2.8秒に届いた＝待たずに受けていたので、当てる所（J10b の直し）が一度も効かず、用件の録音が流れた。G-59 asks4 の記録で、
           どちらの判定も1つだけで、ほかの質問12個を12個とも差し戻し、名乗り・用件の質問15個・質問でない言葉210回を1つも差し戻さない"""
        return self.phase == "calling" and part in ("intro", "purpose", "all", "target")

    def _asked_other(self) -> dict | None:
        """名乗り・用件の録音で答えようとした（_opening_answer）が、いまの言葉の尋ねているかの読みに「ほかのことを尋ねている」が
        あり、「名乗り・用件…を尋ねている」が1つも無い：受けずに、答え方を選び直させる。差し戻したあとも同じ発話にまた名乗り・用件の録音を
        選び、読みがそのままなら、AIに選ばせず、答えを持っていない質問の受け皿（断らない一言の録音＝_own_fallback）で応える（形に合わない
        ときの流れは制御が決める＝DESIGN.md の G-34 の決まり）。読みが分かれた（名乗り・用件の読みもある）・届いていないときは、AIの選びのまま。
        ⛔ 2026-10-25 J10b（番号の出どころを聞かれる・S4・L4）：用件の録音で答えた。⛔ J13r（同じ場面・L4）：差し戻しのあと、AIはまた用件の
        録音を選び、2回目はAIの選びで進む決まりだったので、用件の録音が流れた（G-59 asks4 で、名乗り・用件の質問の読み30回・質問でない言葉の
        読み210回に「ほかのこと」は0回＝読みのほうを採る）"""
        got = self.asks_read.get(self.callee_turns, set())
        if ASKS_OTHER not in got or ASKS_YES in got:
            return None
        if self.other_rejected == self.callee_turns:
            return self._own_fallback("no_answer", None, None, {}, {})
        self.other_rejected = self.callee_turns
        said = f"（「{self.callee_said[:40]}」）" if self.callee_said else ""
        return self._reject(f"このご質問{said}は、名乗りや用件のことではありません（名乗り・用件の録音では答えません）。replay_opening を"
                            f"「{PART_LABEL['own']}」（業務の情報で答えられる問い）か「{PART_LABEL['no_answer']}」（答えを持っていない問い）で"
                            f"呼びます（想定問答にある問いなら、その問いの名前）。{self._answer_choices()}")

    def _known_line_of(self, part: str | None) -> str | None:
        """判定が「答えられるのに預かっている」の答えのもとに挙げた文（部分）が、知っていることの文のどれか（known_lines）なら、その文。
        判定の言い換えは使わない（そのままの文だけ＝中身を確かめなくてよい）。"""
        from grounding import verbatim
        said = (part or "").split("：", 1)[-1]
        return next((k for k in self.known_lines() if said and verbatim(said, [k])), None)

    def _fallback_missing(self, part: str | None, fallback: str | None) -> str | None:
        """受け答え・自分で答える質問で、返す文が使えないときの一言が選ばれていなければ、その差し戻しの理由（_own_answer・grounding_jobs の1か所）。
        受け皿でも、お客様の言葉に応える（✅ 2026-10-23 ユーザー：言葉を足さずに「はい、」で質問に戻るのは「お客様の気分を害する返答」・答えられる問いは
        答える＝例2「OK」）。どれで応えるかは、お客様の言葉を聞いた会話AIが、答えの文と一緒に選ぶ（使えないとわかってから選ばせると、相手をもう
        1回待たせる）。⛔ 2026-10-23 の見直し：自分で答える質問の受け皿の文を、お客様の言葉と同じ名詞の数で選んでいた（`_known_line`）＝「何時まで
        やってます？」では「営業時間」の言葉が無く、答えがあるのに「申し伝えます」の一言になった。AIの答えの文の名詞を足すと、「御社の創業は
        何年ですか？」に「弊社」の重なりで本社の所在地の文を選んだ ⇒ 言葉の重なりで選ぶ形は撤去し、意味の分かる会話AIに選ばせる"""
        if part == "talk" and fallback not in TALK_FALLBACK:
            return (f"「{PART_LABEL['talk']}」では、返す文が使えないときに流す一言も「{ARG_LABEL['fallback']}」で選んで、もう一度呼んでください（"
                    + TALK_CHOICES + "）。")
        if part == "own" and fallback not in self.known_lines():
            return (f"「{PART_LABEL['own']}」では、答えのもとにした業務の情報の文を「{ARG_LABEL['fallback']}」で選んで、もう一度呼んでください"
                    f"（業務の情報に答えが無ければ「{PART_LABEL['no_answer']}」です）。")
        return None

    def _answer_choices(self) -> str:
        """相手の質問に答える部分の一覧（AIへの知らせの文。_asking_note・_answer の1か所）。"""
        faq = "".join(f"・「{n}」" for n in self.answers)
        return (f"誰からの電話か＝「{PART_LABEL['intro']}」・何の用件か・こちらとの関係・営業の電話か＝「{PART_LABEL['purpose']}」"
                + (f"・想定問答にある問い{faq}" if faq else "") + f"・それ以外の問いで、業務の情報にあること＝「{PART_LABEL['own']}」・"
                f"業務の情報にも無いこと（番号をどこで知ったか・金額・社内のことなどで、情報に無いもの）＝「{PART_LABEL['no_answer']}」"
                # ⛔ 2026-10-22 の4回目の流し（G-57 tc38 の L4・tc36 の S2）：答えを持っていない質問の答えの文に、AIが「改めてご連絡いたします」
                #    （約束）を書き続け、書き直しを3回くり返す間、相手は黙って待った ⇒ 答え方を選ぶ時点で、どう書くかを渡す（この1か所）
                "（この2つは、自分の言葉で答えます。答えを持っていない質問は、作らず・断らず・連絡やかけ直しを約束せずに、そのご質問を"
                "担当の者に申し伝える文にします）"
                # ✅ 2026-10-22 ユーザー（5回目）：「世間話のような対応や設定にない無難な対応はライブAIにしかできないよ！」
                f"・質問ではない言葉（あいさつ・お礼・気遣い・世間話・お客様のお話）に、ひとこと返すとき＝「{PART_LABEL['talk']}」（自分の言葉で）")

    def _back_play(self, digressed: bool, asks: bool = True) -> list[dict]:
        """もう伺った質問（呼んでいる段なら、お呼びするお願い）へ戻る前に流す一言（相手のお話・ご質問に応えたあと＝digressed のときだけ。無ければ空）。
        ✅ 2026-10-23 ユーザー：「お客様『そうですよね。』の会話の後に、『はい、ご予約はされていますでしょうか？』だと会話が繋がっていないですよね。
        同じことを繰り返すな！！」⛔ 前は、お話に応えたあと、話を戻すことを言わずに、もう伺った質問をそのまま流していた。録音で流せない通話では、
        AIが同じ一言を言う（_continue）。同じ所（質問・言い直しの番号／お呼びするお願い）へ2回目に戻るときは「それではもう一度確認させていただきますね。」、
        その次からは「確認の途中でしたのでもう一度お伺いさせていただきますが」＝続く問いの頭（phrases.BACK_LINES の並び。✅ 2026-11-07 日本時間3時14分01秒
        ユーザー「(4) H7」・4時40分35秒「たびたび失礼いたしました。はおかしいですね：「確認の途中でしたのでもう一度お伺いさせていただきますが、ご予約は～」」。
        ⛔ それまでは何回戻っても1回目と同じ文）。戻る所で呼び、流した回数を数える（_continue）。
        asks＝戻った先が、続けて1本にできる問い（質問・言い直し）か：問いでない一言（ご案内の申し出・振り返りの一言）と、名前の録音を組んで流すお呼びするお願い
        には、文を終えない一言（3回目からの形）をつながない（その手前の一言のまま。⏳ 問いでない一言へ3回目に戻るときの言い方は伺う）。
        お呼びする段は電話口の方ごとに数える（同じ方にお呼びする一言は2回まで＝_calls_to_speaker）：⛔ 3時台に入れた形は段の全体で数えていたので、取り次がれた
        新しい方に、初めて聞く「それではもう一度確認させていただきますね。」を流しうる形だった"""
        if not digressed:
            return []
        key = self._place()
        n = self.back_counts[key] = self.back_counts.get(key, 0) + 1
        lines = BACK_LINES if asks else [x for x in BACK_LINES if closes(x[1])]
        rid, text = lines[min(n, len(lines)) - 1]
        return [{"kind": "recorded", "id": rid, "text": text}]

    def _place(self) -> str:
        """いまお返事を待っている所（戻る一言の回数を数える単位の唯一の決まり＝_back_play）：お呼びする段は電話口の方ごと（同じ方にお呼びする一言は2回まで）・
        言い直しの返事待ちは、その言い直し（言い直した言葉ごと＝値を直していただいたあとの言い直しは別の所。頭の受けの言葉は数えない＝drop_receipt）・質問は、
        その質問（システムの質問の番号は役目ごとに1つ＝元の質問と文で分ける）。✅ 2026-11-07 日本時間3時14分01秒 ユーザー「(4) H7」の「同じ所（同じ質問・
        同じ言い直し・お呼びするお願い）へ2回目に戻るとき」。⛔ 2026-11-08 日本時間7時台（11-07 23時26分13秒のご質問の見直しの B-4）：言い直しを待っている
        質問の番号か、いまの質問の番号で数えていた＝質問へ1回戻ったあとの、その言い直しへの初めての戻りと、別の質問の W1 への初めての戻りに、2回目の
        「それではもう一度確認させていただきますね。」を流した"""
        if self.phase == "calling":
            return f"calling@{self.speaker_from}"
        if self.awaiting_confirm is not None:
            return f"言い直し@{self.awaiting_confirm}@{drop_receipt(self.awaiting_say or '')}"
        q = self.questions.get(self.current) if self.current is not None else None
        return "" if q is None else f"{q.id}@{q.of or ''}@{q.text}"

    def _continue(self, play: list, shown: str, final: bool = False) -> dict:
        """録音（名乗りの流し直し・お詫びの相づち）を流したあと、いまの位置へ戻る返事（唯一の場所。_op_replay_opening・_resume）。
        いまの位置＝目的の人を呼んでいる段（もう一度だけ呼ぶ）／言い直しの返事待ち（AIが言い直しをもう一度言う）／答えを待っている質問
        （その質問の録音）／聞いている質問が無い（相手が話すのを待つ）。shown＝流した録音の文（空なら、流したのはお詫びの相づちだけ）。
        final＝いまの質問・言い直しについて尋ねられて答えたあと、お客様が相づちだけを返した（_resume の ASKED_REPLY）：話はいまの質問から移っていないので
        戻る一言は付けず、同じ言い直し・同じ質問へは戻らずに、最終の確かめで締めにいく（_final_check。言い直しの返事待ちは言い直した向き・質問の返事待ちは
        こちらが勧めた向き＝final_toward。向きが決まらない質問は、頭に「それでは」を付けて伺い直す）。✅ 2026-11-06 日本時間11時19分55秒・11時55分13秒 ユーザー"""
        if self.after_answer is not None:
            # 答えに添えられた相手の質問に答えた：先に作っておいた返事（次の質問・言い直し・聞き返し・念のための確認・締め＝_question_first）を、答えのあとに流す
            # （取り出す1か所 _take_after_answer＝その返事の「この発話で言った」印を、いま流すこの発話に付け直す）
            return self._led(play, "", self._take_after_answer())
        if self._explaining() and self.point["explained"] is not None:
            # ご納得いただけなかった点をご説明した：次は手順の1か所が決める（ほかに…・ご説明できなければ申し伝えて続ける／締める。ご説明の最中に、ご説明の
            # もとの答えが遅れて届いた判定で取り消されていたら、段を戻して「-」の決まりで進む＝_point_step の back）
            self.point["stage"] = "explained"
            return self._led(play, "", self._advance("", handled=True))
        done = played_note(shown) if shown else ""
        # もう伺ったことへ戻る前の一言（相手のお話・ご質問に応えたあとだけ）：流す所で選んで数える（_back_play）
        digressed, self.digressed, back = self.digressed, False, []
        if self.phase == "calling":
            # 目的の人に繋がる前：名乗り・用件のあとに、もう一度だけ呼ぶ一言（DESIGN.md 4-3）。同じ方に3回目は呼ばない（同じお願いを繰り返さない）
            lead = []
            if self._calls_to_speaker() < 2:
                self.target_called.append(self.callee_turns)
                # まだこちらの名乗りを聞いていない方（取り次がれて代わった方＝_new_ears）には、戻る一言（もう伺ったことへ戻るとき＝10-23）ではなく、あいさつ・
                # 名乗りから（相手が替わったら短く名乗り直す＝本書 9-4。流す1か所 _reception_lead）。⛔ 2026-11-08 日本時間7時台（11-07 23時26分13秒のご質問の
                # 見直しの B-9）：その方の世間話に応えたあと「話を戻させていただきますね。」＋お呼びするお願いを流していた
                lead = self._reception_lead() if self._new_ears() else []
                back = [] if lead else self._back_play(digressed, asks=False)   # お呼びするお願いは名前の録音を組んで流す＝続く一言と1本にしない
                play += lead + back + [self._part_play("target")]
                done = played_note(shown + "".join(p["text"] for p in lead + back) + self.target["target"])
            # 戻る一言・あいさつから始まる：相づちを重ねない（その一言が相手の返事を受ける・あいさつの前に相づちは付けない＝_prepend_played の led と同じ）
            return {"result": "replayed", "play": play, "silent": True, "message": f"{done}{self._now()}"} | (
                {"bridged": True} if (back or lead) and play else {})
        if self.awaiting_confirm is not None and final:
            return self._final_check(self.awaiting_confirm)   # 言い直しについて尋ねられて答えた：同じ言い直しには戻らず、最終の確かめ
        if self.awaiting_confirm is not None:
            # 言い方（say）も渡す：AIの声が外れたかを見張る（offscript。渡した言い方のあるターンだけを見張る＝頭から比べるので、会話AIが戻る一言から
            # 言うときは、その一言を頭に付けて渡す）。戻る一言は、録音で流す通話は録音・会話AIが話す通話は会話AIが言う（質問へ戻るときと同じ）。
            # ⛔ 2026-11-06 日本時間10時台に見つけた：この枝だけ、戻る一言を作って捨てていた（ご遠慮の一言の「いいえ」・お客様のご質問に答えたあとに、
            # 言い直しをそのまま言い直した＝本書 9-4 の「範囲外の質問」の「もう伺った質問へ戻るときは話を戻す一言の録音から」に届いていなかった）
            # もう一度言う文は、伺ったときの頭の受けの言葉を外して「それでは」から（restated_again の1か所＝戻る一言のあとの形も質問と同じ back_fit）。3回目からの
            # 一言（文を終えない「…お伺いさせていただきますが」）は言い直しの頭＝言い直しと1つの文で会話AIが言う（言い直しは値を含むので会話AIの声＝録音のあとに
            # 「改めまして（値）で大丈夫でしょうか？」と言うのと同じ形）。問いでない言い直し（「…承っておきますね。」）には文を終えない一言をつながない（asks）
            back = self._back_play(digressed, asks=self.awaiting_say.endswith(("？", "?")))
            btext = "".join(p["text"] for p in back)
            joined = bool(back) and not closes(btext)
            say, then = restated_again(self.awaiting_say, btext), ""
            note = say_note(say, "もう一度、言い直し")
            if back and not joined and self.play_recordings:
                play += back
                done = played_note(shown + btext)
            elif back and not joined:
                say, then = btext + say, say_note(btext) + "続けて、"
            return {"result": "replayed", "play": play, "speak": True, "say": say,
                    "message": f"{done}続けて、{then}{note}相手の返事を待ってください。"} | ({"bridged": True} if back else {})
        q = self.questions.get(self.current) if self.current else None
        if self._held_now() is not None:
            # 応える前の相手の言葉を、判定のどれかがいまの質問の答えと読んだ（_keep_answer_reading）：質問を流す前に、その言葉の答えの報告か、
            # 質問に戻るかを、AIが音声から選ぶ（録音で応えた＝相手の返事を待たずに、ここへ来た。AIが自分の言葉で応えたときは、同じ知らせを
            # 応える返事で渡してある＝_own_then）。まだ戻らないので、戻る一言は戻るときまで持つ（流していないので数えない）
            self.digressed = digressed
            return {"result": "replayed", "play": play, "silent": True, "message": f"{done}{self._return_note()}"}
        if final and q is not None and not q.reading_of and (toward := final_toward(q)) is not None:
            return self._final_check(q.id, toward)   # 質問について尋ねられて答えた：同じ質問には戻らず、こちらが勧めた向きで最終の確かめ
        # もう流した質問へ戻る（戻る一言・「失礼いたしました。」のあと・尋ねられて答えたあと）＝頭は「それでは」（AGAIN_LEAD）。まだ流していない質問（先に
        # 言われた答えを受け取らずに、初めて流す＝_offer_ahead のあと）は、次の項目として「続きまして」（✅ 2026-11-06 日本時間12時54分30秒 ユーザー：「次の
        # 項目の質問に入る前は、すべて「続きまして」でいいんじゃないですか。」＝戻るときは次の項目ではない）
        again = q is not None and not q.reading_of and (final or self.items[q.id].played)
        if q is not None and not q.reading_of:
            self.items[q.id].played = True
        qs = list(self.questions.values())
        # 戻った先が問いか（3回目からの一言を続けて1本にできるか＝_back_play の asks）：流す形の文が「？」で終わる（ご案内の申し出・振り返りの一言は問いでない）
        asks = q is not None and (q.text if q.reading_of else question_play(qs, q.id)["text"]).endswith(("？", "?"))
        back = self._back_play(digressed, asks=asks)
        btext = "".join(p["text"] for p in back)   # 戻る一言の頭のつなぎの言葉と、質問の頭を重ねない（spoken_question・phrases.back_fit の before）
        joined = bool(back) and not closes(btext)   # 3回目からの一言は質問の頭（質問と1本の録音・会話AIの1つの文）
        bridged = {"bridged": True} if back and q is not None else {}
        if q is not None and self.play_recordings and not q.reading_of:
            qp = question_play(qs, q.id, again=again, before=btext)
            play += ([] if joined else back) + [qp]
            return {"result": "replayed", "play": play,
                    "message": played_note(shown + ("" if joined else btext) + qp["text"]) + "何も言わずに、相手の答えを待ってください。"
                               + answer_wait()} | bridged
        if q is not None:
            then = f"{say_note(btext)}続けて、" if back and not joined else ""
            if q.callback or q.of:
                # 決まった文の質問：流す形と同じ文（phrases.back_fit）をそのまま言う
                lead, quoted = "", question_play(qs, q.id, again=again, before=btext)["text"]
            else:
                lead = btext if joined and q.reading_of else spoken_question(qs, q.id, again=again, before=btext)[1]
                quoted = q.text
            return {"result": "replayed", "play": play, "speak": True,
                    "message": f"{done}続けて、{then}いまの質問 {q.id}「{quoted}」を聞いてください（名乗りは繰り返しません）。"
                               + (start_with(lead) if lead and (again or q.intro or self.lead_ins or joined) else "")} | bridged
        # 聞いている質問が無い（質問が全部済んで締めの最中など）。⛔ 2026-10-17 の見直しで見つけた漏れ：前は、どの場面でも
        #    「相手の答えを待って record_answer で報告」と返していた＝答える質問が無いのに、報告を求めていた
        return {"result": "replayed", "play": play, "silent": True,
                "message": f"{done}何も言わずに、相手が話すのを待ちます。相手が話したら、それに答えます（名乗りは繰り返しません）。"}

    def _ai_question(self) -> dict:
        """「AIですか？」「機械ですか？」「人間ですか？」と聞かれたとき（Intent.md 9-4。唯一の場所）。
        ✅ 2026-10-19 ユーザー：AIだとは伝えない ⇒ 決まった言い方「何か問題でもございましたでしょうか？」（業務設定 ai_answer＝業務の録音 ai。無ければ
           標準＝共通の一言の録音 phrases.STANDARD_PARTS["ai"]＝AI_ANSWER）を流し、相手の返事を待つ。打ち消しの「いえ、」は付けない。相手が問題は無いと答えたら、お詫びの相づちのあと、
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
                    "message": f"{say_note(AI_ASKED_AGAIN)}続けて、{r['message']}{again}"}
        self._await_reply("ai")
        if self.ai_asks == 1:
            # 業務の答え（業務の録音）か、標準の答え（共通の一言の録音）か＝_part_play の1か所（⛔ 前は、ここだけ業務の録音に決め打ちしていた）
            play = self._part_play("ai")
            text = play["text"]
        else:
            k = (self.ai_asks - 3) % len(SILENCE_PROMPT)
            text = SILENCE_PROMPT[k]
            play = {"kind": "recorded", "id": SILENCE_PROMPT_IDS[k], "text": text}
        self.ai_said = text
        then = self._now()
        if self.play_recordings:
            return {"result": "ai_question", "play": [play],
                    "message": f"{played_note(text)}{then}"}
        return {"result": "ai_question", "say": text, "message": f"{say_note(text)}そのあと、{then}"}

    def _explaining(self) -> bool:
        """ご納得いただけなかった点をご説明する段か（_point_step の stage）。"""
        return self.point is not None and self.point["stage"] == "explain"

    def _explain_due(self) -> bool:
        """いま、AIにご説明を頼むか（唯一の決まり＝次へ進む返事 _advance_to・名乗りの録音のあと _opening_reply・いま求めていること _now・受け取れない
        応え方 _explain_problem）：ご説明する段で、まだご説明していない間ずっと（この通話で N1・N2 の答えを受けたあと・続きの電話の始め＝前の通話で点まで
        伺えた。目的のお客様のいる業務は繋がってから＝呼んでいる段では頼まない）。ご説明の前に、お客様が点を言い足した・待たせた・差し戻しが入っても、
        ご説明するまで頼み続ける（言い足した点は同じ回の点に足す＝_point_heard）。⛔ 2026-11-08 日本時間6時台（11-07 23時26分13秒のご質問の見直しの A-1）：
        点を伺ったその発話のあとだけ頼んでいたので、ご説明の前に言い足されると、ご説明が落ちて「ありがとうございます。続きまして…」で次の質問へ進んだ"""
        p = self.point
        return p is not None and p["stage"] == "explain" and p["explained"] is None and self.end_reason is None and self.phase != "calling"

    def _resume(self) -> dict:
        """決まった言い方（「何か問題でもございましたでしょうか？」）に、相手が問題は無いと答えた（「いえ」「別に」「機械みたいだったので」）：
        お詫びの相づち「失礼いたしました。」（_ack）のあと、いまの位置へ戻る（_continue）。相手の質問に自分の言葉で答えたあと（_own_answer）の
        相手の返事（「そうですか」「わかりました」）も、ここから戻る（相づちはお礼）。応える前の相手の言葉にいまの質問の答えがあるか、AIが選ぶ
        ところ（_held_now・_return_note）で、AIが「入っていない」と選んだときも、ここから戻る（覚えておいた言葉は、もう使わない）。"""
        # 取っておいた返事（_question_first）が、先に言われたことを伺う番（_offer_ahead が、その返事を作った発話に立てた印 offered）なら、その言葉は取っておいた
        # 返事のもの＝消さずに、流す時に使う（_take_after_answer が印を流す時の発話に付け直す）。⛔ 2026-11-08 日本時間11時台：消していたので、急いでいる方への
        # 伺いへの「はい、来週なら大丈夫です。」に添えた質問に答えたあと、「来週」の報告を答えたあとの発話の言葉に当てていた
        kept = self.answer_held if self.after_answer is not None and (self.answer_held or {}).get("offered") == self.after_answer[2] else None
        held = self._held_now() is not None and kept is None   # （先に言われたことを持ち続けるかは、報告の入口の _take_rest の1か所で決める）
        ahead = held and bool(self._held_now().get("ahead"))   # 先に言われたことに、次に伺う質問の答えは無かった
        if (self.own_pending == ASKED_REPLY and (qid := self.awaiting_confirm or self.current) is not None
                and self.meaning_said in self.questions[qid].all_conditions):
            # いまの質問について尋ねられて答えたあとの、お客様の返事（「じゃあ、お願いします。」）が、判定の読みでいまの質問の答え：質問に戻らずに、答えとして
            # 報告し直させる（判定が届いてから決める＝wants_checks）。⛔ 2026-11-06 日本時間5時56分55秒のご質問のあとの読み直しで見つけた：会話AIが「質問に
            # 戻る」を選ぶと、答えたお客様に質問を流し直した
            return self._reject("相手の言葉は、いまの質問への答えです（意味の判定）。質問には戻らずに、答えとして報告します。")
        if not (self.ai_pending or self.own_pending or held):
            return self._reject(f"「{PART_LABEL['resume']}」は、エーアイかと聞かれて決まった言い方を流したあと・相手の質問に自分の言葉で答えたあとの、"
                                "相手の返事にだけ使います。")
        own, ai, self.ai_pending, self.own_pending, self.answer_held = self.own_pending, self.ai_pending, False, False, kept
        w = next((p for p in (self.wish, self.handoff) if p is not None and p.get("detail") == self.current), None)
        if ahead and w is not None and w.get("known") is not None and self.items[self.current].state == "empty":
            # 伺おうとした連絡先は、もう伺っていた（_wish_contact）：伺わずに、その連絡先で申し伝えて次へ（伺う番を閉じた＝相づちは伺う番を開いた報告の受けの言葉。Flow._ack）
            self._drop_unasked(self.current)
            self._wish_known(w.pop("known"), w)
            return self._advance("", handled=True) | {"quiet": True, "ahead_closed": True}
        r = self._continue([], "", final=own == ASKED_REPLY)
        if r["result"] == "confirm_required":
            return r   # 最終の確かめ（_final_check）：相づちは確かめの一言の頭（_ack）
        if r["result"] == "end":
            return r | {"led": True}  # 答えに添えられた質問のあとの締め：締めの言い方（「お忙しいところ…ありがとうございました」）に相づちを重ねない
        if ai:
            return r  # 「何か問題でも…」に問題は無いと言われた：お詫びの相づち「失礼いたしました。」（_ack）のあと、話を戻す一言といまの質問
        if not own or r.get("bridged"):
            # 録音で応えた続き（相手はまだ話していない）・話を戻す一言から始まる：相づちを重ねない（一言が相手の返事を受ける）。先に言われたことに答えが無く、伺う番を
            # 閉じて質問へ戻る（ahead）：伺った「はい、」のあとの頭は、伺う番を開いた報告の受けの言葉（Flow._ack の ahead_closed。⛔ まとめ役の見直し (D) まで「はい、」＋
            # 「それでは訪問とお電話のどちらが…」で、申し出を受けていただいたお返事への受けの言葉が抜けた）
            return r | {"quiet": True} | ({"ahead_closed": True} if ahead and not r.get("bridged") else {})
        # 次の質問・念のための確認へ進む（答えはもう受け取った）：相手の返事（「そうなんですね」）を「はい、」で受ける。✅ 2026-10-23 ユーザー：
        # 「そうなんですね。」のあとの「ありがとうございます、それでは…」は「こちらも同様（会話が繋がっていない）」⇒ ⛔ 前は、質問に答えたあとの
        # 返事にお礼の相づちを流していた（お礼を言われていないのに、お礼で受けていた）
        return r | {"talked": True}

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
                 "message": notice(f"さきほどの言葉は、渡した言い方と違ったので、プログラムが{stopped}。"
                                   f"{say_note(say)}相手の返事を待ちます。")}
        return self._log_signal({"off_script": expected, "heard": heard}, reply)

    def on_signal(self, party: str) -> dict:
        assert party in NOT_A_PERSON, party
        return self._log_signal({"party": party}, self._party(party, by="signal"))

    def _log_signal(self, args: dict, reply: dict | None) -> dict | None:
        """電話接続側からの知らせ（AIの操作ではない）を、操作の記録に残す唯一の場所（流した録音・締めを、ほかの返事と同じく扱う）。
        何もしなかった知らせ（None）は残さない。"""
        if reply is not None:
            self._note_said()
            self.log.append({"op": "signal", "args": args, "reply": reply, "callee_turns": self.callee_turns})
        return reply

    def _party(self, party: str, by: str) -> dict:
        """相手の種別が分かったときの扱いの唯一の場所（AIの報告 report_party と、音・回線の信号 on_signal の両方）。by＝どちらで分かったか。"""
        if party == "person" and not self.answering:
            return self._reject(f"「{PARTY_LABEL['person']}」は、電話に出た方の最初の言葉を聞いたときだけ使います（いまの方とのお話を続けます）。")
        self._note("party", party=party, by=by)
        if self.answering and self.voicemail_waiting and party == "ivr" and by == "ai":
            # 留守番電話の案内を聞いて待っている間に、AIが同じ案内を「自動音声の案内」と報告し直した：案内の続きとして待ち続ける（人が出るかも
            # しれない待ちを、案内の呼び方の揺れでやめない。音で分かる確かな知らせ＝ファクスの音は、今までどおり切る）。⛔ 2026-10-25 J8a
            # （留守番電話のあとに人が出る・L4）：「ただいま電話に出ることができません。」を留守番電話と報告したあと、続きの「ピーという
            # 発信音の後に、お名前とご用件をお話しください。」を自動音声と報告し直し、そこで切っていた
            party = "voicemail"
        self.party = party
        if self.answering and party == "voicemail":
            # 第一声が留守番電話の案内：すぐには切らず、黙って聞き続ける（✅ 2026-10-24 ユーザー：「留守番電話が流れて、数秒してから出る方も
            # いらっしゃいますよね。ちょっと待った方がいいんじゃないですか」）。人が出たら名乗る（on_first_words_end）。出なければ on_voicemail_wait_end
            first = not self.voicemail_waiting
            self.voicemail_waiting = True
            # ⛔ 2026-10-24 G-57 J2（留守番電話のあとに人が出る・L4）：案内のあとに出た方の「あ、もしもし、すみません、お待たせしました。」を、
            #    AIはまた留守番電話と報告した（前の文は「人が出ることがあります」だけで、人の声がしたらどう知らせるかを渡していなかった）
            return {"result": "wait", "silent": True, **({"voicemail_wait_s": VOICEMAIL_WAIT_S} if first else {}),
                    "message": "留守番電話の案内のようです。何も言わずに聞き続けます。案内のあとに人が出ることがあります：案内ではない人の声"
                               f"（「もしもし」「お待たせしました」など）がしたら、report_party を「{PARTY_LABEL['person']}」で知らせます。"}
        if self.answering and party not in NOT_A_PERSON:
            # 人（受付の方・目的の人など）：名乗りは、第一声が終わってから（on_first_words_end）。留守番電話の案内のあとに人が出たら、聞き続けるのをやめる
            self.voicemail_waiting = False
            if (reply := self._opening_when_ready()) is not None:
                return reply
            return {"result": "noted", "silent": True,
                    "message": "受け付けました。何も言わずに、相手の言葉を最後まで聞きます（名乗りは、相手の言葉が終わったらプログラムが流します）。"}
        return self._not_a_person(party) if party in NOT_A_PERSON else self._party_known(party)

    def _not_a_person(self, party: str) -> dict:
        """人ではない相手（留守番電話・自動音声の案内・ファクス）の扱いの唯一の場所：留守番電話は業務設定の伝言を最初の1回だけ残す。ほかは話さずに切る。"""
        self.answering = self.voicemail_waiting = False
        if party == "voicemail" and self.voicemail_message and not self.voicemail_left:
            # ✅ 2026-10-19 ユーザー：「依頼者ごとに設定で選べる形にして、必要なら録音をしましょう」⇒ 伝言は依頼主の単位の録音（名乗りの部分 voicemail）
            #    依頼主の録音が無い（名乗りを録音で流さない通話）ときは、AIが設定の文のまま言う。残したことは通話の事実 voicemail_message
            #    （次の電話からは残さない＝voicemail_left。2回目からは、伝言の設定の無い業務と同じく何も言わずに切る）
            # 残したことは、伝言の録音を最後まで流し終えたとき（on_played の1か所）に通話の事実へ。⛔ 2026-11-02（CL-2 の連絡帳 4-167 の頼み6）まで、
            # 流すように頼んだ瞬間に「残した」としていた＝伝言の途中で切れても、次の電話からは残さなかった。AIが言う古い試しの形（録音が無い）
            # だけは、流し終えた知らせが無いので、言わせた時に残す（本番は録音で流す形だけ＝DESIGN.md 6-13）
            spoken = "voicemail" not in self.opening
            if spoken:
                self._note("voicemail_message")
            else:
                self.voicemail_sending = self.voicemail_message
            return self._close("voicemail", text=(self.voicemail_message, "voicemail"), part=True, spoken=spoken)
        return self._hang_up(party)

    def _party_known(self, party: str) -> dict:
        """人の相手の種別が分かったときの扱い（_party から。第一声を聞く段のあと）。"""
        if party == "wrong_party":
            self._note_irreversible("party", "none")   # 番号違いは会話AIの報告だけで決まる（2つ目の判定が読まない）＝録音の確かめまで保留
            return self._close("wrong_party")
        if self.phase == "calling":
            return self._calling_reported(party)
        if party in STANDIN_PARTIES:
            return self._reject(f"「{PARTY_LABEL[party]}」は、目的の人をお呼びしている間だけ使います（いまの方とのお話を続けます）。")
        if self.asking_reception and self.target and party == "target":
            if self._hold_read():
                return self._hold()  # 取り次ぎの読み（「少々お待ちください」）なら黙って待つ
            if not self.callback_held:
                # かけ直しの質問のあとに取り次ぎが無い：電話口の方は、かけ直しの質問を聞いた方（目的の人ではない）。⛔ 2026-10-22 G-57 tc29
                # （不在で別の方に代わる・S4）：山田様の言葉が送り直されたとき、AIが「目的の人」と報告し、山田様に岡田様かを確かめた
                return self._reject("電話口の方は、こちらの質問（かけ直し・お立場）を聞いた方です（そのあと、お取り次ぎはありませんでした）。"
                                    "目的の人ではありません。")
            return self._target_back()
        if self.asking_reception and party == "reception":
            return self._hold()  # かけ直し・お立場の質問の途中で、取り次がれた・待つように言われた：答えではないので黙って待つ
        if (routed := self._judged_first()) is not None:
            # 答えの段で電話口の方を報告されても、判定が待つ・かけ直す・もう一度言ってほしいと読んだ言葉なら、判定の読みで応じる1か所で応じる。⛔ 2026-11-06
            # 日本時間13時台の見直し（C7）で見つけた：判定の読みを当てずに「受け付けました」とだけ返し、お客様には無言だった（前から）
            return routed
        if party == "target" and self.target and self.opening and all(it.state == "empty" for it in self.items.values()):
            # 繋いだあと、まだ何も答えていないうちに、また目的の人が出た＝前の「目的の人」は取り次いだ方だった（AIが早く報告した）か、
            # 人が替わった ⇒ 代わった方への流し直しと同じ（あいさつ・名乗り・用件・いまの質問）。⛔ 2026-10-19 G-57 の4回目：会話AIの聞き取りが
            # 「岡田ですね。」で切れ（続きの「少々お待ちください。」は次の発話に入った）、AIが受付の方を目的の人と報告して用件と質問を流し、
            # 代わった岡田様には名乗りも用件も流れなかった（その場の判定なしで流した回）
            return self._op_replay_opening({"part": "handover"})
        return {"result": "noted", "message": f"受け付けました。{self._now()}"}

    def _calling_reported(self, ai: str, why: str | None = None, offer: str | None = None) -> dict:
        """呼んでいる段で、電話口の方が誰かを決める唯一の場所（DESIGN.md 4-3。AIの報告を、独立した判定に当てる＝1-1 の決まりを目的の人にも）。
        ai＝AIの読み：target（report_party「目的の人」）／handover（replay_opening「代わった方へ」＝代わった方が名乗らない・違うお名前を
        名乗った・確かめに「いいえ」と答えた）／reception（report_party「受付の方」＝お取り次ぎいただく・待つように言われた）／
        away（request_callback＝お呼びした方はいま出られない。why＝AIが選んだ理由・offer＝相手の申し出）／substitute（report_party「代わりの方」
        ＝電話口の方が代わりにご用件を伺うと申し出た）／successor（report_party「後任の方」＝お呼びした方は在籍しておらず、電話口の方が後任）。
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
              山田様に戻る時刻を聞き、そのあと代わって出た岡田様にも「何時ごろお戻りに…」を流した
        (6) 代わりの方・後任の方・在籍していない・ほかの手段・折り返しや伝言の申し出（どれも「電話口の方は目的の人ではない／目的の人とは
            話せない」）は、判定が「ご本人」
            でなければ受け取り（代わりの方・後任の方は、判定が届いていれば、話している方を代理の方と読んだ判定とそろったときだけ＝_proxy_read。
            そろわなければ「この方は目的の人ではない」として確かめる）、応じ方は業務設定で決める（_not_available）。判定が「ご本人」なら食い違う（ほかの手段だけは、ご本人の申し出として
            受け取る）＝「この方は目的の人ではない」として (3)(4) で決める。✅ 2026-10-22 ユーザー：「代わりの方が聞いてくれると言っているのに、
            それを無下にするんですか？」"""
        j = self.meaning_said if self.words_complete else None
        if self.successor and ai == "successor":
            # 後任の方へ切り替えたあと（_to_successor）は、後任の方が目的の人：AIの「後任の方」は「目的の人」の報告（⛔ 2026-10-22 G-57 tc33
            # （在籍していない・S4）：お呼びした後任の田中様の「お電話代わりました。田中です。」を、AIが「後任の方」と報告し、在籍していないと
            # 同じに扱って締めた）
            ai = "target"
        self.target_claimed = ai == "target"
        if self.judging and j is None and (self.midway or not self.words_complete):
            return self._wait_for_words()
        if self.callee_turns < self.target_from:
            # 後任の方へ切り替えた発話（「岡田は退職いたしました」）への報告のし直し：その言葉は前にお呼びした方への返事（判定も前の方の問いで読んだ）。
            # 後任の方への一言の返事を待つ
            return self._reject("お呼びした方は在籍していないと伺い、後任の方への一言をもう流しました（いまの言葉は、前にお呼びした方への返事です）。")
        # こちらに尋ねているときは、ここへ来る前に答える道へ回す（apply の入口＝1か所。不在・取り次ぎより先。⛔ 2026-10-22 G-59：「岡田ですね。
        # 失礼ですが、どちら様でしょうか？」を、Sonnet は3回とも「取り次ぎを待つ」と読んだ＝取り次ぎを先に見ると、質問に答えずに黙って待つ）
        if self._hold_now():
            # 届いた判定のどれか1つでも「取り次ぎを待つ」：黙って待つ（不在より先＝「岡田は外出しておりますので、別の者に代わります。少々
            # お待ちください。」は、取り次ぎの最中に話さない）。間違えて待っても、相手がまた話せば決め直せる（取り消せる）が、間違えて確かめると、
            # 取り次ぎの最中の方に話しかけ、話し手の区切りもずれる。⛔ 2026-10-22 G-57 tc15：判定がそろったときだけ待っていたので、1つが
            # 「取り次ぎを待つ」・もう1つが別の読みのとき、受付の方へ確かめる一言を流した（区切りは取り次ぎで次の発話へ動き、確かめは誰にも
            # 数えられず、知らせの文が「None」になった）
            return self._hold()
        if ai == "successor" and (seen := self._successor_read()):
            # はじめの場面で、AIの「後任の方」と、判定の「電話に出ている方は後任の方」がそろった（どの判定も「ご本人」と読んでいない）：確かめずに
            # 後任の方として繋ぐ（代わりの方・お呼びし直す道より先）。どちらか1つだけなら、前のとおり（AIだけ＝確かめる・判定だけ＝AIの報告の道）＝
            # 後任の方へ切り替えるのは取り消せない側なので、1つの読みでは進まない（試しの形：「本日は私が代わりに担当しております」を判定が1つ
            # 読み違えると、お詫びと「後任のご担当者様でいらっしゃいますでしょうか？」が流れる）。✅ 2026-10-29 ユーザー：「『別の方』と『後任』は
            # 別の軸ですよね！」。⛔ 前は「岡田は異動になりまして、今は私が担当しております。」に「後任のご担当者様でいらっしゃいますでしょうか？」と
            #    確かめ（判定に後任の方を読む所が無かった）、「退職いたしまして、後任の田中に代わります。…」のあとに出た田中様には、お詫びと確かめを流した
            return self._departed(confirm=False, read=seen | {"ai"}, why=why, agreed=True)
        if ai == "handover":
            self._new_speaker(self.callee_turns, after=False)  # 代わった方が話し始めた（確かめを聞いた方の返事なら、替わった印にしない）
        if (handed := self._handed()) and j != TARGET_IS_THERE and ai not in ("target", "successor") and not offer \
                and (why is None or why in AWAY_WHYS):
            # 前の方が「お呼びした方は出られない」と伝えたうえで、電話を代わった方：お呼びした方の代わりにお話を伺う方（代わりの方。応じ方は
            # 業務設定）。代わった方ご自身の申し出（折り返し・伝言・ほかの手段・あとで・別の番号へ・在籍していない）は、その申し出で決める。
            # ✅ 2026-10-22 ユーザー：「代わりの方が聞いてくれると言っているのに、それを無下にするんですか？」。⛔ G-57 tc33（不在で別の方に代わる）：
            #    代わって出た山田様に、声の型によってお立場を伺ったり、いきなり岡田様の戻る時刻を聞いたりした（AIの報告で道が分かれていた）
            return self._not_available("substitute", why, None, handed | ({"ai"} if ai == "substitute" else set()))
        if self._away_now():
            return self._away(ai, why, offer)
        asked = self.asked_target is not None
        read, answered, away = self._target_readings()
        doubt = self._doubted()
        if away and j != TARGET_IS_THERE:
            # 前の言葉で、お呼びした方が出られないと言われていた（この方でも、取り次いだ前の方でも。判定が遅れて届いた分も）：いまの言葉が
            # 「ご本人」と読まれない限り、お呼びし直さずに、かけ直しへ
            return self._away(ai, why, offer)
        if ai in STANDIN_PARTIES or why in ("departed", "other_means") or offer in ("callback", "message"):
            # 折り返し・伝言の申し出も同じ：申し出た方は、お呼びした方の代わりに伝える側（電話口の方は目的の人ではない）。
            # 判定のどれか1つでも「ご本人」と読んだら受け取らない（判定が割れた「決められない」も、取り消せない側へは進まない＝不在・取り次ぎの
            # 読みと同じく1つで数える）。⛔ 2026-10-25 J11a（確かめにいいえ・S2）：名乗らずに出た方の「はい、お電話代わりました。」を、AIが
            # 「代わりの方」と報告し、判定は「ご本人」と「代わった人が話し始めた」に割れた＝「ご本人ではない」として受け取り、確かめずに用件と
            # お立場の質問を流した。これまでの回帰で、本当に代わりの方・後任の方・申し出・在籍していない方だった報告（約130回）に、「ご本人」の
            # 読みは0回
            if not (self.words_complete and TARGET_IS_THERE in self._judged_now().values()) or why == "other_means":
                if j is None and self.judging and ai in STANDIN_PARTIES and self.standin_wait != self.callee_turns:
                    # 判定が1つも届いていない：代わりの方・後任の方として用件や確かめを流すのは、取り消せない側（目的の人の報告を読み1つで
                    # 繋がないのと同じ）⇒ 黙って待ち、判定が届いたら報告し直させる（unreported_note）。同じ発話で2回目も判定が無ければ、
                    # AIの読みだけで受け取る（判定が止まっていても通話を止めない。通話の事実の read が「ai」だけ＝通話後の検証が確かめる）。
                    # ⛔ 2026-10-22 G-57 tc34（確かめにいいえ・L4）：代わって出た岡田様の「お電話代わりました。岡田です。」をAIが「代わりの方」と
                    #    報告し、判定（2つとも「ご本人」）が待つ長さに間に合わず、岡田様にお立場の質問とかけ直しの質問を流した
                    self.standin_wait = self.callee_turns
                    return {"result": "wait", "silent": True,
                            "message": "相手の言葉の読みがそろうまで、何も言わずに待ちます（このあと知らせがあったら、もう一度報告します）。"}
                # どの判定も「ご本人」と読んでいない（別の方・代わった人・届いていない）：電話口の方は目的の人ではない、とそろう（上の (6)）。
                # ほかの手段は、電話口のご本人の申し出でもよい。代わりの方・後任の方は、判定が届いていれば、話している方を代理の方と読んだ判定と
                # そろったときだけ（9-5 の 7「独立した確かめとそろったときだけ受け取る」）。✅ 2026-10-30 ユーザー：「(1)(2)ともお勧めの形でOK」。
                # ⛔ 前は「ご本人」と食い違わなければ受け取っていた＝J11a の「はい、お電話代わりました。」（正しくは決められない）で誤った報告を
                #    止めたのは、3.5 Flash Lite の読み違いの「ご本人」だった（お呼びする段を組だけにすると、組が正しく読んで報告が通った）
                if not (ai in STANDIN_PARTIES and j is not None and not self._proxy_read()):
                    return self._not_available(ai, why, offer, {"ai"} | {k for k in JUDGMENTS if self.check.get(k) not in (None, HOLD)})
            # 判定のどれかが「ご本人」（代わりの方・後任の方・在籍していない、と食い違う）・代わりの方・後任の方の報告が、代理の方の読みとそろわない
            # ＝AIの読みを「この方は目的の人ではない」として決める（確かめる・お呼びし直す）
            ai = "not_target"
        if ai == "away":
            if j is None or (j == TARGET_IS_THERE and why not in AWAY_WHYS):
                # 判定が届いていない（AIの読みだけ＝通話の事実に残し、通話後の検証が確かめる）／電話口のご本人が、いまは話せないと言った
                return self._not_available(ai, why, offer, {"ai"} | (read if j == TARGET_IS_THERE else set()))
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
        if ai == "target" and self.words_complete and any(says_busy(self.check.get(k)) for k in JUDGMENTS):
            # 電話に出ているご本人が、いまは話せないと言った（判定のどれか1つでも）：繋がない（用件・質問を流さない）。理由はAIが聞いた言葉で
            # （ふつうは「あとで」＝(5)）。⛔ 1つの選びの形では、判定は「ご本人」か「不在」のどちらかしか言えず、「ご本人」と読むと、AIの
            # 「目的の人」とそろって繋いでいた（いまは話せない方に、用件と質問を流す）
            return self._reject(f"相手の言葉（「{(self.callee_said or '').strip()[:30]}」）は、お呼びした方ご本人が、いまはお話しできないという意味です"
                                f"（意味の判定）。名乗り・用件・質問は流しません。{offers_rule()}")
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
        return self.words_complete and any(says_away(self.check.get(k)) for k in JUDGMENTS)

    def _hold_now(self) -> bool:
        """いまの相手の言葉（出揃ったもの）を、届いた読みのどれか1つでも「取り次ぎを待つ」と読んだか（呼んでいる段の、黙って待つ側）。
        読み＝目的の人の判定と、尋ねているかの問い合わせ（判定の仕組みは、どの問いにも「取り次ぎを待つ」を選択肢として添える＝meaning_judge.KIND_DESC）と。
        ⛔ 2026-10-22 G-57 tc21：受付の方の「岡田は外出しておりますので、別の者に代わります。少々お待ちください。」（不在と取り次ぎの2つの意味）を、
           目的の人の判定は2つとも「不在」と読み、取り次ぎの最中に、かけ直しの質問へ進んだ（3本中2本）。尋ねているかの問い合わせは「取り次ぎを待つ」と
           読んでいた（G-59：この問い合わせの「取り次ぎを待つ」は、答えた335回で、取り次ぎではない言葉に0回）"""
        return self.words_complete and self._hold_read()

    def _hold_read(self) -> bool:
        """いまの相手の言葉への届いた読みを組み合わせると「取り次ぎを待つ」か（組み合わせの決まりは wait_reading の1か所＝報告の無いターンの judged_turn・
        答えの段の _not_an_answer と同じ）。呼んでいる段は、言葉が出揃ってから（_hold_now）。かけ直しの質問の途中・繋いだあとの取り次ぎの報告もこれで読む。
        ⛔ 2026-11-06 7時台（5時56分55秒のご質問の読み直し）：取り次ぎの報告の4か所は、判定がそろったときだけ待つ決まり（meaning_said）だった＝読みが
           「取り次ぎを待つ」と「決められない」に割れると、待たずに目的の人かを確かめた・名乗りを流し直した（試験 TargetTest.test_split_hold_reading_waits_wherever_the_report_comes）"""
        return wait_reading(self.check) == HOLD

    def _asking_now(self) -> bool:
        """いまの相手の言葉（出揃ったもの）で、電話に出た方がこちらに尋ねているか（別の問い合わせ asks。聞かれたことに答えるのが先）。"""
        return self.words_complete and any(self.check.get(k) in ASKED for k in ASKS_CHECKS)

    def _asked_now(self) -> str | None:
        """答えの段で、いまのお客様の言葉に、報告より先に会話AIが応える番なら、その言葉（問いならその問いの文。無ければ None）。番の種類は _asked_kind の
        1か所。応える番なら、呼んでいる段の「尋ねている」＝_asking_now と同じく、入口 _apply_one の1か所で報告を止める。"""
        kind = self._asked_kind()
        if kind is None:
            return None
        return asked_text(self.callee_said) if kind == "asked" else (self.callee_said or "").strip() or None

    def _asked_kind(self) -> str | None:
        """答えの段で、いまのお客様の言葉に、報告より先に会話AIが応える番の種類（唯一の決まり＝_asked_now・知らせ _asked_note・自分の言葉の答え _own_answer・
        使えないときの一言 _own_fallback が読む）。この発話にもう応えた（question_answered_turn）・答えの文が使えず、尋ねられる前の流れのとおりに進めると決めた
        （asked_skip＝_asked_skip）ときは None：
        - "concern"＝本題で本心を伺った一言に、気になる点を言っていただいた（_concern_decided）。言っていただいた点に業務の情報の範囲で応える（ご承認の手順の
          ご説明と同じ向き＝応える文が使えなければ、ご説明できないときの一言 _point_fallback で申し伝える）。✅ 2026-11-08 日本時間2時21分15秒 ユーザー
          （お言葉は _main_talk）
        - "help"＝迷っているお客様に選ぶ助けを伝える（_hesitation の1か所）。⛔ 2026-11-08 日本時間0時台の見直しの H-4：答えたあとも、同じ発話の遅れた報告に
          選ぶ助けをもう一度言わせうる形だった＝もう応えた発話は上で None
        - "asked"＝いまの質問・言い直しに答える代わりにこちらへ尋ねた（決まりは asks_us の1か所＝いまの発話の言葉と、その意味の判定）。✅ 2026-11-06 日本時間
          4時23分33秒 ユーザー（asks_us に引いたお言葉）
        - "offer"＝申し出（詳しい者より改めて N4・締める前の申し出 N6）に、会話が繋がらないお返事をいただいた（決まりは _offer_unlinked の1か所）。会話AIが受け答えで
          ひとこと応え、そのあとプログラムが申し出を伺い直す（_offer_again）。✅ 2026-11-09 日本時間7時57分50秒 ユーザー（お言葉は _offer_unlinked）"""
        if self.callee_turns in (self.question_answered_turn, self.asked_skip):
            return None
        if self.concern_turn == self.callee_turns:
            return "concern"
        if self._hesitation() == "help":
            return "help"
        if self._may_ask_back() and asks_us([self.check.get(k) for k in JUDGMENTS]):
            return "asked"
        if self._offer_unlinked():
            return "offer"
        return None

    def _offer_unlinked(self) -> bool:
        """申し出のシステムの質問（OFFER_ROLES＝詳しい者より改めて N4・締める前の申し出 N6）に、会話が繋がらないお返事（「うちは月曜が定休日なんですよ。」）を
        いただいたか（唯一の決まり＝応える番の種類 _asked_kind の offer）：いまの質問の最初の答えの番（言い直しの返事待ちではない）で、まだ伺い直していなくて
        （Item.offer_again。聞き取れないお返事への聞き直しのあとでも1回）、伺い直す申し出の中身（こちらがすることの言葉 Question.do_words の「はい」）があり、
        判定の読みがそろって決められない（どの答えも指していない。「わからない」は入れない＝答えたくない・分からないの道）・迷っていない（_hesitation）・電話を
        終える言葉に寄っていない・聞き取れている・こちらへの問いではない（_plain_reply）。
        ✅ 2026-11-09 日本時間7時57分50秒 ユーザー：「1.(a)（お勧め）会話AIがお客様の言葉にひとこと応えてから、申し出を伺い直す。例：「さようでございますか。
        それでは月曜日を避けて、詳しい者よりお話しさせていただくということでよろしいでしょうか？」いただいてもと同じことを言わない」（⛔ 直す前は、聞き直しの
        1か所 _ask_again が「はい、恐れ入ります。詳しい者からのお話のご希望をもう一度お聞かせいただいてもよろしいでしょうか？」と伺い直していた＝お客様のお話に
        応えずに、同じことをもう一度伺う形。⛔ 6時33分57秒のご指摘のあと、私は帳面に「(1) 会話AIがひとこと応え (2) 申し出を「…させていただいてもよろしいで
        しょうか？」で伺い直す」と書き、最初の申し出と同じ終わりをくり返す例を出していた）"""
        qid = self.current
        if not self._may_ask_back() or self.awaiting_confirm is not None or qid is None or self.check_for not in (None, qid):
            return False
        # こちらが応えたあと（選ぶ助け・ご質問への答え・受け答え）のお返事と、迷っているお客様への応じ方の途中（_hesitation）は当てない（それぞれの道＝_resume の
        # 最終の確かめ・質問に戻る）。⛔ 入れる途中、流れの制御から出したお返事の種類ごとの表で見つけた：選ぶ助けのあとの「なるほど。」（判定は決められない）にも
        # この番を当て、会話AIに申し出の伺い直しをさせていた
        if self.own_pending or self.ai_pending or self.hesitating.get(qid):
            return False
        it = self.items[qid]
        # 申し出の質問（Question.offered）か、方法を選んでいただく質問（N5・C11・W1）で、提案できる方法がある（決まりは _proposal_of の1か所）。方法を選んで
        # いただく質問も同じ番・同じ伺い直し（✅ 2026-11-12 日本時間14時58分17秒 ユーザー「アで！」。⛔ 直す前は「はい、恐れ入ります。ご希望の方法をもう一度
        # お聞かせいただいてもよろしいでしょうか？」）
        if it.offer_again or self._proposal_of(qid) is None:
            return False
        readings = [self.check.get(k) for k in JUDGMENTS]
        return {str(r) for r in readings if r is not None} == {UNDECIDED} and self._plain_reply(readings) and self._hesitation() is None

    @staticmethod
    def _asked_blocks(name: str, args: dict) -> bool:
        """答えの段で尋ねられた（_asked_now）ときに受け付けない報告か（唯一の決まり：入口 _apply_one が止める所と、判定を待つか wants_checks の両方が
        使う）。受け付けるのは、聞かれたことへの答え方（replay_opening の、代わった方へ・質問に戻るのほか）と、電話を終える報告（判定で決める）だけ。"""
        return not (name == "request_end" or (name == "replay_opening" and args.get("part") not in ("handover", "resume")))

    def _mark_answered(self) -> None:
        """いまの相手の発話の質問には、もう答えた（唯一の場所。_asked_now・_advance が読む）。答えた操作の中で付ける（その操作の返事の知らせ＝_now が、
        もう答えた番として作られる）。⛔ 2026-11-06 日本時間5時56分55秒のご質問のあとの読み直しで見つけた：操作のあとで付けていたので、操作の中で作る
        知らせがまだ「答える番」と言い、そのとおりに動くと、同じ発話に2回答えた"""
        self.question_answered_turn = self.callee_turns

    def _await_reply(self, kind: str) -> None:
        """相手に応えて、相手の返事を待ち始める（唯一の場所。自分の言葉の答え・受け皿の一言・想定問答の録音で待つ答え・「AIですか」に応える一言・声で
        答えたと分かったとき）。kind＝応えた部分（OWN_PARTS の中の名前・「AIですか」は ai）。答えの段で尋ねられた問い（_asked_now）に答えたなら、
        印は ASKED_REPLY（お客様の返事が答えなら、質問に戻らずに受け取る＝_resume・_answer_in_reply）。この操作で待ち始めたこと（reply_started）は、
        操作のあとに待つ印を消すかの決まり（_apply_one）が読む。"""
        if kind == "ai":
            self.ai_pending = True
        else:
            # 尋ねられて答える番か（_asked_now）は、選ぶ助けを伝えた印を立てる前に読む（印を立てると _hesitation が help でなくなる）
            asked, helping = self._asked_now() is not None, self._hesitation() == "help"
            if helping:
                # 迷っているお客様に選ぶ助けを伝えた（印は選ぶ助けを伝えた発話＝このあとも迷っていれば、まだ伺っていない一言・まだお伝えしていない点＝_hesitation）
                self.hesitating.setdefault(self.awaiting_confirm or self.current, {})["helped"] = self.callee_turns
            self.own_pending = ASKED_REPLY if asked else kind
        self.reply_started = True
        self.reply_said = None   # 応えた中身は、この操作の返事ができた時に覚える（_apply_one）
        self._mark_answered()

    def _may_ask_back(self) -> bool:
        """答えの段（いまの質問か言い直しへの返事を待っている）で、判定の読みしだいで、いまのお客様の言葉がこちらへ尋ねた言葉になりうるか（_asked_now の
        前提・判定を待つかの決まり wants_checks の1か所。尋ねたかは判定の読みで決まる＝queries_us。言葉の形にかかわらず判定が届いてから当てる＝判定の届く
        速さで、同じ言葉への動きが変わらない）。この言葉の答えをもう受け取って、添えられた質問に答える番（after_answer＝_question_first の callee_asked）なら
        当たらない（その質問は答えに添えられた質問の道が受け持つ）。⛔ 2026-11-08 日本時間8時台まで、言葉が「？」で終わる文を含むときだけ当てていた
        （✅ 8時55分08秒 ユーザー「(イ) 判定に「こちらへ尋ねた問いがあるか」を1項目足し、1か所で読む」）"""
        return (self.phase in ("asking", "rechecking") and not self.answering   # 出た方の第一声は判定に出さない（judge_context と同じ）
                and (self.current is not None or self.awaiting_confirm is not None) and self.after_answer is None)

    def _asked_note(self, asked: str) -> str:
        """答えの段で尋ねられた（_asked_now）ときに、いま求めていること（AIへの知らせの文。_now の1か所）：報告はせずに、聞かれたことに合う答え方
        （replay_opening の1か所＝想定問答の録音・自分の言葉）で答え、お客様の返事を待つ（いまの質問・言い直しへは、そのあとプログラムが戻る）。
        いまの質問についてこちらの考えを尋ねられたら、自分で答える質問として、業務の情報の範囲で答える（目的から遠ざかる向きはすすめない＝✅ 2026-11-03
        ユーザー「最終目的に繋がるクロージングはマイナスの言葉で締めない」）。"""
        q = self.questions[self.awaiting_confirm or self.current]
        now = f"言い直し「{self.awaiting_say}」" if self.awaiting_confirm is not None else f"いまの質問 {q.id}「{q.text}」"
        away = "・".join(f"「{m}」" for c in q.away if (m := q.meanings.get(c)))
        avoid = f"{away}のほうはすすめません。" if away else ""
        kind = self._asked_kind()
        if kind == "offer":
            # 申し出への会話が繋がらないお返事にひとこと応える番（_offer_unlinked）。ひとことのあとの申し出の伺い直しはプログラムが組む（_offer_again）。条件の文字は
            # 会話AIが書く（値の文字と同じ＝伺い直しへのお客様の「はい」で確かめる）
            # 方法を選んでいただく質問（N5・C11・W1）は、方法を必ず業務の方法から選ぶ（_proposal_of の1か所。✅ 2026-11-12 日本時間14時58分17秒「アで！」）
            ways, method = "・".join(self._proposal_of(q.id)[1]) or "なし", q.offered is None
            return (f"いまは、相手のお話（「{asked[:40]}」）に、ひとこと応える番です。相手は、{now}に、{'どの方法とも言わずに' if method else 'はい・いいえではない'}別のお話をしました（意味の判定）。"
                    f"報告はまだしません。replay_opening を「{PART_LABEL['talk']}」で呼び、相手のお話を受け止めるひとことを「{ARG_LABEL['answer']}」に書きます（ご要望・"
                    f"気にかかっていることには「かしこまりました。」、ご事情のお話には「さようでございますか。」など、短く1文。問いかけ・約束・申し出の言い直しは入れません。"
                    f"返す文が使えないときの一言も「{ARG_LABEL['fallback']}」から選びます：{TALK_CHOICES}）。続けてプログラムが、相手のお話に{'業務の方法' if method else '申し出'}で応える提案"
                    f"「それでは（{ARG_LABEL['terms']}、{ARG_LABEL['added']}、）（{ARG_LABEL['topic']}）（申し出の中身。{ARG_LABEL['way']}はその中に入ります）ということでよろしいでしょうか？」"
                    f"（こちらから足したものがあれば「ということではいかがでしょうか？」）を組みます。相手のお話にあることだけから、次を書きます（無いものは書きません）："
                    f"「{ARG_LABEL['terms']}」＝相手が言ったご都合（曜日・日にち・時間帯など。例：定休日のお話なら「月曜日を避けて」。「〜て」「〜で」「〜に」で終える）。"
                    f"「{ARG_LABEL['added']}」＝相手は言っていないが、相手のお話に合わせてこちらから足すご都合（例：平日の昼間は外に出ているお話なら「夕方以降に」。同じ終わり）。"
                    f"「{ARG_LABEL['topic']}」＝相手が気にかけていること（料金・内容など）に応えて詳しい者がお話しする中身（例：料金のお話なら「料金の面も含めて」。業務の情報に"
                    f"あることだけ。「〜を」「〜て」で終える）。「{ARG_LABEL['way']}」＝"
                    + (f"相手のお話に合う方法を、業務の方法（{ways}）から1つ必ず選び、その名前をそのまま。" if method else
                       f"相手が言ったご希望の方法が業務の方法（{ways}）にあれば、その名前をそのまま。")
                    + f"相手のお話に、このあと伺うこと（業務の方法に無いご希望の方法・連絡先など）の答えがあれば、"
                    f"提案には書かずに「{ARG_LABEL['rest']}」に、相手の言ったとおりに書きます。")
        if kind == "concern":
            # 本題で本心を伺った一言に、気になる点を言っていただいた（_asked_kind の concern）：言っていただいた点を受け止め、業務の情報の範囲で応える（ご承認の
            # 手順のご説明＝_point_note と同じ向き。押さない・言い返さない）。応えたあと、お返事がいまの質問の答えなら受け取る（_answer_in_reply）
            return (f"いまは、相手が言ってくださった、こちらの話し方・進め方・ご案内についての気になる点（「{asked[:40]}」）に応える番です。報告はまだしません。"
                    f"言っていただいたことを受け止める短い一言（お詫びやお礼）を添えて、その点に応えます：想定問答に答えがあれば、その答えを流します。こちらが知って"
                    f"いること（業務の情報）にあれば、その範囲で、自分の言葉で短く応えます（お客様の誤解なら、正しい中身をご説明します。言い訳・言い返し・押し付けは"
                    f"しません。{avoid}）。知っていることに無ければ、答えを作らずに「{PART_LABEL['no_answer']}」で応えます（{ARG_LABEL['fallback']}に、言っていただいた"
                    f"ことの種類＝{POINT_KIND_CHOICES}、を選びます）。どれも replay_opening で呼びます。応えたら、相手の返事を待ちます（{now}へは、そのあと"
                    f"プログラムが戻ります）。")
        if kind == "help":
            # 迷っているお客様に選ぶ助けを伝える番（_hesitation の1か所）。✅ 2026-11-07 日本時間13時05分06秒 ユーザー（_hesitation に引いたお言葉）
            notes = "／".join(f"{label(c)}＝{n}" for c, n in q.option_notes.items())
            source = (f"選択肢ごとの良い点・気をつける点（{notes}）から" if notes else
                      "相手が迷っている点について、業務の情報の範囲で")
            if self.hesitating.get(self.awaiting_confirm or self.current, {}).get("helped") is not None:
                # 選ぶ助けを伝えたあとも迷っている（何回でも保留で締めない＝22時02分54秒）：前にお伝えしたことを繰り返さない
                source = "前にお伝えしたことの繰り返しではなく、まだお伝えしていない点を、" + source
            # 言い方：良い点は、説明の言い切りで終えず、お客様にすすめる気持ちが伝わる言い方で（✅ 2026-11-07 日本時間23時19分05秒 ユーザー：ご報告の例
            # 「…カード会社のポイントもつきます。」を「・「カードですとお振込の手間がかからず、カード会社のポイントもつきますよ。」」に直していただいた。
            # 13時05分06秒のお言葉の例「…そういう点も 考慮されるといいかもしれませんね」と同じ向き）
            return (f"いまは、相手（「{asked[:40]}」）に、選ぶ助けになることをお伝えする番です。相手は、{now}について、どれにするか迷っています・まだ決めていません"
                    f"（意味の判定）。報告はまだしません。「{PART_LABEL['own']}」で、{source}、選ぶ助けになることを自分の言葉で短くお伝えします"
                    f"（良い点は、説明で言い切って終えず、相手にすすめる気持ちが伝わるやわらかい言い方で＝文の終わりを「〜ますよ。」「〜かもしれませんね。」"
                    f"など。どれかに決めつけない・急かさない。{avoid}答えのもとにした業務の情報の文が無ければ、使えないときの一言を選びます）。伝えたら、"
                    f"相手の返事を待ちます（{now}へは、そのあとプログラムが戻ります）。")
        return (f"いまは、相手の問い（「{asked[:40]}」）に答える番です。相手は、{now}に答える代わりに、こちらに尋ねています（意味の判定）。"
                f"報告はまだしません。聞かれたことに合う replay_opening で答えます（{self._answer_choices()}）。{now}について、どれがよいか・"
                f"したほうがよいかなど、こちらの考えを尋ねられたら、「{PART_LABEL['own']}」で、業務の情報の範囲で、自分の言葉で短く答えます"
                f"（{avoid}答えのもとにした業務の情報の文が無ければ、使えないときの一言は選びません）。答えたら、相手の返事を待ちます"
                f"（{now}へは、そのあとプログラムが戻ります）。")

    def _asked_skip(self, problem: str, drafts: dict, checked: dict) -> dict:
        """答えの段で尋ねられた問い（_asked_now）への答えの文が使えない（中身を確かめられない・書き直しを使い切った）ときの唯一の返事：受け皿の一言では
        応えずに、尋ねられる前の流れのとおりに進める（会話AIに、お客様の言葉を、いまの質問の答え・言い直しへの返事として報告し直させる＝流れの制御の次の一言
        （言い直しと同じ一言・確かめの一言）が、尋ねられたことに応える）。尋ねられたのは、いまの質問についてのこちらの考え＝答えられなかったらおかしい問い
        （✅ 2026-10-22 ユーザー（5回目）：「お客様から見ても、それは答えられないだろうなと思うような質問ならそれでよいが、それは答えられなかったらおかしい
        だろうと思われる内容…では、別の対処が必要」）。お客様の問いは、答えられなかった問いとして依頼者へ渡す記録に残す（callee_question）。"""
        self.asked_skip = self.own_turn = self.callee_turns
        self._question_note({"answered": False, **drafts, **checked})
        return self._reject(f"この文は声に出しません（{problem}）。この問いには答えの文では応えずに、相手の言葉を、いつもの報告で報告し直します"
                            "（尋ねられたことには、プログラムの次の一言が応えます）。")

    def _offer_while_asking(self, name: str, args: dict) -> bool:
        """こちらに尋ねている言葉への報告が、問いの形の申し出か（申し出の報告は止めない＝apply の入口）：代わりの方・後任の方の報告／相手の申し出
        （折り返し・伝言・ほかの手段）を付けたかけ直しの申し出／お呼びした方が出られないという判定の読み（不在）がある、かけ直しの申し出。
        ⛔ 申し出も読み（不在）も無いかけ直しの申し出は止める：純粋な質問（「どちら様でしょうか？」）を、AIが申し出と取り違えて報告すると、
           質問に答えずに先へ進む（目的の人かを確かめる一言を流す）ため。"""
        if name == "report_party":
            return args.get("party") in STANDIN_PARTIES
        if name == "request_callback":
            return bool(args.get("offer")) or self._away_now() or bool(self._target_readings()[2])
        return False

    def _asking_note(self) -> str:
        """電話に出た方が、こちらに質問している：目的の人かはまだ決めず（確かめも用件も流さない）、聞かれたことに録音で答える（唯一の言い方）。
        ✅ 2026-10-22 ユーザー：「相手が喋っていても、それが質問だった場合の対処は？」"""
        return (f"相手の言葉（「{(self.callee_said or '').strip()[:30]}」）は、こちらへの質問です（意味の判定）。目的の人かはまだ決めません"
                f"（確かめも用件も流しません）。聞かれたことに合う replay_opening で答えます（{self._answer_choices()}・"
                f"どちらの〇〇か・聞き取れなかった様子＝「{PART_LABEL['all']}」）。問いの形の申し出（「折り返すようお伝えしましょうか」"
                f"「代わりに私が伺いましょうか」など）は、質問ではなく申し出です。{offers_rule()}聞かれていなければ、もう一度報告してください。")

    def _away(self, ai: str = "away", why: str | None = None, offer: str | None = None) -> dict:
        """お呼びした方がいま電話に出られない：繋がず（名乗り・用件・質問を流さない）、お呼びし直しもしない。かけ直しの申し出を、AIが聞いた
        言葉に合う理由で（AIは言葉の中身を聞いている＝ご本人が「今は手が離せない」なら「あとで」）。断られたときは終える（唯一の場所）。
        why：AIがかけ直しを申し出ていれば、その理由（request_callback）＝そのまま受け取る。申し出ていなければ、申し出るように返す。
        AIが代わりの方・後任の方と報告していれば、それも受け取る（応じ方は業務設定＝_not_available）。
        ⛔ 2026-10-22 ユーザー：「AIが何と報告していてもの中に、重要な報告はないんですか？」＝前は理由を「不在」に決めつけていた"""
        if why or ai in STANDIN_PARTIES:
            return self._not_available(ai, why, offer, {"ai"} | self._target_readings()[2])
        if any(x["reply"].get("away") and self.target_from <= x["callee_turns"] < self.callee_turns for x in self.log):
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
        turns = [t for t, v in self.calling_judged.items() if any(says_away(x) for x in v.values()) and t >= self.target_from]
        return self.calling_words.get(max(turns), "") if turns else (self.callee_said or "")

    def _away_text(self) -> str:
        """お呼びした方がいま電話に出られないときに、AIへ伝える文（_away・unreported_note の1か所）。"""
        return (f"相手の言葉（「{self._away_words().strip()[:30]}」）は、お呼びした方がいま電話に出られないという意味です（意味の判定）。"
                f"名乗り・用件・質問は流さず、お呼びし直しもしません。{offers_rule()}営業をお断りされた・"
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
        # 後任の方へ切り替える前の読みは、前にお呼びした方のもの（数えない＝target_from）
        seen = {t: v for t, v in every.items() if t >= max(self.speaker_from, self.target_from)}
        after = self.asked_target_turn if self.asked_target is not None else self.callee_turns
        # こちらに尋ねた発話の「ご本人」は数えない（答えを待つ発話で、誰かを決める材料にしない。⛔ 2026-10-22 G-59：3.5 Flash Lite は
        # 「岡田ですね。失礼ですが、どちら様でしょうか？」を3回とも「ご本人」と読んだ＝尋ねているかが先なので、その場では繋がないが、読みが
        # 残ると、答えたあとのAIの読み違いと2つそろう）。不在の読みは残す（お呼びした方の事実）
        asking = {t for t, v in self.calling_asks.items() if v == ASKS_YES}
        # かけ直しの質問の途中で、目的の人かを確かめに戻ったあと（_target_back）は、戻る前の不在の読みで繋ぐのを止めない（お呼びした方が
        # 戻ってきたかを確かめている。ご本人でなければ、お呼びし直さずにかけ直しの続きへ＝_call_target）。⛔ 2026-10-22 G-57 tc26（かけ直しの
        # 途中で戻る・S2）：確かめへの「はい、そうです。」の判定が届かず（3.5 Flash Lite が止まっていた）、前の「席を外しておりまして」の
        # 不在の読みで繋がずに、ご本人にかけ直しの質問を流した
        since = max(self.back_turn if self.back_turn is not None else -1, self.target_from)
        return ({k for t, v in seen.items() if t not in asking for k, x in v.items() if x == TARGET_IS_THERE},
                any(v for t, v in seen.items() if t > after),
                {k for t, v in every.items() if t >= since for k, x in v.items() if says_away(x)})

    def _successor_read(self) -> set[str]:
        """はじめの場面で、いまの言葉（出揃ったもの）を「電話に出ている方は後任の方」と読んだ判定の名前（唯一の場所）。どれか1つでも「ご本人」と
        読んでいれば空（後任の方の道へは進まない＝取り消せない側へは1つの読みで止める）。後任の方をお呼びしたあとは、後任の方かは読みそのもの
        （facts_reading）なので空。"""
        now = self._judged_now() if self.words_complete and not self.successor else {}
        return set() if TARGET_IS_THERE in now.values() else {k for k, v in now.items() if says_successor(v)}

    def _proxy_read(self) -> set[str]:
        """いまの言葉（出揃ったもの）を「話している方は代理の方」と読んだ判定の名前（唯一の場所＝says_proxy）。AIの「代わりの方」「後任の方」の
        報告は、これとそろったときだけ受け取る（_calling_reported の (6)）。確からしさが線より下の読み（on_unsure）は数えない。"""
        now = self._judged_now() if self.words_complete else {}
        return {k for k, v in now.items() if says_proxy(v)}

    def _handed(self) -> set[str]:
        """いま電話口にいる方は、前の方が「お呼びした方は出られない」と伝えたうえで代わった方か（唯一の場所）：前の方の言葉（いまの方に替わる前・
        お呼びし直したあとの発話）を「不在」と読んだ判定の名前（無ければ空＝代わった方ではない）。AIの読みだけでは決めない（代わりの方として
        用件を流すのは、取り消せない側）。"""
        # お呼びし直したあとの言葉から（呼ぶ一言を流した発話の読みは、お呼びし直す前のもの）
        since = max(self.back_turn if self.back_turn is not None else -1, self.target_from, max(self.target_called, default=-1) + 1)
        if self.speaker_from <= since:
            return set()
        return {k for t, v in self.calling_judged.items() if since <= t < self.speaker_from for k, x in v.items() if says_away(x)}

    def _doubted(self) -> bool:
        """いま電話口にいる方に、繋ぐのを止める弱い読みがあるか（on_unsure）：いまの言葉（出揃ったもの）の分と、確かめたあとの発話の分
        （遅れて届いた分も）。確かめる前の弱い読みは、確かめた理由なので数えない（_target_readings の「確かめのあと」と同じ区切り）。"""
        after = self.asked_target_turn if self.asked_target is not None else self.callee_turns - 1
        late = any(kinds for t, kinds in self.calling_doubted.items()
                   if t > after and t >= max(self.speaker_from, self.target_from) and t < self.callee_turns)
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
        return (f"目的の人か・取り次ぎか・いらっしゃらないか・申し出か（代わりの方・後任の方・折り返し・伝言・ほかの手段）。名乗り・用件などを"
                f"聞かれたなら、それに合う replay_opening で答えます（{self._answer_choices()}）")

    def _connect(self, read: set[str], who: str = "target", prefix: str = "") -> dict:
        """目的の人に繋がった（DESIGN.md 4-3）：その方にまだ届いていない名乗り・用件を流してから、最初の質問を渡す（唯一の場所）。
        取り次がれて代わった方（passed_on）には、あいさつ・名乗り・用件の全部（呼んだときの名乗りは、取り次いだ方にしか届いていない）。
        最初に出た方が目的の人なら、まだ届いていない分だけ（用件。録音の途中で止めて届いていない名乗りがあれば、それも）。
        録音で流す形では名乗り・用件を録音で流し、最初の質問は _advance が決める（録音か、AIが言うか）。AIが話す形では、AIが言う。
        read＝繋いだ読み（「ai」＝AIの報告・判定の名前）。通話の事実（connect）に残し、繋いだあとに届いた判定も足す（on_check。通話後の検証が、
        判定なしで繋いだ通話を確かめる＝DESIGN.md 6-12）。繋ぐときまでに届いていた、繋いだ発話そのものの判定は turn_reads に残す（「ご本人」以外の
        読みがあれば、CL-2 が通話後に確かめ直す＝CL-1.md 4-34 の 4-127 (a)・CL-2 の 4-154。届いていなければ書かない）。who＝繋いだ方（目的の人 target・後任の方 successor・代わりの方 substitute。依頼主へ渡す
        答えが、誰の答えか＝✅ 2026-10-22 ユーザー：代わりの方とも話す）。"""
        who = "successor" if who == "target" and self.successor else who
        self._note("connect", read=sorted(read), judging=self.judging, who=who, **({"turn_reads": r} if (r := self._judged_now()) else {}))
        if who != "target":
            # 代わりの方・後任の方には、お呼びした方のお名前の読みは聞かない（その方の名前ではない＝reading_question）
            self.order = [qid for qid in self.order if not self.questions[qid].reading_of]
        whom = {"target": "目的の人", "successor": "後任の方（目的の人）", "substitute": "代わりの方（判断いただけるお立場の方）"}[who]
        if self.callback_why:
            # かけ直しの質問の途中で、目的の人に繋がった（_target_back）：かけ直しは要らない（聞いた答えは通話の事実に残したまま。締めはふつう）
            self.facts["callback"][-1]["connected"] = True
            self.callback_why, self.callback_ids, self.back_turn = None, [], None
        keys = [k for k in OPENING_ORDER if self.target.get(k) and (self.passed_on or k in self.opening_unheard)
                and not (k == "greeting" and self._greeted())]  # 判定を待つ間に流したあいさつは、もう流さない（hold_play）
        self.phase, self.opening_unheard, self.passed_on = "asking", [], False
        if who != "substitute":
            self._resume_cut()   # ご本人・後任の方：前の通話で伺っている途中で切れた所から（続きの電話の始めの段を作る1か所。どの道で繋がっても同じ）
        else:
            self.resume_due = False   # 代わりの方には続きの電話の始めの段を作らない
            if self._resumed_call():
                self._start_over()   # 続きの電話の代わりの方：前の通話の答え（ご本人の答え）で決めていたことを外し、営業の最初から伺う
        # まずお詫び（前の電話の事情。予約証の apology）：お呼びした方ご本人にだけ。名乗りが要るなら名乗りのあと、用件・質問の前（共通の一言の録音）
        told = self.told_again if who != "substitute" else None
        if told is not None:
            keys = [k for k in keys if k != "purpose"]   # 申し伝え直す電話：お詫び・申し伝える一言のあとは残りの質問か最後のあいさつ（用件の一文は流さない＝(b) のお言葉の並び）
        sorry = self._sorry_lines() if who != "substitute" else []
        at = next((i + 1 for i, k in enumerate(keys) if k == "intro"), 0)
        lines = [self._purpose_play(who) if k == "purpose" else {"kind": "opening", "part": k, "text": self.target[k]} for k in keys]
        lines[at:at] = sorry
        if self.apology_call:
            # お詫びの電話：用件・質問の代わりに、お詫びの録音を流して、お返事を待つ（業務の質問はしない＝いまの質問を空にする。⛔ 2026-10-24 の見直し
            # （調べ役）：代わりの方に繋いだとき、判断いただけるお立場かの質問が「いまの質問」に残り、締められなかった）
            lines = lines + [self._apology_play()]
            self.apology_asked = self.apology_played = True
            self.current, self.order, self.substitute_q = None, [], None
            return {"result": "next", "silent": True, "play": [x if x["kind"] != "opening" or not self.opening else {**x, "text": self.opening[x["part"]]}
                                                               for x in lines],
                    "message": f"{prefix}{whom}に繋がりました。{played_note(''.join(x['text'] for x in lines))}何も言わずに、相手のお返事を待ってください。"
                               f"お返事を聞いたら request_end を{ARG_LABEL['reason']}"
                               f"「{END_LABEL['done']}」で呼びます。"}
        texts = "".join(x["text"] for x in lines)
        if not lines:
            return self._advance(f"{prefix}{whom}に繋がりました。", handled=True)
        if not self.opening:
            # 名乗りの録音の無い形（会話AIが名乗る）も、質問の頭は同じ1か所（_relead_next）で決める（⛔ 2026-11-06 日本時間23時22分41秒のご質問の見直し A8：
            # この形だけ通らず、続きの電話の最初の質問の頭が「続きまして」のままだった）
            r = self._advance(f"{prefix}{whom}に繋がりました。まず、{say_note(texts)}続けて、", handled=True)
            return self._relead_next(r, lines, resumed=self._resumed_call())
        r = self._advance(f"{prefix}{whom}に繋がりました。{played_note(texts)}続けて、", handled=True)
        r = self._prepend_played(r, [x if x["kind"] != "opening" else {**x, "text": self.opening[x["part"]]} for x in lines])   # 名乗りの部分にだけ文を埋める（お詫びの録音＝recorded・承認したお詫び＝apology は文を持つ）
        # 前に足した用件などの文の頭と、質問のつなぎの言葉を重ねない・続きの電話なら最初の質問の頭は QUESTION_RESUME_LEAD（_relead_next の1か所）
        return self._relead_next(r, [], resumed=self._resumed_call())

    def _wish_reported(self, name: str, args: dict) -> tuple[str, dict]:
        """ほかのご希望を伺っている（W1 がいまの質問）ときの、ほかの手段の申し出（request_callback の「ほかの手段」・メール・ファクス・郵送）を、W1 の答え
        （その方法のご希望＝wish_option。方法を言わなければ、ほかのご希望）として受ける唯一の場所（_apply_one の入口）：W1 の答えの報告と同じ1か所
        （_wish_step）で、連絡先を伺い、申し伝えて続ける（ほかの手段で締めない）。当たらなければ、そのまま (name, args)。CL-1 の 11-03 のご判断の (3)。
        ⛔ 見直しで見つけた：「じゃあメールで送ってもらえますか。」を AI がほかの手段のかけ直しで報告すると、メールアドレスを伺って締め、残りの質問を伺わず、
        ほかのご希望の事実も残らなかった（答えの報告なら申し伝えて続ける＝同じお答えが、AIの選ぶ操作で分かれていた）。"""
        w, wid = self.wish, CALLBACK_IDS["other_wish"]
        if (name != "request_callback" or w is None or w["outcome"] is not None or w["stage"] != "ask" or self.current != wid
                or self.awaiting_confirm is not None or self.phase not in ("asking", "rechecking")):
            return name, args
        offer = args.get("offer")
        if offer not in MEANS and args.get("why") != "other_means":
            return name, args
        cond = wish_option(self.questions[w["q"]], offer) if offer in MEANS else WISH_OTHER
        return "record_answer", {"question_id": wid, "condition_id": cond}

    def _op_request_callback(self, a: dict) -> dict:
        """かけ直しの申し出（DESIGN.md 6-2）。理由ごとに、システムが足す質問（callback_question）を、ふつうの質問と同じ流れ（record_answer・
        言い直して確かめる・聞き返し）で聞き、取れたら締める（ふつうの締め）。残りの業務の質問は聞かない（目的の人ではない・いまは話せない相手）。
        聞いた番号へかけるか・いつかけるかは、門と次の動き（CL-2）が決める：会話の側は、聞いたとおりに通話の事実へ残すだけ。
        言い直しの返事待ちだった業務の質問は、確かめずに終わる（「-」）。念のための確認はしない（相手は話せる状態ではない）。
        offer＝相手の申し出（折り返し・伝言・メール・ファクス・郵送）。メール・ファクス・郵送の申し出は、理由「ほかの手段」として受け取る。"""
        why, offer = a["why"], a.get("offer")
        if why == "unavailable" and not self.unavailable_asked:
            return self._reject(f"「{CALLBACK_LABEL['unavailable']}」は、取り次げないご事情かをプログラムが伺ったあとの返事だけに使います。"
                                "聞いた言葉に合う理由で呼んでください。")
        if offer in MEANS:
            why = "other_means"
        elif why == "other_means" and offer:
            return self._reject(f"「{OFFER_LABEL[offer]}」の申し出は、{ARG_LABEL['why']}「{CALLBACK_LABEL['other_means']}」では使いません"
                                f"（ほかの手段の申し出は「{OFFER_LABEL['email']}」「{OFFER_LABEL['fax']}」「{OFFER_LABEL['post']}」。どれでもなければ付けません）。")
        if self.phase == "closing":
            return self._reject("通話はもう締めに入っています。")
        if self.phase == "calling":
            # 呼んでいる段：AIの「お呼びした方はいま出られない」という読み＝電話口の方が誰かを決める1か所で、判定に当ててから受け取る
            # （取り次ぎの最中は待つ・不在の読みがあれば受け取る・判定と食い違えばお呼びし直すか確かめる）。⛔ 2026-10-22 G-57 tc24（確かめに
            # いいえ・L4）：ここで判定を見ずに受け取っていたので、「いいえ、私は山田と申します。」（判定は2つとも「別の方」）でかけ直しへ進んだ
            return self._calling_reported("away", why=why, offer=offer)
        if why == "departed":
            return self._reject(f"いまお話ししている方とは、もう繋がっています（{CALLBACK_LABEL['departed']}、ではありません）。")
        if (w := self._wait_now()) is not None and (routed := self._wait_route(w, {"ai"} | self._wait_read(), why=why, offer=offer)) is not None:
            return routed   # 決めるのは判定（会話AIの申し出は判定を待つきっかけだけ＝待つ読みなら待つ・決まらなければ1回伺う）
        if why == "later" and w is None and self.judging and any(k in self.heard_check for k in JUDGMENTS):
            # 答えの段の「あとで」（お客様からの折り返しの申し出も）は、判定が読める（選べるもの「あとでかけ直す」「お客様から折り返す」）：判定の読みが届いていて、
            # 待つ・かけ直すと読まなければ、会話AIの申し出だけでは受け取らない（会話AIの報告は判定を待つきっかけだけ＝✅ 2026-11-01 日本時間17時04分 (a)「OK」）。
            # ⛔ 2026-11-06 日本時間23時22分41秒のご質問の見直し（W6）：受け取ってお礼の一言も言っていた（判定の1つがかけ直すと読めば1回伺う＝判定が会話AIに
            # 同意するほど慎重になる逆向きだった）
            return self._reject("判定は、いまの言葉を、あとでかけ直してほしい・お客様から折り返すとは読んでいません。かけ直しは受け付けません。"
                                "何も言わずに、聞いた答えのとおりに record_answer で報告します（「わからない」「答えたくない」も答えです）。")
        if self.callback_why and not (why == "other_means" and self.callback_why != "other_means"):
            # 同じ申し出は1通話1回（ほかの手段の申し出だけは、かけ直しの質問の途中でも受け取る＝「戻りは分からないので、メールで」）
            return self._reject(f"かけ直しの申し出（{CALLBACK_LABEL[self.callback_why]}）は、もう受け付けています。")
        return self._callback(why, {"ai"}, offer=offer)  # 繋いだあと：いま話している方の申し出（電話口の方が誰かは、もう決まっている）

    def _not_available(self, ai: str, why: str | None, offer: str | None, read: set[str]) -> dict:
        """お呼びした方と、いまは話せない（不在・長期の不在・あとで・別の番号へ・在籍していない・ほかの手段）、または電話口の方が代わりにお話しうる
        （代わりの方・後任の方）と決まった：相手の申し出と業務設定で、応じ方を決める唯一の場所（呼んでいる段の _calling_reported・_away から）。
        ✅ 2026-10-22 ユーザー：「代わりの方・伝言・折り返し・在籍していない・ほかの手段・長期の不在があるのが分かっているなら、それを想定の録音に
        すべきじゃないですか？」⇒ 応じる言葉はどれも決まった録音（phrases の OFFER_THANKS・DEPARTED_ACK・SUCCESSOR・CALLBACK_ASK）。
        read＝受け取った読み（「ai」＝AIの報告・判定の名前。通話の事実に残す）。"""
        if offer in MEANS:
            why = "other_means"
        if ai == "substitute":
            return self._substitute(read, why)
        if ai == "successor" or why == "departed":
            return self._departed(confirm=ai == "successor", read=read, why=why)
        return self._callback(why or "absent", read, offer=offer)

    def _substitute(self, read: set[str], why: str | None = None) -> dict:
        """電話口の方が、お呼びした方の代わりにご用件を伺うと申し出た（代わりの方。上司の方・同じ部署の方など）：業務設定で決める（唯一の場所）。
        ✅ 2026-10-22 ユーザー：「代わりの方が聞いてくれると言っているのに、それを無下にするんですか？単なる引き継ぎなら問題ないかもしれないが、
        代わりの人が同等の決定権者や上司なら、完全に失礼なふるまいです」「代わりになる決定権者なのかの判断が必要。判断可能な人なら、話す。
        +業務ごとに選べる」
        - 判断いただけるお立場の方なら話す（既定 decision_maker）：お礼の相づち「ありがとうございます、」（その方がまだ名乗りを聞いていなければ、
          あいさつ・名乗りから）・まだ聞いていなければ用件の録音・続けて「恐れ入りますがこの件についてご判断いただけるお立場の方でいらっしゃい
          ますでしょうか？」（システムが足す質問 S1。答えはふつうの質問と同じ確かめ＝DESIGN.md 1-1）。答えで決める（_substitute_decided）
        - 目的の人だけと話す（target_only）：お礼を言い、こちらからかけ直す形にして、戻る時刻を聞く（_callback）
        - どなたでも話す（anyone）：代わりの方として繋ぐ（_connect）"""
        policy = self.policies["substitute"]
        self.substitute_why = why if why in AWAY_WHYS else "absent"  # お話しできなかったときに聞くこと（お休みなら次のご出社・長期の不在なら戻る日）
        self._note("substitute", policy=policy, read=sorted(read))
        if policy == "target_only":
            return self._callback(self.substitute_why, read, thanks=True)
        if policy == "anyone":
            self.order = list(self.business_ids)
            return self._connect(read, who="substitute") | {"thanks": True}
        self._keep_calling_turn()
        keys = [k for k in OPENING_ORDER if self.target.get(k) and not (k == "greeting" and self._greeted())
                and (self._new_ears() if k != "purpose" else (self.passed_on or k in self.opening_unheard))]
        self.passed_on, self.opening_unheard = False, [k for k in self.opening_unheard if k not in keys]
        q = callback_question("authority")
        self._new_item(q)
        self.order, self.substitute_q = [q.id], q.id
        self.phase, self.current, self.recheck_queue, self.callback_held = "asking", None, [], False
        r = self._led([self._part_play(k) for k in keys], "", self._advance("", handled=True))
        return r | {"thanks": True} | ({"renamed": True} if "intro" in keys else {})

    def _substitute_decided(self, prefix: str) -> dict:
        """代わりの方に、判断いただけるお立場かを聞いた答えが決まった（_advance の入口から）：「判断できる」なら代わりの方として繋ぎ（依頼主へ渡す答えに、
        代わりの方の答えと印を付ける＝通話の事実 connect の who）、それ以外（お取次ぎのみ・わからない・聞き取れない）は、お礼を言い、こちらからかけ直す形に
        して、お呼びした方の戻る時刻を聞く（話を進めない側＝お呼びした方とお話しできる道を残す）。"""
        qid, self.substitute_q = self.substitute_q, None
        it = self.items[qid]
        yes = it.state in GOT and it.condition == AUTHORITY_YES
        # 答え：取れた答え（わからない・答えたくないも）は日本語の名前（label の1か所）、取れなかった（聞き取れない・答える前に切れた）は None。
        # ⛔ 2026-11-02（CL-2 の連絡帳 4-179 の (c)）まで、取れなかったときに状態の名前（dont_know など）をそのまま入れていた
        self.facts["substitute"][-1]["answer"] = label(it.condition) if it.state in TAKEN else None
        self.order = list(self.business_ids)
        if yes:
            return self._connect({"ai"} | {k for k in JUDGMENTS if self._material(qid).get("check", {}).get(k) == AUTHORITY_YES},
                                 who="substitute", prefix=prefix)
        return self._callback(self.substitute_why, {"ai"}, prefix=prefix, thanks=True)

    def _departed(self, confirm: bool, read: set[str], why: str | None = None, agreed: bool = False) -> dict:
        """お呼びした方が在籍していない（退職・異動・担当が変わった）：業務設定で決める（唯一の場所）。
        ✅ 2026-10-22 ユーザー：「目的の人がいない（退職・異動）：(a) 基本は後任の方をお呼びする+業務ごとに選べる」
        - 後任の方をお呼びする（既定 successor）：録音「さようでございましたか。大変失礼いたしました。」のあと、後任の方をお呼びする一言（電話口の方が
          後任の方だと言ったとき＝confirm は、確かめる一言）。ここからは後任の方が目的の人（_to_successor。判定も後任の方の問いで読む）
        - 電話口の方が後任の方だと、AIと判定の読みがそろった（agreed＝_successor_read）：確かめずに後任の方として繋ぐ（_connect。
          まだ名乗りを聞いていない方なら、あいさつ・名乗りから）
        - お詫びの録音は、在籍していないことを、いま電話口の方が伝えたときだけ（AIの理由が「在籍していない」・いまの言葉の不在の読み。判定が
          1つも届いていなければ流す＝前のとおり）。取り次いだ方が伝えて代わった後任の方には流さない（その方は何も伝えていない。⛔ 前は、
          「退職いたしまして、後任の田中に代わります。…」のあとに出た田中様に「そうでしたか。大変失礼いたしました。」を流した）
        - お礼を言って終える（end）：同じお詫びのあと、ふつうの締め。後任の方へ切り替えたあとにまた在籍していないと言われたときも終える"""
        policy = self.policies["departed"]
        self._note("departed", policy=policy, read=sorted(read), successor=self.successor)
        now = self._judged_now()
        told = why == "departed" or not now or any(says_away(v) for v in now.values())
        self._keep_calling_turn()
        lead = [{"kind": "recorded", "id": "departed", "text": DEPARTED_ACK}] if told else []
        if policy == "end" or self.successor:
            return self._led(lead, "", self._close("departed")) | {"led": True}
        self._to_successor()
        if agreed:
            return self._led(lead, "お呼びした方は在籍しておらず、電話口の方が後任の方だと伺いました（読みがそろいました）。", self._connect(read)) | {"led": True}
        if confirm:
            return self._led(lead, "お呼びした方は在籍しておらず、電話口の方が後任の方だと伺いました。", self._ask_target(new_person=False)) | {"led": True}
        keys = [k for k in ("greeting", "intro") if self._new_ears() and self.target.get(k) and not (k == "greeting" and self._greeted())]
        self.passed_on = False
        self.target_called.append(self.callee_turns)
        return self._speak_lines(lead + [self._part_play(k) for k in keys + ["target"]], "call_target",
                                 "お呼びした方は在籍していません（退職・異動）。ここからは後任の方が目的の人です。") | {"led": True}

    def _to_successor(self) -> None:
        """目的の人を、後任の方に切り替える（唯一の場所。_departed）。呼ぶ一言・確かめる一言・聞き返しは後任の方の録音（共通の一言。phrases.SUCCESSOR）
        にし、判定の質問も作り直す。前にお呼びした方への読み・回数（呼んだ・確かめた・お詫びした）は、後任の方には数えない（target_from＝いまの発話は
        前の方への返事）。"""
        self.successor = True
        self.first_called = self.first_called or self.target.get("target", "")   # はじめにお呼びした言葉（判定に見せる＝_calling_questions）
        for d in {id(d): d for d in (self.target, self.opening) if d}.values():
            d.update(SUCCESSOR)
        self.system_parts.update(SUCCESSOR_IDS)
        self._calling_questions()
        self.target_from = self.callee_turns + 1
        self.target_called, self.target_confirmed, self.target_sorry = [], [], []
        self.confirm_open = self.target_claimed = False
        self.back_turn = None

    def _plays_recorded(self, plays: list[dict]) -> bool:
        """決まった言葉 plays（名乗りの部分・申し出に応じる一言など）を、録音で流せる通話か（唯一の場所。⛔ 2026-11-04 に見つけた穴：_led と
        _speak_lines が同じ式を別々に書いていた）。名乗りの部分は名乗りの録音、ほかは名乗りの録音か質問の録音で流す通話。"""
        return all(p["kind"] != "said" and (self.opening if p["kind"] == "opening" else (self.opening or self.play_recordings)) for p in plays)

    def _led(self, plays: list[dict], note: str, r: dict) -> dict:
        """返事 r（_advance・_close・_ask_target などが作ったもの）の前に、決まった言葉 plays（名乗りの部分・申し出に応じる一言の録音）を流す（唯一の
        場所）。録音で流せない通話（名乗りの録音が無い＝AIが話す形）では、AIにその文を先に言わせる。note＝AIへの知らせの頭。
        r が次の質問を伺う返事なら、質問の頭のつなぎの言葉を、足す言葉の文の頭と重ねない（_relead_next の1か所。✅ 2026-11-06 日本時間11時55分13秒
        ユーザー「(1) ウ」＝「…保留とさせていただきますね。続きましてお支払いは…」）。"""
        r = self._relead_next(r, plays)
        if plays:
            texts = "".join(p["text"] for p in plays)
            if self._plays_recorded(plays):
                note += f"{played_note(texts)}続けて、"
                if "play" not in r:
                    r["speak"] = True  # 続きはAIが言う（録音のあとに話させる）
                r["play"] = plays + r.get("play", [])
            elif r.get("play"):
                # 先に会話AIが言う言葉（録音で流せない言葉）のあとに、返事の録音が続く：流す側は録音のあとに会話AIが話す順だけなので、この回は返事の録音の
                # 言葉も会話AIがその文のとおりに言う（質問でも締めでもない返事も同じ＝この1か所。✅ 2026-11-12 まとめ役の (H3)。⛔ 5de3425e では次の質問と
                # 締めだけ別々に作り替え、ほかの返事では「まず…と言って続けて」と録音が同じ回に並び、録音が先に流れうった）
                return self._spoken_all(r, texts, note)
            else:
                note += f"まず、{say_note(texts)}続けて、"
        r["message"] = note + r["message"]
        return r

    def _spoken_all(self, r: dict, before: str, note: str = "") -> dict:
        """返事 r の録音（play）を、先に言う言葉 before に続けて会話AIがその文のとおりに言う形に替える（唯一の場所＝_led）。録音で流したと知らせる文
        （played_note の1か所の書き方）は外し、会話AIへの続きの知らせ（相手の答えを待つ・続けて話す など）は残す。"""
        text = before + "".join(p["text"] for p in r.pop("play")) + (r.pop("say", None) or "")
        r.pop("silent", None)
        r.pop("speak", None)
        r["say"] = text
        r["message"] = f"{note}{say_note(text)}そのあと、" + PLAYED_SENTENCE.sub("", r["message"])
        return r

    def _speak_lines(self, plays: list[dict], result: str, note: str) -> dict:
        """決まった言葉 plays だけを流して、相手の返事を待つ返事（唯一の場所。録音で流せない通話では、AIにその文のとおりに言わせる）。"""
        text = "".join(p["text"] for p in plays)
        if self._plays_recorded(plays):
            return {"result": result, "silent": True, "play": plays,
                    "message": f"{note}{played_note(text)}何も言わずに、{self._now()}"}
        return {"result": result, "say": text, "message": f"{note}{say_note(text)}そのあと、{self._now()}"}

    def _callback(self, why: str, read: set[str], prefix: str = "", offer: str | None = None, thanks: bool = False,
                  roles: tuple | None = None) -> dict:
        """かけ直し・ほかの手段の質問を始める（唯一の場所）。read＝受け取った読み（「ai」＝AIの申し出・不在／ご本人と読んだ判定の名前。「ai」が無ければ
        プログラムが進めた）を通話の事実に残す（AIの読みだけで受け取ったかを、通話後の検証が見る＝DESIGN.md 6-12。繋ぐときの connect と同じ形）。
        目的の人かを確かめに戻ったあと（_target_back）に、同じ理由でまた申し出られたら、聞いた分は残したまま続きから聞く。
        電話口の方が、まだこちらの名乗りを聞いていない方（取り次がれて代わった方）なら、先にあいさつ・名乗りを流す（_reception_lead）。
        roles＝伺うこと（無ければ理由ごとの CALLBACK_PLAN。急いでいる方にご都合のいい日時を伺う＝_hurry_decided）。
        相手の申し出（offer）と業務設定（✅ 2026-10-22 ユーザー）：
        - 折り返し・伝言（伝言を残さない業務）、または thanks（判断いただけるお立場ではなかった代わりの方・目的の人だけと話す業務の代わりの方）
          ⇒ 録音「お気遣いありがとうございます。改めてこちらからお電話させていただきます。」のあと、戻る時刻などを聞く
        - 伝言（伝言を残す業務）⇒ お礼の相づち「ありがとうございます、」のあと、伝言の文（依頼主の単位の録音。はい／いいえで伺う＝M1）、続けて戻る時刻
        - ほかの手段（メール・ファクス・郵送）⇒ その宛先を聞いて締める。手段の申し出が無ければ、聞かずに締める（✅「希望とメールアドレスを記録して、
          お礼を言って締める」）"""
        intro = self._reception_lead()  # かけ直しを決めた発話の読み（不在）も残す：確かめに戻ったとき（_target_back）も、お呼びし直さない
        # 伺っている途中のシステムの質問への申し出（判断いただけるお立場か＝「やはり岡田から折り返させます」・また改めてお掛けしてよいか＝「戻りは3時です」・
        # 待つかかけ直すかなど）：その質問はもう伺わない
        self._drop_pending()
        resume = why == self.callback_why and self.callback_ids
        leave = not resume and offer == "message" and self.policies["message"] == "leave"
        if resume:
            self._note("callback_resume", why=why, read=sorted(read))
            self.order = list(self.callback_ids)
        else:
            self.callback_why = why
            self._note("callback", why=why, read=sorted(read), **({"offer": offer} if offer else {}), **({"message_left": True} if leave else {}))
            roles = ("message",) * leave + (MEANS_PLAN.get(offer, ()) if why == "other_means" else roles or CALLBACK_PLAN[why])
            qs = [callback_question(r, self.message_text if r == "message" else None, answering=self.in_answer_stage) for r in roles]
            for q in qs:
                self._new_item(q)
            self.order = self.callback_ids = [q.id for q in qs]
        self.phase, self.recheck_queue, self.callback_held = "asking", [], False
        self.awaiting_confirm = self.awaiting_say = None
        thanked = not resume and (thanks or offer == "callback" or (offer == "message" and not leave))
        plays = intro + ([{"kind": "recorded", "id": "offer_thanks", "text": OFFER_THANKS}] if thanked else [])
        note = prefix + (NOT_INTRODUCED if intro else "")
        r = self._led(plays, note, self._advance("", handled=True))
        return r | ({"renamed": True} if intro else {}) | ({"led": True} if thanked else {}) | ({"thanks": True} if leave else {})

    def _drop_pending(self) -> None:
        """伺っている途中のシステムの質問（判断いただけるお立場か substitute_q・ご遠慮の一言 refusal_q・本心を伺う一言 concern_q・また改めてお掛けしてよいか
        hurry_q・待つかかけ直すか wait_q）の印と、伺う前に覚えた戻る位置（refusal_back・wait_back）を下ろす唯一の場所。並びを入れ替えて別の手順を始める所
        （かけ直し _callback・急いでいる方への伺い _hurry_ask・目的の人かを確かめに戻る _target_back）が呼ぶ＝その質問はもう伺わないので、答えの入口
        （_advance_to）・待つ読みの応じ方（_wait_route）が、その質問の答え・伺う前の位置で決めない。⛔ 2026-11-08 日本時間6時台（11-07 23時26分13秒の
        ご質問の見直しの A-4）：かけ直しと確かめに戻る所が2つの印だけを手で下ろし、急いでいる方への伺いは何も下ろさなかった＝待つかかけ直すかを伺っている
        途中に急いでいる方への伺い→かけ直しの日時へ進むと、日時を伺っている最中の「待ってください」で伺う前の質問へ戻り、日時の答えを差し戻した"""
        self.substitute_q = self.refusal_q = self.concern_q = self.hurry_q = self.wait_q = None
        self.refusal_back = self.wait_back = None

    def _reception_lead(self) -> list[dict]:
        """呼んでいる段で、電話口の方にシステムの質問を伺う前に流す、あいさつ・名乗りの録音（まだこちらの名乗りを聞いていない方＝取り次がれて
        代わった方。Intent.md 9-4「相手が替わったら短く名乗り直す」。唯一の場所＝_callback・_hurry_ask）。呼んでいる段なら、いまの発話の読みを
        呼んでいる段の記録に残す（_keep_calling_turn）。⛔ 2026-10-22 の見直しで見つけた漏れ：代わった山田様に、名乗らずに
        「承知しました、恐れ入りますが何時ごろお戻りに…」を流していた"""
        if self.phase != "calling":
            return []
        keys = [k for k in ("greeting", "intro") if self._new_ears() and self.target.get(k) and not (k == "greeting" and self._greeted())]
        self._keep_calling_turn()
        self.passed_on = False if keys else self.passed_on
        return [self._part_play(k) for k in keys]

    def _target_back(self) -> dict:
        """かけ直しの質問のあとに取り次ぎがあって、AIが「目的の人」「代わった方」と報告した（お呼びした方が戻って電話口に出た・ほかの方に
        代わった・AIの聞き違い）：呼んでいる段に戻り、
        その方に目的の人かを1回確かめる（唯一の場所。判定はかけ直しの質問の答えとして読んでいて、電話口の方が誰かは読んでいない＝
        確かめの返事を、呼んでいる段の決め方 _calling_reported で決める）。「はい」なら繋ぎ（あいさつ・名乗り・用件・最初の質問）、
        違えば（前に不在と読まれているので）かけ直しの質問の続きへ戻る（_callback）。聞いたかけ直しの答えは通話の事実に残したまま。
        ⛔ 2026-10-22 G-57 tc24（確かめにいいえ・L4）：かけ直しの質問の途中で代わって出た岡田様の「お電話代わりました。岡田です。」に、
           あいさつ・名乗り・用件のあと「恐れ入りますが何時ごろお戻りになられますでしょうか？」を流し、業務の質問を1つも聞かずに締めた"""
        self._note("target_back", why=self.callback_why)
        self._drop_pending()      # 伺っている途中のシステムの質問（判断いただけるお立場か・また改めてお掛けしてよいかなど）に、お呼びした方が戻った：もう伺わない
        self.close_after = None   # 取り次げないと言われたあとに取り次がれた（受付の方が考えを変えた）：取り次げなかった締めでは終えない
        self.back_turn = self.callee_turns
        self.order, self.phase, self.current, self.recheck_queue = list(self.business_ids), "calling", None, []
        self.awaiting_confirm = self.awaiting_say = None
        self._new_speaker(self.callee_turns, after=False, sure=True)
        return self._ask_target(new_person=True)

    def _end(self, reason: str) -> None:
        """通話を終える印の唯一の場所（締め・話さずに切る）：締めた理由（END_REASONS の中の名前）を、通話の事実 end に残す。1通話1回
        （二つ目は来ない＝_op_request_end が、締めたあとの終える報告を止める）。"""
        assert reason in END_REASONS, reason
        assert self.end_reason is None, (self.end_reason, reason)
        self.current, self.phase, self.end_reason = None, "closing", reason
        self._note("end", reason=reason)

    def _ended(self) -> dict:
        """もう締めた（締め・話さずに切る・こちらの不具合で終える、のあと）：何もしない返事（唯一の場所。締めは1通話1回＝_end・_trouble）。"""
        return {"result": "end", "silent": True, "message": "締めの言葉は、もう流しました（または話さずに終えました）。何も言わずに終えます。"}

    def on_ai_trouble(self, entry: dict) -> None:
        """この通話で、使うAIのどれかが止まった・緊急の道で答えた（唯一の場所。通話の報告の ai_trouble＝発信の門が新しい電話を回復まで一時停止する
        ＝CL-2）。✅ 2026-10-30 夜 ユーザー：「基本的にアンソロピックが使えなくなったりその他に使う AI のどれかが止まったら、AI の不具合として
        回復するまで一時停止してください。」。entry：判定の問い合わせの記録（meaning_judge.routed_call の tried の1つ＝model・route・error・
        outage／used）か、会話AI・別の耳の同じ形。同じAI・同じ道・同じ出来事は1つにまとめて回数を数える（最初の発話の番号を残す）。
        glitch＝電話は続けられた不具合（おかしな動き：会話AIが黙った・報告が戻らないので張り替えて続けた＝on_ai_failed）。止めずに警告する
        （✅ 2026-10-31 管理者のご決定（CL-2 の連絡帳 4-174）：「甚大な問題は止める。甚大でない場合は、ダッシュボードに警告を出す。」）。"""
        stopped, answered, glitch = AI_TROUBLE_WHATS
        what = answered if entry.get("used") else glitch if entry.get("glitch") else stopped
        route = entry.get("route") or DIRECT_ROUTE
        for t in self.ai_trouble:
            if (t["ai"], t["route"], t["what"]) == (entry["model"], route, what):
                t["count"] += 1
                return
        self.ai_trouble.append({"ai": entry["model"], "route": route, "what": what, "error": entry.get("error"),
                                "turn": self.callee_turns, "count": 1})
        if self.tell_ai_trouble is not None:   # 新しく足したときだけ、すぐ知らせる（回数が増えただけのときは、報告の終わりの ai_trouble で）
            self.tell_ai_trouble(dict(self.ai_trouble[-1]))

    def _trouble(self, reason: str, why: str) -> dict:
        """こちらの不具合で電話を途中で終える唯一の場所（甚大な問題＝DESIGN.md 6-8。理由は TROUBLE_REASONS）。電話接続側が
        call.end_for_trouble(reason, why) を呼び（why＝根拠の短い文。お客様の言葉・名前は入れない）、お詫びの2つの一言（phrases.LINE_TROUBLE）で終える：「恐れ入ります。こちらの通信に不具合が起きた
        ようですので改めてお電話させていただいてもよろしいでしょうか？」→ お客様のお返事 →「大変申し訳ございません。それではいったんお電話を
        失礼いたします。」→ 切る（流して切り、理由を試行に残し、復旧次第お詫びとともにかけ直すのは CL-2＝CL-2 の連絡帳 4-156・4-158。かけ直しは、
        取れた答えの質問を伺わずに続きから）。会話の側は、このあと何も流さず、AIの報告も受けない（_apply_one）。1通話1回。"""
        assert reason in TROUBLE_REASONS, reason
        if self.end_reason is not None or self.trouble is not None:
            return self._ended()
        self.trouble = {"reason": reason, "why": why, "turn": self.callee_turns}
        if reason == "ai_malfunction":   # 会話AIの不具合も、止まったAIとして数える（on_ai_trouble の1か所）
            self.on_ai_trouble({"model": CONVO_AI, "error": why, "outage": True})
        self.current, self.phase = None, "closing"
        return {"result": "end_trouble", "reason": reason, "why": why, "silent": True,
                "message": "こちらの都合で、この電話はいったん終えます。何も言わずにお待ちください（お詫びの言葉は、プログラムが流します）。"}

    def _hang_up(self, reason: str) -> dict:
        """話さずに切る唯一の場所（人ではない相手）。締めの言葉も相づちも流さない。"""
        if self.end_reason is not None or self.trouble is not None:
            return self._ended()
        self._end(reason)
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
            return head | {"play": [self._part_play(self.confirm_part)],
                           "message": f"{played_note(text, "電話に出た方への")}何も言わずに、{then}"}
        return head | {"say": text, "message": f"{say_note(text, '電話に出た方に')}そのあと、{then}"}

    def _call_target(self, sorry: bool, maybe_new: bool = False, part: str = "target") -> dict:
        """電話口の方は目的の人ではない（確かめに「いいえ」と答えた・違うお名前を名乗った・確かめへの返事が「はい」とそろわなかった）：
        目的の人をお呼びし直す（DESIGN.md 4-3「受付・別の人 ⇒ 決まった言い方で取り次ぎを頼む」。唯一の場所）。
        - sorry：確かめたあと（頭にお詫びの相づち「失礼いたしました。」＝_ack）
        - その方にまだ名乗っていない（取り次がれて代わった方＝passed_on）⇒ あいさつ・名乗りから（DESIGN.md 4-3「相手が替わったら短く名乗り
          直す」）。流したら、その方は名乗りを聞いた方（passed_on を消す。用件はまだ＝繋いだら用件から）
        - maybe_new：電話口の方が替わったかもしれない（確かめたあとのAIの「代わった方」）⇒ 名乗りから
        - 呼ぶ一言は、その方に2回まで（同じお願いを繰り返さない）。流すものが無ければ、お詫びだけで相手の言葉を待つ
        - part：流す呼ぶ一言（ふつうは target。お名前だけでは分からないと言われたときは、部署・フルネームの入った target_full＝_who_unclear が1回だけ。
          同じお願いではないので、2回までの数には入れるが、数で止めない）
        ⛔ 2026-10-21 の見直しで見つけた漏れ：確かめに「いいえ、山本です。」と言われたときの道が無く、AIは番号違いで締めた（G-62）・同じ方に
           もう一度確かめた・回数を使い切って繋いだ"""
        self.confirm_open = False  # 確かめへの返事は、もう待っていない（_now）
        if self.callback_why:
            # かけ直しの質問の途中で確かめに戻った方は、目的の人ではなかった（_target_back）：お呼びした方は不在と分かっているので、お呼び
            # し直さずに、お詫びのあと、かけ直しの質問の続きへ（聞いた分は残したまま）
            return self._callback(self.callback_why, {k for k in JUDGMENTS if self.check.get(k) == TARGET_IS_NOT}) | {"sorry": sorry}
        if sorry and any(c >= self.speaker_from for c in self.target_sorry):
            sorry = False  # お詫びは、同じ方に1回まで
        if sorry:
            self.target_sorry.append(self.callee_turns)
        keys = [k for k in ("greeting", "intro") if (self._new_ears() or maybe_new) and self.target.get(k)
                and not (k == "greeting" and self._greeted())]
        if keys:
            self.passed_on = False
            self.opening_unheard = [k for k in OPENING_ORDER if k == "purpose" and self.target.get(k)]
        if part != "target" or self._calls_to_speaker() < 2:
            keys.append(part)
            self.target_called.append(self.callee_turns)
        text = "".join(self.target[k] for k in keys)
        head = {"result": "call_target", "sorry": sorry}
        what = "相手の方は、目的の人ではありませんでした（目的の人だと、はっきりしませんでした）。"
        if not keys:
            return head | {"silent": True, "message": f"{what}お詫びの相づちだけを流しました。何も言わずに、{self._now()}"}
        if self.opening:
            return head | {"silent": True, "play": [self._part_play(k) for k in keys],
                           "message": f"{what}{played_note(text)}何も言わずに、{self._now()}"}
        return head | {"say": text, "message": f"{what}{say_note(text)}そのあと、{self._now()}"}

    @property
    def apology_delivered(self) -> bool | None:
        """承認したお詫びを伝えられたか（会話の報告の apology_delivered の唯一の場所。お詫びの電話の文 apology_call・約束の電話などの頭のお詫びの文 apology_text＝
        理由 forbidden_speech のどちらも無ければ None）：同じ材料（最後まで流れた録音の文 heard_lines）で、お客様が出て、その文が最後まで流れた。✅ CL-2 の 4-350 の (2)"""
        text = self.apology_call or self.apology_text
        return None if text is None else text in self.heard_lines and self.callee_turns > 0

    def _apology_cut(self) -> bool:
        """お詫びを流したのに届く前に止まり、まだ流し直していない（流し直す唯一の条件）：お詫びを伝える相手と話している（流すように頼んだあと＝
        目的のお客様のいる業務は繋いだあと）・締めに入っていない・最後まで届いていない。⛔ 2026-10-24 の見直し（調べ役）：前は「流していない」だけを
        見ていたので、繋がる前（受付の方とのかけ直しの質問）・断り・苦情・締めたあとでもお詫びを流し、発信禁止の申告が残らなかった"""
        return (bool(self.apology_call) and self.apology_asked and not self.apology_played and not self.apology_replayed
                and self.apology_call not in self.heard_lines and self.phase not in ("calling", "closing"))

    def _apology_play(self) -> dict:
        return {"kind": "apology", "text": self.apology_call}   # 承認したときに聞いていただいた録音（お詫びの案ごと＝CL-2 の apology の録音）

    def _op_request_end(self, a: dict) -> dict:
        reason = a.get("reason")
        if self.end_reason is not None:
            # もう締めた（保留で待ちきった・断られた など。電話接続側の知らせで締めたあとに、AIが終える報告をした）：二度締めない・締めた理由を
            # 変えない（⛔ 2026-10-24 の見直し（調べ役）：二つ目の締めを流し、締めた理由を「質問が終わった」に上書きしていた）。締めたあとの
            # 発信の拒否・苦情は、記録には残す（原則14：その場で発信禁止に登録）
            if reason in ("callee_refused_call", "callee_complaint"):
                self._note_irreversible("do_not_call" if reason == "callee_refused_call" else "complaint",
                                        self._end_check(reason, bool(a.get("judged"))))
            if self.after_answer is not None:
                return self._take_after_answer()   # 相手の質問に答えてから流すはずだった締め：いま流す（取り出す1か所）
            return self._ended()
        if reason == "done" and not a.get("judged") and (due := self._end_due_now()) is not None:
            reason, a = due, {**a, "judged": True}   # 「質問が終わった」の報告でも、判定が電話を終える言葉と読んでいれば、その理由で締める
        if reason == "done" and self._apology_cut():
            # お詫びが届く前に止まっていた（相手が話し始めた）：締める前に、もう一度だけ流す（ほかの理由＝断り・苦情などは、今までどおり記録して締める）
            self.apology_replayed = self.apology_played = True
            return {"result": "next", "silent": True, "play": [self._apology_play()],
                    "message": played_note(self.apology_call) + "何も言わずに、相手のお返事を待ってください。"
                               f"お返事を聞いたら request_end を{ARG_LABEL['reason']}「{END_LABEL['done']}」で呼びます。"}
        if reason == "done" and self.phase == "calling":
            return self._reject("まだ目的の人に繋がっていません（質問を始めていません）。")
        if reason != "done" and self.judging and (not a.get("judged") or reason == UNSURE_END):
            # 決めるのは判定（会話AIの理由は使わない＝判定を待つきっかけだけ）。判定が割れた・確かでない読みに電話を終える寄りがあるときは、
            # 会話AIが報告していなくても、決まらないときの場面へ（_end_unsure）
            decided = self._end_unsure() if reason == UNSURE_END else self._end_judged(reason)
            if isinstance(decided, dict):
                return decided
            reason = decided
        if reason == "not_transferred":
            return self._not_transferred()
        if reason == "transfer_unclear":
            return self._transfer_unclear()
        if reason == "who_unclear":
            return self._who_unclear()
        if reason in ("callee_hurry", "callee_refused_call") and (taken := self._ambiguous_only("通話を断られた", ending=True)):
            # 相手の言葉が「結構です」のような言葉だけ：その言葉だけで、通話を断られたとは決めない（_ambiguous_only）
            return taken
        if reason == "callee_hurry" and self.busy_ask and not self.busy_asked:
            # 業務設定で「引き止める」を選んだとき：1回だけ、決まった一言で聞く。2回目の「急いでいる」は、締める前の伺い（_hurry_ask）。
            # 録音で流す電話では、業務の録音（replay_parts の busy）を流す（お客様に伺う文はプログラムが流す＝9-3。⛔ 前は会話AIに言わせていた）
            self.busy_asked = True
            then = (f"相手が応じたら、いまの質問から続けます。応じなければ、もう一度 request_end を{ARG_LABEL['reason']}"
                    f"「{END_LABEL['callee_hurry']}」で呼んでください。")
            if self.play_recordings:
                return {"result": "busy_ask", "play": [self._part_play("busy")],
                        "message": f"{played_note(self.busy_ask)}{then}"}
            return {"result": "busy_ask", "say": self.busy_ask, "message": f"{say_note(self.busy_ask)}{then}"}
        if reason == "callee_hurry" and not self.hurry_asked and not self.callback_why and not self.close_after:
            # 急いでいる：締める前に1回だけ、また改めてお掛けしてよいかを伺う（_hurry_ask。✅ 2026-11-01 日本時間20時56分 ユーザー (b)）。
            # かけ直しの質問の途中（不在・あとで など＝もうかけ直しの申し出を受けている）・約束の電話の一言のあとは、伺わずに締める
            # （かけ直しの記録はもうある＝同じ申し出は1通話1回）
            return self._hurry_ask()
        if reason in DECLINED_ENDS:
            if reason in ("callee_refused_call", "callee_complaint"):
                self._note_irreversible("do_not_call" if reason == "callee_refused_call" else "complaint",
                                        self._end_check(reason, bool(a.get("judged"))))
            return self._close(reason)
        if self.current is not None:
            # いま聞いている質問がある間は、終えられない（まだ聞いていない・言い直しの返事待ち・念のための確認の最中）
            # ⛔ 2026-10-13 の試験で見つけた漏れ：「まだ聞いていない項目」だけを数えていたため、言い直しの返事待ちや
            #    念のための確認の最中に「終わり」と報告されると、確認を飛ばして通話を終えていた
            return self._reject("まだ終えられません。")
        return self._advance("")

    def _end_now(self) -> str | None:
        """いまの発話の、判定の「電話を終える言葉か」（中の名前。届いた判定がそろって読んだもの・食い違えば「決められない」・届いていなければ None）。"""
        ends = [v.end for v in self._judged_now().values() if getattr(v, "end", None) is not None]
        return (ends[0] if all(e == ends[0] for e in ends) else UNDECIDED) if ends else None

    def _end_lean(self) -> set:
        """いまの発話で、電話を終える言葉かが決まらなかったときの寄り（判定の読みのうち、電話を終える読み＝食い違った読み・確かでなかった読み）。"""
        vals = list(self._judged_now().values())
        got = ({getattr(v, "end", None) for v in vals} | {e for v in vals for e in getattr(v, "end_lean", ())}
               | self.end_leans.get(self.callee_turns, set()))
        return got & set(END_REASON)

    def _end_unsure_now(self, end: str | None) -> bool:
        """いまの発話が、電話を終える言葉か判定でも決まらないか（唯一の決まり＝決まらないときの場面 _end_due_now・迷っているお客様への応じ方 _plain_reply）。
        end＝_end_now。決まらない：届いた読みが食い違った・判定が「決められない」と読んだ（寄りが無くても）・確かな読みが無く、答えを待っている判定も無いのに
        確かでない読みが電話を終える言葉に寄った。⛔ 2026-11-01 夜の見直しで見つけた穴：前は食い違ったときだけで、確かでない読みだけの寄りでは
        何もしなかった。⛔ 同じ夜、正解の表を作りから出して見つけた穴：判定の「決められない」も、寄りが無いと何もせず、「もういいです。」の答えを
        向きを確かめずに受け取って次の質問を流していた"""
        return end == UNDECIDED or (end is None and not self._end_pending() and bool(self._end_lean()))

    def _end_due_now(self) -> str | None:
        """会話AIの報告より先に当てる、判定が読んだ電話を終える言葉（終わりの理由）：あとから届いた判定の読み（end_due）か、いまの発話の言葉が
        出揃ったあとの読み。判定を使わない通話・もう締めた通話は None。"""
        if not self.judging or self.end_reason is not None or self.trouble is not None:
            return None
        if self.end_due is not None:
            due, self.end_due = self.end_due, None
            return due
        if self.end_used_turn == self.callee_turns or self.midway or not self.words_complete:
            return None
        end = self._end_now()
        unsure = self._end_unsure_now(end)
        if unsure and self._judged_due():
            return None   # 待つ・考えている途中・もう一度言ってほしいの読み：電話を終える言葉か決まらないときの場面にしない（報告の無いターンは _judged_first が応じる）
        topic = unsure and self._topic_first()
        if topic:
            self.end_doubt = self.callee_turns   # いまの話への返事として受け取る（答えは向きを確かめる＝_checks）
        due = END_REASON.get(end) or (UNSURE_END if unsure and not topic else None)
        if due is not None or unsure:
            self.end_used_turn = self.callee_turns   # いまの話への答えとも読めた（_topic_first）：会話AIの答えの報告で進む（この発話では当てない）
        return due

    def judged_turn(self, *, voiced: bool) -> dict | None:
        """会話AIが報告しないターンのあと（会話AIが声だけで操作を呼ばなかった・待たせたまま終わった・声を止めた・黙った。電話接続側が、判定を待って
        から呼ぶ＝convo.check_handler の settle のあと）に、判定の読みで動く唯一の入口（会話AIの報告は判定を待つきっかけだけ）。動かないなら None。
        (1) 電話を終える言葉：判定がそう読んでいれば、プログラムが締める（✅ 2026-11-01 日本時間17時04分 ユーザー：(a)「OK」＝報告が無くても判定が読めば
            締める）。決まらない読みに電話を終える寄りがあれば、決まらないときの場面（_end_unsure）
        (2) 待つ・かけ直すの読み（wait_reading）：報告が来たときと同じ1か所（_wait_route）で応じる＝待つように言われたら「はい、」のあと待つ・確かめて
            戻る方には「はい、それではこのままお返事を待っております。」・あとでかけ直す・決まらなければ1回伺う。この発話にもう返事をしていれば
            （差し戻し・判定を待たせた返事のほか）当てない。⛔ 2026-11-06 日本時間2時台（2時28分41秒のご指摘「「はい、」のような相づちがないと失礼です」を
            入れる途中）に見つけた：待つ・かけ直すは報告が来たときしか当てず、会話AIが報告しないと、お客様には無言が続いていた（答えの段では報告を促す
            知らせも出ない＝unreported_note は答えの読みだけ）。答えの読みは当てない（受け取るのは報告と判定がそろったとき＝unreported_note で促す）
        返事は記録に残し、電話接続側は、録音（play）を流し、相づち（ack）を流し、締めていなければ（ご遠慮の一言・急いでいる方への伺い・受付の方への
        確かめ・引き止めの一言・待つ・かけ直し）返事の知らせ（message）を会話AIへ渡す。待つ返事（result hold）なら、待つ長さ（hold_wait_s）のあいだ
        黙って待つ（報告で受けた待つ返事と同じ）。
        voiced＝このターンに、会話AIの声がお客様に流れたか（電話接続側が必ず渡す。流してよい声＝speech_allowed のうち、本当に流した声があったか＝声を流す
        電話接続側だけが知っている事実）。流れていれば、考えている方への「はい。」・もう一度言ってほしい方への流し直しは、会話AIが言ったので重ねない。
        ⛔ 2026-11-06 日本時間13時台の見直し（C10）で見つけた：流れたかを「流してよいか」（speech_allowed）で代わりに決めていた＝会話AIが話す通話では、会話AIが
        黙っていても流れたとみなし、「はい。」も流し直しも出さなかった（お客様には無言）"""
        before = {qid: it.state for qid, it in self.items.items()}
        # いまのターンで受け取った答え（_received_now＝相づち・材料 _material）と、その記録（_record_received）は、報告の入口 _apply_one と同じ決まり。
        # ⛔ 2026-11-08 日本時間10時台に見つけた：この入口は記録を書く所を通らず、ここで受け取った答え（選ぶ助けのあとの「まだ決められないです」＝保留）は
        #    届かなかった確かめ（unchecked）に数えず、遅れた読みも残らなかった（F0 では、決めたあとの読みは記録だけで、通話後の確かめが録音でやり直す道）
        # 返事のあとの決まり（受け取りの記録・待つ印・答えに添えた質問・取っておいた返事）は、報告の入口と同じ1か所（_turn_done）。このターンで答えを受け取った
        # （_received_now＝選ぶ助けのあとの保留・ご承認の迷いなど）なら、その答えに添えた質問に先に答える（_question_first）。⛔ 11時台（J-5 の確かめ）に見つけた：
        # 返事を待つ印を消す決まりが報告の入口の中だけで、ここで答えを受け取ったあとも「相手の質問に自分の言葉で答えたところです」と会話AIに知らせた
        self.report_before = before
        self.reply_started = False
        try:
            got = self._judged_turn(voiced)
            if got is None:
                self._record_received(before, None)
                return None
            name, args, reply = got
            reply = self._turn_done(reply, before, None, took=bool(self._received_now()))
            reply["ack"] = self._ack(name, args, reply)
            if reply["ack"]:
                reply["ack_pause_s"] = ack_pause_s(reply["ack"])
            self._note_said(about=self._said_about())
            self.log.append({"op": "judged_turn", "args": args, "reply": reply, "callee_turns": self.callee_turns, "phase": self.phase})
            return reply
        finally:
            self.report_before = None

    def _judged_turn(self, voiced: bool) -> "tuple[str, dict, dict] | None":
        """報告の無いターンの入口（judged_turn）の中身：返事と、相づちを選ぶ操作の名前（無ければ None）。"""
        due = self._end_due_now()
        if due is not None:
            name, args = "request_end", {"reason": due}
            reply = self._op_request_end({"reason": due, "judged": True})
        else:
            replied = any(x.get("callee_turns") == self.callee_turns and x["reply"].get("result") not in ("rejected", "wait")
                          for x in self.log)
            w = self._wait_now()
            if replied:
                # この発話にもう返事をした（決めた＝on_decided）：このあとに届いた読みは記録だけ（_after_decision）＝もう動かない
                return None
            else:
                # 報告で受けたときと同じ1か所（_judged_first）：待つ・かけ直す・考えている途中・もう一度言ってほしい。会話AIが話す通話で、会話AIの声がもう流れたなら、
                # 流し直しは重ねない（印だけ立てる＝_repeat_now の voiced）
                reply = self._judged_first(voiced=voiced)
                if reply is None and self._asked_now() is not None:
                    # 尋ねられた・迷っているお客様に選ぶ助けを伝える番（報告で来たときは入口 _apply_one の差し戻し）：同じ知らせの1か所（_asked_turn）
                    reply = self._asked_turn(voiced)
            if reply is None:
                return None
            name, args = ("judged_turn", {}) if reply.get("result") == "replayed" else ("request_callback", {"why": "later", "wait": w})
            if reply.get("result") == "thinking" and voiced:
                reply["voiced"] = True   # 会話AIが自分で言った「はい。」（instruction.THINKING_RULE）が流れた：相づちを重ねない（_ack_choice）
        return name, args, reply

    def _end_judged(self, reason: str) -> "str | dict":
        """会話AIの終わりの報告（理由 reason）を受けて、電話を終えるかを判定で決める唯一の場所（終わりの理由か、返事）。判定が電話を終える言葉と
        読めば、その理由（会話AIの選んだ理由ではなく）。言っていないと読めば、受け付けず会話を続けさせる。決まらなければ _end_unsure。判定の答えが
        2回の待ち（waits_again）でも1つも届かなければ _end_late。
        ✅ 2026-11-01 日本時間16時56分 ユーザー：「会話AIは正しい判断ができるの？できないから、別のAIを使ったんじゃないのか？」・17時04分「(a) OK、(b) OK」"""
        if self.meaning_said is None and (self.midway or not self.words_complete):
            return self._wait_for_words()
        self.end_used_turn = self.callee_turns   # この発話の読みは、ここで当てた（2回当てない）
        end = self._end_now()
        if end in END_REASON:
            return END_REASON[end]
        if (r := self._judged_first()) is not None:
            # 待つ・かけ直す・考えている途中・もう一度言ってほしいの読み：会話AIがどの操作で報告しても先に応じる1か所（電話を終える言葉と言い切った読みのほか。
            # ご遠慮の一言の場面にしない・電話を終える言葉ではないと差し戻して黙らせない＝⛔ 23時22分41秒のご質問の見直し W4）
            return r
        if end == END_NONE:
            return self._reject("判定は、いまの言葉を電話を終える言葉とは読んでいません。電話は終えずに、会話を続けます。")
        if end is None and not self._end_lean() and self._end_pending():
            return self._end_late(reason)
        return self._end_unsure()

    def _end_late(self, reason: str) -> "str | dict":
        """会話AIが電話を終える言葉と報告したのに、判定の答えが2回の待ち（最大で約4秒）でも1つも届かない（混んでいる・止まりかけている）：判定が
        止まったときの決まり（止まったときだけ会話AIの読みで進め、通話のあとに録音で確かめ直す＝Intent.md 9-4 の目的のお客様）で、丁寧に締める
        （急いでいる方への伺いもしない）。もうかけない・苦情の印は付けない（通話のあとの確かめで、判定が読んだときだけ）。会話AIの理由は通話の事実
        end_unjudged に残す（通話のあとの確かめが、録音で読み直す）。取り次げない2つは、会話AIの読みのまま（お礼の締め・ご事情を伺う一言＝取り消せる側）。
        ✅ 2026-11-01 日本時間20時56分 ユーザー：(e)「(エ) の案でよいでしょうか。OK」（「それでも届かなければ…丁寧に締めます。「もうかけない」の印は、
        通話のあとの確かめで判定が「断った」と読んだときだけ付けます」）"""
        self._note("end_unjudged", reason=reason)
        if reason in ("not_transferred", "transfer_unclear", "who_unclear"):
            return reason
        return self._close(reason if reason in DECLINED_ENDS else "callee_hurry")

    def _topic_first(self) -> bool:
        """電話を終える言葉か判定でも決まらないとき、ご遠慮の一言より先に、いまの話への返事として受け取るか（唯一の決まり）：答えの段で、いまの
        質問があり、判定のどれかが、いまの言葉をその質問の答えと読んだ（確かでない読みも）。そのときは会話AIが聞いた答えを報告し、ふつうの答えと
        同じ1か所で受け取る（言い切っていなければ、そこで向きを確かめる）。✅ 2026-11-01 日本時間20時56分 ユーザー：「お客様：「もういいです。」
        こちら：「失礼いたしました。今後このようなお電話はご遠慮したほうがよろしいでしょうか？」より、「パンフレットはお送りしない方がいいという
        ことでしょうか？」のような流れが普通じゃないですか？」"""
        q = self.questions.get(self.current) if self.current and self.phase in ("asking", "rechecking") else None
        return q is not None and self._answer_direction(q) is not None

    def _answer_direction(self, q: "Question") -> str | None:
        """いまの言葉を、判定のどれかが、いまの質問の選択肢の答え（向き）と読んだときの、その答え（確かな読みも確かでない読みも。向きが1つに
        決まらない・選択肢の答えでない＝わからない・答えたくない・どちらでも・値を聞く型、なら None）。いまの話を先に伺うか（_topic_first）と、
        伺うときに確かめる向き（_op_record_answer）の唯一の材料。"""
        if q.type not in CHOICE_TYPES or self.check_for not in (None, q.id):
            return None
        got = {str(v) for v in self._judged_now().values() if str(v) in q.conditions}
        got |= self.answer_leans.get(self.callee_turns, set()) & set(q.conditions)
        return next(iter(got)) if len(got) == 1 else None

    def _end_unsure(self) -> "str | dict":
        """電話を終える言葉か、判定でも決まらない（食い違った・確かな読みが無い）：場面は判定の寄り（_end_lean）で選ぶ（会話AIの理由では選ばない）。
        ✅ 2026-11-01 日本時間17時04分 ユーザー：(a)(b)「OK」の表と、20時56分のご判断のとおり：怒っている・苦情に寄る＝確かめずに締めて人に引き継ぐ
        （特例）／急いでいるだけに寄る＝急いでいるとき（業務設定の引き止め・また改めてお掛けしてよいかの伺い＝_op_request_end）／お呼びする段で
        断った・取り次げないに寄る・どこにも寄らない＝受付の方への一言（_transfer_unclear）／答えの段で、いまの話への返事とも取れる＝会話AIが
        答えを報告する（_topic_first）／答えの段で、そうでない＝ご本人への一言（_refusal_confirm）。"""
        lean = self._end_lean()
        if END_ANGRY in lean:
            return "callee_complaint"
        if END_HURRY in lean and not lean & {END_REFUSED, END_REASONED, END_UNCLEAR}:
            return "callee_hurry"
        if self.phase == "calling":
            return "transfer_unclear"
        if self._topic_first():
            self.end_doubt = self.callee_turns   # 答えは向きを確かめる（_checks）
            return self._reject("判定は、いまの言葉を、電話を終える言葉とは決めきれず、いまの質問への答えとも読みました。電話は終えません。"
                                "何も言わずに、聞いた答えのとおりに record_answer で報告します（「わからない」「答えたくない」も答えです）。")
        return self._refusal_confirm()

    def _refusal_confirm(self) -> dict:
        """お呼びした方ご本人と話している段で、電話を終える言葉か決まらず、いまの質問への答えとも読めないとき（_topic_first）：その場で1回だけ
        「失礼いたしました。今後このようなお電話はご遠慮したほうがよろしいでしょうか？」を伺う（システムの質問＝callback_question("refusal")。録音は
        共通の一言 callback_refusal）。はい＝もうかけない・いいえ＝いまの質問へ戻る（_refusal_decided）。2回目は受け付けず、会話を続けさせる
        （伺うのは1回だけ）。"""
        if self._main_talk() and not self.concern_asked:
            return self._concern_ask()   # 本題に入ったあとは、ご遠慮の一言より先に本心を伺う（✅ 2026-11-08 日本時間2時21分15秒）
        if self.refusal_asked:
            return self._reject("今後のお電話をご遠慮したほうがよいかは、もう伺いました。判定でも電話を終える言葉とは決まりませんでした。会話を続けます。")
        self.refusal_asked = True
        self.refusal_q = self._ask_aside("refusal")
        return self._advance("", handled=True)

    def _main_talk(self) -> bool:
        """本題に入っているか（唯一の決まり＝電話を終える言葉か決まらないときの一言の選び _refusal_confirm）：最初の業務の質問を流した（既定）か、この電話で
        業務の質問の答えを1つ以上いただいた（業務ごとに選ぶ＝下）か、続きの電話（前の通話の答えがある）。序盤（名乗り・用件のあと、まだ業務の質問を流して
        いない）の「もういいです。」は営業のお電話そのものへのお断りが多く、本題に入ってからの「もういいです。」は、こちらの進め方に問題があった見込みがある
        （✅ 2026-11-08 日本時間2時21分15秒 ユーザー：「序盤で営業拒否の言葉があった場合は、営業電話そのものに対して拒絶されているということが多いですが、
        本題のクロージングまで入っている段階で、もういいですなどという言葉が出た場合は、そのクロージングのアプローチに対して問題があった可能性があります。」）。"""
        # どこから本題かは業務ごと（POLICIES["main_talk"]＝✅ 2026-11-08 日本時間3時15分04秒 ユーザー「(ア)業務ごとに選択できるようにしてください。」）：
        # asked（既定＝3時48分03秒「(ア)既定は「最初の質問を流したあと」」）＝最初の業務の質問を流したあと（流した印 Item.played）／answered＝業務の答えを
        # 1つ以上いただいたあと
        asked = self.policies["main_talk"] == "asked"
        return bool(self.earlier) or any(it.state in TAKEN or (asked and it.played) for qid, it in self.items.items() if not self.questions[qid].callback)

    def _ask_aside(self, role: str) -> str:
        """いまの質問・言い直しの位置を覚えて、そのすぐ前にシステムの質問を1つ伺う（唯一の場所＝ご遠慮の一言 _refusal_confirm・本心を伺う一言 _concern_ask）。
        戻るのは _back_from_aside・位置だけ戻すのは _restore_aside。返す値は、その質問の番号。"""
        self.refusal_back = (self.current, self.awaiting_confirm, self.awaiting_say)
        self.awaiting_confirm = None   # 伺う間は、言い直しの返事待ちではない（判定に、言い直した言葉を見せない）
        q = callback_question(role)
        self._new_item(q)
        self._put_first(q.id)
        return q.id

    def _restore_aside(self) -> None:
        """_ask_aside で覚えた位置へ戻す（唯一の場所）。"""
        (self.current, self.awaiting_confirm, self.awaiting_say), self.refusal_back = self.refusal_back, None

    def _back_from_aside(self, prefix: str) -> dict:
        """脇で伺ったシステムの質問の答えが、断ったとは言っていない答え（ご遠慮の一言の「いいえ」・はっきりしない／本心を伺った一言の「問題は無い」・
        はっきりしない）だったとき、伺う前の位置へ、話がそれたあとに戻る1か所（_continue）から戻る返事（唯一の場所）：「話を戻させていただきますね。」から質問へ
        （10-23）。✅ 2026-11-01 ユーザー：「全く同じ質問を2度したら、機械だと思われてしまいませんか？」"""
        self._restore_aside()
        self.answer_held, self.digressed = None, True
        r = self._continue([], "")
        return r | {"message": prefix + r["message"]}

    def _concern_ask(self) -> dict:
        """本題に入ったあと（_main_talk）の、電話を終える言葉か決まらず、いまの質問への答えとも読めない言葉（「もういいです。」）に、ご遠慮の一言より先に本心を
        伺う（唯一の場所。_refusal_confirm から1通話1回）：「失礼いたしました。」（相づちの録音）＋「何か問題でもございましたでしょうか？」（システムの質問 D2＝
        callback_question("concern")・共通の一言の録音 ai_answer をそのまま）。答えで分ける（_concern_decided）。✅ 2026-11-08 日本時間2時21分15秒 ユーザー
        （お言葉は phrases.CALLBACK_ASK_REUSED）"""
        self.concern_asked = True
        self.concern_q = self._ask_aside("concern")
        return self._advance("", handled=True)

    def _concern_decided(self, prefix: str) -> dict:
        """本心を伺った一言への答えが決まった（唯一の入口＝_advance）：
        - 問題を言った（「説明が長くて分かりにくいんですよ。」）：伺う前の質問の位置へ戻し、会話AIが、言っていただいた点に業務の情報の範囲で応える（お客様に
          尋ねられて答える1か所と同じ道＝_asked_now の concern_turn・知らせ _asked_note。応えたらお返事を待ち、お答えがいまの質問の答えなら受け取る＝_resume）。
          言っていただいた点は、依頼者へ渡すお客様の言葉の記録に残る（_question_note＝今後の改善）
        - 断ろうとしている（まだ電話を終えたい）：ここでご遠慮の一言（_refusal_confirm。伺う前の位置から）
        - 問題は無い・はっきりしない：断ったとは言われていないので、伺う前の質問へ戻る（_back_from_aside＝ご遠慮の一言の「いいえ」と同じ1か所）"""
        qid, self.concern_q = self.concern_q, None
        it, q = self.items[qid], self.questions[qid]
        cond = it.condition if it.state in GOT else None
        if q.declined(cond):
            self._restore_aside()
            return self._refusal_confirm()
        if cond == "問題を言った":
            self._restore_aside()
            self.concern_turn = self.callee_turns
            self._note("concern", question=self.awaiting_confirm or self.current)   # 言っていただいた点（会話AIが応えた文は _question_note が書き足す）
            return {"result": "callee_asked", "message": prefix + self._now()}
        return self._back_from_aside(prefix)

    def _refusal_decided(self, prefix: str) -> dict:
        """ご本人への確かめの答えが決まった（唯一の入口＝_advance）：はい＝もうかけない（発信の拒否＝原則14）／それ以外（いいえ・はっきりしない）＝
        断ったとは言われていないので、伺う前の位置へ、話がそれたあとに戻る1か所（_continue）から戻る（「結構です」だけで通話の断りとして締めない
        ＝10-17 と同じ向き）：「話を戻させていただきますね。」から質問へ（10-23）。伺う前のお客様の言葉に、いまの質問の答えの読みがあったときは、
        この一言を伺わずに、その答えを先に受け取っている（_topic_first＝20時56分のご判断）。
        ✅ 2026-11-01 ユーザー：「全く同じ質問を2度したら、機械だと思われてしまいませんか？」＝⛔ はじめは、いまの質問の録音をそのまま流し直していた"""
        qid, self.refusal_q = self.refusal_q, None
        if self._yes_no(qid):
            self._note_irreversible("do_not_call", self._answer_check(qid, "はい"))
            return self._close("callee_refused_call", prefix=prefix)
        return self._back_from_aside(prefix)

    def _hurry_ask(self) -> dict:
        """相手が急いでいる・いまは話せない（判定が読んだ。業務設定で引き止めを選んだ業務は、その前に1回だけ引き止める＝busy_ask）：締める前に1回だけ
        「それではまた改めてお掛けしてもよろしいでしょうか？」を伺う（唯一の場所。システムの質問 H1＝callback_question("hurry_again")。どの段でも
        同じ＝お呼びする段の受付の方・ご本人にも）。答えで分ける（_hurry_decided）：はい＝「ご都合のいい日時はいつ頃でしょうか？」で日時を伺って締める・
        いいえ＝締めて、もうかけない（営業拒否）。いまの質問は伺わずに終える（急いでいる方）。2回目の「急いでいる」は伺わずに締める（_op_request_end）。
        ✅ 2026-11-01 日本時間20時56分 ユーザー：(b)「お客様：「いま時間がないので結構です。」こちら：「それでは、後ほど改めてお掛けしてもよろしいでしょうか？」
        それではがないと繋がりがおかしい。お客様が OK なら：「ご都合のいい日時はいつ頃でしょうか？」→ 時刻でなく日時を伺って締める お客様が NO なら：
        締めて、もうかけない（営業拒否）OK」"""
        self.hurry_asked = True
        plays = self._reception_lead()
        self._drop_pending()   # 伺っている途中のシステムの質問（待つかかけ直すか・ご遠慮の一言など）は、もう伺わない（並びを入れ替えて締めに向かう）
        q = callback_question("hurry_again")
        self._new_item(q)
        self.hurry_q, self.order, self.recheck_queue = q.id, [q.id], []
        self.phase, self.current, self.callback_held = "asking", None, False
        self.awaiting_confirm = self.awaiting_say = None
        note = NOT_INTRODUCED if plays else ""
        return self._led(plays, note, self._advance("", handled=True)) | ({"renamed": True} if plays else {})

    def _hurry_decided(self, prefix: str) -> dict:
        """急いでいる方に、また改めてお掛けしてよいかを伺った答えが決まった（唯一の入口＝_advance）：はい＝かけ直しの申し出（あとで）として、ご都合の
        いい日時を伺って締める（_callback。答えはかけ直しの記録の時刻）／いいえ＝もうかけない（営業拒否＝発信の拒否・原則14）で締める／それ以外
        （わからない・答えたくない・聞き取れない）＝もうかけないとは言われていないので、印を付けずに丁寧に締める（取り消せない印は、はっきり
        「いいえ」と言われたときだけ）。"""
        qid, self.hurry_q = self.hurry_q, None
        yes = self._yes_no(qid)
        if yes:
            read = {"ai"} | {k for k in JUDGMENTS if self._material(qid).get("check", {}).get(k) == "はい"}
            return self._callback("later", read, prefix=prefix, roles=("hurry_when",))
        if yes is False:
            self._note_irreversible("do_not_call", self._answer_check(qid, "いいえ"))
            return self._close("callee_refused_call", prefix=prefix)
        return self._close("callee_hurry", prefix=prefix)

    def _wait_check(self) -> dict:
        """待つ・かけ直すの読みに当てる確かめ（唯一の読み口＝_wait_now・_wait_read・お礼の一言 later_offer）：答えの報告を当てる言葉の確かめ heard_check の判定の読みを、
        待つ読みの名前にそろえたもの（H2 を伺っている間は、H2 の答えの名前を同じ意味の待つ読みに＝as_wait）。"""
        asking = self.wait_q is not None
        return {k: as_wait(v, asking) if k in JUDGMENTS else v for k, v in self.heard_check.items()}

    def _wait_now(self) -> str | None:
        """いまのお客様の言葉の、待つ・かけ直すの読み（唯一の決まり wait_reading を、_wait_check に当てる）。"""
        return wait_reading(self._wait_check())

    def _wait_read(self) -> set[str]:
        """待つ・かけ直すと読んだ判定の名前（かけ直しの記録の read＝通話後の検証が、何で決めたかを見る）。"""
        check = self._wait_check()
        return {k for k in JUDGMENTS if check.get(k) in WAIT_KINDS}

    def _wait_route(self, w: str, read: set[str], why: str = "later", offer: str | None = None) -> dict | None:
        """待つ・かけ直すの読み w（wait_reading）に応じる唯一の場所（答えの報告・言い直しへの返事・かけ直しの申し出のどれから来ても同じ＝会話AIの報告は
        判定を待つきっかけだけ）：取り次ぐ・代わる・理由を言わずに待つ（HOLD）＝黙って待つ／この電話の間に確かめて戻る（HOLD_BACK）＝「はい、それではこのまま
        お返事を待っております。」のあと待つ／あとでかけ直す（LATER）＝かけ直しの申し出（答えの段の一言「承知しました。いつ頃おかけ直しさせていただき
        ましょうか？」）／決まらない（WAIT_OR_LATER）＝1回だけ伺う（_wait_or_later）。待つか・かけ直すかを伺っている途中なら、その答えとして決める。
        かけ直しの質問の途中（もう申し出を受けている）の「あとで」・決まらない読みは、ここでは当てない（None＝ふつうの答えの決まりへ）。
        かけ直す側は、会話AIがかけ直しの申し出で報告した理由のまま、かけ直しの1か所 _callback へ。お客様のほうから折り返すという申し出か（お礼の一言）は
        判定が読む（later_offer の1か所）。⛔ 11-06 2時台に見つけた：申し出を渡さず、お客様からの折り返しの申し出にお礼の一言を流さない作りだった。
        ⛔ 11-06 13時台の見直し（C9）で見つけた：申し出を会話AIの報告だけから取っていた＝報告が無い・遅れるとお礼が抜け、申し出も記録に残らなかった。
        ✅ 2026-11-05 日本時間23時19分06秒・23時56分48秒 ユーザー（会話2）「(2) …OK」「待つか、かけ直すかが言葉から決まらないとき：(ア)」
        まだ考えている途中（THINKING）＝「はい。」の相づちのあと待つ（_thinking。どの段・どの質問・伺っている途中でも同じ）。"""
        if w == THINKING:
            return self._thinking()
        if self.wait_q is not None and self.wait_turn == self.callee_turns:
            # H2 を伺うきっかけの発話への報告（報告の無いターンで伺ったあとに、同じ言葉への報告が遅れて届いた）：H2 の答えではない＝重ねない
            # （待つ返事の again と同じ）。⛔ 2026-11-06 日本時間7時台（5時56分55秒のご質問の読み直し EN-A の指摘3）：お客様が答える前に「このまま待つ」と決め、
            # 「はい、それではこのままお返事を待っております。」を流した
            said = (self.callee_said or "").strip()[:30]
            return {"result": "noted", "silent": True, "again": True,
                    "message": f"待つか、かけ直すかは、この言葉（「{said}」）を聞いてもう伺いました。何も言わずに、お客様のお返事を待ってください。{self._now()}"}
        if w == LATER:
            offer = later_offer(self._wait_check(), offer)   # お客様のほうから折り返すという申し出か（判定が読む＝later_offer の1か所）
        if self.wait_q is not None:
            return self._wait_or_later_decided("", CALL_LATER if w == LATER else WAIT_NOW, why=why, offer=offer, read=read)
        if w == HOLD:
            return self._hold()
        if w == HOLD_BACK:
            return self._hold(back=True)
        if not self._wait_handles(w):
            return None
        if self.holding and w in (LATER, WAIT_OR_LATER):
            # この発話にもう待つと応えた（決めた＝on_decided）あとの同じ言葉への報告：お伝えした動き（このまま待つ）を変えない（決めたあとに届いた読みは記録だけ＝
            # _after_decision。✅ 2026-11-07 日本時間3時14分01秒・8時57分04秒の承認の形＝2秒の中に1回だけ決め、あとから覆さない）。⛔ 2026-11-08 日本時間9時台（F0）
            # まで、遅れて届いた読みで割れたら伺い直していた（_wait_rethought＝11-06 9時05分17秒の (2)。組は1つの発話に1つの読みを返すので、遅れて割れる2つ目の読みが
            # もう無い＝ご報告に書く）
            return self._hold()
        if w == LATER:
            return self._call_later(why, read, offer=offer)
        return self._wait_or_later()

    def _call_later(self, why: str, read: set[str], prefix: str = "", offer: str | None = None) -> dict:
        """答えの段で、お客様があとでかけ直すように言った（判定の読み・待つかかけ直すかを伺った答え）かけ直しを始める唯一の場所：頭の相づちは、会話AIがどの操作で
        報告しても・報告しなくても、かけ直しの申し出と同じ「承知しました。」（印 later_asked＝_ack_choice。✅ 2026-11-06 日本時間0時44分31秒「(イ) 答えの段の
        かけ直しすべてで大丈夫」）。⛔ 2026-11-06 日本時間16時台（C6）：操作の名前で決めていたので、答えの報告からは「ありがとうございます。」にもなり、代わった方への
        報告からは頭が無かった。⛔ 23時22分41秒のご質問の見直し（W5）：待つかかけ直すかを伺った答えのかけ直しは、この印を付けずに同じ違いが残っていた"""
        return self._callback(why, read, prefix=prefix, offer=offer) | {"later_asked": True}

    def _wait_handles(self, w: str | None) -> bool:
        """待つ・かけ直すの読み w に、_wait_route が応じるか（唯一の決まり＝_wait_route と、電話を終える言葉か決まらないときにその場面にしないか
        _judged_due）：考えている途中・待つ（HOLD・HOLD_BACK）・待つかかけ直すかを伺っている途中は、どの段でも応じる。あとでかけ直す・決まらないは、
        かけ直しの質問の途中（もう申し出を受けている）とお呼びする段では応じない（ふつうの答え・不在の決まりへ）。"""
        return w is not None and (w in (THINKING, HOLD, HOLD_BACK) or self.wait_q is not None
                                  or not (self.callback_why or self.phase == "calling"))

    def _wait_or_later(self) -> dict:
        """待つのか、かけ直すのか、お言葉から決まらない（判定が WAIT_OR_LATER・待つ読みとかけ直す読みが食い違った＝wait_reading）：1回だけ「かしこまりました。」＋
        「このままお待ちしていた方がよろしいでしょうか？それとも改めておかけ直しさせていただいた方がよろしいでしょうか？」を伺う（システムの質問 H2＝
        callback_question("wait_or_later")・唯一の場所）。答えで分ける（_wait_or_later_decided）。2回目は伺わずに言葉どおり待つ（✅ 23時56分48秒「(ア)」＝1回）。"""
        if self.wait_asked:
            return self._hold(back=True)
        self.wait_asked = True
        self.wait_turn = self.callee_turns
        self.wait_back = (self.current, self.awaiting_confirm, self.awaiting_say)
        self.awaiting_confirm = self.awaiting_say = None
        q = callback_question("wait_or_later")
        self._new_item(q)
        self.wait_q = q.id
        self._put_first(q.id)
        r = self._advance("", handled=True)
        return r | {"message": r["message"] + self._wait_note()}

    def _wait_note(self) -> str:
        """待つかかけ直すか（H2）を伺っている間の、会話AIへの知らせ（伺った返事 _wait_or_later と、いま求めていること _now の1か所）：お客様が、待つかかけ直すか
        ではなく、伺う前の話（元の質問・元の言い直し）に答えたら、その番号で報告する（受けるのは _wait_left の1か所）。H2 を伺っていなければ空。"""
        if self.wait_q is None or self.wait_back is None or self.wait_back[0] is None:
            return ""
        cur, confirm, say = self.wait_back
        if confirm is not None:
            return (f"相手が、待つかかけ直すかではなく、伺う前の {confirm} の言い直し「{say}」に返事をしたら（「やっぱり…」など）、confirm_field を"
                    f"{ARG_LABEL['question_id']}「{confirm}」で報告します。")
        return (f"相手が、待つかかけ直すかではなく、伺う前の質問 {cur}「{self.questions[cur].text}」に答えたら（「あ、いや、…」など）、record_answer を"
                f"{ARG_LABEL['question_id']}「{cur}」で報告します。")

    def _wait_back_to(self) -> str:
        """待つかかけ直すか（H2）を伺う前の位置へ戻す唯一の場所（答えが決まった＝_wait_or_later_decided・待たずにお話を続けた＝_wait_continue）：H2 の印を下ろし、
        並びから外し（締めの前に伺い直さない）、覚えた位置（質問・言い直しの返事待ち）へ戻す。H2 の番号を返す。"""
        qid, self.wait_q = self.wait_q, None
        (self.current, self.awaiting_confirm, self.awaiting_say), self.wait_back = self.wait_back, None
        self._out_of_order(qid)
        return qid

    def _back_of_wait(self) -> str | None:
        """待つかかけ直すか（H2）を伺う前の位置（言い直しの返事待ちなら、その質問）。H2 を伺っていない・伺う前の位置が無いなら None。"""
        return None if self.wait_q is None or self.wait_back is None else (self.wait_back[1] or self.wait_back[0])

    def _kept_on(self) -> bool:
        """待つかかけ直すか（H2）を伺っている間のお客様の言葉を、届いた判定の読みがどれも「待たずに続ける」（KEEP_ON）と読んだか（唯一の決まり＝先に判定の読みで
        応じる1か所 _judged_first・_judged_due）。H2 を伺うきっかけの発話（遅れて届いた読み）には当てない。"""
        if self._back_of_wait() is None or self.callee_turns <= self.wait_turn:
            return False
        got = [str(self.heard_check[k]) for k in JUDGMENTS if self.heard_check.get(k) is not None]
        return bool(got) and set(got) == {KEEP_ON}

    def _wait_left(self, name: str, args: dict) -> dict | None:
        """待つかかけ直すか（H2）を伺っている間に、会話AIが、伺う前の位置への報告（元の質問の答え＝record_answer・元の言い直しへの返事＝confirm_field）をした
        （唯一の場所＝_apply_one。当たらなければ None）：お客様は待たずに、伺う前のお話に答えた（「あ、いや、料金のところが気になってたんです。」・
        「あ、やっぱり送ってください。」）＝待たずにお話を続けた1か所（_wait_continue）へ。判定がこの言葉を、待つ・かけ直す・考えている途中・もう一度言ってほしいと
        読んでいれば、その読みが先（_judged_first＝会話AIの報告は判定を待つきっかけだけ）。H2 を伺うきっかけの発話への遅れた報告は当てない（_wait_route の again）。
        ⛔ 2026-11-08 日本時間7時台（11-07 23時26分13秒のご質問の見直しの A-5）：「いまの質問ではありません」で差し戻し、そのあとの H2 の「わからない」の報告で
        「はい、それではこのままお返事を待っております。」と待った（言われた点を捨てた＝本書 9-3「取得済みのことを重ねて聞かない」）"""
        back = self._back_of_wait()
        if back is None or args.get("question_id") != back or self.callee_turns <= self.wait_turn:
            return None
        if name != ("confirm_field" if self.wait_back[1] is not None else "record_answer"):
            return None
        return self._judged_first() or self._wait_continue()

    def _wait_continue(self, prefix: str = "") -> dict:
        """待つかかけ直すか（H2）を伺っている間に、お客様が、待たずにそのままお話を続けた（判定の読み・H2 の答えが「待たずに続ける」＝KEEP_ON・会話AIが伺う前の位置
        へ報告した＝_wait_left）唯一の場所：伺う前の位置へ戻し（_wait_back_to）、その言葉を、戻した位置について判定に読み直してもらう（先に言われた答えを読み直す
        1か所＝_hold_ahead・ahead_context）。会話AIは、その言葉に伺う前の質問の答え・言い直しへの返事が入っていれば報告し（覚えた言葉に当てる＝_held_words）、
        入っていなければ伺う前の質問・言い直しへ戻る（_resume＝プログラムが流す）。待つかかけ直すかは、もう伺わない。返事の頭は「はい、」だけ（ahead_offer）。"""
        qid, confirm = self._back_of_wait(), self.wait_back[1] is not None
        self._wait_back_to()
        said = self.callee_said.strip()
        self.ahead = {"turn": self.callee_turns, "said": self.callee_said, "rest": said, "qid": None, "check": {}, "asked": False}
        self._hold_ahead(qid, back="confirm" if confirm else "answer")
        return {"result": "next", "question_id": qid, "question": self.questions[qid].text, "ahead_offer": True,
                "message": f"{prefix}受け付けました。待つかかけ直すかは、もう伺いません。{self._return_note()}"}

    def _wait_or_later_decided(self, prefix: str, chosen: str | None = None, why: str = "later", offer: str | None = None,
                               read: set[str] | None = None) -> dict:
        """待つか、かけ直すかを伺った答えが決まった（唯一の入口＝_advance・_wait_route）：かけ直す＝かけ直しの申し出（あとで）として、答えの段の一言で伺う／
        待たずに続ける＝伺う前の位置へ戻って、その言葉を読み直す（_wait_continue）／このまま待つ・はっきりしない（わからない・答えたくない・聞き取れない）＝伺う前の
        質問へ戻り、「はい、それではこのままお返事を待っております。」のあと待つ（お言葉は「待って」なので、言葉どおり待つ＝2回は伺わない。戻られたお言葉は、伺う前の
        質問の答えとして受け取る）。read＝判定の読みで決めたとき（_wait_route）の、待つ・かけ直すと読んだ判定の名前（無ければ、受け取った答えの確かめから＝会話AIの
        答えの報告で決めたとき）。"""
        it, check = self.items[self.wait_q], self._material(self.wait_q).get("check", {})   # H2 の答えの確かめ（伺う前の質問の記録ではない）
        if chosen is None:
            chosen = it.condition if it.state in TAKEN else None
        if chosen == KEEP_ON and self._back_of_wait() is not None:
            return self._wait_continue(prefix)
        qid = self._wait_back_to()
        if chosen == CALL_LATER:
            if read is None:
                read = {"ai"} | {k for k in JUDGMENTS if as_wait(check.get(k), True) == LATER}
            return self._call_later(why, read, prefix=prefix, offer=offer)
        r = self._hold(back=True)
        return r | {"message": prefix + r["message"]}

    def _transfer_unclear(self) -> dict:
        """受付の方が、理由を言わずに取り次げないと言った（取り次ぐことはできません、だけ＝ご事情か営業のお断りか分からない）：その場で1回だけ、
        お呼びした方のご事情かを伺う一言（phrases.UNAVAILABLE_CONFIRM。お名前が入る録音＝名乗りの部分 unavailable_confirm）を流す（唯一の場所）。
        返事で分けるのは会話AI：はい・ご事情＝かけ直しの申し出（今はお電話に出られないご事情＝お取り次ぎいただける日時を伺う）／営業のお断り＝
        通話を断られた（もうかけない）／戻る時刻＝かけ直しの申し出（不在）／ほかは返事の中身に合う報告（決まった終え方は置かない）。
        ✅ 2026-11-01 日本時間5時26分 ユーザー：「その場で、「○○様は、今はお電話に出ることができないといったご事情ということでよろしいでしょうか？」などで
        確認すればいいんじゃないですか？それで、営業拒否なのかどちらかを確認できると思いますよ。」・✅ 2026-11-01 日本時間5時48分 ユーザー：「「はい」や事情を言われた → 理由のある取り次げない：かけ直しの申し出+取次ぎ可能な日時の確認の方がいいんじゃないですか？営業はお断りと言われた → 営業のお断り 戻る時刻を言われた → かけ直しの申し出」「確かめのあとの返事でもはっきりしないときは…：返事による」"""
        if self.phase != "calling":
            return self._reject("目的の人には、もう繋がっています（取り次ぎの話ではありません）。")
        if self.judging and self.meaning_said is None and (self.midway or not self.words_complete):
            return self._wait_for_words()
        if self._hold_now():
            return self._hold()
        if self.unavailable_asked:
            return self._reject("取り次げないご事情かは、もう伺いました。受付の方の返事の中身に合う報告を選んでください（はい・ご事情なら"
                                f" request_callback を{ARG_LABEL['why']}「{CALLBACK_LABEL['unavailable']}」で、戻る時刻なら「{CALLBACK_LABEL['absent']}」で、"
                                f"営業のお電話をお断りと言われたら request_end を「{END_LABEL['callee_refused_call']}」で）。")
        self.unavailable_asked = True   # 通話の事実には足さない（終わり方＝かけ直し・通話を断られた などは、今までの事実で残る）
        text = (self.target or {}).get("unavailable_confirm") or self.opening.get("unavailable_confirm")
        if not text:
            return self._reject("お呼びした方のいない業務では、取り次ぎのご事情は伺いません。聞いた言葉に合う報告を選んでください。")
        then = (f"受付の方の返事を待ちます。はい・ご事情を言われたら request_callback を{ARG_LABEL['why']}「{CALLBACK_LABEL['unavailable']}」で、"
                f"戻る時刻を言われたら「{CALLBACK_LABEL['absent']}」で、営業のお電話はお断りと言われたら request_end を「{END_LABEL['callee_refused_call']}」で"
                "呼びます。ほかの返事は、中身に合う報告を選びます（伺うのは1回だけです）。")
        head = {"result": "confirm_unavailable"}
        if self.play_recordings and (self.opening.get("unavailable_confirm") or "unavailable_confirm" in self.system_parts):
            return head | {"play": [self._part_play("unavailable_confirm")],
                           "message": f"{played_note(text, "受付の方への")}何も言わずに、{then}"}
        return head | {"say": text, "message": f"{say_note(text, '受付の方に')}そのあと、{then}"}

    def _who_unclear(self) -> dict:
        """受付の方が、こちらが伝えたお名前などではどの方か分からないので取り次げないと言った（「お名前だけではどの者か分かりかねますので、お繋ぎ
        できかねます。」）：締めずに、まだお伝えしていない部署・役職とフルネームの入った呼ぶ一言（名乗りの部分 target_full＝instruction.Target.full_call）で、
        1回だけお呼びし直す（唯一の場所。お呼びし直すのは _call_target の1か所。頭は「失礼いたしました。」）。お伝えすることが無い（発信対象データに
        フルネームも部署も無い）・もうお呼びし直した ⇒ 取り次いでいただけなかったときと同じお礼の締め（_not_transferred）で、通話の事実 who_unclear を残し、
        繋がらなかった理由は「お名前だけではどの方か分からなかった」（not_reached。かけ直さずに依頼者へ返す＝CL-2。✅ 2026-11-02 日本時間1時22分 ユーザー：
        (a)「フルネームか部署かどちらかが分かれば、言い直す。どちらもなければ、(イ)」）。
        ✅ 2026-11-01 日本時間23時59分 ユーザー：「名前以外に部署やフルネームを伝えて、取り次いでもらう努力をするべきじゃないでしょうか。そうしないと、
        もう一度かけ直しても断られるだけですよ。」"""
        if self.phase != "calling":
            return self._reject("目的の人には、もう繋がっています（取り次ぎの話ではありません）。")
        if self.judging and self.meaning_said is None and (self.midway or not self.words_complete):
            return self._wait_for_words()
        if self._hold_now():
            return self._hold()
        if (self.target or {}).get("target_full") and not self.full_called:
            self.full_called = True
            return self._call_target(sorry=True, part="target_full")
        self._note("who_unclear", recalled=self.full_called)
        return self._not_transferred()

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
        if self.promised and self.target.get("promise_again"):
            # お客様と約束した電話：締めの前に「（お名前）様とお電話の約束をしておりましたので後ほど改めてお掛けしてもよろしいでしょうか？」を伺い、
            # 答えを通話の事実に残してから、お礼の締め（✅ 2026-10-22 ユーザーの言い方・2026-10-24「5つOK」。CL-2 の連絡帳 4-135）
            self.phase, self.order, self.recheck_queue, self.close_after = "asking", [], [], "not_transferred"
            self._add_callback_question("promise_again", self.target["promise_again"])
            return self._advance("", handled=True)
        return self._close("not_transferred")

    # ── 電話が切れた（電話接続側が、回線が切れた瞬間に1回だけ呼ぶ。お客様が切ったときも、こちらが締めて切ったときも同じ）。
    #    ai_speaking＝その瞬間に、AIの声（録音も）を相手に流していたか／callee_speaking＝その瞬間に、相手の声があったか（電話接続側の
    #    声の見張り）。どちらも分からなければ None（相手の声は、会話AIの話し始め・話し終わりの知らせ＝on_activity で補う）
    def on_line_end(self, ai_speaking: bool | None, callee_speaking: bool | None = None) -> dict:
        """切れ方の材料（会話の報告の cut）を決める唯一の場所。決めるのは切れた瞬間の様子だけで、営業拒否か・かけ直すかは判定が決める
        （CL-2 の judge.classify_cut。DESIGN.md 7-1c の表。✅ 2026-10-24 ユーザー決定＝CL-2 の 4-151：材料を出すのは CL-1）。
        - speaker：切れた瞬間に話していた側。相手の声があれば callee（相手の言葉の途中で切れた。AIも話していても＝相手の言葉が途切れた）／
          AIの声を流していれば ai／どちらも無ければ none／分からなければ None
        - in_closing：締めに入っていた・締めのあと（締めの言葉を決めた＝phase が closing。締めの言葉を流している途中に切れても入る）
        - callee_refused_last：相手の最後の発話を、通話の断りとして受け取った（発信禁止の申告 do_not_call が、その発話についた）。
          「結構です」だけの言葉は、断りと決めない（_ambiguous_only。✅ 2026-10-17 ユーザー）＝ここでも断りに数えない
        2回目からは何もしない（最初の瞬間の様子を残す）。"""
        if self._cut is None:
            callee = self.callee_speaking if callee_speaking is None else callee_speaking
            self._cut = {"speaker": "callee" if callee else {True: "ai", False: "none"}.get(ai_speaking),
                         "in_closing": self.phase == "closing",
                         "callee_refused_last": any(f["turn"] == self.callee_turns for f in self.facts.get("do_not_call", ()))}
        return self.cut

    @property
    def cut(self) -> dict | None:
        """会話の報告の cut（切れていなければ None）：切れた瞬間の様子（on_line_end）と、callee_cannot_hear＝お客様の最後の言葉を、どれかの判定が
        「こちらの声が届いていない様子」（HEAR_LOST）と読んだ（✅ 2026-11-02 日本時間21時16分 ユーザー「(ア) OK」。CL-2 は営業拒否にせず、お詫び
        とともにかけ直す）。報告を作るときに決める（切れる直前の言葉の判定は、切れたあとに届くことが多い）。どれか1つの読みで足りるのは、
        読み落とすと、切れ方の分からない通話として確かめのかけ直しに回り、鳴ったのに出なければ営業拒否とみなされる（取り消せない側）のに
        対し、読み過ぎても、お詫びとともにかけ直すだけ（取り消せる側）だから（確かでない読みは止める側だけに使う＝Intent.md 9-4 と同じ向き）。
        callee_silent（お客様の声も物音も届いていない）は電話の音の大きさが要るので、電話接続の担当と本番で決める（いまは送らない＝CL-2 は false）。"""
        if self._cut is None:
            return None
        # 決めたあとに届いた最後の言葉の読み（late_checks）も材料にする（答えには当てないが、切れたあとの材料＝通話後の確かめと同じ側）
        reads = [*self._judged_now().values(), *(v for k, v in self.late_checks.get(self.callee_turns, {}).items() if k in JUDGMENTS)]
        lost = (self.callee_turns in self.hear_leans
                or any(getattr(v, "hear", None) == HEAR_LOST or HEAR_LOST in getattr(v, "hear_lean", ()) for v in reads))
        return {**self._cut, "callee_cannot_hear": lost}

    @property
    def not_reached(self) -> str | None:
        """目的の人のある通話が、目的の人に繋がらずに終わった（いま終わったら）ときの理由（NOT_REACHED_LABEL の中の名前）。会話の報告の
        not_reached に入れる唯一の場所（電話接続側が、通話の終わりに読む＝相手が切った通話も）。繋がった・目的の人の無い業務・人と話して
        いない（留守番電話・自動音声・ファクス・だれも話さない）・番号違い・通話を断られた（発信の拒否）なら None。
        かけ直しの申し出で終えた ⇒ 不在（戻る時刻が取れたかは、通話の事実 callback が持つ。長期の不在・あとで・別の番号へ・後任の方の不在も）／
        ほかの手段の申し出で終えた ⇒ ほかの手段を望まれた／お呼びした方が在籍していない（後任の方にも繋がらなかった・終える業務）⇒ 在籍して
        いない／それ以外（取り次いでいただけなかった・相手が急いでいる・怒っている・呼んでいる段で切られた）⇒ 取り次いでいただけなかった。
        ⛔ 2026-10-22 CL-2 の 4-130：試作に not_reached の理由を報告する所が無かった（理由が無いと、判定は「理由が無い」で判定待ちになる）"""
        if not self.target or self.facts.get("connect") or self.callee_turns == 0:
            return None
        if self.party in NOT_A_PERSON or self.party == "wrong_party" or self.facts.get("do_not_call"):
            return None
        if self.callback_why:
            return "other_means" if self.callback_why == "other_means" else "absent"
        if self.facts.get("who_unclear"):
            # お名前だけではどの方か分からないと言われ、お伝えすることが無い・お伝えしてもまた分からなかった：かけ直さず、依頼者にフルネーム・部署を
            # 足していただく（CL-2 の次の動き）。✅ 2026-11-02 日本時間1時22分 ユーザー：(a)「フルネームか部署かどちらかが分かれば、言い直す。
            # どちらもなければ、(イ)」（(イ)＝「お礼を言って締め、かけ直さない。依頼者には「お名前だけではどの方か分からなかった」と返し、フルネームや部署を
            # 足していただく」）
            return "who_unclear"
        return "departed" if self.facts.get("departed") else "not_transferred"

    # ── 内部
    def _ambiguous_only(self, ai_said: str, ending: bool = False) -> dict | None:
        """相手の言葉が「結構です」「大丈夫です」のような言葉だけなのに、AIが答えとしてでなく「通話を断られた」「聞き取れない」と報告した
        ときの扱い（唯一の場所）。その言葉だけで決めず、いまの質問の答えとして受け取る。当たらなければ None（AIの報告のまま）。
        - 選択肢の質問：前の質問から意味が決まれば、その選択肢（Question.reply_meanings）。決まらなければ、AIに選択肢を選んで報告し直させ、
          意味を確かめる（_op_record_answer）
        - 値を聞く質問：答えの値ではないので、聞き返す（答えが質問に合わないときと同じ。_bad_form）
        - 自由な意見：その言葉のまま、答えとして報告し直させる
        確かめた・聞き返したあとにまた言われたら（ambiguous_asked）、AIの報告のまま（2度目は、前の会話から断りと分かる）。
        - 言い直しの返事待ち（「念のため…でよろしいでしょうか？」のあと）：その言い直しへの返事とも取れる（「それで結構です」）＝確かめへの返事として
          報告し直させる（意味は、言い直しへの返事と同じく判定が読む＝_op_confirm_field）。言い直しごとに1回（ambiguous_confirm_at）
        - 呼んでいる段（電話を終える報告のとき）：理由を言わずに取り次げないと言われたのと同じ（ご事情か営業のお断りか分からない）＝その場で1回だけ
          ご事情かを伺う一言（_transfer_unclear の1か所。✅ 2026-11-01 日本時間5時26分 ユーザー）。もう伺ったあとは、AIの報告のまま
        - 念のための確認の最中（rechecking）：答えが取れなかった質問をもう1回だけ聞いている＝答えの段と同じ
        ✅ 2026-10-17 ユーザー：「『結構です。』だけでは判断できない。人でも前の質問や会話から判断する言葉です」（Intent.md 9-5 の (3)「その言葉だけで
        通話の断りとして締めない」）
        ⛔ G-14：相手の「結構です。」（「ご予約はされていますでしょうか？」への答え）を、AIは 30本中4本で通話の断りとして締めた
        ⛔ 2026-11-02（CL-2 の連絡帳 4-167 の頼み4）まで、言い直しの返事待ち・呼んでいる段・念のための確認の最中では当てず、「念のためご予約はまだ
           ということでよろしいでしょうか？」への「結構です。」も、断りとして締めていた（発信禁止は取り消せない側）"""
        q = self.questions.get(self.current) if self.current else None
        said = (self.heard_said or "").strip()
        word = ambiguous_word(said)
        if not word:
            return None
        if self.phase == "calling":
            return self._transfer_unclear() if ending and not self.unavailable_asked else None
        if self.awaiting_confirm is not None:
            if self.ambiguous_confirm_at == self.confirm_requested_at:
                return None
            self.ambiguous_confirm_at = self.confirm_requested_at
            return self._reject(f"相手の「{said[:20]}」だけでは、{ai_said}とは決められません（いまの言い直し「{self.awaiting_say}」への返事とも"
                                "取れる言葉です）。相手にはまだ何も言わずに、その言い直しへの返事として、すぐ報告してください（意味はプログラムが"
                                "確かめます）。" + self._confirm_how(self.awaiting_confirm))
        if q is None or self.phase not in ("asking", "rechecking") or self.items[q.id].ambiguous_asked:
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
        """答えの値の形が質問の型に合わない（DESIGN.md 6-2）。値は記録せず、聞き返し（_ask_again）へ。電話番号としては合っているが種類が違う番号
        （wrong_number_kind）は、理由を添えて伺い直す側（kind）。"""
        q = self.questions[qid]
        return self._ask_again(qid, "form", f"値の形が合わない（{value}：{why}）",
                               f"{q.id}「{q.text}」の答えとして「{value}」が報告されましたが、{why}。", kind=wrong_number_kind(q.type, value))

    def _ask_ampm(self, qid: str, value: str) -> dict:
        """時刻の午前・午後を、1回だけ聞く（fill_ampm で決められなかったとき）。答えは、次の答えの報告で前の値に入れる（merge_ampm）。"""
        it = self.items[qid]
        it.ampm_asked, it.pending_value = True, value
        it.set("unclear", f"午前か午後かが分からない（{value}。営業時間 {self.business_hours[0]}〜{self.business_hours[1]}・"
                          f"歯止め時間 {self.designated_limit[0]}〜{self.designated_limit[1]}）")
        return {"result": "ask_back", "question_id": qid, "say": AMPM_ASK, "ask_n": 0, "ask_reason": "ampm",
                "message": f"「{value}」の午前・午後が分かりません。{say_note(AMPM_ASK)}相手の答えを待ってください。"
                           + answer_wait("相手が答えたら、聞いたとおりの値で record_answer で報告します。")}

    def _ask_until(self, qid: str, value: str) -> dict:
        """「あとで」のかけ直しの「〇時まで」で、いま電話を続けられない理由が分からない：その時刻の質問を伺ったときと同じ一言（question_play の1か所＝
        callback_question が段で選んだ一言。答えの段は「いつ頃おかけ直しさせていただきましょうか？」・お呼びする段は承認済みの phrases.CALLBACK_ASK の later_time。
        録音で流す通話では、その録音）で1回だけ伺い直す。聞き返しの回数には数えない。
        ⛔ 2026-11-06 日本時間7時台（5時56分55秒のご質問の読み直し EN-A の指摘5）：答えの段でも、お呼びする段の一言（録音 callback_later_time）を流していた
        （✅ 2026-11-06 日本時間0時44分31秒「(イ) 答えの段のかけ直しすべてで大丈夫」）"""
        it = self.items[qid]
        it.until_asked = True
        it.set("unclear", f"「{value}」の、いま電話を続けられない理由が分からない（理由で、かけてよい時間が決まる）")
        line = question_play(list(self.questions.values()), qid)
        say = line["text"]
        hint = (f"相手の答えに、いま電話を続けられない理由（{'・'.join(UNTIL_LABEL.values())}）があれば「{ARG_LABEL['until_why']}」も選んで、"
                "聞いたとおりの値で record_answer で報告します。")
        if self.play_recordings:
            return {"result": "ask_back", "question_id": qid, "ask_n": 0, "ask_reason": "until", "play": [line],
                    "message": f"{played_note(say)}何も言わずに、相手の答えを待ってください。{answer_wait(hint)}"}
        return {"result": "ask_back", "question_id": qid, "say": say, "ask_n": 0, "ask_reason": "until",
                "message": f"「{value}」の、いま電話を続けられない理由が分かりません。{say_note(say)}相手の答えを待ってください。{answer_wait(hint)}"}

    def _ask_again(self, qid: str, reason: str, why: str, shown: str = "", kind: bool = False) -> dict:
        """聞き返しの唯一の場所（DESIGN.md 6-11。聞き取れない・曖昧＝"unclear"／答えが質問に合わない＝"form"）。AIに選ばせず、ここで
        決める：必須の項目は、その場で3回まで聞き返す（言い方は段ごとに phrases.ask_again）。使い切っていたら・必須でない・念のための確認の
        最中なら、「-」にして次へ（必須なら、締めの前の念のための確認で1回）。⇒ どの場合も、AIへの返事に次にすることが必ず入る。"""
        q, it = self.questions[qid], self.items[qid]
        # 形の合わない答えに、理由を添えて伺い直す一言がある質問（システムの質問の役目ごと＝phrases.CALLBACK_ASK_FORM：SMS でご招待する携帯電話の番号に固定電話の
        # 番号）：1回目はその一言（頭は「はい、」＝_ack の form_reason）。理由を添えて伺っても合わなければ、聞き返さずに「-」（その形の答えは無い＝次の段が決める。
        # 携帯電話の番号なら、ほかの方法を伺う＝_line_other_ways）。✅ 2026-11-05 日本時間0時48分 ユーザー「(ア)(イ)ともお勧めの形でOK」。⛔ それまで、理由を言わずに
        # 「恐れ入ります。携帯電話の番号をもう一度…」「もう一回…」と伺い、締めの前に「お声が聞き取れませんでしたので…」とまた伺った（番号は聞き取れている）
        # 理由を添えるのは、電話番号としては合っているが種類の違う番号（kind＝wrong_number_kind）だけ。けたの足りない番号・「はい」は、ふつうの聞き返し
        form_reason = CALLBACK_ASK_FORM.get(q.callback or "") if reason == "form" and kind else None
        # 理由を添える一言は、その形の答えを初めて言われたときに添える（0時48分「1回目に理由を添えて」）。聞き取れない聞き返しをその場の回数だけ使い切った
        # あとなら、締めの前の念のための確認の1回をここで使う（最初の質問のほかに最大3回のまま＝10-14。締めの前にはもう伺わない）。⛔ 11-05 2時05分37秒の
        # ご質問のあと流れの制御に通して見つけた：使い切ったあとの固定電話の番号に理由を添えず「承知しました。」で次へ進み、締めの前に「お声が聞き取れません
        # でしたので…」と伺っていた（番号は聞き取れている）
        # 必須でなくても、判定と食い違えばその場で1回だけ伺い直す質問（Question.asks_once。答えが質問に合わない＝"form" のときだけ。締めの前には伺わない）
        # 申し出（OFFER_ROLES＝詳しい者からのお話 N4・締める前の申し出 N6）は、聞き取れないときも1回だけ伺い直す（取りこぼすと申し出が無くなる・締める前の申し出は
        # そのまま締めになる。✅ 2026-11-09 日本時間6時33分57秒のご指摘を受けた帳面の伺い。⛔ 直す前は N6 への聞き取れないお返事で、伺い直さずに締めていた＝流れの
        # 制御から出したお返事の種類ごとの表で見つけた）。会話が繋がらないお返事は、ここで「もう一度お聞かせ…」と伺い直さず、ひとこと応えてから申し出を伺い直す
        # （応える番 _asked_kind の offer → _offer_again。✅ 7時57分50秒）
        here = self.phase == "asking" and (it.ask_backs < self.limits.ask_back if q.required else
                                           q.asks_once and (reason == "form" or q.callback in OFFER_ROLES) and it.ask_backs < 1)
        spare = bool(form_reason) and not it.form_asked and not here and self.phase == "asking" and q.required and it.rechecks < self.limits.recheck
        if form_reason and (it.form_asked or not (here or spare)):
            # 理由を添えて伺っても形が合わない・もう伺えない（締めの前の念のための確認でも固定電話の番号だった）：その形の答えは無い（聞き取れている＝
            # 聞き取れないとは扱わない。次の段＝ほかの方法を伺う _line_other_ways が決める）
            it.form_failed = True
            it.set("unresolved", f"{why}・理由を添えて伺い直しても形が合わない（その形の答えは無い）" if it.form_asked
                   else f"{why}・形が合わない答えで、もう伺えない（その形の答えは無い）")
            return self._dash(qid, shown)
        if spare:
            it.rechecks += 1
            it.form_asked = True
            it.set("unclear", f"{why}・理由を添えて伺う（締めの前の念のための確認の1回を使う）")
            return {"result": "ask_back", "question_id": q.id, "say": form_reason, "ask_n": it.ask_backs, "ask_reason": reason, "ask_last": False,
                    "form_reason": True, "message": f"{shown}{say_note(form_reason)}相手の答えを待ってください。" + answer_wait()}
        if here:
            it.ask_backs += 1
            it.set("unclear", f"{why}・聞き返す（{it.ask_backs}回目）")
            # 最後の1回か：その場の回数を使い切り、締めの前の念のための確認も無いとき（依頼主の設定で確認を0回にしたとき）
            last = it.ask_backs == self.limits.ask_back and self.limits.recheck == 0
            say = form_reason or ask_again_phrase(it.ask_backs, reason, q.topic, last=last)
            it.form_asked = it.form_asked or bool(form_reason)
            return ({"result": "ask_back", "question_id": q.id, "say": say, "ask_n": it.ask_backs, "ask_reason": reason, "ask_last": last and not form_reason,
                     "message": f"{shown}{say_note(say)}相手の答えを待ってください。" + answer_wait()}
                    | ({"form_reason": True} if form_reason else {}))
        it.set("unresolved", f"{why}・聞き返しを使い切った" if self.phase == "asking" else f"{why}・念のための確認でも聞けなかった")
        if reason == "unclear" and q.required:
            # 聞き取れないまま「-」にした（その場の聞き返しを使い切った・念のための確認でも聞けなかった）違う質問が、答えを1つも受け取らない
            # まま続いた：声が聞き取れない（poor_audio）で、お詫びして終える（_trouble。DESIGN.md 6-8。答えを受け取ったら数え直す＝_apply_one）。
            # 同じ質問を2回（その場と念のための確認）数えない：1つの質問だけが聞き取れないのは、お声のせいとは限らない（お名前などの答えが
            # 聞き取りにくい）＝ユーザーの甚大は「声がお互いや片方がほとんど聞き取れない場合」
            self.unclear_qids.add(qid)
            if len(self.unclear_qids) >= self.limits.trouble_unclear:
                return self._trouble("poor_audio", f"聞き返しを使い切っても聞き取れないまま「-」にした質問が、答えを受け取らないまま"
                                                   f"{len(self.unclear_qids)}つ続いた（{'・'.join(sorted(self.unclear_qids))}）")
        return self._dash(qid, shown)

    def _enter_reading(self, qid: str, prefix: str = "") -> dict:
        """名前の読みの確かめを始める（reading_question）。まず言ってみる読みを値にして、言い直しの返事待ちにする。言い方は質問の文。"""
        q, it = self.questions[qid], self.items[qid]
        it.condition, it.value = q.conditions[0], q.prefill
        it.tried_readings.append(q.prefill)
        it.set("checking", f"読みが決まらない名前「{q.reading_of}」を、まず言ってみる読みで確かめる")
        self.awaiting_confirm, self.confirm_requested_at, self.awaiting_say = qid, self.callee_turns, q.text
        return {"result": "confirm_required", "question_id": qid, "value": q.prefill, "say": q.text, "after": None,
                "message": f"{prefix}次は、お名前の読みの確認です。{say_note(q.text)}相手の返事を待ってください。{self._confirm_how(qid)}"}

    def _ask_confirm(self, qid: str, after: str | None = None) -> dict:
        """言い直して確かめる。after＝値を直されたあとか（AFTER の鍵。場面の言い方・頭の言葉・流す相づちを、その表の1か所から選ぶ）。
        ⛔ 2026-10-14 ユーザー：「失礼いたしました。」のあとの「かしこまりました」はおかしい。こちらの聞き違いなら「失礼いたしました。
        改めまして、…大丈夫でしょうか？」、相手の言い間違いなら「承知しました。それでは、…よろしいでしょうか？」。
        選択肢の型は、選んだ選択肢の意味の言葉で言い直す（choice_words。「念のためご予約はお済みということでよろしいでしょうか？」）。"""
        it, q = self.items[qid], self.questions[qid]
        choice = q.type in CHOICE_TYPES
        if choice and q.maybe and (after == "maybe" or it.condition == ANY or it.maybe_asked == "pending"):
            # どちらの場合もありうると言われた答え（「どっちでもいいですよ。」）は、その答えのまま言い直さない（「念のためどちらでも構わないということで…」は問題外）・
            # 聞き直さない：依頼者が備える答え（取込で決めた Question.maybe）の可能性を確かめる（どの道から来ても＝最初の答え・言い直しへのお返事・判定の読みを
            # 数え直す所、ここで確かめる＝唯一の場所）。答えはその選択肢・言い切っていない印はお客様の言葉（依頼者へ「はい」とお客様の「どっちでもいい」）。
            # 可能性の確かめへのお返事を待っている間の2回目（はっきりしないお返事＝_confirm_again から）も可能性のまま（maybe_settle。言い切りに替えない）
            after = "maybe_settle" if it.maybe_asked == "pending" else "maybe"
            if it.condition == ANY or not it.hedged:
                it.hedged = customer_said(self.heard_said) or ANY_LABEL
            it.condition = it.value = q.maybe[0]
            it.maybe_asked = "pending"
        if choice and (it.condition in q.away_answers or it.condition == NONE) and after not in ("end_doubt", "sense", "maybe", "maybe_settle"):
            # 目的から遠ざかる向きの選択肢は、その向きの言葉で言い直さない（✅ 2026-11-03 ユーザー：「最終目的に繋がるクロージングはマイナスの言葉で締めない
            # ように例を出して言っているよね！」＝「それでは、今月中のご導入は見送りとさせていただきますね？」は「わざわざ、獲得できそうなお客様を失いかねない
            # 確かめ」。明らかにお断りの方・怒っている方は除く＝その答えは言い直しを通らず、お断り・お詫びの締めの道へ行く）。いまの話の向きを伺う一言
            # （end_doubt。「パンフレットはお送りしない方がいいということでしょうか？」＝11-01 22時31分のご判断）はこのまま
            return self._keep_toward_goal(qid, after)
        if choice and takes_for_now(q, it.condition) and (after or "").startswith("hedged"):
            # ほかに分からない点が無いかを伺う質問（取込の unclear_none）への、言い切っていない「無い」（「今のところ質問はありません。」）も、言い直さずに
            # 受け取る（✅ 2026-11-07 日本時間13時05分06秒 ユーザー：「「が」があろうがなかろうが、この言葉を添える方がいいと思います。その後に続きまして と
            # 入るのがスムーズでしょう。」＝確かめ直さずに一言を添えて次へ。添える一言は答えのあとの一言の1か所 _tell_due）
            return self._keep_toward_goal(qid, after)
        if choice and after == "ambiguous" and Flow._hedged_after(q, it.condition) == "hedged_do":
            # どちらとも取れる答え（「大丈夫です。」）を、進める向きの選択肢で伺う：こちらがすることの言葉で（「はい、それではパンフレットをお送りさせて
            # いただきますね？」）。✅ 2026-11-03 ユーザー：「「はい、それではパンフレットをお送りさせていただきますね」だと前に言ったよね！！なぜ、同じ過ちを
            # 繰り返すの？」（11-01 日本時間20時56分のご判断「「念のためパンフレットをお送りして大丈夫だということでよろしかったでしょうか？」よりも、
            # 「それではパンフレットをお送りさせていただきますね？」の方がいいんじゃないですか？」を、言い切っていない答えにしか当てていなかった）
            # （こちらがすることの言葉が無ければ、10-15・10-18 のご判断の「念のため（意味の言葉）でよろしいでしょうか？」のまま＝どちらとも取れる答えの確かめ）
            after = "hedged_do"
        if (not choice and q.type in ESTIMATE_TYPES and not q.callback and after not in TENTATIVE_AFTERS
                and approx_value(it.value, self.heard_said) is not None):
            # 業務の質問の値が確定でない答え（「15日あたりですかね。」「10名くらいです。」）は、最初の答えでも直された値でも、確定の言い直し（「それでは15日と
            # させていただきますね？」）をしない＝受ける形を選ぶ1か所へ（_tentative）。✅ 2026-11-08 日本時間3時48分03秒 ユーザー：「…確定で確認するのはあいての
            # 意思に反します。」。システムの質問（お呼びした方の戻る時刻など＝10-19 にご承認の伺い方）は今までどおり、お客様の言葉の値で言い直す（「お考え」が合わない）
            return self._tentative(qid)
        # 言い直す言葉：いまの話の向きを伺う言葉（end_doubt）・こちらがすることの言葉（hedged_do）は、選択肢ごとの取込の言葉をそのまま。
        # ほかは意味の言葉（choice_words）
        words = ((q.doubt_words[it.condition] + "ということ") if after == "end_doubt" else q.do_words[it.condition] if after in ("hedged_do", "final_do")
                 else self._offer_words(self._proposal_of(qid)[0], *self._offer_parts(qid)) if after in ("offer_again", "offer_alt")
                 else sense_words(q, it.condition) if after == "sense" else q.maybe[1] if after in ("maybe", "maybe_settle")
                 else choice_words(q, it.condition)) if choice else None
        if choice and words is None:
            # 選択肢の名前が返事の言葉だけ（「はい」「いいえ」）で、意味の言葉が取込で決まらなかった＝言い直せない（止めずに進める）。
            # 曖昧な答えは、言い直さずに聞き返す（聞き返しの回数と言い方は _ask_again の1か所）。それ以外は聞き取れた答えのまま受け付ける
            if after in ("ambiguous", "hedged", "hedged_fact", "hedged_noted", "settle"):
                return self._ask_again(qid, self._ask_kind(), "曖昧な答え（言い直す言葉が無い）")
            it.set("accepted", "選択肢の型で、言い直す言葉が無い（聞き取れた答えのまま受け付ける）")
            return self._advance("受け付けました。")
        it.set("checking", "言い直して確かめる")
        self.awaiting_confirm = qid
        self.confirm_requested_at = self.callee_turns
        # 場面：その項目を聞き返した・念のため確かめた・訂正されたあとは「聞き取りにくかったあと」、それ以外は「ふつう」（phrases.CONFIRM・設計書 6-11 の表）。
        # 念のための確認の段の中で初めて伺った質問（ほかの方法・その連絡先・聞き足し）は「ふつう」（⛔ 11-05 2時05分37秒のご質問のあとに見つけた：段の中なら
        # 全部「…とおっしゃいましたでしょうか？」にしていた）
        situation = AFTER[after][0] if after else (
            "聞き取りにくかったあと" if (it.ask_backs or it.corrections or it.rechecks) else "ふつう")
        n = self.confirm_counts.get(situation, 0)
        self.confirm_counts[situation] = n + 1
        # 言い直す値：値の無い選択肢の型（はい／いいえなど。聞き返したあとは復唱する＝6-11）は、選んだ選択肢で言い直す。
        # ⛔ 2026-10-14 に見つけた穴：値だけを渡していたため、「Noneで、よろしいでしょうか。」という文を作っていた
        shown = (words if after in ("end_doubt", "hedged_do", "final_do", "hedged_noted", "sense", "maybe", "maybe_settle", "offer_again", "offer_alt")
                 else restate_words(words, self.heard_said)) if choice else (it.value or label(it.condition))
        said_as = spoken_clock(shown) if q.type in AMPM_TYPES and it.value else shown   # 時刻は「15日の14時、午後2時」の形で言う（値は変えない）
        if q.reading_of:
            shown = quote(it.value)
            it.tried_readings.append(it.value)
        lead = AFTER[after][1] if after else "言い直し"
        # 名前の読みは、質問と同じ言い方で、直していただいた読みを言う（「改めましてお名前の読みの方は「なかしま」様でよろしかったでしょうか？」）
        if q.reading_of:
            phrase = NAME_READING_ASK[q.reading_kind].format(v=shown)
        elif after == "sense":
            phrase = sense_ask(self.heard_said, shown)   # 「（お客様の言葉）とおっしゃったのは、（候補の言葉）という意味合いでしょうか？」
        elif after == "final_do":
            # 迷っている方への最終の確かめ：こちらがすることの言葉（「〜させていただきます」）を「〜させていただきましょうか？」で伺う（_final_check）
            phrase = confirm_phrase((shown if choice else said_as + "とさせていただきます").removesuffix("ます"), situation, n, meaning=True)
        elif choice:
            phrase = confirm_phrase(shown, situation, n, meaning=True, does=self.client_does)
        elif after in ("hedged_do", "hedged_fact"):
            # 値を伺う型の、言い切っていない最初の答え：「それでは15日の14時、午後2時とさせていただきますね？」・「それでは10名ということで記録させて
            # いただいて大丈夫でしょうか？」（_hedged_after）
            phrase = confirm_phrase(said_as + ("とさせていただきます" if after == "hedged_do" else "ということ"), situation, n, meaning=True, does=self.client_does)
        elif after in ("tentative_do", "tentative_fact"):
            # 確定でない値を、ひとまずその値にして変更の受付を添える（_tentative）：「それではひとまず15日あたりとさせていただきますね？変更があった場合には
            # 前日までにご連絡いただけましたらご対応可能です。」（事実を伺う質問は「それではひとまず10名くらいということで承っておいて大丈夫でしょうか？…」。
            # 「15日をめどに」は「ひとまず15日をめどとさせていただきますね？」＝「にと」を重ねない）
            phrase = (confirm_phrase(TENTATIVELY + said_as.removesuffix("に") + ("とさせていただきます" if after == "tentative_do" else "ということ"),
                                     situation, n, meaning=True, does=self.client_does) + self._change_line(q))
        elif after == "noted":
            # 確定でない値を、お客様の言い方の値のまま承っておく（_tentative）：「それでは15日あたりでお考えということで承っておきますね。」（事実を伺う質問は
            # 「お考え」と言わない＝「それでは10名くらいということで承っておきますね。」・「15日をめどに」は「15日をめどにお考え」）
            thinks = "" if q.asks_fact else "お考え" if said_as.endswith("に") else "でお考え"
            phrase = confirm_phrase(said_as + thinks, situation, n, meaning=True)
        elif after == "noted_undecided":
            # 言い直しへ「まだ決まっていない」と返された値を、値を言わずに承っておく（_tentative）：「それではまだ確定ではないと承っておきますね。」。業務設定に
            # 変更の受付の期限があれば、変更の受付の一文を添える（✅ 2026-11-08 日本時間5時33分05秒 ユーザー「(4) イ」）
            phrase = confirm_phrase(said_as, situation, n, meaning=True) + self._change_line(q)
        else:
            phrase = self.confirm(said_as, situation, n)
        say = self.awaiting_say = (self._lead(lead) if lead else "") + phrase
        return {"result": "confirm_required", "value": shown, "say": say, "after": after,
                "message": f"言い直して確かめます。{say_note(say)}相手の返事を待ってください。" + (self._confirm_how(qid) if choice else "")}

    def _change_line(self, q: "Question") -> str:
        """変更の受付の一文を作る唯一の場所（_ask_confirm のひとまずの形・まだ決まっていないと承る一言）。業務設定に期限（Question.change_by）が無ければ空。
        その期限をこの電話でまだお伝えしていなければ「変更があった場合には前日までにご連絡いただけましたらご対応可能です。」（CHANGE_BY）、もうお伝えして
        いれば「お伝えしております通り前日まで変更は可能です。」（CHANGE_BY_AGAIN）。お伝えしたかは期限の言葉ごとに通話で1つ（change_told）＝同じ質問の
        2回目（ひとまずの形のあとの「まだ確定ではないと承っておきますね。」）も、別の質問で同じ期限をお伝えしたあとも短い形（同じ理由＝同じ文を2回言う）・
        期限が違えば初めの形。お客様に聞き取れなかったと言われて言い直すとき（_repeat_now など）は、伺った文（awaiting_say）をそのまま言うので初めの形のまま。
        ✅ 2026-11-08 日本時間6時04分57秒 ユーザー：「・「変更があった場合には前日までにご連絡いただけましたらご対応可能です。」：2回言っているので、2回目は、
        「お伝えしております通り、前日まで変更は可能です。」」"""
        if not q.change_by:
            return ""
        told = q.change_by in self.change_told
        self.change_told.add(q.change_by)
        return (CHANGE_BY_AGAIN if told else CHANGE_BY).format(by=q.change_by)

    def _keep_toward_goal(self, qid: str, after: str | None) -> dict:
        """目的から遠ざかる向きの答え（と「どれも要らない」）を、その向きの言葉で言い直さずに扱う唯一の場所（_ask_confirm の頭）：
        - まだどちらとも言っていない答え（曖昧な答え「大丈夫です」・言い直しで食い違った答え＝ambiguous・settle）：目的に進む向きの選択肢が1つなら、
          その向きで伺う（こちらがすることの言葉で「それではパンフレットをお送りさせていただきますね？」）。1つに決まらなければ、言い直さずに聞き返す
        - 遠ざかる向きに寄っている・言った答え（言い切っていない答え・こちらの言い直しへの「いえ」）：言い直さずに受け取り、「承知しました。」で次へ
          （✅ 2026-11-03 ユーザー：「提案の「承知しました」で次に進むでもOK」。言い切っていなければ「曖昧」の印とお客様の言葉を依頼者へ渡す）
        - ほかに分からない点が無いかを伺う質問への、言い切っていない「無い」（「今のところ質問はありません。」）：同じく言い直さずに受け取る。添える一言
          「何かわからないことがございましたら…」は _tell_due（✅ 2026-11-07 日本時間13時05分06秒 ユーザー）"""
        it, q = self.items[qid], self.questions[qid]
        if after in ("ambiguous", "settle"):
            ahead = q.toward
            if len(ahead) != 1:
                return self._ask_again(qid, self._ask_kind(), "曖昧な答え（目的から遠ざかる向きの言葉では言い直さない・目的に進む向きが1つに決まらない）")
            it.condition = ahead[0]
            if it.value in q.away_answers:
                it.value = ahead[0]
            return self._ask_confirm(qid, after=after)
        # 言い切っていないかは、お客様の言葉で決める（Flow._hedged の1か所。最初の答えは、確かめ方を選んだ所＝_check_again が同じ決まりで hedged の鍵を渡す）。
        # 言い直しへの訂正（「いや、たぶん要らないかもしれません。」）も同じ（⛔ 2026-11-04 まで、確かめ方の鍵が hedged のときだけ付けていたので、訂正・
        # 事実を伺う質問の言い切っていない答えには付かず、依頼者へお客様の言葉が渡らず、最終目的はほかのご希望を伺わずに締めた）
        if (after or "").startswith("hedged") or Flow._hedged(self.heard_said, self.heard_check):
            it.hedged = customer_said(self.heard_said) or label(it.condition)
        self.awaiting_confirm = None
        it.set("accepted", (f"ほかに分からない点は無いという、言い切っていない答え（「{label(it.condition)}」）：確かめ直さずに受け取り、一言を添える"
                            if takes_for_now(q, it.condition) and it.hedged else
                            f"目的から遠ざかる向きの答え（「{label(it.condition)}」）：その向きの言葉では言い直さずに受け取る")
               + (f"（言い切っていない：「{it.hedged}」）" if it.hedged else ""))
        r = self._advance("受け付けました。")
        return r

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

    def _ask_if_follow_ups(self) -> None:
        """前の質問の答えで伺うかが決まる質問（Question.ask_if）を並びに入れる唯一の場所（通話の途中＝_follow_ups・続きから聞く電話の始め＝前の
        通話で取れた答え earlier）：その答えを受け取ったら、前の質問のすぐあとに入れる（並びを戻されても、また入る）。同じ前の質問で伺う質問は、
        トークスクリプトの順に続けて入れる。⛔ 2026-11-02（CL-2 の 4-185 の (2)）まで、どれも前の質問のすぐあとに入れ、2つ以上あると逆の順に
        なった。⛔ 同じ日まで、続きから聞く電話では呼んでおらず、前の通話で取れた答えで伺うはずの質問を伺わなかった（CL-2 の判定は、伺う条件が
        当たった質問を対象外にしない＝取れていない質問のまま、かけ直し続けうる）。"""
        self.order = follow_ups_in(self.order, self.questions.values(), self._read)   # 入れる決まりは1か所（名乗りのあとに最初に伺う質問と同じ）

    def _read(self, qid: str) -> tuple:
        """質問の答えを、並びの決まり（follow_ups_in・gate_of・held）が読む形にする唯一の場所：（状態, 答えの中の名前, 言い切っていないか）。"""
        it = self.items.get(qid)
        return ("empty", None, False) if it is None else (it.state, it.condition, bool(it.hedged))

    def _follow_ups(self) -> None:
        """答えによって足す、システムの質問（唯一の場所。_advance が次の質問を決める前に呼ぶ）：不在で、戻る時刻を伺っても分からなかった ⇒
        本日のお戻りのご予定を伺い、「いいえ」でなければ、改めてお掛けしてよいかを伺う（✅ 2026-10-24 ユーザー「まだ会話に入っていない言い方5つOK」。
        CL-2 の連絡帳 4-140 (a)：「不在なら、出られる日時を聞き出す。聞き出せなかったら、その日はかけない」＝答えはかけ直しの記録の
        back_today・again。同じ日にまたかけるかは CL-2 の次の動きが決める）。"""
        self._wish_step()           # 先に：ほかのご希望で選び直していただいた答えで、業務設定の追って伺う質問が決まる（下）
        self._ask_if_follow_ups()
        self._point_step()
        self._handoff_step()        # あとに：ご承認の手順を点を言っていただけないまま終えた（上で決まる）ら、営業に引き継ぐ架電は詳しい者からのお話を伺う
        self._no_doubts_step()      # あとに：ご承認の手順で「分かった」と伺ったあとの、ほかに分からない点が無いかを伺う質問（上で決まる）
        if self.callback_why != "absent":
            return
        time_q, today_q = CALLBACK_IDS["time"], CALLBACK_IDS["back_today"]
        if today_q not in self.items:
            if time_q in self.items and self.items[time_q].state != "empty" and self.items[time_q].condition == "unknown":
                self._add_callback_question("back_today")
            return
        if self.items[today_q].state != "empty" and self.items[today_q].condition != "いいえ" and CALLBACK_IDS["again"] not in self.items:
            self._add_callback_question("call_again")

    def _point_step(self) -> None:
        """説明した内容のご承認をいただけなかったときの手順（唯一の場所。_follow_ups から）：ご納得いただけなかった点を伺い（N1）、知っていることで
        ご説明し（AIが相手の質問・お話に答える1か所＝_answer・_own_answer。誤解なら正しい中身を。知っていることに無ければ作らずに担当の者に申し伝える）、
        「ほかにご納得いただけない点はございますでしょうか？」（N2）を伺う。無い・分かった＝ご納得（お礼の相づち「ありがとうございます。」で次へ）／分からない
        点がある＝もう一度ご説明（回数では切らない＝分かるまで親身に）／理由を挙げてお断りしようとしている＝ご納得いただけない（押さない）／どちらとも取れない
        （「なるほど」）＝「ご説明した内容でご納得いただけましたでしょうか？」（N3）。N1 の答えも同じに見分ける（点を言った＝ご説明・お断りしようとしている＝
        ご説明で押さない）。点を言っていただけない（言いたくない・どの部分か分からない・聞き取れない・どちらとも取れない）ときは、ご納得は伺わずに手順を
        終える（N3 を伺うのは、点をご説明したあとの「なるほど」だけ。✅ 2026-11-05 日本時間4時48分13秒 ユーザー）。見分けるのは判定の読み（選ぶ質問の答え）＝会話AIの報告だけでは決めない。✅ 6時21分 ユーザー（_ROLE_MEANINGS）。
        ご納得いただければ、ご承認の答えを「はい」にして次のステップへ（面談のお約束・登録・クロージング＝依頼者の業務の質問の順）。ご納得いただけ
        なければ、ご承認は「いいえ」のまま（最終目的なら _goals_lost で締め、その業務はかけない）。ご説明できなかった・はっきりしないときは、最終目的なら
        お礼を言って締め（not_agreed）、もうかけないの印は付けない（改めてご案内するかは依頼者が決める）。伺った点は通話の事実 not_agreed（依頼者へ返す）。
        ✅ 2026-11-02 日本時間5時37分・5時56分 ユーザー（お言葉は CL-1.md 0-2）"""
        nid, oid, aid = (CALLBACK_IDS[r] for r in POINT_STAGES.values())
        p = self.point
        if p is None or p["outcome"] is not None:
            # 前の通話の答えからは、続きの電話の途中で始めない（ほかの質問の答えのすぐあとに「ご納得いただけなかった点は…」と伺うと話がつながらない＝
            # ほかのご希望の _wish_due と同じ決まり。⛔ 2026-11-04 日本時間7時台の4回目のご質問の見直しで見つけた）。ご承認の質問ごとに1回（1つ目の手順を
            # 終えたあと、2つ目のご承認の質問にもご承認いただけなければ、その質問の手順を始める。⛔ 2026-11-06 日本時間5時56分55秒のご質問のあとの読み直しで
            # 見つけた：1通話1回にしていたので、2つ目のお断りでは点を伺わず、報告は「伺い終えていない」になった）
            done = {e.get("question") for e in self.facts.get("not_agreed", [])} | ({p["q"]} if p is not None else set())
            q = next((q for q in self.questions.values() if q.approval and q.id not in self.earlier and q.id not in done
                      and self._approval_open(q) is not None), None)
            if q is not None and self._point_begin(q):
                self._point_step()   # 同じご承認で手順をまた始めた（N1 はもう伺った）：伺ったときのお返事で進める（_point_begin）
            return
        if p["stage"] == "explain" and p["explained"] is None and (src := p.get("src")) is not None:
            # ご説明を頼んだあと、ご説明の前に、お客様が同じ段の質問にまた答えた（会話AIがその質問の答えとして報告し直した。src＝点を伺った質問・その項目・
            # そのときの履歴の長さ＝_point_heard）：言い足した点（「あ、あと解約の条件も気になってます。」）は同じ回の点に足して、まとめてご説明する（回は数えない）・
            # お断りの向きなら押さずに手順を終える・「ほかに…」の分かった（ご説明の前に分かった）ならご納得で終える。ほかの答え（わからない・どちらとも取れない
            # など）は、伺った点のご説明を続ける（ご説明するまで頼み続ける＝_explain_due）。⛔ 2026-11-08 日本時間6時台（11-07 23時26分13秒のご質問の見直しの
            # A-1）：この段の枝が無く、言い足しを受けると、ご説明せずに次の質問へ進み、言い足した点も記録に残らなかった
            sid, it, seen = src
            if self.items.get(sid) is it and len(it.history) > seen and it.state in GOT:
                role = next(r for r in POINT_STAGES.values() if CALLBACK_IDS[r] == sid)
                if self.questions[sid].declined(it.condition):
                    self._point_heard(it.value, sid, explain=False)
                    self._point_end("declined", by=sid)
                elif it.condition == POINT_AGREED.get(role):
                    self._point_end("agreed", by=sid)
                elif it.condition == POINT_SAID[role]:
                    self._point_heard(it.value, sid)
                else:
                    p["src"] = (sid, it, len(it.history))
            return
        if p["stage"] == "ask" and self.items[nid].state in TAKEN + ("unresolved",):
            cond = self.items[nid].condition if self.items[nid].state in GOT else None
            if cond == POINT_SAID["not_agreed"]:
                self._point_heard(self.items[nid].value, nid)
            elif self.questions[nid].declined(cond):
                self._point_heard(self.items[nid].value, nid, explain=False)
                self._point_end("declined", by=nid)
            elif p["again"] and self.items[nid].state != "refused" and self._approval_explanation():
                # 続きの電話の始めに、点を言っていただけなかった（「覚えていない」・どちらとも取れない・聞き取れない）：会話AIにご説明させず・黙って次の質問へも
                # 進まず、「ごもっともです。」（覚えていない・分からない＝わからない。ほかは「はい、」）＋振り返りの一言「前回は『（ご承認いただく内容の説明の文）』と
                # お話ししたところでお電話が切れてしまいました。」を流し（「ほかに…」N2 の言い方の1つ＝callback_question の recap）、お客様の返事を N2 と同じ3つで
                # 読む（下の other：点を言った＝その点をご説明・分かった＝お礼で次へ・断ろうとしている＝押さない・どちらとも取れない＝手順を終える）。記録は手順を
                # 終えたときに1つ（_point_end。点の無い記録をここで作ると、「覚えていない」の言葉を、次の電話が点としてご説明する＝earlier_point_said）。
                # ✅ 2026-11-05 日本時間4時16分52秒 ユーザー（お言葉は phrases.recap_line）。⛔ それまでの形＝0時48分の (ア)（会話AIが改めてご説明）・3時49分
                # （説明の文が無ければ黙って手順を終える）は、お客様に聞かれていないことを答える・話題を変える形だった。答えたくない（点を言いたくない）ときは、
                # この電話で点を言っていただけなかったときと同じく手順を終える（下の else）。説明の文が無い（振り返る中身が無い）ときは ⏳（次の elif）
                p["stage"] = "other"
                p["gomottomo" if self._unknown_agreed(nid) else "hai"] = self.callee_turns
                self._ask_point("other_point", nid, recap=True)
            elif p["again"] and self.items[nid].state != "refused":
                # 続きの電話で、振り返る説明の文が無い（説明が質問の文の中にある＝最初の質問がご承認の質問）：ご納得は伺わず（4時48分13秒）、点は1回伺ったので
                # 手順を終え、次へ進む前に「何か思い出されたりご不満な点がございましたらいつでもお申し出ください。」を挟む（leave_open。頭は、覚えていないと言われ
                # たら「ごもっともです。」・ほかは「はい、」）。✅ 2026-11-05 日本時間5時20分56秒 ユーザー：「(b)…同上」（下の振り返りのあとと同じ）
                self._leave_open("gomottomo" if self._unknown_agreed(nid) else "hai", "point_anytime")
                self._point_end("unresolved", by=nid)
            elif self.items[nid].state == "dont_know" and not p.get("which"):
                # この電話で、点を言っていただけなかった（どの部分か分からない・どちらとも取れない＝「うーん、なんとなくですかね。」。言いたくない・聞き取れないは
                # 下の else）：手順を終える前に1回だけ「承知しました。」＋「その中でもどのあたりが特に気になっていらっしゃいますでしょうか？」（共通の一言の録音
                # callback_not_agreed_which＝callback_question の which）で、お客様の一言を引き出す。お返事は同じ質問（N1）の答えとして読む（点を言った＝ご説明・
                # 断ろうとしている＝押さない・それでも点を言っていただけない＝下の else で手順を終える）。迷っているお客様に「どういった点でお迷いでしょうか？」で
                # 伺った N1 も同じ。✅ 2026-11-09 日本時間3時46分53秒 ユーザー：「「その中でも、どのあたりが特に、気になっていらっしゃいますでしょうか？」の
                # ような汎用的な言葉で相手の一言を引き出すのが使いやすいんじゃないでしょうか？」
                p["shouchi"] = self.callee_turns   # 「承知しました。」で受けて伺う（答えの N1 を伺い直すので、受け取った「わからない」の相づちの決まりは当たらない）
                self._ask_point("not_agreed", nid, which=True)
            else:
                # 点を言っていただけなかった（この電話＝言いたくない・どの部分か分からない・聞き取れない／続きの電話＝言いたくない）：ご納得は伺わず、押さずに
                # 手順を終える（ご承認は「いいえ」のまま・点は無し＝依頼者へ返す。最終目的ならお礼を言って締め、もうかけないの印は付けない＝_point_end の
                # unresolved）。✅ 2026-11-02 日本時間4時36分のご決定「ご納得いただけなかった点を1回伺って依頼者へ返す・ご承認が最終目的なら伺ってから締める」・
                # 10-13「答えたくない＝二度と聞かない」・6時21分「お断りしようとしているときは押さない」。✅ 2026-11-05 日本時間4時48分13秒 ユーザー：「点を言いたくない
                # やどちらとも取れない…納得していない状況で→ こちら「はい、ご説明した内容でご納得いただけましたでしょうか？」は対尾王としてありえないんじゃないですか？」
                # ⛔ それまで、ご決定に無い「ご納得を伺う」（N3）を CL-1 が足していた。
                # 次へ進む前（締めるときも）に、伺い直さずに「ご不満な点がございましたらいつでもお申し出ください。」を挟む（leave_open。「何か思い出されたり」は、
                # 前の電話のことを伺う場面ではないので言わない＝phrases.POINT_ANYTIME_PLAIN。頭の相づちは、答えの決まり（_ack_choice の「わからない」「答えたくない」
                # ＝「承知しました。」・聞き返しを使い切って先へ進む＝「はい、」）と同じ。この一言の頭なので、続く念のための確認のお詫びにはしない）。
                # ✅ 2026-11-05 日本時間5時45分 ユーザー：「（例えば「承知しました。ご不満な点がございましたらいつでもお申し出ください。続きまして…」のような形は
                # いかがでしょうか。）その形でOK、進めてください」
                self._leave_open(self._unsaid_head(nid), "point_anytime_plain")
                self._point_end("unresolved", by=nid)
        elif p["stage"] == "explained":
            if p["explained"]:
                p["stage"] = "other"
                # 続きの電話の始めからご説明した（resumed）：「ほかに…」は並びの先頭（前の通話で伺えなかった質問より先＝ご説明の続き）
                self._ask_point("other_point", self.current or (None if "resumed" in p else p["q"]))
            else:
                self._point_end("unresolved")
        elif p["stage"] == "other" and self.items[oid].state in TAKEN + ("unresolved",):
            cond = self.items[oid].condition if self.items[oid].state in GOT else None
            which = p.get("which", False)   # いまの「ほかに…」は、どの点かを1回伺う一言か（立てるのは伺う所 _ask_point の1か所）
            if cond == POINT_AGREED["other_point"]:
                self._point_end("agreed", by=oid)
            elif cond == POINT_UNSAID and not which:
                # 分からない点があるとだけ言って、どの点かを言っていない：プログラムが1回「ほかにご不明な点はどのようなことになりますでしょうか？」と伺う（会話AIに
                # 伺わせない＝お客様に伺うことはプログラムが流す＝9-3。⛔ 2026-11-06 日本時間8時台（5時56分55秒のご質問の読み直し EN-D の指摘6）：会話AIに「どの点か
                # 伺ってから」と頼み、問いかけは止められて無言が続き、言っていない点を「そのご質問は…申し伝え」た）。✅ 2026-11-06 日本時間9時05分17秒 ユーザー：
                # 「(a) ほかに分からない点が「ある」とだけ言われたとき：「ほかにご不明な点はどのようなことになりますでしょうか？」」
                p["hai"] = self.callee_turns   # 「はい、」で受けて伺う（お礼の相づちは合わない＝「なるほど」のあとの N3 と同じ）
                self._ask_point("other_point", oid, which=True)
            elif cond == POINT_SAID["other_point"]:
                # 分かるまでご説明する（回数では切らない）。振り返りの一言への返事で点を言っていただいた（recap＝伺う所 _ask_point が決める印）は、1つ目の点（「ほかにも」
                # ではない）
                if not p.get("recap"):
                    p["round"] += 1
                self._point_heard(self.items[oid].value, oid)
            elif self.questions[oid].declined(cond):
                self._point_heard(self.items[oid].value, oid, explain=False)
                self._point_end("declined", by=oid)
            elif which or self.items[oid].state == "refused" or (self.items[oid].state == "unresolved" and self.items[oid].condition is None):
                # どの点かを伺っても点を言っていただけない（また点は言っていない・どちらとも取れない・言いたくない・聞き取れない）：「ほかに分からない点がある」と
                # 言ったお客様にご納得は伺わず（下の else の N3 にしない）、点を言っていただけないときと同じく押さずに手順を終え、一言を挟む（4時48分13秒・5時45分の
                # ご決定＝上の stage ask の else）。挟む一言は、続きの電話の振り返りの一言（recap）への返事なら、前の電話のことを伺う場面なので「何か思い出されたり…」
                # （下の of の枝と同じ＝5時20分56秒の (a)）、ほかは「ご不満な点が…」。⛔ 伺う一言を足したとき、伺ったあとの聞き取れない・言いたくないが N3 へ
                # 流れていた（振り返りの印 of でしか止めていなかった）
                # ご説明のあとの「ほかに…」への言いたくない・聞き取れない（答えが無いまま「-」）も同じ（点を言っていただけない＝ご納得は伺わない。N3 は「なるほど」
                # のようなどちらとも取れない返事と、遅れた判定で答えを取り消して戻した段＝本書 9-5「戻した段の答えは「-」…どちらとも取れない」だけ。⛔ 2026-11-06
                # 日本時間13時台の見直しで見つけた：下の else で N3 を伺っていた）
                # 振り返りへの返事でも、言いたくない（覚えてはいる）は「何か思い出されたり」が合わない＝「ご不満な点が…」（本書 9-5 の続きの電話の答えたくない）
                recap = p.get("recap") and self.items[oid].state != "refused"
                self._leave_open(self._unsaid_head(oid), "point_anytime" if recap else "point_anytime_plain")
                self._point_end("unresolved", by=oid)
            elif self.questions[oid].of:
                # 振り返りの一言（続きの電話の始め＝recap）への返事も、点を言っていただけない（「そうでしたっけ。」・覚えていないまま・聞き取れない）：上と同じく、
                # ご納得は伺わずに手順を終える（点は1回伺った＝4時36分。✅ 4時48分13秒のお言葉）。次へ進む前に、伺い直さずに一言を挟む（leave_open。頭は「はい、」
                # ＝振り返りの頭で「ごもっともです。」をもう言った）。✅ 2026-11-05 日本時間5時20分56秒 ユーザー：「お客様「そうでしたっけ。」と言われて、こちら
                # 「はい、続きましてご訪問の日時のご希望はございますでしょうか？」は流れとしておかしくないですか？：「はい、何か思い出されたり、ご不満な点が
                # ございましたらいつでもお申し出ください」のような言葉を挟んだ方がいいんじゃないでしょうか？「そうでしたっけ。」とはっきりしない状態で、
                # 「はい、改めましてただいまご説明した内容でご承認いただけますでしょうか？」伺い、承認しますとお客様がいうわけないですよね。」
                self._leave_open("hai", "point_anytime")
                self._point_end("unresolved", by=oid)
            else:
                p["stage"], p["hai"] = "confirm", self.callee_turns   # 「なるほど」を「はい、」で受けて伺う（お礼・承りの相づちは合わない）
                self._ask_point("agreed_now", oid)
        elif p["stage"] == "confirm" and self.items[aid].state in TAKEN + ("unresolved",):
            cond = self.items[aid].condition if self.items[aid].state in GOT else None
            self._point_end("agreed" if cond == POINT_AGREED["agreed_now"] else "declined" if self.questions[aid].declined(cond) else "unresolved", by=aid)

    def _unsaid_head(self, qid: str) -> str:
        """点を言っていただけずに手順を終えるとき、挟む一言（「ご不満な点が…」）の頭の相づちを決める唯一の場所：聞き取れた言葉で点を言っていただけなかった
        （わからない・答えたくない・どの点かは言っていない）＝「承知しました。」（答えの決まり＝_ack_choice の「わからない」「答えたくない」と同じ）、
        聞き取れない・どちらとも取れない＝「はい、」（聞き返しを使い切って先へ進むときと同じ）。✅ 2026-11-05 日本時間5時45分 ユーザー：「（「承知しました。
        ご不満な点がございましたらいつでもお申し出ください。続きまして…」のような形）その形でOK、進めてください」"""
        it = self.items[qid]
        # わからない（dont_know）は、判定も「決められない」と読んでいなければ（_unknown_agreed＝わからないかどちらとも取れないかを決める1か所）「承知しました。」。
        # ⛔ 2026-11-06 日本時間13時台の見直しで見つけた：項目の状態だけで決め、どちらとも取れない言葉にも「承知しました。」だった
        return ("shouchi" if it.state == "refused" or (it.state == "dont_know" and self._unknown_agreed(qid))
                or (it.state in GOT and it.condition == POINT_UNSAID) else "hai")

    def _unknown_agreed(self, qid: str) -> bool:
        """受け取った「わからない」（dont_know）を、判定も「決められない」と読んでいないか＝覚えていない・分からないと言った（「ごもっともです。」で受ける）の唯一の
        決まり（_point_step）。判定が「決められない」と読んだ言葉（「そうでしたっけ。」＝どちらとも取れない）は「はい、」で受ける（本書 9-5：「ごもっともです。」
        （覚えていない・分からないとき。ほかは「はい、」））。判定を使わない通話は、会話AIの報告のまま。⛔ 2026-11-06 日本時間5時56分55秒のご質問のあとの
        読み直しで見つけた：会話AIの報告だけで決め、どちらとも取れない言葉に「ごもっともです。」を返していた"""
        check = self._material(qid).get("check", {})   # 受け取った報告の操作の中（記録はそのあと＝_apply_one）なら、いまの言葉の確かめ
        return self.items[qid].state == "dont_know" and Flow._meaning_of(check) != UNDECIDED

    def _sorry_lines(self) -> list[dict]:
        """続きの電話の始めに、名乗りのあと（目的のお客様のいる業務は繋がってから）流すお詫びの録音の並びを作る唯一の場所（_opening_reply・_connect）：予約証の
        理由ごとのお詫び（phrases.APOLOGY_FIRST）。前の電話で申し伝える一言まで言って、お返事の前に切れた（told_again＝resumed_step の told）なら、途中で
        切れたお詫びの代わりに、お話の最後に切れたお詫び（phrases.APOLOGY_CUT_END。同じ日＝先ほど・あとの日＝前回＝apology_when）と、申し伝える一言
        （前の電話の種類の件＝TOLD_AGAIN。「先ほど」「前回」はお詫びと同じ apology_when・担当から連絡すると申し出てよい業務は TOLD_FOLLOW_UP）。✅ 2026-11-06 日本時間9時05分17秒 ユーザーの (b)。
        流したら空にする（2回は流さない）。"""
        told, self.told_again = self.told_again, None
        # お話の最後に切れたお詫びは、申し伝え直したあとに締める電話だけ（ご承認が最終目的＝_advance の not_agreed・残りの質問が無い）。申し伝えたあとも
        # 話が続く電話は、お話の途中で切れたので今までのお詫び。並びは名乗りの録音の文（instruction.Business.opening_parts）と同じ1か所 sorry_texts
        at_end = told is not None and (self.questions[self.point["q"]].final_goal or not self._remaining())
        # 承認したお詫び（"apology"）は、お詫びだけの電話と同じ流し方（kind "apology"＝お詫びの案ごとの録音・_apology_line と同じ形）
        lines = [{"kind": "apology", "text": text} if rid == "apology" else {"kind": "recorded", "id": rid, "text": text}
                 for rid, text in sorry_texts(self.apology_due, told, at_end, self.apology_when, self.policies["follow_up"], self.apology_text)]
        self.apology_due = []
        if told is not None:
            # 申し伝え直した：手順の終わり（unresolved）と記録（このあとまた切れても、3回目は言い直さない＝_point_told）
            self.point["outcome"] = "unresolved"
            self._point_record(**self.point.pop("told_record"))
        return lines

    def _point_told(self, q: "Question") -> None:
        """前の電話で、ご説明できない点の一言（申し伝える一言）まで言って、お返事の前に切れた（resumed_step の told）：この電話では、お詫びのあとに申し伝える
        一言を言い直すだけで、手順は終えたものとする（ご納得は伺わない＝ご説明できなかったときと同じ。最終目的ならお礼を言って締める＝_advance の not_agreed）。
        記録は手順の終わり（unresolved）と、申し伝え直したこと（told）を、申し伝える一言を流す時に残す（_sorry_lines。このあとまた切れても、3回目は言い直さない）。
        ✅ 2026-11-06 日本時間9時05分17秒 ユーザーの (b)"""
        f = earlier_fact(self.earlier.get(q.id) or {}, "point_fact") or {}
        kind = told_kind(self.earlier.get(q.id))
        self.told_again = kind
        # 手順の終わり（unresolved）と記録は、申し伝える一言を流す時（_sorry_lines）に。それまでに切れたら、手順は終えていない（報告の unfinished＝次の電話で
        # また申し伝え直す）。⛔ 2026-11-06 日本時間13時台の見直し（13時27分07秒のご質問・ご承認の手順の持ち場の指摘8）で見つけた：ここ（電話の始め）で書いて
        # いたので、目的のお客様に繋がる前・名乗りの前に切れた電話でも「申し伝え直した・手順の終わり」と残り、一言はお客様に届かないまま次の電話でも言わなかった
        self.point = {"q": q.id, "stage": "told", "round": 1, "explained": False, "turn": None, "outcome": None, "by": None,
                      "hai": None, "again": True,
                      "told_record": dict(point=f.get("point"), said=(f.get("said") or "")[:80] or None, explained=False, told=kind, outcome="unresolved")}

    def _approval_open(self, q: "Question") -> str | None:
        """ご承認いただけなかったときの手順を始めるご承認の答えか（唯一の決まり＝手順を始める _point_step・伺う一言を選ぶ _point_begin・終える _point_end）：
        "declined"＝お断り（Question.declined）／"hesitating"＝ご承認を迷っている（_hesitate_now の approval で受け取った「わからない（まだ決まっていない）」＝
        印 hesitating の approval。✅ 2026-11-08 日本時間2時21分15秒 ユーザー「(1)〜(4)ともイでOK」の (4)：ご承認いただけなかったときの手順と同じ道で
        「はい、どういった点でお迷いでしょうか？」と伺ってご説明し、保留では締めない）／None＝どちらでもない。"""
        it = self.items[q.id]
        if it.state in GOT and q.declined(it.condition):
            return "declined"
        return "hesitating" if it.state == "dont_know" and "approval" in self.hesitating.get(q.id, {}) else None

    def _point_begin(self, q: "Question", again: bool = False, said: str | None = None) -> bool:
        """ご承認いただけなかったときの手順を始める唯一の場所（この通話のご承認のお断りのあと＝_point_step・続きの電話の始め＝Flow.__init__）：ご納得いただけ
        なかった点（N1）を伺う。N1 の前の相づちは、手順のほかの質問と同じ「はい、」（ご承認いただけなかった答えにお礼は合わない）。again＝伺っている途中で
        切れた電話の続き（前の通話の答えに "point" が無い＝resumed_step）：お詫びと用件の一文のすぐあとに、切れた所から「前回お伺いした件でご納得いただけ
        なかった点は…」と伺う（✅ 2026-11-04 日本時間18時16分 ユーザー「はい、OK」）＝並びの先頭に入れる（目的のお客様のいる業務は、繋がってから同じ順）。
        名乗りの録音に続けて流れるので、頭の相づちは付けない。said＝前の通話でご納得いただけなかった点を伺ったお客様の言葉（前の通話の答えの "point_said"。
        "point" が heard＝ご説明の途中で切れた）：点を伺い直さずに、ご説明する段から始める（AIへの知らせ＝_point_note・名乗りの録音のあと＝_opening_reply・
        目的のお客様に繋がったあと＝_connect の _advance。どちらも _explain_due の1か所）。✅ 2026-11-04 日本時間19時26分 ユーザー「(A)…OK」
        返す値＝同じご承認でこの通話にやめた手順をまた始め、N1 を伺い直さなかったか（True なら、呼び手 _point_step がそのお返事で手順を進める）。"""
        # again＝続きの電話の始めから（この電話ではまだご説明していない＝点を伺えなければ、振り返りの一言を流してから伺う＝_point_step の recap）
        # hesitating＝ご承認を迷っているお客様の手順（_approval_open）：1つ目の一言は「どういった点でお迷いでしょうか？」（_ask_point）・お断りの向きで終えたら
        # ご承認を「いいえ」に決め直す（_point_end）・会話AIへの知らせは迷っている点（_point_note）。ほかは同じ道
        self.point = {"q": q.id, "stage": "ask", "round": 1, "explained": None, "turn": None, "outcome": None,
                      "hai": None if again else self.callee_turns, "again": again, "hesitating": self._approval_open(q) == "hesitating"}
        if said is not None:
            # turn＝None：この通話のどの発話にも当たらない（続きの電話の始めに、まだご説明していない間は、ご説明を頼む＝_explain_due）
            self.point.update(stage="explain", resumed=said)
            return False
        self._ask_point("not_agreed", None if again else q.id, again=again)
        return False

    def _ask_point(self, role: str, after: str | None, again: bool = False, recap: bool = False, which: bool = False) -> None:
        """ご承認いただけなかったときの手順のシステムの質問を、前の質問のすぐあとに伺う唯一の場所（after＝None：並びの先頭。2回目からの「ほかに…」は、答えを
        空にして、言い方を替えて伺い直す＝callback_question の nth。again＝続きの電話の始めに、切れた所から伺う一言＝_point_begin。recap＝続きの電話の始めに
        点を言っていただけなかったときの振り返りの一言＝「ほかに…」の言い方の数には入れない＝そのあとの「ほかに…」は1つ目の言い方から）。"""
        if role in CALLBACK_ASK_WHICH:
            # どの点かを1回伺う一言か（N1＝点を言っていただけなかったときの「その中でも…」・N2＝下の「ほかに…」）：同じ段で2回は伺わない（_point_step）
            self.point["which"] = which
        if role == "other_point":
            # いまの「ほかに…」が、どの点かを1回伺う一言か（_point_step の which）・続きの電話の振り返りの一言（とそのあとのどの点か）＝まだ点を1つも伺っていない
            # 「ほかに…」か（recap）。伺い直すたびにここで決める＝答えを遅れた判定で取り消して同じ質問へ戻っても、印は伺った質問のまま（⛔ 答えを受けた時に消して
            # いたので、取り消したあと同じ一言をもう一度伺った（which）・挟む一言が「何か思い出されたり…」から「ご不満な点が…」に変わった（recap＝13時台の見直しの B7））
            self.point["recap"] = recap or (which and bool(self.point.get("recap")))
        if recap:
            q = callback_question(role, recap=self.questions[self.point["q"]])
        elif which:   # どの点かを1回伺う一言（言い方の数には入れない）
            q = callback_question(role, which=True, after_recap=bool(self.point.get("recap")))   # 振り返りの一言への返事なら「ほかに」を外す
        else:
            asked = self._point_marks()["asked"]   # 伺った数（ご承認の質問ごとに通話で1つ）
            nth = asked.get(role, 0)
            asked[role] = nth + 1
            q = callback_question(role, nth=nth, again=again, hesitating=role == "not_agreed" and self.point.get("hesitating", False))
        self._new_item(q)
        self.follow_ups[q.id] = self.point["q"]
        if after is None:
            self._put_first(q.id)
        else:
            self._insert_after(after, q.id)

    def _point_heard(self, point: str | None, by: str, explain: bool = True) -> None:
        """ご納得いただけなかった点・お断りの理由を伺った（by＝その答えの手順の質問）：点は通話の事実に残す（依頼者へ返す）。点なら、AIがご説明する段へ
        （_advance が知らせる）。この回のご説明の前（_explain_due）に言い足された点・お断りの理由は、同じ回の記録に足す（点と根拠の発話を「／」でつなぐ。
        同じ点・同じ言葉は重ねない＝_point_step の言い足しの枝）。"""
        p = self.point
        e = self._point_fact() if self._explain_due() else None
        if explain:
            p.update(stage="explain", turn=self.callee_turns, explained=None, src=(by, self.items[by], len(self.items[by].history)))
        if e is not None:
            said = (self.callee_said or "").strip()[:80]
            for key, new in (("point", point), ("said", said)):
                have = [x for x in (e.get(key) or "").split("／") if x]
                if new and new not in have:
                    e[key] = "／".join(have + [new])
            if not explain:
                e["declining"] = True
            return
        # point＝ご納得いただけなかった点・お断りの理由の値（AIの報告の値。無ければ None）・said＝根拠の発話（_note の1か所）。✅ CL-2 の 4-189：
        # 「said＝根拠の発話・point＝ご納得いただけなかった点の値です。書き分けてください」（⛔ 11-03 まで、根拠の発話を said に上書きし、値を持たなかった）
        self._point_record(point=point, **({} if explain else {"declining": True}))

    def _point_record(self, **extra) -> None:
        """ご承認いただけなかったときの手順の通話の事実 not_agreed を書く唯一の場所（_point_heard・_point_said・_point_end）：根拠の発話（said）は、いまのお客様の
        言葉。ただし点（point）が前の電話で伺った点（続きの電話の始めからご説明した＝resumed）なら、前の電話で点を伺ったお客様の言葉（✅ CL-2 の 4-189「said＝
        根拠の発話」。⛔ 2026-11-05 日本時間2時05分37秒のご質問のあとの読み直しで見つけた：この電話の第一声（「はい、テスト商事でございます。」）を入れていた
        ので、そのあとまた切れると、次の電話はその第一声を点としてご説明した＝earlier_point_said は said を先に読む）。turn は、この電話でご説明した時の番号"""
        p = self.point
        if extra.get("point") is not None and extra.get("point") == p.get("resumed"):
            extra["said"] = extra["point"][:80]
        self._note("not_agreed", question=p["q"], round=p["round"], **extra)

    def _point_fact(self) -> dict | None:
        """いまのご承認の手順の、この回の通話の事実 not_agreed（無ければ None）。"""
        p = self.point
        return next((e for e in self.facts.get("not_agreed", []) if e.get("question") == p["q"] and e.get("round") == p["round"]), None) if p else None

    def _resumed_explaining(self) -> bool:
        """続きの電話の始めのご説明の途中か（前の電話で点まで伺えた＝_point_begin の said。まだご説明していない）の唯一の決まり（AIへの知らせ _point_note・
        中身の判定に渡す言葉 _answered_words・受け取る所 _explain_problem）。"""
        p = self.point
        return p is not None and "resumed" in p and p["stage"] == "explain" and p["turn"] is None and self._point_fact() is None

    def _explain_problem(self, a: dict) -> str | None:
        """ご説明を頼んでいる間（_explain_due）に、受け取れない応え方なら、その理由（_op_replay_opening の1か所）：
        - 受け答え（talk）・質問に戻る（resume）だけ＝ご説明していない（⛔ 2026-11-05 日本時間2時05分37秒のご質問のあとの読み直しで見つけた：受け答えの
          「さようでございますか。」「お世話になっております。」をご説明したと数え、何も説明していないのに「ご説明した内容でご納得いただけましたでしょうか？」
          （0時48分の (ア)）・「ほかにご納得いただけない点は…」（19時26分の (A)）へ進み、記録も「ご説明できた」にしていた）
        - 続きの電話の始めのご説明（_resumed_explaining）：想定問答の録音（話し始めの言葉が入らない）・話し始めの言葉（RESUMED_LEAD）から始まっていない自分の
          言葉の文。✅ 2026-11-04 日本時間19時26分 ユーザー「(A)…OK」（お見せした形：「前回お伺いした料金の件ですが、料金は月額3,000円で、初月は無料と
          なっております。」）。⛔ 21時台の見直しで見つけた：会話AIへの頼み（_point_note）だけで、受け取る所が見ていなかった"""
        part = a.get("part") or ""
        if not self._explain_due():
            return None
        if part in ("talk", "resume"):
            return "いまは、ご説明する番です（受け答えや、質問に戻るだけでは、ご説明になりません）。" + self._point_note()
        if self._resumed_explaining() and (part.startswith(ANSWER_PREFIX) or (part in ("own", "no_answer")
                                                                              and not (a.get("answer") or "").strip().startswith(RESUMED_LEAD))):
            return "続きの電話の始めのご説明は、録音を選ばずに、話し始めの言葉から自分の言葉で言います。" + self._point_note()
        return None

    def _point_said(self, kind: str) -> None:
        """ご説明する段で、AIが応えた（_answer・_own_answer・_own_fallback）：知っていることでご説明できたか（答えを持っていない質問＝ご説明できない）。
        続きの電話の始めのご説明なら、その回の記録をここで作る（点＝前の電話で伺った点のお客様の言葉。⛔ 21時台の見直しで見つけた：作っていなかったので、
        依頼者へ点 None・ご説明できたか無しで返していた）。"""
        p = self.point
        if p is not None and p["stage"] == "explain" and p["explained"] is None:
            if self._resumed_explaining():
                # 根拠の発話（said）も、前の電話で点を伺ったお客様の言葉（この電話の言葉は点の根拠ではない＝_point_record）
                self._point_record(point=p["resumed"])
            p["explained"] = kind != "no_answer"
            if (e := self._point_fact()) is not None:
                e["explained"] = p["explained"]

    def _point_end(self, outcome: str, by: str | None = None) -> None:
        """ご承認いただけなかったときの手順の終わり（ご納得＝ご承認の答えを「はい」にする＝この通話で決め直す1か所 _retake。by＝決め手の答えの質問
        （N2・N3）・次の返事はお礼の相づち）。"""
        p = self.point
        p["outcome"] = outcome
        if not any(e.get("question") == p["q"] for e in self.facts.get("not_agreed", [])):
            # ご納得いただけなかった点を伺わないまま終えた（N1 の答えがどちらとも取れず、ご納得を伺った N3 で決まった）：手順を終えたことを事実に残す（点は無し）。
            # 次の通話の前の通話の答えの "point"（earlier_point）が、これを読む（⛔ 残さないと、続きの電話の始めに、終えた手順をまた伺った）
            # 続きの電話なら、前の電話で伺った点。ご説明したなら、ご説明できたか
            self._point_record(point=p.get("resumed"), **({} if p["explained"] is None else {"explained": p["explained"]}))
        for e in self.facts.get("not_agreed", []):
            if e.get("question") == p["q"]:
                e["outcome"] = outcome
        if outcome == "declined" and self._approval_open(q := self.questions[p["q"]]) == "hesitating":
            # ご承認を迷っていたお客様が、迷っている点を伺ったあと・ご説明のあとにお断りの向きで答えた（断ろうとしている・ご納得いただけない）：ご承認はお客様ご自身の
            # 言葉で「いいえ」（決め直す1か所 _retake。最終目的なら _goals_lost で締め、その業務はかけない＝お断りのときと同じ）
            self._retake(q.id, q.conditions[1], by, "ご承認を迷っていたお客様が、迷っている点を伺ったあとにお断りの向きで答えた")
        if outcome == "agreed":
            q = self.questions[p["q"]]
            self._retake(q.id, q.conditions[0], by, "ご承認いただけなかった点をご説明して、ご納得いただいた")
            p["thank"] = self.callee_turns
            if by == CALLBACK_IDS["other_point"]:
                self.no_doubts = by   # 「ほかに…」に「分かった」＝ほかに分からない点は無い（ご納得を伺った N3 の「はい」は、ご説明した内容へのご納得だけ）

    def _offer_due(self) -> str | None:
        """ご承認の手順を点を言っていただけないまま終える（終えた）とき、伺う申し出の役目（OFFER_ROLES。無ければ None。唯一の決まり＝挟む一言を置かない
        _leave_open・伺い始める _handoff_step）：そのご承認が最終目的で、まだそのご承認で申し出を伺っていなくて、
        - 営業に引き継ぐ架電（業務設定の引き継ぎの方法 handoff_ways がある）⇒ 詳しい者から改めてお話ししてよいか（handoff。残りの質問があっても先に伺う）
        - そうでない架電で、業務設定に締める前の申し出（close_offer）があり、残りの質問が無い ⇒ 締める前の申し出（close_offer。残りの質問があれば、今までどおり
          一言を挟んで残りの質問へ進み、伺い終えて締める前に伺う＝この決まりが伺う番ごとに見る）
        ✅ 2026-11-09 日本時間4時36分49秒 ユーザー：「…営業に引き継ぐ架電なら、「それでは、詳しい者より、改めてお話しさせていただいてもよろしいでしょうか？」の方が
        よくないですか？」・6時33分57秒「・(ア) 営業に引き継がない架電で、ご承認が最終目的で、残りの質問が無いとき：ｂ」"""
        p, h = self.point, self.handoff
        if p is None or self.end_reason is not None or not self.questions[p["q"]].final_goal or (h is not None and h["q"] == p["q"]):
            return None
        if self.handoff_ways:
            return "handoff"
        return "close_offer" if self.close_offer is not None and not self._remaining() else None

    def _handoff_step(self) -> None:
        """営業に引き継ぐ架電で、ご承認が最終目的のご承認の手順を点を言っていただけないまま終えたときの手順（唯一の場所。_follow_ups から）：
        - 「承知しました。」＋「それでは詳しい者より改めてお話しさせていただいてもよろしいでしょうか？」（システムの質問 N4＝共通の一言の録音 callback_handoff）
        - はい ⇒ 改めてのお話の方法を選んでいただく（N5＝「それでは（方法）と（方法）のどちらがよろしいでしょうか？」。方法が1つならその方法）⇒ その方法の
          連絡先を、ほかのご希望と同じ1か所で伺い（_wish_contact・_contact_step。役目＝WISH_MEANS。オンラインはメールアドレス・LINE は伺い方 C11 から＝ID か
          SMS かメールからご招待。この通話か前の通話で取れていれば伺わない・お電話はいまの番号）、ほかのご希望と同じ申し伝える一言（WISH_TOLD。業務の方法の名前に
          当たる方法が無ければ「そのご希望を担当の者に申し伝えます。」）で次へ
        - はっきり「いいえ」 ⇒ お断りと同じ締め（ご承認の「いいえ」を断りとする＝_goals_lost。その業務はかけない）
        - はっきりしない（判定が決められないと読んだ） ⇒ その場で1回だけ伺い直す（N4 は Question.asks_once・言い直さない＝firm_not_needed）。それでも、
          わからない・答えたくない・聞き取れない ⇒ 断られていないので締めずに次へ
        伺った答えは通話の事実 handoff（依頼者へ返す）。✅ 2026-11-09 日本時間4時36分49秒 ユーザー：「それだと多分、もういいです。となると思うので、営業に
        引き継ぐ架電なら、「それでは、詳しい者より、改めてお話しさせていただいてもよろしいでしょうか？」の方がよくないですか？それがOKなら、連絡手段として、
        「訪問、オンライン説明、お電話、セミナー参加」などを選択していただく方向で誘導する。」"""
        h, p = self.handoff, self.point
        if h is None or h["q"] != (p["q"] if p is not None else None):
            role = self._offer_due() if p is not None and p["outcome"] == "unresolved" else None
            if role is not None:
                # 段（stage）：ask＝申し出（N4・N6）・way＝その方法（N5）・detail＝その方法の連絡先（伺い方 C11 も。ほかのご希望と同じ手順の段の名前＝_contact_step）。
                # kind＝申し出の役目（OFFER_ROLES＝通話の事実の名前。締める前の申し出の「いいえ」はお断りの締めにしない＝_goals_lost）
                self.handoff = {"q": p["q"], "kind": role, "stage": "ask", "way": None, "means": None, "detail": None, "outcome": None, "told": None}
                # terms＝会話が繋がらないお返事のあとの伺い直しに入れた、お客様の申し出の受け方の条件（_offer_again。依頼者へ渡す）
                # added＝伺い直しの提案に、こちらから足した条件（お客様の条件 terms とは別の欄）
                self._note(role, question=p["q"], agreed=None, way=None, means=None, via=None, contact=None, outcome=None, terms=None, added=None, topic=None)
                self._handoff_ask(role)
            return
        self._contact_reopen(h)
        if h["outcome"] is not None:
            return
        if h["stage"] == "detail":
            return self._contact_step(h)
        qid = CALLBACK_IDS[h["kind"]] if h["stage"] == "ask" else CALLBACK_IDS["handoff_way"]
        it = self.items[qid]
        if it.state not in TAKEN + ("unresolved",):
            return
        cond = it.condition if it.state in GOT else None
        if h["stage"] == "ask":
            if cond == "はい":
                self._handoff_fact(agreed=True)
                if it.offer_way is not None:
                    # 伺い直しに入れたご希望の方法を受けていただいた：方法は重ねて伺わずに、その方法の連絡先へ（✅ 2026-11-09 日本時間9時48分09秒の (3)）
                    self._handoff_way(it.offer_way)
                elif len(self.offer_ways) > 1:
                    h["stage"] = "way"
                    self._handoff_ask("handoff_way", after=qid)
                else:
                    self._handoff_way(self.offer_ways[0] if self.offer_ways else None)   # 方法が1つならその方法・無ければ伺わずに申し伝える
            else:
                self._handoff_end("declined" if self.questions[qid].declined(cond) else "unresolved")
        else:
            # 業務の方法か、業務の方法に無いご連絡の方法（other_means）：どちらも同じ1か所で、その方法の連絡先を伺って申し伝える（_handoff_way → _wish_contact）
            self._handoff_way(cond if cond in self.questions[qid].conditions else None)

    def _handoff_ask(self, role: str, after: str | None = None) -> None:
        """申し出の手順の質問（申し出 N4・N6・その方法 N5）を伺う唯一の場所（_handoff_step）：並びの先頭（after＝前の段の質問のすぐあと）。質問は業務設定で
        決まる申し出の質問の1か所（offer_questions）から。"""
        q = next(x for x in offer_questions(self.handoff_ways, self.close_offer) if x.callback == role)
        self._new_item(q)
        self.follow_ups[q.id] = self.handoff["q"]
        if after is None:
            self._put_first(q.id)
        else:
            self._insert_after(after, q.id)

    def _handoff_way(self, way: str | None) -> None:
        """改めてのお話の方法が決まった（way＝業務設定の名前のまま。言っていただけなければ None）：その方法の連絡先をその場で伺うか、申し伝えて終える（_handoff_step）。"""
        h = self.handoff
        ms = means_in(way) if way else []
        h["way"], h["means"] = way, (ms[0] if len(ms) == 1 else None)
        self._handoff_fact(way=way, means=h["means"])
        role = WISH_MEANS[h["means"]][2] if h["means"] else None
        if role is None:
            return self._handoff_end("agreed")   # お電話（いまかけている番号がある）・連絡先の無い方法（訪問など）・方法を言っていただけない
        # その方法の連絡先（オンライン＝ご案内の URL をお送りするメールアドレス・LINE＝伺い方 C11 から など）を、ほかのご希望と同じ1か所で伺う（伺い済みなら
        # 伺わない・LINE の伺い方・その連絡先が無いときのほかの方法＝_wish_contact・_line_how・_line_other_ways）。⛔ 2026-11-09 日本時間5時台に入れた形は、
        # 連絡先の段をほかのご希望とは別に書き、LINE は連絡先を伺わずに申し伝えた
        self._wish_contact(role, after=CALLBACK_IDS["handoff_way"] if CALLBACK_IDS["handoff_way"] in self.items else CALLBACK_IDS[h["kind"]], proc=h)

    def _handoff_fact(self, **extra) -> None:
        """申し出の通話の事実（handoff・close_offer＝申し出の役目。この通話のこのご承認の1つ）を書き足す唯一の場所。"""
        for e in self.facts.get(self.handoff["kind"], []):
            if e.get("question") == self.handoff["q"]:
                e.update(extra)

    def _handoff_end(self, outcome: str, contact: str | None = None) -> None:
        """詳しい者からの改めてのお話の手順の終わり（agreed＝方法・連絡先まで伺えた／declined＝はっきり断られた（_goals_lost が締める）／unresolved＝はっきりしない）。
        伺えたら、ほかのご希望と同じ申し伝える一言（WISH_TOLD。方法を言っていただけない＝ご案内の方法の件・業務の方法の名前に当たる方法が無い＝そのご希望）を次の返事で流す。"""
        h = self.handoff
        h["outcome"], h["contact"] = outcome, contact
        if outcome == "declined":
            self._handoff_fact(agreed=False)
        self._wish_facts_now()   # 結果・伺い方・連絡先を写す1か所（ほかのご希望と同じ）
        if outcome == "agreed" and not self._told_said(h):
            # 申し伝える一言は1回だけ（連絡先の段を開き直して＝_contact_reopen、もう一度終えたときは言わない）
            # 方法が無い申し出（業務設定に方法が無い）は「そのご希望を…」・方法を言っていただけないときは「ご案内の方法の件は…」
            h["told"] = h["means"] if h["means"] in WISH_TOLD else "other" if h["way"] or not self.offer_ways else "way"

    def _no_doubts_step(self) -> None:
        """ほかに分からない点が無いかを伺う業務の質問（Question.unclear_none）を、重ねて伺わない唯一の場所（_follow_ups から＝次の質問を決める前に毎回）：
        ご承認いただけなかったときの手順で「ほかにご納得いただけない点はございますでしょうか？」（N2）に「分かった」と答えていただいたら（no_doubts）、
        これから伺う質問（_remaining）のうち、その質問は伺わず、「無い」にあたる答えにする（この通話で答えを決め直す1か所＝_retake。決め手は N2 の答え＝
        遅れて届いた確かめで N2 の答えが食い違えば、この答えも一緒に渡さない）。ご納得を伺った N3 の「はい」（「なるほど」のあと）は、ご説明した内容への
        ご納得で、ほかに分からない点が無いとは言っていないので、伺う。✅ 2026-11-04 日本時間20時27分 ユーザー（お言葉は Question.unclear_none）"""
        by = self.no_doubts
        if by is None or self.items[by].state not in GOT:
            return
        for qid in self._remaining():
            q = self.questions[qid]
            if q.unclear_none and not q.callback:
                said = (self._evidence(by) or {}).get("said") or ""
                self._retake(qid, q.unclear_none, by, f"ご承認の手順で、ほかに分からない点は無いと伺った（「{said[:20]}」）＝重ねて伺わない")

    def _point_note(self) -> str:
        """ご説明する段の、AIへの知らせ（唯一の場所＝_advance の explain_point・名乗りの録音のあと _opening_reply）。続きの電話の始め（resumed）は、
        前の電話で伺った点を、点を伺い直さずに「前回お伺いした（点）の件ですが、」からご説明する（✅ 2026-11-04 日本時間19時26分 ユーザー「(A)…OK」の形。
        頭の言葉が点で変わるので、ご説明の文はAIの自分の言葉＝声に出す前に確かめる。想定問答・業務の情報の中身は、その文の中にそのまま使う）。"""
        p = self.point
        if self._resumed_explaining():
            said = p["resumed"]
            return (f"前回の電話で、お客様にご納得いただけなかった点を伺いました（「{said[:40]}」）。ご説明の途中で電話が切れたので、その点は伺い直さずに、"
                    f"いまからご説明します：「{RESUMED_LEAD}（その点を短く言った言葉）の件ですが、」と話し始めて、続けてその点のご説明を、1つの文にして"
                    f"replay_opening を「{PART_LABEL['own']}」で呼びます（{ARG_LABEL['answer']}に、話し始めの言葉から全部を書きます。想定問答の答え・業務の"
                    f"情報にある中身は、その文の中にそのまま使います。お客様の誤解なら、正しい中身をご説明します）。こちらが知っていることに無ければ、答えを作らずに、"
                    f"話し始めの言葉に続けて担当の者に申し伝えることを伝える文を書き、「{PART_LABEL['no_answer']}」で呼びます（{ARG_LABEL['fallback']}に、"
                    f"その点の種類＝{POINT_KIND_CHOICES}、を選びます）。")
        # 根拠の発話（ご説明の前に言い足された言葉は「／」でつないである＝_point_heard。どれも切らずに渡す＝言い足した点もご説明する）
        said = next((e.get("said") for e in reversed(self.facts.get("not_agreed", []))), None) or ""
        what = "ご承認を迷っている点" if p.get("hesitating") else "ご納得いただけなかった点"   # 迷っているお客様の手順（_point_begin の hesitating）
        # 2つ目からの点も、どの点かは言っていただいている（言っていない答えは、プログラムが1回伺う＝_point_step の which。会話AIに伺わせない＝お客様に伺う
        # ことはプログラムが流す。⛔ 2026-11-08 日本時間6時台（11-07 23時26分13秒のご質問の見直しの A-8）：会話AIに「どの点かを自分の言葉で短く伺ってから」と
        # 頼んでいた＝伺うと問いかけとして差し戻され、無言が続く）
        head = (f"お客様が{what}を伺いました（「{said}」）。" if p["round"] == 1 else
                f"お客様が、ほかにも分からない点・確かめたい点を言いました（「{said}」）。")
        return (f"{head}次へ進む前に、その点を、お客様が分かるまで親身にご説明します：想定問答に答えがあれば、その答えを流します。こちらが知っていること（業務の情報）に"
                f"あれば、その範囲で、自分の言葉で短くご説明します（お客様の誤解なら、正しい中身をご説明します）。知っていることに無ければ、答えを作らずに"
                f"「{PART_LABEL['no_answer']}」で応えます（{ARG_LABEL['fallback']}に、その点の種類＝{POINT_KIND_CHOICES}、を選びます）。どれも replay_opening で呼びます。")

    def _wish_step(self) -> None:
        """選ぶ質問（「どちら」）に「どれも要らない」と答えていただいたときの手順（唯一の場所。_follow_ups から）。その事を受けていただいたうえで伺って
        いる質問（ご連絡の方法を伺う質問なら、ご連絡そのものはよいと言っていただいている）なので、出した選択肢が合わないということ ⇒ 言い切っていても、
        いなくても、ほかのご希望を1回だけ伺う（W1：「はい、」＋ほかのご希望を伺う文＝_wish_text。答えは言い直して確かめない＝Question.firm_not_needed）：
        - 元の質問の選択肢を選び直していただいた（名前は _wish_pick_label・元の選択肢へ戻すのは wish_repick）⇒ その選択肢を元の質問の答えにする（業務設定の
          追って伺う質問・伝える一言は、その答えで決まる＝伝える一言の「言った」の印を外す。前の通話で取れた答えなら、この通話の答えになる）
        - ご連絡の方法を言っていただいた ⇒ その方法の連絡先をその場で伺い（LINE の ID・メールアドレス・ファクス番号・ご住所。この通話で伺い済みなら
          伺わない。お電話は、いまかけている番号がある）、「（方法）でのご連絡のご希望を担当の者に申し伝えます。」で次へ。W1 を伺っているときの、ほかの手段の
          申し出（request_callback）も、この答えとして受ける（_wish_reported）
        - ほかのご希望を言っていただいた ⇒「そのご希望を担当の者に申し伝えます。」で次へ（業務に無いことはお約束しない＝依頼者が決める）
        - 無い・聞き取れない・わからない ⇒ 元の答え（どれも要らない）のまま：最終目的なら、お礼を言って締める（_goals_lost）・そうでなければ、控えると
          受け取って次へ
        元の答えのあとに伝える業務の一言は、W1 を伺い終えてから、決まった答えの一言を1回（続けるときだけ＝_tell_due）。
        明らかなお断り・怒っている方は、答えではなく電話を終える言葉の道（「どれも要らない」にならない）。前の通話でほかのご希望まで伺った答え（前の通話の
        答えの "wish"＝earlier_wish）には伺わない・伺っていなければ（W1 の途中で切れた）、この通話の答えを受け取ったところで1回伺う（_wish_due）。
        最終目的の質問（はい／いいえも）への言い切っていないお断りで、業務の手立ての質問が無いとき（確かめずに受け取った＝_keep_toward_goal）も同じに伺う
        （言い切ったお断りは今までどおり答え＝最終目的なら締める）。
        伺った答えは通話の事実 other_wish（依頼者へ返す）。✅ 2026-11-03 日本時間19時54分・20時48分・21時38分 ユーザー（お言葉は CL-1.md 0-2）"""
        wid = CALLBACK_IDS["other_wish"]
        w = self.wish
        self._contact_reopen(w)
        if w is None or w["outcome"] is not None:
            if self.end_reason is not None:
                return
            q = next((q for q in self.questions.values() if self._wish_due(q)), None)
            if q is not None:
                self._wish_begin(q)
            return
        if w["stage"] == "detail":
            return self._contact_step(w)
        it = self.items[wid]
        if it.state not in TAKEN + ("unresolved",):
            return
        if it.state == "unresolved" and self._will_ask_again(wid):
            return   # 聞き取れない・判定が食い違った：締めの前の念のための確認でもう一度伺う（伺い終えるまで元の答えでは決めない＝_goals_lost の asking）
        q = self.questions[w["q"]]
        cond = it.condition if it.state in GOT else None
        if cond == ANY and self.questions[wid].lists_ways:
            # 並べて伺ったあとの「どれでも」：答え（どちらでも）＝業務設定で最初に並ぶ方法でご案内し、その方法の連絡先を伺う（こちらでお伝えする一言は次の返事の頭＝
            # _advance・連絡先の一言は頭の「それでは」を重ねない＝_wish_contact の announced）。✅ 2026-11-07 日本時間3時14分01秒 ユーザー「(2) H3…イ」
            cond = wish_option(q, means_in(way_offer(q)[0])[0])
            # こちらで方法をお伝えした一言を、申し伝える一言の1回として数える（told_said）：お客様はその方法を望んだとは言っていないので、「メールでのご連絡の
            # ご希望を担当の者に申し伝えます。」は重ねない（「そのご希望」を直したのと同じ理由＝お客様の言っていない希望を指さない）
            w["announce"], w["told_said"] = way_any_line(q), True
        elif w.get("which") and (cond == WAY_UNSAID or it.state in ("dont_know", "refused", "unresolved")):
            # 申し出を受けていただき、方法を並べて伺ったが、それでも方法を言っていただけない（方法は言っていない・わからない・答えたくない・聞き取れない）：
            # LINE の伺い方と同じく、そのご希望を担当の者に申し伝えて続ける（2回は伺わない。受けていただいた申し出をお断りにしない）。⛔ 2026-11-06 日本時間
            # 13時台の見直しで見つけた：方法は言っていない、のときだけ申し伝え、わからない・答えたくない・聞き取れないは「どれも要らない」と同じに受け、
            # 最終目的なら「承知しました。承知いたしました。お忙しいところ…」と締めて、その業務をかけない印を付けていた。申し伝える一言は「ご案内の方法の件は…」
            # （WISH_TOLD の way。✅ 2026-11-07 日本時間3時14分01秒 ユーザー「(2) H3…イ」。⛔ それまでは「そのご希望を…」＝お客様の言っていない希望を指した）
            cond = WISH_OTHER
            w["told_as"] = "way"
        elif cond == WAY_UNSAID:   # 初めての「方法は言っていない」（並べて伺ったあとは上の1か所）
            if not w.get("which"):
                # ご案内の方法の申し出を受けていただいたが、どの方法かは言っていない（「はい、お願いします。」）：申し出た方法を並べて1回だけ伺う（LINE の伺い方の
                # 「はい」だけ＝_line_how と同じ形）。✅ 2026-11-06 日本時間9時05分17秒 ユーザー：「(3) ご案内の方法の申し出に「はい、お願いします。」とだけ
                # 言われる：「それではメールとLINEとZOOMのどちらがよろしいでしょうか？」と伺うOK」。⛔ それまでは「恐れ入ります。ほかのご希望をもう一度…」と聞き直した
                w["which"] = True
                self._new_item(callback_question("other_wish", of=q, which=True))
                self._insert_after(wid, wid)
                return
        means = next((k for k in WISH_MEANS if wish_label(k) == cond), None)
        wish = self.facts.get("other_wish", [])
        self._note("other_wish", question=q.id, wish=cond, value=it.value if cond == WISH_OTHER or means else None, means=means,
                   contact=None, outcome=None, via=None, **self._proposal_terms(wid))
        if (picked := wish_repick(q, cond)) is not None:
            # 選択肢を選び直していただいた：元の質問の答えにする（この通話で決め直す1か所＝_retake。決め手は W1 の答え＝遅れて届いた確かめは W1 の
            # 答えに当て、食い違えば元の質問の答えも渡さない）
            self._retake(q.id, picked, wid, f"ほかのご希望を伺って、選択肢「{picked}」を選び直していただいた")
            return self._wish_end("option")
        if means is None:
            # 決められない・分からない・答えたくない（わからない・答えたくないの受け取り）は「無い」ではない＝unresolved（断られたとはしない＝_goals_lost）
            return self._wish_end("other" if cond == WISH_OTHER else "unresolved" if it.state in ("dont_know", "refused") else "none")
        w["means"] = means
        role = WISH_MEANS[means][2]
        if role is None:
            return self._wish_end("means", None)   # お電話：いまかけている番号がある
        # その方法の連絡先を、その場で伺う（「承知しました。」＋「それでは恐れ入りますがメールアドレスを…」＝_ack。LINE は伺い方を選んでいただく）
        self._wish_contact(role, after=wid)

    def _wish_contact(self, role: str, after: str, again: bool = False, proc: dict | None = None) -> None:
        """ご希望の方法の連絡先を伺う唯一の場所（ほかのご希望で言っていただいたご連絡の方法＝W1 の答えのあと _wish_step・詳しい者からの改めてのお話の方法＝
        _handoff_way・LINE の伺い方のあと＝_line_how・続きの電話の始め＝_wish_contact_again）：この通話か前の通話でもう伺った連絡先なら伺わない（_known_contact）。
        伺うなら、システムの質問 role を前の質問 after のすぐあとに（again＝続きの電話の始め＝並びの先頭に、続きの電話の一言で）。proc＝その手順の段の進み
        （ほかのご希望＝self.wish・詳しい者からのお話＝self.handoff。段・伺い方・連絡先の受け方は同じ＝_contact_step）。"""
        w = self.wish if proc is None else proc
        known = self._known_contact(role)
        if known is not None and self._ahead_now() is None:
            return self._wish_known(known, w)
        # もう伺った連絡先があっても、いま一緒に言われたこと（先に言われた答え＝_offer_ahead）があれば、まずその中の連絡先を伺わずに報告していただく
        # （20時27分 (3)「前の答えの中にその連絡先があれば、伺わずに…言い直して確かめます」。⛔ 21時台の見直しで見つけた：もう伺った連絡先を確かめずに
        # 渡し、いま言われた別の連絡先を捨てていた）。言われたことに無ければ、もう伺った連絡先を使う（_resume の _wish_known）
        did = CALLBACK_IDS[ROLE_KEY[role]]
        w.update(stage="detail", detail=did, known=known)
        # 伺い方（C11）を選んでいただいたあとの一言は、伺い方の一言の「恐れ入りますが」を重ねない（callback_question の chosen＝phrases.CALLBACK_ASK_CHOSEN）
        self._new_item(callback_question(role, again=again, chosen=after == CALLBACK_IDS["line_how"], means=w.get("means"),
                                         announced=bool(w.get("announce"))))
        if again:
            self._put_first(did)
        else:
            self._insert_after(after, did)

    def _wish_known(self, known: "tuple[str, str | None]", proc: dict | None = None) -> None:
        """もう伺った連絡先（_known_contact の答え）で、連絡先の段を終える唯一の場所（伺わない＝_wish_contact・いま言われたことに無かった＝_resume）。"""
        w = self.wish if proc is None else proc
        contact, via = known
        if via is not None:
            self._wish_via(via, w)
        self._contact_end(w, contact)

    def _known_contact(self, role: str) -> "tuple[str, str | None] | None":
        """連絡先を伺う質問（役目 role）の答えが、この通話か前の通話でもう取れているか（_wish_contact。決まりは known_contact の1か所＝続きの電話の始めの
        resumed_step と同じ）：質問の答え（業務の質問の答えも、前の通話で取れた答えも＝items）と、ほかのご希望で伺った連絡先（この通話の通話の事実 other_wish・
        前の通話の答え）・詳しい者からのお話で伺った連絡先（この通話の通話の事実 handoff。同じ形 {"means", "via", "contact"}）。"""
        answers = [(q, it.state, self.answer_of(qid)) for qid, q in self.questions.items() if (it := self.items.get(qid)) is not None]
        return known_contact(role, answers, earlier_records(self.earlier_all) + [e for k in ("other_wish", *OFFER_ROLES) for e in self.facts.get(k, [])])

    def _line_how(self, it: "Item", proc: dict) -> None:
        """LINE でのご連絡の連絡先の伺い方（C11）の答えを受けた（唯一の場所＝_wish_step）：ID・SMS・メールなら、その連絡先を伺う（_wish_contact。伺い済みなら
        伺わない）。方法を言っていない（「はい」だけ）なら、1回だけどれかを伺い直す（「それではLINEのIDとSMSとメールのどちらがよろしいでしょうか？」＝
        callback_question の nth）。それでも決まらない・聞き取れない・わからない・答えたくないなら、連絡先は無いまま申し伝えて続ける（連絡先が取れなかった
        ときと同じ）。proc＝その手順の段の進み（ほかのご希望・詳しい者からのお話＝_contact_step）。✅ 2026-11-04 日本時間19時26分 ユーザー（お言葉は
        phrases.CALLBACK_ASK の line_how）"""
        w, hid = proc, CALLBACK_IDS["line_how"]
        if self._offer_taken(hid):
            return self._way_switch(w, it.offer_way)   # 繋がらないお返事のあとの、LINE でない方法の提案を受けていただいた
        cond = it.condition if it.state in GOT else None
        if cond in LINE_HOW:
            via, role = LINE_HOW[cond]
            self._wish_via(via, w)
            return self._wish_contact(role, after=hid, proc=w)
        if it.offer_again:
            return self._way_unsaid(w)   # その提案を受けていただけなかった（いいえ・また繋がらない・聞き取れない・方法を言っていない）
        if cond == WAY_UNSAID and not w.get("how_again"):
            w["how_again"] = True
            # ほかの方法を伺ったあと（_line_other_ways）なら、取れなかった方法を外した言い方で（「それではLINEのIDとメールのどちらが…」）
            self._new_item(callback_question("line_how", nth=1, without=w.get("without")))
            self._insert_after(hid, hid)
            return
        self._contact_end(w, None)

    def _line_other_ways(self, it: "Item", proc: dict) -> bool:
        """LINE の連絡先の伺い方（C11）で選んでいただいた方法の連絡先が無い（その連絡先の質問 it に、携帯電話が無い・ID が分からない・お伝えいただけない＝
        わからない・答えたくない／理由を添えて伺い直しても形が合わない＝固定電話の番号しか無い＝Item.form_failed）ときに、ほかの方法を1回だけ伺う唯一の場所
        （_wish_step の detail）。聞き取れないだけのときは、今までどおり締めの前の念のための確認でもう1回伺う（10-14 の必須の質問の決まり。連絡先が無いとは
        言われていない）。取れなかった方法を外した伺い方（「それではLINEの
        IDから登録かメールからご招待する方法でもよろしいでしょうか？」＝callback_question の without・言い方は phrases.CALLBACK_ASK_OTHER_WAYS）を、伺い方の質問として伺い直す。そのあとは伺い方の答えの受け方の
        1か所（_line_how）のまま。伺ったら True。LINE でない・伺い方を選んでいただいていない・もう伺ったなら False（今までどおり連絡先は無いまま申し伝えて続ける）。
        取れなかった連絡先の質問は並びから外す（締めの前の念のための確認で「お声が聞き取れませんでしたので携帯電話の番号を…」と伺い直さない＝番号は聞き取れて
        いる・ほかの方法を伺った）。伺い方は渡さない（その方法ではご招待できない＝_wish_via）。✅ 2026-11-05 日本時間0時48分 ユーザー「(ア)(イ)ともお勧めの形でOK」
        （お勧めの形：「携帯電話が無いと言われたら、ほかの2つの方法を伺います：「それではLINEのIDかメールからご招待する方法でもよろしいでしょうか？」」）。
        同じ理由（その方法の連絡先が無い＝ほかの方法ならご招待できる）は、ID・メールを選んでいただいたときにも当たる（10-19 の決まり）。✅ 1時29分06秒
        ユーザー：「…「それではLINEのIDから登録かメールからご招待する方法でもよろしいでしょうか？」にそろえるに決まっているだろ。招待はメールとSMSだけだよ。」
        proc＝その手順の段の進み（ほかのご希望・詳しい者からのお話＝_contact_step）。"""
        w, hid = proc, CALLBACK_IDS["line_how"]
        failed = next((via for via, role in LINE_HOW.values() if via == w.get("via") and CALLBACK_IDS[ROLE_KEY[role]] == w["detail"]), None)
        if w.get("means") != "line" or failed is None or not (it.state in ("dont_know", "refused") or it.form_failed):
            return False
        # 取れなかった連絡先の後始末は、ほかの方法を伺う前でも、伺ったあと（2回目）でも同じ：締めの前に伺い直さない・伺い方は渡さない（⛔ 2026-11-05 日本時間
        # 2時05分37秒のご質問のあとの読み直しで見つけた：ほかの方法を伺う所の中にしか無く、2回目は締めの前に「お声が聞き取れませんでしたので携帯電話の番号を…」と
        # 伺い、依頼者へ via「sms」・「id」が渡った）
        self._out_of_order(w["detail"])
        self._wish_via(None, w)
        if w.get("without"):
            return False   # ほかの方法は、もう伺った（1回だけ）：連絡先は無いまま申し伝えて続ける
        w.update(without=failed, detail=hid)
        w.pop("how_again", None)   # 方法を言わずに「はい」とだけ答えていただいたときの伺い直しは、この一言にも1回（_line_how）
        self._new_item(callback_question("line_how", without=failed))
        self._insert_after(hid, hid)
        return True

    def _wish_facts_now(self) -> None:
        """ほかのご希望の通話の事実 other_wish と、詳しい者からの改めてのお話の通話の事実 handoff（同じ連絡先の手順＝_contact_step）を、答えから書き直す唯一の
        場所（_wish_via・_wish_end・_handoff_end）：伺った連絡先は、いまの
        答え（締めの前の念のための確認で取れた・言い直された連絡先も残る。同じ方法の連絡先は1通話1つ＝CALLBACK_IDS。LINE は、選んでいただいた伺い方の
        連絡先＝LINE_HOW）。渡さなくした答え（unresolved）から書いたもの（W1＝方法と結果・伺い方＝via・連絡先）は、渡さない（9-5 の (7)「依頼者へ渡るのは
        確かめた値か「-」だけ」。⛔ 21時台の見直しで見つけた：伺い方の答えが遅れて届いた確かめで「-」になっても、via は残った）。"""
        hid = CALLBACK_IDS["line_how"]
        for w, key in ((self.wish, "other_wish"), (self.handoff, (self.handoff or {}).get("kind", "handoff"))):
            wish = self.facts.get(key, [])
            if wish and w is not None and wish[-1].get("question") == w["q"]:
                # いま伺っている手順の段の進み（結果＝_wish_end・_handoff_end・伺い方＝_wish_via・もう伺った連絡先＝_wish_known）を写す。⛔ 2026-11-05 日本時間
                # 2時05分37秒のご質問のあとの読み直しで見つけた：段の進みを2か所（_wish_via・_wish_end）が直接書き、渡さなくした W1 の上に積んだ連絡先が依頼者へ
                # 渡った。W1 が聞き取れないだけ（「-」のまま元の答え＝outcome none）でも、記録を「W1 の途中で切れた」（outcome None）に戻し、次の電話で W1 を伺い直した
                wish[-1].update(outcome=w["outcome"], via=w.get("via"), contact=w.get("contact"))
        for e in [e for k in ("other_wish", *OFFER_ROLES) for e in self.facts.get(k, [])]:
            if e.get("means") == "line" and hid in self.items and self.items[hid].state == "unresolved":
                e["via"] = None
            role = WISH_MEANS[e["means"]][2] if e.get("means") else None
            if role == "line_how":
                role = next((r for v, r in LINE_HOW.values() if v == e.get("via")), None)
            if role and (did := CALLBACK_IDS[ROLE_KEY[role]]) in self.items:
                if self.items[did].state in GOT:
                    e["contact"] = self.answer_of(did)
                elif self.items[did].state == "unresolved":
                    e["contact"] = None

    def _wish_via(self, via: str | None, proc: dict | None = None) -> None:
        """LINE の連絡先の伺い方（LINE_HOW の via）を、いまの手順の段の進みに持つ（通話の事実は _wish_facts_now の1か所が写す。依頼者へ返す：ID を伺って
        登録するか、SMS・メールのどちらでご招待するか＝ご招待はメールとSMSだけ・✅ 11-05 1時29分06秒。None＝選んでいただいた方法の連絡先が無い＝_line_other_ways）。"""
        (self.wish if proc is None else proc)["via"] = via
        self._wish_facts_now()

    def _wish_means(self, q: "Question", means: str, role: str | None = None, again: bool = False, told_said: str | None = None) -> None:
        """言っていただいたご連絡の方法（means）の連絡先を伺うところから、ほかのご希望の段を始める唯一の場所（続きの電話の始め _wish_contact_again・業務の
        申し出の質問を方法つきで受けていただいたとき _offer_wish）：通話の事実 other_wish を書き、連絡先を伺う（伺う連絡先の無い方法なら申し伝えて終える）。"""
        self.wished.add(q.id)
        # told_said＝その申し出の条件つきの受けの言い切りでお伝えした方法（_offer_wish。申し伝える一言の1回として数える＝_told_said）
        self.wish = {"q": q.id, "stage": "detail", "detail": None, "means": means, "outcome": None, "told": None, "told_said": told_said}
        self._note("other_wish", question=q.id, wish=wish_label(means), value=None, means=means, contact=None, outcome=None, via=None,
                   **dict.fromkeys(PROPOSAL_TERMS))
        self.items[q.id].told = False
        role = role or WISH_MEANS[means][2]
        if role is None:
            return self._wish_end("means", None)
        self._wish_contact(role, after=q.id, again=again)

    def _wish_contact_again(self, q: "Question", role: str) -> None:
        """前の通話でご連絡の方法までは伺えて、その連絡先を伺う前に切れた電話の続き（resumed_step の連絡先の役目）：お詫び（＋用件の一文）のすぐあとに、
        切れた所から連絡先を伺う（「前回選択いただいたLINEでのご連絡の件で…」＝phrases.CALLBACK_ASK_RESUMED）。この通話の事実 other_wish に、その方法の
        ご希望として残し（結果はこの通話で決まる）、そのあとはこの通話で伺ったときと同じ（_wish_step の detail）。その答えのあとに伝える業務の一言は、
        前の通話では伺い終えるまで待っていて言っていないので、伺い終えてから1回（_wish_begin の again と同じ）。✅ 2026-11-04 日本時間19時26分 ユーザー"""
        self._wish_means(q, earlier_means_of(self.earlier[q.id]), role, again=True)

    def _wish_begin(self, q: "Question", again: bool = False) -> None:
        """質問 q の答えに、ほかのご希望を伺い始める（W1 を並びに入れる唯一の場所＝この通話の答えのあと _wish_step・続きの通話の始め Flow.__init__）。
        again＝ほかのご希望を伺っている途中で切れた電話の続き（前の通話の答えに "wish" が無い＝_unfinished）：お詫びのすぐあとに、切れた所から
        「前回お伺いした（聞くこと）の件で…」と伺う（✅ 2026-11-03 日本時間23時52分36秒 ユーザー「(ア)」）＝並びの先頭に入れる（目的のお客様の
        いる業務は、繋がってから同じ順＝_connect の _advance が並びの先頭から）。その答えのあとに伝える業務の一言は、前の通話では W1 を伺い終えるまで
        待っていて言っていないので、この通話で W1 を伺い終えてから1回言う（_tell_due）。"""
        wid = CALLBACK_IDS["other_wish"]
        self.wished.add(q.id)
        self.wish = {"q": q.id, "stage": "ask", "detail": None, "means": None, "outcome": None, "told": None}
        self._new_item(callback_question("other_wish", of=q, again=again))   # 前に伺った答えの記録は前の回へ（_new_item）
        if again:
            self.items[q.id].told = False
            self._put_first(wid)
        else:
            self._insert_after(q.id, wid)

    def _wish_due(self, q: "Question") -> bool:
        """この質問の答えに、いまほかのご希望を伺うか（唯一の決まり＝_wish_step が伺い始める・伝える一言を W1 のあとまで待つ _tell_due）：選ぶ質問の
        「どれも要らない」（言い切っていても、いなくても）・最終目的の質問への言い切っていないお断り（確かめずに受け取った＝_keep_toward_goal。前の通話で
        取れた答えは、前の通話の答えの "hedged"）。この通話でもう伺った質問・前の通話でほかのご希望まで伺った答え（"wish"＝earlier_wish）・ご承認の質問
        （ご納得いただけなかった点を伺う＝_point_step）・業務設定にその答えのあとに伺う質問があるとき（業務の言葉が先＝✅ 2026-11-03 日本時間8時10分の
        ご判断の形。_business_next の1か所）は伺わない。伺い終えるまで締めないのは _goals_lost（いま伺っている self.wish・結果 _wish_outcome）。"""
        # 前の通話で W1 の答えをいただく前に切れた答え（前の通話の答えに "wish" が無い）は、ここでは伺わない：続きの通話の途中で、ほかの質問の答えの
        # すぐあとに「それでは（選択肢）のほかにご希望は…」と伺うと、話がつながらない（CL-1 が 11-03 の /clear のあとの見直しで見つけた）。続きの通話の
        # 始め（お詫びのすぐあと）に、切れた所から伺う（Flow.__init__ の _wish_begin の again。✅ 2026-11-03 日本時間23時52分36秒 ユーザー「(ア)」）。
        # 伺い終えるまで、その答えでは締めない（_goals_lost の _unfinished）
        if q.id in self.earlier or q.id in self.wished or self._wish_outcome(q.id) is not None:
            return False
        return self._wish_trigger(q)

    def _wish_trigger(self, q: "Question") -> bool:
        """この質問の答えが、ほかのご希望を伺う答えか（伺ったか・いつ伺うかは見ない。唯一の決まり＝_wish_due・_unfinished）：選ぶ質問の「どれも要らない」・
        最終目的の質問への言い切っていないお断り（決まりは wish_trigger の1か所。続きの電話の始めの resumed_step も同じ決まり）。"""
        it = self.items[q.id]
        return wish_trigger(q, it.state, it.condition, bool(it.hedged), self.questions.values())

    def _wish_outcome(self, qid: str) -> str | None:
        """質問 qid に伺ったほかのご希望の結果（WISH_OUTCOMES の1つ。伺っていなければ None）：この通話で伺った最後のもの（通話の事実 other_wish）、無ければ
        前の通話の答えの "wish"（どちらも earlier_wish の1か所の読み方）。ご連絡の方法・ほかのご希望なら、その質問は断られていない（_goals_lost）。"""
        f = self._wish_fact(qid)
        return earlier_wish(f) if f is not None else None

    def _wish_fact(self, qid: str) -> dict | None:
        """質問 qid に伺ったほかのご希望の最後の事実（この通話の通話の事実 other_wish の最後のもの、無ければ前の通話の答えの wish_fact。無ければ None）の唯一の
        取り出し方（結果 _wish_outcome・伺い終えたか _unfinished）。"""
        e = next((e for e in reversed(self.facts.get("other_wish", [])) if e["question"] == qid), None)
        return e if e is not None else earlier_fact(self.earlier.get(qid), "wish_fact")

    def _unfinished(self, q: "Question") -> bool:
        """業務の質問 q の答えのあとに伺う手順（ほかのご希望 W1・その連絡先と伺い方 C11・C12・ご納得いただけなかった点を伺う手順）を、まだ伺い終えていないか
        （唯一の決まり＝報告の unfinished・伺い終えるまで断られたとしない _goals_lost）：
        - ほかのご希望を伺う答え（_wish_trigger）で、W1 の結果が無い・ご連絡の方法までで連絡先を伺う前（earlier_wish_means）
        - ご承認の質問のお断りで、手順の終わり（ご納得・お断り・点を言っていただけないまま終えた）に届いていない（この通話の手順の進み、無ければ前の通話の答えの
          point_fact＝点を伺う前・点は伺えたがご説明の途中）
        前の通話から続く手順を、この通話でも伺い終えなかったときも同じ（前の通話の事実で決まる）。CL-2 の判定は、この質問の答えを「まだ受け取っていない答え」と
        読む（必須なら行を続け、次の電話で切れた所から伺う＝CL-2 の連絡帳 4-213）。"""
        it = self.items[q.id]
        if self._wish_trigger(q):
            f = self._wish_fact(q.id)
            if f is None or earlier_wish(f) is None or earlier_wish_means(f) is not None:
                return True
        if q.approval and it.state in GOT and q.declined(it.condition):
            p = self.point
            if p is not None and p["q"] == q.id:
                return p["outcome"] is None
            return earlier_point_open(earlier_point_of(self.earlier.get(q.id)))   # 続きの電話の始めの段（resumed_step）と同じ1か所
        return False

    def _wish_end(self, outcome: str, contact: str | None = None) -> None:
        """ほかのご希望を伺う段の終わり（_wish_step）：通話の事実に結果と連絡先を書き、ご連絡の方法・ほかのご希望なら申し伝える一言を次の返事の頭に
        （_advance）。"""
        w = self.wish
        w["outcome"], w["contact"] = outcome, contact
        self._wish_facts_now()
        if outcome in ("means", "other") and not self._told_said(w):
            # 申し伝える一言は1回だけ（段を開き直して＝_contact_reopen、もう一度終えたときは言わない）
            w["told"] = w["means"] if outcome == "means" else w.get("told_as") or "other"

    def _contact_step(self, proc: dict) -> None:
        """ご希望の方法の連絡先の段（stage＝detail）の答えを受ける唯一の場所（ほかのご希望＝_wish_step・詳しい者からの改めてのお話＝_handoff_step。proc＝その
        手順の段の進み）：LINE の伺い方（C11）なら、その伺い方の連絡先を伺う（_line_how）・選んでいただいた方法の連絡先が無ければ、ほかの方法を1回だけ伺う
        （_line_other_ways）・それ以外は、取れた連絡先（取れなければ無いまま）で段を終える（_contact_end）。✅ 2026-11-04 日本時間19時26分 ユーザー（LINE の
        伺い方）・⛔✅ 2026-11-09 日本時間4時36分49秒の (e)（詳しい者からのお話の方法の連絡先も、同じ伺い方）"""
        it = self.items[proc["detail"]]
        if it.state not in TAKEN + ("unresolved",):
            return
        if proc["detail"] == CALLBACK_IDS["line_how"]:
            return self._line_how(it, proc)
        if self._line_other_ways(it, proc):
            return
        self._contact_end(proc, self.answer_of(proc["detail"]) if it.state in GOT else None)

    def _contact_end(self, proc: dict, contact: str | None) -> None:
        """ご希望の方法の連絡先の段を終える唯一の場所（取れた連絡先・無ければ None）：終えたときの段の答えを覚え（あとで変われば開き直す＝_contact_reopen）、
        その手順の終わりへ（ほかのご希望＝_wish_end の means・詳しい者からのお話＝_handoff_end の agreed）。"""
        if proc["stage"] == "detail" and (it := self.items.get(proc["detail"])) is not None:
            proc["ended"] = (it.state, it.form_failed)
        if proc is self.handoff:
            self._handoff_end("agreed", contact)
        else:
            self._wish_end("means", contact)

    def _contact_reopen(self, proc: dict | None) -> None:
        """連絡先の段を終えたあと（締めの前の念のための確認で）、その段の答えが変わった（伺い方が取れた・連絡先が無いと分かった・連絡先が取れた）：段を開き直して、
        ふつうの決まりで受け直す唯一の場所（_contact_step＝伺い方の受け方 _line_how・ほかの方法を伺う _line_other_ways・連絡先。ほかのご希望＝_wish_step・
        詳しい者からのお話＝_handoff_step）。申し伝える一言は1回だけ（told_said）。⛔ 11-05 2時05分37秒のご質問のあと流れの制御に通して見つけた：締めの前の
        確認の「携帯は持っていないんです」に、ほかの方法を伺わずに締めていた ⇒ ⛔ その直しは「連絡先が無い」ときだけ開き直していて、確認で取れた伺い方
        （「メールでお願いします。」）は捨てて締めた（別の担当の読み直しで見つけた。伺い方も連絡先も依頼者へ渡らなかった）"""
        if (proc is not None and proc["outcome"] is not None and proc["stage"] == "detail" and (dit := self.items.get(proc["detail"])) is not None
                and dit.state in TAKEN + ("unresolved",) and proc.get("ended") != (dit.state, dit.form_failed)):
            proc["outcome"] = None

    def _start_over(self) -> None:
        """続きの電話で、判断いただける代わりの方に繋がった（_connect の1か所）：前の通話の答えは、お呼びした方ご本人の答え（代わりの方の答えではない＝本書 9-4 の
        「代わりの方の申し出」の「依頼者へ渡す答えは代わりの方の答えと印を付ける」）なので、前の通話の答えで決めていたこと（伺わずに済ませる質問・続きの電話の
        始めの段＝申し伝え直し・ほかのご希望・連絡先・ご承認の手順）を外し、営業の最初の質問から伺う（お詫び・申し伝える一言は流さない＝_connect）。
        ✅ 2026-11-07 日本時間3時14分01秒 ユーザー「(3) H6：…電話が途中で切れることは代わりの方には関係ない話ですよね。しっかりと営業の最初からもしくは引き継がれて
        いる部分から確認していかないといけないんじゃないですか。」。⛔ それまでは、申し伝え直す電話（ご承認が最終目的）で「お忙しいところ…」と締め、前の答えの
        ある続きの電話では残りの質問だけを伺った。外すのは前の通話の答え（伺わずに済ませる質問と、その連絡先）だけ＝続きの電話の始めの段は、ご本人か後任の
        方に繋がった時にだけ作る（_resume_cut。代わりの方には作らない＝前の通話の答えの無い電話と同じ状態になる）"""
        for qid in self.earlier:
            self._new_item(self.questions[qid])
        self.earlier, self.earlier_all = {}, {}
        self.order = list(self.business_ids)

    def _resume_cut(self) -> None:
        """続きの電話の始めの段（前の通話で伺っている途中で切れた手順＝resumed_step：ほかのご希望・ご連絡の方法の連絡先・ご承認の手順の続き・申し伝え直し）を
        作る唯一の場所（1通話1回）。前の通話の答えは、お呼びした方ご本人（目的のお客様のいない業務は電話に出た方）の答えなので、その方と話し始める時に作る＝
        目的のお客様のいない業務は電話の始め（Flow.__init__）・いる業務はご本人か後任の方に繋がった時（_connect）。判断いただける代わりの方には作らない
        （_start_over）。⛔ 2026-11-08 日本時間0時台（11-07 23時26分13秒のご質問の見直しの B-7 と、その確かめで見つけた2つ）：目的のお客様のいる業務でも電話の
        始めに作っていたので、(1) 代わりの方に営業の最初から伺うとき、作った印（follow_ups・wished・point_marks）が残り、ご承認のお断りで例外で止まった・
        「どれも要らない」に W1 を伺わなかった (2) 前の電話がご説明の途中で切れていると、代わりの方の「判断できる」を受け取らず、何も流さなかった (3) 受付の方の
        「不在」からかけ直しの質問を伺ったあとにご本人が戻られると、並びを業務の質問に戻す所（_target_back）が始めの段の質問を消し、ほかのご希望の続きは伺わず、
        ご承認の手順の続きはお詫びのあとすぐ「お忙しいところお時間をいただきましてありがとうございました。」で締めた"""
        if not self.resume_due:
            return
        self.resume_due = False
        business = [q for q in self.questions.values() if not q.callback]   # 業務の質問（トークスクリプトの順＝名乗りの録音・AIへの指示文と同じ材料）
        if (step := resumed_step(business, self.earlier)) is None:
            return
        role, q = step
        if role == "other_wish":
            self._wish_begin(q, again=True)
        elif role in CONTACT_ROLES:
            self._wish_contact_again(q, role)
        elif role == "told":
            self._point_told(q)
        else:
            self._point_begin(q, again=True, said=earlier_said_of(self.earlier[q.id]).strip() if role == "explain" else None)
        if self.target is None:
            # 名乗りの録音の最後に伺う質問（opening_question の1か所。名前の読みの確かめの途中なら、そのあと）。ご説明から始めるなら、いまの質問は無い
            # （目的のお客様のいる業務は、繋がったときの _advance が並びの先頭から）
            first = opening_question(business, self.earlier)
            if role == "explain" and first is None:
                self.current = None
            elif getattr(first, "callback", None) == role:
                self.current = CALLBACK_IDS[ROLE_KEY[role]]

    def _drop_unasked(self, qid: str) -> None:
        """まだ伺っていないシステムの質問を、伺わずに外す（並び・質問・答えから。伺い済みの連絡先だった＝_resume）。"""
        assert self.items[qid].state == "empty" and self.questions[qid].callback, qid
        self._out_of_order(qid)
        self.questions.pop(qid), self.items.pop(qid), self.taken.pop(qid, None)

    def _out_of_order(self, qid: str, keep_current: bool = False) -> None:
        """質問を並びから外す唯一の書き方（伺わない・締めの前に伺い直さない＝_drop_unasked・_line_other_ways）。keep_current＝いま伺っている
        質問なら、お客様の答えは受ける（いまの質問のまま）。"""
        self.order = [x for x in self.order if x != qid]
        if self.current == qid and not keep_current:
            self.current = None

    def _put_first(self, qid: str) -> None:
        """質問を並びの先頭に入れる（書き方は first_in の1か所）。"""
        self.order = first_in(self.order, qid)

    def _insert_after(self, after: str, qid: str) -> None:
        """質問を、前の質問（after）のすぐあとの並びに入れる（_follow_ups）。前の質問が並びに無ければ、終わりに。同じ質問を伺い直すとき
        （after＝その質問）は、同じ位置に（並びに2つ入れない）。書き方は inserted の1か所。"""
        self.order = inserted(self.order, after, qid)

    @property
    def in_answer_stage(self) -> bool:
        """答えの段か（お呼びする段＝calling でない）＝かけ直しの質問の一言を答えの段の一言にするかの唯一の決まり（callback_question の answering）。"""
        return self.phase != "calling"

    def _add_callback_question(self, role: str, text: str | None = None) -> None:
        """システムの質問を、いま聞いている並びの終わりに足す（_follow_ups・_not_transferred）。"""
        q = callback_question(role, text, answering=self.in_answer_stage)
        self._new_item(q)
        self.order = [*self.order, q.id]
        if self.callback_why:
            self.callback_ids = [*self.callback_ids, q.id]

    def _goals_lost(self) -> list[str] | None:
        """最終目的の申し出が断られて、業務の目的が果たせなくなったか（唯一の決まり＝_advance）：断られた最終目的の質問（受け取った答えが、その質問の
        断る答え）の番号の並び。まだ果たせる・最終目的の無い業務なら None。any＝全部断られたら・all＝1つでも断られたら、果たせない。
        断る答えでない答え（わからない・答えたくない・聞き取れない）は、断られたとはしない（まだ果たせる）。"""
        goals = [q for q in self.questions.values() if q.final_goal]
        # 断る答え：その質問の断る答え（取込で決める）と、選ぶ質問の「どれも要らない」。ご承認の質問は、ご納得いただけなかった点を伺い終えてから
        # 答えのあとに伺う手順（ほかのご希望 W1・その連絡先・ご納得いただけなかった点を伺う手順）を伺い終えていない（_unfinished の1か所＝報告の unfinished と
        # 同じ決まり）：伺い終えるまで締めない。前の通話でご説明の途中・W1 の途中で切れた答えも（お客様はまだ聞き終えて・言い終えていない。⛔ 2026-11-04
        # 日本時間18時台に見つけた：続きの電話でほかの質問を伺ったあと、目的が果たせないとして締めていた）
        asking = {q.id for q in goals if self._unfinished(q)}
        p, h = self.point, self.handoff
        if p is not None and p["outcome"] == "unresolved" and not (h is not None and h["q"] == p["q"] and h["kind"] == "handoff" and h["outcome"] == "declined"):
            # 点を言っていただけないまま終えた・ご説明できなかった：断られたとはしない（締めない＝残りの質問へ・営業に引き継ぐ架電は詳しい者からのお話を伺う＝
            # _handoff_step）。詳しい者からのお話もはっきり断られたら、お断りと同じに締める（その業務はかけない）。締める前の申し出（資料のお送りなど）を
            # 断られても、ご承認を断られたとはしない（お礼の締め＝✅ 2026-11-09 日本時間6時33分57秒「(ア)…ｂ」の帳面の伺い「断られたら…お礼の締め」）
            asking.add(p["q"])
        # 言い切っていないお断りのあとに、業務設定の追って伺う質問（ask_if_hedged）をまだ伺い終えていない：伺う前に締めない（⛔ 2026-11-03 の見直しで
        # 見つけた：最終目的の質問への「来月以降になると思います」に追って伺う質問があっても、伺う前に「目的が果たせない」で締めていた）。当たるかは _business_next
        asking |= {q.id for q in goals if any(x.ask_if_hedged and x.id in self.order and self.items[x.id].state == "empty" for x in self._business_next(q.id))}
        # ほかのご希望を伺い終えた質問のうち、ご連絡の方法・ほかのご希望を言っていただいた質問は、断られていない（その方法・ご希望を担当の者に申し伝えて進む。
        # 前の通話で言っていただいたものも＝_wish_outcome）。無いと言われたら、元の答えのまま決める（✅ 2026-11-03 日本時間19時54分・20時48分 ユーザー：
        # 最終目的なら、お礼を言って締める）。伺っている途中は上の _unfinished
        # W1 を答えをいただけないまま終えた（unresolved）も断られていない（✅ 11-01「わからない・答えたくないは断られたとはしない」）
        asking |= {q.id for q in goals if self._wish_outcome(q.id) in ("means", "other", "unresolved")}
        # 言い切っていないお断りを確かめずに受け取った最終目的の質問（業務の手立ての質問が無い＝_keep_toward_goal）は、その答えだけでは締めず、ほかのご希望を
        # 伺った答えで決める（上の asking。✅ 2026-11-03 日本時間21時38分 ユーザー：「…(1) ほかのご希望を1回だけ伺う。ということですよね？それならOKですよ」。
        # 前は確かめてから締めた＝「念のためどちらもご不要ということでよろしかったでしょうか？」＝遠ざかる向きの言葉の確かめ・そのあと 19時49分に伺うまで
        # 「承知しました。」で次へ進めていた）。業務の手立ての質問があれば、その答えで決まる（上の asking・最終目的の設定＝test_final_goal_setting_…）
        refused = [q.id for q in goals if self.items[q.id].state in GOT and q.declined(self.items[q.id].condition) and q.id not in asking]
        if not refused or (self.goal_mode == "any" and len(refused) < len(goals)):
            return None
        return refused

    def _remaining(self) -> list[str]:
        """これから伺う質問（並びの順）：まだ伺っていない質問のうち、先へ進むのを止める答えのあとの業務の質問（held の1か所）を除いたもの。"""
        hold = set(held(self.questions.values(), self.order, self._read))
        return [qid for qid in self.order if self.items[qid].state == "empty" and qid not in hold]

    @property
    def ended_on(self) -> dict | None:
        """ここで電話を終える答え（Question.ends_on）で、残りの業務の質問を伺わなかった（会話の報告の事実 ended_on。無ければ None）を、通話の終わりの答えで
        決める唯一の場所（Flow.report が報告に入れる。答えはあとから変わりうる＝ほかのご希望で選び直した・遅れて届いた確かめ・念のための確認で取れた、ので
        通話の途中では書かない）：question＝止めた質問・answer＝その答え（日本語の名前）・skipped＝伺わなかった業務の質問（トークスクリプトの順。その
        質問の答えで伺うはずだった質問も）。CL-2 の判定は skipped を「伺っていない（対象外）」と数える。答えが決まらないまま伺わなかった質問（聞き取れない
        など）は入れない（取れていない質問のまま＝分からないまま「すべて取れた」にしない）。根拠の発話は、止めた答えを受け取った発話（前の通話の答えなら
        turn は None）。"""
        order = follow_ups_in(self.business_ids, self.questions.values(), self._read)
        g = gate_of(self.questions.values(), order, self._read)
        if g is None or not g[1]:
            return None
        skipped = set(held(self.questions.values(), order, self._read))
        for q in self.questions.values():   # その答えで伺うはずだった質問（伺う条件の前の質問を伺わなかった）も、トークスクリプトの順で続けて
            if q.ask_if and q.id not in order and q.ask_if[0] in skipped:
                skipped.add(q.id)
        qid, t = g[0], self._evidence(g[0]) or {}
        return {"turn": t.get("turn"), "said": (t.get("said") or "").strip()[:80], "question": qid, "answer": label(self.items[qid].condition),
                "skipped": [x for x in self.questions if x in skipped and self.items[x].state == "empty"]}

    def _question_to_us(self) -> str | None:
        """いまのお客様の言葉の中の、答えに添えてこちらへ尋ねた問いの文（無ければ None。唯一の場所＝答えに添えた質問に先に答えるか _question_first）。判定の
        読みが届く通話は、項目「こちらへ尋ねた問いがあるか」の読み（queries_us の1か所）で決め、渡す文は asked_text。判定を使わない通話（会話AIの通話の試験）
        だけ、言葉の形（question_in の語尾の見方）で見る：答えのほかに問いの文があるときだけ（言葉全体が1つの問いの文なら、それは答えとして報告された頼みの形の
        答えそのもの＝「16日に変更できますか。」。判定の説明の「頼み・申し出は、問いの形で言っても尋ねたとしない」と同じ。⛔ 2026-11-08 日本時間11時台：言い直しの
        返事にも当てたら、言葉の形だけで見る通話で、値の直しの頼みに先に答えさせた）。✅ 2026-11-08 日本時間8時55分08秒 ユーザー「(イ) 判定に「こちらへ尋ねた問いがあるか」を1項目足し、1か所で読む」
        （⛔ それまでは判定の読みがあっても語尾の形で決めていた＝ご納得の手順への「あー、そういうことですか。」を質問として会話AIに答えさせ、「。」で
        終わった問い「…決まってるのかなと思って。」を聞き流した）。
        ⛔ G-47：相手が「はい、予約しています。当日はどなたが担当されるんですか？」と答えたのに、プログラムはすぐ次の質問の録音を流し、
           AIには「何も言わずに待って」と伝えた＝相手の質問が10本中10本、聞き流された。"""
        readings = [self.check.get(k) for k in JUDGMENTS]
        if self.judging or any(r is not None for r in readings):
            return asked_text(self.callee_said) if queries_us(readings) else None
        if self.callee_turns == self.words_taken:
            # 言葉の形の見方だけ：言葉そのものを、いまの道が受けた（かけ直しの申し出「あとでかけてもらえますか？」など＝_advance の handled）なら問いとしない
            # （判定を使う通話は、頼み・申し出かを判定の項目が読み分ける＝道で決めない。⛔ 2026-11-08 日本時間11時台：道の印を判定を使う通話にも当てたら、
            # 急いでいる方への伺いへの「はい、来週なら大丈夫です。ちなみに御社ってどちらにあるんですか？」を、かけ直しの道が印を立てて聞き流した）
            return None
        asked = question_in(self.callee_said)
        return asked if asked and asked.strip() != (self.callee_said or "").strip() else None

    def _advance(self, prefix: str, handled: bool = False) -> dict:
        """次へ進む返事（_advance_to）の前に、受け取った答えのあとに伝える一言を流す（唯一の場所）：業務設定の一言（Question.tell_words・まだ言って
        いないもの）と、ほかのご希望を申し伝える一言（phrases.WISH_TOLD＝_wish_step。頭の相づちは「承知しました。」＝19時56分にお見せした案のとおり。
        連絡先は言い直して確かめるときに「はい、ありがとうございます。（連絡先）でよろしいでしょうか？」とお礼を言うので、お礼は重ねない）。両方あれば、
        申し伝える一言（相づち「承知しました。」に続く）が先。業務の一言の録音は依頼主の単位（make_ack_clips.script_table の名前＝業務の質問・ほかのご希望を
        伺う一言と同じ種類 question）。"""
        said = self._taken_on_terms()
        self._proposal_taken()
        self._offer_wish()
        r = self._advance_to(prefix, handled)
        # 条件つきの受けの言い切り（会話AIの言葉）は、ほかの決まった言葉より先に（返事の録音も会話AIが言う形にするのは _led の1か所）
        plays = [{"kind": "said", "text": said}] if said is not None else []
        # ご承認いただけなかったときの手順を、点を言っていただけないまま終えた（_point_step の leave_open＝(頭の相づち, 一言の録音の名前)）：次へ進む前
        # （締めるときも）に、伺い直さずに挟む一言（phrases.POINT_ANYTIME・POINT_ANYTIME_PLAIN。文は共通の一言の表 RECORDED の1か所から）。一言はいちばん先に
        # 流すので、相づちはこの一言の頭（p["open"] の頭＝_ack_choice の point_open。決めるのは leave_open を置く _point_step の1か所）。
        # 1回だけ。✅ 2026-11-05 日本時間5時20分56秒・5時45分 ユーザー
        opened = (p := self.point) is not None and p.get("open")
        if opened:
            plays.append({"kind": "recorded", "id": opened[1], "text": RECORDED[opened[1]]})
            p["open"] = None
            self._point_marks()["anytime_said"] = True   # 言った印（ご承認の質問ごとに通話で1つ＝取り消しで段を戻しても・手順をやめても残す）
        if (w := self.wish) is not None and w.get("announce"):
            # 並べて伺ったあとの「どれでも」に、こちらで方法をお伝えする一言（way_any_line の1か所）。連絡先の一言より先（1回だけ）
            aid, atext = w.pop("announce")
            plays.append(line_play(aid, atext))
        wish = False
        for proc in (w, self.handoff):
            # ほかのご希望・詳しい者からの改めてのお話の方法を伺い終えた：申し伝える一言（WISH_TOLD＝共通の一言の録音 wish_told_（鍵）。頭の相づちは「承知しました。」）。
            # 1回だけ（told_said＝連絡先の段を開き直して、もう一度終えたときは言わない）
            if proc is not None and proc.get("told"):
                plays.append({"kind": "recorded", "id": f"wish_told_{proc['told']}", "text": WISH_TOLD[proc["told"]]})
                proc["told"], proc["told_said"], wish = None, True, True
        told = anytime = False
        for qid, it in self.items.items():
            if (line := self._tell_due(qid, ending=r.get("result") == "end")) is not None:
                told = True
                anytime = anytime or line[1] == "unclear_anytime"
                # 業務の一言は依頼主の単位の録音（question）、どの業務でも同じ一言（unclear_anytime）は共通の一言の録音（recorded）＝line_play
                plays.append(line_play(line[1], line[0]))
        if not plays:
            return r
        if r.get("result") == "recheck":
            # 締めの前の念のための確認の頭のお詫び（_ack の moushiwake_heavy）は、相づちの位置を一言に譲るので、一言のあとに流す（「承知しました。そのご希望を
            # 担当の者に申し伝えます。」＋「申し訳ございません、」＋「お声が聞き取れませんでしたので…」。⛔ 11-03 の /clear のあとの見直しで見つけた：
            # 相づちの直しで、確かめの前のお詫びが抜けていた＝「…申し伝えます。お声が聞き取れませんでしたので…」）
            plays.append({"kind": "recorded", "id": "moushiwake_heavy", "text": ACK["moushiwake_heavy"]})
        out = self._led(plays, "", r) | ({"point_open": opened[0]} if opened else {"wish_told": True} if wish
                                         else {"anytime_told": True} if anytime
                                         else {"told": True} if told else {})
        if (m := self._asked_next) is not None and m[0] is r:
            self._asked_next = (out, m[1])   # 外でさらに言葉を前に足されても、質問のつなぎの言葉を同じ1か所（_relead_next）で決める
        return out

    def _tell_due(self, qid: str, ending: bool) -> str | None:
        """いま流す、その答えのあとに伝える業務の一言（Question.tell_words。無ければ None）・保留の一言・ほかに分からない点は今のところ無いと言われたときの
        一言（unclear_anytime）を決める唯一の場所（流すものは「言った」の印を付ける＝1回だけ）：
        受け取った答えの一言で、まだ言っていないもの。ほかのご希望を伺う答え（_wish_due・いま伺っている W1）は、W1 を伺い終えるまで流さない（答えがまだ
        決まっていない）。伺い終えたら、決まった答えの一言を1回（選び直した答えの一言＝_wish_step が印を外す・ご連絡の方法やほかのご希望で続けるなら元の答えの
        一言）。W1 のあとに締める（ending＝無いと言われて目的が果たせない）なら流さない（CL-1 の 11-03 のご判断の (6)。⛔ 見直しで見つけた：「はい、お気軽に
        ご相談ください。それではほかにご希望などは…」と W1 の前に流し、選び直した答えの一言は「言った」の印のまま流さなかった）。"""
        q, it = self.questions[qid], self.items[qid]
        # 決められないのがお客様の最終の決定（わからない）のときの保留は、ここでは言い渡さない＝受け取る所で伺ってお返事を待つ（_pending_due・_ask_pending。
        # ✅ 2026-11-09 日本時間3時46分53秒 ユーザー「…「それではこの件は保留ということでよろしいでしょうか？」のように聞いてあげた方がいいんじゃないですか？」。
        # ⛔ それまでは、ここで「それでは（聞くこと）は保留とさせていただきますね。」を言い渡して次の質問へ進んでいた）
        line = q.tell_words.get(it.condition) if q.type in CHOICE_TYPES and it.state in TAKEN and not it.told else None
        if line is None and takes_for_now(q, it.condition) and it.state in TAKEN and it.hedged and not it.told:
            # ほかに分からない点が無いかを伺う質問への、言い切っていない「無い」（「今のところ質問はありません。」）を確かめ直さずに受け取ったら（_keep_toward_goal）、
            # 「何かわからないことがございましたらいつでもご遠慮なくおっしゃってくださいね。」を添えてから次の質問へ（共通の一言の録音 unclear_anytime。
            # ✅ 2026-11-07 日本時間13時05分06秒 ユーザー：「「が」があろうがなかろうが、この言葉を添える方がいいと思います。その後に続きまして と入るのが
            # スムーズでしょう。」）。その答えの業務の一言があれば、そちら（依頼者がその答えに言うと決めた言葉＝同じ意味の言葉を重ねない）
            it.told = True
            return RECORDED["unclear_anytime"], "unclear_anytime"
        if line is None:
            return None
        w = self.wish
        if (w is not None and w["q"] == qid and w["outcome"] is None) or self._wish_due(q):
            return None
        it.told = True
        return None if qid in self.wished and ending else (line, tell_recording_id(q, it.condition))

    def _next_question(self, q: "Question", prefix: str) -> dict:
        """次の質問を伺う返事（唯一の場所＝_advance_to）。作った返事と、その中の質問の録音を覚えておく（_asked_next）：前に決まった言葉を足すとき、
        質問の頭のつなぎの言葉をその言葉と重ねないように替える（_relead_next）。"""
        if self.play_recordings:
            # かけ直しで足す質問も録音（⛔ 2026-10-19 G-57：AIに言わせると、3本中1本で「私の方では折り返しのお電話ができかねますので」と
            # 足し、もう1本でも「かしこまりました。」を足した＝決まった文は録音にする決まり（6-13）に戻した）
            play = question_play(list(self.questions.values()), q.id)
            text = play["text"]
            r = {"result": "next", "question_id": q.id, "question": q.text, "play": [play],
                 "message": prefix + played_note(text, f"次の質問 {q.id}") + "何も言わずに、相手の答えを待ってください。" + answer_wait()
                            + choices_note(q)}
        else:
            # つなぎの言葉は、録音の形と同じ1か所（spoken_question：入る一言・前の答えで伺う質問は「それでは」・それ以外は「続きまして」＝NEXT_LEAD）。⛔ 2026-11-04 まで、
            # AIが話す形だけ、つなぎの言葉を流した回数で回していて、前の答えで伺う質問にも「続きまして」、伺わなかった質問を挟むと順番の言葉がずれた
            play, lead = None, spoken_question(list(self.questions.values()), q.id)[1]
            r = {"result": "next", "question_id": q.id, "question": q.text,
                 "message": f"{prefix}次の質問は {q.id}「{q.text}」です。"
                            + (start_with(lead) if lead and (q.intro or self.lead_ins) else "")
                            + choices_note(q)}
        self.items[q.id].played = True
        self._asked_next = (r, play)
        return r

    def _purpose_play(self, who: str | None = None) -> dict:
        """名乗り（お詫び）のあとに流す用件の一文の録音（_opening_reply・_connect）：続きの電話は前の電話とつなげた一文（決まりは purpose_part の1か所。業務の録音に
        無ければ＝用件の名詞が決まらなかった業務は、今の一文）。代わりの方は前の電話でお話ししていない（今の一文）。"""
        parts = self.target or self.opening or {}
        key = purpose_part(self.earlier, self.apology_reasons, self.apology_when, who) if who != "substitute" else "purpose"
        key = key if parts.get(key) else "purpose"
        return {"kind": "opening", "part": key, "text": parts[key]}

    def _resumed_call(self) -> bool:
        """続きの電話か（決まりは resumed_call の1か所。名乗りの返事 _opening_reply・繋がったとき _connect が読む）。"""
        return resumed_call(self.earlier, self.apology_call, self.number_again)

    def _relead_next(self, r: dict, before: list[dict], resumed: bool = False) -> dict:
        """次の質問を伺う返事 r（_next_question が作ったそのもの。中の録音の前に、名乗り・用件などを足されていてもよい）の質問の頭のつなぎの言葉を、
        その前に同じ回に流す言葉（before＝前に足す決まった言葉と、r の中で質問より前に並んだ録音）の文の頭と重ねない形に替える（唯一の場所＝前に言葉を
        足す _led・名乗りを足す _connect。決めるのは spoken_question の relead）。替えるのは質問の1つと、AIへの知らせの中のその文だけ（重ならなければ何も
        変えない）。✅ 2026-11-06 日本時間11時55分13秒 ユーザー「(1) ウ」（「承知しました。それではこの件は保留とさせていただきますね。続きまして…」）。
        ⛔ 入れる途中、全部の試験で見つけた：はじめは返事をまるごと作り直していた＝繋ぐ所（_connect）が前に足した名乗り・用件の録音が消えた"""
        m = self._asked_next
        if m is None or m[0] is not r or r.get("question_id") not in self.questions:
            return r
        qs = list(self.questions.values())
        qid = r["question_id"]
        plays = r.get("play") or []
        if m[1] is not None:   # 録音で流す通話：質問の録音を替える
            at = next((i for i, p in enumerate(plays) if p is m[1]), None)
            if at is None:
                return r
            new = question_play(qs, qid, "".join(p["text"] for p in [*before, *plays[:at]]), resumed=resumed)
            if new != plays[at]:
                r["message"] = r["message"].replace(plays[at]["text"], new["text"], 1)
                plays[at] = new
                self._asked_next = (r, new)
            return r
        old = spoken_question(qs, qid)[1]   # AIが話す通話：AIに渡す話し始めの言葉を替える
        new = spoken_question(qs, qid, "".join(p["text"] for p in before), resumed=resumed)[1]
        if old and new != old:
            r["message"] = r["message"].replace(start_with(old), start_with(new), 1)
        return r

    def _advance_to(self, prefix: str, handled: bool = False) -> dict:
        self.digressed = False  # 次へ進む（もう伺ったことへは戻らない）
        if handled:
            # handled＝相手の言葉そのものを、いまの操作が受けた（かけ直しの申し出「あとでかけてもらえますか？」・目的の人の名乗り・ご遠慮の一言などの
            # きっかけの言葉）＝答えに添えた質問ではない。この印を読むのは、判定を使わない通話の言葉の形の見方だけ（_question_to_us。判定を使う通話は、
            # 頼み・申し出かを判定の項目が読み分ける）。答えに添えた質問に先に答えるかは、返事のあとの1か所（_question_first）が決める
            self.words_taken = self.callee_turns
        if self.substitute_q and self.items[self.substitute_q].state in TAKEN + ("unresolved",):
            return self._substitute_decided(prefix)  # 代わりの方に、判断いただけるお立場かを聞いた答えが決まった（唯一の入口）
        if self.refusal_q and self.items[self.refusal_q].state in TAKEN + ("unresolved",):
            return self._refusal_decided(prefix)     # ご本人に、今後のお電話をご遠慮したほうがよいかを伺った答えが決まった（唯一の入口）
        if self.concern_q and self.items[self.concern_q].state in TAKEN + ("unresolved",):
            return self._concern_decided(prefix)     # 本題に入ったあとの「もういいです。」に本心を伺った答えが決まった（唯一の入口）
        if self.hurry_q and self.items[self.hurry_q].state in TAKEN + ("unresolved",):
            return self._hurry_decided(prefix)       # 急いでいる方に、また改めてお掛けしてよいかを伺った答えが決まった（唯一の入口）
        if self.wait_q and self.items[self.wait_q].state in TAKEN + ("unresolved",):
            return self._wait_or_later_decided(prefix)   # 待つか、かけ直すかを伺った答えが決まった（唯一の入口）
        # 答えによって足す質問を先に決める（ご承認いただけなかったら、締める前にご納得いただけなかった点を伺う＝_goals_lost はその答えを待つ）
        self._follow_ups()
        if self._explain_due():
            # ご納得いただけなかった点を伺った（続きの電話の始めは、前の電話で伺った）：次へ進む前に、AIが知っていることでご説明する（相手の質問・お話に答える
            # 1か所＝replay_opening）
            return {"result": "explain_point", "message": f"{prefix}{self._point_note()}"}
        if self.end_reason is None and (lost := self._goals_lost()) is not None:
            # 最終目的の申し出を断られ、業務の目的が果たせなくなった：残りの質問は伺わずに、お礼の締め（その業務だけかけない＝CL-2 が通話の事実から決める）
            # 根拠の発話は、目的が果たせなくなった断りの答えの発話（受け取ったときの材料＝_evidence の1か所。⛔ 2026-11-05 日本時間2時05分37秒のご質問のあとの
            # 読み直しで見つけた：締めたときの発話を入れていた＝ほかの質問への「いいえ、ないです。」が根拠になった）
            ev = max((e for e in (self._evidence(x) for x in lost) if e is not None and e.get("turn") is not None), key=lambda e: e["turn"], default=None)
            self._note("goal_refused", goals=lost, mode=self.goal_mode,
                       **({"turn": ev["turn"], "said": (ev.get("said") or "").strip()[:80]} if ev is not None else {}))
            return self._close("goal_refused", prefix=f"{prefix}業務の目的が果たせなくなったので、質問はここまでです。")
        # ⛔ 2026-11-09 日本時間4時36分49秒まで、最終目的のご承認で点を言っていただけない・ご説明できなかったときは、お礼を言って締めていた（ご判断に無い道を
        # CL-1 が足していた）。✅ 3時06分51秒 ユーザー：「なぜ断ってるわけじゃないのに、そこで失礼しましたと切るんですか？超一流の営業マンが、断られてもいないのに、
        # 電話を切るなんてありえますか？」⇒ 営業に引き継ぐ架電は詳しい者から改めてお話ししてよいかを伺い（_handoff_step）、ほかは一言を挟んで残りの質問へ
        rest = self._remaining()
        # 念のための確認の段でも、あとから足した聞き足し（分かるお客様・分かる時）を先に伺う（その段で並びに残るのは聞き足しだけ。⛔ 2026-10-24 の
        # 見直し（調べ役）：念のための確認への「担当ではないので分からない」で分かる方を足しても、伺わずに締めていた）
        if self.phase in ("asking", "rechecking") and rest:
            self.current = rest[0]
            q = self.questions[self.current]
            if self.phase == "rechecking" and not q.callback:
                # 念のための確認で取れた答えで、まだ伺っていない業務の質問を伺えるようになった（先へ進むのを止めていた答え＝gate_of・その答えで伺う質問
                # ＝ask_if）：初めて伺う質問なので答えの段に戻す（その場の聞き返し・言い直しの決まりが、ほかの質問と同じに効く。締めの前の念のための確認は
                # 伺い終えてから選び直す＝もう確かめた質問は回数を使い切っている）。⛔ 2026-11-04 に見つけた：念のための確認の段のまま伺い、聞き取れない答えを
                # 聞き返さずに「-」にし、選択肢の答えまで言い直していた（「ご担当の方はいらっしゃいますか」の答えが念のための確認で取れた場面）
                self.phase = "asking"
            if q.reading_of:
                return self._enter_reading(q.id, prefix)
            if (r := self._offer_ahead(q, prefix)) is not None:
                return r
            return self._next_question(q, prefix)
        if self.phase == "asking":
            # 締めの前に、「-」の必須項目を1回だけ念のため確かめる（6-11）。急いでいる相手・かけ直しを頼んだ相手にはしない
            self.recheck_queue = [qid for qid in self.order if self._will_ask_again(qid)]
            self.phase = "rechecking"
        if self.phase == "rechecking" and self.recheck_queue and not self.hurried:
            self.current = self.recheck_queue.pop(0)
            self.items[self.current].rechecks += 1
            q = self.questions[self.current]
            # 締めの前の念のための確認＝その項目を聞く最後の1回（✅ 2026-10-14 ユーザー：最初の質問のほかに最大3回＝その場2回＋この1回）。
            # 言い方は聞き返しと同じ表（phrases.ask_again）の、最後の1回の言い方。相づちは「申し訳ございません、」（_ack）
            say = ask_again_phrase(self.items[self.current].ask_backs + 1, "unclear", q.topic, last=True)
            return {"result": "recheck", "question_id": q.id, "question": q.text, "say": say,
                    "message": f"{prefix}念のための確認です。{q.id} を、もう1回だけ伺います。{say_note(say)}相手の答えを待ってください。" + answer_wait()}
        reason = self.close_after or (("other_means" if self.callback_why == "other_means" else "callback") if self.callback_why else "done")
        return self._close(reason, prefix=f"{prefix}質問は終わりです。")

    def _will_ask_again(self, qid: str) -> bool:
        """「-」にした項目を、締めの前の念のための確認でまた伺うか（唯一の決まり＝その並びを作る所 _advance_to・伺い終えるまで決めない所 _wish_step）：必須の
        項目で、その確認の回数が残り、急いでいる方・かけ直しを頼まれた方ではない（答えの段なら並びにある・確認の段なら確認の並びにある）。"""
        q, it = self.questions.get(qid), self.items.get(qid)
        if q is None or it.state != "unresolved" or not q.required or self.callback_why:
            return False
        if it.offer_again:
            # 会話が繋がらないお返事のあとの提案で、もう伺い直した項目（申し出・方法を選んでいただく質問＝_offer_again）：伺い直しは1回（聞き直しの回数を使い
            # 切った扱い＝7時57分50秒のご判断を入れた帳面の伺い「また繋がらない・はっきりしない ⇒ 伺い直さず今の道」）。⛔ 2026-11-12 日本時間14時58分17秒の
            # 「アで！」を入れる途中、お返事の種類ごとの表で見つけた：必須の N5・C11・W1 は、提案へのはっきりしないお返事のあと「申し伝えます。」と言ったすぐあとに、
            # 締めの前の念のための確認で「お声が聞き取れませんでしたので…もう一度だけお聞かせ…」と伺っていた（聞き取れていたのに電話のせいにして、同じことを2度伺う）
            return False
        if self.phase == "asking":
            return qid in self.order and it.rechecks < self.limits.recheck
        return self.phase == "rechecking" and qid in self.recheck_queue

    @property
    def asking_reception(self) -> bool:
        """目的の人のある業務で、目的の人に繋がる前の電話口の方（受付の方など）に、システムの質問を伺っている（かけ直しの申し出・判断いただける
        お立場か・約束の電話で取り次げないと言われたときの一言・急いでいる方への伺い）＝電話口の方は、まだ呼んでいる段の方（唯一の決まり）。取り次がれて別の方が出たら、
        お呼びした方が戻ったのかを確かめる（_target_back）。⛔ 2026-10-24 の見直し（調べ役）：約束の電話の一言を伺っている間が入っていなかった＝
        受付の方が「それでしたら少々お待ちください」と取り次いでも、代わって出た岡田様に約束の一言を流し、繋がずに終えた。
        お呼びした方ご本人（後任の方も）と繋いだあと（通話の事実 connect の who）は、いつも偽：電話口の方は、もうお呼びした方。代わりの方と
        繋いだあとは、今までどおり（代わりの方と話している途中に、ご本人が電話口に出ることがある＝お呼びした方かを確かめる）。⛔ 2026-11-02
        （CL-2 の連絡帳 4-182 の (4)）まで、ご本人と繋いだあとの「あとでかけて」（かけ直しの質問）・急いでいる方への伺いでも真になり、ご本人の
        「少々お待ちください」で取り次ぎの印が付いて、そのあと出た方をお呼びした方かを確かめる道（呼んでいる段へ戻る）に入りえた。"""
        reached = any(c.get("who") in ("target", "successor") for c in self.facts.get("connect", ()))
        return not reached and bool(self.callback_why or self.substitute_q or self.hurry_q or self.close_after == "not_transferred")

    @property
    def hurried(self) -> bool:
        """相手が急いでいる・通話を断られた・怒っている、で締めたか（念のための確認をしない・締めは断られたときの言い方）。"""
        return self.end_reason in DECLINED_ENDS

    def _asked(self, qid: str) -> bool:
        """その質問を、もう伺ったか（答えが届いた・いま伺っている＝返事で流した。相手がその途中で切っても伺った）。"""
        return qid in self.items and (self.items[qid].state != "empty" or self.current == qid)

    def _yes_no(self, qid: str) -> bool | None:
        """はい／いいえの問いの答えを、記録に書く真偽にする唯一の場所（書かないなら None）：「はい」＝true・「いいえ」＝false。それ以外
        （わからない・答えたくない・聞き取れない・伺う途中で切れた）は、NOT_YES_IS_NO の問いなら false（よいと言われていない）、ほかは書かない。"""
        it = self.items.get(qid)
        if it is None or not self._asked(qid):
            return None
        got = it.condition if it.state in GOT else None
        if got in ("はい", "いいえ"):
            return got == "はい"
        return False if qid in NOT_YES_IS_NO else None

    def _callback_fields(self, qids, why: str) -> dict:
        """かけ直しの記録（通話の事実 callback）の答えの項目を作る唯一の場所（CL-2 の call_report.Callback の形）：「時刻」「番号」はいつも
        持つ（ほかの手段のときは持たない＝CL-2 との約束）。取れた答えだけ値を入れ、はい／いいえは _yes_no の1か所。時刻の答えには、その値と
        一緒に受け取った理由を添える：月単位の理由（cycle）と、「あとで」の「〇時まで」の理由（until_why。値に「まで」があるときだけ＝
        直されて「〇時以降」になったら書かない）。"""
        fields: dict = {} if why == "other_means" else {"time": None, "number": None}
        for qid in qids:
            if (field := CALLBACK_FIELD.get(qid)) is None or qid not in self.items:
                continue
            it = self.items[qid]
            if field in YES_NO_FACT:
                if (v := self._yes_no(qid)) is not None:
                    fields[field] = v
                continue
            fields[field] = self.answer_of(qid) if it.state in GOT else None
            if field == "time" and it.state in GOT:
                if it.cycle:
                    fields["cycle"] = it.cycle   # 月単位の理由でその日・頃を選んだ（CL-2 の 4-106）
                if why == "later" and it.until_why and _UNTIL.search(fields["time"] or ""):
                    fields["until_why"] = it.until_why   # 「〇時まで」の理由（書くのは「あとで」のときだけ＝CL-2 の 4-149 の (1)(2)）
        if fields.get("back_today") is False and "again" not in fields:
            fields["again"] = False   # 本日はお戻りにならない＝その日は改めてお掛けしない
        return fields

    def _note(self, kind: str, **extra) -> None:
        """通話の事実を残す唯一の場所（self.facts）。根拠の発話＝相手の発話の番号（on_callee で数えた番号）と、その文字（会話AIの聞き取り）。
        Intent.md 9-5「各項目には、根拠になった発話（時刻つき）を保存する」・11-1「拒否・同意の記録（根拠の発話）」。時刻は電話接続側が
        発話の番号に付ける（試作では通話の記録の turns）。"""
        assert kind in FACT_KINDS and kind != "ended_on", kind   # ended_on は通話の終わりの答えで決める（Flow.ended_on・Flow.report）
        assert kind != "callback" or extra.get("why") in CALLBACK_REASONS, extra.get("why")   # 一覧に無い理由は、CL-2 の読み手が報告ごと断る
        self.facts.setdefault(kind, []).append({"turn": self.callee_turns, "said": (self.callee_said or "").strip()[:80], **extra})

    def _note_irreversible(self, kind: str, check: str) -> None:
        """取り消せない決定（発信の拒否 do_not_call・怒っていた complaint・番号違い）を残す唯一の場所：事実と、会話AIの読みと2つ目の判定
        （意味の判定）がそろったか（agreed／disagreed／none＝判定が無い）。✅ 2026-10-30 管理者のご決定 (6)：「お客様の断り・番号違い・
        怒っていた（会話AIの報告で発信禁止や行の終わりになるもの）は、2つ目の判定とそろったときだけその場で決め、食い違えば、通話後の録音の
        確かめで決まるまで、そのお客様にかけずに保留する」（Intent.md 18-3）。⛔ 11-02 まで、そろったかを報告に入れておらず（CL-2 の連絡帳
        4-182 の (1)）、どの通話も「判定が無い」になって、そろった断りも毎回録音の確かめまで保留になっていた。"""
        if kind != "party":
            self._note(kind)
        self.irr_checks.append(check)

    def _end_check(self, reason: str, judged: bool) -> str:
        """電話を終える言葉（断り・怒り）で残す事実の、判定とそろったか（_note_irreversible へ渡す）：判定で決めた（judged）か、いまの発話の判定の
        読みが同じ理由なら agreed・別の読みなら disagreed・判定を使わない通話か読みが届いていなければ none。"""
        if judged:
            return "agreed"
        end = self._end_now() if self.judging else None
        return "agreed" if END_REASON.get(end) == reason else "none" if end is None else "disagreed"

    def _answer_check(self, qid: str, cond: str) -> str:
        """答えで残す取り消せない事実（ご遠慮の確かめへの「はい」・また改めて伺ってよいかへの「いいえ」）の、判定とそろったか：受け取った答えの
        判定の読み（taken の check）のどれかが同じなら agreed・読みが届いていて違えば disagreed・届いていなければ none。"""
        reads = [v for k, v in self._material(qid).get("check", {}).items() if k in JUDGMENTS and v is not None]
        return "agreed" if cond in reads else "disagreed" if reads else "none"

    @property
    def irreversible_check(self) -> str:
        """通話の報告の irreversible_check（CL-2 の call_report.IRREVERSIBLE_CHECKS）：取り消せない事実のどれかが食い違えば disagreed、
        判定の無いものがあれば none、ほかは agreed（取り消せない事実が無ければ agreed＝CL-2 は見ない）。"""
        c = self.irr_checks
        return "disagreed" if "disagreed" in c else "none" if "none" in c else "agreed"

    def report(self) -> dict:
        """会話の報告（CL-2 の call_report.from_json が読む外枠）を作る唯一の場所：版と、通話の事実・繋がらなかった理由・切れ方・お詫びを
        伝えられたか・取り消せない事実と判定のそろい・止まったAI。鍵と版は CL-2 の読み手と同じ（試験で比べる。CL-2 の連絡帳 4-179 の (e)：
        ⛔ 2026-11-02 まで外枠が無く、報告の項目を試作の道具の結果の中に散らして返していた）。答えの項目は報告ではなく、項目の書き込み
        （CL-2 の conversation_io.write_item。名前は日本語の名前＝label）で渡す。"""
        # ここで終える答えで伺わなかった質問（ended_on）は、通話の終わりの答えで決まるので、ここで足す（通話の途中の記録 self.facts には書かない）
        facts = self.facts if (e := self.ended_on) is None else {**self.facts, "ended_on": [e]}
        # 手順を伺い終えていない業務の質問（_unfinished の1か所。CL-2 の判定は、その答えを「まだ受け取っていない答え」と読む＝必須なら行を続ける。CL-2 の連絡帳
        # 4-213。⛔ それまで、W1・連絡先・ご承認の手順の途中で切れても、ほかの必須の質問が取れていれば判定が「完了」にし、続きの電話が1本もかからなかった）
        unfinished = [q.id for q in self.questions.values() if not q.callback and self._unfinished(q)]
        return {"version": REPORT_VERSION, "unfinished": unfinished, "facts": facts, "not_reached": self.not_reached, "cut": self.cut,
                "apology_delivered": self.apology_delivered, "irreversible_check": self.irreversible_check, "ai_trouble": self.ai_trouble}

    def _close(self, reason: str, prefix: str = "", spoken: bool = False, text: tuple[str, str] | None = None, part: bool = False) -> dict:
        """締め（DESIGN.md 6-14）を作る唯一の場所。毎回同じ文なので、言い方は決まった1つ（ふつう＝CLOSING・急いでいる／断られた／怒っている
        ＝CLOSE_DECLINED）。reason＝締めた理由（END_REASONS の中の名前。通話の事実の end に残す＝_end）。
        録音で流す形なら録音を流してAIは黙る（6-13：毎回同じ文の発話は録音）。AIが話す形と、spoken（録音の無い文＝名乗りの録音に無い
        留守番電話の伝言）は、AIがその言い方のまま言う。相手が最後に質問をしていたら、その質問に答えてから流す（_question_first の after_answer）。⛔ 2026-10-17 の見直しで見つけた漏れ：ふつうの締めは、録音で流す形のときしか言い方を渡しておらず、
        AIが話す形では「締めの言葉を言って」とだけ伝えていた＝AIが自分で文を作り、「お気軽にお問い合わせください」「お気をつけて
        お越しくださいませ」（命令の形）を足した（G-45）。断られたときの締めは録音にしていなかった（「お忙しいところ」はAIの声で崩れやすい）。
        part：text の録音が依頼主の単位（名乗りの部分。留守番電話の伝言）。無ければ共通の一言（締めの録音）。"""
        if self.end_reason is not None or self.trouble is not None:
            return self._ended()
        polite = ("done", "callback", "not_transferred", "departed", "other_means", "goal_refused", "not_agreed")  # お礼の締め（ほかは断られたときの締め）
        # お客様がお断りになって業務の最終目的が果たせなくなった（goal_refused）と、お礼の締めの直前にお断り・目的から遠ざかる向きの答えを受け取った
        # （_turned_down_now＝最終目的が2つの業務で片方を受けていただいたあとの最後のお断り・ここで終える答え）：「承知いたしました。」で受けてから
        # お礼の締め（✅ 2026-11-02 日本時間6時33分 ユーザー：「…だと唐突なので、こちら「はい、承知いたしました。お忙しいところ…」の方がいいんじゃない
        # でしょうか？」）。断りの印（goal_refused）は、目的が果たせなくなったときだけ（_goals_lost）
        # ただし、この締めの前に、ご承認の手順を終える一言（「承知しました。ご不満な点が…」＝_advance が締めの前に流す point の open）を流すなら、お断りはその一言が
        # 受けたので、お礼の締め（本書 9-5：「最終目的のご承認なら、この一言のあとにお礼の締め」＝✅ 2026-11-05 日本時間5時20分56秒）。⛔ 2026-11-06 日本時間
        # 23時22分41秒のご質問の見直し（A1 を直す途中で見つけた）：伺い直したご承認の「いいえ」で手順を終えると「…ご不満な点が…。承知いたしました。お忙しいところ…」
        received = self.point is not None and bool(self.point.get("open"))
        text, rid = text or ((CLOSING_AFTER_DECLINE, "closing_after_decline")
                             if reason == "goal_refused" or (reason in polite and self._turned_down_now() and not received)
                             else (CLOSING, "closing") if reason in polite else (CLOSE_DECLINED, "closing_declined"))
        self._end(reason)
        if self.play_recordings and not spoken:
            item = {"kind": "opening", "part": rid, "text": text} if part else {"kind": "closing", "id": rid, "text": text}
            return {"result": "end", "play": [item],
                    "message": f"{prefix}{played_note(text, '締めの言葉')}何も言わずに終えます。"}
        # 言い方のとおりに言わせる（足しも削りもしない。約束の言葉も足さない＝G-22：断られたときの締めに、AIが6本中5本で「また改めてご連絡
        # いたします」と約束を足した）。⛔ 2026-10-24 の見直し（調べ役）：前は「約束もしません」と書いていた＝保留で待ちきったときの言い方の
        # 「あらためてかけ直させていただきます」と食い違い、AIがその部分を削るおそれがあった
        return {"result": "end", "say": text,
                "message": f"{prefix}{say_note(text)}言い終えたら、電話を終えます（「またお電話します」のような約束の言葉も足しません）。"}

    def _lead(self, kind: str) -> str:
        """受けの言葉（lead_ins が無効なら空）。種類ごとに順番に回す。"""
        if not self.lead_ins:
            return ""
        n = self.lead_counts.get(kind, 0)
        self.lead_counts[kind] = n + 1
        return lead_in_phrase(kind, n)

    def _lead_note(self, kind: str) -> str:
        lead = self._lead(kind)
        return start_with(lead) if lead else ""

    def _reject(self, why: str) -> dict:
        """差し戻す唯一の場所。理由が何であっても、いま求めていること（_now）を必ず添える。"""
        return {"result": "rejected", "message": f"{why}{self._now()}"}

    def _now(self) -> str:
        """いま求めていること（AIが次にすることを迷わないため）。
        ⛔ 2026-10-13 G-16 の2回目：Q2 の言い直しの返事を待っている間に Q3 を報告され、「いまの質問は Q2 です」とだけ返したため、
           AIは Q2 を最初から聞き直した（言い直しの返事を待っていることが、伝わっていなかった）。"""
        if self.own_tried[0] == self.callee_turns and self.own_turn != self.callee_turns:
            return (f"いまは、相手の言葉に返す文を書き直すところです（まだ声に出しません）。書き直した文を「{ARG_LABEL['answer']}」に書いて、"
                    f"replay_opening を「{PART_LABEL['own']}」「{PART_LABEL['no_answer']}」「{PART_LABEL['talk']}」のうち合うもので呼びます。")
        if (asked := self._asked_now()) is not None:
            return self._asked_note(asked)   # 答えの段で、いまの質問・言い直しに答える代わりにこちらへ尋ねた（報告より先に答える＝_apply_one の入口）
        if self.after_answer is not None and not (self.own_pending or self.ai_pending):
            return (f"いまは、相手の質問（「{self.after_answer[0][:40]}」）に答える番です。replay_opening で答え方を選んで答えます"
                    f"（{self._answer_choices()}）。答えたあと、プログラムが次へ進めます。")
        if self.own_pending:
            what = "相手の言葉に、自分の言葉で受け答えした" if self.own_pending == "talk" else "相手の質問に自分の言葉で答えた"
            return (f"いまは、{what}ところです。{self._answer_in_reply()}相手が返事をしたら（「そうですか」「わかりました」「ねえ」など）、"
                    f"{self._return_note()}相手がほかのこと（答え・申し出・別の質問）を言ったら、それに合う報告をします。")
        if self.ai_pending:
            return (f"いまは、エーアイかと聞かれて流した「{self.ai_said}」への、相手の返事を待っています。何も言わずに"
                    f"待ちます。相手が、問題は無いと言ったら（「いえ」「別に」「機械みたいだったので」など）、{self._return_note()}"
                    f"重ねて聞かれたら（「本当に人ですか」など）、もう一度「{PART_LABEL['ai']}」で呼びます。相手がいまの質問に答えたら、"
                    "その答えを報告します。")
        if self._held_now() is not None:
            return f"いまは、相手の言葉に応えたところです。{self._return_note()}"
        if self._explain_due():
            # ご納得いただけなかった点をご説明する番（まだご説明していない）：差し戻し・待ったあとも、いま求めていることはご説明（⛔ 2026-11-08 日本時間6時台
            # （11-07 23時26分13秒のご質問の見直しの A-1）：「いまの質問は N2…」と返し、会話AIは点を伺い直すか、答えとして報告し直した）
            return "いまは、ご説明する番です。" + self._point_note()
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
            return (f"いまは、まだ目的の人に繋いでいません{held}。電話に出た方への「{self.asked_target}」のお返事を待っています。その方が認めたら（「はい」「そうです」など）、もう一度 report_party を"
                    f"「{PARTY_LABEL['target']}」で呼びます（名乗り・用件・最初の質問は、プログラムが流します）。まだ最初の質問はしていないので、"
                    "用件を話したり、record_answer で報告したりしません。目的の人ではなかったら（「いいえ」・違うお名前を名乗ったなど）、replay_opening を"
                    f"「{PART_LABEL['handover']}」で呼びます（お呼びし直す言い方は、プログラムが流します。番号違いではありません）。席を外している・"
                    "出られないと言われたら、request_callback を、聞いた言葉に合う理由で呼びます。")
        if self.phase == "calling":
            return (f"いまは、目的の人（「{self.target['target']}」）にお取り次ぎいただくのを待っています。電話に出た方が目的の人だと分かったら、"
                    f"report_party を「{PARTY_LABEL['target']}」で呼びます（用件と最初の質問は、プログラムが返事で渡します）。"
                    "お取り次ぎいただく間は、何も言わずに待ちます。" + answer_wait("", "calling"))
        if self.current is None:
            return "質問は終わりです。締めの言葉を言って終えてください。"
        q = self.questions[self.current]
        # ⛔ 2026-10-13 G-28：AIが答えを報告せずに言い直し、差し戻されたあと、「いまの質問は Q2 です」としか伝わらず、
        #    報告しないまま言い直しを繰り返した。⇒ いますることの操作まで書く
        return (f"いまの質問は {q.id}「{q.text}」です。" + answer_wait("相手が答えたら record_answer で報告してください（聞き取れなかったら report_unclear）。")
                + (self._wait_note() if q.id == self.wait_q else ""))

    # ── 依頼主へ渡す書き方（7-1b。Q20 の唯一の場所）
    def answer_of(self, qid: str) -> str | None:
        """取れた項目の答え（依頼主へ渡すもの・張り替えのときにAIへ渡す「聞けたこと」の両方。唯一の場所）。選択肢の型は、宣言された選択肢
        （相手の言葉そのものではない。任された答えは「どちらでも」）、それ以外は値。"""
        it = self.items[qid]
        if self.questions[qid].type in CHOICE_TYPES or it.value in (None, ""):
            return label(it.condition)
        return it.value

    def export(self, unknown_mark: str = "-", refused_mark: str = "✕") -> dict:
        """依頼主へ渡す値（DESIGN.md 7-1b）。言い切っていない返事で受け取った値も、値はそのまま（お客様の言葉は customer_words の別の欄）。"""
        out = {}
        for qid in self._exported():
            it = self.items[qid]
            if it.state == "refused":
                out[qid] = refused_mark
            elif it.state in GOT:
                out[qid] = self.answer_of(qid)
            else:
                out[qid] = unknown_mark
        return out

    def _exported(self) -> list[str]:
        """依頼者へ渡す答えの質問（唯一の決まり＝値 export・お客様の言葉 customer_words）：かけ直しの申し出の答え（通話の事実 facts の callback で渡す）と、
        前の通話で取れた答え（この通話の答えではない＝前の通話で渡した）は渡さない。"""
        return [qid for qid in self.items if not (self.questions[qid].callback or qid in self.earlier)]

    def customer_words(self) -> dict:
        """依頼者へ渡す別の欄：業務の質問ごとの、お客様の言葉（DESIGN.md 7-1b。唯一の場所）。
        - 取れた答え（値を渡す）：言い切っていない返事で受け取ったときだけ、その返事の言葉そのまま（値はそのまま＝export。✅ 2026-10-18 ユーザー：案C（「ご予約」の
          欄は「していない」・別の欄にお客様の言葉「していないはずですが…」）が分かりやすい）
        - 取れていない答え（「-」「✕」を渡す）：その答えをいただいたお客様の言葉（答えが動いた発話の最後の2つまで＝Item.said・1つ40字まで＝customer_said）。
          ✅ 2026-11-08 日本時間3時48分03秒 ユーザー：「本題のお客様の意向は、依頼者に「-」が渡っても、「今は決められない」状態か、「拒否」や「必要ない」という
          ことなのか分からないので、お客様の言葉で簡潔に渡せるように！」。まだ伺っていない質問（お客様の言葉が無い）は渡さない"""
        out = {}
        for qid in self._exported():
            it = self.items[qid]
            words = it.hedged if it.state in GOT else "／".join(it.said[-2:])
            if words:
                out[qid] = words
        return out

    def _note_said(self, said: str | None = None, about: str | None = None) -> None:
        """お客様の発話の言葉を、その発話で答えた質問に残す唯一の場所（Item.said＝依頼者へ「-」「✕」の答えに添える customer_words の材料）：
        - about＝その発話が答えた質問（発話の始めに伺っていた質問＝check_for。質問の段の発話だけ＝お呼びする段の受付の方の言葉は入れない）：答えの状態が動かなくても
          残す（「まだ決まってないんですよね。」に目安を伺った・迷っている方に選ぶ助けを伝えた、なども「今は決められない」の言葉）
        - この発話で答えが動いた質問（伺わずに決めた質問・先に言われた答えも）。答えが「伺っていない」へ戻っただけの質問には残さない
        呼ぶのは、お客様の発話を受けて流れが動く所：記録を残す3か所（操作＝_apply_one・報告の無いターン＝judged_turn は about つき・電話接続側からの知らせ＝
        _log_signal）。said を省けば、いまの発話（heard_said＝前の発話の言葉の答えなら、その発話）。同じ言葉は続けて残さない（同じ発話の報告と報告の無いターン）。"""
        words = customer_said(self.heard_said if said is None else said)
        for qid, it in self.items.items():
            moved, it.said_upto = it.history[it.said_upto:], len(it.history)
            if words and (qid == about or any(to != "empty" for _, to, _ in moved)) and words not in it.said[-1:]:
                it.said.append(words)

    def _said_about(self) -> str | None:
        """いまの発話が答えた質問（_note_said の about の唯一の決まり）：発話の始めが質問の段（asking・rechecking）なら、その時に伺っていた質問（check_for）。"""
        return self.check_for if self.turn_phase in ("asking", "rechecking") else None


def tool_declarations(questions: list[Question], replay: bool = False, target: bool = False, answers: tuple = (), known: tuple = (),
                      handoff_ways: tuple = (), close_offer: "CloseOffer | None" = None):
    """AIが呼べる操作（6-2）の宣言を作る唯一の場所。条件の選択肢は、質問の宣言＋システムの2つ。
    target：目的の人のある業務か（相手の種別に、受付・目的の人を足す。DESIGN.md 4-3）。known：自分で答える質問の受け皿に選べる、知っていることの文
    （Flow.known_lines）。
    ⛔ 2026-10-14 G-33：答えの報告を「返事を待たない」宣言にする形は採らない（AIが受けの言葉を先に言わなかった）。"""
    from google.genai import types

    def s(enum=None, desc=None):
        return types.Schema(type=types.Type.STRING, enum=enum, description=desc)

    # システムが足す質問は、どの業務でも報告できるように、初めから宣言に入れる。役目の一覧（SYSTEM_ROLES の元＝言い方の表）から作る（⛔ 2026-10-24：
    # C6・C7・P1 を足したとき、手で並べた一覧に並べ忘れていた＝AIが答えを報告できない。⛔ 2026-11-03 にも同じ穴を見つけた：ご遠慮の確かめ D1・急いでいる方への
    # H1・ご承認いただけなかったときの N1〜N3 が並んでおらず、その答えの番号と選択肢を宣言に出していなかった）。代わりの方のお立場 S1・約束の電話の一言 P1 は
    # 目的の人のある業務だけ。ほかのご希望を伺う W1 は、伺いうる質問ごと（may_wish。選択肢が質問で変わる）
    texts = {"message": "（伝言の文）", "promise_again": "（約束の電話の一言）"}
    added = [callback_question(r, texts.get(r)) for r in ASKED_ROLES if target or r not in ("authority", "promise_again")]
    added += [callback_question("other_wish", of=q) for q in questions if may_wish(q)]
    # 業務設定で決まる申し出（締める前の申し出 N6・方法を選んでいただく N5＝選択肢が業務で変わる＝W1 と同じ。N4 は上の並びにもある）
    added += offer_questions(handoff_ways, close_offer)
    qids = list(dict.fromkeys([q.id for q in questions] + [q.id for q in added]))
    conds = sorted({c for q in questions + added for c in q.labels})
    A = ARG_LABEL

    def fd(name, desc, props, required):
        # 項目と「必ず要る項目」は、受け取ったときの確かめと同じ表（OP_ARGS）から作る
        req, opt = OP_ARGS[name]
        assert set(props) == {A[k] for k in req + opt} and required == [A[k] for k in req], name
        return types.FunctionDeclaration(name=name, description=desc,
                                         parameters=types.Schema(type=types.Type.OBJECT, properties=props, required=required))

    # 「わからない」「答えたくない」の理由の書き方（答えの報告と、言い直しへの返事の、どちらにも同じ1つ。⛔ 2026-10-24 の見直し（調べ役）：言い直しへの
    # 返事には理由の項目が無く、「担当じゃないので分からない」に直されても、業務の決まり（分かるお客様を聞く）が当たらなかった）
    why_note = (f"「{SYSTEM_LABEL['unknown']}」「{SYSTEM_LABEL['refused']}」のとき、相手が理由を言っていたら「{A['unknown_why']}」で選ぶ"
                f"（わからない：{'・'.join(list(DONT_KNOW_LABEL.values())[:-1])}／答えたくない：{list(REFUSED_LABEL.values())[0]}。言っていなければ書かない）")
    return [types.Tool(function_declarations=[
        fd("record_answer", "相手の答えを、いまの質問の選択肢に分類して報告する。"
           f"相手が「わからない」と答えたら「{SYSTEM_LABEL['unknown']}」、答えを拒んだら「{SYSTEM_LABEL['refused']}」、"
           f"選択肢の質問で、どれでもよいとこちらに任されたら（「どちらでも」「どれでも」）「{ANY_LABEL}」（まだ決めていないなら「{SYSTEM_LABEL['unknown']}」）"
           f"、どの選択肢も要らない・どれもしないでほしいと言われたら「{NONE_LABEL}」（答えることを拒んだだけなら「{SYSTEM_LABEL['refused']}」）"
           f"。「あとで」のかけ直しの時刻で、相手が「3時までなら」のように『〇時まで』と言い、いま電話を続けられない理由も言ったら、"
           f"「{A['until_why']}」でその理由を選ぶ（{'・'.join(UNTIL_LABEL.values())}。理由を言っていなければ書かない）"
           f"。かけ直しの日・時刻で、相手がその日・頃を月単位の理由（月ぎめの業務・締め・振り込みや入金の間隔など）で選んだと言ったら、"
           f"「{A['cycle']}」で「{CYCLE_LABEL['month']}」を選ぶ（言っていなければ書かない）。" + why_note,
           {A["question_id"]: s(qids), A["condition_id"]: s(conds), A["value"]: s(desc="聞き取った値（相手の言葉のとおり）"),
            A["until_why"]: s(list(UNTIL_LABEL.values())), A["cycle"]: s(list(CYCLE_LABEL.values())),
            A["unknown_why"]: s(list(UNKNOWN_WHY_LABEL.values())), A["rest"]: s(desc=REST_DESC),
            A["terms"]: s(desc="申し出を、こちらで応えられる条件つきで受けていただいた答え（「月曜以外なら」）だけ：その条件（「月曜日以外で」のような「〜て」「〜で」「〜に」"
                               "で終わる言葉。無ければ付けない）"),
            A["topic"]: s(desc="同じ答えだけ：お客様が気にかけていることに応えて申し出に含める中身（「料金の面も含めて」。業務の情報にあることだけ。無ければ付けない）"),
            A["way"]: s(desc=f"同じ答えだけ：お客様が言った方法（{'・'.join(offer_ways(handoff_ways, close_offer)) or 'なし'}。業務に無い方法でも名前をそのまま。無ければ付けない）")},
           [A["question_id"], A["condition_id"]]),
        fd("report_unclear", "相手の答えが聞き取れない・曖昧だったことを報告する",
           {A["question_id"]: s(qids)}, [A["question_id"]]),
        fd("confirm_field", "言い直した値への、相手の返事を報告する（相手が返事をしてから呼ぶ）。選択肢の質問で、相手が" + why_note,
           {A["question_id"]: s(qids), A["result"]: s(list(CONFIRM_LABEL.values())), A["correction"]: s(desc="直したあとの値（全部）。選択肢の質問では、相手の答えに合う選択肢の名前"),
            A["unknown_why"]: s(list(UNKNOWN_WHY_LABEL.values())),
            A["terms"]: s(desc="申し出の言い直し・伺い直しを、こちらで応えられる条件つきで受けていただいたお返事（「来週以降ならお願いします」）だけ：その条件"
                               "（「来週以降に」のような「〜て」「〜で」「〜に」で終わる言葉。無ければ付けない）"),
            A["topic"]: s(desc="同じお返事だけ：お客様が気にかけていることに応えて申し出に含める中身（「料金の面も含めて」。業務の情報にあることだけ。無ければ付けない）"),
            A["way"]: s(desc="同じお返事だけ：お客様が言った方法（業務に無い方法でも名前をそのまま。無ければ付けない）")},
           [A["question_id"], A["result"]]),
        # 「エーアイか聞かれた」「質問に戻る」は、名乗りの録音が無い通話でも使う（決まった言い方とそのあとは流れの制御が決める＝Flow._ai_question）
        # 「代わった方へ」は、目的の人のある業務では録音が無くても使う（目的の人を呼んでいる段で、代わった方を知らせる＝Flow._calling_reported）
        fd("replay_opening", ("名乗りや用件を聞き返されたとき、自分では言わずに、録音で流し直してもらう（どちら様＝"
                              f"「{PART_LABEL['intro']}」、ご用件＝「{PART_LABEL['purpose']}」、聞き取れなかった様子＝「{PART_LABEL['all']}」、"
                              f"別の人に代わった＝「{PART_LABEL['handover']}」）。" if replay else
                              f"目的の人をお呼びしているときに、別の人に代わった＝「{PART_LABEL['handover']}」。" if target else "")
           + f"エーアイか・人間かと聞かれた＝「{PART_LABEL['ai']}」（何と言うかはプログラムが決めます）。相手からそのほかの質問をされた＝"
           "想定問答にある問いならその名前" + "".join(f"（「{n}」）" for n in answers)
           + f"、無ければ、業務の情報にあることなら「{PART_LABEL['own']}」・業務の情報にも無いことなら「{PART_LABEL['no_answer']}」（この2つは、"
           f"自分の言葉で答える文を「{ARG_LABEL['answer']}」に書きます。「{PART_LABEL['own']}」では、答えのもとにした業務の情報の文を"
           f"「{ARG_LABEL['fallback']}」で選びます＝答えの文が使えないとき、その文をそのまま言います。声に出すのは返事のあと。番号をどこで知ったか・金額・会社のことなどは"
           f"「{PART_LABEL['purpose']}」では答えません）。質問ではない言葉（あいさつ・お礼・気遣い・世間話・お客様のお話）に、ひとこと返したい＝"
           f"「{PART_LABEL['talk']}」（返す文を「{ARG_LABEL['answer']}」に書き、返す文が使えないときに流す一言を「{ARG_LABEL['fallback']}」から、"
           "お客様のお話と調子にいちばん合うものを選びます：" + TALK_CHOICES
           + f"）。エーアイかへの言い方・自分の言葉の答え・受け答えに、相手が返事をした"
           f"＝「{PART_LABEL['resume']}」",
           {A["part"]: s([v for k, v in PART_LABEL.items() if replay or k in ("ai", "resume") + OWN_PARTS or (target and k == "handover")]
                         + list(answers)),
            A["answer"]: s(desc="自分の言葉で言う文（短く1〜2文。こちらについての事実は業務の情報にあることだけ。名前・約束・お客様への問いかけを入れない）"),
            A["fallback"]: s(list(TALK_LABEL.values()) + list(POINT_KIND_LABEL.values()) + list(known),
                             desc=f"「{PART_LABEL['talk']}」「{PART_LABEL['own']}」のときだけ：返す文が使えないときの一言（受け答えは決まった一言・"
                                  "自分で答える質問は、答えのもとにした業務の情報の文）。ご納得いただけなかった点のご説明を頼まれていて"
                                  f"「{PART_LABEL['no_answer']}」で呼ぶときは、お客様の言ったことの種類（{POINT_KIND_CHOICES}）"),
            A["terms"]: s(desc=f"プログラムが、申し出へのお客様のお話にひとこと応える番だと知らせたときの「{PART_LABEL['talk']}」だけ：お客様の言ったことにある、"
                               "申し出の受け方の条件（「月曜日を避けて」のような「〜て」「〜で」「〜に」で終わる言葉。無ければ付けない）"),
            A["topic"]: s(desc=f"同じ番の「{PART_LABEL['talk']}」だけ：お客様が気にかけていること（料金・内容など）に応えて、詳しい者がお話しする中身"
                               "（「料金の面も含めて」のような「〜を」「〜て」で終わる言葉。業務の情報にあることだけ。無ければ付けない）"),
            A["added"]: s(desc=f"同じ番の「{PART_LABEL['talk']}」だけ：お客様は言っていないが、お客様のお話に合わせてこちらから足すご都合（「夕方以降に」のような"
                               "「〜て」「〜で」「〜に」で終わる言葉。無ければ付けない）"),
            A["way"]: s(desc=f"同じ番の「{PART_LABEL['talk']}」だけ：申し出へのお話なら、お客様が言ったご希望の方法が業務の方法"
                             f"（{'・'.join(offer_ways(handoff_ways, close_offer)) or 'なし'}）にあれば、その名前をそのまま（無ければ付けない）。方法を選んでいただく"
                             "質問へのお話なら、お客様のお話に合う方法を、プログラムが知らせた業務の方法から1つ必ず選んで、その名前をそのまま"),
            A["rest"]: s(desc=f"「{PART_LABEL['resume']}」と、プログラムが申し出へのお話にひとこと応える番だと知らせたときの「{PART_LABEL['talk']}」だけ："
                              + REST_KEEP_DESC)},
           [A["part"]]),
        fd("report_party", f"電話に出た方の最初の言葉を聞いたら、出た方が人か（「{PARTY_LABEL['person']}」"
           + (f"。お呼びする方ご本人が名乗ったら「{PARTY_LABEL['target']}」" if target else "")
           + "）・留守番電話の案内・自動音声の案内・ファクスの音かを知らせる。そのあとも、電話に出たのが人ではないと分かったときや、番号違い"
           "（かけた先とは別の人・別の会社）だったときに知らせる" + ("。受付の方が出た・目的の人に代わった・電話口の方が目的の人の代わりに伺うと言った（代わりの方）・"
                                     "自分が後任だと言った（後任の方）ときも知らせる" if target else ""),
           {A["party"]: s([PARTY_LABEL[k] for k in ("person",) + (PERSON_PARTIES + STANDIN_PARTIES if target else ()) + NOT_A_PERSON + ("wrong_party",)])},
           [A["party"]]),
        fd("request_callback", "相手と、いまは話を進められないときに呼ぶ（理由と相手の申し出を選ぶ。何と言うかはプログラムが流します）。"
           + offers_rule(target),
           {A["why"]: s([v for k, v in CALLBACK_LABEL.items() if target or k != "departed"]),
            A["offer"]: s(list(OFFER_LABEL.values()), desc="相手の申し出（無ければ付けない）"), A["rest"]: s(desc=REST_DESC)}, [A["why"]]),
        fd("request_end", f"電話を終えたいときに呼ぶ。相手が急いでいる・切ろうとしているなら「{END_LABEL['callee_hurry']}」、"
           f"通話自体を断られた（「営業はお断りしています」「もうかけないで」など。メールなど、ほかの手段を示されたときは request_callback）なら"
           f"「{END_LABEL['callee_refused_call']}」、"
           f"相手が怒っている・苦情を言っているなら「{END_LABEL['callee_complaint']}」、受付の方が営業のお断りではない事情で取り次げない"
           f"と言ったなら「{END_LABEL['not_transferred']}」、質問が全部終わったなら「{END_LABEL['done']}」",
           {A["reason"]: s(list(END_LABEL.values()))}, [A["reason"]]),
    ])]
