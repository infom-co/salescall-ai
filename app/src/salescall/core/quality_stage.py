"""業務の版の段（Q22「この業務（版）は、いまどの段か・次の段へ進めるか」）。持ち主 CL-2。DESIGN.md 10-1。

- 段の記録を足すのは、この部品の `open_stage` だけ（I-12）。段を決める処理（模擬通話の合格・指標を数えて次の段へ）は、
  この部品に足す（⏳ 引き継ぎの (9)。いまは記録の形と、門が読む `allowance` だけ）。
- 門は `allowance` を読むだけ（画面も、記録を読んでそのまま出す。自分で数え直さない）。
- 記録が無い版は、まだかけられない（模擬通話の合格・最初の段を開く前。判断できなければ通さない）。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from salescall.core.access import Tx


def current(tx: Tx, version_id: UUID) -> dict[str, Any] | None:
    """いまの段（いちばん新しい記録）。無ければ None。"""
    return tx.one("SELECT stage_id, stage_no, cap, evidence, opened_at FROM salescall.version_stages"
                  " WHERE version_id = %s ORDER BY stage_id DESC LIMIT 1", (version_id,))


def used(tx: Tx, version_id: UUID) -> int:
    """いまの段の件数に数える通話：その版で、相手が出た（応答まで進んだ）試行と、生きている進行中の試行（出るかもしれない）。"""
    return tx.one(
        "SELECT count(*) AS n FROM salescall.attempts a WHERE a.version_id = %s"
        " AND (salescall.is_live(a.state, a.claim_expires_at) OR EXISTS (SELECT 1 FROM salescall.attempt_state_history h"
        "      WHERE h.attempt_id = a.attempt_id AND h.to_state = 'answered'))", (version_id,))["n"]


def allowance(tx: Tx, version_id: UUID) -> tuple[bool, str | None]:
    """その版で、いま新しく予約してよいか（よい, 理由の名前）。理由：version_not_ready・stage_full。"""
    stage = current(tx, version_id)
    if stage is None:
        return False, "version_not_ready"
    if stage["cap"] is not None and used(tx, version_id) >= stage["cap"]:
        return False, "stage_full"
    return True, None


def open_stage(tx: Tx, version_id: UUID, stage_no: int, cap: int | None, *, evidence: str) -> int:
    """段を開く（記録を足す）。cap は会話に至ってよい通話の数（None＝全部）。"""
    return tx.one(
        "INSERT INTO salescall.version_stages (tenant_id, client_id, version_id, stage_no, cap, evidence, opened_by)"
        " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, %s, %s, salescall.cur_actor())"
        " RETURNING stage_id", (version_id, stage_no, cap, evidence))["stage_id"]
