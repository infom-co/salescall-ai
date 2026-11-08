"""版を足して、そろったら自動で替える1か所（business_settings.derive・derive_for_standard・adopt_ready。0183。DESIGN.md 3-8-3 の (5)）。

✅ 2026-11-02 管理者：(h) の流れ「新しい版の録音が承認されるまでは、今の版のまま電話を続けます。承認されたら、その業務の実施回を自動で
新しい版に替えます。電話は止めません。」・伺い2「案1でOK」（同じ文・読み・声の録音は使い回す）。
"""

from __future__ import annotations

from salescall.core import business_settings as bs
from salescall.core import quality_stage
from salescall.core.access import Rejected
from tests import support
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
        self.w.approve_voices(s["tenant"], s["client"], v1, ("Leda",), parts=("intro", "answer_駐車場"))   # Sadachbia は答えの録音が要る
        self.assertEqual(self.tx(s, lambda tx: bs.adopt_ready(tx)), [])                        # Sadachbia が減る
        self.w.approve_voices(s["tenant"], s["client"], v1, ("Sadachbia",), parts=("intro", "answer_駐車場"))
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
        警告として出し、それができれば新しい版でも電話がかけられるようになります。」⇒ 版の録音の部分の一覧は会話の担当の録音の道具が渡し
        （recordings.list_parts）、要る部分は、その一覧のうち承認済みの同じ録音が無い部分だけ（recordings.version_needs の1か所。0212）。
        (1) 版を足した版は、一覧が届くまで「録音の部分の一覧がまだ」（警告・知らせ recording_list_needed・その版ではかけない＝今の版のまま）。
        (2) 一覧が届くと、変わった部分だけが録音の要る部分（警告・知らせ recording_needed）。その部分だけを録って complete を呼ぶと、使い回す部分と
            合わせた組ができ、聞くのは録った部分だけ。一覧の外の部分・文の違う録音は断る。
        (3) 手で前の版に戻した写しは中身が同じなので、元の版の一覧と録音をそのまま使い、聞き直さずに話せる（電話を止めない）。"""
        from salescall.core import recordings
        s = self.ready()
        v0 = s["version"]
        self.w.approve_voices(s["tenant"], s["client"], v0)
        voices = self.tx(s, lambda tx: recordings.ready_voices(tx, v0))
        self.assertTrue(voices)
        self.tx(s, lambda tx: quality_stage.record_mock(tx, v0, GOOD), OPERATOR)
        v1 = self.tx(s, lambda tx: bs.derive(tx, v0, self.add_answer, cause="solution", note="駐車場"), OPERATOR)
        self.assertEqual(self.tx(s, lambda tx: recordings.version_needs(tx, v1)), recordings.VersionNeeds(False, {}))   # 一覧がまだ
        self.assertTrue(self.tx(s, lambda tx: tx.one("SELECT 1 FROM salescall.notification_events WHERE event_key = %s",
                                                     (f"recording_list_needed:{v1}",))))
        self.assertEqual(self.tx(s, lambda tx: recordings.ready_voices(tx, v1)), [])
        part = bs.ANSWER_PART_PREFIX + "駐車場"
        want = {part: "建物の裏に3台分ございます。"}
        got = self.tx(s, lambda tx: support.send_list(tx, v1), OPERATOR)                      # 会話の担当の録音の道具が一覧を渡す
        self.assertEqual(got, recordings.VersionNeeds(True, {v: want for v in voices}))
        self.assertEqual(self.tx(s, lambda tx: recordings.version_needs(tx, v1)), got)
        self.assertEqual(self.tx(s, lambda tx: recordings.ready_voices(tx, v1)), [])            # 録るまでは話さない（今の版のまま）
        note = self.tx(s, lambda tx: tx.one("SELECT refs FROM salescall.notification_events WHERE kind = 'recording_needed'"
                                            " AND event_key = %s", (f"recording_needed:{v1}",)))
        self.assertEqual(note["refs"]["parts"], [part])
        for wrong in ([recordings.Part(part, "違う文です。", "test://x", "1" * 64)],                                 # 文が違う
                      [recordings.Part(part, want[part], "test://x", "1" * 64), recordings.Part("Q9", "一覧に無い文。", "test://x",
                                                                                              "1" * 64)],          # 一覧の外
                      [recordings.Part("intro", "架空の録音（intro）。", "test://x", "1" * 64)]):                  # 要る部分が無い
            with self.subTest(parts=[p.part for p in wrong]), self.assertRaises(ValueError):
                self.tx(s, lambda tx, wrong=wrong: recordings.complete(tx, v1, voices[0], wrong), OPERATOR)
        for v in voices:
            sid = self.tx(s, lambda tx, v=v: recordings.complete(
                tx, v1, v, [recordings.Part(part, want[part], f"test://{v}/{part}", "1" * 64)]), OPERATOR)
            self.assertEqual(self.tx(s, lambda tx, i=sid: recordings.unapproved_parts(tx, i)), [part])   # 聞くのは録った部分だけ
            self.tx(s, lambda tx, i=sid: recordings.approve(tx, i), OPERATOR)
        self.assertEqual(self.tx(s, lambda tx: recordings.version_needs(tx, v1)), recordings.VersionNeeds(True, {}))
        self.assertEqual(self.tx(s, lambda tx: recordings.ready_voices(tx, v1)), voices)
        self.assertEqual(self.tx(s, lambda tx: bs.adopt_ready(tx)), [(s["run"], v1)])
        # 同じ問いの答えを置き換えた版：前の答えの録音は使えず（その部分は録り直す）、ほかの部分は使い回す
        new_text = "建物の裏に5台分ございます。"
        v2 = self.tx(s, lambda tx: bs.derive(tx, v1, lambda r: dict(r, answers=[
            dict(a, text=new_text) if a["name"] == "駐車場" else a for a in r["answers"]]), cause="solution", note="駐車場の答え"), OPERATOR)
        self.tx(s, lambda tx: support.send_list(tx, v2), OPERATOR)
        self.assertEqual(self.tx(s, lambda tx: recordings.version_needs(tx, v2)).parts, {v: {part: new_text} for v in voices})
        sid = self.tx(s, lambda tx: recordings.complete(tx, v2, voices[0], [recordings.Part(part, new_text, "test://new", "2" * 64)]),
                      OPERATOR)
        got = self.tx(s, lambda tx: recordings.parts(tx, sid))
        self.assertEqual((sorted(got), got[part].spoken_text, self.tx(s, lambda tx: recordings.unapproved_parts(tx, sid))),
                         (sorted(["intro", part]), new_text, [part]))

        self.tx(s, lambda tx: bs.assign(tx, s["run"], v0, reason="試験：手で前の版に戻す"), OPERATOR)
        back = self.current(s)
        self.assertNotIn(back, (v0, v1))
        self.assertEqual(self.tx(s, lambda tx: recordings.ready_voices(tx, back)), voices)      # 聞き直さずに、すぐ話せる
        self.assertEqual(self.tx(s, lambda tx: recordings.version_needs(tx, back)), recordings.VersionNeeds(True, {}))

    def test_the_gate_waits_for_the_recording_list_and_calls_once_the_needed_parts_are_recorded(self):
        """場面（0212・計画書 6-1 (4)）：契約者が手で、目的の文を変えた新しい版を実施回に割り当てた。(1) 会話の担当の録音の部分の一覧が届くまで、
        門は「録音の部分の一覧がまだ」で待たせ、契約者に知らせる（ダッシュボードの警告＝version_needs と同じ1か所）。(2) 一覧が届くと、要る部分は
        変わった目的の文だけ（名乗りは承認済みの同じ録音を使う）。録音するまでは「録音がまだ」で待つ。(3) その部分を録って承認すると、門が通す。"""
        from salescall.core import gate, recordings
        s = self.ready()
        v0 = s["version"]
        self.w.approve_voices(s["tenant"], s["client"], v0, parts=("intro", "purpose"))
        voices = self.tx(s, lambda tx: recordings.ready_voices(tx, v0))
        self.assertTrue(voices)
        v1 = self.tx(s, lambda tx: bs.derive(tx, v0, lambda r: dict(r, purpose="ご予約の内容を伺う"), cause="override", note="目的"),
                     OPERATOR)
        self.tx(s, lambda tx: quality_stage.open_stage(tx, v1, 1, None, evidence="試験"), OPERATOR)
        self.tx(s, lambda tx: bs.assign(tx, s["run"], v1, reason="試験：手で新しい版にする"), OPERATOR)
        self.assertEqual(self.current(s), v1)
        told = lambda: [r["summary"] for r in self.tx(s, lambda tx: tx.all(   # noqa: E731
            "SELECT summary FROM salescall.notification_events WHERE kind = 'setting_missing' AND summary = ANY(%s) ORDER BY event_id",
            ([gate.VOICE_LIST_MISSING, gate.VOICE_NOT_READY],)))]
        self.assertEqual(self.reason(s), "voice_not_ready")
        self.assertEqual(told(), [gate.VOICE_LIST_MISSING])
        self.assertEqual(self.tx(s, lambda tx: recordings.version_needs(tx, v1)), recordings.VersionNeeds(False, {}))
        new = recordings.ListedPart("purpose", "ご予約の内容を伺うお電話です。")
        need = self.tx(s, lambda tx: recordings.list_parts(tx, v1, [recordings.ListedPart("intro", "架空の録音（intro）。"), new]),
                       OPERATOR)
        self.assertEqual(need, recordings.VersionNeeds(True, {v: {"purpose": new.spoken_text} for v in voices}))
        self.assertEqual(self.reason(s), "voice_not_ready")
        self.assertEqual(told(), [gate.VOICE_LIST_MISSING, gate.VOICE_NOT_READY])
        for v in voices:
            self.tx(s, lambda tx, v=v: recordings.approve(tx, recordings.complete(tx, v1, v, [recordings.Part(
                "purpose", new.spoken_text, f"test://{v}/purpose", "6" * 64)])), OPERATOR)
        self.assertEqual(self.tx(s, lambda tx: recordings.version_needs(tx, v1)), recordings.VersionNeeds(True, {}))
        got = self.claim(s)
        self.assertIsInstance(got, gate.Claim, got)
        # 会話の担当が一覧を渡し直して部分が増えたら（質問を足した など）、その部分の無い組では話さない（その部分だけ録音していただく）
        more = recordings.ListedPart("Q2", "ご希望の台数をお聞かせください。")
        need = self.tx(s, lambda tx: recordings.list_parts(tx, v1, [recordings.ListedPart("intro", "架空の録音（intro）。"), new, more]),
                       OPERATOR)
        self.assertEqual(need.parts, {v: {"Q2": more.spoken_text} for v in voices})
        self.assertEqual(self.tx(s, lambda tx: recordings.ready_voices(tx, v1)), [])

    def test_the_database_keeps_recording_lists_as_written(self):
        """版の録音の部分の一覧（0212）は、書いたまま（変えない・消さない）。部分は、その一覧を作った取引でだけ足せる（あとから足せない）。
        書いた方は、いま操作している方。ほかの依頼者からは見えない。"""
        from salescall.core import recordings
        s = self.ready()
        v = s["version"]
        self.tx(s, lambda tx: recordings.list_parts(tx, v, [recordings.ListedPart("Q7", "ご希望の日時をお聞かせください。")]), OPERATOR)
        list_id = self.tx(s, lambda tx: tx.one("SELECT salescall.recording_list_of(%s) AS l", (v,))["l"])
        self.assertIsNotNone(list_id)
        for sql, constraint in (
                ("UPDATE salescall.recording_lists SET recorded_by = 'x' WHERE list_id = %s", None),
                ("DELETE FROM salescall.recording_list_parts WHERE list_id = %s", None),
                ("INSERT INTO salescall.recording_list_parts (tenant_id, client_id, list_id, part, spoken_text)"
                 " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, 'Q8', 'あとから足した文。')", "recording_list_part_made_now")):
            with self.subTest(sql=sql), self.assertRaises(Rejected) as e:
                self.tx(s, lambda tx, sql=sql: tx.execute(sql, (list_id,)), OPERATOR)
            if constraint:
                self.assertEqual(e.exception.constraint, constraint)
        with self.assertRaises(Rejected) as e:
            self.tx(s, lambda tx: tx.execute("INSERT INTO salescall.recording_lists (tenant_id, client_id, version_id, recorded_by)"
                                             " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, 'ほかの方')", (v,)), OPERATOR)
        self.assertEqual(e.exception.constraint, "recording_list_recorded_by")
        other = self.ready(tenant=s["tenant"])
        self.assertEqual(self.tx(other, lambda tx: tx.all("SELECT 1 FROM salescall.recording_lists WHERE list_id = %s", (list_id,))), [])

    def test_a_version_made_before_its_origin_was_approved_gets_its_set_when_the_origin_is_approved(self):
        """見直し (1) の (a)：版を作ったときに元の版の録音がまだ承認されていなかった（使い回せる録音が無い）。あとで元の版の録音を承認すると、
        中身の同じ新しい版（元の版の一覧を使う）の録音の要る部分は空になる ⇒ 承認の1か所（recordings.approve）が、その業務の版のうち、使い回せる
        録音だけで一覧がそろう版の組を作る（⛔ 前は組を作る道が無く、新しい版では話せないまま、警告も出なかった）。"""
        from salescall.core import recordings
        from tests.support import _TENANT_VOICES, VOICES
        s = self.ready()
        v0 = self.tx(s, lambda tx: bs.save_version(tx, s["business"], settings_raw()), OPERATOR)   # まだ録音の無い版
        voices = sorted(_TENANT_VOICES.get(s["tenant"], VOICES))
        sids = self.tx(s, lambda tx: [support.make_business_set(
            tx, v, v0, [recordings.Part("intro", "まだ聞いていない録音（intro）。", f"test://{v}/unheard", "9" * 64)]) for v in voices],
            OPERATOR)
        self.assertEqual(self.tx(s, lambda tx: recordings.ready_voices(tx, v0)), [])         # まだ承認していない
        v1 = self.tx(s, lambda tx: bs.derive(tx, v0, lambda r: r, cause="override", note="そのまま"), OPERATOR)
        self.assertEqual((self.tx(s, lambda tx: recordings.version_needs(tx, v1)), self.tx(s, lambda tx: recordings.ready_voices(tx, v1))),
                         (recordings.VersionNeeds(True, {}), []))                         # 中身が同じ＝元の版の一覧を使う
        for sid in sids:
            self.tx(s, lambda tx, i=sid: recordings.approve(tx, i), OPERATOR)
        self.assertEqual(self.tx(s, lambda tx: recordings.ready_voices(tx, v0)), voices)
        self.assertEqual(self.tx(s, lambda tx: recordings.ready_voices(tx, v1)), voices)     # 新しい版も、聞き直さずに話せる
        self.assertEqual(self.tx(s, lambda tx: recordings.version_needs(tx, v1)), recordings.VersionNeeds(True, {}))

    def test_a_part_is_used_again_from_any_approved_recording_of_the_business(self):
        """使い回す録音は、その業務の、同じ声の承認済みの録音のどれでも（`recordings._approved_same` の1か所。版の元をたどった近い版だけではない）。
        V0（名乗り・目的を録音済み）→ V1（目的の文を変えた。録音して承認した）→ V2（V1 に答えを足した）→ V3（目的の文を V0 に戻した）：V2 で録音が
        要るのは足した答えだけ、V3 で要るものは無い（目的の文は V0 の録音を使う）。"""
        from salescall.core import recordings
        s = self.ready()
        v0 = s["version"]
        self.w.approve_voices(s["tenant"], s["client"], v0, parts=("intro", "purpose"))
        voices = self.tx(s, lambda tx: recordings.ready_voices(tx, v0))
        purpose = recordings.ListedPart("purpose", "架空の録音（purpose・改）。")
        v1 = self.tx(s, lambda tx: bs.derive(tx, v0, lambda r: dict(r, purpose=r["purpose"] + "（改）"), cause="override",
                                             note="目的の文を変えた"), OPERATOR)
        self.assertEqual(self.tx(s, lambda tx: recordings.list_parts(tx, v1, [recordings.ListedPart("intro", "架空の録音（intro）。"),
                                                                                purpose])).parts,
                         {v: {"purpose": purpose.spoken_text} for v in voices})
        for v in voices:
            self.tx(s, lambda tx, v=v: recordings.approve(tx, recordings.complete(tx, v1, v, [recordings.Part(
                "purpose", purpose.spoken_text, f"test://{v}/purpose2", "6" * 64)])), OPERATOR)
        v2 = self.tx(s, lambda tx: bs.derive(tx, v1, self.add_answer, cause="solution", note="駐車場"), OPERATOR)
        part = bs.ANSWER_PART_PREFIX + "駐車場"
        self.assertEqual(self.tx(s, lambda tx: support.send_list(tx, v2, purpose)).parts,
                         {v: {part: "建物の裏に3台分ございます。"} for v in voices})
        v3 = self.tx(s, lambda tx: bs.derive(tx, v2, lambda r: dict(r, purpose=r["purpose"].removesuffix("（改）")), cause="override",
                                             note="目的の文を戻した"), OPERATOR)
        for v in voices:
            self.tx(s, lambda tx, v=v: recordings.approve(tx, recordings.complete(tx, v2, v, [recordings.Part(
                part, "建物の裏に3台分ございます。", f"test://{v}/{part}", "7" * 64)])), OPERATOR)
        self.assertEqual(self.tx(s, lambda tx: support.send_list(tx, v3, recordings.ListedPart("purpose", "架空の録音（purpose）。"))),
                         recordings.VersionNeeds(True, {}))
        self.assertEqual(self.tx(s, lambda tx: recordings.ready_voices(tx, v3)), voices)    # 聞き直さずに話せる

    def test_the_copy_root_follows_only_copies(self):
        """写しの元のいちばん元の版（salescall.version_copy_root。版の元をたどる1か所 version_origins から読む＝0201）：写し・写しの写しは、
        写しの元をたどった版。つながり（版を足した）の先の版は、それ自身（中身が違う）。写しの先に足した版の写しは、その足した版まで。"""
        s = self.ready()
        v0 = s["version"]
        root = lambda v: self.tx(s, lambda tx: tx.one("SELECT salescall.version_copy_root(%s) AS r", (v,))["r"])   # noqa: E731
        c1 = self.tx(s, lambda tx: bs.save_from(tx, s["business"], v0), OPERATOR)
        c2 = self.tx(s, lambda tx: bs.save_from(tx, s["business"], c1), OPERATOR)
        d1 = self.tx(s, lambda tx: bs.derive(tx, c2, self.add_answer, cause="solution", note="駐車場"), OPERATOR)
        c3 = self.tx(s, lambda tx: bs.save_from(tx, s["business"], d1), OPERATOR)
        self.assertEqual([root(v) for v in (v0, c1, c2, d1, c3)], [v0, v0, v0, d1, d1])

    def test_a_version_changed_beyond_its_answers_records_only_the_listed_parts_that_changed(self):
        """想定問答のほかの項目が違う版（目的の文を変えた）も、会話の担当の一覧のうち、承認済みの同じ録音の無い部分だけを録音していただく
        （0212。⛔ 前は CL-2 が業務設定の項目の違いから要る部分を決めていたので、この版には要る部分も警告も出ず、組を作る道も無かった）。"""
        from salescall.core import recordings
        s = self.ready()
        v0 = s["version"]
        self.w.approve_voices(s["tenant"], s["client"], v0, parts=("intro", "purpose"))
        voices = self.tx(s, lambda tx: recordings.ready_voices(tx, v0))
        self.assertTrue(voices)
        v1 = self.tx(s, lambda tx: bs.derive(tx, v0, lambda r: dict(r, purpose="ご予約の内容を伺う"), cause="override", note="目的"),
                     OPERATOR)
        self.assertEqual((self.tx(s, lambda tx: recordings.ready_voices(tx, v1)), self.tx(s, lambda tx: recordings.version_needs(tx, v1))),
                         ([], recordings.VersionNeeds(False, {})))
        new = recordings.ListedPart("purpose", "ご予約の内容を伺うお電話です。")
        self.assertEqual(self.tx(s, lambda tx: recordings.list_parts(tx, v1, [recordings.ListedPart("intro", "架空の録音（intro）。"), new]),
                                 OPERATOR).parts, {v: {"purpose": new.spoken_text} for v in voices})
        for v in voices:
            self.tx(s, lambda tx, v=v: recordings.approve(tx, recordings.complete(tx, v1, v, [recordings.Part(
                "purpose", new.spoken_text, f"test://{v}/purpose", "6" * 64)])), OPERATOR)
        self.assertEqual(self.tx(s, lambda tx: recordings.ready_voices(tx, v1)), voices)

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
    def test_answers_added_before_recording_are_recorded_together_in_the_latest_version(self):
        """(2) の2：録音していない途中の版から足した版（V0 は録音済み → V1 はまだ → V2）は、V2 の一覧のうち、承認済みの同じ録音の無い部分
        （V1 と V2 で足した答えの両方）をまとめて録音していただく（version_needs・complete・知らせ recording_needed。⛔ 前は V2 には録音の要る
        部分も警告も出ず、complete も断り、録音が2回に分かれた）。"""
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
        self.tx(s, lambda tx: support.send_list(tx, v2), OPERATOR)
        self.assertEqual(self.tx(s, lambda tx: recordings.version_needs(tx, v2)).parts, {v: want for v in voices})
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
        """(2) の2：録音していない途中の版（V1）へ手で戻した写しは、V1 の一覧（中身が同じ）を使い、そのうち承認済みの同じ録音の無い部分の録音を
        求める（警告と知らせ。⛔ 前は写しの元を知らず、組も警告も無いまま話せなかった）。一覧の無い版の写しは「一覧がまだ」。"""
        from salescall.core import recordings
        s = self.ready()
        v0 = s["version"]
        self.w.approve_voices(s["tenant"], s["client"], v0)
        voices = self.tx(s, lambda tx: recordings.ready_voices(tx, v0))
        self.tx(s, lambda tx: quality_stage.record_mock(tx, v0, GOOD), OPERATOR)
        v1 = self.tx(s, lambda tx: bs.derive(tx, v0, self.add_answer, cause="solution", note="駐車場"), OPERATOR)
        self.tx(s, lambda tx: support.send_list(tx, v1), OPERATOR)
        v2 = self.tx(s, lambda tx: bs.derive(tx, v1, lambda r: dict(r, answers=list(r["answers"]) + [
            {"name": "営業時間", "text": "平日の9時から18時でございます。"}]), cause="solution", note="営業時間"), OPERATOR)
        self.w.approve_voices(s["tenant"], s["client"], v2, parts=("intro", "answer_駐車場", "answer_営業時間"))
        self.assertEqual(self.tx(s, lambda tx: bs.adopt_ready(tx)), [(s["run"], v2)])
        self.tx(s, lambda tx: bs.assign(tx, s["run"], v1, reason="試験：手で録音していない版に戻す"), OPERATOR)
        back = self.current(s)
        self.assertNotIn(back, (v0, v1, v2))
        self.assertEqual(self.tx(s, lambda tx: bs.version_origins(tx, back)), [v1, v0])
        part = bs.ANSWER_PART_PREFIX + "駐車場"
        self.assertEqual(self.tx(s, lambda tx: recordings.version_needs(tx, back)),
                         recordings.VersionNeeds(True, {v: {part: "建物の裏に3台分ございます。"} for v in voices}))
        self.assertTrue(self.tx(s, lambda tx: tx.one("SELECT 1 FROM salescall.notification_events WHERE event_key = %s",
                                                     (f"recording_needed:{back}",))))
        other = self.tx(s, lambda tx: bs.save_from(tx, s["business"], v2), OPERATOR)          # 写しの元の一覧は、承認済みの録音でそろう
        self.assertEqual(self.tx(s, lambda tx: recordings.ready_voices(tx, other)), voices)
        plain = self.tx(s, lambda tx: bs.derive(tx, v0, lambda r: dict(r, purpose="ご予約の内容を伺う"), cause="override", note="目的"),
                        OPERATOR)
        copy = self.tx(s, lambda tx: bs.save_from(tx, s["business"], plain), OPERATOR)        # 一覧の無い版の写し
        self.assertEqual(self.tx(s, lambda tx: recordings.version_needs(tx, copy)), recordings.VersionNeeds(False, {}))
        self.assertTrue(self.tx(s, lambda tx: tx.one("SELECT 1 FROM salescall.notification_events WHERE event_key = %s",
                                                     (f"recording_list_needed:{copy}",))))

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
        """使わない標準の問い（answers_off）だけが違う版は、標準の録音から流す部分だけが変わる。会話の担当の一覧の業務の録音の部分は同じなので、
        一覧が届けば、承認済みの録音でその場で組ができる（聞き直さない）。"""
        from salescall.core import recordings
        s = self.ready()
        with self.w.standard(STD, voices=("Leda", "Sadachbia")) as std:
            v = self.tx(s, lambda tx: bs.save_version(tx, s["business"], settings_raw(), standard_id=std), OPERATOR)
            self.w.approve_voices(s["tenant"], s["client"], v)
            voices = self.tx(s, lambda tx: recordings.ready_voices(tx, v))
            self.assertTrue(voices)
            off = self.tx(s, lambda tx: bs.derive(tx, v, lambda r: dict(r, answers_off=["会社の場所"]), cause="override",
                                                  note="会社の場所を使わない"), OPERATOR)
            self.assertEqual(self.tx(s, lambda tx: support.send_list(tx, off), OPERATOR), recordings.VersionNeeds(True, {}))
            self.assertEqual(self.tx(s, lambda tx: recordings.ready_voices(tx, off)), voices)   # 聞き直さずに話せる

    def test_a_version_whose_name_reading_changed_records_only_the_parts_that_speak_it(self):
        """前からある名前の読みが変わった版は、その名前を話す部分（会話の担当の一覧で文が変わる部分）だけを録音していただく（ほかの部分は
        承認済みの同じ録音を使う。0212。⛔ 前は CL-2 がどの部分がその名前を話すかを決められず、録音を何も使わなかった）。"""
        from salescall.core import recordings
        s = self.ready()
        r = settings_raw()
        r["names"] = list(r["names"]) + [{"text": "西海", "reading": "さいかい", "source": "script", "reading_by": "given",
                                          "candidates": [], "likely": None}]
        v0 = self.tx(s, lambda tx: bs.save_version(tx, s["business"], r), OPERATOR)
        self.w.approve_voices(s["tenant"], s["client"], v0, parts=("intro", "info"))
        voices = self.tx(s, lambda tx: recordings.ready_voices(tx, v0))
        self.assertTrue(voices)
        v1 = self.tx(s, lambda tx: bs.derive(tx, v0, lambda raw: dict(raw, names=[dict(n, reading="にしうみ") if n["text"] == "西海" else n
                                                                             for n in raw["names"]]),
                                             cause="override", note="西海の読み"), OPERATOR)
        new = recordings.ListedPart("info", "「にしうみ」の工場で作っています。")
        self.assertEqual(self.tx(s, lambda tx: recordings.list_parts(tx, v1, [recordings.ListedPart("intro", "架空の録音（intro）。"), new]),
                                 OPERATOR).parts, {v: {"info": new.spoken_text} for v in voices})

    def test_kept_parts_keep_the_readings_they_were_recorded_with(self):
        """使い回す部分は、録音で書いた名前の読みも使い回す（承認の中身の鍵に入る。落とすと、その名前を話す部分は組を作れない）。読みの違う
        録音は使い回さない（一覧の読みと同じものだけ）。"""
        from salescall.core import recordings
        s = self.ready()
        r = settings_raw()
        r["names"] = list(r["names"]) + [{"text": "中島", "reading": None, "source": "script", "reading_by": None,
                                          "candidates": ["なかじま", "なかしま"], "likely": "なかじま"}]
        r["answers"] = [{"name": "担当", "text": "中島がご案内いたします。"}]
        v0 = self.tx(s, lambda tx: bs.save_version(tx, s["business"], r), OPERATOR)
        self.tx(s, lambda tx: recordings.choose_reading(tx, v0, "中島", "なかしま"), OPERATOR)
        part = bs.ANSWER_PART_PREFIX + "担当"
        spoken = recordings.ListedPart(part, "「なかしま」がご案内いたします。", {"中島": "なかしま"})

        def record(tx):
            for v in ("Leda", "Sadachbia"):
                recordings.approve(tx, support.make_business_set(tx, v, v0, [
                    recordings.Part("intro", "架空の録音（intro）。", f"test://{v}/intro", "0" * 64),
                    recordings.Part(part, spoken.spoken_text, f"test://{v}/{part}", "4" * 64, {"中島": "なかしま"})]))
        self.tx(s, record, OPERATOR)
        voices = self.tx(s, lambda tx: recordings.ready_voices(tx, v0))
        self.assertTrue(voices)
        v1 = self.tx(s, lambda tx: bs.derive(tx, v0, self.add_answer, cause="solution", note="駐車場"), OPERATOR)
        parking = bs.ANSWER_PART_PREFIX + "駐車場"
        need = self.tx(s, lambda tx: recordings.list_parts(tx, v1, [recordings.ListedPart("intro", "架空の録音（intro）。"), spoken,
                                                                    recordings.ListedPart(parking, "建物の裏に3台分ございます。")]), OPERATOR)
        self.assertEqual(need.parts, {v: {parking: "建物の裏に3台分ございます。"} for v in voices})
        sid = self.tx(s, lambda tx: recordings.complete(tx, v1, voices[0], [recordings.Part(
            parking, "建物の裏に3台分ございます。", "test://new", "5" * 64)]), OPERATOR)
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
