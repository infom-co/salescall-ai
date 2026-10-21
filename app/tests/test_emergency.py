"""本番の PC の上で押せる緊急停止の試験（DESIGN.md 9-2・5-5。サーバーの側＝core/emergency.py、PC の側＝agent/emergency.py、
発信の出口＝core/dial_exit.py の CallWithheld）。PC からサーバーへ届く流れは tests/test_link.py。"""

from __future__ import annotations

import io
import tempfile
import uuid
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from salescall.agent import emergency as pc
from salescall.core import dial_exit, emergency, stops
from salescall.link.phone import CallWithheld
from tests.support import ADMIN, OPERATOR, SYSTEM
from tests.test_gate import Base, FakeDriver

PC, OTHER_PC = uuid.UUID(int=11), uuid.UUID(int=12)


class OnTheServer(Base):
    def opened(self, s):
        return self.w.a.in_tenant(OPERATOR, s["tenant"], lambda tx: [
            (r["scope"], r["cause"]) for r in stops.open_stops(tx, tenant_id=s["tenant"], client_id=None, business_id=None)])

    def system_stops(self):
        return [r for r in self.w.a.in_system(ADMIN, lambda tx: stops.open_stops(tx, tenant_id=None, client_id=None,
                                                                                    business_id=None))
                if r["cause"] == "manual"]

    def test_a_stop_from_the_pc_stops_only_that_contractor_and_cancels_what_was_not_dialed(self):
        """PC の印がサーバーに届いたら、その契約者の緊急停止（ふつうの停止と同じ。門が止め、まだ発信していない予約を取り消す。
        画面にも出る）。全体・ほかの契約者は止めない。"""
        s, t = self.ready(), self.ready()
        claim = self.claim(s)                                                    # 押す前に取った予約
        event = emergency.stop(self.w.a, s["tenant"], PC, "架空の PC", "試験：画面が開けない")
        self.assertEqual((self.opened(s), self.opened(t), self.system_stops()), ([("tenant", "manual")], [], []))
        driver = FakeDriver()
        self.assertFalse(dial_exit.dial(self.w.a, SYSTEM, s["tenant"], s["client"], claim.attempt_id, driver))
        self.assertEqual((driver.calls, self.reason(s)), ([], "stopped"))
        who = self.w.a.in_tenant(OPERATOR, s["tenant"], lambda tx: tx.one(
            "SELECT actor, evidence FROM salescall.stop_events WHERE event_id = %s", (event,)))
        self.assertEqual((who["actor"], who["evidence"]), ("契約者の PC の緊急停止（架空の PC）", emergency.evidence(PC)))
        with self.assertRaises(ValueError):
            emergency.stop(self.w.a, s["tenant"], PC, "架空の PC", " ")

    def test_a_release_on_the_screen_is_seen_and_a_release_from_the_pc_frees_only_its_own_stops(self):
        """画面から解かれたら `is_open` が偽（PC は印を消す）。PC で解くのは、この道で記録した停止だけ（画面から押した停止は残す）。"""
        s = self.ready()
        event = emergency.stop(self.w.a, s["tenant"], PC, "架空の PC", "試験")
        self.assertTrue(emergency.is_open(self.w.a, s["tenant"], event))
        self.assertTrue(emergency.is_open(self.w.a, s["tenant"], 10 ** 12))      # 見えない停止は、解かれていないとみなす
        self.w.a.in_tenant(OPERATOR, s["tenant"], lambda tx: stops.resume(tx, event, reason="試験：画面から解いた"))
        self.assertFalse(emergency.is_open(self.w.a, s["tenant"], event))
        emergency.stop(self.w.a, s["tenant"], PC, "架空の PC", "試験：また押した")
        screen = self.w.a.in_tenant(OPERATOR, s["tenant"], lambda tx: stops.stop(
            tx, scope="tenant", cause="manual", reason="試験：画面から押した", tenant_id=s["tenant"]))["event_id"]
        self.assertEqual(len(emergency.release(self.w.a, s["tenant"], PC, "架空の PC", "試験：直った")), 1)
        self.assertEqual(self.opened(s), [("tenant", "manual")])
        self.assertTrue(emergency.is_open(self.w.a, s["tenant"], screen))
        with self.assertRaises(ValueError):
            emergency.release(self.w.a, s["tenant"], PC, "架空の PC", " ")

    def test_a_release_from_one_pc_leaves_the_stop_pressed_on_another_pc(self):
        """同じ契約者の2台の PC が押したとき、1台で解いても、もう1台で押した停止は残る（前は、この道で記録した停止をすべて解き、
        もう1台が印を消してかけ始めた）。"""
        s = self.ready()
        mine = emergency.stop(self.w.a, s["tenant"], PC, "架空の PC", "試験：1台目で押した")
        theirs = emergency.stop(self.w.a, s["tenant"], OTHER_PC, "架空の2台目の PC", "試験：2台目で押した")
        self.assertEqual(len(emergency.release(self.w.a, s["tenant"], PC, "架空の PC", "試験：1台目で解いた")), 1)
        self.assertEqual((emergency.is_open(self.w.a, s["tenant"], mine), emergency.is_open(self.w.a, s["tenant"], theirs)),
                         (False, True))

    def test_a_call_withheld_by_the_pc_is_not_dialed_and_not_a_line_fault(self):
        """PC の上で押されているあいだ、PC の部品は「かけて」を断る：試行は「発信に失敗」（声は届いていない）、回線の異常に数えない。"""
        s = self.ready()
        claim = self.claim(s)
        driver = FakeDriver(fail=CallWithheld("試験：PC で押されている"))
        self.assertIs(dial_exit.dial(self.w.a, SYSTEM, s["tenant"], s["client"], claim.attempt_id, driver),
                      dial_exit.Outcome.UNUSABLE)
        self.assertEqual(self.state(s, claim.attempt_id), "call_failed")
        faults = self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: tx.all(
            "SELECT 1 FROM salescall.error_events WHERE source = 'line'"))
        self.assertEqual(faults, [])


class OnThePc(Base):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(pc, "FLAG", Path(tempfile.mkdtemp()) / "EMERGENCY_STOP")
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_cli(self, *argv):
        with redirect_stdout(io.StringIO()) as out:
            code = pc.main(list(argv))
        return code, out.getvalue()

    def test_the_command_places_the_flag_even_without_the_network_and_asks_for_a_release(self):
        """押すのは、ネットが切れていても必ずできる（PC の中の印だけ）。解くのは「解いてほしい」を書くだけ（サーバーで解けたら消える）。"""
        self.assertEqual(self.run_cli()[0], 2)
        self.assertEqual(self.run_cli("release", "試験")[0], 1)                  # 止めていない
        code, out = self.run_cli("stop", "試験：ネットが切れた")
        self.assertEqual(code, 0)
        self.assertEqual(pc.engaged()["reason"], "試験：ネットが切れた")
        self.run_cli("stop", "試験：2回目")
        self.assertEqual((pc.engaged()["reason"], pc.engaged()["event"]), ("試験：2回目", None))   # 押し直しは新しい押し
        self.assertTrue(pc.recorded(7, pc.engaged()["at"]))
        self.assertEqual(self.run_cli("release", "試験：直った")[0], 0)
        self.assertEqual((pc.engaged()["event"], pc.engaged()["release"]), (7, "試験：直った"))   # まだ止めたまま
        self.assertIn("止めています", self.run_cli("status")[1])
        self.assertTrue(pc.clear(7))                                            # サーバーで解けた番号と同じ：印を消す
        self.assertIn("止めていません", self.run_cli("status")[1])
