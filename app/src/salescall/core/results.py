"""結果の版（業務の状態の正本。DESIGN.md 4-1・3-0 の2）。持ち主 CL-2。

- 業務の状態は「結果の版」の最新だけが正本（版が1つも無ければ open）。タスクの行に状態の欄は無い。
- 結果の版を足すのは、この部品の `append` だけ（I-12。app/checks が見張る）。呼ぶのは：
  締めの処理（core/settle.py。判定 core/judge.py の結果。kind = judged／判定し直し rejudged）と、検証モードで契約者が直したとき
  （kind = human。10章）。
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
    """いちばん新しい結果の版（無ければ None）。最新を選ぶのはデータベースの見方 latest_task_results の1か所。"""
    return tx.one(
        "SELECT task_id, version_no, kind, business_state, attempt_id, judge_version, complaint, detail, snapshot,"
        " decided_at, decided_by FROM salescall.latest_task_results WHERE task_id = %s",
        (task_id,),
    )


def of_call(tx: Tx, attempt_id: UUID) -> dict[str, Any] | None:
    """その通話のいまの結果（判定・判定し直し・契約者の直しのいちばん新しい版。無ければ None）。決めるのはデータベースの見方 call_results の1か所（0233）。"""
    return tx.one(
        "SELECT task_id, version_no, kind::text AS kind, business_state::text AS business_state, attempt_id, complaint, detail"
        " FROM salescall.call_results WHERE attempt_id = %s", (attempt_id,))


def correction_of(tx: Tx, attempt_id: UUID) -> dict[str, Any] | None:
    """その通話にいま効いている契約者の直し（通話のいまの結果＝見方 call_results が契約者の版のとき。無ければ None。1か所）。効いているかを決めるのは、
    締め直しのたびに版を書く `settle._judge_and_write` の1か所（直しを読めば契約者の版・読まなければ＝「もうかけないで」への直しのあと依頼者の正式な
    求めで発信禁止を取り消した＝判定し直しの版）。読むのは、締め直し・使う答え（`conversation_io._dropped_by_correction`）。目的のお客様だと
    確かめられなかった通話かも、同じ版を読む（データベースの salescall.call_unconfirmed＝0242）。
    corrected＝直しの元（`corrected_by` の形：version＝契約者が直した版・by＝直した方・kind＝その方の種類・wrong_items＝違っていた質問）。
    ⛔ 2026-11-18 の8回目の見直しまで：いちばん新しい契約者の版を読み、発信禁止を取り消したあとも、使う答えだけ直しを読み続けた。"""
    row = tx.one(
        "SELECT version_no, business_state::text AS business_state, complaint, detail, decided_by, snapshot -> 'corrected' AS corrected"
        " FROM salescall.call_results WHERE attempt_id = %s AND kind = 'human'", (attempt_id,))
    if row is None:
        return None
    c = row.pop("corrected") or {}
    return {**row, "corrected": corrected_by(version=c.get("version") or row["version_no"], by=c.get("by") or row["decided_by"],
                                             kind=c.get("kind") or "operator", wrong_items=c.get("wrong_items") or ())}


def corrected_by(*, version: int | None, by: str, kind: str, wrong_items: Any) -> dict[str, Any]:
    """契約者の版に残す直しの元（結果の版の snapshot の corrected。1か所の形）：version＝契約者が直した版（いま直した版は None＝その版そのもの。締め直しの
    写しの版は、もとの直しの版）・by＝直した方（cur_actor）・kind＝その方の種類（管理者か契約者）・wrong_items＝違っていた項目（質問の番号）。"""
    return {"version": version, "by": by, "kind": kind, "wrong_items": sorted(set(wrong_items))}


def current_state(tx: Tx, task_id: UUID) -> str:
    """いまの業務の状態。結果の版が無ければ open（DESIGN.md 4-1）。"""
    if tx.one("SELECT 1 FROM salescall.tasks WHERE task_id = %s", (task_id,)) is None:
        raise TaskNotFound(str(task_id))
    row = latest(tx, task_id)
    return "open" if row is None else row["business_state"]


def append(tx: Tx, task_id: UUID, *, kind: ResultKind, business_state: BusinessState, attempt_id: UUID | None,
           judge_version: str | None, complaint: bool, snapshot: dict[str, Any], decided_by: str,
           detail: dict[str, Any] | None = None) -> int:
    """結果の版を1つ足し、その版の番号を返す（番号はデータベースが決める＝next_seq。0123）。"""
    lock(tx, task_id)  # 同じタスクへの追記を1つずつ順にする（tasks.py の説明）
    row = tx.one(
        "INSERT INTO salescall.task_results (tenant_id, client_id, task_id, kind, business_state, attempt_id,"
        " judge_version, complaint, detail, snapshot, decided_by)"
        " SELECT t.tenant_id, t.client_id, t.task_id, %s, %s, %s, %s, %s, %s, %s, %s"
        " FROM salescall.tasks t WHERE t.task_id = %s RETURNING version_no",
        (kind, business_state, attempt_id, judge_version, complaint, json.dumps(detail or {}, ensure_ascii=False),
         json.dumps(snapshot, ensure_ascii=False),
         decided_by, task_id),
    )
    if row is None:
        raise TaskNotFound(str(task_id))
    return row["version_no"]
