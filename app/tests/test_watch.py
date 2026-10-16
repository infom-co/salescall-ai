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
        # フェーズ0で実測した、通話の途中の切断（1011 の内部の誤り・1006）：1回だけつなぎ直す
        self.assertEqual(c("ai", 1011, "Internal error encountered."), "session_drop")
        self.assertEqual(c("ear", 1006, None), "session_drop")
        self.assertEqual(c("ai", 1006, None, unreachable=True), "ai_unreachable")   # 開けなかった＝つながらない
        # 報告の行（公式ではない）：意味は引けるが、種類は決めない（不明として数え、続いたら止める）
        msg = "Operation is not implemented, or supported, or enabled."
        self.assertEqual(c("ai", 1008, msg), "unknown_ai")
        got = errors.lookup(ErrorReport("ai", 1008, msg))
        self.assertEqual((got.origin, got.kind), ("reported", None))
        for e in errors.CATALOG:
            with self.subTest(e.meaning):
                self.assertIn(e.origin, ("official", "observed", "reported"))
                self.assertEqual(e.kind is None, e.origin == "reported")
                self.assertTrue(e.kind is None or e.kind in errors.ACTIONS)
                self.assertTrue(e.code is not None or e.text is not None)
                self.assertTrue(e.meaning and e.ref)
        self.assertIn("ai_server_error", errors.COUNTED_FOR["ai_connection"])

    def test_every_counted_kind_has_its_evidence(self):
        # 続いた回数を数え直す「直った証拠」は、止める種類すべてにあり、データベースが知っている名前だけ
        stopping = {k for k, a in errors.ACTIONS.items() if a.stop_cause is not None}
        self.assertEqual(set(errors.EVIDENCE), stopping)
        self.assertTrue(set(errors.EVIDENCE.values()) <= {"in_conversation", "verified", "ringing", "none"})
        self.assertEqual(errors.counted_with("ear_unreachable"), ("ear_unreachable",))
        self.assertNotIn("ear_unreachable", errors.counted_with("ai_unreachable"))
        for k in stopping:
            with self.subTest(k):
                self.assertTrue(all(errors.ACTIONS[x].stop_scope == errors.ACTIONS[k].stop_scope
                                    for x in errors.counted_with(k)))

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

    def test_a_person_resume_restarts_the_count_and_explain_shows_why(self):
        need = owner_access().in_system(SYSTEM, lambda tx: settings.system_value(tx, "stop.line.stop_after_failures"))
        for _ in range(need):
            self.line_error()
        sid = self.line_stops()[0]["event_id"]
        why = self.a.in_tenant(OPERATOR, self.s["tenant"], lambda tx: stops.explain(tx, sid))
        self.assertEqual(why["cause"]["meaning"], stops.CAUSES["line_failure"].meaning)
        self.assertEqual(len(why["errors"]), need)                    # 数えたエラーと、その意味
        self.assertEqual(why["errors"][0]["meaning"], errors.ACTIONS["line"].meaning)
        self.assertIsNone(why["resolved"])
        self.a.in_tenant(OPERATOR, self.s["tenant"], lambda tx: stops.resume(tx, sid, reason="試験：端末を入れ直した"))
        self.line_error()
        self.assertEqual(self.line_stops(), [])                       # 管理者か契約者が解いたら数え直す（前の数でまた止めない）
        for _ in range(need - 1):
            self.line_error()
        self.assertEqual(len(self.line_stops()), 1)                   # 解いたあと、また続いたら止める
        why = self.a.in_tenant(OPERATOR, self.s["tenant"], lambda tx: stops.explain(tx, sid))
        self.assertEqual((why["resolved"]["actor_kind"], why["resolved"]["reason"]), ("operator", "試験：端末を入れ直した"))


class SystemStopsAndChecks(unittest.TestCase):
    def setUp(self):
        self.a = app_access()
        self.addCleanup(resume_open_system_stops)
        override(self, "stop.ai.probe_interval", 1)

    def config_stop(self):
        """設定の誤り（根源的・1回で全体の新しい発信を待つ）で、システムが止めた全体の停止を作る。"""
        watch.after_error(self.a, SYSTEM, ErrorReport("ai", 1007, "thinking_level is not supported for this model"))
        rows = [r for r in self.a.in_system(SYSTEM, lambda tx: stops.open_stops(tx, tenant_id=None, client_id=None,
                                                                                    business_id=None))
                if r["cause"] == "ai_connection"]
        self.assertEqual(len(rows), 1)
        return rows[0]["event_id"]

    def check(self, sid, ok):
        return self.a.in_system(SYSTEM, lambda tx: watch.record_check(tx, sid, ok, evidence="試験：試しの書き込み"))

    def wait_until_due(self, got):
        now = self.a.in_system(SYSTEM, lambda tx: tx.one("SELECT now() AS t")["t"])
        time.sleep(max(0.0, (got["next_check_at"] - now).total_seconds()) + 0.05)

    def test_a_system_stop_resumes_after_successful_checks(self):
        override(self, "stop.flapping.window", 1)                  # 前の試験で止めた分を、ばたつきに数えない
        time.sleep(1.1)
        sid = self.config_stop()
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
        first = self.config_stop()
        g1 = self.check(first, True)
        self.a.in_system(SYSTEM, lambda tx: stops.resume(tx, first, reason="試験"))
        second = self.config_stop()                                 # 短い間に、また止まった
        g2 = self.check(second, True)
        w1 = g1["next_check_at"] - self.a.in_system(SYSTEM, lambda tx: tx.one(
            "SELECT checked_at FROM salescall.stop_checks WHERE stop_event_id = %s", (first,)))["checked_at"]
        w2 = g2["next_check_at"] - self.a.in_system(SYSTEM, lambda tx: tx.one(
            "SELECT checked_at FROM salescall.stop_checks WHERE stop_event_id = %s", (second,)))["checked_at"]
        self.assertGreater(w2, w1)

    def test_manual_stop_is_not_resumed_by_checks(self):
        with self.assertRaises(Rejected):                    # 全体の緊急停止は、管理者だけ（契約者は押せない）
            self.a.in_system(OPERATOR, lambda tx: stops.stop(tx, scope="system", cause="manual", reason="試験"))
        sid = self.a.in_system(ADMIN, lambda tx: stops.stop(tx, scope="system", cause="manual", reason="試験"))["event_id"]
        with self.assertRaises(watch.NotProbeable):
            self.check(sid, True)
        with self.assertRaises(Rejected):
            self.a.in_system(OPERATOR, lambda tx: stops.resume(tx, sid, reason="試験"))
        self.a.in_system(ADMIN, lambda tx: stops.resume(tx, sid, reason="試験"))

    def test_only_admin_resumes_system_stops_by_hand(self):
        sid = self.config_stop()                                       # システムが止めた全体の停止
        with self.assertRaises(Rejected):
            self.a.in_system(OPERATOR, lambda tx: stops.resume(tx, sid, reason="試験"))
        why = self.a.in_system(ADMIN, lambda tx: stops.explain(tx, sid))
        self.assertEqual(why["who_can_resume"], "管理者")
        self.assertIn("設定の誤り", why["errors"][0]["meaning"])
        self.assertIn("根源的なもの：管理者に直しを依頼しました", why["stop"]["reason"])
        self.a.in_system(ADMIN, lambda tx: stops.resume(tx, sid, reason="試験：設定を直した"))
        why = self.a.in_system(ADMIN, lambda tx: stops.explain(tx, sid))
        self.assertEqual(why["resolved"]["actor_kind"], "admin")

    def test_explain_gives_the_meaning_from_the_catalog(self):
        override(self, "stop.ai.stop_after_failures", 2)
        for _ in range(2):
            watch.after_error(self.a, SYSTEM, ErrorReport("ai", 503, "UNAVAILABLE: The model is overloaded."))
        sid = [r for r in self.a.in_system(SYSTEM, lambda tx: stops.open_stops(tx, tenant_id=None, client_id=None,
                                                                                    business_id=None))
               if r["cause"] == "ai_connection"][0]["event_id"]
        why = self.a.in_system(ADMIN, lambda tx: stops.explain(tx, sid))
        self.assertEqual([(e["kind"], e["origin"]) for e in why["errors"]][:2],
                         [("ai_server_error", "official")] * 2)
        self.assertIn("503", why["errors"][0]["meaning"])

    def test_every_cause_has_an_explanation(self):
        own = owner_access()
        causes = own.in_system(SYSTEM, lambda tx: [r["c"] for r in tx.all(
            "SELECT unnest(enum_range(NULL::salescall.stop_cause))::text AS c")])
        self.assertEqual(sorted(causes), sorted(stops.CAUSES))

    def test_checks_are_frozen(self):
        sid = self.config_stop()
        self.check(sid, False)
        own = owner_access()
        for sql in ("UPDATE salescall.stop_checks SET ok = true WHERE stop_event_id = %s",
                    "DELETE FROM salescall.stop_checks WHERE stop_event_id = %s"):
            with self.subTest(sql=sql), self.assertRaises(Rejected):
                own.in_system(OPERATOR, lambda tx: tx.execute(sql, (sid,)))

    def ai_stops(self):
        return [r for r in self.a.in_system(SYSTEM, lambda tx: stops.open_stops(tx, tenant_id=None, client_id=None,
                                                                                    business_id=None))
                if r["cause"] == "ai_connection"]

    def test_ear_errors_are_not_reset_by_conversations(self):
        # 2026-10-16 見直し：別の耳だけつながらないとき、ほかの通話が会話に入るたびに数え直していて、止まらなかった
        override(self, "stop.ai.stop_after_failures", 2)
        w = World(); s = w.setup()
        ear = ErrorReport("ear", None, None, unreachable=True)
        watch.after_error(self.a, SYSTEM, ear)
        aid = w.insert_claim(s)
        for st in ("dialing", "answered", "in_conversation"):
            w.move(s, aid, st)                                       # 会話AIは動いている（別の耳の直った証拠ではない）
        watch.after_error(self.a, SYSTEM, ear)
        self.assertEqual(len(self.ai_stops()), 1)

    def test_counting_needs_the_transaction_of_that_scope(self):
        w = World(); s = w.setup(); other = World().setup()
        with self.assertRaises(PermissionError):                     # 全体のエラーの数は、全体の取引だけ
            self.a.in_tenant(SYSTEM, s["tenant"], lambda tx: watch.decide_stop(tx, "ai_unreachable"))
        n = self.a.in_tenant(SYSTEM, s["tenant"], lambda tx: tx.one(
            "SELECT salescall.consecutive_errors(%s, 'line_failure', 'ringing', %s) AS n", (["line"], other["line"]))["n"])
        self.assertIsNone(n)                                         # ほかの契約者の回線は数えない
        spent = self.a.in_tenant(SYSTEM, s["tenant"], lambda tx: tx.one("SELECT salescall.spent_today_all() AS y")["y"])
        self.assertIsNone(spent)                                     # 全体の費用は、全体の取引だけ
        listed = self.a.in_tenant(SYSTEM, s["tenant"], lambda tx: tx.all(
            "SELECT * FROM salescall.open_system_stops('budget')"))
        self.assertEqual(listed, [])

    def test_checks_of_system_stops_only_from_the_system_transaction(self):
        sid = self.config_stop()
        s = World().setup()
        due = self.a.in_tenant(SYSTEM, s["tenant"], watch.due_checks)
        self.assertNotIn(sid, [r["event_id"] for r in due])          # 契約者の取引からは、全体の停止を確かめない
        with self.assertRaises(Rejected):
            self.a.in_tenant(SYSTEM, s["tenant"], lambda tx: watch.record_check(tx, sid, True, evidence="試験"))

    def test_every_error_reaches_the_admin_and_contractors_only_by_the_table(self):
        # 2026-10-16 見直し：キーの無効化（直せるのは管理者だけ）が、管理者に届いていなかった
        s = World().setup()
        watch.after_error(self.a, SYSTEM, ErrorReport("ai", 1007, "API key not valid. Please pass a valid API key."),
                          tenant_id=s["tenant"], client_id=s["client"])
        like = f"error:key_specific:{s['tenant']}:{s['client']}:%"
        sql = "SELECT for_admin FROM salescall.notification_events WHERE event_key LIKE %s"
        self.assertEqual([r["for_admin"] for r in self.a.in_system(ADMIN, lambda tx: tx.all(sql, ("admin:" + like,)))],
                         [True])
        self.assertEqual(self.a.in_client(OPERATOR, s["tenant"], s["client"], lambda tx: tx.all(sql, (like,))), [])

    def test_a_storage_failure_holds_only_that_row_and_asks_the_admin(self):
        """保存の失敗は全体を止めない：その行だけ判定待ちにして（かけ直さない）、管理者に直しを依頼し、ほかの行は続ける
        （2026-10-16 ユーザー：「理由を記載とあなたへの修正依頼をして、次の行に進んだらいいでしょ」）。"""
        w = World(); s = w.setup()
        aid = w.insert_claim(s)
        w.move(s, aid, "dialing")
        watch.after_error(self.a, SYSTEM, ErrorReport("storage", None, "試験：書き込めない"),
                          tenant_id=s["tenant"], client_id=s["client"], attempt_id=aid)
        self.assertEqual([r for r in self.a.in_system(SYSTEM, lambda tx: stops.open_stops(
            tx, tenant_id=None, client_id=None, business_id=None)) if r["cause"] == "storage_failure"], [])
        kind = self.a.in_client(SYSTEM, s["tenant"], s["client"], lambda tx: tx.one(
            "SELECT kind FROM salescall.task_next_actions WHERE task_id = %s ORDER BY seq DESC LIMIT 1", (s["task"],)))["kind"]
        self.assertEqual(kind, "hold")
        admin = self.a.in_system(ADMIN, lambda tx: tx.all(
            "SELECT summary FROM salescall.notification_events WHERE event_key LIKE %s",
            (f"admin:error:storage:{s['tenant']}:{s['client']}:%",)))
        self.assertTrue(admin and admin[0]["summary"].startswith("直しの依頼"))

    def test_notify_once_an_hour(self):
        for _ in range(2):
            watch.after_error(self.a, SYSTEM, ErrorReport("ai", 1008, "Your project has been denied access"))
        rows = self.a.in_system(SYSTEM, lambda tx: tx.all(
            "SELECT event_key FROM salescall.notification_events WHERE event_key LIKE 'error:project_denied:None:None:%'"
            " AND occurred_at > now() - interval '1 minute'"))
        self.assertEqual(len(rows), 1)


if __name__ == "__main__":
    unittest.main()
