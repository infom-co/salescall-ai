"""中央の見守り役の試験（DESIGN.md 5-4・5-5。supervisor/・core/leases.py・core/scheduler.py・表の作り 0063）。PC の部品とのつなぎは
tests/test_link.py。"""

from __future__ import annotations

import tempfile
import threading
import time
import unittest
import uuid
from unittest import mock
from datetime import timedelta
from pathlib import Path

from salescall.core import (attempts, backup, business_rules, call_report, fix_requests, gate, leases, next_action, quality_stage,
                            registry, scheduler, stops)
from salescall.core.phone import normalize
from salescall.core.access import Rejected
from salescall.supervisor import config, jobs, line, updates
from salescall.supervisor import main as main_mod
from salescall.supervisor.links import Attached, NoLinks
from salescall.supervisor.main import Supervisor
from tests import support
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
            tx.execute("INSERT INTO salescall.item_values (tenant_id, client_id, task_id, version_id, question_id, state, attempt_id)"
                       " VALUES (%s, %s, %s, %s, 'Q1', 'confirmed', %s)",
                       (tenant_id, client_id, claim.task_id, claim.version_id, claim.attempt_id))
            call_report.save(tx, claim.attempt_id, rep())
            attempts.record_verification(tx, claim.attempt_id, summary=CLEAN)
        access.in_client(actor, tenant_id, client_id, work)


def make_runner(line_id, driver):
    return Runner()


class FakeLinks(NoLinks):
    """PC の部品のつなぎの代わり（どの回線が、どの契約者のどの PC につながっているか）。"""

    def __init__(self, attached=None):
        self.attached = dict(attached or {})

    def lines(self):
        return dict(self.attached)

    def driver(self, line_id, at):
        return FakeDriver()


class SupervisorBase(Base):
    def cfg(self, s=None, **more):
        values = {"SALESCALL_DSN": self.w.a._conninfo, "SALESCALL_CALL_RUNNER": "tests.test_supervisor:make_runner", **more}
        return config.load(values)

    def links(self, *xs, pc=None):
        return FakeLinks({x["line"]: Attached(x["tenant"], pc or uuid.UUID(int=1)) for x in xs})

    def ctx(self, s, links=None):
        return jobs.Context(self.w.a, self.cfg(s), HOLDER, links or NoLinks(), s["tenant"])

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
        for step in (lambda tx: leases.take_job(tx, key, "C", 60), lambda tx: leases.return_job(tx, key, "B")):
            with self.assertRaises(Rejected):                                  # 契約者・管理者は、借りる・返すをできない
                self.w.a.in_tenant(OPERATOR, self.w.tenant(), step)


class Recovery(SupervisorBase):
    def test_a_crashed_supervisor_cleans_up_its_own_lines_when_it_starts_again(self):
        """起動したら、前に落ちた自分の回線の、進行中の試行を終わらせる（予約のまま＝取り消し・発信してから＝結果不明）。"""
        s = self.ready()
        c = self.claim(s)
        self.assertTrue(line.dial_exit.dial(self.w.a, SYSTEM, s["tenant"], s["client"], c.attempt_id, FakeDriver()))
        got = self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: leases.recover(tx, HOLDER))
        self.assertEqual((got[0]["line_id"], got[0]["ended"]), (s["line"], 1))
        self.assertEqual(self.state(s, c.attempt_id), "technical_error")

    def test_only_expired_lines_of_a_former_hub_are_cleaned_up(self):
        s = self.ready()
        c = self.claim(s)
        self.assertEqual(self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: leases.recover(tx)), [])   # 生きている回線は触らない
        self.assertEqual(self.state(s, c.attempt_id), "claimed")
        self.lease(s, seconds=-1)                                              # 前の見守り役が落ちて、借用の期限が切れた
        got = self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: leases.recover(tx))
        self.assertEqual(got[0]["released"], 1)
        self.assertEqual(self.state(s, c.attempt_id), "released")


    def test_the_periodic_cleanup_leaves_its_own_lines_and_cleans_those_of_a_former_hub(self):
        """決まった間隔の後始末は、自分が借りている回線を、期限が切れていても終わらせない（データベースに届かないあいだに期限が
        切れても、まだ話している自分の通話を結果不明にしない）。前に動いていた中央の見守り役（名前が違う）の回線は、期限が切れたら
        後始末する（回線を借りるのは中央の見守り役だけ＝5-5。前の「落ちたままの PC の回線の後始末」は要らなくなった）。"""
        s = self.ready()
        c = self.claim(s)
        self.lease(s, seconds=-1)                                              # 自分の借用の期限が切れた（データベースに届かなかった）
        jobs.recover_expired(self.ctx(s))
        self.assertEqual(self.state(s, c.attempt_id), "claimed")
        jobs.recover_expired(jobs.Context(self.w.a, self.cfg(s), "新しい中央の見守り役#z", NoLinks(), s["tenant"]))
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
        driver = FakeDriver()
        got = line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, driver, Runner(RuntimeError("試験")))
        self.assertEqual(self.state(s, got.called), "technical_error")
        self.assertTrue(self.asked_supervisor(s))
        self.assertEqual(getattr(driver, "apologized", 0), 1)                 # 通話が残っていれば、おわびして切る

    def test_promised_calls_go_first_across_clients_and_keep_their_share_of_the_line(self):
        """お客様と約束したかけ直しは、すべての依頼者について先に（共有の回線で、先の依頼者のふつうの電話より、あとの依頼者の
        約束が先）。回線の1日の上限は、今日の約束の分を取り置く（ふつうの電話は使わない）。2026-10-19 ユーザー：「その日の約束の
        電話には目印が付き、それを処理してから、通常の順序の電話を始めた方がいい」「（取り置き）OK」。"""
        s = self.ready()                                                       # 先に作った依頼者：ふつうの電話（いまかけてよい）
        t = self.ready(tenant=s["tenant"])                                     # あとの依頼者：約束したかけ直し
        now = self.tx(t, lambda tx: tx.one("SELECT now() AS t")["t"])
        self.tx(t, lambda tx: support.schedule_call(tx, t["task"], now, contact_id=t["contact"], cause="試験", promised=True))
        self.put_client(s, "line.max_dials_per_day", 1)
        kept = next(x for x in (s, t) if self.tx(s, lambda tx, x=x: tx.one(
            "SELECT salescall.line_promised_due(%s, salescall.jst(now())::date, %s) AS n", (x["line"], uuid.uuid4()))["n"]))
        self.assertEqual(self.reason(s, line=kept["line"]), "line_daily_limit")   # ふつうの電話：約束が割り当たった回線は、その1本分を取り置く
        got = line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, FakeDriver(), Runner())
        self.assertEqual(self.tx(t, lambda tx: tx.one("SELECT task_id FROM salescall.attempts WHERE attempt_id = %s",
                                                      (got.called,)))["task_id"], t["task"])   # あとの依頼者の約束が先

    def test_an_exact_time_promise_keeps_one_line_free_shortly_before(self):
        """お客様が時刻を指定した約束（時刻ちょうど・幅の無い指定）が、これから始めるふつうの電話が終わるより前に来るなら、約束の
        数だけの回線を空けておく（ほかに空いている回線があれば、この回線はふつうの電話に使う）。2026-10-19 ユーザー：「1本は通常業務に
        使っていいでしょ！無駄なことするな。該当の回線を空けておくだけでいいじゃない」。約束が遠ければ、ふつうにかける。"""
        s = self.ready()
        t = self.ready(tenant=s["tenant"])                                     # 回線は2本（どちらも共有・空いている）
        now = self.tx(t, lambda tx: tx.one("SELECT now() AS t")["t"])
        promise = lambda at: self.tx(t, lambda tx: support.schedule_call(   # noqa: E731
            tx, t["task"], at, contact_id=t["contact"], cause="試験", promised=True, callee_at=at))
        promise(now + timedelta(minutes=3))
        c = self.claim(s)
        self.assertIsInstance(c, gate.Claim)                                   # もう1本が空いている：この回線はかける
        self.tx(s, lambda tx: attempts.release(tx, [c.attempt_id]))
        self.lease(t, seconds=-60)                                             # もう1本が使えない（借りられていない）
        self.assertEqual(self.reason(s), "promise_soon")                 # この回線を約束のために空けておく
        self.lease(t)
        u = self.ready(tenant=s["tenant"])
        self.lease(u, seconds=-60)
        busy = self.w.insert_claim(u, line=t["line"])                         # もう1本がほかのお客様と通話中
        self.assertEqual(self.reason(s), "promise_soon")
        self.tx(u, lambda tx: attempts.release(tx, [busy]))
        self.lease(t, seconds=-60)
        self.tx(s, lambda tx: support.schedule_call(tx, s["task"], now, contact_id=s["contact"], cause="試験", promised=True))
        self.assertEqual(self.reason(s), "promise_soon")                       # 時刻の決まっていない約束も、約束の時刻をふさぐ
        self.tx(s, lambda tx: support.schedule_call(tx, s["task"], now, contact_id=s["contact"], cause="試験", promised=True,
                                                        callee_at=now))
        c = self.claim(s)
        self.assertIsInstance(c, gate.Claim)                                   # 時刻を指定された約束どうしは、来た順にかける
        self.tx(s, lambda tx: attempts.release(tx, [c.attempt_id]))
        self.tx(s, lambda tx: next_action.schedule_now(tx, s["task"], cause="試験"))
        promise(now + timedelta(minutes=30))
        c = self.claim(s)
        self.assertIsInstance(c, gate.Claim)                                   # 約束が遠い：かける
        self.tx(s, lambda tx: attempts.release(tx, [c.attempt_id]))
        self.tx(s, lambda tx: next_action.schedule_now(tx, s["task"], cause="試験"))
        self.tx(t, lambda tx: support.schedule_call(tx, t["task"], now + timedelta(minutes=3), contact_id=t["contact"],
                                                        cause="試験：時刻の決まっていない約束", promised=True))
        self.assertIsInstance(self.claim(s), gate.Claim)                       # 時刻を指定していない約束のためには空けない（見直し7）

    def test_a_dedicated_line_is_kept_when_the_shared_line_is_needed_for_another_client(self):
        """専用の回線と共有の回線が混ざっても、約束と空いている回線の割り当てで決める（2026-10-19 の見直し：前は、依頼者Aの
        専用の回線から見ると「共有の回線が空いている」ので空けず、依頼者Bの約束をかける回線が無くなった）。"""
        s = self.ready()                                                       # この回線＝依頼者Aの専用（ふつうの電話）
        t = self.ready(tenant=s["tenant"])                                     # 共有の回線（空いている）
        u = self.ready(tenant=s["tenant"])                                     # 依頼者Bの約束
        self.lease(u, seconds=-60)                                             # 依頼者Bの回線は使えない
        self.w.a.in_tenant(OPERATOR, s["tenant"], lambda tx: tx.execute(
            "UPDATE salescall.lines SET client_id = %s WHERE line_id = %s", (s["client"], s["line"])))
        phone = normalize(fictional_jp_fixed()).e164
        k = self.w.contact(s["tenant"], s["client"], phone)
        mine = self.w.task(s["tenant"], s["client"], s["run"], k)             # 依頼者Aの約束
        now = self.tx(s, lambda tx: tx.one("SELECT now() AS t")["t"])
        at = now + timedelta(minutes=3)
        self.tx(s, lambda tx: support.schedule_call(tx, mine, at, contact_id=k, cause="試験", promised=True, callee_at=at))
        c = self.claim(s)
        self.assertIsInstance(c, gate.Claim)                                   # Aの約束は共有の回線でかけられる
        self.tx(s, lambda tx: attempts.release(tx, [c.attempt_id]))
        self.tx(u, lambda tx: support.schedule_call(tx, u["task"], at, contact_id=u["contact"], cause="試験", promised=True,
                                                        callee_at=at))
        self.assertEqual(self.reason(s), "promise_soon")                       # 共有の回線はBの約束に要る：この回線はAの約束に
        self.assertIn("promise_soon", gate.BUSINESS_REASONS)                   # 平均の短い業務の電話なら始められる

    def test_the_daily_reservation_is_shared_by_the_lines_and_skips_promises_that_will_not_be_called_now(self):
        """回線の1日の上限の取り置きは、約束をかけられる回線で等しく分けた、この回線の分（前は、どの回線も同じ約束をまるごと
        取り置いた）。止まっている依頼者の約束・いま通話中の約束は取り置かない。"""
        s = self.ready()
        t = self.ready(tenant=s["tenant"])
        u = self.ready(tenant=s["tenant"])                                     # 共有の回線が3本
        now = self.tx(s, lambda tx: tx.one("SELECT now() AS t")["t"])
        for x, at in ((t, now), (u, now - timedelta(days=2))):   # 今日の約束と、前の日から残っている約束（どちらも今日かける）
            self.tx(x, lambda tx, x=x, at=at: support.schedule_call(tx, x["task"], at, contact_id=x["contact"], cause="試験",
                                                                        promised=True))
        reserved = lambda: [self.tx(s, lambda tx, x=x: tx.one(   # noqa: E731
            "SELECT salescall.line_promised_due(%s, salescall.jst(now())::date, %s) AS n", (x["line"], uuid.uuid4()))["n"])
            for x in (s, t, u)]
        self.assertEqual(sum(reserved()), 2)                                   # 回線3本の取り置きの合計は、約束の数ちょうど
        self.put_client(s, "line.max_dials_per_day", 2)
        c = self.claim(s)
        self.assertIsInstance(c, gate.Claim)                                   # 約束2つを3本で分ける：この回線の分は1以下
        self.tx(s, lambda tx: attempts.release(tx, [c.attempt_id]))
        self.tx(s, lambda tx: next_action.schedule_now(tx, s["task"], cause="試験"))
        self.put_client(s, "line.max_dials_per_day", 1)
        kept = next(x for x, n in zip((s, t, u), reserved()) if n)            # 約束が割り当たった回線は、その分を取り置く
        self.assertEqual(self.reason(s, line=kept["line"]), "line_daily_limit")
        self.tx(u, lambda tx: stops.stop(tx, cause="budget", reason="試験", tenant_id=u["tenant"], scope="client",
                                         client_id=u["client"]))
        self.tx(t, lambda tx: support.schedule_call(tx, t["task"], now, contact_id=t["contact"], cause="試験", promised=True))
        busy = self.w.insert_claim(t, line=t["line"])                         # Tの約束はいま通話中
        self.assertEqual(sum(reserved()), 0)                                   # 止まっている・通話中の約束は取り置かない
        self.assertIsInstance(self.claim(s), gate.Claim)
        self.tx(t, lambda tx: attempts.release(tx, [busy]))

    def test_a_promise_is_reserved_on_its_clients_dedicated_line_first(self):
        """回線の1日の上限の取り置きは、約束をその依頼者の専用の回線に先に割り当てる。専用の回線が無い依頼者の約束は共有の回線へ
        （⚠️ 前は区別なく割り当て、専用の回線のある依頼者の約束が共有の回線に寄ると、共有の回線がほかの依頼者のふつうの電話を控え、
        専用の回線は上限まで使えた＝見直し5）。"""
        s = self.ready()                                                       # この回線＝依頼者Aの専用
        t = self.ready(tenant=s["tenant"])                                     # 共有の回線・依頼者B（専用の回線なし）
        self.w.a.in_tenant(OPERATOR, s["tenant"], lambda tx: tx.execute(
            "UPDATE salescall.lines SET client_id = %s WHERE line_id = %s", (s["client"], s["line"])))
        now = self.tx(s, lambda tx: tx.one("SELECT now() AS t")["t"])
        for _ in range(3):                                                     # Aの約束3つ（2つだと、守りを外しても約束の並びで
                                                                               # 偶然そろうことがあった＝2026-10-21 の全部の試験で見つけた）
            k = self.w.contact(s["tenant"], s["client"], normalize(fictional_jp_fixed()).e164)
            mine = self.w.task(s["tenant"], s["client"], s["run"], k)
            self.tx(s, lambda tx, mine=mine, k=k: support.schedule_call(tx, mine, now, contact_id=k, cause="試験",
                                                                        promised=True))
        self.tx(t, lambda tx: support.schedule_call(tx, t["task"], now, contact_id=t["contact"], cause="試験", promised=True))
        reserved = [self.tx(s, lambda tx, x=x: tx.one(
            "SELECT salescall.line_promised_due(%s, salescall.jst(now())::date, %s) AS n", (x["line"], uuid.uuid4()))["n"])
            for x in (s, t)]
        self.assertEqual(reserved, [3, 1])                                     # Aの約束はAの専用の回線・Bの約束は共有の回線

    def test_the_guard_is_the_average_call_length_of_the_business_once_measured(self):
        """空ける時間は、これから始めるふつうの電話の業務の平均通話時間（お客様が出た通話。2026-10-19 ユーザー：「30分かかる業務と
        1分で終わる業務では、同じにできない」）。数が少ないうちは設定の初めの値（5分）。"""
        s = self.ready()
        task = {"tenant_id": s["tenant"], "client_id": s["client"], "business_id": s["business"]}
        guard = lambda: self.tx(s, lambda tx: gate.promise_guard_seconds(tx, task))   # noqa: E731
        self.assertEqual(guard(), 300)                                         # まだ測っていない：初めの値
        self.put_client(s, "gate.exact_promise_guard_samples", 0)
        self.assertEqual(guard(), 300)                                         # 0本と決めても、測った通話が無ければ初めの値
        self.put_client(s, "gate.exact_promise_guard_samples", 1)
        broken = self.w.insert_claim(s)
        for st in ("dialing", "answered", "technical_error"):                  # 途中の誤りで終わった通話は数えない
            self.w.move(s, broken, st)
        self.assertEqual(guard(), 300)
        aid = self.w.insert_claim(s)
        for st in ("dialing", "answered", "in_conversation", "ending", "ended"):
            self.w.move(s, aid, st)
        self.assertLess(guard(), 60)                                           # 測った平均（試験の通話はすぐ終わる）

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
        self.assertTrue(self.w.a.in_system(SYSTEM, lambda tx: leases.take_job(tx, bad.key(ctx.tenant_id), "ほかのPC#x", 60)))


class FakeThread:
    """回線ごとの流れの代わり：止める印が立てば止まる（died＝思いがけず止まった）。"""

    def __init__(self, stop):
        self.stop, self.died = stop, False

    def is_alive(self):
        return not (self.died or self.stop.is_set())

    def join(self, timeout=None):
        pass


class Main(SupervisorBase):
    def starter(self):
        started = []

        def start(cfg, access, at, line_id, holder, links):
            started.append((line_id, at.pc_id, FakeThread(threading.Event())))
            return started[-1][2], started[-1][2].stop
        return started, start

    def supervisor(self, s, links=None, **kw):
        """試験の契約者だけを見る中央の見守り役（試験のデータベースのほかの契約者の仕事をしない）。"""
        sup = Supervisor(self.cfg(s), links if links is not None else self.links(s), holder=HOLDER, **kw)
        sup._load_tenants = lambda: setattr(sup, "tenants", [s["tenant"]])
        return sup

    def holder_of(self, s):
        return self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: tx.one(
            "SELECT lease_holder FROM salescall.lines WHERE line_id = %s", (s["line"],)))["lease_holder"]

    def test_a_line_flow_that_stops_is_cleaned_up_and_started_again(self):
        s = self.ready()
        started, start = self.starter()
        sup = self.supervisor(s, start_worker=start)
        sup.start()
        self.assertEqual((len(started), sup.leased), (1, {s["line"]: s["tenant"]}))
        c = self.claim(s)
        line.dial_exit.dial(self.w.a, SYSTEM, s["tenant"], s["client"], c.attempt_id, FakeDriver())
        started[0][2].died = True                                              # 回線ごとの流れが思いがけず止まった
        sup.watch_workers(0.0)
        self.assertEqual(self.state(s, c.attempt_id), "technical_error")        # その回線の通話を後始末
        self.assertTrue(self.asked_supervisor(s))
        sup.watch_workers(1.0)
        self.assertEqual(len(started), 1)                                       # すぐには立ち上げ直さない
        sup.watch_workers(1000.0)
        self.assertEqual(len(started), 2)                                       # 間をあけて立ち上げ直す
        self.lease(s, holder="ほかの見守り役#x")                               # 回線をほかに取られた（期限切れのあと）
        sup.renew(2000.0)
        self.assertEqual((sup.leased, started[1][2].stop.is_set()), ({}, True))  # その回線の流れは止める
        sup.watch_workers(2001.0)
        self.assertNotIn(s["line"], sup.workers)
        self.assertEqual(self.holder_of(s), "ほかの見守り役#x")                  # ほかの見守り役の回線は返さない
        sup.shutdown(0)

    def test_a_line_the_pc_lets_go_is_returned_and_a_line_moved_to_another_pc_starts_there_after_the_old_flow_ends(self):
        """PC がつながりを切った回線は、流れを止め、止まってから後始末して返す（通話の途中では返さない）。同じ回線を別の PC が
        つなぎ直したら、前の流れが止まってから、新しい PC で始める（同じスマホで二重にかけない）。"""
        s = self.ready()
        started, start = self.starter()
        links = self.links(s)
        sup = self.supervisor(s, links, start_worker=start)
        sup.start()
        first = started[0][2]
        links.attached[s["line"]] = Attached(s["tenant"], uuid.UUID(int=2))     # 別の PC がつなぎ直した
        first.stop.set = lambda: None                                           # 前の流れは、まだ通話の途中（止まらない）
        sup.renew(10.0)
        self.assertEqual(len(started), 1)                                       # 前の流れが止まるまで始めない
        sup.watch_workers(11.0)
        sup.renew(12.0)
        self.assertEqual(len(started), 1)
        first.died = True                                                       # 前の流れが止まった
        sup.watch_workers(13.0)
        sup.renew(14.0)
        self.assertEqual((len(started), started[1][1]), (2, uuid.UUID(int=2)))  # 新しい PC で始めた
        links.attached[s["line"]] = Attached(s["tenant"], uuid.UUID(int=2), 7)  # 同じ PC が、すぐつなぎ直した（新しいつなぎ）
        sup.renew(15.0)
        self.assertTrue(started[1][2].stop.is_set())                           # 前のつなぎの流れは止める
        sup.watch_workers(16.0)
        sup.renew(17.0)
        self.assertEqual((len(started), started[2][1]), (3, uuid.UUID(int=2)))  # 新しいつなぎで立ち上げ直した
        c = self.claim(s)
        links.attached.clear()                                                  # PC がつながりを切った
        started[2][2].stop.set = lambda: None                                   # まだ通話の途中
        self.lease(s, seconds=-1)                                               # 借用の期限が切れかけていても
        sup.renew(20.0)
        alive = self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: tx.one(
            "SELECT lease_expires_at > now() AS ok FROM salescall.lines WHERE line_id = %s", (s["line"],)))["ok"]
        self.assertTrue(alive)                                                  # 流れが動いているあいだは延ばし続ける（ほかの見守り役に後始末させない）
        sup.watch_workers(21.0)
        self.assertEqual((self.state(s, c.attempt_id), self.holder_of(s)), ("claimed", HOLDER))   # 通話の途中は返さない
        started[2][2].died = True
        sup.watch_workers(22.0)
        self.assertEqual((self.state(s, c.attempt_id), self.holder_of(s), sup.leased, sup.workers),
                         ("released", None, {}, {}))                            # 止まってから後始末して返す
        sup.shutdown(0)

    def test_the_supervisor_keeps_going_when_the_database_is_unreachable(self):
        """データベースに届かない・回線ごとの流れを立ち上げられない、のどれでも本体は止まらない（次の回にまた試す。続けば間を延ばす）。"""
        s = self.ready()
        class Unreachable:
            def __getattr__(self, name):
                def fail(*a, **k):
                    raise ConnectionError("試験：データベースに届かない")
                return fail
        sup = self.supervisor(s, start_worker=lambda *a: (_ for _ in ()).throw(OSError("試験")))
        real, sup.access = sup.access, Unreachable()
        sup.tick(0.0)                                                          # 止まらない
        self.assertEqual(sup._next_renew, main_mod.RENEW_FALLBACK)
        sup.access = real
        sup.tick(100.0)                                                        # 届いた：借りられたが、立ち上げられない
        w = sup.workers[s["line"]]
        self.assertEqual((w.thread, w.fails, w.restart_at), (None, 1, 100.0 + main_mod.RESTART_FIRST))
        sup.tick(101.0)                                                        # 間をあけるまでは試さない
        self.assertEqual(sup.workers[s["line"]].fails, 1)
        sup.shutdown(0)

    def test_a_server_program_out_of_step_with_the_database_makes_no_new_calls_and_does_no_jobs(self):
        """サーバーのプログラムとデータベースの版がずれていれば、回線ごとの流れを止め（いまの通話は最後まで）、立ち上げず、決まった
        間隔の仕事もしない。ずれは CL-2 へ。版が合ったら、またかける。"""
        s = self.ready()
        started, start = self.starter()
        sup = self.supervisor(s, start_worker=start)
        sup.start()
        self.assertEqual(len(started), 1)
        real = updates.PROGRAM
        updates.PROGRAM = ("0001_古いプログラム.sql", "0" * 64)                    # データベースが先に新しくなった
        try:
            sup.check_version()
            sup.renew(100.0)
            self.assertEqual((sup.version_gap, started[0][2].stop.is_set(), sup.leased),
                             (updates.PROGRAM_OLD, True, {s["line"]: s["tenant"]}))
            sup.watch_workers(10_000.0)
            self.assertEqual((len(started), s["line"] in sup.workers), (1, False))   # 立ち上げない
            sup._running.clear()
            sup.run_jobs(10_000.0)
            self.assertEqual(sup._running, {})                                    # 決まった間隔の仕事もしない
            c = self.claim(s)                                                     # 止めた流れが残した試行（落ちた）は、後始末する
            sup.renew(300.0)
            self.assertEqual(self.state(s, c.attempt_id), "released")
            logged = self.w.a.in_system(SYSTEM, lambda tx: tx.all(
                "SELECT 1 FROM salescall.error_events WHERE source = 'supervisor' AND message = '版のずれ：RuntimeError'"))
            self.assertTrue(logged)                                               # ずれは「見守り役の誤り」＝CL-2 へ修正依頼
        finally:
            updates.PROGRAM = real
        sup.check_version()
        sup.renew(400.0)                                                          # 版が合った：またかける
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
        # Windows で行の終わりが CRLF で取り出されても、指紋は同じ（⚠️ 前は「途中が違う」と読み、かけなくなった）
        import shutil
        from salescall.core import migrate
        crlf = Path(tempfile.mkdtemp(prefix="salescall-test-crlf-"))
        try:
            for f in migrate.migration_files():
                (crlf / f.name).write_bytes(f.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
            self.assertEqual(migrate.fingerprint(crlf), migrate.fingerprint())
        finally:
            shutil.rmtree(crlf)

    def test_an_old_pc_program_is_told_to_update_once_a_day_per_pc(self):
        """PC の部品のプログラムが古い：その契約者と管理者へ知らせる（自動で入れ替える部品があり、設定が自動なら「自動で入れ替えます」。
        無ければ「更新してください」）。同じ PC は1日1回。"""
        s, t = self.ready(), self.ready()
        pc = uuid.uuid4()
        for _ in range(2):
            self.assertFalse(self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: updates.pc_out_of_date(tx, s["tenant"], pc,
                                                                                                     can_replace=False)))
        notes = self.w.a.in_tenant(OPERATOR, s["tenant"], lambda tx: tx.all(
            "SELECT summary FROM salescall.notification_events WHERE kind = 'program_update'"))
        self.assertEqual(len(notes), 1)
        self.assertIn("プログラムを更新してください", notes[0]["summary"])
        admin = self.w.a.in_system(ADMIN, lambda tx: tx.all(
            "SELECT 1 FROM salescall.notification_events WHERE kind = 'program_update' AND origin_tenant = %s", (s["tenant"],)))
        self.assertEqual(len(admin), 1)                                           # 管理者にも写し
        self.assertTrue(self.w.a.in_tenant(SYSTEM, t["tenant"], lambda tx: updates.pc_out_of_date(tx, t["tenant"], pc,
                                                                                                can_replace=True)))
        auto = self.w.a.in_tenant(OPERATOR, t["tenant"], lambda tx: tx.one(
            "SELECT summary FROM salescall.notification_events WHERE kind = 'program_update'"))["summary"]
        self.assertIn("自動で入れ替えます", auto)

    def test_the_supervisor_calls_through_a_line_flow_and_returns_the_line(self):
        """本物の立ち上げ方（回線ごとに別の流れ）で1本かけ、決まった間隔の仕事が締め、止めると回線を返す。"""
        s = self.ready()
        sup = self.supervisor(s)
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
        self.assertIsNone(self.holder_of(s))                                     # 止めたら回線を返す

    def test_only_one_supervisor_per_machine(self):
        from salescall.link.locks import AlreadyRunning, SingleInstance
        path = Path(tempfile.mkdtemp()) / "lock"
        with SingleInstance(path):
            with self.assertRaises(AlreadyRunning):
                with SingleInstance(path):
                    pass

    def test_which_jobs_run(self):
        """仕事は、すべて中央の見守り役がする（5-5。前の「契約者の PC だけ・管理者の PC だけ」の分け方は無い）。ほかの担当の部品が
        要る仕事は、その設定があるときだけ。データベース全体の控えは、書く管理者の PC と接続の設定があるときだけ。控えの見張り・
        止まったまま続く停止の修正依頼・PC の合図の見張りは、いつも動く（取る仕事が動かないことに気づく）。"""
        s = self.ready()
        here = lambda cfg: {j.name for j in jobs.JOBS if j.wanted(cfg)}   # noqa: E731
        bare = here(self.cfg(s))
        self.assertTrue({"recover", "settle", "backups", "backup_watch", "whole_backup_watch", "lasting_stops",
                         "system_lasting_stops", "pc_watch", "budget", "holidays"} <= bare)
        self.assertFalse({"whole_backup", "verify", "stop_checks", "system_stop_checks"} & bare)
        full = here(self.cfg(s, SALESCALL_OWNER_DSN="x", SALESCALL_RESTORE_DSN="y", SALESCALL_ADMIN_TENANT=str(uuid.uuid4()),
                             SALESCALL_VERIFIER="m:v", SALESCALL_PROBER="m:p"))
        self.assertEqual(full, {j.name for j in jobs.JOBS})

    def test_tenant_jobs_run_once_for_every_tenant_and_system_jobs_once(self):
        """契約者ごとの仕事は、すべての契約者について1つずつ（その契約者の取引で）。共通の仕事は1つ。"""
        s, t = self.ready(), self.ready()
        seen = []
        fake = (jobs.Job("試験の契約者ごと", 60, "tenant", 60, lambda c: seen.append(("tenant", c.tenant_id))),
                jobs.Job("試験の共通", 60, "system", 60, lambda c: seen.append(("system", c.tenant_id))))
        sup = Supervisor(self.cfg(s), holder=f"試験#{uuid.uuid4().hex[:6]}")
        sup.check_version()
        sup._load_tenants()
        with mock.patch.object(jobs, "JOBS", fake):
            sup.run_jobs(0.0)
            sup.pool.shutdown(wait=True)
        self.assertIn(("tenant", s["tenant"]), seen)
        self.assertIn(("tenant", t["tenant"]), seen)
        self.assertEqual(seen.count(("system", None)), 1)
        self.assertEqual(len([x for x in seen if x[0] == "tenant"]), len(sup.tenants))

    def test_one_client_failing_does_not_hide_the_backups_of_the_others(self):
        """控えの見張りは依頼者ごと。1つの依頼者の見張りで誤りが出ても、ほかの依頼者を見張る（見直し9：前は最初の誤りで残りを見なかった）。"""
        s = self.ready()
        second = self.ready(tenant=s["tenant"])["client"]
        seen = []

        def watch_client(app, tenant_id, client_id):
            seen.append(client_id)
            if len(seen) == 1:
                raise RuntimeError("試験：1つ目の依頼者で落ちる")
            return False
        with mock.patch.object(backup, "watch_client", watch_client), mock.patch.object(jobs, "report"):
            jobs.backup_watch(self.ctx(s))
        self.assertEqual(sorted(seen), sorted([s["client"], second]))

    def test_settings_are_reported_without_values(self):
        lines = config.status({"SALESCALL_DSN": "postgresql://架空の接続", "GEMINI_KEY_1": "架空のキー"})
        self.assertIn("SALESCALL_DSN：入っています", lines)
        self.assertNotIn("架空の接続", "\n".join(lines))
        self.assertEqual(config.gather({"SALESCALL_DSN": "x", "GEMINI_KEY_1": "架空のキー"}), {"SALESCALL_DSN": "x"})   # ほかの値は読まない
        with self.assertRaises(config.ConfigMissing):
            config.load({"SALESCALL_LISTEN": "0.0.0.0:1"})                     # データベースの接続が無い
        with self.assertRaises(config.ConfigMissing):
            config.load({"SALESCALL_DSN": "x", "SALESCALL_LISTEN": "番号なし"})
        cfg = config.load({"SALESCALL_DSN": "x", "SALESCALL_LISTEN": "0.0.0.0:8765"})
        self.assertEqual((cfg.listen, cfg.calls, cfg.whole_backup), (("0.0.0.0", 8765), False, False))
        self.assertNotIn("x", repr(cfg).replace("0.0.0.0", ""))                  # 接続は表示に出さない
        with self.assertRaises(config.ConfigMissing):                          # つなぎ口の場所が無ければ動かない（黙って電話をかけない）
            main_mod.serve(config.load({"SALESCALL_DSN": "x"}))


if __name__ == "__main__":
    unittest.main()
