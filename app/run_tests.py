"""app/ の試験を、1つの命令で全部動かす（CL-1 の条件 (h)）。持ち主 CL-2。

使い方（リポジトリの場所で）：  python app/run_tests.py

順番：
  1. 決まりの機械の見張り（app/checks。DESIGN.md 14章）
  2. 試験用のデータベースを用意し、表の作りを流す
  3. app/tests の試験を全部

試験用のデータベース：
  - 環境変数 SALESCALL_TEST_SUPERUSER_DSN があれば、そのデータベースの管理者でつなぎ、試験用のデータベースを作り、終わったら消す。
  - 無ければ、PostgreSQL の部品（initdb・pg_ctl）を探し、使い捨てのデータベースを一時フォルダに立て、終わったら消す。
⚠️ 試験のデータは架空のものだけ（実在の会社・人・電話番号を使わない）。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from contextlib import contextmanager
from pathlib import Path
from collections.abc import Iterator

import psycopg

APP = Path(__file__).resolve().parent
sys.path[:0] = [str(APP / "src"), str(APP)]

from checks.rules import run_all  # noqa: E402
from salescall.core import migrate  # noqa: E402

ROLES_SQL = APP / "db" / "setup_roles.sql"


def _pg_bindir() -> Path:
    try:
        out = subprocess.run(["pg_config", "--bindir"], capture_output=True, text=True, check=True).stdout.strip()
        if (Path(out) / "initdb").exists():
            return Path(out)
    except (OSError, subprocess.CalledProcessError):
        pass
    for cand in sorted(Path("/usr/lib/postgresql").glob("*/bin"), reverse=True):
        if (cand / "initdb").exists():
            return cand
    raise SystemExit("PostgreSQL の部品（initdb）が見つかりません。SALESCALL_TEST_SUPERUSER_DSN を指定してください")


@contextmanager
def _throwaway_cluster() -> Iterator[str]:
    """使い捨てのデータベースを立て、管理者の接続文字列を返す。終わったら止めて消す。"""
    bindir = _pg_bindir()
    tmp = Path(tempfile.mkdtemp(prefix="salescall-pg-"))
    as_root = hasattr(os, "geteuid") and os.geteuid() == 0
    prefix = ["runuser", "-u", "postgres", "--"] if as_root else []
    if as_root:
        shutil.chown(tmp, "postgres", "postgres")
    data, sock = tmp / "data", tmp
    port = "54329"
    subprocess.run(prefix + [str(bindir / "initdb"), "-D", str(data), "-U", "postgres", "--auth=trust",
                             "--encoding=UTF8", "--locale=C", "-N"], check=True, capture_output=True)
    # 契約者の合言葉（salescall_t_…）だけは、合言葉を本当に確かめる（払い出した合言葉でつなげることを試す。core/logins.py）
    hba = data / "pg_hba.conf"
    hba.write_text("local all /^salescall_t_ scram-sha-256\n" + hba.read_text())
    subprocess.run(prefix + [str(bindir / "pg_ctl"), "-D", str(data), "-w", "-l", str(tmp / "log"), "-o",
                             f"-c listen_addresses='' -k {sock} -p {port} -c fsync=off", "start"],
                   check=True, capture_output=True)
    try:
        yield f"host={sock} port={port} user=postgres dbname=postgres"
    finally:
        subprocess.run(prefix + [str(bindir / "pg_ctl"), "-D", str(data), "-m", "immediate", "stop"], capture_output=True)
        shutil.rmtree(tmp, ignore_errors=True)


def _with_db(dsn: str, dbname: str, user: str) -> str:
    params = dict(p.split("=", 1) for p in dsn.split())
    params.update(dbname=dbname, user=user)
    return " ".join(f"{k}={v}" for k, v in params.items())


# データベースの守りを、わざと1つずつ外す。外したら、その守りを試す試験が本当に落ちることを確かめる（DESIGN.md 14章：
# 落ちることを確かめていない見張りは、見張っていないのと同じ）。守りを足したら、ここにも足す。
MUTATIONS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("個人の業務を作れない守りを外す",
     "ALTER TABLE salescall.business_contents DROP CONSTRAINT business_contents_corporate_only",
     ("tests.test_foundation.OnlyCorporateForNow.test_individual_target_is_rejected",)),
    ("番号ごとの進行中の制約を外す", "DROP INDEX salescall.attempts_one_active_per_number",
     ("tests.test_foundation.OneActiveAttempt.test_per_number_across_clients_and_tenants",
      "tests.test_foundation.OneActiveAttempt.test_concurrent_claims_on_one_number_only_one_wins")),
    ("回線ごとの進行中の制約を外す", "DROP INDEX salescall.attempts_one_active_per_line",
     ("tests.test_foundation.OneActiveAttempt.test_per_line",)),
    ("連絡先の行の制限を外す", "ALTER TABLE salescall.contacts DISABLE ROW LEVEL SECURITY",
     ("tests.test_foundation.ClientIsolation.test_cannot_see_other_clients_rows",
      "tests.test_foundation.ClientIsolation.test_cannot_write_row_for_other_client")),
    ("タスクの組の参照を外す", "ALTER TABLE salescall.tasks DROP CONSTRAINT tasks_client_id_contact_id_fkey",
     ("tests.test_foundation.ClientIsolation.test_cannot_point_to_other_clients_parent",)),
    ("業務設定の版の変更の拒みを外す", "DROP TRIGGER business_versions_frozen ON salescall.business_versions",
     ("tests.test_foundation.FrozenRecords.test_triggers_refuse_even_the_owner",)),
    # ⚠️「終わり → 発信」は、表のほかに「終わった試行は検証の状態を持つ」決まりでも拒まれる（守りが2重）。
    #    ここでは表だけが守っている「予約 → 終わり」で確かめる。
    ("移ってよい組の表に、予約から終わりへを足す",
     "ALTER TABLE salescall.attempt_transitions DISABLE TRIGGER attempt_transitions_frozen;"
     " INSERT INTO salescall.attempt_transitions VALUES ('claimed', 'ended')",
     ("tests.test_foundation.AttemptTransitions.test_forbidden_moves",)),
    ("期限切れの予約の拒みを外す",
     "CREATE OR REPLACE FUNCTION salescall.attempt_before_update() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN"
     " IF NOT salescall.is_active(NEW.state) THEN NEW.ended_at := now(); NEW.verification := 'pending'; END IF;"
     " RETURN NEW; END $$",
     ("tests.test_foundation.ExpiredClaimCannotDial.test_expired",)),
    ("確定した印（本番の段階）の参照を外す", "ALTER TABLE salescall.deliveries DROP CONSTRAINT deliveries_production_release",
     ("tests.test_foundation.DeliveriesOnlyForReleasedVersions.test_release_then_deliver_once",
      "tests.test_foundation.DeliveriesOnlyForReleasedVersions.test_results_confirmed_in_verification_never_reach_the_requester")),
    ("人の緊急停止をシステムが解けない決まりを外す", "DROP TRIGGER stop_events_before_insert ON salescall.stop_events",
     ("tests.test_stops.ManualStops.test_system_cannot_resume_a_manual_stop",
      "tests.test_stops.ManualStops.test_actor_must_match_the_transaction")),
    ("設定の値の形の確かめを外す", "DROP TRIGGER setting_values_before_insert ON salescall.setting_values",
     ("tests.test_settings.Shapes.test_bad_values_are_rejected",
      "tests.test_settings.Shapes.test_cannot_be_set_finer_than_declared")),
    ("設定の行の見え方を、同じ契約者の中で緩める",
     "ALTER POLICY setting_scope ON salescall.setting_values USING (scope = 'system' OR tenant_id = salescall.cur_tenant())",
     ("tests.test_settings.PerClientSettings.test_other_client_of_the_same_tenant_cannot_see_the_value",)),
    ("設定の枠は、収める設定より細かく決められない決まりを外す（足すときも、移行で書き換えるときも）",
     "DROP TRIGGER setting_definitions_rules_hold ON salescall.setting_definitions",
     ("tests.test_settings.FramesCannotBeFinerThanWhatTheyFrame.test_a_frame_that_a_finer_scope_can_move_is_rejected",
      "tests.test_settings.FramesCannotBeFinerThanWhatTheyFrame.test_a_migration_that_rewrites_definitions_cannot_break_the_frames")),
    ("決められる範囲より細かい設定の値を入れられる",
     "CREATE OR REPLACE FUNCTION salescall.setting_value_before_insert() RETURNS trigger LANGUAGE plpgsql AS $$"
     " DECLARE d salescall.setting_definitions; BEGIN SELECT * INTO d FROM salescall.setting_definitions WHERE key = NEW.key;"
     " IF NOT salescall.setting_value_ok(d.value_type, NEW.value) THEN RAISE EXCEPTION 'x' USING ERRCODE = 'check_violation';"
     " END IF; RETURN NEW; END $$",
     ("tests.test_settings.Shapes.test_cannot_be_set_finer_than_declared",
      "tests.test_settings.PerClientSettings.test_shared_equipment_and_harm_guards_are_system_only")),
    ("「システムだけ」の2つ目の表し方を許す",
     "ALTER TABLE salescall.setting_definitions DROP CONSTRAINT setting_definitions_one_way_for_system_only",
     ("tests.test_settings.FramesCannotBeFinerThanWhatTheyFrame.test_system_only_is_written_one_way",)),
    ("停止を1回だけ解ける決まりを外す", "DROP INDEX salescall.stop_events_resolved_once",
     ("tests.test_stops.SystemStops.test_a_stop_is_resolved_only_once",)),
    ("1つの実施回で管理番号は1つ、の約束を外す", "DROP INDEX salescall.tasks_one_ref_per_run",
     ("tests.test_import.DatabaseGuards.test_one_task_per_client_ref_in_a_run",)),
    ("取込の行ごとの記録の変更の拒みを外す", "DROP TRIGGER import_rows_frozen ON salescall.import_rows",
     ("tests.test_import.DatabaseGuards.test_import_records_cannot_be_changed_even_by_the_owner",)),
    ("取込の記録の行の制限を外す", "ALTER TABLE salescall.import_rows DISABLE ROW LEVEL SECURITY",
     ("tests.test_import.DatabaseGuards.test_other_clients_cannot_see_import_records",)),
    ("契約者の共通の発信禁止で、ほかの依頼者の予約を取り消す処理を空にする",
     "CREATE OR REPLACE FUNCTION salescall.release_claims_for_contractor_dnc(p_phone text) RETURNS integer"
     " LANGUAGE sql SECURITY DEFINER SET search_path = salescall, pg_temp AS $$ SELECT 0 $$",
     ("tests.test_dnc.Register.test_contractor_ledger_applies_to_every_client_of_that_contractor",)),
    ("契約者の共通の発信禁止を、ほかの契約者にも見せる", "ALTER TABLE salescall.contractor_dnc_entries DISABLE ROW LEVEL SECURITY",
     ("tests.test_dnc.Register.test_contractor_ledger_applies_to_every_client_of_that_contractor",)),
    ("契約者の単価を、ほかの契約者にも見せる", "ALTER TABLE salescall.price_versions DISABLE ROW LEVEL SECURITY",
     ("tests.test_usage.PricesPerContractor.test_the_contractors_price_wins_and_tiers_are_separate",)),
    ("判定できない通話を、断られたかが決まったとみなす（前の形）",
     "CREATE OR REPLACE FUNCTION salescall.refusal_undecided(p_attempt uuid) RETURNS boolean LANGUAGE sql STABLE"
     " SET search_path = salescall, pg_temp AS $$ SELECT NOT salescall.is_settled(p_attempt) $$",
     ("tests.test_gate.ActiveConflicts.test_unknown_judgement_keeps_the_same_customer_protected",)),
    ("ほかの依頼者がかけた声を見ない（声を替えない）",
     "CREATE OR REPLACE FUNCTION salescall.number_last_voice_elsewhere(p_phone text) RETURNS text LANGUAGE sql STABLE"
     " SECURITY DEFINER SET search_path = salescall, pg_temp AS $$ SELECT NULL::text $$",
     ("tests.test_gate.CallerNumberAndVoice.test_another_client_or_contractor_gets_the_other_voice",)),
    ("ほかの依頼者の声を、お客様が出なかった発信でも数える",
     "CREATE OR REPLACE FUNCTION salescall.number_last_voice_elsewhere(p_phone text) RETURNS text LANGUAGE sql STABLE"
     " SECURITY DEFINER SET search_path = salescall, pg_temp AS $$ SELECT v.voice_name FROM salescall.attempts a"
     " JOIN salescall.voice_types v ON v.code = a.voice_type WHERE a.phone_e164 = p_phone"
     " AND a.client_id IS DISTINCT FROM salescall.cur_client() AND a.state NOT IN ('claimed', 'released')"
     " ORDER BY a.claimed_at DESC LIMIT 1 $$",
     ("tests.test_gate.CallerNumberAndVoice.test_only_what_the_customer_saw_or_heard_counts",)),
    ("ほかの依頼者の発信番号を、鳴らなかった発信でも数える",
     "CREATE OR REPLACE FUNCTION salescall.line_called_number_elsewhere(p_line uuid, p_phone text) RETURNS boolean"
     " LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$ SELECT EXISTS (SELECT 1"
     " FROM salescall.attempts a WHERE a.line_id = p_line AND a.tenant_id = salescall.cur_tenant()"
     " AND a.phone_e164 = p_phone AND a.client_id IS DISTINCT FROM salescall.cur_client()"
     " AND a.state NOT IN ('claimed', 'released')) $$",
     ("tests.test_gate.CallerNumberAndVoice.test_only_what_the_customer_saw_or_heard_counts",)),
    ("約束したかけ直しかを、あとから変えられる",
     "DROP TRIGGER attempt_record_fixed ON salescall.attempts",
     ("tests.test_gate.NotByCallee.test_a_promised_callback_is_not_counted_afterwards",)),
    ("ほかの依頼者がかけた発信番号を見ない（発信番号を替えない）",
     "CREATE OR REPLACE FUNCTION salescall.line_called_number_elsewhere(p_line uuid, p_phone text) RETURNS boolean"
     " LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$ SELECT false $$",
     ("tests.test_gate.CallerNumberAndVoice.test_caller_number_changes_with_the_client",)),
    ("発信禁止の取り消しは人だけ、の決まりを外す", "DROP TRIGGER dnc_entries_guard ON salescall.dnc_entries",
     ("tests.test_dnc.Revoke.test_only_a_person_revokes_once_and_can_register_again",)),
    ("発信禁止の有効な登録は1つ、の約束を外す", "DROP INDEX salescall.dnc_one_active",
     ("tests.test_dnc.Register.test_any_spelling_is_the_same_entry",)),
    ("管理番号の発信禁止の有効な登録は1つ、の約束を外す", "DROP INDEX salescall.dnc_one_active_ref",
     ("tests.test_dnc.RefusalScope.test_refusing_twice_keeps_one_entry_each",)),
    ("期限の切れた予約を取り消す処理を空にする",
     "CREATE OR REPLACE FUNCTION salescall.release_expired_claims(p_task uuid, p_contact uuid, p_phone text, p_line uuid)"
     " RETURNS integer"
     " LANGUAGE sql SECURITY DEFINER SET search_path = salescall, pg_temp AS $$ SELECT 0 $$",
     ("tests.test_gate.ExpiredClaims.test_expired_claim_is_released_and_cannot_dial",)),
    ("期限の切れた予約も、生きている進行中として数える",
     "CREATE OR REPLACE FUNCTION salescall.is_live(s salescall.attempt_state, expires timestamptz) RETURNS boolean"
     " LANGUAGE sql STABLE AS $$ SELECT salescall.is_active(s) $$",
     ("tests.test_gate.ExpiredClaimsDoNotUseCapacity.test_expired_claims_elsewhere_do_not_block",)),
    ("ほかの依頼者の発信の時刻を見ない",
     "CREATE OR REPLACE FUNCTION salescall.number_last_dial_elsewhere(p_phone text) RETURNS TABLE (relation text, last_at timestamptz)"
     " LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$ SELECT NULL::text, NULL::timestamptz WHERE false $$",
     ("tests.test_gate.Pacing.test_cross_client_interval_tells_same_or_other_tenant_only",)),
    ("回線の停止を、止まっているかの判断から外す",
     "CREATE OR REPLACE FUNCTION salescall.open_stops(p_tenant uuid, p_client uuid, p_business uuid, p_line uuid DEFAULT NULL)"
     " RETURNS SETOF salescall.stop_events LANGUAGE sql STABLE AS $$ SELECT s.* FROM salescall.stop_events s"
     " WHERE s.action = 'stop' AND NOT EXISTS (SELECT 1 FROM salescall.stop_events r WHERE r.resolves_event_id = s.event_id)"
     " AND (s.scope = 'system' OR (s.scope = 'tenant' AND s.tenant_id = p_tenant)"
     " OR (s.scope = 'client' AND s.tenant_id = p_tenant AND s.client_id = p_client)"
     " OR (s.scope = 'business' AND s.tenant_id = p_tenant AND s.client_id = p_client AND s.business_id = p_business)) $$",
     ("tests.test_gate.Stops.test_each_scope_stops_and_the_line_scope_only_its_line",)),
    ("番号ごとの進行中の制約を外す（門から見て）", "DROP INDEX salescall.attempts_one_active_per_number",
     ("tests.test_gate.Concurrency.test_one_number_from_many_clients_only_one_wins",
      "tests.test_gate.ActiveConflicts.test_same_number_elsewhere_tells_only_the_relation")),
    ("回線の停止で、その回線の予約を取り消さない",
     "CREATE OR REPLACE FUNCTION salescall.release_claims_for_stop(p_event_id bigint) RETURNS integer"
     " LANGUAGE sql SECURITY DEFINER SET search_path = salescall, pg_temp AS $$ SELECT 0 $$",
     ("tests.test_gate.Stops.test_line_stop_releases_claims_on_that_line",)),
    ("設定・暦の変更で予約を取り消す処理を空にする",
     "CREATE OR REPLACE FUNCTION salescall.release_claims_in_scope(p_scope text, p_tenant uuid, p_client uuid, p_business uuid)"
     " RETURNS integer LANGUAGE sql SECURITY DEFINER SET search_path = salescall, pg_temp AS $$ SELECT 0 $$",
     ("tests.test_gate.ChangesReleaseClaims.test_setting_change", "tests.test_gate.ChangesReleaseClaims.test_calendar_load")),
    ("お客様の意思と関係ない発信も、間隔・1日の回数に数える",
     "CREATE OR REPLACE FUNCTION salescall.not_by_callee(s salescall.attempt_state, disconnect text, p_attempt uuid)"
     " RETURNS boolean LANGUAGE sql STABLE AS $$ SELECT false $$",
     ("tests.test_gate.NotByCallee.test_failed_or_interrupted_calls_do_not_count",
      "tests.test_gate.Pacing.test_a_call_cut_by_our_failure_is_like_a_line_drop")),
    ("お客様が出る前の結果不明も、検証待ちにする",
     "CREATE OR REPLACE FUNCTION salescall.reached_answer(p_attempt uuid) RETURNS boolean LANGUAGE sql STABLE AS $$ SELECT true $$",
     ("tests.test_gate.Pacing.test_unknown_result_before_answer_needs_no_long_wait",)),
    ("1つの試行に判定の版は1つ、の約束を外す", "DROP INDEX salescall.task_results_one_judgement_per_attempt",
     ("tests.test_settle.DatabaseGuards.test_one_judgement_per_attempt",)),
    ("「わからない」「答えない」の理由は、その状態だけ、の決まりを外す",
     "ALTER TABLE salescall.item_values DROP CONSTRAINT item_values_reason_only_when_not_answered",
     ("tests.test_settle.DatabaseGuards.test_item_reason_only_when_not_answered",)),
    ("番号違い・FAX で管理番号を止めない決まりを外す",
     "ALTER TABLE salescall.dnc_entries DROP CONSTRAINT dnc_entries_ref_not_number_only",
     ("tests.test_dnc.RefusalScope.test_database_refuses_bad_entries",)),
    # 行の番号（DESIGN.md 3-5）
    ("試行は行の番号にだけかけられる、の参照を外す",
     "ALTER TABLE salescall.attempts DROP CONSTRAINT attempts_number_of_task",
     ("tests.test_alternate.DatabasePromises.test_calls_and_schedules_only_to_numbers_of_the_row",)),
    ("予定は行の番号にだけ、の参照を外す",
     "ALTER TABLE salescall.task_next_actions DROP CONSTRAINT task_next_actions_number_of_task",
     ("tests.test_alternate.DatabasePromises.test_calls_and_schedules_only_to_numbers_of_the_row",)),
    ("かけ直す予定は番号を持つ、の決まりを外す",
     "ALTER TABLE salescall.task_next_actions DROP CONSTRAINT task_next_actions_number",
     ("tests.test_alternate.DatabasePromises.test_calls_and_schedules_only_to_numbers_of_the_row",)),
    ("1つの行に進行中の発信は1つ、の制約を外す", "DROP INDEX salescall.attempts_one_active_per_task",
     ("tests.test_alternate.DatabasePromises.test_one_active_call_per_row",)),
    ("タスクを作ったら一覧の番号を足す仕掛けを外す", "DROP TRIGGER task_after_insert ON salescall.tasks",
     ("tests.test_alternate.DatabasePromises.test_list_number_is_added_with_the_task",)),
    ("一覧の番号は1行に1つ、の制約を外す", "DROP INDEX salescall.task_numbers_one_list_number",
     ("tests.test_alternate.DatabasePromises.test_numbers_are_frozen_and_list_number_is_the_task_contact",)),
    ("行の番号の変更の拒みを外す", "DROP TRIGGER task_numbers_frozen ON salescall.task_numbers",
     ("tests.test_alternate.DatabasePromises.test_numbers_are_frozen_and_list_number_is_the_task_contact",)),
    ("期限の切れた予約を戻す範囲から、同じ行を外す",
     "CREATE OR REPLACE FUNCTION salescall.release_expired_claims(p_task uuid, p_contact uuid, p_phone text, p_line uuid)"
     " RETURNS integer LANGUAGE sql SECURITY DEFINER SET search_path = salescall, pg_temp AS $$"
     " WITH r AS (UPDATE salescall.attempts SET state = 'released' WHERE state = 'claimed' AND claim_expires_at <= now()"
     " AND (contact_id = p_contact OR phone_e164 = p_phone OR line_id = p_line) RETURNING 1) SELECT count(*)::integer FROM r $$",
     ("tests.test_alternate.DatabasePromises.test_expired_claim_on_another_number_of_the_row_is_released",)),
    # システムの管理者（CL-3 の 4-1f の3）
    ("システムの値を管理者だけにする仕掛けを外す", "DROP TRIGGER setting_values_system_by_admin ON salescall.setting_values",
     ("tests.test_settings.SystemValuesByAdmin.test_only_admin",)),
    ("全体の範囲の停止を、人では管理者だけにする仕掛けを外す", "DROP TRIGGER system_scope_by_admin ON salescall.stop_events",
     ("tests.test_watch.TenantAiStopsAndChecks.test_manual_stop_is_not_resumed_by_checks",)),
    ("契約者の共通の発信禁止を、管理者か契約者だけにする仕掛けを外す",
     "DROP TRIGGER contractor_dnc_guard ON salescall.contractor_dnc_entries",
     ("tests.test_dnc.Register.test_contractor_ledger_applies_to_every_client_of_that_contractor",)),
    # 承認の部分（10-2）
    ("行を承認できるのは運営者だけ、の仕掛けを外す", "DROP TRIGGER task_approval_by_operator ON salescall.task_approvals",
     ("tests.test_approval.HumanApproval.test_dials_only_approved_rows",)),
    # 利用量の台帳と予算（Q16）
    ("利用量の台帳の変更の拒みを外す", "DROP TRIGGER usage_entries_frozen ON salescall.usage_entries",
     ("tests.test_usage.Ledger.test_frozen",)),
    ("作り直した会話の報告の変更の拒みを外す", "DROP TRIGGER call_report_rebuilds_frozen ON salescall.call_report_rebuilds",
     ("tests.test_settle.Settle.test_rebuilt_reports_cannot_be_changed",)),
    ("お詫びの電話を承認するのは人だけ、の仕掛けを外す", "DROP TRIGGER apology_approval_by_person ON salescall.apology_approvals",
     ("tests.test_apology.Apology.test_the_system_prepares_and_only_a_person_approves",)),
    ("お詫びの指示の変更の拒みを外す", "DROP TRIGGER apology_orders_frozen ON salescall.apology_orders",
     ("tests.test_apology.Apology.test_frozen",)),
    ("業務設定の中身の変更の拒みを外す", "DROP TRIGGER business_contents_frozen ON salescall.business_contents",
     ("tests.test_business_settings.Versions.test_contents_are_frozen",)),
    ("会話で確かめた読みは、その行の通話のもの、の仕掛けを外す",
     "DROP TRIGGER confirmed_reading_of_task ON salescall.task_confirmed_readings",
     ("tests.test_import.Targets.test_an_undecided_reading_is_registered_and_confirmed_in_the_call",)),
    ("タスクの名前の読みの形の約束を外す", "ALTER TABLE salescall.tasks DROP CONSTRAINT task_name_readings",
     ("tests.test_import.Targets.test_the_database_keeps_readings_in_shape",)),
    ("管理者あての知らせを契約者から隠す、の決まりを外す",
     "DROP POLICY notice_scope ON salescall.notification_events; CREATE POLICY notice_scope ON salescall.notification_events"
     " USING (tenant_id IS NULL OR tenant_id = salescall.cur_tenant()) WITH CHECK (true)",
     ("tests.test_notify.Ledger.test_admin_copy_is_seen_only_by_the_admin",)),
    ("報酬の決まりの変更の拒みを外す", "DROP TRIGGER reward_terms_frozen ON salescall.reward_terms",
     ("tests.test_usage.CostRatio.test_terms_are_frozen",)),
    ("いまの結果の版を、最初の版にする",
     "CREATE OR REPLACE VIEW salescall.latest_task_results WITH (security_invoker = true) AS"
     " SELECT DISTINCT ON (r.task_id) r.* FROM salescall.task_results r ORDER BY r.task_id, r.version_no",
     ("tests.test_usage.CostRatio.test_a_completion_corrected_away_is_not_counted",)),
    ("発信禁止の取り消しを、契約者だけにする（前の誤り）",
     "CREATE OR REPLACE FUNCTION salescall.dnc_before_change() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN"
     " IF NEW.revoked_at IS NOT NULL AND salescall.cur_actor_kind() IS DISTINCT FROM 'operator' THEN"
     " RAISE EXCEPTION 'x' USING ERRCODE = 'insufficient_privilege'; END IF; RETURN NEW; END $$",
     ("tests.test_dnc.Revoke.test_the_admin_can_revoke_too",)),
    ("システムが止めた停止を、見守りに見せない",
     "CREATE OR REPLACE FUNCTION salescall.open_system_stops(p_cause text) RETURNS TABLE (event_id bigint, scope text,"
     " tenant_id uuid, client_id uuid, stopped_on date) LANGUAGE sql STABLE SECURITY DEFINER"
     " SET search_path = salescall, pg_temp AS $$ SELECT NULL::bigint, NULL::text, NULL::uuid, NULL::uuid, NULL::date"
     " WHERE false $$",
     ("tests.test_usage.CostRatio.test_deficit_warns_first_and_stops_if_not_confirmed_within_the_grace",)),
    ("見守りに、期限の来た赤字の警告を見せない",
     "CREATE OR REPLACE FUNCTION salescall.open_deficit_warnings() RETURNS TABLE (tenant_id uuid, client_id uuid)"
     " LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$ SELECT NULL::uuid, NULL::uuid"
     " WHERE false $$",
     ("tests.test_usage.CostRatio.test_deficit_warns_first_and_stops_if_not_confirmed_within_the_grace",)),
    ("赤字の警告の確認を、システムの処理にもさせる", "DROP TRIGGER deficit_warning_guard ON salescall.deficit_warnings",
     ("tests.test_usage.CostRatio.test_a_confirmed_deficit_does_not_stop",)),
    ("時給の時間を数えない",
     "CREATE OR REPLACE FUNCTION salescall.work_seconds(p_from timestamptz, p_to timestamptz)"
     " RETURNS TABLE (at timestamptz, seconds numeric) LANGUAGE sql STABLE SECURITY DEFINER"
     " SET search_path = salescall, pg_temp AS $$ SELECT NULL::timestamptz, NULL::numeric WHERE false $$",
     ("tests.test_usage.CostRatio.test_hourly_counts_like_a_person_would",)),
    ("契約者の今日の金額を数えない（いつも0）",
     "CREATE OR REPLACE FUNCTION salescall.spent_today_tenant() RETURNS numeric"
     " LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$ SELECT 0::numeric $$",
     ("tests.test_usage.Budget.test_contractor_limit_counts_only_that_contractor_and_is_only_noticed_to_it",)),
    # 品質の段（Q22）
    ("言ってはいけないことを言ったかを、いつも「言っていない」にする",
     "CREATE OR REPLACE FUNCTION salescall.said_forbidden(p_attempt uuid) RETURNS boolean"
     " LANGUAGE sql STABLE SET search_path = salescall, pg_temp AS $$ SELECT false $$",
     ("tests.test_approval.ForbiddenSpeech.test_the_row_ends_and_the_admin_is_told",)),
    ("検証を済ませる前に結果の数を残す、の仕掛けを外す",
     "DROP TRIGGER attempt_verification_needs_summary ON salescall.attempts",
     ("tests.test_quality.Stages.test_done_needs_the_numbers",)),
    ("録音の承認を、システムの処理でもできる",
     "DROP TRIGGER recording_approval_by_person ON salescall.recording_approvals",
     ("tests.test_gate.CallerNumberAndVoice.test_only_a_person_approves_recordings_and_an_approved_set_is_fixed",)),
    ("承認した録音の組に、部分を足せる",
     "DROP TRIGGER recording_before_insert ON salescall.recordings",
     ("tests.test_gate.CallerNumberAndVoice.test_only_a_person_approves_recordings_and_an_approved_set_is_fixed",)),
    ("依頼者の単位の録音が、ほかの依頼者にも見える",
     "ALTER POLICY recording_scope ON salescall.recordings USING (tenant_id = salescall.cur_tenant())",
     ("tests.test_gate.CallerNumberAndVoice.test_only_a_person_approves_recordings_and_an_approved_set_is_fixed",)),
    ("声の一覧を、管理者でなくても変えられる・使わない印を外せる",
     "DROP TRIGGER voices_guard ON salescall.voices",
     ("tests.test_gate.CallerNumberAndVoice.test_a_new_voice_is_used_without_changing_the_code_and_a_retired_voice_is_not",)),
    ("修正の承認を、管理者でなくてもできる",
     "DROP TRIGGER fix_approval_by_admin ON salescall.fix_approvals",
     ("tests.test_watch.TenantAiStopsAndChecks.test_the_assignee_reports_the_fix_and_only_the_admin_approves",)),
    ("「直した」を、担当の作業でなくても書ける",
     "DROP TRIGGER fix_report_by_system ON salescall.fix_reports",
     ("tests.test_watch.TenantAiStopsAndChecks.test_the_assignee_reports_the_fix_and_only_the_admin_approves",)),
    ("修正依頼が、契約者にも見える",
     "ALTER POLICY fix_scope ON salescall.fix_requests USING (true)",
     ("tests.test_watch.TenantAiStopsAndChecks.test_the_assignee_reports_the_fix_and_only_the_admin_approves",)),
    ("依頼者の名前のほかも直せる・名前を直した記録を残さない",
     "DROP TRIGGER clients_only_name ON salescall.clients",
     ("tests.test_registry.Clients.test_create_rename_and_the_change_is_recorded",)),
    ("依頼者の有効・無効を、システムの処理でも変えられる",
     "DROP TRIGGER client_state_by_person ON salescall.client_states",
     ("tests.test_registry.Clients.test_an_inactive_client_is_not_called_until_it_is_active_again",)),
    ("無効にした依頼者も、門が通す",
     "CREATE OR REPLACE FUNCTION salescall.client_active(p_client uuid) RETURNS boolean LANGUAGE sql STABLE AS $$ SELECT true $$",
     ("tests.test_registry.Clients.test_an_inactive_client_is_not_called_until_it_is_active_again",)),
    ("回線の発信番号を変えられる・使わなくなった印を外せる",
     "DROP TRIGGER line_before_update ON salescall.lines",
     ("tests.test_registry.Lines.test_the_caller_number_is_required_checked_unique_and_fixed",
      "tests.test_registry.Lines.test_a_retired_line_is_not_used_and_its_claims_are_released")),
    ("同じ発信番号の回線を2つ作れる",
     "DROP INDEX salescall.lines_one_per_caller_number",
     ("tests.test_registry.Lines.test_the_caller_number_is_required_checked_unique_and_fixed",)),
    # 依頼者ごとの控え（3-10・0054）
    ("提出用のファイルの、写した記録を変えられる",
     "DROP TRIGGER submission_copied_once ON salescall.submission_files",
     ("tests.test_backup.PerClient.test_a_contractor_backs_up_only_its_own_clients_and_copies_submissions",)),
    ("依頼者の控えのフォルダの名前を、あとから変えられる",
     "DROP TRIGGER client_backup_folders_frozen ON salescall.client_backup_folders",
     ("tests.test_backup.PerClient.test_folders_follow_the_client_name_and_fall_back_to_client_numbers",)),
    ("大文字・小文字だけ違うフォルダの名前を2つ作れる",
     "DROP INDEX salescall.client_backup_folders_one_name",
     ("tests.test_backup.PerClient.test_folders_follow_the_client_name_and_fall_back_to_client_numbers",)),
    ("測り終わる前の録音を消せる（印を見ない）",
     "CREATE OR REPLACE FUNCTION salescall.recordings_held(p_attempts uuid[]) RETURNS SETOF uuid LANGUAGE sql STABLE"
     " SECURITY DEFINER SET search_path = salescall, pg_temp AS $$ SELECT NULL::uuid WHERE false $$",
     ("tests.test_settle.RecordingHolds.test_verification_calls_keep_their_recordings_until_measured",)),
    ("外した録音の印を戻せる",
     "DROP TRIGGER recording_hold_released_once ON salescall.recording_holds",
     ("tests.test_settle.RecordingHolds.test_verification_calls_keep_their_recordings_until_measured",)),
    ("控えの残す日数を0にできる",
     "ALTER TABLE salescall.setting_values DROP CONSTRAINT setting_values_keep_days_at_least_one",
     ("tests.test_backup.PerClient.test_keep_days_are_at_least_one_and_backup_lookups_are_for_the_backup_only",)),
    ("ほかの契約者の控えの情報を、誰の取引からでも読める",
     "CREATE OR REPLACE FUNCTION salescall.only_backup_system() RETURNS void LANGUAGE plpgsql STABLE AS $$ BEGIN END $$",
     ("tests.test_backup.PerClient.test_keep_days_are_at_least_one_and_backup_lookups_are_for_the_backup_only",)),
    ("同じ不具合の修正依頼が、いくつも出る",
     "CREATE OR REPLACE FUNCTION salescall.request_fix(p_assignee text, p_kind text, p_key text, p_summary text, p_refs jsonb)"
     " RETURNS boolean LANGUAGE sql SECURITY DEFINER SET search_path = salescall, pg_temp AS $$"
     " INSERT INTO salescall.fix_requests (assignee, kind, event_key, fault_key, summary, refs)"
     " VALUES (p_assignee, p_kind, p_key || '#' || gen_random_uuid(), p_key, p_summary, coalesce(p_refs, '{}')) RETURNING true $$",
     ("tests.test_backup.PerClient.test_one_fix_request_per_fault_until_reported_fixed",)),
    ("声の型を、誰でも足せる・書き換えられる",
     "DROP TRIGGER voice_types_guard ON salescall.voice_types",
     ("tests.test_gate.CallerNumberAndVoice.test_a_new_voice_is_used_without_changing_the_code_and_a_retired_voice_is_not",)),
    # 見守り役（5-4・0063）
    ("ほかの見守り役が生きて借りている回線を借りられる",
     "CREATE OR REPLACE FUNCTION salescall.lease_line(p_line uuid, p_holder text, p_seconds integer) RETURNS timestamptz"
     " LANGUAGE sql SECURITY DEFINER SET search_path = salescall, pg_temp AS $$"
     " UPDATE salescall.lines SET lease_holder = p_holder, lease_expires_at = now() + make_interval(secs => p_seconds)"
     " WHERE line_id = p_line AND retired_at IS NULL RETURNING lease_expires_at $$",
     ("tests.test_supervisor.Leases.test_one_supervisor_holds_a_line_and_another_takes_it_only_after_it_expires",)),
    ("生きている回線の通話を、後始末で終わらせてしまう",
     "CREATE OR REPLACE FUNCTION salescall.recover_lines(p_holder text, p_line uuid DEFAULT NULL, p_except text DEFAULT NULL,"
     " p_expired_for interval DEFAULT '0 seconds')"
     " RETURNS TABLE (line_id uuid, released integer, ended integer) LANGUAGE plpgsql SECURITY DEFINER"
     " SET search_path = salescall, pg_temp AS $$ DECLARE l record; r integer; e integer; BEGIN"
     " FOR l IN SELECT x.line_id FROM salescall.lines x WHERE x.tenant_id = salescall.cur_tenant() LOOP"
     " UPDATE salescall.attempts SET state = 'released' WHERE attempts.line_id = l.line_id AND state = 'claimed';"
     " GET DIAGNOSTICS r = ROW_COUNT; line_id := l.line_id; released := r; ended := 0; RETURN NEXT; END LOOP; END $$",
     ("tests.test_supervisor.Recovery.test_only_expired_lines_of_other_pcs_are_cleaned_up",)),
    # 落ちた試行の後始末（5-4・0076。2026-10-18 の見直し）
    ("決まった間隔の後始末が、自分の回線のまだ話している通話を終わらせる",
     "CREATE OR REPLACE FUNCTION salescall.recover_lines(p_holder text, p_line uuid DEFAULT NULL, p_except text DEFAULT NULL,"
     " p_expired_for interval DEFAULT '0 seconds')"
     " RETURNS TABLE (line_id uuid, released integer, ended integer) LANGUAGE plpgsql SECURITY DEFINER"
     " SET search_path = salescall, pg_temp AS $$ DECLARE l record; r integer; BEGIN"
     " FOR l IN SELECT x.line_id FROM salescall.lines x WHERE x.tenant_id = salescall.cur_tenant()"
     " AND x.lease_holder IS NOT NULL AND (CASE WHEN p_holder IS NULL THEN x.lease_expires_at < now() - p_expired_for"
     " ELSE x.lease_holder = p_holder END) LOOP"
     " UPDATE salescall.attempts SET state = 'released' WHERE attempts.line_id = l.line_id AND state = 'claimed';"
     " GET DIAGNOSTICS r = ROW_COUNT; line_id := l.line_id; released := r; ended := 0; RETURN NEXT; END LOOP; END $$",
     ("tests.test_supervisor.Recovery.test_the_periodic_cleanup_leaves_its_own_lines_and_the_admin_pc_cleans_stale_ones",)),
    ("契約者の取引から、ほかの契約者の落ちた回線を探せる",
     "CREATE OR REPLACE FUNCTION salescall.tenants_with_stale_lines(p_expired_for interval) RETURNS SETOF uuid"
     " LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$"
     " SELECT DISTINCT x.tenant_id FROM salescall.lines x WHERE x.lease_holder IS NOT NULL"
     " AND x.lease_expires_at < now() - p_expired_for $$",
     ("tests.test_supervisor.Recovery.test_the_periodic_cleanup_leaves_its_own_lines_and_the_admin_pc_cleans_stale_ones",)),
    # 契約者ごとの合言葉（11章・0077。2026-10-18 ユーザー決定）
    ("契約者の合言葉を、データベースが見分けない（ほかの契約者・全体の取引を開ける・管理者として動ける）",
     "CREATE OR REPLACE FUNCTION salescall.login_tenant() RETURNS uuid LANGUAGE sql STABLE AS $$ SELECT NULL::uuid $$",
     ("tests.test_logins.Scope.test_a_contractor_login_reaches_only_that_contractor",)),
    ("契約者の取引から、祝日の暦を書ける",
     "DROP TRIGGER calendar_loads_by_system ON salescall.calendar_loads",
     ("tests.test_logins.Scope.test_a_contractor_login_reaches_only_that_contractor",)),
    ("契約者の合言葉で、全体の仕事・ほかの契約者の仕事を借りられる",
     "CREATE OR REPLACE FUNCTION salescall.take_job(p_key text, p_holder text, p_seconds integer) RETURNS boolean"
     " LANGUAGE plpgsql SECURITY DEFINER SET search_path = salescall, pg_temp AS $$ DECLARE got text; BEGIN"
     " INSERT INTO salescall.job_leases AS j (job_key, holder, expires_at) VALUES (p_key, p_holder,"
     " now() + make_interval(secs => p_seconds)) ON CONFLICT (job_key) DO UPDATE SET holder = EXCLUDED.holder,"
     " expires_at = EXCLUDED.expires_at WHERE j.expires_at < now() OR j.holder = EXCLUDED.holder RETURNING holder INTO got;"
     " RETURN got IS NOT NULL; END $$",
     ("tests.test_logins.Scope.test_a_contractor_login_reaches_only_that_contractor",)),
    ("時給の数え方で、ほかの契約者の依頼者の通話を数える",
     "CREATE OR REPLACE FUNCTION salescall.work_seconds(p_from timestamptz, p_to timestamptz)"
     " RETURNS TABLE (at timestamptz, seconds numeric) LANGUAGE sql STABLE SECURITY DEFINER"
     " SET search_path = salescall, pg_temp AS $$ SELECT a.claimed_at, extract(epoch FROM a.ended_at - a.claimed_at)"
     " FROM salescall.attempts a WHERE a.client_id = salescall.cur_client() AND a.ended_at IS NOT NULL $$",
     ("tests.test_logins.Scope.test_a_contractor_login_reaches_only_that_contractor",)),
    ("契約者が、合言葉を払い出せる",
     "DROP TRIGGER tenant_login_event_guard ON salescall.tenant_login_events",
     ("tests.test_logins.Issue.test_only_the_admin_or_the_system_issues_and_the_password_is_never_shown",)),
    # 「かけない」の例外と、営業として新しくかける電話か（3-4-2・5-2 の7b・0078。2026-10-18 ユーザー）
    ("「かけない」の行の、お客様と約束したかけ直しを止める",
     "CREATE OR REPLACE FUNCTION salescall.task_excluded_now(p_task uuid) RETURNS boolean LANGUAGE sql STABLE AS"
     " $$ SELECT salescall.task_excluded(p_task) $$",
     ("tests.test_exclusions.Marks.test_apologies_resumes_and_promised_calls_are_made_even_on_a_row_set_to_do_not_call",)),
    ("お詫び・かけ直し・約束したかけ直しも、業務の上限で止める",
     "CREATE OR REPLACE FUNCTION salescall.is_new_call(p_purpose text, p_promised boolean) RETURNS boolean"
     " LANGUAGE sql IMMUTABLE AS $$ SELECT true $$",
     ("tests.test_gate.BusinessDailyLimit.test_the_limit_counts_the_business_dials_of_today_and_skips_promised_calls",
      "tests.test_exclusions.Marks.test_apologies_resumes_and_promised_calls_are_made_even_on_a_row_set_to_do_not_call")),
    ("「強制かけない」の行でも、お客様と約束したかけ直しをかける",
     "CREATE OR REPLACE FUNCTION salescall.task_exclusion_level(p_task uuid) RETURNS text LANGUAGE sql STABLE AS"
     " $$ SELECT CASE WHEN salescall.task_excluded(p_task) THEN 'exclude' ELSE 'call' END $$;"
     " CREATE OR REPLACE FUNCTION salescall.task_excluded(p_task uuid) RETURNS boolean LANGUAGE sql STABLE AS"
     " $$ SELECT coalesce((SELECT e.excluded FROM salescall.task_exclusions e WHERE e.task_id = p_task"
     " ORDER BY e.seq DESC LIMIT 1), false) $$",
     ("tests.test_exclusions.Marks.test_force_stops_every_call_of_the_row",)),
    # 契約者の鍵の見直し（11章・0084。2026-10-18）
    ("契約者の取引から、ほかの契約者の管理者あての知らせが読める",
     "DROP POLICY notice_scope ON salescall.notification_events; CREATE POLICY notice_scope ON salescall.notification_events"
     " USING ((tenant_id IS NULL AND (NOT for_admin OR salescall.cur_actor_kind() IN ('admin', 'system')))"
     " OR (tenant_id = salescall.cur_tenant() AND (client_id IS NULL OR salescall.cur_client() IS NULL"
     " OR client_id = salescall.cur_client())))"
     " WITH CHECK ((tenant_id IS NULL AND salescall.cur_tenant() IS NULL AND NOT for_admin)"
     " OR (tenant_id = salescall.cur_tenant() AND client_id IS NOT DISTINCT FROM salescall.cur_client()))",
     ("tests.test_logins.Scope.test_admin_notices_jobs_and_stops_of_others_are_out_of_reach",)),
    ("PC の鍵を、形の違う名前・合言葉の平文で作れる",
     "GRANT CREATE ON SCHEMA salescall TO salescall_login_maker; SET ROLE salescall_login_maker; CREATE OR REPLACE FUNCTION salescall.make_pc_login(p_role text, p_verifier text) RETURNS void LANGUAGE plpgsql"
     " SECURITY DEFINER SET search_path = salescall, pg_temp AS $$ BEGIN"
     " IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = p_role) THEN"
     " EXECUTE format('ALTER ROLE %I LOGIN PASSWORD %L', p_role, p_verifier); ELSE"
     " EXECUTE format('CREATE ROLE %I LOGIN PASSWORD %L IN ROLE salescall_app', p_role, p_verifier); END IF; END $$; RESET ROLE",
     ("tests.test_logins.Scope.test_the_issuing_login_can_only_make_and_stop_pc_keys",)),
    ("PC の鍵を止めても、つないだままの接続が残る",
     "GRANT CREATE ON SCHEMA salescall TO salescall_login_maker; SET ROLE salescall_login_maker; CREATE OR REPLACE FUNCTION salescall.stop_pc_logins(p_name text) RETURNS integer LANGUAGE plpgsql"
     " SECURITY DEFINER SET search_path = salescall, pg_temp AS $$ DECLARE r record; n integer := 0; BEGIN"
     " FOR r IN SELECT rolname FROM pg_roles WHERE CASE WHEN p_name LIKE '%\\_' THEN starts_with(rolname, p_name)"
     " ELSE rolname = p_name END LOOP EXECUTE format('ALTER ROLE %I NOLOGIN', r.rolname); n := n + 1; END LOOP;"
     " RETURN n; END $$; RESET ROLE",
     ("tests.test_logins.Scope.test_the_issuing_login_can_only_make_and_stop_pc_keys",)),
    ("控えのフォルダの名前から、ほかの契約者の依頼者が分かる",
     "CREATE OR REPLACE FUNCTION salescall.backup_folder_taken(p_name text) RETURNS boolean LANGUAGE plpgsql STABLE"
     " SECURITY DEFINER SET search_path = salescall, pg_temp AS $$ BEGIN PERFORM salescall.only_backup_system();"
     " RETURN EXISTS (SELECT 1 FROM salescall.client_backup_folders WHERE lower(folder_name) = lower(p_name)); END $$",
     ("tests.test_backup.PerClient.test_folder_names_do_not_show_other_contractors_clients",)),
    ("ほかの契約者・全体の仕事の借用を返せる",
     "CREATE OR REPLACE FUNCTION salescall.return_job(p_key text, p_holder text) RETURNS boolean LANGUAGE sql"
     " SECURITY DEFINER SET search_path = salescall, pg_temp AS $$"
     " UPDATE salescall.job_leases SET expires_at = now() WHERE job_key = p_key AND holder = p_holder RETURNING true $$",
     ("tests.test_logins.Scope.test_admin_notices_jobs_and_stops_of_others_are_out_of_reach",)),
    # 誤判定率を通話ごとに（10-3・0074。2026-10-18 の見直し）
    ("誤判定率の正解の出どころを、呼ぶ側が偽れる",
     "CREATE OR REPLACE FUNCTION salescall.judgement_check_guard() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN"
     " IF salescall.cur_actor_kind() IS DISTINCT FROM 'operator' AND salescall.cur_actor_kind() IS DISTINCT FROM 'admin' THEN"
     " RAISE EXCEPTION 'x' USING ERRCODE = 'insufficient_privilege'; END IF;"
     " IF NOT salescall.call_to_measure(NEW.attempt_id) THEN RAISE EXCEPTION 'x' USING ERRCODE = 'check_violation'; END IF;"
     " RETURN NEW; END $$",
     ("tests.test_measure.Verification.test_the_source_is_decided_by_the_call_and_each_call_counts_once",)),
    ("お客様と話していない通話に、誤判定率の正解を付けられる・検証の印が外れない",
     "CREATE OR REPLACE FUNCTION salescall.call_to_measure(p_attempt uuid) RETURNS boolean LANGUAGE sql STABLE AS $$ SELECT true $$",
     ("tests.test_measure.Verification.test_calls_without_a_conversation_are_approved_and_their_mark_is_released",)),
    ("その通話の古い判定の版に、誤判定率の正解を付けられる（1本を2回数える）",
     "CREATE OR REPLACE FUNCTION salescall.judgement_check_guard() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN"
     " IF NOT EXISTS (SELECT 1 FROM salescall.task_results r WHERE r.task_id = NEW.task_id AND r.version_no = NEW.version_no"
     " AND r.kind IN ('judged', 'rejudged') AND r.attempt_id = NEW.attempt_id) THEN RAISE EXCEPTION 'x' USING ERRCODE = 'check_violation';"
     " END IF; NEW.source := 'correction'; RETURN NEW; END $$",
     ("tests.test_measure.Verification.test_the_truth_goes_on_the_latest_judgement_of_the_call",)),
    ("同じ仕事を2つの見守り役が同時にできる",
     "CREATE OR REPLACE FUNCTION salescall.take_job(p_key text, p_holder text, p_seconds integer) RETURNS boolean"
     " LANGUAGE sql AS $$ SELECT true $$",
     ("tests.test_supervisor.Leases.test_jobs_are_taken_by_one_supervisor_and_only_by_the_system",)),
    # 行ごとの「かけない」（3-4-2・0071）
    ("システムの処理が、ダッシュボードの「かけない」を付け外しできる",
     "DROP TRIGGER task_exclusion_guard ON salescall.task_exclusions",
     ("tests.test_exclusions.Marks.test_dashboard_marks_stop_the_row_until_it_is_put_back",)),
    ("「かけない」の行に、門が通す・候補に出す",
     "CREATE OR REPLACE FUNCTION salescall.task_exclusion_level(p_task uuid) RETURNS text LANGUAGE sql STABLE AS"
     " $$ SELECT 'call'::text $$",   # 印の正本は task_exclusion_level（0085）
     ("tests.test_exclusions.Marks.test_dashboard_marks_stop_the_row_until_it_is_put_back",)),
    ("「かけない」の記録を書き換えられる",
     "DROP TRIGGER task_exclusions_frozen ON salescall.task_exclusions",
     ("tests.test_exclusions.Marks.test_dashboard_marks_stop_the_row_until_it_is_put_back",)),
    # 番号ごとの応答率（5-2-3・0068）
    ("鳴らせなかった通話も、応答率の本数に数える",
     "CREATE OR REPLACE FUNCTION salescall.line_answer_counts(p_line uuid, p_from date, p_to date) RETURNS TABLE (day date,"
     " hour integer, dials integer, answers integer) LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$"
     " SELECT salescall.jst(a.claimed_at)::date, extract(hour FROM salescall.jst(a.claimed_at))::integer, count(*)::integer,"
     " count(*) FILTER (WHERE EXISTS (SELECT 1 FROM salescall.attempt_state_history h WHERE h.attempt_id = a.attempt_id"
     " AND h.to_state = 'answered'))::integer FROM salescall.attempts a WHERE a.line_id = p_line"
     " AND a.tenant_id = salescall.cur_tenant() AND salescall.cur_client() IS NULL"
     " AND salescall.jst(a.claimed_at)::date BETWEEN p_from AND p_to AND a.state <> 'claimed' GROUP BY 1, 2 $$",
     ("tests.test_answer_rate.Counts.test_only_rung_calls_are_counted_and_only_for_that_contractor",)),
    # 誤判定率の正解と抜き取り（10-3・0066）
    ("システムの処理が、誤判定率の正解を付けられる・人の版に付けられる",
     "DROP TRIGGER judgement_check_guard ON salescall.judgement_checks",
     ("tests.test_measure.Verification.test_only_a_person_checks_and_only_system_versions",)),
    ("誤判定率の正解を書き換えられる",
     "DROP TRIGGER judgement_checks_frozen ON salescall.judgement_checks",
     ("tests.test_measure.Verification.test_only_a_person_checks_and_only_system_versions",)),
    # 発信禁止の一覧の取込（3-6-2・0065）
    ("依頼者の一覧の登録を、取込を通さずに入れられる",
     "ALTER TABLE salescall.dnc_entries DROP CONSTRAINT dnc_entries_list_from_import",
     ("tests.test_dnc_import.Guards.test_only_a_person_imports_and_records_and_list_entries_are_fixed",)),
    ("システムの処理が、発信禁止の一覧を取り込める",
     "DROP TRIGGER dnc_import_by_person ON salescall.dnc_imports",
     ("tests.test_dnc_import.Guards.test_only_a_person_imports_and_records_and_list_entries_are_fixed",)),
    ("一覧から入った登録の、取込を指す欄を書き換えられる",
     "CREATE OR REPLACE FUNCTION salescall.dnc_before_change() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN"
     " IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'x' USING ERRCODE = 'integrity_constraint_violation'; END IF;"
     " IF OLD.revoked_at IS NOT NULL OR (NEW.phone_e164, NEW.client_ref, NEW.reason) IS DISTINCT FROM"
     " (OLD.phone_e164, OLD.client_ref, OLD.reason) THEN RAISE EXCEPTION 'x' USING ERRCODE = 'integrity_constraint_violation';"
     " END IF; RETURN NEW; END $$",
     ("tests.test_dnc_import.Guards.test_only_a_person_imports_and_records_and_list_entries_are_fixed",)),
    ("発信禁止の一覧の取込の記録を書き換えられる",
     "DROP TRIGGER dnc_imports_frozen ON salescall.dnc_imports",
     ("tests.test_dnc_import.Guards.test_only_a_person_imports_and_records_and_list_entries_are_fixed",)),
    ("ほかの契約者の、発信禁止の一覧の取込が見える",
     "ALTER TABLE salescall.dnc_imports DISABLE ROW LEVEL SECURITY",
     ("tests.test_dnc_import.Guards.test_only_a_person_imports_and_records_and_list_entries_are_fixed",)),
    # 費用の見張りと控えは契約者ごと（0064。2026-10-18 ユーザー）
    ("契約者の取引から、ほかの契約者の控えの記録が読める",
     "DROP POLICY backup_scope ON salescall.backup_runs;"
     " CREATE POLICY backup_scope ON salescall.backup_runs USING (true)"
     " WITH CHECK (salescall.cur_actor_kind() = 'system' AND tenant_id IS NOT DISTINCT FROM salescall.cur_tenant())",
     ("tests.test_backup.PerClient.test_a_contractor_backs_up_only_its_own_clients_and_copies_submissions",)),
    ("データベース全体の控え（すべての契約者のデータ）の記録に、契約者を付けられる",
     "ALTER TABLE salescall.backup_runs DROP CONSTRAINT backup_runs_whole_has_no_tenant",
     ("tests.test_backup.PerClient.test_the_whole_database_is_backed_up_only_by_the_admin_pc",)),
    ("契約者の今日の費用に、ほかの契約者の費用が入る",
     "CREATE OR REPLACE FUNCTION salescall.spent_today_tenant() RETURNS numeric LANGUAGE sql STABLE SECURITY DEFINER"
     " SET search_path = salescall, pg_temp AS $$ SELECT CASE WHEN salescall.cur_client() IS NOT NULL THEN NULL ELSE"
     " (SELECT coalesce(sum(amount_yen), 0) FROM salescall.usage_entries"
     "  WHERE salescall.jst(occurred_at)::date = salescall.jst(now())::date) END $$",
     ("tests.test_usage.Budget.test_contractor_limit_counts_only_that_contractor_and_is_only_noticed_to_it",)),
    ("知らせの「同じ出来事」を、契約者をまたいで1つにする（2人目の契約者の知らせが落ちる）",
     "ALTER TABLE salescall.notification_events ADD CONSTRAINT notification_one_key_for_all UNIQUE (event_key)",
     ("tests.test_backup.PerClient.test_a_missing_key_is_asked_to_that_contractor",)),
    ("管理者あての写しが、ほかの契約者の同じ出来事とまとまる",
     "CREATE OR REPLACE FUNCTION salescall.notify_admin(p_kind text, p_key text, p_summary text, p_refs jsonb) RETURNS boolean"
     " LANGUAGE plpgsql SECURITY DEFINER SET search_path = salescall, pg_temp AS $$ DECLARE got bigint; BEGIN"
     " INSERT INTO salescall.notification_events (tenant_id, client_id, kind, event_key, summary, refs, for_admin, origin_tenant)"
     " VALUES (NULL, NULL, p_kind, 'admin:' || p_key, p_summary, coalesce(p_refs, '{}'::jsonb), true, NULL)"
     " ON CONFLICT (event_key, tenant_id, origin_tenant) DO NOTHING RETURNING event_id INTO got; RETURN got IS NOT NULL; END $$",
     ("tests.test_backup.PerClient.test_failures_are_asked_to_fix_by_kind_and_told_to_the_admin_and_that_contractor",)),
    ("契約者の取引から、ほかの契約者の止まっている依頼者が見える",
     "CREATE OR REPLACE FUNCTION salescall.open_system_stops(p_cause text) RETURNS TABLE (event_id bigint, scope text,"
     " tenant_id uuid, client_id uuid, stopped_on date) LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp"
     " AS $$ SELECT s.event_id, s.scope::text, s.tenant_id, s.client_id, salescall.jst(s.at)::date FROM salescall.open_stop_events s"
     " WHERE s.cause::text = p_cause AND s.actor_kind = 'system' $$",
     ("tests.test_watch.TenantAiStopsAndChecks.test_counting_needs_the_transaction_of_that_scope",)),
    ("前の発信の締めが済んでいない行を、門が通す・候補に出す",
     "CREATE OR REPLACE FUNCTION salescall.task_awaiting_decision(p_task uuid) RETURNS boolean LANGUAGE sql AS $$ SELECT false $$",
     ("tests.test_gate.Pacing.test_unsettled_attempt_blocks_the_row",)),
    ("通話後の確かめを「確かめられなかった」で諦められる",
     "ALTER TABLE salescall.attempts DROP CONSTRAINT attempts_verification_never_gives_up",
     ("tests.test_foundation.AttemptTransitions.test_verification_only_pending_to_done",
      "tests.test_settle.Settle.test_a_verification_that_cannot_run_waits_and_never_ends_the_row")),
    # 見守り（Q9・9-1）
    ("何回続いたかを数えない（いつも0）",
     "CREATE OR REPLACE FUNCTION salescall.consecutive_errors(p_kinds text[], p_cause text, p_evidence text,"
     " p_line uuid DEFAULT NULL) RETURNS integer LANGUAGE sql STABLE SECURITY DEFINER"
     " SET search_path = salescall, pg_temp AS $$ SELECT 0 $$",
     ("tests.test_watch.LineStops.test_consecutive_line_errors_stop_only_that_line",)),
    ("呼び出せた発信で数え直さない",
     "CREATE OR REPLACE FUNCTION salescall.consecutive_errors(p_kinds text[], p_cause text, p_evidence text,"
     " p_line uuid DEFAULT NULL) RETURNS integer LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$"
     " SELECT count(*)::integer FROM salescall.error_events e WHERE e.kind = ANY(p_kinds)"
     " AND (p_line IS NULL OR e.line_id = p_line) $$",
     ("tests.test_watch.LineStops.test_a_call_that_rang_resets_the_count",
      "tests.test_watch.LineStops.test_a_person_resume_restarts_the_count_and_explain_shows_why")),
    ("続いたエラーを、直った証拠（呼び出せた発信など）で数え直さない",
     "CREATE OR REPLACE FUNCTION salescall.consecutive_errors(p_kinds text[], p_cause text, p_evidence text,"
     " p_line uuid DEFAULT NULL) RETURNS integer LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$"
     " SELECT count(*)::integer FROM salescall.error_events e WHERE e.kind = ANY(p_kinds)"
     " AND e.tenant_id = salescall.cur_tenant() AND (p_line IS NULL OR e.line_id = p_line) $$",
     ("tests.test_watch.LineStops.test_a_call_that_rang_resets_the_count",)),
    ("エラーの数を、範囲の外の取引にも見せる",
     "CREATE OR REPLACE FUNCTION salescall.consecutive_errors(p_kinds text[], p_cause text, p_evidence text,"
     " p_line uuid DEFAULT NULL) RETURNS integer LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$"
     " SELECT count(*)::integer FROM salescall.error_events e WHERE e.kind = ANY(p_kinds) $$",
     ("tests.test_watch.TenantAiStopsAndChecks.test_counting_needs_the_transaction_of_that_scope",)),
    ("停止を解く確かめを、範囲の外の取引からも書ける",
     "DROP POLICY check_scope ON salescall.stop_checks; CREATE POLICY check_scope ON salescall.stop_checks"
     " USING (true) WITH CHECK (true)",
     ("tests.test_watch.TenantAiStopsAndChecks.test_checks_only_from_that_contractor",)),
    ("解いた停止も、まだ止まっているとみなす",
     "CREATE OR REPLACE VIEW salescall.open_stop_events WITH (security_invoker = true) AS"
     " SELECT s.* FROM salescall.stop_events s WHERE s.action = 'stop'",
     ("tests.test_emergency.Emergency.test_a_resume_on_the_screen_clears_the_flag",
      "tests.test_watch.TenantAiStopsAndChecks.test_an_ai_stop_resumes_after_successful_checks")),
    ("止めた理由の説明に、数えたエラーを出さない",
     "CREATE OR REPLACE FUNCTION salescall.errors_for_stop(p_stop bigint, p_kinds text[], p_limit integer)"
     " RETURNS TABLE (error_id bigint, source text, kind text, code integer, message text, occurred_at timestamptz)"
     " LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$"
     " SELECT NULL::bigint, NULL::text, NULL::text, NULL::integer, NULL::text, NULL::timestamptz WHERE false $$",
     ("tests.test_watch.LineStops.test_a_person_resume_restarts_the_count_and_explain_shows_why",)),
    ("停止を解く確かめの記録の変更の拒みを外す", "DROP TRIGGER stop_checks_frozen ON salescall.stop_checks",
     ("tests.test_watch.TenantAiStopsAndChecks.test_checks_are_frozen",)),
    # 知らせの台帳（Q23）
    ("同じ出来事は1回だけ、の約束を外す",
     "DROP INDEX salescall.notification_events_one",
     ("tests.test_notify.Ledger.test_same_event_only_once_and_unknown_kind_is_refused",)),
    ("知らせの台帳の見える範囲を外す", "ALTER TABLE salescall.notification_events DISABLE ROW LEVEL SECURITY",
     ("tests.test_notify.Ledger.test_who_can_see",)),
    ("知らせの台帳の変更の拒みを外す", "DROP TRIGGER notification_events_frozen ON salescall.notification_events",
     ("tests.test_notify.Ledger.test_frozen",)),
    ("お客様が指定した時刻は約束したかけ直しだけ、の決まりを外す",
     "ALTER TABLE salescall.task_next_actions DROP CONSTRAINT task_next_actions_callee_at",
     ("tests.test_gate.Hours.test_callee_time_only_on_promised_calls",)),
    ("発信の目的は変えられない、の仕掛けを外す", "DROP TRIGGER attempt_purpose_fixed ON salescall.attempts",
     ("tests.test_alternate.WrongNumber.test_as_said_calls_the_first_party_to_ask_again",)),
    ("聞き直しの予定は違った番号を持つ、の決まりを外す",
     "ALTER TABLE salescall.task_next_actions DROP CONSTRAINT task_next_actions_wrong_number",
     ("tests.test_alternate.WrongNumber.test_database_promises",)),
    ("聞き直しの発信は違った番号を持つ、の決まりを外す",
     "ALTER TABLE salescall.attempts DROP CONSTRAINT attempts_wrong_number",
     ("tests.test_alternate.WrongNumber.test_database_promises",)),
    ("番号の確かめは1つの通話に1回、の約束を外す",
     "ALTER TABLE salescall.number_rechecks DROP CONSTRAINT number_rechecks_pkey",
     ("tests.test_alternate.WrongNumber.test_recheck_is_recorded_only_for_a_wrong_number_the_callee_gave",)),
    ("番号の確かめの変更の拒みを外す", "DROP TRIGGER number_rechecks_frozen ON salescall.number_rechecks",
     ("tests.test_alternate.WrongNumber.test_database_promises",)),
    ("別の番号を言われた通話は同じ行の通話、の参照を外す",
     "ALTER TABLE salescall.task_numbers DROP CONSTRAINT task_numbers_client_id_source_attempt_id_task_id_fkey",
     ("tests.test_alternate.DatabasePromises.test_callee_number_needs_a_call_of_the_same_row",)),
    # 約束したかけ直しの取り置き・次の回（0089。2026-10-19 ユーザー決定）
    ("回線の1日の上限で、今日の約束の電話の分を取り置かない",
     "CREATE OR REPLACE FUNCTION salescall.line_promised_due(p_line uuid, p_day date, p_except uuid) RETURNS integer"
     " LANGUAGE sql STABLE AS $$ SELECT 0 $$",
     ("tests.test_supervisor.Rounds.test_promised_calls_go_first_across_clients_and_keep_their_share_of_the_line",)),
    ("時刻を指定した約束の前でも、その回線でほかの電話を始める",
     "CREATE OR REPLACE FUNCTION salescall.line_kept_for_promise(p_line uuid, p_until timestamptz, p_except uuid)"
     " RETURNS boolean LANGUAGE sql STABLE AS $$ SELECT false $$",
     ("tests.test_supervisor.Rounds.test_an_exact_time_promise_keeps_one_line_free_shortly_before",)),
    ("業務の平均通話時間を数えない（いつも初めの値）",
     "CREATE OR REPLACE FUNCTION salescall.business_call_seconds(p_business uuid, p_days integer)"
     " RETURNS TABLE (seconds integer, calls integer) LANGUAGE sql STABLE AS $$ SELECT 0, 0 $$",
     ("tests.test_supervisor.Rounds.test_the_guard_is_the_average_call_length_of_the_business_once_measured",)),
    # 約束の電話の取り置きと空けを、契約者の回線全体で数える（0092。2026-10-19 の見直し）
    ("回線の1日の上限で、どの回線も同じ約束をまるごと取り置く（回線の数で分けない）",
     "CREATE OR REPLACE FUNCTION salescall.line_promised_due(p_line uuid, p_day date, p_except uuid) RETURNS integer"
     " LANGUAGE sql STABLE AS $$ SELECT count(*)::integer FROM salescall.promises_waiting() w"
     " WHERE w.task_id <> p_except AND salescall.jst(w.call_at)::date <= p_day $$",
     ("tests.test_supervisor.Rounds.test_the_daily_reservation_is_shared_by_the_lines_and_skips_promises_that_will_not_be_called_now",)),
    ("これからかける約束に、止まっている依頼者の約束・通話中の約束も入れる",
     "CREATE OR REPLACE FUNCTION salescall.promises_waiting()"
     " RETURNS TABLE (task_id uuid, client_id uuid, call_at timestamptz, callee_at timestamptz)"
     " LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$"
     " SELECT n.task_id, n.client_id, n.call_at, n.callee_at FROM salescall.task_next_actions n"
     " WHERE n.tenant_id = salescall.cur_tenant() AND n.promised AND n.kind = 'call'"
     " AND NOT EXISTS (SELECT 1 FROM salescall.task_next_actions m WHERE m.task_id = n.task_id AND m.seq > n.seq) $$",
     ("tests.test_supervisor.Rounds.test_the_daily_reservation_is_shared_by_the_lines_and_skips_promises_that_will_not_be_called_now",)),
    ("約束の前の空けを、この回線でかけられる約束の数と、ほかに空いている回線の数だけで決める（専用と共有を突き合わせない）",
     "CREATE OR REPLACE FUNCTION salescall.line_kept_for_promise(p_line uuid, p_until timestamptz, p_except uuid)"
     " RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$"
     " WITH me AS (SELECT client_id FROM salescall.lines WHERE line_id = p_line),"
     " soon AS (SELECT w.client_id FROM salescall.promises_waiting() w, me WHERE w.task_id <> p_except"
     " AND w.callee_at > now() AND w.callee_at <= p_until AND (me.client_id IS NULL OR me.client_id = w.client_id)),"
     " free AS (SELECT i.line_id FROM salescall.lines_serving() i WHERE i.idle AND i.line_id <> p_line"
     " AND EXISTS (SELECT 1 FROM soon s WHERE i.client_id IS NULL OR i.client_id = s.client_id))"
     " SELECT (SELECT count(*) FROM soon) > (SELECT count(*) FROM free) $$",
     ("tests.test_supervisor.Rounds.test_a_dedicated_line_is_kept_when_the_shared_line_is_needed_for_another_client",)),
    ("空いている回線に、通話中の回線も数える",
     "CREATE OR REPLACE FUNCTION salescall.lines_serving() RETURNS TABLE (line_id uuid, client_id uuid, idle boolean)"
     " LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$"
     " SELECT o.line_id, o.client_id, true FROM salescall.lines o WHERE o.tenant_id = salescall.cur_tenant()"
     " AND o.retired_at IS NULL AND o.lease_expires_at > now() $$",
     ("tests.test_supervisor.Rounds.test_an_exact_time_promise_keeps_one_line_free_shortly_before",)),
    ("業務の平均通話時間に、途中の誤りで終わった通話も入れる",
     "CREATE OR REPLACE FUNCTION salescall.business_call_seconds(p_business uuid, p_days integer)"
     " RETURNS TABLE (seconds integer, calls integer) LANGUAGE sql STABLE SECURITY DEFINER"
     " SET search_path = salescall, pg_temp AS $$"
     " SELECT coalesce(round(avg(extract(epoch FROM a.ended_at - a.claimed_at)))::integer, 0), count(*)::integer"
     " FROM salescall.attempts a JOIN salescall.business_versions v ON v.version_id = a.version_id"
     " WHERE v.business_id = p_business AND a.tenant_id = salescall.cur_tenant() AND a.ended_at IS NOT NULL"
     " AND salescall.reached_answer(a.attempt_id) $$",
     ("tests.test_supervisor.Rounds.test_the_guard_is_the_average_call_length_of_the_business_once_measured",)),
    ("時刻ちょうどの約束の時間・日数に0を入れられる（約束を一度もかけずに終える）",
     "ALTER TABLE salescall.setting_values DROP CONSTRAINT setting_values_exact_promise_at_least_one",
     ("tests.test_settle.CallbackWindow.test_exact_promise_counts_cannot_be_zero",)),
    # 行のいまの予定・回線でかけてよい依頼者を、データベースの1か所で（0094）
    ("行のいまの予定に、いちばん古い記録を選ぶ",
     "CREATE OR REPLACE VIEW salescall.current_next_actions WITH (security_invoker = true) AS"
     " SELECT n.* FROM salescall.task_next_actions n"
     " WHERE n.seq = (SELECT min(m.seq) FROM salescall.task_next_actions m WHERE m.task_id = n.task_id)",
     ("tests.test_settle.Settle.test_callback_is_a_promised_schedule",)),
    ("回線の1日の上限で、約束を回線の数で割って回線ごとに切り上げる（合計が約束の数を超える）",
     "CREATE OR REPLACE FUNCTION salescall.line_promised_due(p_line uuid, p_day date, p_except uuid) RETURNS integer"
     " LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$"
     " SELECT coalesce(ceil(sum(1.0 / (SELECT count(*) FROM salescall.lines_serving() s"
     " WHERE salescall.line_serves(s.client_id, w.client_id)))), 0)::integer FROM salescall.promises_waiting() w"
     " WHERE w.task_id <> p_except AND salescall.jst(w.call_at)::date <= p_day $$",
     ("tests.test_supervisor.Rounds.test_the_daily_reservation_is_shared_by_the_lines_and_skips_promises_that_will_not_be_called_now",)),
    ("専用の回線で、ほかの依頼者の行もかける",
     "CREATE OR REPLACE FUNCTION salescall.line_serves(p_line_client uuid, p_client uuid) RETURNS boolean"
     " LANGUAGE sql IMMUTABLE AS $$ SELECT true $$",
     ("tests.test_gate.CallerNumberAndVoice.test_caller_number_changes_with_the_client",)),
    ("次の回の決め方に、知らない書き方を入れられる",
     "ALTER TABLE salescall.task_next_actions DROP CONSTRAINT task_next_actions_window_repeat",
     ("tests.test_settle.CallbackWindow.test_a_wrong_repeat_cannot_be_written",)),
    ("かけずに取り消した予約も、発信まで進んだ電話に数える（発信まで進んだかの1か所＝0097）",
     "CREATE OR REPLACE FUNCTION salescall.is_dialed(s salescall.attempt_state) RETURNS boolean LANGUAGE sql IMMUTABLE AS"
     " $$ SELECT s <> 'claimed' $$",
     ("tests.test_gate.DataReasons.test_max_attempts_counts_only_dialed",)),
    ("通話の判定し直しより前の判定を読む（通話ごとの最新の判定の1か所＝0097）",
     "CREATE OR REPLACE VIEW salescall.call_judgements WITH (security_invoker = true) AS"
     " SELECT r.* FROM salescall.task_results r WHERE r.kind IN ('judged', 'rejudged')"
     " AND r.version_no = (SELECT min(m.version_no) FROM salescall.task_results m"
     " WHERE m.attempt_id = r.attempt_id AND m.kind IN ('judged', 'rejudged'))",
     ("tests.test_measure.Verification.test_the_truth_goes_on_the_latest_judgement_of_the_call",)),
    ("約束の取り置きを、専用の回線より先に共有の回線へ割り当てる（0097）",
     "CREATE OR REPLACE FUNCTION salescall.line_promised_due(p_line uuid, p_day date, p_except uuid) RETURNS integer"
     " LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$"
     " WITH me AS (SELECT l.client_id FROM salescall.lines l WHERE l.line_id = p_line AND l.tenant_id = salescall.cur_tenant()),"
     " serving AS (SELECT s.line_id, s.client_id FROM salescall.lines_serving() s UNION SELECT p_line, me.client_id FROM me),"
     " due AS (SELECT w.task_id, w.client_id, dense_rank() OVER (ORDER BY w.task_id) - 1 AS r"
     "  FROM salescall.promises_waiting() w WHERE w.task_id <> p_except AND salescall.jst(w.call_at)::date <= p_day),"
     " cand AS (SELECT d.task_id, d.r, s.line_id, row_number() OVER (PARTITION BY d.task_id ORDER BY s.line_id) - 1 AS k,"
     "  count(*) OVER (PARTITION BY d.task_id) AS n FROM due d JOIN serving s ON salescall.line_serves(s.client_id, d.client_id))"
     " SELECT count(*)::integer FROM cand WHERE line_id = p_line AND k = r % n $$",
     ("tests.test_supervisor.Rounds.test_a_promise_is_reserved_on_its_clients_dedicated_line_first",)),
    ("意味の判定のエラーを記録できない（0098 の出どころ）",
     "ALTER TABLE salescall.error_events DROP CONSTRAINT error_events_source_check;"
     " ALTER TABLE salescall.error_events ADD CONSTRAINT error_events_source_check"
     " CHECK (source IN ('ai', 'ear', 'line', 'storage', 'delivery', 'recording', 'supervisor'))",
     ("tests.test_watch.TenantAiStopsAndChecks.test_a_meaning_judge_failure_never_stops_and_is_split_by_what_failed",)),
    ("その版で選んだ読みと合わない録音の組を承認する（0100）",
     "DROP TRIGGER recording_approval_fits ON salescall.recording_approvals",
     ("tests.test_gate.CallerNumberAndVoice.test_a_reading_chosen_by_listening_to_recordings_becomes_the_name_reading",)),
    ("その版で選んだ読みと合わない読みを、録音に書ける（0100）",
     "DROP TRIGGER recording_reading_before_insert ON salescall.recording_readings",
     ("tests.test_gate.CallerNumberAndVoice.test_a_reading_chosen_by_listening_to_recordings_becomes_the_name_reading",)),
    ("読みを選び直しても、前の読みの録音の組を流す（0100）",
     "CREATE OR REPLACE FUNCTION salescall.recording_set_fits(p_set uuid) RETURNS boolean LANGUAGE sql STABLE AS $$ SELECT true $$",
     ("tests.test_gate.CallerNumberAndVoice.test_a_reading_chosen_by_listening_to_recordings_becomes_the_name_reading",)),
    ("名前の読みを、システムの処理が選べる（0100）",
     "DROP TRIGGER version_name_reading_by_person ON salescall.version_name_readings",
     ("tests.test_gate.CallerNumberAndVoice.test_a_reading_chosen_by_listening_to_recordings_becomes_the_name_reading",)),
    ("一時的なエラーの続きを、記録した依頼者のエラーだけで数える（0100）",
     "CREATE OR REPLACE FUNCTION salescall.error_lasting(p_kind text, p_lasting interval) RETURNS boolean"
     " LANGUAGE sql STABLE SECURITY INVOKER SET search_path = salescall, pg_temp AS $$"
     " WITH e AS (SELECT occurred_at, lag(occurred_at) OVER (ORDER BY occurred_at) AS prev FROM salescall.error_events"
     " WHERE kind = p_kind AND tenant_id IS NOT DISTINCT FROM salescall.cur_tenant() AND occurred_at > now() - interval '7 days')"
     " SELECT coalesce(now() - max(occurred_at) FILTER (WHERE prev IS NULL OR occurred_at - prev > p_lasting) >= p_lasting, false)"
     " FROM e $$",
     ("tests.test_watch.TenantAiStopsAndChecks.test_a_lasting_error_is_counted_across_the_clients_of_the_contractor",)),
    ("行のいちばん古い結果を、いまの結果として読む（0097 で書き直した見方）",
     "CREATE OR REPLACE VIEW salescall.latest_task_results WITH (security_invoker = true) AS"
     " SELECT r.* FROM salescall.task_results r"
     " WHERE r.version_no = (SELECT min(m.version_no) FROM salescall.task_results m WHERE m.task_id = r.task_id)",
     ("tests.test_state.ResultVersions.test_open_until_first_result_then_latest",)),
)


def _fresh_db(superuser_dsn: str) -> tuple[str, str, str]:
    dbname = f"salescall_test_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(superuser_dsn, autocommit=True) as conn:
        conn.execute(ROLES_SQL.read_text(encoding="utf-8"))
        conn.execute(f'CREATE DATABASE "{dbname}" OWNER salescall_owner')
        conn.execute(f'CREATE DATABASE "{dbname}_restore" OWNER salescall_owner')   # 控えを戻して確かめる空のデータベース
    owner_dsn = _with_db(superuser_dsn, dbname, "salescall_owner")
    os.environ["SALESCALL_TEST_RESTORE_DSN"] = _with_db(superuser_dsn, f"{dbname}_restore", "salescall_owner")
    os.environ["SALESCALL_TEST_PROVISIONER_DSN"] = _with_db(superuser_dsn, dbname, "salescall_provisioner")
    migrate.apply(owner_dsn)
    return dbname, owner_dsn, _with_db(superuser_dsn, dbname, "salescall_app")


def _drop_db(superuser_dsn: str, dbname: str) -> None:
    with psycopg.connect(superuser_dsn, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')
        conn.execute(f'DROP DATABASE IF EXISTS "{dbname}_restore" WITH (FORCE)')


def _run_tests(owner_dsn: str, app_dsn: str, names: tuple[str, ...] | None, verbosity: int) -> bool:
    os.environ["SALESCALL_TEST_APP_DSN"] = app_dsn
    os.environ["SALESCALL_TEST_OWNER_DSN"] = owner_dsn
    loader = unittest.defaultTestLoader
    suite = (loader.loadTestsFromNames(names) if names
             else loader.discover(str(APP / "tests"), top_level_dir=str(APP)))
    stream = sys.stderr if verbosity else open(os.devnull, "w")
    return unittest.TextTestRunner(stream=stream, verbosity=verbosity).run(suite).wasSuccessful()


def _missing_tests() -> list[str]:
    """壊し試しが指す試験のうち、見つからない名前（試験の名前を変えた・消した）。⚠️ 見つからない名前は「読み込めない」という
    落ちた試験になり、守りを外しても落ちたと数えていた（2026-10-19 の見直し：11通りが何も確かめていなかった）。"""
    sys.path.insert(0, str(APP))
    missing = []
    for _, _, tests in MUTATIONS:
        for name in tests:
            suite = unittest.defaultTestLoader.loadTestsFromName(name)
            if any(type(t).__name__ == "_FailedTest" for t in suite):
                missing.append(name)
    return missing


def main() -> int:
    missing = _missing_tests()
    for name in missing:
        print("壊し試しが指す試験が見つからない：", name)
    if missing:
        return 1
    violations = run_all(APP)
    for v in violations:
        print("見張りが落ちた：", v)
    if violations:
        return 1
    print("決まりの機械の見張り：合格")

    given = os.environ.get("SALESCALL_TEST_SUPERUSER_DSN")
    cluster = _throwaway_cluster() if not given else _given(given)
    with cluster as superuser_dsn:
        dbname, owner_dsn, app_dsn = _fresh_db(superuser_dsn)
        try:
            if not _run_tests(owner_dsn, app_dsn, None, 1):
                return 1
        finally:
            _drop_db(superuser_dsn, dbname)

        # 守りをわざと外して、その試験が本当に落ちるか
        weak = []
        for title, sql, tests in MUTATIONS:
            dbname, owner_dsn, app_dsn = _fresh_db(superuser_dsn)
            try:
                with psycopg.connect(owner_dsn, autocommit=True) as conn:
                    conn.execute(sql)
                caught = not _run_tests(owner_dsn, app_dsn, tests, 0)
            finally:
                _drop_db(superuser_dsn, dbname)
            print(f"守りを外す：{title} ⇒ {'試験が落ちた（見張れている）' if caught else '⛔ 試験が通ってしまった'}")
            if not caught:
                weak.append(title)
        if weak:
            print("⛔ 守りを外しても落ちない試験がある：", "、".join(weak))
            return 1
        print("守りをわざと外す確かめ：すべて、試験が落ちた")
        return 0


@contextmanager
def _given(dsn: str) -> Iterator[str]:
    yield dsn


if __name__ == "__main__":
    raise SystemExit(main())
