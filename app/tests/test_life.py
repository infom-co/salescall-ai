"""電話の一生の試験（データベースの時計を進める。DESIGN.md 5-4・7-2-1・9-1・17章。持ち主 CL-2）。

本物の部品（回線ごとの処理 one_round〔回線の欄を聞く → テスト番号を鳴らす・候補 → 門 → 発信の出口 → 通話・待つ〕・締めの仕事
settle_ready → 判定 → 次の動き）を、データベースの時計を進めながら何日分も通す（見直し20a の場面 r5・r4 と、壊れた回線。見直し21a の
場面 a1・a2・a3・a4・a6・a7・a9・a10。見直し22 の場面）。時計は、この試験のためだけに立てる使い捨てのデータベース
（run_tests.clock_cluster。libfaketime をデータベースの処理にだけ差し込む）の時計（Python の時計は進めない）。
時計をずらす部品が無い機械では、この試験は飛ばす（壊し試しが、飛ばした試験では落ちないので、run_tests が「見張れていない」と知らせる）。

**読み手と状態の欄を比べる**（DESIGN.md 17章の作り直しの4段目。2026-10-29）：どの場面も、時計を進める各時点で、読み手の答え（締めの
待ち・次の動きが受け取る試行のせい・候補・番号の口・回線ごとの処理が休みの回線でかけないか）と、状態の欄を比べる（tests/shadow.py）。
4段目では読み手が表そのものなので、食い違いは無い（3段目の分け方の一覧 SHADOW_KNOWN・場面ごとの組 SHADOW_SCENES は無くした）。表を
引けない・入口の失敗も無い。17章の 17-8（今の作りの誤り）と、3段目の比べで見つけた誤りが無くなったことは、tests/test_decided.py の
場面 `Errors178.test_s17_…` で示す（管理者のご決定ごとの場面も、同じ所の `Decided`）。

2026-10-24 管理者の決定：「明らかに番号に問題がある場合は、その行は取りやめですが、回線の問題が明らかな場合は行は取りやめでなく一時中止
だけ」「回線か番号かどちらかわからない場合は…1回目から、次の行に進む…一つの行で止まり続けることがないように」「その回線にかける行が
残っていないときは、『テスト番号』で決めます。前に鳴った電話があれば番号のせい、一度も鳴らせていなければ回線のせい」。2026-10-25 夜
管理者：テスト番号は「鳴ったら、テストＯＫなだけ」（あけない。鳴らなかった・見分けられないときだけ 15→30→60→120→240分あける）。
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
from salescall.core import (approval, attempts, conversation_io, dial_exit, fix_requests, health, next_action, quality_stage, settings,
                            watch)
from salescall.core.health_rules import TaskStage
from salescall.core.calling_hours import JST
from salescall.core.phone import normalize
from salescall.supervisor import jobs, line
from salescall.supervisor.links import NoLinks
from tests import shadow, support
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


UNKNOWN = object()   # テスト番号の答えがいつも「見分けられない」


class Phone(FakeDriver):
    """回線の電話の部品の代わり。broken＝回線が壊れている（どの番号もかけられない・テスト番号も鳴らない）。bad＝その番号だけかけられない。
    テスト番号は、壊れていなければ鳴る（answer＝UNKNOWN なら、いつも見分けられない）。"""

    def __init__(self, *, broken=False, bad=(), answer=None):
        super().__init__()
        self.broken, self.bad, self.answer = broken, set(bad), answer

    def test_ring(self, phone):
        self.rings.append(phone)
        if self.answer is UNKNOWN:
            return None
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
        self.shadow_labels, self.shadow_seen = shadow.Labels(), {}   # 読み手と状態の欄の食い違い（tests/shadow.py。DESIGN.md 17章）

    # ── 読み手と状態の欄の比べ（各時点＝回線ごとの処理の何回分かのあと）
    def compare(self, s) -> None:
        at = self.db_now().astimezone(JST).strftime("%m-%d %H:%M")
        for d in shadow.observe(s["tenant"], self.shadow_labels) or ():
            first, n = self.shadow_seen.get(d, (at, 0))
            self.shadow_seen[d] = (first, n + 1)

    def tearDown(self):
        report = os.environ.get("SALESCALL_SHADOW_REPORT")
        if report:   # 作業用：食い違いを書き出す（場面ごと・初めて見た時刻と、見た回数）
            with open(report, "a", encoding="utf-8") as f:
                f.write(f"\n===== {self._testMethodName} {len(self.shadow_seen)}\n")
                for d, (first, n) in self.shadow_seen.items():
                    f.write(f"{first} ×{n} | {d.what} | {d.label} | 読み手={d.current!r} | 欄={d.shadow!r}\n")
        super().tearDown()
        # 4段目：読み手は状態の欄を読むだけなので、どの場面のどの時点でも食い違いは無い（3段目の分け方の一覧＝SHADOW_KNOWN は無し）。
        # 表を引けない・入口の失敗も無い
        self.assertEqual(list(self.shadow_seen), [], "読み手と状態の欄の食い違い（4段目では無いはず）")

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
        if any((d + timedelta(days=i)).month == 1 and (d + timedelta(days=i)).day == 1 for i in range(5)):
            d += timedelta(days=7)   # 試験の暦の休み（1月1日）が平日にかかる週は飛ばす（場面の日数を変えない）
        return datetime(d.year, d.month, d.day, 9, 0, tzinfo=JST)

    # ── 場面
    def start(self, n_rows, at=None):
        self.jump(at or self.next_monday_9())
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

    def give_test_number(self, s, hours=(("00:00", "24:00"),)):
        """テスト番号を入れる（鳴らしてよい時間は初め 0:00〜24:00＝終日。あける長さは初めの値 15→30→60→120→240分）。"""
        phone = normalize(support.fictional_jp_fixed()).e164
        for key, value in (("line.test_number", phone), ("line.test_ring_hours", [list(h) for h in hours])):
            self.w.a.in_tenant(OPERATOR, s["tenant"], lambda tx, key=key, value=value: settings.set_value(
                tx, key, value, scope="tenant", tenant_id=s["tenant"], status="decided", rationale="試験", review_when="試験"))
        return phone

    def ctx(self, s, links=None):
        return jobs.Context(self.w.a, None, HOLDER, links or NoLinks(), s["tenant"])

    def turn(self, s, phone, talk, line_id=None):
        """回線ごとの処理の loop の何回分か（かけなくなるまで。loop と同じく、毎回の頭で回線の欄を聞く＝one_round）。line_id＝ほかの回線。"""
        line_id = line_id or s["line"]
        self.lease(s, line=line_id, seconds=3600)
        for _ in range(8):
            got = line.one_round(self.w.a, SYSTEM, s["tenant"], line_id, HOLDER, phone, talk)
            jobs.settle_ready(self.ctx(s))
            # loop と同じく、かけられなかった電話（called が無く、回線は空いた＝free）のあとは、すぐ次の行へ（loop は数秒で次）。かける行が
            # 無い・休み・門の理由で待つときは、次の時点まで（⛔ 4段目の前：かけられなかった電話のあとも次の時点まで待たせ、次の行で
            # 決まるはずの続きに、15分のテスト番号が先に来た＝場面 a9）
            if got.pause or (got.called is None and not got.free):
                break
        self.w.a.in_tenant(SYSTEM, s["tenant"], watch.lasting_stops)

    def run_until(self, s, phone, talk, until, *, step=timedelta(minutes=10), night=timedelta(minutes=30), each=None, lines=None):
        """時計を step ずつ（夜は night ずつ）進め、毎回その回線をまわす（lines＝(回線, 電話の部品) の並び：毎回この順にまわす）。"""
        t = self.db_now()
        while t < until:
            for line_id, p in lines or [(s["line"], phone)]:
                self.turn(s, p, talk, line_id)
            if each is not None:
                each()
            self.compare(s)   # 各時点で、今の作りと影を比べる（tests/shadow.py）
            t += step if 8 <= t.astimezone(JST).hour < 22 else night
            self.jump(t)

    # ── 読む
    def ended(self, r):
        return self.tx(r, lambda tx: next_action.current(tx, r["task"]))["end_reason"]

    def line_stops(self, s):
        """その回線が休みに入った（回線のせい・声の不調。回線の欄の移った記録＝DESIGN.md 17-1-1）回の並び。"""
        return owner_access().in_tenant(SYSTEM, s["tenant"], lambda tx: tx.all(
            "SELECT move_id FROM salescall.state_moves WHERE subject = 'line' AND key = %s AND to_stage IN ('line_rest', 'audio_rest')"
            " AND from_stage NOT IN ('line_rest', 'audio_rest') ORDER BY move_id", (s["line"],)))

    def number_n(self, r):
        """その行の番号の、番号のせいと決まった続きの数（番号の欄＝17-1-3）。"""
        return self.tx(r, lambda tx: health.number_of(tx, r["contact"]).n)

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
            self.assertEqual(self.number_n(r), 0)
        blames = {self.tx(s, lambda tx, a=a: health.blame_of(tx, a["attempt_id"])) for a in self.dialed(s)}
        self.assertEqual(blames - {"pending"}, {"line"})
        self.assertLessEqual(len(phone.rings), 12)                               # 27時間で、決めた間より詰めない
        tests = self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: [r["tested_at"] for r in tx.all(
            "SELECT tested_at FROM salescall.line_ring_tests WHERE line_id = %s ORDER BY test_id", (s["line"],))])
        self.assertTrue(all((b - a) >= timedelta(minutes=15) for a, b in zip(tests, tests[1:])), tests)

    # ── 見直し21a の場面（0143：鳴らなかった電話のせいは1つの道・テスト番号は1回鳴れば再開・分からない電話の行は決まるまで休む）
    def ring_tests(self, s):
        """その回線のテスト番号の記録（日本時間の時刻・目的・答え）。"""
        return self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: [
            (r["tested_at"].astimezone(JST), r["purpose"], r["rang"]) for r in tx.all(
                "SELECT tested_at, purpose, rang FROM salescall.line_ring_tests WHERE line_id = %s ORDER BY test_id", (s["line"],))])

    def tries(self, s, rows):
        """その行の番号へかけた（発信まで進んだ）数。"""
        phones = {r["phone"] for r in rows}
        return len([a for a in self.dialed(s) if a["phone_e164"] in phones])

    def assert_never_outside(self, tests, start, end):
        for at, _, _ in tests:
            self.assertTrue(start <= at.strftime("%H:%M") < end, (at, start, end))

    def test_a1_three_bad_numbers_side_by_side_end_as_the_numbers_without_stopping_the_line(self):
        """場面 a1：かけられない番号が3つ並ぶ（ほかの3行は話して完了）・回線は元気・テスト番号は終日。月曜9時から金曜18時まで：3つとも
        番号のせいで、決まった順の4回で取りやめになる（それより多くかけない）。回線は一度も止まらず、回線の異常にも数えない。"""
        s, rows = self.start(6)
        self.give_test_number(s)
        bad = rows[3:]
        phone = Phone(bad={r["phone"] for r in bad})
        self.run_until(s, phone, Talk(), self.db_now() + timedelta(days=4, hours=9))
        self.assertEqual((self.line_stops(s), self.line_faults(s)), ([], 0))
        self.assertEqual([self.ended(r) for r in rows[:3]], ["completed"] * 3)
        self.assertEqual([self.ended(r) for r in bad], ["number_unusable"] * 3)
        self.assertEqual(self.tries(s, bad), 12)                                # どれも4回
        self.assertLessEqual(len(phone.rings), 8)                               # かける行が残っていないときだけ

    def test_a2_without_a_test_number_bad_numbers_wait_and_the_line_never_stops(self):
        """場面 a2：かけられない番号 X・Y が並ぶ・テスト番号が無い。⛔ 0129〜0141：違う番号が2つ続けて鳴らないと回線のせいとして
        回線を止め、5日で停止29回。いま：次の行がつながれば番号のせい。かける行が残っていないあいだは、決めずに待ち（前に鳴った電話で
        番号のせいとも決めない＝壊れた回線のお客様の行を失わない）、契約者と管理者に「テスト番号が無いので決められません」と1回知らせる。
        回線は止めない。"""
        s, rows = self.start(5)
        x, y = rows[3], rows[4]
        phone = Phone(bad={x["phone"], y["phone"]})
        self.run_until(s, phone, Talk(), self.db_now() + timedelta(days=2, hours=9))
        self.assertEqual((self.line_stops(s), self.line_faults(s), self.notes(s, "stopped")), ([], 0, 0))
        self.assertEqual([self.ended(r) for r in rows[:3]], ["completed"] * 3)
        for r in (x, y):
            self.assertIsNone(self.ended(r))                                    # 行は失わない（決まるまで休む）
            self.assertEqual(self.number_n(r), 0)
        self.assertEqual(self.tries(s, [x, y]), 2)                              # 1回ずつかけて、決まるまで待つ
        self.assertEqual(self.notes(s, "line_undecided"), 1)
        self.assertEqual(phone.rings, [])

    def test_a3_test_number_only_at_noon_never_stops_a_healthy_line_in_the_morning(self):
        """場面 a3：テスト番号は 12:00〜13:00 だけ・X・Y（かけられない）が元気な行のあいだに並ぶ。⛔ 0141：時間の外は「違う番号2つで回線の
        せい」に戻り、朝に回線を止め、昼まで元気な行にかけなかった。いま：次の行がつながれば番号のせい。かける行が無いあいだは、時間の
        始まりまで決めずに待つ。回線は止めず、テスト番号は時間の外に鳴らさない。X・Y は決まった順の4回で取りやめ。"""
        s, rows = self.start(6)
        self.give_test_number(s, (("12:00", "13:00"),))
        x, y = rows[1], rows[2]
        phone = Phone(bad={x["phone"], y["phone"]})
        self.run_until(s, phone, Talk(), self.db_now() + timedelta(days=4, hours=9))
        self.assertEqual((self.line_stops(s), self.line_faults(s)), ([], 0))
        self.assertEqual([self.ended(r) for r in rows if r not in (x, y)], ["completed"] * 4)
        self.assertEqual([self.ended(r) for r in (x, y)], ["number_unusable"] * 2)
        self.assertEqual(self.tries(s, [x, y]), 8)
        self.assert_never_outside(self.ring_tests(s), "12:00", "13:00")

    def test_a7_the_test_numbers_hours_ending_between_two_bad_numbers_stops_nothing(self):
        """場面 a7：テスト番号は 9:00〜12:00 だけ。11:55 から X・Y（かけられない）、そのあと元気な3行。⛔ 0141：X で印を置いたあと
        時間が過ぎると「違う番号2つ」で回線を止めた。いま：元気な行がつながれば番号のせい。回線は止めない。時間の外に鳴らさない。"""
        monday = self.next_monday_9()
        s, rows = self.start(5, at=monday.replace(hour=11, minute=55))
        self.give_test_number(s, (("09:00", "12:00"),))
        x, y = rows[0], rows[1]
        phone = Phone(bad={x["phone"], y["phone"]})
        self.run_until(s, phone, Talk(), monday.replace(hour=18))
        self.assertEqual((self.line_stops(s), self.line_faults(s)), ([], 0))
        self.assertEqual([self.ended(r) for r in rows[2:]], ["completed"] * 3)
        self.assert_never_outside(self.ring_tests(s), "09:00", "12:00")

    def test_a9_bad_numbers_first_are_decided_by_the_next_row_without_the_test_number(self):
        """場面 a9：一覧の先頭に、かけられない番号 X・Y、そのあと話せる4行（どれも、いまかけてよい）。⛔ 0141：違う番号が2つ続くと、次の
        行がすぐつながるのに管理者の電話（テスト番号）を鳴らした。いま：次の行がつながって番号のせいと決まる（テスト番号は鳴らさない）。
        X・Y は決まった順の4回で取りやめ。テスト番号を鳴らすのは、X・Y のほかにかける行が無いときだけ。"""
        s, rows = self.start(6)
        self.give_test_number(s)
        x, y = rows[0], rows[1]
        phone = Phone(bad={x["phone"], y["phone"]})
        self.run_until(s, phone, Talk(), self.db_now() + timedelta(days=3, hours=8))
        self.assertEqual([self.ended(r) for r in rows[2:]], ["completed"] * 4)
        first = [a for a in self.dialed(s) if a["phone_e164"] in (x["phone"], y["phone"])][:2]
        self.assertEqual([self.tx(s, lambda tx, a=a: health.blame_of(tx, a["attempt_id"])) for a in first], ["number"] * 2)
        done = self.tx(s, lambda tx: tx.one("SELECT max(ended_at) AS t FROM salescall.attempts WHERE task_id = ANY(%s)",
                                            ([r["task"] for r in rows[2:]],))["t"])
        self.assertTrue(all(at > done for at, _, _ in self.ring_tests(s)), (done, self.ring_tests(s)))   # ほかの行があるあいだは鳴らさない
        self.assertEqual([self.ended(r) for r in (x, y)], ["number_unusable"] * 2)
        self.assertEqual(self.tries(s, [x, y]), 8)
        self.assertEqual((self.line_stops(s), self.line_faults(s)), ([], 0))
        self.assertLessEqual(len(phone.rings), 4)

    def test_a4_a_broken_line_whose_test_number_cannot_be_told_apart_waits_and_tells_once(self):
        """場面 a4：回線が壊れ、テスト番号の答えはいつも「見分けられない」。月曜9時から水曜18時まで：どの行も番号のせいにならず
        （取りやめない）、回線のせいとも決めない（証拠が無い）。契約者と管理者には、その続きについて1回だけ知らせ、電話をつなぐ部品の不具合
        として直す担当（CL-2）へ修正依頼を出す。テスト番号を鳴らす間は 15→30→…→240分（詰めない）。"""
        s, rows = self.start(5)
        self.give_test_number(s)
        phone = Phone(broken=True, answer=UNKNOWN)
        self.run_until(s, phone, Talk(), self.db_now() + timedelta(days=2, hours=9))
        for r in rows:
            self.assertNotEqual(self.ended(r), "number_unusable")
            self.assertEqual(self.number_n(r), 0)
        self.assertEqual(self.notes(s, "line_undecided"), 1)
        asked = [r for r in owner_access().in_system(SYSTEM, lambda tx: fix_requests.open_requests(tx, "CL-2"))
                 if r["kind"] == "test_ring_undecided"]
        self.assertEqual(len(asked), 1)
        tests = [at for at, _, _ in self.ring_tests(s)]
        ladder = [15, 30, 60, 120, 240]                                         # 見分けられないが続くあいだ（最後の長さで止まる）
        for i, (a, b) in enumerate(zip(tests, tests[1:])):
            self.assertGreaterEqual(b - a, timedelta(minutes=ladder[min(i, len(ladder) - 1)]), (i, tests))
        self.assertLessEqual(len(tests), 1 + 4 + (57 * 60 - sum(ladder[:4])) // 240 + 1)   # 2日と9時間で 19 回まで

    def test_a6_a_line_broken_for_two_hours_loses_nothing_and_comes_back_with_one_ring(self):
        """場面 a6：回線が月曜9〜11時だけ壊れ、そのあと直る（テスト番号は終日・1分ごとにまわす）。⛔ 0129〜0142：(1) 締めの時点で分からない
        電話も番号のせいと同じ決まった順で予定を書き、6行とも次の日へ送られた (2) 再開には確かめの成功が3回要り、約2分で3回、管理者の電話を
        鳴らした。いま：分からない電話の行は決まるまで休み（予定はそのまま）、回線のせいと決まれば、かけた予定のまま。直れば、テスト番号が
        1回鳴って再開し、その日のうちにどの行も話して完了する。"""
        s, rows = self.start(6)
        self.give_test_number(s)
        phone = Phone(broken=True)
        monday = self.db_now().astimezone(JST)
        self.run_until(s, phone, Talk(), monday.replace(hour=11), step=timedelta(minutes=1))
        self.assertTrue(self.line_stops(s))
        phone.broken = False
        self.run_until(s, phone, Talk(), monday.replace(hour=14), step=timedelta(minutes=1))
        self.assertEqual([self.ended(r) for r in rows], ["completed"] * 6)    # その日のうちに（次の日へ送らない）
        resumed = [t for t in self.ring_tests(s) if t[1] == "resume_check" and t[2]]
        self.assertEqual(len(resumed), 1)                                       # 1回鳴れば再開
        for r in rows:
            self.assertEqual(self.number_n(r), 0)

    def test_a10_a_promise_during_a_short_line_outage_keeps_its_mark_and_is_kept_that_day(self):
        """場面 a10：回線が 14:50〜15:30 だけ壊れる（テスト番号は終日）。P は 15:00 ちょうどの約束、ほかの5行は 14:50 から。⛔ 0129〜0141：
        締めの時点で分からない電話の予定を、番号のせいと同じ決まった順で書き、約束の印が外れて「ふつうの電話」になった。いま：P の行は
        決まるまで休み、回線のせいと決まれば、かけた予定（約束）のまま。回線が直ったら、その日のうちに約束の電話としてかけて完了する。"""
        monday = self.next_monday_9()
        s, rows = self.start(6, at=monday.replace(hour=14, minute=50))
        self.give_test_number(s)
        p = rows[0]
        at = monday.replace(hour=15)
        self.tx(p, lambda tx: support.schedule_call(tx, p["task"], at, contact_id=p["contact"], cause="試験：約束", promised=True,
                                                     callee_at=at))
        phone = Phone(broken=True)
        self.run_until(s, phone, Talk(), monday.replace(hour=15, minute=30), step=timedelta(minutes=2))
        phone.broken = False
        self.run_until(s, phone, Talk(), monday.replace(hour=18), step=timedelta(minutes=2))
        self.assertEqual([self.ended(r) for r in rows], ["completed"] * 6)
        done = self.tx(p, lambda tx: tx.one("SELECT promised, salescall.jst(claimed_at)::date AS d FROM salescall.attempts"
                                             " WHERE task_id = %s AND state = 'ended'", (p["task"],)))
        self.assertEqual((done["promised"], done["d"]), (True, monday.date()))   # 約束の電話として、その日のうちに
        plans = self.tx(p, lambda tx: [r["promised"] for r in tx.all(
            "SELECT promised FROM salescall.task_next_actions WHERE task_id = %s AND kind = 'call' AND seq > 1 ORDER BY seq",
            (p["task"],))])
        self.assertTrue(all(plans), plans)                                      # 約束の印は外れない

    # ── 見直し22 の場面（0144）
    def add_row(self, s, *, contact=None, run=None):
        """行を1つ足す（contact＝同じ番号のお客様・run＝ほかの実施回）。いまかける予定にする。"""
        k = contact or self.w.contact(s["tenant"], s["client"], normalize(support.fictional_jp_fixed()).e164)
        t = self.w.task(s["tenant"], s["client"], run or s["run"], k)
        self.tx(s, lambda tx: next_action.schedule_now(tx, t, cause="試験"))
        r = dict(s, task=t, contact=k)
        r["phone"] = self.tx(r, lambda tx: tx.one("SELECT phone_e164 FROM salescall.contacts WHERE contact_id = %s", (k,))["phone_e164"])
        return r

    def calls_on(self, s, line_id):
        """その回線でかけた（発信まで進んだ）試行の、行・日本時間の時刻・状態。"""
        return owner_access().in_system(SYSTEM, lambda tx: [(r["task_id"], r["claimed_at"].astimezone(JST), r["state"]) for r in tx.all(
            "SELECT task_id, claimed_at, state::text AS state FROM salescall.attempts WHERE line_id = %s"
            " AND salescall.is_dialed(state) ORDER BY claimed_at", (line_id,))])

    def test_n2b_a_row_left_undecided_on_a_broken_line_is_called_by_a_healthy_line(self):
        """場面 n2b（見直し22 の 5）：回線 A は壊れ（どの番号もかけられない・テスト番号も鳴らない）、回線 B は元気。テスト番号は 12:00〜13:00
        だけ。P は 9:30 ちょうどの約束。A がかけて鳴らず、番号のせいか回線のせいかが分からない行も、B は、ふつうの決まりでかけてよい（門の
        awaiting_decision は、その電話をかけた回線にだけ効く＝0144。2026-10-24 管理者「回線の問題が明らかな場合は行は取りやめでなく一時中止
        だけ」＝止めるのは回線であって行ではない）。⛔ 0143：分からない行をどの回線にも出さず、9:30 の約束を12時まで待たせた。
        A の分からなかった電話は、B がかけたあとに締め（行の予定は B の電話の締めが決める）、行の結果はあとの電話（B）のもの。"""
        monday = self.next_monday_9()
        s, rows = self.start(4, at=monday)
        line_b = self.w.line(s["tenant"])
        self.give_test_number(s, (("12:00", "13:00"),))
        p = self.add_row(s)
        at = monday.replace(minute=30)
        self.tx(p, lambda tx: support.schedule_call(tx, p["task"], at, contact_id=p["contact"], cause="試験：約束", promised=True,
                                                     callee_at=at))
        rows.append(p)
        a, b = Phone(broken=True), Phone()
        lines = [(s["line"], a), (line_b, b)]                                   # 毎回 A が先（A が先に行を取る）
        self.run_until(s, None, Talk(), monday.replace(minute=40), step=timedelta(minutes=2), lines=lines)
        self.assertEqual([self.ended(r) for r in rows], ["completed"] * 5)     # 約束の P も、A が手をつけた行も、B がかけて完了
        on_a = {task for task, _, _ in self.calls_on(s, s["line"])}
        self.assertIn(p["task"], on_a)                                         # P は A が先に取って、鳴らなかった
        done = {task: (when, st) for task, when, st in self.calls_on(s, line_b)}
        self.assertEqual(done[p["task"]][1], "ended")
        self.assertLess(done[p["task"]][0], monday.replace(minute=40))        # 約束の回のうちに（12時まで待たせない）
        self.assertTrue(self.tx(p, lambda tx: tx.one("SELECT promised FROM salescall.attempts WHERE task_id = %s AND line_id = %s",
                                                      (p["task"], line_b))["promised"]))   # 約束の電話として
        tried = self.tx(p, lambda tx: tx.one("SELECT claimed_at FROM salescall.attempts WHERE task_id = %s AND line_id = %s",
                                              (p["task"], s["line"]))["claimed_at"])
        plans = self.tx(p, lambda tx: [r["kind"] for r in tx.all(
            "SELECT kind::text AS kind FROM salescall.task_next_actions WHERE task_id = %s AND decided_at > %s ORDER BY seq",
            (p["task"], tried))])
        self.assertEqual(plans, ["end"])                                       # A の電話の締めは予定を書かない（B の締めが決める）
        self.run_until(s, None, Talk(), monday.replace(hour=12, minute=30), step=timedelta(minutes=10), lines=lines)
        for r in rows:                                                         # 行の結果は、あとの電話（B）の結果
            got = self.tx(r, lambda tx, r=r: tx.one(
                "SELECT x.attempt_id, (SELECT a.line_id FROM salescall.attempts a WHERE a.attempt_id = x.attempt_id) AS line_id"
                " FROM salescall.latest_task_results x WHERE x.task_id = %s", (r["task"],)))
            self.assertEqual(got["line_id"], line_b)
        unsettled = self.tx(s, lambda tx: tx.one("SELECT count(*) AS n FROM salescall.attempts WHERE line_id = %s"
                                                 " AND salescall.is_dialed(state) AND NOT salescall.is_settled(attempt_id)",
                                                 (s["line"],))["n"])
        self.assertEqual(unsettled, 0)                                         # A の分からなかった電話も締めた

    def test_n11_an_undecided_call_does_not_hold_the_verification_stage(self):
        """場面 n11（見直し22 の 2）：検証モード・最初の段は2件。テスト番号が無い。回線 A が X（かけられない番号）にかけて分からないまま
        （A はそのあと鳴らさない）、回線 B が約束の2行で段の件数に達する（X ほかの行は、段が進むまで、どの回線もかけない）。お客様の
        ところで鳴らなかった電話は会話の品質にかかわらないので、段の評価は待たない（quality_stage.evaluate の1か所）：段は進み、残りの行
        にもかける。X は失わない（決まるまで休む）。⛔ 0143：締めていない電話が1つでもあると段を進めず、残りの行に永久にかけなかった。"""
        monday = self.next_monday_9()
        s, rows = self.start(5, at=monday)
        line_b = self.w.line(s["tenant"])
        x, good = rows[0], rows[1:]
        for r in good:                                                         # A が X を取るまでは、ほかの行はまだ（10分後）
            self.tx(r, lambda tx, r=r: support.schedule_call(tx, r["task"], monday.replace(minute=10), contact_id=r["contact"],
                                                             cause="試験", promised=False))
        self.put_client(s, "approval.human", True)
        self.override("quality.stage.sizes", [2, 100])
        for r in rows:
            self.tx(r, lambda tx, r=r: approval.approve_task(tx, r["task"]), OPERATOR)
        self.tx(s, lambda tx: quality_stage.open_stage(tx, s["version"], 1, 2, evidence="試験：最初の段は2件"), OPERATOR)
        a, b = Phone(bad={x["phone"]}), Phone(bad={x["phone"]})
        self.turn(s, a, Talk(), s["line"])                                     # 9:00：A が X にかけて鳴らない
        self.assertEqual([task for task, _, _ in self.calls_on(s, s["line"])], [x["task"]])
        now = self.db_now()
        for r in good[:2]:                                                     # 2行は約束（時刻の決まっていない約束）：B が先にかけて段を埋める
            self.tx(r, lambda tx, r=r: support.schedule_call(tx, r["task"], now, contact_id=r["contact"], cause="試験：約束",
                                                             promised=True))
        self.run_until(s, None, Talk(), monday.replace(hour=12), step=timedelta(minutes=5), lines=[(line_b, b), (s["line"], a)])
        self.assertEqual({task for task, _, _ in self.calls_on(s, line_b)[:2]}, {good[0]["task"], good[1]["task"]})   # 段を埋めた
        self.assertEqual([self.ended(r) for r in good], ["completed"] * 4)     # 段が進み、残りの行にもかけた
        stages = self.tx(s, lambda tx: [r["stage_no"] for r in tx.all(
            "SELECT stage_no FROM salescall.version_stages WHERE version_id = %s ORDER BY stage_id", (s["version"],))])
        self.assertEqual(stages[-1], 2)
        self.assertIsNone(self.ended(x))                                       # X は失わない（番号の4回の順で続く）

    def test_n6_one_number_on_two_rows_keeps_the_numbers_four_tries(self):
        """場面 n6（見直し22 の 9）：同じ番号 X（かけられない）が2つの行にある（同じお客様・違う実施回）。回線は元気・テスト番号は終日。
        番号の4回（並び dial.failed_retry_days の初め＝当日2回・翌日1回・2日後1回）は番号ごとの決まり：次にかけてよい時刻は番号ごとの
        1か所（attempts.failed_wait）で決め、門と次の動きがそれを読む（行ごとに別々に決めない）。月曜9時から金曜18時まで：X へかけるのは、
        月曜2回・火曜1回・木曜1回の4回だけ。2つの行とも「番号が使えない」で終わる。⛔ 0129〜0143：もう1つの行の予定は、番号の続きが
        伸びる前に書いたままだったので、月曜に3回かけ、火曜の1回で「番号が使えない」になった。"""
        s, rows = self.start(4)
        self.give_test_number(s)
        x1 = rows[3]
        x2 = self.add_row(s, contact=x1["contact"], run=self.w.run(s["tenant"], s["client"], s["business"], s["version"]))
        phone = Phone(bad={x1["phone"]})
        monday = self.db_now().astimezone(JST)
        self.run_until(s, phone, Talk(), monday + timedelta(days=4, hours=9))
        days = [when.date() - monday.date() for task, when, _ in self.calls_on(s, s["line"]) if task in (x1["task"], x2["task"])]
        self.assertEqual([d.days for d in days], [0, 0, 1, 3])
        self.assertEqual([self.ended(r) for r in (x1, x2)], ["number_unusable"] * 2)
        self.assertEqual([self.ended(r) for r in rows[:3]], ["completed"] * 3)
        for r in (x1, x2):                                                     # 門が断ったら、次の動きが番号の順で1回書き直す（書き直し続けない）
            plans = self.tx(r, lambda tx, r=r: tx.one("SELECT count(*) AS n FROM salescall.task_next_actions WHERE task_id = %s",
                                                      (r["task"],))["n"])
            self.assertLessEqual(plans, 10, r["task"])

    def test_n10_a_test_the_pc_never_tries_is_told_once(self):
        """場面 n10（見直し22 の 9）：回線が壊れ、テスト番号の頼みに PC の部品がいつも答えない（試さなかった＝withheld）。決めにいくテストが
        試されないまま、あける長さの最初（15分）を過ぎても試せないときは、「見分けられない」と同じ知らせを、同じ続きで1回出し、電話を
        つなぐ部品の直す担当へ修正依頼を出す（状態の表の L-12・L-19＝【当(9)】。入口が行う）。それより前は知らせない。行は失わない。
        ⛔ 0143：行が決まらないまま、だれにも知らされなかった。"""
        from salescall.link.phone import CallWithheld

        class Withholds(Phone):
            def test_ring(self, phone_):
                self.rings.append(phone_)
                raise CallWithheld("試験：PC の部品が試さない")
        s, rows = self.start(5)
        self.give_test_number(s)
        phone = Withholds(broken=True)
        monday = self.db_now().astimezone(JST)
        self.run_until(s, phone, Talk(), monday.replace(minute=14), step=timedelta(minutes=1))
        self.assertEqual(phone.rings, [])                                      # 最初の鳴らなかった電話から15分は試さない（【当(5)】）
        self.assertEqual(self.notes(s, "line_undecided"), 0)                   # 15分より前は知らせない
        self.run_until(s, phone, Talk(), monday.replace(hour=11), step=timedelta(minutes=5))
        self.assertTrue(phone.rings)                                           # 試そうとした
        self.assertEqual(self.notes(s, "line_undecided"), 1)                   # 同じ続きで1回
        asked = [r for r in owner_access().in_system(SYSTEM, lambda tx: fix_requests.open_requests(tx, "CL-2"))
                 if r["kind"] == "test_ring_undecided"]
        self.assertEqual(len(asked), 1)
        self.assertEqual(self.ring_tests(s), [])                               # 試さなかったテストは記録しない
        for r in rows:
            self.assertIsNone(self.ended(r))                                   # 行は失わない（決まるまで休む）

    def test_b5b_a_line_whose_voice_is_broken_is_stopped_again_with_longer_waits(self):
        """場面 b5b（2026-10-29 管理者の決定。0144）：回線の声の受け渡しが壊れ、どのお客様とも「声が聞き取れない」で終わる（テスト番号は
        鳴る＝発信はできる）。テスト番号では声が通るかを確かめられないので、1回鳴れば再開し、また聞き取れなければ止め直し、次に再開を
        試すまで 15→30→60→120→240分あける（core/watch.checkable の1か所。並びは line.test_ring_gaps）。月曜9時〜18時：止める・再開の
        回数と、お客様へのお詫びの電話の本数が減る。⛔ 0143 まで：止めてはすぐ再開し、停止13回・お詫び40本だった。"""
        class PoorAudio:
            def run(self, access, actor, claim, *, tenant_id, client_id, call=None):
                call.beat()
                access.in_client(actor, tenant_id, client_id, lambda tx: [
                    attempts.move(tx, claim.attempt_id, st) for st in ("ringing", "answered", "in_conversation")])
                call.end_for_trouble("poor_audio")
        s, rows = self.start(8)
        self.give_test_number(s)
        phone = Phone()
        monday = self.db_now().astimezone(JST)

        def verified():   # 通話後の検証（会話の担当の部品）が済む
            for a in self.tx(s, lambda tx: tx.all("SELECT attempt_id FROM salescall.attempts WHERE verification = 'pending'"
                                                  " AND NOT salescall.is_active(state)")):
                self.tx(s, lambda tx, a=a: attempts.record_verification(tx, a["attempt_id"], summary=CLEAN))
            jobs.settle_ready(self.ctx(s))
        self.run_until(s, phone, PoorAudio(), monday.replace(hour=18), step=timedelta(minutes=5), each=verified)
        resumes = [at for at, purpose, rang in self.ring_tests(s) if purpose == "resume_check" and rang]
        self.assertLessEqual(len(self.line_stops(s)), 7, resumes)              # 止める回数が減る（前は13回）
        self.assertLessEqual(getattr(phone, "apologized", 0), 25)             # お詫びの電話が減る（前は40本）
        ladder = [15, 30, 60, 120, 240]
        for i, (a, b) in enumerate(zip(resumes, resumes[1:])):                 # 止め直すたびに、並びの次の長さをあける
            self.assertGreaterEqual(b - a, timedelta(minutes=ladder[min(i, len(ladder) - 1)]), (i, resumes))
        for r in rows:
            self.assertNotEqual(self.ended(r), "number_unusable")              # 行は失わない


    # ── 見直し20〜22 の場面（作業の場所にあった life_r20・life_r21・life_r22 から移した。DESIGN.md 17章の3段目で、影と比べるため。
    #    比べる時間を縮めたものがある。通話ごとの口の場面＝W は 17章の外なので移していない）
    def note_version(self):
        """動いているプログラムの版が替わった（中央の見守り役が書く＝supervisor/updates.note）。"""
        from salescall.supervisor import updates
        import uuid
        self.w.a.in_system(SYSTEM, lambda tx: updates.note(tx, f"試験の版-{uuid.uuid4().hex}"))

    def fixed(self, s):
        """決まった間隔の仕事 fixed_stops（直した版で、こちらの不具合の停止を解く・状態の欄へ直した版を渡す）。"""
        jobs.fixed_stops(self.ctx(s))

    def report_fixed(self, kind):
        """直す担当が、その種類の修正依頼を直したと記録する。"""
        for r in owner_access().in_system(SYSTEM, lambda tx: tx.all(
                "SELECT request_id FROM salescall.fix_requests q WHERE kind = %s AND NOT EXISTS"
                " (SELECT 1 FROM salescall.fix_reports r WHERE r.request_id = q.request_id)", (kind,))):
            self.w.a.in_system(SYSTEM, lambda tx, r=r: fix_requests.report_fixed(tx, r["request_id"], "試験：直した"))

    def tenant_stops(self, s):
        return self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: [r["cause"] for r in tx.all(
            "SELECT cause::text AS cause FROM salescall.open_stop_events WHERE scope = 'tenant'")])

    def resting(self, r):
        return self.tx(r, lambda tx: health.task_of(tx, r["task"]).stage is TaskStage.RESTING)

    def awaiting(self, r):
        return self.tx(r, lambda tx: health.task_of(tx, r["task"]).stage is TaskStage.AWAIT_RECOVERY)

    def troubles_of(self, r):
        return self.tx(r, lambda tx: [x["trouble"] for x in tx.all(
            "SELECT trouble FROM salescall.attempts WHERE task_id = %s AND salescall.is_dialed(state) ORDER BY claimed_at", (r["task"],))])

    def test_b1_our_trouble_twice_on_one_row_rests_it(self):
        """場面 b1（見直し21a）：X だけ、話し始めたあと毎回 AI の異常（ほかの3行は完了）。X は2回かけて休み、休み始めの知らせは1回。"""
        s, rows = self.start(4)
        x = rows[0]
        self.run_until(s, Phone(), Script({x["phone"]: "trouble:ai_malfunction"}, default="complete"),
                       self.db_now() + timedelta(hours=9))
        self.assertTrue(self.resting(x))
        self.assertEqual(self.troubles_of(x), ["ai_malfunction"] * 2)
        self.assertEqual([self.ended(r) for r in rows[1:]], ["completed"] * 3)
        self.assertEqual(self.notes(s, "task_resting"), 1)

    def test_b2_the_same_trouble_on_three_rows_stops_until_the_fixed_version(self):
        """場面 b2（見直し21a）：最初の3行が続けて AI の異常（ほかの2行は完了）：契約者の新しい電話を止める。版が替わっただけでは解かない。
        直したと記録して版が替わると解け、休んだ行も戻り、5行とも完了する。"""
        s, rows = self.start(5)
        monday = self.db_now().astimezone(JST)
        talk = Script({r["phone"]: "trouble:ai_malfunction" for r in rows[:3]}, default="complete")
        self.run_until(s, Phone(), talk, monday.replace(hour=12))
        self.assertEqual(self.tenant_stops(s), ["program_trouble"])
        self.note_version()
        self.fixed(s)
        self.assertEqual(self.tenant_stops(s), ["program_trouble"])
        self.report_fixed("call_ai_malfunction")
        self.note_version()
        self.fixed(s)
        self.assertEqual(self.tenant_stops(s), [])
        talk.how = {}
        self.run_until(s, Phone(), talk, monday.replace(hour=18))
        self.assertEqual([self.ended(r) for r in rows], ["completed"] * 5)

    def test_b3_the_same_trouble_with_good_talks_between_does_not_stop(self):
        """場面 b3（見直し21a）：同じ理由の行が3つあるが、あいだに話し終えた電話がはさまる（X・良・Y・良・Z）：止めない。"""
        s, rows = self.start(6)
        talk = Script({r["phone"]: "trouble:cannot_proceed" for r in (rows[0], rows[2], rows[4])}, default="complete")
        self.run_until(s, Phone(), talk, self.db_now() + timedelta(hours=9))
        self.assertEqual(self.tenant_stops(s), [])

    def test_b4_poor_audio_with_the_same_customer_is_not_our_trouble(self):
        """場面 b4（見直し21a）：X は毎回「声が聞き取れない」で終える（ほかは完了）。こちらの不具合に数えず（修正依頼なし・行は休まない）、
        同じお客様なので回線も止めない。"""
        asked = lambda: owner_access().in_system(SYSTEM, lambda tx: tx.one(   # noqa: E731 修正依頼の数（試験の場面をまたいで数える）
            "SELECT count(*) AS n FROM salescall.fix_requests")["n"])
        before = asked()
        s, rows = self.start(3)
        x = rows[0]
        self.run_until(s, Phone(), Script({x["phone"]: "trouble:poor_audio"}, default="complete"),
                       self.db_now() + timedelta(hours=9))
        self.assertEqual(self.line_stops(s), [])
        self.assertFalse(self.resting(x))
        self.assertEqual(asked(), before)

    def test_b8_a_version_without_the_fix_keeps_the_rest(self):
        """場面 b8（見直し21a。今の作りの復旧の決まり＝0143 に合わせて、ほかの2行は話して完了）：X が2回こちらの不具合で休む。版だけ
        替わる（直したと記録しない）：X は休んだまま。直したと記録して版が替わると、X も戻って完了する。"""
        s, rows = self.start(3)
        monday = self.db_now().astimezone(JST)
        x = rows[0]
        talk = Script({x["phone"]: "trouble:ai_malfunction"}, default="complete")
        self.run_until(s, Phone(), talk, monday.replace(hour=12))
        self.assertTrue(self.resting(x))
        self.note_version()
        self.fixed(s)
        self.run_until(s, Phone(), talk, monday.replace(hour=14))
        self.assertTrue(self.resting(x))
        self.report_fixed("call_ai_malfunction")
        self.note_version()
        self.fixed(s)
        talk.how[x["phone"]] = "complete"
        self.run_until(s, Phone(), talk, monday.replace(hour=16))
        self.assertEqual(self.ended(x), "completed")

    def test_b9_our_trouble_before_the_answer_on_the_only_row_waits_for_recovery(self):
        """場面 b9（見直し21a）：1行だけ。X は呼び出しの途中でこちらの不具合（出る前）。ほかに話し終える電話も直した版も無いので、
        その行は復旧を待ったまま（失わない）。"""
        s, rows = self.start(1)
        x = rows[0]
        self.run_until(s, Phone(), Script({x["phone"]: ["ring_trouble:cannot_proceed", "complete"]}, default="complete"),
                       self.db_now() + timedelta(hours=9))
        self.assertIsNone(self.ended(x))
        self.assertTrue(self.awaiting(x))

    def test_a5_two_lines_one_broken_and_one_bad_number(self):
        """場面 a5（見直し21a。時間を縮めた）：回線 A（壊れた）と B（元気）。8行のうち X だけかけられない番号。テスト番号は終日。月曜9時〜
        18時：ほかの7行は完了し、X は番号のせいの順のとちゅう（取りやめはまだ。回線のせいの分は数えない）。"""
        s, rows = self.start(8)
        line_b = self.w.line(s["tenant"])
        self.give_test_number(s)
        x = rows[2]
        lines = [(s["line"], Phone(broken=True)), (line_b, Phone(bad={x["phone"]}))]
        self.run_until(s, None, Talk(), self.db_now() + timedelta(hours=9), lines=lines)
        self.assertIsNone(self.ended(x))
        self.assertGreaterEqual(self.number_n(x), 1)
        self.assertEqual([self.ended(r) for r in rows if r is not x], ["completed"] * 7)

    def test_n3_without_a_test_number_a_line_broken_for_half_a_day(self):
        """場面 n3（見直し22。時間を縮めた）：テスト番号が無い。回線が月曜9時〜13時まで壊れ、そのあと直る（システムは知らない）。5行・
        18時まで：どの行も番号のせいにしない（失わない）。"""
        s, rows = self.start(5)
        phone = Phone(broken=True)
        monday = self.db_now().astimezone(JST)
        self.run_until(s, phone, Talk(), monday.replace(hour=13))
        phone.broken = False
        self.run_until(s, phone, Talk(), monday.replace(hour=18))
        for r in rows:
            self.assertNotEqual(self.ended(r), "number_unusable")

    def test_n4_without_a_test_number_a_new_row_decides_the_bad_numbers(self):
        """場面 n4（見直し22。時間を縮めた）：テスト番号が無い・元気な回線。X・Y（かけられない）が一覧の最後。15時に新しい行（話せる）が
        入る：その電話が鳴って、X・Y は番号のせいと決まる。"""
        s, rows = self.start(5)
        x, y = rows[3], rows[4]
        phone = Phone(bad={x["phone"], y["phone"]})
        monday = self.db_now().astimezone(JST)
        self.run_until(s, phone, Talk(), monday.replace(hour=15))
        self.add_row(s)
        self.run_until(s, phone, Talk(), monday.replace(hour=18))
        for r in (x, y):
            self.assertGreaterEqual(self.number_n(r), 1)

    def test_n7_a_bad_number_at_evening_is_decided_the_next_morning(self):
        """場面 n7（見直し22。時間を縮めた）：X（かけられない）を月曜 17:50 にかけ、ほかに行は無い。テスト番号は 9:00〜12:00 だけ。火曜
        13時まで：テスト番号は時間の外に鳴らさず、X は火曜の朝に番号のせいと決まる。"""
        monday = self.next_monday_9()
        s, rows = self.start(1, at=monday.replace(hour=17, minute=50))
        self.give_test_number(s, (("09:00", "12:00"),))
        x = rows[0]
        self.run_until(s, Phone(bad={x["phone"]}), Talk(), monday + timedelta(days=1, hours=4))
        self.assert_never_outside(self.ring_tests(s), "09:00", "12:00")
        self.assertGreaterEqual(self.number_n(x), 1)

    def test_n8_rows_left_undecided_on_a_line_that_is_retired_are_called_by_a_new_line(self):
        """場面 n8（見直し22）：テスト番号なし。回線 A が壊れ、4行とも分からないまま休む。11時に回線 A を使わなくし（回線のせいと決まる）、
        回線 B（元気）を足す：4行とも B でかけて完了する。"""
        from salescall.core import registry
        s, rows = self.start(4)
        monday = self.db_now().astimezone(JST)
        self.run_until(s, Phone(broken=True), Talk(), monday.replace(hour=11))
        self.w.a.in_tenant(OPERATOR, s["tenant"], lambda tx: registry.retire_line(tx, s["line"], reason="試験：返した"))
        line_b = self.w.line(s["tenant"])
        self.run_until(s, None, Talk(), monday.replace(hour=18), lines=[(line_b, Phone())])
        self.assertEqual([self.ended(r) for r in rows], ["completed"] * 4)

    def test_r2_two_broken_lines_never_ring_the_test_number_together(self):
        """場面 r2（見直し20a。時間を縮めた）：回線が2本とも壊れる（同じ契約者）。テスト番号は終日。月曜9時〜13時（5分ごと）：同じ時刻に
        2本で鳴らさない（1つの契約者のテスト番号は1本ずつ）。"""
        s, rows = self.start(6)
        line_b = self.w.line(s["tenant"])
        self.give_test_number(s)
        a, b = Phone(broken=True), Phone(broken=True)
        self.run_until(s, None, Talk(), self.db_now().astimezone(JST).replace(hour=13), step=timedelta(minutes=5),
                       lines=[(s["line"], a), (line_b, b)])
        tests = [t for t, _, _ in self.ring_tests(s)] + [t for t, _, _ in self.ring_tests(dict(s, line=line_b))]
        self.assertEqual(len(tests), len(set(tests)))


class Script(Talk):
    """会話の処理の代わり（見直し20〜22 の筋書き）：番号ごとの筋書き（並びなら1本ずつ取り出し、最後の1つは残す）。'complete'＝話して
    完了（Talk）／'no_answer'＝出ない／'trouble:<理由>'＝出て話し始めたあと、その理由で終える（声が聞き取れないは poor_audio）／
    'ring_trouble:<理由>'＝呼び出しの途中（出る前）に、その理由で終える。"""

    def __init__(self, how=None, default="complete"):
        self.how, self.default = dict(how or {}), default

    def run(self, access, actor, claim, *, tenant_id, client_id, call=None):
        what = self.how.get(claim.phone_e164, self.default)
        if isinstance(what, list):
            what = what.pop(0) if len(what) > 1 else what[0]
        if call is not None:
            call.beat()
        if what == "complete":
            return Talk.run(self, access, actor, claim, tenant_id=tenant_id, client_id=client_id, call=call)

        def move(*sts):
            access.in_client(actor, tenant_id, client_id, lambda tx: [attempts.move(tx, claim.attempt_id, st) for st in sts])
        if what == "no_answer":
            move("ringing", "no_answer")
            return
        kind, _, reason = what.partition(":")
        answered = kind == "trouble"
        move(*(("ringing", "answered", "in_conversation") if answered else ("ringing",)))
        call.end_for_trouble(reason)
        if answered:
            access.in_client(actor, tenant_id, client_id, lambda tx: attempts.record_verification(tx, claim.attempt_id, summary=CLEAN))
