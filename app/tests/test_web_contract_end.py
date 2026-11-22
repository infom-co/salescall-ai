"""依頼者との契約を終える画面の試験（持ち主 CL-3。docs/DASHBOARD.md 5-3b。計画書 11-4・設計書 3-10。CL-2 の 4-480）。
押す・取り消す・外せるかは core の contract_end の1か所で、画面は確かめて呼ぶだけ。外した依頼者はタブから外し、住所を開くと1枚だけを出す
（外したかを決めるのは web/context.client の1か所）。試験のデータは架空のもの。"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from unittest import mock
from urllib.parse import unquote

from salescall.core import backup, contract_end
from salescall.supervisor import jobs
from salescall.supervisor.links import NoLinks
from tests.support import OPERATOR, SYSTEM, World, app_access
from tests.test_gate import HOLDER
from tests.test_web import _World


class ContractEndScreen(_World):
    def state(self, s):
        return app_access().in_tenant(OPERATOR, s["tenant"], lambda tx: contract_end.state(tx, s["client"]))

    def active(self, s):
        return app_access().in_tenant(OPERATOR, s["tenant"], lambda tx: tx.one(
            "SELECT salescall.client_active(%s) AS a", (s["client"],)))["a"]

    def test_ending_is_separate_from_pausing_names_the_client_and_can_be_cancelled_during_the_grace(self):
        c = self.a["client"]
        page = self.get("A", f"/c/{c}/settings").text
        self.assertIn("契約を終える", page)
        self.assertIn(f'action="/c/{c}/contract-end"', page)
        self.assertIn("依頼者「依頼者X」の契約を終えます", page)                 # 取り消せない操作は名前つきで（計画書 5-1）
        self.assertIn("7日", page)                                                # 猶予は設定の値から（7日＝管理者）
        self.assertIn("しばらく休む", page)                                       # 休む（無効）と契約を終えるを分ける
        none = self.post("A", f"/c/{c}/contract-end", {"form_token": self.token("A"), "reason": ""})
        self.assertIn("ok=0", none.headers["location"])                           # 理由は必ず
        self.assertIsNone(self.state(self.a).end_id)
        r = self.post("A", f"/c/{c}/contract-end", {"form_token": self.token("A"), "reason": "架空：取引が終わった"})
        self.assertIn("ok=1", r.headers["location"])
        self.assertIn("契約終了にしました", unquote(r.headers["location"]))
        self.assertIsNotNone(self.state(self.a).end_id)
        self.assertFalse(self.active(self.a))                                     # 新しい電話が止まる（使わない形＝core の1か所）
        tab = self.get("A", f"/c/{c}").text
        self.assertIn("契約終了にしました。新しい電話をかけません", tab)          # 依頼者の画面の帯
        self.assertNotIn("まだ外していない理由", tab)                             # 猶予のあいだは「取り消せます」だけ（同じことを2度言わない）
        self.assertIn("（契約終了）", tab)                                         # タブの印（「無効」と分ける）
        page = self.get("A", f"/c/{c}/settings").text
        self.assertIn(f'action="/c/{c}/contract-end/cancel"', page)
        self.assertNotIn('name="active" value="1"', page)                         # 契約終了のあいだは「有効に戻す」を出さない（取り消しを通す）
        self.assertIn("架空：取引が終わった", page)                               # だれが・なぜ（記録のまま）
        other = self.post("B", f"/c/{c}/contract-end/cancel", {"form_token": self.token("B"), "reason": "架空：ほかの契約者"})
        self.assertEqual(other.status_code, 404)                                  # ほかの契約者からは見えない
        back = self.post("A", f"/c/{c}/contract-end/cancel", {"form_token": self.token("A"), "reason": "架空：押し間違い"})
        self.assertIn("ok=1", back.headers["location"])
        self.assertIsNone(self.state(self.a).end_id)
        self.assertTrue(self.active(self.a))                                      # 取り消すと使う形に戻る

    def test_a_removed_client_leaves_the_tabs_and_its_address_opens_one_page_only(self):
        s, c = self.y, self.y["client"]
        r = self.post("A", f"/c/{c}/contract-end", {"form_token": self.token("A"), "reason": "架空：取引が終わった"})
        self.assertIn("ok=1", r.headers["location"])
        w = World()
        with mock.patch.object(contract_end, "forget_after", lambda tx: timedelta(0)):
            tab = self.get("A", f"/c/{c}").text
            self.assertIn("まだ外していない理由：契約終了のあとの控えを、まだハードディスクに取って確かめていません", tab)   # 猶予が過ぎたあとだけ
            backup._record(w.a, datetime.now().astimezone() + timedelta(seconds=1), "x.ai.enc", ("0" * 64, 1), {"tasks": 1},
                           None, person=None, part="client_ai", tenant_id=s["tenant"], client_id=c)
            jobs.contract_ends(jobs.Context(w.a, None, HOLDER, NoLinks(), s["tenant"]))
        self.assertTrue(self.state(s).forgotten)
        home = self.get("A", "/t").text
        self.assertNotIn(f'data-client="{c}"', home)                              # タブから外す
        self.assertIn("契約が終わった依頼者", home)                                # ホームに名前だけ残す（住所へ行ける）
        self.assertIn(f'href="/c/{c}"', home)
        for path in (f"/c/{c}", f"/c/{c}/settings", f"/c/{c}/dnc", f"/c/{c}/destinations", f"/c/{c}/search?q=架空",
                     f"/c/{c}/b/{s['business']}", f"/c/{c}/b/{s['business']}/runs/{s['run']}"):
            with self.subTest(path=path):
                got = self.get("A", path)
                self.assertEqual(got.status_code, 200)
                self.assertIn("サーバーからデータを外しました", got.text)          # ほかの中身は出さない（1枚だけ）
                self.assertNotIn("お客様リスト", got.text.split("<main", 1)[-1])
        self.assertEqual(self.get("B", f"/c/{c}").status_code, 404)              # ほかの契約者には「見つかりません」のまま
        self.assertIn(f'data-client="{self.a["client"]}"', home)                  # ほかの依頼者はそのまま

    def test_a_paused_client_can_be_ended_and_cancelling_says_calls_start_again(self):
        c = self.b["client"]
        off = self.post("B", f"/c/{c}/active", {"form_token": self.token("B"), "active": "0", "reason": "架空：今月は休む"})
        self.assertIn("ok=1", off.headers["location"])
        page = self.get("B", f"/c/{c}/settings").text
        self.assertIn(f'action="/c/{c}/contract-end"', page)                      # 休んでいても契約を終えられる
        r = self.post("B", f"/c/{c}/contract-end", {"form_token": self.token("B"), "reason": "架空：取引が終わった"})
        self.assertIn("ok=1", r.headers["location"])
        page = self.get("B", f"/c/{c}/settings").text
        cancel = page[page.index(f'action="/c/{c}/contract-end/cancel"') - 300:][:900]
        self.assertIn("また電話をかけます", cancel)                                # 取り消すと有効に戻ることを、窓の文で言う
        back = self.post("B", f"/c/{c}/contract-end/cancel", {"form_token": self.token("B"), "reason": "架空：押し間違い"})
        self.assertIn("ok=1", back.headers["location"])
        again = self.post("B", f"/c/{c}/contract-end/cancel", {"form_token": self.token("B"), "reason": "架空：二度押し"})
        self.assertIn("ok=0", again.headers["location"])                         # 二度押しは断りの文（画面が止まらない）
        self.assertIn("契約終了になっていません", unquote(again.headers["location"]))

    def test_the_removal_notice_has_words(self):
        from salescall.output import notices, words
        self.assertEqual(words.notice_kind("client_forgotten"), "契約が終わった依頼者のデータをサーバーから外しました")
        self.assertIn("client_forgotten", notices.TIMING)


if __name__ == "__main__":
    unittest.main()
