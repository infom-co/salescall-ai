"""分離と守りの試験の残り（docs/DASHBOARD.md 8-3 の S-12・S-15・S-19。持ち主 CL-3）。守りは読み書きの層（CL-2）にあり、ここは画面の側から叩く。
試験のデータは架空のものだけ。契約者A（依頼者X・依頼者Y）と契約者B（依頼者Z＝契約者Aと同じ番号のお客様）は tests.test_web の土台を使う。"""

from __future__ import annotations

import re
import unittest
from uuid import UUID

from salescall.core import notify
from tests.support import OPERATOR, SYSTEM, World, app_access
from tests.test_web import _World


class Isolation(_World):
    def pages_of(self, who: str, s: dict) -> dict[str, str]:
        """その依頼者の画面（概要・業務のタブ・お客様リスト・お客様1件・探した結果・結果の見本）の中身。"""
        c, b, r, t = s["client"], s["business"], s["run"], s["task"]
        paths = [f"/c/{c}", f"/c/{c}/b/{b}", f"/c/{c}/b/{b}/runs/{r}", f"/c/{c}/b/{b}/runs/{r}/rows/{t}",
                 f"/c/{c}/b/{b}/runs/{r}/results", f"/c/{c}/search?q={s['phone'][-8:]}"]
        out = {}
        for p in paths:
            got = self.get(who, p)
            self.assertEqual(got.status_code, 200, p)
            out[p] = got.text
        return out

    def test_s12_an_error_of_client_y_is_not_shown_on_client_x(self):
        """S-12：依頼者Yの処理の誤り（知らせの中身）が、依頼者Xの画面に出ない（同じ契約者の中でも、依頼者の画面は依頼者のものだけ）。"""
        secret = "架空の依頼者Yだけの誤りの中身S12"
        app_access().in_client(SYSTEM, self.y["tenant"], self.y["client"], lambda tx: notify.emit(
            tx, "error_notice", key=f"s12:{self.y['client']}", summary=secret, refs={"client_id": str(self.y["client"])}))
        for path, page in self.pages_of("A", self.a).items():
            with self.subTest(path=path):
                self.assertNotIn(secret, page)
        self.assertNotIn(secret, self.get("B", "/t/notices").text)                 # ほかの契約者の知らせにも出ない
        self.assertIn(secret, self.get("A", "/t/notices").text)                    # 契約者Aの知らせの台帳には出る（同じ契約者のもの）

    def test_s15_a_call_by_another_contractor_to_the_same_number_is_not_shown(self):
        """S-15：契約者Bが先にかけた番号（契約者Aの依頼者Xのお客様と同じ番号）について、契約者Aのどの画面・結果のファイルにも、契約者Bが
        かけたこと（契約者の名前・依頼者の名前・電話の記録）が出ない。"""
        w = World()
        aid = w.insert_claim(self.b)
        for state in ("dialing", "ringing", "answered", "in_conversation", "ending", "ended"):
            w.move(self.b, aid, state, None)
        b_names = ["契約者B", "依頼者Z", str(aid)]
        pages = self.pages_of("A", self.a)
        csv = self.get("A", f"/c/{self.a['client']}/b/{self.a['business']}/runs/{self.a['run']}/results.csv").content.decode("utf-8-sig")
        pages["results.csv"] = csv
        for path, page in pages.items():
            for name in b_names:
                with self.subTest(path=path, name=name):
                    self.assertNotIn(name, page)
        row = pages[f"/c/{self.a['client']}/b/{self.a['business']}/runs/{self.a['run']}/rows/{self.a['task']}"]
        self.assertIn("まだ電話をかけていません", row)                               # 契約者Aのこのお客様の電話の記録は空のまま

    def test_s19_admin_only_operations_are_refused_for_a_contractor(self):
        """S-19：管理者だけの操作（全体の緊急停止・管理者だけの画面）を契約者が送っても、ボタンは出ず、送っても何も変わらない。"""
        def system_stops() -> int:
            return app_access().in_system(SYSTEM, lambda tx: tx.one(
                "SELECT count(*) AS n FROM salescall.open_stop_events WHERE scope::text = 'system'")["n"])
        before = system_stops()
        home = self.get("A", "/t").text
        self.assertNotIn('name="scope" value="system"', home)                     # 全体の緊急停止の選び肢は出ない
        r = self.post("A", "/t/emergency", {"form_token": self.token("A"), "scope": "system", "reason": "試験：乗っ取り"})
        self.assertEqual((r.status_code, r.headers.get("location", "")), (404, ""))   # 「見つかりません」と同じ返事（全体の取引は管理者だけ）
        self.assertEqual(system_stops(), before)                                   # 全体の停止は開いていない
        for path in ("/admin/settings", "/admin/tenants", "/admin/name-reviews"):
            with self.subTest(path=path):
                self.assertEqual(self.get("A", path).status_code, 404)
        r = self.post("A", "/admin/name-reviews/skip", {"form_token": self.token("A"), "tenant_id": str(self.a["tenant"]), "skip": "1"})
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main()
