"""状態の表を引く入口の試験（core/health.py・表の作り 0145・0146。DESIGN.md 17章の4段目。持ち主 CL-2）。

2026-10-29 管理者の承認：「回線・行・契約者の状態を、それぞれ1つの欄に持ちます。OK」。4段目：出来事を起こす所（17-2）が入口を呼び、
入口は表（core/health_rules）を引いて、状態の欄と移った記録を書き、することを行う（知らせ・修正依頼・電話ごとの記録・新しい電話を止める・
戻す）。読み手（締め・次の動き・門・候補・回線ごとの処理）は状態の欄を読む。
- 入口を呼ぶ所（`Places`）：出来事ごとに、起こす所から本当に表が引かれ、引いた表の行番号が残る。
- することを行う（`Doing`）：表が返したことを、入口の1か所が行う（今の作りの同じ仕事は二重に出ない）。
- 欄の守り（`Guards`）：試行のせいは1回だけ書ける（データベースが書き直しを断る）・書けるのは表を引く入口の2つの関数だけ・移った記録は
  書き換えられない・ほかの契約者の欄は扱えない。
- 失敗（`Failures`）：表を引く所が落ちたら、その出来事は黙って捨てない（取引ごと失敗し、あとで1回だけ渡る）。
時計を進める場面（17-8 の今の作りの誤りが無くなったこと）は tests/test_life.py、管理者のご決定どおりかの試験は tests/test_decided.py。
⚠️ 試験のデータは架空のものだけ。
"""

from __future__ import annotations

import json
import re
import unittest
import uuid
from dataclasses import replace
from datetime import timedelta
from unittest import mock

from salescall.core import fix_requests, health, health_rules as hr, next_action, registry, settings, stops, troubles
from salescall.core.access import Rejected
from salescall.core.phone import normalize
from salescall.supervisor import jobs, line
from tests import test_troubles
from tests.support import ADMIN, OPERATOR, SYSTEM, fictional_jp_fixed, owner_access
from tests.test_gate import HOLDER, FakeDriver
from tests.test_life import Phone, Script, Talk
from tests.test_supervisor import SupervisorBase


class HealthBase(SupervisorBase):
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
        """移った記録（欄・出来事・どの段からどの段へ・引いた表の行番号・起こした所）。where で絞る（subject・event・origin）。"""
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

    def put_number(self, s, contact_id, state):
        owner_access().in_system(SYSTEM, lambda tx: tx.execute(
            "UPDATE salescall.number_states SET stage = %s, state = %s WHERE contact_id = %s",
            (health.stage_name(state), json.dumps(health.encode(state)), contact_id)))

    def now(self, s):
        return self.tx(s, lambda tx: tx.one("SELECT now() AS t")["t"])

    def bad_row(self, s):
        """かけられない番号の行と、その電話の部品（その番号だけかけられない）。"""
        x = self.row(s)
        x["phone"] = self.phone_of(x)
        return x, Phone(bad={x["phone"]})

    def finish_others(self, s, x):
        """x のほかの行（s の行）を終える（かけるのは x だけ）。"""
        self.tx(s, lambda tx: next_action._append(tx, s["task"], call_at=None, end_reason="completed", cause="試験：終えた"))

    def due(self, s, line_id=None):
        """回線の欄の次に動く時刻を、いまより前にする（時計を進めない試験で、その時が来たことにする。前のテストも、あける長さより前に
        したことにする）。"""
        ln = self.state(s, "line", line_id or s["line"])
        before = None if ln.tested_at is None else ln.tested_at - timedelta(hours=6)   # 前のテストからあける長さも過ぎた
        self.put_line(s, replace(ln, next_at=self.now(s) - timedelta(seconds=1), tested_at=before), line_id)

    def notes(self, s, kind):
        return self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: [r["refs"] for r in tx.all(
            "SELECT refs FROM salescall.notification_events WHERE kind = %s AND tenant_id = salescall.cur_tenant()"   # 全体あての知らせは見ない
            " ORDER BY event_id", (kind,))])


class Places(HealthBase):
    """出来事を起こす所（17-2）ごとに、入口が表を引く。"""

    def test_a_call_that_did_not_ring_and_the_next_call_that_did(self):
        """E2（電話が終わった所＝回線ごとの処理）：鳴らなかった電話で、回線は確かめ中 (a)（次に動く時刻＝その電話から並びの1つ目）・
        行はその回線だけの判定待ち・試行のせいは「分からない」（L-2・R-1・N-4・V-1）。締めの処理は、判定待ちのあいだ締めない（行の欄を
        読む）。同じ回線で次の行の電話が鳴って話し終えた（E5）：回線は元気に戻り、前の電話は番号のせい（L-5・L-1→E16→R-2・N-1・V-3）。
        締めの処理が前の電話を締め、次の動きは番号の欄の決まった順で予定を書く。"""
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
        self.assertFalse(self.tx(s, lambda tx: tx.one("SELECT salescall.is_settled(%s) AS d", (first,))["d"]))   # 判定待ち：締めない
        self.assertEqual(self.tx(s, lambda tx: health.blame_of(tx, first)), "pending")
        self.row(s)
        got = self.round(s, phone)                                                      # 次の行（鳴る）
        self.assertNotEqual(got, first)
        self.assertEqual((self.blame(s, first), self.tx(s, lambda tx: health.blame_of(tx, first))), ("number", "number"))
        self.assertEqual(self.state(s, "line", s["line"]).stage, hr.LineStage.HEALTHY)
        self.assertEqual(self.state(s, "task", x["task"]).stage, hr.TaskStage.ON_PLAN)
        self.assertEqual(self.state(s, "number", x["contact"]).n, 1)
        self.assertIn(("attempt", "E16", "unknown", "number", ("V-3",), "after_call"), self.moves(s, event="E16"))
        jobs.settle_ready(self.ctx(s))
        self.assertTrue(self.tx(s, lambda tx: tx.one("SELECT salescall.is_settled(%s) AS d", (first,))["d"]))
        wait = self.tx(s, lambda tx: health.number_wait(tx, x["contact"]))
        plan = self.tx(s, lambda tx: next_action.current(tx, x["task"]))
        self.assertGreaterEqual(plan["call_at"], wait.not_before)                      # 番号の欄の決まった順より前にかけない

    def test_the_line_asks_its_column_and_rings_the_test_number(self):
        """E10（回線ごとの処理の毎回の頭＝core/health.tick）と E6・E7・E8・E9（テスト番号の口＝core/ring_test.run）：確かめ中 (a) の
        次に動く時刻が来た＝表が鳴らす頼みを置き（L-7 → (c)）、回線ごとの処理がテスト番号を鳴らす。鳴らなかった＝回線のせい・休み
        （L-10）：続きの試行は「回線」・行は予定どおり（R-3）・番号は数えない（N-3）。休みのあいだ、回線ごとの処理はお客様へかけない。
        次のテストの時刻が来て鳴った（E6）＝元気に戻る（L-16）。見分けられない（E8）・PC の部品が試さなかった（E9）も、その口から渡る。"""
        s = self.ready()
        self.numbered(s)
        x, phone = self.bad_row(s)
        self.finish_others(s, x)
        first = self.round(s, phone)
        self.due(s)
        silent = FakeDriver(ring=False)
        got = line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, silent, Talk())
        self.assertEqual((len(silent.rings), got.pause), (1, "line_stopped"))          # 鳴らして、休みなのでかけない
        self.assertIn(("line", "E10", "check_a", "check_c", ("L-7",), "tick"), self.moves(s, event="E10"))
        self.assertEqual(self.blame(s, first), "line")
        self.assertEqual(self.state(s, "line", s["line"]).stage, hr.LineStage.LINE_REST)
        self.assertIn(("task", "E16", "await_decision", "on_plan", ("R-3",), "tested"), self.moves(s, event="E16"))
        self.assertEqual(self.state(s, "number", x["contact"]).n, 0)
        self.assertEqual(silent.calls, [])                                             # 休みの回線はお客様へかけない
        self.due(s)
        rang = FakeDriver(ring=True)
        line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, rang, Talk())
        self.assertIn(("line", "E6", "line_rest", "healthy", ("L-16",), "tested"), self.moves(s, event="E6"))
        self.assertEqual(self.tx(s, lambda tx: tx.one("SELECT purpose FROM salescall.line_ring_tests WHERE line_id = %s AND rang"
                                                      " ORDER BY test_id DESC LIMIT 1", (s["line"],))["purpose"]), "resume_check")
        for driver, code in ((FakeDriver(ring=None), "E8"), (FakeDriver(busy=True), "E9")):
            ln = self.state(s, "line", s["line"])
            self.put_line(s, hr.Line(hr.LineStage.CHECK_C, run_start=self.now(s), run=(("a", "n"),),
                                     next_at=self.now(s) + timedelta(minutes=1), audio=ln.audio, j=ln.j))
            line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, driver, Talk())
            self.assertEqual(self.moves(s, origin="tested")[-1][1], code)

    def test_time_comes_at_the_head_of_each_round(self):
        """E10（回線ごとの処理の毎回の頭）：確かめ中 (a) の次に動く時刻が来た＝待つ理由を見直し、テスト番号が無いので (b)「テスト番号が
        無い」で待つ（L-7。契約者と管理者に「テスト番号を入れてください」と1回知らせる）。テスト番号が入った（E14＝core/settings.set_value）
        で見直す（L-13）。"""
        s = self.ready()
        x, phone = self.bad_row(s)
        self.finish_others(s, x)
        self.round(s, phone)
        self.due(s)
        self.round(s, phone)
        self.assertIn(("line", "E10", "check_a", "check_b", ("L-7",), "tick"), self.moves(s, event="E10"))
        self.assertEqual(self.state(s, "line", s["line"]).wait, hr.Wait.NO_NUMBER)
        self.assertEqual(len(self.notes(s, "line_undecided")), 1)
        self.numbered(s)   # 鳴らしてよい時間は 0:00〜24:00＝テスト番号を鳴らす頼み（確かめ中 (c)）
        self.assertIn(("line", "E14", "check_b"), [m[:3] for m in self.moves(s, event="E14")])
        self.assertIn(("L-13",), [m[4] for m in self.moves(s, event="E14")])
        self.assertTrue(self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: health.tick(tx, s["line"])).ring)

    def test_our_trouble_rests_the_row_and_a_contractor_puts_it_back(self):
        """E3（通話ごとの口＝core/troubles.after_trouble）：1回目は復旧待ち（R-5）・2回目（同じ行）は休み（R-8）・試行のせいは「こちら」
        （V-2）・契約者は種類ごとの違う行に数える（C-1）。契約者が手で戻した（E12＝core/troubles.restore）：予定どおり（R-10）。"""
        s = self.ready()
        first = self.trouble(s, task=s["task"])
        self.assertEqual(self.blame(s, first), "ours")
        self.assertEqual(self.state(s, "task", s["task"]).stage, hr.TaskStage.AWAIT_RECOVERY)
        self.w.talked(s)   # 復旧した（同じ契約者で、こちらの不具合なしに話し終えた電話。E5 → R-6）
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
        """E4（声が聞き取れずに終えた）：回線へは電話が終わった所で（L-3。そのお客様を声の続きに数える。1人では止めない）、行へは締めの
        あと（R-11。すぐのかけ直しが続けて2回のうち＝salescall.resume_allowed が、締めが残す「途中で切れたか」を読む）。行は声の通る回線を
        待つ（門・候補は、その行を出さない）。番号は鳴った電話（N-6）。"""
        s = self.ready()
        called = self.round(s, FakeDriver(), Script({}, default="trouble:poor_audio"))
        self.assertEqual(self.blame(s, called), "empty")
        ln = self.state(s, "line", s["line"])
        self.assertEqual((ln.stage, ln.m), (hr.LineStage.HEALTHY, 1))
        got = self.moves(s, event="E4")
        self.assertEqual(got, [("line", "E4", "healthy", "healthy", ("L-3",), "after_call"),
                               ("task", "E4", "on_plan", "voice_redial", ("R-11",), "after_settle")])
        jobs.settle_ready(self.ctx(s))
        self.assertEqual(len(self.moves(s, event="E4")), 2)                            # 締めのあとの分も1回だけ
        self.assertEqual(self.reason(s), "voice_redial")

    def test_a_fixed_version_is_given_once(self):
        """E11（決まった間隔の仕事 fixed_stops）：その種類を直したと記録され、そのあと版が替わった＝契約者の数を0に（C-4）・復旧待ちの行を
        戻す（R-6）。同じ直した版は1回だけ。"""
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

    def test_a_version_fixed_before_the_columns_began_is_not_given(self):
        """E11 は、その契約者の欄が動き始めたあとに替わった版だけ（表は E11 の時刻を見ずに待っている行を戻すので、前に直した版で、その
        あとのこちらの不具合の休みを戻さない＝core/health の読み方 (4)）。"""
        key = f"試験：前に直した不具合 {uuid.uuid4()}"
        self.w.a.in_system(SYSTEM, lambda tx: fix_requests.request(tx, "CL-1", "call_ai_malfunction", key=key, summary="試験"))
        self.report_fixed("CL-1", "call_ai_malfunction")
        self.new_version()
        s = self.ready()
        self.trouble(s, task=s["task"])
        self.w.talked(s)
        self.trouble(s, task=s["task"])
        jobs.fixed_stops(self.ctx(s))
        self.assertEqual(self.state(s, "task", s["task"]).stage, hr.TaskStage.RESTING)
        self.assertTrue(self.tx(s, lambda tx: troubles.resting(tx, s["task"])))
        self.assertEqual(self.moves(s, event="E11"), [])

    def test_hands_resume_a_line_and_remove_it(self):
        """E12（画面＝core/health.by_hand_line。管理者か契約者が、直ったことを確かめて手で再開）：休みの回線は元気に（L-21）・元気の回線では
        記録だけ（L-32）。前の作りで回線の異常で止めた停止の記録を、管理者か契約者が解いた（core/stops.resume）も E12。システムが解いた
        ときは出来事にしない。E13（core/registry.retire_line）：使用停止（L-29）。"""
        s = self.ready()
        self.round(s, FakeDriver(), Talk())
        self.put_line(s, hr.Line(hr.LineStage.LINE_REST, t=1, next_at=self.now(s) + timedelta(minutes=15), entered_at=self.now(s),
                                 fix_at=self.now(s) + timedelta(hours=1), tested_at=self.now(s)))
        self.w.a.in_tenant(OPERATOR, s["tenant"], lambda tx: health.by_hand_line(tx, s["line"], "試験：スマホを確かめた"))
        self.assertEqual([m[4] for m in self.moves(s, event="E12")], [("L-21",)])
        self.assertEqual(self.state(s, "line", s["line"]).stage, hr.LineStage.HEALTHY)
        with self.assertRaises(PermissionError):
            self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: health.by_hand_line(tx, s["line"], "試験"))   # システムは手で再開しない
        stop = self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: stops.stop(
            tx, scope="line", cause="line_failure", reason="試験：前の作りの停止", tenant_id=s["tenant"], line_id=s["line"]))["event_id"]
        self.w.a.in_tenant(OPERATOR, s["tenant"], lambda tx: stops.resume(tx, stop, reason="試験：直ったことを確かめた"))
        self.assertEqual([m[4] for m in self.moves(s, event="E12")], [("L-21",), ("L-32",)])
        stop = self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: stops.stop(
            tx, scope="line", cause="line_failure", reason="試験", tenant_id=s["tenant"], line_id=s["line"]))["event_id"]
        self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: stops.resume(tx, stop, reason="試験：確かめで解けた"))
        self.assertEqual(len(self.moves(s, event="E12")), 2)                  # システムが解いた：手の再開ではない
        self.w.a.in_tenant(OPERATOR, s["tenant"], lambda tx: registry.retire_line(tx, s["line"], reason="試験：スマホを替えた"))
        self.assertIn(("line", "E13", "healthy", "out_of_use", ("L-29",), "removed"), self.moves(s, event="E13"))
        self.assertEqual(hr.LineStage.OUT_OF_USE.value, "使用停止")

    def test_another_line_calls_a_row_waiting_on_one_line(self):
        """E15（門の通過＝core/gate.claim）：回線 A の判定待ちの行に、回線 B の予約が通った＝判定待ちの電話を待たずに締める（R-4）。
        同じ回線 A の判定待ちには、門が通さない（締め待ちは行の欄で読む）。"""
        s = self.ready()
        x, phone = self.bad_row(s)
        self.finish_others(s, x)
        self.round(s, phone)
        self.assertEqual(self.state(s, "task", x["task"]).stage, hr.TaskStage.AWAIT_DECISION)
        self.assertEqual(self.reason(s, task=x["task"]), "awaiting_decision")
        b = self.w.line(s["tenant"])
        self.lease(s, line=b)
        got = self.claim(s, line=b, task=x["task"])
        self.assertEqual(type(got).__name__, "Claim", got)
        self.assertIn(("task", "E15", "await_decision", "on_plan", ("R-4",), "claimed"), self.moves(s, event="E15"))

    def test_a_line_that_stays_in_call(self):
        """E17（supervisor/line.loop の Stuck）：かけられなくなった始まりは、数え直しても変わらない（began）。入口は、かけられないまま
        並びの1つ目（15分）を過ぎたら、1回知らせて修正依頼を出す（L-28。回線の異常には数えない）。"""
        st = line.Stuck()
        from salescall.core.dial_exit import Outcome
        busy = line.Round(pause=Outcome.LINE_IN_CALL.value)
        self.assertFalse(st.seen(busy, 0.0))
        self.assertTrue(st.seen(busy, line.STUCK_AFTER))
        self.assertEqual((st.since, st.began), (line.STUCK_AFTER, 0.0))
        st.seen(line.Round(free=True), 100.0)
        self.assertEqual((st.since, st.began), (None, None))
        s = self.ready()
        self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: health.stuck(tx, s["line"], 16 * 60.0))
        got = owner_access().in_tenant(SYSTEM, s["tenant"], lambda tx: tx.one(
            "SELECT table_rows, actions FROM salescall.state_moves WHERE tenant_id = salescall.cur_tenant() AND event = 'E17'"))
        self.assertEqual((got["table_rows"], [a["what"] for a in got["actions"]]), (["L-28"], ["知らせる", "修正依頼"]))
        self.assertEqual(len(self.notes(s, "line_undecided")), 1)
        self.assertEqual(len(self.requests("CL-2", "line_in_call")), 1)
        self.assertEqual(self.faults(s), [])                                  # 回線の異常としては記録しない

    def test_a_held_number_is_looked_at_again_the_next_day(self):
        """E10（印の見直しの時刻が来た番号。N-7＝17-3-6 の【当】(b)）：番号の待つ印は、次の日の始まりに、回線ごとの処理の毎回の頭で
        見直す（⛔ 3段目：日付が替わっても印が残った）。"""
        s = self.ready()
        x, phone = self.bad_row(s)
        self.finish_others(s, x)
        self.round(s, phone)
        held = hr.Number(hr.NumberStage.NONE, unknown=frozenset({"a1"}), unknown_since=self.now(s) - timedelta(days=1),
                         unknown_last=self.now(s) - timedelta(days=1), hold=True, next_at=self.now(s) - timedelta(seconds=1))
        self.put_number(s, x["contact"], held)
        self.assertTrue(self.tx(s, lambda tx: health.number_held(tx, x["contact"])))
        self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: health.tick(tx, s["line"]))
        self.assertIn(("number", "E10", "none", "none", ("N-7",), "tick"), self.moves(s, event="E10"))
        self.assertFalse(self.tx(s, lambda tx: health.number_held(tx, x["contact"])))


class Doing(HealthBase):
    """表が返したことを、入口の1か所が行う（今の作りの同じ仕事は二重に出ない）。"""

    def silent_rest(self, s):
        """テスト番号が鳴らない回線を、表の道で回線のせい・休みにする（確かめ中 (a) → (c) → 鳴らない）。"""
        self.numbered(s)
        x, phone = self.bad_row(s)
        self.finish_others(s, x)
        self.round(s, phone)
        self.due(s)
        line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, FakeDriver(ring=False), Talk())
        return x

    def test_a_line_fault_is_told_when_the_next_test_does_not_ring_either(self):
        """2026-10-29 管理者「2.案B」：テスト番号が鳴らなかった時からお客様へかけない（休み）が、知らせは次のテストでも鳴らなかったとき
        （L-10 は知らせない・L-17 で1回）。止めた知らせは、状態の表が出す1つだけ（停止の記録は作らない＝回線の休みは回線の欄で持つ）。"""
        s = self.ready()
        self.silent_rest(s)
        self.assertEqual((self.notes(s, "stopped"), self.state(s, "line", s["line"]).stage), ([], hr.LineStage.LINE_REST))
        for _ in range(2):
            self.due(s)
            line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, FakeDriver(ring=False), Talk())
        got = self.notes(s, "stopped")
        self.assertEqual([(r["line_id"], r["cause"], r["rows"]) for r in got], [(str(s["line"]), "line_failure", ["L-17"])])
        self.assertEqual(self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: tx.all(
            "SELECT 1 FROM salescall.stop_events WHERE scope = 'line'")), [])
        self.due(s)
        line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, FakeDriver(ring=True), Talk())
        self.assertEqual([r["rows"] for r in self.notes(s, "resumed")], [["L-16"]])

    def test_an_unclear_answer_is_told_once_and_asks_the_phone_fixer(self):
        """2026-10-25 夜 管理者「OK」：見分けられないと返ったら、その続きで1回だけ知らせ、電話をつなぐ部品の直す担当へ修正依頼（同じ
        不具合で1つ）。2回目は知らせない。"""
        s = self.ready()
        self.numbered(s)
        x, phone = self.bad_row(s)
        self.finish_others(s, x)
        self.round(s, phone)
        for _ in range(2):
            self.due(s)
            line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, FakeDriver(ring=None), Talk())
        self.assertEqual([m[4] for m in self.moves(s, event="E8")], [("L-11",), ("L-11",)])
        self.assertEqual(len(self.notes(s, "line_undecided")), 1)
        self.assertEqual(len(self.requests("CL-2", "test_ring_undecided")), 1)

    def test_our_trouble_is_recorded_told_and_asked_per_call_and_three_rows_stop_new_calls(self):
        """2026-10-25 管理者：「その行だけなら、契約者(管理者含む)と、開発AIの担当、記録にそれを伝え、2回続いたら、次の行へ。3行同じ問題なら、
        甚大な問題と判断。」：電話ごとの記録・知らせ・修正依頼は表の R-5 を入口が行う（watch.record_error の1か所。電話ごと）。違う3行で
        契約者の欄が止め（C-1）、入口が新しい電話を止める（契約者の範囲の停止の記録。止めた知らせは表の1つだけ）。直した版（E11）で戻す（C-5）。"""
        s = self.ready()
        asked_before = len(self.requests("CL-1", "call_ai_malfunction"))     # 修正依頼は全体で1つ（ほかの試験の分が開いていることがある）
        rows = [s, self.row(s), self.row(s)]
        for r in rows[1:]:
            self.tx(r, lambda tx, r=r: next_action._append(tx, r["task"], call_at=None, end_reason="completed", cause="試験：まだ"))
        for i, r in enumerate(rows):
            if i:
                self.tx(r, lambda tx, r=r: next_action.schedule_now(tx, r["task"], cause="試験"))
            self.trouble(s, task=r["task"])
        errors_ = self.tx(s, lambda tx: tx.one("SELECT count(*) AS n FROM salescall.error_events WHERE kind = 'call_ai_malfunction'"))
        self.assertEqual(errors_["n"], 3)                                     # 電話ごとに記録
        self.assertEqual(len(self.notes(s, "error_notice")), 3)               # 電話ごとに知らせ
        self.assertIn(len(self.requests("CL-1", "call_ai_malfunction")), range(max(1, asked_before), asked_before + 2))   # 修正依頼は同じ不具合で1つ
        messages = self.tx(s, lambda tx: [r["message"] for r in tx.all(
            "SELECT message FROM salescall.error_events WHERE kind = 'call_ai_malfunction'")])
        self.assertFalse([m for m in messages if "通話ごとの口を通らずに" in m], messages)   # 通話ごとの口が作った中身で記録する
        self.assertEqual(self.state(s, "tenant", s["tenant"]).stage, hr.TenantStage.STOPPED)
        opened = self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: tx.all(
            "SELECT event_id, cause::text AS cause FROM salescall.open_stop_events WHERE scope = 'tenant'"))
        self.assertEqual([r["cause"] for r in opened], ["program_trouble"])
        told = self.notes(s, "stopped")
        self.assertEqual([(r["cause"], r["stop_event_id"]) for r in told], [("program_trouble", opened[0]["event_id"])])
        self.assertEqual(self.reason(s, task=self.row(s)["task"]), "stopped")  # 新しい電話は止まる
        self.report_fixed("CL-1", "call_ai_malfunction")
        self.new_version()
        jobs.fixed_stops(self.ctx(s))
        self.assertEqual(self.state(s, "tenant", s["tenant"]).stage, hr.TenantStage.NORMAL)
        self.assertEqual(self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: tx.all(
            "SELECT 1 FROM salescall.open_stop_events WHERE scope = 'tenant'")), [])
        self.assertEqual(len(self.notes(s, "resumed")), 1)

    def test_a_mistake_is_recorded_and_asks_cl2(self):
        """17章の決まり (1)：表に無い組み合わせが来たら、入口は決めずに誤りとして記録し、直す担当（CL-2）へ修正依頼を出す（状態は変えない）。"""
        s = self.ready()
        self.round(s, FakeDriver(), Talk())
        self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: health.tested(tx, s["line"], "rang"))   # 元気の回線にテスト番号の答え
        got = owner_access().in_tenant(SYSTEM, s["tenant"], lambda tx: tx.one(
            "SELECT mistake->>'gap' AS gap, to_stage FROM salescall.state_moves WHERE tenant_id = salescall.cur_tenant() AND event = 'E6'"))
        self.assertEqual((got["gap"], got["to_stage"]), ("G-L6", "healthy"))
        self.assertEqual(len(self.requests("CL-2", "state_table")), 1)

    def test_the_system_test_number_is_given_to_every_contractor_without_their_own(self):
        """17-3-6 の【当】(a)（2026-10-29 管理者「一時的なtestcallは環境設定に追加」）：管理者がシステムの初めの値のテスト番号を入れた・
        変えたとき、自分の値を持たない契約者ごとに1回ずつ E14 を渡す（テスト番号を待つ回線が、すぐテスト番号を鳴らす頼みに替わる）。
        自分の値を持つ契約者には渡さない。"""
        a, b = self.ready(), self.ready()
        for s in (a, b):
            x, phone = self.bad_row(s)
            self.finish_others(s, x)
            self.round(s, phone)
            self.due(s)
            self.round(s, phone)
            self.assertEqual(self.state(s, "line", s["line"]).wait, hr.Wait.NO_NUMBER)
            self.set_ring_hours(s, ("00:00", "24:00"))
        self.numbered(b)                                                       # b は自分の値を持つ
        before = len(self.moves(b, event="E14"))
        self.addCleanup(lambda: self.w.a.in_system(ADMIN, lambda tx: settings.set_system_value(   # ほかの試験へ残さない（初めの値を無しに戻す）
            tx, "line.test_number", None, status="provisional", rationale="試験：片付け", review_when="試験")))
        self.w.a.in_system(ADMIN, lambda tx: settings.set_system_value(
            tx, "line.test_number", normalize(fictional_jp_fixed()).e164, status="provisional", rationale="試験：仮のテスト番号",
            review_when="試験"))
        self.assertEqual([m[4] for m in self.moves(a, event="E14")], [("L-13",)])
        self.assertEqual(self.state(a, "line", a["line"]).stage, hr.LineStage.CHECK_C)
        self.assertEqual(len(self.moves(b, event="E14")), before)
        with self.assertRaises(PermissionError):                               # 契約者の取引から、ほかの契約者へは移れない
            self.w.a.in_tenant(SYSTEM, a["tenant"], lambda tx: tx.as_tenant(b["tenant"]).__enter__())


class Guards(HealthBase):
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
        """2つの関数は、取引の契約者の欄だけを扱う（ほかの契約者の回線・行・契約者は断る。全体の取引でも扱えない）。自分の値を持たない
        契約者の一覧は、全体の取引の管理者かシステムの処理だけが読める。"""
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
        with self.assertRaises(Rejected):
            self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: tx.all(
                "SELECT * FROM salescall.tenants_without_own_value('line.test_number')"))
        self.assertIn(s["tenant"], [r["t"] for r in self.w.a.in_system(ADMIN, lambda tx: tx.all(
            "SELECT t FROM salescall.tenants_without_own_value('line.test_number') t"))])

    def test_the_table_reads_each_clients_own_values_from_any_transaction_of_its_contractor(self):
        """表が読む設定（0146 の (7)。core/settings.resolve_for_table の1か所）：契約者の取引でも、その依頼者の値で引く（行ごとの制限で
        見えない依頼者の値を、表の5つの決まりだけ読む）。ほかの契約者の依頼者・全体の取引からは読めない。決まりの名前の一覧は、表の
        SETTING_KEYS と同じ。⛔ 0145 の影：契約者の取引で引くと依頼者の値が見えず、システムの値で引いた。"""
        s, t = self.ready(), self.ready()
        self.tx(s, lambda tx: tx.execute(
            "INSERT INTO salescall.setting_values (key, scope, tenant_id, client_id, value, status, rationale, review_when, set_by)"
            " VALUES ('dial.failed_retry_days', 'client', %s, %s, '[1, 2]', 'decided', '試験', '試験', '試験')",
            (s["tenant"], s["client"])), OPERATOR)
        got = self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: settings.resolve_for_table(
            tx, hr.SETTING_KEYS.values(), s["client"]))
        self.assertEqual(got["dial.failed_retry_days"], [1, 2])
        self.assertEqual(set(got), set(hr.SETTING_KEYS.values()))
        names = owner_access().in_system(SYSTEM, lambda tx: tx.one(
            "SELECT prosrc FROM pg_proc WHERE proname = 'table_setting_layers'"))["prosrc"]
        self.assertEqual(sorted(re.findall(r"'([a-z_]+\.[a-z_.]+)'", names)), sorted(hr.SETTING_KEYS.values()))
        with self.assertRaises(Rejected):
            self.w.a.in_tenant(SYSTEM, t["tenant"], lambda tx: tx.all("SELECT * FROM salescall.table_setting_layers(%s)",
                                                                       (s["client"],)))
        with self.assertRaises(Rejected):
            self.w.a.in_system(ADMIN, lambda tx: tx.all("SELECT * FROM salescall.table_setting_layers(NULL)"))

    def test_a_row_awaiting_recovery_can_be_put_back_by_a_person_only(self):
        """休んだ行・復旧待ちの行を戻す守り（0146。行の欄で読む）：戻せるのは管理者か契約者だけ・休みか復旧待ちの行だけ（【当(7)】）。"""
        s = self.ready()
        with self.assertRaises(Rejected) as e:
            self.tx(s, lambda tx: tx.execute(
                "INSERT INTO salescall.task_trouble_restores (tenant_id, client_id, task_id, reason, restored_by, actor_kind)"
                " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, '試験', 'x', 'operator')", (s["task"],)), OPERATOR)
        self.assertEqual(e.exception.constraint, "task_trouble_restore_only_resting")
        self.trouble(s, task=s["task"])
        with self.assertRaises(Rejected) as e:                                 # システムの処理は戻せない（データベースの守り）
            self.tx(s, lambda tx: tx.execute(
                "INSERT INTO salescall.task_trouble_restores (tenant_id, client_id, task_id, reason, restored_by, actor_kind)"
                " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, '試験', 'x', 'operator')", (s["task"],)))
        self.assertEqual(e.exception.constraint, "task_trouble_restore_by_person")
        with self.assertRaises(Rejected):
            self.tx(s, lambda tx: troubles.restore(tx, s["task"], reason="試験"))   # システムは戻せない
        self.assertTrue(self.tx(s, lambda tx: troubles.restore(tx, s["task"], reason="試験：直ったことを確かめた"), OPERATOR))
        self.assertIn(("task", "E12", "await_recovery", "on_plan", ("R-7",), "by_hand_task"), self.moves(s, event="E12"))


class Failures(HealthBase):
    def test_a_failing_table_does_not_lose_the_event(self):
        """表を引く所が落ちたら、その出来事を黙って捨てない（⛔ 3段目の影は、落ちても今の作りを続けた）：電話が終わった所の取引は失敗し
        （状態の欄も試行のせいの欄も書かない）、締めの処理も締めない（締める前に出来事を渡す）。直れば、締めの仕事が1回だけ渡して締める。"""
        s = self.ready()
        with mock.patch.object(health.hr, "apply", side_effect=RuntimeError("試験：表を引く所が落ちた")):
            with self.assertRaises(RuntimeError):
                line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, FakeDriver(), Talk())
            jobs.settle_ready(self.ctx(s))
        called = owner_access().in_system(SYSTEM, lambda tx: tx.one(
            "SELECT attempt_id FROM salescall.attempts WHERE line_id = %s AND salescall.is_dialed(state)", (s["line"],)))["attempt_id"]
        self.assertIsNone(self.blame(s, called))
        self.assertFalse(self.tx(s, lambda tx: tx.one("SELECT salescall.is_settled(%s) AS d", (called,))["d"]))
        self.assertTrue(self.asked_supervisor(s))                              # 締めの仕事の誤りとして記録・修正依頼
        jobs.settle_ready(self.ctx(s))
        jobs.settle_ready(self.ctx(s))
        self.assertTrue(self.tx(s, lambda tx: tx.one("SELECT salescall.is_settled(%s) AS d", (called,))["d"]))
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
