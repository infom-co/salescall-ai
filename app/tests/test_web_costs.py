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
        from salescall.core import reward as reward_core   # 電話と電話の間：初め2分・30秒ずつ（core の1か所。CL-2 4-76）
        self.assertIn(f'name="hourly_gap_seconds" min="{reward_core.HOURLY_GAP_STEP_SECONDS}" step="{reward_core.HOURLY_GAP_STEP_SECONDS}"', page)
        self.assertIn(f'value="{reward_core.HOURLY_GAP_INITIAL_SECONDS}"', page[page.index('id="rw-gap"'):][:400])
        tok = re.search(r'<meta name="form-token" content="([^"]+)"', page).group(1).split("~")[0]
        post = lambda data: http.post(reward_url, data={"form_token": tok, **data}, follow_redirects=False)   # noqa: E731
        self.assertIn("ok=0", post({"per_completed_yen": "5000", "deficit_caution": "normal", "reason": ""}).headers["location"])   # 理由は必ず
        self.assertIn("ok=0", post({"deficit_caution": "normal", "reason": "試験"}).headers["location"])   # どれも 0 円は足せない（core の断り）
        self.assertIn("ok=0", post({"per_completed_yen": "五千", "deficit_caution": "normal", "reason": "試験"}).headers["location"])
        r = post({"per_completed_yen": "5,000", "per_dial_yen": "10", "hourly_gap_seconds": "120", "deficit_caution": "early",
                  "reason": "試験：架空の契約のとおり"})
        self.assertIn("ok=1", r.headers["location"])
        gap = app_access().in_client(OPERATOR, s["tenant"], s["client"], lambda tx: tx.one(
            "SELECT hourly_gap_seconds FROM salescall.reward_terms WHERE business_id = %s ORDER BY terms_id DESC LIMIT 1", (s["business"],)))
        self.assertIsNone(gap["hourly_gap_seconds"])                              # 時給を使わないときは、電話と電話の間を記録に入れない
        page = http.get(reward_url).text
        self.assertIn("5,000円", page)
        self.assertIn("試験：架空の契約のとおり", page)
        self.assertNotIn(f'href="{reward_url}"', http.get("/t/costs").text)       # 決まりが入ったので、もう出さない


    def test_a_client_with_nothing_this_month_is_one_name_not_a_row(self):   # 2026-11-22 の見直し：0円の札が11枚続いた
        s = self.ready()
        self.terms(s, per_dial_yen=10)                                            # 決まりはあるが、今月まだかけていない
        http = TestClient(create_app(app_access(), lambda r: Person("試験の契約者", False, s["tenant"], "架空の契約者"), SECRET))
        costs = http.get("/t/costs").text
        link = f'<a href="/c/{s["client"]}">'
        quiet = costs[costs.index('class="quiet-clients'):]
        self.assertIn(link, quiet[:quiet.index("</p>")])                          # 動きの無い依頼者は、名前だけの1行に
        self.assertNotIn(f"<td>{link}", costs)                                    # 表の行には並べない
        self.talked(s)                                                            # かけたら、表の行に出る
        costs = http.get("/t/costs").text
        self.assertIn(f"<td>{link}", costs)
        self.assertNotIn('class="quiet-clients', costs)

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
