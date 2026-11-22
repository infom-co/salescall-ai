"""システムの標準の想定問答の画面の試験（持ち主 CL-3。管理者だけ。CL-2 4-211 の (b)。置くのは core の business_settings.save_standard の1か所）。"""

from __future__ import annotations

import re
import unittest
from urllib.parse import unquote

from fastapi.testclient import TestClient

from salescall.core import business_settings
from salescall.web.app import create_app
from salescall.web.context import Person
from tests.support import ADMIN, World, app_access

SECRET = b"test-only-secret"


class StandardAnswers(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.s = self.w.setup()
        self.admin = TestClient(create_app(app_access(), lambda r: Person("試験の管理者", True, self.s["tenant"], "架空の契約者"), SECRET))
        self.operator = TestClient(create_app(app_access(), lambda r: Person("試験の契約者", False, self.s["tenant"], "架空の契約者"), SECRET))

    def tearDown(self):
        self.w.a.in_system(ADMIN, lambda tx: business_settings.save_standard(tx, {"answers": []}))   # いまの標準を空に戻す（ほかの試験のため）

    def now(self):
        return self.w.a.in_system(ADMIN, business_settings.current_standard).answers

    def post(self, data):
        tok = re.search(r'<meta name="form-token" content="([^"]+)"', self.admin.get("/admin/standard").text).group(1)
        return self.admin.post("/admin/standard", data={"form_token": tok, **data}, follow_redirects=False)

    def test_the_admin_adds_changes_and_drops_standard_answers(self):
        self.assertEqual(self.operator.get("/admin/standard").status_code, 404)  # 管理者だけ
        r = self.post({"name": ["架空の問いA", "", ""], "text": ["架空の答えAです。", "", ""]})
        self.assertIn("ok=1", r.headers["location"])
        self.assertEqual(self.now(), (("架空の問いA", "架空の答えAです。"),))
        page = self.admin.get("/admin/standard").text
        self.assertIn('value="架空の問いA"', page)
        r = self.post({"name": ["架空の問いA", "架空の問いB", "", ""], "text": ["架空の答えAを直しました。", "架空の答えBです。", "", ""], "drop": ["0"]})
        self.assertIn("ok=1", r.headers["location"])
        self.assertEqual(self.now(), (("架空の問いB", "架空の答えBです。"),))  # 外した問いは無くなり、足した問いが入る
        same = self.post({"name": ["架空の問いB", ""], "text": ["架空の答えBです。", ""]})
        self.assertIn("いまの版と同じ中身", unquote(same.headers["location"]))   # 変えていなければ版を作らない
        bad = self.post({"name": ["架空の問いC"], "text": [""]})
        self.assertIn("ok=0", bad.headers["location"])                         # 答えの無い問いは core が断る（何も置かない）
        self.assertEqual(self.now(), (("架空の問いB", "架空の答えBです。"),))


if __name__ == "__main__":
    unittest.main()
