"""タスク（実施回 × 連絡先）の共通の部品。持ち主 CL-2。

追記だけの記録（結果の版・次の動きの記録）に番号を振る前に、そのタスクの順番を取る（`lock`）。
⚠️ 直列化の水準でも、同時に「いまの最大の番号 + 1」を足すと、やり直し（直列化の失敗）にならず、
   一意の約束の違反として返ることがある（2026-10-13 に試験で確かめた）。順番待ちの表の、そのタスクの行を
   書き換えてから番号を振れば、ぶつかったほうは「やり直し」として返り、読み書きの層がやり直す。
   （タスクの行そのものは固定できない：システムの処理の役は、タスクを書き換える権限を持たないため。）
⇒ 番号を振る処理は、必ずこれを先に呼ぶ。

行の番号（DESIGN.md 3-5）：依頼主の一覧の1行は1つのまま、番号を表（task_numbers）に持つ。一覧の番号の行はデータベースが
タスクを作ったときに自分で足す。相手が言った別の番号を足すのは `add_callee_number` だけ。行の番号を読むのは `numbers` だけ。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from salescall.core import contacts
from salescall.core.access import Tx
from salescall.core.phone import PhoneNumber


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


def numbers(tx: Tx, task_id: UUID) -> list[str]:
    """そのタスク（依頼主の一覧の1行）の番号すべて（そろえた番号。一覧の番号が先、あとは足した順）。
    行ごとに番号を数える処理（発信禁止を行ごとに広げる dnc など）は、必ずこれを呼ぶ。"""
    rows = tx.all("SELECT phone_e164 FROM salescall.task_numbers WHERE task_id = %s"
                  " ORDER BY source = 'callee', added_at, phone_e164", (task_id,))
    if not rows:
        raise TaskNotFound(str(task_id))
    return [r["phone_e164"] for r in rows]


def list_number(tx: Tx, task_id: UUID) -> dict[str, Any]:
    """一覧の番号（取込のときの番号。行の最初の予定は、この番号へかける）。contact_id と phone_e164。"""
    row = tx.one("SELECT contact_id, phone_e164 FROM salescall.task_numbers WHERE task_id = %s AND source = 'list'",
                 (task_id,))
    if row is None:
        raise TaskNotFound(str(task_id))
    return row


def add_callee_number(tx: Tx, task_id: UUID, number: PhoneNumber, *, source_attempt_id: UUID) -> UUID:
    """相手が言った別の番号を、その行の番号に足す（DESIGN.md 3-5。足すのはここだけ）。その番号の連絡先を返す。

    かけるかどうかに関係なく足す（その相手が断ったら、この番号も止めるため。3-6）。同じ番号がもう行にあれば何もしない
    （一覧の番号と同じ番号を言われた場合も）。そろえられない番号は足せない（ValueError）。"""
    contact_id = contacts.get_or_create(tx, number)
    tx.execute(
        "INSERT INTO salescall.task_numbers (tenant_id, client_id, task_id, contact_id, phone_e164, source, source_attempt_id)"
        " SELECT t.tenant_id, t.client_id, t.task_id, %s, %s, 'callee', %s FROM salescall.tasks t WHERE t.task_id = %s"
        " ON CONFLICT (task_id, contact_id) DO NOTHING",
        (contact_id, number.e164, source_attempt_id, task_id))
    if tx.one("SELECT 1 FROM salescall.task_numbers WHERE task_id = %s AND contact_id = %s", (task_id, contact_id)) is None:
        raise TaskNotFound(str(task_id))
    return contact_id
