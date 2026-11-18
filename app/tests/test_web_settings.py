"""設定の画面の試験（持ち主 CL-3。web/setting_forms.py・設定のルート）。架空のデータだけ。

値の形の表の抜け（データベースの形の一覧と突き合わせる）・入れた値がデータベースの形の確かめに通るか・段ごとの分離と守り。
"""

from __future__ import annotations

import re
import unittest

from fastapi.testclient import TestClient

from salescall.output import words
from salescall.web import setting_forms
from salescall.web.actions import Refused
from salescall.web.app import create_app
from salescall.web.context import Person
from tests.support import OPERATOR, SYSTEM, World, app_access

SECRET = b"test-only-secret"

# 値の形ごとの、人が入れる欄の例（read に通して、データベースの確かめ salescall.setting_value_ok に通ることを見る）
SAMPLES: dict[str, dict[str, str]] = {
    "boolean": {"f_v": "1"}, "integer": {"f_v": "3"}, "seconds": {"f_v": "20"}, "ratio": {"f_v": "12.5"},
    "integer_list": {"f_v": "1, 3, 7"}, "hour_list": {"f_v": "12、15"}, "daily_range": {"f_v": "09:00-19:00"},
    "daily_ranges": {"f_v": "00:00-09:00, 14:00-24:00"},
    "weekly_hours": {"f_mon": "09:00-12:00, 13:00-18:00", "f_tue": "9:00〜18:00", "f_sun": "", "f_holiday": ""},
    "gap_or_never": {"f_unit": "days", "f_n": "3"}, "day_or_gap": {"f_unit": "next_day"}, "limit": {"f_none": "1"},
    "phone": {"f_v": "03-0000-0001"}, "choice": {"f_v": "あ"},
}


def _enum(name):
    return app_access().in_system(SYSTEM, lambda tx: [r["v"] for r in tx.all(
        "SELECT e.enumlabel AS v FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid JOIN pg_namespace n ON n.oid = t.typnamespace"
        " WHERE n.nspname = 'salescall' AND t.typname = %s", (name,))])


class Forms(unittest.TestCase):
    def test_every_value_type_has_a_form_and_a_sample(self):
        types = set(_enum("setting_value_type"))
        self.assertEqual(set(setting_forms.FORMS), types)
        self.assertEqual(set(SAMPLES), types)

    def test_what_the_forms_read_is_accepted_by_the_database(self):
        for t, form in SAMPLES.items():
            with self.subTest(type=t):
                v = setting_forms.read(t, form, "f_", {"choices": ["あ", "い"]})
                if t == "choice":   # 選ぶものは、データベースが設定ごとの選び肢で確かめる（形だけの確かめでは見ない）
                    self.assertIn(v, ["あ", "い"])
                    continue
                ok = app_access().in_system(SYSTEM, lambda tx, t=t, v=v: tx.one(
                    "SELECT salescall.setting_value_ok(%s::salescall.setting_value_type, %s::jsonb) AS ok",
                    (t, __import__("json").dumps(v)))["ok"])
                self.assertTrue(ok, (t, v))
                self.assertNotEqual(setting_forms.show(t, v), "")

    def test_unreadable_input_is_refused_with_a_reason(self):
        for t, form in (("integer", {"f_v": "三"}), ("daily_range", {"f_v": "9時から"}), ("ratio", {"f_v": "120"}),
                        ("hour_list", {"f_v": "15, 12"}), ("gap_or_never", {"f_unit": "hours", "f_n": "0"}),
                        ("phone", {"f_v": "番号ではない"}), ("choice", {"f_v": "う"}), ("boolean", {})):
            with self.subTest(type=t):
                with self.assertRaises(Refused):
                    setting_forms.read(t, form, "f_", {"choices": ["あ", "い"]})

    def test_every_setting_group_has_a_word(self):
        heads = app_access().in_system(SYSTEM, lambda tx: {r["k"].split(".")[0] for r in tx.all(
            "SELECT key AS k FROM salescall.setting_definitions")})
        self.assertEqual(heads - set(words.SETTING_GROUPS), set())


class Screens(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        w = World()
        cls.a = w.setup()
        cls.b = w.setup()
        cls.people = {"A": Person("試験の契約者A", False, cls.a["tenant"], "契約者A"),
                      "B": Person("試験の契約者B", False, cls.b["tenant"], "契約者B")}
        cls.http = TestClient(create_app(app_access(), lambda r: cls.people.get(r.headers.get("x-test-person", "")), SECRET))
        cls.key = app_access().in_system(SYSTEM, lambda tx: tx.one(
            "SELECT key FROM salescall.setting_definitions WHERE value_type = 'integer' AND merge_kind = 'override'"
            " AND finest_scope = 'business' ORDER BY key LIMIT 1"))["key"]
        cls.tenant_only = app_access().in_system(SYSTEM, lambda tx: tx.one(
            "SELECT key FROM salescall.setting_definitions WHERE finest_scope = 'tenant' ORDER BY key LIMIT 1"))

    def get(self, who, path):
        return self.http.get(path, headers={"x-test-person": who}, follow_redirects=False)

    def post(self, who, path, data):
        return self.http.post(path, data=data, headers={"x-test-person": who}, follow_redirects=False)

    def token(self, who, path="/t"):
        return re.search(r'<meta name="form-token" content="([^"]+)"', self.get(who, path).text).group(1)

    def test_the_three_levels_open_and_another_contractor_cannot(self):
        biz = f"/c/{self.a['client']}/b/{self.a['business']}/settings"
        for path in ("/t/settings", f"/c/{self.a['client']}/settings", biz):
            with self.subTest(path=path):
                self.assertEqual(self.get("A", path).status_code, 200)
                self.assertNotIn(words.NO_WORD, self.get("A", path).text)
        self.assertEqual(self.get("B", biz).status_code, 404)
        self.assertEqual(self.post("B", biz, {"form_token": self.token("B"), "key": self.key, "f_v": "5", "reason": "乗っ取り"}).status_code, 404)

    def test_a_business_value_is_saved_with_a_reason_and_shown_as_its_own(self):
        biz = f"/c/{self.a['client']}/b/{self.a['business']}/settings"
        tok = self.token("A")
        self.assertIn("ok=0", self.post("A", biz, {"form_token": tok, "key": self.key, "f_v": "5", "reason": ""}).headers["location"])
        self.assertIn("ok=0", self.post("A", biz, {"form_token": tok, "key": self.key, "f_v": "五", "reason": "試験"}).headers["location"])
        r = self.post("A", biz, {"form_token": tok, "key": self.key, "f_v": "5", "reason": "試験（架空）"})
        self.assertIn("ok=1", r.headers["location"])
        got = app_access().in_client(OPERATOR, self.a["tenant"], self.a["client"], lambda tx: __import__(
            "salescall.core.settings", fromlist=["x"]).resolve_detail(tx, self.key, tenant_id=self.a["tenant"], client_id=self.a["client"],
                                                                      business_id=self.a["business"]))
        self.assertEqual((got["value"], got["from"]), (5, "business"))
        self.assertIn("この段で決めた値", self.get("A", biz).text)

    def test_a_setting_that_cannot_be_decided_per_business_is_refused_there(self):
        if self.tenant_only is None:
            self.skipTest("契約者までしか決められない設定がありません")
        biz = f"/c/{self.a['client']}/b/{self.a['business']}/settings"
        r = self.post("A", biz, {"form_token": self.token("A"), "key": self.tenant_only["key"], "f_v": "1", "reason": "試験"})
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main()
