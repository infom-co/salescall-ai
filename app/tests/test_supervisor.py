"""見守り役の試験（DESIGN.md 5-4。supervisor/・core/leases.py・core/scheduler.py・表の作り 0063）。"""

from __future__ import annotations

import tempfile
import threading
import time
import unittest
import uuid
from datetime import timedelta
from pathlib import Path

from salescall.core import (attempts, backup, business_rules, call_report, fix_requests, gate, leases, next_action, quality_stage,
                            registry, scheduler, stops)
from salescall.core.phone import normalize
from salescall.core.access import Rejected
from salescall.supervisor import config, jobs, line, updates
from salescall.supervisor import main as main_mod
from salescall.supervisor.main import Supervisor
from tests.support import ADMIN, CLEAN, OPERATOR, SYSTEM, fictional_jp_fixed, rules_raw
from tests.test_gate import HOLDER, Base, FakeDriver
from tests.test_settle import rep


class Runner:
    """通話を最後まで進める部品の代わり（会話の担当の部品の形）：出て、話して、Q1 が取れて、ふつうに終わる。"""

    def __init__(self, fail: BaseException | None = None) -> None:
        self.fail = fail

    def run(self, access, actor, claim, *, tenant_id, client_id):
        if self.fail:
            access.in_client(actor, tenant_id, client_id, lambda tx: attempts.move(tx, claim.attempt_id, "ringing"))
            raise self.fail
        def work(tx):
            for st in ("ringing", "answered", "in_conversation", "ending", "ended"):
                attempts.move(tx, claim.attempt_id, st)
            tx.execute("INSERT INTO salescall.item_values (tenant_id, client_id, task_id, version_id, question_id, state)"
                       " VALUES (%s, %s, %s, %s, 'Q1', 'confirmed')", (tenant_id, client_id, claim.task_id, claim.version_id))
            call_report.save(tx, claim.attempt_id, rep())
            attempts.record_verification(tx, claim.attempt_id, summary=CLEAN)
        access.in_client(actor, tenant_id, client_id, work)


def make_driver(line_id):
    return FakeDriver()


def make_runner(line_id):
    return Runner()


class SupervisorBase(Base):
    def cfg(self, s, **more):
        values = {"SALESCALL_DSN": self.w.a._conninfo, "SALESCALL_TENANT_ID": str(s["tenant"]),
                  "SALESCALL_LINES": str(s["line"]), "SALESCALL_LINE_DRIVER": "tests.test_supervisor:make_driver",
                  "SALESCALL_CALL_RUNNER": "tests.test_supervisor:make_runner", **more}
        return config.load(values)

    def take(self, s, holder=HOLDER, line_id=None):
        return self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: leases.take_line(tx, line_id or s["line"], holder))

    def state(self, s, attempt_id):
        return self.tx(s, lambda tx: tx.one("SELECT state::text AS s FROM salescall.attempts WHERE attempt_id = %s",
                                            (attempt_id,)))["s"]

    def asked_supervisor(self, s):
        return [r for r in self.w.a.in_system(SYSTEM, lambda tx: fix_requests.open_requests(tx, "CL-2"))
                if r["kind"] == "supervisor" and r["refs"].get("tenant_id") == str(s["tenant"])]


class Leases(SupervisorBase):
    def test_one_supervisor_holds_a_line_and_another_takes_it_only_after_it_expires(self):
        s = self.ready()
        self.assertIsNotNone(self.take(s))                                     # 同じ見守り役は延ばせる
        self.assertIsNone(self.take(s, holder="ほかのPC#x"))                   # 生きて借りている回線は借りられない
        self.override("line.lease", 1)
        self.assertIsNotNone(self.take(s))
        time.sleep(1.2)
        self.assertIsNotNone(self.take(s, holder="ほかのPC#x"))                # 期限が切れたら借りられる
        other = self.w.tenant()
        self.assertIsNone(self.w.a.in_tenant(SYSTEM, other, lambda tx: leases.take_line(tx, s["line"], "x")))   # 契約者の外

    def test_jobs_are_taken_by_one_supervisor_and_only_by_the_system(self):
        key = f"試験の仕事:{uuid.uuid4()}"
        take = lambda who: self.w.a.in_system(SYSTEM, lambda tx: leases.take_job(tx, key, who, 60))   # noqa: E731
        self.assertEqual([take("A"), take("B"), take("A")], [True, False, True])
        self.w.a.in_system(SYSTEM, lambda tx: leases.return_job(tx, key, "A"))
        self.assertTrue(take("B"))
        with self.assertRaises(Rejected):
            self.w.a.in_tenant(OPERATOR, self.w.tenant(), lambda tx: leases.take_job(tx, key, "C", 60))


class Recovery(SupervisorBase):
    def test_a_crashed_supervisor_cleans_up_its_own_lines_when_it_starts_again(self):
        """起動したら、前に落ちた自分の回線の、進行中の試行を終わらせる（予約のまま＝取り消し・発信してから＝結果不明）。"""
        s = self.ready()
        c = self.claim(s)
        self.assertTrue(line.dial_exit.dial(self.w.a, SYSTEM, s["tenant"], s["client"], c.attempt_id, FakeDriver()))
        got = self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: leases.recover(tx, HOLDER))
        self.assertEqual((got[0]["line_id"], got[0]["ended"]), (s["line"], 1))
        self.assertEqual(self.state(s, c.attempt_id), "technical_error")

    def test_only_expired_lines_of_other_pcs_are_cleaned_up(self):
        s = self.ready()
        c = self.claim(s)
        self.assertEqual(self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: leases.recover(tx)), [])   # 生きている回線は触らない
        self.assertEqual(self.state(s, c.attempt_id), "claimed")
        self.lease(s, seconds=-1)                                              # その PC が落ちて、借用の期限が切れた
        got = self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: leases.recover(tx))
        self.assertEqual(got[0]["released"], 1)
        self.assertEqual(self.state(s, c.attempt_id), "released")


    def test_the_periodic_cleanup_leaves_its_own_lines_and_the_admin_pc_cleans_stale_ones(self):
        """決まった間隔の後始末は、自分が借りている回線を、期限が切れていても終わらせない（データベースに届かないあいだに期限が
        切れても、まだ話している自分の通話を結果不明にしない）。契約者の PC が落ちたまま戻らない回線は、管理者の PC が後始末する
        （ふつうの通話の長さより十分長く期限が切れたままのものだけ）。"""
        s = self.ready()
        c = self.claim(s)
        self.lease(s, seconds=-1)                                              # 自分の借用の期限が切れた（データベースに届かなかった）
        jobs.recover_expired(jobs.Context(self.w.a, self.cfg(s), HOLDER))
        self.assertEqual(self.state(s, c.attempt_id), "claimed")
        admin = jobs.Context(self.w.a, self.cfg(s, SALESCALL_SYSTEM_JOBS="1"), "管理者のPC#y")
        jobs.recover_stale(admin)
        self.assertEqual(self.state(s, c.attempt_id), "claimed")               # 切れてすぐは、その契約者の PC に任せる
        self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: tx.execute(
            "UPDATE salescall.lines SET lease_expires_at = now() - %s - interval '1 minute' WHERE line_id = %s",
            (jobs.STALE_AFTER, s["line"])))
        jobs.recover_stale(admin)
        self.assertEqual(self.state(s, c.attempt_id), "released")
        with self.assertRaises(Rejected):                                      # 契約者の取引からは、ほかの契約者を探せない
            self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: leases.tenants_with_stale_lines(tx, jobs.STALE_AFTER))


class Candidates(SupervisorBase):
    def test_only_rows_the_gate_could_take_are_offered(self):
        s = self.ready()
        due = lambda: [r["task_id"] for r in self.tx(s, lambda tx: scheduler.due_tasks(tx, 10))]   # noqa: E731
        lines = lambda: self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: scheduler.clients_for_line(tx, s["line"]))   # noqa: E731
        self.assertEqual((due(), lines()), ([s["task"]], [s["client"]]))
        c = self.claim(s)
        self.assertEqual(due(), [])                                            # 進行中の発信がある
        self.tx(s, lambda tx: attempts.release(tx, [c.attempt_id]))
        self.tx(s, lambda tx: next_action.hold(tx, s["task"], cause="試験"))
        self.assertEqual(due(), [])                                            # 判定し直しを待っている
        self.tx(s, lambda tx: next_action.schedule_now(tx, s["task"], cause="試験"))
        e = self.tx(s, lambda tx: stops.stop(tx, cause="budget", reason="試験", tenant_id=s["tenant"], scope="client",
                                             client_id=s["client"]))
        self.assertEqual((lines(), due()), ([s["client"]], []))                 # 依頼者が止まっている（依頼者の取引で見る）
        self.tx(s, lambda tx: stops.resume(tx, e["event_id"], reason="試験"))
        self.tx(s, lambda tx: registry.set_client_active(tx, False, reason="試験"), OPERATOR)
        self.assertEqual(lines(), [])                                          # 無効の依頼者


class Rounds(SupervisorBase):
    def ctx(self, s):
        cfg = self.cfg(s)
        return jobs.Context(self.w.a, cfg, HOLDER)

    def test_a_round_calls_one_row_and_the_settle_job_settles_it(self):
        s = self.ready()
        got = line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, FakeDriver(), Runner())
        self.assertIsNotNone(got.called)
        self.assertEqual(self.state(s, got.called), "ended")
        jobs.settle_ready(self.ctx(s))
        r = self.tx(s, lambda tx: tx.one("SELECT business_state FROM salescall.task_results WHERE attempt_id = %s",
                                         (got.called,)))
        self.assertEqual(r["business_state"], "completed")

    def test_a_line_reason_rests_the_line_without_rewriting_the_row(self):
        s = self.ready()
        self.lease(s, holder="ほかのPC#x")                                    # この見守り役は回線を借りていない
        before = self.tx(s, lambda tx: next_action.current(tx, s["task"]))["seq"]
        got = line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, FakeDriver(), Runner())
        self.assertEqual((got.called, got.pause), (None, "line_not_leased"))
        self.assertEqual(self.tx(s, lambda tx: next_action.current(tx, s["task"]))["seq"], before)

    def test_a_line_that_cannot_place_calls_rests_and_counts_as_a_line_fault(self):
        """回線の部品がかけられなかった・スマホがまだ通話中なら、休まず次の行へ進まない（行の回数と回線の上限を使い切らない）。
        かけられなかったことは回線の異常として記録する（続けば、処置の表のとおり回線を止める）。"""
        s = self.ready()
        got = line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER,
                             FakeDriver(line.dial_exit.CallNotPlaced("アプリが受け付けなかった")), Runner())
        self.assertEqual((got.called, got.pause), (None, "not_placed"))
        faults = self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: tx.all(
            "SELECT kind FROM salescall.error_events WHERE line_id = %s", (s["line"],)))
        self.assertEqual([r["kind"] for r in faults], ["line"])
        t = self.ready()
        got = line.one_round(self.w.a, SYSTEM, t["tenant"], t["line"], HOLDER, FakeDriver(busy=True), Runner())
        self.assertEqual((got.called, got.pause), (None, "line_in_call"))

    def test_a_failure_in_the_call_ends_only_that_call_and_asks_cl2(self):
        s = self.ready()
        got = line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, FakeDriver(), Runner(RuntimeError("試験")))
        self.assertEqual(self.state(s, got.called), "technical_error")
        self.assertTrue(self.asked_supervisor(s))

    def test_promised_calls_go_first_across_clients_and_keep_their_share_of_the_line(self):
        """お客様と約束したかけ直しは、すべての依頼者について先に（共有の回線で、先の依頼者のふつうの電話より、あとの依頼者の
        約束が先）。回線の1日の上限は、今日の約束の分を取り置く（ふつうの電話は使わない）。2026-10-19 ユーザー：「その日の約束の
        電話には目印が付き、それを処理してから、通常の順序の電話を始めた方がいい」「（取り置き）OK」。"""
        s = self.ready()                                                       # 先に作った依頼者：ふつうの電話（いまかけてよい）
        t = self.ready(tenant=s["tenant"])                                     # あとの依頼者：約束したかけ直し
        now = self.tx(t, lambda tx: tx.one("SELECT now() AS t")["t"])
        self.tx(t, lambda tx: next_action.schedule_call(tx, t["task"], now, contact_id=t["contact"], cause="試験", promised=True))
        self.put_client(s, "line.max_dials_per_day", 1)
        self.assertEqual(self.reason(s), "line_daily_limit")                   # ふつうの電話：約束の1本分を取り置く
        got = line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, FakeDriver(), Runner())
        self.assertEqual(self.tx(t, lambda tx: tx.one("SELECT task_id FROM salescall.attempts WHERE attempt_id = %s",
                                                      (got.called,)))["task_id"], t["task"])   # あとの依頼者の約束が先

    def test_an_exact_time_promise_keeps_the_line_free_shortly_before(self):
        """お客様が時刻を指定した約束（時刻ちょうど・幅の無い指定）の数分前（設定。初め5分）からは、その約束をかけられる回線で
        ふつうの電話を始めない（2026-10-19 ユーザー）。約束の電話そのものは止めない。時刻が遠ければ、ふつうにかける。"""
        s = self.ready()
        t = self.ready(tenant=s["tenant"])
        now = self.tx(t, lambda tx: tx.one("SELECT now() AS t")["t"])
        promise = lambda at: self.tx(t, lambda tx: next_action.schedule_call(   # noqa: E731
            tx, t["task"], at, contact_id=t["contact"], cause="試験", promised=True, callee_at=at))
        promise(now + timedelta(minutes=3))
        self.assertEqual(self.reason(s), "line_promise_soon")                 # ふつうの電話：始めない
        promise(now + timedelta(minutes=30))
        self.assertIsInstance(self.claim(s), gate.Claim)                       # 遠い：かける

    def test_a_capped_business_does_not_keep_the_other_businesses_of_the_client_out(self):
        """上限に達した業務の行が候補の先頭を埋めても、その業務を除いて読み直し、同じ依頼者のほかの業務の行をかける。"""
        s = self.ready()
        b, v = self.w.business_with_version(s["tenant"], s["client"])
        phone = normalize(fictional_jp_fixed()).e164
        k = self.w.contact(s["tenant"], s["client"], phone)
        task_b = self.w.task(s["tenant"], s["client"], self.w.run(s["tenant"], s["client"], b, v), k)
        self.tx(s, lambda tx: quality_stage.open_stage(tx, v, 1, None, evidence="試験"), OPERATOR)
        self.tx(s, lambda tx: business_rules.save(tx, v, rules_raw()), OPERATOR)
        self.tx(s, lambda tx: tx.execute(
            "INSERT INTO salescall.setting_values (key, scope, tenant_id, client_id, business_id, value, status, rationale,"
            " review_when, set_by) VALUES ('calling.business_daily_dials', 'business', %s, %s, %s, '1', 'decided', '試験',"
            " '試験', '試験')", (s["tenant"], s["client"], s["business"])), OPERATOR)
        self.tx(s, lambda tx: next_action.schedule_now(tx, task_b, cause="試験"))       # 業務B の行は、業務A の行より後
        phone_a = normalize(fictional_jp_fixed()).e164
        k_a = self.w.contact(s["tenant"], s["client"], phone_a)
        task_a2 = self.w.task(s["tenant"], s["client"], s["run"], k_a)
        self.past_attempt(dict(s, task=task_a2, contact=k_a, phone=phone_a), ["dialing", "no_answer"])   # 業務A は今日1本＝上限
        self.tx(s, lambda tx: next_action.hold(tx, task_a2, cause="試験：この行は候補に出さない"))
        self.tx(s, lambda tx: next_action.schedule_now(tx, s["task"], cause="試験：業務A の行を先頭に"))
        self.tx(s, lambda tx: next_action.schedule_now(tx, task_b, cause="試験"))
        first = self.tx(s, lambda tx: scheduler.due_tasks(tx, 1))
        self.assertEqual([r["task_id"] for r in first], [s["task"]])                    # 1行ずつなら、先頭は業務A
        old, line.CANDIDATES_PER_CLIENT = line.CANDIDATES_PER_CLIENT, 1
        try:
            got = line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, FakeDriver(), Runner())
        finally:
            line.CANDIDATES_PER_CLIENT = old
        self.assertEqual(self.tx(s, lambda tx: tx.one("SELECT task_id FROM salescall.attempts WHERE attempt_id = %s",
                                                      (got.called,)))["task_id"], task_b)

    def test_a_failing_job_is_recorded_and_returned(self):
        s = self.ready()
        ctx = self.ctx(s)
        bad = jobs.Job(f"試験の壊れた仕事{uuid.uuid4().hex[:6]}", 60, "tenant", 60, lambda c: 1 / 0)
        self.assertTrue(jobs.run_job(ctx, bad))
        self.assertTrue(self.asked_supervisor(s))
        self.assertTrue(self.w.a.in_system(SYSTEM, lambda tx: leases.take_job(tx, bad.key(ctx.cfg), "ほかのPC#x", 60)))


class FakeProcess:
    def __init__(self):
        self.alive, self.exitcode = True, None

    def is_alive(self):
        return self.alive

    def join(self, timeout=None):
        pass


class Main(SupervisorBase):
    def test_a_dead_line_process_is_cleaned_up_and_started_again(self):
        s = self.ready()
        started = []
        def start(cfg, line_id, holder):
            started.append(FakeProcess())
            return started[-1], threading.Event()
        sup = Supervisor(self.cfg(s), start_worker=start, holder=HOLDER)
        sup.start()
        self.assertEqual((len(started), sup.leased), (1, {s["line"]}))
        c = self.claim(s)
        line.dial_exit.dial(self.w.a, SYSTEM, s["tenant"], s["client"], c.attempt_id, FakeDriver())
        started[0].alive, started[0].exitcode = False, 1                        # 回線ごとの処理が落ちた
        sup.watch_workers(0.0)
        self.assertEqual(self.state(s, c.attempt_id), "technical_error")        # その回線の通話を後始末
        self.assertTrue(self.asked_supervisor(s))
        sup.watch_workers(1.0)
        self.assertEqual(len(started), 1)                                       # すぐには立ち上げ直さない
        sup.watch_workers(1000.0)
        self.assertEqual(len(started), 2)                                       # 間をあけて立ち上げ直す
        self.lease(s, holder="ほかのPC#x")                                     # 回線をほかに取られた（期限切れのあと）
        sup.renew(2000.0)
        self.assertEqual(sup.leased, set())
        self.assertNotIn(s["line"], sup.workers)                                # その回線の処理は止める
        sup.shutdown(0)

    def test_the_supervisor_keeps_going_when_the_database_is_unreachable(self):
        """データベースに届かない・回線ごとの処理を立ち上げられない、のどれでも本体は止まらない（次の回にまた試す。続けば間を延ばす）。"""
        s = self.ready()
        class Unreachable:
            def __getattr__(self, name):
                def fail(*a, **k):
                    raise ConnectionError("試験：データベースに届かない")
                return fail
        sup = Supervisor(self.cfg(s), start_worker=lambda *a: (_ for _ in ()).throw(OSError("試験")), holder=HOLDER)
        real, sup.access = sup.access, Unreachable()
        sup.tick(0.0)                                                          # 止まらない
        self.assertEqual(sup._next_renew, main_mod.RENEW_FALLBACK)
        sup.access = real
        sup.tick(100.0)                                                        # 届いた：借りられたが、立ち上げられない
        w = sup.workers[s["line"]]
        self.assertEqual((w.process, w.fails, w.restart_at), (None, 1, 100.0 + main_mod.RESTART_FIRST))
        sup.tick(101.0)                                                        # 間をあけるまでは試さない
        self.assertEqual(sup.workers[s["line"]].fails, 1)
        sup.shutdown(0)

    def test_a_refused_key_reaches_the_login_again_from_every_step(self):
        """PC の鍵が止められた（データベースが断った）：どの段で受けても、本体を止めてログインし直す所（__main__）まで届く。
        ⚠️ 前は各段が「どの誤りでも止めない」ためにふつうの誤りとして受け止め、ログインし直さず、かけないまま回り続けた
        （2026-10-19 の見直し）。後始末（shutdown）は、断られても止まらずに終わる。"""
        from salescall.core.access import LoginRefused
        s = self.ready()
        sup = Supervisor(self.cfg(s), start_worker=lambda *a: (FakeProcess(), threading.Event()), holder=HOLDER)
        sup.start()
        real = sup.access
        class Refusing:
            def __getattr__(self, name):
                def refuse(*a, **k):
                    raise LoginRefused("試験：この PC の鍵は止められています")
                return refuse
        sup.access = Refusing()
        for step in (sup.check_version, lambda: sup.renew(1.0), lambda: sup._launch(s["line"], fails=0, now=1.0),
                     lambda: sup.tick(10_000.0)):
            with self.subTest(step=step), self.assertRaises(LoginRefused):
                step()
        sup.shutdown(0)                                                          # 断られても、後始末は止まらずに終わる
        sup.access = real

    def test_a_program_out_of_step_with_the_database_makes_no_new_calls_and_asks_for_an_update(self):
        """プログラムとデータベースの版がずれていれば、回線ごとの処理を止め（いまの通話は最後まで）、立ち上げない。プログラムが古ければ
        契約者へ「更新」の知らせ（自動か手動かで文が違う）。版が合ったら、またかける。"""
        s = self.ready()
        started = []
        def start(cfg, line_id, holder):
            started.append(FakeProcess())
            return started[-1], threading.Event()
        sup = Supervisor(self.cfg(s), start_worker=start, holder=HOLDER)
        sup.start()
        self.assertEqual(len(started), 1)
        real = updates.PROGRAM
        updates.PROGRAM = ("0001_古いプログラム.sql", "0" * 64)                    # データベースが先に新しくなった
        try:
            sup.check_version()
            sup.renew(100.0)
            self.assertEqual((sup.version_gap, s["line"] in sup.workers, sup.leased), (updates.PROGRAM_OLD, False, {s["line"]}))
            sup.watch_workers(10_000.0)
            self.assertEqual(len(started), 1)                                     # 立ち上げない
            sup._running.clear()
            sup.run_jobs(10_000.0)
            self.assertEqual(sup._running, {})                                    # 決まった間隔の仕事もしない
            c = self.claim(s)                                                     # 止めた処理が残した試行（落ちた）は、後始末する
            sup.renew(300.0)
            self.assertEqual(self.state(s, c.attempt_id), "released")
            notes = self.w.a.in_tenant(OPERATOR, s["tenant"], lambda tx: tx.all(
                "SELECT summary FROM salescall.notification_events WHERE kind = 'program_update'"))
            self.assertEqual(len(notes), 1)
            self.assertIn("プログラムを更新してください", notes[0]["summary"])     # 入れ替えの部品が無い PC：自動とは言わない
            admin = self.w.a.in_system(ADMIN, lambda tx: tx.all(
                "SELECT 1 FROM salescall.notification_events WHERE kind = 'program_update' AND origin_tenant = %s", (s["tenant"],)))
            self.assertEqual(len(admin), 1)                                       # 管理者にも写し
            t = self.ready()                                                      # 入れ替えの部品があり、自動の契約者
            self.assertEqual(self.w.a.in_tenant(SYSTEM, t["tenant"], lambda tx: updates.check(tx, t["tenant"], can_replace=True)),
                             updates.PROGRAM_OLD)
            auto = self.w.a.in_tenant(OPERATOR, t["tenant"], lambda tx: tx.one(
                "SELECT summary FROM salescall.notification_events WHERE kind = 'program_update'"))["summary"]
            self.assertIn("自動で入れ替えます", auto)
        finally:
            updates.PROGRAM = real
        sup.check_version()
        sup.renew(200.0)                                                          # 版が合った：またかける
        self.assertEqual((sup.version_gap, len(started)), (updates.SAME, 2))
        sup.shutdown(0)
        program = ("0080_b.sql", "f" * 64)
        self.assertEqual([updates.gap(x, program) for x in (("0081_c.sql", "1"), ("0079_a.sql", "1"), ("0080_b.sql", "f" * 64),
                                                             ("0080_b.sql", "2"), (None, None), None)],
                         [updates.PROGRAM_OLD, updates.PROGRAM_NEW, updates.SAME, updates.MISMATCH, updates.UNKNOWN,
                          updates.UNKNOWN])
        self.assertEqual(updates.gap(("0080_b.sql", "f" * 64), None), updates.UNKNOWN)   # 表の作りのファイルが無い＝分からない
        live = self.w.a.in_tenant(SYSTEM, s["tenant"], updates.current)
        self.assertEqual(live, updates.SAME)                                     # プログラムとデータベースの指紋は同じ決まり
        # Windows の PC で行の終わりが CRLF で取り出されても、指紋は同じ（⚠️ 前は「途中が違う」と読み、かけなくなった）
        import shutil
        import tempfile
        from pathlib import Path
        from salescall.core import migrate
        crlf = Path(tempfile.mkdtemp(prefix="salescall-test-crlf-"))
        try:
            for f in migrate.migration_files():
                (crlf / f.name).write_bytes(f.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
            self.assertEqual(migrate.fingerprint(crlf), migrate.fingerprint())
        finally:
            shutil.rmtree(crlf)

    def test_the_supervisor_calls_through_a_separate_line_process_and_returns_the_line(self):
        """本物の立ち上げ方（回線ごとに別の処理）で1本かけ、決まった間隔の仕事が締め、止めると回線を返す。"""
        s = self.ready()
        sup = Supervisor(self.cfg(s), holder=HOLDER)
        sup.start()
        done = None
        deadline = time.monotonic() + 60
        while done is None and time.monotonic() < deadline:
            sup.tick()
            time.sleep(0.5)
            done = self.tx(s, lambda tx: tx.one("SELECT business_state FROM salescall.task_results WHERE task_id = %s",
                                                (s["task"],)))
        sup.shutdown(30)
        self.assertEqual(done and done["business_state"], "completed")
        held = self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: tx.one(
            "SELECT lease_holder FROM salescall.lines WHERE line_id = %s", (s["line"],)))["lease_holder"]
        self.assertIsNone(held)                                                 # 止めたら回線を返す

    def test_a_line_still_in_a_call_from_before_is_left_alone(self):
        """本体が落ちて立ち上げ直したとき、前の回線ごとの処理がまだ通話の途中なら、その回線は後始末も立ち上げ直しもしない。"""
        from salescall.supervisor.locks import SingleInstance, line_lock
        s = self.ready()
        c = self.claim(s)
        line.dial_exit.dial(self.w.a, SYSTEM, s["tenant"], s["client"], c.attempt_id, FakeDriver())
        started = []
        sup = Supervisor(self.cfg(s), start_worker=lambda cfg, l, h: started.append(l) or (FakeProcess(), threading.Event()),
                         holder=HOLDER)
        with SingleInstance(line_lock(s["line"])):                             # 前の回線ごとの処理が、まだ動いている
            sup.start()
            self.assertEqual((self.state(s, c.attempt_id), started), ("dialing", []))
        sup.renew(100.0)                                                        # 終わって印が外れたら、後始末して立ち上げる
        self.assertEqual((self.state(s, c.attempt_id), started), ("technical_error", [s["line"]]))
        sup.shutdown(0)

    def test_only_one_supervisor_per_pc(self):
        from salescall.supervisor.locks import AlreadyRunning, SingleInstance as _SingleInstance
        path = Path(tempfile.mkdtemp()) / "lock"
        with _SingleInstance(path):
            with self.assertRaises(AlreadyRunning):
                with _SingleInstance(path):
                    pass

    def test_settings_are_reported_without_values_and_the_key_check_compares_only(self):
        lines = config.status({"SALESCALL_DSN": "postgresql://架空の接続", "GEMINI_KEY_1": "架空のキー"})
        self.assertIn("SALESCALL_DSN：入っています", lines)
        self.assertNotIn("架空の接続", "\n".join(lines))
        self.assertEqual(config.gather({"SALESCALL_DSN": "x", "GEMINI_KEY_1": "架空のキー"}), {"SALESCALL_DSN": "x"})   # ほかの値は読まない
        env = {backup.KEY_ENV: "架空の鍵の元-0123456789-abcdefghij"}
        self.assertTrue(backup.key_matches("架空の鍵の元-0123456789-abcdefghij\n", env))
        self.assertFalse(backup.key_matches("架空の鍵の元-0123456789-abcdefghiJ", env))


if __name__ == "__main__":
    unittest.main()
