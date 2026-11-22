"""言葉の表（output/words.py）の抜けの試験（持ち主 CL-3。docs/DESIGN.md 7-3-1）。

データベースの値の一覧と core の名前の一覧を、言葉の表と突き合わせる。名前が増えたのに言葉が無ければ落ちる（画面に英字の名前を出さない）。
依頼者へ出す言葉に、仕組みの言葉が入っていないかも確かめる（docs/DESIGN.md 7-3-6）。
"""

from __future__ import annotations

import unittest

# 画面に出してはいけない開発の覚え書きのしるし（決めた日付・決まりの番号・文書の名前・担当の名前・内部の言い方）
DEV_NOTE = r"Intent|DESIGN|ユーザー|✅|⏳|⛔|(?<![\d-])20\d\d-\d\d(?!\d)|CL-\d|(?<!ダイヤル)(?<!ダイヤル（)(?<!ダイヤル（0\d{3}・)[（(]?0\d{3}[）)]|見守り役|＝計画書|＝DESIGN"   # 表の作りの番号（0170）。電話番号の頭（ナビダイヤル（0570））は除く。年月（2026-11）は、番号の途中（050-2045-5312 の「2045-53」）を除く＝前後が数字や「-」につながるものは当てない（2026-11-22：試験の架空の番号で、たまに落ちていた）

from salescall.core import ai_troubles, approval, call_report, errors, exclusions, next_action, notify, stops
from salescall.core import business_rules
from salescall.output import words
from tests.support import SYSTEM, app_access



def _enum(name: str) -> list[str]:
    return app_access().in_system(SYSTEM, lambda tx: [r["v"] for r in tx.all(
        "SELECT e.enumlabel AS v FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid"
        " JOIN pg_namespace n ON n.oid = t.typnamespace WHERE n.nspname = 'salescall' AND t.typname = %s ORDER BY e.enumsortorder",
        (name,))])


class PlainText(unittest.TestCase):
    """データの文（知らせの本文・修正依頼の中身など）を画面の形にする1か所（words.plain）。"""
    def test_a_clause_with_an_internal_name_is_left_out_and_the_rest_stays(self):
        got = words.plain("確かめ直してください（確かめ直す通話の一覧は approval.awaiting_ai_recheck。それまで、そのお客様にはかけません）")
        self.assertEqual(got, "確かめ直してください（それまで、そのお客様にはかけません）")
        self.assertEqual(words.plain("一覧は approval.x_y です。次の文です。"), "次の文です。")
        self.assertEqual(words.plain("Claude Sonnet 5.5 と Jev 1.14 で判定"), "Claude Sonnet 5.5 と Jev 1.14 で判定")   # 名前の版はそのまま
        self.assertEqual(words.not_for_client(words.plain("（a_b。c.d_e）のこと")), [])


class EveryNameHasAWord(unittest.TestCase):
    def test_every_retry_policy_and_interval_has_a_screen_word(self):   # 計画書 6-2「再発信の方針」（業務設定の版の画面）
        from salescall.core import business_rules
        self.assertEqual(set(words.RETRY_POLICY_WORDS), set(business_rules.POLICY_KEYS) | set(business_rules.SYSTEM_POLICIES))
        self.assertEqual(set(words.INTERVAL_WORDS), set(business_rules.INTERVAL_UNITS))
        self.assertEqual(words.retry_policy(business_rules.Policy("call", 3, ("hours", 3))), "かけ直す：3回まで・3時間おき")
        self.assertEqual(words.retry_policy(business_rules.Policy("end", None, None)), "かけ直さずに終える")

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
        for kind in notify.KINDS:   # 知らせの種類は、core の定義（開発の説明）ではなく、画面の短い名前（NOTICE_KIND_WORDS。下の試験）
            self.assertEqual(words.notice_kind(kind), words.NOTICE_KIND_WORDS[kind])
        for level in exclusions.LEVEL_NAMES:   # 行の印は、core の名前（依頼者のファイルの列の言葉）ではなく、画面の名前（EXCLUSION_WORDS。下の試験）
            self.assertEqual(words.exclusion_level(level), words.EXCLUSION_WORDS[level])
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
        self.assertEqual(set(words.CALL_TYPE_WORDS), set(business_settings.CALL_TYPES))   # 電話の型（計画書 9-9）

    def test_every_metric_has_a_word_and_a_shape(self):   # 計画書 9-8 の指標（数えるのは core の metrics.business の1か所）
        from datetime import datetime, timedelta, timezone
        from salescall.core import attempts, metrics
        from tests.support import OPERATOR, World
        s = World().setup()
        now = datetime.now(timezone.utc)
        got = app_access().in_client(OPERATOR, s["tenant"], s["client"],
                                     lambda tx: metrics.business(tx, s["business"], since=now - timedelta(days=30), until=now))
        self.assertEqual({k for k, _, _ in words.METRIC_ROWS}, set(got) - {"unpriced", "ai_voice"})
        self.assertEqual(set(words.AI_VOICE_WORDS), set(attempts.VOICE_KINDS))
        self.assertEqual(words.metric_text("rate", None), "まだ数えていません")              # 0 と書かない（原則10）
        self.assertEqual(words.metric_text("rate", {"hits": 0, "total": 0, "value": None}), "まだありません（0件）")
        self.assertEqual(words.metric_text("rate", {"hits": 1, "total": 4, "value": 0.25}), "25.0%（1／4）")
        self.assertEqual(words.metric_text("seconds", 65), "1分05秒")

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


class MachineParties(unittest.TestCase):
    def test_every_machine_party_has_a_call_end_word(self):
        """電話口が機械だった種別（core の call_report.MACHINE_PARTIES）のすべてに、依頼者へ渡す「通話の終わり方」の言葉がある（設計書 7-3-4 の4番目）。"""
        from salescall.core import call_report
        from salescall.output import result_rows
        for p in call_report.MACHINE_PARTIES:
            with self.subTest(party=p):
                self.assertIn(f"machine:{p}", words.CALL_END_CLIENT)
                self.assertEqual(result_rows.call_end({"state": "ended", "machine": p}, None), words.CALL_END_CLIENT[f"machine:{p}"])
        self.assertEqual(result_rows.call_end({"state": "ended", "machine": None}, None), words.ATTEMPT_STATES["ended"].client)


class QuestionChoices(unittest.TestCase):
    def test_value_questions_do_not_show_the_inner_mark_as_a_choice(self):
        """値を伺う質問の選択肢（core の中の印 VALUE_CHOICE）は画面に出さない。選択肢で答える型だけ選択肢を並べる。"""
        from types import SimpleNamespace
        from salescall.core import business_settings
        value = SimpleNamespace(type="datetime", choices=(business_settings.VALUE_CHOICE,))
        self.assertEqual(words.question_choices(value), "（値を伺います）")
        self.assertNotIn(business_settings.VALUE_CHOICE, words.question_choices(value))
        pick = SimpleNamespace(type="choice", choices=("カード", "現金"))
        self.assertEqual(words.question_choices(pick), "カード・現金")


class CallbackWords(unittest.TestCase):
    def test_every_callback_reason_and_until_reason_has_a_screen_word(self):
        """かけ直しの申し出の理由と「〇時まで」の理由の画面の言葉は、core の並びのすべてにちょうど1つずつ（足したら言葉も足す＝ここが落ちる）。"""
        from salescall.core import call_report
        self.assertEqual(set(words.CALLBACK_REASON_WORDS), set(call_report.CALLBACK_REASONS))
        self.assertEqual(set(words.UNTIL_WHY_WORDS), set(call_report.UNTIL_WHYS))


class ExclusionWords(unittest.TestCase):
    def test_every_exclusion_level_has_a_clear_screen_name(self):
        """行の「かけない」の印の画面の名前は、core の段のすべてにあり、発信禁止と取り違えない言葉（「かけない」だけの名前を使わない）。"""
        from salescall.core import exclusions
        self.assertEqual(set(words.EXCLUSION_WORDS), set(exclusions.LEVEL_NAMES))
        self.assertEqual(set(words.EXCLUSION_CHOICE_HELP), set(exclusions.LEVEL_NAMES))   # 選び肢ごとの説明も、どの印にも1つずつ
        self.assertNotIn("かけない", words.EXCLUSION_WORDS.values())
        self.assertNotIn("強制かけない", words.EXCLUSION_WORDS.values())


class ColorRoles(unittest.TestCase):
    def test_colors_come_from_one_table_and_follow_the_quiet_rule(self):
        """色の役割は words.TONES の1か所（設計書 7-1：ふだんは色なし。止まっている・決めることだけに色）。"""
        from salescall.core import health_rules
        self.assertEqual(words.notice_tone("stopped", False), "stop")                   # 止めた知らせは赤（前は青）
        self.assertEqual(words.notice_tone("month_ahead_unanswered", True), "wait")    # 急ぎは橙（赤は止まっているときだけ）
        self.assertEqual(words.notice_tone("resumed", False), "ok")
        self.assertEqual(words.line_tone(health_rules.LineStage.HEALTHY), "")          # 元気な回線は色なし（前は緑が11本並んだ）
        others = [st for st in health_rules.LineStage if st.name != "HEALTHY"]
        self.assertTrue(others and all(words.line_tone(st) == "wait" for st in others))   # 確かめ中・休みは、どこでも橙
        self.assertEqual(set(words.TONES.values()), {"stop", "wait", "ok", "info", ""})


class AnswerMarks(unittest.TestCase):
    def test_every_answer_mark_has_a_word(self):
        """必須の質問の答えの数の分け方（core の judge.ITEM_MARKS と not_asked）のすべてに、画面の言葉がある。"""
        from salescall.core import judge
        self.assertEqual(set(words.ITEM_MARK_WORDS), set(judge.ITEM_MARKS) | {"not_asked"})


if __name__ == "__main__":
    unittest.main()


class NotForClientGaps(unittest.TestCase):
    """依頼者へ出さない言葉の確かめの穴（全角・半角・大文字・漢字の隣・1語の内部の名前）。"""

    def test_each_way_of_writing_is_caught(self):
        from salescall.output import words as w
        for text, hit in (("ＡＩが話した", "AI"), ("ｼｽﾃﾑの都合", "システム"), ("DO_NOT_CALL", "DO_NOT_CALL"),
                          ("do_not_call電話", "do_not_call"), ("holdの行", "hold"), ("gate.same_contact_min_interval秒", "gate.same_contact_min_interval")):
            with self.subTest(text=text):
                self.assertIn(hit, w.not_for_client(text))
        for text in ("Taibot社", "最後までお話しできました", "管理番号"):
            with self.subTest(text=text):
                self.assertEqual(w.not_for_client(text), [])


class DncImportFields(unittest.TestCase):
    def test_every_field_of_the_dnc_import_has_a_word(self):
        from salescall.core import dnc_import
        from salescall.output import words as w
        self.assertEqual(set(w.DNC_IMPORT_FIELD_WORDS), set(dnc_import.FIELDS))

    def test_every_speaker_has_a_word(self):
        from salescall.core import conversation_io
        from salescall.output import words as w
        self.assertEqual(set(w.SPEAKER_WORDS), set(conversation_io.SPEAKERS))

    def test_every_ring_test_purpose_has_a_word(self):
        import re as _re
        from pathlib import Path
        from salescall.output import words as w
        sql = (Path(__file__).resolve().parents[1] / "db" / "migrations" / "0134_test_ring_trouble_system_names.sql").read_text(encoding="utf-8")
        m = _re.search(r"purpose\s+text NOT NULL CHECK \(purpose IN \(([^)]*)\)", sql)
        self.assertIsNotNone(m, "鳴らした目的の決まりが見つかりません")
        self.assertEqual(set(w.RING_PURPOSE_WORDS), set(_re.findall(r"'([a-z_]+)'", m.group(1))))
        self.assertEqual(set(w.RING_WORDS), {True, False, None})

    def test_every_notice_kind_has_a_short_screen_name_without_developer_notes(self):
        import re as _re
        from salescall.core import notify
        from salescall.output import words as w
        self.assertEqual(set(w.NOTICE_KIND_WORDS), set(notify.KINDS))
        for k, v in w.NOTICE_KIND_WORDS.items():
            with self.subTest(kind=k):
                self.assertLessEqual(len(v), 40)
                self.assertIsNone(_re.search(DEV_NOTE, v))


class PlainCoreTexts(unittest.TestCase):
    def test_core_refusals_lose_internal_ids_and_use_screen_names(self):   # 取り込みの断りの文（2026-11-21）：内部の番号・「実施回」を画面に出さない
        got = words.plain("この実施回の業務設定の版が、確かめに通りません（その版の業務設定の中身がありません：cee21249-c094-44a2-adb5-3b0c90ea946b）。")
        self.assertNotRegex(got, r"[0-9a-f]{8}-[0-9a-f]{4}")
        self.assertNotIn("実施回", got)
        self.assertIn("お客様リストの電話の台本の版", got)



class SheetWords(unittest.TestCase):
    def test_every_choice_of_the_business_settings_has_the_sheet_word(self):
        """版の画面に出す選び肢の値は、どれも取り込みの言葉の表（core の business_import.LABELS）にある（words.sheet_word が「―」にならない）。"""
        from salescall.core import business_settings
        from salescall.output import words
        for field, values in business_settings.POLICIES.items():
            for v in values:
                with self.subTest(field=field, value=v):
                    self.assertNotEqual(words.sheet_word(field, v), words.NO_WORD)
