"""エラーの分類と処置（Q9）・見守り（止める・解く。9-1）の試験。"""

from __future__ import annotations

import json
import time
import unittest

from salescall.core import errors, settings, stops, watch
from salescall.core.access import Rejected
from salescall.core.errors import ErrorReport
from tests.support import ADMIN, OPERATOR, SYSTEM, World, app_access, owner_access


class Classify(unittest.TestCase):
    def test_only_observed_pairs_have_a_kind(self):
        c = lambda *a, **k: errors.classify(ErrorReport(*a, **k))
        self.assertEqual(c("ai", 1007, "API key not valid. Please pass a valid API key."), "key_specific")
        self.assertEqual(c("ai", 1008, "Your project has been denied access."), "project_denied")
        self.assertEqual(c("ai", 1007, "thinking_level is not supported for this model"), "config_error")
        self.assertEqual(c("ai", 1011, "You exceeded your current quota"), "project_quota")
        self.assertEqual(c("ai", 1007, "見たことのない文面"), "unknown_ai")          # 番号だけでは決めない
        self.assertEqual(c("ai", None, None, unreachable=True), "ai_unreachable")
        self.assertEqual(c("ear", None, None, unreachable=True), "ear_unreachable")
        self.assertEqual(c("ear", 1099, "x"), "unknown_other")
        self.assertEqual((c("line", None, "x"), c("storage", None, "x"), c("delivery", 500, "x")),
                         ("line", "storage", "delivery"))
        with self.assertRaises(ValueError):
            c("phone", None, None)

    def test_catalog_official_and_observed_decide_reported_only_explains(self):
        c = lambda *a, **k: errors.classify(ErrorReport(*a, **k))
        self.assertEqual(c("ai", 503, "UNAVAILABLE: The model is overloaded."), "ai_server_error")
        self.assertEqual(c("ai", None, "status: DEADLINE_EXCEEDED"), "ai_server_error")
        self.assertEqual(c("ai", 429, "Resource has been exhausted"), "project_denied")
        self.assertEqual(c("ai", 400, "FAILED_PRECONDITION: free tier is not available"), "config_error")
        self.assertEqual(c("ai", 1011, "You exceeded your current quota"), "project_quota")   # 実測の組が先に当たる
        # 報告の行（公式ではない）：意味は引けるが、種類は決めない（不明として数え、続いたら止める）
        self.assertEqual(c("ai", 1011, "Internal error encountered."), "unknown_ai")
        got = errors.lookup(ErrorReport("ai", 1011, "Internal error encountered."))
        self.assertEqual((got.origin, got.kind), ("reported", None))
        for e in errors.CATALOG:
            with self.subTest(e.meaning):
                self.assertIn(e.origin, ("official", "observed", "reported"))
                self.assertEqual(e.kind is None, e.origin == "reported")
                self.assertTrue(e.kind is None or e.kind in errors.ACTIONS)
                self.assertTrue(e.code is not None or e.text is not None)
                self.assertTrue(e.meaning and e.ref)
        self.assertIn("ai_server_error", errors.COUNTED_FOR["ai_connection"])

    def test_the_table_is_complete_and_never_switches_keys_on_unknown(self):
        for kind, a in errors.ACTIONS.items():
            with self.subTest(kind):
                self.assertIn(a.key, ("keep", "mark_key_unusable", "other_project_only"))
                self.assertEqual(a.stop_cause is None, a.stop_scope is None)
                if kind.startswith("unknown") or kind == "config_error":
                    self.assertEqual(a.key, "keep")
        own = owner_access()
        keys = {a.stop_after for a in errors.ACTIONS.values() if a.stop_after}
        for k in keys:   # 「何回続いたら」の設定は、どれも値がある
            self.assertGreaterEqual(own.in_system(SYSTEM, lambda tx: settings.system_value(tx, k)), 1)


def resume_open_system_stops():
    """全体の停止を残さない（ほかの試験の門を止めない）。"""
    a = app_access()
    for row in a.in_system(SYSTEM, lambda tx: stops.open_stops(tx, tenant_id=None, client_id=None, business_id=None)):
        if row["scope"] == "system":
            a.in_system(SYSTEM, lambda tx: stops.resume(tx, row["event_id"], reason="試験のあと解く"))


def override(test, key, value):
    own = owner_access()
    orig = own.in_system(SYSTEM, lambda tx: tx.one(
        "SELECT value, status, rationale, review_when, set_by FROM salescall.setting_values"
        " WHERE key = %s AND scope = 'system' ORDER BY value_id DESC LIMIT 1", (key,)))
    put = lambda v, st, ra, rw, by: own.in_system(SYSTEM, lambda tx: tx.execute(
        "INSERT INTO salescall.setting_values (key, scope, value, status, rationale, review_when, set_by)"
        " VALUES (%s, 'system', %s, %s, %s, %s, %s)", (key, json.dumps(v), st, ra, rw, by)))
    put(value, "provisional", "試験の一時的な値", "試験のあと戻す", "試験")
    test.addCleanup(put, orig["value"], orig["status"], orig["rationale"], orig["review_when"], orig["set_by"])


class LineStops(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.a = app_access()
        self.s = self.w.setup()

    def line_error(self):
        return watch.after_error(self.a, SYSTEM, ErrorReport("line", None, "試験：端末が応答しない"),
                                 tenant_id=self.s["tenant"], client_id=self.s["client"], line_id=self.s["line"])

    def line_stops(self):
        return [r for r in self.a.in_tenant(SYSTEM, self.s["tenant"], lambda tx: stops.open_stops(
            tx, tenant_id=self.s["tenant"], client_id=None, business_id=None, line_id=self.s["line"]))
            if r["scope"] == "line"]

    def test_consecutive_line_errors_stop_only_that_line(self):
        need = owner_access().in_system(SYSTEM, lambda tx: settings.system_value(tx, "stop.line.stop_after_failures"))
        for _ in range(need - 1):
            self.line_error()
        self.assertEqual(self.line_stops(), [])
        self.line_error()
        got = self.line_stops()
        self.assertEqual([r["cause"] for r in got], ["line_failure"])
        self.line_error()
        self.assertEqual(len(self.line_stops()), 1)                  # 重ねて止めない

    def test_a_call_that_rang_resets_the_count(self):
        need = owner_access().in_system(SYSTEM, lambda tx: settings.system_value(tx, "stop.line.stop_after_failures"))
        for _ in range(need - 1):
            self.line_error()
        aid = self.w.insert_claim(self.s)
        for st in ("dialing", "ringing"):
            self.w.move(self.s, aid, st)                             # 回線は動いている
        for _ in range(need - 1):
            self.line_error()
        self.assertEqual(self.line_stops(), [])


class SystemStopsAndChecks(unittest.TestCase):
    def setUp(self):
        self.a = app_access()
        self.addCleanup(resume_open_system_stops)
        override(self, "stop.storage.probe_interval", 1)

    def storage_stop(self):
        watch.after_error(self.a, SYSTEM, ErrorReport("storage", None, "試験：書き込めない"))
        rows = [r for r in self.a.in_system(SYSTEM, lambda tx: stops.open_stops(tx, tenant_id=None, client_id=None,
                                                                                    business_id=None))
                if r["cause"] == "storage_failure"]
        self.assertEqual(len(rows), 1)
        return rows[0]["event_id"]

    def check(self, sid, ok):
        return self.a.in_system(SYSTEM, lambda tx: watch.record_check(tx, sid, ok, evidence="試験：試しの書き込み"))

    def wait_until_due(self, got):
        now = self.a.in_system(SYSTEM, lambda tx: tx.one("SELECT now() AS t")["t"])
        time.sleep(max(0.0, (got["next_check_at"] - now).total_seconds()) + 0.05)

    def test_storage_failure_stops_at_once_and_resumes_after_successes(self):
        override(self, "stop.flapping.window", 1)                  # 前の試験で止めた分を、ばたつきに数えない
        time.sleep(1.1)
        sid = self.storage_stop()
        self.assertIn(sid, [r["event_id"] for r in self.a.in_system(SYSTEM, watch.due_checks)])
        got = self.check(sid, True)
        with self.assertRaises(watch.NotDue):                      # 待ちの途中の確かめは受け付けない
            self.check(sid, True)
        self.wait_until_due(got)
        got = self.check(sid, False)                              # 失敗で数え直し
        self.assertEqual(got["streak"], 0)
        for n in range(1, got["need"] + 1):
            self.wait_until_due(got)
            got = self.check(sid, True)
            self.assertEqual(got["streak"], n)
        self.assertIsNotNone(got["resumed"])
        self.assertNotIn(sid, [r["event_id"] for r in self.a.in_system(SYSTEM, lambda tx: stops.open_stops(
            tx, tenant_id=None, client_id=None, business_id=None))])

    def test_flapping_makes_the_wait_longer(self):
        first = self.storage_stop()
        g1 = self.check(first, True)
        self.a.in_system(SYSTEM, lambda tx: stops.resume(tx, first, reason="試験"))
        second = self.storage_stop()                                 # 短い間に、また止まった
        g2 = self.check(second, True)
        w1 = g1["next_check_at"] - self.a.in_system(SYSTEM, lambda tx: tx.one(
            "SELECT checked_at FROM salescall.stop_checks WHERE stop_event_id = %s", (first,)))["checked_at"]
        w2 = g2["next_check_at"] - self.a.in_system(SYSTEM, lambda tx: tx.one(
            "SELECT checked_at FROM salescall.stop_checks WHERE stop_event_id = %s", (second,)))["checked_at"]
        self.assertGreater(w2, w1)

    def test_manual_stop_is_not_resumed_by_checks(self):
        with self.assertRaises(Rejected):                    # 全体の緊急停止は、システムの管理者だけ（運営者は押せない）
            self.a.in_system(OPERATOR, lambda tx: stops.stop(tx, scope="system", cause="manual", reason="試験"))
        sid = self.a.in_system(ADMIN, lambda tx: stops.stop(tx, scope="system", cause="manual", reason="試験"))["event_id"]
        with self.assertRaises(watch.NotProbeable):
            self.check(sid, True)
        with self.assertRaises(Rejected):
            self.a.in_system(OPERATOR, lambda tx: stops.resume(tx, sid, reason="試験"))
        self.a.in_system(ADMIN, lambda tx: stops.resume(tx, sid, reason="試験"))

    def test_checks_are_frozen(self):
        sid = self.storage_stop()
        self.check(sid, False)
        own = owner_access()
        for sql in ("UPDATE salescall.stop_checks SET ok = true WHERE stop_event_id = %s",
                    "DELETE FROM salescall.stop_checks WHERE stop_event_id = %s"):
            with self.subTest(sql=sql), self.assertRaises(Rejected):
                own.in_system(OPERATOR, lambda tx: tx.execute(sql, (sid,)))

    def test_notify_once_an_hour(self):
        for _ in range(2):
            watch.after_error(self.a, SYSTEM, ErrorReport("ai", 1008, "Your project has been denied access"))
        rows = self.a.in_system(SYSTEM, lambda tx: tx.all(
            "SELECT event_key FROM salescall.notification_events WHERE event_key LIKE 'error:project_denied:None:None:%'"
            " AND occurred_at > now() - interval '1 minute'"))
        self.assertEqual(len(rows), 1)


if __name__ == "__main__":
    unittest.main()
