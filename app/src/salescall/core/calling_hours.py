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


def clock_seconds(hhmm: str) -> int:
    """設定の時刻（"HH:MM"。24:00 まで）を、0時からの秒にする（1か所。時間帯・お客様の営業時間・言葉ごとの幅が使う）。"""
    h, m = hhmm.split(":")
    return int(h) * 3600 + int(m) * 60


def _ranges(hours: dict, key: str) -> list[tuple[int, int]]:
    return [(clock_seconds(a), clock_seconds(b)) for a, b in hours[key]]


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


# ── お客様が言ったかけ直しの時間の幅（DESIGN.md 7-2。表の作り 0086。2026-10-19 ユーザー：「夕方」「3時まで」などに合わせてかける） ──
def restrict(hours: dict, daily: tuple[int, int] | None, days: tuple[str, ...] | None = None) -> dict:
    """かけてよい時間帯を、1日の中の幅（0時からの秒の [始まり, 終わり)）と、日の種類（曜日・祝日の鍵。「平日」など）に絞った残り
    （曜日・祝日ごと。同じ形）。どちらも無ければそのまま。"""
    out = {}
    for key in hours:
        if days is not None and key not in days:
            out[key] = []
            continue
        lo, hi = daily if daily is not None else (0, _DAY)
        kept = [(max(a, lo), min(b, hi)) for a, b in _ranges(hours, key) if max(a, lo) < min(b, hi)]
        out[key] = [[_hhmm(a), _hhmm(b) if b < _DAY else "24:00"] for a, b in kept]
    return out


def in_window(local: datetime, until_local: datetime | None, daily: tuple[int, int] | None,
              days: tuple[str, ...] | None = None, is_holiday: Callable[[date], bool] | None = None) -> bool:
    """その時刻（日本時間・時差なし）が、お客様が言った幅の中か（門が使う1か所）。until＝幅の終わり（含まない）。
    days＝かけてよい日の種類（曜日・祝日の鍵。祝日かどうかは is_holiday で決める＝暦の外は例外のまま呼び出し元へ）。"""
    if until_local is not None and local >= until_local:
        return False
    if daily is not None and not daily[0] <= _of_day(local) < daily[1]:
        return False
    return days is None or day_key(local.date(), is_holiday) in days


# ── 時間帯の区切り（「別の時間帯にかけ直す」。DESIGN.md 3-5・7-2-1。2026-10-15 ユーザー） ──
def band_of(t: datetime, boundaries: list[int]) -> int:
    """その時刻が、1日のうちのどの時間帯か（0 から。boundaries＝区切りの時、例 [12, 15] ⇒ 午前・昼過ぎ・夕方の3つ）。"""
    h = t.hour + t.minute / 60 + t.second / 3600
    return sum(1 for b in boundaries if h >= b)


def next_opening_in_new_band(hours: dict, is_holiday: Callable[[date], bool], after: datetime, length: timedelta,
                             boundaries: list[int], tried: set[int], *, horizon_days: int = 31) -> datetime | None:
    """after 以降で、まだ試していない時間帯（tried に無いもの）の中の、かけてよい最も早い始まり（日本時間）。
    horizon_days 日のうちに無ければ None（夜だけ開くお店など。どうするかは呼ぶ側が決める）。"""
    for offset in range(horizon_days + 1):
        d = after.date() + timedelta(days=offset)
        starts = {r0 for r0, _ in _ranges(hours, day_key(d, is_holiday))} | {b * 3600 for b in boundaries}
        if offset == 0:
            starts.add(_of_day(after))   # 秒の端数も持つ（切り捨てると after より前になり、いまの時間帯を飛ばす）
        for s in sorted(starts):
            candidate = datetime.combine(d, time()) + timedelta(seconds=s)
            if offset == 0 and candidate < after:
                continue
            if band_of(candidate, boundaries) not in tried and covers(hours, is_holiday, candidate, candidate + length):
                return max(candidate, after)
    return None


# ── かけない時間（NG時間。2026-10-15 ユーザー：「12時から13時に会社の電話にかけるのは失礼。飲食店もご飯時はNG。
#    事業者ごとに、NG時間（繁忙時など）を設定できるように」） ──
def subtract(hours: dict, ng: dict) -> dict:
    """かけてよい時間帯から、かけない時間を引いた残り（曜日・祝日ごと。同じ形）。"""
    out = {}
    for key, ranges in hours.items():
        cuts = sorted(_ranges(ng, key))
        kept: list[tuple[int, int]] = []
        for r0, r1 in sorted(_ranges(hours, key)):
            pieces = [(r0, r1)]
            for c0, c1 in cuts:
                pieces = [q for a, b in pieces for q in ((a, min(b, c0)), (max(a, c1), b)) if q[0] < q[1]]
            kept += pieces
        out[key] = [[_hhmm(a), _hhmm(b)] for a, b in kept]
    return out


def _hhmm(sec: int) -> str:
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}"


def allowed(hours: dict, ng: dict, is_holiday: Callable[[date], bool], start: datetime, end: datetime, *,
            callee_at: datetime | None = None) -> bool:
    """start から end まで（日本時間）かけてよいか（門が使う1か所）：かけてよい時間帯から NG時間を引いた残りの内側。
    ただしお客様が自分で指定した時刻（callee_at）があれば、その時刻を含む「その日のNG時間の枠」の中は、NG時間でもかけてよい
    （2026-10-15 ユーザー：「通常は控えるが、相手が自分で指定した時刻なら、かけてよい」）。かけてよい時間帯はいつも守る。"""
    if covers(subtract(hours, ng), is_holiday, start, end):
        return True
    if callee_at is None or not covers(hours, is_holiday, start, end):
        return False
    a = _of_day(callee_at)
    blocks = [(r0, r1) for r0, r1 in _ranges(ng, day_key(callee_at.date(), is_holiday)) if r0 <= a < r1]
    if not blocks:
        return False
    day = datetime.combine(callee_at.date(), time())
    b0, b1 = day + timedelta(seconds=blocks[0][0]), day + timedelta(seconds=blocks[0][1])
    # 区間を、その枠の中と外に分けて確かめる（枠の外は、NG時間を引いた残りの内側であること）
    inside = (max(start, b0), min(end, b1))
    pieces = [(start, min(end, b0)), (max(start, b1), end)]
    if inside[0] >= inside[1]:
        return False                          # その枠に入っていない（日がずれた・枠の外）
    return all(p0 >= p1 or covers(subtract(hours, ng), is_holiday, p0, p1) for p0, p1 in pieces)
