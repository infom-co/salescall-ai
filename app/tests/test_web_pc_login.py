"""PC のログインの入口の試験（持ち主 CL-3。web/pc_login.py）。PC の側の本物の部品（agent/pc_login.login）を、この入口に向けて動かす。"""

from __future__ import annotations

import re
import unittest

from fastapi.testclient import TestClient

from salescall.agent import pc_login as agent_login
from salescall.core import agents
from salescall.web.app import create_app
from salescall.web.context import Person
from tests.support import OPERATOR, World, app_access

SECRET = b"test-only-secret"


class PcLogin(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        w = World()
        cls.a, cls.b = w.setup(), w.setup()
        cls.people = {"A": Person("試験の契約者A", False, cls.a["tenant"], "契約者A"),
                      "B": Person("試験の契約者B", False, cls.b["tenant"], "契約者B")}
        cls.http = TestClient(create_app(app_access(), lambda r: cls.people.get(r.headers.get("x-test-person", "")), SECRET),
                              base_url="https://testserver")

    def _post(self, url, body):
        return self.http.post(url.replace("https://testserver", ""), json=body).json()

    def _decide(self, who, code, decision):
        page = self.http.get(f"/pc-login?code={code}", headers={"x-test-person": who})
        tok = re.search(r'<meta name="form-token" content="([^"]+)"', page.text).group(1)
        return self.http.post("/pc-login", data={"form_token": tok, "code": code, "decision": decision},
                              headers={"x-test-person": who}, follow_redirects=False)

    def _pcs(self, tenant):
        return app_access().in_tenant(OPERATOR, tenant, agents.pcs)

    def test_the_real_pc_side_logs_in_through_this_entrance(self):
        before = len(self._pcs(self.a["tenant"]))

        def browser(url):   # PC がブラウザで開くページ＝契約者Aがログインして許可する
            code = url.split("code=")[1]
            page = self.http.get(f"/pc-login?code={code}", headers={"x-test-person": "A"}).text
            self.assertIn(code, page)
            self.assertIn("架空の PC", page)
            self.assertIn("ok=1", self._decide("A", code, "approve").headers["location"])

        import socket
        from unittest import mock
        with mock.patch.object(socket, "gethostname", return_value="架空の PC"):
            got = agent_login.login("https://testserver", post=self._post, open_browser=browser, say=lambda s: None)   # 本物の間隔で待つ
        self.assertEqual(got.tenant_id, self.a["tenant"])
        self.assertEqual(len(self._pcs(self.a["tenant"])), before + 1)
        self.assertEqual(self._pcs(self.b["tenant"]), [])                      # 別の契約者の PC にはならない

    def test_a_denied_or_unknown_code_gives_no_pass(self):
        start = self._post("https://testserver/pc-login/start", {"pc_label": "架空の PC"})
        self.assertEqual(self._post("https://testserver/pc-login/token", {"device_code": start["device_code"]})["error"], "authorization_pending")
        self.assertEqual(self._post("https://testserver/pc-login/token", {"device_code": start["device_code"]})["error"], "slow_down")
        self.assertIn("ok=0", self._decide("B", "ZZZZ-ZZZZ", "approve").headers["location"])   # 無いコード
        self.assertIn(start["user_code"], self.http.get(f"/pc-login?code={start['user_code'].lower().replace('-', '')}",
                                                         headers={"x-test-person": "B"}).text)   # 小文字・区切り無しでも同じコード
        self.assertIn("ok=1", self._decide("B", start["user_code"], "deny").headers["location"])
        self.assertEqual(self._post("https://testserver/pc-login/token", {"device_code": start["device_code"]})["error"], "access_denied")
        self.assertEqual(self._post("https://testserver/pc-login/token", {"device_code": start["device_code"]})["error"], "expired_token")
        self.assertEqual(self._post("https://testserver/pc-login/token", {"device_code": "でたらめ"})["error"], "expired_token")

    def test_starting_too_often_from_one_place_is_slowed(self):
        own = TestClient(create_app(app_access(), lambda r: None, SECRET), base_url="https://testserver")   # 絞りは画面のサーバーごと
        got = [own.post("/pc-login/start", json={"pc_label": "架空の PC"}).status_code for _ in range(12)]
        self.assertIn(429, got)                                                  # ログイン不要の入口なので、同じ所から続けたら断る

    def test_the_approval_page_needs_a_login(self):
        r = self.http.get("/pc-login?code=BCDF-GHJK", follow_redirects=False)
        self.assertEqual((r.status_code, r.headers["location"]), (303, "/login"))


if __name__ == "__main__":
    unittest.main()
