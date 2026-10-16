"""発信対象の取込の試験（DESIGN.md 3-4・Intent.md 7-2）。

- 行の合否はプレビューと登録で同じ1つの判断。登録は、契約者が見たプレビューと同じときだけ書く。
- 合格の行だけを1つの取引で全部登録（途中で失敗したら何も残らない）。止めた行は行番号と理由だけを残す。
⚠️ 中身は架空のもの（番号は局番のあとが 0000 の、実在しない番号。会社名も架空）。
"""

from __future__ import annotations

import csv
import io
import threading
import unittest
from unittest import mock

from salescall.core import dnc, importer, next_action
from salescall.core.access import Rejected
from salescall.core.import_files import read
from salescall.core.phone import normalize
from tests.support import OPERATOR, World, fictional_jp_fixed, owner_access
from tests.test_import_files import xlsx

MAP = {"phone": "電話番号", "client_ref": "管理番号", "name": "会社名"}


def csv_file(rows, header=("管理番号", "会社名", "電話番号")) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(header)
    w.writerows(rows)
    return b"\xef\xbb\xbf" + buf.getvalue().encode("utf-8")


class Base(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.s = self.w.setup()

    def tx(self, work, actor=OPERATOR, s=None):
        s = s or self.s
        return self.w.a.in_client(actor, s["tenant"], s["client"], work)

    def preview(self, data, mapping=MAP, name="一覧.csv", run=None, **kw):
        sheet = read(name, data, **kw)
        return sheet, self.tx(lambda tx: importer.preview(tx, run or self.s["run"], sheet, mapping))

    def register(self, data, mapping=MAP, name="一覧.csv", run=None, **kw):
        sheet, plan = self.preview(data, mapping, name, run, **kw)
        return self.tx(lambda tx: importer.register(tx, run or self.s["run"], sheet, mapping, expected_digest=plan.digest))

    def outcomes(self, plan_or_result):
        rows = plan_or_result.rows if hasattr(plan_or_result, "rows") else plan_or_result["rows"]
        get = (lambda r: (r.row_no, r.outcome, r.reason_code)) if hasattr(plan_or_result, "rows") else \
              (lambda r: (r["row_no"], r["outcome"], r["reason_code"]))
        return [get(r) for r in rows]

    def tasks(self):
        return self.tx(lambda tx: tx.all(
            "SELECT t.client_ref, t.source_name, t.source_phone, t.source_row, c.phone_e164, t.import_id"
            " FROM salescall.tasks t JOIN salescall.contacts c USING (contact_id)"
            " WHERE t.run_id = %s AND t.import_id IS NOT NULL ORDER BY t.source_row", (self.s["run"],)))


class Registers(Base):
    def test_good_rows_become_tasks_with_the_clients_keys_and_a_first_schedule(self):
        p1, p2 = fictional_jp_fixed(), fictional_jp_fixed()
        r = self.register(csv_file([("A-1", "架空商事", p1), ("A-2", "架空工業", "TEL:" + p2)]))
        self.assertEqual(r["counts"], {"registered": 2, "stopped": 0, "already": 0, "empty": 0})
        got = self.tasks()
        self.assertEqual([(t["client_ref"], t["source_name"], t["source_phone"], t["source_row"], t["phone_e164"])
                          for t in got],
                         [("A-1", "架空商事", p1, 2, normalize(p1).e164),
                          ("A-2", "架空工業", "TEL:" + p2, 3, normalize(p2).e164)])
        self.assertEqual({t["import_id"] for t in got}, {r["import_id"]})
        for row in r["rows"]:
            na = self.tx(lambda tx: next_action.current(tx, row["task_id"]))
            self.assertEqual((na["kind"], na["cause"]), ("call", "取込"))
        rec = self.tx(lambda tx: tx.all("SELECT row_no, outcome FROM salescall.import_rows WHERE import_id = %s"
                                        " ORDER BY row_no", (r["import_id"],)))
        self.assertEqual([(x["row_no"], x["outcome"]) for x in rec], [(2, "registered"), (3, "registered")])

    def test_client_ref_column_is_optional(self):
        r = self.register(csv_file([("架空商事", fictional_jp_fixed())], header=("会社名", "電話番号")),
                          mapping={"phone": "電話番号", "name": "会社名"})
        self.assertEqual(r["counts"]["registered"], 1)
        self.assertIsNone(self.tasks()[0]["client_ref"])

    def test_reimporting_the_same_file_registers_nothing_new(self):
        data = csv_file([("A-1", "架空商事", fictional_jp_fixed()), ("A-2", "架空工業", fictional_jp_fixed())])
        self.register(data)
        again = self.register(data)
        self.assertEqual(self.outcomes(again), [(2, "already", "already_in_run"), (3, "already", "already_in_run")])
        self.assertEqual(len(self.tasks()), 2)
        self.assertTrue(all(x["task_id"] for x in again["rows"]))   # 登録済みの行は、もうあるタスクを指す


class StopsRows(Base):
    def test_each_reason(self):
        dup, dnc_number = fictional_jp_fixed(), fictional_jp_fixed()
        self.tx(lambda tx: dnc.register(tx, dnc_number, "client_list"))
        rows = [
            ("R-2", "番号が不明", "03-0000-0001 内線12"),
            ("R-3", "FAX", "FAX 03-0000-0002"),
            ("R-4", "緊急通報", "110"),
            ("R-5", "ナビダイヤル", "0570-000-001"),
            ("R-6", "発信禁止", dnc_number),
            ("R-7", "合格", dup),
            ("R-8", "同じ番号（書き方違い）", dup.replace("-", "")),
            ("R-7", "同じ管理番号", fictional_jp_fixed()),
            ("", "管理番号なし", fictional_jp_fixed()),
            ("R-11", "番号なし", ""),
            ("R-12", "フリーダイヤル", "0120-000-001"),
            ("1.23457E+15", "管理番号の桁落ち", fictional_jp_fixed()),
        ]
        _, plan = self.preview(csv_file(rows))
        self.assertEqual(self.outcomes(plan), [
            (2, "stopped", "number_unknown"), (3, "stopped", "number_unknown"), (4, "stopped", "number_kind_forbidden"),
            (5, "stopped", "number_kind_not_allowed"), (6, "stopped", "dnc_client"), (7, "registered", None),
            (8, "stopped", "duplicate_number_in_file"), (9, "stopped", "duplicate_ref_in_file"),
            (10, "stopped", "ref_missing"), (11, "stopped", "phone_missing"), (12, "registered", None),
            (13, "stopped", "ref_digits_lost")])
        self.assertIn("行 7", plan.rows[6].reason)
        self.assertIn("行 7", plan.rows[7].reason)

    def test_a_stopped_row_does_not_take_its_number(self):
        """止めた行は番号・管理番号を取らない（止めた行のせいで、ほかの行を止めない）。"""
        x, y = fictional_jp_fixed(), fictional_jp_fixed()
        _, plan = self.preview(csv_file([("A", "1", x), ("A", "2", y), ("B", "3", y)]))
        self.assertEqual(self.outcomes(plan), [(2, "registered", None), (3, "stopped", "duplicate_ref_in_file"),
                                               (4, "registered", None)])

    def test_conflicts_with_the_run(self):
        x, y = fictional_jp_fixed(), fictional_jp_fixed()
        self.register(csv_file([("A", "1", x), ("B", "2", y)]))
        _, plan = self.preview(csv_file([("C", "1", x), ("B", "2", fictional_jp_fixed())]))
        self.assertEqual(self.outcomes(plan), [(2, "stopped", "number_in_run_other_ref"), (3, "stopped", "ref_in_run")])

    def test_shifted_columns_are_stopped(self):
        """値の中の「,」が囲まれていない行は、列がずれて別の列の番号を読む恐れがあるので止める。"""
        data = ("管理番号,会社名,電話番号\nA,架空商事,東京支店," + fictional_jp_fixed() + "\n").encode("utf-8-sig")
        _, plan = self.preview(data)
        self.assertEqual(self.outcomes(plan), [(2, "stopped", "columns_mismatch")])

    def test_empty_rows_are_kept_as_empty(self):
        data = csv_file([("A", "1", fictional_jp_fixed()), (), ("B", "2", fictional_jp_fixed())])
        _, plan = self.preview(data)
        self.assertEqual(self.outcomes(plan), [(2, "registered", None), (3, "empty", None), (4, "registered", None)])

    def test_cell_problems_in_xlsx_stop_the_row(self):
        data = xlsx([["管理番号", "会社名", "電話番号"], ["A", "架空商事", "#N/A"], ["B", "=A1", fictional_jp_fixed()]])
        _, plan = self.preview(data, name="一覧.xlsx")
        self.assertEqual(self.outcomes(plan), [(2, "stopped", "cell_problem"), (3, "stopped", "cell_problem")])
        self.assertIn("会社名", plan.rows[1].reason)


class LeadingZero(Base):
    """表計算ソフトで先頭の 0 が消えた番号（DESIGN.md 3-4 の4）：1つの値だけを見て 0 を足さない。"""

    def test_xlsx_numeric_cells_are_proof(self):
        data = xlsx([["管理番号", "会社名", "電話番号"], ["A", "架空商事", 300009001], ["B", "架空工業", "03-0000-9002"],
                     ["C", "架空物産", "300009003"]])
        _, plan = self.preview(data, name="一覧.xlsx")
        self.assertEqual(self.outcomes(plan), [(2, "registered", None), (3, "registered", None),
                                               (4, "stopped", "number_unknown")])   # 文字の欄は証拠にならない
        self.assertEqual((plan.rows[0].number.national, plan.rows[0].number.zero_added), ("0300009001", True))

    def test_csv_column_where_every_number_lost_its_zero(self):
        r = self.register(csv_file([("A", "1", "300009011"), ("B", "2", "9000009012")]))
        self.assertEqual(r["counts"]["registered"], 2)
        added = self.tx(lambda tx: tx.all("SELECT zero_added FROM salescall.import_rows WHERE import_id = %s",
                                          (r["import_id"],)))
        self.assertEqual([x["zero_added"] for x in added], [True, True])
        self.assertEqual([t["phone_e164"] for t in self.tasks()], ["+81300009011", "+819000009012"])

    def test_csv_mixed_column_adds_nothing(self):
        _, plan = self.preview(csv_file([("A", "1", "300009021"), ("B", "2", "03-0000-9022")]))
        self.assertEqual(self.outcomes(plan), [(2, "stopped", "number_unknown"), (3, "registered", None)])


class SameAsPreview(Base):
    def test_changes_after_preview_write_nothing(self):
        x = fictional_jp_fixed()
        data = csv_file([("A", "1", x), ("B", "2", fictional_jp_fixed())])
        sheet, plan = self.preview(data)
        self.tx(lambda tx: dnc.register(tx, x, "refused_in_call"))   # プレビューのあとで発信禁止に入った
        with self.assertRaises(importer.PlanChanged):
            self.tx(lambda tx: importer.register(tx, self.s["run"], sheet, MAP, expected_digest=plan.digest))
        self.assertEqual(self.tasks(), [])
        self.assertEqual(self.tx(lambda tx: tx.one("SELECT count(*) AS n FROM salescall.imports"))["n"], 0)

    def test_a_different_file_or_mapping_is_a_different_preview(self):
        data = csv_file([("A", "1", fictional_jp_fixed())])
        sheet, plan = self.preview(data)
        with self.assertRaises(importer.PlanChanged):
            self.tx(lambda tx: importer.register(tx, self.s["run"], sheet, {"phone": "電話番号"},
                                                 expected_digest=plan.digest))


class AllOrNothing(Base):
    def test_a_failure_in_the_middle_leaves_nothing(self):
        data = csv_file([("A", "1", fictional_jp_fixed()), ("B", "2", fictional_jp_fixed()),
                         ("C", "3", fictional_jp_fixed())])
        sheet, plan = self.preview(data)
        real = next_action.schedule_now
        calls = []

        def fail_on_second(tx, task_id, *, cause):
            calls.append(task_id)
            if len(calls) == 2:
                raise RuntimeError("試験：2行目で保存に失敗")
            return real(tx, task_id, cause=cause)

        with mock.patch.object(next_action, "schedule_now", fail_on_second):
            with self.assertRaises(RuntimeError):
                self.tx(lambda tx: importer.register(tx, self.s["run"], sheet, MAP, expected_digest=plan.digest))
        self.assertEqual(self.tasks(), [])
        self.assertEqual(self.tx(lambda tx: tx.one("SELECT count(*) AS n FROM salescall.contacts"))["n"], 1)  # setup の分だけ

    def test_concurrent_registrations_of_the_same_file_register_once(self):
        data = csv_file([(f"P-{i}", f"架空の会社{i}", fictional_jp_fixed()) for i in range(8)])
        sheet, plan = self.preview(data)
        results, errors = [], []

        def go():
            try:
                results.append(self.tx(lambda tx: importer.register(tx, self.s["run"], sheet, MAP,
                                                                    expected_digest=plan.digest)))
            except importer.PlanChanged as e:
                errors.append(e)

        threads = [threading.Thread(target=go) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual((len(results), len(errors)), (1, 3))   # 先に入った1つだけ。ほかは「プレビューと違う」
        self.assertEqual(len(self.tasks()), 8)


class Rejects(Base):
    def test_bad_mappings(self):
        data = csv_file([("A", "1", fictional_jp_fixed())])
        for mapping in ({"client_ref": "管理番号"}, {"phone": "無い見出し"}, {"phone": "電話番号", "memo": "会社名"},
                        {"phone": "電話番号", "name": "電話番号"}):
            with self.subTest(mapping=mapping):
                with self.assertRaises(importer.ImportRejected):
                    self.preview(data, mapping)
        dup_header = csv_file([("A", "1", fictional_jp_fixed())], header=("電話番号", "会社名", "電話番号"))
        with self.assertRaises(importer.ImportRejected):
            self.preview(dup_header, {"phone": "電話番号"})

    def test_another_clients_run_is_invisible(self):
        other = self.w.setup(tenant=self.s["tenant"])
        data = csv_file([("A", "1", fictional_jp_fixed())])
        with self.assertRaises(importer.ImportRejected):
            self.preview(data, run=other["run"])


class DatabaseGuards(Base):
    """取込の処理の誤りがあっても、データベースが守る（2重）。"""

    def test_one_task_per_client_ref_in_a_run(self):
        sql = ("INSERT INTO salescall.tasks (tenant_id, client_id, run_id, contact_id, client_ref)"
               " VALUES (%s, %s, %s, %s, 'K-1')")
        first, second = (self.w.contact(self.s["tenant"], self.s["client"], normalize(fictional_jp_fixed()).e164)
                         for _ in range(2))
        self.tx(lambda tx: tx.execute(sql, (self.s["tenant"], self.s["client"], self.s["run"], first)))
        with self.assertRaises(Rejected) as cm:
            self.tx(lambda tx: tx.execute(sql, (self.s["tenant"], self.s["client"], self.s["run"], second)))
        self.assertEqual(cm.exception.constraint, "tasks_one_ref_per_run")

    def test_import_records_cannot_be_changed_even_by_the_owner(self):
        r = self.register(csv_file([("A", "1", fictional_jp_fixed())]))
        for sql in ("UPDATE salescall.import_rows SET outcome = 'stopped' WHERE import_id = %s",
                    "DELETE FROM salescall.import_rows WHERE import_id = %s",
                    "UPDATE salescall.imports SET file_name = 'x' WHERE import_id = %s"):
            with self.subTest(sql=sql), self.assertRaises(Rejected):
                owner_access().in_system(OPERATOR, lambda tx: tx.execute(sql, (r["import_id"],)))

    def test_other_clients_cannot_see_import_records(self):
        self.register(csv_file([("A", "1", fictional_jp_fixed())]))
        other = self.w.setup(tenant=self.s["tenant"])
        n = self.tx(lambda tx: tx.one("SELECT (SELECT count(*) FROM salescall.imports)"
                                      " + (SELECT count(*) FROM salescall.import_rows) AS n"), s=other)["n"]
        self.assertEqual(n, 0)


if __name__ == "__main__":
    unittest.main()
