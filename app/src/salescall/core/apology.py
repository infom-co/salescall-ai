"""お詫びの電話（2026-10-16 ユーザー決定：A。DESIGN.md 10-1-2）。持ち主 CL-2。

言ってはいけないことを言った通話のあと（その行はひとまず終え、管理者へ知らせた）、知らせを見た管理者が「お詫びする」と決めたら、
誤って案内したことと正しい内容を入れる（`order`）。AI がその相手（その通話でかけた番号）にもう一度電話し、決まった文面
（`TEMPLATE`。文面の正本はここだけ）を伝えて終える。

- お詫びの指示を出せるのは人（運営者・システムの管理者）だけ。言ってはいけないことを言った通話のあとだけ（データベースが守る）。
- お詫びの電話は、業務の結果を変えない（業務の判定をしない）。相手に届かなければ、別の時間帯に決まった回数（retry.apology_max）
  までかけ直す（番号の聞き直しと同じ形）。相手が「もうかけないで」と言っていれば、門が止める（お詫びでもかけない）。
- 会話の担当（CL-1）は、目的が apology の予約証を受けたら `for_task` で文面を読み、話し終えたら、伝えられたかを報告に入れる
  （call_report の apology_delivered）。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from salescall.core import next_action
from salescall.core.access import Tx

# 2026-10-16 ユーザー決定の文面（「○○」に、誤って案内したことと正しい内容を入れる）
TEMPLATE = ("先ほどのお電話で、誤って「{wrong}」というご案内をしてしまいましたが、正しくは「{correct}」となります。"
            "不正確なご案内をしてしまいまして、誠に申し訳ございませんでした。")


def text(wrong_text: str, correct_text: str) -> str:
    """伝える文面（1か所）。"""
    return TEMPLATE.format(wrong=wrong_text.strip(), correct=correct_text.strip())


def order(tx: Tx, attempt_id: UUID, *, wrong_text: str, correct_text: str) -> int:
    """お詫びの電話を決める（人だけ）。言ってはいけないことを言った通話（attempt_id）の相手へ、いまかける予定を書く。"""
    a = tx.one("SELECT task_id, contact_id FROM salescall.attempts WHERE attempt_id = %s", (attempt_id,))
    if a is None:
        raise LookupError(f"その通話は無い（または、取引の範囲の外）：{attempt_id}")
    order_id = tx.one(
        "INSERT INTO salescall.apology_orders (tenant_id, client_id, task_id, source_attempt_id, wrong_text, correct_text,"
        " ordered_by) VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, %s, %s, salescall.cur_actor())"
        " RETURNING order_id", (a["task_id"], attempt_id, wrong_text, correct_text))["order_id"]
    next_action.schedule_apology(tx, a["task_id"], a["contact_id"], cause=f"お詫びの指示 {order_id}")
    return order_id


def for_task(tx: Tx, task_id: UUID) -> dict[str, Any] | None:
    """その行の、いちばん新しいお詫びの指示と、伝える文面（無ければ None）。"""
    row = tx.one("SELECT order_id, source_attempt_id, wrong_text, correct_text FROM salescall.apology_orders"
                 " WHERE task_id = %s ORDER BY order_id DESC LIMIT 1", (task_id,))
    return None if row is None else dict(row, text=text(row["wrong_text"], row["correct_text"]))
