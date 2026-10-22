"""発信の門と出口の試験（DESIGN.md 5章）：すべて通ったときだけ予約し、1つでも外れたら理由の名前つきで止まる。
出口は予約証が「予約のまま・期限内」のときだけ回線に電話をかけさせる。わざと壊した入力でも止まることを確かめる。

⚠️ 試験の時刻に左右されないよう、試験のあいだだけ時間帯の外枠を「終日」にし、終わったら元の値を足し直す（追記だけの設定）。
   祝日の暦は架空のもの（試験のデータベースだけ）。番号は架空（局番のあとが 0000）。
"""

from __future__ import annotations

import json
import threading
import time
import unittest
import uuid
from datetime import date, timedelta
from unittest import mock

from salescall.core import (approval, attempts, business_rules, calendar, call_report, dial_exit, dnc, fix_requests, gate,
                            next_action, quality_stage, recordings, scheduler, settings, settle, stops, target_policy, tasks)
from salescall.core.call_report import Cut
from salescall.core.access import Rejected
from salescall.core.calendar import CalendarOutOfRange, CalendarRejected
from salescall.core.phone import normalize
from tests import support
from tests.support import ADMIN, CLEAN, OPERATOR, SYSTEM, World, fictional_jp_fixed, make_report, owner_access, rules_raw

KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun", "holiday")
ALL_DAY = {k: [["00:00", "24:00"]] for k in KEYS}
NO_NG = {k: [] for k in KEYS}
HOLDER = "試験の見守り役"


def calendar_csv(years, holidays=()) -> bytes:
    lines = ["国民の祝日・休日月日,国民の祝日・休日名称"]
    days = {f"{y}/1/1": "試験の休み" for y in years}
    days.update({f"{d.year}/{d.month}/{d.day}": "試験の休み（今日）" for d in holidays})
    lines += [f"{k},{v}" for k, v in days.items()]
    return ("\r\n".join(lines) + "\r\n").encode("cp932")


def local_today():
    return owner_access().in_system(SYSTEM, lambda tx: tx.one("SELECT salescall.jst(now())::date AS d"))["d"]


def load_calendar(holidays=()):
    y = local_today().year
    owner_access().in_system(OPERATOR, lambda tx: calendar.load_official_csv(tx, calendar_csv(range(y - 1, y + 2), holidays)))


class Base(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.override("calling.hours_limit", ALL_DAY)
        self.override("calling.ng_hours", NO_NG)        # 試験の時刻（昼休みなど）に左右されない。NG時間は専用の試験で確かめる
        load_calendar()

    # ── 試験のあいだだけ、システムの値を変える（終わったら、元の行の写しを足して戻す）
    def override(self, key, value):
        own = owner_access()
        orig = own.in_system(SYSTEM, lambda tx: tx.one(
            "SELECT value, status, rationale, review_when, set_by FROM salescall.setting_values"
            " WHERE key = %s AND scope = 'system' ORDER BY value_id DESC LIMIT 1", (key,)))
        put = lambda v, st, ra, rw, by: own.in_system(SYSTEM, lambda tx: tx.execute(
            "INSERT INTO salescall.setting_values (key, scope, value, status, rationale, review_when, set_by)"
            " VALUES (%s, 'system', %s, %s, %s, %s, %s)", (key, json.dumps(v), st, ra, rw, by)))
        put(value, "provisional", "試験の一時的な値", "試験のあと戻す", "試験")
        self.addCleanup(put, orig["value"], orig["status"], orig["rationale"], orig["review_when"], orig["set_by"])

    def put_client(self, s, key, value):
        self.tx(s, lambda tx: tx.execute(
            "INSERT INTO salescall.setting_values (key, scope, tenant_id, client_id, value, status, rationale, review_when,"
            " set_by) VALUES (%s, 'client', %s, %s, %s, 'decided', '試験', '試験', '試験')",
            (key, s["tenant"], s["client"], json.dumps(value))), OPERATOR)

    def tx(self, s, work, actor=SYSTEM):
        return self.w.a.in_client(actor, s["tenant"], s["client"], work)

    def ready(self, *, tenant=None, phone=None, rules=None):
        """かけてよい状態の行を1つ作る（時間帯は終日・予定はいま・最初の段は全部・回線は借りている）。"""
        raw = phone or fictional_jp_fixed()
        s = self.w.setup(phone=normalize(raw).e164, tenant=tenant)
        self.put_client(s, "calling.hours", ALL_DAY)
        self.tx(s, lambda tx: next_action.schedule_now(tx, s["task"], cause="試験"))
        self.tx(s, lambda tx: quality_stage.open_stage(tx, s["version"], 1, None, evidence="試験"), OPERATOR)
        self.tx(s, lambda tx: business_rules.save(tx, s["version"], rules or rules_raw()), OPERATOR)
        self.lease(s)
        return s

    def lease(self, s, line=None, holder=HOLDER, seconds=600):
        self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: tx.execute(
            "UPDATE salescall.lines SET lease_holder = %s, lease_expires_at = now() + %s WHERE line_id = %s",
            (holder, timedelta(seconds=seconds), line or s["line"])))

    def claim(self, s, line=None, holder=HOLDER, task=None):
        return self.tx(s, lambda tx: gate.claim(tx, task_id=task or s["task"], line_id=line or s["line"], lease_holder=holder))

    def reason(self, s, **kw):
        got = self.claim(s, **kw)
        self.assertIsInstance(got, gate.Denied, got)
        return got.reason

    def past_attempt(self, s, path, *, verify=None, line=None, side=None, report=None, settled=True):
        """前の試行を1つ作って、path の状態まで進め、本物の締めの処理で締める（お客様が出た通話には会話の報告を置く）。
        そのあと、門の守りそのものを試すため、次の予定を「いま」に書き直す（次の動きが誤った場合と同じ）。"""
        aid = self.w.insert_claim(s, line=line)
        for state in path:
            self.w.move(s, aid, state, side if state == "dropped" else None)
        if verify:
            self.tx(s, lambda tx: attempts.record_verification(tx, aid, summary=CLEAN))
        if "answered" in path:
            rep = report or make_report()
            self.tx(s, lambda tx: call_report.save(tx, aid, rep))
        if settled and "dialing" in path:     # 予約だけで取り消した試行は、締める対象ではない
            self.tx(s, lambda tx: settle.settle(tx, aid))
            self.tx(s, lambda tx: next_action.schedule_now(tx, s["task"], cause="試験：次の予定を、いまに書き直した"))
        return aid

    def state(self, s, aid):
        return self.tx(s, lambda tx: tx.one("SELECT state FROM salescall.attempts WHERE attempt_id = %s", (aid,)))["state"]


class FakeDriver:
    def __init__(self, fail=None, busy=False):
        self.calls, self.fail, self.busy = [], fail, busy

    def in_call(self):
        return self.busy

    def start_call(self, phone):
        self.calls.append(phone)
        if self.fail:
            raise self.fail

    def apologize_and_hang_up(self):
        self.apologized = getattr(self, "apologized", 0) + 1

    def prepare_call(self, voice_type):
        self.voice_type = voice_type


class ClaimAndDial(Base):
    def test_claim_then_dial_once(self):
        s = self.ready()
        c = self.claim(s)
        self.assertIsInstance(c, gate.Claim, c)
        self.assertEqual((c.task_id, c.phone_e164, c.version_id), (s["task"], s["phone"], s["version"]))
        self.assertEqual(self.state(s, c.attempt_id), "claimed")
        d = FakeDriver()
        self.assertTrue(dial_exit.dial(self.w.a, SYSTEM, s["tenant"], s["client"], c.attempt_id, d))
        self.assertFalse(dial_exit.dial(self.w.a, SYSTEM, s["tenant"], s["client"], c.attempt_id, d))  # 予約証は1回だけ
        self.assertEqual((d.calls, self.state(s, c.attempt_id)), ([s["phone"]], "dialing"))

    def test_claim_lifetime_comes_from_the_database_clock(self):
        s = self.ready()
        c = self.claim(s)
        span = self.tx(s, lambda tx: tx.one("SELECT claim_expires_at - claimed_at AS d FROM salescall.attempts"
                                            " WHERE attempt_id = %s", (c.attempt_id,)))["d"]
        self.assertEqual(span, timedelta(seconds=30))

    def test_denials_are_recorded_with_their_name(self):
        s = self.ready()
        self.tx(s, lambda tx: next_action.end(tx, s["task"], "試験", cause="試験"))
        self.assertEqual(self.reason(s), "task_ended")
        rows = self.tx(s, lambda tx: tx.all("SELECT reason, line_id FROM salescall.gate_denials WHERE task_id = %s",
                                            (s["task"],)))
        self.assertEqual(rows, [{"reason": "task_ended", "line_id": s["line"]}])

    def test_the_same_denial_is_recorded_once_until_the_reason_changes(self):
        """同じ理由で何度も門へ渡し直しても、記録は増やさない。理由が変われば足す（⛔ 2026-10-22 の見直し：同時の上限のあいだ、
        数秒ごとに1行ずつ増え続けた）。"""
        s = self.ready()
        t2 = self.w.task(s["tenant"], s["client"], self.w.run(s["tenant"], s["client"], s["business"], s["version"]),
                         s["contact"])
        for _ in range(3):
            self.assertEqual(self.reason(s, task=t2), "no_schedule")
        self.tx(s, lambda tx: next_action.end(tx, t2, "試験", cause="試験"))
        self.assertEqual(self.reason(s, task=t2), "task_ended")
        rows = self.tx(s, lambda tx: [r["reason"] for r in tx.all(
            "SELECT reason FROM salescall.gate_denials WHERE task_id = %s ORDER BY denial_id", (t2,))])
        self.assertEqual(rows, ["no_schedule", "task_ended"])

    def test_every_reason_has_a_kind(self):
        self.assertTrue(all(kind in ("permanent", "wait", "cannot_judge") for _, kind in gate.REASONS.values()))


class Schedule(Base):
    def test_no_schedule_and_not_due(self):
        s = self.ready()
        t2 = self.w.task(s["tenant"], s["client"], self.w.run(s["tenant"], s["client"], s["business"], s["version"]),
                         s["contact"])
        self.assertEqual(self.reason(s, task=t2), "no_schedule")
        self.tx(s, lambda tx: support.schedule_call(tx, s["task"], tx.one("SELECT now() + interval '1 hour' AS t")["t"],
                                                        contact_id=s["contact"], cause="試験", promised=False))
        self.assertEqual(self.reason(s), "not_due")


class DataReasons(Base):
    def test_dnc_and_number_kinds(self):
        s = self.ready()
        self.tx(s, lambda tx: dnc.register(tx, s["phone"], "manual"))
        self.assertEqual(self.reason(s), "dnc_client")
        navi = self.ready(phone="0570-000-001")
        self.assertEqual(self.reason(navi), "number_kind_not_allowed")

    def test_max_attempts_counts_only_dialed(self):
        s = self.ready()
        self.put_client(s, "retry.max_attempts_per_task", 1)
        self.past_attempt(s, ["released"])                              # 予約だけで戻ったものは数えない
        self.assertIsInstance(self.claim(s), gate.Claim)
        s2 = self.ready()
        self.put_client(s2, "retry.max_attempts_per_task", 1)
        self.past_attempt(s2, ["dialing", "no_answer"])
        self.assertEqual(self.reason(s2), "max_attempts")


class VersionAndStage(Base):
    def test_no_version(self):
        s = self.ready()
        run = uuid.uuid4()
        self.tx(s, lambda tx: tx.execute(
            "INSERT INTO salescall.runs (tenant_id, client_id, business_id, run_id, name, created_by)"
            " VALUES (%s, %s, %s, %s, '版の無い実施回', '試験')", (s["tenant"], s["client"], s["business"], run)), OPERATOR)
        t = self.w.task(s["tenant"], s["client"], run, s["contact"])
        self.tx(s, lambda tx: next_action.schedule_now(tx, t, cause="試験"))
        self.assertEqual(self.reason(s, task=t), "no_version")
        notes = self.tx(s, lambda tx: tx.all("SELECT summary FROM salescall.notification_events WHERE kind = 'setting_missing'"
                                             " AND refs ->> 'reason' = 'no_version'"))
        self.assertEqual([n["summary"] for n in notes], [gate.SETTING_GAPS["no_version"]])   # 黙って待たせない（2026-10-17）

    def test_version_without_stage_cannot_dial(self):
        s = self.ready()
        v2 = self.w.add_version(s["tenant"], s["client"], s["business"], 2)
        self.tx(s, lambda tx: tx.execute(
            "INSERT INTO salescall.run_versions (tenant_id, client_id, run_id, business_id, seq, version_id, assigned_by, reason)"
            " VALUES (%s, %s, %s, %s, 2, %s, '試験', '新しい版')", (s["tenant"], s["client"], s["run"], s["business"], v2)),
            OPERATOR)
        self.tx(s, lambda tx: tx.execute(
            "INSERT INTO salescall.questions (tenant_id, client_id, version_id, question_id, position, type_name, required)"
            " VALUES (%s, %s, %s, 'Q1', 1, 'datetime', true)", (s["tenant"], s["client"], v2)), OPERATOR)
        self.tx(s, lambda tx: business_rules.save(tx, v2, rules_raw()), OPERATOR)   # 決まりは揃っている。段だけが無い
        self.assertEqual(self.reason(s), "version_not_ready")

    def test_stage_cap_counts_answered_and_active(self):
        """段の件数は検証モードだけ（2026-10-16 ユーザー）。"""
        s = self.ready()
        self.put_client(s, "approval.human", True)
        self.tx(s, lambda tx: approval.approve_task(tx, s["task"]), OPERATOR)
        self.tx(s, lambda tx: quality_stage.open_stage(tx, s["version"], 1, 1, evidence="試験：最初の段は1件"), OPERATOR)
        phone2 = normalize(fictional_jp_fixed()).e164
        k2 = self.w.contact(s["tenant"], s["client"], phone2)
        other = dict(s, task=self.w.task(s["tenant"], s["client"], s["run"], k2), contact=k2, phone=phone2)
        self.past_attempt(other, ["dialing", "answered", "ending", "ended"], settled=False)   # 別の行が会話に至った（締めはまだ）
        self.assertEqual(self.reason(s), "stage_full")


class Stops(Base):
    def test_each_scope_stops_and_the_line_scope_only_its_line(self):
        s = self.ready()
        for kw in ({"scope": "client", "client_id": s["client"]},
                   {"scope": "business", "client_id": s["client"], "business_id": s["business"]}):
            with self.subTest(scope=kw["scope"]):
                e = self.tx(s, lambda tx: stops.stop(tx, cause="budget", reason="試験", tenant_id=s["tenant"], **kw))
                self.assertEqual(self.reason(s), "stopped")
                self.tx(s, lambda tx: stops.resume(tx, e["event_id"], reason="試験"))
        e = self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: stops.stop(
            tx, scope="line", cause="line_failure", reason="試験", tenant_id=s["tenant"], line_id=s["line"]))
        self.assertEqual(self.reason(s), "line_stopped")       # 止まっているのが回線だけ：回線の理由（行の予定は書き直さない）
        self.assertIn("line_stopped", gate.LINE_REASONS)
        b = self.tx(s, lambda tx: stops.stop(tx, scope="client", cause="budget", reason="試験", tenant_id=s["tenant"],
                                               client_id=s["client"]))
        self.assertEqual(self.reason(s), "stopped")            # 回線のほかにも止まっていれば、ふつうの「止まっている」
        self.tx(s, lambda tx: stops.resume(tx, b["event_id"], reason="試験"))
        other_line = self.w.line(s["tenant"])
        self.lease(s, line=other_line)
        self.assertIsInstance(self.claim(s, line=other_line), gate.Claim)   # ほかの回線は止まっていない
        self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: stops.resume(tx, e["event_id"], reason="試験"))

    def test_a_deficit_stop_still_lets_promises_and_resumes_through_but_a_quality_stop_does_not(self):
        """止めているあいだのお客様との約束を果たす電話（2026-10-22 ユーザー：赤字では、約束の電話・途中で切れたお客様へのお詫びの
        かけ直し・お詫びの電話の3つだけはかける。緊急停止・回線や会話AIの異常・品質では、かけない）。決まりは停止の原因の表の1か所
        （stops.CAUSES の lets_through）と、電話の種類のデータベースの1か所（salescall.keeping_kind）。候補を出す処理も同じ決まり。"""
        cases = [("normal", False, False), ("normal", True, True), ("resume", False, True)]   # (目的, 約束か, 赤字で通るか)
        for purpose, promised, passes in cases:
            with self.subTest(purpose=purpose, promised=promised):
                s = self.ready()
                now = self.tx(s, lambda tx: tx.one("SELECT now() AS t")["t"])
                self.tx(s, lambda tx: support.schedule_call(tx, s["task"], now, contact_id=s["contact"], cause="試験",
                                                               promised=promised, purpose=purpose))
                d = self.w.a.in_client(SYSTEM, s["tenant"], s["client"], lambda tx: stops.stop(
                    tx, scope="client", cause="deficit", reason="試験：赤字", tenant_id=s["tenant"], client_id=s["client"]))
                listed = self.tx(s, lambda tx: [r["task_id"] for r in scheduler.due_tasks(tx, 10)])
                self.assertEqual(listed, [s["task"]] if passes else [])   # 候補を出す処理も同じ決まり
                if passes:
                    got = self.claim(s)
                    self.assertIsInstance(got, gate.Claim, got)
                    self.tx(s, lambda tx: attempts.release(tx, [got.attempt_id]))
                else:
                    self.assertEqual(self.reason(s), "stopped")
                q = self.tx(s, lambda tx: stops.stop(tx, scope="business", cause="forbidden_speech", reason="試験：品質",
                                                     tenant_id=s["tenant"], client_id=s["client"], business_id=s["business"]))
                self.assertEqual(self.reason(s), "stopped")              # 品質の停止：約束も続きも止める（お詫びの電話だけ通す）
                self.tx(s, lambda tx: stops.resume(tx, q["event_id"], reason="試験"))
                self.tx(s, lambda tx: stops.resume(tx, d["event_id"], reason="試験"), OPERATOR)

    def test_the_kinds_of_promise_keeping_calls_are_decided_in_one_place(self):
        """約束を果たす電話の種類は、データベースの salescall.keeping_kind の1か所。「新しくかける電話」（is_new_call）はその裏返しで、
        名前は stops.KEEPING_KINDS とそろう。"""
        s = self.ready()
        rows = self.tx(s, lambda tx: tx.all(
            "SELECT p, q, salescall.keeping_kind(p, q) AS k, salescall.is_new_call(p, q) AS n"
            " FROM unnest(ARRAY['normal','resume','apology','ask_number_again']) p, unnest(ARRAY[false, true]) q"))
        for r in rows:
            self.assertEqual(r["n"], r["k"] is None, r)
        self.assertEqual({r["k"] for r in rows} - {None}, set(stops.KEEPING_KINDS))
        self.assertEqual(stops.CAUSES["deficit"].lets_through, frozenset(stops.KEEPING_KINDS))
        for c in quality_stage.STOP_CAUSES:
            self.assertEqual(stops.CAUSES[c].lets_through, frozenset({"apology"}))
        for c in ("manual", "line_failure", "ai_connection"):
            self.assertEqual(stops.CAUSES[c].lets_through, frozenset())

    def test_line_stop_releases_claims_on_that_line(self):
        s = self.ready()
        c = self.claim(s)
        self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: stops.stop(
            tx, scope="line", cause="line_failure", reason="試験", tenant_id=s["tenant"], line_id=s["line"]))
        d = FakeDriver()
        self.assertFalse(dial_exit.dial(self.w.a, SYSTEM, s["tenant"], s["client"], c.attempt_id, d))
        self.assertEqual((d.calls, self.state(s, c.attempt_id)), ([], "released"))


class Hours(Base):
    def test_outside_hours(self):
        s = self.ready()
        self.put_client(s, "calling.hours", {k: [] for k in KEYS})
        self.assertEqual(self.reason(s), "outside_hours")

    def test_ng_hours_are_not_called(self):
        """NG時間（2026-10-15 ユーザー）：かけてよい時間帯の内でも、依頼者・業務ごとのNG時間にはかけない。"""
        s = self.ready()
        self.put_client(s, "calling.ng_hours", ALL_DAY)                 # 1日じゅうNG
        self.assertEqual(self.reason(s), "outside_hours")
        s2 = self.ready()
        self.put_client(s2, "calling.ng_hours", NO_NG)
        self.assertIsInstance(self.claim(s2), gate.Claim)
        hours = self.tx(s2, lambda tx: gate.calling_hours_for(tx, tenant_id=s2["tenant"], client_id=s2["client"],
                                                              business_id=s2["business"]))
        self.assertEqual(hours["mon"], [["00:00", "24:00"]])

    def test_callee_named_time_may_fall_in_ng_hours(self):
        """お客様が自分で指定した時刻なら、NG時間でもかける。その日のその枠の中だけ（2026-10-15 ユーザー）。"""
        s = self.ready()
        self.put_client(s, "calling.ng_hours", ALL_DAY)                     # いまはNG時間
        now = self.tx(s, lambda tx: tx.one("SELECT now() AS t, salescall.jst(now()) AS local"))
        if now["local"].hour == 23 and now["local"].minute == 59:
            self.skipTest("日付の変わり目（予約の期限が翌日の枠にかかる）")
        sched = lambda callee_at: self.tx(s, lambda tx: support.schedule_call(
            tx, s["task"], now["t"], contact_id=s["contact"], cause="試験", promised=True, callee_at=callee_at))
        sched(None)
        self.assertEqual(self.reason(s), "outside_hours")                    # こちらからは控える
        sched(now["t"] - timedelta(days=1))
        self.assertEqual(self.reason(s), "outside_hours")                    # 前の日に言われた時刻：日がずれたら控える
        sched(now["t"])
        self.assertIsInstance(self.claim(s), gate.Claim)                    # お客様が指定した時刻

    def test_callee_time_only_on_promised_calls(self):
        s = self.ready()
        with self.assertRaises(Rejected) as e:
            self.tx(s, lambda tx: tx.execute(
                "INSERT INTO salescall.task_next_actions (tenant_id, client_id, task_id, seq, kind, call_at, cause, promised,"
                " contact_id, purpose, callee_at) VALUES (%s, %s, %s, 99, 'call', now(), '試験', false, %s, 'normal', now())",
                (s["tenant"], s["client"], s["task"], s["contact"])))
        self.assertEqual(e.exception.constraint, "task_next_actions_callee_at")

    def test_default_ng_hours_is_lunch_time(self):
        own = owner_access()
        v = own.in_system(SYSTEM, lambda tx: tx.one(
            "SELECT value FROM salescall.setting_values WHERE key = 'calling.ng_hours' AND scope = 'system'"
            " AND status = 'decided' ORDER BY value_id LIMIT 1"))["value"]
        self.assertEqual({k: v[k] for k in KEYS}, {k: [["12:00", "13:00"]] for k in KEYS})

    def test_holiday_uses_the_holiday_hours(self):
        s = self.ready()
        self.put_client(s, "calling.hours", dict(ALL_DAY, holiday=[]))
        self.assertIsInstance(self.claim(s), gate.Claim)
        s2 = self.ready()
        self.put_client(s2, "calling.hours", dict(ALL_DAY, holiday=[]))
        load_calendar(holidays=[local_today()])
        self.addCleanup(load_calendar)
        self.assertEqual(self.reason(s2), "outside_hours")

    def test_unknown_calendar_is_not_guessed(self):
        s = self.ready()

        def no_calendar(tx):
            def is_holiday(d):
                raise CalendarOutOfRange("試験")
            return is_holiday
        with mock.patch.object(gate, "holiday_checker", no_calendar):
            self.assertEqual(self.reason(s), "calendar_out_of_range")

    def test_missing_setting_is_not_guessed(self):
        s = self.ready()
        with mock.patch.object(gate.settings, "resolve", side_effect=settings.SettingMissing("試験")):
            self.assertEqual(self.reason(s), "setting_missing")


class Pacing(Base):
    def test_unsettled_attempt_blocks_the_row(self):
        s = self.ready()
        self.past_attempt(s, ["dialing", "answered", "ending", "ended"], settled=False)   # 検証待ち＝まだ締められない
        self.assertEqual(self.reason(s), "awaiting_decision")

    def test_unsettled_call_blocks_the_contact_in_other_rows(self):
        s = self.ready()
        other_run = self.w.run(s["tenant"], s["client"], s["business"], s["version"])
        other = dict(s, task=self.w.task(s["tenant"], s["client"], other_run, s["contact"]), run=other_run)
        self.past_attempt(other, ["dialing", "answered", "ending", "ended"], settled=False)
        self.assertEqual(self.reason(s), "related_unsettled")
        self.assertIsNone(self.tx(s, lambda tx: gate.pacing(tx, s["task"])).earliest)

    def test_same_contact_interval(self):
        s = self.ready()
        self.past_attempt(s, ["dialing", "no_answer"])
        self.assertEqual(self.reason(s), "same_contact_interval")
        p = self.tx(s, lambda tx: gate.pacing(tx, s["task"]))
        self.assertEqual((p.remaining, p.binding), (2, "same_contact_interval"))

    def test_a_call_cut_by_our_failure_is_like_a_line_drop(self):
        """こちらの仕組みが通話の途中で落ちた（結果不明）は、お客様が切ったのではない：回線が途中で切れた通話と同じく、回数・間隔に
        数えない（2026-10-17 ユーザー。前は1時間あけていた）。次は理由ごとの再発信の方針で決まる。"""
        s = self.ready()
        self.past_attempt(s, ["dialing", "answered", "technical_error"], verify="done")
        self.assertEqual(self.tx(s, lambda tx: gate.pacing(tx, s["task"])).remaining, 3)
        self.assertIsInstance(self.claim(s), gate.Claim)

    def test_unknown_result_before_answer_needs_no_long_wait(self):
        """お客様が出る前の結果不明は、誰とも話していない：検証も長い間隔も要らない（通信の問題で繋がらなかった扱い）。"""
        s = self.ready()
        self.past_attempt(s, ["dialing", "technical_error"])
        self.assertIsInstance(self.claim(s), gate.Claim)

    def test_daily_attempts_count_across_runs(self):
        s = self.ready()
        self.override("gate.same_contact_min_interval", 1)
        self.put_client(s, "gate.max_dials_per_contact_per_day", 1)
        self.past_attempt(s, ["dialing", "busy"])
        time.sleep(1.1)
        self.assertEqual(self.reason(s), "daily_attempts")

    def test_cross_client_interval_tells_same_or_other_tenant_only(self):
        a = self.ready()
        b = self.ready(tenant=a["tenant"], phone=a["phone"])
        other = self.ready(phone=a["phone"])
        self.past_attempt(b, ["dialing", "no_answer"])
        self.assertEqual(self.reason(a), "cross_client_interval_same_tenant")
        self.assertEqual(self.reason(other), "cross_client_interval_other_tenant")   # 契約者の外は、中身を出さない

    def test_the_interval_with_other_contractors_is_the_contractors_setting(self):
        """ほかの契約者が同じ番号へかけたときの間隔は、契約者の設定（○○時間・○○日・かけない。2026-10-16 ユーザー）。"""
        a = self.ready()
        other = self.ready(phone=a["phone"])
        self.past_attempt(a, ["dialing", "no_answer"])
        put_tenant = lambda v: self.w.a.in_tenant(OPERATOR, other["tenant"], lambda tx: settings.set_value(
            tx, "gate.other_contractor_interval", v, scope="tenant", status="decided", rationale="試験", review_when="試験",
            tenant_id=other["tenant"]))
        put_tenant({"hours": 1})
        p = self.tx(other, lambda tx: gate.pacing(tx, other["task"]))
        self.assertEqual(p.binding, "cross_client_interval_other_tenant")
        self.assertLess(p.earliest - self.tx(other, lambda tx: tx.one("SELECT now() AS t")["t"]), timedelta(hours=1, seconds=5))
        put_tenant("never")
        self.assertEqual(self.reason(other), "other_contractor_called")   # かけない：その行は終える側（permanent）
        with self.assertRaises(Rejected):
            put_tenant({"weeks": 1})                                     # 形の違う値は、データベースが拒む

    def test_a_promised_callback_is_not_moved_by_another_clients_call(self):
        """お客様と約束したかけ直しは、ほかの依頼者が同じ番号へかけたあとでも止めない（お客様が頼んだ時刻を守る。⛔ 2026-10-22 の
        見直し：前は約束の時刻を止めて、依頼者をまたいだ間隔＝約3日ずらした）。"""
        a = self.ready()
        b = self.ready(tenant=a["tenant"], phone=a["phone"])
        self.past_attempt(b, ["dialing", "no_answer"])
        self.assertEqual(self.reason(a), "cross_client_interval_same_tenant")   # ふつうの予定なら止める
        self.tx(a, lambda tx: support.schedule_call(tx, a["task"], tx.one("SELECT now() AS t")["t"],
                                                        contact_id=a["contact"], cause="お客様に頼まれた折り返し", promised=True))
        self.assertIsInstance(self.claim(a), gate.Claim)

    def _choose(self, s, value):
        self.w.a.in_tenant(OPERATOR, s["tenant"], lambda tx: settings.set_value(
            tx, "gate.before_other_promise", value, scope="tenant", status="decided", rationale="試験", review_when="試験",
            tenant_id=s["tenant"]))

    def _promise(self, s, *, minutes, purpose="normal"):
        at = self.tx(s, lambda tx: tx.one("SELECT now() + make_interval(mins => %s) AS t", (minutes,))["t"])
        self.tx(s, lambda tx: support.schedule_call(tx, s["task"], at, contact_id=s["contact"], cause="お客様に頼まれた折り返し",
                                                        promised=True, purpose=purpose))
        return at

    def test_before_another_clients_promise_the_contractor_chooses_to_call_wait_or_not_call(self):
        """ほかの依頼者がお客様と約束している番号へ、約束でない電話をかけるか（2026-10-22 ユーザー：「基本は声を変え、契約者ごとに
        設定で選べるといいですね」「『約束のあとまで待つ』に加え、『同じ番号には送らない』も選べた方がいいんじゃない」）。基本は
        かける（声は choose_voice が替える）。「待つ」は約束が済むまで待ち、「かけない」はその行を終える側。"""
        a = self.ready()
        b = self.ready(tenant=a["tenant"], phone=a["phone"])
        promise = self._promise(a, minutes=120)
        self.assertIsInstance(self.claim(b), gate.Claim)                       # 基本：かける
        self.tx(b, lambda tx: attempts.release(tx, [tx.one("SELECT attempt_id FROM salescall.attempts WHERE task_id = %s",
                                                           (b["task"],))["attempt_id"]]))
        self._choose(b, "wait")
        self.assertEqual(self.reason(b), "other_client_promise")             # 契約者が「待つ」を選んだ
        p = self.tx(b, lambda tx: gate.pacing(tx, b["task"]))
        self.assertEqual((p.binding, p.earliest), ("other_client_promise", promise))   # 同じ契約者の約束なら、その時刻まで
        self.assertNotEqual(self.tx(a, lambda tx: gate.pacing(tx, a["task"])).binding, "other_client_promise")   # 自分の約束では待たない
        self._choose(b, "never")
        self.assertEqual(self.reason(b), "other_client_promise_never")       # 「同じ番号にはかけない」：その行は終える側
        self.assertEqual(gate.REASONS["other_client_promise_never"][1], "permanent")

    def test_a_promise_not_yet_called_keeps_others_waiting_even_after_its_time(self):
        """約束の時刻を過ぎても、その約束の電話がまだかかっていなければ待つ（⛔ 2026-10-22 の見直し13：前は時刻を1秒でも過ぎると
        待ちが外れ、こちらの営業の電話が約束の電話より先にお客様に届きえた）。見直すのは決まった間（gate.denied_retry_wait）のあと。"""
        a = self.ready()
        b = self.ready(tenant=a["tenant"], phone=a["phone"])
        self._promise(a, minutes=-1)                                            # 1分前の約束が、まだかかっていない
        self._choose(b, "wait")
        p = self.tx(b, lambda tx: gate.pacing(tx, b["task"]))
        now = self.tx(b, lambda tx: tx.one("SELECT now() AS t")["t"])
        self.assertEqual(p.binding, "other_client_promise")
        self.assertGreater(p.earliest, now)

    def test_the_choice_never_stops_the_rows_own_promises_apologies_or_imports(self):
        """「かけない」「待つ」は、約束でない電話だけ。その行自身の約束のかけ直し・お詫びの電話は止めない（お客様が頼んだ電話・
        お詫びは届ける）。取込でも判断しない（約束は時間で済むので、取り込むときの事実で行を止めない。⛔ 2026-10-22 の見直し13：
        前は約束の電話・お詫びまで止めて行を終え、取込でも止めた）。"""
        a = self.ready()
        b = self.ready(tenant=a["tenant"], phone=a["phone"])
        self._promise(a, minutes=120)
        self._choose(b, "never")
        self._promise(b, minutes=0)                                             # B 自身がお客様と約束したかけ直し
        c = self.claim(b)
        self.assertIsInstance(c, gate.Claim)
        self.tx(b, lambda tx: attempts.release(tx, [c.attempt_id]))
        self._promise(b, minutes=0, purpose="apology")                         # お詫びの電話
        self.assertIsInstance(self.claim(b), gate.Claim)
        blocked = self.tx(b, lambda tx: target_policy.block_reason(tx, normalize(b["phone"]), client_ref=None,
                                                                   tenant_id=b["tenant"], client_id=b["client"],
                                                                   business_id=b["business"]))
        self.assertIsNone(blocked)                                              # 取込（データで決まる理由）では止めない

    def test_another_contractors_promise_time_is_not_shown(self):
        """ほかの契約者の約束は、あることだけが分かり、時刻は渡らない（依頼者をまたいだ間隔の「契約者の外は中身を出さない」と同じ。
        ⛔ 2026-10-22 の見直し13：前は約束の時刻がそのまま次の予定の時刻になった）。決まった間のあとに見直す。"""
        a = self.ready()
        b = self.ready(phone=a["phone"])                                        # ほかの契約者
        promise = self._promise(a, minutes=317)
        self._choose(b, "wait")
        p = self.tx(b, lambda tx: gate.pacing(tx, b["task"]))
        rows = self.tx(b, lambda tx: tx.all("SELECT relation, wait_until FROM salescall.number_next_promise_elsewhere(%s)",
                                            (b["phone"],)))
        self.assertEqual(rows, [{"relation": "other_tenant", "wait_until": None}])
        self.assertEqual(p.binding, "other_client_promise")
        self.assertNotEqual(p.earliest, promise)

    def test_own_retries_are_not_cross_client(self):
        s = self.ready()
        self.override("gate.same_contact_min_interval", 1)
        self.past_attempt(s, ["dialing", "no_answer"])
        time.sleep(1.1)
        self.assertIsInstance(self.claim(s), gate.Claim)   # 同じ依頼者の再発信に、依頼者をまたいだ間隔はかからない


class NotByCallee(Base):
    """2026-10-14 ユーザー決定：最小の間隔（5分）・1日の回数（2回）の対象外。
    お客様と約束したかけ直し／通信の問題などで繋がらなかった／お客様の意思と関係なく切れた発信。"""

    def test_failed_or_interrupted_calls_do_not_count(self):
        interrupted = make_report(cut=Cut("callee", False, False))
        line_cut = make_report(cut=Cut(None, False, False))
        for path, side, rep in ((["dialing", "call_failed"], None, None), (["dialing", "technical_error"], None, None),
                                (["dialing", "answered", "dropped"], "unknown", interrupted),   # お客様の言葉が途中で途切れた
                                (["dialing", "answered", "dropped"], "line", line_cut)):        # 回線の側で切れた
            with self.subTest(path=path, side=side):
                s = self.ready()
                self.put_client(s, "gate.max_dials_per_contact_per_day", 1)
                self.past_attempt(s, path, side=side, report=rep, verify="done" if rep else None)
                self.assertIsInstance(self.claim(s), gate.Claim)

    def test_normal_ends_still_count(self):
        s = self.ready()
        self.past_attempt(s, ["dialing", "answered", "dropped"], side="callee", verify="done")   # 締めのあとにお客様が切った
        self.assertEqual(self.reason(s), "same_contact_interval")

    def test_promised_callback_skips_interval_and_daily_limit(self):
        s = self.ready()
        self.put_client(s, "gate.max_dials_per_contact_per_day", 1)
        self.past_attempt(s, ["dialing", "no_answer"])
        self.assertIn(self.reason(s), ("same_contact_interval", "daily_attempts"))   # ふつうの予定なら、どちらも止める
        self.tx(s, lambda tx: support.schedule_call(tx, s["task"], tx.one("SELECT now() AS t")["t"],
                                                        contact_id=s["contact"], cause="お客様に頼まれた折り返し", promised=True))
        self.assertIsInstance(self.claim(s), gate.Claim)

    def test_one_way_of_counting_for_every_limit(self):
        """行の回数の上限（3回）も、1日の回数と同じ数え方（2026-10-14 ユーザー：統一）。"""
        s = self.ready()
        self.override("gate.same_contact_min_interval", 1)
        self.put_client(s, "retry.max_attempts_per_task", 1)
        self.past_attempt(s, ["dialing", "call_failed"])            # 繋がらなかった：数えない
        self.assertIsInstance(self.claim(s), gate.Claim)
        s2 = self.ready()
        self.put_client(s2, "retry.max_attempts_per_task", 1)
        self.past_attempt(s2, ["dialing", "no_answer"])             # 出なかった：数える
        self.assertEqual(self.reason(s2), "max_attempts")
        self.tx(s2, lambda tx: support.schedule_call(tx, s2["task"], tx.one("SELECT now() AS t")["t"],
                                                         contact_id=s2["contact"], cause="お客様に頼まれた折り返し", promised=True))
        self.assertIsInstance(self.claim(s2), gate.Claim)           # 約束したかけ直しは、回数の上限でも止めない

    def test_a_promised_callback_is_not_counted_afterwards(self):
        """約束したかけ直しは、かけたあとも、行の回数の上限・1日の回数に数えない（2026-10-14 ユーザー：「かけ直しの約束を相手側に
        した場合やかけ直しを依頼された場合は対象外」。2026-10-17 の計画書との突き合わせ：前は、かけたあと数えていたので、そのあと
        のふつうのかけ直しが減った）。最小の間隔は、約束したかけ直しも起点に数える（歯止め）。"""
        s = self.ready()
        self.override("gate.same_contact_min_interval", 1)
        self.put_client(s, "gate.max_dials_per_contact_per_day", 1)
        self.put_client(s, "retry.max_attempts_per_task", 1)
        now = lambda tx: tx.one("SELECT now() AS t")["t"]
        self.tx(s, lambda tx: support.schedule_call(tx, s["task"], now(tx), contact_id=s["contact"],
                                                        cause="お客様に頼まれた折り返し", promised=True))
        c = self.claim(s)
        self.assertIsInstance(c, gate.Claim)
        self.assertTrue(dial_exit.dial(self.w.a, SYSTEM, s["tenant"], s["client"], c.attempt_id, FakeDriver()))
        self.w.move(s, c.attempt_id, "no_answer")                    # 約束したかけ直しに出なかった
        self.tx(s, lambda tx: settle.settle(tx, c.attempt_id))
        self.assertTrue(self.tx(s, lambda tx: tx.one("SELECT promised FROM salescall.attempts WHERE attempt_id = %s",
                                                     (c.attempt_id,))["promised"]))
        self.tx(s, lambda tx: support.schedule_call(tx, s["task"], now(tx), contact_id=s["contact"], cause="ふつうの予定",
                                                        promised=False))
        time.sleep(1.1)
        self.assertIsInstance(self.claim(s), gate.Claim)           # 1日1回・行1回の上限でも、ふつうのかけ直しができる
        with self.assertRaises(Rejected):                           # 約束したかけ直しかは、あとから変えられない
            owner_access().in_client(OPERATOR, s["tenant"], s["client"], lambda tx: tx.execute(
                "UPDATE salescall.attempts SET promised = false WHERE attempt_id = %s", (c.attempt_id,)))

    def test_promised_callback_still_respects_other_rules(self):
        s = self.ready()
        self.tx(s, lambda tx: dnc.register(tx, s["phone"], "manual"))
        self.tx(s, lambda tx: support.schedule_call(tx, s["task"], tx.one("SELECT now() AS t")["t"],
                                                        contact_id=s["contact"], cause="約束", promised=True))
        self.assertEqual(self.reason(s), "dnc_client")


class Lines(Base):
    def test_line_must_be_leased_by_the_caller_beyond_the_claim(self):
        s = self.ready()
        self.assertEqual(self.reason(s, holder="ほかの見守り役"), "line_not_leased")
        self.lease(s, seconds=10)   # 予約証（30秒）より先に借用が切れる
        self.assertEqual(self.reason(s), "line_not_leased")
        self.assertEqual(self.reason(s, line=uuid.uuid4()), "line_unknown")

    def test_line_busy_and_daily_limit(self):
        s = self.ready()
        t = self.ready(tenant=s["tenant"])
        self.lease(t, line=s["line"])
        self.assertIsInstance(self.claim(s), gate.Claim)
        self.assertEqual(self.reason(t, line=s["line"]), "line_busy")
        u = self.ready(tenant=s["tenant"])
        self.past_attempt(u, ["dialing", "no_answer"], line=u["line"])
        v = self.ready(tenant=s["tenant"])
        self.put_client(v, "line.max_dials_per_day", 1)   # 1日の本数は依頼者ごと（2026-10-17 ユーザー）
        self.assertEqual(self.reason(v, line=u["line"]), "line_daily_limit")


class Capacity(Base):
    def test_client_concurrency(self):
        s = self.ready()
        self.put_client(s, "calling.max_concurrent_calls", 1)
        t2 = self.w.task(s["tenant"], s["client"], s["run"], self.w.contact(s["tenant"], s["client"],
                                                                           normalize(fictional_jp_fixed()).e164))
        self.tx(s, lambda tx: next_action.schedule_now(tx, t2, cause="試験"))
        self.assertIsInstance(self.claim(s), gate.Claim)
        line2 = self.w.line(s["tenant"])
        self.lease(s, line=line2)
        self.assertEqual(self.reason(s, task=t2, line=line2), "client_concurrency")

    def test_ai_limit_never_holds_a_call(self):
        """会話AIの1分の上限では、門は電話を待たせない（2026-10-17 ユーザー：「1分制限を超えそうになったら、きりのいいところで、
        次のキーと併用して進んだらいい」「作業を止めるな」）。上限の設定をどれだけ小さくしても、門は通す（キーの選択が併用する）。"""
        a = self.ready()
        self.override("ai.tpm_limit", 1)
        self.assertIsInstance(self.claim(a), gate.Claim)
        self.assertNotIn("ai_capacity", gate.REASONS)

class ActiveConflicts(Base):
    def test_same_contact_in_another_run(self):
        s = self.ready()
        run2 = self.w.run(s["tenant"], s["client"], s["business"], s["version"])
        t2 = self.w.task(s["tenant"], s["client"], run2, s["contact"])
        self.tx(s, lambda tx: next_action.schedule_now(tx, t2, cause="試験"))
        self.assertIsInstance(self.claim(s), gate.Claim)
        line2 = self.w.line(s["tenant"])
        self.lease(s, line=line2)
        # 予約中の発信も「断られたかがまだ決まっていない」ので、同じお客様の行は先にそこで止まる（2026-10-16 の見直し）。
        # データベースの部分一意（contact_active）は、その下の守りとして残る
        self.assertEqual(self.reason(s, task=t2, line=line2), "related_unsettled")

    def test_same_customer_is_protected_in_both_directions_and_while_reserved(self):
        """同じお客様の守りは両方向（2026-10-16 の見直し G3）。行 X の番号 {A, B（お客様が言った別の番号）}・行 Y の番号 {A}。
        Y が A を予約しているあいだ（まだ発信していない）、X は B へもかけない。X が B を予約しているあいだ、Y も A へかけない。"""
        x = self.ready()
        other = normalize(fictional_jp_fixed())
        first = self.past_attempt(x, ["dialing", "call_failed"])        # B を言われた通話の代わり（番号の出どころに要る。間隔に数えない発信）
        self.tx(x, lambda tx: tasks.add_callee_number(tx, x["task"], other, source_attempt_id=first))
        other = other.e164
        run2 = self.w.run(x["tenant"], x["client"], x["business"], x["version"])
        y = self.w.task(x["tenant"], x["client"], run2, x["contact"])
        self.tx(x, lambda tx: next_action.schedule_now(tx, y, cause="試験"))
        b = self.tx(x, lambda tx: tx.one("SELECT contact_id FROM salescall.task_numbers WHERE task_id = %s AND phone_e164 = %s",
                                         (x["task"], other)))["contact_id"]
        self.tx(x, lambda tx: support.schedule_call(tx, x["task"], tx.one("SELECT now() AS n")["n"], contact_id=b,
                                                        cause="試験", promised=False))
        line2 = self.w.line(x["tenant"])
        self.lease(x, line=line2)
        self.assertIsInstance(self.claim(x, task=y, line=line2), gate.Claim)          # Y が A を予約
        self.assertEqual(self.reason(x), "related_unsettled")                           # X は B へもかけない

    def test_an_expired_reservation_does_not_hold_the_same_customer(self):
        """期限の切れた予約（見守り役が落ちて取り消されないまま残ったもの）は、同じお客様のほかの行を待たせない（2026-10-17 の見直し）。"""
        self.override("gate.claim_lifetime", 1)
        x = self.ready()
        other = normalize(fictional_jp_fixed())
        first = self.past_attempt(x, ["dialing", "call_failed"])
        self.tx(x, lambda tx: tasks.add_callee_number(tx, x["task"], other, source_attempt_id=first))
        run2 = self.w.run(x["tenant"], x["client"], x["business"], x["version"])
        y = self.w.task(x["tenant"], x["client"], run2, x["contact"])
        self.tx(x, lambda tx: next_action.schedule_now(tx, y, cause="試験"))
        b = self.tx(x, lambda tx: tx.one("SELECT contact_id FROM salescall.task_numbers WHERE task_id = %s AND phone_e164 = %s",
                                         (x["task"], other.e164)))["contact_id"]
        self.tx(x, lambda tx: support.schedule_call(tx, x["task"], tx.one("SELECT now() AS n")["n"], contact_id=b,
                                                        cause="試験", promised=False))
        line2 = self.w.line(x["tenant"])
        self.lease(x, line=line2)
        self.assertIsInstance(self.claim(x, task=y, line=line2), gate.Claim)          # Y が A を予約して、そのまま期限切れ
        time.sleep(1.2)
        self.assertIsInstance(self.claim(x), gate.Claim)                                # X は B へかけられる

    def test_unknown_judgement_keeps_the_same_customer_protected(self):
        """判定できない（会話の報告の切れ方が無い）通話は、断られたかがまだ決まっていない。同じお客様の別の行は、
        判定し直すまでかけない（前は「締め済み」とみなして5分後にかけえた。2026-10-16 の見直し G2）。"""
        x = self.ready()
        run2 = self.w.run(x["tenant"], x["client"], x["business"], x["version"])
        y = self.w.task(x["tenant"], x["client"], run2, x["contact"])
        self.tx(x, lambda tx: next_action.schedule_now(tx, y, cause="試験"))
        no_cut = make_report(cut=None)
        self.past_attempt(x, ["dialing", "answered", "dropped"], side="callee", report=no_cut, verify="done")
        state = self.tx(x, lambda tx: tx.one("SELECT business_state FROM salescall.latest_task_results WHERE task_id = %s",
                                             (x["task"],)))["business_state"]
        self.assertEqual(state, "unknown")
        self.assertEqual(self.reason(x, task=y), "related_unsettled")

    def test_same_number_elsewhere_tells_only_the_relation(self):
        a = self.ready()
        b = self.ready(tenant=a["tenant"], phone=a["phone"])
        c = self.ready(phone=a["phone"])
        self.assertIsInstance(self.claim(a), gate.Claim)
        self.assertEqual(self.reason(b), "number_active_same_tenant")
        self.assertEqual(self.reason(c), "number_active_other_tenant")



class CallerNumberAndVoice(Base):
    """依頼者が替わったら、期間に関係なく発信番号と声を替える（2026-10-17 ユーザー決定）。依頼者ごとの発信番号が初めの形。
    検証モードでは発信番号は1つでよい。契約者が替われば発信番号は別（契約者の携帯）だが、声は替える。"""

    def dialed(self, s, line=None, path=("ringing", "answered", "ending", "ended")):
        """発信して、状態を path の順に進める（既定：お客様が出て話した）。"""
        c = self.claim(s, line=line)
        self.assertIsInstance(c, gate.Claim, c)
        self.assertTrue(dial_exit.dial(self.w.a, SYSTEM, s["tenant"], s["client"], c.attempt_id, FakeDriver()))
        for state in path:
            self.w.move(s, c.attempt_id, state)
        return c

    @staticmethod
    def voice(tx, s, business_voice):
        """門の声の決め方（choose_voice の1か所）を、その行・その版で呼ぶ。"""
        return gate.choose_voice(tx, {"task_id": s["task"], "phone_e164": s["phone"], "client_id": s["client"]},
                                 s["version"], business_voice, date.today())

    def test_another_client_or_contractor_gets_the_other_voice(self):
        self.override("gate.cross_client_interval", 1)
        a = self.ready()
        self.assertEqual(self.dialed(a).voice_type, "S2")                           # 業務の声の型（Sadachbia）
        time.sleep(1.1)
        b = self.ready(tenant=a["tenant"], phone=a["phone"])                        # 同じ契約者の、ほかの依頼者
        self.assertEqual(self.claim(b).voice_type, "L4")                            # 前が男性の声 ⇒ Leda
        c = self.ready(phone=a["phone"])                                            # ほかの契約者（中身は見えず、声の名前だけ）
        self.assertEqual(self.tx(c, lambda tx: self.voice(tx, c, "S4")), "L4")
        self.assertEqual(self.tx(c, lambda tx: self.voice(tx, c, "L4")), "L4")   # もう違う声

    def test_a_reservation_that_never_dialed_does_not_fix_the_voice(self):
        """発信しなかった予約の声は、お客様が聞いていないので引き継がない（2026-10-17 の見直し）。"""
        self.override("gate.cross_client_interval", 1)
        r = self.ready()
        c = self.claim(r)
        self.tx(r, lambda tx: attempts.release(tx, [c.attempt_id]))           # 予約だけで取り消された（S2）
        y = self.ready(tenant=r["tenant"], phone=r["phone"])
        self.dialed(y)                                                          # ほかの依頼者が Sadachbia でかけた
        time.sleep(1.1)
        self.assertEqual(self.tx(r, lambda tx: self.voice(tx, r, "S2")), "L4")

    def test_only_what_the_customer_saw_or_heard_counts(self):
        """声は、お客様が出た発信の声だけ（出なかった発信の声は聞いていない）。発信番号は、お客様の電話を鳴らした発信だけ
        （出なくても着信の履歴に番号が残る。発信に失敗した発信の番号は出ていない）。2026-10-17 の計画書との突き合わせ。"""
        self.override("gate.cross_client_interval", 1)
        a = self.ready()
        self.dialed(a, path=("ringing", "no_answer"))                              # 鳴ったが出なかった（S2）
        time.sleep(1.1)
        b = self.ready(tenant=a["tenant"], phone=a["phone"])
        self.assertEqual(self.tx(b, lambda tx: self.voice(tx, b, "S2")), "S2")   # 声は替えない
        self.lease(b, line=a["line"])
        self.assertEqual(self.reason(b, line=a["line"]), "line_used_by_other_client")   # 番号は着信の履歴に残る
        c = self.ready()
        self.dialed(c, path=("call_failed",))                                       # 発信に失敗した（鳴っていない）
        time.sleep(1.1)
        d = self.ready(tenant=c["tenant"], phone=c["phone"])
        self.lease(d, line=c["line"])
        self.assertIsInstance(self.claim(d, line=c["line"]), gate.Claim)            # 番号は出ていない：同じ回線でかけてよい

    def test_the_same_row_keeps_its_voice(self):
        """同じ行（同じお客様へのかけ直し・お詫び）は、前に話した声のまま（業務の声の型をあとで替えても）。"""
        a = self.ready()
        first = self.dialed(a)
        self.assertEqual(self.tx(a, lambda tx: self.voice(tx, a, "L4")), first.voice_type)

    def test_only_voices_with_approved_recordings_are_spoken(self):
        """話してよい声は、承認した録音が契約者の単位とその版の両方にある声だけ（2026-10-17 ユーザー：「事前の録音チェック」）。
        無ければ通さずに待ち、契約者に知らせる（止めない）。"""
        only_s = self.w.tenant(voices=("Sadachbia",))                              # Leda の契約者の単位の録音が無い
        a = self.ready(tenant=only_s)
        self.assertEqual(self.tx(a, lambda tx: self.voice(tx, a, "L4")), "S2")     # 業務の声の型が使えなければ、使える声で
        none = self.w.tenant(voices=())
        b = self.ready(tenant=none)
        self.assertEqual(self.reason(b), "voice_not_ready")
        told = self.tx(b, lambda tx: tx.all(
            "SELECT summary FROM salescall.notification_events WHERE kind = 'setting_missing'"))
        self.assertEqual([r["summary"] for r in told], [gate.VOICE_NOT_READY])
        x = self.ready(tenant=only_s)                                              # 承認していない組は使わない
        self.tx(x, lambda tx: recordings.create_set(tx, unit="contractor", voice_name="Leda", parts=[
            recordings.Part("greeting", "お忙しいところ恐れ入ります。", "test://Leda/greeting", "1" * 64)]), OPERATOR)
        self.assertEqual(self.tx(x, lambda tx: recordings.ready_voices(tx, x["version"])), ["Sadachbia"])

    def test_no_other_voice_waits_instead_of_using_the_same_voice(self):
        self.override("gate.cross_client_interval", 1)
        only_s = self.w.tenant(voices=("Sadachbia",))
        a = self.ready(tenant=only_s)
        self.dialed(a)                                                             # Sadachbia でかけた
        time.sleep(1.1)
        b = self.ready(tenant=only_s, phone=a["phone"])                            # ほかの依頼者：別の声の録音が無い
        self.assertEqual(self.reason(b), "voice_used_by_other_client")

    def test_a_new_voice_is_used_without_changing_the_code_and_a_retired_voice_is_not(self):
        """会話AIのモデルが進化して声が増えたら、声の一覧に足すだけ（2026-10-17 ユーザー）。使えなくなった声は印を付ける。"""
        self.override("gate.cross_client_interval", 1)
        self.w.a.in_system(ADMIN, lambda tx: recordings.add_voice(tx, "Kore", note="試験で足した声"))   # 管理者がデータを足すだけ
        self.w.a.in_system(ADMIN, lambda tx: recordings.add_voice_type(tx, "K1", "Kore", default_for_voice=True, note="試験"))
        with self.assertRaises(Rejected):                                          # 声の型を足せるのは管理者だけ
            self.w.a.in_tenant(OPERATOR, self.w.tenant(), lambda tx: recordings.add_voice_type(
                tx, "K2", "Kore", default_for_voice=False, note="契約者が足そうとした"))
        with self.assertRaises(Rejected):                                          # 足した声の型は変えられない
            self.w.a.in_system(ADMIN, lambda tx: tx.execute("UPDATE salescall.voice_types SET note = '書き換え' WHERE code = 'K1'"))
        t = self.w.tenant(voices=("Sadachbia", "Kore"))                            # Leda の録音は無い
        a = self.ready(tenant=t)
        self.w.approve_voices(t, a["client"], a["version"], ("Kore",))
        self.dialed(a)                                                             # Sadachbia
        time.sleep(1.1)
        b = self.ready(tenant=t, phone=a["phone"])
        self.w.approve_voices(t, b["client"], b["version"], ("Kore",))
        self.assertEqual(self.claim(b).voice_type, "K1")                           # 足した声で話す
        with self.assertRaises(Rejected):                                          # 声を足せるのは管理者だけ
            self.w.a.in_tenant(OPERATOR, t, lambda tx: tx.execute(
                "INSERT INTO salescall.voices (voice_name, note) VALUES ('Puck', '契約者が足そうとした')"))
        self.w.a.in_system(ADMIN, lambda tx: tx.execute(
            "UPDATE salescall.voices SET retired_at = now(), retired_reason = '試験：モデルが替わって無くなった'"
            " WHERE voice_name = 'Kore'"))
        self.assertEqual(self.tx(b, lambda tx: recordings.ready_voices(tx, b["version"])), ["Sadachbia"])
        with self.assertRaises(Rejected):                                          # 印は1回だけ・消せない
            self.w.a.in_system(ADMIN, lambda tx: tx.execute("UPDATE salescall.voices SET retired_at = NULL,"
                                                            " retired_reason = NULL WHERE voice_name = 'Kore'"))

    def test_only_a_person_approves_recordings_and_an_approved_set_is_fixed(self):
        s = self.ready()
        sid = self.tx(s, lambda tx: recordings.create_set(tx, unit="business", voice_name="Leda", version_id=s["version"],
                                                          parts=[recordings.Part("Q1", "ご希望の日時をお聞かせください。",
                                                                                 "test://Leda/Q1", "2" * 64)]))
        with self.assertRaises(Rejected):                                          # システムの処理は承認できない
            self.tx(s, lambda tx: recordings.approve(tx, sid))
        self.tx(s, lambda tx: recordings.approve(tx, sid), OPERATOR)
        with self.assertRaises(Rejected):                                          # 承認した組には足せない
            self.tx(s, lambda tx: tx.execute(
                "INSERT INTO salescall.recordings (tenant_id, client_id, set_id, part, spoken_text, audio_ref, audio_sha256)"
                " VALUES (%s, %s, %s, 'Q2', 'x', 'test://x', %s)", (s["tenant"], s["client"], sid, "3" * 64)))
        other = self.ready(tenant=s["tenant"])                                     # 同じ契約者のほかの依頼者からも見えない
        self.assertEqual(self.tx(other, lambda tx: recordings.parts(tx, sid)), {})
        cur = self.tx(s, lambda tx: recordings.current_sets(tx, s["version"], "Leda"))
        self.assertEqual(cur["business"], sid)

    def test_recording_part_names_take_the_same_letters_as_question_ids(self):
        """質問の録音の部品の名前は、質問の番号。番号に使える「-」を、部品の名前も受け付ける（前は、「-」のある番号の質問の録音を
        入れられなかった。0103。見直し9）。番号に使えない文字は、部品の名前にも使えない。"""
        s = self.ready()
        make = lambda name: self.tx(s, lambda tx: recordings.create_set(   # noqa: E731
            tx, unit="business", voice_name="Leda", version_id=s["version"],
            parts=[recordings.Part(name, "ご希望の日時をお聞かせください。", "test://Leda/x", "2" * 64)]))
        sid = make("Q-1")
        self.assertEqual(list(self.tx(s, lambda tx: recordings.parts(tx, sid))), ["Q-1"])
        for bad in ("Q 1", "Q.1", ""):
            with self.subTest(bad=bad), self.assertRaises(Rejected):
                make(bad)

    def test_an_answer_recording_keeps_the_japanese_question_name(self):
        """想定問答の答えの録音の部分は「answer_ ＋ 問いの名前」（日本語のまま。会話の側＝instruction.Business.replay_parts）。
        問いの名前の決まり（30文字まで・Windows のファイル名に使えない文字なし）は、業務設定の確かめと同じ（0112。2026-10-22 の
        見直し13：前は英数字だけを受け付け、答えの録音を持つ業務の録音の組が丸ごと作れなかった）。"""
        s = self.ready()
        make = lambda name: self.tx(s, lambda tx: recordings.create_set(   # noqa: E731
            tx, unit="business", voice_name="Leda", version_id=s["version"],
            parts=[recordings.Part(name, "以前ご予約の際にいただいたお電話番号にお掛けしております。", "test://Leda/a", "2" * 64)]))
        sid = make("answer_番号の出どころ")
        self.assertEqual(list(self.tx(s, lambda tx: recordings.parts(tx, sid))), ["answer_番号の出どころ"])
        for bad in ("answer_番号:出どころ", "answer_" + "あ" * 31, "番号の出どころ", "answer_番号\t"):
            with self.subTest(bad=bad), self.assertRaises(Rejected):
                make(bad)

    def test_a_reading_chosen_by_listening_to_recordings_becomes_the_name_reading(self):
        """読みが決まらない名前は、契約者が録音を聞いて選んだ読みが、その版の名前の読みになる（版で1つ。選び直しは記録を足すだけ）。
        録音は、その読みと合う組だけを作れ・承認でき・流す（合わない声は、その読みで録り直すまで話さない）。Intent.md 6-2。
        ⚠️ 前の形（0099）は読みを声ごとの組に持たせ、選び直せない・使わない声が止める・読みを書かずに素通りできた（見直し6）。"""
        from salescall.core import business_settings
        s = self.ready()
        vid = support.full_version(self.w, s, content_too=False)
        raw = support.content()
        raw["names"] += [{"text": "中島", "reading": None, "source": "script", "reading_by": None,
                          "candidates": ["なかじま", "なかしま"], "likely": "なかじま"},
                         {"text": "中嶋", "reading": None, "source": "script", "reading_by": None,   # 読みの候補が重なる名前
                          "candidates": ["なかじま", "なかしま"], "likely": "なかじま"},
                         {"text": "青葉物流", "reading": "あおばぶつりゅう", "source": "script", "reading_by": "ai",
                          "candidates": [], "likely": None}]
        self.tx(s, lambda tx: tx.execute(
            "INSERT INTO salescall.business_contents (tenant_id, client_id, version_id, content, saved_by)"
            " VALUES (%s, %s, %s, %s, %s)", (s["tenant"], s["client"], vid, json.dumps(raw, ensure_ascii=False), OPERATOR.name)))
        name = lambda: next(n for n in self.tx(s, lambda tx: business_settings.load(tx, vid)).names   # noqa: E731
                            if n.text == "中島")
        choose = lambda r: self.tx(s, lambda tx: recordings.choose_reading(tx, vid, "中島", r), OPERATOR)   # noqa: E731
        part = lambda v, r, p="Q1", said=True: recordings.Part(   # noqa: E731
            p, f"「{r}」様のご紹介でお電話いたしました。", f"test://{v}/{p}/{r}", "4" * 64, readings={"中島": r} if said else {})
        make = lambda v, *ps: self.tx(s, lambda tx: recordings.create_set(   # noqa: E731
            tx, unit="business", voice_name=v, version_id=vid, parts=list(ps)))
        approve = lambda sid: self.tx(s, lambda tx: recordings.approve(tx, sid), OPERATOR)   # noqa: E731
        current = lambda v: self.tx(s, lambda tx: recordings.current_sets(tx, vid, v))["business"]   # noqa: E731
        self.assertEqual((name().reading, name().reading_by), (None, None))          # 選ぶまでは、会話の中で確かめる名前
        with self.assertRaises(ValueError):                                        # 選ぶ前に、その読みの録音は作れない
            make("Leda", part("Leda", "なかしま"))
        with self.assertRaises(Rejected):                                          # 選ぶのは管理者か契約者（システムは選べない）
            self.tx(s, lambda tx: recordings.choose_reading(tx, vid, "中島", "なかしま"))
        choose("なかしま")
        self.assertEqual((name().reading, name().reading_by, name().candidates), ("なかしま", "recording", ()))
        leda, sada = make("Leda", part("Leda", "なかしま")), make("Sadachbia", part("Sadachbia", "なかしま"))
        approve(leda)
        self.assertEqual(current("Leda"), leda)
        with self.assertRaises(ValueError):                                        # 読みで話すのに、読みを書かない（素通りさせない）
            make("Leda", part("Leda", "なかしま", "Q2", said=False))
        with self.assertRaises(ValueError):                                        # 選んでいない読み
            make("Leda", part("Leda", "なかじま"))
        choose("なかじま")                                                          # 選び直せる（前の形では、声どうしで行き詰まった）
        self.assertEqual(name().reading, "なかじま")
        self.assertNotEqual(current("Leda"), leda)                                 # 前の読みの組は、もう流さない
        with self.assertRaises(Rejected) as e:                                     # 前の読みで作った組は承認しない
            approve(sada)
        self.assertEqual(e.exception.constraint, "recording_approval_fits")
        new = make("Leda", part("Leda", "なかじま"))
        approve(new)
        self.assertEqual(current("Leda"), new)                                     # その読みで録り直した声から話す
        with self.assertRaises(Rejected) as e:                                     # データベースも、選んだ読みと違う読みを断る
            self.tx(s, lambda tx: tx.execute(
                "INSERT INTO salescall.recording_readings (tenant_id, client_id, set_id, part, name_text, reading)"
                " VALUES (%s, %s, %s, 'Q1', '中島', 'なかしま')", (s["tenant"], s["client"], sada)))
        self.assertIn(e.exception.constraint, ("recording_reading_as_chosen", None))
        for bad in ({"佐藤": "さとう"}, {"株式会社テスト": "てすと"}):           # 版に無い名前・入っている読みと違う読み
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                make("Leda", recordings.Part("Q1", "x", "test://x", "5" * 64, readings=bad))
        with self.assertRaises(ValueError):                                        # 入口の読みのある名前は選べない
            self.tx(s, lambda tx: recordings.choose_reading(tx, vid, "株式会社テスト", "てすと"), OPERATOR)
        with self.assertRaises(Rejected) as e:                                     # データベースも選ばせない
            self.tx(s, lambda tx: tx.execute(
                "INSERT INTO salescall.version_name_readings (tenant_id, client_id, version_id, name_text, seq, reading)"
                " VALUES (%s, %s, %s, '株式会社テスト', 1, 'てすと')", (s["tenant"], s["client"], vid)), OPERATOR)
        self.assertEqual(e.exception.constraint, "version_name_reading_given")
        # 読みの決まった名前（入口の読み・AI の読み）は、選ばずに、その読みのまま録音を作れる（見直し7：前は、データベースが断った・
        # 管理者か契約者が選ぶまで作れなかった＝フルオートでも人の手作業）
        fixed = recordings.Part("Q3", "「かぶしきがいしゃてすと」の「あおばぶつりゅう」の件でお電話いたしました。", "test://Leda/Q3",
                                "6" * 64, readings={"株式会社テスト": "かぶしきがいしゃてすと", "青葉物流": "あおばぶつりゅう"})
        approve(make("Leda", part("Leda", "なかじま"), fixed))
        self.assertEqual({n.text: (n.reading, n.reading_by) for n in self.tx(s, lambda tx: business_settings.load(tx, vid)).names
                          if n.text in ("株式会社テスト", "青葉物流")},
                         {"株式会社テスト": ("かぶしきがいしゃてすと", "given"), "青葉物流": ("あおばぶつりゅう", "ai")})
        for said in ("「アオバブツリュウ」の件です。", "青葉物流の件です。"):                 # カタカナでも表記のままでも、話していれば書く
            with self.subTest(said=said), self.assertRaises(ValueError):
                make("Leda", recordings.Part("Q3", said, "test://x", "7" * 64))
        make("Leda", part("Leda", "なかじま"))                                     # 読みの重なる「中嶋」の読みは求めない
        with self.assertRaises(ValueError):                                        # 契約者の単位の録音には付けない
            self.w.a.in_tenant(OPERATOR, s["tenant"], lambda tx: recordings.create_set(
                tx, unit="contractor", voice_name="Leda", parts=[part("Leda", "なかじま")]))

    def test_a_chosen_reading_is_carried_to_a_restored_or_copied_version(self):
        """以前の版へ戻した版・業務を複製した版にも、契約者が録音を聞いて選んだ名前の読みが引き継がれる（1か所＝
        business_settings.save_from）。⚠️ 前は中身だけを写したので、読みが決まらない名前に戻り、選び直すまで、その名前を話す録音が
        使えなかった（見直し8）。"""
        from salescall.core import business_settings, registry
        s = self.ready()
        vid = support.full_version(self.w, s, content_too=False)
        raw = support.content()
        raw["names"] += [{"text": "中島", "reading": None, "source": "script", "reading_by": None,
                          "candidates": ["なかじま", "なかしま"], "likely": "なかじま"}]
        self.tx(s, lambda tx: tx.execute(
            "INSERT INTO salescall.business_contents (tenant_id, client_id, version_id, content, saved_by)"
            " VALUES (%s, %s, %s, %s, %s)", (s["tenant"], s["client"], vid, json.dumps(raw, ensure_ascii=False), OPERATOR.name)))
        reading = lambda v: next((n.reading, n.reading_by) for n in self.tx(   # noqa: E731
            s, lambda tx: business_settings.load(tx, v)).names if n.text == "中島")
        self.assertEqual(reading(self.tx(s, lambda tx: business_settings.restore(tx, vid), OPERATOR)), (None, None))
        self.tx(s, lambda tx: recordings.choose_reading(tx, vid, "中島", "なかしま"), OPERATOR)
        self.tx(s, lambda tx: recordings.choose_reading(tx, vid, "中島", "なかじま"), OPERATOR)   # いまの読み（選び直した後）
        back = self.tx(s, lambda tx: business_settings.restore(tx, vid), OPERATOR)
        _, copied = self.tx(s, lambda tx: registry.copy_business(tx, vid, "架空の業務（複製）"), OPERATOR)
        for v in (back, copied):
            with self.subTest(v=v):
                self.assertEqual(reading(v), ("なかじま", "recording"))
                self.assertEqual(reading(v), reading(vid))
                self.assertEqual(self.tx(s, lambda tx: recordings.choose_reading(tx, v, "中島", "なかしま"), OPERATOR), None)
                self.assertEqual(reading(v), ("なかしま", "recording"))        # 引き継いだ版でも選び直せる
        self.assertEqual(reading(vid), ("なかじま", "recording"))                # もとの版は変えない
        with self.assertRaises(Rejected):                                      # 引き継ぐのも、管理者か契約者（選ぶのと同じ）
            self.tx(s, lambda tx: business_settings.restore(tx, vid))

    def test_caller_number_changes_with_the_client(self):
        self.override("gate.cross_client_interval", 1)
        a = self.ready()
        self.dialed(a)
        time.sleep(1.1)
        b = self.ready(tenant=a["tenant"], phone=a["phone"])
        self.lease(b, line=a["line"])
        self.assertEqual(self.reason(b, line=a["line"]), "line_used_by_other_client")   # 同じ発信番号ではかけない
        notice = self.tx(b, lambda tx: tx.all("SELECT summary FROM salescall.notification_events"
                                              " WHERE kind = 'setting_missing' AND refs ->> 'client_id' = %s",
                                              (str(b["client"]),)))
        self.assertEqual([n["summary"] for n in notice], [gate.OWN_LINE_MISSING])   # 依頼者ごとの発信番号が無いと知らせる
        self.assertEqual(gate.OWN_LINE_MISSING,
                         "この依頼者に専用の発信番号がありません。発信番号を依頼者タブの「回線（発信番号）」で設定してください。")
        self.lease(b)
        self.assertIsInstance(self.claim(b), gate.Claim)                            # 自分の発信番号ならかける
        mine = self.ready(tenant=a["tenant"])
        self.w.a.in_tenant(OPERATOR, a["tenant"], lambda tx: tx.execute(
            "UPDATE salescall.lines SET client_id = %s WHERE line_id = %s", (a["client"], mine["line"])))
        self.assertEqual(self.reason(mine), "line_other_client")                   # ほかの依頼者に割り当てた発信番号


class ExpiredClaims(Base):
    def test_expired_claim_is_released_and_cannot_dial(self):
        self.override("gate.claim_lifetime", 1)
        s = self.ready()
        c = self.claim(s)
        time.sleep(1.2)
        d = FakeDriver()
        self.assertFalse(dial_exit.dial(self.w.a, SYSTEM, s["tenant"], s["client"], c.attempt_id, d))
        again = self.claim(s)
        self.assertIsInstance(again, gate.Claim)
        self.assertEqual((d.calls, self.state(s, c.attempt_id)), ([], "released"))


class Exit(Base):
    def test_driver_failures(self):
        s = self.ready()
        c = self.claim(s)
        self.assertIs(dial_exit.dial(self.w.a, SYSTEM, s["tenant"], s["client"], c.attempt_id,
                                     FakeDriver(dial_exit.CallNotPlaced("番号を受け付けなかった"))), dial_exit.Outcome.NOT_PLACED)
        self.assertEqual(self.state(s, c.attempt_id), "call_failed")
        s2 = self.ready()
        c2 = self.claim(s2)
        unknown = FakeDriver(RuntimeError("切断"))
        with self.assertRaises(RuntimeError):
            dial_exit.dial(self.w.a, SYSTEM, s2["tenant"], s2["client"], c2.attempt_id, unknown)
        self.assertEqual(self.state(s2, c2.attempt_id), "technical_error")   # かかったか分からない ⇒ 結果不明
        self.assertEqual(getattr(unknown, "apologized", 0), 1)               # かかっていれば、おわびして切る（無言で放置しない）
        s4 = self.ready()
        c4 = self.claim(s4)
        cut = FakeDriver(RuntimeError("切断"))
        with mock.patch.object(dial_exit.attempts, "move", side_effect=RuntimeError("試験：データベースが止まった")), \
                self.assertRaises(RuntimeError):
            dial_exit.dial(self.w.a, SYSTEM, s4["tenant"], s4["client"], c4.attempt_id, cut)
        self.assertEqual(getattr(cut, "apologized", 0), 1)                   # 記録できなくても、おわびは先に（記録を待たせない）
        s3 = self.ready()
        c3 = self.claim(s3)
        mute = FakeDriver()
        mute.in_call = lambda: (_ for _ in ()).throw(ConnectionError("試験：答えない"))
        self.assertIs(dial_exit.dial(self.w.a, SYSTEM, s3["tenant"], s3["client"], c3.attempt_id, mute),
                      dial_exit.Outcome.LINE_IN_CALL)                        # 通話中か分からない ⇒ 通話中（かけない）
        self.assertEqual(mute.calls, [])

    def test_a_line_still_in_a_call_is_not_dialed(self):
        """回線（スマホ）がまだ前の通話の途中なら、かけずに予約を戻す（処理が落ちて立ち上げ直しても、二重にかけない）。"""
        s = self.ready()
        c = self.claim(s)
        d = FakeDriver(busy=True)
        got = dial_exit.dial(self.w.a, SYSTEM, s["tenant"], s["client"], c.attempt_id, d)
        self.assertEqual((got, d.calls, self.state(s, c.attempt_id)), (dial_exit.Outcome.LINE_IN_CALL, [], "released"))
        self.assertFalse(got)

    def test_dnc_after_claim_stops_the_dial(self):
        s = self.ready()
        c = self.claim(s)
        self.tx(s, lambda tx: dnc.register(tx, s["phone"], "refused_found_after"))
        d = FakeDriver()
        self.assertFalse(dial_exit.dial(self.w.a, SYSTEM, s["tenant"], s["client"], c.attempt_id, d))
        self.assertEqual(d.calls, [])


class UsedSchedule(Base):
    def test_no_dial_before_the_previous_one_is_settled(self):
        """前の発信の締め（判定）が済むまで、次をかけない。予定を誰が書き直しても抜け道にならない。"""
        s = self.ready()
        self.override("gate.same_contact_min_interval", 1)
        c = self.claim(s)
        self.assertTrue(dial_exit.dial(self.w.a, SYSTEM, s["tenant"], s["client"], c.attempt_id, FakeDriver()))
        self.tx(s, lambda tx: attempts.move(tx, c.attempt_id, "no_answer"))
        time.sleep(1.1)
        self.tx(s, lambda tx: next_action.schedule_now(tx, s["task"], cause="締めより先に、予定を書き直した"))
        self.assertEqual(self.reason(s), "awaiting_decision")
        self.tx(s, lambda tx: settle.settle(tx, c.attempt_id))
        self.tx(s, lambda tx: next_action.schedule_now(tx, s["task"], cause="試験：いまに書き直した"))
        self.assertIsInstance(self.claim(s), gate.Claim)

    def test_a_released_claim_does_not_use_the_schedule(self):
        self.override("gate.claim_lifetime", 1)
        s = self.ready()
        self.assertIsInstance(self.claim(s), gate.Claim)
        time.sleep(1.2)
        self.assertIsInstance(self.claim(s), gate.Claim)   # 予約だけで戻ったものは、予定を使っていない


class ChangesReleaseClaims(Base):
    """門の判定に効く変更は、同じ取引でまだ発信していない予約を取り消す（5-2-1 の2）。"""

    def test_setting_change(self):
        s = self.ready()
        c = self.claim(s)
        self.tx(s, lambda tx: settings.set_value(tx, "calling.hours", ALL_DAY, scope="client", status="decided",
                                                 rationale="試験", review_when="試験", tenant_id=s["tenant"],
                                                 client_id=s["client"]), OPERATOR)
        self.assertEqual(self.state(s, c.attempt_id), "released")

    def test_calendar_load(self):
        s = self.ready()
        c = self.claim(s)
        load_calendar()
        self.assertEqual(self.state(s, c.attempt_id), "released")

    def test_ending_the_task(self):
        s = self.ready()
        c = self.claim(s)
        self.tx(s, lambda tx: next_action.end(tx, s["task"], "試験", cause="試験"))
        self.assertEqual(self.state(s, c.attempt_id), "released")

    def test_cannot_release_outside_the_transaction_scope(self):
        a, b = self.ready(), self.ready()
        cb = self.claim(b)
        for args in (("system", None, None, None), ("tenant", b["tenant"], None, None),
                     ("client", b["tenant"], b["client"], None)):
            with self.subTest(scope=args[0]), self.assertRaises(Rejected):
                self.tx(a, lambda tx: tx.one("SELECT salescall.release_claims_in_scope(%s, %s, %s, %s)", args))
        self.assertEqual(self.state(b, cb.attempt_id), "claimed")


class ExpiredClaimsDoNotUseCapacity(Base):
    def test_expired_claims_elsewhere_do_not_block(self):
        self.override("gate.claim_lifetime", 1)
        stale = self.ready()
        self.assertIsInstance(self.claim(stale), gate.Claim)
        time.sleep(1.2)                       # 期限が切れた（取り消されるのは、そのお客様・番号・回線の次の予約のとき）
        # 同じ依頼者の別の行・別の回線（期限の切れた予約は、依頼者の同時の本数に数えない）
        self.put_client(stale, "calling.max_concurrent_calls", 1)
        t2 = self.w.task(stale["tenant"], stale["client"], stale["run"], self.w.contact(
            stale["tenant"], stale["client"], normalize(fictional_jp_fixed()).e164))
        self.tx(stale, lambda tx: next_action.schedule_now(tx, t2, cause="試験"))
        line2 = self.w.line(stale["tenant"])
        self.lease(stale, line=line2)
        self.assertIsInstance(self.claim(stale, task=t2, line=line2), gate.Claim)


class Concurrency(Base):
    def run_all(self, fns):
        out, errors = [], []

        def go(fn):
            try:
                out.append(fn())
            except Exception as e:   # noqa: BLE001  試験：どの例外も記録して、あとで落とす
                errors.append(e)
        threads = [threading.Thread(target=go, args=(fn,)) for fn in fns]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        return out

    def test_one_number_from_many_clients_only_one_wins(self):
        first = self.ready()
        rows = [first] + [self.ready(phone=first["phone"]) for _ in range(5)]
        got = self.run_all([lambda r=r: self.claim(r) for r in rows])
        self.assertEqual(sum(isinstance(g, gate.Claim) for g in got), 1)

class Calendar(Base):
    def test_official_shape_only(self):
        bad = [b"", "見出し,違う\r\n2030/1/1,元日\r\n".encode("cp932"),
               "国民の祝日・休日月日,国民の祝日・休日名称\r\n2030/2/30,ありえない日\r\n".encode("cp932"),
               "国民の祝日・休日月日,国民の祝日・休日名称\r\n2030/1/1,元日\r\n2032/1/1,元日\r\n".encode("cp932"),
               "国民の祝日・休日月日,国民の祝日・休日名称\r\n2030/1/1,元日\r\n2030/1/1,元日\r\n".encode("cp932")]
        for data in bad:
            with self.subTest(data=data[:40]), self.assertRaises(CalendarRejected):
                owner_access().in_system(OPERATOR, lambda tx: calendar.load_official_csv(tx, data))

    def test_out_of_range_and_newest_load_wins(self):
        from datetime import date
        owner_access().in_system(OPERATOR, lambda tx: calendar.load_official_csv(tx, calendar_csv([2091, 2092])))
        check = lambda d: owner_access().in_system(SYSTEM, lambda tx: calendar.holiday_checker(tx)(d))
        self.assertTrue(check(date(2091, 1, 1)))
        self.assertFalse(check(date(2091, 1, 2)))
        with self.assertRaises(CalendarOutOfRange):
            check(date(2093, 1, 1))
        owner_access().in_system(OPERATOR, lambda tx: calendar.load_official_csv(
            tx, calendar_csv([2091, 2092], holidays=[date(2091, 1, 2)])))
        self.assertTrue(check(date(2091, 1, 2)))

    def test_needs_fetch_only_when_next_year_is_missing_or_a_year_has_passed(self):
        from datetime import date, datetime
        today, now = date(2030, 6, 1), datetime(2030, 6, 1, 12)
        self.assertTrue(calendar.needs_fetch(None, None, today, now))                        # まだ何も無い
        self.assertTrue(calendar.needs_fetch(2030, datetime(2030, 2, 1), today, now))        # 翌年の分が無い
        self.assertFalse(calendar.needs_fetch(2031, datetime(2030, 2, 1), today, now))       # 翌年まである
        self.assertTrue(calendar.needs_fetch(2031, datetime(2029, 6, 1, 12), today, now))    # 1年たった（直しを拾う）

    def test_a_missing_next_year_is_told_before_the_year_ends(self):
        """11月になっても翌年の祝日の暦が無ければ、年が替わる前に管理者へ知らせ、CL-2 へ修正依頼を出す（⛔ 2026-10-22 の見直し：
        前は、1月1日になって初めて、門がすべての電話を通さなくなった）。"""
        from datetime import date
        self.assertFalse(calendar.next_year_missing(2030, date(2030, 10, 31)))   # 11月より前は、まだ知らせない
        self.assertTrue(calendar.next_year_missing(2030, date(2030, 11, 1)))
        self.assertTrue(calendar.next_year_missing(None, date(2030, 12, 31)))
        self.assertFalse(calendar.next_year_missing(2031, date(2030, 12, 31)))   # 翌年まである
        with mock.patch.object(calendar, "next_year_missing", lambda covered, today: True):
            owner_access().in_system(SYSTEM, calendar._warn_next_year)
            owner_access().in_system(SYSTEM, calendar._warn_next_year)        # 同じ日に2回でも、知らせと依頼は1つ
        told = owner_access().in_system(ADMIN, lambda tx: tx.all(
            "SELECT summary FROM salescall.notification_events WHERE kind = 'calendar_next_year_missing' AND for_admin"))
        asked = owner_access().in_system(SYSTEM, lambda tx: [r for r in fix_requests.open_requests(tx, "CL-2")
                                                             if r["kind"] == "calendar_next_year_missing"])
        self.assertEqual((len(told), len(asked)), (1, 1))

    def test_sync_does_not_fetch_when_not_needed(self):
        def must_not_fetch():
            raise AssertionError("要らないのに取りに行った")
        got = calendar.sync_official(owner_access(), SYSTEM, fetch=must_not_fetch)   # 翌年の分まである（setUp）
        self.assertFalse(got["loaded"])

    def test_sync_fetches_by_itself_and_skips_the_same_file(self):
        from datetime import date
        y = local_today().year
        data = calendar_csv(range(y - 1, y + 2), holidays=[date(y - 1, 12, 30)])   # 前に取り込んだものと違う中身
        self.addCleanup(load_calendar)
        with mock.patch.object(calendar, "needs_fetch", return_value=True):
            first = calendar.sync_official(owner_access(), SYSTEM, fetch=lambda: data)
            again = calendar.sync_official(owner_access(), SYSTEM, fetch=lambda: data)
        self.assertEqual((first["loaded"], again["loaded"]), (True, False))

    @mock.patch.object(calendar, "needs_fetch", return_value=True)
    def test_sync_rejects_stale_or_broken_files_and_keeps_the_old_calendar(self, _):
        before = owner_access().in_system(SYSTEM, lambda tx: tx.one("SELECT count(*) AS n FROM salescall.calendar_loads"))["n"]
        for fetch in (lambda: calendar_csv([2001, 2002]),                       # 今年が入っていない
                      lambda: b"<html>error</html>"):                           # 形が違う
            with self.assertRaises(calendar.CalendarSyncFailed):
                calendar.sync_official(owner_access(), SYSTEM, fetch=fetch)

        def unreachable():
            raise calendar.CalendarSyncFailed("試験：つながらない")
        with self.assertRaises(calendar.CalendarSyncFailed):
            calendar.sync_official(owner_access(), SYSTEM, fetch=unreachable)
        after = owner_access().in_system(SYSTEM, lambda tx: tx.one("SELECT count(*) AS n FROM salescall.calendar_loads"))["n"]
        self.assertEqual(before, after)

    def test_fetch_reports_network_failure_as_sync_failure(self):
        with mock.patch.object(calendar.urllib.request, "urlopen", side_effect=OSError("つながらない")):
            with self.assertRaises(calendar.CalendarSyncFailed):
                calendar.fetch_official()

    def test_calendar_cannot_be_changed(self):
        with self.assertRaises(Rejected):
            owner_access().in_system(OPERATOR, lambda tx: tx.execute("DELETE FROM salescall.calendar_holidays"))



class Content(Base):
    """話す中身と、目的のお客様を呼ぶ材料（業務設定の1か所の決まり。取込・版の割り当てと同じ）。"""
    def switch_to(self, s, target_person=None, *, content=True):
        """新しい版（業務の決まり・段つき）を作り、実施回に直接割り当てる（割り当ての確かめを通らなかった場合と同じ状態）。"""
        from tests.support import full_version
        vid = full_version(self.w, s, target_person, content_too=content)

        def work(tx):
            tx.execute("INSERT INTO salescall.run_versions (tenant_id, client_id, run_id, business_id, seq, version_id,"
                       " assigned_by, reason) VALUES (%s, %s, %s, %s, 2, %s, %s, %s)",
                       (s["tenant"], s["client"], s["run"], s["business"], vid, "試験", "試験"))
            quality_stage.open_stage(tx, vid, 1, None, evidence="試験")
        self.tx(s, work, OPERATOR)

    def test_a_version_without_content_is_not_called(self):
        s = self.ready()
        self.switch_to(s, content=False)
        self.assertEqual(self.reason(s), "settings_missing")

    def test_a_row_without_the_targets_name_is_not_called(self):
        s = self.ready()
        self.switch_to(s, {"kind": "name", "role": None})
        self.assertEqual(self.reason(s), "target_name_missing")
        s2 = self.ready()
        self.switch_to(s2, {"kind": "role", "role": None})
        self.assertEqual(self.reason(s2), "target_role_missing")
        s3 = self.ready()
        self.switch_to(s3, {"kind": "role", "role": "社長"})                # 業務で役職を書いていれば、行の役職は要らない
        self.assertIsInstance(self.claim(s3), gate.Claim)

if __name__ == "__main__":
    unittest.main()


class BusinessDailyLimit(Base):
    """業務ごとの1日の上限（2026-10-18 ユーザー決定：「業務ごと、デフォルトは上限なし」）。営業として新しくかける電話だけを数え、止める。"""

    def put_business(self, s, value):
        self.tx(s, lambda tx: tx.execute(
            "INSERT INTO salescall.setting_values (key, scope, tenant_id, client_id, business_id, value, status, rationale,"
            " review_when, set_by) VALUES ('calling.business_daily_dials', 'business', %s, %s, %s, %s, 'decided', '試験', '試験',"
            " '試験')", (s["tenant"], s["client"], s["business"], json.dumps(value))), OPERATOR)

    def other_row_dialed(self, s):
        phone = normalize(fictional_jp_fixed()).e164
        k = self.w.contact(s["tenant"], s["client"], phone)
        other = dict(s, task=self.w.task(s["tenant"], s["client"], s["run"], k), contact=k, phone=phone)
        self.past_attempt(other, ["dialing", "no_answer"])

    def test_no_limit_by_default(self):
        s = self.ready()
        self.other_row_dialed(s)
        self.assertIsInstance(self.claim(s), gate.Claim)

    def test_the_limit_counts_the_business_dials_of_today_and_skips_promised_calls(self):
        s = self.ready()
        self.put_business(s, 1)
        self.other_row_dialed(s)
        self.assertEqual(self.reason(s), "business_daily_limit")
        self.assertIn("business_daily_limit", gate.BUSINESS_REASONS)        # 見守り役は、その業務の行をこの回だけ飛ばす
        self.tx(s, lambda tx: support.schedule_call(tx, s["task"], tx.one("SELECT now() AS t")["t"],
                                                        contact_id=s["contact"], cause="試験", promised=True))
        self.assertIsInstance(self.claim(s), gate.Claim)                    # お客様と約束したかけ直しは止めない

    def test_a_live_claim_of_the_business_counts_but_an_expired_one_does_not(self):
        """予約してまだかけていない試行も数える（数えないと、同時に動く回線の本数まで上限を超える）。期限の切れた予約は数えない。"""
        s = self.ready()
        self.put_business(s, 1)
        phone = normalize(fictional_jp_fixed()).e164
        k = self.w.contact(s["tenant"], s["client"], phone)
        other = dict(s, task=self.w.task(s["tenant"], s["client"], s["run"], k), contact=k, phone=phone)
        live = self.w.insert_claim(other)
        self.assertEqual(self.reason(s), "business_daily_limit")
        self.tx(s, lambda tx: attempts.release(tx, [live]))
        self.w.insert_claim(other, expires_in=timedelta(microseconds=1))
        self.assertIsInstance(self.claim(s), gate.Claim)

    def test_the_value_is_null_or_a_positive_number(self):
        s = self.ready()
        for bad in (0, -1, "なし"):
            with self.subTest(bad=bad), self.assertRaises(Rejected):
                self.put_business(s, bad)
        self.put_business(s, None)                                           # 上限なし
        self.other_row_dialed(s)
        self.assertIsInstance(self.claim(s), gate.Claim)
