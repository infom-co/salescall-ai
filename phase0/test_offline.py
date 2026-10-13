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


class FlowTest(unittest.TestCase):
    """流れの制御（flowctl.Flow）。DESIGN.md 4-4・6-2・6-11 の決まりどおりに進む・止まるか。"""

    def flow(self, *, q1_required=True):
        from flowctl import Flow, Question
        return Flow([Question("Q1", "ご予約はされていますか。", "yes_no", required=q1_required, conditions=("yes", "no")),
                     Question("Q2", "ご希望の日時をお聞かせください。", "datetime", conditions=("answered",))])

    def call(self, f, name, **args):
        return f.handle(fc(name, **args))

    def test_yes_no_accepted_and_advances(self):
        f = self.flow()
        r = self.call(f, "record_answer", question_id="Q1", condition_id="yes")
        self.assertEqual((r["result"], f.current, f.items["Q1"].state), ("next", "Q2", "accepted"))

    def test_wrong_question_and_undeclared_condition_rejected(self):
        f = self.flow()
        self.assertEqual(self.call(f, "record_answer", question_id="Q2", condition_id="answered")["result"], "rejected")
        self.assertEqual(self.call(f, "record_answer", question_id="Q1", condition_id="maybe")["result"], "rejected")
        self.assertEqual((f.current, f.items["Q1"].state), ("Q1", "empty"))

    def test_confirm_type_held_until_callee_affirms(self):
        f = self.flow()
        self.call(f, "record_answer", question_id="Q1", condition_id="yes")
        r = self.call(f, "record_answer", question_id="Q2", condition_id="answered", value="15日の午後2時")
        self.assertEqual((r["result"], r["value"]), ("confirm_required", "15日の午後2時"))  # 言い直すのは記録した値そのもの
        self.assertEqual(f.phase, "asking")  # 確かめる前に終わりへ進まない（I-18）
        # 相手の返事が届く前の報告は差し戻す（I-24）
        self.assertEqual(self.call(f, "confirm_field", question_id="Q2", result="affirmed")["result"], "rejected")
        f.on_callee()
        r = self.call(f, "confirm_field", question_id="Q2", result="affirmed")
        self.assertEqual((r["result"], f.items["Q2"].state, f.phase), ("end", "confirmed", "closing"))

    def test_ask_back_once_then_unresolved_then_one_recheck_at_end(self):
        f = self.flow()
        self.call(f, "record_answer", question_id="Q1", condition_id="yes")
        self.assertEqual(self.call(f, "report_unclear", question_id="Q2")["result"], "ask_back")
        r = self.call(f, "report_unclear", question_id="Q2")  # 2回目は聞き返させない（I-20）
        self.assertEqual((r["result"], r["question_id"]), ("recheck", "Q2"))  # 締めの前に念のための確認を1回
        self.assertEqual((f.items["Q2"].state, f.items["Q2"].rechecks), ("unresolved", 1))
        r = self.call(f, "report_unclear", question_id="Q2")  # 念のための確認でも聞けない
        self.assertEqual((r["result"], f.items["Q2"].state), ("end", "unresolved"))
        self.assertEqual(f.export()["Q2"], "-")

    def test_recheck_answer_is_read_back(self):
        f = self.flow()
        self.call(f, "record_answer", question_id="Q1", condition_id="yes")
        self.call(f, "report_unclear", question_id="Q2")
        self.call(f, "report_unclear", question_id="Q2")
        r = self.call(f, "record_answer", question_id="Q2", condition_id="answered", value="15日の午後2時")
        self.assertEqual(r["result"], "confirm_required")  # 念のための確認で聞けたら復唱する（6-11）
        f.on_callee()
        self.assertEqual(self.call(f, "confirm_field", question_id="Q2", result="affirmed")["result"], "end")
        self.assertEqual(f.export()["Q2"], "15日の午後2時")

    def test_yes_no_after_ask_back_needs_readback(self):
        f = self.flow()
        self.call(f, "report_unclear", question_id="Q1")
        r = self.call(f, "record_answer", question_id="Q1", condition_id="yes")
        self.assertEqual(r["result"], "confirm_required")  # 聞き返したあとは、確かめない型でも復唱する（6-11）

    def test_correction_read_back_once_more_then_unresolved(self):
        f = self.flow()
        self.call(f, "record_answer", question_id="Q1", condition_id="yes")
        self.call(f, "record_answer", question_id="Q2", condition_id="answered", value="15日の午後2時")
        f.on_callee()
        r = self.call(f, "confirm_field", question_id="Q2", result="corrected", correction="16日の午後2時")
        self.assertEqual((r["result"], r["value"]), ("confirm_required", "16日の午後2時"))
        f.on_callee()
        r = self.call(f, "confirm_field", question_id="Q2", result="corrected", correction="17日の午後2時")
        self.assertEqual(f.items["Q2"].state, "unresolved")  # 訂正のあとの復唱は1回まで
        self.assertEqual(r["result"], "recheck")  # 「-」の必須項目なので、締めの前に1回だけ念のため

    def test_refused_never_asked_again_and_exported_as_cross(self):
        f = self.flow()
        self.call(f, "record_answer", question_id="Q1", condition_id="refused")
        r = self.call(f, "record_answer", question_id="Q2", condition_id="refused")
        self.assertEqual(r["result"], "end")  # 念のための確認にも入れない
        self.assertEqual(f.export(), {"Q1": "✕", "Q2": "✕"})

    def test_unknown_answer_is_dash_without_recheck(self):
        f = self.flow()
        self.call(f, "record_answer", question_id="Q1", condition_id="yes")
        r = self.call(f, "record_answer", question_id="Q2", condition_id="unknown")
        self.assertEqual((r["result"], f.items["Q2"].state), ("end", "accepted"))  # 聞けたので、念のため聞き直さない
        self.assertEqual(f.export()["Q2"], "-")

    def test_hurry_skips_recheck(self):
        f = self.flow()
        self.call(f, "report_unclear", question_id="Q1")
        self.call(f, "report_unclear", question_id="Q1")  # Q1 は「-」
        r = self.call(f, "request_end", reason="callee_hurry")
        self.assertEqual((r["result"], f.phase, f.items["Q1"].rechecks), ("end", "closing", 0))

    def test_done_rejected_while_questions_remain(self):
        f = self.flow()
        self.assertEqual(self.call(f, "request_end", reason="done")["result"], "rejected")

    def test_optional_item_not_asked_back(self):
        f = self.flow(q1_required=False)
        r = self.call(f, "report_unclear", question_id="Q1")
        self.assertEqual((r["result"], f.items["Q1"].state), ("next", "unresolved"))  # 必須でない項目は聞き返さない

    def test_confirm_without_request_rejected(self):
        f = self.flow()
        self.assertEqual(self.call(f, "confirm_field", question_id="Q1", result="affirmed")["result"], "rejected")

    def test_instruction_quotes_names_only_once(self):
        from flowctl import Question
        from instruction import Business, build
        b = Business(company="アオバ物流", caller="佐藤", purpose="確認", questions=[Question("Q1", "質問", "yes_no", conditions=("yes",))])
        _, text = build(b, "L4")
        self.assertIn("株式会社「アオバ物流」の「佐藤」と申します", text)
        self.assertNotIn("「「", text)
        self.assertNotIn("倍速", text)
        self.assertNotIn("1文字も変えない", text)


class TurnCheckTest(unittest.TestCase):
    """AIの1ターンの見分け方（turn_check）。G-17 の実際の例で確かめる。"""

    def test_morae(self):
        from jtext import morae
        self.assertEqual(morae("失礼いたします。"), 9)  # しつれいいたします
        self.assertEqual(morae("株式会社「アオバ物流」"), morae("株式会社アオバ物流"))  # かぎかっこは数えない

    def test_classify_examples(self):
        from turn_check import classify
        # ふつう：Sadachbia「失礼いたします」1.0秒
        self.assertEqual(classify(voiced_s=1.0, voice="Sadachbia", expected_text="失礼いたします。")["kind"], "ok")
        # 途中で止まった：「失礼いたし」0.75秒（G-17 #33）
        self.assertEqual(classify(voiced_s=0.75, voice="Sadachbia", expected_text="失礼いたします。")["kind"], "cut_off")
        # 同じ文を2回言った：Leda 8.39秒（G-17 #24）
        self.assertEqual(classify(voiced_s=8.39, voice="Leda", expected_text="本日は、先日お送りした資料の件でお電話いたしました。")["kind"], "too_long")
        # 声が無い・時間切れ
        self.assertEqual(classify(voiced_s=0.0, voice="Leda", expected_text="はい。")["kind"], "silent")
        self.assertEqual(classify(voiced_s=3.0, voice="Leda", timed_out=True, expected_text="はい。")["kind"], "silent")
        # 比べる言葉が無い
        self.assertEqual(classify(voiced_s=1.0, voice="Leda")["kind"], "unknown")
        # 言うべき言葉が分かっていれば、AI自身の文字起こしより優先する
        self.assertEqual(classify(voiced_s=0.75, voice="Sadachbia", expected_text="失礼いたします。", intended_text="はい")["compared_with"], "expected")


if __name__ == "__main__":
    unittest.main()
