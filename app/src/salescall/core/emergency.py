"""本番の PC の上で押せる緊急停止の、サーバーの側（CL-3 の連絡帳 4-1d の2：画面が開けないとき＝ネットの切断 などでも止められる道。
DESIGN.md 9-2・5-5）。持ち主 CL-2（運転の状態 Q13。DESIGN.md 9-1）。

2026-10-21 ユーザー承認の分け方：PC はデータベースにつながない。PC の部品（`salescall.agent.emergency`）が PC の中に「止めた」印を残し
（ネットが切れていても必ずできる。印があるあいだ、PC の部品は「かけて」と頼まれても断る）、サーバーにつながったら、サーバー（つなぎ口）が
ここを呼んで、その契約者の緊急停止（manual）を記録する（ふつうの停止と同じ。門が止め、まだ発信していない予約を取り消す。画面にも出る）。
- 押したのは、その PC を使う契約者（範囲はその契約者。全体の範囲の停止は、管理者が画面から）。
- 画面から解かれたら（`is_open` が偽）、サーバーが PC へ伝え、PC は印を消す（解いた記録が正本）。
- PC で解く（`release`）：**その PC が**この道で記録した緊急停止だけを解く（同じ契約者のほかの PC で押した停止は解かない。根拠の文に
  PC の番号を入れて見分ける＝`evidence`）。サーバーに届かなければ、PC の印は消さない（解いたことを記録できないまま、かけ始めない）。
"""

from __future__ import annotations

from uuid import UUID

from salescall.core import stops
from salescall.core.access import Access, Actor, Tx

EVIDENCE = "本番のPCの緊急停止（PC の部品）"


def evidence(pc_id: UUID, pressed_at: str = "") -> str:
    """その PC で押した緊急停止の根拠の文（判断はこの1か所）：PC の番号（PC で解くとき、その PC の停止だけを見分ける）と、押した時刻
    （1回の押し。つなぎ直しても、同じ押しを二重に記録しない）。pressed_at を空にすると、その PC の停止の頭の文字（見分けに使う）。"""
    return f"{EVIDENCE}：{pc_id}：{pressed_at}"


def actor(pc_label: str) -> Actor:
    """押したのは、その PC を使う契約者。"""
    return Actor(f"契約者の PC の緊急停止（{pc_label}）", "operator")


def stop(app: Access, tenant_id: UUID, pc_id: UUID, pc_label: str, reason: str, pressed_at: str) -> int:
    """その契約者の緊急停止を記録する（PC の印がサーバーに届いたとき。停止の番号を返す）。**同じ押しは1回だけ記録する**：記録した
    あと、PC が番号を書く前につながりが切れても、つなぎ直したときは前の番号を返す（もう画面から解かれていれば、その番号＝PC は
    印を消す。解いたあとに止め直さない）。"""
    if not reason.strip():
        raise ValueError("止める理由を書いてください")
    if not pressed_at.strip():
        raise ValueError("押した時刻が要ります（1回の押しを見分ける）")
    def work(tx: Tx) -> int:
        done = tx.one("SELECT event_id FROM salescall.stop_events WHERE action = 'stop' AND evidence = %s"
                      " ORDER BY event_id DESC LIMIT 1", (evidence(pc_id, pressed_at),))
        if done is not None:
            return done["event_id"]
        return stops.stop(tx, scope="tenant", cause="manual", reason=reason, evidence=evidence(pc_id, pressed_at),
                          tenant_id=tenant_id)["event_id"]
    return app.in_tenant(actor(pc_label), tenant_id, work)


def is_open(app: Access, tenant_id: UUID, event_id: int) -> bool:
    """その停止が、まだ解かれていないか（画面から解かれていれば偽＝PC は印を消す）。見えない停止は、解かれていないとみなす（止める側）。"""
    def look(tx: Tx) -> bool:
        row = tx.one("SELECT (SELECT 1 FROM salescall.open_stop_events o WHERE o.event_id = s.event_id) IS NOT NULL AS open"
                     " FROM salescall.stop_events s WHERE s.event_id = %s AND s.action = 'stop'", (event_id,))
        return True if row is None else bool(row["open"])
    return app.in_tenant(actor("確かめ"), tenant_id, look)


def release(app: Access, tenant_id: UUID, pc_id: UUID, pc_label: str, reason: str) -> list[int]:
    """PC で解く：その PC がこの道で記録した、まだ解かれていない緊急停止を解く（解いた停止の番号を返す）。"""
    if not reason.strip():
        raise ValueError("解く理由を書いてください")
    def work(tx: Tx) -> list[int]:
        opened = [r["event_id"] for r in tx.all(
            "SELECT s.event_id FROM salescall.open_stop_events s WHERE s.cause = 'manual' AND starts_with(s.evidence, %s)"
            " AND s.scope = 'tenant' AND s.tenant_id = salescall.cur_tenant()", (evidence(pc_id),))]
        return [stops.resume(tx, e, reason=reason, evidence=evidence(pc_id)) for e in opened]
    return app.in_tenant(actor(pc_label), tenant_id, work)
