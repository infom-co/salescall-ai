"""依頼者からの解決策を、その業務の想定問答に足す（(h)。core/client_solutions.py・0184。DESIGN.md 3-8-3）。

✅ 2026-11-02 5時56分 ユーザー：「依頼者から、解決策の連絡がフィードバックされたら、こちらのトークスクリプトに追加する。」
"""

from __future__ import annotations

from dataclasses import replace

from salescall.core import business_settings as bs
from salescall.core import call_report, client_solutions
from salescall.core.access import Rejected
from tests.support import ADMIN, OPERATOR, SYSTEM, make_report
from tests.test_business_settings import raw as settings_raw
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
        rows = self.tx(s, lambda tx: tx.all("SELECT c.point_text, c.origin, l.version_id FROM salescall.client_solutions c"
                                            " JOIN salescall.client_solution_lines l ON l.solution_id = c.solution_id"
                                            " WHERE c.attempt_id = %s ORDER BY c.recorded_at", (aid,)))
        self.assertEqual([(r["point_text"], r["origin"], r["version_id"]) for r in rows],
                         [("料金", "dashboard", got.version_id), ("料金", "report_file", again.version_id)])
        cause = self.tx(s, lambda tx: tx.one("SELECT cause FROM salescall.version_successions WHERE to_version = %s",
                                             (got.version_id,))["cause"])
        self.assertEqual(cause, "solution")

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

    def test_a_solution_reaches_every_running_line_of_the_business(self):
        """✅ 2026-11-06 ユーザー「業務のすべての実施回」：同じ業務のまだ終えていない実施回が別々の版を使っていれば、どの版の流れにも
        同じ問いの名前で足す（流れごとに版と結果が残る。同じ版を使う実施回は1つの版）。"""
        from salescall.core import registry
        s = self.ready()
        aid = self.called(s)
        other = self.tx(s, lambda tx: bs.save_version(tx, s["business"], settings_raw()), OPERATOR)
        run = self.tx(s, lambda tx: registry.create_run(tx, s["business"], "取り込み直した版の実施回", other, reason="試験"), OPERATOR)
        self.w.task(s["tenant"], s["client"], run, s["contact"])
        got = self.tx(s, lambda tx: client_solutions.add(tx, aid, 0, "月額1万円です。", origin="dashboard",
                                                         name_answer=self.namer("料金")), OPERATOR)
        self.assertEqual([line.base for line in got.lines], [s["version"], other])   # 先頭は電話のあった実施回の流れ
        for line in got.lines:
            self.assertEqual(line.problems, ())
            self.assertIn(("料金", "月額1万円です。"), self.tx(s, lambda tx, v=line.version_id: bs.load(tx, v)).answers)
            self.assertEqual(self.tx(s, lambda tx, b=line.base: bs.successor_tip(tx, b)), line.version_id)

    def test_the_same_solution_again_makes_no_new_version(self):
        """同じ中身になる解決策は、新しい版を作らない（記録は残し、足した版はいまのつながりの先のまま＝見直し(B) の8）。"""
        s = self.ready()
        aid = self.called(s)
        first = self.tx(s, lambda tx: client_solutions.add(tx, aid, 0, "月額1万円です。", origin="dashboard",
                                                           name_answer=self.namer("料金")), OPERATOR)
        again = self.tx(s, lambda tx: client_solutions.add(tx, aid, 0, "月額1万円です。", origin="report_file",
                                                           name_answer=self.namer("料金")), OPERATOR)
        self.assertEqual((again.version_id, again.replaced), (first.version_id, True))
        self.assertEqual(self.tx(s, lambda tx: bs.successor_tip(tx, s["version"])), first.version_id)

    def test_a_solution_keeps_the_standard_of_its_version(self):
        """解決策の版は、元の版の標準のまま（標準を使わない版なら標準を重ねない。標準を替えるのは derive_for_standard の1か所＝見直し(B) の2）。
        業務が「使わない」にした標準の問いに解決策が来たら、使わないを外して業務の答えにする。"""
        from tests.test_versions import STD
        s = self.ready()
        aid = self.called(s)
        with self.w.standard(STD) as std:
            none = self.tx(s, lambda tx: bs.save_version(tx, s["business"], settings_raw(), standard_id=None), OPERATOR)
            self.tx(s, lambda tx: bs.assign(tx, s["run"], none, reason="試験：標準を使わない版"), OPERATOR)
            got = self.tx(s, lambda tx: client_solutions.add(tx, aid, 0, "月額1万円です。", origin="dashboard",
                                                             name_answer=self.namer("料金")), OPERATOR)
            made = self.tx(s, lambda tx: bs.load(tx, got.version_id))
            self.assertIsNone(made.standard_id)
            self.assertNotIn("会社の場所", {a for a, _ in made.answers})
            r = settings_raw()
            r["answers_off"] = ["会社の場所"]
            off = self.tx(s, lambda tx: bs.save_version(tx, s["business"], r, standard_id=std), OPERATOR)
            self.tx(s, lambda tx: bs.assign(tx, s["run"], off, reason="試験：標準の問いを使わない版"), OPERATOR)
            got = self.tx(s, lambda tx: client_solutions.add(tx, aid, 0, "東京都港区の本社でご説明します。", origin="dashboard",
                                                             name_answer=self.namer("会社の場所")), OPERATOR)
            made = self.tx(s, lambda tx: bs.load(tx, got.version_id))
            self.assertEqual((made.standard_id, dict(made.answers)["会社の場所"]), (std, "東京都港区の本社でご説明します。"))
            self.assertNotIn("会社の場所", made.raw.get("answers_off") or [])

    def test_solution_answers_are_kept_when_the_settings_are_imported_again(self):
        """✅ 2026-11-06 ユーザー「残す」：取り込み直した中身に、解決策で足した問いが無ければ残す（知らせつき）。同じ問いがあれば取り込んだ
        中身が先（決めるのは client_solutions.keep_in の1か所。取込の確かめ business_import.review が通す）。"""
        from salescall.core import business_import
        s = self.ready()
        aid = self.called(s)
        self.tx(s, lambda tx: client_solutions.add(tx, aid, 0, "月額1万円です。", origin="dashboard",
                                                   name_answer=self.namer("料金")), OPERATOR)
        p = self.tx(s, lambda tx: business_import.review(tx, s["business"], settings_raw()), OPERATOR)
        self.assertIn({"name": "料金", "text": "月額1万円です。"}, p.raw["answers"])
        self.assertTrue(any("料金" in n for n in p.notes), p.notes)
        r = settings_raw()
        r["answers"] = list(r.get("answers") or []) + [{"name": "料金", "text": "料金は担当よりご案内します。"}]
        p = self.tx(s, lambda tx: business_import.review(tx, s["business"], r), OPERATOR)
        self.assertEqual([a["text"] for a in p.raw["answers"] if a["name"] == "料金"], ["料金は担当よりご案内します。"])

    def test_the_database_keeps_the_line_results_whole(self):
        """流れごとの結果は、足した版があるときだけ理由が空（0189 の CHECK）。記録した方は、いま操作している方（0184）。"""
        s = self.ready()
        aid = self.called(s)
        got = self.tx(s, lambda tx: client_solutions.add(tx, aid, 0, "月額1万円です。", origin="dashboard",
                                                         name_answer=self.namer("料金")), OPERATOR)
        with self.assertRaises(Rejected):
            self.tx(s, lambda tx: tx.execute(
                "INSERT INTO salescall.client_solution_lines (tenant_id, client_id, solution_id, base_version, version_id, replaced, problems)"
                " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, NULL, false, '{}')",
                (got.solution_id, got.version_id)), OPERATOR)
        with self.assertRaises(Rejected) as e:
            self.tx(s, lambda tx: tx.execute(
                "INSERT INTO salescall.client_solutions (tenant_id, client_id, attempt_id, task_id, point_index, point_text,"
                " solution_text, answer_name, origin, recorded_by) VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, 0,"
                " '料金', 'x', '料金', 'dashboard', 'ほかの方')", (aid, s["task"])), OPERATOR)
        self.assertEqual(e.exception.constraint, "client_solution_recorded_by")
        with self.assertRaises(Rejected) as e:                                             # 流れの結果は、足した方だけが書く
            self.tx(s, lambda tx: tx.execute(
                "INSERT INTO salescall.client_solution_lines (tenant_id, client_id, solution_id, base_version, version_id, replaced)"
                " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, %s, false)",
                (got.solution_id, got.version_id, got.version_id)), ADMIN)
        self.assertEqual(e.exception.constraint, "client_solution_line_by_recorder")
