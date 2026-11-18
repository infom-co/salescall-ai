"""修正依頼（仕組みの不具合を、止めずに担当へ直してもらう。人は確かめ・承認だけ）。持ち主 CL-2。表の作り 0048。

2026-10-17 ユーザー：「止めずに CL1 か CL2 に修正依頼を出さないといけないだろ。人はそのプロセスをチェックもしくは承認するだけだよ」。
- 修正依頼を出すのは、この部品の `request` だけ。どの不具合をどの担当が直すかは、呼ぶ側の決まった1か所
  （エラー＝core/errors.py の `Action.fixer`・判定できない原因＝`judge.UNKNOWN_CAUSE_FIXERS`）。
- 担当（CL-1・CL-2・CL-3）は `open_requests` で受け、直したら `report_fixed`（担当の作業＝システムの処理）。
  管理者は、直した中身を確かめて `approve` するだけ（データベースが守る）。
- 修正依頼に、お客様の電話番号・名前・発話の中身を入れない（何の不具合かと、記録の ID だけ）。
"""

from __future__ import annotations

import json
from typing import Any

from salescall.core.access import Tx

ASSIGNEES = ("CL-1", "CL-2", "CL-3")   # CL-1＝会話・別の耳・録音／CL-2＝業務の土台・保存・回線の部品／CL-3＝画面と出力


def request(tx: Tx, assignee: str, kind: str, *, key: str, summary: str, refs: dict[str, Any] | None = None) -> bool:
    """修正依頼を出す（どの取引の中からでも）。key＝どの不具合か（時刻や通話を入れない）。同じ不具合は、担当が「直した」と報告する
    まで1つ（報告のあとにまた起きたら、新しく出す。決めるのはデータベースの request_fix の1か所）。新しく出したら True。"""
    if assignee not in ASSIGNEES:
        raise ValueError(f"修正依頼の担当は {ASSIGNEES} のどれかです（{assignee!r}）")
    return tx.one("SELECT salescall.request_fix(%s, %s, %s, %s, %s) AS new",
                  (assignee, kind, f"fix:{key}", summary,
                   json.dumps(refs or {}, ensure_ascii=False, default=str)))["new"]


def open_requests(tx: Tx, assignee: str) -> list[dict[str, Any]]:
    """その担当の、まだ直していない修正依頼（古い順）。担当の作業（全体の取引）が読む。"""
    return tx.all("SELECT q.request_id, q.kind, q.summary, q.refs, q.requested_at FROM salescall.fix_requests q"
                  " WHERE q.assignee = %s AND NOT EXISTS (SELECT 1 FROM salescall.fix_reports r"
                  "                                        WHERE r.request_id = q.request_id)"
                  " ORDER BY q.request_id", (assignee,))


def report_fixed(tx: Tx, request_id: int, note: str) -> None:
    """担当が直した（何を直したか）。担当の作業（システムの処理）だけが書ける。"""
    tx.execute("INSERT INTO salescall.fix_reports (request_id, note, reported_by) VALUES (%s, %s, salescall.cur_actor())",
               (request_id, note))


def approve(tx: Tx, request_id: int) -> None:
    """管理者が、直した中身を確かめて承認する（管理者だけ）。"""
    tx.execute("INSERT INTO salescall.fix_approvals (request_id, approved_by) VALUES (%s, salescall.cur_actor())",
               (request_id,))
