"""こちらのプログラムの不具合を、その行だけの問題か、すべての行にかかわる問題かで見極める試験（core/troubles.py・表の作り 0139。
DESIGN.md 5-4・7-2-1・9-1）。

2026-10-25 ユーザー（管理者）決定：「同じ行で、こちらのプログラムの不具合が続くとき：その行だけの問題なのか、すべての行にかかわる問題なのかを
見極めないといけないでしょ？その行だけなら、契約者(管理者含む)と、開発AIの担当、記録にそれを伝え、2回続いたら、次の行へ。3行同じ問題なら、
甚大な問題と判断。」
"""

from __future__ import annotations

import time
import uuid

from salescall.core import attempts, errors, fix_requests, gate, next_action, scheduler, settings, stops, troubles
from salescall.core.access import Rejected
from salescall.core.phone import normalize
from salescall.supervisor import jobs, line, updates
from tests.support import ADMIN, CLEAN, OPERATOR, SYSTEM, fictional_jp_fixed, owner_access
from tests.test_gate import HOLDER, FakeDriver
from tests.test_supervisor import Runner, SupervisorBase


class Troubled:
    """通話を進める部品の代わり：お客様が出る前（answered=False）か、出て話し始めたあと（answered=True）に、こちらの不具合で終える
    （reason＝通話を進める部品が見つけた甚大な問題。runner_error なら、通話を終えないまま落ちる）。"""

    def __init__(self, reason: str, answered: bool = False) -> None:
        self.reason, self.answered = reason, answered

    def run(self, access, actor, claim, *, tenant_id, client_id, call=None):
        call.beat()
        path = ("ringing", "answered", "in_conversation") if self.answered else ("ringing",)

        access.in_client(actor, tenant_id, client_id, lambda tx: [attempts.move(tx, claim.attempt_id, st) for st in path])
        if self.reason == "runner_error":
            raise RuntimeError("試験：通話を進める部品が落ちた")
        call.end_for_trouble(self.reason)


class OurTroubles(SupervisorBase):
    def row(self, s):
        """同じ依頼者の、別の番号の行（いまかけてよい）。"""
        k = self.w.contact(s["tenant"], s["client"], normalize(fictional_jp_fixed()).e164)
        t = self.w.task(s["tenant"], s["client"], s["run"], k)
        self.tx(s, lambda tx: next_action.schedule_now(tx, t, cause="試験"))
        return dict(s, task=t, contact=k)

    def trouble(self, s, reason="ai_malfunction", *, answered=False, task=None, again=None):
        """回線ごとの処理で1本かけ、こちらの不具合で終える（本物の通話ごとの口・門・出口を通る）。締める。again（初めは、お客様が出る
        前に終えたとき）なら、その行の予定をいまにする（こちらのせいのかけ直しは、同じお客様への最短の間隔のあと＝それを待たずに、休みの
        決まりそのものを試す。お客様が出たあとに終えたときは、お詫びとともにすぐかけ直す予定のまま）。かけた試行を返す。"""
        if task is not None and self.tx(s, lambda tx: tx.one("SELECT salescall.awaiting_recovery(%s) AS w", (task,))["w"]):
            self.w.talked(s)   # 前のこちらの不具合のあと、復旧した（同じ契約者で、こちらの不具合なしにお客様と話し終えた電話。0141。
            #                    直した記録は無い＝休んだ行・止めた停止は戻らない。違う行の数は、ここから数え直す）
        d = FakeDriver()
        got = line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, d, Troubled(reason, answered))
        self.assertIsNotNone(got.called, got)
        self.assertEqual(d.apologized, 1)                                     # お詫びして終えた
        called = self.tx(s, lambda tx: tx.one("SELECT task_id, trouble FROM salescall.attempts WHERE attempt_id = %s",
                                              (got.called,)))
        self.assertEqual(called["trouble"], reason)
        if task is not None:
            self.assertEqual(called["task_id"], task)
        if answered:
            self.tx(s, lambda tx: attempts.record_verification(tx, got.called, summary=CLEAN))
        jobs.settle_ready(self.ctx(s))
        if (not answered) if again is None else again:
            self.tx(s, lambda tx: next_action.schedule_now(tx, called["task_id"], cause="試験：その時刻が来た"))
        return got.called

    def new_version(self):
        """動いているプログラムの版が替わった（中央の見守り役が書く＝supervisor/updates.note）。"""
        self.assertTrue(self.w.a.in_system(SYSTEM, lambda tx: updates.note(tx, f"試験の版-{uuid.uuid4().hex}")))

    def report_fixed(self, assignee, kind):
        """直す担当が、その種類の修正依頼を直したと記録する（core/fix_requests.report_fixed）。"""
        for r in self.requests(assignee, kind):
            self.w.a.in_system(SYSTEM, lambda tx, r=r: fix_requests.report_fixed(tx, r["request_id"], "試験：直した"))

    def finish(self, s, task):
        """その行を終える（試験：ほかの行へかけるため。こちらの不具合のあとの復旧で、また候補に出ないように）。"""
        self.tx(s, lambda tx: next_action._append(tx, task, call_at=None, end_reason="completed", cause="試験：終えた"))

    def resting(self, s, task):
        return self.tx(s, lambda tx: troubles.resting(tx, task))

    def notices(self, s, kind):
        """その契約者の知らせ（契約者あて）と、管理者あての写しの数。"""
        mine = self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: tx.one(
            "SELECT count(*) AS n FROM salescall.notification_events WHERE kind = %s AND tenant_id = %s", (kind, s["tenant"])))["n"]
        admin = self.w.a.in_system(ADMIN, lambda tx: tx.one(
            "SELECT count(*) AS n FROM salescall.notification_events WHERE kind = %s AND origin_tenant = %s", (kind, s["tenant"])))["n"]
        return mine, admin

    def tenant_stops(self, s):
        return [r["cause"] for r in self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: stops.open_stops(
            tx, tenant_id=s["tenant"], client_id=None, business_id=None))]

    def requests(self, assignee, kind):
        return [r for r in self.w.a.in_system(SYSTEM, lambda tx: fix_requests.open_requests(tx, assignee)) if r["kind"] == kind]

    def test_two_in_a_row_on_one_row_rest_it_and_the_line_moves_to_the_next_row(self):
        """その行だけの問題：電話ごとに、記録・契約者と管理者への知らせ・直す担当への修正依頼（理由ごとの担当は処置の表の1か所）。
        同じ行で2回続いたら、その行は休む（門の理由 task_resting・候補を出す処理も出さない＝salescall.task_resting の1か所）。回線は
        次の行へかける。こちらのせいの電話は、番号にも回線にも行の回数にも数えない。知らせと修正依頼は重ならない（電話ごとの知らせ1つ・
        同じ不具合の修正依頼は1つ・見守り役の誤りとして重ねない・休み始めの知らせは1回）。"""
        s = self.ready()
        remaining = self.tx(s, lambda tx: gate.pacing(tx, s["task"])).remaining
        first = self.trouble(s, task=s["task"])
        self.assertFalse(self.resting(s, s["task"]))                           # 1回だけ：まだ休まない
        self.assertEqual(self.tx(s, lambda tx: attempts.blame(tx, first)), "ours")
        second = self.trouble(s, task=s["task"])
        self.assertTrue(self.resting(s, s["task"]))                            # 2回続いた：休む
        self.assertEqual(self.tx(s, lambda tx: attempts.blame(tx, second)), "ours")
        self.w.talked(s)                                                       # 復旧した（同じ契約者で話し終えた）が、直した記録は無い
        self.assertFalse(self.tx(s, lambda tx: tx.one("SELECT salescall.awaiting_recovery(%s) AS w", (s["task"],))["w"]))
        got = self.claim(s)                                                    # 復旧を待つのではなく、休んだまま
        self.assertEqual((type(got).__name__, getattr(got, "reason", None)), ("Denied", "task_resting"))
        self.assertEqual(gate.REASONS["task_resting"].handling, "unoffered")
        self.assertNotIn(s["task"], [r["task_id"] for r in self.tx(s, lambda tx: scheduler.due_tasks(tx, 10))])
        other = self.row(s)
        got = line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, FakeDriver(), Runner())
        self.assertEqual(self.tx(s, lambda tx: tx.one("SELECT task_id FROM salescall.attempts WHERE attempt_id = %s",
                                                      (got.called,)))["task_id"], other["task"])   # 次の行へ
        # どの数にも入れない
        self.assertEqual(self.tx(s, lambda tx: attempts.failed_run(tx, s["contact"]))["n"], 0)
        self.assertEqual(self.tx(s, lambda tx: gate.pacing(tx, s["task"])).remaining, remaining)
        self.assertEqual(self.faults(s), ["call_ai_malfunction"] * 2)          # 記録（回線の異常には数えない）
        # 伝える：電話ごとの知らせ（契約者と管理者）・修正依頼は同じ不具合で1つ・休み始めの知らせは1回・見守り役の誤りに重ねない
        self.assertEqual(self.notices(s, "error_notice"), (2, 2))
        summaries = self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: [r["summary"] for r in tx.all(
            "SELECT summary FROM salescall.notification_events WHERE kind = 'error_notice'")])
        for summary in summaries:                                              # その行が何を待っているか（0143。新しい知らせは増やさない）
            self.assertIn("この行は復旧を待っています", summary)
            self.assertIn("その不具合を直したと記録され、そのあとプログラムが替わったら", summary)
        self.assertEqual(self.notices(s, "task_resting"), (1, 1))
        self.assertEqual(len(self.requests("CL-1", "call_ai_malfunction")), 1)
        self.assertEqual(self.asked_supervisor(s), [])
        self.assertEqual(self.tenant_stops(s), [])                             # 1つの行だけ：止めない

    def test_a_row_is_counted_once_even_if_its_errors_are_recorded_twice(self):
        """すべての行にかかわる問題は、エラーの数ではなく行の数で数える（salescall.consecutive_errors の program_fixed の1か所）。門は
        復旧まで同じ行にかけない（awaiting_recovery）ので、同じ行の2回目はふつう復旧のあと（数え直したあと）になるが、同じ行の記録が
        重なっても（記録のし直しなど）行は1つ。⛔ エラーの数で数えると、同じ行の2回で契約者の電話を止めうる（0143 の見直しで、前の
        試験は同じ行の2回目の前に復旧させるようになり、この守りを見張れなくなった）。"""
        s = self.ready()
        count = lambda: self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: tx.one(   # noqa: E731
            "SELECT salescall.consecutive_errors(ARRAY['call_cannot_proceed'], 'program_trouble', 'program_fixed') AS n"))["n"]
        self.trouble(s, "cannot_proceed", task=s["task"], again=False)
        self.assertEqual(count(), 1)
        owner_access().in_system(SYSTEM, lambda tx: tx.execute(
            "INSERT INTO salescall.error_events (tenant_id, client_id, line_id, attempt_id, source, kind, code, message)"
            " SELECT tenant_id, client_id, line_id, attempt_id, source, kind, code, message FROM salescall.error_events"
            " WHERE tenant_id = %s AND kind = 'call_cannot_proceed'", (s["tenant"],)))
        self.assertEqual(count(), 1)                                           # 同じ行：1つのまま
        b = self.row(s)
        self.trouble(s, "cannot_proceed", task=b["task"], again=False)
        self.assertEqual(count(), 2)

    def test_the_same_reason_on_three_rows_stops_the_contractor_and_tells_once(self):
        """すべての行にかかわる問題：違う行で同じ理由が3行（設定 stop.trouble.stop_after_rows）になったら、その契約者の新しい電話を
        止める（止め方は watch.after_error → decide_stop の1か所。原因 program_trouble）。同じ行の2回目は数えない。止めた知らせは
        契約者と管理者へ1回。修正依頼は同じ不具合で1つ（重ねない）。その種類の修正依頼が直したと記録され、そのあと版が替わったら解け、
        休んだ行も戻る（2026-10-25 夜 管理者の決定 案A。⛔ 0139：直した記録が無くても、版が替われば戻した）。"""
        s = self.ready()
        self.assertEqual(self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: settings.resolve(
            tx, "stop.trouble.stop_after_rows", tenant_id=s["tenant"], client_id=None, business_id=None)), 3)
        o = self.ready()                                                       # ほかの種類の不具合（ほかの契約者で。直す記録は種類ごと）
        self.trouble(o, "ai_malfunction", task=o["task"])
        self.trouble(s, "cannot_proceed", task=s["task"])
        self.trouble(s, "cannot_proceed", task=s["task"], again=False)         # 同じ行の2回目：行の数は1のまま（行は休む）
        b = self.row(s)
        self.trouble(s, "cannot_proceed", task=b["task"], again=False)
        self.assertEqual(self.tenant_stops(s), [])                             # 2行：まだ止めない
        c = self.row(s)
        self.trouble(s, "cannot_proceed", task=c["task"], again=False)
        self.assertEqual(self.tenant_stops(s), ["program_trouble"])            # 3行：止める
        self.assertEqual(self.notices(s, "stopped"), (1, 1))
        self.assertEqual(len(self.requests("CL-1", "call_cannot_proceed")), 1)
        d = self.row(s)
        got = line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, FakeDriver(), Runner())
        self.assertIsNone(got.called)                                          # 新しい電話はかけない
        self.assertEqual(self.claim(s, task=d["task"]).reason, "stopped")
        self.assertTrue(self.resting(s, s["task"]))
        count = lambda: self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: tx.one(   # noqa: E731
            "SELECT salescall.consecutive_errors(ARRAY['call_cannot_proceed'], 'program_trouble', 'program_fixed') AS n"))["n"]
        # 版が替わっただけ（直した記録が無い）：休んだ行も、止めた停止も戻らない
        self.new_version()
        jobs.fixed_stops(self.ctx(s))
        self.assertEqual((self.tenant_stops(s), self.resting(s, s["task"]), count()), (["program_trouble"], True, 3))
        # ほかの種類を直したと記録され、版が替わった：戻らない
        self.report_fixed("CL-1", "call_ai_malfunction")
        self.new_version()
        jobs.fixed_stops(self.ctx(s))
        self.assertEqual((self.tenant_stops(s), self.resting(s, s["task"]), count()), (["program_trouble"], True, 3))
        # 直したと記録された（まだ版は替わっていない）：まだ戻らない
        self.report_fixed("CL-1", "call_cannot_proceed")
        jobs.fixed_stops(self.ctx(s))
        self.assertEqual((self.tenant_stops(s), self.resting(s, s["task"])), (["program_trouble"], True))
        # そのあと版が替わった：休んだ行は戻り、数え直し、止めた停止は決まった間隔の仕事が解く
        self.new_version()
        self.assertFalse(self.resting(s, s["task"]))
        self.assertEqual(count(), 0)                                           # 直した時から数え直す（停止を解く前から）
        jobs.fixed_stops(self.ctx(s))
        self.assertEqual(self.tenant_stops(s), [])
        self.assertEqual(self.notices(s, "resumed"), (1, 0))

    def test_different_reasons_on_three_rows_do_not_stop(self):
        """違う理由なら、行が3つでも止めない（理由ごとに数える＝処置の表の per_call）。修正依頼は、理由ごとの担当へ（会話の部品は CL-1・
        データベースへの書き込みが進まないのは保存の CL-2＝0143）。"""
        s = self.ready()
        for reason in ("ai_malfunction", "runner_error", "cannot_proceed"):
            self.trouble(s, reason, task=self.row(s)["task"] if reason != "ai_malfunction" else s["task"], again=False)
        self.assertEqual(self.tenant_stops(s), [])
        self.assertTrue(self.requests("CL-1", "call_runner_error"))           # 修正依頼は直す担当へ（ほかの試験の依頼と同じ1つ）
        self.assertEqual({r: errors.ACTIONS[k].fixer for r, k in errors.TROUBLES.items()},
                         {"frozen": "CL-1", "ai_malfunction": "CL-1", "cannot_proceed": "CL-1", "runner_error": "CL-1",
                          "storage_stuck": "CL-2"})
        self.trouble(s, "ai_malfunction", task=self.row(s)["task"], again=False)   # 同じ理由は2行：まだ止めない
        self.assertEqual(self.tenant_stops(s), [])

    def test_a_contractor_or_the_admin_can_put_a_resting_row_back_and_our_calls_never_count(self):
        """戻す道の2つ目：管理者か契約者が戻す（core/troubles.restore の1か所。システムの処理は戻せない＝データベースが守る。休んで
        いない行は戻さない）。戻したあとは、戻した時より後だけを数える（また2回続けば、また休む）。お客様が出たあとにこちらのせいで
        終えた電話は、いつも「途中で切れた」（行の回数に数えず、お詫びとともにかけ直す）。お客様が出る前なら、いつもこちらのせい
        （⛔ 0136：同じ行で3回目からは、ふつうの鳴らなかった電話として番号に数えた）。"""
        s = self.ready()
        remaining = self.tx(s, lambda tx: gate.pacing(tx, s["task"])).remaining
        for _ in range(2):
            aid = self.trouble(s, "ai_malfunction", answered=True, task=s["task"])
            self.assertEqual(self.tx(s, lambda tx: attempts.kind_of(tx, aid)), "cut_off")
        self.assertTrue(self.resting(s, s["task"]))
        self.assertEqual(self.tx(s, lambda tx: next_action.current(tx, s["task"]))["purpose"], "resume")   # お詫びとともに
        with self.assertRaises(ValueError):
            self.tx(s, lambda tx: troubles.restore(tx, s["task"], reason=" "))
        with self.assertRaises(Rejected):
            self.tx(s, lambda tx: troubles.restore(tx, s["task"], reason="試験：システムは戻せない"))
        self.assertTrue(self.tx(s, lambda tx: troubles.restore(tx, s["task"], reason="試験：契約者が戻した"), OPERATOR))
        self.assertFalse(self.resting(s, s["task"]))
        self.assertFalse(self.tx(s, lambda tx: troubles.restore(tx, s["task"], reason="試験：もう戻っている"), OPERATOR))
        with self.assertRaises(Rejected):                                     # 休んでいない行の記録は作れない
            self.tx(s, lambda tx: tx.execute(
                "INSERT INTO salescall.task_trouble_restores (tenant_id, client_id, task_id, reason, restored_by, actor_kind)"
                " VALUES (%s, %s, %s, '試験', '試験', 'operator')", (s["tenant"], s["client"], s["task"])), OPERATOR)
        third = self.trouble(s, "ai_malfunction", task=s["task"])              # 戻したあと1回目（お客様が出る前）
        self.assertEqual(self.tx(s, lambda tx: attempts.blame(tx, third)), "ours")
        self.assertFalse(self.resting(s, s["task"]))
        fourth = self.trouble(s, "ai_malfunction", answered=True, task=s["task"])
        self.assertEqual(self.tx(s, lambda tx: attempts.kind_of(tx, fourth)), "cut_off")   # 続いても行の回数に入れない
        self.assertTrue(self.resting(s, s["task"]))                            # 戻した時から2回：また休む
        self.assertEqual(self.notices(s, "task_resting"), (2, 2))              # また休み始めたことも知らせる（休みごとに1回）
        self.assertEqual(self.tx(s, lambda tx: gate.pacing(tx, s["task"])).remaining, remaining)
        self.assertEqual(self.tx(s, lambda tx: attempts.failed_run(tx, s["contact"]))["n"], 0)
        self.assertTrue(self.tx(s, lambda tx: troubles.restore(tx, s["task"], reason="試験：管理者が戻した"), ADMIN))

    def test_a_ringing_call_without_our_trouble_breaks_the_run_and_crashes_go_one_way(self):
        """「続けて」が途切れるのは、こちらのせいでなく、お客様のところで鳴った電話だけ。通話を進める部品が落ちた電話（runner_error）も
        同じ入口で伝え（直す担当 CL-1）、見守り役の誤りとして重ねない。"""
        class NoAnswer(Runner):   # お客様のところで鳴った（出なかった）：こちらのせいでない
            def run(self, access, actor, claim, *, tenant_id, client_id, call=None):
                call.beat()
                access.in_client(actor, tenant_id, client_id, lambda tx: [attempts.move(tx, claim.attempt_id, st)
                                                                           for st in ("ringing", "no_answer")])
        self.override("gate.same_contact_min_interval", 1)                   # 鳴った電話のあとの間（試験では1秒）
        s = self.ready()
        self.trouble(s, "runner_error", task=s["task"])
        self.w.talked(s)                                                       # 復旧した（0141）
        got = line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, FakeDriver(), NoAnswer())
        self.assertIsNotNone(got.called)
        jobs.settle_ready(self.ctx(s))
        time.sleep(1.1)
        self.tx(s, lambda tx: next_action.schedule_now(tx, s["task"], cause="試験"))
        self.trouble(s, "runner_error", task=s["task"])
        self.assertFalse(self.resting(s, s["task"]))                           # 途切れた：続けて1回
        self.assertEqual(self.faults(s), ["call_runner_error"] * 2)
        self.assertEqual(self.asked_supervisor(s), [])
        self.assertTrue(self.requests("CL-1", "call_runner_error"))

    def test_a_poor_audio_call_between_our_troubles_breaks_the_run_of_the_row(self):
        """行の休みの「続けて」が途切れるのは、こちらの不具合でなく、お客様のところで鳴った電話（salescall.task_trouble_run の1か所）：声が
        聞き取れずに終えた電話（こちらの不具合に数えない＝途中で切れた電話と同じ。0141）も、続きを途切れさせる。"""
        s = self.ready()
        self.trouble(s, "ai_malfunction", answered=True, task=s["task"])
        self.trouble(s, "poor_audio", answered=True, task=s["task"])
        self.assertFalse(self.resting(s, s["task"]))
        self.trouble(s, "ai_malfunction", answered=True, task=s["task"])
        self.assertFalse(self.resting(s, s["task"]))                           # 途切れた：続けて1回

    def test_the_running_program_version_is_noted_once_per_change(self):
        """動いているプログラムの版（supervisor/updates.program_version・note）：まだ書いていない版のときだけ書く（同じ版で立ち上げ
        直しても、前の版に戻しても、入れ替えのあいだ古い版と新しい版が少し重なっても、替わったことにしない＝直していないのに休んだ行を
        戻さない）。中央の見守り役は、データベースと版が合っているときに書く。"""
        a, b = f"試験の版-{uuid.uuid4().hex}", f"試験の版-{uuid.uuid4().hex}"
        note = lambda v: self.w.a.in_system(SYSTEM, lambda tx: updates.note(tx, v))   # noqa: E731
        since = lambda: self.w.a.in_system(SYSTEM, lambda tx: tx.one("SELECT salescall.program_since() AS t"))["t"]   # noqa: E731
        self.assertEqual([note(a), note(a), note(b)], [True, False, True])
        after_b = since()
        self.assertEqual([note(b), note(a)], [False, False])                   # 前の版に戻しても、替わったことにしない
        self.assertEqual(since(), after_b)
        self.assertNotEqual(updates.program_version(), updates.program_version("tests.test_troubles:Troubled"))
        self.assertEqual(updates.program_version("tests.test_troubles:Troubled"),
                         updates.program_version("tests.test_supervisor:make_runner"))   # 同じ部品（tests）の版
        s = self.ready()
        sup = self.supervisor_for(s)
        # 動いているプログラムの版には、設定で読み込むほかの担当の部品（通話を進める部品など）も入る（直したのがほかの担当の部品でも、
        # 休んだ行・止めた契約者が戻る）
        self.assertEqual(sup.program, updates.program_version(sup.cfg.call_runner, sup.cfg.verifier, sup.cfg.prober))
        self.assertNotEqual(sup.program, updates.program_version())
        sup.check_version()
        self.assertTrue(self.w.a.in_system(SYSTEM, lambda tx: tx.one(   # 書いた（ほかの試験が先に書いていれば、その記録のまま）
            "SELECT EXISTS (SELECT 1 FROM salescall.program_versions WHERE version = %s) AS e", (sup.program,)))["e"])
        self.assertFalse(self.w.a.in_system(SYSTEM, lambda tx: updates.note(tx, sup.program)))   # 同じ版は2回書かない
        with self.assertRaises(Rejected):                                     # 契約者の取引からは書けない
            self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: updates.note(tx, f"試験の版-{uuid.uuid4().hex}"))

    def test_the_program_version_covers_every_file_of_the_parts_but_not_their_caches(self):
        """版の指紋（supervisor/updates.program_version の1か所）は、部品の中のファイルのすべて（ソース・部品の文・設定のファイル）で
        決まる：文や設定のファイルだけを直しても版が替わる（⛔ 見直し20a：前は .py だけ）。動かしたときにできる写し（__pycache__・.pyc）・
        隠しファイル・編集の控えは入れない（動かしただけで版が替わらない）。"""
        import importlib
        import sys
        import tempfile
        from pathlib import Path
        root = Path(tempfile.mkdtemp(prefix="salescall-part-"))
        name = f"試験の部品_{uuid.uuid4().hex[:8]}"
        pkg = root / name
        (pkg / "__pycache__").mkdir(parents=True)
        (pkg / "__init__.py").write_text("def make(line_id, driver):\n    return None\n", encoding="utf-8")
        (pkg / "phrases.txt").write_text("恐れ入ります。\n", encoding="utf-8")
        (pkg / "settings.json").write_text('{"wait": 2}\n', encoding="utf-8")
        sys.path.insert(0, str(root))
        self.addCleanup(sys.path.remove, str(root))
        importlib.invalidate_caches()
        spec = f"{name}:make"
        before = updates.program_version(spec)
        (pkg / "__pycache__" / "x.cpython-313.pyc").write_bytes(b"\x00\x01")
        (pkg / ".editor").write_text("x", encoding="utf-8")
        (pkg / "phrases.txt~").write_text("控え", encoding="utf-8")
        self.assertEqual(updates.program_version(spec), before)                # 写し・隠し・控え：替わらない
        (pkg / "phrases.txt").write_text("恐れ入ります。改めて\n", encoding="utf-8")
        after_text = updates.program_version(spec)
        self.assertNotEqual(after_text, before)                                # 部品の文だけを直した：替わる
        (pkg / "settings.json").write_text('{"wait": 3}\n', encoding="utf-8")
        self.assertNotEqual(updates.program_version(spec), after_text)         # 設定のファイルだけを直した：替わる

    def test_rows_count_again_after_a_call_that_talked_without_our_trouble(self):
        """違う3行で同じ不具合なら止める数え方（consecutive_errors の program_fixed の1か所）：同じ契約者で、その不具合なしにお客様と
        話し終えた電話があれば数え直す（続けて3行のときだけ止める。2026-10-25 夜 管理者の決定 案A。⛔ 0139：あいだに話し終えた電話が
        あっても、ばらばらの3行で止めた）。"""
        s = self.ready()

        def one_row(task=None):
            task = task or self.row(s)["task"]
            self.trouble(s, "cannot_proceed", task=task, again=False)
            self.finish(s, task)
        one_row(s["task"])
        one_row()
        self.w.talked(s)                                                       # こちらの不具合なしに話し終えた
        one_row()
        one_row()
        self.assertEqual(self.tenant_stops(s), [])                             # 話し終えた電話のあと2行：まだ止めない
        one_row()
        self.assertEqual(self.tenant_stops(s), ["program_trouble"])            # 続けて3行：止める

    def test_poor_audio_is_like_a_cut_call_not_our_trouble_and_on_different_customers_it_is_the_line(self):
        """「声が聞き取れない」（poor_audio）で終えた電話（2026-10-25 夜 管理者の決定 案A）：お詫びして終えるのは今のまま。こちらの
        プログラムの不具合には数えない（行の休み・全体の停止・修正依頼・記録のどれにもしない＝データベースの salescall.trouble_ours の
        1か所。core/errors.TROUBLES にも無い）。お客様と話している途中で切れた電話と同じ（続けて2回まではお詫びしてすぐかけ直し、超えたら
        ふつうのかけ直しの決まりで回数に数える）。同じ回線で、違うお客様の「声が聞き取れない」が、回線が元気だった証拠（鳴ってふつうに
        終えた電話・鳴ったテスト）を挟まずに続いたら、回線の異常（回線の異常の数え方の1か所 line_faults_unrecorded）。"""
        s = self.ready()
        kinds = []
        for _ in range(3):
            aid = self.trouble(s, "poor_audio", answered=True, task=s["task"], again=True)
            kinds.append(self.tx(s, lambda tx, aid=aid: (attempts.kind_of(tx, aid), attempts.is_ours(tx, aid))))
            self.assertEqual(self.faults(s), [])                               # 同じお客様だけ：回線の異常ではない
        self.assertEqual(kinds, [("cut_off", False), ("cut_off", False), ("reached", False)])
        self.assertFalse(self.resting(s, s["task"]))                           # 行を休ませない
        self.assertEqual(self.tx(s, lambda tx: tx.one("SELECT salescall.awaiting_recovery(%s) AS w", (s["task"],))["w"]), False)
        self.assertEqual((self.notices(s, "error_notice"), self.requests("CL-2", "call_poor_audio"),
                          self.requests("CL-1", "call_poor_audio")), ((0, 0), [], []))
        self.assertNotIn("poor_audio", errors.TROUBLES)
        # 違うお客様で続いた：回線の異常（2本とも）。あいだに回線が元気だった証拠（話し終えた電話）があれば数え直す
        t = self.ready()
        self.trouble(t, "poor_audio", answered=True, task=t["task"])
        self.finish(t, t["task"])
        self.assertEqual(self.faults(t), [])                                   # 1人だけ：まだ回線の異常ではない
        y = self.row(t)["task"]
        self.trouble(t, "poor_audio", answered=True, task=y)
        self.finish(t, y)
        self.assertEqual(self.faults(t), ["line", "line"])
        z = self.row(t)["task"]
        got = line.one_round(self.w.a, SYSTEM, t["tenant"], t["line"], HOLDER, FakeDriver(), Runner())   # 話し終えた（回線は元気）
        self.assertEqual(self.tx(t, lambda tx: tx.one("SELECT task_id FROM salescall.attempts WHERE attempt_id = %s",
                                                      (got.called,)))["task_id"], z)
        jobs.settle_ready(self.ctx(t))
        self.trouble(t, "poor_audio", answered=True, task=self.row(t)["task"])
        self.assertEqual(self.faults(t), ["line", "line"])                     # 元気だった証拠のあとの1本だけ：数えない
        messages = self.w.a.in_tenant(SYSTEM, t["tenant"], lambda tx: [r["message"] for r in tx.all(
            "SELECT message FROM salescall.error_events WHERE line_id = %s AND source = 'line'", (t["line"],))])
        self.assertEqual(messages, [line.POOR_AUDIO_LINE] * 2)                 # 記録の理由の文は理由ごと（「鳴りませんでした」にしない）
        # 違うお客様の「声が聞き取れない」のあいだに、回線が元気だった証拠（話し終えた電話）がはさまる：区切られ、どちらも数えない
        u = self.ready()
        self.trouble(u, "poor_audio", answered=True, task=u["task"])
        self.finish(u, u["task"])
        z = self.row(u)["task"]
        line.one_round(self.w.a, SYSTEM, u["tenant"], u["line"], HOLDER, FakeDriver(), Runner())   # 話し終えた（回線は元気）
        jobs.settle_ready(self.ctx(u))
        self.finish(u, z)
        self.trouble(u, "poor_audio", answered=True, task=self.row(u)["task"])
        self.assertEqual(self.faults(u), [])

    def test_the_python_table_of_our_troubles_follows_the_database_rule(self):
        """どの終えた理由がこちらの不具合かは、データベースの salescall.trouble_ours の1か所。Python の処置の表（core/errors.TROUBLES）は、
        そのうちの理由だけに種類を持ち、種類の名前は salescall.trouble_kind と同じ（表がずれれば、この試験が落ちる）。"""
        s = self.ready()
        for reason in errors.ENDS:
            ours, kind = self.tx(s, lambda tx, r=reason: (lambda row: (row["o"], row["k"]))(tx.one(
                "SELECT salescall.trouble_ours(%s) AS o, salescall.trouble_kind(%s) AS k", (r, r))))
            self.assertEqual(ours, reason in errors.TROUBLES, reason)
            if ours:
                self.assertEqual(errors.TROUBLES[reason], kind)
                self.assertIn(kind, errors.ACTIONS)

    def test_a_call_ended_by_our_trouble_is_called_back_once_recovered(self):
        """こちらの不具合で途中で終えた電話は「復旧次第」かけ直す（2026-10-17 管理者：「復旧次第、お詫びとともにすぐかけ直さないと
        いけないでしょ！」。判断はデータベースの salescall.awaiting_recovery→recovered_after の1か所）。復旧＝その電話が終わったあと、
        (a) 同じ契約者で、こちらの不具合なしにお客様と話し終えた電話をかけ始めた、(b) その種類の不具合を直したと記録され、そのあと版が
        替わった（salescall.trouble_fixed_at＝休んだ行を戻す・止めた停止を解くのと同じ1か所。0143）。それまでは門と候補を出す処理が待たせる
        （お客様が続けて2回お詫びの電話を受けない）。⛔ 0139：1秒ほどでかけ直した。⛔ 0141：どの版の入れ替えでも復旧にした（直した
        記録が無くても＝見直し21b の 2）。版が替わっただけ・出なかった電話（話し終えていない）・その電話が終わる前にかけ始めた電話は
        復旧ではない。復旧を待つかを見るのは、その行の、発信まで進んだいちばん新しい電話（かけずに取り消した予約は見ない）。"""
        s = self.ready()
        self.trouble(s, "ai_malfunction", answered=True, task=s["task"])
        self.assertEqual(self.tx(s, lambda tx: next_action.current(tx, s["task"]))["purpose"], "resume")   # お詫びとともに
        self.assertEqual(self.claim(s).reason, "awaiting_recovery")
        self.assertNotIn(s["task"], [r["task_id"] for r in self.tx(s, lambda tx: scheduler.due_tasks(tx, 10))])
        t = self.ready()                                                       # ほかの契約者が話し終えても、復旧ではない
        self.w.talked(t)
        self.assertEqual(self.claim(s).reason, "awaiting_recovery")
        self.w.talked(s)                                                       # (a) 同じ契約者で話し終えた
        self.assertIsInstance(self.claim(s), gate.Claim)
        u = self.ready()
        self.trouble(u, "cannot_proceed", task=u["task"])
        self.tx(u, lambda tx: next_action.schedule_now(tx, u["task"], cause="試験"))
        self.assertEqual(self.claim(u).reason, "awaiting_recovery")
        self.new_version()                                                     # 版が替わっただけ（直した記録が無い）：まだ
        self.assertEqual(self.claim(u).reason, "awaiting_recovery")
        other = self.other_row(u)                                              # 出なかった電話（話し終えていない）：まだ
        aid = self.w.insert_claim(other)
        for state in ("dialing", "ringing", "no_answer"):
            self.w.move(other, aid, state)
        self.assertEqual(self.claim(u).reason, "awaiting_recovery")
        held = self.w.insert_claim(u)                                          # かけずに取り消した予約：見ない
        self.w.move(u, held, "released")
        self.assertEqual(self.claim(u).reason, "awaiting_recovery")
        self.report_fixed("CL-1", "call_cannot_proceed")                       # (b) 直したと記録され、
        self.assertEqual(self.claim(u).reason, "awaiting_recovery")
        self.new_version()                                                     #     そのあと版が替わった
        self.assertIsInstance(self.claim(u), gate.Claim)
        # その電話が終わる前に（ほかの回線で）かけ始めた電話が話し終えても、復旧ではない（その電話のあとに話せた証拠ではない）
        v = self.ready()
        second = self.w.line(v["tenant"])
        troubled = self.w.insert_claim(v)
        for state in ("dialing", "ringing"):
            self.w.move(v, troubled, state)
        good = self.other_row(v)
        early = self.w.insert_claim(good, line=second)
        for state in ("dialing", "ringing", "answered", "in_conversation"):
            self.w.move(good, early, state)
        self.assertTrue(self.tx(v, lambda tx: attempts.end_for_trouble(tx, troubled, "cannot_proceed", "試験：根拠の文")))
        for state in ("ending", "ended"):
            self.w.move(good, early, state)
        self.assertTrue(self.tx(v, lambda tx: tx.one("SELECT salescall.awaiting_recovery(%s) AS w", (v["task"],))["w"]))
        self.assertEqual(self.tx(v, lambda tx: tx.one("SELECT trouble_why FROM salescall.attempts WHERE attempt_id = %s",
                                                      (troubled,))["trouble_why"]), "試験：根拠の文")

    def other_row(self, s):
        """同じ依頼者の、別の番号の行（試行を直に足す試験用。phone も持つ）。"""
        r = self.row(s)
        r["phone"] = self.tx(s, lambda tx: tx.one("SELECT phone_e164 FROM salescall.contacts WHERE contact_id = %s",
                                                  (r["contact"],))["phone_e164"])
        return r

    def supervisor_for(self, s):
        from salescall.supervisor.main import Supervisor
        from tests.test_supervisor import FakeLinks
        return Supervisor(self.cfg(s), FakeLinks())
