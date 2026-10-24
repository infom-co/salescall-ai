"""会話の処理が書き込む入口の試験（core/conversation_io.py。2026-10-24 ユーザー：入口は「CL-2」。DESIGN.md 4-4・7-1）。"""

from __future__ import annotations

import unittest

from salescall.core import attempts, call_report, conversation_io, dnc, gate
from salescall.core.access import Rejected
from salescall.core.conversation_io import ItemInvalid, ItemValue
from tests.support import CLEAN, SYSTEM
from tests.test_gate import FakeDriver, dial_exit
from tests.test_settle import SettleBase, rep


class Write(SettleBase):
    def answered(self, s, path=("ringing", "answered", "in_conversation")):
        """本物の門・出口を通して1回かけ、path まで進める（通話の途中：項目・報告・検証はまだ）。"""
        c = self.claim(s)
        self.assertIsInstance(c, gate.Claim, c)
        self.assertTrue(dial_exit.dial(self.w.a, SYSTEM, s["tenant"], s["client"], c.attempt_id, FakeDriver()))
        for st in path:
            self.tx(s, lambda tx: attempts.move(tx, c.attempt_id, st))
        return c.attempt_id

    def write(self, s, aid, item):
        return self.tx(s, lambda tx: conversation_io.write_item(tx, aid, item))

    def items(self, s):
        return self.tx(s, lambda tx: tx.all(
            "SELECT question_id, state::text AS state, value, condition, reason, hedged, attempt_id FROM salescall.item_values"
            " WHERE task_id = %s", (s["task"],)))

    def test_the_conversation_writes_through_the_entry_and_settle_reads_it(self):
        """通話の途中で答えを書き（書き換えもできる）、報告を置き、締めがそれを読んで「完了」にする（入口だけを通す1本）。"""
        s = self.ready()
        aid = self.answered(s)
        self.write(s, aid, ItemValue("Q1", "heard", {"text": "来週の火曜"}, "答えた"))
        self.write(s, aid, ItemValue("Q1", "confirmed", {"text": "来週の火曜の午後"}, "答えた"))
        got = self.items(s)
        self.assertEqual([(r["question_id"], r["state"], r["value"], r["condition"], r["attempt_id"]) for r in got],
                         [("Q1", "confirmed", {"text": "来週の火曜の午後"}, "答えた", aid)])
        for st in ("ending", "ended"):
            self.tx(s, lambda tx: attempts.move(tx, aid, st))
        self.tx(s, lambda tx: conversation_io.save_report(tx, aid, rep().to_json()))
        self.tx(s, lambda tx: attempts.record_verification(tx, aid, summary=CLEAN))
        self.assertEqual(self.settle(s, aid)["state"], "completed")

    def test_wrong_items_are_refused(self):
        """書けない答えは断る（別の版にしかない質問・決まっていない状態・返りえない選択肢・状態に合わない理由・決まっていない理由・
        空の言い切っていない言葉）。理由の無い「わからない」は受け取る（判定が理由の抜けとして修正依頼を出す。答えは失わない）。"""
        s = self.ready()
        aid = self.answered(s)
        for item in (ItemValue("Q9", "confirmed", None, "答えた"),
                     ItemValue("Q1", "maybe", None, "答えた"),
                     ItemValue("Q1", "confirmed", None, "はい"),
                     ItemValue("Q1", "confirmed", None, "答えた", reason="undecided"),
                     ItemValue("Q1", "dont_know", None, "わからない", reason="policy"),
                     ItemValue("Q1", "refused", None, "答えたくない", reason="busy"),
                     ItemValue("Q1", "confirmed", None, "答えた", hedged="  ")):
            with self.subTest(item=item), self.assertRaises(ItemInvalid):
                self.write(s, aid, item)
        self.assertEqual(self.items(s), [])
        self.write(s, aid, ItemValue("Q1", "dont_know", None, "わからない"))
        self.write(s, aid, ItemValue("Q1", "refused", None, "答えたくない", reason="unwilling", hedged="ちょっとそれは"))
        self.assertEqual([(r["state"], r["reason"], r["hedged"]) for r in self.items(s)], [("refused", "unwilling", "ちょっとそれは")])

    def test_only_an_answered_call_before_its_verification_is_done(self):
        """お客様が出ていない通話・通話後の検証が済んだ通話には書かない（済むと締めが読むので、そのあとに書き換えない）。"""
        s = self.ready()
        aid = self.answered(s, path=("ringing", "no_answer"))
        with self.assertRaises(ItemInvalid):
            self.write(s, aid, ItemValue("Q1", "confirmed", None, "答えた"))
        t = self.ready()
        bid = self.call(t, items=[], report=rep())    # 出て話して終わり、検証も済んだ
        with self.assertRaises(ItemInvalid):
            self.write(t, bid, ItemValue("Q1", "confirmed", None, "答えた"))

    def test_only_the_newest_call_of_the_row_writes(self):
        """答えは、その行のいちばん新しい通話の番号でだけ書く（前の通話の番号では、データベースが断る＝0108）。前の答えは残る。"""
        s = self.ready()
        first = self.answered(s)
        self.write(s, first, ItemValue("Q1", "dont_know", None, "わからない"))
        for st in ("ending", "ended"):
            self.tx(s, lambda tx: attempts.move(tx, first, st))
        second = self.w.insert_claim(s)                    # 同じ行の次の通話（門の間隔は、ここでは試さない）
        for st in ("dialing", "ringing", "answered"):
            self.w.move(s, second, st)
        with self.assertRaises(Rejected) as e:
            self.write(s, first, ItemValue("Q1", "dont_know", None, "わからない", reason="undecided"))
        self.assertEqual(e.exception.constraint, "item_values_from_newest_call")
        self.write(s, second, ItemValue("Q1", "confirmed", None, "答えた"))
        kept = self.tx(s, lambda tx: tx.all("SELECT attempt_id, state::text AS state FROM salescall.item_value_history"
                                            " WHERE task_id = %s", (s["task"],)))
        self.assertEqual([(r["attempt_id"], r["state"]) for r in kept], [(first, "dont_know")])

    def test_reports_and_declarations_go_through_the_same_one_place(self):
        """報告の形の確かめは call_report.from_json の1か所（合わない報告は断る）。「もうかけないで」は dnc.register_declared の
        1か所（その場で台帳へ。何度呼んでも同じ）。"""
        s = self.ready()
        aid = self.answered(s)
        with self.assertRaises(call_report.ReportInvalid):
            self.tx(s, lambda tx: conversation_io.save_report(tx, aid, {"version": 3, "unknown_key": 1}))
        first = self.tx(s, lambda tx: conversation_io.declare_do_not_call(tx, aid))
        again = self.tx(s, lambda tx: conversation_io.declare_do_not_call(tx, aid))
        self.assertEqual(first["entry_id"], again["entry_id"])
        self.assertTrue(self.tx(s, lambda tx: dnc.refused_in(tx, aid, "refused_in_call")))


if __name__ == "__main__":
    unittest.main()
