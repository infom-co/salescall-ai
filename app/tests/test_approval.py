"""承認の部分（検証モード 10-2・送ってよい印 10-1 の3）の試験。本物の門・出口・締めの処理を通す。"""

from __future__ import annotations

import unittest

from salescall.core import approval, gate, quality_stage
from salescall.core.access import Rejected
from salescall.core.attempts import VerificationSummary
from tests.support import OPERATOR, SYSTEM
from tests.test_quality import Base

TAKEN = [("Q1", "confirmed", None)]


class Helpers(Base):
    """承認の試験の土台（試験は持たない）。"""
    def releases(self, s, task=None):
        return self.tx(s, lambda tx: tx.all("SELECT version_no, released_by FROM salescall.result_releases"
                                            " WHERE task_id = %s ORDER BY version_no", (task or s["task"],)))



class Released(Helpers):
    def test_first_stage_results_wait_until_the_stage_passes(self):
        s = self.fresh(stage_cap=2)
        self.verified_call(s, VerificationSummary(2, 0, 0), items=TAKEN)
        self.assertEqual(self.releases(s), [])                       # 最初の段：まだ送らない
        s2 = self.second_row(s)
        self.verified_call(s2, VerificationSummary(2, 0, 0), items=TAKEN)
        self.assertEqual(self.tx(s, lambda tx: quality_stage.current(tx, s["version"]))["stage_no"], 2)
        self.assertEqual(len(self.releases(s)), 1)                   # 段が進んだので、待っていた結果にもまとめて付く
        self.assertEqual(len(self.releases(s2)), 1)

    def test_results_of_a_stopped_version_are_not_released(self):
        s = self.fresh(stage_cap=1)
        self.verified_call(s, VerificationSummary(2, 2, 0), items=TAKEN)   # 食い違い 100% ⇒ 止まる
        self.assertEqual(self.releases(s), [])

    def test_later_stages_release_at_once(self):
        s = self.fresh(stage_cap=1)
        self.tx(s, lambda tx: quality_stage.open_stage(tx, s["version"], 2, 100, evidence="試験"), OPERATOR)
        self.verified_call(s, VerificationSummary(2, 0, 0), items=TAKEN)
        self.assertEqual(len(self.releases(s)), 1)


class HumanApproval(Helpers):
    def human(self, s):
        self.put_client(s, "approval.human", True)

    def test_dials_only_approved_rows(self):
        s = self.fresh(stage_cap=100)
        self.human(s)
        self.assertEqual(self.reason(s), "awaiting_approval")
        with self.assertRaises(Rejected):                            # システムの処理は承認できない
            self.tx(s, lambda tx: approval.approve_task(tx, s["task"]), SYSTEM)
        self.tx(s, lambda tx: approval.approve_task(tx, s["task"]), OPERATOR)
        self.assertIsInstance(self.claim(s), gate.Claim)

    def test_results_are_released_only_by_the_operator(self):
        s = self.fresh(stage_cap=100)
        self.tx(s, lambda tx: quality_stage.open_stage(tx, s["version"], 2, None, evidence="試験"), OPERATOR)
        self.human(s)
        self.tx(s, lambda tx: approval.approve_task(tx, s["task"]), OPERATOR)
        got = self.verified_call(s, VerificationSummary(2, 0, 0), items=TAKEN)
        self.assertEqual(self.releases(s), [])                       # 人が確かめるまで送らない
        fixed = self.tx(s, lambda tx: approval.correct_result(tx, s["task"], business_state="incomplete", complaint=False,
                                                              snapshot={"理由": "試験：人が聞き直した"}), OPERATOR)
        self.assertEqual([r["version_no"] for r in self.releases(s)], [fixed])
        kinds = self.tx(s, lambda tx: [r["kind"] for r in tx.all(
            "SELECT kind::text AS kind FROM salescall.task_results WHERE task_id = %s ORDER BY version_no", (s["task"],))])
        self.assertEqual(kinds, ["judged", "human"])                  # システムの判定は残す（誤判定率の材料）
        self.assertEqual(got["version_no"], 1)


if __name__ == "__main__":
    unittest.main()
