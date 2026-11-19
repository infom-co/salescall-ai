"""ログインの試験（持ち主 CL-3。web/login.py・/login・/logout）。ログインの表・パスワード・印は core の people（CL-2 4-433）。
試験の取引の入口（ログインを自動で付ける TestAccess）を通らず、本番と同じ入口（Access）で流し、データベースの範囲の確かめ
（salescall.check_scope）の中で、ログインした方の印で画面が動くことを確かめる。架空のデータだけ。"""

from __future__ import annotations

import os
import re
import unittest
import uuid

from fastapi.testclient import TestClient

from salescall.core import people
from salescall.core.access import Access
from salescall.web import login as web_login
from salescall.web.app import create_app
from tests.support import ADMIN, World, app_access

SECRET = b"test-only-secret"
PASSWORD = "試験のログインの合言葉です12345"


class Login(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.a = World().setup()
        cls.login_id = f"test-login-{uuid.uuid4()}"
        cls.name = "架空の契約者の担当"
        app_access().in_system(ADMIN, lambda tx: people.create(tx, tenant_id=cls.a["tenant"], kind="operator", name=cls.name,
                                                               login_id=cls.login_id, password=PASSWORD))
        real = Access(os.environ["SALESCALL_TEST_APP_DSN"])   # 本番と同じ入口（ログインを付け足さない）
        cls.http = TestClient(create_app(real, web_login.identify(real), SECRET), base_url="https://testserver")

    def test_without_a_login_every_page_goes_to_the_login_page(self):
        r = TestClient(self.http.app, base_url="https://testserver").get("/t", follow_redirects=False)
        self.assertEqual((r.status_code, r.headers["location"]), (303, "/login"))
        page = self.http.get("/login").text
        self.assertIn(web_login.NOTE_2FA, page)                                  # 二段階認証の注意の文（✅ 2026-10-18 管理者）

    def test_a_wrong_password_says_only_that_it_is_wrong(self):
        fresh = TestClient(self.http.app, base_url="https://testserver")
        for login_id in (self.login_id, f"no-such-{uuid.uuid4()}"):               # 在るかどうかで言い方を変えない
            with self.subTest(login_id=login_id):
                r = fresh.post("/login", data={"login_id": login_id, "password": "違う合言葉です"}, follow_redirects=False)
                self.assertEqual(r.status_code, 401)
                self.assertIn(web_login.WRONG, r.text)
                self.assertNotIn(web_login.COOKIE, r.headers.get("set-cookie", ""))

    def test_a_login_opens_the_screens_of_its_contractor_and_works_under_the_database_check(self):
        fresh = TestClient(self.http.app, base_url="https://testserver")
        r = fresh.post("/login", data={"login_id": self.login_id, "password": PASSWORD}, follow_redirects=False)
        self.assertEqual((r.status_code, r.headers["location"]), (303, "/t"))
        cookie = r.headers["set-cookie"].lower()
        for part in ("httponly", "secure", "samesite=strict"):
            self.assertIn(part, cookie)
        home = fresh.get("/t")
        self.assertEqual(home.status_code, 200)
        self.assertIn(self.name, home.text)                                       # 上の帯に、ログインした方の名前
        tok = re.search(r'<meta name="form-token" content="([^"]+)"', home.text).group(1)
        made = fresh.post("/t/clients", data={"form_token": tok, "name": f"架空のログインの依頼者{uuid.uuid4().hex[:6]}"},
                          follow_redirects=False)
        self.assertEqual(made.status_code, 303)                                   # 変える操作も、ログインの印の取引で通る（範囲の確かめの中）
        self.assertNotIn("ok=0", made.headers["location"])
        other = World().setup()
        self.assertEqual(fresh.get(f"/c/{other['client']}").status_code, 404)    # ほかの契約者の依頼者は見えない
        tok = re.search(r'<meta name="form-token" content="([^"]+)"', fresh.get("/t").text).group(1)
        out = fresh.post("/logout", data={"form_token": tok}, follow_redirects=False)
        self.assertEqual(out.headers["location"], "/login")
        self.assertEqual(fresh.get("/t", follow_redirects=False).status_code, 303)   # ログアウトしたら、もう開けない


if __name__ == "__main__":
    unittest.main()
