"""データの土台の試験（DESIGN.md 3章・3-0・3-1・4-2・10章・11章）。

どの試験も「約束を、わざと破ろうとすると、データベースが本当に拒む」ことを確かめる（DESIGN.md 14章）。
"""

from __future__ import annotations

import threading
import time
import unittest
import uuid
from datetime import timedelta

from salescall.core.access import Rejected
from tests.support import OPERATOR, SYSTEM, World, fictional_phone, owner_access


class OneContactPerNumber(unittest.TestCase):
    """3-1：同じ依頼主の中で、同じ番号は1つの連絡先。"""

    def test_same_client_same_number_is_rejected(self):
        w = World()
        t = w.tenant(); c = w.client(t); p = fictional_phone()
        w.contact(t, c, p)
        with self.assertRaises(Rejected) as e:
            w.contact(t, c, p)
        self.assertEqual(e.exception.constraint, "contacts_client_id_phone_e164_key")

    def test_other_client_same_number_is_allowed(self):
        w = World()
        t = w.tenant(); p = fictional_phone()
        w.contact(t, w.client(t), p)
        w.contact(t, w.client(t), p)  # 依頼主が違えば別の連絡先

    def test_number_must_be_normalized(self):
        w = World()
        t = w.tenant(); c = w.client(t)
        for bad in ("090-0000-0000", "+81 90 0000 0000", "", "+0123456789"):
            with self.subTest(bad=bad), self.assertRaises(Rejected):
                w.contact(t, c, bad)


class OneTaskPerRunAndContact(unittest.TestCase):
    """3-1：1つの実施回で、同じ連絡先は1タスク（同じファイルを2回取り込んでも二重にならない）。"""

    def test_second_task_is_rejected(self):
        w = World(); s = w.setup()
        with self.assertRaises(Rejected) as e:
            w.task(s["tenant"], s["client"], s["run"], s["contact"])
        self.assertEqual(e.exception.constraint, "tasks_run_id_contact_id_key")


class ClientIsolation(unittest.TestCase):
    """3-0 の1・11章：別の依頼主のデータは、見えない・変えられない・指せない（3重の守り）。"""

    def setUp(self):
        self.w = World()
        self.a = self.w.setup()
        self.b = self.w.setup(tenant=self.a["tenant"])  # 同じ契約者の、別の依頼主

    def test_cannot_see_other_clients_rows(self):
        a, b = self.a, self.b
        rows = self.w.a.in_client(OPERATOR, a["tenant"], a["client"], lambda tx: tx.all(
            "SELECT client_id FROM salescall.contacts"))
        self.assertEqual({r["client_id"] for r in rows}, {a["client"]})
        seen = self.w.a.in_client(OPERATOR, a["tenant"], a["client"], lambda tx: tx.one(
            "SELECT count(*) AS n FROM salescall.tasks WHERE task_id = %s", (b["task"],)))
        self.assertEqual(seen["n"], 0)

    def test_cannot_update_other_clients_rows(self):
        a, b = self.a, self.b
        n = self.w.a.in_client(SYSTEM, a["tenant"], a["client"], lambda tx: tx.execute(
            "UPDATE salescall.item_values SET state = 'confirmed' WHERE task_id = %s", (b["task"],)).rowcount)
        self.assertEqual(n, 0)

    def test_cannot_write_row_for_other_client(self):
        a, b = self.a, self.b
        with self.assertRaises(Rejected):  # 行ごとの制限（書く行の依頼主が、取引の依頼主と違う）
            self.w.a.in_client(OPERATOR, a["tenant"], a["client"], lambda tx: tx.execute(
                "INSERT INTO salescall.contacts (tenant_id, client_id, phone_e164) VALUES (%s, %s, %s)",
                (b["tenant"], b["client"], fictional_phone())))

    def test_cannot_point_to_other_clients_parent(self):
        a, b = self.a, self.b
        with self.assertRaises(Rejected) as e:  # 組の参照（自分の依頼主の行として、別の依頼主の連絡先を指す）
            self.w.task(a["tenant"], a["client"], a["run"], b["contact"])
        self.assertEqual(e.exception.sqlstate, "23503")

    def test_tenant_and_system_scopes_see_no_client_rows(self):
        a = self.a
        n1 = self.w.a.in_tenant(OPERATOR, a["tenant"], lambda tx: tx.one("SELECT count(*) AS n FROM salescall.tasks"))
        n2 = self.w.a.in_system(SYSTEM, lambda tx: tx.one("SELECT count(*) AS n FROM salescall.tasks"))
        self.assertEqual((n1["n"], n2["n"]), (0, 0))

    def test_other_tenant_sees_nothing(self):
        other = self.w.tenant()
        rows = self.w.a.in_tenant(OPERATOR, other, lambda tx: tx.all("SELECT client_id FROM salescall.clients"))
        self.assertEqual(rows, [])

    def test_attempt_number_must_match_contact(self):
        a = self.a
        with self.assertRaises(Rejected):  # 試行の番号は、連絡先の番号と食い違えない（組の参照）
            self.w.insert_claim(dict(a, phone=fictional_phone()))


class FrozenRecords(unittest.TestCase):
    """3-0 の3：変えてはいけない記録は、書き換え・消しを拒む（権限と仕掛けの2重）。"""

    def test_app_role_cannot_change_frozen_records(self):
        w = World(); s = w.setup()
        for sql, params in [
            ("UPDATE salescall.business_versions SET version_no = 9 WHERE version_id = %s", (s["version"],)),
            ("DELETE FROM salescall.business_versions WHERE version_id = %s", (s["version"],)),
            ("UPDATE salescall.questions SET required = false WHERE version_id = %s", (s["version"],)),
            ("UPDATE salescall.run_versions SET reason = 'x' WHERE run_id = %s", (s["run"],)),
            ("DELETE FROM salescall.tasks WHERE task_id = %s", (s["task"],)),
        ]:
            with self.subTest(sql=sql), self.assertRaises(Rejected):
                w.a.in_client(OPERATOR, s["tenant"], s["client"], lambda tx: tx.execute(sql, params))

    def test_triggers_refuse_even_the_owner(self):
        """権限を持つ役（表の持ち主）でも、仕掛けが拒む（権限の付け間違いがあっても守れる）。"""
        w = World(); s = w.setup()
        aid = w.insert_claim(s)
        w.move(s, aid, "released")
        own = owner_access()
        for sql, params in [
            ("UPDATE salescall.business_versions SET version_no = 9 WHERE version_id = %s", (s["version"],)),
            ("DELETE FROM salescall.questions WHERE version_id = %s", (s["version"],)),
            ("DELETE FROM salescall.run_versions WHERE run_id = %s", (s["run"],)),
            ("UPDATE salescall.attempt_state_history SET to_state = 'ended' WHERE attempt_id = %s", (aid,)),
            ("DELETE FROM salescall.attempts WHERE attempt_id = %s", (aid,)),
            ("UPDATE salescall.attempt_transitions SET to_state = 'ended' WHERE from_state = 'claimed'", ()),
            ("UPDATE salescall.field_types SET readback = false WHERE type_name = 'date'", ()),
        ]:
            with self.subTest(sql=sql), self.assertRaises(Rejected):
                own.in_client(OPERATOR, s["tenant"], s["client"], lambda tx: tx.execute(sql, params))

    def test_results_and_next_actions_are_append_only(self):
        w = World(); s = w.setup()

        def add(tx):
            tx.execute("INSERT INTO salescall.task_results (tenant_id, client_id, task_id, version_no, kind, business_state,"
                       " judge_version, notify_human, snapshot, decided_by)"
                       " VALUES (%s, %s, %s, 1, 'judged', 'incomplete', 'judge-1', false, '{}', 'judge')",
                       (s["tenant"], s["client"], s["task"]))
            tx.execute("INSERT INTO salescall.task_next_actions (tenant_id, client_id, task_id, seq, kind, call_at, cause)"
                       " VALUES (%s, %s, %s, 1, 'call', now(), '取り込み')", (s["tenant"], s["client"], s["task"]))
        w.a.in_client(SYSTEM, s["tenant"], s["client"], add)
        own = owner_access()
        for sql in ("UPDATE salescall.task_results SET business_state = 'completed' WHERE task_id = %s",
                    "UPDATE salescall.task_next_actions SET call_at = now() WHERE task_id = %s",
                    "DELETE FROM salescall.task_results WHERE task_id = %s"):
            with self.subTest(sql=sql), self.assertRaises(Rejected):
                own.in_client(OPERATOR, s["tenant"], s["client"], lambda tx: tx.execute(sql, (s["task"],)))

    def test_result_version_numbers_do_not_overlap(self):
        w = World(); s = w.setup()
        sql = ("INSERT INTO salescall.task_results (tenant_id, client_id, task_id, version_no, kind, business_state,"
               " judge_version, notify_human, snapshot, decided_by)"
               " VALUES (%s, %s, %s, 1, 'judged', 'open', 'judge-1', false, '{}', 'judge')")
        w.a.in_client(SYSTEM, s["tenant"], s["client"], lambda tx: tx.execute(sql, (s["tenant"], s["client"], s["task"])))
        with self.assertRaises(Rejected):
            w.a.in_client(SYSTEM, s["tenant"], s["client"], lambda tx: tx.execute(sql, (s["tenant"], s["client"], s["task"])))

    def test_human_version_has_no_judge_version_and_judged_must_have_one(self):
        w = World(); s = w.setup()
        base = ("INSERT INTO salescall.task_results (tenant_id, client_id, task_id, version_no, kind, business_state,"
                " judge_version, notify_human, snapshot, decided_by) VALUES (%s, %s, %s, %s, %s, 'open', %s, false, '{}', 'x')")
        for no, kind, jv in [(1, "judged", None), (2, "human", "judge-1")]:
            with self.subTest(kind=kind), self.assertRaises(Rejected):
                w.a.in_client(SYSTEM, s["tenant"], s["client"], lambda tx: tx.execute(
                    base, (s["tenant"], s["client"], s["task"], no, kind, jv)))


class OnlyCorporateForNow(unittest.TestCase):
    """Intent.md 3-1：個人を対象にした業務は、13章の確かめが済むまで作れない。"""

    def test_individual_target_is_rejected(self):
        w = World(); t = w.tenant(); c = w.client(t)
        with self.assertRaises(Rejected):
            w.a.in_client(OPERATOR, t, c, lambda tx: tx.execute(
                "INSERT INTO salescall.businesses (tenant_id, client_id, name, target_kind) VALUES (%s, %s, 'x', 'individual')",
                (t, c)))


class UnknownFieldTypeIsRejected(unittest.TestCase):
    """I-16：型の一覧に無い型の質問は作れない。"""

    def test_unknown_type(self):
        w = World(); t = w.tenant(); c = w.client(t)
        b, v = w.business_with_version(t, c)
        with self.assertRaises(Rejected):
            w.a.in_client(OPERATOR, t, c, lambda tx: tx.execute(
                "INSERT INTO salescall.questions (tenant_id, client_id, version_id, question_id, position, type_name, required)"
                " VALUES (%s, %s, %s, 'Q2', 2, 'mystery', true)", (t, c, v)))


class AttemptTransitions(unittest.TestCase):
    """4-2・I-27：試行の状態は、決まった組でしか移らない。履歴は自動で残る。"""

    def test_happy_path_and_history(self):
        w = World(); s = w.setup()
        aid = w.insert_claim(s)
        for st in ("dialing", "ringing", "answered", "in_conversation", "ending", "ended"):
            w.move(s, aid, st)
        row = w.a.in_client(SYSTEM, s["tenant"], s["client"], lambda tx: tx.one(
            "SELECT state, verification, ended_at IS NOT NULL AS ended FROM salescall.attempts WHERE attempt_id = %s", (aid,)))
        self.assertEqual((row["state"], row["verification"], row["ended"]), ("ended", "pending", True))
        hist = w.a.in_client(SYSTEM, s["tenant"], s["client"], lambda tx: tx.all(
            "SELECT from_state, to_state FROM salescall.attempt_state_history WHERE attempt_id = %s ORDER BY seq", (aid,)))
        self.assertEqual([h["to_state"] for h in hist],
                         ["claimed", "dialing", "ringing", "answered", "in_conversation", "ending", "ended"])
        self.assertIsNone(hist[0]["from_state"])

    def test_forbidden_moves(self):
        w = World(); s = w.setup()
        aid = w.insert_claim(s)
        # 同じ状態への書き込み（claimed → claimed）は、データベースからは「何も変わらない」としか見えないので数えない（害は無い）
        for bad in ("ended", "answered", "in_conversation"):
            with self.subTest(bad=bad), self.assertRaises(Rejected):
                w.move(s, aid, bad)
        w.move(s, aid, "released")
        for bad in ("dialing", "claimed", "ended"):
            with self.subTest(after="released", bad=bad), self.assertRaises(Rejected):
                w.move(s, aid, bad)

    def test_must_start_as_claimed(self):
        w = World(); s = w.setup()
        with self.assertRaises(Rejected):
            w.a.in_client(SYSTEM, s["tenant"], s["client"], lambda tx: tx.execute(
                "INSERT INTO salescall.attempts (tenant_id, client_id, task_id, contact_id, phone_e164, line_id, version_id,"
                " state, claimed_at, claim_expires_at) VALUES (%s, %s, %s, %s, %s, %s, %s, 'dialing', now(), now() + interval '1 minute')",
                (s["tenant"], s["client"], s["task"], s["contact"], s["phone"], s["line"], s["version"])))

    def test_verification_rule_by_how_it_ended(self):
        w = World()
        for end, path, expected in [
            ("released", [], "not_needed"), ("no_answer", ["dialing"], "not_needed"),
            ("busy", ["dialing"], "not_needed"), ("call_failed", ["dialing"], "not_needed"),
            ("technical_error", ["dialing"], "pending"), ("dropped", ["dialing", "answered"], "pending"),
        ]:
            with self.subTest(end=end):
                s = w.setup()
                aid = w.insert_claim(s)
                for st in path:
                    w.move(s, aid, st)
                w.move(s, aid, end, "unknown" if end == "dropped" else None)
                v = w.a.in_client(SYSTEM, s["tenant"], s["client"], lambda tx: tx.one(
                    "SELECT verification FROM salescall.attempts WHERE attempt_id = %s", (aid,)))
                self.assertEqual(v["verification"], expected)

    def test_dropped_needs_hangup_side_and_nothing_else_may_have_it(self):
        w = World(); s = w.setup()
        aid = w.insert_claim(s)
        w.move(s, aid, "dialing"); w.move(s, aid, "answered")
        with self.assertRaises(Rejected):
            w.move(s, aid, "dropped")            # 切れ方が無い（「不明」も値として要る）
        with self.assertRaises(Rejected):
            w.move(s, aid, "in_conversation", "callee")  # dropped でないのに切れ方を持つ
        w.move(s, aid, "dropped", "callee")

    def test_verification_only_pending_to_done_or_failed(self):
        w = World(); s = w.setup()
        aid = w.insert_claim(s)
        w.move(s, aid, "released")  # not_needed
        set_v = lambda v: w.a.in_client(SYSTEM, s["tenant"], s["client"], lambda tx: tx.execute(
            "UPDATE salescall.attempts SET verification = %s WHERE attempt_id = %s", (v, aid)))
        with self.assertRaises(Rejected):
            set_v("done")
        s2 = w.setup(); a2 = w.insert_claim(s2)
        w.move(s2, a2, "dialing"); w.move(s2, a2, "technical_error")  # pending
        w.a.in_client(SYSTEM, s2["tenant"], s2["client"], lambda tx: tx.execute(
            "UPDATE salescall.attempts SET verification = 'failed' WHERE attempt_id = %s", (a2,)))
        with self.assertRaises(Rejected):
            w.a.in_client(SYSTEM, s2["tenant"], s2["client"], lambda tx: tx.execute(
                "UPDATE salescall.attempts SET verification = 'done' WHERE attempt_id = %s", (a2,)))

    def test_attempt_identity_cannot_change(self):
        w = World(); s = w.setup()
        aid = w.insert_claim(s)
        with self.assertRaises(Rejected):
            w.a.in_client(SYSTEM, s["tenant"], s["client"], lambda tx: tx.execute(
                "UPDATE salescall.attempts SET claim_expires_at = now() + interval '1 hour' WHERE attempt_id = %s", (aid,)))


class ExpiredClaimCannotDial(unittest.TestCase):
    """5-3・I-29：期限が過ぎた予約証では、発信へ進めない（データベースの時刻で決める）。"""

    def test_expired(self):
        w = World(); s = w.setup()
        aid = w.insert_claim(s, expires_in=timedelta(milliseconds=300))
        time.sleep(0.5)
        with self.assertRaises(Rejected):
            w.move(s, aid, "dialing")
        w.move(s, aid, "released")  # 期限切れの予約は戻せる

    def test_within_time(self):
        w = World(); s = w.setup()
        aid = w.insert_claim(s)
        w.move(s, aid, "dialing")


class VersionMustBeRunsCurrent(unittest.TestCase):
    """3-2：試行の版は、予約のときの実施回の版でなければならない。"""

    def test_old_version_after_switch(self):
        w = World(); s = w.setup()
        v2 = w.add_version(s["tenant"], s["client"], s["business"], 2)
        w.a.in_client(OPERATOR, s["tenant"], s["client"], lambda tx: tx.execute(
            "INSERT INTO salescall.run_versions (tenant_id, client_id, run_id, business_id, seq, version_id, assigned_by, reason)"
            " VALUES (%s, %s, %s, %s, 2, %s, %s, '版2へ切り替え')",
            (s["tenant"], s["client"], s["run"], s["business"], v2, OPERATOR.name)))
        with self.assertRaises(Rejected):
            w.insert_claim(s)                 # 古い版（1）で予約しようとする
        w.insert_claim(s, version=v2)         # いまの版（2）なら通る

    def test_cannot_assign_version_of_another_business(self):
        w = World(); s = w.setup()
        _, other_v = w.business_with_version(s["tenant"], s["client"])
        with self.assertRaises(Rejected):
            w.a.in_client(OPERATOR, s["tenant"], s["client"], lambda tx: tx.execute(
                "INSERT INTO salescall.run_versions (tenant_id, client_id, run_id, business_id, seq, version_id, assigned_by, reason)"
                " VALUES (%s, %s, %s, %s, 2, %s, %s, 'x')",
                (s["tenant"], s["client"], s["run"], s["business"], other_v, OPERATOR.name)))


class OneActiveAttempt(unittest.TestCase):
    """3-1・I-2・I-21：進行中の試行は、連絡先・番号（依頼主・契約者をまたいで）・回線ごとに1つだけ。"""

    def test_per_contact(self):
        w = World(); s = w.setup()
        w.insert_claim(s)
        with self.assertRaises(Rejected) as e:
            w.insert_claim(s, line=w.line(s["tenant"]))
        self.assertIn(e.exception.constraint, ("attempts_one_active_per_contact", "attempts_one_active_per_number"))

    def test_per_number_across_clients_and_tenants(self):
        w = World(); p = fictional_phone()
        s1 = w.setup(phone=p)
        s2 = w.setup(phone=p, tenant=s1["tenant"])  # 同じ契約者の別の依頼主
        s3 = w.setup(phone=p)                       # 別の契約者
        w.insert_claim(s1)
        for other in (s2, s3):
            with self.subTest(other=other is s3 and "契約者の外" or "同じ契約者"), self.assertRaises(Rejected) as e:
                w.insert_claim(other)
            self.assertEqual(e.exception.constraint, "attempts_one_active_per_number")

    def test_per_line(self):
        w = World(); s1 = w.setup()
        s2 = w.setup(tenant=s1["tenant"])
        w.insert_claim(s1)
        with self.assertRaises(Rejected) as e:
            w.insert_claim(s2, line=s1["line"])
        self.assertEqual(e.exception.constraint, "attempts_one_active_per_line")

    def test_line_of_another_tenant_cannot_be_used(self):
        w = World(); s1 = w.setup(); s2 = w.setup()
        with self.assertRaises(Rejected):
            w.insert_claim(s1, line=s2["line"])

    def test_after_ending_a_new_claim_is_allowed(self):
        w = World(); s = w.setup()
        aid = w.insert_claim(s)
        w.move(s, aid, "released")
        w.insert_claim(s)

    def test_concurrent_claims_on_one_number_only_one_wins(self):
        """別々の依頼主・契約者の処理が、同じ番号を同時に予約しても、通るのは1つだけ（わざと同時に動かす）。"""
        w = World(); p = fictional_phone()
        setups = [w.setup(phone=p) for _ in range(4)] + [w.setup(phone=p, tenant=None) for _ in range(4)]
        start = threading.Barrier(len(setups))
        ok, rejected, errors = [], [], []

        def claim(s):
            start.wait()
            try:
                ok.append(w.insert_claim(s))
            except Rejected:
                rejected.append(s)
            except Exception as e:  # 想定外の失敗は、試験を落とす
                errors.append(e)
        threads = [threading.Thread(target=claim, args=(s,)) for s in setups]
        for th in threads: th.start()
        for th in threads: th.join()
        self.assertEqual(errors, [])
        self.assertEqual((len(ok), len(rejected)), (1, len(setups) - 1))


class ConflictsAreRetried(unittest.TestCase):
    """5-2-1 の2：直列化の水準でぶつかった取引は、読み書きの層がやり直し、結果は1つずつ順に行ったのと同じになる。"""

    def test_concurrent_append_gets_distinct_sequence_numbers(self):
        w = World(); s = w.setup()
        versions = [w.add_version(s["tenant"], s["client"], s["business"], n) for n in range(2, 10)]
        start = threading.Barrier(len(versions))
        errors = []

        def append(v):
            def work(tx):
                nxt = tx.one("SELECT max(seq) + 1 AS n FROM salescall.run_versions WHERE run_id = %s", (s["run"],))["n"]
                tx.execute("INSERT INTO salescall.run_versions (tenant_id, client_id, run_id, business_id, seq, version_id,"
                           " assigned_by, reason) VALUES (%s, %s, %s, %s, %s, %s, %s, '切り替え')",
                           (s["tenant"], s["client"], s["run"], s["business"], nxt, v, OPERATOR.name))
            start.wait()
            try:
                w.a.in_client(OPERATOR, s["tenant"], s["client"], work)
            except Exception as e:
                errors.append(e)
        threads = [threading.Thread(target=append, args=(v,)) for v in versions]
        for th in threads: th.start()
        for th in threads: th.join()
        self.assertEqual(errors, [])
        seqs = w.a.in_client(OPERATOR, s["tenant"], s["client"], lambda tx: [r["seq"] for r in tx.all(
            "SELECT seq FROM salescall.run_versions WHERE run_id = %s ORDER BY seq", (s["run"],))])
        self.assertEqual(seqs, list(range(1, 10)))


class DeliveriesOnlyForReleasedVersions(unittest.TestCase):
    """10章・3-1：送れるのは「送ってよい印」の付いた版だけ。同じ版を同じ送り先へ2回は作れない。"""

    def test_release_then_deliver_once(self):
        w = World(); s = w.setup(); dest = uuid.uuid4()

        def prepare(tx):
            tx.execute("INSERT INTO salescall.task_results (tenant_id, client_id, task_id, version_no, kind, business_state,"
                       " judge_version, notify_human, snapshot, decided_by)"
                       " VALUES (%s, %s, %s, 1, 'judged', 'completed', 'judge-1', false, '{}', 'judge')",
                       (s["tenant"], s["client"], s["task"]))
            tx.execute("INSERT INTO salescall.destinations (tenant_id, client_id, destination_id, name, method, upsert_capable)"
                       " VALUES (%s, %s, %s, '架空の送り先', 'mock', true)", (s["tenant"], s["client"], dest))
        w.a.in_client(OPERATOR, s["tenant"], s["client"], prepare)
        deliver = lambda: w.a.in_client(SYSTEM, s["tenant"], s["client"], lambda tx: tx.execute(
            "INSERT INTO salescall.deliveries (tenant_id, client_id, task_id, destination_id, version_no, state, send_count)"
            " VALUES (%s, %s, %s, %s, 1, 'pending', 0)", (s["tenant"], s["client"], s["task"], dest)))
        with self.assertRaises(Rejected):
            deliver()  # 印が無い
        w.a.in_client(SYSTEM, s["tenant"], s["client"], lambda tx: tx.execute(
            "INSERT INTO salescall.result_releases (tenant_id, client_id, task_id, version_no, released_by)"
            " VALUES (%s, %s, %s, 1, '承認の部分')", (s["tenant"], s["client"], s["task"])))
        deliver()
        with self.assertRaises(Rejected):
            deliver()  # 同じ版を同じ送り先へ2回


class DoNotCallLedger(unittest.TestCase):
    """Q3：発信禁止の登録は消せない。取り消しは人だけが、記録つきで1回。有効な登録は番号ごとに1つ。"""

    def _register(self, w, s):
        eid = uuid.uuid4()
        w.a.in_client(SYSTEM, s["tenant"], s["client"], lambda tx: tx.execute(
            "INSERT INTO salescall.dnc_entries (tenant_id, client_id, entry_id, phone_e164, reason, registered_by)"
            " VALUES (%s, %s, %s, %s, 'refused_in_call', 'conversation')", (s["tenant"], s["client"], eid, s["phone"])))
        return eid

    def test_rules(self):
        w = World(); s = w.setup()
        eid = self._register(w, s)
        with self.assertRaises(Rejected):
            self._register(w, s)  # 有効な登録が2つになる
        revoke = lambda actor: w.a.in_client(actor, s["tenant"], s["client"], lambda tx: tx.execute(
            "UPDATE salescall.dnc_entries SET revoked_at = now(), revoked_by = %s, revoke_reason = '本人の申し出の取り消し'"
            " WHERE entry_id = %s", (actor.name, eid)))
        with self.assertRaises(Rejected):
            revoke(SYSTEM)  # システムの処理は取り消せない
        with self.assertRaises(Rejected):
            w.a.in_client(OPERATOR, s["tenant"], s["client"], lambda tx: tx.execute(
                "UPDATE salescall.dnc_entries SET phone_e164 = %s WHERE entry_id = %s", (fictional_phone(), eid)))
        with self.assertRaises(Rejected):
            w.a.in_client(OPERATOR, s["tenant"], s["client"], lambda tx: tx.execute(
                "DELETE FROM salescall.dnc_entries WHERE entry_id = %s", (eid,)))
        revoke(OPERATOR)
        with self.assertRaises(Rejected):
            revoke(OPERATOR)  # 取り消しは1回だけ
        self._register(w, s)  # 取り消したあとは、また登録できる


class StopEvents(unittest.TestCase):
    """Intent.md 10-3・Q13：停止を解くのは人だけ。停止の記録は変えられない。範囲の外の停止は書けない。"""

    def test_rules(self):
        w = World(); t = w.tenant(); c = w.client(t)
        ins = lambda actor, scope, tid, cid, action: (lambda tx: tx.execute(
            "INSERT INTO salescall.stop_events (scope, tenant_id, client_id, action, actor, actor_kind, reason)"
            " VALUES (%s, %s, %s, %s, %s, %s, '試験')", (scope, tid, cid, action, actor.name, actor.kind)))
        w.a.in_client(SYSTEM, t, c, ins(SYSTEM, "client", t, c, "stop"))           # システムが止めるのはよい
        with self.assertRaises(Rejected):
            w.a.in_client(SYSTEM, t, c, ins(SYSTEM, "client", t, c, "resume"))     # システムは解けない
        w.a.in_client(OPERATOR, t, c, ins(OPERATOR, "client", t, c, "resume"))
        with self.assertRaises(Rejected):
            w.a.in_tenant(OPERATOR, t, ins(OPERATOR, "system", None, None, "stop"))  # 契約者の取引から全体は止めない
        other = w.tenant()
        with self.assertRaises(Rejected):
            w.a.in_tenant(OPERATOR, t, ins(OPERATOR, "tenant", other, None, "stop"))  # ほかの契約者は止められない


if __name__ == "__main__":
    unittest.main()
