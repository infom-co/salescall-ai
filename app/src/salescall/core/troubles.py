"""こちらのプログラムの不具合で終えた電話を、その行だけの問題か、すべての行にかかわる問題かで見極める（DESIGN.md 5-4・7-2-1・9-1。
表の作り 0139）。持ち主 CL-2。

2026-10-25 ユーザー（管理者）決定：「同じ行で、こちらのプログラムの不具合が続くとき：その行だけの問題なのか、すべての行にかかわる問題なのかを
見極めないといけないでしょ？その行だけなら、契約者(管理者含む)と、開発AIの担当、記録にそれを伝え、2回続いたら、次の行へ。3行同じ問題なら、
甚大な問題と判断。」

- **こちらの不具合で終えた電話**（試行の trouble のうち、こちらの不具合の理由。どれがそうかはデータベースの salescall.trouble_ours の1か所。
  理由ごとの直す担当・止め方は core/errors の TROUBLES と処置の表）は、番号の4回・回線の異常・行の回数のどれにも数えない（salescall.is_ours）。
  「声が聞き取れない」（poor_audio）は、こちらの不具合に数えない（2026-10-25 夜 管理者：お客様と話している途中で切れた電話と同じ。違う
  お客様で続けば回線の異常＝supervisor/line.py の `_unrung`。0141）＝ここを通らない。
- **電話ごとに伝える**（`after_trouble` の1か所。呼ぶのは通話ごとの口＝supervisor/line.py の `_Call` だけ）：その電話の出来事（E3）を、
  回線・行・契約者の状態の表を引く入口（core/health.after_call。DESIGN.md 17章）へ渡す。表が決めたこと（記録・契約者と管理者への知らせ＝
  電話ごと。その行が復旧を待っていること＝errors.RECOVERY_WAIT を文に入れる・直す担当への修正依頼＝同じ不具合で1つ・同じ行で2回続けば
  休ませて1回知らせる・違う3つの行で同じ種類なら契約者の新しい電話を止める）は、入口が行う（電話ごとの3つは watch.record_error の1か所。
  エラーの中身は、ここで作って渡す＝出どころ「通話」・データベースへの書き込みが進まなかったときは「保存」＝errors.TROUBLE_SOURCES。0143）。
  ⛔ 3段目まで：ここで watch.after_error（記録・知らせ・修正依頼・止めるか＝consecutive_errors）と、休み始めの知らせ（task_resting）を出し、
  表の入口は影だった。
- **その行だけの問題**：行の欄（17-1-2）が、1回目は復旧待ち（R-5）、同じ行で2回続けば休み（R-8）。門と候補は行の欄を読む。
- **戻す道は2つ**：(1) その理由の修正依頼が直したと記録され（fix_requests.report_fixed）、そのあと動いているプログラムの版が替わった（E11。
  決めるのはデータベースの salescall.trouble_fixed_at の1か所。2026-10-25 夜 管理者の決定）(2) 管理者・契約者が戻す（`restore` の1か所。
  画面＝CL-3 が呼ぶ。説明は help_texts の task.trouble_restore。休みの行と、復旧待ちの行＝【当(7)】。⛔ 0139〜0145：復旧待ちの行には手の
  出口が無かった＝17-8 の (8)）。
- **すべての行にかかわる問題**：違う行で同じ種類が設定 stop.trouble.stop_after_rows（初め3行）になったら、契約者の欄が「こちらの不具合で止め」
  （C-1。止めている種類の組を持つ）。新しい電話を止める（契約者の範囲の停止の記録＝core/stops。話している電話は切らない）。止めた種類を
  直した版（E11）か、管理者・契約者が解く（E12）と戻る。こちらの不具合なしにお客様と話し終えた電話（同じ契約者）があれば、違う行の数を
  数え直す（C-3）。
"""

from __future__ import annotations

from uuid import UUID

from salescall.core import errors, health
from salescall.core.access import Access, Actor, Tx
from salescall.core.errors import ErrorReport
from salescall.core.health_rules import TaskStage

RESTORABLE = (TaskStage.RESTING, TaskStage.AWAIT_RECOVERY)   # 管理者・契約者が手で戻せる行の欄の段（R-10・R-7）


def report_of(reason: str, what: str) -> ErrorReport:
    """こちらのプログラムの不具合で終えた電話の、エラーの中身（1か所。出どころは理由ごと＝データベースが止まった＝保存。0143）。
    what＝何が起きたか（決まった言葉と誤りの種類の名前だけ。お客様の情報・誤りの文は入れない＝supervisor.report と同じ決まり）。"""
    return ErrorReport(errors.TROUBLE_SOURCES.get(reason, "call"), None, f"{reason}：{what}", trouble=reason)


def resting(tx: Tx, task_id: UUID) -> bool:
    """その行が、こちらのプログラムの不具合で休んでいるか（行の欄が「休み」＝17-1-2）。"""
    return health.task_of(tx, task_id).stage is TaskStage.RESTING


def after_trouble(access: Access, actor: Actor, *, tenant_id: UUID, client_id: UUID, line_id: UUID, attempt_id: UUID,
                  reason: str, what: str) -> str:
    """こちらのプログラムの不具合で通話を終えたあと（試行に理由を残したあと）に呼ぶ入口（1か所）：その電話の出来事（E3）を状態の表の
    入口へ渡す（記録・知らせ・修正依頼・行の休み・契約者の止めは、表が決めて入口が行う）。エラーの種類を返す。"""
    report = report_of(reason, what)
    access.in_client(actor, tenant_id, client_id, lambda tx: health.after_call(tx, attempt_id, report=report))
    return errors.classify(report)


def restore(tx: Tx, task_id: UUID, *, reason: str) -> bool:
    """休んだ行・復旧待ちの行を戻す（管理者か契約者だけ。取引の依頼者の行。理由は必ず書く。1か所＝画面が呼ぶ）。どちらでもなければ
    何もしない（False）。戻したあと、また同じ行で続けて終われば、また休む（表の行の欄が数える）。"""
    if not reason.strip():
        raise ValueError("理由が要ります")
    if health.task_of(tx, task_id).stage not in RESTORABLE:
        return False
    tx.execute("INSERT INTO salescall.task_trouble_restores (tenant_id, client_id, task_id, reason, restored_by, actor_kind)"
               " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, salescall.cur_actor(),"
               "         salescall.cur_actor_kind()::salescall.actor_kind)", (task_id, reason))
    health.by_hand_task(tx, task_id, reason)   # 手で戻した（E12）：表を引く入口（R-10・R-7）
    return True
