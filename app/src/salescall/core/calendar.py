"""祝日の暦（Q12 の材料）。持ち主 CL-2。DESIGN.md 5-2-1 の5。

- 正本は内閣府の「国民の祝日」の公式の一覧（CSV。`OFFICIAL_URL`）。**システムが自分で取りに行って取り込む**（`sync_official`。
  人は介在しない＝2026-10-14 ユーザー）。記憶や手入力では入れない（誤った祝日にかける・かけない、を防ぐ）。
  - **取りに行くのは、要るときだけ**（`needs_fetch`。2026-10-14 ユーザー：「1年で1回、確実に取れたらいい」）：
    翌年の分が手元に無いとき、または最後に取り込んでから1年たったとき（法改正などの直しを拾う）。それ以外は、通信もしない。
    公式の一覧に載るのは翌年の分まで（春分の日・秋分の日は、国が毎年2月に翌年の分を決める）⇒ 実際には年1回（2月ごろ）取れる。
    見守り役は毎日 `sync_official` を呼ぶだけ（要らなければ何もしない。取れなかった日は、次の日にまた試す＝確実に取れるまで）。
  - 中身が前の取り込みと同じなら、何もしない。取りに行けない・形が違う・今年が入っていない（古い・誤った一覧）なら、何も入れず
    `CalendarSyncFailed` を返す（前の暦のまま動く。範囲が切れたら門が止める＝判断できなければ通さない）。知らせは知らせの台帳（Q23）へ。
- 取り込みはシステム共通の取引で行い、同じ取引で、まだ発信していない予約をすべて取り消す（暦は門の判定に効く）。
- 取り込みは追記だけ。年ごとに「その年を含む、いちばん新しい取り込み」が効く（法改正などの直しは、取り込み直し）。
- 取り込んだ範囲の外の日は `CalendarOutOfRange`（判断できない ⇒ 門は通さない）。
- 暦を書くのは、この部品だけ（I-12。app/checks が見張る）。
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
import urllib.request
from collections.abc import Callable
from datetime import date, datetime, timedelta

from salescall.core.access import Access, Actor, Tx

OFFICIAL_URL = "https://www8.cao.go.jp/chosei/shukujitsu/syukujitsu.csv"   # 内閣府「国民の祝日について」の公式の一覧
_MAX_BYTES = 2_000_000          # 公式の一覧は数十KB。これを超えるものは公式の一覧ではない

_HEADER = ("国民の祝日・休日月日", "国民の祝日・休日名称")
_DAY = re.compile(r"^(\d{4})/(\d{1,2})/(\d{1,2})$")


class CalendarOutOfRange(Exception):
    """その日は、取り込んだ暦の範囲の外（祝日かどうか分からない）。"""


class CalendarRejected(Exception):
    """公式の一覧の形ではない（何も取り込まない）。理由は日本語。"""


class CalendarSyncFailed(Exception):
    """公式の一覧を取りに行けなかった・取り込めなかった（何も入れていない。前の暦のまま）。理由は日本語。"""


def fetch_official(timeout: float = 30.0) -> bytes:
    """公式の一覧を取りに行く（HTTPS。証明書を確かめる）。取れなければ CalendarSyncFailed。"""
    try:
        req = urllib.request.Request(OFFICIAL_URL, headers={"User-Agent": "salescall-calendar-sync"})
        with urllib.request.urlopen(req, timeout=timeout) as res:
            if res.status != 200:
                raise CalendarSyncFailed(f"公式の一覧を取れませんでした（応答 {res.status}）")
            data = res.read(_MAX_BYTES + 1)
    except CalendarSyncFailed:
        raise
    except OSError as e:   # 通信の失敗（つながらない・時間切れ・証明書の誤りを含む）
        raise CalendarSyncFailed(f"公式の一覧を取りに行けませんでした（{type(e).__name__}）") from None
    if len(data) > _MAX_BYTES:
        raise CalendarSyncFailed("公式の一覧として大きすぎます")
    return data


REFRESH_AFTER = timedelta(days=365)   # 翌年の分があっても、これだけたったら1回取り直す（法改正などの直しを拾う）


def needs_fetch(covered_until: int | None, last_loaded_at: datetime | None, today: date, now: datetime) -> bool:
    """取りに行く必要があるか（決まりはここ1か所）。翌年の分が無い・最後の取り込みから1年たった、のどちらか。"""
    if covered_until is None or last_loaded_at is None:
        return True
    return covered_until < today.year + 1 or now - last_loaded_at >= REFRESH_AFTER


def sync_official(access: Access, actor: Actor, *, fetch: Callable[[], bytes] = fetch_official) -> dict:
    """要るときだけ公式の一覧を取りに行き、前の取り込みと違えば取り込む（見守り役が毎日呼ぶ）。結果を返す。"""
    state = access.in_system(actor, lambda tx: tx.one(
        "SELECT (SELECT max(last_year) FROM salescall.calendar_loads) AS covered_until,"
        "       (SELECT max(loaded_at) FROM salescall.calendar_loads) AS last_loaded_at,"
        "       salescall.jst(now())::date AS today, now() AS now"))
    if not needs_fetch(state["covered_until"], state["last_loaded_at"], state["today"], state["now"]):
        return {"loaded": False, "reason": "翌年の分まで入っていて、最後の取り込みから1年たっていない（取りに行かない）"}
    data = fetch()

    def work(tx: Tx) -> dict:
        latest = tx.one("SELECT file_sha256 FROM salescall.calendar_loads ORDER BY load_id DESC LIMIT 1")
        if latest is not None and latest["file_sha256"] == hashlib.sha256(data).hexdigest():
            return {"loaded": False, "reason": "前の取り込みと同じ"}
        this_year = tx.one("SELECT salescall.jst(now())::date AS d")["d"].year
        try:
            result = load_official_csv(tx, data, must_cover=this_year)
        except CalendarRejected as e:
            raise CalendarSyncFailed(f"公式の一覧として取り込めませんでした：{e}") from None
        return {"loaded": True, **result}
    return access.in_system(actor, work)


def load_official_csv(tx: Tx, data: bytes, *, must_cover: int | None = None) -> dict:
    """公式の一覧（Shift_JIS の CSV）を取り込む。取り込んだ年の範囲と祝日の数を返す。
    must_cover：その年が入っていなければ断る（取りに行った一覧が古い・誤ったものでないかの確かめ）。"""
    try:
        text = data.decode("cp932")
    except UnicodeDecodeError:
        raise CalendarRejected("Shift_JIS として読めません（内閣府の公式の一覧をそのまま渡してください）") from None
    rows = list(csv.reader(io.StringIO(text, newline=""), strict=True))
    if not rows or tuple(c.strip() for c in rows[0]) != _HEADER:
        raise CalendarRejected("見出しが公式の一覧と違います")
    days: dict[date, str] = {}
    for n, row in enumerate(rows[1:], start=2):
        if not any(c.strip() for c in row):
            continue
        if len(row) != 2:
            raise CalendarRejected(f"{n} 行目の列の数が違います")
        m = _DAY.match(row[0].strip())
        name = row[1].strip()
        if m is None or not name:
            raise CalendarRejected(f"{n} 行目の日付か名前が読めません")
        try:
            d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            raise CalendarRejected(f"{n} 行目の日付がありえない日です") from None
        if d in days:
            raise CalendarRejected(f"{n} 行目の日付が2回出てきます")
        days[d] = name
    if not days:
        raise CalendarRejected("祝日が1つもありません")
    years = sorted({d.year for d in days})
    missing = sorted(set(range(years[0], years[-1] + 1)) - set(years))
    if missing:
        raise CalendarRejected(f"祝日が1つも無い年があります（{missing}）。一覧が途中で切れていないか確かめてください")
    if must_cover is not None and not years[0] <= must_cover <= years[-1]:
        raise CalendarRejected(f"{must_cover} 年が入っていません（古い一覧か、誤った一覧です）")
    load_id = tx.one(
        "INSERT INTO salescall.calendar_loads (file_sha256, first_year, last_year, loaded_by)"
        " VALUES (%s, %s, %s, salescall.cur_actor()) RETURNING load_id",
        (hashlib.sha256(data).hexdigest(), years[0], years[-1]),
    )["load_id"]
    for d, name in sorted(days.items()):
        tx.execute("INSERT INTO salescall.calendar_holidays (load_id, day, name) VALUES (%s, %s, %s)", (load_id, d, name))
    # 暦は門の判定に効く（祝日の時間帯）：同じ取引で、まだ発信していない予約をすべて取り消す（5-2-1 の2）
    tx.one("SELECT salescall.release_claims_in_scope('system', NULL, NULL, NULL)")
    return {"load_id": load_id, "first_year": years[0], "last_year": years[-1], "holidays": len(days)}


def holiday_checker(tx: Tx) -> Callable[[date], bool]:
    """その日が祝日かを答える関数（取引の中で使う。年ごとに1回だけ読む）。範囲の外は CalendarOutOfRange。"""
    by_year: dict[int, frozenset[date]] = {}

    def is_holiday(d: date) -> bool:
        if d.year not in by_year:
            load = tx.one("SELECT load_id FROM salescall.calendar_loads WHERE %s BETWEEN first_year AND last_year"
                          " ORDER BY load_id DESC LIMIT 1", (d.year,))
            if load is None:
                raise CalendarOutOfRange(f"{d.year} 年の祝日の暦がありません")
            by_year[d.year] = frozenset(r["day"] for r in tx.all(
                "SELECT day FROM salescall.calendar_holidays WHERE load_id = %s"
                " AND day BETWEEN make_date(%s, 1, 1) AND make_date(%s, 12, 31)", (load["load_id"], d.year, d.year)))
        return d in by_year[d.year]
    return is_holiday
