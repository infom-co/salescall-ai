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


if __name__ == "__main__":
    unittest.main()
