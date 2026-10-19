"""候補を出す処理（スケジューラー。DESIGN.md 5-1 の3・5-4）。持ち主 CL-2。

- **判定はしない**：かけてよいかは門（`gate.claim`）だけが決める。ここは「予定の時刻を過ぎた行」を、門に渡す順に並べるだけ。
- 門が必ず断る行は出さない（門と同じ1か所の決まりを読む）：次の予定が「かける」でない・時刻の前・前の発信の締めが済んでいない
  （salescall.task_awaiting_decision）・「かけない」で止まる（salescall.task_excluded_now。「かけない」ならお詫び・かけ直し・約束したかけ直しは例外＝かける。「強制かけない」は例外なし。0078・0085）・進行中の発信がある。依頼者を無効にした（salescall.client_active）・依頼者より広い範囲で
  止まっている（stops.open_stops）依頼者の行も出さない（門に渡しても断られ、行ごとに予定を書き直し続けるため）。業務の範囲の
  停止は門に任せる（お詫びの電話は、業務の品質の停止では止めない＝門の決まり）。
- 並べ方：お客様と約束したかけ直しを先に、そのあとは予定の時刻の古い順。回線ごとの処理は、約束したかけ直しだけ（promised_only）
  をすべての依頼者について先に出してから、ふつうの電話へ進む（依頼者をまたいでも約束が先＝2026-10-19 ユーザー）。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from salescall.core import stops
from salescall.core.access import Tx


def clients_for_line(tx: Tx, line_id: UUID) -> list[UUID]:
    """その回線でかけてよい依頼者（契約者の取引）：回線に依頼者を割り当てていればその依頼者だけ、共有の回線なら契約者のすべての
    依頼者のうち、有効なもの（止まっているかは、依頼者の取引の `due_tasks` が見る＝依頼者の範囲の停止は依頼者の取引でしか見えない）。"""
    line = tx.one("SELECT tenant_id, client_id FROM salescall.lines WHERE line_id = %s", (line_id,))
    if line is None:
        return []
    return [r["client_id"] for r in tx.all(
        "SELECT client_id FROM salescall.clients WHERE tenant_id = %s AND (%s::uuid IS NULL OR client_id = %s)"
        " AND salescall.client_active(client_id) ORDER BY created_at", (line["tenant_id"], line["client_id"], line["client_id"]))]


def due_tasks(tx: Tx, limit: int, *, skip_businesses: set[UUID] | frozenset[UUID] = frozenset(),
              promised_only: bool = False) -> list[dict[str, Any]]:
    """取引の依頼者の、いまかけてよさそうな行（門に渡す順）。依頼者より広い範囲（全体・契約者・依頼者）で止まっていれば、無し。
    skip_businesses＝この回、門が業務の理由（gate.BUSINESS_REASONS）で断った業務（その業務の行を出さない。判断は門がしたもの）。
    promised_only＝お客様と約束したかけ直しだけ（回線ごとの処理が、すべての依頼者の約束を先に済ませるのに使う）。"""
    me = tx.one("SELECT salescall.cur_tenant() AS t, salescall.cur_client() AS c")
    if stops.open_stops(tx, tenant_id=me["t"], client_id=me["c"], business_id=None):
        return []
    return tx.all(
        "SELECT t.task_id, r.business_id FROM salescall.tasks t"
        " JOIN salescall.runs r ON r.client_id = t.client_id AND r.run_id = t.run_id"
        " JOIN LATERAL (SELECT n.kind, n.call_at, n.promised FROM salescall.task_next_actions n WHERE n.task_id = t.task_id"
        "               ORDER BY n.seq DESC LIMIT 1) n ON true"
        " WHERE n.kind = 'call' AND n.call_at <= now()"
        "   AND NOT salescall.task_awaiting_decision(t.task_id) AND NOT salescall.task_excluded_now(t.task_id)"
        "   AND NOT EXISTS (SELECT 1 FROM salescall.attempts a WHERE a.task_id = t.task_id AND salescall.is_active(a.state))"
        "   AND r.business_id <> ALL(%s::uuid[]) AND (n.promised OR NOT %s)"
        " ORDER BY n.promised DESC, n.call_at, t.task_id LIMIT %s", (list(skip_businesses), promised_only, limit))
