"""発信禁止の一覧の取込の試験（DESIGN.md 3-6-2・Intent.md 原則14・7-2。core/dnc_import.py・表の作り 0065）。

- 依頼者の取引なら依頼者の台帳、契約者の取引なら契約者の共通の台帳（入口は1つ）。取り込めるのは管理者か契約者だけ。
- どこまで止めるかは dnc.stop_set の1か所（依頼者の一覧は、その番号・管理番号を持つ行のお客様の番号すべてまで。依頼者をまたがない）。
- 登録はプレビューと同じときだけ・1つの取引で全部。一覧から入った登録は、どの取込から入ったかを持つ。
⚠️ 中身は架空のもの（番号は局番のあとが 0000 の、実在しない番号。会社名も架空）。
"""

from __future__ import annotations

import unittest

from salescall.core import dnc, dnc_import, target_policy, tasks
from salescall.core.access import Rejected
from salescall.core.import_columns import ImportRejected
from salescall.core.import_files import read
from salescall.core.phone import normalize
from tests.support import OPERATOR, SYSTEM, World, ensure_rules, fictional_jp_fixed, owner_access
from tests.test_dnc import claim_row, import_rows, in_client
from tests.test_import import csv_file
from tests.test_import_files import xlsx

LIST = {"phone": "電話番号", "client_ref": "管理番号"}


def dnc_csv(rows, header=("管理番号", "会社名", "電話番号")) -> bytes:
    return csv_file(rows, header)


class Base(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.s = self.w.setup()
        ensure_rules(self.w, self.s)

    def client_tx(self, work, actor=OPERATOR, s=None):
        s = s or self.s
        return self.w.a.in_client(actor, s["tenant"], s["client"], work)

    def tenant_tx(self, work, actor=OPERATOR):
        return self.w.a.in_tenant(actor, self.s["tenant"], work)

    def take(self, data, mapping=LIST, *, run=None, name="発信禁止.csv"):
        """プレビューを見て、そのまま登録する（run＝依頼者の取引。None なら契約者の取引）。"""
        sheet = read(name, data)
        tx = self.client_tx if run is not False else self.tenant_tx
        plan = tx(lambda t: dnc_import.preview(t, sheet, mapping))
        return plan, tx(lambda t: dnc_import.register(t, sheet, mapping, expected_digest=plan.digest))

    def blocked(self, number, ref=None, s=None):
        s = s or self.s
        return self.client_tx(lambda tx: target_policy.block_reason(
            tx, normalize(number), client_ref=ref, tenant_id=s["tenant"], client_id=s["client"], business_id=s["business"]), s=s)


class ClientList(Base):
    def test_the_list_is_registered_once_with_reasons_and_rows(self):
        a, b = fictional_jp_fixed(), fictional_jp_fixed()
        data = dnc_csv([("R-1", "架空商事", a), ("", "", ""), ("R-2", "架空工業", "0300009"), ("", "架空物産", b),
                        ("", "架空商事", a)])
        plan, got = self.take(data)
        self.assertEqual([(r.row_no, r.outcome, r.reason_code) for r in plan.rows],
                         [(2, "registered", "ref_matches_no_row"), (3, "empty", None),
                          (4, "registered", "number_unreadable"),      # 番号が読めなくても、管理番号で止める（止める側に倒す）
                          (5, "registered", None), (6, "already", "already_stopped")])
        self.assertEqual((got["counts"]["numbers"], got["counts"]["client_refs"]), (2, 2))
        self.assertEqual(self.blocked(fictional_jp_fixed(), ref="R-2")[0], "dnc_client_ref")
        for number in (a, b):
            self.assertEqual(self.blocked(number)[0], "dnc_client")    # 発信対象の取込と門が、同じ1か所で止める
        self.assertEqual(self.blocked(fictional_jp_fixed(), ref="R-1")[0], "dnc_client_ref")   # 一覧の管理番号のお客様も
        rows = self.client_tx(lambda tx: tx.all(
            "SELECT reason::text AS reason, import_id FROM salescall.dnc_entries WHERE import_id = %s", (got["import_id"],)))
        self.assertEqual(({r["reason"] for r in rows}, len(rows)), ({"client_list"}, 4))
        again_plan, again = self.take(data)                             # 同じ一覧をもう一度：新しく止めるものは無い
        self.assertEqual((again["counts"]["registered"], again["counts"]["numbers"]), (0, 0))
        rec = self.client_tx(lambda tx: tx.all(
            "SELECT outcome, new_numbers FROM salescall.dnc_import_rows WHERE import_id = %s ORDER BY row_no", (got["import_id"],)))
        self.assertEqual([r["outcome"] for r in rec], ["registered", "empty", "registered", "registered", "already"])

    def test_numbers_that_lost_their_leading_zero_are_read_like_the_target_list(self):
        """表計算ソフトで先頭の 0 が消えた番号は、発信対象の取込と同じ決め方で 0 を足して止める（同じ番号を別の番号に読まない）。"""
        number = fictional_jp_fixed()
        digits = normalize(number).e164.replace("+81", "")
        got = self.take(xlsx([("電話番号",), (int(digits),)]), {"phone": "電話番号"}, name="発信禁止.xlsx")[1]
        self.assertEqual(got["counts"]["numbers"], 1)
        self.assertEqual(self.blocked(number)[0], "dnc_client")

    def test_a_reference_that_matches_no_row_is_pointed_out(self):
        """管理番号に当たる行が無ければ、登録はするが注意を出す（数の型の欄なら、先頭の 0 が消えていないか確かめてもらう）。
        当たる行があれば注意は無い。"""
        import_rows(self.w, self.s, self.s["run"], [("000123", "架空商事", fictional_jp_fixed())])
        plan = self.take(xlsx([("管理番号",), (123,), ("000123",)]), {"client_ref": "管理番号"}, name="発信禁止.xlsx")[0]
        self.assertEqual([(r.outcome, r.reason_code) for r in plan.rows],
                         [("registered", "ref_matches_no_row"), ("registered", None)])
        self.assertIn("先頭の 0", plan.rows[0].reason)

    def test_the_customers_rows_are_stopped_like_a_refusal_but_not_other_clients(self):
        """一覧の番号・管理番号を持つ行のお客様は、断ったお客様と同じく、その行の番号すべてまで止める（お客様ごと。原則14）。
        ほかの依頼者の行は止めない（依頼者をまたがない）。止めた番号の、まだ発信していない予約は取り消す。"""
        a, b, c, d = fictional_jp_fixed(), normalize(fictional_jp_fixed()), fictional_jp_fixed(), fictional_jp_fixed()
        rows = import_rows(self.w, self.s, self.s["run"], [("X", "架空商事", a), ("Y", "架空工業", c)])["rows"]
        first, _ = claim_row(self.w, self.s, rows[0]["task_id"], dialing=True)
        in_client(self.w, self.s, lambda tx: tasks.add_callee_number(tx, rows[0]["task_id"], b, source_attempt_id=first))
        waiting, _ = claim_row(self.w, self.s, rows[1]["task_id"])
        other = World().setup(tenant=self.s["tenant"], phone=normalize(b.e164).e164)
        plan, got = self.take(dnc_csv([("", "架空商事", a), ("Y", "", d)]))
        self.assertEqual(set(plan.rows[0].new_numbers), {normalize(a).e164, b.e164})   # お客様が言った別の番号も
        self.assertEqual(set(plan.rows[1].new_numbers), {normalize(d).e164, normalize(c).e164})   # 管理番号の行の番号も
        self.assertEqual(self.blocked(b.e164)[0], "dnc_client")
        self.assertIsNone(self.blocked(b.e164, s=other))                   # ほかの依頼者は止めない
        state = in_client(self.w, self.s, lambda tx: tx.one("SELECT state::text AS s FROM salescall.attempts WHERE attempt_id = %s",
                                                            (waiting,)))["s"]
        self.assertEqual((state, got["released_claims"] >= 1), ("released", True))

    def test_nothing_is_written_if_the_ledger_changed_after_the_preview(self):
        a, b = fictional_jp_fixed(), fictional_jp_fixed()
        sheet = read("発信禁止.csv", dnc_csv([("", "架空商事", a), ("", "架空工業", b)]))
        plan = self.client_tx(lambda tx: dnc_import.preview(tx, sheet, LIST))
        self.client_tx(lambda tx: dnc.register(tx, a, "manual"))          # プレビューのあとで台帳が変わった
        with self.assertRaises(dnc_import.PlanChanged):
            self.client_tx(lambda tx: dnc_import.register(tx, sheet, LIST, expected_digest=plan.digest))
        self.assertIsNone(self.blocked(b))                                 # 何も書いていない

    def test_mapping_is_checked_before_anything(self):
        data = dnc_csv([("", "架空商事", fictional_jp_fixed())])
        for mapping in ({"name": "会社名"}, {}, {"phone": "無い列"}):
            with self.subTest(mapping=mapping), self.assertRaises(ImportRejected):
                self.client_tx(lambda tx: dnc_import.preview(tx, read("発信禁止.csv", data), mapping))


class ContractorList(Base):
    def test_a_contractor_list_stops_the_number_for_all_clients_of_that_contractor(self):
        a = fictional_jp_fixed()
        other = World().setup(tenant=self.s["tenant"])
        elsewhere = World().setup()
        got = self.take(dnc_csv([("", "架空商事", a)]), {"phone": "電話番号"}, run=False)[1]
        self.assertEqual(got["counts"]["numbers"], 1)
        for s in (self.s, other):
            self.assertEqual(self.blocked(a, s=s)[0], "dnc_contractor")
        self.assertIsNone(self.blocked(a, s=elsewhere))                    # ほかの契約者には効かない
        entry = self.tenant_tx(lambda tx: tx.one("SELECT import_id FROM salescall.contractor_dnc_entries WHERE phone_e164 = %s",
                                                 (normalize(a).e164,)))
        self.assertEqual(entry["import_id"], got["import_id"])
        with self.assertRaises(ImportRejected):                            # 管理番号は依頼者ごとのもの
            self.tenant_tx(lambda tx: dnc_import.preview(tx, read("x.csv", dnc_csv([("R", "", a)])), LIST))
        listed = self.tenant_tx(lambda tx: tx.all("SELECT ledger FROM salescall.dnc_imports"))
        self.assertEqual([r["ledger"] for r in listed], ["contractor"])


class Guards(Base):
    def test_only_a_person_imports_and_records_and_list_entries_are_fixed(self):
        a = fictional_jp_fixed()
        sheet = read("発信禁止.csv", dnc_csv([("", "架空商事", a)]))
        plan = self.client_tx(lambda tx: dnc_import.preview(tx, sheet, LIST), actor=SYSTEM)
        with self.assertRaises(Rejected):                                  # システムの処理は一覧を取り込まない
            self.client_tx(lambda tx: dnc_import.register(tx, sheet, LIST, expected_digest=plan.digest), actor=SYSTEM)
        with self.assertRaises(Rejected):                                  # 依頼者の一覧の登録は、必ず取込から
            self.client_tx(lambda tx: tx.execute(
                "INSERT INTO salescall.dnc_entries (tenant_id, client_id, phone_e164, reason, registered_by)"
                " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, 'client_list', 'x')", (normalize(a).e164,)))
        got = self.take(dnc_csv([("", "架空商事", a)]))[1]
        later = self.take(dnc_csv([("", "架空工業", fictional_jp_fixed())]))[1]
        own = owner_access()
        for sql, args in (("UPDATE salescall.dnc_entries SET import_id = %s WHERE import_id = %s", (later["import_id"], got["import_id"])),
                          ("UPDATE salescall.dnc_imports SET file_name = 'x' WHERE import_id = %s", (got["import_id"],)),
                          ("DELETE FROM salescall.dnc_import_rows WHERE import_id = %s", (got["import_id"],))):
            with self.subTest(sql=sql), self.assertRaises(Rejected):     # 取込の記録と、取込を指す欄は変えられない
                own.in_client(OPERATOR, self.s["tenant"], self.s["client"], lambda tx: tx.execute(sql, args))
        other = World().setup()
        seen = self.w.a.in_client(OPERATOR, other["tenant"], other["client"], lambda tx: tx.all(
            "SELECT 1 FROM salescall.dnc_imports WHERE import_id = %s", (got["import_id"],)))
        self.assertEqual(seen, [])                                         # ほかの契約者の取込は見えない


if __name__ == "__main__":
    unittest.main()
