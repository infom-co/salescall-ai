"""締めの処理の試験（1回の発信のあと：判定 → 結果の版・切れ方・発信禁止・次の動きを1つの取引で）。DESIGN.md 7章。"""

from __future__ import annotations

import tempfile
import time
import unittest
import uuid
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

from salescall.core import (approval, attempts, backup, business_rules, calling_hours, call_report, dnc, fix_requests, gate,
                            next_action, quality_stage, recordings, results, settle, stops, watch)
from salescall.core.errors import ErrorReport
from salescall.core.access import Rejected
from salescall.core.call_report import Cut
from tests import support
from tests.support import ADMIN, CLEAN, OPERATOR, SYSTEM, app_access, callback, make_report, owner_access, rules_raw
from tests.test_gate import KEYS, Base, FakeDriver, dial_exit

TALK = ["dialing", "answered", "in_conversation", "ending", "ended"]


def rep(parties=("target",), *, dnc_=False, callback=None, cut=Cut("ai", True, False), not_reached=None):
    return make_report(parties, dnc=dnc_, callback=callback, cut=cut, not_reached=not_reached)


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
        aid = self.call(c, report=make_report(complaint=True),
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
        admin = self.w.a.in_system(ADMIN, lambda tx: tx.all(          # 管理者あては、全体の取引で読む（0084）
            "SELECT for_admin FROM salescall.notification_events WHERE event_key = %s", (f"admin:judge_unknown:{aid}",)))
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
        at = self.tx(s, lambda tx: tx.one("SELECT date_trunc('minute', now() + interval '2 hours') AS t")["t"])
        aid = self.call(s, report=rep(callback=callback("later", at)))   # お客様が言った時刻（分まで）
        got = self.settle(s, aid)
        nxt = self.tx(s, lambda tx: next_action.current(tx, s["task"]))
        self.assertEqual((got["state"], nxt["kind"], nxt["promised"]), ("callback_scheduled", "call", True))
        self.assertGreaterEqual(nxt["call_at"], at)
        self.assertEqual(nxt["callee_at"], at)                          # お客様が言った時刻（NG時間でもかけてよい根拠）

    def test_a_window_callback_is_called_inside_the_window(self):
        """「（日）の夕方」：幅の始まり（16時）にかけ、幅（〜18時・1日の中の16〜18時）を次の予定に持たせる。NG時間の根拠
        （お客様が指定したちょうどの時刻）は持たない（2026-10-19 ユーザー：「それに合わせてかけるべき」）。"""
        s = self.ready()
        day = self.tx(s, lambda tx: tx.one("SELECT (salescall.jst(now()) + interval '2 days')::date AS d")["d"])
        aid = self.call(s, report=rep(callback=callback("later", time=f"{day.month}月{day.day}日の夕方")))
        got = self.settle(s, aid)
        nxt = self.tx(s, lambda tx: tx.one(
            "SELECT kind, promised, callee_at, salescall.jst(call_at) AS at, salescall.jst(window_until) AS until, window_daily"
            " FROM salescall.task_next_actions WHERE task_id = %s ORDER BY seq DESC LIMIT 1", (s["task"],)))
        self.assertEqual(got["state"], "callback_scheduled")
        self.assertEqual((nxt["kind"], nxt["promised"], nxt["callee_at"]), ("call", True, None))
        self.assertEqual((nxt["at"], nxt["until"], nxt["window_daily"]),
                         (datetime.combine(day, datetime.min.time()) + timedelta(hours=16),
                          datetime.combine(day, datetime.min.time()) + timedelta(hours=18), [16 * 3600, 18 * 3600]))

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

    def test_a_recording_that_never_arrives_is_settled_without_it_after_the_wait(self):
        """録音が届かないまま、通話の終わりから待つ時間（10分）を過ぎたら、確かめを「録音なし」で済ませ、通話中の報告だけで結果を
        決める（2026-10-18 ユーザー決定）。待つ時間の中では済ませない。録音が残らないことは修正依頼にする。"""
        s = self.ready()
        aid = self.call(s, items=[("Q1", "confirmed", None)], verify=None)
        with self.assertRaises(attempts.RecordingWaitNotOver):                  # まだ待つ時間の中
            self.tx(s, lambda tx: attempts.finish_without_recording(tx, aid))
        self.override("verification.recording_wait", 1)
        time.sleep(1.1)
        self.tx(s, lambda tx: attempts.finish_without_recording(tx, aid))
        got = self.tx(s, lambda tx: tx.one("SELECT recording_missing, items_checked FROM salescall.verification_summaries"
                                           " WHERE attempt_id = %s", (aid,)))
        self.assertEqual((got["recording_missing"], got["items_checked"]), (True, 0))
        self.assertEqual(self.settle(s, aid)["state"], "completed")              # 通話中の報告だけで決まる
        asked = self.w.a.in_system(SYSTEM, lambda tx: [r["kind"] for r in fix_requests.open_requests(tx, "CL-2")])
        self.assertIn("recording", asked)

    def test_a_verification_that_cannot_run_waits_and_never_ends_the_row(self):
        """通話後の確かめができないのは、仕組みの不具合だけ。行を終えず、確かめは待ちのまま（直ってからやり直す）。待っているあいだ、
        門は同じお客様にかけない（2026-10-17 ユーザー：「そんな状態自体が問題では？」）。「確かめられなかった」は書けない。録音が残らなくても回線は止めず、修正依頼を出す。"""
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
        self.assertEqual([r for r in self.tx(s, lambda tx: stops.open_stops(
            tx, tenant_id=s["tenant"], client_id=None, business_id=None, line_id=s["line"])) if r["scope"] == "line"],
            [])                                                         # 回線は止めない（修正依頼を出して、ほかの行を続ける）
        self.assertIn("recording", [r["kind"] for r in app_access().in_system(SYSTEM, lambda tx: fix_requests.open_requests(
            tx, "CL-2"))])
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


class CutOffCalls(SettleBase):
    """途中で切れた通話（お客様が出たあと、こちらの仕組みが止まった・回線が切れた）は、復旧次第、お詫びとともにすぐかけ直す
    （2026-10-17 ユーザー：「復旧次第、お詫びとともにすぐかけ直さないといけないでしょ！」）。回数・間隔には数えない。"""

    def resumed(self, got):
        d = got["decision"]
        self.assertEqual((d.kind, d.target.purpose, d.promised), ("call", "resume", True))

    def test_a_line_drop_while_the_customer_talked_is_called_back_with_an_apology(self):
        s = self.ready()
        aid = self.call(s, ("dialing", "ringing", "answered", "in_conversation", "dropped"), side="line",
                        report=rep(cut=Cut("callee", False, False)))
        self.resumed(self.settle(s, aid))
        c = self.claim(s)                                               # すぐ（間隔をあけずに）かけられる
        self.assertEqual((type(c).__name__, getattr(c, "purpose", None)), ("Claim", "resume"))

    def test_at_most_two_call_backs_in_a_row_then_the_usual_retry(self):
        """お詫びしてすぐかけ直すのは、同じ行で続けて2回まで（2026-10-18 ユーザー 案A）。3回目に切れたら、ふつうの再発信の方針。"""
        s = self.ready()
        cut = ("dialing", "ringing", "answered", "in_conversation", "dropped")
        purposes = []
        for _ in range(3):
            aid = self.call(s, cut, side="line", report=rep(cut=Cut("callee", False, False)))
            d = self.settle(s, aid)["decision"]
            purposes.append(d.target.purpose if d.kind == "call" and d.target else d.kind)
        self.assertEqual(purposes[:2], ["resume", "resume"])
        self.assertNotEqual(purposes[2], "resume")

    def test_a_call_cut_by_our_failure_is_called_back_with_an_apology(self):
        s = self.ready()
        aid = self.call(s, ("dialing", "ringing", "answered", "in_conversation", "technical_error"))
        self.resumed(self.settle(s, aid))

    def test_a_call_back_keeps_the_promised_window(self):
        """幅の約束の電話が途中で切れた：すぐのかけ直しも、同じ約束の回のまま（話せなければ、理由に合わせた次の回へ）。"""
        s = self.ready()
        now = self.tx(s, lambda tx: tx.one("SELECT now() AS t")["t"])
        window = next_action.Window(now + timedelta(days=1), (0, 24 * 3600), None, now - timedelta(hours=1), "day")
        self.tx(s, lambda tx: support.schedule_call(tx, s["task"], now, contact_id=s["contact"], cause="試験", promised=True,
                                                       window=window))
        aid = self.call(s, ("dialing", "ringing", "answered", "in_conversation", "technical_error"))
        got = self.settle(s, aid)
        self.resumed(got)
        self.assertEqual(got["decision"].window, window)

    def test_a_call_back_keeps_the_promised_exact_time(self):
        """時刻ちょうどの約束の電話が途中で切れた：すぐのかけ直しも、お客様が指定した時刻を持つ（NG時間の枠の中の約束でも、門が
        止めない。見直し7で試験を足した）。"""
        s = self.ready()
        t = self.tx(s, lambda tx: tx.one("SELECT date_trunc('minute', now()) AS t")["t"])
        window = self.tx(s, lambda tx: next_action._exact_window(tx, s["task"], t))
        self.tx(s, lambda tx: support.schedule_call(tx, s["task"], t, contact_id=s["contact"], cause="試験", promised=True,
                                                       callee_at=t, window=window))
        got = self.settle(s, self.call(s, ("dialing", "ringing", "answered", "in_conversation", "technical_error")))
        self.resumed(got)
        self.assertEqual((got["decision"].callee_at, got["decision"].window), (t, window))

    def test_a_call_back_after_an_untimed_promise_keeps_the_promise(self):
        """時刻の決まっていない約束（「あとで」）をした通話・その約束を守っていた電話が途中で切れ、お詫びのすぐのかけ直しに出なかった：
        約束したかけ直しのまま（時刻の決まっていない約束の方針。⚠️ 前は、ふつうのかけ直しになり、行の回数の上限で終わった＝見直し5）。"""
        cut = ("dialing", "ringing", "answered", "in_conversation", "technical_error")
        for case in ("said_in_the_cut_call", "kept_by_the_cut_call"):
            with self.subTest(case=case):
                s = self.ready()
                self.put_client(s, "retry.max_attempts_per_task", 1)
                if case == "said_in_the_cut_call":
                    aid = self.call(s, cut, report=rep(callback=callback("later", time="あとで")))
                else:
                    now = self.tx(s, lambda tx: tx.one("SELECT now() AS t")["t"])
                    self.tx(s, lambda tx: support.schedule_call(tx, s["task"], now, contact_id=s["contact"],
                                                                cause="試験：時刻の決まっていない約束", promised=True))
                    aid = self.call(s, cut)
                self.resumed(self.settle(s, aid))
                d = self.settle(s, self.call(s, ["dialing", "no_answer"]))["decision"]
                self.assertEqual((d.kind, d.promised, d.target.purpose), ("call", True, "normal"))
                self.assertIn("時刻の決まっていない約束", d.rule)

    def test_the_time_the_callee_spoke_is_the_report_when_the_end_was_recorded_later(self):
        """お客様が言った「30分後」の起点：会話の処理が報告をしまった時刻が、通話の終わりの記録より早ければ、そちら（途中の誤りで
        終わった通話は、後始末した時刻が終わりになる＝2026-10-19 の見直し）。"""
        s = self.ready()
        c = self.claim(s)
        self.assertTrue(dial_exit.dial(self.w.a, SYSTEM, s["tenant"], s["client"], c.attempt_id, FakeDriver()))
        for st in ("answered", "in_conversation"):
            self.tx(s, lambda tx: attempts.move(tx, c.attempt_id, st))
        self.tx(s, lambda tx: call_report.save(tx, c.attempt_id, rep(callback=callback("later", time="30分後"))))
        time.sleep(0.3)
        self.tx(s, lambda tx: attempts.move(tx, c.attempt_id, "technical_error"))
        self.tx(s, lambda tx: attempts.record_verification(tx, c.attempt_id, summary=CLEAN))
        got = self.tx(s, lambda tx: tx.one(
            "SELECT a.ended_at, r.saved_at FROM salescall.attempts a JOIN salescall.call_reports r USING (attempt_id)"
            " WHERE a.attempt_id = %s", (c.attempt_id,)))
        self.assertLess(got["saved_at"], got["ended_at"])
        self.assertEqual(self.tx(s, lambda tx: settle._attempt(tx, c.attempt_id))["spoken_at"], got["saved_at"])
        self.settle(s, c.attempt_id)                                          # 判定も、この時刻から「30分後」を読む
        detail = self.tx(s, lambda tx: results.latest(tx, s["task"]))["detail"]
        self.assertEqual(datetime.fromisoformat(detail["callback"]["at"]), got["saved_at"] + timedelta(minutes=30))

    def test_a_customer_who_hung_up_during_their_own_words_is_not_called_back_at_once(self):
        """お客様の言葉の途中で切れても、回線の信号が「お客様が切った」なら、お客様が自分で切った（すぐかけ直さない）。"""
        s = self.ready()
        aid = self.call(s, ("dialing", "ringing", "answered", "in_conversation", "dropped"), side="callee",
                        report=rep(cut=Cut("callee", False, False)))
        got = self.settle(s, aid)["decision"]
        self.assertNotEqual(got.target and got.target.purpose, "resume")

    def test_a_customer_who_hung_up_or_was_not_reached_is_not_called_back_at_once(self):
        s = self.ready()
        aid = self.call(s, ("dialing", "ringing", "answered", "in_conversation", "dropped"), side="callee",
                        report=rep(cut=Cut("ai", False, False)))            # AIが話している途中で、お客様が切った＝断った
        self.assertEqual(self.settle(s, aid)["decision"].end_reason, "do_not_call")
        t = self.ready()
        aid = self.call(t, ("dialing", "technical_error"))               # お客様が出る前に止まった：お詫びは要らない
        got = self.settle(t, aid)["decision"]
        self.assertNotEqual(got.target and got.target.purpose, "resume")


class CallbackWindow(SettleBase):
    """お客様が言ったかけ直しの時間の幅（「夕方」「3時まで」「13日なら」など。DESIGN.md 7-2・表の作り 0086。2026-10-19 ユーザー：
    「それに合わせてかけるべき」。話せなかったら、お客様がその日を選んだ理由に合わせて次の回。理由の分からない日だけの約束は、
    日が過ぎたら時刻の決まっていない約束の方針）。"""

    def local_now(self, s):
        return self.tx(s, lambda tx: tx.one("SELECT salescall.jst(now()) AS t")["t"])

    def from_jst(self, s, t):
        return self.tx(s, lambda tx: tx.one("SELECT salescall.from_jst(%s) AS t", (t,))["t"])

    def latest(self, s):
        return self.tx(s, lambda tx: tx.one(
            "SELECT kind, promised, callee_at, salescall.jst(call_at) AS at, salescall.jst(window_until) AS until, window_daily"
            " FROM salescall.task_next_actions WHERE task_id = %s ORDER BY seq DESC LIMIT 1", (s["task"],)))

    def test_the_gate_does_not_call_outside_the_window_and_moves_to_its_next_start(self):
        """幅の外では、門がかけない（理由 callback_window）。次の動きは、幅の中の次の始まりへ予定を移す（幅はそのまま）。"""
        s = self.ready()
        local = self.local_now(s)
        lo = local.hour + 2 if local.hour <= 20 else local.hour - 6          # いまを含まない1時間
        day = local.date() if local.hour <= 20 else local.date() + timedelta(days=1)
        until = self.from_jst(s, datetime.combine(local.date() + timedelta(days=3), datetime.min.time()))
        now = self.tx(s, lambda tx: tx.one("SELECT now() AS t")["t"])
        self.tx(s, lambda tx: support.schedule_call(
            tx, s["task"], now, contact_id=s["contact"], cause="試験", promised=True,
            window=next_action.Window(until, (lo * 3600, (lo + 1) * 3600))))
        denied = self.claim(s)
        self.assertIsInstance(denied, gate.Denied)
        self.assertEqual(denied.reason, "callback_window")
        self.tx(s, lambda tx: next_action.after_denial(tx, s["task"], denied, tenant_id=s["tenant"], client_id=s["client"],
                                                       business_id=s["business"]))
        nxt = self.latest(s)
        self.assertEqual((nxt["at"], nxt["promised"], nxt["window_daily"]),
                         (datetime.combine(day, datetime.min.time()) + timedelta(hours=lo), True, [lo * 3600, (lo + 1) * 3600]))
        self.assertEqual(self.tx(s, lambda tx: tx.one("SELECT window_until FROM salescall.task_next_actions WHERE task_id = %s"
                                                      " ORDER BY seq DESC LIMIT 1", (s["task"],)))["window_until"], until)

    def opening(self, s, after, window, now=None):
        now = now or self.tx(s, lambda tx: tx.one("SELECT now() AS t")["t"])
        return self.tx(s, lambda tx: next_action._window_opening(tx, s["task"], after, window, now=now))

    def test_a_passed_window_moves_to_the_same_window_of_the_next_day(self):
        """時間の言葉がある幅が過ぎたら、次の日の同じ幅（幅の終わりは、その日の幅の終わり）。理由の分からない日だけの約束は、
        過ぎたら幅を外す（時刻の決まっていない約束＝契約者の間隔）。決めるのは next_action._window_opening の1か所。"""
        s = self.ready()
        d2 = self.local_now(s).date() + timedelta(days=2)
        at = lambda d, h: self.from_jst(s, datetime.combine(d, datetime.min.time()) + timedelta(hours=h))   # noqa: E731
        evening = next_action.Window(at(d2, 18), (16 * 3600, 18 * 3600), None, at(d2, 16), "day")   # 時間の言葉＝次の日の同じ幅
        got = self.opening(s, at(d2, 19), evening)
        self.assertEqual(got, (at(d2 + timedelta(days=1), 16),
                               next_action.Window(at(d2 + timedelta(days=1), 18 + 6), (16 * 3600, 18 * 3600), None,
                                                  at(d2 + timedelta(days=1), 0), "day", 1)))   # 次の回は次の日（1日の中の幅で絞る）
        got = self.opening(s, at(d2, 17), evening)
        self.assertEqual(got, (at(d2, 17), evening))                          # まだ幅の中：その時刻
        whole_day = next_action.Window(at(d2 + timedelta(days=1), 0), None)   # 「（d2）ならいつでも」
        self.assertEqual(self.opening(s, at(d2, 19), whole_day), (at(d2, 19), whole_day))
        self.assertEqual(self.opening(s, at(d2 + timedelta(days=1), 9), whole_day), (None, None))   # 日が過ぎた
        late = next_action.Window(at(d2, 23), (22 * 3600, 23 * 3600), None, at(d2, 22), "day")
        self.put_client(s, "calling.hours", {k: [["09:00", "18:00"]] for k in KEYS})
        self.assertEqual(self.opening(s, at(d2, 9), late), (None, None))     # 幅がかけてよい時間帯と重ならない：約束の回は無い
        self.put_client(s, "calling.hours", {k: [] for k in KEYS})
        self.assertEqual(self.opening(s, at(d2, 9), evening), (None, evening))   # 時間帯そのものが無い：幅を保つ（設定の形）
        self.put_client(s, "calling.hours", {k: [["00:00", "24:00"]] for k in KEYS})
        self.put_client(s, "calling.ng_hours", {k: [["16:00", "17:00"]] for k in KEYS})
        got = self.opening(s, at(d2, 9), evening)
        self.assertEqual(got[0], at(d2, 17))                                  # 幅の中でも NG時間は守る（時刻の指定ではない）

    def test_the_gate_keeps_to_the_kinds_of_days(self):
        """「平日なら」「土日祝なら」：その日の種類の外では、門がかけない。次の動きは、その種類の次の日の始まりへ（0087）。"""
        from salescall.core.calendar import holiday_checker
        s = self.ready()
        today = self.tx(s, lambda tx: calling_hours.day_key(tx.one("SELECT salescall.jst(now())::date AS d")["d"],
                                                             holiday_checker(tx)))
        others = tuple(k for k in KEYS if k != today)
        now = self.tx(s, lambda tx: tx.one("SELECT now() AS t")["t"])
        self.tx(s, lambda tx: support.schedule_call(
            tx, s["task"], now, contact_id=s["contact"], cause="試験", promised=True,
            window=next_action.Window(None, None, others)))
        denied = self.claim(s)
        self.assertEqual(getattr(denied, "reason", None), "callback_window")
        self.tx(s, lambda tx: next_action.after_denial(tx, s["task"], denied, tenant_id=s["tenant"], client_id=s["client"],
                                                       business_id=s["business"]))
        nxt = self.tx(s, lambda tx: tx.one(
            "SELECT salescall.jst(call_at) AS at, window_days FROM salescall.task_next_actions WHERE task_id = %s"
            " ORDER BY seq DESC LIMIT 1", (s["task"],)))
        key = self.tx(s, lambda tx: calling_hours.day_key(nxt["at"].date(), holiday_checker(tx)))
        self.assertIn(key, others)
        self.assertEqual((nxt["at"].hour, nxt["at"].minute, sorted(nxt["window_days"])), (0, 0, sorted(others)))

    def test_a_day_with_no_calling_time_is_retried_after_the_day_not_before(self):
        """日だけの約束（「（d4）なら」）で、その日にかけられる時刻が無い（その曜日は時間帯が無い）：その日が過ぎてから、時刻の
        決まっていない約束の方針の間隔で。お客様と約束したかけ直しのまま。⚠️ 前は、いまから間隔を数え、お客様が言った日より前にかけた（2026-10-19 の見直し）。
        門で幅が過ぎたと分かったとき（_window_over）も同じ。"""
        from salescall.core.calendar import holiday_checker
        s = self.ready()
        d4 = self.local_now(s).date() + timedelta(days=4)
        key = self.tx(s, lambda tx: calling_hours.day_key(d4, holiday_checker(tx)))
        self.put_client(s, "calling.hours", {k: ([] if k == key else [["00:00", "24:00"]]) for k in KEYS})
        aid = self.call(s, report=rep(callback=callback("later", time=f"{d4.month}月{d4.day}日なら")))
        self.assertEqual(self.settle(s, aid)["state"], "callback_scheduled")
        after_day = datetime.combine(d4 + timedelta(days=1), datetime.min.time())
        nxt = self.latest(s)
        self.assertEqual((nxt["kind"], nxt["promised"], nxt["until"]), ("call", True, None))
        self.assertGreaterEqual(nxt["at"], after_day + timedelta(hours=1))   # 試験の方針の間隔＝1時間
        target = next_action.Target(s["contact"])
        now = self.tx(s, lambda tx: tx.one("SELECT now() AS t")["t"])
        d = self.tx(s, lambda tx: next_action._window_over(
            tx, s["task"], now, target, next_action.Window(self.from_jst(s, after_day), None)))
        self.assertEqual((d.kind, d.promised), ("call", True))
        self.assertGreaterEqual(d.at, self.from_jst(s, after_day + timedelta(hours=1)))

    def test_a_weekday_promise_moves_to_the_same_weekday_and_ends_after_the_policy_count(self):
        """「（曜日）なら」：その日に話せなかったら、次の週の同じ曜日（お客様がその日を選んだ理由に合わせる）。約束したかけ直しのまま。
        次の回へ移るたびに数え、時刻の決まっていない約束の方針の回数（試験の方針は3回）を超えたら終える。その日に一度も
        かけられなかったら、お詫びの印を付ける（2026-10-19 ユーザー決定）。"""
        s = self.ready()
        local = self.local_now(s)
        d = local.date() + timedelta(days=3)
        word = "月火水木金土日"[d.weekday()] + "曜日なら"
        aid = self.call(s, report=rep(callback=callback("later", time=word)))
        self.assertEqual(self.settle(s, aid)["state"], "callback_scheduled")
        first = self.tx(s, lambda tx: next_action.window_of(next_action.current(tx, s["task"])))
        midnight = lambda day: self.from_jst(s, datetime.combine(day, datetime.min.time()))   # noqa: E731
        self.assertEqual((first.repeat, first.round, first.since, first.until),
                         ("week", 0, midnight(d), midnight(d + timedelta(days=1))))
        target = next_action.Target(s["contact"])
        now = self.tx(s, lambda tx: tx.one("SELECT now() AS t")["t"])
        moved = self.tx(s, lambda tx: next_action._keep_promise(tx, s["task"], first.until, first, target, now=now))
        self.assertEqual((moved.kind, moved.promised, moved.window.round, moved.window.since, moved.apology),
                         ("call", True, 1, midnight(d + timedelta(days=7)), True))   # 次の週の同じ曜日・その日にかけていない
        self.assertEqual(moved.at, midnight(d + timedelta(days=7)))
        late = self.tx(s, lambda tx: next_action._keep_promise(tx, s["task"], first.until, first, target,
                                                               now=first.until + timedelta(days=14)))
        self.assertEqual((late.window.round, late.window.since, late.apology),
                         (3, midnight(d + timedelta(days=21)), True))   # 話せないまま過ぎた回も数える（見守り役が止まっていた）
        last = replace(first, round=3)
        over = self.tx(s, lambda tx: next_action._keep_promise(tx, s["task"], last.until, last, target, now=now))
        self.assertEqual((over.kind, over.end_reason), ("end", "callback_unspecified_time"))   # 最初の回＋方針の回数（3）を超える回は終える
        self.put_client(s, "calling.hours", {k: [] for k in KEYS})
        kept = self.tx(s, lambda tx: next_action._keep_promise(tx, s["task"], first.until, first, target, now=now))
        self.assertEqual((kept.kind, kept.promised, kept.window), ("call", True, first))   # 時間帯が無い：約束の幅のまま確かめ直す
        self.assertEqual(kept.at, now + next_action.NO_OPENING_RECHECK)

    def test_an_exact_time_promise_is_called_a_few_times_in_its_hour_then_on_the_next_days(self):
        """時刻ちょうどの約束（「明日の15時」）に出なければ、その時刻からの時間（初め1時間）の中で、前の電話から5分後・さらに10分後・
        さらに20分後にかけ、それでも出なければ次の日の同じ時刻の回へ。約束の日を含めて3日で打ち止め（2026-10-19 ユーザー：「「明日の
        15時」ちょうどに出られなければ、15時から16時に数回かける。出なければ、翌日の15時、翌々日の15時で打ち止め」「5分後→さらに
        10分後→さらに20分後くらいがいいんじゃない？」）。"""
        s = self.ready()
        self.put_client(s, "calling.ng_hours", {k: [] for k in KEYS})
        clock = self.tx(s, lambda tx: tx.one(
            "SELECT now() AS now, date_trunc('minute', now()) - interval '1 minute' AS t,"
            " extract(epoch FROM date_trunc('day', salescall.jst(now())) + interval '1 day' - salescall.jst(now())) AS left"))
        now, t = clock["now"], clock["t"]
        slot = min(3600, int(clock["left"]) - 120)            # 試験の回が日をまたがない長さ（夜中に流しても同じ確かめ）
        if slot < 1800:
            self.skipTest("日本時間の0時の直前：回の時間が短すぎる")
        self.put_client(s, "callback.exact_slot", slot)
        target = next_action.Target(s["contact"])
        first = self.tx(s, lambda tx: next_action._keep_promise(
            tx, s["task"], t, next_action._exact_window(tx, s["task"], t), target, now=now, exact=True))
        self.assertEqual((first.kind, first.promised, first.callee_at, first.window.round), ("call", True, t, 0))
        self.tx(s, lambda tx: next_action._write(tx, s["task"], first))
        due_now = lambda: self.tx(s, lambda tx: support.schedule_call(   # noqa: E731
            tx, s["task"], now, contact_id=s["contact"], cause="試験：次の1回をいまかける", promised=True, callee_at=t,
            window=next_action.window_of(next_action.current(tx, s["task"]))))
        claimed = lambda aid: self.tx(s, lambda tx: tx.one(   # noqa: E731
            "SELECT claimed_at FROM salescall.attempts WHERE attempt_id = %s", (aid,))["claimed_at"])
        for gap in (300, 600, 1200):                           # 前の電話から5分後・さらに10分後・さらに20分後
            aid = self.call(s, ["dialing", "no_answer"])
            self.settle(s, aid)
            cur = self.tx(s, lambda tx: next_action.current(tx, s["task"]))
            self.assertLess(abs((cur["call_at"] - (claimed(aid) + timedelta(seconds=gap))).total_seconds()), 2)
            self.assertEqual((cur["promised"], cur["callee_at"], cur["window_round"]), (True, t, 0))
            due_now()
        self.settle(s, self.call(s, ["dialing", "no_answer"]))   # 4本目にも出ない
        cur = self.tx(s, lambda tx: next_action.current(tx, s["task"]))
        self.assertEqual((cur["call_at"], cur["callee_at"], cur["window_round"], cur["apology_owed"]),
                         (t + timedelta(days=1), t + timedelta(days=1), 1, False))   # 4本で次の日の同じ時刻（かけたのでお詫びは無い）
        denied = self.tx(s, lambda tx: next_action.after_denial(   # 門で止められた（時間帯の外など）：時刻ちょうどの約束のまま
            tx, s["task"], gate.Denied("outside_hours", *gate.REASONS["outside_hours"]),
            tenant_id=s["tenant"], client_id=s["client"], business_id=s["business"]))
        self.assertEqual((denied.callee_at, denied.window.round), (t + timedelta(days=1), 1))   # 約束の時刻を捨てない（見直し7）
        second = next_action.window_of(cur)
        third = self.tx(s, lambda tx: next_action._keep_promise(tx, s["task"], second.until, second, target, now=now, exact=True))
        self.assertEqual((third.kind, third.at, third.callee_at, third.window.round, third.apology),
                         ("call", t + timedelta(days=2), t + timedelta(days=2), 2, True))   # 2日目に話せなければ3日目（その日にかけていない）
        skipped = self.tx(s, lambda tx: next_action._keep_promise(   # 見守り役が止まって2日目を飛ばした：その前の回にはかけていても、
            tx, s["task"], first.window.until, first.window, target, now=t + timedelta(days=2, minutes=1), exact=True))
        self.assertEqual((skipped.window.round, skipped.apology), (2, True))   # 飛んだ回があればお詫びから
        last = replace(second, round=2)
        over = self.tx(s, lambda tx: next_action._keep_promise(tx, s["task"], last.until, last, target, now=now, exact=True))
        self.assertEqual((over.kind, over.end_reason), ("end", "callback_exact_unreached"))   # 3日目にも話せなければ打ち止め

    def test_an_exact_time_promise_in_the_ng_hours_keeps_its_time(self):
        """お客様がNG時間の中の時刻（「12時に」）を指定した約束：回の中のかけられる時刻を、NG時間を引かずに探す（お客様が指定した
        時刻はNG時間でもかけてよい＝2026-10-15 ユーザー。⚠️ 引いて探すと「かけられる時刻が無い」と読んだ＝2026-10-19 の見直し）。"""
        s = self.ready()
        self.put_client(s, "calling.ng_hours", {k: [["00:00", "24:00"]] for k in KEYS})   # どの時刻もNG時間
        t = self.tx(s, lambda tx: tx.one("SELECT date_trunc('minute', now()) + interval '2 hours' AS t")["t"])
        now = self.tx(s, lambda tx: tx.one("SELECT now() AS t")["t"])
        d = self.tx(s, lambda tx: next_action._keep_promise(
            tx, s["task"], t, next_action._exact_window(tx, s["task"], t), next_action.Target(s["contact"]), now=now, exact=True))
        self.assertEqual((d.kind, d.at, d.callee_at), ("call", t, t))

    def test_an_exact_promise_retry_keeps_out_of_another_ng_frame(self):
        """時刻ちょうどの約束の回のかけ直しは、約束の時刻を含むNG時間の枠だけNG時間でもかけてよい。ほかのNG時間の枠は控える
        （門の `calling_hours.allowed` と同じ決まり＝`excuse_ng` の1か所。⚠️ 前は次の動きがNG時間を全部無視し、門が止め続けた）。"""
        from salescall.core.calendar import holiday_checker
        s = self.ready()
        clock = self.tx(s, lambda tx: tx.one(
            "SELECT now() AS now, date_trunc('minute', now()) + interval '10 minutes' AS t,"
            " salescall.jst(date_trunc('minute', now()) + interval '10 minutes') AS t_local"))
        now, t, t_local = clock["now"], clock["t"], clock["t_local"]
        if t_local.hour == 23:
            self.skipTest("日本時間の23時台：試験の回が日をまたぐ")
        hhmm = lambda d: (t_local + d).strftime("%H:%M")   # noqa: E731
        self.put_client(s, "calling.ng_hours", {k: [[hhmm(timedelta(minutes=3)), hhmm(timedelta(minutes=50))]] for k in KEYS})
        w = self.tx(s, lambda tx: next_action._exact_window(tx, s["task"], t))
        d = self.tx(s, lambda tx: next_action._keep_promise(tx, s["task"], t + timedelta(minutes=5), w,
                                                            next_action.Target(s["contact"]), now=now, exact=True))
        self.assertEqual(d.at, t + timedelta(minutes=50))                     # ほかの枠の終わりまで待つ
        allowed = lambda m: self.tx(s, lambda tx: calling_hours.allowed(   # noqa: E731
            {k: [["00:00", "24:00"]] for k in KEYS},
            {k: [[hhmm(timedelta(minutes=3)), hhmm(timedelta(minutes=50))]] for k in KEYS}, holiday_checker(tx),
            t_local + timedelta(minutes=m), t_local + timedelta(minutes=m, seconds=60), callee_at=t_local))
        self.assertEqual((allowed(5), allowed(50)), (False, True))           # 門も同じ答え

    def test_a_window_retry_past_the_round_moves_to_the_next_round_without_an_apology(self):
        """幅の約束で出なかったとき、再発信の方針の間隔（「2日後」）が回の終わりを越えるなら、次の回へ（回を飛ばさない。その回には
        かけたので、お詫びの印は付けない。⚠️ 前は回を2つ飛ばし、お詫びの印も付けた＝2026-10-19 の見直し）。"""
        keys = ("open", "incomplete", "not_reached_absent", "not_reached_not_transferred", "callback_unspecified_time")
        s = self.ready(rules=rules_raw(retry={k: {"action": "call", "max_times": 3, "interval": {"days": 2}} for k in keys}))
        local = self.local_now(s)
        midnight = lambda day: self.from_jst(s, datetime.combine(day, datetime.min.time()))   # noqa: E731
        w = next_action.Window(midnight(local.date() + timedelta(days=1)), (0, 24 * 3600), None, midnight(local.date()), "day")
        now = self.tx(s, lambda tx: tx.one("SELECT now() AS t")["t"])
        self.tx(s, lambda tx: support.schedule_call(tx, s["task"], now, contact_id=s["contact"], cause="試験", promised=True,
                                                       window=w))
        self.settle(s, self.call(s, ["dialing", "no_answer"]))
        cur = self.tx(s, lambda tx: next_action.current(tx, s["task"]))
        self.assertEqual((cur["promised"], cur["window_round"], cur["window_from"], cur["apology_owed"]),
                         (True, 1, midnight(local.date() + timedelta(days=1)), False))

    def test_a_window_retry_within_the_round_counts_up_to_the_policy_times(self):
        """幅の約束で出なかったとき、再発信の方針の回数（ここでは1回）の中なら、同じ回の中で方針の間隔でかけ直す（境目。見直し7で
        試験を足した）。"""
        keys = ("open", "incomplete", "not_reached_absent", "not_reached_not_transferred", "callback_unspecified_time")
        s = self.ready(rules=rules_raw(retry={k: {"action": "call", "max_times": 1, "interval": {"hours": 1}} for k in keys}))
        now = self.tx(s, lambda tx: tx.one("SELECT now() AS t")["t"])
        w = next_action.Window(now + timedelta(days=2), (0, 24 * 3600), None, now - timedelta(hours=1), "week")
        self.tx(s, lambda tx: support.schedule_call(tx, s["task"], now, contact_id=s["contact"], cause="試験", promised=True,
                                                       window=w))
        self.settle(s, self.call(s, ["dialing", "no_answer"]))
        cur = self.tx(s, lambda tx: next_action.current(tx, s["task"]))
        self.assertEqual((cur["promised"], cur["window_round"]), (True, 0))   # 1回目：同じ回の中
        self.assertLess(cur["call_at"], now + timedelta(hours=2))

    def test_an_untimed_promise_ends_when_later_is_said_more_than_the_policy_times(self):
        """「あとで」と言われた回数が方針の回数（ここでは1回）を超えたら終える。言われた回数そのものは続ける（境目。見直し7で試験を
        足した）。"""
        keys = ("open", "incomplete", "not_reached_absent", "not_reached_not_transferred", "callback_unspecified_time")
        s = self.ready(rules=rules_raw(retry={k: {"action": "call", "max_times": 1, "interval": {"hours": 1}} for k in keys}))
        later = lambda: self.settle(s, self.call(s, report=rep(callback=callback("later", time="あとで"))))   # noqa: E731
        first = later()["decision"]
        self.assertEqual((first.kind, first.promised), ("call", True))           # 1回目は続ける
        now = self.tx(s, lambda tx: tx.one("SELECT now() AS t")["t"])
        self.tx(s, lambda tx: support.schedule_call(tx, s["task"], now, contact_id=s["contact"], cause="試験：いまかける",
                                                       promised=True))
        d = later()["decision"]
        self.assertEqual((d.kind, d.end_reason), ("end", "callback_unspecified_time"))   # 2回目は方針の回数を超える

    def test_an_untimed_promise_stays_a_promise_when_not_answered(self):
        """時刻の決まっていない約束（「あとで」）の電話に出なかった：約束したかけ直しのまま（行の回数の上限で終えない）、契約者の
        方針（callback_unspecified_time）の回数で終える（⚠️ 前は、約束でないふつうのかけ直しになり、回数の上限で終わった）。"""
        s = self.ready()
        self.put_client(s, "retry.max_attempts_per_task", 1)
        aid = self.call(s, report=rep(callback=callback("later", time="あとで")))
        self.assertEqual(self.settle(s, aid)["state"], "callback_scheduled")
        now = self.tx(s, lambda tx: tx.one("SELECT now() AS t")["t"])
        kinds = []
        for _ in range(3):                                                   # 試験の方針：3回まで
            self.tx(s, lambda tx: support.schedule_call(tx, s["task"], now, contact_id=s["contact"],
                                                           cause="試験：いまかける", promised=True))
            d = self.settle(s, self.call(s, ["dialing", "no_answer"]))["decision"]
            kinds.append((d.kind, d.promised, d.end_reason))
        self.assertEqual(kinds, [("call", True, None), ("call", True, None), ("end", None, "callback_unspecified_time")])

    def test_calls_on_the_promised_day_are_not_counted_after_the_day_passed(self):
        """理由の分からない日だけの約束（「13日なら」）の日にかけて出なかった電話は、日が過ぎて時刻の決まっていない約束に移ったあとの
        回数に数えない（その方針の回数は、移ってからの電話で数える。⚠️ 前は約束の日の分まで数え、移ったとたん1回もかけずに終えた＝
        見直し5）。"""
        s = self.ready()
        now = self.tx(s, lambda tx: tx.one("SELECT now() AS t")["t"])
        today = next_action.Window(now + timedelta(hours=6), None, None, now - timedelta(hours=6), None)
        for _ in range(3):                                                   # 試験の方針の回数（3）だけ、その日に出なかった
            self.tx(s, lambda tx: support.schedule_call(tx, s["task"], now, contact_id=s["contact"], cause="試験",
                                                        promised=True, window=today))
            self.settle(s, self.call(s, ["dialing", "no_answer"]))
        d = self.tx(s, lambda tx: next_action._window_over(tx, s["task"], now, next_action.Target(s["contact"]), today))
        self.assertEqual((d.kind, d.promised), ("call", True))
        self.assertEqual(self.tx(s, lambda tx: next_action._unspecified_calls(tx, s["task"])), 0)

    def test_a_promise_kept_without_a_round_end_survives_no_answer(self):
        """終わりの無い幅のまま書いた約束（かけてよい時間帯が無いあいだ・暦の外で、幅をそのまま保った予定）の電話に出なかった：
        締めが落ちず、約束のまま（⚠️ 前は回の終わりの無い幅と比べて落ちた＝見直し5）。"""
        s = self.ready()
        now = self.tx(s, lambda tx: tx.one("SELECT now() AS t")["t"])
        self.tx(s, lambda tx: support.schedule_call(tx, s["task"], now, contact_id=s["contact"], cause="試験", promised=True,
                                                    window=next_action.Window(None, None, tuple(KEYS), None, None)))
        d = self.settle(s, self.call(s, ["dialing", "no_answer"]))["decision"]
        self.assertEqual((d.kind, d.promised), ("call", True))
        self.assertIsNotNone(d.window)

    def test_minutes_later_is_called_as_soon_as_possible_not_as_an_exact_time(self):
        """「30分後」：その時からの「すぐ」（時刻ちょうどの約束ではない＝お客様が指定した時刻の印 callee_at を持たない。2026-10-19
        ユーザー：「いまは忙しいけど、できるだけ早くかけ直してということが多い」）。"""
        s = self.ready()
        aid = self.call(s, report=rep(callback=callback("later", time="30分後")))
        self.settle(s, aid)
        cur = self.tx(s, lambda tx: next_action.current(tx, s["task"]))
        ended = self.tx(s, lambda tx: tx.one("SELECT least(ended_at, (SELECT saved_at FROM salescall.call_reports"
                                             " WHERE attempt_id = %s)) AS t FROM salescall.attempts WHERE attempt_id = %s",
                                             (aid, aid))["t"])
        self.assertEqual((cur["promised"], cur["callee_at"], cur["window_daily"]), (True, None, None))
        self.assertGreaterEqual(cur["call_at"], ended + timedelta(minutes=30) - timedelta(seconds=1))

    def test_a_monthly_reason_moves_to_the_same_day_next_month(self):
        """月の理由（月ぎめ・入金の間隔）で選んだ日は、話せなければ次の月の同じ日の回（2026-10-19 ユーザー）。"""
        s = self.ready()
        local = self.local_now(s)
        first = (local.date().replace(day=1) + timedelta(days=32)).replace(day=3)
        nxt = (first.replace(day=1) + timedelta(days=32)).replace(day=3)
        midnight = lambda day: self.from_jst(s, datetime.combine(day, datetime.min.time()))   # noqa: E731
        w = next_action.Window(midnight(first + timedelta(days=1)), None, None, midnight(first), "month:day:3")
        now = self.tx(s, lambda tx: tx.one("SELECT now() AS t")["t"])
        d = self.tx(s, lambda tx: next_action._keep_promise(tx, s["task"], w.until, w, next_action.Target(s["contact"]),
                                                            now=now))
        self.assertEqual((d.kind, d.window.round, d.window.since, d.at), ("call", 1, midnight(nxt), midnight(nxt)))

    def test_exact_promise_counts_cannot_be_zero(self):
        """時刻ちょうどの約束の時間・日数・かけ直しの間は1以上（0なら約束を一度もかけずに終えてしまう。データベースが守る＝0093）。"""
        s = self.ready()
        for key, value in (("callback.exact_slot", 0), ("callback.exact_days", 0), ("callback.exact_retry_gaps", [300, 0]),
                           ("callback.exact_retry_gaps", [])):
            with self.subTest(key=key, value=value), self.assertRaises(Rejected):
                self.put_client(s, key, value)

    def test_the_call_that_made_the_promise_is_not_a_call_in_its_round(self):
        """約束の回にかけたか（お詫びの印）は、その回の予定を決めたあとの電話で見る（「今日の夕方」と約束した通話そのものを、
        その日にかけた電話に数えない）。回線の側でかけられなかった電話も数えない。"""
        s = self.ready()
        aid = self.call(s)
        self.settle(s, aid)
        now = self.tx(s, lambda tx: tx.one("SELECT now() AS t")["t"])
        today = next_action.Window(now + timedelta(hours=6), None, None, now - timedelta(hours=6), "day")
        self.tx(s, lambda tx: support.schedule_call(tx, s["task"], now, contact_id=s["contact"], cause="試験", promised=True,
                                                       window=today))
        missed = lambda: self.tx(s, lambda tx: next_action._missed(tx, s["task"], today))   # noqa: E731
        self.assertTrue(missed())                                             # 約束をした通話のほかに、かけていない
        self.settle(s, self.call(s, ["dialing", "call_failed"]))
        self.tx(s, lambda tx: support.schedule_call(tx, s["task"], now, contact_id=s["contact"], cause="試験", promised=True,
                                                       window=today))
        self.assertTrue(missed())                                             # 回線の側でかけられなかった
        self.settle(s, self.call(s, ["dialing", "no_answer"]))
        self.assertFalse(missed())                                            # かけた（出なかった）

    def test_an_open_window_is_written_without_a_round(self):
        """終わりの無い幅（「平日なら」）は、回が決まるまで、次の回の決め方を持たない（表の作りの約束。⚠️ 前は暦の外に当たると、
        回の無い幅に決め方を付けて書こうとし、データベースに断られた＝2026-10-19 の見直し）。"""
        s = self.ready()
        now = self.tx(s, lambda tx: tx.one("SELECT now() AS t")["t"])
        self.tx(s, lambda tx: support.schedule_call(tx, s["task"], now, contact_id=s["contact"], cause="試験", promised=True,
                                                       window=next_action.Window(None, None, ("mon",), None, "day")))
        cur = self.tx(s, lambda tx: next_action.current(tx, s["task"]))
        self.assertEqual((cur["window_days"], cur["window_repeat"], cur["window_from"]), (["mon"], None, None))

    def test_a_missed_promise_day_is_apologised_for_until_the_callee_is_reached(self):
        """約束の回に一度もかけられなかった：次の予定にお詫びの印。門の予約証が会話の処理へ渡し（missed_promise）、出なかった
        通話のあとも引き継ぐ。目的のお客様と話せた締めで外れる。"""
        s = self.ready()
        now = self.tx(s, lambda tx: tx.one("SELECT now() AS t")["t"])
        past = next_action.Window(now - timedelta(days=1), None, None, now - timedelta(days=2), "day")
        self.tx(s, lambda tx: support.schedule_call(tx, s["task"], now - timedelta(days=2), contact_id=s["contact"],
                                                       cause="試験", promised=True, window=past))
        denied = self.claim(s)
        self.assertEqual(getattr(denied, "reason", None), "callback_window")
        d = self.tx(s, lambda tx: next_action.after_denial(tx, s["task"], denied, tenant_id=s["tenant"], client_id=s["client"],
                                                           business_id=s["business"]))
        self.assertEqual((d.kind, d.promised, d.window.round, d.apology), ("call", True, 1, True))
        owed = lambda: self.tx(s, lambda tx: next_action.current(tx, s["task"]))["apology_owed"]   # noqa: E731
        c = self.claim(s)
        self.assertTrue(c.missed_promise)
        self.tx(s, lambda tx: attempts.release(tx, [c.attempt_id]))
        self.settle(s, self.call(s, ["dialing", "no_answer"]))
        self.assertTrue(owed())                                               # 出なかった：お詫びはまだ
        cur = self.tx(s, lambda tx: next_action.current(tx, s["task"]))
        self.tx(s, lambda tx: support.schedule_call(tx, s["task"], cur["call_at"] - timedelta(hours=2),
                                                       contact_id=s["contact"], cause="試験：いまかける", promised=True,
                                                       window=next_action.window_of(cur)))   # 印は引き継ぐ
        c = self.claim(s)
        self.assertTrue(c.missed_promise)
        self.tx(s, lambda tx: attempts.release(tx, [c.attempt_id]))
        self.settle(s, self.call(s))                                          # 目的のお客様と話せた
        self.assertFalse(owed())

    def test_a_wrong_repeat_cannot_be_written(self):
        """次の回の決め方は、決まった書き方だけ（データベースが守る＝0089）。"""
        s = self.ready()
        now = self.tx(s, lambda tx: tx.one("SELECT now() AS t")["t"])
        for repeat in ("year", "month:day:32", "month:"):
            with self.subTest(repeat=repeat), self.assertRaises(Rejected):
                self.tx(s, lambda tx: support.schedule_call(
                    tx, s["task"], now, contact_id=s["contact"], cause="試験", promised=True,
                    window=next_action.Window(now + timedelta(days=1), None, None, now, repeat)))

    def test_after_no_answer_the_next_call_keeps_the_window(self):
        """幅の中でかけて出なかった：次のかけ直し（契約者の再発信の方針の間隔）も、同じ幅の中で、約束したかけ直しのまま
        （2026-10-19 ユーザー：約束の電話は優先枠・話せなかったら理由に合わせて次の回。前はふつうのかけ直しにしていた）。"""
        s = self.ready()
        local = self.local_now(s)
        until = self.from_jst(s, datetime.combine(local.date() + timedelta(days=5), datetime.min.time()))
        now = self.tx(s, lambda tx: tx.one("SELECT now() AS t")["t"])
        self.tx(s, lambda tx: support.schedule_call(
            tx, s["task"], now, contact_id=s["contact"], cause="試験", promised=True,
            window=next_action.Window(until, (0, 24 * 3600))))
        aid = self.call(s, ["dialing", "no_answer"])
        self.settle(s, aid)
        nxt = self.latest(s)
        self.assertEqual((nxt["kind"], nxt["promised"], nxt["window_daily"]), ("call", True, [0, 24 * 3600]))
        self.assertLess(nxt["at"], self.tx(s, lambda tx: tx.one("SELECT salescall.jst(%s) AS t", (until,))["t"]))


class Voicemail(SettleBase):
    def test_a_voicemail_message_is_left_only_once_per_number_of_the_client(self):
        """留守番電話の伝言は最初の1回だけ（2026-10-19 ユーザー）。数える単位は、同じ依頼者のすべての一覧の同じ番号（依頼者を
        またいでは数えない）。会話の処理は、伝言を残す前に call_report.voicemail_left を読む。"""
        s = self.ready()
        left = lambda x, phone: self.tx(x, lambda tx: call_report.voicemail_left(tx, phone))   # noqa: E731
        self.assertFalse(left(s, s["phone"]))
        message = replace(rep(("voicemail",)), voicemail_messages=(call_report.Said(0, ""),))
        self.settle(s, self.call(s, report=message))
        self.assertTrue(left(s, s["phone"]))
        other = self.ready(tenant=s["tenant"])                                 # ほかの依頼者：数えない
        self.assertFalse(left(other, s["phone"]))
        silent = self.ready()
        self.settle(silent, self.call(silent, report=rep(("voicemail",))))     # 伝言を残さずに切った通話は数えない
        self.assertFalse(left(silent, silent["phone"]))


class RecordingHolds(SettleBase):
    """検証モードの通話と、誤判定率の抜き取りの通話の録音は、測り終わるまで消さない（2026-10-17 ユーザー 案A。Intent.md 11-4 第2版）。"""

    def test_verification_calls_keep_their_recordings_until_measured(self):
        s = self.ready()
        normal = self.call(s, items=[("Q1", "confirmed", None)])
        self.settle(s, normal)
        t = self.ready()
        self.put_client(t, "approval.human", True)                     # 検証モードでかける（印はかけたときのモードで付く）
        self.tx(t, lambda tx: approval.approve_task(tx, t["task"]), OPERATOR)
        self.tx(t, lambda tx: quality_stage.open_stage(tx, t["version"], 1, 1, evidence="試験：最初の段は1件"), OPERATOR)
        checked = self.call(t, items=[("Q1", "confirmed", None)])
        self.settle(t, checked)
        held = lambda x: self.tx(x, lambda tx: [r["reason"] for r in tx.all(   # noqa: E731
            "SELECT reason FROM salescall.recording_holds WHERE released_at IS NULL ORDER BY reason")])
        self.assertEqual((held(s), held(t)), ([], ["verification"]))
        self.tx(s, lambda tx: recordings.hold(tx, normal, "sample"))    # 誤判定率の抜き取り（抜き取る処理が付ける）

        root = Path(tempfile.mkdtemp(prefix="salescall-test-rec-"))
        (root / backup.DRIVE_MARK).write_text("控え用")
        drive = backup.Drive(root)
        long_ago = datetime.now().astimezone() - timedelta(days=200)    # 2か月（60日）より古い
        files = {}
        for x, aid in ((s, normal), (t, checked), (s, uuid.uuid4())):
            d = drive.folder(backup.client_folder(self.w.a, x["tenant"], x["client"]), backup.RECORDING).path
            d.mkdir(parents=True, exist_ok=True)
            files[aid] = d / backup.recording_file_name(aid, long_ago, "call")
            files[aid].write_bytes(b"x")
        for x in (s, t):                                                 # 片付けは契約者ごと（その契約者の PC）
            backup.prune(self.w.a, drive, x["tenant"])
        self.assertEqual([f.exists() for f in files.values()], [True, True, False])   # 印の無い古い録音だけ消す

        self.assertEqual(self.tx(s, lambda tx: recordings.release(tx, [normal], "sample", measurement="試験の誤判定率 第1回")), 1)
        self.tx(t, lambda tx: recordings.release(tx, [checked], "verification", measurement="試験の検証の期間"))
        with self.assertRaises(Rejected):                                # 外した印は戻せない
            self.tx(t, lambda tx: tx.execute("UPDATE salescall.recording_holds SET released_at = NULL, released_for = NULL"))
        for x in (s, t):                                                 # 片付けは契約者ごと（その契約者の PC）
            backup.prune(self.w.a, drive, x["tenant"])
        self.assertEqual([f.exists() for f in files.values()], [False, False, False])  # 測り終わったら、ふつうの決まり


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
