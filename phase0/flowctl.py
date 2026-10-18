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
from phrases import AMPM_ASK, CLOSE_DECLINED, CLOSING, LEAD_IN, NAME_READING_ASK
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


def spoken_question(questions: list["Question"], qid: str) -> tuple[str, str]:
    """録音で流す質問の文（つなぎの言葉＋質問の文）と、そのつなぎの言葉。つなぎの言葉は質問の順番で決める（2問目「それでは」・3問目
    「続きまして」・4問目「では」…）。最初の質問は名乗りと一緒に流すので、つなぎの言葉は付けない。
    ✅ 2026-10-15 ユーザー（聞き比べ）：つなぎの言葉と質問を1本で録音したほうが「しいて言えば自然」⇒ 1本の録音にする。順番で決めておけば、
    依頼主ごとの録音は質問1つにつき1本のまま（流れの制御・録音を作る道具の両方が、ここを使う）"""
    q = next(x for x in questions if x.id == qid)
    if q.reading_of:  # プログラムが足す読みの確かめ：AIが言う（録音でない）。つなぎの言葉は付けない
        return q.text, ""
    # ⛔ 見直しで見つけた漏れ：読みの確かめを挟むと、そのあとの質問のつなぎの言葉（＝録音の名前）が1つずれた（「それでは」⇒「続きまして」）
    i = [x.id for x in questions if not x.reading_of].index(qid)
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
             "reason": "理由", "part": "流し直す部分", "party": "相手"}
SYSTEM_LABEL = {"unknown": "わからない", "refused": "答えたくない"}
ANY_LABEL = "どちらでも"
# 意味の2つ目の判定（meaning_judge。DESIGN.md 6-10 の (6)）が、どの選択肢とも決められないと答えたときの名前（受け入れにも断りにも取れる・言い切っていない・答えになっていない）
UNDECIDED = "決められない"
# 意味の判定が、答えではないと決める言葉（どの場面でも）：相手が取り次ぐので待つように言った／代わった別の人が話し始めた（DESIGN.md 6-8）
HOLD = "取り次ぎを待つ"
HANDOVER = "代わった人が話し始めた"
# 確かめの名前（Flow.on_check）：別の耳の文字（聞き取り。6-10 の (5)）・意味の判定（6-10 の (6)。judge2＝後ろで走らせる2つ目の判定）
CHECKS = ("ear", "judge", "judge2")
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
             "callee_complaint": "相手が怒っている"}
# 断られたときの締め（CLOSE_DECLINED）で終える理由。念のための確認はしない（DESIGN.md 6-5）
DECLINED_ENDS = ("callee_hurry", "callee_refused_call", "callee_complaint")
REPLY_KEY_LABEL = {"result": "結果", "message": "次にすること", "question_id": "質問", "question": "質問の文", "value": "値", "say": "言い方"}
RESULT_LABEL = {"next": "次の質問", "confirm_required": "言い直して確かめる", "rejected": "差し戻し", "ask_back": "聞き返す",
                "recheck": "念のための確認", "end": "締め", "busy_ask": "引き止めの一言", "replayed": "録音で流し直した",
                "hold": "取り次ぎを待つ", "noted": "受け付けた"}
# 名乗りの流し直し（DESIGN.md 6-13。✅ 2026-10-13 ユーザー：名乗り直しも録音で流す「間が大丈夫ならOK」）：AIが選ぶ部分の名前
# 名乗り＝「どちら様ですか」／用件＝「何のご用件ですか」／最初から＝聞き取れなかった様子（「もう一度」「はい？」）／代わった方へ＝別の人に代わった
PART_LABEL = {"intro": "名乗り", "purpose": "用件", "all": "最初から", "handover": "代わった方へ"}
OPENING_ORDER = ("greeting", "intro", "purpose")  # 名乗りの録音の部分の順（instruction.Business.replay_parts の鍵）
# 相手の種別（DESIGN.md 4-3。report_party）：電話に出た相手が何か（AIに見せる名前なので英字を使わない＝FAX は「ファクス」）。受付・目的の人は、目的の人のある業務だけでAIに見せる
# （「誰でも」の業務は、最初の相手が目的の人＝報告しなくてよい）。人ではない相手（留守番電話・自動音声の案内・FAX）には話さずに終え、
# 番号違いは決まった締めで終える（Intent.md 9-4）
PARTY_LABEL = {"reception": "受付の方", "target": "目的の人", "voicemail": "留守番電話", "ivr": "自動音声の案内", "fax": "ファクスの音",
               "wrong_party": "番号違い"}
NOT_A_PERSON = ("voicemail", "ivr", "fax")
PERSON_PARTIES = ("reception", "target")
# hang_up＝話さずに切る（電話接続側が読む）
INTERNAL_KEYS = {"ack", "ask_n", "ask_reason", "ask_last", "after", "play", "speak", "hang_up"}  # 中で使うだけの項目（電話接続側・数え方が読む）。AIへの返事には入れない
# こちらが使う名前（AIに見せる日本語の名前）。値の欄に入っていたら、相手の答えではない（form_problem）
_LABELS = set(SYSTEM_LABEL.values()) | set(CONFIRM_LABEL.values()) | set(END_LABEL.values()) | set(PARTY_LABEL.values()) | {"答えた"}
_FROM_LABEL = {"condition_id": {**{v: k for k, v in SYSTEM_LABEL.items()}, ANY_LABEL: ANY}, "result": {v: k for k, v in CONFIRM_LABEL.items()},
               "reason": {v: k for k, v in END_LABEL.items()}, "part": {v: k for k, v in PART_LABEL.items()},
               "party": {v: k for k, v in PARTY_LABEL.items()}}
_ARG_FROM_LABEL = {v: k for k, v in ARG_LABEL.items()}


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
           "report_party": (("party",), ())}
# ✅ 2026-10-14 ユーザー：考えている相手への「はい」は形 A（AIが自分で言う）。⇒ 比べた形 B（AIは黙って「相手が考えている」と報告し、
#    相づちはプログラムが録音で流す。G-36）の操作は外した（B は、答えを考えている途中と読み違えて黙った回があった）
OPS = tuple(OP_ARGS)
# 選択肢が決まっている項目（中の名前 → その表）。答えの種類（condition_id）は質問ごとに違うので、record_answer の中で確かめる
_ENUM_ARGS = {"result": CONFIRM_LABEL, "reason": END_LABEL, "part": PART_LABEL, "party": PARTY_LABEL}


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
    # 選択肢の型の、選択肢ごとの意味の言葉（選択肢 → 「ご予約はお済みということ」）。答えを言い直して確かめるときに使う（choice_words）。
    # 取込で文字のモデルが決める（人は確かめない）。無い選択肢は名前のまま言い直す（返事の言葉だけの名前は言い直せない）
    meanings: dict = field(default_factory=dict)
    # 受け入れにも断りにも取れる言葉（AMBIGUOUS_WORDS）だけの答えが、この質問ではどの選択肢の意味になるか（言葉 → 選択肢。例：
    # 「パンフレットをお送りしてもよろしいでしょうか？」の「結構」→「いいえ」）。取込で文字のモデル2つが質問の文から決める（meanings.decide_replies。
    # G-49）。無い言葉は、通話の中で意味を確かめる。✅ 2026-10-17 ユーザー：「人でも前の質問や会話から判断する言葉です」
    reply_meanings: dict = field(default_factory=dict)

    def __post_init__(self):
        assert self.type in TYPES, f"{self.id} の型「{self.type}」は、型の一覧にありません（I-16）"
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
                 voicemail_message: str | None = None):
        """confirm：言い直しの言い方を作る関数 (値, 場面, 何回目) → 文（ふだんは phrases.confirm。言い方を比べる試験 G-25 だけが別のものを渡す）。
        busy_ask：相手が「忙しい」と言ったときに1回だけ言う一言（業務設定。None なら丁寧に終える＝既定。Intent.md 9-4）。"""
        self.confirm = confirm
        self.busy_ask = busy_ask
        # 留守番電話に残す伝言（業務設定。Intent.md 9-4・18-2 の14。None なら残さずに切る＝安全側）
        self.voicemail_message = voicemail_message
        self.party: str | None = None     # いまの相手の種別（PARTY_LABEL の中の名前。報告が無ければ None＝不明。DESIGN.md 4-3）
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
        # いまの相手の発話の確かめ（on_check。CHECKS の名前 → 別の耳の文字／意味の判定の中の名前。届いていないものは入らない）
        self.check: dict[str, str] = {}
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
        # 名乗りの録音のうち、相手にまだ届いていない部分（on_opening_cut。録音が最後まで流れたら空）
        self.opening_unheard: list[str] = []
        if self.questions[self.current].reading_of:
            self._enter_reading(self.current)

    # ── 名乗りの録音の途中で相手が話し始めて、録音を止めた（電話接続側の見張りが止めた。DESIGN.md 6-13）
    def on_opening_cut(self, done: list[str]) -> None:
        """届いた文（done）から、まだ届いていない名乗りの部分を覚える。流し直しのとき、AIが選んだ部分に足して流す（_op_replay_opening）。
        ⛔ G-50：用件が届く前に「どちら様ですか」と聞かれると、AIは「名乗り」だけを選んだ（11本中11本）＝用件が一度も届かないまま
        質問へ進む。どこまで届いたかはプログラムが知っているので、足すのはプログラム（AIの選び方に任せない）。"""
        self.opening_unheard = [k for k in OPENING_ORDER if self.opening.get(k) and self.opening[k] not in done]

    # ── 相手の発話が届いた（本番では、相手の音声の発話の検出で数える）
    def on_callee(self) -> None:
        self.callee_turns += 1
        self.callee_said = ""
        self.check = {}
        self.holding = False

    # ── 相手の同じ発話を、張り替えた新しいつなぎへ送り直す（DESIGN.md 6-7）：相手は話していないので数えず、文字だけを受け取り直す。
    #    ⛔ 2026-10-17 に見つけた穴：送り直しで on_callee を呼ばないまま、新しいつなぎの聞き取りを足していた（同じ言葉が2回たまる）
    def on_resend(self) -> None:
        self.callee_said = ""
        self.check = {}

    # ── 相手の言葉の文字が届いた（会話AIの聞き取り。値を直されたときの直され方を、プログラムが決めるのに使う）
    def on_heard(self, text: str) -> None:
        self.callee_said += text

    @property
    def ear_said(self) -> str | None:
        return self.check.get("ear")

    @property
    def meaning_said(self) -> str | None:
        return self.check.get("judge")

    # ── この通話で使う確かめ（電話接続側が、通話の始めに1回呼ぶ。CHECKS の名前）
    def use_checks(self, *kinds: str) -> None:
        assert all(k in CHECKS for k in kinds), kinds
        self.checks_used = tuple(kinds)

    # ── 確かめが届いた（唯一の入口）：別の耳（会話AIとは別の音声認識。DESIGN.md 6-10 の (5)。G-15）の文字、または意味の判定（会話AIとは
    #    別の文字のAI。meaning_judge。6-10 の (6)。G-53）の答え（中の名前・HOLD・HANDOVER・UNDECIDED）。turn＝どの相手の発話の確かめか
    #    （on_callee で数えた番号。省けば、いまの発話）。落ちた・時間切れは呼ばない（届いていない＝unchecked）。
    #    その発話への報告で、もう受け取った項目があれば、同じ決まりで当てる（_late）
    def on_check(self, kind: str, value: str | None, turn: int | None = None) -> None:
        assert kind in CHECKS, kind
        turn = self.callee_turns if turn is None else turn
        if value is None:
            return
        if turn == self.callee_turns:
            self.check[kind] = value
        for qid, t in list(self.taken.items()):
            if t["turn"] == turn and kind not in t["check"]:
                t["check"][kind] = value
                self._late(qid)

    def judge_context(self) -> tuple[str | None, str | None]:
        """意味の判定に渡すもの（相手の発話の始めに決める）：いまの質問と、こちらが言い直して確かめた言葉（言い直しの返事を待っているとき）。"""
        return self.current, (self.awaiting_say if self.awaiting_confirm is not None else None)

    # ── AIのターンが、声は出したのに報告なしで終わった（電話接続側が呼ぶ）：報告を促す知らせ（出さないなら None）。
    def unreported_note(self) -> str | None:
        """いまの質問の答えだと、意味の判定が決めた相手の言葉（on_check の judge が選択肢・どちらでも・わからない・答えたくないのどれか）に、
        AIが報告しなかったときだけ、1つの相手の発話に1回まで。知らせには、いまの質問の文を入れない（報告することだけ）。
        この知らせへのAIの声は、報告より前のものは相手に流さない（電話接続側）。
        ⛔ 2026-10-17：記録の通話で、相手の「はい。」「はい、先週しました。」（雑音）のあと、AIが短く声を出しただけで報告せずに終えた回が34回。
           プログラムは気づく手立てを持っていなかった（PHASE0「答えの報告が抜けたとき」の案）。
        ⛔ 2026-10-17 の1回目：判定を見ずに、相手の言葉があれば出し、知らせに「いまの質問は…」を入れた ⇒ 取り次ぎ（「代わりますので少々
           お待ちください」）のあとにも出て、声の型 S4 の3本中2本で、代わった方にAIが自分で名乗って質問した・流し直しを頼まなかった
           （6-7 の G-19 と同じ形：AIへの文字の合図が会話を乱す）。答えの3本では、AIは3本とも黙って正しく報告した。"""
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
        q = self.questions.get(self.current) if self.current else None
        if q is None:
            return False
        if name == "record_answer":
            return self.awaiting_confirm is None and (q.type in CHOICE_TYPES or not q.confirm or a.get("condition_id") in SYSTEM_CONDITIONS)
        return name == "confirm_field" and self.awaiting_confirm is not None

    # ── AIの操作を受ける唯一の入口（AIに見せる言葉＝日本語）。ここで1回だけ中の名前に置き換え、返事を日本語に戻す。
    #    相づち（ack）は電話接続側が流すもので、AIには見せない（AIの返事から外す）
    def handle(self, fc) -> dict:
        return self._reply_out(self.apply(fc.name, self._args_in(dict(fc.args or {}))))

    # ── handle と同じ。返事を「話させない」形（SILENT）で返すかも返す（電話接続側が録音を流すときは、AIに話させない）
    def handle_call(self, fc) -> tuple[dict, bool]:
        reply = self.handle(fc)
        last = self.log[-1]["reply"]
        return reply, (bool(last.get("play")) and not last.get("speak")) or bool(last.get("hang_up"))

    # ── 中の名前での入口（handle と試験は、ここを通す）。記録（log）は中の名前で残す
    def apply(self, name: str, args: dict) -> dict:
        before = {qid: it.state for qid, it in self.items.items()}
        if name not in OPS:
            reply = self._reject(f"その操作はありません（使える操作は {'・'.join(OPS)}）。")
        else:
            why = self._check_args(name, args)
            reply = self._reject(why) if why else getattr(self, f"_op_{name}")(args)
        for qid, it in self.items.items():
            # 名前の読みは、認めたか直されたかを相手の言葉でプログラムが決める（✅ 2026-10-14 ユーザー決定 (a)。_reading_denied）＝確かめに当てない
            if it.state in TAKEN and before[qid] != it.state and not self.questions[qid].reading_of:
                # 受け取ったときの材料を残す（遅れて届いた確かめを、同じ決まりで当てる＝_late／届かなかった確かめを数える＝unchecked）。
                # 言い直しへの返事で確かめた項目は、値を聞く型なら「合っている」が受け取った答え（選択肢の型は選択肢）
                q, confirming = self.questions[qid], it.state == "confirmed"
                self.taken[qid] = {"turn": self.callee_turns, "said": self.callee_said, "check": dict(self.check), "confirming": confirming,
                                   "reported": "affirmed" if confirming and q.type not in CHOICE_TYPES else it.condition}
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
        if result in ("rejected", "hold"):
            return None  # 差し戻し・取り次ぎを待つ（相手はいま聞いていない）
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
        if name == "report_party":
            return None  # 留守番電話・番号違い・受付に「承知しました」などは合わない（番号違いの締めの頭に「失礼いたしました。」を流すと、締めの「失礼」と重なる）
        if name == "replay_opening":
            # 名乗りの流し直しの頭は「失礼いたしました。」（6-13）。代わった方へは、録音のあいさつから始まるので相づちは流さない
            return None if args.get("part") == "handover" else "shitsurei"
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
    def _check_args(name: str, args: dict) -> str | None:
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
            if k in args and args[k] not in table:
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
            if why and q.required:
                # 言い直す選択肢が無い（聞き取りが食い違う・値を聞く型で答えがあったと判定した）：質問から聞き直す（6-11 の回数の中で）
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
        (3) 意味の判定（check["judge"]・check["judge2"]。意味。6-10 の (6)。meaning_judge）
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
        for kind in ("judge", "judge2"):
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
                   for k in ("judge", "judge2"))

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
            return None
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
            self.holding = True
            return {"result": "hold", "message": f"相手は、ほかの人に取り次いでいます（「{said}」）。答えではありません。何も言わずに、"
                                                 f"代わった方が話し始めるのを待ってください。{then}"}
        if self.meaning_said == HANDOVER:
            return self._reject(f"相手の言葉（「{said}」）は、代わった方が話し始めた言葉で、答えではありません。" + then.replace("代わった方が話し始めたら、", ""))
        return None

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
        if not self.opening:
            return self._reject("この通話では、名乗りの録音はありません。聞かれたことに、自分で短く答えてください。")
        if part == "handover" and self.meaning_said == HOLD:
            return self._not_an_answer()
        if self.replays >= self.limits.replays:
            return self._reject(f"名乗りの録音は、もう{self.replays}回流しました（同じ録音を何度も流しません）。聞かれたことに、自分の言葉で短く答えてください。")
        keys = {"intro": ["intro"], "purpose": ["purpose"], "all": ["intro", "purpose"], "handover": ["greeting", "intro", "purpose"]}[part]
        # まだ届いていない名乗り・用件も一緒に流す（on_opening_cut）。あいさつは足さない（頭にお詫びの相づちが流れるので、重ねない）
        keys = [k for k in OPENING_ORDER if self.opening.get(k) and (k in keys or (k != "greeting" and k in self.opening_unheard))]
        if not keys:
            return self._reject(f"「{PART_LABEL[part]}」の録音はありません。聞かれたことに、自分で短く答えてください。")
        self.replays += 1
        self.opening_unheard = [k for k in self.opening_unheard if k not in keys]
        play = [{"kind": "opening", "part": k, "text": self.opening[k]} for k in keys]
        shown = "".join(self.opening[k] for k in keys)
        if self.awaiting_confirm is not None:
            return {"result": "replayed", "play": play, "speak": True,
                    "message": f"「{shown}」は、プログラムが録音で流しました（相手にもう聞こえています）。続けて、言い直し「{self.awaiting_say}」"
                               "をもう一度言って、相手の返事を待ってください（前に言葉を足しません）。"}
        q = self.questions.get(self.current) if self.current else None
        if q is not None and self.play_recordings and not q.reading_of:
            text, lead = spoken_question(list(self.questions.values()), q.id)
            play.append({"kind": "question", "id": q.id, "text": text, "lead": lead})
            shown += text
        elif q is not None:
            return {"result": "replayed", "play": play, "speak": True,
                    "message": f"「{shown}」は、プログラムが録音で流しました（相手にもう聞こえています）。続けて、いまの質問 {q.id}「{q.text}」を"
                               "聞いてください（名乗りは繰り返しません）。"}
        if q is None:
            # 聞いている質問が無い（質問が全部済んで締めの最中など）。⛔ 2026-10-17 の見直しで見つけた漏れ：前は、どの場面でも
            #    「相手の答えを待って record_answer で報告」と返していた＝答える質問が無いのに、報告を求めていた
            return {"result": "replayed", "play": play,
                    "message": f"「{shown}」は、プログラムが録音で流しました（相手にもう聞こえています）。何も言わずに、相手が話すのを待ちます。"
                               "相手が話したら、それに答えます（名乗りは繰り返しません）。"}
        return {"result": "replayed", "play": play,
                "message": f"「{shown}」は、プログラムが録音で流しました（相手にもう聞こえています）。何も言わずに、相手の答えを待ってください。"
                           "相手が答えたら record_answer で報告します。"}

    def _op_report_party(self, a: dict) -> dict:
        """相手の種別の報告（DESIGN.md 4-3・6-2）。人ではない相手（留守番電話・自動音声の案内・FAX）には話さずに切る（留守番電話は、業務設定の
        伝言があれば残してから切る）。番号違いは、断られたときと同じ決まった締めで終える（業務の判定は範囲外＝7-1 の順2）。受付・目的の人は
        記録して続ける（目的の人に繋がってから質問を始める流れは、目的の人のある業務の流れ）。
        ⛔ 2026-10-18 の見直しで見つけた漏れ：相手の種別を報告する手立てが無く、留守番電話の案内・自動音声にも、AIは質問を続けるか、
           自分で何かを言うしかなかった（Intent.md 9-4 の「伝言を残すか残さないかを設定で選ぶ」に届かない）"""
        party = a["party"]
        self.party = party
        self._note("party", party=party)
        if party == "voicemail" and self.voicemail_message:
            return self._close("voicemail", text=(self.voicemail_message, "voicemail_message"))
        if party in NOT_A_PERSON:
            return self._hang_up(party)
        if party == "wrong_party":
            return self._close("wrong_party")
        return {"result": "noted", "message": f"受け付けました。{self._now()}"}

    def _hang_up(self, reason: str) -> dict:
        """話さずに切る唯一の場所（人ではない相手）。締めの言葉も相づちも流さない。"""
        self.current, self.phase, self.end_reason = None, "closing", reason
        self._note("end", reason=reason)
        return {"result": "end", "hang_up": True,
                "message": f"相手は{PARTY_LABEL[reason]}です（人ではありません）。何も言わずに終えます（言葉を言いません）。"}

    def _op_request_end(self, a: dict) -> dict:
        reason = a.get("reason")
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

    def _advance(self, prefix: str) -> dict:
        # 相手が質問をしていたら、録音は流さずに、AIがまず短く答えてから次へ進む（録音で流すと、相手の質問にかぶせることになる）
        asked = self._callee_question()
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
                text, lead = spoken_question(list(self.questions.values()), q.id)
                return {"result": "next", "question_id": q.id, "question": q.text,
                        "play": [{"kind": "question", "id": q.id, "text": text, "lead": lead}],
                        "message": f"{prefix}次の質問 {q.id}「{text}」は、プログラムが録音で流しました（相手にもう聞こえています）。"
                                   "何も言わずに、相手の答えを待ってください。相手が答えたら record_answer で報告します。" + choices_note(q)}
            return {"result": "next", "question_id": q.id, "question": q.text,
                    "message": f"{prefix}次の質問は {q.id}「{q.text}」です。" + self._lead_note("次の質問") + choices_note(q)}
        if self.phase == "asking":
            # 締めの前に、「-」の必須項目を1回だけ念のため確かめる（6-11）。急いでいる相手にはしない
            self.recheck_queue = [qid for qid in self.order
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
        return self._close("done", prefix=f"{prefix}質問は終わりです。", asked=bool(asked))

    @property
    def hurried(self) -> bool:
        """相手が急いでいる・通話を断られた・怒っている、で締めたか（念のための確認をしない・締めは断られたときの言い方）。"""
        return self.end_reason in DECLINED_ENDS

    def _note(self, kind: str, **extra) -> None:
        """通話の事実を残す唯一の場所（self.facts）。根拠の発話＝相手の発話の番号（on_callee で数えた番号）と、その文字（会話AIの聞き取り）。
        Intent.md 9-5「各項目には、根拠になった発話（時刻つき）を保存する」・11-1「拒否・同意の記録（根拠の発話）」。時刻は電話接続側が
        発話の番号に付ける（試作では通話の記録の turns）。"""
        self.facts.setdefault(kind, []).append({"turn": self.callee_turns, "said": (self.callee_said or "").strip()[:80], **extra})

    def _close(self, reason: str, prefix: str = "", asked: bool = False, text: tuple[str, str] | None = None) -> dict:
        """締め（DESIGN.md 6-14）を作る唯一の場所。毎回同じ文なので、言い方は決まった1つ（ふつう＝CLOSING・急いでいる／断られた／怒っている
        ＝CLOSE_DECLINED）。reason＝締めた理由（END_LABEL の中の名前。通話の事実の end に残す）。
        録音で流す形なら録音を流してAIは黙る（6-13：毎回同じ文の発話は録音）。AIが話す形と、相手の質問にAIが答えてから締めるときは、
        AIがその言い方のまま言う。⛔ 2026-10-17 の見直しで見つけた漏れ：ふつうの締めは、録音で流す形のときしか言い方を渡しておらず、
        AIが話す形では「締めの言葉を言って」とだけ伝えていた＝AIが自分で文を作り、「お気軽にお問い合わせください」「お気をつけて
        お越しくださいませ」（命令の形）を足した（G-45）。断られたときの締めは録音にしていなかった（「お忙しいところ」はAIの声で崩れやすい）。"""
        text, rid = text or ((CLOSING, "closing") if reason == "done" else (CLOSE_DECLINED, "closing_declined"))
        self.current, self.phase, self.end_reason = None, "closing", reason
        self._note("end", reason=reason)
        if self.play_recordings and not asked:
            return {"result": "end", "play": [{"kind": "closing", "id": rid, "text": text}],
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
        return {qid: it.hedged for qid, it in self.items.items() if it.state in GOT and it.hedged}


def tool_declarations(questions: list[Question], replay: bool = False, target: bool = False):
    """AIが呼べる操作（6-2）の宣言を作る唯一の場所。条件の選択肢は、質問の宣言＋システムの2つ。
    target：目的の人のある業務か（相手の種別に、受付・目的の人を足す。DESIGN.md 4-3）。
    ⛔ 2026-10-14 G-33：答えの報告を「返事を待たない」宣言にする形は採らない（AIが受けの言葉を先に言わなかった）。"""
    from google.genai import types

    def s(enum=None, desc=None):
        return types.Schema(type=types.Type.STRING, enum=enum, description=desc)

    qids = [q.id for q in questions]
    conds = sorted({c for q in questions for c in q.labels})
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
        *([fd("replay_opening", "名乗りや用件を聞き返されたとき、自分では言わずに、録音で流し直してもらう（どちら様＝"
              f"「{PART_LABEL['intro']}」、ご用件＝「{PART_LABEL['purpose']}」、聞き取れなかった様子＝「{PART_LABEL['all']}」、"
              f"別の人に代わった＝「{PART_LABEL['handover']}」）",
              {A["part"]: s(list(PART_LABEL.values()))}, [A["part"]])] if replay else []),
        fd("report_party", "電話に出たのが人ではないとき（留守番電話の案内・自動音声の案内・ファクスの音）や、番号違い（かけた先とは別の人・別の会社）"
           "だったときに知らせる" + ("。受付の方が出た・目的の人に代わったときも知らせる" if target else ""),
           {A["party"]: s([PARTY_LABEL[k] for k in (PERSON_PARTIES if target else ()) + NOT_A_PERSON + ("wrong_party",)])}, [A["party"]]),
        fd("request_end", f"電話を終えたいときに呼ぶ。相手が急いでいる・切ろうとしているなら「{END_LABEL['callee_hurry']}」、"
           f"通話自体を断られた（「営業はお断りしています」「もうかけないで」など）なら「{END_LABEL['callee_refused_call']}」、"
           f"相手が怒っている・苦情を言っているなら「{END_LABEL['callee_complaint']}」、質問が全部終わったなら「{END_LABEL['done']}」",
           {A["reason"]: s(list(END_LABEL.values()))}, [A["reason"]]),
    ])]
