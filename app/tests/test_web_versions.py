"""お客様リストの台本の版を替える（前の版へ戻す）と、業務の複製の試験（持ち主 CL-3。docs/DASHBOARD.md 5-8。計画書 5-3 の (11)・
4章 C）。替えるのは core の business_settings.assign、複製は registry.copy_business の1か所で、画面は確かめて呼ぶだけ。試験のデータは架空のもの。"""

from __future__ import annotations

import re
import unittest
from urllib.parse import unquote

from markupsafe import escape

from salescall.core import business_settings, registry
from tests.support import OPERATOR, World, app_access
from tests.test_web import _World, ensure_rules


class Versions(_World):
    def in_x(self, work):
        return app_access().in_client(OPERATOR, self.a["tenant"], self.a["client"], work)

    def setUp(self):
        ensure_rules(World(), self.a)
        b = self.a["business"]
        self.v1 = self.a["version"]
        self.v2 = self.in_x(lambda tx: business_settings.save_from(tx, b, self.v1))   # 中身の同じ2つ目の版
        self.run = self.in_x(lambda tx: registry.create_run(tx, b, "架空の版替えのお客様リスト", self.v1, reason="架空の試験"))
        self.url = f"/c/{self.a['client']}/b/{b}/runs/{self.run}/version"

    def current(self):
        return self.in_x(lambda tx: tx.one("SELECT salescall.run_current_version(%s) AS v", (self.run,))["v"])

    def no(self, v):
        return self.in_x(lambda tx: tx.one("SELECT version_no FROM salescall.business_versions WHERE version_id = %s", (v,))["version_no"])

    def test_the_version_of_a_list_is_changed_with_a_reason_and_recorded(self):
        tab = self.get("A", f"/c/{self.a['client']}/b/{self.a['business']}").text
        self.assertIn(f'action="/c/{self.a["client"]}/b/{self.a["business"]}/runs/{self.run}/version"', tab)   # 業務のタブに入口
        self.assertIn("{radio:version_id}に替えます", tab)                       # 名前つきの確かめの窓
        none = self.post("A", self.url, {"form_token": self.token("A"), "version_id": str(self.v2), "reason": ""})
        self.assertIn("ok=0", none.headers["location"])                         # 理由は必ず
        self.assertEqual(self.current(), self.v1)
        same = self.post("A", self.url, {"form_token": self.token("A"), "version_id": str(self.v1), "reason": "架空：同じ版"})
        self.assertIn("ok=0", same.headers["location"])                         # いまの版は選べない（何も変えない）
        r = self.post("A", self.url, {"form_token": self.token("A"), "version_id": str(self.v2), "reason": "架空：新しい版へ"})
        loc = unquote(r.headers["location"])
        self.assertIn("ok=1", loc)
        self.assertIn(f"版 {self.no(self.v2)} に替えました", loc)
        self.assertIn("#runs", loc)
        self.assertEqual(self.current(), self.v2)
        back = self.post("A", self.url, {"form_token": self.token("A"), "version_id": str(self.v1), "reason": "架空：前の版へ戻す"})
        self.assertIn("ok=1", back.headers["location"])                         # 前の版へ戻すのも同じ入口
        now = self.current()
        self.assertIn(now, (self.v1,) + tuple(self.in_x(lambda tx: [r["version_id"] for r in tx.all(
            "SELECT version_id FROM salescall.version_copies WHERE copied_from = %s", (self.v1,))])))   # 前の版そのもの、またはその写し
        tab = self.get("A", f"/c/{self.a['client']}/b/{self.a['business']}").text
        part = tab[tab.index("架空の版替えのお客様リスト"):]
        self.assertIn("版の記録", part[:6000])
        self.assertIn("架空：前の版へ戻す", part[:6000])                         # だれが・なぜ（記録の表のまま）
        self.assertIn("架空：新しい版へ", part[:6000])

    def test_a_version_of_another_business_or_another_contractor_is_refused(self):
        other = self.post("A", self.url, {"form_token": self.token("A"), "version_id": str(self.y["version"]), "reason": "架空"})
        self.assertEqual(other.status_code, 404)                                # 依頼者Yの業務の版
        b = self.post("B", self.url, {"form_token": self.token("B"), "version_id": str(self.v2), "reason": "乗っ取り"})
        self.assertEqual(b.status_code, 404)                                    # ほかの契約者
        self.assertEqual(self.current(), self.v1)

    def test_the_script_page_lists_the_standard_answers_with_where_they_come_from(self):   # ✅ 2026-11-02 管理者「デフォルトの想定を…表示しておき」
        std = (("架空の標準の問い", "架空の標準の答えです。"), ("架空の書き替える問い", "架空の標準の答え2です。"),
               ("架空の使わない問い", "架空の標準の答え3です。"))
        with World().standard(std):
            def make(tx):
                raw = dict(business_settings._stored(tx, self.v1))
                raw["answers"] = [{"name": "架空の書き替える問い", "text": "架空の業務の答えです。"},
                                  {"name": "架空の業務だけの問い", "text": "架空の業務だけの答えです。"}]
                raw["answers_off"] = ["架空の使わない問い"]
                return business_settings.save_version(tx, self.a["business"], raw)
            v3 = self.in_x(make)

        def rows(tx):
            s = business_settings.load(tx, v3)
            return business_settings.answer_rows(s, business_settings.standard(tx, s.standard_id))
        got = self.in_x(rows)
        self.assertEqual({r["source"] for r in got}, {"standard", "overridden", "business", "off"})
        page = self.get("A", f"/c/{self.a['client']}/b/{self.a['business']}/versions/{v3}").text
        part = page[page.index("想定問答"):]
        self.assertIn("<th>どこから</th>", part)
        from salescall.output import words
        for r in got:
            with self.subTest(name=r["name"]):
                self.assertIn(str(escape(r["name"])), part)
                self.assertIn(words.ANSWER_SOURCE_WORDS[r["source"]], part)
        self.assertIn("標準の答え：架空の標準の答え2です。", part)                 # 書き替えた問いは、標準の答えも添える
        self.assertNotIn("架空の標準の答え3です。", part)                         # 使わない問いの答えは出さない（「答えません」）

    def test_a_business_is_copied_from_a_version_and_opened(self):
        url = f"/c/{self.a['client']}/b/{self.a['business']}/versions/{self.v1}/copy"
        page = self.get("A", f"/c/{self.a['client']}/b/{self.a['business']}/versions/{self.v1}").text
        self.assertIn(f'action="{url}"', page)
        self.assertEqual(self.post("B", url, {"form_token": self.token("B"), "name": "乗っ取りの業務"}).status_code, 404)
        r = self.post("A", url, {"form_token": self.token("A"), "name": "架空の複製した業務"})
        loc = unquote(r.headers["location"])
        self.assertIn("ok=1", loc)
        self.assertIn("業務を複製しました", loc)
        new = re.search(rf"/c/{self.a['client']}/b/([0-9a-f-]+)", loc).group(1)
        self.assertNotEqual(new, str(self.a["business"]))
        got = self.in_x(lambda tx: tx.one("SELECT b.name, count(v.*) AS n FROM salescall.businesses b"
                                          " LEFT JOIN salescall.business_versions v ON v.business_id = b.business_id"
                                          " WHERE b.business_id = %s GROUP BY b.name", (new,)))
        self.assertEqual((got["name"], got["n"]), ("架空の複製した業務", 1))   # 新しい業務と、その最初の版
        self.assertIn("架空の複製した業務", self.get("A", f"/c/{self.a['client']}/b/{new}").text)
        again = self.post("A", url, {"form_token": self.token("A"), "name": "架空の複製した業務"})
        self.assertIn("ok=0", again.headers["location"])                        # 同じ名前の業務は core が断る（その文のまま）


if __name__ == "__main__":
    unittest.main()
