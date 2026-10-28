"""設定の定義と値の試験（DESIGN.md 8章・表の作り 0005）。
ユーザー：「忘れないように、最適だと思う値を入れておいた方がよくない？」⇒ 定義のある設定には、必ずシステムの値がある
（無ければ、この試験が落ちる）。値は形が確かめられ、仮の値は根拠と見直しの合図つきで一覧にできる。"""

from __future__ import annotations

import json
import unittest
import uuid

from salescall.core import settings
from salescall.core.access import Rejected
from tests.support import ADMIN, OPERATOR, SYSTEM, World, owner_access


ALL_DAY = {d: [["00:00", "23:59"]] for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun", "holiday")}


def sys_tx(work):
    """全体の取引（システムの値を変えられるのは管理者だけ。0017）。"""
    return World().a.in_system(ADMIN, work)


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
        hours = sys_tx(lambda tx: settings.initial_value(tx, "calling.hours"))
        self.assertEqual((hours["mon"], hours["sun"], hours["holiday"]), ([["09:00", "18:00"]], [], []))
        with self.assertRaises(ValueError):   # 依頼者・業務ごとに変えられる設定を、システムの値だけで読ませない
            sys_tx(lambda tx: settings.system_value(tx, "calling.hours"))

    def test_missing_value_is_an_error_not_a_default(self):
        with self.assertRaises(settings.SettingMissing):
            sys_tx(lambda tx: settings.system_value(tx, "no.such_setting"))


class Shapes(unittest.TestCase):
    def test_bad_values_are_rejected(self):
        for key, value in [("gate.claim_lifetime", 2.5), ("gate.claim_lifetime", 0), ("gate.claim_lifetime", "abc"),
                           ("quality.max_complaint", 1.5), ("quality.max_complaint", "0.1"),
                           ("quality.stage.sizes", []), ("quality.stage.sizes", [20, "x"]),
                           ("callback.call_alternate_number", 1), ("callback.call_alternate_number", "true"),
                           ("calling.hours", {"mon": [["09:00", "18:00"]]}),
                           ("calling.hours", {d: [["18:00", "09:00"]] for d in
                                              ("mon", "tue", "wed", "thu", "fri", "sat", "sun", "holiday")}),
                           ("calling.hours", {d: "09:00" for d in
                                              ("mon", "tue", "wed", "thu", "fri", "sat", "sun", "holiday")}),
                           ("callee.business_hours", ["09:00"]), ("callee.business_hours", ["09:00", "09:00"]),
                           ("callee.business_hours", ["9:00", "19:00"]), ("callee.business_hours", ["24:00", "09:00"]),
                           ("callee.business_hours", "09:00-19:00"), ("callee.business_hours", [9, 19]),
                           ("retry.time_band_boundaries", []), ("retry.time_band_boundaries", [15, 12]),
                           ("retry.time_band_boundaries", [12, 12]), ("retry.time_band_boundaries", [0, 12]),
                           ("retry.time_band_boundaries", [12, 24]), ("retry.time_band_boundaries", [11.5]),
                           ("line.test_number", "0312345678"), ("line.test_number", "+81-3-1234-5678"),
                           ("line.test_number", "+0312345678"), ("line.test_number", 81312345678),
                           ("line.test_number", "")]:
            with self.subTest(key=key, value=value), self.assertRaises(Rejected):
                sys_tx(lambda tx: settings.set_system_value(tx, key, value, status="provisional",
                                                            rationale="試験", review_when="試験"))

    def test_a_phone_is_the_normalized_number_or_none(self):
        """テスト番号（0133・0134）：そろえた番号（E.164）か、まだ無い（null）。契約者ごとに入れる。"""
        for value in ("+81312345678", None):
            with self.subTest(value=value):
                sys_tx(lambda tx: settings.set_system_value(tx, "line.test_number", value, status="provisional",
                                                            rationale="試験", review_when="試験"))
        self.assertIsNone(sys_tx(lambda tx: settings.initial_value(tx, "line.test_number")))

    def test_cannot_be_set_finer_than_declared(self):
        w = World(); t = w.tenant(); c = w.client(t)
        b, _ = w.business_with_version(t, c)
        for key, scope, value in [("gate.claim_lifetime", "client", 60), ("gate.claim_lifetime", "tenant", 60),
                                  ("gate.cross_client_interval", "client", 60), ("stop.ai.stop_after_failures", "client", 1),
                                  ("line.max_dials_per_day", "business", 10), ("calling.hours_limit", "business", ALL_DAY)]:
            with self.subTest(key=key, scope=scope), self.assertRaises(Rejected):
                if scope == "tenant":
                    w.a.in_tenant(OPERATOR, t, lambda tx: tx.execute(
                        "INSERT INTO salescall.setting_values (key, scope, tenant_id, value, status, rationale, review_when,"
                        " set_by) VALUES (%s, 'tenant', %s, %s, 'decided', 'x', 'x', 'x')", (key, t, json.dumps(value))))
                else:
                    put(w, t, c, key, value, scope=scope, business=b if scope == "business" else None)
        # 範囲の重なりで合成する設定（時間帯）は、依頼者ごとの設定で狭められる
        w.a.in_client(OPERATOR, t, c, lambda tx: tx.execute(
            "INSERT INTO salescall.setting_values (key, scope, tenant_id, client_id, value, status, rationale,"
            " review_when, set_by) VALUES ('calling.hours', 'client', %s, %s, %s, 'decided', '依頼者の営業時間', 'x', 'x')",
            (t, c, '{"mon": [["10:00","17:00"]], "tue": [], "wed": [], "thu": [], "fri": [], "sat": [], "sun": [], "holiday": []}')))

    def test_business_hours_may_cross_midnight(self):
        """営業時間は日をまたいでよい（深夜営業 18:00〜翌03:00。2026-10-15 ユーザー）。依頼者ごとに入れられる。"""
        w = World(); t = w.tenant(); c = w.client(t)
        w.a.in_client(OPERATOR, t, c, lambda tx: tx.execute(
            "INSERT INTO salescall.setting_values (key, scope, tenant_id, client_id, value, status, rationale,"
            " review_when, set_by) VALUES ('callee.business_hours', 'client', %s, %s, '[\"18:00\", \"03:00\"]',"
            " 'decided', '深夜営業のお店', 'x', 'x')", (t, c)))
        got = w.a.in_client(OPERATOR, t, c, lambda tx: settings.resolve(tx, "callee.business_hours", tenant_id=t,
                                                                         client_id=c, business_id=None))
        self.assertEqual(got, ["18:00", "03:00"])

    def test_value_needs_a_definition(self):
        with self.assertRaises(Rejected):
            sys_tx(lambda tx: settings.set_system_value(tx, "undefined.key", 1, status="provisional",
                                                        rationale="x", review_when="x"))


class SystemValuesByAdmin(unittest.TestCase):
    """システムの値（すべての契約者に効く）を変えられるのは、管理者だけ（CL-3 の 4-1f の3）。"""

    def test_only_admin(self):
        a = World().a
        for actor in (OPERATOR, SYSTEM):
            with self.subTest(actor=actor.kind), self.assertRaises(Rejected) as e:
                a.in_system(actor, lambda tx: settings.set_system_value(tx, "gate.denied_retry_wait", 300,
                                                                      status="provisional", rationale="試験",
                                                                      review_when="試験"))
            self.assertEqual(e.exception.constraint, "system_values_by_admin")
        from decimal import Decimal
        from salescall.core import usage
        with self.assertRaises(Rejected):
            a.in_system(OPERATOR, lambda tx: usage.set_price(tx, source="line", service="rakuten_link", tier=None,
                                                             unit="seconds", yen_per_unit=Decimal(0),
                                                             valid_from="2026-01-02", note="試験"))
        w = World(); t = w.tenant(); c = w.client(t)                   # 依頼者ごとの値は、契約者が入れられる
        w.a.in_client(OPERATOR, t, c, lambda tx: tx.execute(
            "INSERT INTO salescall.setting_values (key, scope, tenant_id, client_id, value, status, rationale, review_when,"
            " set_by) VALUES ('retry.max_attempts_per_task', 'client', %s, %s, '2', 'decided', '試験', '試験', '試験')",
            (t, c)))


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
        " review_when, set_by) VALUES (%s, %s, %s, %s, %s, %s, 'decided', '依頼者の設定（試験）', '試験', '試験')",
        (key, scope, t, c, business, json.dumps(value))))


def resolved(w, t, c, key, business=None):
    return w.a.in_client(SYSTEM, t, c, lambda tx: settings.resolve(
        tx, key, tenant_id=t, client_id=c, business_id=business))


class UserDecisions(unittest.TestCase):
    """2026-10-13 ユーザー決定の値が入っていること。"""

    def test_values(self):
        v = lambda k: sys_tx(lambda tx: settings.initial_value(tx, k))
        self.assertEqual((v("retry.max_attempts_per_task"), v("quality.mock.calls"), v("stop.ai_rate_limit.backoff")),
                         (3, 10, [10, 30, 120, 600, 1800]))
        decided = {r["key"] for r in sys_tx(settings.provisional)}
        self.assertTrue({"retry.max_attempts_per_task", "quality.mock.calls", "stop.ai_rate_limit.backoff",
                         "calling.hours_limit", "call.hangup_after_decline_wait", "call.hangup_after_close_wait",
                         "callback.call_alternate_number", "calling.allow_toll_free", "calling.allow_navi_dial",
                         "dnc.client_ref_is_stable_id", "call.silence_prompt_after", "conversation.ask_back_required",
                         "conversation.recheck_required", "callee.business_hours",
                         "retry.ask_number_again_max"}.isdisjoint(decided))
        self.assertEqual((v("retry.ask_number_again_max"), v("retry.time_band_boundaries")), (3, [12, 15]))
        self.assertEqual(v("callee.business_hours"), ["09:00", "19:00"])
        self.assertEqual((v("call.silence_prompt_after"), v("conversation.ask_back_required"),
                          v("conversation.recheck_required")), (5, 2, 1))
        self.assertEqual((v("calling.allow_toll_free"), v("calling.allow_navi_dial"), v("dnc.client_ref_is_stable_id")),
                         (True, False, False))
        self.assertEqual((v("call.hangup_after_decline_wait"), v("call.hangup_after_close_wait"),
                          v("callback.call_alternate_number")), (7, 20, True))
        # 2026-10-24：受付の保留を待つ長さ（業務ごと・初め5分）・1日の上限のキーを休ませる長さ（30分。0131）
        self.assertEqual((v("call.hold_wait"), v("stop.ai_quota.rest")), (300, 1800))


class PerClientSettings(unittest.TestCase):
    """依頼者タブごとに設定できる。2026-10-13 ユーザー：「川下の方が強くないと、依頼主ごとの意味がない」
    ⇒ 業務 ＞ 依頼者 ＞ 契約者 ＞ システム の順で上書き。超えられない枠を持つ設定だけ、枠の内側に収める。"""

    def setUp(self):
        self.w = World(); self.t = self.w.tenant(); self.c = self.w.client(self.t)

    def detail(self, key, business=None, client=None):
        c = client or self.c
        return self.w.a.in_client(SYSTEM, self.t, c, lambda tx: settings.resolve_detail(
            tx, key, tenant_id=self.t, client_id=c, business_id=business))

    def test_system_value_when_client_has_none(self):
        self.assertEqual(self.detail("retry.max_attempts_per_task"), {"value": 3, "from": "system", "bounded_by": None})

    def test_client_value_wins_both_ways(self):
        put(self.w, self.t, self.c, "retry.max_attempts_per_task", 2)
        self.assertEqual(self.detail("retry.max_attempts_per_task")["value"], 2)
        c2 = self.w.client(self.t)
        put(self.w, self.t, c2, "retry.max_attempts_per_task", 5)
        self.assertEqual(self.detail("retry.max_attempts_per_task", client=c2), {"value": 5, "from": "client", "bounded_by": None})

    def test_quality_and_mock_values_follow_the_client(self):
        for key, value in [("quality.mock.calls", 5), ("quality.mock.min_value_match", 0.9),
                           ("quality.max_complaint", 0.05), ("quality.stage.sizes", [10, 50])]:
            with self.subTest(key=key):
                put(self.w, self.t, self.c, key, value)
                self.assertEqual(self.detail(key)["value"], value)

    def test_calling_hours_follow_the_client_inside_the_frame(self):
        """2026-10-13 ユーザー：日祝も含め、依頼者ごとに自由に。歯止めは深夜・早朝（毎日 7〜22時）だけ。"""
        put(self.w, self.t, self.c, "calling.hours", {
            "mon": [["06:00", "12:00"], ["13:00", "23:00"]], "tue": [["10:00", "17:00"]], "wed": [], "thu": [["09:00", "18:00"]],
            "fri": [["09:00", "18:00"]], "sat": [["09:00", "18:00"]], "sun": [["10:00", "16:00"]], "holiday": [["10:00", "16:00"]]})
        d = self.detail("calling.hours")
        h = d["value"]
        self.assertEqual(h["mon"], [["07:00", "12:00"], ["13:00", "22:00"]])   # 深夜・早朝の歯止めの内側に収まる
        self.assertEqual((h["tue"], h["wed"], h["sat"], h["sun"], h["holiday"]),
                         ([["10:00", "17:00"]], [], [["09:00", "18:00"]], [["10:00", "16:00"]], [["10:00", "16:00"]]))
        self.assertEqual((d["from"], d["bounded_by"]), ("client", "calling.hours_limit"))

    def test_calling_hours_inside_the_frame_are_untouched(self):
        put(self.w, self.t, self.c, "calling.hours", {d: [["08:00", "21:00"]] for d in
                                                      ("mon", "tue", "wed", "thu", "fri", "sat", "sun", "holiday")})
        self.assertIsNone(self.detail("calling.hours")["bounded_by"])

    def test_concurrent_calls_follow_the_client(self):
        """依頼者ごとの本数は上書き（全体の本数は、門が会話AIの枠と回線の数で絞る。写しの外枠は持たない＝15-2 の34）。"""
        put(self.w, self.t, self.c, "calling.max_concurrent_calls", 10)
        self.assertEqual(self.detail("calling.max_concurrent_calls"), {"value": 10, "from": "client", "bounded_by": None})

    def test_business_over_client_over_tenant(self):
        b, _ = self.w.business_with_version(self.t, self.c)
        self.w.a.in_tenant(OPERATOR, self.t, lambda tx: tx.execute(
            "INSERT INTO salescall.setting_values (key, scope, tenant_id, value, status, rationale, review_when, set_by)"
            " VALUES ('retry.max_attempts_per_task', 'tenant', %s, '2', 'decided', '契約者の決まり', '試験', '試験')", (self.t,)))
        self.assertEqual(self.detail("retry.max_attempts_per_task"), {"value": 2, "from": "tenant", "bounded_by": None})
        put(self.w, self.t, self.c, "retry.max_attempts_per_task", 5, scope="business", business=b)
        self.assertEqual(self.detail("retry.max_attempts_per_task", business=b)["value"], 5)

    def test_shared_equipment_and_harm_guards_are_system_only(self):
        for key, value in [("stop.ai_rate_limit.backoff", [5]), ("gate.claim_lifetime", 1), ("line.lease", 1),
                           ("quality.max_forbidden_speech", 3), ("quality.max_unknown_judgments", 3)]:
            with self.subTest(key=key), self.assertRaises(Rejected):
                put(self.w, self.t, self.c, key, value)

    def test_other_client_of_the_same_tenant_cannot_see_the_value(self):
        put(self.w, self.t, self.c, "retry.max_attempts_per_task", 1)
        c2 = self.w.client(self.t)
        n = self.w.a.in_client(OPERATOR, self.t, c2, lambda tx: tx.one(
            "SELECT count(*) AS n FROM salescall.setting_values WHERE scope = 'client'"))["n"]
        self.assertEqual(n, 0)
        self.assertEqual(self.detail("retry.max_attempts_per_task", client=c2)["value"], 3)


class Choice(unittest.TestCase):
    """値の形「選択」（0081・0082）：選べるのは定義の選択肢だけ。選択の形の設定だけが選択肢を持つ。"""

    def test_only_the_declared_choices_are_accepted(self):
        w = World()
        t = w.tenant()
        c = w.client(t)
        self.assertEqual(resolved(w, t, c, "judge.models"), "jev_and_sonnet")  # 2026-10-25 夜 ユーザー（CL-1 の 4-57）：Jev と Sonnet の組
        # （⛔ 前は 2026-10-18 の「当面は Sonnet だけ」のまま＝0082。0142 で新しい行が効く）
        def put_tenant(value):
            w.a.in_tenant(OPERATOR, t, lambda tx: tx.execute(
                "INSERT INTO salescall.setting_values (key, scope, tenant_id, value, status, rationale, review_when, set_by)"
                " VALUES ('judge.models', 'tenant', %s, %s, 'decided', '試験', '試験', '試験')", (t, json.dumps(value))))
        for bad in ("gpt", 1, ["sonnet"], None):
            with self.subTest(bad=bad), self.assertRaises(Rejected):
                put_tenant(bad)
        put_tenant("sonnet")                                                    # 契約者が選ぶ（2つの Sonnet の組）
        self.assertEqual(resolved(w, t, c, "judge.models"), "sonnet")
        for choices, value_type in ((None, "choice"), (["a"], "choice"), (["a", "b"], "integer")):
            with self.subTest(choices=choices, value_type=value_type), self.assertRaises(Rejected):
                owner_access().in_system(SYSTEM, lambda tx: tx.execute(
                    "INSERT INTO salescall.setting_definitions (key, merge_kind, value_type, meaning, design_ref, finest_scope,"
                    " choices) VALUES ('test.choice', 'override', %s, '試験', '試験', 'system', %s)", (value_type, choices)))


class ScopesDecidedByUser(unittest.TestCase):
    """2026-10-17 ユーザー：「1回線から1日 300本　依頼者ごと」「同じお客様への最小の間隔 5分　依頼者ごと」「依頼者をまたいだ間隔
    3日　契約者」「会話AI・回線の異常で止める回数と、解くまでの確かめ　契約者」「深夜・早朝の歯止め 7〜22時 など　依頼者ごと」。"""

    CLIENT = ("line.max_dials_per_day", "gate.same_contact_min_interval", "calling.hours_limit",
              "measure.sample_per_week",   # 抜き取る本数は依頼者ごと（2026-10-18 ユーザー）
              "callback.period_morning", "callback.period_forenoon", "callback.period_afternoon", "callback.period_evening",
              "callback.period_night",     # かけ直しの言葉ごとの幅は依頼者ごと（2026-10-19。0088）
              "gate.exact_promise_guard", "gate.exact_promise_guard_samples",  # 時刻を指定した約束の前の空け（0090・0091）
              "callback.exact_slot", "callback.exact_retry_gaps", "callback.exact_days")   # 時刻ちょうどの約束の回（0093）
    TENANT = ("gate.cross_client_interval", "stop.ai.stop_after_failures", "stop.ai.probe_interval",
              "stop.ai.resume_after_successes", "stop.line.stop_after_failures", "stop.line.probe_interval",
              "stop.line.resume_after_successes", "stop.flapping.window", "stop.flapping.wait_multiplier",
              "stop.flapping.max_wait", "budget.tenant_daily_yen",   # 費用の見張りは契約者ごと（2026-10-18 ユーザー）
              "judge.models", "pc.auto_update")   # 判定に使うAI・プログラムの更新（2026-10-18 ユーザー）

    def setUp(self):
        self.w = World(); self.t = self.w.tenant(); self.c = self.w.client(self.t)

    def test_declared(self):
        got = dict(sys_tx(lambda tx: [(r["key"], r["finest_scope"]) for r in tx.all(
            "SELECT key, finest_scope::text FROM salescall.setting_definitions")]))
        self.assertEqual({k: got[k] for k in self.CLIENT}, {k: "client" for k in self.CLIENT})
        self.assertEqual({k: got[k] for k in self.TENANT}, {k: "tenant" for k in self.TENANT})
        self.assertEqual(got["gate.claim_lifetime"], "system")       # すべての契約者に共通のもの（設備の手順の値）は管理者だけ
        self.assertNotIn("budget.system_daily_yen", got)             # すべての契約者の費用の合計は、誰が払う額でもないので見ない

    def test_client_values_are_used(self):
        for key, value in [("line.max_dials_per_day", 120), ("gate.same_contact_min_interval", 900)]:
            with self.subTest(key=key):
                put(self.w, self.t, self.c, key, value)
                self.assertEqual(resolved(self.w, self.t, self.c, key), value)
                self.assertNotEqual(resolved(self.w, self.t, self.w.client(self.t), key), value)

    def test_tenant_values_are_used_by_every_client_of_the_contractor(self):
        for key, value in [("gate.cross_client_interval", 86400), ("stop.ai.stop_after_failures", 5)]:
            with self.subTest(key=key):
                self.w.a.in_tenant(OPERATOR, self.t, lambda tx: tx.execute(
                    "INSERT INTO salescall.setting_values (key, scope, tenant_id, value, status, rationale, review_when,"
                    " set_by) VALUES (%s, 'tenant', %s, %s, 'decided', '契約者の決まり', '試験', '試験')",
                    (key, self.t, json.dumps(value))))
                self.assertEqual(resolved(self.w, self.t, self.w.client(self.t), key), value)
                other = self.w.tenant()
                self.assertNotEqual(resolved(self.w, other, self.w.client(other), key), value)

    def test_the_client_hours_limit_frames_the_business_hours(self):
        """深夜・早朝の歯止めは依頼者ごと。業務の時間帯の入力の誤りは、その依頼者の歯止めの内側に収まる。"""
        b, _ = self.w.business_with_version(self.t, self.c)
        put(self.w, self.t, self.c, "calling.hours_limit", {d: [["08:00", "20:00"]] for d in ALL_DAY})
        put(self.w, self.t, self.c, "calling.hours", ALL_DAY, scope="business", business=b)
        d = self.w.a.in_client(SYSTEM, self.t, self.c, lambda tx: settings.resolve_detail(
            tx, "calling.hours", tenant_id=self.t, client_id=self.c, business_id=b))
        self.assertEqual((d["value"]["mon"], d["from"], d["bounded_by"]),
                         ([["08:00", "20:00"]], "business", "calling.hours_limit"))
        put(self.w, self.t, self.c, "calling.hours_limit", ALL_DAY)   # 依頼者が 24時間よいと決めれば、そのとおり
        self.assertEqual(resolved(self.w, self.t, self.c, "calling.hours", business=b)["mon"], [["00:00", "23:59"]])


class FramesCannotBeFinerThanWhatTheyFrame(unittest.TestCase):
    def test_a_frame_that_a_finer_scope_can_move_is_rejected(self):
        """枠（超えられない歯止め）を、収める設定より細かい範囲で動かせたら、枠にならない。"""
        with self.assertRaises(Rejected):
            owner_access().in_system(SYSTEM, lambda tx: tx.execute(
                "INSERT INTO salescall.setting_definitions (key, merge_kind, value_type, meaning, design_ref, bound_key,"
                " bound_side, finest_scope) VALUES ('test.bad_frame', 'override', 'integer', '試験', '試験',"
                " 'retry.max_attempts_per_task', 'upper', 'tenant')"))

    def test_a_migration_that_rewrites_definitions_cannot_break_the_frames(self):
        """定義の変更止めを外して書き換える移行（0045 のような）でも、枠が収める設定より細かくなれば断る。"""
        def rewrite(tx):
            tx.execute("ALTER TABLE salescall.setting_definitions DISABLE TRIGGER setting_definitions_frozen")
            tx.execute("UPDATE salescall.setting_definitions SET finest_scope = 'business' WHERE key = 'calling.hours_limit'")
            tx.execute("UPDATE salescall.setting_definitions SET finest_scope = 'client' WHERE key = 'calling.hours'")
            tx.execute("ALTER TABLE salescall.setting_definitions ENABLE TRIGGER setting_definitions_frozen")
        with self.assertRaises(Rejected):
            owner_access().in_system(SYSTEM, rewrite)
        got = owner_access().in_system(SYSTEM, lambda tx: tx.one(
            "SELECT finest_scope::text AS f FROM salescall.setting_definitions WHERE key = 'calling.hours_limit'"))["f"]
        self.assertEqual(got, "client")                                  # 書き換えは残らない

    def test_system_only_is_written_one_way(self):
        """「システムだけ」は、いちばん細かく決められる範囲で表す（合成の種類の system_only は使わない）。"""
        with self.assertRaises(Rejected):
            owner_access().in_system(SYSTEM, lambda tx: tx.execute(
                "INSERT INTO salescall.setting_definitions (key, merge_kind, value_type, meaning, design_ref, finest_scope)"
                " VALUES ('test.old_way', 'system_only', 'integer', '試験', '試験', 'system')"))


if __name__ == "__main__":
    unittest.main()
