"""品質の段（Q22）と模擬通話の合格（DESIGN.md 10-1）の試験。本物の門・出口・締めの処理を通す。"""

from __future__ import annotations

import unittest
import uuid
from dataclasses import replace
from uuid import UUID

from salescall.core import approval, attempts, business_rules, fix_requests, next_action, notify, quality_stage, stops
from salescall.core.access import Rejected
from salescall.core.attempts import VerificationSummary
from salescall.core.phone import normalize
from salescall.core.quality_stage import MockResult
from salescall.core import gate
from tests.support import ADMIN, OPERATOR, SYSTEM, fictional_jp_fixed, owner_access, rules_raw, said
from tests.test_settle import TALK, SettleBase, rep

GOOD = MockResult(calls=10, value_hits=40, value_total=40, judgment_hits=10, judgment_total=10, forbidden_speech=0,
                  evidence="試験：台本 v1")


class Base(SettleBase):
    def fresh(self, *, stage_cap=None):
        """段の無い版から始める行（模擬通話にまだ合格していない依頼者）。stage_cap を渡すと、その件数の最初の段を開く。"""
        s = self.ready()
        own_version = self.w.add_version(s["tenant"], s["client"], s["business"])
        self.tx(s, lambda tx: tx.execute(
            "INSERT INTO salescall.questions (tenant_id, client_id, version_id, question_id, position, type_name, required)"
            " VALUES (%s, %s, %s, 'Q1', 1, 'datetime', true)", (s["tenant"], s["client"], own_version)), OPERATOR)
        self.tx(s, lambda tx: business_rules.save(tx, own_version, rules_raw()), OPERATOR)
        self.tx(s, lambda tx: tx.execute(
            "INSERT INTO salescall.run_versions (tenant_id, client_id, run_id, business_id, seq, version_id, assigned_by, reason)"
            " VALUES (%s, %s, %s, %s, 2, %s, '試験', '新しい版')", (s["tenant"], s["client"], s["run"], s["business"],
                                                                 own_version)), OPERATOR)
        s = dict(s, version=own_version)
        if stage_cap is not None:
            self.tx(s, lambda tx: quality_stage.open_stage(tx, own_version, 1, stage_cap, evidence="試験"), OPERATOR)
        return s

    def newer_version(self, s):
        v = self.w.add_version(s["tenant"], s["client"], s["business"])
        self.tx(s, lambda tx: tx.execute(
            "INSERT INTO salescall.questions (tenant_id, client_id, version_id, question_id, position, type_name, required)"
            " VALUES (%s, %s, %s, 'Q1', 1, 'datetime', true)", (s["tenant"], s["client"], v)), OPERATOR)
        self.tx(s, lambda tx: business_rules.save(tx, v, rules_raw()), OPERATOR)
        self.tx(s, lambda tx: tx.execute(
            "INSERT INTO salescall.run_versions (tenant_id, client_id, run_id, business_id, version_id, assigned_by, reason)"
            " VALUES (%s, %s, %s, %s, %s, '試験', '直した版')", (s["tenant"], s["client"], s["run"], s["business"], v)),
            OPERATOR)
        return dict(s, version=v)

    def verified_call(self, s, summary, **kw):
        aid = self.call(s, verify=None, **kw)
        self.tx(s, lambda tx: attempts.record_verification(tx, aid, summary=summary))
        return self.settle(s, aid)

    def second_row(self, s):
        phone = normalize(fictional_jp_fixed()).e164
        k = self.w.contact(s["tenant"], s["client"], phone)
        t = self.w.task(s["tenant"], s["client"], s["run"], k)
        self.tx(s, lambda tx: next_action.schedule_now(tx, t, cause="試験"))
        return dict(s, task=t, contact=k, phone=phone)

    def business_stops(self, s):
        return self.tx(s, lambda tx: stops.open_stops(tx, tenant_id=s["tenant"], client_id=s["client"],
                                                      business_id=s["business"]))


class Mock(Base):
    def test_first_business_of_a_client_waits_for_the_mock(self):
        s = self.fresh()
        self.assertEqual(self.tx(s, lambda tx: quality_stage.prepare(tx, s["version"])), "mock_needed")
        self.assertEqual(self.reason(s), "version_not_ready")
        for bad in (MockResult(9, 40, 40, 9, 9, 0, "本数が足りない"), MockResult(10, 30, 40, 10, 10, 0, "値が合わない"),
                    MockResult(10, 40, 40, 10, 10, 1, "言ってはいけないこと")):
            with self.subTest(bad.evidence):
                got = self.tx(s, lambda tx: quality_stage.record_mock(tx, s["version"], bad))
                self.assertEqual((got["passed"], got["outcome"]), (False, "failed"))
        self.assertIsNone(self.tx(s, lambda tx: quality_stage.current(tx, s["version"])))
        got = self.tx(s, lambda tx: quality_stage.record_mock(tx, s["version"], GOOD))
        self.assertEqual((got["passed"], got["outcome"]), (True, "opened"))
        stage = self.tx(s, lambda tx: quality_stage.current(tx, s["version"]))
        self.assertEqual((stage["stage_no"], stage["cap"]), (1, 20))
        newer = self.newer_version(s)                                # 依頼者ごとに最初のみ：次の版はすぐ始める
        self.assertEqual(self.tx(s, lambda tx: quality_stage.prepare(tx, newer["version"])), "opened")


class Stages(Base):
    """検証モードの品質の門（2026-10-16 ユーザー：言ってはいけない・品質の基準・判定できない・段階的な立ち上げは検証モードで）。"""
    def fresh(self, **kw):
        s = super().fresh(**kw)
        self.put_client(s, "approval.human", True)                     # 検証モード（契約者が行を承認してから発信する）
        self.tx(s, lambda tx: approval.approve_task(tx, s["task"]), OPERATOR)
        return s

    def second_row(self, s):
        r = super().second_row(s)
        self.tx(r, lambda tx: approval.approve_task(tx, r["task"]), OPERATOR)
        return r

    def test_forbidden_speech_stops_at_once_and_only_a_new_version_resumes(self):
        s = self.fresh(stage_cap=20)
        got = self.verified_call(s, said(3))      # 言ってはいけないことが1件
        self.assertEqual([r["cause"] for r in self.business_stops(s)], ["forbidden_speech"])
        self.tx(s, lambda tx: next_action.schedule_now(tx, s["task"], cause="試験：予定をいまに"))
        self.assertEqual(self.reason(s), "stopped")
        self.assertEqual(self.tx(s, lambda tx: quality_stage.prepare(tx, s["version"])), "already")
        again = self.tx(s, lambda tx: quality_stage.record_mock(tx, s["version"], GOOD))
        self.assertEqual(again["outcome"], "stopped_version")          # 止まった版そのものでは再開しない
        self.assertEqual(len(self.business_stops(s)), 1)
        newer = self.newer_version(s)
        self.assertEqual(self.tx(s, lambda tx: quality_stage.prepare(tx, newer["version"])), "mock_needed")
        fixed = self.tx(s, lambda tx: quality_stage.record_mock(tx, newer["version"], GOOD))
        self.assertEqual(fixed["outcome"], "opened")
        self.assertEqual(self.business_stops(s), [])                   # 新しい版の合格で、自動で再開
        self.assertIsNotNone(got)

    def test_switching_to_production_releases_the_quality_stop(self):
        """検証モードで品質で止めた業務を本番に切り替えたら、システムが品質の停止を解き、止まっていたあいだに割り当てた版も始める（品質の門は
        検証モードの業務だけ＝原則9。決まった間隔の仕事が quality_stage.release_production の1か所を呼ぶ）。検証モードのままなら解かない。
        ⛔ 2026-11-18 の6回目の見直しまで：本番に切り替えても止まったまま（解く道は新しい版の模擬通話の合格だけ）で、お詫びのほかの電話が
        かからなかった。"""
        s = self.fresh()
        self.assertEqual(self.tx(s, lambda tx: quality_stage.record_mock(tx, s["version"], GOOD))["outcome"], "opened")   # 依頼者の最初の合格
        self.verified_call(s, said(3))      # 言ってはいけないことが1件
        self.assertEqual([r["cause"] for r in self.business_stops(s)], ["forbidden_speech"])
        newer = self.newer_version(s)
        self.assertEqual(self.tx(newer, lambda tx: quality_stage.prepare(tx, newer["version"])), "mock_needed")
        self.assertEqual(self.tx(s, quality_stage.release_production), [])       # 検証モードのあいだは解かない
        self.assertEqual(len(self.business_stops(s)), 1)
        self.put_client(s, "approval.human", False)                             # 本番に切り替えた
        got = self.tx(s, quality_stage.release_production)
        self.assertEqual((len(got), self.business_stops(s)), (1, []))
        self.assertIsNotNone(self.tx(s, lambda tx: quality_stage.current(tx, newer["version"])))   # 待っていた版も始めた
        self.assertEqual(self.tx(s, quality_stage.release_production), [])       # 1回だけ
        self.assertIsInstance(self.claim(Base.second_row(self, s)), gate.Claim)   # 業務はまたかけられる（本番なので承認は要らない）
        self.assertEqual(self.tx(s, lambda tx: quality_stage.allowance(tx, s["version"])), (True, None))   # 本番：止まった版でもかける
        # 検証モードに戻したら、止まった版そのものは、まだかけない（止まった版そのものでは再開しない＝2026-10-16。⛔ 2026-11-18 の7回目の見直しまで：
        # 模擬通話なしでそのまま再開した）。止まっていたあいだに割り当てた新しい版は、かけられる
        self.put_client(s, "approval.human", True)
        self.assertEqual(self.tx(s, lambda tx: quality_stage.allowance(tx, s["version"])), (False, "version_not_ready"))
        self.assertEqual(self.tx(s, lambda tx: quality_stage.allowance(tx, newer["version"])), (True, None))

    def test_a_copy_of_the_stopped_version_is_the_stopped_version(self):
        """止まった版の写し（以前の版へ戻す・手で前の版に戻す＝business_settings.save_from）も、止まった版そのもの（中身が同じ）：模擬通話に
        合格しても再開しない（品質の段の1か所 `_stopped_version` が、写しの元をたどった版＝salescall.version_copy_root で比べる。0197）。
        ⛔ 2026-11-07 の第三者の見直し (2) の5：前は版の番号で比べたので、写しで同じ中身のまま再開できた。"""
        from salescall.core import business_settings
        s = self.fresh(stage_cap=20)
        self.verified_call(s, said(3))      # 言ってはいけないことが1件
        self.assertEqual([r["cause"] for r in self.business_stops(s)], ["forbidden_speech"])
        copy = self.tx(s, lambda tx: business_settings.restore(tx, s["version"]), OPERATOR)
        again = self.tx(s, lambda tx: business_settings.restore(tx, copy), OPERATOR)          # 写しの写しも
        for v in (copy, again):
            with self.subTest(version=v):
                self.assertEqual(self.tx(s, lambda tx, v=v: quality_stage.start_kind(tx, v)), "stopped_version")
        got = self.tx(s, lambda tx: quality_stage.record_mock(tx, again, GOOD))
        self.assertEqual(got["outcome"], "stopped_version")
        self.assertEqual(len(self.business_stops(s)), 1)

    def test_a_person_resume_measures_the_version_again(self):
        s = self.fresh(stage_cap=2)
        self.verified_call(s, said(3), items=[("Q1", "confirmed", None)])
        sid = self.business_stops(s)[0]["event_id"]
        why = self.tx(s, lambda tx: stops.explain(tx, sid))
        self.assertEqual(why["stop"]["cause"], "forbidden_speech")
        self.assertEqual(why["cause"]["lets_through"], ["apology"])   # 止めているあいだも通す電話（画面に出す。表の1か所から）
        self.assertEqual(why["quality"]["metrics"]["forbidden_speech"]["hits"], 1)   # どの指標が外れたか
        self.assertEqual(len(why["held_results"]), 1)                 # 検証モード：契約者の確かめを待つ結果
        self.tx(s, lambda tx: stops.resume(tx, sid, reason="試験：場面を聞いて、言い方の問題ではないと判断"), OPERATOR)
        self.assertEqual(self.tx(s, lambda tx: quality_stage.used(tx, s["version"])), 0)   # 測り直し
        self.verified_call(self.second_row(s), VerificationSummary(2, 0, 0), items=[("Q1", "confirmed", None)])
        self.assertEqual(self.business_stops(s), [])                   # 前の1件で、また止まらない
        self.verified_call(self.second_row(s), said(), items=[("Q1", "confirmed", None)])
        self.assertEqual([r["cause"] for r in self.business_stops(s)], ["forbidden_speech"])   # 解いたあとの分は数える

    def test_one_unknown_stops_with_its_cause_and_a_mock_with_the_scene_resumes(self):
        """判定できない：1件でその業務を止め、原因を知らせる。その場面を入れた模擬通話の合格で、同じ版でも再開
        （2026-10-16 ユーザー決定：(a)(c)(d)）。"""
        s = self.fresh(stage_cap=20)
        self.verified_call(s, VerificationSummary(1, 0, 0), report=rep(cut=None))
        stops_now = self.business_stops(s)
        self.assertEqual([r["cause"] for r in stops_now], ["judge_unknown"])
        self.assertIn("切れ方の材料が報告に無い", stops_now[0]["reason"])       # 何が原因かを、止めた理由に書く
        why = self.tx(s, lambda tx: stops.explain(tx, stops_now[0]["event_id"]))
        unk = why["quality"]["metrics"]["unknown_judgments"]
        self.assertEqual((unk["cause"], len(unk["attempts"])), ("cut_missing", 1))
        no_scenes = self.tx(s, lambda tx: quality_stage.record_mock(tx, s["version"], GOOD))
        self.assertEqual(no_scenes["outcome"], "scenes_missing")       # 止めた原因の場面を入れていない
        self.assertEqual(len(self.business_stops(s)), 1)
        with_scenes = replace(GOOD, scenes=tuple(UUID(a) for a in unk["attempts"]), evidence="試験：場面を入れた台本")
        got = self.tx(s, lambda tx: quality_stage.record_mock(tx, s["version"], with_scenes))
        self.assertEqual(got["outcome"], "opened")
        self.assertEqual(self.business_stops(s), [])                   # 同じ版でも再開（原因は業務設定とは限らない）

    def test_resume_needs_every_unknown_call_since_the_stop(self):
        """止めたあとに締めた別の通話（回線が複数のとき）も判定できなかったら、その場面も入れないと再開しない。"""
        s = self.fresh(stage_cap=20)
        s2 = self.second_row(s)
        late = self.call(s2, report=rep(("reception",)), verify=None)      # 先にかけ始めていた通話
        self.verified_call(s, VerificationSummary(1, 0, 0), report=rep(cut=None))   # 判定できない ⇒ 止まる
        sid = self.business_stops(s)[0]["event_id"]
        first = self.tx(s, lambda tx: stops.explain(tx, sid))["quality"]["metrics"]["unknown_judgments"]["attempts"]
        self.tx(s, lambda tx: attempts.record_verification(tx, late, summary=VerificationSummary(1, 0, 0)))
        self.settle(s2, late)                                              # 止まったあとに締めた：これも判定できない
        only_first = replace(GOOD, scenes=tuple(UUID(a) for a in first), evidence="試験：最初の場面だけ")
        got = self.tx(s, lambda tx: quality_stage.record_mock(tx, s["version"], only_first))
        self.assertEqual(got["outcome"], "scenes_missing")
        both = replace(GOOD, scenes=tuple(UUID(a) for a in first) + (late,), evidence="試験：両方の場面")
        self.assertEqual(self.tx(s, lambda tx: quality_stage.record_mock(tx, s["version"], both))["outcome"], "opened")

    def test_an_unknown_while_already_stopped_is_not_forgotten(self):
        """言ってはいけないことで止まったあとに締めた別の通話が判定できなかったら、新しい版の模擬通話にも、その場面を求める
        （2026-10-16 見直し：品質の停止がもうあるあいだは評価しないので、前は判定できないを見落として再開した）。"""
        s = self.fresh(stage_cap=20)
        s2 = self.second_row(s)
        late = self.call(s2, report=rep(("reception",)), verify=None)      # 先にかけ始めていた通話
        self.verified_call(s, said(3))                                      # 言ってはいけないこと ⇒ 止まる
        self.tx(s, lambda tx: attempts.record_verification(tx, late, summary=VerificationSummary(1, 0, 0)))
        self.settle(s2, late)                                              # 止まったあとに締めた：判定できない
        newer = self.newer_version(s)
        got = self.tx(s, lambda tx: quality_stage.record_mock(tx, newer["version"], GOOD))
        self.assertEqual(got["outcome"], "scenes_missing")
        with_scene = replace(GOOD, scenes=(late,), evidence="試験：判定できなかった場面を入れた")
        self.assertEqual(self.tx(s, lambda tx: quality_stage.record_mock(tx, newer["version"], with_scene))["outcome"], "opened")

    def test_stage_end_advances_when_inside(self):
        s = self.fresh(stage_cap=1)
        self.verified_call(s, VerificationSummary(3, 0, 0), items=[("Q1", "confirmed", None)])
        stage = self.tx(s, lambda tx: quality_stage.current(tx, s["version"]))
        self.assertEqual((stage["stage_no"], stage["cap"]), (2, 100))
        ev = self.tx(s, lambda tx: tx.one("SELECT outcome, metrics FROM salescall.quality_evaluations WHERE version_id = %s"
                                          " ORDER BY eval_id DESC LIMIT 1", (s["version"],)))
        self.assertEqual(ev["outcome"], "advance")
        self.assertEqual(ev["metrics"]["ear_disagreement"]["total"], 3)          # 画面は記録を読む（数え直さない）
        notices = self.tx(s, lambda tx: tx.all("SELECT kind FROM salescall.notification_events WHERE kind = 'quality_stage'"
                                               " AND refs ->> 'version_id' = %s", (str(s["version"]),)))
        self.assertEqual(len(notices), 1)

    def test_stage_end_stops_when_a_ratio_is_outside(self):
        s = self.fresh(stage_cap=1)
        self.verified_call(s, VerificationSummary(4, 2, 0), items=[("Q1", "confirmed", None)])   # 食い違い 50%
        self.assertEqual([r["cause"] for r in self.business_stops(s)], ["quality_gate"])

    def test_ratios_wait_for_the_stage_end(self):
        s = self.fresh(stage_cap=5)
        self.verified_call(s, VerificationSummary(4, 2, 0), items=[("Q1", "confirmed", None)])   # まだ段の途中
        self.assertEqual(self.business_stops(s), [])

    def test_done_needs_the_numbers(self):
        s = self.fresh(stage_cap=5)
        aid = self.call(s, verify=None)
        with self.assertRaises(ValueError):
            self.tx(s, lambda tx: attempts.record_verification(tx, aid, summary=None))
        with self.assertRaises(Rejected) as e:                         # 処理を通らずに書いても、データベースが拒む
            self.tx(s, lambda tx: tx.execute("UPDATE salescall.attempts SET verification = 'done' WHERE attempt_id = %s",
                                             (aid,)))
        self.assertEqual(e.exception.constraint, "attempts_verification_needs_summary")


if __name__ == "__main__":
    unittest.main()


class BusinessMetrics(Base):
    """業務（と声の型）・期間の指標（core/metrics.business。CL-3 の 57）：数え方は品質の段と同じ1か所（core/metrics.counts）。"""

    def test_the_business_metrics_count_like_the_quality_stage(self):
        from datetime import timedelta
        from salescall.core import metrics
        s = self.fresh(stage_cap=5)
        start = self.tx(s, lambda tx: tx.one("SELECT now() AS t")["t"]) - timedelta(minutes=1)
        self.verified_call(s, VerificationSummary(4, 1, 0), items=[("Q1", "confirmed", None)])   # 1行目：話せて完了
        u = self.second_row(s)
        self.verified_call(u, VerificationSummary(2, 0, 0))                                       # 2行目：話せたが取れていない
        until = self.tx(s, lambda tx: tx.one("SELECT now() AS t")["t"]) + timedelta(minutes=1)
        got = self.tx(s, lambda tx: metrics.business(tx, s["business"], since=start, until=until))
        self.assertEqual((got["dialed"], got["talked"], got["completion"]["hits"], got["completion"]["total"]), (2, 2, 1, 2))
        self.assertEqual((got["ear_disagreement"]["hits"], got["ear_disagreement"]["total"], got["redial"]["hits"]), (1, 6, 0))
        self.assertIsNotNone(got["average_talk_seconds"])
        # 必須の質問の「取れた」「-」「✕」（判定に使った答え・印の分け方は judge.item_mark の1か所。CL-3 の 42）
        tally = self.tx(s, lambda tx: metrics.required_items(tx, s["business"], since=start, until=until))
        self.assertEqual(tally, {"Q1": {"taken": 1, "dont_know": 0, "refused": 0, "open": 1, "not_asked": 0}})
        self.assertEqual((got["required_taken"]["hits"], got["required_taken"]["total"]), (1, 2))
        # 品質の段と同じ数（同じ1か所から数える）
        stage = self.tx(s, lambda tx: quality_stage.metrics(tx, s["version"]))
        self.assertEqual((stage["ear_disagreement"]["hits"], stage["ear_disagreement"]["total"]),
                         (got["ear_disagreement"]["hits"], got["ear_disagreement"]["total"]))
        # 期間の外・ほかの声の型の通話は数えない
        empty = self.tx(s, lambda tx: metrics.business(tx, s["business"], since=until, until=until + timedelta(days=1)))
        self.assertEqual((empty["dialed"], empty["completion"]["value"], empty["average_talk_seconds"]), (0, None, None))
        other = self.tx(s, lambda tx: metrics.business(tx, s["business"], since=start, until=until, voice_type="架空の声の型"))
        self.assertEqual(other["dialed"], 0)
        # ダッシュボードの「今日」の数（metrics.day。CL-3 の 41）：留守番電話の通話は、電話に出た数（トータル通話数）にだけ入る
        from tests.support import make_report
        w = self.second_row(s)
        self.verified_call(w, VerificationSummary(0, 0, 0), report=make_report(("voicemail",), end="voicemail"))
        r = self.second_row(s)                                                     # 受付の方とだけ話した（お呼びした方は不在）：お客様と話したが、目的のお客様とは話せていない
        self.verified_call(r, VerificationSummary(0, 0, 0), report=make_report(("reception",), not_reached="absent"))
        today = self.tx(s, lambda tx: tx.one("SELECT salescall.jst(now())::date AS d")["d"])
        self.assertEqual(self.tx(s, lambda tx: metrics.day(tx, today)), {"dialed": 4, "answered": 4, "talked": 3, "reached": 2})
        self.assertEqual(self.tx(s, lambda tx: metrics.day(tx, today, business_id=uuid.uuid4())),
                         {"dialed": 0, "answered": 0, "talked": 0, "reached": 0})
        self.assertEqual(self.tx(s, lambda tx: metrics.day(tx, today - timedelta(days=1)))["dialed"], 0)
        # 同じ行の2本目は再発信
        from tests import support
        now = self.tx(s, lambda tx: tx.one("SELECT now() AS t")["t"])
        self.tx(s, lambda tx: support.schedule_call(tx, s["task"], now, contact_id=s["contact"], cause="試験：お客様と約束したかけ直し",
                                                       promised=True))   # 間隔の歯止めは門の試験で見る
        self.verified_call(s, VerificationSummary(1, 0, 0), items=[("Q1", "confirmed", None)])
        later = self.tx(s, lambda tx: metrics.business(tx, s["business"], since=start, until=until + timedelta(minutes=5)))
        self.assertEqual((later["dialed"], later["redial"]["hits"]), (5, 1))   # 留守番電話の行・受付の方だけの行を足したので5本

    def test_the_ai_voice_rates_are_not_counted_until_the_verification_counts_them(self):
        """会話AIの声の率（黙り・声の途切れ・言葉の抜け・同じ文の2回・英語。CL-1 の連絡帳 4-221）：通話後の検証が数えた通話が範囲に無ければ「まだ数えていない」
        （None。0 と言わない＝原則10）。数えた通話があれば、見つかったターン÷確かめたターン（確かめたターンが0なら率は出さない）。数えていない通話は、率に入らない。"""
        from datetime import timedelta
        from salescall.core import metrics
        s = self.fresh(stage_cap=5)
        start = self.tx(s, lambda tx: tx.one("SELECT now() AS t")["t"]) - timedelta(minutes=1)
        self.verified_call(s, VerificationSummary(1, 0, 0))                                       # 数えていない通話（検証の部品の前）
        span = lambda: self.tx(s, lambda tx: metrics.business(tx, s["business"], since=start, until=start + timedelta(hours=1)))["ai_voice"]
        self.assertEqual(span(), dict.fromkeys(attempts.VOICE_KINDS))
        voice = {k: (0, 0) for k in attempts.VOICE_KINDS} | {"silent": (10, 1), "cut_off": (8, 2)}
        self.verified_call(self.second_row(s), VerificationSummary(1, 0, 0, ai_voice=voice))
        self.verified_call(self.second_row(s), VerificationSummary(1, 0, 0, ai_voice=voice | {"silent": (10, 0)}))
        got = span()
        self.assertEqual((got["silent"]["hits"], got["silent"]["total"], got["silent"]["value"]), (1, 20, 0.05))
        self.assertEqual((got["cut_off"]["hits"], got["cut_off"]["total"]), (4, 16))
        self.assertEqual((got["wrong_language"]["total"], got["wrong_language"]["value"]), (0, None))   # 確かめたターンが無い：率を出さない

    def test_the_ai_voice_counts_are_checked_and_kept_only_with_a_recorded_check(self):
        """会話AIの声の数は、種類のすべてを (確かめた数, 見つかった数) で渡す（見つかった数は確かめた数まで）。録音なしで済ませた確かめには持てない。
        処理を通らずに書いても、データベースが、録音で確かめた検証の結果の無い通話には書かせない（0274）。"""
        full = {k: (1, 0) for k in attempts.VOICE_KINDS}
        for bad in ({"silent": (1, 0)}, full | {"silent": (1, 2)}, full | {"silent": (1,)}, full | {"silent": (True, False)}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                VerificationSummary(1, 0, 0, ai_voice=bad)
        with self.assertRaises(ValueError):
            VerificationSummary(0, 0, 0, recording_missing=True, ai_voice=full)
        s = self.fresh(stage_cap=5)
        aid = self.call(s, verify=None)
        with self.assertRaises(Rejected) as e:
            self.tx(s, lambda tx: tx.execute("INSERT INTO salescall.verification_voice (tenant_id, client_id, attempt_id, kind, checked, found)"
                                             " SELECT tenant_id, client_id, attempt_id, 'silent', 1, 0 FROM salescall.attempts WHERE attempt_id = %s",
                                             (aid,)))
        self.assertEqual(e.exception.constraint, "verification_voice_after_summary")
        self.tx(s, lambda tx: tx.execute("INSERT INTO salescall.verification_summaries (tenant_id, client_id, attempt_id, items_checked, items_disagreed,"
                                         " forbidden_speech, recording_missing) SELECT tenant_id, client_id, attempt_id, 0, 0, 0, true"
                                         " FROM salescall.attempts WHERE attempt_id = %s", (aid,)))   # 録音なしで済ませた確かめ
        with self.assertRaises(Rejected) as e:
            self.tx(s, lambda tx: tx.execute("INSERT INTO salescall.verification_voice (tenant_id, client_id, attempt_id, kind, checked, found)"
                                             " SELECT tenant_id, client_id, attempt_id, 'silent', 1, 0 FROM salescall.attempts WHERE attempt_id = %s",
                                             (aid,)))
        self.assertEqual(e.exception.constraint, "verification_voice_after_summary")
        other = self.second_row(s)
        checked = self.call(other, verify=None)
        self.tx(other, lambda tx: attempts.record_verification(tx, checked, summary=VerificationSummary(1, 0, 0)))
        with self.assertRaises(Rejected) as e:                         # 見つかった数は確かめた数まで
            self.tx(other, lambda tx: tx.execute("INSERT INTO salescall.verification_voice (tenant_id, client_id, attempt_id, kind, checked, found)"
                                                 " SELECT tenant_id, client_id, attempt_id, 'silent', 1, 2 FROM salescall.attempts WHERE attempt_id = %s",
                                                 (checked,)))
        self.assertEqual(e.exception.constraint, "verification_voice_found_within")


class Production(Base):
    """本番（検証モードでない業務）の品質の見方：止めない（2026-10-22 ユーザー：「前にも言ったはずで本番は止めない（担当への修正依頼と
    管理者への知らせ）ただし、大問題は止める」）。1件ごとの手当て（その行を終える・判定待ち）と、通話ごとの契約者と管理者への知らせ・
    直す担当への修正依頼（同じ不具合は1つ）は締めの処理がする。段階的な立ち上げ（段の件数）も、品質の評価もしない。⛔ 見直し23 の (3)：
    撤回された 2026-10-16 の決定の名残（その日のうちに同じ原因が設定 quality.production_same_cause_per_day の件数になったら管理者へ知らせる
    ＝知らせの種類 quality_alert）を外した（0148）。"""
    def rows(self, s, n):
        return [s] + [self.second_row(s) for _ in range(n - 1)]

    def admin_notes(self, s, kind):
        return self.w.a.in_system(ADMIN, lambda tx: tx.all(
            "SELECT event_key FROM salescall.notification_events WHERE for_admin AND kind = %s"
            " AND refs ->> 'business_id' = %s ORDER BY event_key", (kind, str(s["business"]))))

    def evaluations(self, s):
        return self.tx(s, lambda tx: tx.one("SELECT count(*) AS n FROM salescall.quality_evaluations WHERE version_id = %s",
                                            (s["version"],)))["n"]

    def test_forbidden_speech_never_stops_the_business_and_tells_per_call(self):
        s = self.fresh(stage_cap=20)
        rows = self.rows(s, 4)
        got = self.verified_call(rows[0], said(3))
        self.assertEqual((got["decision"].kind, got["decision"].end_reason), ("end", "forbidden_speech"))   # その行は終える
        for r in rows[1:]:
            self.verified_call(r, said(3))
        self.assertEqual(self.business_stops(s), [])                   # 何件になっても、業務は止めない
        told = self.w.a.in_system(ADMIN, lambda tx: tx.one(
            "SELECT count(*) AS n FROM salescall.notification_events WHERE for_admin AND kind = 'forbidden_speech'"
            " AND refs ->> 'version_id' = %s", (str(s["version"]),)))["n"]
        self.assertEqual(told, 4)                                      # 管理者へは通話ごと（締めの処理）
        self.assertEqual(self.admin_notes(s, "quality_alert"), [])     # 1日の件数でまとめた知らせは無い
        self.assertNotIn("quality_alert", notify.KINDS)
        asked = self.w.a.in_system(SYSTEM, lambda tx: [r for r in fix_requests.open_requests(tx, "CL-1")
                                                       if r["kind"] == "forbidden_speech"
                                                       and r["refs"].get("version_id") == str(s["version"])])
        self.assertEqual(len(asked), 1)                                  # 担当への修正依頼（同じ不具合は1つ）
        self.assertEqual(self.evaluations(s), 0)                         # 本番は品質の評価をしない
        self.assertIsInstance(self.claim(self.second_row(s)), gate.Claim)   # 業務はかけ続ける
        gone = owner_access().in_system(SYSTEM, lambda tx: tx.one(
            "SELECT 1 FROM salescall.setting_definitions WHERE key = 'quality.production_same_cause_per_day'"))
        self.assertIsNone(gone)

    def test_unknowns_never_stop(self):
        s = self.fresh(stage_cap=20)
        rows = self.rows(s, 3)
        self.verified_call(rows[0], VerificationSummary(1, 0, 0), report=rep(cut=None))       # 切れ方の材料が無い
        self.verified_call(rows[1], VerificationSummary(1, 0, 0), items=[("Q1", "dont_know", None)])   # 理由が無い（別の原因）
        self.verified_call(rows[2], VerificationSummary(1, 0, 0), report=rep(cut=None))
        self.assertEqual(self.business_stops(s), [])
        self.assertEqual(self.evaluations(s), 0)

    def test_the_stage_cap_is_only_for_verification(self):
        s = self.fresh(stage_cap=1)
        self.verified_call(s, VerificationSummary(2, 2, 0), items=[("Q1", "confirmed", None)])   # 食い違い100％でも
        self.assertEqual(self.business_stops(s), [])                   # 割合の基準は見ない（検証モードだけ）
        r = self.second_row(s)
        self.assertIsInstance(self.claim(r), gate.Claim)                # 段の件数（1件）を超えてかける
