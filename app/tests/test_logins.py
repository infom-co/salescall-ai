"""契約者ごとのデータベースの合言葉の試験（DESIGN.md 11章。core/logins.py・表の作り 0077。2026-10-18 ユーザー決定）。

- 合言葉を払い出すのは、全体の取引の管理者か、アカウントを作るシステムの処理だけ。替えたら前の合言葉は使えない。使えなくできる。
- 契約者の合言葉でつないだら、データベースが、その契約者の分だけにする（ほかの契約者・全体の取引・管理者としての動きを断る）。
- 契約者の合言葉から、すべての契約者に効くもの（祝日の暦・ほかの契約者の仕事の借用）を書けない。
⚠️ 合言葉は、試験の中で作って使うだけ（表示しない）。
"""

from __future__ import annotations

import os
import unittest

from salescall.core import logins, notify, stops
from salescall.core.access import Access, LoginRefused, Provisioner, Rejected, snapshot
from tests.support import ADMIN, OPERATOR, SYSTEM, World

CHECKS_PASSWORDS = "SALESCALL_TEST_SUPERUSER_DSN" not in os.environ   # 使い捨てのデータベースは、契約者の合言葉を本当に確かめる


class Base(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.s, self.other = self.w.setup(), World().setup()
        self.prov = Provisioner(os.environ["SALESCALL_TEST_PROVISIONER_DSN"])

    def issue(self, s=None, actor=SYSTEM, replaces=None):
        return logins.issue(self.w.a, actor, self.prov, (s or self.s)["tenant"], base_conninfo=os.environ["SALESCALL_TEST_APP_DSN"],
                            pc_label="試験のPC", replaces=replaces)


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

    def test_admin_notices_jobs_and_stops_of_others_are_out_of_reach(self):
        """契約者の取引（種類が「システムの処理」でも）から、ほかの契約者の管理者あての知らせは読めない。ほかの契約者・全体の仕事の
        借用は返せない。ほかの契約者の停止で、その予約は取り消せない（0084）。"""
        mine = Access(self.issue().conninfo)
        s, o = self.s, self.other
        self.w.a.in_tenant(SYSTEM, o["tenant"], lambda tx: notify.emit(tx, "backup_failed", key="試験:管理者あて", summary="試験",
                                                                      admin=True, contractor=False))
        for access in (mine, self.w.a):                                       # 契約者の鍵でも、システムの役の契約者の取引でも
            with self.subTest(access=access):
                seen = access.in_tenant(SYSTEM, s["tenant"], lambda tx: tx.all(
                    "SELECT 1 FROM salescall.notification_events WHERE for_admin"))
                self.assertEqual(seen, [])
        self.assertEqual(len(self.w.a.in_system(ADMIN, lambda tx: tx.all(
            "SELECT 1 FROM salescall.notification_events WHERE for_admin AND origin_tenant = %s", (o["tenant"],)))), 1)
        with self.assertRaises(Rejected):
            mine.in_tenant(SYSTEM, s["tenant"], lambda tx: tx.one("SELECT salescall.return_job('holidays', 'x')"))
        stop = self.w.a.in_tenant(SYSTEM, o["tenant"], lambda tx: stops.stop(
            tx, cause="budget", reason="試験", tenant_id=o["tenant"], scope="tenant"))
        with self.assertRaises(Rejected):
            mine.in_tenant(SYSTEM, s["tenant"], lambda tx: tx.one("SELECT salescall.release_claims_for_stop(%s)",
                                                                  (stop["event_id"],)))

    def test_the_issuing_login_can_only_make_and_stop_pc_keys(self):
        """払い出し専用の役は、PC の鍵の形の名前・確かめの値の形の合言葉で、作る・止めるだけ（システムの役の合言葉を替える・
        名前を自由に付けた役を作る・自分をシステムの役にする、はできない。0084）。止めたら、つないだままの接続も切れる。"""
        prov = Access(os.environ["SALESCALL_TEST_PROVISIONER_DSN"])
        verifier = logins._scram("試験")
        for name, sql, args in (
                ("システムの役の合言葉を替える", "ALTER ROLE salescall_app PASSWORD 'x'", ()),
                ("役を作る", "CREATE ROLE 試験の役 LOGIN", ()),
                ("システムの役になる", "SET ROLE salescall_app", ()),
                ("形の違う名前", "SELECT salescall.make_pc_login('salescall_t_自由な名前', %s)", (verifier,)),
                ("合言葉の平文", "SELECT salescall.make_pc_login(%s, 'そのままの合言葉')",
                 (logins.role_name(self.s["tenant"], "0a1b2c3d"),)),
                ("表を読む", "SELECT 1 FROM salescall.tasks", ())):
            with self.subTest(name), self.assertRaises(Rejected):
                prov.in_system(SYSTEM, lambda tx: tx.execute(sql, args))
        login = self.issue()
        with snapshot(login.conninfo) as (_, held):                         # つないだままの接続
            logins.revoke(self.w.a, SYSTEM, self.prov, self.s["tenant"], pc=login.pc)
            with self.assertRaises(Exception):
                held.one("SELECT 1 AS x")

    def test_the_system_login_is_unchanged(self):
        """管理者の PC・画面の機械のシステムの役は、前と同じ（全体の取引・管理者として動ける）。"""
        self.assertEqual(self.w.a.in_system(ADMIN, lambda tx: tx.one("SELECT salescall.cur_actor_kind() AS k"))["k"], "admin")


class Issue(Base):
    def test_only_the_admin_or_the_system_issues_and_the_password_is_never_shown(self):
        with self.assertRaises(Rejected):
            self.issue(actor=OPERATOR)
        login = self.issue(actor=ADMIN)
        self.assertNotIn(login.password, repr(login))
        self.assertNotIn(login.password, repr(login) + login.role)
        self.assertEqual(login.role, logins.role_name(self.s["tenant"], login.pc))
        with self.assertRaises(LookupError):
            logins.revoke(self.w.a, SYSTEM, self.prov, self.other["tenant"])   # 払い出していない契約者
        kinds = self.w.a.in_system(ADMIN, lambda tx: [(r["kind"], r["pc"], r["pc_label"]) for r in tx.all(
            "SELECT kind, pc, pc_label FROM salescall.tenant_login_events WHERE tenant_id = %s ORDER BY event_id", (self.s["tenant"],))])
        self.assertEqual(kinds, [("issued", login.pc, "試験のPC")])
        self.assertEqual(self.w.a.in_tenant(OPERATOR, self.s["tenant"], lambda tx: tx.all(
            "SELECT 1 FROM salescall.tenant_login_events")), [])                # 契約者の取引からは見えない

    @unittest.skipUnless(CHECKS_PASSWORDS, "鍵を確かめるのは、使い捨てのデータベースだけ")
    def test_keys_are_per_pc_and_relogin_replaces_and_revoking_closes(self):
        """鍵は PC ごと（2台目の PC でログインしても、1台目は使える）。同じ PC でログインし直すと前の鍵は止まる。PC のログインを
        外す＝その PC だけ、契約が終わった＝その契約者のすべての PC。"""
        ok = lambda login: Access(login.conninfo).in_tenant(OPERATOR, self.s["tenant"], lambda tx: tx.one("SELECT 1 AS x"))  # noqa: E731
        first, second = self.issue(), self.issue()
        self.assertEqual((ok(first)["x"], ok(second)["x"]), (1, 1))
        again = self.issue(replaces=first.pc)                                # 1台目でログインし直した
        self.assertEqual(ok(again)["x"], 1)
        with self.assertRaises(LoginRefused):                                # 前の鍵は断られる（見守り役はログインし直す）
            ok(first)
        self.assertEqual(logins.revoke(self.w.a, ADMIN, self.prov, self.s["tenant"], pc=second.pc), 1)
        with self.assertRaises(Exception):
            ok(second)
        self.assertEqual(ok(again)["x"], 1)                                  # ほかの PC は使える
        self.assertEqual(logins.revoke(self.w.a, SYSTEM, self.prov, self.s["tenant"]), 3)   # 契約が終わった：すべて（止まった鍵も数える）
        with self.assertRaises(Exception):
            ok(again)


if __name__ == "__main__":
    unittest.main()
