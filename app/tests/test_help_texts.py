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
                 "target_person": "business.target_person", "busy_line": "business.busy_line",
                 "voicemail_message": "business.voicemail_message", "ai_answer": "business.ai_answer", "answers": "business.answers",
                 "voice": "business.voice",
                 "questions": "business.questions"}
        self.assertTrue(set(items) <= business_settings.KEYS)
        responses = {k: f"business.{k}" for k in business_settings.RESPONSE_KEYS}   # 相手の申し出への応じ方（CL-1 の 4-28）
        items.update(responses)
        for name in [*items.values(), "business.handling", "business.retry", "business.forbidden", "line.client"]:
            with self.subTest(name):
                self.assertTrue(owner_access().in_system(SYSTEM, lambda tx: help_texts.explain(tx, name)).strip())
        with self.assertRaises(LookupError):
            owner_access().in_system(SYSTEM, lambda tx: help_texts.explain(tx, "無い項目"))

    def test_the_screen_texts_have_no_development_notes_and_every_choice_has_words(self):
        """画面に出す説明（設定の定義の help＝0268・設定でない項目の FIELDS）と選び肢の言葉は、契約者が読む日本語だけ（CL-3 の 27・45）：決めた日付・文書の名前・
        担当の名前・設定の名前・選び肢の英字の名前・「ユーザー」（画面の呼び方は管理者）を含まない。選ぶ設定は、選び肢のすべてに言葉がある（データベースも守る）。
        開発の根拠は meaning に残す（画面には出さない）。"""
        import re
        from salescall.core.access import Rejected
        rows = owner_access().in_system(SYSTEM, lambda tx: tx.all("SELECT key, help, choices, choice_words FROM salescall.setting_definitions"))
        dev = re.compile(r"(?<![A-Za-z])[a-z_]+\.[a-z_]+|Intent|DESIGN|✅|⏳|⛔|20\d\d-\d|CL-\d|計画書|設計書|ユーザー")
        texts = {r["key"]: [r["help"], *(r["choice_words"] or {}).values()] for r in rows}
        texts.update({k: [v] for k, v in help_texts.FIELDS.items()})
        for key, words in texts.items():
            for text in words:
                with self.subTest(key=key, text=text[:30]):
                    self.assertIsNone(dev.search(text))
        for r in rows:
            with self.subTest(key=r["key"]):
                choices = set(r["choices"] or ())
                self.assertEqual(set(owner_access().in_system(SYSTEM, lambda tx: help_texts.choice_words(tx, r["key"]))), choices)
                self.assertEqual(owner_access().in_system(SYSTEM, lambda tx: help_texts.explain(tx, r["key"])), r["help"])   # 画面は help を出す
                for value in choices:   # 選び肢の英字の名前は、説明にも言葉にも出さない
                    self.assertNotIn(value, r["help"])
        with self.assertRaises(Rejected) as e:   # 選び肢の言葉の欠けた定義は入れられない
            owner_access().in_system(SYSTEM, lambda tx: tx.execute(
                "INSERT INTO salescall.setting_definitions (key, merge_kind, value_type, meaning, design_ref, finest_scope, choices, help, choice_words)"
                " VALUES ('test.choice', 'override', 'choice', '試験', '試験', 'business', ARRAY['a', 'b'], '試験', '{\"a\": \"エー\"}')"))
        self.assertEqual(e.exception.constraint, "setting_definitions_choice_words")
