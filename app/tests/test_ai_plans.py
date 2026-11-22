"""契約プラン（AI の料金。どのキーで呼ぶか）の試験（core/ai_plans.py・0176。2026-11-05 ユーザー決定）。"""

from __future__ import annotations

import unittest

from salescall.core import ai_plans, settings
from salescall.core.access import Rejected
from tests.support import ADMIN, OPERATOR, SYSTEM, World


class Plans(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.s = self.w.setup()

    def plan(self, ai, s=None):
        s = s or self.s
        return self.w.a.in_tenant(OPERATOR, s["tenant"], lambda tx: ai_plans.plan(tx, ai, tenant_id=s["tenant"]))

    def choose(self, key, value, s=None, actor=ADMIN):
        """契約者ごとのプランを書く（書けるのは管理者だけ＝0190。✅ 2026-11-06 ユーザー：伺い「プランの切替」に「管理者だけ」）。"""
        s = s or self.s
        return self.w.a.in_tenant(actor, s["tenant"], lambda tx: settings.set_value(
            tx, key, value, scope="tenant", status="decided", rationale="試験：契約プラン", review_when="試験", tenant_id=s["tenant"]))

    def test_conversation_and_import_are_chosen_per_contractor_and_judge_is_always_ours(self):
        """会話 AI と業務を組む AI は契約者ごとに選べる（AI料金込み／自分で API を契約）。判定はいつもシステムのキー（「判定は企業秘密なので
        こちらのみ」）。ほかの契約者の選びは効かない。"""
        # 契約者が選ぶ前は「AI 料金別」＝own（2026-11-06 ユーザー）。判定はいつも included
        self.assertEqual([self.plan(a) for a in ("conversation", "import", "judge")], ["own", "own", "included"])
        self.choose("ai.plan_conversation", "included")                  # 片方ずつ（取り違えない）
        self.assertEqual([self.plan(a) for a in ("conversation", "import", "judge")], ["included", "own", "included"])
        self.choose("ai.plan_import", "included")
        self.choose("ai.plan_conversation", "own")
        self.assertEqual([self.plan(a) for a in ("conversation", "import", "judge")], ["own", "included", "included"])
        other = self.w.setup()
        self.assertEqual(self.plan("conversation", other), "own")
        with self.assertRaises(ValueError):
            self.plan("ear")

    def test_only_the_two_plans_can_be_chosen(self):
        """選べるのは2つだけ（データベースが断る）。判定の AI の設定は無い（選べない）。"""
        with self.assertRaises(Rejected):
            self.choose("ai.plan_conversation", "free")
        with self.assertRaises(settings.SettingMissing):
            self.w.a.in_tenant(OPERATOR, self.s["tenant"], lambda tx: settings.resolve(
                tx, "ai.plan_judge", tenant_id=self.s["tenant"], client_id=None, business_id=None))

    def test_only_the_administrator_switches_a_contractors_plan(self):
        """契約プランは料金に関わるので、契約者ごとの値でも、書けるのは管理者だけ（データベースが守る＝設定の定義の admin_writes と
        system_values_by_admin の1か所。✅ 2026-11-06 ユーザー：「管理者だけ」）。契約者が書こうとしても、プランは変わらない。"""
        for key in ("ai.plan_conversation", "ai.plan_import"):
            with self.subTest(key=key), self.assertRaises(Rejected) as e:
                self.choose(key, "included", actor=OPERATOR)
            self.assertEqual(e.exception.constraint, "setting_admin_writes")
        self.assertEqual([self.plan(a) for a in ("conversation", "import")], ["own", "own"])
        self.choose("ai.plan_conversation", "included")
        self.assertEqual(self.plan("conversation"), "included")


class Requests(Plans):
    def test_a_contractor_asks_the_admin_switches_and_the_request_closes_itself(self):
        """契約プランの申し込み（0288。CL-3 の連絡帳 79）：契約者は AI ごとに1つだけ申し込める・管理者へ知らせる・管理者がそのプランに切り替えたら
        データベースが閉じる・見送るときは管理者が理由を書いて閉じる・全部の契約者の申し込みを読めるのは管理者だけ。"""
        ask = lambda ai, plan, actor=OPERATOR: self.w.a.in_tenant(actor, self.s["tenant"], lambda tx: ai_plans.request(   # noqa: E731
            tx, ai, plan, note="試験：料金込みにしたい"))
        opened = lambda ai: self.w.a.in_tenant(OPERATOR, self.s["tenant"], lambda tx: ai_plans.open_request(tx, ai))   # noqa: E731
        listed = lambda: [r["request_id"] for r in self.w.a.in_system(ADMIN, ai_plans.requests)]   # noqa: E731
        with self.assertRaises(ValueError):                                     # いまと同じプランは申し込まない
            ask("conversation", "own")
        rid = ask("conversation", "included")
        self.assertEqual(opened("conversation")["request_id"], rid)
        with self.assertRaises(Rejected) as e:                                  # 開いた申し込みは AI ごとに1つ
            ask("conversation", "included")
        self.assertEqual(e.exception.constraint, "plan_request_one_open")
        with self.assertRaises(Rejected) as e:                                  # 申し込めるのは人だけ
            ask("import", "included", actor=SYSTEM)
        self.assertEqual(e.exception.constraint, "plan_request_by_person")
        told = self.w.a.in_system(ADMIN, lambda tx: tx.all(
            "SELECT kind FROM salescall.notification_events WHERE for_admin AND event_key = %s", (f"admin:plan_request:{rid}",)))
        self.assertEqual([r["kind"] for r in told], ["plan_request"])
        self.assertIn(rid, listed())
        with self.assertRaises(Rejected):                                       # 全部の契約者の申し込みは管理者だけ
            self.w.a.in_system(SYSTEM, ai_plans.requests)
        self.choose("ai.plan_conversation", "included")                         # 管理者が切り替える＝閉じる
        self.assertIsNone(opened("conversation"))
        self.assertNotIn(rid, listed())
        other = ask("import", "included")
        with self.assertRaises(Rejected):                                       # 見送れるのは管理者だけ
            self.w.a.in_tenant(OPERATOR, self.s["tenant"], lambda tx: ai_plans.decline(tx, other, note="試験"))
        self.w.a.in_tenant(ADMIN, self.s["tenant"], lambda tx: ai_plans.decline(tx, other, note="試験：いまは切り替えられない"))
        self.assertIsNone(opened("import"))
        self.assertEqual(self.plan("import"), "own")


class Tiers(Plans):
    def tier(self, ai, s=None):
        s = s or self.s
        return self.w.a.in_tenant(OPERATOR, s["tenant"], lambda tx: ai_plans.tier(tx, ai, tenant_id=s["tenant"]))

    def test_the_contractor_chooses_free_or_paid_only_for_its_own_key(self):
        """無料枠と有料枠（0257。✅ 2026-10-16 ユーザー「契約者が無料枠と有料枠を選べる。…有料枠をおすすめする（強制はしない）」・CL-3 の 36）：
        自分で API を契約するプラン（own）のときだけ、契約者が選んだ枠。初めは、おすすめの有料枠。契約者が自分で選べる（プランと違い、料金の
        プランではない）。AI料金込み・判定の AI は、契約者の枠を持たない（None）。選べるのは free と paid だけ。"""
        self.assertEqual([self.tier(a) for a in ("conversation", "import", "judge")], ["paid", "paid", None])
        self.choose("ai.tier_conversation", "free", actor=OPERATOR)
        self.assertEqual([self.tier(a) for a in ("conversation", "import")], ["free", "paid"])
        self.choose("ai.plan_conversation", "included")
        self.assertIsNone(self.tier("conversation"))                     # システムのキー：契約者の枠ではない
        with self.assertRaises(Rejected):
            self.choose("ai.tier_import", "cheap", actor=OPERATOR)
        # 画面が読む名前（CL-3 の 55）：枠の設定は、プランの設定と同じ AI ごとに1つずつ・選べる値は TIERS とそろう
        self.assertEqual(set(ai_plans.TIER_SETTINGS), set(ai_plans.PLAN_SETTINGS))
        rows = self.w.a.in_tenant(OPERATOR, self.s["tenant"], lambda tx: tx.all(
            "SELECT key, choices FROM salescall.setting_definitions WHERE key = ANY(%s)", (list(ai_plans.TIER_SETTINGS.values()),)))
        self.assertEqual({r["key"]: tuple(r["choices"]) for r in rows}, {k: ai_plans.TIERS for k in ai_plans.TIER_SETTINGS.values()})
