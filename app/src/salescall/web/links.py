"""画面の住所を作る1か所（お客様の行・1本の電話）。受け箱・見張り・検索など、どこから飛んでも同じ住所になる（docs/DASHBOARD.md 2-2）。
依頼者の取引の中で呼ぶ（行 → 実施回 → 業務は、その取引で見える行だけ。見えなければ空の住所）。"""

from __future__ import annotations

from typing import Any

from salescall.core.access import Tx


def row_url(client_id: Any, business_id: Any, run_id: Any, task_id: Any) -> str:
    """お客様の行の住所の形（1か所）。"""
    return f"/c/{client_id}/b/{business_id}/runs/{run_id}/rows/{task_id}"


def row(tx: Tx, task_id: Any) -> str:
    r = tx.one("SELECT salescall.cur_client() AS client_id, t.run_id, r.business_id FROM salescall.tasks t"
               " JOIN salescall.runs r ON r.run_id = t.run_id WHERE t.task_id = %s", (task_id,))
    return row_url(r["client_id"], r["business_id"], r["run_id"], task_id) if r else ""


def rows(tx: Tx, task_ids: list[Any]) -> dict[Any, dict[str, str]]:
    """まとめて引く形（一覧に使う。1回の問い合わせ）：行 → {url・会社名とお呼びする方}。"""
    if not task_ids:
        return {}
    got = tx.all("SELECT salescall.cur_client() AS client_id, t.task_id, t.run_id, r.business_id, t.source_name, t.target_name"
                 " FROM salescall.tasks t JOIN salescall.runs r ON r.run_id = t.run_id WHERE t.task_id = ANY(%s::uuid[])", ([str(t) for t in task_ids],))
    return {g["task_id"]: {"url": row_url(g["client_id"], g["business_id"], g["run_id"], g["task_id"]),
                           "who": " ／ ".join(x for x in (g["source_name"], g["target_name"]) if x) or "（会社名なし）"} for g in got}


def call(tx: Tx, task_id: Any, attempt_id: Any) -> str:
    base = row(tx, task_id)
    return f"{base}/calls/{attempt_id}" if base and attempt_id else base
