"""こちらのプログラムの不具合で終えた電話を、その行だけの問題か、すべての行にかかわる問題かで見極める（DESIGN.md 5-4・7-2-1・9-1。
表の作り 0139）。持ち主 CL-2。

2026-10-25 ユーザー（管理者）決定：「同じ行で、こちらのプログラムの不具合が続くとき：その行だけの問題なのか、すべての行にかかわる問題なのかを
見極めないといけないでしょ？その行だけなら、契約者(管理者含む)と、開発AIの担当、記録にそれを伝え、2回続いたら、次の行へ。3行同じ問題なら、
甚大な問題と判断。」

- **こちらの不具合で終えた電話**（試行の trouble のうち、こちらの不具合の理由。どれがそうかはデータベースの salescall.trouble_ours の1か所。
  理由ごとの直す担当・止め方は core/errors の TROUBLES と処置の表）は、番号の4回・回線の異常・行の回数のどれにも数えない（salescall.is_ours）。
  「声が聞き取れない」（poor_audio）は、こちらの不具合に数えない（2026-10-25 夜 管理者：お客様と話している途中で切れた電話と同じ。違う
  お客様で続けば回線の異常＝supervisor/line.py の `_unrung`。0141）＝ここを通らない。
- **電話ごとに伝える**（`after_trouble` の1か所。呼ぶのは通話ごとの口＝supervisor/line.py の `_Call` だけ）：エラーの入口
  （watch.after_error）に、出どころ「通話」で渡す＝記録（error_events）・契約者と管理者への知らせ（電話ごと）・直す担当への修正依頼
  （同じ不具合で1つ）。止めるかも同じ道（watch.decide_stop）。
- **その行だけの問題**：同じ行で続けて salescall.trouble_rest_after()（2回）になったら、その行は休む（データベースの salescall.task_resting の
  1か所。門の理由 task_resting と、候補を出す処理が読む）。休み始めたら、契約者と管理者に1回知らせる（`task_resting`）。
- **戻す道は2つ**：(1) その理由の修正依頼が直したと記録され（fix_requests.report_fixed）、そのあと動いているプログラムの版が替わった（中央の
  見守り役が版を書く＝supervisor/updates.note。決めるのはデータベースの salescall.trouble_fixed_at の1か所＝止めた停止を解くのも同じ。
  数えるのは、その時より後だけ。2026-10-25 夜 管理者の決定。⛔ 0139：版が替われば、どの理由でも戻した）(2) 管理者・契約者が戻す（`restore` の
  1か所。画面＝CL-3 が呼ぶ。説明は help_texts の task.trouble_restore）。
- **すべての行にかかわる問題**：違う行で同じ理由が設定 stop.trouble.stop_after_rows（初め3行）になったら、その契約者の新しい電話を止める
  （watch.decide_stop。話している電話は切らない。その種類を直したと記録され、そのあと版が替わったら watch.resume_fixed が解く。管理者・
  契約者も解ける）。こちらの不具合なしにお客様と話し終えた電話（同じ契約者）があれば、違う行の数を数え直す（続けて3行のときだけ止める。
  データベースの consecutive_errors の program_fixed。0141）。
"""

from __future__ import annotations

from uuid import UUID

from salescall.core import notify, watch
from salescall.core.access import Access, Actor, Tx
from salescall.core.errors import ErrorReport

RESTING_SUMMARY = ("こちらのプログラムの不具合で、同じ行の電話が続けて終わりました。この行は休ませ、次の行へ進みます"
                   "（直したプログラムの版に替わると自動で戻ります。管理者・契約者が戻すこともできます）。")


def resting(tx: Tx, task_id: UUID) -> bool:
    """その行が、こちらのプログラムの不具合で休んでいるか（決まりはデータベースの salescall.task_resting の1か所）。"""
    return tx.one("SELECT salescall.task_resting(%s) AS r", (task_id,))["r"]


def after_trouble(access: Access, actor: Actor, *, tenant_id: UUID, client_id: UUID, line_id: UUID, attempt_id: UUID,
                  reason: str, what: str) -> str:
    """こちらのプログラムの不具合で通話を終えたあと（試行に理由を残したあと）に呼ぶ入口（1か所）：記録・契約者と管理者への知らせ・
    直す担当への修正依頼・止めるかを、エラーの入口の1か所で行い、その行が休み始めたら1回知らせる。エラーの種類を返す。
    what＝何が起きたか（決まった言葉と誤りの種類の名前だけ。お客様の情報・誤りの文は入れない＝supervisor.report と同じ決まり）。"""
    kind = watch.after_error(access, actor, ErrorReport("call", None, f"{reason}：{what}", trouble=reason),
                             tenant_id=tenant_id, client_id=client_id, line_id=line_id, attempt_id=attempt_id)

    def tell(tx: Tx) -> None:
        row = tx.one("SELECT task_id FROM salescall.attempts WHERE attempt_id = %s", (attempt_id,))
        if row is not None and resting(tx, row["task_id"]):
            notify.emit(tx, "task_resting", key=f"task_resting:{attempt_id}", summary=RESTING_SUMMARY, admin=True,
                        refs={"task_id": row["task_id"], "attempt_id": attempt_id, "kind": kind})
    access.in_client(actor, tenant_id, client_id, tell)
    return kind


def restore(tx: Tx, task_id: UUID, *, reason: str) -> bool:
    """休んだ行を戻す（管理者か契約者だけ。取引の依頼者の行。理由は必ず書く。1か所＝画面が呼ぶ）。休んでいなければ何もしない（False）。
    戻したあと、また同じ行で続けて終われば、また休む（数えるのは戻した時より後だけ）。"""
    if not reason.strip():
        raise ValueError("理由が要ります")
    if not resting(tx, task_id):
        return False
    tx.execute("INSERT INTO salescall.task_trouble_restores (tenant_id, client_id, task_id, reason, restored_by, actor_kind)"
               " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, salescall.cur_actor(),"
               "         salescall.cur_actor_kind()::salescall.actor_kind)", (task_id, reason))
    return True
