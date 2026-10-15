"""かけてよい時刻か（Q12 の決まりの部分）。持ち主 CL-2。DESIGN.md 5-2 の2・5-2-1 の4・5。

データベースを読まない部品（時間帯の値と、祝日かどうかを答える関数を受け取る）。
門（Q2）・次の動き（Q4：次の時間帯の始まり）・会話の値の確かめ（CL-1 の value_types：折り返しの日時が時間帯の内か）が、
どれもこれを呼ぶ（時間帯の決まりを2か所に書かない）。時間帯の値は設定の合成（Q10）が出したもの、祝日は暦（core/calendar.py）。

- 時刻は日本時間の、時差を持たない日時（データベースの時刻を日本時間に直したもの）で受け取る。
- 時間帯は「始まりを含み、終わりを含まない」。終わりは 24:00（その日の終わり）まで書ける。
- 区間（予約から予約証の期限まで）は、全部が時間帯の内側にあること（5-2-1 の4）。日をまたぐ区間は、両方の日を確かめる。
- 祝日かどうかが分からない日（暦の範囲の外）は、`is_holiday` が例外を出す ⇒ そのまま呼び出し元へ返す（判断できない）。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, time, timedelta

DAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_DAY = 24 * 3600


def day_key(d: date, is_holiday: Callable[[date], bool]) -> str:
    """その日の時間帯の鍵（祝日なら holiday、そうでなければ曜日）。"""
    return "holiday" if is_holiday(d) else DAY_KEYS[d.weekday()]


def _seconds(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 3600 + int(m) * 60


def _ranges(hours: dict, key: str) -> list[tuple[int, int]]:
    return [(_seconds(a), _seconds(b)) for a, b in hours[key]]


def _of_day(t: datetime) -> float:
    return t.hour * 3600 + t.minute * 60 + t.second + t.microsecond / 1e6


def covers(hours: dict, is_holiday: Callable[[date], bool], start: datetime, end: datetime) -> bool:
    """start から end まで（日本時間）が、すべて時間帯の内側か。start == end なら、その時刻が内側か。"""
    if end < start:
        raise ValueError("終わりが始まりより前です")
    if start.tzinfo is not None or end.tzinfo is not None:
        raise ValueError("日本時間の、時差を持たない日時で渡してください")
    if start == end:
        a = _of_day(start)
        return any(r0 <= a < r1 for r0, r1 in _ranges(hours, day_key(start.date(), is_holiday)))
    t = start
    while t < end:
        next_midnight = datetime.combine(t.date() + timedelta(days=1), time())
        seg_end = min(end, next_midnight)
        a = _of_day(t)
        b = _DAY if seg_end == next_midnight else _of_day(seg_end)
        if not any(r0 <= a and b <= r1 for r0, r1 in _ranges(hours, day_key(t.date(), is_holiday))):
            return False
        t = seg_end
    return True


def next_opening(hours: dict, is_holiday: Callable[[date], bool], after: datetime, length: timedelta,
                 *, horizon_days: int = 31) -> datetime | None:
    """after 以降で、length の長さの区間がまるごと時間帯に入る、最も早い始まり（日本時間）。horizon_days 日のうちに無ければ None。"""
    if covers(hours, is_holiday, after, after + length):
        return after
    for offset in range(horizon_days + 1):
        d = after.date() + timedelta(days=offset)
        for r0, _ in sorted(_ranges(hours, day_key(d, is_holiday))):
            candidate = datetime.combine(d, time()) + timedelta(seconds=r0)
            if candidate >= after and covers(hours, is_holiday, candidate, candidate + length):
                return candidate
    return None


# ── 時間帯の区切り（「別の時間帯にかけ直す」。DESIGN.md 3-5・7-2-1。2026-10-15 ユーザー） ──
def band_of(t: datetime, boundaries: list[int]) -> int:
    """その時刻が、1日のうちのどの時間帯か（0 から。boundaries＝区切りの時、例 [12, 15] ⇒ 午前・昼過ぎ・夕方の3つ）。"""
    h = t.hour + t.minute / 60 + t.second / 3600
    return sum(1 for b in boundaries if h >= b)


def next_opening_in_new_band(hours: dict, is_holiday: Callable[[date], bool], after: datetime, length: timedelta,
                             boundaries: list[int], tried: set[int], *, horizon_days: int = 31) -> datetime | None:
    """after 以降で、まだ試していない時間帯（tried に無いもの）の中の、かけてよい最も早い始まり（日本時間）。
    その業務の時間帯に、試していない時間帯がもう無ければ（例：夜だけ開くお店）、翌日以降の同じ頃からの次の始まり（日を変える）。"""
    for offset in range(horizon_days + 1):
        d = after.date() + timedelta(days=offset)
        starts = {r0 for r0, _ in _ranges(hours, day_key(d, is_holiday))} | {b * 3600 for b in boundaries}
        if offset == 0:
            starts.add(int(_of_day(after)))
        for s in sorted(starts):
            candidate = datetime.combine(d, time()) + timedelta(seconds=s)
            if offset == 0 and candidate < after:
                continue
            if band_of(candidate, boundaries) not in tried and covers(hours, is_holiday, candidate, candidate + length):
                return max(candidate, after)
    # 試していない時間帯が無い：出なかった直後にかけ直さず、翌日以降の同じ頃から（日を変える）
    return next_opening(hours, is_holiday, after + timedelta(days=1), length, horizon_days=horizon_days)
