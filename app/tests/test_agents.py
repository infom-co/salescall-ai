"""PC ごとの通行証と、PC が動いているかの見張りの試験（DESIGN.md 5-5。core/agents.py・表の作り 0105）。"""

from __future__ import annotations

import unittest
import uuid
from unittest import mock

from salescall.core import agents, settings
from salescall.core.access import Rejected
from tests.support import ADMIN, OPERATOR, SYSTEM, World, app_access, fictional_caller
from salescall.core import registry


class Passes(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.a = app_access()

    def test_a_pass_is_given_once_kept_only_as_a_fingerprint_and_identifies_the_pc(self):
        """通行証は1回だけ返し、データベースには指紋だけ（2026-10-21 ユーザー：読み取られないように）。サーバーは通行証から契約者と
        PC を決める。止めた通行証・知らない通行証は通らない。"""
        t = self.w.tenant()
        p = agents.issue(self.a, SYSTEM, t, label="架空の PC")
        self.assertNotIn(p.token, repr(p))
        rows = self.a.in_tenant(OPERATOR, t, lambda tx: tx.all("SELECT token_sha256 FROM salescall.pc_agents"))
        self.assertEqual(rows, [{"token_sha256": agents.fingerprint(p.token)}])
        who = lambda tok: self.a.in_system(SYSTEM, lambda tx: agents.identify(tx, tok))   # noqa: E731
        self.assertEqual(who(p.token), agents.Who(t, p.pc_id, "架空の PC"))
        self.assertIsNone(who("知らない通行証"))
        self.assertTrue(self.a.in_tenant(OPERATOR, t, lambda tx: agents.revoke(tx, p.pc_id)))   # 契約者が「この PC のログインを外す」
        self.assertIsNone(who(p.token))
        self.assertFalse(self.a.in_tenant(OPERATOR, t, lambda tx: agents.revoke(tx, p.pc_id)))  # 止めた印は1回だけ

    def test_logging_in_again_on_the_same_pc_stops_the_old_pass(self):
        t = self.w.tenant()
        old = agents.issue(self.a, SYSTEM, t, label="架空の PC")
        new = agents.issue(self.a, SYSTEM, t, label="架空の PC", replaces=old.pc_id)
        who = lambda tok: self.a.in_system(SYSTEM, lambda tx: agents.identify(tx, tok))   # noqa: E731
        self.assertIsNone(who(old.token))
        self.assertEqual(who(new.token), agents.Who(t, new.pc_id, "架空の PC"))

    def test_who_may_issue_stop_and_change(self):
        """払い出すのはシステムの処理か管理者だけ。止めるのは管理者・契約者・システムの処理。通行証・合図は書き換えられない。消せない。
        ほかの契約者の PC は見えない・止められない。サーバーの関数は全体の取引のシステムの処理だけ。"""
        t, other = self.w.tenant(), self.w.tenant()
        with self.assertRaises(Rejected):
            agents.issue(self.a, OPERATOR, t, label="契約者が自分で払い出す")
        p = agents.issue(self.a, ADMIN, t, label="架空の PC")
        for sql in ("UPDATE salescall.pc_agents SET token_sha256 = repeat('0', 64)",
                    "UPDATE salescall.pc_agents SET last_seen_at = now()",
                    "DELETE FROM salescall.pc_agents"):
            with self.subTest(sql=sql), self.assertRaises(Rejected):
                self.a.in_tenant(OPERATOR, t, lambda tx, sql=sql: tx.execute(sql))
        with self.assertRaises(LookupError):
            self.a.in_tenant(OPERATOR, other, lambda tx: agents.revoke(tx, p.pc_id))
        self.assertEqual(self.a.in_tenant(OPERATOR, other, agents.pcs), [])
        for call in ("SELECT * FROM salescall.agent_by_token(repeat('0', 64))", "SELECT salescall.all_tenants()",
                     "SELECT salescall.agent_beat(gen_random_uuid(), '{}')"):
            with self.subTest(call=call), self.assertRaises(Rejected):
                self.a.in_tenant(SYSTEM, t, lambda tx, call=call: tx.all(call))
            with self.subTest(call=call, by="admin"), self.assertRaises(Rejected):
                self.a.in_system(ADMIN, lambda tx, call=call: tx.all(call))

    def test_a_beat_keeps_only_the_tenants_lines_in_use(self):
        """合図で伝えた回線は、その契約者の、使っている回線だけが残る（ほかの契約者の回線を名乗って、その電話をかけさせない）。"""
        t, other = self.w.tenant(), self.w.tenant()
        mine = self.a.in_tenant(OPERATOR, t, lambda tx: registry.create_line(tx, "架空の回線", method="rakuten_link",
                                                                              caller=fictional_caller()))
        theirs = self.a.in_tenant(OPERATOR, other, lambda tx: registry.create_line(tx, "架空の回線", method="rakuten_link",
                                                                                    caller=fictional_caller()))
        p = agents.issue(self.a, SYSTEM, t, label="架空の PC")
        kept = self.a.in_system(SYSTEM, lambda tx: agents.beat(tx, p.pc_id, [mine, theirs, uuid.uuid4()]))
        self.assertEqual(kept, [mine])
        self.a.in_tenant(OPERATOR, t, lambda tx: agents.revoke(tx, p.pc_id))
        self.assertIsNone(self.a.in_system(SYSTEM, lambda tx: agents.beat(tx, p.pc_id, [mine])))


class Down(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.a = app_access()

    def notices(self, t):
        return self.a.in_tenant(OPERATOR, t, lambda tx: tx.all(
            "SELECT event_key FROM salescall.notification_events WHERE kind = 'pc_down'"))

    def test_a_pc_that_goes_quiet_is_noticed_once_per_outage(self):
        """合図が途切れた PC は、その契約者と管理者へ知らせる（1回の途切れに1回。2026-10-21 ユーザー：「その PC が止まっていると、
        控えが取れていないことに誰も気づきません」への答え）。合図が戻って、また途切れたら、また知らせる。止めた PC は知らせない。"""
        t = self.w.tenant()
        p = agents.issue(self.a, SYSTEM, t, label="架空の PC")
        watch = lambda: self.a.in_tenant(SYSTEM, t, agents.watch_down)   # noqa: E731
        self.assertEqual(watch(), [])                                     # 払い出したばかり
        with mock.patch.object(settings, "system_value", side_effect=lambda tx, key: 0 if key == "pc.down_after" else None):
            self.assertEqual(watch(), [p.pc_id])
            self.assertEqual(watch(), [])                                 # 同じ途切れは1回だけ
            self.a.in_system(SYSTEM, lambda tx: agents.beat(tx, p.pc_id, []))
            self.assertEqual(watch(), [p.pc_id])                          # 戻って、また途切れた
            self.assertEqual(len(self.notices(t)), 2)
            admin = self.a.in_system(ADMIN, lambda tx: tx.all(
                "SELECT origin_tenant FROM salescall.notification_events WHERE kind = 'pc_down'"))
            self.assertEqual([r["origin_tenant"] for r in admin], [t, t])  # 管理者へも
            self.a.in_tenant(OPERATOR, t, lambda tx: agents.revoke(tx, p.pc_id))
            self.a.in_system(SYSTEM, lambda tx: tx.execute("SELECT pg_sleep(0.01)"))
            self.assertEqual(watch(), [])                                 # 止めた PC は知らせない


if __name__ == "__main__":
    unittest.main()
