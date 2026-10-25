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
from tests.support import ADMIN, CLEAN, OPERATOR, SYSTEM, fictional_jp_fixed
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
        got = self.claim(s)
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
        self.assertEqual(self.notices(s, "task_resting"), (1, 1))
        self.assertEqual(len(self.requests("CL-1", "call_ai_malfunction")), 1)
        self.assertEqual(self.asked_supervisor(s), [])
        self.assertEqual(self.tenant_stops(s), [])                             # 1つの行だけ：止めない

    def test_the_same_reason_on_three_rows_stops_the_contractor_and_tells_once(self):
        """すべての行にかかわる問題：違う行で同じ理由が3行（設定 stop.trouble.stop_after_rows）になったら、その契約者の新しい電話を
        止める（止め方は watch.after_error → decide_stop の1か所。原因 program_trouble）。同じ行の2回目は数えない。止めた知らせは
        契約者と管理者へ1回。修正依頼は同じ不具合で1つ（重ねない）。直したプログラムの版に替われば解け、休んだ行も戻る。"""
        s = self.ready()
        self.assertEqual(self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: settings.resolve(
            tx, "stop.trouble.stop_after_rows", tenant_id=s["tenant"], client_id=None, business_id=None)), 3)
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
        # 直したプログラムの版に替わった：休んだ行は戻り、止めた停止は決まった間隔の仕事が解く
        jobs.fixed_stops(self.ctx(s))
        self.assertEqual(self.tenant_stops(s), ["program_trouble"])            # 版が替わるまでは解かない
        self.assertTrue(self.w.a.in_system(SYSTEM, lambda tx: updates.note(tx, f"試験の版-{uuid.uuid4().hex}")))
        self.assertFalse(self.resting(s, s["task"]))
        n = self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: tx.one(
            "SELECT salescall.consecutive_errors(ARRAY['call_cannot_proceed'], 'program_trouble', 'program_fixed') AS n"))["n"]
        self.assertEqual(n, 0)                                                 # 版が替わった時から数え直す（停止を解く前から）
        jobs.fixed_stops(self.ctx(s))
        self.assertEqual(self.tenant_stops(s), [])
        self.assertEqual(self.notices(s, "resumed"), (1, 0))

    def test_different_reasons_on_three_rows_do_not_stop(self):
        """違う理由なら、行が3つでも止めない（理由ごとに数える＝処置の表の per_call）。修正依頼は、理由ごとの担当へ（会話の部品は CL-1、
        声の受け渡し＝電話接続は CL-2）。"""
        s = self.ready()
        for reason in ("ai_malfunction", "poor_audio", "cannot_proceed"):
            self.trouble(s, reason, task=self.row(s)["task"] if reason != "ai_malfunction" else s["task"], again=False)
        self.assertEqual(self.tenant_stops(s), [])
        self.assertEqual(len(self.requests("CL-2", "call_poor_audio")), 1)
        self.assertEqual({errors.ACTIONS[k].fixer for k in errors.TROUBLES.values()}, {"CL-1", "CL-2"})
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
            aid = self.trouble(s, "poor_audio", answered=True, task=s["task"])
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
        third = self.trouble(s, "poor_audio", task=s["task"])                  # 戻したあと1回目（お客様が出る前）
        self.assertEqual(self.tx(s, lambda tx: attempts.blame(tx, third)), "ours")
        self.assertFalse(self.resting(s, s["task"]))
        fourth = self.trouble(s, "poor_audio", answered=True, task=s["task"])
        self.assertEqual(self.tx(s, lambda tx: attempts.kind_of(tx, fourth)), "cut_off")   # 続いても行の回数に入れない
        self.assertTrue(self.resting(s, s["task"]))                            # 戻した時から2回：また休む
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
        sup.check_version()
        self.assertTrue(self.w.a.in_system(SYSTEM, lambda tx: tx.one(   # 書いた（ほかの試験が先に書いていれば、その記録のまま）
            "SELECT EXISTS (SELECT 1 FROM salescall.program_versions WHERE version = %s) AS e", (sup.program,)))["e"])
        self.assertFalse(self.w.a.in_system(SYSTEM, lambda tx: updates.note(tx, sup.program)))   # 同じ版は2回書かない
        with self.assertRaises(Rejected):                                     # 契約者の取引からは書けない
            self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: updates.note(tx, f"試験の版-{uuid.uuid4().hex}"))

    def supervisor_for(self, s):
        from salescall.supervisor.main import Supervisor
        from tests.test_supervisor import FakeLinks
        return Supervisor(self.cfg(s), FakeLinks())
