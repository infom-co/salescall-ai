"""本番の PC の上で押せる緊急停止の、サーバーの側（CL-3 の連絡帳 4-1d の2：画面が開けないとき＝ネットの切断 などでも止められる道。
DESIGN.md 9-2・5-5）。持ち主 CL-2（運転の状態 Q13。DESIGN.md 9-1）。

2026-10-21 ユーザー承認の分け方：PC はデータベースにつながない。PC の部品（`salescall.agent.emergency`）が PC の中に「止めた」印を残し
（ネットが切れていても必ずできる。印があるあいだ、PC の部品は「かけて」と頼まれても断る）、サーバーにつながったら、サーバー（つなぎ口）が
ここを呼んで、その契約者の緊急停止（manual）を記録する（ふつうの停止と同じ。門が止め、まだ発信していない予約を取り消す。画面にも出る）。
- 押したのは、その PC を使う契約者（範囲はその契約者。全体の範囲の停止は、管理者が画面から）。
- 画面から解かれたら（`is_open` が偽）、サーバーが PC へ伝え、PC は印を消す（解いた記録が正本）。
- PC で解く（`release`）：この道で記録した、その契約者の緊急停止を解く。サーバーに届かなければ、PC の印は消さない（解いたことを記録
  できないまま、かけ始めない）。
"""

from __future__ import annotations

from uuid import UUID

from salescall.core import stops
from salescall.core.access import Access, Actor, Tx

EVIDENCE = "本番のPCの緊急停止（PC の部品）"


def actor(pc_label: str) -> Actor:
    """押したのは、その PC を使う契約者。"""
    return Actor(f"契約者の PC の緊急停止（{pc_label}）", "operator")


def stop(app: Access, tenant_id: UUID, pc_label: str, reason: str) -> int:
    """その契約者の緊急停止を記録する（PC の印がサーバーに届いたとき。止めた停止の番号を返す）。"""
    if not reason.strip():
        raise ValueError("止める理由を書いてください")
    return app.in_tenant(actor(pc_label), tenant_id, lambda tx: stops.stop(
        tx, scope="tenant", cause="manual", reason=reason, evidence=EVIDENCE, tenant_id=tenant_id)["event_id"])


def is_open(app: Access, tenant_id: UUID, event_id: int) -> bool:
    """その停止が、まだ解かれていないか（画面から解かれていれば偽＝PC は印を消す）。見えない停止は、解かれていないとみなす（止める側）。"""
    def look(tx: Tx) -> bool:
        row = tx.one("SELECT (SELECT 1 FROM salescall.open_stop_events o WHERE o.event_id = s.event_id) IS NOT NULL AS open"
                     " FROM salescall.stop_events s WHERE s.event_id = %s AND s.action = 'stop'", (event_id,))
        return True if row is None else bool(row["open"])
    return app.in_tenant(actor("確かめ"), tenant_id, look)


def release(app: Access, tenant_id: UUID, pc_label: str, reason: str) -> list[int]:
    """PC で解く：この道で記録した、その契約者のまだ解かれていない緊急停止を解く（解いた停止の番号を返す）。"""
    if not reason.strip():
        raise ValueError("解く理由を書いてください")
    def work(tx: Tx) -> list[int]:
        opened = [r["event_id"] for r in tx.all(
            "SELECT s.event_id FROM salescall.open_stop_events s WHERE s.cause = 'manual' AND s.evidence = %s"
            " AND s.scope = 'tenant' AND s.tenant_id = salescall.cur_tenant()", (EVIDENCE,))]
        return [stops.resume(tx, e, reason=reason, evidence=EVIDENCE) for e in opened]
    return app.in_tenant(actor(pc_label), tenant_id, work)
