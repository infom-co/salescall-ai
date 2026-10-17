"""登録画面の i マークの詳細説明（core/help_texts.py）の試験。"""

from __future__ import annotations

import unittest

from salescall.core import business_settings, help_texts
from tests.support import SYSTEM, owner_access


class Explain(unittest.TestCase):
    def test_every_setting_and_registration_item_has_an_explanation(self):
        """設定の値はすべて、設定の定義の意味が説明になる。業務設定の項目と回線の割り当ても説明がある（書き漏れを空にしない）。"""
        keys = owner_access().in_system(SYSTEM, lambda tx: [r["key"] for r in tx.all("SELECT key FROM salescall.setting_definitions")])
        for key in keys:
            with self.subTest(key):
                got = owner_access().in_system(SYSTEM, lambda tx: help_texts.explain(tx, key))
                self.assertTrue(got.strip())
        items = {"purpose": "business.purpose", "target_class": "business.target_class", "caller": "business.caller",
                 "target_person": "business.target_person", "busy_line": "business.busy_line", "voice": "business.voice",
                 "questions": "business.questions"}
        self.assertTrue(set(items) <= business_settings.KEYS)
        for name in [*items.values(), "business.handling", "business.retry", "business.forbidden", "line.client"]:
            with self.subTest(name):
                self.assertTrue(owner_access().in_system(SYSTEM, lambda tx: help_texts.explain(tx, name)).strip())
        with self.assertRaises(LookupError):
            owner_access().in_system(SYSTEM, lambda tx: help_texts.explain(tx, "無い項目"))
