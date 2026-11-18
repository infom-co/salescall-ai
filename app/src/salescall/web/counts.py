"""数える部品の1か所（docs/DASHBOARD.md 6-5）。ホーム・いつも見える帯・依頼者の概要は、ここだけを読む。

数えるだけ（割合は計算しない）。何を「済み」「判定待ち」とするかは、答える場所（次の予定の最新＝見方 current_next_actions、
業務の結果の最新＝見方 latest_task_results、発信まで進んだか＝salescall.is_dialed）の答えを数える。ここで決め直さない。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from salescall.core.access import Tx
from salescall.web.context import Ctx

_OPEN = ("SELECT event_id, scope::text AS scope, cause::text AS cause, reason, at, client_id, business_id, line_id"
         " FROM salescall.open_stop_events")


def open_stops(ctx: Ctx) -> list[dict[str, Any]]:
    """その契約者で、まだ解かれていない停止のすべて（新しい順）。

    データベースの決まりで、依頼者・業務の範囲の停止は、その依頼者の取引でしか見えない（0007 の stop_scope）。⇒ 契約者の取引で見える
    停止（全体・契約者・回線の範囲）と、依頼者ごとの取引で見える停止を合わせ、同じ停止は1つにする。⛔ 2026-11-18 の見本の画面で、
    契約者の取引だけを読み、依頼者の範囲の停止を数え漏らしていたのを見つけた。各行の `client_id`＝開く・解くときに使う取引の依頼者
    （依頼者・業務の範囲だけ。ほかは None＝契約者の取引）。"""
    found: dict[int, dict[str, Any]] = {r["event_id"]: {**r, "client_id": None} for r in ctx.tenant(lambda tx: tx.all(_OPEN))}
    for c in ctx.clients():
        for r in ctx.client(c["client_id"], lambda tx: tx.all(_OPEN + " WHERE scope IN ('client', 'business')"), read_only=True):
            found[r["event_id"]] = {**r, "client_name": c["name"]}
    return sorted(found.values(), key=lambda r: (r["at"], r["event_id"]), reverse=True)



def stops_for(stops: list[dict[str, Any]], client_id: UUID, business_id: UUID | None = None) -> list[dict[str, Any]]:
    """`open_stops` のうち、その依頼者（業務を渡せば、その業務）の新しい電話に効く停止（依頼者・業務の画面の赤い帯）。
    効く範囲の決まりは1か所：全体・契約者の範囲＝どの依頼者にも効く／依頼者の範囲＝その依頼者／業務の範囲＝その業務（依頼者の
    概要では、その依頼者のどの業務の停止も出す）。回線の範囲は、その回線を使う行だけに効くので、ここには出さない（「止まっているもの」に出る）。"""
    def hits(s: dict[str, Any]) -> bool:
        return SCOPE_HOLDS[s["scope"]](s, client_id, business_id)   # 知らない範囲は KeyError（黙って出さない形にしない）
    return [s for s in stops if hits(s)]


# 停止の範囲ごとに、その依頼者・業務に効くか（stops.SCOPE_NAMES のすべての範囲を書く。試験が突き合わせる）
SCOPE_HOLDS: dict[str, Any] = {
    "system": lambda s, c, b: True,
    "tenant": lambda s, c, b: True,
    "client": lambda s, c, b: s["client_id"] == c,
    "business": lambda s, c, b: s["client_id"] == c and (b is None or s["business_id"] == b),
    "line": lambda s, c, b: False,   # 回線の範囲は、その回線を使う行だけに効く（「止まっているもの」に出る）
}


def client_today(tx: Tx, business_id: UUID | None = None) -> dict[str, int]:
    """依頼者の取引で、今日（日本時間）の数と、行の次の予定ごとの数。business_id を渡せば、その業務の行だけ（業務のタブ）。
    依頼者のタブと業務のタブは、同じこの部品で数える（業務の絞り方は、行 → 実施回 → 業務の1つ）。"""
    today = tx.today()
    only = "" if business_id is None else " AND t.run_id IN (SELECT run_id FROM salescall.runs WHERE business_id = %s)"
    extra = () if business_id is None else (business_id,)
    dialed = tx.one("SELECT count(*) AS n FROM salescall.attempts a JOIN salescall.tasks t ON t.task_id = a.task_id"
                    " WHERE salescall.is_dialed(a.state) AND (a.claimed_at AT TIME ZONE 'Asia/Tokyo')::date = %s" + only,
                    (today, *extra))["n"]
    kinds = {r["kind"]: r["n"] for r in tx.all(
        "SELECT coalesce(n.kind::text, 'none') AS kind, count(*) AS n FROM salescall.tasks t"
        " LEFT JOIN salescall.current_next_actions n ON n.task_id = t.task_id WHERE true" + only + " GROUP BY 1", extra)}
    return {"dialed_today": dialed, "call": kinds.get("call", 0), "hold": kinds.get("hold", 0),
            "end": kinds.get("end", 0), "none": kinds.get("none", 0)}
