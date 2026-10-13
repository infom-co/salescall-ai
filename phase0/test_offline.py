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
        return f.apply(name, args)  # 中の名前での入口（AIからの入口 handle の置き換えは、AIToJapaneseTest で確かめる）

    def test_yes_no_accepted_and_advances(self):
        f = self.flow()
        r = self.call(f, "record_answer", question_id="Q1", condition_id="yes", value="はい、予約しています。")
        self.assertEqual((r["result"], f.current, f.items["Q1"].state), ("next", "Q2", "accepted"))
        self.assertEqual(f.export()["Q1"], "yes")  # 選択肢の型は、相手の言葉ではなく宣言された選択肢を渡す

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

    def test_done_rejected_while_awaiting_readback_or_rechecking(self):
        f = self.flow()
        self.call(f, "record_answer", question_id="Q1", condition_id="yes")
        self.call(f, "record_answer", question_id="Q2", condition_id="answered", value="15日の午後2時")  # 最後の質問の言い直し待ち
        self.assertEqual(self.call(f, "request_end", reason="done")["result"], "rejected")
        self.assertEqual(f.items["Q2"].state, "checking")
        f = self.flow()
        self.call(f, "record_answer", question_id="Q1", condition_id="yes")
        self.call(f, "report_unclear", question_id="Q2")
        self.call(f, "report_unclear", question_id="Q2")                  # 聞き返しを使い切り「-」⇒ 念のための確認へ
        self.assertEqual((f.phase, f.current), ("rechecking", "Q2"))
        self.assertEqual(self.call(f, "request_end", reason="done")["result"], "rejected")  # 念のための確認を飛ばさない
        self.assertEqual(self.call(f, "request_end", reason="callee_hurry")["result"], "end")  # 急いでいる相手は終えてよい
        self.assertEqual(f.export()["Q2"], "-")

    def test_optional_item_not_asked_back(self):
        f = self.flow(q1_required=False)
        r = self.call(f, "report_unclear", question_id="Q1")
        self.assertEqual((r["result"], f.items["Q1"].state), ("next", "unresolved"))  # 必須でない項目は聞き返さない

    def test_record_answer_rejected_while_awaiting_readback_reply(self):
        # G-16：言い直した値に相手が「はい」と答えたのに、AIが record_answer を呼んだ ⇒ 差し戻して confirm_field へ導く
        f = self.flow()
        self.call(f, "record_answer", question_id="Q1", condition_id="yes")
        self.call(f, "record_answer", question_id="Q2", condition_id="answered", value="15日の午後2時")
        f.on_callee()
        r = self.call(f, "record_answer", question_id="Q2", condition_id="answered", value="15日の午後2時")
        self.assertEqual(r["result"], "rejected")
        self.assertIn("confirm_field", r["message"])

    def test_value_normalized(self):
        from flowctl import normalize_value
        self.assertEqual(normalize_value("datetime", "15日の午後2時です。"), "15日の午後2時")
        self.assertEqual(normalize_value("number", "3人です"), "3人")
        self.assertEqual(normalize_value("name", "あかね"), "あかね")  # 名前の終わりを削らない
        self.assertEqual(normalize_value("yes_no", "はい、そうです。"), "はい、そうです。")  # 選択肢の型はそのまま（渡すのは選択肢）

    def test_every_rejection_says_what_to_do_now(self):
        # 2026-10-13 G-16 の2回目：Q2 の言い直しの返事を待っている間に、Q3 の答えとして報告された
        from flowctl import Flow, Question
        f = Flow([Question("Q1", "ご予約はされていますか。", "yes_no", conditions=("yes", "no")),
                  Question("Q2", "ご希望の日時をお聞かせください。", "datetime", conditions=("answered",)),
                  Question("Q3", "ご来店の人数をお聞かせください。", "number", conditions=("answered",))])
        self.call(f, "record_answer", question_id="Q1", condition_id="yes")
        f.on_callee()
        self.call(f, "record_answer", question_id="Q2", condition_id="answered", value="15日の午後2時です。")
        f.on_callee()
        for op, kw in (("record_answer", {"question_id": "Q3", "condition_id": "answered", "value": "3人"}),
                       ("report_unclear", {"question_id": "Q3"}), ("request_end", {"reason": "done"}),
                       ("confirm_field", {"question_id": "Q3", "result": "affirmed"}), ("no_such_op", {})):
            r = self.call(f, op, **kw)
            self.assertEqual(r["result"], "rejected", op)
            self.assertIn("15日の午後2時で、よろしいでしょうか。", r["message"], op)  # 待っている言い直しと、その言い方
            self.assertIn("confirm_field", r["message"], op)
        self.assertEqual(f.items["Q3"].state, "empty")  # 差し戻した報告は、記録に入らない
        r = self.call(f, "confirm_field", question_id="Q2", result="affirmed")
        self.assertEqual((r["result"], r["question_id"]), ("next", "Q3"))
        r = self.call(f, "record_answer", question_id="Q1", condition_id="yes")
        self.assertIn("いまの質問は Q3「", r["message"])  # 言い直しを待っていないときは、いまの質問とその文

    def test_value_required_for_value_types(self):
        # 2026-10-13 G-25：値の無い答えを受け付け、言い直しに「Noneで、よろしいでしょうか。」を入れていた
        f = self.flow()
        self.call(f, "record_answer", question_id="Q1", condition_id="yes")          # 選択肢の型は、値が無くてよい
        r = self.call(f, "record_answer", question_id="Q2", condition_id="answered")  # 値の要る型で値が無い
        self.assertEqual(r["result"], "rejected")
        self.assertNotIn("None", r["message"])
        self.assertEqual((f.items["Q2"].state, f.items["Q2"].value), ("empty", None))  # 記録は書き換えない
        r = self.call(f, "record_answer", question_id="Q2", condition_id="unknown")   # 「わからない」は値が無くてよい
        self.assertEqual(r["result"], "end")

    def test_correction_value_required(self):
        f = self.flow()
        self.call(f, "record_answer", question_id="Q1", condition_id="yes")
        self.call(f, "record_answer", question_id="Q2", condition_id="answered", value="15日の午後2時")
        f.on_callee()
        r = self.call(f, "confirm_field", question_id="Q2", result="corrected")       # 訂正の値が無い
        self.assertEqual(r["result"], "rejected")
        self.assertEqual((f.awaiting_confirm, f.items["Q2"].value, f.items["Q2"].corrections), ("Q2", "15日の午後2時", 0))
        r = self.call(f, "confirm_field", question_id="Q2", result="corrected", correction="16日の午後2時です。")
        self.assertEqual((r["result"], r["value"]), ("confirm_required", "16日の午後2時"))

    def test_confirm_phrases_rotate_by_situation(self):
        from flowctl import Flow, Question
        from phrases import CONFIRM
        f = Flow([Question("Q1", "日時は。", "datetime", conditions=("answered",)), Question("Q2", "人数は。", "number", conditions=("answered",)),
                  Question("Q3", "電話番号は。", "phone", conditions=("answered",))])
        r1 = self.call(f, "record_answer", question_id="Q1", condition_id="answered", value="15日")
        f.on_callee()
        self.call(f, "confirm_field", question_id="Q1", result="affirmed")
        r2 = self.call(f, "record_answer", question_id="Q2", condition_id="answered", value="3人")
        self.assertEqual(r1["say"], CONFIRM["ふつう"][0].format(v="15日"))
        self.assertEqual(r2["say"], CONFIRM["ふつう"][1].format(v="3人"))       # 同じ言い方を続けない（順番に回す）
        f.on_callee()
        self.assertIn(r2["say"], self.call(f, "record_answer", question_id="Q3", condition_id="answered", value="x")["message"])  # 差し戻しも同じ言い方
        self.call(f, "confirm_field", question_id="Q2", result="affirmed")
        self.call(f, "report_unclear", question_id="Q3")                       # 聞き返したあとの値
        r3 = self.call(f, "record_answer", question_id="Q3", condition_id="answered", value="090-1234-5678")
        self.assertEqual(r3["say"], CONFIRM["聞き取りにくかったあと"][0].format(v="090-1234-5678"))
        f.on_callee()
        r4 = self.call(f, "confirm_field", question_id="Q3", result="corrected", correction="090-1234-5679")
        self.assertEqual(r4["say"], CONFIRM["聞き取りにくかったあと"][1].format(v="090-1234-5679"))  # 訂正のあとも同じ場面で、次の言い方

    def test_lead_ins_rotate_and_default_off(self):
        from flowctl import Flow, Question
        from phrases import LEAD_IN
        qs = [Question("Q1", "日時は。", "datetime", conditions=("答えた",)), Question("Q2", "人数は。", "number", conditions=("答えた",))]
        f = Flow(qs)                                                       # 既定は付けない（承認前）
        self.assertEqual(self.call(f, "record_answer", question_id="Q1", condition_id="答えた", value="15日")["say"], "15日で、よろしいでしょうか。")
        f = Flow(qs, lead_ins=True)
        r1 = self.call(f, "record_answer", question_id="Q1", condition_id="答えた", value="15日")
        self.assertTrue(r1["say"].startswith(LEAD_IN["言い直し"][0]))
        f.on_callee()
        r2 = self.call(f, "confirm_field", question_id="Q1", result="affirmed")
        self.assertIn(f"「{LEAD_IN['次の質問'][0]}」から話し始めてください。", r2["message"])
        r3 = self.call(f, "record_answer", question_id="Q2", condition_id="答えた", value="3人")
        self.assertTrue(r3["say"].startswith(LEAD_IN["言い直し"][1]))   # 同じ受けの言葉を続けない
        f.on_callee()
        self.assertIn(r3["say"], self.call(f, "request_end", reason="done")["message"])  # 差し戻しも同じ言い方

    def test_busy_ask_once_when_set(self):
        from flowctl import Flow, Question
        qs = [Question("Q1", "ご予約はされていますか。", "yes_no", conditions=("yes", "no"))]
        f = Flow(qs)                                                       # 既定：丁寧に終える
        self.assertEqual(self.call(f, "request_end", reason="callee_hurry")["result"], "end")
        f = Flow(qs, busy_ask="少しだけ、お時間をいただけますでしょうか。")      # 業務設定で「引き止める」を選んだ
        r = self.call(f, "request_end", reason="callee_hurry")
        self.assertEqual((r["result"], r["say"], f.phase), ("busy_ask", "少しだけ、お時間をいただけますでしょうか。", "asking"))
        self.assertEqual(self.call(f, "request_end", reason="callee_hurry")["result"], "end")  # 2回目は終える（引き止めは1回だけ）
        f = Flow(qs, busy_ask="少しだけ、お時間をいただけますでしょうか。")
        self.assertEqual(self.call(f, "request_end", reason="callee_refused_call")["result"], "end")  # 断られたら必ず終える（原則14）

    def test_confirm_phrase_is_the_fixed_one(self):
        from phrases import confirm
        f = self.flow()
        self.call(f, "record_answer", question_id="Q1", condition_id="yes")
        r = self.call(f, "record_answer", question_id="Q2", condition_id="answered", value="16日の午後2時")
        self.assertEqual(r["say"], confirm("16日の午後2時"))
        self.assertIn(f"「{confirm('16日の午後2時')}」", r["message"])

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
        # 日本語でない言葉を話した（G-17：考えていることを英語で口に出した）。長さが見合っていても拾う
        eng = "To read this script, I'll project a polite and natural business tone."
        self.assertEqual(classify(voiced_s=7.8, voice="Sadachbia", expected_text="来週の火曜日、10月20日の午後3時に、改めてお電話してもよろしいでしょうか。", intended_text=eng)["kind"], "wrong_language")
        # 社名などの短いローマ字は数えない
        self.assertEqual(classify(voiced_s=1.6, voice="Leda", intended_text="ABC株式会社です。")["kind"], "ok")

    def test_language_is_judged_by_what_was_heard(self):
        from turn_check import classify, foreign_text
        en = "念のため確認させてください。15日の午後2時でお間違いございませんか。"
        # 2026-10-13 G-16 の3回目：英語を声に出した（別の耳にも英語）
        c = classify(voiced_s=8.7, voice="Leda", intended_text=en + '*Confirming the date and time "15th at 2 PM".*',
                     heard_text=en + "Confirming the date and time, 15th at 2:00 p.m.")
        self.assertEqual((c["kind"], c["language_from"]), ("wrong_language", "heard"))
        # 2026-10-13 G-5：声は日本語なのに、AI自身の文字が韓国語だった ⇒ 誤りではない。長さは別の耳の文字で比べる
        c = classify(voiced_s=8.48, voice="Sadachbia", intended_text="ご 계약의 확정에 대해서는私からはお答えできかねます。本日はご予約の確認をいただき、誠にありがとうございました。",
                     heard_text="ご契約の確定については、私からはお答えできません。本日はご予約の確認をいただき、誠にありがとうございました。")
        self.assertEqual((c["kind"], c["compared_with"]), ("ok", "heard"))
        # 別の耳が無いとき・何も文字にしなかったときは、AI自身の文字で見る。ハングルなど、日本語の文字・ローマ字以外も見る
        self.assertEqual(classify(voiced_s=2.0, voice="Leda", intended_text="はい、계약입니다。")["kind"], "wrong_language")
        self.assertEqual(classify(voiced_s=2.0, voice="Leda", intended_text="はい、계약입니다。", heard_text="")["kind"], "wrong_language")
        self.assertEqual(classify(voiced_s=2.0, voice="Leda", intended_text="はい。", heard_text="はい、계약입니다。")["kind"], "wrong_language")
        # 日本語の文の中の名前のローマ字・全角の英字・長音の記号・繰り返しの記号は、日本語として扱う
        for ok in ("弊社の『スマートリンクPro』です。", "株式会社ＡＢＣホールディングスの佐藤々木です。", "スーパー々", "15日の午後2時、3名様。"):
            self.assertIsNone(foreign_text(ok), ok)
        self.assertIsNotNone(foreign_text("To read this script, I'll project a polite tone."))
        # 言うべき言葉が分かっていれば、AI自身の文字起こしより優先する
        self.assertEqual(classify(voiced_s=0.75, voice="Sadachbia", expected_text="失礼いたします。", intended_text="はい")["compared_with"], "expected")



class PlaybackTest(unittest.TestCase):
    """定型の音声を流している最中の、相手の声の見張り（playback）。"""

    def tone(self, seconds, rate=16000, amp=0.2):
        import numpy as np
        n = int(seconds * rate)
        x = amp * np.sin(2 * np.pi * 200 * np.arange(n) / rate)
        return (x * 32767).astype("<i2").tobytes()

    def silence(self, seconds, rate=16000):
        return b"\x00\x00" * int(seconds * rate)

    def test_short_backchannel_does_not_stop(self):
        from playback import barge_in_at
        pcm = self.silence(1.0) + self.tone(0.4) + self.silence(2.0)  # 「はい」くらいの短い声
        self.assertIsNone(barge_in_at(pcm, 16000))

    def test_longer_speech_stops_after_min_speech(self):
        from playback import MIN_SPEECH_S, barge_in_at, speech_start_before
        pcm = self.silence(1.0) + self.tone(1.5) + self.silence(1.0)
        stop = barge_in_at(pcm, 16000)
        self.assertAlmostEqual(stop, 1.0 + MIN_SPEECH_S, delta=0.05)
        self.assertAlmostEqual(speech_start_before(pcm, 16000, stop), 1.0, delta=0.05)  # 話し始めから送る

    def test_breath_inside_speech_counts_as_one(self):
        from playback import barge_in_at
        pcm = self.silence(0.5) + self.tone(0.5) + self.silence(0.15) + self.tone(0.5) + self.silence(1.0)
        self.assertIsNotNone(barge_in_at(pcm, 16000))  # 息継ぎ（0.15秒）で切れても、続いた声とみなす

    def test_split_played(self):
        from playback import Clip, split_played
        clips = [Clip("A。", 1.5), Clip("B。", 2.0), Clip("C。", 1.8)]
        self.assertEqual(split_played(clips, None), (["A。", "B。", "C。"], []))
        self.assertEqual(split_played(clips, 2.0), (["A。"], ["B。", "C。"]))  # 途中で止めた文は「まだ」
        self.assertEqual(split_played(clips, 3.5), (["A。", "B。"], ["C。"]))



class QuoteInTextTest(unittest.TestCase):
    """文の中の名前を「」でくくる処理（instruction.quote_in_text）。名前の位置にだけ付け、ほかの文字は変えない。"""

    def q(self, text, names):
        from instruction import quote_in_text
        out = quote_in_text(text, names)
        self.assertEqual(out.replace("「", "").replace("」", ""), text.replace("「", "").replace("」", ""))  # ほかの文字は変えない
        return out

    def test_company_with_corp_form(self):
        self.assertEqual(self.q("株式会社アオバ物流の佐藤と申します。", ["アオバ物流", "佐藤"]), "株式会社「アオバ物流」の「佐藤」と申します。")
        self.assertEqual(self.q("アオバ物流株式会社の者です。", ["アオバ物流"]), "「アオバ物流」株式会社の者です。")

    def test_every_occurrence(self):
        self.assertEqual(self.q("佐藤と申します。佐藤がご案内します。", ["佐藤"]), "「佐藤」と申します。「佐藤」がご案内します。")

    def test_longest_first_no_overlap(self):
        self.assertEqual(self.q("川崎市中原区ですね。中原区の方ですね。", ["川崎市中原区", "中原区"]), "「川崎市中原区」ですね。「中原区」の方ですね。")

    def test_already_bracketed_not_doubled(self):
        self.assertEqual(self.q("弊社の『スマートリンクPro』と、スマートリンクProです。", ["スマートリンクPro"]),
                         "弊社の『スマートリンクPro』と、「スマートリンクPro」です。")

    def test_full_and_half_width(self):
        self.assertEqual(self.q("株式会社ＡＢＣホールディングスです。", ["ABCホールディングス"]), "株式会社「ＡＢＣホールディングス」です。")  # 書き方は台本のまま

    def test_not_inside_another_word(self):
        self.assertEqual(self.q("上野原の事務所の上野様です。", ["上野"]), "上野原の事務所の「上野」様です。")
        self.assertEqual(self.q("株式会社日本橋商事の上野です。", ["日本"]), "株式会社日本橋商事の上野です。")  # 「日本橋」の中の「日本」

    def test_words_right_after_name(self):
        self.assertEqual(self.q("株式会社ＡＢＣホールディングス営業部の小鳥遊と申します。", ["ABCホールディングス", "小鳥遊"]),
                         "株式会社「ＡＢＣホールディングス」営業部の「小鳥遊」と申します。")
        self.assertEqual(self.q("弊社スマートリンクProのご案内です。", ["スマートリンクPro"]), "弊社「スマートリンクPro」のご案内です。")
        self.assertEqual(self.q("上野駅の近くです。", ["上野"]), "「上野」駅の近くです。")

    def test_limit_name_split_by_tokenizer(self):
        # 限界（instruction.quote_in_text の説明）：「神田川」は「神田／川」に分かれる。長いほうの名前が一覧にあれば、長いほうに付く
        self.assertEqual(self.q("東京の神田と、神田川です。", ["神田", "神田川"]), "東京の「神田」と、「神田川」です。")
        self.assertEqual(self.q("東京の神田と、神田川です。", ["神田"]), "東京の「神田」と、「神田」川です。")

    def test_honorific_and_shop(self):
        self.assertEqual(self.q("大阪の日本橋店にお越しいただきました。", ["日本橋"]), "大阪の「日本橋」店にお越しいただきました。")



class CalleeSampleTest(unittest.TestCase):
    """相手役の声の確かめ（make_callee_samples.sample_ok）。問いかけは「？」まで聞こえること。"""

    def test_question_must_be_heard_as_question(self):
        from make_callee_samples import sample_ok
        self.assertFalse(sample_ok("はい？", "はい。")["ok"])     # 2026-10-13 G-22：聞き返しが答えに聞こえていた
        self.assertTrue(sample_ok("はい？", "はい？")["ok"])
        self.assertTrue(sample_ok("はい、どちら様ですか？", "はい、どちら様ですか?")["ok"])  # 半角の「?」も同じ

    def test_fillers_and_lexical_questions(self):
        from make_callee_samples import question_by_words, sample_ok
        self.assertTrue(sample_ok("あ、はい、どちら様？", "はい、どちら様?")["ok"])          # 頭の「あ」を落としても意味は同じ
        self.assertTrue(sample_ok("はい、何でしょう？", "はい、なんでしょう。")["ok"])      # 「何」で問いかけと分かる
        self.assertFalse(sample_ok("はい、予約しています。", "いいえ、予約しています。")["ok"])  # 応答の言葉は必ず比べる
        self.assertFalse(sample_ok("はい。", "Hey.")["ok"])
        self.assertTrue(question_by_words("もう一度お願いできますか？"))
        self.assertFalse(question_by_words("はい？"))

    def test_reading_must_match(self):
        from make_callee_samples import sample_ok
        self.assertFalse(sample_ok("はい、予約しています。", "はい、知ってます。")["ok"])
        self.assertTrue(sample_ok("はい、予約しています。", "はい、予約しています")["ok"])  # 句点の有無は問わない（問いかけでない文）



class CompareReadingTest(unittest.TestCase):
    """2つの文を比べるための読み（jtext.compare_reading・listen.differences）。"""

    def test_same_sound_different_kanji_is_not_a_difference(self):
        from listen import differences
        self.assertEqual(differences("お電話代わりました。", "お電話変わりました。"), [])  # ⛔ pykakasi では「だいわり」と読んでいた
        self.assertEqual(differences("株式会社「アオバ物流」の「佐藤」と申します。", "株式会社青葉物流の佐藤と申します。"), [])
        self.assertEqual(differences("15日の午後2時です。", "15 日の午後 2 時です"), [])  # 空白・句読点は数えない

    def test_real_difference_remains(self):
        from listen import differences
        self.assertEqual([d["heard"] for d in differences("ご来店の人数をお聞かせください。", "来店の人数をお聞かせください。")], [""])
        self.assertTrue(differences("15日の午後2時ですね。", "4日の午後2時ですね。"))



class AIToJapaneseTest(unittest.TestCase):
    """AIに見せる言葉は日本語だけ（2026-10-13 ユーザー指摘「こちらが英語を使っていませんか？」）。置き換えは flowctl の表の1か所。"""

    def business(self):
        from flowctl import Question
        from instruction import Business
        return Business(company="アオバ物流", caller="佐藤", purpose="ご予約の確認",
                        questions=[Question("Q1", "ご予約はされていますか。", "yes_no", conditions=("はい", "いいえ")),
                                   Question("Q2", "ご希望の日時をお聞かせください。", "datetime", conditions=("答えた",))])

    def test_handle_translates_both_ways(self):
        from flowctl import Flow
        f = Flow(self.business().questions)
        r = f.handle(fc("record_answer", 質問="Q1", 答えの種類="はい"))
        self.assertEqual((r["結果"], r["質問"]), ("次の質問", "Q2"))
        r = f.handle(fc("record_answer", 質問="Q2", 答えの種類="答えた", 値="15日の午後2時です。"))
        self.assertEqual((r["結果"], r["値"], r["言い方"]), ("言い直して確かめる", "15日の午後2時", "15日の午後2時で、よろしいでしょうか。"))
        f.on_callee()
        r = f.handle(fc("confirm_field", 質問="Q2", 返事の種類="合っている"))
        self.assertEqual(r["結果"], "締め")
        self.assertEqual(f.log[-1]["args"], {"question_id": "Q2", "result": "affirmed"})  # 記録は中の名前
        g = Flow(self.business().questions)
        g.handle(fc("record_answer", 質問="Q1", 答えの種類="答えたくない"))
        self.assertEqual(g.export()["Q1"], "✕")
        self.assertEqual(g.handle(fc("request_end", 理由="相手が急いでいる"))["結果"], "締め")

    def test_misspelled_or_missing_args_rejected_before_recording(self):
        # 2026-10-13 G-26・G-28：AIが項目名を「与えの返事」「合い手の返事」と書き違えた。返事の種類が無いまま受け取り、
        # 「相手が答えなかった」として、相手が認めた値を「-」にしていた（60回中4回。どれも黙って起きた）
        from flowctl import Flow
        for bad in ({"質問": "Q2", "与えの返事": "合っている", "訂正された値": "15日の午後2時"},   # G-28 で実際に届いた形
                    {"質問": "Q2"},                                                      # 返事の種類が無い
                    {"質問": "Q2", "返事の種類": "合ってる"},                              # 選択肢に無い値
                    {"質問": "Q2", "返事の種類": 1}):                                     # 文字でない値
            f = Flow(self.business().questions)
            f.handle(fc("record_answer", 質問="Q1", 答えの種類="はい"))
            f.handle(fc("record_answer", 質問="Q2", 答えの種類="答えた", 値="15日の午後2時です。"))
            f.on_callee()
            r = f.handle(fc("confirm_field", **bad))
            self.assertEqual(r["結果"], "差し戻し", bad)
            self.assertIn("15日の午後2時で、よろしいでしょうか。", r["次にすること"], bad)  # いま求めていること（_now）も添える
            self.assertEqual((f.awaiting_confirm, f.items["Q2"].state), ("Q2", "checking"), bad)  # 記録は書き換えない
            if "与えの返事" in bad:
                self.assertIn("「与えの返事」という項目は", r["次にすること"])
                self.assertIn("返事の種類", r["次にすること"])  # 使える項目の名前を言う
            r = f.handle(fc("confirm_field", 質問="Q2", 返事の種類="合っている"))  # 正しく報告し直せば、受け付ける
            self.assertEqual((r["結果"], f.items["Q2"].state), ("締め", "confirmed"), bad)
        f = Flow(self.business().questions)
        self.assertEqual(f.handle(fc("request_end", 理由="もう切りたい"))["結果"], "差し戻し")
        self.assertEqual(f.handle(fc("record_answer", 答えの種類="はい"))["結果"], "差し戻し")      # 質問が無い
        self.assertEqual(f.items["Q1"].state, "empty")

    def test_declarations_and_checks_use_one_table(self):
        from flowctl import ARG_LABEL, OP_ARGS, tool_declarations
        decls = {d.name: d for d in tool_declarations(self.business().questions)[0].function_declarations}
        self.assertEqual(set(decls), set(OP_ARGS))
        for name, (req, opt) in OP_ARGS.items():
            self.assertEqual(set(decls[name].parameters.properties), {ARG_LABEL[k] for k in req + opt}, name)
            self.assertEqual(decls[name].parameters.required, [ARG_LABEL[k] for k in req], name)

    def test_no_english_shown_to_ai(self):
        import json
        import re

        from flowctl import Flow, tool_declarations
        from instruction import build
        b = self.business()
        allowed = {"record_answer", "report_unclear", "confirm_field", "request_end", "Q1", "Q2"}  # 操作の名前（道具の決まり）と質問の番号
        shown = [build(b, t)[1] for t in ("S2", "L4")]
        shown += [build(b, "S2", opening_mode="recorded")[1]]
        for d in tool_declarations(b.questions)[0].function_declarations:
            shown.append(json.dumps(d.model_dump(exclude_none=True), ensure_ascii=False))
        f = Flow(b.questions)
        for name, args in (("record_answer", {"質問": "Q2", "答えの種類": "はい"}), ("record_answer", {"質問": "Q1", "答えの種類": "はい"}),
                           ("record_answer", {"質問": "Q2", "答えの種類": "答えた"}),
                           ("record_answer", {"質問": "Q2", "答えの種類": "答えた", "値": "15日"}), ("request_end", {"理由": "質問が終わった"}),
                           ("no_such", {})):
            shown.append(json.dumps(f.handle(fc(name, **args)), ensure_ascii=False))
        schema_words = {"name", "description", "parameters", "type", "properties", "enum", "required", "OBJECT", "STRING"}  # 宣言の形の名前
        for s in shown:
            words = set(re.findall(r"[A-Za-z_][A-Za-z_0-9]*", s)) - allowed - schema_words
            self.assertEqual(words, set(), s[:200])


if __name__ == "__main__":
    unittest.main()
