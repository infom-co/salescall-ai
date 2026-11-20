"""ログインと安全の画面・ログインの2段目・Google でのログインの試験（持ち主 CL-3。web/app.py の /login/second・/login/google・/t/account）。
確かめるのは core の passkeys・google_login の1か所（CL-2 4-456・4-457）。画面は、問いを渡し、端末の答えを返すだけ。
本番と同じ入口（Access）で流す。端末は CL-2 の試験の端末（ソフトの鍵）、Google は試験の偽物（鍵を渡す）。架空のデータだけ。"""

from __future__ import annotations

import json
import os
import re
import unittest
import uuid

from fastapi.testclient import TestClient

from salescall.core import passkeys, people
from salescall.core.access import Access
from salescall.web import login as web_login
from salescall.web.app import create_app
from tests.support import ADMIN, World, app_access
from tests.test_google_login import CLIENT, FakeGoogle
from tests.test_passkeys import Device

SECRET = b"test-only-secret"
PASSWORD = "試験のログインの合言葉です12345"
SITE = passkeys.Site(rp_id="testserver", origin="https://testserver")


class Security(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.a = World().setup()
        cls.google = FakeGoogle()
        cls.real = Access(os.environ["SALESCALL_TEST_APP_DSN"])   # 本番と同じ入口（ログインを付け足さない）
        cls.app = create_app(cls.real, web_login.identify(cls.real), SECRET, site=SITE, google_client_id=CLIENT,
                             google_keys=lambda: cls.google.keys)

    def person(self):
        login_id = f"test-security-{uuid.uuid4()}"
        app_access().in_system(ADMIN, lambda tx: people.create(tx, tenant_id=self.a["tenant"], kind="operator", name="架空の契約者の担当",
                                                               login_id=login_id, password=PASSWORD))
        return login_id

    def client(self):
        return TestClient(self.app, base_url="https://testserver")

    def log_in(self, http, login_id):
        return http.post("/login", data={"login_id": login_id, "password": PASSWORD}, follow_redirects=False)

    def token(self, http):
        return re.search(r'<meta name="form-token" content="([^"]+)"', http.get("/t/account").text).group(1)

    def register(self, http, device, label="仕事の PC"):
        tok = self.token(http)
        opts = http.post("/t/account/passkey/options", headers={"X-Form-Token": tok})
        self.assertEqual(opts.status_code, 200, opts.text)
        r = http.post("/t/account/passkey", headers={"X-Form-Token": tok},
                      json={"credential": json.loads(device.create(opts.text)), "label": label})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["codes"]

    def test_a_passkey_is_registered_on_the_account_page_and_then_needed_after_the_password(self):
        login_id, device = self.person(), Device(SITE)
        http = self.client()
        self.assertEqual(self.log_in(http, login_id).headers["location"], "/t")       # まだパスキーが無い＝パスワードだけ
        self.assertIn("パスキーはまだありません", http.get("/t/account").text)
        codes = self.register(http, device)
        self.assertEqual(len(codes), passkeys.RECOVERY_CODES)                         # 最初のパスキーのときだけ、予備の合言葉を一度だけ返す
        page = http.get("/t/account").text
        self.assertIn("仕事の PC", page)
        self.assertNotIn(codes[0], page)                                              # 合言葉は、あとから見られない
        second = self.client()
        r = self.log_in(second, login_id)
        self.assertEqual(r.headers["location"], "/login/second")                      # パスワードのあと、パスキーで確かめるまで使えない
        self.assertEqual(second.get("/t", follow_redirects=False).headers["location"], "/login")
        self.assertEqual(second.get("/login", follow_redirects=False).headers["location"], "/login/second")
        opts = second.post("/login/second/options")
        done = second.post("/login/second/verify", content=device.get(opts.text))
        self.assertEqual(done.json(), {"next": "/t"})
        self.assertEqual(second.get("/t").status_code, 200)

    def test_a_recovery_code_works_once_and_a_wrong_answer_does_not_pass(self):
        login_id, device = self.person(), Device(SITE)
        http = self.client()
        self.log_in(http, login_id)
        codes = self.register(http, device)
        other = self.client()
        self.log_in(other, login_id)
        bad = other.post("/login/second/verify", content=Device(SITE).get(other.post("/login/second/options").text))
        self.assertEqual(bad.status_code, 400)                                        # 登録していない端末では通らない
        self.assertEqual(other.post("/login/second/code", data={"code": codes[0]}, follow_redirects=False).headers["location"], "/t")
        again = self.client()
        self.log_in(again, login_id)
        self.assertEqual(again.post("/login/second/code", data={"code": codes[0]}).status_code, 401)   # 同じ合言葉は2度使えない

    def test_the_account_actions_need_the_page_token(self):
        login_id = self.person()
        http = self.client()
        self.log_in(http, login_id)
        self.assertEqual(http.post("/t/account/passkey/options").status_code, 400)          # 札が無ければ断る
        self.assertEqual(http.post("/t/account/passkey/options", headers={"X-Form-Token": "x.y"}).status_code, 400)

    def test_a_linked_google_account_logs_in_and_an_unlinked_one_is_told_to_link_first(self):
        login_id = self.person()
        sub = f"kakuu-{uuid.uuid4()}"
        fresh = self.client()
        r = fresh.post("/login/google", data={"credential": self.google.token(sub)})
        self.assertEqual(r.status_code, 400)
        self.assertIn("まだ結び付いていません", r.json()["error"])
        self.assertNotIn(web_login.COOKIE, r.headers.get("set-cookie", ""))
        self.assertEqual(fresh.post("/login/google", data={"credential": "壊れた印"}).status_code, 400)
        http = self.client()
        self.log_in(http, login_id)
        tok = self.token(http)
        linked = http.post("/t/account/google", headers={"X-Form-Token": tok}, data={"credential": self.google.token(sub)})
        self.assertEqual(linked.status_code, 200, linked.text)
        self.assertIn(f"{sub}@example.test", http.get(linked.json()["next"]).text)
        g = self.client()
        ok = g.post("/login/google", data={"credential": self.google.token(sub)})
        self.assertEqual(ok.json(), {"next": "/t"})
        self.assertEqual(g.get("/t").status_code, 200)

    def test_without_the_server_settings_the_buttons_are_not_shown(self):
        bare = TestClient(create_app(self.real, web_login.identify(self.real), SECRET), base_url="https://testserver")
        page = bare.get("/login").text
        self.assertNotIn("accounts.google.com", page)
        self.assertEqual(bare.post("/login/google", data={"credential": "x"}).status_code, 400)
        self.assertIn("accounts.google.com", self.client().get("/login").text)


if __name__ == "__main__":
    unittest.main()
