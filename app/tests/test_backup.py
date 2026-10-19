"""データベースの控えの試験（Intent.md 14-1。core/backup.py・表の作り 0052）。"""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from salescall.core import backup, fix_requests, registry
from salescall.core.access import Rejected
from tests.support import ADMIN, OPERATOR, SYSTEM, World, app_access, owner_access

KEY = bytes(range(32))   # 試験の鍵（本番の鍵は呼ぶ側が渡す）
ENV = {backup.KEY_ENV: "試験の控えの鍵の文字-0123456789abcdef"}   # 試験の PC の環境変数（架空の鍵の文字）
CKEY = backup.load_key(ENV)


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
            " AND event_key LIKE '%%:system:-'"))
        self.assertEqual(len(told), 1)                                # 1日1回（同じ日の失敗は重ねない）
        self.assertNotIn("password", str(told) + str([r["summary"] for r in asked]))

    def test_the_check_never_wipes_the_real_database(self):
        """確かめ用の接続先を本番と書き間違えても、消さずに止める（2026-10-18 の見直し）。"""
        got = backup.take(
            self.app, os.environ["SALESCALL_TEST_OWNER_DSN"], os.environ["SALESCALL_TEST_OWNER_DSN"], [self.away], key=KEY)
        self.assertFalse(got.ok)
        self.assertIn("本番のデータベースと同じ", got.error)
        n = owner_access().in_system(SYSTEM, lambda tx: tx.one("SELECT count(*) AS n FROM salescall.clients"))["n"]
        self.assertGreater(n, 0)                                        # 本番の表は残っている

    def test_tool_errors_do_not_carry_customer_data(self):
        """道具の誤りの文（お客様の行の中身が入りうる）は残さず、段と種類だけを残す。パスワードは引数に入れない。"""
        with self.assertRaises(backup.BackupFailed) as got:
            backup._run(["sh", "-c", "echo 'COPY contacts, line 1: 架空太郎 09000000000' >&2; exit 1", "sh"],
                        "dbname=x password=架空のパスワード", "試験の段")
        self.assertEqual(str(got.exception), "試験の段に失敗しました（種類は分からない。終了コード 1）")
        dsn, secret = backup.tool_connection("dbname=x password=架空のパスワード")
        self.assertNotIn("架空のパスワード", dsn)
        self.assertEqual(secret, {"PGPASSWORD": "架空のパスワード"})

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


class Key(unittest.TestCase):
    """控えの鍵：Proton Pass で作った文字を PC の環境変数に入れ、そこから作る（2026-10-17 ユーザー）。"""

    def test_the_same_text_makes_the_same_key_and_the_value_is_never_shown(self):
        text = "架空の鍵の元-0123456789-abcdefghij"
        key = backup.load_key({backup.KEY_ENV: text + "\n"})            # 写したときの改行は外す
        self.assertEqual((len(key), key), (32, backup.key_from_text(text)))
        self.assertNotEqual(key, backup.key_from_text(text + "x"))
        blob = backup.encrypt(b"data", key, "f")
        self.assertEqual(backup.decrypt(blob, backup.load_key({backup.KEY_ENV: text}), "f"), b"data")
        for env, says in (({}, "入っていません"), ({backup.KEY_ENV: "short-secret"}, "20文字以上")):
            with self.subTest(says=says), self.assertRaises(backup.BackupFailed) as got:
                backup.load_key(env)
            self.assertIn(says, str(got.exception))
            self.assertNotIn("short-secret", str(got.exception))


class PerClient(unittest.TestCase):
    """依頼者ごとの控え（2026-10-17 ユーザー：「E:\\salescall-backup\\client1」「AI用と、提出用で分ける。AI用は…HDに1年
    …\\AI。提出用はFly.ioには保存しない　HDに3年 …\\submission」「client1・client2 がデフォルト、自社情報からフォルダ名が自動で」）。
    **控えは契約者ごと**（2026-10-18 ユーザー：「費用の見張りと控えは、契約者ごとですよ」）：契約者の PC が、その契約者の依頼者の分だけを
    置く。データベース全体の控えは、すべての契約者のデータが入るので管理者の PC だけ。"""

    def setUp(self):
        self.app = app_access()
        self.w = World()
        self.root = Path(tempfile.mkdtemp(prefix="salescall-test-hdd-"))
        (self.root / backup.DRIVE_MARK).write_text("控え用")
        self.drive = backup.Drive(self.root)

    def run_contractor(self, tenant, environ=ENV):
        return backup.run_contractor(self.app, tenant, self.root, environ)

    def run_whole(self, environ=ENV):
        return backup.run_whole(self.app, os.environ["SALESCALL_TEST_OWNER_DSN"], os.environ["SALESCALL_TEST_RESTORE_DSN"],
                                self.root, environ)

    def named_client(self, t, name):
        return self.w.a.in_tenant(OPERATOR, t, lambda tx: registry.create_client(tx, name))

    def runs(self, tenant, actor=OPERATOR):
        return self.app.in_tenant(actor, tenant, lambda tx: tx.all(
            "SELECT kind, tenant_id, client_id, restore_ok FROM salescall.backup_runs ORDER BY run_id"))

    def notices(self, tenant=None):
        """控えの知らせ（tenant＝その契約者の台帳／None＝管理者あて）。"""
        if tenant is None:
            return self.app.in_system(ADMIN, lambda tx: tx.all(
                "SELECT summary, origin_tenant FROM salescall.notification_events WHERE kind = 'backup_failed' AND for_admin"))
        return self.app.in_tenant(OPERATOR, tenant, lambda tx: tx.all(
            "SELECT summary FROM salescall.notification_events WHERE kind = 'backup_failed'"))

    def test_folders_follow_the_client_name_and_fall_back_to_client_numbers(self):
        t = self.w.tenant()
        for given, want in (("株式会社 架空商事:本社", "架空商事-本社"),       # 2026-10-17 ユーザー：「架空商事-本社」だけでいい
                            ("㈱架空商事　本社", "架空商事-本社"), ("架空商事（株） 本社", "架空商事-本社"),
                            ("架空ＡＢＣ株式会社", "架空ABC"), ("一般社団法人 架空協会 / 東京支部", "架空協会-東京支部"),
                            ("株式会社", None), ("CON", None), ("System", None)):
            with self.subTest(given=given):
                self.assertEqual(backup.folder_name_from(given), want)
        a = self.named_client(t, "株式会社 架空商事:本社")
        b = self.named_client(t, "CON")                                  # Windows で使えない名前
        name_a = backup.client_folder(self.app, t, a)
        self.assertEqual(name_a, "架空商事-本社")
        self.assertRegex(backup.client_folder(self.app, t, b), r"^client\d+$")
        self.w.a.in_tenant(OPERATOR, t, lambda tx: registry.rename_client(tx, a, "名前を直した"))
        self.assertEqual(backup.client_folder(self.app, t, a), name_a)   # 名前を直しても、フォルダは変えない
        latin = self.named_client(t, "Kakuu Shoji")
        self.assertEqual(backup.client_folder(self.app, t, latin), "Kakuu-Shoji")
        self.assertRegex(backup.client_folder(self.app, t, self.named_client(t, "KAKUU SHOJI")), r"^client\d+$")
        with self.assertRaises(Rejected):                                 # 大文字・小文字だけ違う名前は、表も重なりとして拒む（Windows）
            self.w.a.in_client(SYSTEM, t, self.named_client(t, "別の依頼者"), lambda tx: tx.execute(
                "INSERT INTO salescall.client_backup_folders (tenant_id, client_id, folder_name)"
                " VALUES (salescall.cur_tenant(), salescall.cur_client(), 'KAKUU-SHOJI')"))
        for who in (self.w.a, owner_access()):                            # 決めたフォルダの名前は、表の持ち主でも変えられない
            with self.subTest(who=who), self.assertRaises(Rejected):
                who.in_tenant(OPERATOR, t, lambda tx: tx.execute(
                    "UPDATE salescall.client_backup_folders SET folder_name = 'x' WHERE client_id = %s", (a,)))

    def test_a_contractor_backs_up_only_its_own_clients_and_copies_submissions(self):
        s, other = World().setup(), World().setup()
        sub = self.w.a.in_client(SYSTEM, s["tenant"], s["client"], lambda tx: backup.record_submission(
            tx, "結果_2026-10-17.csv", "架空の結果".encode()))
        theirs = self.w.a.in_client(SYSTEM, other["tenant"], other["client"], lambda tx: backup.record_submission(
            tx, "よその結果.csv", "よその結果".encode()))
        results = self.run_contractor(s["tenant"])
        self.assertTrue(all(r.ok for r in results), [r.error for r in results if not r.ok])
        base = self.root / backup.BASE
        folder = backup.client_folder(self.app, s["tenant"], s["client"])
        self.assertEqual(sorted(p.name for p in base.iterdir()), [folder])   # ほかの契約者の依頼者・データベース全体は置かない
        again = self.run_contractor(s["tenant"])                          # 数分後にもう一度：今日の分は済んだので、重ねて取らない
        ai = list((base / folder / backup.AI).glob("salescall-*.ai.enc"))
        self.assertEqual((again, len(ai)), ([], 1))
        head, tables = backup.read_client_export(backup.open_file(ai[0], CKEY))
        self.assertEqual(head["client_id"], str(s["client"]))
        self.assertTrue(tables["tasks"])                                  # その依頼者の行が入っている
        self.assertEqual({r["client_id"] for rows in tables.values() for r in rows}, {str(s["client"])})   # ほかの依頼者は入らない
        own = owner_access().in_system(SYSTEM, lambda tx: {t: tx.one(f'SELECT count(*) AS n FROM salescall."{t}" WHERE client_id = %s',
                                                                     (s["client"],))["n"] for t in head["counts"]})
        self.assertEqual({t: n for t, n in head["counts"].items() if t != "backup_runs"},
                         {t: n for t, n in own.items() if t != "backup_runs"})   # 契約者の取引からでも、その依頼者の行はすべて入る
        copied = list((base / folder / backup.SUBMISSION).glob("salescall-*.enc"))
        self.assertEqual([backup.open_file(f, CKEY) for f in copied], ["架空の結果".encode()])
        row = self.w.a.in_client(SYSTEM, s["tenant"], s["client"], lambda tx: tx.one(
            "SELECT content, copied_as FROM salescall.submission_files WHERE submission_id = %s", (sub,)))
        self.assertEqual((row["content"], row["copied_as"]), (None, copied[0].name))   # Fly.io には残さない
        left = self.w.a.in_client(SYSTEM, other["tenant"], other["client"], lambda tx: tx.one(
            "SELECT content FROM salescall.submission_files WHERE submission_id = %s", (theirs,)))["content"]
        self.assertIsNotNone(left)                                        # ほかの契約者の提出用は、その契約者の PC が写す
        with self.assertRaises(Rejected):                                 # 写した記録は変えられない
            self.w.a.in_client(SYSTEM, s["tenant"], s["client"], lambda tx: tx.execute(
                "UPDATE salescall.submission_files SET copied_as = 'x' WHERE submission_id = %s", (sub,)))
        kinds = {(r["kind"], r["tenant_id"]) for r in self.runs(s["tenant"])}
        self.assertEqual(kinds, {("client_ai", s["tenant"]), ("prune", s["tenant"])})   # 記録はその契約者の分
        self.assertEqual(self.runs(other["tenant"]), [])                  # ほかの契約者からは見えない
        with self.assertRaises(Rejected):                                 # 書き出しは書かない取引（長く読んでも、通話の処理をやり直させない）
            self.app.in_client(SYSTEM, s["tenant"], s["client"], lambda tx: backup.record_submission(tx, "x.csv", b"x"),
                               read_only=True)
        with self.assertRaises(Rejected):                                 # 記録を書けるのはシステムの処理だけ
            self.app.in_tenant(OPERATOR, s["tenant"], lambda tx: tx.execute(
                "INSERT INTO salescall.backup_runs (kind, tenant_id, restore_ok, error) VALUES ('drive', %s, false, '試験')",
                (s["tenant"],)))

    def test_the_whole_database_is_backed_up_only_by_the_admin_pc(self):
        """データベース全体の控え（すべての契約者のデータが入る）は、管理者の PC だけ。記録は契約者を持たない。"""
        World().setup()
        # 1日1回（ほかの試験が今日もう取っていれば取らない）。ほかの試験が1時間以内に失敗させていれば、やり直さない
        was_due = backup.due(self.app) and backup._may_try(self.app, "system")
        results = self.run_whole()
        self.assertTrue(all(r.ok for r in results), [r.error for r in results if not r.ok])
        base = self.root / backup.BASE
        self.assertEqual(bool(list((base / backup.WHOLE / backup.AI).glob("salescall-*.dump.enc"))), was_due)
        self.assertFalse(was_due and backup.due(self.app))
        self.assertEqual([p.name for p in (base.iterdir() if base.exists() else ()) if p.name != backup.WHOLE], [])   # 依頼者のフォルダは作らない
        with self.assertRaises(Rejected):                                 # データベース全体の記録に契約者は付けられない
            self.app.in_tenant(SYSTEM, self.w.tenant(), lambda tx: tx.execute(
                "INSERT INTO salescall.backup_runs (kind, tenant_id, restore_ok, error)"
                " VALUES ('system', salescall.cur_tenant(), false, '試験')"))

    def test_submissions_are_copied_right_away_and_kept_while_the_drive_is_missing(self):
        """見守り役が数分ごとに写す（Fly.io に残る時間を短く）。ハードディスクが外れていれば写さずに残し（消すと失う）、つないだら写す。
        つなぐのは契約者（その PC の持ち主）なので、修正依頼ではなく、その契約者に「つないでください」と知らせる。"""
        s = World().setup()
        sub = self.w.a.in_client(SYSTEM, s["tenant"], s["client"], lambda tx: backup.record_submission(
            tx, "結果.csv", "架空の結果".encode()))
        content = lambda: self.w.a.in_client(SYSTEM, s["tenant"], s["client"], lambda tx: tx.one(   # noqa: E731
            "SELECT content FROM salescall.submission_files WHERE submission_id = %s", (sub,)))["content"]
        (self.root / backup.DRIVE_MARK).unlink()
        for _ in range(3):                                               # 数分ごとに呼ばれても（まだ外れている）
            self.assertEqual([r.error for r in backup.copy_submissions(self.app, self.drive, s["tenant"], key=CKEY)],
                             [backup.DRIVE_MISSING])
        self.assertEqual([r["kind"] for r in self.runs(s["tenant"])], ["drive"])   # 記録は1時間に1行まで
        self.assertEqual([r["summary"] for r in self.notices(s["tenant"])], [backup.DriveMissing.ask])
        self.assertNotIn(s["tenant"], [r["origin_tenant"] for r in self.notices()])   # 管理者の作業ではない
        self.assertIsNotNone(content())                                   # 外れている間は残す
        (self.root / backup.DRIVE_MARK).write_text("控え用")
        self.assertEqual(backup.copy_submissions(self.app, self.drive, s["tenant"], key=CKEY), [])
        self.assertIsNone(content())
        self.assertEqual(backup.copy_submissions(self.app, self.drive, s["tenant"], key=CKEY), [])   # 写すものが無ければ何もしない

    def test_a_missing_key_is_asked_to_that_contractor(self):
        """鍵が無ければ、その契約者に「鍵を入れてください」。2人の契約者が同じ日に同じ知らせ（同じ鍵の出来事）になっても、
        それぞれに届く（知らせの「同じ出来事」は契約者ごと＝0064）。"""
        s, other = World().setup(), World().setup()
        for x in (s, other):
            self.assertEqual([r.ok for r in self.run_contractor(x["tenant"], environ={})], [False])
            self.assertEqual([r["kind"] for r in self.runs(x["tenant"])], ["key"])
            self.assertEqual([r["summary"] for r in self.notices(x["tenant"])], [backup.KeyMissing.ask])
        self.assertFalse((self.root / backup.BASE).exists())

    def test_failures_are_asked_to_fix_by_kind_and_told_to_the_admin_and_that_contractor(self):
        """同じ日に、データベース全体と依頼者ごとの控えが両方失敗したら、修正依頼も種類ごとに出る（片方が埋もれない）。
        プログラムの誤りは、管理者へ（直せるのは管理者だけ）と、契約者の控えならその契約者へも知らせる。2人の契約者の知らせは、
        同じ日の同じ種類でも、それぞれに届く（0064）。"""
        s, other = World().setup(), World().setup()
        got = [backup._client_ai(self.app, self.drive, x["tenant"], x["client"], key=b"short") for x in (s, other)]
        got.append(backup.take(self.app, os.environ["SALESCALL_TEST_OWNER_DSN"], os.environ["SALESCALL_TEST_RESTORE_DSN"],
                               [self.drive.folder(backup.WHOLE, backup.AI)], key=b"short"))
        self.assertFalse(any(r.ok for r in got))
        asked = self.app.in_system(SYSTEM, lambda tx: [r["summary"] for r in fix_requests.open_requests(tx, "CL-2")
                                                       if r["kind"] == "backup_failed"])
        for part in ("system", "client_ai"):
            with self.subTest(part=part):
                self.assertTrue(any(x.startswith(backup.PARTS[part] + "：") for x in asked), asked)
        for x in (s, other):
            with self.subTest(tenant=x["tenant"]):
                self.assertEqual(len(self.notices(x["tenant"])), 1)
                self.assertIn(x["tenant"], [r["origin_tenant"] for r in self.notices()])

    def test_backups_not_taken_are_noticed_even_without_any_backup_settings(self):
        """控えを取る PC に設定が無く、取る仕事が1度も動かなくても、取れていないことに気づいて知らせる（記録だけを見る見守り。
        依頼者ごと＝その契約者と管理者・データベース全体＝管理者。1日1回。止めない）。⚠️ 前は、失敗の記録も知らせも出ないまま、
        控えが1つも無くなった（見直し8。Intent.md 14-1「取れなかったら知らせる」）。見張りの長さを短くし、記録の時刻を少し先にして
        確かめる（試験で1日待たない）。"""
        s = World().setup()
        second = self.named_client(s["tenant"], "架空の依頼者（2）")
        soon = datetime.now().astimezone() + timedelta(seconds=30)
        ok = lambda c, error=None: backup._record(   # noqa: E731
            self.app, soon, "x.ai.enc", b"x", {"tasks": 1}, ["試験の場所1", "試験の場所2"], error, person=None, part="client_ai",
            tenant_id=s["tenant"], client_id=c)
        mine = lambda: [r["summary"] for r in self.notices(s["tenant"]) if r["summary"] == backup.OVERDUE_CLIENT]   # noqa: E731
        with mock.patch.object(backup, "OVERDUE", timedelta(days=1)):          # 作ったばかりの依頼者は数えない
            self.assertEqual(backup.watch_contractor(self.app, s["tenant"]), [])
        with mock.patch.object(backup, "OVERDUE", timedelta(microseconds=1)):
            self.assertEqual(sorted(backup.watch_contractor(self.app, s["tenant"])), sorted([s["client"], second]))
            self.assertEqual(len(mine()), 2)                                   # 依頼者ごとに、その契約者へ
            backup.watch_contractor(self.app, s["tenant"])
            self.assertEqual(len(mine()), 2)                                   # 知らせは1日1回
            self.assertEqual([r["origin_tenant"] for r in self.notices() if r["summary"] == backup.OVERDUE_CLIENT
                              ].count(s["tenant"]), 2)                         # 管理者へも
            ok(s["client"], error="試験：戻せなかった")                       # 戻せなかった控えは、取れたうちに入らない
            ok(second)                                                         # ほかの依頼者の控えも入らない
            self.assertEqual(backup.watch_contractor(self.app, s["tenant"]), [s["client"]])
            ok(s["client"])
            self.assertEqual(backup.watch_contractor(self.app, s["tenant"]), [])
            self.assertTrue(backup.watch_whole(self.app))                      # データベース全体は管理者へ（1日1回）
            whole = lambda: [r for r in self.notices() if r["summary"] == backup.OVERDUE_WHOLE]   # noqa: E731
            self.assertEqual([r["origin_tenant"] for r in whole()], [None])
            backup.watch_whole(self.app)
            self.assertEqual(len(whole()), 1)
            backup._record(self.app, soon, "x.dump.enc", b"x", {"tasks": 1}, ["試験の場所1", "試験の場所2"], None, person=None)
            self.assertFalse(backup.watch_whole(self.app))
        self.assertNotIn(backup.OVERDUE_WHOLE, [r["summary"] for r in self.notices(s["tenant"])])   # 全体の控えは契約者へ知らせない

    def test_folder_names_do_not_show_other_contractors_clients(self):
        """フォルダの名前の重なりは、契約者の中だけで見る（ほかの契約者に同じ名前の依頼者がいても、名前はずれない＝いるかが分からない。
        0084）。同じ契約者の中では重ならない。"""
        t1, t2 = self.w.tenant(), self.w.tenant()
        a, b = self.named_client(t1, "架空商事株式会社"), self.named_client(t2, "架空商事株式会社")
        c = self.named_client(t1, "架空商事株式会社")
        names = [backup.client_folder(self.app, t, x) for t, x in ((t1, a), (t2, b), (t1, c))]
        self.assertEqual(names[0], names[1])
        self.assertNotEqual(names[0].lower(), names[2].lower())

    def test_two_clients_failing_on_the_same_day_are_both_told(self):
        """同じ契約者の2つの依頼者の控えが同じ日に失敗しても、知らせはそれぞれに届く（2026-10-18 の見直し：前は2つ目が黙って落ちた）。"""
        s = World().setup()
        other = World().setup(tenant=s["tenant"])
        for x in (s, other):
            self.assertFalse(backup._client_ai(self.app, self.drive, x["tenant"], x["client"], key=b"short").ok)
        self.assertEqual(len(self.notices(s["tenant"])), 2)

    def test_old_backups_are_removed_by_kind_only_in_that_contractors_folders(self):
        """AI用は1年、提出用は3年（ハードディスク）。名前に日時のある控えのファイルだけを消す。ほかの契約者のフォルダには触れない
        （ハードディスクは PC＝契約者ごと。フォルダの名前は契約者の中で重ならない＝ほかの契約者と同じ名前でもよい。0084）。"""
        s, other = World().setup(), World().setup()
        theirs_root = Path(tempfile.mkdtemp(prefix="salescall-test-hdd-"))   # ほかの契約者の PC のハードディスク
        now = datetime.now().astimezone()
        def put(x, kind, days_ago, tail):
            root = self.root if x is s else theirs_root
            d = root / backup.BASE / backup.client_folder(self.app, x["tenant"], x["client"]) / kind
            d.mkdir(parents=True, exist_ok=True)
            f = d / f"salescall-{now - timedelta(days=days_ago):%Y%m%d-%H%M%S}{tail}"
            f.write_bytes(b"x")
            return f
        old_ai, new_ai = put(s, backup.AI, 400, ".ai.enc"), put(s, backup.AI, 300, ".ai.enc")
        mid_sub, old_sub = put(s, backup.SUBMISSION, 400, "-1-a.csv.enc"), put(s, backup.SUBMISSION, 1200, "-2-b.csv.enc")
        theirs = put(other, backup.AI, 400, ".ai.enc")
        memo = new_ai.parent / "人が置いたメモ.txt"
        memo.write_text("消さない")
        backup.prune(self.app, self.drive, s["tenant"], now=now)
        self.assertEqual([f.exists() for f in (old_ai, new_ai, mid_sub, old_sub, memo, theirs)],
                         [False, True, True, False, True, True])

    def test_the_newest_verified_backup_is_kept_even_if_old(self):
        """控えの失敗が長く続いても、戻せることまで確かめた一番新しい控えは消さない。"""
        now = datetime.now().astimezone()
        dest = [self.drive.folder(backup.WHOLE, backup.AI)]
        take = lambda days: backup.take(self.app, os.environ["SALESCALL_TEST_OWNER_DSN"],   # noqa: E731
                                        os.environ["SALESCALL_TEST_RESTORE_DSN"], dest, key=KEY, now=now - timedelta(days=days))
        oldest, older = take(500), take(400)
        self.assertTrue(oldest.ok and older.ok)
        backup.prune(self.app, self.drive, None, now=now)
        self.assertEqual([(dest[0].path / r.file_name).exists() for r in (oldest, older)], [False, True])

    def test_keep_days_are_at_least_one_and_backup_lookups_are_for_the_backup_only(self):
        with self.assertRaises(Rejected):
            self.app.in_system(ADMIN, lambda tx: tx.execute(
                "INSERT INTO salescall.setting_values (key, scope, value, status, rationale, review_when, set_by)"
                " VALUES ('backup.ai.keep_days_drive', 'system', '0', 'decided', '試験', '試験', '試験')"))
        t = self.w.tenant()
        for sql in ("SELECT salescall.backup_folder_taken('client1')", "SELECT salescall.recordings_held(ARRAY[]::uuid[])"):
            with self.subTest(sql=sql), self.assertRaises(Rejected):   # ほかの契約者の情報は、控えの処理だけが読める
                self.w.a.in_tenant(OPERATOR, t, lambda tx: tx.execute(sql))

    def test_one_fix_request_per_fault_until_reported_fixed(self):
        """同じ不具合の修正依頼は、直したと報告されるまで1つ（前は1時間ごと・1日ごと・通話ごとに増えた）。"""
        ask = lambda: self.app.in_system(SYSTEM, lambda tx: fix_requests.request(   # noqa: E731
            tx, "CL-2", "backup_failed", key="試験の不具合:1", summary="試験"))
        self.assertEqual([ask(), ask()], [True, False])
        rid = self.app.in_system(SYSTEM, lambda tx: [r["request_id"] for r in fix_requests.open_requests(tx, "CL-2")
                                                      if r["summary"] == "試験"][0])
        self.app.in_system(SYSTEM, lambda tx: fix_requests.report_fixed(tx, rid, "試験：直した"))
        self.assertEqual(ask(), True)                                    # 直したあとにまた起きた：新しく出す

    def test_nothing_is_written_when_the_drive_is_not_attached(self):
        s = World().setup()
        (self.root / backup.DRIVE_MARK).unlink()
        self.assertEqual([r.ok for r in self.run_contractor(s["tenant"])], [False])
        self.assertEqual([r.ok for r in self.run_whole()], [False])
        self.assertFalse((self.root / backup.BASE).exists())


if __name__ == "__main__":
    unittest.main()
