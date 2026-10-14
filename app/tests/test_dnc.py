"""発信禁止台帳（Q3）と「その相手・番号へかけてよいか」（target_policy）の試験。

⚠️ 番号は架空のもの（局番のあとが 0000 の、実在しない番号）。
"""

from __future__ import annotations

import json
import unittest

from salescall.core import attempts, dnc, importer, target_policy
from salescall.core.access import Rejected
from salescall.core.import_files import read
from salescall.core.phone import normalize
from tests.support import OPERATOR, SYSTEM, World, fictional_jp_fixed
from tests.test_import import MAP, csv_file


def jp_setup(w, tenant=None, phone=None):
    raw = phone or fictional_jp_fixed()
    s = w.setup(phone=normalize(raw).e164, tenant=tenant)
    s["raw"] = raw
    return s


def in_client(w, s, work, actor=SYSTEM):
    return w.a.in_client(actor, s["tenant"], s["client"], work)


def state_of(w, s, aid):
    return in_client(w, s, lambda tx: tx.one("SELECT state FROM salescall.attempts WHERE attempt_id = %s", (aid,)))["state"]


def blocked(w, s, raw):
    return in_client(w, s, lambda tx: target_policy.block_reason(
        tx, normalize(raw), client_ref=None, tenant_id=s["tenant"], client_id=s["client"], business_id=s["business"]))


class Register(unittest.TestCase):
    def test_any_spelling_is_the_same_entry(self):
        w = World(); s = jp_setup(w)
        digits = s["raw"].replace("-", "")
        first = in_client(w, s, lambda tx: dnc.register(tx, s["raw"], "refused_in_call"))
        for spelling in (digits, "０３－００００－" + digits[-4:].translate(str.maketrans("0123456789", "０１２３４５６７８９")),
                         "+81 " + digits[1:], f"TEL:{s['raw']}"):
            with self.subTest(spelling=spelling):
                again = in_client(w, s, lambda tx: dnc.register(tx, spelling, "manual"))
                self.assertEqual(again["entry_id"], first["entry_id"])   # 何度登録しても1つ
                self.assertEqual(again["reason"], "refused_in_call")     # 前の登録はそのまま
        n = in_client(w, s, lambda tx: tx.one("SELECT count(*) AS n FROM salescall.dnc_entries"))["n"]
        self.assertEqual(n, 1)

    def test_unknown_numbers_cannot_be_registered(self):
        w = World(); s = jp_setup(w)
        for raw in ("03-0000-0001 内線12", "300000001", "FAX 03-0000-0001", ""):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    in_client(w, s, lambda tx: dnc.register(tx, raw, "manual"))

    def test_registering_releases_claims_of_that_number_in_the_same_transaction(self):
        w = World(); s = jp_setup(w); other_number = jp_setup(w)
        same_number_other_client = jp_setup(w, phone=s["raw"])
        a = w.insert_claim(s); b = w.insert_claim(other_number)
        r = in_client(w, s, lambda tx: dnc.register(tx, s["raw"], "refused_found_after"))
        self.assertEqual(r["released_claims"], 1)
        self.assertEqual((state_of(w, s, a), state_of(w, other_number, b)), ("released", "claimed"))
        self.assertIsNone(blocked(w, same_number_other_client, s["raw"]))   # 依頼主の台帳は、その依頼主にだけ効く

    def test_system_ledger_applies_to_every_client_and_releases_their_claims(self):
        w = World(); s1 = jp_setup(w)
        a = w.insert_claim(s1)
        n = w.a.in_system(OPERATOR, lambda tx: dnc.register_system(tx, s1["raw"], "総務省の注意喚起（試験）"))
        self.assertEqual(n, 1)
        self.assertEqual(state_of(w, s1, a), "released")
        s2 = World().setup()   # ほかの契約者の依頼主
        s2["raw"] = s1["raw"]
        self.assertEqual(blocked(w, s2, s1["raw"])[0], "dnc_system")

    def test_other_clients_cannot_see_the_ledger(self):
        w = World(); s = jp_setup(w); other = jp_setup(w, tenant=s["tenant"])
        in_client(w, s, lambda tx: dnc.register(tx, s["raw"], "manual"))
        n = in_client(w, other, lambda tx: tx.one("SELECT count(*) AS n FROM salescall.dnc_entries"))["n"]
        self.assertEqual(n, 0)


class Revoke(unittest.TestCase):
    def test_only_a_person_revokes_once_and_can_register_again(self):
        w = World(); s = jp_setup(w)
        e = in_client(w, s, lambda tx: dnc.register(tx, s["raw"], "manual"))["entry_id"]
        with self.assertRaises(Rejected):
            in_client(w, s, lambda tx: dnc.revoke(tx, e, "システムは取り消せない"))
        in_client(w, s, lambda tx: dnc.revoke(tx, e, "依頼主から登録の誤りと連絡（試験）"), actor=OPERATOR)
        self.assertIsNone(blocked(w, s, s["raw"]))
        with self.assertRaises(dnc.DncEntryNotFound):
            in_client(w, s, lambda tx: dnc.revoke(tx, e, "2回目"), actor=OPERATOR)
        again = in_client(w, s, lambda tx: dnc.register(tx, s["raw"], "refused_in_call"))
        self.assertNotEqual(again["entry_id"], e)
        self.assertEqual(blocked(w, s, s["raw"])[0], "dnc_client")


class Policy(unittest.TestCase):
    """番号で決まる「かけない理由」は1か所（取込と門の両方がこれを呼ぶ）。"""

    def test_reasons(self):
        w = World(); s = jp_setup(w)
        cases = {
            "03-0000-0001": None, "090-0000-0001": None, "050-0000-0001": None,
            "0120-000-001": None,                                      # フリーダイヤル：初めの値は「かける」
            "0570-000-001": "number_kind_not_allowed",                 # ナビダイヤル：初めの値は「かけない」
            "110": "number_kind_forbidden", "0990-000-001": "number_kind_forbidden",
            "+44 20 0000 0000": "number_kind_forbidden", "#8000": "number_kind_forbidden",
            "300000001": "number_unknown", "03-0000-0001 内線1": "number_unknown",
        }
        for raw, code in cases.items():
            with self.subTest(raw=raw):
                got = blocked(w, s, raw)
                self.assertEqual(got[0] if got else None, code)

    def test_client_setting_decides_toll_free_and_navi(self):
        w = World(); s = jp_setup(w)
        for key, value in (("calling.allow_navi_dial", True), ("calling.allow_toll_free", False)):
            in_client(w, s, lambda tx: tx.execute(
                "INSERT INTO salescall.setting_values (key, scope, tenant_id, client_id, value, status, rationale,"
                " review_when, set_by) VALUES (%s, 'client', %s, %s, %s, 'decided', '試験', '試験', '試験')",
                (key, s["tenant"], s["client"], json.dumps(value))), actor=OPERATOR)
        self.assertIsNone(blocked(w, s, "0570-000-001"))
        self.assertEqual(blocked(w, s, "0120-000-001")[0], "number_kind_not_allowed")
        other = jp_setup(w, tenant=s["tenant"])
        self.assertEqual(blocked(w, other, "0570-000-001")[0], "number_kind_not_allowed")   # ほかの依頼主は初めの値のまま


def set_client(w, s, key, value):
    in_client(w, s, lambda tx: tx.execute(
        "INSERT INTO salescall.setting_values (key, scope, tenant_id, client_id, value, status, rationale,"
        " review_when, set_by) VALUES (%s, 'client', %s, %s, %s, 'decided', '試験', '試験', '試験')",
        (key, s["tenant"], s["client"], json.dumps(value))), actor=OPERATOR)


def import_rows(w, s, run, rows):
    sheet = read("一覧.csv", csv_file(rows))

    def work(tx):
        plan = importer.preview(tx, run, sheet, MAP)
        return importer.register(tx, run, sheet, MAP, expected_digest=plan.digest)
    return in_client(w, s, work, actor=OPERATOR)


def claim_row(w, s, task_id, *, dialing=False):
    """取込で作った行に、予約（試行）を1つ作る。"""
    row = in_client(w, s, lambda tx: tx.one(
        "SELECT t.contact_id, c.phone_e164 FROM salescall.tasks t JOIN salescall.contacts c USING (contact_id)"
        " WHERE t.task_id = %s", (task_id,)))
    aid = w.insert_claim(dict(s, task=task_id, contact=row["contact_id"], phone=row["phone_e164"]),
                         line=w.line(s["tenant"]))
    if dialing:
        in_client(w, s, lambda tx: attempts.start_dialing(tx, aid))
    return aid, row["phone_e164"]


class RefusalScope(unittest.TestCase):
    """2026-10-14 ユーザー決定：(b) 断った相手の行の番号すべてを止める（必ず）＋(c) 依頼主ごとの設定で選べば管理番号でも止める。"""

    def setUp(self):
        self.w = World()
        self.s = self.w.setup()
        self.run2 = self.w.run(self.s["tenant"], self.s["client"], self.s["business"], self.s["version"])
        self.run3 = self.w.run(self.s["tenant"], self.s["client"], self.s["business"], self.s["version"])

    def refuse_on(self, task_id, reason="refused_in_call"):
        aid, phone = claim_row(self.w, self.s, task_id, dialing=True)
        return in_client(self.w, self.s, lambda tx: dnc.register(tx, phone, reason, source_attempt_id=aid))

    def outcome(self, result):
        return [(r["outcome"], r["reason_code"]) for r in result["rows"]]

    def test_by_default_the_rows_numbers_are_stopped_but_not_the_ref(self):
        r = import_rows(self.w, self.s, self.s["run"], [("A-1", "架空商事", fictional_jp_fixed())])
        got = self.refuse_on(r["rows"][0]["task_id"])
        self.assertEqual((len(got["numbers"]), got["client_refs"]), (1, []))
        again = import_rows(self.w, self.s, self.run2, [("A-1", "架空商事", fictional_jp_fixed())])
        self.assertEqual(self.outcome(again), [("registered", None)])   # 管理番号が相手を表すかは分からない

    def test_stable_ref_stops_the_same_party_with_another_number(self):
        set_client(self.w, self.s, "dnc.client_ref_is_stable_id", True)
        r1 = import_rows(self.w, self.s, self.s["run"], [("A-1", "架空商事", fictional_jp_fixed())])
        r2 = import_rows(self.w, self.s, self.run2, [("A-1", "架空商事 支店", fictional_jp_fixed()),
                                                     ("B-1", "架空工業", fictional_jp_fixed())])
        other_claim, _ = claim_row(self.w, self.s, r2["rows"][0]["task_id"])        # 別の実施回の同じ相手の予約
        unrelated_claim, _ = claim_row(self.w, self.s, r2["rows"][1]["task_id"])
        got = self.refuse_on(r1["rows"][0]["task_id"])
        self.assertEqual((got["client_refs"], got["released_claims"]), (["A-1"], 1))
        state = lambda aid: in_client(self.w, self.s, lambda tx: tx.one(
            "SELECT state FROM salescall.attempts WHERE attempt_id = %s", (aid,)))["state"]
        self.assertEqual((state(other_claim), state(unrelated_claim)), ("released", "claimed"))
        later = import_rows(self.w, self.s, self.run3, [("A-1", "架空商事", fictional_jp_fixed()),
                                                        ("B-2", "架空物産", fictional_jp_fixed())])
        self.assertEqual(self.outcome(later), [("stopped", "dnc_client_ref"), ("registered", None)])
        # 門も同じ判断を呼ぶ（別の実施回にもう入っていた同じ相手の行は、かけない）
        number = normalize(fictional_jp_fixed())
        hit = in_client(self.w, self.s, lambda tx: target_policy.block_reason(
            tx, number, client_ref="A-1", tenant_id=self.s["tenant"], client_id=self.s["client"],
            business_id=self.s["business"]))
        self.assertEqual(hit[0], "dnc_client_ref")

    def test_turning_the_setting_off_keeps_existing_ref_entries_until_a_person_revokes(self):
        set_client(self.w, self.s, "dnc.client_ref_is_stable_id", True)
        r = import_rows(self.w, self.s, self.s["run"], [("A-1", "架空商事", fictional_jp_fixed())])
        self.refuse_on(r["rows"][0]["task_id"])
        set_client(self.w, self.s, "dnc.client_ref_is_stable_id", False)
        rows = [("A-1", "架空商事", fictional_jp_fixed())]
        self.assertEqual(self.outcome(import_rows(self.w, self.s, self.run2, rows)), [("stopped", "dnc_client_ref")])
        ref_entry = in_client(self.w, self.s, lambda tx: tx.one(
            "SELECT entry_id FROM salescall.dnc_entries WHERE client_ref = 'A-1'"))["entry_id"]
        in_client(self.w, self.s, lambda tx: dnc.revoke(tx, ref_entry, "依頼主の管理番号の付け方が変わった（試験）"),
                  actor=OPERATOR)
        self.assertEqual(self.outcome(import_rows(self.w, self.s, self.run2, rows)), [("registered", None)])

    def test_wrong_party_stays_on_that_number(self):
        set_client(self.w, self.s, "dnc.client_ref_is_stable_id", True)
        r = import_rows(self.w, self.s, self.s["run"], [("A-1", "架空商事", fictional_jp_fixed())])
        got = self.refuse_on(r["rows"][0]["task_id"], reason="wrong_party")
        self.assertEqual((len(got["numbers"]), got["client_refs"]), (1, []))
        again = import_rows(self.w, self.s, self.run2, [("A-1", "架空商事", fictional_jp_fixed())])
        self.assertEqual(self.outcome(again), [("registered", None)])   # 相手は断っていない

    def test_refusing_twice_keeps_one_entry_each(self):
        set_client(self.w, self.s, "dnc.client_ref_is_stable_id", True)
        r = import_rows(self.w, self.s, self.s["run"], [("A-1", "架空商事", fictional_jp_fixed())])
        self.refuse_on(r["rows"][0]["task_id"])
        in_client(self.w, self.s, lambda tx: tx.execute(
            "UPDATE salescall.attempts SET state = 'no_answer' WHERE task_id = %s AND state = 'dialing'",
            (r["rows"][0]["task_id"],)))
        self.refuse_on(r["rows"][0]["task_id"], reason="refused_found_after")
        n = in_client(self.w, self.s, lambda tx: tx.one(
            "SELECT count(*) FILTER (WHERE phone_e164 IS NOT NULL) AS numbers,"
            "       count(*) FILTER (WHERE client_ref IS NOT NULL) AS refs FROM salescall.dnc_entries"))
        self.assertEqual((n["numbers"], n["refs"]), (1, 1))

    def test_database_refuses_bad_entries(self):
        for cols, vals in ((("client_ref", "reason"), ("A-1", "wrong_party")),   # 番号違いで相手を止めない
                           (("reason",), ("manual",)),                           # 番号も管理番号も無い
                           (("phone_e164", "client_ref", "reason"), ("+81300000001", "A-1", "manual"))):  # 両方
            with self.subTest(cols=cols), self.assertRaises(Rejected):
                in_client(self.w, self.s, lambda tx: tx.execute(
                    f"INSERT INTO salescall.dnc_entries (tenant_id, client_id, registered_by, {', '.join(cols)})"
                    f" VALUES (%s, %s, '試験', {', '.join(['%s'] * len(vals))})",
                    (self.s["tenant"], self.s["client"], *vals)))


if __name__ == "__main__":
    unittest.main()
