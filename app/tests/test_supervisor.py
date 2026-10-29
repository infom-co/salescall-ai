"""中央の見守り役の試験（DESIGN.md 5-4・5-5。supervisor/・core/leases.py・core/scheduler.py・表の作り 0063）。PC の部品とのつなぎは
tests/test_link.py。"""

from __future__ import annotations

import tempfile
import contextlib
import inspect
import threading
import time
import unittest
import uuid
from unittest import mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

from salescall.core import (approval, attempts, backup, business_rules, call_report, calling_hours, conversation_io, errors,
                            fix_requests, gate, help_texts, leases, migrate, next_action, notify, quality_stage, registry, ring_test,
                            scheduler, settings, stops, watch)
from salescall.core.phone import normalize
from salescall.core.access import Rejected
from salescall import supervisor
from salescall.supervisor import config, jobs, line, plugins, updates
from salescall.supervisor import main as main_mod
from salescall.supervisor.links import Attached, NoLinks
from salescall.supervisor.main import Supervisor
from tests import support
from tests.support import ADMIN, CLEAN, OPERATOR, SYSTEM, fictional_jp_fixed, rules_raw
from tests.test_gate import HOLDER, Base, FakeDriver
from tests.test_settle import rep


def until_true(test, seconds):
    """test() が真になるまで待ち、その値を返す（待っても真にならなければ None）。"""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        got = test()
        if got:
            return got
        time.sleep(0.05)
    return None


class Runner:
    """通話を最後まで進める部品の代わり（会話の担当の部品の形）：出て、話して、Q1 が取れて、ふつうに終わる。"""

    def __init__(self, fail: BaseException | None = None, unanswered: bool = False) -> None:
        self.fail, self.unanswered = fail, unanswered

    def run(self, access, actor, claim, *, tenant_id, client_id, call=None):
        if call is not None:
            call.beat()   # 「動いています」（通話を進める部品は、通話のあいだ1秒より短い間隔で知らせる）
        if self.unanswered:   # 呼び出しのあと、お客様が出る前に電話が終わった（電話の部品がそう知らせた）
            access.in_client(actor, tenant_id, client_id, lambda tx: [attempts.move(tx, claim.attempt_id, st)
                                                                       for st in ("ringing", "technical_error")])
            return
        if self.fail:
            access.in_client(actor, tenant_id, client_id, lambda tx: attempts.move(tx, claim.attempt_id, "ringing"))
            raise self.fail
        def work(tx):
            for st in ("ringing", "answered", "in_conversation", "ending", "ended"):
                attempts.move(tx, claim.attempt_id, st)
            conversation_io.write_item(tx, claim.attempt_id, conversation_io.ItemValue("Q1", "confirmed", None, "答えた"))
            conversation_io.save_report(tx, claim.attempt_id, rep().to_json())   # 会話の処理は、書き込む入口だけを通す
            attempts.record_verification(tx, claim.attempt_id, summary=CLEAN)
        access.in_client(actor, tenant_id, client_id, work)


def make_runner(line_id, driver):
    return Runner()


class StuckRunner:
    """通話を進める部品の代わり：1回知らせて呼び出しまで進め、そのまま固まって戻らない（release が立つまで）。"""
    release = threading.Event()

    def run(self, access, actor, claim, *, tenant_id, client_id, call=None):
        call.beat()
        access.in_client(actor, tenant_id, client_id, lambda tx: attempts.move(tx, claim.attempt_id, "ringing"))
        StuckRunner.release.wait(60)


STUCK_MADE: list = []


def make_stuck_runner(line_id, driver):
    """1つ目の通話の部品は戻らない（StuckRunner）。立て直したあとの部品は、ふつうに話して終える。"""
    STUCK_MADE.append(line_id)
    return StuckRunner() if len(STUCK_MADE) == 1 else Runner()


PROBED: list = []   # 試験の確かめの部品が確かめた停止


class Prober:
    def probe(self, stop):
        PROBED.append(stop["event_id"])
        return True, "試験：確かめの部品"


def make_prober():
    return Prober()


class FakeLinks(NoLinks):
    """PC の部品のつなぎの代わり（どの回線が、どの契約者のどの PC につながっているか）。"""

    def __init__(self, attached=None):
        self.attached = dict(attached or {})

    def lines(self):
        return dict(self.attached)

    def driver(self, line_id, at):
        return FakeDriver()


class SupervisorBase(Base):
    def cfg(self, s=None, **more):
        values = {"SALESCALL_DSN": self.w.a._conninfo, "SALESCALL_CALL_RUNNER": "tests.test_supervisor:make_runner", **more}
        return config.load(values)

    def links(self, *xs, pc=None):
        return FakeLinks({x["line"]: Attached(x["tenant"], pc or uuid.UUID(int=1)) for x in xs})

    def faults(self, s):
        """その回線の、回線の異常の記録（記録した順）。"""
        return [r["kind"] for r in self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: tx.all(
            "SELECT kind FROM salescall.error_events WHERE line_id = %s ORDER BY error_id", (s["line"],)))]

    def ctx(self, s, links=None):
        return jobs.Context(self.w.a, self.cfg(s), HOLDER, links or NoLinks(), s["tenant"])

    def take(self, s, holder=HOLDER, line_id=None):
        return self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: leases.take_line(tx, line_id or s["line"], holder))

    def state(self, s, attempt_id):
        return self.tx(s, lambda tx: tx.one("SELECT state::text AS s FROM salescall.attempts WHERE attempt_id = %s",
                                            (attempt_id,)))["s"]

    def numbered(self, s):
        """その契約者にテスト番号を入れる（架空の番号。返すのは、その番号）。鳴らしてよい時間は 0:00〜24:00（この仕事専用の電話の形＝
        時刻しだいで試験が変わらない。時間の外の試験は `ring_hours` で決める）。鳴らす間は1秒（続けて試すときは `next_ring` で待つ）。"""
        phone = normalize(fictional_jp_fixed()).e164
        self.w.a.in_tenant(OPERATOR, s["tenant"], lambda tx: settings.set_value(
            tx, "line.test_number", phone, scope="tenant", tenant_id=s["tenant"], status="decided", rationale="試験",
            review_when="試験"))
        self.set_ring_hours(s, ("00:00", "24:00"))
        self.w.a.in_tenant(OPERATOR, s["tenant"], lambda tx: settings.set_value(   # 鳴らす間は1秒（間そのものの試験は、それぞれで決める）
            tx, "line.test_ring_gaps", [1], scope="tenant", tenant_id=s["tenant"], status="decided", rationale="試験",
            review_when="試験"))
        return phone

    @staticmethod
    def next_ring():
        """テスト番号の鳴らす間（試験では1秒）が過ぎるまで待つ。"""
        time.sleep(1.1)

    def set_ring_hours(self, s, *ranges):
        """鳴らしてよい時間を、範囲（"HH:MM" の組）の並びにする（いくつでも）。"""
        self.w.a.in_tenant(OPERATOR, s["tenant"], lambda tx: settings.set_value(
            tx, "line.test_ring_hours", [list(r) for r in ranges], scope="tenant", tenant_id=s["tenant"], status="decided",
            rationale="試験", review_when="試験"))

    def ring_hours(self, s, *, inside):
        """鳴らしてよい時間を、いまを含む2時間（inside）か、いまを含まない1時間（2時間後から）にする（日本時間・時刻しだいで変わらない
        形。日をまたぐ範囲にもなる）。"""
        now = calling_hours.local(datetime.now(timezone.utc))
        start = now - timedelta(hours=1) if inside else now + timedelta(hours=2)
        end = start + timedelta(hours=2 if inside else 1)
        self.set_ring_hours(s, (start.strftime("%H:%M"), end.strftime("%H:%M")))

    def asked_supervisor(self, s):
        """その契約者で見守り役の誤りを記録し、CL-2 へ修正依頼が出ているか（同じ不具合の修正依頼は契約者をまたいで1つ＝
        2026-10-22 の見直しなので、依頼は種類で見て、どの契約者で起きたかは誤りの記録で見る）。返すのは、その契約者の誤りの文。"""
        asked = [r for r in self.w.a.in_system(SYSTEM, lambda tx: fix_requests.open_requests(tx, "CL-2"))
                 if r["kind"] == "supervisor"]
        seen = self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: [r["message"] for r in tx.all(
            "SELECT message FROM salescall.error_events WHERE source = 'supervisor' AND tenant_id = %s ORDER BY error_id",
            (s["tenant"],))])
        return seen if asked else []


class Leases(SupervisorBase):
    def test_one_supervisor_holds_a_line_and_another_takes_it_only_after_it_expires(self):
        s = self.ready()
        self.assertIsNotNone(self.take(s))                                     # 同じ見守り役は延ばせる
        self.assertIsNone(self.take(s, holder="ほかのPC#x"))                   # 生きて借りている回線は借りられない
        self.override("line.lease", 1)
        self.assertIsNotNone(self.take(s))
        time.sleep(1.2)
        self.assertIsNotNone(self.take(s, holder="ほかのPC#x"))                # 期限が切れたら借りられる
        other = self.w.tenant()
        self.assertIsNone(self.w.a.in_tenant(SYSTEM, other, lambda tx: leases.take_line(tx, s["line"], "x")))   # 契約者の外

    def test_jobs_are_taken_by_one_supervisor_and_only_by_the_system(self):
        key = f"試験の仕事:{uuid.uuid4()}"
        take = lambda who: self.w.a.in_system(SYSTEM, lambda tx: leases.take_job(tx, key, who, 60))   # noqa: E731
        self.assertEqual([take("A"), take("B"), take("A")], [True, False, True])
        self.w.a.in_system(SYSTEM, lambda tx: leases.return_job(tx, key, "A"))
        self.assertTrue(take("B"))
        for step in (lambda tx: leases.take_job(tx, key, "C", 60), lambda tx: leases.return_job(tx, key, "B")):
            with self.assertRaises(Rejected):                                  # 契約者・管理者は、借りる・返すをできない
                self.w.a.in_tenant(OPERATOR, self.w.tenant(), step)


class Recovery(SupervisorBase):
    def test_a_crashed_supervisor_cleans_up_its_own_lines_when_it_starts_again(self):
        """起動したら、前に落ちた自分の回線の、進行中の試行を終わらせる（予約のまま＝取り消し・発信してから＝結果不明）。"""
        s = self.ready()
        c = self.claim(s)
        self.assertTrue(line.dial_exit.dial(self.w.a, SYSTEM, s["tenant"], s["client"], c.attempt_id, FakeDriver()))
        got = self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: leases.recover(tx, HOLDER))
        self.assertEqual((got[0]["line_id"], got[0]["ended"]), (s["line"], 1))
        self.assertEqual(self.state(s, c.attempt_id), "technical_error")

    def test_only_expired_lines_of_a_former_hub_are_cleaned_up(self):
        s = self.ready()
        c = self.claim(s)
        self.assertEqual(self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: leases.recover(tx)), [])   # 生きている回線は触らない
        self.assertEqual(self.state(s, c.attempt_id), "claimed")
        self.lease(s, seconds=-1)                                              # 前の見守り役が落ちて、借用の期限が切れた
        got = self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: leases.recover(tx))
        self.assertEqual(got[0]["released"], 1)
        self.assertEqual(self.state(s, c.attempt_id), "released")


    def test_the_periodic_cleanup_leaves_its_own_lines_and_cleans_those_of_a_former_hub(self):
        """決まった間隔の後始末は、自分が借りている回線を、期限が切れていても終わらせない（データベースに届かないあいだに期限が
        切れても、まだ話している自分の通話を結果不明にしない）。前に動いていた中央の見守り役（名前が違う）の回線は、期限が切れたら
        後始末する（回線を借りるのは中央の見守り役だけ＝5-5。前の「落ちたままの PC の回線の後始末」は要らなくなった）。"""
        s = self.ready()
        c = self.claim(s)
        self.lease(s, seconds=-1)                                              # 自分の借用の期限が切れた（データベースに届かなかった）
        jobs.recover_expired(self.ctx(s))
        self.assertEqual(self.state(s, c.attempt_id), "claimed")
        jobs.recover_expired(jobs.Context(self.w.a, self.cfg(s), "新しい中央の見守り役#z", NoLinks(), s["tenant"]))
        self.assertEqual(self.state(s, c.attempt_id), "released")


class Candidates(SupervisorBase):
    def test_only_rows_the_gate_could_take_are_offered(self):
        s = self.ready()
        due = lambda: [r["task_id"] for r in self.tx(s, lambda tx: scheduler.due_tasks(tx, 10))]   # noqa: E731
        lines = lambda: self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: scheduler.clients_for_line(tx, s["line"]))   # noqa: E731
        self.assertEqual((due(), lines()), ([s["task"]], [s["client"]]))
        c = self.claim(s)
        self.assertEqual(due(), [])                                            # 進行中の発信がある
        self.tx(s, lambda tx: attempts.release(tx, [c.attempt_id]))
        self.tx(s, lambda tx: next_action.hold(tx, s["task"], cause="試験"))
        self.assertEqual(due(), [])                                            # 判定し直しを待っている
        self.tx(s, lambda tx: next_action.schedule_now(tx, s["task"], cause="試験"))
        e = self.tx(s, lambda tx: stops.stop(tx, cause="budget", reason="試験", tenant_id=s["tenant"], scope="client",
                                             client_id=s["client"]))
        self.assertEqual((lines(), due()), ([s["client"]], []))                 # 依頼者が止まっている（依頼者の取引で見る）
        self.tx(s, lambda tx: stops.resume(tx, e["event_id"], reason="試験"))
        self.tx(s, lambda tx: registry.set_client_active(tx, False, reason="試験"), OPERATOR)
        self.assertEqual(lines(), [])                                          # 無効の依頼者


class Rounds(SupervisorBase):
    def test_a_round_calls_one_row_and_the_settle_job_settles_it(self):
        s = self.ready()
        got = line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, FakeDriver(), Runner())
        self.assertIsNotNone(got.called)
        self.assertEqual(self.state(s, got.called), "ended")
        jobs.settle_ready(self.ctx(s))
        r = self.tx(s, lambda tx: tx.one("SELECT business_state FROM salescall.task_results WHERE attempt_id = %s",
                                         (got.called,)))
        self.assertEqual(r["business_state"], "completed")

    def test_a_frozen_call_is_ended_with_the_apology_and_a_long_one_is_not(self):
        """通話の長さでは切らない。通話を進める部品の「動いています」の知らせ（call.beat）が call.frozen_after（ユーザー：3秒。試験は
        1秒）途切れたときだけ、こちらのプログラムが固まったとして、おわびの2つの一言で終え、結果不明にして理由 frozen を残す（line._Call
        の1か所。データベースを通さない。2026-10-24 ユーザー：「20秒でも長すぎるでしょ」）。"""
        class Slow:
            """話している間、0.3秒ごとに「動いています」と知らせる（talking＝知らせる回数）。0回なら、1回知らせたあと固まる。"""
            def __init__(self, talking: int) -> None:
                self.talking, self.release = talking, threading.Event()

            def run(self, access, actor, claim, *, tenant_id, client_id, call=None):
                call.beat()
                access.in_client(actor, tenant_id, client_id, lambda tx: attempts.move(tx, claim.attempt_id, "ringing"))
                for _ in range(self.talking):
                    time.sleep(0.3)
                    call.beat()
                if not self.talking:
                    self.release.wait(10)                                     # 固まった（見張りが終えるまで戻らない）
                    raise RuntimeError("固まっていた")
                Runner().run(access, actor, claim, tenant_id=tenant_id, client_id=client_id, call=call)
        with mock.patch.object(line, "frozen_after", lambda tx: 1.0), mock.patch.object(line, "WATCH_EVERY", 0.05):
            s = self.ready()
            long = FakeDriver()
            got = line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, long, Slow(talking=8))   # 2.4秒＞1秒
            self.assertEqual(self.state(s, got.called), "ended")              # 動いている長い通話は切らない
            self.assertEqual(getattr(long, "apologized", 0), 0)
            self.assertFalse(supervisor.call_live(s["line"]))                # 終わった通話は「話している」に残らない
            t = self.ready()
            stuck, runner = FakeDriver(), Slow(talking=0)
            with mock.patch.object(line, "report") as reported:
                timer = threading.Timer(4.0, runner.release.set)
                timer.start()
                got = line.one_round(self.w.a, SYSTEM, t["tenant"], t["line"], HOLDER, stuck, runner)
                timer.cancel()
            row = self.tx(t, lambda tx: tx.one("SELECT state::text AS st, trouble FROM salescall.attempts WHERE attempt_id = %s",
                                               (got.called,)))
            self.assertEqual((row["st"], row["trouble"]), ("technical_error", "frozen"))   # 固まった通話は結果不明・理由つき
            self.assertEqual(stuck.apologized, 1)                             # おわびの2つの一言で、1回だけ終えた
            self.assertEqual(reported.call_args_list, [])                     # 見守り役の誤りに重ねない（落ちたのも同じ出来事）
            told = self.tx(t, lambda tx: [(r["kind"], r["message"]) for r in tx.all(
                "SELECT kind, message FROM salescall.error_events WHERE attempt_id = %s", (got.called,))])
            self.assertEqual(len(told), 1)                                    # こちらの不具合として1回だけ伝えた（0139）
            self.assertEqual(told[0][0], "call_frozen")
            self.assertIn("途切れました", told[0][1])

    def test_the_watch_covers_only_the_call_in_progress_lives_until_the_apology_ends_and_never_waits_on_writes(self):
        """通話ごとの口（line._Call の1か所。0136。⛔ 見直し19a）：(1) お客様が切って試行が終わったあとの片付けで知らせが途切れても、
        おわびせず、こちらのせいの印も付けない (2) かける前から「話している」（発信の出口がかけている途中も）。おわびして切っている
        あいだも「話している」（止める処理がおわびの途中で切らない）。切り終えたら外れる (3) 通話の途中の書き込みは通話ごとの書き手：
        データベースが遅くても（書き込み1つは call.frozen_after より短い＝進んでいる）、通話を進める流れは待たずに知らせを続け、元気な
        通話を切らない。戻ったら書き終えてから締める。"""
        class AfterEnd:
            def run(self, access, actor, claim, *, tenant_id, client_id, call=None):
                Runner().run(access, actor, claim, tenant_id=tenant_id, client_id=client_id, call=call)
                time.sleep(1.6)                                                # 終わったあとの片付けで、知らせが止まる

        class Frozen:
            def __init__(self):
                self.release = threading.Event()

            def run(self, access, actor, claim, *, tenant_id, client_id, call=None):
                call.beat()
                access.in_client(actor, tenant_id, client_id, lambda tx: attempts.move(tx, claim.attempt_id, "ringing"))
                self.release.wait(10)

        class Watched(FakeDriver):
            def __init__(self, line_id):
                super().__init__()
                self.line_id, self.seen = line_id, {}

            def start_call(self, phone):
                self.seen["dialing"] = supervisor.call_live(self.line_id)
                return super().start_call(phone)

            def apologize_and_hang_up(self):
                self.seen["apologizing"] = supervisor.call_live(self.line_id)
                time.sleep(0.5)
                super().apologize_and_hang_up()

        class SlowWrites:
            def run(self, access, actor, claim, *, tenant_id, client_id, call=None):
                call.beat()
                began = time.monotonic()
                for _ in range(2):
                    call.write(lambda tx: tx.execute("SELECT pg_sleep(0.6)"))   # データベースが遅い（合わせて1.2秒）

                def finish(tx):
                    for st in ("ringing", "answered", "in_conversation", "ending", "ended"):
                        attempts.move(tx, claim.attempt_id, st)
                    conversation_io.save_report(tx, claim.attempt_id, rep().to_json())
                    attempts.record_verification(tx, claim.attempt_id, summary=CLEAN)
                call.write(finish)
                self.waited = time.monotonic() - began
                for _ in range(8):                                             # 話している（知らせは途切れない）
                    time.sleep(0.2)
                    call.beat()
        with mock.patch.object(line, "frozen_after", lambda tx: 1.0), mock.patch.object(line, "WATCH_EVERY", 0.05):
            s = self.ready()
            d = Watched(s["line"])
            got = line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, d, AfterEnd())
            row = self.tx(s, lambda tx: tx.one("SELECT state::text AS st, trouble FROM salescall.attempts WHERE attempt_id = %s",
                                               (got.called,)))
            self.assertEqual((row["st"], row["trouble"], getattr(d, "apologized", 0)), ("ended", None, 0))   # (1)
            self.assertTrue(d.seen["dialing"])                                 # (2) かけている途中も「話している」
            t = self.ready()
            d, runner = Watched(t["line"]), Frozen()
            timer = threading.Timer(4.0, runner.release.set)
            timer.start()
            with mock.patch.object(line, "report"):
                got = line.one_round(self.w.a, SYSTEM, t["tenant"], t["line"], HOLDER, d, runner)
            timer.cancel()
            self.assertEqual((d.apologized, d.seen["apologizing"]), (1, True))   # (2) おわびの途中は「話している」
            self.assertFalse(supervisor.call_live(t["line"]))                  # 切り終えたら外れる
            u = self.ready()
            d, runner = FakeDriver(), SlowWrites()
            got = line.one_round(self.w.a, SYSTEM, u["tenant"], u["line"], HOLDER, d, runner)
            self.assertLess(runner.waited, 0.5)                                # (3) 書き込みを待たない
            row = self.tx(u, lambda tx: tx.one("SELECT state::text AS st, trouble FROM salescall.attempts WHERE attempt_id = %s",
                                               (got.called,)))
            self.assertEqual((row["st"], row["trouble"], getattr(d, "apologized", 0)), ("ended", None, 0))

    def test_a_call_with_a_grave_problem_is_ended_with_the_apology_and_called_back(self):
        """その電話そのものに甚大な問題（2026-10-24 ユーザー決定：意図しない状態・声が聞き取れない・決まりどおり進められない）：
        通話を進める部品が call.end_for_trouble(理由) を呼ぶと、おわびの2つの一言で終え、結果不明にして理由を残す（frozen と同じ道）。
        お客様が出たあとなら、判定は「途中で切れた」と読み（judge.cut_off）、復旧次第お詫びとともにすぐかけ直す（目的 resume）。
        2回目は何もしない。決まった理由のほか・frozen（見張りだけが使う）は断る。"""
        seen = {}

        class Troubled:
            def run(self, access, actor, claim, *, tenant_id, client_id, call=None):
                call.beat()
                access.in_client(actor, tenant_id, client_id, lambda tx: [
                    attempts.move(tx, claim.attempt_id, st) for st in ("ringing", "answered", "in_conversation")])
                for bad in ("frozen", "storage_stuck", "rude"):   # 見張りだけが使う理由・決まりに無い理由は断る
                    with self.assertRaises(ValueError):
                        call.end_for_trouble(bad)
                try:                                                          # データベースも、決まった4つのほかは断る
                    access.in_client(actor, tenant_id, client_id, lambda tx: tx.execute(
                        "UPDATE salescall.attempts SET state = 'technical_error', trouble = 'rude' WHERE attempt_id = %s",
                        (claim.attempt_id,)))
                except Rejected as e:
                    seen["rude"] = e.constraint
                call.end_for_trouble("poor_audio", "試験：声が途切れ、3回聞き返しても聞き取れない")   # 根拠の文（CL-1 の連絡帳 4-59）
                call.end_for_trouble("poor_audio")                            # 2回目は何もしない
                seen["ended"] = call.ended
            assertRaises = self.assertRaises

        s = self.ready()
        d = FakeDriver()
        got = line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, d, Troubled())
        self.assertTrue(seen["ended"])
        self.assertEqual(seen.get("rude"), "attempts_trouble_check")
        self.assertEqual(d.apologized, 1)
        row = self.tx(s, lambda tx: tx.one("SELECT state::text AS st, trouble FROM salescall.attempts WHERE attempt_id = %s",
                                           (got.called,)))
        self.assertEqual((row["st"], row["trouble"]), ("technical_error", "poor_audio"))
        self.tx(s, lambda tx: attempts.record_verification(tx, got.called, summary=CLEAN))
        jobs.settle_ready(self.ctx(s))
        nxt = self.tx(s, lambda tx: next_action.current(tx, s["task"]))
        self.assertEqual(nxt["purpose"], "resume")                           # お詫びとともにすぐかけ直す
        # 終えた理由は、進行中の通話を結果不明にするときに1回だけ（あとで変えない・ほかの状態には付けない）
        with self.assertRaises(Rejected) as e:
            self.tx(s, lambda tx: tx.execute("UPDATE salescall.attempts SET trouble = 'cannot_proceed' WHERE attempt_id = %s",
                                             (got.called,)))
        self.assertEqual(e.exception.constraint, "attempt_trouble_fixed")
        self.assertEqual(self.tx(s, lambda tx: tx.one("SELECT trouble_why FROM salescall.attempts WHERE attempt_id = %s",
                                                      (got.called,))["trouble_why"]), "試験：声が途切れ、3回聞き返しても聞き取れない")
        with self.assertRaises(Rejected) as e:                                 # 根拠の文も、あとで変えない（理由と同じ1回だけ）
            self.tx(s, lambda tx: tx.execute("UPDATE salescall.attempts SET trouble_why = '書き換え' WHERE attempt_id = %s",
                                             (got.called,)))
        self.assertEqual(e.exception.constraint, "attempt_trouble_fixed")
        self.assertFalse(self.tx(s, lambda tx: attempts.end_for_trouble(tx, got.called, "poor_audio")))

    def test_the_call_knows_by_itself_when_it_is_finishing_and_never_waits_a_fixed_time(self):
        """通話ごとの口（line._Call の1か所。0143・見直し22 の 1・3）：通話の部品が頼んだ終わりの移り（話し終えた ending・ended＝
        conversation_io.move_work）は、書き手が書き切るのを（書き手が進むあいだ）待ってから、データベースで進行中かを見る。決まった秒数は
        待たない（⛔ 0141：仮の設定 call.end_write_wait＝5秒まで書き手を待ち、本当に固まった通話のおわびが最大8秒遅れ、書き込みが5秒より
        遅いと話し終えた電話を固まったとした）。
        (1) 話し終えた（move_work で ending・ended を頼んだ）あと、データベースが遅く（書き込みは進んでいる）、片付けで知らせを止めた：
            おわびを流さず、取れた結果も消さない（見直し21a の場面 w5・w8）
        (2) 書き込みが待ち行列に残ったまま、通話の部品が固まった（終わりの移りは頼んでいない）：書き込みを待たず、すぐおわびして終える
        (3) 通話の部品が書き手を待っている（flush）あいだに、書き込み1つが call.frozen_after より長く終わらない（データベースが止まった）：
            記録を待たずに、すぐおわびして終え、出どころ「保存」として知らせ、保存の担当（CL-2）へ修正依頼を出す（storage_stuck）"""
        class Finishing:
            def __init__(self, slow, finish=True, freeze=1.6, beating=False, flush=False):
                self.slow, self.finish, self.freeze, self.release = slow, finish, freeze, threading.Event()
                self.beating = beating   # 知らせを続ける（元気な通話。終えられたら戻る）
                self.flush = flush       # 書いた結果を続けて読む（書き手を待つ）

            def run(self, access, actor, claim, *, tenant_id, client_id, call=None):
                call.beat()
                access.in_client(actor, tenant_id, client_id, lambda tx: [
                    attempts.move(tx, claim.attempt_id, st) for st in ("ringing", "answered", "in_conversation")])
                for s in self.slow:
                    call.write(lambda tx, s=s: tx.execute("SELECT pg_sleep(%s)", (s,)))   # データベースが遅い
                if self.finish:
                    call.write(conversation_io.move_work(claim.attempt_id, "ending"))    # 話し終えた（頼んだだけ）
                    call.write(conversation_io.move_work(claim.attempt_id, "ended"))
                    call.write(conversation_io.report_work(claim.attempt_id, rep().to_json()))
                    call.write(lambda tx: attempts.record_verification(tx, claim.attempt_id, summary=CLEAN))
                if self.flush:
                    call.flush()                                               # 書き手を待つ（データベースが止まれば、待つうちに終えられる）
                if self.beating:                                               # 元気に話している（終えられたら戻る）
                    while not call.ended and not self.release.wait(0.2):
                        call.beat()
                    return
                self.release.wait(self.freeze)                                 # 片付け・固まった：知らせを止めた

        def row(got, s):
            return self.tx(s, lambda tx: tx.one("SELECT state::text AS st, trouble FROM salescall.attempts WHERE attempt_id = %s",
                                                (got.called,)))
        with mock.patch.object(line, "frozen_after", lambda tx: 1.0), mock.patch.object(line, "WATCH_EVERY", 0.05):
            s = self.ready()                                                   # (1)
            d = FakeDriver()
            got = line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, d, Finishing([0.7, 0.7, 0.7]))
            self.assertEqual((row(got, s)["st"], row(got, s)["trouble"], getattr(d, "apologized", 0)), ("ended", None, 0))
            t = self.ready()                                                   # (2)
            apologized = []

            class Timed(FakeDriver):
                def apologize_and_hang_up(self):
                    apologized.append(time.monotonic())
                    super().apologize_and_hang_up()
            d, runner = Timed(), Finishing([0.7, 0.7, 0.7, 0.7], finish=False, freeze=10)
            began = time.monotonic()
            timer = threading.Timer(6.0, runner.release.set)
            timer.start()
            with mock.patch.object(line, "report"):
                got = line.one_round(self.w.a, SYSTEM, t["tenant"], t["line"], HOLDER, d, runner)
            timer.cancel()
            self.assertEqual((row(got, t)["st"], row(got, t)["trouble"], d.apologized), ("technical_error", "frozen", 1))
            self.assertLess(apologized[0] - began, 1.8)                       # 書き込み（合わせて2.8秒）を待たずに、すぐ
            u = self.ready()                                                   # (3)
            d, runner = Timed(), Finishing([2.5], finish=False, freeze=10, beating=True, flush=True)
            apologized.clear()
            began = time.monotonic()
            timer = threading.Timer(6.0, runner.release.set)
            timer.start()
            with mock.patch.object(line, "report"):
                got = line.one_round(self.w.a, SYSTEM, u["tenant"], u["line"], HOLDER, d, runner)
            timer.cancel()
            self.assertEqual((row(got, u)["st"], row(got, u)["trouble"], d.apologized), ("technical_error", "storage_stuck", 1))
            self.assertLess(apologized[0] - began, 1.8)                       # 書き込みが終わるのを待たずに、すぐ
            told = self.tx(u, lambda tx: [(r["source"], r["kind"]) for r in tx.all(
                "SELECT source, kind FROM salescall.error_events WHERE attempt_id = %s", (got.called,))])
            self.assertEqual(told, [("storage", "call_storage_stuck")])       # 出どころ「保存」
            self.assertTrue([r for r in self.w.a.in_system(SYSTEM, lambda tx: fix_requests.open_requests(tx, "CL-2"))
                             if r["kind"] == "call_storage_stuck"])

    def test_a_flush_that_waits_on_a_slow_but_moving_writer_is_not_a_freeze(self):
        """通話の途中で、書いた結果を続けて読むために flush を呼ぶ（plugins.CallControl の約束）。データベースが遅くても（書き込み1つは
        call.frozen_after より短い＝書き手は進んでいる）、待っているあいだは「動いています」の途切れに数えない（0143。⛔ 見直し21a の
        場面 w11：flush で待つあいだ知らせを出せず、元気な通話を固まったとして切った）。待ち終えたときから数え直す（待ち終えてすぐ
        知らせなくても、待っていた長さを途切れに数えない。見直し22 の 8）。"""
        class FlushMidCall:
            def run(self, access, actor, claim, *, tenant_id, client_id, call=None):
                call.beat()
                call.write(conversation_io.move_work(claim.attempt_id, "ringing"))
                call.write(conversation_io.move_work(claim.attempt_id, "answered"))
                call.write(conversation_io.move_work(claim.attempt_id, "in_conversation"))
                for _ in range(4):
                    call.write(lambda tx: tx.execute("SELECT pg_sleep(0.6)"))   # 合わせて2.4秒（知らせの途切れの長さより長い）
                call.write(conversation_io.item_work(claim.attempt_id, conversation_io.ItemValue("Q1", "confirmed", None, "答えた")))
                call.flush()                                                   # 書いた結果を続けて読む
                time.sleep(0.6)                                                # 読んだ結果で次を決める（知らせの途切れの長さより短い）
                for _ in range(3):
                    call.beat()
                    time.sleep(0.2)
                for st in ("ending", "ended"):
                    call.write(conversation_io.move_work(claim.attempt_id, st))
                call.write(conversation_io.report_work(claim.attempt_id, rep().to_json()))
                call.write(lambda tx: attempts.record_verification(tx, claim.attempt_id, summary=CLEAN))
                call.flush()
        with mock.patch.object(line, "frozen_after", lambda tx: 1.0), mock.patch.object(line, "WATCH_EVERY", 0.05):
            s = self.ready()
            d = FakeDriver()
            got = line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, d, FlushMidCall())
        row = self.tx(s, lambda tx: tx.one("SELECT state::text AS st, trouble FROM salescall.attempts WHERE attempt_id = %s",
                                           (got.called,)))
        self.assertEqual((row["st"], row["trouble"], getattr(d, "apologized", 0)), ("ended", None, 0))
        with self.assertRaises(ValueError):                                    # 移せない先は、頼むその場で断る
            conversation_io.move_work(got.called, "claimed")

    def test_a_slow_write_never_ends_a_call_that_is_still_beating(self):
        """「動いています」が続いているあいだは、書き込みが遅くても終えない（line._Call の1か所。見直し22 の 1）。⛔ 0143：書き込み1つが
        call.frozen_after より長いだけで、元気な通話を「データベースが止まった」としておわびで切り（場面 w20）、違う3行で続くと、こちらの
        不具合が3行続いたとして契約者の新しい電話を止めた（場面 w25）。3つの行で、話している途中に書き込み1つが固まりとみなす長さより
        長い：どれもおわびせず、話し終えて結果を残し、こちらの不具合として伝えず、契約者の電話も止めない。"""
        class Talking:
            def run(self, access, actor, claim, *, tenant_id, client_id, call=None):
                call.beat()
                for st in ("ringing", "answered", "in_conversation"):
                    call.write(conversation_io.move_work(claim.attempt_id, st))
                call.write(lambda tx: tx.execute("SELECT pg_sleep(1.2)"))   # 書き込み1つが固まりとみなす長さ（0.5秒）より長い
                began = time.monotonic()
                while time.monotonic() - began < 1.8:                        # 話し続ける（知らせは続く）
                    call.beat()
                    time.sleep(0.1)
                    if call.ended:
                        return
                for st in ("ending", "ended"):
                    call.write(conversation_io.move_work(claim.attempt_id, st))
                call.write(conversation_io.report_work(claim.attempt_id, rep().to_json()))
                call.write(lambda tx: attempts.record_verification(tx, claim.attempt_id, summary=CLEAN))
                call.flush()
        with mock.patch.object(line, "frozen_after", lambda tx: 0.5), mock.patch.object(line, "WATCH_EVERY", 0.05):
            s = self.ready()
            Blame.rows(self, s, 3)
            d = FakeDriver()
            got = []
            for _ in range(3):
                r = line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, d, Talking())
                jobs.settle_ready(self.ctx(s))
                got.append(self.tx(s, lambda tx, r=r: tx.one(
                    "SELECT state::text AS st, trouble FROM salescall.attempts WHERE attempt_id = %s", (r.called,))))
        self.assertEqual([(g["st"], g["trouble"]) for g in got], [("ended", None)] * 3)
        self.assertEqual(getattr(d, "apologized", 0), 0)
        self.assertEqual(self.tx(s, lambda tx: tx.one("SELECT count(*) AS n FROM salescall.error_events WHERE tenant_id = %s",
                                                      (s["tenant"],)))["n"], 0)   # こちらの不具合として伝えない
        self.assertEqual(self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: stops.open_stops(
            tx, tenant_id=s["tenant"], client_id=None, business_id=None)), [])   # 契約者の電話を止めない

    def test_an_end_move_is_written_first_and_a_call_still_in_progress_is_ended(self):
        """終わりの移り（conversation_io.move_work の ending・ended など）は、書き手が書けたあとに「終わった」とし、通話を終えると決める
        ときは、書き手がそれを書き切るのを待ってから、データベースで進行中かを見る（line._Call._end の1か所。見直し22 の 3）。⛔ 0143：頼んだ
        時点で手元の印を立て、終えると決めても何も記録しなかった：(1) お別れの言葉（ending）の途中で固まった電話は、試行が ending のまま
        残り、その回線は「使用中」で次の行へかけられなかった（場面 w22b） (2) 移れない移り（話している途中から no_answer）を頼んで固まった
        電話に、おわびを流さなかった（場面 w23）。(3) ending を書けたあとに書き手が止まった（データベースが止まった）：お別れの途中なので
        おわびして切る (4) ended を書けたあとに書き手が止まった：話し終えた電話（おわびを流さない）。"""
        class Freezes:
            def __init__(self, moves, stuck=False):
                self.moves, self.stuck = moves, stuck

            def run(self, access, actor, claim, *, tenant_id, client_id, call=None):
                call.beat()
                for st in ("ringing", "answered", "in_conversation"):
                    call.write(conversation_io.move_work(claim.attempt_id, st))
                for st in self.moves:
                    call.write(conversation_io.move_work(claim.attempt_id, st))
                if self.stuck:                                               # 書き手が止まる（データベースが止まった）
                    call.write(lambda tx: tx.execute("SELECT pg_sleep(2)"))
                    with contextlib.suppress(plugins.WriteFailed):
                        call.flush()
                    return
                began = time.monotonic()
                while not call.ended and time.monotonic() - began < 3:       # 固まる（知らせない）。終えられたら戻る
                    time.sleep(0.05)

        def row(r, s):
            return self.tx(s, lambda tx: tx.one("SELECT state::text AS st, trouble FROM salescall.attempts WHERE attempt_id = %s",
                                                (r.called,)))
        with mock.patch.object(line, "frozen_after", lambda tx: 0.5), mock.patch.object(line, "WATCH_EVERY", 0.05), \
                mock.patch.object(line, "report"):
            s = self.ready()                                                   # (1)
            nxt = Blame.rows(self, s, 2)[1]
            d = FakeDriver()
            r = line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, d, Freezes(["ending"]))
            self.assertEqual((row(r, s)["st"], row(r, s)["trouble"], d.apologized), ("technical_error", "frozen", 1))
            jobs.settle_ready(self.ctx(s))
            again = line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, d, Runner())
            self.assertIsNotNone(again.called)                                 # その回線は次の行へかけられる
            self.assertEqual(self.tx(s, lambda tx: tx.one("SELECT task_id FROM salescall.attempts WHERE attempt_id = %s",
                                                          (again.called,)))["task_id"], nxt["task"])
            t = self.ready()                                                   # (2)
            d = FakeDriver()
            r = line.one_round(self.w.a, SYSTEM, t["tenant"], t["line"], HOLDER, d, Freezes(["no_answer"]))
            self.assertEqual((row(r, t)["st"], row(r, t)["trouble"], d.apologized), ("technical_error", "frozen", 1))
            u = self.ready()                                                   # (3)
            d = FakeDriver()
            r = line.one_round(self.w.a, SYSTEM, u["tenant"], u["line"], HOLDER, d, Freezes(["ending"], stuck=True))
            self.assertEqual((row(r, u)["st"], row(r, u)["trouble"], d.apologized), ("technical_error", "storage_stuck", 1))
            v = self.ready()                                                   # (4)
            d = FakeDriver()
            r = line.one_round(self.w.a, SYSTEM, v["tenant"], v["line"], HOLDER, d, Freezes(["ending", "ended"], stuck=True))
            self.assertEqual((row(r, v)["st"], row(r, v)["trouble"], getattr(d, "apologized", 0)), ("ended", None, 0))

    def test_a_writer_that_stops_after_the_call_part_returns_ends_the_call_in_progress(self):
        """通話を進める部品が終えないまま戻り、そのあと書き手が進まない（書き込み1つが call.frozen_after より長い＝データベースが止まった）：
        記録を待たずに、すぐおわびして終え、データベースが戻ったら終わりの記録を書く（storage_stuck。line._Call.after_runner の1か所。
        見直し22 の 8：外しても落ちる試験が無かった）。"""
        class Returns:
            def run(self, access, actor, claim, *, tenant_id, client_id, call=None):
                call.beat()
                for st in ("ringing", "answered", "in_conversation"):
                    call.write(conversation_io.move_work(claim.attempt_id, st))
                call.write(lambda tx: tx.execute("SELECT pg_sleep(1.5)"))      # 戻ったあと、書き手が止まる
        with mock.patch.object(line, "frozen_after", lambda tx: 0.5), mock.patch.object(line, "WATCH_EVERY", 0.05), \
                mock.patch.object(line, "report"):
            s = self.ready()
            d = FakeDriver()
            got = line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, d, Returns())
        row = self.tx(s, lambda tx: tx.one("SELECT state::text AS st, trouble FROM salescall.attempts WHERE attempt_id = %s",
                                           (got.called,)))
        self.assertEqual((row["st"], row["trouble"], d.apologized), ("technical_error", "storage_stuck", 1))

    def test_the_reason_why_is_cut_to_its_limit(self):
        """通話を終えた根拠の文（why）は、前後の空白を除いて200字までに切って残す（attempts.end_for_trouble の1か所。データベースの守り
        attempts_trouble_why と同じ長さ。見直し22 の 8：外しても落ちる試験が無かった＝長い文で記録が断られ、通話が進行中のまま残る）。"""
        class Long:
            def run(self, access, actor, claim, *, tenant_id, client_id, call=None):
                call.beat()
                access.in_client(actor, tenant_id, client_id, lambda tx: [
                    attempts.move(tx, claim.attempt_id, st) for st in ("ringing", "answered", "in_conversation")])
                call.end_for_trouble("cannot_proceed", "  " + "あ" * (attempts.WHY_MAX + 50) + "  ")
        s = self.ready()
        with mock.patch.object(line, "report"):
            got = line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, FakeDriver(), Long())
        row = self.tx(s, lambda tx: tx.one("SELECT state::text AS st, trouble_why FROM salescall.attempts WHERE attempt_id = %s",
                                           (got.called,)))
        self.assertEqual((row["st"], row["trouble_why"]), ("technical_error", "あ" * attempts.WHY_MAX))

    def test_writes_that_cannot_be_written_are_handed_back_to_the_call(self):
        """見直し20a の (7)：通話ごとの書き手は、書けなかったことを通話の部品へ返す（⛔ 前は記録して捨て、通話の部品は知らずに通話を
        終えた）。(1) 受け口の決まりのうち、データベースを読まずに分かる確かめ（conversation_io.check_item・報告の形）は、頼んだその場で
        誤りを返す（conversation_io.item_work・report_work）(2) 書くときに分かった誤りは、書き手が持って flush で plugins.WriteFailed に
        して返す（頼み直せる）。受け口が断った誤りは見守り役の誤りに数えず、データベースの誤りは見守り役の誤りとしても記録する
        (3) 受け取らずに戻った分は、回線ごとの処理が記録する。会話の流れ（知らせ）は書き込みを待たない。"""
        seen = {}

        class Writes:
            def __init__(self, collect):
                self.collect = collect

            def run(self, access, actor, claim, *, tenant_id, client_id, call=None):
                call.beat()
                access.in_client(actor, tenant_id, client_id, lambda tx: [
                    attempts.move(tx, claim.attempt_id, st) for st in ("ringing", "answered", "in_conversation")])
                with self_.assertRaises(conversation_io.ItemInvalid):          # (1) その場で
                    call.write(conversation_io.item_work(claim.attempt_id, conversation_io.ItemValue(
                        "Q1", "dont_know", None, None, "知らない理由")))
                with self_.assertRaises(call_report.ReportInvalid):
                    call.write(conversation_io.report_work(claim.attempt_id, {"version": -1}))
                call.write(conversation_io.item_work(claim.attempt_id, conversation_io.ItemValue("Q_NOT_THERE", "confirmed")))

                def broken(tx):
                    raise RuntimeError("試験：書けない")
                call.write(broken)
                if not self.collect:
                    return                                                     # (3) 受け取らずに戻った
                try:
                    call.flush()
                except plugins.WriteFailed as e:                               # (2) flush で返る（頼んだ順）
                    seen["failed"] = [type(x).__name__ for _, x in e.failed]
                call.flush()                                                   # 返した分は、もう持たない
                call.write(conversation_io.item_work(claim.attempt_id, conversation_io.ItemValue("Q1", "confirmed", None, "答えた")))
                call.write(conversation_io.report_work(claim.attempt_id, rep().to_json()))

                def finish(tx):
                    for st in ("ending", "ended"):
                        attempts.move(tx, claim.attempt_id, st)
                    attempts.record_verification(tx, claim.attempt_id, summary=CLEAN)
                call.write(finish)
                call.flush()
        self_ = self
        s = self.ready()
        with mock.patch.object(line, "report") as reported:
            got = line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, FakeDriver(), Writes(True))
        self.assertEqual(seen["failed"], ["ItemInvalid", "RuntimeError"])
        self.assertEqual([c.args[2] for c in reported.call_args_list], ["通話の書き込み"])   # データベースの誤りだけ
        self.assertEqual(self.state(s, got.called), "ended")
        self.assertEqual(self.tx(s, lambda tx: tx.one("SELECT count(*) AS n FROM salescall.item_values WHERE attempt_id = %s",
                                                      (got.called,)))["n"], 1)
        t = self.ready()
        with mock.patch.object(line, "report") as reported:
            line.one_round(self.w.a, SYSTEM, t["tenant"], t["line"], HOLDER, FakeDriver(), Writes(False))
        self.assertEqual(sorted(c.args[2] for c in reported.call_args_list),
                         ["通話の書き込み", "通話の書き込み（受け口が断ったまま、通話の部品が受け取らなかった）"])

    def test_a_line_reason_rests_the_line_without_rewriting_the_row(self):
        s = self.ready()
        self.lease(s, holder="ほかのPC#x")                                    # この見守り役は回線を借りていない
        before = self.tx(s, lambda tx: next_action.current(tx, s["task"]))["seq"]
        got = line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, FakeDriver(), Runner())
        self.assertEqual((got.called, got.pause), (None, "line_not_leased"))
        self.assertEqual(self.tx(s, lambda tx: next_action.current(tx, s["task"]))["seq"], before)

    def test_a_stopped_line_rests_without_rewriting_the_row(self):
        """回線が止まっている（回線の異常など）：その回線は休み、行の予定は書き直さない（ほかの回線がかけられる。⛔ 2026-10-22 の
        見直し14：前は止まった回線が「止まっている」で断られるたびに、行の予定を5分ずつ先へ書き直し、約束の電話を先送りした）。"""
        from salescall.core import stops
        s = self.ready()
        self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: stops.stop(
            tx, scope="line", cause="line_failure", reason="試験", tenant_id=s["tenant"], line_id=s["line"]))
        before = self.tx(s, lambda tx: next_action.current(tx, s["task"]))["seq"]
        got = line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, FakeDriver(), Runner())
        self.assertEqual((got.called, got.pause), (None, "line_stopped"))
        self.assertEqual(self.tx(s, lambda tx: next_action.current(tx, s["task"]))["seq"], before)

    def another_row(self, s):
        """同じ依頼者の、別の番号の行（いまかけてよい）。"""
        k = self.w.contact(s["tenant"], s["client"], normalize(fictional_jp_fixed()).e164)
        t = self.w.task(s["tenant"], s["client"], s["run"], k)
        self.tx(s, lambda tx: next_action.schedule_now(tx, t, cause="試験"))
        return dict(s, task=t, contact=k)

    def test_a_call_that_could_not_be_placed_is_a_failed_dial_whose_blame_the_line_decides(self):
        """電話の部品が、かけようとしてかけられなかった（CallNotPlaced）：試行は「発信に失敗」（どの回数にも数えない）。だれのせいかは
        アプリの言い分では決めず、同じ回線のあとの証拠で決める（salescall.unrung_blame＝0129・0143）：決まるまでは、回線を休ませず、
        回線の異常にも数えず、その行は締めずに休み、次の行へ進む。違う番号が続けて鳴らなくても決めない（違う番号の数では決めない）。
        次の行がつながれば、どれも番号のせい。かける行が無くなり、テスト番号が無ければ、決めずに待ち、契約者と管理者に1回知らせる。
        ⛔ 0129：前は「回線のせいか分からない」をいつも回線の異常に数えて休んだ（見直し18a の問題2）。⛔ 0129〜0141：違う番号が2つ続けば
        回線のせいとした（見直し21a）。スマホがまだ通話中なら休む（数えない）。"""
        s = self.ready()
        other = self.another_row(s)
        fail = FakeDriver(line.dial_exit.CallNotPlaced("試験：かけられない"))
        got = line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, fail, Runner())
        self.assertEqual((got.called, got.pause, got.free), (None, None, True))
        first = self.tx(s, lambda tx: tx.all("SELECT attempt_id, task_id, state::text AS st,"
                                             " salescall.dial_kind(state, cut_off, attempt_id) AS k FROM salescall.attempts"
                                             " WHERE line_id = %s", (s["line"],)))
        self.assertEqual([(r["st"], r["k"]) for r in first], [("call_failed", "not_placed")])
        jobs.settle_ready(self.ctx(s))
        self.assertTrue(self.tx(s, lambda tx: tx.one("SELECT salescall.task_awaiting_decision(%s) AS w",
                                                     (first[0]["task_id"],))["w"]))   # 決まるまで締めない（その行は休む）
        got = line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, fail, Runner())   # 次の行（別の番号）も鳴らない
        self.assertEqual((got.called, got.pause), (None, None))
        self.assertEqual(self.faults(s), [])                                   # 違う番号が2つでも、まだ決めない
        jobs.settle_ready(self.ctx(s))
        for _ in range(2):                                                     # かける行が無い：テスト番号が無い＝決めずに待つ
            got = line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, fail, Runner())
            self.assertEqual((got.called, got.pause), (None, line.RING_TEST_PAUSE))
        told = self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: tx.one(
            "SELECT count(*) AS n FROM salescall.notification_events WHERE kind = 'line_undecided'"))["n"]
        self.assertEqual(told, 1)
        dials = self.tx(s, lambda tx: [r["attempt_id"] for r in tx.all(
            "SELECT attempt_id FROM salescall.attempts WHERE line_id = %s ORDER BY claimed_at", (s["line"],))])
        self.assertEqual({self.tx(s, lambda tx, a=a: attempts.blame(tx, a)) for a in dials}, {"pending"})
        self.another_row(s)                                                    # 新しい行がつながった：どれも番号のせい
        got = line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, FakeDriver(), Runner())
        self.assertIsNotNone(got.called)
        self.assertEqual({self.tx(s, lambda tx, a=a: attempts.blame(tx, a)) for a in dials}, {"number"})
        self.assertEqual(self.faults(s), [])
        jobs.settle_ready(self.ctx(s))
        for task in (s["task"], other["task"]):                                # 決まった順の続きで、あとへ
            self.assertFalse(self.tx(s, lambda tx, task=task: tx.one(
                "SELECT salescall.task_awaiting_decision(%s) AS w", (task,))["w"]))
        t = self.ready()
        got = line.one_round(self.w.a, SYSTEM, t["tenant"], t["line"], HOLDER, FakeDriver(busy=True), Runner())
        self.assertEqual((got.called, got.pause), (None, "line_in_call"))

    def test_a_call_that_ends_before_the_answer_on_the_line_side_counts_as_a_line_fault_once_it_is_the_lines(self):
        """お客様が出る前に終わった通話（結果不明。どの回数にも数えない）も同じ1か所：回線のせいと分かれば（テスト番号も鳴らなかった）、
        回線の異常として数え（続けば、その回線を止めて知らせる＝数えない電話の歯止め。2026-10-22 深夜 ユーザー「回線の異常が続いたら
        回線を止める仕組み：OK」）、回線を休ませる。まだ分からない・ほかの番号が鳴った（番号のせい）ときは数えない。"""
        class Before(Runner):
            def run(self, access, actor, claim, *, tenant_id, client_id, call=None):
                access.in_client(actor, tenant_id, client_id, lambda tx: (attempts.move(tx, claim.attempt_id, "ringing"),
                                                                          attempts.move(tx, claim.attempt_id, "technical_error")))
        s = self.ready()
        self.numbered(s)
        self.another_row(s)
        for _ in range(2):
            got = line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, FakeDriver(ring=False), Before())
            self.assertEqual((got.called is not None, got.pause), (True, None))
            jobs.settle_ready(self.ctx(s))
        self.assertEqual(self.faults(s), [])
        got = line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, FakeDriver(ring=False), Before())   # テスト番号も鳴らない
        self.assertEqual((got.called, got.pause), (None, line.UNRUNG_PAUSE))
        self.assertEqual(self.faults(s), ["line", "line"])
        line._unrung(self.w.a, SYSTEM, s["tenant"], s["line"])               # 何度読んでも二重に数えない
        self.assertEqual(self.faults(s), ["line", "line"])
        message = self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: tx.one(
            "SELECT message FROM salescall.error_events WHERE line_id = %s LIMIT 1", (s["line"],)))["message"]
        self.assertEqual(message, line.UNRUNG_LINE)                           # 理由ごとの文（声が聞き取れないは POOR_AUDIO_LINE）
        t = self.ready()
        self.another_row(t)
        line.one_round(self.w.a, SYSTEM, t["tenant"], t["line"], HOLDER, FakeDriver(), Before())
        jobs.settle_ready(self.ctx(t))
        line.one_round(self.w.a, SYSTEM, t["tenant"], t["line"], HOLDER, FakeDriver(), Runner())   # ほかの番号は鳴った
        self.assertEqual(self.faults(t), [])

    def test_calls_that_keep_ending_before_the_answer_stop_the_line_and_our_own_crashes_do_not(self):
        """お客様が出る前に終わる電話が（違う番号で）続けば、その回線を止める（⛔ 見直し17a：前は、その電話自身の呼び出しで回線が
        直ったとみなして数え直し、3日で12本かけても回線は止まらなかった）。通話を進める部品が落ちた電話は、こちらのせい
        （runner_error。0136）：お詫びして終え、番号にも回線にも数えない（⛔ 見直し19a：前は回線の異常に数え、こちらの不具合で回線を
        止め、お客様の番号の続きにも数えうる形だった）。"""
        s = self.ready()
        need = self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: settings.resolve(
            tx, "stop.line.stop_after_failures", tenant_id=s["tenant"], client_id=None, business_id=None))
        self.w.a.in_tenant(OPERATOR, s["tenant"], lambda tx: settings.set_value(   # こちらの不具合の止め方は tests/test_troubles で試す
            tx, "stop.trouble.stop_after_rows", 100, scope="tenant", tenant_id=s["tenant"], status="decided", rationale="試験",
            review_when="試験"))

        def fresh():   # 別のお客様（同じ番号の空けに当たらない）
            task = self.w.task(s["tenant"], s["client"], s["run"], self.w.contact(
                s["tenant"], s["client"], normalize(fictional_jp_fixed()).e164))
            self.tx(s, lambda tx: next_action.schedule_now(tx, task, cause="試験"))

        def stopped():
            return [(r["scope"], r["cause"]) for r in self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: stops.open_stops(
                tx, tenant_id=s["tenant"], client_id=None, business_id=None, line_id=s["line"]))]

        for i in range(need + 1):   # こちらの部品が落ちた電話：回線は止めない
            if i:
                fresh()
            d = FakeDriver()
            got = line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, d, Runner(RuntimeError("試験")))
            self.assertEqual(self.state(s, got.called), "technical_error")
            self.assertEqual(d.apologized, 1)   # お詫びして終えた
            self.assertEqual(self.tx(s, lambda tx: (attempts.blame(tx, got.called), tx.one(
                "SELECT trouble FROM salescall.attempts WHERE attempt_id = %s", (got.called,))["trouble"])), ("ours", "runner_error"))
            self.assertIsNone(got.pause)
        self.assertEqual(stopped(), [])
        self.numbered(s)
        for i in range(need):       # お客様が出る前に終わる電話が、違う番号で続く：まだ分からない（次の行へ）
            fresh()
            got = line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, FakeDriver(ring=False), Runner(unanswered=True))
            self.assertEqual(self.state(s, got.called), "technical_error")
            self.assertEqual(self.tx(s, lambda tx: attempts.blame(tx, got.called)), "pending")
        self.assertEqual(stopped(), [])
        got = line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, FakeDriver(ring=False), Runner())   # テスト番号も鳴らない
        self.assertEqual(got.pause, line.UNRUNG_PAUSE)                         # 回線のせい：まとめて数えて止める
        self.assertEqual(stopped(), [("line", "line_failure")])
        fresh()
        got = line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, FakeDriver(), Runner())
        self.assertEqual((got.called, got.pause), (None, "line_stopped"))

    def test_a_line_stuck_in_a_call_counts_as_a_line_fault(self):
        """回線が「通話中」のまま（電話の部品が答えない も）、かけられない状態が通話の最長より長く続いたら、回線の異常として数える
        （続けば、処置の表のとおり、回線を止めて知らせる）。⛔ 2026-10-22 の見直し15：前は休み続けるだけで、だれにも知らされなかった。"""
        stuck, in_call, free, unknown = line.Stuck(), line.Round(pause="line_in_call"), line.Round(free=True), line.Round()
        self.assertFalse(stuck.seen(in_call, 0.0))
        self.assertFalse(stuck.seen(unknown, line.STUCK_AFTER / 2))           # 分からない回（かける行が無い）は、そのまま
        self.assertFalse(stuck.seen(in_call, line.STUCK_AFTER - 1))
        self.assertTrue(stuck.seen(in_call, line.STUCK_AFTER))               # 続いた：数える
        self.assertFalse(stuck.seen(in_call, line.STUCK_AFTER * 2 - 1))       # 数えたら、また初めから
        self.assertTrue(stuck.seen(in_call, line.STUCK_AFTER * 2))
        self.assertFalse(stuck.seen(free, line.STUCK_AFTER * 2 + 1))          # 「通話中ではない」と分かった：数え直す
        self.assertFalse(stuck.seen(in_call, line.STUCK_AFTER * 3))
        self.assertFalse(stuck.seen(in_call, line.STUCK_AFTER * 4 - 2))

        s = self.ready()                                                       # 回線ごとの流れ：本物の数える入口を通る
        got = line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, FakeDriver(busy=True), Runner())
        self.assertEqual((got.pause, got.free), ("line_in_call", False))
        got = line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, FakeDriver(), Runner())
        self.assertTrue(got.free)
        rounds = []

        class Stop:                                                            # 3回まわったら止まる（休みの待ちは、待たずに返す）
            def is_set(self):
                return len(rounds) >= 3

            def wait(self, seconds):
                return self.is_set()
        stop = Stop()

        def one(*args):
            rounds.append(1)
            return line.Round(pause="line_in_call")
        with mock.patch.object(line, "one_round", one), mock.patch.object(line, "STUCK_AFTER", 0.0):
            line.loop(self.w.a, s["tenant"], s["line"], HOLDER, FakeDriver(busy=True), Runner(), stop)
        faults = self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: tx.all(
            "SELECT kind, message FROM salescall.error_events WHERE line_id = %s", (s["line"],)))
        self.assertEqual([r["kind"] for r in faults], ["line", "line"])        # 2回目・3回目の「通話中」で数えた
        self.assertIn("通話中", faults[0]["message"])

    def test_when_every_client_is_held_by_its_own_limit_the_line_rests(self):
        """どの依頼者も、依頼者の理由（同時の本数の上限など）でかけられないときは、その回線を休ませる（⛔ 2026-10-22 の見直し：前は
        休まずに門へ渡し直し続け、断りの記録が数秒ごとに増えた）。"""
        s = self.ready()
        self.put_client(s, "calling.max_concurrent_calls", 1)
        t2 = self.w.task(s["tenant"], s["client"], s["run"], self.w.contact(s["tenant"], s["client"],
                                                                           normalize(fictional_jp_fixed()).e164))
        self.tx(s, lambda tx: next_action.schedule_now(tx, t2, cause="試験"))
        busy = self.claim(s)                                                   # 同じ依頼者の1本が、ほかの回線で進行中
        line2 = self.w.line(s["tenant"])
        self.lease(s, line=line2)
        got = line.one_round(self.w.a, SYSTEM, s["tenant"], line2, HOLDER, FakeDriver(), Runner())
        self.tx(s, lambda tx: attempts.release(tx, [busy.attempt_id]))
        self.assertEqual((got.called, got.pause), (None, "client_concurrency"))

    def test_a_failure_in_the_call_ends_only_that_call_and_asks_its_fixer_once(self):
        """通話を進める部品が落ちた電話は、その通話だけをおわびして終え、こちらのプログラムの不具合（runner_error）として1つの入口で
        伝える（直す担当は処置の表の1か所＝会話の部品なので CL-1。見守り役の誤りとして CL-2 へ重ねない。0139）。"""
        s = self.ready()
        driver = FakeDriver()
        got = line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, driver, Runner(RuntimeError("試験")))
        self.assertEqual(self.state(s, got.called), "technical_error")
        self.assertEqual(self.faults(s), ["call_runner_error"])
        self.assertTrue([r for r in self.w.a.in_system(SYSTEM, lambda tx: fix_requests.open_requests(tx, "CL-1"))
                         if r["kind"] == "call_runner_error"])
        self.assertEqual(self.asked_supervisor(s), [])
        self.assertEqual(getattr(driver, "apologized", 0), 1)                 # 通話が残っていれば、おわびして切る

    def test_promised_calls_go_first_across_clients_and_keep_their_share_of_the_line(self):
        """お客様と約束したかけ直しは、すべての依頼者について先に（共有の回線で、先の依頼者のふつうの電話より、あとの依頼者の
        約束が先）。回線の1日の上限は、今日の約束の分を取り置く（ふつうの電話は使わない）。2026-10-19 ユーザー：「その日の約束の
        電話には目印が付き、それを処理してから、通常の順序の電話を始めた方がいい」「（取り置き）OK」。"""
        s = self.ready()                                                       # 先に作った依頼者：ふつうの電話（いまかけてよい）
        t = self.ready(tenant=s["tenant"])                                     # あとの依頼者：約束したかけ直し
        now = self.tx(t, lambda tx: tx.one("SELECT now() AS t")["t"])
        self.tx(t, lambda tx: support.schedule_call(tx, t["task"], now, contact_id=t["contact"], cause="試験", promised=True))
        self.put_client(s, "line.max_dials_per_day", 1)
        kept = next(x for x in (s, t) if self.tx(s, lambda tx, x=x: tx.one(
            "SELECT salescall.line_promised_due(%s, salescall.jst(now())::date, %s) AS n", (x["line"], uuid.uuid4()))["n"]))
        self.assertEqual(self.reason(s, line=kept["line"]), "line_daily_limit")   # ふつうの電話：約束が割り当たった回線は、その1本分を取り置く
        got = line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, FakeDriver(), Runner())
        self.assertEqual(self.tx(t, lambda tx: tx.one("SELECT task_id FROM salescall.attempts WHERE attempt_id = %s",
                                                      (got.called,)))["task_id"], t["task"])   # あとの依頼者の約束が先

    def test_an_exact_time_promise_keeps_one_line_free_shortly_before(self):
        """お客様が時刻を指定した約束（時刻ちょうど・幅の無い指定）が、これから始めるふつうの電話が終わるより前に来るなら、約束の
        数だけの回線を空けておく（ほかに空いている回線があれば、この回線はふつうの電話に使う）。2026-10-19 ユーザー：「1本は通常業務に
        使っていいでしょ！無駄なことするな。該当の回線を空けておくだけでいいじゃない」。約束が遠ければ、ふつうにかける。"""
        s = self.ready()
        t = self.ready(tenant=s["tenant"])                                     # 回線は2本（どちらも共有・空いている）
        now = self.tx(t, lambda tx: tx.one("SELECT now() AS t")["t"])
        promise = lambda at: self.tx(t, lambda tx: support.schedule_call(   # noqa: E731
            tx, t["task"], at, contact_id=t["contact"], cause="試験", promised=True, callee_at=at))
        promise(now + timedelta(minutes=3))
        c = self.claim(s)
        self.assertIsInstance(c, gate.Claim)                                   # もう1本が空いている：この回線はかける
        self.tx(s, lambda tx: attempts.release(tx, [c.attempt_id]))
        self.lease(t, seconds=-60)                                             # もう1本が使えない（借りられていない）
        self.assertEqual(self.reason(s), "promise_soon")                 # この回線を約束のために空けておく
        self.lease(t)
        u = self.ready(tenant=s["tenant"])
        self.lease(u, seconds=-60)
        busy = self.w.insert_claim(u, line=t["line"])                         # もう1本がほかのお客様と通話中
        self.assertEqual(self.reason(s), "promise_soon")
        self.tx(u, lambda tx: attempts.release(tx, [busy]))
        self.lease(t, seconds=-60)
        self.tx(s, lambda tx: support.schedule_call(tx, s["task"], now, contact_id=s["contact"], cause="試験", promised=True))
        self.assertEqual(self.reason(s), "promise_soon")                       # 時刻の決まっていない約束も、約束の時刻をふさぐ
        self.tx(s, lambda tx: support.schedule_call(tx, s["task"], now, contact_id=s["contact"], cause="試験", promised=True,
                                                        callee_at=now))
        c = self.claim(s)
        self.assertIsInstance(c, gate.Claim)                                   # 時刻を指定された約束どうしは、来た順にかける
        self.tx(s, lambda tx: attempts.release(tx, [c.attempt_id]))
        self.tx(s, lambda tx: next_action.schedule_now(tx, s["task"], cause="試験"))
        promise(now + timedelta(minutes=30))
        c = self.claim(s)
        self.assertIsInstance(c, gate.Claim)                                   # 約束が遠い：かける
        self.tx(s, lambda tx: attempts.release(tx, [c.attempt_id]))
        self.tx(s, lambda tx: next_action.schedule_now(tx, s["task"], cause="試験"))
        self.tx(t, lambda tx: support.schedule_call(tx, t["task"], now + timedelta(minutes=3), contact_id=t["contact"],
                                                        cause="試験：時刻の決まっていない約束", promised=True))
        self.assertIsInstance(self.claim(s), gate.Claim)                       # 時刻を指定していない約束のためには空けない（見直し7）

    def test_a_dedicated_line_is_kept_when_the_shared_line_is_needed_for_another_client(self):
        """専用の回線と共有の回線が混ざっても、約束と空いている回線の割り当てで決める（2026-10-19 の見直し：前は、依頼者Aの
        専用の回線から見ると「共有の回線が空いている」ので空けず、依頼者Bの約束をかける回線が無くなった）。"""
        s = self.ready()                                                       # この回線＝依頼者Aの専用（ふつうの電話）
        t = self.ready(tenant=s["tenant"])                                     # 共有の回線（空いている）
        u = self.ready(tenant=s["tenant"])                                     # 依頼者Bの約束
        self.lease(u, seconds=-60)                                             # 依頼者Bの回線は使えない
        self.w.a.in_tenant(OPERATOR, s["tenant"], lambda tx: tx.execute(
            "UPDATE salescall.lines SET client_id = %s WHERE line_id = %s", (s["client"], s["line"])))
        phone = normalize(fictional_jp_fixed()).e164
        k = self.w.contact(s["tenant"], s["client"], phone)
        mine = self.w.task(s["tenant"], s["client"], s["run"], k)             # 依頼者Aの約束
        now = self.tx(s, lambda tx: tx.one("SELECT now() AS t")["t"])
        at = now + timedelta(minutes=3)
        self.tx(s, lambda tx: support.schedule_call(tx, mine, at, contact_id=k, cause="試験", promised=True, callee_at=at))
        c = self.claim(s)
        self.assertIsInstance(c, gate.Claim)                                   # Aの約束は共有の回線でかけられる
        self.tx(s, lambda tx: attempts.release(tx, [c.attempt_id]))
        self.tx(u, lambda tx: support.schedule_call(tx, u["task"], at, contact_id=u["contact"], cause="試験", promised=True,
                                                        callee_at=at))
        self.assertEqual(self.reason(s), "promise_soon")                       # 共有の回線はBの約束に要る：この回線はAの約束に
        self.assertIn("promise_soon", gate.BUSINESS_REASONS)                   # 平均の短い業務の電話なら始められる

    def test_the_daily_reservation_is_shared_by_the_lines_and_skips_promises_that_will_not_be_called_now(self):
        """回線の1日の上限の取り置きは、約束をかけられる回線で等しく分けた、この回線の分（前は、どの回線も同じ約束をまるごと
        取り置いた）。止まっている依頼者の約束・いま通話中の約束は取り置かない。"""
        s = self.ready()
        t = self.ready(tenant=s["tenant"])
        u = self.ready(tenant=s["tenant"])                                     # 共有の回線が3本
        now = self.tx(s, lambda tx: tx.one("SELECT now() AS t")["t"])
        for x, at in ((t, now), (u, now - timedelta(days=2))):   # 今日の約束と、前の日から残っている約束（どちらも今日かける）
            self.tx(x, lambda tx, x=x, at=at: support.schedule_call(tx, x["task"], at, contact_id=x["contact"], cause="試験",
                                                                        promised=True))
        reserved = lambda: [self.tx(s, lambda tx, x=x: tx.one(   # noqa: E731
            "SELECT salescall.line_promised_due(%s, salescall.jst(now())::date, %s) AS n", (x["line"], uuid.uuid4()))["n"])
            for x in (s, t, u)]
        self.assertEqual(sum(reserved()), 2)                                   # 回線3本の取り置きの合計は、約束の数ちょうど
        self.put_client(s, "line.max_dials_per_day", 2)
        c = self.claim(s)
        self.assertIsInstance(c, gate.Claim)                                   # 約束2つを3本で分ける：この回線の分は1以下
        self.tx(s, lambda tx: attempts.release(tx, [c.attempt_id]))
        self.tx(s, lambda tx: next_action.schedule_now(tx, s["task"], cause="試験"))
        self.put_client(s, "line.max_dials_per_day", 1)
        kept = next(x for x, n in zip((s, t, u), reserved()) if n)            # 約束が割り当たった回線は、その分を取り置く
        self.assertEqual(self.reason(s, line=kept["line"]), "line_daily_limit")
        self.tx(u, lambda tx: stops.stop(tx, cause="budget", reason="試験", tenant_id=u["tenant"], scope="client",
                                         client_id=u["client"]))
        self.tx(t, lambda tx: support.schedule_call(tx, t["task"], now, contact_id=t["contact"], cause="試験", promised=True))
        busy = self.w.insert_claim(t, line=t["line"])                         # Tの約束はいま通話中
        self.assertEqual(sum(reserved()), 0)                                   # 止まっている・通話中の約束は取り置かない
        self.assertIsInstance(self.claim(s), gate.Claim)
        self.tx(t, lambda tx: attempts.release(tx, [busy]))

    def test_a_promise_is_reserved_on_its_clients_dedicated_line_first(self):
        """回線の1日の上限の取り置きは、約束をその依頼者の専用の回線に先に割り当てる。専用の回線が無い依頼者の約束は共有の回線へ
        （⚠️ 前は区別なく割り当て、専用の回線のある依頼者の約束が共有の回線に寄ると、共有の回線がほかの依頼者のふつうの電話を控え、
        専用の回線は上限まで使えた＝見直し5）。"""
        s = self.ready()                                                       # この回線＝依頼者Aの専用
        t = self.ready(tenant=s["tenant"])                                     # 共有の回線・依頼者B（専用の回線なし）
        self.w.a.in_tenant(OPERATOR, s["tenant"], lambda tx: tx.execute(
            "UPDATE salescall.lines SET client_id = %s WHERE line_id = %s", (s["client"], s["line"])))
        now = self.tx(s, lambda tx: tx.one("SELECT now() AS t")["t"])
        for _ in range(3):                                                     # Aの約束3つ（2つだと、守りを外しても約束の並びで
                                                                               # 偶然そろうことがあった＝2026-10-21 の全部の試験で見つけた）
            k = self.w.contact(s["tenant"], s["client"], normalize(fictional_jp_fixed()).e164)
            mine = self.w.task(s["tenant"], s["client"], s["run"], k)
            self.tx(s, lambda tx, mine=mine, k=k: support.schedule_call(tx, mine, now, contact_id=k, cause="試験",
                                                                        promised=True))
        self.tx(t, lambda tx: support.schedule_call(tx, t["task"], now, contact_id=t["contact"], cause="試験", promised=True))
        reserved = [self.tx(s, lambda tx, x=x: tx.one(
            "SELECT salescall.line_promised_due(%s, salescall.jst(now())::date, %s) AS n", (x["line"], uuid.uuid4()))["n"])
            for x in (s, t)]
        self.assertEqual(reserved, [3, 1])                                     # Aの約束はAの専用の回線・Bの約束は共有の回線

    def test_the_guard_is_the_average_call_length_of_the_business_once_measured(self):
        """空ける時間は、これから始めるふつうの電話の業務の平均通話時間（お客様が出た通話。2026-10-19 ユーザー：「30分かかる業務と
        1分で終わる業務では、同じにできない」）。数が少ないうちは設定の初めの値（5分）。"""
        s = self.ready()
        task = {"tenant_id": s["tenant"], "client_id": s["client"], "business_id": s["business"]}
        guard = lambda: self.tx(s, lambda tx: gate.promise_guard_seconds(tx, task))   # noqa: E731
        self.assertEqual(guard(), 300)                                         # まだ測っていない：初めの値
        self.put_client(s, "gate.exact_promise_guard_samples", 0)
        self.assertEqual(guard(), 300)                                         # 0本と決めても、測った通話が無ければ初めの値
        self.put_client(s, "gate.exact_promise_guard_samples", 1)
        broken = self.w.insert_claim(s)
        for st in ("dialing", "answered", "technical_error"):                  # 途中の誤りで終わった通話は数えない
            self.w.move(s, broken, st)
        self.assertEqual(guard(), 300)
        aid = self.w.insert_claim(s)
        for st in ("dialing", "answered", "in_conversation", "ending", "ended"):
            self.w.move(s, aid, st)
        self.assertLess(guard(), 60)                                           # 測った平均（試験の通話はすぐ終わる）

    def test_a_capped_business_does_not_keep_the_other_businesses_of_the_client_out(self):
        """上限に達した業務の行が候補の先頭を埋めても、その業務を除いて読み直し、同じ依頼者のほかの業務の行をかける。"""
        s = self.ready()
        b, v = self.w.business_with_version(s["tenant"], s["client"])
        phone = normalize(fictional_jp_fixed()).e164
        k = self.w.contact(s["tenant"], s["client"], phone)
        task_b = self.w.task(s["tenant"], s["client"], self.w.run(s["tenant"], s["client"], b, v), k)
        self.tx(s, lambda tx: quality_stage.open_stage(tx, v, 1, None, evidence="試験"), OPERATOR)
        self.tx(s, lambda tx: business_rules.save(tx, v, rules_raw()), OPERATOR)
        self.tx(s, lambda tx: tx.execute(
            "INSERT INTO salescall.setting_values (key, scope, tenant_id, client_id, business_id, value, status, rationale,"
            " review_when, set_by) VALUES ('calling.business_daily_dials', 'business', %s, %s, %s, '1', 'decided', '試験',"
            " '試験', '試験')", (s["tenant"], s["client"], s["business"])), OPERATOR)
        self.tx(s, lambda tx: next_action.schedule_now(tx, task_b, cause="試験"))       # 業務B の行は、業務A の行より後
        phone_a = normalize(fictional_jp_fixed()).e164
        k_a = self.w.contact(s["tenant"], s["client"], phone_a)
        task_a2 = self.w.task(s["tenant"], s["client"], s["run"], k_a)
        self.past_attempt(dict(s, task=task_a2, contact=k_a, phone=phone_a), ["dialing", "no_answer"])   # 業務A は今日1本＝上限
        self.tx(s, lambda tx: next_action.hold(tx, task_a2, cause="試験：この行は候補に出さない"))
        self.tx(s, lambda tx: next_action.schedule_now(tx, s["task"], cause="試験：業務A の行を先頭に"))
        self.tx(s, lambda tx: next_action.schedule_now(tx, task_b, cause="試験"))
        first = self.tx(s, lambda tx: scheduler.due_tasks(tx, 1))
        self.assertEqual([r["task_id"] for r in first], [s["task"]])                    # 1行ずつなら、先頭は業務A
        old, line.CANDIDATES_PER_CLIENT = line.CANDIDATES_PER_CLIENT, 1
        try:
            got = line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, FakeDriver(), Runner())
        finally:
            line.CANDIDATES_PER_CLIENT = old
        self.assertEqual(self.tx(s, lambda tx: tx.one("SELECT task_id FROM salescall.attempts WHERE attempt_id = %s",
                                                      (got.called,)))["task_id"], task_b)

    def test_a_run_whose_version_cannot_call_is_skipped_without_rewriting_its_rows(self):
        """実施回の版の理由（ここでは、まだかけられる段の前＝version_not_ready）で門が断った行は、この回だけその実施回を飛ばし、
        予定は書き直さない（gate.STAGE_REASONS・RUN_REASONS。⛔ 見直し16：前は断るたびに次の動きが予定を5分先へ書き直し、待つ
        あいだ記録が増え続けた）。同じ依頼者のほかの実施回の行はかける。"""
        s = self.ready()
        b, v = self.w.business_with_version(s["tenant"], s["client"])   # 段を開いていない版＝まだかけられない
        self.tx(s, lambda tx: business_rules.save(tx, v, rules_raw()), OPERATOR)
        k = self.w.contact(s["tenant"], s["client"], normalize(fictional_jp_fixed()).e164)
        waiting = self.w.task(s["tenant"], s["client"], self.w.run(s["tenant"], s["client"], b, v), k)
        self.tx(s, lambda tx: next_action.schedule_now(tx, waiting, cause="試験：まだかけられない版の行を先頭に"))
        self.tx(s, lambda tx: next_action.schedule_now(tx, s["task"], cause="試験"))
        count = lambda: self.tx(s, lambda tx: tx.one(   # noqa: E731
            "SELECT count(*) AS n FROM salescall.task_next_actions WHERE task_id = %s", (waiting,))["n"])
        before = count()
        old, line.CANDIDATES_PER_CLIENT = line.CANDIDATES_PER_CLIENT, 1
        try:
            got = line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, FakeDriver(), Runner())
        finally:
            line.CANDIDATES_PER_CLIENT = old
        self.assertEqual(self.tx(s, lambda tx: tx.one("SELECT task_id FROM salescall.attempts WHERE attempt_id = %s",
                                                      (got.called,)))["task_id"], s["task"])
        self.assertEqual(count(), before)                                      # 待つ行の予定は書き直さない

    def test_no_reason_from_the_gate_keeps_adding_plans_while_the_line_goes_round(self):
        """理由の表で、行の予定を書き直さない扱いの理由（gate.PLAN_KEPT）のどれで門が断り続けても、回線がまわるあいだ、行の予定の
        記録は増えない（表で回す。扱いが plan・plan_if_known の理由は、次の動きが分かっている時刻へ1回だけ書く＝test_judge の
        AfterDenial）。⛔ 見直し17a：前は、どの集まりにも入らない理由（ほかの依頼者がかけた番号・名前の無い行・暦の無い日・設定の値の
        無い行など）を、回るたびに5分先へ書き直した。"""
        s = self.ready()
        self.tx(s, lambda tx: next_action.schedule_now(tx, s["task"], cause="試験"))
        plans = lambda: self.tx(s, lambda tx: tx.one(   # noqa: E731
            "SELECT count(*) AS n FROM salescall.task_next_actions WHERE task_id = %s", (s["task"],))["n"])
        before = plans()
        for reason in sorted(gate.PLAN_KEPT):
            with self.subTest(reason), mock.patch.object(gate, "claim", lambda tx, reason=reason, **kw: gate.denied(reason)):
                for _ in range(3):
                    line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, FakeDriver(), Runner())
                self.assertEqual(plans(), before)

    def test_a_failing_job_is_recorded_and_returned(self):
        s = self.ready()
        ctx = self.ctx(s)
        bad = jobs.Job(f"試験の壊れた仕事{uuid.uuid4().hex[:6]}", 60, "tenant", 60, lambda c: 1 / 0)
        self.assertTrue(jobs.run_job(ctx, bad))
        self.assertTrue(self.asked_supervisor(s))
        self.assertTrue(self.w.a.in_system(SYSTEM, lambda tx: leases.take_job(tx, bad.key(ctx.tenant_id), "ほかのPC#x", 60)))


class Blame(SupervisorBase):
    """お客様のところで鳴らなかった電話が、番号のせいか回線のせいか（salescall.unrung_blame の1か所＝0129・0143）を、回線ごとの処理と
    締めを通して確かめる。2026-10-24 管理者：「明らかに番号に問題がある場合は、その行は取りやめですが、回線の問題が明らかな場合は
    行は取りやめでなく一時中止だけですよ。」「回線か番号かどちらかわからない場合は、その行に固執するのでなく、1回目から、次の行に進む
    ようにしてください。次の行に進んで、次の行がつながればすぐに回線の問題か番号の問題か判断できますよね。」「その回線にかける行が
    残っていないときは、『テスト番号』で決めます。」（見直し18a の問題1〜3・見直し21a の場面）"""

    class Before(Runner):
        """どの電話も、お客様が出る前に回線の側で終わる（結果不明）。"""
        def run(self, access, actor, claim, *, tenant_id, client_id, call=None):
            access.in_client(actor, tenant_id, client_id, lambda tx: (attempts.move(tx, claim.attempt_id, "ringing"),
                                                                      attempts.move(tx, claim.attempt_id, "technical_error")))

    class Refuses(FakeDriver):
        """ある番号だけ、電話の部品がかけられない（ほかの番号はかかる）。"""
        def __init__(self, bad):
            super().__init__()
            self.bad = bad

        def start_call(self, phone):
            self.calls.append(phone)
            if phone == self.bad:
                raise line.dial_exit.CallNotPlaced("試験：この番号はかけられない")

    def rows(self, s, n):
        out = [s]
        for _ in range(n - 1):
            k = self.w.contact(s["tenant"], s["client"], normalize(fictional_jp_fixed()).e164)
            t = self.w.task(s["tenant"], s["client"], s["run"], k)
            self.tx(s, lambda tx: next_action.schedule_now(tx, t, cause="試験"))
            out.append(dict(s, task=t, contact=k))
        return out

    def line_stops(self, s):
        return self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: stops.open_stops(
            tx, tenant_id=s["tenant"], client_id=None, business_id=None, line_id=s["line"]))

    def no_row_lost(self, s, rows):
        for r in rows:
            self.assertFalse(self.tx(s, lambda tx: attempts.number_unusable(tx, r["contact"], tenant_id=s["tenant"],
                                                                             client_id=s["client"], business_id=s["business"])))
            self.assertEqual(self.tx(s, lambda tx: next_action.current(tx, r["task"]))["kind"], "call")

    def drive(self, s, driver, runner, rounds, *, resume=False):
        """回線ごとの処理を rounds 回まわす（毎回、締めの仕事も）。resume＝回線が止まったら、確かめが通って自動で解けたことにする。
        テスト番号を鳴らす間を待って休んだ回（RING_TEST_PAUSE）のあとは、その間（試験では1秒）を待つ（回線ごとの処理の休みと同じ）。"""
        got, stopped = [], 0
        for _ in range(rounds):
            r = line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, driver, runner)
            got.append(r)
            if r.pause in (line.RING_TEST_PAUSE, line.UNRUNG_PAUSE):
                self.next_ring()
            jobs.settle_ready(self.ctx(s))
            open_ = self.line_stops(s)
            if open_:
                stopped += 1
                if not resume:
                    break
                self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: [stops.resume(tx, e["event_id"], reason="試験：確かめが通った")
                                                                    for e in open_])
        return got, stopped

    def dials(self, s):
        return self.tx(s, lambda tx: [r["attempt_id"] for r in tx.all(
            "SELECT attempt_id FROM salescall.attempts WHERE line_id = %s AND salescall.is_dialed(state) ORDER BY claimed_at",
            (s["line"],))])

    def told(self, s):
        """テスト番号で決められない・確かめられない知らせ（line_undecided）の数：契約者あてと、管理者あての写し。"""
        mine = self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: tx.one(
            "SELECT count(*) AS n FROM salescall.notification_events WHERE kind = 'line_undecided'"))["n"]
        admin = len(self.w.a.in_system(ADMIN, lambda tx: tx.all(
            "SELECT 1 FROM salescall.notification_events WHERE kind = 'line_undecided' AND origin_tenant = %s", (s["tenant"],))))
        return mine, admin

    def undecided_requests(self):
        return [r for r in self.w.a.in_system(SYSTEM, lambda tx: fix_requests.open_requests(tx, "CL-2"))
                if r["kind"] == "test_ring_undecided"]

    def test_a_broken_line_that_comes_back_on_its_own_loses_no_row(self):
        """問題1：どの電話もお客様が出る前に終わるほど壊れた回線（テスト番号も鳴らない）で、回線の停止が自動で解けても、行は1つも
        「番号が使えない」にならない（回線のせいの電話は番号に数えない）。分からない電話のあとは次の行へ進み、かける行が無くなったら
        テスト番号で決める（鳴らない＝回線のせい）。回線は回線の異常で止まり（知らせ）、解けても、また止まる。"""
        s = self.ready()
        self.numbered(s)
        rows = self.rows(s, 3)
        got, stopped = self.drive(s, FakeDriver(ring=False), self.Before(), 12, resume=True)
        self.assertGreaterEqual(stopped, 2)
        dials = self.dials(s)
        self.assertGreaterEqual(len(dials), 6)
        self.assertEqual({self.tx(s, lambda tx: attempts.blame(tx, a)) for a in dials} - {"pending"}, {"line"})
        self.no_row_lost(s, rows)
        notes = self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: tx.one(
            "SELECT count(*) AS n FROM salescall.notification_events WHERE kind = 'stopped'"))["n"]
        self.assertGreater(notes, 0)                                           # 回線の異常は知らせる

    def test_one_number_that_cannot_be_placed_does_not_starve_the_line(self):
        """問題2：ある番号だけ、電話の部品がかけられない（ほかの番号はかかる）：その番号のせい（回線がほかの番号を鳴らせた）。回線は
        休まず、回線の異常にも数えず、ほかの行へかけ続ける（その番号の行は、決まるまで休み、決まれば決まった順で、あとへ）。⛔ 見直し18a
        の問題2：前は回線の異常として数えて休み、その行が候補の先頭に残り続け、1日でほかの行へ0本しかかけられなかった。"""
        s = self.ready()
        self.rows(s, 3)
        bad = self.bad_number(s)
        got, stopped = self.drive(s, self.Refuses(bad), Runner(), 3)
        self.assertEqual(stopped, 0)
        self.assertEqual([r.pause for r in got], [None, None, None])
        self.assertEqual(self.faults(s), [])
        self.assertEqual(sum(r.called is not None for r in got), 2)            # ほかの2行にかけた
        failed = self.tx(s, lambda tx: tx.one("SELECT attempt_id FROM salescall.attempts WHERE contact_id = %s", (s["contact"],)))
        self.assertEqual(self.tx(s, lambda tx: attempts.blame(tx, failed["attempt_id"])), "number")
        self.assertEqual(self.tx(s, lambda tx: attempts.failed_run(tx, s["contact"]))["n_sure"], 1)
        nxt = self.tx(s, lambda tx: next_action.current(tx, s["task"]))
        now = self.tx(s, lambda tx: tx.one("SELECT now() AS t")["t"])
        self.assertGreater(nxt["call_at"], now)                                # 決まった順で、あとへ（候補の先頭に残らない）

    def test_a_line_that_rejects_every_number_stops_and_loses_no_row(self):
        """問題3：どの番号もかけられない回線（アプリの設定の誤りなど）：分からない電話のあとは次の行へ進み、かける行が無くなったら
        テスト番号で決める。テスト番号も鳴らない＝回線のせい：行はどれも取りやめにならず、続きの電話をまとめて回線の異常として数えて
        止まり、知らせる。⛔ 見直し18a の問題3：前は番号のせいとして数え、4日で行がすべて終わり、だれにも知らされなかった。"""
        s = self.ready()
        phone = self.numbered(s)
        rows = self.rows(s, 4)
        d = FakeDriver(line.dial_exit.CallNotPlaced("試験：かけられない"), ring=False)
        got, stopped = self.drive(s, d, Runner(), 8)
        self.assertEqual(stopped, 1)
        self.assertEqual(len(got), 5)                                          # 4行にかけて（どれも鳴らない）、テスト番号で決めた
        self.assertEqual(d.rings, [phone])
        self.assertEqual([r["cause"] for r in self.line_stops(s)], ["line_failure"])
        self.assertEqual(self.faults(s), ["line"] * 4)
        self.no_row_lost(s, rows)
        for r in rows:
            self.assertEqual(self.tx(s, lambda tx: attempts.failed_run(tx, r["contact"]))["n"], 0)

    def fail_once(self, s, driver):
        """その行に1本かけて、鳴らずに終える（いまの予定にしてから。締めの仕事も）。かけた試行を返す。"""
        self.tx(s, lambda tx: next_action.schedule_now(tx, s["task"], cause="試験：その時刻が来た"))
        got = line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, driver, Runner())
        jobs.settle_ready(self.ctx(s))
        return self.tx(s, lambda tx: tx.one("SELECT attempt_id FROM salescall.attempts WHERE task_id = %s"
                                            " ORDER BY claimed_at DESC LIMIT 1", (s["task"],)))["attempt_id"] if got else None

    def closures(self, s):
        return self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: [(r["reason"], r["verdict"]) for r in tx.all(
            "SELECT reason, verdict FROM salescall.line_run_closures WHERE line_id = %s ORDER BY closure_id", (s["line"],))])

    def test_the_last_row_waits_for_the_test_number_and_an_earlier_ring_decides_nothing(self):
        """一覧の最後の1件（その回線にまもなくかける行が、ほかに残っていない）で、番号か回線か分からないとき：テスト番号で決める
        （2026-10-24 管理者：「その回線にかける行が残っていないときは、『テスト番号』で決めます」）。テスト番号が無いあいだは、前に
        この回線が鳴らせていても番号のせいと決めず（壊れた回線のお客様の行を失わない）、決めずに待ち、契約者と管理者に1回「テスト番号が
        無いので決められません」と知らせる。その行は、決まるまで締めずに休む（予定はそのまま）。テスト番号が入れば、それで決める（鳴らない
        ＝回線のせい：行は続き、かけた予定のまま）。⛔ 0131〜0141：テスト番号が無いとき前の区切りで決め、前に鳴った電話があれば番号のせい
        として行を取りやめうった。"""
        s = self.ready()
        other = self.rows(s, 2)[1]
        line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, FakeDriver(), Runner())   # 先頭の行が鳴った（回線は元気だった）
        jobs.settle_ready(self.ctx(s))
        first = self.tx(s, lambda tx: tx.one("SELECT contact_id FROM salescall.attempts WHERE line_id = %s"
                                             " ORDER BY claimed_at LIMIT 1", (s["line"],)))["contact_id"]
        t = s if first == other["contact"] else other                        # まだかけていない行を、かけられない番号の行にする
        rang = other if t is s else s
        self.tx(s, lambda tx: next_action._append(tx, rang["task"], call_at=None, end_reason="completed", cause="試験：終えた"))
        bad = self.bad_number(t)
        aid = self.fail_once(t, self.Refuses(bad))
        planned = self.tx(t, lambda tx: next_action.current(tx, t["task"]))
        with mock.patch.object(ring_test.fix_requests, "request") as asked:
            for _ in range(2):                                                 # かける行が無い回：テスト番号が無い＝決めずに待つ
                got = line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, self.Refuses(bad), Runner())
                self.assertEqual((got.called, got.pause), (None, line.RING_TEST_PAUSE))
        self.assertEqual(asked.call_args_list, [])                             # テスト番号が無いのは部品の不具合ではない
        self.assertEqual((self.closures(s), self.faults(s)), ([], []))
        self.assertEqual(self.tx(t, lambda tx: attempts.blame(tx, aid)), "pending")   # 前に鳴った電話があっても決めない
        self.assertEqual(self.told(s), (1, 1))                                 # テスト番号が無いことを1回だけ知らせる
        summary = self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: tx.one(
            "SELECT summary FROM salescall.notification_events WHERE kind = 'line_undecided'"))["summary"]
        self.assertIn("テスト番号が無い", summary)
        self.assertTrue(self.tx(t, lambda tx: tx.one("SELECT salescall.task_awaiting_decision(%s) AS w", (t["task"],))["w"]))
        self.assertEqual(self.tx(t, lambda tx: next_action.current(tx, t["task"]))["seq"], planned["seq"])   # 予定は書き直さない
        phone = self.numbered(s)                                               # テスト番号が入った：鳴らない＝回線のせい
        d = self.Refuses(bad)
        d.ring = False
        got = line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, d, Runner())
        self.assertEqual((got.pause, d.rings), (line.UNRUNG_PAUSE, [phone]))
        self.assertEqual(self.closures(s), [("test_silent", "line")])
        self.assertEqual((self.faults(s), self.tx(t, lambda tx: attempts.blame(tx, aid))), (["line"], "line"))
        jobs.settle_ready(self.ctx(s))
        nxt = self.tx(t, lambda tx: next_action.current(tx, t["task"]))
        now = self.tx(t, lambda tx: tx.one("SELECT now() AS t")["t"])
        self.assertEqual(nxt["kind"], "call")                                  # 行は続く（かけた予定のまま、いまから）
        self.assertLessEqual(nxt["call_at"], now)
        self.assertEqual(self.tx(t, lambda tx: attempts.failed_run(tx, t["contact"]))["n"], 0)   # 番号に数えない

    def test_only_a_row_the_line_can_call_soon_keeps_the_run_open(self):
        """まだ分からない続きを、テスト番号を鳴らさずに次の電話の結果で待つのは、その回線がまもなく（テスト番号をあける長さの並びの
        1つ目＝初め15分。core/ring_test.decide_within）かけられる行があるときだけ。20分先の行・明日かける行・続きの番号の行は待つ理由に
        ならない：テスト番号で決めにいく（0136。⛔ 見直し19a：前は夜や翌週にかける行が1つでも残っていれば決めず、その行が止まり続けた）。
        まもなくかける行があれば、テスト番号は鳴らさない（その電話で決まる。⛔ 見直し21a の場面 a9：違う番号が2つ続くと、次の行がすぐ
        つながる場面でも管理者の電話＝テスト番号を鳴らした）。"""
        s = self.ready()
        phone = self.numbered(s)
        self.w.a.in_tenant(OPERATOR, s["tenant"], lambda tx: settings.set_value(   # 待つ長さは初めの値（並びの1つ目＝15分）
            tx, "line.test_ring_gaps", [900, 1800], scope="tenant", tenant_id=s["tenant"], status="decided", rationale="試験",
            review_when="試験"))
        later = self.rows(s, 2)[1]
        bad = self.bad_number(s)
        later_phone = self.bad_number(later)

        def at(row, when):
            t = self.tx(s, lambda tx: tx.one(f"SELECT now() + interval '{when}' AS t")["t"])
            self.tx(s, lambda tx: support.schedule_call(tx, row["task"], t, contact_id=row["contact"],
                                                        cause="試験：その時刻にかける", promised=False))
        at(later, "1 day")
        d = self.Refuses(bad)
        d.ring = True
        aid = self.fail_once(s, d)                                             # 1回目が鳴らない
        self.assertEqual(self.tx(s, lambda tx: attempts.blame(tx, aid)), "pending")
        self.assertEqual(d.rings, [])
        soon = lambda phones: self.tx(s, lambda tx: scheduler.soon_left(tx, s["line"], not_phones=phones))   # noqa: E731
        at(later, "20 minutes")                                                # 15分より先：待たない
        self.assertFalse(soon([bad]))
        at(later, "10 minutes")                                                # ほかの行がまもなく：その電話で決まるので待つ
        self.assertTrue(soon([bad]))
        self.assertFalse(soon([bad, later_phone]))                            # 続きの番号の行なら、待つ理由にならない
        line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, d, Runner())
        self.assertEqual((d.rings, self.closures(s)), ([], []))               # テスト番号は鳴らさない
        self.assertTrue(self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: attempts.line_run_pending(tx, s["line"])))
        at(later, "1 day")                                                     # 明日の行だけ：決めにいく
        line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, d, Runner())
        self.assertEqual(d.rings, [phone])
        self.assertEqual(self.tx(s, lambda tx: attempts.blame(tx, aid)), "number")

    def test_a_row_left_undecided_on_one_line_waits_only_on_that_line(self):
        """まだ分からない電話の行は、その電話をかけた回線でだけ待つ（門の awaiting_decision＝salescall.task_awaiting_decision(行, 回線) の
        1か所。0144）。ほかの回線は、ふつうの決まりでかけてよい（2026-10-24 管理者「回線の問題が明らかな場合は行は取りやめでなく一時中止
        だけ」＝止めるのは回線であって行ではない）。回線を渡さないとき（締めの処理の判定し直しの待ち）は、どの回線の締めも待つ。
        ⛔ 0143：どの回線にも出さず、回線 A が壊れると、元気な回線 B でも約束の電話にかけられなかった（見直し22 の 5・場面 n2b）。"""
        s = self.ready()
        bad = self.bad_number(s)
        aid = self.fail_once(s, self.Refuses(bad))                             # 回線 A で鳴らない（まだ分からない）
        self.assertEqual(self.tx(s, lambda tx: attempts.blame(tx, aid)), "pending")
        self.tx(s, lambda tx: next_action.schedule_now(tx, s["task"], cause="試験：その時刻が来た"))
        self.assertEqual(self.reason(s), "awaiting_decision")                  # 回線 A：決まるまで待つ
        other = self.w.line(s["tenant"])
        self.lease(s, line=other)
        self.assertTrue(self.tx(s, lambda tx: tx.one("SELECT salescall.task_awaiting_decision(%s) AS w", (s["task"],))["w"]))
        self.assertTrue([r for r in self.tx(s, lambda tx: scheduler.due_tasks(tx, 5, line_id=other)) if r["task_id"] == s["task"]])
        self.assertIsInstance(self.claim(s, line=other), gate.Claim)           # 回線 B：かけてよい

    def test_a_row_the_quality_stage_holds_back_does_not_keep_the_run_open(self):
        """まもなくかけられる行（scheduler.soon_left）に、門が品質の段で断る行（段の件数に達した・まだかけられる段の前）は数えない（段が
        進むまでかけないので、その電話の結果では決まらない）。門と同じ1か所（gate.stage_reason）を読む＝門の判断を2か所に書かない。
        ⛔ 0143：数えたので、段の件数に達した検証モードで、まだ分からない続きをテスト番号で決めにいかなかった（見直し22 の 2。場面 n11）。"""
        s = self.ready()
        self.numbered(s)
        self.w.a.in_tenant(OPERATOR, s["tenant"], lambda tx: settings.set_value(   # 待つ長さは初めの値（並びの1つ目＝15分）
            tx, "line.test_ring_gaps", [900, 1800], scope="tenant", tenant_id=s["tenant"], status="decided", rationale="試験",
            review_when="試験"))
        self.put_client(s, "approval.human", True)                             # 検証モード（段の件数を見る）
        later = self.rows(s, 2)[1]
        for r in (s, later):
            self.tx(r, lambda tx, r=r: approval.approve_task(tx, r["task"]), OPERATOR)
        soon_at = self.tx(s, lambda tx: tx.one("SELECT now() + interval '10 minutes' AS t")["t"])
        self.tx(s, lambda tx: support.schedule_call(tx, later["task"], soon_at, contact_id=later["contact"],
                                                    cause="試験：まもなくかける", promised=False))
        bad = self.bad_number(s)
        d = self.Refuses(bad)
        d.ring = True
        aid = self.fail_once(s, d)                                             # 1回目が鳴らない
        self.assertEqual(self.tx(s, lambda tx: attempts.blame(tx, aid)), "pending")
        soon = lambda: self.tx(s, lambda tx: scheduler.soon_left(tx, s["line"], not_phones=[bad]))   # noqa: E731
        self.assertTrue(soon())                                                # 10分後の行：その電話の結果を待つ
        third = self.rows(s, 2)[1]                                             # ほかの回線が別の行を予約した（段の1件に数える）
        self.tx(third, lambda tx: approval.approve_task(tx, third["task"]), OPERATOR)
        other_line = self.w.line(s["tenant"])
        self.lease(s, line=other_line)
        self.assertIsInstance(self.claim(third, line=other_line), gate.Claim)
        self.tx(s, lambda tx: quality_stage.open_stage(tx, s["version"], 1, 1, evidence="試験：段の件数に達した"), OPERATOR)
        self.assertEqual(self.tx(s, lambda tx: gate.stage_reason(tx, s["version"], None)), "stage_full")
        self.assertFalse(soon())                                               # 段で断られる行は、待つ理由にならない

    def ring_logged(self, s, rows, line_id=None):
        """テストの記録を、時刻をさかのぼって入れる（rows＝(何分前, 結果) の並び。鳴らす間の試験用）。"""
        support.owner_access().in_system(SYSTEM, lambda tx: [tx.execute(
            "INSERT INTO salescall.line_ring_tests (tenant_id, line_id, purpose, rang, tested_at)"
            " VALUES (%s, %s, 'no_rows', %s, now() - make_interval(mins => %s))", (s["tenant"], line_id or s["line"], rang, ago))
            for ago, rang in rows])

    def test_the_test_number_keeps_its_gap_across_lines_and_steps_through_the_contractors_list(self):
        """テスト番号を鳴らす間（契約者ごと・回線をまたいで1つ。core/ring_test.wait_seconds の1か所。0139）：長さの並び
        line.test_ring_gaps（初め 15→30→60→120→240分＝2026-10-25 ユーザー：「15⇒30⇒60⇒120⇒240分　がデフォルト契約者ごとに設定」）。
        鳴らなかった・見分けられなかったが続けて1回目のあと並びの1つ目、2回目のあと2つ目…、最後の長さで止まる。鳴ったら、テストは OK で
        終わり、あける長さは無い（2026-10-25 夜 管理者の決定：「鳴らなかった場合に、15分→30分→60分→120分→240分といったよね？…鳴ったら、
        テストＯＫなだけじゃないの？」。⛔ 0139 の読み方：鳴ったあとも15分・鳴らなかった1回目のあと30分）。間の前は鳴らさない（記録もしない）。
        ⛔ 見直し19a：前は止めた回線の確かめのたびに（1分ごとに）、契約者自身の電話を鳴らしていた。⛔ 0136：初め30分・倍ずつ・4時間。"""
        s = self.ready()
        phone = self.numbered(s)
        system = self.w.a.in_tenant(SYSTEM, self.ready()["tenant"], lambda tx: settings.resolve(
            tx, "line.test_ring_gaps", tenant_id=None, client_id=None, business_id=None))
        self.assertEqual(system, [900, 1800, 3600, 7200, 14400])                # 初めの値（システム）
        self.w.a.in_tenant(OPERATOR, s["tenant"], lambda tx: settings.set_value(   # 契約者ごとに変えられる
            tx, "line.test_ring_gaps", system, scope="tenant", tenant_id=s["tenant"], status="decided", rationale="試験",
            review_when="試験"))
        wait = lambda: self.w.a.in_tenant(SYSTEM, s["tenant"], ring_test.wait_seconds)   # noqa: E731
        self.assertEqual(wait(), 0)
        other = self.w.line(s["tenant"])
        self.ring_logged(s, [(11, True)], other)                               # 鳴った：あけない（OK で終わり）
        self.assertEqual(wait(), 0)
        self.ring_logged(s, [(10, None)], other)                               # ほかの回線で10分前に見分けられなかった：15分
        self.assertAlmostEqual(wait(), 5 * 60, delta=30)
        d = FakeDriver(ring=True)
        self.assertEqual(ring_test.run(self.w.a, SYSTEM, s["tenant"], s["line"], d, "no_rows"), ring_test.Result.WAIT)
        self.assertEqual((d.rings, self.ring_tests_of(s)), ([], []))          # 鳴らさず、記録もしない
        self.ring_logged(s, [(9, False)])                                      # 続けて2回：30分
        self.assertAlmostEqual(wait(), 21 * 60, delta=30)
        self.ring_logged(s, [(8, None)])                                       # 3回：60分
        self.assertAlmostEqual(wait(), 52 * 60, delta=30)
        self.ring_logged(s, [(7, False)])                                      # 4回：120分
        self.assertAlmostEqual(wait(), 113 * 60, delta=30)
        self.ring_logged(s, [(6, None)])                                       # 5回：240分（並びの最後）
        self.assertAlmostEqual(wait(), 234 * 60, delta=30)
        self.ring_logged(s, [(5, False)])                                      # 6回：最後の長さのまま
        self.assertAlmostEqual(wait(), 235 * 60, delta=30)
        self.ring_logged(s, [(4, True)], other)                                # 鳴った：あけない
        self.assertEqual(wait(), 0)
        self.assertEqual(phone, self.w.a.in_tenant(SYSTEM, s["tenant"], ring_test.number))
        for gone in ("line.test_ring_gap", "line.test_ring_gap_max"):           # 前の「初め・倍ずつ・最大」は残さない
            self.assertIsNone(self.w.a.in_system(SYSTEM, lambda tx, gone=gone: tx.one(
                "SELECT key FROM salescall.setting_definitions WHERE key = %s", (gone,))))


    def test_a_test_number_that_cannot_be_told_apart_tells_at_once_and_keeps_trying(self):
        """テスト番号の答えは3つ（link/phone.Phone.test_ring の約束。0141）：回線から発信できた（呼び出しの音・話し中の音・電源オフや
        圏外のお知らせ＝鳴ったと同じ）・発信できない（回線のせい）・見分けられない。見分けられないと返ったら、そのまだ分からない続きに
        ついて契約者と管理者にすぐ1回知らせ（同じ続きで何度見分けられなくても1回。2026-10-25 夜 管理者「OK」）、電話をつなぐ部品の不具合
        として直す担当（CL-2）へ修正依頼を出す（同じ不具合で1つ。0143）。テスト番号で決めるのはやめない（あける長さのあとで、また鳴らす。
        ⛔ 0136〜0140：「分からない」が3回続いたら前の区切りで決め、1日たってから知らせた）。"""
        s = self.ready()
        self.numbered(s)
        self.fail_once(s, self.Refuses(self.bad_number(s)))
        self.assertEqual(self.told(s), (0, 0))
        unknown = FakeDriver(ring=None)
        for _ in range(4):
            self.next_ring()
            self.assertEqual(line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, unknown, Runner()).pause,
                             line.RING_TEST_PAUSE)                             # 決まらない：休んで試し直す（決めるのをやめない）
            self.assertEqual(self.told(s), (1, 1))                             # 1回目ですぐ・同じ続きで何度でも1回
            self.assertEqual(len(self.undecided_requests()), 1)                # 部品の不具合として修正依頼（同じ不具合で1つ）
        self.assertEqual((self.closures(s), self.w.a.in_tenant(SYSTEM, s["tenant"], ring_test.usable)), ([], True))
        self.assertEqual(len(unknown.rings), 4)
        for gone in ("line.test_ring_unknown_limit", "line.decide_horizon", "line.pending_alert_after"):   # 外した設定（0141）
            self.assertIsNone(self.w.a.in_system(SYSTEM, lambda tx, gone=gone: tx.one(
                "SELECT key FROM salescall.setting_definitions WHERE key = %s", (gone,))))
        self.assertNotIn("test_ring_unknown", notify.KINDS)                    # 0141 から出さない知らせの名前は残さない（0143）

    def test_a_stopped_line_whose_test_number_cannot_be_told_apart_tells_once_per_stop(self):
        """止めた回線の再開の確かめでも、テスト番号の答えが見分けられなければ、その停止について契約者と管理者にすぐ1回知らせ、直す担当
        （CL-2）へ修正依頼を出す（決めにいくテストと同じ1か所＝core/ring_test._tell_undecided。0143。⛔ 0141：再開の確かめでは知らせず、
        回線が止まったまま、だれも理由を知らなかった）。確かめの記録には数えない（あける長さのあとで、また鳴らす）。"""
        s = self.ready()
        self.numbered(s)
        stop = self.stopped_line(s)
        self.assertEqual(self.told(s), (0, 0))
        unknown = FakeDriver(ring=None)
        for _ in range(2):
            self.next_ring()
            self.assertIsNone(line._check_stop(self.w.a, SYSTEM, s["tenant"], s["line"], unknown))
            self.assertEqual(self.told(s), (1, 1))
        self.assertEqual(len(unknown.rings), 2)
        self.assertEqual(self.checks_of(stop), 0)                               # 確かめには数えない
        self.assertEqual(len(self.undecided_requests()), 1)
        refs = self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: tx.one(
            "SELECT refs FROM salescall.notification_events WHERE kind = 'line_undecided'"))["refs"]
        self.assertEqual(refs.get("stop_event_id"), stop["event_id"])

    def test_a_test_the_pc_part_did_not_try_is_not_recorded(self):
        """PC の部品がテスト番号を試さなかった（つながっていない・答えが期限に届かない・PC の上の緊急停止・話している途中＝CallWithheld）：
        結果ではないので記録せず（見分けられないにしない）、あける長さも伸ばさず、知らせない（PC がつながらないことは、ふつうの PC の
        見張りが知らせる）。⛔ 0134〜0142：「見分けられない」として記録し、あける長さを伸ばし、誤った知らせを出した（見直し21a の 10）。"""
        s = self.ready()
        self.numbered(s)
        self.fail_once(s, self.Refuses(self.bad_number(s)))

        class NotTried(FakeDriver):
            def test_ring(self, to):
                self.rings.append(to)
                raise line.dial_exit.CallWithheld("試験：PC の部品が試さなかった")
        d = NotTried()
        with mock.patch.object(ring_test.fix_requests, "request") as asked:
            for _ in range(2):
                self.assertEqual(ring_test.run(self.w.a, SYSTEM, s["tenant"], s["line"], d, "no_rows"), ring_test.Result.WITHHELD)
                self.assertEqual(line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, d, Runner()).pause,
                                 line.RING_TEST_PAUSE)
        self.assertEqual(len(d.rings), 4)                                      # あける長さは伸びない（すぐまた試せる）
        self.assertEqual((self.ring_tests_of(s), self.told(s), asked.call_args_list), ([], (0, 0), []))
        self.assertEqual(self.w.a.in_tenant(SYSTEM, s["tenant"], ring_test.wait_seconds), 0)

    def test_a_run_is_not_closed_while_the_line_has_a_call_in_progress(self):
        """回線に進行中の電話があるあいだは、まだ分からない続きを閉じない（その電話の結果で決まる。データベースの1か所
        salescall.close_unrung_run。使わなくした回線は、待たずに閉じる）。閉じる理由は、テスト番号が鳴らなかった・回線を使わなくした、の
        2つだけ（0143。⛔ 0131〜0141 の no_rows＝テスト番号で決められないとき前の区切りで決める、は外した）。"""
        s = self.ready()
        self.fail_once(s, self.Refuses(self.bad_number(s)))
        other = self.rows(s, 2)[1]
        c = self.claim(s, task=other["task"])
        self.assertIsInstance(c, gate.Claim, getattr(c, "reason", None))
        line.dial_exit.dial(self.w.a, SYSTEM, s["tenant"], s["client"], c.attempt_id, FakeDriver())   # かけている途中
        self.ring_logged(s, [(0, False)])                                      # テスト番号が鳴らなかった記録
        self.assertFalse(self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: attempts.close_unrung_run(tx, s["line"], "test_silent")))
        self.assertEqual(self.closures(s), [])
        with self.assertRaises(Rejected):
            self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: attempts.close_unrung_run(tx, s["line"], "no_rows"))
        self.w.a.in_tenant(OPERATOR, s["tenant"], lambda tx: registry.retire_line(tx, s["line"], reason="試験：端末を返した"))
        self.assertEqual(self.closures(s), [("retired", "line")])

    def test_calls_from_before_the_evidence_rule_are_closed_as_undecided_and_counted_nowhere(self):
        """前の版のデータ（0136 の移し替え）：0129 より前の、鳴らなかった電話は、だれのせいかの証拠を持たないので、移し替えのときに回線
        ごとに区切りを1つ置き（理由 migration・決め方 none＝決めない）、番号にも回線にも数えない。区切りのあとの電話から、いまの決まりで
        決める（この区切りの決め方は引き継がない）。前の版の予定の読み替え（番号を伺い直す理由が無い予定）はデータベースの1か所
        salescall.number_again_of。"""
        s = self.ready()
        bad = self.bad_number(s)
        self.tx(s, lambda tx: next_action.schedule_now(tx, s["task"], cause="試験"))
        line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, self.Refuses(bad), Runner())
        old = self.tx(s, lambda tx: tx.one("SELECT attempt_id FROM salescall.attempts WHERE line_id = %s", (s["line"],)))["attempt_id"]
        self.assertEqual(self.tx(s, lambda tx: attempts.blame(tx, old)), "pending")
        support.owner_access().in_system(SYSTEM, lambda tx: tx.execute(   # 移し替えと同じ区切り（0136 の (5)）
            "INSERT INTO salescall.line_run_closures (tenant_id, line_id, reason, verdict) VALUES (%s, %s, 'migration', 'none')",
            (s["tenant"], s["line"])))
        self.assertEqual(self.tx(s, lambda tx: attempts.blame(tx, old)), "none")
        self.assertEqual(self.tx(s, lambda tx: attempts.failed_run(tx, s["contact"]))["n"], 0)
        self.assertFalse(self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: attempts.line_run_pending(tx, s["line"])))
        for reason, verdict in (("no_rows", "none"), ("no_rows", "number"), ("test_silent", "number")):   # 決めない区切りは移し替えだけ
            with self.subTest(reason=reason, verdict=verdict), self.assertRaises(Exception):
                support.owner_access().in_system(SYSTEM, lambda tx: tx.execute(
                    "INSERT INTO salescall.line_run_closures (tenant_id, line_id, reason, verdict) VALUES (%s, %s, %s, %s)",
                    (s["tenant"], s["line"], reason, verdict)))
        jobs.settle_ready(self.ctx(s))
        self.tx(s, lambda tx: next_action._append(tx, s["task"], call_at=None, end_reason="completed", cause="試験：終えた"))
        other = self.rows(s, 2)[1]
        worse = self.bad_number(other)
        line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, self.Refuses(worse), Runner())   # 区切りのあと
        self.assertTrue(self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: attempts.line_run_pending(tx, s["line"])))
        phone = self.numbered(s)
        d = self.Refuses(worse)
        d.ring = False
        line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, d, Runner())
        self.assertEqual(d.rings, [phone])
        self.assertEqual(self.closures(s)[-1], ("test_silent", "line"))      # 区切りの決め方（none）は引き継がない
        self.assertEqual(self.tx(s, lambda tx: attempts.blame(tx, old)), "none")
        again = lambda purpose, mark: self.tx(s, lambda tx: tx.one(   # noqa: E731
            "SELECT salescall.number_again_of(%s, %s) AS r", (purpose, mark))["r"])
        self.assertEqual([again("ask_number_again", None), again("ask_number_again", "unreachable"), again("normal", None)],
                         ["wrong_party", "unreachable", None])

    def test_a_call_in_progress_when_the_line_is_retired_is_the_lines(self):
        """回線を使わなくしたとき、その回線でかけている途中だった電話は、あとで鳴らずに終われば回線のせい（使わなくした回線のあとに区切りが
        無い電話＝salescall.unrung_blame の1か所。0136。⛔ 見直し19a：前は、まだ終わっていない電話は閉じた続きに入らず、分からないまま
        知らせも無く待ち続けた）。"""
        s = self.ready()
        c = self.claim(s)
        self.assertIsInstance(c, gate.Claim, c)
        line.dial_exit.dial(self.w.a, SYSTEM, s["tenant"], s["client"], c.attempt_id, FakeDriver())
        self.w.a.in_tenant(OPERATOR, s["tenant"], lambda tx: registry.retire_line(tx, s["line"], reason="試験：端末を返した"))
        self.assertEqual(self.closures(s), [])                                  # まだ分からない続きは無かった
        self.tx(s, lambda tx: [attempts.move(tx, c.attempt_id, st) for st in ("ringing", "technical_error")])
        self.assertEqual(self.tx(s, lambda tx: attempts.blame(tx, c.attempt_id)), "line")
        self.assertEqual(self.tx(s, lambda tx: attempts.failed_run(tx, s["contact"]))["n"], 0)

    def test_a_stopped_line_with_no_rows_is_still_checked_by_the_test_number(self):
        """止めた回線の再開の確かめは、かける行の有る無しと分けて、回線ごとの処理の毎回の頭で（0136。⛔ 見直し19a：前は門が「止まって
        いる」で断ったときだけ確かめ、その回線にかける行が無いあいだは、止めた回線をテスト番号で確かめなかった）。"""
        s = self.ready()
        phone = self.numbered(s)
        self.stopped_line(s)
        tasks_ = self.tx(s, lambda tx: [r["task_id"] for r in tx.all("SELECT task_id FROM salescall.tasks WHERE client_id = %s",
                                                                       (s["client"],))])
        for t in tasks_:                                                       # かける行は残っていない
            self.tx(s, lambda tx, t=t: next_action._append(tx, t, call_at=None, end_reason="completed", cause="試験：終えた"))
        time.sleep(1.1)                                                        # 確かめの時刻は来ている
        rounds = []

        class Stop:
            def is_set(self):
                return len(rounds) >= 1

            def wait(self, seconds):
                rounds.append(seconds)
                return self.is_set()
        d = FakeDriver(ring=True)
        line.loop(self.w.a, s["tenant"], s["line"], HOLDER, d, Runner(), Stop())
        self.assertEqual(d.rings, [phone])
        self.assertEqual(self.ring_tests_of(s)[-1], ("resume_check", True))


    def test_a_line_the_gate_holds_back_decides_without_waiting_for_its_rows(self):
        """その回線が門の回線の理由（今日の上限など）でいまかけられなければ、ほかの行がいまかけられる時刻でも、まもなくかけられる行は
        無い：待たずにテスト番号で決めにいく（0136。⛔ 見直し19a：前は、その回線が翌朝までかけられないのに、残っている行を待ち続けた）。"""
        s = self.ready()
        phone = self.numbered(s)
        other = self.rows(s, 2)[1]
        tomorrow = self.tx(s, lambda tx: tx.one("SELECT now() + interval '1 day' AS t")["t"])
        self.tx(s, lambda tx: support.schedule_call(tx, other["task"], tomorrow, contact_id=other["contact"],
                                                    cause="試験：明日かける", promised=False))
        bad = self.bad_number(s)
        d = self.Refuses(bad)
        d.ring = False
        self.fail_once(s, d)                                                   # 1回目が鳴らない
        self.tx(s, lambda tx: next_action.schedule_now(tx, other["task"], cause="試験：いまかけられる"))
        with mock.patch.object(gate, "claim", lambda tx, **kw: gate.denied("line_daily_limit")):
            got = line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, d, Runner())
        self.assertEqual(got.pause, "line_daily_limit")
        self.assertEqual(d.rings, [phone])
        self.assertEqual(self.closures(s), [("test_silent", "line")])

    def test_retiring_the_line_closes_its_run_as_the_lines(self):
        """回線を使わなくしたら、その回線のまだ分からない続きは回線のせい（番号に数えず、ほかの回線でかけ直して確かめる）。"""
        s = self.ready()
        self.fail_once(s, self.Refuses(self.bad_number(s)))
        self.w.a.in_tenant(OPERATOR, s["tenant"], lambda tx: registry.retire_line(tx, s["line"], reason="試験：端末を返した"))
        self.assertEqual(self.closures(s), [("retired", "line")])
        self.assertEqual(self.tx(s, lambda tx: attempts.failed_run(tx, s["contact"]))["n"], 0)
        self.assertFalse(self.tx(s, lambda tx: attempts.number_unconfirmed(tx, s["contact"], tenant_id=s["tenant"],
                                                                          client_id=s["client"], business_id=s["business"])))

    # 2026-10-24 ユーザー決定：「その回線にかける行が残っていないときは、『テスト番号』で決めます。」「回線が止まったあとの再開の
    # 確かめ：OK」（core/ring_test の1か所。0134）
    def bad_number(self, s):
        return self.tx(s, lambda tx: tx.one("SELECT phone_e164 FROM salescall.contacts WHERE contact_id = %s",
                                            (s["contact"],))["phone_e164"])

    def ring_tests_of(self, s):
        return self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: [(r["purpose"], r["rang"]) for r in tx.all(
            "SELECT purpose, rang FROM salescall.line_ring_tests WHERE line_id = %s ORDER BY test_id", (s["line"],))])

    def test_a_test_number_that_rings_makes_the_last_rows_run_the_numbers(self):
        """かける行が残っていない回に、テスト番号が鳴った：回線は元気＝まだ分からなかった続きは番号のせい（鳴ったテストは、鳴った電話と
        同じ区切り＝salescall.line_rings）。続きを閉じる印は作らない。その行は決まった順の続きで、あとへ（回線の異常には数えない）。
        続きが決まったので、次の回は試さない（テスト番号を鳴らし続けない）。"""
        s = self.ready()
        phone = self.numbered(s)
        bad = self.bad_number(s)
        aid = self.fail_once(s, self.Refuses(bad))
        d = self.Refuses(bad)
        d.ring = True
        got = line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, d, Runner())
        self.assertEqual((got.called, got.pause), (None, None))
        self.assertEqual(d.rings, [phone])                                    # テスト番号へ（お客様の番号ではない）
        self.assertEqual(self.ring_tests_of(s), [("no_rows", True)])
        self.assertEqual(self.closures(s), [])
        self.assertEqual(self.tx(s, lambda tx: attempts.blame(tx, aid)), "number")
        jobs.settle_ready(self.ctx(s))
        self.assertEqual(self.tx(s, lambda tx: attempts.failed_run(tx, s["contact"]))["n_sure"], 1)
        nxt = self.tx(s, lambda tx: next_action.current(tx, s["task"]))
        self.assertGreater(nxt["call_at"], self.tx(s, lambda tx: tx.one("SELECT now() AS t")["t"]))   # 決まった順で、あとへ
        self.assertEqual(self.faults(s), [])
        line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, d, Runner())
        self.assertEqual(d.rings, [phone])

    def test_a_silent_test_number_makes_the_run_the_lines_even_after_an_earlier_ring(self):
        """テスト番号が鳴らなかった：回線のせい（前に鳴った電話があっても＝いまの回線の証拠が先）。続きを閉じ（test_silent）、番号に
        数えず、回線の異常として数えて回線を休ませる。行は続く。"""
        s = self.ready()
        self.numbered(s)
        other = self.rows(s, 2)[1]
        line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, FakeDriver(), Runner())   # 先頭の行が鳴った
        jobs.settle_ready(self.ctx(s))
        first = self.tx(s, lambda tx: tx.one("SELECT contact_id FROM salescall.attempts WHERE line_id = %s"
                                             " ORDER BY claimed_at LIMIT 1", (s["line"],)))["contact_id"]
        t = s if first == other["contact"] else other
        rang = other if t is s else s
        self.tx(s, lambda tx: next_action._append(tx, rang["task"], call_at=None, end_reason="completed", cause="試験：終えた"))
        bad = self.bad_number(t)
        self.fail_once(t, self.Refuses(bad))
        d = self.Refuses(bad)
        d.ring = False
        got = line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, d, Runner())
        self.assertEqual(got.pause, line.UNRUNG_PAUSE)
        self.assertEqual(self.closures(s), [("test_silent", "line")])
        self.assertEqual(self.tx(t, lambda tx: attempts.failed_run(tx, t["contact"]))["n"], 0)   # 番号に数えない
        self.assertFalse(self.tx(t, lambda tx: attempts.number_unusable(tx, t["contact"], tenant_id=t["tenant"],
                                                                        client_id=t["client"], business_id=t["business"])))
        self.assertEqual(self.faults(s), ["line"])
        self.assertEqual(self.tx(t, lambda tx: next_action.current(tx, t["task"]))["kind"], "call")   # 行は続く

    def test_a_test_number_that_decides_nothing_is_tried_again_later(self):
        """電話の部品が答えない（分からない）・回線が話している途中：どちらの証拠にもせず（記録だけ・話している途中は試さない）、休んで
        から試し直す。鳴らなかった記録なしに「テストが鳴らなかった」で閉じられない。テストの記録は変えられない。"""
        s = self.ready()
        phone = self.numbered(s)
        bad = self.bad_number(s)
        self.fail_once(s, self.Refuses(bad))
        busy = FakeDriver(busy=True)
        got = line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, busy, Runner())
        self.assertEqual((got.pause, busy.rings), (line.RING_TEST_PAUSE, []))
        d = self.Refuses(bad)                                                 # 答えない（None）
        got = line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, d, Runner())
        self.assertEqual((got.pause, d.rings), (line.RING_TEST_PAUSE, [phone]))
        self.assertEqual(self.ring_tests_of(s), [("no_rows", None)])
        self.assertEqual(self.closures(s), [])
        self.assertTrue(self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: attempts.line_run_pending(tx, s["line"])))
        with self.assertRaises(Rejected):
            self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: attempts.close_unrung_run(tx, s["line"], "test_silent"))
        with self.assertRaises(Rejected):
            support.owner_access().in_tenant(SYSTEM, s["tenant"], lambda tx: tx.execute(
                "UPDATE salescall.line_ring_tests SET rang = false WHERE line_id = %s", (s["line"],)))

    def test_a_stopped_line_is_resumed_by_the_test_number(self):
        """回線の異常で止めた回線の再開の確かめは、テスト番号があればテスト番号で（回線ごとの処理が、その回線の電話の部品で。確かめの
        部品は使わない）：確かめの間隔ごとに試し、1回鳴れば解く（2026-10-25 夜 管理者：「鳴ったら、テストＯＫなだけ」。決めるのは
        core/watch.record_check の1か所。⛔ 0136〜0142：アプリの確かめ用の回数＝stop.line.resume_after_successes を使い、再開のたびに約2分で
        3回、管理者の電話を鳴らした＝見直し21a の 3）。鳴ったテストは回線が直った証拠（数え直し）。"""
        s = self.ready()
        phone = self.numbered(s)
        stop = self.stopped_line(s)                                           # アプリの確かめの回数は2（テスト番号では使わない）
        self.tx(s, lambda tx: next_action.schedule_now(tx, s["task"], cause="試験：その時刻が来た"))
        self.assertEqual(line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, FakeDriver(), Runner()).pause,
                         "line_stopped")
        del PROBED[:]
        jobs.stop_checks(jobs.Context(self.w.a, self.cfg(s, SALESCALL_PROBER="tests.test_supervisor:make_prober"), HOLDER,
                                      NoLinks(), s["tenant"]))
        self.assertEqual(PROBED, [])                                          # 確かめの部品は、この停止を確かめない
        self.next_ring()
        unknown = FakeDriver()                                                # 電話の部品が見分けられない：記録せず、いつもの休み
        self.assertIsNone(line._check_stop(self.w.a, SYSTEM, s["tenant"], s["line"], unknown))   # （すぐ試し直さない）
        self.assertEqual(unknown.rings, [phone])
        self.assertEqual(self.checks_of(stop), 0)
        self.next_ring()
        d = FakeDriver(ring=True)
        self.assertIsNone(line._check_stop(self.w.a, SYSTEM, s["tenant"], s["line"], d))   # 1回鳴った：解けた
        self.assertEqual(d.rings, [phone])
        self.assertEqual(self.line_stops(s), [])
        errors_ = self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: tx.one(
            "SELECT salescall.consecutive_errors(ARRAY['line'], 'line_failure', 'line_ok', %s) AS n", (s["line"],)))["n"]
        self.assertEqual(errors_, 0)                                          # 鳴ったテストは、回線が直った証拠（数え直し）
        self.assertEqual([t for t in self.ring_tests_of(s) if t[0] == "resume_check"],
                         [("resume_check", None), ("resume_check", True)])

    # 2026-10-24 ユーザー：「（テスト番号を夜も鳴らしてよいか）私の電話です。軌道に乗れば、新しい電話をもうひとつ契約予定なのでその後に
    # 考えます。この仕事専用なら夜でもOK」（設定 line.test_ring_hours。鳴らすかは core/ring_test.run の1か所。0135）
    def stopped_line(self, s):
        """テスト番号のある契約者の回線を、回線の異常で止める（確かめの間隔1秒・続けて2回で解く）。止めた停止を返す。"""
        for key, value in (("stop.line.probe_interval", 1), ("stop.line.resume_after_successes", 2)):
            self.w.a.in_tenant(OPERATOR, s["tenant"], lambda tx, key=key, value=value: settings.set_value(
                tx, key, value, scope="tenant", tenant_id=s["tenant"], status="decided", rationale="試験", review_when="試験"))
        self.broken_until_stopped(s)
        return self.line_stops(s)[0]

    def test_a_line_stopped_again_for_silence_is_checked_at_once(self):
        """鳴らない（声以外の理由）で止めた回線は、止め直しても、今までどおりすぐテスト番号で確かめる（あける長さの並びで待たせるのは、
        声が聞き取れずに止め直した回線だけ＝salescall.audio_stops_in_a_row・core/watch.checkable。2026-10-29 管理者の決定。0144）。"""
        s = self.ready()
        self.numbered(s)
        self.stopped_line(s)
        self.next_ring()
        line._check_stop(self.w.a, SYSTEM, s["tenant"], s["line"], FakeDriver(ring=True))   # 1回鳴って再開
        self.assertEqual(self.line_stops(s), [])
        self.next_ring()
        self.broken_until_stopped(s)                                           # また鳴らずに止めた
        again = self.line_stops(s)[0]["event_id"]
        self.assertEqual(self.tx(s, lambda tx: tx.one("SELECT salescall.audio_stops_in_a_row(%s) AS n", (again,))["n"]), 0)
        waits = {r["event_id"]: r["wait"] for r in self.w.a.in_tenant(SYSTEM, s["tenant"], watch.checkable)}
        self.assertEqual(waits[again], 0)                                      # すぐ確かめる

    def broken_until_stopped(self, s):
        """どの番号もかけられない回線（テスト番号も鳴らない）を、回線の異常で止まるまでまわす（行は4つ）。分からない電話のあとは次の行へ
        進み、かける行が無くなったら、テスト番号で決めて回線のせいにする（0143）。"""
        self.rows(s, 4)
        broken = FakeDriver(line.dial_exit.CallNotPlaced("試験：かけられない"), ring=False)
        got, stopped = self.drive(s, broken, Runner(), 12)
        self.assertEqual(stopped, 1)
        self.assertIn(False, [t[1] for t in self.ring_tests_of(s)])            # テスト番号で回線のせいと決めた

    def checks_of(self, stop):
        return support.owner_access().in_system(SYSTEM, lambda tx: tx.one(
            "SELECT count(*) AS n FROM salescall.stop_checks WHERE stop_event_id = %s", (stop["event_id"],)))["n"]

    def test_the_test_ring_hours_are_the_contractors_any_number_of_ranges_and_start_at_7_to_22(self):
        """鳴らしてよい時間の初めの値は 7:00〜22:00 の1つ（いまはユーザー自身の電話＝深夜・早朝の歯止めと同じ）。契約者ごとに、1日に何か所
        でも決められる（日をまたいでも・0:00〜24:00 も。2026-10-25 ユーザー：「止める回線を2択だけじゃなく、こちらが決められるようにして。
        昼間に私は寝ていることがあるし、夜中でも起きていることがある。0～24時の選択は残したうえで。」）。重なり・空の並びは、データベースの
        値の形の確かめ（setting_value_ok の daily_ranges）の1か所が断る。i マークの説明に原文と使い方の例がある。"""
        s = self.ready()
        hours = lambda t: self.w.a.in_tenant(SYSTEM, t, ring_test.hours)   # noqa: E731
        allowed = lambda: self.w.a.in_tenant(SYSTEM, s["tenant"], ring_test.allowed)   # noqa: E731
        self.assertEqual(hours(s["tenant"]), ((7 * 3600, 22 * 3600),))
        self.set_ring_hours(s, ("00:00", "24:00"))
        self.assertEqual(hours(s["tenant"]), ((0, 24 * 3600),))
        self.assertTrue(allowed())                                            # いつでも（夜も）
        self.set_ring_hours(s, ("00:00", "09:00"), ("14:00", "24:00"))        # 管理者の例：昼に寝ている
        self.assertEqual(hours(s["tenant"]), ((0, 9 * 3600), (14 * 3600, 24 * 3600)))
        now = calling_hours.local(datetime.now(timezone.utc))
        hhmm = lambda h: (now + timedelta(hours=h)).strftime("%H:%M")   # noqa: E731（いまからの時刻。時刻しだいで変わらない形）
        self.set_ring_hours(s, (hhmm(3), hhmm(4)), (hhmm(-1), hhmm(1)))       # 2つ目の範囲がいまを含む
        self.assertTrue(allowed())
        self.set_ring_hours(s, (hhmm(2), hhmm(3)), (hhmm(5), hhmm(6)))        # どちらもいまを含まない
        self.assertFalse(allowed())
        for bad in ([], [("09:00", "12:00"), ("11:00", "13:00")], [("22:00", "02:00"), ("01:00", "03:00")],
                    [("00:00", "24:00"), ("05:00", "06:00")], [("09:00", "09:00")], [("09:00", "25:00")]):
            with self.subTest(bad=bad), self.assertRaises(Rejected):
                self.set_ring_hours(s, *bad)
        self.set_ring_hours(s, ("22:00", "02:00"), ("02:00", "05:00"))        # 端が触れるだけは重なりではない
        other = self.ready()                                                  # ほかの契約者は初めの値のまま
        self.assertEqual(hours(other["tenant"]), ((7 * 3600, 22 * 3600),))
        meaning = support.owner_access().in_system(SYSTEM, lambda tx: help_texts.explain(tx, "line.test_ring_hours"))
        for words in ("0:00〜9:00", "14:00〜24:00", "昼間に私は寝ていることがあるし", "0:00〜24:00"):
            self.assertIn(words, meaning)

    def test_values_in_the_old_single_range_form_are_moved_into_the_list(self):
        """前の形（範囲1つ ["07:00","22:00"]）の値が入ったデータベースでも読める：0139 が、どの記録も並びの形に移す（読み口は並びの形
        だけを読む＝前の形を受ける2つ目の読み方を作らない）。前の「初めの間・いちばん長い間」を変えていた契約者は、同じ長さの並びに移す。
        移し替えの文は 0139 のファイルからそのまま読み、元に戻す取引の中で流す。"""
        s = self.ready()
        text = (Path(migrate.MIGRATIONS_DIR) / "0139_ring_hours_gaps_and_our_troubles.sql").read_text()
        def statement(head):
            i = text.index(head)
            return text[i:text.index(";\n", i) + 1]
        move_hours = statement("UPDATE salescall.setting_values SET value = jsonb_build_array(value)")
        move_gaps = statement("INSERT INTO salescall.setting_values (key, scope, tenant_id, value, status, rationale, review_when, set_by)\nSELECT 'line.test_ring_gaps'")
        class Undo(Exception):   # 移し替えを試した取引を、元に戻す（ほかの試験に残さない）
            pass

        def work(tx):
            tx.execute("ALTER TABLE salescall.setting_values DISABLE TRIGGER setting_values_before_insert")
            tx.execute("INSERT INTO salescall.setting_values (key, scope, tenant_id, value, status, rationale, review_when, set_by)"
                       " VALUES ('line.test_ring_hours', 'tenant', %s, '[\"21:00\", \"03:00\"]', 'decided', '前の形', '試験', '試験')",
                       (s["tenant"],))
            tx.execute("INSERT INTO salescall.setting_definitions (key, merge_kind, value_type, meaning, design_ref, finest_scope)"
                       " VALUES ('line.test_ring_gap', 'override', 'seconds', '前の形', '試験', 'tenant'),"
                       "        ('line.test_ring_gap_max', 'override', 'seconds', '前の形', '試験', 'tenant')")
            tx.execute("INSERT INTO salescall.setting_values (key, scope, tenant_id, value, status, rationale, review_when, set_by)"
                       " VALUES ('line.test_ring_gap', 'tenant', %s, '600', 'decided', '前の形', '試験', '試験'),"
                       "        ('line.test_ring_gap_max', 'tenant', %s, '3000', 'decided', '前の形', '試験', '試験')",
                       (s["tenant"], s["tenant"]))
            tx.execute("ALTER TABLE salescall.setting_values DISABLE TRIGGER setting_values_frozen")
            tx.execute(move_hours)
            tx.execute(move_gaps)
            raise Undo([(r["key"], r["value"], r["ok"]) for r in tx.all(
                "SELECT key, value, salescall.setting_value_ok(d.value_type, v.value) AS ok"
                " FROM salescall.setting_values v JOIN salescall.setting_definitions d USING (key)"
                " WHERE v.tenant_id = %s AND key IN ('line.test_ring_hours', 'line.test_ring_gaps') ORDER BY value_id",
                (s["tenant"],))])
        with self.assertRaises(Undo) as got:
            support.owner_access().in_system(SYSTEM, work)
        self.assertEqual(got.exception.args[0], [("line.test_ring_hours", [["21:00", "03:00"]], True),
                                                 ("line.test_ring_gaps", [600, 1200, 2400, 3000], True)])
        self.assertEqual(self.w.a.in_tenant(SYSTEM, s["tenant"], ring_test.hours), ((7 * 3600, 22 * 3600),))   # 元に戻った

    def test_the_test_number_is_not_rung_outside_its_hours(self):
        """かける行が残っていない回線の確かめ：鳴らしてよい時間の外では鳴らさず、記録もせず、前の区切りで決めることもしない（時間が
        来るまで休んで試し直す）。時間の内に入れば鳴らして決める。"""
        s = self.ready()
        phone = self.numbered(s)
        self.ring_hours(s, inside=False)
        bad = self.bad_number(s)
        self.fail_once(s, self.Refuses(bad))
        d = self.Refuses(bad)
        d.ring = True
        got = line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, d, Runner())
        self.assertEqual((got.pause, d.rings), (line.RING_TEST_PAUSE, []))
        self.assertEqual((self.ring_tests_of(s), self.closures(s)), ([], []))
        self.assertTrue(self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: attempts.line_run_pending(tx, s["line"])))
        self.ring_hours(s, inside=True)
        line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, d, Runner())
        self.assertEqual(d.rings, [phone])
        self.assertEqual(self.ring_tests_of(s), [("no_rows", True)])

    def test_a_stopped_line_of_a_contractor_without_a_test_number_is_checked_by_the_app_check(self):
        """テスト番号の無い契約者の、回線の異常で止めた回線は、確かめの部品（アプリが応答するか）で確かめる（どちらで確かめるかは
        core/ring_test.checks_stop の1か所。テスト番号があるかは ring_test.usable）。その回線の処理はテスト番号を鳴らしにいかない。
        ⛔ テスト番号があるかを見ないと、だれも確かめず、回線が止まったままになる。"""
        from salescall.core import stops
        s = self.ready()
        self.w.a.in_tenant(OPERATOR, s["tenant"], lambda tx: settings.set_value(
            tx, "stop.line.probe_interval", 1, scope="tenant", tenant_id=s["tenant"], status="decided", rationale="試験",
            review_when="試験"))
        self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: stops.stop(
            tx, scope="line", cause="line_failure", reason="試験", tenant_id=s["tenant"], line_id=s["line"]))
        stop = self.line_stops(s)[0]
        time.sleep(1.1)                                                       # 確かめの時刻は来ている
        d = FakeDriver(ring=True)
        line._check_stop(self.w.a, SYSTEM, s["tenant"], s["line"], d)
        self.assertEqual(d.rings, [])                                         # テスト番号は鳴らさない（無い）
        del PROBED[:]
        jobs.stop_checks(jobs.Context(self.w.a, self.cfg(s, SALESCALL_PROBER="tests.test_supervisor:make_prober"), HOLDER,
                                      NoLinks(), s["tenant"]))
        self.assertIn(stop["event_id"], PROBED)                               # 確かめの部品が確かめた

    def test_a_stopped_line_waits_for_the_test_numbers_hours_and_no_weaker_check_resumes_it(self):
        """止めた回線の再開の確かめ：鳴らしてよい時間の外では、テスト番号を鳴らさず（記録しない＝確かめの間隔も伸びない）、アプリが
        応答するかだけの確かめ（確かめの部品）でも解かない。回線は止まったまま朝（時間の始まり）を待つ。時間に入れば鳴らして解く。"""
        s = self.ready()
        phone = self.numbered(s)
        stop = self.stopped_line(s)
        self.ring_hours(s, inside=False)
        time.sleep(1.1)                                                       # 確かめの時刻は来ている
        del PROBED[:]
        jobs.stop_checks(jobs.Context(self.w.a, self.cfg(s, SALESCALL_PROBER="tests.test_supervisor:make_prober"), HOLDER,
                                      NoLinks(), s["tenant"]))
        self.assertEqual(PROBED, [])                                          # 弱い確かめでは解かない
        d = FakeDriver(ring=True)
        for _ in range(3):
            self.assertIsNone(line._check_stop(self.w.a, SYSTEM, s["tenant"], s["line"], d))
        self.assertEqual((d.rings, [t for t in self.ring_tests_of(s) if t[0] == "resume_check"], self.checks_of(stop)), ([], [], 0))
        self.assertEqual(len(self.line_stops(s)), 1)                          # 止まったまま
        self.ring_hours(s, inside=True)
        line._check_stop(self.w.a, SYSTEM, s["tenant"], s["line"], d)
        self.assertEqual((d.rings, self.checks_of(stop)), ([phone], 1))       # 時間に入ったので鳴らして確かめた

    def test_the_night_wait_does_not_make_a_lasting_stop_fix_request(self):
        """「止まったまま1時間以上」の修正依頼（Intent.md 10-3）は、テスト番号で確かめる停止では、鳴らしてよい時間の長さで数える
        （夜は確かめていないので、夜の待ちで「確かめが通らない」とはしない）。見張りの長さを短くして確かめる（試験で1時間待たない）。"""
        s = self.ready()
        self.numbered(s)
        stop = self.stopped_line(s)
        self.ring_hours(s, inside=False)
        time.sleep(0.05)
        lasting = lambda: self.w.a.in_tenant(SYSTEM, s["tenant"], watch.lasting_stops)   # noqa: E731
        asked = lambda: [r for r in self.w.a.in_system(SYSTEM, lambda tx: fix_requests.open_requests(tx, "CL-2"))   # noqa: E731
                         if r["refs"].get("stop_event_id") == stop["event_id"]]
        with mock.patch.object(errors, "LASTING", timedelta(milliseconds=1)):
            self.assertEqual((lasting(), asked()), ([], []))                  # 時間の外だけ止まっていた：まだ数えない
            self.ring_hours(s, inside=True)
            self.assertEqual(lasting(), [stop["event_id"]])                   # 時間の内で続いた：修正依頼
            self.assertEqual(len(asked()), 1)


    # 見直し21a：違う番号が続けて鳴らなくても、違う番号の数では決めない（1つの道。0143）
    class RefusesSome(FakeDriver):
        """決めた番号だけ、電話の部品がかけられない（bad＝番号の集まり。ほかの番号はかかる）。ring＝テスト番号の答え。"""
        def __init__(self, bad, ring=None):
            super().__init__(ring=ring)
            self.bad = set(bad)

        def start_call(self, phone):
            self.calls.append(phone)
            if phone in self.bad:
                raise line.dial_exit.CallNotPlaced("試験：この番号はかけられない")

    def two_bad(self, s, *, rang_first=False):
        """一覧の先頭に、かけられない番号の行が2つ（X・Y）並び、そのあとに元気な行が2つある形（rang_first＝その前に、この回線で
        1本話して終えた）。返すのは (X, Y, ほかの行の並び, かけられない番号の集まり)。"""
        rows = self.rows(s, 5 if rang_first else 4)
        if rang_first:
            line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, FakeDriver(), Runner())
            jobs.settle_ready(self.ctx(s))
            done = self.tx(s, lambda tx: tx.one("SELECT task_id FROM salescall.attempts WHERE line_id = %s", (s["line"],)))["task_id"]
            rows = [r for r in rows if r["task"] != done]
            self.tx(s, lambda tx: next_action._append(tx, done, call_at=None, end_reason="completed", cause="試験：終えた"))
        order = self.tx(s, lambda tx: [r["task_id"] for r in scheduler.due_tasks(tx, 10)])
        rows.sort(key=lambda r: order.index(r["task"]))
        x, y, rest = rows[0], rows[1], rows[2:]
        return x, y, rest, {self.bad_number(x), self.bad_number(y)}

    def round_(self, s, d, runner=None):
        got = line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, d, runner or Runner())
        jobs.settle_ready(self.ctx(s))
        return got

    def blames(self, s, rows):
        return [self.tx(s, lambda tx, r=r: attempts.blame(tx, tx.one(
            "SELECT attempt_id FROM salescall.attempts WHERE task_id = %s ORDER BY claimed_at DESC LIMIT 1", (r["task"],))["attempt_id"]))
            for r in rows]

    def test_two_bad_numbers_side_by_side_are_decided_by_the_next_call_without_the_test_number(self):
        """元気な回線で、かけられない番号の行XとYが並ぶ（そのあとに元気な行）：XとYが続けて鳴らなくても回線のせいと決めず（違う番号の数
        では決めない）、次の行へ進む。次の行がつながれば、XとYは番号のせい（テスト番号は鳴らさない）。回線は止めず、回線の異常にも数えない。
        XとYは決まった順の続きで、あとへ。2026-10-24 管理者：「次の行に進んで、次の行がつながればすぐに回線の問題か番号の問題か判断
        できますよね」。⛔ 0129〜0141：違う番号が2つ続けば回線のせい・テスト番号を先に鳴らす、の別の道があり、元気な回線を止め（見直し21a の
        場面 a3・a7・a2：5日で停止29回）、次の行がすぐつながる場面でもテスト番号を鳴らした（場面 a9）。"""
        s = self.ready()
        self.numbered(s)
        x, y, rest, bad = self.two_bad(s)
        d = self.RefusesSome(bad, ring=True)
        self.assertEqual([self.round_(s, d).pause for _ in range(2)], [None, None])   # X・Y が鳴らない：次の行へ
        self.assertEqual(self.blames(s, [x, y]), ["pending", "pending"])
        self.assertEqual((self.faults(s), self.closures(s), self.line_stops(s)), ([], [], []))
        got = self.round_(s, d)                                                # 次の行がつながった
        self.assertEqual(self.tx(s, lambda tx: tx.one("SELECT task_id FROM salescall.attempts WHERE attempt_id = %s",
                                                      (got.called,)))["task_id"], rest[0]["task"])
        self.assertEqual(self.blames(s, [x, y]), ["number", "number"])
        self.assertEqual(d.rings, [])                                          # テスト番号は鳴らさない
        self.assertEqual((self.faults(s), self.line_stops(s)), ([], []))
        now = self.tx(s, lambda tx: tx.one("SELECT now() AS t")["t"])
        for r in (x, y):
            self.assertEqual(self.tx(s, lambda tx, r=r: attempts.failed_run(tx, r["contact"]))["n_sure"], 1)   # 番号に数える
            self.assertGreater(self.tx(s, lambda tx, r=r: next_action.current(tx, r["task"]))["call_at"], now)   # 決まった順で、あとへ

    def test_bad_numbers_on_a_broken_line_are_the_lines_by_the_test_number_once_no_row_is_left(self):
        """壊れた回線（どの番号もかけられない・テスト番号も鳴らない）：分からない電話のあとも次の行へ進み（その電話も、まだ分からない＝
        番号の4回にも回線の異常にも数えない）、その回線にまもなくかける行が無くなったら、テスト番号で決める。鳴らなかった＝回線のせい：
        続きの電話をまとめて回線の異常として数えて止め、知らせる。行は取りやめにならない（番号に数えない。かけた予定のまま）。テスト番号は
        1回だけ。"""
        s = self.ready()
        phone = self.numbered(s)
        x, y, rest, bad = self.two_bad(s)
        rows = [x, y] + rest
        d = self.RefusesSome({self.bad_number(r) for r in rows}, ring=False)
        self.assertEqual([self.round_(s, d).pause for _ in rows], [None] * len(rows))
        self.assertEqual((d.rings, self.faults(s)), ([], []))
        got = self.round_(s, d)                                                # かける行が無くなった：テスト番号で決める
        self.assertEqual((got.called, got.pause, d.rings), (None, line.UNRUNG_PAUSE, [phone]))
        self.assertEqual(self.closures(s), [("test_silent", "line")])
        self.assertEqual(self.blames(s, rows), ["line"] * len(rows))
        self.assertEqual(self.faults(s), ["line"] * len(rows))
        self.assertTrue(self.line_stops(s))
        self.no_row_lost(s, rows)
        for r in rows:
            self.assertEqual(self.tx(s, lambda tx, r=r: attempts.failed_run(tx, r["contact"]))["n"], 0)

    def test_outside_the_test_numbers_hours_bad_numbers_wait_and_never_stop_a_healthy_line(self):
        """テスト番号を鳴らしてよい時間の外（見直し21a の場面 a3・a7：テスト番号は昼だけ）：かけられない番号XとYが並んでも、次の行が
        つながれば番号のせい（回線は止めない）。その回線にかける行が残っていないあいだは、決めずに待ち（違う番号の数で回線のせいにしない＝
        元気な回線を止めない）、鳴らしてよい時間になったらテスト番号で決める。⛔ 0141：時間の外は「違う番号2つで回線のせい」に戻り、朝に
        回線を止めて、昼まで元気な行にかけなかった。"""
        s = self.ready()
        self.numbered(s)
        self.ring_hours(s, inside=False)
        x, y, rest, bad = self.two_bad(s)
        d = self.RefusesSome(bad, ring=True)
        for _ in range(3):
            self.round_(s, d)
        self.assertEqual(self.blames(s, [x, y]), ["number", "number"])
        self.assertEqual((d.rings, self.faults(s), self.line_stops(s)), ([], [], []))
        t = self.ready()                                                       # どの行もかけられない・時間の外
        phone = self.numbered(t)
        self.ring_hours(t, inside=False)
        x, y, rest, bad = self.two_bad(t)
        rows = [x, y] + rest
        d = self.RefusesSome({self.bad_number(r) for r in rows}, ring=False)
        for _ in rows:
            self.round_(t, d)
        for _ in range(2):
            self.assertEqual(self.round_(t, d).pause, line.RING_TEST_PAUSE)    # 決めずに待つ（時間の始まりまで）
        self.assertEqual((d.rings, self.faults(t), self.closures(t), self.line_stops(t)), ([], [], [], []))
        self.assertEqual(set(self.blames(t, rows)), {"pending"})
        self.ring_hours(t, inside=True)                                        # 鳴らしてよい時間になった
        self.round_(t, d)
        self.assertEqual(d.rings, [phone])
        self.assertEqual(set(self.blames(t, rows)), {"line"})

    # 見直し20a の (10)(11)：外しても落ちなかった判断の試験
    def test_one_test_ring_at_a_time_per_contractor_across_lines_and_supervisors(self):
        """テスト番号は、契約者ごとに1本ずつ（回線をまたいで・中央の見守り役をまたいで同時に鳴らさない＝いまは管理者自身の電話）。
        決まりはデータベースの鍵（Access.exclusive。契約者ごと）：ほかの回線が鳴らしている途中・ほかの見守り役（入れ替えで新旧が重なる）
        が鍵を持っているあいだは鳴らさず、記録もしない（WAIT）。⛔ 見直し20a：前の鍵はプログラムの中だけで、見守り役の入れ替えで新旧が
        重なると同時に鳴りえた。"""
        s = self.ready()
        phone = self.numbered(s)
        other = self.w.line(s["tenant"])
        ringing, release = threading.Event(), threading.Event()

        class Slow(FakeDriver):
            def test_ring(self, to):
                ringing.set()
                release.wait(10)
                return super().test_ring(to)
        a, b = Slow(ring=True), FakeDriver(ring=True)
        got = {}
        th = threading.Thread(target=lambda: got.setdefault("a", ring_test.run(self.w.a, SYSTEM, s["tenant"], s["line"], a,
                                                                                 "no_rows")))
        th.start()
        self.assertTrue(ringing.wait(10))
        self.assertEqual(ring_test.run(self.w.a, SYSTEM, s["tenant"], other, b, "no_rows"), ring_test.Result.WAIT)
        self.assertEqual(b.rings, [])                                           # ほかの回線が鳴らしている途中：鳴らさない
        release.set()
        th.join(10)
        self.assertEqual((got["a"], a.rings), (ring_test.Result.RANG, [phone]))
        self.next_ring()
        with support.owner_access().exclusive(f"salescall.ring_test:{s['tenant']}") as held:   # ほかの見守り役が持っている
            self.assertTrue(held)
            self.assertEqual(ring_test.run(self.w.a, SYSTEM, s["tenant"], other, b, "no_rows"), ring_test.Result.WAIT)
        self.assertEqual(b.rings, [])
        self.assertEqual(ring_test.run(self.w.a, SYSTEM, s["tenant"], other, b, "no_rows"), ring_test.Result.RANG)
        self.assertEqual(self.ring_tests_of(s), [("no_rows", True)])            # 記録は鳴らした分だけ

    def test_a_phone_part_that_fails_while_ringing_the_test_number_is_unknown_not_silent(self):
        """テスト番号を鳴らす途中で電話の部品が誤りを返した：「分からない」（どちらの証拠にもしない。記録だけ）。「鳴らなかった」に
        しない（回線のせいの証拠にしない＝続きを閉じない）。"""
        s = self.ready()
        self.numbered(s)
        self.fail_once(s, self.Refuses(self.bad_number(s)))

        class Broken(FakeDriver):
            def test_ring(self, to):
                raise RuntimeError("試験：電話の部品の誤り")
        self.assertEqual(ring_test.run(self.w.a, SYSTEM, s["tenant"], s["line"], Broken(), "no_rows"), ring_test.Result.UNKNOWN)
        self.assertEqual((self.ring_tests_of(s), self.closures(s)), ([("no_rows", None)], []))

    def test_a_row_waiting_on_the_contractor_does_not_keep_the_run_open(self):
        """まもなくかけられる行（scheduler.soon_left）に、門が必ず断って待ち続ける行（契約者の承認待ち など。gate.waits_unknown）は
        数えない：その行を待たずに、テスト番号で決めにいく。"""
        s = self.ready()
        phone = self.numbered(s)
        self.put_client(s, "approval.human", True)
        self.tx(s, lambda tx: approval.approve_task(tx, s["task"]), OPERATOR)
        waiting = self.rows(s, 2)[1]                                           # 承認されていない（いま予定の行）
        self.assertEqual(self.tx(s, lambda tx: gate.waits_unknown(tx, waiting["task"], s["line"])), "awaiting_approval")
        d = self.Refuses(self.bad_number(s))
        d.ring = True
        line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, d, Runner())   # X が鳴らない
        jobs.settle_ready(self.ctx(s))
        line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, d, Runner())   # かける行が無い：決めにいく
        self.assertEqual(d.rings, [phone])


class FakeThread:
    """回線ごとの流れの代わり：止める印が立てば止まる（died＝思いがけず止まった）。"""

    def __init__(self, stop):
        self.stop, self.died = stop, False

    def is_alive(self):
        return not (self.died or self.stop.is_set())

    def join(self, timeout=None):
        pass


class Main(SupervisorBase):
    def starter(self):
        started = []

        def start(cfg, access, at, line_id, holder, links):
            started.append((line_id, at.pc_id, FakeThread(threading.Event())))
            return started[-1][2], started[-1][2].stop
        return started, start

    def supervisor(self, s, links=None, **kw):
        """試験の契約者だけを見る中央の見守り役（試験のデータベースのほかの契約者の仕事をしない）。"""
        sup = Supervisor(self.cfg(s), links if links is not None else self.links(s), holder=HOLDER, **kw)
        sup._load_tenants = lambda: setattr(sup, "tenants", [s["tenant"]])
        return sup

    def holder_of(self, s):
        return self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: tx.one(
            "SELECT lease_holder FROM salescall.lines WHERE line_id = %s", (s["line"],)))["lease_holder"]

    def test_stopping_waits_for_a_live_call_however_long(self):
        """止めるとき、話している通話（supervisor.call_live の1か所）がある回線の流れは、待つ長さを過ぎても終わるまで待つ（通話の長さで
        打ち切らない＝2026-10-24 ユーザー。⛔ 見直し19a の壊し試し：待つ長さで打ち切っても落ちる試験が無かった）。"""
        s = self.ready()
        started, start = self.starter()
        sup = self.supervisor(s, start_worker=start)
        sup.start()
        flow = started[0][2]
        flow.stop.set = lambda: None                                            # 流れは通話の途中（止まらない）
        call = mock.Mock(live=True)
        supervisor.call_began(s["line"], call)

        def hang_up():
            supervisor.call_over(s["line"], call)
            flow.died = True
        timer = threading.Timer(1.5, hang_up)
        timer.start()
        began = time.monotonic()
        try:
            sup.shutdown(0)
        finally:
            timer.cancel()
            supervisor.call_over(s["line"], call)
        self.assertGreaterEqual(time.monotonic() - began, 1.4)

    def test_a_line_flow_that_stops_is_cleaned_up_and_started_again(self):
        s = self.ready()
        started, start = self.starter()
        sup = self.supervisor(s, start_worker=start)
        sup.start()
        self.assertEqual((len(started), sup.leased), (1, {s["line"]: s["tenant"]}))
        c = self.claim(s)
        line.dial_exit.dial(self.w.a, SYSTEM, s["tenant"], s["client"], c.attempt_id, FakeDriver())
        started[0][2].died = True                                              # 回線ごとの流れが思いがけず止まった
        sup.watch_workers(0.0)
        self.assertEqual(self.state(s, c.attempt_id), "technical_error")        # その回線の通話を後始末
        self.assertTrue(self.asked_supervisor(s))
        sup.watch_workers(1.0)
        self.assertEqual(len(started), 1)                                       # すぐには立ち上げ直さない
        sup.watch_workers(1000.0)
        self.assertEqual(len(started), 2)                                       # 間をあけて立ち上げ直す
        self.lease(s, holder="ほかの見守り役#x")                               # 回線をほかに取られた（期限切れのあと）
        sup.renew(2000.0)
        self.assertEqual((sup.leased, started[1][2].stop.is_set()), ({}, True))  # その回線の流れは止める
        sup.watch_workers(2001.0)
        self.assertNotIn(s["line"], sup.workers)
        self.assertEqual(self.holder_of(s), "ほかの見守り役#x")                  # ほかの見守り役の回線は返さない
        sup.shutdown(0)

    def test_a_line_flow_that_dies_during_a_call_hangs_up_with_an_apology(self):
        """回線ごとの流れが思いがけず止まったとき、スマホがまだ通話中なら、おわびの録音を流して切ってもらう（データベースの後始末
        だけでは、電話がつながったまま無言になる。⛔ 2026-10-22 の見直し）。通話中でなければ、何もしない。"""
        s = self.ready()
        started, start = self.starter()
        links = self.links(s)
        in_call = FakeDriver(busy=True)
        links.driver = lambda line_id, at: in_call
        sup = self.supervisor(s, links, start_worker=start)
        sup.start()
        started[0][2].died = True
        sup.watch_workers(0.0)
        self.assertEqual(getattr(in_call, "apologized", 0), 1)
        idle = FakeDriver(busy=False)
        links.driver = lambda line_id, at: idle
        sup.watch_workers(1000.0)                                              # 間をあけて立ち上げ直した
        started[1][2].died = True
        sup.watch_workers(1001.0)
        self.assertEqual(getattr(idle, "apologized", 0), 0)                    # 通話中でない回線には、おわびを流さない
        sup.shutdown(0)

    def test_a_busy_connection_pool_keeps_the_line_and_is_recorded_later_in_one_line(self):
        """データベースのつなぎの混雑（共有のつなぎが全部使われている）は、届かないのとは分ける：借りている回線の流れは止めず、次の回に
        延ばす。誤りは回数だけを数え、あとでまとめて1行に記録する（⛔ 2026-10-22 の見直し：前は回線を手放し、混雑のたびに記録を
        書いて、さらに混ませた）。"""
        from salescall import supervisor as package
        from salescall.core.access import PoolBusy
        s = self.ready()
        started, start = self.starter()
        sup = self.supervisor(s, start_worker=start)
        sup.start()

        tried = []

        class Busy:
            def __getattr__(self, name):
                def fail(*a, **k):
                    tried.append(name)
                    raise PoolBusy("試験：つなぎの混雑")
                return fail
        before = package._BUSY["回線の借用"]
        sup.links.attached[uuid.uuid4()] = sup.links.attached[s["line"]]       # もう1本つながっている
        real, sup.access = sup.access, Busy()
        sup.renew(10.0)
        self.assertEqual(len(tried), 1)                                         # 混んでいると分かったら、この回はもう借りに行かない
        with mock.patch.object(main_mod, "flush_busy") as flushed:
            sup._next_renew = 0.0
            sup.tick(11.0)                                                      # この回の借用も混んだ
            flushed.assert_not_called()                                         # 混んだ回には、混雑の記録を書きに行かない
            sup.access = real
            sup.tick(12.0)
            flushed.assert_called_once_with(real)                               # 混雑の無い回に、まとめて書く
        self.assertEqual((sup.leased, started[0][2].stop.is_set()), ({s["line"]: s["tenant"]}, False))   # 流れは止めない
        self.assertGreaterEqual(package._BUSY["回線の借用"], before + 1)        # 記録は書かず、数えるだけ
        package.flush_busy(real)
        self.assertEqual(package._BUSY["回線の借用"], 0)
        noted = self.w.a.in_system(SYSTEM, lambda tx: tx.all(
            "SELECT message FROM salescall.error_events WHERE source = 'supervisor' AND message LIKE %s",
            ("回線の借用：共有のつなぎの混雑%",)))
        self.assertTrue(noted)                                                 # 空いたときに、まとめて記録した
        sup.shutdown(0)

    def test_a_stuck_job_is_reported_once_and_does_not_hold_up_stopping(self):
        """決まった間隔の仕事が、借りる長さを過ぎても終わらない（固まった）ときは、1回だけ記録する。止めるときは、決めた長さだけ
        待って止まる（⛔ 2026-10-22 の見直し：前は誰も気づかず、止めるときも待ち続けた）。"""
        s = self.ready()
        release = threading.Event()
        stuck = jobs.Job(f"試験の固まる仕事{uuid.uuid4().hex[:6]}", 60, "tenant", 5, lambda c: release.wait(30))
        sup = self.supervisor(s)
        sup.check_version()
        sup._load_tenants()
        try:
            with mock.patch.object(jobs, "JOBS", (stuck,)), mock.patch.object(main_mod, "report") as reported:
                sup.run_jobs(0.0)
                sup.run_jobs(3.0)                                              # まだ借りる長さの内側
                self.assertEqual(reported.call_count, 0)
                sup.run_jobs(100.0)
                sup.run_jobs(200.0)                                            # 1回だけ
                self.assertEqual(reported.call_count, 1)
                self.assertIsInstance(reported.call_args.args[3], TimeoutError)
                began = time.monotonic()
                sup.shutdown(0.5)
                self.assertLess(time.monotonic() - began, 10)                 # 固まった仕事を待ち続けない
        finally:
            release.set()

    def test_a_line_the_pc_lets_go_is_returned_and_a_line_moved_to_another_pc_starts_there_after_the_old_flow_ends(self):
        """PC がつながりを切った回線は、流れを止め、止まってから後始末して返す（通話の途中では返さない）。同じ回線を別の PC が
        つなぎ直したら、前の流れが止まってから、新しい PC で始める（同じスマホで二重にかけない）。"""
        s = self.ready()
        started, start = self.starter()
        links = self.links(s)
        sup = self.supervisor(s, links, start_worker=start)
        sup.start()
        first = started[0][2]
        links.attached[s["line"]] = Attached(s["tenant"], uuid.UUID(int=2))     # 別の PC がつなぎ直した
        first.stop.set = lambda: None                                           # 前の流れは、まだ通話の途中（止まらない）
        sup.renew(10.0)
        self.assertEqual(len(started), 1)                                       # 前の流れが止まるまで始めない
        sup.watch_workers(11.0)
        sup.renew(12.0)
        self.assertEqual(len(started), 1)
        first.died = True                                                       # 前の流れが止まった
        sup.watch_workers(13.0)
        sup.renew(14.0)
        self.assertEqual((len(started), started[1][1]), (2, uuid.UUID(int=2)))  # 新しい PC で始めた
        links.attached[s["line"]] = Attached(s["tenant"], uuid.UUID(int=2), 7)  # 同じ PC が、すぐつなぎ直した（新しいつなぎ）
        sup.renew(15.0)
        self.assertTrue(started[1][2].stop.is_set())                           # 前のつなぎの流れは止める
        sup.watch_workers(16.0)
        sup.renew(17.0)
        self.assertEqual((len(started), started[2][1]), (3, uuid.UUID(int=2)))  # 新しいつなぎで立ち上げ直した
        c = self.claim(s)
        links.attached.clear()                                                  # PC がつながりを切った
        started[2][2].stop.set = lambda: None                                   # まだ通話の途中
        self.lease(s, seconds=-1)                                               # 借用の期限が切れかけていても
        sup.renew(20.0)
        alive = self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: tx.one(
            "SELECT lease_expires_at > now() AS ok FROM salescall.lines WHERE line_id = %s", (s["line"],)))["ok"]
        self.assertTrue(alive)                                                  # 流れが動いているあいだは延ばし続ける（ほかの見守り役に後始末させない）
        sup.watch_workers(21.0)
        self.assertEqual((self.state(s, c.attempt_id), self.holder_of(s)), ("claimed", HOLDER))   # 通話の途中は返さない
        started[2][2].died = True
        sup.watch_workers(22.0)
        self.assertEqual((self.state(s, c.attempt_id), self.holder_of(s), sup.leased, sup.workers),
                         ("released", None, {}, {}))                            # 止まってから後始末して返す
        sup.shutdown(0)

    def test_the_supervisor_keeps_going_when_the_database_is_unreachable(self):
        """データベースに届かない・回線ごとの流れを立ち上げられない、のどれでも本体は止まらない（次の回にまた試す。続けば間を延ばす）。"""
        s = self.ready()
        class Unreachable:
            def __getattr__(self, name):
                def fail(*a, **k):
                    raise ConnectionError("試験：データベースに届かない")
                return fail
        sup = self.supervisor(s, start_worker=lambda *a: (_ for _ in ()).throw(OSError("試験")))
        real, sup.access = sup.access, Unreachable()
        sup.tick(0.0)                                                          # 止まらない
        self.assertEqual(sup._next_renew, main_mod.RENEW_FALLBACK)
        sup.access = real
        sup.tick(100.0)                                                        # 届いた：借りられたが、立ち上げられない
        w = sup.workers[s["line"]]
        self.assertEqual((w.thread, w.fails, w.restart_at), (None, 1, 100.0 + main_mod.RESTART_FIRST))
        sup.tick(101.0)                                                        # 間をあけるまでは試さない
        self.assertEqual(sup.workers[s["line"]].fails, 1)
        sup.shutdown(0)

    def test_a_server_program_out_of_step_with_the_database_makes_no_new_calls_and_does_no_jobs(self):
        """サーバーのプログラムとデータベースの版がずれていれば、回線ごとの流れを止め（いまの通話は最後まで）、立ち上げず、決まった
        間隔の仕事もしない。ずれは CL-2 へ。版が合ったら、またかける。"""
        s = self.ready()
        started, start = self.starter()
        sup = self.supervisor(s, start_worker=start)
        sup.start()
        self.assertEqual(len(started), 1)
        real = updates.PROGRAM
        updates.PROGRAM = ("0001_古いプログラム.sql", "0" * 64)                    # データベースが先に新しくなった
        try:
            sup.check_version()
            sup.renew(100.0)
            self.assertEqual((sup.version_gap, started[0][2].stop.is_set(), sup.leased),
                             (updates.PROGRAM_OLD, True, {s["line"]: s["tenant"]}))
            sup.watch_workers(10_000.0)
            self.assertEqual((len(started), s["line"] in sup.workers), (1, False))   # 立ち上げない
            sup._running.clear()
            sup.run_jobs(10_000.0)
            self.assertEqual(sup._running, {})                                    # 決まった間隔の仕事もしない
            c = self.claim(s)                                                     # 止めた流れが残した試行（落ちた）は、後始末する
            sup.renew(300.0)
            self.assertEqual(self.state(s, c.attempt_id), "released")
            logged = self.w.a.in_system(SYSTEM, lambda tx: tx.all(
                "SELECT 1 FROM salescall.error_events WHERE source = 'supervisor' AND message = '版のずれ：RuntimeError'"))
            self.assertTrue(logged)                                               # ずれは「見守り役の誤り」＝CL-2 へ修正依頼
        finally:
            updates.PROGRAM = real
        sup.check_version()
        sup.renew(400.0)                                                          # 版が合った：またかける
        self.assertEqual((sup.version_gap, len(started)), (updates.SAME, 2))
        sup.shutdown(0)
        program = ("0080_b.sql", "f" * 64)
        self.assertEqual([updates.gap(x, program) for x in (("0081_c.sql", "1"), ("0079_a.sql", "1"), ("0080_b.sql", "f" * 64),
                                                             ("0080_b.sql", "2"), (None, None), None)],
                         [updates.PROGRAM_OLD, updates.PROGRAM_NEW, updates.SAME, updates.MISMATCH, updates.UNKNOWN,
                          updates.UNKNOWN])
        self.assertEqual(updates.gap(("0080_b.sql", "f" * 64), None), updates.UNKNOWN)   # 表の作りのファイルが無い＝分からない
        live = self.w.a.in_tenant(SYSTEM, s["tenant"], updates.current)
        self.assertEqual(live, updates.SAME)                                     # プログラムとデータベースの指紋は同じ決まり
        # Windows で行の終わりが CRLF で取り出されても、指紋は同じ（⚠️ 前は「途中が違う」と読み、かけなくなった）
        import shutil
        from salescall.core import migrate
        crlf = Path(tempfile.mkdtemp(prefix="salescall-test-crlf-"))
        try:
            for f in migrate.migration_files():
                (crlf / f.name).write_bytes(f.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
            self.assertEqual(migrate.fingerprint(crlf), migrate.fingerprint())
        finally:
            shutil.rmtree(crlf)

    def test_an_old_pc_program_is_told_to_update_once_a_day_per_pc(self):
        """PC の部品のプログラムが古い：その契約者と管理者へ知らせる（自動で入れ替える部品があり、設定が自動なら「自動で入れ替えます」。
        無ければ「更新してください」）。同じ PC は1日1回。"""
        s, t = self.ready(), self.ready()
        pc = uuid.uuid4()
        for _ in range(2):
            self.assertFalse(self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: updates.pc_out_of_date(tx, s["tenant"], pc,
                                                                                                     can_replace=False)))
        notes = self.w.a.in_tenant(OPERATOR, s["tenant"], lambda tx: tx.all(
            "SELECT summary FROM salescall.notification_events WHERE kind = 'program_update'"))
        self.assertEqual(len(notes), 1)
        self.assertIn("プログラムを更新してください", notes[0]["summary"])
        admin = self.w.a.in_system(ADMIN, lambda tx: tx.all(
            "SELECT 1 FROM salescall.notification_events WHERE kind = 'program_update' AND origin_tenant = %s", (s["tenant"],)))
        self.assertEqual(len(admin), 1)                                           # 管理者にも写し
        self.assertTrue(self.w.a.in_tenant(SYSTEM, t["tenant"], lambda tx: updates.pc_out_of_date(tx, t["tenant"], pc,
                                                                                                can_replace=True)))
        auto = self.w.a.in_tenant(OPERATOR, t["tenant"], lambda tx: tx.one(
            "SELECT summary FROM salescall.notification_events WHERE kind = 'program_update'"))["summary"]
        self.assertIn("自動で入れ替えます", auto)

    def test_the_supervisor_calls_through_a_line_flow_and_returns_the_line(self):
        """本物の立ち上げ方（回線ごとに別の流れ）で1本かけ、決まった間隔の仕事が締め、止めると回線を返す。"""
        s = self.ready()
        sup = self.supervisor(s)
        sup.start()
        done = None
        deadline = time.monotonic() + 60
        while done is None and time.monotonic() < deadline:
            sup.tick()
            time.sleep(0.5)
            done = self.tx(s, lambda tx: tx.one("SELECT business_state FROM salescall.task_results WHERE task_id = %s",
                                                (s["task"],)))
        sup.shutdown(30)
        self.assertEqual(done and done["business_state"], "completed")
        self.assertIsNone(self.holder_of(s))                                     # 止めたら回線を返す

    def test_a_line_whose_call_part_never_comes_back_is_rebuilt_and_told(self):
        """見直し20a の (6)：通話を進める部品が固まり、終えると決めたあとも戻らない：おわびを流し終える長さ（protocol.APOLOGY_WAIT。試験では
        0秒）と call.frozen_after（試験では1秒）を過ぎたら（0143。⛔ 0141：仮の設定 call.stuck_after）、中央の見守り役がその回線の流れを
        捨てて立て直し（古い流れには止める印を立てる＝戻れば、そこで終わる。新しい通話の部品で、次の電話をかける）、契約者と管理者に知らせ、
        CL-1 へ修正依頼を出す（判断は line._Call.abandoned の1か所。伝えるのは watch.after_error の1か所＝種類 runner_stuck）。⛔ 前は流れが
        生きているあいだ何もせず、借用を延ばし続け、その回線は二度とかけず、だれにも知らされなかった。"""
        s = self.ready()
        other = self.w.task(s["tenant"], s["client"], s["run"], self.w.contact(s["tenant"], s["client"],
                                                                               normalize(fictional_jp_fixed()).e164))
        self.tx(s, lambda tx: next_action.schedule_now(tx, other, cause="試験"))
        del STUCK_MADE[:]
        StuckRunner.release.clear()
        self.addCleanup(StuckRunner.release.set)
        limits = line.CallLimits(frozen_after=1.0)
        with mock.patch.object(line, "call_limits", lambda tx: limits), mock.patch.object(line, "WATCH_EVERY", 0.05), \
                mock.patch.object(line, "APOLOGY_WAIT", 0.0):
            sup = Supervisor(self.cfg(s, SALESCALL_CALL_RUNNER="tests.test_supervisor:make_stuck_runner"), self.links(s),
                             holder=HOLDER)
            sup._load_tenants = lambda: setattr(sup, "tenants", [s["tenant"]])
            sup.start()
            try:
                first, first_stop = sup.workers[s["line"]].thread, sup.workers[s["line"]].stop
                stuck = until_true(lambda: supervisor.stuck_call(s["line"]), 20)
                self.assertTrue(stuck.ended)                                   # おわびして終えた（戻らない）
                self.assertEqual(stuck.stuck_source, "runner")
                sup.watch_workers(time.monotonic())                             # 戻らない：流れを捨てて立て直す
                self.assertTrue(first.is_alive())                               # 固まった流れはそのまま（止める印だけ）
                self.assertTrue(first_stop.is_set())                            # 戻れば、そこで終わる（二重にかけない）
                self.assertIsNone(supervisor.stuck_call(s["line"]))
                sup.watch_workers(time.monotonic())
                self.assertIsNot(sup.workers[s["line"]].thread, first)         # 新しい流れ・新しい通話の部品
                self.assertEqual(len(STUCK_MADE), 2)
                done = until_true(lambda: self.tx(s, lambda tx: tx.one(
                    "SELECT 1 FROM salescall.attempts WHERE line_id = %s AND state = 'ended'", (s["line"],))), 30)
                self.assertTrue(done)                                           # 次の電話をかけて話した
            finally:
                StuckRunner.release.set()
                sup.shutdown(10)
        told = self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: [r["kind"] for r in tx.all(
            "SELECT kind FROM salescall.error_events WHERE line_id = %s ORDER BY error_id", (s["line"],))])
        self.assertEqual(told.count("runner_stuck"), 1)
        self.assertIn("call_frozen", told)
        notes = self.w.a.in_system(ADMIN, lambda tx: tx.all(
            "SELECT 1 FROM salescall.notification_events WHERE kind = 'error_notice' AND origin_tenant = %s"
            " AND refs ->> 'kind' = 'runner_stuck'", (s["tenant"],)))
        self.assertEqual(len(notes), 1)                                         # 管理者へ
        mine = self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: tx.all(
            "SELECT 1 FROM salescall.notification_events WHERE kind = 'error_notice' AND tenant_id = %s"
            " AND refs ->> 'kind' = 'runner_stuck'", (s["tenant"],)))
        self.assertEqual(len(mine), 1)                                          # 契約者へ
        self.assertTrue([r for r in self.w.a.in_system(SYSTEM, lambda tx: fix_requests.open_requests(tx, "CL-1"))
                         if r["kind"] == "runner_stuck"])                       # CL-1 へ修正依頼

    def test_a_call_part_that_came_back_is_never_abandoned_but_a_stuck_writer_is(self):
        """「戻らない」（line._Call.abandoned の1か所。0143）：(1) 通話を進める部品が戻ったら、終えると決めてから長くたっても戻らないとは
        しない (2) 通話を進める部品が戻ったあと、書き手が進まない（書き込み1つが call.frozen_after より長く終わらない＝データベースが
        止まった）：中央の見守り役が気づき（supervisor.stuck_call）、その回線の流れを捨てて立て直し（古い流れには止める印）、出どころ
        「保存」として契約者と管理者に知らせ、保存の担当（CL-2）へ修正依頼を出す（main.Supervisor._abandon の1か所）。⛔ 見直し21a の 5・
        21b の 1（場面 w12）：戻ったあとの書き手の待ちは上限なしで、書き込みがデータベースで止まると、回線が黙って止まった。"""
        class Returns:
            def run(self, access, actor, claim, *, tenant_id, client_id, call=None):
                self.call = call
                call.beat()
                call.write(conversation_io.move_work(claim.attempt_id, "ringing"))   # 終えないまま戻る（落ちたのと同じ）
                call.flush()

        class SlowLastWrite:
            def __init__(self):
                self.call = None

            def run(self, access, actor, claim, *, tenant_id, client_id, call=None):
                self.call = call
                call.beat()
                for st in ("ringing", "answered", "in_conversation", "ending", "ended"):
                    call.write(conversation_io.move_work(claim.attempt_id, st))
                call.write(lambda tx: tx.execute("SELECT pg_sleep(3)"))           # 最後の書き込みがデータベースで止まる
        with mock.patch.object(line, "frozen_after", lambda tx: 0.5), mock.patch.object(line, "WATCH_EVERY", 0.05), \
                mock.patch.object(line, "APOLOGY_WAIT", 0.0), mock.patch.object(line, "report"):
            s = self.ready()
            runner = Returns()
            line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, FakeDriver(), runner)
            self.assertIsNotNone(runner.call.end_at)                          # こちらのせいで終えた（runner_error）
            time.sleep(0.7)
            self.assertFalse(runner.call.abandoned)                           # (1) 戻った部品は、戻らないとしない
            t2 = self.ready()
            runner = SlowLastWrite()
            got = {}
            th = threading.Thread(target=lambda: got.setdefault("r", line.one_round(
                self.w.a, SYSTEM, t2["tenant"], t2["line"], HOLDER, FakeDriver(), runner)))
            th.start()
            stuck = until_true(lambda: supervisor.stuck_call(t2["line"]), 10)   # (2) 見張りが気づく
            self.assertIsNotNone(stuck)
            self.assertEqual(stuck.stuck_source, "storage")
            left = FakeDriver(busy=True)                                       # 電話はまだつながっている（と電話の部品が答える）

            class StillOn(FakeLinks):
                def driver(self, line_id, at):
                    return left
            sup = self.supervisor(t2, links=StillOn({t2["line"]: Attached(t2["tenant"], uuid.UUID(int=1))}))
            stop = threading.Event()
            w = main_mod._Worker(t2["line"], Attached(t2["tenant"], uuid.UUID(int=1)), FakeThread(stop), stop)
            sup.workers[t2["line"]] = w
            sup._abandon(t2["line"], w, stuck, time.monotonic())
            self.assertTrue(stop.is_set())                                     # 古い流れには止める印
            self.assertIsNone(sup.workers[t2["line"]].thread)                  # 立て直す（次の回に新しい流れ）
            self.assertEqual(getattr(left, "apologized", 0), 1)               # つながったままの電話は、おわびして切ってもらう（見直し22 の 8）
            th.join(15)
        self.assertEqual(self.state(t2, got["r"].called), "ended")              # 話し終えた電話（おわびは流さない）
        told = self.w.a.in_tenant(SYSTEM, t2["tenant"], lambda tx: [(r["source"], r["kind"]) for r in tx.all(
            "SELECT source, kind FROM salescall.error_events WHERE line_id = %s ORDER BY error_id", (t2["line"],))])
        self.assertEqual(told, [("storage", "storage")])
        self.assertTrue([r for r in self.w.a.in_system(SYSTEM, lambda tx: fix_requests.open_requests(tx, "CL-2"))
                         if r["kind"] == "storage"])

    def test_only_one_supervisor_per_machine(self):
        from salescall.link.locks import AlreadyRunning, SingleInstance
        path = Path(tempfile.mkdtemp()) / "lock"
        with SingleInstance(path):
            with self.assertRaises(AlreadyRunning):
                with SingleInstance(path):
                    pass

    def test_which_jobs_run(self):
        """仕事は、すべて中央の見守り役がする（5-5。前の「契約者の PC だけ・管理者の PC だけ」の分け方は無い）。ほかの担当の部品が
        要る仕事は、その設定があるときだけ。データベース全体の控えは、書く管理者の PC と接続の設定があるときだけ。控えの見張り・
        止まったまま続く停止の修正依頼・PC の合図の見張りは、いつも動く（取る仕事が動かないことに気づく）。"""
        s = self.ready()
        here = lambda cfg: {j.name for j in jobs.JOBS if j.wanted(cfg)}   # noqa: E731
        bare = here(self.cfg(s))
        self.assertTrue({"recover", "settle", "backups", "backup_watch", "whole_backup_watch", "lasting_stops",
                         "system_lasting_stops", "pc_watch", "budget", "holidays", "fixed_stops"} <= bare)   # fixed_stops＝直した版で解く（0139）
        self.assertFalse({"whole_backup", "verify", "stop_checks", "system_stop_checks"} & bare)
        full = here(self.cfg(s, SALESCALL_OWNER_DSN="x", SALESCALL_RESTORE_DSN="y", SALESCALL_ADMIN_TENANT=str(uuid.uuid4()),
                             SALESCALL_VERIFIER="m:v", SALESCALL_PROBER="m:p"))
        self.assertEqual(full, {j.name for j in jobs.JOBS})

    def test_tenant_jobs_run_once_for_every_tenant_and_system_jobs_once(self):
        """契約者ごとの仕事は、すべての契約者について1つずつ（その契約者の取引で）。共通の仕事は1つ。"""
        s, t = self.ready(), self.ready()
        seen = []
        fake = (jobs.Job("試験の契約者ごと", 60, "tenant", 60, lambda c: seen.append(("tenant", c.tenant_id))),
                jobs.Job("試験の共通", 60, "system", 60, lambda c: seen.append(("system", c.tenant_id))))
        sup = Supervisor(self.cfg(s), holder=f"試験#{uuid.uuid4().hex[:6]}")
        sup.check_version()
        sup._load_tenants()
        with mock.patch.object(jobs, "JOBS", fake):
            sup.run_jobs(0.0)
            sup.pool.shutdown(wait=True)
        self.assertIn(("tenant", s["tenant"]), seen)
        self.assertIn(("tenant", t["tenant"]), seen)
        self.assertEqual(seen.count(("system", None)), 1)
        self.assertEqual(len([x for x in seen if x[0] == "tenant"]), len(sup.tenants))

    def test_one_client_failing_does_not_hide_the_backups_of_the_others(self):
        """控えの見張りは依頼者ごと。1つの依頼者の見張りで誤りが出ても、ほかの依頼者を見張る（見直し9：前は最初の誤りで残りを見なかった）。"""
        s = self.ready()
        second = self.ready(tenant=s["tenant"])["client"]
        seen = []

        def watch_client(app, tenant_id, client_id):
            seen.append(client_id)
            if len(seen) == 1:
                raise RuntimeError("試験：1つ目の依頼者で落ちる")
            return False
        with mock.patch.object(backup, "watch_client", watch_client), mock.patch.object(jobs, "report"):
            jobs.backup_watch(self.ctx(s))
        self.assertEqual(sorted(seen), sorted([s["client"], second]))

    def test_settings_are_reported_without_values(self):
        lines = config.status({"SALESCALL_DSN": "postgresql://架空の接続", "GEMINI_KEY_1": "架空のキー"})
        self.assertIn("SALESCALL_DSN：入っています", lines)
        self.assertNotIn("架空の接続", "\n".join(lines))
        self.assertEqual(config.gather({"SALESCALL_DSN": "x", "GEMINI_KEY_1": "架空のキー"}), {"SALESCALL_DSN": "x"})   # ほかの値は読まない
        with self.assertRaises(config.ConfigMissing):
            config.load({"SALESCALL_LISTEN": "0.0.0.0:1"})                     # データベースの接続が無い
        with self.assertRaises(config.ConfigMissing):
            config.load({"SALESCALL_DSN": "x", "SALESCALL_LISTEN": "番号なし"})
        cfg = config.load({"SALESCALL_DSN": "x", "SALESCALL_LISTEN": "0.0.0.0:8765"})
        self.assertEqual((cfg.listen, cfg.calls, cfg.whole_backup), (("0.0.0.0", 8765), False, False))
        self.assertNotIn("x", repr(cfg).replace("0.0.0.0", ""))                  # 接続は表示に出さない
        with self.assertRaises(config.ConfigMissing):                          # つなぎ口の場所が無ければ動かない（黙って電話をかけない）
            main_mod.serve(config.load({"SALESCALL_DSN": "x"}))
        with self.assertRaises(config.ConfigMissing):                          # 電話をかけるなら、通話後の検証の部品も要る（2026-10-22 の見直し：
            main_mod.serve(config.load({"SALESCALL_DSN": "x", "SALESCALL_LISTEN": "127.0.0.1:1",   # 無いと、かけた電話を締められず、
                                        "SALESCALL_CALL_RUNNER": "m:r"}))                            # 行もお客様も待ち続ける）
        with self.assertRaises(config.ConfigMissing) as got:                   # 電話をかけるなら、停止を解く確かめの部品も要る（見直し14：
            main_mod.serve(config.load({"SALESCALL_DSN": "x", "SALESCALL_LISTEN": "127.0.0.1:1",   # 無いと、回線・会話AIの停止が自動
                                        "SALESCALL_CALL_RUNNER": "m:r", "SALESCALL_VERIFIER": "m:v"}))   # では解けない）
        self.assertIn("SALESCALL_PROBER", str(got.exception))
        with self.assertRaises(config.ConfigMissing):                          # 移る間の形（この機械の中だけで待つ）は、この機械の PC の
            main_mod.serve(config.load({"SALESCALL_DSN": "x", "SALESCALL_LISTEN": "127.0.0.1:1"}))   # 部品の契約者が要る（見直し13）

    def test_the_supervisor_starts_again_by_itself_after_an_unexpected_error(self):
        """思いがけない誤りでは終わらず、間をあけて自分で動き直す（人が立ち上げ直さない。⛔ 2026-10-22 の見直し）。設定の抜けは、
        直すまで動けないので終わる（終わった理由を出す）。"""
        from salescall.supervisor import __main__ as entry
        calls = []

        def serve(cfg, stop=None):
            calls.append(cfg)
            if len(calls) == 1:
                raise RuntimeError("試験：思いがけない誤り")
        with mock.patch.object(entry.main, "serve", serve), mock.patch.object(entry.config, "load", lambda: "試験の設定"), \
                mock.patch.object(entry, "left_over", lambda: []), \
                mock.patch.object(entry.time, "sleep") as slept, mock.patch("builtins.print"):
            self.assertEqual(entry.run(["run"]), 0)
        self.assertEqual(len(calls), 2)
        slept.assert_called_once_with(entry.RESTART_WAIT)
        with mock.patch.object(entry.config, "load", side_effect=config.ConfigMissing("試験：設定の抜け")), \
                mock.patch.object(entry, "left_over", lambda: []), mock.patch("builtins.print"):
            self.assertEqual(entry.run(["run"]), 1)

    def test_the_end_signal_stops_like_ctrl_c(self):
        """終わりの合図（SIGTERM。Fly.io の入れ替えなど）は、Ctrl+C と同じ止め方（止める印を立てる＝新しい発信を止め、いまの通話の
        終わりを待ってから止まる）。⛔ 2026-10-22 の見直し14：前は受け取らず、話している通話がいきなり切れうった。"""
        import signal
        from salescall.supervisor import __main__ as entry
        seen = []
        before = signal.getsignal(signal.SIGTERM)

        def serve(cfg, stop=None):
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)             # 合図が届いた
            seen.append(stop.is_set())
        try:
            with mock.patch.object(entry.main, "serve", serve), mock.patch.object(entry.config, "load", lambda: "試験の設定"), \
                    mock.patch.object(entry, "left_over", lambda: []):
                self.assertEqual(entry.run(["run"]), 0)
        finally:
            signal.signal(signal.SIGTERM, before)
        self.assertEqual(seen, [True])
        src = Path(main_mod.__file__).read_text(encoding="utf-8")
        self.assertIn("while not stop.is_set():", src)                         # 本体は止める印で回るのをやめ、止め方（shutdown）へ

    def test_flows_left_from_the_previous_round_end_the_process_instead_of_restarting_inside_it(self):
        """前の回の見守り役の流れ（固まった仕事など）が残っていれば、同じ処理の中では動き直さず、処理を終える（動かす所の仕組みが
        立ち上げ直す）。止めるときも、残った流れを待たずに終える（⛔ 2026-10-22 の見直し13：固まった仕事を Python が待ち続け、
        処理が終わらなかった・まだ通話中の流れの回線を後始末して二重に立ち上げえた）。"""
        from salescall.supervisor import __main__ as entry
        release = threading.Event()
        stuck = threading.Thread(target=release.wait, name="salescall-job_0", daemon=True)
        stuck.start()
        try:
            self.assertIn("salescall-job_0", entry.left_over())
            with mock.patch.object(entry.main, "serve", side_effect=RuntimeError("試験")), \
                    mock.patch.object(entry.config, "load", lambda: "試験の設定"), \
                    mock.patch.object(entry.os, "_exit", side_effect=SystemExit) as ended, \
                    mock.patch.object(entry.time, "sleep") as slept, mock.patch("builtins.print"):
                with self.assertRaises(SystemExit):
                    entry.run(["run"])
            ended.assert_called_once_with(1)
            slept.assert_not_called()                                           # 同じ処理の中では動き直さない
            with mock.patch.object(entry.main, "serve", lambda cfg, stop=None: None), \
                    mock.patch.object(entry.config, "load", lambda: "試験の設定"), \
                    mock.patch.object(entry.os, "_exit", side_effect=SystemExit) as ended:
                with self.assertRaises(SystemExit):
                    entry.run(["run"])                                          # 止めたあとも、残った流れを待たない
            ended.assert_called_once_with(0)
        finally:
            release.set()
            stuck.join()
        self.assertNotIn("salescall-job_0", entry.left_over())
        self.assertNotIn("salescall-worker-0", entry.left_over())   # データベースのつなぎの流れは数えない


class Drain(SupervisorBase):
    """入れ替えの前の drain（新しい電話をかけず、いまの通話が終わったら止まる。⛔ 2026-10-22 の見直し15：前は、動かす所の仕組みの
    待つ長さ（Fly.io の kill_timeout）だけに頼っていた）。"""

    def setUp(self):
        super().setUp()
        self.dir = Path(tempfile.mkdtemp(prefix="salescall-drain-"))
        patcher = mock.patch.object(main_mod, "lock_dir", lambda: self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_calls_have_no_length_limit_and_waits_follow_the_live_calls(self):
        """通話の長さの上限は持たない（2026-10-24 ユーザー：時間で業務を打ち切らない）。止めるとき・通行証を止めたときに待つかは、話して
        いる通話の1か所（supervisor.call_live。固まった通話は回線ごとの処理が3秒で終える＝データベースを通さない）。drain の命令は、
        上限なしで待つ。⛔ 0130 の「進んだ時刻」（CALL_STALL＝90秒）は外した。"""
        from salescall import supervisor
        from salescall.supervisor import channel
        self.assertFalse(hasattr(supervisor, "CALL_STALL") or hasattr(supervisor, "CALL_LONGEST"))
        self.assertFalse(hasattr(channel, "REVOKED_WAIT"))
        self.assertIsNone(inspect.signature(main_mod.ask_drain).parameters["wait_seconds"].default)
        readme = (Path(main_mod.__file__).parents[3] / "README.md").read_text(encoding="utf-8")
        self.assertIn(f"`supervisor.main.SHUTDOWN_WAIT`（{int(main_mod.SHUTDOWN_WAIT)}秒）", readme)   # README の値も同じ所から
        line_id, call = uuid.uuid4(), mock.Mock(live=True)
        self.assertFalse(supervisor.call_live(line_id))
        supervisor.call_began(line_id, call)
        self.assertTrue(supervisor.call_live(line_id))
        call.live = False                                                       # おわびして切り終えた通話は「話している」に入らない
        self.assertFalse(supervisor.call_live(line_id))
        supervisor.call_over(line_id, mock.Mock())                              # ほかの通話の口では外さない
        call.live = True
        self.assertTrue(supervisor.call_live(line_id))
        supervisor.call_over(line_id, call)
        self.assertFalse(supervisor.call_live(line_id))

    def test_drain_asks_the_running_supervisor_and_waits_until_it_stops(self):
        flag = self.dir / main_mod.DRAIN_FLAG
        self.assertIsNone(main_mod.ask_drain(wait_seconds=1, every=0.05))       # 動いていない：印は置かない
        self.assertFalse(flag.exists())
        since = time.time()
        got = []
        with main_mod.SingleInstance(self.dir / main_mod.LOCK_FILE):           # 見守り役が動いている
            asker = threading.Thread(target=lambda: got.append(main_mod.ask_drain(wait_seconds=10, every=0.05)))
            asker.start()
            deadline = time.monotonic() + 5
            while not flag.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            main_mod.drained("前の回の番号")   # 前の回の見守り役が遅れて書いた「止まった」の印：止まったとは受け取らない（見直し16b）
            time.sleep(0.2)
            self.assertEqual(got, [])
            token = main_mod.drain_asked(since)
            self.assertTrue(token)                                              # 見守り役が印を見た（見たら消す）
            self.assertFalse(flag.exists())
            time.sleep(0.2)
            self.assertEqual(got, [])                                           # 止まるまで待つ
        # 印のファイルを放したあと、止まり終えた印が書かれるまでのあいだも、命令は印のファイルを押さえない（その一瞬に動き出した
        # 新しい見守り役が「もう動いている」と思って終わらない。⛔ 見直し16）
        for _ in range(30):
            with main_mod.SingleInstance(self.dir / main_mod.LOCK_FILE):
                pass
            time.sleep(0.01)
        self.assertEqual(got, [])
        main_mod.drained(token)                                                # 見守り役が止まり終えた
        asker.join(5)
        self.assertEqual(got, [True])                                          # 止まった
        flag.write_text(repr(since - 60))                                      # 動き出す前の印（前の回の残り）：止まらない
        self.assertFalse(main_mod.drain_asked(since))
        self.assertFalse(flag.exists())
        self.assertFalse(main_mod.drain_asked(since))                          # 印が無い

    def test_the_supervisor_stops_like_ctrl_c_when_drain_is_asked(self):
        cfg = config.load({"SALESCALL_DSN": self.w.a._conninfo, "SALESCALL_LISTEN": "0.0.0.0:0"})
        stop, ended = threading.Event(), threading.Event()
        shut = []
        real = main_mod.Supervisor.shutdown

        def shutdown(sup, *a, **k):
            shut.append(stop.is_set())
            return real(sup, *a, **k)
        with mock.patch.object(main_mod.Supervisor, "shutdown", shutdown), mock.patch("builtins.print"):
            runner = threading.Thread(target=lambda: (main_mod.serve(cfg, stop), ended.set()), daemon=True)
            runner.start()
            deadline = time.monotonic() + 10
            while not (self.dir / main_mod.LOCK_FILE).exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            time.sleep(0.5)
            self.assertEqual(main_mod.ask_drain(wait_seconds=60, every=0.1), True)
            runner.join(10)
        self.assertTrue(ended.is_set())
        self.assertEqual(shut, [True])                                          # 止める印を立ててから、ほかの止め方と同じ shutdown へ


if __name__ == "__main__":
    unittest.main()


class Watchdog(unittest.TestCase):
    """見守り役の本体が固まったら、自分で気づいて処理を終える（動かす所の仕組みが立ち上げ直す。2026-10-22 ユーザー承認）。"""

    def test_a_body_that_stops_turning_is_noticed_once_and_not_while_stopping(self):
        from salescall.supervisor import watchdog
        dog = watchdog.Watchdog(lambda quiet: None, limit=100.0)
        now = time.monotonic()
        self.assertIsNone(dog.stalled(now + 99))                       # 限度まではまだ
        self.assertGreaterEqual(dog.stalled(now + 101), 100)
        dog.beat()
        self.assertIsNone(dog.stalled(time.monotonic() + 50))           # まわれば数え直す
        dog.stop()
        self.assertIsNone(dog.stalled(time.monotonic() + 10_000))       # 止めるあいだは見張らない
        seen = []
        with mock.patch.object(watchdog, "CHECK_EVERY", 0.02):
            dog = watchdog.Watchdog(seen.append, limit=0.05)
            dog.start()
            deadline = time.monotonic() + 5
            while not seen and time.monotonic() < deadline:
                time.sleep(0.02)
            time.sleep(0.1)
        self.assertEqual(len(seen), 1)                                   # 1回だけ（その始末で処理が終わる）

    def test_the_end_tries_to_record_without_waiting_long_and_ends_the_process(self):
        from salescall.supervisor import watchdog
        recorded, ended = [], []
        with mock.patch.object(watchdog.os, "_exit", ended.append):
            watchdog.end_process(lambda: recorded.append(True))(700.0)
            watchdog.end_process(lambda: time.sleep(60))(700.0)          # 記録が固まっても、待たずに終える
        self.assertEqual((recorded, ended), ([True], [1, 1]))

    def test_serve_beats_the_watchdog_and_stops_watching_when_stopping(self):
        """本体の1回ごとに beat し、止めるとき（いまの通話の終わりを待つ前）に見張りをやめる。"""
        src = Path(main_mod.__file__).read_text(encoding="utf-8")
        loop = src[src.index("while not stop.is_set():", src.index("dog.start()")):]
        self.assertTrue(loop.split("\n")[1].strip() == "dog.beat()")
        self.assertLess(src.index("dog.stop()"), src.index("sup.shutdown()", src.index("dog.stop()")))
