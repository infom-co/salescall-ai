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
from tests.support import OPERATOR, app_access
from tests.test_usage import CostRatio

SECRET = b"test-only-secret"


class DeficitOnScreen(CostRatio):
    def test_reward_terms_are_added_per_business_from_its_page(self):   # 報酬の決まりは業務ごと（✅ 2026-11-18 管理者・CL-2 4-437）
        s = self.ready()
        self.talked(s)                                                            # 決まりの無いまま、今月かけた
        http = TestClient(create_app(app_access(), lambda r: Person("試験の契約者", False, s["tenant"], "架空の契約者"), b"test-only-secret"))
        reward_url = f'/c/{s["client"]}/b/{s["business"]}/reward'
        costs = http.get("/t/costs").text
        self.assertIn(f'href="{reward_url}"', costs)                              # 決まりの無い業務から、その業務の報酬の決まりの画面へ
        page = http.get(reward_url).text
        self.assertIn("報酬の決まりが設定されていません", page)
        tok = re.search(r'<meta name="form-token" content="([^"]+)"', page).group(1).split("~")[0]
        post = lambda data: http.post(reward_url, data={"form_token": tok, **data}, follow_redirects=False)   # noqa: E731
        self.assertIn("ok=0", post({"per_completed_yen": "5000", "deficit_caution": "normal", "reason": ""}).headers["location"])   # 理由は必ず
        self.assertIn("ok=0", post({"deficit_caution": "normal", "reason": "試験"}).headers["location"])   # どれも 0 円は足せない（core の断り）
        self.assertIn("ok=0", post({"per_completed_yen": "五千", "deficit_caution": "normal", "reason": "試験"}).headers["location"])
        r = post({"per_completed_yen": "5,000", "per_dial_yen": "10", "deficit_caution": "early", "reason": "試験：架空の契約のとおり"})
        self.assertIn("ok=1", r.headers["location"])
        page = http.get(reward_url).text
        self.assertIn("5,000円", page)
        self.assertIn("試験：架空の契約のとおり", page)
        self.assertNotIn(f'href="{reward_url}"', http.get("/t/costs").text)       # 決まりが入ったので、もう出さない


    def test_the_deficit_stays_on_screen_after_it_is_confirmed(self):
        s = self.ready()
        self.terms(s, per_dial_yen=10)
        self.talked(s)
        self.priced("0.01")
        self.spend(s, 15)                                                      # 費用 15円 ＞ 報酬 10円
        person = Person("試験の契約者", False, s["tenant"], "架空の契約者")
        http = TestClient(create_app(app_access(), lambda r: person, SECRET))
        costs = http.get("/t/costs").text
        from salescall.core import reward
        from salescall.web.app import _yen
        month = app_access().in_client(OPERATOR, s["tenant"], s["client"], lambda tx: reward.month(tx))
        self.assertIn("うち実測", costs)                                         # 費用の実測と推定（core の reward.month の cost_by_kind をそのまま）
        self.assertIn(_yen(month["cost_by_kind"]["measured"]), costs)
        self.assertEqual(month["cost_by_kind"]["measured"] + month["cost_by_kind"]["estimated"], month["cost"])
        self.assertIn("確かめた（これ以上お知らせしない）", costs)
        self.assertIn("赤字の確認", http.get("/t/inbox").text)
        self.assertIn("報酬より費用が多くなっています", http.get(f'/c/{s["client"]}').text)
        from tests.test_web import visible_primary_buttons                     # 赤字の札があっても、主のボタンは1画面に1つ（設計書 7-4）
        for path in ("/t/costs", f'/c/{s["client"]}', f'/c/{s["client"]}/b/{s["business"]}'):
            with self.subTest(path=path):
                got = visible_primary_buttons(http.get(path).text)
                self.assertLessEqual(len(got), 1, got)
        form = re.search(r'<form method="post" action="(/c/[^"]+/inbox/deficit/confirm)"[^>]*>(.*?)</form>', costs, re.S)
        sent = dict(re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)">', form.group(2)))
        r = http.post(form.group(1), data=sent, follow_redirects=False)
        self.assertTrue(r.headers["location"].startswith("/t/costs?"))         # 費用と報酬の画面へ戻る
        self.assertIn("ok=1", r.headers["location"])
        from urllib.parse import unquote
        self.assertIn("赤字の確認（依頼者「", unquote(r.headers["location"]))   # 結果の1行は、決めた札の種類と依頼者の名前を添える
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
        form = re.search(r'<form method="post" action="(/c/[^"]+/inbox/deficit/confirm)"[^>]*>(.*?)</form>', overview, re.S)
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
