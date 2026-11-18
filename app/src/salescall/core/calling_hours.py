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

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any

from salescall.link.jst import JST   # 日本時間（決まりは link.jst の1か所。ここから使う部品のために名前を残す）


def local(t: datetime) -> datetime:
    """時刻を、日本時間の時差を持たない日時にする（Python の側の1か所。SQL の中では salescall.jst）。"""
    return t.astimezone(JST).replace(tzinfo=None)


def from_local(t: datetime) -> datetime:
    """日本時間の時差を持たない日時を、時刻に戻す（Python の側の1か所。SQL の中では salescall.from_jst）。"""
    return t.replace(tzinfo=JST)


def label(t: datetime) -> str:
    """時刻を、契約者への知らせに出す日本時間の形にする（1か所。例：12月15日15時・12月15日15時30分。年は出さない）。"""
    t = local(t)
    return f"{t.month}月{t.day}日{t.hour}時" + (f"{t.minute}分" if t.minute else "")


DAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
HOURS_KEYS = DAY_KEYS + ("holiday",)   # 時間帯の値の鍵（曜日ごと＋祝日）
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


def daily_spans(ranges: Sequence[tuple[int, int]]) -> list[tuple[int, int]]:
    """1日の中の時刻の範囲の並び（設定の値の形 daily_ranges を秒にしたもの。それぞれ終わりが始まりより前なら日をまたぐ＝[18時, 3時) は
    18時から翌3時まで。[0時, 24時) は1日じゅう）を、1日の中の区間（0時からの秒の [始まり, 終わり)。重なりは1つにまとめる）にする
    （決まりの1か所。`in_daily`・`daily_seconds` が使う）。2026-10-25 ユーザー：「止める回線を2択だけじゃなく、こちらが決められるように
    して。昼間に私は寝ていることがあるし、夜中でも起きていることがある。0～24時の選択は残したうえで。」"""
    spans: list[tuple[int, int]] = []
    for a, b in ranges:
        spans += [(a, b)] if a < b else [(a, _DAY), (0, b)]
    return _union([s for s in spans if s[0] < s[1]])


def in_daily(local: datetime, ranges: Sequence[tuple[int, int]]) -> bool:
    """その時刻（日本時間・時差なし）が、1日の中の時刻の範囲の並び（`daily_spans` と同じ決まり）のどれかの内か（1か所）。"""
    t = _of_day(local)
    return any(a <= t < b for a, b in daily_spans(ranges))


def daily_seconds(start: datetime, end: datetime, ranges: Sequence[tuple[int, int]]) -> float:
    """start から end まで（日本時間・時差なし）のうち、1日の中の時刻の範囲の並び（`daily_spans` と同じ決まり）に入る秒の合計（1か所）。"""
    if end <= start:
        return 0.0
    spans = daily_spans(ranges)
    total = 0.0
    for offset in range((end.date() - start.date()).days + 1):
        day = datetime.combine(start.date() + timedelta(days=offset), time())
        for s0, s1 in spans:
            lo, hi = max(start, day + timedelta(seconds=s0)), min(end, day + timedelta(seconds=s1))
            total += max(0.0, (hi - lo).total_seconds())
    return total


def next_opening(hours: dict, is_holiday: Callable[[date], bool], after: datetime, length: timedelta,
                 *, horizon_days: int = 31, back_to: datetime | None = None, ends: dict | None = None) -> datetime | None:
    """after 以降で、length の長さの区間がまるごと時間帯に入る、最も早い始まり（日本時間）。horizon_days 日のうちに無ければ None。
    back_to（after より前）があれば、after に始められないとき、先に after より前を探す：区間（予約から予約証の期限まで）が back_to から
    after までの間に重なる（始まりが after 以前・終わりが back_to 以後）、after にいちばん近い（最も遅い）始まり（`_latest_before`）。
    ends＝NG時間を引く前の依頼者の時間帯（back_to と一緒に渡す）：前を探すとき、期限を置いてよい終わりは、その時間帯そのものの終わり
    （こちらの都合の終わり）だけ。NG時間を引いてできた終わり（その後ろがNG時間）には置かない（✅ 2026-11-14 ユーザー「13時にかける
    （おすすめ）」）。区間を時間帯で見る決まり（`covers`）は、どちらも同じ1か所。"""
    if covers(hours, is_holiday, after, after + length):
        return after
    if back_to is not None and back_to < after:
        if ends is None:
            raise ValueError("前を探すときは、NG時間を引く前の時間帯（ends）も渡してください")
        found = _latest_before(hours, ends, is_holiday, after, back_to - length, length)
        if found is not None:
            return found
    for offset in range(horizon_days + 1):
        d = after.date() + timedelta(days=offset)
        for r0, _ in sorted(_ranges(hours, day_key(d, is_holiday))):
            candidate = datetime.combine(d, time()) + timedelta(seconds=r0)
            if candidate >= after and covers(hours, is_holiday, candidate, candidate + length):
                return candidate
    return None


def _latest_before(hours: dict, ends: dict, is_holiday: Callable[[date], bool], before: datetime, lowest: datetime,
                   length: timedelta) -> datetime | None:
    """lowest から before までの始まりのうち、length の長さの区間がまるごと時間帯（hours）に入り、終わりが ends（NG時間を引く前の依頼者の
    時間帯）の区間の終わりちょうどに来る、最も遅い始まり（`next_opening` の back_to だけが呼ぶ）。終わりちょうどにかけてよいのは、その後ろが
    こちらの都合の終わり（依頼者の時間帯の終わり）のときだけ（✅ 2026-11-14 ユーザー「13時にかける（おすすめ）」＝後ろがNG時間なら、NG時間が明ける
    のを待つ）。before に始められないとき、時間帯に入る最も遅い始まりは、どれかの区間の終わりの length 前なので、ほかの始まりは見なくてよい。"""
    found = []
    for offset in range((before.date() - lowest.date()).days + 1):
        day = datetime.combine(lowest.date() + timedelta(days=offset), time())
        for _, r1 in _ranges(ends, day_key(day.date(), is_holiday)):
            t = day + timedelta(seconds=r1) - length
            if lowest <= t <= before and covers(hours, is_holiday, t, t + length):
                found.append(t)
    return max(found, default=None)


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
        out[key] = [[_hhmm(a), _hhmm(b)] for a, b in kept]
    return out


def in_window(local: datetime, until_local: datetime | None, daily: tuple[int, int] | None,
              days: tuple[str, ...] | None = None, is_holiday: Callable[[date], bool] | None = None, *,
              since_local: datetime | None = None, recover_until_local: datetime | None = None) -> bool:
    """その時刻（日本時間・時差なし）が、お客様が言った幅の中か（門が使う1か所）。[since, until)＝いまの回（since は含む・until は
    含まない）。days＝かけてよい日の種類（曜日・祝日の鍵。祝日かどうかは is_holiday で決める＝暦の外は例外のまま呼び出し元へ）。
    ⛔ 2026-10-22 の見直し14：前は回の始まりを見ず、「週末なら」の約束を、お客様が言っていない金曜にかけえた。
    recover_until_local＝こちらの止めで約束の時刻にかけられなかった回を、その日のうちに取り戻す電話の期限（次の予定の列 window_recover_until。
    ✅ 2026-11-08 ユーザー＝計画書 7-3 (4)「復旧しだい今日」。0216）：その回は、約束の時刻の1回分でなく、この期限まで（1日の中の幅は見ない。
    かけてよい時間はお客様指定の歯止め時間＝`callee_hours`）。"""
    if recover_until_local is not None:
        until_local, daily = recover_until_local, None
    if until_local is not None and local >= until_local:
        return False
    if since_local is not None and local < since_local:
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


def intersect(values: list[dict]) -> dict:
    """曜日・祝日ごとの時間帯の重なり（1か所。設定の合成の「厳しいほう（範囲）」と、依頼者の歯止めの内側に収めるときに使う）。
    どこかの階層でかけない曜日は、かけない。"""
    out = {}
    for key in HOURS_KEYS:
        ranges = _ranges(values[0], key)
        for v in values[1:]:
            ranges = [(max(a1, a2), min(b1, b2)) for a1, b1 in ranges for a2, b2 in _ranges(v, key) if max(a1, a2) < min(b1, b2)]
        out[key] = [[_hhmm(a), _hhmm(b)] for a, b in sorted(ranges)]
    return out


def excuse_ng(ng: dict, of_day: int, keys: tuple[str, ...] | None = None) -> dict:
    """お客様が自分で指定した時刻（0時からの秒）を含むNG時間の枠を外したNG時間（1か所。曜日・祝日ごと。keys＝外す曜日・祝日の鍵、
    None なら全部）。2026-10-15 ユーザー：「NG時間は通常は控えるが、相手が自分で指定した時刻なら、かけてよい」＝その時刻を含む
    枠の中だけ。ほかの枠は控える。門（`allowed`）も、時刻ちょうどの約束の回の時刻を探す次の動きも、これを使う。"""
    return {key: [r for r in ranges if keys is not None and key not in keys
                  or not clock_seconds(r[0]) <= of_day < clock_seconds(r[1])] for key, ranges in ng.items()}


def _hhmm(sec: int) -> str:
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}"


def _union(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for a, b in sorted(ranges):
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


# 「〇時まで」の言い方の、かけてよい時間の決め方（設定 callback.until_* の選択肢と同じ。決めた幅を作るのは spoken_time.read の1か所）：
# now＝今から〇時まで／after＝〇時から（「〇時以降」と同じ）／slot_after＝〇時から1回分（exact_slot）
UNTIL_RULES = ("now", "after", "slot_after")
UNTIL_SETTINGS = {"other_party": "callback.until_other_party", "unavailable": "callback.until_unavailable",
                  "preparing": "callback.until_preparing"}   # 「〇時まで」の理由（call_report.UNTIL_WHYS）→ その決め方の設定


@dataclass(frozen=True)
class CalleeSettings:
    """お客様の時間の設定（読むのは gate.callee_settings の1か所。判定・門・次の動きが同じものを使う。0時からの秒）：
    business_hours＝お客様の営業時間（設定 callee.business_hours。午前・午後の読み分けと、お客様が言った幅の言葉で決まっていない端を切る
    ＝2026-11-08 ユーザー。0216）・designated_limit＝お客様指定の歯止め時間（設定 callee.designated_limit。業務ごと。お客様が言った幅は、
    この外にはかけない。営業時間で切ると空になるときだけ、端を埋める）・exact_slot＝時刻ちょうどの
    約束のその日の時間と、歯止め時間の外の時刻を確かめて「はい」と言われたときの1回分（設定 callback.exact_slot）。⛔ 見直し17b：前は
    門と締めが別々に読み、時刻の読み・判定の引数に初めの値（7〜22時・1時間）の写しがあって、渡し忘れても黙って初めの値になった。
    until_rules＝「〇時まで」の言い方の、いま電話を続けられなかった理由（call_report.UNTIL_WHYS）ごとの、かけてよい時間の決め方
    （UNTIL_RULES。設定 callback.until_*。業務ごと。2026-10-23 ユーザー：「かけ直さないといけない理由によるでしょ」）。
    periods＝言葉ごとの1日の中の幅（「朝」「夕方」など → 0時からの秒の [始まり, 終わり)。依頼者ごとの設定 callback.period_*＝0088 を
    spoken_time.periods_from で言葉の表にしたもの。2026-10-19 ユーザー「OK」。時刻の読み spoken_time.read が読む）。⛔ 見直し23 の (2)：
    前は時刻の読みと判定の入力に、初めの値の写し（spoken_time.DEFAULT_PERIODS・PERIODS）があり、渡し忘れても黙って初めの値になった。"""
    business_hours: tuple[int, int]
    designated_limit: tuple[int, int]
    exact_slot: int
    until_rules: tuple[tuple[str, str], ...]
    periods: tuple[tuple[str, tuple[int, int]], ...]

    def until_rule(self, why: str | None) -> str | None:
        """「〇時まで」の理由の、かけてよい時間の決め方（1か所）。理由が分からなければ None（決めない＝通話の中で確かめる）。"""
        return None if why is None else dict(self.until_rules)[why]

    def period_table(self) -> dict[str, tuple[int, int]]:
        """言葉ごとの幅の表（時刻の読み spoken_time.read に渡す形）。"""
        return dict(self.periods)

    def to_json(self) -> dict[str, Any]:
        """判定の記録（snapshot）と、判定し直しの比べに使う形（設定を直したら判定し直す）。"""
        return {"business_hours": list(self.business_hours), "designated_limit": list(self.designated_limit),
                "exact_slot": self.exact_slot, "until_rules": dict(self.until_rules),
                "periods": {w: list(r) for w, r in self.periods}}


@dataclass(frozen=True)
class CalleeWindow:
    """お客様が言った時刻・時間の幅の約束（次の予定の幅）のうち、かけてよい時間帯を決める材料（1か所。門と次の動きが同じものを使う）。
    daily＝1日の中の幅（0時からの秒の [始まり, 終わり)。時間の言葉があるときだけ。言葉の表の端・数の端。端の決まっていない側は
    0時・24時）・days＝日の種類（曜日・祝日の鍵）・said＝daily の始まり・終わりを、お客様が実際に口にしたか（「4時以降」の4時・
    「3時まで」の3時・時刻ちょうどは両方。言葉の表の端は口にしていない）・day_said＝お客様がその日を言った（依頼者の休みの曜日・
    祝日でもかける。日を言っていない今日の回も言った日＝見直し16）。recover＝こちらの止めで約束の時刻にかけられなかった時刻ちょうどの
    約束の回を、その日のうちに取り戻す電話（次の予定の列 window_recover_until がある。かけてよいのは、お客様指定の歯止め時間の中。0216）。"""
    daily: tuple[int, int] | None
    days: tuple[str, ...] | None
    said: tuple[bool, bool] | None
    day_said: bool
    recover: bool = False


def callee_window(row: dict) -> CalleeWindow | None:
    """次の予定の記録（列 window_*）の、かけてよい時間帯を決める材料。幅を持たない予定は None（1か所）。"""
    if all(row.get(k) is None for k in ("window_until", "window_daily", "window_days", "window_from")):
        return None
    t = lambda v: None if v is None else tuple(v)   # noqa: E731
    return CalleeWindow(t(row["window_daily"]), t(row["window_days"]), t(row.get("window_said")), bool(row.get("window_day_said")),
                        row.get("window_recover_until") is not None)


def callee_range(daily: tuple[int, int], said: tuple[bool, bool] | None,
                 limit: tuple[int, int], beyond_slot: int | None = None, *, made: bool = False,
                 business: tuple[int, int]) -> tuple[int, int] | None:
    """お客様が言った1日の中の幅から、かけてよい [始まり, 終わり)（1か所。2026-10-23 ユーザー：「『朝』も時間帯が決まっているでしょ」
    「お客様の日時指定の場合は、『お客様指定の歯止め時間』で見るようにしないといけない」）：
    - 幅はすべてお客様の指定（言葉の表の幅：朝〜10時・午前中〜12時・午後一13〜14時・午後13時〜・夕方16〜18時・夜18〜20時、数の幅：
      「3時まで」「4時以降」「3時から5時」・時刻ちょうど）。依頼者の時間帯・NG時間・依頼者の深夜早朝の歯止めでは切らない（呼ぶ側
      `callee_hours`）。
    - 端の決まっていない側（朝・午前中の始まり、午後の終わり、「〜まで」の始まり、「〜以降」の終わり＝0時・24時でお客様が口にして
      いない端）は、お客様の営業時間 business（設定 callee.business_hours。業務ごと。初め 9:00〜19:00。日をまたいでよい）を、お客様指定の
      歯止め時間 limit（設定 callee.designated_limit。業務ごと。初め 7:00〜22:00）の中に絞った時間で切る（✅ 2026-11-08 ユーザー＝計画書 7-3 (3)
      「1日3回・営業時間内」。⛔ 0118〜0215：limit で埋め、「3時以降」に夜21時半までかけた）。営業時間で切ると空になるとき（営業時間が12時
      からのお店の「朝」・「20時以降」）だけ、前の決まりのとおり limit で埋める（2026-10-23 ユーザー：営業時間で埋めると、営業時間が12時からの
      お店で「朝」にかからなくなる）。言葉の表の端（「夕方」の16時・18時など。0時・24時でない端）は、言葉で決まった端なので limit でだけ切る。
    - お客様が実際に口にした端（時刻ちょうど・「6時以降」の6時など）は、limit の外でも切らない（2026-10-22 ユーザー：「お客様が
      指定した時刻ならNG時間でもかけてよい（歯止めなし、ただし、午前と午後を間違えないように）」。2026-10-23 ユーザー「OK」）。
    - 口にした端が limit の外で、反対の端を limit で埋めると空になるとき（「23時以降なら」「明日の朝6時まで」）：通話の中で確かめて
      「はい」と言われたときだけ（beyond_slot＝1回分の長さ。設定 callback.exact_slot＝時刻ちょうどの約束と同じ）、口にした端から
      1回分（「23時以降」＝23時から・「6時まで」＝6時の前の1回分）。確かめていなければ None（2026-10-23 ユーザー：「案B＆案A
      (確かめたうえで案A)」）。
    - made＝幅の終わりを、お客様が口にした始まりの時刻から、こちらが作った（1回分。時刻ちょうどの約束の回・「〇時までに確認して
      おきます」の「〇時から1回分」）。作った終わりは「口にした端」ではない：始まりが歯止め時間の中なら、終わりを歯止め時間で切る
      （「21時45分」の約束は 21:45〜22:00）。始まりが歯止め時間の外なら、作った幅は、通話の中で確かめて「はい」と言われたときだけ
      （beyond_slot。口にした時刻から1回分）。確かめていなければ None（⛔ 見直し18c の (2)・18a の (4)：前は作った終わりも口にした端に
      入れ、「23時までに確認しておきます」を確かめずに 23:00〜23:30、「21時45分」の約束に 22:00・22:15 にもかけた）。
    空なら None。"""
    lo, hi = daily
    said_lo, said_hi = said or (False, False)
    if made:
        if limit[0] <= lo < limit[1]:
            return lo, min(hi, limit[1])
        return None if beyond_slot is None else (lo, min(lo + beyond_slot, _DAY))
    open_hours = [(max(a, limit[0]), min(b, limit[1])) for a, b in daily_spans([business]) if max(a, limit[0]) < min(b, limit[1])]
    got = _edges(lo, hi, said_lo, said_hi, limit, open_hours) or _edges(lo, hi, said_lo, said_hi, limit, [limit])
    if got is not None:
        return got
    if beyond_slot is None or said_lo == said_hi:   # 確かめていない・口にした端が1つでない（両方口にした幅は空にならない）
        return None
    return (lo, min(lo + beyond_slot, _DAY)) if said_lo else (max(hi - beyond_slot, 0), hi)


def _edges(lo: int, hi: int, said_lo: bool, said_hi: bool, limit: tuple[int, int],
           open_hours: list[tuple[int, int]]) -> tuple[int, int] | None:
    """`callee_range` の端の決め方（そこだけが呼ぶ）：口にした端はそのまま・言葉の表の端（0時・24時でない端）は limit で切る・端の決まって
    いない側（0時・24時）は open_hours（1日の中の区間の並び）の始まり・終わりで切る。空なら None。"""
    a = lo if said_lo else max(lo, limit[0]) if lo > 0 else min((s for s, e in open_hours if e > lo), default=None)
    if a is None:
        return None
    b = hi if said_hi else min(hi, limit[1]) if hi < _DAY else max((e for s, e in open_hours if e > a and s < hi), default=None)
    return (a, b) if b is not None and a < b else None


def callee_hours(hours: dict, ng: dict, guard: dict, limit: tuple[int, int], w: CalleeWindow, *,
                 business: tuple[int, int]) -> dict:
    """お客様が言った時刻・時間の幅（約束の幅）でかける電話の、かけてよい時間帯（曜日・祝日ごと。1か所。門の時刻の確かめ
    `allowed` と、次の動きが幅の中を探す所が、同じもの＝gate.window_hours を使う）。
    - 時間の言葉・時刻のある幅（w.daily）：`callee_range` の1か所（お客様の指定なので、依頼者の時間帯・NG時間の外でもかける。端の
      決まっていない側はお客様の営業時間 business で切る。口にした端は limit の外でも）。
    - 取り戻す電話（w.recover。こちらの止めで約束の時刻にかけられなかった時刻ちょうどの約束の回を、その日のうちに）：お客様指定の歯止め
      時間 limit の中（✅ 2026-11-08 ユーザー＝計画書 7-3 (4)「お客様指定の歯止めの中で」。0216）。
    - 日だけの幅（「13日なら」「週末なら」。時間を言っていない）：その日の依頼者のいつもの時間（依頼者の時間帯から NG時間を引いた
      もの。依頼者の時間帯が1日も無いときは歯止め）（2026-10-22 ユーザー「OK」）。
    - お客様がその日を言った（w.day_said。「土曜の夕方」「週末なら」「13日なら」・日を言っていない今日の回）：依頼者の休みの曜日・
      祝日でもかける（時間を言っていない幅は、依頼者のほかの日の時間を当てる）。言っていない（「夕方なら」の次の日の回）：依頼者の
      かける日だけ（休みの日は、かけられる時刻の無い回＝数えずに飛ばす。次の動き `_window_opening`）。
    w.days＝日の種類（曜日・祝日の鍵）で絞る。"""
    base_all = hours if any(hours.get(k) for k in HOURS_KEYS) else guard
    usual = [[_hhmm(a), _hhmm(b)] for a, b in _union([r for key in HOURS_KEYS for r in _ranges(base_all, key)])]
    out = {}
    for key in HOURS_KEYS:
        own = base_all.get(key) or []
        if (w.days is not None and key not in w.days) or not (own or w.day_said):
            out[key] = []
            continue
        if w.recover:
            out[key] = [[_hhmm(limit[0]), _hhmm(limit[1])]]
            continue
        if w.daily is not None:
            got = callee_range(w.daily, w.said, limit, business=business)
            out[key] = [] if got is None else [[_hhmm(got[0]), _hhmm(got[1])]]
            continue
        base = subtract({key: own or usual}, {key: ng.get(key) or []})
        out[key] = [[_hhmm(a), _hhmm(b)] for a, b in _union(_ranges(base, key))]
    return out


def allowed(hours: dict, ng: dict, is_holiday: Callable[[date], bool], start: datetime, end: datetime, *,
            callee_at: datetime | None = None, window: dict | None = None) -> bool:
    """start から end まで（日本時間）かけてよいか（門が使う1か所）：かけてよい時間帯から NG時間を引いた残りの内側。
    window＝お客様が言った幅の約束の電話の、かけてよい時間帯（`callee_hours` の答え＝gate.window_hours）：その内側（お客様の指定
    なので、依頼者の時間帯・NG時間の外でもかける。端の決まっていない側は、お客様指定の歯止め時間で埋めて切る＝2026-10-22・10-23 ユーザー）。幅の約束でない電話で、お客様が自分で指定した時刻（callee_at）があれば、
    その日は、その時刻を含むNG時間の枠の中もかけてよい（`excuse_ng`。2026-10-15 ユーザー）。"""
    if window is not None:
        return covers(window, is_holiday, start, end)
    if covers(subtract(hours, ng), is_holiday, start, end):
        return True
    if callee_at is None or start.date() != callee_at.date():
        return False                          # 日がずれた：ふつうどおり控える
    excused = excuse_ng(ng, _of_day(callee_at), (day_key(callee_at.date(), is_holiday),))
    return covers(subtract(hours, excused), is_holiday, start, end)