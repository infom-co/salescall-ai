"""行ごとの「かけない」（除外。Intent.md 5-3 (8)・7-2。DESIGN.md 3-4-2。表の作り 0071）。持ち主 CL-2。

2026-10-18 ユーザー決定：「電話番号＆お客様のリストから、かけないを入力できる列や、取り込んだデータをダッシュボードから、レ点や
一括チェックでかけないを選択できるようにしとくのがいい」。

- **印を付け外しするのは `mark` の1か所**。入口は2つ：一覧の取込の「かけない」の列（core/importer.py が呼ぶ）と、ダッシュボードの
  レ点・一括（画面＝CL-3 が呼ぶ。管理者か契約者だけ＝データベースが守る）。記録は足していく（最新が正本。戻した記録も残る）。
- **行がいま「かけない」かは、データベースの1か所**（`salescall.task_excluded`）。**かけるのを止めるかは `salescall.task_excluded_now`**
  （「かけない」の印があり、次の予定が営業として新しくかける電話のとき＝`salescall.is_new_call`）。門（理由 excluded）も、候補を
  出す処理も、これを読む。お詫びの電話・途中で切れたあとのかけ直し・お客様と約束したかけ直しは「かけない」の例外＝かける（2026-10-18
  ユーザー：「かけないことを除外ということなので、その3つは途中でかけないに切り替えてもかけるという意味ですよ」）。
- 発信禁止とは別のもの：発信禁止はお客様（番号）を止め、自動では戻さない。「かけない」は依頼者・契約者の都合でその行をかけないで
  おく印で、ダッシュボードで戻せる（戻したら、ふつうにかける）。
- 「かけない」を付けた取引の中で、その行のまだ発信していない予約を取り消す（DESIGN.md 5-2-1 の2）。
- 一覧の「かけない」の列の読み方は `read_mark` の1か所（推測で読まない：読めない値は、その行を止めて理由を見せる）。
"""

from __future__ import annotations

import unicodedata
from typing import Any
from uuid import UUID

from salescall.core import attempts
from salescall.core.access import Tx

# 「かけない」の列の値（文字の形をそろえ、大文字・小文字を区別しない形で比べる）。空はかける
YES = frozenset({"かけない", "除外", "はい", "1", "true", "○", "◯", "●", "✓", "✔", "レ", "x", "×", "✕", "✖"})
NO = frozenset({"かける", "いいえ", "0", "false"})
IMPORT_REASON = "一覧の「かけない」の列"


def read_mark(cell: Any) -> bool | None:
    """「かけない」の列の欄を読む：True＝かけない・False＝かける（空を含む）・None＝読めない（推測しない）。
    Excel のチェック（TRUE・FALSE）はそのまま。"""
    if getattr(cell, "flag", None) is not None:
        return bool(cell.flag)
    if cell.problem is not None:
        return None
    text = unicodedata.normalize("NFKC", cell.text or "").strip().casefold()
    if text == "" or text in NO:
        return False
    return True if text in YES else None


def mark(tx: Tx, task_ids: list[UUID], *, excluded: bool, reason: str, source: str = "dashboard",
         import_id: UUID | None = None) -> list[UUID]:
    """取引の依頼者の行に「かけない」を付ける（excluded=True）・外す（False）。いまと同じ行は記録を足さない。付け外しした行を返す。"""
    if not reason.strip():
        raise ValueError("理由が要ります")
    changed = [r["task_id"] for r in tx.all(
        "SELECT t.task_id FROM salescall.tasks t WHERE t.task_id = ANY(%s) AND salescall.task_excluded(t.task_id) <> %s"
        " ORDER BY t.task_id", (list(task_ids), excluded))]
    for task_id in changed:
        tx.execute("INSERT INTO salescall.task_exclusions (tenant_id, client_id, task_id, excluded, reason, source, import_id,"
                   "                                      decided_by)"
                   " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, %s, %s, %s, salescall.cur_actor())",
                   (task_id, excluded, reason, source, import_id))
    if excluded and changed:
        attempts.release(tx, [r["attempt_id"] for r in tx.all(
            "SELECT attempt_id FROM salescall.attempts WHERE task_id = ANY(%s) AND state = 'claimed'"
            "   AND salescall.is_new_call(purpose, promised)", (changed,))])   # 例外の3つの予約は取り消さない
    return changed


def excluded(tx: Tx, task_id: UUID) -> bool:
    """その行が、いま「かけない」か（データベースの1か所を読むだけ）。"""
    return tx.one("SELECT salescall.task_excluded(%s) AS x", (task_id,))["x"]
