"""依頼者・業務・実施回・回線を作る・直す部品の試験（Intent.md 4章 A・C・5-3 (4)(11)。core/registry.py・表の作り 0051）。"""

from __future__ import annotations

import unittest

from salescall.core import business_settings, gate, registry
from salescall.core.access import Rejected
from tests.support import ADMIN, OPERATOR, SYSTEM, fictional_caller
from tests.test_gate import Base


class Clients(Base):
    def test_create_rename_and_the_change_is_recorded(self):
        t = self.w.tenant()
        c = self.w.a.in_tenant(OPERATOR, t, lambda tx: registry.create_client(tx, "架空の依頼者A"))
        self.w.a.in_tenant(OPERATOR, t, lambda tx: registry.rename_client(tx, c, "架空の依頼者B"))
        got = self.w.a.in_tenant(OPERATOR, t, lambda tx: tx.all(
            "SELECT kind, old_name, new_name, changed_by FROM salescall.name_changes WHERE target_id = %s", (c,)))
        self.assertEqual(got, [{"kind": "client", "old_name": "架空の依頼者A", "new_name": "架空の依頼者B",
                                "changed_by": OPERATOR.name}])
        with self.assertRaises(Rejected):                                  # 名前のほかは直せない
            self.w.a.in_tenant(OPERATOR, t, lambda tx: tx.execute(
                "UPDATE salescall.clients SET created_at = now() - interval '1 day' WHERE client_id = %s", (c,)))
        with self.assertRaises(Rejected):                                  # 名前を直せるのは管理者か契約者だけ
            self.w.a.in_tenant(SYSTEM, t, lambda tx: registry.rename_client(tx, c, "システムが直した"))
        with self.assertRaises(ValueError):
            self.w.a.in_tenant(OPERATOR, t, lambda tx: registry.create_client(tx, "  "))

    def test_an_inactive_client_is_not_called_until_it_is_active_again(self):
        """無効にした依頼者は、門が通さない。まだ発信していない予約は取り消す。有効に戻せば、またかける。"""
        s = self.ready()
        c = self.claim(s)
        self.assertIsInstance(c, gate.Claim)
        self.tx(s, lambda tx: registry.set_client_active(tx, False, reason="試験：契約が終わった"), OPERATOR)
        state = self.tx(s, lambda tx: tx.one("SELECT state FROM salescall.attempts WHERE attempt_id = %s",
                                             (c.attempt_id,)))["state"]
        self.assertEqual(state, "released")
        self.assertEqual(self.reason(s), "client_inactive")
        with self.assertRaises(Rejected):                                  # システムの処理は、依頼者を有効・無効にできない
            self.tx(s, lambda tx: registry.set_client_active(tx, True, reason="システム"), SYSTEM)
        active = lambda: self.w.a.in_system(ADMIN, lambda tx: tx.one(   # noqa: E731
            "SELECT salescall.client_active(%s) AS a", (s["client"],))["a"])
        self.assertIs(active(), False)                                     # 管理者の全体の取引からも、無効と見える
        other = self.w.tenant()
        self.assertIsNone(self.w.a.in_tenant(OPERATOR, other, lambda tx: tx.one(   # ほかの契約者には答えない
            "SELECT salescall.client_active(%s) AS a", (s["client"],))["a"]))
        self.tx(s, lambda tx: registry.set_client_active(tx, True, reason="試験：再開"), ADMIN)
        self.assertIsInstance(self.claim(s), gate.Claim)


class BusinessesAndRuns(Base):
    def test_copy_a_business_and_create_a_run_with_a_version(self):
        s = self.ready()
        b, v = self.tx(s, lambda tx: registry.copy_business(tx, s["version"], "架空の業務（複製）"), OPERATOR)
        self.assertNotEqual(b, s["business"])
        same = self.tx(s, lambda tx: (business_settings.load(tx, s["version"]).raw, business_settings.load(tx, v).raw))
        self.assertEqual(same[0], same[1])                                 # 中身は同じ・もとの業務は変えない
        run = self.tx(s, lambda tx: registry.create_run(tx, b, "架空の実施回", v, reason="試験：最初の版"), OPERATOR)
        cur = self.tx(s, lambda tx: tx.one("SELECT salescall.run_current_version(%s) AS v", (run,)))["v"]
        self.assertEqual(cur, v)
        with self.assertRaises(Rejected):                                  # ほかの業務の版は割り当てられない
            self.tx(s, lambda tx: registry.create_run(tx, b, "取り違え", s["version"], reason="試験"), OPERATOR)
        self.tx(s, lambda tx: registry.rename_run(tx, run, "架空の実施回（直した）"), OPERATOR)
        self.tx(s, lambda tx: registry.rename_business(tx, b, "架空の業務（直した）"), OPERATOR)
        kinds = self.tx(s, lambda tx: [r["kind"] for r in tx.all(
            "SELECT kind FROM salescall.name_changes WHERE target_id IN (%s, %s) ORDER BY change_id", (run, b))])
        self.assertEqual(kinds, ["run", "business"])


class Lines(Base):
    def test_the_caller_number_is_required_checked_unique_and_fixed(self):
        t = self.w.tenant()
        mk = lambda caller: self.w.a.in_tenant(OPERATOR, t, lambda tx: registry.create_line(   # noqa: E731
            tx, "架空の回線", method="rakuten_link", caller=caller))
        for bad in ("+447700900001", "0120-000-000", "x"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                mk(bad)
        number = fictional_caller()
        line = mk(number)
        with self.assertRaises(Rejected):                                  # 同じ番号の回線は2つ作れない
            mk(number)
        with self.assertRaises(Rejected):                                  # 発信番号は変えられない
            self.w.a.in_tenant(OPERATOR, t, lambda tx: tx.execute(
                "UPDATE salescall.lines SET caller_e164 = '+817000000000' WHERE line_id = %s", (line,)))
        with self.assertRaises(Rejected):                                  # 発信番号の無い回線は作れない
            self.w.a.in_tenant(OPERATOR, t, lambda tx: tx.execute(
                "INSERT INTO salescall.lines (tenant_id, name, method) VALUES (%s, 'x', 'rakuten_link')", (t,)))

    def test_a_retired_line_is_not_used_and_its_claims_are_released(self):
        s = self.ready()
        c = self.claim(s)
        self.w.a.in_tenant(OPERATOR, s["tenant"], lambda tx: registry.retire_line(tx, s["line"], reason="試験：端末を返した"))
        self.assertEqual(self.tx(s, lambda tx: tx.one("SELECT state FROM salescall.attempts WHERE attempt_id = %s",
                                                      (c.attempt_id,)))["state"], "released")
        self.assertEqual(self.reason(s), "line_retired")
        with self.assertRaises(Rejected):                                  # 印は外せない
            self.w.a.in_tenant(OPERATOR, s["tenant"], lambda tx: tx.execute(
                "UPDATE salescall.lines SET retired_at = NULL, retired_reason = NULL WHERE line_id = %s", (s["line"],)))
        with self.assertRaises(Rejected):                                  # 使わなくなった回線は、依頼者の割り当ても替えない
            self.w.a.in_tenant(OPERATOR, s["tenant"], lambda tx: registry.assign_line(tx, s["line"], self.w.client(s["tenant"])))
        self.w.a.in_tenant(OPERATOR, s["tenant"], lambda tx: registry.rename_line(tx, s["line"], "返した端末"))   # 名前は直せる

    def test_assigning_a_line_to_another_client_releases_its_claims(self):
        s = self.ready()
        c = self.claim(s)
        other = self.w.client(s["tenant"])
        self.w.a.in_tenant(OPERATOR, s["tenant"], lambda tx: registry.assign_line(tx, s["line"], other))
        self.assertEqual(self.tx(s, lambda tx: tx.one("SELECT state FROM salescall.attempts WHERE attempt_id = %s",
                                                      (c.attempt_id,)))["state"], "released")
        self.assertEqual(self.reason(s), "line_other_client")


if __name__ == "__main__":
    unittest.main()
