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
from tests.support import ADMIN, OPERATOR, SYSTEM, World, app_access

SECRET = b"test-only-secret"

# 値の形ごとの、人が入れる欄の例（read に通して、データベースの確かめ salescall.setting_value_ok に通ることを見る）
SAMPLES: dict[str, dict[str, str]] = {
    "boolean": {"f_v": "1"}, "integer": {"f_v": "3"}, "seconds": {"f_v": "20"}, "ratio": {"f_v": "12.5"},
    "integer_list": {"f_v": "1, 3, 7"}, "hour_list": {"f_v": "12、15"}, "daily_range": {"f_v": "09:00-19:00"},
    "daily_ranges": {"f_v": "00:00-09:00, 14:00-24:00"},
    "weekly_hours": {"f_mon": "09:00-12:00, 13:00-18:00", "f_tue": "9:00〜18:00", "f_sun": "", "f_holiday": ""},
    "gap_or_never": {"f_unit": "days", "f_n": "3"}, "day_or_gap": {"f_unit": "next_day"}, "limit": {"f_none": "1"},
    "phone": {"f_v": "03-0000-0001"}, "choice": {"f_v": "あ"},
    "chatwork_room": {"f_v": "123456"},
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

    def test_spaces_around_the_range_mark_are_part_of_the_range(self):
        got = setting_forms.read("daily_ranges", {"f_v": "09:00 - 12:00, 13:00 〜 18:00"}, "f_", {})
        self.assertEqual(got, [["09:00", "12:00"], ["13:00", "18:00"]])
        self.assertEqual(setting_forms.read("weekly_hours", {"f_mon": "9:00 - 12:00 13:00 - 18:00"}, "f_", {})["mon"],
                         [["09:00", "12:00"], ["13:00", "18:00"]])

    def test_weekly_hours_are_shown_short_by_grouping_the_same_days(self):   # 7つ並べると読めない（2026-11-21 の見直し）
        day = [["09:00", "18:00"]]
        self.assertEqual(setting_forms.show("weekly_hours", {k: [["08:00", "22:00"]] for k, _ in setting_forms.DAYS}),
                         "毎日（祝日も）08:00〜22:00")
        week = {k: (day if k in ("mon", "tue", "wed", "thu", "fri") else []) for k, _ in setting_forms.DAYS}
        self.assertEqual(setting_forms.show("weekly_hours", week), "月〜金 09:00〜18:00 ／ 土・日・祝日 なし")
        week.update(tue=[], sat=day)                                            # 続かない曜日は「・」で、続く3日以上は「〜」で
        self.assertEqual(setting_forms.show("weekly_hours", week), "月・水〜土 09:00〜18:00 ／ 火・日・祝日 なし")

    def test_the_edges_of_a_day_are_read_as_from_and_until(self):   # 「午後」の幅が「13:00〜00:00」と出ていた（2026-11-21 の見直し）
        self.assertEqual(setting_forms.show("daily_range", ["13:00", "00:00"]), "13:00から（その日の終わりまで）")
        self.assertEqual(setting_forms.show("daily_range", ["00:00", "10:00"]), "10:00まで")
        self.assertEqual(setting_forms.show("daily_ranges", [["00:00", "24:00"]]), "いつでも（0時〜24時）")
        self.assertEqual(setting_forms.show("daily_range", ["16:00", "18:00"]), "16:00〜18:00")

    def test_a_phone_number_is_cleared_only_when_asked_and_shown_by_the_last_four(self):
        with self.assertRaises(Refused):
            setting_forms.read("phone", {"f_v": ""}, "f_", {})                 # 空のまま送っても消えない
        self.assertIsNone(setting_forms.read("phone", {"f_v": "", "f_clear": "1"}, "f_", {}))
        with self.assertRaises(Refused):
            setting_forms.read("phone", {"f_v": "03-0000-0001", "f_clear": "1"}, "f_", {})
        shown = setting_forms.show("phone", "+81300000001")
        self.assertIn("0001", shown)
        self.assertNotIn("0300", shown)                                        # 下4桁のほかは出さない

    def test_every_setting_has_a_short_screen_name(self):   # 見出しの短い名前（output/words.SETTING_LABELS の1か所）
        keys = {r["key"] for r in app_access().in_system(SYSTEM, lambda tx: tx.all("SELECT key FROM salescall.setting_definitions"))}
        self.assertEqual(set(words.SETTING_LABELS), keys)
        self.assertLessEqual(set(words.CONFIRMED_SETTINGS), keys)                 # 確かめの窓を付ける設定（setting_confirm.json）も実在の設定だけ
        for k, v in words.SETTING_LABELS.items():
            with self.subTest(key=k):
                self.assertLessEqual(len(v), 24)

    def test_every_setting_group_has_a_word(self):
        heads = app_access().in_system(SYSTEM, lambda tx: {r["k"].split(".")[0] for r in tx.all(
            "SELECT key AS k FROM salescall.setting_definitions")})
        self.assertEqual(heads - set(words.SETTING_GROUPS), set())

    def test_every_number_setting_has_a_unit_and_seconds_read_as_time(self):
        """数の設定には単位を添える（「30」ではなく「30日」）。長い秒は人の時間に直す（「300秒」ではなく「5分（300秒）」）。"""
        keys = app_access().in_system(SYSTEM, lambda tx: {r["key"] for r in tx.all(
            "SELECT key FROM salescall.setting_definitions WHERE value_type::text = ANY(%s)", (list(words.UNIT_VALUE_TYPES),))})
        self.assertEqual(set(words.SETTING_UNITS), keys)
        self.assertEqual(setting_forms.show("seconds", 20), "20秒")
        self.assertEqual(setting_forms.show("seconds", 300), "5分（300秒）")
        self.assertEqual(setting_forms.show("seconds", 5400), "1時間30分（5400秒）")
        self.assertEqual(setting_forms.show("integer", 30, "日"), "30日")
        self.assertEqual(setting_forms.show("limit", None, "本"), "上限なし")
        self.assertEqual(setting_forms.show("integer_list", [60, 600], "秒"), "1分（60秒）、10分（600秒）")

    def test_every_yes_no_setting_answers_in_its_own_words(self):
        """はい／いいえの設定は、値を言葉で見せる（見出しが問いの形でなくても答えが浮かない。例「決められない決定の扱い：いいえ」だった）。"""
        keys = app_access().in_system(SYSTEM, lambda tx: {r["key"] for r in tx.all(
            "SELECT key FROM salescall.setting_definitions WHERE value_type::text = 'boolean'")})
        self.assertEqual(set(words.SETTING_YES_NO), keys)
        for k, (yes, no) in words.SETTING_YES_NO.items():
            with self.subTest(key=k):
                self.assertTrue(yes and no and yes != no)
        self.assertEqual(words.yes_no("decision.undecided_as_decided", False), "契約者に委ねる（確かめるまでかけない）")

    def test_the_groups_are_in_the_order_of_the_work_not_of_the_internal_names(self):
        rows = [{"group": g, "key": g + ".x"} for g in ("login", "approval", "calling", "business", "zzz")]
        self.assertEqual([g for g, _ in words.grouped_settings(rows)], ["business", "calling", "approval", "login", "zzz"])


    def test_the_everyday_groups_are_real_groups(self):   # ふだんのまとまり（開いて並べる）は、まとまりの名前の表の中から選ぶ
        self.assertTrue(words.EVERYDAY_GROUPS)
        self.assertLessEqual(set(words.EVERYDAY_GROUPS), set(words.SETTING_GROUPS))


class ScreenWords(unittest.TestCase):
    def test_every_choice_has_a_word(self):
        """選び肢の言葉は設定の定義の1か所（choice_words＝CL-2 4-450）。画面はそれを出すだけ（どの選び肢にも言葉がある）。"""
        rows = app_access().in_system(SYSTEM, lambda tx: tx.all(
            "SELECT key, choices, choice_words FROM salescall.setting_definitions WHERE choices IS NOT NULL"))
        for r in rows:
            for c in r["choices"]:
                with self.subTest(key=r["key"], choice=c):
                    self.assertNotEqual(words.choice_word(r["choice_words"], c), words.NO_WORD)

    def test_the_settings_page_shows_the_contractor_help_without_internal_names(self):
        """設定の画面の説明は、契約者向けの説明（help）だけ。開発の根拠や中の英字の名前を出さない。"""
        helps = app_access().in_system(SYSTEM, lambda tx: tx.all("SELECT key, help FROM salescall.setting_definitions"))
        for r in helps:
            with self.subTest(key=r["key"]):
                self.assertTrue(r["help"])
                self.assertEqual(re.findall(r"\b[a-z]+(?:_[a-z0-9]+)+\b|\b[a-z]+\.[a-z0-9_]+\b", r["help"]), [], r["help"])


class Screens(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        w = World()
        cls.a = w.setup()
        cls.b = w.setup()
        cls.people = {"ADMIN": Person("試験の管理者", True, cls.a["tenant"], "契約者A"),
                      "A": Person("試験の契約者A", False, cls.a["tenant"], "契約者A"),
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
        page = self.get("A", biz).text
        self.assertIn("ここで決めた値", page)
        anchor = "s-" + self.key.replace(".", "-")                                 # 上の「ここで決めている値」から、その行へ飛べる
        self.assertIn(f'href="#{anchor}"', page.split("ここで決めている値", 1)[1].split("</section>", 1)[0])
        self.assertIn(f'id="{anchor}"', page)
        # 戻った画面は、変えた設定の所へ送り、結果の1行も、どの設定を変えたかを名前で言って、その札の上に出す（93 の設定のどれかを探させない）
        loc = r.headers["location"]
        self.assertTrue(loc.endswith("#changed"))
        back = self.get("A", loc.split("#")[0]).text
        note = back[back.index('id="changed"'):back.index(f'id="{anchor}"')]
        self.assertIn(f"「{words.SETTING_LABELS[self.key]}」を変えました", note)
        self.assertIn("just-changed", back[back.index(f'id="changed"'):back.index(f'id="{anchor}"') + 200])
        bad = self.post("A", biz, {"form_token": tok, "key": self.key, "f_v": "五", "reason": "試験"})
        again = self.get("A", bad.headers["location"].split("#")[0]).text
        at = again.index(f'id="{anchor}"')
        self.assertIn("open", again[at:at + 40])                                  # 断られたら開いたまま（その場で直せる）

    def test_the_everyday_groups_are_open_and_the_rest_is_folded_once(self):
        """設定の画面は、ふだんのまとまりだけを開いて並べ、残りは「くわしい設定」1つにたたむ（どのまとまりも1回だけ出る）。
        2026-11-20 の見直し：まとまり約30・項目100以上が同じ重さで並び、スマホで3万6千ピクセルの長さだった。"""
        for path in ("/t/settings", f"/c/{self.a['client']}/b/{self.a['business']}/settings"):
            with self.subTest(path=path):
                page = self.get("A", path).text
                self.assertEqual(page.count('id="advanced"'), 1)
                opened, folded = page.split('id="advanced"', 1)
                shown = re.findall(r'<section class="setting-group" id="g-([a-z_]+)"', page)
                self.assertEqual(len(shown), len(set(shown)))
                self.assertTrue(shown)
                for g in shown:
                    with self.subTest(group=g):
                        self.assertIn(f'id="g-{g}"', opened if g in words.EVERYDAY_GROUPS else folded)
                self.assertIn('href="#advanced"', opened)          # 目次から、たたんだ所へも行ける
                self.assertNotIn("open>", folded.split(">", 1)[0] + ">")   # ふだんのまとまりがある段では、初めはたたんでいる

    def test_the_system_values_are_for_the_admin_only(self):
        self.assertEqual(self.get("A", "/admin/settings").status_code, 404)
        self.assertEqual(self.post("A", "/admin/settings", {"form_token": self.token("A"), "key": self.key, "f_v": "9",
                                                            "reason": "乗っ取り"}).status_code, 404)
        page = self.get("ADMIN", "/admin/settings")
        self.assertEqual(page.status_code, 200)
        self.assertIn("システムの初めの値", page.text)

    def test_the_ai_tier_rows_show_only_when_the_contractor_uses_its_own_api(self):
        """会話AIの枠は、自分で API を契約するプランのときだけ（決めるのは core の ai_plans.tier の1か所。CL-2 4-438・4-446 の 55）。"""
        from salescall.core import ai_plans, settings as core_settings
        label = words.SETTING_LABELS[ai_plans.TIER_SETTINGS["conversation"]]
        w = World()
        c = w.setup()
        http = TestClient(create_app(app_access(), lambda r: Person("試験の契約者C", False, c["tenant"], "契約者C"), SECRET))
        self.assertIn(label, http.get("/t/settings").text)                            # 初めは自分で契約する（own）＝枠を出す
        app_access().in_tenant(ADMIN, c["tenant"], lambda tx: core_settings.set_value(   # プランを切り替えるのは管理者だけ
            tx, ai_plans.PLAN_SETTINGS["conversation"], "included", scope="tenant", tenant_id=c["tenant"],
            status="decided", rationale="試験（架空）：AI料金込みにする", review_when="試験のあいだ"))
        self.assertNotIn(label, http.get("/t/settings").text)                         # AI料金込み＝システムのキー＝契約者の枠は無い

    def test_a_setting_that_cannot_be_decided_per_business_is_refused_there(self):
        if self.tenant_only is None:
            self.skipTest("契約者までしか決められない設定がありません")
        biz = f"/c/{self.a['client']}/b/{self.a['business']}/settings"
        r = self.post("A", biz, {"form_token": self.token("A"), "key": self.tenant_only["key"], "f_v": "1", "reason": "試験"})
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main()


class SettingsOrder(unittest.TestCase):
    def test_notification_destinations_come_first_and_short_message_before_chatwork(self):
        """設定の並びは言葉の表の順（意味の順）。知らせの受け先はショートメールが先（✅ 2026-11-18 管理者「Rakuten Link が最優先」）、
        受け先は送り直す回数より先。"""
        o = words.SETTING_ORDER
        self.assertLess(o["notify.sms_to"], o["notify.chatwork_room"])
        self.assertLess(o["notify.chatwork_room"], o["notify.max_tries_sms"])
        self.assertLess(o["notify.max_tries_sms"], o["notify.send_hours"])
