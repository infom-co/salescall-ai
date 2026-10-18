"""お客様が電話で言った、かけ直しの時刻を読む（Q「その言葉は、いつのことか」）。持ち主 CL-2。DESIGN.md 7-1d・7-2。

会話の報告（版3。CL-1 の連絡帳 2-2 の版 0.4・4-22）は、かけ直しの時刻を、確かめ済みの話し言葉のまま返す（例「明日の午後3時」）。
その言葉を時刻に直すのは、**この1か所**（締めの判定が呼ぶ。会話の側も、通話の中で「この言葉で時刻が決まるか・確かめが要るか」を
知るときは、これを呼ぶ＝読み方を2つにしない）。

**読み方（2026-10-19 ユーザー決定）**：時刻の言葉は「かけてよい時間の幅」として読む（「夕方」「3時まで」も、それに合わせてかける。
「明日なら、いつでも」「13日であれば、そちらのご都合で」「来週」「月末」「週中」「しあさって」なども読む）。答えは5つ：
- `AT`（ちょうどの時刻）：「明日の午後3時」「正午」「30分後」。お客様が自分で指定した時刻なので、その時刻を含むNG時間の枠でもかけてよい。
- `WINDOW`（幅）：start〜end（end＝その時刻より前。None＝終わり無し）、1日の中の幅 daily（時間の言葉があるときだけ。次の日の
  同じ幅にかけ直すのに使う＝案A）、日の種類 days（「平日」「土日祝」などのときだけ。かけてよい日の鍵＝calling_hours の曜日・
  祝日の鍵）。幅の中でも、かけてよい時間帯・NG時間は守る（お客様は時刻を指定していない）。「すぐ」「なるべく早く」は、いまから
  終わり無しの幅（2026-10-14 ユーザー「『なるべく早く』は『即』」）。
- `UNSPECIFIED`：時刻をこちらに任せる答え（「いつでも」「そちらのご都合で」「あとで」「後ほど」だけ）・時刻を聞けなかった ⇒
  時刻の決まっていない約束（契約者が決めた再発信の方針の間隔。⚠️「あとで」と言われたお客様に、すぐかけ直さない）。
- `CONFIRM`：言い方は分かるが、いつかが1つに決まらない ⇒ **通話の中で確かめる**（会話＝CL-1）。例：「昼」（12時〜13時ごろとも、
  午後とも取れる。法人には12時〜13時は避けるのがマナー＝2026-10-19 ユーザー）・営業時間で午前か午後かが決まらない時刻・今日と
  同じ曜日だけ・日曜日の「来週」など・過ぎた時刻・いくつもの日にまたがる「3時」・「年明け」「年末」「数日後」のような幅の決まらない言葉。
- `UNREADABLE`：時刻の言葉として読めない（読み方の抜け）。
⇒ 判定は、確かめのないまま残った CONFIRM・UNREADABLE を推し量らずに「判定できない」にする（行は判定待ちで残し、直す担当へ修正依頼）。

**午前・午後**（2026-10-19 ユーザー：「通常午前3時に電話をかけてほしい人はいないので、営業時間の範囲だけ」。CL-1 の fill_ampm と同じ決まり）：
午前・午後の無い 1〜12時は、お客様の営業時間（設定 callee.business_hours。会社の初めは 9:00〜19:00。日をまたいでよい）に入るほうに
する。両方入る・どちらも入らないときだけ確かめる。「12時」は昼の12時。

言葉ごとの幅（`PERIODS`。かけてよい時間帯と重なる所だけ使う。2026-10-19 ユーザー「OK」）：朝・朝一＝〜10時・午前（中）＝〜12時・
午後一＝13〜14時・午後＝13時〜・夕方＝16〜18時・夜（晩）＝18〜20時。日の言葉の一覧は DESIGN.md 7-1d の表。
- 時刻は日本の時刻（日本標準時）で読む。結果は時差つきの時刻。
"""

from __future__ import annotations

import calendar as _cal
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

# 読み方の版（読み方を変えたら上げる）。判定の版（judge.JUDGE_VERSION）に入る＝上げると、読めなかった「判定待ち」の行が判定し直される
VERSION = "4"
JST = timezone(timedelta(hours=9))
AT, WINDOW, UNSPECIFIED, CONFIRM, UNREADABLE = "at", "window", "unspecified", "confirm", "unreadable"
DAY = 24 * 3600
H = 3600
DEFAULT_BUSINESS_HOURS = (9 * H, 19 * H)   # 設定 callee.business_hours の初めの値（会社 9:00〜19:00）

# 言葉ごとの1日の中の幅（秒。終わりは含まない）。かけてよい時間帯と重なる所だけを使う（朝・午前の始まりは、その日の始業になる）。
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
    s0, s1 = (int(v[:2]) * H + int(v[3:]) * 60 for v in value)
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
_TAIL = re.compile(r"(?:に|で|には|お願いします|でお願いします)*"
                   r"(?:(?:なら|であれば|でしたら|だったら)?(?:大丈夫|いつでも大丈夫|結構|OK|構いません|かまいません|いい)?(?:です)?)")

_REL_DAYS = {"今日": 0, "本日": 0, "きょう": 0, "明日": 1, "あした": 1, "あす": 1, "明後日": 2, "あさって": 2,
             "明々後日": 3, "明明後日": 3, "しあさって": 3}
_WEEKDAYS = "月火水木金土日"
_WEEK_OFFSET = {"今週": 0, "来週": 1, "再来週": 2}
_MONTH_OFFSET = {"今月": 0, "来月": 1, "再来月": 2}
_REL = re.compile(r"今日中|本日中|今日|本日|きょう|明々後日|明明後日|しあさって|明後日|あさって|明日中|明日|あした|あす")
_REL_PERIOD = {"今朝": (0, "朝"), "今晩": (0, "夜"), "今夜": (0, "夜"), "明朝": (1, "朝"), "明晩": (1, "夜")}
_AFTER = re.compile(r"(?P<n>\d{1,2})(?P<unit>日|週間|か月|ヶ月|ヵ月|カ月|箇月)後")
_RANGE_AFTER = re.compile(r"(?P<a>\d{1,2})(?:〜|～|-|か|から)(?P<b>\d{1,2})(?P<unit>日|週間)後")
_LATER_CLOCK = re.compile(r"(?P<n>\d{1,3})(?P<unit>分|時間)(?P<half>半)?後")
_MONTH_PART = re.compile(r"(?:(?P<mrel>今月|来月|再来月)|(?P<mon>\d{1,2})月)?の?"
                         r"(?P<part>上旬|中旬|下旬|中頃|中ごろ|半ば|初め|はじめ|頭|月初め|月初|月末|末)")
_MONTH = re.compile(r"(?:(?P<mrel>今月|来月|再来月)|(?P<mon>\d{1,2})月)(?P<within>中|いっぱい)?(?!\d)")
_YEAR = re.compile(r"(?P<y>年内|今年中|今年いっぱい|来年)")
_WEEKEND = re.compile(r"(?:(?P<week>今週|来週|再来週)(?:の?週末|の?土日|末)|週末|土日)(?!祝)")
_MIDWEEK = re.compile(r"(?:(?P<week>今週|来週|再来週)の?(?:週中|週の半ば|週半ば|週の中ごろ|週の中頃|半ば|中ごろ|中頃)"
                      r"|週中|週の半ば|週半ば|週の中ごろ|週の中頃)")
_WEEK_START = re.compile(r"週明け|週初め|週のはじめ|週の初め")
_WEEK = re.compile(r"(?P<week>今週|来週|再来週)(?P<within>中|いっぱい)?(?!の?[月火水木金土日]曜)(?!の?週)(?!末)(?!半ば)")
_DATE = re.compile(r"(?:(?P<mon>\d{1,2})月)?(?P<dom>\d{1,2})日")
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
    daily: tuple[int, int] | None = None  # WINDOW：1日の中の幅（秒）。時間の言葉があるときだけ（次の日の同じ幅＝案A）
    why: str | None = None               # CONFIRM・UNREADABLE の理由（日本語）
    days: tuple[str, ...] | None = None  # WINDOW：かけてよい日の種類（「平日」「土日祝」など。曜日・祝日の鍵）

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
    """「十五」「二十」「三十一」のような漢数字（99まで）を数字にする。"""
    s = m.group()
    if "十" not in s:
        return "".join(str(_KANJI[c]) for c in s)
    tens, _, ones = s.partition("十")
    if len(tens) > 1 or len(ones) > 1:
        return s                                   # 決まりに無い書き方：そのまま残す（読めない）
    return str((_KANJI[tens] if tens else 1) * 10 + (_KANJI[ones] if ones else 0))


def _clean(text: str) -> str:
    t = unicodedata.normalize("NFKC", text)
    t = re.sub(r"[\s。,.・!！]", "", t).replace("ケ月", "ヶ月")
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
              "月末": (last - 2, last), "末": (last - 2, last)}[part]
    return date(year, month, lo), date(year, month, hi)


def _named_month(today: date, mrel: str | None, mon: str | None, part: str | None) -> tuple[date, date]:
    """月の言葉の日の範囲（今日以後。「11月」は今日以後でいちばん近いその月）。"""
    if mrel is not None:
        return _month_range(*_month_add(today, _MONTH_OFFSET[mrel]), part)
    if mon is not None:
        m = int(mon)
        if not 1 <= m <= 12:
            raise _Ambiguous("月が読めません")
        for year in (today.year, today.year + 1):
            r = _month_range(year, m, part)
            if r[1] >= today:
                return r
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


def _day_part(t: str, today: date) -> tuple[tuple[date, date] | None, str | None, int]:
    """頭の日の言葉を読む：（日の範囲〔両端を含む〕, その日に付いた時間の言葉〔「今晩」の「夜」など〕, 読んだ長さ）。
    日の言葉が無ければ (None, None, 0)。"""
    for word in _AMBIGUOUS_DAY:
        if t.startswith(word):
            raise _Ambiguous(f"「{word}」は、いつのことか1つに決まりません")
    for word, (n, period) in _REL_PERIOD.items():
        if t.startswith(word):
            d = today + timedelta(days=n)
            return (d, d), period, len(word)
    if m := _REL.match(t):
        d = today + timedelta(days=_REL_DAYS[m.group().removesuffix("中")])
        days, end = _check_wday(t, m.end(), (d, d))
        return days, None, end
    if m := _RANGE_AFTER.match(t):
        a, b, per = int(m.group("a")), int(m.group("b")), 7 if m.group("unit") == "週間" else 1
        if not 1 <= a < b:
            raise _Ambiguous("「〜日後」の範囲が読めません")
        return (today + timedelta(days=a * per), today + timedelta(days=b * per)), None, m.end()
    if m := _AFTER.match(t):
        n, unit = int(m.group("n")), m.group("unit")
        if n < 1:
            raise _Ambiguous("「〜後」が読めません")
        d = today + timedelta(days=n) if unit == "日" else today + timedelta(weeks=n) if unit == "週間" \
            else _months_later(today, n)
        return (d, d), None, m.end()
    if m := _YEAR.match(t):
        if m.group("y") == "来年":
            return (date(today.year + 1, 1, 1), date(today.year + 1, 12, 31)), None, m.end()
        return (today, date(today.year, 12, 31)), None, m.end()
    if m := _MONTH_PART.match(t):
        return _named_month(today, m.group("mrel"), m.group("mon"), m.group("part")), None, m.end()
    if m := _MONTH.match(t):
        return _named_month(today, m.group("mrel"), m.group("mon"), None), None, m.end()
    if m := _MIDWEEK.match(t):   # 週中・週の半ば＝火〜木（✅ 2026-10-19 ユーザー「OK」）
        monday = _week_monday(today, m.group("week") or "今週")
        lo, hi = monday + timedelta(days=1), monday + timedelta(days=3)
        if hi < today:
            raise _Ambiguous("今週の半ばはもう過ぎています（来週のことか、決まりません）")
        return (max(lo, today), hi), None, m.end()
    if m := _WEEK_START.match(t):  # 週明け＝明日以後でいちばん近い月曜日の1日（✅ 2026-10-19 ユーザー「OK」）
        d = today + timedelta(days=(7 - today.weekday()) % 7 or 7)
        return (d, d), None, m.end()
    if m := _WEEKEND.match(t):
        if m.group("week") is None and today.weekday() == 6:
            raise _Ambiguous("今日は日曜日なので、今日のことか来週末か決まりません")
        monday = _week_monday(today, m.group("week") or "今週")
        return (max(monday + timedelta(days=5), today), monday + timedelta(days=6)), None, m.end()
    if m := _WEEK.match(t):
        monday = _week_monday(today, m.group("week"))
        return (max(monday, today), monday + timedelta(days=6)), None, m.end()
    if m := _DATE.match(t):
        dom, mon = int(m.group("dom")), m.group("mon")
        found = None
        for k in range(0, 13 if mon is None else 2):   # 今日以後でいちばん近いその日（月が無ければ月を、あれば年を進める）
            y, mo = _month_add(today, k) if mon is None else (today.year + k, int(mon))
            try:
                c = date(y, mo, dom)
            except ValueError:
                if mon is not None:
                    raise _Ambiguous("その月に無い日です") from None
                continue
            if c >= today:
                found = c
                break
        if found is None:
            raise _Ambiguous("日が読めません")
        days, end = _check_wday(t, m.end(), (found, found))
        return days, None, end
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
        return (d, d), None, m.end()
    return None, None, 0


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


def _in_hours(hour: int, hours: tuple[int, int]) -> bool:
    """その時（0〜23）が営業時間に入るか（CL-1 の fill_ampm と同じ決まり：時の単位で、終わりの時も含む。日をまたいでよい）。"""
    open_, close = hours[0] // H, hours[1] // H
    return open_ <= hour <= close if open_ <= close else (hour >= open_ or hour <= close)


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
    am_in, pm_in = _in_hours(am, business), _in_hours(pm, business)
    if am_in == pm_in:
        raise _Ambiguous("営業時間からは、午前か午後かが決まりません")
    return (am if am_in else pm) * H + minute * 60


def _time_part(t: str, pos: int, business: tuple[int, int],
               periods: dict[str, tuple[int, int]] = PERIODS) -> tuple[str, int, int, int]:
    """時刻の言葉を読む：（種類 point／window／whole, 始まりの秒, 終わりの秒, 読んだ位置）。読めなければ _Ambiguous か ValueError。"""
    rest = t[pos:]
    if _WHOLE.fullmatch(rest):
        return "whole", 0, DAY, len(t)
    if _TAIL.fullmatch(rest):
        return "whole", 0, DAY, pos
    for word in _AMBIGUOUS_TIME:
        if rest.startswith(word):
            raise _Ambiguous(f"「{word}」は、いつのことか1つに決まりません（昼は、12時〜13時ごろとも午後とも取れます。"
                             "法人には12時〜13時は避けるのがマナー）")
    if rest.startswith("正午"):
        return "point", 12 * H, 12 * H, pos + 2
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
                    return "window", s0, DAY, f.end()
                if _UNTIL_TAIL.match(t, end):
                    raise _Ambiguous(f"「{word}まで」は、その初めまでか終わりまでか決まりません")
                return "window", s0, s1, end
        raise ValueError(f"時刻がありません（「{rest}」）")
    sec, end = _hour(m, business), m.end()
    if n := _HOUR_TO.match(t, end):
        until = _hour(n, business)
        if until <= sec:
            raise _Ambiguous("「〜から〜まで」の終わりが始まりより前です")
        return "window", sec, until, n.end()
    if m2 := _BAND_TAIL.match(t, end):
        return "window", sec, min(sec + H, DAY), m2.end()
    if m2 := _POINT_TAIL.match(t, end):
        return "point", sec, sec, m2.end()
    if m2 := _FROM_TAIL.match(t, end):
        return "window", sec, DAY, m2.end()
    if m2 := _UNTIL_TAIL.match(t, end):
        return "window", 0, sec, m2.end()
    return "point", sec, sec, end


def _at(d: date, sec: int) -> datetime:
    return datetime.combine(d, time(), tzinfo=JST) + timedelta(seconds=sec)


def read(text: str | None, now: datetime, business_hours: tuple[int, int] = DEFAULT_BUSINESS_HOURS,
         periods: dict[str, tuple[int, int]] = PERIODS) -> Reading:
    """お客様が言った時刻の言葉を読む。now＝その通話の時刻（「明日」などの起点。過ぎた時刻は確かめが要る）。
    business_hours＝お客様の営業時間（0時からの秒の (始まり, 終わり)。設定 callee.business_hours。午前・午後を決める）。
    periods＝言葉ごとの幅（依頼者ごとの設定 callback.period_* を `periods_from` で表にしたもの）。
    言葉が無い（時刻を聞けなかった）ときは UNSPECIFIED。"""
    if text is None or not text.strip():
        return Reading(UNSPECIFIED)
    t = _clean(text)
    local = now.astimezone(JST)
    if _UNSPECIFIED.fullmatch(t):
        return Reading(UNSPECIFIED)
    if _ASAP.fullmatch(t):
        return Reading(WINDOW, local, None)
    if (m := _LATER_CLOCK.match(t)) and _TAIL.fullmatch(t[m.end():]):   # 「30分後」「1時間後」「1時間半後」
        n = int(m.group("n"))
        minutes = n if m.group("unit") == "分" else n * 60 + (30 if m.group("half") else 0)
        if minutes < 1:
            return _confirm("「〜後」が読めません")
        return Reading(AT, local + timedelta(minutes=minutes))
    try:
        days, period, pos = _day_part(t, local.date())
        pos = _JOIN.match(t, pos).end()
        kinds, pos = _day_kinds(t, pos)
        pos = _JOIN.match(t, pos).end()
        if period is not None and not _TAIL.fullmatch(t[pos:]):
            # 「今晩8時」「明朝9時」：日に付いた時間の言葉を、うしろの時刻の頭に付けて読む（「夜8時」＝20時）
            kind, s0, s1, end = _time_part(period + t[pos:], 0, business_hours, periods)
            end += pos - len(period)
        elif period is not None:                      # 「今晩」「明朝」だけ
            (s0, s1), kind, end = periods[period], "window", pos
        else:
            kind, s0, s1, end = _time_part(t, pos, business_hours, periods)
    except _Ambiguous as e:
        return _confirm(str(e))
    except ValueError as e:
        return _unreadable(str(e))
    if not _TAIL.fullmatch(t[end:]):
        return _unreadable(f"読めない言葉があります（「{t[end:]}」）")
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
        return Reading(AT, at)
    daily = (s0, s1) if kind == "window" else None
    if days is None:                                  # 「平日の夕方」「土日祝ならいつでも」：日の種類だけ（終わり無し）
        return Reading(WINDOW, local, None, daily, days=kinds)
    start, end_at = _at(days[0], s0), _at(days[1], s1)
    if end_at <= local:
        return _confirm("その頃はもう過ぎています（今日のことか、明日以降か決まりません）")
    return Reading(WINDOW, max(start, local), end_at, daily, days=kinds)
