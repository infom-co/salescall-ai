"""電話の一生の試験（データベースの時計を進める。DESIGN.md 5-4・7-2-1・9-1。持ち主 CL-2）。

本物の部品（回線ごとの処理 one_round〔候補 → 門 → 発信の出口 → 通話〕・止めた回線の確かめ _check_stop・締めの仕事 settle_ready →
判定 → 次の動き）を、データベースの時計を進めながら何日分も通す（見直し20a の場面 r5・r4 と、壊れた回線）。時計は、この試験のためだけに
立てる使い捨てのデータベース（run_tests.clock_cluster。libfaketime をデータベースの処理にだけ差し込む）の時計（Python の時計は進めない）。
時計をずらす部品が無い機械では、この試験は飛ばす（壊し試しが、飛ばした試験では落ちないので、run_tests が「見張れていない」と知らせる）。

2026-10-24 管理者の決定：「明らかに番号に問題がある場合は、その行は取りやめですが、回線の問題が明らかな場合は行は取りやめでなく一時中止
だけ」「回線か番号かどちらかわからない場合は…1回目から、次の行に進む…一つの行で止まり続けることがないように」「その回線にかける行が
残っていないときは、『テスト番号』で決めます。前に鳴った電話があれば番号のせい、一度も鳴らせていなければ回線のせい」。
⚠️ 試験のデータは架空のものだけ（番号は架空の範囲）。
"""

from __future__ import annotations

import contextlib
import os
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import run_tests
from salescall.core import access as access_mod
from salescall.core import attempts, conversation_io, dial_exit, next_action, settings, watch
from salescall.core.calling_hours import JST
from salescall.core.phone import normalize
from salescall.supervisor import jobs, line
from salescall.supervisor.links import NoLinks
from tests import support
from tests.support import CLEAN, OPERATOR, SYSTEM, owner_access, rules_raw
from tests.test_gate import HOLDER, KEYS, FakeDriver
from tests.test_settle import rep
from tests.test_supervisor import SupervisorBase

_ENV = ("SALESCALL_TEST_APP_DSN", "SALESCALL_TEST_OWNER_DSN", "SALESCALL_TEST_RESTORE_DSN", "SALESCALL_TEST_ADMIN_DSN")
_DB: dict = {}


def setUpModule():
    if run_tests.faketime_library() is None:
        raise unittest.SkipTest("データベースの時計をずらす部品（libfaketime）がありません")
    stack = contextlib.ExitStack()
    dsn, clock = stack.enter_context(run_tests.clock_cluster())
    saved = {k: os.environ.get(k) for k in _ENV}
    try:
        db, owner, app = run_tests._fresh_db(dsn)
    except BaseException:
        stack.close()
        raise
    os.environ.update(SALESCALL_TEST_APP_DSN=app, SALESCALL_TEST_OWNER_DSN=owner)
    _DB.update(stack=stack, saved=saved, dsn=dsn, db=db, clock=clock, conninfos=(app, owner))


def tearDownModule():
    if not _DB:
        return
    for k, v in _DB["saved"].items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    with access_mod._POOLS_LOCK:   # この試験のデータベースへの共有のつなぎを閉じる（ほかの試験に残さない）
        pools = [access_mod._POOLS.pop(c) for c in _DB["conninfos"] if c in access_mod._POOLS]
    for pool in pools:
        pool.close(timeout=5)
    try:
        run_tests._drop_db(_DB["dsn"], _DB["db"])
    finally:
        _DB["stack"].close()
        _DB.clear()


WEEKDAY_9_18 = {k: ([["09:00", "18:00"]] if k in ("mon", "tue", "wed", "thu", "fri") else []) for k in KEYS}
LIMIT_7_22 = {k: [["07:00", "22:00"]] for k in KEYS}
NG_NONE = {k: [] for k in KEYS}


class Phone(FakeDriver):
    """回線の電話の部品の代わり。broken＝回線が壊れている（どの番号もかけられない・テスト番号も鳴らない）。bad＝その番号だけかけられない。
    テスト番号は、壊れていなければ鳴る。"""

    def __init__(self, *, broken=False, bad=()):
        super().__init__()
        self.broken, self.bad = broken, set(bad)

    def test_ring(self, phone):
        self.rings.append(phone)
        return not self.broken

    def start_call(self, phone):
        self.calls.append(phone)
        if self.broken or phone in self.bad:
            raise dial_exit.CallNotPlaced("試験：かけられない")


class Talk:
    """会話の処理の代わり：出て、話して、Q1 が取れて、ふつうに終わる。"""

    def run(self, access, actor, claim, *, tenant_id, client_id, call=None):
        if call is not None:
            call.beat()

        def work(tx):
            for st in ("ringing", "answered", "in_conversation", "ending", "ended"):
                attempts.move(tx, claim.attempt_id, st)
            conversation_io.write_item(tx, claim.attempt_id, conversation_io.ItemValue("Q1", "confirmed", None, "答えた"))
            conversation_io.save_report(tx, claim.attempt_id, rep().to_json())
            attempts.record_verification(tx, claim.attempt_id, summary=CLEAN)
        access.in_client(actor, tenant_id, client_id, work)


class Life(SupervisorBase):
    def setUp(self):
        super().setUp()
        self.override("calling.ng_hours", NG_NONE)
        self.override("calling.hours_limit", LIMIT_7_22)
        self.override("callee.designated_limit", ["07:00", "22:00"])

    # ── 時計（データベースだけ）
    def db_now(self):
        return owner_access().in_system(SYSTEM, lambda tx: tx.one("SELECT now() AS t")["t"])

    def jump(self, at: datetime) -> None:
        off = (at - datetime.now(timezone.utc)).total_seconds()
        Path(_DB["clock"]).write_text(f"{off:+.3f}\n")
        got = self.db_now()
        self.assertLess(abs((got - at).total_seconds()), 5, (got, at))

    def next_monday_9(self) -> datetime:
        """いまのデータベースの時計より後の、月曜の朝9時（日本時間。時計は前にしか進めない＝試験どうしで戻さない）。"""
        d = (self.db_now() + timedelta(days=1)).astimezone(JST).date()
        d += timedelta(days=(7 - d.weekday()) % 7)
        return datetime(d.year, d.month, d.day, 9, 0, tzinfo=JST)

    # ── 場面
    def start(self, n_rows):
        self.jump(self.next_monday_9())
        s = self.ready(rules=rules_raw(retry={k: {"action": "call", "max_times": 8, "interval": {"hours": 2}}
                                              for k in ("open", "incomplete", "not_reached_absent", "callback_unspecified_time")}))
        self.put_client(s, "calling.hours", WEEKDAY_9_18)
        rows = [s]
        for _ in range(n_rows - 1):
            k = self.w.contact(s["tenant"], s["client"], normalize(support.fictional_jp_fixed()).e164)
            t = self.w.task(s["tenant"], s["client"], s["run"], k)
            self.tx(s, lambda tx: next_action.schedule_now(tx, t, cause="試験"))
            rows.append(dict(s, task=t, contact=k))
        for r in rows:
            r["phone"] = self.tx(r, lambda tx, r=r: tx.one("SELECT phone_e164 FROM salescall.contacts WHERE contact_id = %s",
                                                           (r["contact"],))["phone_e164"])
        return s, rows

    def give_test_number(self, s):
        """テスト番号を入れる（鳴らしてよい時間は 0:00〜24:00＝終日。あける長さは初めの値 15→30→60→120→240分）。"""
        phone = normalize(support.fictional_jp_fixed()).e164
        for key, value in (("line.test_number", phone), ("line.test_ring_hours", [["00:00", "24:00"]])):
            self.w.a.in_tenant(OPERATOR, s["tenant"], lambda tx, key=key, value=value: settings.set_value(
                tx, key, value, scope="tenant", tenant_id=s["tenant"], status="decided", rationale="試験", review_when="試験"))
        return phone

    def ctx(self, s, links=None):
        return jobs.Context(self.w.a, None, HOLDER, links or NoLinks(), s["tenant"])

    def turn(self, s, phone, talk):
        """回線ごとの処理の loop の何回分か（かけなくなるまで。loop と同じく、毎回の頭で止めた回線の確かめ）。"""
        self.lease(s, seconds=3600)
        for _ in range(8):
            line._check_stop(self.w.a, SYSTEM, s["tenant"], s["line"], phone)
            got = line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, phone, talk)
            jobs.settle_ready(self.ctx(s))
            if got.called is None or got.pause:
                break
        self.w.a.in_tenant(SYSTEM, s["tenant"], watch.lasting_stops)

    def run_until(self, s, phone, talk, until, *, step=timedelta(minutes=10), night=timedelta(minutes=30), each=None):
        """時計を step ずつ（夜は night ずつ）進め、毎回その回線をまわす。"""
        t = self.db_now()
        while t < until:
            self.turn(s, phone, talk)
            if each is not None:
                each()
            t += step if 8 <= t.astimezone(JST).hour < 22 else night
            self.jump(t)

    # ── 読む
    def ended(self, r):
        return self.tx(r, lambda tx: next_action.current(tx, r["task"]))["end_reason"]

    def line_stops(self, s):
        return self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: tx.all(
            "SELECT event_id FROM salescall.stop_events WHERE scope = 'line' AND action = 'stop' AND line_id = %s", (s["line"],)))

    def notes(self, s, kind):
        return self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: tx.one(
            "SELECT count(*) AS n FROM salescall.notification_events WHERE kind = %s", (kind,)))["n"]

    def line_faults(self, s):
        return self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: tx.one(
            "SELECT count(*) AS n FROM salescall.error_events WHERE source = 'line'"))["n"]

    def dialed(self, s):
        return owner_access().in_system(SYSTEM, lambda tx: tx.all(
            "SELECT attempt_id, phone_e164 FROM salescall.attempts WHERE tenant_id = %s AND salescall.is_dialed(state)"
            " ORDER BY claimed_at", (s["tenant"],)))

    # ── 場面 r5：かけられない番号が2つ並ぶ・回線は元気・テスト番号は終日鳴る
    def test_two_bad_numbers_side_by_side_on_a_healthy_line_end_as_the_numbers_without_stopping_the_line(self):
        """一覧の5行のうち、かけられない番号の行XとYが並ぶ（ほかの3行は話して完了）。回線は元気・テスト番号は終日鳴る。月曜9時から
        金曜18時まで：XとYは、テスト番号で番号のせいと決まり、決まった順（当日2回・翌日・2日後）の4回で取りやめになる。回線は一度も
        止まらず、回線の異常にも数えず、止めた知らせも出ない。テスト番号を鳴らすのは、XとYが続けて鳴らなかったときだけ（少ない）。
        ⛔ 見直し20a（場面 r5）：前はテスト番号を挟まずに回線のせいにして、5日で回線の停止14回・知らせ28件・テスト番号47回、XとYは
        取りやめにならなかった。"""
        s, rows = self.start(5)
        self.give_test_number(s)
        x, y = rows[3], rows[4]
        phone = Phone(bad={x["phone"], y["phone"]})
        start = self.db_now()
        self.run_until(s, phone, Talk(), start + timedelta(days=4, hours=9))
        self.assertEqual(self.line_stops(s), [])
        self.assertEqual((self.line_faults(s), self.notes(s, "stopped")), (0, 0))
        self.assertEqual([self.ended(r) for r in rows[:3]], ["completed"] * 3)
        self.assertEqual([self.ended(r) for r in (x, y)], ["number_unusable"] * 2)
        self.assertLessEqual(len(phone.rings), 6)                               # XとYの4回の組ごとに1回ほど
        tries = [a for a in self.dialed(s) if a["phone_e164"] in (x["phone"], y["phone"])]
        self.assertEqual(len(tries), 8)                                         # どちらも4回（それより多くかけない）

    # ── 場面 r4：一覧の多くが、かけられない番号・回線は元気
    def test_a_list_of_mostly_bad_numbers_on_a_healthy_line_never_stops_the_line(self):
        """元気な回線で、10行のうち6行がかけられない番号（ほかは話して完了）。テスト番号は終日。月曜9時から木曜18時まで：かけられない
        番号の行はどれも番号のせいで取りやめ、話せる行はどれも完了。回線は一度も止まらない。テスト番号を鳴らす回数は、違う番号が続けて
        鳴らなかったときだけ（1日に十数回より少ない）。⛔ 見直し20a（場面 r4）。"""
        s, rows = self.start(10)
        self.give_test_number(s)
        bad = [r for i, r in enumerate(rows) if i % 2 == 0] + [rows[1]]
        good = [r for r in rows if r not in bad]
        phone = Phone(bad={r["phone"] for r in bad})
        start = self.db_now()
        per_day = []
        for day in range(4):
            before = len(phone.rings)
            self.run_until(s, phone, Talk(), start + timedelta(days=day, hours=9 if day == 3 else 15), step=timedelta(minutes=5))
            per_day.append(len(phone.rings) - before)
        self.assertEqual(self.line_stops(s), [])
        self.assertEqual(self.line_faults(s), 0)
        self.assertEqual([self.ended(r) for r in good], ["completed"] * len(good))
        self.assertEqual([self.ended(r) for r in bad], ["number_unusable"] * len(bad))
        self.assertLessEqual(max(per_day), 12, per_day)

    # ── 壊れた回線：今までどおり回線のせい（行は取りやめにならない）
    def test_a_broken_line_is_the_lines_and_no_row_is_ended(self):
        """壊れた回線（どの番号もかけられない・テスト番号も鳴らない）：違う番号が2つ続けて鳴らないと、テスト番号を鳴らせる時になりしだい
        テスト番号で決め、鳴らないので回線のせい（回線の異常に数えて休み、続けば止めて知らせる）。待つあいだも回線は次の行へかけ続ける
        （2026-10-24 管理者「作業を止めるな」）が、どの行も番号のせいにならず、取りやめにならない。テスト番号を鳴らすのは、決めた間
        （15→30→…→240分）より詰めない。"""
        s, rows = self.start(5)
        self.give_test_number(s)
        phone = Phone(broken=True)
        start = self.db_now()
        self.run_until(s, phone, Talk(), start + timedelta(days=1, hours=3))
        self.assertTrue(self.line_stops(s))
        self.assertGreater(self.notes(s, "stopped"), 0)                          # 止めたことを知らせる
        for r in rows:
            self.assertNotEqual(self.ended(r), "number_unusable")
            self.assertEqual(self.tx(r, lambda tx, r=r: attempts.failed_run(tx, r["contact"]))["n_sure"], 0)
        blames = {self.tx(s, lambda tx, a=a: attempts.blame(tx, a["attempt_id"])) for a in self.dialed(s)}
        self.assertEqual(blames - {"pending"}, {"line"})
        self.assertLessEqual(len(phone.rings), 12)                               # 27時間で、決めた間より詰めない
        tests = self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: [r["tested_at"] for r in tx.all(
            "SELECT tested_at FROM salescall.line_ring_tests WHERE line_id = %s ORDER BY test_id", (s["line"],))])
        self.assertTrue(all((b - a) >= timedelta(minutes=15) for a, b in zip(tests, tests[1:])), tests)
