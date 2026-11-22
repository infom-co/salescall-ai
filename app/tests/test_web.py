"""ダッシュボードの試験（持ち主 CL-3。docs/DASHBOARD.md 8章）。画面を通して叩く。架空のデータだけを使う。

ログインの仕組み（利用者の表）は CL-2 に頼んでいるので、ここでは架空の人を返す関数を渡す（本番の画面のサーバーは、ログインを確かめる
関数を渡されなければ動かない）。
"""

from __future__ import annotations

import re
from markupsafe import escape
from urllib.parse import quote, unquote
import unittest
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from salescall.core import stops
from salescall.web.app import create_app
from salescall.web.context import Person
from tests.support import OPERATOR, SYSTEM, World, app_access, ensure_rules, fictional_caller, fictional_jp_fixed

SECRET = b"test-only-secret"
from tests.test_output_words import DEV_NOTE  # noqa: E402  画面に出してはいけない開発の覚え書きのしるし（1か所）



def _unnamed_fields(page: str) -> list[str]:
    """名前（label で包む・label の for・aria-label・aria-labelledby・title）の無い入力欄・選ぶ欄・文の欄（隠した欄とボタンは除く）。
    2026-11-22 の見直し：見出しの文と欄が結び付いていない欄が7つあった（読み上げで、どの欄か分からない）。"""
    from html.parser import HTMLParser

    class P(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.depth, self.fors, self.fields = 0, set(), []

        def handle_starttag(self, tag, attrs):
            a = dict(attrs)
            if tag == "label":
                self.depth += 1
                if a.get("for"):
                    self.fors.add(a["for"])
            elif tag in ("input", "select", "textarea") and a.get("type") not in ("hidden", "submit", "button"):
                named = self.depth > 0 or any(a.get(k) for k in ("aria-label", "aria-labelledby", "title"))
                self.fields.append((a.get("id"), named, f'{tag} name={a.get("name", "")}'))

        def handle_endtag(self, tag):
            if tag == "label" and self.depth:
                self.depth -= 1

    p = P()
    p.feed(page)
    return [d for i, named, d in p.fields if not named and not (i and i in p.fors)]

class _World(unittest.TestCase):
    """架空の契約者A（依頼者X・Y）と契約者B（依頼者Z）。試験の組が共通に使う準備（試験そのものは持たない）。"""

    @classmethod
    def setUpClass(cls):
        w = World()
        cls.a = w.setup()                               # 契約者A・依頼者X
        cls.y = w.setup(tenant=cls.a["tenant"])         # 同じ契約者Aの依頼者Y（番号は別）
        cls.b = w.setup(phone=cls.a["phone"])           # 契約者B・依頼者Z（契約者Aと同じ番号のお客様）
        app_access().in_tenant(OPERATOR, cls.a["tenant"], lambda tx: tx.execute(
            "UPDATE salescall.clients SET name = CASE client_id WHEN %s THEN '依頼者X' ELSE '依頼者Y' END WHERE tenant_id = %s",
            (cls.a["client"], cls.a["tenant"])))
        app_access().in_tenant(OPERATOR, cls.b["tenant"], lambda tx: tx.execute(
            "UPDATE salescall.clients SET name = '依頼者Z（契約者B）' WHERE client_id = %s", (cls.b["client"],)))
        cls.people = {"A": Person("試験の契約者A", False, cls.a["tenant"], "契約者A"),
                      "B": Person("試験の契約者B", False, cls.b["tenant"], "契約者B"),
                      "ADMIN": Person("試験の管理者", True, cls.a["tenant"], "契約者A")}

        def identify(request):
            return cls.people.get(request.headers.get("x-test-person", ""))
        cls.http = TestClient(create_app(app_access(), identify, SECRET))

    def get(self, who, path):
        return self.http.get(path, headers={"x-test-person": who}, follow_redirects=False)

    def post(self, who, path, data):
        return self.http.post(path, data=data, headers={"x-test-person": who}, follow_redirects=False)

    def token(self, who, path="/t"):
        """なりすましの札（二度押しの札を外したもの＝1つの札で、いくつもの操作を送る試験のため。二度押しは、その試験で確かめる）。"""
        m = re.search(r'<meta name="form-token" content="([^"]+)"', self.get(who, path).text)
        self.assertIsNotNone(m, "画面に札がありません")
        return m.group(1).split("~")[0]

    def _stop(self, tenant: UUID, reason: str) -> int:
        return app_access().in_tenant(OPERATOR, tenant, lambda tx: stops.stop(
            tx, scope="tenant", cause="manual", reason=reason, tenant_id=tenant))["event_id"]

    def _open(self, tenant: UUID, eid: int) -> bool:
        return app_access().in_tenant(OPERATOR, tenant, lambda tx: tx.one(
            "SELECT 1 FROM salescall.open_stop_events WHERE event_id = %s", (eid,)) is not None)



class Web(_World):
    # ── ログイン ──
    def test_without_login_every_page_goes_to_the_login_page(self):   # S-23
        for path in ("/t", "/t/inbox", "/t/stops", "/t/notices", f"/c/{self.a['client']}", "/t/badges"):
            with self.subTest(path=path):
                r = self.http.get(path, follow_redirects=False)
                self.assertEqual(r.status_code, 303)
                self.assertEqual(r.headers["location"], "/login")

    def test_the_server_does_not_start_without_a_secret(self):
        with self.assertRaises(ValueError):
            create_app(app_access(), lambda r: None, b"")

    # ── 分離（docs/DASHBOARD.md 8-3） ──
    def test_the_client_list_shows_only_the_contractors_own_clients(self):   # S-13
        page = self.get("A", "/t").text
        self.assertIn("依頼者X", page)
        self.assertIn("依頼者Y", page)
        self.assertNotIn("依頼者Z", page)

    def test_another_contractors_client_answers_the_same_as_a_missing_one(self):   # S-2・S-14
        other = self.get("A", f"/c/{self.b['client']}")
        missing = self.get("A", "/c/00000000-0000-0000-0000-000000000000")
        self.assertEqual(other.status_code, 404)
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(other.text, missing.text)
        self.assertNotIn("依頼者Z", other.text)

    def test_a_run_of_another_client_cannot_be_opened_under_this_client(self):   # S-4
        x = self.a
        for client, business, run in ((x["client"], x["business"], self.y["run"]),         # 同じ契約者の別の依頼者の実施回
                                      (x["client"], self.y["business"], self.y["run"]),    # 別の依頼者の業務を、この依頼者の住所で
                                      (x["client"], x["business"], self.b["run"]),         # 別の契約者の実施回
                                      (x["client"], self.b["business"], self.b["run"])):
            with self.subTest(business=business, run=run):
                self.assertEqual(self.get("A", f"/c/{client}/b/{business}/runs/{run}").status_code, 404)

    def test_a_business_of_another_client_answers_the_same_as_a_missing_one(self):   # 業務のタブの分離（S-4 と同じ考え）
        other = self.get("A", f"/c/{self.a['client']}/b/{self.y['business']}")
        stranger = self.get("A", f"/c/{self.a['client']}/b/{self.b['business']}")
        missing = self.get("A", f"/c/{self.a['client']}/b/00000000-0000-0000-0000-000000000000")
        self.assertEqual({other.status_code, stranger.status_code, missing.status_code}, {404})
        self.assertEqual(other.text, missing.text)
        self.assertEqual(stranger.text, missing.text)

    def test_the_client_tab_shows_the_business_tabs_of_that_client_only(self):   # 2段のタブ（2026-11-18 管理者）
        names = {k: app_access().in_client(OPERATOR, w["tenant"], w["client"], lambda tx, w=w: tx.one(   # 業務は依頼者の取引でだけ見える
            "SELECT name FROM salescall.businesses WHERE business_id = %s", (w["business"],))["name"]) for k, w in (("x", self.a), ("y", self.y))}
        page = self.get("A", f"/c/{self.a['client']}").text
        self.assertIn(f'href="/c/{self.a["client"]}/b/{self.a["business"]}"', page)
        self.assertNotIn(str(self.y["business"]), page)
        self.assertIn("概要（全業務）", page)
        biz = self.get("A", f"/c/{self.a['client']}/b/{self.a['business']}")
        self.assertEqual(biz.status_code, 200)
        self.assertIn(names["x"], biz.text)
        self.assertIn(f'href="/c/{self.a["client"]}/b/{self.a["business"]}/runs/{self.a["run"]}"', biz.text)

    def test_a_stop_that_holds_a_client_is_shown_on_its_tabs_only(self):   # 業務のタブを開いて「止まっている」と分かる
        eid = app_access().in_client(OPERATOR, self.a["tenant"], self.a["client"], lambda tx: stops.stop(
            tx, scope="client", cause="manual", reason="依頼者Xだけの試験の停止", tenant_id=self.a["tenant"], client_id=self.a["client"]))["event_id"]
        try:
            for path in (f"/c/{self.a['client']}", f"/c/{self.a['client']}/b/{self.a['business']}"):
                with self.subTest(path=path):
                    self.assertIn(f"/c/{self.a['client']}/stops/{eid}", self.get("A", path).text)
            self.assertNotIn(f"/stops/{eid}", self.get("A", f"/c/{self.y['client']}").text)   # 別の依頼者には出さない
        finally:
            app_access().in_client(OPERATOR, self.a["tenant"], self.a["client"], lambda tx: stops.resume(tx, eid, reason="試験の片付け"))

    def test_every_stop_scope_is_decided_for_the_client_tabs(self):   # 範囲が増えたのに決めていなければ落ちる
        from salescall.web import counts
        self.assertEqual(set(counts.SCOPE_HOLDS), set(stops.SCOPE_NAMES))

    def test_the_business_tab_says_when_recordings_hold_a_run(self):   # 録音の要るものは core の1か所の答えのとおりに出す
        from salescall.core import recordings
        need = app_access().in_client(OPERATOR, self.a["tenant"], self.a["client"], lambda tx: recordings.version_needs(
            tx, tx.one("SELECT salescall.run_current_version(%s) AS v", (self.a["run"],))["v"]))
        page = self.get("A", f"/c/{self.a['client']}/b/{self.a['business']}").text
        self.assertEqual("録音がそろうまで" in page, (not need.listed) or bool(need.parts))
        self.assertNotIn("録音がそろうまで", self.get("A", f"/c/{self.a['client']}").text)   # 概要（全業務）では出さない（業務のタブで出す）

    def test_a_version_page_shows_the_call_type_and_the_materials_of_the_acquire_type(self):
        """電話の型（確認の電話／顧客獲得型）を台本の画面に出す。顧客獲得型のときだけ、その中身（「はい」をいただく質問・特別感の条件・
        悩みと事例・お時間）も出す（計画書 9-9。読むのは core の business_settings.load の1か所）。"""
        import dataclasses
        from unittest import mock
        from salescall.core import business_settings
        from salescall.output import words
        ensure_rules(World(), self.a)
        url = f"/c/{self.a['client']}/b/{self.a['business']}/versions/{self.a['version']}"
        page = self.get("A", url).text
        self.assertIn(words.CALL_TYPE_WORDS["confirm"], page)
        self.assertNotIn("顧客獲得型の中身", page)
        real = business_settings.load
        acquire = lambda tx, v: dataclasses.replace(real(tx, v), call_type="acquire", yes_asks=("架空の求人をご掲載されていて",),
                                                    special="架空の採用に積極的", pains=(("架空の悩み", "架空の事例"),), meeting_time="1時間")
        with mock.patch.object(business_settings, "load", side_effect=acquire):
            page = self.get("A", url).text
        self.assertIn(words.CALL_TYPE_WORDS["acquire"], page)
        part = page[page.index("顧客獲得型の中身"):]
        for text in ("架空の求人をご掲載されていて", "架空の採用に積極的", "架空の悩み", "事例：架空の事例", "1時間"):
            self.assertIn(text, part)

    def test_a_version_page_shows_the_settings_of_its_own_business_only(self):
        ensure_rules(World(), self.a)
        ok = self.get("A", f"/c/{self.a['client']}/b/{self.a['business']}/versions/{self.a['version']}")
        self.assertEqual(ok.status_code, 200)
        self.assertIn("電話の台本", ok.text)
        self.assertNotIn(words_missing(), ok.text)
        # i マーク：説明の文は core の help_texts の1か所をそのまま（画面に写さない。設計書 6-7）
        from salescall.core import help_texts
        self.assertIn('<summary aria-label="「業務の目的」の説明">i</summary>', ok.text)
        self.assertIn(str(escape(help_texts.FIELDS["business.purpose"])), ok.text)
        self.assertIn(str(escape(help_texts.FIELDS["business.retry"])), ok.text)
        # 電話の中での応じ方・扱い・言ってはいけないこと（計画書 6-2 の項目を版の画面に。値の言葉は取り込みと同じ business_import.LABELS の1か所）
        for heading in ("電話の中での応じ方", "「わからない」「答えたくない」の扱い", "言ってはいけないこと"):
            self.assertIn(heading, ok.text)
        from salescall.output import words as w
        self.assertIn(w.sheet_word("substitute_policy", "decision_maker"), ok.text)
        self.assertNotIn(w.NO_WORD, ok.text[ok.text.index("電話の中での応じ方"):ok.text.index("「わからない」「答えたくない」の扱い")])
        from salescall.core import business_rules
        from salescall.output import words
        rules = app_access().in_client(OPERATOR, self.a["tenant"], self.a["client"], lambda tx: business_rules.load(tx, self.a["version"]))
        part = ok.text[ok.text.index('id="retry"'):]                            # 結果ごとのかけ直し（計画書 6-2。読むのは core の1か所）
        for k in business_rules.POLICY_KEYS:
            self.assertIn(f"{words.RETRY_POLICY_WORDS[k]}</td><td class=\"wide\">{words.retry_policy(rules.retry[k])}", part)
        # まとめて変える（方針だけを変えた新しい版を作る＝core の business_settings.change_retry。CL-2 4-440 の 49）
        from salescall.core import business_settings
        url = f"/c/{self.a['client']}/b/{self.a['business']}/versions/{self.a['version']}/retry"
        latest = app_access().in_client(OPERATOR, self.a["tenant"], self.a["client"], lambda tx: business_settings.successor_tip(tx, self.a["version"]))
        self.assertEqual(latest, self.a["version"])                                 # 試験の業務の版は、つながりのいちばん後
        self.assertIn(f'action="{url}"', ok.text)
        data = {"form_token": self.token("A")}
        for k in business_rules.POLICY_KEYS:
            p = rules.retry[k]
            data.update({f"{k}_action": p.action, f"{k}_times": str(p.max_times or ""), f"{k}_n": str(p.interval[1] if p.interval else ""),
                         f"{k}_unit": p.interval[0] if p.interval else "hours"})
        data.update({"not_reached_absent_action": "call", "not_reached_absent_times": "2", "not_reached_absent_n": "1",
                     "not_reached_absent_unit": "days"})
        self.assertIn("ok=0", self.post("A", url, {**data, "reason": ""}).headers["location"])   # 理由は必ず
        r = self.post("A", url, {**data, "reason": "試験：不在は2回まで"})
        self.assertIn("ok=1", r.headers["location"])
        new = r.headers["location"].split("/versions/")[1].split("?")[0]
        self.assertNotEqual(new, str(self.a["version"]))                        # 新しい版（前の版は残る）
        changed = app_access().in_client(OPERATOR, self.a["tenant"], self.a["client"], lambda tx: business_rules.load(tx, UUID(new)))
        self.assertEqual(changed.retry["not_reached_absent"], business_rules.Policy("call", 2, ("days", 1)))
        self.assertEqual(self.get("A", f"/c/{self.a['client']}/b/{self.a['business']}/versions/{self.y['version']}").status_code, 404)

    def test_the_counts_of_client_x_do_not_move_when_client_y_changes(self):   # S-6
        before = self.get("A", f"/c/{self.a['client']}").text
        def busy_y(tx):
            from salescall.core import next_action
            next_action.schedule_now(tx, self.y["task"], cause="試験：依頼者Yの予定をいまに")
        app_access().in_client(OPERATOR, self.y["tenant"], self.y["client"], busy_y)
        after = self.get("A", f"/c/{self.a['client']}").text
        grab = lambda t: re.findall(r'<span class="value">([^<]*)</span>', t)   # noqa: E731
        self.assertEqual(grab(before), grab(after))

    def test_search_finds_rows_of_this_client_only(self):   # S-7
        from salescall.core import customer_rows, phone
        def first(w):
            return app_access().in_client(OPERATOR, w["tenant"], w["client"], lambda tx: customer_rows.progress(tx, w["run"])[0])
        mine, other = first(self.a), first(self.b)                       # 契約者Bの依頼者Zは、同じ番号のお客様
        hit = self.get("A", f"/c/{self.a['client']}/search?q={phone.normalize(mine['dial_phone']).national}").text
        self.assertIn(f"/rows/{mine['task_id']}", hit)
        self.assertNotIn(str(other["task_id"]), hit)
        self.assertNotIn(str(other["task_id"]), self.get("A", f"/c/{self.y['client']}/search?q={phone.normalize(mine['dial_phone']).national}").text)
        self.assertEqual(self.get("A", f"/c/{self.b['client']}/search?q=a").status_code, 404)

    def test_an_operation_sent_from_an_old_tab_only_touches_the_client_in_its_address(self):   # S-5
        tok = self.token("A")
        other_task = self.y["task"]                                         # タブ2で開いていた依頼者Yの行を、タブ1の依頼者Xの住所で送る
        url = f"/c/{self.a['client']}/b/{self.a['business']}/runs/{self.a['run']}/exclude"
        self.assertEqual(self.post("A", url, {"form_token": tok, "task_id": str(other_task), "level": "exclude", "reason": "試験"}).status_code, 404)
        r = self.post("A", f"/c/{self.a['client']}/inbox/old_promise/keep", {"form_token": tok, "task_id": str(other_task)})
        self.assertIn("ok=0", r.headers["location"])

    def test_the_inbox_filter_ignores_a_client_of_another_contractor(self):
        page = self.get("A", f"/t/inbox?client={self.b['client']}&kind=no_such_kind")
        self.assertEqual(page.status_code, 200)
        self.assertNotIn("依頼者Z", page.text)

    def test_the_customer_list_shows_the_rows_of_the_run(self):
        r = self.get("A", f"/c/{self.a['client']}/b/{self.a['business']}/runs/{self.a['run']}")
        self.assertEqual(r.status_code, 200)
        self.assertIn("お客様リスト", r.text)
        self.assertIn("まだかけていない", r.text)   # まだ次の予定が無いお客様（words.NOT_PLANNED）

    # ── お客様の行の画面と直し（docs/DASHBOARD.md 5-7） ──
    def _row_url(self, w=None, task=None):
        w = w or self.a
        return f"/c/{self.a['client']}/b/{w['business']}/runs/{w['run']}/rows/{task or w['task']}"

    def _tasks_now(self, field):
        return app_access().in_client(OPERATOR, self.a["tenant"], self.a["client"], lambda tx: tx.one(
            f"SELECT {field} AS v FROM salescall.tasks_now WHERE task_id = %s", (self.a["task"],))["v"])

    def test_a_row_of_another_client_or_run_cannot_be_opened_or_fixed_here(self):   # S-4 と同じ考え
        for url in (self._row_url(task=self.y["task"]), self._row_url(task=self.b["task"]),
                    f"/c/{self.a['client']}/b/{self.a['business']}/runs/{self.y['run']}/rows/{self.a['task']}"):
            with self.subTest(url=url):
                self.assertEqual(self.get("A", url).status_code, 404)
                r = self.post("A", url + "/fix", {"form_token": self.token("A"), "field": "source_name", "value": "乗っ取り",
                                                 "go": "1"})
                self.assertEqual(r.status_code, 404)
        self.assertEqual(self.get("B", self._row_url()).status_code, 404)

    def test_the_row_page_shows_the_numbers_and_the_fix_form(self):
        page = self.get("A", self._row_url())
        self.assertEqual(page.status_code, 200, page.text[:300])
        self.assertIn("いまかける番号", page.text)
        self.assertIn("直す欄", page.text)

    def test_a_company_name_fix_goes_through_a_confirmation_then_is_written(self):
        url, tok = self._row_url(), self.token("A")
        form = {"form_token": tok, "field": "source_name", "value": "架空商事（直した）", "reading": "かくうしょうじ"}
        step = self.post("A", url + "/fix", form)
        self.assertEqual(step.status_code, 200)
        self.assertIn("この内容で直します", step.text)                       # 会社名は担当の方の選びが要らない（core の答え）
        self.assertNotEqual(self._tasks_now("source_name"), "架空商事（直した）")   # 確かめまでは何も書かない
        r = self.post("A", url + "/fix", {**form, "go": "1"})
        self.assertIn("ok=1", r.headers["location"])
        self.assertEqual(self._tasks_now("source_name"), "架空商事（直した）")

    def test_a_person_fix_asks_whether_it_is_the_same_person_first(self):
        url, tok = self._row_url(), self.token("A")
        form = {"form_token": tok, "field": "target_name", "value": "架空の別の方", "reading": "べつのかた"}
        step = self.post("A", url + "/fix", form)
        self.assertIn("いままでの担当の方と同じ方ですか", step.text)
        so_far = step.text[step.text.index('class="facts so-far"'):step.text.index('class="question"')]
        self.assertIn("架空の別の方", so_far)                                      # 途中の手順でも、何をどう直すかが見える（新しい値）
        self.assertIn("いまの値", so_far)
        forged = self.post("A", url + "/fix", {**form, "go": "1"})             # 選ばずに「直す」を送っても書かない
        self.assertIn("ok=0", forged.headers["location"])
        self.assertNotEqual(self._tasks_now("target_name"), "架空の別の方")
        step = self.post("A", url + "/fix", {**form, "same": "1"})
        self.assertIn("この内容で直します", step.text)
        self.assertIn("同じ方", step.text)
        r = self.post("A", url + "/fix", {**form, "same": "1", "go": "1"})
        self.assertIn("ok=1", r.headers["location"])
        self.assertEqual(self._tasks_now("target_name"), "架空の別の方")

    def test_a_field_is_cleared_only_when_clearing_is_chosen(self):
        """欄を消すのは「この欄を消す」を選んだときだけ（空のまま送っただけでは消さない＝設定の画面と同じ1つの決まり。2026-11-20 の見直し）。"""
        url, tok = self._row_url(), self.token("A")
        empty = self.post("A", url + "/fix", {"form_token": tok, "field": "source_name", "value": ""})
        self.assertIn("新しい値を入れてください", empty.text)
        self.assertNotIn("この内容で直します", empty.text)
        forged = self.post("A", url + "/fix", {"form_token": tok, "field": "source_name", "value": "", "go": "1"})
        self.assertIn("ok=0", forged.headers["location"])                     # 確かめを飛ばして送っても消さない
        self.assertNotEqual(self._tasks_now("source_name"), None)
        both = self.post("A", url + "/fix", {"form_token": tok, "field": "source_name", "value": "架空", "clear": "1"})
        self.assertIn("新しい値を空にしてください", both.text)
        chosen = self.post("A", url + "/fix", {"form_token": tok, "field": "source_name", "value": "", "clear": "1"})
        self.assertIn("この内容で直します", chosen.text)
        self.assertIn("（消す）", chosen.text)
        self.assertIn('name="clear" value="1"', chosen.text)                   # 確かめの画面からの送信にも「消す」を持ち越す

    def test_a_wrong_number_is_refused_with_the_reason_and_nothing_changes(self):
        step = self.post("A", self._row_url() + "/fix", {"form_token": self.token("A"), "field": "dial_phone", "value": "0120"})
        self.assertEqual(step.status_code, 200)
        self.assertIn("same", step.text)                                       # 番号の直しは、まず担当の方の選び
        r = self.post("A", self._row_url() + "/fix", {"form_token": self.token("A"), "field": "dial_phone", "value": "番号ではない",
                                                     "same": "1", "go": "1"})
        self.assertIn("ok=0", r.headers["location"])                           # core の断りの文が出る（500 にならない）

    def _level(self):
        from salescall.core import exclusions
        return app_access().in_client(OPERATOR, self.a["tenant"], self.a["client"], lambda tx: exclusions.level(tx, self.a["task"]))

    def test_rows_are_marked_not_to_call_with_a_reason_and_can_be_put_back(self):   # 計画書 7-2（レ点・一括）
        url = f"/c/{self.a['client']}/b/{self.a['business']}/runs/{self.a['run']}/exclude"
        tok = self.token("A")
        no_reason = self.post("A", url, {"form_token": tok, "task_id": str(self.a["task"]), "level": "exclude", "reason": " "})
        self.assertIn("ok=0", no_reason.headers["location"])
        self.assertEqual(self._level(), "call")
        try:
            r = self.post("A", url, {"form_token": tok, "task_id": str(self.a["task"]), "level": "force", "reason": "試験（架空）"})
            self.assertIn("ok=1", r.headers["location"])
            self.assertIn("お客様 1件を「1本もかけない」にしました。", unquote(r.headers["location"]))
            same = self.post("A", url, {"form_token": tok, "task_id": str(self.a["task"]), "level": "force", "reason": "試験（架空）"})
            self.assertIn("どれももう「1本もかけない」でした", unquote(same.headers["location"]))   # 何も変わらなかったときは、そう言う
            self.assertEqual(self._level(), "force")
            self.assertIn("1本もかけない", self.get("A", f"/c/{self.a['client']}/b/{self.a['business']}/runs/{self.a['run']}").text)
        finally:
            back = self.post("A", url, {"form_token": tok, "task_id": str(self.a["task"]), "level": "call", "reason": "試験の片付け",
                                        "back": "row"})
        self.assertIn(f"/rows/{self.a['task']}", back.headers["location"])        # 1行の画面から付けたら、1行の画面へ戻る
        self.assertEqual(self._level(), "call")

    def test_rows_of_another_run_or_client_cannot_be_marked_here(self):
        url = f"/c/{self.a['client']}/b/{self.a['business']}/runs/{self.a['run']}/exclude"
        for bad in (self.y["task"], self.b["task"], "これは行の番号ではない"):
            with self.subTest(task=bad):
                r = self.post("A", url, {"form_token": self.token("A"), "task_id": [str(self.a["task"]), str(bad)],
                                         "level": "exclude", "reason": "試験（架空）"})
                self.assertEqual(r.status_code, 404)
        self.assertEqual(self._level(), "call")                                 # 混ぜた行の印も付けない（全部か、何もしないか）

    # ── お客様リストの取込（docs/DASHBOARD.md 5-8） ──
    def _import_base(self):
        return f"/c/{self.a['client']}/b/{self.a['business']}/runs/{self.a['run']}/import"

    def _upload(self, who="A", body=None):
        body = body or ("会社名,電話番号,管理番号\n架空取込商事,%s,T-%s\n番号なし商事,,T-x\n" % (fictional_jp_fixed(), uuid4().hex[:6])).encode()
        return self.http.post(self._import_base(), data={"form_token": self.token(who)}, files={"file": ("一覧.csv", body, "text/csv")},
                              headers={"x-test-person": who}, follow_redirects=False)

    def _count_rows(self):
        return app_access().in_client(OPERATOR, self.a["tenant"], self.a["client"], lambda tx: tx.one(
            "SELECT count(*) AS n FROM salescall.tasks WHERE run_id = %s", (self.a["run"],))["n"])

    def test_a_list_whose_script_is_empty_says_so_before_any_file_is_chosen(self):
        """台本の中身が無いお客様リストでは、取り込みの1番目の手順で理由を出し、「次へ」を押せない形にする
        （前は、ファイルを選んで列を合わせたあとの最後の「次へ」で、core の取り込みに初めて断られた。読む答えは counts.version_ready の1か所）。"""
        from tests.support import full_version
        w = World()
        s = w.setup(tenant=self.a["tenant"])
        empty = full_version(w, s, content_too=False)
        run = w.run(s["tenant"], s["client"], s["business"], empty)
        page = self.get("A", f"/c/{s['client']}/b/{s['business']}/runs/{run}/import").text
        self.assertIn("電話の台本に、まだ中身がありません", page)
        self.assertIn('type="submit" disabled>次へ（列を確かめる）', page)
        self.assertIn('<fieldset class="plain" disabled>', page)                # ファイルを選ぶ欄から押せない形（選んでも進めない作業をさせない）
        self.assertIn("取り込めるのは、電話の台本が入ってからです", page)         # 押せない理由を横に（parts.why_disabled）
        ensure_rules(World(), self.a)                                          # 中身のそろった版（取込の試験と同じ準備）
        ready = self.get("A", f"/c/{self.a['client']}/b/{self.a['business']}/runs/{self.a['run']}/import").text
        self.assertNotIn("電話の台本に、まだ中身がありません", ready)          # 中身のある版では出さない
        self.assertNotIn('type="submit" disabled>次へ', ready)

    def test_a_list_is_imported_through_mapping_and_preview(self):
        ensure_rules(World(), self.a)                                          # 業務設定の中身（CL-2 の取込の試験と同じ準備）
        r = self._upload()
        self.assertEqual(r.status_code, 303, r.text[:300])
        key_url = r.headers["location"]
        page = self.get("A", key_url).text
        self.assertIn('<option value="会社名" selected>', page)                 # 見出しからの案
        self.assertIn('<option value="電話番号" selected>', page)
        before = self._count_rows()
        tok = self.token("A")
        prev = self.post("A", key_url + "/preview", {"form_token": tok, "map_name": "会社名", "map_phone": "電話番号",
                                                     "map_client_ref": "管理番号"})
        self.assertEqual(prev.status_code, 200, prev.text[:300])
        self.assertIn("登録できない行と理由", prev.text)                               # 番号の無い行は止める（理由は core の文）
        self.assertEqual(self._count_rows(), before)                            # プレビューでは何も書かない
        reg = self.post("A", key_url + "/register", {"form_token": tok})
        self.assertIn("ok=1", reg.headers["location"])
        self.assertEqual(self._count_rows(), before + 1)
        self.assertEqual(self.post("A", key_url + "/register", {"form_token": tok}).status_code, 404)   # 置き場から消えた（二度登録しない）

    def test_an_upload_is_only_for_the_person_and_the_run_that_put_it(self):
        key_url = self._upload().headers["location"]
        self.assertEqual(self.get("ADMIN", key_url).status_code, 404)           # 置いた人でない
        other_run = key_url.replace(str(self.a["run"]), str(self.y["run"]))
        self.assertEqual(self.get("A", other_run).status_code, 404)
        self.assertEqual(self.post("A", key_url + "/register", {"form_token": self.token("A")}).status_code, 404)   # プレビュー前
        self.assertEqual(self.get("B", key_url).status_code, 404)

    def test_an_unreadable_file_is_refused_with_the_reason(self):
        r = self.http.post(self._import_base(), data={"form_token": self.token("A")}, files={"file": ("一覧.pdf", b"%PDF", "application/pdf")},
                           headers={"x-test-person": "A"}, follow_redirects=False)
        self.assertIn("ok=0", r.headers["location"])

    # ── 依頼者・業務・実施回を作る（docs/DASHBOARD.md 5-6） ──
    def test_a_client_a_business_and_a_run_are_created_in_order(self):
        tok = self.token("A")
        self.assertIn("ok=0", self.post("A", "/t/clients", {"form_token": tok, "name": "  "}).headers["location"])   # 空の名前は作らない
        r = self.post("A", "/t/clients", {"form_token": tok, "name": "架空の新しい依頼者"})
        self.assertIn("ok=1", r.headers["location"])
        cid = r.headers["location"].split("/")[2].split("?")[0]
        self.assertIn("架空の新しい依頼者", self.get("A", "/t").text)
        self.assertNotIn("架空の新しい依頼者", self.get("B", "/t").text)        # 別の契約者には出ない
        r = self.post("A", f"/c/{cid}/businesses", {"form_token": tok, "name": "架空の新しい業務"})
        self.assertIn("ok=1", r.headers["location"])
        self.assertEqual(self.post("B", f"/c/{cid}/businesses", {"form_token": self.token("B"), "name": "乗っ取り"}).status_code, 404)

    # ── 名前を直す・依頼者を無効にする（計画書 4章 A「登録・編集・無効化」。3回目の見直し「高」の3） ──
    def test_names_are_fixed_in_one_way_for_clients_businesses_runs_and_lines(self):
        tok = self.token("A")
        cid = self.post("A", "/t/clients", {"form_token": tok, "name": "架空の名前を直す依頼者"}).headers["location"].split("/")[2].split("?")[0]
        bid = self.post("A", f"/c/{cid}/businesses", {"form_token": tok, "name": "架空の名前を直す業務"}).headers["location"].split("/")[4].split("?")[0]
        self.assertIn("名前を直す", self.get("A", f"/c/{cid}").text)
        r = self.post("A", f"/c/{cid}/name", {"form_token": tok, "name": "架空の直した依頼者"})
        self.assertEqual(r.headers["location"].split("?")[0], f"/c/{cid}")
        self.assertIn("ok=1", r.headers["location"])
        self.assertIn("架空の直した依頼者", self.get("A", "/t").text)
        self.assertIn("ok=0", self.post("A", f"/c/{cid}/name", {"form_token": tok, "name": "依頼者X"}).headers["location"])   # 同じ名前は断る
        self.assertIn("ok=0", self.post("A", f"/c/{cid}/name", {"form_token": tok, "name": " "}).headers["location"])        # 空の名前は断る
        self.assertEqual(self.post("B", f"/c/{cid}/name", {"form_token": self.token("B"), "name": "乗っ取り"}).status_code, 404)
        self.assertIn("ok=1", self.post("A", f"/c/{cid}/b/{bid}/name", {"form_token": tok, "name": "架空の直した業務"}).headers["location"])
        self.assertIn("架空の直した業務", self.get("A", f"/c/{cid}/b/{bid}").text)
        self.assertEqual(self.post("A", f"/c/{self.y['client']}/b/{bid}/name", {"form_token": tok, "name": "よその業務"}).status_code, 404)
        run = f"/c/{self.a['client']}/b/{self.a['business']}/runs/{self.a['run']}"
        old = re.search(r"<h1>お客様リスト：(.*?)</h1>", self.get("A", run).text).group(1)
        try:
            self.assertIn("ok=1", self.post("A", run + "/name", {"form_token": tok, "name": "架空の直したお客様リスト"}).headers["location"])
            self.assertIn("お客様リスト：架空の直したお客様リスト", self.get("A", run).text)
        finally:
            import html
            self.post("A", run + "/name", {"form_token": tok, "name": html.unescape(old)})
        line = f"/t/lines/{self.a['line']}/name"
        self.assertIn("名前を直す", self.get("A", "/t/lines").text)
        self.assertIn("ok=1", self.post("A", line, {"form_token": tok, "name": "架空の直した回線"}).headers["location"])
        self.assertIn("架空の直した回線", self.get("A", "/t/lines").text)
        self.assertIn("ok=0", self.post("B", line, {"form_token": self.token("B"), "name": "乗っ取り"}).headers["location"])

    def test_a_client_is_made_inactive_and_back_with_a_reason(self):
        tok = self.token("A")
        cid = self.post("A", "/t/clients", {"form_token": tok, "name": "架空の無効にする依頼者"}).headers["location"].split("/")[2].split("?")[0]
        home = self.get("A", "/t").text                                                   # まだ何も無い依頼者は、ホームの表に並べず名前だけの1行に
        quiet = home[home.index('class="quiet-clients'):]
        self.assertIn(f'<a href="/c/{cid}">', quiet[:quiet.index("</p>")])
        self.assertNotIn(f'<td><a href="/c/{cid}">', home)
        settings_page = self.get("A", f"/c/{cid}/settings").text
        self.assertIn("この依頼者を無効にする", settings_page)
        self.assertRegex(settings_page, r'data-confirm-ok="無効にする" data-confirm-tone="danger"')   # 最後の一押しは赤（app.js の1か所）
        self.assertIn("ok=0", self.post("A", f"/c/{cid}/active", {"form_token": tok, "active": "0", "reason": ""}).headers["location"])   # 理由が要る
        r = self.post("A", f"/c/{cid}/active", {"form_token": tok, "active": "0", "reason": "架空の取引を終えた"})
        self.assertIn("ok=1", r.headers["location"])
        self.assertEqual(r.headers["location"].split("?")[0], f"/c/{cid}/settings")
        active = app_access().in_tenant(OPERATOR, self.a["tenant"], lambda tx: tx.one("SELECT salescall.client_active(%s) AS a", (cid,))["a"])
        self.assertFalse(active)                                                            # 門が見るのと同じ1か所
        page = self.get("A", f"/c/{cid}").text
        self.assertIn("この依頼者は無効です", page)
        self.assertIn("架空の取引を終えた", page)
        self.assertIn("（無効）", page)                                                     # タブにも
        home = self.get("A", "/t").text
        self.assertIn("無効</span>", home[home.index(f'<td><a href="/c/{cid}">'):].split("</tr>")[0])   # ホームの依頼者ごとの表にも
        self.assertIn("有効に戻す", self.get("A", f"/c/{cid}/settings").text)
        self.assertEqual(self.post("B", f"/c/{cid}/active", {"form_token": self.token("B"), "active": "1", "reason": "乗っ取り"}).status_code, 404)
        self.assertIn("ok=1", self.post("A", f"/c/{cid}/active", {"form_token": tok, "active": "1", "reason": "架空の取引を再開"}).headers["location"])
        self.assertNotIn("この依頼者は無効です", self.get("A", f"/c/{cid}").text)

    def test_a_run_takes_only_a_version_of_its_own_business(self):
        url = f"/c/{self.a['client']}/b/{self.a['business']}/runs"
        tok = self.token("A")
        other = self.post("A", url, {"form_token": tok, "name": "架空の実施回", "version_id": str(self.y["version"])})
        self.assertEqual(other.status_code, 404)                                # 別の依頼者の業務の版は使えない
        none = self.post("A", url, {"form_token": tok, "name": "架空の実施回", "version_id": ""})
        self.assertIn("ok=0", none.headers["location"])
        ensure_rules(World(), self.a)
        r = self.post("A", url, {"form_token": tok, "name": "架空の2つ目の実施回", "version_id": str(self.a["version"])})
        self.assertIn("ok=1", r.headers["location"])
        self.assertIn("/import", r.headers["location"])                         # 作ったら、取り込みへ

    # ── 回線と PC（docs/DASHBOARD.md 5-5） ──
    def test_a_line_is_added_assigned_and_retired_with_reasons(self):
        tok = self.token("A")
        method = app_access().in_tenant(OPERATOR, self.a["tenant"], lambda tx: tx.one(
            "SELECT method FROM salescall.line_methods ORDER BY added_at LIMIT 1"))["method"]
        bad = self.post("A", "/t/lines", {"form_token": tok, "name": "架空の回線", "method": method, "caller": "番号ではない"})
        self.assertIn("ok=0", bad.headers["location"])                         # core の断りの文が出る（何も作らない）
        caller = fictional_caller()
        r = self.post("A", "/t/lines", {"form_token": tok, "name": "架空の画面試験の回線", "method": method, "caller": caller})
        self.assertIn("ok=1", r.headers["location"])
        page = self.get("A", "/t/lines").text
        self.assertIn("架空の画面試験の回線", page)
        self.assertNotIn("架空の画面試験の回線", self.get("B", "/t/lines").text)
        lid = app_access().in_tenant(OPERATOR, self.a["tenant"], lambda tx: tx.one(
            "SELECT line_id FROM salescall.lines WHERE name = '架空の画面試験の回線'"))["line_id"]
        # 回線の状態（電話網の側）と、PC につながっているか（こちらの側）を分けて見せる（PC が無いと、元気な回線でも電話はかからない）
        def row_of(text):
            at = text.index("架空の画面試験の回線")
            return text[at:text.index("</tr>", at)]
        from salescall.core import agents
        from salescall.web import counts as _counts
        if app_access().in_tenant(OPERATOR, self.a["tenant"], _counts.tenant_system)["pcs"] == 0:
            self.assertNotIn("どの PC にもつながっていません", row_of(page))     # PC が1台も無いことは上の札が1回だけ言う（回線ごとにくり返さない）
            self.assertIn("電話をつなぐ PC がありません", page)
        p = agents.issue(app_access(), SYSTEM, self.a["tenant"], label="架空の画面試験の PC")
        self.assertIn("どの PC にもつながっていません", row_of(self.get("A", "/t/lines").text))   # PC はあるのに、この回線を受け持っていない
        app_access().in_system(SYSTEM, lambda tx: agents.beat(tx, p.pc_id, [lid]))
        served = row_of(self.get("A", "/t/lines").text)
        self.assertIn("PC「架空の画面試験の PC」", served)
        self.assertNotIn("どの PC にもつながっていません", served)
        app_access().in_tenant(OPERATOR, self.a["tenant"], lambda tx: agents.revoke(tx, p.pc_id))   # ほかの試験の PC の数を変えない
        self.assertEqual(self.post("A", f"/t/lines/{lid}/assign", {"form_token": tok, "client_id": str(self.b["client"])}).status_code, 404)
        self.assertIn("ok=1", self.post("A", f"/t/lines/{lid}/assign", {"form_token": tok, "client_id": str(self.a["client"])}).headers["location"])
        page = self.get("A", "/t/lines").text                                  # 使用停止（元に戻せない）は、どの回線かを名前つきで見せてから（計画書 5-1）
        self.assertIn("「架空の画面試験の回線」を使用停止にする", page)
        self.assertIn('<p><b>回線「架空の画面試験の回線」</b>', page)
        self.assertIn("ok=0", self.post("B", f"/t/lines/{lid}/retire", {"form_token": self.token("B"), "reason": "乗っ取り"}).headers["location"])
        self.assertIn("ok=0", self.post("A", f"/t/lines/{lid}/retire", {"form_token": tok, "reason": ""}).headers["location"])
        self.assertIn("ok=1", self.post("A", f"/t/lines/{lid}/retire", {"form_token": tok, "reason": "試験の片付け"}).headers["location"])

    # ── 発信禁止（docs/DASHBOARD.md 5-10） ──
    def test_an_operation_sent_twice_from_the_same_page_works_once(self):   # 二度押しの札（DASHBOARD.md 8-1。札は core の once の1か所）
        page = self.get("A", f"/c/{self.a['client']}/dnc").text
        tok = re.search(r'<meta name="form-token" content="([^"]+)"', page).group(1)
        self.assertIn("~", tok)                                                  # 画面を開くたびに出す札が、なりすましの札につながっている
        number = fictional_jp_fixed()
        data = {"form_token": tok, "phone": number, "reason": "試験：二度押し"}
        no_reason = self.post("A", f"/c/{self.a['client']}/dnc", {**data, "reason": ""})
        self.assertIn("ok=0", no_reason.headers["location"])                     # 断られた操作では札を使わない（もう一度送れる）
        first = self.post("A", f"/c/{self.a['client']}/dnc", data)
        self.assertIn("ok=1", first.headers["location"])
        from salescall.web import names
        from salescall.core import phone as _phone
        self.assertIn(f"{names.phone_text(_phone.normalize(number).e164)} を登録しました", unquote(first.headers["location"]))   # どの番号かを言う
        second = self.post("A", f"/c/{self.a['client']}/dnc", data)               # 同じ画面からもう1回（二度押し）
        self.assertIn("ok=0", second.headers["location"])
        count = app_access().in_client(OPERATOR, self.a["tenant"], self.a["client"], lambda tx: tx.one(
            "SELECT count(*) AS n FROM salescall.dnc_entries WHERE note = %s", ("試験：二度押し",)))["n"]
        self.assertEqual(count, 1)                                               # 1回だけ効いた
        other = re.search(r'<meta name="form-token" content="([^"]+)"', self.get("B", "/t").text).group(1)
        stolen = self.post("A", f"/c/{self.a['client']}/dnc", {**data, "form_token": tok.split("~")[0] + "~" + other.split("~")[1]})
        self.assertIn("ok=0", stolen.headers["location"])                        # ほかの方の札は使えない（データベースが断る）

    def test_a_number_is_registered_not_to_call_and_revoked_with_a_reason(self):
        tok = self.token("A")
        number = fictional_jp_fixed()
        self.assertIn("ok=0", self.post("A", f"/c/{self.a['client']}/dnc", {"form_token": tok, "phone": "番号ではない", "reason": "試験"}).headers["location"])
        self.assertIn("ok=0", self.post("A", f"/c/{self.a['client']}/dnc", {"form_token": tok, "phone": number}).headers["location"])   # 理由が無ければ入れない（CL-2 4-430 の 25）
        self.assertIn("ok=1", self.post("A", f"/c/{self.a['client']}/dnc", {"form_token": tok, "phone": number,
                                                                          "reason": "試験：依頼者から、かけないでとのご連絡"}).headers["location"])
        entry = app_access().in_client(OPERATOR, self.a["tenant"], self.a["client"], lambda tx: tx.one(
            "SELECT entry_id FROM salescall.dnc_entries WHERE reason = 'manual' AND revoked_at IS NULL ORDER BY registered_at DESC LIMIT 1"))["entry_id"]
        page = self.get("A", f"/c/{self.a['client']}/dnc").text
        self.assertIn("ダッシュボードで登録した", page)
        self.assertIn("試験：依頼者から、かけないでとのご連絡", page)                                     # なぜ入れたかが一覧に残る
        self.assertEqual(self.get("A", f"/c/{self.b['client']}/dnc").status_code, 404)                   # 別の契約者の依頼者
        self.assertIn("ok=0", self.post("A", f"/c/{self.y['client']}/dnc/revoke",                        # 別の依頼者の住所からは取り消せない
                                         {"form_token": tok, "entry_id": str(entry), "reason": "試験"}).headers["location"])
        self.assertIn("ok=0", self.post("A", f"/c/{self.a['client']}/dnc/revoke", {"form_token": tok, "entry_id": str(entry), "reason": ""}).headers["location"])
        self.assertIn("ok=1", self.post("A", f"/c/{self.a['client']}/dnc/revoke",
                                         {"form_token": tok, "entry_id": str(entry), "reason": "試験の片付け"}).headers["location"])

    def test_the_contractor_list_is_per_contractor(self):
        tok = self.token("A")
        self.assertIn("ok=0", self.post("A", "/t/dnc", {"form_token": tok, "phone": fictional_jp_fixed(), "reason": ""}).headers["location"])
        self.assertIn("ok=1", self.post("A", "/t/dnc", {"form_token": tok, "phone": fictional_jp_fixed(), "reason": "試験（架空）"}).headers["location"])
        self.assertIn("試験（架空）", self.get("A", "/t/dnc").text)
        self.assertNotIn("試験（架空）", self.get("B", "/t/dnc").text)

    def test_a_contractor_price_is_added_and_only_this_contractor_sees_it(self):
        prices = app_access().in_tenant(OPERATOR, self.a["tenant"], __import__("salescall.core.usage", fromlist=["x"]).price_table)
        if not prices:
            self.skipTest("初めの単価がありません")
        p = prices[0]
        form = {"form_token": self.token("A"), "source": p["source"], "service": p["service"], "unit": p["unit"], "tier": p["tier"] or "",
                "yen": "12.5", "reason": ""}
        self.assertIn("ok=0", self.post("A", "/t/costs/price", form).headers["location"])            # 理由が要る
        self.assertIn("ok=0", self.post("A", "/t/costs/price", {**form, "yen": "十円", "reason": "試験"}).headers["location"])
        self.assertIn("ok=1", self.post("A", "/t/costs/price", {**form, "reason": "試験（架空）"}).headers["location"])
        mine = [x for x in app_access().in_tenant(OPERATOR, self.a["tenant"], __import__("salescall.core.usage", fromlist=["x"]).price_table)
                if (x["source"], x["service"], x["unit"], x["tier"]) == (p["source"], p["service"], p["unit"], p["tier"])][0]
        self.assertTrue(mine["by_contractor"])
        theirs = [x for x in app_access().in_tenant(OPERATOR, self.b["tenant"], __import__("salescall.core.usage", fromlist=["x"]).price_table)
                  if (x["source"], x["service"], x["unit"], x["tier"]) == (p["source"], p["service"], p["unit"], p["tier"])][0]
        self.assertFalse(theirs["by_contractor"])                                # 別の契約者には効かない

    # ── 停止と再開 ──
    def test_a_stop_of_another_contractor_is_not_seen(self):   # S-18
        eid = self._stop(self.b["tenant"], "契約者Bの試験の停止")
        self.assertNotIn("契約者Bの試験の停止", self.get("A", "/t/stops").text)
        self.assertEqual(self.get("A", f"/t/stops/{eid}").status_code, 404)
        self.assertIn("契約者Bの試験の停止", self.get("B", "/t/stops").text)

    def test_resuming_needs_the_page_token_and_a_reason(self):
        eid = self._stop(self.a["tenant"], "再開の試験の停止")
        page = f"/t/stops/{eid}"
        self.assertIn("再開の試験の停止", self.get("A", page).text)
        tok = self.token("A", page)
        # 札が無い・違う ⇒ 何も変わらない
        r = self.post("A", page + "/resume", {"reason": "確かめた"})
        self.assertIn("ok=0", r.headers["location"])
        r = self.post("A", page + "/resume", {"reason": "確かめた", "form_token": "x.y"})
        self.assertIn("ok=0", r.headers["location"])
        # 理由が空 ⇒ 何も変わらない
        r = self.post("A", page + "/resume", {"reason": "  ", "form_token": tok})
        self.assertIn("ok=0", r.headers["location"])
        self.assertTrue(self._open(self.a["tenant"], eid))
        # 契約者Bの札では、契約者Aの人として送れない
        r = self.post("A", page + "/resume", {"reason": "確かめた", "form_token": self.token("B")})
        self.assertIn("ok=0", r.headers["location"])
        # 札と理由がそろえば再開する
        r = self.post("A", page + "/resume", {"reason": "試験：確かめた", "form_token": tok})
        self.assertIn("ok=1", r.headers["location"])
        self.assertFalse(self._open(self.a["tenant"], eid))

    def test_another_contractor_cannot_resume_a_stop(self):   # S-14
        eid = self._stop(self.a["tenant"], "Bが解けない停止")
        r = self.post("B", f"/t/stops/{eid}/resume", {"reason": "解く", "form_token": self.token("B")})
        self.assertIn("ok=0", r.headers["location"])
        self.assertTrue(self._open(self.a["tenant"], eid))

    def test_a_stop_of_one_client_is_listed_in_the_whole_view_and_resumed_under_that_client(self):   # 2026-11-18 の誤りの試験
        eid = app_access().in_client(OPERATOR, self.a["tenant"], self.y["client"], lambda tx: stops.stop(
            tx, scope="client", cause="manual", reason="依頼者Yだけの試験の停止", tenant_id=self.a["tenant"],
            client_id=self.y["client"]))["event_id"]
        whole = self.get("A", "/t/stops").text
        self.assertIn("依頼者Yだけの試験の停止", whole)            # 契約者の取引だけで読むと、ここに出ない
        self.assertIn(f"/c/{self.y['client']}/stops/{eid}", whole)
        self.assertNotIn("依頼者Yだけの試験の停止", self.get("B", "/t/stops").text)
        self.assertEqual(self.get("A", f"/c/{self.a['client']}/stops/{eid}").status_code, 404)   # 依頼者Xの住所では開けない
        page = f"/c/{self.y['client']}/stops/{eid}"
        self.assertIn("依頼者Yだけの試験の停止", self.get("A", page).text)
        r = self.post("A", page + "/resume", {"reason": "試験：確かめた", "form_token": self.token("A")})
        self.assertIn("ok=1", r.headers["location"])
        self.assertNotIn("依頼者Yだけの試験の停止", self.get("A", "/t/stops").text)

    # ── 文字は文字として出す ──
    def test_a_name_from_a_file_is_shown_as_text(self):   # S-17
        app_access().in_tenant(OPERATOR, self.a["tenant"], lambda tx: tx.execute(
            "UPDATE salescall.clients SET name = '<script>alert(1)</script>' WHERE client_id = %s", (self.y["client"],)))
        try:
            page = self.get("A", "/t").text
            self.assertNotIn("<script>alert(1)</script>", page)
            self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", page)
        finally:
            app_access().in_tenant(OPERATOR, self.a["tenant"], lambda tx: tx.execute(
                "UPDATE salescall.clients SET name = '依頼者Y' WHERE client_id = %s", (self.y["client"],)))

    def test_every_linked_page_opens_in_japanese_only(self):   # 全部のリンクをたどる（画面を足しても漏れを見つける）
        # 探すの画面は、リンクでなく探す欄から開くので、はじめから入れる
        self._crawl("A", ["/t", f"/c/{self.a['client']}/b/{self.a['business']}", f"/c/{self.a['client']}/search?q=架空"])

    def test_every_page_the_admin_sees_opens_in_japanese_only(self):   # 管理者だけの画面（システムの初めの値・全体の知らせ など）も
        self._crawl("ADMIN", ["/t", "/admin/settings", "/t/notices", f"/c/{self.a['client']}/b/{self.a['business']}"])

    def _crawl(self, who, start):
        import html as _html
        from collections import deque
        internal = re.compile(r"\b[a-z]+(?:_[a-z0-9]+)+\b|\b[a-z]+\.[a-z][a-z0-9_]+\b")
        seen, todo, problems = set(), deque(start), []
        while todo and len(seen) < 300:
            path = todo.popleft()
            if path in seen:
                continue
            seen.add(path)
            r = self.get(who, path)
            if r.status_code != 200:
                problems.append((path, r.status_code))
                continue
            if "text/html" not in r.headers.get("content-type", ""):
                continue
            visible = _html.unescape(re.sub(r"<[^>]+>", " ", re.sub(r"<script.*?</script>|<style.*?</style>", "", r.text, flags=re.S)))
            if words_missing() in visible:
                problems.append((path, "言葉の抜け"))
            problems += [(path, m) for m in internal.findall(visible)]
            problems += [(path, "開発の覚え書き：" + m.group(0)) for m in re.finditer(DEV_NOTE, visible)]   # 画面に開発の根拠を出さない
            problems += [(path, "名前の無い欄：" + d) for d in _unnamed_fields(r.text)]   # どの欄にも、何を入れる欄かの名前を結び付ける
            # 道しるべの最後（いまいる所）が依頼者・業務・お客様リストの名前なら、その画面そのものにいるときだけ（中の画面が名前を渡し
            # 忘れると、依頼者の画面にいるように見える＝3回目の見直し「中」の9）
            here = re.search(r'<span class="here" aria-current="page">(.*?)</span>', r.text)
            where = path.split("?")[0].split("#")[0]
            for head, own in (("依頼者「", r"/c/[^/]+"), ("業務「", r"/c/[^/]+/b/[^/]+"), ("お客様リスト「", r"/c/[^/]+/b/[^/]+/runs/[^/]+")):
                if here and here.group(1).startswith(head) and not re.fullmatch(own, where):
                    problems.append((path, "いまいる所の名前が無い：" + here.group(1)))
            # 取り消せない操作・電話を再開させる操作の送信は、どれも確かめの窓（data-confirm＝名前つきの文）を通す（計画書 5-1。3回目の見直し「高」の2）
            for tag_ in re.findall(r"<form\b[^>]*>", r.text):
                act = re.search(r'action="([^"]*)"', tag_)
                if act and re.search(DANGER_ACTIONS, act.group(1)) and 'data-confirm-tone="danger"' not in tag_:
                    problems.append((path, "赤の確かめの無い危ない操作：" + act.group(1)))
                if act and re.search(CONFIRMED_ACTIONS, act.group(1)) \
                        and "data-confirm=" not in tag_:
                    problems.append((path, "確かめの無い取り消せない操作：" + act.group(1)))
            if who != "ADMIN":   # 契約者の画面に内部の言い方を出さない（何が内部の言い方かは words.CONTRACTOR_INNER の1か所。管理者には覚え書きも出してよい）
                from salescall.output import words as _w
                problems += [(path, "内部の言い方：" + m.group(0)) for m in _w.CONTRACTOR_INNER.finditer(visible)]
            todo += [h for h in re.findall(r'href="(/[^"#?]*)', r.text)   # /t/group はタブのグループを選ぶ操作（選んで元の画面へ戻す）
                     if not h.startswith(("/static", "/pc-login")) and h != "/t/group" and h not in seen]
        self.assertGreater(len(seen), 10)
        self.assertEqual(problems, [])

    def test_a_notice_for_the_admin_is_shown_to_the_admin_only(self):   # 誰に見せるかはデータベースの1か所（画面で絞り直さない）
        from salescall.core import notify
        kind = next(iter(notify.KINDS))
        app_access().in_tenant(OPERATOR, self.a["tenant"], lambda tx: notify.emit(
            tx, kind, key="試験：管理者だけの知らせ", summary="架空の管理者あての知らせ", admin=True, contractor=False))
        self.assertNotIn("架空の管理者あての知らせ", self.get("A", "/t/notices").text)
        self.assertIn("架空の管理者あての知らせ", self.get("ADMIN", "/t/notices").text)

    def test_a_notice_written_with_the_fixer_name_is_shown_in_screen_words(self):   # 知らせの本文はデータの文＝words.plain の1か所を通す
        from salescall.core import notify
        kind = next(iter(notify.KINDS))
        app_access().in_tenant(OPERATOR, self.a["tenant"], lambda tx: notify.emit(
            tx, kind, key="試験：担当の名前の入った知らせ", summary="CL-2 に修正依頼：架空の不具合（CL-2 4-425）", admin=True))
        for who in ("A", "ADMIN"):   # 契約者あて・管理者あての両方に同じ本文が出る
            with self.subTest(who=who):
                page = self.get(who, "/t/notices").text
                self.assertIn("業務の土台の担当に修正依頼：架空の不具合", page)
                self.assertNotRegex(page, r"CL-\d")

    def test_a_notice_waiting_for_the_bundle_is_on_the_screen_at_once_with_when(self):   # ✅ 2026-11-19 管理者「ダッシュボードには全てすぐに報告」
        import unittest.mock
        from datetime import datetime
        from salescall.core import notify
        from salescall.output import notices
        app_access().in_tenant(OPERATOR, self.a["tenant"], lambda tx: notify.emit(
            tx, "quality_stage", key="試験：朝にまとめる知らせ", summary="架空の朝にまとめる知らせ"))
        later = lambda tx, tenant_id: (lambda e: notices.SendTime(datetime(2030, 1, 2, 7, 0), True))   # noqa: E731
        with unittest.mock.patch.object(notices, "schedule", later):
            page = self.get("A", "/t/notices").text
        row = page[page.index("架空の朝にまとめる知らせ"):]
        self.assertIn("まとめて送ります", row[:600])                              # 送る前でも、画面にはすぐ出る
        self.assertIn("1月2日7時", row[:600])                                      # いつ送るか（書き方は画面の1か所）

    def test_every_fixer_has_a_screen_name(self):
        from salescall.core import fix_requests
        from salescall.output import words
        self.assertEqual(set(words.ASSIGNEE_WORDS), set(fix_requests.ASSIGNEES))

    def test_an_undelivered_notice_for_the_admin_is_watched_for_the_admin_only(self):   # 送信も見張りも、契約者あてと同じ1か所
        from salescall.core import notify
        from salescall.output import notices
        from tests.support import ADMIN

        class Broken:
            def send(self, text):
                raise notices.SendFailed("試験：つながらない")
        kind = next(iter(notify.KINDS))
        app_access().in_tenant(OPERATOR, self.a["tenant"], lambda tx: notify.emit(
            tx, kind, key="試験：管理者あての届かない知らせ", summary="架空の届かない管理者あての知らせ", admin=True, contractor=False))
        eid = app_access().in_system(ADMIN, lambda tx: tx.one(
            "SELECT event_id FROM salescall.notification_events WHERE event_key = %s AND for_admin", ("admin:試験：管理者あての届かない知らせ",)))["event_id"]
        got = [notices.deliver_event(app_access(), SYSTEM, None, eid, {"sms": Broken()}, 1) for _ in range(2)]
        self.assertEqual(got, ["failed", "gave_up"])                             # 管理者あても、回数を使い切れば記録して止める
        admin = self.get("ADMIN", "/t/watch").text
        part = admin[admin.index('id="undelivered_admin"'):]
        self.assertIn("架空の届かない管理者あての知らせ", part[:3000])
        self.assertIn("/t/notices#admin-notices", part[:3000])                  # 知らせの画面の「管理者あて」へ飛べる
        self.assertIn("送れていない管理者あての知らせ", self.get("ADMIN", "/t").text)   # 管理者のホームの「気をつけること」
        operator = self.get("A", "/t/watch").text
        self.assertNotIn('id="undelivered_admin"', operator)                    # 契約者には項目ごと出さない
        self.assertNotIn("架空の届かない管理者あての知らせ", operator)

    def test_a_new_client_shows_how_to_begin_with_making_a_business_on_the_spot(self):
        """依頼者を作った直後（業務がまだ無い）は、依頼者の画面の「はじめかた」で、その場で業務を作れる（作る受け口はタブの「＋ 業務」と同じ）。"""
        r = self.post("A", "/t/clients", {"form_token": self.token("A"), "name": "架空のはじめての依頼者"})
        page = self.get("A", r.headers["location"].split("?")[0]).text
        self.assertIn("<h2>はじめかた</h2>", page)
        cid = r.headers["location"].split("/c/")[1].split("?")[0]
        self.assertIn(f'<form class="inline-create" method="post" action="/c/{cid}/businesses">', page)
        self.assertNotIn('id="runs"', page)                                    # 業務が無いあいだは、お客様リストの段を出さない
        made = self.post("A", f"/c/{cid}/businesses", {"form_token": self.token("A"), "name": "架空のはじめての業務"})
        self.assertEqual(made.status_code, 303)
        self.assertNotIn('class="inline-create"', self.get("A", f"/c/{cid}").text)   # 業務ができたら、もう出さない
        self.assertNotIn('class="inline-create"', self.get("A", f"/c/{self.a['client']}").text)

    def test_a_new_business_shows_how_to_begin_and_a_running_one_does_not_ask_again(self):   # はじめかた（counts.setup_steps）
        r = self.post("A", f"/c/{self.a['client']}/businesses", {"form_token": self.token("A"), "name": "架空の新しく始める業務"})
        self.assertEqual(r.status_code, 303)
        page = self.get("A", r.headers["location"].split("?")[0]).text
        self.assertIn("<h2>はじめかた</h2>", page)
        card = page[page.index("<h2>はじめかた</h2>"):page.index("</section>", page.index("<h2>はじめかた</h2>"))]
        self.assertNotIn("結果別の行の数", page)                                 # 行の無い業務では、0 ばかりの数の枠を出さない
        self.assertIn("電話の台本を入れる", card)
        from salescall.output import words as w
        self.assertIn(w.SCRIPT_NOT_READY, card)                                  # 台本を作る画面がまだ無いことを、どこでも同じ1つの文で
        self.assertIn('class="next"', card)                                     # 次にすることを強める
        self.assertIn('<button class="btn" type="button" disabled>＋ お客様リストを作る</button>', page)   # 台本が無いあいだは押せない形
        self.assertIn("電話の台本が入ってから作れます", page)                   # 押せない理由を横に（parts.why_disabled）
        self.assertIn('<span class="mark" aria-hidden="true">1</span>', card)   # 残っている手順は1から数える
        self.assertIn("回線と PC を用意する", card)                               # 電話をつなぐ PC も手順に入る（PC が無いと電話はかからない）
        self.assertEqual(card.count("済み"), 0)                                  # 試験の契約者には回線はあるが PC が無い＝まだ
        running = self.get("A", f"/c/{self.a['client']}/b/{self.a['business']}").text
        # 動いている業務でも、PC が無ければ電話はかからないので、手順は「回線と PC を用意する」だけが残る（前は PC を見ずに「そろった」としていた）
        steps = running[running.index("<h2>はじめかた</h2>"):running.index("</section>", running.index("<h2>はじめかた</h2>"))]
        self.assertEqual(steps.count("まだ</span>"), 2)
        self.assertIn("回線と PC を用意する", steps)
        self.assertIn("知らせの受け先を決める", steps)                           # 知らせの受け先も手順に入る（counts.readiness と同じ答え）
        # 設定の画面は、決めれば直る設定（受け先）に「決めてください」を付ける（どれかは counts.readiness の keys の1か所）
        settings_page = self.get("A", "/t/settings").text
        from salescall.web import counts
        for key in counts.NOTIFY_KEYS:
            row = settings_page[settings_page.index(f'id="s-{key.replace(".", "-")}"'):]
            self.assertIn("決めてください", row[:row.index("</summary>")])
        self.assertNotIn("決めてください", self.get("A", f"/c/{self.a['client']}/settings").text)   # 依頼者の設定には出さない

    def test_a_notice_about_a_customer_says_where_and_links_to_the_row(self):   # 知らせの「どこの」「見る」（links の1か所）
        from salescall.core import notify
        app_access().in_client(OPERATOR, self.a["tenant"], self.a["client"], lambda tx: notify.emit(
            tx, "complaint", key=f"試験：行の知らせ:{uuid4()}", summary="架空の行についての知らせ", refs={"task_id": str(self.a["task"])}))
        page = self.get("A", "/t/notices").text
        at = page.index("架空の行についての知らせ")
        row = page[page.rindex("<tr>", 0, at):page.index("</tr>", at)]
        self.assertIn("依頼者X", row)
        self.assertIn(f"/rows/{self.a['task']}", row)
        self.assertNotIn(f"/rows/{self.a['task']}", self.get("B", "/t/notices").text)   # ほかの契約者には出ない

    def test_every_page_inside_a_client_shows_where_you_are(self):   # 道筋（base.html の1か所）：どの依頼者・業務を操作しているか
        for path in (f"/c/{self.a['client']}", f"/c/{self.a['client']}/b/{self.a['business']}",
                     f"/c/{self.a['client']}/b/{self.a['business']}/runs/{self.a['run']}", self._row_url(),
                     f"/c/{self.a['client']}/b/{self.a['business']}/runs/{self.a['run']}/results", self._import_base(),
                     f"/c/{self.a['client']}/dnc", f"/c/{self.a['client']}/settings"):
            with self.subTest(path=path):
                page = self.get("A", path).text
                crumbs = page[page.index('class="crumbs"'):page.index("</nav>", page.index('class="crumbs"'))]
                self.assertIn("依頼者「依頼者X」", crumbs)
                # 最後はいつも「いまいる所」（押せない字）。自分へのリンクで終わらない（2026-11-20 の見直し）
                self.assertRegex(crumbs, r'<span class="here" aria-current="page">[^<]+</span>\s*$')

    def test_every_page_has_one_name_and_marks_itself_in_the_side_nav(self):
        """画面の名前は words.PAGES の1か所（左の道しるべ・見出し・ホームの札が同じ名前）。開いた画面は、左の道しるべで自分に印が付く。"""
        from salescall.output import words as w
        for key, (name, href) in w.PAGES.items():
            with self.subTest(page=key):
                page = self.get("A", href).text
                self.assertIn(f'<a href="{href}" aria-current="page">{name}</a>', page)
        self.assertNotIn("見張り", self.get("A", "/t").text + self.get("A", "/t/watch").text)   # 前は「見張り」と「気をつけること」の2つの名前

    def test_inside_a_client_the_side_nav_shows_where_you_are(self):
        """依頼者・業務の中では、左の道しるべに、その中の画面のまとまりが出て、開いている画面に印が付く（名前は words の1か所）。"""
        from salescall.output import words as w
        c, b = self.a["client"], self.a["business"]
        for path, name in ((f"/c/{c}", w.CLIENT_NAV[""]), (f"/c/{c}/dnc", w.CLIENT_NAV["/dnc"]),
                           (f"/c/{c}/b/{b}", w.BUSINESS_NAV[""]), (f"/c/{c}/b/{b}/settings", w.BUSINESS_NAV["/settings"])):
            with self.subTest(path=path):
                page = self.get("A", path).text
                self.assertIn(f'<a href="{path}" aria-current="page">{name}</a>', page)
        self.assertNotIn("nav-place", self.get("A", "/t").text)                  # 全体の画面では出さない

    def test_the_way_back_is_the_breadcrumb_only(self):
        """戻り方はパンくずの1か所（全体の画面の下の画面も、親の画面へ戻れる）。ページごとの「← 戻る」は書かない
        （取り込みの手順の中で1つ前の手順へ戻るリンクは別）。"""
        import pathlib
        tpl = pathlib.Path(__file__).resolve().parents[1].joinpath("src/salescall/web/templates")
        backs = [f.name for f in tpl.glob("*.html") if re.search(r">\s*← ", f.read_text(encoding="utf-8"))]
        self.assertEqual(backs, [])
        page = self.get("A", "/t/dnc/import").text
        self.assertIn('<nav class="crumbs" aria-label="いまいる所">', page)
        self.assertIn('<a href="/t/dnc">共通の発信禁止</a>', page)

    def test_every_empty_state_says_what_comes_next(self):
        """空のときの形（parts.empty）は、次にすること・これから何が起きるかを必ず書く（「登録はありません」だけで終えない）。"""
        import pathlib
        bare = []
        for f in sorted(pathlib.Path(__file__).resolve().parents[1].joinpath("src/salescall/web/templates").glob("*.html")):
            for m in re.finditer(r"\bempty\(\s*(\"[^\"]*\"|[^,()\"]+)\s*\)", f.read_text(encoding="utf-8")):
                bare.append(f"{f.name}: {m.group(0)}")
        self.assertEqual(bare, [])

    def test_one_name_for_one_thing(self):
        """1つの物事に1つの呼び方（2026-11-20 の見直し）：お客様リストの中の1件は「お客様」（「行」は、取り込むファイル・渡すファイルの
        中の1行だけ）。止めたものを動かすのは「再開」（「解く」と混ぜない）。画面のひな形を見る（CL-2 の部品の文は、お願い 65）。"""
        import pathlib
        src = pathlib.Path(__file__).resolve().parents[1].joinpath("src/salescall")
        tpl = src / "web/templates"
        odd = []
        # 画面に出る文：すべてのひな形（説明の書き込み {# #} は見ない）・見張りの項目の文（watch.py の文字列）・ブラウザの文（app.js の文字列）・設定の名前
        shown = {f.name: re.sub(r"\{#.*?#\}", "", f.read_text(encoding="utf-8"), flags=re.S) for f in sorted(tpl.glob("*.html"))}
        for name in ("web/watch.py", "web/static/app.js"):
            shown[name] = " ".join(re.findall(r'"([^"\n]*)"', (src / name).read_text(encoding="utf-8")))
        shown["setting_labels.json"] = (src / "output/setting_labels.json").read_text(encoding="utf-8")
        for name, text in shown.items():
            odd += [f"{name}: {m.group(0)}" for m in re.finditer(
                r"(?:この|その|ほかの|まだ|止めた|休んでいる|休ませている|待っている|かけられない|次の)行|行を選ぶ|次の回|「かけないで」の一覧|本番|決め直し中", text)]   # 本番＝画面では「フルオート」（words.MODE_WORDS）・決め直し中＝「結果を確かめ中」（words.NEXT_KINDS の hold）
        # 設定の名前は、どれもお客様の扱いの話（ファイルの行の話は無い）なので、「行」そのものを使わない（実行・行う などの言葉は別）
        odd += [f"setting_labels.json: {m.group(0)}" for m in re.finditer(r"\S{0,6}(?<![実銀旅運])行(?![うっいわ])\S{0,4}", shown["setting_labels.json"])]
        for f in sorted(tpl.glob("*.html")):
            text = re.sub(r"\{#.*?#\}", "", f.read_text(encoding="utf-8"), flags=re.S)
            odd += [f"{f.name}: {m.group(0)}" for m in re.finditer(r"解[くけい]", text)]
        self.assertEqual(odd, [])

    def test_the_metrics_page_opens_for_each_span_and_says_what_comes_next(self):   # 計画書 9-8（数えるのは core の metrics.business）
        base = f"/c/{self.a['client']}/b/{self.a['business']}/metrics"
        for q in ("", "?days=7", "?days=90", "?days=5"):                       # 選びに無い日数は30日
            with self.subTest(q=q):
                r = self.get("A", base + q)
                self.assertEqual(r.status_code, 200, r.text[:300])
                self.assertIn("電話の様子と指標", r.text)
        self.assertEqual(self.get("B", base).status_code, 404)                 # ほかの契約者の業務は見えない
        from unittest import mock
        from salescall.core import attempts, metrics
        from salescall.output import words as w
        rate = {"hits": 1, "total": 4, "value": 0.25}
        fake = {k: (rate if kind == "rate" else 3) for k, _, kind in w.METRIC_ROWS}
        fake.update(unpriced=1, ai_voice={k: None for k in attempts.VOICE_KINDS})
        with mock.patch.object(metrics, "business", return_value=fake):
            page = self.get("A", base).text
        self.assertIn("25.0%（1／4）", page)
        self.assertIn("単価の無い電話 1本は入っていません", page)
        self.assertIn("まだ数えていません", page)                              # 会話AIの声の率は、数えていなければ 0 と書かない

    def test_what_keeps_calls_from_going_out_is_shown_in_one_shape_on_three_pages(self):   # 答えは counts.readiness の1か所
        for path in ("/t", "/t/watch", "/t/lines"):
            with self.subTest(path=path):
                page = self.get("A", path).text
                self.assertIn("電話をつなぐ PC がありません", page)              # 試験の契約者には回線はあるが PC が無い
                self.assertIn("知らせの受け先が決まっていません", page)
        self.assertIn("いまは電話がかかりません", self.get("A", "/t").text)      # 「何もしなくても電話は進みます」と言わない
        # テスト番号（計画書 8-8）：あるかは core の ring_test.usable の1か所。無ければ同じ形で出し、設定の行に「決めてください」
        from salescall.core import ring_test
        usable = app_access().in_tenant(OPERATOR, self.a["tenant"], ring_test.usable)
        home, settings_page = self.get("A", "/t").text, self.get("A", "/t/settings").text
        self.assertEqual("テスト番号が決まっていません" in home, not usable)
        row = settings_page[settings_page.index('id="s-line-test_number"'):]
        self.assertEqual("決めてください" in row[:row.index("</summary>")], not usable)

    def test_a_setting_that_changes_how_calls_go_asks_with_its_name(self):   # 設定の定義の confirm_text の1か所（CL-2 4-470 の (2)）
        import html as _html
        wanted = app_access().in_system(SYSTEM, lambda tx: tx.all(
            "SELECT key, confirm_text, finest_scope::text AS finest_scope FROM salescall.setting_definitions WHERE confirm_text IS NOT NULL"))
        self.assertTrue(wanted)
        pages = {"tenant": self.get("A", "/t/settings").text,
                 "business": self.get("A", f"/c/{self.a['client']}/b/{self.a['business']}/settings").text}
        for r in wanted:
            for level, page in pages.items():
                if level == "business" and r["finest_scope"] != "business":
                    continue
                with self.subTest(key=r["key"], level=level):
                    row = page[page.index(f'id="s-{r["key"].replace(".", "-")}"'):]
                    form = re.search(r"<form\b[^>]*>", row).group(0)
                    self.assertIn('data-confirm="' + ("業務「" if level == "business" else ""), form)   # どこの設定かを名前つきで
                    self.assertIn(_html.escape(r["confirm_text"], quote=True), form)
        plain = app_access().in_system(SYSTEM, lambda tx: tx.one(
            "SELECT key FROM salescall.setting_definitions WHERE confirm_text IS NULL AND finest_scope::text = 'business' ORDER BY key LIMIT 1"))["key"]
        row = pages["business"][pages["business"].index(f'id="s-{plain.replace(".", "-")}"'):]
        self.assertNotIn("data-confirm=", re.search(r"<form\b[^>]*>", row).group(0))   # 文の無い設定は窓を通さない
        business = self.get("A", f"/c/{self.a['client']}/b/{self.a['business']}").text
        self.assertRegex(business, r"<h1>[^<]*<span class=\"tag info\">(検証モード|フルオート)</span></h1>")   # 進め方は見出しの横に

    def test_only_the_top_inbox_card_has_the_primary_button(self):   # 主のボタンは1画面に1つ（承認待ちは急ぐ順。4回目の見直し「中」の7）
        from uuid import uuid4
        from salescall.web import inbox
        from salescall.web.app import _templates
        env = _templates().env
        cards = [inbox.Item("deficit", {"warning_id": str(n)}, "赤字の確認", "確かめてください", buttons=(inbox.Button("confirm", "確かめた", main=True),))
                 for n in range(3)]
        page = env.from_string('{% from "parts.html" import inbox_item %}{% for i in items %}{{ inbox_item(place, i, "t", lead=loop.first) }}{% endfor %}').render(
            items=cards, place={"client_id": uuid4(), "name": "依頼者X"})
        self.assertEqual(visible_primary_buttons(page), ["確かめた"])
        self.assertEqual(page.count("確かめた"), 3)                              # ほかの札のボタンも出ている（白い枠で）

    def test_the_script_reads_form_attributes_not_shadowable_properties(self):
        """画面の動き（app.js）は、form の method・action などを属性で読む（form.method は、名前が method の欄があるとその欄を指す。
        2026-11-21：回線を足す欄で画面の動きが止まり、二度押しの守りが働いていなかった）。"""
        import pathlib
        js = pathlib.Path(__file__).resolve().parents[1].joinpath("src/salescall/web/static/app.js").read_text(encoding="utf-8")
        code = "\n".join(line.split("//")[0] for line in js.splitlines())
        self.assertEqual(re.findall(r"\bform\.(?:method|action|target|encoding|enctype)\b", code), [])

    def test_no_narrow_width_rule_is_beaten_by_a_later_rule_of_the_same_selector(self):
        """狭い幅の決まり（@media max-width）は、あとに書いた同じ部品・同じ性質のふつうの決まりに負けない（負けると黙って効かない。
        ⛔ 2026-11-22：小窓・まとめて変える帯の狭い幅の決まりが効かず、320px で小窓が画面の外へはみ出していた）。"""
        import pathlib
        css = pathlib.Path(__file__).resolve().parents[1].joinpath("src/salescall/web/static/app.css").read_text(encoding="utf-8")
        css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
        items, media = [], None
        for m in re.finditer(r"@media[^{]*\{|[^{}]+\{[^{}]*\}|\}", css):
            t = m.group(0)
            if t.startswith("@media"):
                media = t
                continue
            if t == "}":
                media = None
                continue
            sel, body = t.split("{", 1)
            for one in sel.split(","):
                for d in body.rstrip("}").split(";"):
                    if ":" in d and d.split(":", 1)[0].strip():
                        items.append((m.start(), media, " ".join(one.split()), d.split(":", 1)[0].strip()))
        beaten = sorted({(sel, prop) for at, med, sel, prop in items if med and "max-width" in med
                         and any(at2 > at and med2 is None and sel2 == sel and prop2 == prop for at2, med2, sel2, prop2 in items)
                         and not any(at3 > at and med3 and "max-width" in med3 and sel3 == sel and prop3 == prop
                                     for at3, med3, sel3, prop3 in items)})   # あとの狭い幅の決まりで直してあるものは除く
        self.assertEqual(beaten, [])

    def test_every_i_mark_on_the_screens_has_its_text_in_one_place(self):
        """ひな形で使う i マークの鍵は、どれも core の help_texts.FIELDS にある（説明を画面に写さない・書き漏れを試験で見つける。設計書 6-7）。"""
        import pathlib
        from salescall.core import help_texts
        used = set()
        for f in pathlib.Path(__file__).resolve().parents[1].joinpath("src/salescall/web/templates").glob("*.html"):
            used |= set(re.findall(r'info\("([^"]+)"', f.read_text(encoding="utf-8")))
        self.assertTrue(used)
        self.assertEqual(sorted(used - set(help_texts.FIELDS)), [])

    def test_a_line_is_named_with_its_caller_number(self):   # 同じ名前の回線を見分ける（web/names の1か所。2026-11-21 の見直し）
        from salescall.web import names
        self.assertEqual(names.line_label("スマホ1", "+819000000000"), "スマホ1（090-0000-0000）")
        self.assertEqual(names.line_label("スマホ1", None), "スマホ1")
        self.assertEqual(names.line_label(None, None), "回線")
        # 番号の見せ方は core の phone の1か所（PhoneNumber.display）：区切りが1つに決まる種類だけ区切り、固定電話は推し量って区切らない
        self.assertEqual(names.phone_text("+81312345678"), "0312345678")
        self.assertEqual(names.phone_text("+81120000001"), "0120-000-001")

    def test_every_time_cell_has_one_shape(self):
        """表の時刻の升は、どの表も .when の1つの形（数字の幅はそろえ、左寄せ・折らない）。数量の右寄せ（num）では書かない
        （2026-11-20 の見直し：時刻を右寄せにした表と左寄せの表が混ざり、「いつから」の見出しと値がずれていた）。"""
        import pathlib
        odd = []
        for f in sorted(pathlib.Path(__file__).resolve().parents[1].joinpath("src/salescall/web/templates").glob("*.html")):
            for m in re.finditer(r'<td(?: class="([^"]*)")?>\{\{ *time\(', f.read_text(encoding="utf-8")):
                if m.group(1) != "when":
                    odd.append(f"{f.name}: {m.group(0)}")
        self.assertEqual(odd, [])

    def test_the_home_shows_what_to_look_at_and_how_things_usually_are(self):
        """ホームは2段：いま見ること（止まっている・承認待ち・気をつけること）と、ふだんの様子（今日の電話・回線と PC・今月の費用と報酬。
        計画書 5-2）。どの数も core の答えを数えるだけ。"""
        home = self.get("A", "/t").text
        for word in ("いま見ること", "ふだんの様子", "今日かけた電話", "目的のお客様と話せた", "休んでいる回線", "使っている回線", "今月の費用", "今月の報酬"):
            with self.subTest(word=word):
                self.assertIn(word, home)
        self.assertIn('href="/t/lines"', home)
        self.assertIn("電話をつなぐ PC がありません", home)                      # 回線があって PC が0台＝電話はかからない（counts.readiness の1か所で知らせる）
        self.assertIn('href="/t/costs"', home)

    def test_nothing_is_never_colored(self):
        """色の役割（parts.html の1か所）：0件・「なし」に色を付けない（緑は「済んだ・通った」だけ。何もないと見分けがつかなくなる）。"""
        for who in ("A", "B"):
            home = self.get(who, "/t").text
            self.assertEqual(re.findall(r'<div class="stat (\w+)"><span class="label">[^<]*</span><span class="value">0</span>', home), [])
        self.assertNotIn('<span class="tag ok">なし</span>', self.get("A", "/t/watch").text)

    def test_every_page_has_balanced_tags(self):
        """どの画面も、タグの開きと閉じが合っている（閉じタグの書き過ぎで、表が本文の枠の外へはみ出していた＝2026-11-20 のホーム）。
        開いている順と違う閉じタグ・余る閉じタグがあれば落ちる。"""
        from html.parser import HTMLParser
        void = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}

        class Check(HTMLParser):
            def __init__(self):
                super().__init__()
                self.stack, self.bad = [], []

            def handle_starttag(self, tag, attrs):
                if tag not in void:
                    self.stack.append(tag)

            def handle_startendtag(self, tag, attrs):
                pass

            def handle_endtag(self, tag):
                if tag in void:
                    return
                if not self.stack or self.stack[-1] != tag:
                    self.bad.append((tag, list(self.stack[-3:])))
                    if tag in self.stack:   # 合うところまで戻して続ける（ひとつの書き間違いで、あとを全部落とさない）
                        while self.stack and self.stack.pop() != tag:
                            pass
                    return
                self.stack.pop()

        paths = ("/t", "/t/inbox", "/t/stops", "/t/watch", "/t/notices", "/t/costs", "/t/lines", "/t/dnc", "/t/settings", "/t/account",
                 f"/c/{self.a['client']}", f"/c/{self.a['client']}/b/{self.a['business']}", self._row_url(),
                 f"/c/{self.a['client']}/b/{self.a['business']}/runs/{self.a['run']}",
                 f"/c/{self.a['client']}/b/{self.a['business']}/runs/{self.a['run']}/results", f"/c/{self.a['client']}/destinations")
        for path in paths:
            with self.subTest(path=path):
                c = Check()
                c.feed(self.get("A", path).text)
                self.assertEqual(c.bad, [])
                self.assertEqual(c.stack, [])

    def test_every_page_opens(self):
        for path in ("/t", "/t/inbox", "/t/stops", "/t/notices", "/t/badges", f"/c/{self.a['client']}",
                     f"/c/{self.a['client']}/b/{self.a['business']}", f"/t/inbox?client={self.a['client']}",
                     f"/c/{self.a['client']}/b/{self.a['business']}/runs/{self.a['run']}", self._row_url(), self._import_base(), "/t/lines", "/t/dnc", "/t/costs",
                     f"/c/{self.a['client']}/b/{self.a['business']}/runs/{self.a['run']}/results",
                     f"/c/{self.a['client']}/dnc"):
            with self.subTest(path=path):
                r = self.get("A", path)
                self.assertEqual(r.status_code, 200, r.text[:300])
                self.assertNotIn(words_missing(), r.text)
                self.assertNotIn("実施回", re.sub(r"架空の[^<」]*?実施回", "", r.text))   # ✅ 2026-11-20 管理者「イ」：画面の呼び方は「お客様リスト」にそろえる（見本のデータの名前は除く）
                self.assertLessEqual(len(visible_primary_buttons(r.text)), 1, visible_primary_buttons(r.text))   # 設計書 7-4：主のボタンは1画面に1つ
        for who, path in (("A", "/t/groups"), ("A", f"/c/{self.a['client']}/destinations"), ("A", "/t/settings"), ("ADMIN", "/admin/tenants")):
            with self.subTest(path=path):
                r = self.get(who, path)
                self.assertEqual(r.status_code, 200, r.text[:300])
                self.assertLessEqual(len(visible_primary_buttons(r.text)), 1, visible_primary_buttons(r.text))


# 確かめの窓を必ず通す送り先（取り消せない操作と、電話を再開させる操作）：結果を直す・かけるかを変える・発信禁止を外す・PC のログインを外す・
# 止めたもの／回線の再開・緊急の道での再開・依頼者を無効にする／戻す・回線の使用停止・依頼者へ渡すファイルの書き出し・赤字を確かめた
CONFIRMED_ACTIONS = r"/result$|/exclude$|/revoke$|/resume$|/stops/[^/]+/emergency$|/active$|/retire$|/results\.(xlsx|csv)$|/inbox/deficit/confirm$"


# 確かめの窓の最後の一押しを赤にする送り先（取り消せない・電話を止める側。app.js の1か所が data-confirm-tone で切り替える）：
# 回線の使用停止・PC のログインを外す・発信禁止を外す（依頼者を無効にするは、有効に戻すと同じ送り先なので、その試験で見る）
DANGER_ACTIONS = r"/retire$|/pcs/[^/]+/revoke$|/dnc/revoke$"


def visible_primary_buttons(page: str) -> list[str]:
    """開かなくても見えている主のボタン（藍＝class に btn と main）の文字。たたんだ小窓（<details>）と、閉じた窓（<dialog>。確かめの窓など）の中は、開いたときだけ1つ見えるので数えない。"""
    from html.parser import HTMLParser

    class P(HTMLParser):
        def __init__(self):
            super().__init__()
            self.closed, self.found, self.cur = 0, [], None

        def handle_starttag(self, tag, attrs):
            a = dict(attrs)
            if tag in ("details", "dialog"):
                self.closed += 0 if "open" in a else 1
                self.stack.append("open" in a)
            cls = (a.get("class") or "").split()
            if "btn" in cls and "main" in cls and not self.closed and tag in ("button", "a"):
                self.cur = ""
                self.found.append(self.cur)

        def handle_data(self, data):
            if self.cur is not None:
                self.found[-1] += data.strip()

        def handle_endtag(self, tag):
            if tag in ("button", "a"):
                self.cur = None
            if tag in ("details", "dialog") and self.stack:
                self.closed -= 0 if self.stack.pop() else 1

    p = P()
    p.stack = []
    p.feed(page)
    return p.found


class EmergencyStop(_World):
    """緊急停止のボタン（範囲：契約者の全部／依頼者だけ／全体＝管理者だけ）。押した停止は、試験の終わりに必ず解く（ほかの試験に響かせない）。"""

    def press(self, who, data):
        return self.post(who, "/t/emergency", {"form_token": self.token(who), "reason": "試験の緊急停止", **data})

    def _opened(self, tenant, client=None, scope="tenant"):
        def read(tx):
            return [r["event_id"] for r in tx.all("SELECT event_id FROM salescall.open_stop_events WHERE cause = 'manual'"
                                                     " AND scope = %s AND reason = '試験の緊急停止'", (scope,))]
        a = app_access()
        return a.in_client(OPERATOR, tenant, client, read) if client else a.in_tenant(OPERATOR, tenant, read)

    def _resume_all(self, tenant, client=None, scope="tenant"):
        for eid in self._opened(tenant, client, scope):
            work = lambda tx, e=eid: stops.resume(tx, e, reason="試験の後片付け")
            (app_access().in_client(OPERATOR, tenant, client, work) if client else app_access().in_tenant(OPERATOR, tenant, work))

    def test_a_contractor_stops_the_whole_contractor_once(self):
        try:
            r = self.press("B", {"scope": "tenant"})
            self.assertIn("ok=1", r.headers["location"])
            self.assertEqual(len(self._opened(self.b["tenant"])), 1)
            r = self.press("B", {"scope": "tenant"})          # 2回押しても重ねない
            self.assertEqual(len(self._opened(self.b["tenant"])), 1)
            self.assertIn("試験の緊急停止", self.get("B", "/t/stops").text)
            self.assertNotIn("試験の緊急停止", self.get("A", "/t/stops").text)
        finally:
            self._resume_all(self.b["tenant"])

    def test_a_contractor_stops_one_client(self):
        try:
            r = self.press("A", {"scope": "client", "client_id": str(self.y["client"])})
            self.assertIn("ok=1", r.headers["location"])
            self.assertEqual(len(self._opened(self.a["tenant"], self.y["client"], "client")), 1)
            self.assertEqual(self._opened(self.a["tenant"], self.a["client"], "client"), [])
            # 依頼者のタブの印の材料（帯と同じ数え方の内訳）：止めた依頼者だけに「止まっている」の印（ほかの依頼者のタブには付けない）
            marks = re.search(r'data-tab-marks data-stop="([^"]*)"', self.get("A", "/t/badges").text).group(1).split()
            self.assertIn(str(self.y["client"]), marks)
            self.assertNotIn(str(self.a["client"]), marks)
            # ホームの依頼者ごとの表も同じ答え（counts.stopped_clients）：止めた依頼者の行だけに「止まっています」
            home = self.get("A", "/t").text
            row = lambda c: home[home.index(f'<td><a href="/c/{c}">'):].split("</tr>")[0]   # noqa: E731
            self.assertIn("止まっています", row(self.y["client"]))
            self.assertNotIn("止まっています", row(self.a["client"]))
        finally:
            self._resume_all(self.a["tenant"], self.y["client"], "client")

    def test_a_contractor_stops_one_business_only(self):
        """業務の画面から押すと、その業務だけを止められる（依頼者のほかの業務は止めない）。"""
        cid, bid = self.a["client"], self.a["business"]
        try:
            r = self.press("A", {"scope": "business", "client_id": str(cid), "business_id": str(bid)})
            self.assertIn("ok=1", r.headers["location"])
            self.assertEqual(len(self._opened(self.a["tenant"], cid, "business")), 1)
            self.assertEqual(self._opened(self.a["tenant"], cid, "client"), [])
            self.assertIn("業務「", self.get("A", f"/c/{cid}/b/{bid}").text)       # 業務の画面に、業務だけを止める選び肢が出る
        finally:
            self._resume_all(self.a["tenant"], cid, "business")

    def test_a_business_of_another_client_cannot_be_stopped(self):
        r = self.press("A", {"scope": "business", "client_id": str(self.y["client"]), "business_id": str(self.a["business"])})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(self._opened(self.a["tenant"], self.y["client"], "business"), [])

    def test_a_contractor_cannot_stop_another_contractors_client_or_everything(self):
        r = self.press("A", {"scope": "client", "client_id": str(self.b["client"])})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(self._opened(self.b["tenant"], self.b["client"], "client"), [])
        r = self.press("A", {"scope": "system"})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(self._opened(self.a["tenant"], scope="system"), [])

    def test_a_reason_is_required(self):
        r = self.post("A", "/t/emergency", {"form_token": self.token("A"), "reason": " ", "scope": "tenant"})
        self.assertIn("ok=0", r.headers["location"])
        self.assertEqual(self._opened(self.a["tenant"]), [])


class AdminSeesEverything(_World):
    """✅ 2026-11-18 管理者「管理者は全てを見れて操作できる」。切り替えた契約者は、どの画面にも帯で出す（取り違えを防ぐ）。"""

    def fresh(self):
        def identify(request):
            return self.people.get(request.headers.get("x-test-person", ""))
        return TestClient(create_app(app_access(), identify, SECRET), base_url="https://testserver")   # クッキーは https だけで送る

    def test_the_choice_cookie_is_https_only_and_not_for_scripts(self):
        http = self.fresh()
        tok = re.search(r'<meta name="form-token" content="([^"]+)"', http.get("/t", headers={"x-test-person": "ADMIN"}).text).group(1)
        r = http.post("/admin/acting", data={"tenant_id": str(self.b["tenant"]), "form_token": tok},
                      headers={"x-test-person": "ADMIN"}, follow_redirects=False)
        cookie = r.headers["set-cookie"].lower()
        for part in ("secure", "httponly", "samesite=strict"):
            self.assertIn(part, cookie)
        http.post("/admin/acting", data={"tenant_id": "", "form_token": tok}, headers={"x-test-person": "ADMIN"})

    def test_the_admin_picks_a_contractor_from_the_list_and_others_cannot_see_it(self):   # 一覧は core の registry.tenants（CL-2 4-430 の 9）
        http = self.fresh()
        page = http.get("/admin/tenants", headers={"x-test-person": "ADMIN"}).text
        b_name = app_access().in_tenant(OPERATOR, self.b["tenant"], lambda tx: tx.one(
            "SELECT name FROM salescall.tenants WHERE tenant_id = %s", (self.b["tenant"],)))["name"]
        self.assertIn(b_name, page)
        self.assertIn(f'value="{self.b["tenant"]}"', page)                     # 選ぶボタン（/admin/acting の1か所へ送る）
        self.assertEqual(http.get("/admin/tenants", headers={"x-test-person": "A"}).status_code, 404)   # 契約者には無い画面
        tok = re.search(r'<meta name="form-token" content="([^"]+)"', page).group(1)
        http.post("/admin/acting", data={"tenant_id": str(self.b["tenant"]), "form_token": tok}, headers={"x-test-person": "ADMIN"})
        self.assertIn(f"管理者として「<b>{b_name}</b>」を見ています", http.get("/t", headers={"x-test-person": "ADMIN"}).text)
        http.post("/admin/acting", data={"tenant_id": "", "form_token": tok}, headers={"x-test-person": "ADMIN"})

    def test_a_fixed_request_waits_for_the_admin_only_and_the_admin_approves_it(self):   # 修正依頼の承認（全体の範囲）
        from salescall.core import fix_requests
        key = f"web-test-{uuid4()}"
        def make(tx):
            fix_requests.request(tx, "CL-3", "web_test", key=key, summary="画面の試験の不具合（架空）")
            rid = tx.one("SELECT max(request_id) AS r FROM salescall.fix_requests WHERE summary = %s", ("画面の試験の不具合（架空）",))["r"]
            fix_requests.report_fixed(tx, rid, "試験のために直した（架空）")
            return rid
        rid = app_access().in_system(SYSTEM, make)
        self.assertNotIn("画面の試験の不具合（架空）", self.get("A", "/t/inbox").text)              # 契約者には出さない
        self.assertEqual(self.post("A", "/admin/inbox/fix/approve",
                                   {"form_token": self.token("A"), "request_id": str(rid)}).status_code, 404)
        self.assertEqual(self.post("A", f"/c/{self.a['client']}/inbox/fix/approve",                   # 依頼者の道からも決められない
                                   {"form_token": self.token("A"), "request_id": str(rid)}).status_code, 404)
        page = self.get("ADMIN", "/t/inbox").text
        self.assertIn("画面の試験の不具合（架空）", page)
        self.assertIn("全体（管理者だけ）", page)
        r = self.post("ADMIN", "/admin/inbox/fix/approve", {"form_token": self.token("ADMIN"), "request_id": str(rid)})
        self.assertIn("ok=1", r.headers["location"])
        self.assertIn("修正依頼の承認（全体）：", unquote(r.headers["location"]))
        self.assertTrue(app_access().in_system(SYSTEM, lambda tx: tx.one(
            "SELECT 1 FROM salescall.fix_approvals WHERE request_id = %s", (rid,)) is not None))
        self.assertNotIn("画面の試験の不具合（架空）", self.get("ADMIN", "/t/inbox").text)

    def test_the_admin_switches_to_another_contractor_sees_and_operates_it(self):
        http = self.fresh()
        tok = re.search(r'<meta name="form-token" content="([^"]+)"', http.get("/t", headers={"x-test-person": "ADMIN"}).text).group(1)
        r = http.post("/admin/acting", data={"tenant_id": str(self.b["tenant"]), "form_token": tok},
                      headers={"x-test-person": "ADMIN"}, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        page = http.get("/t", headers={"x-test-person": "ADMIN"}).text
        self.assertIn("依頼者Z", page)
        self.assertNotIn("依頼者X", page)
        self.assertIn("管理者として", page)
        eid = self._stop(self.b["tenant"], "管理者が解く試験の停止")
        stale = http.post(f"/t/stops/{eid}/resume", data={"reason": "試験：前の契約者の画面から", "form_token": tok},
                          headers={"x-test-person": "ADMIN"}, follow_redirects=False)
        self.assertIn("ok=0", stale.headers["location"])                         # 切り替える前の契約者の画面の札では、切り替えた先に効かせない
        self.assertIn(quote("古くなっています"), stale.headers["location"])
        self.assertTrue(self._open(self.b["tenant"], eid))
        b_tok = re.search(r'<meta name="form-token" content="([^"]+)"', page).group(1)   # 切り替えたあとに開いた画面の札
        r = http.post(f"/t/stops/{eid}/resume", data={"reason": "試験：管理者が確かめた", "form_token": b_tok},
                      headers={"x-test-person": "ADMIN"}, follow_redirects=False)
        self.assertIn("ok=1", r.headers["location"])
        self.assertFalse(self._open(self.b["tenant"], eid))
        # 戻る
        http.post("/admin/acting", data={"tenant_id": "", "form_token": tok}, headers={"x-test-person": "ADMIN"})
        page = http.get("/t", headers={"x-test-person": "ADMIN"}).text
        self.assertIn("依頼者X", page)
        self.assertNotIn("管理者として", page)

    def test_the_admin_can_stop_everything_and_it_shows_for_every_contractor(self):
        from tests.support import ADMIN
        tok = self.token("ADMIN")
        r = self.post("ADMIN", "/t/emergency", {"form_token": tok, "reason": "試験の全体の緊急停止", "scope": "system"})
        def opened(tx):
            return [x["event_id"] for x in tx.all("SELECT event_id FROM salescall.open_stop_events WHERE scope = 'system'"
                                                   " AND reason = '試験の全体の緊急停止'")]
        try:
            self.assertIn("ok=1", r.headers["location"])
            self.assertEqual(len(app_access().in_tenant(OPERATOR, self.b["tenant"], opened)), 1)
            self.assertIn("試験の全体の緊急停止", self.get("B", "/t/stops").text)   # 全体の停止は、どの契約者にも見える
        finally:
            for eid in app_access().in_system(ADMIN, opened):
                app_access().in_system(ADMIN, lambda tx, e=eid: stops.resume(tx, e, reason="試験の後片付け"))

    def test_a_contractor_cannot_switch_and_a_forged_choice_is_ignored(self):
        http = self.fresh()
        tok = re.search(r'<meta name="form-token" content="([^"]+)"', http.get("/t", headers={"x-test-person": "A"}).text).group(1)
        r = http.post("/admin/acting", data={"tenant_id": str(self.b["tenant"]), "form_token": tok},
                      headers={"x-test-person": "A"}, follow_redirects=False)
        self.assertEqual(r.status_code, 404)
        from salescall.web import actions
        forged = actions.sign_choice(SECRET, "試験の管理者", self.b["tenant"])   # 管理者の札を契約者が使っても効かない
        http.cookies.set("acting_tenant", forged)
        page = http.get("/t", headers={"x-test-person": "A"}).text
        self.assertNotIn("依頼者Z", page)
        http.cookies.set("acting_tenant", f"{self.b['tenant']}.00")
        page = http.get("/t", headers={"x-test-person": "ADMIN"}).text   # 署名の合わない札は捨てる
        self.assertNotIn("依頼者Z", page)


def words_missing() -> str:
    from salescall.output import words
    return words.NO_WORD


if __name__ == "__main__":
    unittest.main()
