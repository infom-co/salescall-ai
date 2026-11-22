"""システムの様子の画面の試験（持ち主 CL-3。管理者だけ。設計書 5-13）：祝日の暦の様子・回線の方式・声の一覧。読む・書くのは core の1か所。"""

from __future__ import annotations

import re
import unittest
import uuid
from urllib.parse import unquote

from fastapi.testclient import TestClient

from salescall.web.app import create_app
from salescall.web.context import Person
from tests.support import ADMIN, World, app_access

SECRET = b"test-only-secret"


class AdminSystem(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.s = self.w.setup()
        self.admin = TestClient(create_app(app_access(), lambda r: Person("試験の管理者", True, self.s["tenant"], "架空の契約者"), SECRET))
        self.operator = TestClient(create_app(app_access(), lambda r: Person("試験の契約者", False, self.s["tenant"], "架空の契約者"), SECRET))

    def token(self):
        return re.search(r'<meta name="form-token" content="([^"]+)"', self.admin.get("/admin/system").text).group(1)

    def test_only_the_admin_sees_it_and_it_shows_the_calendar_methods_and_voices(self):
        self.assertEqual(self.operator.get("/admin/system").status_code, 404)
        page = self.admin.get("/admin/system").text
        for part in ('id="calendar"', 'id="line-methods"', 'id="voices"'):
            self.assertIn(part, page)

    def test_a_voice_is_added_and_retired_with_a_reason(self):
        name = "Test" + uuid.uuid4().hex[:6]
        r = self.admin.post("/admin/system/voices", data={"form_token": self.token(), "voice_name": name, "note": "架空の声", "display_gender": "女性"},
                            follow_redirects=False)
        self.assertIn("ok=1", r.headers["location"])
        page = self.admin.get("/admin/system").text
        self.assertIn(name, page)
        self.assertIn("まだ無い（この声では話しません）", page[page.index(name):][:600])   # 声の型が無いうちは話さない
        none = self.admin.post("/admin/system/voices/retire", data={"form_token": self.token(), "voice_name": name, "reason": ""}, follow_redirects=False)
        self.assertIn("ok=0", none.headers["location"])                         # 理由は必ず
        self.assertEqual(self.operator.post("/admin/system/voices/retire", data={"form_token": self.token(), "voice_name": name, "reason": "乗っ取り"},
                                            follow_redirects=False).status_code, 404)
        r = self.admin.post("/admin/system/voices/retire", data={"form_token": self.token(), "voice_name": name, "reason": "架空：試験の声"},
                            follow_redirects=False)
        self.assertIn("ok=1", r.headers["location"])
        retired = self.w.a.in_system(ADMIN, lambda tx: tx.one("SELECT retired_reason FROM salescall.voices WHERE voice_name = %s", (name,)))
        self.assertEqual(retired["retired_reason"], "架空：試験の声")
        bad = self.admin.post("/admin/system/voices", data={"form_token": self.token(), "voice_name": "日本語の名前", "note": "架空"},
                              follow_redirects=False)
        self.assertIn("ok=0", bad.headers["location"])                         # 名前の決まりはデータベースが断る（何も足さない）
        self.assertIn("受け付けられませんでした", unquote(bad.headers["location"]))


if __name__ == "__main__":
    unittest.main()
