"""誤判定率の正解と抜き取りの試験（Intent.md 16-3・11-4 第2版。DESIGN.md 10-3。core/measure.py・表の作り 0066）。

- 正解を入れるのは measure.record_check の1か所（管理者か契約者だけ）。検証モードの承認・直したことも正解になる。
- 抜き取りは業務ごと・週ごとに1回、前の週の、お客様と話せた本番の通話から無作為に。母数・本数・乱数の種を残す。
- 正解が付いたら、その通話の「測り終わるまで消さない」印を外す。本番で気づいて直したものは、誤判定率に混ぜない。
⚠️ 中身は架空のもの。
"""

from __future__ import annotations

import random
import unittest
from datetime import timedelta

from salescall.core import approval, measure, quality_stage
from salescall.core.access import Rejected
from tests.support import OPERATOR, SYSTEM, owner_access
from tests.test_settle import SettleBase

TAKEN = [("Q1", "confirmed", None)]


class Base(SettleBase):
    def holds(self, s, aid):
        return self.tx(s, lambda tx: [(r["reason"], r["released_at"] is not None) for r in tx.all(
            "SELECT reason, released_at FROM salescall.recording_holds WHERE attempt_id = %s ORDER BY reason", (aid,))])

    def verified_mode(self, s):
        self.put_client(s, "approval.human", True)
        self.tx(s, lambda tx: approval.approve_task(tx, s["task"]), OPERATOR)
        self.tx(s, lambda tx: quality_stage.open_stage(tx, s["version"], 1, None, evidence="試験"), OPERATOR)

    def rates(self, s, **kw):
        return self.tx(s, lambda tx: measure.rates(tx, s["business"], **kw))

    def jst_now(self, s):
        return self.tx(s, lambda tx: tx.one("SELECT salescall.jst(now()) AS t")["t"])


class Verification(Base):
    def test_approving_and_correcting_are_the_truth_and_release_the_recording(self):
        """検証モード：契約者の承認＝判定どおりが正解、直した＝直した中身が正解（Intent.md 16-3）。正解が付いたら録音の印を外す。"""
        s, t = self.ready(), self.ready(tenant=None)
        for x in (s, t):
            self.verified_mode(x)
        a = self.call(s, items=TAKEN)
        self.settle(s, a)
        self.assertEqual(self.holds(s, a), [("verification", False)])     # 測り終わるまで消さない
        self.tx(s, lambda tx: approval.approve_result(tx, s["task"], 1), OPERATOR)
        self.assertEqual(self.holds(s, a), [("verification", True)])      # 正解が付いた＝測り終わった
        self.assertEqual({k: self.rates(s)[k] for k in ("checked", "wrong")}, {"checked": 1, "wrong": 0})
        b = self.call(t, items=TAKEN)
        self.settle(t, b)
        self.tx(t, lambda tx: approval.correct_result(tx, t["task"], checked_version=1, business_state="incomplete",
                                                      wrong_items=["Q1"], complaint=False, snapshot={"試験": "聞き直した"}),
                OPERATOR)
        got = self.rates(t)
        self.assertEqual({k: got[k] for k in ("checked", "wrong", "state_wrong", "items_wrong")},
                         {"checked": 1, "wrong": 1, "state_wrong": 1, "items_wrong": 1})
        self.assertEqual(self.holds(t, b), [("verification", True)])
        with self.assertRaises(LookupError):                              # 直すのは、システムが判定した版だけ
            self.tx(t, lambda tx: approval.correct_result(tx, t["task"], checked_version=2, business_state="completed",
                                                          wrong_items=[], complaint=False, snapshot={}), OPERATOR)

    def test_only_a_person_checks_and_only_system_versions(self):
        s = self.ready()
        a = self.call(s, items=TAKEN)
        self.settle(s, a)
        with self.assertRaises(Rejected):                                 # システムの処理は正解を付けない
            self.tx(s, lambda tx: measure.record_check(tx, s["task"], 1, truth_state="completed", wrong_items=[],
                                                       source="sample"), SYSTEM)
        self.tx(s, lambda tx: approval.correct_result(tx, s["task"], checked_version=1, business_state="declined",
                                                      wrong_items=[], complaint=False, snapshot={}), OPERATOR)
        with self.assertRaises(Rejected):                                 # 人の版には正解を付けない
            self.tx(s, lambda tx: tx.execute(
                "INSERT INTO salescall.judgement_checks (tenant_id, client_id, task_id, version_no, attempt_id, source,"
                " truth_state, checked_by) VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, 2, %s, 'sample',"
                " 'completed', 'x')", (s["task"], a)), OPERATOR)
        for sql in ("UPDATE salescall.judgement_checks SET truth_state = 'completed'", "DELETE FROM salescall.judgement_checks"):
            with self.subTest(sql=sql), self.assertRaises(Rejected):     # 正解は、表の持ち主でも書き換えない（付け直すなら足す）
                owner_access().in_client(OPERATOR, s["tenant"], s["client"], lambda tx: tx.execute(sql))

    def test_production_corrections_are_kept_apart_from_the_rate(self):
        """本番で契約者が気づいて直したものは、気づいたものだけ＝偏った材料なので、誤判定率に混ぜない（記録はする）。"""
        s = self.ready()
        a = self.call(s, items=TAKEN)
        self.settle(s, a)
        self.tx(s, lambda tx: approval.correct_result(tx, s["task"], checked_version=1, business_state="declined",
                                                      wrong_items=[], complaint=False, snapshot={}), OPERATOR)
        self.assertEqual(self.rates(s)["checked"], 0)
        self.assertEqual(self.rates(s, sources=("correction",))["wrong"], 1)


class Samples(Base):
    def next_tuesday(self, s):
        now = self.jst_now(s)
        return measure.week_start(now.date()), now.replace(hour=0, minute=0, second=0, microsecond=0) \
            + timedelta(days=8 - now.weekday())

    def test_a_week_is_drawn_once_at_random_and_recorded(self):
        s = self.ready()
        a = self.call(s, items=TAKEN)
        self.settle(s, a)
        week, tuesday = self.next_tuesday(s)
        self.assertEqual(self.tx(s, lambda tx: measure.draw(tx, now=tuesday - timedelta(hours=1))), [])   # 1日あけてから
        drawn = self.tx(s, lambda tx: measure.draw(tx, now=tuesday))
        self.assertEqual(len(drawn), 1)
        d = self.tx(s, lambda tx: tx.one("SELECT * FROM salescall.sample_draws WHERE draw_id = %s", (drawn[0],)))
        self.assertEqual((d["week_start"], d["pool_size"], d["wanted"], d["drawn"]), (week, 1, 10, 1))
        pool = [a]                                                           # 同じ種で、同じ選び方をやり直せる
        self.assertEqual(sorted(random.Random(d["seed"]).sample(pool, 1)), [a])
        self.assertEqual(self.holds(s, a), [("sample", False)])
        notes = self.tx(s, lambda tx: tx.all("SELECT kind FROM salescall.notification_events WHERE event_key = %s",
                                             (f"sample:{drawn[0]}",)))
        self.assertEqual([n["kind"] for n in notes], ["sample_ready"])
        self.assertEqual(self.tx(s, lambda tx: measure.draw(tx, now=tuesday)), [])   # 同じ週は1回だけ
        self.tx(s, lambda tx: measure.record_check(tx, s["task"], 1, truth_state="completed", wrong_items=[],
                                                   source="sample"), OPERATOR)
        self.assertEqual(self.holds(s, a), [("sample", True)])
        self.assertEqual({k: self.rates(s)[k] for k in ("checked", "wrong")}, {"checked": 1, "wrong": 0})

    def test_verification_calls_and_calls_without_a_conversation_are_not_drawn(self):
        s, t = self.ready(), self.ready()
        self.verified_mode(t)
        self.settle(s, self.call(s, ["dialing", "ringing", "no_answer"]))   # お客様と話していない
        self.settle(t, self.call(t, items=TAKEN))                           # 検証モード（すべて確かめる）
        _, tuesday = self.next_tuesday(s)
        for x in (s, t):
            drawn = self.tx(x, lambda tx: measure.draw(tx, now=tuesday))
            pool = self.tx(x, lambda tx: tx.one("SELECT pool_size, drawn FROM salescall.sample_draws WHERE draw_id = %s",
                                                (drawn[0],)))
            self.assertEqual((pool["pool_size"], pool["drawn"]), (0, 0))


class UpperBound(unittest.TestCase):
    def test_few_checks_do_not_claim_a_low_rate(self):
        self.assertAlmostEqual(measure.upper_bound(0, 60), 0.0487, places=3)   # 60本で誤り0本 ⇒ 5%より低いと言える
        self.assertAlmostEqual(measure.upper_bound(0, 10), 0.2589, places=3)   # 10本では、まだ26%まで否定できない
        self.assertAlmostEqual(measure.upper_bound(1, 100), 0.0466, places=3)
        self.assertEqual(measure.upper_bound(3, 3), 1.0)


if __name__ == "__main__":
    unittest.main()
