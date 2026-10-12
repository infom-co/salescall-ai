"""接続せずに確かめられる部分の試験（python -m unittest test_offline）。

- .env の読み方（書き方の違いを吸収できるか）
- キーの値が、画面・結果のファイルに出ないか（DESIGN.md I-10。わざと漏らそうとして、伏せられるか）
- 試験用の流れの制御（g06_flow.MiniFlow）が、決まりどおりに進む・止まるか（I-18・I-20 の入口）
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import common
from g06_flow import ASK_BACK, MiniFlow


def fc(name, **args):
    return SimpleNamespace(name=name, args=args, id="x")


class EnvAndRedactionTest(unittest.TestCase):
    def setUp(self):
        common._LOADED_SECRETS.clear()
        self.tmp = Path(tempfile.mkdtemp())
        self.env = self.tmp / ".env"
        self.secret = "AIza" + "S" * 35
        self.env.write_text(
            "# comment\n"
            f"GEMINI_API_KEY_1={self.secret}\n"
            "export GEMINI_API_KEY_2='AIzaQUOTED_________________________x'\n"
            "GOOGLE_API_KEY_3=\"AIzaDOUBLE_________________________y\" # memo\n"
            "OTHER=value\n",
            encoding="utf-8",
        )

    def test_reads_all_forms(self):
        keys = common.load_keys(self.env)
        self.assertEqual([k.name for k in keys], ["GEMINI_API_KEY_1", "GEMINI_API_KEY_2", "GOOGLE_API_KEY_3"])
        self.assertTrue(keys[1].value.startswith("AIzaQUOTED"))
        self.assertTrue(keys[2].value.endswith("y"))

    def test_label_and_repr_hide_value(self):
        k = common.load_keys(self.env)[0]
        self.assertNotIn(self.secret, k.label)
        self.assertNotIn(self.secret, repr(k))

    def test_redact_in_errors_and_results(self):
        common.load_keys(self.env)
        err = RuntimeError(f"failed with key={self.secret}")
        info = common.describe_error(err)
        self.assertNotIn(self.secret, json.dumps(info))
        common.RESULTS_DIR = self.tmp / "results"
        path = common.save_result("TEST", {"leak": self.secret, "nested": {"also": f"x{self.secret}x"}})
        self.assertNotIn(self.secret, path.read_text(encoding="utf-8"))

    def test_falls_back_to_environ_when_no_file(self):
        import os
        os.environ["GEMINI_API_KEY_TEST"] = "AIza" + "E" * 35
        try:
            keys = common.load_keys(self.tmp / "no_such.env", ["GEMINI_API_KEY_TEST"])
            self.assertEqual(keys[0].name, "GEMINI_API_KEY_TEST")
            self.assertEqual(common.redact(f"x{keys[0].value}x"), "x***x")
        finally:
            del os.environ["GEMINI_API_KEY_TEST"]

    def test_explicit_names_missing(self):
        with self.assertRaises(SystemExit):
            common.load_keys(self.env, ["NOT_THERE"])


class MiniFlowTest(unittest.TestCase):
    def test_yes_no_accepted_and_advances(self):
        f = MiniFlow()
        r = f.handle(fc("record_answer", question_id="Q1", condition_id="yes"))
        self.assertEqual(r["result"], "accepted")
        self.assertEqual(f.current, "Q2")

    def test_wrong_question_rejected(self):
        f = MiniFlow()
        r = f.handle(fc("record_answer", question_id="Q2", condition_id="answered"))
        self.assertEqual(r["result"], "rejected")
        self.assertFalse(f.checks["question_id_matched"])
        self.assertEqual(f.current, "Q1")

    def test_confirm_type_held_until_confirm(self):
        f = MiniFlow()
        f.handle(fc("record_answer", question_id="Q1", condition_id="yes"))
        r = f.handle(fc("record_answer", question_id="Q2", condition_id="answered", value="15日14時"))
        self.assertEqual(r["result"], "confirm_required")
        self.assertFalse(f.done)  # 確かめる前に終わりへ進まない（I-18）
        f.on_callee()  # 相手が返事をした
        r = f.handle(fc("confirm_field", question_id="Q2", result="affirmed"))
        self.assertEqual(r["result"], "confirmed")
        self.assertTrue(f.done)
        self.assertEqual(f.values["Q2"]["state"], "confirmed")

    def test_ask_back_only_once(self):
        f = MiniFlow()
        f.handle(fc("record_answer", question_id="Q1", condition_id="yes"))
        r1 = f.handle(fc("report_unclear", question_id="Q2"))
        self.assertEqual(r1["result"], "ask_back")
        self.assertIn(ASK_BACK, r1["message"])
        r2 = f.handle(fc("report_unclear", question_id="Q2"))
        self.assertEqual(r2["result"], "give_up")  # 2回目は聞き返させない（I-20）
        self.assertEqual(f.values["Q2"]["state"], "unresolved")

    def test_yes_no_after_ask_back_needs_readback(self):
        f = MiniFlow()
        f.handle(fc("report_unclear", question_id="Q1"))
        r = f.handle(fc("record_answer", question_id="Q1", condition_id="yes"))
        self.assertEqual(r["result"], "confirm_required")  # 聞き返したあとは、確かめない型でも復唱する（6-11）

    def test_correction_then_one_more_readback(self):
        f = MiniFlow()
        f.handle(fc("record_answer", question_id="Q1", condition_id="yes"))
        f.handle(fc("record_answer", question_id="Q2", condition_id="answered", value="15日14時"))
        f.on_callee()
        r = f.handle(fc("confirm_field", question_id="Q2", result="corrected", correction="16日14時"))
        self.assertEqual(r["result"], "confirm_required")
        self.assertEqual(f.values["Q2"]["value"], "16日14時")
        self.assertIn("16日14時", r["message"])  # 言い直すのは、記録した値そのもの

    def test_confirm_before_callee_reply_rejected(self):
        # 2026-10-12 の G-6：AIが相手の返事を待たずに「肯定された」と報告した ⇒ 差し戻す
        f = MiniFlow()
        f.handle(fc("record_answer", question_id="Q1", condition_id="yes"))
        f.handle(fc("record_answer", question_id="Q2", condition_id="answered", value="15日14時"))
        r = f.handle(fc("confirm_field", question_id="Q2", result="affirmed"))
        self.assertEqual(r["result"], "rejected")
        self.assertNotEqual(f.values["Q2"]["state"], "confirmed")
        f.on_callee()
        r = f.handle(fc("confirm_field", question_id="Q2", result="affirmed"))
        self.assertEqual(r["result"], "confirmed")

    def test_confirm_without_request_rejected(self):
        f = MiniFlow()
        r = f.handle(fc("confirm_field", question_id="Q1", result="affirmed"))
        self.assertEqual(r["result"], "rejected")


if __name__ == "__main__":
    unittest.main()
