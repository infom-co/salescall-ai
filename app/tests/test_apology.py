"""お詫びの電話（2026-10-16 ユーザー決定：A。DESIGN.md 10-1-2）の試験。本物の門・出口・締めの処理を通す。"""

from __future__ import annotations

import unittest
from dataclasses import replace

from salescall.core import apology, attempts, gate, quality_stage, reward, stops
from salescall.core.access import Rejected
from salescall.core.attempts import VerificationSummary
from tests.support import OPERATOR, SYSTEM, owner_access
from tests.test_approval import TAKEN, Helpers
from tests.test_settle import rep

SAID = VerificationSummary(2, 0, 1)        # 言ってはいけないことを1件言った


class Apology(Helpers):
    def incident(self, s):
        """言ってはいけないことを言った通話（その行は終わり、業務は止まる）。その試行を返す。"""
        self.verified_call(s, SAID, items=TAKEN)
        return self.tx(s, lambda tx: tx.one("SELECT attempt_id FROM salescall.attempts WHERE task_id = %s"
                                            " ORDER BY claimed_at DESC LIMIT 1", (s["task"],)))["attempt_id"]

    def order(self, s, aid, actor=OPERATOR):
        return self.tx(s, lambda tx: apology.order(tx, aid, wrong_text="初月無料", correct_text="初月は半額"), actor)

    def apology_call(self, s, **report):
        aid = self.call(s, report=replace(rep(), **report))
        return self.settle(s, aid)

    def results(self, s):
        return self.tx(s, lambda tx: tx.one("SELECT count(*) AS n FROM salescall.task_results WHERE task_id = %s",
                                            (s["task"],)))["n"]

    def test_only_a_person_orders_and_only_after_forbidden_speech(self):
        s = self.fresh(stage_cap=20)
        aid = self.incident(s)
        with self.assertRaises(Rejected):
            self.order(s, aid, actor=SYSTEM)                          # システムは決めない（管理者が決める）
        clean = self.fresh(stage_cap=20)                               # 言ってはいけないことを言っていない通話（別の業務）
        self.verified_call(clean, VerificationSummary(1, 0, 0), items=TAKEN)
        clean_aid = self.tx(clean, lambda tx: tx.one("SELECT attempt_id FROM salescall.attempts WHERE task_id = %s",
                                                     (clean["task"],)))["attempt_id"]
        with self.assertRaises(Rejected) as e:
            self.order(clean, clean_aid)
        self.assertEqual(e.exception.constraint, "apology_order_after_forbidden")
        self.order(s, aid)
        got = self.tx(s, lambda tx: apology.for_task(tx, s["task"]))
        self.assertEqual(got["text"], "先ほどのお電話で、誤って「初月無料」というご案内をしてしまいましたが、正しくは「初月は半額」"
                                      "となります。不正確なご案内をしてしまいまして、誠に申し訳ございませんでした。")

    def test_delivered_ends_the_row_without_changing_the_result(self):
        s = self.fresh(stage_cap=20)
        self.order(s, self.incident(s))
        self.assertEqual([r["cause"] for r in self.business_stops(s)], ["forbidden_speech"])
        claim = self.claim(s)
        self.assertIsInstance(claim, gate.Claim)                       # 業務の品質の停止では、お詫びは止めない
        self.assertEqual(claim.purpose, "apology")
        self.tx(s, lambda tx: attempts.release(tx, [claim.attempt_id]))   # 予約は戻し、下で出口を通してかける
        before = self.results(s)
        got = self.apology_call(s, apology_delivered=True)
        self.assertEqual((got["decision"].kind, got["decision"].end_reason), ("end", "apology_done"))
        self.assertEqual(self.results(s), before)                      # 業務の結果は変えない
        notices = self.tx(s, lambda tx: tx.all("SELECT summary FROM salescall.notification_events WHERE kind = 'apology'"
                                               " AND refs ->> 'task_id' = %s", (str(s["task"]),)))
        self.assertEqual([n["summary"] for n in notices], ["お詫びを伝えました。"])
        self.assertEqual(self.tx(s, lambda tx: quality_stage.used(tx, s["version"])), 1)    # 段の件数に数えない
        m = self.tx(s, lambda tx: reward.month(tx, confidence=0.95, gap_max=300))
        self.assertEqual(m["breakdown"]["dial"]["count"], 1)           # 報酬にも数えない（こちらの誤りのため）

    def test_not_delivered_tries_another_band_up_to_the_limit(self):
        s = self.fresh(stage_cap=20)
        self.order(s, self.incident(s))
        got = self.apology_call(s, apology_delivered=False)
        self.assertEqual((got["decision"].kind, got["decision"].target.purpose), ("call", "apology"))   # 別の時間帯に
        t = self.fresh(stage_cap=20)
        self.put_client(t, "retry.apology_max", 1)
        self.order(t, self.incident(t))
        got = self.apology_call(t)                                     # 報告に無い ⇒ 伝えていない側に倒す
        self.assertEqual((got["decision"].kind, got["decision"].end_reason), ("end", "apology_unreached"))

    def test_refusal_ends_and_registers_dnc(self):
        s = self.fresh(stage_cap=20)
        self.order(s, self.incident(s))
        got = self.apology_call(s, do_not_call=True)
        self.assertEqual(got["decision"].end_reason, "do_not_call")
        self.assertEqual(self.tx(s, lambda tx: tx.one("SELECT count(*) AS n FROM salescall.dnc_entries"
                                                      " WHERE phone_e164 = %s", (s["phone"],)))["n"], 1)

    def test_other_stops_still_stop_it(self):
        s = self.fresh(stage_cap=20)
        self.order(s, self.incident(s))
        self.tx(s, lambda tx: stops.stop(tx, scope="business", cause="manual", reason="試験：人の緊急停止",
                                         tenant_id=s["tenant"], client_id=s["client"], business_id=s["business"]), OPERATOR)
        self.assertEqual(self.reason(s), "stopped")                    # 人の緊急停止では止める

    def test_frozen(self):
        s = self.fresh(stage_cap=20)
        self.order(s, self.incident(s))
        own = owner_access()
        for sql in ("UPDATE salescall.apology_orders SET correct_text = 'x'", "DELETE FROM salescall.apology_orders"):
            with self.subTest(sql=sql), self.assertRaises(Rejected):
                own.in_client(OPERATOR, s["tenant"], s["client"], lambda tx: tx.execute(sql))


if __name__ == "__main__":
    unittest.main()
