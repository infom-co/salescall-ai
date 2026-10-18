"""お客様が電話で言った、かけ直しの時刻を読む（Q「その言葉は、いつのことか」）。持ち主 CL-2。DESIGN.md 7-1 の (7)・7-2。

会話の報告（版3。CL-1 の連絡帳 2-2 の版 0.4・4-22）は、かけ直しの時刻を、確かめ済みの話し言葉のまま返す（例「明日の午後3時」）。
その言葉を時刻に直すのは、**この1か所**（締めの判定が呼ぶ。会話の側も、通話の中で「この言葉で時刻が決まるか・確かめが要るか」を
知るときは、これを呼ぶ＝読み方を2つにしない）。

**読み方（2026-10-19 ユーザー決定）**：時刻の言葉は「かけてよい時間の幅」として読む（「夕方」「3時まで」も、それに合わせてかける。
「明日なら、いつでも」「13日であれば、そちらのご都合で」「来週」「月末」も読む）。答えは5つ：
- `AT`（ちょうどの時刻）：「明日の午後3時」「正午」。お客様が自分で指定した時刻なので、その時刻を含むNG時間の枠でもかけてよい（7-2）。
- `WINDOW`（幅）：start〜end（end＝その時刻より前。None＝終わり無し）と、1日の中の幅 daily（「夕方」のような時間の言葉が
  あるときだけ。次の日の同じ幅にかけ直すのに使う＝案A）。幅の中でも、かけてよい時間帯・NG時間は守る（お客様は時刻を
  指定していない）。「すぐ」「なるべく早く」は、いまから終わり無しの幅（2026-10-14 ユーザー「『なるべく早く』は『即』」）。
- `UNSPECIFIED`：時刻をこちらに任せる答え（「いつでも」「そちらのご都合で」だけ）・時刻を聞けなかった ⇒ 時刻の決まっていない約束
  （契約者が決めた再発信の方針の間隔。⚠️「あとで」と言われたお客様に、すぐかけ直さない）。
- `CONFIRM`：言い方は分かるが、いつかが1つに決まらない ⇒ **通話の中で確かめる**（会話＝CL-1）。例：「昼」（12時〜13時ごろとも、
  午後とも取れる。法人には12時〜13時は避けるのがマナー＝2026-10-19 ユーザー：「12時以降なのか、13時以降なのか、正午過ぎは
  避けた方がよろしいですよねなどの確認が必要」）・午前か午後かが決まらない時刻・今日と同じ曜日だけ・日曜日の「来週」など・
  過ぎた時刻・いくつもの日にまたがる「3時」。
- `UNREADABLE`：時刻の言葉として読めない（読み方の抜け）。
⇒ 判定は、確かめのないまま残った CONFIRM・UNREADABLE を推し量らずに「判定できない」にする（行は判定待ちで残し、直す担当へ修正依頼）。

言葉ごとの幅（`PERIODS`。かけてよい時間帯と重なる所だけ使う）：朝＝〜10時・午前（中）＝〜12時・午後＝13時〜・夕方＝16時〜18時・
夜（晩）＝18時〜20時。日の言葉：今日・明日・明後日／「20日」「10月20日（火）」（今日以後でいちばん近いその日）／曜日（週は月曜から。
週の言葉の無い曜日は、明日以後でいちばん近いその曜日）／今週（中）・来週・再来週／週末（その週の土日）・来週末／今月（中）・来月・
再来月・「11月」／上旬（1〜10日）・中旬（11〜20日）・下旬（21日〜末日）・初め／頭／月初（1〜5日）・末／月末（末日までの5日）。
時刻：「N時」「N時N分」「N時半」（午前・午後・朝・夕方・夜・晩を前に付けてよい）・「13〜23時」・「12時」「正午」。うしろの「頃」
「くらい」＝その時刻／「以降」「から」「過ぎ」＝その時刻からその日の終わりまで／「まで」＝その日の初めからその時刻まで／
「N時からM時」＝その間。
- 時刻は日本の時刻（日本標準時）で読む。結果は時差つきの時刻。
"""

from __future__ import annotations

import calendar as _cal
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

# 読み方の版（読み方を変えたら上げる）。判定の版（judge.JUDGE_VERSION）に入る＝上げると、読めなかった「判定待ち」の行が判定し直される
VERSION = "2"
JST = timezone(timedelta(hours=9))
AT, WINDOW, UNSPECIFIED, CONFIRM, UNREADABLE = "at", "window", "unspecified", "confirm", "unreadable"
DAY = 24 * 3600

# 言葉ごとの1日の中の幅（秒。終わりは含まない）。かけてよい時間帯と重なる所だけを使う（朝・午前の始まりは、その日の始業になる）
PERIODS: dict[str, tuple[int, int]] = {"朝": (0, 10 * 3600), "午前": (0, 12 * 3600), "午前中": (0, 12 * 3600),
                                       "午後": (13 * 3600, DAY), "夕方": (16 * 3600, 18 * 3600), "夕刻": (16 * 3600, 18 * 3600),
                                       "夜": (18 * 3600, 20 * 3600), "晩": (18 * 3600, 20 * 3600)}
# 確かめが要る言葉（いつかが1つに決まらない）
_AMBIGUOUS = ("お昼過ぎ", "お昼すぎ", "昼過ぎ", "昼すぎ", "お昼頃", "お昼ごろ", "昼頃", "昼ごろ", "お昼", "昼間", "昼")

# すぐの答え・時刻を任せる答え（言葉のあとに「で」「大丈夫です」などが付いてもよい）
_OK_TAIL = r"(?:で|に)?(?:よろしい|大丈夫|結構|いい|構いません|かまいません|お願いします)?(?:です)?"
_ASAP_WORDS = r"(?:今すぐ|いますぐ|すぐ|なるべく早く|なるべくはやく|できるだけ早く|出来るだけ早く|早めに|はやめに|至急)"
_ANY_WORDS = r"(?:いつでも|何時でも|そちらのご都合|そちらの都合|ご都合のよい時|ご都合のいい時)"
_ASAP = re.compile(_ASAP_WORDS + _OK_TAIL)
_UNSPECIFIED = re.compile(_ANY_WORDS + _OK_TAIL)
_WHOLE = re.compile(r"(?:" + _ANY_WORDS + "|" + _ASAP_WORDS + ")" + _OK_TAIL)   # 日のあとの「いつでも」など＝その日のいつでも
_JOIN = re.compile(r"(?:の|なら|であれば|でしたら|だったら|でも|は|、)*")
_TAIL = re.compile(r"(?:に|で|お願いします|でお願いします)*")

_REL_DAYS = {"今日": 0, "本日": 0, "きょう": 0, "明日": 1, "あした": 1, "あす": 1, "明後日": 2, "あさって": 2}
_WEEKDAYS = "月火水木金土日"
_WEEK_OFFSET = {"今週": 0, "来週": 1, "再来週": 2}
_MONTH_OFFSET = {"今月": 0, "来月": 1, "再来月": 2}
_REL = re.compile(r"今日|本日|きょう|明後日|あさって|明日|あした|あす")
_MONTH_PART = re.compile(r"(?:(?P<mrel>今月|来月|再来月)|(?P<mon>\d{1,2})月)?の?"
                         r"(?P<part>上旬|中旬|下旬|初め|はじめ|頭|月初め|月初|月末|末)")
_MONTH = re.compile(r"(?:(?P<mrel>今月|来月|再来月)|(?P<mon>\d{1,2})月)(?P<within>中|いっぱい)?(?!\d)")
_WEEKEND = re.compile(r"(?:(?P<week>今週|来週|再来週)(?:の?週末|の?土日|末)|週末|土日)")
_WEEK = re.compile(r"(?P<week>今週|来週|再来週)(?P<within>中|いっぱい)?(?!の?[月火水木金土日]曜)(?!の?週末)")
_DATE = re.compile(r"(?:(?P<mon>\d{1,2})月)?(?P<dom>\d{1,2})日")
_WDAY = re.compile(r"(?P<week>今週|来週|再来週)?の?(?P<wday>[月火水木金土日])曜日?")
_WDAY_CHECK = re.compile(r"\((?P<p>[月火水木金土日])(?:曜日?)?\)|の?(?P<w>[月火水木金土日])曜日?")
_HOUR = re.compile(r"(?P<part>午前|午後|朝|夕方|夕刻|夜|晩)?の?(?P<hour>\d{1,2})時(?:(?P<min>\d{1,2})分|(?P<half>半))?")
_HOUR_PARTS = {"午前": (range(0, 12), 0), "午後": (range(0, 12), 12), "朝": (range(4, 12), 0), "夕方": (range(3, 8), 12),
               "夕刻": (range(3, 8), 12), "夜": (range(6, 12), 12), "晩": (range(6, 12), 12)}
_POINT_TAIL = re.compile(r"頃|ごろ|くらい|ぐらい|位|ちょうど")
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
    t = re.sub(r"[\s。,.・]", "", t)
    return re.sub(r"[〇零一二三四五六七八九十]+", _kanji_number, t)


def _month_add(d: date, n: int) -> tuple[int, int]:
    k = d.year * 12 + d.month - 1 + n
    return k // 12, k % 12 + 1


def _month_range(year: int, month: int, part: str | None) -> tuple[date, date]:
    last = _cal.monthrange(year, month)[1]
    lo, hi = {None: (1, last), "上旬": (1, 10), "中旬": (11, 20), "下旬": (21, last), "初め": (1, 5), "はじめ": (1, 5),
              "頭": (1, 5), "月初め": (1, 5), "月初": (1, 5), "月末": (last - 4, last), "末": (last - 4, last)}[part]
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


def _day_part(t: str, today: date) -> tuple[tuple[date, date] | None, int]:
    """頭の日の言葉を読む：（日の範囲〔両端を含む〕, 読んだ長さ）。日の言葉が無ければ (None, 0)。"""
    if m := _REL.match(t):
        d = today + timedelta(days=_REL_DAYS[m.group()])
        return _check_wday(t, m.end(), (d, d))
    if m := _MONTH_PART.match(t):
        return _named_month(today, m.group("mrel"), m.group("mon"), m.group("part")), m.end()
    if m := _MONTH.match(t):
        return _named_month(today, m.group("mrel"), m.group("mon"), None), m.end()
    if m := _WEEKEND.match(t):
        if m.group("week") is None and today.weekday() == 6:
            raise _Ambiguous("今日は日曜日なので、今日のことか来週末か決まりません")
        monday = _week_monday(today, m.group("week") or "今週")
        return (max(monday + timedelta(days=5), today), monday + timedelta(days=6)), m.end()
    if m := _WEEK.match(t):
        monday = _week_monday(today, m.group("week"))
        return (max(monday, today), monday + timedelta(days=6)), m.end()
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
        return _check_wday(t, m.end(), (found, found))
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
        return (d, d), m.end()
    return None, 0


def _check_wday(t: str, pos: int, days: tuple[date, date]) -> tuple[tuple[date, date], int]:
    """日のあとに曜日が添えてあれば（「20日（火）」「明日の木曜」）、合っているかを確かめる。"""
    m = _WDAY_CHECK.match(t, pos)
    if m is None:
        return days, pos
    if _WEEKDAYS.index(m.group("p") or m.group("w")) != days[0].weekday():
        raise _Ambiguous("日と曜日が合いません")
    return days, m.end()


def _hour(m: re.Match) -> int:
    """何時何分（その日の0時からの秒）。午前か午後かが決まらなければ _Ambiguous。"""
    h, part = int(m.group("hour")), m.group("part")
    minute = 30 if m.group("half") else int(m.group("min") or 0)
    if minute > 59:
        raise _Ambiguous("分が読めません")
    if part is not None:
        hours, add = _HOUR_PARTS[part]
        if h not in hours:
            raise _Ambiguous(f"「{part}」と{h}時が合いません（12時ちょうどは、午前・午後のどちらか決まりません）")
        h += add
    elif not (h == 12 or 13 <= h <= 23):
        raise _Ambiguous("午前か午後かが決まりません")
    return h * 3600 + minute * 60


def _time_part(t: str, pos: int) -> tuple[str, int, int, int]:
    """時刻の言葉を読む：（種類 point／window／whole, 始まりの秒, 終わりの秒, 読んだ位置）。読めなければ _Ambiguous か ValueError。"""
    rest = t[pos:]
    if rest == "" or _WHOLE.fullmatch(rest):
        return "whole", 0, DAY, len(t)
    for word in _AMBIGUOUS:
        if rest.startswith(word):
            raise _Ambiguous(f"「{word}」は、12時〜13時ごろとも午後とも取れます（法人には12時〜13時は避けるのがマナー）")
    if rest.startswith("正午"):
        return "point", 12 * 3600, 12 * 3600, pos + 2
    m = _HOUR.match(t, pos)
    if m is None:
        for word in sorted(PERIODS, key=len, reverse=True):
            if rest.startswith(word):
                end = pos + len(word)
                if t.startswith("中", end):
                    end += 1
                s0, s1 = PERIODS[word]
                return "window", s0, s1, end
        raise ValueError(f"時刻がありません（「{rest}」）")
    sec, end = _hour(m), m.end()
    if (n := re.compile(r"から(?P<part>午前|午後|朝|夕方|夕刻|夜|晩)?の?(?P<hour>\d{1,2})時(?:(?P<min>\d{1,2})分|(?P<half>半))?"
                        r"(?:まで|の間|の間に|までの間)?").match(t, end)):
        try:
            until = _hour(n)
        except _Ambiguous:
            h = int(n.group("hour"))                 # 「午後3時から5時」：前の時刻が午後なら、あとの時刻も午後
            until = (h + 12) * 3600 + (30 * 60 if n.group("half") else int(n.group("min") or 0) * 60) \
                if n.group("part") is None and 1 <= h <= 11 and sec >= 12 * 3600 else None
            if until is None:
                raise
        if until <= sec:
            raise _Ambiguous("「〜から〜まで」の終わりが始まりより前です")
        return "window", sec, until, n.end()
    if m2 := _POINT_TAIL.match(t, end):
        return "point", sec, sec, m2.end()
    if m2 := _FROM_TAIL.match(t, end):
        return "window", sec, DAY, m2.end()
    if m2 := _UNTIL_TAIL.match(t, end):
        return "window", 0, sec, m2.end()
    return "point", sec, sec, end


def _at(d: date, sec: int) -> datetime:
    return datetime.combine(d, time(), tzinfo=JST) + timedelta(seconds=sec)


def read(text: str | None, now: datetime) -> Reading:
    """お客様が言った時刻の言葉を読む。now＝その通話の時刻（「明日」などの起点。過ぎた時刻は確かめが要る）。
    言葉が無い（時刻を聞けなかった）ときは UNSPECIFIED。"""
    if text is None or not text.strip():
        return Reading(UNSPECIFIED)
    t = _clean(text)
    local = now.astimezone(JST)
    if _UNSPECIFIED.fullmatch(t):
        return Reading(UNSPECIFIED)
    if _ASAP.fullmatch(t):
        return Reading(WINDOW, local, None)
    try:
        days, pos = _day_part(t, local.date())
        pos = _JOIN.match(t, pos).end()
        kind, s0, s1, end = _time_part(t, pos)
    except _Ambiguous as e:
        return _confirm(str(e))
    except ValueError as e:
        return _unreadable(str(e))
    if not _TAIL.fullmatch(t[end:]):
        return _unreadable(f"読めない言葉があります（「{t[end:]}」）")
    if days is None:
        if kind == "whole":
            return _unreadable("日も時刻もありません")
        days = (local.date(), local.date())          # 日が無ければ今日
    d0, d1 = days
    if kind == "point":
        if d0 != d1:
            return _confirm("いくつもの日にまたがっていて、どの日の時刻か決まりません")
        at = _at(d0, s0)
        if at <= local:
            return _confirm("通話の時刻より前の時刻です（今日のことか、明日以降か決まりません）")
        return Reading(AT, at)
    start, end_at = _at(d0, s0), _at(d1, s1)
    if end_at <= local:
        return _confirm("その頃はもう過ぎています（今日のことか、明日以降か決まりません）")
    return Reading(WINDOW, max(start, local), end_at, (s0, s1) if kind == "window" else None)
