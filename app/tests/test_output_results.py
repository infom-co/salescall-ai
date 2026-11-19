"""依頼者へ渡す結果の行と CSV の試験（持ち主 CL-3。output/result_rows.py・output/files.py・書き出しの画面）。架空のデータだけ。"""

from __future__ import annotations

import csv
import io
import re
import unittest

from fastapi.testclient import TestClient

from salescall.output import files, result_rows, words
from salescall.web.app import create_app
from salescall.web.context import Person
from tests.support import OPERATOR, World, app_access

SECRET = b"test-only-secret"


class CallEnd(unittest.TestCase):
    """通話の終わり方（設計書 7-3-4 の順）。"""

    def test_each_case_in_the_order_of_the_design(self):
        cases = [
            (None, "end", "お電話していません"), (None, "call", "まだおかけしていません"), (None, None, "まだおかけしていません"),
            ({"state": "no_answer", "notice": "power_off"}, "call", "電源が入っていないか圏外"),
            ({"state": "no_answer", "notice": None}, "call", "応答なし"), ({"state": "busy"}, "call", "話し中"),
            ({"state": "call_failed", "notice": "not_in_use"}, "end", "使われていない番号"),
            ({"state": "call_failed", "notice": None}, "call", "つながりませんでした"),
            ({"state": "technical_error"}, "call", "こちらの都合で途中で終わりました"),
            ({"state": "dropped", "hangup_side": "callee"}, "call", "お客様が途中で切られました"),
            ({"state": "dropped", "hangup_side": "line"}, "call", "回線が途中で切れました"),
            ({"state": "dropped", "hangup_side": "unknown"}, "call", "途中で切れました"),
            ({"state": "ended"}, "end", "最後までお話しできました"), ({"state": "in_conversation"}, "call", "不明"),
        ]
        for last, kind, want in cases:
            with self.subTest(last=last, kind=kind):
                self.assertEqual(result_rows.call_end(last, kind), want)

    def test_no_word_for_the_client_says_anything_about_the_system(self):
        for w in list(words.CALL_END_CLIENT.values()) + list(result_rows.COLUMNS):
            with self.subTest(word=w):
                self.assertEqual(words.not_for_client(w), [])


class AnswerCells(unittest.TestCase):
    """質問ごとの2列（DESIGN.md 7-3-2）。✅ 2026-11-08・2026-11-20 管理者「「-」「✕」以外で記載する」（CL-1 の連絡帳 4-206）：取れなかった答えは、
    答えの欄にお客様の言葉。言葉が無ければ状態の短い言葉。取れた答えは値のまま、言い切っていない返事のときだけ横の欄にお客様の言葉。"""
    from types import SimpleNamespace as _Q
    VALUE = _Q(id="Q1", text="ご希望の日はいつでしょうか？", ask_about="ご希望の日", type="date")
    CHOICE = _Q(id="Q2", text="ご予約はされていますか？", ask_about=None, type="yes_no")

    def cell(self, q, item, not_asked=()):
        return result_rows.answer_cells(q, item, set(not_asked))

    def test_a_taken_answer_is_the_value_and_words_only_when_not_said_plainly(self):
        self.assertEqual(self.cell(self.VALUE, {"state": "confirmed", "value": {"text": "15日"}})[:2], ("15日", ""))
        self.assertEqual(self.cell(self.CHOICE, {"state": "accepted", "condition": "いいえ", "hedged": "していないはずですが"})[:2],
                         ("いいえ", "していないはずですが"))                                # 案C：値はそのまま・横の欄にお客様の言葉

    def test_an_answer_not_taken_is_written_in_the_customers_words(self):
        for state in ("dont_know", "refused", "unresolved", "unclear"):
            with self.subTest(state=state):
                got = self.cell(self.CHOICE, {"state": state, "hedged": "まだ決めていないんですよね／上と相談します"})
                self.assertEqual(got, ("まだ決めていないんですよね／上と相談します", "", None))   # 「-」「✕」ではない

    def test_an_answer_without_words_says_its_state_shortly(self):
        for state, word in (("refused", "お答えを控えられた"), ("dont_know", "わからないとのこと"), ("empty", "伺っていない")):
            with self.subTest(state=state):
                self.assertEqual(self.cell(self.CHOICE, {"state": state}), (word, "", word))
        self.assertEqual(self.cell(self.CHOICE, None)[0], "伺っていない")
        self.assertEqual(self.cell(self.CHOICE, {"state": "accepted", "condition": "はい"}, not_asked=("Q2",))[0], result_rows.NOT_ASKED)

    def test_no_mark_or_system_word_reaches_the_client(self):
        for state, w in words.ITEM_STATES.items():
            with self.subTest(state=state):
                self.assertNotIn(w.client, ("-", "✕"))
                self.assertEqual(words.not_for_client(w.client or ""), [])

    def test_the_words_are_shortened_or_kept_whole_by_the_business_setting(self):   # ✅ 2026-11-20 管理者「ウ」「業務ごと」
        long = "それは上の者と相談してから決めることになっていますので、今すぐにはお答えできないんです"
        item = {"state": "dont_know", "hedged": f"えっと／まだ決めていないんですよね／{long}"}
        short = self.cell(self.CHOICE, item)[0]
        self.assertEqual(short, f"まだ決めていないんですよね／{long[:result_rows.WORDS_MAX_CHARS]}")   # 最後の2つまで・1つ40字まで
        whole = result_rows.answer_cells(self.CHOICE, item, set(), "full")[0]
        self.assertEqual(whole, item["hedged"])                                    # 全部残す業務は、言ったとおり全部

    def test_every_question_we_could_not_answer_is_listed(self):   # 計画書 11-3「お答えできなかったご質問は必ず載せる」（判定の中身 callee_questions）
        detail = {"callee_questions": [{"said": "解約金はかかりますか？", "answered": False, "answer": None},
                                       {"said": "営業時間は何時までですか？", "answered": True, "answer": "19時までです。"},
                                       {"said": "支店でも使えますか？", "answered": False, "answer": None},
                                       {"said": "請求書は郵送ですか？" + "あ" * 50, "answered": False, "answer": None}]}
        got = result_rows.unanswered_questions(detail)
        self.assertEqual(got.split("／")[:2], ["解約金はかかりますか？", "支店でも使えますか？"])   # 答えた質問は入れない・数は減らさない
        self.assertEqual(len(got.split("／")), 3)
        self.assertEqual(len(got.split("／")[2]), result_rows.WORDS_MAX_CHARS)                   # 縮める業務は1つずつの長さだけ
        self.assertTrue(result_rows.unanswered_questions(detail, "full").endswith("あ" * 50))
        self.assertEqual(result_rows.unanswered_questions({}), "")

    def test_the_headers_are_two_per_question(self):
        self.assertEqual(result_rows.question_headers([self.VALUE, self.CHOICE]),
                         ("ご希望の日", "ご希望の日（お客様の言葉）", "ご予約はされていますか？", "ご予約はされていますか？（お客様の言葉）"))


class Csv(unittest.TestCase):
    def _row(self, **kw):
        base = dict(client_ref="A-1", company="架空商事", person="山田様（部長）", full_name="", department="", phone="03-0000-0001",
                    handling="済み", result="完了",
                    call_end="最後までお話しできました", dialed=2, last_at="11月18日15時")
        return result_rows.ResultRow(**{**base, **kw})

    def test_the_file_opens_in_excel_with_the_columns(self):
        data = files.to_csv([self._row()])
        self.assertTrue(data.startswith("\ufeff".encode("utf-8")))
        got = list(csv.reader(io.StringIO(data.decode("utf-8-sig"))))
        self.assertEqual(got[0], list(result_rows.COLUMNS))
        self.assertEqual(got[1][0], "A-1")

    def test_a_system_word_stops_the_file(self):
        with self.assertRaises(files.ClientWordsLeak):
            files.to_csv([self._row(call_end="AIが終えた")])

    def test_a_cell_that_looks_like_a_formula_stays_text(self):
        got = list(csv.reader(io.StringIO(files.to_csv([self._row(company="=HYPERLINK(\"x\")", client_ref="@1", person="+1", phone="-2")])
                                          .decode("utf-8-sig"))))[1]
        row = dict(zip(result_rows.COLUMNS, got))
        self.assertEqual([row["管理番号"], row["会社名"], row["お呼びする方"], row["電話番号"]], ["'@1", "'=HYPERLINK(\"x\")", "'+1", "'-2"])

    def test_the_blanks_to_fill_say_so_in_the_csv(self):   # ✅ 2026-11-18 管理者「(ア)でOK」
        r = self._row(full_name="", department="営業部", to_fill=("お呼びする方のフルネーム",))
        got = dict(zip(result_rows.COLUMNS, list(csv.reader(io.StringIO(files.to_csv([r]).decode("utf-8-sig"))))[1]))
        self.assertEqual(got["お呼びする方のフルネーム"], result_rows.PLEASE_FILL)
        self.assertEqual(got["お呼びする方の部署"], "営業部")
        self.assertTrue(set(result_rows.TO_FILL_COLUMNS.values()) <= set(result_rows.COLUMNS))
        from salescall.core import business_settings
        self.assertEqual(set(result_rows.TO_FILL_COLUMNS), set(business_settings.WHO_UNCLEAR_FIELDS))   # 欄の正本と突き合わせる

    def test_the_excel_file_marks_the_blanks_in_red_and_keeps_formulas_as_text(self):   # ✅ 2026-11-18 管理者「(ア)でOK」・CL-2 4-439 の 6
        import io as _io
        from openpyxl import load_workbook
        row = self._row(company="=HYPERLINK(\"x\")", to_fill=("お呼びする方のフルネーム",))
        wb = load_workbook(_io.BytesIO(files.write("xlsx", [row])))
        ws = wb.active
        head = [c.value for c in ws[1]]
        self.assertEqual(head[:len(result_rows.COLUMNS)], list(result_rows.COLUMNS))
        cells = {h: ws.cell(row=2, column=i + 1) for i, h in enumerate(head)}
        self.assertEqual((cells["会社名"].value, cells["会社名"].data_type), ("=HYPERLINK(\"x\")", "s"))   # 式として動かさない
        blank = cells["お呼びする方のフルネーム"]
        self.assertIsNone(blank.value)                                            # 依頼者に入れていただく空欄
        self.assertEqual(blank.fill.fgColor.rgb[-6:], result_rows.TO_FILL_BACKGROUND.lstrip("#"))
        self.assertEqual(blank.font.color.rgb[-6:], result_rows.TO_FILL_TEXT.lstrip("#"))
        self.assertTrue(files.file_name("依頼者", "実施回", __import__("datetime").date(2026, 11, 20), "xlsx").endswith(".xlsx"))

    def test_the_file_name_is_checked_and_safe(self):
        from datetime import date
        self.assertEqual(files.file_name("架空/商事", "11月分", date(2026, 11, 18)), "架空_商事_11月分_結果_2026-11-18.csv")
        with self.assertRaises(files.ClientWordsLeak):
            files.file_name("架空商事", "AI検証の回", date(2026, 11, 18))

    def test_the_red_is_readable_and_the_same_on_the_screen(self):   # ✅ 2026-11-18 管理者「文字が見えにくい奇抜な赤にならないように」
        from pathlib import Path

        def lum(h):
            c = [int(h.lstrip("#")[i:i + 2], 16) / 255 for i in (0, 2, 4)]
            c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
            return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]
        a, b = sorted((lum(result_rows.TO_FILL_TEXT), lum(result_rows.TO_FILL_BACKGROUND)), reverse=True)
        self.assertGreaterEqual((a + 0.05) / (b + 0.05), 4.5)
        css = (Path(__file__).resolve().parents[1] / "src" / "salescall" / "web" / "static" / "app.css").read_text(encoding="utf-8")
        light = css[:css.index("@media (prefers-color-scheme: dark)")]
        self.assertIn(f"--stop: {result_rows.TO_FILL_TEXT}", light)
        self.assertIn(f"--stop-soft: {result_rows.TO_FILL_BACKGROUND}", light)

    def test_customer_data_is_not_checked(self):   # 会社名はお客様・依頼者のデータ（確かめない）
        self.assertTrue(files.to_csv([self._row(company="AIシステム開発 株式会社")]))


class Screen(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        w = World()
        cls.a = w.setup()
        cls.y = w.setup(tenant=cls.a["tenant"])
        person = Person("試験の契約者A", False, cls.a["tenant"], "契約者A")
        cls.http = TestClient(create_app(app_access(), lambda r: person, SECRET))
        cls.base = f"/c/{cls.a['client']}/b/{cls.a['business']}/runs/{cls.a['run']}/results.csv"

    def _submissions(self):
        return app_access().in_client(OPERATOR, self.a["tenant"], self.a["client"], lambda tx: tx.one(
            "SELECT count(*) AS n FROM salescall.submission_files")["n"])

    def test_checking_does_not_keep_a_copy_and_submitting_does(self):
        before = self._submissions()
        r = self.http.get(self.base)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.headers["content-type"].startswith("text/csv"))
        self.assertEqual(self._submissions(), before)
        tok = re.search(r'<meta name="form-token" content="([^"]+)"', self.http.get("/t").text).group(1)
        r = self.http.post(self.base, data={"form_token": tok}, follow_redirects=False)
        self.assertIn("ok=0", r.headers["location"])        # 確定した結果がまだ無い＝依頼者へは何も渡さない（控えにも置かない）
        self.assertEqual(self._submissions(), before)
        forged = self.http.post(self.base, data={"form_token": "でたらめ"}, follow_redirects=False)
        self.assertIn("ok=0", forged.headers["location"])
        self.assertEqual(self._submissions(), before)

    def test_the_file_of_client_x_has_rows_of_client_x_only(self):   # S-8
        from salescall.core import customer_rows, phone
        def numbers(w):
            return app_access().in_client(OPERATOR, w["tenant"], w["client"], lambda tx: [
                phone.normalize(r["dial_phone"]).national or r["dial_phone"] for r in customer_rows.progress(tx, w["run"])])
        mine, theirs = numbers(self.a), numbers(self.y)
        self.assertTrue(theirs)
        text = self.http.get(self.base).content.decode("utf-8-sig")
        page = self.http.get(self.base.replace("/results.csv", "/results")).text
        for n in theirs:
            if n not in mine:
                self.assertNotIn(n, text)
                self.assertNotIn(n, page)

    def test_the_hand_over_button_waits_for_a_confirmed_result(self):   # 渡せるかは files.deliverable の1か所（ボタンも渡す処理も）
        page = self.http.get(self.base.replace("/results.csv", "/results")).text
        self.assertIn("確定した結果がまだ無いので、依頼者へは渡せません", page)
        self.assertRegex(page, r'<button class="btn main" type="submit" disabled>Excel で書き出す')
        self.assertRegex(page, r'formaction="[^"]+/results.csv" disabled>CSV で書き出す')

    def test_a_run_of_another_client_cannot_be_written_out_here(self):
        other = f"/c/{self.a['client']}/b/{self.a['business']}/runs/{self.y['run']}/results.csv"
        self.assertEqual(self.http.get(other).status_code, 404)


if __name__ == "__main__":
    unittest.main()
