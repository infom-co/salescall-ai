"""ダッシュボードの試験（持ち主 CL-3。docs/DASHBOARD.md 8章）。画面を通して叩く。架空のデータだけを使う。

ログインの仕組み（利用者の表）は CL-2 に頼んでいるので、ここでは架空の人を返す関数を渡す（本番の画面のサーバーは、ログインを確かめる
関数を渡されなければ動かない）。
"""

from __future__ import annotations

import re
import unittest
from uuid import UUID

from fastapi.testclient import TestClient

from salescall.core import stops
from salescall.web.app import create_app
from salescall.web.context import Person
from tests.support import OPERATOR, World, app_access

SECRET = b"test-only-secret"


class _World(unittest.TestCase):
    """架空の契約者A（依頼者X・Y）と契約者B（依頼者Z）。試験の組が共通に使う準備（試験そのものは持たない）。"""

    @classmethod
    def setUpClass(cls):
        w = World()
        cls.a = w.setup()                               # 契約者A・依頼者X
        cls.y = w.setup(tenant=cls.a["tenant"])         # 同じ契約者Aの依頼者Y（番号は別）
        cls.b = w.setup(phone=cls.a["phone"])           # 契約者B・依頼者Z（契約者Aと同じ番号のお客様）
        app_access().in_tenant(OPERATOR, cls.a["tenant"], lambda tx: tx.execute(
            "UPDATE salescall.clients SET name = CASE client_id WHEN %s THEN '依頼者X' ELSE '依頼者Y' END WHERE tenant_id = %s",
            (cls.a["client"], cls.a["tenant"])))
        app_access().in_tenant(OPERATOR, cls.b["tenant"], lambda tx: tx.execute(
            "UPDATE salescall.clients SET name = '依頼者Z（契約者B）' WHERE client_id = %s", (cls.b["client"],)))
        cls.people = {"A": Person("試験の契約者A", False, cls.a["tenant"], "契約者A"),
                      "B": Person("試験の契約者B", False, cls.b["tenant"], "契約者B"),
                      "ADMIN": Person("試験の管理者", True, cls.a["tenant"], "契約者A")}

        def identify(request):
            return cls.people.get(request.headers.get("x-test-person", ""))
        cls.http = TestClient(create_app(app_access(), identify, SECRET))

    def get(self, who, path):
        return self.http.get(path, headers={"x-test-person": who}, follow_redirects=False)

    def post(self, who, path, data):
        return self.http.post(path, data=data, headers={"x-test-person": who}, follow_redirects=False)

    def token(self, who, path="/t"):
        m = re.search(r'<meta name="form-token" content="([^"]+)"', self.get(who, path).text)
        self.assertIsNotNone(m, "画面に札がありません")
        return m.group(1)

    def _stop(self, tenant: UUID, reason: str) -> int:
        return app_access().in_tenant(OPERATOR, tenant, lambda tx: stops.stop(
            tx, scope="tenant", cause="manual", reason=reason, tenant_id=tenant))["event_id"]

    def _open(self, tenant: UUID, eid: int) -> bool:
        return app_access().in_tenant(OPERATOR, tenant, lambda tx: tx.one(
            "SELECT 1 FROM salescall.open_stop_events WHERE event_id = %s", (eid,)) is not None)



class Web(_World):
    # ── ログイン ──
    def test_without_login_every_page_goes_to_the_login_page(self):   # S-23
        for path in ("/t", "/t/inbox", "/t/stops", "/t/notices", f"/c/{self.a['client']}", "/t/badges"):
            with self.subTest(path=path):
                r = self.http.get(path, follow_redirects=False)
                self.assertEqual(r.status_code, 303)
                self.assertEqual(r.headers["location"], "/login")

    def test_the_server_does_not_start_without_a_secret(self):
        with self.assertRaises(ValueError):
            create_app(app_access(), lambda r: None, b"")

    # ── 分離（docs/DASHBOARD.md 8-3） ──
    def test_the_client_list_shows_only_the_contractors_own_clients(self):   # S-13
        page = self.get("A", "/t").text
        self.assertIn("依頼者X", page)
        self.assertIn("依頼者Y", page)
        self.assertNotIn("依頼者Z", page)

    def test_another_contractors_client_answers_the_same_as_a_missing_one(self):   # S-2・S-14
        other = self.get("A", f"/c/{self.b['client']}")
        missing = self.get("A", "/c/00000000-0000-0000-0000-000000000000")
        self.assertEqual(other.status_code, 404)
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(other.text, missing.text)
        self.assertNotIn("依頼者Z", other.text)

    def test_a_run_of_another_client_cannot_be_opened_under_this_client(self):   # S-4
        r = self.get("A", f"/c/{self.a['client']}/runs/{self.y['run']}")
        self.assertEqual(r.status_code, 404)
        r = self.get("A", f"/c/{self.a['client']}/runs/{self.b['run']}")
        self.assertEqual(r.status_code, 404)

    def test_the_customer_list_shows_the_rows_of_the_run(self):
        r = self.get("A", f"/c/{self.a['client']}/runs/{self.a['run']}")
        self.assertEqual(r.status_code, 200)
        self.assertIn("お客様リスト", r.text)
        self.assertIn("これから", r.text)   # まだ次の予定が無い行

    # ── 停止と再開 ──
    def test_a_stop_of_another_contractor_is_not_seen(self):   # S-18
        eid = self._stop(self.b["tenant"], "契約者Bの試験の停止")
        self.assertNotIn("契約者Bの試験の停止", self.get("A", "/t/stops").text)
        self.assertEqual(self.get("A", f"/t/stops/{eid}").status_code, 404)
        self.assertIn("契約者Bの試験の停止", self.get("B", "/t/stops").text)

    def test_resuming_needs_the_page_token_and_a_reason(self):
        eid = self._stop(self.a["tenant"], "再開の試験の停止")
        page = f"/t/stops/{eid}"
        self.assertIn("再開の試験の停止", self.get("A", page).text)
        tok = self.token("A", page)
        # 札が無い・違う ⇒ 何も変わらない
        r = self.post("A", page + "/resume", {"reason": "確かめた"})
        self.assertIn("ok=0", r.headers["location"])
        r = self.post("A", page + "/resume", {"reason": "確かめた", "form_token": "x.y"})
        self.assertIn("ok=0", r.headers["location"])
        # 理由が空 ⇒ 何も変わらない
        r = self.post("A", page + "/resume", {"reason": "  ", "form_token": tok})
        self.assertIn("ok=0", r.headers["location"])
        self.assertTrue(self._open(self.a["tenant"], eid))
        # 契約者Bの札では、契約者Aの人として送れない
        r = self.post("A", page + "/resume", {"reason": "確かめた", "form_token": self.token("B")})
        self.assertIn("ok=0", r.headers["location"])
        # 札と理由がそろえば再開する
        r = self.post("A", page + "/resume", {"reason": "試験：確かめた", "form_token": tok})
        self.assertIn("ok=1", r.headers["location"])
        self.assertFalse(self._open(self.a["tenant"], eid))

    def test_another_contractor_cannot_resume_a_stop(self):   # S-14
        eid = self._stop(self.a["tenant"], "Bが解けない停止")
        r = self.post("B", f"/t/stops/{eid}/resume", {"reason": "解く", "form_token": self.token("B")})
        self.assertIn("ok=0", r.headers["location"])
        self.assertTrue(self._open(self.a["tenant"], eid))

    def test_a_stop_of_one_client_is_listed_in_the_whole_view_and_resumed_under_that_client(self):   # 2026-11-18 の誤りの試験
        eid = app_access().in_client(OPERATOR, self.a["tenant"], self.y["client"], lambda tx: stops.stop(
            tx, scope="client", cause="manual", reason="依頼者Yだけの試験の停止", tenant_id=self.a["tenant"],
            client_id=self.y["client"]))["event_id"]
        whole = self.get("A", "/t/stops").text
        self.assertIn("依頼者Yだけの試験の停止", whole)            # 契約者の取引だけで読むと、ここに出ない
        self.assertIn(f"/c/{self.y['client']}/stops/{eid}", whole)
        self.assertNotIn("依頼者Yだけの試験の停止", self.get("B", "/t/stops").text)
        self.assertEqual(self.get("A", f"/c/{self.a['client']}/stops/{eid}").status_code, 404)   # 依頼者Xの住所では開けない
        page = f"/c/{self.y['client']}/stops/{eid}"
        self.assertIn("依頼者Yだけの試験の停止", self.get("A", page).text)
        r = self.post("A", page + "/resume", {"reason": "試験：確かめた", "form_token": self.token("A")})
        self.assertIn("ok=1", r.headers["location"])
        self.assertNotIn("依頼者Yだけの試験の停止", self.get("A", "/t/stops").text)

    # ── 文字は文字として出す ──
    def test_a_name_from_a_file_is_shown_as_text(self):   # S-17
        app_access().in_tenant(OPERATOR, self.a["tenant"], lambda tx: tx.execute(
            "UPDATE salescall.clients SET name = '<script>alert(1)</script>' WHERE client_id = %s", (self.y["client"],)))
        try:
            page = self.get("A", "/t").text
            self.assertNotIn("<script>alert(1)</script>", page)
            self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", page)
        finally:
            app_access().in_tenant(OPERATOR, self.a["tenant"], lambda tx: tx.execute(
                "UPDATE salescall.clients SET name = '依頼者Y' WHERE client_id = %s", (self.y["client"],)))

    def test_every_page_opens(self):
        for path in ("/t", "/t/inbox", "/t/stops", "/t/notices", "/t/badges", f"/c/{self.a['client']}",
                     f"/c/{self.a['client']}/runs/{self.a['run']}"):
            with self.subTest(path=path):
                r = self.get("A", path)
                self.assertEqual(r.status_code, 200, r.text[:300])
                self.assertNotIn(words_missing(), r.text)


class EmergencyStop(_World):
    """緊急停止のボタン（範囲：契約者の全部／依頼者だけ／全体＝管理者だけ）。押した停止は、試験の終わりに必ず解く（ほかの試験に響かせない）。"""

    def press(self, who, data):
        return self.post(who, "/t/emergency", {"form_token": self.token(who), "reason": "試験の緊急停止", **data})

    def _opened(self, tenant, client=None, scope="tenant"):
        def read(tx):
            return [r["event_id"] for r in tx.all("SELECT event_id FROM salescall.open_stop_events WHERE cause = 'manual'"
                                                     " AND scope = %s AND reason = '試験の緊急停止'", (scope,))]
        a = app_access()
        return a.in_client(OPERATOR, tenant, client, read) if client else a.in_tenant(OPERATOR, tenant, read)

    def _resume_all(self, tenant, client=None, scope="tenant"):
        for eid in self._opened(tenant, client, scope):
            work = lambda tx, e=eid: stops.resume(tx, e, reason="試験の後片付け")
            (app_access().in_client(OPERATOR, tenant, client, work) if client else app_access().in_tenant(OPERATOR, tenant, work))

    def test_a_contractor_stops_the_whole_contractor_once(self):
        try:
            r = self.press("B", {"scope": "tenant"})
            self.assertIn("ok=1", r.headers["location"])
            self.assertEqual(len(self._opened(self.b["tenant"])), 1)
            r = self.press("B", {"scope": "tenant"})          # 2回押しても重ねない
            self.assertEqual(len(self._opened(self.b["tenant"])), 1)
            self.assertIn("試験の緊急停止", self.get("B", "/t/stops").text)
            self.assertNotIn("試験の緊急停止", self.get("A", "/t/stops").text)
        finally:
            self._resume_all(self.b["tenant"])

    def test_a_contractor_stops_one_client(self):
        try:
            r = self.press("A", {"scope": "client", "client_id": str(self.y["client"])})
            self.assertIn("ok=1", r.headers["location"])
            self.assertEqual(len(self._opened(self.a["tenant"], self.y["client"], "client")), 1)
            self.assertEqual(self._opened(self.a["tenant"], self.a["client"], "client"), [])
        finally:
            self._resume_all(self.a["tenant"], self.y["client"], "client")

    def test_a_contractor_cannot_stop_another_contractors_client_or_everything(self):
        r = self.press("A", {"scope": "client", "client_id": str(self.b["client"])})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(self._opened(self.b["tenant"], self.b["client"], "client"), [])
        r = self.press("A", {"scope": "system"})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(self._opened(self.a["tenant"], scope="system"), [])

    def test_a_reason_is_required(self):
        r = self.post("A", "/t/emergency", {"form_token": self.token("A"), "reason": " ", "scope": "tenant"})
        self.assertIn("ok=0", r.headers["location"])
        self.assertEqual(self._opened(self.a["tenant"]), [])


class AdminSeesEverything(_World):
    """✅ 2026-11-18 管理者「管理者は全てを見れて操作できる」。切り替えた契約者は、どの画面にも帯で出す（取り違えを防ぐ）。"""

    def fresh(self):
        def identify(request):
            return self.people.get(request.headers.get("x-test-person", ""))
        return TestClient(create_app(app_access(), identify, SECRET))

    def test_the_admin_switches_to_another_contractor_sees_and_operates_it(self):
        http = self.fresh()
        tok = re.search(r'<meta name="form-token" content="([^"]+)"', http.get("/t", headers={"x-test-person": "ADMIN"}).text).group(1)
        r = http.post("/admin/acting", data={"tenant_id": str(self.b["tenant"]), "form_token": tok},
                      headers={"x-test-person": "ADMIN"}, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        page = http.get("/t", headers={"x-test-person": "ADMIN"}).text
        self.assertIn("依頼者Z", page)
        self.assertNotIn("依頼者X", page)
        self.assertIn("管理者として", page)
        eid = self._stop(self.b["tenant"], "管理者が解く試験の停止")
        r = http.post(f"/t/stops/{eid}/resume", data={"reason": "試験：管理者が確かめた", "form_token": tok},
                      headers={"x-test-person": "ADMIN"}, follow_redirects=False)
        self.assertIn("ok=1", r.headers["location"])
        self.assertFalse(self._open(self.b["tenant"], eid))
        # 戻る
        http.post("/admin/acting", data={"tenant_id": "", "form_token": tok}, headers={"x-test-person": "ADMIN"})
        page = http.get("/t", headers={"x-test-person": "ADMIN"}).text
        self.assertIn("依頼者X", page)
        self.assertNotIn("管理者として", page)

    def test_the_admin_can_stop_everything_and_it_shows_for_every_contractor(self):
        from tests.support import ADMIN
        tok = self.token("ADMIN")
        r = self.post("ADMIN", "/t/emergency", {"form_token": tok, "reason": "試験の全体の緊急停止", "scope": "system"})
        def opened(tx):
            return [x["event_id"] for x in tx.all("SELECT event_id FROM salescall.open_stop_events WHERE scope = 'system'"
                                                   " AND reason = '試験の全体の緊急停止'")]
        try:
            self.assertIn("ok=1", r.headers["location"])
            self.assertEqual(len(app_access().in_tenant(OPERATOR, self.b["tenant"], opened)), 1)
            self.assertIn("試験の全体の緊急停止", self.get("B", "/t/stops").text)   # 全体の停止は、どの契約者にも見える
        finally:
            for eid in app_access().in_system(ADMIN, opened):
                app_access().in_system(ADMIN, lambda tx, e=eid: stops.resume(tx, e, reason="試験の後片付け"))

    def test_a_contractor_cannot_switch_and_a_forged_choice_is_ignored(self):
        http = self.fresh()
        tok = re.search(r'<meta name="form-token" content="([^"]+)"', http.get("/t", headers={"x-test-person": "A"}).text).group(1)
        r = http.post("/admin/acting", data={"tenant_id": str(self.b["tenant"]), "form_token": tok},
                      headers={"x-test-person": "A"}, follow_redirects=False)
        self.assertEqual(r.status_code, 404)
        from salescall.web import actions
        forged = actions.sign_choice(SECRET, "試験の管理者", self.b["tenant"])   # 管理者の札を契約者が使っても効かない
        http.cookies.set("acting_tenant", forged)
        page = http.get("/t", headers={"x-test-person": "A"}).text
        self.assertNotIn("依頼者Z", page)
        http.cookies.set("acting_tenant", f"{self.b['tenant']}.00")
        page = http.get("/t", headers={"x-test-person": "ADMIN"}).text   # 署名の合わない札は捨てる
        self.assertNotIn("依頼者Z", page)


def words_missing() -> str:
    from salescall.output import words
    return words.NO_WORD


if __name__ == "__main__":
    unittest.main()
