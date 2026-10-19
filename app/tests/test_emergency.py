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
from tests.support import ADMIN, OPERATOR, SYSTEM
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

    def env(self, **values):
        """見守り役と同じ設定の名前だけを入れる（ほかは消す）。PC の鍵は無いものとする。"""
        from salescall.supervisor import config, pc_login
        for k in config.NAMES:
            self.addCleanup(os.environ.__setitem__, k, os.environ[k]) if k in os.environ else None
            os.environ.pop(k, None)
        os.environ.update(values)
        original = pc_login.load
        pc_login.load = lambda: None
        self.addCleanup(setattr, pc_login, "load", original)

    def test_the_admin_pc_stops_with_the_same_connection_as_the_supervisor(self):
        """管理者の PC：見守り役と同じ設定（SALESCALL_DSN・SALESCALL_TENANT_ID）で、管理者として全体を止め、解ける。⚠️ 前は
        PC の準備に無い名前（SALESCALL_DATABASE_URL など）を読み、印だけで止まって解けなかった（2026-10-19 の見直し）。"""
        s = self.ready()
        self.env(SALESCALL_DSN=self.w.a._conninfo, SALESCALL_TENANT_ID=str(s["tenant"]))
        with redirect_stdout(io.StringIO()):
            self.assertEqual(emergency.main(["stop", "試験：管理者の PC"]), 0)
        self.assertEqual(len(self.system_stops()), 1)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(emergency.main(["release", "試験：直った"]), 0)
        self.assertIsNone(emergency.engaged())
        self.assertEqual(self.system_stops(), [])

    def test_a_contractor_pc_stops_and_releases_its_contractor_with_the_pc_key(self):
        """契約者の PC：見守り役と同じ PC の鍵で、その契約者として、その契約者を止め、解ける（全体は止めない）。"""
        from salescall.core import logins
        from salescall.core.access import Provisioner
        from salescall.supervisor import pc_login
        s = self.ready()
        self.env()
        key = logins.issue(self.w.a, SYSTEM, Provisioner(os.environ["SALESCALL_TEST_PROVISIONER_DSN"]), s["tenant"],
                           pc_label="試験のPC", base_conninfo=os.environ["SALESCALL_TEST_APP_DSN"], replaces=None)
        pc_login.load = lambda: pc_login.PcKey(key.tenant_id, key.pc, key.conninfo)
        opened = lambda: self.w.a.in_tenant(OPERATOR, s["tenant"], lambda tx: [   # noqa: E731
            (r["scope"], r["cause"]) for r in stops.open_stops(tx, tenant_id=s["tenant"], client_id=None, business_id=None)])
        with redirect_stdout(io.StringIO()):
            self.assertEqual(emergency.main(["stop", "試験：契約者の PC"]), 0)
        self.assertEqual(opened(), [("tenant", "manual")])
        self.assertEqual(self.system_stops(), [])
        with redirect_stdout(io.StringIO()):
            self.assertEqual(emergency.main(["release", "試験：直った"]), 0)
        self.assertEqual((opened(), emergency.engaged()), ([], None))

    def test_a_revoked_pc_key_still_places_the_flag_and_tells_to_log_in_again(self):
        """契約者の PC の鍵が止められていても（データベースが断る＝LoginRefused）、止める印は残し、ログインし直すよう知らせる。
        状態の確かめ・解くも落ちない（⚠️ 前は受け止めず、印を置いたあと誤りの表示で落ちた＝見直し5）。"""
        from salescall.core import logins
        from salescall.core.access import Provisioner
        from salescall.supervisor import pc_login
        s = self.ready()
        self.env()
        provisioner = Provisioner(os.environ["SALESCALL_TEST_PROVISIONER_DSN"])
        key = logins.issue(self.w.a, SYSTEM, provisioner, s["tenant"], pc_label="試験のPC",
                           base_conninfo=os.environ["SALESCALL_TEST_APP_DSN"], replaces=None)
        logins.revoke(self.w.a, SYSTEM, provisioner, s["tenant"], pc=key.pc)
        pc_login.load = lambda: pc_login.PcKey(key.tenant_id, key.pc, key.conninfo)
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(emergency.main(["stop", "試験：止められた鍵"]), 0)
        self.assertIsNotNone(emergency.engaged())
        self.assertIn("ログインし直して", out.getvalue())
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(emergency.main(["status"]), 0)
            self.assertEqual(emergency.main(["release", "試験：解けない"]), 1)
        self.assertIn("ログインし直して", out.getvalue())                 # 解けなかった理由も見せる（見直し7で確かめを足した）
        self.assertIsNotNone(emergency.engaged())                      # 解いたことを記録できないまま、かけ始めない

    def test_a_broken_pc_key_still_places_the_flag(self):
        """PC の鍵のファイルが壊れていても、止める印は残す（止めることは、いつでもできる。⚠️ 前は印を書く前に落ちた）。"""
        from salescall.supervisor import pc_login
        self.env()
        pc_login.load = lambda: (_ for _ in ()).throw(ValueError("壊れた鍵のファイル"))
        with redirect_stdout(io.StringIO()):
            self.assertEqual(emergency.main(["stop", "試験：鍵が壊れている"]), 0)
        self.assertIsNotNone(emergency.engaged())

    def test_without_who_pressed_the_flag_is_kept_but_not_recorded(self):
        self.env()                                                     # つなぎ先も PC の鍵も無い
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(emergency.main(["stop", "試験：誰か分からない"]), 0)
        self.assertIsNotNone(emergency.engaged())                      # 止めることは、いつでもできる
        self.assertIsNone(emergency.engaged()["kind"])
        self.assertIn("ログイン", out.getvalue())
        self.assertEqual(self.system_stops(), [])                      # 決め打ちで管理者として記録しない
        with redirect_stdout(io.StringIO()):
            self.assertEqual(emergency.main(["release", "試験"]), 1)  # 誰か分からなければ解かない
        self.assertIsNotNone(emergency.engaged())
