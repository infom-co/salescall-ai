"""「お名前だけではどの方か分からなかった」行の出力の試験（持ち主 CL-3）。✅ 2026-11-18 管理者「(ア)でOK」：結果のファイルに
「フルネーム」「部署」の欄を足し、CSV では空欄に「（ご記入ください）」、見本の画面（と Excel）では赤い背景。どの欄かは core の
business_settings.who_unclear_blanks の1か所。状態は CL-2 の試験の土台で、本物の流れから作る（架空のデータだけ）。"""

from __future__ import annotations

import csv
import io
import unittest

from fastapi.testclient import TestClient

from salescall.output import result_rows
from salescall.web.app import create_app
from salescall.web.context import Person
from tests.support import app_access
from tests.test_settle import DialFixAfterCalls

SECRET = b"test-only-secret"


class WhoUnclearOut(DialFixAfterCalls):
    def test_the_blanks_to_fill_are_marked_in_the_file_and_on_the_screen(self):
        s = self.ready()
        self.who_unclear_end(s)
        got = self.tx(s, lambda tx: result_rows.rows(tx, s["run"]))
        row = next(r for r in got if r.to_fill)
        self.assertEqual(set(row.to_fill), set(result_rows.TO_FILL_COLUMNS.values()))   # フルネームも部署も空の行
        http = TestClient(create_app(app_access(), lambda r: Person("試験の契約者", False, s["tenant"], "架空の契約者"), SECRET))
        base = f'/c/{s["client"]}/b/{s["business"]}/runs/{s["run"]}'
        text = http.get(base + "/results.csv").content.decode("utf-8-sig")
        lines = list(csv.reader(io.StringIO(text)))
        cells = dict(zip(lines[0], lines[1]))
        self.assertEqual(cells["お呼びする方のフルネーム"], result_rows.PLEASE_FILL)
        self.assertEqual(cells["お呼びする方の部署"], result_rows.PLEASE_FILL)
        page = http.get(base + "/results").text
        self.assertEqual(page.count('class="to-fill"'), 2)                               # その行の、その2つの欄だけ赤い


def load_tests(loader, tests, pattern):
    """この試験だけを読む（受け継いだ CL-2 の組の試験を、ここで流し直さない）。"""
    return unittest.TestSuite(loader.loadTestsFromNames(
        [f"{__name__}.WhoUnclearOut.{n}" for n in WhoUnclearOut.__dict__ if n.startswith("test_")]))


if __name__ == "__main__":
    unittest.main()
