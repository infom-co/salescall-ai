"""承認の部分（検証モード。DESIGN.md 10-1 の3・10-2）。持ち主 CL-2。

- **いまどちらのモードか（設定 approval.human）を読むのは、この部品だけ**（I-13。app/checks が見張る）。門・締めの処理は、
  ここに問い合わせるだけ（判定の処理はモードを読まない＝両モードで1文字も変わらない）。
- 発信の前（`task_cleared`。門が呼ぶ）：人の承認を使う業務では、運営者が承認した行だけを通す（承認の記録は `approve_task`）。
- 「送ってよい」の印（result_releases）を付けるのも、ここだけ：
  - フルオート（`after_judged`。締めの処理が判定のあとに呼ぶ）：判定した版に自動で付ける。**ただし最初の段の版の結果は、その段の
    指標が基準を満たして次の段へ進んでから、まとめて付ける**（基準を外れた版の結果を、確かめないまま依頼主へ送らない。10-1 の3）。
  - 人の承認を使う業務：運営者が承認した版（`approve_result`）か、直した版（`correct_result`。人の版を足す。システムの版は残す）
    にだけ付ける。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from salescall.core import quality_stage, results, settings
from salescall.core.access import Tx


def _human(tx: Tx, task_id: UUID) -> bool:
    s = tx.one("SELECT t.tenant_id, t.client_id, r.business_id FROM salescall.tasks t"
               " JOIN salescall.runs r ON r.client_id = t.client_id AND r.run_id = t.run_id WHERE t.task_id = %s", (task_id,))
    return bool(settings.resolve(tx, "approval.human", tenant_id=s["tenant_id"], client_id=s["client_id"],
                                 business_id=s["business_id"]))


def task_cleared(tx: Tx, task_id: UUID) -> bool:
    """門が呼ぶ：この行へ発信してよいか（承認の分だけ）。フルオートなら常に通す。"""
    if not _human(tx, task_id):
        return True
    return tx.one("SELECT 1 FROM salescall.task_approvals WHERE task_id = %s", (task_id,)) is not None


def approve_task(tx: Tx, task_id: UUID) -> None:
    """運営者が行を承認する（運営者だけ。データベースが守る）。"""
    tx.execute("INSERT INTO salescall.task_approvals (tenant_id, client_id, task_id, approved_by)"
               " SELECT tenant_id, client_id, task_id, salescall.cur_actor() FROM salescall.tasks WHERE task_id = %s",
               (task_id,))


def _release(tx: Tx, task_id: UUID, version_no: int) -> None:
    tx.execute("INSERT INTO salescall.result_releases (tenant_id, client_id, task_id, version_no, released_by)"
               " SELECT tenant_id, client_id, task_id, version_no, salescall.cur_actor() FROM salescall.task_results"
               " WHERE task_id = %s AND version_no = %s ON CONFLICT DO NOTHING", (task_id, version_no))


def after_judged(tx: Tx, task_id: UUID, version_no: int, version_id: UUID) -> list[tuple[UUID, int]]:
    """締めの処理が呼ぶ（判定の版を足し、品質の段を評価したあと）。フルオートなら印を付ける（最初の段の版は、段が進むまで待ち、
    進んだら、その版の待っていた結果にまとめて付ける）。付けた（行, 版の番号）を返す。"""
    if _human(tx, task_id):
        return []
    if tx.one("SELECT business_state::text AS s FROM salescall.task_results WHERE task_id = %s AND version_no = %s",
              (task_id, version_no))["s"] == "unknown":
        return []                                  # 判定できなかった結果は送らない（判定し直した版を送る）
    stage = quality_stage.current(tx, version_id)
    if stage is None or stage["stage_no"] == 1:
        return []                                  # 最初の段の指標が基準を満たすまで待つ
    held = tx.all(
        "SELECT r.task_id, r.version_no FROM salescall.task_results r JOIN salescall.attempts a ON a.attempt_id = r.attempt_id"
        " WHERE a.version_id = %s AND r.kind IN ('judged', 'rejudged') AND r.business_state <> 'unknown'"
        " AND NOT EXISTS (SELECT 1 FROM salescall.result_releases x WHERE x.task_id = r.task_id"
        "                 AND x.version_no = r.version_no) ORDER BY r.task_id, r.version_no", (version_id,))
    for h in held:
        if not _human(tx, h["task_id"]):
            _release(tx, h["task_id"], h["version_no"])
    return [(h["task_id"], h["version_no"]) for h in held]


def approve_result(tx: Tx, task_id: UUID, version_no: int) -> None:
    """人の承認：運営者が、その結果の版を確かめて「送ってよい」にする。"""
    _release(tx, task_id, version_no)


def correct_result(tx: Tx, task_id: UUID, *, business_state: str, complaint: bool, snapshot: dict[str, Any],
                   detail: dict[str, Any] | None = None) -> int:
    """人の承認：運営者が結果を直す。人の版を足して（システムが判定した版は残す＝誤判定率の材料）、その版を送ってよいにする。"""
    version_no = results.append(tx, task_id, kind="human", business_state=business_state, attempt_id=None,
                                judge_version=None, complaint=complaint, snapshot=snapshot, decided_by="運営者",
                                detail=detail)
    _release(tx, task_id, version_no)
    return version_no
