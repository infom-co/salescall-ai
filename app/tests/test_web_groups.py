"""依頼者のグループの試験（持ち主 CL-3。タブの左の選び・/t/groups）。グループの表と部品は core の registry（CL-2 4-436）。
✅ 2026-11-18 管理者「依頼者が増えた場合は『グループ』を作れるといいね」。グループは並べ方だけ（見られる範囲は変わらない）。架空のデータだけ。"""

from __future__ import annotations

import re
import unittest

from fastapi.testclient import TestClient

from salescall.web.app import create_app
from salescall.web.context import Person
from tests.support import OPERATOR, World, app_access

SECRET = b"test-only-secret"


class Groups(unittest.TestCase):
    def test_a_group_narrows_the_tabs_and_changes_nothing_else(self):
        w = World()
        a = w.setup()
        other = app_access().in_tenant(OPERATOR, a["tenant"], lambda tx: tx.one(
            "INSERT INTO salescall.clients (tenant_id, name) VALUES (salescall.cur_tenant(), '架空のグループ外の依頼者') RETURNING client_id"))["client_id"]
        http = TestClient(create_app(app_access(), lambda r: Person("試験の契約者", False, a["tenant"], "架空の契約者"), SECRET),
                          base_url="https://testserver")   # 選びの印は https だけで送る
        tok = lambda: re.search(r'<meta name="form-token" content="([^"]+)"', http.get("/t").text).group(1).split("~")[0]   # noqa: E731
        self.assertIn("＋ グループを作る", http.get("/t").text)
        r = http.post("/t/groups", data={"form_token": tok(), "op": "create", "name": "架空のグループ"}, follow_redirects=False)
        self.assertIn("ok=1", r.headers["location"])
        page = http.get("/t/groups").text
        gid = re.search(r'name="op" value="members"><input type="hidden" name="group_id" value="([^"]+)"', page).group(1)
        r = http.post("/t/groups", data={"form_token": tok(), "op": "members", "group_id": gid, "client_id": str(a["client"])},
                      follow_redirects=False)
        self.assertIn("ok=1", r.headers["location"])
        http.get(f"/t/group?g={gid}&back=/t")                                     # グループを選ぶ（ブラウザに覚えさせる）
        home = http.get("/t").text
        tabs = home[home.index('class="scroller"'):home.index('</div>', home.index('class="scroller"'))]
        self.assertIn(f'/c/{a["client"]}"', tabs)
        self.assertNotIn(f'/c/{other}"', tabs)                                    # グループの外の依頼者は、タブに並べない
        self.assertEqual(http.get(f"/c/{other}").status_code, 200)                # ただし開ける（並べ方だけ。見られる範囲は変わらない）
        http.get("/t/group?g=none&back=/t")                                       # グループなし
        tabs = http.get("/t").text
        self.assertIn(f'/c/{other}"', tabs[tabs.index('class="scroller"'):])
        http.get("/t/group?back=/t")                                              # すべて
        self.assertIn("ok=1", http.post("/t/groups", data={"form_token": tok(), "op": "remove", "group_id": gid},
                                        follow_redirects=False).headers["location"])
        self.assertIn("グループはまだありません", http.get("/t/groups").text)


if __name__ == "__main__":
    unittest.main()
