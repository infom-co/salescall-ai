"""依頼者からの解決策を、その業務の想定問答に足す（(h)。core/client_solutions.py・0184。DESIGN.md 3-8-3）。

✅ 2026-11-02 5時56分 ユーザー：「依頼者から、解決策の連絡がフィードバックされたら、こちらのトークスクリプトに追加する。」
"""

from __future__ import annotations

from dataclasses import replace

from salescall.core import business_settings as bs
from salescall.core import call_report, client_solutions
from salescall.core.access import Rejected
from tests.support import OPERATOR, SYSTEM, make_report
from tests.test_gate import Base

POINTS = (call_report.NotAgreed(4, "Q1", "料金のところがちょっと。", 1, False, None, False, "料金"),
          call_report.NotAgreed(6, "Q1", "納期は？", 1, True, None, False, "納期"))


class Solutions(Base):
    def called(self, s):
        """その行で話し終えた通話を1本（ご納得いただけなかった点が2つ：料金＝ご説明できなかった・納期＝ご説明できた）。"""
        aid = self.w.insert_claim(s)
        for st in ("dialing", "ringing", "answered", "in_conversation", "ending", "ended"):
            self.w.move(s, aid, st)
        self.tx(s, lambda tx: call_report.save(tx, aid, replace(make_report(), not_agreed=POINTS)))
        return aid

    @staticmethod
    def namer(name):
        seen = []

        def name_answer(point, text, answers):
            seen.append((point, text, [a for a, _ in answers]))
            return name
        name_answer.seen = seen
        return name_answer

    def test_a_solution_to_an_unexplained_point_becomes_an_answer_in_a_new_version(self):
        """ご説明できなかった点に返った解決策を、AI が決めた問いの名前で、今の版のつながりの先の新しい版の想定問答に足す（きっかけ
        solution）。同じ問いの名前が返れば、答えを置き換える。どの点にどの解決策を足したかが残る。"""
        s = self.ready()
        aid = self.called(s)
        self.assertEqual(self.tx(s, lambda tx: client_solutions.points(tx, aid)), [{"index": 0, "point": "料金"}])
        first = self.namer("料金")
        got = self.tx(s, lambda tx: client_solutions.add(tx, aid, 0, "月額1万円で、初めの月は無料です。", origin="dashboard",
                                                         name_answer=first), OPERATOR)
        self.assertEqual((got.answer_name, got.replaced, got.problems), ("料金", False, ()))
        self.assertEqual(first.seen[0][:2], ("料金", "月額1万円で、初めの月は無料です。"))
        self.assertEqual(self.tx(s, lambda tx: bs.successor_tip(tx, s["version"])), got.version_id)
        self.assertIn(("料金", "月額1万円で、初めの月は無料です。"), self.tx(s, lambda tx: bs.load(tx, got.version_id)).answers)
        again = self.tx(s, lambda tx: client_solutions.add(tx, aid, 0, "月額1万円です。", origin="report_file",
                                                           name_answer=self.namer("料金")), OPERATOR)
        self.assertTrue(again.replaced)
        answers = dict(self.tx(s, lambda tx: bs.load(tx, again.version_id)).answers)
        self.assertEqual(answers["料金"], "月額1万円です。")
        self.assertEqual(self.tx(s, lambda tx: bs.successor_tip(tx, s["version"])), again.version_id)   # 列の先から作る
        rows = self.tx(s, lambda tx: tx.all("SELECT point_text, answer_name, origin, version_id FROM salescall.client_solutions"
                                            " WHERE attempt_id = %s ORDER BY recorded_at", (aid,)))
        self.assertEqual([(r["point_text"], r["origin"], r["version_id"]) for r in rows],
                         [("料金", "dashboard", got.version_id), ("料金", "report_file", again.version_id)])

    def test_a_point_told_again_in_a_later_call_is_not_asked_for_twice(self):
        """続きの電話で申し伝え直した点（同じ質問・同じ点）は、前の通話でもう解決策を求めているので、あとの通話では求めない（1つの行で
        1回。⛔ 2026-11-06 の見直し (C) の2）。別の点は求める。"""
        s = self.ready()
        first = self.called(s)
        later = self.w.insert_claim(s)
        for st in ("dialing", "ringing", "answered", "in_conversation", "ending", "ended"):
            self.w.move(s, later, st)
        again = replace(POINTS[0], turn=2, outcome="unresolved", told="request")
        new = call_report.NotAgreed(5, "Q1", "解約のときは？", 1, False, None, False, "解約")
        self.tx(s, lambda tx: call_report.save(tx, later, replace(make_report(), not_agreed=(again, new))))
        self.assertEqual(self.tx(s, lambda tx: client_solutions.points(tx, first)), [{"index": 0, "point": "料金"}])
        self.assertEqual(self.tx(s, lambda tx: client_solutions.points(tx, later)), [{"index": 1, "point": "解約"}])

    def test_only_unexplained_points_take_a_solution_and_only_a_person_adds_it(self):
        """解決策を受けるのは、ご説明できなかった点だけ（ご説明できた点・無い点・空の文は受けない）。足せるのは管理者か契約者だけ。"""
        s = self.ready()
        aid = self.called(s)
        for index, text in ((1, "納期は2週間です。"), (5, "x"), (0, "　")):
            with self.subTest(index=index), self.assertRaises(client_solutions.SolutionInvalid):
                self.tx(s, lambda tx: client_solutions.add(tx, aid, index, text, origin="dashboard",
                                                           name_answer=self.namer("料金")), OPERATOR)
        with self.assertRaises(Rejected):
            self.tx(s, lambda tx: client_solutions.add(tx, aid, 0, "月額1万円です。", origin="dashboard",
                                                       name_answer=self.namer("料金")), SYSTEM)

    def test_a_solution_that_needs_the_contractor_to_confirm_a_reading_is_held(self):
        """取込と同じ確かめに通らなければ（検証モードで、解決策の文の名前の AI の読みを契約者が確かめていない）、版を作らずに理由を
        残し、契約者に知らせる。契約者が読みを確かめてから同じ1か所でもう一度足せば、版ができる。"""
        s = self.ready()
        self.put_client(s, "approval.human", True)
        aid = self.called(s)
        finder = lambda text: ([{"surface": "架空工業", "reading": "かくうこうぎょう", "source": "モデル2つ", "candidates": []}]   # noqa: E731
                               if "架空工業" in text else [])
        held = self.tx(s, lambda tx: client_solutions.add(tx, aid, 0, "架空工業の製品と同じ料金です。", origin="dashboard",
                                                          name_answer=self.namer("料金"), find_names=finder), OPERATOR)
        self.assertIsNone(held.version_id)
        self.assertTrue(held.problems)
        notes = self.tx(s, lambda tx: tx.all("SELECT 1 FROM salescall.notification_events WHERE kind = 'solution_held'"))
        self.assertEqual(len(notes), 1)
        ok = self.tx(s, lambda tx: client_solutions.add(tx, aid, 0, "架空工業の製品と同じ料金です。", origin="dashboard",
                                                        name_answer=self.namer("料金"), find_names=finder,
                                                        confirmed_names=[("架空工業", "かくうこうぎょう")]), OPERATOR)
        self.assertIsNotNone(ok.version_id)
