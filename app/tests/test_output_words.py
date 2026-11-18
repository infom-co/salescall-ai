"""言葉の表（output/words.py）の抜けの試験（持ち主 CL-3。docs/DESIGN.md 7-3-1）。

データベースの値の一覧と core の名前の一覧を、言葉の表と突き合わせる。名前が増えたのに言葉が無ければ落ちる（画面に英字の名前を出さない）。
依頼者へ出す言葉に、仕組みの言葉が入っていないかも確かめる（docs/DESIGN.md 7-3-6）。
"""

from __future__ import annotations

import unittest

from salescall.core import ai_troubles, approval, call_report, errors, exclusions, next_action, notify, stops
from salescall.core import business_rules
from salescall.output import words
from tests.support import SYSTEM, app_access



def _enum(name: str) -> list[str]:
    return app_access().in_system(SYSTEM, lambda tx: [r["v"] for r in tx.all(
        "SELECT e.enumlabel AS v FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid"
        " JOIN pg_namespace n ON n.oid = t.typnamespace WHERE n.nspname = 'salescall' AND t.typname = %s ORDER BY e.enumsortorder",
        (name,))])


class EveryNameHasAWord(unittest.TestCase):
    def test_every_database_state_has_a_screen_word(self):
        for enum, table in (("business_state", words.BUSINESS_STATES), ("attempt_state", words.ATTEMPT_STATES),
                            ("item_state", words.ITEM_STATES), ("delivery_state", words.DELIVERY_STATES),
                            ("next_kind", words.NEXT_KINDS)):
            values = _enum(enum)
            self.assertTrue(values, enum)
            for v in values:
                with self.subTest(enum=enum, value=v):
                    self.assertIn(v, table, f"{enum} の {v} に言葉がありません（output/words.py に足す）")
                    self.assertTrue(table[v].screen)

    def test_every_end_reason_the_next_action_can_write_has_a_word(self):
        names = (set(next_action.NUMBER_ENDS) | set(next_action.COUNT_ENDS) | set(next_action.DNC_ENDS)
                 | set(next_action.END_STATES) | set(business_rules.POLICY_KEYS)
                 | {words.POLICY_MAX_PREFIX + k for k in business_rules.POLICY_KEYS})
        for n in sorted(names):
            with self.subTest(end_reason=n):
                self.assertNotEqual(words.end_reason(n), words.NO_WORD)
                self.assertNotIn(words.NO_WORD, words.end_reason(n))

    def test_names_whose_words_live_in_core_are_read_not_copied(self):
        for cause in stops.CAUSES:
            self.assertEqual(words.stop_cause(cause), stops.CAUSES[cause].label)
        for scope in stops.SCOPE_NAMES:
            self.assertEqual(words.stop_scope(scope), stops.SCOPE_NAMES[scope])
        for kind in notify.KINDS:
            self.assertEqual(words.notice_kind(kind), notify.KINDS[kind])
        for level in exclusions.LEVEL_NAMES:
            self.assertEqual(words.exclusion_level(level), exclusions.LEVEL_NAMES[level])
        for stage in ai_troubles.STAGES:
            self.assertEqual(words.ai_trouble_stage(stage), ai_troubles.STAGES[stage])
        for k in stops.KEEPING_KINDS:
            self.assertEqual(words.keeping_kind(k), stops.KEEPING_KINDS[k])

    def test_every_way_a_call_was_ended_early_has_a_word_read_from_core(self):   # 2026-11-18 CL-2 4-421 の (1)：worker_lost
        for name in errors.ENDS:
            with self.subTest(end=name):
                self.assertNotEqual(words.call_end(name), words.NO_WORD)
                if name in errors.TROUBLES:
                    self.assertEqual(words.call_end(name), errors.LABELS[errors.TROUBLES[name]])
                    self.assertNotIn(name, words.CALL_ENDS_NOT_OURS)   # 正本を写さない
        self.assertEqual(words.call_end("worker_lost"), "回線の処理が通話の途中で止まりました")

    def test_every_price_source_unit_and_tier_has_a_word(self):
        import re as _re
        defs = app_access().in_system(SYSTEM, lambda tx: " ".join(r["d"] for r in tx.all(
            "SELECT pg_get_constraintdef(c.oid) AS d FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid"
            " JOIN pg_namespace n ON n.oid = t.relnamespace WHERE n.nspname = 'salescall' AND t.relname = 'price_versions'"
            " AND c.contype = 'c'")))
        def values(col):
            m = _re.search(col + r"\)? = ANY \(\(?ARRAY\[([^\]]*)\]", defs) or _re.search(col + r" = ANY \(ARRAY\[([^\]]*)\]", defs)
            return set(_re.findall(r"'([a-z_]+)'", m.group(1))) if m else set()
        self.assertTrue(values("source"))
        self.assertEqual(values("source") - set(words.PRICE_SOURCE_WORDS), set())
        self.assertEqual(values("unit") - set(words.PRICE_UNITS), set())

    def test_every_quality_metric_has_a_word(self):
        from salescall.core import quality_stage
        self.assertEqual(set(words.QUALITY_METRIC_WORDS), set(quality_stage.METRICS))

    def test_every_field_type_and_target_kind_has_a_word(self):
        from salescall.core import business_settings
        types = app_access().in_system(SYSTEM, lambda tx: {r["t"] for r in tx.all("SELECT type_name AS t FROM salescall.field_types")})
        self.assertEqual(set(words.FIELD_TYPE_WORDS), types)
        self.assertEqual(set(words.TARGET_PERSON_WORDS), set(business_settings.TARGET_PERSON_KINDS))

    def test_every_dnc_reason_has_a_word(self):
        import typing
        from salescall.core import dnc
        self.assertEqual(set(words.DNC_REASON_WORDS), set(typing.get_args(dnc.Reason)))

    def test_every_import_field_has_a_word(self):
        from salescall.core import importer
        self.assertEqual(set(words.IMPORT_FIELD_WORDS), set(importer.FIELDS))

    def test_every_number_label_and_origin_has_a_word(self):
        from salescall.core import customer_contacts
        self.assertEqual(set(words.NUMBER_LABEL_WORDS), set(customer_contacts.NUMBER_LABELS))
        self.assertEqual(set(words.ORIGIN_WORDS), set(customer_contacts.ORIGINS))

    def test_the_decision_parts_and_kinds_have_words(self):
        self.assertEqual(set(words.DECISION_PARTS), set(call_report.DECISION_PARTS))
        self.assertEqual(set(words.DECISION_KINDS), set(approval.WAIT_KINDS))

    def test_an_unknown_name_shows_no_english(self):
        self.assertEqual(words.word(words.BUSINESS_STATES, "no_such_state").screen, words.NO_WORD)
        self.assertEqual(words.end_reason("no_such_reason"), words.NO_WORD)
        self.assertEqual(words.stop_cause("no_such_cause"), words.NO_WORD)


class ClientWordsSayNothingAboutTheSystem(unittest.TestCase):
    def test_no_client_word_contains_a_system_word(self):
        for name, table in (("業務の状態", words.BUSINESS_STATES), ("試行の状態", words.ATTEMPT_STATES),
                            ("項目の状態", words.ITEM_STATES), ("連携の状態", words.DELIVERY_STATES),
                            ("次の予定", words.NEXT_KINDS)):
            for key, w in table.items():
                if w.client is None:
                    continue
                with self.subTest(table=name, key=key):
                    self.assertEqual(words.not_for_client(w.client), [], w.client)

    def test_states_that_are_inside_the_system_are_not_given_to_the_client(self):
        self.assertIsNone(words.BUSINESS_STATES["unknown"].client)   # 判定できない＝判定待ち（依頼者へは判定し直してから）
        self.assertIsNone(words.NEXT_KINDS["hold"].client)           # 判定待ち
        self.assertIsNone(words.ATTEMPT_STATES["released"].client)   # 発信していない予約


class NotForClient(unittest.TestCase):
    def test_the_words_the_admin_decided_are_caught(self):   # 2026-11-18 管理者の一覧
        for text in ("AIが電話しました", "人工知能", "自動で終えた", "システムの都合", "the System said", "データベースの誤り",
                     "ボットです", "a bot", "ロボット", "判定待ち", "検証の段階", "模擬通話", "do_not_call", "callback_scheduled"):
            with self.subTest(text=text):
                self.assertTrue(words.not_for_client(text))

    def test_ordinary_client_words_pass(self):
        for text in ("完了", "お断り", "ご担当の方に繋がらず", "最後までお話しできました", "お急ぎのため途中で終えました", "Taibot商事"):
            with self.subTest(text=text):
                self.assertEqual(words.not_for_client(text), [])


if __name__ == "__main__":
    unittest.main()
