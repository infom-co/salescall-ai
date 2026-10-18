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

from salescall.core import business_settings, dnc, importer, next_action, settings, tasks
from salescall.core.access import Rejected
from salescall.core.import_files import read
from salescall.core.phone import normalize
from tests.support import OPERATOR, World, ensure_rules, fictional_jp_fixed, owner_access
from tests.test_import_files import xlsx

MAP = {"phone": "電話番号", "client_ref": "管理番号", "name": "会社名"}


def ai(texts):
    """試験の AI の読み（架空。どの名前にも同じ読みを、決まった読みとして返す）。"""
    return {t: {"reading": "てすとがいしゃ", "candidates": [], "likely": None} for t in texts}


def undecided(texts):
    """試験の AI の読み（架空。どの名前も読みが決まらず、候補を返す）。"""
    return {t: {"reading": "", "candidates": ["なかじま", "なかしま"], "likely": "なかじま"} for t in texts}


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
        ensure_rules(self.w, self.s)

    def tx(self, work, actor=OPERATOR, s=None):
        s = s or self.s
        return self.w.a.in_client(actor, s["tenant"], s["client"], work)

    def preview(self, data, mapping=MAP, name="一覧.csv", run=None, **kw):
        sheet = read(name, data, **kw)
        return sheet, self.tx(lambda tx: importer.preview(tx, run or self.s["run"], sheet, mapping, suggest_readings=ai))

    def register(self, data, mapping=MAP, name="一覧.csv", run=None, **kw):
        sheet, plan = self.preview(data, mapping, name, run, **kw)
        return self.tx(lambda tx: importer.register(tx, run or self.s["run"], sheet, mapping, expected_digest=plan.digest,
                                                    ai_readings=plan.ai_readings))

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
        self.tx(lambda tx: dnc.register(tx, dnc_number, "manual"))
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


    def test_csv_column_with_separators_or_labels_adds_nothing(self):
        """区切りや言葉のある欄は文字として打ったもの。0 が消えた証拠にならないので、0 を足さない（2026-10-16 の見直し I13）。"""
        _, plan = self.preview(csv_file([("A", "1", "3-0000-9031"), ("B", "2", "TEL:300009032")]))
        self.assertEqual(self.outcomes(plan), [(2, "stopped", "number_unknown"), (3, "stopped", "number_unknown")])

class SameAsPreview(Base):
    def test_changes_after_preview_write_nothing(self):
        x = fictional_jp_fixed()
        data = csv_file([("A", "1", x), ("B", "2", fictional_jp_fixed())])
        sheet, plan = self.preview(data)
        self.tx(lambda tx: dnc.register(tx, x, "refused_in_call"))   # プレビューのあとで発信禁止に入った
        with self.assertRaises(importer.PlanChanged):
            self.tx(lambda tx: importer.register(tx, self.s["run"], sheet, MAP, expected_digest=plan.digest, ai_readings=plan.ai_readings))
        self.assertEqual(self.tasks(), [])
        self.assertEqual(self.tx(lambda tx: tx.one("SELECT count(*) AS n FROM salescall.imports"))["n"], 0)

    def test_a_different_file_or_mapping_is_a_different_preview(self):
        data = csv_file([("A", "1", fictional_jp_fixed())])
        sheet, plan = self.preview(data)
        with self.assertRaises(importer.PlanChanged):
            self.tx(lambda tx: importer.register(tx, self.s["run"], sheet, {"phone": "電話番号"},
                                                 expected_digest=plan.digest, ai_readings=plan.ai_readings))


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
                self.tx(lambda tx: importer.register(tx, self.s["run"], sheet, MAP, expected_digest=plan.digest, ai_readings=plan.ai_readings))
        self.assertEqual(self.tasks(), [])
        self.assertEqual(self.tx(lambda tx: tx.one("SELECT count(*) AS n FROM salescall.contacts"))["n"], 1)  # setup の分だけ

    def test_concurrent_registrations_of_the_same_file_register_once(self):
        data = csv_file([(f"P-{i}", f"架空の会社{i}", fictional_jp_fixed()) for i in range(8)])
        sheet, plan = self.preview(data)
        results, errors = [], []

        def go():
            try:
                results.append(self.tx(lambda tx: importer.register(tx, self.s["run"], sheet, MAP,
                                                                    expected_digest=plan.digest, ai_readings=plan.ai_readings)))
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



class Targets(Base):
    """目的のお客様の名前・読み・役職（Intent.md 6-2・6-8・7-1。DESIGN.md 3-4）。要るかは業務設定の版の1か所で決まる。"""
    HEADER = ("管理番号", "会社名", "電話番号", "担当者名", "担当者名の読み", "役職")
    MAP = {"phone": "電話番号", "client_ref": "管理番号", "name": "会社名", "person_name": "担当者名",
           "person_name_reading": "担当者名の読み", "person_role": "役職"}

    def run_with(self, target_person):
        """目的のお客様の種類を決めた版と、その版の実施回を作る。"""
        from tests.support import full_version
        vid = full_version(self.w, self.s, target_person)
        return vid, self.w.run(self.s["tenant"], self.s["client"], self.s["business"], vid)

    def test_a_business_that_calls_by_name_needs_the_name_and_its_reading(self):
        _, run = self.run_with({"kind": "name", "role": None})
        with self.assertRaises(importer.ImportRejected):                  # 名前の列を選んでいなければ始めない
            self.preview(csv_file([("A", "架空商事", fictional_jp_fixed())]), run=run)
        data = csv_file([("A", "架空商事", fictional_jp_fixed(), "岡田", "おかだ", "部長"),
                         ("B", "架空工業", fictional_jp_fixed(), None, None, "部長"),
                         ("C", "架空物産", fictional_jp_fixed(), "東海林", "東海林", None)], header=self.HEADER)
        r = self.register(data, mapping=self.MAP, run=run)
        self.assertEqual(self.outcomes(r), [(2, "registered", None), (3, "stopped", "target_name_missing"),
                                            (4, "stopped", "reading_invalid")])    # 読みが漢字のまま＝読みとして使えない
        task = r["rows"][0]["task_id"]
        got = self.tx(lambda tx: business_settings.target_of(tx, task))
        self.assertEqual(got, {"kind": "name", "role": None,
                               "person": {"text": "岡田", "reading": "おかだ", "reading_by": "given", "candidates": [],
                                          "likely": None},
                               "company": {"text": "架空商事", "reading": "てすとがいしゃ", "reading_by": "ai",
                                           "candidates": [], "likely": None}})   # 会社名の読みは AI が決めた読み

    def test_a_business_that_calls_by_role_needs_the_role_unless_the_business_names_it(self):
        _, run = self.run_with({"kind": "role", "role": None})
        data = csv_file([("A", "架空商事", fictional_jp_fixed(), None, None, "院長"),
                         ("B", "架空工業", fictional_jp_fixed(), None, None, None)], header=self.HEADER)
        r = self.register(data, mapping=self.MAP, run=run)
        self.assertEqual(self.outcomes(r), [(2, "registered", None), (3, "stopped", "target_role_missing")])
        self.assertEqual(self.tx(lambda tx: business_settings.target_of(tx, r["rows"][0]["task_id"]))["role"], "院長")
        _, run2 = self.run_with({"kind": "role", "role": "社長"})              # 業務で役職を1つ書いていれば、一覧の列は要らない
        r = self.register(csv_file([("C", "架空物産", fictional_jp_fixed())]), run=run2)
        self.assertEqual(self.outcomes(r), [(2, "registered", None)])
        self.assertEqual(self.tx(lambda tx: business_settings.target_of(tx, r["rows"][0]["task_id"]))["role"], "社長")

    def test_in_verification_mode_the_contractor_confirms_ai_readings(self):
        """検証モード：AI の読みの案は、契約者が確かめるまで登録しない。確かめた（直した）読みで登録する。"""
        _, run = self.run_with({"kind": "name", "role": None})
        self.tx(lambda tx: settings.set_value(tx, "approval.human", True, scope="business", status="decided",
                                              rationale="試験：検証モード", review_when="試験", tenant_id=self.s["tenant"],
                                              client_id=self.s["client"], business_id=self.s["business"]))
        data = csv_file([("A", "架空商事", fictional_jp_fixed(), "岡田", None, None)], header=self.HEADER)
        sheet, plan = self.preview(data, mapping=self.MAP, run=run)
        self.assertEqual(self.outcomes(plan), [(2, "stopped", "reading_unconfirmed")])
        self.assertEqual({c["text"] for c in plan.to_confirm}, {"架空商事", "岡田"})   # 確かめる名前（AI の案と行番号つき）
        fixed = [("架空商事", "かくうしょうじ"), ("岡田", "おかだ")]
        plan = self.tx(lambda tx: importer.preview(tx, run, sheet, self.MAP, confirmed_names=fixed, suggest_readings=ai))
        self.assertEqual(self.outcomes(plan), [(2, "registered", None)])
        r = self.tx(lambda tx: importer.register(tx, run, sheet, self.MAP, expected_digest=plan.digest, confirmed_names=fixed,
                                                 ai_readings=plan.ai_readings))
        row = self.tx(lambda tx: tx.one("SELECT source_name_reading, source_name_reading_by, target_name_reading,"
                                        " target_name_reading_by FROM salescall.tasks WHERE task_id = %s",
                                        (r["rows"][0]["task_id"],)))
        self.assertEqual(dict(row), {"source_name_reading": "かくうしょうじ", "source_name_reading_by": "person",
                                     "target_name_reading": "おかだ", "target_name_reading_by": "person"})

    def test_a_confirmed_reading_does_not_overwrite_a_reading_in_the_file(self):
        """同じ漢字でも行ごとに読みが違いうる（東海林）。契約者が確かめるのは AI の案だけなので、一覧に書いてある読みは上書きしない。"""
        _, run = self.run_with({"kind": "name", "role": None})
        data = csv_file([("A", "架空商事", fictional_jp_fixed(), "東海林", "しょうじ", None),
                         ("B", "架空工業", fictional_jp_fixed(), "東海林", None, None)], header=self.HEADER)
        sheet = read("一覧.csv", data)
        fixed = [("東海林", "とうかいりん")]
        plan = self.tx(lambda tx: importer.preview(tx, run, sheet, self.MAP, confirmed_names=fixed, suggest_readings=ai))
        got = {r.row_no: (r.person_reading.reading, r.person_reading.by) for r in plan.rows}
        self.assertEqual(got, {2: ("しょうじ", "given"), 3: ("とうかいりん", "person")})

    def test_the_ai_is_asked_once(self):
        """AI に聞くのはプレビューの1回だけ。登録はプレビューの答えを使う（2回目の答えが違っても、見たとおりに入る）。"""
        answers = iter(["かくうしょうじ", "ちがうよみ"])
        asked = []

        def changing(texts):
            asked.append(list(texts))
            return {t: {"reading": next(answers)} for t in texts}
        data = csv_file([("A", "架空商事", fictional_jp_fixed())])
        sheet = read("一覧.csv", data)
        plan = self.tx(lambda tx: importer.preview(tx, self.s["run"], sheet, MAP, suggest_readings=changing))
        self.tx(lambda tx: importer.register(tx, self.s["run"], sheet, MAP, expected_digest=plan.digest,
                                             ai_readings=plan.ai_readings))
        self.assertEqual(len(asked), 1)
        self.assertEqual(self.tx(lambda tx: tx.one("SELECT source_name_reading FROM salescall.tasks WHERE source_row = 2"
                                                   " AND run_id = %s", (self.s["run"],)))["source_name_reading"],
                         "かくうしょうじ")

    def test_a_version_that_needs_names_is_not_assigned_to_rows_without_them(self):
        """版の割り当ても、同じ決まりで確かめる（目的のお客様を呼べない行を残したまま、版を替えない）。"""
        self.register(csv_file([("A", "架空商事", fictional_jp_fixed())]))          # 名前の列の無い一覧（誰でもの版）
        vid, _ = self.run_with({"kind": "name", "role": None})
        with self.assertRaises(business_settings.SettingsInvalid) as e:
            self.tx(lambda tx: business_settings.assign(tx, self.s["run"], vid, reason="試験：名前を使う版へ"))
        self.assertIn("2 行に、その列がありません", e.exception.problems[0])   # setup の行と取り込んだ行

    def test_an_undecided_reading_is_registered_and_confirmed_in_the_call(self):
        """読みが決まらない名前でも止めない（2026-10-16 ユーザー：会話の中で確かめる）。考えられる読みとまず言ってみる読みを持って
        登録し、会話で確かめた読みがあれば、次からはそれを決まった読みとして使う（CL-1 の連絡帳 4-15）。"""
        vid, run = self.run_with({"kind": "name", "role": None})
        data = csv_file([("A", "架空商事", fictional_jp_fixed(), "中島", None, None)], header=self.HEADER)
        sheet = read("一覧.csv", data)
        plan = self.tx(lambda tx: importer.preview(tx, run, sheet, self.MAP, suggest_readings=undecided))
        self.assertEqual(self.outcomes(plan), [(2, "registered", None)])
        r = self.tx(lambda tx: importer.register(tx, run, sheet, self.MAP, expected_digest=plan.digest,
                                                 ai_readings=plan.ai_readings))
        task = r["rows"][0]["task_id"]
        got = self.tx(lambda tx: business_settings.target_of(tx, task))["person"]
        self.assertEqual(got, {"text": "中島", "reading": None, "reading_by": None, "candidates": ["なかじま", "なかしま"],
                               "likely": "なかじま"})
        aid = self.w.insert_claim({**self.s, "task": task,
                                   "contact": self.tx(lambda tx: tasks.list_number(tx, task))["contact_id"],
                                   "phone": self.tx(lambda tx: tasks.list_number(tx, task))["phone_e164"]}, version=vid)
        self.tx(lambda tx: tasks.record_confirmed_reading(tx, task, "target", "ナカシマ", attempt_id=aid))
        got = self.tx(lambda tx: business_settings.target_of(tx, task))["person"]
        self.assertEqual((got["reading"], got["reading_by"]), ("なかしま", "conversation"))
        with self.assertRaises(Rejected):                                 # ほかの行の通話で確かめた読みは付けない
            self.tx(lambda tx: tasks.record_confirmed_reading(tx, self.s["task"], "target", "なかしま", attempt_id=aid))

    def test_the_database_keeps_readings_in_shape(self):
        """読みと読みを決めたものは組・決まった読みに候補を付けない・読みはひらがな・名前の無い読みは無い。取込の処理が誤っても、
        データベースが拒む。"""
        cid = self.tx(lambda tx: tx.one("SELECT contact_id FROM salescall.contacts LIMIT 1"))["contact_id"]
        _, run = self.run_with(None)
        for cols, vals in ((("target_name", "target_name_reading"), ("岡田", "おかだ")),
                           (("target_name", "target_name_reading", "target_name_reading_by"), ("岡田", "オカダ", "given")),
                           (("target_name", "target_name_reading", "target_name_reading_by", "target_name_reading_likely"),
                            ("岡田", "おかだ", "given", "おかだ")),
                           (("source_name_reading_candidates",), (["なかじま"],)),
                           (("source_name", "source_name_reading_likely"), ("中島", "中島"))):
            with self.subTest(cols=cols), self.assertRaises(Rejected) as e:
                self.tx(lambda tx: tx.execute(
                    f"INSERT INTO salescall.tasks (tenant_id, client_id, run_id, contact_id, {', '.join(cols)})"
                    f" VALUES (%s, %s, %s, %s, {', '.join(['%s'] * len(cols))})",
                    (self.s["tenant"], self.s["client"], run, cid, *vals)))
            self.assertEqual(e.exception.constraint, "task_name_readings")

    def test_a_reading_column_alone_is_refused(self):
        with self.assertRaises(importer.ImportRejected):
            self.preview(csv_file([("A", "架空商事", fictional_jp_fixed())]),
                         mapping={"phone": "電話番号", "name_reading": "会社名"})

if __name__ == "__main__":
    unittest.main()
