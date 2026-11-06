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
        # 足せなかった解決策は、取り込み直しても残さない（client_solutions.kept_answers＝足せた流れがあるものだけ）
        self.assertEqual(self.tx(s, lambda tx: client_solutions.kept_answers(tx, s["business"])), [])
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
        r2 = settings_raw()
        r2["answers"] = list(r2.get("answers") or []) + [{"name": "別の実施回だけの問い", "text": "別の実施回だけの答えです。"}]
        other = self.tx(s, lambda tx: bs.save_version(tx, s["business"], r2), OPERATOR)
        run = self.tx(s, lambda tx: registry.create_run(tx, s["business"], "取り込み直した版の実施回", other, reason="試験"), OPERATOR)
        self.w.task(s["tenant"], s["client"], run, s["contact"])
        # 同じ依頼者のほかの業務の実施回には足さない（使っている版を決めるのは business_settings.in_use_versions の1か所）
        b2, v2 = self.w.business_with_version(s["tenant"], s["client"])
        self.w.task(s["tenant"], s["client"], self.w.run(s["tenant"], s["client"], b2, v2), s["contact"])
        namer = self.namer("料金")
        got = self.tx(s, lambda tx: client_solutions.add(tx, aid, 0, "月額1万円です。", origin="dashboard",
                                                         name_answer=namer), OPERATOR)
        self.assertEqual([line.base for line in got.lines], [s["version"], other])   # 先頭は電話のあった実施回の流れ
        # 問いの名前は、電話のあった実施回の流れのいまの答えと比べて決める（ほかの流れの答えではない）
        self.assertEqual(namer.seen[0][2], [a for a, _ in self.tx(s, lambda tx: bs.load(tx, s["version"])).answers])
        self.assertEqual(self.tx(s, lambda tx: bs.successor_tip(tx, v2)), v2)
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
        """✅ 2026-11-06 ユーザー「残す」：取り込み直したファイルに、解決策で足した問いが無ければ残す（知らせつき）。同じ問いがあれば
        取り込んだ中身が先・ファイルが「使わない」にした問いは戻さない・足せなかった解決策は残さない・同じ問いの解決策が2つなら新しい方
        （決めるのは client_solutions.keep_in の1か所。足すのはファイルの入口 business_import._from_file で、AI に名前を探させる前＝
        残した文の名前も洗い出す。⛔ 2026-11-06 の2回目の見直し (3) の A-1）。"""
        from salescall.core import business_import
        from tests.test_business_import import book, finder
        from tests.test_versions import STD
        s = self.ready()
        aid = self.called(s)
        add = lambda text, name: self.tx(s, lambda tx: client_solutions.add(   # noqa: E731
            tx, aid, 0, text, origin="dashboard", name_answer=self.namer(name)), OPERATOR)
        with self.w.standard(STD):
            add("月額1万円です。", "料金")
            add("架空工業の製品と同じ料金です。", "料金")                       # 同じ問いの2つ目（同じ取引でも、足した順で新しい方）
            add("東京都港区の本社でご説明します。", "会社の場所")
            find = finder()
            p = self.tx(s, lambda tx: business_import.preview(tx, s["business"], "業務設定.xlsx", book(), find_names=find), OPERATOR)
            self.assertIn({"name": "料金", "text": "架空工業の製品と同じ料金です。"}, p.raw["answers"])
            self.assertTrue(any("料金" in n for n in p.notes), p.notes)
            self.assertTrue(any("架空工業の製品と同じ料金です。" in t for t in find.asked))   # 残した文も名前を探す
            rows = [("問いの名前", "答え", "標準を使わない"), ("料金", "料金は担当よりご案内します。", None), ("会社の場所", None, "はい")]
            p = self.tx(s, lambda tx: business_import.preview(tx, s["business"], "業務設定.xlsx", book(sheets={"想定問答": rows}),
                                                               find_names=finder()), OPERATOR)
            self.assertEqual([a["text"] for a in p.raw["answers"] if a["name"] == "料金"], ["料金は担当よりご案内します。"])
            self.assertNotIn("会社の場所", {a["name"] for a in p.raw["answers"]})    # ファイルが「使わない」にした問いは戻さない

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
        # 流れの版は、その解決策の業務の版で、足した版は足す元の版かそのつながりの先だけ（0192）。記録の時刻と並びはデータベースが決める
        b2, v2 = self.w.business_with_version(s["tenant"], s["client"])
        for base, made in ((v2, v2), (got.version_id, s["version"])):
            with self.subTest(base=base, made=made), self.assertRaises(Rejected) as e:
                self.tx(s, lambda tx: tx.execute(
                    "INSERT INTO salescall.client_solution_lines (tenant_id, client_id, solution_id, base_version, version_id, replaced)"
                    " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, %s, false)",
                    (got.solution_id, base, made)), OPERATOR)
            self.assertEqual(e.exception.constraint, "client_solution_line_versions")
        sid = self.tx(s, lambda tx: tx.one(
            "INSERT INTO salescall.client_solutions (tenant_id, client_id, attempt_id, task_id, point_index, point_text,"
            " solution_text, answer_name, origin, recorded_by, recorded_at) VALUES (salescall.cur_tenant(), salescall.cur_client(), %s,"
            " %s, 0, '料金', 'x', '料金', 'dashboard', salescall.cur_actor(), '2999-01-01') RETURNING recorded_at, seq",
            (aid, s["task"])), OPERATOR)
        self.assertLess(sid["recorded_at"].year, 2999)
        self.assertGreater(sid["seq"], self.tx(s, lambda tx: tx.one("SELECT seq FROM salescall.client_solutions WHERE solution_id = %s",
                                                                    (got.solution_id,)))["seq"])

    def test_earlier_solutions_left_out_of_a_line_put_back_by_hand_are_offered_and_added_only_by_choice(self):
        """✅ 2026-11-06 ユーザー＝計画書 6-1 (3)：契約者が手で前の版に戻した流れに、あとで別の解決策が来たとき、戻す前に足した解決策も
        入れるかは「契約者にそれをダッシュボードで確認します。」⇒ 自動では入れない。その流れに入っていない前の解決策の一覧（not_in_line）を
        出し、契約者に知らせ（solution_earlier）、契約者が選んだものだけを、新しい解決策と同じ道（_add_line）で足す（add_earlier）。"""
        s = self.ready()
        aid = self.called(s)
        first = self.tx(s, lambda tx: client_solutions.add(tx, aid, 0, "月額1万円です。", origin="dashboard",
                                                           name_answer=self.namer("料金")), OPERATOR)
        self.tx(s, lambda tx: bs.assign(tx, s["run"], first.version_id, reason="試験：解決策の版にする"), OPERATOR)
        self.tx(s, lambda tx: bs.assign(tx, s["run"], s["version"], reason="試験：手で前の版に戻す"), OPERATOR)
        back = self.tx(s, lambda tx: tx.one("SELECT salescall.run_current_version(%s) AS v", (s["run"],))["v"])
        self.assertNotIn("料金", dict(self.tx(s, lambda tx: bs.load(tx, back)).answers))
        later = self.w.insert_claim(s, version=back)
        for st in ("dialing", "ringing", "answered", "in_conversation", "ending", "ended"):
            self.w.move(s, later, st)
        point = call_report.NotAgreed(5, "Q1", "解約のときは？", 1, False, None, False, "解約")
        self.tx(s, lambda tx: call_report.save(tx, later, replace(make_report(), not_agreed=(point,))))
        got = self.tx(s, lambda tx: client_solutions.add(tx, later, 0, "いつでも解約できます。", origin="dashboard",
                                                         name_answer=self.namer("解約")), OPERATOR)
        self.assertEqual([line.base for line in got.lines], [back])
        answers = dict(self.tx(s, lambda tx: bs.load(tx, got.version_id)).answers)
        self.assertTrue("解約" in answers and "料金" not in answers, answers)                # 自動では入れない
        offered = self.tx(s, lambda tx: client_solutions.not_in_line(tx, back))
        self.assertEqual([(r["solution_id"], r["answer_name"], r["solution_text"]) for r in offered],
                         [(first.solution_id, "料金", "月額1万円です。")])
        note = self.tx(s, lambda tx: tx.one("SELECT refs FROM salescall.notification_events WHERE kind = 'solution_earlier'"))
        self.assertEqual(note["refs"]["earlier"], [str(first.solution_id)])
        self.assertEqual([r["answer_name"] for r in self.tx(s, lambda tx: client_solutions.not_in_line(tx, first.version_id))],
                         ["解約"])   # 戻す前の流れ（もう使っていない）には、戻したあとの解決策が入っていない（入っているものは出さない）
        with self.assertRaises(client_solutions.SolutionInvalid):                              # 一覧に無いものは足さない
            self.tx(s, lambda tx: client_solutions.add_earlier(tx, back, [got.solution_id]), OPERATOR)
        with self.assertRaises(Rejected):                                                       # 選んで足せるのは管理者か契約者だけ
            self.tx(s, lambda tx: client_solutions.add_earlier(tx, back, [first.solution_id]), SYSTEM)
        for actor, chooser in ((SYSTEM, "salescall.cur_actor()"), (ADMIN, "'ほかの方'")):        # 選んだ方は、いま操作している管理者か契約者
            with self.subTest(actor=actor), self.assertRaises(Rejected) as e:
                self.tx(s, lambda tx: tx.execute(
                    "INSERT INTO salescall.client_solution_lines (tenant_id, client_id, solution_id, base_version, version_id, replaced,"
                    f" chosen_by) VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, %s, false, {chooser})",
                    (first.solution_id, got.version_id, got.version_id)), actor)
            self.assertEqual(e.exception.constraint, "client_solution_line_chosen_by_person")
        lines = self.tx(s, lambda tx: client_solutions.add_earlier(tx, back, [first.solution_id]), ADMIN)
        self.assertEqual((len(lines), lines[0].base, lines[0].problems), (1, got.version_id, ()))
        answers = dict(self.tx(s, lambda tx: bs.load(tx, lines[0].version_id)).answers)
        self.assertEqual((answers["料金"], answers["解約"]), ("月額1万円です。", "いつでも解約できます。"))
        self.assertEqual(self.tx(s, lambda tx: client_solutions.not_in_line(tx, back)), [])
        chosen = self.tx(s, lambda tx: tx.one("SELECT chosen_by FROM salescall.client_solution_lines WHERE solution_id = %s"
                                              " AND base_version = %s", (first.solution_id, got.version_id)))["chosen_by"]
        self.assertTrue(chosen)

    def test_a_run_with_every_row_ended_takes_no_solution(self):
        """行をすべて終えた実施回は「使っている版」に入らない（解決策も足さない。まだ終えていない行か＝salescall.task_open の1か所＝0192）。"""
        from salescall.core import next_action, registry
        s = self.ready()
        aid = self.called(s)
        r2 = settings_raw()
        r2["answers"] = list(r2.get("answers") or []) + [{"name": "別の実施回だけの問い", "text": "別の実施回だけの答えです。"}]
        other = self.tx(s, lambda tx: bs.save_version(tx, s["business"], r2), OPERATOR)
        run = self.tx(s, lambda tx: registry.create_run(tx, s["business"], "終えた実施回", other, reason="試験"), OPERATOR)
        t2 = self.w.task(s["tenant"], s["client"], run, s["contact"])
        self.tx(s, lambda tx: next_action.end(tx, t2, "completed", cause="試験：終えた"))
        got = self.tx(s, lambda tx: client_solutions.add(tx, aid, 0, "月額1万円です。", origin="dashboard",
                                                         name_answer=self.namer("料金")), OPERATOR)
        self.assertEqual([line.base for line in got.lines], [s["version"]])
        self.assertTrue(self.tx(s, lambda tx: tx.one("SELECT salescall.task_open(%s) AS o", (s["task"],))["o"]))
        self.assertFalse(self.tx(s, lambda tx: tx.one("SELECT salescall.task_open(%s) AS o", (t2,))["o"]))
