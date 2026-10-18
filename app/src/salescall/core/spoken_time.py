"""お客様が電話で言った、かけ直しの時刻を読む（Q「その言葉は、いつのことか」）。持ち主 CL-2。DESIGN.md 7-1 の (7)。

会話の報告（版3。CL-1 の連絡帳 2-2 の版 0.4・4-22）は、かけ直しの時刻を、確かめ済みの話し言葉のまま返す（例「明日の午後3時」）。
その言葉を時刻に直すのは、**この1か所**（締めの判定が呼ぶ。会話の側も、通話の中で「この言葉で時刻が決まるか」を確かめるときは、これを呼ぶ）。

- 読むのは、日と時刻がはっきり決まる言い方だけ（下の決まり）。**推し量らない**（原則10）：決まらない言い方は「読めない」
  （`UNREADABLE`）。判定はそれを「判定できない」とし、行は判定待ちで残して、読み方を直す担当へ修正依頼を出す（直したら判定し直す）。
  ⇒ 読めない言い方を、時刻の決まっていない約束（契約者の再発信の方針）として扱って、お客様が言った時刻と違う時刻にかけない。
- すぐの答え（「すぐ」「なるべく早く」など）は、通話の時刻（`AT`。門が許す最も早い時刻にかける。2026-10-14 ユーザー「『なるべく
  早く』は『即』」）。時刻をこちらに任せる答え（「いつでも」「そちらのご都合で」）は `UNSPECIFIED`（時刻の決まっていない約束＝
  契約者が決めた再発信の方針 callback_unspecified_time。⚠️「あとで」と言われたお客様に、すぐかけ直さない）。
- 日：今日・明日・明後日（かな書きも）／「20日」（今日以後でいちばん近いその日）／「10月20日」（今日以後でいちばん近いその日）／
  曜日（「来週の月曜日」など。週は月曜から。**今日か、言われた曜日が日曜日のときの「今週・来週・再来週」は、週の始まりで答えが
  変わるので読めない**。週の言葉の無い曜日は、明日以後でいちばん近いその曜日。今日と同じ曜日だけを言われたら、今日か来週かが
  決まらないので読めない）。日と曜日の両方があれば、合っているときだけ。日が無ければ今日。
- 時刻：「午前・午後・朝・昼・夕方・夜・晩」＋「N時」（「N分」「半」）、「13時」〜「23時」、「12時」「正午」（昼の12時）。午前・午後の
  無い 1〜11 時は読めない（会話の側が営業時間で補うか、お客様に聞く＝CL-1 の fill_ampm）。うしろの「頃」「以降」「から」などは
  その時刻（その時刻からかけてよい）。「まで」（その前）など、決まりに無い言葉が残れば読めない。
- 読んだ時刻が、通話の時刻より前（過ぎた時刻）なら読めない（例：夕方の通話で「午後3時」＝明日のことか決まらない）。
- 時刻は日本の時刻（日本標準時）で読む。結果は時差つきの時刻。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

# 読み方の版（読み方を変えたら上げる）。判定の版（judge.JUDGE_VERSION）に入る＝上げると、読めなかった「判定待ち」の行が判定し直される
VERSION = "1"
JST = timezone(timedelta(hours=9))
AT, UNSPECIFIED, UNREADABLE = "at", "unspecified", "unreadable"

# すぐの答え・時刻を任せる答え（言葉のあとに「で」「大丈夫です」などが付いてもよい）
_OK_TAIL = r"(?:で|に)?(?:よろしい|大丈夫|結構|いい|構いません|かまいません|お願いします)?(?:です)?"
_ASAP = re.compile(r"(?:今すぐ|いますぐ|すぐ|なるべく早く|なるべくはやく|できるだけ早く|出来るだけ早く|早めに|はやめに|至急)" + _OK_TAIL)
_UNSPECIFIED = re.compile(r"(?:いつでも|何時でも|そちらのご都合|そちらの都合)" + _OK_TAIL)
_REL_DAYS = {"今日": 0, "本日": 0, "きょう": 0, "明日": 1, "あした": 1, "あす": 1, "明後日": 2, "あさって": 2}
_WEEKDAYS = "月火水木金土日"
_DAY = re.compile(
    r"(?:(?P<rel>今日|本日|きょう|明後日|あさって|明日|あした|あす)"
    r"|(?:(?P<month>\d{1,2})月)?(?P<dom>\d{1,2})日"
    r"|(?P<week>今週|来週|再来週)?の?(?=[月火水木金土日]曜))"
    r"(?:\((?P<wday_p>[月火水木金土日])(?:曜日?)?\)|の?(?P<wday>[月火水木金土日])曜日?)?")
_TIME = re.compile(r"(?P<noon>正午)|(?P<part>午前|午後|朝|昼|夕方|夕刻|夜|晩)?の?(?P<hour>\d{1,2})時(?:(?P<min>\d{1,2})分|(?P<half>半))?")
_TAIL = re.compile(r"(?:頃|ごろ|くらい|ぐらい|位|以降|以後|過ぎ|すぎ|から|に|で)*")
# 時刻の部分ごとに、読んでよい時の範囲と、24時の時への直し方（範囲の外は読めない）
_PARTS = {"午前": (range(0, 12), 0), "午後": (range(0, 12), 12), "朝": (range(4, 12), 0), "夕方": (range(3, 8), 12),
          "夕刻": (range(3, 8), 12), "夜": (range(6, 12), 12), "晩": (range(6, 12), 12)}
_KANJI = {"〇": 0, "零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


@dataclass(frozen=True)
class Reading:
    kind: str                 # AT / UNSPECIFIED / UNREADABLE
    at: datetime | None       # AT のときだけ（時差つき）
    why: str | None = None    # UNREADABLE のときの理由（日本語）


def _unreadable(why: str) -> Reading:
    return Reading(UNREADABLE, None, why)


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
    t = re.sub(r"[\s、。,.・]", "", t)
    return re.sub(r"[〇零一二三四五六七八九十]+", _kanji_number, t)


def _day(m: re.Match, today: date) -> date | str:
    """日（読めなければ理由の文字）。"""
    wday = m.group("wday") or m.group("wday_p")
    if m.group("rel"):
        d = today + timedelta(days=_REL_DAYS[m.group("rel")])
    elif m.group("dom"):
        dom, month = int(m.group("dom")), m.group("month")
        candidates = []
        for k in range(0, 13 if month is None else 2):   # 今日以後でいちばん近いその日（月が無ければ月を、あれば年を進める）
            y, mo = (today.year + (today.month - 1 + k) // 12, (today.month - 1 + k) % 12 + 1) if month is None \
                else (today.year + k, int(month))
            try:
                c = date(y, mo, dom)
            except ValueError:
                if month is not None:
                    return "その月に無い日です"
                continue
            if c >= today:
                candidates.append(c)
                break
        if not candidates:
            return "日が読めません"
        d = candidates[0]
    else:
        week = m.group("week")
        target = _WEEKDAYS.index(wday)
        if week is None:
            if target == today.weekday():
                return "今日と同じ曜日だけでは、今日か来週かが決まりません"
            return today + timedelta(days=(target - today.weekday()) % 7)
        if today.weekday() == 6 or target == 6:
            return "日曜日が入ると、週の始まり（月曜・日曜）で日が変わります"
        monday = today - timedelta(days=today.weekday()) + timedelta(weeks={"今週": 0, "来週": 1, "再来週": 2}[week])
        d = monday + timedelta(days=target)
        if d < today:
            return "過ぎた曜日です"
        return d
    if wday is not None and _WEEKDAYS.index(wday) != d.weekday():
        return "日と曜日が合いません"
    return d


def _hour(m: re.Match) -> tuple[int, int] | str:
    """（時, 分）。読めなければ理由の文字。"""
    if m.group("noon"):
        return 12, 0
    h, part = int(m.group("hour")), m.group("part")
    minute = 30 if m.group("half") else int(m.group("min") or 0)
    if minute > 59:
        return "分が読めません"
    if part == "昼":
        if h == 12:
            return 12, minute
        return (h + 12, minute) if 1 <= h <= 3 else "昼の時刻が読めません"
    if part is not None:
        hours, add = _PARTS[part]
        if h not in hours:
            return f"「{part}」と{h}時が合いません（12時ちょうどは、午前・午後のどちらか決まりません）"
        return h + add, minute
    if h == 12 or 13 <= h <= 23:
        return h, minute
    return "午前か午後かが決まりません"


def read(text: str | None, now: datetime) -> Reading:
    """お客様が言った時刻の言葉を読む。now＝その通話の時刻（「明日」などの起点。過ぎた時刻は読めない）。
    言葉が無い（時刻を聞けなかった）ときは UNSPECIFIED。"""
    if text is None or not text.strip():
        return Reading(UNSPECIFIED, None)
    t = _clean(text)
    if _UNSPECIFIED.fullmatch(t):
        return Reading(UNSPECIFIED, None)
    local = now.astimezone(JST)
    if _ASAP.fullmatch(t):
        return Reading(AT, local)
    day = local.date()
    pos = 0
    m = _DAY.match(t)
    if m and m.end() > 0:
        got = _day(m, local.date())
        if isinstance(got, str):
            return _unreadable(got)
        day, pos = got, m.end()
        if t.startswith("の", pos):
            pos += 1
    m = _TIME.match(t, pos)
    if not m:
        return _unreadable("時刻がありません")
    hm = _hour(m)
    if isinstance(hm, str):
        return _unreadable(hm)
    rest = t[m.end():]
    if not _TAIL.fullmatch(rest):
        return _unreadable(f"読めない言葉があります（「{rest}」）")
    at = datetime(day.year, day.month, day.day, hm[0], hm[1], tzinfo=JST)
    if at <= local:
        return _unreadable("通話の時刻より前の時刻です")
    return Reading(AT, at)
