"""データベースの控えの試験（Intent.md 14-1。core/backup.py・表の作り 0052）。"""

from __future__ import annotations

import os
import tempfile
import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from salescall.core import backup, fix_requests, registry
from salescall.core.access import Rejected
from tests.support import ADMIN, OPERATOR, SYSTEM, World, app_access, owner_access

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
        self.here = backup.LocalDirectory("PC の中（試験）", self.tmp / "pc")
        self.drive_root = self.tmp / "hdd"
        self.drive_root.mkdir()
        (self.drive_root / backup.DRIVE_MARK).write_text("控え用")
        self.away = backup.AttachedDrive("PC に接続したハードディスク（試験）", self.drive_root / "salescall", drive_root=self.drive_root)
        World().setup()                                             # 控えに入る行を作っておく

    def take(self, destinations, key=KEY):
        return backup.take(self.app, os.environ["SALESCALL_TEST_OWNER_DSN"], os.environ["SALESCALL_TEST_RESTORE_DSN"],
                           destinations, key=key)

    def test_take_place_on_the_attached_drive_and_restore_with_the_same_counts(self):
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
        for destinations, key, says in (([self.here], KEY, "Fly.io とは別の場所"),
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
            "SELECT summary FROM salescall.notification_events WHERE kind = 'backup_failed' AND for_admin"
            " AND event_key LIKE '%%:program'"))
        self.assertEqual(len(told), 1)                                # 1日1回（同じ日の失敗は重ねない）
        self.assertNotIn("password", str(told) + str([r["summary"] for r in asked]))

    def test_a_missing_drive_is_not_written_inside_the_pc_and_asks_a_person_to_attach_it(self):
        """ハードディスクが外れていたら置かない（PC の中に同じ名前のフォルダを作って置いてしまわない）。つなぐのは人なので、
        修正依頼ではなく、管理者に「つないでください」と知らせる。"""
        (self.drive_root / backup.DRIVE_MARK).unlink()
        got = self.take([self.away])
        self.assertFalse(got.ok)
        self.assertIn("つながっていません", got.error)
        self.assertFalse((self.drive_root / "salescall").exists())
        told = self.app.in_system(ADMIN, lambda tx: [r["summary"] for r in tx.all(
            "SELECT summary FROM salescall.notification_events WHERE kind = 'backup_failed' AND for_admin")])
        self.assertIn("控え用のハードディスクがつながっていません。PC にハードディスクをつないでください。", told)


class PerClient(unittest.TestCase):
    """依頼者ごとの控え（2026-10-17 ユーザー：「E:\\salescall-backup\\client1」「AI用と、提出用で分ける。AI用は…HDに1年
    …\\AI。提出用はFly.ioには保存しない　HDに3年 …\\submission」「client1・client2 がデフォルト、自社情報からフォルダ名が自動で」）。"""

    def setUp(self):
        self.app = app_access()
        self.w = World()
        self.root = Path(tempfile.mkdtemp(prefix="salescall-test-hdd-"))
        (self.root / backup.DRIVE_MARK).write_text("控え用")
        self.drive = backup.Drive(self.root)

    def run_daily(self):
        return backup.run_daily(self.app, os.environ["SALESCALL_TEST_OWNER_DSN"], os.environ["SALESCALL_TEST_RESTORE_DSN"],
                                self.drive, key=KEY)

    def named_client(self, t, name):
        return self.w.a.in_tenant(OPERATOR, t, lambda tx: registry.create_client(tx, name))

    def test_folders_follow_the_client_name_and_fall_back_to_client_numbers(self):
        t = self.w.tenant()
        for given, want in (("株式会社 架空商事:本社", "架空商事-本社"),       # 2026-10-17 ユーザー：「架空商事-本社」だけでいい
                            ("㈱架空商事　本社", "架空商事-本社"), ("架空商事（株） 本社", "架空商事-本社"),
                            ("架空ＡＢＣ株式会社", "架空ABC"), ("一般社団法人 架空協会 / 東京支部", "架空協会-東京支部"),
                            ("株式会社", None), ("CON", None)):
            with self.subTest(given=given):
                self.assertEqual(backup.folder_name_from(given), want)
        a = self.named_client(t, "株式会社 架空商事:本社")
        b = self.named_client(t, "CON")                                  # Windows で使えない名前
        name_a = backup.client_folder(self.app, t, a)
        self.assertEqual(name_a, "架空商事-本社")
        self.assertRegex(backup.client_folder(self.app, t, b), r"^client\d+$")
        self.w.a.in_tenant(OPERATOR, t, lambda tx: registry.rename_client(tx, a, "名前を直した"))
        self.assertEqual(backup.client_folder(self.app, t, a), name_a)   # 名前を直しても、フォルダは変えない
        for who in (self.w.a, owner_access()):                            # 決めたフォルダの名前は、表の持ち主でも変えられない
            with self.subTest(who=who), self.assertRaises(Rejected):
                who.in_tenant(OPERATOR, t, lambda tx: tx.execute(
                    "UPDATE salescall.client_backup_folders SET folder_name = 'x' WHERE client_id = %s", (a,)))

    def test_daily_backup_places_system_and_per_client_ai_and_copies_submissions(self):
        s = World().setup()
        sub = self.w.a.in_client(SYSTEM, s["tenant"], s["client"], lambda tx: backup.record_submission(
            tx, "結果_2026-10-17.csv", "架空の結果".encode()))
        results = self.run_daily()
        self.assertTrue(all(r.ok for r in results), [r.error for r in results if not r.ok])
        base = self.root / backup.BASE
        self.assertTrue(list((base / "system" / backup.AI).glob("salescall-*.dump.enc")))
        folder = backup.client_folder(self.app, s["tenant"], s["client"])
        ai = list((base / folder / backup.AI).glob("salescall-*.ai.enc"))
        self.assertEqual(len(ai), 1)
        head, tables = backup.read_client_export(backup.open_file(ai[0], KEY))
        self.assertEqual(head["client_id"], str(s["client"]))
        self.assertTrue(tables["tasks"])                                  # その依頼者の行が入っている
        self.assertEqual({r["client_id"] for rows in tables.values() for r in rows}, {str(s["client"])})   # ほかの依頼者は入らない
        copied = list((base / folder / backup.SUBMISSION).glob("salescall-*.enc"))
        self.assertEqual([backup.open_file(f, KEY) for f in copied], ["架空の結果".encode()])
        row = self.w.a.in_client(SYSTEM, s["tenant"], s["client"], lambda tx: tx.one(
            "SELECT content, copied_as FROM salescall.submission_files WHERE submission_id = %s", (sub,)))
        self.assertEqual((row["content"], row["copied_as"]), (None, copied[0].name))   # Fly.io には残さない
        with self.assertRaises(Rejected):                                 # 写した記録は変えられない
            self.w.a.in_client(SYSTEM, s["tenant"], s["client"], lambda tx: tx.execute(
                "UPDATE salescall.submission_files SET copied_as = 'x' WHERE submission_id = %s", (sub,)))

    def test_old_backups_are_removed_by_kind(self):
        """AI用は1年、提出用は3年（ハードディスク）。名前に日時のある控えのファイルだけを消す。"""
        s = World().setup()
        folder = backup.client_folder(self.app, s["tenant"], s["client"])
        now = datetime.now().astimezone()
        def put(kind, days_ago, tail):
            d = self.root / backup.BASE / folder / kind
            d.mkdir(parents=True, exist_ok=True)
            f = d / f"salescall-{now - timedelta(days=days_ago):%Y%m%d-%H%M%S}{tail}"
            f.write_bytes(b"x")
            return f
        old_ai, new_ai = put(backup.AI, 400, ".ai.enc"), put(backup.AI, 300, ".ai.enc")
        mid_sub, old_sub = put(backup.SUBMISSION, 400, "-1-a.csv.enc"), put(backup.SUBMISSION, 1200, "-2-b.csv.enc")
        other = self.root / backup.BASE / folder / backup.AI / "人が置いたメモ.txt"
        other.write_text("消さない")
        backup.prune(self.app, self.drive, now=now)
        self.assertEqual([f.exists() for f in (old_ai, new_ai, mid_sub, old_sub, other)], [False, True, True, False, True])

    def test_nothing_is_written_when_the_drive_is_not_attached(self):
        (self.root / backup.DRIVE_MARK).unlink()
        got = self.run_daily()
        self.assertEqual([r.ok for r in got], [False])
        self.assertFalse((self.root / backup.BASE).exists())


if __name__ == "__main__":
    unittest.main()
