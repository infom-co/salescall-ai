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
        rows = owner_access().in_system(SYSTEM, lambda tx: tx.all("SELECT key, help, choices, choice_words, confirm_text FROM salescall.setting_definitions"))
        dev = re.compile(r"(?<![A-Za-z])[a-z_]+\.[a-z_]+|Intent|DESIGN|✅|⏳|⛔|20\d\d-\d|CL-\d|計画書|設計書|ユーザー")
        texts = {r["key"]: [r["help"], *(r["choice_words"] or {}).values(), *([r["confirm_text"]] if r["confirm_text"] else [])] for r in rows}   # 確かめの文＝0279
        texts.update({k: [v] for k, v in help_texts.FIELDS.items()})
        # 画面の呼び方（CL-3 の 64・65。✅ 2026-11-20 管理者「イ」・2026-11-21 管理者「伺い1：ア」）：「実施回」でなく「お客様リスト」・お客様の1件を
        # 「行」と呼ばない（ファイルの「1行に」「3行目」のように数のあとの行と、ほかの言葉の一部は除く）・止めたものを動かすのは「解く」でなく「再開する」
        old_names = re.compile(r"実施回|(?<![0-9０-９実銀発進並移旅執施運代流慣修苦興急刊挙一改通試])行(?=[にをのではが（])|解(?:く|か|き|け|いた|いて)")
        for key, words in texts.items():
            for text in words:
                with self.subTest(key=key, text=text[:30]):
                    self.assertIsNone(dev.search(text))
                    self.assertIsNone(old_names.search(text))
        for r in rows:
            with self.subTest(key=r["key"]):
                choices = set(r["choices"] or ())
                self.assertEqual(set(owner_access().in_system(SYSTEM, lambda tx: help_texts.choice_words(tx, r["key"]))), choices)
                self.assertEqual(owner_access().in_system(SYSTEM, lambda tx: help_texts.explain(tx, r["key"])), r["help"])   # 画面は help を出す
                for value in choices:   # 選び肢の英字の名前は、説明にも言葉にも出さない
                    self.assertNotIn(value, r["help"])
        with self.assertRaises(Rejected) as e:   # 選び肢の言葉の欠けた定義は入れられない
            owner_access().in_system(SYSTEM, lambda tx: tx.execute(
                "INSERT INTO salescall.setting_definitions (key, merge_kind, value_type, meaning, design_ref, finest_scope, choices, help, choice_words,"
                " screen_group) VALUES ('test.choice', 'override', 'choice', '試験', '試験', 'business', ARRAY['a', 'b'], '試験', '{\"a\": \"エー\"}', 'call')"))
        self.assertEqual(e.exception.constraint, "setting_definitions_choice_words")

    def test_every_setting_is_in_one_screen_group_and_the_confirm_window_follows_one_rule(self):
        """画面のまとまり（0279＝CL-3 のお願い 70）：設定の定義の1か所が持ち、中身で分ける（名前の頭の言葉では分けない）。どのまとまりにも設定が
        1つ以上あり、まとまりの無い定義は入れられない。CL-3 が挙げた例＝断られたお客様の扱い（名前の頭が business. と calling.）は1つの
        まとまり、時間帯の区切り（retry.）はかけてよい時間帯（calling.）と同じまとまり。
        確かめの文（confirm_text。お願い 70 の追記）：あれば画面が確かめの窓を通す。付ける決まりは1つ＝変えると、断られた・苦情のあったお客様や
        「もうかけないで」と言われた番号にかけることになる設定と、契約者の確かめを通さずにかける・決めることになる設定。空の文は入れられない。
        説明 help に書いた効く範囲・一緒に替わること（検証モード・電話の型・品質の段）は、窓の文でも落とさない（⛔ 2026-11-22 の読み直しで、
        検証モードを外すと品質の段も無くなること・確認の電話の業務だけに効くこと・検証モードでは効かないことを落としていた）。"""
        from salescall.core.access import Rejected
        got = owner_access().in_system(SYSTEM, lambda tx: {r["key"]: r for r in tx.all(
            "SELECT key, screen_group::text AS grp, confirm_text, help FROM salescall.setting_definitions")})
        kinds = owner_access().in_system(SYSTEM, lambda tx: [r["g"] for r in tx.all(
            "SELECT unnest(enum_range(NULL::salescall.setting_group))::text AS g")])
        self.assertEqual(sorted(set(kinds) - {r["grp"] for r in got.values()}), [])          # 空のまとまりは無い
        self.assertEqual({got[k]["grp"] for k in ("business.call_declined_again", "business.declined_wait_days", "calling.call_after_refusal",
                                                  "dnc.former_refusal_calls_new_person")}, {"refusal"})
        self.assertEqual(got["retry.time_band_boundaries"]["grp"], got["calling.hours"]["grp"])
        self.assertEqual(sorted(k for k, r in got.items() if r["confirm_text"]),
                         ["approval.human", "business.call_declined_again", "calling.call_after_refusal", "decision.undecided_as_decided",
                          "dnc.former_refusal_calls_new_person"])
        scope_words = ("検証モード", "確認の電話", "顧客獲得型", "品質の段")
        self.assertEqual({k: [w for w in scope_words if w in r["help"] and w not in r["confirm_text"]]
                          for k, r in got.items() if r["confirm_text"]},
                         {k: [] for k, r in got.items() if r["confirm_text"]})
        insert = ("INSERT INTO salescall.setting_definitions (key, merge_kind, value_type, meaning, design_ref, finest_scope, help, screen_group,"
                  " confirm_text) VALUES ('test.group', 'override', 'integer', '試験', '試験', 'business', '試験', %s, %s)")
        with self.assertRaises(Rejected):   # まとまりの無い定義は入れられない
            owner_access().in_system(SYSTEM, lambda tx: tx.execute(insert, (None, None)))
        with self.assertRaises(Rejected) as e:   # 空の確かめの文は入れられない（文が無いなら NULL＝窓を通さない）
            owner_access().in_system(SYSTEM, lambda tx: tx.execute(insert, ("call", " ")))
        self.assertEqual(e.exception.constraint, "setting_definitions_confirm_text")
