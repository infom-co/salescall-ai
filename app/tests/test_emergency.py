"""本番のPCの緊急停止（core/emergency.py。CL-3 の 4-1d の2）の試験。"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from salescall.core import dial_exit, emergency, stops
from salescall.core.access import Access
from tests.support import ADMIN
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
