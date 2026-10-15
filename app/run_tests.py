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
    ("送ってよい印の参照を外す", "ALTER TABLE salescall.deliveries DROP CONSTRAINT deliveries_client_id_task_id_version_no_fkey",
     ("tests.test_foundation.DeliveriesOnlyForReleasedVersions.test_release_then_deliver_once",)),
    ("人の緊急停止をシステムが解けない決まりを外す", "DROP TRIGGER stop_events_before_insert ON salescall.stop_events",
     ("tests.test_stops.ManualStops.test_system_cannot_resume_a_manual_stop",
      "tests.test_stops.ManualStops.test_actor_must_match_the_transaction")),
    ("設定の値の形の確かめを外す", "DROP TRIGGER setting_values_before_insert ON salescall.setting_values",
     ("tests.test_settings.Shapes.test_bad_values_are_rejected",
      "tests.test_settings.Shapes.test_system_only_cannot_be_set_lower")),
    ("設定の行の見え方を、同じ契約者の中で緩める",
     "ALTER POLICY setting_scope ON salescall.setting_values USING (scope = 'system' OR tenant_id = salescall.cur_tenant())",
     ("tests.test_settings.PerClientSettings.test_other_client_of_the_same_tenant_cannot_see_the_value",)),
    ("設定の枠は「システムだけ」の決まりを外す",
     "DROP TRIGGER setting_definitions_before_insert ON salescall.setting_definitions",
     ("tests.test_settings.FramesMustBeSystemOnly.test_a_frame_that_clients_can_move_is_rejected",)),
    ("停止を1回だけ解ける決まりを外す", "DROP INDEX salescall.stop_events_resolved_once",
     ("tests.test_stops.SystemStops.test_a_stop_is_resolved_only_once",)),
    ("1つの実施回で管理番号は1つ、の約束を外す", "DROP INDEX salescall.tasks_one_ref_per_run",
     ("tests.test_import.DatabaseGuards.test_one_task_per_client_ref_in_a_run",)),
    ("取込の行ごとの記録の変更の拒みを外す", "DROP TRIGGER import_rows_frozen ON salescall.import_rows",
     ("tests.test_import.DatabaseGuards.test_import_records_cannot_be_changed_even_by_the_owner",)),
    ("取込の記録の行の制限を外す", "ALTER TABLE salescall.import_rows DISABLE ROW LEVEL SECURITY",
     ("tests.test_import.DatabaseGuards.test_other_clients_cannot_see_import_records",)),
    ("システム共通の発信禁止で、ほかの依頼主の予約を取り消す処理を空にする",
     "CREATE OR REPLACE FUNCTION salescall.release_claims_for_system_dnc(p_phone text) RETURNS integer"
     " LANGUAGE sql SECURITY DEFINER SET search_path = salescall, pg_temp AS $$ SELECT 0 $$",
     ("tests.test_dnc.Register.test_system_ledger_applies_to_every_client_and_releases_their_claims",)),
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
    ("ほかの依頼主の発信の時刻を見ない",
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
    ("相手の意思と関係ない発信も、間隔・1日の回数に数える",
     "CREATE OR REPLACE FUNCTION salescall.not_by_callee(s salescall.attempt_state, disconnect text, p_attempt uuid)"
     " RETURNS boolean LANGUAGE sql STABLE AS $$ SELECT false $$",
     ("tests.test_gate.NotByCallee.test_failed_or_line_dropped_calls_do_not_count",)),
    ("相手が出る前の結果不明も、検証待ちにする",
     "CREATE OR REPLACE FUNCTION salescall.reached_answer(p_attempt uuid) RETURNS boolean LANGUAGE sql STABLE AS $$ SELECT true $$",
     ("tests.test_gate.Pacing.test_unknown_result_before_answer_needs_no_long_wait",)),
    ("1つの試行に判定の版は1つ、の約束を外す", "DROP INDEX salescall.task_results_one_judgement_per_attempt",
     ("tests.test_settle.DatabaseGuards.test_one_judgement_per_attempt",)),
    ("「わからない」「答えない」の理由は、その状態だけ、の決まりを外す",
     "ALTER TABLE salescall.item_values DROP CONSTRAINT item_values_reason_only_when_not_answered",
     ("tests.test_settle.DatabaseGuards.test_item_reason_only_when_not_answered",)),
    ("番号違いで管理番号を止めない決まりを外す",
     "ALTER TABLE salescall.dnc_entries DROP CONSTRAINT dnc_entries_ref_not_wrong_party",
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
     ("tests.test_watch.SystemStopsAndChecks.test_manual_stop_is_not_resumed_by_checks",
      "tests.test_watch.SystemStopsAndChecks.test_only_admin_resumes_system_stops_by_hand")),
    ("システム共通の発信禁止を管理者だけにする仕掛けを外す", "DROP TRIGGER system_dnc_by_admin ON salescall.system_dnc_entries",
     ("tests.test_dnc.Register.test_system_ledger_applies_to_every_client_and_releases_their_claims",)),
    # 承認の部分（10-2）
    ("行を承認できるのは運営者だけ、の仕掛けを外す", "DROP TRIGGER task_approval_by_operator ON salescall.task_approvals",
     ("tests.test_approval.HumanApproval.test_dials_only_approved_rows",)),
    # 利用量の台帳と予算（Q16）
    ("利用量の台帳の変更の拒みを外す", "DROP TRIGGER usage_entries_frozen ON salescall.usage_entries",
     ("tests.test_usage.Ledger.test_frozen",)),
    ("報酬の決まりの変更の拒みを外す", "DROP TRIGGER reward_terms_frozen ON salescall.reward_terms",
     ("tests.test_usage.CostRatio.test_terms_are_frozen",)),
    ("システムが止めた停止を、見守りに見せない",
     "CREATE OR REPLACE FUNCTION salescall.open_system_stops(p_cause text) RETURNS TABLE (event_id bigint, scope text,"
     " tenant_id uuid, client_id uuid, stopped_on date) LANGUAGE sql STABLE SECURITY DEFINER"
     " SET search_path = salescall, pg_temp AS $$ SELECT NULL::bigint, NULL::text, NULL::uuid, NULL::uuid, NULL::date"
     " WHERE false $$",
     ("tests.test_usage.CostRatio.test_deficit_stops_and_resumes_when_terms_are_fixed",
      "tests.test_usage.Budget.test_system_limit_and_new_day")),
    ("全体の今日の金額を数えない（いつも0）",
     "CREATE OR REPLACE FUNCTION salescall.spent_today_all() RETURNS numeric"
     " LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$ SELECT 0::numeric $$",
     ("tests.test_usage.Budget.test_system_limit_and_new_day",)),
    # 品質の段（Q22）
    ("言ってはいけないことを言ったかを、いつも「言っていない」にする",
     "CREATE OR REPLACE FUNCTION salescall.said_forbidden(p_attempt uuid) RETURNS boolean"
     " LANGUAGE sql STABLE SET search_path = salescall, pg_temp AS $$ SELECT false $$",
     ("tests.test_approval.ForbiddenSpeech.test_the_row_ends_and_the_admin_is_told",)),
    ("検証を済ませる前に結果の数を残す、の仕掛けを外す",
     "DROP TRIGGER attempt_verification_needs_summary ON salescall.attempts",
     ("tests.test_quality.Stages.test_done_needs_the_numbers",)),
    # 見守り（Q9・9-1）
    ("何回続いたかを数えない（いつも0）",
     "CREATE OR REPLACE FUNCTION salescall.consecutive_errors(p_kinds text[], p_cause text, p_line uuid DEFAULT NULL)"
     " RETURNS integer LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$ SELECT 0 $$",
     ("tests.test_watch.LineStops.test_consecutive_line_errors_stop_only_that_line",)),
    ("呼び出せた発信で数え直さない",
     "CREATE OR REPLACE FUNCTION salescall.consecutive_errors(p_kinds text[], p_cause text, p_line uuid DEFAULT NULL)"
     " RETURNS integer LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$"
     " SELECT count(*)::integer FROM salescall.error_events e WHERE e.kind = ANY(p_kinds)"
     " AND (p_line IS NULL OR e.line_id = p_line) $$",
     ("tests.test_watch.LineStops.test_a_call_that_rang_resets_the_count",
      "tests.test_watch.LineStops.test_a_person_resume_restarts_the_count_and_explain_shows_why")),
    ("止めた理由の説明に、数えたエラーを出さない",
     "CREATE OR REPLACE FUNCTION salescall.errors_for_stop(p_stop bigint, p_kinds text[], p_limit integer)"
     " RETURNS TABLE (error_id bigint, source text, kind text, code integer, message text, occurred_at timestamptz)"
     " LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$"
     " SELECT NULL::bigint, NULL::text, NULL::text, NULL::integer, NULL::text, NULL::timestamptz WHERE false $$",
     ("tests.test_watch.SystemStopsAndChecks.test_only_admin_resumes_system_stops_by_hand",)),
    ("停止を解く確かめの記録の変更の拒みを外す", "DROP TRIGGER stop_checks_frozen ON salescall.stop_checks",
     ("tests.test_watch.SystemStopsAndChecks.test_checks_are_frozen",)),
    # 知らせの台帳（Q23）
    ("同じ出来事は1回だけ、の約束を外す",
     "ALTER TABLE salescall.notification_events DROP CONSTRAINT notification_events_event_key_key",
     ("tests.test_notify.Ledger.test_same_event_only_once_and_unknown_kind_is_refused",)),
    ("知らせの台帳の見える範囲を外す", "ALTER TABLE salescall.notification_events DISABLE ROW LEVEL SECURITY",
     ("tests.test_notify.Ledger.test_who_can_see",)),
    ("知らせの台帳の変更の拒みを外す", "DROP TRIGGER notification_events_frozen ON salescall.notification_events",
     ("tests.test_notify.Ledger.test_frozen",)),
    ("相手が指定した時刻は約束したかけ直しだけ、の決まりを外す",
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
)


def _fresh_db(superuser_dsn: str) -> tuple[str, str, str]:
    dbname = f"salescall_test_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(superuser_dsn, autocommit=True) as conn:
        conn.execute(ROLES_SQL.read_text(encoding="utf-8"))
        conn.execute(f'CREATE DATABASE "{dbname}" OWNER salescall_owner')
    owner_dsn = _with_db(superuser_dsn, dbname, "salescall_owner")
    migrate.apply(owner_dsn)
    return dbname, owner_dsn, _with_db(superuser_dsn, dbname, "salescall_app")


def _drop_db(superuser_dsn: str, dbname: str) -> None:
    with psycopg.connect(superuser_dsn, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')


def _run_tests(owner_dsn: str, app_dsn: str, names: tuple[str, ...] | None, verbosity: int) -> bool:
    os.environ["SALESCALL_TEST_APP_DSN"] = app_dsn
    os.environ["SALESCALL_TEST_OWNER_DSN"] = owner_dsn
    loader = unittest.defaultTestLoader
    suite = (loader.loadTestsFromNames(names) if names
             else loader.discover(str(APP / "tests"), top_level_dir=str(APP)))
    stream = sys.stderr if verbosity else open(os.devnull, "w")
    return unittest.TextTestRunner(stream=stream, verbosity=verbosity).run(suite).wasSuccessful()


def main() -> int:
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
