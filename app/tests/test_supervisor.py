"""中央の見守り役の試験（DESIGN.md 5-4・5-5。supervisor/・core/leases.py・core/scheduler.py・表の作り 0063）。PC の部品とのつなぎは
tests/test_link.py。"""

from __future__ import annotations

import tempfile
import inspect
import threading
import time
import unittest
import uuid
from unittest import mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

from salescall.core import (attempts, backup, business_rules, calling_hours, conversation_io, errors, fix_requests, gate, leases,
                            next_action, quality_stage, registry, ring_test, scheduler, settings, stops, watch)
from salescall.core.phone import normalize
from salescall.core.access import Rejected
from salescall import supervisor
from salescall.supervisor import config, jobs, line, updates
from salescall.supervisor import main as main_mod
from salescall.supervisor.links import Attached, NoLinks
from salescall.supervisor.main import Supervisor
from tests import support
from tests.support import ADMIN, CLEAN, OPERATOR, SYSTEM, fictional_jp_fixed, rules_raw
from tests.test_gate import HOLDER, Base, FakeDriver
from tests.test_settle import rep


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
            self.assertIn("途切れました", str(reported.call_args_list[0].args[3]))

    def test_the_watch_covers_only_the_call_in_progress_lives_until_the_apology_ends_and_never_waits_on_writes(self):
        """通話ごとの口（line._Call の1か所。0136。⛔ 見直し19a）：(1) お客様が切って試行が終わったあとの片付けで知らせが途切れても、
        おわびせず、こちらのせいの印も付けない (2) かける前から「話している」（発信の出口がかけている途中も）。おわびして切っている
        あいだも「話している」（止める処理がおわびの途中で切らない）。切り終えたら外れる (3) 通話の途中の書き込みは通話ごとの書き手：
        データベースが2秒止まっても、通話を進める流れは待たずに知らせを続け、元気な通話を切らない。戻ったら書き終えてから締める。"""
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
                call.write(lambda tx: tx.execute("SELECT pg_sleep(2)"))     # データベースが2秒止まる

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
                for bad in ("frozen", "rude"):
                    with self.assertRaises(ValueError):
                        call.end_for_trouble(bad)
                try:                                                          # データベースも、決まった4つのほかは断る
                    access.in_client(actor, tenant_id, client_id, lambda tx: tx.execute(
                        "UPDATE salescall.attempts SET state = 'technical_error', trouble = 'rude' WHERE attempt_id = %s",
                        (claim.attempt_id,)))
                except Rejected as e:
                    seen["rude"] = e.constraint
                call.end_for_trouble("poor_audio")
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
        self.assertFalse(self.tx(s, lambda tx: attempts.end_for_trouble(tx, got.called, "poor_audio")))

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
        アプリの言い分では決めず、同じ回線の前後の電話の証拠で決める（salescall.unrung_blame＝0129）：1本だけでは、まだ分からない
        ＝回線を休ませず、回線の異常にも数えない。鳴った電話を挟まずに違う番号も鳴らなければ回線のせい＝前の分もまとめて回線の異常
        として数え、回線を休ませる（一時中止）。⛔ 0129：前は「回線のせいか分からない」をいつも回線の異常に数えて休み、試行は取り消した
        （1つの番号だけがかけられないと、回線が止まり続けた＝見直し18a の問題2）。スマホがまだ通話中なら休む（数えない）。"""
        s = self.ready()
        other = self.another_row(s)
        fail = FakeDriver(line.dial_exit.CallNotPlaced("試験：かけられない"))
        got = line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, fail, Runner())
        self.assertEqual((got.called, got.pause, got.free), (None, None, True))
        self.assertEqual(self.faults(s), [])
        first = self.tx(s, lambda tx: tx.all("SELECT attempt_id, state::text AS st, salescall.dial_kind(state, cut_off, attempt_id)"
                                             " AS k FROM salescall.attempts WHERE line_id = %s", (s["line"],)))
        self.assertEqual([(r["st"], r["k"]) for r in first], [("call_failed", "not_placed")])
        jobs.settle_ready(self.ctx(s))
        got = line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, fail, Runner())   # 別の番号も鳴らない
        self.assertEqual((got.called, got.pause), (None, line.UNRUNG_PAUSE))
        self.assertEqual(self.faults(s), ["line", "line"])                     # 前の1本も、まとめて回線のせい
        line._unrung(self.w.a, SYSTEM, s["tenant"], s["line"])               # 何度読んでも二重に数えない
        self.assertEqual(self.faults(s), ["line", "line"])
        self.assertEqual({self.tx(s, lambda tx: attempts.blame(tx, r["attempt_id"])) for r in first}, {"line"})
        del other
        t = self.ready()
        got = line.one_round(self.w.a, SYSTEM, t["tenant"], t["line"], HOLDER, FakeDriver(busy=True), Runner())
        self.assertEqual((got.called, got.pause), (None, "line_in_call"))

    def test_a_call_that_ends_before_the_answer_on_the_line_side_counts_as_a_line_fault_once_it_is_the_lines(self):
        """お客様が出る前に終わった通話（結果不明。どの回数にも数えない）も同じ1か所：回線のせいと分かれば（鳴った電話を挟まずに
        違う番号も鳴らなかった）、回線の異常として数え（続けば、その回線を止めて知らせる＝数えない電話の歯止め。2026-10-22 深夜
        ユーザー「回線の異常が続いたら回線を止める仕組み：OK」）、回線を休ませる。1本だけ（まだ分からない）・ほかの番号が鳴った
        （番号のせい）ときは数えない。お客様が出たあとの結果不明・ふつうに終わった通話も数えない。"""
        class Before(Runner):
            def run(self, access, actor, claim, *, tenant_id, client_id, call=None):
                access.in_client(actor, tenant_id, client_id, lambda tx: (attempts.move(tx, claim.attempt_id, "ringing"),
                                                                          attempts.move(tx, claim.attempt_id, "technical_error")))
        s = self.ready()
        self.another_row(s)
        got = line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, FakeDriver(), Before())
        self.assertEqual((got.called is not None, got.pause), (True, None))
        self.assertEqual(self.faults(s), [])
        jobs.settle_ready(self.ctx(s))
        got = line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, FakeDriver(), Before())
        self.assertEqual((got.called is not None, got.pause), (True, line.UNRUNG_PAUSE))
        self.assertEqual(self.faults(s), ["line", "line"])
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
        for i in range(need):       # お客様が出る前に終わる電話が、違う番号で続く：回線のせい
            fresh()
            got = line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, FakeDriver(), Runner(unanswered=True))
            self.assertEqual(self.state(s, got.called), "technical_error")
            if i == 0:              # 前のこちらのせいの電話（違う番号）は、続きの違う番号に数えない：まだ分からない
                self.assertEqual(self.tx(s, lambda tx: attempts.blame(tx, got.called)), "pending")
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

    def test_a_failure_in_the_call_ends_only_that_call_and_asks_cl2(self):
        s = self.ready()
        driver = FakeDriver()
        got = line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, driver, Runner(RuntimeError("試験")))
        self.assertEqual(self.state(s, got.called), "technical_error")
        self.assertTrue(self.asked_supervisor(s))
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
    """お客様のところで鳴らなかった電話が、番号のせいか回線のせいか（salescall.unrung_blame の1か所＝0129）を、回線ごとの処理と
    締めを通して確かめる。2026-10-24 ユーザー：「明らかに番号に問題がある場合は、その行は取りやめですが、回線の問題が明らかな場合は
    行は取りやめでなく一時中止だけですよ。」（見直し18a の問題1〜3 の場面）"""

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
        """回線ごとの処理を rounds 回まわす（毎回、締めの仕事も）。resume＝回線が止まったら、確かめが通って自動で解けたことにする。"""
        got, stopped = [], 0
        for _ in range(rounds):
            r = line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, driver, runner)
            got.append(r)
            jobs.settle_ready(self.ctx(s))
            open_ = self.line_stops(s)
            if open_:
                stopped += 1
                if not resume:
                    break
                self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: [stops.resume(tx, e["event_id"], reason="試験：確かめが通った")
                                                                    for e in open_])
        return got, stopped

    def test_a_broken_line_that_comes_back_on_its_own_loses_no_row(self):
        """問題1：どの電話もお客様が出る前に終わるほど壊れた回線で、回線の停止が自動で解けても、行は1つも「番号が使えない」に
        ならない（回線のせいの電話は番号に数えない）。回線は回線の異常で止まり（知らせ）、解けても、また止まる。"""
        s = self.ready()
        rows = self.rows(s, 3)
        got, stopped = self.drive(s, FakeDriver(), self.Before(), 12, resume=True)
        self.assertGreaterEqual(stopped, 2)
        dials = self.tx(s, lambda tx: [r["attempt_id"] for r in tx.all(
            "SELECT attempt_id FROM salescall.attempts WHERE line_id = %s AND salescall.is_dialed(state) ORDER BY claimed_at",
            (s["line"],))])
        self.assertGreaterEqual(len(dials), 8)
        self.assertEqual({self.tx(s, lambda tx: attempts.blame(tx, a)) for a in dials[1:]}, {"line"})
        self.no_row_lost(s, rows)
        notes = self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: tx.one(
            "SELECT count(*) AS n FROM salescall.notification_events WHERE kind = 'stopped'"))["n"]
        self.assertGreater(notes, 0)                                           # 回線の異常は知らせる

    def test_one_number_that_cannot_be_placed_does_not_starve_the_line(self):
        """問題2：ある番号だけ、電話の部品がかけられない（ほかの番号はかかる）：その番号のせい（回線がほかの番号を鳴らせた）。回線は
        休まず、回線の異常にも数えず、ほかの行へかけ続ける（その番号の行は決まった順で、あとへ）。⛔ 見直し18a の問題2：前は回線の
        異常として数えて休み、その行が候補の先頭に残り続け、1日でほかの行へ0本しかかけられなかった。"""
        s = self.ready()
        rows = self.rows(s, 3)
        bad = self.tx(s, lambda tx: tx.one("SELECT phone_e164 FROM salescall.contacts WHERE contact_id = %s",
                                           (s["contact"],))["phone_e164"])
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
        """問題3：どの番号もかけられない回線（アプリの設定の誤りなど）：回線のせい。行はどれも取りやめにならず、回線の異常として
        数えて止まり、知らせる。⛔ 見直し18a の問題3：前は番号のせいとして数え、4日で行がすべて終わり、だれにも知らされなかった。"""
        s = self.ready()
        rows = self.rows(s, 4)
        need = self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: settings.resolve(
            tx, "stop.line.stop_after_failures", tenant_id=s["tenant"], client_id=None, business_id=None))
        got, stopped = self.drive(s, FakeDriver(line.dial_exit.CallNotPlaced("試験：かけられない")), Runner(), 6)
        self.assertEqual(stopped, 1)
        self.assertEqual(len(got), need)                                       # 鳴らない電話が need 本続いたところで止まる
        self.assertEqual([r["cause"] for r in self.line_stops(s)], ["line_failure"])
        self.assertEqual(self.faults(s), ["line"] * need)
        self.no_row_lost(s, rows)
        for r in rows:
            self.assertEqual(self.tx(s, lambda tx: attempts.failed_run(tx, r["contact"]))["n"], 0)


    def fail_until_unconfirmed(self, s, driver):
        """その行だけを、決まった順の数（並びの数）だけ、かけて失敗させる（毎回、予定をいまに書き直す）。"""
        need = len(self.tx(s, lambda tx: attempts.failed_retry_days(tx, tenant_id=s["tenant"], client_id=s["client"],
                                                                    business_id=s["business"])))
        for _ in range(need):
            self.tx(s, lambda tx: next_action.schedule_now(tx, s["task"], cause="試験：その時刻が来た"))
            line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, driver, Runner())
            jobs.settle_ready(self.ctx(s))
        self.tx(s, lambda tx: next_action.schedule_now(tx, s["task"], cause="試験：その時刻が来た"))
        return need

    def closures(self, s):
        return self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: [(r["reason"], r["verdict"]) for r in tx.all(
            "SELECT reason, verdict FROM salescall.line_run_closures WHERE line_id = %s ORDER BY closure_id", (s["line"],))])

    def test_the_last_row_on_the_line_does_not_wait_forever(self):
        """一覧の最後の1件（その回線にかける行が、ほかに残っていない）で、番号か回線か分からないまま止まり続けない（2026-10-24 ユーザー：
        「一つの行で止まり続けることがないようにしてください」）。その回線が一度も鳴らせていなければ回線のせい：番号に数えず（行は続く）、
        回線の異常として数える。⛔ 前は、その行を候補に出さないまま、だれにも知らされずに待ち続けた。"""
        s = self.ready()
        bad = self.tx(s, lambda tx: tx.one("SELECT phone_e164 FROM salescall.contacts WHERE contact_id = %s",
                                           (s["contact"],))["phone_e164"])
        need = self.fail_until_unconfirmed(s, self.Refuses(bad))
        self.assertEqual(self.closures(s), [])
        self.assertTrue(self.tx(s, lambda tx: attempts.number_unconfirmed(tx, s["contact"], tenant_id=s["tenant"],
                                                                         client_id=s["client"], business_id=s["business"])))
        line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, self.Refuses(bad), Runner())   # かける行が無い回
        self.assertEqual(self.closures(s), [("no_rows", "line")])
        self.assertEqual(self.tx(s, lambda tx: attempts.failed_run(tx, s["contact"]))["n"], 0)   # 番号に数えない
        self.assertEqual(self.faults(s), ["line"] * need)                    # 回線の異常として数える（知らせ）
        self.assertEqual(self.tx(s, lambda tx: next_action.current(tx, s["task"]))["kind"], "call")   # 行は続く
        line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, self.Refuses(bad), Runner())
        self.assertEqual(len(self.closures(s)), 1)                           # 閉じるのは、まだ分からない続きがあるときだけ

    def test_a_line_that_rang_before_closes_the_last_rows_run_as_the_numbers(self):
        """その回線が、続きの前にほかの番号を鳴らせていた（直前まで元気だった）なら、かける行が残っていない回に閉じた続きは番号のせい：
        並びの数だけ続いた番号は取りやめになる。"""
        s = self.ready()
        other = self.rows(s, 2)[1]
        line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, FakeDriver(), Runner())   # 先頭の行（s）が鳴った
        jobs.settle_ready(self.ctx(s))
        first = self.tx(s, lambda tx: tx.one("SELECT contact_id FROM salescall.attempts WHERE line_id = %s"
                                             " ORDER BY claimed_at LIMIT 1", (s["line"],)))["contact_id"]
        t = s if first == other["contact"] else other                        # まだかけていない行を、壊れた番号の行にする
        rang = other if t is s else s
        self.tx(s, lambda tx: next_action._append(tx, rang["task"], call_at=None, end_reason="completed", cause="試験：終えた"))
        bad = self.tx(s, lambda tx: tx.one("SELECT phone_e164 FROM salescall.contacts WHERE contact_id = %s",
                                           (t["contact"],))["phone_e164"])
        self.fail_until_unconfirmed(t, self.Refuses(bad))
        line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, self.Refuses(bad), Runner())
        self.assertEqual(self.closures(s), [("no_rows", "number")])
        self.assertTrue(self.tx(t, lambda tx: attempts.number_unusable(tx, t["contact"], tenant_id=t["tenant"],
                                                                      client_id=t["client"], business_id=t["business"])))
        self.assertEqual(self.faults(s), [])

    def test_only_a_row_the_line_can_call_soon_keeps_the_run_open(self):
        """まだ分からない続きを待つのは、その回線がまもなく（line.decide_horizon。初め30分）かけられる行があるときだけ（その電話の結果で
        決まる）。明日かける行・同じ番号の行は待つ理由にならない：テスト番号（無ければ前の区切り）で決めにいく（0136。⛔ 見直し19a：
        前は夜や翌週にかける行が1つでも残っていれば決めず、その行が止まり続けた）。"""
        s = self.ready()
        later = self.rows(s, 2)[1]
        bad = self.tx(s, lambda tx: tx.one("SELECT phone_e164 FROM salescall.contacts WHERE contact_id = %s",
                                           (s["contact"],))["phone_e164"])

        def at(row, when):
            t = self.tx(s, lambda tx: tx.one(f"SELECT now() + interval '{when}' AS t")["t"])
            self.tx(s, lambda tx: support.schedule_call(tx, row["task"], t, contact_id=row["contact"],
                                                        cause="試験：その時刻にかける", promised=False))
        at(later, "1 day")
        self.tx(s, lambda tx: next_action.schedule_now(tx, s["task"], cause="試験"))
        got = line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, self.Refuses(bad), Runner())   # 1回目が鳴らない
        jobs.settle_ready(self.ctx(s))
        self.assertEqual(self.tx(s, lambda tx: attempts.blame(tx, got.called or tx.one(
            "SELECT attempt_id FROM salescall.attempts WHERE line_id = %s", (s["line"],))["attempt_id"])), "pending")
        at(s, "10 minutes")                                                    # 同じ番号の行がまもなく：待つ理由にならない
        at(later, "10 minutes")                                                # ほかの行がまもなく：その電話で決まるので待つ
        line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, self.Refuses(bad), Runner())
        self.assertEqual(self.closures(s), [])
        self.assertTrue(self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: attempts.line_run_pending(tx, s["line"])))
        at(later, "1 day")                                                     # 明日の行・同じ番号の行だけ：1回目から決めにいく
        line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, self.Refuses(bad), Runner())
        self.assertEqual(self.closures(s), [("no_rows", "line")])

    def ring_logged(self, s, rows, line_id=None):
        """テストの記録を、時刻をさかのぼって入れる（rows＝(何分前, 結果) の並び。鳴らす間の試験用）。"""
        support.owner_access().in_system(SYSTEM, lambda tx: [tx.execute(
            "INSERT INTO salescall.line_ring_tests (tenant_id, line_id, purpose, rang, tested_at)"
            " VALUES (%s, %s, 'no_rows', %s, now() - make_interval(mins => %s))", (s["tenant"], line_id or s["line"], rang, ago))
            for ago, rang in rows])

    def test_the_test_number_keeps_its_gap_across_lines_and_doubles_while_it_does_not_ring(self):
        """テスト番号を鳴らす間（契約者ごと・回線をまたいで1つ。core/ring_test.wait_seconds の1か所。0136）：前のテストから30分。
        続けて「鳴った」以外なら、その数だけ倍にし、4時間で止める。鳴れば初めに戻る。間の前は鳴らさない（記録もしない）。
        ⛔ 見直し19a：前は止めた回線の確かめのたびに（1分ごとに）、契約者自身の電話を鳴らしていた。"""
        s = self.ready()
        phone = self.numbered(s)
        for key, value in (("line.test_ring_gap", 1800), ("line.test_ring_gap_max", 14400)):
            self.w.a.in_tenant(OPERATOR, s["tenant"], lambda tx, key=key, value=value: settings.set_value(
                tx, key, value, scope="tenant", tenant_id=s["tenant"], status="decided", rationale="試験", review_when="試験"))
        wait = lambda: self.w.a.in_tenant(SYSTEM, s["tenant"], ring_test.wait_seconds)   # noqa: E731
        self.assertEqual(wait(), 0)
        other = self.w.line(s["tenant"])
        self.ring_logged(s, [(10, True)], other)                               # ほかの回線で10分前に鳴った
        self.assertAlmostEqual(wait(), 20 * 60, delta=30)
        d = FakeDriver(ring=True)
        self.assertEqual(ring_test.run(self.w.a, SYSTEM, s["tenant"], s["line"], d, "no_rows"), ring_test.Result.WAIT)
        self.assertEqual((d.rings, self.ring_tests_of(s)), ([], []))          # 鳴らさず、記録もしない
        self.ring_logged(s, [(9, None)])                                       # 鳴った以外が1回：1時間
        self.assertAlmostEqual(wait(), 51 * 60, delta=30)
        self.ring_logged(s, [(8, False), (7, None)])                           # 3回：4時間（1800×8＝4時間で止まる）
        self.assertAlmostEqual(wait(), 4 * 3600 - 7 * 60, delta=30)
        self.ring_logged(s, [(6, False)])                                      # 4回：4時間のまま
        self.assertAlmostEqual(wait(), 4 * 3600 - 6 * 60, delta=30)
        self.ring_logged(s, [(5, True)], other)                                # 鳴った：初めの30分に戻る
        self.assertAlmostEqual(wait(), 25 * 60, delta=30)
        self.assertEqual(phone, self.w.a.in_tenant(SYSTEM, s["tenant"], ring_test.number))

    def test_a_test_number_that_keeps_answering_unknown_falls_back_and_tells_once(self):
        """テスト番号の結果は3つ（鳴った・鳴らなかった＝回線のせい・分からない＝話し中・電源が切れている・圏外・見分けられない）。
        分からないが続けて3回（line.test_ring_unknown_limit）になったら、テスト番号で決めるのをやめ（前の区切りで決める・止めた回線は
        アプリの確かめ）、契約者と管理者に1回知らせる。鳴った・鳴らなかったが出れば元に戻る（0136。⛔ 見直し19a：前はテスト番号の電話の
        都合で決まらないまま、試し続けた）。"""
        s = self.ready()
        self.numbered(s)
        bad = self.tx(s, lambda tx: tx.one("SELECT phone_e164 FROM salescall.contacts WHERE contact_id = %s",
                                           (s["contact"],))["phone_e164"])
        self.fail_until_unconfirmed(s, self.Refuses(bad))
        usable = lambda: self.w.a.in_tenant(SYSTEM, s["tenant"], ring_test.usable)   # noqa: E731
        told = lambda: self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: tx.one(   # noqa: E731
            "SELECT count(*) AS n FROM salescall.notification_events WHERE kind = 'test_ring_unknown'"))["n"]
        unknown = FakeDriver(ring=None)
        for _ in range(2):
            self.assertEqual(line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, unknown, Runner()).pause,
                             line.RING_TEST_PAUSE)                             # 決まらない：休んで試し直す
            self.next_ring()
        self.assertEqual((self.closures(s), usable(), told()), ([], True, 0))
        line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, unknown, Runner())   # 3回目：前の区切りで決める
        self.assertEqual(len(self.closures(s)), 1)
        self.assertEqual((usable(), told()), (False, 1))
        admin = self.w.a.in_system(ADMIN, lambda tx: tx.all(
            "SELECT 1 FROM salescall.notification_events WHERE kind = 'test_ring_unknown' AND origin_tenant = %s", (s["tenant"],)))
        self.assertEqual(len(admin), 1)                                        # 管理者にも写し
        stop = {"cause": "line_failure", "line_id": s["line"]}
        self.assertFalse(self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: ring_test.checks_stop(tx, stop)))   # アプリの確かめ
        self.next_ring()
        ring_test.run(self.w.a, SYSTEM, s["tenant"], s["line"], unknown, "resume_check")
        self.assertEqual(told(), 1)                                            # 続きごとに1回
        self.next_ring()
        ring_test.run(self.w.a, SYSTEM, s["tenant"], s["line"], FakeDriver(ring=True), "resume_check")
        self.assertTrue(usable())                                              # 鳴った：元に戻る
        self.assertTrue(self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: ring_test.checks_stop(tx, stop)))

    def test_a_run_left_undecided_too_long_is_told_once(self):
        """まだ分からない続きが、テスト番号を鳴らしてよい時間で数えて line.pending_alert_after（初め1日）を過ぎても決まらなければ、
        契約者と管理者に1回知らせる（`line_undecided`。0136。だれにも知られずに行が待ち続けない）。"""
        s = self.ready()
        self.numbered(s)
        self.w.a.in_tenant(OPERATOR, s["tenant"], lambda tx: settings.set_value(
            tx, "line.pending_alert_after", 1, scope="tenant", tenant_id=s["tenant"], status="decided", rationale="試験",
            review_when="試験"))
        bad = self.tx(s, lambda tx: tx.one("SELECT phone_e164 FROM salescall.contacts WHERE contact_id = %s",
                                           (s["contact"],))["phone_e164"])
        self.fail_until_unconfirmed(s, self.Refuses(bad))
        told = lambda: self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: tx.one(   # noqa: E731
            "SELECT count(*) AS n FROM salescall.notification_events WHERE kind = 'line_undecided'"))["n"]
        busy = FakeDriver(busy=True)                                           # 回線が話している：テストが決まらない
        time.sleep(1.1)
        for _ in range(2):
            line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, busy, Runner())
        self.assertEqual(told(), 1)
        self.assertEqual(self.closures(s), [])

    def test_a_run_is_not_closed_while_the_line_has_a_call_in_progress(self):
        """回線に進行中の電話があるあいだは、まだ分からない続きを閉じない（その電話の結果で決まる。データベースの1か所
        salescall.close_unrung_run。使わなくした回線は、待たずに閉じる）。"""
        s = self.ready()
        bad = self.tx(s, lambda tx: tx.one("SELECT phone_e164 FROM salescall.contacts WHERE contact_id = %s",
                                           (s["contact"],))["phone_e164"])
        self.fail_until_unconfirmed(s, self.Refuses(bad))
        other = self.rows(s, 2)[1]
        c = self.claim(s, task=other["task"])
        self.assertIsInstance(c, gate.Claim, getattr(c, "reason", None))
        line.dial_exit.dial(self.w.a, SYSTEM, s["tenant"], s["client"], c.attempt_id, FakeDriver())   # かけている途中
        self.assertFalse(self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: attempts.close_unrung_run(tx, s["line"], "no_rows")))
        self.assertEqual(self.closures(s), [])
        self.w.a.in_tenant(OPERATOR, s["tenant"], lambda tx: registry.retire_line(tx, s["line"], reason="試験：端末を返した"))
        self.assertEqual(self.closures(s), [("retired", "line")])

    def test_calls_from_before_the_evidence_rule_are_closed_as_undecided_and_counted_nowhere(self):
        """前の版のデータ（0136 の移し替え）：0129 より前の、鳴らなかった電話は、だれのせいかの証拠を持たないので、移し替えのときに回線
        ごとに区切りを1つ置き（理由 migration・決め方 none＝決めない）、番号にも回線にも数えない。区切りのあとの電話から、いまの決まりで
        決める（前の区切りで決めるときも、この区切りの決め方は引き継がない）。前の版の予定の読み替え（番号を伺い直す理由が無い予定）は
        データベースの1か所 salescall.number_again_of。"""
        s = self.ready()
        bad = self.tx(s, lambda tx: tx.one("SELECT phone_e164 FROM salescall.contacts WHERE contact_id = %s",
                                           (s["contact"],))["phone_e164"])
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
        with self.assertRaises(Exception):                                     # 決めない区切りは移し替えだけ
            support.owner_access().in_system(SYSTEM, lambda tx: tx.execute(
                "INSERT INTO salescall.line_run_closures (tenant_id, line_id, reason, verdict) VALUES (%s, %s, 'no_rows', 'none')",
                (s["tenant"], s["line"])))
        other = self.rows(s, 2)[1]
        worse = self.tx(s, lambda tx: tx.one("SELECT phone_e164 FROM salescall.contacts WHERE contact_id = %s",
                                             (other["contact"],))["phone_e164"])
        line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, self.Refuses(worse), Runner())   # 区切りのあと
        self.assertTrue(self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: attempts.line_run_pending(tx, s["line"])))
        self.assertTrue(self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: attempts.close_unrung_run(tx, s["line"], "no_rows")))
        self.assertEqual(self.closures(s)[-1], ("no_rows", "line"))           # 区切りの決め方（none）は引き継がない
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
        無い：待たずに決めにいく（0136。⛔ 見直し19a：前は、その回線が翌朝までかけられないのに、残っている行を待ち続けた）。"""
        s = self.ready()
        other = self.rows(s, 2)[1]
        tomorrow = self.tx(s, lambda tx: tx.one("SELECT now() + interval '1 day' AS t")["t"])
        self.tx(s, lambda tx: support.schedule_call(tx, other["task"], tomorrow, contact_id=other["contact"],
                                                    cause="試験：明日かける", promised=False))
        bad = self.tx(s, lambda tx: tx.one("SELECT phone_e164 FROM salescall.contacts WHERE contact_id = %s",
                                           (s["contact"],))["phone_e164"])
        self.tx(s, lambda tx: next_action.schedule_now(tx, s["task"], cause="試験"))
        line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, self.Refuses(bad), Runner())   # 1回目が鳴らない
        jobs.settle_ready(self.ctx(s))
        self.tx(s, lambda tx: next_action.schedule_now(tx, other["task"], cause="試験：いまかけられる"))
        with mock.patch.object(gate, "claim", lambda tx, **kw: gate.denied("line_daily_limit")):
            got = line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, self.Refuses(bad), Runner())
        self.assertEqual(got.pause, "line_daily_limit")
        self.assertEqual(self.closures(s), [("no_rows", "line")])

    def test_retiring_the_line_closes_its_run_as_the_lines(self):
        """回線を使わなくしたら、その回線のまだ分からない続きは回線のせい（番号に数えず、ほかの回線でかけ直して確かめる）。"""
        s = self.ready()
        bad = self.tx(s, lambda tx: tx.one("SELECT phone_e164 FROM salescall.contacts WHERE contact_id = %s",
                                           (s["contact"],))["phone_e164"])
        self.fail_until_unconfirmed(s, self.Refuses(bad))
        self.w.a.in_tenant(OPERATOR, s["tenant"], lambda tx: registry.retire_line(tx, s["line"], reason="試験：端末を返した"))
        self.assertEqual(self.closures(s), [("retired", "line")])
        self.assertEqual(self.tx(s, lambda tx: attempts.failed_run(tx, s["contact"]))["n"], 0)
        self.assertFalse(self.tx(s, lambda tx: attempts.number_unconfirmed(tx, s["contact"], tenant_id=s["tenant"],
                                                                          client_id=s["client"], business_id=s["business"])))


    # 2026-10-24 ユーザー決定：「その回線にかける行が残っていないときは、『テスト番号』で決めます。前に鳴った電話があれば番号のせい、
    # 一度も鳴らせていなければ回線のせいです。」「回線が止まったあとの再開の確かめ：OK」（core/ring_test の1か所。0134）
    def numbered(self, s):
        """その契約者にテスト番号を入れる（架空の番号。返すのは、その番号）。鳴らしてよい時間は 0:00〜24:00（この仕事専用の電話の形＝
        時刻しだいで試験が変わらない。時間の外の試験は `ring_hours` で決める）。鳴らす間は1秒（続けて試すときは `next_ring` で待つ）。"""
        phone = normalize(fictional_jp_fixed()).e164
        self.w.a.in_tenant(OPERATOR, s["tenant"], lambda tx: settings.set_value(
            tx, "line.test_number", phone, scope="tenant", tenant_id=s["tenant"], status="decided", rationale="試験",
            review_when="試験"))
        self.set_ring_hours(s, "00:00", "24:00")
        for key in ("line.test_ring_gap", "line.test_ring_gap_max"):   # 鳴らす間は1秒（間そのものの試験は、それぞれで決める）
            self.w.a.in_tenant(OPERATOR, s["tenant"], lambda tx, key=key: settings.set_value(
                tx, key, 1, scope="tenant", tenant_id=s["tenant"], status="decided", rationale="試験", review_when="試験"))
        return phone

    @staticmethod
    def next_ring():
        """テスト番号の鳴らす間（試験では1秒）が過ぎるまで待つ。"""
        time.sleep(1.1)

    def set_ring_hours(self, s, start, end):
        self.w.a.in_tenant(OPERATOR, s["tenant"], lambda tx: settings.set_value(
            tx, "line.test_ring_hours", [start, end], scope="tenant", tenant_id=s["tenant"], status="decided", rationale="試験",
            review_when="試験"))

    def ring_hours(self, s, *, inside):
        """鳴らしてよい時間を、いまを含む2時間（inside）か、いまを含まない1時間（2時間後から）にする（日本時間・時刻しだいで変わらない
        形。日をまたぐ範囲にもなる）。"""
        now = calling_hours.local(datetime.now(timezone.utc))
        start = now - timedelta(hours=1) if inside else now + timedelta(hours=2)
        end = start + timedelta(hours=2 if inside else 1)
        self.set_ring_hours(s, start.strftime("%H:%M"), end.strftime("%H:%M"))

    def bad_number(self, s):
        return self.tx(s, lambda tx: tx.one("SELECT phone_e164 FROM salescall.contacts WHERE contact_id = %s",
                                            (s["contact"],))["phone_e164"])

    def ring_tests_of(self, s):
        return self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: [(r["purpose"], r["rang"]) for r in tx.all(
            "SELECT purpose, rang FROM salescall.line_ring_tests WHERE line_id = %s ORDER BY test_id", (s["line"],))])

    def test_a_test_number_that_rings_makes_the_last_rows_run_the_numbers(self):
        """かける行が残っていない回に、テスト番号が鳴った：回線は元気＝まだ分からなかった続きは番号のせい（鳴ったテストは、鳴った電話と
        同じ区切り＝salescall.line_rings）。続きを閉じる印は作らない。その番号は決まった順の数で取りやめになり、回線の異常には数えない。
        続きが決まったので、次の回は試さない（テスト番号を鳴らし続けない）。"""
        s = self.ready()
        phone = self.numbered(s)
        bad = self.bad_number(s)
        self.fail_until_unconfirmed(s, self.Refuses(bad))
        d = self.Refuses(bad)
        d.ring = True
        got = line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, d, Runner())
        self.assertEqual((got.called, got.pause), (None, None))
        self.assertEqual(d.rings, [phone])                                    # テスト番号へ（お客様の番号ではない）
        self.assertEqual(self.ring_tests_of(s), [("no_rows", True)])
        self.assertEqual(self.closures(s), [])
        self.assertTrue(self.tx(s, lambda tx: attempts.number_unusable(tx, s["contact"], tenant_id=s["tenant"],
                                                                       client_id=s["client"], business_id=s["business"])))
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
        need = self.fail_until_unconfirmed(t, self.Refuses(bad))
        d = self.Refuses(bad)
        d.ring = False
        got = line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, d, Runner())
        self.assertEqual(got.pause, line.UNRUNG_PAUSE)
        self.assertEqual(self.closures(s), [("test_silent", "line")])
        self.assertEqual(self.tx(t, lambda tx: attempts.failed_run(tx, t["contact"]))["n"], 0)   # 番号に数えない
        self.assertFalse(self.tx(t, lambda tx: attempts.number_unusable(tx, t["contact"], tenant_id=t["tenant"],
                                                                        client_id=t["client"], business_id=t["business"])))
        self.assertEqual(self.faults(s), ["line"] * need)
        self.assertEqual(self.tx(t, lambda tx: next_action.current(tx, t["task"]))["kind"], "call")   # 行は続く

    def test_a_test_number_that_decides_nothing_is_tried_again_later(self):
        """電話の部品が答えない（分からない）・回線が話している途中：どちらの証拠にもせず（記録だけ・話している途中は試さない）、休んで
        から試し直す。鳴らなかった記録なしに「テストが鳴らなかった」で閉じられない。テストの記録は変えられない。"""
        s = self.ready()
        phone = self.numbered(s)
        bad = self.bad_number(s)
        self.fail_until_unconfirmed(s, self.Refuses(bad))
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
        部品は使わない）：確かめの間隔ごとに試し、決めた回数続けて鳴れば解く。鳴ったテストは回線が直った証拠（数え直し）。"""
        s = self.ready()
        phone = self.numbered(s)
        for key, value in (("stop.line.probe_interval", 1), ("stop.line.resume_after_successes", 2)):
            self.w.a.in_tenant(OPERATOR, s["tenant"], lambda tx, key=key, value=value: settings.set_value(
                tx, key, value, scope="tenant", tenant_id=s["tenant"], status="decided", rationale="試験", review_when="試験"))
        self.rows(s, 4)
        got, stopped = self.drive(s, FakeDriver(line.dial_exit.CallNotPlaced("試験：かけられない")), Runner(), 6)
        self.assertEqual(stopped, 1)
        self.assertEqual(line.one_round(self.w.a, SYSTEM, s["tenant"], s["line"], HOLDER, FakeDriver(), Runner()).pause,
                         "line_stopped")
        del PROBED[:]
        jobs.stop_checks(jobs.Context(self.w.a, self.cfg(s, SALESCALL_PROBER="tests.test_supervisor:make_prober"), HOLDER,
                                      NoLinks(), s["tenant"]))
        self.assertEqual(PROBED, [])                                          # 確かめの部品は、この停止を確かめない
        unknown = FakeDriver()                                                # 電話の部品が答えない：記録せず、いつもの休み
        self.assertIsNone(line._check_stop(self.w.a, SYSTEM, s["tenant"], s["line"], unknown))   # （すぐ試し直さない）
        self.assertEqual(unknown.rings, [phone])
        self.next_ring()
        d = FakeDriver(ring=True)
        wait = line._check_stop(self.w.a, SYSTEM, s["tenant"], s["line"], d)
        self.assertEqual(d.rings, [phone])
        self.assertTrue(0 < wait <= 1)                                        # 次の確かめまで（回線の処理はそれより長く休まない）
        errors = lambda: self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: tx.one(  # noqa: E731
            "SELECT salescall.consecutive_errors(ARRAY['line'], 'line_failure', 'line_ok', %s) AS n", (s["line"],)))["n"]
        self.assertEqual(errors(), 0)                                         # 鳴ったテストは、回線が直った証拠（数え直し）
        self.assertEqual(line._check_stop(self.w.a, SYSTEM, s["tenant"], s["line"], d) > 0, True)
        self.assertEqual(d.rings, [phone])                                    # 間隔の前は試さない
        self.assertEqual(len(self.line_stops(s)), 1)
        time.sleep(1.1)
        self.assertIsNone(line._check_stop(self.w.a, SYSTEM, s["tenant"], s["line"], d))   # 2回続けて鳴った：解けた
        self.assertEqual(self.line_stops(s), [])
        self.assertEqual(self.ring_tests_of(s), [("resume_check", None)] + [("resume_check", True)] * 2)

    # 2026-10-24 ユーザー：「（テスト番号を夜も鳴らしてよいか）私の電話です。軌道に乗れば、新しい電話をもうひとつ契約予定なのでその後に
    # 考えます。この仕事専用なら夜でもOK」（設定 line.test_ring_hours。鳴らすかは core/ring_test.run の1か所。0135）
    def stopped_line(self, s):
        """テスト番号のある契約者の回線を、回線の異常で止める（確かめの間隔1秒・続けて2回で解く）。止めた停止を返す。"""
        for key, value in (("stop.line.probe_interval", 1), ("stop.line.resume_after_successes", 2)):
            self.w.a.in_tenant(OPERATOR, s["tenant"], lambda tx, key=key, value=value: settings.set_value(
                tx, key, value, scope="tenant", tenant_id=s["tenant"], status="decided", rationale="試験", review_when="試験"))
        self.rows(s, 4)
        got, stopped = self.drive(s, FakeDriver(line.dial_exit.CallNotPlaced("試験：かけられない")), Runner(), 6)
        self.assertEqual(stopped, 1)
        return self.line_stops(s)[0]

    def checks_of(self, stop):
        return support.owner_access().in_system(SYSTEM, lambda tx: tx.one(
            "SELECT count(*) AS n FROM salescall.stop_checks WHERE stop_event_id = %s", (stop["event_id"],)))["n"]

    def test_the_test_ring_hours_are_the_contractors_and_start_at_7_to_22(self):
        """鳴らしてよい時間の初めの値は 7:00〜22:00（いまはユーザー自身の電話＝深夜・早朝の歯止めと同じ）。契約者ごとに変えられ、
        この仕事専用の電話に替えたら 0:00〜24:00（いつでも鳴らしてよい）。"""
        s = self.ready()
        self.assertEqual(self.w.a.in_tenant(SYSTEM, s["tenant"], ring_test.hours), (7 * 3600, 22 * 3600))
        self.set_ring_hours(s, "00:00", "24:00")
        self.assertEqual(self.w.a.in_tenant(SYSTEM, s["tenant"], ring_test.hours), (0, 24 * 3600))
        self.assertTrue(self.w.a.in_tenant(SYSTEM, s["tenant"], ring_test.allowed))   # いつでも（夜も）
        other = self.ready()                                                  # ほかの契約者は初めの値のまま
        self.assertEqual(self.w.a.in_tenant(SYSTEM, other["tenant"], ring_test.hours), (7 * 3600, 22 * 3600))

    def test_the_test_number_is_not_rung_outside_its_hours(self):
        """かける行が残っていない回線の確かめ：鳴らしてよい時間の外では鳴らさず、記録もせず、前の区切りで決めることもしない（時間が
        来るまで休んで試し直す）。時間の内に入れば鳴らして決める。"""
        s = self.ready()
        phone = self.numbered(s)
        self.ring_hours(s, inside=False)
        bad = self.bad_number(s)
        self.fail_until_unconfirmed(s, self.Refuses(bad))
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
        self.assertEqual((d.rings, self.ring_tests_of(s), self.checks_of(stop)), ([], [], 0))
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
                         "system_lasting_stops", "pc_watch", "budget", "holidays"} <= bare)
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
