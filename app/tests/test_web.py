"""ダッシュボードの試験（持ち主 CL-3。docs/DASHBOARD.md 8章）。画面を通して叩く。架空のデータだけを使う。

ログインの仕組み（利用者の表）は CL-2 に頼んでいるので、ここでは架空の人を返す関数を渡す（本番の画面のサーバーは、ログインを確かめる
関数を渡されなければ動かない）。
"""

from __future__ import annotations

import re
from urllib.parse import quote
import unittest
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from salescall.core import stops
from salescall.web.app import create_app
from salescall.web.context import Person
from tests.support import OPERATOR, SYSTEM, World, app_access, ensure_rules, fictional_caller, fictional_jp_fixed

SECRET = b"test-only-secret"
from tests.test_output_words import DEV_NOTE  # noqa: E402  画面に出してはいけない開発の覚え書きのしるし（1か所）


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

    def test_a_version_page_shows_the_settings_of_its_own_business_only(self):
        ensure_rules(World(), self.a)
        ok = self.get("A", f"/c/{self.a['client']}/b/{self.a['business']}/versions/{self.a['version']}")
        self.assertEqual(ok.status_code, 200)
        self.assertIn("電話の台本", ok.text)
        self.assertNotIn(words_missing(), ok.text)
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
        self.assertIn("これから", r.text)   # まだ次の予定が無い行

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
        self.assertIn("いまの担当の方と同じ方ですか", step.text)
        forged = self.post("A", url + "/fix", {**form, "go": "1"})             # 選ばずに「直す」を送っても書かない
        self.assertIn("ok=0", forged.headers["location"])
        self.assertNotEqual(self._tasks_now("target_name"), "架空の別の方")
        step = self.post("A", url + "/fix", {**form, "same": "1"})
        self.assertIn("この内容で直します", step.text)
        self.assertIn("同じ方", step.text)
        r = self.post("A", url + "/fix", {**form, "same": "1", "go": "1"})
        self.assertIn("ok=1", r.headers["location"])
        self.assertEqual(self._tasks_now("target_name"), "架空の別の方")

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
        self._crawl("A", ["/t", f"/c/{self.a['client']}/b/{self.a['business']}"])

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
        self.assertEqual(card.count("済み"), 1)                                  # 回線（共有）だけが済み（試験の契約者には回線がある）
        running = self.get("A", f"/c/{self.a['client']}/b/{self.a['business']}").text
        self.assertNotIn("<h2>はじめかた</h2>", running)                         # 手順がそろった業務では出さない

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
        backs = [f.name for f in tpl.glob("*.html") if f.name not in ("import_map.html", "import_preview.html") and re.search(r">\s*← ", f.read_text(encoding="utf-8"))]
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

    def test_the_home_shows_what_to_look_at_and_how_things_usually_are(self):
        """ホームは2段：いま見ること（止まっている・承認待ち・気をつけること）と、ふだんの様子（今日の電話・回線と PC・今月の費用と報酬。
        計画書 5-2）。どの数も core の答えを数えるだけ。"""
        home = self.get("A", "/t").text
        for word in ("いま見ること", "ふだんの様子", "今日かけた電話", "目的のお客様と話せた", "休んでいる回線", "使っている回線", "今月の費用", "今月の報酬"):
            with self.subTest(word=word):
                self.assertIn(word, home)
        self.assertIn('href="/t/lines"', home)
        self.assertIn("電話をつなぐ PC がまだありません", home)                  # 回線があって PC が0台＝電話はかからない（全体の1か所で知らせる）
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
