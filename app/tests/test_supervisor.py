"""見守り役の試験（DESIGN.md 5-4。supervisor/・core/leases.py・core/scheduler.py・表の作り 0063）。"""

from __future__ import annotations

import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path

from salescall.core import attempts, backup, call_report, fix_requests, leases, next_action, registry, scheduler, stops
from salescall.core.access import Rejected
from salescall.supervisor import config, jobs, line
from salescall.supervisor.main import Supervisor
from tests.support import CLEAN, OPERATOR, SYSTEM
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

    def test_a_failure_in_the_call_ends_only_that_call_and_asks_cl2(self):
        s = self.ready()
        got = line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, FakeDriver(), Runner(RuntimeError("試験")))
        self.assertEqual(self.state(s, got.called), "technical_error")
        self.assertTrue(self.asked_supervisor(s))

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
