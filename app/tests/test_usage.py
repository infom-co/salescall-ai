"""利用量の台帳と予算（Q16）の試験。"""

from __future__ import annotations

from decimal import Decimal

from salescall.core import reward, stops, usage
from salescall.core.access import Rejected
from tests.support import OPERATOR, SYSTEM, owner_access
from tests.test_settle import SettleBase
from tests.test_watch import override, resume_open_system_stops


class Ledger(SettleBase):
    def entries(self, s):
        return self.tx(s, lambda tx: tx.all("SELECT source, kind, unit, quantity, amount_yen FROM salescall.usage_entries"
                                            " ORDER BY entry_id"))

    def test_line_estimate_is_written_when_settling_a_talked_call(self):
        s = self.ready()
        self.settle(s, self.call(s, items=[("Q1", "confirmed", None)]))
        got = self.entries(s)
        self.assertEqual([(e["source"], e["kind"], e["unit"]) for e in got], [("line", "estimated", "seconds")])
        self.assertEqual(got[0]["amount_yen"], 0)                     # Rakuten Link の国内通話は無料
        s2 = self.ready()
        self.settle(s2, self.call(s2, ["dialing", "no_answer"]))
        self.assertEqual(self.entries(s2), [])                        # 相手が出なかった通話は書かない

    def test_missing_price_is_not_guessed(self):
        s = self.ready()
        with self.assertRaises(usage.PriceMissing):
            self.tx(s, lambda tx: usage.record(tx, source="ai", kind="estimated", quantity=10, unit="seconds"))

    def test_frozen(self):
        s = self.ready()
        self.tx(s, lambda tx: usage.record(tx, source="ai", kind="measured", quantity=100, unit="input_tokens"))
        own = owner_access()
        for sql in ("UPDATE salescall.usage_entries SET amount_yen = 5", "DELETE FROM salescall.usage_entries"):
            with self.subTest(sql=sql), self.assertRaises(Rejected):
                own.in_client(OPERATOR, s["tenant"], s["client"], lambda tx: tx.execute(sql))


class Priced(SettleBase):
    """単価を変える試験の土台（試験は持たない）。"""
    def setUp(self):
        super().setUp()
        self.addCleanup(resume_open_system_stops)

    def priced(self, yen):
        """試験のあいだだけ、会話AIの入力の単価を変える（終わったら 0円の単価を足して戻す）。"""
        own = owner_access()
        put = lambda y: own.in_system(SYSTEM, lambda tx: usage.set_price(
            tx, source="ai", unit="input_tokens", yen_per_unit=Decimal(y),
            valid_from=tx.one("SELECT clock_timestamp() AS t")["t"], note="試験"))
        put(yen)
        self.addCleanup(put, 0)


class Budget(Priced):
    def test_client_over_its_daily_limit_is_stopped(self):
        s = self.ready()
        self.put_client(s, "budget.client_daily_yen", 5)
        self.priced("0.01")
        got = self.tx(s, lambda tx: usage.record(tx, source="ai", kind="measured", quantity=100, unit="input_tokens"))
        self.assertIsNone(got["stopped"])                              # 1円：上限の内側
        got = self.tx(s, lambda tx: usage.record(tx, source="ai", kind="measured", quantity=500, unit="input_tokens"))
        self.assertIsNotNone(got["stopped"])                           # 6円 ＞ 5円
        self.assertEqual(self.reason(s), "stopped")
        again = self.tx(s, lambda tx: usage.record(tx, source="ai", kind="measured", quantity=1, unit="input_tokens"))
        self.assertIsNone(again["stopped"])                            # 重ねて止めない

    def test_system_limit_and_new_day(self):
        s = self.ready()
        override(self, "budget.system_daily_yen", 0)
        self.priced("0.01")
        self.tx(s, lambda tx: usage.record(tx, source="ai", kind="measured", quantity=100, unit="input_tokens"))
        got = usage.tick(self.w.a, SYSTEM)
        self.assertIsNotNone(got["stopped"])
        self.assertEqual(self.reason(s), "stopped")
        # 前の日に止めた費用の停止は、日が替わったので解く（止めた時刻を前の日にして作る）
        own = owner_access()
        old = own.in_system(SYSTEM, lambda tx: tx.one(
            "INSERT INTO salescall.stop_events (scope, action, cause, actor, actor_kind, reason, at)"
            " VALUES ('system', 'stop', 'budget', %s, 'system', '試験：前の日', now() - interval '1 day')"
            " RETURNING event_id", (SYSTEM.name,)))["event_id"]
        resumed = usage.tick(self.w.a, SYSTEM)["resumed"]
        self.assertEqual(len(resumed), 1)                              # 今日止めた分は、まだ解かない
        self.assertNotIn(old, [r["event_id"] for r in self.w.a.in_system(SYSTEM, lambda tx: stops.open_stops(
            tx, tenant_id=None, client_id=None, business_id=None))])


class CostRatio(Priced):
    """報酬に対する費用の割合（2026-10-15 ユーザー決定：既定 30％・依頼主ごとに選べる。超えたら知らせるだけ。赤字なら止める）。"""
    def terms(self, s, *, days_ago=1, **yen):
        return self.tx(s, lambda tx: reward.set_terms(
            tx, reward.Terms(**{k: Decimal(v) for k, v in yen.items()}),
            valid_from=tx.one("SELECT now() - %s * interval '1 day' AS t", (days_ago,))["t"], note="試験"), OPERATOR)

    def talked(self, s):
        self.settle(s, self.call(s, items=[("Q1", "confirmed", None)]))     # 1本かけて、話せて、完了

    def spend(self, s, yen):
        return self.tx(s, lambda tx: usage.record(tx, source="ai", kind="measured", quantity=int(yen * 100),
                                                  unit="input_tokens"))      # 単価 0.01円

    def notices(self, s):
        return self.tx(s, lambda tx: tx.all("SELECT summary FROM salescall.notification_events WHERE kind = 'budget'"
                                            " AND event_key LIKE %s", (f"cost_ratio:{s['client']}:%",)))

    def client_stops(self, s):
        return [r["cause"] for r in self.tx(s, lambda tx: stops.open_stops(tx, tenant_id=s["tenant"], client_id=s["client"],
                                                                          business_id=None))]

    def test_each_form_is_counted_at_the_price_of_its_time(self):
        s = self.ready()
        self.terms(s, per_completed_yen=100, per_dial_yen=1, per_talk_yen=10)
        self.talked(s)
        self.terms(s, days_ago=0, per_completed_yen=1000, per_dial_yen=1000, per_talk_yen=1000)   # あとから変えた
        m = self.tx(s, reward.month)
        self.assertEqual({k: (v["count"], v["yen"]) for k, v in m["breakdown"].items() if k != "monthly"},
                         {"completed": (1, 100), "dial": (1, 1), "talk": (1, 10)})    # 過去の分は、そのときの単価
        self.assertEqual((m["revenue"], m["cost"], m["deficit"]), (111, 0, False))
        with self.assertRaises(ValueError):
            self.terms(s, days_ago=0)                                  # どれも 0 円の決まりは足せない

    def test_monthly_is_prorated(self):
        s = self.ready()
        self.terms(s, days_ago=40, monthly_yen=3000)
        got = self.tx(s, reward.month)["breakdown"]["monthly"]["yen"]
        self.assertTrue(0 < got <= 3000)

    def test_over_the_ratio_only_notifies_and_each_client_can_choose(self):
        s = self.ready()
        self.put_client(s, "budget.deficit_min_dials", 1)
        self.terms(s, per_talk_yen=10)
        self.talked(s)
        self.priced("0.01")
        self.put_client(s, "budget.max_cost_ratio", 0.6)
        self.spend(s, 5)                                               # 50％：この依頼主の上限 60％ の内側
        self.assertEqual(self.notices(s), [])
        self.put_client(s, "budget.max_cost_ratio", 0.3)
        got = self.spend(s, 0.01)
        self.assertIsNone(got["stopped"])                              # 知らせるだけ
        self.assertEqual(len(self.notices(s)), 1)
        self.spend(s, 0.01)
        self.assertEqual(len(self.notices(s)), 1)                      # 1日1回
        self.assertEqual(self.client_stops(s), [])

    def test_deficit_stops_and_resumes_when_terms_are_fixed(self):
        s = self.ready()
        self.put_client(s, "budget.deficit_min_dials", 1)
        self.terms(s, per_talk_yen=10)
        self.talked(s)
        self.priced("0.01")
        self.assertIsNotNone(self.spend(s, 15)["stopped"])             # 費用 15円 ＞ 報酬 10円
        self.assertEqual(self.client_stops(s), ["deficit"])
        sid = self.tx(s, lambda tx: stops.open_stops(tx, tenant_id=s["tenant"], client_id=s["client"],
                                                     business_id=None))[0]["event_id"]
        self.assertEqual(self.tx(s, lambda tx: stops.explain(tx, sid))["cause"]["meaning"], stops.CAUSES["deficit"].meaning)
        usage.tick(self.w.a, SYSTEM)
        self.assertEqual(self.client_stops(s), ["deficit"])            # 見守りが見直しても、赤字のまま
        self.terms(s, per_talk_yen=10, monthly_yen=10 ** 9)            # 報酬の決まりを直した
        usage.tick(self.w.a, SYSTEM)
        self.assertEqual(self.client_stops(s), [])                     # 赤字でなくなったので、自動で解く

    def test_no_terms_is_a_deficit_once_enough_dials(self):
        s = self.ready()
        self.priced("0.01")
        self.talked(s)
        self.spend(s, 1)
        self.assertEqual(self.client_stops(s), [])                     # 本数が足りないうちは判断しない（既定 30本）
        self.put_client(s, "budget.deficit_min_dials", 1)
        self.spend(s, 1)
        self.assertEqual(self.client_stops(s), ["deficit"])
        reason = self.tx(s, lambda tx: tx.one("SELECT reason FROM salescall.stop_events WHERE cause = 'deficit'"
                                              " ORDER BY event_id DESC LIMIT 1"))["reason"]
        self.assertIn("報酬の決まりがありません", reason)

    def test_terms_are_frozen(self):
        s = self.ready()
        self.terms(s, per_dial_yen=1)
        own = owner_access()
        for sql in ("UPDATE salescall.reward_terms SET per_dial_yen = 5", "DELETE FROM salescall.reward_terms"):
            with self.subTest(sql=sql), self.assertRaises(Rejected):
                own.in_client(OPERATOR, s["tenant"], s["client"], lambda tx: tx.execute(sql))
