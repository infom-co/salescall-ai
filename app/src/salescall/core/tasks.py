"""タスク（実施回 × 連絡先）の共通の部品。持ち主 CL-2。

追記だけの記録（結果の版・次の動きの記録）に番号を振る前に、そのタスクの順番を取る（`lock`）。
⚠️ 直列化の水準でも、同時に「いまの最大の番号 + 1」を足すと、やり直し（直列化の失敗）にならず、
   一意の約束の違反として返ることがある（2026-10-13 に試験で確かめた）。順番待ちの表の、そのタスクの行を
   書き換えてから番号を振れば、ぶつかったほうは「やり直し」として返り、読み書きの層がやり直す。
   （タスクの行そのものは固定できない：システムの処理の役は、タスクを書き換える権限を持たないため。）
⇒ 番号を振る処理は、必ずこれを先に呼ぶ。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from salescall.core.access import Tx


class TaskNotFound(Exception):
    """そのタスクは無い（または、取引の依頼主の範囲の外）。"""


def lock(tx: Tx, task_id: UUID) -> dict[str, Any]:
    """このタスクへの追記の順番を、この取引が終わるまで取る。無ければ TaskNotFound。"""
    row = tx.one(
        "INSERT INTO salescall.task_append_locks (tenant_id, client_id, task_id, bumps)"
        " SELECT tenant_id, client_id, task_id, 1 FROM salescall.tasks WHERE task_id = %s"
        " ON CONFLICT (task_id) DO UPDATE SET bumps = salescall.task_append_locks.bumps + 1"
        " RETURNING tenant_id, client_id, task_id",
        (task_id,),
    )
    if row is None:
        raise TaskNotFound(str(task_id))
    return row
