"""発信禁止の一覧の取り込みの画面の試験（持ち主 CL-3。Intent.md 7-2）。架空のデータだけ。

依頼者の台帳と契約者の共通の台帳を、同じ部品で：ファイル → 列 → 行ごとの結果（何も書かない）→ 登録。登録は確かめた結果と同じときだけ
（core の dnc_import の1か所）。置いたファイルは、置いた方・同じ置き場（契約者・依頼者）のほかからは開けない。
"""

from __future__ import annotations

import re
import unittest

from salescall.core import phone as phone_core
from tests.support import OPERATOR, app_access, fictional_jp_fixed
from tests.test_web import _World


class DncImport(_World):
    def _send(self, who, base, body):
        return self.http.post(base, data={"form_token": self.token(who), "encoding": "utf-8"},   # 数字だけの小さな CSV は文字の決まりを選ぶ
                              files={"file": ("かけないで.csv", body, "text/csv")},
                              headers={"x-test-person": who}, follow_redirects=False)

    def _flow(self, who, base, body):
        r = self._send(who, base, body)
        self.assertEqual(r.status_code, 303, r.text[:200])
        key_url = r.headers["location"]
        self.assertTrue(key_url.startswith(base + "/"), key_url)
        page = self.get(who, key_url).text
        self.assertIn("電話番号", page)
        mapping = dict(re.findall(r'<select id="map-([a-z_]+)" name="map_[a-z_]+">.*?<option value="([^"]*)" selected', page, re.S))
        return key_url, mapping

    def _entries(self, client=None):
        a = app_access()
        if client:
            return a.in_client(OPERATOR, self.a["tenant"], client, lambda tx: {r["phone_e164"] for r in tx.all(
                "SELECT phone_e164 FROM salescall.dnc_entries WHERE revoked_at IS NULL AND phone_e164 IS NOT NULL")})
        return a.in_tenant(OPERATOR, self.a["tenant"], lambda tx: {r["phone_e164"] for r in tx.all(
            "SELECT phone_e164 FROM salescall.contractor_dnc_entries WHERE revoked_at IS NULL")})

    def test_a_client_list_is_previewed_then_registered(self):
        n = fictional_jp_fixed()
        e164 = phone_core.normalize(n).e164
        base = f"/c/{self.a['client']}/dnc/import"
        key_url, mapping = self._flow("A", base, f"電話番号,管理番号\n{n},D-1\nでたらめ,\n".encode())
        self.assertEqual(mapping.get("phone"), "電話番号")                     # 見出しからの案
        tok = self.token("A")
        pv = self.post("A", key_url + "/preview", {"form_token": tok, "map_phone": "電話番号", "map_client_ref": "管理番号"})
        self.assertEqual(pv.status_code, 200)
        self.assertIn("止められない行", pv.text)
        self.assertNotIn(e164, self._entries(self.a["client"]))                 # 行ごとの結果を出しただけでは書かない
        other = self.post("B", key_url + "/register", {"form_token": self.token("B")})
        self.assertEqual(other.status_code, 404)                                 # 別の契約者は、置いたファイルを使えない
        r = self.post("A", key_url + "/register", {"form_token": tok})
        self.assertIn("ok=1", r.headers["location"])
        self.assertIn(e164, self._entries(self.a["client"]))
        self.assertNotIn(e164, self._entries(self.y["client"]))                 # 依頼者をまたいでは止めない
        self.assertEqual(self.post("A", key_url + "/register", {"form_token": tok}).status_code, 404)   # 置き場は片付いた

    def test_the_contractor_list_takes_numbers_only_and_another_client_cannot_open_the_file(self):
        n = fictional_jp_fixed()
        e164 = phone_core.normalize(n).e164
        key_url, _ = self._flow("A", "/t/dnc/import", f"電話番号,管理番号\n{n},D-2\n".encode())
        page = self.get("A", key_url).text
        self.assertNotIn("依頼者の管理番号", page)                                # 共通の台帳は番号だけ（core と同じ）
        key = key_url.rsplit("/", 1)[1]
        self.assertEqual(self.get("A", f"/c/{self.a['client']}/dnc/import/{key}").status_code, 404)   # 置き場が違えば開けない
        tok = self.token("A")
        self.post("A", key_url + "/preview", {"form_token": tok, "map_phone": "電話番号"})
        r = self.post("A", key_url + "/register", {"form_token": tok})
        self.assertIn("ok=1", r.headers["location"])
        self.assertIn(e164, self._entries())

    def test_registering_without_a_preview_writes_nothing(self):
        key_url, _ = self._flow("A", "/t/dnc/import", f"電話番号\n{fictional_jp_fixed()}\n".encode())
        self.assertEqual(self.post("A", key_url + "/register", {"form_token": self.token("A")}).status_code, 404)


if __name__ == "__main__":
    unittest.main()
