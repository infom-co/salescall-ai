"""エラーの分類と処置（Q9）・見守り（止める・解く。9-1）の試験。"""

from __future__ import annotations

import json
import time
import unittest
from datetime import timedelta
from unittest import mock

from salescall.core import errors, fix_requests, settings, stops, watch
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
        self.assertEqual(c("ear", 1099, "x"), "ear_error")                 # 別の耳の不明：止めない（CL-1 に修正依頼）
        self.assertEqual(c("ear", 503, "UNAVAILABLE"), "ear_unreachable")    # 別の耳の一時的な誤り：止めない
        self.assertEqual(c("ear", 1007, "thinking_level is not supported for this model"), "ear_error")
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
        self.assertNotIn("ear_unreachable", errors.counted_with("ai_unreachable"))
        for k in stopping:
            with self.subTest(k):
                self.assertTrue(all(errors.ACTIONS[x].stop_scope == errors.ACTIONS[k].stop_scope
                                    for x in errors.counted_with(k)))

    def test_every_temporary_stop_has_someone_to_fix_it_when_it_lasts(self):
        # 一時的なもので止める種類は、止まったまま続いたら修正依頼を出す担当がある（見直し8：前は、止まると新しいエラーが起きないので、
        # 何時間止まっても修正依頼が出なかった）。根源的なもの（fixer）は、起きたときに出している
        for k, a in errors.ACTIONS.items():
            if a.stop_cause is not None:
                with self.subTest(k):
                    self.assertIsNotNone(a.fixer or a.lasting_fixer)

    def test_every_kind_has_a_short_label(self):
        self.assertEqual(set(errors.LABELS), set(errors.ACTIONS))           # 知らせは短い名前（2026-10-17）
        self.assertTrue(all(len(v) <= 25 for v in errors.LABELS.values()))

    def test_the_table_is_complete_and_never_switches_keys_on_unknown(self):
        for kind, a in errors.ACTIONS.items():
            with self.subTest(kind):
                self.assertIn(a.key, ("keep", "mark_key_unusable", "next_key"))
                self.assertEqual(a.stop_cause is None, a.stop_scope is None)
                self.assertIn(a.stop_scope, (None, "tenant", "line"))     # エラーで全体は止めない（会話AIの枠は契約者ごと）
                if kind.startswith("unknown") or kind == "config_error":
                    self.assertEqual(a.key, "keep")
        own = owner_access()
        keys = {a.stop_after for a in errors.ACTIONS.values() if a.stop_after}
        for k in keys:   # 「何回続いたら」の設定は、どれも値がある
            self.assertGreaterEqual(own.in_system(SYSTEM, lambda tx: settings.initial_value(tx, k)), 1)


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
        need = owner_access().in_system(SYSTEM, lambda tx: settings.initial_value(tx, "stop.line.stop_after_failures"))
        for _ in range(need - 1):
            self.line_error()
        self.assertEqual(self.line_stops(), [])
        self.line_error()
        got = self.line_stops()
        self.assertEqual([r["cause"] for r in got], ["line_failure"])
        self.line_error()
        self.assertEqual(len(self.line_stops()), 1)                  # 重ねて止めない

    def test_the_contractor_decides_how_many_failures_stop_a_line(self):
        """止める回数は契約者ごと（2026-10-17 ユーザー：「会話AI・回線の異常で止める回数と、解くまでの確かめ　契約者」）。"""
        self.w.a.in_tenant(OPERATOR, self.s["tenant"], lambda tx: tx.execute(
            "INSERT INTO salescall.setting_values (key, scope, tenant_id, value, status, rationale, review_when, set_by)"
            " VALUES ('stop.line.stop_after_failures', 'tenant', %s, '1', 'decided', '契約者の決まり', '試験', '試験')",
            (self.s["tenant"],)))
        self.line_error()
        self.assertEqual([r["cause"] for r in self.line_stops()], ["line_failure"])

    def test_a_call_that_rang_resets_the_count(self):
        need = owner_access().in_system(SYSTEM, lambda tx: settings.initial_value(tx, "stop.line.stop_after_failures"))
        for _ in range(need - 1):
            self.line_error()
        aid = self.w.insert_claim(self.s)
        for st in ("dialing", "ringing"):
            self.w.move(self.s, aid, st)                             # 回線は動いている
        for _ in range(need - 1):
            self.line_error()
        self.assertEqual(self.line_stops(), [])

    def test_a_person_resume_restarts_the_count_and_explain_shows_why(self):
        need = owner_access().in_system(SYSTEM, lambda tx: settings.initial_value(tx, "stop.line.stop_after_failures"))
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


class TenantAiStopsAndChecks(unittest.TestCase):
    """会話AIの異常で止めるのは、その契約者だけ（会話AIの枠は契約者ごと＝キー・プロジェクトは契約者のもの。2026-10-16 ユーザー。
    2026-10-17 見直し：前は全体で数えて全体を止めていた）。"""
    def setUp(self):
        self.a = app_access()
        self.s = World().setup()
        override(self, "stop.ai.probe_interval", 1)

    def ai_error(self, report, s=None):
        s = s or self.s
        return watch.after_error(self.a, SYSTEM, report, tenant_id=s["tenant"], client_id=s["client"])

    def ai_stops(self, s=None):
        s = s or self.s
        return [r for r in self.a.in_tenant(SYSTEM, s["tenant"], lambda tx: stops.open_stops(
            tx, tenant_id=s["tenant"], client_id=None, business_id=None)) if r["cause"] == "ai_connection"]

    def config_stop(self):
        """設定の誤り（根源的・1回で、その契約者の新しい発信を待つ）で、システムが止めた契約者の停止を作る。"""
        self.ai_error(ErrorReport("ai", 1007, "thinking_level is not supported for this model"))
        rows = self.ai_stops()
        self.assertEqual([r["scope"] for r in rows], ["tenant"])
        return rows[0]["event_id"]

    def check(self, sid, ok):
        return self.a.in_tenant(SYSTEM, self.s["tenant"], lambda tx: watch.record_check(tx, sid, ok, evidence="試験：試しのセッション"))

    def wait_until_due(self, got):
        now = self.a.in_system(SYSTEM, lambda tx: tx.one("SELECT now() AS t")["t"])
        time.sleep(max(0.0, (got["next_check_at"] - now).total_seconds()) + 0.05)

    def test_one_contractors_ai_errors_stop_only_that_contractor(self):
        override(self, "stop.ai.stop_after_failures", 2)
        other = World().setup()
        self.ai_error(ErrorReport("ai", 503, "UNAVAILABLE: The model is overloaded."))
        self.ai_error(ErrorReport("ai", 503, "UNAVAILABLE: The model is overloaded."), s=other)
        self.assertEqual((self.ai_stops(), self.ai_stops(other)), ([], []))   # 契約者ごとに数える（1回ずつ）
        self.ai_error(ErrorReport("ai", 503, "UNAVAILABLE: The model is overloaded."))
        self.assertEqual(len(self.ai_stops()), 1)
        self.assertEqual(self.ai_stops(other), [])                     # ほかの契約者は止めない
        claim = self.a.in_tenant(SYSTEM, other["tenant"], lambda tx: stops.open_stops(
            tx, tenant_id=other["tenant"], client_id=other["client"], business_id=None))
        self.assertEqual(claim, [])

    def test_a_stop_kind_needs_its_contractor(self):
        with self.assertRaises(ValueError):                            # どの契約者のキーで起きたか分からなければ数えられない
            watch.after_error(self.a, SYSTEM, ErrorReport("ai", 503, "UNAVAILABLE"))

    def test_an_ai_stop_resumes_after_successful_checks(self):
        override(self, "stop.flapping.window", 1)                  # 前の試験で止めた分を、ばたつきに数えない
        time.sleep(1.1)
        sid = self.config_stop()
        self.assertIn(sid, [r["event_id"] for r in self.a.in_tenant(SYSTEM, self.s["tenant"], watch.due_checks)])
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
        self.assertEqual(self.ai_stops(), [])

    def test_flapping_makes_the_wait_longer(self):
        first = self.config_stop()
        g1 = self.check(first, True)
        self.a.in_tenant(OPERATOR, self.s["tenant"], lambda tx: stops.resume(tx, first, reason="試験"))
        second = self.config_stop()                                 # 短い間に、また止まった
        g2 = self.check(second, True)
        at = lambda sid: self.a.in_tenant(SYSTEM, self.s["tenant"], lambda tx: tx.one(
            "SELECT checked_at FROM salescall.stop_checks WHERE stop_event_id = %s", (sid,)))["checked_at"]
        self.assertGreater(g2["next_check_at"] - at(second), g1["next_check_at"] - at(first))

    def test_manual_stop_is_not_resumed_by_checks(self):
        with self.assertRaises(Rejected):                    # 全体の緊急停止は、管理者だけ（契約者は押せない）
            self.a.in_system(OPERATOR, lambda tx: stops.stop(tx, scope="system", cause="manual", reason="試験"))
        sid = self.a.in_system(ADMIN, lambda tx: stops.stop(tx, scope="system", cause="manual", reason="試験"))["event_id"]
        with self.assertRaises(watch.NotProbeable):
            self.a.in_system(SYSTEM, lambda tx: watch.record_check(tx, sid, True, evidence="試験"))
        with self.assertRaises(Rejected):
            self.a.in_system(OPERATOR, lambda tx: stops.resume(tx, sid, reason="試験"))
        self.a.in_system(ADMIN, lambda tx: stops.resume(tx, sid, reason="試験"))

    def test_the_contractor_can_resume_and_cl1_is_asked_to_fix(self):
        sid = self.config_stop()                                       # システムが止めた契約者の停止（根源的）
        why = self.a.in_tenant(OPERATOR, self.s["tenant"], lambda tx: stops.explain(tx, sid))
        self.assertEqual(why["who_can_resume"], "その契約者・管理者")
        self.assertIn("設定の誤り", why["errors"][0]["meaning"])
        self.assertIn("根源的なもの：CL-1 に修正依頼を出しました", why["stop"]["reason"])
        admin = self.a.in_system(ADMIN, lambda tx: tx.all(
            "SELECT summary FROM salescall.notification_events WHERE event_key = %s", (f"admin:stop:{sid}",)))
        self.assertEqual([a["summary"] for a in admin], ["発信を止めました（契約者：会話AIの異常）。"])
        self.a.in_tenant(OPERATOR, self.s["tenant"], lambda tx: stops.resume(tx, sid, reason="試験：設定を直した"))
        why = self.a.in_tenant(OPERATOR, self.s["tenant"], lambda tx: stops.explain(tx, sid))
        self.assertEqual(why["resolved"]["actor_kind"], "operator")

    def test_explain_gives_the_meaning_from_the_catalog(self):
        override(self, "stop.ai.stop_after_failures", 2)
        for _ in range(2):
            self.ai_error(ErrorReport("ai", 503, "UNAVAILABLE: The model is overloaded."))
        sid = self.ai_stops()[0]["event_id"]
        why = self.a.in_tenant(OPERATOR, self.s["tenant"], lambda tx: stops.explain(tx, sid))
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

    def test_ear_errors_never_stop_calling_but_tell_the_administrator(self):
        """別の耳（通話後の検証）につながらなくても、発信は止めない（2026-10-17：止めるのはお客様に影響が出るときだけ。
        検証が済むまで、その行と同じお客様には門がかけない）。管理者に知らせる。"""
        override(self, "stop.ai.stop_after_failures", 2)
        ear = ErrorReport("ear", None, None, unreachable=True)
        for _ in range(5):
            self.ai_error(ear)
        self.assertEqual(self.ai_stops(), [])
        n = self.a.in_system(SYSTEM, lambda tx: tx.one(
            "SELECT count(*) AS n FROM salescall.notification_events WHERE for_admin AND kind = 'error_notice'"
            " AND refs ->> 'kind' = 'ear_unreachable'"))["n"]
        self.assertGreaterEqual(n, 1)

    def test_no_ear_error_ever_stops_calling(self):
        """別の耳（通話後の検証）のエラーは、どの種類でも発信を止めない（2026-10-17 の計画書との突き合わせ：前は別の耳の
        Google の側の誤り 503 が続く・設定の誤り 1007 が1回で、その契約者の発信を止めていた）。"""
        override(self, "stop.ai.stop_after_failures", 2)
        for report in (ErrorReport("ear", 503, "UNAVAILABLE"), ErrorReport("ear", 1007, "thinking_level is not supported"),
                       ErrorReport("ear", 1099, "x")):
            for _ in range(3):
                self.ai_error(report)
        self.assertEqual(self.ai_stops(), [])
        for k in [k for k in errors.ACTIONS if k.startswith("ear_")]:
            self.assertIsNone(errors.ACTIONS[k].stop_cause)

    def test_counting_needs_the_transaction_of_that_scope(self):
        other = World().setup()
        with self.assertRaises(PermissionError):                     # 会話AIのエラーの数は、その契約者の取引だけ
            self.a.in_system(SYSTEM, lambda tx: watch.decide_stop(tx, "ai_unreachable"))
        n = self.a.in_tenant(SYSTEM, self.s["tenant"], lambda tx: tx.one(
            "SELECT salescall.consecutive_errors(%s, 'line_failure', 'ringing', %s) AS n", (["line"], other["line"]))["n"])
        self.assertIsNone(n)                                         # ほかの契約者の回線は数えない
        spent = self.a.in_client(SYSTEM, self.s["tenant"], self.s["client"], lambda tx: tx.one(
            "SELECT salescall.spent_today_tenant() AS y")["y"])
        self.assertIsNone(spent)                                     # 契約者の今日の費用は、その契約者の取引だけ（0064）

    def test_checks_only_from_that_contractor(self):
        sid = self.config_stop()
        other = World().setup()
        due = self.a.in_tenant(SYSTEM, other["tenant"], watch.due_checks)
        self.assertNotIn(sid, [r["event_id"] for r in due])          # ほかの契約者の取引からは、確かめない
        with self.assertRaises((LookupError, Rejected)):
            self.a.in_tenant(SYSTEM, other["tenant"], lambda tx: watch.record_check(tx, sid, True, evidence="試験"))
        with self.assertRaises(Rejected):                            # データベースも守る（処理の確かめを通らない書き方でも）
            self.a.in_tenant(SYSTEM, other["tenant"], lambda tx: tx.execute(
                "INSERT INTO salescall.stop_checks (stop_event_id, ok, streak, next_check_at, evidence)"
                " VALUES (%s, true, 1, now() + interval '1 hour', '試験')", (sid,)))
        due = self.a.in_system(SYSTEM, watch.due_checks)
        self.assertNotIn(sid, [r["event_id"] for r in due])          # 全体の取引からも、契約者の停止は確かめない

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

    def test_a_storage_failure_does_not_stop_and_asks_cl2_to_fix_it(self):
        """保存の失敗は止めない：CL-2 に修正依頼を出し、ほかの行は続ける。管理者は確かめ・承認だけ（2026-10-16 ユーザー：「理由を記載とあなたへの
        修正依頼をして、次の行に進んだらいいでしょ」）。保存できなかった通話は締めが済まないので、門がその行と同じお客様を守る。
        2026-10-17：前は行に「判定待ち」を別に付けていたが、締めのあとに付くと解く道が無く、止まり続けたので外した。"""
        w = World(); s = w.setup()
        aid = w.insert_claim(s)
        w.move(s, aid, "dialing")
        watch.after_error(self.a, SYSTEM, ErrorReport("storage", None, "試験：書き込めない"),
                          tenant_id=s["tenant"], client_id=s["client"], attempt_id=aid)
        self.assertEqual([r for r in self.a.in_system(SYSTEM, lambda tx: stops.open_stops(
            tx, tenant_id=None, client_id=None, business_id=None)) if r["cause"] == "storage_failure"], [])
        nxt = self.a.in_client(SYSTEM, s["tenant"], s["client"], lambda tx: tx.one(
            "SELECT kind FROM salescall.task_next_actions WHERE task_id = %s ORDER BY seq DESC LIMIT 1", (s["task"],)))
        self.assertNotEqual(nxt and nxt["kind"], "hold")
        undecided = self.a.in_client(SYSTEM, s["tenant"], s["client"], lambda tx: tx.one(
            "SELECT salescall.refusal_undecided(%s) AS u", (aid,)))["u"]
        self.assertTrue(undecided)                                           # 締めが済むまで、同じお客様は守られる
        admin = self.a.in_system(ADMIN, lambda tx: tx.all(
            "SELECT summary FROM salescall.notification_events WHERE event_key LIKE %s",
            (f"admin:error:storage:{s['tenant']}:{s['client']}:%",)))
        self.assertEqual([a["summary"] for a in admin], ["CL-2 に修正依頼：保存に失敗しました"])   # 注意は短く
        got = [r for r in self.a.in_system(SYSTEM, lambda tx: fix_requests.open_requests(tx, "CL-2"))
               if r["refs"].get("tenant_id") == str(s["tenant"])]
        self.assertEqual([(r["kind"], r["summary"]) for r in got], [("storage", "保存に失敗しました")])

    def test_a_missing_recording_does_not_stop_the_line_and_asks_for_a_fix(self):
        """録音が残らない通話があっても、回線を止めない。修正依頼を出し、ほかの行は続ける（2026-10-17 ユーザー：「また止めて人に
        介在してもらってるじゃないか…止めずに CL1 か CL2 に修正依頼を出さないといけないだろ」）。"""
        w = World(); s = w.setup()
        for _ in range(5):
            watch.after_error(self.a, SYSTEM, ErrorReport("recording", None, "試験：録音のファイルが無い"),
                              tenant_id=s["tenant"], client_id=s["client"], line_id=s["line"])
        self.assertEqual(self.a.in_tenant(SYSTEM, s["tenant"], lambda tx: stops.open_stops(
            tx, tenant_id=s["tenant"], client_id=None, business_id=None, line_id=s["line"])), [])
        got = self.a.in_system(SYSTEM, lambda tx: fix_requests.open_requests(tx, "CL-2"))
        self.assertIn("recording", [r["kind"] for r in got])

    def test_a_meaning_judge_failure_never_stops_and_is_split_by_what_failed(self):
        """意味の判定が答えなかった（CL-1 の連絡帳 4-24）：止まっている（時間切れ・つながらない・上限・キーが止められた・混雑）と、
        問い合わせの中身が断られた（400 など）に分ける。どちらも発信は止めない（代わりの判定と通話後の検証がある）。分け方は表の1か所。"""
        kinds = {(code, unreachable): errors.classify(ErrorReport("judge", code, None, unreachable=unreachable))
                 for code, unreachable in ((None, True), (None, False), (500, False), (503, False), (401, False),
                                           (403, False), (408, False), (429, False), (400, False), (404, False), (422, False))}
        self.assertEqual({k for k, v in kinds.items() if v == "judge_rejected"}, {(400, False), (404, False), (422, False)})
        self.assertEqual({k for k, v in kinds.items() if v == "judge_key"}, {(401, False), (403, False)})
        self.assertEqual(errors.classify(ErrorReport("judge", 400, "API key not valid. Please pass a valid API key.")),
                         "judge_key")                                   # Google のキーの誤りは 400（会話AIと同じ決まり。見直し6）
        same = lambda a: (a.key, a.retry, a.stop_cause, a.stop_scope, a.notify)   # noqa: E731
        self.assertEqual(same(errors.ACTIONS["judge_key"]), same(errors.ACTIONS["key_specific"]))   # 会話AIのキーの誤りと同じ
        for kind in ("judge_outage", "judge_rejected"):
            a = errors.ACTIONS[kind]
            self.assertEqual((a.stop_cause, a.fundamental, a.lasting_fixer), (None, False, "CL-1"))
        s = World().setup()
        got = watch.after_error(self.a, SYSTEM, ErrorReport("judge", 503, "試験：混雑"), tenant_id=s["tenant"],
                                client_id=s["client"])
        self.assertEqual(got, "judge_outage")
        self.assertEqual(self.a.in_tenant(SYSTEM, s["tenant"], lambda tx: stops.open_stops(
            tx, tenant_id=s["tenant"], client_id=s["client"], business_id=None)), [])
        self.assertEqual(owner_access().in_system(SYSTEM, lambda tx: settings.initial_value(tx, "judge.outage_bypass")), 60)

    def test_a_lasting_temporary_error_becomes_a_fix_request(self):
        """一時的なもの（別の耳につながらない）でも、1時間以上続いたら担当（CL-1）へ修正依頼を出す。続いていなければ出さない
        （2026-10-18 の見直し：前は何日続いても修正依頼にならず、その間お客様の行が待ち続けた）。"""
        ear = ErrorReport("ear", None, None, unreachable=True)
        def asked(t=None):
            return [r for r in self.a.in_system(SYSTEM, lambda tx: fix_requests.open_requests(tx, "CL-1"))
                    if r["kind"] == "ear_unreachable"]
        for r in asked():   # 同じ不具合は契約者をまたいで1つ（2026-10-22 の見直し）：ほかの試験が出した依頼を、直したことにしておく
            self.a.in_system(SYSTEM, lambda tx, r=r: fix_requests.report_fixed(tx, r["request_id"], "試験：前の依頼を閉じる"))
        short = World().setup()
        watch.after_error(self.a, SYSTEM, ear, tenant_id=short["tenant"], client_id=short["client"])
        self.assertEqual(asked(short["tenant"]), [])
        long = World().setup()
        for minutes in (110, 70, 30):                                   # 30〜40分おきに、2時間近く続いている
            self.a.in_client(SYSTEM, long["tenant"], long["client"], lambda tx: tx.execute(
                "INSERT INTO salescall.error_events (tenant_id, client_id, source, kind, occurred_at)"
                " VALUES (salescall.cur_tenant(), salescall.cur_client(), 'ear', 'ear_unreachable', now() - %s)",
                (timedelta(minutes=minutes),)))
        watch.after_error(self.a, SYSTEM, ear, tenant_id=long["tenant"], client_id=long["client"])
        self.assertEqual(len(asked(long["tenant"])), 1)
        for r in asked():
            self.a.in_system(SYSTEM, lambda tx, r=r: fix_requests.report_fixed(tx, r["request_id"], "試験：直した"))
        apart = World().setup()
        for minutes in (180, 10):                                       # 3時間前と10分前：間が1時間より空いたら数え直す
            self.a.in_client(SYSTEM, apart["tenant"], apart["client"], lambda tx: tx.execute(
                "INSERT INTO salescall.error_events (tenant_id, client_id, source, kind, occurred_at)"
                " VALUES (salescall.cur_tenant(), salescall.cur_client(), 'ear', 'ear_unreachable', now() - %s)",
                (timedelta(minutes=minutes),)))
        watch.after_error(self.a, SYSTEM, ear, tenant_id=apart["tenant"], client_id=apart["client"])
        self.assertEqual(asked(apart["tenant"]), [])

    def test_a_stop_that_lasts_becomes_a_fix_request_even_without_checks(self):
        """一時的なもので止めたまま1時間以上たったら、担当（会話AI＝CL-1）へ修正依頼を出す（Intent.md 10-3。同じ不具合は1つ）。解く確かめ
        の部品が無い PC でも出す（⚠️ 見直し8では確かめの中で出していたので、確かめが動かないと出なかった＝見直し9）。止めたばかりには
        出さない。見張りの長さを短くして確かめる（試験で1時間待たない）。"""
        override(self, "stop.ai.stop_after_failures", 2)
        for _ in range(2):
            self.ai_error(ErrorReport("ai", 503, "UNAVAILABLE: The model is overloaded."))
        sid = self.ai_stops()[0]["event_id"]
        lasting = lambda: self.a.in_tenant(SYSTEM, self.s["tenant"], watch.lasting_stops)   # noqa: E731
        asked = lambda: [r for r in self.a.in_system(SYSTEM, lambda tx: fix_requests.open_requests(tx, "CL-1"))   # noqa: E731
                         if r["kind"] == "ai_server_error" and r["refs"].get("stop_event_id") == sid]
        self.assertEqual((lasting(), asked()), ([], []))                 # 止めたばかり
        with mock.patch.object(errors, "LASTING", timedelta(0)):
            self.assertEqual(lasting(), [sid])                          # 確かめを1度もしていなくても出す
            self.assertEqual(len(asked()), 1)                           # 何度見ても1つ（同じ不具合は直すまで1つ）

    def test_a_lasting_error_is_counted_across_the_clients_of_the_contractor(self):
        """「1時間以上続いた」は、契約者のすべての依頼者のエラーで数える（修正依頼の重なりを防ぐ鍵と同じ範囲。⚠️ 前は記録した依頼者の
        取引のまま数え、ほかの依頼者のエラーが見えず、契約者全体で続いていても修正依頼が出なかった＝見直し6）。"""
        w = World()
        a = w.setup()
        b = w.setup(tenant=a["tenant"])                                 # 同じ契約者のほかの依頼者
        for minutes in (110, 70):                                       # 依頼者A で、2時間近く前から
            self.a.in_client(SYSTEM, a["tenant"], a["client"], lambda tx: tx.execute(
                "INSERT INTO salescall.error_events (tenant_id, client_id, source, kind, occurred_at)"
                " VALUES (salescall.cur_tenant(), salescall.cur_client(), 'ear', 'ear_unreachable', now() - %s)",
                (timedelta(minutes=minutes),)))
        self.a.in_client(SYSTEM, b["tenant"], b["client"], lambda tx: tx.execute(
            "INSERT INTO salescall.error_events (tenant_id, client_id, source, kind, occurred_at)"
            " VALUES (salescall.cur_tenant(), salescall.cur_client(), 'ear', 'ear_unreachable', now() - interval '30 minutes')"))
        watch.after_error(self.a, SYSTEM, ErrorReport("ear", None, None, unreachable=True), tenant_id=b["tenant"],
                          client_id=b["client"])                        # 依頼者B の取引で記録する
        asked = [r for r in self.a.in_system(SYSTEM, lambda tx: fix_requests.open_requests(tx, "CL-1"))
                 if r["kind"] == "ear_unreachable" and r["refs"].get("tenant_id") == str(a["tenant"])]
        self.assertEqual(len(asked), 1)

    def test_the_assignee_reports_the_fix_and_only_the_admin_approves(self):
        """担当が「直した」を書き、管理者が確かめて承認する（人は承認だけ）。契約者には修正依頼は見えない。"""
        w = World(); s = w.setup()
        watch.after_error(self.a, SYSTEM, ErrorReport("storage", None, f"試験：書き込めない（{s['tenant']}）"),
                          tenant_id=s["tenant"], client_id=s["client"])   # 不具合ごとに1つ（試験ごとに別の不具合にする）
        rid = [r for r in self.a.in_system(SYSTEM, lambda tx: fix_requests.open_requests(tx, "CL-2"))
               if r["refs"].get("tenant_id") == str(s["tenant"])][0]["request_id"]
        self.assertEqual(self.a.in_tenant(OPERATOR, s["tenant"], lambda tx: tx.all(
            "SELECT 1 FROM salescall.fix_requests")), [])
        with self.assertRaises(Rejected):                                       # 承認を先に書けない・システムは承認できない
            self.a.in_system(SYSTEM, lambda tx: fix_requests.approve(tx, rid))
        with self.assertRaises(Rejected):                                       # 「直した」は人が書かない
            self.a.in_system(ADMIN, lambda tx: fix_requests.report_fixed(tx, rid, "試験"))
        self.a.in_system(SYSTEM, lambda tx: fix_requests.report_fixed(tx, rid, "試験：書き込みの誤りを直した"))
        with self.assertRaises(Rejected):
            self.a.in_system(SYSTEM, lambda tx: fix_requests.approve(tx, rid))
        self.a.in_system(ADMIN, lambda tx: fix_requests.approve(tx, rid))
        self.assertNotIn(rid, [r["request_id"] for r in self.a.in_system(SYSTEM, lambda tx: fix_requests.open_requests(
            tx, "CL-2"))])

    def test_notify_once_an_hour(self):
        for _ in range(2):
            watch.after_error(self.a, SYSTEM, ErrorReport("ai", 1008, "Your project has been denied access"))
        rows = self.a.in_system(SYSTEM, lambda tx: tx.all(
            "SELECT event_key FROM salescall.notification_events WHERE event_key LIKE 'error:project_denied:None:None:%'"
            " AND occurred_at > now() - interval '1 minute'"))
        self.assertEqual(len(rows), 1)


if __name__ == "__main__":
    unittest.main()
