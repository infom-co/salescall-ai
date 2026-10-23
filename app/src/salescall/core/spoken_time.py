"""お客様が電話で言った、かけ直しの時刻を読む（Q「その言葉は、いつのことか」）。持ち主 CL-2。DESIGN.md 7-1d・7-2。

会話の報告（版3。CL-1 の連絡帳 2-2 の版 0.4・4-22）は、かけ直しの時刻を、確かめ済みの話し言葉のまま返す（例「明日の午後3時」）。
その言葉を時刻に直すのは、**この1か所**（締めの判定が呼ぶ。会話の側も、通話の中で「この言葉で時刻が決まるか・確かめが要るか」を
知るときは、これを呼ぶ＝読み方を2つにしない）。

**読み方（2026-10-19 ユーザー決定）**：時刻の言葉は「かけてよい時間の幅」として読む（「夕方」「3時まで」も、それに合わせてかける。
「明日なら、いつでも」「13日であれば、そちらのご都合で」「来週」「月末」「週中」「しあさって」なども読む）。答えは5つ：
- `AT`（ちょうどの時刻）：「明日の午後3時」「正午」。お客様が自分で指定した時刻なので、その時刻を含むNG時間の枠でもかけてよい。
- `WINDOW`（幅）：start〜end（end＝その時刻より前。None＝終わり無し）、1日の中の幅 daily（時間の言葉があるときだけ）、
  話せなかったときの次の回の決め方 repeat（お客様がその日を選んだ理由。下の DAY_REPEAT ほか）、日の種類 days（「平日」
  「土日祝」などのときだけ。かけてよい日の鍵＝calling_hours の曜日・祝日の鍵）。幅の中でも、かけてよい時間帯・NG時間は守る（お客様は時刻を指定していない）。「すぐ」「なるべく早く」は、いまから
  終わり無しの幅（2026-10-14 ユーザー「『なるべく早く』は『即』」）。「30分後」「1時間後」は、その時からの終わり無しの幅（「すぐ」と
  同じ。2026-10-19 ユーザー：「いまは忙しいけど、できるだけ早くかけ直して」）。
- `UNSPECIFIED`：時刻をこちらに任せる答え（「いつでも」「そちらのご都合で」「あとで」「後ほど」だけ）・時刻を聞けなかった ⇒
  時刻の決まっていない約束（契約者が決めた再発信の方針の間隔。⚠️「あとで」と言われたお客様に、すぐかけ直さない）。
- `CONFIRM`：言い方は分かるが、いつかが1つに決まらない ⇒ **通話の中で確かめる**（会話＝CL-1）。例：「昼」（12時〜13時ごろとも、
  午後とも取れる。法人には12時〜13時は避けるのがマナー＝2026-10-19 ユーザー）・営業時間で午前か午後かが決まらない時刻・今日と
  同じ曜日だけ・日曜日の「来週」など・過ぎた時刻・いくつもの日にまたがる「3時」・「年明け」「年末」「数日後」のような幅の決まらない言葉。
- `UNREADABLE`：時刻の言葉として読めない（読み方の抜け）。
⇒ 判定は、確かめのないまま残った CONFIRM・UNREADABLE を推し量らずに「判定できない」にする（行は判定待ちで残し、直す担当へ修正依頼）。

**午前・午後**（2026-10-19 ユーザー：「通常午前3時に電話をかけてほしい人はいないので、営業時間の範囲だけ」。CL-1 の fill_ampm と同じ決まり）：
午前・午後の無い 1〜12時は、お客様の営業時間（設定 callee.business_hours。会社の初めは 9:00〜19:00。日をまたいでよい）に入るほうに
する（営業時間は [始まり, 終わり)：終わりの時刻ちょうどは入らない＝営業時間 9〜19時の「7時」は確かめる。見直し15）。両方入る・
どちらも入らないときだけ確かめる。「12時」は昼の12時。

言葉ごとの幅（`PERIODS`。2026-10-19 ユーザー「OK」。幅はお客様の指定＝2026-10-23 ユーザー「『朝』も時間帯が決まっているでしょ」）：朝・朝一＝〜10時・午前（中）＝〜12時・
午後一＝13〜14時・午後＝13時〜・夕方＝16〜18時・夜（晩）＝18〜20時。日の言葉の一覧は DESIGN.md 7-1d の表。
幅の端のうち、お客様が実際に口にした端（「4時以降」の4時・「3時まで」の3時・時刻ちょうど）を返す（`Reading.said`）。端の決まって
いない側（「朝」の0時・「午後」の24時）は、かける所（calling_hours.callee_range の1か所）がお客様指定の歯止め時間（設定
callee.designated_limit）で埋めて切る（口にした端はその外でも切らない）。お客様の営業時間（callee.business_hours）は、午前・午後の
読み分けだけに使う（2026-10-23 ユーザー）。
- 時刻は日本の時刻（日本標準時）で読む。結果は時差つきの時刻。
"""

from __future__ import annotations

import calendar as _cal
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from salescall.core.calling_hours import JST, callee_range, clock_seconds

# 読み方の版（読み方を変えたら上げる）。判定の版（judge.JUDGE_VERSION）に入る＝上げると、読めなかった「判定待ち」の行が判定し直される
VERSION = "13"   # 13：「〇時まで」（終わりだけを口にした幅）は、いま電話を続けられなかった理由で、かけてよい時間を決める（理由が分からなければ確かめる。2026-10-23 ユーザー：「かけ直さないといけない理由によるでしょ」）。12：その「23時以降なら」「朝6時まで」を確かめて「はい」なら、口にした端から1回分（callback.exact_slot）の幅（2026-10-23 ユーザー：「案B＆案A(確かめたうえで案A)」）。11：お客様が口にした端の反対側を「お客様指定の歯止め時間」で埋めると空になる幅（「23時以降なら」「朝6時まで」）は、通話の中で確かめる（見直し16b）。10：日を言わない約束の今日の回は、お客様が言った日（day_said。見直し16）
AT, WINDOW, UNSPECIFIED, CONFIRM, UNREADABLE = "at", "window", "unspecified", "confirm", "unreadable"
# 幅の中でお客様と話せなかったときの、次の回（お客様がその日を選んだ理由に合わせる。2026-10-19 ユーザー決定）：
# DAY＝時間の言葉（「夕方」「3時以降」）・日の種類だけ（「平日なら」）・「すぐ」⇒ 次の日の同じ幅／WEEK＝曜日・週末・週中・
# 週明け（「金曜なら」「週末なら」）⇒ 次の週の同じ日／"month:..."＝月の中の日・頃は、月の理由（月ぎめの業務・入金の間隔など。
# 会話の報告の callback.cycle＝month）があるときだけ ⇒ 次の月の同じ日・頃（ユーザー：「たまたま希望日が月末・月の初め・上旬に
# あたった場合が多い」）／None＝理由の分からない日（「13日」「明日」「3日後」「来週」）⇒ 時刻の決まっていない約束の方針
DAY_REPEAT, WEEK = "day", "week"
DAY = 24 * 3600
H = 3600
# お客様の営業時間・お客様指定の歯止め時間・1回分の長さの初めの値は、設定の1か所（データベース）にだけある（ここに写しを持たない。
# 読むのは gate.callee_settings。⛔ 見直し17b）。

# 言葉ごとの1日の中の幅（秒。終わりは含まない）。端の決まっていない側（朝・午前の始まり＝0時、午後の終わり＝24時）は、かける所が
# お客様指定の歯止め時間で埋める（calling_hours.callee_range）。
# 依頼者ごとの設定（callback.period_*。表の作り 0088）を `periods_from` で、言葉の表にする（判定が設定を渡す）。下は初めの値
PERIOD_SETTINGS = {"morning": "callback.period_morning", "forenoon": "callback.period_forenoon",
                   "afternoon": "callback.period_afternoon", "evening": "callback.period_evening", "night": "callback.period_night"}
DEFAULT_PERIODS = {"morning": (0, 10 * H), "forenoon": (0, 12 * H), "afternoon": (13 * H, DAY), "evening": (16 * H, 18 * H),
                   "night": (18 * H, 20 * H)}


def periods_from(ranges: dict[str, tuple[int, int]]) -> dict[str, tuple[int, int]]:
    """設定の幅（morning など → 秒の (始まり, 終わり)）を、言葉 → 幅の表にする。午後一＝午後の始まりから1時間。"""
    a = ranges["afternoon"]
    words = {"morning": ("朝一", "朝イチ", "朝いち", "朝"), "forenoon": ("午前中", "午前"), "afternoon": ("午後",),
             "evening": ("夕方", "夕刻"), "night": ("夜", "晩")}
    out = {w: ranges[k] for k, ws in words.items() for w in ws}
    out.update({w: (a[0], min(a[0] + H, a[1])) for w in ("午後一", "午後イチ", "午後いち")})
    return out


def range_seconds(value: list[str]) -> tuple[int, int]:
    """設定の時刻の範囲（["16:00", "18:00"]）を秒にする。終わりが始まり以前なら、その日の終わりまで（日をまたいでは読まない）。"""
    s0, s1 = (clock_seconds(v) for v in value)
    return s0, s1 if s1 > s0 else DAY


PERIODS = periods_from(DEFAULT_PERIODS)
# 確かめが要る言葉（いつかが1つに決まらない）
_AMBIGUOUS_TIME = ("お昼前", "昼前", "お昼過ぎ", "お昼すぎ", "昼過ぎ", "昼すぎ", "お昼頃", "お昼ごろ", "昼頃", "昼ごろ", "お昼", "昼間",
                   "昼", "夜遅く", "遅く", "深夜", "夜中", "明け方", "早朝", "定時後", "終業後", "仕事終わり", "仕事のあと")
_AMBIGUOUS_DAY = ("年明け", "年始", "年末", "年度末", "年度初め", "数日後", "数日中", "近日", "そのうち", "近いうちに", "近いうち",
                  "週の後半", "週の前半", "月の後半", "月の前半", "連休明け", "お盆明け", "盆明け")

# すぐの答え・時刻を任せる答え（言葉のあとに「で」「大丈夫です」などが付いてもよい）
_OK_TAIL = r"(?:で|に)?(?:よろしい|大丈夫|結構|いい|構いません|かまいません|お願いします|OK)?(?:です)?"
_ASAP_WORDS = r"(?:今すぐ|いますぐ|すぐ|なるべく早く|なるべくはやく|できるだけ早く|出来るだけ早く|早めに|はやめに|至急)"
_ANY_WORDS = r"(?:いつでも|何時でも|そちらのご都合|そちらの都合|ご都合のよい時|ご都合のいい時)"
_LATER_WORDS = r"(?:あとで|後で|後ほど|のちほど|また今度|改めて)"
_ASAP = re.compile(_ASAP_WORDS + _OK_TAIL)
_UNSPECIFIED = re.compile(r"(?:" + _ANY_WORDS + "|" + _LATER_WORDS + ")" + _OK_TAIL)
_WHOLE = re.compile(r"(?:" + _ANY_WORDS + "|" + _ASAP_WORDS + ")" + _OK_TAIL)   # 日のあとの「いつでも」など＝その日のいつでも
_JOIN = re.compile(r"(?:の|なら|であれば|でしたら|だったら|でも|は|、)*")
# うしろに付いてよい言葉（「でしたら大丈夫です」なども。条件の言葉＋受け入れの言葉）
# （「3時がいいです」「午前中ならいつでも」も＝2026-10-19 の見直しで足した）
_TAIL = re.compile(r"(?:に|で|が|には|お願いします|でお願いします)*"
                   r"(?:(?:なら|であれば|でしたら|だったら)?(?:大丈夫|いつでも大丈夫|いつでも|結構|OK|構いません|かまいません|"
                   r"いい|よろしい)?(?:です)?)")
# 時間の言葉のあとの「ごろ」「くらい」（「夕方ごろ」＝夕方、「正午ごろ」＝正午。「30分後くらい」は _LATER_CLOCK の中）
_ABOUT = re.compile(r"頃|ごろ|くらい|ぐらい|位")

_REL_DAYS = {"今日": 0, "本日": 0, "きょう": 0, "明日": 1, "あした": 1, "あす": 1, "明後日": 2, "あさって": 2,
             "明々後日": 3, "明明後日": 3, "しあさって": 3}
_WEEKDAYS = "月火水木金土日"
_WEEK_OFFSET = {"今週": 0, "来週": 1, "再来週": 2}
_MONTH_OFFSET = {"今月": 0, "来月": 1, "再来月": 2}
_REL = re.compile(r"今日中|本日中|今日|本日|きょう|明々後日|明明後日|しあさって|明後日|あさって|明日中|明日|あした|あす")
_REL_PERIOD = {"今朝": (0, "朝"), "今晩": (0, "夜"), "今夜": (0, "夜"), "明朝": (1, "朝"), "明晩": (1, "夜")}
_AFTER = re.compile(r"(?P<n>\d{1,2})(?P<unit>日|週間|か月|ヶ月|ヵ月|カ月|箇月)後")
_RANGE_AFTER = re.compile(r"(?P<a>\d{1,2})(?:〜|～|-|か|から|、|,|・)(?P<b>\d{1,2})(?P<unit>日|週間)後")
_LATER_CLOCK = re.compile(r"(?P<n>\d{1,3})(?P<unit>分|時間)(?P<half>半)?後(?:頃|ごろ|くらい|ぐらい|位)?")
_MONTH_PART = re.compile(r"(?:(?P<mrel>今月|来月|再来月)|(?P<mon>\d{1,2})月)?の?"
                         r"(?P<part>上旬|中旬|下旬|中頃|中ごろ|半ば|初め|はじめ|頭|月初め|月初|月末|末|月の初め|月のはじめ|月の頭|"
                         r"月の終わり|月終わり)")
_MONTH = re.compile(r"(?:(?P<mrel>今月|来月|再来月)|(?P<mon>\d{1,2})月)(?P<within>中|いっぱい)?(?!\d)(?!の\d{1,2}日)")
_YEAR = re.compile(r"(?P<y>年内|今年中|今年いっぱい|来年)")
# 月の前に付いた年（「来年の1月5日」「2027年1月」）。年があれば、その年の中だけで探す
_YEAR_OF = re.compile(r"(?:(?P<y>\d{4})年|(?P<rel>今年|来年|再来年))の?(?=\d{1,2}月)")
_YEAR_OFFSET = {"今年": 0, "来年": 1, "再来年": 2}
# 年の無い「○月」「○月○日」を来年と読むのは、今日から半年の内だけ。それより先は、過ぎた日の言い違いか来年のことかが決まらない
# ⇒ 確かめる（⚠️ 前は昨日の日付を、1年後の同じ日と読んだ＝2026-10-19 の見直し）。日だけ（「13日」）は、今月か来月だけ
_FAR = timedelta(days=183)
_WEEKEND = re.compile(r"(?:(?P<week>今週|来週|再来週)(?:の?週末|の?土日|末)|週末|土日)(?!祝)")
_MIDWEEK = re.compile(r"(?:(?P<week>今週|来週|再来週)の?(?:週中|週の半ば|週半ば|週の中ごろ|週の中頃|半ば|中ごろ|中頃)"
                      r"|週中|週の半ば|週半ば|週の中ごろ|週の中頃)")
_WEEK_START = re.compile(r"週明け|週初め|週のはじめ|週の初め")
_WEEK = re.compile(r"(?P<week>今週|来週|再来週)(?P<within>中|いっぱい)?(?!の?[月火水木金土日]曜)(?!の?週)(?!末)(?!半ば)")
_DATE = re.compile(r"(?:(?P<mrel>今月|来月|再来月)の?|(?P<mon>\d{1,2})月)?(?P<dom>\d{1,2})日")
_WDAY = re.compile(r"(?P<week>今週|来週|再来週)?の?(?P<wday>[月火水木金土日])曜日?")
_WDAY_CHECK = re.compile(r"\((?P<p>[月火水木金土日])(?:曜日?)?\)|の?(?P<w>[月火水木金土日])曜日?")
# 日の種類（その中の日だけ。calling_hours の曜日・祝日の鍵）
_DAY_KINDS = [("土日祝日", ("sat", "sun", "holiday")), ("土日祝", ("sat", "sun", "holiday")),
              ("休日", ("sat", "sun", "holiday")), ("お休みの日", ("sat", "sun", "holiday")), ("休みの日", ("sat", "sun", "holiday")),
              ("平日", ("mon", "tue", "wed", "thu", "fri"))]
_HOUR = re.compile(r"(?P<part>午前|午後|朝|夕方|夕刻|夜|晩)?の?(?P<hour>\d{1,2})時(?:(?P<min>\d{1,2})分|(?P<half>半))?")
_HOUR_TO = re.compile(r"(?:から|〜|～|-)(?P<part>午前|午後|朝|夕方|夕刻|夜|晩)?の?(?P<hour>\d{1,2})時(?:(?P<min>\d{1,2})分|"
                      r"(?P<half>半))?(?:まで|の間|の間に|までの間)?")
_HOUR_PARTS = {"午前": (range(0, 12), 0), "午後": (range(0, 12), 12), "朝": (range(4, 12), 0), "夕方": (range(3, 8), 12),
               "夕刻": (range(3, 8), 12), "夜": (range(6, 12), 12), "晩": (range(6, 12), 12)}
_POINT_TAIL = re.compile(r"頃|ごろ|くらい|ぐらい|位|ちょうど|前後")
_BAND_TAIL = re.compile(r"台")
_FROM_TAIL = re.compile(r"以降|以後|過ぎ|すぎ|から(?!\d)|より後|より後ろ")
_UNTIL_TAIL = re.compile(r"までに|まで|より前|前")
_KANJI = {"〇": 0, "零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


@dataclass(frozen=True)
class Reading:
    kind: str                            # AT / WINDOW / UNSPECIFIED / CONFIRM / UNREADABLE
    start: datetime | None = None        # AT：その時刻／WINDOW：幅の始まり（いまより前にしない）
    end: datetime | None = None          # WINDOW：幅の終わり（含まない。None＝終わり無し）
    daily: tuple[int, int] | None = None  # WINDOW：1日の中の幅（秒）。時間の言葉があるときだけ
    why: str | None = None               # CONFIRM・UNREADABLE の理由（日本語）
    days: tuple[str, ...] | None = None  # WINDOW：かけてよい日の種類（「平日」「土日祝」など。曜日・祝日の鍵）
    repeat: str | None = None            # WINDOW：話せなかったときの次の回（DAY_REPEAT・WEEK・"month:..."・None。`next_occurrence`）
    since: datetime | None = None        # WINDOW：その回の本当の始まり（start は、いまより前にしないので、回の途中で言われると遅い。
    #                                      次の回は、ここから数える＝「週中」を水曜に言われても、次の回は火曜から）
    said: tuple[bool, bool] = (False, False)   # WINDOW：1日の中の幅（daily）の始まり・終わりを、お客様が実際に言ったか（「4時以降」＝
    #                                      (True, False)・「3時まで」＝(False, True)・「3時から5時」「3時台」＝(True, True)・「夕方」「朝」など
    #                                      言葉の表から来た端＝False）。口にした端は、お客様指定の歯止め時間の外でも切らない（calling_hours.
    #                                      callee_range の1か所。2026-10-22 ユーザー。⛔ 見直し15：前は「朝なら」で0時にかけえた）
    day_said: bool = False               # AT・WINDOW：お客様がその日（日・曜日・日の種類）を言った（依頼者の休みの日でもかける根拠）。
    #                                      日を言わない「3時なら」「夕方なら」「すぐ」も、いま話している今日の回は言った日（見直し16：
    #                                      依頼者の休みの日に約束の電話で話していても、お客様は今日のつもり）。次の日からの回は、
    #                                      言っていない（依頼者のかける日だけ＝next_action の Window.later）

    @property
    def exact(self) -> bool:
        """お客様が自分で時刻を指定したか（その時刻を含むNG時間の枠でもかけてよい根拠）。"""
        return self.kind == AT


class _Ambiguous(Exception):
    """確かめが要る（理由）。"""


def _confirm(why: str) -> Reading:
    return Reading(CONFIRM, why=why)


def _unreadable(why: str) -> Reading:
    return Reading(UNREADABLE, why=why)


def _kanji_number(m: re.Match) -> str:
    """「十五」「二十」「三十一」のような漢数字（99まで）を数字にする。十の無い2文字以上は、〇を含む位取り（「二〇二七年」）だけ
    並べて数字にし、続いた2つの数（「二三日」「四五日」＝2〜3日・4〜5日）は範囲にする。ほかはそのまま残す（読めない）。
    ⚠️ 前は十の無い漢数字を全部つなげ、「二三日後」を23日後と読んだ（2026-10-19 の見直し）。"""
    s = m.group()
    if "十" not in s:
        digits = [_KANJI[c] for c in s]
        if len(digits) == 1 or any(c in "〇零" for c in s):
            return "".join(map(str, digits))
        if len(digits) == 2 and digits[1] == digits[0] + 1:
            return f"{digits[0]}〜{digits[1]}"
        return s
    tens, _, ones = s.partition("十")
    if len(tens) > 1 or len(ones) > 1 or "十" in ones:
        return s                                   # 決まりに無い書き方（「十十」など）：そのまま残す（読めない）
    return str((_KANJI[tens] if tens else 1) * 10 + (_KANJI[ones] if ones else 0))


def _clean(text: str) -> str:
    t = unicodedata.normalize("NFKC", text)
    # 消すのは、空白・文の終わりだけ。区切り（, . ・）は残す：消すと「1・2時間後」が12時間後、「1,2日」が12日になる
    # （2026-10-19 の見直し）。区切りの残った言い方は読めない（勝手に決めない）。範囲の区切りとして読むのは、決まった言い方の中だけ
    t = re.sub(r"[\s。!！]", "", t).replace("ケ月", "ヶ月")
    # 漢数字は、数を表すところ（うしろに日・時・分・月・週・か月・年・〜・台・半が続く）だけ数字にする（「朝一」「午後一」は残す）
    return re.sub(r"[〇零一二三四五六七八九十]+(?=[日時分月週かヶヵカ箇年〜～\-台半])", _kanji_number, t)


def _month_add(d: date, n: int) -> tuple[int, int]:
    k = d.year * 12 + d.month - 1 + n
    return k // 12, k % 12 + 1


def _month_range(year: int, month: int, part: str | None) -> tuple[date, date]:
    last = _cal.monthrange(year, month)[1]
    # 2026-10-19 ユーザー：月末＝月の最後の3日・月の初め／頭＝1〜3日。上旬・中旬・下旬はふつうの意味
    lo, hi = {None: (1, last), "上旬": (1, 10), "中旬": (11, 20), "中頃": (11, 20), "中ごろ": (11, 20), "半ば": (11, 20),
              "下旬": (21, last), "初め": (1, 3), "はじめ": (1, 3), "頭": (1, 3), "月初め": (1, 3), "月初": (1, 3),
              "月の初め": (1, 3), "月のはじめ": (1, 3), "月の頭": (1, 3),
              "月末": (last - 2, last), "末": (last - 2, last), "月の終わり": (last - 2, last), "月終わり": (last - 2, last)}[part]
    return date(year, month, lo), date(year, month, hi)


def _years(today: date, year: int | None) -> tuple[int, ...]:
    return (today.year, today.year + 1) if year is None else (year,)


def _named_month(today: date, mrel: str | None, mon: str | None, part: str | None,
                 year: int | None = None) -> tuple[date, date]:
    """月の言葉の日の範囲（今日以後。「11月」は今日以後でいちばん近いその月。年が付いていれば、その年）。"""
    if mrel is not None:
        return _month_range(*_month_add(today, _MONTH_OFFSET[mrel]), part)
    if mon is not None:
        m = int(mon)
        if not 1 <= m <= 12:
            raise _Ambiguous("月が読めません")
        for y in _years(today, year):
            r = _month_range(y, m, part)
            if r[1] >= today:
                if year is None and r[0] > today + _FAR:
                    raise _Ambiguous("過ぎた月の言い違いか、来年のことか決まりません")
                return r
        raise _Ambiguous("過ぎた月です")
    lo, hi = _month_range(today.year, today.month, part)   # 月の無い「上旬」「月末」＝今月
    if hi < today:
        raise _Ambiguous("今月のその頃はもう過ぎています（来月のことか、決まりません）")
    return lo, hi


def _week_monday(today: date, week: str) -> date:
    if today.weekday() == 6:
        raise _Ambiguous("今日は日曜日なので、週の始まり（月曜・日曜）で「今週・来週」の日が変わります")
    return today - timedelta(days=today.weekday()) + timedelta(weeks=_WEEK_OFFSET[week])


def _months_later(today: date, n: int) -> date:
    y, m = _month_add(today, n)
    return date(y, m, min(today.day, _cal.monthrange(y, m)[1]))


def _day_part(t: str, today: date) -> tuple[tuple[date, date] | None, str | None, int, str | None]:
    """頭の日の言葉を読む：（日の範囲〔両端を含む〕, その日に付いた時間の言葉〔「今晩」の「夜」など〕, 読んだ長さ, 日の選び方）。
    日の選び方（`_repeat` が、話せなかったときの次の回を決めるのに使う）：WEEK＝曜日・週末・週中・週明け（その曜日なら出られる）／
    "month:..."＝月の中の日・頃（月の理由があるときだけ次の月）／None＝理由の分からない日（「13日」「明日」「3日後」「来週」）。
    日の言葉が無ければ (None, None, 0, None)。"""
    for word in _AMBIGUOUS_DAY:
        if t.startswith(word):
            raise _Ambiguous(f"「{word}」は、いつのことか1つに決まりません")
    for word, (n, period) in _REL_PERIOD.items():
        if t.startswith(word):
            d = today + timedelta(days=n)
            return (d, d), period, len(word), None
    if m := _REL.match(t):
        d = today + timedelta(days=_REL_DAYS[m.group().removesuffix("中")])
        days, end = _check_wday(t, m.end(), (d, d))
        return days, None, end, None
    if m := _RANGE_AFTER.match(t):
        a, b, per = int(m.group("a")), int(m.group("b")), 7 if m.group("unit") == "週間" else 1
        if not 1 <= a < b:
            raise _Ambiguous("「〜日後」の範囲が読めません")
        return (today + timedelta(days=a * per), today + timedelta(days=b * per)), None, m.end(), None
    if m := _AFTER.match(t):
        n, unit = int(m.group("n")), m.group("unit")
        if n < 1:
            raise _Ambiguous("「〜後」が読めません")
        d = today + timedelta(days=n) if unit == "日" else today + timedelta(weeks=n) if unit == "週間" \
            else _months_later(today, n)
        return (d, d), None, m.end(), None
    year, pos = None, 0
    if m := _YEAR_OF.match(t):
        year = int(m.group("y")) if m.group("y") else today.year + _YEAR_OFFSET[m.group("rel")]
        pos = m.end()
    elif m := _YEAR.match(t):
        if m.group("y") == "来年":
            return (date(today.year + 1, 1, 1), date(today.year + 1, 12, 31)), None, m.end(), None
        return (today, date(today.year, 12, 31)), None, m.end(), None
    if m := _MONTH_PART.match(t, pos):
        return (_named_month(today, m.group("mrel"), m.group("mon"), m.group("part"), year), None, m.end(),
                f"month:part:{m.group('part')}")
    if m := _MONTH.match(t, pos):
        if m.group("mon") is not None or year is None:   # 年のあとは、数の月だけ（「来年の来月」は読まない）
            return _named_month(today, m.group("mrel"), m.group("mon"), None, year), None, m.end(), "month:all"
    if m := _MIDWEEK.match(t):   # 週中・週の半ば＝火〜木（✅ 2026-10-19 ユーザー「OK」）
        monday = _week_monday(today, m.group("week") or "今週")
        lo, hi = monday + timedelta(days=1), monday + timedelta(days=3)
        if hi < today:
            raise _Ambiguous("今週の半ばはもう過ぎています（来週のことか、決まりません）")
        return (lo, hi), None, m.end(), WEEK   # 始まりは読みの本体が、いまより前にしない（回の本当の始まりは残す）
    if m := _WEEK_START.match(t):  # 週明け＝明日以後でいちばん近い月曜日の1日（✅ 2026-10-19 ユーザー「OK」）
        d = today + timedelta(days=(7 - today.weekday()) % 7 or 7)
        return (d, d), None, m.end(), WEEK
    if m := _WEEKEND.match(t):
        if m.group("week") is None and today.weekday() == 6:
            raise _Ambiguous("今日は日曜日なので、今日のことか来週末か決まりません")
        monday = _week_monday(today, m.group("week") or "今週")
        return (monday + timedelta(days=5), monday + timedelta(days=6)), None, m.end(), WEEK   # 同上
    if m := _WEEK.match(t):
        monday = _week_monday(today, m.group("week"))
        return (max(monday, today), monday + timedelta(days=6)), None, m.end(), None
    if (m := _DATE.match(t, pos)) and (m.group("mon") is not None or year is None):
        dom, mon = int(m.group("dom")), m.group("mon")
        if m.group("mrel") is not None:   # 「来月3日」「今月の20日」
            y, mo = _month_add(today, _MONTH_OFFSET[m.group("mrel")])
            try:
                c = date(y, mo, dom)
            except ValueError:
                raise _Ambiguous("その月に無い日です") from None
            if c < today:
                raise _Ambiguous("過ぎた日です")
            days, end = _check_wday(t, m.end(), (c, c))
            return days, None, end, f"month:day:{dom}"
        found = None
        # 今日以後でいちばん近いその日（月が無ければ今月か来月、あれば今年か来年。年が付いていれば、その年）
        for y, mo in ([_month_add(today, k) for k in range(2)] if mon is None
                      else [(y, int(mon)) for y in _years(today, year)]):
            try:
                c = date(y, mo, dom)
            except ValueError:
                # その月に無い日（11月の「31日」・2月の「30日」）：次の月へ送らない（⚠️ 前は11月を飛ばして12月31日と読んだ＝
                # 2026-10-19 の見直し）。月の終わりのつもりか、決まらない
                raise _Ambiguous("その月に無い日です") from None
            if c >= today:
                found = c
                break
        if found is None:
            raise _Ambiguous("その日は、今月・来月に無いか、過ぎています" if mon is None else "過ぎた日です")
        if mon is not None and year is None and found > today + _FAR:
            raise _Ambiguous("過ぎた日の言い違いか、来年のことか決まりません")
        days, end = _check_wday(t, m.end(), (found, found))
        return days, None, end, f"month:day:{dom}"
    if year is not None:
        raise _Ambiguous("年のあとの月日が読めません")
    if m := _WDAY.match(t):
        week, target = m.group("week"), _WEEKDAYS.index(m.group("wday"))
        if week is None:
            if target == today.weekday():
                raise _Ambiguous("今日と同じ曜日だけでは、今日か来週かが決まりません")
            d = today + timedelta(days=(target - today.weekday()) % 7)
        else:
            if target == 6:
                raise _Ambiguous("日曜日は、週の始まり（月曜・日曜）で日が変わります")
            d = _week_monday(today, week) + timedelta(days=target)
            if d < today:
                raise _Ambiguous("過ぎた曜日です")
        return (d, d), None, m.end(), WEEK
    return None, None, 0, None


def _day_kinds(t: str, pos: int) -> tuple[tuple[str, ...] | None, int]:
    """日の種類の言葉（「平日」「土日祝」など）を読む：（鍵の並び, 読んだ位置）。"""
    for word, keys in _DAY_KINDS:
        if t.startswith(word, pos):
            return keys, pos + len(word)
    return None, pos


def _check_wday(t: str, pos: int, days: tuple[date, date]) -> tuple[tuple[date, date], int]:
    """日のあとに曜日が添えてあれば（「20日（火）」「明日の木曜」）、合っているかを確かめる。"""
    m = _WDAY_CHECK.match(t, pos)
    if m is None:
        return days, pos
    if _WEEKDAYS.index(m.group("p") or m.group("w")) != days[0].weekday():
        raise _Ambiguous("日と曜日が合いません")
    return days, m.end()


def _in_hours(sec: int, hours: tuple[int, int]) -> bool:
    """その時刻（0時からの秒）が営業時間 [始まり, 終わり) に入るか（1か所。日をまたいでよい）。終わりの時刻ちょうどは入らない：
    営業時間 9〜19時で「7時」は、7時（始まりより前）も19時（終わりちょうど）も入らない ⇒ 午前・午後を決めずに通話の中で確かめる
    （⛔ 2026-10-22 の見直し15：前は時の単位で終わりの時も含め、「明日の7時」を19時と読み、営業の終わった時刻に依頼者の時間帯の
    外でもかけた。CL-1 の fill_ampm にも同じ直しを頼む）。"""
    open_, close = hours
    return open_ <= sec < close if open_ <= close else (sec >= open_ or sec < close)


def _hour(m: re.Match, business: tuple[int, int]) -> int:
    """何時何分（その日の0時からの秒）。午前か午後かが決まらなければ _Ambiguous。"""
    h, part = int(m.group("hour")), m.group("part")
    minute = 30 if m.group("half") else int(m.group("min") or 0)
    if minute > 59:
        raise _Ambiguous("分が読めません")
    if part is not None:
        hours, add = _HOUR_PARTS[part]
        if h not in hours:
            raise _Ambiguous(f"「{part}」と{h}時が合いません（12時ちょうどは、午前・午後のどちらか決まりません）")
        return (h + add) * H + minute * 60
    if 13 <= h <= 23:
        return h * H + minute * 60
    if not 1 <= h <= 12:
        raise _Ambiguous("時刻が読めません")
    am, pm = (0, 12) if h == 12 else (h, h + 12)
    am_in, pm_in = _in_hours(am * H + minute * 60, business), _in_hours(pm * H + minute * 60, business)
    if am_in == pm_in:
        raise _Ambiguous("営業時間からは、午前か午後かが決まりません")
    return (am if am_in else pm) * H + minute * 60


def _time_part(t: str, pos: int, business: tuple[int, int],
               periods: dict[str, tuple[int, int]] = PERIODS) -> tuple[str, int, int, int, tuple[bool, bool]]:
    """時刻の言葉を読む：（種類 point／window／whole, 始まりの秒, 終わりの秒, 読んだ位置, 始まり・終わりをお客様が言ったか
    ＝`Reading.said`）。読めなければ _Ambiguous か ValueError。"""
    rest = t[pos:]
    if _WHOLE.fullmatch(rest):
        return "whole", 0, DAY, len(t), NOT_SAID
    if _TAIL.fullmatch(rest):
        return "whole", 0, DAY, pos, NOT_SAID
    for word in _AMBIGUOUS_TIME:
        if rest.startswith(word):
            raise _Ambiguous(f"「{word}」は、いつのことか1つに決まりません（昼は、12時〜13時ごろとも午後とも取れます。"
                             "法人には12時〜13時は避けるのがマナー）")
    if rest.startswith("正午"):
        end = pos + 2
        if a := _ABOUT.match(t, end):
            end = a.end()
        return "point", 12 * H, 12 * H, end, BOTH_SAID
    m = _HOUR.match(t, pos)
    if m is None:
        for word in sorted(periods, key=len, reverse=True):
            if rest.startswith(word):
                end = pos + len(word)
                if t.startswith("中", end):
                    end += 1
                s0, s1 = periods[word]
                if f := _FROM_TAIL.match(t, end):
                    if f.group() in ("過ぎ", "すぎ"):
                        raise _Ambiguous(f"「{word}過ぎ」は、いつからか1つに決まりません")
                    return "window", s0, DAY, f.end(), NOT_SAID
                if _UNTIL_TAIL.match(t, end):
                    raise _Ambiguous(f"「{word}まで」は、その初めまでか終わりまでか決まりません")
                if a := _ABOUT.match(t, end):
                    end = a.end()
                return "window", s0, s1, end, NOT_SAID
        raise ValueError(f"時刻がありません（「{rest}」）")
    sec, end = _hour(m, business), m.end()
    if n := _HOUR_TO.match(t, end):
        until = _hour(n, business)
        if until <= sec:
            raise _Ambiguous("「〜から〜まで」の終わりが始まりより前です")
        return "window", sec, until, n.end(), BOTH_SAID
    if m2 := _BAND_TAIL.match(t, end):
        return "window", sec, min(sec + H, DAY), m2.end(), BOTH_SAID
    if m2 := _POINT_TAIL.match(t, end):
        return "point", sec, sec, m2.end(), BOTH_SAID
    if m2 := _FROM_TAIL.match(t, end):
        return "window", sec, DAY, m2.end(), (True, False)
    if m2 := _UNTIL_TAIL.match(t, end):
        if m2.group() == "前" and m.group("min"):
            # 「3時10分前」は、2時50分（3時の10分前）とも、3時10分より前とも取れる（2026-10-19 の見直し）
            raise _Ambiguous("「〜時〜分前」は、その時刻の〜分前か、その時刻より前か決まりません")
        return "window", 0, sec, m2.end(), (False, True)
    return "point", sec, sec, end, BOTH_SAID


NOT_SAID, BOTH_SAID = (False, False), (True, True)   # 1日の中の幅の端を、お客様が言ったか（`Reading.said`）
UNTIL_SAID = (False, True)                           # 終わりだけを口にした幅（「3時まで」「3時より前」）


def _at(d: date, sec: int) -> datetime:
    return datetime.combine(d, time(), tzinfo=JST) + timedelta(seconds=sec)


def read(text: str | None, now: datetime, *, business_hours: tuple[int, int], limit: tuple[int, int], slot: int,
         until: str | None, periods: dict[str, tuple[int, int]] = PERIODS, monthly: bool = False,
         beyond_ok: bool = False) -> Reading:
    """お客様が言った時刻の言葉を読む。now＝その通話の時刻（「明日」などの起点。過ぎた時刻は確かめが要る）。
    business_hours＝お客様の営業時間（0時からの秒の (始まり, 終わり)。設定 callee.business_hours。午前・午後を決める）。
    periods＝言葉ごとの幅（依頼者ごとの設定 callback.period_* を `periods_from` で表にしたもの）。
    monthly＝お客様が月の理由（月ぎめの業務・入金の間隔など）を言った（会話の報告の callback.cycle＝month）。
    limit＝お客様指定の歯止め時間（設定 callee.designated_limit。幅の端の決まっていない側を埋めて切る＝calling_hours.callee_range）。
    埋めると幅が空になる言い方（口にした端が歯止め時間の外：「23時以降なら」「明日の朝6時まで」）は、通話の中で確かめる（CONFIRM）。
    確かめて「はい」と言われた（会話の報告の callback.beyond_limit_ok＝beyond_ok）ときは、口にした端から1回分（slot＝設定
    callback.exact_slot。時刻ちょうどの約束と同じ）の幅（2026-10-23 ユーザー：「案B＆案A(確かめたうえで案A)」。決めるのは
    calling_hours.callee_range の1か所。⛔ 見直し16b：前は空の幅が時刻の決まっていない約束になり、お客様の言ったことと反対の
    時刻＝依頼者のいつもの時間にかけた）。言葉が無い（時刻を聞けなかった）ときは UNSPECIFIED。
    until＝「〇時まで」（終わりだけを口にした幅）のときの、かけてよい時間の決め方（calling_hours.UNTIL_RULES のどれか。いま電話を
    続けられなかった理由から CalleeSettings.until_rule が決める。分からなければ None）。now＝今から〇時まで・after＝〇時から
    （「〇時以降」と同じ）・slot_after＝〇時から1回分（slot）。None なら決めずに確かめる（CONFIRM。2026-10-23 ユーザー：「電話を
    切った理由があるから、電話を続けられなかったと考えるべきでしょ。その理由を確かめないと、何時から3時までの何時が決められない
    でしょ！」。⛔ 前は理由を見ずに、いつも「今から〇時まで」にしていた）。"""
    if text is None or not text.strip():
        return Reading(UNSPECIFIED)
    t = _clean(text)
    local = now.astimezone(JST)
    if _UNSPECIFIED.fullmatch(t):
        return Reading(UNSPECIFIED)
    if _ASAP.fullmatch(t):
        return Reading(WINDOW, local, None, repeat=DAY_REPEAT, day_said=True)
    if (m := _LATER_CLOCK.match(t)) and _TAIL.fullmatch(t[m.end():]):   # 「30分後」「1時間後」「1時間半後」
        n = int(m.group("n"))
        minutes = n if m.group("unit") == "分" else n * 60 + (30 if m.group("half") else 0)
        if minutes < 1:
            return _confirm("「〜後」が読めません")
        # 「いまは忙しいが、できるだけ早くかけ直して」（2026-10-19 ユーザー）：時刻ちょうどの約束ではなく、その時からの「すぐ」と
        # 同じ（終わり無しの幅。話せなければ「すぐ」と同じ決まりでかけ直す。NG時間は守る）
        return Reading(WINDOW, local + timedelta(minutes=minutes), None, repeat=DAY_REPEAT, day_said=True)
    try:
        days, period, pos, cycle = _day_part(t, local.date())
        pos = _JOIN.match(t, pos).end()
        kinds, pos = _day_kinds(t, pos)
        pos = _JOIN.match(t, pos).end()
        if period is not None and not _TAIL.fullmatch(t[pos:]):
            # 「今晩8時」「明朝9時」：日に付いた時間の言葉を、うしろの時刻の頭に付けて読む（「夜8時」＝20時）
            kind, s0, s1, end, said = _time_part(period + t[pos:], 0, business_hours, periods)
            end += pos - len(period)
        elif period is not None:                      # 「今晩」「明朝」だけ
            (s0, s1), kind, end, said = periods[period], "window", pos, NOT_SAID
        else:
            kind, s0, s1, end, said = _time_part(t, pos, business_hours, periods)
    except _Ambiguous as e:
        return _confirm(str(e))
    except ValueError as e:
        return _unreadable(str(e))
    if not _TAIL.fullmatch(t[end:]):
        return _unreadable(f"読めない言葉があります（「{t[end:]}」）")
    # お客様がその日（日・曜日・日の種類）を言った。日を言わなければ今日の回＝いま話している日なので、言った日（見直し16）
    day_said = True
    if days is None:
        if kind == "whole" and kinds is None:
            return _unreadable("日も時刻もありません")
        if kinds is None:
            days = (local.date(), local.date())       # 日が無ければ今日
    if kind == "point":
        if days is None or days[0] != days[1] or kinds is not None:
            return _confirm("いくつもの日にまたがっていて、どの日の時刻か決まりません")
        at = _at(days[0], s0)
        if at <= local:
            return _confirm("通話の時刻より前の時刻です（今日のことか、明日以降か決まりません）")
        return Reading(AT, at, day_said=day_said)
    if kind == "window" and s0 >= s1:
        # 「午前0時まで」など、幅が空になる言い方（⚠️ 前は始まりと終わりが同じ幅を返し、次の予定の決まり＝0086 に当たりえた）
        return _confirm("幅が空になる言い方です（いつまでか決まりません）")
    daily = (s0, s1) if kind == "window" else None
    said = said if daily is not None else NOT_SAID
    if daily is not None and said == UNTIL_SAID:      # 「〇時まで」：いま電話を続けられなかった理由で、かけてよい時間を決める
        if until is None:
            return _confirm("「〇時まで」は、いま電話を続けられない理由で、かけてよい時間が変わります（別の番号・別の方へ＝今から・"
                            "それまでは出られない＝その時刻から・それまでに確認しておく＝その時刻から1回分）。理由か「何時ごろでしたら"
                            "よろしいでしょうか」を伺ってください")
        if until == "after":                           # 口にした時刻から（終わりは言葉で決まっていない＝歯止め時間で埋める）
            daily, said = (s1, DAY), (True, False)
        elif until == "slot_after":                    # 口にした時刻から1回分（両方の端をお客様の言葉から決めた幅）
            daily, said = (s1, min(s1 + slot, DAY)), BOTH_SAID
        elif until != "now":
            raise ValueError(f"「〇時まで」の決め方が違います：{until!r}")
        s0, s1 = daily
    if daily is not None and callee_range(daily, said, limit) is None:   # かけてよい時間が空になる（判断は callee_range の1か所）
        slotted = callee_range(daily, said, limit, slot) if beyond_ok else None
        if slotted is None:
            return _confirm("お客様が言った時刻が、お客様指定の歯止め時間の外です（夜遅く・朝早く）。その時刻でよいかを確かめてください"
                            "（「はい」なら、その時刻から1回分かける）")
        daily, said = slotted, (True, True)            # 確かめ済み：口にした端から1回分（両方の端をお客様が認めた幅）
        s0, s1 = daily
    if days is None:                                  # 「平日の夕方」「土日祝ならいつでも」：日の種類だけ（終わり無し）
        return Reading(WINDOW, local, None, daily, days=kinds, repeat=DAY_REPEAT, said=said, day_said=day_said)
    start, end_at = _at(days[0], s0), _at(days[1], s1)
    if end_at <= local:
        return _confirm("その頃はもう過ぎています（今日のことか、明日以降か決まりません）")
    return Reading(WINDOW, max(start, local), end_at, daily, days=kinds, repeat=_repeat(cycle, daily, monthly), since=start,
                   said=said, day_said=day_said)


def _repeat(cycle: str | None, daily: tuple[int, int] | None, monthly: bool) -> str | None:
    """話せなかったときの次の回の決め方（1か所。お客様がその日を選んだ理由に合わせる＝DAY_REPEAT の上の説明）。
    曜日・週末は時間の言葉より強い（「金曜の夕方」＝次の金曜の夕方）。"""
    if cycle == WEEK:
        return WEEK
    if cycle is not None and cycle.startswith("month:") and monthly:
        return cycle
    return DAY_REPEAT if daily is not None else None


def next_occurrence(repeat: str, start: datetime, end: datetime) -> tuple[datetime, datetime]:
    """幅の次の回（[始まり, 終わり)。日本の時刻）。repeat は `Reading.repeat`（None の幅に次の回は無い）。
    DAY_REPEAT＝いまの回の最後の日の次の1日（1日の中の幅は、次の予定の daily が絞る）／WEEK＝7日あと／
    "month:part:頃"・"month:all"・"month:day:日"＝次の月の同じ頃・まるごと・同じ日（その月に無い日は、その月の終わりの日）。"""
    start, end = start.astimezone(JST), end.astimezone(JST)
    if repeat == DAY_REPEAT:
        d = (end - timedelta(microseconds=1)).date() + timedelta(days=1)
        return _at(d, 0), _at(d + timedelta(days=1), 0)
    if repeat == WEEK:
        return start + timedelta(days=7), end + timedelta(days=7)
    if repeat.startswith("month:"):
        y, m = _month_add(start.date(), 1)
        kind, _, arg = repeat.removeprefix("month:").partition(":")
        if kind == "day":
            d = date(y, m, min(int(arg), _cal.monthrange(y, m)[1]))
            lo, hi = d, d
        else:
            lo, hi = _month_range(y, m, None if kind == "all" else arg)
        return _at(lo, 0), _at(hi + timedelta(days=1), 0)
    raise ValueError(f"次の回の決め方が読めません：{repeat!r}")
