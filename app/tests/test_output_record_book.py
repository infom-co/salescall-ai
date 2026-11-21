"""記録のすべての書き出し＝基本の形の試験（持ち主 CL-3。output/record_book・output/files.book・/…/runs/{実施回}/records.xlsx。設計書 7-3-5b）。
✅ 2026-11-21 管理者「基本の形はデータベースを出力することもできるようにします」・伺い3「イ」（契約者の手元の控え）・伺い4「データベースを見て決める」。
架空のデータだけ。"""

from __future__ import annotations

import inspect
import io
import re
import unittest

from fastapi.testclient import TestClient

from salescall.core import call_report, judge
from salescall.output import record_book, words
from salescall.web.app import create_app
from salescall.web.context import Person
from tests.support import OPERATOR, SYSTEM, World, app_access

SECRET = b"test-only-secret"
SHEETS = ["お客様", "番号と連絡先", "電話", "やり取り", "答え", "結果の版", "お客様のお話", "操作の記録"]


class Words(unittest.TestCase):
    def test_every_detail_kind_the_judge_writes_has_a_word(self):
        """判定の中身の種類（judge._detail が書く鍵）は、どれも「お客様のお話」のシートの種類の言葉を持つ（判定が種類を足したら、ここが落ちる）。"""
        src = inspect.getsource(judge._detail)
        keys = set(re.findall(r'\*\*\(\{"([a-z_0-9]+)"', src)) | {"cut", "cut_recorded"} | set(call_report.CONCERN_KINDS) | set(call_report.OFFER_KINDS)
        self.assertEqual(keys - set(words.DETAIL_KIND_WORDS), set())

    def test_every_call_purpose_has_a_word(self):
        got = app_access().in_system(SYSTEM, lambda tx: tx.one(
            "SELECT pg_get_constraintdef(oid) AS d FROM pg_constraint WHERE conname = 'attempts_purpose_kind'"))["d"]
        self.assertEqual(set(words.CALL_PURPOSE_WORDS), set(re.findall(r"'([a-z_]+)'", got)))

    def test_every_result_kind_has_a_word(self):
        kinds = app_access().in_system(SYSTEM, lambda tx: {r["k"] for r in tx.all("SELECT unnest(enum_range(NULL::salescall.result_kind))::text AS k")})
        self.assertEqual(set(words.RESULT_KIND_WORDS), kinds)


class Book(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        w = World()
        cls.a = w.setup()
        cls.b = w.setup()
        w.insert_claim(cls.a)   # 電話1本（予約のまま）
        cls.people = {"A": Person("試験の契約者A", False, cls.a["tenant"], "契約者A"), "B": Person("試験の契約者B", False, cls.b["tenant"], "契約者B")}
        cls.http = TestClient(create_app(app_access(), lambda r: cls.people.get(r.headers.get("x-test-person", "")), SECRET))
        cls.url = f"/c/{cls.a['client']}/b/{cls.a['business']}/runs/{cls.a['run']}/records.xlsx"

    def test_the_sheets_follow_the_records_of_the_database(self):
        got = app_access().in_client(OPERATOR, self.a["tenant"], self.a["client"], lambda tx: record_book.sheets(tx, self.a["run"]))
        self.assertEqual([name for name, _, _ in got], SHEETS)
        for name, header, rows in got:
            with self.subTest(sheet=name):
                self.assertEqual(header[:2], record_book.KEY_HEADER)            # どのシートにも突き合わせの鍵
                self.assertTrue(all(len(r) == len(header) for r in rows))
        for name, header, rows in got:                                       # 記録の値の英字の名前をそのまま出さない（言葉の表から引く）
            for r in rows:
                with self.subTest(sheet=name):
                    self.assertEqual([v for v in r if isinstance(v, str) and re.fullmatch(r"[a-z_]+", v)], [])
        sheet = dict((n, rows) for n, _, rows in got)
        self.assertEqual(len(sheet["お客様"]), 1)
        self.assertEqual(len(sheet["電話"]), 1)
        self.assertGreaterEqual(len(sheet["番号と連絡先"]), 1)

    def test_the_book_downloads_for_its_own_contractor_only(self):
        r = self.http.get(self.url, headers={"x-test-person": "A"})
        self.assertEqual(r.status_code, 200, r.text[:200])
        self.assertIn("attachment", r.headers["content-disposition"])
        from openpyxl import load_workbook   # 試験の中で読むだけ（書き出しの部品は output/files の1か所）
        self.assertEqual(load_workbook(io.BytesIO(r.content)).sheetnames, SHEETS)
        self.assertEqual(self.http.get(self.url, headers={"x-test-person": "B"}).status_code, 404)   # ほかの契約者には見えない

    def test_the_list_page_offers_it_as_a_copy_at_hand(self):
        page = self.http.get(f"/c/{self.a['client']}/b/{self.a['business']}/runs/{self.a['run']}", headers={"x-test-person": "A"}).text
        self.assertIn(words.EXPORT_WORDS["records"], page)
        self.assertIn("records.xlsx", page)


if __name__ == "__main__":
    unittest.main()
