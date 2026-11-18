"""お客様リストの行の進み具合（ダッシュボードで見る形。DESIGN.md 3-4-1）。持ち主 CL-2。画面は CL-3。

✅ 2026-11-05 ユーザー：「それらはダッシュボードに取込、ダッシュボードでも進捗確認や修正ができるようにすること。」

読むだけ。どの値も、それを決める1か所の見方・部品から読む（ここで決め直さない）：行のいまの名前・役職・部署＝見方 tasks_now（直しを
当てたもの）／かける番号＝salescall.task_dial_contact（ダッシュボードで直していれば直した番号）／業務の状態＝見方 latest_task_results（無ければ open）／次の予定＝見方 current_next_actions／
「かけない」の印＝salescall.task_excluded／連絡先と番号＝customer_contacts.now。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from salescall.core import customer_contacts
from salescall.core.access import Tx


def progress(tx: Tx, run_id: UUID) -> list[dict[str, Any]]:
    """その実施回の行ごとの進み具合（一覧の行番号の順）：行・管理番号・会社名・お呼びする方（名前・役職・部署）・かける番号・業務の状態・
    次の予定（かける／判定待ち／終える・時刻・理由）・「かけない」の印・かけた回数・最後の電話（状態・時刻）・連絡先と番号。"""
    rows = tx.all(
        "SELECT t.task_id, t.source_row, t.client_ref, t.source_name, t.target_name, t.target_role, t.target_department,"
        "       k.phone_e164 AS dial_phone, coalesce(r.business_state::text, 'open') AS business_state,"
        "       n.kind AS next_kind, n.call_at AS next_call_at, n.end_reason AS next_end_reason, n.purpose AS next_purpose,"
        "       salescall.task_excluded(t.task_id) AS excluded,"
        "       (SELECT count(*) FROM salescall.attempts a WHERE a.task_id = t.task_id AND a.state <> 'released') AS attempts,"
        "       l.state::text AS last_state, l.claimed_at AS last_at"
        " FROM salescall.tasks_now t"
        " JOIN salescall.task_numbers k ON k.task_id = t.task_id AND k.contact_id = salescall.task_dial_contact(t.task_id)"
        " LEFT JOIN salescall.latest_task_results r ON r.task_id = t.task_id"
        " LEFT JOIN salescall.current_next_actions n ON n.task_id = t.task_id"
        " LEFT JOIN LATERAL (SELECT a.state, a.claimed_at FROM salescall.attempts a WHERE a.task_id = t.task_id AND a.state <> 'released'"
        "                    ORDER BY a.claimed_at DESC LIMIT 1) l ON true"
        " WHERE t.run_id = %s ORDER BY t.source_row NULLS LAST, t.task_id", (run_id,))
    return [{**dict(r), "contacts": customer_contacts.now(tx, r["task_id"])} for r in rows]
