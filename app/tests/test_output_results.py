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


class Csv(unittest.TestCase):
    def _row(self, **kw):
        base = dict(client_ref="A-1", company="架空商事", person="山田様（部長）", phone="03-0000-0001", handling="済み", result="完了",
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
        self.assertEqual(got[:4], ["'@1", "'=HYPERLINK(\"x\")", "'+1", "'-2"])   # 管理番号・会社名・お呼びした方・電話番号

    def test_the_file_name_is_checked_and_safe(self):
        from datetime import date
        self.assertEqual(files.file_name("架空/商事", "11月分", date(2026, 11, 18)), "架空_商事_11月分_結果_2026-11-18.csv")
        with self.assertRaises(files.ClientWordsLeak):
            files.file_name("架空商事", "AI検証の回", date(2026, 11, 18))

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

    def test_a_run_of_another_client_cannot_be_written_out_here(self):
        other = f"/c/{self.a['client']}/b/{self.a['business']}/runs/{self.y['run']}/results.csv"
        self.assertEqual(self.http.get(other).status_code, 404)


if __name__ == "__main__":
    unittest.main()
