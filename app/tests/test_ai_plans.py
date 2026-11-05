"""契約プラン（AI の料金。どのキーで呼ぶか）の試験（core/ai_plans.py・0176。2026-11-05 ユーザー決定）。"""

from __future__ import annotations

import unittest

from salescall.core import ai_plans, settings
from salescall.core.access import Rejected
from tests.support import OPERATOR, World


class Plans(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.s = self.w.setup()

    def plan(self, ai, s=None):
        s = s or self.s
        return self.w.a.in_tenant(OPERATOR, s["tenant"], lambda tx: ai_plans.plan(tx, ai, tenant_id=s["tenant"]))

    def choose(self, key, value, s=None):
        s = s or self.s
        return self.w.a.in_tenant(OPERATOR, s["tenant"], lambda tx: settings.set_value(
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
