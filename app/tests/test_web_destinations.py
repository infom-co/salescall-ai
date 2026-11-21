"""結果の送り先の試験（持ち主 CL-3。output/destinations・/c/{依頼者}/destinations）。業務ごとの選びは core の destinations（CL-2 4-435）。架空のデータだけ。"""

from __future__ import annotations

import re
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from salescall.core import destinations
from salescall.output import destinations as result_destinations
from salescall.web.app import create_app
from salescall.web.context import Person
from tests.support import OPERATOR, World, app_access

SECRET = b"test-only-secret"
# 試験のための架空の送り方（本物の部品ではない）
FAKE = {"fake_a": result_destinations.Method("架空の送り方A"), "fake_b": result_destinations.Method("架空の送り方B")}


class Destinations(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.a = World().setup()
        cls.b2 = app_access().in_client(OPERATOR, cls.a["tenant"], cls.a["client"], lambda tx: tx.one(
            "INSERT INTO salescall.businesses (tenant_id, client_id, name) VALUES (salescall.cur_tenant(), salescall.cur_client(),"
            " '架空の2つ目の業務') RETURNING business_id"))["business_id"]
        cls.http = TestClient(create_app(app_access(), lambda r: Person("試験の契約者", False, cls.a["tenant"], "架空の契約者"), SECRET))
        cls.path = f"/c/{cls.a['client']}/destinations"

    def tok(self):
        return re.search(r'<meta name="form-token" content="([^"]+)"', self.http.get(self.path).text).group(1).split("~")[0]

    def post(self, data):
        return self.http.post(self.path, data={"form_token": self.tok(), **data}, follow_redirects=False).headers["location"]

    def sends(self, business_id):
        return {d["name"] for d in app_access().in_client(OPERATOR, self.a["tenant"], self.a["client"],
                                                          lambda tx: destinations.for_business(tx, business_id))}

    def test_without_a_sending_part_nothing_can_be_made_and_the_page_says_files(self):
        page = self.http.get(self.path).text
        self.assertIn("ファイル（Excel・CSV）で渡します", page)
        self.assertNotIn('value="create"', page)                                   # 部品の無い送り方は選べない
        self.assertIn("ok=0", self.post({"op": "create", "name": "架空の送り先", "method": "kintone"}))

    def test_make_fix_and_choose_per_business(self):
        with mock.patch.dict(result_destinations.METHODS, FAKE, clear=True):
            self.assertIn('value="create"', self.http.get(self.path).text)
            self.assertIn("ok=1", self.post({"op": "create", "name": "架空の台帳", "method": "fake_a", "auto_writeback": "1"}))
            self.assertIn("ok=1", self.post({"op": "create", "name": "架空の表", "method": "fake_b"}))
            self.assertIn("ok=0", self.post({"op": "create", "name": "  ", "method": "fake_b"}))          # 名前は必ず
            dests = {d["name"]: d for d in app_access().in_client(OPERATOR, self.a["tenant"], self.a["client"], result_destinations.of_client)}
            self.assertTrue(dests["架空の台帳"]["auto_writeback"])
            self.assertFalse(dests["架空の表"]["auto_writeback"])
            self.assertTrue(all(d["upsert_capable"] for d in dests.values()))         # 送り先は、上書きで受けられるものだけ（4-5）
            # 選んでいない業務は、すべての送り先へ
            self.assertEqual(self.sends(self.a["business"]), {"架空の台帳", "架空の表"})
            biz = str(self.a["business"])
            # 1つ目の業務は台帳だけに
            self.assertIn("ok=1", self.post({"op": "business", "business_id": biz, "mode": "pick",
                                             "destination_id": str(dests["架空の台帳"]["destination_id"])}))
            self.assertEqual(self.sends(self.a["business"]), {"架空の台帳"})
            self.assertEqual(self.sends(self.b2), {"架空の台帳", "架空の表"})   # ほかの業務は変わらない
            # 「選んだ送り先だけ」で1つも選ばないのは受けない（どこへも書き戻さないなら「ファイルだけ」を選ぶ）
            self.assertIn("ok=0", self.post({"op": "business", "business_id": biz, "mode": "pick"}))
            self.assertEqual(self.sends(self.a["business"]), {"架空の台帳"})
            # ファイルだけ（どこへも書き戻さない＝CL-2 4-446 の 56）
            self.assertIn("ok=1", self.post({"op": "business", "business_id": biz, "mode": "file"}))
            self.assertEqual(self.sends(self.a["business"]), set())
            self.assertIn("ファイルだけ", self.http.get(self.path).text)
            # 依頼者の送り先すべてに戻す
            self.assertIn("ok=1", self.post({"op": "business", "business_id": biz, "mode": "all"}))
            self.assertEqual(self.sends(self.a["business"]), {"架空の台帳", "架空の表"})
            self.assertIn("ok=0", self.post({"op": "business", "business_id": biz}))   # 渡し方を選ばなければ受けない
            # 直す：名前と自動の書き戻し
            self.assertIn("ok=1", self.post({"op": "update", "id": str(dests["架空の台帳"]["destination_id"]), "name": "架空の台帳（新）"}))
            got = {d["name"]: d for d in app_access().in_client(OPERATOR, self.a["tenant"], self.a["client"], result_destinations.of_client)}
            self.assertFalse(got["架空の台帳（新）"]["auto_writeback"])
            page = self.http.get(self.path).text
            self.assertIn("架空の台帳（新）", page)
            self.assertIn(f'id="b-{self.a["business"]}"', page)
            self.assertIn("の仕組みへ自動で書き戻す", page)                       # どの依頼者の仕組みへ送るかを名前で見せる（計画書 5-1）

    def test_another_contractor_cannot_open_or_change(self):
        other = World().setup()
        http = TestClient(create_app(app_access(), lambda r: Person("試験の契約者B", False, other["tenant"], "架空の契約者B"), SECRET))
        self.assertEqual(http.get(self.path).status_code, 404)
        tok = re.search(r'<meta name="form-token" content="([^"]+)"', http.get("/t").text).group(1).split("~")[0]
        r = http.post(self.path, data={"form_token": tok, "op": "create", "name": "乗っ取り", "method": "x"}, follow_redirects=False)
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main()


class NoDestinationYet(unittest.TestCase):
    """送り先が1つも無いうちは、1つの札（いまはファイルで渡す）だけ。足せない「送り先を足す」の札も、選ぶ意味の無い業務ごとの渡し方も
    出さない（2026-11-20 の見直し：送り先0件なのに「依頼者の送り先すべて」が選ばれて見えた）。"""

    @classmethod
    def setUpClass(cls):
        cls.a = World().setup()
        cls.http = TestClient(create_app(app_access(), lambda r: Person("試験の契約者", False, cls.a["tenant"], "架空の契約者"), SECRET))
        cls.path = f"/c/{cls.a['client']}/destinations"

    def test_one_card_says_files_and_nothing_to_choose(self):
        page = self.http.get(self.path).text
        self.assertIn("送り先はまだありません。いまは、結果をファイル（Excel・CSV）で渡します", page)
        self.assertNotIn("業務ごとの渡し方", page)
        self.assertNotIn("送り先を足す", page)                                     # 送る部品が無いので、足す入口を出さない
        with mock.patch.dict(result_destinations.METHODS, FAKE, clear=True):
            page = self.http.get(self.path).text
            self.assertIn("送り先を足す", page)                                     # 足せる送り方があれば、同じ札の中で足す
            self.assertNotIn("業務ごとの渡し方", page)                              # 送り先ができるまでは、業務ごとの選びは出さない
