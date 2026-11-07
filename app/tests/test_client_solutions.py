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
        （決めるのは client_solutions.keep_in の1か所。足すのは取込の入口の共通の1か所 business_import.preview_shaped で、AI に名前を探させる前＝
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
        offered = self.tx(s, lambda tx: client_solutions.not_in_line(tx, s["run"]))
        self.assertEqual([(r["solution_id"], r["answer_name"], r["solution_text"]) for r in offered],
                         [(first.solution_id, "料金", "月額1万円です。")])
        note = self.tx(s, lambda tx: tx.one("SELECT refs FROM salescall.notification_events WHERE kind = 'solution_earlier'"))
        self.assertEqual(note["refs"]["earlier"], [str(first.solution_id)])
        self.assertEqual([r["answer_name"] for r in self.tx(s, lambda tx: client_solutions.not_in_version(tx, first.version_id))],
                         ["解約"])   # 戻す前の流れ（もう使っていない）には、戻したあとの解決策が入っていない（入っているものは出さない）
        with self.assertRaises(client_solutions.SolutionInvalid):                              # 一覧に無いものは足さない
            self.tx(s, lambda tx: client_solutions.add_earlier(tx, s["run"], [got.solution_id]), OPERATOR)
        with self.assertRaises(Rejected):                                                       # 選んで足せるのは管理者か契約者だけ
            self.tx(s, lambda tx: client_solutions.add_earlier(tx, s["run"], [first.solution_id]), SYSTEM)
        for actor, chooser in ((SYSTEM, "salescall.cur_actor()"), (ADMIN, "'ほかの方'")):        # 選んだ方は、いま操作している管理者か契約者
            with self.subTest(actor=actor), self.assertRaises(Rejected) as e:
                self.tx(s, lambda tx: tx.execute(
                    "INSERT INTO salescall.client_solution_lines (tenant_id, client_id, solution_id, base_version, version_id, replaced,"
                    f" chosen_by) VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, %s, false, {chooser})",
                    (first.solution_id, got.version_id, got.version_id)), actor)
            self.assertEqual(e.exception.constraint, "client_solution_line_chosen_by_person")
        lines = self.tx(s, lambda tx: client_solutions.add_earlier(tx, s["run"], [first.solution_id]), ADMIN)
        self.assertEqual((len(lines), lines[0].base, lines[0].problems), (1, got.version_id, ()))
        answers = dict(self.tx(s, lambda tx: bs.load(tx, lines[0].version_id)).answers)
        self.assertEqual((answers["料金"], answers["解約"]), ("月額1万円です。", "いつでも解約できます。"))
        self.assertEqual(self.tx(s, lambda tx: client_solutions.not_in_line(tx, s["run"])), [])
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

    # ── 2026-11-07 の第三者の見直し (2)
    def with_tokai(self, s, *, info=(), answers=()):
        """前からある AI の読みの名前「東海」（業務の情報で話す）を持つ版を作って、実施回に割り当てる。info・answers＝足す業務の情報・問答。"""
        r = settings_raw()
        r["info"] = list(r.get("info") or []) + ["東海の工場で作っています。", *info]
        r["answers"] = list(r.get("answers") or []) + list(answers)
        r["names"] = list(r["names"]) + [{"text": "東海", "reading": "とうかい", "source": "script", "reading_by": "ai",
                                          "candidates": [], "likely": None}]
        v = self.tx(s, lambda tx: bs.save_version(tx, s["business"], r), OPERATOR)
        self.tx(s, lambda tx: bs.assign(tx, s["run"], v, reason="試験"), OPERATOR)
        return v

    def test_a_solution_looks_for_names_only_in_its_own_text_and_keeps_the_versions_names(self):
        """(2) の1：解決策で名前を探すのは、足す問答の文だけ。版にある名前と読みは版のまま（AI が「東海」を別の読みで返しても変えない）。
        名前の一覧は足した名前だけ増え、録音は変わった答えの部分（足した名前を話す、前からある答えも＝その名前の読みを持たない録音を
        引き継がない）だけを求める（引き継ぎ・知らせ・警告が働く）。⛔ 前は台本全体の名前を探し直し、版の名前の一覧が変わったので録音を
        引き継げず（録音の要る部分も知らせも出ない）、新しい版がいつまでも電話に使われなかった。"""
        from salescall.core import recordings
        from tests.test_business_import import finder
        s = self.ready()
        aid = self.called(s)
        v0 = self.with_tokai(s, answers=[{"name": "場所", "text": "本社は港区にございます。"}])
        self.w.approve_voices(s["tenant"], s["client"], v0, parts=("intro", "answer_場所"))
        voices = self.tx(s, lambda tx: recordings.ready_voices(tx, v0))
        self.assertTrue(voices)
        find = finder({"surface": "港区", "reading": "みなとく", "source": "モデル2つ", "candidates": [], "likely": None},
                      {"surface": "東海", "reading": "あずま", "source": "モデル2つ", "candidates": [], "likely": None})
        text = "東京都港区の本社でご説明します。"
        got = self.tx(s, lambda tx: client_solutions.add(tx, aid, 0, text, origin="dashboard", name_answer=self.namer("料金"),
                                                         find_names=find), OPERATOR)
        self.assertEqual(find.asked, [text])                                                   # 足す文だけ
        before = self.tx(s, lambda tx: bs.load(tx, v0)).raw["names"]
        after = self.tx(s, lambda tx: bs.load(tx, got.version_id)).raw["names"]
        self.assertEqual(after[:len(before)], before)                                          # 版の名前と読みは版のまま
        self.assertEqual([(n["text"], n["reading"]) for n in after[len(before):]], [("港区", "みなとく")])
        p = bs.ANSWER_PART_PREFIX
        want = {p + "料金": text, p + "場所": "本社は港区にございます。"}
        self.assertEqual(self.tx(s, lambda tx: recordings.needed_parts(tx, got.version_id)), {v: want for v in voices})
        self.assertTrue(self.tx(s, lambda tx: tx.one("SELECT 1 FROM salescall.notification_events WHERE event_key = %s",
                                                     (f"recording_needed:{got.version_id}",))))

    def test_a_name_the_solution_adds_that_other_text_speaks_keeps_no_recordings(self):
        """解決策が足した名前を、答えのほかの文（業務の情報・名乗り・質問など。部分と文の対応は会話の側）も話すなら、どの録音の部分が変わったかを
        決められないので、録音を引き継がない（core/recordings._changes の1か所。その名前の読みを持たない録音で話さない）。"""
        from salescall.core import recordings
        from tests.test_business_import import finder
        s = self.ready()
        aid = self.called(s)
        v0 = self.with_tokai(s, info=["港区の本社があります。"])
        self.w.approve_voices(s["tenant"], s["client"], v0)
        self.assertTrue(self.tx(s, lambda tx: recordings.ready_voices(tx, v0)))
        find = finder({"surface": "港区", "reading": "みなとく", "source": "モデル2つ", "candidates": [], "likely": None})
        got = self.tx(s, lambda tx: client_solutions.add(tx, aid, 0, "港区の本社でご説明します。", origin="dashboard",
                                                         name_answer=self.namer("料金"), find_names=find), OPERATOR)
        self.assertIn("港区", [n.text for n in self.tx(s, lambda tx: bs.load(tx, got.version_id)).names])
        self.assertEqual((self.tx(s, lambda tx: recordings.needed_parts(tx, got.version_id)),
                          self.tx(s, lambda tx: recordings.ready_voices(tx, got.version_id))), ({}, []))

    def test_a_solution_the_import_turned_off_is_replaced(self):
        """取り込み直した中身が「使わない」にした標準の問いの解決策も、取り込んだ中身が先（契約者が決めた）なので、置き換えられたと残す。"""
        from salescall.core import business_import
        from tests.test_versions import STD
        s = self.ready()
        aid = self.called(s)
        with self.w.standard(STD) as std:
            v = self.tx(s, lambda tx: bs.save_version(tx, s["business"], settings_raw(), standard_id=std), OPERATOR)
            self.tx(s, lambda tx: bs.assign(tx, s["run"], v, reason="試験：標準を使う版"), OPERATOR)
            got = self.tx(s, lambda tx: client_solutions.add(tx, aid, 0, "東京都港区の本社でご説明します。", origin="dashboard",
                                                             name_answer=self.namer("会社の場所")), OPERATOR)
            raw = dict(settings_raw(), answers_off=["会社の場所"])
            p = self.tx(s, lambda tx: business_import.preview_shaped(tx, s["business"], raw), OPERATOR)
            self.assertEqual(p.problems, [])
            vi = self.tx(s, lambda tx: business_import.register_shaped(tx, s["business"], raw, expected_digest=p.digest, ai=p.ai),
                         OPERATOR)
            replaced = self.tx(s, lambda tx: tx.all("SELECT solution_id, version_id FROM salescall.client_solution_replacements"))
            self.assertEqual([(r["solution_id"], r["version_id"]) for r in replaced], [(got.solution_id, vi)])

    def test_a_solution_in_verification_mode_does_not_ask_again_for_readings_already_in_the_version(self):
        """(2) の3：検証モードに切り替えたあとも、版に前からある AI の読みは確かめ直さない（置いたときの確かめを信じる）。解決策の文に
        新しい名前が無ければ、そのまま版ができる。"""
        s = self.ready()
        aid = self.called(s)
        self.with_tokai(s)
        self.put_client(s, "approval.human", True)
        got = self.tx(s, lambda tx: client_solutions.add(tx, aid, 0, "月額1万円です。", origin="dashboard",
                                                         name_answer=self.namer("料金")), OPERATOR)
        self.assertEqual(got.problems, ())
        self.assertIsNotNone(got.version_id)

    def test_a_solution_the_import_replaced_is_no_longer_offered(self):
        """(2) の4：取り込み直しで、取り込んだ中身が先になった解決策（同じ問いの名前の答えを書いた）は、置き換えられたと残し（1か所＝
        client_solutions.replaced。登録の共通の1か所 business_import.register_shaped が書く）、取り込みの答えの入った流れには、流れに入って
        いない前の解決策に出さず、知らせ solution_earlier も出さない（選んで取り込んだ中身を上書きしない。入っていない流れでは、解決策の古い文
        でなく取り込みの答えで出す＝0204）。そのあとの取り込み直しでも残さない。⛔ 前は出続け、毎回知らせた。"""
        from salescall.core import business_import
        from tests.test_business_import import book
        s = self.ready()
        aid = self.called(s)
        first = self.tx(s, lambda tx: client_solutions.add(tx, aid, 0, "月額1万円です。", origin="dashboard",
                                                           name_answer=self.namer("料金")), OPERATOR)
        rows = [("問いの名前", "答え", "標準を使わない"), ("料金", "料金は担当よりご案内します。", None)]
        data = book(sheets={"想定問答": rows})
        p = self.tx(s, lambda tx: business_import.preview(tx, s["business"], "業務設定.xlsx", data), OPERATOR)
        vi = self.tx(s, lambda tx: business_import.register(tx, s["business"], "業務設定.xlsx", data, expected_digest=p.digest,
                                                             ai=p.ai), OPERATOR)
        replaced = self.tx(s, lambda tx: tx.all("SELECT solution_id, version_id FROM salescall.client_solution_replacements"))
        self.assertEqual([(r["solution_id"], r["version_id"]) for r in replaced], [(first.solution_id, vi)])
        self.tx(s, lambda tx: bs.assign(tx, s["run"], vi, reason="試験：取り込み直した版"), OPERATOR)
        self.assertEqual(self.tx(s, lambda tx: client_solutions.not_in_line(tx, s["run"])), [])
        later = self.w.insert_claim(s, version=vi)
        for st in ("dialing", "ringing", "answered", "in_conversation", "ending", "ended"):
            self.w.move(s, later, st)
        point = call_report.NotAgreed(5, "Q1", "解約のときは？", 1, False, None, False, "解約")
        self.tx(s, lambda tx: call_report.save(tx, later, replace(make_report(), not_agreed=(point,))))
        self.tx(s, lambda tx: client_solutions.add(tx, later, 0, "いつでも解約できます。", origin="dashboard",
                                                   name_answer=self.namer("解約")), OPERATOR)
        self.assertEqual(self.tx(s, lambda tx: tx.all("SELECT 1 FROM salescall.notification_events WHERE kind = 'solution_earlier'")),
                         [])
        p = self.tx(s, lambda tx: business_import.preview(tx, s["business"], "業務設定.xlsx", book()), OPERATOR)
        self.assertNotIn("料金", {a["name"] for a in p.raw["answers"]})                       # 置き換えられた解決策は、よみがえらない
        self.assertIn("解約", {a["name"] for a in p.raw["answers"]})
        self.assertEqual(self.tx(s, lambda tx: client_solutions.kept_answers(tx, s["business"])), [("解約", "いつでも解約できます。")])
        with self.assertRaises(Rejected) as e:                                                # 置き換えた版は、その取引で取り込み直した版だけ
            self.tx(s, lambda tx: client_solutions.replaced(tx, [first.solution_id], s["version"]), OPERATOR)
        self.assertIn(e.exception.constraint, ("client_solution_replacement_version", "client_solution_replacements_pkey"))
        sid = self.tx(s, lambda tx: tx.one("SELECT solution_id FROM salescall.client_solutions WHERE answer_name = '解約'"))["solution_id"]
        with self.assertRaises(Rejected) as e:
            self.tx(s, lambda tx: client_solutions.replaced(tx, [sid], s["version"]), OPERATOR)
        self.assertEqual(e.exception.constraint, "client_solution_replacement_version")

    def test_a_held_line_does_not_offer_the_solution_just_added(self):
        """(2) の4：確かめに通らなかった流れ（検証モードで、解決策の文の新しい名前の AI の読みをまだ確かめていない）に、いま足した解決策
        そのものを「流れに入っていない前の解決策」として知らせない（足せなかったことは solution_held で知らせる）。"""
        from salescall.core import registry
        from tests.test_business_import import finder
        s = self.ready()
        self.put_client(s, "approval.human", True)
        aid = self.called(s)
        r2 = settings_raw()
        r2["names"] = list(r2["names"]) + [{"text": "港区", "reading": "みなとく", "source": "script", "reading_by": "given",
                                            "candidates": [], "likely": None}]
        r2["info"] = ["港区の本社があります。"]
        other = self.tx(s, lambda tx: bs.save_version(tx, s["business"], r2), OPERATOR)
        run = self.tx(s, lambda tx: registry.create_run(tx, s["business"], "港区の名前を持つ版の実施回", other, reason="試験"), OPERATOR)
        self.w.task(s["tenant"], s["client"], run, s["contact"])
        find = finder({"surface": "港区", "reading": "みなとく", "source": "モデル2つ", "candidates": [], "likely": None})
        got = self.tx(s, lambda tx: client_solutions.add(tx, aid, 0, "港区の本社でご説明します。", origin="dashboard",
                                                         name_answer=self.namer("料金"), find_names=find), OPERATOR)
        self.assertEqual([(line.base, line.version_id is None) for line in got.lines], [(s["version"], True), (other, False)])
        self.assertEqual(self.tx(s, lambda tx: tx.all("SELECT 1 FROM salescall.notification_events WHERE kind = 'solution_earlier'")),
                         [])

    def test_every_import_entry_keeps_the_solutions(self):
        """(2) の6：依頼者の解決策で足した問答を残すのは、取込のどの入口も通る共通の1か所（business_import.preview_shaped・register_shaped。
        形にしたあと、AI に聞く前）。ファイルの入口でも、形にした中身を渡す入口（画面の入力・方式C・資料一式）でも残り、プレビューと登録で
        同じ中身（指紋が合う）。⛔ 前はファイルの入口だけが残していた。"""
        from salescall.core import business_import
        from tests.test_business_import import book
        s = self.ready()
        aid = self.called(s)
        self.tx(s, lambda tx: client_solutions.add(tx, aid, 0, "月額1万円です。", origin="dashboard", name_answer=self.namer("料金")),
                OPERATOR)
        raw = settings_raw()
        p = self.tx(s, lambda tx: business_import.preview_shaped(tx, s["business"], raw), OPERATOR)
        self.assertIn({"name": "料金", "text": "月額1万円です。"}, p.raw["answers"])
        v = self.tx(s, lambda tx: business_import.register_shaped(tx, s["business"], raw, expected_digest=p.digest, ai=p.ai), OPERATOR)
        self.assertEqual(dict(self.tx(s, lambda tx: bs.load(tx, v)).answers)["料金"], "月額1万円です。")
        p = self.tx(s, lambda tx: business_import.preview(tx, s["business"], "業務設定.xlsx", book()), OPERATOR)
        v = self.tx(s, lambda tx: business_import.register(tx, s["business"], "業務設定.xlsx", book(), expected_digest=p.digest, ai=p.ai),
                    OPERATOR)
        self.assertEqual(dict(self.tx(s, lambda tx: bs.load(tx, v)).answers)["料金"], "月額1万円です。")

    def test_solutions_are_kept_only_for_their_own_business(self):
        """取り込み直しで残す解決策・流れに入っていない前の解決策は、その業務の解決策だけ（`_latest` の1か所が業務で絞る）。"""
        s = self.ready()
        aid = self.called(s)
        self.tx(s, lambda tx: client_solutions.add(tx, aid, 0, "月額1万円です。", origin="dashboard", name_answer=self.namer("料金")),
                OPERATOR)
        b2, v2 = self.w.business_with_version(s["tenant"], s["client"])
        self.assertEqual(self.tx(s, lambda tx: client_solutions.kept_answers(tx, s["business"])), [("料金", "月額1万円です。")])
        self.assertEqual(self.tx(s, lambda tx: client_solutions.kept_answers(tx, b2)), [])

    def test_a_solution_removed_by_hand_is_offered_but_not_put_back_by_an_import(self):
        """✅ 2026-11-07 ユーザー「契約者が確かめる」（計画書 6-1 の (3) が (2) より先）：契約者が手で前の版に戻して外した解決策（前の版の流れに
        入っていて、戻した版に入っていない＝client_solutions.removed_by_hand の1か所。0197）は、取り込み直しでも自動では入れない（keep_in。
        知らせつき）。流れに入っていない前の解決策の一覧には「手で外した」印つきで出し、契約者が選んで足せば、外したことは消える（そのあとの
        取り込み直しでは残す）。自動の割り当て・つながりの先の版で実施回を作るときは、外したことにしない（古い版で作るときは外す＝0200）。
        外した印・確かめは実施回ごと（0200）。"""
        from salescall.core import business_import
        from tests.test_business_import import book
        s = self.ready()
        aid = self.called(s)
        first = self.tx(s, lambda tx: client_solutions.add(tx, aid, 0, "月額1万円です。", origin="dashboard",
                                                           name_answer=self.namer("料金")), OPERATOR)
        self.tx(s, lambda tx: bs.assign(tx, s["run"], first.version_id, reason="試験：解決策の版にする"), OPERATOR)
        self.assertEqual(self.tx(s, lambda tx: tx.all("SELECT 1 FROM salescall.client_solution_removals")), [])   # 入れた方は外していない
        self.tx(s, lambda tx: bs.assign(tx, s["run"], s["version"], reason="試験：手で前の版に戻す"), OPERATOR)
        back = self.tx(s, lambda tx: tx.one("SELECT salescall.run_current_version(%s) AS v", (s["run"],))["v"])
        removed = self.tx(s, lambda tx: tx.all("SELECT solution_id, version_id FROM salescall.client_solution_removals"))
        self.assertEqual([(r["solution_id"], r["version_id"]) for r in removed], [(first.solution_id, back)])
        self.tx(s, lambda tx: bs.assign(tx, s["run"], s["version"], reason="試験：外したままの前の版へ、もう一度手で戻す"), OPERATOR)
        self.assertEqual(len(self.tx(s, lambda tx: tx.all("SELECT 1 FROM salescall.client_solution_removals"))), 1)   # 前の版にも無かった
        p = self.tx(s, lambda tx: business_import.preview(tx, s["business"], "業務設定.xlsx", book()), OPERATOR)
        self.assertNotIn("料金", {a["name"] for a in p.raw["answers"]})                       # 取り込み直しでも自動では入れない
        self.assertTrue(any("料金" in n and "外した" in n for n in p.notes), p.notes)
        self.assertEqual(self.tx(s, lambda tx: client_solutions.kept_answers(tx, s["business"])), [])
        offered = self.tx(s, lambda tx: client_solutions.not_in_line(tx, s["run"]))
        self.assertEqual([(r["solution_id"], r["removed"]) for r in offered], [(first.solution_id, True)])   # 一覧には出す
        self.tx(s, lambda tx: client_solutions.add_earlier(tx, s["run"], [first.solution_id]), OPERATOR)
        p = self.tx(s, lambda tx: business_import.preview(tx, s["business"], "業務設定.xlsx", book()), OPERATOR)
        self.assertIn({"name": "料金", "text": "月額1万円です。"}, p.raw["answers"])            # 選んで足したあとは残す
        with self.assertRaises(Rejected) as e:                                                # 書けるのは、その取引で手で割り当てたときだけ
            self.tx(s, lambda tx: tx.execute(
                "INSERT INTO salescall.client_solution_removals (tenant_id, client_id, solution_id, version_id, recorded_by)"
                " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, salescall.cur_actor())", (first.solution_id, back)),
                OPERATOR)
        self.assertEqual(e.exception.constraint, "client_solution_removal_by_assignment")

    # ── ✅ 2026-11-07 ユーザー（伺い「古い版で実施回を作ったとき、入っていない解決策を契約者が手で外したとみなすか」）：
    #    「みなす（契約者が承認をしないと自動で入らず、新規の電話もかけられません、ダッシュボードに承認ボタンを分かりやすく表示）」
    def other_row(self, s, run, version):
        """その実施回に、別の番号のお客様の行を1つ作り、予定をいまにする（まだ一度もかけていない行）。"""
        from salescall.core import next_action
        from salescall.core.phone import normalize
        from tests.support import fictional_jp_fixed
        phone = normalize(fictional_jp_fixed()).e164
        k = self.w.contact(s["tenant"], s["client"], phone)
        t = self.w.task(s["tenant"], s["client"], run, k)
        self.tx(s, lambda tx: next_action.schedule_now(tx, t, cause="試験"))
        return dict(s, run=run, task=t, contact=k, phone=phone, version=version)

    def old_run(self, s, name):
        """その業務に、解決策の入る前の版（s の最初の版＝つながりの先でない版）で実施回を作り、最初の段を開ける。（実施回, 割り当てた写し）。"""
        from salescall.core import quality_stage, registry
        run = self.tx(s, lambda tx: registry.create_run(tx, s["business"], name, s["version"], reason="試験：古い版で作る"), OPERATOR)
        v = self.tx(s, lambda tx: tx.one("SELECT salescall.run_current_version(%s) AS v", (run,))["v"])
        self.tx(s, lambda tx: quality_stage.open_stage(tx, v, 1, None, evidence="試験"), OPERATOR)
        return run, v

    def marks(self, s, run):
        return [(r["solution_id"], r["removed"], r["unconfirmed"]) for r in self.tx(s, lambda tx: client_solutions.not_in_line(tx, run))]

    def test_an_older_version_chosen_for_a_new_run_removes_the_newer_solutions_and_holds_new_rows_until_confirmed(self):
        """(1) 管理者か契約者が、実施回を作るときに、その業務のつながりの先でない版（解決策の入る前の古い版）を選んだら、その版の流れに
        入っていない解決策を、その実施回で「手で外した」とする（手で版を替えたときと同じ1か所＝client_solutions.removed_by_hand）。つながりの
        先の版で作ったときは外さない。契約者に知らせる（ダッシュボードの承認ボタンの元）。
        (2)(3) 契約者がその実施回で「入れる」（add_earlier）か「入れない」（leave_out）かを確かめるまで、その実施回では、お客様と約束した
        かけ直し・続きの電話・お詫びの電話のほかは電話をかけない（門の理由 solutions_unconfirmed。候補にも出さない。一度かけた行の
        ふつうのかけ直しも止める＝⛔ 2026-11-07 の見直し：前は、まだ一度もかけていない行だけを止めた）。「入れない」と確かめたら、門は
        すぐ通す（取り込み直しでも自動では入れないまま）。"""
        from salescall.core import attempts, business_import, gate, next_action, quality_stage, registry, scheduler
        from tests.test_business_import import book
        s = self.ready()
        aid = self.called(s)
        first = self.tx(s, lambda tx: client_solutions.add(tx, aid, 0, "月額1万円です。", origin="dashboard",
                                                           name_answer=self.namer("料金")), OPERATOR)
        sid = first.solution_id
        tip_run = self.tx(s, lambda tx: registry.create_run(tx, s["business"], "先の版の実施回", first.version_id, reason="試験"),
                          OPERATOR)
        self.assertEqual(self.tx(s, lambda tx: tx.all("SELECT 1 FROM salescall.client_solution_removals")), [])   # 先の版は外さない
        run, v = self.old_run(s, "古い版の実施回")
        self.assertNotEqual(v, s["version"])                                                   # 写し（つながりの先の無い版）
        removed = self.tx(s, lambda tx: tx.all("SELECT solution_id, version_id, run_id FROM salescall.client_solution_removals"))
        self.assertEqual([(r["solution_id"], r["version_id"], r["run_id"]) for r in removed], [(sid, v, run)])
        self.assertEqual(self.marks(s, run), [(sid, True, True)])
        self.assertEqual(self.marks(s, tip_run), [])                                           # 先の版の流れには入っている
        note = self.tx(s, lambda tx: tx.all("SELECT refs FROM salescall.notification_events WHERE kind = 'solution_unconfirmed'"))
        self.assertEqual([(n["refs"]["run_id"], n["refs"]["version_id"], n["refs"]["solutions"]) for n in note],
                         [(str(run), str(v), [str(sid)])])
        # 門：まだ一度もかけていない行は止める（候補にも出さない）。先の版の実施回は止めない
        new = self.other_row(s, run, v)
        self.assertEqual(self.reason(new), "solutions_unconfirmed")
        self.assertIn("ダッシュボード", gate.REASONS["solutions_unconfirmed"].message)
        self.assertEqual(self.tx(s, lambda tx: gate.waits_unknown(tx, new["task"])), "solutions_unconfirmed")
        self.assertNotIn(new["task"], [r["task_id"] for r in self.tx(s, lambda tx: scheduler.due_tasks(tx, 50))])
        on_tip = self.other_row(s, tip_run, first.version_id)
        self.assertIsNone(self.tx(s, lambda tx: gate.waits_unknown(tx, on_tip["task"])))
        called = self.other_row(s, run, v)
        self.past_attempt(called, ("dialing", "no_answer"))                                     # 一度かけた行のふつうのかけ直しも止める
        self.assertEqual(self.reason(called), "solutions_unconfirmed")
        # 止めないのは、お客様と約束したかけ直し・続きの電話・お詫びの電話（新しい電話でない＝salescall.is_new_call の1か所）だけ
        from tests import support
        for purpose, promised in (("normal", True), ("resume", True), ("apology", True)):
            with self.subTest(purpose=purpose):
                self.tx(s, lambda tx: support.schedule_call(tx, called["task"], tx.one("SELECT now() AS t")["t"],
                                                            contact_id=called["contact"], cause="試験", promised=promised,
                                                            purpose=purpose))
                self.assertFalse(self.tx(s, lambda tx: gate.unconfirmed_solutions(tx, {"task_id": called["task"], "run_id": run})))
        self.tx(s, lambda tx: next_action.schedule_now(tx, called["task"], cause="試験：ふつうの電話に戻す"))
        self.assertTrue(self.tx(s, lambda tx: gate.unconfirmed_solutions(tx, {"task_id": called["task"], "run_id": run})))
        # 「入れない」：その実施回の、まだ確かめていないものだけ・管理者か契約者だけ・その実施回で外した解決策だけ
        with self.assertRaises(client_solutions.SolutionInvalid):
            self.tx(s, lambda tx: client_solutions.leave_out(tx, run, [aid]), OPERATOR)
        with self.assertRaises(client_solutions.SolutionInvalid):                                # ほかの実施回の承認では受けない
            self.tx(s, lambda tx: client_solutions.leave_out(tx, tip_run, [sid]), OPERATOR)
        with self.assertRaises(Rejected) as e:
            self.tx(s, lambda tx: client_solutions.leave_out(tx, run, [sid]), SYSTEM)
        self.assertEqual(e.exception.constraint, "client_solution_leave_out_by_person")
        with self.assertRaises(Rejected) as e:
            self.tx(s, lambda tx: tx.execute(
                "INSERT INTO salescall.client_solution_leave_outs (tenant_id, client_id, solution_id, run_id, recorded_by)"
                " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, salescall.cur_actor())", (sid, tip_run)), OPERATOR)
        self.assertEqual(e.exception.constraint, "client_solution_leave_out_removed")
        self.tx(s, lambda tx: client_solutions.leave_out(tx, run, [sid]), OPERATOR)
        self.assertEqual(self.marks(s, run), [(sid, True, False)])                               # 一覧には残る（あとで「入れる」も押せる）
        got = self.claim(new)
        self.assertIsInstance(got, gate.Claim, got)                                              # 確かめたら、すぐ通す
        self.tx(s, lambda tx: attempts.release(tx, [got.attempt_id]))
        with self.assertRaises(client_solutions.SolutionInvalid):                                # もう確かめたものは、また「入れない」にしない
            self.tx(s, lambda tx: client_solutions.leave_out(tx, run, [sid]), OPERATOR)
        p = self.tx(s, lambda tx: business_import.preview(tx, s["business"], "業務設定.xlsx", book()), OPERATOR)
        self.assertNotIn("料金", {a["name"] for a in p.raw["answers"]})                         # 入れないまま（自動では入れない）
        # 同じ実施回で、もう一度外した（先の版へ手で替えて、また古い版へ）：その実施回で、また確かめが要る（いちばん新しい外した印のあと）
        self.tx(s, lambda tx: bs.assign(tx, run, first.version_id, reason="試験：先の版へ"), OPERATOR)
        self.tx(s, lambda tx: bs.assign(tx, run, s["version"], reason="試験：また古い版へ"), OPERATOR)
        again = self.tx(s, lambda tx: tx.one("SELECT salescall.run_current_version(%s) AS v", (run,))["v"])
        self.tx(s, lambda tx: quality_stage.open_stage(tx, again, 1, None, evidence="試験"), OPERATOR)
        self.assertEqual(self.marks(s, run), [(sid, True, True)])
        self.assertEqual(self.reason(new), "solutions_unconfirmed")                              # かけずに取り消した予約は、かけたことにならない

    def test_each_run_confirms_its_own_removed_solutions(self):
        """✅ 2026-11-07 本体の決定「確かめは実施回ごと」：外すのも確かめるのも、契約者が実施回ごとにする操作。
        (1) ほかの実施回でまた古い版を選んで外しても、もう確かめた実施回の電話は止まらない（⛔ 業務ごとの確かめでは、また止まった）。
        (2) ある実施回で「入れない」・「入れる」を選んでも、まだ選んでいないほかの実施回は、まだ確かめていないまま（⛔ 業務ごとの確かめでは、
        ほかの実施回まで確かめ済みになった＝契約者がしていない判断を作りが代わりにした）。
        取り込み直しは、まだ終えていない行のある実施回のどれかで外したままなら入れない（client_solutions.held_back の1か所）。"""
        from salescall.core import attempts, business_import, gate
        from tests.test_business_import import book
        s = self.ready()
        aid = self.called(s)
        sid = self.tx(s, lambda tx: client_solutions.add(tx, aid, 0, "月額1万円です。", origin="dashboard",
                                                         name_answer=self.namer("料金")), OPERATOR).solution_id
        run, v = self.old_run(s, "古い版の実施回1")
        row = self.other_row(s, run, v)
        self.tx(s, lambda tx: client_solutions.leave_out(tx, run, [sid]), OPERATOR)
        r2, v2 = self.old_run(s, "古い版の実施回2")
        r3, v3 = self.old_run(s, "古い版の実施回3")
        r4, v4 = self.old_run(s, "古い版の実施回4")
        rows = {r: self.other_row(s, r, x) for r, x in ((r2, v2), (r3, v3), (r4, v4))}
        self.assertEqual(self.marks(s, run), [(sid, True, False)])                               # (1) 確かめた実施回は、確かめたまま
        got = self.claim(row)
        self.assertIsInstance(got, gate.Claim, got)
        self.tx(s, lambda tx: attempts.release(tx, [got.attempt_id]))
        for r in (r2, r3, r4):
            self.assertEqual(self.marks(s, r), [(sid, True, True)])
            self.assertEqual(self.reason(rows[r]), "solutions_unconfirmed")
        self.tx(s, lambda tx: client_solutions.leave_out(tx, r2, [sid]), OPERATOR)               # (2) 「入れない」は、その実施回だけ
        self.assertEqual([self.marks(s, r) for r in (r2, r3, r4)], [[(sid, True, False)], [(sid, True, True)], [(sid, True, True)]])
        self.tx(s, lambda tx: client_solutions.add_earlier(tx, r3, [sid]), OPERATOR)             # (2) 「入れる」も、その実施回だけ
        self.assertEqual(self.marks(s, r3), [])                                                  # その実施回の流れに入った
        chosen = self.tx(s, lambda tx: tx.all("SELECT chosen_run FROM salescall.client_solution_lines WHERE solution_id = %s"
                                              " AND chosen_by IS NOT NULL", (sid,)))
        self.assertEqual([c["chosen_run"] for c in chosen], [r3])                                # どの実施回の承認かを残す
        self.assertEqual(self.marks(s, r4), [(sid, True, True)])
        self.assertEqual(self.reason(rows[r4]), "solutions_unconfirmed")
        self.assertIsNone(self.tx(s, lambda tx: gate.waits_unknown(tx, rows[r2]["task"])))
        with self.assertRaises(Rejected) as e:                                                   # 「入れる」を選んだ実施回は、その業務の実施回だけ
            b2, v_other = self.w.business_with_version(s["tenant"], s["client"])
            other = self.w.run(s["tenant"], s["client"], b2, v_other)
            self.tx(s, lambda tx: tx.execute(
                "INSERT INTO salescall.client_solution_lines (tenant_id, client_id, solution_id, base_version, version_id, replaced,"
                " chosen_by, chosen_run) VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, %s, false,"
                " salescall.cur_actor(), %s)", (sid, v4, v4, other)), OPERATOR)
        self.assertEqual(e.exception.constraint, "client_solution_line_chosen_run")

        def other_run_mark(tx):   # 外した印は、その取引で手で割り当てた実施回のものだけ
            bs.assign(tx, r4, s["version"], reason="試験：もう一度古い版")
            cur = tx.one("SELECT salescall.run_current_version(%s) AS v", (r4,))["v"]
            tx.execute("INSERT INTO salescall.client_solution_removals (tenant_id, client_id, solution_id, version_id, run_id, recorded_by)"
                       " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, %s, salescall.cur_actor())", (sid, cur, r2))
        with self.assertRaises(Rejected) as e:
            self.tx(s, other_run_mark, OPERATOR)
        self.assertEqual(e.exception.constraint, "client_solution_removal_by_assignment")
        p = self.tx(s, lambda tx: business_import.preview(tx, s["business"], "業務設定.xlsx", book()), OPERATOR)
        self.assertNotIn("料金", {a["name"] for a in p.raw["answers"]})                         # どれかの実施回で外したまま：入れない
        self.assertEqual(self.tx(s, lambda tx: client_solutions.held_back(tx, s["business"])), {sid})
        for r in (run, r2, r4):
            self.tx(s, lambda tx, r=r: client_solutions.add_earlier(tx, r, [sid]), OPERATOR)
        self.assertEqual(self.tx(s, lambda tx: client_solutions.held_back(tx, s["business"])), set())
        p = self.tx(s, lambda tx: business_import.preview(tx, s["business"], "業務設定.xlsx", book()), OPERATOR)
        self.assertIn({"name": "料金", "text": "月額1万円です。"}, p.raw["answers"])            # どの実施回でも入れた：残す

    def test_an_automatic_assignment_removes_nothing(self):
        """自動の割り当て（auto）は、つながりの中の古い版でも、手で外したことにしない（決めるのは client_solutions.removed_by_hand の1か所）。"""
        s = self.ready()
        aid = self.called(s)
        first = self.tx(s, lambda tx: client_solutions.add(tx, aid, 0, "月額1万円です。", origin="dashboard",
                                                           name_answer=self.namer("料金")), OPERATOR)
        self.tx(s, lambda tx: bs.assign(tx, s["run"], first.version_id, reason="試験：解決策の版にする"), OPERATOR)
        self.tx(s, lambda tx: bs.assign(tx, s["run"], s["version"], reason="自動：試験", auto=True))
        copy = self.tx(s, lambda tx: bs.save_from(tx, s["business"], s["version"]), OPERATOR)   # 解決策の入っていない、つながりの先の版
        self.tx(s, lambda tx: bs.assign(tx, s["run"], copy, reason="自動：試験", auto=True))
        self.assertEqual(self.tx(s, lambda tx: tx.all("SELECT 1 FROM salescall.client_solution_removals")), [])

    def cur_v(self, s, run):
        return self.tx(s, lambda tx: tx.one("SELECT salescall.run_current_version(%s) AS v", (run,))["v"])

    def reverted(self, s):
        """解決策を1つ足し、いまの実施回を手で前の版に戻す（写し C が割り当たる）。(解決策, 解決策の版, C)。"""
        aid = self.called(s)
        first = self.tx(s, lambda tx: client_solutions.add(tx, aid, 0, "月額1万円です。", origin="dashboard",
                                                           name_answer=self.namer("料金")), OPERATOR)
        self.tx(s, lambda tx: bs.assign(tx, s["run"], first.version_id, reason="試験：解決策の版"), OPERATOR)
        self.tx(s, lambda tx: bs.assign(tx, s["run"], s["version"], reason="試験：手で前の版に戻す"), OPERATOR)
        return first.solution_id, first.version_id, self.cur_v(s, s["run"])

    # ── 2026-11-07 の第三者の見直し (2) の5・6：契約者の取引が直接書いても、割り当ての決まりを通る守り／外しても試験が落ちなかった守り
    #    （0197・0200。その解決策の業務かは 0201 の salescall.solution_business の1か所）
    @staticmethod
    def removal(solution, version, run, by="salescall.cur_actor()"):
        return lambda tx: tx.execute(
            "INSERT INTO salescall.client_solution_removals (tenant_id, client_id, solution_id, version_id, run_id, recorded_by)"
            f" VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, %s, {by})", (solution, version, run))

    def rejected(self, s, work, actor, constraint):
        with self.assertRaises(Rejected) as e:
            self.tx(s, work, actor)
        self.assertEqual(e.exception.constraint, constraint)

    def test_a_removal_is_kept_only_with_a_hand_assignment_of_its_business_in_the_same_transaction(self):
        """手で外した印を書けるのは、その取引で、いま操作している方が、その実施回に、その解決策の業務の版を手で割り当てたときだけ（記録した方は
        いま操作している方）。4つの決まりを1つずつ確かめる（どれを外しても、ほかの決まりでは断られない場面）。"""
        from salescall.core import registry
        s = self.ready()
        sid, _, back = self.reverted(s)
        again = lambda tx: bs.assign(tx, s["run"], back, reason="試験：同じ版をもう一度手で")   # noqa: E731（前と同じ版：印は付かない）
        # 記録した方：その取引で手で割り当てても、記録した方が別の方なら断る
        self.rejected(s, lambda tx: (again(tx), self.removal(sid, back, s["run"], "'だれか別の方'")(tx)), OPERATOR,
                      "client_solution_removal_recorded_by")
        # その取引で割り当てたか：前の取引の割り当てでは書けない
        self.rejected(s, self.removal(sid, back, s["run"]), OPERATOR, "client_solution_removal_by_assignment")
        # 手の割り当てか：自動の割り当て（システムの処理）では書けない
        self.rejected(s, lambda tx: (bs.assign(tx, s["run"], back, reason="自動：試験", auto=True),
                                     self.removal(sid, back, s["run"])(tx)), SYSTEM, "client_solution_removal_by_assignment")
        # その解決策の業務の版か：ほかの業務の実施回に、その業務の版を手で割り当てた取引でも、その版への印は書けない
        b2, _ = self.w.business_with_version(s["tenant"], s["client"])
        v2 = self.tx(s, lambda tx: bs.save_version(tx, b2, settings_raw()), OPERATOR)
        self.rejected(s, lambda tx: self.removal(sid, v2, registry.create_run(tx, b2, "ほかの業務の実施回", v2, reason="試験"))(tx),
                      OPERATOR, "client_solution_removal_by_assignment")

    def test_a_replacement_and_a_leave_out_are_recorded_by_the_person_operating(self):
        """置き換えられた解決策（0197）と「入れない」（0200）の記録した方は、いま操作している方（ほかの決まりには当たる場面で、記録した方だけを
        変えて確かめる）。流れの結果の「入れる」の選んだ方と実施回は、そろって書く（CHECK client_solution_lines_chosen_run）。"""
        s = self.ready()
        sid, _, _ = self.reverted(s)
        self.assertEqual(self.marks(s, s["run"]), [(sid, True, True)])
        self.rejected(s, lambda tx: tx.execute(
            "INSERT INTO salescall.client_solution_replacements (tenant_id, client_id, solution_id, version_id, recorded_by)"
            " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, 'だれか別の方')",
            (sid, bs.save_version(tx, s["business"], settings_raw()))), OPERATOR, "client_solution_replacement_recorded_by")
        self.rejected(s, lambda tx: tx.execute(
            "INSERT INTO salescall.client_solution_leave_outs (tenant_id, client_id, solution_id, run_id, recorded_by)"
            " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, 'だれか別の方')", (sid, s["run"])), OPERATOR,
            "client_solution_leave_out_by_person")
        self.rejected(s, lambda tx: tx.execute(
            "INSERT INTO salescall.client_solution_lines (tenant_id, client_id, solution_id, base_version, version_id, replaced, chosen_by)"
            " SELECT salescall.cur_tenant(), salescall.cur_client(), %s, v, v, false, salescall.cur_actor()"
            " FROM (SELECT %s::uuid AS v) x", (sid, bs.save_version(tx, s["business"], settings_raw()))), OPERATOR,
            "client_solution_lines_chosen_run")

    def test_the_answer_of_a_version_is_read_like_the_settings(self):
        """解決策が版の流れに入っているかを比べる、版の答え（データベースの salescall.version_answer。0201）は、業務設定の重ね方
        （business_settings.load の answers＝業務の答え、無ければ「使わない」にしていない標準の答え）と同じ答え。"""
        from tests.test_versions import STD
        s = self.ready()
        with self.w.standard(STD) as std:
            plain = settings_raw()
            off = dict(settings_raw(), answers_off=["会社の場所"])
            own = dict(settings_raw(), answers=[{"name": "会社の場所", "text": "大阪の支店でご説明します。"},
                                                {"name": "料金", "text": "月額1万円です。"}])
            for raw, standard in ((plain, std), (off, std), (own, std), (own, None)):
                with self.subTest(off=raw.get("answers_off"), own=bool(raw["answers"]), standard=standard is not None):
                    v = self.tx(s, lambda tx: bs.save_version(tx, s["business"], raw, standard_id=standard), OPERATOR)
                    want = dict(self.tx(s, lambda tx: bs.load(tx, v)).answers)
                    names = sorted(set(want) | {"会社の場所", "料金", "無い問い"})
                    got = {n: self.tx(s, lambda tx, n=n: tx.one("SELECT salescall.version_answer(%s, %s) AS a", (v, n))["a"])
                           for n in names}
                    self.assertEqual(got, {n: want.get(n) for n in names})

    def test_a_hand_assignment_written_directly_goes_through_the_assignment_rules(self):
        """契約者の取引が実施回の版の割り当て（run_versions）へ直接書いても（見直し (2) の5）：つながりの途中の版は手で割り当てられない
        （写しを作る道＝business_settings.assign を通す）。つながりの先の版なら、手で外した印は、同じ取引でデータベースが付ける
        （salescall.hand_removals の1か所。⛔ 前は Python の割り当ての道だけが付けた）。"""
        s = self.ready()
        aid = self.called(s)
        first = self.tx(s, lambda tx: client_solutions.add(tx, aid, 0, "月額1万円です。", origin="dashboard",
                                                           name_answer=self.namer("料金")), OPERATOR)
        self.tx(s, lambda tx: bs.assign(tx, s["run"], first.version_id, reason="試験：解決策の版にする"), OPERATOR)
        put = ("INSERT INTO salescall.run_versions (tenant_id, client_id, run_id, business_id, version_id, assigned_by, reason)"
               " SELECT r.tenant_id, r.client_id, r.run_id, r.business_id, %s, salescall.cur_actor(), '直接' FROM salescall.runs r"
               " WHERE r.run_id = %s")
        self.rejected(s, lambda tx: tx.execute(put, (s["version"], s["run"])), OPERATOR, "run_version_manual_tip")   # 途中の版
        copy = self.tx(s, lambda tx: bs.save_from(tx, s["business"], s["version"]), OPERATOR)
        self.tx(s, lambda tx: tx.execute(put, (copy, s["run"])), OPERATOR)
        self.assertEqual(self.marks(s, s["run"]), [(first.solution_id, True, True)])

    def test_a_run_made_on_any_version_without_a_solution_marks_it_removed(self):
        """✅ 2026-11-07 ユーザー＝計画書 6-1 (6)：実施回を作るとき（管理者か契約者が選んだとき）は、その業務のいまの解決策のうち、割り当てた
        版に入っていないもの（client_solutions._outside の1か所）を、すべて「手で外した」とする。手で前の版に戻した写し（それ自身がつながりの
        先）・写しの写し・写しの先の版で作っても当たる。⛔ 2026-11-07 の見直し：前は選んだ版の「自分のつながりの先」とだけ比べ、写しで作った
        実施回には何も印を付けず、門も開いていた。"""
        from salescall.core import registry
        s = self.ready()
        sid, _, copy = self.reverted(s)
        self.assertEqual(self.tx(s, lambda tx: bs.successor_tip(tx, copy)), copy)
        on_copy = self.tx(s, lambda tx: registry.create_run(tx, s["business"], "写しで作った実施回", copy, reason="試験"), OPERATOR)
        self.assertEqual(self.marks(s, on_copy), [(sid, True, True)])
        copy2 = self.tx(s, lambda tx: bs.save_from(tx, s["business"], copy), OPERATOR)          # 写しの写し（どの実施回も使っていない）
        on_copy2 = self.tx(s, lambda tx: registry.create_run(tx, s["business"], "写しの写しで作った実施回", copy2, reason="試験"),
                           OPERATOR)
        self.assertEqual(self.cur_v(s, on_copy2), copy2)                                        # 使われていない先の版は、そのまま
        self.assertEqual(self.marks(s, on_copy2), [(sid, True, True)])
        self.assertEqual(self.tx(s, lambda tx: client_solutions.held_back(tx, s["business"])), {sid})

    def test_a_run_given_another_runs_version_without_a_solution_gets_its_own_line(self):
        """2026-11-07 のつなぎ目の見直し (A)：同じ版（解決策の入っていない版）を2つの実施回が使うと、片方の「入れる」で作った版が、もう
        片方の流れの先にもなり、もう片方の承認になった。⇒ 手で割り当てる版が、ほかのまだ終えていない実施回のいまの版で、解決策が
        入っていなければ、写しを作って流れを分ける（business_settings.assign の `_shared_apart` の1か所）。片方の「入れる」は、もう片方の
        一覧・門・自動の割り当てに効かない。解決策がすべて入っている版は分けない。"""
        from salescall.core import gate, quality_stage, registry
        s = self.ready()
        sid, solved, copy = self.reverted(s)
        self.other_row(s, s["run"], copy)
        whole = self.tx(s, lambda tx: registry.create_run(tx, s["business"], "解決策の入った版の実施回", solved, reason="試験"),
                        OPERATOR)
        self.assertEqual(self.cur_v(s, whole), solved)                                          # 入っている版は分けない
        r2 = self.tx(s, lambda tx: registry.create_run(tx, s["business"], "R2", copy, reason="試験：R1 の版を選ぶ"), OPERATOR)
        c2 = self.cur_v(s, r2)
        self.assertNotEqual(c2, copy)                                                           # 写しを作って流れを分けた
        self.tx(s, lambda tx: quality_stage.open_stage(tx, c2, 1, None, evidence="試験"), OPERATOR)
        row2 = self.other_row(s, r2, c2)
        self.assertEqual((self.marks(s, s["run"]), self.marks(s, r2)), ([(sid, True, True)], [(sid, True, True)]))
        self.tx(s, lambda tx: client_solutions.add_earlier(tx, s["run"], [sid]), OPERATOR)      # R1 だけ「入れる」
        self.assertEqual((self.marks(s, s["run"]), self.marks(s, r2)), ([], [(sid, True, True)]))
        self.assertEqual(self.reason(row2), "solutions_unconfirmed")
        self.tx(s, lambda tx: bs.adopt_ready(tx, r2))
        self.assertEqual(self.cur_v(s, r2), c2)                                                 # R1 の「入れる」の版へは替わらない
        self.assertIsInstance(self.claim(row2), gate.Denied)

    def test_a_run_without_rows_yet_holds_its_removed_solutions_back_from_an_import(self):
        """2026-11-07 のつなぎ目の見直し (D)：取り込み直しで入れない解決策は、版のある実施回すべて（終えた実施回＝行がすべて終わった実施回
        だけ除く）で外したもの。行がまだ0の実施回は終えていない（まだ終えていない実施回の1か所＝business_settings._versions_in_use）。"""
        s = self.ready()
        aid = self.called(s)
        sid = self.tx(s, lambda tx: client_solutions.add(tx, aid, 0, "月額1万円です。", origin="dashboard",
                                                         name_answer=self.namer("料金")), OPERATOR).solution_id
        run, _ = self.old_run(s, "行の無い古い版の実施回")
        self.assertIn(run, self.tx(s, lambda tx: bs.in_use_runs(tx, s["business"])))
        self.assertEqual(self.tx(s, lambda tx: client_solutions.held_back(tx, s["business"])), {sid})

    def test_the_notice_of_earlier_solutions_names_the_runs_of_the_line(self):
        """2026-11-07 のつなぎ目の見直し (C)：知らせ solution_earlier に、その版の流れを使う実施回（まだ終えていない実施回）の並びを入れる
        （確かめは実施回ごと。画面が実施回ごとの入口を呼べるように）。"""
        s = self.ready()
        sid, _, copy = self.reverted(s)
        self.other_row(s, s["run"], copy)
        now = dict(s, version=copy)                                                               # いまの版（写し）でかけた通話
        aid = self.w.insert_claim(now)
        for st in ("dialing", "ringing", "answered", "in_conversation", "ending", "ended"):
            self.w.move(now, aid, st)
        later = call_report.NotAgreed(5, "Q1", "解約のときは？", 1, False, None, False, "解約")
        self.tx(s, lambda tx: call_report.save(tx, aid, replace(make_report(), not_agreed=(later,))))
        self.tx(s, lambda tx: client_solutions.add(tx, aid, 0, "いつでも解約できます。", origin="dashboard",
                                                   name_answer=self.namer("解約")), OPERATOR)
        notes = self.tx(s, lambda tx: tx.all("SELECT refs FROM salescall.notification_events WHERE kind = 'solution_earlier'"))
        self.assertEqual([(n["refs"]["runs"], n["refs"]["earlier"]) for n in notes], [([str(s["run"])], [str(sid)])])

    def test_an_approved_recording_of_the_same_text_is_used_again(self):
        """✅ 計画書 6-1 (4)・6-2「承認は録音の中身に付け」（0172）：要る部分ごとに、同じ業務・同じ声の録音から、部分の名前・文・読みが同じで
        承認済みのものを使う（recordings._approved_same の1か所。needed_parts・complete・carry・知らせが同じ答えを読む）。⛔ 2026-11-07 の
        見直し：前は版の元をたどったいちばん近い版の録音しか見ず、手で戻す前の版で録音して承認した同じ文を、録り直させた。"""
        from salescall.core import recordings
        s = self.ready()
        aid = self.called(s)
        first = self.tx(s, lambda tx: client_solutions.add(tx, aid, 0, "月額1万円です。", origin="dashboard",
                                                           name_answer=self.namer("料金")), OPERATOR)
        need = self.tx(s, lambda tx: recordings.needed_parts(tx, first.version_id))
        self.assertTrue(need)
        sets = [self.tx(s, lambda tx, voice=voice, parts=parts: recordings.complete(tx, first.version_id, voice, [
            recordings.Part(p, t, f"test://{voice}/{p}", "1" * 64) for p, t in parts.items()]), OPERATOR) for voice, parts in need.items()]

        def put_back_and_choose_in():
            self.tx(s, lambda tx: bs.assign(tx, s["run"], first.version_id, reason="試験：解決策の版"), OPERATOR)
            self.tx(s, lambda tx: bs.assign(tx, s["run"], s["version"], reason="試験：手で前の版に戻す"), OPERATOR)
            return self.tx(s, lambda tx: client_solutions.add_earlier(tx, s["run"], [first.solution_id]), OPERATOR)[0].version_id
        # まだ承認していない録音は使わない（録音していただく）
        self.assertEqual(self.tx(s, lambda tx, v=put_back_and_choose_in(): recordings.needed_parts(tx, v)), need)
        for set_id in sets:
            self.tx(s, lambda tx, set_id=set_id: recordings.approve(tx, set_id), OPERATOR)
        made = put_back_and_choose_in()
        self.assertEqual(self.tx(s, lambda tx: recordings.needed_parts(tx, made)), {})
        self.assertEqual(self.tx(s, lambda tx: recordings.ready_voices(tx, made)),
                         self.tx(s, lambda tx: recordings.ready_voices(tx, first.version_id)))
        notes = self.tx(s, lambda tx: tx.all("SELECT 1 FROM salescall.notification_events WHERE kind = 'recording_needed'"
                                             " AND refs ->> 'version_id' = %s", (str(made),)))
        self.assertEqual(notes, [])

    # ── 0204（✅ 計画書 6-1 (5)(6) にそろえる＝本体 CL-2 の決定）：取り込み直しで置き換えた解決策も、置き換えた取り込みの答えとして、
    #    手で外した実施回の確かめに数える。⛔ 前は、置き換えた解決策を一覧から除いたので、取り込み直しで確かめ待ちが消え、門が通った
    def imported(self, s, answers):
        """業務設定を取り込み直す（想定問答だけを answers＝[(問いの名前, 答え)] にしたファイル）。登録した版を返す。"""
        from salescall.core import business_import
        from tests.test_business_import import book
        data = book(sheets={"想定問答": [("問いの名前", "答え")] + list(answers)})
        p = self.tx(s, lambda tx: business_import.preview(tx, s["business"], "業務設定.xlsx", data), OPERATOR)
        self.assertEqual(p.problems, [])
        return self.tx(s, lambda tx: business_import.register(tx, s["business"], "業務設定.xlsx", data, expected_digest=p.digest,
                                                              ai=p.ai), OPERATOR)

    def offered(self, s, run):
        """その実施回の確かめの一覧：[(解決策, 文, 置き換えた取り込みの答えか, 手で外した, まだ確かめていない)]。"""
        return [(r["solution_id"], r["solution_text"], r["replaced"], r["removed"], r["unconfirmed"])
                for r in self.tx(s, lambda tx: client_solutions.not_in_line(tx, run))]

    def test_an_import_that_replaces_a_removed_solution_still_waits_for_the_contractor(self):
        """場面 S（第三者の見直し）：解決策「料金」を足し、実施回 R1 を手で前の版に戻した（写し C。承認待ち）。契約者が取り込み直し、取り込んだ
        中身に「料金」の答えを書いた。R1 は写し C のまま（取り込み直しで R1 の版を自動で替えない＝6-1 (5)）なので、確かめの一覧に、置き換えた
        取り込みの答えで残り、門は止めたまま（6-1 (6)）。「入れる」を選ぶと、R1 の流れに入るのは取り込みの答え（解決策の古い文で上書きしない）
        で、録音が要るのは、その答えの部分だけ（6-1 (4)）。選んだら、門はすぐ通す。"""
        from salescall.core import gate, quality_stage, recordings
        s = self.ready()
        sid, _, copy = self.reverted(s)
        self.tx(s, lambda tx: quality_stage.open_stage(tx, copy, 1, None, evidence="試験"), OPERATOR)
        row = self.other_row(s, s["run"], copy)
        self.assertEqual(self.reason(row), "solutions_unconfirmed")
        vi = self.imported(s, [("料金", "料金は担当よりご案内します。")])
        replaced = self.tx(s, lambda tx: tx.all("SELECT solution_id, version_id FROM salescall.client_solution_replacements"))
        self.assertEqual([(r["solution_id"], r["version_id"]) for r in replaced], [(sid, vi)])
        self.tx(s, lambda tx: bs.adopt_ready(tx, s["run"]))
        self.assertEqual(self.cur_v(s, s["run"]), copy)                                          # R1 の版は替わらない
        self.assertEqual(self.offered(s, s["run"]), [(sid, "料金は担当よりご案内します。", True, True, True)])
        self.assertEqual(self.reason(row), "solutions_unconfirmed")                              # 門は止めたまま
        line, = self.tx(s, lambda tx: client_solutions.add_earlier(tx, s["run"], [sid]), OPERATOR)
        self.assertEqual(line.base, copy)
        self.assertEqual(dict(self.tx(s, lambda tx: bs.load(tx, line.version_id)).answers)["料金"], "料金は担当よりご案内します。")
        need = self.tx(s, lambda tx: recordings.needed_parts(tx, line.version_id))
        self.assertTrue(need)
        for voice, parts in need.items():                                                        # 録音が要るのは、入れた答えの部分だけ
            self.assertEqual(list(parts.values()), ["料金は担当よりご案内します。"], voice)
        self.assertEqual(self.offered(s, s["run"]), [])
        got = self.claim(row)
        self.assertIsInstance(got, gate.Claim, got)

    def test_the_confirmation_list_follows_the_latest_import_and_drops_a_question_the_import_no_longer_answers(self):
        """取り込み直すたびに置き換えを残し（0204 (2)）、確かめの一覧は、いちばん新しく置き換えた取り込みの答えを出す（⛔ 前は1つの解決策に
        置き換えは1つだけ）。同じ答えの取り込み直しでは置き換えを足さない。置き換えた答えは取り込みの中身なので、あとの取り込み直しに無ければ
        解決策として残さず（よみがえらせない）、入れる中身が無いので一覧にも出さない。「入れない」と確かめたら門を通し、そのあとの取り込み直しで
        答えが変わっても、また止めない。"""
        from salescall.core import gate
        s = self.ready()
        sid, solved, copy = self.reverted(s)
        v1 = self.imported(s, [("料金", "料金は担当よりご案内します。")])
        v2 = self.imported(s, [("料金", "料金は月額1万2千円です。")])
        replaced = self.tx(s, lambda tx: tx.all("SELECT solution_id, version_id FROM salescall.client_solution_replacements ORDER BY seq"))
        self.assertEqual([(r["solution_id"], r["version_id"]) for r in replaced], [(sid, v1), (sid, v2)])
        self.assertEqual(self.offered(s, s["run"]), [(sid, "料金は月額1万2千円です。", True, True, True)])
        self.imported(s, [("料金", "料金は月額1万2千円です。")])                                   # 同じ答え：置き換えを足さない
        self.assertEqual(len(self.tx(s, lambda tx: tx.all("SELECT 1 FROM salescall.client_solution_replacements"))), 2)
        self.tx(s, lambda tx: client_solutions.leave_out(tx, s["run"], [sid]), OPERATOR)
        self.assertEqual(self.offered(s, s["run"]), [(sid, "料金は月額1万2千円です。", True, True, False)])
        self.imported(s, [("料金", "料金は月額1万5千円です。")])
        self.assertEqual(self.offered(s, s["run"]), [(sid, "料金は月額1万5千円です。", True, True, False)])   # 確かめたまま
        self.assertIsNone(self.tx(s, lambda tx: gate.waits_unknown(tx, s["task"])))
        v4 = self.imported(s, [])                                                                 # 「料金」を消した取り込み直し
        self.assertNotIn("料金", dict(self.tx(s, lambda tx: bs.load(tx, v4)).answers))             # よみがえらせない
        self.assertEqual(self.offered(s, s["run"]), [])
        self.assertEqual(self.tx(s, lambda tx: client_solutions.not_in_version(tx, solved)), [])    # 答えのある版にも、空の中身を出さない
        self.assertEqual(self.tx(s, lambda tx: client_solutions.kept_answers(tx, s["business"])), [])

    def test_a_run_that_chose_to_put_a_solution_in_is_not_held_again_when_an_import_changes_it(self):
        """手で外した解決策を、その実施回で「入れる」と選んだあと、取り込み直しで答えが変わっても、その実施回は承認待ちに戻さない（「入れない」と
        同じく確かめ済み＝client_solutions._marks の1か所。⛔ 0204 の前は「入れる」を、流れに入って一覧に出ないことでしか数えなかった）。
        一覧には新しい答えで出るので、また「入れる」を選べる。"""
        s = self.ready()
        sid, _, _ = self.reverted(s)
        self.tx(s, lambda tx: client_solutions.add_earlier(tx, s["run"], [sid]), OPERATOR)
        self.assertEqual(self.offered(s, s["run"]), [])
        self.imported(s, [("料金", "料金は担当よりご案内します。")])
        self.assertEqual(self.offered(s, s["run"]), [(sid, "料金は担当よりご案内します。", True, True, False)])
        self.assertEqual(self.tx(s, lambda tx: client_solutions.unconfirmed(tx, s["run"])), [])
        self.tx(s, lambda tx: client_solutions.add_earlier(tx, s["run"], [sid]), OPERATOR)
        self.assertEqual(self.offered(s, s["run"]), [])

    def test_an_import_that_renames_or_repeats_a_removed_solution_still_waits_for_the_contractor(self):
        """取り込み直しの前後で同じ形の抜けが無いか：手で外した解決策の問いを、取り込み直した中身が (1) 別の名前で書いた（名前が変わった＝
        「料金」は無い。手で外したので、解決策も残さない＝6-1 (5)）(2) 解決策と同じ答えで書いた、どちらでも、置き換えではなく、実施回の版は
        写しのまま（中身が入っていない）なので、解決策の文のまま一覧に出し、承認待ちのまま。"""
        from salescall.core import quality_stage
        s = self.ready()
        sid, _, copy = self.reverted(s)
        self.tx(s, lambda tx: quality_stage.open_stage(tx, copy, 1, None, evidence="試験"), OPERATOR)
        row = self.other_row(s, s["run"], copy)
        for answers, kept in (([("料金のご案内", "料金は担当よりご案内します。")], None), ([("料金", "月額1万円です。")], "月額1万円です。")):
            with self.subTest(answers=answers):
                v = self.imported(s, answers)
                self.assertEqual(dict(self.tx(s, lambda tx: bs.load(tx, v)).answers).get("料金"), kept)
                self.assertEqual(self.tx(s, lambda tx: tx.all("SELECT 1 FROM salescall.client_solution_replacements")), [])
                self.assertEqual(self.cur_v(s, s["run"]), copy)
                self.assertEqual(self.offered(s, s["run"]), [(sid, "月額1万円です。", False, True, True)])
                self.assertEqual(self.reason(row), "solutions_unconfirmed")

    def test_a_hand_assignment_after_an_import_removes_the_import_answer_that_replaced_a_solution(self):
        """手で外した印（データベースの hand_removals）も、置き換えた取り込みの答えで数える（同じ1か所 business_solutions を読む）：取り込み直した
        版から、取り込みの答えの入っていない版へ手で戻した実施回・古い版で作った実施回は、承認待ちになる（6-1 (6)）。"""
        from salescall.core import registry
        s = self.ready()
        aid = self.called(s)
        sid = self.tx(s, lambda tx: client_solutions.add(tx, aid, 0, "月額1万円です。", origin="dashboard",
                                                         name_answer=self.namer("料金")), OPERATOR).solution_id
        vi = self.imported(s, [("料金", "料金は担当よりご案内します。")])
        self.tx(s, lambda tx: bs.assign(tx, s["run"], vi, reason="試験：取り込み直した版"), OPERATOR)
        self.assertEqual(self.offered(s, s["run"]), [])                                          # 取り込みの答えが入っている
        self.tx(s, lambda tx: bs.assign(tx, s["run"], s["version"], reason="試験：手で前の版に戻す"), OPERATOR)
        self.assertEqual(self.offered(s, s["run"]), [(sid, "料金は担当よりご案内します。", True, True, True)])
        old = self.tx(s, lambda tx: registry.create_run(tx, s["business"], "古い版の実施回", s["version"], reason="試験"), OPERATOR)
        self.assertEqual(self.offered(s, old), [(sid, "料金は担当よりご案内します。", True, True, True)])
