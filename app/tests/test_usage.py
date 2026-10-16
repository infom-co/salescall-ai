"""利用量の台帳と予算（Q16）の試験。"""

from __future__ import annotations

import time
from decimal import Decimal

from salescall.core import approval, gate, reward, stops, usage
from salescall.core.access import Rejected
from tests.support import ADMIN, OPERATOR, SYSTEM, owner_access
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
        self.assertEqual(self.entries(s2), [])                        # お客様が出なかった通話は書かない

    def test_a_missing_price_is_a_notice_not_a_stop(self):
        """単価が無くても止めない（お客様に影響しない）。量だけ記録し、金額は空（推測で 0円にしない）。契約者へ「設定がされて
        いません」を知らせる（2026-10-16 ユーザー）。"""
        s = self.ready()
        got = self.tx(s, lambda tx: usage.record(tx, source="ai", service="gemini-3.8-live", tier="free", kind="estimated",
                                                 quantity=10, unit="seconds"))
        self.assertEqual((got["amount_yen"], got["stopped"]), (None, None))
        kinds = self.tx(s, lambda tx: [r["kind"] for r in tx.all("SELECT kind FROM salescall.notification_events"
                                                                 " WHERE event_key LIKE 'price_missing:%'")])
        self.assertEqual(kinds, ["setting_missing"])
        self.assertEqual(self.tx(s, lambda tx: reward.month(tx, confidence=0.95, gap_max=300))["unpriced_entries"], 1)

    def test_frozen(self):
        s = self.ready()
        self.tx(s, lambda tx: usage.record(tx, source="ai", service="gemini-3.8-live", tier="free", kind="measured", quantity=100, unit="input_tokens"))
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
            tx, source="ai", service="gemini-3.8-live", tier="free", unit="input_tokens", yen_per_unit=Decimal(y),
            valid_from=tx.one("SELECT clock_timestamp() AS t")["t"], note="試験"))
        put(yen)
        self.addCleanup(put, 0)


class PricesPerContractor(Priced):
    def test_the_contractors_price_wins_and_tiers_are_separate(self):
        """費用の単価は契約者ごと（2026-10-16 ユーザー：「契約者　使用するAIのAPIと回線を選べます」「APIも無料枠と有料枠が選択可能」）。
        契約者が入れた単価が初めの値より強く、ほかの契約者には効かない。無料枠と有料枠は別の単価。"""
        s, other = self.ready(), self.ready()
        put = lambda t, tier, y, actor=OPERATOR: self.w.a.in_tenant(actor, t, lambda tx: usage.set_price(
            tx, source="ai", service="gemini-3.8-live", tier=tier, unit="input_tokens", yen_per_unit=Decimal(y),
            valid_from=tx.one("SELECT clock_timestamp() AS t")["t"], note="試験"))
        put(s["tenant"], "paid", "0.5")
        with self.assertRaises(Rejected):
            put(s["tenant"], "paid", "0.1", SYSTEM)                       # システムの処理は単価を入れない
        rec = lambda x, tier: self.tx(x, lambda tx: usage.record(
            tx, source="ai", service="gemini-3.8-live", tier=tier, kind="measured", quantity=10, unit="input_tokens"))
        self.assertEqual(rec(s, "paid")["amount_yen"], Decimal("5.0"))
        self.assertEqual(rec(s, "free")["amount_yen"], 0)                 # 無料枠は初めの値（0円）
        with self.assertRaises(usage.PriceMissing):
            self.tx(other, lambda tx: usage.price_for(tx, source="ai", service="gemini-3.8-live", tier="paid",
                                                      unit="input_tokens"))   # ほかの契約者の単価は使わない
        seen = self.tx(other, lambda tx: tx.one("SELECT count(*) AS n FROM salescall.price_versions WHERE tenant_id = %s",
                                                (s["tenant"],)))["n"]
        self.assertEqual(seen, 0)                                         # ほかの契約者の単価は、データベースが見せない


class Budget(Priced):
    def ample(self, s):
        """赤字で止まらないよう、十分な月額の報酬を入れる（1日の上限だけを確かめる）。"""
        self.tx(s, lambda tx: reward.set_terms(tx, reward.Terms(monthly_yen=Decimal(10 ** 12)),
                                               valid_from=tx.one("SELECT now() - interval '40 days' AS t")["t"],
                                               note="試験"), OPERATOR)

    def budget_notices(self, s, like):
        return self.w.a.in_system(ADMIN, lambda tx: tx.all(
            "SELECT event_key FROM salescall.notification_events WHERE kind = 'budget' AND event_key LIKE %s", (like,))) \
            if s is None else self.tx(s, lambda tx: tx.all(
                "SELECT event_key FROM salescall.notification_events WHERE kind = 'budget' AND event_key LIKE %s", (like,)))

    def test_client_over_its_daily_limit_is_only_noticed(self):
        """依頼者の1日の上限を超えても止めない。注意を知らせるだけ（1日1回。2026-10-16 ユーザー：「注意喚起のみ」）。"""
        s = self.ready()
        self.ample(s)
        self.put_client(s, "budget.client_daily_yen", 5)
        self.priced("0.01")
        rec = lambda q: self.tx(s, lambda tx: usage.record(tx, source="ai", service="gemini-3.8-live", tier="free",
                                                           kind="measured", quantity=q, unit="input_tokens"))
        rec(100)
        self.assertEqual(self.budget_notices(s, f"client_daily:{s['client']}:%"), [])   # 1円：上限の内側
        self.assertIsNone(rec(500)["stopped"])                          # 6円 ＞ 5円：止めない
        rec(1)
        self.assertEqual(len(self.budget_notices(s, f"client_daily:{s['client']}:%")), 1)   # 1日1回
        self.assertIsInstance(self.claim(s), gate.Claim)                 # 発信は続く

    def test_system_limit_is_only_noticed_to_the_admin(self):
        s = self.ready()
        self.ample(s)
        override(self, "budget.system_daily_yen", 0)
        self.priced("0.01")
        self.tx(s, lambda tx: usage.record(tx, source="ai", service="gemini-3.8-live", tier="free", kind="measured",
                                           quantity=100, unit="input_tokens"))
        self.assertEqual(usage.tick(self.w.a, SYSTEM)["stopped"], [])
        self.assertEqual(len(self.budget_notices(None, "admin:system_daily:%")), 1)
        self.assertIsInstance(self.claim(s), gate.Claim)


class CostRatio(Priced):
    """報酬に対する費用の割合（2026-10-15 ユーザー決定：既定 30％・依頼者ごとに選べる。超えたら知らせるだけ。赤字なら止める）。"""
    def terms(self, s, *, days_ago=1, **yen):
        return self.tx(s, lambda tx: reward.set_terms(
            tx, reward.Terms(**{k: Decimal(v) for k, v in yen.items()}),
            valid_from=tx.one("SELECT now() - %s * interval '1 day' AS t", (days_ago,))["t"], note="試験"), OPERATOR)

    def talked(self, s):
        aid = self.call(s, items=[("Q1", "confirmed", None)])
        self.settle(s, aid)                                            # 1本かけて、話せて、完了
        return aid

    def spend(self, s, yen):
        return self.tx(s, lambda tx: usage.record(tx, source="ai", service="gemini-3.8-live", tier="free", kind="measured", quantity=int(yen * 100),
                                                  unit="input_tokens"))      # 単価 0.01円

    def month(self, s, confidence=0.95, gap_max=300):
        return self.tx(s, lambda tx: reward.month(tx, confidence=confidence, gap_max=gap_max))

    def notices(self, s):
        return self.tx(s, lambda tx: tx.all("SELECT summary FROM salescall.notification_events WHERE kind = 'budget'"
                                            " AND event_key LIKE %s", (f"cost_ratio:{s['client']}:%",)))

    def client_stops(self, s):
        return [r["cause"] for r in self.tx(s, lambda tx: stops.open_stops(tx, tenant_id=s["tenant"], client_id=s["client"],
                                                                          business_id=None))]

    def test_upper_limit_of_a_count(self):
        self.assertAlmostEqual(reward.poisson_upper(0, 0.95), 2.9957, places=3)    # 0件を見たとき（3の決まり）
        self.assertAlmostEqual(reward.poisson_upper(10, 0.95), 16.962, places=2)
        self.assertGreater(reward.poisson_upper(1000, 0.95), 1000)

    def test_each_form_is_counted_at_the_price_of_its_time(self):
        s = self.ready()
        self.terms(s, per_completed_yen=100, per_dial_yen=1, per_talk_yen=10)
        self.talked(s)
        self.terms(s, days_ago=0, per_completed_yen=1000, per_dial_yen=1000, per_talk_yen=1000)   # あとから変えた
        m = self.month(s)
        self.assertEqual({k: (v["count"], v["yen"]) for k, v in m["breakdown"].items() if k in ("completed", "dial", "talk")},
                         {"completed": (1, 100), "dial": (1, 1), "talk": (1, 10)})    # 過去の分は、そのときの単価
        self.assertEqual((m["revenue"], m["cost"]), (111, 0))
        self.assertGreater(m["upper"], m["revenue"])                   # 成果・話せた本数のぶれを多めに見込む
        with self.assertRaises(ValueError):
            self.terms(s, days_ago=0)                                  # どれも 0 円の決まりは足せない

    def test_a_completion_corrected_away_is_not_counted(self):
        # 2026-10-16 見直し：契約者が「完了でない」と直した行も、はじめて完了になった版で成果に数えていた
        s = self.ready()
        self.terms(s, per_completed_yen=100)
        self.talked(s)
        self.assertEqual(self.month(s)["breakdown"]["completed"]["count"], 1)
        self.tx(s, lambda tx: approval.correct_result(tx, s["task"], business_state="incomplete", complaint=False,
                                                      snapshot={"試験": "契約者が直した"}), OPERATOR)
        self.assertEqual(self.month(s)["breakdown"]["completed"]["count"], 0)
        self.tx(s, lambda tx: approval.correct_result(tx, s["task"], business_state="completed", complaint=False,
                                                      snapshot={"試験": "やはり完了"}), OPERATOR)
        self.assertEqual(self.month(s)["breakdown"]["completed"]["count"], 1)   # 数える時刻は、はじめて完了になった時

    def test_monthly_is_prorated(self):
        s = self.ready()
        self.terms(s, days_ago=40, monthly_yen=3000)
        got = self.month(s)["breakdown"]["monthly"]["yen"]
        self.assertTrue(0 < got <= 3000)

    def test_hourly_counts_the_calling_work_time(self):
        s = self.ready()
        self.terms(s, hourly_yen=3600)                                 # 1秒 1円
        aid = self.talked(s)
        took = self.tx(s, lambda tx: tx.one("SELECT extract(epoch FROM ended_at - claimed_at) AS t FROM salescall.attempts"
                                            " WHERE attempt_id = %s", (aid,)))["t"]
        got = self.month(s)["breakdown"]["hourly"]
        self.assertAlmostEqual(float(got["seconds"]), float(took), places=3)   # 予約から終わりまで（次の発信が無い）
        self.assertAlmostEqual(float(got["yen"]), float(took), places=3)

    def test_over_the_ratio_only_notifies_and_each_client_can_choose(self):
        s = self.ready()
        self.terms(s, per_dial_yen=10)                                 # かけた本数はぶれない：報酬は 10円ちょうど
        self.talked(s)
        self.priced("0.01")
        self.put_client(s, "budget.max_cost_ratio", 0.6)
        self.spend(s, 5)                                               # 50％：この依頼者の上限 60％ の内側
        self.assertEqual(self.notices(s), [])
        self.put_client(s, "budget.max_cost_ratio", 0.3)
        got = self.spend(s, 0.01)
        self.assertIsNone(got["stopped"])                              # 知らせるだけ
        self.assertEqual(len(self.notices(s)), 1)
        self.spend(s, 0.01)
        self.assertEqual(len(self.notices(s)), 1)                      # 1日1回
        self.assertEqual(self.client_stops(s), [])

    def warnings(self, s):
        return self.tx(s, lambda tx: tx.all("SELECT warning_id, confirmed_at, closed_why FROM salescall.deficit_warnings"
                                            " ORDER BY warning_id"))

    def test_deficit_warns_first_and_stops_if_not_confirmed_within_the_grace(self):
        """赤字：まず警告して知らせ、期限（1時間）までに確認ボタンが押されなければ止める（2026-10-16 ユーザー）。止めるのは
        見守りの tick でも（利用量の記録が無くても、期限が来たら止める）。赤字でなくなったら自動で解く。"""
        s = self.ready()
        self.terms(s, per_dial_yen=10)
        self.talked(s)
        self.priced("0.01")
        self.put_client(s, "budget.deficit_grace", 1)
        self.assertIsNone(self.spend(s, 15)["stopped"])                # 費用 15円 ＞ 報酬 10円：まず警告
        self.assertEqual(len(self.warnings(s)), 1)
        self.assertEqual(self.client_stops(s), [])
        notes = self.tx(s, lambda tx: tx.all("SELECT kind FROM salescall.notification_events WHERE kind = 'deficit_warning'"))
        self.assertEqual(len(notes), 1)
        time.sleep(1.2)
        usage.tick(self.w.a, SYSTEM)                                   # 期限が過ぎても確認されていない
        self.assertEqual(self.client_stops(s), ["deficit"])
        sid = self.tx(s, lambda tx: stops.open_stops(tx, tenant_id=s["tenant"], client_id=s["client"],
                                                     business_id=None))[0]["event_id"]
        self.assertEqual(self.tx(s, lambda tx: stops.explain(tx, sid))["cause"]["meaning"], stops.CAUSES["deficit"].meaning)
        usage.tick(self.w.a, SYSTEM)
        self.assertEqual(self.client_stops(s), ["deficit"])            # 見守りが見直しても、赤字のまま
        self.terms(s, per_dial_yen=10, monthly_yen=10 ** 9)            # 報酬の決まりを直した
        usage.tick(self.w.a, SYSTEM)
        self.assertEqual(self.client_stops(s), [])                     # 赤字でなくなったので、自動で解く

    def test_a_confirmed_deficit_does_not_stop(self):
        s = self.ready()
        self.terms(s, per_dial_yen=10)
        self.talked(s)
        self.priced("0.01")
        self.put_client(s, "budget.deficit_grace", 1)
        self.spend(s, 15)
        wid = self.warnings(s)[0]["warning_id"]
        with self.assertRaises(Rejected):
            self.tx(s, lambda tx: usage.confirm_deficit(tx, wid))         # システムの処理は確認できない
        self.tx(s, lambda tx: usage.confirm_deficit(tx, wid), OPERATOR)  # 契約者が確認ボタンを押した
        time.sleep(1.2)
        usage.tick(self.w.a, SYSTEM)
        self.spend(s, 1)
        self.assertEqual(self.client_stops(s), [])                     # 確認が効いているあいだは止めない

    def test_a_count_that_can_vary_is_judged_only_when_sure(self):
        s = self.ready()
        self.put_client(s, "budget.client_daily_yen", 100000)
        self.terms(s, per_completed_yen=1000)                          # 成果1件 1,000円。まだ成果0件
        self.priced("0.01")
        self.spend(s, 2900)
        self.assertEqual(self.warnings(s), [])                         # 0件でも、多めに見込めば約2,996円：まだ判断しない
        self.spend(s, 100)
        self.assertEqual(len(self.warnings(s)), 1)                     # 3,000円 ＞ 約2,996円：赤字と判断（まず警告）

    def test_no_terms_is_a_notice_not_a_stop(self):
        """報酬の決まりが無いのは設定の抜け（お客様には影響しない）：止めずに「設定がされていません」を知らせる（2026-10-16
        ユーザー：「止めるというのは、このまま進めると、お客様に対して、影響が出てしまうということだけ」）。"""
        s = self.ready()
        self.priced("0.01")
        self.spend(s, 1)
        self.assertEqual(self.client_stops(s), [])
        rows = self.tx(s, lambda tx: tx.all("SELECT kind FROM salescall.notification_events WHERE event_key LIKE %s",
                                            (f"reward_terms_missing:{s['client']}:%",)))
        self.assertEqual(rows, [{"kind": "setting_missing"}])

    def test_terms_are_frozen(self):
        s = self.ready()
        self.terms(s, per_dial_yen=1)
        own = owner_access()
        for sql in ("UPDATE salescall.reward_terms SET per_dial_yen = 5", "DELETE FROM salescall.reward_terms"):
            with self.subTest(sql=sql), self.assertRaises(Rejected):
                own.in_client(OPERATOR, s["tenant"], s["client"], lambda tx: tx.execute(sql))
