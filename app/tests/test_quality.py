"""品質の段（Q22）と模擬通話の合格（DESIGN.md 10-1）の試験。本物の門・出口・締めの処理を通す。"""

from __future__ import annotations

import unittest

from salescall.core import attempts, business_rules, next_action, quality_stage, stops
from salescall.core.access import Rejected
from salescall.core.attempts import VerificationSummary
from salescall.core.phone import normalize
from salescall.core.quality_stage import MockResult
from tests.support import OPERATOR, SYSTEM, fictional_jp_fixed, rules_raw
from tests.test_settle import TALK, SettleBase, rep

GOOD = MockResult(calls=10, value_hits=40, value_total=40, judgment_hits=10, judgment_total=10, forbidden_speech=0,
                  evidence="試験：台本 v1")


class Base(SettleBase):
    def fresh(self, *, stage_cap=None):
        """段の無い版から始める行（模擬通話にまだ合格していない依頼主）。stage_cap を渡すと、その件数の最初の段を開く。"""
        s = self.ready()
        own_version = self.w.add_version(s["tenant"], s["client"], s["business"], 2)
        self.tx(s, lambda tx: tx.execute(
            "INSERT INTO salescall.questions (tenant_id, client_id, version_id, question_id, position, type_name, required)"
            " VALUES (%s, %s, %s, 'Q1', 1, 'datetime', true)", (s["tenant"], s["client"], own_version)), OPERATOR)
        self.tx(s, lambda tx: business_rules.save(tx, own_version, rules_raw()), OPERATOR)
        self.tx(s, lambda tx: tx.execute(
            "INSERT INTO salescall.run_versions (tenant_id, client_id, run_id, business_id, seq, version_id, assigned_by, reason)"
            " VALUES (%s, %s, %s, %s, 2, %s, '試験', '新しい版')", (s["tenant"], s["client"], s["run"], s["business"],
                                                                 own_version)), OPERATOR)
        s = dict(s, version=own_version)
        if stage_cap is not None:
            self.tx(s, lambda tx: quality_stage.open_stage(tx, own_version, 1, stage_cap, evidence="試験"), OPERATOR)
        return s

    def newer_version(self, s, seq):
        v = self.w.add_version(s["tenant"], s["client"], s["business"], seq)
        self.tx(s, lambda tx: tx.execute(
            "INSERT INTO salescall.questions (tenant_id, client_id, version_id, question_id, position, type_name, required)"
            " VALUES (%s, %s, %s, 'Q1', 1, 'datetime', true)", (s["tenant"], s["client"], v)), OPERATOR)
        self.tx(s, lambda tx: business_rules.save(tx, v, rules_raw()), OPERATOR)
        self.tx(s, lambda tx: tx.execute(
            "INSERT INTO salescall.run_versions (tenant_id, client_id, run_id, business_id, seq, version_id, assigned_by, reason)"
            " VALUES (%s, %s, %s, %s, %s, %s, '試験', '直した版')", (s["tenant"], s["client"], s["run"], s["business"], seq, v)),
            OPERATOR)
        return dict(s, version=v)

    def verified_call(self, s, summary, **kw):
        aid = self.call(s, verify=None, **kw)
        self.tx(s, lambda tx: attempts.record_verification(tx, aid, "done", summary=summary))
        return self.settle(s, aid)

    def second_row(self, s):
        phone = normalize(fictional_jp_fixed()).e164
        k = self.w.contact(s["tenant"], s["client"], phone)
        t = self.w.task(s["tenant"], s["client"], s["run"], k)
        self.tx(s, lambda tx: next_action.schedule_now(tx, t, cause="試験"))
        return dict(s, task=t, contact=k, phone=phone)

    def business_stops(self, s):
        return self.tx(s, lambda tx: stops.open_stops(tx, tenant_id=s["tenant"], client_id=s["client"],
                                                      business_id=s["business"]))


class Mock(Base):
    def test_first_business_of_a_client_waits_for_the_mock(self):
        s = self.fresh()
        self.assertEqual(self.tx(s, lambda tx: quality_stage.prepare(tx, s["version"])), "mock_needed")
        self.assertEqual(self.reason(s), "version_not_ready")
        for bad in (MockResult(9, 40, 40, 9, 9, 0, "本数が足りない"), MockResult(10, 30, 40, 10, 10, 0, "値が合わない"),
                    MockResult(10, 40, 40, 10, 10, 1, "言ってはいけないこと")):
            with self.subTest(bad.evidence):
                got = self.tx(s, lambda tx: quality_stage.record_mock(tx, s["version"], bad))
                self.assertEqual((got["passed"], got["outcome"]), (False, "failed"))
        self.assertIsNone(self.tx(s, lambda tx: quality_stage.current(tx, s["version"])))
        got = self.tx(s, lambda tx: quality_stage.record_mock(tx, s["version"], GOOD))
        self.assertEqual((got["passed"], got["outcome"]), (True, "opened"))
        stage = self.tx(s, lambda tx: quality_stage.current(tx, s["version"]))
        self.assertEqual((stage["stage_no"], stage["cap"]), (1, 20))
        newer = self.newer_version(s, 3)                                # 依頼主ごとに最初のみ：次の版はすぐ始める
        self.assertEqual(self.tx(s, lambda tx: quality_stage.prepare(tx, newer["version"])), "opened")


class Stages(Base):
    def test_forbidden_speech_stops_at_once_and_only_a_new_version_resumes(self):
        s = self.fresh(stage_cap=20)
        got = self.verified_call(s, VerificationSummary(3, 0, 1))      # 言ってはいけないことが1件
        self.assertEqual([r["cause"] for r in self.business_stops(s)], ["forbidden_speech"])
        self.tx(s, lambda tx: next_action.schedule_now(tx, s["task"], cause="試験：予定をいまに"))
        self.assertEqual(self.reason(s), "stopped")
        self.assertEqual(self.tx(s, lambda tx: quality_stage.prepare(tx, s["version"])), "already")
        again = self.tx(s, lambda tx: quality_stage.record_mock(tx, s["version"], GOOD))
        self.assertEqual(again["outcome"], "stopped_version")          # 止まった版そのものでは再開しない
        self.assertEqual(len(self.business_stops(s)), 1)
        newer = self.newer_version(s, 3)
        self.assertEqual(self.tx(s, lambda tx: quality_stage.prepare(tx, newer["version"])), "mock_needed")
        fixed = self.tx(s, lambda tx: quality_stage.record_mock(tx, newer["version"], GOOD))
        self.assertEqual(fixed["outcome"], "opened")
        self.assertEqual(self.business_stops(s), [])                   # 新しい版の合格で、自動で再開
        self.assertIsNotNone(got)

    def test_a_person_resume_measures_the_version_again(self):
        s = self.fresh(stage_cap=2)
        self.verified_call(s, VerificationSummary(3, 0, 1), items=[("Q1", "confirmed", None)])
        sid = self.business_stops(s)[0]["event_id"]
        why = self.tx(s, lambda tx: stops.explain(tx, sid))
        self.assertEqual(why["stop"]["cause"], "forbidden_speech")
        self.assertEqual(why["quality"]["metrics"]["forbidden_speech"]["hits"], 1)   # どの指標が外れたか
        self.assertEqual(len(why["held_results"]), 1)                 # 送らずに待っている結果（人が確かめて送れる）
        self.tx(s, lambda tx: stops.resume(tx, sid, reason="試験：場面を聞いて、言い方の問題ではないと判断"), OPERATOR)
        self.assertEqual(self.tx(s, lambda tx: quality_stage.used(tx, s["version"])), 0)   # 測り直し
        self.verified_call(self.second_row(s), VerificationSummary(2, 0, 0), items=[("Q1", "confirmed", None)])
        self.assertEqual(self.business_stops(s), [])                   # 前の1件で、また止まらない
        self.verified_call(self.second_row(s), VerificationSummary(2, 0, 1), items=[("Q1", "confirmed", None)])
        self.assertEqual([r["cause"] for r in self.business_stops(s)], ["forbidden_speech"])   # 解いたあとの分は数える

    def test_stage_end_advances_when_inside(self):
        s = self.fresh(stage_cap=1)
        self.verified_call(s, VerificationSummary(3, 0, 0), items=[("Q1", "confirmed", None)])
        stage = self.tx(s, lambda tx: quality_stage.current(tx, s["version"]))
        self.assertEqual((stage["stage_no"], stage["cap"]), (2, 100))
        ev = self.tx(s, lambda tx: tx.one("SELECT outcome, metrics FROM salescall.quality_evaluations WHERE version_id = %s"
                                          " ORDER BY eval_id DESC LIMIT 1", (s["version"],)))
        self.assertEqual(ev["outcome"], "advance")
        self.assertEqual(ev["metrics"]["ear_disagreement"]["total"], 3)          # 画面は記録を読む（数え直さない）
        notices = self.tx(s, lambda tx: tx.all("SELECT kind FROM salescall.notification_events WHERE kind = 'quality_stage'"
                                               " AND refs ->> 'version_id' = %s", (str(s["version"]),)))
        self.assertEqual(len(notices), 1)

    def test_stage_end_stops_when_a_ratio_is_outside(self):
        s = self.fresh(stage_cap=1)
        self.verified_call(s, VerificationSummary(4, 2, 0), items=[("Q1", "confirmed", None)])   # 食い違い 50%
        self.assertEqual([r["cause"] for r in self.business_stops(s)], ["quality_gate"])

    def test_ratios_wait_for_the_stage_end(self):
        s = self.fresh(stage_cap=5)
        self.verified_call(s, VerificationSummary(4, 2, 0), items=[("Q1", "confirmed", None)])   # まだ段の途中
        self.assertEqual(self.business_stops(s), [])

    def test_done_needs_the_numbers(self):
        s = self.fresh(stage_cap=5)
        aid = self.call(s, verify=None)
        with self.assertRaises(ValueError):
            self.tx(s, lambda tx: attempts.record_verification(tx, aid, "done", summary=None))
        with self.assertRaises(Rejected) as e:                         # 処理を通らずに書いても、データベースが拒む
            self.tx(s, lambda tx: tx.execute("UPDATE salescall.attempts SET verification = 'done' WHERE attempt_id = %s",
                                             (aid,)))
        self.assertEqual(e.exception.constraint, "attempts_verification_needs_summary")


if __name__ == "__main__":
    unittest.main()
