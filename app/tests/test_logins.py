"""契約者ごとのデータベースの合言葉の試験（DESIGN.md 11章。core/logins.py・表の作り 0077。2026-10-18 ユーザー決定）。

- 合言葉を払い出すのは、全体の取引の管理者か、アカウントを作るシステムの処理だけ。替えたら前の合言葉は使えない。使えなくできる。
- 契約者の合言葉でつないだら、データベースが、その契約者の分だけにする（ほかの契約者・全体の取引・管理者としての動きを断る）。
- 契約者の合言葉から、すべての契約者に効くもの（祝日の暦・ほかの契約者の仕事の借用）を書けない。
⚠️ 合言葉は、試験の中で作って使うだけ（表示しない）。
"""

from __future__ import annotations

import os
import unittest

from salescall.core import logins
from salescall.core.access import Access, Provisioner, Rejected
from tests.support import ADMIN, OPERATOR, SYSTEM, World

CHECKS_PASSWORDS = "SALESCALL_TEST_SUPERUSER_DSN" not in os.environ   # 使い捨てのデータベースは、契約者の合言葉を本当に確かめる


class Base(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.s, self.other = self.w.setup(), World().setup()
        self.prov = Provisioner(os.environ["SALESCALL_TEST_PROVISIONER_DSN"])

    def issue(self, s=None, actor=SYSTEM):
        return logins.issue(self.w.a, actor, self.prov, (s or self.s)["tenant"], base_conninfo=os.environ["SALESCALL_TEST_APP_DSN"])


class Scope(Base):
    def test_a_contractor_login_reaches_only_that_contractor(self):
        mine = Access(self.issue().conninfo)
        s, o = self.s, self.other
        theirs = self.w.insert_claim(o)                                      # ほかの契約者の、終わった通話
        for state in ("dialing", "no_answer"):
            self.w.move(o, theirs, state)
        rows = mine.in_client(OPERATOR, s["tenant"], s["client"], lambda tx: tx.all("SELECT task_id FROM salescall.tasks"))
        self.assertEqual([r["task_id"] for r in rows], [s["task"]])
        self.assertEqual(mine.in_client(OPERATOR, s["tenant"], o["client"], lambda tx: tx.all("SELECT 1 FROM salescall.tasks")), [])
        self.assertEqual(mine.in_client(OPERATOR, s["tenant"], o["client"], lambda tx: tx.all(
            "SELECT * FROM salescall.work_seconds(now() - interval '1 day', now())")), [])
        for name, run in (("ほかの契約者の取引", lambda: mine.in_tenant(OPERATOR, o["tenant"], lambda tx: tx.all(
                              "SELECT 1 FROM salescall.clients"))),
                          ("全体の取引", lambda: mine.in_system(SYSTEM, lambda tx: tx.all("SELECT 1 FROM salescall.clients"))),
                          ("管理者として", lambda: mine.in_tenant(ADMIN, s["tenant"], lambda tx: tx.one(
                              "SELECT salescall.cur_actor_kind() AS k"))),
                          ("祝日の暦を書く", lambda: mine.in_tenant(SYSTEM, s["tenant"], lambda tx: tx.execute(
                              "INSERT INTO salescall.calendar_loads (file_sha256, first_year, last_year, loaded_by)"
                              " VALUES (repeat('0', 64), 2026, 2026, 'x')"))),
                          ("全体の仕事を借りる", lambda: mine.in_tenant(SYSTEM, s["tenant"], lambda tx: tx.one(
                              "SELECT salescall.take_job('holidays', 'x', 60)"))),
                          ("ほかの契約者の仕事を借りる", lambda: mine.in_tenant(SYSTEM, s["tenant"], lambda tx: tx.one(
                              "SELECT salescall.take_job(%s, 'x', 60)", (f"settle:{o['tenant']}",))))):
            with self.subTest(name), self.assertRaises(Rejected):
                run()
        self.assertTrue(mine.in_tenant(SYSTEM, s["tenant"], lambda tx: tx.one(
            "SELECT salescall.take_job(%s, 'x', 60) AS ok", (f"settle:{s['tenant']}",))["ok"]))   # その契約者の仕事は借りられる

    def test_the_system_login_is_unchanged(self):
        """管理者の PC・画面の機械のシステムの役は、前と同じ（全体の取引・管理者として動ける）。"""
        self.assertEqual(self.w.a.in_system(ADMIN, lambda tx: tx.one("SELECT salescall.cur_actor_kind() AS k"))["k"], "admin")


class Issue(Base):
    def test_only_the_admin_or_the_system_issues_and_the_password_is_never_shown(self):
        with self.assertRaises(Rejected):
            self.issue(actor=OPERATOR)
        login = self.issue(actor=ADMIN)
        self.assertNotIn(login.password, repr(login))
        self.assertEqual(login.role, logins.role_name(self.s["tenant"]))
        with self.assertRaises(LookupError):
            logins.revoke(self.w.a, SYSTEM, self.prov, self.other["tenant"])   # 払い出していない契約者
        kinds = self.w.a.in_system(ADMIN, lambda tx: [r["kind"] for r in tx.all(
            "SELECT kind FROM salescall.tenant_login_events WHERE tenant_id = %s ORDER BY event_id", (self.s["tenant"],))])
        self.assertEqual(kinds, ["issued"])
        self.assertEqual(self.w.a.in_tenant(OPERATOR, self.s["tenant"], lambda tx: tx.all(
            "SELECT 1 FROM salescall.tenant_login_events")), [])                # 契約者の取引からは見えない

    @unittest.skipUnless(CHECKS_PASSWORDS, "合言葉を確かめるのは、使い捨てのデータベースだけ")
    def test_reissuing_replaces_the_password_and_revoking_closes_the_login(self):
        first, second = self.issue(), self.issue()
        ok = lambda login: Access(login.conninfo).in_tenant(OPERATOR, self.s["tenant"], lambda tx: tx.one("SELECT 1 AS x"))  # noqa: E731
        self.assertEqual(ok(second)["x"], 1)
        with self.assertRaises(Exception):                                   # 前の合言葉は使えない
            ok(first)
        logins.revoke(self.w.a, SYSTEM, self.prov, self.s["tenant"])
        with self.assertRaises(Exception):                                   # 契約が終わったら、つなげない
            ok(second)


if __name__ == "__main__":
    unittest.main()
