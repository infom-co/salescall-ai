"""誤判定率と抜き取りの画面の試験（持ち主 CL-3。/c/{依頼者}/b/{業務}/samples。計画書 16-3）。割合は core の measure.rates、
抜き取りは measure.draw（CL-2）。本物の電話の流れ（CL-2 の試験の土台）で抜き取りを起こしてから見る。架空のデータだけ。"""

from __future__ import annotations

import re
from datetime import timedelta

from fastapi.testclient import TestClient

from salescall.core import measure
from salescall.web.app import create_app
from salescall.web.context import Person
from tests.support import OPERATOR, app_access
from tests.test_measure import TAKEN, Base

SECRET = b"test-only-secret"


class SamplesPage(Base):
    def http(self, s):
        return TestClient(create_app(app_access(), lambda r: Person("試験の契約者", False, s["tenant"], "架空の契約者"), SECRET))

    def test_the_page_shows_the_drawn_calls_and_the_rate_from_core(self):
        s = self.ready()
        a = self.call(s, items=TAKEN)
        self.settle(s, a)
        http = self.http(s)
        path = f"/c/{s['client']}/b/{s['business']}/samples"
        page = http.get(path).text
        self.assertIn("まだ抜き取っていません", page)
        self.assertIn("まだ正解が付いていません", page)
        # 週を進めて抜き取る（core の measure.draw）
        now = self.jst_now(s)
        tuesday = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=8 - now.weekday())
        drawn = self.tx(s, lambda tx: measure.draw(tx, now=tuesday))
        self.assertEqual(len(drawn), 1)
        page = http.get(path).text
        self.assertIn("話せた電話 1本から 1本", page)
        self.assertIn("正解はまだ", page)
        self.assertIn(f"/calls/{a}", page)                                       # 抜き取った電話のやり取りへ飛べる
        self.assertNotIn('name="truth_state"', page)                             # 録音を聞く仕組みができるまで、正解の欄は出さない
        # 知らせ「抜き取りました」から、この画面へ飛ぶ
        notices = http.get("/t/notices").text
        self.assertIn(path, notices)
        # 正解が付けば、割合は core の答えのまま出る
        self.tx(s, lambda tx: measure.record_check(tx, s["task"], 1, truth_state="completed", wrong_items=[]), OPERATOR)
        page = http.get(path).text
        self.assertIn("正解を付けた", page)
        got = self.rates(s, "sample")
        self.assertIn(f"（{got['checked']}本のうち誤り {got['wrong']}本）", page)
        self.assertIn("{:.1f}%".format(got["upper"] * 100), page)                # 上の限度も core の値のまま
        self.assertEqual(re.findall(r"\b[a-z]+_[a-z_]+\b", page[page.index("<main"):]), [])   # 英字の内部の名前を出さない

    def test_another_contractor_cannot_open_it(self):
        s, other = self.ready(), self.ready(tenant=None)
        http = self.http(other)
        self.assertEqual(http.get(f"/c/{s['client']}/b/{s['business']}/samples").status_code, 404)
