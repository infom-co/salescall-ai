"""タスク（実施回 × 連絡先）の共通の部品。持ち主 CL-2。

追記だけの記録（結果の版・次の動きの記録）に番号を振る前に、そのタスクの順番を取る（`lock`）。
⚠️ 直列化の水準でも、同時に「いまの最大の番号 + 1」を足すと、やり直し（直列化の失敗）にならず、
   一意の約束の違反として返ることがある（2026-10-13 に試験で確かめた）。順番待ちの表の、そのタスクの行を
   書き換えてから番号を振れば、ぶつかったほうは「やり直し」として返り、読み書きの層がやり直す。
   （タスクの行そのものは固定できない：システムの処理の役は、タスクを書き換える権限を持たないため。）
⇒ 番号を振る処理は、必ずこれを先に呼ぶ。

会話の中でお客様に確かめた名前の読み（2026-10-16 ユーザー・CL-1 の連絡帳 4-15）：書くのは `record_confirmed_reading` だけ。
次にかけるときは、この読みを決まった読みとして使う（読むのは business_settings.target_of の1か所）。

行の番号（DESIGN.md 3-5）：依頼者の一覧の1行は1つのまま、番号を表（task_numbers）に持つ。一覧の番号の行はデータベースが
タスクを作ったときに自分で足す。お客様が言った別の番号を足すのは `add_callee_number` だけ。記録だけの番号（かけない番号。0175）を
足すのは `add_record_number` だけ。行の番号を読むのは `numbers` だけ。いまかける番号は `dial_number` だけ（ダッシュボードの直しは
`fix_dial_number`。0179。⛔ 前は取り込んだ番号を返す list_number もあり、かける番号を読む所が2つあった）。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from salescall.core import contacts, readings
from salescall.core.access import Tx
from salescall.core.phone import PhoneNumber


class TaskNotFound(Exception):
    """そのタスクは無い（または、取引の依頼者の範囲の外）。"""


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


def scope(tx: Tx, task_id: UUID) -> dict[str, Any]:
    """その行の契約者・依頼者・業務（設定を読む範囲。1か所）。無ければ TaskNotFound。"""
    row = tx.one("SELECT t.tenant_id, t.client_id, r.business_id FROM salescall.tasks t"
                 " JOIN salescall.runs r ON r.client_id = t.client_id AND r.run_id = t.run_id WHERE t.task_id = %s", (task_id,))
    if row is None:
        raise TaskNotFound(str(task_id))
    return row


def numbers(tx: Tx, task_id: UUID) -> list[str]:
    """そのタスク（依頼者の一覧の1行）の番号すべて（そろえた番号。一覧の番号が先、あとは足した順）。
    行ごとに番号を数える処理（発信禁止を行ごとに広げる dnc など）は、必ずこれを呼ぶ。"""
    rows = tx.all("SELECT phone_e164 FROM salescall.task_numbers WHERE task_id = %s"
                  " ORDER BY source = 'callee', added_at, phone_e164", (task_id,))
    if not rows:
        raise TaskNotFound(str(task_id))
    return [r["phone_e164"] for r in rows]


def dial_number(tx: Tx, task_id: UUID) -> dict[str, Any]:
    """いまかける番号（contact_id・phone_e164）。ダッシュボードで直していれば、いちばん新しい直しの番号、無ければ一覧の番号（決めるのは
    データベースの関数 salescall.task_dial_contact の1か所。0179）。行の最初の予定・予定の無い行の門は、この番号へかける。"""
    row = tx.one("SELECT n.contact_id, n.phone_e164 FROM salescall.task_numbers n"
                 " WHERE n.task_id = %s AND n.contact_id = salescall.task_dial_contact(%s)", (task_id, task_id))
    if row is None:
        raise TaskNotFound(str(task_id))
    return row


def fix_dial_number(tx: Tx, task_id: UUID, number: PhoneNumber, label: str) -> UUID:
    """いまかける番号を、ダッシュボードで直した番号にする（書くのはここだけ。かけてよいかの確かめと予定の付け替えは core/row_fixes.py）。
    取り込んだ番号は残す（一覧の番号のまま。かけなくなる）。行に無い番号は記録だけの番号として足してから付け替える（名札 label）。
    直した番号の連絡先を返す。"""
    contact_id = contacts.get_or_create(tx, number)
    if tx.one("SELECT 1 FROM salescall.task_numbers WHERE task_id = %s AND contact_id = %s", (task_id, contact_id)) is None:
        add_record_number(tx, task_id, number, label, origin="dashboard")
    tx.execute("INSERT INTO salescall.task_dial_fixes (tenant_id, client_id, task_id, contact_id, recorded_by)"
               " SELECT t.tenant_id, t.client_id, t.task_id, %s, salescall.cur_actor() FROM salescall.tasks t WHERE t.task_id = %s",
               (contact_id, task_id))
    return contact_id


def add_callee_number(tx: Tx, task_id: UUID, number: PhoneNumber, *, source_attempt_id: UUID) -> UUID:
    """お客様が言った別の番号を、その行の番号に足す（DESIGN.md 3-5。足すのはここだけ）。その番号の連絡先を返す。

    かけるかどうかに関係なく足す（そのお客様が断ったら、この番号も止めるため。3-6）。同じ番号がもう行にあれば何もしない
    （一覧の番号と同じ番号を言われた場合も）。そろえられない番号は足せない（ValueError）。"""
    contact_id = contacts.get_or_create(tx, number)
    tx.execute(
        "INSERT INTO salescall.task_numbers (tenant_id, client_id, task_id, contact_id, phone_e164, source, source_attempt_id)"
        " SELECT t.tenant_id, t.client_id, t.task_id, %s, %s, 'callee', %s FROM salescall.tasks t WHERE t.task_id = %s"
        " ON CONFLICT (task_id, contact_id) DO NOTHING",
        (contact_id, number.e164, source_attempt_id, task_id))
    if tx.one("SELECT 1 FROM salescall.task_numbers WHERE task_id = %s AND contact_id = %s", (task_id, contact_id)) is None:
        raise TaskNotFound(str(task_id))
    # お客様がこの番号を指定した記録（行にもうあった番号でも書く。いちばん新しい直しより後の指定は、直した番号より先にかける＝
    # salescall.task_designated_contact・next_action.current_dial。2026-11-06 ユーザー：「お客様の言った番号へ」。0188）
    tx.execute("INSERT INTO salescall.task_designations (tenant_id, client_id, task_id, contact_id, attempt_id)"
               " SELECT t.tenant_id, t.client_id, t.task_id, %s, %s FROM salescall.tasks t WHERE t.task_id = %s",
               (contact_id, source_attempt_id, task_id))
    return contact_id


def add_record_number(tx: Tx, task_id: UUID, number: PhoneNumber, label: str, *, origin: str,
                      attempt_id: UUID | None = None) -> UUID:
    """記録だけの番号（かけない番号。0175）を、その行の番号に足す（足すのはここだけ。決め方・呼ぶ所は core/customer_contacts.py）。
    同じ番号がもう行にあれば何もしない（かける番号と同じ番号を書いた・伺った場合も）。その番号の連絡先を返す。そのお客様が断ったら、
    この番号も止める（発信禁止は `numbers` の1か所を読む）。試行・次の予定にはならない（データベースが断る）。"""
    contact_id = contacts.get_or_create(tx, number)
    tx.execute(
        "INSERT INTO salescall.task_numbers (tenant_id, client_id, task_id, contact_id, phone_e164, source, label, record_origin,"
        "                                    source_attempt_id, recorded_by)"
        " SELECT t.tenant_id, t.client_id, t.task_id, %s, %s, 'record', %s, %s, %s,"
        "        CASE WHEN %s = 'dashboard' THEN salescall.cur_actor() END"
        " FROM salescall.tasks t WHERE t.task_id = %s"
        " ON CONFLICT (task_id, contact_id) DO NOTHING",
        (contact_id, number.e164, label, origin, attempt_id if origin == "call" else None, origin, task_id))
    return contact_id


def record_sub_number(tx: Tx, task_id: UUID, attempt_id: UUID) -> None:
    """その通話でかけた番号が、前にかける番号だった番号（salescall.task_number_replaced）で、番号を直したあとにかけた通話なら、その番号を
    サブの番号に記録する（書くのはここだけ。お客様と話せたか＝call_report.talked_in は呼ぶ側＝next_action.after_call が確かめる。0182）。
    2026-11-06 ユーザー：「話せたら、新番号がメイン、旧番号はサブ」。"""
    tx.execute(
        "INSERT INTO salescall.task_sub_numbers (tenant_id, client_id, task_id, contact_id, attempt_id)"
        " SELECT a.tenant_id, a.client_id, a.task_id, a.contact_id, a.attempt_id FROM salescall.attempts a"
        " WHERE a.attempt_id = %s AND a.task_id = %s AND a.claimed_at >= salescall.task_count_from(a.task_id)"
        "   AND salescall.task_number_replaced_at(a.task_id, a.contact_id, a.claimed_at)"   # かけたときの立場（0188。データベースも守る）
        " ON CONFLICT (attempt_id) DO NOTHING", (attempt_id, task_id))   # 判定し直しで同じ通話をまた締めても1つ


def record_confirmed_reading(tx: Tx, task_id: UUID, which: str, reading: str, *, attempt_id: UUID) -> None:
    """会話の中でお客様に確かめた名前の読みを記録する（which：source＝お客様の会社名／target＝目的のお客様の名前）。
    読みはひらがなに直して持つ（readings.normalize）。読みとして使えなければ ValueError。確かめた通話は、その行の通話だけ
    （データベースが守る）。"""
    if which not in ("source", "target"):
        raise ValueError(f"どの名前か（which）は source か target です：{which!r}")
    r = readings.required(reading, f"読み「{reading}」")
    tx.execute("INSERT INTO salescall.task_confirmed_readings (tenant_id, client_id, task_id, which, reading, attempt_id,"
               " confirmed_by) SELECT t.tenant_id, t.client_id, t.task_id, %s, %s, %s, salescall.cur_actor()"
               " FROM salescall.tasks t WHERE t.task_id = %s", (which, r, attempt_id, task_id))
