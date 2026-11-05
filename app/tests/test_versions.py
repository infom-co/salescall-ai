"""版を足して、そろったら自動で替える1か所（business_settings.derive・derive_for_standard・adopt_ready。0183。DESIGN.md 3-8-3 の (5)）。

✅ 2026-11-02 管理者：(h) の流れ「新しい版の録音が承認されるまでは、今の版のまま電話を続けます。承認されたら、その業務の実施回を自動で
新しい版に替えます。電話は止めません。」・伺い2「案1でOK」（同じ文・読み・声の録音は使い回す）。
"""

from __future__ import annotations

from salescall.core import business_settings as bs
from salescall.core import quality_stage
from salescall.core.access import Rejected
from tests.support import OPERATOR
from tests.test_business_settings import raw as settings_raw
from tests.test_gate import Base
from tests.test_quality import GOOD

STD = (("番号の出どころ", "以前お問い合わせいただいた際のお電話番号にお掛けしております。"),
       ("会社の場所", "東京都港区にございます。"))


class Versions(Base):
    def add_answer(self, raw):
        raw["answers"] = list(raw.get("answers") or []) + [{"name": "駐車場", "text": "建物の裏に3台分ございます。"}]
        return raw

    def current(self, s):
        return self.tx(s, lambda tx: tx.one("SELECT salescall.run_current_version(%s) AS v", (s["run"],))["v"])

    def test_a_derived_version_is_adopted_only_when_it_can_speak_without_stopping_calls(self):
        """後の版は、(1) 今の版で話してよい声がすべてそろい (2) 品質の段で止まらずに始められるようになってから、実施回に自動で割り当てる。
        それまでは今の版のまま（電話を止めない）。割り当ての記録には「自動」と、何を変えたかが残る。"""
        s = self.ready()
        v0 = s["version"]
        self.w.approve_voices(s["tenant"], s["client"], v0)
        v1 = self.tx(s, lambda tx: bs.derive(tx, v0, self.add_answer, cause="solution", note="駐車場の問いを足した"), OPERATOR)
        self.assertEqual(self.tx(s, lambda tx: bs.adopt_ready(tx)), [])                        # 後の版の録音がまだ無い
        self.w.approve_voices(s["tenant"], s["client"], v1)
        self.assertEqual(self.tx(s, lambda tx: bs.adopt_ready(tx)), [])                        # 模擬通話の合格がまだ（止まりうる）
        self.assertEqual(self.tx(s, lambda tx: quality_stage.start_kind(tx, v1)), "mock_needed")
        self.assertEqual(self.current(s), v0)
        self.tx(s, lambda tx: quality_stage.record_mock(tx, v0, GOOD), OPERATOR)                # この依頼者は模擬通話に合格済み
        self.assertEqual(self.tx(s, lambda tx: bs.adopt_ready(tx)), [(s["run"], v1)])
        self.assertEqual(self.current(s), v1)
        why = self.tx(s, lambda tx: tx.one("SELECT reason FROM salescall.run_versions WHERE run_id = %s ORDER BY seq DESC LIMIT 1",
                                           (s["run"],))["reason"])
        self.assertTrue(why.startswith("自動：") and "駐車場の問いを足した" in why, why)
        self.assertEqual(self.tx(s, lambda tx: bs.adopt_ready(tx)), [])                        # もう後の版は無い

    def test_derivations_form_one_line_and_the_newest_ready_one_is_adopted(self):
        """版を足すのは、いつもつながりの先から（まだ割り当てていない後の版の変更を失わない）。つながりは1本（1つの版の後の版は1つ。
        データベースが守る）。後の版がいくつも続けば、話せるものの中でいちばん後の版を割り当てる。"""
        s = self.ready()
        v0 = s["version"]
        self.w.approve_voices(s["tenant"], s["client"], v0)
        self.tx(s, lambda tx: quality_stage.record_mock(tx, v0, GOOD), OPERATOR)
        v1 = self.tx(s, lambda tx: bs.derive(tx, v0, self.add_answer, cause="solution", note="駐車場"), OPERATOR)
        v2 = self.tx(s, lambda tx: bs.derive(tx, v0, lambda r: dict(r, answers=list(r["answers"]) + [
            {"name": "営業時間", "text": "平日の9時から18時でございます。"}]), cause="solution", note="営業時間"), OPERATOR)
        self.assertEqual(self.tx(s, lambda tx: bs.successor_tip(tx, v0)), v2)
        names = [a[0] for a in self.tx(s, lambda tx: bs.load(tx, v2)).answers]
        self.assertTrue({"駐車場", "営業時間"} <= set(names), names)                            # v1 の変更も入っている
        other = self.tx(s, lambda tx: bs.save_version(tx, s["business"], settings_raw()), OPERATOR)   # つながりの無い版
        with self.assertRaises(Rejected):                                                      # 1つの版の後の版は1つだけ
            self.tx(s, lambda tx: tx.execute(
                "INSERT INTO salescall.version_successions (tenant_id, client_id, business_id, from_version, to_version, cause, note,"
                " recorded_by) VALUES (%s, %s, %s, %s, %s, 'solution', 'x', 'x')",
                (s["tenant"], s["client"], s["business"], v0, other)), OPERATOR)
        self.w.approve_voices(s["tenant"], s["client"], v1)
        self.assertEqual(self.tx(s, lambda tx: bs.adopt_ready(tx)), [(s["run"], v1)])         # v2 はまだ録音が無い：v1 まで
        self.w.approve_voices(s["tenant"], s["client"], v2)
        self.assertEqual(self.tx(s, lambda tx: bs.adopt_ready(tx)), [(s["run"], v2)])

    def test_a_new_standard_makes_a_new_version_for_the_businesses_that_use_it(self):
        """管理者が標準の想定問答を新しくしたら、使っている業務の版（まだ終えていない行のある実施回の今の版）のつながりの先に、いまの
        標準に合わせた版を作る（1回だけ）。標準を使わない版には作らない。そろえば自動で替える（標準の録音の組も要る）。"""
        s = self.ready()
        self.tx(s, lambda tx: quality_stage.record_mock(tx, s["version"], GOOD), OPERATOR)
        with self.w.standard(STD, voices=("Leda", "Sadachbia")) as first:
            v = self.tx(s, lambda tx: bs.save_version(tx, s["business"], settings_raw(), standard_id=first), OPERATOR)
            self.tx(s, lambda tx: bs.assign(tx, s["run"], v, reason="試験"), OPERATOR)
            self.w.approve_voices(s["tenant"], s["client"], v)
            with self.w.standard(STD[:1], voices=("Leda", "Sadachbia")) as second:
                made = self.tx(s, lambda tx: bs.derive_for_standard(tx))
                self.assertEqual(len(made), 1)
                self.assertEqual(self.tx(s, lambda tx: bs.load(tx, made[0])).standard_id, second)
                self.assertEqual(self.tx(s, lambda tx: bs.derive_for_standard(tx)), [])       # もう作ってある
                self.w.approve_voices(s["tenant"], s["client"], made[0])
                self.assertEqual(self.tx(s, lambda tx: bs.adopt_ready(tx)), [(s["run"], made[0])])
                cause = self.tx(s, lambda tx: tx.one("SELECT cause FROM salescall.version_successions WHERE to_version = %s",
                                                     (made[0],))["cause"])
                self.assertEqual(cause, "standard")
            none = self.tx(s, lambda tx: bs.save_version(tx, s["business"], settings_raw(), standard_id=None), OPERATOR)
            self.tx(s, lambda tx: bs.assign(tx, s["run"], none, reason="試験：標準を使わない版"), OPERATOR)
            self.assertEqual(self.tx(s, lambda tx: bs.derive_for_standard(tx)), [])           # 標準を使わない版には作らない

    def test_a_standard_that_does_not_fit_the_business_is_told_and_calls_go_on(self):
        """新しい標準が業務の設定と合わず（業務が「使わない」にした問いが新しい標準に無い など）新しい版を作れなければ、契約者に知らせ
        （同じ版と標準で1回）、今の版のまま電話を続ける。"""
        s = self.ready()
        with self.w.standard(STD) as first:
            r = settings_raw()
            r["answers_off"] = ["会社の場所"]
            v = self.tx(s, lambda tx: bs.save_version(tx, s["business"], r, standard_id=first), OPERATOR)
            self.tx(s, lambda tx: bs.assign(tx, s["run"], v, reason="試験"), OPERATOR)
            with self.w.standard(STD[:1]):
                for _ in range(2):
                    self.assertEqual(self.tx(s, lambda tx: bs.derive_for_standard(tx)), [])
                notes = self.tx(s, lambda tx: tx.all("SELECT summary FROM salescall.notification_events WHERE kind = 'version_not_derived'"))
                self.assertEqual(len(notes), 1)
                self.assertEqual(self.current(s), v)

    def test_a_later_version_with_fewer_voices_or_unfit_rows_is_not_adopted(self):
        """今の版で話してよい声が後の版で減る・後の版がこの実施回の行を呼べない（目的のお客様の名前の列が無い）なら、割り当てない
        （今の版のまま。割り当てられない理由は assign と同じ1か所＝assign_problems）。"""
        s = self.ready()
        v0 = s["version"]
        self.w.approve_voices(s["tenant"], s["client"], v0, ("Leda", "Sadachbia"))
        self.tx(s, lambda tx: quality_stage.record_mock(tx, v0, GOOD), OPERATOR)
        v1 = self.tx(s, lambda tx: bs.derive(tx, v0, self.add_answer, cause="solution", note="駐車場"), OPERATOR)
        self.w.approve_voices(s["tenant"], s["client"], v1, ("Leda",))
        self.assertEqual(self.tx(s, lambda tx: bs.adopt_ready(tx)), [])                        # Sadachbia が減る
        self.w.approve_voices(s["tenant"], s["client"], v1, ("Sadachbia",))
        v2 = self.tx(s, lambda tx: bs.derive(tx, v0, lambda r: dict(r, target_person={"kind": "name", "role": None}),
                                             cause="override", note="お名前でお呼びする"), OPERATOR)
        self.w.approve_voices(s["tenant"], s["client"], v2, ("Leda", "Sadachbia"))
        self.assertTrue(self.tx(s, lambda tx: bs.assign_problems(tx, s["run"], v2)))
        self.assertEqual(self.tx(s, lambda tx: bs.adopt_ready(tx)), [(s["run"], v1)])         # v2 は呼べない：v1 まで

    def test_when_several_later_versions_are_ready_the_newest_is_adopted(self):
        """後の版が2つとも話せるようになっていれば、いちばん後の版を割り当てる（前の版を飛ばす）。"""
        s = self.ready()
        v0 = s["version"]
        self.w.approve_voices(s["tenant"], s["client"], v0)
        self.tx(s, lambda tx: quality_stage.record_mock(tx, v0, GOOD), OPERATOR)
        v1 = self.tx(s, lambda tx: bs.derive(tx, v0, self.add_answer, cause="solution", note="駐車場"), OPERATOR)
        v2 = self.tx(s, lambda tx: bs.derive(tx, v1, lambda r: r, cause="override", note="そのまま"), OPERATOR)
        self.w.approve_voices(s["tenant"], s["client"], v1)
        self.w.approve_voices(s["tenant"], s["client"], v2)
        self.assertEqual(self.tx(s, lambda tx: bs.adopt_ready(tx)), [(s["run"], v2)])

    def test_a_version_put_back_by_hand_is_not_moved_on_again(self):
        """契約者が手で前の版に戻したら、戻す前に作った後の版へは自動で替えない（そのあと、その後の版から作った版へも替えない＝戻す前の
        変更が入っている）。自動で替えた版から先は、続けて自動で替える（自動の割り当ては手の割り当てに数えない＝0185）。"""
        s = self.ready()
        v0 = s["version"]
        self.w.approve_voices(s["tenant"], s["client"], v0)
        self.tx(s, lambda tx: quality_stage.record_mock(tx, v0, GOOD), OPERATOR)
        v1 = self.tx(s, lambda tx: bs.derive(tx, v0, self.add_answer, cause="solution", note="駐車場"), OPERATOR)
        self.w.approve_voices(s["tenant"], s["client"], v1)
        self.assertEqual(self.tx(s, lambda tx: bs.adopt_ready(tx)), [(s["run"], v1)])        # 自動で v1 へ
        v2 = self.tx(s, lambda tx: bs.derive(tx, v1, lambda r: r, cause="override", note="そのまま"), OPERATOR)
        self.w.approve_voices(s["tenant"], s["client"], v2)
        self.assertEqual(self.tx(s, lambda tx: bs.adopt_ready(tx)), [(s["run"], v2)])        # 自動の版から先は続けて自動で
        self.tx(s, lambda tx: bs.assign(tx, s["run"], v0, reason="試験：手で前の版に戻す"), OPERATOR)
        self.assertEqual(self.tx(s, lambda tx: bs.adopt_ready(tx)), [])                      # 戻した判断を上書きしない
        v3 = self.tx(s, lambda tx: bs.derive(tx, v0, lambda r: r, cause="override", note="戻したあとに作った"), OPERATOR)
        self.w.approve_voices(s["tenant"], s["client"], v3)
        self.assertEqual(self.tx(s, lambda tx: bs.successor_tip(tx, v0)), v3)               # v3 は v2 の先（戻す前の変更を含む）
        self.assertEqual(self.tx(s, lambda tx: bs.adopt_ready(tx)), [])
        self.assertEqual(self.current(s), v0)
