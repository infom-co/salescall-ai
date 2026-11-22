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


    def test_s1_lists_of_client_y_are_not_returned_on_client_x(self):
        """S-1：依頼者Xの住所で、依頼者Yのお客様リスト（一覧・結果の見本・結果のファイル・手元の控え・取り込み）を求めても返さない（「見つかりません」）。"""
        x, y = self.a, self.y
        for tail in ("", "/results", "/results.csv", "/records.xlsx", "/import"):
            for path in (f"/c/{x['client']}/b/{x['business']}/runs/{y['run']}{tail}", f"/c/{x['client']}/b/{y['business']}/runs/{y['run']}{tail}"):
                with self.subTest(path=path):
                    r = self.get("A", path)
                    self.assertEqual(r.status_code, 404)
                    self.assertNotIn(str(y["task"]), r.text)

    def test_s10_costs_do_not_mix_another_contractor(self):
        """S-10：費用と報酬の画面に、ほかの契約者の依頼者が名前も数も出ない（同じ契約者の依頼者は出る）。"""
        a, b = self.get("A", "/t/costs").text, self.get("B", "/t/costs").text
        self.assertIn("依頼者X", a)
        self.assertIn("依頼者Y", a)
        self.assertNotIn("依頼者Z", a)
        self.assertNotIn("依頼者X", b)
        self.assertNotIn("依頼者Y", b)

    def test_s11_a_reason_for_holding_a_call_shows_only_the_reason(self):
        """S-11：かける直前に見送った理由は、core の言葉の表（gate.REASONS）の文をそのまま出すだけで、ほかの依頼者・契約者の結果・答え・名前を
        差し込む所が無い（同じ番号へほかの依頼者がかけたばかり、の理由も、だれがかけたかを言わない。ほかの契約者のときは中身も言わない）。"""
        from salescall.core import gate
        from salescall.output import words
        for name, reason in gate.REASONS.items():
            with self.subTest(reason=name):
                self.assertEqual(words.gate_reason(name), reason.message)          # 画面で言葉を足さない
                self.assertNotIn("{", reason.message)                             # 差し込む所が無い
        self.assertNotIn("依頼者", gate.REASONS["cross_client_interval_other_tenant"].message)
        self.assertNotIn("契約者", gate.REASONS["cross_client_interval_other_tenant"].message)

    def test_s16_results_of_client_y_are_not_handed_over_through_client_x(self):
        """S-16：依頼者Xの住所で、依頼者Yの業務・お客様リストの結果を依頼者へ渡すファイルとして出させても、断られ、提出用の控えは1つも増えない
        （出力の型の持ち主は core の output_templates.current の1か所＝ほかの依頼者の業務は LookupError）。"""
        def kept() -> int:
            return app_access().in_client(OPERATOR, self.y["tenant"], self.y["client"], lambda tx: tx.one(
                "SELECT count(*) AS n FROM salescall.submission_files")["n"])
        before = kept()
        x, y = self.a, self.y
        for path in (f"/c/{x['client']}/b/{y['business']}/runs/{y['run']}/results.csv",
                     f"/c/{x['client']}/b/{x['business']}/runs/{y['run']}/results.xlsx"):
            with self.subTest(path=path):
                self.assertEqual(self.post("A", path, {"form_token": self.token("A")}).status_code, 404)
        self.assertEqual(kept(), before)
        from salescall.core import output_templates
        with self.assertRaises(LookupError):                                       # 依頼者Xの取引から依頼者Yの業務の型は読めない
            app_access().in_client(OPERATOR, x["tenant"], x["client"], lambda tx: output_templates.current(tx, y["business"]))

if __name__ == "__main__":
    unittest.main()
