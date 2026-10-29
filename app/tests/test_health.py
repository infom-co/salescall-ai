"""状態の欄の影の試験（core/health.py・表の作り 0145。DESIGN.md 17章の3段目。持ち主 CL-2）。

2026-10-29 管理者の承認：「回線・行・契約者の状態を、それぞれ1つの欄に持ちます。OK」。今回は影：出来事を起こす所（17-2）が、今の作りの
処理の横で入口を呼び、入口は表（core/health_rules）を引いて、状態の欄と移った記録を書くだけ（することは行わない・今の作りの答えは変えない）。
- 入口を呼ぶ所（`Places`）：出来事ごとに、起こす所から本当に表が引かれ、引いた表の行番号が残る。
- 欄の守り（`Guards`）：試行のせいは1回だけ書ける（データベースが書き直しを断る）・書けるのは表を引く入口の2つの関数だけ・移った記録は
  書き換えられない・ほかの契約者の欄は扱えない。
- 影の失敗（`Failures`）：影が落ちても、今の作りは続く（失敗は記録する）。
各時点で今の作りの答えと比べる試験は、電話の一生の試験（tests/test_life.py の比べ＝tests/shadow.py）。⚠️ 試験のデータは架空のものだけ。
"""

from __future__ import annotations

import json
import unittest
import uuid
from dataclasses import replace
from datetime import timedelta
from unittest import mock

from salescall.core import attempts, health, health_rules as hr, next_action, ring_test, registry, stops, troubles
from salescall.core.access import Rejected
from salescall.supervisor import jobs, line
from tests import test_troubles
from tests.support import ADMIN, OPERATOR, SYSTEM, owner_access
from tests.test_gate import HOLDER, FakeDriver
from tests.test_life import Phone, Script, Talk
from tests.test_supervisor import SupervisorBase


class ShadowBase(SupervisorBase):
    row = test_troubles.OurTroubles.row
    trouble = test_troubles.OurTroubles.trouble
    report_fixed = test_troubles.OurTroubles.report_fixed
    new_version = test_troubles.OurTroubles.new_version
    requests = test_troubles.OurTroubles.requests

    def phone_of(self, r):
        return self.tx(r, lambda tx: tx.one("SELECT phone_e164 FROM salescall.contacts WHERE contact_id = %s",
                                            (r["contact"],))["phone_e164"])

    def round(self, s, phone, talk=None, line_id=None):
        """回線ごとの処理の1回分（本物の部品）と、締めの仕事。その回線でいちばん新しくかけた試行を返す（かけられなかった電話も）。"""
        line.one_round(self.w.a, SYSTEM, s["tenant"], line_id or s["line"], HOLDER, phone, talk or Talk())
        jobs.settle_ready(self.ctx(s))
        return owner_access().in_system(SYSTEM, lambda tx: tx.one(
            "SELECT attempt_id FROM salescall.attempts WHERE line_id = %s AND salescall.is_dialed(state)"
            " ORDER BY claimed_at DESC LIMIT 1", (line_id or s["line"],))["attempt_id"])

    def moves(self, s, **where):
        """移った記録（出来事・欄・どの段からどの段へ・引いた表の行番号・起こした所）。where で絞る（subject・event・origin）。"""
        conds = " AND ".join(f"m.{k} = %({k})s" for k in where)
        return [(r["subject"], r["event"], r["from_stage"], r["to_stage"], tuple(r["table_rows"]), r["origin"])
                for r in owner_access().in_tenant(SYSTEM, s["tenant"], lambda tx: tx.all(
                    "SELECT * FROM salescall.state_moves m WHERE m.tenant_id = salescall.cur_tenant()"
                    + (" AND " + conds if conds else "") + " ORDER BY m.move_id", where))]

    def state(self, s, subject, key):
        table, col = {"line": ("line_states", "line_id"), "task": ("task_states", "task_id"), "number": ("number_states", "contact_id"),
                      "tenant": ("tenant_states", "tenant_id")}[subject]
        row = owner_access().in_system(SYSTEM, lambda tx: tx.one(f"SELECT state FROM salescall.{table} WHERE {col} = %s", (key,)))
        return None if row is None else health.decode(row["state"])

    def blame(self, s, attempt_id):
        row = owner_access().in_system(SYSTEM, lambda tx: tx.one("SELECT blame FROM salescall.attempt_blames WHERE attempt_id = %s",
                                                                 (attempt_id,)))
        return None if row is None else row["blame"]

    def put_line(self, s, state, line_id=None):
        """試験の場面を作る：回線の欄を直に書く（本番で書くのは表を引く入口だけ。時計を進めない試験で、段を先に置くため）。"""
        owner_access().in_system(SYSTEM, lambda tx: tx.execute(
            "UPDATE salescall.line_states SET stage = %s, state = %s WHERE line_id = %s",
            (health.stage_name(state), json.dumps(health.encode(state)), line_id or s["line"])))

    def now(self, s):
        return self.tx(s, lambda tx: tx.one("SELECT now() AS t")["t"])

    def bad_row(self, s):
        """かけられない番号の行と、その電話の部品（その番号だけかけられない）。"""
        x = self.row(s)
        x["phone"] = self.phone_of(x)
        return x, Phone(bad={x["phone"]})


class Places(ShadowBase):
    """出来事を起こす所（17-2）ごとに、入口が表を引く。"""

    def test_a_call_that_did_not_ring_and_the_next_call_that_did(self):
        """E2（締めの仕事＝supervisor/jobs.settle_ready）：鳴らなかった電話で、回線は確かめ中 (a)（次に動く時刻＝その電話から15分）・
        行はその回線だけの判定待ち・試行のせいは「分からない」（L-2・R-1・N-4・V-1）。同じ回線で次の行の電話が鳴って話し終えた（E5）：
        回線は元気に戻り、前の電話は番号のせい（L-5・L-1→E16→R-2・N-1・V-3）。今の作り（unrung_blame）と同じ答え。"""
        s = self.ready()
        x, phone = self.bad_row(s)
        self.finish_others(s, x)
        first = self.round(s, phone)
        self.assertEqual(self.blame(s, first), "unknown")
        ln = self.state(s, "line", s["line"])
        self.assertEqual((ln.stage, ln.run_start + timedelta(minutes=15)), (hr.LineStage.CHECK_A, ln.next_at))
        task = self.state(s, "task", x["task"])
        self.assertEqual((task.stage, task.line), (hr.TaskStage.AWAIT_DECISION, str(s["line"])))
        self.assertIn(("line", "E2", "healthy", "check_a", ("L-2",), "after_call"), self.moves(s, event="E2"))
        self.assertEqual(self.tx(s, lambda tx: attempts.blame(tx, first)), "pending")   # 今の作りは今のまま
        self.row(s)
        got = self.round(s, phone)                                                      # 次の行（鳴る）
        self.assertNotEqual(got, first)
        self.assertEqual(self.blame(s, first), "number")
        self.assertEqual(self.tx(s, lambda tx: attempts.blame(tx, first)), "number")
        self.assertEqual(self.state(s, "line", s["line"]).stage, hr.LineStage.HEALTHY)
        self.assertEqual(self.state(s, "task", x["task"]).stage, hr.TaskStage.ON_PLAN)
        self.assertEqual(self.state(s, "number", x["contact"]).n, 1)
        self.assertIn(("attempt", "E16", "unknown", "number", ("V-3",), "after_call"), self.moves(s, event="E16"))

    def test_test_number_answers_decide_the_run(self):
        """E6・E7（テスト番号の口＝core/ring_test.run）：確かめ中 (c) の回線でテスト番号が鳴らなかった＝回線のせい・休み（L-10）。続きの
        試行は「回線」・行は予定どおり（R-3）・番号は数えない（N-3）。今の作り（close_unrung_run の test_silent）も回線のせい。休みで次の
        テストが鳴った（E6）＝元気に戻る（L-16）。E8（見分けられない）・E9（PC の部品が試さなかった）も、その口から渡る。"""
        s = self.ready()
        self.numbered(s)
        x, phone = self.bad_row(s)
        self.finish_others(s, x)
        first = self.round(s, phone)
        ln = self.state(s, "line", s["line"])
        self.put_line(s, replace(ln, stage=hr.LineStage.CHECK_C, next_at=self.now(s) + timedelta(minutes=1)))
        self.assertIs(ring_test.run(self.w.a, SYSTEM, s["tenant"], s["line"], FakeDriver(ring=False), "no_rows"), ring_test.Result.SILENT)
        self.assertEqual((self.blame(s, first), self.tx(s, lambda tx: attempts.blame(tx, first))), ("line", "line"))
        self.assertEqual(self.state(s, "line", s["line"]).stage, hr.LineStage.LINE_REST)
        self.assertIn(("task", "E16", "await_decision", "on_plan", ("R-3",), "tested"), self.moves(s, event="E16"))
        self.assertEqual(self.state(s, "number", x["contact"]).n, 0)
        self.next_ring()
        self.assertIs(ring_test.run(self.w.a, SYSTEM, s["tenant"], s["line"], FakeDriver(ring=True), "resume_check"), ring_test.Result.RANG)
        self.assertIn(("line", "E6", "line_rest", "healthy", ("L-16",), "tested"), self.moves(s, event="E6"))
        self.next_ring()
        ring_test.run(self.w.a, SYSTEM, s["tenant"], s["line"], FakeDriver(ring=None), "no_rows")
        self.next_ring()
        ring_test.run(self.w.a, SYSTEM, s["tenant"], s["line"], FakeDriver(busy=True), "no_rows")
        self.assertEqual([m[1] for m in self.moves(s, origin="tested")][-2:], ["E8", "E9"])

    def finish_others(self, s, x):
        """x のほかの行（s の行）を終える（かけるのは x だけ）。"""
        self.tx(s, lambda tx: next_action._append(tx, s["task"], call_at=None, end_reason="completed", cause="試験：終えた"))

    def test_time_comes_at_the_head_of_each_round(self):
        """E10（回線ごとの処理の毎回の頭＝supervisor/line.one_round）：確かめ中 (a) の次に動く時刻が来た＝待つ理由を見直し、テスト番号が
        無いので (b)「テスト番号が無い」で待つ（L-7。知らせは記録だけ）。テスト番号が入った（E14＝core/settings.set_value）で見直す（L-13）。"""
        s = self.ready()
        x, phone = self.bad_row(s)
        self.finish_others(s, x)
        self.round(s, phone)
        ln = self.state(s, "line", s["line"])
        self.put_line(s, replace(ln, next_at=self.now(s) - timedelta(seconds=1)))
        self.round(s, phone)
        self.assertIn(("line", "E10", "check_a", "check_b", ("L-7",), "tick"), self.moves(s, event="E10"))
        self.assertEqual(self.state(s, "line", s["line"]).wait, hr.Wait.NO_NUMBER)
        self.numbered(s)   # 鳴らしてよい時間の内か外かは、試験を流す時刻しだい（L-13 で待つ理由を見直すことだけを見る）
        self.assertIn(("line", "E14", "check_b"), [m[:3] for m in self.moves(s, event="E14")])
        self.assertIn(("L-13",), [m[4] for m in self.moves(s, event="E14")])

    def test_our_trouble_rests_the_row_and_a_contractor_puts_it_back(self):
        """E3（通話ごとの口＝core/troubles.after_trouble）：1回目は復旧待ち（R-5）・2回目（同じ行）は休み（R-8）・試行のせいは「こちら」
        （V-2）・契約者は種類ごとの違う行に数える（C-1）。今の作り（task_resting）と同じ。契約者が手で戻した（E12＝core/troubles.restore）：
        予定どおり（R-10）。"""
        s = self.ready()
        first = self.trouble(s, task=s["task"])
        self.assertEqual(self.blame(s, first), "ours")
        self.assertEqual(self.state(s, "task", s["task"]).stage, hr.TaskStage.AWAIT_RECOVERY)
        talked = self.w.talked(s)   # 復旧した（同じ契約者で、こちらの不具合なしに話し終えた電話）を影へも渡す（E5 → R-6）
        self.tx(s, lambda tx: health.after_call(tx, talked))
        self.assertEqual(self.state(s, "task", s["task"]).stage, hr.TaskStage.ON_PLAN)
        self.trouble(s, task=s["task"])
        self.assertEqual(self.state(s, "task", s["task"]).stage, hr.TaskStage.RESTING)
        self.assertTrue(self.tx(s, lambda tx: troubles.resting(tx, s["task"])))
        self.assertEqual(len(self.state(s, "tenant", s["tenant"]).r("call_ai_malfunction")), 1)
        self.tx(s, lambda tx: troubles.restore(tx, s["task"], reason="試験：直ったことを確かめた"), OPERATOR)
        self.assertIn(("task", "E12", "resting", "on_plan", ("R-10",), "by_hand_task"), self.moves(s, event="E12"))

    def test_our_trouble_is_given_at_the_call_before_the_settle(self):
        """E3 は通話ごとの口（core/troubles.after_trouble）で渡る：お客様が出たあとにこちらの不具合で終えた電話は、通話後の検証が済む
        （締めの仕事が締める）前から、行は復旧待ち・試行のせいは「こちら」。"""
        s = self.ready()
        called = line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, FakeDriver(),
                                test_troubles.Troubled("ai_malfunction", answered=True)).called
        self.assertEqual(self.tx(s, lambda tx: tx.one("SELECT verification::text AS v FROM salescall.attempts WHERE attempt_id = %s",
                                                      (called,)))["v"], "pending")   # 締めはまだ
        self.assertEqual(self.blame(s, called), "ours")
        self.assertEqual(self.state(s, "task", s["task"]).stage, hr.TaskStage.AWAIT_RECOVERY)

    def test_poor_audio_counts_on_the_line_and_the_row_waits_for_a_voiced_line(self):
        """E4（声が聞き取れずに終えた。締めの仕事のあと）：回線の声の続きに、そのお客様を数える（L-3。1人では止めない）。行は、声が通ると
        分かっている回線を待つ（R-11。すぐのかけ直しが続けて2回のうち＝今の作りの salescall.resume_allowed）。番号は鳴った電話（N-6）。"""
        s = self.ready()
        called = self.round(s, FakeDriver(), Script({}, default="trouble:poor_audio"))
        self.assertEqual(self.blame(s, called), "empty")
        ln = self.state(s, "line", s["line"])
        self.assertEqual((ln.stage, ln.m), (hr.LineStage.HEALTHY, 1))
        self.assertIn(("task", "E4", "on_plan", "voice_redial", ("R-11",), "after_call"), self.moves(s, event="E4"))

    def test_a_fixed_version_is_given_once(self):
        """E11（決まった間隔の仕事 fixed_stops）：その種類を直したと記録され、そのあと版が替わった＝契約者の数を0に（C-4）。同じ直した版は
        1回だけ。"""
        s = self.ready()
        self.trouble(s, task=s["task"])
        self.report_fixed("CL-1", "call_ai_malfunction")
        self.new_version()
        jobs.fixed_stops(self.ctx(s))
        jobs.fixed_stops(self.ctx(s))
        got = self.moves(s, event="E11")
        self.assertIn(("tenant", "E11", "normal", "normal", ("C-4",), "fixed"), got)
        self.assertIn(("task", "E11", "await_recovery", "on_plan", ("R-6",), "fixed"), got)
        self.assertEqual(len([m for m in got if m[0] == "tenant"]), 1)
        self.assertEqual(len(self.state(s, "tenant", s["tenant"]).r("call_ai_malfunction")), 0)
        self.assertEqual(owner_access().in_tenant(SYSTEM, s["tenant"], lambda tx: tx.all(   # 2回目は渡そうともしない（失敗の記録も無い）
            "SELECT failure FROM salescall.state_moves WHERE tenant_id = salescall.cur_tenant() AND failure IS NOT NULL")), [])

    def test_a_version_fixed_before_the_shadow_began_is_not_given(self):
        """E11 は、その契約者の影が動き始めたあとに替わった版だけ（表は E11 の時刻を見ずに待っている行を戻すので、前に直した版で、その
        あとのこちらの不具合の休みを戻さない＝core/health の読み方 (5)）。今の作り（task_resting）も休んだまま。"""
        from salescall.core import fix_requests
        key = f"試験：前に直した不具合 {uuid.uuid4()}"
        self.w.a.in_system(SYSTEM, lambda tx: fix_requests.request(tx, "CL-1", "call_ai_malfunction", key=key, summary="試験"))
        self.report_fixed("CL-1", "call_ai_malfunction")
        self.new_version()
        s = self.ready()
        self.trouble(s, task=s["task"])
        talked = self.w.talked(s)
        self.tx(s, lambda tx: health.after_call(tx, talked))
        self.trouble(s, task=s["task"])
        jobs.fixed_stops(self.ctx(s))
        self.assertEqual(self.state(s, "task", s["task"]).stage, hr.TaskStage.RESTING)
        self.assertTrue(self.tx(s, lambda tx: troubles.resting(tx, s["task"])))
        self.assertEqual(self.moves(s, event="E11"), [])

    def test_hands_resume_a_line_stop_and_remove_the_line(self):
        """E12（core/stops.resume。管理者か契約者が、回線の異常で止めた回線を解いた）：元気の回線では記録だけ（L-32）。システムが解いたとき
        は出来事にしない。E13（core/registry.retire_line）：使用停止（L-29）。"""
        s = self.ready()
        self.round(s, FakeDriver(), Talk())
        stop = self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: stops.stop(
            tx, scope="line", cause="line_failure", reason="試験", tenant_id=s["tenant"], line_id=s["line"]))["event_id"]
        self.w.a.in_tenant(OPERATOR, s["tenant"], lambda tx: stops.resume(tx, stop, reason="試験：直ったことを確かめた"))
        self.assertEqual([m[4] for m in self.moves(s, event="E12")], [("L-32",)])
        stop = self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: stops.stop(
            tx, scope="line", cause="line_failure", reason="試験", tenant_id=s["tenant"], line_id=s["line"]))["event_id"]
        self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: stops.resume(tx, stop, reason="試験：確かめで解けた"))
        self.assertEqual(len(self.moves(s, event="E12")), 1)                  # システムが解いた：手の再開ではない
        self.w.a.in_tenant(OPERATOR, s["tenant"], lambda tx: registry.retire_line(tx, s["line"], reason="試験：スマホを替えた"))
        self.assertIn(("line", "E13", "healthy", "out_of_use", ("L-29",), "removed"), self.moves(s, event="E13"))
        self.assertEqual(hr.LineStage.OUT_OF_USE.value, "使用停止")

    def test_another_line_calls_a_row_waiting_on_one_line(self):
        """E15（門の通過＝core/gate.claim）：回線 A の判定待ちの行に、回線 B の予約が通った＝判定待ちの電話を待たずに締める（R-4）。
        同じ回線 A の判定待ちには、門が通さない（出来事にならない）。"""
        s = self.ready()
        x, phone = self.bad_row(s)
        self.finish_others(s, x)
        self.round(s, phone)
        self.assertEqual(self.state(s, "task", x["task"]).stage, hr.TaskStage.AWAIT_DECISION)
        b = self.w.line(s["tenant"])
        self.lease(s, line=b)
        got = self.claim(s, line=b, task=x["task"])
        self.assertEqual(type(got).__name__, "Claim", got)
        self.assertIn(("task", "E15", "await_decision", "on_plan", ("R-4",), "claimed"), self.moves(s, event="E15"))

    def test_a_line_that_stays_in_call(self):
        """E17（supervisor/line.loop の Stuck）：かけられなくなった始まりは、数え直しても変わらない（began）。入口は、かけられないまま
        並びの1つ目（15分）を過ぎたら、1回知らせと修正依頼を記録する（L-28）。"""
        st = line.Stuck()
        from salescall.core.dial_exit import Outcome
        busy = line.Round(pause=Outcome.LINE_IN_CALL.value)
        self.assertFalse(st.seen(busy, 0.0))
        self.assertTrue(st.seen(busy, line.STUCK_AFTER))
        self.assertEqual((st.since, st.began), (line.STUCK_AFTER, 0.0))
        st.seen(line.Round(free=True), 100.0)
        self.assertEqual((st.since, st.began), (None, None))
        s = self.ready()
        health.in_tenant(self.w.a, SYSTEM, s["tenant"], health.stuck, s["line"], 16 * 60.0)
        got = owner_access().in_tenant(SYSTEM, s["tenant"], lambda tx: tx.one(
            "SELECT table_rows, actions FROM salescall.state_moves WHERE tenant_id = salescall.cur_tenant() AND event = 'E17'"))
        self.assertEqual((got["table_rows"], [a["what"] for a in got["actions"]]), (["L-28"], ["知らせる", "修正依頼"]))


class Guards(ShadowBase):
    """欄の守り（データベース）。"""

    def decided(self, s):
        """番号のせいと決まった試行を1つ作る（本物の道で）。"""
        x, phone = self.bad_row(s)
        self.finish_others(s, x)
        first = self.round(s, phone)
        self.row(s)
        self.round(s, phone)
        self.assertEqual(self.blame(s, first), "number")
        return first

    finish_others = Places.finish_others

    def test_the_blame_of_an_attempt_is_written_once(self):
        """試行のせいは1回だけ書ける（17-1-5・V-5）：番号になった試行を、回線にも分からないにも戻せない。空から番号へ飛ぶ・初めから番号で
        作る・消す、もデータベースが断る（持ち主の役でも）。"""
        s = self.ready()
        first = self.decided(s)
        own = owner_access()
        for blame in ("line", "unknown", "empty", "ours"):
            with self.assertRaises(Rejected) as e:
                own.in_system(SYSTEM, lambda tx, b=blame: tx.execute("UPDATE salescall.attempt_blames SET blame = %s WHERE attempt_id = %s",
                                                                     (b, first)))
            self.assertEqual(e.exception.constraint, "attempt_blame_once")
        with self.assertRaises(Rejected):
            own.in_system(SYSTEM, lambda tx: tx.execute("DELETE FROM salescall.attempt_blames WHERE attempt_id = %s", (first,)))
        other = self.w.insert_claim(s)
        with self.assertRaises(Rejected) as e:
            own.in_system(SYSTEM, lambda tx: tx.execute(
                "INSERT INTO salescall.attempt_blames (attempt_id, tenant_id, client_id, blame) VALUES (%s, %s, %s, 'number')",
                (other, s["tenant"], s["client"])))
        self.assertEqual(e.exception.constraint, "attempt_blame_once")
        own.in_system(SYSTEM, lambda tx: tx.execute(
            "INSERT INTO salescall.attempt_blames (attempt_id, tenant_id, client_id, blame) VALUES (%s, %s, %s, 'empty')",
            (other, s["tenant"], s["client"])))
        with self.assertRaises(Rejected):
            own.in_system(SYSTEM, lambda tx: tx.execute("UPDATE salescall.attempt_blames SET blame = 'line' WHERE attempt_id = %s",
                                                        (other,)))

    def test_only_the_two_functions_write_the_states(self):
        """アプリの役は、状態の欄・試行のせいの欄・移った記録を読むだけ（直に書けない）。書くのは health_take・health_save だけ。移った記録は
        書き換えも消すこともできない。"""
        s = self.ready()
        self.round(s, FakeDriver(), Talk())
        for sql in ("UPDATE salescall.line_states SET stage = 'healthy'",
                    "INSERT INTO salescall.tenant_states (tenant_id, stage, state) VALUES (salescall.cur_tenant(), 'normal', '{}')",
                    "UPDATE salescall.task_states SET stage = 'on_plan'",
                    "UPDATE salescall.attempt_blames SET blame = 'empty'",
                    "INSERT INTO salescall.state_moves (tenant_id, origin, failure) VALUES (salescall.cur_tenant(), '試験', '試験')"):
            with self.assertRaises(Rejected, msg=sql):
                self.tx(s, lambda tx, sql=sql: tx.execute(sql))
        self.assertTrue(self.tx(s, lambda tx: tx.all("SELECT * FROM salescall.line_states")))   # 読むことはできる
        for sql in ("UPDATE salescall.state_moves SET origin = '試験'", "DELETE FROM salescall.state_moves"):
            with self.assertRaises(Rejected, msg=sql):
                owner_access().in_system(SYSTEM, lambda tx, sql=sql: tx.execute(sql))

    def test_another_contractors_states_are_out_of_reach(self):
        """2つの関数は、取引の契約者の欄だけを扱う（ほかの契約者の回線・行・契約者は断る。全体の取引でも扱えない）。"""
        s, t = self.ready(), self.ready()
        init = {"stage": "healthy", "state": health.encode(hr.Line(hr.LineStage.HEALTHY))}
        for want in ([{"s": "line", "k": str(t["line"]), "init": init}],
                     [{"s": "tenant", "k": str(t["tenant"]), "init": init}],
                     [{"s": "task", "k": str(t["task"]), "init": init}]):
            with self.assertRaises(Rejected):
                self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx, w=want: tx.one("SELECT salescall.health_take(%s)", (json.dumps(w),)))
        with self.assertRaises(Rejected):
            self.w.a.in_system(ADMIN, lambda tx: tx.one("SELECT salescall.health_take(%s)",
                                                        (json.dumps([{"s": "line", "k": str(s["line"]), "init": init}]),)))
        self.round(t, FakeDriver(), Talk())
        with self.assertRaises(Rejected):
            self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: tx.one(
                "SELECT salescall.health_save(%s, '[]')",
                (json.dumps([{"s": "line", "k": str(t["line"]), "stage": "out_of_use", "state": {}}]),)))
        self.assertEqual(self.state(t, "line", t["line"]).stage, hr.LineStage.HEALTHY)


class Failures(ShadowBase):
    def test_a_failing_shadow_never_stops_the_current_work(self):
        """影（表を引く所）が落ちても、今の作りは続く：電話は締まり、行の結果も予定も今の作りのまま。影の欄は移らず、失敗が移った記録に
        残る（起こした所と、誤りの種類の名前）。"""
        s = self.ready()
        with mock.patch.object(health.hr, "apply", side_effect=RuntimeError("試験：表を引く所が落ちた")):
            called = self.round(s, FakeDriver(), Talk())
        self.assertTrue(self.tx(s, lambda tx: tx.one("SELECT salescall.is_settled(%s) AS d", (called,))["d"]))
        self.assertIsNone(self.blame(s, called))
        got = owner_access().in_tenant(SYSTEM, s["tenant"], lambda tx: [(r["origin"], r["failure"][:12]) for r in tx.all(
            "SELECT origin, failure FROM salescall.state_moves WHERE tenant_id = salescall.cur_tenant() AND failure IS NOT NULL"
            " ORDER BY move_id")])
        self.assertIn(("after_call", "RuntimeError"), got)
        self.tx(s, lambda tx: health.after_call(tx, called))                   # 直れば、あとで渡せる（1回だけ）
        self.tx(s, lambda tx: health.after_call(tx, called))
        self.assertEqual(self.blame(s, called), "empty")
        self.assertEqual(len(self.moves(s, subject="task", event="E5")), 1)


class LockOrder(unittest.TestCase):
    """鍵を取る順は 17章のとおり（回線→契約者。行・番号・試行のせいはそのあと）：入口が渡す順にかかわらず、データベースの関数へは
    この順で頼む（取引どうしが逆の順で鍵を待ち合って止まらない）。データベースを使わない。"""

    def test_the_states_are_asked_in_the_order_of_chapter_17(self):
        asked = []

        class Tx:
            def one(self, sql, params=None):
                if "health_take" in sql:
                    asked.extend(w["s"] for w in json.loads(params[0]))
                    raise LookupError("試験：ここまで")
                return {"t": uuid.uuid4(), "now": None}
        run = health._Run(Tx(), "試験")
        with self.assertRaises(LookupError):
            run.take(("attempt", uuid.uuid4()), ("number", uuid.uuid4()), ("task", uuid.uuid4()), ("tenant", uuid.uuid4()),
                     ("line", uuid.uuid4()))
        self.assertEqual(asked, list(health.ORDER))
