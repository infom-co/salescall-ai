"""締めの処理の試験（1回の発信のあと：判定 → 結果の版・切れ方・発信禁止・次の動きを1つの取引で）。DESIGN.md 7章。"""

from __future__ import annotations

import time
import unittest
from datetime import timedelta

from salescall.core import (attempts, business_rules, calling_hours, call_report, dnc, gate, next_action, quality_stage, results,
                            settle, stops, watch)
from salescall.core.errors import ErrorReport
from salescall.core.access import Rejected
from salescall.core.call_report import CallReport, Callback, Cut
from tests.support import CLEAN, OPERATOR, SYSTEM, app_access, owner_access, rules_raw
from tests.test_gate import KEYS, Base, FakeDriver, dial_exit

TALK = ["dialing", "answered", "in_conversation", "ending", "ended"]


def rep(parties=("target",), *, dnc_=False, callback=None, cut=Cut("ai", True, False), not_reached=None):
    return CallReport(tuple(parties), dnc_, False, False, False, not_reached, callback, cut)


class SettleBase(Base):
    """締めの試験の土台（試験は持たない）。"""
    def call(self, s, path=TALK, *, report=None, items=(), verify="done", side=None):
        """本物の門・出口を通して1回かけ、path まで進め、項目と報告を置く（締めはまだ）。"""
        c = self.claim(s)
        self.assertTrue(dial_exit.dial(self.w.a, SYSTEM, s["tenant"], s["client"], c.attempt_id, FakeDriver()))
        for st in path[1:]:
            self.tx(s, lambda tx: attempts.move(tx, c.attempt_id, st, hangup_side=side if st == "dropped" else None))
        for q, state, cond in items:
            self.tx(s, lambda tx: tx.execute(
                "INSERT INTO salescall.item_values (tenant_id, client_id, task_id, version_id, question_id, state, condition)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s)", (s["tenant"], s["client"], s["task"], s["version"], q, state, cond)))
        if "answered" in path:
            self.tx(s, lambda tx: call_report.save(tx, c.attempt_id, report or rep()))
            if verify:
                self.tx(s, lambda tx: attempts.record_verification(tx, c.attempt_id, summary=CLEAN))
        return c.attempt_id

    def settle(self, s, aid):
        return self.tx(s, lambda tx: settle.settle(tx, aid))


class Settle(SettleBase):
    def test_completed_ends_the_row(self):
        s = self.ready()
        aid = self.call(s, items=[("Q1", "confirmed", None)])
        got = self.settle(s, aid)
        self.assertEqual((got["state"], got["decision"].kind, got["decision"].end_reason), ("completed", "end", "completed"))
        r = self.tx(s, lambda tx: results.latest(tx, s["task"]))
        self.assertEqual((r["kind"], r["attempt_id"], r["snapshot"]["items"]["Q1"]["state"]), ("judged", aid, "confirmed"))
        self.assertEqual(self.tx(s, lambda tx: tx.one("SELECT disconnect FROM salescall.attempts WHERE attempt_id = %s",
                                                      (aid,)))["disconnect"], "normal")
        self.assertEqual(self.reason(s), "task_ended")

    def test_no_answer_is_open_and_scheduled_by_the_policy(self):
        s = self.ready()
        aid = self.call(s, ["dialing", "no_answer"])
        got = self.settle(s, aid)
        self.assertEqual((got["state"], got["decision"].kind, got["decision"].promised), ("open", "call", False))
        nxt = self.tx(s, lambda tx: next_action.current(tx, s["task"]))
        p = self.tx(s, lambda tx: gate.pacing(tx, s["task"]))
        self.assertGreaterEqual(nxt["call_at"], p.earliest)          # 門の最も早い時刻より前にしない

    def test_refusal_registers_dnc_and_ends(self):
        s = self.ready()
        aid = self.call(s, report=rep(dnc_=True))
        got = self.settle(s, aid)
        self.assertEqual((got["state"], got["decision"].end_reason), ("do_not_call", "do_not_call"))
        hit = self.tx(s, lambda tx: dnc.lookup(tx, s["phone"]))
        self.assertEqual(hit["ledger"], "client")

    def test_hangup_during_our_talk_is_refusal_and_line_cut_is_not(self):
        s = self.ready()
        aid = self.call(s, ["dialing", "answered", "dropped"], report=rep(cut=Cut("ai", False, False)), side="callee")
        self.assertEqual(self.settle(s, aid)["state"], "do_not_call")
        s2 = self.ready()
        aid2 = self.call(s2, ["dialing", "answered", "dropped"], report=rep(cut=Cut(None, False, False)), side="line",
                         items=[])
        got = self.settle(s2, aid2)
        self.assertEqual(got["state"], "incomplete")
        self.assertEqual(self.tx(s2, lambda tx: tx.one("SELECT disconnect FROM salescall.attempts WHERE attempt_id = %s",
                                                       (aid2,)))["disconnect"], "interrupted")
        p = self.tx(s2, lambda tx: gate.pacing(tx, s2["task"], promised=False))
        self.assertEqual(p.remaining, 3)                              # 不自然な中断は、かけた回数に数えない

    def test_retry_interval_in_hours_or_days(self):
        """かけ直しの間隔は業務ごとに「n 時間おき」「n 日後」（2026-10-15 ユーザー）。n 日後は、前回と別の時間帯で、
        NG時間を避けた最も早い時刻。"""
        hours = self.ready()
        got = self.settle(hours, self.call(hours, ["dialing", "no_answer"]))
        span = self.tx(hours, lambda tx: tx.one("SELECT %s - claimed_at AS d FROM salescall.attempts"
                                                " WHERE task_id = %s", (got["decision"].at, hours["task"])))["d"]
        self.assertGreaterEqual(span, timedelta(hours=1))                         # 1時間おき（試験の決まり）
        days = self.ready(rules=rules_raw(retry={k: {"action": "call", "max_times": 3, "interval": {"days": 2}}
                                                 for k in business_rules.POLICY_KEYS}))
        aid = self.call(days, ["dialing", "no_answer"])
        # NG時間は発信のあとに入れる（いまが昼休みでも、この試験の発信は通す。効かせたいのは次の予定の決め方）
        self.put_client(days, "calling.ng_hours", {k: [["12:00", "13:00"]] for k in KEYS})
        got = self.settle(days, aid)
        row = self.tx(days, lambda tx: tx.one(
            "SELECT salescall.jst(%s) AS next, salescall.jst(claimed_at) AS tried, salescall.jst(now())::date AS today"
            " FROM salescall.attempts WHERE task_id = %s", (got["decision"].at, days["task"])))
        self.assertEqual(row["next"].date(), row["today"] + timedelta(days=2))
        self.assertNotEqual(calling_hours.band_of(row["next"], [12, 15]), calling_hours.band_of(row["tried"], [12, 15]))
        self.assertFalse(12 <= row["next"].hour < 13)                              # NG時間（昼休み）は避ける

    def test_a_fax_line_is_never_called_again_and_a_complaint_reaches_the_contractor(self):
        """FAX の回線：その番号は発信禁止（その番号だけ）にして行を終える（Intent.md 9-4）。苦情：行を終え、契約者へ知らせる
        （Intent.md 18-3：クレームは契約者が対応する特例）。どちらも 2026-10-17 の計画書との突き合わせで直した。"""
        s = self.ready()
        aid = self.call(s, report=rep(("fax",), cut=None))
        got = self.settle(s, aid)
        self.assertEqual((got["state"], got["decision"].kind), ("out_of_scope", "end"))
        dnc_rows = self.tx(s, lambda tx: tx.all("SELECT phone_e164, client_ref, reason::text AS reason"
                                                " FROM salescall.dnc_entries WHERE source_attempt_id = %s", (aid,)))
        self.assertEqual(dnc_rows, [{"phone_e164": s["phone"], "client_ref": None, "reason": "fax_line"}])
        c = self.ready()
        aid = self.call(c, report=CallReport(("target",), False, False, True, False, None, None, Cut("ai", True, False)),
                        items=[("Q1", "confirmed", None)])
        got = self.settle(c, aid)
        self.assertEqual(got["decision"].kind, "end")
        rows = self.tx(c, lambda tx: tx.all("SELECT kind, summary FROM salescall.notification_events WHERE event_key = %s",
                                            (f"complaint:{aid}",)))
        self.assertEqual(rows, [{"kind": "complaint", "summary": "苦情があった通話がありました。"}])

    def test_unknown_holds_the_row_and_is_rejudged_when_fixed(self):
        """判定できない（切れ方の材料が報告されていない）：行は終えずに判定待ちで残し、知らせの台帳に入れる。
        原因が直ったら（録音から作り直した報告）、判定し直して、ふつうの次の動きへ（2026-10-16 ユーザー決定）。"""
        s = self.ready()
        aid = self.call(s, report=rep(cut=None), items=[("Q1", "confirmed", None)])
        got = self.settle(s, aid)
        self.assertEqual((got["state"], got["decision"].kind), ("unknown", "hold"))
        rows = self.tx(s, lambda tx: tx.all("SELECT kind FROM salescall.notification_events WHERE event_key = %s",
                                            (f"judge_unknown:{aid}",)))
        self.assertEqual(rows, [{"kind": "judge_unknown"}])
        admin = self.tx(s, lambda tx: tx.all("SELECT for_admin FROM salescall.notification_events WHERE event_key = %s",
                                             (f"admin:judge_unknown:{aid}",)))
        self.assertEqual(admin, [{"for_admin": True}])                    # 会話の処理の誤り：直せる管理者にも知らせる
        self.assertEqual(self.tx(s, lambda tx: tx.one("SELECT disconnect FROM salescall.attempts WHERE attempt_id = %s",
                                                      (aid,)))["disconnect"], None)      # 判定できないときは何も決めない
        self.assertEqual(self.reason(s), "judge_pending")                 # 門は、判定待ちの行にかけない
        self.assertEqual(self.tx(s, settle.rejudge_held), [])            # 何も直っていなければ、判定し直さない
        self.tx(s, lambda tx: call_report.save_rebuilt(tx, aid, rep(), evidence="試験：録音から作り直した"))
        again = self.tx(s, settle.rejudge_held)
        self.assertEqual([(g["state"], g["decision"].kind, g["decision"].end_reason) for g in again],
                         [("completed", "end", "completed")])
        kinds = self.tx(s, lambda tx: [r["kind"] for r in tx.all(
            "SELECT kind::text AS kind FROM salescall.task_results WHERE task_id = %s ORDER BY version_no", (s["task"],))])
        self.assertEqual(kinds, ["judged", "rejudged"])                   # 判定できなかった記録も残す
        self.assertEqual(self.tx(s, settle.rejudge_held), [])            # もう判定待ちではない

    def test_a_held_row_is_rejudged_when_its_items_are_fixed(self):
        """「わからない」の理由が無い（会話の処理の誤り）で判定待ちになった行は、項目の値が直ったら判定し直す（2026-10-16
        見直し：前は項目を比べず、会話の処理を直して項目を作り直しても、判定待ちのままだった）。"""
        s = self.ready()
        aid = self.call(s, items=[("Q1", "dont_know", None)])
        got = self.settle(s, aid)
        self.assertEqual((got["state"], got["decision"].kind), ("unknown", "hold"))
        self.assertEqual(self.tx(s, settle.rejudge_held), [])
        self.tx(s, lambda tx: tx.execute("UPDATE salescall.item_values SET reason = 'undecided' WHERE task_id = %s"
                                         " AND question_id = 'Q1'", (s["task"],)))        # 会話の処理が理由を書き直した
        again = self.tx(s, settle.rejudge_held)
        self.assertEqual([g["state"] for g in again], ["incomplete"])

    def test_rebuilt_reports_cannot_be_changed(self):
        s = self.ready()
        aid = self.call(s, report=rep(cut=None))
        self.tx(s, lambda tx: call_report.save_rebuilt(tx, aid, rep(), evidence="試験"))
        own = owner_access()
        for sql in ("UPDATE salescall.call_report_rebuilds SET evidence = 'x'", "DELETE FROM salescall.call_report_rebuilds"):
            with self.subTest(sql=sql), self.assertRaises(Rejected):
                own.in_client(OPERATOR, s["tenant"], s["client"], lambda tx: tx.execute(sql))

    def test_wrong_party_registers_only_that_number(self):
        s = self.ready()
        aid = self.call(s, report=rep(("wrong_party",), cut=Cut("ai", True, False)))
        self.assertEqual(self.settle(s, aid)["state"], "out_of_scope")
        row = self.tx(s, lambda tx: tx.one("SELECT reason::text AS r FROM salescall.dnc_entries WHERE phone_e164 = %s",
                                           (s["phone"],)))
        self.assertEqual(row["r"], "wrong_party")

    def test_callback_is_a_promised_schedule(self):
        s = self.ready()
        at = self.tx(s, lambda tx: tx.one("SELECT now() + interval '2 hours' AS t")["t"])
        aid = self.call(s, report=rep(callback=Callback("later", at, None, None, None)))
        got = self.settle(s, aid)
        nxt = self.tx(s, lambda tx: next_action.current(tx, s["task"]))
        self.assertEqual((got["state"], nxt["kind"], nxt["promised"]), ("callback_scheduled", "call", True))
        self.assertGreaterEqual(nxt["call_at"], at)
        self.assertEqual(nxt["callee_at"], at)                          # お客様が言った時刻（NG時間でもかけてよい根拠）

    def test_not_ready_and_once_only(self):
        s = self.ready()
        aid = self.call(s, verify=None)
        with self.assertRaises(settle.NotReady):
            self.settle(s, aid)
        self.assertEqual([r["attempt_id"] for r in self.tx(s, lambda tx: settle.unsettled(tx))
                          if r["task_id"] == s["task"]], [])
        self.tx(s, lambda tx: attempts.record_verification(tx, aid, summary=CLEAN))
        self.assertIn(aid, [r["attempt_id"] for r in self.tx(s, lambda tx: settle.unsettled(tx))])
        first = self.settle(s, aid)
        again = self.settle(s, aid)
        self.assertEqual((again["already"], again["version_no"]), (True, first["version_no"]))
        self.assertNotIn(aid, [r["attempt_id"] for r in self.tx(s, lambda tx: settle.unsettled(tx))])

    def test_a_verification_that_cannot_run_waits_and_never_ends_the_row(self):
        """通話後の確かめができないのは、仕組みの不具合だけ。行を終えず、確かめは待ちのまま（直ってからやり直す）。待っているあいだ、
        門は同じお客様にかけない（2026-10-17 ユーザー：「そんな状態自体が問題では？」）。「確かめられなかった」は書けない。"""
        s = self.ready()
        aid = self.call(s, verify=None)
        with self.assertRaises(Rejected):
            self.tx(s, lambda tx: tx.execute("UPDATE salescall.attempts SET verification = 'failed' WHERE attempt_id = %s",
                                             (aid,)))
        with self.assertRaises(settle.NotReady):
            self.settle(s, aid)
        other_run = self.w.run(s["tenant"], s["client"], s["business"], s["version"])   # 同じお客様の、ほかの一覧の行
        other = self.w.task(s["tenant"], s["client"], other_run, s["contact"])
        self.assertEqual(self.tx(s, lambda tx: gate.pacing(tx, other)).binding, "related_unsettled")
        kind = watch.after_error(app_access(), SYSTEM, ErrorReport("recording", None, "試験：録音のファイルが無い"),
                                 tenant_id=s["tenant"], client_id=s["client"], line_id=s["line"], attempt_id=aid)
        self.assertEqual(kind, "recording")
        self.assertEqual([r["cause"] for r in self.tx(s, lambda tx: stops.open_stops(
            tx, tenant_id=s["tenant"], client_id=None, business_id=None, line_id=s["line"])) if r["scope"] == "line"],
            ["line_failure"])                                           # 録音が残らない回線は、すぐ止める
        self.tx(s, lambda tx: attempts.record_verification(tx, aid, summary=CLEAN))   # 直ってからやり直して済めば
        got = self.settle(s, aid)
        self.assertNotEqual(got["decision"].kind, "end")

    def test_gate_does_not_dial_without_rules(self):
        """判定できない通話をしない：業務の決まりが無い版では、門が通さない。"""
        s = self.ready()
        v2 = self.w.add_version(s["tenant"], s["client"], s["business"], 2)
        self.tx(s, lambda tx: tx.execute(
            "INSERT INTO salescall.run_versions (tenant_id, client_id, run_id, business_id, seq, version_id, assigned_by, reason)"
            " VALUES (%s, %s, %s, %s, 2, %s, '試験', '決まりの無い版')", (s["tenant"], s["client"], s["run"], s["business"], v2)),
            OPERATOR)
        self.tx(s, lambda tx: quality_stage.open_stage(tx, v2, 1, None, evidence="試験"), OPERATOR)
        self.assertEqual(self.reason(s), "rules_missing")

    def test_missing_rules_are_not_guessed(self):
        """業務の決まりが無い版でかけた発信は、締めない（既定値で決めない）。何も書かない。"""
        s = self.ready()
        v2 = self.w.add_version(s["tenant"], s["client"], s["business"], 2)
        self.tx(s, lambda tx: tx.execute(
            "INSERT INTO salescall.run_versions (tenant_id, client_id, run_id, business_id, seq, version_id, assigned_by, reason)"
            " VALUES (%s, %s, %s, %s, 2, %s, '試験', '決まりの無い版')", (s["tenant"], s["client"], s["run"], s["business"], v2)),
            OPERATOR)
        self.tx(s, lambda tx: quality_stage.open_stage(tx, v2, 1, None, evidence="試験"), OPERATOR)
        aid = self.w.insert_claim(dict(s, version=v2))      # 門を通らずに作った試行（門の守りが外れた場合を試す）
        for st in ("dialing", "no_answer"):
            self.w.move(s, aid, st)
        with self.assertRaises(business_rules.RulesMissing):
            self.settle(s, aid)
        self.assertIsNone(self.tx(s, lambda tx: results.latest(tx, s["task"])))


class DatabaseGuards(Base):
    def test_one_judgement_per_attempt(self):
        s = self.ready()
        aid = self.past_attempt(s, ["dialing", "no_answer"])
        with self.assertRaises(Rejected):
            self.tx(s, lambda tx: tx.execute(
                "INSERT INTO salescall.task_results (tenant_id, client_id, task_id, version_no, kind, business_state,"
                " attempt_id, judge_version, complaint, detail, snapshot, decided_by)"
                " VALUES (%s, %s, %s, 99, 'judged', 'open', %s, 'x', false, '{}', '{}', 'x')",
                (s["tenant"], s["client"], s["task"], aid)))

    def test_disconnect_is_written_once_after_the_end(self):
        s = self.ready()
        c = self.claim(s)
        with self.assertRaises(Rejected):
            self.tx(s, lambda tx: attempts.record_disconnect(tx, c.attempt_id, "normal"))   # まだ終わっていない
        aid = self.past_attempt(self.ready(), ["dialing", "answered", "dropped"], side="line",
                                report=rep(cut=Cut("callee", False, False)), verify="done")
        with self.assertRaises(Rejected):
            owner_access().in_system(SYSTEM, lambda tx: tx.execute(
                "UPDATE salescall.attempts SET disconnect = 'refused' WHERE attempt_id = %s", (aid,)))

    def test_reports_and_rules_cannot_be_changed(self):
        s = self.ready()
        for sql in ("UPDATE salescall.business_rules SET rules = '{}' WHERE version_id = %s",
                    "DELETE FROM salescall.business_rules WHERE version_id = %s"):
            with self.subTest(sql=sql), self.assertRaises(Rejected):
                owner_access().in_system(OPERATOR, lambda tx: tx.execute(sql, (s["version"],)))

    def test_item_reason_only_when_not_answered(self):
        s = self.ready()
        with self.assertRaises(Rejected):
            self.tx(s, lambda tx: tx.execute(
                "INSERT INTO salescall.item_values (tenant_id, client_id, task_id, version_id, question_id, state, reason)"
                " VALUES (%s, %s, %s, %s, 'Q1', 'confirmed', 'undecided')",
                (s["tenant"], s["client"], s["task"], s["version"])))


class AfterDenial(Base):
    def test_outside_hours_moves_to_the_next_opening(self):
        s = self.ready()
        self.put_client(s, "calling.hours", {k: [] for k in ("mon", "tue", "wed", "thu", "fri", "sat", "sun", "holiday")})
        d = self.claim(s)
        got = self.tx(s, lambda tx: next_action.after_denial(tx, s["task"], d, tenant_id=s["tenant"], client_id=s["client"],
                                                             business_id=s["business"]))
        # これから先、時間帯が無い（依頼者の時間帯を空にした）：行を終えず、1日後にもう一度確かめる（2026-10-16 の見直し G6）
        self.assertEqual(got.kind, "call")
        self.assertEqual(self.tx(s, lambda tx: next_action.current(tx, s["task"]))["kind"], "call")

    def test_permanent_reason_ends_and_waiting_reason_reschedules(self):
        s = self.ready()
        self.tx(s, lambda tx: dnc.register(tx, s["phone"], "manual"))
        d = self.claim(s)
        got = self.tx(s, lambda tx: next_action.after_denial(tx, s["task"], d, tenant_id=s["tenant"], client_id=s["client"],
                                                             business_id=s["business"]))
        self.assertEqual((got.kind, got.end_reason), ("end", "dnc_client"))
        t = self.ready()
        self.lease(t, holder="ほかの見守り役")
        d = self.claim(t)
        self.assertEqual(d.reason, "line_not_leased")
        got = self.tx(t, lambda tx: next_action.after_denial(tx, t["task"], d, tenant_id=t["tenant"], client_id=t["client"],
                                                             business_id=t["business"]))
        self.assertEqual(got.kind, "call")

    def test_awaiting_decision_writes_nothing(self):
        s = self.ready()
        self.past_attempt(s, ["dialing", "answered", "ending", "ended"], settled=False)
        before = self.tx(s, lambda tx: next_action.current(tx, s["task"]))
        d = self.claim(s)
        self.assertEqual(d.reason, "awaiting_decision")
        got = self.tx(s, lambda tx: next_action.after_denial(tx, s["task"], d, tenant_id=s["tenant"], client_id=s["client"],
                                                             business_id=s["business"]))
        self.assertIsNone(got)
        self.assertEqual(self.tx(s, lambda tx: next_action.current(tx, s["task"])), before)


if __name__ == "__main__":
    unittest.main()
