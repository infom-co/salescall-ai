"""画面の住所を作る1か所（お客様の行・1本の電話）。受け箱・見張り・検索など、どこから飛んでも同じ住所になる（docs/DASHBOARD.md 2-2）。
依頼者の取引の中で呼ぶ（行 → 実施回 → 業務は、その取引で見える行だけ。見えなければ空の住所）。"""

from __future__ import annotations

from typing import Any

from salescall.core.access import Tx


def row(tx: Tx, task_id: Any) -> str:
    r = tx.one("SELECT salescall.cur_client() AS client_id, t.run_id, r.business_id FROM salescall.tasks t"
               " JOIN salescall.runs r ON r.run_id = t.run_id WHERE t.task_id = %s", (task_id,))
    return f"/c/{r['client_id']}/b/{r['business_id']}/runs/{r['run_id']}/rows/{task_id}" if r else ""


def call(tx: Tx, task_id: Any, attempt_id: Any) -> str:
    base = row(tx, task_id)
    return f"{base}/calls/{attempt_id}" if base and attempt_id else base
