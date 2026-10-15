"""利用量の台帳と予算（Q16「いくら使ったか」。DESIGN.md 12-1・9-1 の budget）。持ち主 CL-2。

- 台帳に書くのは、この部品の `record` だけ（I-12。app/checks が見張る）。単価を足すのは `set_price` だけ（システム共通）。
- 1行ごとに：出どころ（会話AI・別の耳・回線）・実測か推定か・量・単位・使った単価・金額。金額は記録したときの単価で決まる。
  単価が無い組は記録できない（PriceMissing。推測で 0円にしない）。
- 回線：Rakuten Link は明細を返さないので、締めの処理が「話していた秒数 × 単価」の推定を書く（`record_line_estimate`）。
  会話AI・別の耳：返ってきた量を実測として、返らなければ推定として、会話の処理・検証（CL-1）が `record` を呼ぶ。
- 予算（1日の上限。日本時間の日）：依頼主の上限を超えたら、記録した取引の中で、その依頼主を止める（原因 budget）。
  全体の上限と、日が替わったときの解除は、見守りが呼ぶ `tick`（全体の取引から）。門は停止を見るだけ（判断を増やさない）。
- 画面は台帳を読み、実測と推定を分けて出す（通話時間から料金を計算し直さない）。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import UUID

from salescall.core import settings, stops
from salescall.core.access import Access, Actor, Tx


class PriceMissing(Exception):
    """その出どころ・単位の単価が無い（記録できない）。"""


def set_price(tx: Tx, *, source: str, unit: str, yen_per_unit: Decimal, valid_from: Any, note: str) -> int:
    """単価を足す（システム共通。全体の取引で）。あとから足した単価は、その時刻からの記録に使う。"""
    return tx.one("INSERT INTO salescall.price_versions (source, unit, yen_per_unit, valid_from, note, set_by)"
                  " VALUES (%s, %s, %s, %s, %s, salescall.cur_actor()) RETURNING price_id",
                  (source, unit, yen_per_unit, valid_from, note))["price_id"]


def record(tx: Tx, *, source: str, kind: str, quantity: Decimal | int, unit: str,
           attempt_id: UUID | None = None) -> dict[str, Any]:
    """利用量を1行記録する（取引の依頼主の分）。依頼主の1日の上限を超えたら、その依頼主を止める（同じ取引）。"""
    price = tx.one("SELECT price_id, yen_per_unit FROM salescall.price_versions WHERE source = %s AND unit = %s"
                   " AND valid_from <= now() ORDER BY valid_from DESC LIMIT 1", (source, unit))
    if price is None:
        raise PriceMissing(f"単価がありません：{source}・{unit}")
    amount = Decimal(quantity) * Decimal(price["yen_per_unit"])
    row = tx.one(
        "INSERT INTO salescall.usage_entries (tenant_id, client_id, attempt_id, source, kind, quantity, unit, price_id,"
        " amount_yen) VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, %s, %s, %s, %s, %s)"
        " RETURNING entry_id, tenant_id, client_id",
        (attempt_id, source, kind, quantity, unit, price["price_id"], amount))
    stopped = _check_client_budget(tx, row["tenant_id"], row["client_id"])
    return {"entry_id": row["entry_id"], "amount_yen": amount, "stopped": stopped}


def spent_today(tx: Tx) -> Decimal:
    """取引の依頼主の、今日（日本時間）の金額。"""
    return tx.one("SELECT coalesce(sum(amount_yen), 0) AS y FROM salescall.usage_entries"
                  " WHERE salescall.jst(occurred_at)::date = salescall.jst(now())::date")["y"]


def _open_budget_stop(tx: Tx, scope: str) -> bool:
    return tx.one("SELECT 1 FROM salescall.stop_events s WHERE s.action = 'stop' AND s.cause = 'budget' AND s.scope = %s"
                  " AND NOT EXISTS (SELECT 1 FROM salescall.stop_events r WHERE r.resolves_event_id = s.event_id)"
                  " LIMIT 1", (scope,)) is not None


def _check_client_budget(tx: Tx, tenant_id: UUID, client_id: UUID) -> int | None:
    limit = Decimal(settings.resolve(tx, "budget.client_daily_yen", tenant_id=tenant_id, client_id=client_id,
                                     business_id=None))
    spent = spent_today(tx)
    if spent <= limit or _open_budget_stop(tx, "client"):
        return None
    return stops.stop(tx, scope="client", cause="budget", reason=f"今日の費用が上限を超えた（{spent}円 ＞ {limit}円）",
                      evidence="利用量の台帳", tenant_id=tenant_id, client_id=client_id)["event_id"]


def record_line_estimate(tx: Tx, attempt_id: UUID) -> dict[str, Any] | None:
    """回線の料金の推定（締めの処理が呼ぶ）：相手が出てから終わるまでの秒数 × 単価。相手が出なかった通話は書かない。"""
    row = tx.one(
        "SELECT extract(epoch FROM a.ended_at - h.at) AS seconds FROM salescall.attempts a"
        " JOIN LATERAL (SELECT min(at) AS at FROM salescall.attempt_state_history"
        "               WHERE attempt_id = a.attempt_id AND to_state = 'answered') h ON true"
        " WHERE a.attempt_id = %s AND a.ended_at IS NOT NULL AND h.at IS NOT NULL", (attempt_id,))
    if row is None:
        return None
    return record(tx, source="line", kind="estimated", quantity=Decimal(row["seconds"]).quantize(Decimal("0.001")),
                  unit="seconds", attempt_id=attempt_id)


def tick(access: Access, actor: Actor) -> dict[str, Any]:
    """見守りが決まった間隔で呼ぶ：全体の1日の上限を超えたら全体を止める。日が替わった費用の停止を解く。"""
    def system(tx: Tx) -> int | None:
        limit = Decimal(settings.system_value(tx, "budget.system_daily_yen"))
        spent = tx.one("SELECT salescall.spent_today_all() AS y")["y"]
        if spent <= limit or _open_budget_stop(tx, "system"):
            return None
        return stops.stop(tx, scope="system", cause="budget", reason=f"全体の今日の費用が上限を超えた（{spent}円 ＞ {limit}円）",
                          evidence="利用量の台帳")["event_id"]
    stopped = access.in_system(actor, system)
    resumed = []
    for s in access.in_system(actor, lambda tx: tx.all(
            "SELECT b.*, salescall.jst(now())::date AS today FROM salescall.open_budget_stops() b")):
        if s["stopped_on"] >= s["today"]:
            continue
        work = lambda tx: stops.resume(tx, s["event_id"], reason="日が替わった（費用の上限の期間）")  # noqa: E731
        if s["scope"] == "system":
            resumed.append(access.in_system(actor, work))
        else:
            resumed.append(access.in_client(actor, s["tenant_id"], s["client_id"], work))
    return {"stopped": stopped, "resumed": resumed}
