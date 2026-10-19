"""利用量の台帳と予算（Q16）の試験。"""

from __future__ import annotations

import time
from decimal import Decimal

from salescall.core import approval, gate, reward, settings, stops, usage
from salescall.core.access import Rejected
from tests.support import ADMIN, OPERATOR, SYSTEM, owner_access
from salescall.core.call_report import Cut
from tests.test_settle import SettleBase, rep
from tests.test_watch import resume_open_system_stops


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
        self.assertEqual(self.tx(s, lambda tx: reward.month(tx))["unpriced_entries"], 1)

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


    def test_the_judge_ai_has_its_own_source_and_tier(self):
        """意味の判定に使う AI（judge.models）の費用は、出どころ「意味の判定」として、枠（無料・有料）ごとの単価で記録する（見直し8：
        前は出どころが会話AI・別の耳・回線だけで、判定の費用を書く場所が無く、費用の見張り・赤字の判断から抜けた）。"""
        s = self.ready()
        put = lambda tier, actor=OPERATOR: self.w.a.in_tenant(actor, s["tenant"], lambda tx: usage.set_price(   # noqa: E731
            tx, source="judge", service="試験の判定AI", tier=tier, unit="input_tokens", yen_per_unit=Decimal("0.2"),
            valid_from=tx.one("SELECT clock_timestamp() AS t")["t"], note="試験"))
        put("paid")
        with self.assertRaises(Rejected):                                 # 判定の AI にも枠がある（回線とちがう）
            put(None)
        got = self.tx(s, lambda tx: usage.record(tx, source="judge", service="試験の判定AI", tier="paid", kind="measured",
                                                 quantity=10, unit="input_tokens"))
        self.assertEqual(got["amount_yen"], Decimal("2.0"))
        self.assertEqual(self.tx(s, lambda tx: tx.one("SELECT source FROM salescall.usage_entries WHERE entry_id = %s",
                                                      (got["entry_id"],)))["source"], "judge")


class Budget(Priced):
    def ample(self, s):
        """赤字で止まらないよう、十分な月額の報酬を入れる（1日の上限だけを確かめる）。"""
        self.tx(s, lambda tx: reward.set_terms(tx, reward.Terms(monthly_yen=Decimal(10 ** 12), deficit_caution="normal"),
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

    def test_contractor_limit_counts_only_that_contractor_and_is_only_noticed_to_it(self):
        """契約者の1日の上限（その契約者のすべての依頼者の合計）を超えたら、その契約者へ注意を知らせるだけ（止めない）。
        ほかの契約者の費用は数えない（2026-10-18 ユーザー：「費用の見張りと控えは、契約者ごとですよ」＝費用は契約者が払う）。"""
        s, other = self.ready(), self.ready()
        for x in (s, other):
            self.ample(x)
        self.w.a.in_tenant(OPERATOR, s["tenant"], lambda tx: settings.set_value(
            tx, "budget.tenant_daily_yen", 0, scope="tenant", tenant_id=s["tenant"], status="decided", rationale="試験",
            review_when="試験"))
        self.priced("0.01")
        spend = lambda x: self.tx(x, lambda tx: usage.record(   # noqa: E731
            tx, source="ai", service="gemini-3.8-live", tier="free", kind="measured", quantity=100, unit="input_tokens"))
        notices = lambda x: self.w.a.in_tenant(OPERATOR, x["tenant"], lambda tx: tx.all(   # noqa: E731
            "SELECT event_key FROM salescall.notification_events WHERE kind = 'budget' AND event_key LIKE 'tenant_daily:%'"))
        spend(other)                                                    # ほかの契約者だけが使った
        self.assertEqual(usage.tick(self.w.a, SYSTEM, s["tenant"]), {"stopped": [], "resumed": []})
        self.assertEqual(notices(s), [])
        spend(s)
        usage.tick(self.w.a, SYSTEM, s["tenant"])
        usage.tick(self.w.a, SYSTEM, s["tenant"])
        usage.tick(self.w.a, SYSTEM, other["tenant"])
        self.assertEqual((len(notices(s)), notices(other)), (1, []))  # その契約者へ1日1回。ほかの契約者の上限は初めの値のまま
        self.assertIsInstance(self.claim(s), gate.Claim)                 # 発信は続く


class CostRatio(Priced):
    """報酬に対する費用の割合（2026-10-15 ユーザー決定：既定 30％・依頼者ごとに選べる。超えたら知らせるだけ。赤字なら止める）。"""
    def terms(self, s, *, days_ago=1, gap=None, caution="normal", **yen):
        return self.tx(s, lambda tx: reward.set_terms(
            tx, reward.Terms(**{k: Decimal(v) for k, v in yen.items()}, hourly_gap_seconds=gap,
                             deficit_caution=caution),
            valid_from=tx.one("SELECT now() - %s * interval '1 day' AS t", (days_ago,))["t"], note="試験"), OPERATOR)

    def talked(self, s):
        aid = self.call(s, items=[("Q1", "confirmed", None)])
        self.settle(s, aid)                                            # 1本かけて、話せて、完了
        return aid

    def spend(self, s, yen):
        return self.tx(s, lambda tx: usage.record(tx, source="ai", service="gemini-3.8-live", tier="free", kind="measured", quantity=int(yen * 100),
                                                  unit="input_tokens"))      # 単価 0.01円

    def month(self, s):
        return self.tx(s, lambda tx: reward.month(tx))

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
        self.tx(s, lambda tx: approval.correct_result(tx, s["task"], checked_version=1, business_state="incomplete",
                                                      wrong_items=[], complaint=False, snapshot={"試験": "契約者が直した"}),
                OPERATOR)
        self.assertEqual(self.month(s)["breakdown"]["completed"]["count"], 0)
        self.tx(s, lambda tx: approval.correct_result(tx, s["task"], checked_version=1, business_state="completed",
                                                      wrong_items=[], complaint=False, snapshot={"試験": "やはり完了"}),
                OPERATOR)
        self.assertEqual(self.month(s)["breakdown"]["completed"]["count"], 1)   # 数える時刻は、はじめて完了になった時

    def test_monthly_is_prorated(self):
        s = self.ready()
        self.terms(s, days_ago=40, monthly_yen=3000)
        got = self.month(s)["breakdown"]["monthly"]["yen"]
        self.assertTrue(0 < got <= 3000)

    def test_hourly_counts_like_a_person_would(self):
        """時給の作業時間は、人と同じ速さで数える：電話1本ごとに、かけ始めから終わりまで＋電話と電話の間（依頼者ごとの秒数）
        （2026-10-17 ユーザー：「時給換算では、人がやるのと同じスピードが重要」「1から2分くらい」「依頼者ごとに上下で設定」）。"""
        s = self.ready()
        with self.assertRaises(ValueError):                            # 時給には、電話と電話の間が要る
            self.terms(s, hourly_yen=3600)
        with self.assertRaises(ValueError):                            # 赤字の判断の慎重さも、必ず書く
            self.terms(s, per_dial_yen=1, caution=None)
        self.terms(s, hourly_yen=3600, gap=90)                         # 1秒 1円・間は 90秒
        aid = self.talked(s)
        took = self.tx(s, lambda tx: tx.one("SELECT extract(epoch FROM ended_at - claimed_at) AS t FROM salescall.attempts"
                                            " WHERE attempt_id = %s", (aid,)))["t"]
        got = self.month(s)["breakdown"]["hourly"]
        self.assertAlmostEqual(float(got["seconds"]), float(took) + 90, places=3)
        self.assertAlmostEqual(float(got["yen"]), float(took) + 90, places=3)

    def test_a_call_cut_and_called_back_counts_as_one_customer(self):
        """途中で切れて、お詫びしてすぐかけ直した電話は、1本あたりの報酬では1人のお客様に1本分。時給では2本の時間を足すが、電話と
        電話の間は1回だけ（2026-10-18 ユーザー：「1人のお客さんに対して、50円ですよね」「時給では1人のお客様への総時間」）。"""
        s = self.ready()
        self.terms(s, per_dial_yen=50, per_talk_yen=50, hourly_yen=3600, gap=120)
        cut = self.call(s, ("dialing", "ringing", "answered", "in_conversation", "dropped"), side="line",
                        report=rep(cut=Cut("callee", False, False)))
        self.assertEqual(self.settle(s, cut)["decision"].target.purpose, "resume")
        again = self.talked(s)                                          # お詫びしてすぐかけ直した電話
        self.assertEqual(self.tx(s, lambda tx: tx.one("SELECT purpose FROM salescall.attempts WHERE attempt_id = %s",
                                                      (again,)))["purpose"], "resume")
        m = self.month(s)["breakdown"]
        self.assertEqual((m["dial"]["count"], m["talk"]["count"]), (1, 1))   # 50円ずつ1回
        took = self.tx(s, lambda tx: tx.one("SELECT sum(extract(epoch FROM ended_at - claimed_at)) AS t FROM salescall.attempts"
                                            " WHERE attempt_id IN (%s, %s)", (cut, again)))["t"]
        self.assertAlmostEqual(float(m["hourly"]["seconds"]), float(took) + 120, places=3)   # 2本の時間＋間は1回

    def test_the_caution_of_the_deficit_notice_is_per_client_and_in_plain_words(self):
        """赤字の知らせの早さは依頼者ごと・3つの選び方（2026-10-17 ユーザー：案B「成果が0件のまま、使った費用が成果1件分の報酬の
        4分の1・半分・1件分を超えたら知らせる」）。画面の例の本数（成果1件 1,000円・電話1本 2円）が、中の計算と合っている。"""
        for name, examples in (("early", 125), ("normal", 250), ("careful", 500)):
            calls = 1000 * float(reward.caution_margin(name)) / 2
            self.assertLess(abs(calls - examples) / examples, 0.01, name)
            self.assertIn(f"約{examples:,}本", reward.CAUTIONS[name][1])
        s = self.ready()
        self.terms(s, per_completed_yen=1000, caution="careful")
        careful = self.month(s)["upper"]
        t = self.ready()
        self.terms(t, per_completed_yen=1000, caution="early")
        self.assertGreater(careful, self.month(t)["upper"])
        with self.assertRaises(ValueError):
            self.terms(t, per_completed_yen=1000, caution="0.95")

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
        usage.tick(self.w.a, SYSTEM, s["tenant"])                                   # 期限が過ぎても確認されていない
        self.assertEqual(self.client_stops(s), ["deficit"])
        sid = self.tx(s, lambda tx: stops.open_stops(tx, tenant_id=s["tenant"], client_id=s["client"],
                                                     business_id=None))[0]["event_id"]
        self.assertEqual(self.tx(s, lambda tx: stops.explain(tx, sid))["cause"]["meaning"], stops.CAUSES["deficit"].meaning)
        usage.tick(self.w.a, SYSTEM, s["tenant"])
        self.assertEqual(self.client_stops(s), ["deficit"])            # 見守りが見直しても、赤字のまま
        self.terms(s, per_dial_yen=10, monthly_yen=10 ** 9)            # 報酬の決まりを直した
        usage.tick(self.w.a, SYSTEM, s["tenant"])
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
        usage.tick(self.w.a, SYSTEM, s["tenant"])
        self.spend(s, 1)
        self.assertEqual(self.client_stops(s), [])                     # 確認が効いているあいだは止めない

    def test_a_count_that_can_vary_is_judged_only_when_sure(self):
        s = self.ready()
        self.put_client(s, "budget.client_daily_yen", 100000)
        self.terms(s, per_completed_yen=1000, caution="normal")        # 成果1件 1,000円。まだ成果0件。中くらい＝成果1件分の半分
        self.priced("0.01")
        self.spend(s, 490)
        self.assertEqual(self.warnings(s), [])                         # 0件でも、成果半分（500円）まで見込む：まだ判断しない
        self.spend(s, 20)
        self.assertEqual(len(self.warnings(s)), 1)                     # 510円 ＞ 500円：赤字と判断（まず警告）

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
