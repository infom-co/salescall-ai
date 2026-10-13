"""設定の定義と値の試験（DESIGN.md 8章・表の作り 0005）。
ユーザー：「忘れないように、最適だと思う値を入れておいた方がよくない？」⇒ 定義のある設定には、必ずシステムの値がある
（無ければ、この試験が落ちる）。値は形が確かめられ、仮の値は根拠と見直しの合図つきで一覧にできる。"""

from __future__ import annotations

import json
import unittest
import uuid

from salescall.core import settings
from salescall.core.access import Rejected
from tests.support import OPERATOR, SYSTEM, World


def sys_tx(work):
    return World().a.in_system(SYSTEM, work)


class EveryDefinitionHasAValue(unittest.TestCase):
    def test_no_definition_is_left_empty(self):
        missing = sys_tx(lambda tx: [r["key"] for r in tx.all(
            "SELECT d.key FROM salescall.setting_definitions d"
            " WHERE NOT EXISTS (SELECT 1 FROM salescall.setting_values v WHERE v.key = d.key AND v.scope = 'system')"
            " ORDER BY d.key")])
        self.assertEqual(missing, [])

    def test_current_values_have_the_right_shape(self):
        bad = sys_tx(lambda tx: [r["key"] for r in tx.all(
            "SELECT v.key FROM (SELECT DISTINCT ON (key) * FROM salescall.setting_values WHERE scope = 'system'"
            "  ORDER BY key, value_id DESC) v JOIN salescall.setting_definitions d USING (key)"
            " WHERE NOT salescall.setting_value_ok(d.value_type, v.value)")])
        self.assertEqual(bad, [])

    def test_provisional_values_carry_rationale_and_review(self):
        rows = sys_tx(settings.provisional)
        self.assertGreater(len(rows), 30)
        for r in rows:
            self.assertTrue(r["rationale"].strip() and r["review_when"].strip(), r["key"])

    def test_reading_examples(self):
        self.assertEqual(sys_tx(lambda tx: settings.system_value(tx, "gate.claim_lifetime")), 30)
        hours = sys_tx(lambda tx: settings.system_value(tx, "calling.hours"))
        self.assertEqual((hours["mon"], hours["sun"], hours["holiday"]), ([["09:00", "18:00"]], [], []))

    def test_missing_value_is_an_error_not_a_default(self):
        with self.assertRaises(settings.SettingMissing):
            sys_tx(lambda tx: settings.system_value(tx, "no.such_setting"))


class Shapes(unittest.TestCase):
    def test_bad_values_are_rejected(self):
        for key, value in [("gate.claim_lifetime", 2.5), ("gate.claim_lifetime", 0), ("gate.claim_lifetime", "abc"),
                           ("quality.max_complaint", 1.5), ("quality.max_complaint", "0.1"),
                           ("quality.stage.sizes", []), ("quality.stage.sizes", [20, "x"]),
                           ("calling.hours", {"mon": [["09:00", "18:00"]]}),
                           ("calling.hours", {d: [["18:00", "09:00"]] for d in
                                              ("mon", "tue", "wed", "thu", "fri", "sat", "sun", "holiday")}),
                           ("calling.hours", {d: "09:00" for d in
                                              ("mon", "tue", "wed", "thu", "fri", "sat", "sun", "holiday")})]:
            with self.subTest(key=key, value=value), self.assertRaises(Rejected):
                sys_tx(lambda tx: settings.set_system_value(tx, key, value, status="provisional",
                                                            rationale="試験", review_when="試験"))

    def test_system_only_cannot_be_set_lower(self):
        w = World(); t = w.tenant(); c = w.client(t)
        with self.assertRaises(Rejected):
            w.a.in_client(OPERATOR, t, c, lambda tx: tx.execute(
                "INSERT INTO salescall.setting_values (key, scope, tenant_id, client_id, value, status, rationale,"
                " review_when, set_by) VALUES ('gate.claim_lifetime', 'client', %s, %s, '60', 'decided', 'x', 'x', 'x')",
                (t, c)))
        # 範囲の重なりで合成する設定（時間帯）は、依頼主が狭められる
        w.a.in_client(OPERATOR, t, c, lambda tx: tx.execute(
            "INSERT INTO salescall.setting_values (key, scope, tenant_id, client_id, value, status, rationale,"
            " review_when, set_by) VALUES ('calling.hours', 'client', %s, %s, %s, 'decided', '依頼主の営業時間', 'x', 'x')",
            (t, c, '{"mon": [["10:00","17:00"]], "tue": [], "wed": [], "thu": [], "fri": [], "sat": [], "sun": [], "holiday": []}')))

    def test_value_needs_a_definition(self):
        with self.assertRaises(Rejected):
            sys_tx(lambda tx: settings.set_system_value(tx, "undefined.key", 1, status="provisional",
                                                        rationale="x", review_when="x"))


class AppendOnly(unittest.TestCase):
    def test_latest_is_current_and_history_is_kept(self):
        key = "gate.denied_retry_wait"
        before = sys_tx(lambda tx: settings.system_value(tx, key))
        try:
            sys_tx(lambda tx: settings.set_system_value(tx, key, 600, status="decided", rationale="試験で変えた",
                                                        review_when="試験"))
            self.assertEqual(sys_tx(lambda tx: settings.system_value(tx, key)), 600)
            self.assertNotIn(key, [r["key"] for r in sys_tx(settings.provisional)])  # 確定は仮の一覧に出ない
        finally:
            sys_tx(lambda tx: settings.set_system_value(tx, key, before, status="provisional", rationale="試験のあと元へ",
                                                        review_when="試験"))
        with self.assertRaises(Rejected):
            sys_tx(lambda tx: tx.execute("UPDATE salescall.setting_values SET value = '1' WHERE key = %s", (key,)))

    def test_client_scope_values_are_not_visible_to_other_clients(self):
        w = World(); t = w.tenant(); c1 = w.client(t); c2 = w.client(t)
        w.a.in_client(OPERATOR, t, c1, lambda tx: tx.execute(
            "INSERT INTO salescall.setting_values (key, scope, tenant_id, client_id, value, status, rationale,"
            " review_when, set_by) VALUES ('retry.max_attempts_per_task', 'client', %s, %s, '3', 'decided', 'x', 'x', 'x')",
            (t, c1)))
        other = World().tenant()
        n = w.a.in_tenant(OPERATOR, other, lambda tx: tx.one(
            "SELECT count(*) AS n FROM salescall.setting_values WHERE scope <> 'system'"))["n"]
        self.assertEqual(n, 0)


def put(w, t, c, key, value, *, scope="client", business=None):
    w.a.in_client(OPERATOR, t, c, lambda tx: tx.execute(
        "INSERT INTO salescall.setting_values (key, scope, tenant_id, client_id, business_id, value, status, rationale,"
        " review_when, set_by) VALUES (%s, %s, %s, %s, %s, %s, 'decided', '依頼主の設定（試験）', '試験', '試験')",
        (key, scope, t, c, business, json.dumps(value))))


def resolved(w, t, c, key, business=None):
    return w.a.in_client(SYSTEM, t, c, lambda tx: settings.resolve(
        tx, key, tenant_id=t, client_id=c, business_id=business))


class UserDecisions(unittest.TestCase):
    """2026-10-13 ユーザー決定の値が入っていること。"""

    def test_values(self):
        v = lambda k: sys_tx(lambda tx: settings.system_value(tx, k))
        self.assertEqual((v("retry.max_attempts_per_task"), v("quality.mock.calls"), v("stop.ai_rate_limit.backoff")),
                         (3, 10, [10, 30, 120, 600, 1800]))
        decided = {r["key"] for r in sys_tx(settings.provisional)}
        self.assertTrue({"retry.max_attempts_per_task", "quality.mock.calls", "stop.ai_rate_limit.backoff"}.isdisjoint(decided))


class PerClientSettings(unittest.TestCase):
    """依頼主タブごとに設定できる。ただし、システムの上限より緩められない（合成のしかたで決まる。8章）。"""

    def setUp(self):
        self.w = World(); self.t = self.w.tenant(); self.c = self.w.client(self.t)

    def test_system_value_when_client_has_none(self):
        self.assertEqual(resolved(self.w, self.t, self.c, "retry.max_attempts_per_task"), 3)

    def test_client_can_tighten_but_not_loosen_the_upper_limit(self):
        put(self.w, self.t, self.c, "retry.max_attempts_per_task", 2)
        self.assertEqual(resolved(self.w, self.t, self.c, "retry.max_attempts_per_task"), 2)
        c2 = self.w.client(self.t)
        put(self.w, self.t, c2, "retry.max_attempts_per_task", 5)
        self.assertEqual(resolved(self.w, self.t, c2, "retry.max_attempts_per_task"), 3)  # 5 は効かない

    def test_intervals_and_match_ratios_only_get_stricter(self):
        put(self.w, self.t, self.c, "gate.unknown_result_min_interval", 3600)       # 短くしようとしても
        self.assertEqual(resolved(self.w, self.t, self.c, "gate.unknown_result_min_interval"), 86400)
        put(self.w, self.t, self.c, "quality.mock.min_value_match", 0.99)
        self.assertEqual(resolved(self.w, self.t, self.c, "quality.mock.min_value_match"), 0.99)
        put(self.w, self.t, self.c, "quality.mock.calls", 20)
        self.assertEqual(resolved(self.w, self.t, self.c, "quality.mock.calls"), 20)

    def test_calling_hours_are_the_overlap(self):
        put(self.w, self.t, self.c, "calling.hours", {
            "mon": [["08:00", "12:00"], ["13:00", "20:00"]], "tue": [["10:00", "17:00"]], "wed": [], "thu": [["09:00", "18:00"]],
            "fri": [["09:00", "18:00"]], "sat": [["09:00", "18:00"]], "sun": [], "holiday": []})
        h = resolved(self.w, self.t, self.c, "calling.hours")
        self.assertEqual(h["mon"], [["09:00", "12:00"], ["13:00", "18:00"]])  # システムの 9〜18時 の内側だけ
        self.assertEqual((h["tue"], h["wed"], h["sat"]), ([["10:00", "17:00"]], [], []))  # 土曜はシステムがかけない

    def test_business_layer_and_tenant_layer(self):
        b, _ = self.w.business_with_version(self.t, self.c)
        self.w.a.in_tenant(OPERATOR, self.t, lambda tx: tx.execute(
            "INSERT INTO salescall.setting_values (key, scope, tenant_id, value, status, rationale, review_when, set_by)"
            " VALUES ('retry.max_attempts_per_task', 'tenant', %s, '2', 'decided', '契約者の決まり', '試験', '試験')", (self.t,)))
        put(self.w, self.t, self.c, "retry.max_attempts_per_task", 1, scope="business", business=b)
        self.assertEqual(resolved(self.w, self.t, self.c, "retry.max_attempts_per_task"), 2)
        self.assertEqual(resolved(self.w, self.t, self.c, "retry.max_attempts_per_task", business=b), 1)

    def test_shared_equipment_values_are_system_only(self):
        for key in ("stop.ai_rate_limit.backoff", "gate.claim_lifetime", "line.lease", "gate.ai_tpm_limit"):
            with self.subTest(key=key), self.assertRaises(Rejected):
                put(self.w, self.t, self.c, key, 1 if key != "stop.ai_rate_limit.backoff" else [5])

    def test_other_client_of_the_same_tenant_cannot_see_the_value(self):
        put(self.w, self.t, self.c, "retry.max_attempts_per_task", 1)
        c2 = self.w.client(self.t)
        n = self.w.a.in_client(OPERATOR, self.t, c2, lambda tx: tx.one(
            "SELECT count(*) AS n FROM salescall.setting_values WHERE scope = 'client'"))["n"]
        self.assertEqual(n, 0)
        self.assertEqual(resolved(self.w, self.t, c2, "retry.max_attempts_per_task"), 3)


if __name__ == "__main__":
    unittest.main()
