"""データベースの控えの試験（Intent.md 14-1。core/backup.py・表の作り 0052）。"""

from __future__ import annotations

import os
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

from salescall.core import backup, fix_requests
from tests.support import ADMIN, SYSTEM, World, app_access

KEY = bytes(range(32))   # 試験の鍵（本番の鍵は呼ぶ側が渡す）


@dataclass(frozen=True)
class Tampered(backup.LocalDirectory):
    """読み戻すと中身が変わっている置き場（置いた控えが壊れた・取り替えられた）。"""
    def get(self, file_name: str) -> bytes:
        data = bytearray(super().get(file_name))
        data[-1] ^= 0xFF
        return bytes(data)


class Backup(unittest.TestCase):
    def setUp(self):
        self.app = app_access()
        self.tmp = Path(tempfile.mkdtemp(prefix="salescall-test-backup-"))
        self.here = backup.LocalDirectory("Fly.io の置き場（試験）", self.tmp / "fly")
        self.away = backup.LocalDirectory("別の場所（試験）", self.tmp / "away", offsite=True)
        World().setup()                                             # 控えに入る行を作っておく

    def take(self, destinations, key=KEY):
        return backup.take(self.app, os.environ["SALESCALL_TEST_OWNER_DSN"], os.environ["SALESCALL_TEST_RESTORE_DSN"],
                           destinations, key=key)

    def test_take_place_in_two_places_and_restore_with_the_same_counts(self):
        got = self.take([self.here, self.away])
        self.assertTrue(got.ok, got.error)
        for d in (self.here, self.away):
            blob = (d.path / got.file_name).read_bytes()
            self.assertTrue(blob.startswith(backup.MAGIC))
            self.assertNotIn("架空の依頼者".encode(), blob)             # 暗号化してある（中身が読めない）
        row = backup.latest(self.app)
        self.assertEqual((row["restore_ok"], row["error"], sorted(row["destinations"])),
                         (True, None, sorted([self.here.name, self.away.name])))
        self.assertFalse(backup.due(self.app))                       # 今日の分は済んだ
        with self.assertRaises(backup.BackupFailed):                  # 違う鍵では戻せない
            backup.decrypt((self.away.path / got.file_name).read_bytes(), bytes(32), got.file_name)

    def test_a_failure_does_not_stop_and_asks_cl2_to_fix_it(self):
        """取れない・戻せないときは、止めずに CL-2 へ修正依頼を出し、管理者に知らせる（2026-10-17 ユーザー）。"""
        for destinations, key, says in (([self.here], KEY, "2か所以上"),
                                        ([self.here, self.away], b"short", "32バイト"),
                                        ([self.here, Tampered("壊れた置き場", self.tmp / "bad", offsite=True)], KEY,
                                         "置いたものと違います")):
            with self.subTest(says=says):
                got = self.take(destinations, key=key)
                self.assertFalse(got.ok)
                self.assertIn(says, got.error)
                self.assertFalse(backup.latest(self.app)["restore_ok"])
        asked = self.app.in_system(SYSTEM, lambda tx: fix_requests.open_requests(tx, "CL-2"))
        self.assertIn("backup_failed", [r["kind"] for r in asked])
        told = self.app.in_system(ADMIN, lambda tx: tx.all(
            "SELECT summary FROM salescall.notification_events WHERE kind = 'backup_failed' AND for_admin"))
        self.assertEqual(len(told), 1)                                # 1日1回（同じ日の失敗は重ねない）
        self.assertNotIn("password", str(told) + str([r["summary"] for r in asked]))


if __name__ == "__main__":
    unittest.main()
