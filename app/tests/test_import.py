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
from datetime import timedelta
from unittest import mock

from salescall.core import business_settings, conversation_io, customer_contacts, dnc, importer, next_action, row_fixes, settings, tasks
from salescall.core.access import Rejected
from salescall.core.import_files import read
from salescall.core.phone import normalize
from tests.support import OPERATOR, SYSTEM, World, ensure_rules, fictional_jp_fixed, owner_access
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



class CustomerContacts(Base):
    """お客様リストの番号と連絡先（2026-11-05 ユーザー。0175・core/customer_contacts.py）。かける番号は、お客様が指定した電話番号が
    あればその番号、無ければ会社の番号。ほかの番号は記録だけ（そのお客様が断ったら止める範囲には入る）。番号ではない連絡先は
    記録を足していく。"""
    HEADER = ("管理番号", "会社名", "電話番号", "指定の番号", "携帯", "メール", "ファクス", "住所", "LINE", "面談ID", "面談サービス")
    MAP = {"phone": "電話番号", "client_ref": "管理番号", "name": "会社名", "designated_phone": "指定の番号", "mobile": "携帯",
           "email": "メール", "fax": "ファクス", "address": "住所", "line_id": "LINE", "meeting_id": "面談ID",
           "meeting_service": "面談サービス"}

    def numbers(self, task_id):
        return self.tx(lambda tx: [(r["phone_e164"], r["source"], r["label"]) for r in tx.all(
            "SELECT phone_e164, source, label FROM salescall.task_numbers WHERE task_id = %s ORDER BY source, phone_e164",
            (task_id,))])

    def test_the_designated_number_is_dialed_and_the_others_are_only_recorded(self):
        company, mobile, mobile2, fax = fictional_jp_fixed(), "090-0000-0101", "090-0000-0102", fictional_jp_fixed()
        rows = [("A", "架空商事", company, mobile, None, "a@example.com", fax, "東京都千代田区", "abc_123", "123 456", "Zoom"),
                ("B", "架空工業", fictional_jp_fixed(), None, mobile2, None, None, None, None, None, None),
                ("C", "架空物産", None, "090-0000-0103", None, None, None, None, None, None, None),
                ("D", "架空銀行", None, None, "090-0000-0104", None, None, None, None, None, None),
                ("E", "架空電機", fictional_jp_fixed(), None, None, "a@b", None, None, None, None, None),
                ("F", "架空建設", fictional_jp_fixed(), None, "090-0000", None, None, None, None, None, None),
                ("G", "架空運輸", fictional_jp_fixed(), None, None, None, None, None, None, None, "Zoom"),
                ("H", "架空食品", fictional_jp_fixed(), None, None, None, None, None, None, "999", None)]
        r = self.register(csv_file(rows, header=self.HEADER), mapping=self.MAP)
        self.assertEqual(self.outcomes(r), [(2, "registered", None), (3, "registered", None), (4, "registered", None),
                                            (5, "stopped", "phone_missing"), (6, "stopped", "contact_invalid"),
                                            (7, "stopped", "mobile_unknown"), (8, "stopped", "contact_invalid"),
                                            (9, "stopped", "contact_invalid")])
        a, b, c = (r["rows"][i]["task_id"] for i in range(3))
        e = normalize
        self.assertEqual(self.numbers(a), sorted([(e(mobile).e164, "list", "designated"), (e(company).e164, "record", "company")],
                                                 key=lambda x: (x[1], x[0])))
        self.assertEqual(self.numbers(b), sorted([(e(rows[1][2]).e164, "list", "company"), (e(mobile2).e164, "record", "mobile")],
                                                 key=lambda x: (x[1], x[0])))
        self.assertEqual(self.numbers(c), [(e("090-0000-0103").e164, "list", "designated")])
        self.assertEqual(self.tx(lambda tx: tasks.dial_number(tx, a))["phone_e164"], e(mobile).e164)   # かけるのは指定の番号
        got = self.tx(lambda tx: customer_contacts.now(tx, a))["contacts"]
        self.assertEqual({(x["kind"], x["value"], x["service"], x["origin"]) for x in got},
                         {("email", "a@example.com", None, "list"), ("fax", fax, None, "list"),
                          ("address", "東京都千代田区", None, "list"), ("line_id", "abc_123", None, "list"),
                          ("meeting", "123 456", "Zoom", "list")})
        self.assertEqual(self.tx(lambda tx: customer_contacts.now(tx, b))["contacts"], [])
        # 会話の側が重ねて伺わないために読む形（役目の名前。CL-1 の連絡帳 4-92 の (5)）：指定の番号が携帯電話なら、それが携帯電話の番号
        self.assertEqual(self.tx(lambda tx: conversation_io.known_contacts(tx, a)),
                         {"email": "a@example.com", "fax": fax, "address": "東京都千代田区", "line": "abc_123", "mobile": e(mobile).e164})
        self.assertEqual(self.tx(lambda tx: conversation_io.known_contacts(tx, b)), {"mobile": e(mobile2).e164})
        self.assertEqual(self.tx(lambda tx: conversation_io.known_contacts(tx, c)), {"mobile": e("090-0000-0103").e164})

    def test_the_same_number_twice_is_one_number(self):
        """指定の番号と携帯が同じなら、番号は1つ（かける番号として）。"""
        mobile = "090-0000-0111"
        r = self.register(csv_file([("A", "架空商事", fictional_jp_fixed(), mobile, mobile, None, None, None, None, None, None)],
                                   header=self.HEADER), mapping=self.MAP)
        self.assertEqual([x[1:] for x in self.numbers(r["rows"][0]["task_id"])], [("list", "designated"), ("record", "company")])

    def test_a_customer_who_refused_on_a_recorded_number_is_not_registered(self):
        """記録だけの番号でも、そのお客様が断っていれば（発信禁止）、その行は登録しない（どの番号でかけても同じお客様＝原則14）。"""
        mobile = "090-0000-0121"
        self.tx(lambda tx: dnc.register(tx, mobile, "manual"))
        r = self.register(csv_file([("A", "架空商事", fictional_jp_fixed(), None, mobile, None, None, None, None, None, None)],
                                   header=self.HEADER), mapping=self.MAP)
        self.assertEqual(self.outcomes(r), [(2, "stopped", "dnc_client")])
        self.assertIn("「携帯」の番号", r["rows"][0]["reason"])

    def test_a_recorded_number_is_never_dialed(self):
        """記録だけの番号は、次の予定にも試行にもならない（処理に誤りがあってもデータベースが断る）。"""
        r = self.register(csv_file([("A", "架空商事", fictional_jp_fixed(), None, "090-0000-0131", None, None, None, None, None,
                                     None)], header=self.HEADER), mapping=self.MAP)
        task = r["rows"][0]["task_id"]
        record = self.tx(lambda tx: tx.one("SELECT contact_id FROM salescall.task_numbers WHERE task_id = %s AND source = 'record'",
                                           (task,)))["contact_id"]
        with self.assertRaises(Rejected) as cm:
            self.tx(lambda tx: next_action._append(tx, task, call_at=None, end_reason=None, cause="試験", call_now=True,
                                                   promised=False, target=next_action.Target(record)))
        self.assertEqual(cm.exception.constraint, "dial_only_dialable_numbers")

    def test_a_recorded_number_cannot_be_claimed_for_a_call(self):
        """記録だけの番号へは、試行（予約）も作れない（データベースの守り。門に誤りがあっても）。"""
        n = normalize("090-0000-0151")
        cid = self.tx(lambda tx: customer_contacts.add_number(tx, self.s["task"], n, "mobile", origin="list"))
        sql = ("INSERT INTO salescall.attempts (tenant_id, client_id, attempt_id, task_id, contact_id, phone_e164, line_id,"
               " version_id, state, claimed_at, claim_expires_at, purpose)"
               " VALUES (%s, %s, gen_random_uuid(), %s, %s, %s, %s, %s, 'claimed', now(), now() + interval '1 minute', 'normal')")
        with self.assertRaises(Rejected) as cm:
            self.tx(lambda tx: tx.execute(sql, (self.s["tenant"], self.s["client"], self.s["task"], cid, n.e164, self.s["line"],
                                                self.s["version"])), actor=SYSTEM)
        self.assertEqual(cm.exception.constraint, "dial_only_dialable_numbers")

    def test_the_dashboard_fixes_are_recorded_with_the_person(self):
        """ダッシュボードで直せるのは管理者か契約者だけで、記録した方が残る（直しは記録を足すだけ＝いまの連絡先は最新）。
        システムの処理はダッシュボードの直しを書けない（データベースが守る）。"""
        task = self.s["task"]
        self.tx(lambda tx: customer_contacts.add(tx, task, "email", "new@example.com", origin="dashboard"))
        self.tx(lambda tx: customer_contacts.add_number(tx, task, normalize("090-0000-0161"), "mobile", origin="dashboard"))
        rows = self.tx(lambda tx: tx.all("SELECT value, recorded_by FROM salescall.task_contacts WHERE task_id = %s", (task,)))
        self.assertEqual([(r["value"], r["recorded_by"]) for r in rows], [("new@example.com", OPERATOR.name)])
        self.assertEqual(self.tx(lambda tx: tx.one("SELECT recorded_by FROM salescall.task_numbers WHERE task_id = %s"
                                                   " AND source = 'record'", (task,)))["recorded_by"], OPERATOR.name)
        for work in (lambda tx: customer_contacts.add(tx, task, "email", "x@example.com", origin="dashboard"),
                     lambda tx: customer_contacts.add_number(tx, task, normalize("090-0000-0162"), "mobile", origin="dashboard")):
            with self.subTest(work=work), self.assertRaises(Rejected) as cm:
                self.tx(work, actor=SYSTEM)
            self.assertEqual(cm.exception.constraint, "customer_contact_by_person")
        with self.assertRaises(customer_contacts.ContactInvalid):         # 形の決まりは取込と同じ1か所
            self.tx(lambda tx: customer_contacts.add(tx, task, "email", "x@", origin="dashboard"))

    def test_the_contacts_in_the_list_are_checked_by_one_rule(self):
        """連絡先の形の決まりは1か所（customer_contacts.problem）。サービス名だけの列は選べない。"""
        with self.assertRaises(importer.ImportRejected):
            self.preview(csv_file([("A", "架空商事", fictional_jp_fixed())]),
                         mapping={"phone": "電話番号", "meeting_service": "会社名"})
        with self.assertRaises(importer.ImportRejected):                  # 番号の列が1つも無い
            self.preview(csv_file([("A", "架空商事", fictional_jp_fixed())]), mapping={"name": "会社名"})
        self.assertIsNone(customer_contacts.problem("email", "taro.yamada@example.co.jp"))
        for kind, value, service in (("email", "taro@", None), ("fax", "ファクス", None), ("line_id", "a b", None),
                                     ("meeting", "1", None), ("address", "東京", "Zoom"), ("phone", "x", None)):
            with self.subTest(kind=kind, value=value):
                self.assertIsNotNone(customer_contacts.problem(kind, value, service))


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
                                           "candidates": [], "likely": None},     # 会社名の読みは AI が決めた読み
                               "target": {"kind": "法人", "name": "岡田", "reading": "おかだ", "role": "", "full_name": "",
                                          "full_reading": "", "department": ""}})  # 会話の「目的の人」の形

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
        got = self.tx(lambda tx: business_settings.target_of(tx, r["rows"][0]["task_id"]))
        self.assertEqual((got["role"], got["target"]), ("社長", {"kind": "法人", "name": "", "reading": "", "role": "社長",
                                                                 "full_name": "", "full_reading": "", "department": ""}))

    def test_the_full_name_and_department_are_taken_when_the_list_has_them(self):
        """お呼びする方のフルネーム（表記と読み）と部署は、一覧にあれば取り込む（無くてもよい列。CL-1 の連絡帳 4-65 の (d)。ふだんはお名字で
        お呼びし、お名前だけでは分からないと言われたときだけ使う）。フルネームの読みもお名字と同じ決め方（読みの列・AI の読み・読みとして
        使えない値は止める）。会話が読む1か所（target_of）に渡る。"""
        _, run = self.run_with({"kind": "name", "role": None})
        header = self.HEADER + ("フルネーム", "フルネームの読み", "部署")
        mapping = dict(self.MAP, person_full_name="フルネーム", person_full_name_reading="フルネームの読み",
                       person_department="部署")
        data = csv_file([("A", "架空商事", fictional_jp_fixed(), "岡田", "おかだ", None, "岡田太郎", "おかだたろう", "営業部"),
                         ("B", "架空工業", fictional_jp_fixed(), "岡田", "おかだ", None, "岡田次郎", "岡田次郎", None)], header=header)
        r = self.register(data, mapping=mapping, run=run)
        self.assertEqual(self.outcomes(r), [(2, "registered", None), (3, "stopped", "reading_invalid")])   # 読みが漢字のまま
        t = self.tx(lambda tx: business_settings.target_of(tx, r["rows"][0]["task_id"]))["target"]
        self.assertEqual((t["full_name"], t["full_reading"], t["department"]), ("岡田太郎", "おかだたろう", "営業部"))
        with self.assertRaises(importer.ImportRejected):                  # 読みの列だけを選ぶことはできない
            self.preview(data, mapping={k: v for k, v in mapping.items() if k != "person_full_name"}, run=run)

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
                                   "contact": self.tx(lambda tx: tasks.dial_number(tx, task))["contact_id"],
                                   "phone": self.tx(lambda tx: tasks.dial_number(tx, task))["phone_e164"]}, version=vid)
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


class RowFixes(Targets):
    """お客様リストの行の直し（ダッシュボード。0178・core/row_fixes.py。2026-11-05 ユーザー：「ダッシュボードでも進捗確認や修正ができるように
    すること」）。直しは記録を足すだけで、行のいまの値は見方 tasks_now の1か所（会話・門・材料の数えが読む）。"""

    def one_row(self, kind="name", name=("中島", None), role=None):
        vid, run = self.run_with({"kind": kind, "role": None})
        data = csv_file([("A", "架空商事", fictional_jp_fixed(), name[0], name[1], role)], header=self.HEADER)
        sheet = read("一覧.csv", data)
        plan = self.tx(lambda tx: importer.preview(tx, run, sheet, self.MAP, suggest_readings=undecided))
        r = self.tx(lambda tx: importer.register(tx, run, sheet, self.MAP, expected_digest=plan.digest,
                                                 ai_readings=plan.ai_readings))
        return vid, r["rows"][0]["task_id"]

    def claim(self, vid, task):
        n = self.tx(lambda tx: tasks.dial_number(tx, task))
        return self.w.insert_claim({**self.s, "task": task, "contact": n["contact_id"], "phone": n["phone_e164"]}, version=vid)

    def test_a_fixed_name_is_what_the_call_reads_and_old_confirmed_readings_do_not_carry_over(self):
        """名前を直すと、会話が読む1か所（target_of）が直した名前と読みを返す。直す前に会話で確かめた読みは、直した名前には付けない
        （直したあとに確かめた読みだけが効く）。"""
        vid, task = self.one_row()
        aid = self.claim(vid, task)
        self.tx(lambda tx: tasks.record_confirmed_reading(tx, task, "target", "ナカシマ", attempt_id=aid))
        self.assertEqual(self.tx(lambda tx: business_settings.target_of(tx, task))["person"]["reading"], "なかしま")
        got = self.tx(lambda tx: row_fixes.fix(tx, task, "target_name", "中嶋", reading="ナカジマ"))
        self.assertEqual(got, {"field": "target_name", "value": "中嶋", "reading": "なかじま"})
        p = self.tx(lambda tx: business_settings.target_of(tx, task))["person"]
        self.assertEqual((p["text"], p["reading"], p["reading_by"]), ("中嶋", "なかじま", "person"))   # 前の確かめは付けない
        self.tx(lambda tx: tasks.record_confirmed_reading(tx, task, "target", "なかしま", attempt_id=aid))
        p = self.tx(lambda tx: business_settings.target_of(tx, task))["person"]
        self.assertEqual((p["reading"], p["reading_by"]), ("なかしま", "conversation"))      # 直したあとに確かめた読みは効く
        # 読みを入れなければ AI の読み（取込と同じ決め方）
        self.tx(lambda tx: row_fixes.fix(tx, task, "source_name", "架空商事ホールディングス", suggest=ai))
        c = self.tx(lambda tx: business_settings.target_of(tx, task))["company"]
        self.assertEqual((c["text"], c["reading"], c["reading_by"]), ("架空商事ホールディングス", "てすとがいしゃ", "ai"))

    def test_full_name_department_and_role_fixes_and_clears(self):
        """フルネーム・部署・役職も直せる。空にすると、その欄を消す（取込の値に戻さない）。直した値を、依頼者に埋めていただく欄の決め方
        （who_unclear_blanks）・門の材料の確かめ（gate._task）も読む。"""
        vid, task = self.one_row(kind="role_and_name", name=("岡田", "おかだ"), role="部長")
        self.tx(lambda tx: row_fixes.fix(tx, task, "target_full_name", "岡田太郎", reading="おかだたろう"))
        self.tx(lambda tx: row_fixes.fix(tx, task, "target_department", "営業部"))
        self.assertEqual(self.tx(lambda tx: business_settings.who_unclear_blanks(tx, task)), ())
        self.tx(lambda tx: row_fixes.fix(tx, task, "target_department", "  "))           # 空＝消す
        self.assertEqual(self.tx(lambda tx: business_settings.who_unclear_blanks(tx, task)), ("department",))
        self.tx(lambda tx: row_fixes.fix(tx, task, "target_role", None))
        from salescall.core import gate
        self.assertIsNone(self.tx(lambda tx: gate._task(tx, task))["target_role"])          # 門も直した値を読む
        t = self.tx(lambda tx: business_settings.target_of(tx, task))["target"]
        self.assertEqual((t["full_name"], t["full_reading"], t["role"]), ("岡田太郎", "おかだたろう", ""))

    def test_a_role_without_its_charge_is_refused_when_fixed_and_the_gate_says_why(self):
        """「ご担当者」だけ（何の担当か無い）の役職は、ダッシュボードの直しでも、取込・業務設定と同じ1か所（business_settings.role_problem）で
        断る（⛔ 2026-11-07 の第三者の見直し (2) の7：ここだけ通していた）。前から入っている行は、門が target_role_unclear で断り、説明に
        何を直せばよいか（何の担当かを書く）を出す（「役職がありません」とは言わない）。"""
        from salescall.core import gate
        vid, task = self.one_row(kind="role", name=(None, None), role="院長")
        for bare in ("ご担当者さん", "ご担当の方々", "担当者"):
            with self.subTest(bare=bare), self.assertRaises(row_fixes.FixInvalid) as e:
                self.tx(lambda tx: row_fixes.fix(tx, task, "target_role", bare))
            self.assertIn("何の担当かを書いてください", str(e.exception))
        self.assertEqual(self.tx(lambda tx: row_fixes.fix(tx, task, "target_role", "経理のご担当者様"))["value"], "経理のご担当者様")
        # 業務が一覧の役職でお呼びしない（名前で呼ぶ）なら、取込・門と同じく断らない（業務設定の TargetNeeds.role_unclear の1か所。
        # ⛔ 2026-11-07 の見直し：行の直しだけ業務によらず断っていた）
        _, by_name = self.one_row()
        self.assertEqual(self.tx(lambda tx: row_fixes.fix(tx, by_name, "target_role", "ご担当者さん"))["value"], "ご担当者さん")
        self.tx(lambda tx: tx.execute(   # 前から入っている「ご担当者」だけの役職（直す前の形）
            "INSERT INTO salescall.task_fixes (tenant_id, client_id, task_id, field, value, recorded_by)"
            " SELECT tenant_id, client_id, task_id, 'target_role', 'ご担当者さん', salescall.cur_actor() FROM salescall.tasks"
            " WHERE task_id = %s", (task,)))
        content = self.tx(lambda tx: gate._content(tx, vid))
        self.assertEqual(self.tx(lambda tx: gate._target_short(tx, gate._task(tx, task), "normal", content)), "target_role_unclear")
        self.assertIn("何の担当かを書いてください", gate.denied("target_role_unclear").message)
        self.assertEqual(gate.REASONS["target_role_unclear"].message,
                         "この業務は目的のお客様を役職で呼びますが、この行の役職は、" + business_settings.ROLE_UNCLEAR)

    def test_fixes_are_by_a_person_with_the_same_reading_rules(self):
        """直せるのは管理者か契約者だけ（データベースが守る）。読みはひらがなかカタカナ・読みを持つのは名前の欄だけ・直せる欄は決まった5つ。
        検証モードの業務では、AI の読みを契約者が確かめる決まりなので、名前を直すときは読みも入れる。"""
        vid, task = self.one_row()
        with self.assertRaises(Rejected) as cm:
            self.tx(lambda tx: row_fixes.fix(tx, task, "target_role", "部長"), actor=SYSTEM)
        self.assertEqual(cm.exception.constraint, "task_fix_by_person")
        for field, value, reading in (("target_name", "中嶋", "中嶋"), ("target_role", "部長", "ぶちょう"), ("phone", "x", None)):
            with self.subTest(field=field), self.assertRaises(row_fixes.FixInvalid):
                self.tx(lambda tx: row_fixes.fix(tx, task, field, value, reading=reading, suggest=ai))
        self.tx(lambda tx: settings.set_value(tx, "approval.human", True, scope="business", status="decided",
                                              rationale="試験：検証モード", review_when="試験", tenant_id=self.s["tenant"],
                                              client_id=self.s["client"], business_id=self.s["business"]))
        with self.assertRaises(row_fixes.FixInvalid):
            self.tx(lambda tx: row_fixes.fix(tx, task, "target_name", "中嶋", suggest=ai))
        self.tx(lambda tx: row_fixes.fix(tx, task, "target_name", "中嶋", reading="なかじま"))
        rows = self.tx(lambda tx: tx.all("SELECT field, recorded_by FROM salescall.task_fixes WHERE task_id = %s", (task,)))
        self.assertEqual([(r["field"], r["recorded_by"]) for r in rows], [("target_name", OPERATOR.name)])

    def test_the_dashboard_progress_reads_each_value_from_its_one_place(self):
        """行ごとの進み具合（customer_rows.progress。読むだけ）：直したあとの名前・役職・かける番号・業務の状態（結果が無ければ open）・
        次の予定・「かけない」の印・かけた回数・連絡先。どれも、それを決める1か所から読む。"""
        from salescall.core import customer_rows
        vid, task = self.one_row(kind="role_and_name", name=("岡田", "おかだ"), role="部長")
        self.tx(lambda tx: row_fixes.fix(tx, task, "target_role", "取締役"))
        self.tx(lambda tx: customer_contacts.add(tx, task, "email", "okada@example.com", origin="dashboard"))
        run = self.tx(lambda tx: tx.one("SELECT run_id FROM salescall.tasks WHERE task_id = %s", (task,)))["run_id"]
        got = self.tx(lambda tx: customer_rows.progress(tx, run))
        self.assertEqual(len(got), 1)
        g = got[0]
        self.assertEqual((g["task_id"], g["source_row"], g["client_ref"], g["source_name"], g["target_name"], g["target_role"]),
                         (task, 2, "A", "架空商事", "岡田", "取締役"))
        self.assertEqual((g["business_state"], g["next_kind"], g["excluded"], g["attempts"], g["last_state"]),
                         ("open", "call", False, 0, None))
        self.assertEqual(g["dial_phone"], self.tx(lambda tx: tasks.dial_number(tx, task))["phone_e164"])
        self.assertEqual([c["value"] for c in g["contacts"]["contacts"]], ["okada@example.com"])
        aid = self.claim(vid, task)
        g = self.tx(lambda tx: customer_rows.progress(tx, run))[0]
        self.assertEqual((g["attempts"], g["last_state"]), (1, "claimed"))
        self.w.move({**self.s, "task": task}, aid, "released")
        self.assertEqual(self.tx(lambda tx: customer_rows.progress(tx, run))[0]["attempts"], 0)   # 取り消した予約は数えない


class DialFixes(CustomerContacts):
    """かける番号の直し（ダッシュボード。0179・row_fixes.fix_dial_number。2026-11-06 ユーザー：「取り込んだ電話番号は残したまま、新しい電話番号が
    旧番号のすぐ下に入力できるようにします。新番号を登録すると、旧電話番号はグレーアウトして、新番号がくっきり表示されます。」）。"""

    def row(self, mobile=None, ref="A"):
        r = self.register(csv_file([(ref, "架空商事", fictional_jp_fixed(), None, mobile, None, None, None, None, None, None)],
                                   header=self.HEADER), mapping=self.MAP)
        return r["rows"][0]["task_id"]

    def plan(self, task):
        return self.tx(lambda tx: next_action.current(tx, task))

    def test_the_new_number_is_dialed_and_the_imported_one_is_kept(self):
        """直した番号がいまかける番号（行の最初の予定は付け替わる・時刻はそのまま）。取り込んだ番号は残り、いまかける番号ではない（画面で灰色に
        する印 dialing）。電話のあとの番号の選び（usable）・門の予定の無い行の番号も、いまかける番号を選ぶ。"""
        from salescall.core import gate
        task = self.row()
        old = self.tx(lambda tx: tasks.dial_number(tx, task))
        before = self.plan(task)
        new_phone = fictional_jp_fixed()
        got = self.tx(lambda tx: row_fixes.fix_dial_number(tx, task, new_phone, same_person=True))
        self.assertEqual(got["phone_e164"], normalize(new_phone).e164)
        after = self.plan(task)
        self.assertEqual((after["contact_id"], after["call_at"], after["kind"]), (got["contact_id"], before["call_at"], "call"))
        nums = {n["phone"]: n["dialing"] for n in self.tx(lambda tx: customer_contacts.now(tx, task))["numbers"]}
        self.assertEqual(nums, {old["phone_e164"]: False, got["phone_e164"]: True})
        from salescall.core import customer_rows
        run = self.tx(lambda tx: tx.one("SELECT run_id FROM salescall.tasks WHERE task_id = %s", (task,)))["run_id"]
        self.assertEqual(self.tx(lambda tx: customer_rows.progress(tx, run))[0]["dial_phone"], got["phone_e164"])
        n = self.tx(lambda tx: next_action.numbers_of(tx, task))
        self.assertEqual(next_action.usable(next_action.Target(old["contact_id"]), n).contact_id, got["contact_id"])
        self.assertEqual(next_action.usable(next_action.Target(old["contact_id"], None, "ask_number_again"), n).contact_id,
                         old["contact_id"])                                       # 番号の聞き直しは替えない
        self.assertEqual(next_action.usable(next_action.Target(old["contact_id"], None, "apology"), n).contact_id,
                         old["contact_id"])                                       # お詫び（その番号で受けたお客様へ）も替えない
        self.assertEqual(self.tx(lambda tx: gate._task(tx, task))["phone_e164"], got["phone_e164"])
        # 取り込んだ番号に戻すこともできる（直しは足していくだけ）
        self.tx(lambda tx: row_fixes.fix_dial_number(tx, task, old["phone_e164"], same_person=True))
        self.assertEqual(self.plan(task)["contact_id"], old["contact_id"])

    def test_a_recorded_mobile_can_become_the_number_to_dial(self):
        """お客様リストで記録だけにした携帯電話番号も、ダッシュボードでかける番号に直せる（データベースの守りは、いまかける番号だけ通す）。"""
        task = self.row(mobile="090-0000-0171")
        got = self.tx(lambda tx: row_fixes.fix_dial_number(tx, task, "090-0000-0171", same_person=True))
        self.assertEqual(self.plan(task)["contact_id"], got["contact_id"])
        # 予定の無い行（止めている行）の門も、いまかける番号を読む
        from salescall.core import gate
        self.tx(lambda tx: next_action.hold(tx, task, cause="試験"))
        self.assertEqual(self.tx(lambda tx: gate._task(tx, task))["phone_e164"], got["phone_e164"])

    def test_a_promised_call_goes_to_the_old_number_first(self):
        """まだかけていない約束の電話は、まず前の番号でかける（2026-11-06 ユーザー：「まだ約束の電話をかけていない場合は、旧番号で
        かからなければ、新番号でかけます」）。約束の電話のあとの番号の選び（usable）は、新しい番号を選ぶ（上の試験）。"""
        from tests import support
        task = self.row()
        old = self.tx(lambda tx: tasks.dial_number(tx, task))
        now = self.tx(lambda tx: tx.one("SELECT now() AS t")["t"])
        self.tx(lambda tx: support.schedule_call(tx, task, now, contact_id=old["contact_id"], cause="試験：約束", promised=True))
        self.tx(lambda tx: row_fixes.fix_dial_number(tx, task, fictional_jp_fixed(), same_person=True))
        self.assertEqual((self.plan(task)["contact_id"], self.plan(task)["promised"]), (old["contact_id"], True))

    def test_rows_ended_by_the_number_or_by_the_counts_come_back_and_others_do_not(self):
        """終えた行で番号を直したとき（2026-11-06 ユーザー）：番号のせい・決めた回数（日数）までかけて終えた行は、新しい番号へすぐかけて
        よい予定に戻る（ふつうの電話）。お客様が断った・業務を終えた行などは戻らない（next_action.reopens_on_dial_fix の1か所）。"""
        back = ("number_unusable", "alternate_wrong_again", "alternate_not_called", "alternate_blocked:number_kind_not_allowed",
                "alternate_blocked:number_unknown", "alternate_blocked:other_contractor_called",
                "ask_number_again_unanswered", "number_kind_forbidden", "number_kind_not_allowed", "number_unknown",
                "other_contractor_called", "other_client_promise_never", "max_attempts", "policy_max:open",
                "callback_exact_unreached", "callback_unspecified_time", "apology_wrong_party", "apology_fax_line", "apology_unreached")
        stay = ("completed", "declined", "do_not_call", "concluded", "out_of_scope", "complaint", "forbidden_speech", "other_means",
                "not_reached_departed", "not_reached_who_unclear", "open", "declined_in_business", "dnc_client", "apology_done",
                # お客様が言った番号が、お客様の断りで止まって終えた行（発信禁止・管理番号・同じ業務で断られたお客様。⛔ 2026-11-06 の
                # 見直し：前は理由を見ずに戻した）
                "alternate_blocked:dnc_client", "alternate_blocked:dnc_contractor", "alternate_blocked:dnc_client_ref",
                "alternate_blocked:declined_in_business", "dnc_contractor", "dnc_client_ref")
        for n, reason in enumerate(back + stay):
            with self.subTest(reason=reason):
                task = self.row(ref=f"R{n}")
                self.tx(lambda tx: next_action.end(tx, task, reason, cause="試験"))
                got = self.tx(lambda tx: row_fixes.fix_dial_number(tx, task, fictional_jp_fixed(), same_person=True))
                p = self.plan(task)
                if reason in back:
                    self.assertEqual((p["kind"], p["contact_id"], p["promised"], p["purpose"]),
                                     ("call", got["contact_id"], False, "normal"))
                else:
                    self.assertEqual((p["kind"], p["end_reason"]), ("end", reason))

    def test_which_ends_come_back_is_decided_in_one_place(self):
        """番号違い・FAX の回線で対象外にした行は戻る（ほかの対象外は戻らない）。時刻の決まっていない約束を、回数でなく契約者の方針が
        「終える」ので終えた行は戻らない。"""
        from salescall.core import business_rules

        class Tx:
            def __init__(self, why):
                self.why = why

            def one(self, sql, params=()):
                return {"why": self.why}
        for why, want in (("wrong_party", True), ("fax_line", True), ("request", False)):
            with self.subTest(why=why):
                self.assertEqual(next_action.reopens_on_dial_fix(Tx(why), None, "out_of_scope"), want)
        # 番号のせいの終わりの理由（門が番号や番号への契約者の設定で終えた行も入る＝2026-11-06 CL-2 の案への「(1)…OK (2)…OK (3)…OK」）と、
        # 決めた回数で終えた理由は、どれも戻る。お客様が断った・業務を終えたなどは戻らない（一覧は計画書 3-4-1 の (7) から書き写した。
        # NUMBER_ENDS そのものを読むと、一覧から抜けても見張れない＝⛔ 2026-11-06 の見直し (A) の9）
        for end in ("number_unusable", "alternate_wrong_again", "alternate_not_called", "ask_number_again_unanswered",
                    "number_unknown", "number_kind_forbidden", "number_kind_not_allowed", "other_contractor_called",
                    "other_client_promise_never", "alternate_blocked:number_kind_forbidden", "max_attempts", "callback_exact_unreached",
                    "policy_max:open", "apology_wrong_party", "apology_fax_line",
                    "apology_unreached"):   # お詫びの電話を決めた回数かけても届かなかった（計画書 7-1 (14)。0195）
            with self.subTest(end=end):
                self.assertTrue(next_action.reopens_on_dial_fix(Tx(None), None, end))
        for end in ("do_not_call", "complaint", "forbidden_speech", "apology_done", "completed"):
            with self.subTest(end=end):
                self.assertFalse(next_action.reopens_on_dial_fix(Tx(None), None, end))
        # 「あとで」を方針の回数より多く言って終えた行は、どれも約束の一環だったときだけ戻る（営業拒否か、まだ分からないものがあれば
        # 戻らない＝`_later_refused`。計画書 7-1 (13)。場面の試験は tests.test_settle.DialFixAfterCalls）
        for end, action, refused, want in (("callback_unspecified_time", "end", False, False),
                                           ("callback_unspecified_time", "call", True, True),
                                           (next_action.LATER_OVER, "end", False, False), (next_action.LATER_OVER, "call", False, True),
                                           (next_action.LATER_OVER, "call", True, False)):
            with self.subTest(end=end, action=action, refused=refused), \
                    mock.patch.object(next_action, "_unspecified_policy", return_value=business_rules.Policy(action, 1, None)), \
                    mock.patch.object(next_action, "_later_refused", return_value=refused):
                self.assertEqual(next_action.reopens_on_dial_fix(Tx(None), None, end), want)

    def test_a_row_ended_by_a_number_only_dnc_comes_back(self):
        """門が発信禁止で終えた行は、その番号の登録が番号違い・FAX の回線（その番号だけ）なら番号のせいなので、番号を直すと戻る。
        お客様が断った・手で登録した番号は戻らない（⛔ 見直し (A) の8 (3)）。お客様が言った番号が発信禁止で止まって終えた行
        （alternate_blocked:dnc_client）も、同じ1か所（next_action._blocked_by_number）で決める（⛔ 2026-11-06 の見直し：前は理由を見ずに戻した）。"""
        cases = [(end, reasons, back) for end in ("dnc_client", "alternate_blocked:dnc_client")
                 for reasons, back in ((("wrong_party",), True), (("fax_line",), True), (("manual",), False),
                                       (("refused_in_call",), False),
                                       (("wrong_party", "refused_in_call"), False))]   # 行のほかの番号（携帯）に断りの登録があれば戻さない
        for n, (end, reasons, back) in enumerate(cases):
            with self.subTest(end=end, reasons=reasons):
                mobile = normalize(f"090-0001-{n:04d}").e164
                task = self.row(mobile=mobile, ref=f"D{n}")
                old = self.tx(lambda tx: tasks.dial_number(tx, task))
                self.tx(lambda tx: dnc.register(tx, old["phone_e164"], reasons[0]))
                for reason in reasons[1:]:
                    self.tx(lambda tx: dnc.register(tx, mobile, reason))
                self.tx(lambda tx: next_action.end(tx, task, end, cause="試験：発信禁止で止めて終えた"))
                got = self.tx(lambda tx: row_fixes.fix_dial_number(tx, task, fictional_jp_fixed(), same_person=True))
                p = self.plan(task)
                self.assertEqual((p["kind"], p["contact_id"] if back else p["end_reason"]),
                                 ("call", got["contact_id"]) if back else ("end", end))

    def test_the_standing_of_a_number_is_read_at_that_time(self):
        """番号の立場（前にかける番号だったか）は、その時刻で読む（いまかける番号も、その時刻までの直しで＝salescall.task_dial_contact_at・
        0196）。直す前にかけた通話の番号は「かけたとき前の番号」ではない（⛔ 2026-11-06 の見直し：前は、いまかける番号だけ「いま」で読み、
        通話の途中で直すと、その通話の番号が前の番号とされた）。"""
        task = self.row(ref="S")
        old = self.tx(lambda tx: tasks.dial_number(tx, task))
        before = self.tx(lambda tx: tx.one("SELECT now() AS t")["t"])
        self.tx(lambda tx: row_fixes.fix_dial_number(tx, task, fictional_jp_fixed(), same_person=True))
        then, now = (self.tx(lambda tx: tx.one("SELECT salescall.task_number_replaced_at(%s, %s, %s) AS r",
                                               (task, old["contact_id"], at))["r"]) for at in (before, "infinity"))
        self.assertEqual((then, now), (False, True))

    def test_a_claim_taken_before_the_fix_is_released(self):
        """門が前の番号へ予約を出したあと、発信の前に契約者が番号を直したら、その予約は取り消す（門の判定に効く変更。DESIGN.md 5-2-1 の2。
        予約し直すのは門＝直したあとの予定で。⛔ 2026-11-06 の見直し：前は予約が残り、直したあとに前の番号へかかった）。"""
        task = self.row(ref="C")
        old = self.tx(lambda tx: tasks.dial_number(tx, task))
        version = self.tx(lambda tx: tx.one("SELECT salescall.run_current_version(t.run_id) AS v FROM salescall.tasks t"
                                            " WHERE t.task_id = %s", (task,)))["v"]
        aid = self.w.insert_claim({**self.s, "task": task, "contact": old["contact_id"], "phone": old["phone_e164"]}, version=version)
        self.tx(lambda tx: row_fixes.fix_dial_number(tx, task, fictional_jp_fixed(), same_person=True))
        self.assertEqual(self.tx(lambda tx: tx.one("SELECT state FROM salescall.attempts WHERE attempt_id = %s", (aid,)))["state"],
                         "released")

    def test_a_number_another_row_of_the_run_dials_is_not_taken(self):
        """同じ実施回のほかの行がかけている番号へは直せない（1つの実施回で同じお客様は1行＝データベースの守り 0188）。追加の取込も、直した
        番号を同じ実施回の行として見る（⛔ 見直し (A) の3）。"""
        first, second = self.row(ref="A"), self.row(ref="B")
        theirs = self.tx(lambda tx: tasks.dial_number(tx, second))["phone_e164"]
        with self.assertRaises(row_fixes.FixInvalid):
            self.tx(lambda tx: row_fixes.fix_dial_number(tx, first, theirs, same_person=True))
        mine = fictional_jp_fixed()
        self.tx(lambda tx: row_fixes.fix_dial_number(tx, first, mine, same_person=True))
        r = self.register(csv_file([("Z", "架空商事", mine, None, None, None, None, None, None, None, None)], header=self.HEADER),
                          mapping=self.MAP)
        self.assertEqual(r["rows"][0]["reason_code"], "number_in_run_other_ref")

    def test_the_database_stamps_the_time_of_every_fix(self):
        """直しの時刻はデータベースが決める（0193 の stamp_now）。⛔ 2026-11-06 の見直し：前は契約者の取引が、かける番号の直しの時刻を
        先の時刻にでき（行の回数を数え始める時刻が先になり、行の回数が0のまま・サブも効かない）、お呼びする方の直しの時刻を前にできた
        （約束のあとに相手が替わったかを取り違える）。"""
        task = self.row()
        k = self.tx(lambda tx: tasks.dial_number(tx, task))["contact_id"]
        for table, columns, values, shift in (
                ("task_dial_fixes", "contact_id, same_person", "%s, true", "10 years"),
                ("task_fixes", "field, value", "'target_role', %s", "-1 year")):
            with self.subTest(table=table):
                got = self.tx(lambda tx: tx.one(
                    f"INSERT INTO salescall.{table} (tenant_id, client_id, task_id, {columns}, recorded_by, recorded_at)"
                    f" SELECT t.tenant_id, t.client_id, t.task_id, {values}, salescall.cur_actor(), now() + interval '{shift}'"
                    " FROM salescall.tasks t WHERE t.task_id = %s RETURNING recorded_at = now() AS stamped",
                    (k if table == "task_dial_fixes" else "部長", task)))
                self.assertTrue(got["stamped"])
        self.assertLessEqual(self.tx(lambda tx: tx.one("SELECT salescall.task_count_from(%s) <= now() AS ok", (task,)))["ok"], True)

    def test_the_latest_fix_is_one_place_for_the_number_and_the_count(self):
        """いまかける番号（task_dial_contact）と、行の回数を数え始める時刻（task_count_from）は、同じ「いちばん新しい直し」（0193 の
        task_latest_dial_fix＝順番の番号のいちばん大きいもの）を読む。⛔ 2026-11-06 の見直し：前は片方が順番の番号、片方が時刻の最大で決めていた。"""
        task = self.row()
        self.tx(lambda tx: row_fixes.fix_dial_number(tx, task, fictional_jp_fixed(), same_person=True))
        self.tx(lambda tx: row_fixes.fix_dial_number(tx, task, fictional_jp_fixed(), same_person=True))
        got = self.tx(lambda tx: tx.one(
            "SELECT (salescall.task_latest_dial_fix(%s)).contact_id = salescall.task_dial_contact(%s) AS same_number,"
            "       (salescall.task_latest_dial_fix(%s)).recorded_at = salescall.task_count_from(%s) AS same_time,"
            "       (salescall.task_latest_dial_fix(%s)).seq = (SELECT max(seq) FROM salescall.task_dial_fixes WHERE task_id = %s) AS latest",
            (task,) * 6))
        self.assertEqual(got, {"same_number": True, "same_time": True, "latest": True})

    def test_an_added_import_sees_every_number_a_row_may_dial(self):
        """追加の取込も、同じ実施回の行がかけうる番号（salescall.task_dialable の1か所＝直しの守りと同じ）を見る：行B を y・w と直し、
        まだかけていない約束の電話が y のままなら、y の行は取り込まない（⛔ 2026-11-06 の見直し：前は取り込んだ番号といまかける番号だけ）。"""
        from tests import support
        task = self.row(ref="B")
        y = fictional_jp_fixed()
        ky = self.tx(lambda tx: row_fixes.fix_dial_number(tx, task, y, same_person=True))["contact_id"]
        self.tx(lambda tx: row_fixes.fix_dial_number(tx, task, fictional_jp_fixed(), same_person=True))
        now = self.tx(lambda tx: tx.one("SELECT now() AS t")["t"])
        self.tx(lambda tx: support.schedule_call(tx, task, now, contact_id=ky, cause="試験：約束", promised=True))
        r = self.register(csv_file([("Z", "架空商事", y, None, None, None, None, None, None, None, None)], header=self.HEADER),
                          mapping=self.MAP)
        self.assertEqual(r["rows"][0]["reason_code"], "number_in_run_other_ref")

    def test_two_fixes_to_the_same_number_at_once_take_it_once(self):
        """同時に2つの行を同じ番号へ直しても、片方だけ（書く取引は直列化できる形＝SERIALIZABLE で動き、あとから確定するほうはやり直しで
        先の直しを見て、データベースの守りに断られる）。"""
        from salescall.core import contacts
        first, second = self.row(ref="A"), self.row(ref="B")
        phone = fictional_jp_fixed()
        self.tx(lambda tx: contacts.get_or_create(tx, normalize(phone)))   # 番号の連絡先は先に（連絡先を作る所で待ち合わせない）
        start = threading.Barrier(2)
        got, refused = [], []

        def go(task):
            waited = []

            def work(tx):
                out = row_fixes.fix_dial_number(tx, task, phone, same_person=True)
                if not waited:   # 1回目だけ、2つの取引がどちらも書いてから確定する（やり直しでは待たない）
                    waited.append(True)
                    try:
                        start.wait(timeout=20)
                    except threading.BrokenBarrierError:
                        pass
                return out
            try:
                got.append(self.tx(work))
            except row_fixes.FixInvalid:
                refused.append(task)

        threads = [threading.Thread(target=go, args=(t,)) for t in (first, second)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual((len(got), len(refused)), (1, 1))
        self.assertEqual(self.tx(lambda tx: tx.one(
            "SELECT count(*) AS n FROM salescall.tasks t WHERE t.task_id = ANY(%s)"
            " AND salescall.task_dial_contact(t.task_id) = %s", ([first, second], got[0]["contact_id"])))["n"], 1)

    def test_a_second_fix_counts_again_and_drops_the_sub(self):
        """2回直したとき：行の回数は、いちばん新しい直しから数え直す（salescall.task_count_from）。前の直しのあとに記録したサブが効かない
        ことは、通話のある試験（test_settle.DialFixAfterCalls）で見る。記録した方は、いま操作している方（データベースの守り）。"""
        task = self.row()
        old = self.tx(lambda tx: tasks.dial_number(tx, task))
        first = self.tx(lambda tx: row_fixes.fix_dial_number(tx, task, fictional_jp_fixed(), same_person=True))
        at_first = self.tx(lambda tx: tx.one("SELECT salescall.task_count_from(%s) AS t", (task,)))["t"]
        self.tx(lambda tx: row_fixes.fix_dial_number(tx, task, fictional_jp_fixed(), same_person=True))
        self.assertGreater(self.tx(lambda tx: tx.one("SELECT salescall.task_count_from(%s) AS t", (task,)))["t"], at_first)
        with self.assertRaises(Rejected) as e:
            self.tx(lambda tx: tx.execute(
                "INSERT INTO salescall.task_dial_fixes (tenant_id, client_id, task_id, contact_id, recorded_by, same_person)"
                " SELECT t.tenant_id, t.client_id, t.task_id, %s, 'だれか別の方', true FROM salescall.tasks t WHERE t.task_id = %s",
                (first["contact_id"], task)))
        self.assertEqual(e.exception.constraint, "task_dial_fix_recorded_by")

    def test_a_row_put_back_after_a_dnc_is_revoked_points_its_plan_like_a_fix(self):
        """発信禁止を取り消して終える前の予定に戻すときも、番号を直したときと同じ1か所でいまの番号へ向ける：まだかけていない約束の電話は
        前の番号のまま（⛔ 見直し (A) の7：前は約束の電話もすぐ新しい番号へ替えた）。"""
        from tests import support
        task = self.row(ref="V")
        old = self.tx(lambda tx: tasks.dial_number(tx, task))
        now = self.tx(lambda tx: tx.one("SELECT now() AS t")["t"])
        self.tx(lambda tx: support.schedule_call(tx, task, now, contact_id=old["contact_id"], cause="試験：約束", promised=True))
        self.tx(lambda tx: row_fixes.fix_dial_number(tx, task, fictional_jp_fixed(), same_person=True))
        entry = self.tx(lambda tx: dnc.register(tx, old["phone_e164"], "manual"))
        self.tx(lambda tx: next_action.end(tx, task, "dnc_client", cause="試験：門が発信禁止で終えた"))
        ids = [r["entry_id"] for r in self.tx(lambda tx: tx.all(
            "SELECT entry_id FROM salescall.dnc_entries WHERE phone_e164 = %s AND revoked_at IS NULL", (old["phone_e164"],)))]
        for i in ids:
            self.tx(lambda tx: dnc.revoke(tx, i, "依頼者からの正式な求め（試験）"))
        p = self.plan(task)
        self.assertEqual((p["kind"], p["contact_id"], p["promised"]), ("call", old["contact_id"], True))
        self.assertTrue(entry)

    def test_fixing_to_the_number_already_dialed_writes_nothing(self):
        task = self.row()
        old = self.tx(lambda tx: tasks.dial_number(tx, task))
        self.tx(lambda tx: row_fixes.fix_dial_number(tx, task, old["phone_e164"], same_person=True))
        self.assertEqual(self.tx(lambda tx: tx.one("SELECT count(*) AS n FROM salescall.task_dial_fixes WHERE task_id = %s",
                                                   (task,)))["n"], 0)

    def test_a_number_that_was_once_dialed_can_still_be_called_after_another_fix(self):
        """番号を2回直しても、前にかける番号だった記録だけの番号（ここでは携帯電話番号）へ、まだかけていない約束の電話をかけられる
        （データベースの守りは、いままでに一度でもかける番号だった番号を通す＝0182。⛔ 0179 では断った）。"""
        from tests import support
        task = self.row(mobile="090-0000-0173", ref="C")
        mobile = self.tx(lambda tx: row_fixes.fix_dial_number(tx, task, "090-0000-0173", same_person=True))
        self.tx(lambda tx: row_fixes.fix_dial_number(tx, task, fictional_jp_fixed(), same_person=True))
        now = self.tx(lambda tx: tx.one("SELECT now() AS t")["t"])
        self.tx(lambda tx: support.schedule_call(tx, task, now, contact_id=mobile["contact_id"], cause="試験：約束", promised=True))
        self.assertEqual(self.plan(task)["contact_id"], mobile["contact_id"])

    def test_the_new_number_passes_the_same_checks_and_only_a_person_fixes_it(self):
        """直した番号も取込と同じ確かめ（読めない・発信禁止は断る）。直せるのは管理者か契約者だけ（データベースが守る）。"""
        task = self.row()
        bad = fictional_jp_fixed()
        self.tx(lambda tx: dnc.register(tx, bad, "manual"))
        for phone in ("abc", bad):
            with self.subTest(phone=phone), self.assertRaises(row_fixes.FixInvalid):
                self.tx(lambda tx: row_fixes.fix_dial_number(tx, task, phone, same_person=True))
        task = self.row(mobile="090-0000-0172", ref="B")      # 行にある番号へ直す（記録だけの番号を足す守りより先に、直しの守りに当たる）
        with self.assertRaises(Rejected) as cm:
            self.tx(lambda tx: row_fixes.fix_dial_number(tx, task, "090-0000-0172", same_person=True), actor=SYSTEM)
        self.assertEqual(cm.exception.constraint, "task_dial_fix_by_person")
