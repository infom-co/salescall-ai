"""ログインの試験（持ち主 CL-3。web/login.py・/login・/logout）。ログインの表・パスワード・印は core の people（CL-2 4-433）。
試験の取引の入口（ログインを自動で付ける TestAccess）を通らず、本番と同じ入口（Access）で流し、データベースの範囲の確かめ
（salescall.check_scope）の中で、ログインした方の印で画面が動くことを確かめる。架空のデータだけ。"""

from __future__ import annotations

import os
import re
import unittest
import uuid
from urllib.parse import unquote

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

    def test_one_changes_the_password_after_the_current_one_is_checked_and_every_login_ends(self):
        login_id = f"test-login-pw-{uuid.uuid4()}"
        app_access().in_system(ADMIN, lambda tx: people.create(tx, tenant_id=self.a["tenant"], kind="operator", name="架空の替える方",
                                                               login_id=login_id, password=PASSWORD))

        def signed_in():
            c = TestClient(self.http.app, base_url="https://testserver")
            c.post("/login", data={"login_id": login_id, "password": PASSWORD}, follow_redirects=False)
            return c
        me, elsewhere = signed_in(), signed_in()
        page = me.get("/t/account").text
        self.assertIn('action="/t/account/password"', page)
        tok = re.search(r'<meta name="form-token" content="([^"]+)"', page).group(1)
        new = "新しい架空の合言葉です67890"
        for data, why in (({"current_password": "違う合言葉です", "new_password": new, "new_password_again": new}, "いまのパスワードが合いません"),
                          ({"current_password": PASSWORD, "new_password": new, "new_password_again": new + "x"}, "2つの欄で違います"),
                          ({"current_password": PASSWORD, "new_password": "短い", "new_password_again": "短い"}, "文字以上")):
            with self.subTest(why=why):
                r = me.post("/t/account/password", data={"form_token": tok, **data}, follow_redirects=False)
                self.assertIn("ok=0", r.headers["location"])
                self.assertIn(why, unquote(r.headers["location"]))
        self.assertEqual(elsewhere.get("/t", follow_redirects=False).status_code, 200)   # 断ったときは、何も変わっていない
        r = me.post("/t/account/password", data={"form_token": tok, "current_password": PASSWORD, "new_password": new,
                                                 "new_password_again": new}, follow_redirects=False)
        self.assertEqual(r.headers["location"], "/login?changed=password")
        self.assertIn(web_login.PASSWORD_CHANGED, me.get("/login?changed=password").text)
        self.assertEqual(elsewhere.get("/t", follow_redirects=False).status_code, 303)   # ほかの所のログインも外れた
        fresh = TestClient(self.http.app, base_url="https://testserver")
        self.assertEqual(fresh.post("/login", data={"login_id": login_id, "password": PASSWORD}, follow_redirects=False).status_code, 401)
        self.assertEqual(fresh.post("/login", data={"login_id": login_id, "password": new}, follow_redirects=False).headers["location"], "/t")

    def test_the_admin_makes_resets_and_stops_logins_and_a_contractor_cannot(self):
        admin_id = f"test-admin-{uuid.uuid4()}"
        app_access().in_system(ADMIN, lambda tx: people.create(tx, tenant_id=self.a["tenant"], kind="admin", name="架空の管理者",
                                                               login_id=admin_id, password=PASSWORD))

        real = Access(os.environ["SALESCALL_TEST_APP_DSN"])
        app = create_app(real, web_login.identify(real), SECRET)   # この試験だけの画面のサーバー（同じ所からのログインの回数の絞りは、サーバーごと）

        def signed_in(login_id, password=PASSWORD):
            c = TestClient(app, base_url="https://testserver")
            r = c.post("/login", data={"login_id": login_id, "password": password}, follow_redirects=False)
            return c, r
        admin, _ = signed_in(admin_id)
        operator, _ = signed_in(self.login_id)
        self.assertEqual(operator.get("/admin/people").status_code, 404)        # 契約者には無い画面
        page = admin.get("/admin/people").text
        self.assertIn(self.login_id, page)
        tok = re.search(r'<meta name="form-token" content="([^"]+)"', page).group(1)
        self.assertEqual(operator.post("/admin/people", data={"form_token": tok, "tenant_id": str(self.a["tenant"]), "kind": "operator",
                                                              "name": "乗っ取り", "login_id": "x", "password": PASSWORD,
                                                              "password_again": PASSWORD}, follow_redirects=False).status_code, 404)
        new_id = f"test-made-{uuid.uuid4()}"
        made = {"form_token": tok, "tenant_id": str(self.a["tenant"]), "kind": "operator", "name": "架空の作った担当", "login_id": new_id,
                "password": PASSWORD, "password_again": PASSWORD}
        self.assertIn("ok=1", admin.post("/admin/people", data=made, follow_redirects=False).headers["location"])
        self.assertIn("ok=0", admin.post("/admin/people", data=made, follow_redirects=False).headers["location"])   # 同じログインIDは core が断る
        them, r = signed_in(new_id)
        self.assertEqual(r.headers["location"], "/t")                             # 作ったログインで入れる
        pid = app_access().in_system(ADMIN, lambda tx: tx.one("SELECT person_id FROM salescall.people WHERE login_id = %s", (new_id,)))["person_id"]
        reset = "決め直した架空の合言葉です13579"
        r = admin.post(f"/admin/people/{pid}/password", data={"form_token": tok, "password": reset, "password_again": reset},
                       follow_redirects=False)
        self.assertIn("ok=1", r.headers["location"])
        self.assertEqual(them.get("/t", follow_redirects=False).status_code, 303)   # 決め直すと、その方のログインは外れる
        self.assertEqual(signed_in(new_id, reset)[1].headers["location"], "/t")
        none = admin.post(f"/admin/people/{pid}/active", data={"form_token": tok, "active": "0", "reason": ""}, follow_redirects=False)
        self.assertIn("ok=0", none.headers["location"])                          # 理由は必ず
        off = admin.post(f"/admin/people/{pid}/active", data={"form_token": tok, "active": "0", "reason": "架空：担当を外れた"}, follow_redirects=False)
        self.assertIn("ok=1", off.headers["location"])
        self.assertEqual(signed_in(new_id, reset)[1].status_code, 401)           # 使えないログインは通らない
        me = app_access().in_system(ADMIN, lambda tx: tx.one("SELECT person_id FROM salescall.people WHERE login_id = %s", (admin_id,)))["person_id"]
        mine = admin.post(f"/admin/people/{me}/active", data={"form_token": tok, "active": "0", "reason": "架空"}, follow_redirects=False)
        self.assertIn("ok=0", mine.headers["location"])                          # ご自分は使えないにできない


if __name__ == "__main__":
    unittest.main()
