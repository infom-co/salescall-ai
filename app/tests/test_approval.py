"""承認の部分（検証モード 10-2・送ってよい印 10-1 の3）の試験。本物の門・出口・締めの処理を通す。"""

from __future__ import annotations

import unittest

from salescall.core import approval, gate, quality_stage
from salescall.core.access import Rejected
from salescall.core.attempts import VerificationSummary
from tests.support import OPERATOR, SYSTEM, said
from tests.test_quality import Base

TAKEN = [("Q1", "confirmed", None)]


class Helpers(Base):
    """承認の試験の土台（試験は持たない）。"""
    def releases(self, s, task=None):
        return self.tx(s, lambda tx: tx.all("SELECT version_no, released_by FROM salescall.result_releases"
                                            " WHERE task_id = %s ORDER BY version_no", (task or s["task"],)))



class Released(Helpers):
    def test_first_stage_results_are_confirmed_without_waiting_for_the_stage(self):
        """本格稼働（フルオート）では、最初の段でも、判定した結果をそのまま確定する（2026-10-16 ユーザー：検証時は契約者が
        確かめ、本格稼働してから依頼者ごとの形で渡す。前は段が埋まるまで待たせ、行が少ないと、いつまでも確定しなかった）。"""
        s = self.fresh(stage_cap=20)
        self.verified_call(s, VerificationSummary(2, 0, 0), items=TAKEN)
        self.assertEqual(self.tx(s, lambda tx: quality_stage.current(tx, s["version"]))["stage_no"], 1)
        self.assertEqual(len(self.releases(s)), 1)

    def test_a_quality_stop_stops_new_calls_but_results_stay_as_they_are(self):
        s = self.fresh(stage_cap=1)
        self.verified_call(s, VerificationSummary(2, 2, 0), items=TAKEN)   # 食い違い 100% ⇒ 止まる（新しい発信を）
        self.assertEqual(len(self.releases(s)), 1)                         # 取れた結果は、ふつうどおり確定する

    def test_later_stages_release_at_once(self):
        s = self.fresh(stage_cap=1)
        self.tx(s, lambda tx: quality_stage.open_stage(tx, s["version"], 2, 100, evidence="試験"), OPERATOR)
        self.verified_call(s, VerificationSummary(2, 0, 0), items=TAKEN)
        self.assertEqual(len(self.releases(s)), 1)


class ForbiddenSpeech(Helpers):
    def test_the_row_ends_and_the_admin_is_told(self):
        s = self.fresh(stage_cap=1)
        self.tx(s, lambda tx: quality_stage.open_stage(tx, s["version"], 2, 100, evidence="試験"), OPERATOR)
        got = self.verified_call(s, said(), items=TAKEN)
        self.assertEqual((got["decision"].kind, got["decision"].end_reason), ("end", "forbidden_speech"))   # 判定に関係なく終える
        notices = self.tx(s, lambda tx: tx.all("SELECT client_id FROM salescall.notification_events"
                                               " WHERE kind = 'forbidden_speech' AND refs ->> 'task_id' = %s",
                                               (str(s["task"]),)))
        self.assertEqual([n["client_id"] for n in notices], [s["client"]])   # 契約者（＝管理者）の知らせ
        self.assertEqual(len(self.releases(s)), 1)                   # 取れた結果は、ふつうどおり（管理者も契約者も待たない）


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
        self.assertEqual(self.releases(s), [])                       # 契約者が確かめるまで送らない
        fixed = self.tx(s, lambda tx: approval.correct_result(tx, s["task"], checked_version=1, business_state="incomplete",
                                                              wrong_items=["Q1"], complaint=False,
                                                              snapshot={"理由": "試験：契約者が聞き直した"}), OPERATOR)
        self.assertEqual([r["version_no"] for r in self.releases(s)], [fixed])
        kinds = self.tx(s, lambda tx: [r["kind"] for r in tx.all(
            "SELECT kind::text AS kind FROM salescall.task_results WHERE task_id = %s ORDER BY version_no", (s["task"],))])
        self.assertEqual(kinds, ["judged", "human"])                  # システムの判定は残す（誤判定率の材料）
        self.assertEqual(got["version_no"], 1)


if __name__ == "__main__":
    unittest.main()
