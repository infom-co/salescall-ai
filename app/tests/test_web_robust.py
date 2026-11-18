"""画面のどの送り先も、でたらめな値で止まらない（500 にならない）ことの試験（持ち主 CL-3。docs/DASHBOARD.md 8-3）。

1つずつの入力の欄を見て直すのではなく、画面のすべての POST の送り先を、ひな形に出てくるすべての欄の名前に、形の違う値（空・文字・
NaN・無限大・負・長すぎる数・形の違う ID・全角の数）を入れて叩く。断るのは画面の言葉（303 で戻る・404・403）で、プログラムの誤りで
止まらないこと。送り先や欄を足しても、この試験が自動で叩く（足し忘れが無い）。架空のデータだけ。
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path
from uuid import uuid4

from tests.test_web import _World

TEMPLATES = Path(__file__).resolve().parents[1] / "src" / "salescall" / "web" / "templates"
BAD = ("", "x", "NaN", "Infinity", "-1", "9" * 40, "zz-not-an-id", "１２", "<b>x</b>")


def field_names() -> list[str]:
    names = set()
    for f in TEMPLATES.glob("*.html"):
        names |= set(re.findall(r'name="([a-z_][a-z0-9_]*)"', f.read_text(encoding="utf-8")))
    return sorted(names - {"form_token"})


class NothingBreaks(_World):
    def _path(self, route) -> str:
        real = {"client_id": self.a["client"], "business_id": self.a["business"], "run_id": self.a["run"], "task_id": self.a["task"],
                "kind": "decision", "action": "approve", "event_id": 999999999, "key": "x", "vid": uuid4(), "line_id": uuid4(),
                "pc_id": uuid4()}
        return re.sub(r"\{([a-z_]+)(:[a-z]+)?\}", lambda m: str(real.get(m.group(1), uuid4())), route.path)

    def test_every_post_answers_in_words_for_any_value(self):
        posts = [r for r in self.http.app.routes if "POST" in getattr(r, "methods", set())]
        self.assertGreater(len(posts), 20)
        names = field_names()
        token = self.token("ADMIN")
        for route in posts:
            path = self._path(route)
            for bad in BAD:
                with self.subTest(path=route.path, value=bad):
                    if route.path.startswith("/pc-login/"):   # PC が送る JSON の入口
                        r = self.http.post(path, content=bad.encode(), headers={"content-type": "application/json"})
                        self.assertIn(r.status_code, (200, 400, 429), r.text[:200])
                        continue
                    r = self.http.post(path, data={"form_token": token, **{n: bad for n in names}},
                                       headers={"x-test-person": "ADMIN"}, follow_redirects=False)
                    self.assertIn(r.status_code, (200, 303, 403, 404), r.text[:300])   # 200＝次の段の画面・確かめのファイル

    def test_every_get_answers_in_words_for_a_wrong_id_in_the_address(self):
        """住所の ID の欄すべてに、形の違う値・無い ID を入れて GET を叩く（どれも 500 にならず、日本語の返事）。住所を足しても自動で叩く。"""
        gets = [r for r in self.http.app.routes if "GET" in getattr(r, "methods", set()) and "{" in getattr(r, "path", "")]
        self.assertGreater(len(gets), 10)
        for route in gets:
            for bad in ("zz-not-an-id", str(uuid4()), "0"):
                path = re.sub(r"\{([a-z_]+)(:[a-z]+)?\}", bad, route.path)
                with self.subTest(path=route.path, value=bad):
                    r = self.get("ADMIN", path)
                    self.assertIn(r.status_code, (200, 303, 403, 404), r.text[:200])
                    self.assertNotIn('"detail"', r.text)                          # 枠組みの英語の返事を出さない

    def test_a_wrong_address_answers_in_japanese(self):
        for path in ("/c/not-an-id", f"/c/{self.a['client']}/b/not-an-id", "/t/stops/not-a-number", "/t/inbox?ok=x", "/no/such/page"):
            with self.subTest(path=path):
                r = self.get("A", path)
                self.assertEqual(r.status_code, 404, r.text[:200])
                self.assertIn("見つかりません", r.text)
        r = self.http.put("/t", headers={"x-test-person": "A"})                 # 開き方が違う
        self.assertIn("この開き方はできません", r.text)

    def test_yen_is_written_without_an_exponent(self):
        from decimal import Decimal
        from salescall.web.app import _yen
        self.assertEqual(_yen(Decimal("12345.6")), "12,346円")
        self.assertEqual(_yen(Decimal("35000"), unit_price=True), "35,000円")
        self.assertEqual(_yen(Decimal("0.000012"), unit_price=True), "0.000012円")
        self.assertEqual(_yen(Decimal("12.50"), unit_price=True), "12.5円")
        self.assertEqual(_yen(Decimal("NaN")), "—")

    def test_the_inbox_order_keeps_the_time_difference(self):
        from datetime import datetime, timedelta, timezone
        from salescall.web import inbox
        japan = timezone(timedelta(hours=9))
        early = inbox.Item("x", {}, "早い", "", at=datetime(2026, 11, 18, 10, 0, tzinfo=japan))       # 世界の時刻で 1時
        late = inbox.Item("x", {}, "遅い", "", at=datetime(2026, 11, 18, 2, 0, tzinfo=timezone.utc))
        self.assertEqual([i.title for i in inbox.ordered([late, early])], ["早い", "遅い"])

    def test_every_table_scrolls_inside_its_own_frame(self):
        """表は必ず横に送る枠の中（スマホの幅で画面ごと横にはみ出さない）。ひな形を足しても、この試験が見る。"""
        for f in TEMPLATES.glob("*.html"):
            text = f.read_text(encoding="utf-8")
            for m in re.finditer(r'<table class="list', text):
                with self.subTest(template=f.name, at=m.start()):
                    self.assertRegex(text[max(0, m.start() - 80):m.start()], r'<div class="table-scroll"[^>]*>\s*$')


if __name__ == "__main__":
    unittest.main()
