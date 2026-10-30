"""運転の状態の試験（DESIGN.md 9-1・Intent.md 10-3）：止めるのも解くのもシステムが原因ごとに行い、
管理者か契約者が押した緊急停止だけは管理者か契約者が解く。止めたら、まだ発信していない予約は同じ取引で取り消される。"""

from __future__ import annotations

import unittest

from salescall.core import attempts, stops
from salescall.core.access import Rejected
from tests.support import OPERATOR, SYSTEM, World


def state_of(w, s, aid):
    return w.a.in_client(SYSTEM, s["tenant"], s["client"], lambda tx: tx.one(
        "SELECT state FROM salescall.attempts WHERE attempt_id = %s", (aid,)))["state"]


def open_for(w, s, business=None):
    return w.a.in_client(SYSTEM, s["tenant"], s["client"], lambda tx: stops.open_stops(
        tx, tenant_id=s["tenant"], client_id=s["client"], business_id=business or s["business"]))


class SystemStops(unittest.TestCase):
    def test_stop_releases_claims_in_scope_only_and_system_can_resume(self):
        w = World()
        s1 = w.setup(); s2 = w.setup(tenant=s1["tenant"]); other = w.setup()
        a1, a2, a3 = w.insert_claim(s1), w.insert_claim(s2), w.insert_claim(other)
        r = w.a.in_tenant(SYSTEM, s1["tenant"], lambda tx: stops.stop(
            tx, scope="tenant", cause="line_failure", reason="回線の異常が続いた", tenant_id=s1["tenant"]))
        self.assertEqual(r["released_claims"], 2)
        self.assertEqual((state_of(w, s1, a1), state_of(w, s2, a2), state_of(w, other, a3)),
                         ("released", "released", "claimed"))   # ほかの契約者の予約は取り消さない
        self.assertEqual([x["cause"] for x in open_for(w, s1)], ["line_failure"])
        self.assertEqual(open_for(w, other), [])
        w.a.in_tenant(SYSTEM, s1["tenant"], lambda tx: stops.resume(
            tx, r["event_id"], reason="回線の確かめが3回続けて正常", evidence="確かめ 3/3"))
        self.assertEqual(open_for(w, s1), [])

    def test_0120_resumes_the_deficit_stops_left_open(self):
        """赤字では止めない（2026-10-22 深夜 ユーザー：「止めない。知らせるだけでいい」）。0118 より前に開いていた赤字の停止は、表の作り
        0120 がシステムとして理由を付けて解き、「解いた」を知らせる（解く処理も通す電話の表の行も無いので、残ると約束・お詫びの電話も
        含めて、その依頼者の電話がすべて止まったままになった＝見直し16b）。0120 の (1) の文そのものを流して確かめる。いまは赤字で新しく
        止めるのをデータベースが断る（0148＝見直し23 の (4)）ので、0118 より前の停止の記録は、断る守りを外した取引の中で作り、確かめたら
        取引ごと元に戻す（ほかの試験に残さない）。"""
        from pathlib import Path
        from tests.support import owner_access
        w = World(); s = w.setup()
        sql = (Path(__file__).resolve().parents[1] / "db" / "migrations" / "0120_review16b_fixes.sql").read_text(encoding="utf-8")
        part = sql.split("-- ═════════ (1) ═════════")[1].split("-- ═════════ (2) ═════════")[0]

        class Undo(Exception):
            pass

        def work(tx):
            tx.execute("ALTER TABLE salescall.stop_events DROP CONSTRAINT stop_events_cause_retired")
            e = tx.one("INSERT INTO salescall.stop_events (scope, tenant_id, client_id, action, cause, actor, actor_kind, reason)"
                       " VALUES ('client', %s, %s, 'stop', 'deficit', salescall.cur_actor(),"
                       "         salescall.cur_actor_kind()::salescall.actor_kind, '試験：0118 より前の赤字の停止') RETURNING event_id",
                       (s["tenant"], s["client"]))["event_id"]
            tx.execute(part)
            raise Undo(tx.one(
                "SELECT r.actor_kind::text AS k, (SELECT count(*) FROM salescall.notification_events n"
                "  WHERE n.event_key = 'resume:' || r.event_id AND n.kind = 'resumed') AS told,"
                " (SELECT count(*) FROM salescall.open_stop_events o WHERE o.event_id = %s) AS still"
                " FROM salescall.stop_events r WHERE r.resolves_event_id = %s", (e, e)))
        with self.assertRaises(Undo) as got:
            owner_access().in_system(SYSTEM, work)
        row = got.exception.args[0]
        self.assertEqual((row["k"], row["told"], row["still"]), ("system", 1, 0))
        self.assertEqual(open_for(w, s), [])

    def test_dialing_attempts_are_not_released(self):
        w = World(); s = w.setup()
        aid = w.insert_claim(s)
        w.a.in_client(SYSTEM, s["tenant"], s["client"], lambda tx: attempts.start_dialing(tx, aid))
        r = w.a.in_client(SYSTEM, s["tenant"], s["client"], lambda tx: stops.stop(
            tx, scope="client", cause="ai_connection", reason="会話AIの異常", tenant_id=s["tenant"], client_id=s["client"]))
        self.assertEqual(r["released_claims"], 0)
        self.assertEqual(state_of(w, s, aid), "dialing")   # 発信してしまった通話は、安全に締めて終える（取り消さない）

    def test_overlapping_causes_keep_it_stopped(self):
        w = World(); s = w.setup()
        st = lambda cause: w.a.in_client(SYSTEM, s["tenant"], s["client"], lambda tx: stops.stop(
            tx, scope="client", cause=cause, reason="試験", tenant_id=s["tenant"], client_id=s["client"]))["event_id"]
        e1, e2 = st("ai_connection"), st("line_failure")
        w.a.in_client(SYSTEM, s["tenant"], s["client"], lambda tx: stops.resume(tx, e1, reason="日が替わった"))
        self.assertEqual([x["cause"] for x in open_for(w, s)], ["line_failure"])  # 片方を解いても止まったまま
        w.a.in_client(SYSTEM, s["tenant"], s["client"], lambda tx: stops.resume(tx, e2, reason="回線が正常"))
        self.assertEqual(open_for(w, s), [])

    def test_a_stop_is_resolved_only_once(self):
        w = World(); s = w.setup()
        e = w.a.in_client(SYSTEM, s["tenant"], s["client"], lambda tx: stops.stop(
            tx, scope="client", cause="ai_connection", reason="試験", tenant_id=s["tenant"], client_id=s["client"]))["event_id"]
        w.a.in_client(SYSTEM, s["tenant"], s["client"], lambda tx: stops.resume(tx, e, reason="1回目"))
        with self.assertRaises(Rejected):
            w.a.in_client(SYSTEM, s["tenant"], s["client"], lambda tx: stops.resume(tx, e, reason="2回目"))

    def test_business_scope(self):
        w = World(); s = w.setup()
        _, other_v = w.business_with_version(s["tenant"], s["client"])
        w.a.in_client(SYSTEM, s["tenant"], s["client"], lambda tx: stops.stop(
            tx, scope="business", cause="quality_gate", reason="食い違いの割合が基準を外れた",
            tenant_id=s["tenant"], client_id=s["client"], business_id=s["business"]))
        self.assertEqual([x["cause"] for x in open_for(w, s)], ["quality_gate"])
        other_business = w.a.in_client(SYSTEM, s["tenant"], s["client"], lambda tx: tx.one(
            "SELECT business_id FROM salescall.business_versions WHERE version_id = %s", (other_v,)))["business_id"]
        self.assertEqual(open_for(w, s, business=other_business), [])   # 同じ依頼者の、ほかの業務は止まらない

    def test_system_stop_is_seen_everywhere(self):
        w = World(); s = w.setup()
        e = w.a.in_system(SYSTEM, lambda tx: stops.stop(tx, scope="system", cause="ai_connection", reason="会話AIの異常"))
        try:
            self.assertIn("ai_connection", [x["cause"] for x in open_for(w, s)])
        finally:
            w.a.in_system(SYSTEM, lambda tx: stops.resume(tx, e["event_id"], reason="試しの書き込みが続けて成功"))


class StopsAreNotVisibleToOtherClients(unittest.TestCase):
    def test_same_tenant_other_client(self):
        w = World(); s = w.setup(); s2 = w.setup(tenant=s["tenant"])
        w.a.in_client(SYSTEM, s["tenant"], s["client"], lambda tx: stops.stop(
            tx, scope="client", cause="ai_connection", reason="x", tenant_id=s["tenant"], client_id=s["client"]))
        n = w.a.in_client(SYSTEM, s2["tenant"], s2["client"], lambda tx: tx.one(
            "SELECT count(*) AS n FROM salescall.stop_events WHERE scope = 'client'"))["n"]
        self.assertEqual(n, 0)
        self.assertEqual(open_for(w, s2), [])


class ManualStops(unittest.TestCase):
    def setUp(self):
        self.w = World(); self.s = self.w.setup()
        self.e = self.w.a.in_client(OPERATOR, self.s["tenant"], self.s["client"], lambda tx: stops.stop(
            tx, scope="client", cause="manual", reason="契約者の緊急停止",
            tenant_id=self.s["tenant"], client_id=self.s["client"]))["event_id"]

    def test_system_cannot_resume_a_manual_stop(self):
        s = self.s
        with self.assertRaises(Rejected):
            self.w.a.in_client(SYSTEM, s["tenant"], s["client"], lambda tx: stops.resume(tx, self.e, reason="自動"))
        self.w.a.in_client(OPERATOR, s["tenant"], s["client"], lambda tx: stops.resume(tx, self.e, reason="確かめた"))
        self.assertEqual(open_for(self.w, s), [])

    def test_system_cannot_press_a_manual_stop(self):
        s = self.s
        with self.assertRaises(Rejected):
            self.w.a.in_client(SYSTEM, s["tenant"], s["client"], lambda tx: stops.stop(
                tx, scope="client", cause="manual", reason="x", tenant_id=s["tenant"], client_id=s["client"]))

    def test_actor_must_match_the_transaction(self):
        """システムの処理が「管理者・契約者」と書いて、管理者か契約者が押した緊急停止を解こうとしても、データベースが拒む。"""
        s = self.s
        with self.assertRaises(Rejected):
            self.w.a.in_client(SYSTEM, s["tenant"], s["client"], lambda tx: tx.execute(
                "INSERT INTO salescall.stop_events (scope, tenant_id, client_id, action, resolves_event_id, actor, actor_kind, reason)"
                " VALUES ('client', %s, %s, 'resume', %s, %s, 'operator', 'なりすまし')",
                (s["tenant"], s["client"], self.e, OPERATOR.name)))

    def test_resume_must_match_the_scope(self):
        s = self.s
        with self.assertRaises(Rejected):
            self.w.a.in_tenant(OPERATOR, s["tenant"], lambda tx: tx.execute(
                "INSERT INTO salescall.stop_events (scope, tenant_id, action, resolves_event_id, actor, actor_kind, reason)"
                " VALUES ('tenant', %s, 'resume', %s, %s, 'operator', '範囲違い')", (s["tenant"], self.e, OPERATOR.name)))


class StopRecordsAreFrozenAndScoped(unittest.TestCase):
    def test_rules(self):
        w = World(); s = w.setup(); other = w.tenant()
        with self.assertRaises(Rejected):   # 契約者の取引から、全体は止めない
            w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: stops.stop(tx, scope="system", cause="ai_connection", reason="x"))
        with self.assertRaises(Rejected):   # ほかの契約者は止められない
            w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: stops.stop(
                tx, scope="tenant", cause="ai_connection", reason="x", tenant_id=other))
        e = w.a.in_client(SYSTEM, s["tenant"], s["client"], lambda tx: stops.stop(
            tx, scope="client", cause="ai_connection", reason="x", tenant_id=s["tenant"], client_id=s["client"]))["event_id"]
        with self.assertRaises(Rejected):   # 停止の記録は変えられない
            w.a.in_client(SYSTEM, s["tenant"], s["client"], lambda tx: tx.execute(
                "UPDATE salescall.stop_events SET reason = 'y' WHERE event_id = %s", (e,)))


class RetiredCauses(unittest.TestCase):
    """止める処理の無い原因（保存の失敗・会話AIの利用拒否・費用の上限・赤字。どれも、いまは止めずに知らせる・修正依頼を出す・次のキーで
    続ける＝2026-10-16・10-17・10-22 深夜 ユーザー）で、新しく止めるのはデータベースが断る（見直し23 の (4)。0148）。前の記録の値と
    しては残す（何で止まったかの説明＝stops.CAUSES は、その値も読める）。止めてよい原因の型（stops.Cause）からも外した。"""

    def test_new_stops_with_retired_causes_are_refused(self):
        import typing
        w = World(); s = w.setup()
        for cause in stops.RETIRED_CAUSES:
            with self.subTest(cause=cause), self.assertRaises(Rejected) as e:
                w.a.in_client(SYSTEM, s["tenant"], s["client"], lambda tx, cause=cause: stops.stop(
                    tx, scope="client", cause=cause, reason="試験", tenant_id=s["tenant"], client_id=s["client"]))
            self.assertEqual(e.exception.constraint, "stop_events_cause_retired")
        self.assertEqual(set(stops.RETIRED_CAUSES), {"storage_failure", "project_denied", "budget", "deficit"})
        self.assertTrue(set(stops.RETIRED_CAUSES) <= set(stops.CAUSES))                      # 記録の値の説明は残す
        self.assertFalse(set(stops.RETIRED_CAUSES) & set(typing.get_args(stops.Cause)))      # 新しく止める型からは外した
        self.assertEqual(open_for(w, s), [])
        e = w.a.in_client(SYSTEM, s["tenant"], s["client"], lambda tx: stops.stop(           # ほかの原因は今までどおり止められる
            tx, scope="client", cause="ai_connection", reason="試験", tenant_id=s["tenant"], client_id=s["client"]))
        w.a.in_client(SYSTEM, s["tenant"], s["client"], lambda tx: stops.resume(tx, e["event_id"], reason="試験"))


if __name__ == "__main__":
    unittest.main()
