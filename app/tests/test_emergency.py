"""本番のPCの緊急停止（core/emergency.py。CL-3 の 4-1d の2）の試験。"""

from __future__ import annotations

import io
import os
import tempfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from salescall.core import dial_exit, emergency, stops
from salescall.core.access import Access
from tests.support import ADMIN, OPERATOR
from tests.test_gate import Base, FakeDriver

UNREACHABLE = Access("host=127.0.0.1 port=1 dbname=none user=none connect_timeout=1", max_attempts=1)


class Emergency(Base):
    def setUp(self):
        super().setUp()
        flag = Path(tempfile.mkdtemp()) / "EMERGENCY_STOP"
        os.environ["SALESCALL_EMERGENCY_FLAG"] = str(flag)
        self.addCleanup(os.environ.pop, "SALESCALL_EMERGENCY_FLAG", None)
        self.addCleanup(lambda: emergency.release_all(self.w.a, ADMIN, "試験のあと解く"))

    def system_stops(self):
        return [r for r in self.w.a.in_system(ADMIN, lambda tx: stops.open_stops(tx, tenant_id=None, client_id=None,
                                                                                    business_id=None))
                if r["cause"] == "manual"]

    def test_stop_records_and_the_exit_does_not_dial(self):
        s = self.ready()
        claim = self.claim(s)                                          # 押す前に取った予約
        got = emergency.stop_all(self.w.a, ADMIN, "試験：画面が開けない")
        self.assertIsNotNone(got["db_event"])
        self.assertEqual(len(self.system_stops()), 1)                  # 画面にも出る、ふつうの緊急停止
        driver = FakeDriver()
        self.assertFalse(dial_exit.dial(self.w.a, ADMIN, s["tenant"], s["client"], claim.attempt_id, driver))
        self.assertEqual(driver.calls, [])                             # 出口でかけない
        self.assertEqual(self.reason(s), "stopped")
        done = emergency.release_all(self.w.a, ADMIN, "試験：直った")
        self.assertTrue(done["released"])
        self.assertEqual((emergency.engaged(), self.system_stops()), (None, []))

    def test_a_stop_pressed_while_marking_dialing_still_does_not_dial(self):
        """発信中にする取引のあいだに押された印でも、回線に渡す直前に見て、かけない（2026-10-16 の見直し G5）。"""
        s = self.ready()
        claim = self.claim(s)
        real = dial_exit.attempts.start_dialing

        def start_then_press(tx, attempt_id):
            ok = real(tx, attempt_id)
            emergency._write_flag({"reason": "試験：取引のあいだに押した", "kind": "admin", "db_event": None})
            return ok
        driver = FakeDriver()
        with mock.patch.object(dial_exit.attempts, "start_dialing", start_then_press):
            self.assertFalse(dial_exit.dial(self.w.a, ADMIN, s["tenant"], s["client"], claim.attempt_id, driver))
        self.assertEqual(driver.calls, [])
        self.assertEqual(self.state(s, claim.attempt_id), "call_failed")                  # かけていない（回数に数えない）

    def test_without_the_database_the_flag_still_stops_and_is_kept(self):
        s = self.ready()
        got = emergency.stop_all(UNREACHABLE, ADMIN, "試験：ネットが切れた")
        self.assertIsNone(got["db_event"])
        self.assertIsNotNone(got["db_error"])
        self.assertIsNotNone(emergency.engaged())                     # 印は必ず残る
        claim = self.claim(s)                                          # データベースに停止は無い（届かなかった）
        driver = FakeDriver()
        self.assertFalse(dial_exit.dial(self.w.a, ADMIN, s["tenant"], s["client"], claim.attempt_id, driver))
        self.assertEqual(driver.calls, [])                             # ネットが戻っても、印があればかけない
        self.assertFalse(emergency.release_all(UNREACHABLE, ADMIN, "試験")["released"])
        self.assertIsNotNone(emergency.engaged())                     # 解いたことを記録できなければ、印は消さない

    def test_without_a_connection_setting_the_flag_is_still_written(self):
        got = emergency.stop_all(None, ADMIN, "試験：つなぎ先が無い")
        self.assertIsNotNone(emergency.engaged())
        self.assertIsNone(got["db_event"])
        self.assertFalse(emergency.release_all(None, ADMIN, "試験")["released"])

    def test_reason_is_required(self):
        with self.assertRaises(ValueError):
            emergency.stop_all(self.w.a, ADMIN, " ")

    def test_a_resume_on_the_screen_clears_the_flag(self):
        # 2026-10-16 見直し：前は、画面から解いても印が残り、画面は「止まっていない」のに、このPCは黙ってかけなかった
        s = self.ready()
        got = emergency.stop_all(self.w.a, ADMIN, "試験：画面が開けない")
        self.w.a.in_system(ADMIN, lambda tx: stops.resume(tx, got["db_event"], reason="試験：画面から解いた"))
        self.assertIsNotNone(emergency.engaged())                     # 取引なしでは、印のとおり（止める側）
        driver = FakeDriver()
        claim = self.claim(s)
        self.assertTrue(dial_exit.dial(self.w.a, ADMIN, s["tenant"], s["client"], claim.attempt_id, driver))
        self.assertEqual(len(driver.calls), 1)                         # 解いた記録が正本：印は消え、かける
        self.assertIsNone(emergency.engaged())

    def test_a_contractor_stops_only_their_own_scope(self):
        # 2026-10-16 見直し：前は、誰が押しても管理者として全体を止めていた
        s = self.ready()
        with self.assertRaises(PermissionError):
            emergency._in_scope(self.w.a, OPERATOR, None, lambda tx: None)   # 契約者の番号が無ければ、範囲が決まらない
        got = emergency.stop_all(self.w.a, OPERATOR, "試験：契約者が押した", tenant_id=s["tenant"])
        self.assertIsNotNone(got["db_event"])
        self.assertEqual(self.system_stops(), [])                      # 全体は止めない
        mine = self.w.a.in_tenant(OPERATOR, s["tenant"], lambda tx: stops.open_stops(
            tx, tenant_id=s["tenant"], client_id=None, business_id=None))
        self.assertEqual([(r["scope"], r["cause"]) for r in mine], [("tenant", "manual")])
        self.assertFalse(emergency.release_all(self.w.a, ADMIN, "試験：別の範囲")["released"])   # 押した範囲で解く
        self.assertIsNotNone(emergency.engaged())
        done = emergency.release_all(self.w.a, OPERATOR, "試験：直った", tenant_id=s["tenant"])
        self.assertEqual((done["released"], len(done["resumed"])), (True, 1))
        self.assertIsNone(emergency.engaged())

    def test_without_who_pressed_the_flag_is_kept_but_not_recorded(self):
        for k in ("SALESCALL_ACTOR_KIND", "SALESCALL_ACTOR_NAME", "SALESCALL_DATABASE_URL"):
            self.addCleanup(os.environ.__setitem__, k, os.environ[k]) if k in os.environ else None
            os.environ.pop(k, None)
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(emergency.main(["stop", "試験：誰か分からない"]), 0)
        self.assertIsNotNone(emergency.engaged())                      # 止めることは、いつでもできる
        self.assertIsNone(emergency.engaged()["kind"])
        self.assertIn("SALESCALL_ACTOR_KIND", out.getvalue())
        self.assertEqual(self.system_stops(), [])                      # 決め打ちで管理者として記録しない
        with redirect_stdout(io.StringIO()):
            self.assertEqual(emergency.main(["release", "試験"]), 1)  # 誰か分からなければ解かない
        self.assertIsNotNone(emergency.engaged())
