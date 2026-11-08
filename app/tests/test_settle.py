"""締めの処理の試験（1回の発信のあと：判定 → 結果の版・切れ方・発信禁止・次の動きを1つの取引で）。DESIGN.md 7章。"""

from __future__ import annotations

import copy
import re
import tempfile
import time
import unittest
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from salescall.core import (approval, attempts, backup, business_rules, calling_hours, call_report, dnc, exclusions, fix_requests, gate,
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
from tests.test_gate import ALL_DAY, HOLDER, KEYS, Base, FakeDriver, dial_exit

TALK = ["dialing", "answered", "in_conversation", "ending", "ended"]


def rep(parties=("target",), *, dnc_=False, callback=None, cut=Cut("ai", True, False), not_reached=None):
    return make_report(parties, dnc=dnc_, callback=callback, cut=cut, not_reached=not_reached)


def row_fixes_fix(tx, task_id, phone, keep):
    """契約者が「担当の方は違う」で、かける番号を直す（keep＝前の担当の方との約束も守るか。計画書 7-1 (21)）。"""
    from salescall.core import row_fixes
    return row_fixes.fix_dial_number(tx, task_id, phone, same_person=False, keep_old_promise=keep)


class SettleBase(Base):
    """締めの試験の土台（試験は持たない）。"""
    def call(self, s, path=TALK, *, report=None, items=(), verify="done", side=None):
        """本物の門・出口を通して1回かけ、path まで進め、項目と報告を置く（締めはまだ）。"""
        c = self.claim(s)
        self.assertIsInstance(c, gate.Claim, c)
        self.assertTrue(dial_exit.dial(self.w.a, SYSTEM, s["tenant"], s["client"], c.attempt_id, FakeDriver()))
        for st in path[1:]:
            self.tx(s, lambda tx: attempts.move(tx, c.attempt_id, st, hangup_side=side if st == "dropped" else None))
        for q, state, cond, *hedged in items:   # 4つ目があれば、言い切っていない返事の言葉（「曖昧」の印）
            self.tx(s, lambda tx: tx.execute(
                "INSERT INTO salescall.item_values (tenant_id, client_id, task_id, version_id, question_id, state, condition,"
                " hedged, attempt_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (s["tenant"], s["client"], s["task"], s["version"], q, state, cond, (hedged or [None])[0], c.attempt_id)))
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
        self.assertEqual(self.tx(s, lambda tx: next_action._round_calls(tx, s["task"], w, 9, None)), 1)
        self.assertEqual(self.tx(s, lambda tx: next_action._round_calls(tx, s["task"], replace(w, since=None), 2, None)), 2)

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
        """またかけてよいと言われたときの、同じ回の中のかけ直しの上限は、その回のその日にかけた数（幅の約束の1日の回数＝業務ごとの設定
        callback.window_daily_max。ここでは1回）。1本目で次の回へ（⛔ 2026-10-22 の見直し14：前は、別の結果の回数と混ぜて数えていた。⛔ 0215
        まで：比べる相手は不在の方針の回数だった＝✅ 2026-11-08 ユーザー＝計画書 7-3 (3)「1日3回」。0216）。"""
        s = self.ready(rules=rules_raw(retry={k: {"action": "call", "max_times": 5, "interval": {"hours": 1}}
                                              for k in business_rules.POLICY_KEYS}))
        self.put_client(s, "callback.window_daily_max", 1)
        clock = self.tx(s, lambda tx: tx.one("SELECT now() AS now, salescall.jst_day_start(salescall.jst(now())::date + 1) AS midnight"))
        window = next_action.Window(min(clock["now"] + timedelta(hours=3), clock["midnight"]), (0, 24 * 3600), None,
                                    clock["now"] - timedelta(hours=1), "day")   # 今日のうちに終わる回
        self.tx(s, lambda tx: support.schedule_call(tx, s["task"], clock["now"], contact_id=s["contact"], cause="試験", promised=True,
                                                       window=window))
        first = self.settle(s, self.call(s, report=self.report(True)))["decision"]
        self.assertEqual((first.promised, first.window.round), (True, 1))       # その日の1本目で回数に届いた：回の終わり（次の回）へ
        self.assertGreaterEqual(first.at, window.until)
        # 回が日をまたいで続く幅（終わりが明日）でも、その日の回数に届いたら、その日はかけない（次の日から＝同じ回のまま）
        t = self.ready(rules=rules_raw(retry={k: {"action": "call", "max_times": 5, "interval": {"hours": 1}}
                                              for k in business_rules.POLICY_KEYS}))
        self.put_client(t, "callback.window_daily_max", 1)
        self.promised(t, window_hours=48)
        got = self.settle(t, self.call(t, report=self.report(True)))["decision"]
        tomorrow = self.tx(t, lambda tx: tx.one("SELECT salescall.jst_day_start(salescall.jst(now())::date + 1) AS d")["d"])
        self.assertEqual((got.promised, got.window.round), (True, 0))
        self.assertGreaterEqual(got.at, tomorrow)

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
            self.assertEqual(r.may_call_again(lambda c: c.time_said is None), again is True)
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


    def test_every_row_count_starts_again_when_the_number_is_fixed(self):
        """かける番号をダッシュボードで直したら、行の回数（行の回数の上限・再発信の方針の回数・「あとで」と言われた回数・時刻の決まって
        いない約束の電話の数）は、どれも直したときから数え直す（2026-11-06 ユーザー：「新しい番号で数え直す」。数え始めは
        salescall.task_count_from の1か所＝0180、数える範囲は attempts.in_row_count の1か所）。"""
        from salescall.core import row_fixes
        keys = ("open", "incomplete", "not_reached_absent", "callback_unspecified_time")
        s = self.ready(rules=rules_raw(retry={k: {"action": "call", "max_times": 3, "interval": {"hours": 1}} for k in keys}))
        self.put_client(s, "retry.max_attempts_per_task", 3)
        aid = self.call(s, report=rep(callback=callback("later", time="あとで")))
        self.assertEqual(self.settle(s, aid)["state"], "callback_scheduled")
        now = self.tx(s, lambda tx: tx.one("SELECT now() AS t")["t"])
        self.tx(s, lambda tx: support.schedule_call(tx, s["task"], now, contact_id=s["contact"], cause="試験：いまかける",
                                                       promised=True))
        self.settle(s, self.call(s, ["dialing", "no_answer"]))
        key = "callback_unspecified_time"
        counts = lambda: self.tx(s, lambda tx: (   # noqa: E731
            next_action._times(tx, s["task"], key), len(next_action._laters(tx, s["task"])),   # 「あとで」の並び（見直し(3)の4）
            next_action._unspecified_calls(tx, s["task"]), gate.pacing(tx, s["task"], promised=False).remaining))
        self.assertEqual(counts(), (1, 1, 1, 2))
        new = self.tx(s, lambda tx: row_fixes.fix_dial_number(tx, s["task"], fictional_jp_fixed(), same_person=True), OPERATOR)
        self.assertEqual(counts(), (0, 0, 0, 3))
        # まだかけていない約束の電話は、まず前の番号で（2026-11-06 ユーザー）。出なければ、次は新しい番号で、約束のまま。前の番号へ
        # かけたその1本は、新しい番号の回数に数えない（salescall.task_number_replaced＝0181）
        now = self.tx(s, lambda tx: tx.one("SELECT now() AS t")["t"])
        self.tx(s, lambda tx: support.schedule_call(tx, s["task"], now, contact_id=s["contact"], cause="試験：いまかける",
                                                       promised=True))
        d = self.settle(s, self.call(s, ["dialing", "no_answer"]))["decision"]
        self.assertEqual((d.kind, d.promised, d.target.contact_id), ("call", True, new["contact_id"]))
        self.assertEqual(counts(), (0, 0, 0, 3))
        # 取り込んだ番号へ直し戻しても、数え始めは、いちばん新しい直しのとき（salescall.task_count_from の1か所）。直す前に取り込んだ
        # 番号へかけた電話は、その番号がまたいまかける番号になっても数えない（前にかける番号だった番号かは、いまの立場で読むため、
        # 数え始めが無いと数えてしまう）
        first = self.tx(s, lambda tx: tx.one("SELECT phone_e164 FROM salescall.contacts WHERE contact_id = %s", (s["contact"],))["phone_e164"])
        back = self.tx(s, lambda tx: row_fixes.fix_dial_number(tx, s["task"], first, same_person=True), OPERATOR)
        self.assertEqual(back["contact_id"], s["contact"])
        self.assertEqual(counts(), (0, 0, 0, 3))


    def test_a_promise_moved_to_the_new_number_counts_its_rounds_again(self):
        """約束の電話が、前にかける番号だった番号から、いまかける番号へ移るときだけ、約束の回（何回目か・かけられなかった回）を0から
        数え直す（幅・時刻・理由は変えない。2026-11-06 ユーザー：「数え直す（お客様との約束は変更しない）」）。"""
        old, new, other = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        w = next_action.Window(None, (9 * 3600, 12 * 3600), repeat="day", round=2, missed=1)
        n = next_action.Numbers(dial=new, superseded=frozenset({old}))
        T = next_action.Target
        # 1つ目の引数＝かけた番号が、かけたときに前にかける番号だった番号か（0188 の task_number_replaced_at）
        self.assertEqual(next_action.recount_on_switch(w, True, T(new), n), replace(w, round=0, missed=0))
        self.assertEqual(next_action.recount_on_switch(w, False, T(new), n), w)       # 移らない
        self.assertEqual(next_action.recount_on_switch(w, True, T(other), n), w)      # お客様が言った別の番号へ
        self.assertIsNone(next_action.recount_on_switch(None, True, T(new), n))

    def test_a_promise_moving_to_the_new_number_counts_its_rounds_again_after_a_call_or_the_gate(self):
        """番号を直したあとも、まだかけていない約束の電話は前の番号で。出なかった（通話のあと）・その番号へもうかけられない（門の
        number_unusable）ときは、新しい番号へ約束のまま移り、約束の回は0から数え直す（幅・理由は変えない。2026-11-06 ユーザー）。"""
        from salescall.core import row_fixes
        for via in ("call", "gate"):
            with self.subTest(via=via):
                s = self.ready()
                new = self.tx(s, lambda tx: row_fixes.fix_dial_number(tx, s["task"], fictional_jp_fixed(), same_person=True), OPERATOR)
                now = self.tx(s, lambda tx: tx.one("SELECT now() AS t")["t"])
                w = next_action.Window(now + timedelta(hours=1), None, None, now - timedelta(hours=1), "week", round=2)
                self.tx(s, lambda tx: support.schedule_call(tx, s["task"], now, contact_id=s["contact"], cause="試験：約束",
                                                               promised=True, window=w))
                if via == "call":
                    d = self.settle(s, self.call(s, ["dialing", "no_answer"]))["decision"]
                    # 前の番号へかけたその1本は、約束の回の中の本数にも数えない（幅の約束・時刻ちょうどの約束の、その回の本数）
                    later = self.tx(s, lambda tx: tx.one("SELECT now() AS t")["t"])
                    self.assertEqual(self.tx(s, lambda tx: next_action._round_calls(tx, s["task"], w, 9, None)), 0)
                    day = next_action.Window(None, (0, 86400), None, later, None, round=1)   # その日の約束の回（1本だけの回）
                    end = self.tx(s, lambda tx: next_action._slot_of(day))[1]
                    if end - later > timedelta(minutes=10):                      # 日の終わりの間際は、その日の回の外になる
                        self.assertLess(self.tx(s, lambda tx: next_action._exact_next_try(tx, s["task"], replace(day, until=end),
                                                                                          later, None)), end)   # 前の番号の1本で使い切らない
                else:
                    scope = dict(tenant_id=s["tenant"], client_id=s["client"], business_id=s["business"])
                    d = self.tx(s, lambda tx: next_action.after_denial(tx, s["task"], gate.denied("number_unusable"), **scope))
                self.assertEqual((d.kind, d.promised, d.target.contact_id), ("call", True, new["contact_id"]))
                self.assertLessEqual(d.window.round, 1)                      # 前の番号の2回目のままなら2以上
                self.assertEqual((d.window.repeat, d.window.daily), (w.repeat, w.daily))   # 約束は変えない

    def test_the_old_number_becomes_the_sub_when_we_talk_on_it(self):
        """番号を直したあと、まず前の番号でかけた約束の電話でお客様と話せたら、前の番号はサブになる（0182）。次はメイン（新しい番号）へ。
        メインで話せなければサブへ、サブのあとはメインへ（2026-11-06 ユーザー：「話せたら、新番号がメイン、旧番号はサブ」・「かけ直しの
        約束をしていて、旧番号でつながった場合、新しい番号がつながらないときに元の番号へもかける」）。"""
        from salescall.core import customer_contacts, row_fixes
        s = self.ready()
        old = s["contact"]
        new = self.tx(s, lambda tx: row_fixes.fix_dial_number(tx, s["task"], fictional_jp_fixed(), same_person=True), OPERATOR)["contact_id"]
        now = lambda: self.tx(s, lambda tx: tx.one("SELECT now() AS t")["t"])   # noqa: E731
        at = lambda c: self.tx(s, lambda tx: support.schedule_call(tx, s["task"], now(), contact_id=c, cause="試験：いまかける",   # noqa: E731
                                                                     promised=True))
        at(old)                                                                 # まだかけていない約束の電話（前の番号）
        d = self.settle(s, self.call(s, report=rep(callback=callback("later", time="あとで"))))["decision"]
        self.assertEqual((d.kind, d.promised, d.target.contact_id), ("call", True, new))       # 話せた ⇒ 次はメイン
        self.assertEqual(self.tx(s, lambda tx: next_action.numbers_of(tx, s["task"])).sub, old)
        nums = {n["phone"]: (n["dialing"], n["sub"]) for n in self.tx(s, lambda tx: customer_contacts.now(tx, s["task"]))["numbers"]}
        self.assertEqual(nums[s["phone"]], (False, True))
        at(new)
        d = self.settle(s, self.call(s, ["dialing", "no_answer"]))["decision"]
        self.assertEqual((d.kind, d.promised, d.target.contact_id), ("call", True, old))       # メインで話せない ⇒ サブ
        at(old)
        d = self.settle(s, self.call(s, ["dialing", "no_answer"]))["decision"]
        self.assertEqual((d.kind, d.promised, d.target.contact_id), ("call", True, new))       # サブのあとはメイン
        # サブへの電話は、新しい番号の回数に数える（前にかける番号だった番号ではない＝0182 で task_number_replaced から外した）
        self.assertFalse(self.tx(s, lambda tx: tx.one("SELECT salescall.task_number_replaced(%s, %s) AS r", (s["task"], old))["r"]))

    def test_a_planned_call_to_the_sub_that_our_program_ended_stays_on_the_sub(self):
        """メインで話せず次がサブへの電話に決まったあと、そのサブへの電話がこちらのせいで鳴らなかったら、かけた予定のまま（サブへ）
        かけ直す（`_line_fault_again`＝かけなかったのと同じ。予定を向け直すだけの `_point` はサブをメインへ替えない＝current_dial の
        called。⛔ 2026-11-06 の見直し：前はメインへ替わり、つながらなかったメインへまたかけた）。"""
        from salescall.core import row_fixes
        s = self.ready()
        old = s["contact"]
        new = self.tx(s, lambda tx: row_fixes.fix_dial_number(tx, s["task"], fictional_jp_fixed(), same_person=True), OPERATOR)["contact_id"]
        now = lambda: self.tx(s, lambda tx: tx.one("SELECT now() AS t")["t"])   # noqa: E731
        at = lambda c: self.tx(s, lambda tx: support.schedule_call(tx, s["task"], now(), contact_id=c, cause="試験：いまかける",   # noqa: E731
                                                                     promised=True))
        at(old)
        self.settle(s, self.call(s, report=rep(callback=callback("later", time="あとで"))))   # 前の番号で話せた ⇒ サブ
        at(new)
        d = self.settle(s, self.call(s, ["dialing", "no_answer"]))["decision"]
        self.assertEqual(d.target.contact_id, old)                                            # メインで話せない ⇒ サブ
        at(old)                                                                               # その時刻が来た（サブへの予定のまま）
        aid = self.call(s, ["dialing", "ringing"])
        self.assertTrue(self.tx(s, lambda tx: attempts.end_for_trouble(tx, aid, "runner_error")))
        self.tx(s, lambda tx: health.after_call(tx, aid))
        d = self.settle(s, aid)["decision"]
        self.assertEqual((d.kind, d.target.contact_id), ("call", old))                       # かけた予定のまま（サブへ）
        self.assertIn("こちらのせい", d.rule)

    def test_a_dead_main_number_goes_to_the_sub_first(self):
        """メインの番号が使えない（鳴らない電話が続いた）ときは、まずサブの番号へ（お客様が言った別の番号より先）。"""
        main, subn, said = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        n = next_action.Numbers(unusable=frozenset({main}), said=(said,), dial=main, sub=subn)
        self.assertEqual(next_action.usable(next_action.Target(main), n).contact_id, subn)
        self.assertEqual(next_action.usable(next_action.Target(main), replace(n, sub=None)).contact_id, said)

class DialFixAfterCalls(SettleBase):
    """番号を直したあとの電話の流れ（2026-11-06 の見直し (A)。ユーザー：「旧番号でかからなければ、新番号でかけます」・「数え直す
    （お客様との約束は変更しない）」・「お客様の言った番号へ」）。"""

    def fixed(self, s):
        from salescall.core import row_fixes
        return self.tx(s, lambda tx: row_fixes.fix_dial_number(tx, s["task"], fictional_jp_fixed(), same_person=True), OPERATOR)["contact_id"]

    def at(self, s, contact, **kw):
        now = self.tx(s, lambda tx: tx.one("SELECT now() AS t")["t"])
        kw.setdefault("promised", True)
        self.tx(s, lambda tx: support.schedule_call(tx, s["task"], now, contact_id=contact, cause="試験：いまかける", **kw))
        return now

    def test_a_promise_on_the_old_number_that_ends_by_the_number_goes_on_to_the_new_number(self):
        """直したあと、まだかけていなかった約束の電話を前の番号でかけて番号違いだった（番号のせいで終える決定）⇒ 行を終えずに、いまかける
        番号へ約束のまま（⛔ 見直し (A) の1：前は行が終わり、番号を入れ直しても戻らなかった）。"""
        s = self.ready()
        new = self.fixed(s)
        self.at(s, s["contact"])
        d = self.settle(s, self.call(s, report=rep(("wrong_party",), cut=Cut("ai", True, False))))["decision"]
        self.assertEqual((d.kind, d.promised, d.target.contact_id), ("call", True, new))

    def test_a_promise_moved_after_talking_on_the_old_number_counts_its_rounds_again(self):
        """前の番号で受付の方と話せたが目的のお客様が不在：前の番号はサブになっても、約束の回は0から数え直し、その1本は新しい番号の
        回数にも数えない（かけたときの立場で見る＝0188 の task_number_replaced_at。⛔ 見直し (A) の2：話せなかったときと食い違った）。"""
        s = self.ready()
        new = self.fixed(s)
        now = self.tx(s, lambda tx: tx.one("SELECT now() AS t")["t"])
        w = next_action.Window(now + timedelta(hours=1), None, None, now - timedelta(hours=1), "week", round=2)
        self.at(s, s["contact"], window=w)
        d = self.settle(s, self.call(s, report=rep(("reception",), not_reached="absent")))["decision"]
        self.assertEqual((d.kind, d.promised, d.target.contact_id), ("call", True, new))
        self.assertLessEqual(d.window.round, 1)
        self.assertEqual(self.tx(s, lambda tx: next_action.numbers_of(tx, s["task"])).sub, s["contact"])   # サブにはなる
        self.assertEqual(self.tx(s, lambda tx: next_action._round_calls(tx, s["task"], w, 9, None)), 0)

    def test_a_promise_already_tried_on_the_old_number_moves_at_the_fix(self):
        """約束の電話を前の番号でもうかけていた（鳴った）なら、番号を直したときに新しい番号へ移し、約束の回を0から数え直す（「まだ約束の
        電話をかけていない場合は、旧番号で」の外。⛔ 見直し (A) の8 (2)）。"""
        from salescall.core import row_fixes
        s = self.ready()
        now = self.tx(s, lambda tx: tx.one("SELECT now() AS t")["t"])
        w = next_action.Window(now + timedelta(hours=1), None, None, now - timedelta(hours=1), "week", round=2)
        self.at(s, s["contact"], window=w)
        self.settle(s, self.call(s, ["dialing", "no_answer"]))                  # 前の番号で約束の電話に出なかった
        self.assertEqual(self.tx(s, lambda tx: next_action.current(tx, s["task"]))["contact_id"], s["contact"])
        new = self.tx(s, lambda tx: row_fixes.fix_dial_number(tx, s["task"], fictional_jp_fixed(), same_person=True), OPERATOR)["contact_id"]
        p = self.tx(s, lambda tx: next_action.current(tx, s["task"]))
        self.assertEqual((p["contact_id"], p["promised"], p["window_round"]), (new, True, 0))

    def test_the_number_the_callee_names_after_a_fix_is_called(self):
        """直したあと、新しい番号で話せたお客様が前の番号へかけてと言ったら、前の番号へ（出なくても、前の番号のまま）。直す前の指定は、
        あとの直しが勝つ（0188 の task_designations。✅ 2026-11-06 ユーザー：「お客様の言った番号へ」）。"""
        phone = fictional_jp_fixed()
        s = self.ready(phone=phone)
        new = self.fixed(s)
        self.at(s, new, promised=False)
        d = self.settle(s, self.call(s, report=rep(("reception",), callback=callback("other_number", None, phone, time="すぐ"))))
        self.assertEqual(d["decision"].target.contact_id, s["contact"])
        self.at(s, s["contact"])                                                # 試験：その時刻が来たことにする（同じお客様の間隔の外）
        d = self.settle(s, self.call(s, ["dialing", "no_answer"]))["decision"]
        self.assertEqual(d.target.contact_id, s["contact"])                      # 出なくても、お客様が指定した番号のまま
        again = self.fixed(s)                                                   # 指定のあとに、契約者がまた直した
        self.assertEqual(self.tx(s, lambda tx: next_action.current(tx, s["task"]))["contact_id"], again)

    def test_a_designation_counts_only_from_the_time_it_was_made(self):
        """お客様が指定した番号は、指定した時刻から（salescall.task_designated_contact_at の、その時刻までの指定だけ＝0196。第三者の見直し (2)
        の6：外しても試験が落ちなかった）。指定より前の時刻で読めば、指定は無い（その時刻の番号の立場を、あとの指定で変えない）。"""
        phone = fictional_jp_fixed()
        s = self.ready(phone=phone)
        new = self.fixed(s)
        before = self.tx(s, lambda tx: tx.one("SELECT now() AS t")["t"])
        self.at(s, new, promised=False)
        self.settle(s, self.call(s, report=rep(("reception",), callback=callback("other_number", None, phone, time="すぐ"))))
        at = lambda t: self.tx(s, lambda tx: tx.one("SELECT salescall.task_designated_contact_at(%s, %s) AS c", (s["task"], t))["c"])   # noqa: E731
        self.assertIsNone(at(before))
        self.assertEqual(at(self.tx(s, lambda tx: tx.one("SELECT now() AS t")["t"])), s["contact"])

    def test_a_call_made_again_as_planned_after_a_fix_goes_to_the_new_number(self):
        """前の番号への電話の途中で番号を直し、その電話がこちらのせい・回線のせいで鳴らなかった：かけた予定のままかけ直す道も、いまの
        番号へ向ける（`_point` の1か所。⛔ 見直し (A) の5：前は前の番号へのふつうの電話をまた書いた）。"""
        s = self.ready()
        aid = self.call(s, ["dialing", "ringing"])                               # 前の番号へかけている途中
        new = self.fixed(s)
        self.assertTrue(self.tx(s, lambda tx: attempts.end_for_trouble(tx, aid, "runner_error")))
        self.tx(s, lambda tx: health.after_call(tx, aid))
        d = self.settle(s, aid)["decision"]
        self.assertEqual((d.kind, d.target.contact_id), ("call", new))
        self.assertIn("こちらのせい", d.rule)

    def test_an_ended_promise_comes_back_at_its_time_from_the_first_day_unless_the_person_changed(self):
        """お客様と時刻を決めた約束で、決めた日数かけても話せずに終えた行は、番号を直すと、約束の時刻のまま1日目から数え直してかける。
        約束のあとにお呼びする方を直していれば（相手が違う）、最初から＝すぐかけてよいふつうの電話（✅ 2026-11-06 ユーザー：「約束の
        時刻（毎日15時）のまま、新しい番号で1日目から数え直してかける。だが、担当の相手が電話番号で違うのであれば、最初から。」）。"""
        from salescall.core import calling_hours, row_fixes
        # 相手が違う＝お呼びする方の名前・フルネーム・役職・部署のどれを直しても（⛔ 2026-11-06 の見直し：役職だけでしか見張れていなかった）
        for changed, value, reading in ((None, None, None), ("target_role", "営業部長", None), ("target_name", "架空", "かくう"),
                                        ("target_full_name", "架空 太郎", "かくう たろう"), ("target_department", "営業部", None)):
            with self.subTest(changed=changed):
                s = self.ready()
                now = self.tx(s, lambda tx: tx.one("SELECT now() AS t")["t"])
                local = calling_hours.local(now - timedelta(days=3))
                said = calling_hours.from_local(local.replace(hour=15, minute=0, second=0, microsecond=0))
                w = self.tx(s, lambda tx: next_action._exact_window(tx, s["task"], said, day_said=True, beyond_ok=False))
                self.tx(s, lambda tx: support.schedule_call(tx, s["task"], said, contact_id=s["contact"], cause="試験：約束",
                                                           promised=True, callee_at=said, window=w))
                if changed:
                    self.tx(s, lambda tx: row_fixes.fix(tx, s["task"], changed, value, reading=reading, same_person=False,
                                                        keep_old_promise=False), OPERATOR)   # 次の担当の方にだけかける（0213）
                self.tx(s, lambda tx: next_action.end(tx, s["task"], "callback_exact_unreached", cause="試験：3日かけて話せなかった"))
                new = self.fixed(s)
                p = self.tx(s, lambda tx: next_action.current(tx, s["task"]))
                self.assertEqual((p["kind"], p["contact_id"], p["promised"]), ("call", new, not changed))
                if not changed:
                    self.assertEqual((calling_hours.local(p["callee_at"]).hour, p["window_round"]), (15, 0))
                    self.assertGreaterEqual(p["callee_at"], now)
                    self.assertEqual(p["call_at"], max(p["callee_at"], now))   # 約束の時刻ちょうど（いまがその回の中なら、いま）

    def test_a_sub_from_before_the_latest_fix_does_not_count(self):
        """前の番号で話せてサブになったあと、もう一度直したら、そのサブは効かない（salescall.task_sub_contact_at＝いちばん新しい直しより
        後に記録したものだけ）。"""
        s = self.ready()
        self.fixed(s)
        self.at(s, s["contact"])
        self.settle(s, self.call(s, report=rep(("reception",), not_reached="absent")))
        self.assertEqual(self.tx(s, lambda tx: next_action.numbers_of(tx, s["task"])).sub, s["contact"])
        self.fixed(s)
        self.assertIsNone(self.tx(s, lambda tx: next_action.numbers_of(tx, s["task"])).sub)

    def test_only_a_promised_call_on_the_old_number_makes_the_sub(self):
        """前の番号で話せてもサブにするのは、お客様と約束したかけ直しの電話だけ（✅ 2026-11-06 ユーザー：伺い「サブの範囲」に「約束の
        電話だけ」。計画書 7-1 の (5)「かけ直しの約束をしていて、旧番号でつながった場合」）。約束でない電話で話せても、前の番号は
        灰色のまま（決めるのは salescall.call_makes_sub の1か所＝0190）。"""
        s = self.ready()
        self.fixed(s)
        self.at(s, s["contact"], promised=False)
        aid = self.call(s, report=rep(("reception",), not_reached="absent"))
        self.assertEqual(self.tx(s, lambda tx: tx.one("SELECT contact_id, promised FROM salescall.attempts WHERE attempt_id = %s",
                                                      (aid,))), {"contact_id": s["contact"], "promised": False})
        self.settle(s, aid)
        self.assertIsNone(self.tx(s, lambda tx: next_action.numbers_of(tx, s["task"])).sub)
        self.assertFalse(self.tx(s, lambda tx: tx.one("SELECT salescall.call_makes_sub(%s) AS m", (aid,))["m"]))
        with self.assertRaises(Rejected) as e:
            self.tx(s, lambda tx: tx.execute(
                "INSERT INTO salescall.task_sub_numbers (tenant_id, client_id, task_id, contact_id, attempt_id)"
                " VALUES (%s, %s, %s, %s, %s)", (s["tenant"], s["client"], s["task"], s["contact"], aid)), SYSTEM)
        self.assertEqual(e.exception.constraint, "task_sub_number_of_call")

    def test_a_resumed_call_on_the_old_number_does_not_make_the_sub(self):
        """途中で切れた電話の続き（目的 resume。約束の印は付く）を前の番号でかけて話せても、サブにしない（サブはお客様と約束したかけ直しの
        電話だけ＝目的 normal の約束。0190）。"""
        s = self.ready()
        self.fixed(s)
        self.at(s, s["contact"], purpose="resume")
        aid = self.call(s, report=rep(("reception",), not_reached="absent"))
        self.assertEqual(self.tx(s, lambda tx: tx.one("SELECT contact_id, purpose, promised FROM salescall.attempts WHERE attempt_id = %s",
                                                      (aid,))), {"contact_id": s["contact"], "purpose": "resume", "promised": True})
        self.settle(s, aid)
        self.assertIsNone(self.tx(s, lambda tx: next_action.numbers_of(tx, s["task"])).sub)

    def test_the_database_keeps_the_sub_number_to_the_old_number_of_the_call(self):
        """サブに書けるのは、その通話でかけた番号で、直したあとの通話で、かけたときに前にかける番号だった番号だけ（データベースの守り。
        ⛔ 見直し (A) の4：前は Python の1か所だけが守り、その通話でかけていない番号も書けた）。"""
        s = self.ready()
        new = self.fixed(s)
        self.at(s, new, promised=False)
        aid = self.call(s, ["dialing", "no_answer"])
        for contact in (s["contact"], new):
            with self.subTest(contact=contact), self.assertRaises(Rejected) as e:
                self.tx(s, lambda tx: tx.execute(
                    "INSERT INTO salescall.task_sub_numbers (tenant_id, client_id, task_id, contact_id, attempt_id)"
                    " VALUES (%s, %s, %s, %s, %s)", (s["tenant"], s["client"], s["task"], contact, aid)), SYSTEM)
            self.assertEqual(e.exception.constraint, "task_sub_number_of_call")

    # ── 2026-11-06 の見直し（0193）：業務を終えたあとのお詫び・戻した電話のお詫びの印・約束の電話の1か所・続きの電話・ほかの行の番号
    CUT = ("dialing", "ringing", "answered", "in_conversation", "dropped")   # お客様が出て話している途中で回線が切れた

    def cut(self, s):
        """途中で切れた電話（回線のせい・お客様が話していた）を締める。次の予定は続きの電話（お詫びの印 cut_off）。"""
        aid = self.call(s, self.CUT, side="line", report=rep(cut=Cut("callee", False, False)))
        d = self.settle(s, aid)["decision"]
        self.assertEqual((d.kind, d.target.purpose), ("call", "resume"))
        return aid

    def fix_to(self, s, phone, task=None):
        from salescall.core import row_fixes
        return self.tx(s, lambda tx: row_fixes.fix_dial_number(tx, task or s["task"], phone, same_person=True), OPERATOR)["contact_id"]

    def plans_after(self, s, seq):
        return self.tx(s, lambda tx: tx.all("SELECT kind, purpose, promised FROM salescall.task_next_actions WHERE task_id = %s"
                                            " AND seq > %s ORDER BY seq", (s["task"], seq)))

    def test_an_apology_after_the_business_ended_is_never_turned_into_a_sales_call(self):
        """言ってはいけないことで終えた行・お客様が業務を断った行（お詫びが付く＝core/apology.schedule_if_ready）のお詫びの電話が番号のせいで
        終わっても、番号を直して営業の電話に変えて戻さない（計画書 7-1 (2)「業務を終えた行は戻しません」。戻す形は next_action._restart の
        1か所）。番号を直したあと、門がお詫びの電話（前の番号）を番号のせいで止めても、新しい番号への営業の電話にしない（⛔ 2026-11-06 の
        見直し：前は、どちらも目的「ふつう」の営業の電話として戻した）。"""
        for ended in ("forbidden_speech", "declined"):
            for when in ("fix_after_the_apology_ended", "gate_after_the_fix"):
                with self.subTest(ended=ended, when=when):
                    s = self.ready()
                    self.tx(s, lambda tx: next_action.end(tx, s["task"], ended, cause="試験：業務を終えた"))
                    seq = self.tx(s, lambda tx: next_action.schedule_apology(tx, s["task"], s["contact"], cause="試験：お詫びの承認",
                                                                                     source=None))
                    if when == "fix_after_the_apology_ended":
                        self.tx(s, lambda tx: next_action.end(tx, s["task"], "number_unusable", cause="試験：お詫びの電話の番号が使えない"))
                        self.fixed(s)
                        p = self.tx(s, lambda tx: next_action.current(tx, s["task"]))
                        self.assertEqual((p["kind"], p["end_reason"]), ("end", "number_unusable"))
                    else:
                        self.fixed(s)
                        p = self.tx(s, lambda tx: next_action.current(tx, s["task"]))
                        self.assertEqual((p["purpose"], p["contact_id"]), ("apology", s["contact"]))   # お詫びは前の番号のまま
                        scope = dict(tenant_id=s["tenant"], client_id=s["client"], business_id=s["business"])
                        d = self.tx(s, lambda tx: next_action.after_denial(tx, s["task"], gate.denied("number_kind_forbidden"), **scope))
                        self.assertEqual((d.kind, d.end_reason), ("end", "number_kind_forbidden"))
                    self.assertNotIn("normal", [r["purpose"] for r in self.plans_after(s, seq)])

    def test_a_row_that_heard_later_too_often_comes_back_unless_a_later_was_a_refusal(self):
        """✅ 2026-11-08 ユーザー＝計画書 7-1 (27)：お客様が「あとで」を方針の回数より多く言って終えた行を、番号を直したとき「戻す」（営業拒否と読んだ
        「あとで」は断りとして別に終わるので、回数で終えた行の「あとで」は約束の一環とみなす＝7-1 (13) の「営業拒否でなければ戻せます」）。
        会話の報告のかけ直しの申し出の intent が無い「あとで」でも戻す（⛔ 0209 まで：まだ分からないとして戻さなかった＝`_later_refused`）。
        約束の電話を方針の回数かけて話せずに終えた行も戻す（決めた回数で終えた行）。
        ✅ 2026-11-06 ユーザー＝計画書 7-1 (16)：営業拒否と読んだ「あとで」は、その通話の判定で断り（発信の拒否＝もうかけない・発信禁止に
        登録。0195）なので、行はお客様が断った行（do_not_call）として終わり、番号を直しても戻らない（next_action.reopens_on_fix）。
        営業拒否と読んだ「あとで」は、受付の営業拒否と同じ事実 do_not_call で書く（intent では書かない＝2026-11-07 見直し(3)の1）。"""
        keys = ("open", "incomplete", "not_reached_absent", "callback_unspecified_time")
        for intents, back in (((None, None), True), (("promise", "refusal"), False), (("promise", "promise"), True),
                              (("unanswered",), True)):
            with self.subTest(intents=intents):
                s = self.ready(rules=rules_raw(retry={k: {"action": "call", "max_times": 1, "interval": {"hours": 1}} for k in keys}))
                for n, intent in enumerate(intents):
                    if n:
                        self.at(s, s["contact"])
                    if intent == "unanswered":   # 「あとで」は1回（方針の回数の中）。約束の電話に出ないまま方針の回数で終える
                        self.settle(s, self.call(s, report=rep(callback=callback("later", time="あとで"))))
                        self.at(s, s["contact"])
                        d = self.settle(s, self.call(s, ["dialing", "no_answer"]))["decision"]
                    else:   # 営業拒否と読んだ「あとで」は、事実 do_not_call（intent は書かない）
                        cb = replace(callback("later", time="あとで"), intent=None if intent == "refusal" else intent)
                        d = self.settle(s, self.call(s, report=rep(callback=cb, dnc_=intent == "refusal")))["decision"]
                why = "callback_unspecified_time" if intents == ("unanswered",) else \
                    "do_not_call" if "refusal" in intents else next_action.LATER_OVER
                self.assertEqual((d.kind, d.end_reason), ("end", why))
                if why == "do_not_call":   # 受付で営業拒否を言われたときと同じ：その番号は発信禁止（お客様が断った）
                    self.assertEqual(self.tx(s, lambda tx: tx.all(
                        "SELECT e.reason FROM salescall.dnc_entries e JOIN salescall.task_numbers n ON n.phone_e164 = e.phone_e164"
                        " WHERE n.task_id = %s AND e.revoked_at IS NULL", (s["task"],))), [{"reason": "refused_in_call"}])
                if intents == ("promise", "promise"):   # 約束の一環かは、判定の中身に記録として残す（会話の報告の intent。7-1 (13)）
                    self.assertEqual([r["i"] for r in self.tx(s, lambda tx: tx.all(
                        "SELECT detail -> 'callback' ->> 'intent' AS i FROM salescall.call_judgements WHERE task_id = %s"
                        " ORDER BY attempt_id", (s["task"],)))], ["promise", "promise"])
                new = self.fixed(s)
                p = self.tx(s, lambda tx: next_action.current(tx, s["task"]))
                self.assertEqual((p["kind"], p["contact_id"] if back else p["end_reason"]), ("call", new) if back else ("end", why))

    # ── 2026-11-07 見直し(3)の4：「あとで」と言われた回数と、戻すかを決めるときに見る「あとで」は、同じ並び（next_action._laters の1か所）
    LATER_ONCE = {**{k: {"action": "call", "max_times": 3, "interval": {"hours": 1}} for k in ("open", "incomplete", "not_reached_absent")},
                  "callback_unspecified_time": {"action": "call", "max_times": 1, "interval": {"hours": 1}}}

    def later_call(self, s, contact, intent):
        self.at(s, contact)
        cb = replace(callback("later", time="あとで"), intent=intent)
        return self.settle(s, self.call(s, report=rep(callback=cb)))["decision"]

    def test_the_laters_are_counted_on_every_number_since_the_count_start(self):
        """「あとで」と言われた回数（行を終えるか）は、時刻の決まっていない約束の判定だけ・数え始め以降の、どの番号への電話も（attempts.count_start。
        直したあと前の番号へかけた電話で言われた「あとで」も入る：お客様の言葉は、どの番号で言われても同じお客様の言葉。next_action._laters の
        1か所）。そうして終えた行は、番号を直せば、「あとで」の意味（intent）によらず戻る（✅ 2026-11-08 ユーザー＝計画書 7-1 (27)）。⛔ 2026-11-07 の
        見直し：数える範囲が行の回数（attempts.in_row_count）で、直したあと前の番号で言われた「あとで」を数えなかった。"""
        for intent in (None, "promise"):
            with self.subTest(intent=intent):
                s = self.ready(rules=rules_raw(retry=self.LATER_ONCE))
                new = self.fixed(s)
                d = self.later_call(s, s["contact"], intent)            # 直したあと、前の番号で約束の電話：「あとで」
                self.assertEqual(d.kind, "call")
                d = self.later_call(s, new, "promise")                  # 新しい番号で2回目の「あとで」（前の番号の「あとで」も数える）
                self.assertEqual((d.kind, d.end_reason), ("end", next_action.LATER_OVER))
                again = self.fixed(s)
                p = self.tx(s, lambda tx: next_action.current(tx, s["task"]))
                self.assertEqual((p["kind"], p["contact_id"]), ("call", again))

    def test_only_untimed_laters_are_counted(self):
        """数えるのは時刻の決まっていない約束の判定（「あとで」）だけ（ほかの結果の通話は数えない）。⛔ 2026-11-07 見直し(3)の8（M1）：絞りを外して
        落ちる試験が無かった。"""
        s = self.ready(rules=rules_raw(retry=self.LATER_ONCE))
        d = self.settle(s, self.call(s))["decision"]                # 必須の質問が取れていない（「あとで」ではない）
        self.assertEqual(d.kind, "call")
        self.assertEqual(self.later_call(s, s["contact"], "promise").kind, "call")
        d = self.later_call(s, s["contact"], "promise")
        self.assertEqual((d.kind, d.end_reason), ("end", next_action.LATER_OVER))
        self.assertEqual(len(self.tx(s, lambda tx: next_action._laters(tx, s["task"]))), 2)

    def test_a_row_put_back_by_a_fix_carries_no_apology_for_a_cut(self):
        """途中で切れたあと決めた回数までかけて終えた行を、番号を直して戻した電話には、途中で切れたお詫びの印を付けない（計画書 7-1 (11)。
        ⛔ 2026-11-06 の見直し：前は終えた予定の印と元の電話を引き継いだ）。続きの電話の予定は、切れた電話が守っていた予定に読み替える
        （ふつうの電話の続き ⇒ すぐかけてよいふつうの電話。約束の電話の続き ⇒ 約束の幅のまま1日目から）。"""
        for promised in (False, True):
            with self.subTest(promised=promised):
                s = self.ready()
                if promised:
                    now = self.tx(s, lambda tx: tx.one("SELECT now() AS t")["t"])
                    w = next_action.Window(now + timedelta(hours=1), None, None, now - timedelta(hours=1), "week", round=2)
                    self.at(s, s["contact"], window=w)
                self.cut(s)
                self.assertEqual(self.tx(s, lambda tx: next_action.current(tx, s["task"]))["apology_reasons"], ["cut_off"])
                self.tx(s, lambda tx: next_action.end(tx, s["task"], "max_attempts", cause="試験：決めた回数までかけた"))
                new = self.fixed(s)
                p = self.tx(s, lambda tx: next_action.current(tx, s["task"]))
                self.assertEqual((p["kind"], p["contact_id"], p["purpose"], p["promised"], p["apology_reasons"], p["cut_call"]),
                                 ("call", new, "normal", promised, [], None))
                if promised:
                    self.assertEqual((p["window_repeat"], p["window_round"]), ("week", 0))

    def test_a_resumed_call_moves_to_the_new_number_at_the_fix(self):
        """続きの電話（目的 resume。約束の印は付く）は約束の電話ではないので（データベースの salescall.promise_call の1か所）、番号を
        直したら、いまかける番号へ替える（設計書 3-4-1「続きの電話も、いまかける番号に替える」。行を終えていないので、お詫びの印はそのまま。
        ⛔ 2026-11-06 の見直し：前は約束の印だけを見て前の番号のままにした）。"""
        s = self.ready()
        aid = self.cut(s)
        new = self.fixed(s)
        p = self.tx(s, lambda tx: next_action.current(tx, s["task"]))
        self.assertEqual((p["contact_id"], p["purpose"], p["promised"], p["apology_reasons"], p["cut_call"]),
                         (new, "resume", True, ["cut_off"], aid))
        self.assertFalse(self.tx(s, lambda tx: tx.one("SELECT salescall.promise_call('resume', true) AS p")["p"]))

    def test_a_promise_made_on_a_resumed_call_goes_to_the_old_number_first(self):
        """ふつうの電話が切れたあとの続きの電話で、お客様とかけ直しを約束した ⇒ その約束の電話はまだかけていないので、番号を直しても、まず
        前の番号で（約束の始まり＝`_promise_since` は約束の電話の予定だけを見る＝`_is_promise` と同じ1か所。⛔ 2026-11-06 の見直し：前は
        続きの予定を約束の始まりにして、続きの電話を「約束の電話をもうかけた」とみなし、直したとたん新しい番号へ移した）。"""
        s = self.ready()
        self.cut(s)
        d = self.settle(s, self.call(s, report=rep(callback=callback("later", time="あとで"))))["decision"]
        self.assertEqual((d.promised, d.target.purpose, d.target.contact_id), (True, "normal", s["contact"]))
        self.fixed(s)
        p = self.tx(s, lambda tx: next_action.current(tx, s["task"]))
        self.assertEqual((p["contact_id"], p["promised"], p["purpose"]), (s["contact"], True, "normal"))

    def test_a_promise_on_the_old_number_stopped_by_the_gate_goes_on_to_the_new_number_with_its_apology(self):
        """直したあと、まだかけていない約束の電話（前の番号）を門が番号のせいで止めた ⇒ 新しい番号へ約束のまま（門のあとの `_moved_on_fix`）。
        行を終えていないので、お詫びの印（途中で切れた・その元の電話）は引き継ぐ。通話のあと（前の番号が番号違い）も同じ。"""
        for after in ("gate", "call"):
            with self.subTest(after=after):
                s = self.ready()
                aid = self.cut(s)
                new = self.fixed(s)
                self.at(s, s["contact"])                                       # まだかけていない約束の電話（前の番号）
                cur = self.tx(s, lambda tx: next_action.current(tx, s["task"]))
                self.assertEqual((cur["contact_id"], cur["apology_reasons"]), (s["contact"], ["cut_off"]))
                if after == "gate":
                    scope = dict(tenant_id=s["tenant"], client_id=s["client"], business_id=s["business"])
                    d = self.tx(s, lambda tx: next_action.after_denial(tx, s["task"], gate.denied("number_kind_forbidden"), **scope))
                else:
                    d = self.settle(s, self.call(s, report=rep(("wrong_party",), cut=Cut("ai", True, False))))["decision"]
                self.assertEqual((d.kind, d.promised, d.target.contact_id), ("call", True, new))
                p = self.tx(s, lambda tx: next_action.current(tx, s["task"]))
                self.assertEqual((p["apology_reasons"], p["cut_call"]), (["cut_off"], aid))

    def test_a_promise_on_the_old_number_we_could_not_talk_on_makes_no_sub(self):
        """前の番号へかけた約束の電話でも、お客様と話せなかった（出なかった）ら、サブにしない（話せたかは会話の報告の1か所＝after_call の
        connected）。"""
        s = self.ready()
        self.fixed(s)
        self.at(s, s["contact"])
        self.settle(s, self.call(s, ["dialing", "no_answer"]))
        self.assertIsNone(self.tx(s, lambda tx: next_action.numbers_of(tx, s["task"])).sub)

    def test_a_check_call_that_did_not_ring_on_the_old_number_is_made_again_to_the_new_number(self):
        """切れ方の分からない通話の確かめのかけ直し（前の番号）の途中で番号を直し、その電話が鳴らなかった ⇒ お詫びからのかけ直しは、
        いまかける番号へ（`_check_unrung` も `_point` の1か所）。"""
        s = self.ready()
        unclear = self.call(s, UnclearCutChecks.SILENT, side="callee", report=rep(cut=Cut("ai", False, False)))
        self.settle(s, unclear)
        check = self.call(s, ("dialing", "call_failed"))                     # 確かめのかけ直し（前の番号）
        new = self.fixed(s)
        self.assertEqual(self.tx(s, lambda tx: next_action.check_outcome(tx, check)).outcome, "unrung")
        d = self.settle(s, check)["decision"]
        self.assertEqual((d.kind, d.target.purpose, d.target.contact_id), ("call", "resume", new))

    def test_another_rows_numbers_it_may_still_dial_are_not_taken(self):
        """同じ実施回のほかの行がかけうる番号（サブ・いまの予定の番号・お客様が指定した番号）へは直せない（その行がかけうる番号は
        データベースの salescall.task_dialable の1か所。守りと追加の取込が読む＝0193）。⛔ 2026-11-06 の見直し：前は守りが、ほかの行の
        いまかける番号とサブだけを見て、まだかけていない約束の電話の番号・お客様が指定した番号へ直せた。"""
        from salescall.core import row_fixes
        for which in ("sub", "plan", "designated"):
            with self.subTest(which=which):
                s = self.ready()
                y = fictional_jp_fixed()
                ky = self.fix_to(s, y)                                         # 行B：取り込んだ番号 → y → w（y は前の番号）
                kw = self.fix_to(s, fictional_jp_fixed())
                if which == "sub":                                             # 前の番号 y で約束の電話に話せた ⇒ y はサブ
                    self.at(s, ky)
                    self.settle(s, self.call(s, report=rep(("reception",), not_reached="absent")))
                    self.assertEqual(self.tx(s, lambda tx: next_action.numbers_of(tx, s["task"])).sub, ky)
                elif which == "plan":                                          # まだかけていない約束の電話は y のまま
                    self.at(s, ky)
                else:                                                          # 新しい番号 w で話せたお客様が y を指定した
                    self.at(s, kw, promised=False)
                    self.settle(s, self.call(s, report=rep(("reception",), callback=callback("other_number", None, y, time="すぐ"))))
                if which != "plan":                                            # いまの予定の番号には残さない（その番号だけを見る）
                    self.tx(s, lambda tx: next_action.end(tx, s["task"], "completed", cause="試験：業務を終えた"))
                k = self.w.contact(s["tenant"], s["client"], normalize(fictional_jp_fixed()).e164)
                a = self.w.task(s["tenant"], s["client"], s["run"], k)       # 同じ実施回の行A
                with self.assertRaises(row_fixes.FixInvalid):
                    self.fix_to(s, y, task=a)

    # ── 「担当の方は違う」を、まだ終えていない行にも効かせる（2026-11-07 の見直し。前の番号からいまかける番号へ移る1か所＝
    #    next_action._onto_dial。計画書 7-1 (8)「担当の相手が電話番号で違うのであれば、最初から」・(12)「違う相手の場合は、お詫びなしで、
    #    どこまで引き継がれているのかを確認しつつ」・(19)）。同じ相手なら約束・お詫びのまま（7-1 (4)(6)）。
    def fixed_as(self, s, same, keep=None):
        from salescall.core import row_fixes
        return self.tx(s, lambda tx: row_fixes.fix_dial_number(tx, s["task"], fictional_jp_fixed(), same_person=same,
                                                               keep_old_promise=keep), OPERATOR)["contact_id"]

    def promise_window(self, s):
        now = self.tx(s, lambda tx: tx.one("SELECT now() AS t")["t"])
        w = next_action.Window(now + timedelta(hours=1), None, None, now - timedelta(hours=1), "week", round=2)
        self.at(s, s["contact"], window=w)

    def handover_now(self, s):
        """いまの予定の、引き継ぎを確かめる印（予定を書く1か所が求めて記録したもの＝next_action._handover_due。0203）。"""
        return self.tx(s, lambda tx: next_action.current(tx, s["task"]))["handover"]

    def plan(self, s):
        p = self.tx(s, lambda tx: next_action.current(tx, s["task"]))
        return (p["kind"], p["contact_id"], p["purpose"], p["promised"], p["callee_at"] is not None, p["window_until"] is not None,
                p["apology_reasons"], p["cut_call"] is not None, p["handover"])

    def handed_over(self, new):
        """違う相手への形：すぐかけてよいふつうの電話・約束も時刻も幅も無い・お詫びの印も元の電話も無い・引き継ぎを確かめる印。"""
        return ("call", new, "normal", False, False, False, [], False, True)

    def test_a_promise_already_tried_does_not_go_on_as_a_promise_to_another_person(self):
        """約束の電話を前の番号でもうかけた（鳴った）あと番号を直す（まだ終えていない行）：違う相手なら、約束をやめて引き継ぎを確かめる
        ふつうの電話（`after_dial_fix` も `_onto_dial` を通る）。同じ相手なら、約束のまま新しい番号で数え直す（7-1 (4)(6)）。"""
        for same in (True, False):
            with self.subTest(same=same):
                s = self.ready()
                self.promise_window(s)
                self.settle(s, self.call(s, ["dialing", "no_answer"]))
                new = self.fixed_as(s, same, None if same else False)   # 違う方：約束の日・幅が残っているので選ぶ（次の担当の方にだけ）
                p = self.plan(s)
                if same:
                    self.assertEqual(p[:4] + p[5:6] + p[8:], ("call", new, "normal", True, True, False))
                else:
                    self.assertEqual(p, self.handed_over(new))

    def test_an_untried_promise_that_fails_on_the_old_number_goes_on_without_the_promise_to_another_person(self):
        """まだかけていない約束の電話は前の番号で1回（7-1 (4)）。同じ相手なら、前の番号で出ずに新しい番号へ約束のまま移る（通話のあと＝
        `_moved_on_fix`）。違う相手で「約束も守る」を選んだなら、前の番号で出なくても、約束の決まりいっぱいまで前の担当の方の番号のまま（✅ 2026-11-08
        ユーザー＝計画書 7-1 (32)。next_action._kept_old_promise。⛔ 0213：1回で新しい番号の新しい担当の方へ移った）。"""
        for same in (True, False):
            with self.subTest(same=same):
                s = self.ready()
                self.promise_window(s)
                new = self.fixed_as(s, same, None if same else True)              # 違う方：前の担当の方との約束も守る（7-1 (21)）
                self.assertEqual(self.plan(s)[:2], ("call", s["contact"]))       # 前の番号で1回
                d = self.settle(s, self.call(s, ["dialing", "no_answer"]))["decision"]
                self.assertEqual((d.kind, d.target.contact_id, d.promised, self.handover_now(s)),
                                 ("call", new if same else s["contact"], True, False))

    def test_a_cut_call_resumed_after_a_fix_goes_to_another_person_without_the_apology(self):
        """お客様と話している途中で切れた（続きの電話・お詫びの印 cut_off）あと、「担当の方は違う」で番号を直す ⇒ 新しい番号の別の担当の
        方へは、途中で切れたお詫びをせず、引き継ぎを確かめるふつうの電話（基準は切れた電話の時刻＝`_carried_since`）。同じ相手なら、
        続きの電話のまま（お詫びの印も）。"""
        for same in (True, False):
            with self.subTest(same=same):
                s = self.ready()
                self.cut(s)
                new = self.fixed_as(s, same)
                p = self.plan(s)
                if same:
                    self.assertEqual((p[0], p[1], p[2], p[6], p[7], p[8]), ("call", new, "resume", ["cut_off"], True, False))
                else:
                    self.assertEqual(p, self.handed_over(new))

    def test_what_is_carried_is_read_from_the_earlier_periods(self):
        """前の期で行に引き継ぐことがあるか（`next_action._carried` の1か所）は、前の担当の方の期の通話と予定のすべてで見る：切れた電話の続きで
        受付の方と話せても（目的のお客様は不在）、「違う」で直すと、新しい担当の方へは引き継ぎを確かめる形。お呼びする方が替わる前は、引き継ぐ
        ことは無い（期が1つだけ）。⛔ 0202 まで：いちばん新しい「話した通話」の時刻と、直した時刻を比べていた（`_carried_since`。0203 で期に替えた）。"""
        s = self.ready()
        cut = self.cut(s)
        later = self.call(s, report=rep(("reception",), not_reached="absent"))  # 続きの電話：受付の方と話せたが、目的のお客様は不在
        self.settle(s, later)
        self.assertEqual(self.tx(s, lambda tx: next_action.current(tx, s["task"]))["cut_call"], cut)
        carried = lambda: self.tx(s, lambda tx: next_action._carried(tx, s["task"], next_action.person_now(tx, s["task"])))   # noqa: E731
        self.assertFalse(carried())
        new = self.fixed_as(s, False)
        self.assertTrue(carried())
        self.assertEqual(self.plan(s), self.handed_over(new))

    # ── 担当の期（0203。第三者の見直しの指摘1〜8）
    def test_a_refusal_by_the_new_person_on_the_old_number_they_named_stops_every_number(self):
        """指摘1（取り返しのつかない害）：新しい担当の方（鈴木様）が直通 N で話し、前の番号 O（代表番号）を指定した ⇒ O への約束は、いまの担当の
        方の期の予定（O が前の番号でも、前の担当の方への通話ではない＝salescall.former_person_call は期で見る）。O で鈴木様が「もうかけないで」と
        断れば、行の番号すべてを止め、行を終える（N へまたかけない）。直す前に O で受付と話していても、選び待ちにしない（契約者への知らせの無い
        止まりを作らない）。⛔ 0202：O への通話を前の担当の方への通話とみなし、断った鈴木様の N へまたかけた。"""
        for before in ("talked", "no_answer"):
            with self.subTest(before=before):
                s = self.ready()
                self.put_client(s, "gate.same_contact_min_interval", 1)
                self.put_client(s, "gate.max_dials_per_contact_per_day", 20)
                if before == "talked":
                    self.settle(s, self.call(s, report=rep(("reception",), not_reached="absent")))
                else:
                    self.settle(s, self.call(s, ["dialing", "no_answer"]))
                new = self.fixed_as(s, False)
                self.at(s, new, promised=False)
                time.sleep(1.1)
                self.settle(s, self.call(s, report=rep(callback=callback("other_number", None, s["phone"], time="すぐ"))))
                self.assertEqual(self.plan(s)[:4], ("call", s["contact"], "normal", True))
                self.assertIsNone(self.tx(s, lambda tx: tasks.old_promise_waiting(tx, s["task"])))
                c = self.claim(s)
                self.assertIsInstance(c, gate.Claim, c)
                self.tx(s, lambda tx: attempts.release(tx, [c.attempt_id]))
                time.sleep(1.1)
                aid = self.call(s, report=rep(dnc_=True))                       # O で、鈴木様が「もうかけないで」
                self.assertFalse(self.tx(s, lambda tx: tasks.former_person_call(tx, aid)))
                got = self.settle(s, aid)
                self.assertEqual((got["decision"].kind, got["decision"].end_reason), ("end", "do_not_call"))
                self.assertEqual([self.stopped(s, p) for p in (s["phone"], self.phone_of(s, new))], [True, True])
                self.assertIsNone(self.tx(s, lambda tx: results.latest(tx, s["task"]))["detail"].get("former_person"))

    def test_the_laters_of_the_old_person_are_not_counted_for_the_new_person(self):
        """指摘7：前の担当の方（田中様）の「あとで」は、新しい担当の方（鈴木様）の「あとで」の回数に入れない（`next_action._laters` が数えるのは、
        いまの担当の期の通話だけ）。田中様が3回「あとで」と言ったあと、契約者が新しい担当の方を選び、鈴木様が初めて「あとで」と言っても、行を
        終えない（時刻の決まっていない約束のまま）。⛔ 0202：4回目と数え、方針の回数より多いとして行を終えた。"""
        from salescall.core import row_fixes
        s = self.ready()
        self.put_client(s, "gate.same_contact_min_interval", 1)
        self.put_client(s, "gate.max_dials_per_contact_per_day", 20)
        self.promise_window(s)
        new = self.fixed_as(s, False, True)
        for i in range(3):                                                      # 田中様が「あとで」を3回（契約者は2回「守る」）
            self.at(s, s["contact"])
            time.sleep(1.1)
            self.settle(s, self.call(s, report=rep(callback=callback("later", time="あとで"))))
            self.tx(s, lambda tx: row_fixes.choose_old_promise(tx, s["task"], keep_old_promise=i < 2), OPERATOR)
        self.assertEqual(self.plan(s), self.handed_over(new))
        self.assertEqual(self.tx(s, lambda tx: next_action._laters(tx, s["task"])), [])
        self.at(s, new, promised=False)
        time.sleep(1.1)
        d = self.settle(s, self.call(s, report=rep(callback=callback("later", time="あとで"))))["decision"]   # 鈴木様が初めて「あとで」
        self.assertEqual((d.kind, d.target.contact_id, d.promised), ("call", new, True))
        self.assertEqual(len(self.tx(s, lambda tx: next_action._laters(tx, s["task"]))), 1)

    def test_a_refusal_on_another_row_stops_only_the_old_persons_numbers_of_this_row(self):
        """指摘8：同じ依頼者のほかの行（R2）で、この行（R1）の前の担当の方の番号 O から断りが来た ⇒ R1 で止めるのは、R1 の中で O から直接断られた
        ときと同じ（計画書 7-1 (22)：前の担当の方の番号だけ。決めるのは core/dnc._row_stop の1か所）。鈴木様の番号 N は止めず、R1 の門は通る。
        O が R1 で新しい担当の方の番号（鈴木様の通話で指定された番号）なら、R1 の番号すべてを止める（原則14）。⛔ 0202：R1 の番号すべてを止めた。"""
        for named in (False, True):
            with self.subTest(named_by_new_person=named):
                s = self.ready()
                self.put_client(s, "gate.same_contact_min_interval", 1)
                self.put_client(s, "gate.max_dials_per_contact_per_day", 20)
                new = self.fixed_as(s, False)                                   # R1：「違う」で鈴木様の番号 N へ
                if named:                                                       # 鈴木様が N で、前の番号 O を指定した
                    self.at(s, new, promised=False)
                    time.sleep(1.1)
                    self.settle(s, self.call(s, report=rep(callback=callback("other_number", None, s["phone"], time="すぐ"))))
                run2 = self.w.run(s["tenant"], s["client"], s["business"], s["version"])
                r2 = dict(s, run=run2, task=self.w.task(s["tenant"], s["client"], run2, s["contact"]))   # R2：O を持つ、ほかの一覧の行
                self.tx(r2, lambda tx: next_action.schedule_now(tx, r2["task"], cause="試験"))
                self.settle(r2, self.call(r2, report=rep(dnc_=True)))           # R2 で O から「もうかけないで」
                self.assertEqual((self.stopped(s, s["phone"]), self.stopped(s, self.phone_of(s, new))), (True, named))

    def test_a_settled_call_keeps_its_person_when_the_person_changes_again(self):
        """前の担当の方への通話かは、その通話を締めたときの期で見る（salescall.former_person_call・salescall.settled_at。見直し H の (B3)）：
        新しい担当の方への通話を締めたあとに、もう一度「違う」で直しても、その通話は前の担当の方への通話にならない（締め直しの行の終わり方・
        止め直しの範囲が、あとから変わらない）。まだ締めていない通話は、いまの期で見る。"""
        s = self.ready()
        self.put_client(s, "gate.same_contact_min_interval", 1)
        new = self.fixed_as(s, False)
        self.at(s, new, promised=False)
        time.sleep(1.1)
        aid = self.call(s, report=rep(dnc_=True))                               # 鈴木様が断った（いまの担当の方への通話）
        self.settle(s, aid)
        self.fixed_as(s, False)                                                 # 締めたあとに、もう一度「違う」で直す
        self.assertFalse(self.tx(s, lambda tx: tasks.former_person_call(tx, aid)))
        self.assertTrue(self.ends_row(s, aid))

    def test_a_number_fixed_as_the_new_persons_stays_theirs_after_a_later_same_person_fix(self):
        """前の担当の方が言った番号 X へ「違う」で直し、そのあと「同じ」で別の番号 N2 へ直しても、X は新しい担当の方の番号（期の始まりのあとの
        直しの番号＝salescall.task_current_person_number）。前の担当の方の番号は、取り込んだ番号だけ。"""
        from salescall.core import row_fixes
        said = fictional_jp_fixed()
        s = self.ready()
        self.settle(s, self.call(s, report=rep(("reception",), callback=callback("other_number", None, said, time="あとで"))))
        self.tx(s, lambda tx: row_fixes_fix(tx, s["task"], said, None), OPERATOR)
        self.fixed_as(s, True)
        self.assertEqual(self.tx(s, lambda tx: tasks.former_numbers(tx, s["task"])), [s["phone"]])

    def test_a_new_promise_on_a_call_dialed_before_the_fix_waits_for_the_choice(self):
        """直す前に予約した通話で、前の担当の方が新しいかけ直しの約束をし、その通話を締める前に契約者が「違う」で直した ⇒ その約束は前の担当の方の
        期の予定（予定の期の元は、その通話＝next_action._plan_person の after）で、契約者の選びを待つ（計画書 7-1 (23)）。"""
        s = self.ready()
        aid = self.call(s, report=rep(callback=callback("later", time="あとで")))   # 直す前の通話（まだ締めていない）
        self.fixed_as(s, False)
        self.settle(s, aid)
        self.assertEqual(self.plan(s)[:4], ("call", s["contact"], "normal", True))
        self.assertEqual(self.tx(s, lambda tx: tasks.old_promise_waiting(tx, s["task"])), aid)

    def test_going_back_to_the_old_person_who_made_a_new_promise_ends_the_wait(self):
        """「約束も守る」で前の番号へかけ、前の担当の方（田中様）が新しい約束をした ⇒ 契約者の選び待ち。選ぶ前に田中様に戻す（計画書 7-1 (30)）⇒ その約束は
        田中様ご本人の約束としてまた生きる（いまの予定が、いまの期の約束になる）ので、選び待ちにしない（選び待ちは、いまの予定が前の担当の方の期の約束の
        ときだけ＝salescall.task_old_promise_call。設計書 3-4-1【0213】の (a)）。"""
        from salescall.core import row_fixes
        s = self.ready()
        self.put_client(s, "gate.same_contact_min_interval", 1)
        self.put_client(s, "gate.max_dials_per_contact_per_day", 20)
        self.promise_window(s)
        self.fixed_as(s, False, True)
        aid = self.call(s, report=rep(callback=callback("later", time="あとで")))   # 田中様の新しい約束
        self.settle(s, aid)
        self.assertEqual(self.tx(s, lambda tx: tasks.old_promise_waiting(tx, s["task"])), aid)
        first = self.phone_of(s, s["contact"])
        self.tx(s, lambda tx: row_fixes.fix_dial_number(tx, s["task"], first, same_person=False, back_to=row_fixes.FIRST_PERIOD), OPERATOR)
        self.assertEqual(self.plan(s)[:4], ("call", s["contact"], "normal", True))
        self.assertIsNone(self.tx(s, lambda tx: next_action.current(tx, s["task"]))["person_since"])
        self.assertIsNone(self.tx(s, lambda tx: tasks.old_promise_waiting(tx, s["task"])))

    def test_a_row_the_old_person_ended_with_their_own_answer_comes_back_for_the_new_person(self):
        """✅ 2026-11-07 ユーザーの新しい決定：直す前に前の担当の方（田中様）が用件を断って（その方個人の答え）終えた行を、あとで「違う」で直したら、
        新しい担当の方（鈴木様）へ引き継ぎを確かめる電話（行を戻す。直したあとに断ったとき＝計画書 7-1 (24) とそろえる。戻すかは
        next_action.reopens_on_fix が next_action.answer_still_ends の1か所で決める）。会社としての答え（目的を果たした）と「もうかけないで」で
        終えた行は戻さない。「同じ」で直しても戻さない。"""
        from tests.test_judge import goal_refused
        cases = (("declined", False, dict(report=goal_refused(rep())), True),
                 ("declined", True, dict(report=goal_refused(rep())), False),
                 ("completed", False, dict(items=[("Q1", "confirmed", None)]), False),
                 ("do_not_call", False, dict(report=rep(dnc_=True)), False))
        for state, same, call, back in cases:
            with self.subTest(state=state, same=same):
                s = self.ready()
                got = self.settle(s, self.call(s, **call))
                self.assertEqual((got["state"], got["decision"].kind), (state, "end"))
                new = self.fixed_as(s, same)
                p = self.plan(s)
                if back:
                    self.assertEqual(p, self.handed_over(new))
                else:
                    self.assertEqual((p[0], self.tx(s, lambda tx: next_action.current(tx, s["task"]))["end_reason"]), ("end", state))

    def test_a_number_the_old_person_names_after_the_fix_is_the_old_persons(self):
        """「約束も守る」で前の番号へかけ、前の担当の方が別の番号 X を言った ⇒ X は前の担当の方の番号（前の期の通話で言われた番号＝
        salescall.task_former_number）。断りの範囲・代わりの番号は、これを読む。"""
        said = fictional_jp_fixed()
        s = self.ready()
        self.promise_window(s)
        self.fixed_as(s, False, True)
        self.settle(s, self.call(s, report=rep(callback=callback("other_number", None, said, time="あとで"))))
        self.assertIn(normalize(said).e164, self.tx(s, lambda tx: tasks.former_numbers(tx, s["task"])))

    def test_a_name_fix_keeps_the_dialed_number_as_the_new_persons(self):
        """お呼びする方の部署を直した（番号は同じ）⇒ お呼びする方は替わる（期が替わる）が、いまかける番号は新しい担当の方の番号（前の担当の方の
        番号にしない＝salescall.task_current_person_number）。"""
        from salescall.core import row_fixes
        s = self.ready()
        self.settle(s, self.call(s, report=rep(("reception",), not_reached="absent")))
        self.tx(s, lambda tx: row_fixes.fix(tx, s["task"], "target_department", "総務部", same_person=False), OPERATOR)
        self.assertIsNotNone(self.tx(s, lambda tx: next_action.person_now(tx, s["task"])))
        self.assertEqual(self.tx(s, lambda tx: tasks.former_numbers(tx, s["task"])), [])

    def test_the_refusal_range_of_a_settled_call_does_not_change_with_a_later_fix(self):
        """前の担当の方の断りで止める範囲（dnc.refusal_reach）は、その通話を締めたときの立場で読む（tasks.former_numbers の as_of_call。見直し H の
        (B3)）：締めたあとに、もう一度「違う」で直しても、前の新しい担当の方の番号は、その断りの範囲に入らない（取り消しのあとの止め直しが
        広がらない）。"""
        s = self.ready()
        self.promise_window(s)
        first = self.fixed_as(s, False, True)
        aid = self.call(s, report=rep(dnc_=True))                               # 前の担当の方が断った
        self.settle(s, aid)
        second = self.fixed_as(s, False)                                        # もう一度「違う」で直す
        got, _ = self.tx(s, lambda tx: dnc.refusal_reach(tx, aid))
        self.assertIn(s["phone"], got)
        self.assertNotIn(self.phone_of(s, first), got)
        self.assertNotIn(self.phone_of(s, second), got)                         # 締めたあとに足した番号も入らない（0213 の task_former_number）

    def test_a_call_keeps_the_person_of_its_plan(self):
        """通話の担当の期（attempts.person_since）は、予約したときの予定の期を写す（門の1か所）。あとから変えられない（データベースの守り
        attempt_record_fixed）。"""
        s = self.ready()
        self.promise_window(s)
        self.fixed_as(s, False, True)                                           # 前の担当の方との約束を守る（予定は前の期のまま）
        c = self.claim(s)
        self.assertIsInstance(c, gate.Claim, c)
        row = self.tx(s, lambda tx: tx.one("SELECT a.person_since, n.person_since AS plan FROM salescall.attempts a"
                                           " JOIN salescall.current_next_actions n ON n.task_id = a.task_id WHERE a.attempt_id = %s",
                                           (c.attempt_id,)))
        self.assertEqual((row["person_since"], row["plan"]), (None, None))
        self.assertTrue(self.tx(s, lambda tx: tasks.former_person_call(tx, c.attempt_id)))
        with self.assertRaises(Rejected):
            self.tx(s, lambda tx: tx.execute("UPDATE salescall.attempts SET person_since = now() WHERE attempt_id = %s", (c.attempt_id,)))

    def test_a_promise_cut_on_the_old_number_resumes_to_another_person_without_the_promise(self):
        """「違う」で直したあと、まだかけていない約束の電話を前の番号へかけ、前のお客様と話している途中で切れた ⇒ 続きの電話は
        いまかける番号へ移る（`current_dial`）が、新しい番号の先は別の担当の方なので、約束・お詫びを引き継がず、引き継ぎを確かめる
        ふつうの電話（約束の始まりは直す前＝`_carried_since`）。"""
        s = self.ready()
        self.promise_window(s)
        new = self.fixed_as(s, False, True)                                     # 前の担当の方との約束も守る（7-1 (21)）
        d = self.settle(s, self.call(s, self.CUT, side="line", report=rep(cut=Cut("callee", False, False))))["decision"]
        self.assertEqual(self.plan(s), self.handed_over(new))
        # 締めの処理へ返す決定も、新しい担当の方への形（前の番号から移る1か所 `_moved_on_fix` が `_onto_dial` を通す）
        self.assertEqual((d.kind, d.target.contact_id, d.target.purpose, d.promised, d.window), ("call", new, "normal", False, None))

    def test_a_promise_call_ringing_during_a_drop_fix_returns_the_handover_form(self):
        """前の番号への約束の電話が鳴っているあいだに「違う」・「次の担当の方にだけ」で直し、その電話に出なかった ⇒ 前の番号から新しい番号へ移る1か所
        （`_moved_on_fix` の `_onto_dial`）が、締めの処理へ返す決定も、新しい担当の方への形（約束・幅の無い、ふつうの電話）にする（予定も同じ形）。0214 で、
        「守る」約束の電話がこの道を通らなくなったので、見張る場面を足した。"""
        s = self.ready()
        self.promise_window(s)
        aid = self.call(s, ["dialing", "ringing"])                              # 前の番号へかけている途中
        new = self.fixed_as(s, False, False)                                    # 次の担当の方にだけ
        self.tx(s, lambda tx: attempts.move(tx, aid, "no_answer"))
        d = self.settle(s, aid)["decision"]
        self.assertEqual((d.kind, d.target.contact_id, d.target.purpose, d.promised, d.window), ("call", new, "normal", False, None))
        self.assertEqual(self.plan(s), self.handed_over(new))

    # ── 前の担当の方との約束が残っているときの選び（✅ 2026-11-07 ユーザー＝計画書 7-1 (21)：「前の担当者とのかけ直しの約束があったが、
    #    次の担当にだけかけるのかを契約者に問うて、選択してもらう。」。0201）
    def fixes(self, s):
        return self.tx(s, lambda tx: tx.all("SELECT same_person, keep_old_promise FROM salescall.task_dial_fixes WHERE task_id = %s"
                                            " ORDER BY seq", (s["task"],)))

    def direct_fix(self, s, same, keep):
        """契約者の取引が、直しの記録へ直接書く（データベースの守りの確かめ）。"""
        self.tx(s, lambda tx: tx.execute(
            "INSERT INTO salescall.task_dial_fixes (tenant_id, client_id, task_id, contact_id, recorded_by, same_person, keep_old_promise)"
            " SELECT t.tenant_id, t.client_id, t.task_id, %s, salescall.cur_actor(), %s, %s FROM salescall.tasks t WHERE t.task_id = %s",
            (self.w.contact(s["tenant"], s["client"], normalize(fictional_jp_fixed()).e164), same, keep, s["task"])), OPERATOR)

    def test_another_person_with_an_untried_promise_must_choose_whether_to_keep_it(self):
        """「担当の方は違う」で、約束の電話が残っていれば、約束も守るか・次の担当の方にだけかけるかを必ず選ぶ（選ばなければ直せない＝何も
        書かない）。選びが要らないとき（同じ方・約束が無い）は、選びを渡せない。データベースも同じ1か所（salescall.task_old_promise_choice_needed
        ＝salescall.task_promise_alive）で守る。"""
        from salescall.core import row_fixes
        s = self.ready()
        self.promise_window(s)
        self.assertTrue(self.tx(s, lambda tx: next_action.promise_alive(tx, s["task"])))
        with self.assertRaises(row_fixes.FixInvalid) as e:
            self.fixed_as(s, False)
        self.assertIn("選んでください", str(e.exception))
        with self.assertRaises(row_fixes.FixInvalid):
            self.fixed_as(s, True, True)                                        # 同じ方には選びが無い
        self.assertEqual(self.fixes(s), [])                                     # 何も書いていない
        for same, keep in ((False, None), (True, False)):
            with self.subTest(same=same, keep=keep), self.assertRaises(Rejected) as e:
                self.direct_fix(s, same, keep)
            self.assertEqual(e.exception.constraint, "task_dial_fix_old_promise_choice")
        # 約束でない予定には、選びが無い
        s = self.ready()
        self.at(s, s["contact"], promised=False)
        self.assertFalse(self.tx(s, lambda tx: next_action.promise_alive(tx, s["task"])))
        with self.assertRaises(row_fixes.FixInvalid):
            self.fixed_as(s, False, True)
        with self.assertRaises(Rejected) as e:
            self.direct_fix(s, False, False)
        self.assertEqual(e.exception.constraint, "task_dial_fix_old_promise_choice")
        self.fixed_as(s, False)
        self.assertEqual(self.fixes(s), [{"same_person": False, "keep_old_promise": None}])

    def test_a_promise_already_tried_once_still_asks_whether_to_keep_it(self):
        """✅ 2026-11-07 ユーザーの新しい決定：約束の電話を1回かけたあとでも、約束の日・幅がまだ残っていれば、「違う」で直すとき、前の担当の方との
        約束も守るかを選ぶ（例：「月〜金の15〜17時」、月曜に1回かけて出ず、火曜に「違う」で直す）。守る ⇒ 約束の電話は前の番号のまま、残りの回の
        数え方もいまの約束のまま（回・幅を変えない）。次の担当の方にだけ ⇒ 約束をやめ、新しい番号へ引き継ぎを確かめる電話。"""
        from salescall.core import row_fixes
        for keep in (True, False):
            with self.subTest(keep=keep):
                s = self.ready()
                self.promise_window(s)
                self.settle(s, self.call(s, ["dialing", "no_answer"]))           # 約束の電話を前の番号で1回かけた（出なかった）
                before = self.tx(s, lambda tx: next_action.current(tx, s["task"]))
                self.assertEqual((before["contact_id"], before["promised"]), (s["contact"], True))
                self.assertTrue(self.tx(s, lambda tx: next_action.promise_alive(tx, s["task"])))
                with self.assertRaises(row_fixes.FixInvalid):
                    self.fixed_as(s, False)                                     # 選ばなければ直せない
                new = self.fixed_as(s, False, keep)
                after = self.tx(s, lambda tx: next_action.current(tx, s["task"]))
                if keep:
                    self.assertEqual((after["seq"], after["contact_id"], after["promised"], after["window_round"]),
                                     (before["seq"], s["contact"], True, before["window_round"]))
                else:
                    self.assertEqual(self.plan(s), self.handed_over(new))

    def test_keeping_the_old_promise_calls_the_old_person_first_and_then_hands_over_without_a_sub(self):
        """「約束も守る」：約束の電話は、まず前の番号へ前の担当の方宛てに（7-1 (4)）。前の番号で前の担当の方と話せても、前の番号はサブに
        しない（担当の方が違う＝salescall.call_makes_sub の1か所。⛔ 見直し (1) の (b)：前はサブになり、新しい番号がつながらないと前の
        番号の前の担当の方へかけた）。前の担当の方が不在なら、約束の決まりいっぱいまで前の番号のまま（✅ 2026-11-08 ユーザー＝計画書 7-1 (32)。⛔ 0213：
        1回で新しい番号の担当の方へ引き継いだ）。"""
        s = self.ready()
        self.promise_window(s)
        self.fixed_as(s, False, True)
        self.assertEqual(self.fixes(s), [{"same_person": False, "keep_old_promise": True}])
        p = self.plan(s)
        self.assertEqual((p[0], p[1], p[3], p[5]), ("call", s["contact"], True, True))   # 約束のまま前の番号へ
        aid = self.call(s, report=rep(("reception",), not_reached="absent"))      # 前の番号で前の会社の受付の方と話せた
        d = self.settle(s, aid)["decision"]
        self.assertEqual((d.kind, d.target.contact_id, d.promised, self.handover_now(s)), ("call", s["contact"], True, False))
        self.assertFalse(self.tx(s, lambda tx: tx.one("SELECT salescall.call_makes_sub(%s) AS m", (aid,))["m"]))
        self.assertIsNone(self.tx(s, lambda tx: next_action.numbers_of(tx, s["task"])).sub)

    def test_calling_only_the_next_person_drops_the_promise_at_once(self):
        """「次の担当の方にだけかける」：約束をやめ、新しい番号へ、すぐかけてよい引き継ぎを確かめる電話（「違う」の形＝`_onto_dial`）。"""
        s = self.ready()
        self.promise_window(s)
        new = self.fixed_as(s, False, False)
        self.assertEqual(self.fixes(s), [{"same_person": False, "keep_old_promise": False}])
        self.assertEqual(self.plan(s), self.handed_over(new))
        self.assertFalse(self.tx(s, lambda tx: next_action.promise_alive(tx, s["task"])))

    def test_a_plain_plan_hands_over_to_another_person_only_after_talking_on_the_row(self):
        """約束もお詫びも無いふつうの予定：その行で前のお客様と話していれば、「違う」で直すと引き継ぎを確かめる印を付ける（元の時刻は、その行で
        話したいちばん新しい通話＝`_carried_since`。⛔ 見直し (1) の (c)：前は元の時刻が無く、印が付かなかった）。一度も話していなければ、
        確かめる引き継ぎが無いので印を付けない。"""
        for talked in (True, False):
            with self.subTest(talked=talked):
                s = self.ready()
                self.at(s, s["contact"], promised=False)
                if talked:
                    self.settle(s, self.call(s, report=rep(("reception",), not_reached="absent")))
                else:
                    self.settle(s, self.call(s, ["dialing", "no_answer"]))
                self.assertEqual(self.plan(s)[:4], ("call", s["contact"], "normal", False))
                new = self.fixed_as(s, False)
                p = self.plan(s)
                self.assertEqual((p[0], p[1], p[2], p[3], p[8]), ("call", new, "normal", False, talked))
                if talked:   # 新しい担当の方が出なかった：引き継ぎを確かめる印のまま、ふつうの決まりで（もう一度「替わった」の形にして、すぐかけ直さない＝0202）
                    self.put_client(s, "gate.same_contact_min_interval", 1)    # 試験：同じお客様の間隔の外にする（1秒）
                    time.sleep(1.1)
                    d = self.settle(s, self.call(s, ["dialing", "no_answer"]))["decision"]
                    self.assertEqual((d.kind, d.target.contact_id, self.handover_now(s)), ("call", new, True))
                    self.assertNotIn("お呼びする方が替わった", d.rule)

    # ── 前の担当の方の断り・新しい約束（✅ 2026-11-07 ユーザー＝計画書 7-1 (22)「鈴木様へはかける」・(23)「契約者に知らせて選んでもらう」。0202）
    def record_number(self, s):
        """契約者がダッシュボードで書いた記録だけの番号を行に足し、その番号を返す。"""
        n = normalize(fictional_jp_fixed())
        self.tx(s, lambda tx: tasks.add_record_number(tx, s["task"], n, "company", origin="dashboard"), OPERATOR)
        return n.e164

    def stable_ref(self, s):
        """行に管理番号を付け、依頼者の設定で「管理番号はお客様を表す変わらない番号」にする（断りで管理番号も止まる形）。"""
        self.put_client(s, "dnc.client_ref_is_stable_id", True)
        owner_access().in_system(SYSTEM, lambda tx: tx.execute("UPDATE salescall.tasks SET client_ref = %s WHERE task_id = %s",
                                                               (f"R-{s['task']}", s["task"])))

    def stopped(self, s, e164):
        return self.tx(s, lambda tx: dnc.lookup(tx, e164)) is not None

    def ref_stopped(self, s):
        return self.tx(s, lambda tx: tx.one("SELECT 1 FROM salescall.dnc_entries WHERE client_ref = %s AND revoked_at IS NULL",
                                            (f"R-{s['task']}",))) is not None

    def ends_row(self, s, aid):
        """締めの `_ends_row`（あとに発信のある行を、あとから決まった断りで終えるか）の答え。"""
        return self.tx(s, lambda tx: settle._ends_row(tx, settle._attempt(tx, aid), "do_not_call", False, False))

    def test_the_old_person_refusing_stops_only_the_old_persons_numbers_and_the_new_person_is_called(self):
        """「担当の方は違う」・「約束も守る」で直し、前の番号で前の担当の方が「もうかけないで」と断った ⇒ 止めるのは前の担当の方の番号（断った
        通話の番号・直す前から行にあった番号）だけ。新しい担当の方の番号（直した番号・そのあとに足した番号）と行の管理番号は止めず、行を終えず
        新しい担当の方へ引き継ぎを確かめる電話（計画書 7-1 (22)。範囲は dnc.refusal_reach、行の終わり方は next_action.refusal_ends_row の、
        どちらも tasks.former_person_call の1か所）。⛔ 0202 の前：行が断りで終わり、行の番号すべてと管理番号が止まって、新しい担当の方へは
        一度もかからなかった。"""
        s = self.ready()
        self.stable_ref(s)
        old_mobile = self.record_number(s)                                      # 直す前から行にある番号（前の担当の方の番号）
        self.promise_window(s)
        new = self.fixed_as(s, False, True)                                     # 違う方・前の担当の方との約束も守る（7-1 (21)）
        new_mobile = self.record_number(s)                                      # 直したあとに足した番号（新しい担当の方の番号）
        aid = self.call(s, report=rep(dnc_=True))                               # 前の番号で、前の担当の方が「もうかけないで」
        got = self.settle(s, aid)
        self.assertEqual(got["state"], "do_not_call")
        self.assertEqual(self.plan(s), self.handed_over(new))
        self.assertEqual([self.stopped(s, p) for p in (s["phone"], old_mobile, self.phone_of(s, new), new_mobile)],
                         [True, True, False, False])
        self.assertFalse(self.ref_stopped(s))
        self.assertTrue(self.tx(s, lambda tx: tasks.former_person_call(tx, aid)))
        self.assertEqual(self.tx(s, lambda tx: results.latest(tx, s["task"]))["detail"].get("former_person"), "personal")
        self.assertFalse(self.ends_row(s, aid))                                 # あとから決まっても、行を終えない（締めの `_ends_row`）
        # 新しい担当の方が出なかった：引き継ぎを確かめる印のまま、ふつうの決まりで（もう一度「替わった」の形にして、すぐかけ直さない）
        self.put_client(s, "gate.same_contact_min_interval", 1)                # 試験：同じお客様の間隔の外にする（1秒）
        time.sleep(1.1)
        d = self.settle(s, self.call(s, ["dialing", "no_answer"]))["decision"]
        self.assertEqual((d.kind, d.target.contact_id, self.handover_now(s)), ("call", new, True))
        self.assertNotIn("お呼びする方が替わった", d.rule)

    def phone_of(self, s, contact):
        return self.tx(s, lambda tx: tx.one("SELECT phone_e164 FROM salescall.contacts WHERE contact_id = %s", (contact,)))["phone_e164"]

    def test_every_other_refusal_still_stops_every_number_of_the_row_and_ends_it(self):
        """前の担当の方の断りの例外は、ほかの断りに漏れない（原則14）：同じ方で直したあとの前の番号での断り・「違う」で直したあとの新しい担当の
        方の断り・直していない行の断りは、いままでどおり行の番号すべてと管理番号を止め、行を終える。"""
        for case in ("same_person", "new_person", "no_fix"):
            with self.subTest(case=case):
                s = self.ready()
                self.stable_ref(s)
                self.record_number(s)
                self.promise_window(s)
                if case == "same_person":
                    self.fixed_as(s, True)                                      # 同じ方：約束の電話は前の番号で1回
                elif case == "new_person":
                    self.fixed_as(s, False, False)                              # 違う方・次の担当の方にだけ：新しい番号で、新しい担当の方が断る
                aid = self.call(s, report=rep(dnc_=True))
                got = self.settle(s, aid)
                self.assertEqual((got["state"], got["decision"].end_reason), ("do_not_call", "do_not_call"))
                numbers = self.tx(s, lambda tx: tasks.numbers(tx, s["task"]))
                self.assertEqual([self.stopped(s, p) for p in numbers], [True] * len(numbers))
                self.assertTrue(self.ref_stopped(s))
                self.assertTrue(self.ends_row(s, aid))
                self.assertFalse(self.tx(s, lambda tx: tasks.former_person_call(tx, aid)))

    def test_a_new_promise_from_the_old_person_waits_for_the_contractor_to_choose(self):
        """「約束も守る」で前の番号へかけ、前の担当の方が新しいかけ直しの約束をした ⇒ その約束は前の番号のまま書き、契約者に知らせ、選ぶまで門も
        候補も止める（計画書 7-1 (23)。tasks.old_promise_waiting の1か所）。守る ⇒ 前の番号へ新しい約束どおり（まだかけていない約束。そのあとも
        同じ決まり＝また約束があれば、また選ぶ）。新しい担当の方 ⇒ 約束をやめ、新しい番号へ引き継ぎを確かめる電話。選びは待っているときだけ、
        その通話に1回（データベースも守る）。⛔ 0202 の前：前の担当の方との新しい約束が、新しい番号の新しい担当の方へ引き継ぎの電話になった。"""
        from salescall.core import row_fixes
        choose = lambda s, keep: self.tx(s, lambda tx: row_fixes.choose_old_promise(tx, s["task"], keep_old_promise=keep), OPERATOR)   # noqa: E731
        waiting = lambda s: self.tx(s, lambda tx: tasks.old_promise_waiting(tx, s["task"]))   # noqa: E731
        s = self.ready()
        self.promise_window(s)
        new = self.fixed_as(s, False, True)
        aid = self.call(s, report=rep(callback=callback("later", time="あとで")))   # 前の担当の方が「あとで」（新しい約束）
        d = self.settle(s, aid)["decision"]
        self.assertEqual((d.kind, d.target.contact_id, d.promised, self.handover_now(s)), ("call", s["contact"], True, False))
        self.assertEqual(waiting(s), aid)
        self.assertTrue(self.tx(s, lambda tx: next_action.promise_alive(tx, s["task"])))   # まだかけていない約束（番号を直せば、また選ぶ）
        self.assertEqual(self.tx(s, lambda tx: [r["kind"] for r in tx.all(
            "SELECT kind FROM salescall.notification_events WHERE event_key = %s", (f"old_promise_choice:{aid}",))]), ["old_promise_choice"])
        self.at(s, s["contact"])                                                # 試験：約束の時刻が来たことにする
        self.assertEqual(self.reason(s), "old_promise_choice")
        self.assertNotIn(s["task"], [r["task_id"] for r in self.tx(s, lambda tx: scheduler.due_tasks(tx, 10))])
        choose(s, True)                                                         # 守る
        self.assertIsNone(waiting(s))
        self.assertEqual(self.plan(s)[:4], ("call", s["contact"], "normal", True))
        self.assertTrue(self.tx(s, lambda tx: next_action.promise_alive(tx, s["task"])))   # 守ると選んだ約束は、まだかけていない約束
        with self.assertRaises(row_fixes.FixInvalid):
            choose(s, False)                                                    # もう選んだ（待っていない）
        with self.assertRaises(Rejected) as e:                                  # データベースも守る：待っていない通話の選びは書けない
            self.tx(s, lambda tx: tx.execute(
                "INSERT INTO salescall.task_old_promise_choices (tenant_id, client_id, task_id, attempt_id, keep_old_promise, recorded_by)"
                " SELECT t.tenant_id, t.client_id, t.task_id, %s, false, salescall.cur_actor() FROM salescall.tasks t WHERE t.task_id = %s",
                (aid, s["task"])), OPERATOR)
        self.assertEqual(e.exception.constraint, "task_old_promise_choice_waiting")
        # 守った約束の電話で、前の担当の方がまた約束した ⇒ また選ぶ。こんどは新しい担当の方へ
        again = self.call(s, report=rep(callback=callback("later", time="あとで")))
        self.settle(s, again)
        self.assertEqual(waiting(s), again)
        choose(s, False)
        self.assertIsNone(waiting(s))
        self.assertEqual(self.plan(s), self.handed_over(new))
        self.assertEqual(self.tx(s, lambda tx: tx.all("SELECT attempt_id, keep_old_promise FROM salescall.task_old_promise_choices"
                                                      " WHERE task_id = %s ORDER BY seq", (s["task"],))),
                         [{"attempt_id": aid, "keep_old_promise": True}, {"attempt_id": again, "keep_old_promise": False}])
        s = self.ready()                                                        # 前の担当の方への電話の前は、選べない
        self.promise_window(s)
        self.fixed_as(s, False, True)
        with self.assertRaises(row_fixes.FixInvalid):
            choose(s, True)

    def test_a_promise_on_the_number_that_becomes_the_new_persons_number_is_not_carried_over(self):
        """前の担当の方が言った番号への約束の予定があり、契約者がその番号へ「担当の方は違う」で直した ⇒ 予定はもう直した番号にあるが、前の担当の方
        との約束は新しい担当の方へ引き継がない（引き継ぎを確かめるふつうの電話。`_onto_dial` の1か所）。その番号は新しい担当の方の番号（前の担当
        の方の番号は、取り込んだ番号だけ＝tasks.former_numbers）。⛔ 0202 の前：番号が「移った」かで決め、約束が新しい担当の方へ続いた。
        前の担当の方へかける道が無いので、この直しでは「約束も守る」を選べない（本体の決定。選びが要るかの1か所 salescall.task_old_promise_choice_needed
        ＝データベースの守りも同じ）：選びを渡せば `FixInvalid`（理由の文つき・何も書かない）。"""
        from salescall.core import row_fixes
        said = fictional_jp_fixed()
        s = self.ready()
        self.settle(s, self.call(s, report=rep(("reception",), callback=callback("other_number", None, said, time="あとで"))))
        p = self.plan(s)
        self.assertEqual((p[0], p[2], p[3], self.phone_of(s, p[1])), ("call", "normal", True, normalize(said).e164))
        self.assertTrue(self.tx(s, lambda tx: next_action.promise_alive(tx, s["task"])))
        for keep in (True, False):
            with self.subTest(keep=keep), self.assertRaises(row_fixes.FixInvalid) as e:
                self.tx(s, lambda tx: row_fixes_fix(tx, s["task"], said, keep), OPERATOR)
            self.assertIn("「約束も守る」は選べません", str(e.exception))
        with self.assertRaises(Rejected) as e:                                  # データベースも守る
            self.tx(s, lambda tx: tx.execute(
                "INSERT INTO salescall.task_dial_fixes (tenant_id, client_id, task_id, contact_id, recorded_by, same_person, keep_old_promise)"
                " SELECT t.tenant_id, t.client_id, t.task_id, %s, salescall.cur_actor(), false, true FROM salescall.tasks t WHERE t.task_id = %s",
                (p[1], s["task"])), OPERATOR)
        self.assertEqual(e.exception.constraint, "task_dial_fix_old_promise_choice")
        self.assertEqual(self.fixes(s), [])
        new = self.tx(s, lambda tx: row_fixes_fix(tx, s["task"], said, None), OPERATOR)["contact_id"]
        self.assertEqual(new, p[1])
        self.assertEqual(self.plan(s), self.handed_over(new))
        self.assertEqual(self.tx(s, lambda tx: tasks.former_numbers(tx, s["task"])), [s["phone"]])

    def test_a_choice_made_during_the_call_to_the_old_person_is_not_the_choice_for_its_new_promise(self):
        """前の担当の方への通話の途中（まだ締めていない）に、契約者が「違う」で直して「守る」を選んだ ⇒ その選びは前の約束についての選び：締める
        までは選び待ちにせず（まだ締めていない通話は選びの元にしない＝salescall.task_old_promise_call）、締めて前の担当の方が新しい約束をしていれば、
        その新しい約束の選びを待つ（数えるのは、その通話を締めたあとの選びだけ＝salescall.task_promise_choice の1か所。見直し H の (B1)(B4)）。
        約束の電話が鳴ったあとでも、約束の日・幅が残っていれば選ぶ（✅ 2026-11-07 ユーザーの新しい決定）ので、入口から選べる。"""
        s = self.ready()
        self.promise_window(s)
        aid = self.call(s, report=rep(callback=callback("later", time="あとで")))   # 前の担当の方の新しい約束（まだ締めていない）
        self.fixed_as(s, False, True)                                           # 通話の途中で「違う」・「守る」
        self.assertIsNone(self.tx(s, lambda tx: tasks.old_promise_waiting(tx, s["task"])))
        self.settle(s, aid)
        self.assertEqual(self.plan(s)[:4], ("call", s["contact"], "normal", True))
        self.assertEqual(self.tx(s, lambda tx: tasks.old_promise_waiting(tx, s["task"])), aid)
        self.assertFalse(self.tx(s, lambda tx: next_action._old_promise_dropped(tx, s["task"])))

    def test_a_new_promise_from_the_old_person_is_untried_while_waiting_and_after_keeping_it(self):
        """前の担当の方の新しい約束は、選びを待っているあいだも、「守る」を選んだあとも、まだかけていない約束（salescall.task_promise_tried：数えるのは
        いまの約束に対する「守る」の選びのあとの電話だけ）。だから「同じ」で番号を直しても、約束の電話は前の番号のまま（まず前の番号で）。"""
        from salescall.core import row_fixes
        for keep in (False, True):
            with self.subTest(chosen=keep):
                s = self.ready()
                self.promise_window(s)
                self.fixed_as(s, False, True)
                aid = self.call(s, report=rep(callback=callback("later", time="あとで")))
                self.settle(s, aid)
                if keep:
                    self.tx(s, lambda tx: row_fixes.choose_old_promise(tx, s["task"], keep_old_promise=True), OPERATOR)
                self.assertFalse(self.tx(s, lambda tx: next_action._promise_tried(tx, s["task"])))
                self.fixed_as(s, True)                                          # 同じ方（新しい担当の方）の別の番号へ
                self.assertEqual(self.plan(s)[:4], ("call", s["contact"], "normal", True))

    def test_a_kept_promise_not_answered_on_a_number_the_old_person_named_does_not_wait(self):
        """前の担当の方が言った番号 X への約束を「守る」で残し、X へかけて出なかった ⇒ 約束の電話は X のまま（X は前の番号でない）だが、前の担当の方は
        新しい約束をしていないので、選び待ちにしない（選びの元は、お客様が出た通話だけ＝salescall.task_old_promise_call）。"""
        from salescall.core import row_fixes
        said = fictional_jp_fixed()
        s = self.ready()
        self.put_client(s, "gate.same_contact_min_interval", 1)
        self.put_client(s, "gate.max_dials_per_contact_per_day", 20)
        self.settle(s, self.call(s, report=rep(("reception",), callback=callback("other_number", None, said, time="あとで"))))
        x = self.plan(s)[1]
        self.tx(s, lambda tx: row_fixes.fix_dial_number(tx, s["task"], fictional_jp_fixed(), same_person=False, keep_old_promise=True), OPERATOR)
        self.assertEqual(self.plan(s)[:4], ("call", x, "normal", True))
        self.at(s, x)
        time.sleep(1.1)
        self.settle(s, self.call(s, ["dialing", "no_answer"]))
        self.assertEqual(self.plan(s)[:2], ("call", x))
        self.assertIsNone(self.tx(s, lambda tx: tasks.old_promise_waiting(tx, s["task"])))

    def test_a_dropped_choice_for_an_earlier_promise_does_not_drop_a_later_promise(self):
        """「次の担当の方にだけ」で前の約束をやめたあと、新しい担当の方がかけ直しを約束し、契約者が「同じ」で番号を直した ⇒ 前の約束の選びは、いまの
        約束の選びではない（数えるのは、いまの約束の始まりからの選び＝salescall.task_promise_choice）。まだかけていない約束は、まず前の番号で。"""
        s = self.ready()
        self.put_client(s, "gate.same_contact_min_interval", 1)
        self.promise_window(s)
        first = self.fixed_as(s, False, False)                                  # 前の約束をやめた
        self.assertEqual(self.plan(s), self.handed_over(first))
        time.sleep(1.1)
        self.settle(s, self.call(s, report=rep(callback=callback("later", time="あとで"))))   # 新しい担当の方が「あとで」
        self.assertEqual(self.plan(s)[:4], ("call", first, "normal", True))
        self.assertFalse(self.tx(s, lambda tx: next_action._old_promise_dropped(tx, s["task"])))
        self.fixed_as(s, True)                                                  # 同じ方の別の番号へ
        self.assertEqual(self.plan(s)[:4], ("call", first, "normal", True))

    def test_a_new_promise_after_a_plain_plan_counts_from_its_own_start(self):
        """いまの約束の始まり（salescall.task_promise_since）は、約束でない予定で区切る：前の約束の電話をかけた（鳴った）あと、ふつうの予定をはさみ、
        また約束した ⇒ その約束は、まだかけていない約束（「同じ」で番号を直しても、まず前の番号で）。"""
        s = self.ready()
        self.put_client(s, "gate.same_contact_min_interval", 1)
        self.put_client(s, "gate.max_dials_per_contact_per_day", 20)
        self.promise_window(s)
        self.settle(s, self.call(s, ["dialing", "no_answer"]))                  # 前の約束の電話（鳴った）
        self.at(s, s["contact"], promised=False)                                # ふつうの予定
        time.sleep(1.1)
        self.settle(s, self.call(s, report=rep(callback=callback("later", time="あとで"))))   # また約束した
        self.assertEqual(self.plan(s)[:4], ("call", s["contact"], "normal", True))
        self.fixed(s)                                                           # 同じ方の番号へ
        self.assertEqual(self.plan(s)[:2], ("call", s["contact"]))

    def test_the_old_promise_choices_are_guarded(self):
        """新しい約束の選び（task_old_promise_choices）の守り：選べるのは管理者か契約者だけ・記録した方は、いま操作している方・時刻はデータベースが
        付ける・あとから変えられない・ほかの依頼者からは見えない（見直し H の (B4)）。"""
        s = self.ready()
        self.promise_window(s)
        self.fixed_as(s, False, True)
        aid = self.call(s, report=rep(callback=callback("later", time="あとで")))
        self.settle(s, aid)
        put = ("INSERT INTO salescall.task_old_promise_choices (tenant_id, client_id, task_id, attempt_id, keep_old_promise, recorded_by,"
               " recorded_at) SELECT t.tenant_id, t.client_id, t.task_id, %s, true, {who}, '2000-01-01' FROM salescall.tasks t WHERE t.task_id = %s")
        for actor, who, constraint in ((SYSTEM, "salescall.cur_actor()", "task_old_promise_choice_by_person"),
                                       (OPERATOR, "'ほかの方'", "task_old_promise_choice_recorded_by")):
            with self.subTest(constraint=constraint), self.assertRaises(Rejected) as e:
                self.tx(s, lambda tx: tx.execute(put.format(who=who), (aid, s["task"])), actor)
            self.assertEqual(e.exception.constraint, constraint)
        self.tx(s, lambda tx: tx.execute(put.format(who="salescall.cur_actor()"), (aid, s["task"])), OPERATOR)
        at = self.tx(s, lambda tx: tx.one("SELECT recorded_at > now() - interval '1 hour' AS fresh FROM salescall.task_old_promise_choices"
                                          " WHERE attempt_id = %s", (aid,)))
        self.assertTrue(at["fresh"])                                            # 渡した時刻（2000年）でなく、データベースの時刻
        for sql in ("UPDATE salescall.task_old_promise_choices SET keep_old_promise = false", "DELETE FROM salescall.task_old_promise_choices"):
            with self.subTest(sql=sql), self.assertRaises(Rejected):
                owner_access().in_client(OPERATOR, s["tenant"], s["client"], lambda tx: tx.execute(sql))
        other = self.ready()
        self.assertEqual(self.tx(other, lambda tx: tx.one("SELECT count(*) AS n FROM salescall.task_old_promise_choices")["n"]), 0)

    def test_the_old_person_stays_the_old_person_after_a_later_same_person_fix(self):
        """「違う」で直したあとに「同じ」（新しい担当の方の別の番号）で直しても、担当の方は替わったまま：基準は「違う」を選んだ、いちばん新しい
        直し（salescall.task_former_number の1か所。0202 の本体の指示 (1)。⛔ 前は、いちばん新しい直しだけを見て、前の担当の方の断りで行の番号
        すべてを止め、行を終えた）。"""
        s = self.ready()
        self.promise_window(s)
        first = self.fixed_as(s, False, True)                                    # 違う方・前の担当の方との約束も守る
        second = self.fixed_as(s, True)                                          # 新しい担当の方の別の番号（同じ方）
        self.assertEqual(self.plan(s)[:2], ("call", s["contact"]))              # 約束は、いまも前の番号の前の担当の方へ
        aid = self.call(s, report=rep(dnc_=True))
        self.settle(s, aid)
        self.assertEqual(self.plan(s), self.handed_over(second))
        self.assertEqual([self.stopped(s, p) for p in (s["phone"], self.phone_of(s, first), self.phone_of(s, second))], [True, False, False])

    def test_a_call_dialed_before_the_fix_and_settled_after_it_is_to_the_old_person(self):
        """直す前に予約してかけ、つながっているあいだに契約者が「担当の方は違う」で直した通話も、電話口は前の番号の前の担当の方：前の担当の方への
        通話（かけた時刻ではなく締めるときで見る＝salescall.former_person_call。0202 の本体の指示 (3)）。断れば、前の担当の方の番号だけを止め、
        新しい担当の方へ引き継ぎ。"""
        s = self.ready()
        aid = self.call(s, report=rep(dnc_=True))                               # 直す前の通話（まだ締めていない）
        new = self.fixed_as(s, False)
        self.settle(s, aid)
        self.assertEqual(self.plan(s), self.handed_over(new))
        self.assertEqual([self.stopped(s, p) for p in (s["phone"], self.phone_of(s, new))], [True, False])

    def test_the_old_persons_numbers_are_not_alternatives_for_the_new_person(self):
        """番号が使えないときの代わり（`usable`・`numbers_of` の said）に、「違う」を選んだ直しのあとは、前の担当の方が言った番号を入れない
        （新しい担当の方への代わりにしない。salescall.task_former_number の1か所。0202 の本体の指示 (2)）。直したあとに言われた番号は入る。"""
        s = self.ready()
        first = self.call(s, ["dialing", "no_answer"])
        self.settle(s, first)
        old_said = self.tx(s, lambda tx: tasks.add_callee_number(tx, s["task"], normalize(fictional_jp_fixed()), source_attempt_id=first))
        said = lambda: self.tx(s, lambda tx: next_action.numbers_of(tx, s["task"])).said   # noqa: E731
        self.assertEqual(said(), (old_said,))
        new = self.fixed_as(s, False)
        self.assertEqual(said(), ())
        self.put_client(s, "gate.same_contact_min_interval", 1)                # 試験：同じお客様の間隔の外にする（1秒）
        self.at(s, new, promised=False)                                         # 試験：新しい番号への予定の時刻が来た
        time.sleep(1.1)
        later = self.call(s, ["dialing", "no_answer"])                          # 新しい番号への電話
        self.settle(s, later)
        new_said = self.tx(s, lambda tx: tasks.add_callee_number(tx, s["task"], normalize(fictional_jp_fixed()), source_attempt_id=later))
        self.assertEqual(said(), (new_said,))
        self.assertNotEqual(new, new_said)

    def test_other_rows_wait_only_for_the_numbers_an_undecided_refusal_would_stop(self):
        """締めがまだの前の担当の方への通話は、断られたときに止まる範囲（前の担当の方の番号だけ＝dnc.refusal_reach）の行だけを待たせる（待たせる
        範囲 `undecided_reach` も同じ1か所。0202 の本体の指示 (4)。⛔ 前は行の範囲で待たせ、新しい担当の方の番号を持つほかの行も待った）。"""
        s = self.ready()
        self.promise_window(s)
        new = self.fixed_as(s, False, True)
        run2 = self.w.run(s["tenant"], s["client"], s["business"], s["version"])
        shares_new = self.w.task(s["tenant"], s["client"], run2, new)           # 新しい担当の方の番号を持つ、ほかの一覧の行
        shares_old = self.w.task(s["tenant"], s["client"], run2, s["contact"])   # 前の担当の方の番号を持つ、ほかの一覧の行
        self.call(s, report=rep(dnc_=True))                                     # 前の担当の方への通話（まだ締めていない）
        waits = lambda t: self.tx(s, lambda tx: dnc.undecided_reach(tx, t))   # noqa: E731
        self.assertEqual((waits(shares_old), waits(shares_new)), ("unsettled", None))

    def test_the_old_persons_own_answers_go_on_to_the_new_person_and_company_answers_end_the_row(self):
        """✅ 2026-11-07 ユーザー＝計画書 7-1 (24)：前の担当の方が用件を断った（「うちは要らない」。発信禁止ではない）ときも、新しい担当の方へは
        引き継ぎを確かめる電話。「対象外」「目的を果たした」など会社としての答えは行を終える（答えの分け方は next_action.FORMER_ANSWERS の1か所。
        結果の版の印 former_person も同じ1か所）。用件の断りでは、どの番号も発信禁止にしない。"""
        from tests.test_judge import goal_refused
        cases = (("declined", dict(report=goal_refused(rep())), "personal"),
                 ("completed", dict(items=[("Q1", "confirmed", None)]), "company"),
                 ("out_of_scope", dict(report=make_report(oos=True)), "company"))
        for state, call, answer in cases:
            with self.subTest(state=state):
                s = self.ready()
                self.promise_window(s)
                new = self.fixed_as(s, False, True)
                got = self.settle(s, self.call(s, **call))
                self.assertEqual(got["state"], state)
                self.assertEqual(self.tx(s, lambda tx: results.latest(tx, s["task"]))["detail"].get("former_person"), answer)
                if answer == "personal":
                    self.assertEqual(self.plan(s), self.handed_over(new))
                    self.assertEqual([self.stopped(s, p) for p in (s["phone"], self.phone_of(s, new))], [False, False])
                else:
                    self.assertEqual((got["decision"].kind, got["decision"].end_reason), ("end", state))

    def test_every_judged_state_has_an_answer_kind_for_the_old_person(self):
        """前の担当の方への通話の答えの分け方（next_action.FORMER_ANSWERS）は、判定の状態の名前をすべて当てはめる（results.BusinessState と同じ組）。
        番号違い・FAX は番号の答え、その方の側の事情で電話ではかけ直さない結果（business_rules.SYSTEM_ENDS）は、その方個人の答え。"""
        from typing import get_args
        self.assertEqual(set(next_action.FORMER_ANSWERS), set(get_args(results.BusinessState)))
        self.assertEqual({next_action.former_answer("out_of_scope", out_of_scope=o) for o in ("wrong_party", "fax_line")}, {"number"})
        self.assertEqual(next_action.former_answer("out_of_scope", out_of_scope="request"), "company")
        for key in business_rules.SYSTEM_ENDS:
            self.assertEqual(next_action.former_answer("not_reached", key=key), "personal")
        self.assertEqual(next_action.former_answer("not_reached", key="not_reached_absent"), "continue")

    # ── お呼びする方の名前などの直しでも、前の担当の方との約束を守るかを選ぶ（0206。計画書 7-1 (19)「名前・役職・部署を直したときは違う相手」・
    #    (21)(25)・0202 の本体の決定「約束の番号が新しい担当の方の番号なら『守る』は選べない」。選びの1か所＝row_fixes._old_promise_choice）
    def asked_same(self, s, field, value, same):
        """画面の流れ（0210。計画書 7-1 (28)）：1つ目の問い「いまの担当の方と同じ方か」を出す直し（row_fixes.same_person_choice_needed）なら、
        契約者が same を選ぶ（試験の初めの値は「違う」）。出さない直しなら選ばない（None）。"""
        from salescall.core import row_fixes
        return same if self.tx(s, lambda tx: row_fixes.same_person_choice_needed(tx, s["task"], field=field, value=value)) else None

    def name_fix(self, s, keep=None, field="target_department", value="総務部", same=False):
        from salescall.core import row_fixes
        same_person = self.asked_same(s, field, value, same)
        return self.tx(s, lambda tx: row_fixes.fix(tx, s["task"], field, value, same_person=same_person, keep_old_promise=keep), OPERATOR)

    def choice_needed(self, s, phone=None, same=False, **fix):
        """画面の入口の答え（番号を直す画面は phone・名前などを直す画面は field と value＝`fix` に渡すものと同じ。1つ目の問いを出す直しなら、
        契約者の選び same も渡す＝`asked_same`）。"""
        from salescall.core import row_fixes
        if "field" in fix and "same_person" not in fix:
            fix["same_person"] = self.asked_same(s, fix["field"], fix.get("value"), same)
        return self.tx(s, lambda tx: row_fixes.old_promise_choice_needed(tx, s["task"], phone, **fix))

    def direct_name_fix(self, s, keep, field="target_department", same=False):
        """契約者の取引が、行の欄の直しの記録へ直接書く（データベースの守りの確かめ）。担当の方は同じか・違うかは、値が本当に変わる直しなら same
        （データベースの salescall.person_fix_differs）。"""
        self.tx(s, lambda tx: tx.execute(
            "INSERT INTO salescall.task_fixes (tenant_id, client_id, task_id, field, value, recorded_by, same_person, keep_old_promise)"
            " SELECT t.tenant_id, t.client_id, t.task_id, %s, '総務部', salescall.cur_actor(),"
            "        CASE WHEN salescall.person_fix_differs(t.task_id, %s, '総務部', NULL) THEN %s END, %s"
            " FROM salescall.tasks t WHERE t.task_id = %s",
            (field, field, same, keep, s["task"])), OPERATOR)

    def promise_on_another_number(self, s):
        """前の担当の方（田中様）が別の番号（携帯 M）を言って約束した ⇒ 約束の幅のある予定が M に（いまかける番号は取り込んだ番号のまま）。M を返す。"""
        said = fictional_jp_fixed()
        self.put_client(s, "gate.same_contact_min_interval", 1)
        self.put_client(s, "gate.max_dials_per_contact_per_day", 20)
        self.settle(s, self.call(s, report=rep(callback=callback("other_number", None, said, time="あとで"))))
        m = self.plan(s)[1]
        self.assertEqual(self.phone_of(s, m), normalize(said).e164)
        now = self.tx(s, lambda tx: tx.one("SELECT now() AS t")["t"])
        self.at(s, m, window=next_action.Window(now + timedelta(hours=1), None, None, now - timedelta(hours=1), "week", round=2))
        return m

    def test_a_name_fix_with_a_promise_on_the_number_dialed_may_keep_it(self):
        """「守る」を選べるかは、その約束の番号で前の担当の方にお呼びできるか（データベースの salescall.task_promise_keepable の1か所＝本体の決定
        2026-11-08。0213）：名前などの直しでは番号は変わらないので、約束の番号が直す前からの会社の番号（いまかける番号）でも選べる（会話は、その通話の
        期のお名前でお呼びする＝0210）。選ばなければ直せない（データベースも同じ1か所で守る＝task_fix_guard）。守る ⇒ 約束は会社の番号のまま前の
        担当の方の期（その電話は前の担当の方への通話・会話は前の担当の方のお名前）。次の担当の方にだけ ⇒ 約束をやめ、引き継ぎを確かめる電話
        （next_action.after_person_fix＝番号の直しと同じ `_repoint`）。直す前に予約した通話は取り消す。
        ⛔ 0206〜0212：約束の番号がいまかける番号なら「守る」は選べず、約束をやめた（通話中の直しのあとの前の担当の方の新しい約束では、同じ番号でも
        選べた＝2つの道で答えが違った）。⛔ 0205 まで：名前などを直しても予定を見直さなかった。"""
        from salescall.core import row_fixes
        for keep in (True, False):
            with self.subTest(keep=keep):
                s = self.ready()
                self.put_client(s, "gate.same_contact_min_interval", 1)
                self.person_fix(s, "target_name", "田中", "たなか", same=True)
                self.promise_window(s)
                self.assertTrue(self.choice_needed(s, field="target_name", value="鈴木"))
                with self.assertRaises(row_fixes.FixInvalid) as e:
                    self.person_fix(s, "target_name", "鈴木", "すずき")
                self.assertIn("選んでください", str(e.exception))
                with self.assertRaises(Rejected) as e:
                    self.direct_name_fix(s, None)
                self.assertEqual(e.exception.constraint, "task_fix_old_promise_choice")
                claim = self.claim(s)                                          # 直す前に予約した通話
                self.assertIsInstance(claim, gate.Claim, claim)
                self.tx(s, lambda tx: attempts.release(tx, [claim.attempt_id]))
                claim = self.claim(s)
                self.assertIsInstance(claim, gate.Claim, claim)
                self.person_fix(s, "target_name", "鈴木", "すずき", keep=keep)
                self.assertEqual(self.tx(s, lambda tx: tx.one("SELECT state::text AS s FROM salescall.attempts WHERE attempt_id = %s",
                                                              (claim.attempt_id,)))["s"], "released")
                p = self.tx(s, lambda tx: next_action.current(tx, s["task"]))
                if keep:                                                       # 約束は会社の番号のまま、田中様の期
                    self.assertEqual((p["contact_id"], p["promised"], p["person_since"]), (s["contact"], True, None))
                    self.assertTrue(self.tx(s, lambda tx: next_action.promise_alive(tx, s["task"])))
                    self.assertEqual(self.period_name(s), "田中")
                    self.at(s, s["contact"])                                    # 試験：約束の時刻が来た
                    time.sleep(1.1)
                    aid = self.call(s, ["dialing", "no_answer"])
                    self.assertEqual(self.tx(s, lambda tx: tx.one("SELECT contact_id FROM salescall.attempts WHERE attempt_id = %s",
                                                                  (aid,)))["contact_id"], s["contact"])
                    self.assertTrue(self.tx(s, lambda tx: tasks.former_person_call(tx, aid)))   # 前の担当の方（田中様）への通話
                else:
                    self.assertEqual(self.plan(s), self.handed_over(s["contact"]))
                    self.assertFalse(self.tx(s, lambda tx: next_action.promise_alive(tx, s["task"])))
                    self.assertEqual(self.period_name(s), "鈴木")
        # 会社名の直しは、お呼びする方が替わる直しではない（選びを渡せない・予定も期も変えない）
        before = self.tx(s, lambda tx: next_action.current(tx, s["task"]))
        since = self.tx(s, lambda tx: next_action.person_now(tx, s["task"]))
        with self.assertRaises(row_fixes.FixInvalid):
            self.name_fix(s, keep=False, field="source_name", value="架空商事")
        self.name_fix(s, field="source_name", value="架空商事")
        self.assertEqual(self.tx(s, lambda tx: next_action.current(tx, s["task"]))["seq"], before["seq"])
        self.assertEqual(self.tx(s, lambda tx: next_action.person_now(tx, s["task"])), since)

    def test_a_dial_fix_onto_the_promise_number_still_cannot_keep_it_and_both_ways_agree(self):
        """「守る」を選べるかの1か所（salescall.task_promise_keepable。0213）は、番号の「違う」の直しで約束の番号が直す番号（新しい担当の方だけの番号）なら
        偽（いままでどおり。0202 の本体の決定）。通話中に名前などを「違う」で直したあとの、前の担当の方の新しい約束の選び（row_fixes.choose_old_promise）も
        同じ1か所を読む：その約束の番号が会社の番号なら「守る」を選べる（画面の入口 row_fixes.old_promise_keepable も真）。「守る」を選べない約束に「守る」を
        渡すと断る（入口もデータベースも＝task_old_promise_keep_guard）。"""
        from salescall.core import row_fixes
        s = self.ready()
        m = self.promise_on_another_number(s)                                  # 田中様が携帯 M を言って約束した
        self.assertFalse(self.choice_needed(s, self.phone_of(s, m)))           # M へ「違う」で直す：M は新しい担当の方だけの番号＝選べない
        self.assertTrue(self.choice_needed(s, fictional_jp_fixed()))           # ほかの番号へ「違う」で直す：M で田中様にお呼びできる＝選べる
        self.assertTrue(self.choice_needed(s, field="target_department", value="総務部"))   # 名前などの直し：選べる
        mp = self.phone_of(s, m)
        with self.assertRaises(row_fixes.FixInvalid) as e:
            self.tx(s, lambda tx: row_fixes_fix(tx, s["task"], mp, True), OPERATOR)
        self.assertIn("「約束も守る」は選べません", str(e.exception))
        self.tx(s, lambda tx: row_fixes_fix(tx, s["task"], mp, None), OPERATOR)
        self.assertEqual(self.plan(s), self.handed_over(m))
        # 通話中の名前の直しのあとの、前の担当の方の新しい約束（会社の番号）：「守る」を選べる（直すときの選びと同じ答え）
        s = self.ready()
        self.put_client(s, "gate.same_contact_min_interval", 1)
        self.person_fix(s, "target_name", "田中", "たなか", same=True)
        c = self.claim(s)
        self.assertTrue(dial_exit.dial(self.w.a, SYSTEM, s["tenant"], s["client"], c.attempt_id, FakeDriver()))
        for st in ("answered", "in_conversation"):
            self.tx(s, lambda tx: attempts.move(tx, c.attempt_id, st))
        self.person_fix(s, "target_name", "鈴木", "すずき", same=False)
        for st in ("ending", "ended"):
            self.tx(s, lambda tx: attempts.move(tx, c.attempt_id, st))
        self.tx(s, lambda tx: call_report.save(tx, c.attempt_id, rep(callback=callback("later", time="あとで"))))
        self.tx(s, lambda tx: attempts.record_verification(tx, c.attempt_id, summary=CLEAN))
        self.tx(s, lambda tx: health.after_call(tx, c.attempt_id))
        self.settle(s, c.attempt_id)
        self.assertEqual(self.tx(s, lambda tx: tasks.old_promise_waiting(tx, s["task"])), c.attempt_id)
        self.assertTrue(self.tx(s, lambda tx: row_fixes.old_promise_keepable(tx, s["task"])))
        # その約束の番号を、新しい担当の方の番号にした（試験：鈴木様の期に、その番号へ「同じ」で直した記録）⇒ 「守る」は選べない
        self.tx(s, lambda tx: tx.execute(
            "INSERT INTO salescall.task_dial_fixes (tenant_id, client_id, task_id, contact_id, recorded_by, same_person)"
            " SELECT t.tenant_id, t.client_id, t.task_id, %s, salescall.cur_actor(), true FROM salescall.tasks t WHERE t.task_id = %s",
            (s["contact"], s["task"])), OPERATOR)
        self.assertFalse(self.tx(s, lambda tx: row_fixes.old_promise_keepable(tx, s["task"])))
        with self.assertRaises(row_fixes.FixInvalid) as e:
            self.tx(s, lambda tx: row_fixes.choose_old_promise(tx, s["task"], keep_old_promise=True), OPERATOR)
        self.assertIn("「約束も守る」は選べません", str(e.exception))
        with self.assertRaises(Rejected) as e:
            self.tx(s, lambda tx: tx.execute(
                "INSERT INTO salescall.task_old_promise_choices (tenant_id, client_id, task_id, attempt_id, keep_old_promise, recorded_by)"
                " SELECT t.tenant_id, t.client_id, t.task_id, %s, true, salescall.cur_actor() FROM salescall.tasks t WHERE t.task_id = %s",
                (c.attempt_id, s["task"])), OPERATOR)
        self.assertEqual(e.exception.constraint, "task_old_promise_choice_keepable")
        self.tx(s, lambda tx: row_fixes.choose_old_promise(tx, s["task"], keep_old_promise=False), OPERATOR)
        self.assertEqual(self.plan(s), self.handed_over(s["contact"]))

    def test_a_name_fix_with_a_promise_on_another_number_asks_whether_to_keep_it(self):
        """前の担当の方との約束の番号が、いまかける番号でない（前の担当の方の携帯 M）⇒ 名前などを直すとき、約束も守るか・次の担当の方にだけかけるかを
        必ず選ぶ（番号の「違う」の直しと同じ1か所。約束の電話を1回かけたあとでも、約束の日・幅が残っていれば＝7-1 (25)）。選ばなければ直せない（何も
        書かない。データベースも守る）。画面の入口（row_fixes.old_promise_choice_needed）も同じ答え。守る ⇒ 約束は M のまま前の担当の方の期（その電話は
        前の担当の方への通話）。次の担当の方にだけ ⇒ 約束をやめ、いまかける番号へ引き継ぎを確かめる電話（1回かけた約束でも＝`_point`。⛔ 0205 まで：
        まだかけていない約束だけを見ていたので、1回かけた約束は選んでも M に残った）。選びは記録の見方 old_promise_choices に入る。"""
        from salescall.core import row_fixes
        for tried in (False, True):
            for keep in (True, False):
                with self.subTest(tried=tried, keep=keep):
                    s = self.ready()
                    m = self.promise_on_another_number(s)
                    if tried:                                                   # 約束の電話を M で1回かけた（出なかった）
                        time.sleep(1.1)
                        self.settle(s, self.call(s, ["dialing", "no_answer"]))
                        self.assertEqual(self.plan(s)[:4], ("call", m, "normal", True))
                    self.assertTrue(self.tx(s, lambda tx: next_action.promise_alive(tx, s["task"])))
                    self.assertTrue(self.choice_needed(s, field="target_department", value="総務部"))
                    self.assertTrue(self.choice_needed(s, fictional_jp_fixed()))   # 番号を「違う」で直す画面でも同じ
                    self.assertFalse(self.choice_needed(s, self.phone_of(s, m)))   # 約束の番号へ直すなら選べない
                    with self.assertRaises(row_fixes.FixInvalid):
                        self.choice_needed(s, "番号ではない")
                    with self.assertRaises(row_fixes.FixInvalid) as e:
                        self.name_fix(s)
                    self.assertIn("選んでください", str(e.exception))
                    with self.assertRaises(Rejected) as e:
                        self.direct_name_fix(s, None)
                    self.assertEqual(e.exception.constraint, "task_fix_old_promise_choice")
                    with self.assertRaises(Rejected) as e:                     # 会社名の直しには、選びが無い
                        self.direct_name_fix(s, keep, field="source_name")
                    self.assertEqual(e.exception.constraint, "task_fix_old_promise_choice")
                    self.assertEqual(self.tx(s, lambda tx: tx.one("SELECT count(*) AS n FROM salescall.task_fixes WHERE task_id = %s",
                                                                  (s["task"],)))["n"], 0)
                    self.name_fix(s, keep=keep)
                    self.assertEqual([r["keep_old_promise"] for r in self.tx(s, lambda tx: tx.all(
                        "SELECT keep_old_promise FROM salescall.old_promise_choices WHERE task_id = %s", (s["task"],)))], [keep])
                    p = self.tx(s, lambda tx: next_action.current(tx, s["task"]))
                    if keep:
                        self.assertEqual((p["contact_id"], p["promised"], p["person_since"]), (m, True, None))
                        self.at(s, m)                                           # 試験：約束の時刻が来た
                        time.sleep(1.1)
                        aid = self.call(s, ["dialing", "no_answer"])
                        self.assertEqual(self.tx(s, lambda tx: tx.one("SELECT contact_id FROM salescall.attempts WHERE attempt_id = %s",
                                                                      (aid,)))["contact_id"], m)
                        self.assertTrue(self.tx(s, lambda tx: tasks.former_person_call(tx, aid)))
                    else:
                        self.assertEqual(self.plan(s), self.handed_over(s["contact"]))
                        self.assertEqual(p["cause"].count("お呼びする方が替わった"), 1)   # 新しい担当の方への形は1回だけ

    def test_an_old_promise_is_not_written_into_the_new_persons_period_on_any_way(self):
        """前の担当の方の期の予定が、いまの担当の方の番号への予定になるときは、どの道でも新しい担当の方への形（next_action._onto_dial の1か所。いまかける
        番号でない、いまの担当の方の番号＝前に直した番号・番号が使えないときの代わり、でも）。決定を書く1か所（`_write`）が必ず通し、予定を書く1か所
        （`_append`）は、通っていない予定（前の担当の方の約束・お詫びのまま、いまの担当の方の期）を書かない（0206。⛔ 0205 まで：`_onto_dial` は
        いまかける番号だけを見ていて、番号が使えないときの代わりへ移る予定に、前の担当の方との約束の印が残りえた）。"""
        def kept(s):
            self.promise_window(s)
            new = self.fixed_as(s, False, True)                                # 守る：約束は前の番号のまま、前の担当の方の期
            second = self.fixed(s)                                              # 同じ方の別の番号へ：new は、いまかける番号でない新しい担当の方の番号
            self.assertEqual(self.plan(s)[:4], ("call", s["contact"], "normal", True))
            return new, second
        # 道が先に新しい担当の方への形にした予定は、決定を書く1か所で、もう一度その形にしない（守る約束を使い切って移る＝`_after_former`）
        s = self.ready()
        _, second = kept(s)
        now = self.tx(s, lambda tx: tx.one("SELECT now() AS t")["t"])
        self.at(s, s["contact"], window=next_action.Window(now + timedelta(seconds=30), None, None, now - timedelta(hours=1), "week", round=50))
        self.settle(s, self.call(s, ["dialing", "no_answer"]))
        self.assertEqual(self.plan(s), self.handed_over(second))
        self.assertEqual(self.tx(s, lambda tx: next_action.current(tx, s["task"]))["cause"].count("お呼びする方が替わった"), 1)
        s = self.ready()
        new, _ = kept(s)
        now = self.tx(s, lambda tx: tx.one("SELECT now() AS t")["t"])
        w = next_action.Window(now + timedelta(hours=1), None, None, now - timedelta(hours=1), "week", round=2)
        with self.assertRaises(ValueError) as e:
            self.tx(s, lambda tx: support.schedule_call(tx, s["task"], now, contact_id=new, cause="試験：約束のまま", promised=True,
                                                       window=w))
        self.assertIn("持ち込めません", str(e.exception))
        self.tx(s, lambda tx: next_action._write(tx, s["task"], next_action._call(now, True, "試験：約束のまま新しい担当の方の番号へ",
                                                                                  next_action.Target(new), window=w)))
        self.assertEqual(self.plan(s), self.handed_over(new))

    # ── 値が変わらない直しは、お呼びする方の替わりにしない・名前などの直しでも、前の担当の方個人の答えで終えた行を戻す（0208。計画書 7-1 (19)(26)。
    #    替わるかの1か所＝データベースの salescall.person_fix_changes・終えた行を戻すかの1か所＝next_action.reopens_on_fix）
    def person_fix(self, s, field, value, reading=None, keep=None, same=False):
        from salescall.core import row_fixes
        same_person = self.asked_same(s, field, value, same)
        return self.tx(s, lambda tx: row_fixes.fix(tx, s["task"], field, value, reading=reading, same_person=same_person,
                                                   keep_old_promise=keep), OPERATOR)

    def test_a_fix_that_keeps_the_value_does_not_change_the_person(self):
        """計画書 7-1 (19)「名前・役職・部署を直したときは違う相手」は、中身が変わったときのこと：同じ名前のまま読みだけを直す・同じ値を入れ直す直しは、
        お呼びする方の替わりにしない（担当の期も予定も変わらない・前の担当の方との約束の選びは要らない＝渡せば断る。データベースの守りも同じ1か所）。
        比べるのは、その直しの直前の値（前に直していれば、その直しの値。取り込んだ値ではない）。名前を変えれば、期が替わり、約束の選びが要る。
        ⛔ 0206 まで：直しの記録が1つ増えるだけで期が替わり、読みだけの直しでも約束の選びを求め、予定が新しい担当の方への形になった。"""
        from salescall.core import row_fixes
        s = self.ready()
        self.person_fix(s, "target_name", "田中", "たなか")                      # 最初のお名前（まだ話していない）
        self.person_fix(s, "target_department", "総務部")
        self.promise_on_another_number(s)                                       # 田中様が携帯 M を言って約束した
        since = self.tx(s, lambda tx: next_action.person_now(tx, s["task"]))
        seq = self.tx(s, lambda tx: next_action.current(tx, s["task"]))["seq"]
        self.assertTrue(self.choice_needed(s, field="target_name", value="鈴木"))   # 中身が変われば選ぶ
        for field, value, reading in (("target_name", "田中", "でんなか"), ("target_name", "田中", "たなか"), ("target_department", "総務部", None)):
            with self.subTest(field=field, reading=reading):
                self.assertFalse(self.choice_needed(s, field=field, value=value))   # 画面の入口も同じ答え（選びを出さない）
                with self.assertRaises(row_fixes.FixInvalid):
                    self.person_fix(s, field, value, reading, keep=True)
                self.person_fix(s, field, value, reading)
                self.assertEqual(self.tx(s, lambda tx: next_action.current(tx, s["task"]))["seq"], seq)
                self.assertEqual(self.tx(s, lambda tx: next_action.person_now(tx, s["task"])), since)
        self.assertEqual(self.tx(s, lambda tx: tx.one("SELECT target_name_reading AS r FROM salescall.tasks_now WHERE task_id = %s",
                                                      (s["task"],)))["r"], "たなか")   # 読みの直しは効いている
        with self.assertRaises(Rejected) as e:                                  # データベースも同じ1か所で守る
            self.direct_name_fix(s, True)
        self.assertEqual(e.exception.constraint, "task_fix_old_promise_choice")
        self.direct_name_fix(s, None)
        self.assertEqual(self.tx(s, lambda tx: next_action.person_now(tx, s["task"])), since)
        # 名前を変える ⇒ 期が替わる（約束の選びが要る）
        with self.assertRaises(row_fixes.FixInvalid):
            self.person_fix(s, "target_name", "鈴木", "すずき")
        self.person_fix(s, "target_name", "鈴木", "すずき", keep=False)
        changed = self.tx(s, lambda tx: next_action.person_now(tx, s["task"]))
        self.assertNotEqual(changed, since)
        self.assertEqual(changed, self.tx(s, lambda tx: tx.one(                # 期の始まりは、名前を変えた直しの時刻
            "SELECT recorded_at FROM salescall.task_fixes WHERE task_id = %s ORDER BY seq DESC LIMIT 1", (s["task"],)))["recorded_at"])
        self.assertEqual(self.plan(s), self.handed_over(s["contact"]))
        # 直したあとの値（鈴木）を入れ直しても替わらない（比べるのは、その直しの直前の値）
        seq = self.tx(s, lambda tx: next_action.current(tx, s["task"]))["seq"]
        self.person_fix(s, "target_name", "鈴木", "すずき")
        self.assertEqual(self.tx(s, lambda tx: next_action.current(tx, s["task"]))["seq"], seq)
        self.assertEqual(self.tx(s, lambda tx: next_action.person_now(tx, s["task"])), changed)

    def test_a_name_fix_brings_back_a_row_the_old_person_ended_with_their_own_answer(self):
        """計画書 7-1 (19)(26)：名前などの直しも「違う相手」なので、番号の「違う」の直しとそろえ、前の担当の方（田中様）個人の答え（在籍していない・
        用件の断り・ほかの手段）で終えた行を、新しい担当の方（鈴木様）へ引き継ぎを確かめる電話に戻す（戻すかは番号の直しと同じ1か所
        next_action.reopens_on_fix → answer_still_ends。戻す道も同じ `_restart`）。会社としての答え（目的を果たした・対象外）と「もうかけないで」で
        終えた行は戻さない。番号のせいで終えた行は、番号が変わらないので戻さない（計画書 7-1 (2) は番号の直しの決まり）。決めた回数で終えた行は
        戻す（新しい担当の方には、まだかけていない＝計画書 7-1 (8)。番号を「違う」で直したときと同じ＝見直し J-(C)。0210）。
        ⛔ 0206 まで：名前などの直しでは、終えた行を戻さなかった。⛔ 0209 まで：決めた回数で終えた行を戻さなかった。"""
        from salescall.core.call_report import Departed, Said
        from tests.test_judge import goal_refused
        departed = replace(rep(("reception",), not_reached="departed"),
                           departures=(Departed("end", ("ai",), False, Said(1, "田中は退職いたしました。")),))
        cases = (("declined", dict(report=goal_refused(rep())), True),
                 ("other_means", dict(report=rep(("reception",), callback=callback("other_means", None), not_reached="other_means")), True),
                 ("not_reached_departed", dict(report=departed), True),
                 ("completed", dict(items=[("Q1", "confirmed", None)]), False),
                 ("out_of_scope", dict(report=make_report(oos=True)), False),
                 ("do_not_call", dict(report=rep(dnc_=True)), False),
                 ("number_unusable", None, False),
                 ("max_attempts", None, True))   # 決めた回数で終えた：新しい担当の方には、まだかけていない（計画書 7-1 (8)。0210）
        for end, call, back in cases:
            with self.subTest(end=end):
                s = self.ready()
                if call is None:                                                # 番号のせい・決めた回数で終えた
                    self.settle(s, self.call(s, report=rep(("reception",), not_reached="absent")))
                    self.tx(s, lambda tx: next_action.end(tx, s["task"], end, cause="試験：番号のせい・決めた回数"))
                else:
                    got = self.settle(s, self.call(s, **call))
                    self.assertEqual((got["decision"].kind, got["decision"].end_reason), ("end", end))
                self.name_fix(s)
                p = self.plan(s)
                if back:
                    self.assertEqual(p, self.handed_over(s["contact"]))
                else:
                    self.assertEqual((p[0], self.tx(s, lambda tx: next_action.current(tx, s["task"]))["end_reason"]), ("end", end))

    # ── 表記の揺れだけの直しは、お呼びする方の替わりにしない（0209。比べる形の1か所＝データベースの salescall.compare_form。替わるかの1か所＝
    #    salescall.person_fix_changes。値の整え方と替わるかを Python で決めるのは row_fixes._person_fix の1か所＝fix と画面の入口が通る）
    def stored(self, s, field):
        return self.tx(s, lambda tx: tx.one(f"SELECT {field} AS v FROM salescall.tasks_now WHERE task_id = %s", (s["task"],)))["v"]

    def test_a_fix_that_only_changes_how_it_is_written_does_not_change_the_person(self):
        """全角・半角の空白・空白の有無・半角カナ・全角の数字だけが違う値を入れ直しても、同じお客様（担当の期も予定も変わらない・前の担当の方との
        約束の選びは要らない＝渡せば断る・画面の入口も選びを出さない。データベースの守りも同じ1か所）。値そのものは、契約者が入れたまま残る。
        本当に違う名前なら、期が替わり、約束の選びが要る。⛔ 0208 まで：文字そのもので比べたので、「田中　太郎」と「田中 太郎」が違う値になり、
        期が替わった（引き継ぎを確かめる電話・約束の選び＝お客様に影響する）。"""
        from salescall.core import row_fixes
        s = self.ready()
        self.person_fix(s, "target_name", "田中　太郎", "たなかたろう")           # 最初の値（まだ話していない）
        self.person_fix(s, "target_full_name", "ﾀﾅｶ ﾀﾛｳ", "たなかたろう")
        self.person_fix(s, "target_department", "営業部 第１課")
        self.promise_on_another_number(s)                                       # 田中様が携帯 M を言って約束した
        since = self.tx(s, lambda tx: next_action.person_now(tx, s["task"]))
        seq = self.tx(s, lambda tx: next_action.current(tx, s["task"]))["seq"]
        for field, value, reading in (("target_name", "田中 太郎", "たなかたろう"), ("target_name", "田中太郎", "たなかたろう"),
                                      ("target_name", " 田中　 太郎\t", "たなかたろう"), ("target_full_name", "タナカタロウ", "たなかたろう"),
                                      ("target_full_name", "ﾀﾅｶ　ﾀﾛｳ", "たなかたろう"), ("target_department", "営業部第1課", None),
                                      ("target_department", "営業部　第１課", None)):
            with self.subTest(field=field, value=value):
                self.assertFalse(self.choice_needed(s, field=field, value=value))
                with self.assertRaises(row_fixes.FixInvalid):
                    self.person_fix(s, field, value, reading, keep=True)
                self.person_fix(s, field, value, reading)
                self.assertEqual(self.stored(s, field), value.strip())         # 値は入れたまま（比べるときだけそろえる）
                self.assertEqual(self.tx(s, lambda tx: next_action.current(tx, s["task"]))["seq"], seq)
                self.assertEqual(self.tx(s, lambda tx: next_action.person_now(tx, s["task"])), since)
        with self.assertRaises(Rejected) as e:                                  # データベースも同じ1か所で守る
            self.tx(s, lambda tx: tx.execute(
                "INSERT INTO salescall.task_fixes (tenant_id, client_id, task_id, field, value, recorded_by, same_person, keep_old_promise)"
                " SELECT t.tenant_id, t.client_id, t.task_id, 'target_department', 'ｴｲｷﾞｮｳﾌﾞ', salescall.cur_actor(), false, NULL"
                " FROM salescall.tasks t WHERE t.task_id = %s", (s["task"],)), OPERATOR)
        self.assertEqual(e.exception.constraint, "task_fix_old_promise_choice")   # 本当に違う値には選びが要る（守りが比べている）
        with self.assertRaises(Rejected) as e:
            self.tx(s, lambda tx: tx.execute(
                "INSERT INTO salescall.task_fixes (tenant_id, client_id, task_id, field, value, recorded_by, keep_old_promise)"
                " SELECT t.tenant_id, t.client_id, t.task_id, 'target_department', '営業部　第1課', salescall.cur_actor(), true"
                " FROM salescall.tasks t WHERE t.task_id = %s", (s["task"],)), OPERATOR)
        self.assertEqual(e.exception.constraint, "task_fix_old_promise_choice")   # 表記の揺れだけには選びを渡せない
        # 本当に違う名前 ⇒ 期が替わる（約束の選びが要る。画面の入口も同じ答え）
        self.assertTrue(self.choice_needed(s, field="target_name", value="田中 次郎"))
        with self.assertRaises(row_fixes.FixInvalid):
            self.person_fix(s, "target_name", "田中 次郎", "たなかじろう")
        self.person_fix(s, "target_name", "田中 次郎", "たなかじろう", keep=False)
        self.assertNotEqual(self.tx(s, lambda tx: next_action.person_now(tx, s["task"])), since)
        self.assertEqual(self.plan(s), self.handed_over(s["contact"]))

    def test_a_fix_that_only_changes_how_it_is_written_does_not_bring_back_an_ended_row(self):
        """前の担当の方個人の答え（用件の断り）で終えた行は、表記の揺れだけの直しでは戻さない（お呼びする方は替わっていない＝同じお客様に、
        引き継ぎを確かめる電話をかけない）。本当に違う名前なら戻す（0208 の決まり）。"""
        from tests.test_judge import goal_refused
        s = self.ready()
        self.person_fix(s, "target_department", "営業部 第１課")               # 最初の値（まだ話していない）
        got = self.settle(s, self.call(s, report=goal_refused(rep())))
        self.assertEqual((got["decision"].kind, got["decision"].end_reason), ("end", "declined"))
        for value in ("営業部第1課", "営業部　第１課", "営業部 第1課"):
            with self.subTest(value=value):
                self.assertFalse(self.choice_needed(s, field="target_department", value=value))
                self.person_fix(s, "target_department", value)
                self.assertEqual((self.plan(s)[0], self.tx(s, lambda tx: next_action.current(tx, s["task"]))["end_reason"]),
                                 ("end", "declined"))
        self.person_fix(s, "target_department", "総務部")
        self.assertEqual(self.plan(s), self.handed_over(s["contact"]))

    def test_the_screen_entry_answers_as_the_fix_does(self):
        """画面の入口（row_fixes.old_promise_choice_needed）は、名前などを直す画面では欄と値を受け、直す入口 `fix` と同じ1か所
        （row_fixes._person_fix＝値の整え方と、替わるか）で答える：お呼びする方が替わらない直し（同じ値・表記の揺れだけ・会社名）は偽、
        替わる直し（違う値・欄を消す直し）は、約束が残っていて、その約束の番号で前の担当の方にお呼びできれば真（いまかける番号でも＝0213。`fix` は選ばなければ断る）。
        渡すのは、番号か、欄と値の、どちらか一方だけ。⛔ 0208 まで：欄も値も受けず、名前などの直しはいつも替わる直しとして答えた
        （値が変わらない直しでも真＝画面が選びを出すのに、`fix` は選びを断った）。"""
        from salescall.core import row_fixes
        s = self.ready()
        self.person_fix(s, "target_department", "総務部")
        self.promise_on_another_number(s)
        cases = (("target_department", "総務部", False), ("target_department", " 総務部\u3000", False), ("target_department", "ｿｳﾑﾌﾞ", True),
                 ("target_department", "経理部", True), ("target_department", None, True), ("target_department", "\u3000", True),
                 ("source_name", "架空商事", False))
        for field, value, needed in cases:
            with self.subTest(field=field, value=value):
                self.assertEqual(self.choice_needed(s, field=field, value=value), needed)
                if needed:                                                      # fix も、選ばなければ断る（何も書かない）
                    with self.assertRaises(row_fixes.FixInvalid) as e:
                        self.person_fix(s, field, value)
                    self.assertIn("選んでください", str(e.exception))
                else:                                                           # fix も、選びを渡せば断る
                    with self.assertRaises(row_fixes.FixInvalid):
                        self.person_fix(s, field, value, keep=False)
        self.assertEqual(self.stored(s, "target_department"), "総務部")
        for wrong in (dict(), dict(phone=fictional_jp_fixed(), field="target_department", value="経理部"), dict(value="経理部")):
            with self.subTest(wrong=wrong), self.assertRaises(TypeError):
                self.tx(s, lambda tx: row_fixes.old_promise_choice_needed(tx, s["task"], **wrong))
        with self.assertRaises(row_fixes.FixInvalid):                           # 直せない欄は、fix と同じに断る
            self.choice_needed(s, field="target_phone", value="x")
        # 約束の番号がいまかける番号（直す前からの会社の番号）でも、違う値なら選びが要る（その番号で前の担当の方にお呼びできる＝0213 の本体の決定）
        s = self.ready()
        self.promise_window(s)
        self.assertTrue(self.choice_needed(s, field="target_department", value="経理部"))


    # ── 名前などの直しでも「担当の方は同じか・違うか」を契約者が選ぶ・その通話の担当の期の値・締めた時刻（0210。✅ 2026-11-08 ユーザー＝
    #    計画書 7-1 (27)(28)。第三者の見直し J・データベースの監査 K）
    def raw_fix(self, s, field, value, same, keep=None):
        """契約者の取引が、行の欄の直しの記録へ直接書く（担当の方の選び same と約束の選び keep をそのまま。データベースの守りの確かめ）。"""
        self.tx(s, lambda tx: tx.execute(
            "INSERT INTO salescall.task_fixes (tenant_id, client_id, task_id, field, value, recorded_by, same_person, keep_old_promise)"
            " SELECT t.tenant_id, t.client_id, t.task_id, %s, %s, salescall.cur_actor(), %s, %s FROM salescall.tasks t WHERE t.task_id = %s",
            (field, value, same, keep, s["task"])), OPERATOR)

    def test_a_name_fix_asks_whether_it_is_the_same_person(self):
        """✅ 2026-11-08 ユーザー＝計画書 7-1 (28)：お呼びする方の名前・役職・部署を直すとき、同じ方の誤字の直しもあるので、番号の直しと同じく契約者が
        「担当の方は同じか・違うか」を選ぶ（row_fixes.fix の same_person・画面の入口 same_person_choice_needed）。値が本当に変わる直しは必ず選ぶ（選ばな
        ければ直せない＝何も書かない。データベースの守り task_fix_guard も同じ1か所 salescall.person_fix_differs）。「同じ」なら担当の期も予定も約束も
        そのまま（2つ目の選び＝前の担当の方との約束も守るか、は出さない）。「違う」のときだけ、お呼びする方が替わる（7-1 (19)）。値が変わらない
        直しは選ばない（渡せば断る）。⛔ 0209 まで：値が変われば、それだけで替わった（同じ方の誤字の直しでも、引き継ぎを確かめる電話・約束の選び）。"""
        from salescall.core import row_fixes
        s = self.ready()
        m = self.promise_on_another_number(s)                                  # 田中様が携帯 M を言って約束した
        since = self.tx(s, lambda tx: next_action.person_now(tx, s["task"]))
        seq = self.tx(s, lambda tx: next_action.current(tx, s["task"]))["seq"]
        self.assertTrue(self.tx(s, lambda tx: row_fixes.same_person_choice_needed(tx, s["task"], field="target_department", value="総務部")))
        with self.assertRaises(row_fixes.FixInvalid) as e:
            self.tx(s, lambda tx: row_fixes.fix(tx, s["task"], "target_department", "総務部"), OPERATOR)
        self.assertIn("同じ方か", str(e.exception))
        with self.assertRaises(row_fixes.FixInvalid):                         # 画面の入口も、選ばなければ答えない（fix と同じ1か所）
            self.choice_needed(s, field="target_department", value="総務部", same_person=None)
        with self.assertRaises(Rejected) as e:                                  # データベースも同じ1か所で守る
            self.raw_fix(s, "target_department", "総務部", None)
        self.assertEqual(e.exception.constraint, "task_fix_same_person_choice")
        # 「同じ」：期も予定も約束もそのまま。2つ目の選びは出さない（渡せば断る）
        self.assertFalse(self.choice_needed(s, field="target_department", value="総務部", same=True))
        with self.assertRaises(row_fixes.FixInvalid):
            self.tx(s, lambda tx: row_fixes.fix(tx, s["task"], "target_department", "総務部", same_person=True, keep_old_promise=True),
                    OPERATOR)
        self.tx(s, lambda tx: row_fixes.fix(tx, s["task"], "target_department", "総務部", same_person=True), OPERATOR)
        self.assertEqual(self.tx(s, lambda tx: next_action.person_now(tx, s["task"])), since)
        self.assertEqual(self.tx(s, lambda tx: next_action.current(tx, s["task"]))["seq"], seq)
        self.assertEqual(self.plan(s)[:4], ("call", m, "normal", True))
        # 値が変わらない直しは選ばない（渡せば断る＝入口もデータベースも）
        self.assertFalse(self.tx(s, lambda tx: row_fixes.same_person_choice_needed(tx, s["task"], field="target_department", value="総務部")))
        with self.assertRaises(row_fixes.FixInvalid):
            self.tx(s, lambda tx: row_fixes.fix(tx, s["task"], "target_department", "総務部", same_person=False), OPERATOR)
        with self.assertRaises(Rejected) as e:
            self.raw_fix(s, "target_department", "総務部", True)
        self.assertEqual(e.exception.constraint, "task_fix_same_person_choice")
        # 「違う」：期が替わり、2つ目の選びが要る
        self.assertTrue(self.choice_needed(s, field="target_department", value="経理部", same=False))
        self.tx(s, lambda tx: row_fixes.fix(tx, s["task"], "target_department", "経理部", same_person=False, keep_old_promise=False), OPERATOR)
        self.assertNotEqual(self.tx(s, lambda tx: next_action.person_now(tx, s["task"])), since)
        self.assertEqual(self.plan(s), self.handed_over(s["contact"]))
        self.assertEqual([(r["same_person"], r["keep_old_promise"]) for r in self.tx(s, lambda tx: tx.all(
            "SELECT same_person, keep_old_promise FROM salescall.task_fixes WHERE task_id = %s ORDER BY seq", (s["task"],)))],
            [(True, None), (False, False)])

    def test_filling_a_blank_also_asks_whether_it_is_the_same_person(self):
        """空欄を埋める直しも値が変わる直しなので、担当の方は同じか・違うかを選ぶ（データベースの監査 K の (a)。例：受付に「お名前だけではどの方か
        分からない」と言われて終えた行で、フルネーム・部署を埋める）。「同じ」なら期も予定もそのまま（同じお客様に、引き継ぎを確かめる電話を
        かけない）。「違う」なら、前の担当の方個人の答え（お名前だけではどの方か分からない）で終えた行は、新しい担当の方へ引き継ぎを確かめる電話に
        戻る。⛔ 0209 まで：空欄を埋めると自動で期が替わった。「同じ」で埋めたら、行を戻して、埋めたお名前・部署でお呼びする（✅ 2026-11-08 ユーザー＝
        計画書 7-1 (29)「埋めたらかけ直す」。0213。⛔ 0210〜0212：「同じ」で埋めても行は終わったままだった）。"""
        from salescall.core import row_fixes
        from salescall.core.call_report import Said, WhoUnclear
        for same in (True, False):
            with self.subTest(same=same):
                s = self.ready()
                report = replace(rep(("reception",), not_reached="who_unclear"),
                                 who_unclears=(WhoUnclear(Said(1, "お名前だけではどの者か分かりかねます。"), False),))
                self.settle(s, self.call(s, report=report))
                self.assertEqual(self.plan(s)[0], "end")
                for field, value in (("target_full_name", "田中 太郎"), ("target_department", "営業部")):
                    self.assertTrue(self.tx(s, lambda tx: row_fixes.same_person_choice_needed(tx, s["task"], field=field, value=value)))
                    self.tx(s, lambda tx: row_fixes.fix(tx, s["task"], field, value, reading="たなかたろう" if field == "target_full_name" else None,
                                                        same_person=same), OPERATOR)
                    if not same:
                        break
                if same:
                    self.assertIsNone(self.tx(s, lambda tx: next_action.person_now(tx, s["task"])))
                    self.assertEqual(self.plan(s), ("call", s["contact"], "normal", False, False, False, [], False, False))
                else:
                    self.assertIsNotNone(self.tx(s, lambda tx: next_action.person_now(tx, s["task"])))
                    self.assertEqual(self.plan(s), self.handed_over(s["contact"]))

    def test_naming_the_new_person_after_a_dial_fix_is_the_same_person(self):
        """番号を「違う」で直し（約束も守る）、そのあと名前を新しい担当の方の名前に直す：画面の問いは「いまの担当の方と同じ方か」なので「同じ」＝
        選びが2回出ない（2つ目の選びも無い）。期も予定もそのまま（前の担当の方との約束は前の番号のまま。その電話は前の担当の方への通話）。見直し
        J3 の伺い (D)。「違う」（3人目の方）を選べば、2つ目の選びが要り、「次の担当の方にだけ」なら約束をやめる（いまの約束に対する、いちばん新しい
        選び＝salescall.task_promise_choice が読まれる）。"""
        from salescall.core import row_fixes
        for second in ("same", "third_drop", "third_keep"):
            with self.subTest(second=second):
                s = self.ready()
                self.put_client(s, "gate.same_contact_min_interval", 1)
                self.person_fix(s, "target_name", "田中", "たなか", same=True)
                self.promise_window(s)
                new = self.fixed_as(s, False, True)                            # 番号を「違う」で直す・約束も守る
                since = self.tx(s, lambda tx: next_action.person_now(tx, s["task"]))
                if second == "same":
                    self.assertFalse(self.choice_needed(s, field="target_name", value="鈴木", same=True))
                    self.person_fix(s, "target_name", "鈴木", "すずき", same=True)
                    self.assertEqual(self.tx(s, lambda tx: next_action.person_now(tx, s["task"])), since)
                    self.assertEqual(self.plan(s)[:4], ("call", s["contact"], "normal", True))
                else:
                    keep = second == "third_keep"
                    self.assertTrue(self.choice_needed(s, field="target_name", value="佐藤", same=False))
                    self.person_fix(s, "target_name", "佐藤", "さとう", keep=keep, same=False)
                    self.assertNotEqual(self.tx(s, lambda tx: next_action.person_now(tx, s["task"])), since)
                    if keep:
                        self.assertEqual(self.plan(s)[:4], ("call", s["contact"], "normal", True))
                    else:                                                       # いちばん新しい選び（次の担当の方にだけ）が効く
                        self.assertEqual(self.plan(s), self.handed_over(new))
                        continue
                time.sleep(1.1)
                aid = self.call(s, ["dialing", "no_answer"])
                self.assertTrue(self.tx(s, lambda tx: tasks.former_person_call(tx, aid)))
                self.settle(s, aid)
                self.assertEqual(self.plan(s)[:4], ("call", s["contact"], "normal", True))   # 守る約束は、前の番号のまま（7-1 (32)）

    def test_a_name_fix_brings_back_rows_ended_by_counts_for_the_new_person(self):
        """名前などを「違う」で直したときも、番号を「違う」で直したときと同じく、決めた回数（日数）・「あとで」の回数で終えた行を戻す（新しい担当の方
        には、まだかけていない＝計画書 7-1 (8)「担当の相手が…違うのであれば、最初から」・(27)。戻すかは reopens_on_fix の1か所）。前の担当の方と
        話していれば、引き継ぎを確かめる電話。「同じ」で直せば戻さない（番号のせいで終えた行も、番号が変わらないので戻さない）。見直し J5・J-(C)。"""
        for end in ("callback_later_over", "callback_exact_unreached", "max_attempts", "callback_unspecified_time", "number_unusable"):
            for same in (False, True):
                with self.subTest(end=end, same=same):
                    s = self.ready()
                    self.settle(s, self.call(s, report=rep(callback=callback("later", time="あとで"))))   # 田中様の「あとで」
                    self.tx(s, lambda tx: next_action.end(tx, s["task"], end, cause="試験：田中様の行を終えた"))
                    self.name_fix(s, same=same)
                    if same or end == "number_unusable":
                        self.assertEqual(self.tx(s, lambda tx: next_action.current(tx, s["task"]))["end_reason"], end)
                    else:
                        self.assertEqual(self.plan(s), self.handed_over(s["contact"]))

    def test_a_new_promise_from_the_old_person_during_a_name_fix_waits_for_the_choice(self):
        """通話中（前の担当の方と話している途中）に、契約者が名前などを「違う」で直し、前の担当の方が新しいかけ直しの約束（「あとで」）をした ⇒
        その約束は前の担当の方の期の予定のまま書き（番号は、いまかける番号＝名前などの直しでは番号が変わらない）、契約者に知らせて選んでいただく
        （計画書 7-1 (23)。番号の直しのときと同じ1か所＝salescall.task_old_promise_waiting・知らせ old_promise_choice。前の担当の方の期に残すのは
        next_action._plan_person の1か所＝決定の印 former_promise）。守る ⇒ その番号へ前の担当の方宛てに（その通話の会話は前の担当の方の値を読む）・
        新しい担当の方 ⇒ 約束をやめ、引き継ぎを確かめる電話。⛔ 0209 まで：約束が、知らせも無く新しい担当の方への形になった（見直し J2）。"""
        from salescall.core import row_fixes
        for keep in (True, False):
            with self.subTest(keep=keep):
                s = self.ready()
                self.put_client(s, "gate.same_contact_min_interval", 1)
                self.person_fix(s, "target_name", "田中", "たなか", same=True)
                c = self.claim(s)
                self.assertTrue(dial_exit.dial(self.w.a, SYSTEM, s["tenant"], s["client"], c.attempt_id, FakeDriver()))
                for st in ("answered", "in_conversation"):
                    self.tx(s, lambda tx: attempts.move(tx, c.attempt_id, st))
                self.person_fix(s, "target_name", "鈴木", "すずき", same=False)   # 通話中に契約者が「違う」で直した
                self.assertEqual(self.period_name(s), "田中")                   # 通話中の会話は、その通話の期の値（田中様）
                for st in ("ending", "ended"):
                    self.tx(s, lambda tx: attempts.move(tx, c.attempt_id, st))
                self.tx(s, lambda tx: call_report.save(tx, c.attempt_id, rep(callback=callback("later", time="あとで"))))
                self.tx(s, lambda tx: attempts.record_verification(tx, c.attempt_id, summary=CLEAN))
                self.tx(s, lambda tx: health.after_call(tx, c.attempt_id))
                self.assertTrue(self.tx(s, lambda tx: tasks.former_person_call(tx, c.attempt_id)))
                self.assertEqual(self.settle(s, c.attempt_id)["state"], "callback_scheduled")
                p = self.tx(s, lambda tx: next_action.current(tx, s["task"]))
                self.assertEqual((p["contact_id"], p["promised"], p["person_since"]), (s["contact"], True, None))   # 前の担当の方の期
                self.assertEqual(self.tx(s, lambda tx: tasks.old_promise_waiting(tx, s["task"])), c.attempt_id)
                self.assertEqual(self.tx(s, lambda tx: tx.one("SELECT count(*) AS n FROM salescall.notification_events"
                                                              " WHERE kind = 'old_promise_choice'"))["n"], 1)
                self.assertEqual(self.period_name(s), "田中")                   # これからかける電話（前の担当の方への約束）の値
                self.tx(s, lambda tx: row_fixes.choose_old_promise(tx, s["task"], keep_old_promise=keep), OPERATOR)
                if keep:
                    self.assertEqual(self.plan(s)[:4], ("call", s["contact"], "normal", True))
                    self.assertEqual(self.period_name(s), "田中")
                else:
                    self.assertEqual(self.plan(s), self.handed_over(s["contact"]))
                    self.assertEqual(self.period_name(s), "鈴木")

    def period_name(self, s):
        """会話・門・録音の名前の部分が読む、その通話の担当の期のお呼びする方の名前（データベースの1か所 salescall.task_call_values。0210）。
        門の材料（gate._task）も同じ値を読む。"""
        got = self.tx(s, lambda tx: tx.one("SELECT target_name FROM salescall.task_call_values(%s)", (s["task"],)))["target_name"]
        self.assertEqual(self.tx(s, lambda tx: gate._task(tx, s["task"]))["target_name"], got)
        return got

    def test_the_call_reads_the_values_of_its_period(self):
        """会話が読むお呼びする方の値は、その通話の担当の期のもの（予約した通話の期・予約の前は予定の期の、その期の終わりの直前までに効いていた値＝
        salescall.task_call_values の1か所。会話の business_settings.target_of・門の材料・録音の名前の部分が読む）：名前を「違う」で直し、前の担当の方
        との約束を守ると、その約束の電話（予約・通話中）は前の担当の方のお名前・部署。そのあと新しい担当の方への電話は新しい値。期の中の「同じ」の
        直しは、その期の値に入る。⛔ 0209 まで：いまの値（新しい担当の方の名前）でお呼びした（見直し J-1。前の担当の方への約束・お詫び・番号の
        聞き直しの電話でも）。"""
        s = self.ready()
        self.put_client(s, "gate.same_contact_min_interval", 1)
        self.person_fix(s, "target_name", "田中", "たなか", same=True)
        self.person_fix(s, "target_department", "営業部", same=True)
        m = self.promise_on_another_number(s)                                  # 田中様が携帯 M を言って約束した
        self.person_fix(s, "target_department", "営業一部", same=True)          # 同じ方の部署の直し（田中様の期の値になる）
        self.person_fix(s, "target_name", "鈴木", "すずき", keep=True, same=False)   # 違う方・約束も守る
        self.person_fix(s, "target_department", "総務部", same=True)            # 鈴木様の部署（鈴木様の期の値）
        values = lambda: self.tx(s, lambda tx: tx.one("SELECT target_name, target_department FROM salescall.task_call_values(%s)",
                                                      (s["task"],)))
        self.assertEqual(dict(values()), {"target_name": "田中", "target_department": "営業一部"})
        self.assertEqual(self.period_name(s), "田中")
        self.assertEqual(self.stored(s, "target_name"), "鈴木")                # いまの値は鈴木様（ダッシュボード）
        self.at(s, m)
        time.sleep(1.1)
        c = self.claim(s)                                                       # 前の担当の方への約束の電話を予約した
        self.assertIsInstance(c, gate.Claim, c)
        self.assertEqual(dict(values()), {"target_name": "田中", "target_department": "営業一部"})
        self.tx(s, lambda tx: attempts.release(tx, [c.attempt_id]))
        from tests.test_judge import goal_refused
        self.settle(s, self.call(s, report=goal_refused(rep(), ("Q1",))))     # 田中様が用件を断った（その方個人の答え）⇒ 鈴木様へ引き継ぎ
        self.assertEqual(self.plan(s), self.handed_over(s["contact"]))
        self.assertEqual(dict(values()), {"target_name": "鈴木", "target_department": "総務部"})

    def test_a_result_held_while_the_person_changes_settles_after_the_change(self):
        """締めた時刻（salescall.settled_at）は、行の結果が決まった最初の版（判定できない・取り消せない決定の保留の版は入らない＝salescall.result_decided）：
        取り消せない決定（断り）の保留のあいだに「違う」で直し、そのあと契約者が承認して断りと決まると、その通話は前の担当の方への通話（計画書 7-1 (22)：止める
        のは前の担当の方の番号だけ・新しい担当の方へは引き継ぎを確かめる電話）。⛔ 0209 まで：判定待ちの版の時刻を締めた時刻にしたので、いまの担当の
        方の断りになり、新しい担当の方の番号まで止まった（見直し J-3。判定できない＝unknown の版も同じく入らない）。"""
        s = self.ready()
        self.stable_ref(s)
        old_mobile = self.record_number(s)
        self.put_client(s, "approval.human", True)                              # 検証モード：断りは契約者が承認するまで保留
        self.tx(s, lambda tx: approval.approve_task(tx, s["task"]), OPERATOR)
        aid = self.call(s, report=make_report(("target",), dnc=True, check="agreed"), items=[("Q1", "confirmed", None)])
        self.assertEqual(self.settle(s, aid)["decision"].kind, "hold")          # 取り消せない決定の保留（結果の版は decision_wait）
        self.assertIsNone(self.tx(s, lambda tx: tx.one("SELECT salescall.settled_at(%s) AS t", (aid,)))["t"])
        new = self.fixed_as(s, False)                                           # 保留のあいだに「違う」で直した
        seq = self.tx(s, lambda tx: tx.one("SELECT salescall.decision_wait_seq(%s) AS s", (aid,)))["s"]
        self.assertEqual(self.tx(s, lambda tx: approval.approve_decision(tx, aid, seq), OPERATOR)["state"], "do_not_call")
        self.assertTrue(self.tx(s, lambda tx: tasks.former_person_call(tx, aid)))
        self.assertEqual([self.stopped(s, p) for p in (s["phone"], old_mobile, self.phone_of(s, new))], [True, True, False])
        self.assertFalse(self.ref_stopped(s))
        self.assertEqual(self.plan(s), self.handed_over(new))
        # 判定できない（unknown）の版も、締めた時刻にしない
        u = self.ready()
        aid = self.call(u, report=rep(cut=None), items=(("Q1", "confirmed", None),))   # 切れ方の材料が無い＝判定できない
        self.assertEqual(self.settle(u, aid)["state"], "unknown")
        self.assertIsNone(self.tx(u, lambda tx: tx.one("SELECT salescall.settled_at(%s) AS t", (aid,)))["t"])

    def test_a_later_version_does_not_move_the_time_a_call_was_settled(self):
        """締めた時刻は、行の結果が決まった最初の版（データベースの監査 K の (b)：いちばん新しい版にしても落ちる試験が無かった）：締めたあとに「違う」で
        直し、その通話の結果の版が足されても（判定し直し・契約者の版）、その通話は前の担当の方への通話にならない。"""
        s = self.ready()
        aid = self.call(s, report=rep(("reception",), not_reached="absent"))
        self.settle(s, aid)
        first = self.tx(s, lambda tx: tx.one("SELECT salescall.settled_at(%s) AS t", (aid,)))["t"]
        self.fixed_as(s, False)
        self.tx(s, lambda tx: results.append(tx, s["task"], kind="human", business_state="not_reached", attempt_id=aid, judge_version=None,
                                             complaint=False, snapshot={}, decided_by="試験：あとから足した版",
                                             detail={"not_reached": "absent"}))
        self.assertEqual(self.tx(s, lambda tx: tx.one("SELECT salescall.settled_at(%s) AS t", (aid,)))["t"], first)
        self.assertFalse(self.tx(s, lambda tx: tasks.former_person_call(tx, aid)))

    def test_a_number_fixed_for_the_old_person_before_the_change_is_the_old_persons(self):
        """期の始まりより前の番号の直し（「同じ」で直した前の担当の方の番号）は、いまかける番号でなければ前の担当の方の番号（いまの担当の方の番号は、
        期の始まりのあとの直しの番号だけ＝salescall.task_current_person_number。データベースの監査 K の (b)：その時刻の絞りを外しても落ちる試験が
        無かった）。"""
        s = self.ready()
        n1 = self.fixed_as(s, True)                                             # 田中様の別の番号へ「同じ」で直した
        self.fixed_as(s, True)                                                  # 田中様の、もう1つの番号へ
        self.person_fix(s, "target_name", "鈴木", "すずき", same=False)          # そのあと、お呼びする方が替わった
        self.assertIn(self.phone_of(s, n1), self.tx(s, lambda tx: tasks.former_numbers(tx, s["task"])))

    def test_the_first_fix_compares_with_the_imported_value_of_each_field(self):
        """直しの直前の値（salescall.task_field_before）は、前に直していなければ、その欄の取り込んだ値（欄ごとに取り違えない。データベースの監査 K の
        (b)：取り込んだ部署・役職・フルネームを取り違えても落ちる試験が無かった）。直せる欄の組は、row_fixes.FIELDS・直しの記録の CHECK（0178）で
        同じ（K の (e)。直しの直前の値は欄の名前で読むので、3つ目の写しは持たない＝0210）。"""
        from salescall.core import row_fixes
        s = self.ready()
        values = {"target_name": "田中", "target_full_name": "田中 太郎", "target_role": "部長", "target_department": "営業部"}
        owner_access().in_system(SYSTEM, lambda tx: tx.execute(
            "UPDATE salescall.tasks SET target_name = %s, target_full_name = %s, target_role = %s, target_department = %s WHERE task_id = %s",
            (*values.values(), s["task"])))
        for field, value in values.items():
            with self.subTest(field=field):
                self.assertFalse(self.tx(s, lambda tx: row_fixes.same_person_choice_needed(tx, s["task"], field=field, value=value)))
                for other in values.values():
                    if other != value:
                        self.assertTrue(self.tx(s, lambda tx: row_fixes.same_person_choice_needed(tx, s["task"], field=field, value=other)))
        check = self.tx(s, lambda tx: tx.one("SELECT pg_get_constraintdef(c.oid) AS d FROM pg_constraint c"
                                             " WHERE c.conrelid = 'salescall.task_fixes'::regclass AND c.contype = 'c'"
                                             " AND pg_get_constraintdef(c.oid) LIKE '%%field%%source_name%%'"))["d"]
        self.assertEqual(sorted(re.findall(r"'(\w+)'", check)), sorted(row_fixes.FIELDS))

    def test_a_call_takes_the_period_of_its_plan_whatever_is_given(self):
        """通話の担当の期は、予約するとき、いまの予定の期をデータベースが入れる（attempt_before_insert の1か所。渡された値は使わない＝データベースの
        監査 K の (e)。⛔ 0209 まで：門の Python が予定から写していた）。"""
        s = self.ready()
        new = self.fixed_as(s, False)
        since = self.tx(s, lambda tx: next_action.person_now(tx, s["task"]))
        self.assertEqual(self.tx(s, lambda tx: next_action.current(tx, s["task"]))["person_since"], since)
        aid = uuid.uuid4()
        self.w.a.in_client(SYSTEM, s["tenant"], s["client"], lambda tx: tx.execute(
            "INSERT INTO salescall.attempts (tenant_id, client_id, attempt_id, task_id, contact_id, phone_e164, line_id,"
            " version_id, state, claimed_at, claim_expires_at, purpose, person_since)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'claimed', now(), now() + interval '1 minute', 'normal', '2000-01-01')",
            (s["tenant"], s["client"], aid, s["task"], new, self.phone_of(s, new), s["line"], s["version"])))
        self.assertEqual(self.tx(s, lambda tx: tx.one("SELECT person_since FROM salescall.attempts WHERE attempt_id = %s", (aid,)))["person_since"],
                         since)

    def test_periods_recorded_by_an_older_rule_are_put_back_by_the_current_rule(self):
        """記録してある予定・通話の期を、いまの決まりで置き直す（salescall.repoint_person_periods。0210 の移行が呼ぶ）：0203 の移行は、読みだけの直しの
        時刻も期の始まりとして埋めた（その時点の決まり）。いまの決まりで、その時刻に効いていた期（最初の担当の方）に置き直すと、ご本人の「もうかけないで」
        は、いまの担当の方の断りになり、行の番号すべてを止める。⛔ 0209 まで：置き直さず、前の担当の方の断りとして、ほかの番号が止まらなかった
        （見直し J-2）。"""
        s = self.ready()
        self.stable_ref(s)
        self.person_fix(s, "target_name", "田中", "たなか", same=True)
        mobile = self.record_number(s)
        self.person_fix(s, "target_name", "田中", "でんなか")                    # 読みだけの直し（お呼びする方は替わらない）
        reading_fix = self.tx(s, lambda tx: tx.one("SELECT max(recorded_at) AS t FROM salescall.task_fixes WHERE task_id = %s",
                                                   (s["task"],)))["t"]
        aid = self.call(s, report=rep(dnc_=True))                               # ご本人が「もうかけないで」（締める前）

        def old_rule(tx):   # 0203 の移行の埋め方（欄の直しの記録があれば替わりとみなした）を、この行に当てる
            tx.execute("ALTER TABLE salescall.attempts DISABLE TRIGGER USER")
            tx.execute("UPDATE salescall.attempts SET person_since = %s WHERE attempt_id = %s", (reading_fix, aid))
            tx.execute("ALTER TABLE salescall.attempts ENABLE TRIGGER USER")
        owner_access().in_system(SYSTEM, old_rule)
        self.assertTrue(self.tx(s, lambda tx: tasks.former_person_call(tx, aid)))   # 古い決まりの期のままなら、前の担当の方への通話
        owner_access().in_system(SYSTEM, lambda tx: tx.execute("SELECT salescall.repoint_person_periods()"))
        self.assertIsNone(self.tx(s, lambda tx: tx.one("SELECT person_since FROM salescall.attempts WHERE attempt_id = %s", (aid,)))["person_since"])
        self.assertFalse(self.tx(s, lambda tx: tasks.former_person_call(tx, aid)))
        self.assertEqual(self.settle(s, aid)["state"], "do_not_call")
        self.assertEqual([self.stopped(s, s["phone"]), self.stopped(s, mobile), self.ref_stopped(s)], [True, True, True])

    # ── 依頼者に埋めていただく欄を埋めたらかけ直す・前の担当の方に戻す（0213。✅ 2026-11-08 ユーザー＝計画書 7-1 (29)(30)）
    def who_unclear_end(self, s, recalled=False):
        from salescall.core.call_report import Said, WhoUnclear
        report = replace(rep(("reception",), not_reached="who_unclear"),
                         who_unclears=(WhoUnclear(Said(1, "お名前だけではどの者か分かりかねます。"), recalled),))
        got = self.settle(s, self.call(s, report=report))
        self.assertEqual((got["decision"].kind, got["decision"].end_reason), ("end", "not_reached_who_unclear"))

    def test_filling_the_blanks_after_who_unclear_calls_again_with_the_filled_name(self):
        """受付の方に「お名前だけではどの方か分かりかねます」と言われて終えた行（依頼者に埋めていただく欄＝business_settings.who_unclear_blanks）を、契約者が
        埋めたら（担当の方は「同じ」）：行を戻し、埋めたお名前・部署でお呼びする電話（✅ 2026-11-08 ユーザー＝計画書 7-1 (29)「埋めたらかけ直す」）。戻すかは
        直しで終えた行を戻す1か所（next_action.reopens_on_fix → answer_still_ends。締めたときに空いていた欄の、どれかが埋まった）。フルネームだけ・部署だけでも
        戻る。埋めずにほかの欄（お名前）を直しても戻らない。両方あってもまた分からないと言われた行（お呼びし直した）は、埋める欄が無いので戻らない。
        戻したあとも、担当の期は同じ（引き継ぎを確かめる印は付かない）。"""
        for fill in ("full_name", "department", "other", "recalled", "counted"):
            with self.subTest(fill=fill):
                s = self.ready()
                if fill == "counted":                                          # 決めた回数で終えた行は、「同じ」で埋めても戻らない
                    self.settle(s, self.call(s, report=rep(("reception",), not_reached="absent")))
                    self.tx(s, lambda tx: next_action.end(tx, s["task"], "max_attempts", cause="試験：決めた回数までかけた"))
                    self.person_fix(s, "target_full_name", "田中 太郎", "たなかたろう", same=True)
                    p = self.tx(s, lambda tx: next_action.current(tx, s["task"]))
                    self.assertEqual((p["kind"], p["end_reason"]), ("end", "max_attempts"))
                    continue
                if fill == "recalled":                                         # 両方あってもまた分からない（お呼びし直した）
                    self.person_fix(s, "target_full_name", "田中 太郎", "たなかたろう", same=True)
                    self.person_fix(s, "target_department", "営業部", same=True)
                self.who_unclear_end(s, recalled=fill == "recalled")
                if fill == "full_name":
                    self.person_fix(s, "target_full_name", "田中 太郎", "たなかたろう", same=True)
                elif fill == "department":
                    self.person_fix(s, "target_department", "営業部", same=True)
                elif fill == "other":
                    self.person_fix(s, "target_name", "田中", "たなか", same=True)
                else:
                    self.person_fix(s, "target_department", "営業一部", same=True)
                self.assertIsNone(self.tx(s, lambda tx: next_action.person_now(tx, s["task"])))
                p = self.tx(s, lambda tx: next_action.current(tx, s["task"]))
                if fill in ("full_name", "department"):
                    self.assertEqual(self.plan(s), ("call", s["contact"], "normal", False, False, False, [], False, False))
                    got = self.tx(s, lambda tx: tx.one("SELECT target_full_name, target_department FROM salescall.task_call_values(%s)",
                                                       (s["task"],)))
                    self.assertEqual(got[f"target_{fill}"], "田中 太郎" if fill == "full_name" else "営業部")   # 埋めたお名前・部署でお呼びする
                else:
                    self.assertEqual((p["kind"], p["end_reason"]), ("end", "not_reached_who_unclear"))

    def back_name(self, s, value, reading, back_to, keep=None):
        from salescall.core import row_fixes
        return self.tx(s, lambda tx: row_fixes.fix(tx, s["task"], "target_name", value, reading=reading, same_person=False,
                                                   keep_old_promise=keep, back_to=back_to), OPERATOR)

    def back_choices(self, s):
        from salescall.core import row_fixes
        return self.tx(s, lambda tx: row_fixes.back_choices(tx, s["task"]))

    def test_going_back_to_the_former_person_brings_back_their_answer(self):
        """担当を鈴木様に直したあと、「やはり田中様だった」と前の担当の方に戻す（✅ 2026-11-08 ユーザー＝計画書 7-1 (30)「前の方へ戻す選び」）：直す画面に
        「前の担当の方に戻す」を出し（画面の入口 row_fixes.back_choices＝その行の前の期と、その期のお名前）、戻すと、新しい期を作らず、田中様の期がまた
        いまの期になる（データベースの salescall.task_person_changes の1か所）。田中様が用件を断っていれば、その答えも戻るので行は終わり、田中様へはかけない
        （next_action._period_ends → answer_still_ends）。名前などの直しでも、番号の直しでも同じ。"""
        from salescall.core import row_fixes
        from tests.test_judge import goal_refused
        for way in ("name", "dial"):
            with self.subTest(way=way):
                s = self.ready()
                self.put_client(s, "gate.same_contact_min_interval", 1)
                self.person_fix(s, "target_name", "田中", "たなか", same=True)
                self.settle(s, self.call(s, report=rep(("reception",), not_reached="absent")))   # 田中様は不在だった（まだ続く）
                time.sleep(1.1)
                self.at(s, s["contact"], promised=False)
                got = self.settle(s, self.call(s, report=goal_refused(rep())))   # 次の電話で、田中様が用件を断った
                self.assertEqual((got["decision"].kind, got["decision"].end_reason), ("end", "declined"))
                self.assertEqual(self.back_choices(s), [])                     # 替わったことが無い行には、戻す期が無い
                if way == "name":
                    self.person_fix(s, "target_name", "鈴木", "すずき", same=False)
                    new = s["contact"]
                else:
                    new = self.fixed_as(s, False)
                self.assertEqual(self.plan(s), self.handed_over(new))          # 鈴木様へ引き継ぎ（計画書 7-1 (26)）
                choices = self.back_choices(s)
                self.assertEqual([(c["back_to"], c["since"], c["values"]["target_name"]) for c in choices],
                                 [(row_fixes.FIRST_PERIOD, None, "田中")])
                if way == "name":
                    self.back_name(s, "田中", "たなか", choices[0]["back_to"])
                else:
                    now_phone = self.phone_of(s, new)
                    with self.assertRaises(row_fixes.FixInvalid):              # いまかける番号と同じ番号へは戻せない（直しを書かない）
                        self.tx(s, lambda tx: row_fixes.fix_dial_number(tx, s["task"], now_phone, same_person=False,
                                                                        back_to=choices[0]["back_to"]), OPERATOR)
                    first = self.phone_of(s, s["contact"])
                    self.tx(s, lambda tx: row_fixes.fix_dial_number(tx, s["task"], first, same_person=False,
                                                                    back_to=choices[0]["back_to"]), OPERATOR)
                self.assertIsNone(self.tx(s, lambda tx: next_action.person_now(tx, s["task"])))   # 新しい期を作らず、田中様の期
                p = self.tx(s, lambda tx: next_action.current(tx, s["task"]))
                self.assertEqual((p["kind"], p["end_reason"], p["person_since"]), ("end", "declined", None))   # 田中様へはかけない
                self.assertNotIsInstance(self.claim(s), gate.Claim)            # 門も予約しない
                self.assertEqual([(c["since"] is None, c["values"]["target_name"]) for c in self.back_choices(s)],
                                 [(False, "鈴木" if way == "name" else "田中")])   # 戻せる期は、こんどは鈴木様の期

    def test_going_back_keeps_the_former_persons_promise_as_theirs(self):
        """田中様が「あとで」と約束していた行で、鈴木様に「違う」で直し、田中様に戻す（計画書 7-1 (30)。約束の選び 7-1 (21)〜(25) との重なり＝0213）：
        - 「約束も守る」を選んでいた ⇒ 約束は田中様の期のまま残っていて、戻すと田中様ご本人の約束としてまた生きる（もう前の担当の方への約束ではない＝
          戻す直しでは「守るか」の選びを出さない・渡せば断る。その電話は前の担当の方への通話でない）。
        - 「次の担当の方にだけ」を選んでいた ⇒ 約束は、そのときにやめた（契約者の選び）。戻すと、やめた約束も戻る（✅ 2026-11-08 ユーザー＝計画書 7-1 (31)
          「約束を戻す」。時刻の決まっていない「あとで」の約束は日・幅が無いので過ぎない＝next_action._period_promise。田中様とは話しているので、引き継ぎを
          確かめる印は付かない）。⛔ 0213：戻しても約束は戻らず、田中様へのふつうの電話だった。"""
        from salescall.core import row_fixes
        for keep in (True, False):
            with self.subTest(keep=keep):
                s = self.ready()
                self.put_client(s, "gate.same_contact_min_interval", 1)
                self.person_fix(s, "target_name", "田中", "たなか", same=True)
                self.settle(s, self.call(s, report=rep(callback=callback("later", time="あとで"))))
                self.assertTrue(self.tx(s, lambda tx: next_action.promise_alive(tx, s["task"])))
                self.person_fix(s, "target_name", "鈴木", "すずき", keep=keep, same=False)
                back = self.back_choices(s)[0]["back_to"]
                self.assertFalse(self.choice_needed(s, field="target_name", value="田中", same_person=False, back_to=back))
                if keep:
                    with self.assertRaises(row_fixes.FixInvalid):
                        self.back_name(s, "田中", "たなか", back, keep=True)
                    other = self.record_number(s)                              # 田中様の別の番号（記録だけ）へ戻す番号の直し
                    for table, cols, vals in (("task_fixes", "field, value", "'target_name', '田中'"),
                                              ("task_dial_fixes", "contact_id",
                                               f"(SELECT k.contact_id FROM salescall.task_numbers k WHERE k.task_id = t.task_id"
                                               f" AND k.phone_e164 = '{other}')")):   # データベースも同じ1か所で守る
                        with self.subTest(table=table), self.assertRaises(Rejected) as e:
                            self.tx(s, lambda tx: tx.execute(
                                f"INSERT INTO salescall.{table} (tenant_id, client_id, task_id, {cols}, recorded_by, same_person, keep_old_promise,"
                                f" back_to) SELECT t.tenant_id, t.client_id, t.task_id, {vals}, salescall.cur_actor(), false, true, '-infinity'"
                                " FROM salescall.tasks t WHERE t.task_id = %s", (s["task"],)), OPERATOR)
                        self.assertIn(e.exception.constraint, ("task_fix_old_promise_choice", "task_dial_fix_old_promise_choice"))
                self.back_name(s, "田中", "たなか", back)
                p = self.tx(s, lambda tx: next_action.current(tx, s["task"]))
                self.assertIsNone(self.tx(s, lambda tx: next_action.person_now(tx, s["task"])))
                if keep:
                    self.assertEqual((p["kind"], p["contact_id"], p["promised"], p["person_since"]), ("call", s["contact"], True, None))
                    self.assertTrue(self.tx(s, lambda tx: next_action.promise_alive(tx, s["task"])))
                    self.assertIsNone(self.tx(s, lambda tx: tasks.old_promise_waiting(tx, s["task"])))
                    self.at(s, s["contact"])
                    time.sleep(1.1)
                    aid = self.call(s, ["dialing", "no_answer"])
                    self.assertFalse(self.tx(s, lambda tx: tasks.former_person_call(tx, aid)))   # 田中様ご本人への約束の電話
                    self.settle(s, aid)
                    third = self.fixed_as(s, True)                             # 田中様の別の番号へ（同じ方）：もうかけた約束は新しい番号へ
                    p = self.tx(s, lambda tx: next_action.current(tx, s["task"]))   # （田中様ご本人の約束＝「守る」約束ではない。7-1 (4)）
                    self.assertEqual((p["contact_id"], p["promised"]), (third, True))
                else:                                                          # やめた「あとで」の約束が戻る
                    self.assertEqual(self.plan(s), ("call", s["contact"], "normal", True, False, False, [], False, False))
                    self.assertIsNone(p["person_since"])
                    self.assertTrue(self.tx(s, lambda tx: next_action.promise_alive(tx, s["task"])))
                self.assertEqual(self.period_name(s), "田中")

    def test_going_back_asks_about_the_promise_of_the_person_left(self):
        """田中様（話した）→ 鈴木様に「違う」で直し、鈴木様が「あとで」と約束 → 田中様に戻す：こんどは鈴木様が前の担当の方なので、鈴木様との約束も守るかを
        選ぶ（番号・名前の「違う」の直しと同じ1か所＝salescall.task_old_promise_choice_needed。計画書 7-1 (21)(30)。0213）。守る ⇒ 鈴木様の約束は鈴木様の期の
        まま（その電話は前の担当の方への通話・会話は鈴木様のお名前）。次の担当の方にだけ ⇒ 約束をやめ、田中様へのふつうの電話（最初の担当の方の期へ
        戻しても、前の期の約束をいまの期へ持ち込まない＝next_action._onto_dial・_carried。田中様とはもう話しているので、引き継ぎを確かめる印は付かない）。"""
        from salescall.core import row_fixes
        for keep in (True, False):
            with self.subTest(keep=keep):
                s = self.ready()
                self.put_client(s, "gate.same_contact_min_interval", 1)
                self.person_fix(s, "target_name", "田中", "たなか", same=True)
                self.settle(s, self.call(s, report=rep()))                     # 田中様と話した
                self.person_fix(s, "target_name", "鈴木", "すずき", same=False)
                time.sleep(1.1)
                self.at(s, s["contact"], promised=False)
                self.settle(s, self.call(s, report=rep(callback=callback("later", time="あとで"))))   # 鈴木様の約束
                p = self.tx(s, lambda tx: next_action.current(tx, s["task"]))
                self.assertEqual((p["promised"], p["person_since"]), (True, self.tx(s, lambda tx: next_action.person_now(tx, s["task"]))))
                first = row_fixes.FIRST_PERIOD
                self.assertTrue(self.choice_needed(s, field="target_name", value="田中", same_person=False, back_to=first))
                with self.assertRaises(row_fixes.FixInvalid) as e:
                    self.back_name(s, "田中", "たなか", first)
                self.assertIn("選んでください", str(e.exception))
                self.back_name(s, "田中", "たなか", first, keep=keep)
                p = self.tx(s, lambda tx: next_action.current(tx, s["task"]))
                if keep:
                    self.assertEqual((p["promised"], p["person_since"] is not None), (True, True))   # 鈴木様の期の約束のまま
                    self.assertEqual(self.period_name(s), "鈴木")
                else:
                    self.assertEqual(self.plan(s), ("call", s["contact"], "normal", False, False, False, [], False, False))
                    self.assertIsNone(p["person_since"])
                    self.assertEqual(self.period_name(s), "田中")

    def test_a_same_person_fix_keeps_the_plan_and_the_claim(self):
        """名前などを「同じ」で直しても（値は変わる）、まだ終えていない行の予定も、予約した通話もそのまま（「同じ」なら予定も約束もそのまま＝計画書 7-1 (28)。
        「同じ」の直しで予定の決め直しの1か所を通るのは、終えた行を戻すかを見るためだけ＝next_action._after_fix。0213）。"""
        s = self.ready()
        before = self.tx(s, lambda tx: next_action.current(tx, s["task"]))["seq"]
        claim = self.claim(s)
        self.assertIsInstance(claim, gate.Claim, claim)
        self.person_fix(s, "target_department", "営業部", same=True)
        self.assertEqual(self.tx(s, lambda tx: next_action.current(tx, s["task"]))["seq"], before)
        self.assertEqual(self.tx(s, lambda tx: tx.one("SELECT state::text AS s FROM salescall.attempts WHERE attempt_id = %s",
                                                      (claim.attempt_id,)))["s"], "claimed")

    def test_going_back_only_to_an_earlier_period_of_the_row(self):
        """戻せるのは、その行の前の期だけ（いまの期・まだ無い期には戻せない＝入口の `FixInvalid`・データベースの守り task_person_back_guard。どちらも
        salescall.task_back_ok の1か所）。「担当の方は同じ」の直しでは戻せない。最初の期でない期（鈴木様）へも戻せる（田中様→鈴木様→佐藤様→鈴木様）。
        戻せる期の一覧は、新しく終わった期の順。"""
        from salescall.core import row_fixes
        s = self.ready()
        self.person_fix(s, "target_name", "田中", "たなか", same=True)
        with self.assertRaises(row_fixes.FixInvalid):                         # 替わったことが無い：いまの期（最初の期）しか無い
            self.back_name(s, "鈴木", "すずき", row_fixes.FIRST_PERIOD)
        self.person_fix(s, "target_name", "鈴木", "すずき", same=False)
        suzuki = self.tx(s, lambda tx: next_action.person_now(tx, s["task"]))
        self.person_fix(s, "target_name", "佐藤", "さとう", same=False)
        sato = self.tx(s, lambda tx: next_action.person_now(tx, s["task"]))
        self.assertEqual([(c["since"], c["values"]["target_name"]) for c in self.back_choices(s)], [(suzuki, "鈴木"), (None, "田中")])
        now = self.tx(s, lambda tx: tx.one("SELECT now() AS t")["t"])
        for wrong in (sato.isoformat(), now.isoformat(), "鈴木様の期", (now.replace(tzinfo=None)).isoformat()):
            with self.subTest(wrong=wrong), self.assertRaises(row_fixes.FixInvalid):   # いまの期・まだ無い期・読めない鍵
                self.back_name(s, "鈴木", "すずき", wrong)
        with self.assertRaises(row_fixes.FixInvalid):                         # 「同じ」の直しでは戻せない
            self.tx(s, lambda tx: row_fixes.fix(tx, s["task"], "target_name", "鈴木", reading="すずき", same_person=True,
                                                back_to=suzuki.isoformat()), OPERATOR)
        for back in (sato, now):                                               # データベースも守る（入口を通らずに書いても）
            with self.subTest(back=back), self.assertRaises(Rejected) as e:
                self.tx(s, lambda tx: tx.execute(
                    "INSERT INTO salescall.task_fixes (tenant_id, client_id, task_id, field, value, recorded_by, same_person, back_to)"
                    " SELECT t.tenant_id, t.client_id, t.task_id, 'target_name', '鈴木', salescall.cur_actor(), false, %s FROM salescall.tasks t"
                    " WHERE t.task_id = %s", (back, s["task"])), OPERATOR)
            self.assertEqual(e.exception.constraint, "task_person_back_to_former")
        with self.assertRaises(Rejected) as e:
            self.tx(s, lambda tx: tx.execute(
                "INSERT INTO salescall.task_dial_fixes (tenant_id, client_id, task_id, contact_id, recorded_by, same_person, back_to)"
                " SELECT t.tenant_id, t.client_id, t.task_id, t.contact_id, salescall.cur_actor(), true, %s FROM salescall.tasks t"
                " WHERE t.task_id = %s", (suzuki, s["task"])), OPERATOR)
        self.assertEqual(e.exception.constraint, "task_person_back_to_former")
        self.back_name(s, "鈴木", "すずき", suzuki.isoformat())
        self.assertEqual(self.tx(s, lambda tx: next_action.person_now(tx, s["task"])), suzuki)   # 新しい期を作らず、鈴木様の期
        self.assertEqual(self.period_name(s), "鈴木")
        self.person_fix(s, "target_department", "総務部", same=True)            # 戻った期の中の「同じ」の直しも、その期の値
        self.assertEqual(self.tx(s, lambda tx: tx.one("SELECT target_department FROM salescall.task_call_values(%s)",
                                                      (s["task"],)))["target_department"], "総務部")
        self.assertEqual([(c["since"], c["values"]["target_name"]) for c in self.back_choices(s)], [(sato, "佐藤"), (None, "田中")])
        values = lambda since: self.tx(s, lambda tx: tx.one("SELECT target_name FROM salescall.task_person_values(%s, %s)",
                                                            (s["task"], since)))["target_name"]
        self.assertEqual((values(None), values(suzuki), values(sato)), ("田中", "鈴木", "佐藤"))   # その期の終わりの直前の値

    def back_to_first(self, s, way):
        """田中様（最初の担当の方）に戻す直し（名前などの直し・番号の直し。計画書 7-1 (30)）。"""
        from salescall.core import row_fixes
        if way == "name":
            self.back_name(s, "田中", "たなか", row_fixes.FIRST_PERIOD)
        else:
            first = self.phone_of(s, s["contact"])
            self.tx(s, lambda tx: row_fixes.fix_dial_number(tx, s["task"], first, same_person=False, back_to=row_fixes.FIRST_PERIOD),
                    OPERATOR)

    def test_going_back_brings_back_a_dropped_promise_while_its_time_remains(self):
        """田中様と明日15時の約束 → 鈴木様へ「違う」・「次の担当の方にだけかける」で約束をやめる → 田中様に戻す ⇒ やめた約束を戻す（✅ 2026-11-08 ユーザー＝
        計画書 7-1 (31)「約束を戻す」）：明日15時の約束の予定（約束の印・お客様が指定した時刻・幅・回の数え）に戻り、約束の時刻にかける（予定の期は田中様の期。
        next_action._period_promise の1か所）。約束の日・幅がもう過ぎてから戻すと、ふつうの電話。名前などの直しでも、番号の直しでも同じ。
        ⛔ 0213：戻しても約束は戻らず、田中様へのふつうの電話だった。"""
        from salescall.core import calling_hours
        for way in ("name", "dial"):
            for passed in (False, True):
                with self.subTest(way=way, passed=passed):
                    s = self.ready()
                    self.put_client(s, "gate.same_contact_min_interval", 1)
                    self.person_fix(s, "target_name", "田中", "たなか", same=True)
                    now = self.tx(s, lambda tx: tx.one("SELECT now() AS t")["t"])
                    day = calling_hours.local(now - timedelta(days=2) if passed else now + timedelta(days=1))
                    said = calling_hours.from_local(day.replace(hour=15, minute=0, second=0, microsecond=0))
                    w = self.tx(s, lambda tx: next_action._exact_window(tx, s["task"], said, day_said=True, beyond_ok=False))
                    w = replace(w, round=1, missed=1)                           # 回の数え（2日目の回・かけられなかった回が1つ）
                    self.tx(s, lambda tx: support.schedule_call(tx, s["task"], said, contact_id=s["contact"], cause="試験：田中様と15時の約束",
                                                               promised=True, callee_at=said, window=w))
                    if way == "name":
                        self.person_fix(s, "target_name", "鈴木", "すずき", keep=False, same=False)
                        new = s["contact"]
                    else:
                        new = self.fixed_as(s, False, False)
                    self.assertEqual(self.plan(s), self.handed_over(new))      # 約束をやめ、鈴木様へ引き継ぎを確かめる電話
                    self.back_to_first(s, way)
                    p = self.tx(s, lambda tx: next_action.current(tx, s["task"]))
                    self.assertIsNone(self.tx(s, lambda tx: next_action.person_now(tx, s["task"])))
                    if passed:                                                  # 約束の日・幅が過ぎた：ふつうの電話
                        self.assertEqual(self.plan(s), ("call", s["contact"], "normal", False, False, False, [], False, False))
                        continue
                    self.assertEqual((p["kind"], p["contact_id"], p["purpose"], p["promised"], p["callee_at"], p["call_at"],
                                      p["window_until"], p["window_round"], p["window_missed"], p["person_since"], p["handover"]),
                                     ("call", s["contact"], "normal", True, said, said, w.until, 1, 1, None, False))
                    self.assertTrue(self.tx(s, lambda tx: next_action.promise_alive(tx, s["task"])))
                    self.assertEqual(self.period_name(s), "田中")

    def test_going_back_after_the_round_passed_goes_to_the_next_round_while_days_remain(self):
        """✅ 2026-11-08 本体の決定（計画書 7-1 (31) の「約束の日・幅がもう過ぎていれば、ふつうの電話」の読み方）：「過ぎた」は約束全体（すべての日・回）が
        終わったとき。田中様と「昨日の15時」の約束（1日目の回）を「次の担当の方にだけ」でやめ、その回が過ぎてから田中様に戻す ⇒ まだ日が残っている
        （3日まで＝callback.exact_days）ので、次の回（次の15時）の約束の電話に戻す（約束の印・お客様が指定した時刻。その回にかけられなかったお詫びの
        印も付く）。今日のうちに取り戻す電話（こちらの止めのとき＝計画書 7-3 (4)）にはしない。すべての日が過ぎていれば、ふつうの電話（上の試験）。
        決めるのは next_action._period_promise の1か所。⛔ 0214：いまの回の終わりが過ぎたら、日が残っていても、ふつうの電話にしていた。0216。"""
        from salescall.core import calling_hours
        # recent＝約束の時刻が「いまの2時間前」（今日のその時刻の回も、もう過ぎた。試験の歯止め時間は1日じゅうなので、こちらの止めなら今日のうちに
        # 取り戻せる形）。それでも取り戻さず、次の回（明日のその時刻）へ（かけなかったのは契約者の選び）
        for way, recent in (("name", False), ("dial", False), ("name", True)):
            with self.subTest(way=way, recent=recent):
                s = self.ready()
                self.put_client(s, "gate.same_contact_min_interval", 1)
                self.person_fix(s, "target_name", "田中", "たなか", same=True)
                now = self.tx(s, lambda tx: tx.one("SELECT now() AS t")["t"])
                if recent and calling_hours.local(now).hour < 3:
                    continue   # 今日のその時刻の回が、日をまたぐ時刻は飛ばす
                day = calling_hours.local(now - timedelta(days=1))
                said = calling_hours.from_local(day.replace(hour=15, minute=0, second=0, microsecond=0)) if not recent else \
                    (now - timedelta(days=1, hours=2)).replace(second=0, microsecond=0)
                w = self.tx(s, lambda tx: next_action._exact_window(tx, s["task"], said, day_said=True, beyond_ok=False))
                self.tx(s, lambda tx: support.schedule_call(tx, s["task"], said, contact_id=s["contact"], cause="試験：田中様と昨日15時の約束",
                                                           promised=True, callee_at=said, window=w))
                if way == "name":
                    self.person_fix(s, "target_name", "鈴木", "すずき", keep=False, same=False)
                    new = s["contact"]
                else:
                    new = self.fixed_as(s, False, False)
                self.assertEqual(self.plan(s), self.handed_over(new))
                self.back_to_first(s, way)
                p = self.tx(s, lambda tx: next_action.current(tx, s["task"]))
                local_at, local_said = calling_hours.local(p["callee_at"]), calling_hours.local(said)
                self.assertEqual((p["kind"], p["contact_id"], p["purpose"], p["promised"], (local_at.hour, local_at.minute),
                                  p["window_recover_until"], p["person_since"]),
                                 ("call", s["contact"], "normal", True, (local_said.hour, local_said.minute), None, None))
                self.assertGreater(p["callee_at"], now)                          # 次の回（今日か明日の約束の時刻）
                self.assertIn(p["window_round"], (1, 2))
                self.assertIn("missed_promise", p["apology_reasons"])           # 約束の日にかけられなかったお詫び

    def test_a_dropped_promise_brought_back_does_not_count_the_calls_to_the_other_person(self):
        """やめていた約束を戻したとき、約束の回の数え（もうかけた回）は、約束の始めから数えたまま：やめていたあいだにかけた電話は、新しい担当の方への電話
        なので、戻した約束の回に数えない（担当の期の比べ＝core/attempts.in_promise_count の1か所。計画書 7-1 (31)。0214）。"""
        s = self.ready()
        self.put_client(s, "gate.same_contact_min_interval", 1)
        self.put_client(s, "gate.max_dials_per_contact_per_day", 20)
        self.person_fix(s, "target_name", "田中", "たなか", same=True)
        self.promise_window(s)                                                  # 田中様との幅の約束（いまの回の中）
        w = self.tx(s, lambda tx: next_action.window_of(next_action.current(tx, s["task"])))
        self.person_fix(s, "target_name", "鈴木", "すずき", keep=False, same=False)
        suzuki = self.tx(s, lambda tx: next_action.person_now(tx, s["task"]))
        time.sleep(1.1)
        self.settle(s, self.call(s, report=rep(("reception",), not_reached="absent")))   # 鈴木様への電話（約束の回の中）
        self.back_to_first(s, "name")
        p = self.tx(s, lambda tx: next_action.current(tx, s["task"]))
        self.assertEqual((p["promised"], p["window_until"], p["person_since"]), (True, w.until, None))   # 約束の回のまま戻った
        rounds = lambda period: self.tx(s, lambda tx: next_action._round_calls(tx, s["task"], w, 99, period))   # noqa: E731
        self.assertEqual((rounds(None), rounds(suzuki)), (0, 1))               # 田中様の約束の回には、鈴木様への電話を数えない
        self.assertTrue(self.tx(s, lambda tx: next_action._missed(tx, s["task"], w)))   # 田中様へは、まだかけていない

    def kept_promise(self, s, way):
        """田中様との幅の約束を「違う」・「約束も守る」で直す（番号の直し＝前の番号 O に約束が残る・名前などの直し＝会社の番号に約束が残る・
        mobile＝田中様が言った携帯 M への約束を、名前などの直しで残す）。新しい担当の方（鈴木様）へかける番号を返す。"""
        self.put_client(s, "gate.same_contact_min_interval", 1)
        self.put_client(s, "gate.max_dials_per_contact_per_day", 20)
        self.person_fix(s, "target_name", "田中", "たなか", same=True)
        at = self.promise_on_another_number(s) if way == "mobile" else s["contact"]
        if way != "mobile":
            self.promise_window(s)
        if way in ("name", "mobile"):
            self.person_fix(s, "target_name", "鈴木", "すずき", keep=True, same=False)
            new = s["contact"]
        else:
            new = self.fixed_as(s, False, True)
        p = self.tx(s, lambda tx: next_action.current(tx, s["task"]))
        self.assertEqual((p["contact_id"], p["promised"], p["person_since"]), (at, True, None))   # 田中様の約束のまま
        return new

    def test_a_kept_promise_is_called_through_its_window_and_then_hands_over(self):
        """「前の担当の方との約束も守る」を選んだ ⇒ 約束の日・幅のあいだは、約束の決まりどおり田中様へかけ続ける（1回で移らない）。受付の方が出て「不在」でも、
        田中様は新しい約束をしていないので選び待ちにしない（データベースの salescall.task_old_promise_call＝新しい約束をした通話だけ）。約束を使い切っても
        話せなければ、行を終えずに鈴木様へ引き継ぎを確かめる電話（✅ 2026-11-08 ユーザー＝計画書 7-1 (32)「約束の幅いっぱい → 鈴木様」。番号の直しも名前の
        直しも同じ＝next_action._kept_old_promise・_after_former の1か所）。
        ⛔ 0213：番号の直しだけ、前の番号へ1回かけて出なければ新しい番号へ移った。名前などの直しでは約束を使い切ると行が終わり、鈴木様へかからなかった。"""
        for way in ("dial", "name"):
            with self.subTest(way=way):
                s = self.ready()
                new = self.kept_promise(s, way)
                now = self.tx(s, lambda tx: tx.one("SELECT now() AS t")["t"])   # 試験：約束の最初の回の中で、いまかける（同じ回のまま2本）
                w = next_action.Window(now + timedelta(hours=1), None, None, now - timedelta(hours=1), "week")
                for n, report in enumerate((None, rep(("reception",), not_reached="absent")), 1):   # 出なかった・受付の方が出て不在
                    self.at(s, s["contact"], window=w)
                    time.sleep(1.1)
                    aid = self.call(s, ["dialing", "no_answer"]) if report is None else self.call(s, report=report)
                    self.settle(s, aid)
                    self.assertTrue(self.tx(s, lambda tx: tasks.former_person_call(tx, aid)))   # 田中様への通話
                    # 約束の回の本数：田中様の番号への電話も数える（番号の直しの数え直しは、いまの担当の方の決まり＝attempts.in_promise_count）
                    self.assertEqual(self.tx(s, lambda tx: next_action._round_calls(tx, s["task"], w, 99, None)), n)
                    p = self.tx(s, lambda tx: next_action.current(tx, s["task"]))
                    self.assertEqual((p["kind"], p["contact_id"], p["promised"], p["person_since"], p["handover"]),
                                     ("call", s["contact"], True, None, False))   # 田中様の約束のまま（1回で移らない）
                    self.assertIsNone(self.tx(s, lambda tx: tasks.old_promise_waiting(tx, s["task"])))   # 選び待ちにしない
                now = self.tx(s, lambda tx: tx.one("SELECT now() AS t")["t"])
                self.use_up(s)
                self.assertEqual(self.plan(s), self.handed_over(new))          # 使い切った：行を終えず、鈴木様へ引き継ぎ
                self.assertIsNotNone(self.tx(s, lambda tx: next_action.current(tx, s["task"]))["person_since"])
                self.assertEqual(self.period_name(s), "鈴木" if way == "name" else "田中")   # 番号の直しでは名前は変わらない
                self.back_to_first(s, way)                                     # 田中様に戻しても、使い切った約束は戻らない（やめた約束ではない）
                self.assertEqual(self.plan(s), ("call", s["contact"], "normal", False, False, False, [], False, False))

    def use_up(self, s):
        """試験：「守る」約束の最後の回が、もう終わる ⇒ その回の電話に出ない（約束を使い切った）。"""
        now = self.tx(s, lambda tx: tx.one("SELECT now() AS t")["t"])
        self.at(s, s["contact"], window=next_action.Window(now + timedelta(seconds=120), None, None, now - timedelta(hours=1), "week",
                                                           round=50))
        time.sleep(1.1)
        self.settle(s, self.call(s, ["dialing", "no_answer"]))

    def test_a_kept_promise_already_called_stays_on_its_number_when_the_number_is_fixed_again(self):
        """「守る」約束の電話を一度かけたあと、契約者が「同じ」で番号をまた直した ⇒ 約束は前の担当の方の番号のまま（いまの約束の電話をもうかけていても、
        いまかける番号へ移さない＝next_action._point が `_kept_old_promise` を読む。「同じ」の番号の直しの「前の番号へは1回」＝計画書 7-1 (4) は、前の
        担当の方との「守る」約束には当てない＝7-1 (32)）。"""
        for way in ("dial", "name"):
            with self.subTest(way=way):
                s = self.ready()
                self.kept_promise(s, way)
                time.sleep(1.1)
                self.settle(s, self.call(s, ["dialing", "no_answer"]))
                third = self.fixed_as(s, True)                                 # 鈴木様の別の番号へ（同じ方）
                p = self.tx(s, lambda tx: next_action.current(tx, s["task"]))
                self.assertEqual((p["contact_id"], p["promised"], p["person_since"]), (s["contact"], True, None))
                self.use_up(s)
                self.assertEqual(self.plan(s), self.handed_over(third))

    def test_a_kept_promise_answered_by_the_old_person_follows_the_former_answers(self):
        """「守る」約束の途中で田中様が出て話した ⇒ いまの決まりどおり（前の担当の方の答えの分け方 FORMER_ANSWERS の1か所）：まだ続く答え（約束でない
        答え）なら鈴木様へ引き継ぎ・用件の断り（その方個人の答え）なら鈴木様へ・新しい約束なら契約者の選び。番号の直しも名前などの直しも同じ
        （next_action._after_former の1か所。⛔ 0213：前の担当の方の携帯では、話したあとも田中様への電話が続いた）。"""
        from tests.test_judge import goal_refused
        for way in ("dial", "name", "mobile"):
            for answer, report in (("continue", rep()), ("declined", goal_refused(rep())),
                                   ("promise", rep(callback=callback("later", time="あとで")))):
                with self.subTest(way=way, answer=answer):
                    s = self.ready()
                    new = self.kept_promise(s, way)
                    time.sleep(1.1)
                    aid = self.call(s, report=report)
                    self.settle(s, aid)
                    if answer == "promise":
                        self.assertEqual(self.tx(s, lambda tx: tasks.old_promise_waiting(tx, s["task"])), aid)
                    else:
                        self.assertEqual(self.plan(s), self.handed_over(new))
                        self.assertIsNotNone(self.tx(s, lambda tx: next_action.current(tx, s["task"]))["person_since"])

    def test_a_kept_promise_used_up_at_the_gate_also_hands_over(self):
        """「守る」約束の回が、門で止められているあいだに使い切られた（幅の外で止められ、次の回が打ち止め）⇒ 通話のあとと同じく、行を終えずに新しい担当の方へ
        引き継ぎを確かめる電話（門のあとも next_action._after_former の1か所。前の担当の方の期の予定を止めたとき。0214。⛔ 0213 まで：行が終わった）。
        お呼びする方が替わっていない行（same）の約束は、いままでどおり行を終える。"""
        for way in ("dial", "name", "same"):
            with self.subTest(way=way):
                s = self.ready()
                if way == "same":
                    self.put_client(s, "gate.same_contact_min_interval", 1)
                    self.promise_window(s)
                else:
                    new = self.kept_promise(s, way)
                now = self.tx(s, lambda tx: tx.one("SELECT now() AS t")["t"])
                self.at(s, s["contact"], window=next_action.Window(now - timedelta(minutes=1), None, None, now - timedelta(hours=1), "week",
                                                                   round=50))   # 試験：最後の回が、かけられないまま過ぎた
                d = self.tx(s, lambda tx: next_action.after_denial(tx, s["task"], gate.denied("callback_window"), tenant_id=s["tenant"],
                                                                   client_id=s["client"], business_id=s["business"]))
                if way == "same":
                    self.assertEqual((d.kind, d.end_reason), ("end", "callback_unspecified_time"))
                else:
                    self.assertEqual(self.plan(s), self.handed_over(new), d)

    def test_going_back_keeps_the_promise_kept_for_the_person_left_before_a_dropped_one(self):
        """田中様の約束を「次の担当の方にだけ」でやめ、鈴木様が「あとで」と約束 → 田中様に戻すとき鈴木様との約束を「守る」⇒ 契約者が守ると選んだ鈴木様の約束を
        先にする（やめた田中様の約束は戻さない＝next_action._period_promise。0214）。"""
        from salescall.core import row_fixes
        s = self.ready()
        self.put_client(s, "gate.same_contact_min_interval", 1)
        self.put_client(s, "gate.max_dials_per_contact_per_day", 20)
        self.person_fix(s, "target_name", "田中", "たなか", same=True)
        self.promise_window(s)
        self.person_fix(s, "target_name", "鈴木", "すずき", keep=False, same=False)
        suzuki = self.tx(s, lambda tx: next_action.person_now(tx, s["task"]))
        time.sleep(1.1)
        self.settle(s, self.call(s, report=rep(callback=callback("later", time="あとで"))))   # 鈴木様の約束
        self.back_name(s, "田中", "たなか", row_fixes.FIRST_PERIOD, keep=True)
        p = self.tx(s, lambda tx: next_action.current(tx, s["task"]))
        self.assertEqual((p["promised"], p["window_until"], p["person_since"]), (True, None, suzuki))   # 鈴木様の約束のまま
        self.assertEqual(self.period_name(s), "鈴木")

    def test_an_ask_again_to_the_old_person_used_up_hands_over_to_the_new_person(self):
        """前の担当の方の期に残る番号の聞き直し（その番号で電話を受けた方への電話）を、決めた回数かけても出なかった ⇒ 行を終えずに、新しい担当の方へ
        引き継ぎを確かめる電話（「守る」約束を使い切ったとき・お詫びを使い切ったときと同じ next_action._after_former の1か所。0214。⛔ 0213 まで：名前などの
        直しでは行が終わった）。"""
        s = self.ready()
        self.put_client(s, "gate.same_contact_min_interval", 1)
        self.put_client(s, "retry.ask_number_again_max", 1)
        self.person_fix(s, "target_name", "田中", "たなか", same=True)
        now = self.tx(s, lambda tx: tx.one("SELECT now() AS t")["t"])
        self.tx(s, lambda tx: support.schedule_call(tx, s["task"], now, contact_id=s["contact"], cause="試験：番号の聞き直し",
                                                   promised=True, purpose="ask_number_again", wrong_contact_id=s["contact"]))
        self.person_fix(s, "target_name", "鈴木", "すずき", same=False)
        p = self.tx(s, lambda tx: next_action.current(tx, s["task"]))
        self.assertEqual((p["purpose"], p["person_since"]), ("ask_number_again", None))   # 田中様への聞き直しのまま
        time.sleep(1.1)
        aid = self.call(s, report=rep(("reception",), not_reached="absent"))
        self.settle(s, aid)
        self.assertTrue(self.tx(s, lambda tx: tasks.former_person_call(tx, aid)))
        self.assertEqual(self.plan(s), self.handed_over(s["contact"]))

    def test_the_reached_target_is_written_only_with_the_settle_record(self):
        """目的のお客様と話せたか（attempts.reached_target。引き継ぎを確かめる印が読む）を書けるのは、その通話の締めの記録（結果の版・お詫びの電話の
        結び）を書く取引の中だけ（データベースの守り attempt_reached_target_guard。0211。データベースの監査 K の (d)：守りが無かった）。"""
        s = self.ready()
        aid = self.call(s, report=rep(("reception",), not_reached="absent"))
        self.settle(s, aid)
        with self.assertRaises(Rejected) as e:
            self.tx(s, lambda tx: attempts.record_reached_target(tx, aid, True))
        self.assertEqual(e.exception.constraint, "attempt_reached_target_by_settle")
        self.tx(s, lambda tx: attempts.record_reached_target(tx, aid, False))   # 変わらない書き直しは、通す（締め直しと同じ値）


class CompareForm(unittest.TestCase):
    def test_the_compare_form_is_the_same_as_the_source_check(self):
        """お呼びする方が替わるかで値を比べる形（データベースの salescall.compare_form。0209）は、出典の照らしの1か所
        core/source_check.normalize と同じ決まり（文字の形をそろえ・見た目の同じ記号をそろえ・空白をすべて除く）。空は NULL（値が無いのと
        同じ）。空白の文字・記号の組がずれれば、ここが落ちる。"""
        from salescall.core import source_check
        marks = [chr(k) for k in source_check._SAME_MARKS]
        spaces = [chr(c) for c in range(0x110000) if chr(c).isspace()]
        samples = (spaces + marks + [f"田{c}中" for c in spaces + marks]
                   + ["田中　太郎", " 田中 太郎 ", "ﾀﾅｶ ﾀﾛｳ", "タナカタロウ", "第１営業部", "ＡＢＣ商事", "営業部―第一課", "9:00〜18:00",
                      "ー", "ｰ", "①", "", "\u3000\u3000"])
        got = owner_access().in_system(SYSTEM, lambda tx: [r["f"] for r in tx.all(
            "SELECT salescall.compare_form(x) AS f FROM unnest(%s::text[]) WITH ORDINALITY u(x, i) ORDER BY i", (samples,))])
        self.assertEqual(got, [source_check.normalize(x) or None for x in samples])
        self.assertIsNone(owner_access().in_system(SYSTEM, lambda tx: tx.one("SELECT salescall.compare_form(NULL) AS f")["f"]))


class SalesPurposes(unittest.TestCase):
    def test_the_sales_purposes_are_the_same_in_python_and_the_database(self):
        """営業の電話の目的（引き継ぎを確かめる印を持てる・番号を直したらいまかける番号へ替える）の組は1つ：next_action.SALES_PURPOSES と、
        データベースの salescall.sales_purpose（次の予定の CHECK task_next_actions_handover_calls が読む。0201）が同じ組。"""
        got = owner_access().in_system(SYSTEM, lambda tx: [r["p"] for r in tx.all(
            "SELECT p FROM unnest(%s::text[]) p WHERE salescall.sales_purpose(p)", (list(next_action.PURPOSES),))])
        self.assertEqual(sorted(got), sorted(next_action.SALES_PURPOSES))
        self.assertEqual(set(next_action.NUMBER_BOUND), set(next_action.PURPOSES) - set(next_action.SALES_PURPOSES))


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
        run2 = self.w.run(s["tenant"], s["client"], s["business"], s["version"])
        t2 = self.w.task(s["tenant"], s["client"], run2, s["contact"])
        other = dict(s, task=t2)
        self.tx(s, lambda tx: next_action.schedule_now(tx, t2, cause="試験"))
        first = self.call(s, ["dialing", "call_failed"])                       # 1つ目：番号のせい
        d = self.settle(s, first)["decision"]
        # 1日目にもう1回（並びの1つ目）：次の日は失敗した日のまま、時刻は最短の間隔のあと（日付の替わり目をまたいでも同じ決まりで比べる。
        # ⛔ 2026-11-02：前は試験の始めに読んだ「今日」と比べたので、日本時間の0時をまたいで流れると落ちた）
        failed_day = self.tx(s, lambda tx: tx.one("SELECT salescall.jst(ended_at)::date AS d FROM salescall.attempts WHERE attempt_id = %s",
                                                  (first,))["d"])
        wait = self.tx(s, lambda tx: health.number_wait(tx, s["contact"]))
        after = self.tx(s, lambda tx: tx.one("SELECT now() AS t")["t"])
        self.assertEqual(wait.day, failed_day)
        self.assertTrue(wait.not_before <= d.at <= max(after, wait.not_before), (d.at, wait, after))
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
        again = self.tx(s, lambda tx: health.number_wait(tx, s["contact"]))
        self.assertEqual(again.day, failed_day)                                # 分からない電話では、次の日へ送らない
        self.assertEqual(at, max(now, again.not_before))
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

    def test_ask_number_again_on_the_old_number_goes_once_then_to_the_new_number(self):
        """✅ 2026-11-07 ユーザー（伺い「聞き直し」に「お詫びと同じ」＝計画書 7-1 (18)）：番号を直したとき、前の番号への番号の聞き直しは
        直したあと1回まで。届かなければ、いまかける番号へふつうの電話（契約者が直した番号を正しい番号とみなす）。前の番号への聞き直しが
        もう鳴っていれば、直したときに移す（next_action._old_number_moves の1か所＝お詫びと同じ）。⛔ 0195 の初めの作りは、前の番号への
        聞き直しを直したときから0回で数え直し、同じお客様へ決めた回数をもう一度かけた。"""
        from salescall.core import row_fixes
        for rang_before in (False, True):
            with self.subTest(rang_before=rang_before):
                s = self.ready()
                old = s["contact"]
                now = lambda: self.tx(s, lambda tx: tx.one("SELECT now() AS t")["t"])   # noqa: E731
                ask = lambda: self.tx(s, lambda tx: support.schedule_call(   # noqa: E731
                    tx, s["task"], now(), contact_id=old, cause="試験：番号の聞き直し", promised=True, purpose="ask_number_again",
                    wrong_contact_id=old))
                ask()
                if rang_before:
                    self.settle(s, self.call(s, report=self.absent(False)))           # 直す前に前の番号で1回鳴った
                new = self.tx(s, lambda tx: row_fixes.fix_dial_number(tx, s["task"], fictional_jp_fixed(), same_person=True), OPERATOR)["contact_id"]
                p = self.tx(s, lambda tx: next_action.current(tx, s["task"]))
                if rang_before:                                                        # 直したときに、いまかける番号へ
                    self.assertEqual((p["purpose"], p["contact_id"]), ("normal", new))
                    continue
                self.assertEqual((p["purpose"], p["contact_id"]), ("ask_number_again", old))   # まだなら前の番号で1回
                ask()
                d = self.settle(s, self.call(s, report=self.absent(False)))["decision"]
                self.assertEqual((d.kind, d.target.purpose, d.target.contact_id), ("call", "normal", new))

    def test_the_answer_of_the_call_ending_record_counts(self):
        """同じ日にかけてよいかは、その通話を締めた「不在」「取り次げない」の記録のうち、いちばん新しいものの答え（1か所）。"""
        from salescall.core.call_report import NotTransferred, Said
        r = self.absent(True)
        untimed = lambda c: c.time_said is None   # noqa: E731（判定は時刻の言葉を読んで決める＝judge._untimed_offer）
        self.assertTrue(r.may_call_again(untimed))
        later = replace(r, not_transferred=(NotTransferred(Said(5, "やはりお繋ぎできかねます。"), None),))
        self.assertFalse(later.may_call_again(untimed))         # あとの「取り次げない」で聞いていない
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

    def test_who_unclear_ends_the_row_and_returns_the_blanks_to_fill(self):
        """お名前だけではどの方か分からなかった（繋がらなかった理由 who_unclear。CL-1 の連絡帳 4-65 の (e)(g)。✅ 2026-11-02 日本時間1時22分・
        1時54分 ユーザー）：日を改めて同じお名前でかけても同じになるので、かけ直さずに行を終え（システムの方針 not_reached_who_unclear）、
        結果に、依頼者に埋めていただく欄（その行の一覧で空いているフルネーム・部署＝business_settings.who_unclear_blanks の1か所）を残す。
        お呼びし直しても分からなかった行（recalled）も同じ。"""
        from salescall.core.call_report import Said, WhoUnclear
        for recalled in (False, True):
            with self.subTest(recalled=recalled):
                s = self.ready()
                report = replace(rep(("reception",), not_reached="who_unclear"),
                                 who_unclears=(WhoUnclear(Said(1, "お名前だけではどの者か分かりかねます。"), recalled),))
                aid = self.call(s, report=report)
                got = self.settle(s, aid)
                self.assertEqual((got["state"], got["decision"].kind, got["decision"].end_reason),
                                 ("not_reached", "end", "not_reached_who_unclear"))
                r = self.tx(s, lambda tx: results.latest(tx, s["task"]))
                self.assertEqual((r["detail"]["not_reached"], r["detail"]["who_unclear_blanks"]),
                                 ("who_unclear", ["full_name", "department"]))


class NotAskedSettle(SettleBase):
    def test_a_question_whose_condition_did_not_hold_is_returned_as_not_asked(self):
        """前の答えで伺う質問（業務設定の ask_if。CL-1 の連絡帳 4-66 の (b)(e)）：締めは、置いた版の条件（business_settings.ask_ifs の
        1か所）を判定に渡す。前の質問の答えが伺う答えでなければ、伺っていない（対象外）＝取れていない必須の質問に数えず（すべて取れた）、
        結果の中身 not_asked で依頼者へ返す。伺う答えなのに伺えていなければ、取れていない（一部だけ取れた）。"""
        two = support.content()
        two["questions"].append({"id": "Q2", "text": "以上の内容でよろしいでしょうか？", "ask_about": "ご承認", "type": "yes_no",
                                 "required": True, "choices": None, "next": None, "unit": None, "meanings": None, "replies": None,
                                 "asks_fact": False, "do_words": None, "away": None, "intro": "それでは重要な点だけご説明させていただきます。",
                                 "ask_if": {"after": "Q1", "answers": ["わからない"], "hedged": False}, "approval": True, "final_goal": False, "declines": [], "tell_words": {}, "ends_on": [], "unclear_none": "", "doubt_words": {}, "asks_way": False, "unclear_none": "", "doubt_words": {}, "asks_way": False, "option_notes": {}, "change_by": "", "definite_value": False})
        for q1, state, more in ((("Q1", "confirmed", "答えた"), "completed", {"not_asked": ["Q2"]}),
                                (("Q1", "accepted", "わからない"), "incomplete", {"missing": ["Q2"]})):
            with self.subTest(q1=q1):
                self.settled_with(two, q1, state, more)

    def settled_with(self, content, q1, state, more):
        """その業務設定の中身（質問 Q1・Q2。Q2 は伺う条件つきの必須）で、Q1 の項目だけを置いた通話を締め、結果の状態と中身を確かめる。"""
        from unittest import mock
        with mock.patch.object(support, "content", lambda target_person=None: copy.deepcopy(content)):
            s = self.w.setup(phone=normalize(fictional_jp_fixed()).e164)
        self.tx(s, lambda tx: tx.execute(
            "INSERT INTO salescall.questions (tenant_id, client_id, version_id, question_id, position, type_name, required)"
            " VALUES (%s, %s, %s, 'Q2', 2, 'yes_no', true)", (s["tenant"], s["client"], s["version"])), OPERATOR)
        self.put_client(s, "calling.hours", ALL_DAY)
        self.tx(s, lambda tx: next_action.schedule_now(tx, s["task"], cause="試験"))
        self.tx(s, lambda tx: quality_stage.open_stage(tx, s["version"], 1, None, evidence="試験"), OPERATOR)
        self.tx(s, lambda tx: business_rules.save(tx, s["version"], support.rules_raw(required=("Q1", "Q2"))), OPERATOR)
        self.lease(s)
        aid = self.call(s, items=(q1,))
        got = self.settle(s, aid)
        self.assertEqual(got["state"], state)
        r = self.tx(s, lambda tx: results.latest(tx, s["task"]))
        for k, v in more.items():
            self.assertEqual(r["detail"][k], v)
        return s, aid

    def test_a_question_asked_only_after_a_hedged_answer_is_not_asked_after_a_definite_one(self):
        """言い切っていないときだけ伺う質問（伺う条件の hedged。CL-1 の連絡帳 4-71 の (b)）：締めは、行の項目の「曖昧」の印（項目の値の
        hedged）を判定に渡す。前の質問の答えが伺う答えでも、言い切っていれば伺っていない（対象外）。言い切っていなければ伺う＝伺えていなければ
        取れていない。印は判定の入力の写しにも残る（判定し直すかの比べ）。"""
        two = support.content()
        two["questions"][0].update(type="yes_no", asks_fact=False)
        two["questions"].append({"id": "Q2", "text": "日時変更なさいますか？それとも、キャンセルなさいますか？", "ask_about": "日時の変更", "type": "yes_no",
                                 "required": True, "choices": None, "next": None, "unit": None, "meanings": None, "replies": None,
                                 "asks_fact": False, "do_words": None, "away": None, "intro": None,
                                 "ask_if": {"after": "Q1", "answers": ["いいえ"], "hedged": True}, "approval": False, "final_goal": False,
                                 "declines": [], "tell_words": {}, "ends_on": [], "unclear_none": "", "doubt_words": {}, "asks_way": False, "unclear_none": "", "doubt_words": {}, "asks_way": False, "option_notes": {}, "change_by": "", "definite_value": False})
        for q1, state, more, hedged in ((("Q1", "accepted", "いいえ"), "completed", {"not_asked": ["Q2"]}, False),
                                        (("Q1", "accepted", "いいえ", "都合が悪くなってしまいまして"), "incomplete", {"missing": ["Q2"]}, True),
                                        (("Q1", "accepted", "はい", "たぶん大丈夫かと"), "completed", {"not_asked": ["Q2"]}, True)):
            with self.subTest(q1=q1):
                s, aid = self.settled_with(two, q1, state, more)
                snap = self.tx(s, lambda tx: tx.one("SELECT snapshot FROM salescall.call_judgements WHERE attempt_id = %s",
                                                    (aid,)))["snapshot"]
                self.assertEqual(snap["items"]["Q1"].get("hedged", False), hedged)

    def test_customer_words_on_an_answer_not_taken_are_not_a_hedged_mark(self):
        """項目の値の欄 hedged には、「-」「✕」で渡る答えにも依頼者へ渡すお客様の言葉が入る（CL-1 の連絡帳 4-115 の (2)）。締めが判定に渡す
        「曖昧」の印は、取れた答えのときだけ（`judge.hedged_words` の1か所）：わからない答えの言葉を「言い切っていない答え」と読むと、伺う条件の
        「言い切っていないときだけ伺う」が、わからない答えのあとの質問を伺うはずの質問と読み、取れていない質問に数える。"""
        s = self.ready()
        aid = self.call(s, items=(("Q1", "dont_know", "わからない", "まだ決まってないんですよね。"),))
        self.assertFalse(self.tx(s, lambda tx: settle._items(tx, s["task"]))["Q1"].hedged)
        self.tx(s, lambda tx: tx.execute("UPDATE salescall.item_values SET state = 'accepted', condition = '答えた'"
                                         " WHERE task_id = %s AND attempt_id = %s", (s["task"], aid)))
        self.assertTrue(self.tx(s, lambda tx: settle._items(tx, s["task"]))["Q1"].hedged)


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

    def test_the_claim_tells_whether_the_cut_call_was_today(self):
        """途中で切れた電話の続きの予約証に、切れた電話が今日か（apology_when＝same_day／later。CL-1 の連絡帳 4-99：「先ほどは
        （前回は）、お話の最後に通信が悪く…」）。決めるのは、お詫びの理由 cut_off を付けた元の電話（次の予定の cut_call＝0187）で、
        そのあとの出なかった電話ではない（⛔ 0186 の思い違い：いちばん新しい発信で決めていて、昨日切れたあと今日出なかった電話が
        あると「先ほどは」と言った）。お詫びの理由の無い電話には入れない。"""
        s = self.ready()
        c = self.claim(s)
        self.assertIsNone(c.apology_when)                                     # ふつうの電話
        self.tx(s, lambda tx: attempts.release(tx, [c.attempt_id]))
        cut = self.call(s, ("dialing", "ringing", "answered", "in_conversation", "dropped"), side="line",
                        report=rep(cut=Cut("callee", False, False)))
        self.settle(s, cut)
        self.assertEqual(self.tx(s, lambda tx: next_action.current(tx, s["task"]))["cut_call"], cut)
        c = self.claim(s)
        self.assertEqual((c.purpose, c.apology, c.apology_when), ("resume", ("cut_off",), "same_day"))
        self.tx(s, lambda tx: attempts.release(tx, [c.attempt_id]))

        def back(tx):   # 試験：切れた電話を前の日にする（時刻はデータベースが決めるので、持ち主の取引の中だけ仕掛けを外す）
            tx.execute("ALTER TABLE salescall.attempts DISABLE TRIGGER USER")
            tx.execute("UPDATE salescall.attempts SET claimed_at = claimed_at - interval '1 day',"
                       " ended_at = ended_at - interval '1 day' WHERE attempt_id = %s", (cut,))
            tx.execute("ALTER TABLE salescall.attempts ENABLE TRIGGER USER")
        owner_access().in_system(SYSTEM, back)
        self.settle(s, self.call(s, ["dialing", "no_answer"]))                 # 今日、続きの電話に出なかった
        self.assertEqual(self.tx(s, lambda tx: next_action.current(tx, s["task"]))["cut_call"], cut)   # 元の電話は引き継ぐ
        now = self.tx(s, lambda tx: tx.one("SELECT now() AS t")["t"])
        self.tx(s, lambda tx: support.schedule_call(tx, s["task"], now, contact_id=s["contact"],
                                                       cause="試験：その時刻が来たことにする", promised=True))
        c = self.claim(s)
        self.assertEqual((c.apology, c.apology_when), (("cut_off",), "later"))   # 切れたのは昨日＝「前回は」

    def test_the_apology_is_for_the_last_cut_call_in_which_the_callee_was_talking(self):
        """お詫びの元の電話（cut_call）は、目的のお客様と話していた電話が切れたらその電話。受付の方と話している途中で切れた電話では
        替えない（お詫びを聞くのは目的のお客様＝2026-11-06 の見直し (C) の3）。目的のお客様とまた話して切れたら、その新しい電話。"""
        s = self.ready()
        c = self.claim(s)
        self.tx(s, lambda tx: attempts.release(tx, [c.attempt_id]))
        cut = ("dialing", "ringing", "answered", "in_conversation", "dropped")
        first = self.call(s, cut, side="line", report=rep(cut=Cut("callee", False, False)))
        self.settle(s, first)

        def back(tx):   # 試験：目的のお客様と話していて切れた電話を前の日にする（持ち主の取引の中だけ仕掛けを外す）
            tx.execute("ALTER TABLE salescall.attempts DISABLE TRIGGER USER")
            tx.execute("UPDATE salescall.attempts SET claimed_at = claimed_at - interval '1 day',"
                       " ended_at = ended_at - interval '1 day' WHERE attempt_id = %s", (first,))
            tx.execute("ALTER TABLE salescall.attempts ENABLE TRIGGER USER")
        owner_access().in_system(SYSTEM, back)
        cur = lambda: self.tx(s, lambda tx: next_action.current(tx, s["task"]))   # noqa: E731
        now = lambda: self.tx(s, lambda tx: tx.one("SELECT now() AS t")["t"])   # noqa: E731
        come = lambda: self.tx(s, lambda tx: support.schedule_call(   # noqa: E731
            tx, s["task"], now(), contact_id=s["contact"], cause="試験：その時刻が来たことにする", promised=True))
        reception = self.call(s, cut, side="line", report=rep(("reception",), cut=Cut("callee", False, False)))
        self.settle(s, reception)                                             # 今日、受付の方と話している途中で切れた
        self.assertEqual((cur()["apology_reasons"], cur()["cut_call"]), (["cut_off"], first))
        come()
        c = self.claim(s)
        self.assertEqual(c.apology_when, "later")                             # お詫びは、昨日の目的のお客様との電話について
        self.tx(s, lambda tx: attempts.release(tx, [c.attempt_id]))
        again = self.call(s, cut, side="line", report=rep(cut=Cut("callee", False, False)))
        self.settle(s, again)                                                 # 今日、目的のお客様とまた話して切れた
        self.assertEqual(cur()["cut_call"], again)
        come()
        self.assertEqual(self.claim(s).apology_when, "same_day")

    def test_whether_a_call_was_today_is_the_japanese_date_of_its_end(self):
        """前の電話が今日かは、電話が終わった時刻の日本時間の日付（salescall.call_today の1か所。0188）。日本時間の0時の前後で分かれ、
        かけ始めが前の日でも、0時を過ぎて終わった電話は今日（2026-11-06 の見直し (C) の4・6）。"""
        s = self.ready()
        aid = self.call(s, ["dialing", "no_answer"])
        today = lambda: self.tx(s, lambda tx: tx.one("SELECT salescall.call_today(%s) AS t", (aid,))["t"])   # noqa: E731
        midnight = self.tx(s, lambda tx: tx.one(
            "SELECT (date_trunc('day', now() AT TIME ZONE 'Asia/Tokyo') AT TIME ZONE 'Asia/Tokyo') AS m, now() AS n"))
        if midnight["n"] - midnight["m"] < timedelta(minutes=2):
            self.skipTest("日本時間の0時の直後は、0時を過ぎた終わりの時刻を作れない")

        def at(claimed, ended):
            def work(tx):   # 試験：時刻はデータベースが決めるので、持ち主の取引の中だけ仕掛けを外す
                tx.execute("ALTER TABLE salescall.attempts DISABLE TRIGGER USER")
                tx.execute("UPDATE salescall.attempts SET claimed_at = %s, ended_at = %s WHERE attempt_id = %s", (claimed, ended, aid))
                tx.execute("ALTER TABLE salescall.attempts ENABLE TRIGGER USER")
            owner_access().in_system(SYSTEM, work)
        m = midnight["m"]
        at(m - timedelta(minutes=2), m + timedelta(minutes=1))
        self.assertTrue(today())                       # 前の日の 23時58分にかけ始め、0時01分に終わった＝今日
        at(m - timedelta(minutes=3), m - timedelta(minutes=1))
        self.assertFalse(today())                      # 前の日の 23時59分に終わった＝前の日（日本時間の0時が境）

    def test_the_cut_off_reason_always_names_the_call_that_was_cut(self):
        """お詫びの理由 cut_off と、その元の電話（cut_call）は、いつも一緒（データベースの守り 0187。元の電話はその行の試行だけ）。
        予定を書く1か所（next_action._append）も、元の電話の分からない理由 cut_off を書かない。"""
        s = self.ready()
        sql = ("INSERT INTO salescall.task_next_actions (tenant_id, client_id, task_id, kind, call_at, cause, promised,"
               " contact_id, purpose, apology_reasons, cut_call)"
               " SELECT tenant_id, client_id, task_id, kind, call_at, cause, promised, contact_id, purpose, %s, %s"
               " FROM salescall.current_next_actions WHERE task_id = %s")
        c = self.claim(s)
        self.tx(s, lambda tx: attempts.release(tx, [c.attempt_id]))
        other = self.ready()
        oc = self.claim(other)
        for reasons, call, constraint in ((["cut_off"], None, "task_next_actions_cut_call"),
                                          ([], c.attempt_id, "task_next_actions_cut_call"),
                                          (["cut_off"], oc.attempt_id, "task_next_actions_cut_call_of_task")):
            with self.subTest(reasons=reasons), self.assertRaises(Rejected) as e:
                self.tx(s, lambda tx: tx.execute(sql, (reasons, call, s["task"])))
            self.assertEqual(e.exception.constraint, constraint)
        with self.assertRaises(ValueError):
            self.tx(s, lambda tx: next_action._append(tx, s["task"], call_at=None, call_now=True, end_reason=None, cause="試験",
                                                       promised=True, target=next_action.Target(s["contact"], None, "resume"),
                                                       apology=("cut_off",)))

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
        cur = self.tx(s, lambda tx: next_action.current(tx, s["task"]))
        self.assertEqual((cur["apology_reasons"], cur["cut_call"]), (["cut_off"], aid))   # お詫びは、切れ方の分からなかった通話について
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
        self.assertEqual((d.kind, d.end_reason), ("end", next_action.LATER_OVER))   # 2回目は方針の回数を超える（0194 で終わりの理由を分けた）

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
        got = self.tx(s, lambda tx: next_action._exact_next_try(tx, s["task"], window, now, None))
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
        self.tx(s, lambda tx: tx.execute(sql, (0, ["missed_promise"], s["task"])))
        self.assertTrue(self.tx(s, lambda tx: next_action.current(tx, s["task"]))["apology_owed"])
        c = self.claim(s)
        self.assertEqual((c.apology, c.apology_when), (("missed_promise",), None))   # 同じ日かは、途中で切れた電話のときだけ

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



class Review26a(SettleBase):
    """見直し26a（電話の一生を動かして確かめた。0162）：行が宙に浮かない・まだ分からない電話は予定を書かない。"""

    SILENT = ("dialing", "ringing", "answered", "in_conversation", "dropped")

    def other_row(self, s):
        run2 = self.w.run(s["tenant"], s["client"], s["business"], s["version"])
        t = self.w.task(s["tenant"], s["client"], run2, s["contact"])
        self.tx(s, lambda tx: next_action.schedule_now(tx, t, cause="試験"))
        return t

    def test_forcing_no_call_stops_waiting_for_the_unclear_cut_check(self):
        """切れ方が分からない通話の行を「強制かけない」にしたら、確かめのかけ直しはもうかからないので、待たない（同じお客様のほかの行を
        止め続けない。⛔ 見直し26a の 1：ほかの行が門でも候補でも永久に止まった）。確定の印の無い結果の版も確かめ直す。"""
        s = self.ready()
        other = self.other_row(s)
        aid = self.call(s, self.SILENT, side="callee", report=rep(cut=Cut("ai", False, False)))
        self.assertEqual(self.settle(s, aid)["decision"].check_of, aid)
        pending = lambda: self.tx(s, lambda tx: tx.one("SELECT salescall.unclear_check_pending(%s) AS p", (aid,)))["p"]   # noqa: E731
        self.assertTrue(pending())
        self.assertEqual(self.tx(s, lambda tx: gate.pacing(tx, other)).binding, "related_decision_pending")
        self.tx(s, lambda tx: exclusions.mark(tx, [s["task"]], "exclude", reason="試験：契約者の判断"), OPERATOR)
        self.assertTrue(pending())                    # 「かけない」なら、確かめのかけ直し（かけ直しの例外）はかかる：待つ
        self.tx(s, lambda tx: exclusions.mark(tx, [s["task"]], "force", reason="試験：契約者の判断"), OPERATOR)
        self.assertFalse(pending())                   # 強制かけない：もうかからない
        self.assertNotEqual(self.tx(s, lambda tx: gate.pacing(tx, other)).binding, "related_decision_pending")
        released = self.tx(s, lambda tx: tx.one("SELECT count(*) AS n FROM salescall.result_releases WHERE task_id = %s",
                                                (s["task"],)))["n"]
        self.assertEqual(released, 1)                 # 結果の版を確かめ直して確定した（行が終わったときと同じ）

    def test_an_unrung_call_settled_after_another_lines_claim_was_dropped_keeps_the_plan(self):
        """鳴らなかった電話（番号のせいか回線のせいか、まだ分からない）を、ほかの回線の予約で待たずに締めても、その予約がかける前に取り消され
        ていたら、行の予定は書き直さない（いまの予定のまま。⛔ 見直し26a の 3：あとの発信が無いので、出なかった電話として再発信の決まりで
        予定を1時間後へ書き直した）。"""
        s = self.ready()
        line_b = self.w.line(s["tenant"])
        self.lease(s, line=line_b)
        c = self.claim(s)
        dial_exit.dial(self.w.a, SYSTEM, s["tenant"], s["client"], c.attempt_id, FakeDriver(fail=dial_exit.CallNotPlaced("試験")))
        self.tx(s, lambda tx: health.after_call(tx, c.attempt_id))
        self.assertEqual(self.tx(s, lambda tx: health.blame_of(tx, c.attempt_id)), "pending")
        b = self.claim(s, line=line_b)                # ほかの回線の門が通す（表の R-4：待たずに締めてよい）
        self.assertIsInstance(b, gate.Claim, b)
        before = self.tx(s, lambda tx: next_action.current(tx, s["task"]))
        self.tx(s, lambda tx: attempts.release(tx, [b.attempt_id]))   # かける前に取り消された（PC が答えなかった など）
        self.tx(s, lambda tx: settle.settle(tx, c.attempt_id))
        self.assertEqual(self.tx(s, lambda tx: next_action.current(tx, s["task"]))["seq"], before["seq"])   # 予定は、いまのまま


if __name__ == "__main__":
    unittest.main()
