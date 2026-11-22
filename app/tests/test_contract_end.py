"""依頼者との契約が終わったとき（設計書 3-10。計画書 11-4。表の作り 0285）。

✅ 2026-11-22 管理者：「ハードディスクに残す。消すのはハードディスクで消すだけ。」・サーバーは「(ア) HDに写してサーバーから外す」。"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from unittest import mock

from salescall.core import backup, contract_end
from salescall.core.access import Rejected
from salescall.supervisor import jobs
from salescall.supervisor.links import NoLinks
from tests.support import ADMIN, OPERATOR, SYSTEM, World, owner_access
from tests.test_gate import HOLDER
from tests.test_settle import SettleBase

TAKEN = [("Q1", "confirmed", None)]

# 外す表（依頼者の欄を持つ表のうち、残す表でないもの。表の作り 0285 が表の一覧から求める）の控え。表を足したら、残すか外すかを決めて、ここを直す
# （ほかの依頼者・契約者の判断に使う表なら salescall.forget_kept_tables に入れる＝外すとお客様に害が出る）
FORGOTTEN_TABLES = [
    'apology_approvals', 'apology_orders', 'apology_outcomes', 'approved_contents', 'attempt_ai_troubles', 'attempt_blames',
    'business_contents', 'business_destination_choosing', 'business_destinations', 'business_free_slots', 'business_rules',
    'call_report_rebuilds', 'call_reports', 'call_utterances', 'client_group_changes', 'client_solution_leave_outs',
    'client_solution_lines', 'client_solution_removals', 'client_solution_replacements', 'client_solutions',
    'correction_call_confirms', 'correction_calls', 'decision_answers', 'decision_asks', 'decision_waits', 'deficit_warnings',
    'deliveries', 'destinations', 'dnc_import_rows', 'error_events', 'forbidden_findings', 'gate_denials', 'import_rows',
    'imports', 'item_values', 'judgement_checks', 'mock_runs', 'more_call_approvals', 'name_changes', 'name_recording_reviews',
    'notification_events', 'number_rechecks', 'number_states', 'output_templates', 'part_contents', 'quality_evaluations',
    'questions', 'recording_approvals', 'recording_holds', 'recording_list_parts', 'recording_listens', 'recording_lists',
    'recording_readings', 'recording_sets', 'recording_unsaved', 'recordings', 'result_releases', 'reward_terms', 'run_versions',
    'sample_draw_calls', 'sample_draws', 'setting_values', 'solution_text_names', 'state_moves', 'stop_events',
    'submission_files', 'target_rechecks', 'task_append_locks', 'task_approvals', 'task_confirmed_readings',
    'task_contact_sources', 'task_contacts', 'task_designations', 'task_dial_fixes', 'task_exclusions', 'task_fixes',
    'task_month_ahead_answers', 'task_next_actions', 'task_old_promise_choices', 'task_results', 'task_states',
    'task_sub_numbers', 'task_trouble_restores', 'unclear_checks', 'usage_entries', 'verification_summaries',
    'verification_voice', 'version_copies', 'version_name_readings', 'version_stages', 'version_successions',
]


class ContractEnd(SettleBase):
    def count(self, table, client):
        return owner_access().in_system(SYSTEM, lambda tx: tx.one(
            f'SELECT count(*) AS n FROM salescall."{table}" WHERE client_id = %s', (client,)))["n"]

    def backed_up(self, s, when=None):
        """契約終了のあとに、その依頼者の AI用の控えを取り、戻せることまで確かめた（記録だけを作る）。"""
        backup._record(self.w.a, when or datetime.now().astimezone() + timedelta(seconds=1), "x.ai.enc", ("0" * 64, 1), {"tasks": 1},
                       None, person=None, part="client_ai", tenant_id=s["tenant"], client_id=s["client"])

    def state(self, s):
        return self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: contract_end.state(tx, s["client"]))

    def test_an_ended_client_waits_then_is_removed_keeping_what_other_clients_need(self):
        s = self.ready()
        a = self.call(s, items=TAKEN)
        self.settle(s, a)
        mate = self.ready(tenant=s["tenant"])                                   # 同じ契約者のほかの依頼者
        b = self.call(mate, items=TAKEN)
        self.settle(mate, b)
        before_mate = {t: self.count(t, mate["client"]) for t in ("attempts", "item_values", "tasks", "notification_events")}
        owner_access().in_system(SYSTEM, lambda tx: tx.execute(   # お客様の行の中身（依頼者の管理番号・名前・読み・役職・部署）
            "UPDATE salescall.tasks SET client_ref = 'K-001', source_name = '架空商事', target_name = '山田', target_name_reading = 'やまだ',"
            " target_name_reading_by = 'given', target_role = '部長', target_department = '総務部' WHERE task_id = %s", (s["task"],)))
        self.assertGreater(self.count("item_values", s["client"]), 0)
        # 押す：新しい発信が止まる。猶予のあいだは取り消せる
        end = lambda: self.w.a.in_client(OPERATOR, s["tenant"], s["client"], lambda tx: contract_end.end(tx, reason="試験：契約が終わった"))  # noqa: E731
        end()
        self.assertFalse(self.tx(s, lambda tx: tx.one("SELECT salescall.client_active(%s) AS a", (s["client"],)))["a"])
        self.assertEqual(self.state(s).blocked, "契約終了の猶予のあいだです（取り消せます）")
        with self.assertRaises(Rejected):                                        # 契約終了のあいだは、使う形に戻せない（取り消してから）
            self.w.a.in_client(OPERATOR, s["tenant"], s["client"], lambda tx: __import__("salescall.core.registry", fromlist=["x"])
                               .set_client_active(tx, True, reason="試験"))
        with self.assertRaises(Rejected) as refused:                             # 押すのは管理者か契約者だけ
            self.w.a.in_client(SYSTEM, s["tenant"], s["client"], lambda tx: contract_end.end(tx, reason="試験"))
        self.assertEqual(refused.exception.constraint, "contract_end_by_person")
        self.w.a.in_client(OPERATOR, s["tenant"], s["client"], lambda tx: contract_end.cancel(tx, reason="試験：押し間違い"))
        self.assertTrue(self.tx(s, lambda tx: tx.one("SELECT salescall.client_active(%s) AS a", (s["client"],)))["a"])
        self.assertIsNone(self.state(s).end_id)
        end()
        with mock.patch.object(contract_end, "forget_after", lambda tx: timedelta(0)):
            self.assertEqual(self.state(s).blocked, "契約終了のあとの控えを、まだハードディスクに取って確かめていません")
            self.backed_up(s)
            self.assertIsNone(self.state(s).blocked)
            ctx = jobs.Context(self.w.a, None, HOLDER, NoLinks(), s["tenant"])
            jobs.contract_ends(ctx)
        st = self.state(s)
        self.assertTrue(st.forgotten)
        # 外したもの・残したもの
        for t in ("item_values", "task_results", "call_reports", "notification_events", "task_next_actions", "setting_values"):
            with self.subTest(removed=t):
                self.assertEqual(self.count(t, s["client"]), 0)
        for t in ("attempts", "attempt_state_history", "tasks", "task_numbers", "contacts", "clients"):
            with self.subTest(kept=t):
                self.assertGreater(self.count(t, s["client"]), 0)
        task = owner_access().in_system(SYSTEM, lambda tx: tx.one(
            "SELECT client_ref, source_name, target_name, target_name_reading, target_role, target_department, import_id"
            " FROM salescall.tasks WHERE task_id = %s", (s["task"],)))
        self.assertEqual(set(task.values()), {None})                              # お客様の中身は空
        self.assertEqual({t: self.count(t, mate["client"]) for t in before_mate}, before_mate)   # ほかの依頼者は変わらない
        folder = backup.client_folder(self.w.a, s["tenant"], s["client"])
        self.assertEqual(backup.prune_rules(self.w.a, s["tenant"])[folder]["days"], {})   # ハードディスクの控えは日数で消さない
        self.assertNotIn(s["client"], backup.client_ids(self.w.a, s["tenant"]))           # 控えと見張りを続けない
        with self.assertRaises(Rejected):                                         # 外したあとは使う形に戻せない・取り消せない・2回は外さない
            self.w.a.in_client(OPERATOR, s["tenant"], s["client"], lambda tx: contract_end.cancel(tx, reason="試験"))
        with self.assertRaises(Rejected):
            self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: tx.one("SELECT salescall.forget_client(%s, interval '0') AS r", (s["client"],)))

    def test_only_the_removal_can_delete_and_only_that_clients_rows(self):
        """消してよいかの判断は salescall.forgetting の1か所：印の無い取引・ほかの依頼者の印・残す表は、表の持ち主でも消せない。"""
        s = self.ready()
        other = self.ready()
        a = self.call(s, items=TAKEN)
        self.settle(s, a)
        own = owner_access()
        delete = "DELETE FROM salescall.call_reports WHERE client_id = %s"   # 「消せない」守りのある表（ほかから指されない）
        with self.assertRaises(Rejected):
            own.in_system(ADMIN, lambda tx: tx.execute(delete, (s["client"],)))
        with self.assertRaises(Rejected):
            own.in_system(ADMIN, lambda tx: (tx.execute(f"SET LOCAL salescall.forgetting_client = '{other['client']}'"),   # 外す処理の印の場面
                                             tx.execute(delete, (s["client"],))))
        with self.assertRaises(Rejected):                                         # 残す表は、印があっても消せない
            own.in_system(ADMIN, lambda tx: (tx.execute(f"SET LOCAL salescall.forgetting_client = '{s['client']}'"),
                                             tx.execute("DELETE FROM salescall.attempt_state_history WHERE attempt_id = %s", (a,))))
        own.in_system(ADMIN, lambda tx: (tx.execute(f"SET LOCAL salescall.forgetting_client = '{s['client']}'"),   # 印の依頼者の行なら消せる
                                         tx.execute(delete, (s["client"],))))
        with self.assertRaises(Rejected) as refused:                              # ほかの契約者の依頼者のことは読めない
            self.w.a.in_tenant(SYSTEM, other["tenant"], lambda tx: tx.one("SELECT salescall.forget_blocked(%s, interval '0') AS b",
                                                                          (s["client"],)))
        self.assertEqual(refused.exception.constraint, "forget_blocked_in_tenant")
        with self.assertRaises(Rejected):                                         # 契約終了でない依頼者は外せない・システムの処理だけ
            self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: tx.one("SELECT salescall.forget_client(%s, interval '0') AS r", (s["client"],)))
        with self.assertRaises(Rejected):
            self.w.a.in_tenant(OPERATOR, s["tenant"], lambda tx: tx.one("SELECT salescall.forget_client(%s, interval '0') AS r", (s["client"],)))

    def test_a_client_with_a_submission_not_yet_on_the_drive_is_not_removed(self):
        """提出用のファイルは、ハードディスクへ写したらサーバーから消える形（3-10）。まだ写していないものがあれば外さない（外すと失う）。"""
        s = self.ready()
        self.w.a.in_client(OPERATOR, s["tenant"], s["client"], lambda tx: contract_end.end(tx, reason="試験"))
        self.backed_up(s)
        self.w.a.in_client(SYSTEM, s["tenant"], s["client"], lambda tx: backup.record_submission(tx, "結果.csv", b"x", template_id=None))
        with mock.patch.object(contract_end, "forget_after", lambda tx: timedelta(0)):
            self.assertEqual(self.state(s).blocked, "提出用のファイルを、まだハードディスクへ写していません")
            self.assertEqual(self.w.a.in_tenant(SYSTEM, s["tenant"], contract_end.forget_ready), [])

    def test_every_client_table_is_either_kept_or_removed_on_purpose(self):
        """表を足したら、残すか外すかを決める（決めずに足すと、ここが落ちる）。残す表は salescall.forget_kept_tables の1か所。"""
        got = owner_access().in_system(SYSTEM, lambda tx: sorted(r["t"] for r in tx.all(
            "SELECT c.relname::text AS t FROM pg_class c JOIN pg_namespace ns ON ns.oid = c.relnamespace"
            " WHERE ns.nspname = 'salescall' AND c.relkind IN ('r', 'p') AND NOT (c.relname::text = ANY (salescall.forget_kept_tables()))"
            " AND EXISTS (SELECT 1 FROM pg_attribute at WHERE at.attrelid = c.oid AND at.attname = 'client_id' AND NOT at.attisdropped)")))
        self.assertEqual(got, FORGOTTEN_TABLES)
