"""候補を出す処理（スケジューラー。DESIGN.md 5-1 の3・5-4）。持ち主 CL-2。

- **判定はしない**：かけてよいかは門（`gate.claim`）だけが決める。ここは「予定の時刻を過ぎた行」を、門に渡す順に並べるだけ。
- 門が必ず断る行は出さない（門と同じ1か所の決まりを読む）：次の予定が「かける」でない・時刻の前・前の発信の締めが済んでいない
  （salescall.task_awaiting_decision）・「かけない」で止まる（salescall.task_excluded_now。「かけない」ならお詫び・かけ直し・約束した
  かけ直しは例外＝かける。「強制かけない」は例外なし。0078・0085）・その行か、かける番号に、いま進行中の発信がある（データベースの
  「進行中は1本」の守りと同じ。期限の切れた予約は門が取り消すので数えない。番号はほかの依頼者・契約者の発信も見る＝salescall.number_live。
  ⛔ 見直し15：前は門が「進行中」で断るたびに、行の予定を5分先へ書き直していた）・止まっている（全体・契約者・依頼者・業務の
  範囲。その停止でも通す、お客様との約束を果たす電話は出す。決まりはデータベースの salescall.blocking_stops の1か所＝門と同じ。
  例：品質の停止では、お詫びの電話は通す。⛔ 見直し15：前は業務の
  範囲の停止を門に任せ、止まった業務の行の予定を、門が5分ごとに書き直し続けた）。
- 並べ方：お客様と約束したかけ直しを先に、そのあとは予定の時刻の古い順。回線ごとの処理は、約束したかけ直しだけ（promised_only）
  をすべての依頼者について先に出してから、ふつうの電話へ進む（依頼者をまたいでも約束が先＝2026-10-19 ユーザー）。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from salescall.core import gate
from salescall.core.access import Tx


def clients_for_line(tx: Tx, line_id: UUID) -> list[UUID]:
    """その回線でかけてよい依頼者（契約者の取引）：回線に依頼者を割り当てていればその依頼者だけ、共有の回線なら契約者のすべての
    依頼者のうち、有効なもの（止まっているかは、依頼者の取引の `due_tasks` が見る＝依頼者の範囲の停止は依頼者の取引でしか見えない）。"""
    line = tx.one("SELECT tenant_id, client_id FROM salescall.lines WHERE line_id = %s", (line_id,))
    if line is None:
        return []
    return [r["client_id"] for r in tx.all(
        "SELECT client_id FROM salescall.clients WHERE tenant_id = %s AND salescall.line_serves(%s, client_id)"
        " AND salescall.client_active(client_id) ORDER BY created_at", (line["tenant_id"], line["client_id"]))]


def due_tasks(tx: Tx, limit: int, *, skip_businesses: set[UUID] | frozenset[UUID] = frozenset(),
              skip_runs: set[UUID] | frozenset[UUID] = frozenset(), skip_stage: set[UUID] | frozenset[UUID] = frozenset(),
              promised_only: bool = False, line_id: UUID | None = None) -> list[dict[str, Any]]:
    """取引の依頼者の、いまかけてよさそうな行（門に渡す順）。
    skip_businesses＝この回、門が業務の理由（gate.BUSINESS_REASONS）で断った業務（その業務の行を出さない。判断は門がしたもの）。
    skip_runs＝この回、門が実施回の版の理由（gate.RUN_REASONS）で断った実施回（その実施回の行を出さない）。skip_stage＝門が品質の段の
    理由（gate.STAGE_REASONS）で断った実施回（決まった文面だけの電話＝品質の段を見ない電話のほかを出さない。どの電話がそれに当たるかは
    門と同じ salescall.fixed_text_kind の1か所）。
    promised_only＝お客様と約束したかけ直しだけ（回線ごとの処理が、すべての依頼者の約束を先に済ませるのに使う）。
    門が必ず断り、いつまで待てば変わるか分からない行（門の理由の表で扱いが「候補に出さない」の理由。gate.waits_unknown の1か所＝
    門と同じ関数：契約者の承認待ち・同じお客様の発信で断られたかが未決・目的のお客様を呼ぶ材料が無い・話してよい別の声が無い・
    待つ時刻の分からない約束。line_id＝その回線で探す回線ごとの処理なら、その回線の発信番号でほかの依頼者がかけた番号も）も出さない
    （予定は書き直さない。変われば、また出る。⛔ 見直し16・17a：前は門が断るたびに、次の動きが予定を5分先へ書き直し、行ごとに記録が
    1日288ずつ増えた）。"""
    out: list[dict[str, Any]] = []
    page = max(limit, 50)
    offset = 0
    while len(out) < limit:
        rows = _candidates(tx, page, offset, skip_businesses, skip_runs, skip_stage, promised_only)
        out += [r for r in rows if gate.waits_unknown(tx, r["task_id"], line_id) is None][:limit - len(out)]
        if len(rows) < page:
            break
        offset += page
    return out


def _candidates(tx: Tx, page: int, offset: int, skip_businesses: set[UUID] | frozenset[UUID],
                skip_runs: set[UUID] | frozenset[UUID], skip_stage: set[UUID] | frozenset[UUID],
                promised_only: bool) -> list[dict[str, Any]]:
    """データベースだけで決まる候補（門に渡す順。下の条件は門と同じ1か所の決まりを読む）。"""
    return tx.all(
        "SELECT t.task_id, r.business_id, t.run_id FROM salescall.tasks t"
        " JOIN salescall.runs r ON r.client_id = t.client_id AND r.run_id = t.run_id"
        " JOIN LATERAL (SELECT c.kind, c.call_at, c.promised, c.purpose, c.contact_id FROM salescall.current_next_actions c"
        "               WHERE c.task_id = t.task_id) n"
        "   ON true"   # 行ごとに索引で読む（いまの予定を選ぶのはデータベースの見方の1か所＝0094）
        " JOIN salescall.task_numbers k ON k.task_id = t.task_id AND k.contact_id = n.contact_id"
        " WHERE n.kind = 'call' AND n.call_at <= now()"
        "   AND NOT salescall.task_awaiting_decision(t.task_id) AND NOT salescall.task_excluded_now(t.task_id)"
        "   AND NOT EXISTS (SELECT 1 FROM salescall.attempts a WHERE a.task_id = t.task_id"
        "                   AND salescall.is_live(a.state, a.claim_expires_at))"
        "   AND NOT salescall.number_live(k.phone_e164)"
        "   AND NOT EXISTS (SELECT 1 FROM salescall.blocking_stops(t.tenant_id, t.client_id, r.business_id, NULL,"
        "                                                          salescall.keeping_kind(n.purpose, n.promised)))"
        "   AND r.business_id <> ALL(%s::uuid[]) AND t.run_id <> ALL(%s::uuid[])"
        "   AND (t.run_id <> ALL(%s::uuid[]) OR salescall.fixed_text_kind(salescall.keeping_kind(n.purpose, n.promised)))"
        "   AND (n.promised OR NOT %s)"
        " ORDER BY n.promised DESC, n.call_at, t.task_id LIMIT %s OFFSET %s",
        (list(skip_businesses), list(skip_runs), list(skip_stage), promised_only, page, offset))
