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
import re
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


def _edited(migration: str, start: str, old: str, new: str, *, end: str = "END $$") -> str:
    """壊し試し用に、表の作りの1つの関数（start から end＝`END $$`・言語 sql の関数は改行のあとの `$$` まで）の old を new に
    替えた形を返す（いまの形からずらす。関数を写して持たない＝関数を直しても、壊し試しが古い形に戻さない）。"""
    text = (APP / "db" / "migrations" / migration).read_text()
    begin = text.index(start)
    body = text[begin:text.index(end, begin) + len(end)]
    if body.count(old) != 1:
        raise AssertionError(f"壊し試しの置き換え元が {migration} の関数に1つだけありません：{old!r}")
    edited = body.replace(old, new).replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1)
    # 作り直すと、あとの表の作りで足した「持ち主の権限で動く」形が外れる（狙った守りでなく権限の誤りで試験が落ち、見張れていない
    # のに「見張れている」と出る）。あとで足していれば、作り直したあとも同じ形に戻す
    signature = re.match(r"CREATE FUNCTION (salescall\.\w+\([^)]*\))", body)
    if signature:
        definer = f"ALTER FUNCTION {signature.group(1)} SECURITY DEFINER SET search_path = salescall, pg_temp"
        if any(definer in f.read_text() for f in sorted((APP / "db" / "migrations").glob("*.sql"))):
            edited += "; " + definer
    return edited


# データベースの守りを、わざと1つずつ外す。外したら、その守りを試す試験が本当に落ちることを確かめる（DESIGN.md 14章：
# 落ちることを確かめていない見張りは、見張っていないのと同じ）。守りを足したら、ここにも足す。
MUTATIONS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("お詫び・続き・番号の聞き直しの予定に約束の印を求める守りを外す（0126 の前の形：付け忘れると、お詫びの電話が後回しになる）",
     "ALTER TABLE salescall.task_next_actions DROP CONSTRAINT task_next_actions_promise_mark",
     ("tests.test_settle.DatabaseGuards.test_fixed_purposes_always_carry_the_promise_mark",)),
    ("約束の印を必ず持つ目的から、番号の聞き直しを外す（0126 の1か所の決まりを狭める）",
     "CREATE OR REPLACE FUNCTION salescall.needs_promise_mark(p_purpose text) RETURNS boolean LANGUAGE sql IMMUTABLE AS"
     " $$ SELECT salescall.keeping_kind(p_purpose, false) IS NOT NULL $$",
     ("tests.test_settle.DatabaseGuards.test_fixed_purposes_always_carry_the_promise_mark",)),
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
     ("tests.test_gate.UncountedDials.test_a_promised_callback_is_not_counted_afterwards",)),
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
    ("ほかの依頼者の発信からの間隔を見ない",
     "CREATE OR REPLACE FUNCTION salescall.number_dial_wait_elsewhere(p_phone text, p_same_gap interval, p_other_gap interval)"
     " RETURNS TABLE (relation text, wait_until timestamptz)"
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
     "CREATE OR REPLACE FUNCTION salescall.dial_kind(s salescall.attempt_state, p_cut_off boolean, p_attempt uuid) RETURNS text"
     " LANGUAGE sql STABLE AS $$ SELECT CASE WHEN salescall.is_dialed(s) THEN 'reached' END $$",
     ("tests.test_gate.UncountedDials.test_failed_or_interrupted_calls_do_not_count",
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
    ("見守りに、期限の来た赤字の警告を見せない",
     "CREATE OR REPLACE FUNCTION salescall.open_deficit_warnings() RETURNS TABLE (tenant_id uuid, client_id uuid)"
     " LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$ SELECT NULL::uuid, NULL::uuid"
     " WHERE false $$",
     ("tests.test_usage.CostRatio.test_deficit_warns_and_reminds_once_but_never_stops",)),
    ("赤字の警告の確認を、システムの処理にもさせる", "DROP TRIGGER deficit_warning_guard ON salescall.deficit_warnings",
     ("tests.test_usage.CostRatio.test_a_confirmed_deficit_is_not_reminded",)),
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
     "CREATE OR REPLACE FUNCTION salescall.recordings_held() RETURNS SETOF uuid LANGUAGE sql STABLE"
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
     "CREATE OR REPLACE FUNCTION salescall.recover_lines(p_holder text, p_line uuid DEFAULT NULL, p_except text DEFAULT NULL)"
     " RETURNS TABLE (line_id uuid, released integer, ended integer) LANGUAGE plpgsql SECURITY DEFINER"
     " SET search_path = salescall, pg_temp AS $$ DECLARE l record; r integer; e integer; BEGIN"
     " FOR l IN SELECT x.line_id FROM salescall.lines x WHERE x.tenant_id = salescall.cur_tenant() LOOP"
     " UPDATE salescall.attempts SET state = 'released' WHERE attempts.line_id = l.line_id AND state = 'claimed';"
     " GET DIAGNOSTICS r = ROW_COUNT; line_id := l.line_id; released := r; ended := 0; RETURN NEXT; END LOOP; END $$",
     ("tests.test_supervisor.Recovery.test_only_expired_lines_of_a_former_hub_are_cleaned_up",)),
    # 落ちた試行の後始末（5-4・0076。2026-10-18 の見直し）
    ("決まった間隔の後始末が、自分の回線のまだ話している通話を終わらせる",
     "CREATE OR REPLACE FUNCTION salescall.recover_lines(p_holder text, p_line uuid DEFAULT NULL, p_except text DEFAULT NULL)"
     " RETURNS TABLE (line_id uuid, released integer, ended integer) LANGUAGE plpgsql SECURITY DEFINER"
     " SET search_path = salescall, pg_temp AS $$ DECLARE l record; r integer; BEGIN"
     " FOR l IN SELECT x.line_id FROM salescall.lines x WHERE x.tenant_id = salescall.cur_tenant()"
     " AND x.lease_holder IS NOT NULL AND (CASE WHEN p_holder IS NULL THEN x.lease_expires_at < now()"
     " ELSE x.lease_holder = p_holder END) LOOP"
     " UPDATE salescall.attempts SET state = 'released' WHERE attempts.line_id = l.line_id AND state = 'claimed';"
     " GET DIAGNOSTICS r = ROW_COUNT; line_id := l.line_id; released := r; ended := 0; RETURN NEXT; END LOOP; END $$",
     ("tests.test_supervisor.Recovery.test_the_periodic_cleanup_leaves_its_own_lines_and_cleans_those_of_a_former_hub",)),
    # 契約者をまたげた所（11章・0077。PC の鍵の仕組みは 0107 で外した。守りは残す）
    ("契約者の取引から、祝日の暦を書ける",
     "DROP TRIGGER calendar_loads_by_system ON salescall.calendar_loads",
     ("tests.test_foundation.TenantTransactions.test_other_contractors_and_shared_things_are_out_of_reach",)),
    ("時給の数え方で、ほかの契約者の依頼者の通話を数える",
     "CREATE OR REPLACE FUNCTION salescall.work_seconds(p_from timestamptz, p_to timestamptz)"
     " RETURNS TABLE (at timestamptz, seconds numeric) LANGUAGE sql STABLE SECURITY DEFINER"
     " SET search_path = salescall, pg_temp AS $$ SELECT a.claimed_at, extract(epoch FROM a.ended_at - a.claimed_at)"
     " FROM salescall.attempts a WHERE a.client_id = salescall.cur_client() AND a.ended_at IS NOT NULL $$",
     ("tests.test_foundation.TenantTransactions.test_other_contractors_and_shared_things_are_out_of_reach",)),
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
    # お客様との約束を果たす電話の種類（0115。赤字では止めない＝0118 のあとは、新しい電話かどうかの決まりに使う）
    ("途中で切れたお客様へのお詫びのかけ直しを、約束を果たす電話とみなさない（新しい電話として「強制かけない」の行で止まる）",
     "CREATE OR REPLACE FUNCTION salescall.keeping_kind(p_purpose text, p_promised boolean) RETURNS text"
     " LANGUAGE sql IMMUTABLE AS $$ SELECT CASE WHEN p_purpose = 'apology' THEN 'apology'"
     " WHEN coalesce(p_promised, false) THEN 'promised' END $$",
     ("tests.test_exclusions.Marks.test_apologies_resumes_and_promised_calls_are_made_even_on_a_row_set_to_do_not_call",)),
    ("「強制かけない」の行でも、お客様と約束したかけ直しをかける",
     "CREATE OR REPLACE FUNCTION salescall.task_exclusion_level(p_task uuid) RETURNS text LANGUAGE sql STABLE AS"
     " $$ SELECT CASE WHEN salescall.task_excluded(p_task) THEN 'exclude' ELSE 'call' END $$;"
     " CREATE OR REPLACE FUNCTION salescall.task_excluded(p_task uuid) RETURNS boolean LANGUAGE sql STABLE AS"
     " $$ SELECT coalesce((SELECT e.excluded FROM salescall.task_exclusions e WHERE e.task_id = p_task"
     " ORDER BY e.seq DESC LIMIT 1), false) $$",
     ("tests.test_exclusions.Marks.test_force_stops_every_call_of_the_row",)),
    # 契約者をまたげた所の見直し（11章・0084。2026-10-18）
    ("契約者の取引から、ほかの契約者の管理者あての知らせが読める",
     "DROP POLICY notice_scope ON salescall.notification_events; CREATE POLICY notice_scope ON salescall.notification_events"
     " USING ((tenant_id IS NULL AND (NOT for_admin OR salescall.cur_actor_kind() IN ('admin', 'system')))"
     " OR (tenant_id = salescall.cur_tenant() AND (client_id IS NULL OR salescall.cur_client() IS NULL"
     " OR client_id = salescall.cur_client())))"
     " WITH CHECK ((tenant_id IS NULL AND salescall.cur_tenant() IS NULL AND NOT for_admin)"
     " OR (tenant_id = salescall.cur_tenant() AND client_id IS NOT DISTINCT FROM salescall.cur_client()))",
     ("tests.test_foundation.TenantTransactions.test_other_contractors_and_shared_things_are_out_of_reach",)),
    ("控えのフォルダの名前から、ほかの契約者の依頼者が分かる",
     "CREATE OR REPLACE FUNCTION salescall.backup_folder_taken(p_name text) RETURNS boolean LANGUAGE plpgsql STABLE"
     " SECURITY DEFINER SET search_path = salescall, pg_temp AS $$ BEGIN PERFORM salescall.only_backup_system();"
     " RETURN EXISTS (SELECT 1 FROM salescall.client_backup_folders WHERE lower(folder_name) = lower(p_name)); END $$",
     ("tests.test_backup.PerClient.test_folder_names_do_not_show_other_contractors_clients",)),
    ("システムの処理でなくても、仕事の借用を返せる",
     "CREATE OR REPLACE FUNCTION salescall.return_job(p_key text, p_holder text) RETURNS boolean LANGUAGE sql"
     " SECURITY DEFINER SET search_path = salescall, pg_temp AS $$"
     " UPDATE salescall.job_leases SET expires_at = now() WHERE job_key = p_key AND holder = p_holder RETURNING true $$",
     ("tests.test_supervisor.Leases.test_jobs_are_taken_by_one_supervisor_and_only_by_the_system",)),
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
     ("tests.test_backup.PerClient.test_the_whole_database_is_backed_up_only_to_the_admin_pc",)),
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
    ("鳴って終わった発信で数え直さない",
     "CREATE OR REPLACE FUNCTION salescall.consecutive_errors(p_kinds text[], p_cause text, p_evidence text,"
     " p_line uuid DEFAULT NULL) RETURNS integer LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$"
     " SELECT count(*)::integer FROM salescall.error_events e WHERE e.kind = ANY(p_kinds)"
     " AND (p_line IS NULL OR e.line_id = p_line) $$",
     ("tests.test_watch.LineStops.test_a_call_that_rang_and_ended_resets_the_count",
      "tests.test_watch.LineStops.test_a_person_resume_restarts_the_count_and_explain_shows_why")),
    ("続いたエラーを、直った証拠（鳴って終わった発信など）で数え直さない",
     "CREATE OR REPLACE FUNCTION salescall.consecutive_errors(p_kinds text[], p_cause text, p_evidence text,"
     " p_line uuid DEFAULT NULL) RETURNS integer LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$"
     " SELECT count(*)::integer FROM salescall.error_events e WHERE e.kind = ANY(p_kinds)"
     " AND e.tenant_id = salescall.cur_tenant() AND (p_line IS NULL OR e.line_id = p_line) $$",
     ("tests.test_watch.LineStops.test_a_call_that_rang_and_ended_resets_the_count",)),
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
     ("tests.test_emergency.OnTheServer.test_a_release_on_the_screen_is_seen_and_a_release_from_the_pc_frees_only_its_own_stops",
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
     " LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$"
     " SELECT count(*)::integer FROM salescall.promises_waiting() w"
     " WHERE w.task_id <> p_except AND salescall.jst(w.call_at)::date <= p_day $$",
     ("tests.test_supervisor.Rounds.test_the_daily_reservation_is_shared_by_the_lines_and_skips_promises_that_will_not_be_called_now",)),
    ("これからかける約束に、止まっている依頼者の約束・通話中の約束も入れる",
     "CREATE OR REPLACE FUNCTION salescall.promises_waiting()"
     " RETURNS TABLE (task_id uuid, client_id uuid, call_at timestamptz, callee_at timestamptz, phone_e164 text)"
     " LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$"
     " SELECT n.task_id, n.client_id, n.call_at, n.callee_at, k.phone_e164 FROM salescall.task_next_actions n"
     " JOIN salescall.task_numbers k ON k.task_id = n.task_id AND k.contact_id = n.contact_id"
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
    # 名前のいまの読みに、版の読みも入れる（0101。見直し7）
    ("版の中身を置いても、名前の決まった読みを読みの表に入れない",
     "DROP TRIGGER business_content_readings ON salescall.business_contents",
     ("tests.test_gate.CallerNumberAndVoice.test_a_reading_chosen_by_listening_to_recordings_becomes_the_name_reading",)),
    ("名前のいまの読みを、録音で選んだ読みだけにする（0100 の形）",
     "CREATE OR REPLACE VIEW salescall.current_name_readings WITH (security_invoker = true) AS"
     " SELECT n.version_id, n.name_text, n.reading, n.reading_by FROM salescall.version_name_readings n"
     " WHERE n.reading_by = 'recording' AND n.seq = (SELECT max(m.seq) FROM salescall.version_name_readings m"
     " WHERE m.version_id = n.version_id AND m.name_text = n.name_text)",
     ("tests.test_gate.CallerNumberAndVoice.test_a_reading_chosen_by_listening_to_recordings_becomes_the_name_reading",)),
    ("入口の読みのある名前も、録音で選べる",
     "CREATE OR REPLACE FUNCTION salescall.version_name_reading_by_person() RETURNS trigger LANGUAGE plpgsql AS $$"
     " BEGIN IF NEW.reading_by <> 'recording' THEN RETURN NEW; END IF;"
     " IF coalesce(salescall.cur_actor_kind()::text, '') NOT IN ('operator', 'admin') THEN"
     " RAISE EXCEPTION 'x' USING ERRCODE = 'insufficient_privilege', CONSTRAINT = 'version_name_reading_by_person'; END IF;"
     " RETURN NEW; END $$",
     ("tests.test_gate.CallerNumberAndVoice.test_a_reading_chosen_by_listening_to_recordings_becomes_the_name_reading",)),
    ("続いたエラーを数える関数を、誰でも呼べる（0100 の穴）",
     "GRANT EXECUTE ON FUNCTION salescall.error_lasting(text, interval) TO PUBLIC",
     ("tests.test_foundation.DefinerFunctions.test_no_definer_function_is_open_to_everyone",)),
    ("アプリの役が、ほかの依頼者の約束の一覧を読める",
     "GRANT EXECUTE ON FUNCTION salescall.promises_waiting() TO salescall_app",
     ("tests.test_foundation.DefinerFunctions.test_lists_of_other_clients_promises_are_used_only_inside",)),
    # 試験の強さの見直し（見直し7：壊しても落ちなかった所に試験を足した）
    ("時刻を指定していない約束のためにも、約束の前に回線を空ける",
     "CREATE OR REPLACE FUNCTION salescall.promises_waiting()"
     " RETURNS TABLE (task_id uuid, client_id uuid, call_at timestamptz, callee_at timestamptz, phone_e164 text)"
     " LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$"
     " SELECT n.task_id, n.client_id, n.call_at, coalesce(n.callee_at, n.call_at), k.phone_e164"
     " FROM salescall.task_next_actions n"
     " JOIN salescall.tasks t ON t.task_id = n.task_id"
     " JOIN salescall.runs r ON r.run_id = t.run_id"
     " JOIN salescall.task_numbers k ON k.task_id = n.task_id AND k.contact_id = n.contact_id"
     " WHERE n.tenant_id = salescall.cur_tenant() AND n.promised AND n.kind = 'call'"
     " AND n.seq = (SELECT c.seq FROM salescall.current_next_actions c WHERE c.task_id = n.task_id)"
     " AND salescall.client_active(n.client_id) AND NOT salescall.task_excluded_now(n.task_id)"
     " AND NOT EXISTS (SELECT 1 FROM salescall.blocking_stops(n.tenant_id, n.client_id, r.business_id, NULL,"
     " salescall.keeping_kind(n.purpose, n.promised)))"
     " AND NOT EXISTS (SELECT 1 FROM salescall.attempts a"
     " WHERE a.task_id = n.task_id AND salescall.is_live(a.state, a.claim_expires_at))"
     " $$;",
     ("tests.test_supervisor.Rounds.test_an_exact_time_promise_keeps_one_line_free_shortly_before",)),
    ("回線の1日の上限で、前の日から残っている約束を取り置かない",
     "CREATE OR REPLACE FUNCTION salescall.line_promised_due(p_line uuid, p_day date, p_except uuid) RETURNS integer"
     " LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$"
     " WITH me AS (SELECT l.client_id FROM salescall.lines l WHERE l.line_id = p_line AND l.tenant_id = salescall.cur_tenant()),"
     " serving AS (SELECT s.line_id, s.client_id FROM salescall.lines_serving() s"
     " UNION SELECT p_line, me.client_id FROM me),"
     " due AS (SELECT w.task_id, w.client_id, dense_rank() OVER (ORDER BY w.task_id) - 1 AS r"
     " FROM salescall.promises_waiting() w"
     " WHERE w.task_id <> p_except AND salescall.jst(w.call_at)::date = p_day),"
     " cand AS (SELECT d.task_id, d.r, s.line_id, s.client_id IS NOT NULL AS own,"
     " count(*) FILTER (WHERE s.client_id IS NOT NULL) OVER (PARTITION BY d.task_id) AS owns"
     " FROM due d JOIN serving s ON salescall.line_serves(s.client_id, d.client_id)),"
     " pool AS (SELECT c.task_id, c.r, c.line_id,"
     " row_number() OVER (PARTITION BY c.task_id ORDER BY c.line_id) - 1 AS k,"
     " count(*) OVER (PARTITION BY c.task_id) AS n"
     " FROM cand c WHERE c.own OR c.owns = 0)"
     " SELECT count(*)::integer FROM pool WHERE line_id = p_line AND k = r % n"
     " $$;",
     ("tests.test_supervisor.Rounds.test_the_daily_reservation_is_shared_by_the_lines_and_skips_promises_that_will_not_be_called_now",)),
    ("続いたエラーで、1時間より長く空いても続きとして数える",
     "CREATE OR REPLACE FUNCTION salescall.error_lasting(p_kind text, p_lasting interval) RETURNS boolean"
     " LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$"
     " WITH e AS (SELECT occurred_at, lag(occurred_at) OVER (ORDER BY occurred_at) AS prev FROM salescall.error_events"
     " WHERE kind = p_kind AND tenant_id IS NOT DISTINCT FROM salescall.cur_tenant()"
     " AND occurred_at > now() - interval '7 days')"
     " SELECT coalesce(now() - max(occurred_at) FILTER (WHERE prev IS NULL) >= p_lasting, false) FROM e"
     " $$;",
     ("tests.test_watch.TenantAiStopsAndChecks.test_a_lasting_temporary_error_becomes_a_fix_request",)),
    ("門が、お客様の指定した約束の時刻を見ない",
     "CREATE OR REPLACE FUNCTION salescall.next_customer_promise_at(p_except uuid) RETURNS timestamptz"
     " LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$ SELECT NULL::timestamptz $$",
     ("tests.test_supervisor.Rounds.test_an_exact_time_promise_keeps_one_line_free_shortly_before",)),
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
    ("意味の判定の単価に、枠を求めない（0102 の前の守り）",
     "ALTER TABLE salescall.price_versions DROP CONSTRAINT price_versions_tier;"
     " ALTER TABLE salescall.price_versions ADD CONSTRAINT price_versions_tier CHECK ((source IN ('ai', 'ear')) = (tier IS NOT NULL)"
     " OR source = 'judge')",
     ("tests.test_usage.PricesPerContractor.test_the_judge_ai_has_its_own_source_and_tier",)),
    ("全体の取引から、最初の契約者を作った時刻を読めない（0102 の前：契約者の表は見えない）",
     "CREATE OR REPLACE FUNCTION salescall.first_tenant_at() RETURNS timestamptz LANGUAGE sql STABLE SECURITY INVOKER"
     " SET search_path = salescall, pg_temp AS $$ SELECT min(created_at) FROM salescall.tenants $$",
     ("tests.test_backup.PerClient.test_backups_not_taken_are_noticed_even_without_any_backup_settings",)),
    ("契約者の取引にも、最初の契約者を作った時刻を返す（0103 の前：ほかの契約者がいつ始めたかが分かる）",
     "CREATE OR REPLACE FUNCTION salescall.first_tenant_at() RETURNS timestamptz LANGUAGE sql STABLE SECURITY DEFINER"
     " SET search_path = salescall, pg_temp AS $$ SELECT min(created_at) FROM salescall.tenants $$",
     ("tests.test_backup.PerClient.test_the_first_contractor_time_is_given_only_to_the_whole_database_watch",)),
    ("録音の部品の名前に「-」を受け付けない（0103 の前：「-」のある番号の質問の録音を入れられない）",
     "ALTER TABLE salescall.recordings DROP CONSTRAINT recordings_part_check;"
     " ALTER TABLE salescall.recordings ADD CONSTRAINT recordings_part_check CHECK (part ~ '^[A-Za-z0-9_]{1,64}$')",
     ("tests.test_gate.CallerNumberAndVoice.test_recording_part_names_take_the_same_letters_as_question_ids",)),
    ("録音の部品の名前に、どの文字でも受け付ける",
     "ALTER TABLE salescall.recordings DROP CONSTRAINT recordings_part_check;"
     " ALTER TABLE salescall.recordings ADD CONSTRAINT recordings_part_check CHECK (part ~ '^.{0,64}$')",
     ("tests.test_gate.CallerNumberAndVoice.test_recording_part_names_take_the_same_letters_as_question_ids",
      "tests.test_gate.CallerNumberAndVoice.test_an_answer_recording_keeps_the_japanese_question_name")),
    ("想定問答の答えの録音の部分の名前を、英数字だけにする（0112 の前：日本語の問いの名前の答えの録音を入れられない）",
     "ALTER TABLE salescall.recordings DROP CONSTRAINT recordings_part_check;"
     " ALTER TABLE salescall.recordings ADD CONSTRAINT recordings_part_check CHECK (part ~ '^[A-Za-z0-9_-]{1,64}$')",
     ("tests.test_gate.CallerNumberAndVoice.test_an_answer_recording_keeps_the_japanese_question_name",)),
    ("回線を、一覧に無い方式でも作れる（0104 の前）",
     "ALTER TABLE salescall.lines DROP CONSTRAINT lines_method",
     ("tests.test_registry.Lines.test_the_line_method_is_chosen_from_the_list",)),
    ("回線の方式の一覧を、契約者も足せる・変えられる",
     "DROP TRIGGER line_methods_guard ON salescall.line_methods",
     ("tests.test_registry.Lines.test_the_line_method_is_chosen_from_the_list",)),
    ("回線の単価を、一覧に無い方式にも入れられる（0104 の前：書き方が違うと単価が見つからない）",
     "DROP TRIGGER price_versions_line_method ON salescall.price_versions",
     ("tests.test_usage.PricesPerContractor.test_a_line_price_is_only_for_a_method_in_the_list",)),
    ("PC の通行証を、誰でも払い出せる・書き換えられる（0105 の守り）",
     "DROP TRIGGER pc_agents_guard ON salescall.pc_agents",
     ("tests.test_agents.Passes.test_who_may_issue_stop_and_change",)),
    ("止めた通行証でもサーバーにつなげる（0105・0106）",
     "CREATE OR REPLACE FUNCTION salescall.agent_by_token(p_sha text) RETURNS TABLE (pc_id uuid, tenant_id uuid, label text)"
     " LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$ BEGIN PERFORM salescall.hub_only();"
     " RETURN QUERY SELECT a.pc_id, a.tenant_id, a.label FROM salescall.pc_agents a WHERE a.token_sha256 = p_sha; END $$",
     ("tests.test_agents.Passes.test_a_pass_is_given_once_kept_only_as_a_fingerprint_and_identifies_the_pc",)),
    ("サーバーだけの関数を、契約者の取引・管理者からも呼べる（0105）",
     "CREATE OR REPLACE FUNCTION salescall.hub_only() RETURNS void LANGUAGE plpgsql STABLE AS $$ BEGIN END $$",
     ("tests.test_agents.Passes.test_who_may_issue_stop_and_change",)),
    ("データベースが、サーバーの役でないつなぎも受け付ける（0107：前の PC の鍵の役が残ると、何でも読める）",
     "CREATE OR REPLACE FUNCTION salescall.server_session() RETURNS boolean LANGUAGE sql STABLE AS $$ SELECT true $$",
     ("tests.test_foundation.ServerOnly.test_a_leftover_login_role_can_do_nothing",)),
    ("共通の表の守りの決まりを外す（0107）",
     "ALTER TABLE salescall.setting_definitions DISABLE ROW LEVEL SECURITY",
     ("tests.test_foundation.ServerOnly.test_every_table_and_every_definer_function_is_guarded",
      "tests.test_foundation.ServerOnly.test_a_leftover_login_role_can_do_nothing")),
    ("測り終わるまで消さない録音の一覧に、ほかの契約者の通話が混ざる（0106）",
     "CREATE OR REPLACE FUNCTION salescall.recordings_held() RETURNS SETOF uuid LANGUAGE plpgsql STABLE SECURITY DEFINER"
     " SET search_path = salescall, pg_temp AS $$ BEGIN PERFORM salescall.only_backup_system();"
     " RETURN QUERY SELECT DISTINCT h.attempt_id FROM salescall.recording_holds h WHERE h.released_at IS NULL ORDER BY 1; END $$",
     ("tests.test_settle.RecordingHolds.test_verification_calls_keep_their_recordings_until_measured",)),
    ("合図で、ほかの契約者の回線も名乗れる（0105）",
     "CREATE OR REPLACE FUNCTION salescall.agent_beat(p_pc uuid, p_lines uuid[]) RETURNS uuid[]"
     " LANGUAGE plpgsql SECURITY DEFINER SET search_path = salescall, pg_temp AS $$ DECLARE kept uuid[]; BEGIN"
     " PERFORM salescall.hub_only(); UPDATE salescall.pc_agents a SET last_seen_at = now(), lines = p_lines"
     " WHERE a.pc_id = p_pc AND a.revoked_at IS NULL RETURNING a.lines INTO kept; RETURN kept; END $$",
     ("tests.test_agents.Passes.test_a_beat_keeps_only_the_tenants_lines_in_use",)),
    ("通話の途中で使わない印を付けた回線を、合図がすぐ外す（0109：声もおわびも通らず、お客様を無言で放置する）",
     _edited("0109_review_1022.sql", "CREATE OR REPLACE FUNCTION salescall.agent_beat(",
             "OR EXISTS (SELECT 1 FROM salescall.attempts t", "AND EXISTS (SELECT 1 FROM salescall.attempts t"),
     ("tests.test_link.LostConnection.test_a_line_retired_during_a_call_keeps_its_voice_and_apology_until_the_call_ends",)),
    ("目的のお客様だと確かめられなかった通話の答えを戻さない・「-」にしない（0108：別の方の答えが依頼者へ渡る）",
     "DROP TRIGGER target_recheck_blanks ON salescall.target_rechecks",
     ("tests.test_settle.TargetRecheck.test_another_person_answers_become_dashes_and_the_call_does_not_count_as_reaching_the_target",
      "tests.test_settle.TargetRecheck.test_a_recheck_is_only_for_calls_connected_without_the_judges_and_a_missing_recording_blanks_the_answers")),
    ("確かめられなかった通話の答えを、前の通話の答えに戻さず「-」にするだけ（0108：目的のお客様から取った答えまで消す）",
     "CREATE OR REPLACE FUNCTION salescall.target_recheck_blanks() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN"
     " IF NEW.outcome <> 'target' THEN UPDATE salescall.item_values v SET state = 'unresolved', value = NULL, condition = NULL,"
     " reason = NULL, hedged = NULL WHERE v.attempt_id = NEW.attempt_id; END IF; RETURN NEW; END $$",
     ("tests.test_settle.TargetRecheck.test_the_target_keeps_the_answers_and_a_later_call_by_another_person_gives_back_the_earlier_answers",)),
    ("前の通話の番号のまま答えを書き換えられる（0108：戻す答えを取り違え、別の方の答えが残る）",
     "DROP TRIGGER item_value_guard ON salescall.item_values",
     ("tests.test_settle.TargetRecheck.test_the_target_keeps_the_answers_and_a_later_call_by_another_person_gives_back_the_earlier_answers",)),
    ("答えの守りを、設定の値で外せる形に戻す（0108：システムの処理の役が自分で立てて、前の通話の番号のまま書き換えられる）",
     _edited("0108_target_recheck.sql", "CREATE FUNCTION salescall.item_value_guard()", "IF pg_trigger_depth() > 1 THEN",
             "IF pg_trigger_depth() > 1 OR coalesce(current_setting('salescall.item_restore', true), '') <> '' THEN"),
     ("tests.test_settle.TargetRecheck.test_the_guard_cannot_be_switched_off_by_a_setting_and_unconfirmed_answers_are_never_given_back",)),
    ("戻すとき、目的のお客様だと確かめられなかった通話の答えも選ぶ（0108：別の方の答え・確かめていない答えに戻す）",
     _edited("0108_target_recheck.sql", "CREATE FUNCTION salescall.target_recheck_blanks()",
             "AND NOT EXISTS (SELECT 1 FROM salescall.target_rechecks x WHERE x.attempt_id = h.attempt_id AND x.outcome <> 'target')",
             ""),
     ("tests.test_settle.TargetRecheck.test_the_guard_cannot_be_switched_off_by_a_setting_and_unconfirmed_answers_are_never_given_back",)),
    ("戻すとき、業務設定の版を戻さない（0108：古い版の答えが新しい版の印のまま残る）",
     _edited("0108_target_recheck.sql", "CREATE FUNCTION salescall.target_recheck_blanks()", "SET version_id = prev.version_id, ", "SET "),
     ("tests.test_settle.TargetRecheck.test_the_target_keeps_the_answers_and_a_later_call_by_another_person_gives_back_the_earlier_answers",)),
    ("目的のお客様の確かめ直しの変更の拒みを外す（0108）", "DROP TRIGGER target_rechecks_frozen ON salescall.target_rechecks",
     ("tests.test_settle.TargetRecheck.test_another_person_answers_become_dashes_and_the_call_does_not_count_as_reaching_the_target",)),
    # ── 2026-10-22 の見直し（0109）の守り。試験は tests/test_guards_1022.py
    ("かけられなかった発信（call_failed）も、お客様のところで鳴った発信に数える（0109：回線の1日の上限が減り、ほかの依頼者が"
     "その番号へ3日かけられない）",
     "CREATE OR REPLACE FUNCTION salescall.is_placed(s salescall.attempt_state) RETURNS boolean LANGUAGE sql IMMUTABLE AS"
     " $$ SELECT salescall.is_dialed(s) $$",
     ("tests.test_guards_1022.Guards.test_a_call_the_line_could_not_place_counts_nowhere",)),
    ("録音の部分を、見えない組・依頼者の違う組に足せる（0109：依頼者Aの取引から依頼者Bの組に足して承認できる）",
     _edited("0109_review_1022.sql", "CREATE OR REPLACE FUNCTION salescall.recording_before_insert()",
             "s.client_id IS NOT DISTINCT FROM NEW.client_id) THEN", "true) AND false THEN"),
     ("tests.test_guards_1022.Guards.test_a_recording_set_cannot_be_added_to_or_approved_from_another_client",)),
    ("録音の組を、依頼者の違う名で承認できる（0109）",
     _edited("0109_review_1022.sql", "CREATE OR REPLACE FUNCTION salescall.recording_approval_by_person()",
             "s.client_id IS NOT DISTINCT FROM NEW.client_id) THEN", "true) AND false THEN"),
     ("tests.test_guards_1022.Guards.test_a_recording_set_cannot_be_added_to_or_approved_from_another_client",)),
    ("次の動きの記録の番号を、書く側が渡せる（0109：最大の番号を入れると、あとの「最大＋1」が桁あふれで失敗し続ける）",
     "DROP TRIGGER next_seq ON salescall.task_next_actions",
     ("tests.test_guards_1022.Guards.test_the_order_and_the_time_of_latest_wins_records_are_decided_by_the_database",)),
    ("実施回の版の番号を、書く側が渡せる（0109）", "DROP TRIGGER next_seq ON salescall.run_versions",
     ("tests.test_guards_1022.Guards.test_the_order_and_the_time_of_latest_wins_records_are_decided_by_the_database",)),
    ("依頼者の有効・無効の番号を、書く側が渡せる（0109：依頼者を無効にできなくなる）", "DROP TRIGGER next_seq ON salescall.client_states",
     ("tests.test_guards_1022.Guards.test_the_order_and_the_time_of_latest_wins_records_are_decided_by_the_database",)),
    ("名前の読みの番号を、書く側が渡せる（0109）", "DROP TRIGGER next_seq ON salescall.version_name_readings",
     ("tests.test_guards_1022.Guards.test_only_readings_chosen_by_listening_get_an_order_number",)),
    ("名前の読みの番号を、版の中身に書いた読みにも振る（0109：業務設定の中身を置けない）",
     "DROP TRIGGER next_seq ON salescall.version_name_readings; CREATE TRIGGER next_seq BEFORE INSERT ON"
     " salescall.version_name_readings FOR EACH ROW EXECUTE FUNCTION salescall.next_seq('seq', 'version_id', 'name_text')",
     ("tests.test_guards_1022.Guards.test_only_readings_chosen_by_listening_get_an_order_number",)),
    ("次の動きを決めた時刻を、書く側が渡せる（0109）", "DROP TRIGGER stamp_now ON salescall.task_next_actions",
     ("tests.test_guards_1022.Guards.test_the_order_and_the_time_of_latest_wins_records_are_decided_by_the_database",)),
    ("録音の承認の時刻を、書く側が渡せる（0109：未来の時刻で、あとの承認が「いまの組」にならない）",
     "DROP TRIGGER stamp_now ON salescall.recording_approvals",
     ("tests.test_guards_1022.Guards.test_the_order_and_the_time_of_latest_wins_records_are_decided_by_the_database",)),
    ("お詫びの承認の時刻を、書く側が渡せる（0109）", "DROP TRIGGER stamp_now ON salescall.apology_approvals",
     ("tests.test_guards_1022.Guards.test_an_apology_approval_cannot_point_at_another_contractors_order",)),
    ("お詫びの承認が、ほかの契約者の案を指せる（0109：契約者Bの承認を「重複」で塞げる。0131 からは、承認に結ぶ録音の組の守り"
     "＝その案の組だけ、も同じことを止めるので、2つとも外す）",
     "ALTER TABLE salescall.apology_approvals DROP CONSTRAINT apology_approvals_order_of_client;"
     " DROP TRIGGER apology_approval_recording ON salescall.apology_approvals",
     ("tests.test_guards_1022.Guards.test_an_apology_approval_cannot_point_at_another_contractors_order",)),
    ("品質の評価が、ほかの契約者の停止を指せる（0109）", "DROP TRIGGER stop_event_visible ON salescall.quality_evaluations",
     ("tests.test_guards_1022.Guards.test_a_stop_or_a_price_of_another_contractor_cannot_be_pointed_at",)),
    ("利用量が、ほかの契約者の単価を指せる（0109）", "DROP TRIGGER price_visible ON salescall.usage_entries",
     ("tests.test_guards_1022.Guards.test_a_stop_or_a_price_of_another_contractor_cannot_be_pointed_at",)),
    ("答えを、別のお客様の行へ付け替えられる（0108：中身が変わらなければ通す早道）",
     _edited("0108_target_recheck.sql", "CREATE FUNCTION salescall.item_value_guard()",
             "IF TG_OP = 'UPDATE' AND (NEW.tenant_id,", "IF false AND (NEW.tenant_id,"),
     ("tests.test_guards_1022.Guards.test_an_answer_cannot_be_moved_to_another_customer",)),
    ("サーバーの役が、状態の移り変わりの記録に直接書ける（0109：予約のままの試行が「お客様が出た」になる）",
     "GRANT INSERT ON salescall.attempt_state_history TO salescall_app",
     ("tests.test_guards_1022.Guards.test_records_written_only_by_the_database_cannot_be_written_directly",)),
    ("サーバーの役が、前の答えの記録に直接書ける（0109：取っていない値が「確かめた答え」として戻る）",
     "GRANT INSERT ON salescall.item_value_history TO salescall_app",
     ("tests.test_guards_1022.Guards.test_records_written_only_by_the_database_cannot_be_written_directly",)),
    ("サーバーの役が、名前の変更の記録に直接書ける（0109：ほかの依頼者の分も足せる）",
     "GRANT INSERT ON salescall.name_changes TO salescall_app",
     ("tests.test_guards_1022.Guards.test_records_written_only_by_the_database_cannot_be_written_directly",)),
    ("状態の移り変わりを残す仕掛けを、呼んだ役の権限で動かす（0109：直接書けなくした記録を、仕掛けも書けない）",
     "ALTER FUNCTION salescall.attempt_after_write() SECURITY INVOKER",
     ("tests.test_guards_1022.Guards.test_records_written_only_by_the_database_cannot_be_written_directly",)),
    ("前の答えを残す仕掛けを、呼んだ役の権限で動かす（0109）", "ALTER FUNCTION salescall.item_value_guard() SECURITY INVOKER",
     ("tests.test_guards_1022.Guards.test_records_written_only_by_the_database_cannot_be_written_directly",)),
    ("名前の変更を残す仕掛けを、呼んだ役の権限で動かす（0109）", "ALTER FUNCTION salescall.only_name_changes() SECURITY INVOKER",
     ("tests.test_guards_1022.Guards.test_records_written_only_by_the_database_cannot_be_written_directly",)),
    ("回線の名前の変更を残す仕掛けを、呼んだ役の権限で動かす（0109）", "ALTER FUNCTION salescall.line_before_update() SECURITY INVOKER",
     ("tests.test_guards_1022.Guards.test_records_written_only_by_the_database_cannot_be_written_directly",)),
    ("連携の台帳の、どの行・どの送り先・どの版かを書き換えられる（0109：「送った」記録を、送っていない別のお客様へ付け替える）",
     "GRANT UPDATE ON salescall.deliveries TO salescall_app",
     ("tests.test_foundation.ServerOnly.test_a_delivery_record_keeps_which_row_it_was_sent_for",)),
    ("控えを残す日数を、契約者が設定で変えられない（0110：2026-10-22 ユーザー「設定で契約者が変えられるようにしてください」）",
     "ALTER TABLE salescall.setting_definitions DISABLE TRIGGER setting_definitions_frozen;"
     " UPDATE salescall.setting_definitions SET finest_scope = 'system' WHERE key = 'backup.ai.keep_days_drive';"
     " ALTER TABLE salescall.setting_definitions ENABLE TRIGGER setting_definitions_frozen",
     ("tests.test_backup.PerClient.test_a_contractor_sets_its_own_keep_days",)),
    ("書き換える関数を足しても、決まりの機械の表に載せずに済む（呼んでよい部品の見張りから漏れる）",
     "ALTER FUNCTION salescall.next_customer_promise_at(uuid) VOLATILE",   # 書き換えうる関数が1つ増えた形
     ("tests.test_foundation.ServerOnly.test_every_function_that_writes_has_its_place_in_the_rules",)),
    ("ほかの依頼者の約束を、門が見ない（0111：契約者が「待つ」「かけない」を選んでも、約束の直前にほかの依頼者の電話がかかる）",
     "CREATE OR REPLACE FUNCTION salescall.number_next_promise_elsewhere(p_phone text) RETURNS TABLE (relation text, wait_until timestamptz) LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$ SELECT NULL::text, NULL::timestamptz WHERE salescall.cur_client() IS NULL $$",
     ("tests.test_gate.Pacing.test_before_another_clients_promise_the_contractor_chooses_to_call_wait_or_not_call",)),
    ("ほかの契約者の約束の時刻を渡す（0111：契約者の外の中身が出る）",
     "CREATE OR REPLACE FUNCTION salescall.number_next_promise_elsewhere(p_phone text) RETURNS TABLE (relation text, wait_until timestamptz) LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$ SELECT x.relation, max(x.until) FROM (SELECT CASE WHEN n.tenant_id = salescall.cur_tenant() THEN 'same_tenant' ELSE 'other_tenant' END AS relation, coalesce(n.window_until, greatest(n.callee_at, n.call_at)) AS until FROM salescall.current_next_actions n JOIN salescall.task_numbers k ON k.task_id = n.task_id AND k.contact_id = n.contact_id WHERE k.phone_e164 = p_phone AND n.client_id IS DISTINCT FROM salescall.cur_client() AND n.kind = 'call' AND n.promised) x GROUP BY x.relation $$",
     ("tests.test_gate.Pacing.test_another_contractors_promise_time_is_not_shown",)),
    ("約束の時刻を過ぎたら、まだかけていない約束でも待たない（0111：営業の電話が約束の電話より先に届く）",
     "CREATE OR REPLACE FUNCTION salescall.number_next_promise_elsewhere(p_phone text) RETURNS TABLE (relation text, wait_until timestamptz) LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$ SELECT x.relation, CASE WHEN x.relation = 'same_tenant' THEN max(x.until) END FROM (SELECT CASE WHEN n.tenant_id = salescall.cur_tenant() THEN 'same_tenant' ELSE 'other_tenant' END AS relation, coalesce(n.window_until, greatest(n.callee_at, n.call_at)) AS until FROM salescall.current_next_actions n JOIN salescall.task_numbers k ON k.task_id = n.task_id AND k.contact_id = n.contact_id WHERE k.phone_e164 = p_phone AND n.client_id IS DISTINCT FROM salescall.cur_client() AND n.kind = 'call' AND n.promised AND coalesce(n.window_until, greatest(n.callee_at, n.call_at)) > now()) x GROUP BY x.relation $$",
     ("tests.test_gate.Pacing.test_a_promise_not_yet_called_keeps_others_waiting_even_after_its_time",)),
    ("行の回数に、お客様と約束したかけ直しの電話も数える（0113：約束の電話で、行の回数と再発信の方針の回数が減る）",
     "CREATE OR REPLACE FUNCTION salescall.counts_as_try(s salescall.attempt_state, p_cut_off boolean, p_promised boolean,"
     " p_attempt uuid) RETURNS boolean LANGUAGE sql STABLE AS $$ SELECT salescall.dial_kind(s, p_cut_off, p_attempt) = 'reached' $$",
     ("tests.test_settle.CountedCalls.test_promised_calls_are_not_counted_like_the_gate",)),
    ("行の回数に、回線の側の失敗・切れた発信も数える（0113：お客様に一度も繋がらないまま、再発信の方針の回数で行が終わる）",
     "CREATE OR REPLACE FUNCTION salescall.counts_as_try(s salescall.attempt_state, p_cut_off boolean, p_promised boolean,"
     " p_attempt uuid) RETURNS boolean LANGUAGE sql STABLE AS $$ SELECT salescall.is_dialed(s) AND NOT p_promised $$",
     ("tests.test_settle.CountedCalls.test_line_side_failures_do_not_use_up_the_policy",
      "tests.test_settle.CountedCalls.test_line_cuts_in_a_row_do_not_end_the_row")),
    ("PC の部品がかけなかった発信を、取り消しにできない（0113：発信の失敗として判定され、方針の回数と目的を失う）",
     "ALTER TABLE salescall.attempt_transitions DISABLE TRIGGER attempt_transitions_frozen;"
     " DELETE FROM salescall.attempt_transitions WHERE from_state = 'dialing' AND to_state = 'released'",
     ("tests.test_emergency.OnTheServer.test_a_call_withheld_by_the_pc_is_not_dialed_and_not_a_line_fault",)),
    ("途中で切れたかを、あとから書き換えられる（0113：続けて切れた数がずれる）",
     "DROP TRIGGER attempt_cut_off_once ON salescall.attempts",
     ("tests.test_settle.CutOffCalls.test_whether_a_call_was_cut_is_kept_once_on_the_attempt",)),
    ("途中で切れたかを、終わる前の試行に書ける（0113）",
     "ALTER TABLE salescall.attempts DROP CONSTRAINT attempts_cut_off_after_end",
     ("tests.test_settle.CutOffCalls.test_whether_a_call_was_cut_is_kept_once_on_the_attempt",)),
    ("お詫びの印と理由が食い違ってよい（0113：会話の処理が、理由の無いお詫び・決まっていない理由を受け取る）",
     "ALTER TABLE salescall.task_next_actions DROP CONSTRAINT task_next_actions_apology_reasons",
     ("tests.test_settle.DatabaseGuards.test_apology_reasons_and_missed_rounds_are_kept_consistent",)),
    ("かけられなかった回の数が、過ぎた回の数を超えてよい（0113：約束の打ち止めの数がずれる）",
     "ALTER TABLE salescall.task_next_actions DROP CONSTRAINT task_next_actions_window_missed",
     ("tests.test_settle.DatabaseGuards.test_apology_reasons_and_missed_rounds_are_kept_consistent",)),
    ("後任の方をお呼びする印が、終わる予定に付いてよい（0114：会話の処理に、かけない予定の印が渡りうる）",
     "ALTER TABLE salescall.task_next_actions DROP CONSTRAINT task_next_actions_successor_calls",
     ("tests.test_settle.DatabaseGuards.test_the_successor_mark_is_only_on_a_call",)),
    ("お客様が口にした端の印が、1日の中の幅の無い予定に付いてよい・形が崩れてよい（0118：門が、お客様の言っていない端を"
     "歯止めの外まで広げうる）",
     "ALTER TABLE salescall.task_next_actions DROP CONSTRAINT task_next_actions_window_said_check",
     ("tests.test_settle.DatabaseGuards.test_the_said_edges_belong_to_a_daily_window",)),
    ("発信に失敗したときのかけ直しの並びが、1日目から始まらない・小さくなってよい（0118：番号を一度もかけずに終える・日が戻る）",
     "ALTER TABLE salescall.setting_values DROP CONSTRAINT setting_values_failed_retry_days_order",
     ("tests.test_settle.UnusableNumber.test_the_order_is_per_client",)),
    # 見直し15（0116）：数え方・止めているあいだに通す電話・約束を果たす電話・判定できない通話の扱いを、それぞれ1か所に
    ("数え方が、途中で切れたかの記録を見ずに、切れ方 interrupted と結果不明をすべて数えない（0116 の前の形：お客様が自分で切った"
     "通話を数えず、終わりなくかけ続ける）",
     "CREATE OR REPLACE FUNCTION salescall.dial_kind(s salescall.attempt_state, p_cut_off boolean, p_attempt uuid) RETURNS text"
     " LANGUAGE sql STABLE AS $$ SELECT CASE WHEN NOT salescall.is_dialed(s) THEN NULL WHEN NOT salescall.is_placed(s) THEN"
     " 'not_placed' WHEN s = 'technical_error' OR (SELECT a.disconnect FROM salescall.attempts a WHERE a.attempt_id = p_attempt)"
     " = 'interrupted' THEN 'cut_off' ELSE 'reached' END $$",
     ("tests.test_gate.UncountedDials.test_a_customer_hanging_up_in_the_middle_of_own_words_counts",
      "tests.test_gate.Pacing.test_a_call_cut_by_our_failure_is_like_a_line_drop")),
    ("発信に失敗（鳴っていない）も、お客様に届いた1回に数える（0116：業務の1日の上限・聞き直しの回数が減る）",
     "CREATE OR REPLACE FUNCTION salescall.dial_kind(s salescall.attempt_state, p_cut_off boolean, p_attempt uuid) RETURNS text"
     " LANGUAGE sql STABLE AS $$ SELECT CASE WHEN NOT salescall.is_dialed(s) THEN NULL WHEN coalesce(p_cut_off, false) THEN"
     " 'cut_off' WHEN s = 'technical_error' AND NOT salescall.reached_answer(p_attempt) THEN 'line_failure' ELSE 'reached' END $$",
     ("tests.test_gate.BusinessDailyLimit.test_a_failed_dial_does_not_use_up_the_business_limit",)),
    ("品質の停止でも通す電話の表から、お詫びの電話を外す（0116：品質の停止のあいだ、お詫びの電話がかからない）",
     "ALTER TABLE salescall.stop_lets_through DISABLE TRIGGER stop_lets_through_frozen;"
     " DELETE FROM salescall.stop_lets_through WHERE cause IN ('quality_gate', 'forbidden_speech', 'judge_unknown')",
     ("tests.test_gate.Stops.test_a_quality_stop_lets_only_the_apology_call_through",)),
    ("決まった文面だけの電話（お詫び）でも、品質の段・目的のお客様を呼ぶ材料を見る（0120 の前は表の行で決めていた）",
     "CREATE OR REPLACE FUNCTION salescall.fixed_text_kind(p_kind text) RETURNS boolean LANGUAGE sql IMMUTABLE AS $$ SELECT false $$",
     ("tests.test_gate.VersionAndStage.test_only_fixed_text_calls_skip_the_stage",
      "tests.test_apology.Apology.test_an_apology_call_is_neither_counted_in_the_stage_nor_measured")),
    # 見直し16（0118）：続けて切れた数・発信に失敗した数・断られたかの決まり・赤字の知らせ・声の性別・お客様が口にした端
    ("続けて何回切れても、お詫びしてすぐかけ直す（0118 の前の形：電波の悪いお客様へ終わりなくかけ続ける）",
     "CREATE OR REPLACE FUNCTION salescall.dial_kind(s salescall.attempt_state, p_cut_off boolean, p_attempt uuid) RETURNS text"
     " LANGUAGE sql STABLE AS $$ SELECT CASE WHEN NOT salescall.is_dialed(s) THEN NULL WHEN NOT salescall.is_placed(s) THEN"
     " 'not_placed' WHEN coalesce(p_cut_off, false) THEN 'cut_off' WHEN s = 'technical_error' AND NOT"
     " salescall.reached_answer(p_attempt) THEN 'line_failure' ELSE 'reached' END $$;"
     " CREATE OR REPLACE FUNCTION salescall.resume_allowed(p_attempt uuid) RETURNS boolean LANGUAGE sql STABLE"
     " AS $$ SELECT coalesce((SELECT a.cut_off FROM salescall.attempts a WHERE a.attempt_id = p_attempt), false) $$",
     ("tests.test_settle.CutOffCalls.test_cuts_beyond_the_call_backs_count_and_the_row_ends",)),
    ("続けて切れた数を、行の回数に数えるかの決まりと、すぐかけ直すかの決まりで別々に持つ（0118：数え方と予定が食い違う）",
     "CREATE OR REPLACE FUNCTION salescall.resume_in_a_row() RETURNS integer LANGUAGE sql IMMUTABLE AS $$ SELECT 5 $$;"
     " CREATE OR REPLACE FUNCTION salescall.dial_kind(s salescall.attempt_state, p_cut_off boolean, p_attempt uuid) RETURNS text"
     " LANGUAGE sql STABLE AS $$ SELECT CASE WHEN NOT salescall.is_dialed(s) THEN NULL WHEN NOT salescall.is_placed(s) THEN"
     " 'not_placed' WHEN coalesce(p_cut_off, false) AND salescall.cuts_in_a_row(p_attempt) <= 2 THEN 'cut_off'"
     " WHEN s = 'technical_error' AND NOT salescall.reached_answer(p_attempt) THEN 'line_failure' ELSE 'reached' END $$",
     ("tests.test_settle.CutOffCalls.test_cuts_beyond_the_call_backs_count_and_the_row_ends",)),
    ("途中で切れて判定できなかった通話も、断られたかが決まっていないとみなす（0118 の前の形：同じお客様のほかの行を止め続ける）",
     "CREATE OR REPLACE FUNCTION salescall.refusal_undecided(p_attempt uuid) RETURNS boolean LANGUAGE sql STABLE"
     " SET search_path = salescall, pg_temp AS $$ SELECT NOT salescall.is_settled(p_attempt)"
     " OR salescall.unknown_pending(p_attempt) $$",
     ("tests.test_settle.CutOffCalls.test_rejudging_an_old_cut_call_does_not_revive_the_ended_row_nor_hold_the_customer",)),
    ("同じ番号の、発信に失敗した数を数えない（0118：受け付けない番号へ、終わりなくかける）",
     _edited("0129_unrung_blame.sql", "CREATE FUNCTION salescall.failed_run(", "SELECT (count(*) FILTER (WHERE c.blame IN ('number', 'pending')))::integer,\n         (count(*) FILTER (WHERE c.blame = 'number'))::integer,", 'SELECT 0, 0,', end="\n$$"),
     ("tests.test_settle.UnusableNumber.test_four_failures_follow_the_order_and_stop_the_number",
      "tests.test_settle.UnusableNumber.test_other_rows_of_the_same_number_do_not_dial_it_again")),
    ("同じ番号の、発信に失敗した数を、あいだにかけられた発信があっても数え続ける（0118：番号を早く捨てる）",
     _edited("0129_unrung_blame.sql", "CREATE FUNCTION salescall.failed_run(", "WHERE NOT x.rang AND x.claimed_at > coalesce((SELECT max(y.claimed_at) FROM x y WHERE y.rang), '-infinity'::timestamptz))", 'WHERE NOT x.rang)', end="\n$$"),
     ("tests.test_settle.UnusableNumber.test_a_call_that_rang_restarts_the_run",)),
    ("声の性別の表示名を、あとから書き換えられる（0118：前の通話の声の性別の意味が変わる）",
     "CREATE OR REPLACE FUNCTION salescall.voices_guard() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN"
     " IF current_user <> 'salescall_owner' AND salescall.cur_actor_kind() IS DISTINCT FROM 'admin' THEN"
     " RAISE EXCEPTION 'x' USING ERRCODE = 'insufficient_privilege'; END IF;"
     " IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'x' USING ERRCODE = 'check_violation'; END IF;"
     " IF TG_OP = 'UPDATE' AND ((NEW.voice_name, NEW.note, NEW.added_at) IS DISTINCT FROM (OLD.voice_name, OLD.note, OLD.added_at)"
     " OR OLD.retired_at IS NOT NULL) THEN RAISE EXCEPTION 'x' USING ERRCODE = 'check_violation'; END IF;"
     " RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END; END $$",
     ("tests.test_gate.CallerNumberAndVoice.test_another_clients_voice_is_followed_by_the_other_gender_when_there_is_one",)),
    ("赤字の警告の「もう一度の知らせ」を、何度でも書き換えられる（0118：期限を過ぎた赤字を、何度も知らせる・知らせた記録が消える）",
     "CREATE OR REPLACE FUNCTION salescall.deficit_warning_guard() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN"
     " IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'x' USING ERRCODE = 'integrity_constraint_violation'; END IF;"
     " IF OLD.confirmed_at IS NULL AND NEW.confirmed_at IS NOT NULL AND salescall.cur_actor_kind() IS DISTINCT FROM 'operator'"
     " AND salescall.cur_actor_kind() IS DISTINCT FROM 'admin' THEN RAISE EXCEPTION 'x' USING ERRCODE = 'insufficient_privilege',"
     " CONSTRAINT = 'deficit_confirmed_by_person'; END IF; RETURN NEW; END $$",
     ("tests.test_usage.CostRatio.test_deficit_warns_and_reminds_once_but_never_stops",)),
    ("お客様指定の歯止め時間の始まりと終わりが逆でもよい（0118：お客様が言っていない深夜・早朝にかけうる・幅が空になる）",
     "ALTER TABLE salescall.setting_values DROP CONSTRAINT setting_values_designated_limit_order",
     ("tests.test_settle.CallbackWindow.test_the_designated_limit_is_one_day_and_per_business",)),
    ("止めているあいだに通す電話の表を、持ち主が書き換えられる（0116）", "DROP TRIGGER stop_lets_through_frozen ON salescall.stop_lets_through",
     ("tests.test_gate.Stops.test_the_kinds_of_promise_keeping_calls_are_decided_in_one_place",)),
    ("止めたとき、その停止でも通す電話の予約も取り消す（0116 の前の形）",
     "CREATE OR REPLACE FUNCTION salescall.release_claims_for_stop(p_event_id bigint) RETURNS integer"
     " LANGUAGE plpgsql SECURITY DEFINER SET search_path = salescall, pg_temp AS $$"
     " DECLARE s salescall.stop_events; n integer;"
     " BEGIN SELECT * INTO s FROM salescall.open_stop_events e WHERE e.event_id = p_event_id;"
     " UPDATE salescall.attempts a SET state = 'released' WHERE a.state = 'claimed'"
     " AND (s.scope = 'system' OR (s.scope = 'tenant' AND a.tenant_id = s.tenant_id) OR (s.scope = 'client' AND a.client_id = s.client_id)"
     " OR (s.scope = 'business' AND a.client_id = s.client_id AND a.version_id IN (SELECT v.version_id FROM salescall.business_versions v"
     " WHERE v.business_id = s.business_id)) OR (s.scope = 'line' AND a.line_id = s.line_id));"
     " GET DIAGNOSTICS n = ROW_COUNT; RETURN n; END $$",
     ("tests.test_gate.PromisesStopsAndConflicts.test_a_stop_keeps_the_claims_of_calls_it_lets_through",)),
    ("番号の聞き直しを、約束を果たす電話に入れる（0116 の前の形：ほかの依頼者の聞き直しの電話で、こちらの行を終える・待つ）",
     "CREATE OR REPLACE FUNCTION salescall.keeping_kind(p_purpose text, p_promised boolean) RETURNS text LANGUAGE sql IMMUTABLE AS"
     " $$ SELECT CASE WHEN p_purpose = 'apology' THEN 'apology' WHEN p_purpose = 'resume' THEN 'resume'"
     " WHEN coalesce(p_promised, false) THEN 'promised' END $$",
     ("tests.test_gate.PromisesStopsAndConflicts.test_only_promised_callbacks_of_another_client_are_waited_for",)),
    ("ほかの依頼者の約束を、印 promised で読み、止まっていても数える（0111 の形）",
     "CREATE OR REPLACE FUNCTION salescall.number_next_promise_elsewhere(p_phone text) RETURNS TABLE (relation text, wait_until timestamptz)"
     " LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$"
     " SELECT x.relation, CASE WHEN x.relation = 'same_tenant' THEN max(x.until) END"
     " FROM (SELECT CASE WHEN n.tenant_id = salescall.cur_tenant() THEN 'same_tenant' ELSE 'other_tenant' END AS relation,"
     " coalesce(n.window_until, greatest(n.callee_at, n.call_at)) AS until FROM salescall.current_next_actions n"
     " JOIN salescall.task_numbers k ON k.task_id = n.task_id AND k.contact_id = n.contact_id"
     " WHERE k.phone_e164 = p_phone AND n.client_id IS DISTINCT FROM salescall.cur_client() AND n.kind = 'call' AND n.promised) x"
     " GROUP BY x.relation $$",
     ("tests.test_gate.PromisesStopsAndConflicts.test_only_promised_callbacks_of_another_client_are_waited_for",
      "tests.test_gate.PromisesStopsAndConflicts.test_another_clients_promise_is_waited_for_only_while_that_stop_lets_it_through")),
    ("ほかの依頼者の約束に、自分の依頼者の約束も入れる（0111 の条件を外す）",
     "CREATE OR REPLACE FUNCTION salescall.number_next_promise_elsewhere(p_phone text) RETURNS TABLE (relation text, wait_until timestamptz)"
     " LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$"
     " SELECT x.relation, CASE WHEN x.relation = 'same_tenant' THEN max(x.until) END"
     " FROM (SELECT CASE WHEN n.tenant_id = salescall.cur_tenant() THEN 'same_tenant' ELSE 'other_tenant' END AS relation,"
     " coalesce(n.window_until, greatest(n.callee_at, n.call_at)) AS until FROM salescall.current_next_actions n"
     " JOIN salescall.task_numbers k ON k.task_id = n.task_id AND k.contact_id = n.contact_id"
     " WHERE k.phone_e164 = p_phone AND n.kind = 'call' AND salescall.keeping_kind(n.purpose, n.promised) = 'promised') x"
     " GROUP BY x.relation $$",
     ("tests.test_gate.PromisesStopsAndConflicts.test_own_clients_promises_are_not_another_clients",)),
    ("これからかける約束の一覧が、その停止でも通す約束も外す（0116 の前の形）",
     "CREATE OR REPLACE FUNCTION salescall.promises_waiting()"
     " RETURNS TABLE (task_id uuid, client_id uuid, call_at timestamptz, callee_at timestamptz, phone_e164 text)"
     " LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$"
     " SELECT n.task_id, n.client_id, n.call_at, n.callee_at, k.phone_e164"
     " FROM salescall.task_next_actions n"
     " JOIN salescall.tasks t ON t.task_id = n.task_id"
     " JOIN salescall.runs r ON r.run_id = t.run_id"
     " JOIN salescall.task_numbers k ON k.task_id = n.task_id AND k.contact_id = n.contact_id"
     " WHERE n.tenant_id = salescall.cur_tenant() AND n.promised AND n.kind = 'call'"
     " AND n.seq = (SELECT c.seq FROM salescall.current_next_actions c WHERE c.task_id = n.task_id)"
     " AND salescall.client_active(n.client_id) AND NOT salescall.task_excluded_now(n.task_id)"
     " AND NOT EXISTS (SELECT 1 FROM salescall.open_stops(n.tenant_id, n.client_id, r.business_id))"
     " AND NOT EXISTS (SELECT 1 FROM salescall.attempts a"
     " WHERE a.task_id = n.task_id AND salescall.is_live(a.state, a.claim_expires_at)) $$",
     ("tests.test_gate.PromisesStopsAndConflicts.test_promises_kept_for_the_line_while_a_stop_lets_them_through",)),
    ("同じ番号の、時刻を指定した約束の前でも、約束でない電話を待たせない（0116 の前の形）",
     "CREATE OR REPLACE FUNCTION salescall.number_promise_soon(p_phone text) RETURNS timestamptz"
     " LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$ BEGIN RETURN NULL; END $$",
     ("tests.test_gate.PromisesStopsAndConflicts.test_a_normal_call_waits_for_an_exact_promise_coming_soon_on_the_same_number",)),
    ("ほかの契約者の発信から待つ時刻を、正確な時刻で返す（0116 の前の形：ほかの契約者がかけた時刻が分かる）",
     "CREATE OR REPLACE FUNCTION salescall.number_dial_wait_elsewhere(p_phone text, p_same_gap interval, p_other_gap interval)"
     " RETURNS TABLE (relation text, wait_until timestamptz) LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp"
     " AS $$ SELECT x.relation, x.until FROM (SELECT CASE WHEN a.tenant_id = salescall.cur_tenant() THEN 'same_tenant'"
     " ELSE 'other_tenant' END AS relation, max(a.claimed_at + CASE WHEN a.tenant_id = salescall.cur_tenant() THEN p_same_gap"
     " ELSE p_other_gap END) AS until FROM salescall.attempts a WHERE a.phone_e164 = p_phone"
     " AND a.client_id IS DISTINCT FROM salescall.cur_client() AND salescall.is_placed(a.state) GROUP BY 1) x WHERE x.until > now() $$",
     ("tests.test_gate.Pacing.test_the_interval_with_other_contractors_is_the_contractors_setting",)),
    ("候補を出す処理が、ほかの依頼者・契約者の進行中の発信を見ない（0116 の前の形：門が断り、予定を書き直し続ける）",
     "CREATE OR REPLACE FUNCTION salescall.number_live(p_phone text) RETURNS boolean"
     " LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$ BEGIN"
     " PERFORM salescall.server_only(); RETURN false; END $$",
     ("tests.test_gate.PromisesStopsAndConflicts.test_conflicts_with_a_call_in_progress_skip_only_this_round",)),
    ("判定できない通話が、あとの通話で決まっても「まだ決まっていない」のまま（0116 の前の形：同じお客様のほかの行が永久にかからない）",
     "CREATE OR REPLACE FUNCTION salescall.unknown_pending(p_attempt uuid) RETURNS boolean"
     " LANGUAGE sql STABLE SET search_path = salescall, pg_temp AS $$ SELECT salescall.result_unknown(p_attempt) $$",
     ("tests.test_undecided_unknown.UnknownPending.test_a_later_call_that_reaches_the_customer_and_is_decided_settles_it",)),
    ("判定できない通話を、あとの通話がお客様につながらなくても決まったとみなす（0116）",
     "CREATE OR REPLACE FUNCTION salescall.unknown_pending(p_attempt uuid) RETURNS boolean"
     " LANGUAGE sql STABLE SET search_path = salescall, pg_temp AS $$ SELECT salescall.result_unknown(p_attempt)"
     " AND NOT EXISTS (SELECT 1 FROM salescall.attempts a JOIN salescall.attempts b ON b.task_id = a.task_id"
     " WHERE a.attempt_id = p_attempt AND b.claimed_at > a.claimed_at AND salescall.is_settled(b.attempt_id)"
     " AND NOT salescall.result_unknown(b.attempt_id)) $$",
     ("tests.test_undecided_unknown.UnknownPending.test_a_later_call_that_did_not_reach_the_customer_keeps_it_pending",)),
    ("控えの記録の種類の決まりを外す（0109）", "ALTER TABLE salescall.backup_runs DROP CONSTRAINT backup_runs_kind_check",
     ("tests.test_backup.PerClient.test_a_contractor_backs_up_only_its_own_clients_and_copies_submissions",)),
    # 見直し16b（0120）：続けて切れた数の数え直し・判定できない通話の未決・発信に失敗が続いた最初の日
    ("続けて切れた数を、発信に失敗・お客様が出る前の回線の失敗でも数え直す（0120 の前の形：交互に続くと、どの回数にも数えないまま、"
     "すぐのかけ直しを続ける）",
     "CREATE OR REPLACE FUNCTION salescall.cuts_in_a_row(p_attempt uuid) RETURNS integer LANGUAGE sql STABLE"
     " SET search_path = salescall, pg_temp AS $$ SELECT count(*)::integer FROM salescall.attempts a JOIN salescall.attempts b"
     " ON b.task_id = a.task_id WHERE a.attempt_id = p_attempt AND salescall.is_dialed(b.state) AND b.claimed_at <= a.claimed_at"
     " AND coalesce(b.cut_off, false) AND NOT EXISTS (SELECT 1 FROM salescall.attempts c WHERE c.task_id = a.task_id"
     " AND salescall.is_dialed(c.state) AND c.claimed_at > b.claimed_at AND c.claimed_at <= a.claimed_at"
     " AND NOT coalesce(c.cut_off, false)) $$",
     ("tests.test_settle.CutOffCalls.test_only_a_reached_call_restarts_the_count_of_cuts_in_a_row",)),
    ("続けて切れた数を、行の中で切れた数すべてにする（「続けて」を見ない）",
     "CREATE OR REPLACE FUNCTION salescall.cuts_in_a_row(p_attempt uuid) RETURNS integer LANGUAGE sql STABLE"
     " SET search_path = salescall, pg_temp AS $$ SELECT count(*)::integer FROM salescall.attempts a JOIN salescall.attempts b"
     " ON b.task_id = a.task_id WHERE a.attempt_id = p_attempt AND salescall.is_dialed(b.state) AND b.claimed_at <= a.claimed_at"
     " AND coalesce(b.cut_off, false) $$",
     ("tests.test_settle.CutOffCalls.test_only_a_reached_call_restarts_the_count_of_cuts_in_a_row",)),
    ("あとの発信がつながれば、その結果も判定できなくても、前の通話は決まったとみなす",
     "CREATE OR REPLACE FUNCTION salescall.unknown_pending(p_attempt uuid) RETURNS boolean LANGUAGE sql STABLE"
     " SET search_path = salescall, pg_temp AS $$ SELECT salescall.result_unknown(p_attempt)"
     " AND NOT EXISTS (SELECT 1 FROM salescall.attempts a JOIN salescall.attempts b ON b.task_id = a.task_id"
     " WHERE a.attempt_id = p_attempt AND b.claimed_at > a.claimed_at AND salescall.reached_answer(b.attempt_id)"
     " AND salescall.is_settled(b.attempt_id)) $$",
     ("tests.test_undecided_unknown.UnknownPending.test_a_later_call_whose_result_is_also_unknown_keeps_it_pending",)),
    ("発信に失敗が続いた最初の日を、いちばん新しい失敗の日にする（次にかける日がずれる）",
     _edited("0129_unrung_blame.sql", "CREATE FUNCTION salescall.failed_run(", 'salescall.jst(min(c.claimed_at)', 'salescall.jst(max(c.claimed_at)', end="\n$$"),
     ("tests.test_settle.UnusableNumber.test_the_run_of_failures_starts_on_its_first_day",)),
    # 見直し17a（0122）：回線が直った証拠
    ("回線が直った証拠を、前の「その回線で呼び出し中に移った」に戻す（お客様が出る前に回線の側で終わる電話が続いても、回線が止まらない）",
     'CREATE OR REPLACE FUNCTION salescall.consecutive_errors(p_kinds text[], p_cause text, p_evidence text, p_line '
     'uuid DEFAULT NULL) RETURNS integer LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp '
     'AS $$ WITH allowed AS ( SELECT salescall.cur_tenant() IS NOT NULL AND (p_line IS NULL OR EXISTS (SELECT 1 FROM '
     'salescall.lines l WHERE l.line_id = p_line AND l.tenant_id = salescall.cur_tenant())) AS ok ), since AS ( '
     "SELECT greatest( CASE p_evidence WHEN 'in_conversation' THEN (SELECT coalesce(max(h.at), "
     "'-infinity'::timestamptz) FROM salescall.attempt_state_history h JOIN salescall.attempts a ON a.attempt_id = "
     "h.attempt_id WHERE h.to_state = 'in_conversation' AND a.tenant_id = salescall.cur_tenant()) WHEN 'verified' "
     "THEN (SELECT coalesce(max(v.recorded_at), '-infinity'::timestamptz) FROM salescall.verification_summaries v "
     "WHERE v.tenant_id = salescall.cur_tenant()) WHEN 'line_ok' THEN (SELECT coalesce(max(h.at), "
     "'-infinity'::timestamptz) FROM salescall.attempt_state_history h JOIN salescall.attempts a ON a.attempt_id = "
     "h.attempt_id WHERE h.to_state = 'ringing' AND a.line_id = p_line) WHEN 'none' THEN '-infinity'::timestamptz "
     "END, (SELECT coalesce(max(r.at), '-infinity'::timestamptz) FROM salescall.stop_events r JOIN "
     'salescall.stop_events s ON s.event_id = r.resolves_event_id WHERE s.cause::text = p_cause AND s.tenant_id = '
     "salescall.cur_tenant() AND CASE WHEN p_line IS NULL THEN s.scope = 'tenant' ELSE s.scope = 'line' AND "
     's.line_id = p_line END)) AS t ) SELECT CASE WHEN NOT (SELECT ok FROM allowed) OR p_evidence NOT IN '
     "('in_conversation', 'verified', 'line_ok', 'none') OR (p_evidence = 'line_ok' AND p_line IS NULL) THEN NULL "
     'ELSE (SELECT count(*)::integer FROM salescall.error_events e, since WHERE e.kind = ANY(p_kinds) AND '
     'e.occurred_at > since.t AND e.tenant_id = salescall.cur_tenant() AND (p_line IS NULL OR e.line_id = p_line)) '
     'END $$;',
     ("tests.test_watch.LineStops.test_only_a_call_that_rang_and_ended_resets_the_count",)),
    # 2026-10-23 ユーザー：お客様が出る前に毎回終わる番号も「番号を受け付けないときと同じ」（0124）
    ("番号の続きが、発信に失敗だけを数える（0124 の前の形：お客様が出る前に終わる番号へ、方針の間隔で終わりなくかける）",
     _edited("0129_unrung_blame.sql", "CREATE FUNCTION salescall.failed_run(", 'salescall.rang(a.state, a.cut_off, a.attempt_id) AS rang', 'salescall.is_placed(a.state) AS rang', end="\n$$"),
     ("tests.test_settle.UnusableNumber.test_calls_ending_before_the_callee_answers_follow_the_same_order",)),
    ("お客様のところで鳴ったか（rang）が、お客様が出る前に終わった電話も鳴ったとみなす",
     "CREATE OR REPLACE FUNCTION salescall.rang(s salescall.attempt_state, p_cut_off boolean, p_attempt uuid) RETURNS boolean"
     " LANGUAGE sql STABLE AS $$ SELECT salescall.is_placed(s) $$",
     ("tests.test_settle.UnusableNumber.test_a_call_that_rang_restarts_the_run",)),
    # 見直し17b（全部の試験でも落ちなかった壊し方に、試験を足した）
    ("発信に失敗の続きの「最後の失敗の終わり」を、続いた最初の失敗の終わりにする（次にかける時刻を取り違える）",
     _edited("0129_unrung_blame.sql", "CREATE FUNCTION salescall.failed_run(", 'max(c.ended_at) FILTER', 'min(c.ended_at) FILTER', end="\n$$"),
     ("tests.test_settle.UnusableNumber.test_the_run_of_failures_starts_on_its_first_day",)),
    ("これからかける約束の一覧（回線の取り置き）が「強制かけない」の行も入れる",
     "CREATE OR REPLACE FUNCTION salescall.promises_waiting()\nRETURNS TABLE (task_id uuid, client_id uuid, call_at timestamptz, callee_at timestamptz, phone_e164 text)\nLANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$\n  SELECT n.task_id, n.client_id, n.call_at, n.callee_at, k.phone_e164\n  FROM salescall.task_next_actions n\n  JOIN salescall.tasks t ON t.task_id = n.task_id\n  JOIN salescall.runs r ON r.run_id = t.run_id\n  JOIN salescall.task_numbers k ON k.task_id = n.task_id AND k.contact_id = n.contact_id\n  WHERE n.tenant_id = salescall.cur_tenant() AND n.promised AND n.kind = 'call'\n    AND n.seq = (SELECT c.seq FROM salescall.current_next_actions c WHERE c.task_id = n.task_id)\n    AND salescall.client_active(n.client_id)\n    AND NOT EXISTS (SELECT 1 FROM salescall.blocking_stops(n.tenant_id, n.client_id, r.business_id, NULL,\n                                                           salescall.keeping_kind(n.purpose, n.promised)))\n    AND NOT EXISTS (SELECT 1 FROM salescall.attempts a\n                    WHERE a.task_id = n.task_id AND salescall.is_live(a.state, a.claim_expires_at))\n$$;",
     ("tests.test_gate.PromisesStopsAndConflicts.test_a_promise_of_a_row_marked_never_call_is_not_kept_for",)),
    ("赤字の警告を「赤字で止めた（stopped）」で閉じられる（0123 の前：赤字では止めないのに、止めたと記録できる）",
     "ALTER TABLE salescall.deficit_warnings DROP CONSTRAINT deficit_warnings_closed_why_check;"
     " ALTER TABLE salescall.deficit_warnings ADD CONSTRAINT deficit_warnings_closed_why_check"
     " CHECK (closed_why IN ('not_deficit', 'confirm_expired', 'stopped'))",
     ("tests.test_usage.CostRatio.test_an_expired_confirmation_closes_with_its_own_reason_and_warns_again",)),
    ("結果の版の番号を、書く側が渡せる（0123 の前：書く側が「最大＋1」を書き、大きな番号を入れると桁あふれで失敗し続ける）",
     "DROP TRIGGER next_seq ON salescall.task_results",
     ("tests.test_foundation.FrozenRecords.test_result_and_setting_version_numbers_are_decided_by_the_database",)),
    ("業務設定の版の番号を、書く側が渡せる（0123 の前）", "DROP TRIGGER next_seq ON salescall.business_versions",
     ("tests.test_foundation.FrozenRecords.test_result_and_setting_version_numbers_are_decided_by_the_database",)),
    ("判定できない通話を、あとの通話がお客様につながれば、締めの前でも決まったとみなす（見直し17b：この場面の試験が無かった）",
     "CREATE OR REPLACE FUNCTION salescall.unknown_pending(p_attempt uuid) RETURNS boolean LANGUAGE sql STABLE"
     " SET search_path = salescall, pg_temp AS $$ SELECT salescall.result_unknown(p_attempt)"
     " AND NOT EXISTS (SELECT 1 FROM salescall.attempts a JOIN salescall.attempts b ON b.task_id = a.task_id"
     " WHERE a.attempt_id = p_attempt AND b.claimed_at > a.claimed_at AND salescall.reached_answer(b.attempt_id)"
     " AND NOT salescall.result_unknown(b.attempt_id)) $$",
     ("tests.test_undecided_unknown.UnknownPending.test_a_later_call_that_reached_the_customer_but_is_not_settled_keeps_it_pending",)),
    ("すぐかけ直してよいかを「続けて切れた数が1以上」で決める（0123 の前：その発信が回線の失敗でも、前に切れた数が残れば真）",
     "CREATE OR REPLACE FUNCTION salescall.resume_allowed(p_attempt uuid) RETURNS boolean LANGUAGE sql STABLE"
     " SET search_path = salescall, pg_temp AS $$ SELECT salescall.cuts_in_a_row(p_attempt) BETWEEN 1 AND salescall.resume_in_a_row() $$",
     ("tests.test_settle.CutOffCalls.test_only_a_reached_call_restarts_the_count_of_cuts_in_a_row",)),
    ("1本あたりの報酬・時給の間・応答率に、続きの電話も数える（0123 の前は目的の名前を3か所に書いていた）",
     "CREATE OR REPLACE FUNCTION salescall.counts_per_call(p_kind text) RETURNS boolean LANGUAGE sql IMMUTABLE AS"
     " $$ SELECT salescall.counts_for_reward(p_kind) $$",
     ("tests.test_usage.CostRatio.test_a_call_cut_and_called_back_counts_as_one_customer",)),
    ("報酬に、お詫びの電話も数える（0123）",
     "CREATE OR REPLACE FUNCTION salescall.counts_for_reward(p_kind text) RETURNS boolean LANGUAGE sql IMMUTABLE AS"
     " $$ SELECT true $$",
     ("tests.test_apology.Apology.test_delivered_ends_the_row_without_changing_the_result",)),
    ("回線の応答率に、呼び出し中のあとの回線の失敗も数える（0123 の前：呼び出し中に入ったかで数えた）",
     "CREATE OR REPLACE FUNCTION salescall.line_answer_counts(p_line uuid, p_from date, p_to date)"
     " RETURNS TABLE (day date, hour integer, dials integer, answers integer) LANGUAGE sql STABLE SECURITY DEFINER"
     " SET search_path = salescall, pg_temp AS $$ SELECT salescall.jst(a.claimed_at)::date,"
     " extract(hour FROM salescall.jst(a.claimed_at))::integer, count(*)::integer,"
     " count(*) FILTER (WHERE salescall.reached_answer(a.attempt_id))::integer FROM salescall.attempts a"
     " WHERE a.line_id = p_line AND a.tenant_id = salescall.cur_tenant() AND salescall.cur_client() IS NULL"
     " AND salescall.counts_per_call(salescall.keeping_kind(a.purpose, a.promised)) AND NOT salescall.is_active(a.state)"
     " AND salescall.jst(a.claimed_at)::date BETWEEN p_from AND p_to AND EXISTS (SELECT 1 FROM salescall.attempt_state_history h"
     " WHERE h.attempt_id = a.attempt_id AND h.to_state IN ('ringing', 'answered', 'busy', 'no_answer')) GROUP BY 1, 2 $$",
     ("tests.test_answer_rate.Counts.test_only_rung_calls_are_counted_and_only_for_that_contractor",)),
    ("ほかの依頼者の発信番号を、呼び出し中に入った発信だけで見る（0123 の前：呼び出し中を知らせずに出た回線の発信を見落とす）",
     "CREATE OR REPLACE FUNCTION salescall.line_called_number_elsewhere(p_line uuid, p_phone text) RETURNS boolean"
     " LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$ SELECT EXISTS (SELECT 1"
     " FROM salescall.attempts a WHERE a.line_id = p_line AND a.tenant_id = salescall.cur_tenant()"
     " AND a.phone_e164 = p_phone AND a.client_id IS DISTINCT FROM salescall.cur_client() AND salescall.is_dialed(a.state)"
     " AND EXISTS (SELECT 1 FROM salescall.attempt_state_history h WHERE h.attempt_id = a.attempt_id AND h.to_state = 'ringing')) $$",
     ("tests.test_gate.CallerNumberAndVoice.test_only_what_the_customer_saw_or_heard_counts",)),
    ("回線の1日の上限に、発信に失敗（鳴っていない）も数える（0109 の前の形。見直し17b：変異が無かった）",
     "CREATE OR REPLACE FUNCTION salescall.line_dials_on(p_line uuid, p_day date) RETURNS integer LANGUAGE sql STABLE"
     " SECURITY DEFINER SET search_path = salescall, pg_temp AS $$ SELECT count(*)::integer FROM salescall.attempts a"
     " WHERE a.line_id = p_line AND a.tenant_id = salescall.cur_tenant() AND salescall.is_dialed(a.state)"
     " AND salescall.jst(a.claimed_at)::date = p_day $$",
     ("tests.test_guards_1022.Guards.test_a_call_the_line_could_not_place_counts_nowhere",)),
    ("かけられる回線の一覧が、回線の停止だけを見る（0123 の前：契約者の緊急停止のあいだも、その回線に約束を割り当てる）",
     "CREATE OR REPLACE FUNCTION salescall.lines_serving() RETURNS TABLE (line_id uuid, client_id uuid, idle boolean)"
     " LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$ SELECT o.line_id, o.client_id,"
     " NOT EXISTS (SELECT 1 FROM salescall.attempts a WHERE a.line_id = o.line_id AND salescall.is_live(a.state, a.claim_expires_at))"
     " FROM salescall.lines o WHERE o.tenant_id = salescall.cur_tenant() AND o.retired_at IS NULL AND o.lease_expires_at > now()"
     " AND NOT EXISTS (SELECT 1 FROM salescall.open_stop_events s WHERE s.scope = 'line' AND s.line_id = o.line_id) $$",
     ("tests.test_gate.PromisesStopsAndConflicts.test_lines_under_a_contractor_wide_stop_do_not_serve_promises",)),
    # 見直し18（0128）
    ("回線が直った証拠に、ほかの回線で鳴って終わった電話も数える（見直し18a の (5)：壊しても試験が落ちなかった）",
     _edited("0124_unrung_numbers.sql", "CREATE OR REPLACE FUNCTION salescall.consecutive_errors(",
             "WHERE a.line_id = p_line AND a.ended_at IS NOT NULL", "WHERE a.ended_at IS NOT NULL", end="\n$$"),
     ("tests.test_watch.LineStops.test_a_call_that_rang_on_another_line_is_not_evidence",)),
    ("判定できない通話は、原因によらず、断られたかが決まっていないとみなす（0128 の前の形：時刻・理由の抜けで、同じお客様のほかの行を止める）",
     "CREATE OR REPLACE FUNCTION salescall.refusal_undecided(p_attempt uuid) RETURNS boolean LANGUAGE sql STABLE"
     " SET search_path = salescall, pg_temp AS $$ SELECT NOT salescall.is_settled(p_attempt)"
     " OR (salescall.unknown_pending(p_attempt)"
     " AND NOT coalesce((SELECT a.cut_off FROM salescall.attempts a WHERE a.attempt_id = p_attempt), false)) $$",
     ("tests.test_undecided_unknown.UnknownPending.test_an_unknown_call_whose_refusal_is_known_does_not_hold_the_customer",)),
    ("判定できない通話を、切れ方の材料が無いときも、断られたかが分かっているとみなす（原因ごとの印を読まない）",
     "CREATE OR REPLACE FUNCTION salescall.refusal_undecided(p_attempt uuid) RETURNS boolean LANGUAGE sql STABLE"
     " SET search_path = salescall, pg_temp AS $$ SELECT NOT salescall.is_settled(p_attempt) $$",
     ("tests.test_undecided_unknown.UnknownPending.test_an_unknown_call_that_was_not_cut_still_holds_the_customer",)),
    # 2026-10-24 ユーザー：「明らかに番号に問題がある場合は、その行は取りやめですが、回線の問題が明らかな場合は行は取りやめでなく
    # 一時中止だけ」（0129：鳴らなかった電話のせいを、同じ回線の前後の電話の証拠で決める）
    ("鳴らなかった電話を、違う番号が続けて鳴らなくても回線のせいにしない（0129 の前：壊れた回線で、お客様の行が「番号が使えない」で終わる）",
     _edited("0131_close_unrung_run_hold_apology_recording.sql", "CREATE OR REPLACE FUNCTION salescall.unrung_blame(", "> 1 THEN 'line'", "> 99 THEN 'line'", end="\n$$"),
     ("tests.test_settle.UnusableNumber.test_a_broken_line_does_not_make_numbers_unusable_on_its_own",
      "tests.test_supervisor.Blame.test_a_line_that_rejects_every_number_stops_and_loses_no_row")),
    ("鳴らなかった電話を、あとに回線が鳴らせた証拠が無くても番号のせいにする（回線が壊れた直後の番号を、取りやめうる）",
     _edited("0131_close_unrung_run_hold_apology_recording.sql", "CREATE OR REPLACE FUNCTION salescall.unrung_blame(", "WHEN hi.rang_at IS NOT NULL AND (hi.closed_at IS NULL OR hi.rang_at < hi.closed_at) THEN 'number'", "WHEN true THEN 'number'", end="\n$$"),
     ("tests.test_gate.Exit.test_driver_failures",
      "tests.test_settle.UnusableNumber.test_a_last_failure_that_turns_out_the_lines_does_not_end_the_row")),
    ("回線が元気だった証拠に、ほかの回線で鳴った電話も数える",
     _edited("0131_close_unrung_run_hold_apology_recording.sql", "CREATE OR REPLACE FUNCTION salescall.unrung_blame(", 'WHERE b.line_id = me.line_id AND b.claimed_at > me.claimed_at', 'WHERE b.claimed_at > me.claimed_at', end="\n$$"),
     ("tests.test_settle.UnusableNumber.test_the_blame_of_a_call_is_read_on_its_own_line_only",)),
    ("回線の続きを、鳴った電話で区切らない（前に鳴らなかった別の番号まで、回線のせいの証拠にする）",
     _edited("0131_close_unrung_run_hold_apology_recording.sql", "CREATE OR REPLACE FUNCTION salescall.unrung_blame(", "ORDER BY b.claimed_at DESC LIMIT 1), '-infinity'::timestamptz),", "ORDER BY b.claimed_at DESC LIMIT 0), '-infinity'::timestamptz),", end="\n$$"),
     ("tests.test_settle.UnusableNumber.test_the_run_on_the_line_is_cut_by_a_call_that_rang",)),
    ("番号の続きに、回線のせいの電話も数える（0129 の前：回線の故障で、番号が使えないと決まる）",
     _edited("0129_unrung_blame.sql", "CREATE FUNCTION salescall.failed_run(", "WHERE c.blame IN ('number', 'pending')))::integer,", 'WHERE c.blame IS NOT NULL))::integer,', end="\n$$"),
     ("tests.test_settle.UnusableNumber.test_a_broken_line_does_not_make_numbers_unusable_on_its_own",
      "tests.test_supervisor.Blame.test_a_line_that_rejects_every_number_stops_and_loses_no_row")),
    ("番号を取りやめる数に、まだ分からない電話も数える（回線のせいかもしれない番号を取りやめる）",
     _edited("0129_unrung_blame.sql", "CREATE FUNCTION salescall.failed_run(", "(count(*) FILTER (WHERE c.blame = 'number'))::integer", '(count(*) FILTER (WHERE c.blame IS NOT NULL))::integer', end="\n$$"),
     ("tests.test_settle.UnusableNumber.test_four_failures_follow_the_order_and_stop_the_number",)),
    ("回線の異常として記録済みの電話も、また数える（1本の失敗を何度も回線の異常に数える）",
     _edited("0129_unrung_blame.sql", "CREATE FUNCTION salescall.line_faults_unrecorded(", "AND NOT EXISTS (SELECT 1 FROM salescall.error_events e WHERE e.attempt_id = a.attempt_id AND e.source = 'line')", 'AND true', end="\n$$"),
     ("tests.test_supervisor.Rounds.test_a_call_that_could_not_be_placed_is_a_failed_dial_whose_blame_the_line_decides",)),
    ("番号のせい・まだ分からない電話も、回線の異常として数える（0129 の前：1つの番号のために回線が止まり続ける）",
     _edited("0129_unrung_blame.sql", "CREATE FUNCTION salescall.line_faults_unrecorded(", "AND salescall.unrung_blame(a.attempt_id) = 'line'", 'AND salescall.unrung_blame(a.attempt_id) IS NOT NULL', end="\n$$"),
     ("tests.test_supervisor.Blame.test_one_number_that_cannot_be_placed_does_not_starve_the_line",
      "tests.test_supervisor.Rounds.test_a_call_that_ends_before_the_answer_on_the_line_side_counts_as_a_line_fault_once_it_is_the_lines")),
    ("番号の聞き直しの予定に、伺い直す理由を求めない（0130 の前：会話が、別の方のお電話か繋がらない番号かを言い分けられない）",
     "ALTER TABLE salescall.task_next_actions DROP CONSTRAINT task_next_actions_number_again_reason",
     ("tests.test_alternate.WrongNumber.test_database_promises",)),
    ("使ったキーの識別名の形を見ない（0130：キーの値そのものが試行に残りうる）",
     "CREATE OR REPLACE FUNCTION salescall.keys_used_ok(p jsonb) RETURNS boolean LANGUAGE sql IMMUTABLE AS $$ SELECT true $$",
     ("tests.test_conversation_io.Materials.test_keys_used_keep_only_names_and_fingerprints",)),
    ("通話の進んだ時刻の守りを外す（0130：発信の時刻が入らず、後ろへ戻せ、終わった通話も進んだことにできる）",
     "DROP TRIGGER attempt_progress ON salescall.attempts",
     ("tests.test_conversation_io.Materials.test_progress_moves_forward_only_while_the_call_is_live",)),
    ("通話が進まなくなったと決して言わない（0130：固まった通話を、おわびして切れない）",
     _edited("0130_cl1_requests_1024.sql", "CREATE FUNCTION salescall.call_stalled(", "AND coalesce(a.progress_at, a.claimed_at) < now() - p_gap_seconds * interval '1 second'", "AND false", end="\n$$"),
     ("tests.test_conversation_io.Materials.test_progress_moves_forward_only_while_the_call_is_live",
      "tests.test_supervisor.Rounds.test_a_call_that_stops_progressing_is_ended_with_the_apology_and_a_long_one_is_not")),
    ("回線に進んでいる通話があっても、無いと答える（0130：止めるとき・通行証を止めたとき、話している通話を待たない）",
     _edited("0130_cl1_requests_1024.sql", "CREATE FUNCTION salescall.line_call_progressing(", "AND coalesce(a.progress_at, a.claimed_at) >= now() - p_gap_seconds * interval '1 second'", "AND false", end="\n$$"),
     ("tests.test_conversation_io.Materials.test_progress_moves_forward_only_while_the_call_is_live",)),
    # 2026-10-24 ユーザー：「一つの行で止まり続けることがないようにしてください」（0131：まだ分からない続きを閉じる）
    ("鳴らなかった電話のせいを、閉じた印で決めない（0131 の前：一覧の最後の1件が、分からないまま止まり続ける）",
     _edited("0131_close_unrung_run_hold_apology_recording.sql", "CREATE OR REPLACE FUNCTION salescall.unrung_blame(", "WHEN hi.closed_at IS NOT NULL THEN (SELECT c.verdict", "WHEN false THEN (SELECT c.verdict", end="\n$$"),
     ("tests.test_supervisor.Blame.test_the_last_row_on_the_line_does_not_wait_forever",
      "tests.test_supervisor.Blame.test_retiring_the_line_closes_its_run_as_the_lines")),
    ("まだ分からない続きは無い、と答える（0131 の前：閉じないので、一覧の最後の1件が止まり続ける）",
     _edited("0131_close_unrung_run_hold_apology_recording.sql", "CREATE FUNCTION salescall.line_run_pending(", "SELECT coalesce((SELECT count(DISTINCT b.phone_e164) = 1", "SELECT coalesce((SELECT false AND count(DISTINCT b.phone_e164) = 1", end="\n$$"),
     ("tests.test_supervisor.Blame.test_the_last_row_on_the_line_does_not_wait_forever",)),
    ("閉じた続きを、前に鳴った電話があっても回線のせいにする（番号のせいの番号を取りやめられず、かけ続ける）",
     _edited("0131_close_unrung_run_hold_apology_recording.sql", "CREATE FUNCTION salescall.close_unrung_run(", "OR last_ring > last_close.closed_at) THEN 'number'", "OR last_ring > last_close.closed_at) THEN 'line'"),
     ("tests.test_supervisor.Blame.test_a_line_that_rang_before_closes_the_last_rows_run_as_the_numbers",)),
    ("閉じた続きを、一度も鳴らせていない回線でも番号のせいにする（回線のせいかもしれない番号を取りやめる）",
     _edited("0131_close_unrung_run_hold_apology_recording.sql", "CREATE FUNCTION salescall.close_unrung_run(", "ELSE 'line' END;", "ELSE 'number' END;"),
     ("tests.test_supervisor.Blame.test_the_last_row_on_the_line_does_not_wait_forever",)),
    ("お詫びの録音の文を、案の文と比べない（0131：契約者が承認していない文を流しうる）",
     "DROP TRIGGER apology_recording_check ON salescall.recordings",
     ("tests.test_apology.Apology.test_the_apology_is_recorded_and_the_approval_binds_the_recording",)),
    ("お詫びの録音を、誤った通話と別の声でも作れる（0131：電話の途中で声がまじる）",
     "DROP TRIGGER apology_recording_set_check ON salescall.recording_sets",
     ("tests.test_apology.Apology.test_the_apology_is_recorded_and_the_approval_binds_the_recording",)),
    ("お詫びの録音の組を、2つそろわなくても承認できる（0131：あとの日の文の録音が無いまま流しうる）",
     "DROP TRIGGER apology_recording_approval_check ON salescall.recording_approvals",
     ("tests.test_apology.Apology.test_the_apology_is_recorded_and_the_approval_binds_the_recording",)),
    ("お詫びの電話の承認に、承認した・その案の録音の組を求めない（0131：聞いていない録音・ほかの案の録音で承認しうる）",
     "DROP TRIGGER apology_approval_recording ON salescall.apology_approvals",
     ("tests.test_apology.Apology.test_the_apology_is_recorded_and_the_approval_binds_the_recording",)),
)


def _fresh_db(superuser_dsn: str) -> tuple[str, str, str]:
    dbname = f"salescall_test_{uuid.uuid4().hex[:8]}"
    migrate.setup_roles(superuser_dsn)
    with psycopg.connect(superuser_dsn, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{dbname}" OWNER salescall_owner')
        conn.execute(f'CREATE DATABASE "{dbname}_restore" OWNER salescall_owner')   # 控えを戻して確かめる空のデータベース
    owner_dsn = _with_db(superuser_dsn, dbname, "salescall_owner")
    os.environ["SALESCALL_TEST_RESTORE_DSN"] = _with_db(superuser_dsn, f"{dbname}_restore", "salescall_owner")
    migrate.apply(owner_dsn)
    migrate.finish_roles(superuser_dsn)   # 本番と同じく、流し終えたら前の表の作りのための権限を外す
    os.environ["SALESCALL_TEST_ADMIN_DSN"] = superuser_dsn
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
                    overloads = _overloads(conn)
                    conn.execute(sql)
                    added = sorted(set(_overloads(conn).items()) - set(overloads.items()))
                # 守りを「置き換え」ずに、同じ名前の関数を足してしまった壊し試しは、何も外していない（呼ぶ側が「関数が1つに決まらない」
                # で落ち、見張れているように見える＝2026-10-21 の見直しで見つけた）。⇒ 試験を走らせずに、壊し試しの誤りとして止める
                caught = not added and not _run_tests(owner_dsn, app_dsn, tests, 0)
            finally:
                _drop_db(superuser_dsn, dbname)
            if added:
                print(f"守りを外す：{title} ⇒ ⛔ 壊し試しが、今の関数を置き換えずに同じ名前の関数を足した（形がずれている：{added}）")
            else:
                print(f"守りを外す：{title} ⇒ {'試験が落ちた（見張れている）' if caught else '⛔ 試験が通ってしまった'}")
            if not caught:
                weak.append(title)
        if weak:
            print("⛔ 守りを外しても落ちない試験がある：", "、".join(weak))
            return 1
        print("守りをわざと外す確かめ：すべて、試験が落ちた")
        return 0


def _overloads(conn: psycopg.Connection) -> dict[str, int]:
    """表の作りの場所の関数の、名前ごとの数（壊し試しが関数を置き換えたか、足してしまったかを見分ける）。"""
    return {r[0]: r[1] for r in conn.execute(
        "SELECT p.proname, count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
        " WHERE n.nspname = 'salescall' GROUP BY 1").fetchall()}


@contextmanager
def _given(dsn: str) -> Iterator[str]:
    yield dsn


if __name__ == "__main__":
    raise SystemExit(main())
