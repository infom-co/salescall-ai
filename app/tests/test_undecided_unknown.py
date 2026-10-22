"""判定できない結果が「まだ決まっていない」かの試験（データベースの1か所 salescall.unknown_pending＝0116。判定し直しの対象と、
断られたかがまだ決まっていない＝refusal_undecided の両方が読む）。見直し15c。"""

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
        """お詫びのかけ直しがお客様につながって判定が決まったら、前の通話は決まったとみなす（判定し直しもしない）。同じお客様の
        ほかの行も、待たずにかけられる（⛔ 見直し15c：前は前の通話が「まだ決まっていない」のまま残り、永久にかからなかった）。"""
        s = self.ready()
        o = self.other_row(s)
        a = self.cut_unknown(s)
        self.assertTrue(self.undecided(s, a))
        self.assertEqual(self.tx(s, lambda tx: settle.awaiting_rejudge(tx, s["task"])), a)
        self.assertEqual(self.tx(o, lambda tx: gate.pacing(tx, o["task"])).binding, "related_unsettled")
        self.answered(s)
        b = self.call(s, report=rep())                                       # お詫びのかけ直し：お客様と話せた
        self.assertNotEqual(self.settle(s, b)["state"], "unknown")
        self.assertFalse(self.undecided(s, a))
        self.assertIsNone(self.tx(s, lambda tx: settle.awaiting_rejudge(tx, s["task"])))
        self.assertNotEqual(self.tx(o, lambda tx: gate.pacing(tx, o["task"])).binding, "related_unsettled")

    def test_a_later_call_that_did_not_reach_the_customer_keeps_it_pending(self):
        """お詫びのかけ直しに出なかった：前の通話は、まだ決まっていない（判定し直しの対象のまま。直れば判定し直す）。"""
        s = self.ready()
        a = self.cut_unknown(s)
        self.answered(s)
        b = self.call(s, ["dialing", "no_answer"])
        self.assertNotEqual(self.settle(s, b)["state"], "unknown")
        self.assertTrue(self.undecided(s, a))
        self.assertEqual(self.tx(s, lambda tx: settle.awaiting_rejudge(tx, s["task"])), a)
