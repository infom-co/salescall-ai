"""承認の部分（検証モード。DESIGN.md 10-1 の3・10-2）。持ち主 CL-2。

- **いまどちらのモードか（設定 approval.human）を読むのは、この部品だけ**（I-13。app/checks が見張る）。門・締めの処理は、
  ここに問い合わせるだけ（判定の処理はモードを読まない＝両モードで1文字も変わらない）。業務設定の版を作るとき、AI の名前の
  読みの案を契約者が確かめるか（検証モードのあいだだけ。2026-10-16 ユーザー：「検証中はAIが認識したものを私が確認します」）も、
  ここに問い合わせる（`human_for_business`。検証の切り替えは1つ）。
- 発信の前（`task_cleared`。門が呼ぶ）：契約者の承認を使う業務では、契約者が承認した行だけを通す（承認の記録は `approve_task`）。
- 「確定した」の印（result_releases。名前は前のまま「送ってよい印」）を付けるのも、ここだけ。**この印は、依頼者へ送ることでは
  ない**：依頼者へ渡すのは、依頼者ごとの形に整えた最終のもの（報告・依頼者のシステムへの書き込み）だけで、いつ・どの形で渡すかは
  出力の型（CL-3。DESIGN.md 7-3）が決める。印の付いた版だけが、その最終のものに入る。検証の段階では、依頼者へは何も渡さない。
  依頼者に、途中の作業や仕組みのことを知らせない（2026-10-16 ユーザー：「依頼者にが途中作業していることは絶対に知らせてはいけない」
  「知れると契約破棄の可能性があります」）。
  - フルオート（`after_judged`。締めの処理が判定のあとに呼ぶ）：判定した版に自動で付ける（品質で止まっても、取れた結果は
    ふつうどおり。止めるのは新しい発信。2026-10-15 ユーザー：「取れた結果は、ふつうどおり扱う」）。
    ⛔ 2026-10-16 に外したもの：最初の段の結果を、段が進むまで待たせていた。段が埋まらない（一覧の行が少ない・版を替えた）と、
    いつまでも確定しなかった（ユーザー：「検証時は案B、本格稼働してから…送る」＝検証モードは契約者が確かめ、本格稼働は自動）。
  - 契約者の承認を使う業務（検証モード）：契約者が承認した版（`approve_result`）か、直した版（`correct_result`。契約者の版を足す。
    システムの版は残す）にだけ付ける。
  - **承認・直したことは、誤判定率の正解になる**（Intent.md 16-3「検証モードで契約者が直した結果も、正解として使う」）：システムが
    判定した版を承認したら「判定どおりが正解」、直したら「直した中身が正解」として `measure.record_check` に入れる（入れる所は
    measure の1か所。その通話の「測り終わるまで消さない」印も、そこで外れる）。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from salescall.core import measure, results, settings
from salescall.core.access import Tx


SYSTEM_KINDS = measure.SYSTEM_KINDS      # システムが判定した版（直す相手・正解を付ける相手。決まりは measure の1か所）


def human_for_business(tx: Tx, business_id: UUID) -> bool:
    """その業務が検証モード（契約者の承認を使う）か。"""
    b = tx.one("SELECT tenant_id, client_id FROM salescall.businesses WHERE business_id = %s", (business_id,))
    if b is None:
        raise LookupError(f"その業務は無い（または、取引の範囲の外）：{business_id}")
    return bool(settings.resolve(tx, "approval.human", tenant_id=b["tenant_id"], client_id=b["client_id"],
                                 business_id=business_id))


def _human(tx: Tx, task_id: UUID) -> bool:
    s = tx.one("SELECT r.business_id FROM salescall.tasks t"
               " JOIN salescall.runs r ON r.client_id = t.client_id AND r.run_id = t.run_id WHERE t.task_id = %s", (task_id,))
    return human_for_business(tx, s["business_id"])


def task_cleared(tx: Tx, task_id: UUID) -> bool:
    """門が呼ぶ：この行へ発信してよいか（承認の分だけ）。フルオートなら常に通す。"""
    if not _human(tx, task_id):
        return True
    return tx.one("SELECT 1 FROM salescall.task_approvals WHERE task_id = %s", (task_id,)) is not None


def approve_task(tx: Tx, task_id: UUID) -> None:
    """契約者が行を承認する（契約者だけ。データベースが守る）。"""
    tx.execute("INSERT INTO salescall.task_approvals (tenant_id, client_id, task_id, approved_by)"
               " SELECT tenant_id, client_id, task_id, salescall.cur_actor() FROM salescall.tasks WHERE task_id = %s",
               (task_id,))


def _release(tx: Tx, task_id: UUID, version_no: int) -> None:
    """「確定した」の印を付ける（1か所）。検証の段階か本番かも印に持たせる（検証の段階の結果は、依頼者へ何も渡さない＝2026-10-16
    ユーザー。出力の型 CL-3 が、この印で分ける。2026-10-17 の計画書との突き合わせで足した）。"""
    stage = "verification" if _human(tx, task_id) else "production"
    tx.execute("INSERT INTO salescall.result_releases (tenant_id, client_id, task_id, version_no, released_by, stage)"
               " SELECT tenant_id, client_id, task_id, version_no, salescall.cur_actor(), %s FROM salescall.task_results"
               " WHERE task_id = %s AND version_no = %s ON CONFLICT DO NOTHING", (stage, task_id, version_no))


def after_judged(tx: Tx, task_id: UUID, version_no: int, version_id: UUID) -> list[tuple[UUID, int]]:
    """締めの処理が呼ぶ（判定の版を足し、品質の段を評価したあと）。フルオートなら、その版に「確定した」の印を付ける。
    付けた（行, 版の番号）を返す（付けなければ空）。"""
    if _human(tx, task_id):
        return []                                  # 検証モード：契約者が確かめる
    if tx.one("SELECT business_state::text AS s FROM salescall.task_results WHERE task_id = %s AND version_no = %s",
              (task_id, version_no))["s"] == "unknown":
        return []                                  # 判定できなかった結果は確定しない（判定し直した版が確定する）
    _release(tx, task_id, version_no)
    return [(task_id, version_no)]


def approve_result(tx: Tx, task_id: UUID, version_no: int) -> None:
    """契約者の承認：契約者が、その結果の版を確かめて「送ってよい」にする。システムが判定した版なら、判定どおりが正解。"""
    _release(tx, task_id, version_no)
    v = tx.one("SELECT business_state::text AS state FROM salescall.task_results WHERE task_id = %s AND version_no = %s",
               (task_id, version_no))
    if v["state"] != "unknown":   # 判定できなかった版を承認しても、正解（業務の状態）は分からない
        measure.record_check(tx, task_id, version_no, truth_state=v["state"], wrong_items=[])


def correct_result(tx: Tx, task_id: UUID, *, checked_version: int, business_state: str, wrong_items: list[str],
                   complaint: bool, snapshot: dict[str, Any], detail: dict[str, Any] | None = None) -> int:
    """契約者の承認：契約者が、システムが判定した版（checked_version）を確かめて直す。契約者の版を足して（システムが判定した版は
    残す）、その版を送ってよいにする。直した中身（正しい業務の状態・違っていた項目＝質問の番号）は、その版の正解になる。"""
    v = tx.one("SELECT kind::text AS kind FROM salescall.task_results WHERE task_id = %s AND version_no = %s",
               (task_id, checked_version))
    if v is None or v["kind"] not in SYSTEM_KINDS:
        raise LookupError(f"直すのは、システムが判定した結果の版です：{task_id} の版 {checked_version}")
    version_no = results.append(tx, task_id, kind="human", business_state=business_state, attempt_id=None,
                                judge_version=None, complaint=complaint, snapshot=snapshot, decided_by="契約者",
                                detail=detail)
    _release(tx, task_id, version_no)
    measure.record_check(tx, task_id, checked_version, truth_state=business_state, wrong_items=wrong_items)
    return version_no
