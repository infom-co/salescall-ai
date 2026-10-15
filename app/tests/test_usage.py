"""利用量の台帳と予算（Q16）の試験。"""

from __future__ import annotations

from decimal import Decimal

from salescall.core import stops, usage
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


class Budget(SettleBase):
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
