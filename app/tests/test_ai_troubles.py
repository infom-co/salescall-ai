"""AIの不具合での一時停止と、契約者以上の緊急の道での再開の試験（2026-10-30 深夜 管理者のご決定。0155。CL-1 の連絡帳 4-62）。

ご決定：「基本的にアンソロピックが使えなくなったりその他に使う AI のどれかが止まったら、AI の不具合として回復するまで一時停止して
ください。ただし、どうしても使わなければいけないシーンが出たときは、契約者以上の権限でOpenRouter を通して Google が預かる Claudeを
使って再開することができるようにしておいてください。」
"""

from __future__ import annotations

import time
import unittest
from datetime import timedelta

from salescall.core import ai_troubles, call_report, conversation_io, errors, stops, watch
from salescall.core.access import Rejected
from salescall.core.call_report import AiTrouble, ReportInvalid
from salescall.core.errors import ErrorReport
from tests.support import ADMIN, OPERATOR, SYSTEM, World, app_access, make_report, owner_access
from tests.test_watch import override

JEV = "Jev 1.14"
SONNET = "Claude Sonnet 5.5（考える）"
EMERGENCY = "緊急の道（OpenRouter を通して Google が預かる Claude）"


def trouble(ai=JEV, route=errors.DIRECT, what="止まった", error="時間切れ", turn=3, count=1) -> AiTrouble:
    return AiTrouble(ai, route, what, error, turn, count)


class Shape(unittest.TestCase):
    """止まったAIの形（データの形 版 1.08・1.09＝おかしな動き）と、種類の分け方（処置の表の1か所）。"""

    def test_three_kinds_by_ai_and_route(self):
        c = lambda ai, route: errors.classify(ErrorReport("ai_trouble", None, "x", ai=ai, route=route))   # noqa: E731
        self.assertEqual(c(JEV, errors.DIRECT), "ai_stopped")
        self.assertEqual(c("別の耳（soniox）", errors.DIRECT), "ear_stopped")   # 別の耳は、緊急の道で補えない（2026-10-31 管理者）
        self.assertEqual(c(errors.CONVO_AI, errors.DIRECT), "convo_stopped")
        self.assertEqual(c(SONNET, EMERGENCY), "emergency_stopped")
        # 使用額の上限の文でも、止まったAI（回復するまで一時停止。上限の知らせより先に分ける）
        self.assertEqual(errors.classify(ErrorReport("ai_trouble", None, "You have reached your specified API usage limits",
                                                     ai=SONNET, route=errors.DIRECT)), "ai_stopped")
        with self.assertRaises(ValueError):
            errors.classify(ErrorReport("ai_trouble", None, "x"))
        self.assertEqual(errors.classify(ErrorReport("ai_glitch", None, "x", ai=JEV, route=errors.DIRECT)), "ai_glitch")
        for kind in ("ai_stopped", "ear_stopped", "convo_stopped", "emergency_stopped"):
            a = errors.ACTIONS[kind]
            with self.subTest(kind):
                self.assertEqual((a.stop_cause, a.stop_scope, a.stop_after, a.lasting_fixer, errors.EVIDENCE[kind]),
                                 ("ai_connection", "tenant", None, "CL-1", "none"))
        # 緊急の道で補えるのは、意味の判定が止まったときだけ（会話AI・別の耳・緊急の道そのもの・会話AIの異常は補えない。2026-10-31
        # 管理者：別の耳は「『再開できない』でいい」）。補えない種類には、どれも画面に出す理由がある
        self.assertEqual({k for k, a in errors.ACTIONS.items() if a.emergency}, {"ai_stopped"})
        self.assertEqual(set(errors.EMERGENCY_BLOCKERS),
                         set(errors.COUNTED_FOR["ai_connection"]) - {"ai_stopped"})
        self.assertEqual(set(errors.EMERGENCY_CANNOT), set(errors.EMERGENCY_BLOCKERS))
        # おかしな動き（甚大でない）は止めない。契約者に知らせる（ダッシュボードの警告）
        g = errors.ACTIONS["ai_glitch"]
        self.assertEqual((g.stop_cause, g.notify, g.lasting_fixer), (None, True, "CL-1"))

    def test_report_round_trip_and_old_reports(self):
        r = make_report()
        raw = r.to_json()
        self.assertEqual(raw["ai_trouble"], [])
        del raw["ai_trouble"]                                          # 古い報告（無い）は空
        self.assertEqual(call_report.from_json(raw).ai_troubles, ())
        raw["ai_trouble"] = None
        self.assertEqual(call_report.from_json(raw).ai_troubles, ())
        raw["ai_trouble"] = [trouble().to_json(), trouble(SONNET, EMERGENCY, "緊急の道で答えた", None, None, 2).to_json()]
        got = call_report.from_json(raw)
        self.assertEqual(got.ai_troubles, (trouble(), trouble(SONNET, EMERGENCY, "緊急の道で答えた", None, None, 2)))
        self.assertEqual(call_report.from_json(got.to_json()), got)

    def test_bad_shapes_are_refused(self):
        raw = make_report().to_json()
        good = trouble().to_json()
        bad = [dict(good, what="遅かった"), dict(good, ai=""), dict(good, route=None), dict(good, error="x" * 301),
               dict(good, turn=-1), dict(good, count=0), dict(good, count=True), dict(good, extra=1)]
        for b in bad:
            with self.subTest(b):
                with self.assertRaises(ReportInvalid):
                    call_report.from_json(dict(raw, ai_trouble=[b]))
        with self.assertRaises(ReportInvalid):                         # 同じAI・同じ道・同じ出来事は1つ（会話の側がまとめる）
            call_report.from_json(dict(raw, ai_trouble=[good, good]))
        with self.assertRaises(ReportInvalid):
            call_report.from_json(dict(raw, ai_trouble={"ai": JEV}))
        with self.assertRaises(ReportInvalid):                         # 通話の途中の知らせも、同じ1つの確かめ
            conversation_io.ai_trouble_work(None, dict(good, what="遅かった"))


class SameNames(unittest.TestCase):
    """Python の一覧と、データベースの決まり（CHECK）が同じ名前か（片方だけ変えると、書くときに初めて落ちる。見直し25b の 9）と、緊急の道の
    判断の1か所（errors.is_emergency_route）を、判定の料金の名前も読むこと（見直し25b の 7）。"""

    def checks(self, table, column):
        import re
        rows = owner_access().in_system(SYSTEM, lambda tx: tx.all(
            "SELECT pg_get_constraintdef(c.oid) AS d FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid"
            " JOIN pg_namespace n ON n.oid = t.relnamespace WHERE n.nspname = 'salescall' AND t.relname = %s AND c.contype = 'c'",
            (table,)))
        got = [set(re.findall(r"'([^']*)'::text", r["d"])) for r in rows if re.search(rf"\b{column}\b", r["d"])
               and "ANY" in r["d"]]
        self.assertEqual(len(got), 1, (table, column, rows))
        return got[0]

    def test_python_lists_match_the_database(self):
        self.assertEqual(self.checks("attempt_ai_troubles", "what"), set(call_report.AI_TROUBLE_WHATS))
        self.assertEqual(self.checks("attempt_ai_troubles", "stage"), set(ai_troubles.STAGES))
        self.assertEqual(self.checks("error_events", "source"), set(errors.SOURCES))

    def test_the_judge_price_name_follows_the_route(self):
        from salescall.core import usage
        self.assertEqual(usage.judge_service(JEV), usage.JUDGE_SERVICES[JEV])
        self.assertEqual(usage.judge_service(SONNET, errors.DIRECT), usage.JUDGE_SERVICES[SONNET])
        self.assertEqual(usage.judge_service(SONNET, EMERGENCY), usage.JUDGE_EMERGENCY_SERVICE)
        with self.assertRaises(KeyError):
            usage.judge_service("知らない判定")


class _Base(unittest.TestCase):
    """止まったAIの試験の道具（架空の契約者ひとそろい・止まったAIを書く・止まっているかを読む）。"""

    def setUp(self):
        self.a = app_access()
        self.w = World()
        self.s = self.w.setup()

    def attempt(self, s=None):
        s = s or self.s
        aid = self.w.insert_claim(s)
        for state in ("dialing", "ringing", "answered", "in_conversation"):
            self.w.move(s, aid, state)
        return aid

    def note(self, aid, *troubles, s=None):
        s = s or self.s
        kinds = self.a.in_client(SYSTEM, s["tenant"], s["client"], lambda tx: ai_troubles.note(tx, aid, troubles))
        return kinds, watch.after_ai_troubles(self.a, SYSTEM, s["tenant"])

    def ai_stops(self, s=None):
        s = s or self.s
        return [r["event_id"] for r in self.a.in_tenant(SYSTEM, s["tenant"], lambda tx: stops.open_stops(
            tx, tenant_id=s["tenant"], client_id=None, business_id=None)) if r["cause"] == "ai_connection"]

    def blocking(self, s=None):
        s = s or self.s
        return self.a.in_client(SYSTEM, s["tenant"], s["client"], lambda tx: stops.blocking(
            tx, tenant_id=s["tenant"], client_id=s["client"], business_id=s["business"], kind=None))

    def serving(self, s=None):
        """約束の電話を受け持てる回線（salescall.lines_serving。回線を借りた形にして読む）。"""
        s = s or self.s
        self.a.in_tenant(SYSTEM, s["tenant"], lambda tx: tx.one("SELECT salescall.lease_line(%s, '試験', 60)", (s["line"],)))
        return [r["line_id"] for r in owner_access().in_tenant(SYSTEM, s["tenant"], lambda tx: tx.all(
            "SELECT line_id FROM salescall.lines_serving()"))]

    def judge_route(self, aid, s=None):
        s = s or self.s
        return self.a.in_client(SYSTEM, s["tenant"], s["client"], lambda tx: tx.one(
            "SELECT judge_route FROM salescall.attempts WHERE attempt_id = %s", (aid,))["judge_route"])



class Pause(_Base):
    """止まったAIで、その契約者の新しい電話を止める・回復の確かめで解く・緊急の道で再開する。"""

    def test_one_stopped_ai_stops_the_contractor_at_once_and_only_once(self):
        other = World().setup()
        aid = self.attempt()
        self.assertIn(self.s["line"], self.serving())
        kinds, stopped = self.note(aid, trouble())
        self.assertEqual(kinds, ["ai_stopped"])
        self.assertEqual(len(stopped), 1)
        self.assertNotIn(self.s["line"], self.serving())               # 止まっているあいだは、約束の電話も受け持たない
        self.assertEqual(len(self.ai_stops()), 1)
        self.assertEqual(self.ai_stops(other), [])                     # ほかの契約者は止めない（キーと枠は契約者ごと）
        self.assertTrue(self.blocking())                               # 新しい電話はかけない
        # 同じAI・同じ道・同じ出来事をもう一度（報告の ai_trouble にも入っていた）：重ねて数えない・重ねて止めない
        kinds, stopped = self.note(aid, trouble(count=4))
        self.assertEqual((kinds, stopped), ([], []))
        n = self.a.in_tenant(SYSTEM, self.s["tenant"], lambda tx: tx.one(
            "SELECT count(*) AS n FROM salescall.error_events WHERE attempt_id = %s", (aid,))["n"])
        self.assertEqual(n, 1)
        # 「緊急の道で答えた」は書くだけ（止める材料にしない）
        kinds, _ = self.note(aid, trouble(SONNET, EMERGENCY, "緊急の道で答えた", None))
        self.assertEqual(kinds, [])
        got = self.a.in_tenant(SYSTEM, self.s["tenant"], lambda tx: stops.explain(tx, self.ai_stops()[0]))
        self.assertEqual({(t["ai"], t["what"]) for t in got["ai_troubles"]}, {(JEV, "止まった"), (SONNET, "緊急の道で答えた")})
        self.assertEqual(got["cause"]["label"], "AIの不具合")

    def test_another_call_reaching_the_customer_does_not_hide_the_stopped_ai(self):
        # 止まったAIは1回で止める。どこかの通話が会話に入ったことでは数え直さない（数え直すと、同じ時刻にほかの通話が会話に入っただけで
        # 止まらなくなる＝続いたエラーの証拠 none）
        aid = self.attempt(_with_task(self.w, self.s))   # 話し終えた電話（talked）は、もとの回線でかける
        self.a.in_client(SYSTEM, self.s["tenant"], self.s["client"], lambda tx: ai_troubles.note(tx, aid, (trouble(),)))
        self.w.talked(self.s)
        self.assertEqual(len(watch.after_ai_troubles(self.a, SYSTEM, self.s["tenant"])), 1)

    def test_a_report_with_ai_trouble_stops_through_the_same_door(self):
        aid = self.attempt()
        raw = dict(make_report().to_json(), ai_trouble=[trouble(errors.CONVO_AI, error="張り替えても声が出ない").to_json()])
        self.a.in_client(SYSTEM, self.s["tenant"], self.s["client"], lambda tx: conversation_io.save_report(tx, aid, raw))
        stopped = watch.after_ai_troubles(self.a, SYSTEM, self.s["tenant"])
        self.assertEqual(len(stopped), 1)
        self.assertEqual(watch.after_ai_troubles(self.a, SYSTEM, self.s["tenant"]), [])   # もう止まっている

    def test_the_contractor_can_reopen_on_the_emergency_route_while_a_judge_is_down(self):
        aid = self.attempt()
        self.note(aid, trouble(SONNET, error="529 overloaded"))
        sid = self.ai_stops()[0]
        self.assertIsNone(self.a.in_tenant(OPERATOR, self.s["tenant"], lambda tx: stops.emergency_possible(tx, sid)))
        with self.assertRaises(Rejected):                              # システムの処理は出せない（契約者か管理者だけ）
            self.a.in_tenant(SYSTEM, self.s["tenant"], lambda tx: stops.emergency_pass(tx, sid, reason="試験"))
        got = self.a.in_tenant(OPERATOR, self.s["tenant"], lambda tx: stops.emergency_pass(
            tx, sid, reason="試験：今日中に約束の電話を済ませたい"))
        self.assertEqual(got["actor_kind"], "operator")
        self.assertEqual(self.blocking(), [])                          # その停止では止めない
        self.assertEqual(self.ai_stops(), [sid])                       # 停止そのものは解けていない（回復の確かめで解く）
        self.assertIn(self.s["line"], self.serving())                  # 約束の電話を受け持てる回線にも数える（門と同じ1か所）
        claimed = self.w.insert_claim(_with_task(self.w, self.s))
        self.assertEqual(self.judge_route(claimed), "emergency")       # 判定を初めから緊急の道へ（データベースが決める）
        with self.assertRaises(Rejected):                              # 判定の道は変えられない
            self.a.in_client(SYSTEM, self.s["tenant"], self.s["client"], lambda tx: tx.execute(
                "UPDATE salescall.attempts SET judge_route = NULL WHERE attempt_id = %s", (claimed,)))
        with self.assertRaises(stops.EmergencyNotAllowed):             # 2回は出さない
            self.a.in_tenant(OPERATOR, self.s["tenant"], lambda tx: stops.emergency_pass(tx, sid, reason="試験"))
        # 緊急の道で補える種類（判定）がまた止まっても、止め直さない
        _, stopped = self.note(self.attempt(_with_task(self.w, self.s)), trouble(JEV))
        self.assertEqual(stopped, [])
        self.assertEqual(self.blocking(), [])
        # 補えない種類（緊急の道そのもの）が止まったら、また止める（新しい停止は、緊急の道では再開できない）
        _, stopped = self.note(self.attempt(_with_task(self.w, self.s)), trouble(SONNET, EMERGENCY))
        self.assertEqual(len(stopped), 1)
        self.assertTrue(self.blocking())
        new = stopped[0]["event_id"]
        self.assertIsNotNone(self.a.in_tenant(OPERATOR, self.s["tenant"], lambda tx: stops.emergency_possible(tx, new)))
        # 契約者が両方を解いたら、ふつうの道に戻る
        for e in self.ai_stops():
            self.a.in_tenant(OPERATOR, self.s["tenant"], lambda tx, e=e: stops.resume(tx, e, reason="試験：回復を確かめた"))
        self.assertEqual(self.blocking(), [])
        later = self.w.insert_claim(_with_task(self.w, self.s))
        self.assertIsNone(self.judge_route(later))

    def test_no_emergency_route_when_the_conversation_ai_itself_is_down(self):
        aid = self.attempt()
        self.note(aid, trouble(errors.CONVO_AI))
        sid = self.ai_stops()[0]
        why = self.a.in_tenant(OPERATOR, self.s["tenant"], lambda tx: stops.emergency_possible(tx, sid))
        self.assertIn("会話AI", why)
        with self.assertRaises(stops.EmergencyNotAllowed):
            self.a.in_tenant(ADMIN, self.s["tenant"], lambda tx: stops.emergency_pass(tx, sid, reason="試験"))
        got = self.a.in_tenant(OPERATOR, self.s["tenant"], lambda tx: stops.explain(tx, sid))
        self.assertEqual(got["emergency"]["impossible_because"], why)
        # 会話AIの異常のエラー（つながらない 3回など）が先にあっても同じ
        other = World().setup()
        override(self, "stop.ai.stop_after_failures", 1)
        watch.after_error(self.a, SYSTEM, ErrorReport("ai", None, None, unreachable=True), tenant_id=other["tenant"],
                          client_id=other["client"])
        sid2 = self.ai_stops(other)[0]
        self.assertIsNotNone(self.a.in_tenant(OPERATOR, other["tenant"], lambda tx: stops.emergency_possible(tx, sid2)))

    def test_only_the_system_writes_stopped_ais_and_never_rewrites_them(self):
        aid = self.attempt()
        with self.assertRaises(Rejected):
            self.a.in_client(OPERATOR, self.s["tenant"], self.s["client"], lambda tx: ai_troubles.note(tx, aid, (trouble(),)))
        other = World().setup()
        with self.assertRaises(Rejected):                              # ほかの依頼者の通話には書けない（表の持ち主でも＝引き金が守る）
            owner_access().in_client(SYSTEM, other["tenant"], other["client"], lambda tx: tx.execute(
                "INSERT INTO salescall.attempt_ai_troubles (tenant_id, client_id, attempt_id, ai, route, what)"
                " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, 'Jev 1.14', '直接', '止まった')", (aid,)))
        self.note(aid, trouble())
        for sql in ("UPDATE salescall.attempt_ai_troubles SET error = 'x' WHERE attempt_id = %s",
                    "DELETE FROM salescall.attempt_ai_troubles WHERE attempt_id = %s"):
            with self.subTest(sql[:6]), self.assertRaises(Rejected):   # 表の持ち主でも（権限ではなく、引き金が守る）
                owner_access().in_tenant(SYSTEM, self.s["tenant"], lambda tx, sql=sql: tx.execute(sql, (aid,)))

    def test_the_call_door_stops_right_away_without_waiting_for_the_end_of_the_call(self):
        """通話ごとの口の call.ai_trouble（通話の途中）：書き手が書いたすぐあと、通話の終わりを待たずに止める。"""
        from salescall.supervisor.line import CallLimits, _Call
        aid = self.attempt()
        call = _Call(self.a, SYSTEM, self.s["tenant"], self.s["client"], self.s["line"], aid, None, CallLimits(3.0))
        call.ai_trouble(trouble().to_json())
        call.flush()
        self.assertEqual(len(self.ai_stops()), 1)
        with self.assertRaises(ReportInvalid):                         # 形が違えば、その場で
            call.ai_trouble({"ai": JEV})


class VerificationEar(_Base):
    """通話後の確かめ（別の耳）が止まったとき（2026-10-31 管理者のご決定：「通話中は止めません。ただし、次の電話はかけず、確かめが通れば
    自動で再開します。」。0158）：止まったかは会話の側が決め、通話中と同じ入口・同じ判断で次の電話を止める。"""

    EAR = "別の耳（soniox）"

    def ended(self, s=None):
        s = s or self.s
        aid = self.attempt(s)
        for state in ("ending", "ended"):
            self.w.move(s, aid, state)
        return aid

    def verify_fail(self, aid, s=None):
        s = s or self.s
        return conversation_io.verification_ai_trouble(self.a, SYSTEM, s["tenant"], s["client"], aid,
                                                        trouble(self.EAR, error="503 混雑").to_json())

    def test_a_stopped_ear_after_the_call_stops_the_next_calls_but_not_the_talking_one(self):
        talking = self.attempt(_with_task(self.w, self.s))              # ほかの回線で話している電話
        done = self.ended()
        stopped = self.verify_fail(done)
        self.assertEqual(len(stopped), 1)
        self.assertTrue(self.blocking())                               # 次の電話はかけない
        state = self.a.in_client(SYSTEM, self.s["tenant"], self.s["client"], lambda tx: tx.one(
            "SELECT state::text AS s FROM salescall.attempts WHERE attempt_id = %s", (talking,))["s"])
        self.assertEqual(state, "in_conversation")                      # 話している電話は切らない
        got = self.a.in_tenant(SYSTEM, self.s["tenant"], lambda tx: stops.explain(tx, stopped[0]["event_id"]))
        self.assertEqual([(t["stage"], t["ai"]) for t in got["ai_troubles"]], [("verification", self.EAR)])
        # 別の耳は緊急の道で補えない：再開できず、画面に理由を出す（2026-10-31 管理者：「『再開できない』でいいが、ダッシュボードに
        # なぜ再開できないのかの警告を出すようにして下さい」）
        self.assertEqual(got["emergency"]["impossible_because"], errors.EMERGENCY_CANNOT["ear_stopped"])
        self.assertIn("別の耳の代わりは無い", got["emergency"]["impossible_because"])
        with self.assertRaises(stops.EmergencyNotAllowed):
            self.a.in_tenant(OPERATOR, self.s["tenant"], lambda tx: stops.emergency_pass(tx, stopped[0]["event_id"], reason="試験"))
        self.assertEqual(self.verify_fail(done), [])                    # もう止まっている（重ねて止めない）

    def test_after_resuming_the_same_calls_check_stopping_again_stops_again(self):
        # 通話後の確かめは、確かめのたびに別の出来事（重ねると、再開したあとに同じ通話の確かめがまた止まっても止め直せない）
        done = self.ended()
        first = self.verify_fail(done)[0]["event_id"]
        self.a.in_tenant(OPERATOR, self.s["tenant"], lambda tx: stops.resume(tx, first, reason="試験：回復を確かめた"))
        self.assertEqual(self.blocking(), [])
        again = self.verify_fail(done)
        self.assertEqual(len(again), 1)
        self.assertTrue(self.blocking())

    def test_an_ear_stopping_after_reopening_on_the_emergency_route_stops_again(self):
        # 判定の止まりで緊急の道で再開していても、別の耳が止まれば、また止める（別の耳は補えない）
        self.note(self.attempt(), trouble(JEV))
        sid = self.ai_stops()[0]
        self.a.in_tenant(OPERATOR, self.s["tenant"], lambda tx: stops.emergency_pass(tx, sid, reason="試験：どうしても今日"))
        self.assertEqual(self.blocking(), [])
        self.assertEqual(len(self.verify_fail(self.ended(_with_task(self.w, self.s)))), 1)
        self.assertTrue(self.blocking())

    def test_only_ended_calls_take_a_check_trouble(self):
        talking = self.attempt()
        with self.assertRaises(Rejected):
            self.verify_fail(talking)
        with self.assertRaises(ReportInvalid):
            conversation_io.verification_ai_trouble(self.a, SYSTEM, self.s["tenant"], self.s["client"], self.ended(_with_task(
                self.w, self.s)), {"ai": self.EAR})
        with self.assertRaises(ValueError):
            self.a.in_client(SYSTEM, self.s["tenant"], self.s["client"], lambda tx: ai_troubles.note(
                tx, talking, (trouble(),), stage="あとで"))


class Review25(_Base):
    """見直し25（0159）：止める・緊急の道を出せるか・解くの数え方を1か所に（解いたあとの古い出来事・止める数に届かないエラー・解くのと
    同時に書いた止まったAI・緊急の道を出せる停止）。"""

    def check(self, sid, ok, begun=None, s=None):
        s = s or self.s
        begun = begun or self.a.in_tenant(SYSTEM, s["tenant"], watch.check_begins)
        return self.a.in_tenant(SYSTEM, s["tenant"], lambda tx: watch.record_check(tx, sid, ok, evidence="試験：確かめ", begun_at=begun))

    def possible(self, sid, s=None):
        s = s or self.s
        return self.a.in_tenant(OPERATOR, s["tenant"], lambda tx: stops.emergency_possible(tx, sid))

    def test_old_events_of_the_same_call_do_not_stop_again_after_reopening(self):
        # ⛔ 見直し25a の A：前は、その通話で前に記録した種類（解けた会話AIの止まり）を読み直し、再開のもとになった判定の止まりと合わせて
        # 数えたので、緊急の道で再開したあとに「会話AIが止まった」として止め直した
        override(self, "stop.ai.resume_after_successes", 1)
        aid = self.attempt()
        self.note(aid, trouble(errors.CONVO_AI))
        first = self.ai_stops()[0]
        self.assertIsNotNone(self.check(first, True)["resumed"])               # 回復の確かめで解ける
        self.note(aid, trouble(JEV))                                           # 同じ通話で、こんどは判定が止まった
        sid = self.ai_stops()[0]
        self.assertIsNone(self.possible(sid))                                  # 解けた会話AIの止まりでは断らない
        self.a.in_tenant(OPERATOR, self.s["tenant"], lambda tx: stops.emergency_pass(tx, sid, reason="試験：どうしても今日"))
        self.assertEqual(watch.after_ai_troubles(self.a, SYSTEM, self.s["tenant"]), [])   # 古い出来事で止め直さない
        self.assertEqual(self.blocking(), [])
        got = self.a.in_tenant(SYSTEM, self.s["tenant"], lambda tx: stops.explain(tx, first))
        self.assertEqual([t["ai"] for t in got["ai_troubles"]], [errors.CONVO_AI])   # 解けた停止の説明に、あとの止まりは出ない
        _, stopped = self.note(self.attempt(_with_task(self.w, self.s)), trouble(errors.CONVO_AI))
        self.assertEqual(len(stopped), 1)                                      # 新しく会話AIが止まれば、また止める
        self.assertTrue(self.blocking())

    def test_a_conversation_ai_error_below_the_stop_count_does_not_block_the_emergency_route(self):
        # ⛔ 見直し25a の B：前は、緊急の道を出せるかだけが「前の解き以後に1回でも」で決めた（止める基準に届かない一時的なエラー1回で、
        # ずっと出せなかった）。いまは止め直すかと同じ数え方（watch.ai_blocker）
        override(self, "stop.ai.stop_after_failures", 3)
        unreachable = lambda: watch.after_error(self.a, SYSTEM, ErrorReport("ai", None, None, unreachable=True),   # noqa: E731
                                                tenant_id=self.s["tenant"], client_id=self.s["client"])
        other = World().setup()                                                # 1回では止めない（どの種類で決め直しても）
        watch.after_error(self.a, SYSTEM, ErrorReport("ai", None, None, unreachable=True), tenant_id=other["tenant"],
                          client_id=other["client"])
        self.assertEqual(watch.after_ai_troubles(self.a, SYSTEM, other["tenant"]), [])
        self.note(self.attempt(), trouble(JEV))                                # 判定が止まった（会話に入った通話のあと）
        sid = self.ai_stops()[0]
        unreachable()                                                          # 会話AIにつながらない1回（基準に届かない）
        self.assertIsNone(self.possible(sid))
        unreachable(), unreachable()                                          # 基準（3回）に届いた
        self.assertIn("会話AI", self.possible(sid))

    def test_a_stopped_ai_written_while_the_check_runs_is_not_lost(self):
        # ⛔ 見直し25a の C：前は、解いた時刻から数え直したので、解くのと同時に書いた止まったAIが数えられずに消えた
        override(self, "stop.ai.resume_after_successes", 1)
        self.note(self.attempt(), trouble(JEV))
        sid = self.ai_stops()[0]
        begun = self.a.in_tenant(SYSTEM, self.s["tenant"], watch.check_begins)
        self.note(self.attempt(_with_task(self.w, self.s)), trouble(SONNET))   # 確かめの間に、また止まった（見える）
        got = self.check(sid, True, begun)
        self.assertEqual((got["ok"], got["resumed"]), (False, None))             # 解かない
        # 見えないうちに書いた分（確かめを始めたあとに起き、解いた取引のあとで書けた）：解いたあとで数えて止め直す
        other = World().setup()
        override(self, "stop.ai.resume_after_successes", 1)
        self.note(self.attempt(other), trouble(JEV), s=other)
        sid2 = self.ai_stops(other)[0]
        begun = self.a.in_tenant(SYSTEM, other["tenant"], watch.check_begins)
        time.sleep(0.05)                                                       # 確かめを始めてから解くまでの間に起きた（解いた時刻より前）
        self.assertIsNotNone(self.check(sid2, True, begun, s=other)["resumed"])
        owner_access().in_client(SYSTEM, other["tenant"], other["client"], lambda tx: tx.execute(
            "INSERT INTO salescall.error_events (tenant_id, client_id, source, kind, message, occurred_at)"
            " VALUES (salescall.cur_tenant(), salescall.cur_client(), 'ai_trouble', 'ai_stopped', '試験：同時に書いた', %s)",
            (begun + timedelta(milliseconds=1),)))
        self.assertEqual(len(watch.after_ai_troubles(self.a, SYSTEM, other["tenant"])), 1)
        # 確かめを始める前の止まりは、通った確かめが「回復した」と示している（数えない）
        self.assertEqual(watch.after_ai_troubles(self.a, SYSTEM, self.s["tenant"]), [])

    def test_the_supervisor_decides_again_when_the_first_decision_was_missed(self):
        # 書いたすぐあとの判断が落ちても、見守り役の仕事 ai_stops が決め直す（見直し25b の (4)：通話後の確かめの道には無かった）
        from types import SimpleNamespace
        from salescall.supervisor import jobs
        aid = self.attempt()
        self.a.in_client(SYSTEM, self.s["tenant"], self.s["client"], lambda tx: ai_troubles.note(tx, aid, (trouble(),)))
        self.assertEqual(self.ai_stops(), [])                          # 書いただけ（決める取引が落ちた）
        job = next(j for j in jobs.JOBS if j.name == "ai_stops")
        self.assertTrue(job.wanted(SimpleNamespace(prober=None, verifier=None)))   # 確かめの部品が無くても動く
        jobs.ai_stops(SimpleNamespace(access=self.a, tenant_id=self.s["tenant"]))
        self.assertEqual(len(self.ai_stops()), 1)

    def test_a_glitch_that_did_not_end_the_call_only_warns(self):
        # 甚大でない（電話は続けられた）AIのおかしな動きは、止めずに、契約者と管理者のダッシュボードに警告（2026-10-31 管理者：「甚大な
        # 問題は止める。甚大でない場合は、ダッシュボードに警告を出す。」。甚大な問題＝お詫びして終えた通話は「止まった」で止める）
        aid = self.attempt()
        kinds, stopped = self.note(aid, trouble(errors.CONVO_AI, what="おかしな動き", error="同じ質問を2回続けた"))
        self.assertEqual((kinds, stopped), (["ai_glitch"], []))
        self.assertEqual(self.ai_stops(), [])
        self.assertEqual(self.blocking(), [])
        got = owner_access().in_tenant(SYSTEM, self.s["tenant"], lambda tx: tx.one(
            "SELECT count(*) AS n FROM salescall.notification_events WHERE kind = 'error_notice' AND tenant_id = %s"
            " AND refs ->> 'kind' = 'ai_glitch'", (self.s["tenant"],)))["n"]
        self.assertEqual(got, 1)                                               # 契約者に知らせる（ダッシュボードの警告）
        self.assertEqual(watch.after_ai_troubles(self.a, SYSTEM, self.s["tenant"]), [])   # 決め直しても止めない

    def test_the_emergency_route_is_only_for_ai_trouble_stops(self):
        # 緊急の道で再開できる停止は1か所（salescall.emergency_stop。入口・守り・判定の道が同じ関数を読む。見直し25b の 4）
        sid = self.a.in_tenant(SYSTEM, self.s["tenant"], lambda tx: stops.stop(   # システムが止めた、AIの不具合でない停止
            tx, scope="tenant", cause="program_trouble", reason="試験：こちらの不具合", tenant_id=self.s["tenant"]))["event_id"]
        self.assertIn("AIの不具合", self.possible(sid))
        with self.assertRaises(Rejected):                                      # 入口を通らなくても、守りが断る
            self.a.in_tenant(OPERATOR, self.s["tenant"], lambda tx: tx.execute(
                "INSERT INTO salescall.stop_emergency_passes (stop_event_id, tenant_id, reason, passed_by, actor_kind)"
                " VALUES (%s, salescall.cur_tenant(), '試験', salescall.cur_actor(), 'operator')", (sid,)))
        owner_access().in_tenant(OPERATOR, self.s["tenant"], lambda tx: (   # 守りを外して書いても、判定の道は付かない・止めたまま
            tx.execute("ALTER TABLE salescall.stop_emergency_passes DISABLE TRIGGER USER"),
            tx.execute("INSERT INTO salescall.stop_emergency_passes (stop_event_id, tenant_id, reason, passed_by, actor_kind)"
                       " VALUES (%s, salescall.cur_tenant(), '試験', salescall.cur_actor(), 'operator')", (sid,)),
            tx.execute("ALTER TABLE salescall.stop_emergency_passes ENABLE TRIGGER USER")))
        self.assertIsNone(self.judge_route(self.w.insert_claim(_with_task(self.w, self.s))))


def _other_phone() -> str:
    from salescall.core.phone import normalize
    from tests.support import fictional_jp_fixed
    return normalize(fictional_jp_fixed()).e164


def _with_task(w: World, s: dict) -> dict:
    """同じ実施回の、別の架空のお客様の行と、別の回線（同じ行・同じ回線に進行中の予約は1つだけなので、別々に予約する）。"""
    phone = _other_phone()
    k = w.contact(s["tenant"], s["client"], phone)
    return dict(s, task=w.task(s["tenant"], s["client"], s["run"], k), contact=k, phone=phone, line=w.line(s["tenant"]))


if __name__ == "__main__":
    unittest.main()
