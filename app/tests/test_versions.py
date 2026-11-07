"""版を足して、そろったら自動で替える1か所（business_settings.derive・derive_for_standard・adopt_ready。0183。DESIGN.md 3-8-3 の (5)）。

✅ 2026-11-02 管理者：(h) の流れ「新しい版の録音が承認されるまでは、今の版のまま電話を続けます。承認されたら、その業務の実施回を自動で
新しい版に替えます。電話は止めません。」・伺い2「案1でOK」（同じ文・読み・声の録音は使い回す）。
"""

from __future__ import annotations

from salescall.core import business_settings as bs
from salescall.core import quality_stage
from salescall.core.access import Rejected
from tests.support import OPERATOR, SYSTEM
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
        def second(tx):                                                                        # つながりの無い、その取引で作った版
            other = bs.save_version(tx, s["business"], settings_raw())
            tx.execute(
                "INSERT INTO salescall.version_successions (tenant_id, client_id, business_id, from_version, to_version, cause, note,"
                " recorded_by) VALUES (%s, %s, %s, %s, %s, 'solution', 'x', salescall.cur_actor())",   # ほかの守りには当たらない形
                (s["tenant"], s["client"], s["business"], v0, other))
        with self.assertRaises(Rejected) as e:                                                 # 1つの版の後の版は1つだけ
            self.tx(s, second, OPERATOR)
        self.assertEqual(e.exception.constraint, "version_successions_from_version_key")
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

    def test_a_version_put_back_by_hand_is_a_copy_and_later_changes_reach_it(self):
        """手で前の版に戻すと（後の版がある版を割り当てる）、その版の写しを割り当てる（以前の版へ戻す1か所＝save_from）。戻す前に作った
        後の版へは替わらない（写しにはつながりの先が無い）。戻したあとに足した版は、写しのつながりの先にでき（戻す前の変更を含まない）、
        録音がそろえば自動で替わる（⛔ 0185：時刻で外していたので、戻したあとに足した版まで届かなかった＝見直し(B) の1）。後の版がある
        版で実施回を作っても同じ（写しを割り当てる）。"""
        s = self.ready()
        v0 = s["version"]
        self.w.approve_voices(s["tenant"], s["client"], v0)
        self.tx(s, lambda tx: quality_stage.record_mock(tx, v0, GOOD), OPERATOR)
        v1 = self.tx(s, lambda tx: bs.derive(tx, v0, self.add_answer, cause="solution", note="駐車場"), OPERATOR)
        self.w.approve_voices(s["tenant"], s["client"], v1)
        self.assertEqual(self.tx(s, lambda tx: bs.adopt_ready(tx)), [(s["run"], v1)])        # 自動で v1 へ
        self.tx(s, lambda tx: bs.assign(tx, s["run"], v0, reason="試験：手で前の版に戻す"), OPERATOR)
        back = self.current(s)
        self.assertNotIn(back, (v0, v1))                                                    # 写し
        self.assertEqual(self.tx(s, lambda tx: bs.load(tx, back).raw), self.tx(s, lambda tx: bs.load(tx, v0).raw))
        self.assertEqual(self.tx(s, lambda tx: bs.successor_tip(tx, back)), back)
        self.assertEqual(self.tx(s, lambda tx: bs.adopt_ready(tx)), [])                      # 戻した判断を上書きしない
        v3 = self.tx(s, lambda tx: bs.derive(tx, back, lambda r: dict(r, answers=list(r.get("answers") or []) + [
            {"name": "営業時間", "text": "平日の9時から18時でございます。"}]), cause="solution", note="戻したあとに足した"), OPERATOR)
        names = {a for a, _ in self.tx(s, lambda tx: bs.load(tx, v3)).answers}
        self.assertTrue("営業時間" in names and "駐車場" not in names, names)                  # 戻す前の変更は入らない
        self.w.approve_voices(s["tenant"], s["client"], v3)
        self.assertEqual(self.tx(s, lambda tx: bs.adopt_ready(tx)), [(s["run"], v3)])        # そろえば自動で替わる
        from salescall.core import registry
        run = self.tx(s, lambda tx: registry.create_run(tx, s["business"], "前の版の実施回", v0, reason="試験"), OPERATOR)
        made = self.tx(s, lambda tx: tx.one("SELECT salescall.run_current_version(%s) AS v", (run,))["v"])
        self.assertNotIn(made, (v0, v1, back, v3))
        self.assertEqual(self.tx(s, lambda tx: bs.successor_tip(tx, made)), made)

    def test_a_copy_keeps_its_recordings_and_a_derived_version_records_only_what_changed(self):
        """✅ 2026-11-06 ユーザー＝計画書 6-1 (4)：「録音が必要な部分がある場合のみ必要な部分だけ録音を実施します。それはダッシュボードにも
        警告として出し、それができれば新しい版でも電話がかけられるようになります。」⇒ 変わっていない部分は元の版の録音を引き継ぐ1か所
        （core/recordings.carry。写し＝save_from と、版を足す＝derive の両方が呼ぶ。0194）。
        (1) 手で前の版に戻した写しは中身が同じなので、元の版の録音をその場で引き継ぎ、聞き直さずに話せる（電話を止めない）。
        (2) 版を足して想定問答が変わった版は、変わった部分だけが録音の要る部分（画面が読む needed_parts・契約者への知らせ recording_needed）。
            その部分だけを録って complete を呼ぶと、引き継ぐ部分と合わせた組ができ、聞くのは録った部分だけ。"""
        from salescall.core import recordings
        s = self.ready()
        v0 = s["version"]
        self.w.approve_voices(s["tenant"], s["client"], v0)
        voices = self.tx(s, lambda tx: recordings.ready_voices(tx, v0))
        self.assertTrue(voices)
        self.tx(s, lambda tx: quality_stage.record_mock(tx, v0, GOOD), OPERATOR)
        v1 = self.tx(s, lambda tx: bs.derive(tx, v0, self.add_answer, cause="solution", note="駐車場"), OPERATOR)
        part = bs.ANSWER_PART_PREFIX + "駐車場"
        want = {part: "建物の裏に3台分ございます。"}
        self.assertEqual(self.tx(s, lambda tx: recordings.needed_parts(tx, v1)), {v: want for v in voices})
        self.assertEqual(self.tx(s, lambda tx: recordings.ready_voices(tx, v1)), [])            # 録るまでは話さない（今の版のまま）
        note = self.tx(s, lambda tx: tx.one("SELECT refs FROM salescall.notification_events WHERE kind = 'recording_needed'"
                                            " AND event_key = %s", (f"recording_needed:{v1}",)))
        self.assertEqual(note["refs"]["parts"], [part])
        with self.assertRaises(ValueError):                                                  # 要る部分と違う録音では作らない
            self.tx(s, lambda tx: recordings.complete(tx, v1, voices[0], [recordings.Part(part, "違う文です。", "test://x", "1" * 64)]),
                    OPERATOR)
        for v in voices:
            sid = self.tx(s, lambda tx, v=v: recordings.complete(
                tx, v1, v, [recordings.Part(part, want[part], f"test://{v}/{part}", "1" * 64)]), OPERATOR)
            self.assertEqual(self.tx(s, lambda tx, i=sid: recordings.unapproved_parts(tx, i)), [part])   # 聞くのは録った部分だけ
            self.tx(s, lambda tx, i=sid: recordings.approve(tx, i), OPERATOR)
        self.assertEqual(self.tx(s, lambda tx: recordings.needed_parts(tx, v1)), {})
        self.assertEqual(self.tx(s, lambda tx: recordings.ready_voices(tx, v1)), voices)
        self.assertEqual(self.tx(s, lambda tx: bs.adopt_ready(tx)), [(s["run"], v1)])
        # 同じ問いの答えを置き換えた版：前の答えの録音は引き継がず（その部分は録り直す）、ほかの部分は引き継ぐ
        new_text = "建物の裏に5台分ございます。"
        v2 = self.tx(s, lambda tx: bs.derive(tx, v1, lambda r: dict(r, answers=[
            dict(a, text=new_text) if a["name"] == "駐車場" else a for a in r["answers"]]), cause="solution", note="駐車場の答え"), OPERATOR)
        self.assertEqual(self.tx(s, lambda tx: recordings.needed_parts(tx, v2)), {v: {part: new_text} for v in voices})
        sid = self.tx(s, lambda tx: recordings.complete(tx, v2, voices[0], [recordings.Part(part, new_text, "test://new", "2" * 64)]),
                      OPERATOR)
        got = self.tx(s, lambda tx: recordings.parts(tx, sid))
        self.assertEqual((sorted(got), got[part].spoken_text, self.tx(s, lambda tx: recordings.unapproved_parts(tx, sid))),
                         (sorted(["intro", part]), new_text, [part]))

        self.tx(s, lambda tx: bs.assign(tx, s["run"], v0, reason="試験：手で前の版に戻す"), OPERATOR)
        back = self.current(s)
        self.assertNotIn(back, (v0, v1))
        self.assertEqual(self.tx(s, lambda tx: recordings.ready_voices(tx, back)), voices)      # 聞き直さずに、すぐ話せる
        self.assertEqual(self.tx(s, lambda tx: recordings.needed_parts(tx, back)), {})

    def test_a_version_changed_beyond_its_answers_keeps_no_recordings(self):
        """想定問答のほかの項目も違う版（どの録音の部分が変わったかを、ここでは決められない）：元の版の録音を引き継がない（録音の道具が、
        その版の部分の一覧で組を作る。core/recordings._changes）。"""
        from salescall.core import recordings
        s = self.ready()
        v0 = s["version"]
        self.w.approve_voices(s["tenant"], s["client"], v0)
        self.assertTrue(self.tx(s, lambda tx: recordings.ready_voices(tx, v0)))
        v1 = self.tx(s, lambda tx: bs.derive(tx, v0, lambda r: dict(r, purpose="ご予約の内容を伺う"), cause="override", note="目的"),
                     OPERATOR)
        self.assertEqual((self.tx(s, lambda tx: recordings.ready_voices(tx, v1)), self.tx(s, lambda tx: recordings.needed_parts(tx, v1))),
                         ([], {}))

    def test_a_later_version_needs_a_voice_even_when_the_current_one_has_none(self):
        """今の版に話してよい声が無くても、後の版には声が1つ以上要る（声の無い版へ替えても電話はかけられない）。"""
        s = self.ready()
        v0 = self.tx(s, lambda tx: bs.save_version(tx, s["business"], settings_raw()), OPERATOR)   # 録音の無い版
        self.tx(s, lambda tx: bs.assign(tx, s["run"], v0, reason="試験：録音の無い版"), OPERATOR)
        self.tx(s, lambda tx: quality_stage.record_mock(tx, v0, GOOD), OPERATOR)
        self.tx(s, lambda tx: bs.derive(tx, v0, self.add_answer, cause="solution", note="駐車場"), OPERATOR)
        self.assertEqual(self.tx(s, lambda tx: bs.adopt_ready(tx)), [])

    def test_the_database_keeps_successions_forward_and_auto_assignments_to_the_system(self):
        """版のつながりは、前の版より後に作った版へだけ（逆向き・輪は作れない）・同じ業務の版だけ・記録した方は操作している方・時刻は
        データベースが決める。自動の割り当て（auto）はシステムの処理だけで、時刻はデータベースが決める（見直し(B) の4・0189）。"""
        s = self.ready()
        v0 = s["version"]
        v1 = self.tx(s, lambda tx: bs.save_version(tx, s["business"], settings_raw()), OPERATOR)
        other, _ = self.w.business_with_version(s["tenant"], s["client"])
        self.w.add_version(s["tenant"], s["client"], other)
        sql = ("INSERT INTO salescall.version_successions (tenant_id, client_id, business_id, from_version, to_version, cause, note,"
               " recorded_by, recorded_at) VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, %s, 'solution', 'x', %s, %s)")
        far_sql = ("INSERT INTO salescall.business_versions (tenant_id, client_id, business_id, created_by)"   # ほかの業務の版を、その取引で
                   " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, salescall.cur_actor()) RETURNING version_id")   # 作る
        for business, frm, to, by, constraint in ((s["business"], v1, v0, "操作", "version_succession_forward"),
                                                  (s["business"], v0, v1, "ほかの方", "version_succession_recorded_by"),
                                                  (s["business"], v0, "far", "操作", None)):
            def work(tx, business=business, frm=frm, to=to, by=by):   # ほかの業務の版（番号は v1 より大きい＝向きでは断られない）
                to = tx.one(far_sql, (other,))["version_id"] if to == "far" else to
                tx.execute(sql, (business, frm, to, tx.one("SELECT salescall.cur_actor() AS a")["a"] if by == "操作" else by,
                                 "2000-01-01"))
            with self.subTest(constraint=constraint), self.assertRaises(Rejected) as e:
                self.tx(s, work, OPERATOR)
            if constraint is not None:
                self.assertEqual(e.exception.constraint, constraint)
            else:                                                                           # 同じ業務の版だけ（0183 の外部キー）
                self.assertIn("fkey", e.exception.constraint or "")
        # きっかけと記録した方の種類：標準の想定問答の版はシステムの処理だけ・解決策の版は管理者か契約者だけ（0192）
        std_sql = sql.replace("'solution'", "'standard'")
        for q, who in ((std_sql, OPERATOR), (sql, SYSTEM)):
            with self.subTest(who=who.name), self.assertRaises(Rejected) as e:
                self.tx(s, lambda tx, q=q: tx.execute(q, (s["business"], v0, v1, tx.one("SELECT salescall.cur_actor() AS a")["a"],
                                                         "2000-01-01")), who)
            self.assertEqual(e.exception.constraint, "version_succession_cause_actor")
        # 後の版は、その取引で作った版だけ（前からある版を、ほかの版の後に直接つなげると、adopt_ready が自動で割り当ててしまう。0197）
        with self.assertRaises(Rejected) as e:
            self.tx(s, lambda tx: tx.execute(sql, (s["business"], v0, v1, tx.one("SELECT salescall.cur_actor() AS a")["a"],
                                                   "2000-01-01")), OPERATOR)
        self.assertEqual(e.exception.constraint, "version_origin_made_now")
        made = self.tx(s, lambda tx: tx.one("INSERT INTO salescall.business_versions (tenant_id, client_id, business_id, created_by,"
                                            " created_at) VALUES (salescall.cur_tenant(), salescall.cur_client(), %s,"
                                            " salescall.cur_actor(), '2000-01-01') RETURNING created_at", (s["business"],)), OPERATOR)
        self.assertGreater(made["created_at"].year, 2000)                                   # 版の作られた時刻はデータベースが決める

        def link(tx):
            v2 = tx.one(far_sql, (s["business"],))["version_id"]
            tx.execute(sql, (s["business"], v0, v2, tx.one("SELECT salescall.cur_actor() AS a")["a"], "2000-01-01"))
            return v2
        v2 = self.tx(s, link, OPERATOR)
        at = self.tx(s, lambda tx: tx.one("SELECT recorded_at > now() - interval '1 hour' AS fresh FROM salescall.version_successions"
                                          " WHERE from_version = %s", (v0,))["fresh"])
        self.assertTrue(at)
        self.assertEqual(self.tx(s, lambda tx: bs.successor_tip(tx, v0)), v2)
        with self.assertRaises(Rejected) as e:
            self.tx(s, lambda tx: bs.assign(tx, s["run"], v1, reason="試験：自動に見せかける", auto=True), OPERATOR)
        self.assertEqual(e.exception.constraint, "run_version_auto_by_system")
        self.tx(s, lambda tx: tx.execute(
            "INSERT INTO salescall.run_versions (tenant_id, client_id, run_id, business_id, version_id, assigned_by, reason, assigned_at)"
            " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, %s, salescall.cur_actor(), '試験', '2000-01-01')",
            (s["run"], s["business"], v1)), OPERATOR)
        fresh = self.tx(s, lambda tx: tx.one("SELECT assigned_at > now() - interval '1 hour' AS f FROM salescall.run_versions"
                                             " WHERE run_id = %s ORDER BY seq DESC LIMIT 1", (s["run"],))["f"])
        self.assertTrue(fresh)

    def test_one_failing_version_does_not_stop_the_others_in_the_job(self):
        """版を自動で替える仕事は、版ごと・実施回ごとに別の取引（1つが落ちても、ほかの実施回は替わる＝見直し(B) の5）。"""
        from salescall.supervisor import jobs
        from salescall.core import registry
        s = self.ready()
        v0 = s["version"]
        broken = self.tx(s, lambda tx: registry.create_run(tx, s["business"], "2つ目の実施回", v0, reason="試験"), OPERATOR)
        self.w.task(s["tenant"], s["client"], broken, s["contact"])   # まだ終えていない行のある2つ目の実施回（同じ版）
        self.w.approve_voices(s["tenant"], s["client"], v0)
        self.tx(s, lambda tx: quality_stage.record_mock(tx, v0, GOOD), OPERATOR)
        v1 = self.tx(s, lambda tx: bs.derive(tx, v0, self.add_answer, cause="solution", note="駐車場"), OPERATOR)
        self.w.approve_voices(s["tenant"], s["client"], v1)
        calls = []
        real = bs.adopt_ready

        def adopt(tx, run_id=None):
            calls.append(run_id)
            if len(calls) == 1:
                raise RuntimeError("試験：1つ目の実施回で落ちる")
            return real(tx, run_id)
        reported = []
        from types import SimpleNamespace
        from unittest import mock
        with mock.patch.object(bs, "adopt_ready", adopt), mock.patch.object(jobs, "report", lambda *a: reported.append(a)), \
                mock.patch.object(jobs, "_clients", lambda ctx: [s["client"]]):
            jobs.versions(SimpleNamespace(access=self.w.a, tenant_id=s["tenant"]))
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(reported), 1)
        moved = [r for r in (s["run"], broken)
                 if self.tx(s, lambda tx, r=r: tx.one("SELECT salescall.run_current_version(%s) AS v", (r,))["v"]) == v1]
        self.assertEqual(len(moved), 1)                                                     # 落ちなかった方は替わった


    def test_a_derived_version_keeps_the_readings_chosen_by_ear(self):
        """版を足しても、契約者が録音を聞いて選んだ名前の読みは引き継ぐ（以前の版へ戻すのと同じ recordings.carry_readings）。"""
        from salescall.core import recordings
        s = self.ready()
        r = settings_raw()
        r["names"] = list(r.get("names") or []) + [{"text": "中島", "reading": None, "source": "script", "reading_by": None,
                                                    "candidates": ["なかじま", "なかしま"], "likely": "なかじま"}]
        v = self.tx(s, lambda tx: bs.save_version(tx, s["business"], r), OPERATOR)
        self.tx(s, lambda tx: recordings.choose_reading(tx, v, "中島", "なかしま"), OPERATOR)
        v1 = self.tx(s, lambda tx: bs.derive(tx, v, self.add_answer, cause="solution", note="駐車場"), OPERATOR)
        name = next(n for n in self.tx(s, lambda tx: bs.load(tx, v1)).names if n.text == "中島")
        self.assertEqual((name.reading, name.reading_by), ("なかしま", "recording"))

    # ── 2026-11-07 の第三者の見直し (2)（0197。版の元＝つながりの前の版か写しの元の1か所）
    def test_answers_added_before_recording_are_recorded_together_from_the_nearest_recorded_version(self):
        """(2) の2：録音していない途中の版から足した版（V0 は録音済み → V1 はまだ → V2）は、いまの組がある、いちばん近い元の版（V0）から、
        変わった部分をまとめて録音していただく（needed_parts・complete・知らせ recording_needed。⛔ 前は「つながりの1つ前の版」だけを見たので、
        V2 には録音の要る部分も警告も出ず、complete も断り、録音が2回に分かれた）。"""
        from salescall.core import recordings
        s = self.ready()
        v0 = s["version"]
        self.w.approve_voices(s["tenant"], s["client"], v0)
        voices = self.tx(s, lambda tx: recordings.ready_voices(tx, v0))
        self.tx(s, lambda tx: quality_stage.record_mock(tx, v0, GOOD), OPERATOR)
        v1 = self.tx(s, lambda tx: bs.derive(tx, v0, self.add_answer, cause="solution", note="駐車場"), OPERATOR)
        v2 = self.tx(s, lambda tx: bs.derive(tx, v1, lambda r: dict(r, answers=list(r["answers"]) + [
            {"name": "営業時間", "text": "平日の9時から18時でございます。"}]), cause="solution", note="営業時間"), OPERATOR)
        p = bs.ANSWER_PART_PREFIX
        want = {p + "駐車場": "建物の裏に3台分ございます。", p + "営業時間": "平日の9時から18時でございます。"}
        self.assertEqual(self.tx(s, lambda tx: recordings.needed_parts(tx, v2)), {v: want for v in voices})
        note = self.tx(s, lambda tx: tx.one("SELECT refs FROM salescall.notification_events WHERE event_key = %s",
                                            (f"recording_needed:{v2}",)))
        self.assertEqual(note["refs"]["parts"], sorted(want))
        for v in voices:
            sid = self.tx(s, lambda tx, v=v: recordings.complete(
                tx, v2, v, [recordings.Part(k, t, f"test://{v}/{k}", "3" * 64) for k, t in want.items()]), OPERATOR)
            self.assertEqual(self.tx(s, lambda tx, i=sid: recordings.unapproved_parts(tx, i)), sorted(want))   # 聞くのは録った部分だけ
            self.tx(s, lambda tx, i=sid: recordings.approve(tx, i), OPERATOR)
        self.assertEqual(self.tx(s, lambda tx: recordings.ready_voices(tx, v2)), voices)
        self.assertEqual(self.tx(s, lambda tx: bs.adopt_ready(tx)), [(s["run"], v2)])         # 1回の録音で、いちばん後の版へ

    def test_a_copy_of_an_unrecorded_version_asks_for_what_differs_from_its_recorded_origin(self):
        """(2) の2：録音していない途中の版（V1）へ手で戻した写しは、写しの元（V1）・そのつながりの前の版（V0）とたどって、いまの組がある
        いちばん近い元の版（V0）から変わった部分の録音を求める（警告と知らせ。⛔ 前は写しの元を知らず、組も警告も無いまま話せなかった）。"""
        from salescall.core import recordings
        s = self.ready()
        v0 = s["version"]
        self.w.approve_voices(s["tenant"], s["client"], v0)
        voices = self.tx(s, lambda tx: recordings.ready_voices(tx, v0))
        self.tx(s, lambda tx: quality_stage.record_mock(tx, v0, GOOD), OPERATOR)
        v1 = self.tx(s, lambda tx: bs.derive(tx, v0, self.add_answer, cause="solution", note="駐車場"), OPERATOR)
        v2 = self.tx(s, lambda tx: bs.derive(tx, v1, lambda r: dict(r, answers=list(r["answers"]) + [
            {"name": "営業時間", "text": "平日の9時から18時でございます。"}]), cause="solution", note="営業時間"), OPERATOR)
        self.w.approve_voices(s["tenant"], s["client"], v2, parts=("intro", "answer_駐車場", "answer_営業時間"))
        self.assertEqual(self.tx(s, lambda tx: bs.adopt_ready(tx)), [(s["run"], v2)])
        self.tx(s, lambda tx: bs.assign(tx, s["run"], v1, reason="試験：手で録音していない版に戻す"), OPERATOR)
        back = self.current(s)
        self.assertNotIn(back, (v0, v1, v2))
        self.assertEqual(self.tx(s, lambda tx: bs.version_origins(tx, back)), [v1, v0])
        part = bs.ANSWER_PART_PREFIX + "駐車場"
        self.assertEqual(self.tx(s, lambda tx: recordings.needed_parts(tx, back)), {v: {part: "建物の裏に3台分ございます。"} for v in voices})
        self.assertTrue(self.tx(s, lambda tx: tx.one("SELECT 1 FROM salescall.notification_events WHERE event_key = %s",
                                                     (f"recording_needed:{back}",))))

    def test_a_version_put_back_or_derived_after_verification_starts_is_not_checked_again(self):
        """(2) の3：検証モードに切り替えたあと、後の版がある前の版へ手で戻す（写し）・同じ版から版を足すのは、置いたときの確かめを信じて、
        前からある AI の読みを確かめ直さない（⛔ 前は版を作る確かめを通したので、後の版がある版へは戻せず、版も足せなかった）。新しく足す
        AI の読みは、検証モードでは契約者が確かめる（今までどおり）。"""
        from salescall.core import settings
        s = self.ready()
        r = settings_raw()
        r["info"] = list(r.get("info") or []) + ["東海の工場で作っています。"]
        r["names"] = list(r["names"]) + [{"text": "東海", "reading": "とうかい", "source": "script", "reading_by": "ai",
                                          "candidates": [], "likely": None}]
        v0 = self.tx(s, lambda tx: bs.save_version(tx, s["business"], r), OPERATOR)
        self.tx(s, lambda tx: bs.assign(tx, s["run"], v0, reason="試験"), OPERATOR)
        v1 = self.tx(s, lambda tx: bs.derive(tx, v0, self.add_answer, cause="solution", note="駐車場"), OPERATOR)
        self.tx(s, lambda tx: bs.assign(tx, s["run"], v1, reason="試験：v1"), OPERATOR)
        self.tx(s, lambda tx: settings.set_value(tx, "approval.human", True, scope="business", status="decided",
                                              rationale="試験：検証モード", review_when="試験",
                                              tenant_id=s["tenant"], client_id=s["client"], business_id=s["business"]), OPERATOR)
        self.tx(s, lambda tx: bs.assign(tx, s["run"], v0, reason="試験：手で v0 に戻す"), OPERATOR)
        back = self.current(s)
        self.assertNotIn(back, (v0, v1))
        self.assertEqual(self.tx(s, lambda tx: bs.load(tx, back).raw), self.tx(s, lambda tx: bs.load(tx, v0).raw))
        v2 = self.tx(s, lambda tx: bs.derive(tx, back, lambda raw: dict(raw, answers=list(raw["answers"]) + [
            {"name": "営業時間", "text": "平日の9時から18時でございます。"}]), cause="solution", note="営業時間"), OPERATOR)
        self.assertIn("営業時間", dict(self.tx(s, lambda tx: bs.load(tx, v2)).answers))
        with self.assertRaises(bs.SettingsInvalid):                                        # 新しく足す AI の読みは確かめる
            self.tx(s, lambda tx: bs.derive(tx, v2, lambda raw: dict(raw, names=list(raw["names"]) + [
                {"text": "西海", "reading": "さいかい", "source": "script", "reading_by": "ai", "candidates": [], "likely": None}]),
                cause="override", note="名前を足す"), OPERATOR)

    def test_answers_off_changes_keep_the_business_recordings(self):
        """使わない標準の問い（answers_off）だけが違う版は、標準の録音から流す部分だけが変わる（業務の録音の部分は同じ）ので、業務の録音を
        その場で引き継ぐ（core/recordings._changes の1か所。想定問答の外の違いとはみなさない）。"""
        from salescall.core import recordings
        s = self.ready()
        with self.w.standard(STD, voices=("Leda", "Sadachbia")) as std:
            v = self.tx(s, lambda tx: bs.save_version(tx, s["business"], settings_raw(), standard_id=std), OPERATOR)
            self.w.approve_voices(s["tenant"], s["client"], v)
            voices = self.tx(s, lambda tx: recordings.ready_voices(tx, v))
            self.assertTrue(voices)
            off = self.tx(s, lambda tx: bs.derive(tx, v, lambda r: dict(r, answers_off=["会社の場所"]), cause="override",
                                                  note="会社の場所を使わない"), OPERATOR)
            self.assertEqual(self.tx(s, lambda tx: recordings.needed_parts(tx, off)), {})
            self.assertEqual(self.tx(s, lambda tx: recordings.ready_voices(tx, off)), voices)   # 聞き直さずに話せる

    def test_a_version_whose_names_changed_keeps_no_recordings(self):
        """前からある名前の読みが変わった版は、どの録音の部分がその名前を話すかを、ここでは決められないので、録音を引き継がない（名前の一覧の
        違いで引き継ぐのは「足しただけ」のとき＝core/recordings._changes の1か所）。"""
        from salescall.core import recordings
        s = self.ready()
        r = settings_raw()
        r["names"] = list(r["names"]) + [{"text": "西海", "reading": "さいかい", "source": "script", "reading_by": "given",
                                          "candidates": [], "likely": None}]
        v0 = self.tx(s, lambda tx: bs.save_version(tx, s["business"], r), OPERATOR)
        self.w.approve_voices(s["tenant"], s["client"], v0)
        self.assertTrue(self.tx(s, lambda tx: recordings.ready_voices(tx, v0)))
        v1 = self.tx(s, lambda tx: bs.derive(tx, v0, lambda raw: dict(raw, names=[dict(n, reading="にしうみ") if n["text"] == "西海" else n
                                                                             for n in raw["names"]]),
                                             cause="override", note="西海の読み"), OPERATOR)
        self.assertEqual((self.tx(s, lambda tx: recordings.needed_parts(tx, v1)), self.tx(s, lambda tx: recordings.ready_voices(tx, v1))),
                         ({}, []))

    def test_kept_parts_keep_the_readings_they_were_recorded_with(self):
        """引き継ぐ部分は、録音で書いた名前の読みも引き継ぐ（承認の中身の鍵に入る。落とすと、その名前を話す部分は組を作れない）。"""
        from salescall.core import recordings
        s = self.ready()
        r = settings_raw()
        r["names"] = list(r["names"]) + [{"text": "中島", "reading": None, "source": "script", "reading_by": None,
                                          "candidates": ["なかじま", "なかしま"], "likely": "なかじま"}]
        r["answers"] = [{"name": "担当", "text": "中島がご案内いたします。"}]
        v0 = self.tx(s, lambda tx: bs.save_version(tx, s["business"], r), OPERATOR)
        self.tx(s, lambda tx: recordings.choose_reading(tx, v0, "中島", "なかしま"), OPERATOR)
        part = bs.ANSWER_PART_PREFIX + "担当"

        def record(tx):
            for v in ("Leda", "Sadachbia"):
                recordings.approve(tx, recordings.create_set(tx, unit="business", voice_name=v, version_id=v0, parts=[
                    recordings.Part("intro", "架空の録音（intro）。", f"test://{v}/intro", "0" * 64),
                    recordings.Part(part, "「なかしま」がご案内いたします。", f"test://{v}/{part}", "4" * 64, {"中島": "なかしま"})]))
        self.tx(s, record, OPERATOR)
        voices = self.tx(s, lambda tx: recordings.ready_voices(tx, v0))
        self.assertTrue(voices)
        v1 = self.tx(s, lambda tx: bs.derive(tx, v0, self.add_answer, cause="solution", note="駐車場"), OPERATOR)
        self.assertEqual(set(self.tx(s, lambda tx: recordings.needed_parts(tx, v1))), set(voices))
        sid = self.tx(s, lambda tx: recordings.complete(tx, v1, voices[0], [recordings.Part(
            bs.ANSWER_PART_PREFIX + "駐車場", "建物の裏に3台分ございます。", "test://new", "5" * 64)]), OPERATOR)
        got = self.tx(s, lambda tx: tx.one("SELECT reading FROM salescall.recording_readings WHERE set_id = %s AND part = %s",
                                           (sid, part)))
        self.assertEqual(got["reading"], "なかしま")

    def test_the_database_keeps_copies_to_versions_made_in_the_same_transaction(self):
        """写しの元（version_copies）も、つながりと同じ守り（0197 の version_origin_check の1か所）：写しの版はその取引で作った版だけ・
        1つの版の元は1つ（つながりか写しのどちらか）・輪にならない・記録した方は操作している方。"""
        s = self.ready()
        v0 = s["version"]
        v1 = self.tx(s, lambda tx: bs.save_version(tx, s["business"], settings_raw()), OPERATOR)
        sql = ("INSERT INTO salescall.version_copies (tenant_id, client_id, version_id, copied_from, recorded_by)"
               " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, {by})")
        me = "salescall.cur_actor()"
        for frm, to, by, constraint in ((v0, v1, me, "version_origin_made_now"), (v0, "new", "'ほかの方'", "version_copy_recorded_by")):
            def work(tx, frm=frm, to=to, by=by):
                to = bs.save_version(tx, s["business"], settings_raw()) if to == "new" else to
                tx.execute(sql.format(by=by), (to, frm))
            with self.subTest(constraint=constraint), self.assertRaises(Rejected) as e:
                self.tx(s, work, OPERATOR)
            self.assertEqual(e.exception.constraint, constraint)

        def twice(tx):                                                                       # 写しにしてから、つながりの後の版にもする
            new = bs.save_version(tx, s["business"], settings_raw())
            tx.execute(sql.format(by=me), (new, v0))
            tx.execute("INSERT INTO salescall.version_successions (tenant_id, client_id, business_id, from_version, to_version, cause,"
                       " note, recorded_by) VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, %s, 'solution', 'x',"
                       " salescall.cur_actor())", (s["business"], v1, new))
        with self.assertRaises(Rejected) as e:
            self.tx(s, twice, OPERATOR)
        self.assertEqual(e.exception.constraint, "version_origin_one")

        def loop(tx):                                                                        # 新しい2つの版を、たがいの写しにする
            a = bs.save_version(tx, s["business"], settings_raw())
            b = bs.save_version(tx, s["business"], settings_raw())
            tx.execute(sql.format(by=me), (a, b))
            tx.execute(sql.format(by=me), (b, a))
        with self.assertRaises(Rejected) as e:
            self.tx(s, loop, OPERATOR)
        self.assertEqual(e.exception.constraint, "version_origin_no_loop")
        copy = self.tx(s, lambda tx: bs.restore(tx, v0), OPERATOR)                           # 写しの1か所（save_from）が元を残す
        self.assertEqual(self.tx(s, lambda tx: bs.version_origins(tx, copy)), [v0])
