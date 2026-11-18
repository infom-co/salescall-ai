"""赤字の知らせの画面の試験（持ち主 CL-3）。状態は CL-2 の試験の土台で、本物の流れから作る（架空のデータだけ）。

✅ 2026-10-30 管理者「警告して、1時間で赤字の確認ボタンを契約者が押したらもう知らせない。確認ボタンは画面に出したまま。」
⇒ 赤字のあいだ、費用と報酬の画面と依頼者のタブに、同じ部品で出し続ける。押したあとも「確かめました」の印で残す。受け箱からは消える。
"""

from __future__ import annotations

import re
import unittest

from fastapi.testclient import TestClient

from salescall.web.app import create_app
from salescall.web.context import Person
from tests.support import app_access
from tests.test_usage import CostRatio

SECRET = b"test-only-secret"


class DeficitOnScreen(CostRatio):
    def test_the_deficit_stays_on_screen_after_it_is_confirmed(self):
        s = self.ready()
        self.terms(s, per_dial_yen=10)
        self.talked(s)
        self.priced("0.01")
        self.spend(s, 15)                                                      # 費用 15円 ＞ 報酬 10円
        person = Person("試験の契約者", False, s["tenant"], "架空の契約者")
        http = TestClient(create_app(app_access(), lambda r: person, SECRET))
        costs = http.get("/t/costs").text
        self.assertIn("確かめた（これ以上お知らせしない）", costs)
        self.assertIn("赤字の確認", http.get("/t/inbox").text)
        self.assertIn("報酬より費用が多くなっています", http.get(f'/c/{s["client"]}').text)
        form = re.search(r'<form method="post" action="(/c/[^"]+/inbox/deficit/confirm)">(.*?)</form>', costs, re.S)
        sent = dict(re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)">', form.group(2)))
        r = http.post(form.group(1), data=sent, follow_redirects=False)
        self.assertTrue(r.headers["location"].startswith("/t/costs?"))         # 費用と報酬の画面へ戻る
        self.assertIn("ok=1", r.headers["location"])
        for page in (http.get("/t/costs").text, http.get(f'/c/{s["client"]}/b/{s["business"]}').text):
            self.assertIn("確かめました", page)                                  # ボタンは画面に出したまま（印として）
            self.assertNotIn("確かめた（これ以上お知らせしない）", page)
        self.assertNotIn("赤字の確認", http.get("/t/inbox").text)                # 待っているものではなくなった

    def test_confirming_from_the_client_overview_comes_back_to_it(self):   # 戻る先の決まり（app.safe_back の1か所）
        s = self.ready()
        self.terms(s, per_dial_yen=10)
        self.talked(s)
        self.priced("0.01")
        self.spend(s, 15)
        person = Person("試験の契約者", False, s["tenant"], "架空の契約者")
        http = TestClient(create_app(app_access(), lambda r: person, SECRET))
        overview = http.get(f'/c/{s["client"]}').text
        form = re.search(r'<form method="post" action="(/c/[^"]+/inbox/deficit/confirm)">(.*?)</form>', overview, re.S)
        sent = dict(re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)">', form.group(2)))
        self.assertEqual(sent["back"], f'/c/{s["client"]}')
        r = http.post(form.group(1), data=sent, follow_redirects=False)
        self.assertTrue(r.headers["location"].startswith(f'/c/{s["client"]}?'), r.headers["location"])


def load_tests(loader, tests, pattern):
    """この試験だけを読む（受け継いだ CL-2 の組の試験を、ここで流し直さない）。"""
    return unittest.TestSuite(loader.loadTestsFromNames(
        [f"{__name__}.DeficitOnScreen.{n}" for n in DeficitOnScreen.__dict__ if n.startswith("test_")]))


if __name__ == "__main__":
    unittest.main()
