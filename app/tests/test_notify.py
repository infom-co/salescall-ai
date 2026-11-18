"""知らせの台帳の試験（Q23）：出来事は1回だけ・見える範囲・変えられない・止めた／解いた／暦／判定できないで入る。"""

from __future__ import annotations

import unittest
from unittest import mock

from salescall.core import calendar, notify, stops
from salescall.core.access import Rejected
from tests.support import ADMIN, OPERATOR, SYSTEM, World, app_access, owner_access


def events(access, tenant=None, client=None, key_prefix=""):
    sql = ("SELECT kind, event_key, tenant_id, client_id FROM salescall.notification_events WHERE event_key LIKE %s"
           " ORDER BY event_id")
    if tenant is None:
        return access.in_system(SYSTEM, lambda tx: tx.all(sql, (key_prefix + "%",)))
    if client is None:
        return access.in_tenant(SYSTEM, tenant, lambda tx: tx.all(sql, (key_prefix + "%",)))
    return access.in_client(SYSTEM, tenant, client, lambda tx: tx.all(sql, (key_prefix + "%",)))


class Ledger(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.a = app_access()

    def test_same_event_only_once_and_unknown_kind_is_refused(self):
        s = self.w.setup()
        key = f"test:{s['task']}"
        emit = lambda: self.a.in_client(SYSTEM, s["tenant"], s["client"], lambda tx: notify.emit(
            tx, "judge_unknown", key=key, summary="試験"))
        self.assertTrue(emit())
        self.assertFalse(emit())
        self.assertEqual(len(events(self.a, s["tenant"], s["client"], key)), 1)
        with self.assertRaises(notify.UnknownKind):
            self.a.in_client(SYSTEM, s["tenant"], s["client"], lambda tx: notify.emit(tx, "no_such", key="x", summary="x"))

    def test_who_can_see(self):
        s = self.w.setup()
        other_client = self.w.client(s["tenant"])
        stranger = self.w.setup()
        key = f"test:{s['task']}"
        self.a.in_client(SYSTEM, s["tenant"], s["client"], lambda tx: notify.emit(tx, "judge_unknown", key=key, summary="x"))
        self.assertEqual(len(events(self.a, s["tenant"], s["client"], key)), 1)        # その依頼者
        self.assertEqual(len(events(self.a, s["tenant"], None, key)), 1)               # 契約者
        self.assertEqual(events(self.a, s["tenant"], other_client, key), [])           # 同じ契約者のほかの依頼者
        self.assertEqual(events(self.a, stranger["tenant"], None, key), [])            # ほかの契約者
        self.assertEqual(events(self.a, None, None, key), [])                          # 全体の取引

    def test_admin_copy_is_seen_only_by_the_admin(self):
        """管理者あての写し（プログラムの誤りなど。2026-10-16 ユーザー承認）：管理者と送る処理だけが読める。契約者には見えない。"""
        s = self.w.setup()
        stranger = self.w.setup()
        key = f"test:{s['task']}"
        self.a.in_client(SYSTEM, s["tenant"], s["client"], lambda tx: notify.emit(
            tx, "judge_unknown", key=key, summary="x", refs={"attempt_id": "a1"}, admin=True))
        sql = "SELECT tenant_id, refs, for_admin FROM salescall.notification_events WHERE event_key = %s"
        admin = self.a.in_system(ADMIN, lambda tx: tx.all(sql, ("admin:" + key,)))
        self.assertEqual(len(admin), 1)
        self.assertEqual((admin[0]["tenant_id"], admin[0]["for_admin"], admin[0]["refs"]["client_id"]),
                         (None, True, str(s["client"])))                     # どの依頼者の出来事かは refs に
        self.assertEqual(len(events(self.a, s["tenant"], s["client"], key)), 1)        # 契約者の台帳にも入る
        for t, c in ((s["tenant"], s["client"]), (stranger["tenant"], None)):
            got = (self.a.in_client(OPERATOR, t, c, lambda tx: tx.all(sql, ("admin:" + key,))) if c is not None
                   else self.a.in_tenant(OPERATOR, t, lambda tx: tx.all(sql, ("admin:" + key,))))
            self.assertEqual(got, [])                                         # 契約者には見えない（自分の分もほかの分も）
        with self.assertRaises(Rejected):                                     # 契約者は、管理者あての行を自分で書けない
            self.a.in_system(OPERATOR, lambda tx: tx.execute(
                "INSERT INTO salescall.notification_events (kind, event_key, summary, for_admin)"
                " VALUES ('judge_unknown', 'forged', 'x', true)"))

    def test_frozen(self):
        s = self.w.setup()
        key = f"test:{s['task']}"
        self.a.in_client(SYSTEM, s["tenant"], s["client"], lambda tx: notify.emit(tx, "judge_unknown", key=key, summary="x"))
        own = owner_access()
        for sql in ("UPDATE salescall.notification_events SET summary = 'y' WHERE event_key = %s",
                    "DELETE FROM salescall.notification_events WHERE event_key = %s"):
            with self.subTest(sql=sql), self.assertRaises(Rejected):
                own.in_system(OPERATOR, lambda tx: tx.execute(sql, (key,)))


class Emitters(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.a = app_access()

    def test_stop_and_resume(self):
        s = self.w.setup()
        got = self.a.in_client(SYSTEM, s["tenant"], s["client"], lambda tx: stops.stop(
            tx, scope="client", cause="line_failure", reason="試験", tenant_id=s["tenant"], client_id=s["client"]))
        self.a.in_client(SYSTEM, s["tenant"], s["client"], lambda tx: stops.resume(tx, got["event_id"], reason="試験"))
        kinds = [e["kind"] for e in events(self.a, s["tenant"], s["client"])
                 if e["event_key"] in (f"stop:{got['event_id']}",) or e["event_key"].startswith("resume:")]
        self.assertIn("stopped", kinds)
        self.assertIn("resumed", kinds)

    def test_calendar_failure_is_noticed_once_a_day(self):
        def broken():
            raise OSError("試験：取りに行けない")
        with mock.patch.object(calendar, "needs_fetch", return_value=True):
            for _ in range(2):
                with self.assertRaises(OSError):
                    calendar.sync_official(self.a, SYSTEM, fetch=broken)
        today = self.a.in_system(SYSTEM, lambda tx: tx.one("SELECT salescall.jst(now())::date AS d"))["d"]
        rows = events(self.a, None, None, "admin:calendar_sync_failed:")   # 暦は管理者の持ち物：管理者あてだけ（Intent.md 1-4）
        self.assertEqual([r["event_key"] for r in rows if r["event_key"].endswith(str(today))],
                         [f"admin:calendar_sync_failed:{today}"])
        self.assertEqual(events(self.a, None, None, "calendar_sync_failed:"), [])   # 契約者の台帳には入れない


if __name__ == "__main__":
    unittest.main()
