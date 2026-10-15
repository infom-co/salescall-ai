"""状態の移り変わりの処理の試験（DESIGN.md 4-1・4-2）：試行の状態の部品・結果の版・次の動きの記録。"""

from __future__ import annotations

import threading
import time
import unittest
from datetime import datetime, timedelta, timezone

from salescall.core import attempts, next_action, results
from salescall.core.tasks import TaskNotFound
from salescall.core.access import Rejected
from tests.support import SYSTEM, World


class AttemptParts(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.s = self.w.setup()

    def run_tx(self, work):
        return self.w.a.in_client(SYSTEM, self.s["tenant"], self.s["client"], work)

    def test_exit_dials_only_a_live_claim_once(self):
        aid = self.w.insert_claim(self.s)
        self.assertTrue(self.run_tx(lambda tx: attempts.start_dialing(tx, aid)))
        self.assertFalse(self.run_tx(lambda tx: attempts.start_dialing(tx, aid)))  # 2回目は発信しない

    def test_exit_refuses_expired_and_released_claims(self):
        aid = self.w.insert_claim(self.s, expires_in=timedelta(milliseconds=300))
        time.sleep(0.5)
        self.assertFalse(self.run_tx(lambda tx: attempts.start_dialing(tx, aid)))
        s2 = self.w.setup(); a2 = self.w.insert_claim(s2)
        self.w.a.in_client(SYSTEM, s2["tenant"], s2["client"], lambda tx: attempts.release(tx, [a2]))
        self.assertFalse(self.w.a.in_client(SYSTEM, s2["tenant"], s2["client"], lambda tx: attempts.start_dialing(tx, a2)))

    def test_release_touches_only_claims(self):
        aid = self.w.insert_claim(self.s)
        self.run_tx(lambda tx: attempts.start_dialing(tx, aid))
        self.assertEqual(self.run_tx(lambda tx: attempts.release(tx, [aid])), 0)  # 発信したものは取り消さない

    def test_move_and_verification(self):
        aid = self.w.insert_claim(self.s)
        self.run_tx(lambda tx: attempts.start_dialing(tx, aid))
        for st in ("ringing", "answered", "in_conversation", "ending", "ended"):
            row = self.run_tx(lambda tx: attempts.move(tx, aid, st))
        self.assertEqual((row["state"], row["verification"]), ("ended", "pending"))
        self.run_tx(lambda tx: attempts.record_verification(tx, aid, "done"))
        with self.assertRaises(Rejected):
            self.run_tx(lambda tx: attempts.record_verification(tx, aid, "failed"))

    def test_move_refuses_gate_and_exit_states(self):
        aid = self.w.insert_claim(self.s)
        for st in ("dialing", "released", "claimed"):
            with self.subTest(st=st), self.assertRaises(ValueError):
                self.run_tx(lambda tx: attempts.move(tx, aid, st))

    def test_move_of_other_clients_attempt_is_not_found(self):
        aid = self.w.insert_claim(self.s)
        other = self.w.setup(tenant=self.s["tenant"])
        with self.assertRaises(attempts.AttemptNotFound):
            self.w.a.in_client(SYSTEM, other["tenant"], other["client"], lambda tx: attempts.move(tx, aid, "ringing"))


class ResultVersions(unittest.TestCase):
    def test_open_until_first_result_then_latest(self):
        w = World(); s = w.setup()
        run = lambda work: w.a.in_client(SYSTEM, s["tenant"], s["client"], work)
        self.assertEqual(run(lambda tx: results.current_state(tx, s["task"])), "open")
        kw = dict(attempt_id=None, complaint=False, snapshot={"Q1": "empty"}, decided_by="judge")
        n1 = run(lambda tx: results.append(tx, s["task"], kind="judged", business_state="incomplete", judge_version="j1", **kw))
        n2 = run(lambda tx: results.append(tx, s["task"], kind="human", business_state="completed", judge_version=None, **kw))
        self.assertEqual((n1, n2), (1, 2))
        self.assertEqual(run(lambda tx: results.current_state(tx, s["task"])), "completed")

    def test_concurrent_appends_get_distinct_versions(self):
        w = World(); s = w.setup()
        start = threading.Barrier(6); errors = []

        def add():
            start.wait()
            try:
                w.a.in_client(SYSTEM, s["tenant"], s["client"], lambda tx: results.append(
                    tx, s["task"], kind="judged", business_state="open", attempt_id=None, judge_version="j1",
                    complaint=False, snapshot={}, decided_by="judge"))
            except Exception as e:
                errors.append(e)
        ths = [threading.Thread(target=add) for _ in range(6)]
        for t in ths: t.start()
        for t in ths: t.join()
        self.assertEqual(errors, [])
        nos = w.a.in_client(SYSTEM, s["tenant"], s["client"], lambda tx: [r["version_no"] for r in tx.all(
            "SELECT version_no FROM salescall.task_results WHERE task_id = %s ORDER BY version_no", (s["task"],))])
        self.assertEqual(nos, [1, 2, 3, 4, 5, 6])

    def test_other_clients_task_is_not_found(self):
        w = World(); s = w.setup(); other = w.setup(tenant=s["tenant"])
        with self.assertRaises(TaskNotFound):
            w.a.in_client(SYSTEM, other["tenant"], other["client"], lambda tx: results.current_state(tx, s["task"]))
        with self.assertRaises(TaskNotFound):
            w.a.in_client(SYSTEM, other["tenant"], other["client"], lambda tx: results.append(
                tx, s["task"], kind="judged", business_state="open", attempt_id=None, judge_version="j1",
                complaint=False, snapshot={}, decided_by="judge"))

    def test_result_attempt_must_belong_to_the_task(self):
        w = World(); s = w.setup(); s2 = w.setup(tenant=s["tenant"])
        # 同じ依頼主の、別のタスクの試行を指すことはできない
        k2 = w.contact(s["tenant"], s["client"], s2["phone"])
        t2 = w.task(s["tenant"], s["client"], s["run"], k2)
        aid = w.insert_claim(dict(s, task=t2, contact=k2, phone=s2["phone"], line=w.line(s["tenant"])))
        with self.assertRaises(Rejected):
            w.a.in_client(SYSTEM, s["tenant"], s["client"], lambda tx: results.append(
                tx, s["task"], kind="judged", business_state="open", attempt_id=aid, judge_version="j1",
                complaint=False, snapshot={}, decided_by="judge"))


class NextActions(unittest.TestCase):
    def test_concurrent_appends_get_distinct_seq(self):
        w = World(); s = w.setup()
        start = threading.Barrier(6); errors = []
        at = datetime(2030, 1, 7, 1, 0, tzinfo=timezone.utc)

        def add():
            start.wait()
            try:
                w.a.in_client(SYSTEM, s["tenant"], s["client"],
                              lambda tx: next_action.schedule_call(tx, s["task"], at, contact_id=s["contact"], cause="試験", promised=False))
            except Exception as e:
                errors.append(e)
        ths = [threading.Thread(target=add) for _ in range(6)]
        for t in ths: t.start()
        for t in ths: t.join()
        self.assertEqual(errors, [])
        self.assertEqual(w.a.in_client(SYSTEM, s["tenant"], s["client"],
                                       lambda tx: next_action.current(tx, s["task"]))["seq"], 6)

    def test_latest_is_current_and_exactly_one_of_call_or_end(self):
        w = World(); s = w.setup()
        run = lambda work: w.a.in_client(SYSTEM, s["tenant"], s["client"], work)
        self.assertIsNone(run(lambda tx: next_action.current(tx, s["task"])))
        at = datetime(2030, 1, 7, 1, 0, tzinfo=timezone.utc)
        run(lambda tx: next_action.schedule_call(tx, s["task"], at, contact_id=s["contact"], cause="取り込み", promised=False))
        run(lambda tx: next_action.end(tx, s["task"], "回数の上限", cause="判定のあと"))
        cur = run(lambda tx: next_action.current(tx, s["task"]))
        self.assertEqual((cur["seq"], cur["kind"], cur["end_reason"]), (2, "end", "回数の上限"))
        with self.assertRaises(ValueError):
            run(lambda tx: next_action._append(tx, s["task"], call_at=at, end_reason="x", cause="y"))


if __name__ == "__main__":
    unittest.main()
