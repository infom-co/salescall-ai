"""発信禁止台帳（Q3）と「その番号へかけてよいか」（number_policy）の試験。

⚠️ 番号は架空のもの（局番のあとが 0000 の、実在しない番号）。
"""

from __future__ import annotations

import json
import unittest

from salescall.core import dnc, number_policy
from salescall.core.access import Rejected
from salescall.core.phone import normalize
from tests.support import OPERATOR, SYSTEM, World, fictional_jp_fixed


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
    return in_client(w, s, lambda tx: number_policy.block_reason(
        tx, normalize(raw), tenant_id=s["tenant"], client_id=s["client"], business_id=s["business"]))


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


if __name__ == "__main__":
    unittest.main()
