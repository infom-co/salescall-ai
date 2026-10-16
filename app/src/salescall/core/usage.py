"""利用量の台帳と予算（Q16「いくら使ったか」。DESIGN.md 12-1・9-1 の budget）。持ち主 CL-2。

- 台帳に書くのは、この部品の `record` だけ（I-12。app/checks が見張る）。単価を足すのは `set_price` だけ（システム共通）。
- 1行ごとに：出どころ（会話AI・別の耳・回線）・実測か推定か・量・単位・使った単価・金額。金額は記録したときの単価で決まる。
  単価が無い組は記録できない（PriceMissing。推測で 0円にしない）。
- 回線：Rakuten Link は明細を返さないので、締めの処理が「話していた秒数 × 単価」の推定を書く（`record_line_estimate`）。
  会話AI・別の耳：返ってきた量を実測として、返らなければ推定として、会話の処理・検証（CL-1）が `record` を呼ぶ。
- 予算（1日の上限。日本時間の日）：依頼者の上限を超えたら、記録した取引の中で、その依頼者を止める（原因 budget）。
  全体の上限と、日が替わったときの解除は、見守りが呼ぶ `tick`（全体の取引から）。門は停止を見るだけ（判断を増やさない）。
- **報酬に対する費用の割合**（2026-10-15 ユーザー決定。今月の数は core/reward.py の `month`）：記録のたびに、その依頼者の今月を見る。
  どちらの判断も、成果・話せた本数のぶれを多めに見込んだ報酬（`upper`。確かさは設定 budget.judge_confidence）で行う
  ＝判断し始める本数を決め打ちにせず、報酬と費用から逆算される。
  - 赤字（今月の費用 ＞ 多めに見込んだ報酬）⇒ その依頼者を止める（原因 deficit）。報酬の決まりが無い依頼者は、報酬 0 円なので、
    費用がかかれば赤字として止まる（決まりを入れ忘れたまま、費用だけかけ続けない）。
  - 赤字でなく、割合（費用 ÷ 多めに見込んだ報酬）が上限（budget.max_cost_ratio。既定 30％。依頼者ごとに選べる）を超えた
    ⇒ 知らせるだけ（1日1回）。
  - 赤字の停止は、見守りの `tick` が今月の数を見直し、赤字でなくなったら解く（報酬の決まりを直した・月が替わった・月額の日割りが
    積み上がった）。
- 画面は台帳を読み、実測と推定を分けて出す（通話時間から料金を計算し直さない）。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import UUID

from salescall.core import notify, reward, settings, stops
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
    """利用量を1行記録する（取引の依頼者の分）。依頼者の1日の上限を超えたら、その依頼者を止める（同じ取引）。"""
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
    by_ratio = _check_cost_ratio(tx, row["tenant_id"], row["client_id"])
    return {"entry_id": row["entry_id"], "amount_yen": amount, "stopped": stopped or by_ratio}


def spent_today(tx: Tx) -> Decimal:
    """取引の依頼者の、今日（日本時間）の金額。"""
    return tx.one("SELECT coalesce(sum(amount_yen), 0) AS y FROM salescall.usage_entries"
                  " WHERE salescall.jst(occurred_at)::date = salescall.jst(now())::date")["y"]


def _open_stop(tx: Tx, scope: str, cause: str) -> bool:
    return tx.one("SELECT 1 FROM salescall.stop_events s WHERE s.action = 'stop' AND s.cause::text = %s AND s.scope = %s"
                  " AND NOT EXISTS (SELECT 1 FROM salescall.stop_events r WHERE r.resolves_event_id = s.event_id)"
                  " LIMIT 1", (cause, scope)) is not None


def _check_client_budget(tx: Tx, tenant_id: UUID, client_id: UUID) -> int | None:
    limit = Decimal(settings.resolve(tx, "budget.client_daily_yen", tenant_id=tenant_id, client_id=client_id,
                                     business_id=None))
    spent = spent_today(tx)
    if spent <= limit or _open_stop(tx, "client", "budget"):
        return None
    return stops.stop(tx, scope="client", cause="budget", reason=f"今日の費用が上限を超えた（{spent}円 ＞ {limit}円）",
                      evidence="利用量の台帳", tenant_id=tenant_id, client_id=client_id)["event_id"]


def _deficit(tx: Tx, tenant_id: UUID, client_id: UUID) -> tuple[bool, dict[str, Any]]:
    """取引の依頼者が、いま赤字として止めるべきか（判断は1か所：費用 ＞ 多めに見込んだ報酬）と、今月の数。"""
    m = reward.month(tx, confidence=float(settings.system_value(tx, "budget.judge_confidence")),
                     gap_max=int(settings.system_value(tx, "budget.hourly_gap_max")))
    return m["cost"] > m["upper"], m


def _check_cost_ratio(tx: Tx, tenant_id: UUID, client_id: UUID) -> int | None:
    deficit, m = _deficit(tx, tenant_id, client_id)
    figures = f"今月の費用 {m['cost']:.2f}円・報酬 {m['revenue']:.2f}円"
    if deficit:
        if _open_stop(tx, "client", "deficit"):
            return None
        why = "" if m["has_terms"] else "（報酬の決まりがありません）"
        return stops.stop(tx, scope="client", cause="deficit", reason=f"赤字：{figures}{why}",
                          evidence="利用量の台帳・報酬の決まり", tenant_id=tenant_id, client_id=client_id)["event_id"]
    limit = Decimal(settings.resolve(tx, "budget.max_cost_ratio", tenant_id=tenant_id, client_id=client_id,
                                     business_id=None))
    if m["upper"] > 0 and m["cost"] / m["upper"] > limit:
        ratio = m["cost"] / m["upper"]
        day = tx.one("SELECT salescall.jst(now())::date AS d")["d"]
        notify.emit(tx, "budget", key=f"cost_ratio:{client_id}:{day}",
                    summary=f"報酬に対する費用の割合が上限を超えました（多めに見込んでも {ratio:.0%} ＞ {limit:.0%}。{figures}）",
                    refs={"client_id": client_id, "ratio": str(ratio), "limit": str(limit)})
    return None


def record_line_estimate(tx: Tx, attempt_id: UUID) -> dict[str, Any] | None:
    """回線の料金の推定（締めの処理が呼ぶ）：お客様が出てから終わるまでの秒数 × 単価。お客様が出なかった通話は書かない。"""
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
    """見守りが決まった間隔で呼ぶ：全体の1日の上限を超えたら全体を止める。日が替わった費用の停止を解く。
    赤字の停止は、今月の数を見直して、赤字でなくなっていたら解く。"""
    def system(tx: Tx) -> int | None:
        limit = Decimal(settings.system_value(tx, "budget.system_daily_yen"))
        spent = tx.one("SELECT salescall.spent_today_all() AS y")["y"]
        if spent <= limit or _open_stop(tx, "system", "budget"):
            return None
        return stops.stop(tx, scope="system", cause="budget", reason=f"全体の今日の費用が上限を超えた（{spent}円 ＞ {limit}円）",
                          evidence="利用量の台帳")["event_id"]
    stopped = access.in_system(actor, system)
    resumed = []
    for s in access.in_system(actor, lambda tx: tx.all(
            "SELECT b.*, salescall.jst(now())::date AS today FROM salescall.open_system_stops('budget') b")):
        if s["stopped_on"] >= s["today"]:
            continue
        work = lambda tx: stops.resume(tx, s["event_id"], reason="日が替わった（費用の上限の期間）")  # noqa: E731
        if s["scope"] == "system":
            resumed.append(access.in_system(actor, work))
        else:
            resumed.append(access.in_client(actor, s["tenant_id"], s["client_id"], work))
    for s in access.in_system(actor, lambda tx: tx.all("SELECT * FROM salescall.open_system_stops('deficit')")):
        def check(tx: Tx, s=s) -> int | None:
            deficit, m = _deficit(tx, s["tenant_id"], s["client_id"])
            if deficit:
                return None
            return stops.resume(tx, s["event_id"], reason=f"赤字でなくなった（今月の費用 {m['cost']:.2f}円・報酬 {m['revenue']:.2f}円）",
                                evidence="利用量の台帳・報酬の決まり")
        got = access.in_client(actor, s["tenant_id"], s["client_id"], check)
        if got is not None:
            resumed.append(got)
    return {"stopped": stopped, "resumed": resumed}
