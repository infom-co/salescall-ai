"""行ごとの「かけない」の試験（2026-10-18 ユーザー決定。DESIGN.md 3-4-2。core/exclusions.py・表の作り 0071）。

- 入口は2つ：一覧の取込の「かけない」の列・ダッシュボードのレ点や一括（管理者か契約者だけ）。付け外しは exclusions.mark の1か所。
- 行がいま「かけない」かはデータベースの1か所（task_excluded）。門も候補を出す処理も同じものを読む。戻せば、ふつうにかける。
⚠️ 中身は架空のもの。
"""

from __future__ import annotations

import unittest

from salescall.core import attempts, exclusions, gate, importer, next_action, scheduler
from salescall.core.access import Rejected
from salescall.core.import_files import Cell, read
from tests.support import OPERATOR, SYSTEM, fictional_jp_fixed, owner_access
from tests.test_gate import Base
from tests.test_import import ai, csv_file
from tests.test_import_files import xlsx

MAP = {"phone": "電話番号", "client_ref": "管理番号", "name": "会社名", "exclude": "かけない"}


class ReadMark(unittest.TestCase):
    def test_marks_are_read_without_guessing(self):
        for text in ("かけない", "○", "レ", "1", "TRUE", "✓", "x", "×", "除外", "はい", "ｘ"):
            with self.subTest(text=text):
                self.assertIs(exclusions.read_mark(Cell(text)), True)
        for text in (None, "", "  ", "0", "FALSE", "かける", "いいえ"):
            with self.subTest(text=text):
                self.assertIs(exclusions.read_mark(Cell(text)), False)
        for text in ("たぶん", "2", "後で"):
            with self.subTest(text=text):
                self.assertIsNone(exclusions.read_mark(Cell(text)))           # 読めない値は推測しない
        self.assertIs(exclusions.read_mark(Cell(None, problem="TRUE・FALSE の値です", flag=True)), True)   # Excel のチェック
        self.assertIsNone(exclusions.read_mark(Cell(None, problem="表計算ソフトのエラーの値です（#N/A など）")))


class Marks(Base):
    def test_dashboard_marks_stop_the_row_until_it_is_put_back(self):
        s = self.ready()
        waiting = self.claim(s)
        self.assertIsInstance(waiting, gate.Claim)
        with self.assertRaises(Rejected):                                    # ダッシュボードの付け外しは人だけ
            self.tx(s, lambda tx: exclusions.mark(tx, [s["task"]], excluded=True, reason="試験"), SYSTEM)
        self.assertEqual(self.tx(s, lambda tx: exclusions.mark(tx, [s["task"]], excluded=True, reason="依頼者の都合"), OPERATOR),
                         [s["task"]])
        state = self.tx(s, lambda tx: tx.one("SELECT state::text AS s FROM salescall.attempts WHERE attempt_id = %s",
                                             (waiting.attempt_id,)))["s"]
        self.assertEqual(state, "released")                                   # まだ発信していない予約は取り消す
        self.assertEqual(self.reason(s), "excluded")
        self.assertNotIn(s["task"], [t["task_id"] for t in self.tx(s, lambda tx: scheduler.due_tasks(tx, 50))])
        self.assertEqual(self.tx(s, lambda tx: exclusions.mark(tx, [s["task"]], excluded=True, reason="2回目"), OPERATOR), [])
        self.tx(s, lambda tx: exclusions.mark(tx, [s["task"]], excluded=False, reason="依頼者から再開の連絡"), OPERATOR)
        self.assertIsInstance(self.claim(s), gate.Claim)                      # 戻したら、ふつうにかける
        rows = self.tx(s, lambda tx: tx.all("SELECT excluded FROM salescall.task_exclusions WHERE task_id = %s ORDER BY seq",
                                            (s["task"],)))
        self.assertEqual([r["excluded"] for r in rows], [True, False])        # 付けた・戻した記録が残る
        with self.assertRaises(Rejected):
            owner_access().in_client(OPERATOR, s["tenant"], s["client"], lambda tx: tx.execute(
                "UPDATE salescall.task_exclusions SET excluded = true WHERE task_id = %s", (s["task"],)))


    def test_apologies_resumes_and_promised_calls_are_made_even_on_a_row_set_to_do_not_call(self):
        """途中で「かけない」に変えても、お客様と約束したかけ直し（お詫び・途中で切れたあとのかけ直しも同じ決まり）はかける
        （2026-10-18 ユーザー：「かけないことを除外ということなので、その3つは途中でかけないに切り替えてもかけるという意味ですよ」）。
        まだ発信していない、その予約も取り消さない。"""
        s = self.ready()
        self.tx(s, lambda tx: next_action.schedule_call(tx, s["task"], tx.one("SELECT now() AS t")["t"],
                                                        contact_id=s["contact"], cause="試験", promised=True))
        promised = self.claim(s)
        self.assertIsInstance(promised, gate.Claim)
        self.tx(s, lambda tx: exclusions.mark(tx, [s["task"]], excluded=True, reason="依頼者の都合"), OPERATOR)
        state = self.tx(s, lambda tx: tx.one("SELECT state::text AS s FROM salescall.attempts WHERE attempt_id = %s",
                                             (promised.attempt_id,)))["s"]
        self.assertEqual(state, "claimed")                                    # 約束したかけ直しの予約は残す
        self.tx(s, lambda tx: attempts.release(tx, [promised.attempt_id]))
        self.assertIn(s["task"], [t["task_id"] for t in self.tx(s, lambda tx: scheduler.due_tasks(tx, 50))])
        self.assertIsInstance(self.claim(s), gate.Claim)                      # 印があっても、約束したかけ直しはかける
        with self.subTest("決まりは1か所"):
            got = self.tx(s, lambda tx: [tx.one("SELECT salescall.is_new_call(%s, %s) AS n", a)["n"] for a in (
                ("normal", False), ("ask_number_again", False), ("apology", False), ("resume", False), ("normal", True))])
            self.assertEqual(got, [True, True, False, False, False])


class ImportColumn(Base):
    def test_the_list_column_marks_rows_and_unreadable_values_stop_the_row(self):
        s = self.ready()
        a, b, c = fictional_jp_fixed(), fictional_jp_fixed(), fictional_jp_fixed()
        data = csv_file([("R-1", "架空商事", a, "かけない"), ("R-2", "架空工業", b, ""), ("R-3", "架空物産", c, "たぶん")],
                        header=("管理番号", "会社名", "電話番号", "かけない"))
        sheet = read("一覧.csv", data)
        plan = self.tx(s, lambda tx: importer.preview(tx, s["run"], sheet, MAP, suggest_readings=ai))
        self.assertEqual([(r.outcome, r.reason_code, r.excluded) for r in plan.rows],
                         [("registered", None, True), ("registered", None, False), ("stopped", "exclude_unreadable", False)])
        got = self.tx(s, lambda tx: importer.register(tx, s["run"], sheet, MAP, expected_digest=plan.digest,
                                                      ai_readings=plan.ai_readings))
        first, second = got["rows"][0]["task_id"], got["rows"][1]["task_id"]
        self.assertEqual(self.tx(s, lambda tx: [exclusions.excluded(tx, first), exclusions.excluded(tx, second)]), [True, False])
        src = self.tx(s, lambda tx: tx.one("SELECT source, import_id FROM salescall.task_exclusions WHERE task_id = %s", (first,)))
        self.assertEqual((src["source"], src["import_id"]), ("import", got["import_id"]))

    def test_excel_check_boxes_are_read(self):
        s = self.ready()
        data = xlsx([("電話番号", "かけない"), (fictional_jp_fixed(), True), (fictional_jp_fixed(), False)])
        sheet = read("一覧.xlsx", data)
        plan = self.tx(s, lambda tx: importer.preview(tx, s["run"], sheet, {"phone": "電話番号", "exclude": "かけない"},
                                                      suggest_readings=ai))
        self.assertEqual([(r.outcome, r.excluded) for r in plan.rows], [("registered", True), ("registered", False)])


if __name__ == "__main__":
    unittest.main()
