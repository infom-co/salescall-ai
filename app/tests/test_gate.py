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
from datetime import timedelta
from unittest import mock

from salescall.core import attempts, calendar, dial_exit, dnc, gate, next_action, quality_stage, settings, stops
from salescall.core.access import Rejected
from salescall.core.calendar import CalendarOutOfRange, CalendarRejected
from salescall.core.phone import normalize
from tests.support import OPERATOR, SYSTEM, World, fictional_jp_fixed, owner_access

KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun", "holiday")
ALL_DAY = {k: [["00:00", "24:00"]] for k in KEYS}
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
        self.override("gate.ai_tpm_limit", 10 ** 9)   # 会話AIの枠は全体で共有：ほかの試験の進行中の試行に左右されない
        load_calendar()

    def capacity_of(self, extra):
        """いま生きている進行中の数 ＋ extra 本だけ通る枠にする（0.8 ÷ 7,500 の計算で、ちょうどの値）。
        生きている数は、門の関数を使わずに数える（門の数え方を誤っても、この試験が気づくように）。"""
        n = owner_access().in_system(SYSTEM, lambda tx: tx.one(
            "SELECT count(*) AS n FROM salescall.attempts WHERE state IN ('dialing', 'ringing', 'answered',"
            " 'in_conversation', 'ending') OR (state = 'claimed' AND claim_expires_at > now())"))["n"]
        self.override("gate.ai_tpm_limit", (n + extra) * 9375)

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

    def ready(self, *, tenant=None, phone=None):
        """かけてよい状態の行を1つ作る（時間帯は終日・予定はいま・最初の段は全部・回線は借りている）。"""
        raw = phone or fictional_jp_fixed()
        s = self.w.setup(phone=normalize(raw).e164, tenant=tenant)
        self.put_client(s, "calling.hours", ALL_DAY)
        self.tx(s, lambda tx: next_action.schedule_now(tx, s["task"], cause="試験"))
        self.tx(s, lambda tx: quality_stage.open_stage(tx, s["version"], 1, None, evidence="試験"), OPERATOR)
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

    def past_attempt(self, s, path, *, verify=None, line=None):
        """前の試行を1つ作って、path の状態まで進め、次の動きが予定を決め直したことにする（予定は1回の発信で使い切るため）。"""
        aid = self.w.insert_claim(s, line=line)
        for state in path:
            self.w.move(s, aid, state)
        if verify:
            self.tx(s, lambda tx: attempts.record_verification(tx, aid, verify))
        self.tx(s, lambda tx: next_action.schedule_now(tx, s["task"], cause="試験：前の発信のあとに決め直した"))
        return aid

    def state(self, s, aid):
        return self.tx(s, lambda tx: tx.one("SELECT state FROM salescall.attempts WHERE attempt_id = %s", (aid,)))["state"]


class FakeDriver:
    def __init__(self, fail=None):
        self.calls, self.fail = [], fail

    def start_call(self, phone):
        self.calls.append(phone)
        if self.fail:
            raise self.fail


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

    def test_every_reason_has_a_kind(self):
        self.assertTrue(all(kind in ("permanent", "wait", "cannot_judge") for _, kind in gate.REASONS.values()))


class Schedule(Base):
    def test_no_schedule_and_not_due(self):
        s = self.ready()
        t2 = self.w.task(s["tenant"], s["client"], self.w.run(s["tenant"], s["client"], s["business"], s["version"]),
                         s["contact"])
        self.assertEqual(self.reason(s, task=t2), "no_schedule")
        self.tx(s, lambda tx: next_action.schedule_call(tx, s["task"], tx.one("SELECT now() + interval '1 hour' AS t")["t"],
                                                        cause="試験"))
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

    def test_version_without_stage_cannot_dial(self):
        s = self.ready()
        v2 = self.w.add_version(s["tenant"], s["client"], s["business"], 2)
        self.tx(s, lambda tx: tx.execute(
            "INSERT INTO salescall.run_versions (tenant_id, client_id, run_id, business_id, seq, version_id, assigned_by, reason)"
            " VALUES (%s, %s, %s, %s, 2, %s, '試験', '新しい版')", (s["tenant"], s["client"], s["run"], s["business"], v2)),
            OPERATOR)
        self.assertEqual(self.reason(s), "version_not_ready")

    def test_stage_cap_counts_answered_and_active(self):
        s = self.ready()
        self.tx(s, lambda tx: quality_stage.open_stage(tx, s["version"], 1, 1, evidence="試験：最初の段は1件"), OPERATOR)
        self.past_attempt(s, ["dialing", "answered", "ending", "ended"], verify="done")
        self.override("gate.same_contact_min_interval", 1)
        time.sleep(1.1)
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
        self.assertEqual(self.reason(s), "stopped")
        other_line = self.w.line(s["tenant"])
        self.lease(s, line=other_line)
        self.assertIsInstance(self.claim(s, line=other_line), gate.Claim)   # ほかの回線は止まっていない
        self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: stops.resume(tx, e["event_id"], reason="試験"))

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
    def test_verification_pending_blocks_the_contact(self):
        s = self.ready()
        self.past_attempt(s, ["dialing", "answered", "ending", "ended"])
        self.assertEqual(self.reason(s), "verification_pending")
        self.assertIsNone(self.tx(s, lambda tx: gate.pacing(tx, s["task"])).earliest)

    def test_same_contact_interval(self):
        s = self.ready()
        self.past_attempt(s, ["dialing", "no_answer"])
        self.assertEqual(self.reason(s), "same_contact_interval")
        p = self.tx(s, lambda tx: gate.pacing(tx, s["task"]))
        self.assertEqual((p.remaining, p.binding), (2, "same_contact_interval"))

    def test_unknown_result_waits_longer(self):
        s = self.ready()
        self.past_attempt(s, ["dialing", "technical_error"], verify="done")
        self.assertEqual(self.reason(s), "unknown_result_interval")

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

    def test_own_retries_are_not_cross_client(self):
        s = self.ready()
        self.override("gate.same_contact_min_interval", 1)
        self.past_attempt(s, ["dialing", "no_answer"])
        time.sleep(1.1)
        self.assertIsInstance(self.claim(s), gate.Claim)   # 同じ依頼主の再発信に、依頼主をまたいだ間隔はかからない


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
        self.override("line.max_dials_per_day", 1)
        u = self.ready(tenant=s["tenant"])
        self.past_attempt(u, ["dialing", "no_answer"], line=u["line"])
        v = self.ready(tenant=s["tenant"])
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

    def test_ai_capacity_is_shared_by_everyone(self):
        a, b = self.ready(), self.ready()
        self.capacity_of(1)
        self.assertIsInstance(self.claim(a), gate.Claim)
        self.assertEqual(self.reason(b), "ai_capacity")


class ActiveConflicts(Base):
    def test_same_contact_in_another_run(self):
        s = self.ready()
        run2 = self.w.run(s["tenant"], s["client"], s["business"], s["version"])
        t2 = self.w.task(s["tenant"], s["client"], run2, s["contact"])
        self.tx(s, lambda tx: next_action.schedule_now(tx, t2, cause="試験"))
        self.assertIsInstance(self.claim(s), gate.Claim)
        line2 = self.w.line(s["tenant"])
        self.lease(s, line=line2)
        self.assertEqual(self.reason(s, task=t2, line=line2), "contact_active")

    def test_same_number_elsewhere_tells_only_the_relation(self):
        a = self.ready()
        b = self.ready(tenant=a["tenant"], phone=a["phone"])
        c = self.ready(phone=a["phone"])
        self.assertIsInstance(self.claim(a), gate.Claim)
        self.assertEqual(self.reason(b), "number_active_same_tenant")
        self.assertEqual(self.reason(c), "number_active_other_tenant")


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
        self.assertFalse(dial_exit.dial(self.w.a, SYSTEM, s["tenant"], s["client"], c.attempt_id,
                                        FakeDriver(dial_exit.CallNotPlaced("番号を受け付けなかった"))))
        self.assertEqual(self.state(s, c.attempt_id), "call_failed")
        s2 = self.ready()
        c2 = self.claim(s2)
        with self.assertRaises(RuntimeError):
            dial_exit.dial(self.w.a, SYSTEM, s2["tenant"], s2["client"], c2.attempt_id, FakeDriver(RuntimeError("切断")))
        self.assertEqual(self.state(s2, c2.attempt_id), "technical_error")   # かかったか分からない ⇒ 結果不明

    def test_dnc_after_claim_stops_the_dial(self):
        s = self.ready()
        c = self.claim(s)
        self.tx(s, lambda tx: dnc.register(tx, s["phone"], "refused_found_after"))
        d = FakeDriver()
        self.assertFalse(dial_exit.dial(self.w.a, SYSTEM, s["tenant"], s["client"], c.attempt_id, d))
        self.assertEqual(d.calls, [])


class UsedSchedule(Base):
    def test_a_schedule_is_used_up_by_one_dial(self):
        """判定・次の動きが予定を決め直す前に、使った予定でもう一度かけない。"""
        s = self.ready()
        self.override("gate.same_contact_min_interval", 1)
        c = self.claim(s)
        self.assertTrue(dial_exit.dial(self.w.a, SYSTEM, s["tenant"], s["client"], c.attempt_id, FakeDriver()))
        self.tx(s, lambda tx: attempts.move(tx, c.attempt_id, "no_answer"))
        time.sleep(1.1)
        self.assertEqual(self.reason(s), "awaiting_decision")
        self.tx(s, lambda tx: next_action.schedule_now(tx, s["task"], cause="次の動き"))
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
        time.sleep(1.2)                       # 期限が切れた（取り消されるのは、その相手・番号・回線の次の予約のとき）
        fresh = self.ready()
        self.capacity_of(1)
        self.put_client(fresh, "calling.max_concurrent_calls", 1)
        self.assertIsInstance(self.claim(fresh), gate.Claim)


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

    def test_ai_capacity_holds_under_concurrency(self):
        rows = [self.ready() for _ in range(6)]
        self.capacity_of(2)
        got = self.run_all([lambda r=r: self.claim(r) for r in rows])
        self.assertEqual(sum(isinstance(g, gate.Claim) for g in got), 2)


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

    def test_calendar_cannot_be_changed(self):
        with self.assertRaises(Rejected):
            owner_access().in_system(OPERATOR, lambda tx: tx.execute("DELETE FROM salescall.calendar_holidays"))


if __name__ == "__main__":
    unittest.main()
