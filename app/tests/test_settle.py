"""締めの処理の試験（1回の発信のあと：判定 → 結果の版・切れ方・発信禁止・次の動きを1つの取引で）。DESIGN.md 7章。"""

from __future__ import annotations

import tempfile
import time
import unittest
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from salescall.core import (approval, attempts, backup, business_rules, calling_hours, call_report, dnc, fix_requests, gate,
                            next_action, quality_stage, recordings, results, scheduler, settings, settle, stops, target_recheck, tasks,
                            watch)
from salescall.core.errors import ErrorReport
from salescall.core import health
from salescall.core.access import Rejected
from salescall.core.call_report import Cut
from salescall.core.phone import normalize
from salescall.link import phone
from tests import support
from tests.support import (ADMIN, CLEAN, OPERATOR, SYSTEM, app_access, callback, fictional_jp_fixed, make_report, owner_access,
                           rules_raw)
from tests.test_gate import HOLDER, KEYS, Base, FakeDriver, dial_exit

TALK = ["dialing", "answered", "in_conversation", "ending", "ended"]


def rep(parties=("target",), *, dnc_=False, callback=None, cut=Cut("ai", True, False), not_reached=None):
    return make_report(parties, dnc=dnc_, callback=callback, cut=cut, not_reached=not_reached)


class SettleBase(Base):
    """締めの試験の土台（試験は持たない）。"""
    def call(self, s, path=TALK, *, report=None, items=(), verify="done", side=None):
        """本物の門・出口を通して1回かけ、path まで進め、項目と報告を置く（締めはまだ）。"""
        c = self.claim(s)
        self.assertIsInstance(c, gate.Claim, c)
        self.assertTrue(dial_exit.dial(self.w.a, SYSTEM, s["tenant"], s["client"], c.attempt_id, FakeDriver()))
        for st in path[1:]:
            self.tx(s, lambda tx: attempts.move(tx, c.attempt_id, st, hangup_side=side if st == "dropped" else None))
        for q, state, cond in items:
            self.tx(s, lambda tx: tx.execute(
                "INSERT INTO salescall.item_values (tenant_id, client_id, task_id, version_id, question_id, state, condition,"
                " attempt_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (s["tenant"], s["client"], s["task"], s["version"], q, state, cond, c.attempt_id)))
        if "answered" in path:
            self.tx(s, lambda tx: call_report.save(tx, c.attempt_id, report or rep()))
            if verify:
                self.tx(s, lambda tx: attempts.record_verification(tx, c.attempt_id, summary=CLEAN))
        self.tx(s, lambda tx: health.after_call(tx, c.attempt_id))   # 電話が終わった所（回線ごとの処理）と同じく（終わっていなければ何もしない）
        return c.attempt_id

    def settle(self, s, aid, *, decide=True):
        """本物の締めの処理で締める。鳴らなかった電話は、だれのせいかが決まるまで締めない（行の欄が判定待ち）ので、decide なら、まだ分からない
        電話は、その回線のテスト番号が鳴った＝番号のせい、と決めてから締める（決め方そのものを試さない試験の近道。発信の回数には
        数えない）。決め方や、決まるまで休むことを試す試験は decide=False。"""
        self.tx(s, lambda tx: health.after_call(tx, aid))   # 電話が終わった所と同じく、出来事を状態の表の入口へ（1回だけ）
        if decide and self.tx(s, lambda tx: health.blame_of(tx, aid)) == "pending":
            line = self.tx(s, lambda tx: tx.one("SELECT line_id FROM salescall.attempts WHERE attempt_id = %s", (aid,)))["line_id"]
            self.w.test_number_rang(s, line=line)
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

    def test_a_call_our_own_program_ended_is_made_again_as_planned_and_counts_nowhere(self):
        """こちらのせい（こちらのプログラムが固まった・甚大な問題で終えた・通話を進める部品が落ちた＝試行の trouble）でお客様のところで
        鳴らなかった電話は、番号にも回線にも数えない（試行のせいの欄の ours。0136）。次の動きは1か所（next_action._line_fault_again）
        で、かけた予定のまま、復旧次第（⛔ 見直し19a：前は回線のせい・番号のせいの続きに入り、こちらの不具合で番号を取りやめうる・回線を
        止めうる形だった）。復旧＝同じ契約者で、こちらの不具合なしにお客様と話し終えた電話がある・直したプログラムの版に替わった（それまで
        門と候補を出す処理が待たせる＝行の欄の「復旧待ち」。17章の R-5・R-6。2026-10-17 管理者：「復旧次第、お詫びとともにすぐかけ直さ
        ないといけないでしょ！」）。"""
        s = self.ready()
        aid = self.call(s, ["dialing", "ringing"])
        self.assertTrue(self.tx(s, lambda tx: attempts.end_for_trouble(tx, aid, "runner_error")))
        self.tx(s, lambda tx: health.after_call(tx, aid))                      # 電話が終わった所（回線ごとの処理）と同じく
        self.assertEqual(self.tx(s, lambda tx: health.blame_of(tx, aid)), "ours")
        d = self.settle(s, aid)["decision"]
        self.assertEqual((d.kind, d.target.purpose), ("call", "normal"))
        self.assertIn("こちらのせい", d.rule)
        self.assertEqual(self.reason(s), "awaiting_recovery")                 # 復旧するまで、かけない
        self.assertNotIn(s["task"], [r["task_id"] for r in self.tx(s, lambda tx: scheduler.due_tasks(tx, 10))])   # 候補にも出さない
        self.assertEqual(self.tx(s, lambda tx: support.number_run(tx, s["contact"]))["n"], 0)       # 番号に数えない
        self.assertFalse(self.tx(s, lambda tx: support.line_checking(tx, s["line"])))           # 回線の続きにも入らない
        self.w.talked(s)                                                       # 同じ契約者で、こちらの不具合なしに話し終えた＝復旧
        with self.assertRaises(ValueError):
            self.tx(s, lambda tx: attempts.end_for_trouble(tx, aid, "who_knows"))
        # 同じ行で、こちらのせいが続いても、どれもこちらのせい（数えない）。数えないまま鳴らし続けない歯止めは、行の休み（同じ行で続けて
        # 2回で休む＝行の欄の R-8。0139。⛔ 0136：3回目からはふつうの鳴らなかった電話として番号に数えた。⛔ 見直しの一生の
        # 試験 1o：数えないと5日で 270 回かけ続けた）
        self.tx(s, lambda tx: next_action.schedule_now(tx, s["task"], cause="試験：その時刻が来た"))
        again = self.call(s, ["dialing", "ringing"])
        self.tx(s, lambda tx: attempts.end_for_trouble(tx, again, "frozen"))
        self.tx(s, lambda tx: health.after_call(tx, again))
        self.assertEqual(self.tx(s, lambda tx: health.blame_of(tx, again)), "ours")
        self.settle(s, again)
        self.assertEqual(self.tx(s, lambda tx: support.number_run(tx, s["contact"]))["n"], 0)
        self.tx(s, lambda tx: next_action.schedule_now(tx, s["task"], cause="試験：その時刻が来た"))
        self.assertEqual(self.reason(s), "task_resting")                     # 2回続いた：その行は休む

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

    def test_hangup_after_refusal_words_is_refusal_and_line_cut_is_not(self):
        """2026-10-30 管理者の指摘で書き替えた：切れ方で「断った」になるのは、営業拒否の言葉のあとに切れたときだけ（発信禁止・行を終える）。
        回線の側で切れた通話は途中で切れた電話（数えない）。営業拒否の言葉の無い切れ方は UnclearCutChecks。"""
        s = self.ready()
        aid = self.call(s, ["dialing", "answered", "dropped"], report=rep(cut=Cut("ai", False, True)), side="callee")
        self.assertEqual(self.settle(s, aid)["state"], "do_not_call")
        self.assertEqual(self.tx(s, lambda tx: tx.one("SELECT reason::text AS r FROM salescall.dnc_entries WHERE source_attempt_id = %s",
                                                      (aid,)))["r"], "refused_in_call")
        self.assertIsNone(self.tx(s, lambda tx: tx.one("SELECT 1 FROM salescall.unclear_checks WHERE task_id = %s", (s["task"],))))
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

    def test_an_exact_time_on_a_named_day_is_called_even_on_the_clients_day_off(self):
        """「（土曜）の15時」：お客様がその日を言った時刻ちょうどの約束は、依頼者の休みの日でもかける（最初の回に「その日を言った」を
        持たせる＝next_action._exact_window の1か所。見直し16b：この場面の試験が無かった）。"""
        s = self.ready()
        day = self.tx(s, lambda tx: tx.one(
            "SELECT (salescall.jst(now())::date + ((6 - extract(isodow FROM salescall.jst(now()))::int + 7) % 7 + 7)) AS d")["d"])
        self.assertEqual(day.isoweekday(), 6)                                  # 次の次の土曜（依頼者の初めの値は平日だけ）
        got = self.settle(s, self.call(s, report=rep(callback=callback("later", time=f"{day.month}月{day.day}日の15時"))))
        nxt = self.tx(s, lambda tx: next_action.current(tx, s["task"]))
        self.assertEqual((got["state"], nxt["window_day_said"]), ("callback_scheduled", True))
        hours = self.tx(s, lambda tx: gate.window_hours(tx, tenant_id=s["tenant"], client_id=s["client"], business_id=s["business"],
                                                        window=calling_hours.callee_window(nxt)))
        self.assertTrue(any(hours.values()))                                    # 土曜でも、その時刻はかけてよい

    def test_calls_in_a_promised_round_are_counted_like_the_policy(self):
        """約束の回にかけた数は、比べる相手の再発信の方針の回数と同じ数え方（お客様に届いた1回＝attempts.reached）で数える（発信に
        失敗・お客様が出る前の回線の失敗は数えない）。回の始まりが分からない幅は、方針の回数をそのまま使う（next_action._round_calls
        の1か所。見直し16b：前は鳴った電話で数え、方針の回数と数え方が混ざっていた。回の始まりが分からないときの試験も無かった）。"""
        s = self.ready()
        since = self.tx(s, lambda tx: tx.one("SELECT now() - interval '1 minute' AS t")["t"])
        for path in (["dialing", "no_answer"], ["dialing", "call_failed"], ["dialing", "ringing", "technical_error"]):
            self.past_attempt(s, path)
        w = next_action.Window(None, None, None, since, None)
        self.assertEqual(self.tx(s, lambda tx: next_action._round_calls(tx, s["task"], w, 9)), 1)
        self.assertEqual(self.tx(s, lambda tx: next_action._round_calls(tx, s["task"], replace(w, since=None), 2)), 2)

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
        v2 = self.w.add_version(s["tenant"], s["client"], s["business"])
        self.tx(s, lambda tx: tx.execute(
            "INSERT INTO salescall.run_versions (tenant_id, client_id, run_id, business_id, seq, version_id, assigned_by, reason)"
            " VALUES (%s, %s, %s, %s, 2, %s, '試験', '決まりの無い版')", (s["tenant"], s["client"], s["run"], s["business"], v2)),
            OPERATOR)
        self.tx(s, lambda tx: quality_stage.open_stage(tx, v2, 1, None, evidence="試験"), OPERATOR)
        self.assertEqual(self.reason(s), "rules_missing")

    def test_missing_rules_are_not_guessed(self):
        """業務の決まりが無い版でかけた発信は、締めない（既定値で決めない）。何も書かない。"""
        s = self.ready()
        v2 = self.w.add_version(s["tenant"], s["client"], s["business"])
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


class TargetRecheck(SettleBase):
    """目的のお客様に、判定なしで繋いだ通話の確かめ直し（DESIGN.md 6-12 の (e)・7-1e・CL-1 の連絡帳 4-25・0108）。"""

    def connected_report(self, read=("ai",)):
        from salescall.core.call_report import Connect, Said
        return replace(rep(), connects=(Connect(Said(3, "はい、そうです"), tuple(read), True),))

    def connected(self, s, read=("ai",), *, items=(("Q1", "confirmed", None),)):
        return self.call(s, report=self.connected_report(read), items=items, verify=None)

    def item(self, s, q="Q1"):
        return self.tx(s, lambda tx: tx.one("SELECT state::text AS state, condition, value, attempt_id, version_id FROM salescall.item_values"
                                            " WHERE task_id = %s AND question_id = %s", (s["task"], q)))

    def later_call(self, s, report):
        """同じ行の、あとの通話（門を通らずに作る。データベースの守りを試すため）。"""
        aid = self.w.insert_claim(s)
        for st in TALK:
            self.w.move(s, aid, st)
        self.tx(s, lambda tx: call_report.save(tx, aid, report))
        return aid

    def test_another_person_answers_become_dashes_and_the_call_does_not_count_as_reaching_the_target(self):
        """確かめ直すまで通話後の検証を済ませられない。別の方だったら、その通話の答えを「-」にし（前の答えが無いとき。別の方の答えを
        依頼者へ渡さない）、目的のお客様と話せたと数えない（約束の日のお詫びの印を外さない）。結果の中身にも残す。"""
        s = self.ready()
        aid = self.connected(s, items=(("Q1", "confirmed", "はい"),))
        with self.assertRaises(attempts.RecheckMissing):
            self.tx(s, lambda tx: attempts.record_verification(tx, aid, summary=CLEAN))
        with self.assertRaises(target_recheck.RecheckInvalid):                    # 何で確かめたかは必ず要る
            self.tx(s, lambda tx: target_recheck.record(tx, aid, "not_target", evidence=" ", checked_by="試験"))
        self.tx(s, lambda tx: target_recheck.record(tx, aid, "not_target", evidence="試験：別の耳で「いえ、山本です」",
                                                    checked_by="試験"))
        got = self.item(s)
        self.assertEqual((got["state"], got["condition"], got["value"]), ("unresolved", None, None))
        with self.assertRaises(Rejected):                                          # 記録は、表の持ち主でも変えられない
            owner_access().in_system(OPERATOR, lambda tx: tx.execute(
                "UPDATE salescall.target_rechecks SET outcome = 'target' WHERE attempt_id = %s", (aid,)))
        self.tx(s, lambda tx: attempts.record_verification(tx, aid, summary=CLEAN))
        # 目的のお客様に取り次いでもらえなかったのと同じ（「一部だけ取れた」にしない＝判定と締めで「繋がった」を分けない）
        self.assertEqual(self.settle(s, aid)["state"], "not_reached")
        r = self.tx(s, lambda tx: results.latest(tx, s["task"]))
        self.assertEqual((r["detail"].get("target_unconfirmed"), r["detail"].get("not_reached"), r["detail"].get("policy_key"),
                          r["snapshot"]["target_recheck"]), (True, "not_transferred", "not_reached_not_transferred", "not_target"))
        inp = self.tx(s, lambda tx: settle._judge_input(tx, settle._attempt(tx, aid), s["version"])[0])
        self.assertEqual((inp.report.reached(inp.anyone_target), inp.reached_target), (True, False))

    def test_the_target_keeps_the_answers_and_a_later_call_by_another_person_gives_back_the_earlier_answers(self):
        """目的のお客様だったら答えはそのまま。後の通話（判定なしで繋いだ）が上書きした答えは、その通話が別の方だったら、前の通話で
        目的のお客様から取った答えに戻す（後の通話が上書きするとき、前の答えを残す）。前の通話の番号のまま上書きすることはできない
        （戻す答えを取り違えるため）。"""
        s = self.ready()
        aid = self.connected(s, items=(("Q1", "confirmed", "はい"),))
        self.tx(s, lambda tx: target_recheck.record(tx, aid, "target", evidence="試験：録音で「はい、岡田です」", checked_by="試験"))
        self.assertEqual(self.item(s)["state"], "confirmed")
        inp = self.tx(s, lambda tx: settle._judge_input(tx, settle._attempt(tx, aid), s["version"])[0])
        self.assertTrue(inp.reached_target)
        later = self.later_call(s, self.connected_report())
        with self.assertRaises(Rejected):                                          # 前の通話の番号のまま書き換えない
            self.tx(s, lambda tx: tx.execute("UPDATE salescall.item_values SET condition = 'いいえ' WHERE task_id = %s",
                                             (s["task"],)))
        newer = support.full_version(self.w, s)                                  # 後の通話は、新しい業務設定の版で書く
        self.tx(s, lambda tx: tx.execute("UPDATE salescall.item_values SET condition = 'いいえ', attempt_id = %s, version_id = %s"
                                         " WHERE task_id = %s", (later, newer, s["task"])))
        self.tx(s, lambda tx: target_recheck.record(tx, later, "not_target", evidence="試験：別の方", checked_by="試験"))
        got = self.item(s)
        self.assertEqual((got["state"], got["condition"], got["attempt_id"], got["version_id"]),
                         ("confirmed", "はい", aid, s["version"]))                 # 前の答えに戻る（その答えを取った版も）

    def test_the_guard_cannot_be_switched_off_by_a_setting_and_unconfirmed_answers_are_never_given_back(self):
        """答えの守りは、戻す仕掛けの中からの書き込みだけを通す（設定の値を自分で立てても通さない＝2026-10-22 の見直し）。戻すのは、
        目的のお客様だと確かめられなかった通話の答え以外（その通話のあとの通話も確かめられなかったら「-」のまま）。"""
        s = self.ready()
        first = self.connected(s, items=(("Q1", "confirmed", "はい"),))
        self.tx(s, lambda tx: target_recheck.record(tx, first, "not_target", evidence="試験：別の方", checked_by="試験"))
        self.assertEqual((self.item(s)["state"], self.item(s)["attempt_id"]), ("unresolved", first))
        later = self.later_call(s, self.connected_report())

        def sneak(tx):
            tx.execute("SET LOCAL salescall.item_restore = 'x'")             # 前の形の見分け（設定の値）を自分で立てる
            tx.execute("UPDATE salescall.item_values SET state = 'confirmed', condition = 'いいえ' WHERE task_id = %s", (s["task"],))
        with self.assertRaises(Rejected):                                          # 前の通話の番号のまま書き換えない
            self.tx(s, sneak)
        self.tx(s, lambda tx: tx.execute("UPDATE salescall.item_values SET state = 'confirmed', condition = 'いいえ', attempt_id = %s"
                                         " WHERE task_id = %s", (later, s["task"])))
        self.tx(s, lambda tx: target_recheck.record(tx, later, "cannot_check", evidence="試験：録音が無い", checked_by="試験"))
        got = self.item(s)
        self.assertEqual((got["state"], got["condition"], got["attempt_id"]), ("unresolved", None, later))   # 別の方の「-」に戻さない

    def test_a_recheck_is_only_for_calls_connected_without_the_judges_and_a_missing_recording_blanks_the_answers(self):
        """確かめ直しを記録できるのは、要る通話だけ。録音が届かずに済ませた通話は「確かめられなかった」＝答えは「-」で、目的の
        お客様と話せたと数えない。"""
        s = self.ready()
        plain = self.call(s, items=(("Q1", "confirmed", None),), verify=None)
        with self.assertRaises(target_recheck.RecheckInvalid):
            self.tx(s, lambda tx: target_recheck.record(tx, plain, "target", evidence="試験", checked_by="試験"))
        u = self.ready()
        aid = self.connected(u)
        with self.assertRaises(target_recheck.RecheckInvalid):
            self.tx(u, lambda tx: target_recheck.record(tx, aid, "maybe", evidence="試験", checked_by="試験"))
        self.override("verification.recording_wait", 1)
        time.sleep(1.1)
        self.tx(u, lambda tx: attempts.finish_without_recording(tx, aid))
        self.assertEqual((self.tx(u, lambda tx: target_recheck.load(tx, aid)), self.item(u)["state"]),
                         ("cannot_check", "unresolved"))
        inp = self.tx(u, lambda tx: settle._judge_input(tx, settle._attempt(tx, aid), u["version"])[0])
        self.assertFalse(inp.reached_target)

    def test_a_rebuilt_report_with_an_unchecked_connect_is_not_settled_or_rejudged_until_rechecked(self):
        """録音から作り直した報告（判定できない行のため）に判定なしの繋ぎが入ったら、検証が済んでいても、確かめ直しが記録される
        まで締めない・判定し直さない（確かめていない答えで決めない）。"""
        s = self.ready()
        aid = self.call(s, report=rep(cut=None), items=(("Q1", "confirmed", None),))   # 切れ方の材料が無い＝判定できない
        first = self.settle(s, aid)
        self.assertEqual(first["state"], "unknown")
        self.tx(s, lambda tx: call_report.save_rebuilt(tx, aid, self.connected_report(), evidence="試験：録音から"))
        self.assertIsNone(self.tx(s, lambda tx: settle.rejudge(tx, aid)))         # 確かめ直しを待つ
        asked = self.w.a.in_system(SYSTEM, lambda tx: [r["refs"] for r in fix_requests.open_requests(tx, "CL-1")
                                                       if r["kind"] == "target_recheck_missing"])
        self.assertEqual([r["attempt_id"] for r in asked], [str(aid)])            # 待つだけにせず、CL-1 へ修正依頼
        with self.assertRaises(settle.NotReady):
            self.tx(s, lambda tx: settle._judge_input(tx, settle._attempt(tx, aid), s["version"]))
        self.tx(s, lambda tx: target_recheck.record(tx, aid, "not_target", evidence="試験", checked_by="試験"))
        again = self.tx(s, lambda tx: settle.rejudge(tx, aid))
        self.assertEqual(again["state"], "not_reached")

    def test_a_promised_call_answered_by_another_person_keeps_the_promise(self):
        """約束の幅の中でかけた電話で、判定なしで繋いだ方が別の方だった：目的のお客様と話せていないので、約束の幅のまま次へ
        （2026-10-22 の見直し：前は判定が「一部だけ取れた」にして、約束の幅を外し、ふつうの再発信にしていた）。"""
        s = self.ready()
        now = self.tx(s, lambda tx: tx.one("SELECT now() AS t")["t"])
        window = next_action.Window(now + timedelta(days=1), (0, 24 * 3600), None, now - timedelta(hours=1), "day")
        self.tx(s, lambda tx: support.schedule_call(tx, s["task"], now, contact_id=s["contact"], cause="試験", promised=True,
                                                       window=window))
        aid = self.connected(s, items=(("Q1", "confirmed", "はい"),))
        self.tx(s, lambda tx: target_recheck.record(tx, aid, "not_target", evidence="試験：別の方", checked_by="試験"))
        self.tx(s, lambda tx: attempts.record_verification(tx, aid, summary=CLEAN))
        got = self.settle(s, aid)
        self.assertEqual((got["state"], got["decision"].promised, got["decision"].window.until), ("not_reached", True,
                                                                                                    window.until))

    def test_answers_from_a_call_whose_report_was_never_written_are_rechecked(self):
        """会話の処理が報告を書く前に落ちた通話で答えを取っていた：どう目的のお客様だと決めたかが分からないので、確かめ直すまで検証を
        済ませない。確かめられなければ答えは「-」（⛔ 2026-10-22 の見直し：前は確かめていない答えが「完了」で渡りえた）。"""
        s = self.ready()
        aid = self.call(s, ("dialing", "ringing"), verify=None)
        for st in ("answered", "in_conversation", "technical_error"):
            self.tx(s, lambda tx, st=st: attempts.move(tx, aid, st))
        self.tx(s, lambda tx: tx.execute(
            "INSERT INTO salescall.item_values (tenant_id, client_id, task_id, version_id, question_id, state, attempt_id)"
            " VALUES (%s, %s, %s, %s, 'Q1', 'confirmed', %s)", (s["tenant"], s["client"], s["task"], s["version"], aid)))
        with self.assertRaises(attempts.RecheckMissing):
            self.tx(s, lambda tx: attempts.record_verification(tx, aid, summary=CLEAN))
        self.tx(s, lambda tx: target_recheck.record(tx, aid, "cannot_check", evidence="試験：録音で確かめられない", checked_by="試験"))
        self.assertEqual(self.item(s)["state"], "unresolved")

    def test_a_report_with_an_unchecked_connect_arriving_after_verification_asks_cl1(self):
        """会話の報告が通話後の検証より後に届き、判定なしの繋ぎが入っていた：締めずに待ち、CL-1 へ修正依頼を出す（待つだけにしない。
        2026-10-22 の見直し）。"""
        s = self.ready()
        aid = self.call(s, ("dialing", "ringing"), verify=None)
        for st in ("answered", "in_conversation", "ending", "ended"):
            self.tx(s, lambda tx, st=st: attempts.move(tx, aid, st))
        self.tx(s, lambda tx: attempts.record_verification(tx, aid, summary=CLEAN))   # 報告より先に検証が済んだ
        self.tx(s, lambda tx: call_report.save(tx, aid, self.connected_report()))
        self.assertEqual(self.settle(s, aid).get("waiting"), "target_recheck")
        asked = self.w.a.in_system(SYSTEM, lambda tx: [r for r in fix_requests.open_requests(tx, "CL-1")
                                                       if r["kind"] == "target_recheck_missing"])
        self.assertTrue(asked)

    def test_a_connect_without_a_party_still_counts_as_talking_with_the_customer(self):
        """電話口の種別を残さずに繋いだ通話（AIの「代わった方へ」のあと、判定の「ご本人」2つで繋いだ）も、お客様と話した通話：
        お客様が断りの言葉のあとに切れば、発信の拒否（会話に至らなかった、にしない）。"""
        from salescall.core.call_report import Connect, Said
        s = self.ready()
        r = replace(make_report((), cut=Cut("callee", False, True)), connects=(Connect(Said(3, "はい"), ("judge", "judge2"), True),))
        aid = self.call(s, report=r, items=())
        self.assertEqual(self.settle(s, aid)["state"], "do_not_call")


class PromiseNotTransferred(SettleBase):
    """約束の電話で、受付の方に事情で取り次げないと言われた（2026-10-22 ユーザー：「また事情で取り次いでもらえなかったときの
    最後に、『後でまたおかけしてもよろしいでしょうか』のような形で括り、OKなら、当日の約束の時刻の幅の中かけ直してもいいと
    思うけど、そうでなければ、当日の幅はやめたほうがいい」）。"""

    def promised(self, s, window_hours=30):
        now = self.tx(s, lambda tx: tx.one("SELECT now() AS t")["t"])
        window = next_action.Window(now + timedelta(hours=window_hours), (0, 24 * 3600), None, now - timedelta(hours=1), "day")
        self.tx(s, lambda tx: support.schedule_call(tx, s["task"], now, contact_id=s["contact"], cause="試験", promised=True,
                                                       window=window))
        return now, window

    def report(self, again):
        from salescall.core.call_report import NotTransferred, Said
        said = Said(1, "担当が分からないのでお繋ぎできかねます。")
        return replace(rep(("reception",), not_reached="not_transferred"), not_transferred=(NotTransferred(said, again),))

    def test_ok_to_call_again_keeps_today_in_the_window(self):
        """「またおかけしてよい」と言われた：当日の約束の幅の中で、不在の方針の間隔（試験の業務は1時間おき）でかけ直す。"""
        s = self.ready()
        now, window = self.promised(s)
        got = self.settle(s, self.call(s, report=self.report(True)))
        d = got["decision"]
        self.assertEqual((got["state"], d.promised, d.window.until), ("not_reached", True, window.until))
        self.assertLess(d.at, now + timedelta(hours=2))
        self.assertGreaterEqual(d.at, now + timedelta(hours=1))

    def test_not_ok_or_not_asked_moves_past_today(self):
        """断られた・聞いていない：当日の幅ではかけ直さず、次の日へ（約束のまま。幅が当日を越えて続いても、当日の分はやめる）。"""
        for again in (False, None):
            with self.subTest(again=again):
                s = self.ready()
                self.promised(s)
                got = self.settle(s, self.call(s, report=self.report(again)))
                d = got["decision"]
                tomorrow = self.tx(s, lambda tx: tx.one(
                    "SELECT (date_trunc('day', salescall.jst(now())) + interval '1 day') AT TIME ZONE 'Asia/Tokyo' AS t")["t"])
                self.assertEqual((got["state"], d.promised), ("not_reached", True))
                self.assertGreaterEqual(d.at, tomorrow)

    def test_an_exact_time_promise_moves_to_the_next_day_unless_ok(self):
        """時刻ちょうどの約束（「明日の15時」）：またかけてよいと言われれば、その時刻の時間の中で5分後に。そうでなければ、その日の
        時間の中ではかけ直さず、次の日の同じ時刻へ（前は、言われなくても5分後・10分後・20分後とかけ直していた）。"""
        for again, same_day in ((True, True), (None, False), (False, False)):
            with self.subTest(again=again):
                s = self.ready()
                self.put_client(s, "calling.ng_hours", {k: [] for k in KEYS})
                clock = self.tx(s, lambda tx: tx.one(
                    "SELECT now() AS now, greatest(date_trunc('minute', now()) - interval '1 minute',"   # 0時をまたいで前の日にしない
                    " salescall.from_jst(date_trunc('day', salescall.jst(now())))) AS t, extract(epoch FROM"   # （⛔ 見直し17d-2）
                    " date_trunc('day', salescall.jst(now())) + interval '1 day' - salescall.jst(now())) AS left"))
                t, left = clock["t"], int(clock["left"])
                if left < 1800:
                    self.skipTest("日本時間の0時の直前：回の時間が短すぎる")
                self.put_client(s, "callback.exact_slot", min(3600, left - 120))
                window = self.tx(s, lambda tx: next_action._exact_window(tx, s["task"], t, day_said=True))
                self.tx(s, lambda tx: support.schedule_call(tx, s["task"], clock["now"], contact_id=s["contact"], cause="試験",
                                                               promised=True, callee_at=t, window=window))
                self.settle(s, self.call(s, report=self.report(again)))
                cur = self.tx(s, lambda tx: next_action.current(tx, s["task"]))
                if same_day:
                    self.assertEqual((cur["window_round"], cur["callee_at"]), (0, t))
                    self.assertLess(cur["call_at"], t + timedelta(minutes=10))
                else:
                    self.assertEqual((cur["window_round"], cur["call_at"], cur["callee_at"]),
                                     (1, t + timedelta(days=1), t + timedelta(days=1)))

    def test_an_untimed_promise_also_moves_past_today_unless_ok(self):
        """時刻の決まっていない約束（「あとで」）の電話でも同じ決まり：またかけてよいと言われなければ、当日はかけず次の日から
        （⛔ 2026-10-22 の見直し14：前は時刻・幅の約束だけで、時刻の決まっていない約束は当日にかけ直していた）。"""
        for again, today in ((True, True), (None, False), (False, False)):
            with self.subTest(again=again):
                s = self.ready()
                clock = self.tx(s, lambda tx: tx.one(
                    "SELECT now() AS now, (date_trunc('day', salescall.jst(now())) + interval '1 day') AT TIME ZONE 'Asia/Tokyo'"
                    " AS tomorrow"))
                if clock["tomorrow"] - clock["now"] < timedelta(hours=3):
                    self.skipTest("日本時間の0時の近く：当日のかけ直しが次の日になる")
                self.tx(s, lambda tx: support.schedule_call(tx, s["task"], clock["now"], contact_id=s["contact"],
                                                               cause="試験：時刻の決まっていない約束", promised=True))
                d = self.settle(s, self.call(s, report=self.report(again)))["decision"]
                self.assertEqual((d.kind, d.promised), ("call", True))
                if today:
                    self.assertLess(d.at, clock["tomorrow"])
                else:
                    self.assertGreaterEqual(d.at, clock["tomorrow"])

    def test_ok_to_call_again_counts_only_the_calls_in_the_round(self):
        """またかけてよいと言われたときの、同じ回の中のかけ直しの上限は、その回にかけた数（不在の方針の回数。ここでは1回）。
        2本目で次の回へ（⛔ 2026-10-22 の見直し14：前は、別の結果の回数と混ぜて数えていた）。"""
        s = self.ready(rules=rules_raw(retry={k: {"action": "call", "max_times": 1, "interval": {"hours": 1}}
                                              for k in business_rules.POLICY_KEYS}))
        now, window = self.promised(s)
        first = self.settle(s, self.call(s, report=self.report(True)))["decision"]
        self.assertEqual((first.promised, first.window.round), (True, 0))
        self.tx(s, lambda tx: support.schedule_call(tx, s["task"], now, contact_id=s["contact"], cause="試験", promised=True,
                                                       window=first.window))
        second = self.settle(s, self.call(s, report=self.report(True)))["decision"]
        self.assertEqual((second.promised, second.window.round), (True, 1))
        self.assertGreaterEqual(second.at, window.until)

    def test_the_claim_tells_the_conversation_whether_it_is_a_promised_call(self):
        """予約証が、約束の電話かを会話の処理へ渡す（約束の電話のときだけ、締めの前に「後ほど改めてお掛けしても」と聞く）。"""
        s = self.ready()
        self.assertFalse(self.claim(s).promised)
        s = self.ready()
        self.promised(s)
        self.assertTrue(self.claim(s).promised)

    def test_the_answer_is_kept_in_the_report(self):
        """答えは報告に残り、置いて読み直しても同じ（聞いていないときは項目が無い）。"""
        from salescall.core.call_report import from_json
        for again in (True, False, None):
            r = self.report(again)
            self.assertEqual(from_json(r.to_json()), r)
            self.assertEqual(r.may_call_again, again is True)
            self.assertEqual("again" in r.to_json()["facts"]["not_transferred"][0], again is not None)


class CountedCalls(SettleBase):
    """行の回数に数える発信は、門と次の動きの再発信の方針の回数で同じ決まり（データベースの salescall.counts_as_try の1か所。
    ⛔ 2026-10-22 の見直し14：前は方針の回数が、その結果の出た通話をすべて数え、回線の側の失敗・切れた通話・約束の電話で、
    お客様に一度も繋がらないまま行が終わった）。"""

    ONE = {k: {"action": "call", "max_times": 1, "interval": {"hours": 1}} for k in business_rules.POLICY_KEYS}

    def due_now(self, s):
        self.tx(s, lambda tx: next_action.schedule_now(tx, s["task"], cause="試験：その時刻が来たことにする"))

    def test_line_side_failures_do_not_use_up_the_policy(self):
        """回線の側でかけられなかった・お客様が出る前にこちらの仕組みが止まった発信は、方針の回数（ここでは1回）に数えない。
        お客様のところで鳴って出なかった発信は、これまでどおり数える（2本目で方針の回数を超える）。"""
        for path in (["dialing", "call_failed"], ["dialing", "technical_error"]):
            with self.subTest(path=path[-1]):
                s = self.ready(rules=rules_raw(retry=self.ONE))
                for _ in range(2):   # 発信に失敗が同じ番号で続けて3回になると、その番号へはかけない（下の試験）
                    d = self.settle(s, self.call(s, path))["decision"]
                    self.assertEqual(d.kind, "call", d.rule)
                    self.due_now(s)
                self.assertEqual(self.tx(s, lambda tx: next_action._times(tx, s["task"], "open")), 0)
                kinds = []
                for _ in range(2):                    # 最小の間隔（門の歯止め）を待たずに、出なかった発信を置く
                    d = self.settle(s, self.past_attempt(s, ["dialing", "no_answer"], settled=False))["decision"]
                    kinds.append((d.kind, d.end_reason))
                self.assertEqual(kinds, [("call", None), ("end", "policy_max:open")])

    def test_promised_calls_are_not_counted_like_the_gate(self):
        """お客様と約束したかけ直しの電話は、門の行の回数にも、方針の回数にも数えない（同じ決まり）。"""
        s = self.ready(rules=rules_raw(retry=self.ONE))
        now = self.tx(s, lambda tx: tx.one("SELECT now() AS t")["t"])
        self.tx(s, lambda tx: support.schedule_call(tx, s["task"], now, contact_id=s["contact"],
                                                       cause="試験：時刻の決まっていない約束", promised=True))
        self.settle(s, self.call(s, ["dialing", "no_answer"]))
        self.assertEqual(self.tx(s, lambda tx: next_action._times(tx, s["task"], "open")), 0)
        p = self.tx(s, lambda tx: gate.pacing(tx, s["task"], promised=False))
        self.assertEqual(p.remaining, 3)

    def test_line_cuts_in_a_row_do_not_end_the_row(self):
        """途中で切れた通話が続いても（すぐのかけ直しは2回まで。3回目からはふつうの再発信の方針）、方針の回数（1回）で行を
        終えない（切れた通話は数えない）。"""
        s = self.ready(rules=rules_raw(retry=self.ONE))
        cut = ("dialing", "ringing", "answered", "in_conversation", "dropped")
        for _ in range(3):
            d = self.settle(s, self.call(s, cut, side="line", report=rep(cut=Cut("callee", False, False))))["decision"]
            self.due_now(s)
        self.assertEqual(d.kind, "call", d.rule)
        self.assertNotEqual(d.target.purpose, "resume")


class UnusableNumber(SettleBase):
    """回線の部品が番号を受け付けない（発信に失敗＝お客様の電話は鳴っていない）電話は行の回数に数えず、決まった順（設定
    dial.failed_retry_days。初めは [1, 1, 2, 4]＝その日にもう1回・次の日に1回・そのさらに2日後に1回）で同じ番号へかけ直し、計4回続けて
    受け付けられなければ、その番号へはかけない（2026-10-22 深夜 ユーザー：「同じ番号で続けて当日2回＆翌日1回、さらに2日後1回の計4回
    受け付けられなかったら、その番号へはかけない」。計画書 10-3「無限に再試行しない」）。お客様が言った別の番号があれば、そちらへ。"""

    def due_now(self, s, contact=None):
        now = self.tx(s, lambda tx: tx.one("SELECT now() AS t")["t"])
        self.tx(s, lambda tx: support.schedule_call(tx, s["task"], now, contact_id=contact or s["contact"], cause="試験：その時刻が来た",
                                                       promised=False))

    def jst_date(self, s, at):
        return self.tx(s, lambda tx: tx.one("SELECT salescall.jst(%s)::date AS d", (at,))["d"])

    def test_four_failures_follow_the_order_and_stop_the_number(self):
        s = self.ready()
        today = self.tx(s, lambda tx: tx.one("SELECT salescall.jst(now())::date AS d")["d"])
        gap = timedelta(seconds=int(self.tx(s, lambda tx: settings.resolve(tx, "gate.same_contact_min_interval", tenant_id=s["tenant"],
                                                                          client_id=s["client"], business_id=s["business"]))))
        got = []
        for _ in range(4):
            aid = self.call(s, ["dialing", "call_failed"])
            d = self.settle(s, aid)["decision"]
            ended = self.tx(s, lambda tx: tx.one("SELECT ended_at FROM salescall.attempts WHERE attempt_id = %s", (aid,)))["ended_at"]
            got.append((d, ended))
            self.due_now(s)
        (d1, e1), (d2, _), (d3, _), (d4, _) = got
        self.assertEqual((d1.kind, d2.kind, d3.kind), ("call", "call", "call"))
        # 1日目にもう1回（同じお客様への最短の間隔をあけて、すぐ）。⛔ 見直し17d-2：前は「今日の日付」で確かめたので、日の終わりの
        # 数分（間隔で次の日へまたぐ）に流すと落ちた。決まりは「最後の失敗の終わり＋最短の間隔」
        self.assertEqual(d1.at, e1 + gap)
        self.assertEqual(self.jst_date(s, d2.at), today + timedelta(days=1))   # 2日目
        self.assertEqual(self.jst_date(s, d3.at), today + timedelta(days=3))   # 4日目（そのさらに2日後）
        # 4回目：番号のせいと分かった4回目で、その番号へはかけない（0143：分からないあいだは締めずに休み、決まってから1回だけ決める。
        # ⛔ 0129〜0141：分からない4回目にも予定を書き、門の number_unconfirmed で待たせた）
        self.assertEqual((d4.kind, d4.end_reason), ("end", "number_unusable"))
        self.assertEqual(self.tx(s, lambda tx: gate.pacing(tx, s["task"]).remaining), 3)   # 行の回数には数えない

    def test_calls_ending_before_the_callee_answers_follow_the_same_order(self):
        """回線は元気でも、ある番号だけ毎回お客様が出る前に終わる（呼び出したあと結果不明＝数え方の line_failure）と、番号を受け付け
        ないときと同じ順でかけ、計4回続いたら、その番号へはかけない（2026-10-23 ユーザー：「番号を受け付けないときと同じ。当日2回・
        翌日1回・その2日後1回の計4回続いたら、その番号へはかけない」。⛔ 見直し17a：前は回線の異常にだけ数え、ほかの番号にかかる
        元気な回線は止まらないので、その番号へは方針の間隔で終わりなくかけ続けた）。発信に失敗とまざっても、続いた数に入る。"""
        for name, paths in (("出る前に終わる", [["dialing", "ringing", "technical_error"]] * 4),
                            ("発信に失敗とまざる", [["dialing", "call_failed"], ["dialing", "ringing", "technical_error"],
                                                    ["dialing", "call_failed"], ["dialing", "ringing", "technical_error"]])):
            with self.subTest(name):
                s = self.ready()
                today = self.tx(s, lambda tx: tx.one("SELECT salescall.jst(now())::date AS d")["d"])
                got = []
                for path in paths:
                    d = self.settle(s, self.call(s, path))["decision"]
                    got.append(d)
                    self.due_now(s)
                d1, d2, d3, d4 = got
                self.assertEqual((d1.kind, d2.kind, d3.kind), ("call",) * 3, [d.rule for d in got])
                self.assertEqual(self.jst_date(s, d2.at), today + timedelta(days=1))   # 2日目
                self.assertEqual(self.jst_date(s, d3.at), today + timedelta(days=3))   # 4日目（そのさらに2日後）
                self.assertEqual((d4.kind, d4.end_reason), ("end", "number_unusable"))
                self.assertEqual(self.tx(s, lambda tx: gate.pacing(tx, s["task"]).remaining), 3)   # 行の回数には数えない

    def test_a_call_that_rang_restarts_the_run(self):
        """続いた数を0に戻すのは、お客様のところで電話が鳴った発信（salescall.rang＝数え方の「お客様に届いた1回」か「途中で切れた」）。
        お客様が出なかった（鳴った）電話がはさまれば、数え直す。"""
        s = self.ready()
        run = lambda: self.tx(s, lambda tx: support.number_run(tx, s["contact"]))["n"]
        for path, n in ((["dialing", "ringing", "technical_error"], 1), (["dialing", "call_failed"], 2),
                        (["dialing", "no_answer"], 0)):
            self.settle(s, self.call(s, path))
            self.assertEqual(run(), n, path)
            self.due_now(s)

    def test_a_broken_line_does_not_make_numbers_unusable_on_its_own(self):
        """同じ回線で、鳴った電話を挟まずに、別々の番号へかけた電話がどれもお客様が出る前に終わった：違う番号がいくつ続いても、
        まだ分からない（0143：違う番号の数では決めない）。どの行も締めずに休む（予定・約束の印はそのまま）。テスト番号が鳴らなかったら
        回線のせい（0129。2026-10-24 ユーザー：「回線の問題が明らかな場合は行は取りやめでなく一時中止だけ」）：どの番号の続きにも数えず、
        行はかけた予定のまま（どの数にも数えない）。⛔ 見直し18a の問題1：前は、回線の停止が自動で解けると、壊れた回線のお客様の行が
        4日ですべて「番号が使えない」で終わった。⛔ 0129〜0141：違う番号が2つ続けば回線のせいとした（見直し21a）。"""
        s = self.ready()
        rows = [s]
        for _ in range(2):
            k = self.w.contact(s["tenant"], s["client"], normalize(fictional_jp_fixed()).e164)
            t = self.w.task(s["tenant"], s["client"], s["run"], k)
            self.tx(s, lambda tx: next_action.schedule_now(tx, t, cause="試験"))
            rows.append(dict(s, task=t, contact=k))
        got, before = [], []
        for r in rows:
            before.append(self.tx(s, lambda tx: next_action.current(tx, r["task"])))
            aid = self.call(r, ["dialing", "ringing", "technical_error"])
            self.assertEqual(self.settle(r, aid, decide=False).get("waiting"), "await_decision")   # 締めずに休む
            got.append(aid)
        self.assertEqual([self.tx(s, lambda tx: health.blame_of(tx, a)) for a in got], ["pending"] * 3)
        self.assertEqual([self.tx(s, lambda tx: next_action.current(tx, r["task"]))["seq"] for r in rows],
                         [b["seq"] for b in before])                          # 予定は書かない
        self.assertTrue(self.w.test_number_silent(s))                              # テスト番号も鳴らない
        self.assertEqual([self.tx(s, lambda tx: health.blame_of(tx, a)) for a in got], ["line"] * 3)
        for r, b, aid in zip(rows, before, got):   # 回線のせい：かけた予定のまま（約束の印・かける先も同じ。どの数にも数えない）
            d = self.settle(r, aid)["decision"]
            self.assertEqual((d.kind, d.promised, d.target.contact_id), ("call", b["promised"], r["contact"]), d.rule)
        for r in rows:
            self.assertEqual(self.tx(s, lambda tx: support.number_run(tx, r["contact"]))["n"], 0)
            self.assertFalse(self.tx(s, lambda tx: health.number_dead(tx, r["contact"])))
        self.ring_elsewhere(s)                                                 # 直ったあと鳴っても、回線のせいのまま（変わらない）
        self.assertEqual([self.tx(s, lambda tx: health.blame_of(tx, a)) for a in got], ["line"] * 3)

    def test_a_last_failure_that_turns_out_the_lines_does_not_end_the_row(self):
        """並びを使い切る4回目が、まだ番号のせいか分からないあいだは、行を締めずに休む（予定は書かない。門が awaiting_decision で断り、
        候補を出す処理も出さない＝0143）。テスト番号が鳴らなかった（回線のせい）なら、4回目は数から外れ（3回）、行は、かけた予定の
        まま続く（門が通す）。2026-10-24 ユーザー：「回線の問題が明らかな場合は行は取りやめでなく一時中止だけ」。⛔ 0129〜0141：分からない
        4回目にも予定を書き、門の number_unconfirmed で待たせた。"""
        t = self.ready()
        for _ in range(3):
            self.settle(t, self.call(t, ["dialing", "call_failed"]))           # 番号のせい（テスト番号が鳴った）
            self.due_now(t)
        before = self.tx(t, lambda tx: next_action.current(tx, t["task"]))
        last = self.call(t, ["dialing", "call_failed"])
        self.assertEqual(self.settle(t, last, decide=False).get("waiting"), "await_decision")
        self.assertEqual(self.tx(t, lambda tx: next_action.current(tx, t["task"]))["seq"], before["seq"])   # 予定は書かない
        self.assertEqual(self.claim(t).reason, "awaiting_decision")
        self.assertNotIn(t["task"], [r["task_id"] for r in self.tx(t, lambda tx: scheduler.due_tasks(tx, 50))])
        run = self.tx(t, lambda tx: support.number_run(tx, t["contact"]))
        self.assertEqual((run["n"], run["n_sure"]), (4, 3))
        self.assertTrue(self.w.test_number_silent(t))                              # テスト番号も鳴らない
        self.assertEqual(self.tx(t, lambda tx: health.blame_of(tx, last)), "line")
        run = self.tx(t, lambda tx: support.number_run(tx, t["contact"]))
        self.assertEqual((run["n"], run["n_sure"]), (3, 3))
        d = self.settle(t, last)["decision"]                                   # 決まったら、1回だけ決める
        self.assertEqual((d.kind, d.promised, d.target.contact_id), ("call", before["promised"], t["contact"]), d.rule)
        self.assertIn(t["task"], [r["task_id"] for r in self.tx(t, lambda tx: scheduler.due_tasks(tx, 50))])
        self.assertEqual(self.claim(t).reason, "line_stopped")                # テスト番号が鳴らなかった回線は休み（L-10）
        other = self.w.line(t["tenant"])
        self.lease(t, line=other)
        self.assertIsInstance(self.claim(t, line=other), gate.Claim)          # 行は、ほかの回線でかけられる

    def test_a_pending_call_of_the_same_number_on_another_row_does_not_move_the_day(self):
        """同じ番号の別の行（別の実行）に、まだ分からない電話があっても、この行の次の日は、番号のせいと分かった数（n_sure）だけで
        決める（next_action._failed_retry_at の1か所。0143）。分からない電話は日を送らない。⛔ 0129〜0141：分からない電話も数に入れて
        次の日へ送った（見直し21a の場面 a6）。同じ番号のほかの行は、決まるまで門が number_unconfirmed で待たせない（並びを使い切って
        いない）。⇒ 見直し22 の 9：ただし、分からない電話を番号のせいに数えると番号の順で次の日になるあいだは待つ（同じ日に3回目を
        かけない）。"""
        s = self.ready()
        today = self.tx(s, lambda tx: tx.one("SELECT salescall.jst(now())::date AS d")["d"])
        run2 = self.w.run(s["tenant"], s["client"], s["business"], s["version"])
        t2 = self.w.task(s["tenant"], s["client"], run2, s["contact"])
        other = dict(s, task=t2)
        self.tx(s, lambda tx: next_action.schedule_now(tx, t2, cause="試験"))
        d = self.settle(s, self.call(s, ["dialing", "call_failed"]))["decision"]   # 1つ目：番号のせい
        self.assertEqual(self.jst_date(s, d.at), today)                        # 1日目にもう1回（並びの1つ目）
        # 別の行の予定は、番号の続きが伸びる前に書いたもの：番号ごとの決まった順（最後の失敗＋最短の間隔）より前にはかけない（門の
        # number_failed_wait。決まりは番号の欄＝core/health.number_wait の1か所。見直し22 の 9）。次の動きが、その順で予定を書き直す
        got = self.claim(other)
        self.assertEqual(getattr(got, "reason", None), "number_failed_wait")
        self.tx(s, lambda tx: next_action.after_denial(tx, t2, got, tenant_id=s["tenant"], client_id=s["client"],
                                                       business_id=s["business"]))
        replanned = self.tx(s, lambda tx: next_action.current(tx, t2))
        self.assertLess(abs((replanned["call_at"] - d.at).total_seconds()), 2)
        self.assertIn("鳴らない電話が続く番号", replanned["cause"])               # 通話のあとと同じ1か所で（決まった間ごとに書き直さない）
        self.tx(s, lambda tx: next_action.schedule_now(tx, t2, cause="試験：その時刻が来た"))
        aid = self.call(other, ["dialing", "call_failed"])                     # 別の行：まだ分からない
        self.assertEqual(self.tx(s, lambda tx: health.blame_of(tx, aid)), "pending")
        run = self.tx(s, lambda tx: support.number_run(tx, s["contact"]))
        self.assertEqual((run["n"], run["n_sure"]), (2, 1))
        now = self.tx(s, lambda tx: tx.one("SELECT now() AS t")["t"])
        at = self.tx(s, lambda tx: next_action._failed_retry_at(tx, s["task"], s["contact"], now, now))
        self.assertEqual(self.jst_date(s, at), today)                          # 分からない電話では、次の日へ送らない
        # ただし、分からない電話を番号のせいに数えると番号の順で次の日になる（当日の2回を使い切る）あいだは、同じ番号の行は、どの回線
        # でもかけずに待つ（番号の欄の待つ印＝N-4 の1か所。予定は書き直さない。見直し22 の 9）
        self.assertTrue(self.tx(s, lambda tx: health.number_held(tx, s["contact"])))
        self.tx(s, lambda tx: next_action.schedule_now(tx, s["task"], cause="試験：その時刻が来た"))
        self.assertEqual(self.reason(s), "number_unconfirmed")

    def test_the_blame_of_a_call_is_read_on_its_own_line_only(self):
        """だれのせいかは、その電話の回線の上の前後の電話だけで決まる（ほかの回線の鳴った電話は、この回線が元気だった証拠に
        ならない＝回線の欄は回線ごと。回線が2本の場面）。"""
        s = self.ready()
        other_line = self.w.line(s["tenant"])
        aid = self.call(s, ["dialing", "call_failed"])
        self.ring_elsewhere(s, line=other_line)
        self.assertEqual(self.tx(s, lambda tx: health.blame_of(tx, aid)), "pending")
        self.ring_elsewhere(s)
        self.assertEqual(self.tx(s, lambda tx: health.blame_of(tx, aid)), "number")

    def test_the_run_on_the_line_is_cut_by_a_call_that_rang(self):
        """続き（鳴った電話で区切った1かたまり）の外の、違う番号の鳴らなかった電話は、回線のせいの証拠にならない：鳴らなかった
        番号A → 鳴った番号B → 鳴らなかった番号C は、A も C も番号のせい（あとに鳴った電話があれば）。"""
        s = self.ready()
        k = self.w.contact(s["tenant"], s["client"], normalize(fictional_jp_fixed()).e164)
        t = self.w.task(s["tenant"], s["client"], s["run"], k)
        self.tx(s, lambda tx: next_action.schedule_now(tx, t, cause="試験"))
        c = dict(s, task=t, contact=k)
        a = self.call(s, ["dialing", "call_failed"])
        self.ring_elsewhere(s)
        b = self.call(c, ["dialing", "call_failed"])
        self.ring_elsewhere(s)
        self.assertEqual([self.tx(s, lambda tx: health.blame_of(tx, x)) for x in (a, b)], ["number", "number"])

    def test_the_run_of_failures_starts_on_its_first_day(self):
        """続いた失敗の数・続いた最初の日・最後の失敗の終わりは、番号の欄の1か所（DESIGN.md 17-1-3。次にかける日は最初の日から
        数える。見直し16b：前は最初の日を別の問い合わせで求め直し、日をまたいで続いた場面の試験が無かった）。"""
        s = self.ready()
        for days_ago in (2, 1):   # 2日前と1日前に、受け付けられなかった
            aid = self.w.insert_claim(s)
            for state in ("dialing", "call_failed"):
                self.w.move(s, aid, state)

            def back(tx):   # 試験：時刻はデータベースが決めるので、持ち主の取引の中だけ仕掛けを外して、その日へ戻す
                tx.execute("ALTER TABLE salescall.attempts DISABLE TRIGGER USER")
                tx.execute("UPDATE salescall.attempts SET claimed_at = claimed_at - make_interval(days => %s),"
                           " ended_at = ended_at - make_interval(days => %s) WHERE attempt_id = %s", (days_ago, days_ago, aid))
                tx.execute("ALTER TABLE salescall.attempts ENABLE TRIGGER USER")
            owner_access().in_system(SYSTEM, back)
            self.tx(s, lambda tx, aid=aid: health.after_call(tx, aid))        # 電話が終わった所と同じく（戻した時刻で）
        self.w.test_number_rang(s)                                             # 番号のせいと分かった（番号の欄が数えるのは、その数だけ）
        run = self.tx(s, lambda tx: support.number_run(tx, s["contact"]))
        today = self.tx(s, lambda tx: tx.one("SELECT salescall.jst(now())::date AS d")["d"])
        self.assertEqual((run["n"], run["first_day"]), (2, today - timedelta(days=2)))
        # 最後の失敗の終わりは、いちばん新しい失敗（1日前）の終わり（次にかける時刻はここから数える。⛔ 見直し17b：取り違えても
        # 試験が気づかなかった）
        latest = self.tx(s, lambda tx: tx.one("SELECT max(ended_at) AS e FROM salescall.attempts WHERE contact_id = %s",
                                             (s["contact"],))["e"])
        self.assertEqual(run["last_end"], latest)

    def test_a_failure_moved_to_a_later_day_moves_the_rest_too(self):
        """2つ目の失敗が、決まった順の日（1日目）より遅れた（時間帯の外などで2日目にずれた）とき、3つ目は2日目でなく3日目
        （次の日は「最初の失敗の日＋並び」と「前の失敗の日＋並びの差」の遅いほう＝next_action._failed_retry_at の1か所。⛔ 見直し18c の
        (1)：前は最初の日からだけ数え、ずれた2つ目と同じ日に3つ目を詰めた）。"""
        s = self.ready()
        for days_ago in (1, 0):   # 1日前に1つ目・今日に2つ目（本当は1日目＝1日前にかけるはずだった分）
            aid = self.w.insert_claim(s)
            for state in ("dialing", "call_failed"):
                self.w.move(s, aid, state)

            def back(tx):
                tx.execute("ALTER TABLE salescall.attempts DISABLE TRIGGER USER")
                tx.execute("UPDATE salescall.attempts SET claimed_at = claimed_at - make_interval(days => %s),"
                           " ended_at = ended_at - make_interval(days => %s) WHERE attempt_id = %s", (days_ago, days_ago, aid))
                tx.execute("ALTER TABLE salescall.attempts ENABLE TRIGGER USER")
            owner_access().in_system(SYSTEM, back)
            self.tx(s, lambda tx, aid=aid: health.after_call(tx, aid))        # 電話が終わった所と同じく（戻した時刻で）
        self.w.test_number_rang(s)                                             # 番号のせいと分かった（日を送るのは、その数だけ）
        today = self.tx(s, lambda tx: tx.one("SELECT salescall.jst(now())::date AS d")["d"])
        self.assertEqual(self.tx(s, lambda tx: support.number_run(tx, s["contact"]))["first_day"], today - timedelta(days=1))
        now = self.tx(s, lambda tx: tx.one("SELECT now() AS t")["t"])
        at = self.tx(s, lambda tx: next_action._failed_retry_at(tx, s["task"], s["contact"], now, now))
        self.assertEqual(self.jst_date(s, at), today + timedelta(days=1))   # 前の失敗の日（今日）＋並びの差（1日）

    def test_outside_hours_waits_for_the_next_opening(self):
        """門が時間帯の外で断ったら、次の予定は次の時間帯の始まり（gate.next_opening の1か所。⛔ 見直し17a：前は決まった間のあとへ
        書き直し続けた。見直し18a の (5)：壊しても試験が落ちなかった。見直し18a の Records.test_outside_hours_overnight の場面）。"""
        s = self.ready()
        hour = self.tx(s, lambda tx: tx.one("SELECT extract(hour FROM salescall.jst(now()))::int AS h")["h"])
        band = [["13:00", "14:00"]] if hour < 12 else [["06:00", "07:00"]]   # いまを含まない時間帯
        self.put_client(s, "calling.hours", {k: band for k in KEYS})
        scope = dict(tenant_id=s["tenant"], client_id=s["client"], business_id=s["business"])
        d = self.tx(s, lambda tx: next_action.after_denial(tx, s["task"], gate.denied("outside_hours"), **scope))
        opening = self.tx(s, lambda tx: gate.next_opening(tx, tx.one("SELECT now() AS t")["t"], **scope))
        self.assertEqual((d.kind, d.at), ("call", opening))

    def test_the_order_is_per_client(self):
        """並びは依頼者ごとに変えられる（[1]＝最初の失敗で、その番号へはかけない）。並びは1日目から始まり、小さくならない。"""
        s = self.ready()
        self.put_client(s, "dial.failed_retry_days", [1])
        d = self.settle(s, self.call(s, ["dialing", "call_failed"]))["decision"]   # 番号のせいと分かった1回目で
        self.assertEqual((d.kind, d.end_reason), ("end", "number_unusable"))
        for bad in ([2, 3], [1, 3, 2]):
            with self.subTest(bad=bad), self.assertRaises(Rejected) as e:
                self.put_client(s, "dial.failed_retry_days", bad)
            self.assertEqual(e.exception.constraint, "setting_values_failed_retry_days_order")

    def test_a_placed_call_between_restarts_the_count(self):
        s = self.ready()
        for path in (["dialing", "call_failed"], ["dialing", "call_failed"], ["dialing", "call_failed"], ["dialing", "no_answer"],
                     ["dialing", "call_failed"], ["dialing", "call_failed"], ["dialing", "call_failed"]):
            d = self.settle(s, self.past_attempt(s, path, settled=False))["decision"]
            self.assertEqual(d.kind, "call", d.rule)

    def test_the_number_the_callee_gave_is_used_instead(self):
        from salescall.core.phone import normalize
        s = self.ready()
        first = self.call(s, ["dialing", "no_answer"])
        self.settle(s, first)
        other = self.tx(s, lambda tx: tasks.add_callee_number(tx, s["task"], normalize("0312345679"), source_attempt_id=first))
        for _ in range(4):
            d = self.settle(s, self.past_attempt(s, ["dialing", "call_failed"], settled=False))["decision"]
        self.assertEqual((d.kind, d.contact_id), ("call", other))              # 番号のせいと分かった4回目で、言われた番号へ

    def test_a_number_the_callee_gave_that_never_rings_is_asked_again(self):
        """お客様が言った番号が、番号のせいで繋がらない（並びを使い切り、同じ回線がほかの番号を鳴らせた）：その番号を言われた通話の
        番号へ、番号を伺い直す（予約証の number_again＝unreachable。2026-10-24 ユーザー：「別の方のお電話よりも、繋がらない番号の
        パターンも必要じゃないですか？」・CL-1 の連絡帳 4-41 の (3)）。伺い直すのは同じ行で1回だけ（番号違いの聞き直しと同じ数え方）。"""
        from salescall.core.phone import normalize
        s = self.ready()
        first = self.call(s, ["dialing", "no_answer"])
        self.settle(s, first)
        phone = normalize("0312345679")
        other = self.tx(s, lambda tx: tasks.add_callee_number(tx, s["task"], phone, source_attempt_id=first))
        alt = dict(s, contact=other, phone=phone.e164)
        for _ in range(4):
            self.due_now(s, contact=other)
            d = self.settle(alt, self.past_attempt(alt, ["dialing", "call_failed"], settled=False))["decision"]
        # 番号のせいと分かった4回目（回線は元気だった）で、言われた通話の番号へ伺い直す
        self.assertEqual((d.kind, d.contact_id, d.target.purpose, d.target.wrong_contact_id, d.target.number_again),
                         ("call", s["contact"], "ask_number_again", other, "unreachable"))
        c = self.claim(s)
        self.assertIsInstance(c, gate.Claim, c)
        self.assertEqual((c.purpose, c.number_again, c.wrong_phone_e164), ("ask_number_again", "unreachable", phone.e164))
        # 同じ行で、もう伺い直していれば、繋がらない番号でも伺い直さずに終える
        numbers = next_action.Numbers(unusable=frozenset({other}), sources=((other, s["contact"]),), asked_again=True)
        self.assertEqual(next_action.usable(next_action.Target(other), numbers).end_reason, "number_unusable")
        numbers = next_action.Numbers(unusable=frozenset({other}), sources=((other, s["contact"]),))
        self.assertEqual(next_action.usable(next_action.Target(other), numbers),
                         next_action.Target(s["contact"], None, "ask_number_again", other, number_again="unreachable"))

    def test_only_calls_to_whoever_answers_move_to_another_number(self):
        """使えない番号のとき、お客様が言った別の番号へ替えるのは、ふつうの電話・続きの電話（行のお客様へかける電話）だけ。番号の
        聞き直し・お詫びの電話は、その番号のお客様へかける電話なので替えずに行を終える（next_action.usable の1か所。見直し16b：
        この場面の試験が無かった）。"""
        bad, other = uuid.uuid4(), uuid.uuid4()
        numbers = next_action.Numbers(unusable=frozenset({bad}), said=(other,))
        for purpose, to in (("normal", other), ("resume", other), ("ask_number_again", None), ("apology", None)):
            with self.subTest(purpose=purpose):
                got = next_action.usable(next_action.Target(bad, None, purpose), numbers)
                self.assertEqual((got.contact_id, got.end_reason), (to, None if to else "number_unusable"))

    def test_other_rows_of_the_same_number_do_not_dial_it_again(self):
        """同じ番号のほかの行も、その番号へはかけない（門が「番号が使えない」で断り、次の動きが行を終える。決まりは
        番号の欄の「使えない」＝N-1 の1か所。⛔ 見直し16：前は、ほかの行が、それぞれ1回ずつ失敗するまでかけ、そのたびに回線の異常に
        数えられた）。"""
        s = self.ready()
        run2 = self.w.run(s["tenant"], s["client"], s["business"], s["version"])
        t2 = self.w.task(s["tenant"], s["client"], run2, s["contact"])
        self.tx(s, lambda tx: next_action.schedule_now(tx, t2, cause="試験"))
        for _ in range(4):
            self.settle(s, self.past_attempt(s, ["dialing", "call_failed"], settled=False))
        self.ring_elsewhere(s)                                                 # 回線は元気だった：番号のせい
        got = self.tx(s, lambda tx: gate.claim(tx, task_id=t2, line_id=s["line"], lease_holder=HOLDER))
        self.assertIsInstance(got, gate.Denied)
        self.assertEqual(got.reason, "number_unusable")
        d = self.tx(s, lambda tx: next_action.after_denial(tx, t2, got, tenant_id=s["tenant"], client_id=s["client"],
                                                         business_id=s["business"]))
        self.assertEqual((d.kind, d.end_reason), ("end", "number_unusable"))

    def test_a_row_refused_for_an_unusable_number_moves_to_the_number_the_callee_gave(self):
        """門が「番号が使えない」で断った行に、お客様が言った別の番号があれば、次の動きはそちらへかける（next_action.after_denial の
        1か所。⛔ 見直し17b：外しても試験が気づかなかった＝別の番号があっても行が終わりえた）。"""
        from salescall.core.phone import normalize
        s = self.ready()
        run2 = self.w.run(s["tenant"], s["client"], s["business"], s["version"])
        t2 = self.w.task(s["tenant"], s["client"], run2, s["contact"])
        s2 = {**s, "task": t2}   # 別の番号は、その行の通話で言われたもの
        self.tx(s, lambda tx: next_action.schedule_now(tx, t2, cause="試験"))
        first = self.call(s2, ["dialing", "no_answer"])
        self.settle(s2, first)
        other = self.tx(s, lambda tx: tasks.add_callee_number(tx, t2, normalize("0312345679"), source_attempt_id=first))
        self.tx(s, lambda tx: next_action.schedule_now(tx, t2, cause="試験"))
        for _ in range(4):
            self.settle(s, self.past_attempt(s, ["dialing", "call_failed"], settled=False))
        self.ring_elsewhere(s)
        got = self.tx(s, lambda tx: gate.claim(tx, task_id=t2, line_id=s["line"], lease_holder=HOLDER))
        self.assertEqual(got.reason, "number_unusable")
        d = self.tx(s, lambda tx: next_action.after_denial(tx, t2, got, tenant_id=s["tenant"], client_id=s["client"],
                                                         business_id=s["business"]))
        self.assertEqual((d.kind, d.contact_id), ("call", other))


class SameDayAndSuccessor(SettleBase):
    """目的のお客様に繋がらなかった通話の、同じ日のかけ直しと、在籍していないときの後任の方（2026-10-22 ユーザー：「本日、本人が
    戻られる可能性があるのかと、戻られる可能性があるのであれば後ほどおかけしてもよろしいでしょうかというお尋ねが必要…同じ日に
    2回以上、その言葉がなくかけるのであれば煙たがられますよ」「基本は後任へつないでもらう。選択可能にしたはずです」）。"""

    def tomorrow(self, s):
        return self.tx(s, lambda tx: tx.one(
            "SELECT (date_trunc('day', salescall.jst(now())) + interval '1 day') AT TIME ZONE 'Asia/Tokyo' AS t")["t"])

    def absent(self, again=None):
        from salescall.core.call_report import Callback, Said
        cb = Callback("absent", None, None, None, Said(1, "あいにく岡田は席を外しております。"), again=again,
                      back_today=None if again is None else again)
        return rep(("reception",), callback=cb, not_reached="absent")

    def departed(self, policy, *, after_successor=False, parties=("reception",), not_reached="departed"):
        from salescall.core.call_report import Departed, Said
        d = Departed(policy, ("ai",), after_successor, Said(1, "岡田は退職いたしました。"))
        return replace(rep(parties, not_reached=not_reached), departures=(d,))

    def test_absent_is_called_again_the_same_day_only_when_allowed(self):
        """不在で戻る時刻が分からない：「後でかけてよい」と言われたときだけ、同じ日に「不在」の方針の間隔（試験の業務は1時間おき）で。
        言われなかった・聞いていない ⇒ 方針が「n 時間おき」でも、次の日より前にはかけない。"""
        for again, same_day in ((True, True), (False, False), (None, False)):
            with self.subTest(again=again):
                s = self.ready()
                now = self.tx(s, lambda tx: tx.one("SELECT now() AS t")["t"])
                got = self.settle(s, self.call(s, report=self.absent(again)))
                d = got["decision"]
                self.assertEqual((got["state"], d.kind, d.promised), ("not_reached", "call", False))
                if same_day:
                    self.assertLess(d.at, now + timedelta(hours=1, minutes=5))
                else:
                    self.assertGreaterEqual(d.at, self.tomorrow(s))

    def test_an_untimed_promise_not_transferred_moves_one_day_not_two(self):
        """時刻の決まっていない約束の電話で取り次げず、またかけてよいとは言われなかった：方針が「1日後」なら次の日（方針どおりの時刻が
        今日に当たるときだけ次の日へ送る＝_policy_at の1か所。⛔ 見直し15：前は次の日から数え始め、2日後になった）。"""
        from salescall.core.call_report import NotTransferred, Said
        keys = ("open", "incomplete", "not_reached_absent", "callback_unspecified_time")
        s = self.ready(rules=rules_raw(retry={k: {"action": "call", "max_times": 3, "interval": {"days": 1}} for k in keys}))
        self.settle(s, self.call(s, report=rep(callback=callback("later", time="あとで"))))
        nt = replace(rep(("reception",), not_reached="not_transferred"),
                     not_transferred=(NotTransferred(Said(1, "ただいまお繋ぎできかねます。"), None),))
        d = self.settle(s, self.past_attempt(s, list(TALK), report=nt, settled=False, verify=True))["decision"]
        self.assertEqual((d.kind, d.promised), ("call", True))
        self.assertGreaterEqual(d.at, self.tomorrow(s))
        self.assertLess(d.at, self.tomorrow(s) + timedelta(days=1))   # 2日後ではない

    def test_ask_number_again_waits_for_the_next_day_without_leave(self):
        """番号の聞き直しの電話で、目的のお客様に繋がらず、またかけてよいとは言われなかった：同じ日にはかけ直さない（not_today の
        1か所。⛔ 見直し15：前は番号の聞き直し・お詫びの電話で見ず、同じ日の別の時間帯にかけ直した）。"""
        s = self.ready()
        now = self.tx(s, lambda tx: tx.one("SELECT now() AS t")["t"])
        self.tx(s, lambda tx: support.schedule_call(tx, s["task"], now, contact_id=s["contact"], cause="試験：番号の聞き直し",
                                                       promised=True, purpose="ask_number_again", wrong_contact_id=s["contact"]))
        d = self.settle(s, self.call(s, report=self.absent(False)))["decision"]
        self.assertEqual((d.kind, d.target.purpose), ("call", "ask_number_again"))
        self.assertGreaterEqual(d.at, self.tomorrow(s))

    def test_the_answer_of_the_call_ending_record_counts(self):
        """同じ日にかけてよいかは、その通話を締めた「不在」「取り次げない」の記録のうち、いちばん新しいものの答え（1か所）。"""
        from salescall.core.call_report import NotTransferred, Said
        r = self.absent(True)
        self.assertTrue(r.may_call_again)
        later = replace(r, not_transferred=(NotTransferred(Said(5, "やはりお繋ぎできかねます。"), None),))
        self.assertFalse(later.may_call_again)                  # あとの「取り次げない」で聞いていない
        self.assertEqual(call_report.from_json(r.to_json()), r)   # 置いて読み直しても同じ

    def test_departed_with_the_successor_policy_chases_the_successor(self):
        """在籍していない・業務の応じ方が「後任の方をお呼びする」（既定）で、後任の方に繋がらないまま終わった：行を終えず、次の電話で
        後任のご担当者様をお呼びする（予約証の印 successor）。印は行が終わるまで引き継ぐ。回数・間隔は不在と同じ（同じ日は、
        またかけてよいと言われたときだけ）。"""
        s = self.ready()
        got = self.settle(s, self.call(s, report=self.departed("successor")))
        d = got["decision"]
        self.assertEqual((got["state"], d.kind, d.target.successor), ("not_reached", "call", True))
        self.assertGreaterEqual(d.at, self.tomorrow(s))
        self.assertEqual(self.tx(s, lambda tx: next_action.current(tx, s["task"]))["successor"], True)
        now = self.tx(s, lambda tx: tx.one("SELECT now() AS t")["t"])
        self.tx(s, lambda tx: support.schedule_call(tx, s["task"], now, contact_id=s["contact"], cause="試験：その時刻が来た",
                                                       promised=True, successor=True))   # 間隔の歯止めは門の試験で見る
        c = self.claim(s)
        self.assertEqual((type(c).__name__, getattr(c, "reason", None)), ("Claim", None))
        self.assertTrue(c.successor)
        self.tx(s, lambda tx: attempts.release(tx, [c.attempt_id]))
        got = self.settle(s, self.call(s, report=self.absent()))     # 後任の方も席を外していた
        self.assertEqual((got["state"], got["decision"].target.successor), ("not_reached", True))
        self.tx(s, lambda tx: support.schedule_call(tx, s["task"], now, contact_id=s["contact"], cause="試験：その時刻が来た",
                                                       promised=True, successor=True))   # 間隔の歯止めは門の試験で見る
        got = self.settle(s, self.call(s, items=(("Q1", "confirmed", None),)))   # 後任の方と話せて完了
        self.assertEqual(got["state"], "completed")

    def test_departed_ends_the_row_when_the_business_chose_to_end_or_no_successor_is_there(self):
        """業務で「お礼を言って終える」を選んでいた・後任の方へ切り替えたあとも在籍していない ⇒ 行を終える（理由 departed）。"""
        for report in (self.departed("end"), self.departed("successor", after_successor=True)):
            with self.subTest(report=report.departures):
                s = self.ready()
                got = self.settle(s, self.call(s, report=report))
                self.assertEqual((got["state"], got["decision"].kind, got["decision"].end_reason),
                                 ("not_reached", "end", "not_reached_departed"))

class StuckVerification(SettleBase):
    def test_a_verification_that_never_finishes_becomes_a_fix_request(self):
        """通話後の検証が、録音を待つ時間＋1時間たっても終わらない：CL-1 へ修正依頼（検証の部品が動かないと、行もお客様も待ち
        続けるため。2026-10-22 の見直し）。"""
        s = self.ready()
        aid = self.call(s, verify=None)
        self.assertEqual(self.tx(s, attempts.watch_stuck_verifications), 0)        # 終わったばかり：まだ
        from unittest import mock
        with mock.patch.object(attempts, "STUCK_AFTER", timedelta(days=-1)):    # 時間がたったことにする（試験で待たない）
            self.assertGreaterEqual(self.tx(s, attempts.watch_stuck_verifications), 1)
        asked = self.w.a.in_system(SYSTEM, lambda tx: [r for r in fix_requests.open_requests(tx, "CL-1")
                                                       if r["kind"] == "verification_stuck"])
        self.assertTrue(asked)


class CutOffCalls(SettleBase):
    """途中で切れた通話（お客様が出たあと、こちらの仕組みが止まった・回線が切れた）は、復旧次第、お詫びとともにすぐかけ直す
    （2026-10-17 ユーザー：「復旧次第、お詫びとともにすぐかけ直さないといけないでしょ！」）。回数・間隔には数えない。"""

    def resumed(self, got):
        d = got["decision"]
        self.assertEqual((d.kind, d.target.purpose, d.promised), ("call", "resume", True))

    def test_a_cut_call_that_cannot_be_judged_is_called_back_with_an_apology(self):
        """途中で切れて判定できない：通話のあとの確かめで断りの言葉が見つかっていなければ、お詫びのかけ直し（すぐ）。判定し直しの
        対象のまま（2026-10-22 ユーザー）。断りの言葉が見つかったときの営業拒否は、判定の最初の断りの読み（judge.refusal）が受け持つ。"""
        s = self.ready()
        cut = ("dialing", "ringing", "answered", "in_conversation", "technical_error")
        aid = self.call(s, cut, report=rep(cut=None), items=(("Q1", "dont_know", None),))   # 理由の無い「わからない」＝判定できない
        got = self.settle(s, aid)
        self.assertEqual(got["state"], "unknown")
        self.resumed(got)
        self.assertEqual(self.tx(s, lambda tx: next_action.current(tx, s["task"]))["apology_reasons"], ["cut_off"])
        self.assertEqual(self.tx(s, lambda tx: settle.awaiting_rejudge(tx, s["task"])), aid)

    def test_a_cut_call_with_a_promise_that_cannot_be_judged_keeps_the_promise(self):
        """途中で切れて判定できない通話でも、会話の報告にかけ直しの申し出（約束）があれば、すぐにはかけず約束を守り、お詫びから
        （ユーザー決定7と同じ扱い。時刻の読めない約束は、時刻の決まっていない約束の間隔。⛔ 見直し15：前はすぐかけ直した）。"""
        s = self.ready()
        cut = ("dialing", "ringing", "answered", "in_conversation", "technical_error")
        aid = self.call(s, cut, report=rep(callback=callback("later", time="ほにゃらら時"), cut=None))
        got = self.settle(s, aid)
        d = got["decision"]
        self.assertEqual(got["state"], "unknown")
        self.assertEqual((d.kind, d.target.purpose, d.promised, d.window), ("call", "normal", True, None))
        self.assertGreaterEqual(d.at, self.tx(s, lambda tx: tx.one("SELECT now() + interval '59 minutes' AS t")["t"]))   # 方針の間隔
        self.assertEqual(self.tx(s, lambda tx: next_action.current(tx, s["task"]))["apology_reasons"], ["cut_off"])
        self.assertEqual(self.tx(s, lambda tx: settle.awaiting_rejudge(tx, s["task"])), aid)

    def test_a_line_drop_while_the_customer_talked_is_called_back_with_an_apology(self):
        s = self.ready()
        aid = self.call(s, ("dialing", "ringing", "answered", "in_conversation", "dropped"), side="line",
                        report=rep(cut=Cut("callee", False, False)))
        self.resumed(self.settle(s, aid))
        c = self.claim(s)                                               # すぐ（間隔をあけずに）かけられる
        self.assertEqual((type(c).__name__, getattr(c, "purpose", None)), ("Claim", "resume"))

    def trouble_cut(self, s, reason="ai_malfunction"):
        """お客様と話している途中で、こちらのプログラムの不具合で終えた電話（門を通さずに作る。締める）。"""
        aid = self.w.insert_claim(s)
        for st in ("dialing", "ringing", "answered", "in_conversation"):
            self.w.move(s, aid, st)
        self.assertTrue(self.tx(s, lambda tx: attempts.end_for_trouble(tx, aid, reason)))
        self.tx(s, lambda tx: attempts.record_verification(tx, aid, summary=CLEAN))
        return aid, self.settle(s, aid)

    def test_our_own_troubles_neither_count_nor_restart_the_customers_cuts_and_are_always_called_back(self):
        """こちらのプログラムの不具合でお客様と話している途中に終えた電話は、いつも「途中で切れた」（行の回数に数えない）で、お詫びとともに
        かけ直す（salescall.dial_kind・resume_allowed の is_ours。0139）。お客様の電波などで切れた続きの数（cuts_in_a_row）には入れず、
        数え直しもしない（2026-10-25 ユーザー：こちらの不具合は番号にも回線にも行の回数にも数えない。続けば行を休ませる）。「声が
        聞き取れない」は、こちらの不具合でなく、お客様の側で切れた電話と同じに数える（0141）。"""
        s = self.ready()
        line_cut = ("dialing", "ringing", "answered", "in_conversation", "dropped")
        kinds = []
        self.past_attempt(s, line_cut, side="line", report=rep(cut=Cut("callee", False, False)), verify=True)
        for _ in range(2):
            aid, got = self.trouble_cut(s)
            self.assertEqual(got["decision"].target.purpose, "resume")
            kinds.append(self.tx(s, lambda tx: attempts.kind_of(tx, aid)))
        for _ in range(2):   # お客様の側で切れた2回目・3回目（こちらの不具合は数えも数え直しもしない＝3回目は数える）
            aid = self.past_attempt(s, line_cut, side="line", report=rep(cut=Cut("callee", False, False)), verify=True)
            kinds.append(self.tx(s, lambda tx: attempts.kind_of(tx, aid)))
        self.assertEqual(kinds, ["cut_off", "cut_off", "cut_off", "reached"])
        aid, got = self.trouble_cut(s, "ai_malfunction")                 # 続いたお客様の切れのあとでも、こちらの不具合は数えない
        self.assertEqual(self.tx(s, lambda tx: attempts.kind_of(tx, aid)), "cut_off")
        self.resumed(got)
        # 「声が聞き取れない」は、こちらの不具合でなく、お客様の側で切れた電話と同じ（2026-10-25 夜 管理者の決定 案A。0141）：続けて
        # 切れた数に入り、すぐのかけ直しの回を超えていれば、ふつうの電話の回数に数える（すぐにはかけ直さない）
        aid, got = self.trouble_cut(s, "poor_audio")
        self.assertEqual(self.tx(s, lambda tx: attempts.kind_of(tx, aid)), "reached")
        self.assertNotEqual(got["decision"].target.purpose if got["decision"].target else None, "resume")

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
        # すぐのかけ直しはやめても、次にお客様につながったときはお詫びから（⛔ 見直し15：前は3回目でお詫びの印も外れた）
        self.assertEqual(self.tx(s, lambda tx: next_action.current(tx, s["task"]))["apology_reasons"], ["cut_off"])

    def test_cuts_beyond_the_call_backs_count_and_the_row_ends(self):
        """お詫びしてすぐかけ直す（続けて2回まで）を超えて、なお続けて切れた通話は、ふつうの電話の回数に数え、ふつうの決まりで終わる
        （2026-10-18 ユーザー 案A：「同じ行で続けて2回まで。それでも切れたら、ふつうのかけ直しの決まり」。決めるのはデータベースの
        dial_kind の1か所。⛔ 見直し16：前は続けて何回切れても数えず、電波の悪いお客様へ終わりなくかけ続けた）。"""
        s = self.ready()
        cut = ("dialing", "ringing", "answered", "in_conversation", "dropped")
        got = []
        for _ in range(10):
            # 試験：ふつうのかけ直しの時刻・間隔を待たずに、次の発信を置く（門の間隔の守りは別の試験）
            aid = self.past_attempt(s, cut, side="line", report=rep(cut=Cut("callee", False, False)), verify=True, settled=False)
            d = self.settle(s, aid)["decision"]
            got.append(d.target.purpose if d.kind == "call" and d.target else d.end_reason)
            if d.kind != "call":
                break
        self.assertEqual(got[:2], ["resume", "resume"])
        self.assertNotEqual(got[2], "resume")                                  # 3回目に続けて切れたら、ふつうのかけ直し
        self.assertLess(len(got), 10)
        self.assertTrue(got[-1] == "max_attempts" or got[-1].startswith("policy_max:"), got)

    def test_only_a_reached_call_restarts_the_count_of_cuts_in_a_row(self):
        """続けて切れた数（salescall.cuts_in_a_row。すぐのお詫びのかけ直しを何回までするか・回数に数え始めるかの1か所）を0に戻すのは、
        お客様に届いて途中で切れなかった発信だけ。発信に失敗（鳴っていない）・お客様が出る前の回線の失敗は、はさまっても数え直さない
        （⛔ 見直し16b：前は、どの発信がはさまっても数え直し、「切れた → 回線の失敗 → 切れた」が交互に続くと、どの回数にも数えない
        まま、すぐのかけ直しを続けえた）。"""
        cut = ("dialing", "ringing", "answered", "in_conversation", "dropped")
        between = {"回線の失敗": ("dialing", "ringing", "technical_error"), "発信に失敗": ("dialing", "call_failed"),
                   "話せた": ("dialing", "ringing", "answered", "in_conversation", "ending", "ended")}
        for name, expect in (("回線の失敗", 2), ("発信に失敗", 2), ("話せた", 1)):
            with self.subTest(between=name):
                s = self.ready()

                def one(path):
                    aid = self.past_attempt(s, path, side="line", verify="answered" in path, settled=False,
                                            report=rep(cut=Cut("callee", False, False)) if path is cut else None)
                    self.settle(s, aid)
                    return aid
                one(cut)
                mid = one(between[name])
                last = one(cut)
                self.assertEqual(self.tx(s, lambda tx: tx.one("SELECT salescall.cuts_in_a_row(%s) AS n", (last,)))["n"], expect)
                # すぐかけ直してよいかは、その発信が切れたときだけ（salescall.resume_allowed の1か所。⛔ 見直し17b：前は「続けて切れた
                # 数が1以上」で代わりにしていて、間の回線の失敗・発信に失敗でも、前に切れた数が残れば真になった）
                allowed = lambda aid: self.tx(s, lambda tx: tx.one("SELECT salescall.resume_allowed(%s) AS ok", (aid,)))["ok"]
                self.assertEqual((allowed(mid), allowed(last)), (False, True))

    def test_rejudging_an_old_cut_call_does_not_revive_the_ended_row_nor_hold_the_customer(self):
        """途中で切れて判定できなかった通話A（お詫びのかけ直しを予定）のあと、かけ直しにも、そのあとの電話にも出ないまま行が終わった。
        (1) 同じお客様のほかの行は止めない：途中で切れた通話は、通話のあとの確かめで断りの言葉が見つからなければ、断られていない
        （2026-10-22 ユーザー決定8。データベースの salescall.refusal_undecided＝0118）。
        (2) あとで判定の原因が直って A を判定し直しても、終えた行は生き返らない（あとに発信があれば、結果の版だけを足し、予定は書き
        直さない＝settle._judge_and_write の1か所。⛔ 見直し16：前は「すぐお詫びのかけ直し」が書かれた）。"""
        import uuid as _uuid
        s = self.ready()
        run2 = self.w.run(s["tenant"], s["client"], s["business"], s["version"])
        other = self.w.task(s["tenant"], s["client"], run2, s["contact"])
        aid = self.call(s, ("dialing", "ringing", "answered", "in_conversation", "technical_error"), report=rep(cut=None),
                        items=(("Q1", "dont_know", None),))
        first = self.settle(s, aid)
        self.assertEqual((first["state"], first["decision"].target.purpose), ("unknown", "resume"))
        undecided = lambda: self.tx(s, lambda tx: tx.one("SELECT salescall.refusal_undecided(%s) AS u", (aid,)))["u"]   # noqa: E731
        self.assertFalse(undecided())                                          # (1) 断られていない
        self.assertIsNone(self.tx(s, lambda tx: dnc.undecided_reach(tx, other)))
        for _ in range(10):                                                    # 予定どおりにかけて、出ない
            cur = self.tx(s, lambda tx: next_action.current(tx, s["task"]))
            if cur["kind"] != "call":
                break
            b = _uuid.uuid4()
            self.tx(s, lambda tx: tx.execute(
                "INSERT INTO salescall.attempts (tenant_id, client_id, attempt_id, task_id, contact_id, phone_e164, line_id,"
                " version_id, state, claimed_at, claim_expires_at, purpose, promised)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'claimed', now(), now() + interval '1 minute', %s, %s)",
                (s["tenant"], s["client"], b, s["task"], s["contact"], s["phone"], s["line"], s["version"], cur["purpose"],
                 cur["promised"])))
            for st in ("dialing", "no_answer"):
                self.w.move(s, b, st)
            self.settle(s, b)
        ended = self.tx(s, lambda tx: next_action.current(tx, s["task"]))
        self.assertEqual(ended["kind"], "end")
        self.assertEqual(self.tx(s, lambda tx: settle.awaiting_rejudge(tx, s["task"])), aid)   # 結果を直すための判定し直しは残る
        self.tx(s, lambda tx: call_report.save_rebuilt(tx, aid, rep(cut=Cut("ai", False, False)), evidence="試験"))
        again = self.tx(s, settle.rejudge_held)
        self.assertEqual([g["decision"] for g in again], [None])               # (2) 結果の版だけ
        self.assertEqual(self.tx(s, lambda tx: next_action.current(tx, s["task"]))["seq"], ended["seq"])

    def test_a_call_cut_by_our_failure_is_called_back_with_an_apology(self):
        s = self.ready()
        aid = self.call(s, ("dialing", "ringing", "answered", "in_conversation", "technical_error"), report=rep(cut=None))
        self.resumed(self.settle(s, aid))

    def test_a_failure_after_the_closing_started_is_not_a_cut(self):
        """締めに入ったあと（締めた理由がある・切れた瞬間が締めの言葉の中）にこちらの仕組みが止まった通話は、話の途中ではない：
        すぐお詫びの電話をかけない（⛔ 2026-10-22 の見直し14：前は、締めの言葉のあとで止まった通話にも、すぐかけた）。"""
        from salescall.core.call_report import End, Said
        for case, report in (("締めの言葉の中", rep()),
                             ("締めた理由がある", replace(rep(cut=None), ends=(End("done", Said(3, "失礼いたします")),)))):
            with self.subTest(case):
                s = self.ready()
                aid = self.call(s, ("dialing", "ringing", "answered", "in_conversation", "technical_error"), report=report)
                d = self.settle(s, aid)["decision"]
                self.assertNotEqual(d.target and d.target.purpose, "resume")
                self.assertFalse(self.tx(s, lambda tx: tx.one(
                    "SELECT cut_off FROM salescall.attempts WHERE attempt_id = %s", (aid,)))["cut_off"])

    def test_a_call_back_keeps_the_promised_window(self):
        """幅の約束の電話が途中で切れた：すぐのかけ直しも、同じ約束の回のまま（話せなければ、理由に合わせた次の回へ）。"""
        s = self.ready()
        now = self.tx(s, lambda tx: tx.one("SELECT now() AS t")["t"])
        window = next_action.Window(now + timedelta(days=1), (0, 24 * 3600), None, now - timedelta(hours=1), "day")
        self.tx(s, lambda tx: support.schedule_call(tx, s["task"], now, contact_id=s["contact"], cause="試験", promised=True,
                                                       window=window))
        aid = self.call(s, ("dialing", "ringing", "answered", "in_conversation", "technical_error"), report=rep(cut=None))
        got = self.settle(s, aid)
        self.resumed(got)
        self.assertEqual(got["decision"].window, window)

    def test_a_call_back_keeps_the_promised_exact_time(self):
        """時刻ちょうどの約束の電話が途中で切れた：すぐのかけ直しも、お客様が指定した時刻を持つ（NG時間の枠の中の約束でも、門が
        止めない。見直し7で試験を足した）。"""
        s = self.ready()
        t = self.tx(s, lambda tx: tx.one("SELECT date_trunc('minute', now()) AS t")["t"])
        window = self.tx(s, lambda tx: next_action._exact_window(tx, s["task"], t, day_said=True))
        self.tx(s, lambda tx: support.schedule_call(tx, s["task"], t, contact_id=s["contact"], cause="試験", promised=True,
                                                       callee_at=t, window=window))
        got = self.settle(s, self.call(s, ("dialing", "ringing", "answered", "in_conversation", "technical_error"),
                                       report=rep(cut=None)))
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
                    aid = self.call(s, cut, report=rep(callback=callback("later", time="あとで"), cut=None))
                else:
                    now = self.tx(s, lambda tx: tx.one("SELECT now() AS t")["t"])
                    self.tx(s, lambda tx: support.schedule_call(tx, s["task"], now, contact_id=s["contact"],
                                                                cause="試験：時刻の決まっていない約束", promised=True))
                    aid = self.call(s, cut, report=rep(cut=None))
                if case == "said_in_the_cut_call":
                    # 「あとで」と言ったお客様の電話が途中で切れた：すぐにはかけず、約束の間隔で、お詫びから始める（2026-10-22
                    # ユーザー：「OK」＝「すぐにはかけず、『あとで』の約束の間隔でかけて、お詫びから始めます」）
                    d = self.settle(s, aid)["decision"]
                    self.assertEqual((d.kind, d.promised, d.target.purpose), ("call", True, "normal"))
                    self.assertIn("時刻の決まっていない約束", d.rule)
                    self.assertEqual(self.tx(s, lambda tx: next_action.current(tx, s["task"]))["apology_reasons"], ["cut_off"])
                    continue
                self.resumed(self.settle(s, aid))
                d = self.settle(s, self.call(s, ["dialing", "no_answer"]))["decision"]
                self.assertEqual((d.kind, d.promised, d.target.purpose), ("call", True, "normal"))
                self.assertIn("時刻の決まっていない約束", d.rule)

    def test_the_apology_for_a_cut_is_kept_until_the_callee_is_reached(self):
        """途中で切れた：次の予定にお詫びの印（cut_off）。予約証が会話の処理へ渡し、すぐのかけ直しに出なかったあとも引き継ぐ。
        外れるのは、目的のお客様と話せた締めだけ（⛔ 2026-10-22 の見直し14：前は続きの電話に出ないと、お詫びが消えた）。"""
        s = self.ready()
        cut = ("dialing", "ringing", "answered", "in_conversation", "dropped")
        self.settle(s, self.call(s, cut, side="line", report=rep(cut=Cut("callee", False, False))))
        reasons = lambda: self.tx(s, lambda tx: next_action.current(tx, s["task"]))["apology_reasons"]   # noqa: E731
        self.assertEqual(reasons(), ["cut_off"])
        c = self.claim(s)
        self.assertEqual((c.purpose, c.apology), ("resume", ("cut_off",)))
        self.tx(s, lambda tx: attempts.release(tx, [c.attempt_id]))
        self.settle(s, self.call(s, ["dialing", "no_answer"]))                 # 続きの電話に出なかった
        self.assertEqual(reasons(), ["cut_off"])
        now = self.tx(s, lambda tx: tx.one("SELECT now() AS t")["t"])
        self.tx(s, lambda tx: support.schedule_call(tx, s["task"], now, contact_id=s["contact"],
                                                       cause="試験：その時刻が来たことにする", promised=True))
        c = self.claim(s)
        self.assertEqual(c.apology, ("cut_off",))
        self.tx(s, lambda tx: attempts.release(tx, [c.attempt_id]))
        self.settle(s, self.call(s))                                          # 目的のお客様と話せた
        self.assertEqual(reasons(), [])

    def test_whether_a_call_was_cut_is_kept_once_on_the_attempt(self):
        """途中で切れたかは、締めの処理が試行に1回だけ残す（データベースが守る＝0113）。続けて切れた数は、これを数える。"""
        s = self.ready()
        aid = self.call(s, ("dialing", "ringing", "answered", "in_conversation", "dropped"), side="line",
                        report=rep(cut=Cut("callee", False, False)))
        self.settle(s, aid)
        self.assertTrue(self.tx(s, lambda tx: tx.one("SELECT cut_off FROM salescall.attempts WHERE attempt_id = %s",
                                                     (aid,)))["cut_off"])
        with self.assertRaises(Rejected) as e:
            self.tx(s, lambda tx: attempts.record_cut_off(tx, aid, False))
        self.assertEqual(e.exception.constraint, "attempts_cut_off_once")
        c = self.claim(s)
        with self.assertRaises(Rejected):                                      # 終わる前には残せない
            self.tx(s, lambda tx: attempts.record_cut_off(tx, c.attempt_id, True))

    def test_the_time_the_callee_spoke_is_the_report_when_the_end_was_recorded_later(self):
        """お客様が言った「30分後」の起点：会話の処理が報告をしまった時刻が、通話の終わりの記録より早ければ、そちら（途中の誤りで
        終わった通話は、後始末した時刻が終わりになる＝2026-10-19 の見直し）。"""
        s = self.ready()
        c = self.claim(s)
        self.assertIsInstance(c, gate.Claim, c)
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
                        report=rep(cut=Cut("ai", False, True)))             # 断りの言葉のあとに、お客様が切った＝断った
        self.assertEqual(self.settle(s, aid)["decision"].end_reason, "do_not_call")
        t = self.ready()
        aid = self.call(t, ("dialing", "technical_error"))               # お客様が出る前に止まった：お詫びは要らない
        got = self.settle(t, aid)["decision"]
        self.assertNotEqual(got.target and got.target.purpose, "resume")


class UnclearCutChecks(SettleBase):
    """切れ方の分からない通話の確かめのかけ直し（2026-10-30 管理者の決定）：「営業拒否の言葉が無く切れ方が分からない通話は、すぐ確かめの
    かけ直しをする。かけ直しがお客様の電話まで鳴ったら両方の回線は元気＝営業拒否（お客様が出て話を続けたら、その話の結果どおり）。
    鳴らなかったら回線のせい＝15分（テスト番号のあける長さの並びの1つ目）おいてお詫びからかけ直す。同じお客様に確かめのかけ直しは1回だけ」。
    決まるまでは、発信禁止を登録しない・行を終えない・結果を確定しない・同じお客様のほかの行を門で待たせる（取り消せない決定の洗い出し）。
    声が届かないお客様が黙って切った通話は、営業拒否にしない（2026-10-30 管理者の指摘。「声が聞き取れない」と同じ）。"""

    SILENT = ("dialing", "ringing", "answered", "in_conversation", "dropped")   # お客様が出て話し、何も言わずに切った

    def unclear(self, s, **kw):
        """AI が話している途中に、お客様が何も言わずに切った（営業拒否の言葉も、声が届かなかった証拠も無い）通話を締める。"""
        aid = self.call(s, self.SILENT, side="callee", report=rep(cut=Cut("ai", False, False)), **kw)
        return aid, self.settle(s, aid)

    def come(self, s):
        """いまの予定の時刻が来た（試験の近道。かける先・目的は同じで、時刻だけいま。同じお客様への最短の間隔で門に止められないよう、
        約束の印をつける＝確かめのかけ直しかどうかには効かない）。"""
        cur = self.tx(s, lambda tx: next_action.current(tx, s["task"]))
        self.tx(s, lambda tx: next_action._append(tx, s["task"], call_at=None, call_now=True, end_reason=None, cause="試験：その時刻が来た",
                                                  promised=True, target=next_action.Target(cur["contact_id"], None, cur["purpose"])))

    def other_row(self, s):
        run2 = self.w.run(s["tenant"], s["client"], s["business"], s["version"])   # 同じお客様の、ほかの一覧の行（番号を共有する）
        return self.w.task(s["tenant"], s["client"], run2, s["contact"])

    def released(self, s, aid):
        return self.tx(s, lambda tx: tx.one(
            "SELECT count(*) AS n FROM salescall.result_releases l JOIN salescall.task_results r"
            " ON r.task_id = l.task_id AND r.version_no = l.version_no WHERE r.attempt_id = %s", (aid,)))["n"]

    def dnc_reasons(self, s):
        return sorted(r["r"] for r in self.tx(s, lambda tx: tx.all(
            "SELECT reason::text AS r FROM salescall.dnc_entries WHERE phone_e164 = %s AND revoked_at IS NULL", (s["phone"],))))

    def test_until_the_check_call_nothing_irreversible_is_decided(self):
        s = self.ready()
        other = self.other_row(s)
        aid, got = self.unclear(s)
        d = got["decision"]
        self.assertEqual((got["state"], d.kind, d.promised, d.target.purpose, d.check_of), ("incomplete", "call", True, "resume", aid))
        self.assertIn("cut_off", self.tx(s, lambda tx: next_action.current(tx, s["task"]))["apology_reasons"])   # お詫びから
        self.assertEqual(self.dnc_reasons(s), [])                                         # 発信禁止を登録しない
        self.assertIsNone(self.tx(s, lambda tx: tx.one("SELECT disconnect FROM salescall.attempts WHERE attempt_id = %s",
                                                       (aid,)))["disconnect"])            # 切れ方もまだ決めない
        self.assertEqual(self.released(s, aid), 0)                                        # 結果を確定しない
        self.assertTrue(self.tx(s, lambda tx: tx.one("SELECT salescall.refusal_undecided(%s) AS u", (aid,)))["u"])
        self.assertEqual(self.tx(s, lambda tx: gate.pacing(tx, other)).binding, "related_decision_pending")   # ほかの行は待つ
        self.assertIn("related_decision_pending", gate.REASONS)

    def test_a_check_call_that_rang_without_an_answer_is_a_refusal(self):
        """営業拒否の言葉が無い × かけ直しが鳴って出なかった ⇒ 営業拒否とみなす（発信禁止の台帳には、はっきり断られたときと別の理由。
        広げ方は営業拒否と同じ＝同じお客様のほかの行も止まる）。"""
        s = self.ready()
        other = self.other_row(s)
        aid, _ = self.unclear(s)
        check = self.call(s, ("dialing", "ringing", "no_answer"))
        self.assertEqual(self.tx(s, lambda tx: next_action.check_outcome(tx, check)).outcome, "rang")
        got = self.settle(s, check)
        self.assertEqual((got["state"], got["decision"].end_reason), ("do_not_call", "do_not_call"))
        self.assertEqual(self.dnc_reasons(s), ["refused_unclear_checked"])
        self.assertFalse(self.tx(s, lambda tx: tx.one("SELECT salescall.refusal_undecided(%s) AS u", (aid,)))["u"])
        self.assertNotIn(self.tx(s, lambda tx: gate.pacing(tx, other)).binding, ("related_decision_pending", "related_unsettled"))
        self.tx(s, lambda tx: next_action.schedule_now(tx, other, cause="試験：ほかの行の最初の予定"))
        self.assertEqual(self.reason(s, task=other), "dnc_client")                      # ほかの行も止まった
        self.assertEqual(self.released(s, check), 1)                                       # 確かめの締めの版が確定する

    def test_a_check_call_the_customer_answered_follows_that_talk(self):
        """営業拒否の言葉が無い × かけ直しが鳴って、お客様が出て話を続けた ⇒ その話の結果どおり（営業拒否にしない）。"""
        s = self.ready()
        aid, _ = self.unclear(s)
        check = self.call(s, items=[("Q1", "confirmed", None)])
        got = self.settle(s, check)
        self.assertEqual((got["state"], got["decision"].end_reason), ("completed", "completed"))
        self.assertEqual(self.dnc_reasons(s), [])

    def test_a_check_call_that_did_not_ring_is_retried_with_an_apology_after_the_first_gap(self):
        """営業拒否の言葉が無い × かけ直しが鳴らなかった ⇒ 回線のせいとして、テスト番号をあける長さの並びの1つ目（15分）おいて、
        お詫びからかけ直す。そのかけ直しは確かめのかけ直しではない（同じお客様に1回だけ）＝鳴って出なくても営業拒否にしない。"""
        s = self.ready()
        aid, _ = self.unclear(s)
        check = self.call(s, ("dialing", "call_failed"))
        self.assertEqual(self.tx(s, lambda tx: next_action.check_outcome(tx, check)).outcome, "unrung")
        got = self.settle(s, check)
        d = got["decision"]
        ended = self.tx(s, lambda tx: tx.one("SELECT ended_at FROM salescall.attempts WHERE attempt_id = %s", (check,)))["ended_at"]
        self.assertEqual((d.kind, d.promised, d.target.purpose, d.check_of), ("call", True, "resume", None))
        self.assertGreaterEqual(d.at, ended + timedelta(minutes=15))
        self.assertIn("cut_off", self.tx(s, lambda tx: next_action.current(tx, s["task"]))["apology_reasons"])
        self.assertFalse(self.tx(s, lambda tx: tx.one("SELECT salescall.refusal_undecided(%s) AS u", (aid,)))["u"])
        self.come(s)
        later = self.call(s, ("dialing", "ringing", "no_answer"))
        self.assertIsNone(self.tx(s, lambda tx: next_action.check_outcome(tx, later)))
        self.assertNotEqual(self.settle(s, later)["state"], "do_not_call")
        self.assertEqual(self.dnc_reasons(s), [])
        # もう一度、決められない切れ方になっても、確かめのかけ直しはしない（同じお客様に1回だけ）
        self.come(s)
        again, got = self.unclear(s)
        self.assertIsNone(got["decision"].check_of)

    def test_a_silent_hang_up_by_a_customer_who_could_not_hear_is_not_a_refusal(self):
        """声が届かないお客様が黙って切った通話は、営業拒否にしない（2026-10-30 管理者の指摘）。「声が聞き取れない」と同じ扱い：こちらの
        不具合に数えない・途中で切れた電話と同じ（お詫びとともにかけ直す）・状態の表の E4（声が通ると分かっている回線から＝R-11）。"""
        s = self.ready()
        aid = self.call(s, self.SILENT, side="callee", report=rep(cut=Cut("ai", False, False, callee_cannot_hear=True)))
        self.assertEqual(self.tx(s, lambda tx: health.call_event(tx, aid)), "E4")
        got = self.settle(s, aid)
        self.tx(s, lambda tx: health.after_settle(tx, aid))   # 締めのあと（回線ごとの処理の締めの仕事と同じ＝supervisor/jobs）
        self.assertEqual((got["state"], got["decision"].target.purpose, got["decision"].check_of), ("incomplete", "resume", None))
        self.assertEqual(self.dnc_reasons(s), [])
        row = self.tx(s, lambda tx: tx.one("SELECT disconnect, cut_off, trouble FROM salescall.attempts WHERE attempt_id = %s", (aid,)))
        self.assertEqual((row["disconnect"], row["cut_off"], row["trouble"]), ("interrupted", True, None))
        self.assertEqual(self.tx(s, lambda tx: support.task_stage(tx, s["task"])), "voice_redial")
        self.assertIn(s["contact"], {uuid.UUID(str(c)) for c in self.tx(s, lambda tx: health.line_of(tx, s["line"])).audio})

    def test_the_database_keeps_one_check_per_row_and_never_changes_it(self):
        """確かめのかけ直しは同じ行で1回だけ（表 unclear_checks は行ごとに1行。書き換え・消しはできない）。"""
        s = self.ready()
        aid, _ = self.unclear(s)
        check = self.call(s, ("dialing", "ringing", "no_answer"))
        with self.assertRaises(Rejected):
            self.tx(s, lambda tx: tx.execute("INSERT INTO salescall.unclear_checks (tenant_id, client_id, task_id, attempt_id)"
                                             " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s)", (s["task"], check)))
        for sql in ("UPDATE salescall.unclear_checks SET decided_at = now() WHERE task_id = %s AND attempt_id <> %s",
                    "DELETE FROM salescall.unclear_checks WHERE task_id = %s AND attempt_id <> %s"):
            with self.subTest(sql=sql[:6]), self.assertRaises(Rejected):   # 表の持ち主でも（権限ではなく、引き金が守る）
                owner_access().in_tenant(SYSTEM, s["tenant"], lambda tx: tx.execute(sql, (s["task"], check)))

    def test_the_retired_reason_found_after_is_not_written(self):
        """通話後の検証で見つけた断り（refused_found_after）は、書く入口がどこにも無かった（取り消せない決定の洗い出し）。あとから見つける
        道は録音から作り直した報告の1本にそろえ、この理由ではもう書かない（データベースが守る）。"""
        s = self.ready()
        with self.assertRaises(Rejected):
            self.tx(s, lambda tx: tx.execute(
                "INSERT INTO salescall.dnc_entries (tenant_id, client_id, phone_e164, reason, registered_by)"
                " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, 'refused_found_after', 'test')", (s["phone"],)))


class DialNotices(SettleBase):
    """お客様の番号へかけて、電話網のお知らせの声が返った電話（取り消せない決定の洗い出し。2026-10-25 夜 管理者のご決定＝テスト番号の
    話し中・電源オフ・圏外は、こちらの回線から発信できた証拠、と同じ当てはめ。0151）。"""

    def notice_call(self, s, notice):
        c = self.claim(s)
        self.assertTrue(dial_exit.dial(self.w.a, SYSTEM, s["tenant"], s["client"], c.attempt_id, FakeDriver()))
        self.tx(s, lambda tx: attempts.end_by_notice(tx, c.attempt_id, notice))
        self.tx(s, lambda tx: health.after_call(tx, c.attempt_id))
        return c.attempt_id

    def test_power_off_and_out_of_range_are_the_same_as_no_answer(self):
        """電源オフ・圏外：こちらの回線は元気（確かめ中にしない）・番号のせいにも数えない＝「お客様が出なかった」と同じ（ふつうの方針）。"""
        for notice in ("power_off", "out_of_range"):
            with self.subTest(notice):
                s = self.ready()
                aid = self.notice_call(s, notice)
                self.assertEqual(self.tx(s, lambda tx: health.call_event(tx, aid)), "E1")
                self.assertFalse(self.tx(s, lambda tx: support.line_checking(tx, s["line"])))
                self.assertEqual(self.tx(s, lambda tx: support.number_run(tx, s["contact"]))["n"], 0)
                got = self.settle(s, aid, decide=False)
                self.assertEqual((got["state"], got["decision"].kind, got["decision"].promised), ("open", "call", False))

    def test_not_in_use_alone_is_the_numbers_fault(self):
        """「この番号は現在使われておりません」のお知らせだけが番号のせい：テスト番号を待たずに決まる（回線は確かめ中にしない）。"""
        s = self.ready()
        aid = self.notice_call(s, "not_in_use")
        self.assertFalse(self.tx(s, lambda tx: support.line_checking(tx, s["line"])))
        self.assertEqual(self.tx(s, lambda tx: health.blame_of(tx, aid)), "number")
        self.assertEqual(self.tx(s, lambda tx: support.number_run(tx, s["contact"]))["n_sure"], 1)
        got = self.settle(s, aid, decide=False)
        self.assertEqual((got["state"], got["decision"].kind), ("open", "call"))

    def test_the_database_keeps_the_notice_with_its_ending_once(self):
        """お知らせの種類は、その終わり方（電源オフ・圏外＝出なかった／使われていない＝発信に失敗）とだけ、進行中の試行を終えるときに
        1回だけ残る（データベースが守る）。"""
        s = self.ready()
        c = self.claim(s)
        self.assertTrue(dial_exit.dial(self.w.a, SYSTEM, s["tenant"], s["client"], c.attempt_id, FakeDriver()))
        with self.assertRaises(Rejected):   # 使われていないのお知らせで「出なかった」にはしない
            self.tx(s, lambda tx: tx.execute("UPDATE salescall.attempts SET state = 'no_answer', notice = 'not_in_use'"
                                             " WHERE attempt_id = %s", (c.attempt_id,)))
        self.tx(s, lambda tx: attempts.end_by_notice(tx, c.attempt_id, "power_off"))
        with self.assertRaises(Rejected):   # 終えたあとに書き換えない
            self.tx(s, lambda tx: tx.execute("UPDATE salescall.attempts SET notice = 'out_of_range' WHERE attempt_id = %s",
                                             (c.attempt_id,)))

    def test_an_unknown_notice_is_a_failed_dial_decided_by_evidence(self):
        """お知らせの種類が分からない（None）：今までどおり発信に失敗（だれのせいかは証拠で）。知らない名前は受け取らない。"""
        s = self.ready()
        aid = self.notice_call(s, None)
        self.assertEqual(self.tx(s, lambda tx: health.blame_of(tx, aid)), "pending")
        self.assertTrue(self.tx(s, lambda tx: support.line_checking(tx, s["line"])))
        with self.assertRaises(ValueError):
            attempts.end_by_notice(None, aid, "busy_tone")
        self.assertEqual(set(attempts.NOTICE_ENDS) - {None}, set(phone.NOTICES))   # 名前の正本は link/phone.NOTICES


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
        # お客様が言った幅はお客様の指定：依頼者の時間帯・NG時間の外でもかける。口にした時刻（「夜10時から11時」）は深夜早朝の歯止め
        # の外でも（2026-10-22 ユーザー：「お客様が指定した時刻ならNG時間でもかけてよい（歯止めなし…）」）。端の決まっていない側（「午後」
        # の24時）は、お客様指定の歯止め時間（callee.designated_limit。初め 7〜22時）で埋めて切る（2026-10-23 ユーザー。⛔ 見直し15：
        # 前は「午後なら」で23時にかけえた。依頼者の時間帯では切らない）。決まりは calling_hours.callee_range の1か所
        late = next_action.Window(at(d2, 23), (22 * 3600, 23 * 3600), None, at(d2, 22), "day", said=(True, True))
        self.put_client(s, "calling.hours", {k: [["09:00", "18:00"]] for k in KEYS})
        self.put_client(s, "callee.designated_limit", ["07:00", "22:00"])   # 初めの値（試験の土台は1日じゅうに替えている）
        self.assertEqual(self.opening(s, at(d2, 9), late), (at(d2, 22), late))   # 依頼者の時間帯（〜18時）・歯止め（〜22時）の外
        afternoon = next_action.Window(at(d2 + timedelta(days=1), 0), (13 * 3600, 24 * 3600), None, at(d2, 0), "day")   # 「午後」
        self.assertEqual(self.opening(s, at(d2, 19), afternoon), (at(d2, 19), afternoon))   # 依頼者の18時を越えて22時まで
        self.assertEqual(self.opening(s, at(d2, 22), afternoon),
                         (at(d2 + timedelta(days=1), 13),
                          replace(afternoon, until=at(d2 + timedelta(days=2), 0), since=at(d2 + timedelta(days=1), 0), round=1)))
        self.tx(s, lambda tx: tx.execute(   # 業務ごとのお客様指定の歯止め時間（2026-10-23 ユーザー：業務ごとに選べる）
            "INSERT INTO salescall.setting_values (key, scope, tenant_id, client_id, business_id, value, status, rationale,"
            " review_when, set_by) VALUES ('callee.designated_limit', 'business', %s, %s, %s, '[\"08:00\", \"21:00\"]', 'decided',"
            " '試験', '試験', '試験')", (s["tenant"], s["client"], s["business"])), OPERATOR)
        self.assertEqual(self.opening(s, at(d2, 21), afternoon)[0], at(d2 + timedelta(days=1), 13))
        self.put_client(s, "calling.hours", {k: [] for k in KEYS})
        self.assertEqual(self.opening(s, at(d2, 9), evening), (at(d2, 16), evening))   # 依頼者の時間帯が1日も無ければ歯止め（7〜22時）の中
        self.assertEqual(self.opening(s, at(d2, 9), late), (at(d2, 22), late))
        self.assertEqual(self.opening(s, at(d2, 9), whole_day), (at(d2, 9), whole_day))   # 日だけの幅：歯止めの中
        self.put_client(s, "calling.hours", {k: [["00:00", "24:00"]] for k in KEYS})
        self.put_client(s, "calling.ng_hours", {k: [["16:00", "17:00"]] for k in KEYS})
        self.assertEqual(self.opening(s, at(d2, 9), evening)[0], at(d2, 16))   # 言葉の表の幅（「夕方」）もお客様の指定：NG時間でも
        said = replace(evening, said=(True, False))                             # 「4時以降」：NG時間でもかける
        self.assertEqual(self.opening(s, at(d2, 9), said)[0], at(d2, 16))
        self.assertEqual(self.opening(s, at(d2, 16), whole_day)[0], at(d2, 17))   # 日だけの幅は、NG時間を守る（時間を言っていない）

    def test_the_business_limit_and_slot_reach_the_judgement_through_settle(self):
        """締めは、業務ごとの「お客様指定の歯止め時間」と1回分の長さ（callback.exact_slot）を判定に渡す（読むのは1か所）。歯止め時間の
        外の口にした時刻は、確かめていなければ判定できない・確かめて「はい」なら口にした端から1回分（2026-10-23 ユーザー：「案B＆案A
        (確かめたうえで案A)」。⛔ 見直し17b：試験の土台が歯止め時間を1日じゅうにしていて、締めが渡し忘れても気づかなかった）。"""
        s = self.ready()
        self.tx(s, lambda tx: tx.execute(
            "INSERT INTO salescall.setting_values (key, scope, tenant_id, client_id, business_id, value, status, rationale,"
            " review_when, set_by) VALUES ('callee.designated_limit', 'business', %s, %s, %s, '[\"08:00\", \"21:00\"]', 'decided',"
            " '試験', '試験', '試験')", (s["tenant"], s["client"], s["business"])), OPERATOR)
        day = self.tx(s, lambda tx: tx.one("SELECT (salescall.jst(now()) + interval '2 days')::date AS d")["d"])
        said = f"{day.month}月{day.day}日の21時半以降"                        # 業務の歯止め（〜21時）の外。初めの値（〜22時）なら内
        got = self.settle(s, self.call(s, report=rep(callback=callback("later", time=said))))
        r = self.tx(s, lambda tx: results.latest(tx, s["task"]))
        self.assertEqual((got["state"], r["detail"]["unknown_cause"]), ("unknown", "callback_time_unconfirmed"))
        t = self.ready()
        self.put_client(t, "callback.exact_slot", 1800)                     # 1回分の長さは依頼者ごと（初めの値は1時間）
        self.put_client(t, "callee.designated_limit", ["07:00", "22:00"])
        agreed = replace(callback("later", time=f"{day.month}月{day.day}日の23時以降"), beyond_limit_ok=True)
        got = self.settle(t, self.call(t, report=rep(callback=agreed)))
        nxt = self.tx(t, lambda tx: next_action.current(tx, t["task"]))
        self.assertEqual((got["state"], nxt["window_daily"]), ("callback_scheduled", [23 * 3600, 23 * 3600 + 1800]))   # 1回分＝30分

    def test_changing_the_callee_settings_rejudges(self):
        """お客様の時間の設定（歯止め時間・1回分・営業時間）は判定の入力なので、判定の記録に残し、直したら判定し直す（⛔ 見直し17b：
        前は記録にも比べにも入っておらず、設定を直しても判定できないのまま残った）。"""
        s = self.ready()
        self.put_client(s, "callee.designated_limit", ["08:00", "21:00"])
        day = self.tx(s, lambda tx: tx.one("SELECT (salescall.jst(now()) + interval '2 days')::date AS d")["d"])
        aid = self.call(s, report=rep(callback=callback("later", time=f"{day.month}月{day.day}日の21時半以降")))
        self.assertEqual(self.settle(s, aid)["state"], "unknown")
        self.assertIsNone(self.tx(s, lambda tx: settle.rejudge(tx, aid)))      # 何も直していない：判定し直さない
        self.put_client(s, "callee.designated_limit", ["07:00", "22:00"])
        got = self.tx(s, lambda tx: settle.rejudge(tx, aid))
        self.assertEqual(got["state"], "callback_scheduled")

    def test_the_client_period_widths_reach_the_judgement_through_settle(self):
        """言葉ごとの幅（「夕方」など。依頼者ごとの設定 callback.period_*＝0088）は、お客様の時間の設定の1か所（gate.callee_settings の
        CalleeSettings.periods）で読み、締めが判定に渡す。依頼者が幅を変えたら、その依頼者の判定はその幅で読み（ほかの依頼者は初めの
        幅のまま）、判定の記録（お客様の時間の設定）にも、読んだ幅が残る（⛔ 見直し23 の (2)：前は時刻の読みと判定の入力に初めの値の写しが
        あり、渡し忘れても黙って初めの値になった）。"""
        s, t = self.ready(), self.ready()
        widths = lambda x: self.tx(x, lambda tx: gate.callee_settings(   # noqa: E731
            tx, tenant_id=x["tenant"], client_id=x["client"], business_id=x["business"]).period_table())
        self.assertEqual((widths(s)["夕方"], widths(s)["午後一"]), ((16 * 3600, 18 * 3600), (13 * 3600, 14 * 3600)))   # 初めの値
        self.put_client(s, "callback.period_evening", ["17:00", "19:00"])                # この依頼者だけ、夕方を17〜19時に
        self.put_client(s, "callback.period_afternoon", ["14:00", "00:00"])              # 午後を14時から（午後一は14〜15時）
        self.assertEqual((widths(s)["夕方"], widths(s)["午後一"], widths(t)["夕方"]),
                         ((17 * 3600, 19 * 3600), (14 * 3600, 15 * 3600), (16 * 3600, 18 * 3600)))
        day = self.tx(s, lambda tx: tx.one("SELECT (salescall.jst(now()) + interval '2 days')::date AS d")["d"])
        said = callback("later", time=f"{day.month}月{day.day}日の夕方")
        daily = lambda x: self.tx(x, lambda tx: next_action.current(tx, x["task"]))["window_daily"]   # noqa: E731
        for x, want in ((s, [17 * 3600, 19 * 3600]), (t, [16 * 3600, 18 * 3600])):     # ほかの依頼者は初めの幅
            with self.subTest(client=x["client"]):
                self.assertEqual(self.settle(x, self.call(x, report=rep(callback=said)))["state"], "callback_scheduled")
                self.assertEqual(daily(x), want)
        kept = self.tx(s, lambda tx: tx.one("SELECT snapshot -> 'callee' -> 'periods' -> '夕方' AS p FROM salescall.call_judgements"
                                            " WHERE attempt_id IN (SELECT attempt_id FROM salescall.attempts WHERE task_id = %s)",
                                            (s["task"],)))["p"]
        self.assertEqual(kept, [17 * 3600, 19 * 3600])                                   # 判定の記録に、読んだ幅

    def test_until_is_decided_by_the_business_setting_of_each_reason(self):
        """「〇時まで」のかけてよい時間は、電話を続けられなかった理由ごとの業務の設定（0127）で決まり、設定を読むのはお客様の時間の
        設定（gate.callee_settings）の1か所。選べる3つと理由の並びは、作りの表（calling_hours）とデータベースで食い違わない。
        業務ごとに変えられる（2026-10-23 ユーザー：理由と決め方の表に「OK」。契約者が設定で変えられる）。"""
        s = self.ready()
        cs = self.tx(s, lambda tx: gate.callee_settings(tx, tenant_id=s["tenant"], client_id=s["client"], business_id=s["business"]))
        self.assertEqual(cs.until_rules, (("other_party", "now"), ("unavailable", "after"), ("preparing", "slot_after")))
        rows = self.tx(s, lambda tx: tx.all("SELECT key, choices FROM salescall.setting_definitions WHERE key LIKE 'callback.until\\_%%'"))
        self.assertEqual({r["key"]: tuple(r["choices"]) for r in rows},
                         {key: calling_hours.UNTIL_RULES for key in calling_hours.UNTIL_SETTINGS.values()})
        with self.assertRaises(Rejected):
            self.put_client(s, "callback.until_unavailable", "soon")   # 選べるのは3つだけ
        day = self.tx(s, lambda tx: tx.one("SELECT (salescall.jst(now()) + interval '2 days')::date AS d")["d"])
        back = callback("absent", time=f"{day.month}月{day.day}日の15時まで")   # 「15時までには戻ります」
        t = self.ready()
        self.tx(t, lambda tx: tx.execute(   # この業務だけ「今から15時まで」に変える
            "INSERT INTO salescall.setting_values (key, scope, tenant_id, client_id, business_id, value, status, rationale,"
            " review_when, set_by) VALUES ('callback.until_unavailable', 'business', %s, %s, %s, '\"now\"', 'decided',"
            " '試験', '試験', '試験')", (t["tenant"], t["client"], t["business"])), OPERATOR)
        for x, want in ((s, 15 * 3600), (t, None)):
            self.assertEqual(self.settle(x, self.call(x, report=rep(callback=back)))["state"], "callback_scheduled")
            daily = self.tx(x, lambda tx: next_action.current(tx, x["task"]))["window_daily"]
            if want:
                self.assertEqual(daily[0], want)                             # 既定：15時から（出られないのは15時まで）
            else:
                self.assertEqual(daily[1], 15 * 3600)                        # この業務：15時まで

    def test_the_designated_limit_is_one_day_and_per_business(self):
        """お客様指定の歯止め時間（callee.designated_limit）は、業務ごとに選べる1日の中の [始まり, 終わり)（日をまたがない。
        2026-10-23 ユーザー：「『営業時間』と『お客様指定の歯止め時間』も業務ごとに選択できるようにした方がいいね」）。"""
        s = self.ready()
        with self.assertRaises(Rejected) as e:
            self.put_client(s, "callee.designated_limit", ["22:00", "07:00"])   # 日をまたぐ（設定の型では、日をまたいでよい）
        self.assertEqual(e.exception.constraint, "setting_values_designated_limit_order", e.exception)
        got = self.tx(s, lambda tx: tx.one("SELECT value FROM salescall.setting_values WHERE key = 'callee.designated_limit'"
                                           " AND scope = 'system' AND set_by = 'ユーザー' ORDER BY value_id DESC LIMIT 1"))["value"]
        self.assertEqual(got, ["07:00", "22:00"])   # 初めの値（2026-10-23 ユーザー OK。試験の土台は1日じゅうに替えている）
        # 終わりだけ 24:00（1日の終わり）を書ける（0121）。始まりの 24:00 は書けない
        self.put_client(s, "callee.designated_limit", ["00:00", "24:00"])
        self.assertEqual(self.tx(s, lambda tx: settings.resolve(tx, "callee.designated_limit", tenant_id=s["tenant"],
                                                                client_id=s["client"], business_id=s["business"])), ["00:00", "24:00"])
        with self.assertRaises(Rejected):
            self.put_client(s, "callee.designated_limit", ["24:00", "24:00"])

    def test_the_next_day_round_of_a_window_without_a_day_skips_the_clients_closed_days(self):
        """日を言っていない幅（「夕方なら」）・お客様が言った日の次の日の回は、依頼者のかける日だけ（休みの日の回は、かけられる時刻の
        無い回として数えずに飛ばす＝Window.later。⛔ 見直し15：前は依頼者の休みの土日にもかけた）。曜日の理由の回（「金曜なら」）は、
        お客様が選んだ日のまま。"""
        s = self.ready()
        self.put_client(s, "calling.hours", {k: ([["09:00", "18:00"]] if k in ("mon", "tue", "wed", "thu", "fri") else [])
                                             for k in KEYS})
        local = self.local_now(s).date()
        friday = local + timedelta(days=(4 - local.weekday()) % 7 + 7)
        at = lambda d, h: self.from_jst(s, datetime.combine(d, datetime.min.time()) + timedelta(hours=h))   # noqa: E731
        evening = next_action.Window(at(friday, 24), (16 * 3600, 18 * 3600), None, at(friday, 0), "day", day_said=True)
        got = self.opening(s, at(friday, 19), evening)
        from salescall.core.calendar import holiday_checker
        is_holiday = self.tx(s, holiday_checker)
        monday = friday + timedelta(days=3)
        while is_holiday(monday):                       # 月曜が祝日なら、その次の平日
            monday += timedelta(days=1)
        self.assertEqual(got, (at(monday, 16), replace(evening, until=at(monday, 24), since=at(monday, 0), round=1,
                                                         day_said=False)))

    def test_today_is_the_day_the_customer_said_even_without_saying_the_day(self):
        """日を言わない「すぐ」「夕方なら」も、いま話している今日の回は、お客様が言った日（依頼者の休みの日に約束の電話で話して
        いても、お客様は今日のつもり。spoken_time の day_said。見直し16：前は依頼者の休みの日なら、次のかける日まで待った）。今日に
        かけられなければ、次の日からは依頼者のかける日だけ（Window.later）。"""
        s = self.ready()
        self.put_client(s, "calling.hours", {k: ([["09:00", "18:00"]] if k in ("mon", "tue", "wed", "thu", "fri") else [])
                                             for k in KEYS})
        local = self.local_now(s).date()
        saturday = local + timedelta(days=(5 - local.weekday()) % 7 + 7)
        at = lambda d, h: self.from_jst(s, datetime.combine(d, datetime.min.time()) + timedelta(hours=h))   # noqa: E731
        from salescall.core import spoken_time
        for text in ("すぐ", "夕方なら"):
            with self.subTest(text=text):
                self.assertTrue(spoken_time.read(text, at(saturday, 10), business_hours=support.CALLEE.business_hours,
                                                 limit=support.CALLEE.designated_limit, slot=support.CALLEE.exact_slot,
                                                 until=None, periods=support.CALLEE.period_table()).day_said)
        asap = next_action.Window(None, None, None, at(saturday, 10), "day", day_said=True)
        self.assertEqual(self.opening(s, at(saturday, 10), asap, now=at(saturday, 10)),
                         (at(saturday, 10), replace(asap, until=at(saturday, 24), since=at(saturday, 0))))   # 休みの土曜でも今日
        from salescall.core.calendar import holiday_checker
        is_holiday = self.tx(s, holiday_checker)
        monday = saturday + timedelta(days=2)
        while is_holiday(monday):
            monday += timedelta(days=1)
        self.assertEqual(self.opening(s, at(saturday, 19), asap, now=at(saturday, 19)),
                         (at(monday, 9), replace(asap, until=at(monday, 24), since=at(monday, 0), day_said=False)))   # 日曜は飛ばす

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
        """日だけの約束（「（d4）なら」）で、その日にかけられる時刻が無い（その曜日は1日じゅうNG時間。依頼者の休みの曜日なら、
        ほかの日の時間を当ててかける＝別の試験）：その日が過ぎてから、時刻の
        決まっていない約束の方針の間隔で。お客様と約束したかけ直しのまま。⚠️ 前は、いまから間隔を数え、お客様が言った日より前にかけた（2026-10-19 の見直し）。
        門で幅が過ぎたと分かったとき（_window_over）も同じ。"""
        from salescall.core.calendar import holiday_checker
        s = self.ready()
        d4 = self.local_now(s).date() + timedelta(days=4)
        key = self.tx(s, lambda tx: calling_hours.day_key(d4, holiday_checker(tx)))
        self.put_client(s, "calling.hours", {k: [["00:00", "24:00"]] for k in KEYS})
        self.put_client(s, "calling.ng_hours", {k: ([["00:00", "24:00"]] if k == key else []) for k in KEYS})
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
                         ("call", True, 1, midnight(d + timedelta(days=7)), "missed_promise"))   # 次の週の同じ曜日・その日にかけていない
        self.assertEqual(moved.at, midnight(d + timedelta(days=7)))
        late = self.tx(s, lambda tx: next_action._keep_promise(tx, s["task"], first.until, first, target,
                                                               now=first.until + timedelta(days=14)))
        self.assertEqual((late.window.round, late.window.missed, late.window.since, late.apology),
                         (3, 3, midnight(d + timedelta(days=21)), "missed_promise"))   # 過ぎた回は回に数えるが、かけなかった回は打ち止めに数えない
        uncalled = self.tx(s, lambda tx: next_action._keep_promise(tx, s["task"], first.until, replace(first, round=3), target,
                                                                   now=now))
        self.assertEqual((uncalled.kind, uncalled.window.round, uncalled.window.missed), ("call", 4, 1))   # 一度もかけていない回で打ち切らない
        called = next_action.Window(now + timedelta(hours=1), None, None, now - timedelta(hours=1), "week", round=3)   # かけた回
        over = self.tx(s, lambda tx: next_action._keep_promise(tx, s["task"], called.until, called, target, now=now))
        self.assertEqual((over.kind, over.end_reason), ("end", "callback_unspecified_time"))   # かけた回が、最初の回＋方針の回数（3）を超えたら終える
        self.put_client(s, "calling.hours", {k: [] for k in KEYS})
        guard = self.tx(s, lambda tx: next_action._keep_promise(tx, s["task"], first.until, first, target, now=now))
        self.assertEqual((guard.kind, guard.window.round, guard.window.since), ("call", 1, midnight(d + timedelta(days=7))))   # 依頼者の
        # 時間帯が無い：深夜早朝の歯止め（calling.hours_limit）の中で、次の回へ
        self.put_client(s, "calling.ng_hours", {k: [["00:00", "24:00"]] for k in KEYS})
        kept = self.tx(s, lambda tx: next_action._keep_promise(tx, s["task"], first.until, first, target, now=now))
        self.assertEqual((kept.kind, kept.promised, kept.window), ("call", True, first))   # 時間帯が無い：約束の幅のまま確かめ直す
        self.assertEqual(kept.at, now + timedelta(seconds=self.tx(s, lambda tx: next_action.no_opening_recheck(tx, s["task"]))))
        self.assertEqual(self.tx(s, lambda tx: next_action.no_opening_recheck(tx, s["task"])), 86400)   # 仮の設定の初めの値＝1日

    def test_an_exact_time_promise_is_called_a_few_times_in_its_slot_then_on_the_next_days(self):
        """時刻ちょうどの約束（「明日の15時」）に出なければ、その時刻からの1回分（callback.exact_slot）の中で、前の電話から5分後・
        さらに10分後にかけ、最後の1本は1回分の終わりにかけ、それでも出なければ次の日の同じ時刻の回へ。約束の日を含めて3日で打ち止め
        （2026-10-19 ユーザー：「「明日の15時」ちょうどに出られなければ、15時から16時に数回かける。出なければ、翌日の15時、翌々日の15時で
        打ち止め」「5分後→さらに10分後→さらに20分後くらいがいいんじゃない？」⇒ 2026-10-23 ユーザー：「時刻ちょうどの約束も含めて、
        30分」・案A「4本目を30分の終わりに合わせる」＝15:00・15:05・15:15・15:30）。"""
        s = self.ready()
        self.put_client(s, "calling.ng_hours", {k: [] for k in KEYS})
        clock = self.tx(s, lambda tx: tx.one(
            "SELECT now() AS now, greatest(date_trunc('minute', now()) - interval '1 minute', salescall.from_jst(date_trunc('day', salescall.jst(now())))) AS t,"
            " extract(epoch FROM date_trunc('day', salescall.jst(now())) + interval '1 day' - salescall.jst(now())) AS left"))
        now, t = clock["now"], clock["t"]
        slot = min(3600, int(clock["left"]) - 120)            # 試験の回が日をまたがない長さ（夜中に流しても同じ確かめ）
        if slot < 1800:
            self.skipTest("日本時間の0時の直前：回の時間が短すぎる")
        self.put_client(s, "callback.exact_slot", slot)
        target = next_action.Target(s["contact"])
        first = self.tx(s, lambda tx: next_action._keep_promise(
            tx, s["task"], t, next_action._exact_window(tx, s["task"], t, day_said=True), target, now=now, exact=True))
        self.assertEqual((first.kind, first.promised, first.callee_at, first.window.round), ("call", True, t, 0))
        self.tx(s, lambda tx: next_action._write(tx, s["task"], first))
        due_now = lambda: self.tx(s, lambda tx: support.schedule_call(   # noqa: E731
            tx, s["task"], now, contact_id=s["contact"], cause="試験：次の1回をいまかける", promised=True, callee_at=t,
            window=next_action.window_of(next_action.current(tx, s["task"]))))
        claimed = lambda aid: self.tx(s, lambda tx: tx.one(   # noqa: E731
            "SELECT claimed_at FROM salescall.attempts WHERE attempt_id = %s", (aid,))["claimed_at"])
        for planned in (300, 900, slot):                       # 約束の時刻から5分後・15分後・1回分の終わり（前の電話から5分はあける）
            aid = self.call(s, ["dialing", "no_answer"])
            self.settle(s, aid)
            cur = self.tx(s, lambda tx: next_action.current(tx, s["task"]))
            want = max(t + timedelta(seconds=planned), claimed(aid) + timedelta(seconds=300))
            self.assertLess(abs((cur["call_at"] - want).total_seconds()), 2)
            self.assertEqual((cur["promised"], cur["callee_at"], cur["window_round"]), (True, t, 0))
            due_now()
        self.settle(s, self.call(s, ["dialing", "no_answer"]))   # 4本目にも出ない
        cur = self.tx(s, lambda tx: next_action.current(tx, s["task"]))
        self.assertEqual((cur["call_at"], cur["callee_at"], cur["window_round"], cur["apology_owed"]),
                         (t + timedelta(days=1), t + timedelta(days=1), 1, False))   # 4本で次の日の同じ時刻（かけたのでお詫びは無い）
        denied = self.tx(s, lambda tx: next_action.after_denial(   # 門で止められた（時間帯の外など）：時刻ちょうどの約束のまま
            tx, s["task"], gate.denied("outside_hours"),
            tenant_id=s["tenant"], client_id=s["client"], business_id=s["business"]))
        self.assertEqual((denied.callee_at, denied.window.round), (t + timedelta(days=1), 1))   # 約束の時刻を捨てない（見直し7）
        second = next_action.window_of(cur)
        third = self.tx(s, lambda tx: next_action._keep_promise(tx, s["task"], second.until, second, target, now=now, exact=True))
        self.assertEqual((third.kind, third.at, third.callee_at, third.window.round, third.apology),
                         ("call", t + timedelta(days=2), t + timedelta(days=2), 2, "missed_promise"))   # 2日目に話せなければ3日目（その日にかけていない）
        skipped = self.tx(s, lambda tx: next_action._keep_promise(   # 見守り役が止まって2日目を飛ばした：その前の回にはかけていても、
            tx, s["task"], first.window.until, first.window, target, now=t + timedelta(days=2, minutes=1), exact=True))
        self.assertEqual((skipped.window.round, skipped.window.missed, skipped.apology), (2, 1, "missed_promise"))   # 飛んだ回があればお詫びから
        uncalled = self.tx(s, lambda tx: next_action._keep_promise(tx, s["task"], second.until, replace(second, round=2), target,
                                                                   now=now, exact=True))
        self.assertEqual((uncalled.kind, uncalled.window.round), ("call", 3))   # かけられなかった日は数えない（打ち切らない）
        last = replace(first.window, round=2)                                    # 3日目もかけて話せなかった（その日にかけた）
        over = self.tx(s, lambda tx: next_action._keep_promise(tx, s["task"], last.until, last, target, now=now, exact=True))
        self.assertEqual((over.kind, over.end_reason), ("end", "callback_exact_unreached"))   # 3日かけて話せなければ打ち止め

    def exact_ready(self, s):
        """時刻ちょうどの約束の試験の、いまの時刻（約束の時刻＝1分前）と回（その日のうちに収まる長さ）。"""
        self.put_client(s, "calling.ng_hours", {k: [] for k in KEYS})
        clock = self.tx(s, lambda tx: tx.one(
            "SELECT now() AS now, greatest(date_trunc('minute', now()) - interval '1 minute', salescall.from_jst(date_trunc('day', salescall.jst(now())))) AS t,"
            " extract(epoch FROM date_trunc('day', salescall.jst(now())) + interval '1 day' - salescall.jst(now())) AS left"))
        slot = min(3600, int(clock["left"]) - 120)
        if slot < 1800:
            self.skipTest("日本時間の0時の直前：回の時間が短すぎる")
        self.put_client(s, "callback.exact_slot", slot)
        return clock["now"], clock["t"], self.tx(s, lambda tx: next_action._exact_window(tx, s["task"], clock["t"], day_said=True))

    def test_failed_dials_on_an_exact_promise_follow_the_order_of_failed_numbers(self):
        """時刻ちょうどの約束の電話で、お客様のところで鳴らなかった（回線の部品が番号を受け付けない＝発信に失敗・お客様が出る前に
        回線の側で終わった＝結果不明。2026-10-23 ユーザー：お客様が出る前に終わる番号も「番号を受け付けないときと同じ」）：その回の本数には
        数えず、決まった順（dial.failed_retry_days＝当日にもう1回・次の日・そのさらに2日後）より前にはかけない。1回目の失敗のあとは
        同じ回の中で5分あけ、2回目のあとは次の日の同じ時刻の回へ（その回にお客様の電話は一度も鳴っていない＝お詫びから）。
        ⛔ 見直し17a：前は約束を守る次の動きを先に返し、15:00・15:05・15:10・15:15 と4回失敗して、その日のうちに番号も行も終えた
        （2026-10-22 深夜 ユーザー：「同じ番号で続けて当日2回＆翌日1回、さらに2日後1回の計4回」）。"""
        for path in (["dialing", "call_failed"], ["dialing", "ringing", "technical_error"]):
            with self.subTest(path=path[-1]):
                s = self.ready()
                now, t, window = self.exact_ready(s)
                claimed = lambda aid: self.tx(s, lambda tx: tx.one(   # noqa: E731
                    "SELECT claimed_at FROM salescall.attempts WHERE attempt_id = %s", (aid,))["claimed_at"])
                self.tx(s, lambda tx: support.schedule_call(tx, s["task"], now, contact_id=s["contact"], cause="試験", promised=True,
                                                               callee_at=t, window=window))
                aid = self.call(s, path)
                self.settle(s, aid)
                cur = self.tx(s, lambda tx: next_action.current(tx, s["task"]))
                self.assertEqual((cur["kind"], cur["promised"], cur["window_round"]), ("call", True, 0))   # 回の本数に数えない（同じ回のまま）
                self.assertLess(abs((cur["call_at"] - (claimed(aid) + timedelta(seconds=300))).total_seconds()), 2)
                self.tx(s, lambda tx: support.schedule_call(tx, s["task"], now, contact_id=s["contact"], cause="試験", promised=True,
                                                               callee_at=t, window=window))
                self.settle(s, self.call(s, path))   # 2回目の失敗：当日の2回を使った
                cur = self.tx(s, lambda tx: next_action.current(tx, s["task"]))
                self.assertEqual((cur["kind"], cur["promised"], cur["call_at"], cur["callee_at"], cur["window_round"], cur["apology_owed"]),
                                 ("call", True, t + timedelta(days=1), t + timedelta(days=1), 1, True))   # 次の日の同じ時刻・お詫びから

    def test_a_round_with_no_call_that_rang_is_apologised_for(self):
        """時刻ちょうどの約束の回の時間が過ぎて次の日の回へ移るとき、その回にお客様の電話が一度も鳴っていなければ（鳴らなかった電話は
        回の本数に数えない＝salescall.dial_kind・rang の1か所）、お詫びから始める。⛔ 見直し17c：前は「鳴りえた電話」で数え、回線の
        不具合が続くだけで約束を打ち切りえた。"""
        s = self.ready()
        now, t, window = self.exact_ready(s)
        self.tx(s, lambda tx: support.schedule_call(tx, s["task"], now, contact_id=s["contact"], cause="試験", promised=True,
                                                       callee_at=t, window=window))
        self.settle(s, self.call(s, ["dialing", "ringing", "technical_error"]))
        target = next_action.Target(s["contact"])
        moved = self.tx(s, lambda tx: next_action._keep_promise(tx, s["task"], window.until, window, target, now=now, exact=True))
        self.assertEqual((moved.kind, moved.window.round, moved.window.missed, moved.apology), ("call", 1, 1, "missed_promise"))

    def test_an_exact_promise_is_called_once_on_the_second_and_third_days(self):
        """時刻ちょうどの約束の2日目・3日目は、その時刻に1本だけ（2026-10-22 ユーザー：「2日目・3日目は、その時刻に1本ずつ」）。
        出なければ、その日の中でかけ直さず、次の日の同じ時刻の回へ。"""
        s = self.ready()
        self.put_client(s, "calling.ng_hours", {k: [] for k in KEYS})
        clock = self.tx(s, lambda tx: tx.one(
            "SELECT now() AS now, greatest(date_trunc('minute', now()) - interval '1 minute', salescall.from_jst(date_trunc('day', salescall.jst(now())))) AS t,"
            " extract(epoch FROM date_trunc('day', salescall.jst(now())) + interval '1 day' - salescall.jst(now())) AS left"))
        now, t = clock["now"], clock["t"]
        slot = min(3600, int(clock["left"]) - 120)
        if slot < 1800:
            self.skipTest("日本時間の0時の直前：回の時間が短すぎる")
        self.put_client(s, "callback.exact_slot", slot)
        window = replace(self.tx(s, lambda tx: next_action._exact_window(tx, s["task"], t, day_said=True)), round=1)   # 2日目の回
        self.tx(s, lambda tx: support.schedule_call(tx, s["task"], now, contact_id=s["contact"], cause="試験", promised=True,
                                                       callee_at=t, window=window))
        self.settle(s, self.call(s, ["dialing", "no_answer"]))
        cur = self.tx(s, lambda tx: next_action.current(tx, s["task"]))
        self.assertEqual((cur["call_at"], cur["callee_at"], cur["window_round"]),
                         (t + timedelta(days=1), t + timedelta(days=1), 2))   # 1本で次の日の同じ時刻

    def test_an_exact_time_promise_in_the_ng_hours_keeps_its_time(self):
        """お客様がNG時間の中の時刻（「12時に」）を指定した約束：回の中のかけられる時刻を、NG時間を引かずに探す（お客様が指定した
        時刻はNG時間でもかけてよい＝2026-10-15 ユーザー。⚠️ 引いて探すと「かけられる時刻が無い」と読んだ＝2026-10-19 の見直し）。"""
        s = self.ready()
        self.put_client(s, "calling.ng_hours", {k: [["00:00", "24:00"]] for k in KEYS})   # どの時刻もNG時間
        t = self.tx(s, lambda tx: tx.one("SELECT date_trunc('minute', now()) + interval '2 hours' AS t")["t"])
        now = self.tx(s, lambda tx: tx.one("SELECT now() AS t")["t"])
        d = self.tx(s, lambda tx: next_action._keep_promise(
            tx, s["task"], t, next_action._exact_window(tx, s["task"], t, day_said=True), next_action.Target(s["contact"]), now=now, exact=True))
        self.assertEqual((d.kind, d.at, d.callee_at), ("call", t, t))

    def test_an_exact_promise_retry_calls_within_its_slot_even_in_another_ng_frame(self):
        """時刻ちょうどの約束の回のかけ直しは、回の時間（お客様が言った時刻からの時間）の中なら、どのNG時間の枠でもかける（2026-10-22
        ユーザー：「お客様が指定した時刻ならNG時間でもかけてよい（歯止めなし…）」。次の動きと門は同じ決まり＝`calling_hours.callee_hours`
        の1か所。⛔ 前は、約束の時刻を含む枠だけを外し、ほかの枠の終わりまで待った）。幅を持たない時刻の指定は、今まで通り
        約束の時刻を含む枠だけ外す（`excuse_ng`）。"""
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
        w = self.tx(s, lambda tx: next_action._exact_window(tx, s["task"], t, day_said=True))
        d = self.tx(s, lambda tx: next_action._keep_promise(tx, s["task"], t + timedelta(minutes=5), w,
                                                            next_action.Target(s["contact"]), now=now, exact=True))
        self.assertEqual(d.at, t + timedelta(minutes=5))                      # 枠の終わりまで待たない
        allowed = lambda m, window: self.tx(s, lambda tx: calling_hours.allowed(   # noqa: E731
            {k: [["00:00", "24:00"]] for k in KEYS},
            {k: [[hhmm(timedelta(minutes=3)), hhmm(timedelta(minutes=50))]] for k in KEYS}, holiday_checker(tx),
            t_local + timedelta(minutes=m), t_local + timedelta(minutes=m, seconds=60), callee_at=t_local,
            window=window and gate.window_hours(tx, tenant_id=s["tenant"], client_id=s["client"], business_id=s["business"],
                                                window=window)))
        self.assertEqual((allowed(5, w.callee), allowed(30, w.callee)), (True, True))   # 門も同じ答え（gate.window_hours。30分＝最後の1本）
        self.assertFalse(allowed(50, w.callee))   # 1回分（初め30分）と遅れの許し（5分）の外
        self.assertEqual((allowed(5, None), allowed(50, None)), (False, True))   # 幅を持たない時刻の指定

    def test_a_window_retry_past_the_round_moves_to_the_next_round_without_an_apology(self):
        """幅の約束で出なかったとき、再発信の方針の間隔（「2日後」）が回の終わりを越えるなら、次の回へ（回を飛ばさない。その回には
        かけたので、お詫びの印は付けない。⚠️ 前は回を2つ飛ばし、お詫びの印も付けた＝2026-10-19 の見直し）。"""
        keys = ("open", "incomplete", "not_reached_absent", "callback_unspecified_time")
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
        keys = ("open", "incomplete", "not_reached_absent", "callback_unspecified_time")
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
        keys = ("open", "incomplete", "not_reached_absent", "callback_unspecified_time")
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

    def test_line_failures_do_not_use_up_an_untimed_promise(self):
        """時刻の決まっていない約束（「あとで」）の電話が、お客様が出る前に回線の側で終わった（結果不明＝数え方の line_failure）：
        方針の回数に数えない（ふつうの電話と同じ数え方＝salescall.dial_kind の1か所。2026-10-22 深夜 ユーザー）。⛔ 見直し17c：前は
        「鳴りえた電話」で数え、回線の不具合が続くだけで約束を打ち切った。続けば、番号を受け付けないときと同じ順で、計4回で
        その番号へかけない（2026-10-23 ユーザー）＝方針の回数（3）では終わらず、番号の続きで終わる。"""
        s = self.ready()
        aid = self.call(s, report=rep(callback=callback("later", time="あとで")))
        self.assertEqual(self.settle(s, aid)["state"], "callback_scheduled")
        now = self.tx(s, lambda tx: tx.one("SELECT now() AS t")["t"])
        got = []
        for _ in range(4):                                                   # 試験の方針の回数（3）を超えても
            self.tx(s, lambda tx: support.schedule_call(tx, s["task"], now, contact_id=s["contact"],
                                                           cause="試験：いまかける", promised=True))
            d = self.settle(s, self.call(s, ["dialing", "ringing", "technical_error"]))["decision"]
            got.append((d.kind, d.promised, d.end_reason))
        # 約束のまま（方針の回数に数えない）。番号のせいと分かった4回目で、番号の続きで終わる（0129・0143）
        self.assertEqual(got, [("call", True, None)] * 3 + [("end", None, "number_unusable")])

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

    def test_the_plan_of_an_exact_promise_ends_at_the_end_of_its_slot(self):
        """時刻ちょうどの約束の1日目にかける時刻の並び（`_exact_plan` の1か所）：約束の時刻・5分後・15分後・そして1回分の終わり
        （2026-10-23 ユーザー：「時刻ちょうどの約束も含めて、30分」・案A＝15:00・15:05・15:15・15:30）。1回分を短くしても、最後の
        1本は必ず1回分の終わりに来る（間の並びのうち越えるものは使わない）。2日目・3日目は約束の時刻の1本だけ。"""
        at = datetime(2030, 1, 7, 15, 0, tzinfo=timezone.utc)
        plan = lambda minutes, round_=0: [   # noqa: E731
            int((x - at).total_seconds() // 60)
            for x in next_action._exact_plan(at, at + timedelta(minutes=minutes), [300, 600], round_)]
        self.assertEqual(plan(30), [0, 5, 15, 30])
        self.assertEqual(plan(20), [0, 5, 15, 20])
        self.assertEqual(plan(15), [0, 5, 15])          # 15分ちょうどの並びは使わず、1回分の終わりの1本にする
        self.assertEqual(plan(10), [0, 5, 10])
        self.assertEqual(plan(60), [0, 5, 15, 60])
        self.assertEqual(plan(30, 1), [0])

    def test_the_slot_of_an_exact_promise_is_thirty_minutes_and_its_last_call_passes_the_gate(self):
        """1回分（callback.exact_slot）の初めの値は30分、間の並びの初めは 5分・10分（2026-10-23 ユーザー。0125）。最後の1本（30分の
        終わり）は、門の幅の確かめ（calling_hours.in_window）と時間帯の確かめ（allowed）を通る：門が見る回の幅は、1回分の終わりから
        最初の間（5分）だけ長い（`_exact_window`）。"""
        from salescall.core.calendar import holiday_checker
        s = self.ready()
        self.put_client(s, "calling.ng_hours", {k: [] for k in KEYS})
        clock = self.tx(s, lambda tx: tx.one(
            "SELECT now() AS now, date_trunc('minute', now()) AS t, salescall.jst(date_trunc('minute', now())) AS t_local"))
        now, t, t_local = clock["now"], clock["t"], clock["t_local"]
        if t_local.hour == 23 and t_local.minute >= 20:
            self.skipTest("日本時間の0時の直前：試験の回が日をまたぐ")
        cs = self.tx(s, lambda tx: gate.callee_settings(tx, tenant_id=s["tenant"], client_id=s["client"], business_id=s["business"]))
        gaps = self.tx(s, lambda tx: next_action._exact_gaps(tx, s["task"]))
        self.assertEqual((cs.exact_slot, gaps), (1800, [300, 600]))
        w = self.tx(s, lambda tx: next_action._exact_window(tx, s["task"], t, day_said=True))
        self.assertEqual(w.until, t + timedelta(minutes=35))
        final_local = t_local + timedelta(minutes=30)
        start_local, until_local = next_action._slot_of(w)
        self.assertTrue(calling_hours.in_window(final_local, until_local.astimezone(calling_hours.JST).replace(tzinfo=None),
                                                w.daily, w.days, None,
                                                since_local=start_local.astimezone(calling_hours.JST).replace(tzinfo=None)))
        hours = {k: [["00:00", "24:00"]] for k in KEYS}
        callee = self.tx(s, lambda tx: gate.window_hours(tx, tenant_id=s["tenant"], client_id=s["client"],
                                                         business_id=s["business"], window=w.callee))
        lifetime = timedelta(seconds=30)
        ok = lambda local: self.tx(s, lambda tx: calling_hours.allowed(   # noqa: E731
            hours, {k: [] for k in KEYS}, holiday_checker(tx), local, local + lifetime, callee_at=t_local, window=callee))
        self.assertEqual((ok(final_local), ok(final_local + timedelta(minutes=3)), ok(t_local + timedelta(minutes=36))),
                         (True, True, False))

    def test_the_end_of_an_exact_promise_slot_is_not_a_said_edge(self):
        """時刻ちょうどの約束の回の終わりは、こちらが作った端（口にしたのは約束の時刻だけ）：歯止め時間（7〜22時）の中の約束は、
        終わりを歯止め時間で切る＝「21時45分」の約束は 21:45・21:50 だけ（22:00・22:15 にはかけない）。歯止めの外の約束（23時）は、
        通話の中で確かめていなければ口にした時刻そのものの1本だけ、確かめて「はい」なら 23:00 から1回分（決めるのは
        calling_hours.callee_range の made の1か所。⛔ 見直し18a の (4)・18c の (2)：前は回の両端を口にした端にした）。"""
        s = self.ready()
        self.put_client(s, "callee.designated_limit", ["07:00", "22:00"])   # 初めの値（試験の土台は1日じゅうに替えている）
        day = self.tx(s, lambda tx: tx.one("SELECT salescall.from_jst(date_trunc('day', salescall.jst(now())) + interval '1 day') AS d")["d"])
        callee = lambda w: self.tx(s, lambda tx: gate.window_hours(   # noqa: E731
            tx, tenant_id=s["tenant"], client_id=s["client"], business_id=s["business"], window=w.callee))
        t = day + timedelta(hours=21, minutes=45)
        w = self.tx(s, lambda tx: next_action._exact_window(tx, s["task"], t, day_said=True))
        self.assertEqual((w.daily, w.said, w.until), ((21 * 3600 + 2700, 22 * 3600), (True, False), day + timedelta(hours=22)))
        self.assertEqual(callee(w)["mon"], [["21:45", "22:00"]])                         # 門も同じ幅
        start, end = next_action._slot_of(w)
        plan = next_action._exact_plan(start, start + timedelta(minutes=30), [300, 600], 0)
        self.assertEqual([x for x in plan if x < end], [t, t + timedelta(minutes=5)])   # 21:45・21:50 だけ
        late = day + timedelta(hours=23)
        w = self.tx(s, lambda tx: next_action._exact_window(tx, s["task"], late, day_said=True))
        self.assertEqual((w.daily, w.said), ((23 * 3600, 23 * 3600 + 300), (True, True)))   # 口にした時刻そのもの（5分の遅れの許し）
        start, end = next_action._slot_of(w)
        self.assertEqual([x for x in next_action._exact_plan(start, start + timedelta(minutes=30), [300, 600], 0) if x < end],
                         [late])
        w = self.tx(s, lambda tx: next_action._exact_window(tx, s["task"], late, day_said=True, beyond_ok=True))
        self.assertEqual((w.daily, w.said), ((23 * 3600, 23 * 3600 + 2100), (True, True)))   # 確かめ済み：23:00 から1回分（＋5分）

    def test_a_promise_on_a_later_day_of_the_failed_order_keeps_its_time(self):
        """鳴らない電話が続く番号の決まった順（dial.failed_retry_days）の次の日が先の日で、その日に約束（「明日の10時」）があるとき：
        その日の始まりから約束を守り直す＝約束の時刻 10:00 にかける（`_not_before_failed` の1か所。決まった順の時刻＝前と別の時間帯の
        14時から探すと、約束の回を過ぎて次の日へ回る。⛔ 見直し18a の (5)：壊しても試験が落ちなかった。見直し18a の MorningPromise の場面）。"""
        s = self.ready()
        self.put_client(s, "calling.ng_hours", {k: [] for k in KEYS})
        clock = self.tx(s, lambda tx: tx.one(
            "SELECT now() AS now, salescall.from_jst(date_trunc('day', salescall.jst(now())) + interval '1 day') AS day"))
        now, day = clock["now"], clock["day"]
        t = day + timedelta(hours=10)
        target = next_action.Target(s["contact"])
        d = self.tx(s, lambda tx: next_action._keep_promise(
            tx, s["task"], t, next_action._exact_window(tx, s["task"], t, day_said=True), target, now=now, exact=True))
        self.assertEqual((d.kind, d.at), ("call", t))
        got = self.tx(s, lambda tx: next_action._not_before_failed(tx, s["task"], d, day + timedelta(hours=14), now=now))
        self.assertEqual((got.kind, got.at, got.callee_at, got.window.round), ("call", t, t, 0))

    def test_an_unrung_dial_in_an_exact_round_is_not_one_of_its_calls(self):
        """時刻ちょうどの約束の回の本数は、お客様に届いた電話だけで数える（`_exact_next_try`。お客様が出る前に回線の側で終わった電話は
        数えない）：回線の失敗1本と、鳴って出なかった3本のあとでも、最後の1本（1回分の終わり）が残る（⛔ 見直し18a の (5)：数え方を
        「発信した電話すべて」に壊しても試験が落ちなかった。見直し18a の test_exact_line_failure_then_rang の場面）。"""
        s = self.ready()
        now, t, window = self.exact_ready(s)
        paths = [["dialing", "ringing", "technical_error"]] + [["dialing", "ringing", "no_answer"]] * 3
        for path in paths:
            aid = self.w.insert_claim(s)
            for st in path:
                self.w.move(s, aid, st)
        slot = self.tx(s, lambda tx: gate.callee_settings(tx, tenant_id=s["tenant"], client_id=s["client"],
                                                          business_id=s["business"]).exact_slot)
        got = self.tx(s, lambda tx: next_action._exact_next_try(tx, s["task"], window, now))
        self.assertEqual(got, t + timedelta(seconds=slot))                          # 最後の1本（1回分の終わり）

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
        self.assertEqual((d.kind, d.promised, d.window.round, d.apology), ("call", True, 1, "missed_promise"))
        owed = lambda: self.tx(s, lambda tx: next_action.current(tx, s["task"]))["apology_owed"]   # noqa: E731
        c = self.claim(s)
        self.assertEqual(c.apology, ("missed_promise",))
        self.tx(s, lambda tx: attempts.release(tx, [c.attempt_id]))
        self.settle(s, self.call(s, ["dialing", "no_answer"]))
        self.assertTrue(owed())                                               # 出なかった：お詫びはまだ
        # 印は次の回の予定に引き継ぐ（門は予定の印をそのまま予約証へ写す＝上で確かめた）。⛔ 見直し17d-2：前は次の回の時刻の
        # 2時間前へ予定を動かして予約し直したので、夜（次の回が次の日の幅になる）には幅の外で断られて落ちた
        cur = self.tx(s, lambda tx: next_action.current(tx, s["task"]))
        self.assertEqual(cur["apology_reasons"], ["missed_promise"])
        now = self.tx(s, lambda tx: tx.one("SELECT now() AS t")["t"])
        self.tx(s, lambda tx: support.schedule_call(   # 試験：次の回の幅を、いまから始まる形にしてかける（印は予定が引き継ぐ）
            tx, s["task"], now, contact_id=s["contact"], cause="試験：いまかける", promised=True,
            window=replace(next_action.window_of(cur), since=now - timedelta(minutes=1))))
        c = self.claim(s)
        self.assertEqual(c.apology, ("missed_promise",))
        self.tx(s, lambda tx: attempts.release(tx, [c.attempt_id]))
        self.settle(s, self.call(s))                                          # 目的のお客様と話せた
        self.assertFalse(owed())

    def weekend_words(self, s):
        """週末の言い方。日曜日の「週末」「土日」は、今日か来週かが決まらず確かめになる（spoken_time の決まり）ので、日曜日だけ
        「土曜なら」にする（⛔ 見直し17d-2：前は日曜日に流すと落ちた）。"""
        sunday = self.tx(s, lambda tx: tx.one("SELECT extract(isodow FROM salescall.jst(now())) = 7 AS x")["x"])
        return "土曜なら" if sunday else "週末なら"

    def test_a_weekend_promise_with_weekday_hours_is_called_at_the_clients_time_of_day(self):
        """お客様の幅が日・曜日だけ（「週末なら」）で、依頼者の時間帯が平日だけ：依頼者の1日の中の時間（9〜18時）を、お客様が言った
        土日に当ててかける（2026-10-22 ユーザー決定。依頼者の休みの曜日・祝日には関係なく、お客様が言った日にかける）。門と次の動きは
        同じ決まり（`calling_hours.callee_hours` の1か所）。⛔ 前は、重ならないとして時刻の決まっていない約束にし、お客様が言った日に
        かけなかった。"""
        from salescall.core.calendar import holiday_checker
        weekday = {k: ([["09:00", "18:00"]] if k in ("mon", "tue", "wed", "thu", "fri") else []) for k in KEYS}
        s = self.ready()
        aid = self.call(s, report=rep(callback=callback("later", time=self.weekend_words(s))))
        self.put_client(s, "calling.hours", weekday)                 # 発信のあとで、依頼者の時間帯を平日だけにする
        got = self.settle(s, aid)
        cur = self.tx(s, lambda tx: next_action.current(tx, s["task"]))
        at = self.tx(s, lambda tx: tx.one("SELECT salescall.jst(%s) AS t", (cur["call_at"],))["t"])
        is_holiday = self.tx(s, lambda tx: (lambda f: f(at.date()))(holiday_checker(tx)))
        self.assertEqual((got["state"], cur["kind"], cur["promised"], cur["window_until"] is not None),
                         ("callback_scheduled", "call", True, True))
        self.assertTrue(at.weekday() in (5, 6) or is_holiday)
        self.assertTrue(9 <= at.hour < 18, at)                         # 依頼者の1日の中の時間
        saturday = datetime.combine(at.date() + timedelta(days=(5 - at.weekday()) % 7), datetime.min.time()).replace(hour=10)
        allowed = lambda t, window: self.tx(s, lambda tx: calling_hours.allowed(   # noqa: E731
            weekday, {k: [] for k in KEYS}, lambda d: False, t, t + timedelta(minutes=1),
            window=window and gate.window_hours(tx, tenant_id=s["tenant"], client_id=s["client"], business_id=s["business"],
                                                window=window)))
        weekend = calling_hours.CalleeWindow(None, ("sat", "sun"), None, True)
        self.assertEqual((allowed(saturday, weekend), allowed(saturday.replace(hour=20), weekend),
                          allowed(saturday, None)), (True, False, False))   # 門も同じ答え（約束でない電話は休みの日にかけない）

    def test_a_day_only_promise_with_no_calling_time_at_all_does_not_loop(self):
        """お客様の幅が日・曜日だけ（「週末なら」）で、依頼者の時間帯が1日も無い：暦を先まで読んで回を探さず、時刻の決まっていない
        約束へ（門で止められたあとも同じ。⛔ 2026-10-22 の見直し14：前は回を400回先まで探して暦の外に当たり、同じ予定を5分ごとに
        書き続け、行が終わらなかった）。"""
        s = self.ready()
        aid = self.call(s, report=rep(callback=callback("later", time=self.weekend_words(s))))
        self.put_client(s, "calling.hours", {k: [] for k in KEYS})
        from salescall.core import calendar
        read = []
        orig = calendar.holiday_checker

        def counting(tx):
            f = orig(tx)
            return lambda d: (read.append(d), f(d))[1]
        from unittest import mock
        with mock.patch.object(next_action, "holiday_checker", counting):
            got = self.settle(s, aid)
            cur = self.tx(s, lambda tx: next_action.current(tx, s["task"]))
            self.assertEqual((got["state"], cur["kind"], cur["promised"]), ("callback_scheduled", "call", True))
            for _ in range(3):                                       # 門が時間帯の外で止めても、同じ予定を書き続けない
                self.tx(s, lambda tx: next_action.after_denial(
                    tx, s["task"], gate.denied("outside_hours"),
                    tenant_id=s["tenant"], client_id=s["client"], business_id=s["business"]))
        self.assertLess(max(read, default=None) - min(read), timedelta(days=40))   # 暦を何年も先まで読まない

    def test_the_gate_calls_a_promised_window_outside_the_clients_hours_and_ng_hours(self):
        """お客様が1日の中の時間を言った幅（「夜」「15時」など）の約束の電話は、幅の中なら、依頼者の時間帯・NG時間の外でも、門が
        通す（2026-10-22 ユーザー）。同じときに、幅を持たない電話は止める（理由 outside_hours）。"""
        s = self.ready()
        self.put_client(s, "calling.hours", {k: [] for k in KEYS})
        self.put_client(s, "calling.ng_hours", {k: [["00:00", "24:00"]] for k in KEYS})
        clock = self.tx(s, lambda tx: tx.one(
            "SELECT now() AS now, extract(epoch FROM salescall.jst(now()) - date_trunc('day', salescall.jst(now()))) AS sec"))
        now, sec = clock["now"], int(clock["sec"])
        if sec > 22 * 3600:
            self.skipTest("日本時間の22時より後：幅が日をまたぐ")
        self.tx(s, lambda tx: support.schedule_call(tx, s["task"], now, contact_id=s["contact"], cause="試験：幅の約束",
                                                       promised=True,
                                                       window=next_action.Window(now + timedelta(days=1), (max(0, sec - 60), sec + 7200),
                                                                                 said=(True, True))))
        c = self.claim(s)
        self.assertEqual(type(c).__name__, "Claim", getattr(c, "reason", None))
        self.tx(s, lambda tx: attempts.release(tx, [c.attempt_id]))
        self.tx(s, lambda tx: support.schedule_call(tx, s["task"], now, contact_id=s["contact"], cause="試験：幅を持たない約束",
                                                       promised=True))
        self.assertEqual(self.reason(s), "outside_hours")

    def test_a_round_that_has_not_started_is_not_called_before_its_start(self):
        """回の始まり（「週末なら」の土曜0時など）より前には、かける時刻を選ばず、門も止める（⛔ 2026-10-22 の見直し14：前は、
        いまが回の始まりより前だと、始まりより前の時刻を返し、門も回の終わりしか見なかった）。"""
        s = self.ready()
        now = self.tx(s, lambda tx: tx.one("SELECT now() AS t")["t"])
        w = next_action.Window(now + timedelta(hours=5), (0, 24 * 3600), None, now + timedelta(hours=2), "day")
        at, w2 = self.tx(s, lambda tx: next_action._window_opening(tx, s["task"], now, w, now=now))
        self.assertGreaterEqual(at, w.since)
        self.assertEqual(w2, w)
        self.tx(s, lambda tx: support.schedule_call(tx, s["task"], now, contact_id=s["contact"], cause="試験", promised=True,
                                                       window=w))
        self.assertEqual(self.reason(s), "callback_window")

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

        from salescall.agent import backup as pc
        roots = {x["tenant"]: Path(tempfile.mkdtemp(prefix="salescall-test-rec-")) for x in (s, t)}   # 契約者ごとの PC
        for root in roots.values():
            (root / pc.DRIVE_MARK).write_text("控え用")
        long_ago = datetime.now().astimezone() - timedelta(days=200)    # 2か月（60日）より古い
        files = {}
        for x, aid in ((s, normal), (t, checked), (s, uuid.uuid4())):
            d = pc.Drive(roots[x["tenant"]]).place([backup.client_folder(self.w.a, x["tenant"], x["client"]), backup.RECORDING])
            d.mkdir(parents=True, exist_ok=True)
            files[aid] = d / backup.recording_file_name(aid, long_ago, "call")
            files[aid].write_bytes(b"x")
        prune = lambda x: pc.prune(pc.Drive(roots[x["tenant"]]), backup.prune_rules(self.w.a, x["tenant"]))   # noqa: E731
        held = lambda x: {h for r in backup.prune_rules(self.w.a, x["tenant"]).values() for h in r["held"]}   # noqa: E731
        self.assertEqual((str(normal) in held(s), str(checked) in held(s)), (True, False))   # ほかの契約者の通話は渡さない
        for x in (s, t):                                                 # 片付けは契約者ごと（決まりはサーバー・消すのはその契約者の PC）
            prune(x)
        self.assertEqual([f.exists() for f in files.values()], [True, True, False])   # 印の無い古い録音だけ消す

        self.assertEqual(self.tx(s, lambda tx: recordings.release(tx, [normal], "sample", measurement="試験の誤判定率 第1回")), 1)
        self.tx(t, lambda tx: recordings.release(tx, [checked], "verification", measurement="試験の検証の期間"))
        with self.assertRaises(Rejected):                                # 外した印は戻せない
            self.tx(t, lambda tx: tx.execute("UPDATE salescall.recording_holds SET released_at = NULL, released_for = NULL"))
        for x in (s, t):
            prune(x)
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

    def test_apology_reasons_and_missed_rounds_are_kept_consistent(self):
        """お詫びの印は理由から作る列（理由があれば印。0116）で、理由は決まったものだけ。かけられなかった回の数は、過ぎた回の数を
        超えない（0113）。"""
        s = self.ready()
        sql = ("INSERT INTO salescall.task_next_actions (tenant_id, client_id, task_id, kind, call_at, cause, promised,"
               " contact_id, purpose, window_missed, apology_reasons)"
               " SELECT tenant_id, client_id, task_id, kind, call_at, cause, promised, contact_id, purpose, %s, %s"
               " FROM salescall.current_next_actions WHERE task_id = %s")
        for missed, reasons, constraint in ((0, ["sorry"], "task_next_actions_apology_reasons"),
                                            (1, [], "task_next_actions_window_missed")):
            with self.subTest(reasons=reasons, missed=missed), self.assertRaises(Rejected) as e:
                self.tx(s, lambda tx: tx.execute(sql, (missed, reasons, s["task"])))
            self.assertEqual(e.exception.constraint, constraint)
        self.tx(s, lambda tx: tx.execute(sql, (0, ["missed_promise", "cut_off"], s["task"])))
        self.assertTrue(self.tx(s, lambda tx: next_action.current(tx, s["task"]))["apology_owed"])
        c = self.claim(s)
        self.assertEqual(c.apology, ("missed_promise", "cut_off"))

    def test_fixed_purposes_always_carry_the_promise_mark(self):
        """お詫びの電話・すぐのかけ直し（続き）・番号の聞き直しの予定は、必ず約束の印を持つ（先に出す・取り置く電話＝Q38。0126：
        付け忘れると、お詫びの電話がふつうの電話の後ろに回され、取り置きもされない）。ふつうの電話は、印が無くても書ける。"""
        s = self.ready()
        sql = ("INSERT INTO salescall.task_next_actions (tenant_id, client_id, task_id, kind, call_at, cause, promised,"
               " contact_id, purpose, wrong_contact_id, number_again)"
               " SELECT tenant_id, client_id, task_id, 'call', now(), cause, %s, contact_id, %s,"
               " CASE WHEN %s = 'ask_number_again' THEN contact_id END, CASE WHEN %s = 'ask_number_again' THEN 'wrong_party' END"
               " FROM salescall.current_next_actions WHERE task_id = %s")
        for purpose in ("apology", "resume", "ask_number_again"):
            with self.subTest(purpose=purpose), self.assertRaises(Rejected) as e:
                self.tx(s, lambda tx: tx.execute(sql, (False, purpose, purpose, purpose, s["task"])))
            self.assertEqual(e.exception.constraint, "task_next_actions_promise_mark")
            self.tx(s, lambda tx: tx.execute(sql, (True, purpose, purpose, purpose, s["task"])))
        self.tx(s, lambda tx: tx.execute(sql, (False, "normal", "normal", "normal", s["task"])))

    def test_the_successor_mark_is_only_on_a_call(self):
        """後任のご担当者様をお呼びする印は、かける予定にだけ付く（終わる予定には付かない。0114）。"""
        s = self.ready()
        sql = ("INSERT INTO salescall.task_next_actions (tenant_id, client_id, task_id, seq, kind, call_at, end_reason, cause,"
               " promised, contact_id, purpose, successor)"
               " SELECT tenant_id, client_id, task_id, seq + 1, 'end', NULL, 'not_reached_departed', cause, NULL, NULL, NULL,"
               " true FROM salescall.current_next_actions WHERE task_id = %s")
        with self.assertRaises(Rejected) as e:
            self.tx(s, lambda tx: tx.execute(sql, (s["task"],)))
        self.assertEqual(e.exception.constraint, "task_next_actions_successor_calls")

    def test_the_said_edges_belong_to_a_daily_window(self):
        """お客様が口にした端の印（window_said。深夜早朝の歯止めの外でも切らない端）は、1日の中の幅にだけ、始まり・終わりの2つで持てる
        （0118：幅の無い印・数の違う印は、門が歯止めの外にかけうる）。"""
        s = self.ready()
        sql = ("INSERT INTO salescall.task_next_actions (tenant_id, client_id, task_id, kind, call_at, cause, promised, contact_id,"
               " purpose, window_until, window_daily, window_said)"
               " SELECT tenant_id, client_id, task_id, 'call', now(), cause, true, contact_id, 'normal', now() + interval '1 day',"
               " %s, %s FROM salescall.current_next_actions WHERE task_id = %s")
        for daily, said in ((None, [True, True]), ([16 * 3600, 18 * 3600], [True]), ([16 * 3600, 18 * 3600], [True, None])):
            with self.subTest(daily=daily, said=said), self.assertRaises(Rejected) as e:
                self.tx(s, lambda tx: tx.execute(sql, (daily, said, s["task"])))
            self.assertEqual(e.exception.constraint, "task_next_actions_window_said_check")
        self.tx(s, lambda tx: tx.execute(sql, ([16 * 3600, 18 * 3600], [True, False], s["task"])))

    def test_item_reason_only_when_not_answered(self):
        s = self.ready()
        aid = self.w.insert_claim(s)
        sql = ("INSERT INTO salescall.item_values (tenant_id, client_id, task_id, version_id, question_id, state, reason, attempt_id)"
               " VALUES (%s, %s, %s, %s, 'Q1', %s, %s, %s)")
        with self.assertRaises(Rejected):
            self.tx(s, lambda tx: tx.execute(sql, (s["tenant"], s["client"], s["task"], s["version"], "confirmed", "undecided", aid)))
        with self.assertRaises(Rejected):                                          # どの通話で取ったかは必ず要る（0108）
            self.tx(s, lambda tx: tx.execute(sql, (s["tenant"], s["client"], s["task"], s["version"], "confirmed", None, None)))
        self.tx(s, lambda tx: tx.execute(sql, (s["tenant"], s["client"], s["task"], s["version"], "dont_know", "undecided", aid)))


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
        with self.assertRaises(ValueError):   # 回線の理由は、行の予定を書き直さない（gate.PLAN_KEPT。見直し17a）
            self.tx(t, lambda tx: next_action.after_denial(tx, t["task"], d, tenant_id=t["tenant"], client_id=t["client"],
                                                           business_id=t["business"]))
        u = self.ready()
        self.past_attempt(u, ["dialing", "ringing", "no_answer"])
        d = self.claim(u)
        self.assertEqual(d.reason, "same_contact_interval")
        got = self.tx(u, lambda tx: next_action.after_denial(tx, u["task"], d, tenant_id=u["tenant"], client_id=u["client"],
                                                             business_id=u["business"]))
        self.assertEqual((got.kind, got.at), ("call", self.tx(u, lambda tx: gate.pacing(tx, u["task"])).earliest))   # 間隔が空く時刻へ1回

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
