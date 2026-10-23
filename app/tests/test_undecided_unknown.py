"""判定できない結果の試験。判定し直しが要るか（データベースの1か所 salescall.unknown_pending＝0116。settle.awaiting_rejudge が読む）と、
断られたかがまだ決まっていないか（salescall.refusal_undecided＝0118。同じお客様のほかの行を止めるか）は別の問い。途中で切れた通話
（お客様の意思と関係なく）は、通話のあとの確かめで断りの言葉が見つからなければ、断られていない（2026-10-22 ユーザー：「通話の
あとの確かめで断りの言葉が見つからなければ、お詫びのかけ直しをします。断り（営業拒否）の言葉が見つかったときは、営業拒否」）。
見直し15c・16。"""

from __future__ import annotations

from salescall.core import gate, next_action, settle
from tests.test_settle import SettleBase, rep

CUT = ("dialing", "ringing", "answered", "in_conversation", "technical_error")


class UnknownPending(SettleBase):
    def cut_unknown(self, s):
        """途中で切れて判定できない通話（お詫びのかけ直しを予定する。判定し直しの対象）。"""
        aid = self.call(s, CUT, report=rep(cut=None), items=(("Q1", "dont_know", None),))   # 理由の無い「わからない」＝判定できない
        got = self.settle(s, aid)
        self.assertEqual((got["state"], got["decision"].target.purpose), ("unknown", "resume"))
        return aid

    def answered(self, s):
        """項目の答えがそろった（あとの通話で聞けた）。"""
        self.tx(s, lambda tx: tx.execute(
            "UPDATE salescall.item_values SET state = 'confirmed' WHERE task_id = %s AND question_id = 'Q1'", (s["task"],)))

    def undecided(self, s, aid):
        return self.tx(s, lambda tx: tx.one("SELECT salescall.refusal_undecided(%s) AS u", (aid,)))["u"]

    def other_row(self, s):
        run = self.w.run(s["tenant"], s["client"], s["business"], s["version"])
        o = dict(s, task=self.w.task(s["tenant"], s["client"], run, s["contact"]), run=run)
        self.tx(o, lambda tx: next_action.schedule_now(tx, o["task"], cause="試験"))
        return o

    def test_a_later_call_that_reaches_the_customer_and_is_decided_settles_it(self):
        """途中で切れて判定できない通話は、判定し直しの対象だが、断られてはいない（同じお客様のほかの行は待たずにかけられる。
        ⛔ 見直し16：前は判定し直しが済むまで止め、行が終わったあとも止め続けた）。お詫びのかけ直しがお客様につながって判定が
        決まったら、前の通話は決まったとみなす（判定し直しもしない。⛔ 見直し15c）。"""
        s = self.ready()
        o = self.other_row(s)
        a = self.cut_unknown(s)
        self.assertFalse(self.undecided(s, a))
        self.assertEqual(self.tx(s, lambda tx: settle.awaiting_rejudge(tx, s["task"])), a)
        self.assertNotEqual(self.tx(o, lambda tx: gate.pacing(tx, o["task"])).binding, "related_unsettled")
        self.answered(s)
        b = self.call(s, report=rep())                                       # お詫びのかけ直し：お客様と話せた
        self.assertNotEqual(self.settle(s, b)["state"], "unknown")
        self.assertFalse(self.undecided(s, a))
        self.assertIsNone(self.tx(s, lambda tx: settle.awaiting_rejudge(tx, s["task"])))

    def test_a_later_call_that_did_not_reach_the_customer_keeps_it_pending(self):
        """お詫びのかけ直しに出なかった：前の通話は、判定し直しの対象のまま（直れば判定し直す）。断られてはいない。"""
        s = self.ready()
        a = self.cut_unknown(s)
        self.answered(s)
        b = self.call(s, ["dialing", "no_answer"])
        self.assertNotEqual(self.settle(s, b)["state"], "unknown")
        self.assertFalse(self.undecided(s, a))
        self.assertEqual(self.tx(s, lambda tx: settle.awaiting_rejudge(tx, s["task"])), a)

    def test_an_unknown_call_that_was_not_cut_still_holds_the_customer(self):
        """途中で切れていない、判定できない通話（会話の処理の誤り）は、判定し直しまで、断られたかがまだ決まっていない（同じお客様の
        ほかの行は待つ。判定し直しは、直す担当への修正依頼のあと）。"""
        s = self.ready()
        o = self.other_row(s)
        a = self.call(s, report=rep(), items=(("Q1", "dont_know", None),))
        self.assertEqual(self.settle(s, a)["state"], "unknown")
        self.assertTrue(self.undecided(s, a))
        self.assertEqual(self.tx(o, lambda tx: gate.pacing(tx, o["task"])).binding, "related_unsettled")

    def test_a_later_call_whose_result_is_also_unknown_keeps_it_pending(self):
        """あとの発信がお客様につながっても、その結果も判定できなければ、前の通話はまだ判定し直しの対象（決まったとみなすのは、あとの
        結果が決まったときだけ＝salescall.unknown_pending の1か所。見直し16b：この場面の試験が無かった）。"""
        s = self.ready()
        a = self.cut_unknown(s)
        b = self.call(s, report=rep())                                       # つながったが、理由の無い「わからない」のまま
        self.assertEqual(self.settle(s, b)["state"], "unknown")
        self.assertTrue(self.tx(s, lambda tx: tx.one("SELECT salescall.unknown_pending(%s) AS p", (a,)))["p"])

    def test_no_rejudging_while_a_later_call_waits_to_be_settled(self):
        """あとの発信の締めがまだ（通話後の検証待ちなど）の行は、判定し直しの対象に出さない（締めと判定し直しが、行の予定を同時に
        書かない＝settle.awaiting_rejudge の1か所。締めが済めば、また出る。見直し16b：この場面の試験が無かった）。"""
        s = self.ready()
        a = self.cut_unknown(s)
        b = self.call(s, ["dialing", "no_answer"])                            # 発信したが、まだ締めていない
        self.assertIsNone(self.tx(s, lambda tx: settle.awaiting_rejudge(tx, s["task"])))
        self.settle(s, b)
        self.assertEqual(self.tx(s, lambda tx: settle.awaiting_rejudge(tx, s["task"])), a)

    def test_a_later_call_that_reached_the_customer_but_is_not_settled_keeps_it_pending(self):
        """あとの発信がお客様につながっても、締めがまだなら（結果がまだ無い）、前の通話はまだ決まっていない（salescall.unknown_pending の
        1か所だけで正しい答えにする。いまの読む所は締め待ちの行を先に外すが、それに頼らない。⛔ 見直し17b：この場面の試験が無かった）。"""
        s = self.ready()
        a = self.cut_unknown(s)
        self.answered(s)
        b = self.call(s, report=rep())                                       # お詫びのかけ直しでお客様と話せたが、まだ締めていない
        pending = lambda: self.tx(s, lambda tx: tx.one("SELECT salescall.unknown_pending(%s) AS p", (a,)))["p"]
        self.assertTrue(pending())
        self.settle(s, b)
        self.assertFalse(pending())
