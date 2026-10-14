"""祝日の暦（Q12 の材料）。持ち主 CL-2。DESIGN.md 5-2-1 の5。

- 正本は内閣府の「国民の祝日」の公式の一覧（CSV：https://www8.cao.go.jp/chosei/shukujitsu/syukujitsu.csv）。
  運営者がそのファイルを取り込む（`load_official_csv`）。記憶や手入力では入れない（誤った祝日にかける・かけない、を防ぐ）。
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
from collections.abc import Callable
from datetime import date

from salescall.core.access import Tx

_HEADER = ("国民の祝日・休日月日", "国民の祝日・休日名称")
_DAY = re.compile(r"^(\d{4})/(\d{1,2})/(\d{1,2})$")


class CalendarOutOfRange(Exception):
    """その日は、取り込んだ暦の範囲の外（祝日かどうか分からない）。"""


class CalendarRejected(Exception):
    """公式の一覧の形ではない（何も取り込まない）。理由は日本語。"""


def load_official_csv(tx: Tx, data: bytes) -> dict:
    """公式の一覧（Shift_JIS の CSV）を取り込む。取り込んだ年の範囲と祝日の数を返す。"""
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
