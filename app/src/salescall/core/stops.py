"""運転の状態（Q13）：止める・解く・止まっているか。持ち主 CL-2。DESIGN.md 9-1・Intent.md 10-3。

- 止めるのも解くのも、原因ごとに決めた条件で、システムが行う。人が解くのは、人が押した緊急停止（manual）だけ。
- 止まっているか＝「まだ解かれていない停止が1つでもあるか」（原因が重なれば、片方を解いても止まったまま）。
- 止めるときは、同じ取引で、まだ発信していない予約を取り消す（DESIGN.md 5-2-1 の2）。
- 止めた・解いたは、知らせの台帳に入れる（同じ取引。core/notify.py。送るのは CL-3）。
- 停止の記録を足すのは、この部品だけ（I-12。app/checks が見張る）。「誰が」は取引の「誰が」をそのまま使う
  （データベースが、違えば拒む）。システムが人の緊急停止を解こうとすると、データベースが拒む。
"""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from salescall.core import notify
from salescall.core.access import Tx

Scope = Literal["system", "tenant", "client", "business", "line"]   # line＝その回線だけ（回線の異常。9-1）
Cause = Literal["manual", "storage_failure", "line_failure", "ai_connection", "project_denied",
                "forbidden_speech", "quality_gate", "budget"]


def stop(tx: Tx, *, scope: Scope, cause: Cause, reason: str, evidence: str | None = None,
         tenant_id: UUID | None = None, client_id: UUID | None = None, business_id: UUID | None = None,
         line_id: UUID | None = None) -> dict[str, Any]:
    """止める。止めた記録と、取り消した予約の数を返す。"""
    row = tx.one(
        "INSERT INTO salescall.stop_events (scope, tenant_id, client_id, business_id, line_id, action, cause, actor,"
        " actor_kind, reason, evidence)"
        " VALUES (%s, %s, %s, %s, %s, 'stop', %s, salescall.cur_actor(), salescall.cur_actor_kind()::salescall.actor_kind,"
        " %s, %s) RETURNING event_id",
        (scope, tenant_id, client_id, business_id, line_id, cause, reason, evidence),
    )
    released = tx.one("SELECT salescall.release_claims_for_stop(%s) AS n", (row["event_id"],))["n"]
    notify.emit(tx, "stopped", key=f"stop:{row['event_id']}", summary=f"発信を止めました（範囲：{scope}・原因：{cause}）。{reason}",
                refs={"stop_event_id": row["event_id"], "scope": scope, "cause": cause, "client_id": client_id,
                      "business_id": business_id, "line_id": line_id})
    return {"event_id": row["event_id"], "released_claims": released}


def resume(tx: Tx, stop_event_id: int, *, reason: str, evidence: str | None = None) -> int:
    """その停止を解く（範囲は停止と同じ）。解いた記録の番号を返す。"""
    row = tx.one(
        "INSERT INTO salescall.stop_events (scope, tenant_id, client_id, business_id, line_id, action, resolves_event_id,"
        " actor, actor_kind, reason, evidence)"
        " SELECT scope, tenant_id, client_id, business_id, line_id, 'resume', event_id, salescall.cur_actor(),"
        "        salescall.cur_actor_kind()::salescall.actor_kind, %s, %s"
        " FROM salescall.stop_events WHERE event_id = %s AND action = 'stop' RETURNING event_id",
        (reason, evidence, stop_event_id),
    )
    if row is None:
        raise LookupError(f"その停止は無い（または、取引の範囲の外）：{stop_event_id}")
    notify.emit(tx, "resumed", key=f"resume:{row['event_id']}", summary=f"止めていた発信を解きました（停止 {stop_event_id}）。{reason}",
                refs={"stop_event_id": stop_event_id, "resume_event_id": row["event_id"]})
    return row["event_id"]


def open_stops(tx: Tx, *, tenant_id: UUID | None, client_id: UUID | None,
               business_id: UUID | None, line_id: UUID | None = None) -> list[dict[str, Any]]:
    """まだ解かれていない停止のうち、この範囲にかかるもの（空なら止まっていない）。line_id を渡すと、その回線の停止も含める。"""
    return tx.all(
        "SELECT event_id, scope, cause, reason, at FROM salescall.open_stops(%s, %s, %s, %s) ORDER BY event_id",
        (tenant_id, client_id, business_id, line_id),
    )
