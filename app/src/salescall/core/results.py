"""結果の版（業務の状態の正本。DESIGN.md 4-1・3-0 の2）。持ち主 CL-2。

- 業務の状態は「結果の版」の最新だけが正本（版が1つも無ければ open）。タスクの行に状態の欄は無い。
- 結果の版を足すのは、この部品の `append` だけ（I-12。app/checks が見張る）。呼ぶのは：
  判定（judge_task の結果を記録する側。kind = judged / rejudged）と、検証モードで人が直したとき（kind = human。10章）。
- 結果の版は書き換えない・消さない（データベースが拒む）。番号は 1, 2, 3 … と重ならない。
"""

from __future__ import annotations

import json
from typing import Any, Literal
from uuid import UUID

from salescall.core.access import Tx
from salescall.core.tasks import TaskNotFound, lock

BusinessState = Literal["open", "completed", "callback_scheduled", "incomplete", "declined",
                        "do_not_call", "out_of_scope", "not_reached", "concluded", "unknown"]
ResultKind = Literal["judged", "human", "rejudged"]


def latest(tx: Tx, task_id: UUID) -> dict[str, Any] | None:
    """いちばん新しい結果の版（無ければ None）。"""
    return tx.one(
        "SELECT task_id, version_no, kind, business_state, attempt_id, judge_version, complaint, detail, snapshot,"
        " decided_at, decided_by FROM salescall.task_results WHERE task_id = %s ORDER BY version_no DESC LIMIT 1",
        (task_id,),
    )


def current_state(tx: Tx, task_id: UUID) -> str:
    """いまの業務の状態。結果の版が無ければ open（DESIGN.md 4-1）。"""
    if tx.one("SELECT 1 FROM salescall.tasks WHERE task_id = %s", (task_id,)) is None:
        raise TaskNotFound(str(task_id))
    row = latest(tx, task_id)
    return "open" if row is None else row["business_state"]


def append(tx: Tx, task_id: UUID, *, kind: ResultKind, business_state: BusinessState, attempt_id: UUID | None,
           judge_version: str | None, complaint: bool, snapshot: dict[str, Any], decided_by: str,
           detail: dict[str, Any] | None = None) -> int:
    """結果の版を1つ足し、その版の番号を返す。"""
    lock(tx, task_id)  # 同じタスクへの追記を1つずつ順にする（tasks.py の説明）
    row = tx.one(
        "INSERT INTO salescall.task_results (tenant_id, client_id, task_id, version_no, kind, business_state, attempt_id,"
        " judge_version, complaint, detail, snapshot, decided_by)"
        " SELECT t.tenant_id, t.client_id, t.task_id,"
        "        coalesce((SELECT max(r.version_no) FROM salescall.task_results r WHERE r.task_id = t.task_id), 0) + 1,"
        "        %s, %s, %s, %s, %s, %s, %s, %s"
        " FROM salescall.tasks t WHERE t.task_id = %s RETURNING version_no",
        (kind, business_state, attempt_id, judge_version, complaint, json.dumps(detail or {}, ensure_ascii=False),
         json.dumps(snapshot, ensure_ascii=False),
         decided_by, task_id),
    )
    if row is None:
        raise TaskNotFound(str(task_id))
    return row["version_no"]
