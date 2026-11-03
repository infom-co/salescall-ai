"""app/ の試験を、1つの命令で全部動かす（CL-1 の条件 (h)）。持ち主 CL-2。

使い方（リポジトリの場所で）：  python app/run_tests.py

順番：
  1. 決まりの機械の見張り（app/checks。DESIGN.md 14章）
  2. 試験用のデータベースを用意し、表の作りを流す
  3. app/tests の試験を全部
  4. 壊し試し：データベースの守り（MUTATIONS）と Python の判断（PY_MUTATIONS。app の写しで）を1つずつ外し、その試験が落ちるか

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
def _throwaway_cluster(port: str = "54329", clock: Path | None = None) -> Iterator[str]:
    """使い捨てのデータベースを立て、管理者の接続文字列を返す。終わったら止めて消す。clock＝データベースの時計をずらすファイル
    （中身は「いまから何秒ずらすか」＝例 +3600。libfaketime をデータベースの処理にだけ差し込む＝`clock_cluster`）。"""
    bindir = _pg_bindir()
    tmp = Path(tempfile.mkdtemp(prefix="salescall-pg-"))
    as_root = hasattr(os, "geteuid") and os.geteuid() == 0
    prefix = ["runuser", "-u", "postgres", "--"] if as_root else []
    if as_root:
        shutil.chown(tmp, "postgres", "postgres")
    data, sock = tmp / "data", tmp
    env = []
    if clock is not None:
        lib = faketime_library()
        env = ["env", f"LD_PRELOAD={lib}", f"FAKETIME_TIMESTAMP_FILE={clock}", "FAKETIME_NO_CACHE=1",
               "FAKETIME_DONT_FAKE_MONOTONIC=1", "DONT_FAKE_MONOTONIC=1", "FAKETIME_DONT_RESET=1"]
    subprocess.run(prefix + [str(bindir / "initdb"), "-D", str(data), "-U", "postgres", "--auth=trust",
                             "--encoding=UTF8", "--locale=C", "-N"], check=True, capture_output=True)
    subprocess.run(prefix + env + [str(bindir / "pg_ctl"), "-D", str(data), "-w", "-l", str(tmp / "log"), "-o",
                                   f"-c listen_addresses='' -k {sock} -p {port} -c fsync=off", "start"],
                   check=True, capture_output=True)
    try:
        yield f"host={sock} port={port} user=postgres dbname=postgres"
    finally:
        subprocess.run(prefix + [str(bindir / "pg_ctl"), "-D", str(data), "-m", "immediate", "stop"], capture_output=True)
        shutil.rmtree(tmp, ignore_errors=True)


FAKETIME_PLACES = ("/usr/lib/x86_64-linux-gnu/faketime/libfaketime.so.1", "/usr/lib/aarch64-linux-gnu/faketime/libfaketime.so.1",
                   "/usr/lib/faketime/libfaketime.so.1", "/usr/local/lib/faketime/libfaketime.so.1")


def faketime_library() -> Path | None:
    """データベースの時計をずらす部品（libfaketime。Debian・Ubuntu の faketime の部品）の場所。無ければ None。"""
    return next((Path(x) for x in FAKETIME_PLACES if Path(x).exists()), None)


@contextmanager
def clock_cluster() -> Iterator[tuple[str, Path]]:
    """時計を進められる使い捨てのデータベースを立て、（管理者の接続文字列, 時計のファイル）を返す（電話の一生の試験＝tests/test_life.py
    だけが使う。ほかの試験は時計をずらさない、ふつうのデータベースで流す）。時計のファイルに「いまから何秒ずらすか」を書くと、データベースの
    now()・clock_timestamp() がその時刻になる（Python の時計はずらさない）。部品が無ければ RuntimeError。"""
    if faketime_library() is None:
        raise RuntimeError("データベースの時計をずらす部品（libfaketime。apt の libfaketime）がありません")
    folder = Path(tempfile.mkdtemp(prefix="salescall-clock-"))
    clock = folder / "clock"
    clock.write_text("+0\n")
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        shutil.chown(folder, "postgres", "postgres")
        os.chmod(clock, 0o666)
    try:
        with _throwaway_cluster("54339", clock) as dsn:
            yield dsn, clock
    finally:
        shutil.rmtree(folder, ignore_errors=True)


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


def _view_edited(migration: str, start: str, old: str, new: str) -> str:
    """壊し試し用に、表の作りの1つの見方（start から最初の「;」まで）の old を new に替えた形を返す（`_edited` の見方の版。見方を
    写して持たない）。"""
    return _edited(migration, start, old, new, end=";").replace("CREATE VIEW", "CREATE OR REPLACE VIEW", 1)


# データベースの守りを、わざと1つずつ外す。外したら、その守りを試す試験が本当に落ちることを確かめる（DESIGN.md 14章：
# 落ちることを確かめていない見張りは、見張っていないのと同じ）。守りを足したら、ここにも足す。
_M139 = "0139_ring_hours_gaps_and_our_troubles.sql"
_M172 = "0172_approval_belongs_to_content.sql"
_CONTENT = "tests.test_gate.CallerNumberAndVoice.test_an_approval_belongs_to_the_content_of_the_recording"
_VOICE_COMMON = "tests.test_gate.CallerNumberAndVoice.test_a_voice_is_used_only_when_its_common_set_holds_every_common_part"
_T139 = "tests.test_troubles.OurTroubles."
_M141 = "0141_review20a_hold_runs_and_calls.sql"
_M145, _TH = "0145_health_states_shadow.sql", "tests.test_health."
_M155, _TA = "0155_ai_trouble_pause.sql", "tests.test_ai_troubles."
_M159 = "0159_review25_one_place_for_ai_stops_and_kept_calls.sql"
_M170 = "0170_no_raw_ai_error_stops.sql"
_M161, _TR = "0161_review26b_decisions_releases_waits.sql", "tests.test_irreversible.Review26."
_M146 = "0146_health_states_read.sql"
_M147 = "0147_stage5_remove_old_reads.sql"
_B141 = "tests.test_supervisor.Blame."
_M143 = "0143_review21_one_way_and_call_watch.sql"
_T143 = "tests.test_life.Life."
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
    ("前の発信の締めが済んでいない行を、門が通す・候補に出す（締めを待つ行の1か所＝salescall.task_unsettled）",
     _edited(_M146, "CREATE FUNCTION salescall.task_unsettled(", "AND NOT salescall.is_settled(a.attempt_id)", "AND false", end="\n$$"),
     ("tests.test_gate.Pacing.test_unsettled_attempt_blocks_the_row",)),
    ("まだ分からない電話の行を、どの回線でも待たせる（0143 の形：回線 A が壊れると、元気な回線 B も、その行・約束の電話にかけられない"
     "＝見直し22 の 5・場面 n2b）",
     _edited(_M146, "CREATE FUNCTION salescall.task_unsettled(", "AND NOT (p_line IS NOT NULL AND EXISTS (", "AND NOT (false AND EXISTS (",
             end="\n$$"),
     (_B141 + "test_a_row_left_undecided_on_one_line_waits_only_on_that_line",)),
    ("通話後の確かめを「確かめられなかった」で諦められる",
     "ALTER TABLE salescall.attempts DROP CONSTRAINT attempts_verification_never_gives_up",
     ("tests.test_foundation.AttemptTransitions.test_verification_only_pending_to_done",
      "tests.test_settle.Settle.test_a_verification_that_cannot_run_waits_and_never_ends_the_row")),
    # 見守り（Q9・9-1）
    ("止まったAIを数えない（いつも0。AIの不具合で契約者を止める数え方。0170）",
     "CREATE OR REPLACE FUNCTION salescall.errors_since(p_kinds text[], p_cause text,"
     " p_from timestamptz DEFAULT NULL) RETURNS integer LANGUAGE sql STABLE SECURITY DEFINER"
     " SET search_path = salescall, pg_temp AS $$ SELECT 0 $$",
     ("tests.test_watch.TenantAiStopsAndChecks.test_one_contractors_stopped_ai_stops_only_that_contractor",)),
    ("エラーの数を、範囲の外の取引にも見せる",
     "CREATE OR REPLACE FUNCTION salescall.errors_since(p_kinds text[], p_cause text,"
     " p_from timestamptz DEFAULT NULL) RETURNS integer LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$"
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
     # ⛔ 5段目の前：回線の停止の説明の試験を見ていた（回線のエラーは、もう回線の停止に数えない＝0147）。数えたエラーを出すのは、いまは
     # AIの不具合で止めた停止（0170 から、止まったAIだけ）
     ("tests.test_watch.TenantAiStopsAndChecks.test_the_contractor_can_resume",)),
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
     _edited(_M146, "CREATE OR REPLACE FUNCTION salescall.lines_serving()",
             "NOT EXISTS (SELECT 1 FROM salescall.attempts a WHERE a.line_id = o.line_id AND salescall.is_live(a.state, a.claim_expires_at))",
             "true", end="\n$$"),
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
    ("組がそろったかで、その版の読みと合うかを見ない（0172：読みを選び直しても、前の読みの録音の組を流す）",
     _edited(_M172, "CREATE FUNCTION salescall.set_ready(", "\n     AND salescall.recording_set_fits(p_set)", "", end="\n$$"),
     ("tests.test_gate.CallerNumberAndVoice.test_a_reading_chosen_by_listening_to_recordings_becomes_the_name_reading",)),
    ("部分の承認を、その組を承認したかだけで決める（0172 の前の形：版が変わると、同じ中身まで聞き直す）",
     "CREATE OR REPLACE VIEW salescall.part_states WITH (security_invoker = true) AS SELECT k.set_id, k.part,"
     " EXISTS (SELECT 1 FROM salescall.recording_approvals p WHERE p.set_id = k.set_id) AS approved FROM salescall.part_contents k",
     (_CONTENT,)),
    ("同じ中身の承認を、持ち主の違う部分にも使う（0172：契約者の持ち物の録音の承認で、依頼者の業務の録音を流す）",
     _view_edited(_M172, "CREATE VIEW salescall.part_states", " AND a.client_id IS NOT DISTINCT FROM s.client_id", ""),
     (_CONTENT,)),
    ("同じ中身の承認を、ほかの声の部分にも使う（0172：聞いていない声の録音を流す）",
     _view_edited(_M172, "CREATE VIEW salescall.part_states", "a.voice_name = s.voice_name AND ", ""),
     (_CONTENT,)),
    ("同じ文なら、音声の中身が違っても承認済みにする（0172：聞いていない音声を流す）",
     _edited(_M172, "CREATE FUNCTION salescall.content_of(", "chr(31) || r.audio_sha256 || ", "", end="\n$$"),
     (_CONTENT,)),
    ("部分の無い組もそろったとする（0172：流す録音の無い組が、いまの組になる）",
     _edited(_M172, "CREATE FUNCTION salescall.set_ready(", "SELECT EXISTS (SELECT 1 FROM salescall.recordings WHERE set_id = p_set)\n     AND ",
             "SELECT ", end="\n$$"),
     (_CONTENT,)),
    ("読みを足しても、部分の中身の鍵を作り直さない（0172：選んだ読みの違う録音を、聞かずにそろったとする）",
     "DROP TRIGGER part_contents_put ON salescall.recording_readings",
     ("tests.test_gate.CallerNumberAndVoice.test_a_reading_chosen_by_listening_to_recordings_becomes_the_name_reading",)),
    ("組を承認しても、承認した中身の台帳に書かない（0172：聞いて承認した録音が、いつまでもそろわない）",
     "DROP TRIGGER approved_contents_add ON salescall.recording_approvals",
     (_CONTENT,)),
    ("録音の組を作った時刻を、書く側が渡せる（0172：未来の時刻の組が、いつまでもいまの組になる）",
     "DROP TRIGGER stamp_now ON salescall.recording_sets",
     (_CONTENT,)),
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
    # 見直し17a（0122）：回線が直った証拠
    # 2026-10-23 ユーザー：お客様が出る前に毎回終わる番号も「番号を受け付けないときと同じ」（0124）
    ("お客様のところで鳴ったか（rang）が、お客様が出る前に終わった電話も鳴ったとみなす",
     "CREATE OR REPLACE FUNCTION salescall.rang(s salescall.attempt_state, p_cut_off boolean, p_attempt uuid) RETURNS boolean"
     " LANGUAGE sql STABLE AS $$ SELECT salescall.is_placed(s) $$",
     ("tests.test_settle.UnusableNumber.test_a_call_that_rang_restarts_the_run",)),
    # 見直し17b（全部の試験でも落ちなかった壊し方に、試験を足した）
    ("これからかける約束の一覧（回線の取り置き）が「強制かけない」の行も入れる",
     "CREATE OR REPLACE FUNCTION salescall.promises_waiting()\nRETURNS TABLE (task_id uuid, client_id uuid, call_at timestamptz, callee_at timestamptz, phone_e164 text)\nLANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$\n  SELECT n.task_id, n.client_id, n.call_at, n.callee_at, k.phone_e164\n  FROM salescall.task_next_actions n\n  JOIN salescall.tasks t ON t.task_id = n.task_id\n  JOIN salescall.runs r ON r.run_id = t.run_id\n  JOIN salescall.task_numbers k ON k.task_id = n.task_id AND k.contact_id = n.contact_id\n  WHERE n.tenant_id = salescall.cur_tenant() AND n.promised AND n.kind = 'call'\n    AND n.seq = (SELECT c.seq FROM salescall.current_next_actions c WHERE c.task_id = n.task_id)\n    AND salescall.client_active(n.client_id)\n    AND NOT EXISTS (SELECT 1 FROM salescall.blocking_stops(n.tenant_id, n.client_id, r.business_id, NULL,\n                                                           salescall.keeping_kind(n.purpose, n.promised)))\n    AND NOT EXISTS (SELECT 1 FROM salescall.attempts a\n                    WHERE a.task_id = n.task_id AND salescall.is_live(a.state, a.claim_expires_at))\n$$;",
     ("tests.test_gate.PromisesStopsAndConflicts.test_a_promise_of_a_row_marked_never_call_is_not_kept_for",)),
    ("赤字の警告を「赤字で止めた（stopped）」で閉じられる（0123 の前：赤字では止めないのに、止めたと記録できる）",
     "ALTER TABLE salescall.deficit_warnings DROP CONSTRAINT deficit_warnings_closed_why_check;"
     " ALTER TABLE salescall.deficit_warnings ADD CONSTRAINT deficit_warnings_closed_why_check"
     " CHECK (closed_why IN ('not_deficit', 'confirm_expired', 'stopped'))",
     ("tests.test_usage.CostRatio.test_a_confirmed_deficit_is_not_told_again_while_it_lasts_and_a_new_deficit_warns_again",)),
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
     _edited(_M146, "CREATE OR REPLACE FUNCTION salescall.lines_serving()",
             "AND NOT EXISTS (SELECT 1 FROM salescall.open_stops(o.tenant_id, NULL, NULL, o.line_id))",
             "AND NOT EXISTS (SELECT 1 FROM salescall.open_stop_events s WHERE s.scope = 'line' AND s.line_id = o.line_id)", end="\n$$"),
     ("tests.test_gate.PromisesStopsAndConflicts.test_lines_under_a_contractor_wide_stop_do_not_serve_promises",)),
    # 見直し18（0128）
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
    ("番号の聞き直しの予定に、伺い直す理由を求めない（0130 の前：会話が、別の方のお電話か繋がらない番号かを言い分けられない）",
     "ALTER TABLE salescall.task_next_actions DROP CONSTRAINT task_next_actions_number_again_reason",
     ("tests.test_alternate.WrongNumber.test_database_promises",)),
    ("使ったキーの識別名の形を見ない（0130：キーの値そのものが試行に残りうる）",
     "CREATE OR REPLACE FUNCTION salescall.keys_used_ok(p jsonb) RETURNS boolean LANGUAGE sql IMMUTABLE AS $$ SELECT true $$",
     ("tests.test_conversation_io.Materials.test_keys_used_keep_only_names_and_fingerprints",)),
    # 2026-10-24 ユーザー：「一つの行で止まり続けることがないようにしてください」（0131：まだ分からない続きを閉じる）
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
    ("お詫びの案の文の守りを、確かめ済みにしない（0137 の前：0131 より前の、伝える文の無い案が残りうる）",
     "ALTER TABLE salescall.apology_orders DROP CONSTRAINT apology_orders_texts,"
     " ADD CONSTRAINT apology_orders_texts CHECK (text_same_day IS NOT NULL AND text_later IS NOT NULL) NOT VALID",
     ("tests.test_apology.Apology.test_the_apology_is_recorded_and_the_approval_binds_the_recording",)),
    ("お詫びの電話の承認の録音の守りを、確かめ済みにしない（0137 の前：0131 より前の、録音の無い承認が残りうる）",
     "ALTER TABLE salescall.apology_approvals DROP CONSTRAINT apology_approvals_recorded,"
     " ADD CONSTRAINT apology_approvals_recorded CHECK (set_id IS NOT NULL) NOT VALID",
     ("tests.test_apology.Apology.test_the_apology_is_recorded_and_the_approval_binds_the_recording",)),
    # 2026-10-24 ユーザー：「『営業部部長の〇〇様は』も『営業部部長』『〇〇様』『は』に分けて録音しておいた方が汎用性があるんじゃないですか？」
    # （0134：名前の録音の台帳を、役職の部分・名前の部分・助詞から始まる続く言葉の3つに）
    ("名前の録音の台帳の文を、鍵と突き合わせない（0137：別の読み・別の部署の録音で、お客様のお名前を呼びうる）",
     _edited("0137_name_ledger_cuts_undecided.sql", "CREATE OR REPLACE FUNCTION salescall.name_recording_check()",
             "  IF btrim(NEW.spoken_text) = '' OR NOT (CASE s.unit", "  IF false AND (CASE s.unit"),
     ("tests.test_gate.NameLedger.test_the_database_keeps_the_ledger_in_shape",)),
    ("名前の録音の台帳の組の部分の名前を見ない（0137：名前の部分の組に別の一言を入れうる）",
     _edited("0137_name_ledger_cuts_undecided.sql", "CREATE OR REPLACE FUNCTION salescall.name_recording_check()",
             "  IF NEW.part IS DISTINCT FROM (CASE s.unit", "  IF false AND NEW.part IS DISTINCT FROM (CASE s.unit"),
     ("tests.test_gate.NameLedger.test_the_database_keeps_the_ledger_in_shape",)),
    ("名前の録音の台帳の文の切れ目を、また決め打ちする（0137 の前の 0134：続く言葉は助詞から・役職の部分は「の」で終わる。CL-1 の G-65 と"
     "ユーザーの判断の前に、正しい録音を断りうる）",
     _edited("0137_name_ledger_cuts_undecided.sql", "CREATE OR REPLACE FUNCTION salescall.name_recording_check()",
             "WHEN 'name_role' THEN strpos(NEW.spoken_text, s.name_role) > 0",
             "WHEN 'name_role' THEN NEW.spoken_text = s.name_role || 'の'"),
     ("tests.test_gate.NameLedger.test_the_ledger_does_not_fix_where_the_cuts_are",)),
    ("名前の録音の台帳の鍵の決まりを外す（0134：読みと役職の両方を持つ名前の部分・役職の部分に読み・契約者の組に鍵）",
     "ALTER TABLE salescall.recording_sets DROP CONSTRAINT recording_sets_name_key",
     ("tests.test_gate.NameLedger.test_the_database_keeps_the_ledger_in_shape",)),
    ("台帳のいまの録音に、承認していない組も入れる（0132：契約者が聞いていない録音で、お客様のお名前を呼ぶ）",
     "DROP VIEW salescall.current_name_recordings; "
     + _edited("0134_test_ring_trouble_system_names.sql", "CREATE VIEW salescall.current_name_recordings",
               "FROM salescall.recording_sets s JOIN salescall.recording_approvals p USING (set_id)",
               "FROM salescall.recording_sets s LEFT JOIN salescall.recording_approvals p USING (set_id)", end="TO salescall_app;"),
     ("tests.test_gate.NameLedger.test_a_row_is_called_only_in_a_voice_whose_name_recordings_are_all_approved",)),
    # 2026-10-24 夜のご決定（0134）：テスト番号・甚大な問題の電話を終える・名前の録音の台帳のシステムの単位
    ("テスト番号の記録を書き換えられる（0134：鳴らなかったテストを鳴ったことにできる）",
     "DROP TRIGGER line_ring_tests_frozen ON salescall.line_ring_tests",
     ("tests.test_supervisor.Blame.test_a_phone_part_that_fails_while_ringing_the_test_number_is_unknown_not_silent",)),
    ("テスト番号の値の形を見ない（0134：そろえていない番号へテストをかけうる）",
     _edited(_M139, "CREATE OR REPLACE FUNCTION salescall.setting_value_ok(", "WHEN 'phone' THEN jsonb_typeof(v) = 'null' OR (jsonb_typeof(v) = 'string' AND (v #>> '{}') ~ '^\\+[1-9][0-9]{7,14}$')", "WHEN 'phone' THEN true", end="\n$$"),
     ("tests.test_settings.Shapes.test_bad_values_are_rejected",)),
    ("通話を終えた理由を、あとで変えられる・ほかの状態にも付けられる（0134：甚大な問題の記録が書き換わる）",
     "DROP TRIGGER attempt_trouble_fixed ON salescall.attempts",
     ("tests.test_supervisor.Rounds.test_a_call_with_a_grave_problem_is_ended_with_the_apology_and_called_back",)),
    ("通話を終えた理由に、決まった5つのほかを書ける（0134・0136）",
     "ALTER TABLE salescall.attempts DROP CONSTRAINT attempts_trouble_check",
     ("tests.test_supervisor.Rounds.test_a_call_with_a_grave_problem_is_ended_with_the_apology_and_called_back",)),
    ("システムの録音（名前の録音・共通の一言）を、管理者でなくても書ける（0134・0171：システムの処理が、聞いていない録音を全契約者に流しうる）",
     _edited("0171_common_recordings_are_system.sql", "CREATE OR REPLACE FUNCTION salescall.system_recording_check()", "IF NEW.tenant_id IS NULL AND salescall.cur_actor_kind() IS DISTINCT FROM 'admin' THEN", "IF false THEN"),
     ("tests.test_gate.NameLedger.test_only_the_admin_writes_the_systems_names",)),
    ("システムの単位の名前の録音を、契約者の取引からも書ける（0134：1つの契約者が、ほかの契約者の流す録音を変えうる）",
     "DROP POLICY system_names_write ON salescall.recording_sets; CREATE POLICY system_names_write ON salescall.recording_sets"
     " FOR INSERT WITH CHECK (tenant_id IS NULL)",
     ("tests.test_gate.NameLedger.test_only_the_admin_writes_the_systems_names",)),
    ("システムの単位の組に、契約者の取引からも部分を足せる（0134：1つの契約者が、ほかの契約者の流す録音を変えうる）",
     "DROP POLICY system_names_write ON salescall.recordings; CREATE POLICY system_names_write ON salescall.recordings"
     " FOR INSERT WITH CHECK (tenant_id IS NULL)",
     ("tests.test_gate.NameLedger.test_only_the_admin_writes_the_systems_names",)),
    ("システムの単位の承認を、契約者の取引からもできる（0134）",
     "DROP POLICY system_names_write ON salescall.recording_approvals; CREATE POLICY system_names_write"
     " ON salescall.recording_approvals FOR INSERT WITH CHECK (tenant_id IS NULL)",
     ("tests.test_gate.NameLedger.test_only_the_admin_writes_the_systems_names",)),
    ("システムの単位を、名前の録音の台帳と共通の一言のほかの組（業務の録音）にも作れる（0134・0171：依頼者の録音の組をすべての"
     "契約者に流しうる）",
     "ALTER TABLE salescall.recording_sets DROP CONSTRAINT recording_sets_system_units",
     ("tests.test_gate.NameLedger.test_only_the_admin_writes_the_systems_names",)),
    ("共通の一言の組を、契約者の行にも作れる（0171：契約者ごとに同じ録音を作り直して聞き直す形に戻る）",
     "ALTER TABLE salescall.recording_sets DROP CONSTRAINT recording_sets_common_is_system",
     (_VOICE_COMMON,)),
    ("システムの単位の名前の録音を、契約者の取引から読めない（0134 の前：契約者ごとに同じ名字を録り直す）",
     "DROP POLICY system_names_read ON salescall.recording_sets",
     ("tests.test_gate.NameLedger.test_the_systems_names_serve_every_contractor_and_the_contractors_own_come_first",)),
    ("システムの単位の名前の録音の部分を、契約者の取引から読めない（0134：呼べると数えても、流す録音が無い）",
     "DROP POLICY system_names_read ON salescall.recordings",
     ("tests.test_gate.NameLedger.test_the_systems_names_serve_every_contractor_and_the_contractors_own_come_first",)),
    ("システムの単位の承認を、契約者の取引から読めない（0134：承認したシステムの録音を使えない）",
     "DROP POLICY system_names_read ON salescall.recording_approvals",
     ("tests.test_gate.NameLedger.test_the_systems_names_serve_every_contractor_and_the_contractors_own_come_first",)),
    ("台帳のいまの録音で、システムのものを契約者が承認したものより先にする（0134：契約者が聞いて選び直した録音を流さない）",
     "DROP VIEW salescall.current_name_recordings; "
     + _edited("0134_test_ring_trouble_system_names.sql", "CREATE VIEW salescall.current_name_recordings",
               "(s.tenant_id IS NULL), p.approved_at DESC", "(s.tenant_id IS NOT NULL), p.approved_at DESC", end="TO salescall_app;"),
     ("tests.test_gate.NameLedger.test_the_systems_names_serve_every_contractor_and_the_contractors_own_come_first",)),
    ("テスト番号を鳴らしてよい時間の初めの値を 0〜24時にする（0135：いまはユーザー自身の電話なので、夜中に鳴らしうる）",
     "INSERT INTO salescall.setting_values (key, scope, value, status, rationale, review_when, set_by)"
     " VALUES ('line.test_ring_hours', 'system', '[[\"00:00\", \"24:00\"]]', 'decided', '壊し試し', '壊し試し', '壊し試し')",
     ("tests.test_supervisor.Blame.test_the_test_ring_hours_are_the_contractors_any_number_of_ranges_and_start_at_7_to_22",)),
    ("テスト番号を鳴らしてよい時間を、契約者ごとに変えられない（0135：この仕事専用の電話に替えても、夜に鳴らせない）",
     "ALTER TABLE salescall.setting_definitions DISABLE TRIGGER setting_definitions_frozen;"
     " UPDATE salescall.setting_definitions SET finest_scope = 'system' WHERE key = 'line.test_ring_hours';"
     " ALTER TABLE salescall.setting_definitions ENABLE TRIGGER setting_definitions_frozen",
     ("tests.test_supervisor.Blame.test_the_test_ring_hours_are_the_contractors_any_number_of_ranges_and_start_at_7_to_22",)),
    # 見直し19（0136）：こちらのせい・使わなくした回線・前の版のデータ・決めにいく判断・テスト番号の鳴らし方
    ("番号を伺い直す理由の無い前の予定を読み替えない（0136 の前：門は空のまま予約証へ写した）",
     _edited("0136_review19_lines_numbers.sql", "CREATE FUNCTION salescall.number_again_of(", "CASE WHEN p_purpose = 'ask_number_again' THEN 'wrong_party' END", "NULL::text", end="\n$$"),
     ("tests.test_gate.Exit.test_old_plans_without_a_reason_to_ask_the_number_again_read_as_wrong_party",)),
    # 2026-10-25 の管理者の決定（0139）：テスト番号を鳴らしてよい時間を何か所でも・あける長さの並び・こちらの不具合を行だけか全体かで見極める
    ("テスト番号を鳴らしてよい時間の範囲どうしの重なりを断らない（0139：同じ時間を2つ書いても通る＝値の形の確かめの1か所が働かない）",
     _edited(_M139, "CREATE OR REPLACE FUNCTION salescall.setting_value_ok(", "ELSE NOT EXISTS (SELECT 1 FROM salescall.daily_range_spans(v) x", "ELSE true OR NOT EXISTS (SELECT 1 FROM salescall.daily_range_spans(v) x", end="\n$$"),
     ("tests.test_supervisor.Blame." + "test_the_test_ring_hours_are_the_contractors_any_number_of_ranges_and_start_at_7_to_22",)),
    ("テスト番号を鳴らしてよい時間を、空の並びでも通す（0139：いつも時間の外になり、テスト番号で決めなくなる）",
     _edited(_M139, "CREATE OR REPLACE FUNCTION salescall.setting_value_ok(", "WHEN jsonb_array_length(v) < 1 THEN false\n                                  WHEN EXISTS (SELECT 1 FROM jsonb_array_elements(v) r", "WHEN false THEN false\n                                  WHEN EXISTS (SELECT 1 FROM jsonb_array_elements(v) r", end="\n$$"),
     ("tests.test_supervisor.Blame." + "test_the_test_ring_hours_are_the_contractors_any_number_of_ranges_and_start_at_7_to_22",)),
    ("日をまたぐ範囲の朝の側を、重なりの確かめに入れない（0139：22時〜2時と1時〜3時の重なりを見のがす）",
     _edited(_M139, "CREATE FUNCTION salescall.daily_range_spans(", "UNION ALL SELECT r.i, 0, r.b FROM r WHERE r.a > r.b AND r.b > 0", "UNION ALL SELECT r.i, 0, r.b FROM r WHERE false", end="\n$$"),
     ("tests.test_supervisor.Blame." + "test_the_test_ring_hours_are_the_contractors_any_number_of_ranges_and_start_at_7_to_22",)),
    ("こちらのせいで途中で終えた電話も、お客様の側で続けて切れた数に入れる（0120 の形：3回目からを行の回数に数える）",
     _edited(_M141, "CREATE OR REPLACE FUNCTION salescall.cuts_in_a_row(", "    AND NOT salescall.trouble_ours(b.trouble)\n", "", end="\n$$"),
     ("tests.test_settle.CutOffCalls.test_our_own_troubles_neither_count_nor_restart_the_customers_cuts_and_are_always_called_back",)),
    # 0147 で、数え方の種類は「途中で切れた」をすぐのかけ直しの1か所（resume_allowed）で読む形に寄せた。数え方の種類が自分で数え直す
    # （is_ours を持たない前の式に戻す）と、こちらのせいの電話を数える
    ("お客様の側で切れた続きのあとの、こちらのせいで途中で終えた電話を、行の回数に数える（数え方の種類が resume_allowed を読まず、"
     "is_ours を持たない式で自分で数える）",
     _edited(_M147, "CREATE OR REPLACE FUNCTION salescall.dial_kind(", "AND salescall.resume_allowed(p_attempt) THEN 'cut_off'",
             "AND salescall.cuts_in_a_row(p_attempt) <= salescall.resume_in_a_row() THEN 'cut_off'", end="\n$$"),
     ("tests.test_settle.CutOffCalls.test_our_own_troubles_neither_count_nor_restart_the_customers_cuts_and_are_always_called_back",)),
    ("お客様の側で切れた続きのあとの、こちらのせいで途中で終えた電話を、お詫びとともにかけ直さない（resume_allowed の is_ours を外す）",
     _edited(_M139, "CREATE OR REPLACE FUNCTION salescall.resume_allowed(", "AND (salescall.is_ours(p_attempt) OR salescall.cuts_in_a_row(p_attempt)", "AND (salescall.cuts_in_a_row(p_attempt)", end="\n$$"),
     ("tests.test_settle.CutOffCalls.test_our_own_troubles_neither_count_nor_restart_the_customers_cuts_and_are_always_called_back",)),
    ("休んだ行・復旧待ちの行を、システムの処理も戻せる（0139：管理者・契約者だけが戻す、に反する）",
     _edited(_M146, "CREATE OR REPLACE FUNCTION salescall.task_trouble_restore_guard()",
             "IF salescall.cur_actor_kind() IS DISTINCT FROM 'operator' AND salescall.cur_actor_kind() IS DISTINCT FROM 'admin' THEN",
             "IF false THEN"),
     (_T139 + "test_a_contractor_or_the_admin_can_put_a_resting_row_back_and_our_calls_never_count",
      _TH + "Guards.test_a_row_awaiting_recovery_can_be_put_back_by_a_person_only")),
    ("休んでいない・復旧を待っていない行にも、戻した記録を作れる（戻した時が数え始めになり、休みが黙ってずれる）",
     _edited(_M146, "CREATE OR REPLACE FUNCTION salescall.task_trouble_restore_guard()",
             "IF NOT EXISTS (SELECT 1 FROM salescall.task_states s WHERE s.task_id = NEW.task_id AND s.stage IN ('resting', 'await_recovery')) THEN",
             "IF false THEN"),
     (_TH + "Guards.test_a_row_awaiting_recovery_can_be_put_back_by_a_person_only",)),
    ("同じプログラムの版でも、立ち上げ直す・前の版に戻すたびに「版が替わった」とする（直していないのに、休んだ行が戻り、止めた契約者が解ける）",
     _edited(_M139, "CREATE FUNCTION salescall.note_program(", "IF EXISTS (SELECT 1 FROM salescall.program_versions WHERE version = p_version) THEN", "IF false THEN")
     + "; DROP INDEX salescall.program_versions_once",
     (_T139 + "test_the_running_program_version_is_noted_once_per_change",)),
    # 0141（見直し20a の (8)〜(11)。2026-10-25 夜 管理者の決定 案A）
    ("「声が聞き取れない」を、こちらの不具合に数える（行を休ませ・止め・修正依頼を出し、途中で切れた数に入れない）",
     _edited(_M143, "CREATE OR REPLACE FUNCTION salescall.trouble_ours(", "SELECT p_trouble IS NOT NULL AND NOT salescall.trouble_counts_line(p_trouble)", "SELECT p_trouble IS NOT NULL", end="\n$$"),
     (_T139 + "test_poor_audio_is_like_a_cut_call_not_our_trouble_and_on_different_customers_it_is_the_line",
      _T139 + "test_the_python_table_of_our_troubles_follows_the_database_rule",
      "tests.test_settle.CutOffCalls.test_our_own_troubles_neither_count_nor_restart_the_customers_cuts_and_are_always_called_back")),
    ("直した記録より前の版の替わりも、直した版とみなす（直す前の版で戻す）",
     _edited(_M141, "CREATE FUNCTION salescall.trouble_fixed_at(", "WHERE v.seen_at > r.reported_at", "WHERE true", end="\n$$"),
     (_T139 + "test_the_same_reason_on_three_rows_stops_the_contractor_and_tells_once",)),
    ("ほかの種類の不具合を直した記録でも、休んだ行・止めた停止を戻す",
     _edited(_M141, "CREATE FUNCTION salescall.trouble_fixed_at(", "WHERE q.kind = p_kind", "WHERE true", end="\n$$"),
     (_T139 + "test_the_same_reason_on_three_rows_stops_the_contractor_and_tells_once",)),
    ("「声が聞き取れない」を、回線の異常に数えうる理由にしない（1か所 trouble_counts_line。こちらの不具合に数え、違うお客様で続いても回線を休ませない）",
     _edited(_M143, "CREATE FUNCTION salescall.trouble_counts_line(", "SELECT coalesce(p_trouble = 'poor_audio', false)", "SELECT false", end="\n$$"),
     (_T139 + "test_poor_audio_is_like_a_cut_call_not_our_trouble_and_on_different_customers_it_is_the_line",)),
    ("通話を終えた根拠の文を、あとで書き換えられる（0143：理由と同じ書き換えで1回だけ）",
     _edited(_M143, "CREATE OR REPLACE FUNCTION salescall.attempt_trouble_fixed(", " OR NEW.trouble_why IS DISTINCT FROM OLD.trouble_why", ""),
     ("tests.test_supervisor.Rounds.test_a_call_with_a_grave_problem_is_ended_with_the_apology_and_called_back",)),
    # ⛔ 5段目の前：この壊し試しは「テスト番号で確かめる停止は1回鳴れば解く」の説明の文（stop.line.resume_after_successes）を見ていた。
    # その仮の設定は 0147 で外した（回線の休みと再開は回線の状態の表＝17-6）ので、外したものが残らないことを見る形に指し直した
    ("外した回線の仮の設定（stop.line.*。17-6）が残る（回線の休み・再開を、状態の表と別の回数で決めうる）",
     "INSERT INTO salescall.setting_definitions (key, merge_kind, value_type, meaning, design_ref, finest_scope)"
     " VALUES ('stop.line.resume_after_successes', 'override', 'integer', '回線の確かめが何回続けて正常なら解くか（契約者ごと）',"
     " 'DESIGN.md 9-1', 'tenant')",
     ("tests.test_settings.Choice.test_meanings_of_the_review21_settings_follow_the_decisions",
      "tests.test_watch.LineStops.test_line_errors_are_recorded_but_never_stop_the_line")),
    ("意味の判定の組の説明を、Jev を先に出す形にしない（0142 の形：3つを同時に出し、Sonnet の料金を払い続ける＝main の e3019c4 に反する）",
     "ALTER TABLE salescall.setting_definitions DISABLE TRIGGER setting_definitions_frozen;"
     " UPDATE salescall.setting_definitions SET meaning = 'jev_and_sonnet＝3つを同時に出す' WHERE key = 'judge.models';"
     " ALTER TABLE salescall.setting_definitions ENABLE TRIGGER setting_definitions_frozen",
     ("tests.test_settings.Choice.test_meanings_of_the_review21_settings_follow_the_decisions",)),
    # ── 状態の欄と、試行のせいの欄（DESIGN.md 17章の3段目＝影。0145）
    ("試行のせいを1回だけ書ける守りを外す（番号のせいと決まった試行を、あとから回線のせいにも、分からないにも書き直せる）",
     "DROP TRIGGER attempt_blame_once ON salescall.attempt_blames", (_TH + "Guards.test_the_blame_of_an_attempt_is_written_once",)),
    ("移った記録の書き換えの拒みを外す", "DROP TRIGGER state_moves_frozen ON salescall.state_moves",
     (_TH + "Guards.test_only_the_two_functions_write_the_states",)),
    ("アプリの役に、回線の欄を直に書く権限を渡す（表を引く入口の外から、状態を変えられる）",
     "GRANT INSERT, UPDATE ON salescall.line_states TO salescall_app", (_TH + "Guards.test_only_the_two_functions_write_the_states",)),
    ("鍵を取って読む関数が、ほかの契約者の回線も読んで作る",
     _edited(_M146, "CREATE OR REPLACE FUNCTION salescall.health_take(",
             "        WHERE l.line_id = k AND l.tenant_id = v_tenant ON CONFLICT DO NOTHING;\n        GET DIAGNOSTICS n = ROW_COUNT;\n"
             "        SELECT s.line_id AS key, NULL::uuid AS client_id, s.stage, s.state INTO r FROM salescall.line_states s\n"
             "        WHERE s.line_id = k AND s.tenant_id = v_tenant FOR UPDATE;",
             "        WHERE l.line_id = k ON CONFLICT DO NOTHING;\n        GET DIAGNOSTICS n = ROW_COUNT;\n"
             "        SELECT s.line_id AS key, NULL::uuid AS client_id, s.stage, s.state INTO r FROM salescall.line_states s\n"
             "        WHERE s.line_id = k FOR UPDATE;"),
     (_TH + "Guards.test_another_contractors_states_are_out_of_reach",)),
    ("書く関数が、ほかの契約者の回線の欄も書き換える",
     _edited(_M146, "CREATE OR REPLACE FUNCTION salescall.health_save(",
             "        UPDATE salescall.line_states SET stage = w->>'stage', state = w->'state', moved_at = clock_timestamp()\n"
             "        WHERE line_id = k AND tenant_id = v_tenant;",
             "        UPDATE salescall.line_states SET stage = w->>'stage', state = w->'state', moved_at = clock_timestamp()\n"
             "        WHERE line_id = k;"),
     (_TH + "Guards.test_another_contractors_states_are_out_of_reach",)),
    # ── 4段目（0146）：回線の空き・表が読む設定・試行のせいの書き方
    ("回線の欄が休み・使用停止の回線も、約束の取り置きに割り当てる（0146 の (3)。回線の休みは停止の記録を作らない）",
     _edited(_M146, "CREATE OR REPLACE FUNCTION salescall.lines_serving()",
             "    AND NOT EXISTS (SELECT 1 FROM salescall.line_states s WHERE s.line_id = o.line_id\n"
             "                    AND s.stage IN ('line_rest', 'audio_rest', 'out_of_use'))", "", end="\n$$"),
     ("tests.test_gate.PromisesStopsAndConflicts.test_lines_under_a_contractor_wide_stop_do_not_serve_promises",)),
    ("表が読む設定を、ほかの契約者の依頼者についても返す（0146 の (7)）",
     _edited(_M146, "CREATE FUNCTION salescall.table_setting_layers(",
             "  IF p_client IS NOT NULL AND NOT EXISTS (SELECT 1 FROM salescall.clients c WHERE c.client_id = p_client AND c.tenant_id = v_tenant) THEN",
             "  IF false THEN"),
     (_TH + "Guards.test_the_table_reads_each_clients_own_values_from_any_transaction_of_its_contractor",)),
    ("表が読む設定に、依頼者の範囲の値を返さない（契約者の取引で引くと、依頼者ごとの番号の決まった順が効かない＝0145 の影の形）",
     _edited(_M146, "CREATE FUNCTION salescall.table_setting_layers(",
             "           OR (v.scope = 'client' AND v.tenant_id = v_tenant AND v.client_id = p_client))",
             "           OR false)"),
     (_TH + "Guards.test_the_table_reads_each_clients_own_values_from_any_transaction_of_its_contractor",
      "tests.test_settle.UnusableNumber.test_the_order_is_per_client")),
    ("自分の値を持たない契約者の一覧を、契約者の取引からも読める（0146 の (5)）",
     _edited(_M146, "CREATE FUNCTION salescall.tenants_without_own_value(",
             "  IF salescall.cur_tenant() IS NOT NULL OR coalesce(salescall.cur_actor_kind(), '') NOT IN ('admin', 'system') THEN",
             "  IF false THEN"),
     (_TH + "Guards.test_another_contractors_states_are_out_of_reach",)),
    ("自分の値を持つ契約者にも、システムの初めの値のテスト番号を渡す（0146 の (5)）",
     _edited(_M146, "CREATE FUNCTION salescall.tenants_without_own_value(",
             "               WHERE NOT EXISTS (SELECT 1 FROM salescall.setting_values v WHERE v.key = p_key AND v.scope = 'tenant'",
             "               WHERE NOT EXISTS (SELECT 1 FROM salescall.setting_values v WHERE false AND v.scope = 'tenant'"),
     (_TH + "Doing.test_the_system_test_number_is_given_to_every_contractor_without_their_own",)),
    ("印の見直しの時刻が来た番号を、表を引く入口へ渡さない（0146 の (4)：印が次の日も外れない）",
     _edited(_M146, "CREATE OR REPLACE FUNCTION salescall.health_take(", "AND (x.state->'next_at'->>'$t')::timestamptz <= now()",
             "AND false"),
     (_TH + "Places.test_a_held_number_is_looked_at_again_the_next_day",)),
    ("1回の表引きで空から番号・回線へ移った試行のせいを、守りに1つずつ通さずに書く（0146 の (6)。使用停止の回線の途中の電話で、書けずに落ちる）",
     _edited(_M146, "CREATE OR REPLACE FUNCTION salescall.health_save(", "        IF w->>'stage' IN ('number', 'line') THEN", "        IF false THEN"),
     (_B141 + "test_a_call_in_progress_when_the_line_is_retired_is_the_lines",)),
    # ── 見直し23（0148）
    ("止める処理の無い原因（保存の失敗・会話AIの利用拒否・費用の上限・赤字）でも、新しく止められる（見直し23 の (4)）",
     "ALTER TABLE salescall.stop_events DROP CONSTRAINT stop_events_cause_retired",
     ("tests.test_stops.RetiredCauses.test_new_stops_with_retired_causes_are_refused",)),
    ("管理者のご決定の値（ほかの契約者と同じ番号の間隔＝3日）を、また「仮」の印にする（見直し23 の (7)）",
     "INSERT INTO salescall.setting_values (key, scope, value, status, rationale, review_when, set_by)"
     " VALUES ('gate.other_contractor_interval', 'system', '{\"days\": 3}', 'provisional', '試験：仮に戻す', '試験', '試験')",
     ("tests.test_settings.Review23Marks.test_marks_follow_the_decisions",)),
    # ── 切れ方の分からない通話の確かめのかけ直し・お知らせの声（2026-10-30 管理者の決定と指摘・取り消せない決定の洗い出し。0150・0151）
    ("確かめのかけ直しの答えを待っている通話を、断られたかが決まったとみなす（同じお客様のほかの行からかけ、結果も確定する）",
     "CREATE OR REPLACE FUNCTION salescall.unclear_check_pending(p_attempt uuid) RETURNS boolean LANGUAGE sql STABLE AS $$ SELECT false $$",
     ("tests.test_settle.UnclearCutChecks.test_until_the_check_call_nothing_irreversible_is_decided",)),
    ("確かめのかけ直しを、同じ行で何度でも記録できる（同じお客様に1回だけ、をデータベースが守らない）",
     "ALTER TABLE salescall.unclear_checks DROP CONSTRAINT unclear_checks_pkey",
     ("tests.test_settle.UnclearCutChecks.test_the_database_keeps_one_check_per_row_and_never_changes_it",)),
    ("確かめのかけ直しの記録を、あとから書き換え・消せる",
     "DROP TRIGGER unclear_checks_frozen ON salescall.unclear_checks",
     ("tests.test_settle.UnclearCutChecks.test_the_database_keeps_one_check_per_row_and_never_changes_it",)),
    ("通話後の検証で見つけた断り（書く入口の無い理由）で、また書ける",
     "ALTER TABLE salescall.dnc_entries DROP CONSTRAINT dnc_entries_found_after_retired",
     ("tests.test_settle.UnclearCutChecks.test_the_retired_reason_found_after_is_not_written",)),
    ("お知らせの種類と試行の終わり方が食い違ってよい（使われていない番号を「出なかった」にできる）",
     "ALTER TABLE salescall.attempts DROP CONSTRAINT attempts_notice",
     ("tests.test_settle.DialNotices.test_the_database_keeps_the_notice_with_its_ending_once",)),
    ("お知らせの種類を、終えたあとに書き換えられる",
     "DROP TRIGGER attempt_notice_once ON salescall.attempts",
     ("tests.test_settle.DialNotices.test_the_database_keeps_the_notice_with_its_ending_once",)),
    # ── 取り消せない決定の確かめ・発信禁止の取り消し・やり取りの文字（2026-10-30 管理者のご決定 (6)(7)。0152）
    ("2つ目の判定とそろわない取り消せない決定の保留を、まだ決まっていないに数えない（同じお客様のほかの行からかけ、結果も確定する）",
     "CREATE OR REPLACE FUNCTION salescall.decision_pending(p_attempt uuid) RETURNS boolean LANGUAGE sql STABLE AS $$"
     " SELECT salescall.unclear_check_pending(p_attempt) $$",
     ("tests.test_irreversible.Irreversible.test_a_disagreed_refusal_waits_for_the_recording_check_which_confirms_it",)),
    ("断られたかがまだ決まっていないの1か所が、取り消せない決定の保留を読まない（0150 の形に戻す）",
     "CREATE OR REPLACE FUNCTION salescall.refusal_undecided(p_attempt uuid) RETURNS boolean LANGUAGE sql STABLE AS $$"
     " SELECT NOT salescall.is_settled(p_attempt) OR salescall.unclear_check_pending(p_attempt) $$",
     ("tests.test_irreversible.Irreversible.test_a_disagreed_refusal_waits_for_the_recording_check_which_confirms_it",)),
    ("承認・録音の確かめに、だれでも答えられる（システムの処理が契約者の承認を押せる）",
     "DROP TRIGGER decision_answer_guard ON salescall.decision_answers",
     ("tests.test_irreversible.VerificationMode.test_an_agreed_refusal_waits_for_the_contractor_and_is_confirmed_by_the_button",)),
    ("取り消せない決定の保留を、書き換え・消せる", "DROP TRIGGER decision_waits_frozen ON salescall.decision_waits",
     ("tests.test_irreversible.Irreversible.test_the_waits_and_answers_are_never_changed",)),
    ("取り消せない決定の答えを、書き換え・消せる", "DROP TRIGGER decision_answers_frozen ON salescall.decision_answers",
     ("tests.test_irreversible.Irreversible.test_the_waits_and_answers_are_never_changed",)),
    ("やり取りの文字を、あとから書き換え・入口の外から消せる（0157：守りの名前を call_utterances_kept に替えた）",
     "DROP TRIGGER call_utterances_kept ON salescall.call_utterances",
     ("tests.test_irreversible.Utterances.test_the_words_are_kept_in_order_and_never_changed",
      "tests.test_irreversible.Utterances.test_the_words_go_with_the_recording_days_but_held_calls_are_kept")),
    ("決まっていない通話の録音を、古い録音を消す処理が消せる（確かめられなくなる）",
     _edited(_M159, "CREATE OR REPLACE FUNCTION salescall.recordings_held(",
             "    UNION SELECT w.attempt_id FROM salescall.decision_waits w WHERE w.tenant_id = salescall.cur_tenant()\n", ""),
     ("tests.test_irreversible.Irreversible.test_a_disagreed_refusal_waits_for_the_recording_check_which_confirms_it",)),
    # ── 録音の確かめで決められない保留は、契約者に確かめてもらう（0153）・保留と答えは中身ごと・報告が変われば決め直す・確定の印の守り（0154）
    ("録音で決められず契約者に求めた保留にも、契約者が答えられない（いつまでも保留のまま）",
     _edited("0154_decisions_by_part.sql", "CREATE OR REPLACE FUNCTION salescall.decision_asks_contractor(",
             "OR EXISTS (SELECT 1 FROM salescall.decision_asks k WHERE k.attempt_id = w.attempt_id AND k.seq = w.seq)", "OR false",
             end="\n$$;"),
     ("tests.test_irreversible.RecordingCannotDecide.test_a_missing_recording_the_ai_cannot_decide_from_the_call_text_asks_the_contractor",)),
    ("承認・差し戻しを、いまの版でない版・いまの中身と違う中身にも答えられる",
     _edited(_M161, "CREATE OR REPLACE FUNCTION salescall.decision_answer_guard(",
             "IF NEW.by_kind = 'approval' AND NOT (", "IF false AND NOT ("),
     ("tests.test_irreversible.VerificationMode.test_an_agreed_refusal_waits_for_the_contractor_and_is_confirmed_by_the_button",)),
    ("取り消しの答えを、発信禁止を取り消さないまま入れられる（ほかの保留が閉じる）",
     _edited(_M161, "CREATE OR REPLACE FUNCTION salescall.decision_answer_guard(",
             "IF NEW.by_kind = 'revoke' AND NOT (", "IF false AND NOT ("),
     ("tests.test_irreversible.ByPart.test_guards",)),
    ("外した録音の確かめの答えを、契約者が入れられる",
     _edited(_M161, "CREATE OR REPLACE FUNCTION salescall.decision_answer_guard(",
             "IF NEW.by_kind = 'recording' THEN", "IF false THEN"),
     ("tests.test_irreversible.VerificationMode.test_an_agreed_refusal_waits_for_the_contractor_and_is_confirmed_by_the_button",)),
    ("待っている中身を、いちばん新しい版でなく、どの版でも数える（決め直した保留・閉じた保留が残る）",
     _edited("0154_decisions_by_part.sql", "CREATE FUNCTION salescall.decision_open(",
             "WHERE w.attempt_id = p_attempt AND w.seq = salescall.decision_wait_seq(p_attempt)), '{}')",
             "WHERE w.attempt_id = p_attempt ORDER BY w.seq LIMIT 1), '{}')", end="\n$$;"),
     ("tests.test_irreversible.VerificationMode.test_a_sent_back_refusal_is_taken_out_of_the_report_and_settled_as_usual",
      "tests.test_irreversible.VerificationMode.test_a_changed_report_makes_a_new_wait_and_an_old_button_is_refused")),
    ("保留を、その通話と違う行・飛んだ版の番号で書ける", "DROP TRIGGER decision_wait_guard ON salescall.decision_waits",
     ("tests.test_irreversible.ByPart.test_guards",)),
    ("契約者に確かめてもらう印を、どの保留・どの版にも付けられる", "DROP TRIGGER decision_ask_guard ON salescall.decision_asks",
     ("tests.test_irreversible.RecordingCannotDecide.test_marks_only_on_the_current_recording_wait",)),
    ("契約者に確かめてもらう印を、書き換え・消せる", "DROP TRIGGER decision_asks_frozen ON salescall.decision_asks",
     ("tests.test_irreversible.Irreversible.test_the_waits_and_answers_are_never_changed",)),
    ("まだ決まっていない通話の版に、確定の印を付けられる", "DROP TRIGGER result_release_guard ON salescall.result_releases",
     ("tests.test_irreversible.VerificationMode.test_the_held_version_cannot_be_released_or_corrected_before_the_decision",)),
    ("あとの通話の結果がある前の通話の版が、行の結果として確定する",
     _edited(_M161, "CREATE OR REPLACE FUNCTION salescall.release_blocked(",
             "OR (r.kind IN ('judged', 'rejudged') AND r.business_state", "OR (false AND r.kind IN ('judged', 'rejudged') AND r.business_state",
            end="\n$$;"),
     ("tests.test_irreversible.ByPart.test_revoking_an_overlapping_entry_keeps_the_later_refusal",)),
    ("通話後の検証が済んだあとにも、やり取りの文字を直に書き足せる", "DROP TRIGGER call_utterance_guard ON salescall.call_utterances",
     ("tests.test_irreversible.ByPart.test_guards",)),
    # ── AIの不具合での一時停止と、契約者以上の緊急の道での再開（2026-10-30 深夜 管理者のご決定。0155）
    ("止まったAIを、契約者が書ける・ほかの依頼者の通話に書ける", "DROP TRIGGER attempt_ai_trouble_guard ON salescall.attempt_ai_troubles",
     (_TA + "Pause.test_only_the_system_writes_stopped_ais_and_never_rewrites_them",)),
    ("止まったAIを、書き換え・消せる", "DROP TRIGGER attempt_ai_troubles_frozen ON salescall.attempt_ai_troubles",
     (_TA + "Pause.test_only_the_system_writes_stopped_ais_and_never_rewrites_them",)),
    ("止まったAIで、ほかの通話が会話に入ると数え直す（同じ時刻にほかの通話が会話に入っただけで、止まらない）",
     _edited(_M170, "CREATE FUNCTION salescall.errors_since(", "greatest(salescall.errors_counted_since(p_cause), ",
             "greatest(salescall.errors_counted_since(p_cause), (SELECT coalesce(max(h.at), '-infinity'::timestamptz)"
             " FROM salescall.attempt_state_history h JOIN salescall.attempts a ON a.attempt_id = h.attempt_id"
             " WHERE h.to_state = 'in_conversation' AND a.tenant_id = salescall.cur_tenant()), ", end="\n$$;"),
     (_TA + "Pause.test_another_call_reaching_the_customer_does_not_hide_the_stopped_ai",)),
    ("システムの処理（契約者・管理者でない）が、緊急の道で再開できる",
     _edited(_M159, "CREATE OR REPLACE FUNCTION salescall.stop_emergency_pass_guard(",
             "IF salescall.cur_actor_kind() NOT IN ('operator', 'admin') OR", "IF false AND salescall.cur_actor_kind() NOT IN ('operator', 'admin') OR"),
     (_TA + "Pause.test_the_contractor_can_reopen_on_the_emergency_route_while_a_judge_is_down",)),
    ("緊急の道での再開を出しても、その停止で新しい電話を止めたまま",
     _edited(_M155, "CREATE OR REPLACE FUNCTION salescall.blocking_stops(",
             "    AND NOT EXISTS (SELECT 1 FROM salescall.stop_emergency_passes p WHERE p.stop_event_id = s.event_id)", "",
             end="\n$$;"),
     (_TA + "Pause.test_the_contractor_can_reopen_on_the_emergency_route_while_a_judge_is_down",)),
    ("緊急の道で再開した契約者の電話でも、判定をふつうの道へ送る（予約に判定の道を付けない）",
     "DROP TRIGGER attempt_judge_route ON salescall.attempts",
     (_TA + "Pause.test_the_contractor_can_reopen_on_the_emergency_route_while_a_judge_is_down",)),
    ("緊急の道で再開しても、約束の電話を受け持てる回線に数えない（約束の受け持ちが狂う）",
     _edited(_M155, "CREATE OR REPLACE FUNCTION salescall.lines_serving(",
             "    AND NOT EXISTS (SELECT 1 FROM salescall.blocking_stops(o.tenant_id, NULL, NULL, o.line_id, NULL))",
             "    AND NOT EXISTS (SELECT 1 FROM salescall.open_stops(o.tenant_id, NULL, NULL, o.line_id))", end="\n$$;"),
     (_TA + "Pause.test_the_contractor_can_reopen_on_the_emergency_route_while_a_judge_is_down",)),
    # ── 通話後の確かめ（別の耳）が止まったときも、次の電話を止める（2026-10-31 管理者のご決定。0158）
    ("通話後の確かめで止まったAIも、通話の中と同じく1つに数える（再開したあとに同じ通話の確かめがまた止まっても、止め直さない）",
     "DROP INDEX salescall.attempt_ai_troubles_once_in_call;"
     " CREATE UNIQUE INDEX attempt_ai_troubles_once_in_call ON salescall.attempt_ai_troubles (attempt_id, ai, route, what)",
     (_TA + "VerificationEar.test_after_resuming_the_same_calls_check_stopping_again_stops_again",)),
    ("通話後の確かめで止まったAIを、終わっていない通話にも書ける",
     _edited("0158_verification_ear_pause.sql", "CREATE OR REPLACE FUNCTION salescall.attempt_ai_trouble_guard(",
             "IF NEW.stage = 'verification' AND NOT EXISTS", "IF false AND NOT EXISTS"),
     (_TA + "VerificationEar.test_only_ended_calls_take_a_check_trouble",)),
    ("試行の判定の道を、あとから書き換えられる",
     _edited(_M155, "CREATE OR REPLACE FUNCTION salescall.attempt_record_fixed(",
             "(NEW.promised, NEW.voice_type, NEW.judge_route) IS DISTINCT FROM (OLD.promised, OLD.voice_type, OLD.judge_route)",
             "(NEW.promised, NEW.voice_type) IS DISTINCT FROM (OLD.promised, OLD.voice_type)"),
     (_TA + "Pause.test_the_contractor_can_reopen_on_the_emergency_route_while_a_judge_is_down",)),
    # ── やり取りの文字を、録音と同じ決まりで消す（計画書 11-4。0157）
    ("測り終わっていない通話のやり取りの文字も消える",
     _edited(_M159, "CREATE OR REPLACE FUNCTION salescall.purge_utterances(",
             "    AND NOT salescall.attempt_kept(u.attempt_id);", ";"),
     ("tests.test_irreversible.Utterances.test_the_words_go_with_the_recording_days_but_held_calls_are_kept",)),
    ("残す日数を待たずに、やり取りの文字が消える",
     _edited(_M159, "CREATE OR REPLACE FUNCTION salescall.purge_utterances(",
             "    AND a.ended_at < now() - make_interval(days => p_days)", ""),
     ("tests.test_irreversible.Utterances.test_the_words_go_with_the_recording_days_but_held_calls_are_kept",)),
    # ── 見直し25（0159）：止める・緊急の道・解くの数え方と、消さない通話を1か所に
    ("確かめで解いても、解いた時刻から数え直す（解くのと同時に書いた止まったAIが、数えられずに消える）",
     _edited(_M159, "CREATE FUNCTION salescall.errors_counted_since(", "max(coalesce(r.counted_from, r.at))", "max(r.at)",
             end="\n$$;"),
     (_TA + "Review25.test_a_stopped_ai_written_while_the_check_runs_is_not_lost",)),
    ("数え始めの時刻を渡しても、その前から数える（確かめの間にまた止まっても解く）",
     _edited(_M170, "CREATE FUNCTION salescall.errors_since(", "coalesce(p_from, '-infinity'::timestamptz)",
             "'-infinity'::timestamptz", end="\n$$;"),
     (_TA + "Review25.test_a_stopped_ai_written_while_the_check_runs_is_not_lost",)),
    ("AIの不具合でない停止（緊急停止など）にも、緊急の道で再開できる",
     _edited(_M159, "CREATE FUNCTION salescall.emergency_stop(", "s.cause = 'ai_connection' AND ", "", end="\n$$;"),
     (_TA + "Review25.test_the_emergency_route_is_only_for_ai_trouble_stops",)),
    ("緊急の道の許可を、入口を通らずに、どの停止にも書ける",
     _edited(_M159, "CREATE OR REPLACE FUNCTION salescall.stop_emergency_pass_guard(",
             "IF NEW.tenant_id IS DISTINCT FROM salescall.cur_tenant() OR NOT salescall.emergency_stop(NEW.stop_event_id) THEN",
             "IF false THEN"),
     (_TA + "Review25.test_the_emergency_route_is_only_for_ai_trouble_stops",)),
    ("緊急の道の許可があれば、AIの不具合でない停止でも、判定を緊急の道へ送る",
     _edited(_M159, "CREATE OR REPLACE FUNCTION salescall.emergency_route(",
             " AND salescall.emergency_stop(p.stop_event_id)", " AND EXISTS (SELECT 1 FROM salescall.open_stop_events s"
             " WHERE s.event_id = p.stop_event_id)", end="\n$$;"),
     (_TA + "Review25.test_the_emergency_route_is_only_for_ai_trouble_stops",)),
    ("切れ方の確かめのかけ直しを待つ通話も、消さない通話に数えない",
     _edited(_M159, "CREATE FUNCTION salescall.attempt_kept(", "      OR salescall.decision_pending(p_attempt)", "", end="\n$$;"),
     ("tests.test_irreversible.Utterances.test_a_call_waiting_for_the_unclear_cut_check_and_an_unended_call_are_kept",)),
    ("録音の片付けが、切れ方の確かめのかけ直しを待つ通話の録音を消してよいとする",
     _edited(_M159, "CREATE OR REPLACE FUNCTION salescall.recordings_held(",
             "    UNION SELECT u.attempt_id FROM salescall.unclear_checks u WHERE u.tenant_id = salescall.cur_tenant()) x",
             ") x"),
     ("tests.test_irreversible.Utterances.test_a_call_waiting_for_the_unclear_cut_check_and_an_unended_call_are_kept",)),
    ("やり取りの文字の守りが、消さない通話を見ない（消す印を立てれば消せる）",
     _edited(_M159, "CREATE OR REPLACE FUNCTION salescall.call_utterances_kept(",
             "  IF salescall.attempt_kept(OLD.attempt_id) THEN", "  IF false THEN"),
     ("tests.test_irreversible.Utterances.test_a_call_waiting_for_the_unclear_cut_check_and_an_unended_call_are_kept",)),
    ("終わっていない通話のやり取りの文字も、予約の時刻で数えて消す",
     _edited(_M159, "CREATE OR REPLACE FUNCTION salescall.purge_utterances(",
             "    AND a.ended_at < now() - make_interval(days => p_days)",
             "    AND coalesce(a.ended_at, a.claimed_at) < now() - make_interval(days => p_days)"),
     ("tests.test_irreversible.Utterances.test_a_call_waiting_for_the_unclear_cut_check_and_an_unended_call_are_kept",)),
    ("止まったAIを、表の持ち主なら、ほかの依頼者の通話に書ける",
     _edited("0158_verification_ear_pause.sql", "CREATE OR REPLACE FUNCTION salescall.attempt_ai_trouble_guard(",
             "  IF NOT EXISTS (SELECT 1 FROM salescall.attempts a WHERE a.attempt_id = NEW.attempt_id\n"
             "                   AND a.tenant_id = NEW.tenant_id AND a.client_id = NEW.client_id) THEN",
             "  IF false THEN"),
     (_TA + "Pause.test_only_the_system_writes_stopped_ais_and_never_rewrites_them",)),
    # ── 見直し26b（0161）：取り消せない決定・確定の印・保留の守り
    ("差し戻したあとに、同じ通話の前の版（断りの版）を確定できる",
     _edited(_M161, "CREATE OR REPLACE FUNCTION salescall.release_blocked(",
             "           OR (r.kind IN ('judged', 'rejudged')\n               AND EXISTS (SELECT 1 FROM salescall.task_results n",
             "           OR (false\n               AND EXISTS (SELECT 1 FROM salescall.task_results n", end="\n$$;"),
     (_TR + "test_an_old_version_cannot_be_released_after_sending_back", _TR + "test_a_sent_back_check_call_refusal_does_not_come_back")),
    ("確定の印の段を、かけたときのモードと違えて付けられる（検証モードの業務の版に本番の印）",
     _edited(_M161, "CREATE OR REPLACE FUNCTION salescall.result_release_guard(",
             "  IF NEW.stage IS DISTINCT FROM (CASE", "  IF false AND NEW.stage IS DISTINCT FROM (CASE"),
     (_TR + "test_the_stage_of_a_release_follows_the_mode_when_called_and_only_people_confirm_verification",)),
    ("検証の段の結果を、システムの処理が確定できる",
     _edited(_M161, "CREATE OR REPLACE FUNCTION salescall.result_release_guard(",
             "  IF NEW.stage = 'verification' AND coalesce", "  IF false AND coalesce"),
     (_TR + "test_the_stage_of_a_release_follows_the_mode_when_called_and_only_people_confirm_verification",)),
    ("検証の印を見ずに、どの版も本番の段とみなす",
     _edited(_M161, "CREATE FUNCTION salescall.attempt_in_verification(",
             "  SELECT EXISTS (SELECT 1 FROM salescall.recording_holds h WHERE h.attempt_id = p_attempt AND h.reason = 'verification')",
             "  SELECT false", end="\n$$;"),
     (_TR + "test_the_stage_of_a_release_follows_the_mode_when_called_and_only_people_confirm_verification",
      "tests.test_irreversible.VerificationMode.test_an_agreed_refusal_waits_for_the_contractor_and_is_confirmed_by_the_button")),
    ("取り消しの答えに、取り消していない中身（怒っていた）を入れられる",
     _edited(_M161, "CREATE OR REPLACE FUNCTION salescall.decision_answer_guard(",
             "cardinality(NEW.parts) > 0 AND NEW.parts <@ salescall.revoked_parts(NEW.attempt_id)",
             "EXISTS (SELECT 1 FROM salescall.dnc_entries e WHERE e.source_attempt_id = NEW.attempt_id AND e.revoked_at IS NOT NULL)"),
     (_TR + "test_a_revoke_answer_carries_only_the_revoked_entries_parts",)),
    ("取り消した登録の理由から、外す中身を決めない（断りを取り消しても外れない）",
     _edited(_M161, "CREATE FUNCTION salescall.revoked_parts(", "WHEN 'refused_in_call' THEN 'refusal' ", "", end="\n$$;"),
     (_TR + "test_a_revoke_answer_carries_only_the_revoked_entries_parts",
      "tests.test_irreversible.Revoke.test_revoking_by_the_source_call_puts_the_rows_back_and_takes_the_refusal_out")),
    ("保留の版を、締めの入口を通らずに書ける（契約者が中身の空の版で保留を閉じる）",
     _edited(_M161, "CREATE OR REPLACE FUNCTION salescall.decision_wait_guard(",
             "  IF current_setting('salescall.settling', true) IS DISTINCT FROM NEW.attempt_id::text THEN", "  IF false THEN"),
     (_TR + "test_only_the_settling_entry_writes_a_wait",)),
    ("広げた登録の一部だけを取り消しても、断りを外して締め直す（取り消していない登録が残っていても、外す中身を返す）",
     _edited(_M161, "CREATE FUNCTION salescall.revoked_parts(",
             "  SELECT CASE WHEN EXISTS (SELECT 1 FROM salescall.dnc_entries e WHERE e.source_attempt_id = p_attempt AND e.revoked_at IS NULL)",
             "  SELECT CASE WHEN false", end="\n$$;"),
     ("tests.test_irreversible.Revoke.test_revoking_one_of_the_spread_entries_keeps_the_refusal",)),
    ("答えた者を、取引の人と違う名前で書ける",
     _edited(_M161, "CREATE OR REPLACE FUNCTION salescall.decision_answer_guard(",
             "  IF NEW.answered_by IS DISTINCT FROM salescall.cur_actor() THEN", "  IF false THEN"),
     (_TR + "test_an_answer_is_by_the_person_of_the_transaction",)),
    # ── 見直し26a（0162）：行が宙に浮かない
    ("強制かけないの行でも、確かめのかけ直しの答えを待ち続ける（同じお客様のほかの行が永久に止まる）",
     _edited("0162_review26a_life_of_a_call.sql", "CREATE OR REPLACE FUNCTION salescall.unclear_check_pending(",
             "\n      AND NOT salescall.task_excluded_now(u.task_id))", ")", end="\n$$;"),
     ("tests.test_settle.Review26a.test_forcing_no_call_stops_waiting_for_the_unclear_cut_check",)),
    # ── 見直し26c（0163）：回線が黙って止まらない
    ("回線に残った試行を、回線ごとの流れの頭で見つけない（終わりの記録が書けずに残った試行で、回線が二度とかけない）",
     _edited("0163_review26c_line_leftovers.sql", "CREATE FUNCTION salescall.line_has_leftovers(",
             "  SELECT EXISTS (SELECT 1 FROM salescall.attempts a", "  SELECT false AND EXISTS (SELECT 1 FROM salescall.attempts a",
             end="\n$$;"),
     ("tests.test_supervisor.Review26c.test_a_call_whose_end_could_not_be_recorded_does_not_stop_the_line",)),
    # ── 2026-10-31 管理者のご判断 伺い3（0164）：AI でも決められない取り消せない決定
    ("設定で決まったとみなした保留の版も「まだ決まっていない」に入れる（そのお客様と行が待ち続ける）",
     _edited("0164_review26_answers_ai_decides_and_db_stalls.sql", "CREATE OR REPLACE FUNCTION salescall.decision_open(",
             "\n                     AND w.checked <> 'assumed'", "", end="\n$$;"),
     ("tests.test_irreversible.ForbiddenUnclearCut.test_when_the_ai_cannot_decide_the_setting_chooses",)),
    ("決まったとみなした印を、承認待ち・中身の無い版にも付けられる",
     "ALTER TABLE salescall.decision_waits DROP CONSTRAINT decision_waits_assumed",
     ("tests.test_irreversible.ForbiddenUnclearCut.test_assumed_waits_are_only_recording_waits_with_parts",)),
    # ── 見直し28（0165）
    ("AIの停止を解く確かめの回数を、ご決定（確かめが通れば自動で再開）より多い 2 回に戻す（0165 の (1)。通った確かめを1回むだに待たせる）",
     "INSERT INTO salescall.setting_values (key, scope, value, status, rationale, review_when, set_by)"
     " VALUES ('stop.ai.resume_after_successes', 'system', '2', 'decided', '壊し試し', '壊し試し', '壊し試し')",
     ("tests.test_watch.TenantAiStopsAndChecks.test_an_ai_stop_resumes_after_successful_checks",)),
    ("名前の録音の台帳の続く言葉に、取り次げないの確かめの一言（unavailable_confirm）を入れられない（0165 の (3)。その一言を流せない）",
     "ALTER TABLE salescall.recording_sets DROP CONSTRAINT recording_sets_name_key;"
     " ALTER TABLE salescall.recording_sets ADD CONSTRAINT recording_sets_name_key CHECK (CASE unit"
     " WHEN 'name_tail' THEN tail IN ('target', 'target_individual', 'target_confirm', 'target_reconfirm', 'promise_again',"
     " 'target_successor') ELSE true END) NOT VALID",
     ("tests.test_gate.NameLedger.test_a_row_is_called_only_in_a_voice_whose_name_recordings_are_all_approved",)),
    ("契約者に確かめてもらう印の理由を、AI が確かめ直しても決められなかったときに限る守りを外す（0168 の (3)。AI がまだ確かめ直して"
     "いない保留も、人に求めうる＝伺い9）",
     "ALTER TABLE salescall.decision_asks DROP CONSTRAINT decision_asks_ai_rechecked",
     ("tests.test_irreversible.RecordingCannotDecide.test_marks_only_on_the_current_recording_wait",)),
    # ── 伺い8（2026-11-01）：用件・最終目的を断られたお客様に、同じ業務の次の実施回でもかけない（業務の設定）
    ("断られたお客様かを、行のいちばん新しい予定でなく、前に一度でも断られたかで決める（断りが外れて予定に戻った行のお客様にもかけない）",
     _edited("0168_ukagai9_10_ai_recheck_and_storage_rest.sql", "CREATE FUNCTION salescall.declined_in_business(",
             "(SELECT n.kind = 'end' AND n.end_reason = 'declined' FROM salescall.task_next_actions n\n"
             "           WHERE n.task_id = t.task_id ORDER BY n.seq DESC LIMIT 1)",
             "EXISTS (SELECT 1 FROM salescall.task_next_actions n WHERE n.task_id = t.task_id AND n.end_reason = 'declined')",
             end="\n$$;"),
     ("tests.test_gate.DataReasons.test_a_customer_who_declined_is_not_called_again_in_the_same_business",)),
    ("断られたお客様を、ほかの業務でもかけない（依頼者のほかの業務の電話まで止める）",
     _edited("0168_ukagai9_10_ai_recheck_and_storage_rest.sql", "CREATE FUNCTION salescall.declined_in_business(",
             "    WHERE r.business_id = p_business\n", "    WHERE true\n", end="\n$$;"),
     ("tests.test_gate.DataReasons.test_a_customer_who_declined_is_not_called_again_in_the_same_business",)),
)

# Python の判断を、わざと1つずつ外す（データベースの守りの壊し試しと同じ考え。0136。⛔ 見直し19a：Python の判断を外しても落ちる試験の
# 無いものが5つあった＝見張っていなかった）。置き換えは app の写し（`_py_copy`）で行い、別の Python で試験を走らせる（この Python に
# 読み込んだ部品を汚さない）。判断を足したら、ここにも足す。（題・app の中の場所・置き換え元・置き換え先・落ちるはずの試験）
_LINE, _RING, _NEXT = "src/salescall/supervisor/line.py", "src/salescall/core/ring_test.py", "src/salescall/core/next_action.py"
_RULES, _HR = "src/salescall/core/health_rules.py", "tests.test_health_rules."
_FU, _OT = "tests.test_irreversible.ForbiddenUnclearCut.", "tests.test_troubles.OurTroubles."
_BS, _BI = "src/salescall/core/business_settings.py", "src/salescall/core/business_import.py"
_APPROVAL, _SETTLE = "src/salescall/core/approval.py", "src/salescall/core/settle.py"
PY_MUTATIONS: tuple[tuple[str, str, str, str, tuple[str, ...]], ...] = (
    ("かけてから「話している」に載せる（かけている途中に、止める・ログインを外す処理が閉じうる）", _LINE,
     "                call.start()   # かける前から", "                pass   # かける前から",
     ("tests.test_supervisor.Rounds.test_the_watch_covers_only_the_call_in_progress_lives_until_the_apology_ends_and_never_waits_on_writes",)),
    ("止めるとき、話している通話も待つ長さで打ち切る（通話の長さで業務を打ち切らない、に反する）", "src/salescall/supervisor/main.py",
     "if not alive or (left <= 0 and not any(call_live(l) for l in alive)):", "if not alive or left <= 0:",
     ("tests.test_supervisor.Main.test_stopping_waits_for_a_live_call_however_long",)),
    ("「話している」を、おわびして切り終えるまで続けない（止める処理が、おわびの途中で切る）", "src/salescall/supervisor/__init__.py",
     'return call is not None and bool(getattr(call, "live", True))', "return call is not None and not getattr(call, 'ended', False)",
     ("tests.test_supervisor.Rounds.test_the_watch_covers_only_the_call_in_progress_lives_until_the_apology_ends_and_never_waits_on_writes",)),
    ("おわびし終える前に「話している」から外れる", _LINE,
     "        return not (self.ended and self._over.is_set())", "        return not self.ended",
     ("tests.test_supervisor.Rounds.test_the_watch_covers_only_the_call_in_progress_lives_until_the_apology_ends_and_never_waits_on_writes",)),
    ("終わった通話にも、おわびを流す（見張るのは進行中のあいだだけ、に反する）", _LINE,
     "                live, recorded = record_or_report()\n                if live:\n                    dial_exit.abandon(self.driver)",
     "                live, recorded = record_or_report()\n                if True:\n                    dial_exit.abandon(self.driver)",
     ("tests.test_supervisor.Rounds.test_the_watch_covers_only_the_call_in_progress_lives_until_the_apology_ends_and_never_waits_on_writes",)),
    ("通話の途中の書き込みを、通話を進める流れで待つ（データベースが遅いと、元気な通話を「固まった」として切る）", _LINE,
     "        self._writes.put(work)\n", "        self.access.in_client(self.actor, self.tenant_id, self.client_id, work)\n",
     ("tests.test_supervisor.Rounds.test_the_watch_covers_only_the_call_in_progress_lives_until_the_apology_ends_and_never_waits_on_writes",)),
    ("通話を進める部品が落ちた電話を、こちらのせいで終えない（結果不明のまま進行中に残る）", _LINE,
     '            self._end("runner_error", "通話を終えないまま戻りました" if error is None', '            None and self._end("runner_error", "通話を終えないまま戻りました" if error is None',
     ("tests.test_supervisor.Rounds.test_calls_that_keep_ending_before_the_answer_stop_the_line_and_our_own_crashes_do_not",)),
    ("お詫びの電話が番号のせいでなく鳴らなかったとき、かけた予定のままにしない（お詫びの回数を使い切りうる）", _NEXT,
     "    redo = _line_fault_again(tx, task_id, dialed, dialed_at, now, blame) if dialed_at is not None else None",
     "    redo = None",
     ("tests.test_apology.Apology.test_an_apology_call_that_did_not_ring_through_no_fault_of_the_number_is_made_again_as_planned",)),
    ("門が、こちらの不具合で終えた電話のあと、復旧を待たずに通す（行の欄が復旧待ちでも通す。直っていなければ、お客様が続けてお詫びの電話を受ける）",
     "src/salescall/core/gate.py", "    if waits is not None:\n        return _deny(waits)",
     "    if waits not in (None, \"awaiting_recovery\"):\n        return _deny(waits)",
     ("tests.test_settle.Settle.test_a_call_our_own_program_ended_is_made_again_as_planned_and_counts_nowhere",
      _T139 + "test_a_call_ended_by_our_trouble_is_called_back_once_recovered")),
    ("候補を出す処理が、復旧を待つ行も出す（門が断るたびに、回線がその行で止まりうる）", "src/salescall/core/scheduler.py",
     "(line_id, list(gate.TASK_STAGE_REASONS),",
     "(line_id, [k for k in gate.TASK_STAGE_REASONS if k != \"await_recovery\"],",
     ("tests.test_settle.Settle.test_a_call_our_own_program_ended_is_made_again_as_planned_and_counts_nowhere",)),
    ("共通の一言の足りない部分を、管理者に知らせない（0171：直せるのは管理者だけなのに、管理者が気づかない）", "src/salescall/core/gate.py",
     "        admin = True   # 共通の一言を直せるのは管理者だけ（0171）", "        admin = False",
     (_VOICE_COMMON,)),
    ("共通の一言の足りない部分を、その版の業務の録音の無い声の分まで並べる（0171 の前の形：声を問わず並べた）",
     "src/salescall/core/recordings.py",
     '''        " WHERE v.retired_at IS NULL AND " + _CURRENT.format(where=_BUSINESS) +''',
     '''        " WHERE v.retired_at IS NULL AND %s IS NOT NULL" +''',
     (_VOICE_COMMON,)),
    ("話してよい声の共通の一言を、取引の契約者の組から読む（0171 の前の形：システムの組を読まない）",
     "src/salescall/core/recordings.py",
     '''_COMMON = "s.unit = 'contractor' AND s.tenant_id IS NULL"''',
     '''_COMMON = "s.unit = 'contractor' AND s.tenant_id = salescall.cur_tenant()"''',
     (_VOICE_COMMON,)),
    ("お詫びの電話を、承認したお詫びの録音と違う声でも話す（録音の文と声がまじる）", "src/salescall/core/gate.py",
     "    if must is not None:\n        ready = [v for v in ready if v == must]", "    if False:\n        ready = [v for v in ready if v == must]",
     ("tests.test_apology.Apology.test_an_apology_call_waits_for_the_voice_the_apology_was_recorded_in",)),
    ("テスト番号を、鳴らす間を待たずに鳴らす（前のテストから、続けて鳴らなかった回数の長さをあけない。契約者の電話を続けて鳴らす）", _RULES,
     "    if line.t >= 1 and line.tested_at is not None and ev.at < line.tested_at + rules.gap(line.t):", "    if False:",
     (_HR + "Decisions.test_1029_every_check_knows_which_step_and_how_many_times",
      _HR + "Stage4.test_a_test_waits_for_its_gap_even_when_another_wait_ends_first")),
    ("テスト番号の鳴らす間を、鳴らない・分からないが続いても伸ばさない（回線ごとの何回目か＝回線の欄の t を見ない）", _RULES,
     "        return self.test_ring_gaps[min(t, len(self.test_ring_gaps)) - 1]", "        return self.test_ring_gaps[0]",
     (_B141 + "test_the_gaps_grow_per_line_and_a_ringing_test_has_none",
      _HR + "Decisions.test_1025_the_gaps_are_15_30_60_120_240_minutes_set_per_contractor")),
    ("テスト番号が鳴ったあとも、何回目かを持ち越す（0139 の読み方：鳴ったあともあける。管理者「鳴ったら、テストＯＫなだけ」に反する）", _RULES,
     "    return Line(LineStage.HEALTHY, audio=line.audio, suspect=line.suspect, j=line.j)",
     "    return Line(LineStage.HEALTHY, audio=line.audio, suspect=line.suspect, j=line.j, t=line.t)",
     (_B141 + "test_the_gaps_grow_per_line_and_a_ringing_test_has_none",
      _HR + "Decisions.test_1025_night_a_ringing_test_is_just_ok")),
    ("テスト番号の答えが見分けられないと返っても、知らせない・修正依頼を出さない（だれにも知られずに続きが決まらない。L-11）", _RULES,
     "def _l11(line: Line, ev: Event, rules: Rules) -> _Step:\n    t = line.t + 1\n    told, acts = _once(line.told, \"見分けられない\",",
     "def _l11(line: Line, ev: Event, rules: Rules) -> _Step:\n    t = line.t + 1\n    told, acts = _once(line.told | {\"見分けられない\"}, \"見分けられない\",",
     (_B141 + "test_a_test_number_that_cannot_be_told_apart_tells_at_once_and_keeps_trying",
      _HR + "Decisions.test_1025_night_an_unclear_answer_is_told_at_once_one_time")),
    ("見分けられない知らせを、同じ続き・同じ休みで何度も出す（同じことを何度も知らせる。1回だけの印の1か所＝_once）", _RULES,
     "    if key in told:\n        return told, ()", "    if key in told and False:\n        return told, ()",
     (_B141 + "test_a_test_number_that_cannot_be_told_apart_tells_at_once_and_keeps_trying",)),
    ("休みの回線の確かめで見分けられなかったとき、知らせない（0141 の形：回線が休んだまま、だれも理由を知らない。L-18・L-26）", _RULES,
     "def _l18_l26(line: Line, ev: Event, rules: Rules) -> _Step:\n    t = line.t + 1\n    told, acts = _once(line.told, \"見分けられない\",",
     "def _l18_l26(line: Line, ev: Event, rules: Rules) -> _Step:\n    t = line.t + 1\n    told, acts = _once(line.told | {\"見分けられない\"}, \"見分けられない\",",
     (_B141 + "test_a_test_number_that_cannot_be_told_apart_tells_at_once_and_keeps_trying",)),
    ("見分けられないテスト番号を、電話をつなぐ部品の不具合として直す担当へ伝えない（L-11 の修正依頼を記録だけにする）", _RULES,
     "                        _do(FIX, fixer=PHONE_FIXER, fix_kind=\"test_ring_undecided\", fix_key=\"test_ring_undecided\",\n"
     "                            about=\"電話をつなぐ部品：テスト番号の答えを見分けられない\")))\n"
     "    return _Step(replace(line, stage=LineStage.CHECK_B, t=t, wait=Wait.GAP",
     "                        _do(RECORD,\n"
     "                            about=\"電話をつなぐ部品：テスト番号の答えを見分けられない\")))\n"
     "    return _Step(replace(line, stage=LineStage.CHECK_B, t=t, wait=Wait.GAP",
     ("tests.test_decided.Decided.test_1025_night_an_unclear_answer_is_told_at_once_one_time",)),
    ("テスト番号が無いとき、決められないことを知らせない（だれにも知られずに続きが決まらない。L-7・L-15）", _RULES,
     "        told, acts = _once(told, \"テスト番号が無い\",", "        told, acts = _once(told | {\"テスト番号が無い\"}, \"テスト番号が無い\",",
     (_B141 + "test_the_last_row_waits_for_the_test_number_and_an_earlier_ring_decides_nothing",
      _B141 + "test_a_resting_line_without_a_test_number_waits_and_no_app_check_resumes_it",
      _T143 + "test_a2_without_a_test_number_bad_numbers_wait_and_the_line_never_stops")),
    ("PC の部品が試さなかったテストを、見分けられないとして記録する（0134〜0142 の形：あける長さを伸ばし、誤った知らせを出す）", _RING,
     "        except CallWithheld:   # PC の部品が試さなかった", "        except ZeroDivisionError:   # PC の部品が試さなかった",
     ("tests.test_supervisor.Blame.test_a_test_the_pc_part_did_not_try_is_not_recorded",)),
    ("サーバーが、PC の答えが届かないテストを「見分けられない」にする（試さなかったと分けない）", "src/salescall/supervisor/channel.py",
     '''            raise CallWithheld("PC の部品がつながっていない・答えが期限に届きませんでした") from None''',
     "            return None",
     ("tests.test_link.CallingState.test_a_test_ring_the_pc_did_not_try_is_not_an_answer",)),
    ("PC の部品が、緊急停止の印があってもテスト番号の答えを「見分けられない」で返す（試さなかったと分けない）", "src/salescall/agent/main.py",
     '''                return {"ok": False, "error": "withheld"}\n            try:\n                rang''',
     '''                return {"ok": True, "value": None}\n            try:\n                rang''',
     ("tests.test_link.CallingState.test_a_test_ring_the_pc_did_not_try_is_not_an_answer",)),
    ("テスト番号で決められるかに、テスト番号があるかを見ない（無い契約者でテスト番号を鳴らしにいき、決まらないまま待つ）", _RING,
     "    return number(tx) is not None", "    return True",
     (_B141 + "test_the_last_row_waits_for_the_test_number_and_an_earlier_ring_decides_nothing",
      _B141 + "test_a_resting_line_without_a_test_number_waits_and_no_app_check_resumes_it")),
    ("候補を出す処理が、まだ分からない電話をかけた回線にも、その行を出す（判定待ちの行に、同じ回線ですぐかけ直す）",
     "src/salescall/core/scheduler.py",
     "        \"   AND NOT salescall.task_unsettled(t.task_id, %s) AND NOT salescall.task_excluded_now(t.task_id)\"",
     "        \"   AND (NOT salescall.task_unsettled(t.task_id, %s) OR true) AND NOT salescall.task_excluded_now(t.task_id)\"",
     (_B141 + "test_a_row_left_undecided_on_one_line_waits_only_on_that_line",)),
    ("最初の鳴らなかった電話から15分（並びの1つ目）を待たずに、テスト番号を鳴らす（次の行がつながれば決まるのに、管理者の電話を鳴らす＝見直し21a の場面 a9）",
     _RULES, "                      run=((ev.attempt, ev.number),), next_at=ev.at + rules.first_gap()))",
     "                      run=((ev.attempt, ev.number),), next_at=ev.at))",
     (_B141 + "test_the_test_number_rings_fifteen_minutes_after_the_first_unrung_call_whatever_rows_are_left",
      _B141 + "test_two_bad_numbers_side_by_side_are_decided_by_the_next_call_without_the_test_number",
      _HR + "Decisions.test_1025_night_fifteen_minutes_with_no_new_setting")),
    ("まだ分からない電話の行も、その場で締めて予定を書く（0129〜0141 の形：壊れた回線の2時間で行が次の日へ送られ、約束の印も外れる＝"
     "見直し21a の場面 a6・a10）", "src/salescall/core/settle.py",
     "    if health.waiting_decision(tx, attempt_id):", "    if False:",
     (_B141 + "test_the_last_row_waits_for_the_test_number_and_an_earlier_ring_decides_nothing",
      _T143 + "test_a6_a_line_broken_for_two_hours_loses_nothing_and_comes_back_with_one_ring",
      _T143 + "test_a10_a_promise_during_a_short_line_outage_keeps_its_mark_and_is_kept_that_day")),
    ("日をまたいで送る数に、まだ分からない電話も入れる（0129〜0141 の形。N-1 の決まった順の次の日）", _RULES,
     "    day = _number_day(rules, first, last, c)", "    day = _number_day(rules, first, last, c + len(unknown))",
     ("tests.test_settle.UnusableNumber.test_a_pending_call_of_the_same_number_on_another_row_does_not_move_the_day",
      "tests.test_decided.Decided.test_1024_an_unknown_call_is_not_counted_on_the_number_until_it_is_decided")),
    ("休みの回線で、テスト番号が1回鳴っても再開しない（再開のたびに管理者の電話を何度も鳴らす＝見直し21a の 3。L-16）", _RULES,
     "    _row(\"L-16\", L, LS.LINE_REST, \"E6\", _l16,", "    _row(\"L-16\", L, LS.LINE_REST, \"E6\", _l17,",
     (_B141 + "test_a_resting_line_with_no_rows_is_still_checked_by_the_test_number",
      _HR + "Decisions.test_1024_resuming_a_stopped_line_is_checked_with_the_test_number",
      _T143 + "test_a6_a_line_broken_for_two_hours_loses_nothing_and_comes_back_with_one_ring")),
    ("止めている種類を見ずに、直した版でも、こちらの不具合の止めを解かない（C-5 を引かない）", _RULES,
     "\"【決 10-25 夜 案A (2)】・【当(8)】\", when=lambda s, e, r: e.kind in s.stopped),",
     "\"【決 10-25 夜 案A (2)】・【当(8)】\", when=lambda s, e, r: False),",
     (_T139 + "test_the_same_reason_on_three_rows_stops_the_contractor_and_tells_once",)),
    ("声が聞き取れずに休ませた回線を、回線のせい（テスト番号が鳴らない）の休みにする（休みの理由を分けない。L-3）", _RULES,
     "    return _Step(_rest(LineStage.AUDIO_REST, rules, ev.at, j=j, t=0, next_at=nxt, **keep),",
     "    return _Step(_rest(LineStage.LINE_REST, rules, ev.at, j=j, t=0, next_at=nxt, **keep),",
     (_T139 + "test_poor_audio_is_like_a_cut_call_not_our_trouble_and_on_different_customers_it_is_the_line",
      _HR + "Decided1029.test_2_two_different_customers_stop_a_line")),
    ("こちらの不具合の知らせに、その行が復旧を待っていることを書かない（最後の行が復旧待ちで止まっても、だれも知らない）",
     "src/salescall/core/watch.py",
     "                   + (f\"。この行は復旧を待っています（{errors.RECOVERY_WAIT}）\" if action.per_call else \"\"))", "                   )",
     (_T139 + "test_two_in_a_row_on_one_row_rest_it_and_the_line_moves_to_the_next_row",)),
    ("通話を終えた根拠の文を、試行に残さない（CL-1 の連絡帳 4-59）", "src/salescall/core/attempts.py",
     "\" WHERE attempt_id = %s AND salescall.is_active(state) AND salescall.is_dialed(state)\"\n                  \" RETURNING attempt_id\", (reason, why, attempt_id))",
     "\" WHERE attempt_id = %s AND salescall.is_active(state) AND salescall.is_dialed(state)\"\n                  \" RETURNING attempt_id\", (reason, None, attempt_id))",
     ("tests.test_supervisor.Rounds.test_a_call_with_a_grave_problem_is_ended_with_the_apology_and_called_back",)),
    ("確かめ中 (a) の時刻（最初の鳴らなかった電話から15分）が来ても、テスト番号を鳴らさない（かける行が残る限り決めない＝17-8 の (5)。L-7）",
     _RULES, "def _l7_l8(line: Line, ev: Event, rules: Rules) -> _Step:\n    return _ring_or_wait(line, ev, rules, checking=True)",
     "def _l7_l8(line: Line, ev: Event, rules: Rules) -> _Step:\n    return _Step(line)",
     (_B141 + "test_the_test_number_rings_fifteen_minutes_after_the_first_unrung_call_whatever_rows_are_left",
      _B141 + "test_the_last_row_waits_for_the_test_number_and_an_earlier_ring_decides_nothing")),
    ("次の電話で決まるのを待つ長さを、あける長さの並びの1つ目でなく別の長さにする（0141 の前：別の設定 line.decide_horizon＝30分）",
     _RULES, "        return self.test_ring_gaps[0]\n", "        return self.test_ring_gaps[1]\n",
     (_B141 + "test_the_test_number_rings_fifteen_minutes_after_the_first_unrung_call_whatever_rows_are_left",
      _HR + "Decisions.test_1025_night_fifteen_minutes_with_no_new_setting")),
    ("回線の欄が鳴らす頼みを置いても、回線ごとの処理がテスト番号を鳴らさない（門がその回線を止めていても・かける行が無くても決める、に反する）",
     _LINE, "    if now.ring:   # 表が鳴らす頼みを置いた", "    if False:   # 表が鳴らす頼みを置いた",
     (_B141 + "test_the_test_number_rings_fifteen_minutes_after_the_first_unrung_call_whatever_rows_are_left",
      _B141 + "test_a_resting_line_with_no_rows_is_still_checked_by_the_test_number")),
    ("休みの回線のテスト番号（再開の確かめ）を、回線ごとの処理が鳴らさない（かける行が無いあいだ確かめない）", "src/salescall/core/health.py",
     "    return Tick(s, s.stage is hr.LineStage.CHECK_C or (s.stage in hr.RESTS and s.wait is hr.Wait.ANSWER))",
     "    return Tick(s, s.stage is hr.LineStage.CHECK_C)",
     (_B141 + "test_a_resting_line_with_no_rows_is_still_checked_by_the_test_number",)),
    ("テスト番号を鳴らしてよい時間を、並びの1つ目だけで決める（0139：昼に寝ている契約者の2つ目の時間に鳴らさない）",
     "src/salescall/core/calling_hours.py",
     "    for a, b in ranges:\n        spans +=", "    for a, b in ranges[:1]:\n        spans +=",
     ("tests.test_calling_hours.Daily.test_several_ranges_in_a_day",)),
    ("こちらの不具合の知らせを、電話ごとでなく1時間に1回にまとめる（2026-10-25 ユーザー「契約者(管理者含む)と…記録にそれを伝え」に反する）",
     "src/salescall/core/watch.py",
     "    key = (f\"{kind}:attempt:{attempt_id}\" if action.per_call and attempt_id is not None", "    key = (f\"{kind}:attempt:{attempt_id}\" if False",
     (_T139 + "test_two_in_a_row_on_one_row_rest_it_and_the_line_moves_to_the_next_row",)),
    ("こちらの不具合を、管理者へは修正依頼を新しく出したときだけ知らせる（電話ごとに管理者へ伝えない）", "src/salescall/core/watch.py",
     "    admin = new_request or fixer is None or action.per_call", "    admin = new_request or fixer is None",
     (_T139 + "test_two_in_a_row_on_one_row_rest_it_and_the_line_moves_to_the_next_row",)),
    ("違う理由のこちらの不具合も合わせて数える（違う理由の3行で、契約者の電話を止める。契約者の欄の種類ごとの組を1つにする）", _RULES,
     "        return dict(self.rows).get(k, frozenset())", "        return frozenset().union(*dict(self.rows).values())",
     (_T139 + "test_different_reasons_on_three_rows_do_not_stop",)),
    ("こちらの不具合で終えた電話を、通話ごとの口から伝えない（記録・知らせが、通話の部品が見つけた中身にならない）", _LINE,
     "        if recorded:\n            self.troubled = True", "        if False:\n            self.troubled = True",
     (_TH + "Doing.test_our_trouble_is_recorded_told_and_asked_per_call_and_three_rows_stop_new_calls",)),
    ("固まったあとに落ちた通話部品の誤りを、見守り役の誤りとして重ねて伝える（同じ出来事の知らせと修正依頼が重なる）", _LINE,
     "        if error is not None and not self.troubled:", "        if error is not None:",
     ("tests.test_supervisor.Rounds.test_a_frozen_call_is_ended_with_the_apology_and_a_long_one_is_not",)),
    ("休み始めた行を知らせない（R-8）", _RULES,
     "                         answered=bool(ev.answered), since=ev.at), _per_call(ev) + rest)",
     "                         answered=bool(ev.answered), since=ev.at), _per_call(ev))",
     (_T139 + "test_two_in_a_row_on_one_row_rest_it_and_the_line_moves_to_the_next_row",)),
    ("候補を出す処理が、休んだ行も出す（門が断るたびに、回線がその行で止まりうる）", "src/salescall/core/scheduler.py",
     "(line_id, list(gate.TASK_STAGE_REASONS),",
     "(line_id, [k for k in gate.TASK_STAGE_REASONS if k != \"resting\"],",
     (_T139 + "test_two_in_a_row_on_one_row_rest_it_and_the_line_moves_to_the_next_row",)),
    ("門が、休んだ行を通す（候補を出したあとで休んだ行にかける）", "src/salescall/core/gate.py",
     "    if waits is not None:\n        return _deny(waits)", "    if waits not in (None, \"task_resting\"):\n        return _deny(waits)",
     (_T139 + "test_two_in_a_row_on_one_row_rest_it_and_the_line_moves_to_the_next_row",)),
    ("直したプログラムの版に替わっても、こちらの不具合の停止を解かない（C-5）", _RULES,
     "    return _Step(Tenant(TenantStage.NORMAL, rows=rows), (_do(RESUME_CALLS),) + acts)",
     "    return _Step(Tenant(TenantStage.NORMAL, rows=rows), acts)",
     (_T139 + "test_the_same_reason_on_three_rows_stops_the_contractor_and_tells_once",)),
    ("中央の見守り役が、動いているプログラムの版を書かない（直しても、休んだ行・止めた契約者が戻らない）", "src/salescall/supervisor/main.py",
     "        if self.version_gap == updates.SAME:   # 版が合っている", "        if False:   # 版が合っている",
     (_T139 + "test_the_running_program_version_is_noted_once_per_change",)),
    ("決める入口の呼び手の表から、こちらの不具合を伝える入口を外す（どこからでも伝えられる＝2つ目の伝え方を作れる）", "checks/rules.py",
     '    ("troubles", "after_trouble"): frozenset({_SUP + "line.py"}),           # こちらの不具合を伝える：通話ごとの口だけ（0139）\n', "",
     ("tests.test_checks.EntriesCalledFromTheirPlaces.test_an_entry_outside_its_places_is_caught",)),
    ("決める入口の呼び手の表から、甚大な問題で終える入口を外す（どこからでも終えられる）", "checks/rules.py",
     '    ("attempts", "end_for_trouble"): frozenset({_SUP + "line.py"}),          # 甚大な問題で終える：通話ごとの口だけ\n', "",
     ("tests.test_checks.EntriesCalledFromTheirPlaces.test_an_entry_outside_its_places_is_caught",)),
    # ── 見直し20a ──
    # ── 見直し21a・21b（0143）：通話ごとの口の待ち・戻らない部品
    ("通話の部品が書き手を待っているあいだ（書き手は進んでいる）も、知らせの途切れに数える（データベースが遅いと、元気な通話を固まったと切る"
     "＝見直し21a の場面 w11）", _LINE,
     "                    return\n                continue\n", "                    return\n",
     ("tests.test_supervisor.Rounds.test_a_flush_that_waits_on_a_slow_but_moving_writer_is_not_a_freeze",)),
    ("通話の部品が頼んだ終わりの移り（話し終えた）を、書き手が書き切るのを待たずに決める（書き込みが遅いと、話し終えた電話におわびを"
     "流し、取れた結果を消す＝場面 w5・w8）", _LINE,
     "(not self._drain(self._finish_ticket) or self._stalled())", "(not self._drain(0) or self._stalled())",
     ("tests.test_supervisor.Rounds.test_the_call_knows_by_itself_when_it_is_finishing_and_never_waits_a_fixed_time",)),
    ("終わりの移りを、頼んだ時点で「終わった」とする（0143 の形：断られた移り・お別れの途中で書き手が止まった電話に、おわびを流さない"
     "＝見直し22 の 3）", _LINE,
     "                self._finish_ticket = self._asked\n",
     "                self._finish_ticket = self._asked\n                self.finishing = True\n",
     ("tests.test_supervisor.Rounds.test_an_end_move_is_written_first_and_a_call_still_in_progress_is_ended",)),
    ("終わりの移りを頼んだあとは、試行がまだ進行中でも終わりの記録を書かない（0143 の形：お別れの途中で固まった電話が ending のまま"
     "残り、回線が黙って止まる＝見直し22 の 3・場面 w22b・w23）", _LINE,
     "            else:\n                live, recorded = record_or_report()\n",
     "            else:\n                live, recorded = (False, False) if self._finish_ticket else record_or_report()\n",
     ("tests.test_supervisor.Rounds.test_an_end_move_is_written_first_and_a_call_still_in_progress_is_ended",)),
    ("通話の部品が書き手を待っているあいだに、書き込みが進まない（データベースが止まった）ことに気づかない（お客様を無言で待たせる）", _LINE,
     "                if self._stalled(now):   # 書き込み1つが frozen_after より長く終わらない",
     "                if False:   # 書き込み1つが frozen_after より長く終わらない",
     ("tests.test_supervisor.Rounds.test_the_call_knows_by_itself_when_it_is_finishing_and_never_waits_a_fixed_time",)),
    ("「動いています」が続いていても、書き込み1つが遅いだけで、データベースが止まったとして終える（0143 の形：元気な通話をおわびで切り、"
     "3行で契約者の電話を止める＝見直し22 の 1・場面 w20・w25）", _LINE,
     "            if self._waiting:   # 通話の部品が書き手を待っている（flush）：書き手が進んでいれば、途切れに数えない",
     "            if self._waiting or self._stalled(now):   # 通話の部品が書き手を待っている（flush）：書き手が進んでいれば、途切れに数えない",
     ("tests.test_supervisor.Rounds.test_a_slow_write_never_ends_a_call_that_is_still_beating",)),
    ("データベースが止まったとき、記録を待ってからおわびする（おわびが遅れる）", _LINE,
     "            if stuck:   # データベースが止まった：記録を待たずに、すぐおわびして切る",
     "            if False:   # データベースが止まった：記録を待たずに、すぐおわびして切る",
     ("tests.test_supervisor.Rounds.test_the_call_knows_by_itself_when_it_is_finishing_and_never_waits_a_fixed_time",)),
    # ── 見直し22 の 8：外しても落ちる試験が無かったもの
    ("通話の部品が戻ったあと、書き手が進まなくても終えない（進行中の通話が残り、お客様が無言の電話に残る）", _LINE,
     "        else:   # 書き手が進まない：データベースが止まった\n"
     "            self._end(\"storage_stuck\", \"データベースへの書き込みが進みません（通話の部品が戻ったあと）\")\n", "",
     ("tests.test_supervisor.Rounds.test_a_writer_that_stops_after_the_call_part_returns_ends_the_call_in_progress",)),
    ("flush で待ち終えたとき、途切れを数え直さない（待っていた長さを途切れに数え、元気な通話を固まったとする）", _LINE,
     "            self.last = max(self.last, time.monotonic())   # 待っていたあいだは、途切れに数えない\n", "",
     ("tests.test_supervisor.Rounds.test_a_flush_that_waits_on_a_slow_but_moving_writer_is_not_a_freeze",)),
    ("通話を終えた根拠の文を200字に切らない（長い文で記録が断られ、通話が進行中のまま残る）", "src/salescall/core/attempts.py",
     "    why = None if why is None else why.strip()[:WHY_MAX] or None", "    why = None if why is None else why or None",
     ("tests.test_supervisor.Rounds.test_the_reason_why_is_cut_to_its_limit",)),
    ("戻らない通話の部品の回線を立て直すとき、つながったままの電話を切ってもらわない（お客様が無言の電話に残る）",
     "src/salescall/supervisor/main.py", "        w.stop.set()\n        self._hang_up_left(line_id, w.at)\n", "        w.stop.set()\n",
     ("tests.test_supervisor.Main.test_a_call_part_that_came_back_is_never_abandoned_but_a_stuck_writer_is",)),
    ("通話を終えた理由と出どころが合うかを確かめない（データベースの止まりを会話の担当へ・固まりを保存の担当へ伝えうる）",
     "src/salescall/core/errors.py",
     "        if e.trouble not in TROUBLES or TROUBLE_SOURCES.get(e.trouble, \"call\") != e.source:\n",
     "        if e.trouble not in TROUBLES:\n",
     (_T139 + "test_different_reasons_on_three_rows_do_not_stop",)),
    ("PC の部品が、答えの期限を過ぎてから鳴らし終えたテスト番号の答えを返す（サーバーはもう待っていない答えを結果にしうる）",
     "src/salescall/agent/main.py",
     "            if time.monotonic() - got > p.ANSWER_BY:   # 答えの期限を過ぎた：サーバーはもう待っていない（答えは届かない＝試さなかった扱い）\n"
     "                return {\"ok\": False, \"error\": \"withheld\"}\n", "",
     ("tests.test_link.CallingState.test_a_test_ring_the_pc_did_not_try_is_not_an_answer",)),
    # ── 見直し22 の 2・5・9（0144）
    ("段の評価が、お客様のところで鳴らなかった締めていない電話も待つ（0143 の形：番号か回線か分からない電話で段が進まず、残りの行に"
     "永久にかけない＝場面 n11）", "src/salescall/core/quality_stage.py",
     "            f\" AND (salescall.is_active(a.state) OR {attempts.rang('a')}) LIMIT 1\"", "            \" LIMIT 1\"",
     ("tests.test_life.Life.test_n11_an_undecided_call_does_not_hold_the_verification_stage",)),
    ("門が、声のかけ直しを待つ行を通す（声が聞き取れなかった回線で、そのまますぐかけ直す＝3段目の誤り。R-11・R-12）", "src/salescall/core/gate.py",
     "    if waits is not None:\n        return _deny(waits)", "    if waits not in (None, \"voice_redial\"):\n        return _deny(waits)",
     (_TH + "Places.test_poor_audio_counts_on_the_line_and_the_row_waits_for_a_voiced_line",)),
    ("ほかの回線が判定待ちの行にかけたあとも、まだ分からない電話を締めずに待つ（あとで締めた古い電話の結果が、あとの電話の結果より新しい版になる。R-4）",
     _RULES, "def _r4(task: Task, ev: Event, rules: Rules) -> _Step:\n    return _Step(_back(task), (_do(CLOSE, attempt=task.attempt),))",
     "def _r4(task: Task, ev: Event, rules: Rules) -> _Step:\n    return _Step(task, (_do(CLOSE, attempt=task.attempt),))",
     ("tests.test_life.Life.test_n2b_a_row_left_undecided_on_a_broken_line_is_called_by_a_healthy_line",
      _TH + "Places.test_another_line_calls_a_row_waiting_on_one_line")),
    ("あとに発信がある古い電話の締めでも、行の予定を書く（判定し直しのときだけ書かない＝0143 の形）", "src/salescall/core/settle.py",
     "    if (_later_dial(tx, a) or health.blame_of", "    if kind == \"rejudged\" and (_later_dial(tx, a) or health.blame_of",
     ("tests.test_life.Life.test_n2b_a_row_left_undecided_on_a_broken_line_is_called_by_a_healthy_line",)),
    ("門が、番号ごとの決まった順（当日2回・翌日・2日後）を見ない（同じ番号のもう1つの行の古い予定で、1日目に3回かける＝場面 n6）",
     "src/salescall/core/gate.py", "    if number_waits(tx, task, now):\n        return _deny(\"number_failed_wait\")",
     "    if False:\n        return _deny(\"number_failed_wait\")",
     ("tests.test_life.Life.test_n6_one_number_on_two_rows_keeps_the_numbers_four_tries",
      "tests.test_settle.UnusableNumber.test_a_pending_call_of_the_same_number_on_another_row_does_not_move_the_day")),
    ("門が、次の動きがその順で書いた予定も、番号ごとの決まった順で断る（約束の時刻・時間帯まで決めた予定を、門が書き直させる）",
     "src/salescall/core/gate.py", "    if planned is not None and planned[\"decided_at\"] >= wait.last_end:\n        return False\n", "",
     ("tests.test_settle.UnusableNumber.test_four_failures_follow_the_order_and_stop_the_number",)),
    ("まだ分からない電話のあいだ、番号の順の日をまたぐところでも、同じ番号へかけ続ける（0143 の形：ほかの回線・ほかの行が同じ日に"
     "かけ続け、決まってみると1日目に4回で「番号が使えない」になりうる。N-4 の印）", _RULES,
     "    return _number_day(rules, first, last, c) > today", "    return False",
     ("tests.test_settle.UnusableNumber.test_a_pending_call_of_the_same_number_on_another_row_does_not_move_the_day",)),
    ("声が聞き取れずに止め直した回線を、すぐテスト番号で再開する（0143 の形：止めてはすぐ再開し、1日に停止13回・お詫び40本＝場面 b5b。L-3）",
     _RULES, "    nxt = ev.at if j == 1 else ev.at + rules.gap(j - 1)", "    nxt = ev.at",
     ("tests.test_troubles.OurTroubles.test_a_line_stopped_again_for_poor_audio_waits_longer_before_the_test_number",
      "tests.test_decided.Decided.test_1029_a_voice_rest_rings_back_stops_again_on_one_call_and_can_be_resumed_by_hand")),
    ("門が番号ごとの決まった順で断ったあと、次の動きが同じ順で予定を書き直さない（決まった間ごとに書き直し続ける）", _NEXT,
     "    if denied.reason == \"number_failed_wait\" and now_target is not None:", "    if False:",
     ("tests.test_settle.UnusableNumber.test_a_pending_call_of_the_same_number_on_another_row_does_not_move_the_day",)),
    ("決めにいくテストを PC の部品がいつも試さなくても、知らせない（行が決まらないまま、だれにも知らされない＝場面 n10。L-12）", _RULES,
     "    if at - since < rules.first_gap():\n        return line.told, ()", "    if True:\n        return line.told, ()",
     ("tests.test_life.Life.test_n10_a_test_the_pc_never_tries_is_told_once",
      _B141 + "test_a_test_the_pc_part_did_not_try_is_not_recorded")),
    ("決めにいくテストを PC の部品が試さなかったら、すぐ知らせる（話している途中などで1回試せなかっただけでも知らせる。L-12・L-19）", _RULES,
     "    if at - since < rules.first_gap():\n        return line.told, ()", "    if False:\n        return line.told, ()",
     ("tests.test_life.Life.test_n10_a_test_the_pc_never_tries_is_told_once",
      _B141 + "test_a_test_the_pc_part_did_not_try_is_not_recorded")),
    ("通話の部品が戻ったあと、書き手が進まなくても戻らないとしない（書き込みがデータベースで止まると、回線が黙って止まる＝場面 w12）", _LINE,
     "        return (self._done.is_set() or self._over.is_set()) and self._stalled(now)", "        return False",
     ("tests.test_supervisor.Main.test_a_call_part_that_came_back_is_never_abandoned_but_a_stuck_writer_is",)),
    ("戻らない理由を、書き手か通話の部品かで分けない（データベースが止まったのを、会話の担当へ伝える）", _LINE,
     "        return \"storage\" if self._stalled() else \"runner\"", "        return \"runner\"",
     ("tests.test_supervisor.Main.test_a_call_part_that_came_back_is_never_abandoned_but_a_stuck_writer_is",)),
    ("通話の部品が戻ったのに、戻らないとする（見張りが、通話の部品が戻ったかを見ない＝見直し21a の q19）", _LINE,
     "        if end is not None and not self._done.is_set() and now - end >= APOLOGY_WAIT + self.frozen_after:",
     "        if end is not None and now - end >= APOLOGY_WAIT + self.frozen_after:",
     ("tests.test_supervisor.Main.test_a_call_part_that_came_back_is_never_abandoned_but_a_stuck_writer_is",)),
    ("戻らない通話の部品の古い流れに、止める印を立てない（戻ったあと、新しい流れと二重にかけうる＝見直し21a の q18）",
     "src/salescall/supervisor/main.py", "        call_over(line_id, call)   # 「話している」から外す（止める処理・ログインを外す処理が、戻らない部品を待たない）\n        w.stop.set()\n",
     "        call_over(line_id, call)   # 「話している」から外す（止める処理・ログインを外す処理が、戻らない部品を待たない）\n",
     ("tests.test_supervisor.Main.test_a_line_whose_call_part_never_comes_back_is_rebuilt_and_told",
      "tests.test_supervisor.Main.test_a_call_part_that_came_back_is_never_abandoned_but_a_stuck_writer_is")),
    ("書けなかった書き込みを持たない（通話の部品へ返さず、捨てる）", _LINE,
     "                    with self._progress:\n                        self._failed.append((work, e))",
     "                    with self._progress:\n                        pass",
     ("tests.test_supervisor.Rounds.test_writes_that_cannot_be_written_are_handed_back_to_the_call",)),
    ("答えの形を、頼んだその場で確かめない（通話の部品が、その場で聞き直せない）", "src/salescall/core/conversation_io.py",
     "    check_item(item)\n    return lambda tx: write_item(tx, attempt_id, item)", "    return lambda tx: write_item(tx, attempt_id, item)",
     ("tests.test_supervisor.Rounds.test_writes_that_cannot_be_written_are_handed_back_to_the_call",)),
    ("受け口が断った書き込みも、見守り役の誤りとして記録する（直すのは頼んだ側＝CL-2 への修正依頼を誤って出す）", _LINE,
     "                    if not isinstance(e, conversation_io.REFUSED):", "                    if True:",
     ("tests.test_supervisor.Rounds.test_writes_that_cannot_be_written_are_handed_back_to_the_call",)),
    ("通話を進める部品が、おわびして終えたあとも戻らないのに、回線の流れを立て直さない（回線が黙って止まる）", "src/salescall/supervisor/main.py",
     "                if stuck is not None:   # 通話の部品が戻らない", "                if False:   # 通話の部品が戻らない",
     ("tests.test_supervisor.Main.test_a_line_whose_call_part_never_comes_back_is_rebuilt_and_told",)),
    ("終えると決めたあと戻らない通話の部品に、気づかない", _LINE,
     "        if end is not None and not self._done.is_set() and now - end >= APOLOGY_WAIT + self.frozen_after:\n            return True",
     "        if False:\n            return True",
     ("tests.test_supervisor.Main.test_a_line_whose_call_part_never_comes_back_is_rebuilt_and_told",)),
    ("テスト番号を、契約者ごとに1本ずつにしない（回線をまたいで・見守り役をまたいで同時に鳴らす）", _RING,
     "        if not mine:   # ほかの回線・ほかの見守り役が鳴らしている途中", "        if False:   # ほかの回線・ほかの見守り役が鳴らしている途中",
     (_B141 + "test_one_test_ring_at_a_time_per_contractor_across_lines_and_supervisors",)),
    ("候補を出す処理が、声のかけ直しを待つ行も出す（門が断るたびに、回線がその行で止まりうる）", "src/salescall/core/scheduler.py",
     "(line_id, list(gate.TASK_STAGE_REASONS),",
     "(line_id, [k for k in gate.TASK_STAGE_REASONS if k != \"voice_redial\"],",
     (_TH + "Places.test_poor_audio_counts_on_the_line_and_the_row_waits_for_a_voiced_line",
      "tests.test_decided.Errors178.test_s17_9_a_row_ended_by_poor_audio_is_not_called_again_at_once_on_the_same_line")),
    ("知らせの重ね防ぎの鍵を、欄ごとに1つにする（戻したあとの2回目の休みを知らせない）", "src/salescall/core/health.py",
     "key=f\"state:{g.box.subject}:{g.box.key}:{g.event.code}:{g.event.at.isoformat()}:{d.get('note')}\"",
     "key=f\"state:{g.box.subject}:{g.box.key}:{d.get('note')}\"",
     (_T139 + "test_a_contractor_or_the_admin_can_put_a_resting_row_back_and_our_calls_never_count",)),
    ("「12時」の決まり（昼が時間内なら昼）を外す（昼も夜中も時間内のとき、確かめに回る）", "src/salescall/core/spoken_time.py",
     "    if noon and pm_in:\n        return pm", "    if False:\n        return pm",
     ("tests.test_spoken_time.MoreWords.test_am_or_pm_from_business_hours",)),
    ("営業時間の終わり 24:00 の、0時ちょうど（閉店ちょうど）を時間外と見る", "src/salescall/core/spoken_time.py",
     "    if close >= DAY and sec == 0:\n        sec = DAY\n", "",
     ("tests.test_spoken_time.MoreWords.test_am_or_pm_from_business_hours",)),
    ("決まった間隔の仕事から、直した版でこちらの不具合の停止を解く仕事を外す", "src/salescall/supervisor/jobs.py",
     '    Job("fixed_stops", 60, "tenant", 300, fixed_stops),\n', "",
     ("tests.test_supervisor.Main.test_which_jobs_run",)),
    ("動いているプログラムの版に、ほかの担当の部品を入れない（会話の部品を直しても、休んだ行・止めた契約者が戻らない）",
     "src/salescall/supervisor/main.py",
     "        self.program = updates.program_version(self.cfg.call_runner, self.cfg.verifier, self.cfg.prober)",
     "        self.program = updates.program_version()",
     (_T139 + "test_the_running_program_version_is_noted_once_per_change",)),
    ("版の指紋を .py だけにする（部品の文や設定のファイルだけを直しても、版が替わらない）", "src/salescall/supervisor/updates.py",
     'sorted(f for f in root.rglob("*") if _source_file(root, f))', 'sorted(f for f in root.rglob("*.py") if _source_file(root, f))',
     (_T139 + "test_the_program_version_covers_every_file_of_the_parts_but_not_their_caches",)),
    ("テスト番号の部品が誤りを返したとき、「分からない」でなく「鳴らなかった」にする（回線のせいの証拠にする）", _RING,
     "            rang = None\n\n        def work", "            rang = False\n\n        def work",
     (_B141 + "test_a_phone_part_that_fails_while_ringing_the_test_number_is_unknown_not_silent",)),
    ("呼び手の表の見張りが、別名で取り込んだ部品を見ない（`import … as 別名` で2つ目の決め方を作れる）", "checks/rules.py",
     "                hits.append((aliases.get(node.value.id, node.value.id), node.attr))",
     "                hits.append((node.value.id, node.attr))",
     ("tests.test_checks.EntriesCalledFromTheirPlaces.test_an_entry_outside_its_places_is_caught",)),
    ("おわびの答えを、ほかの頼みと同じ長さ（REPLY_WAIT）しか待たない（ログインを外した PC のつなぎを、おわびの途中で閉じる）",
     "src/salescall/supervisor/channel.py",
     'got = self.hub.ask(session, "request", wait=p.APOLOGY_WAIT, op="apologize", line=self.line_id)',
     'got = self.hub.ask(session, "request", op="apologize", line=self.line_id)',
     ("tests.test_link.LostConnection.test_an_apology_longer_than_the_reply_wait_is_not_cut_by_a_revoked_pass",)),
    ("止めている種類の1つを直した版で、ほかの種類の止めも解く（片方だけ直って、もう片方の不具合でまた3行がお詫びで終わる。【当(8)】・C-5）",
     _RULES, "    if left:", "    if False:",
     (_HR + "Stage4.test_a_fixed_version_lifts_only_its_own_kind_of_stop",)),
    ("通話ごとの口が、お詫びして終えた電話をどれも、こちらの不具合として伝える（「声が聞き取れない」も修正依頼・行の休みに数える）", _LINE,
     "            return ended, ended and attempts.is_ours(tx, self.attempt_id)", "            return ended, ended",
     (_T139 + "test_poor_audio_is_like_a_cut_call_not_our_trouble_and_on_different_customers_it_is_the_line",)),
    ("「声が聞き取れない」で終えた電話を、回線の欄へ渡さない（違うお客様で続いても回線を休ませない）", "src/salescall/core/health.py",
     "        sends = [(line, hr.Event(\"E4\", at, attempt=me, number=number, line=line_id, audio_elsewhere=elsewhere)),\n",
     "        sends = [\n",
     (_T139 + "test_poor_audio_is_like_a_cut_call_not_our_trouble_and_on_different_customers_it_is_the_line",)),
    # ── 状態の欄の影（DESIGN.md 17章の3段目。core/health.py と、出来事を起こす所）
    ("締めの処理が、まだ渡していない電話の出来事（E1〜E5）を、締める前に状態の欄へ渡さない", "src/salescall/core/settle.py",
     "    health.after_call(tx, attempt_id)   # まだ状態の表へ渡していなければ渡す", "    pass   # まだ状態の表へ渡していなければ渡す",
     (_TH + "Failures.test_a_failing_table_does_not_lose_the_event",)),
    ("通話ごとの口が、こちらの不具合で終えた電話（E3）を、その口のエラーの中身とともに渡さない", "src/salescall/core/troubles.py",
     "    access.in_client(actor, tenant_id, client_id, lambda tx: health.after_call(tx, attempt_id, report=report))", "    pass",
     (_TH + "Places.test_our_trouble_is_given_at_the_call_before_the_settle",
      _TH + "Doing.test_our_trouble_is_recorded_told_and_asked_per_call_and_three_rows_stop_new_calls")),
    ("テスト番号の答え（E6〜E8）を状態の欄へ渡さない", _RING,
     '            health.tested(tx, line_id, "unknown" if rang is None else "rang" if rang else "silent")', "            pass",
     (_TH + "Places.test_the_line_asks_its_column_and_rings_the_test_number",)),
    ("テスト番号を試さなかった（話している途中＝E9）を渡さない", _RING,
     "        if busy:\n            access.in_tenant(actor, tenant_id, lambda tx: health.tested(tx, line_id, \"withheld\"))",
     "        if busy:\n            pass",
     (_B141 + "test_a_test_the_pc_part_did_not_try_is_not_recorded",)),
    ("回線ごとの処理の毎回の頭で、次に動く時刻（E10）を見ない", "src/salescall/core/health.py",
     "    if due is not None and run.now >= due:\n        run.give(line, hr.Event(\"E10\"", "    if False:\n        run.give(line, hr.Event(\"E10\"",
     (_TH + "Places.test_time_comes_at_the_head_of_each_round",)),
    ("テスト番号が入った（E14）を渡さない", "src/salescall/core/settings.py", "        health.number_set(tx)\n", "        pass\n",
     (_TH + "Places.test_time_comes_at_the_head_of_each_round",)),
    ("手で解いた回線の停止（E12）を渡さない", "src/salescall/core/stops.py", "    health.by_hand_stop(tx, stop_event_id, reason)", "    pass",
     (_TH + "Places.test_hands_resume_a_line_and_remove_it",)),
    ("システムが解いた停止も、手で解いた（E12）として渡す", "src/salescall/core/health.py",
     "    who, name = _by(tx)\n    if who is None:\n        return\n    s = tx.one(", "    who, name = _by(tx)\n    s = tx.one(",
     (_TH + "Places.test_hands_resume_a_line_and_remove_it",)),
    ("回線を使用停止にした（E13）を渡さない", "src/salescall/core/registry.py", "    health.removed(tx, line_id, reason)", "    pass",
     (_TH + "Places.test_hands_resume_a_line_and_remove_it",)),
    ("手で戻した休んだ行（E12）を渡さない", "src/salescall/core/troubles.py", "    health.by_hand_task(tx, task_id, reason)", "    pass",
     (_TH + "Places.test_our_trouble_rests_the_row_and_a_contractor_puts_it_back",)),
    ("門の通過で、ほかの回線の判定待ちの行（E15）を渡さない", "src/salescall/core/gate.py", "        health.claimed(tx, task_id, line_id)", "        pass",
     (_TH + "Places.test_another_line_calls_a_row_waiting_on_one_line",)),
    ("直した版（E11）を渡さない", "src/salescall/supervisor/jobs.py", "    ctx.access.in_tenant(ACTOR, ctx.tenant_id, health.fixed)", "    pass",
     (_TH + "Places.test_a_fixed_version_is_given_once",)),
    ("同じ直した版（E11）を、何度でも渡す", "src/salescall/core/health.py",
     "                  (run.tenant, once)) is not None:", "                  (run.tenant, once)) is None and False:",
     (_TH + "Places.test_a_fixed_version_is_given_once",)),
    ("声が聞き取れずに終えた電話（E4）を、ふつうの鳴った電話（E1）にする", "src/salescall/core/health.py",
     '    if a["by_audio"] or _cut_by_audio(tx, a):\n        return "E4"\n', "",
     (_TH + "Places.test_poor_audio_counts_on_the_line_and_the_row_waits_for_a_voiced_line",)),
    ("表を引く所が落ちても、締めの処理が出来事を捨てて締める（出来事を黙って失う。⛔ 3段目の影の受け止め方）", "src/salescall/core/settle.py",
     "    health.after_call(tx, attempt_id)   # まだ状態の表へ渡していなければ渡す",
     "    try:\n        health.after_call(tx, attempt_id)\n    except Exception:   # noqa: BLE001\n        pass   # まだ状態の表へ渡していなければ渡す",
     (_TH + "Failures.test_a_failing_table_does_not_lose_the_event",)),
    ("鍵を取る順を、17章の順（回線→契約者→行・番号・試行のせい）にしない", "src/salescall/core/health.py",
     "        for subject, key in sorted(wants, key=lambda w: ORDER.index(w[0])):", "        for subject, key in wants:",
     (_TH + "LockOrder.test_the_states_are_asked_in_the_order_of_chapter_17",)),
    ("直した版（E11）を、その契約者の欄が動き始める前の版でも渡す（前の版で、そのあとのこちらの不具合の休みを戻す）",
     "src/salescall/core/health.py",
     '                    " AND t > (SELECT min(m.event_at) FROM salescall.state_moves m WHERE m.tenant_id = salescall.cur_tenant())"\n', "",
     (_TH + "Places.test_a_version_fixed_before_the_columns_began_is_not_given",)),
    ("比べる試験が、影の食い違いを見落とさないか：こちらの不具合なしに話し終えた電話（E5）を、ふつうの鳴った電話（E1）として渡す"
     "（復旧待ちの行が戻らず、違う行の数も0に戻らない）", "src/salescall/core/health.py",
     '    if a["ended_clean"] and call_report.talked_in(tx, a["attempt_id"]):   # お客様と話したかは会話の報告の1か所（見直し29c の 6）\n        return "E5"\n', "",
     ("tests.test_life.Life.test_b3_the_same_trouble_with_good_talks_between_does_not_stop",)),
    # ── 4段目（DESIGN.md 17章）：読まなくなった表の作りの関数（unrung_blame・failed_run・consecutive_errors の回線と行の数え方・
    #    task_trouble_run・awaiting_recovery・audio_stops_in_a_row など）の守りを、いまその判断をする1か所（状態の表）で
    ("声で休ませた回線の止めた回数を、声が聞き取れて話し終えた電話で0に戻さない（直ったあとも、1本で止め直す。L-1）", _RULES,
     "        return _Step(replace(line, audio=frozenset(), suspect=0, j=0))",
     "        return _Step(replace(line, audio=frozenset(), suspect=0, j=line.j))",
     (_T139 + "test_a_line_stopped_again_for_poor_audio_waits_longer_before_the_test_number",
      "tests.test_decided.Decided.test_1029_a_call_heard_well_counts_the_voice_again_from_the_start")),
    ("声が聞き取れない電話の続き（違うお客様の組）を、あとの鳴った電話で区切らない（間に話し終えた電話があっても、違うお客様の2本で休み。L-1）",
     _RULES, "        return _Step(replace(line, audio=frozenset(), suspect=0, j=0))",
     "        return _Step(replace(line, suspect=0, j=0))",
     (_T139 + "test_poor_audio_is_like_a_cut_call_not_our_trouble_and_on_different_customers_it_is_the_line",)),
    ("声で休ませた回線を再開したとき、止めた回数を0に戻す（再開のあと、また聞き取れなくても1本で止め直さない＝17-8 の (7)。L-24・L-21）",
     _RULES, "    return Line(LineStage.HEALTHY, j=line.j)", "    return Line(LineStage.HEALTHY, j=0)",
     (_T139 + "test_a_line_stopped_again_for_poor_audio_waits_longer_before_the_test_number",
      _HR + "Decisions.test_1029_after_a_ringing_test_one_more_poor_call_stops_it_again_and_the_gaps_grow")),
    ("違うお客様の「声が聞き取れない」が続いても、回線を休ませない（L-3）", _RULES,
     "        stop = len(audio) >= rules.audio_stop_after_customers", "        stop = False",
     (_T139 + "test_poor_audio_is_like_a_cut_call_not_our_trouble_and_on_different_customers_it_is_the_line",
      _HR + "Decided1029.test_2_two_different_customers_stop_a_line")),
    ("同じお客様の「声が聞き取れない」だけでも、違うお客様として数える（お客様1人の電話の都合で回線を休ませうる。L-3）", _RULES,
     "    audio = line.audio | {ev.number}", "    audio = line.audio | {ev.attempt}",
     (_T139 + "test_poor_audio_is_like_a_cut_call_not_our_trouble_and_on_different_customers_it_is_the_line",)),
    ("「声が聞き取れない」電話を、回線が元気だった証拠にする（違うお客様で続いても休ませない。L-3 を L-1 にする）", _RULES,
     "    _row(\"L-3\", L, LS.HEALTHY, \"E4\", _l3,", "    _row(\"L-3\", L, LS.HEALTHY, \"E4\", _l1,",
     (_T139 + "test_poor_audio_is_like_a_cut_call_not_our_trouble_and_on_different_customers_it_is_the_line",)),
    ("回線の続きを、あとのお客様のところで鳴った電話で区切らない（鳴った電話のあとも、まだ分からないまま。L-5）", _RULES,
     "def _l5(line: Line, ev: Event, rules: Rules) -> _Step:\n    return _Step(_healthy(line), _decide_run(line, \"番号\"))",
     "def _l5(line: Line, ev: Event, rules: Rules) -> _Step:\n    return _Step(line)",
     ("tests.test_settle.UnusableNumber.test_the_run_on_the_line_is_cut_by_a_call_that_rang",
      _HR + "Decisions.test_1024_an_unknown_call_moves_on_to_the_next_row_from_the_first_time")),
    ("テスト番号が鳴っても、まだ分からない続きを番号のせいと決めない（L-9）", _RULES,
     "def _l9(line: Line, ev: Event, rules: Rules) -> _Step:\n    return _Step(_healthy(line), _decide_run(line, \"番号\"))",
     "def _l9(line: Line, ev: Event, rules: Rules) -> _Step:\n    return _Step(_healthy(line))",
     (_B141 + "test_a_test_number_that_rings_makes_the_last_rows_run_the_numbers",)),
    ("テスト番号が鳴らなくても、まだ分からない続きを回線のせいと決めない（L-10）", _RULES,
     "    acts = _decide_run(line, \"回線\") + (_do(RECORD, about=\"テスト番号が鳴らなかったので、回線のせいで休ませた\"),)",
     "    acts = (_do(RECORD, about=\"テスト番号が鳴らなかったので、回線のせいで休ませた\"),)",
     ("tests.test_settle.UnusableNumber.test_a_broken_line_does_not_make_numbers_unusable_on_its_own",
      _B141 + "test_a_silent_test_number_makes_the_run_the_lines_even_after_an_earlier_ring")),
    ("違う番号が2つ続けて鳴らなければ、前の続きを回線のせいにする（0129〜0141 の形：元気な回線を止め、再開しては止まる＝見直し21a の場面 a3・a7。L-6）",
     _RULES, "    return _Step(replace(line, run=line.run + ((ev.attempt, ev.number),)))",
     "    return _Step(replace(line, run=line.run + ((ev.attempt, ev.number),)),"
     " _decide_run(line, \"回線\") if len({n for _, n in line.run} | {ev.number}) >= 2 else ())",
     (_B141 + "test_two_bad_numbers_side_by_side_are_decided_by_the_next_call_without_the_test_number",
      _T143 + "test_a7_the_test_numbers_hours_ending_between_two_bad_numbers_stops_nothing")),
    ("鳴らなかった電話を、あとに回線が鳴らせた証拠が無くても番号のせいにする（回線が壊れた直後の番号を、取りやめうる。L-2）", _RULES,
     "    return _Step(Line(LineStage.CHECK_A, audio=line.audio, suspect=line.suspect, j=line.j, t=0, run_start=ev.at,",
     "    return _Step(line, (_do(DECIDED, attempt=ev.attempt, blame=\"番号\"),))\n    return _Step(Line(LineStage.CHECK_A, audio=line.audio, suspect=line.suspect, j=line.j, t=0, run_start=ev.at,",
     ("tests.test_gate.Exit.test_driver_failures",
      "tests.test_settle.UnusableNumber.test_a_last_failure_that_turns_out_the_lines_does_not_end_the_row")),
    ("使用停止の回線でかけている途中だった電話を、回線のせいにしない（0136 の前：分からないまま、知らせも無く待ち続ける。L-33）", _RULES,
     "    return _Step(line, (_do(DECIDED, attempt=ev.attempt, blame=\"回線\"),))", "    return _Step(line)",
     (_B141 + "test_a_call_in_progress_when_the_line_is_retired_is_the_lines",)),
    ("テスト番号が無いのに、待たずにテスト番号を鳴らしにいく（決まらないまま、テスト番号の頼みをくり返す。L-7・L-15）", _RULES,
     "        return Wait.NO_NUMBER, None, acts, told", "        return None, None, acts, told",
     (_B141 + "test_the_last_row_waits_for_the_test_number_and_an_earlier_ring_decides_nothing",
      _B141 + "test_a_resting_line_without_a_test_number_waits_and_no_app_check_resumes_it")),
    ("テスト番号の答えを記録せずに、表へ渡す（何を根拠に決めたか確かめられない）", _RING,
     "            tx.execute(\"INSERT INTO salescall.line_ring_tests (tenant_id, line_id, purpose, rang)\"",
     "            0 and tx.execute(\"INSERT INTO salescall.line_ring_tests (tenant_id, line_id, purpose, rang)\"",
     (_B141 + "test_one_test_ring_at_a_time_per_contractor_across_lines_and_supervisors",
      _B141 + "test_a_phone_part_that_fails_while_ringing_the_test_number_is_unknown_not_silent")),
    ("話している電話がある回線でも、テスト番号を鳴らす（その電話の結果を待たずに決める）", _RING,
     "            busy = driver.in_call()", "            busy = False",
     (_B141 + "test_a_test_the_pc_part_did_not_try_is_not_recorded",)),
    ("お客様のところで鳴ったかを、発信に失敗したかだけで決める（お客様が出る前に回線の側で終わる電話を、鳴った電話にする）",
     "src/salescall/core/health.py",
     "\"       salescall.rang(a.state, a.cut_off, a.attempt_id) AS rang, salescall.trouble_ours(a.trouble) AS ours,\"",
     "\"       (a.state <> 'call_failed') AS rang, salescall.trouble_ours(a.trouble) AS ours,\"",
     ("tests.test_settle.UnusableNumber.test_calls_ending_before_the_callee_answers_follow_the_same_order",
      "tests.test_supervisor.Rounds.test_a_call_that_ends_before_the_answer_on_the_line_side_rests_the_line_once_it_is_the_lines")),
    ("鳴らなかった電話を、番号・回線の続きに入れない（E2 を渡さない）", "src/salescall/core/health.py",
     "    if not a[\"rang\"]:\n        return \"E2\"\n", "",
     ("tests.test_settle.UnusableNumber.test_four_failures_follow_the_order_and_stop_the_number",
      _TH + "Places.test_a_call_that_did_not_ring_and_the_next_call_that_did")),
    ("こちらのせい（試行の trouble）で鳴らなかった電話を、番号・回線の続きに数える（0136 の前：こちらの不具合で番号を取りやめうる）",
     "src/salescall/core/health.py", "    if a[\"ours\"]:", "    if False:",
     ("tests.test_settle.Settle.test_a_call_our_own_program_ended_is_made_again_as_planned_and_counts_nowhere",)),
    ("同じ番号の、番号のせいの鳴らなかった電話を数えない（0118：受け付けない番号へ、終わりなくかける。N-1）", _RULES,
     "    c = num.n + 1\n    last = _local_day(ev.call_at)", "    c = num.n\n    last = _local_day(ev.call_at)",
     ("tests.test_settle.UnusableNumber.test_four_failures_follow_the_order_and_stop_the_number",
      "tests.test_settle.UnusableNumber.test_other_rows_of_the_same_number_do_not_dial_it_again")),
    ("同じ番号の続きを、あいだにお客様のところで鳴った電話があっても数え続ける（0118：番号を早く捨てる。N-2）", _RULES,
     "def _n2(num: Number, ev: Event, rules: Rules) -> _Step:\n", "def _n2(num: Number, ev: Event, rules: Rules) -> _Step:\n    return _Step(num)\n",
     ("tests.test_settle.UnusableNumber.test_a_call_that_rang_restarts_the_run",)),
    ("番号の続いた最初の日を、いちばん新しい失敗の日にする（次にかける日がずれる。N-1）", _RULES,
     "    first = num.first_day or last", "    first = last",
     ("tests.test_settle.UnusableNumber.test_the_run_of_failures_starts_on_its_first_day",)),
    ("番号の続きの「最後の失敗の終わり」を、続いた最初の失敗の終わりにする（次にかける時刻を取り違える。N-1）", _RULES,
     "    new = Number(NumberStage.RUN, n=c, first_day=first, last_end=ev.call_at, next_day=day,",
     "    new = Number(NumberStage.RUN, n=c, first_day=first, last_end=num.last_end or ev.call_at, next_day=day,",
     ("tests.test_settle.UnusableNumber.test_the_run_of_failures_starts_on_its_first_day",)),
    ("番号の続きに、回線のせいの電話も数える（0129 の前：回線の故障で、番号が使えないと決まる。N-3 を N-1 にする）", _RULES,
     "    _row(\"N-3\", N, (NS.NONE, NS.RUN), \"E16\", _n3,", "    _row(\"N-3\", N, (NS.NONE, NS.RUN), \"E16\", _n1,",
     ("tests.test_settle.UnusableNumber.test_a_broken_line_does_not_make_numbers_unusable_on_its_own",
      _B141 + "test_a_broken_line_rests_and_loses_no_row")),
    ("番号を取りやめる数に、まだ分からない電話も数える（回線のせいかもしれない番号を取りやめる。N-1）", _RULES,
     "    if c >= len(rules.failed_retry_days):\n        return _Step(Number(NumberStage.DEAD",
     "    if c + len(num.unknown - {ev.attempt}) >= len(rules.failed_retry_days):\n        return _Step(Number(NumberStage.DEAD",
     ("tests.test_settle.UnusableNumber.test_a_last_failure_that_turns_out_the_lines_does_not_end_the_row",
      "tests.test_settle.UnusableNumber.test_a_pending_call_of_the_same_number_on_another_row_does_not_move_the_day",
      "tests.test_decided.Decided.test_1024_an_unknown_call_is_not_counted_on_the_number_until_it_is_decided")),
    ("その不具合を直したと記録され、そのあと版が替わっても、休んだ行を戻さない（R-10）", _RULES,
     "         when=lambda s, e, r: e.code == \"E12\" or e.kind in s.kinds),", "         when=lambda s, e, r: e.code == \"E12\"),",
     (_T139 + "test_the_same_reason_on_three_rows_stops_the_contractor_and_tells_once",)),
    ("管理者・契約者が戻しても、休んだ行を戻さない（R-10）", _RULES,
     "         when=lambda s, e, r: e.code == \"E12\" or e.kind in s.kinds),", "         when=lambda s, e, r: e.code != \"E12\" and e.kind in s.kinds),",
     (_T139 + "test_a_contractor_or_the_admin_can_put_a_resting_row_back_and_our_calls_never_count",)),
    ("こちらのせいでない、お客様のところで鳴った電話をはさんでも、行の続きを途切れさせない（たまたまの2回で行を休ませる。R-9）", _RULES,
     "    return _Step(_back(task, n=0, n_kind=None, storage_retried=False))\n\n\ndef _r10", "    return _Step(task)\n\n\ndef _r10",
     (_T139 + "test_a_ringing_call_without_our_trouble_breaks_the_run_and_crashes_go_one_way",)),
    ("行の休みの続きを、声が聞き取れずに終えた電話で途切れさせない（こちらの不具合でない、お客様のところで鳴った電話なのに、休ませる。R-9）", _RULES,
     "    _row(\"R-9\", T, TS.ON_PLAN, (\"E1\", \"E4\", \"E5\"), _r9,", "    _row(\"R-9\", T, TS.ON_PLAN, (\"E1\", \"E5\"), _r9,",
     (_T139 + "test_a_poor_audio_call_between_our_troubles_breaks_the_run_of_the_row",)),
    ("すべての行にかかわる問題を、行の数でなく電話の数で数える（同じ行の2回で、契約者の電話を止めうる。C-1）", _RULES,
     "    tasks = ten.r(ev.kind) | {ev.task}\n    rows = ten.with_r(ev.kind, tasks)\n    if len(tasks) < rules.trouble_stop_after_rows:",
     "    tasks = ten.r(ev.kind) | {ev.attempt}\n    rows = ten.with_r(ev.kind, tasks)\n    if len(tasks) < rules.trouble_stop_after_rows:",
     (_T139 + "test_a_row_is_counted_once_even_if_its_errors_are_recorded_twice",
      _T139 + "test_the_same_reason_on_three_rows_stops_the_contractor_and_tells_once",
      "tests.test_decided.Decided.test_1025_the_same_row_twice_moves_on_and_three_rows_stop_new_calls")),
    ("すべての行にかかわる問題を、その種類を直したと記録され版が替わっても数え直さない（C-5）", _RULES,
     "    left = ten.stopped - {ev.kind}\n    rows = ten.with_r(ev.kind, frozenset())", "    left = ten.stopped - {ev.kind}\n    rows = ten.rows",
     (_T139 + "test_the_same_reason_on_three_rows_stops_the_contractor_and_tells_once",)),
    ("違う3行の数え方で、こちらの不具合なしに話し終えた電話があっても数え直さない（ばらばらの3行で止める。C-3）", _RULES,
     "    return _Step(replace(ten, rows=()))", "    return _Step(ten)",
     (_T139 + "test_rows_count_again_after_a_call_that_talked_without_our_trouble",)),
    ("直した記録が無くても、版が替われば直した版（E11）として渡す（0139 の形：直していないのに休んだ行を戻す）", "src/salescall/core/health.py",
     "(SELECT k, salescall.trouble_fixed_at(k) AS t FROM unnest(%s::text[]) k)", "(SELECT k, salescall.program_since() AS t FROM unnest(%s::text[]) k)",
     (_T139 + "test_the_same_reason_on_three_rows_stops_the_contractor_and_tells_once",
      _T139 + "test_a_call_ended_by_our_trouble_is_called_back_once_recovered")),
    ("復旧に、同じ契約者で話し終えた電話を見ない（直したと記録されて版が替わるまで、お客様へかけ直さない。R-6）", _RULES,
     "         when=lambda s, e, r: (e.code == \"E5\" and not e.own and e.started_at >= s.ended_at)",
     "         when=lambda s, e, r: (e.code == \"E5\" and False)",
     (_T139 + "test_a_call_ended_by_our_trouble_is_called_back_once_recovered",)),
    ("復旧に、その種類を直して版が替わったことを見ない（直したのに、お客様へかけ直さない。R-6）", _RULES,
     "         or (e.code in (\"E11\", \"E21\") and e.kind == s.kind)),", "         or False),",
     (_T139 + "test_a_call_ended_by_our_trouble_is_called_back_once_recovered",)),
    ("復旧を、その電話が終わった時でなく、話し終えた電話の終わりと比べる（その電話の途中に、ほかの回線でかけ始めた電話を復旧の証拠にする。R-6）",
     _RULES, "and not e.own and e.started_at >= s.ended_at)", "and not e.own and e.at >= s.ended_at)",
     (_T139 + "test_a_call_ended_by_our_trouble_is_called_back_once_recovered",)),
    ("復旧の証拠に、話し終えていない電話（出なかった・話し中）も数える（E5 を、お客様のところで鳴っただけの電話にも渡す）",
     "src/salescall/core/health.py", "(a.state = 'ended' AND a.trouble IS NULL) AS ended_clean", "(a.trouble IS NULL) AS ended_clean",
     ("tests.test_health.Places.test_a_call_that_reached_only_a_voicemail_is_no_recovery",)),
    ("取り消した予約（発信まで進まなかった）も、終わった電話として状態の欄へ渡す（取り消しで、復旧を待たずにかけ直しうる）",
     "src/salescall/core/health.py",
     "    \" WHERE a.attempt_id = %s AND salescall.is_dialed(a.state) AND NOT salescall.is_active(a.state) AND a.ended_at IS NOT NULL\")",
     "    \" WHERE a.attempt_id = %s AND NOT salescall.is_active(a.state) AND a.ended_at IS NOT NULL\")",
     (_T139 + "test_a_call_ended_by_our_trouble_is_called_back_once_recovered",)),
    ("こちらの不具合でない「声が聞き取れない」のあとも、復旧を待つ（途中で切れた電話と同じに、すぐかけ直さない。R-11 を R-5 にする）", _RULES,
     "    _row(\"R-11\", T, TS.ON_PLAN, \"E4\", _r11,", "    _row(\"R-11\", T, TS.ON_PLAN, \"E4\", _r5,",
     (_T139 + "test_poor_audio_is_like_a_cut_call_not_our_trouble_and_on_different_customers_it_is_the_line",)),
    # ── 4段目：状態の表を引く入口が「すること」を行う所（core/health._execute）と、読み手が欄を読む所
    ("契約者の欄が止めても、新しい電話を止めない（止める停止の記録を作らない。C-1 の STOP_CALLS を行わない）", "src/salescall/core/health.py",
     "                made[\"stop_event_id\"] = _stop_calls(run, g)", "                made[\"stop_event_id\"] = None",
     (_TH + "Doing.test_our_trouble_is_recorded_told_and_asked_per_call_and_three_rows_stop_new_calls",
      _T139 + "test_the_same_reason_on_three_rows_stops_the_contractor_and_tells_once")),
    ("契約者の欄が止めを解いても、新しい電話の止めを解かない（C-5・C-6 の RESUME_CALLS を行わない）", "src/salescall/core/health.py",
     "                made[\"stop_event_id\"] = _resume_calls(run, g)", "                made[\"stop_event_id\"] = None",
     (_TH + "Doing.test_our_trouble_is_recorded_told_and_asked_per_call_and_three_rows_stop_new_calls",
      _T139 + "test_the_same_reason_on_three_rows_stops_the_contractor_and_tells_once")),
    ("表が「契約者と管理者に知らせる」としても、管理者へ写しを出さない", "src/salescall/core/health.py",
     "admin=\"管理者\" in to, contractor=\"契約者\" in to)", "admin=False, contractor=\"契約者\" in to)",
     ("tests.test_decided.Decided.test_1025_night_an_unclear_answer_is_told_at_once_one_time",
      "tests.test_decided.Decided.test_1029_1_plan_b_a_silent_line_is_told_only_when_the_next_test_is_silent_too")),
    ("表が出した修正依頼を、直す担当へ出さない", "src/salescall/core/health.py",
     "    fix_requests.request(run.tx, d.get(\"fixer\"),", "    0 and fix_requests.request(run.tx, d.get(\"fixer\"),",
     (_TH + "Doing.test_an_unclear_answer_is_told_once_and_asks_the_phone_fixer",
      "tests.test_decided.Errors178.test_s17_4_a_resting_line_whose_test_is_never_tried_is_told_once")),
    ("門が、回線の欄が休みの回線でも通す（回線の休みは停止の記録を作らないので、門が欄を読まないと、休みの回線でお客様へかける）",
     "src/salescall/core/gate.py", "    if health.line_of(tx, line_id).stage in RESTS:", "    if False:",
     ("tests.test_settle.UnusableNumber.test_a_last_failure_that_turns_out_the_lines_does_not_end_the_row",)),
    ("門と候補が読む番号の待つ印を、いつも「無い」と答える（N-4。分からない電話の番号に、同じ日に3回目をかける）", "src/salescall/core/health.py",
     "    return number_of(tx, contact_id).hold", "    return False",
     ("tests.test_settle.UnusableNumber.test_a_pending_call_of_the_same_number_on_another_row_does_not_move_the_day",
      "tests.test_decided.Decided.test_1029_n4_plan_a_the_other_rows_of_a_number_wait_while_the_unknown_call_would_use_up_today")),
    ("お客様のところで鳴ったあとに、こちらの不具合で終えた電話も「こちらのせいで鳴らなかった」として次の動きへ渡す（お詫びのかけ直しにならない）",
     "src/salescall/core/health.py", "    if row is None or (row[\"blame\"] == \"ours\" and row[\"rang\"]):", "    if row is None:",
     ("tests.test_settle.CutOffCalls.test_our_own_troubles_neither_count_nor_restart_the_customers_cuts_and_are_always_called_back",)),
    ("表が読む決まりを、その依頼者の範囲でなく契約者の範囲で読む（依頼者ごとの番号の決まった順が効かない）", "src/salescall/core/health.py",
     "settings.resolve_for_table(self.tx, hr.SETTING_KEYS.values(), client))", "settings.resolve_for_table(self.tx, hr.SETTING_KEYS.values(), None))",
     ("tests.test_settle.UnusableNumber.test_the_order_is_per_client",)),
    ("こちらのせいで終えた電話も、番号の続きに入れる（0136 の形：戻したあとの3回目を、ふつうの鳴らなかった電話として番号に数える。E3 を番号の欄へも渡す）",
     "src/salescall/core/health.py", "        sends = [(line, ev), (task, ev), (att, ev)] + ([(ten, ev)] if to_tenant else [])",
     "        sends = [(line, ev), (task, ev), (att, ev), (num, hr.Event(\"E2\", at, attempt=me, number=number, line=line_id))]"
     " + ([(ten, ev)] if to_tenant else [])",
     (_T139 + "test_a_contractor_or_the_admin_can_put_a_resting_row_back_and_our_calls_never_count",)),
    ("同じ行で続いても休ませない（2回で休む数を大きくする：数えないまま、こちらの不具合でお客様へ鳴らし続ける＝一生の試験 1o。R-8）", _RULES,
     "    _row(\"R-8\", T, TS.ON_PLAN, \"E3\", _r8, \"【決 10-25】・【当(11)】\", when=lambda s, e, r: s.n == 1),",
     "    _row(\"R-8\", T, TS.ON_PLAN, \"E3\", _r8, \"【決 10-25】・【当(11)】\", when=lambda s, e, r: s.n == 1000),",
     (_T139 + "test_two_in_a_row_on_one_row_rest_it_and_the_line_moves_to_the_next_row",
      "tests.test_settle.Settle.test_a_call_our_own_program_ended_is_made_again_as_planned_and_counts_nowhere")),
    # ── 見直し23
    ("言葉ごとの幅を、依頼者の設定でなく初めの値で読む（依頼者が「夕方」を変えても、判定は16〜18時のまま。見直し23 の (2)）",
     "src/salescall/core/gate.py",
     "    periods = spoken_time.periods_from({k: spoken_time.range_seconds(get(key)) for k, key in PERIOD_SETTINGS.items()})",
     "    periods = spoken_time.periods_from({k: spoken_time.range_seconds(v) for k, v in {\"morning\": [\"00:00\", \"10:00\"],"
     " \"forenoon\": [\"00:00\", \"12:00\"], \"afternoon\": [\"13:00\", \"00:00\"], \"evening\": [\"16:00\", \"18:00\"],"
     " \"night\": [\"18:00\", \"20:00\"]}.items()})",
     ("tests.test_settle.CallbackWindow.test_the_client_period_widths_reach_the_judgement_through_settle",)),
    ("本番の業務も、検証モードと同じく品質の基準で評価して止める（撤回された 2026-10-16 の決定の形。見直し23 の (3)）",
     "src/salescall/core/quality_stage.py",
     "    if not _verification(tx, version_id):   # 本番：段も基準も見ない（1件ごとの知らせと修正依頼は、締めの処理が通話ごとに出す）\n"
     "        return None",
     "    pass",
     ("tests.test_quality.Production.test_forbidden_speech_never_stops_the_business_and_tells_per_call",)),
    ("番号の聞き直し・お詫びの電話の別の時間帯へのかけ直しで、前の同じ目的の電話から最短の間隔をあけない（見直し23 の (8)）", _NEXT,
     "        local = max(local, gate.same_contact_free_at(tx, scope, max(d.ended for d in dials)))",
     "        local = local",
     ("tests.test_apology.Apology.test_the_next_band_waits_the_shortest_interval_after_the_last_apology_call",)),
    ("赤字の確認ボタンが押されても、期限が来たら、もう一度知らせる（2026-10-30 管理者：押したらもう知らせない）", "src/salescall/core/usage.py",
     "    if w is not None and w[\"confirmed_at\"] is not None:\n        return None",
     "    if False:\n        return None",
     ("tests.test_usage.CostRatio.test_a_confirmed_deficit_is_not_told_again_while_it_lasts_and_a_new_deficit_warns_again",
      "tests.test_usage.CostRatio.test_a_confirmed_deficit_is_not_reminded")),
    ("API の使用額の上限（管理者の支払いの件）を、返事の文で見分けない（意味の判定の 400 として、1時間続くと CL-1 へ修正依頼になる）",
     "src/salescall/core/errors.py",
     "    if spend_limit(e):\n        return \"spend_limit\"",
     "    if False:\n        return \"spend_limit\"",
     ("tests.test_watch.TenantAiStopsAndChecks.test_the_spend_limit_is_told_to_the_admin_not_asked_to_a_fixer",)),
    ("残高不足（402）を、支払いの件として見分けない（意味の判定の 400 番台の「中身が断られた」に入り、1時間続くと CL-1 へ修正依頼になる）",
     "src/salescall/core/errors.py",
     "    return (e.code in SPEND_LIMIT_CODES and e.source in SPEND_LIMIT_CODE_SOURCES) \\\n        or (",
     "    return (",
     ("tests.test_watch.TenantAiStopsAndChecks.test_the_spend_limit_is_told_to_the_admin_not_asked_to_a_fixer",)),
    ("連携の書き戻しの 402（依頼者の側の支払いの件）も、管理者の支払いの件にする", "src/salescall/core/errors.py",
     " and e.source in SPEND_LIMIT_CODE_SOURCES)", ")",
     ("tests.test_watch.TenantAiStopsAndChecks.test_the_spend_limit_is_told_to_the_admin_not_asked_to_a_fixer",)),
    # ── 切れ方（2026-10-30 管理者の指摘と決定・取り消せない決定の洗い出し。0150・0151）
    ("AI が話していた・だれも話していなかっただけで「断った」にする（⛔ 前の穴：声が届かないお客様が黙って切った通話も営業拒否）",
     "src/salescall/core/judge.py",
     '    return "unclear"                             # AI が話している途中',
     '    return "refused" if cut.speaker in ("ai", "none") else "unclear"   # AI が話している途中',
     ("tests.test_judge.CutTable.test_each_row", "tests.test_settle.UnclearCutChecks.test_until_the_check_call_nothing_irreversible_is_decided")),
    ("声が片方に届いていなかった証拠を見ない（声が届かず切れた通話を、決められない切れ方にする）", "src/salescall/core/judge.py",
     '    if cut.callee_cannot_hear or cut.callee_silent:\n        return "audio"', '    if False:\n        return "audio"',
     ("tests.test_judge.CutTable.test_each_row",
      "tests.test_settle.UnclearCutChecks.test_a_silent_hang_up_by_a_customer_who_could_not_hear_is_not_a_refusal")),
    ("声が届かず切れた通話を、途中で切れた電話にしない（お詫びのかけ直しをしない）", "src/salescall/core/judge.py",
     'or raw_cut == "audio"\n', '\n',
     ("tests.test_judge.CutTable.test_a_callee_who_could_not_hear_and_hung_up_silently_is_not_a_refusal",
      "tests.test_settle.UnclearCutChecks.test_a_silent_hang_up_by_a_customer_who_could_not_hear_is_not_a_refusal")),
    ("会話の報告の、声が届いていなかった証拠を読まない（いつも false）", "src/salescall/core/call_report.py",
     '_bool_or_false(c, "callee_cannot_hear"), _bool_or_false(c, "callee_silent"))', 'False, False)',
     ("tests.test_judge.CutTable.test_the_voice_evidence_is_read_from_the_report_and_old_reports_have_none",)),
    ("状態の表の入口が、声が届かず切れた電話を E4 にしない（声の通る回線からかけ直さない）", "src/salescall/core/health.py",
     '                        report=report) == "audio"', '                        report=report) == "never"',
     ("tests.test_settle.UnclearCutChecks.test_a_silent_hang_up_by_a_customer_who_could_not_hear_is_not_a_refusal",)),
    ("確かめのかけ直しが鳴ったのに出なかった通話を、営業拒否にしない", "src/salescall/core/judge.py",
     "    if inp.check_rang and not (", "    if False and not (",
     ("tests.test_judge.CutTable.test_a_check_call_that_rang_without_a_talk_is_a_refusal",
      "tests.test_settle.UnclearCutChecks.test_a_check_call_that_rang_without_an_answer_is_a_refusal")),
    ("確かめのかけ直しで決まった営業拒否を、はっきり断られたときと同じ理由で台帳に残す", "src/salescall/core/judge.py",
     'dnc="refused_unclear_checked", refusal="unclear_checked"', 'dnc="refused_in_call", refusal="unclear_checked"',
     ("tests.test_judge.Order.test_dnc_reasons_and_flags", "tests.test_settle.UnclearCutChecks.test_a_check_call_that_rang_without_an_answer_is_a_refusal")),
    ("確かめのかけ直しを、同じお客様に何度でもする", _NEXT,
     '    return attempt_id if done is None or done["attempt_id"] == attempt_id else None', '    return attempt_id',
     ("tests.test_settle.UnclearCutChecks.test_a_check_call_that_did_not_ring_is_retried_with_an_apology_after_the_first_gap",)),
    ("切れ方の分からない通話のあと、確かめのかけ直しをしない（ふつうの再発信の方針で）", _NEXT,
     '    if check_of is not None and not j.complaint and', '    if False and not j.complaint and',
     ("tests.test_settle.UnclearCutChecks.test_until_the_check_call_nothing_irreversible_is_decided",)),
    ("確かめのかけ直しが鳴らなかったとき、回線のせいとして時間をおいてお詫びからかけ直さない", _NEXT,
     '    if check is not None and check.outcome == "unrung" and', '    if False and',
     ("tests.test_settle.UnclearCutChecks.test_a_check_call_that_did_not_ring_is_retried_with_an_apology_after_the_first_gap",)),
    ("確かめのかけ直しが鳴らなかったとき、時間をおかずにかけ直す", _NEXT,
     "    at = max(now, check.ended_at + health.first_gap(tx))", "    at = now",
     ("tests.test_settle.UnclearCutChecks.test_a_check_call_that_did_not_ring_is_retried_with_an_apology_after_the_first_gap",)),
    ("確かめのかけ直しの答えを待っている通話の結果を確定する", "src/salescall/core/approval.py",
     "    if _blocked(tx, task_id, version_no):\n        return []",
     "    if False:\n        return []",
     ("tests.test_settle.UnclearCutChecks.test_until_the_check_call_nothing_irreversible_is_decided",)),
    ("同じお客様のほかの行を、確かめを待っていても、ほかの決まっていない発信と同じ理由で待たせる（画面が理由を出し分けられない）",
     "src/salescall/core/dnc.py", '            if p["pending"]:\n                return "decision_pending"\n', "",
     ("tests.test_settle.UnclearCutChecks.test_until_the_check_call_nothing_irreversible_is_decided",)),
    ("「この番号は現在使われておりません」のお知らせでも、回線を確かめ中にして、テスト番号を待つ", "src/salescall/core/health.py",
     '    if code == "E2" and a["notice"] == "not_in_use":', '    if False:',
     ("tests.test_settle.DialNotices.test_not_in_use_alone_is_the_numbers_fault",)),
    ("電源オフ・圏外のお知らせを、発信に失敗（回線を確かめ中にする・番号のせいに数えうる）にする", "src/salescall/core/attempts.py",
     '{"power_off": "no_answer", "out_of_range": "no_answer",', '{"power_off": "call_failed", "out_of_range": "call_failed",',
     ("tests.test_settle.DialNotices.test_power_off_and_out_of_range_are_the_same_as_no_answer",)),
    # ── 声が聞き取れない回線の数え方（2026-10-30 夜 管理者：お客様の回線のせいの見分け・3回・数え直すのは話し終えた電話だけ）
    ("声の続きを、出なかった電話（E1）でも数え直す（声の壊れた回線が止まらない）", _RULES,
     '    if ev.code == "E5":\n        return _Step(replace(line, audio=frozenset(), suspect=0, j=0))',
     '    if True:\n        return _Step(replace(line, audio=frozenset(), suspect=0, j=0))',
     (_HR + "Decided1029.test_1030_a_customer_not_heard_on_another_line_is_the_customers_line_and_three_in_a_row_are_ours",)),
    ("ほかの回線でも聞き取れなかったお客様も、この回線の声の続きに数える", _RULES,
     "    if ev.audio_elsewhere:\n", "    if False:\n",
     (_HR + "Decided1029.test_1030_a_customer_not_heard_on_another_line_is_the_customers_line_and_three_in_a_row_are_ours",)),
    ("お客様の回線のせいと見た電話が続いても、回線を休ませない", _RULES,
     "new.suspect < rules.customer_line_stop_after", "True",
     (_HR + "Decided1029.test_1030_a_customer_not_heard_on_another_line_is_the_customers_line_and_three_in_a_row_are_ours",)),
    ("お客様の回線のせいと分かったお客様を、前の回線の声の続きから外さない", _RULES,
     "audio=line.audio - {ev.number}", "audio=line.audio",
     (_HR + "Decided1029.test_1030_a_customer_not_heard_on_another_line_is_the_customers_line_and_three_in_a_row_are_ours",)),
    ("番号の欄に、声が聞き取れなかった回線を持たない", _RULES,
     "audio_lines=num.audio_lines | {ev.line}", "audio_lines=num.audio_lines",
     (_HR + "Decided1029.test_1030_a_customer_not_heard_on_another_line_is_the_customers_line_and_three_in_a_row_are_ours",)),
    ("話し終えても、番号の欄の声の材料を空にしない", _RULES,
     "    return _Step(replace(num, audio_lines=frozenset()))", "    return _Step(num)",
     (_HR + "Decided1029.test_1030_a_customer_not_heard_on_another_line_is_the_customers_line_and_three_in_a_row_are_ours",)),
    ("入口が、ほかの回線でも聞き取れなかったかを読まない", "src/salescall/core/health.py",
     "        elsewhere = frozenset(num.state.audio_lines) - {line_id}", "        elsewhere = frozenset()",
     ("tests.test_health.Places.test_a_customer_not_heard_on_another_line_too_is_the_customers_line",)),
    ("入口が、前の回線へ「お客様の回線のせいと分かった」を渡さない", "src/salescall/core/health.py",
     '        sends += [(run.boxes[(LINE, other)], hr.Event("E20", at, number=number))',
     '        sends += [] and [(run.boxes[(LINE, other)], hr.Event("E20", at, number=number))',
     ("tests.test_health.Places.test_a_customer_not_heard_on_another_line_too_is_the_customers_line",)),
    ("検証モードの業務でも、そろった断りをその場で決める（契約者の承認を待たない）", _APPROVAL,
     '    if in_verification(tx, attempt_id):   #', '    if False:   #',
     ("tests.test_irreversible.VerificationMode.test_an_agreed_refusal_waits_for_the_contractor_and_is_confirmed_by_the_button",
      "tests.test_irreversible.VerificationMode.test_a_sent_back_refusal_is_taken_out_of_the_report_and_settled_as_usual")),
    ("2つ目の判定とそろわない断りも、その場で決める", _APPROVAL,
     '(frozenset() if irr.check == "agreed" else irr.unsure)', 'frozenset()',
     ("tests.test_irreversible.Irreversible.test_a_disagreed_refusal_waits_for_the_recording_check_which_confirms_it",
      "tests.test_irreversible.Irreversible.test_a_refusal_the_recording_check_did_not_find_is_settled_as_usual")),
    ("通話の途中の申告（そろったときだけ入る）の断りも、報告の2つ目の判定で保留にする", "src/salescall/core/judge.py",
     '        elif not (inp.declared_in_call or j.detail.get("refusal") == "unclear_checked"):', '        elif True:',
     ("tests.test_irreversible.Irreversible.test_a_declared_refusal_is_sure_but_the_report_check_is_read_for_the_rest",)),
    ("保留のあいだも発信禁止を登録する", _SETTLE,
     '    if j.dnc_reason is not None and wait is None:', '    if j.dnc_reason is not None:',
     ("tests.test_irreversible.Irreversible.test_a_disagreed_refusal_waits_for_the_recording_check_which_confirms_it",)),
    ("保留のあいだも苦情を知らせる（あとで苦情でないと分かっても、知らせが残る）", _SETTLE,
     '    if j.complaint and wait is None:', '    if j.complaint:',
     ("tests.test_irreversible.Irreversible.test_wrong_party_and_anger_wait_too_when_not_agreed",)),
    ("保留のあいだも行を終える（判定待ちにしない）", _NEXT, "    if waiting is not None:\n        # 取り消せない決定",
     "    if False:\n        # 取り消せない決定",
     ("tests.test_irreversible.Irreversible.test_a_disagreed_refusal_waits_for_the_recording_check_which_confirms_it",)),
    ("差し戻し・取り消した決定を、その通話の報告から外さない（締め直しても営業拒否のまま）", _SETTLE,
     "    if report is not None and gone:", "    if False:",
     ("tests.test_irreversible.VerificationMode.test_a_sent_back_refusal_is_taken_out_of_the_report_and_settled_as_usual",
      "tests.test_irreversible.Revoke.test_revoking_by_the_source_call_puts_the_rows_back_and_takes_the_refusal_out")),
    ("録音から作り直した報告が届いても、保留を決め直さない（そろっても保留のまま）", _SETTLE,
     '        if call_report.load_with_source(tx, w["attempt_id"])[1] != w["report_source"] \\', '        if False \\',
     ("tests.test_irreversible.Irreversible.test_a_disagreed_refusal_waits_for_the_recording_check_which_confirms_it",)),
    ("お詫びの電話の断りは、2つ目の判定とそろわなくても、その場で決める", _SETTLE,
     "    if wait is not None:\n        # 取り消せない決定（断り・番号違い）", "    if False:\n        # 取り消せない決定（断り・番号違い）",
     ("tests.test_apology.Apology.test_a_disagreed_refusal_in_an_apology_call_waits_for_the_recording_check",)),
    ("検証モードの業務でも、通話の途中の申告をその場で台帳へ入れる", "src/salescall/core/dnc.py",
     '    if not approval.decides_on_spot(tx, attempt_id):', '    if False:',
     ("tests.test_irreversible.VerificationMode.test_the_declaration_in_the_call_is_not_put_in_the_ledger",)),
    ("発信禁止をすべて取り消しても、その通話を締め直さない（結果の「営業拒否」が残り、行も終わったまま）", "src/salescall/core/dnc.py",
     "            approval.withdraw_revoked(tx, source, parts, reason)\n", "            pass\n",
     ("tests.test_irreversible.Revoke.test_revoking_by_the_source_call_puts_the_rows_back_and_takes_the_refusal_out",)),
    ("発信禁止を取り消しても、門が終えたほかの行を予定に戻さない", "src/salescall/core/dnc.py",
     '    reopened = next_action.reopen_revoked(tx, cause=', '    reopened = [] or None and next_action.reopen_revoked(tx, cause=',
     ("tests.test_irreversible.Revoke.test_revoking_by_the_source_call_puts_the_rows_back_and_takes_the_refusal_out",)),
    ("契約者の共通の発信禁止を取り消しても、その登録で門が終えた行を予定に戻さない", _NEXT,
     "OR EXISTS (SELECT 1 FROM salescall.contractor_dnc_entries e WHERE e.revoked_at IS NOT NULL",
     "OR false AND EXISTS (SELECT 1 FROM salescall.contractor_dnc_entries e WHERE e.revoked_at IS NOT NULL",
     ("tests.test_irreversible.Revoke.test_revoking_a_contractor_entry_puts_the_rows_back_by_the_job",)),
    ("まだほかの登録で発信禁止のままの行も、予定に戻す", _NEXT,
     "        if blocked is not None and blocked[0] in DNC_ENDS:\n            continue", "        if False:\n            continue",
     ("tests.test_irreversible.ByPart.test_a_revoke_does_not_report_a_row_still_stopped",
      "tests.test_irreversible.Revoke.test_revoking_a_contractor_entry_puts_the_rows_back_by_the_job")),
    ("取り消した番号を、まだ有効なほかの決定（あとの通話の断り）で止め直さない", "src/salescall/core/dnc.py",
     "    _restop(tx, rows)\n", "",
     ("tests.test_irreversible.ByPart.test_revoking_an_overlapping_entry_keeps_the_later_refusal",)),
    ("同じ出どころの、取り消した番号まで止め直す（一部の取り消しが元に戻る）", "src/salescall/core/dnc.py",
     'x not in {g["phone_e164"] for g in gone}', 'True',
     ("tests.test_irreversible.Revoke.test_revoking_one_of_the_spread_entries_keeps_the_refusal",)),
    ("同じ行の前の通話の保留を、門が見ない（あとの発信があると、同じ行からかける）", "src/salescall/core/dnc.py",
     '    if tx.one("SELECT EXISTS (SELECT 1 FROM salescall.attempts a WHERE a.task_id = %s"',
     '    if False and tx.one("SELECT EXISTS (SELECT 1 FROM salescall.attempts a WHERE a.task_id = %s"',
     ("tests.test_irreversible.ByPart.test_a_wait_found_by_rejudging_an_earlier_call_stops_the_same_row",)),
    ("確かめのかけ直しの「鳴った」を、回線が元気かで決める（電源オフ・圏外・話し中で営業拒否にする）", _NEXT,
     '    rang = code != "E2" and row["notice"] not in ("power_off", "out_of_range") and row["state"] != "busy"',
     '    rang = code != "E2"',
     ("tests.test_irreversible.ByPart.test_a_check_call_that_hit_power_off_or_busy_did_not_ring",)),
    ("前の通話の断りがあとから決まっても、あとの発信があれば行を終えない", _SETTLE,
     '            and not _ends_row(tx, a, j.state, j.complaint, wait is not None):',
     '            and True:',
     ("tests.test_irreversible.ByPart.test_a_wait_found_by_rejudging_an_earlier_call_stops_the_same_row",)),
    ("締めてあるお詫びの電話は、断りを取り消しても次の動きを決め直さない（行が終わったまま）", _SETTLE,
     "        return _settle_apology(tx, a, again=True)", "        return settle(tx, attempt_id)",
     ("tests.test_apology.Apology.test_revoking_an_apology_call_refusal_puts_the_row_back",)),
    ("行が終わっても、確かめのかけ直しを待っていた通話の結果を確定しない（前の作業担当の残した問い (8)）", _NEXT,
     "    approval.after_ended(tx, task_id)\n", "",
     ("tests.test_irreversible.EndedWithoutCheck.test_the_unclear_call_result_is_confirmed_as_it_is_when_the_row_ends",)),
    # ── 決められない保留：AI がまだ確かめ直していなければ会話の担当に頼み（人に求めない＝2026-11-01 伺い9）、AI が確かめ直しても
    #    決められなければ契約者に確かめてもらう・差し戻しは誤判定率の正解に入る（0153・0154）
    ("録音が届かなかった保留を、録音で確かめ直す頼みにする（会話の担当に、通話中の文字から確かめ直してもらえない）", _APPROVAL,
     '"recording_missing" if v["missing"] else', '"recording_missing" if False else',
     ("tests.test_irreversible.RecordingCannotDecide.test_a_missing_recording_waits_for_the_ai_to_recheck_from_the_call_text",)),
    ("録音から作り直してもそろわない保留を、作り直されないものと同じにする", _APPROVAL,
     '"recording_undecided" if v["rebuilt"] else', '"recording_undecided" if False else',
     ("tests.test_irreversible.Irreversible.test_a_disagreed_refusal_waits_for_the_recording_check_which_confirms_it",
      "tests.test_irreversible.RecordingCannotDecide.test_a_rebuilt_report_that_still_disagrees_asks_the_contractor_at_once")),
    ("AI がまだ確かめ直していない保留も、契約者に求める・設定で決まったとみなす（確かめずに発信禁止へ入りうる・人に頼る＝伺い9）", _APPROVAL,
     'AI_RECHECK = ("recording_missing", "recording_unchecked")', "AI_RECHECK = ()",
     ("tests.test_irreversible.RecordingCannotDecide.test_an_unchecked_wait_asks_only_the_conversation_side",
      "tests.test_irreversible.RecordingCannotDecide.test_a_missing_recording_waits_for_the_ai_to_recheck_from_the_call_text")),
    ("AI がまだ確かめ直していない保留も、設定で決まったとみなす（確かめずに発信禁止へ入る＝伺い9）", _APPROVAL,
     "    if why in AI_RECHECK:\n        return None\n", "",
     (_FU + "test_the_setting_does_not_cover_a_wait_the_ai_did_not_recheck",
      "tests.test_irreversible.RecordingCannotDecide.test_an_unchecked_wait_asks_only_the_conversation_side")),
    ("契約者に求める入口が、AI がまだ確かめ直していない理由も受け付ける（人に求めない守りが無い）", _APPROVAL,
     "    if why not in ASK_WHY or why in AI_RECHECK:", "    if why not in ASK_WHY:",
     ("tests.test_irreversible.RecordingCannotDecide.test_the_contractor_is_never_asked_while_the_ai_has_not_rechecked",)),
    ("会話の担当が確かめ直す一覧に、AI が確かめ直しても決められなかった保留も出す（同じ通話を何度も作り直させる）", _APPROVAL,
     "        why, _ = _undecidable_why(tx, w[\"attempt_id\"])\n        if why in AI_RECHECK:\n            out.append(",
     "        why, _ = _undecidable_why(tx, w[\"attempt_id\"])\n        if True:\n            out.append(",
     ("tests.test_irreversible.Irreversible.test_a_disagreed_refusal_waits_for_the_recording_check_which_confirms_it",)),
    ("報告が変わっても、保留の版を足さない（契約者に、前の理由・前の中身のまま求め続ける）", _APPROVAL,
     'cur["report_source"] == report_source', 'True',
     ("tests.test_irreversible.Irreversible.test_a_disagreed_refusal_waits_for_the_recording_check_which_confirms_it",)),
    ("待たなくなっても、保留を閉じない（答えたあとも「まだ決まっていない」のまま）", _APPROVAL,
     "    if cur is None and wait is None:\n        return", "    if wait is None:\n        return",
     ("tests.test_irreversible.VerificationMode.test_a_sent_back_refusal_is_taken_out_of_the_report_and_settled_as_usual",)),
    ("承認した中身も、また待つ（承認しても決まらない）", _APPROVAL,
     "need, kind = irr.parts - confirmed, \"approval\"", "need, kind = irr.parts, \"approval\"",
     ("tests.test_irreversible.VerificationMode.test_an_agreed_refusal_waits_for_the_contractor_and_is_confirmed_by_the_button",)),
    ("差し戻した、その場で決めていたはずの決定を、誤判定率の正解に入れない", "src/salescall/core/measure.py",
     "sorted(set(wrong_items) | set(sent_back))", "sorted(set(wrong_items))",
     ("tests.test_irreversible.VerificationMode.test_a_sent_back_refusal_is_taken_out_of_the_report_and_settled_as_usual",)),
    ("2つ目の判定とそろわなかった決定の差し戻しも、システムの誤りに数える", _APPROVAL,
     " AND w.checked = 'agreed'\"\n", "\"\n",
     ("tests.test_irreversible.VerificationMode.test_a_sent_back_refusal_the_judges_did_not_agree_on_is_not_counted_as_wrong",)),
    ("保留の版を、結果を直す道で確定できる", _APPROVAL,
     "    if _blocked(tx, task_id, checked_version):", "    if False:",
     ("tests.test_irreversible.VerificationMode.test_the_held_version_cannot_be_released_or_corrected_before_the_decision",
      "tests.test_irreversible.Review26.test_an_old_version_cannot_be_released_after_sending_back")),
    ("会話の報告の2つ目の判定の項目を読まない（いつも「そろった」）", "src/salescall/core/call_report.py",
     '    check = "none" if d.get("irreversible_check") is None else \\\n'
     '        _choice(d["irreversible_check"], IRREVERSIBLE_CHECKS, "irreversible_check")',
     '    check = "agreed"',
     ("tests.test_irreversible.ReportShape.test_the_check_is_read_and_old_reports_have_none",
      "tests.test_irreversible.Irreversible.test_a_refusal_the_recording_check_did_not_find_is_settled_as_usual")),
    ("通話後の検証が済んだ通話にも、やり取りの文字を足せる", "src/salescall/core/conversation_io.py",
     '        raise UtteranceInvalid("通話後の検証が済んだ通話のやり取りには、足しません")', '        pass',
     ("tests.test_irreversible.Utterances.test_the_words_are_kept_in_order_and_never_changed",)),
    # ── AIの不具合での一時停止と、契約者以上の緊急の道での再開（2026-10-30 深夜 管理者のご決定。0155）
    ("会話AIが止まっても、判定・別の耳と同じ（緊急の道で再開できる）にする", "src/salescall/core/errors.py",
     '        if e.ai == CONVO_AI:\n            return "convo_stopped"\n', '',
     ("tests.test_ai_troubles.Shape.test_three_kinds_by_ai_and_route",
      "tests.test_ai_troubles.Pause.test_no_emergency_route_when_the_conversation_ai_itself_is_down")),
    ("緊急の道そのものが止まっても、直接の道のAIが止まったのと同じにする", "src/salescall/core/errors.py",
     '        if is_emergency_route(e.route):\n            return "emergency_stopped"\n', '',
     ("tests.test_ai_troubles.Shape.test_three_kinds_by_ai_and_route",
      "tests.test_ai_troubles.Pause.test_the_contractor_can_reopen_on_the_emergency_route_while_a_judge_is_down")),
    ("緊急の道で再開した停止があれば、補えない種類（緊急の道・会話AI）が止まっても止め直さない", "src/salescall/core/watch.py",
     "                       passed_too=action.emergency) is not None:", "                       passed_too=True) is not None:",
     ("tests.test_ai_troubles.Pause.test_the_contractor_can_reopen_on_the_emergency_route_while_a_judge_is_down",)),
    ("補えない種類（会話AI）が止まっていても、緊急の道で再開できる", "src/salescall/core/stops.py",
     "    if blocked is not None:\n        return errors.EMERGENCY_CANNOT[blocked]", "    if False:\n        return errors.EMERGENCY_CANNOT[blocked]",
     ("tests.test_ai_troubles.Pause.test_no_emergency_route_when_the_conversation_ai_itself_is_down",)),
    ("同じ止まったAIを、通話の途中の知らせと報告で二重に数える", "src/salescall/core/ai_troubles.py",
     '            " ON CONFLICT (attempt_id, ai, route, what) WHERE stage = \'call\' DO NOTHING RETURNING attempt_id",',
     '            " ON CONFLICT (attempt_id, ai, route, what) WHERE stage = \'call\' DO UPDATE SET error = EXCLUDED.error RETURNING attempt_id",',
     ("tests.test_ai_troubles.Pause.test_one_stopped_ai_stops_the_contractor_at_once_and_only_once",)),
    ("通話後の確かめの入口が、通話の中の分として書く（再開したあとに同じ通話の確かめがまた止まっても、止め直さない）",
     "src/salescall/core/conversation_io.py",
     'ai_troubles.note(tx, attempt_id, (trouble,), stage="verification"))', 'ai_troubles.note(tx, attempt_id, (trouble,), stage="call"))',
     ("tests.test_ai_troubles.VerificationEar.test_after_resuming_the_same_calls_check_stopping_again_stops_again",
      "tests.test_ai_troubles.VerificationEar.test_a_stopped_ear_after_the_call_stops_the_next_calls_but_not_the_talking_one")),
    ("「緊急の道で答えた」でも止める", "src/salescall/core/ai_troubles.py",
     'ERROR_SOURCE = {STOPPED: "ai_trouble", GLITCH: "ai_glitch"}',
     'ERROR_SOURCE = {STOPPED: "ai_trouble", GLITCH: "ai_glitch", ANSWERED_ON_EMERGENCY: "ai_trouble"}',
     ("tests.test_ai_troubles.Pause.test_one_stopped_ai_stops_the_contractor_at_once_and_only_once",)),
    ("報告の止まったAIを書かない（会話の処理が通話の途中に知らせないと、止まらない）", "src/salescall/core/conversation_io.py",
     "    ai_troubles.note(tx, attempt_id, report.ai_troubles)   # 止まったAI（通話の途中に書いた分は重ねない。0155）", "",
     ("tests.test_ai_troubles.Pause.test_a_report_with_ai_trouble_stops_through_the_same_door",)),
    ("通話の途中の知らせで、通話の終わりまで止めない（そのあいだ、ほかの回線が止まったAIで新しい電話をかける）", _LINE,
     "                    if isinstance(work, conversation_io.TroubleWork):", "                    if False:",
     ("tests.test_ai_troubles.Pause.test_the_call_door_stops_right_away_without_waiting_for_the_end_of_the_call",)),
    # ── 見直し25（0159）
    ("緊急の道を出せるかを、補えない種類でなく、止める種類のどれかで決める（Sonnet が止まっただけで、緊急の道を出せない）",
     "src/salescall/core/watch.py",
     "    return next((k for k in errors.EMERGENCY_BLOCKERS if _happened(tx, k)), None)",
     '    return next((k for k in errors.EMERGENCY_BLOCKERS if _happened(tx, k) or _happened(tx, "ai_stopped")), None)',
     (_TA + "Pause.test_the_contractor_can_reopen_on_the_emergency_route_while_a_judge_is_down",)),
    ("同じ原因のほかの種類で、その種類が起きたことにする（Sonnet が止まっただけで「会話AIが止まった」になり、緊急の道を出せない）",
     "src/salescall/core/watch.py", '    n = tx.one("SELECT salescall.errors_since(%s, %s, %s) AS n", ([kind], action.stop_cause, since))["n"]',
     '    n = tx.one("SELECT salescall.errors_since(%s, %s, %s) AS n", (list(errors.COUNTED_FOR[action.stop_cause]),'
     ' action.stop_cause, since))["n"]',
     (_TA + "Pause.test_the_contractor_can_reopen_on_the_emergency_route_while_a_judge_is_down",)),
    ("会話AIにつながらない（生の誤り）で、また契約者を止める（止まったかを会話の側と2か所で決める。CL-1 の 4-69 の 4-179 の (a)）",
     "src/salescall/core/errors.py", '    "ai_unreachable": Action("keep", "backoff", None, None, False,',
     '    "ai_unreachable": Action("keep", "backoff", "ai_connection", "tenant", False,',
     ("tests.test_watch.TenantAiStopsAndChecks.test_raw_conversation_ai_errors_never_stop_but_reach_cl1",
      _TA + "Review25.test_raw_conversation_ai_errors_never_block_the_emergency_route")),
    ("設定の誤り（生の誤り）1回で、また契約者を止める", "src/salescall/core/errors.py",
     '    "config_error": Action("keep", "none", None, None, True,', '    "config_error": Action("keep", "none", "ai_connection", "tenant", True,',
     ("tests.test_watch.TenantAiStopsAndChecks.test_raw_conversation_ai_errors_never_stop_but_reach_cl1",)),
    ("別の耳の生の誤りを、会話AIの種類のまま記録する（別の耳の 503 が「会話AIの側の誤り」になる）", "src/salescall/core/errors.py",
     '        if e.source == "ear" and kind in CONVO_RAW:', '        if e.source == "ear" and kind == "config_error":',
     ("tests.test_watch.Classify.test_only_observed_pairs_have_a_kind",)),
    ("確かめの間にまた止まっても、確かめが通れば解く", "src/salescall/core/watch.py",
     "        if late is not None:\n            ok, evidence = False,", "        if False:\n            ok, evidence = False,",
     (_TA + "Review25.test_a_stopped_ai_written_while_the_check_runs_is_not_lost",)),
    ("確かめで解いても、確かめを始めた時刻を残さない（解いた時刻から数え直す）", "src/salescall/core/watch.py",
     "                               counted_from=begun_at)", "                               counted_from=None)",
     (_TA + "Review25.test_a_stopped_ai_written_while_the_check_runs_is_not_lost",)),
    ("緊急の道を出せる停止かを、入口で確かめない", "src/salescall/core/stops.py",
     '    if not s["ok"]:\n        return "緊急の道で再開できるのは', '    if False:\n        return "緊急の道で再開できるのは',
     (_TA + "Review25.test_the_emergency_route_is_only_for_ai_trouble_stops",)),
    ("停止の説明に、その停止を解いたあとの止まったAIまで出す", "src/salescall/core/ai_troubles.py",
     "        \"   AND t.noted_at <= coalesce(r.counted_from, r.at, 'infinity'::timestamptz)\"", "        \"   AND true\"",
     (_TA + "Review25.test_old_events_of_the_same_call_do_not_stop_again_after_reopening",)),
    ("見守り役が、止まったAIで止めるかを決め直さない", "src/salescall/supervisor/jobs.py",
     "    ctx.access.in_tenant(ACTOR, ctx.tenant_id, watch.decide_ai_stops)", "    pass",
     (_TA + "Review25.test_the_supervisor_decides_again_when_the_first_decision_was_missed",)),
    ("通話後の確かめの入口が、止めるかを決めない", "src/salescall/core/conversation_io.py",
     "    return watch.after_ai_troubles(access, actor, tenant_id)\n", "    return []\n",
     (_TA + "VerificationEar.test_a_stopped_ear_after_the_call_stops_the_next_calls_but_not_the_talking_one",)),
    ("やり取りの文字を、Fly.io でなくハードディスクの日数で消す", "src/salescall/core/conversation_io.py",
     '"recording.keep_days_cloud"', '"recording.keep_days_drive"',
     ("tests.test_irreversible.Utterances.test_the_words_go_with_the_recording_days_but_held_calls_are_kept",)),
    ("判定の料金の名前を、緊急の道で答えた分も直接の道の名前にする", "src/salescall/core/usage.py",
     "    if errors.is_emergency_route(route):\n        return JUDGE_EMERGENCY_SERVICE\n",
     "",
     (_TA + "SameNames.test_the_judge_price_name_follows_the_route",)),
    # ── 別の耳は緊急の道で補えない・甚大でないおかしな動きは警告だけ（2026-10-31 管理者のご決定。0160）
    ("別の耳が止まっても、判定と同じ（緊急の道で再開できる）にする", "src/salescall/core/errors.py",
     '        if e.ai.startswith(EAR_AI):\n            return "ear_stopped"\n', '',
     (_TA + "Shape.test_three_kinds_by_ai_and_route",
      _TA + "VerificationEar.test_a_stopped_ear_after_the_call_stops_the_next_calls_but_not_the_talking_one",
      _TA + "VerificationEar.test_an_ear_stopping_after_reopening_on_the_emergency_route_stops_again")),
    ("緊急の道で再開できない理由を、補えない種類ごとに出さない（ダッシュボードに、なぜ再開できないかが出ない）",
     "src/salescall/core/stops.py", "        return errors.EMERGENCY_CANNOT[blocked]", '        return "回復を待ってください"',
     (_TA + "VerificationEar.test_a_stopped_ear_after_the_call_stops_the_next_calls_but_not_the_talking_one",)),
    ("甚大でないおかしな動きでも止める", "src/salescall/core/ai_troubles.py",
     'ERROR_SOURCE = {STOPPED: "ai_trouble", GLITCH: "ai_glitch"}', 'ERROR_SOURCE = {STOPPED: "ai_trouble", GLITCH: "ai_trouble"}',
     (_TA + "Review25.test_a_glitch_that_did_not_end_the_call_only_warns",)),
    ("甚大でないおかしな動きを、警告せずに書くだけにする", "src/salescall/core/ai_troubles.py",
     'ERROR_SOURCE = {STOPPED: "ai_trouble", GLITCH: "ai_glitch"}', 'ERROR_SOURCE = {STOPPED: "ai_trouble"}',
     (_TA + "Review25.test_a_glitch_that_did_not_end_the_call_only_warns",)),
    # ── 見直し26b（0161）
    ("断りで行を終えた通話の決定を取り消しても、あとに発信があれば、行の予定と結果を決め直さない", "src/salescall/core/settle.py",
     '            resettle(tx, last["attempt_id"])', "            pass",
     (_TR + "test_revoking_a_refusal_that_ended_a_row_with_a_later_dial_puts_the_row_back",)),
    ("あとでお客様と話した通話があっても、前の通話の断りで行を終える", "src/salescall/core/settle.py",
     '    return not waiting and (state == "do_not_call" or complaint) and not _later_talk(tx, a)',
     '    return not waiting and (state == "do_not_call" or complaint)',
     (_TR + "test_a_refusal_decided_after_a_later_call_talked_keeps_the_later_calls_plan",)),
    ("確かめのかけ直しで決まった断りを差し戻しても、締め直しで断りが出直す", "src/salescall/core/settle.py",
     'check_rang=_check_rang(next_action.check_outcome(tx, attempt_id)) and "refusal" not in gone,',
     "check_rang=_check_rang(next_action.check_outcome(tx, attempt_id)),",
     (_TR + "test_a_sent_back_check_call_refusal_does_not_come_back",)),
    ("強制かけないにしても、確定の印の無い結果の版を確かめ直さない", "src/salescall/core/exclusions.py",
     "            approval.after_ended(tx, task_id)", "            pass",
     ("tests.test_settle.Review26a.test_forcing_no_call_stops_waiting_for_the_unclear_cut_check",)),
    ("まだ分からない電話を、ほかの回線の予約が取り消されたあとに締めると、出なかった電話として予定を書き直す", "src/salescall/core/settle.py",
     '    if (_later_dial(tx, a) or health.blame_of(tx, attempt_id) == "pending") \\', "    if _later_dial(tx, a) \\",
     ("tests.test_settle.Review26a.test_an_unrung_call_settled_after_another_lines_claim_was_dropped_keeps_the_plan",)),
    # ── 見直し26c
    ("回線ごとの流れの頭で、残った試行を後始末しない", "src/salescall/supervisor/line.py",
     "    access.in_tenant(actor, tenant_id, lambda tx: leases.recover_left(tx, holder, line_id))\n", "",
     ("tests.test_supervisor.Review26c.test_a_call_whose_end_could_not_be_recorded_does_not_stop_the_line",)),
    ("書けなかった分があって戻った通話を、会話の部品の不具合として終える", "src/salescall/supervisor/line.py",
     "            if store_error is not None:", "            if False:",
     ("tests.test_supervisor.Review26c.test_a_call_that_could_not_be_written_is_the_storages_fault",)),
    ("ほかの担当の部品を待つ仕事も、締め・後始末と同じ流れの組で回す", "src/salescall/supervisor/main.py",
     "        return self.heavy_pool if job.heavy else self.parts_pool if job.parts else self.pool",
     "        return self.heavy_pool if job.heavy else self.pool",
     ("tests.test_supervisor.Review26c.test_jobs_that_wait_on_other_parts_run_in_their_own_pool",)),
    ("かけられなくなった時刻を、毎回、取引の時刻から数え直す（知らせが回すたびに出る）", "src/salescall/core/health.py",
     "    since = since or run.now - timedelta(seconds=lasted)", "    since = run.now - timedelta(seconds=lasted)",
     ("tests.test_supervisor.Review26c.test_a_line_stuck_in_a_call_is_told_once_per_episode",)),
    ("回線を返せたかを見る前に、借りている印を外す", "src/salescall/supervisor/main.py",
     "        tenant_id = self.leased.get(line_id)\n        if tenant_id is None:\n            return\n        def back(",
     "        tenant_id = self.leased.pop(line_id, None)\n        if tenant_id is None:\n            return\n        def back(",
     ("tests.test_supervisor.Review26c.test_a_line_is_marked_returned_only_after_it_was_returned",)),
    ("止めている途中の流れでは、通話の部品が戻らなくても立て直さない", "src/salescall/supervisor/main.py",
     "                if stuck is not None:   # 通話の部品が戻らない", "                if stuck is not None and not w.stopping:   # 通話の部品が戻らない",
     ("tests.test_supervisor.Review26c.test_a_stuck_call_in_a_flow_being_stopped_is_abandoned_too",)),
    ("締めのあとの確定を、かけたときのモードでなく、いまの業務のモードで決める", "src/salescall/core/approval.py",
     "    if _in_verification(tx, task_id, version_no):\n        return []", "    if _human(tx, task_id):\n        return []",
     (_TR + "test_the_stage_of_a_release_follows_the_mode_when_called_and_only_people_confirm_verification",)),
    # ── 2026-10-31 管理者のご判断 伺い3（言ってはいけないこと＋分からない切れ方は AI で確かめ、決められなければ設定）
    ("言ってはいけないことを言った通話の、分からない切れ方を断りの候補にしない（断ったかが分からないお客様にほかの行からかける）",
     "src/salescall/core/judge.py", '    if raw_cut == "unclear" and inp.forbidden:', "    if False:",
     (_FU + "test_it_is_held_and_not_called_again_until_the_ai_rechecks_when_the_ai_did_not_check",)),
    ("どの AI もそうだと読んでいない断りの候補を、2つ目の判定とそろったかで見る（その場で決まる）",
     "src/salescall/core/judge.py", '            undecided.add("refusal")', '            unsure.add("refusal")',
     (_FU + "test_it_is_held_and_not_called_again_until_the_ai_rechecks_when_the_ai_did_not_check",)),
    ("フルオートで、どの AI もそうだと読んでいない中身を待たない", "src/salescall/core/approval.py",
     '(frozenset() if irr.check == "agreed" else irr.unsure) | irr.undecided)', '(frozenset() if irr.check == "agreed" else irr.unsure))',
     (_FU + "test_it_is_held_and_not_called_again_until_the_ai_rechecks_when_the_ai_did_not_check",)),
    ("どの AI もそうだと読んでいない中身だけの保留を「2つ目の判定がそろった」と書く", "src/salescall/core/approval.py",
     '    check = "none" if need <= irr.undecided else irr.check', "    check = irr.check",
     (_FU + "test_it_is_held_and_not_called_again_until_the_ai_rechecks_when_the_ai_did_not_check",)),
    ("設定で決まったとみなした保留も、締めの処理が待つ", "src/salescall/core/settle.py",
     "    wait = found if found is not None and found.holds else None", "    wait = found",
     (_FU + "test_when_the_ai_cannot_decide_the_setting_chooses",)),
    ("あとから設定を「はい」にしても、待っていた保留を締め直さない", "src/salescall/core/settle.py",
     ' \\\n                or (w["kind"] == "recording" and approval.assumable(tx, w["task_id"], w["attempt_id"]) is not None):', ":",
     (_FU + "test_when_the_ai_cannot_decide_the_setting_chooses",)),
    ("契約者に求めていた保留が、設定で決まったとみなされても、版を足さない", "src/salescall/core/approval.py",
     ' \\\n            and (cur["checked"] == ASSUMED) == assumed:', ":",
     (_FU + "test_when_the_ai_cannot_decide_the_setting_chooses",)),
    ("差し戻した、どの AI もそうだと読んでいない中身を、その場で決めた誤りに数える", "src/salescall/core/approval.py",
     "\"   AND NOT coalesce(r.snapshot->'irreversible'->'undecided' ? p, false)\"", "\"\"",
     (_FU + "test_verification_mode_always_waits_for_the_contractor_and_a_send_back_is_not_counted_as_wrong",)),
    ("断りを外すと答えた通話でも、言ってはいけないことと分からない切れ方から断りの候補を出し直す", "src/salescall/core/settle.py",
     '                     forbidden=attempts.forbidden_speech(tx, attempt_id) and "refusal" not in gone)',
     "                     forbidden=attempts.forbidden_speech(tx, attempt_id))",
     (_FU + "test_sent_back_it_is_not_a_refusal_the_row_ends_and_other_rows_are_free",)),
    # ── 伺い4（範囲の日は依頼者のかける日の中だけ）
    ("日の範囲（「月末」「来週」）も、お客様がその日を言ったとする（依頼者の休みの日にかける）", "src/salescall/core/spoken_time.py",
     "    return days is None or cycle == _WEEKEND_CYCLE", "    return True",
     ("tests.test_spoken_time.NamedDay.test_named_days_and_today_are_said_ranges_are_not",)),
    ("「週末」を、お客様がその日を言ったとしない", "src/salescall/core/spoken_time.py",
     "    return days is None or cycle == _WEEKEND_CYCLE", "    return days is None",
     ("tests.test_spoken_time.NamedDay.test_named_days_and_today_are_said_ranges_are_not",)),
    ("1日だけの日に日の種類が付くと（「明日の平日」）、名指しの日としない（依頼者の休みのその日にかけない）", "src/salescall/core/spoken_time.py",
     "    if days is not None and days[1] is not None and days[0] == days[1]:\n        return True",
     "    if False:\n        return True",
     ("tests.test_spoken_time.NamedDay.test_named_days_and_today_are_said_ranges_are_not",)),
    ("同じ日にまたかけてよいかの材料の「不在で時刻が取れなかった」を、判定と別に「時刻の言葉が無い」で決める（「後ほど」の不在を数えない）",
     "src/salescall/core/call_report.py",
     "        asked += [(c.evidence.turn, c.again) for c in self.callbacks if untimed(c)]",
     "        asked += [(c.evidence.turn, c.again) for c in self.callbacks if c.reason in UNTIMED_OUTCOMES and c.time_said is None]",
     ("tests.test_call_report_cl1.Untimed1101.test_an_absence_with_words_but_no_time_may_be_called_again_today",)),
    ("お客様と話したかを、試行が ended まで進んだかだけで決める（留守番電話で終えた電話で、休んだ行が復旧する。見直し29c の 6）",
     "src/salescall/core/health.py",
     '    if a["ended_clean"] and call_report.talked_in(tx, a["attempt_id"]):', '    if a["ended_clean"]:',
     ("tests.test_health.Places.test_a_call_that_reached_only_a_voicemail_is_no_recovery",)),
    ("日の種類の「平日」を、お客様がその日を言ったとする（依頼者の休みの平日にかける。伺い4 の当てはめ）", "src/salescall/core/spoken_time.py",
     "        return set(kinds) <= _REST_KINDS", "        return True",
     ("tests.test_spoken_time.NamedDay.test_named_days_and_today_are_said_ranges_are_not",)),
    ("「土日祝」「休日」を、お客様がその日を言ったとしない（休みの日を選んだお客様にかけない）", "src/salescall/core/spoken_time.py",
     "        return set(kinds) <= _REST_KINDS", "        return False",
     ("tests.test_spoken_time.NamedDay.test_named_days_and_today_are_said_ranges_are_not",)),
    # ── 伺い5（データベースの一時的な止まりは「違う3行」に数えない・戻るまで次の電話をかけない）
    ("データベースの一時的な止まりも、違う行の数に数える", "src/salescall/core/errors.py",
     " and ACTIONS[kind].stop_cause in TABLE_CAUSES\n", "\n",
     (_OT + "test_a_database_stall_on_three_rows_does_not_stop_the_contractor",)),
    ("こちらの不具合の電話を、種類を見ずに契約者の欄へ渡す", "src/salescall/core/health.py",
     '    to_tenant = code == "E5" or (code == "E3" and errors.counts_rows(a["kind"]))', '    to_tenant = code in ("E3", "E5")',
     (_OT + "test_a_database_stall_on_three_rows_does_not_stop_the_contractor",)),
    ("データベースの止まりで終えたことを、回線ごとの処理へ返さない", _LINE,
     '                                 storage=call.end_reason == "storage_stuck")', "                                 storage=False)",
     (_OT + "test_a_line_whose_call_ended_on_a_database_stall_waits_until_writes_come_back",)),
    ("書き込みが戻ったかを、長さを見ずに戻ったとする", _LINE,
     "    return time.monotonic() - began <= limit[0]", "    return True",
     (_OT + "test_a_line_whose_call_ended_on_a_database_stall_waits_until_writes_come_back",)),
    ("データベースの止まりで終えた回線が、書き込みが戻ったかを確かめずに次の電話をかける", _LINE,
     "            storage = got.storage\n", "            storage = False\n",
     (_OT + "test_a_line_whose_call_ended_on_a_database_stall_waits_until_writes_come_back",)),
    ("回線の1回分が落ちたあと、書き込みが戻ったかを確かめずに次の電話へ進む", _LINE,
     "            storage = True\n            stop.wait(IDLE_SECONDS)", "            stop.wait(IDLE_SECONDS)",
     (_OT + "test_a_line_whose_call_ended_on_a_database_stall_waits_until_writes_come_back",)),
    # ── 事実を伺う質問か（CL-1 の連絡帳 2-1 の版 0.6。✅ 2026-11-01 ユーザー：(a)「OK」）
    ("事実を伺う質問にも「どちらでも」を返りうる答えに足す（「お任せします」を取れた答えにする）", _BS,
     '((ANY_CHOICE, NONE_CHOICE) if q.type == "choice" and not q.asks_fact else ())',
     '((ANY_CHOICE, NONE_CHOICE) if q.type == "choice" else ())',
     ("tests.test_business_settings.Check.test_a_question_asking_a_fact_has_no_any_answer",)),
    ("事実を伺う質問かが真偽でなくても通す", _BS,
     '            if not isinstance(q["asks_fact"], bool):\n', '            if False:\n',
     ("tests.test_business_settings.Check.test_a_question_asking_a_fact_has_no_any_answer",)),
    ("書かれた「事実を伺う質問ではない」を読まない（どの質問も事実を伺う質問にする）", "src/salescall/core/business_settings.py",
     '                                q["asks_fact"] is not False, do_words, away, intro,\n',
     '                                True, do_words, away, intro,\n',
     ("tests.test_business_settings.Check.test_a_question_asking_a_fact_has_no_any_answer",)),
    ("取込で、事実を伺う質問かが決まらなかった質問を「事実を伺う質問ではない」にする", _BI,
     '            q["asks_fact"] = (ai.get("facts") or {}).get(text, True)', '            q["asks_fact"] = (ai.get("facts") or {}).get(text, False)',
     ("tests.test_business_import.Import.test_whether_a_question_asks_a_fact_is_decided_without_a_person",)),
    ("取込で、AI の形の違う答えを、そのまま事実を伺う質問かに入れる", _BI,
     "    return {t: v is not False for t, v in got.items() if t in texts}", "    return {t: v for t, v in got.items() if t in texts}",
     ("tests.test_business_import.Import.test_whether_a_question_asks_a_fact_is_decided_without_a_person",)),
    # ── 見直し28
    ("データベースの一時的な止まりも、違う行の数に数える（こちらの不具合の処置を作る1か所の rows を見ない）", "src/salescall/core/errors.py",
     '    return Action("keep", "none", "program_trouble" if rows else None,', '    return Action("keep", "none", "program_trouble",',
     (_OT + "test_a_database_stall_on_three_rows_does_not_stop_the_contractor",)),
    ("知らない判定の名前で、判定の料金の名前が落ちる（その判定の利用量が残らない）", "src/salescall/core/usage.py",
     "    return JUDGE_SERVICES.get(name, JUDGE_UNKNOWN + name)", "    return JUDGE_SERVICES[name]",
     ("tests.test_ai_troubles.SameNames.test_the_judge_price_name_follows_the_route",)),
    ("事実を伺う質問か（asks_fact）の無い前の版・断りの答えを業務の決まりに持つ前の版を、今の形に直さずに読む（前に置いた版が読めない）",
     "src/salescall/core/business_settings.py",
     '    return _current_shape(row["content"], row["rules"])',
     '    return dict(row["content"], rules=row["rules"])',
     ("tests.test_business_settings.Versions.test_a_version_saved_before_asks_fact_is_read_as_fact_questions",
      "tests.test_business_settings.Versions.test_a_version_saved_with_decline_rules_reads_them_as_final_goals")),
    ("取込で、文が空の質問の事実を伺う質問かを埋めない（同じ質問に問題が2つ出る）", _BI,
     '    for q in (raw.get("questions") if isinstance(raw.get("questions"), list) else []):\n        # 事実を伺う質問か',
     '    for q in _fact_questions(raw):\n        # 事実を伺う質問か',
     ("tests.test_business_import.Import.test_whether_a_question_asks_a_fact_is_decided_without_a_person",)),
    ("決まったとみなした中身を、いちばん新しい保留の版でしか覚えていない（別の中身の待ちのあと、断りをまた契約者に求める）",
     "src/salescall/core/approval.py",
     '"SELECT parts FROM salescall.decision_waits WHERE attempt_id = %s AND checked = %s",\n'
     '                                          (attempt_id, ASSUMED))',
     '"SELECT parts FROM salescall.decision_waits WHERE attempt_id = %s AND checked = %s"\n'
     '                                          " AND seq = salescall.decision_wait_seq(%s)", (attempt_id, ASSUMED, attempt_id))',
     (_FU + "test_an_assumed_part_stays_settled_after_newer_waits",)),
    ("AIの停止を解く確かめの間隔を、確かめの間隔より延ばす（ばたつきの待ち。ご決定「確かめが通れば自動で再開します」に無い）",
     "src/salescall/core/watch.py", "    next_at = now + timedelta(seconds=interval)\n",
     "    next_at = now + timedelta(seconds=interval * 2)\n",
     ("tests.test_watch.TenantAiStopsAndChecks.test_a_stop_again_soon_does_not_lengthen_the_wait",)),
    ("決まったとみなした中身も、締め直しでまた待つ（設定を戻すと、もう発信禁止を書いたお客様について契約者に答えを求め直す）",
     "src/salescall/core/approval.py", "    confirmed = _settled(tx, attempt_id)", '    confirmed = _answered(tx, attempt_id, "confirmed")',
     (_FU + "test_when_the_ai_cannot_decide_the_setting_chooses",)),
    ("手で再開した回線は、声で止めた回数まで0に戻す（再開の形が2つになる。L-21）", _RULES,
     "    return _Step(_resumed(line), (_do(RECORD, about=f\"{ev.by}が手で再開した：{ev.reason}\"),",
     "    return _Step(Line(LineStage.HEALTHY), (_do(RECORD, about=f\"{ev.by}が手で再開した：{ev.reason}\"),",
     (_HR + "Decisions.test_1029_after_a_ringing_test_one_more_poor_call_stops_it_again_and_the_gaps_grow",
      _HR + "Decisions.test_1015_a_person_in_charge_can_restart_by_hand_and_counts_start_again")),
    ("書き込みが戻っても、データベースの止まりで待つ行をかけ直さない（ほかの行の電話を話し終えるまで待つ。R-6 の E21）", _RULES,
     '         or (e.code in ("E11", "E21") and e.kind == s.kind)),', '         or (e.code == "E11" and e.kind == s.kind)),',
     (_HR + "Decisions.test_1031_a_database_stall_is_called_back_when_writes_come_back",
      _OT + "test_a_database_stall_on_three_rows_does_not_stop_the_contractor")),
    ("書き込みが戻っても、復旧待ちの行へ伝えない（データベースの止まりで終えた行が、お詫びとともにかけ直されない）",
     "src/salescall/supervisor/jobs.py",
     "    line.timed_write(ctx.access, ctx.tenant_id, health.storage_back)\n", "    pass\n",
     (_OT + "test_the_recovery_job_reaches_rows_whatever_happened_to_the_line",
      _OT + "test_a_database_stall_on_three_rows_does_not_stop_the_contractor",
      "tests.test_supervisor.Rounds.test_the_call_knows_by_itself_when_it_is_finishing_and_never_waits_a_fixed_time")),
    ("書き込みが戻ったら、休みの行（同じ行で2回止まった）にも、契約者の書き込みの確かめだけで伝える（その行の書き込みを確かめずに戻す・"
     "1つの行の鍵が固まると、復旧待ちの行まで戻らない。伺い10 は行ごとの確かめ＝storage_row_back）",
     "src/salescall/core/health.py",
     '    (waiting,) = run.take((TASK, Stages(("await_recovery",))))\n    ev = hr.Event("E21"',
     '    (waiting,) = run.take((TASK, Stages(("await_recovery", "resting"))))\n    ev = hr.Event("E21"',
     (_OT + "test_only_rows_resting_on_database_stalls_alone_come_back_and_a_stuck_row_does_not_stop_the_others",)),
    ("書き込みが戻らない続きの印を、戻ったときに数え直さない（2つ目の止まりの続きでは、知らせも修正依頼も出ない）", _LINE,
     "                storage = told = False", "                storage = False",
     (_OT + "test_a_line_whose_call_ended_on_a_database_stall_waits_until_writes_come_back",)),
    ("書き込みが戻らない続きを、確かめのたびに記録する（同じ続きで知らせが重なる）", _LINE,
     "                    if not told:\n", "                    if True:\n",
     (_OT + "test_a_line_whose_call_ended_on_a_database_stall_waits_until_writes_come_back",)),
    ("書き込みが戻らない続きを、記録も知らせもしない（回線が黙って止まり続ける）", _LINE,
     "                        storage_not_back(access, tenant_id, line)\n", "                        pass\n",
     (_OT + "test_a_line_whose_call_ended_on_a_database_stall_waits_until_writes_come_back",)),
    ("書き込みの確かめに時間切れを付けない（データベースが固まると、回線の流れごと止まる）", _LINE,
     "        access.in_tenant(ACTOR, tenant_id, work, time_limit=bound)", "        access.in_tenant(ACTOR, tenant_id, work)",
     (_OT + "test_a_line_whose_call_ended_on_a_database_stall_waits_until_writes_come_back",
      _OT + "test_the_recovery_job_reaches_rows_whatever_happened_to_the_line")),
    ("取引を始める1か所が、渡された時間切れを付けない", "src/salescall/core/access.py",
     "                        if time_limit is not None:\n", "                        if False:\n",
     (_OT + "test_a_line_whose_call_ended_on_a_database_stall_waits_until_writes_come_back",
      _OT + "test_the_recovery_job_reaches_rows_whatever_happened_to_the_line")),
    ("書き込みの確かめが時間切れでも、戻ったとする", _LINE,
     "    except TimedOut:\n        return False", "    except TimedOut:\n        return True",
     (_OT + "test_a_line_whose_call_ended_on_a_database_stall_waits_until_writes_come_back",)),
    ("その行に要る名前の録音に、取り次げないの確かめの一言を入れない（その一言を流せない）", "src/salescall/core/recordings.py",
     '             "promise_again", "unavailable_confirm") + (("target_successor",) if corporate else ())',
     '             "promise_again") + (("target_successor",) if corporate else ())',
     ("tests.test_gate.NameLedger.test_what_a_row_needs",)),
    ("契約者の単位の録音に、お取り次ぎいただける日時を伺う一言が無くても話す", "src/salescall/core/recordings.py",
     '"callback_refusal", "callback_transfer_time", "closing",', '"callback_refusal", "closing",',
     ("tests.test_gate.NameLedger.test_the_parts_cl1_added_on_11_01_are_received",)),
    ("契約者の単位の録音に、ご本人へのご遠慮の確かめ（callback_refusal）が無くても話す（CL-1 の 4-63 (a)。その一言を流せない）",
     "src/salescall/core/recordings.py",
     '"callback_reachable_time", "callback_refusal", "callback_transfer_time",', '"callback_reachable_time", "callback_transfer_time",',
     ("tests.test_gate.NameLedger.test_the_refusal_confirm_cl1_added_on_11_01_is_received",)),
    ("契約者の単位の録音に、急いでいる方への伺い（callback_hurry_again）が無くても話す（CL-1 の 4-64 (a)）", "src/salescall/core/recordings.py",
     '"callback_email", "callback_hurry_again", "callback_hurry_when",', '"callback_email", "callback_hurry_when",',
     ("tests.test_gate.NameLedger.test_the_hurry_ask_and_the_thinking_prompt_cl1_added_on_11_01_are_received",)),
    ("質問の番号に、急いでいる方への伺いの番号 H1 を使える（CL-1 の 4-64 (c)）", _BS,
     '"D1", "H1", "N1", "N2"', '"D1", "N1", "N2"',
     ("tests.test_gate.NameLedger.test_the_hurry_ask_and_the_thinking_prompt_cl1_added_on_11_01_are_received",)),
    ("判定が届かず会話AIの読みで締めた断りを、取り消せない決定の候補にしない（もうかけない印の無いお客様にまたかける）",
     "src/salescall/core/call_report.py",
     "        return bool(self.refusals) or self.unjudged_refusal", "        return bool(self.refusals)",
     ("tests.test_call_report_cl1.Hurry1101.test_an_unjudged_refusal_waits_for_the_recording_check",)),
    ("判定が届かず会話AIの読みで締めた断りを、報告の irreversible_check のとおり（そろった）として、その場で発信禁止にする",
     "src/salescall/core/judge.py",
     '    check = "agreed" if not unsure else "none" if unjudged or r is None else r.irreversible_check',
     '    check = "agreed" if not unsure else "none" if r is None else r.irreversible_check',
     ("tests.test_call_report_cl1.Hurry1101.test_an_unjudged_refusal_waits_for_the_recording_check",)),
    ("判定が届かず会話AIの読みで締めた怒っていたを、苦情の候補にしない", "src/salescall/core/call_report.py",
     "        return bool(self.complaints) or self.unjudged_complaint", "        return bool(self.complaints)",
     ("tests.test_call_report_cl1.Hurry1101.test_an_unjudged_refusal_waits_for_the_recording_check",)),
    ("差し戻し・取り消しで断りを外しても、判定が届かずに締めた断りが残る（締め直しでまた断りの候補になる）",
     "src/salescall/core/call_report.py",
     "                       unjudged_ends=tuple(u for u in self.unjudged_ends if u.reason not in drop_unjudged),\n", "",
     ("tests.test_call_report_cl1.Hurry1101.test_an_unjudged_refusal_waits_for_the_recording_check",)),
    ("最終目的を断られて締めた通話を、用件を断られたと読まない（行が終わらず、断ったお客様にまたかける）", "src/salescall/core/judge.py",
     "    if r and r.goal_refusals:\n", "    if False:\n",
     ("tests.test_call_report_cl1.Hurry1101.test_a_refused_final_goal_ends_the_row_without_a_do_not_call",
      "tests.test_judge.Order.test_only_the_goal_refused_fact_is_declined")),
    ("質問の番号に、ご遠慮の確かめの番号 R1 を使える（CL-1 の 4-63 (b)。業務の答えとご遠慮の答えを取り違える）", _BS,
     '"S1", "P1", "R1", "D1"', '"S1", "P1", "D1"',
     ("tests.test_gate.NameLedger.test_the_refusal_confirm_cl1_added_on_11_01_is_received",)),
    ("質問の番号に、取り次げないの確かめの一言の名前を使える（会話の側で取り違える）", _BS,
     '"target_full", "target_successor_full", "unavailable_confirm",', '"target_full", "target_successor_full",',
     ("tests.test_gate.NameLedger.test_the_parts_cl1_added_on_11_01_are_received",)),
    ("取込で、事実を伺う質問かを AI に聞かない", _BI,
     '    facts = _decided_facts(sorted({q["text"] for q in _fact_questions(raw)}), decide_facts)\n', '    facts = {}\n',
     ("tests.test_business_import.Import.test_whether_a_question_asks_a_fact_is_decided_without_a_person",)),
    # ── CL-1 の連絡帳 4-65（2026-11-02 ✅ ユーザー）：こちらがすることの言葉・入る一言・依頼者がすること・フルネームと部署・お名前だけでは
    #    どの方か分からなかった・使えない名前
    ("「その理由のまま締めない」締めた理由（お名前だけでは分からない・理由を言わずに取り次げない）で締めた報告を受ける",
     "src/salescall/core/call_report.py",
     "    if any(e.reason in NEVER_CLOSES for e in ends):", "    if False:",
     ("tests.test_call_report_cl1.Anyone18b.test_which_facts_show_a_person_is_decided_in_one_table",)),
    ("お名前だけではどの方か分からなかった行を、システムが終えずに業務の方針でかけ直す", "src/salescall/core/business_rules.py",
     'SYSTEM_POLICIES["not_reached_who_unclear"] = Policy("end", None, None)', 'pass',
     ("tests.test_settle.SameDayAndSuccessor.test_who_unclear_ends_the_row_and_returns_the_blanks_to_fill",)),
    ("依頼者に埋めていただく欄を、空いていない欄にする", _BS,
     "    return tuple(f for f in WHO_UNCLEAR_FIELDS if not have[f])", "    return tuple(f for f in WHO_UNCLEAR_FIELDS if have[f])",
     ("tests.test_gate.NameLedger.test_the_full_name_call_needs_its_parts_only_when_it_adds_something",
      "tests.test_settle.SameDayAndSuccessor.test_who_unclear_ends_the_row_and_returns_the_blanks_to_fill")),
    ("お名前だけでは分からなかった行の結果に、埋めていただく欄を残さない", "src/salescall/core/settle.py",
     '                                           if j.detail.get("not_reached") == "who_unclear" else {})},',
     '                                           if False else {})},',
     ("tests.test_settle.SameDayAndSuccessor.test_who_unclear_ends_the_row_and_returns_the_blanks_to_fill",)),
    ("フルネームでお呼びし直す一言の名前の部分を、台帳で求めない（お名字の録音だけでかけ、フルネームの一言が流せない）",
     "src/salescall/core/recordings.py",
     '        full = target["full_reading"] if target["full_name"] and target["full_name"] != target["name"] else reading',
     "        full = reading",
     ("tests.test_gate.NameLedger.test_the_full_name_call_needs_its_parts_only_when_it_adds_something",)),
    ("部署でお呼びし直す一言の役職の部分を、台帳で求めない", "src/salescall/core/recordings.py",
     '        full_role = (target["department"] + (role or "")) if target["department"] else role', "        full_role = role",
     ("tests.test_gate.NameLedger.test_the_full_name_call_needs_its_parts_only_when_it_adds_something",)),
    ("役職に入っている部署も、会話へ部署として渡す（同じことを2度言う）", _BS,
     '    department = t["target_department"] if uses_name and t["target_department"] and t["target_department"] not in (role or "") \\\n',
     '    department = t["target_department"] if uses_name and t["target_department"] \\\n',
     ("tests.test_gate.NameLedger.test_the_full_name_call_needs_its_parts_only_when_it_adds_something",)),
    ("事実を伺う質問にも、こちらがすることの言葉を付けられる", _BS,
     '            if do_words and q["asks_fact"] is not False:', "            if False:",
     ("tests.test_business_settings.Check.test_do_words_intro_and_client_does",)),
    ("最初の質問にも、入る一言を付けられる（名乗りと一緒に流すので使われない）", _BS,
     "            if intro is not None and n == 1:", "            if False:",
     ("tests.test_business_settings.Check.test_do_words_intro_and_client_does",)),
    ("前の版の質問を、こちらがすることの言葉・入る一言の無いまま読む（足す前に置いた版が読めない）", "src/salescall/core/business_settings.py",
     '    added = {"asks_fact": True, "do_words": {}, "away": [], "intro": None, "ask_if": None,',
     '    added = {"asks_fact": True, "away": [], "ask_if": None,',
     ("tests.test_business_settings.Versions.test_a_version_saved_before_asks_fact_is_read_as_fact_questions",)),
    ("取込で、事実を伺う質問にも、こちらがすることの言葉を AI に聞く", _BI,
     '        if key in _WISH_KEYS and (not isinstance(q, dict) or _asks_fact(q, facts or {})):',
     '        if key in _WISH_KEYS and not isinstance(q, dict):',
     ("tests.test_business_import.Import.test_do_words_intro_and_client_does_come_in",)),
    ("取込で、こちらがすることの言葉の AI の答えを入れない", _BI,
     '        q["do_words"] = {**(ai.get("do_words") or {}).get(_pair_key(q["text"], choices), {}), **given} or None',
     '        q["do_words"] = {**given} or None',
     ("tests.test_business_import.Import.test_do_words_intro_and_client_does_come_in",)),
    ("前のひな形（入る一言の列が無い）を読めない", _BI,
     '            if not hits and f"{role}.{col}" in OPTIONAL_COLUMNS and f"{role}.{col}" not in mapping.get("columns", {}):',
     "            if False:",
     ("tests.test_business_import.Import.test_do_words_intro_and_client_does_come_in",)),
    ("一覧のフルネームの読みが読みとして使えなくても、行を止めない", "src/salescall/core/importer.py",
     "            bad = bad_name or bad_person or bad_full\n", "            bad = bad_name or bad_person\n",
     ("tests.test_import.Targets.test_the_full_name_and_department_are_taken_when_the_list_has_them",)),
    ("想定問答の問いの名前に「お呼びした方」を使える（AI が流し直しで選ぶ部分と取り違える）", _BS,
     '"エーアイか聞かれた", "お呼びした方", "質問に戻る",', '"エーアイか聞かれた", "質問に戻る",',
     ("tests.test_business_settings.Check.test_answers_the_ai_may_give",)),
    # ── 伺い8（2026-11-01）：断られたお客様に、同じ業務の次の実施回でもかけない（業務の設定。かけない理由の1か所）
    ("断られたお客様に、同じ業務の次の実施回でもかける（伺い8 の案1 が効かない）", "src/salescall/core/target_policy.py",
     "    if hit is None and business_id is not None and _declined_in_business(", "    if False and business_id is not None and _declined_in_business(",
     ("tests.test_gate.DataReasons.test_a_customer_who_declined_is_not_called_again_in_the_same_business",)),
    ("業務の設定「次の実施回でもかける」（案2）を見ない", "src/salescall/core/target_policy.py",
     '    if settings.resolve(tx, "business.call_declined_again", **scope):\n        return False\n', "",
     ("tests.test_gate.DataReasons.test_a_customer_who_declined_is_not_called_again_in_the_same_business",)),
    ("管理番号を、お客様を表す変わらない番号としていない業務でも、管理番号で断ったお客様と決める", "src/salescall/core/target_policy.py",
     '    ref = client_ref if client_ref is not None and settings.resolve(tx, "dnc.client_ref_is_stable_id", **scope) else None',
     "    ref = client_ref",
     ("tests.test_gate.DataReasons.test_a_customer_who_declined_is_not_called_again_in_the_same_business",)),
    # ── 伺い10（2026-11-01 管理者「人は甚大な問題以外で介入しない」）：データベースの止まりだけで休んだ行は、行の書き込みの確かめで1回だけ戻す
    ("データベースの止まりで休んだ行を、何度でも書き込みの確かめで戻す（その行に関わる不具合でも、お詫びの電話がかかり続ける）", _RULES,
     "task.kinds == frozenset({kind}) and not task.storage_retried", "task.kinds == frozenset({kind})",
     (_T139 + "test_a_row_resting_on_database_stalls_comes_back_once_by_itself",
      _HR + "Decisions.test_1101_ukagai10_a_row_resting_on_database_stalls_returns_once_without_people")),
    ("ほかの不具合も続いて休んだ行も、書き込みの確かめで戻す（直した版を待たない）", _RULES,
     "task.kinds == frozenset({kind}) and not task.storage_retried", "kind in task.kinds and not task.storage_retried",
     (_T139 + "test_only_rows_resting_on_database_stalls_alone_come_back_and_a_stuck_row_does_not_stop_the_others",
      _HR + "Decisions.test_1101_ukagai10_a_row_resting_on_database_stalls_returns_once_without_people")),
    ("書き込みの確かめで戻した行の続き（n）を0にする（戻した行がまた止まっても、すぐ休み直さず、お詫びの電話がもう1本かかる）", _RULES,
     "    return _Step(_back(task, storage_retried=True), acts", "    return _Step(_back(task, n=0, n_kind=None, storage_retried=True), acts",
     (_T139 + "test_a_row_resting_on_database_stalls_comes_back_once_by_itself",
      _HR + "Decisions.test_1101_ukagai10_a_row_resting_on_database_stalls_returns_once_without_people")),
    ("予定どおりへ戻すときに、書き込みの確かめで戻したかを落とす（休み直した行を、また書き込みの確かめで戻す）", _RULES,
     '                storage_retried=keep.get("storage_retried", task.storage_retried))', '                storage_retried=keep.get("storage_retried", False))',
     (_T139 + "test_a_row_resting_on_database_stalls_comes_back_once_by_itself",)),
    ("こちらの不具合なしにこの行の電話が鳴っても、書き込みの確かめで戻したかを残す（次の止まりの続きで戻せない）", _RULES,
     "    return _Step(_back(task, n=0, n_kind=None, storage_retried=False))", "    return _Step(_back(task, n=0, n_kind=None))",
     (_HR + "Decisions.test_1101_ukagai10_a_row_resting_on_database_stalls_returns_once_without_people",)),
    ("直した版で戻っても、書き込みの確かめで戻したかを残す", _RULES,
     "    return _Step(_back(task, n=0, n_kind=None, storage_retried=False), acts", "    return _Step(_back(task, n=0, n_kind=None), acts",
     (_T139 + "test_a_row_resting_on_database_stalls_comes_back_once_by_itself",
      _HR + "Decisions.test_1101_ukagai10_a_row_resting_on_database_stalls_returns_once_without_people")),
    ("データベースの止まりで休んだ行を、決まった間隔の仕事が戻さない（直した版か人の手を待つ＝伺い10 の前）", "src/salescall/supervisor/jobs.py",
     "        for task_id in rows:\n            line.timed_write(", "        for task_id in ():\n            line.timed_write(",
     (_T139 + "test_a_row_resting_on_database_stalls_comes_back_once_by_itself",)),
    ("休んだ行を、1つの取引でまとめて確かめる（1つの行の書き込みが固まると、ほかの行も戻らない）", "src/salescall/supervisor/jobs.py",
     "    if line.timed_write(ctx.access, ctx.tenant_id, lambda tx: rows.extend(health.storage_rows(tx))):\n"
     "        for task_id in rows:\n"
     "            line.timed_write(ctx.access, ctx.tenant_id, lambda tx, t=task_id: health.storage_row_back(tx, t))",
     "    line.timed_write(ctx.access, ctx.tenant_id,\n"
     "                     lambda tx: [health.storage_row_back(tx, t) for t in health.storage_rows(tx)])",
     (_T139 + "test_only_rows_resting_on_database_stalls_alone_come_back_and_a_stuck_row_does_not_stop_the_others",)),
    # ── CL-1 の連絡帳 4-66（2026-11-02 ✅ ユーザー）：選ぶ質問の「どれも要らない」・前の答えで伺う質問・説明した内容のご承認
    ("選ぶ質問に「どれも要らない」を足さない（会話の側が返す答えの値で、答えの書き込みが断られる）", _BS,
     '((ANY_CHOICE, NONE_CHOICE) if q.type == "choice" and not q.asks_fact else ())',
     '((ANY_CHOICE,) if q.type == "choice" and not q.asks_fact else ())',
     ("tests.test_business_settings.Check.test_none_is_a_system_answer_of_choice_questions",)),
    ("依頼者の選択肢に「どれも要らない」と書ける（システムの答えと取り違える）", _BS,
     "                if c in SYSTEM_CHOICES or c in (ANY_CHOICE, NONE_CHOICE) or c in OUTPUT_MARKS or c in (END, ANY):",
     "                if c in SYSTEM_CHOICES or c in (ANY_CHOICE,) or c in OUTPUT_MARKS or c in (END, ANY):",
     ("tests.test_business_settings.Check.test_none_is_a_system_answer_of_choice_questions",)),
    ("「どれも要らない」を、申し出を断る答えにしない（最終目的の申し出で「どれも要らない」と言われた道を、必須を飛ばす道として通さない）",
     "src/salescall/core/business_settings.py",
     "    return frozenset(q.declines) | ({NONE_CHOICE} & set(possible)) | (",
     "    return frozenset(q.declines) | (",
     ("tests.test_business_settings.Check.test_none_is_a_system_answer_of_choice_questions",)),
    ("ご承認の質問の「いいえ」を、申し出を断る答えにしない（会話の側の Question.declined と食い違う）", "src/salescall/core/business_settings.py",
     "        {q.choices[1]} if q.approval and len(q.choices) > 1 else set())", "        set())",
     ("tests.test_business_settings.Check.test_final_goals_their_decline_answers_and_how_the_goal_is_met",)),
    ("最終目的が2つ以上なのに、果たし方を書かなくても通す（会話の側の決まり any／all をこちらで推し量る）", "src/salescall/core/business_settings.py",
     "    if goals >= 2 and goal_mode not in GOAL_MODES:", "    if False:",
     ("tests.test_business_settings.Check.test_final_goals_their_decline_answers_and_how_the_goal_is_met",)),
    ("断る答えの無い最終目的の申し出を通す（断られても目的が果たせなくならず、会話の側が締められない）", "src/salescall/core/business_settings.py",
     "        if question.final_goal and not declined_answers(question):", "        if False:",
     ("tests.test_business_settings.Check.test_final_goals_their_decline_answers_and_how_the_goal_is_met",)),
    ("崩れた断る答えの質問も読めたとして、分岐を確かめる（必須を聞かずに終わる道のうその理由を出す）", "src/salescall/core/business_settings.py",
     "            if declines is None:\n                continue\n", "            if declines is None:\n                declines = ()\n",
     ("tests.test_business_import.Import.test_malformed_input_is_a_problem_not_a_crash",)),
    ("前の版の業務の決まりの断りの答えを、最終目的の申し出として読まない（前の版で、用件を断られたお客様にかけ直す）",
     "src/salescall/core/business_settings.py",
     '            out["final_goal"], out["declines"] = True, list(old_decline[out["id"]])', "            pass",
     ("tests.test_business_settings.Versions.test_a_version_saved_with_decline_rules_reads_them_as_final_goals",)),
    ("前の版の断りの答えが2つ以上のとき、果たし方を any として読む（前は1つでも断られたら用件を断られた＝all）",
     "src/salescall/core/business_settings.py", '        out["goal_mode"] = "all"', '        out["goal_mode"] = "any"',
     ("tests.test_business_settings.Versions.test_a_version_saved_with_decline_rules_reads_them_as_final_goals",)),
    ("前の版の業務の決まりを、断りの答えを外さずに読む（前に置いた版が読めない）", "src/salescall/core/business_rules.py",
     '    return {k: v for k, v in raw.items() if k != "decline"} if isinstance(raw, dict) else raw', "    return raw",
     ("tests.test_business_settings.Versions.test_a_version_saved_with_decline_rules_reads_them_as_final_goals",)),
    ("前の版の、受け皿の無い分岐に「どれも要らない」の行き先を足さない（足す前に置いた版が読めない）", _BS,
     '            out["next"] = {**nxt, NONE_CHOICE: ids[i + 1] if i + 1 < len(ids) else END}', '            pass',
     ("tests.test_business_settings.Versions.test_a_version_saved_before_none_reads_none_as_going_on",)),
    ("伺う条件の前の質問を、後ろに並ぶ質問・自分でもよいとする（伺う条件が輪になり、どちらも伺わない）", _BS,
     "    src = next((q for q in earlier if isinstance(after, str) and q.id == after), None)",
     "    src = Question(after, '', None, 'choice', False, ('はい',), {}) if isinstance(after, str) else None",
     ("tests.test_business_settings.Check.test_questions_asked_only_after_an_answer_and_approval_questions",)),
    ("伺う条件の答えを、前の質問の答えか確かめない（来ない答えを待ち、いつまでも伺わない）", _BS,
     "    unknown = [a for a in answers if a not in possible_answers(src)]", "    unknown = []",
     ("tests.test_business_settings.Check.test_questions_asked_only_after_an_answer_and_approval_questions",)),
    ("ご承認を伺う質問を、はい／いいえ以外の質問にもできる（ご承認いただけない答えが決まらない）", _BS,
     '            elif q["approval"] and q["type"] != "yes_no":', "            elif False:",
     ("tests.test_business_settings.Check.test_questions_asked_only_after_an_answer_and_approval_questions",)),
    ("必須の質問がすべて伺う条件つきでも通す（どれも伺わない通話が「すべて取れた」になる）", _BS,
     "    elif questions and not any(q.required and q.ask_if is None for q in questions):", "    elif False:",
     ("tests.test_business_settings.Check.test_questions_asked_only_after_an_answer_and_approval_questions",)),
    ("判定へ渡す伺う条件を、置いた版から読まない（伺っていない質問を取れていないと数え、かけ直し続ける）", "src/salescall/core/business_settings.py",
     '            for q in _stored(tx, version_id)["questions"] if q["ask_if"] is not None}',
     '            for q in _stored(tx, version_id)["questions"] if False}',
     ("tests.test_business_settings.Versions.test_questions_asked_after_an_answer_are_read_for_the_judge",
      "tests.test_settle.NotAskedSettle.test_a_question_whose_condition_did_not_hold_is_returned_as_not_asked")),
    ("締めが、判定へ伺う条件を渡さない", "src/salescall/core/settle.py",
     "                     ask_if=business_settings.ask_ifs(tx, rules_version),", "                     ask_if={},",
     ("tests.test_settle.NotAskedSettle.test_a_question_whose_condition_did_not_hold_is_returned_as_not_asked",)),
    ("伺っていない（対象外）の必須の質問を、取れていない質問に数える（かけ直し続ける）", "src/salescall/core/judge.py",
     "    missing = [q for q in rules.required if q not in skipped and (q not in inp.items or inp.items[q].state not in TAKEN)]",
     "    missing = [q for q in rules.required if q not in inp.items or inp.items[q].state not in TAKEN]",
     ("tests.test_judge.NotAsked.test_a_question_whose_condition_did_not_hold_is_not_missing",)),
    ("前の質問の答えをまだ受け取っていないのに、対象外にする（伺うかが決まっていない質問を取れたことにする）", "src/salescall/core/judge.py",
     "(src is not None and src.state in ANSWERED\n",
     "(src is None or True\n",
     ("tests.test_judge.NotAsked.test_a_question_whose_condition_did_not_hold_is_not_missing",)),
    ("前の質問の答えを受け取った「答えたくない」でも、伺う条件を決めない（対象外にしない）", "src/salescall/core/judge.py",
     'ANSWERED = TAKEN | {"dont_know", "refused"}', "ANSWERED = TAKEN",
     ("tests.test_judge.NotAsked.test_a_question_whose_condition_did_not_hold_is_not_missing",)),
    ("前の質問も対象外のとき、その次の質問を対象外にしない", "src/salescall/core/judge.py",
     "        if cond.after in out or (src", "        if (src",
     ("tests.test_judge.NotAsked.test_a_question_after_a_question_that_was_not_asked_is_not_asked_either",)),
    ("伺った質問も、前の答えが違えば対象外にする（取れなかった答えを隠す）", "src/salescall/core/judge.py",
     '        if it is not None and it.state != "empty":\n            continue\n', "",
     ("tests.test_judge.NotAsked.test_a_question_whose_condition_did_not_hold_is_not_missing",)),
    ("伺っていない（対象外）の質問を、依頼者へ返さない", "src/salescall/core/judge.py",
     '            **({"not_asked": sorted(skipped)} if skipped else {}),\n', "",
     ("tests.test_judge.NotAsked.test_a_question_whose_condition_did_not_hold_is_not_missing",)),
    ("ご納得いただけなかった点を、依頼者へ返さない", "src/salescall/core/judge.py",
     '    agreed = inp.report.not_agreed if inp.report is not None else ()',
     '    agreed = ()',
     ("tests.test_judge.NotAsked.test_points_not_agreed_are_returned",
      "tests.test_call_report_cl1.Approval1102.test_points_not_agreed_are_received_and_returned")),
    ("会話の報告のご納得いただけなかった点を読まない", "src/salescall/core/call_report.py",
     "    not_agreed = _not_agreed(facts)\n", "    not_agreed = ()\n",
     ("tests.test_call_report_cl1.Approval1102.test_points_not_agreed_are_received_and_returned",)),
    ("ご納得いただけなかった点で、お答えいただけなかった（null）報告を丸ごと断る", "src/salescall/core/call_report.py",
     "        if said is not None and not isinstance(said, str):", "        if not isinstance(said, str):",
     ("tests.test_call_report_cl1.Approval1102.test_points_not_agreed_are_received_and_returned",)),
    ("ご納得いただけなかった点を、人の言葉の事実に数えない（話した通話を話していないと読む）", "src/salescall/core/call_report.py",
     '"who_unclears", "not_agreed", "other_wishes")',
     '"who_unclears", "other_wishes")',
     ("tests.test_call_report_cl1.Approval1102.test_points_not_agreed_are_received_and_returned",)),
    ("契約者の単位の録音に、ご納得いただけなかった点を伺う一言（callback_not_agreed）が無くても話す（CL-1 の 4-66 (d)）",
     "src/salescall/core/recordings.py",
     '"callback_later_time", "callback_not_agreed", "callback_number",', '"callback_later_time", "callback_number",',
     ("tests.test_gate.NameLedger.test_the_not_agreed_ask_cl1_added_on_11_02_is_received",)),
    ("質問の番号に、ご納得いただけなかった点を伺う番号 N1 を使える（CL-1 の 4-66 (d)）", _BS,
     '"H1", "N1", "N2"', '"H1", "N2"',
     ("tests.test_gate.NameLedger.test_the_not_agreed_ask_cl1_added_on_11_02_is_received",)),
    ("契約者の単位の録音に、callback_agreed_now が無くても話す（CL-1 の 4-66 のあとの (f)(i)）", "src/salescall/core/recordings.py",
     '"callback_agreed_now", "callback_other_point", "callback_other_point_2", "closing_after_decline", "own_fallback_point",',
     '"callback_other_point", "callback_other_point_2", "closing_after_decline", "own_fallback_point",',
     ("tests.test_gate.NameLedger.test_the_parts_cl1_added_after_4_66_are_received",)),
    ("契約者の単位の録音に、callback_other_point が無くても話す（CL-1 の 4-66 のあとの (f)(i)）", "src/salescall/core/recordings.py",
     '"callback_agreed_now", "callback_other_point", "callback_other_point_2", "closing_after_decline", "own_fallback_point",',
     '"callback_agreed_now", "callback_other_point_2", "closing_after_decline", "own_fallback_point",',
     ("tests.test_gate.NameLedger.test_the_parts_cl1_added_after_4_66_are_received",)),
    ("契約者の単位の録音に、callback_other_point_2 が無くても話す（CL-1 の 4-66 のあとの (f)(i)）", "src/salescall/core/recordings.py",
     '"callback_agreed_now", "callback_other_point", "callback_other_point_2", "closing_after_decline", "own_fallback_point",',
     '"callback_agreed_now", "callback_other_point", "closing_after_decline", "own_fallback_point",',
     ("tests.test_gate.NameLedger.test_the_parts_cl1_added_after_4_66_are_received",)),
    ("契約者の単位の録音に、closing_after_decline が無くても話す（CL-1 の 4-66 のあとの (f)(i)）", "src/salescall/core/recordings.py",
     '"callback_agreed_now", "callback_other_point", "callback_other_point_2", "closing_after_decline", "own_fallback_point",',
     '"callback_agreed_now", "callback_other_point", "callback_other_point_2", "own_fallback_point",',
     ("tests.test_gate.NameLedger.test_the_parts_cl1_added_after_4_66_are_received",)),
    ("契約者の単位の録音に、own_fallback_point が無くても話す（CL-1 の 4-66 のあとの (f)(i)）", "src/salescall/core/recordings.py",
     '"callback_agreed_now", "callback_other_point", "callback_other_point_2", "closing_after_decline", "own_fallback_point",',
     '"callback_agreed_now", "callback_other_point", "callback_other_point_2", "closing_after_decline",',
     ("tests.test_gate.NameLedger.test_the_parts_cl1_added_after_4_66_are_received",)),
    ("契約者の単位の録音に、相づち「かしこまりました。」（kashikomari）が無くても話す（CL-1 の 4-66 のあとの (k)）",
     "src/salescall/core/recordings.py",
     '"hold_timeout", "ikaga", "kashikomari",', '"hold_timeout", "ikaga",',
     ("tests.test_gate.NameLedger.test_the_parts_cl1_added_after_4_66_are_received",)),
    ("質問の番号に、ほかにご納得いただけない点を伺う番号 N2 を使える（CL-1 の 4-66 のあとの (f)）", _BS,
     '"N1", "N2", "N3"', '"N1", "N3"',
     ("tests.test_gate.NameLedger.test_the_parts_cl1_added_after_4_66_are_received",)),
    ("質問の番号に、ご納得いただけたかを伺う番号 N3 を使える（CL-1 の 4-66 のあとの (f)）", _BS,
     '"N1", "N2", "N3", "W1",', '"N1", "N2", "W1",',
     ("tests.test_gate.NameLedger.test_the_parts_cl1_added_after_4_66_are_received",)),
    ("締めた理由 not_agreed を「結論として終えた」にしない（断る答えなら用件を断られた＝同じ業務の次の実施回でかけない）",
     "src/salescall/core/judge.py",
     '    if r and r.end_reason == "not_agreed":\n        return done("concluded", conclusion="not_agreed")\n', "",
     ("tests.test_call_report_cl1.NotAgreedEnd1102.test_the_points_carry_round_explained_and_outcome_and_the_end_concludes",)),
    ("締めた理由に not_agreed が無い（会話の報告を丸ごと断る）", "src/salescall/core/call_report.py",
     '"transfer_unclear", "who_unclear", "not_agreed")', '"transfer_unclear", "who_unclear")',
     ("tests.test_call_report_cl1.NotAgreedEnd1102.test_the_points_carry_round_explained_and_outcome_and_the_end_concludes",)),
    ("締めた理由 not_agreed を人の言葉の締めに数えない（話した通話を話していないと読む）", "src/salescall/core/call_report.py",
     '"hold_timeout", "goal_refused", "not_agreed"})', '"hold_timeout", "goal_refused"})',
     ("tests.test_call_report_cl1.Anyone18b.test_which_facts_show_a_person_is_decided_in_one_table",)),
    ("ご納得いただけなかった点の何回目のご説明か（round）を確かめない", "src/salescall/core/call_report.py",
     "        if rnd is not None and (not isinstance(rnd, int) or isinstance(rnd, bool) or rnd < 1):\n", "        if False:\n",
     ("tests.test_call_report_cl1.NotAgreedEnd1102.test_the_points_carry_round_explained_and_outcome_and_the_end_concludes",)),
    ("ご納得いただけなかった点のご説明できたか（explained）を確かめない", "src/salescall/core/call_report.py",
     "        if explained is not None and not isinstance(explained, bool):\n", "        if False:\n",
     ("tests.test_call_report_cl1.NotAgreedEnd1102.test_the_points_carry_round_explained_and_outcome_and_the_end_concludes",)),
    ("ご納得いただけなかった点の結び（outcome）を確かめない", "src/salescall/core/call_report.py",
     "        if outcome is not None and outcome not in NOT_AGREED_OUTCOMES:\n", "        if False:\n",
     ("tests.test_call_report_cl1.NotAgreedEnd1102.test_the_points_carry_round_explained_and_outcome_and_the_end_concludes",)),
    ("ご納得いただけなかった点の回・ご説明できたか・結び・お断りの理由かを持たない", "src/salescall/core/call_report.py",
     'out.append(NotAgreed(turn, question, said, rnd, explained, outcome, _bool_or_false(r, "declining"), point))',
     'out.append(NotAgreed(turn, question, said))',
     ("tests.test_call_report_cl1.NotAgreedEnd1102.test_the_points_carry_round_explained_and_outcome_and_the_end_concludes",)),
    ("ご納得いただけなかった点の回・ご説明できたか・結び・お断りの理由かを記録に書かない（読み戻すと消える）",
     "src/salescall/core/call_report.py",
     '"said": n.said, "point": n.point, "round": n.round,\n                            "explained": n.explained, "outcome": n.outcome, "declining": n.declining}',
     '"said": n.said}',
     ("tests.test_call_report_cl1.NotAgreedEnd1102.test_the_points_carry_round_explained_and_outcome_and_the_end_concludes",)),
    ("質問の番号に、ご遠慮の確かめの番号 D1 を使える（CL-1 の 4-69）", _BS,
     '"R1", "D1", "H1"', '"R1", "H1"',
     ("tests.test_gate.NameLedger.test_the_refusal_confirm_number_d1_is_received",)),
    ("かけ直しの理由に、今はお電話に出られないご事情（unavailable）が無い（会話の報告を丸ごと断る。CL-1 の 4-69）",
     "src/salescall/core/call_report.py",
     '"ask_who", "ask_when", "unavailable")', '"ask_who", "ask_when")',
     ("tests.test_call_report_cl1.Untimed1101.test_unavailable_with_a_time_is_a_promise_and_without_one_is_one_more_day",)),
    ("今はお電話に出られないご事情で日時が取れなかった申し出を、時刻の決まっていない約束にする（日を改めて1回にしない）",
     "src/salescall/core/call_report.py",
     '                    "unavailable": ("not_transferred", "not_reached_not_transferred")}', '                    }',
     ("tests.test_call_report_cl1.Untimed1101.test_unavailable_with_a_time_is_a_promise_and_without_one_is_one_more_day",)),
    ("今はお電話に出られないご事情の「〇時まで」を、それまでは出られないと読まない（CL-1 の 4-69 の (3)）",
     "src/salescall/core/call_report.py",
     '                   "unavailable": "unavailable"}', '                   }',
     ("tests.test_call_report_cl1.Untimed1101.test_unavailable_with_a_time_is_a_promise_and_without_one_is_one_more_day",)),
    ("時刻の取れなかった申し出の再発信の方針を、理由に関わらず「不在」にする（ご事情を回数の上限までかける）", "src/salescall/core/judge.py",
     'policy_key=call_report.UNTIMED_OUTCOMES[cb.reason][1], again=again)', 'policy_key="not_reached_absent", again=again)',
     ("tests.test_call_report_cl1.Untimed1101.test_unavailable_with_a_time_is_a_promise_and_without_one_is_one_more_day",)),
    ("時刻の取れなかった申し出の繋がらなかった理由を、理由に関わらず「不在」にする", "src/salescall/core/judge.py",
     '        return call_report.UNTIMED_OUTCOMES[cb.reason][0], r.may_call_again', '        return "absent", r.may_call_again',
     ("tests.test_call_report_cl1.Untimed1101.test_unavailable_with_a_time_is_a_promise_and_without_one_is_one_more_day",)),
    ("ご納得いただけなかった点の値（point）を読まず、根拠の発話（said）を値にする（CL-1 の 4-69 の 4-185 の (1)）",
     "src/salescall/core/call_report.py",
     '        point = r["point"] if "point" in r else said      # point の無い前の形は said が値',
     '        point = said',
     ("tests.test_call_report_cl1.Approval1102.test_points_not_agreed_are_received_and_returned",)),
    ("ご納得いただけなかった点の値を確かめない（文字でない値を受ける）", "src/salescall/core/call_report.py",
     '        if point is not None and not isinstance(point, str):', '        if False:',
     ("tests.test_call_report_cl1.Approval1102.test_points_not_agreed_are_received_and_returned",)),
    ("ご納得いただけなかった点の値を記録に書かない（読み戻すと前の形の読み替えで根拠の発話が値になる）", "src/salescall/core/call_report.py",
     '"said": n.said, "point": n.point, "round": n.round,', '"said": n.said, "round": n.round,',
     ("tests.test_call_report_cl1.Approval1102.test_points_not_agreed_are_received_and_returned",)),
    ("依頼者へ返すご納得いただけなかった点に、値（point）を入れない", "src/salescall/core/judge.py",
     '[{"question": n.question, "point": n.point, "said": n.said,', '[{"question": n.question, "said": n.said,',
     ("tests.test_call_report_cl1.Approval1102.test_points_not_agreed_are_received_and_returned", "tests.test_judge.NotAsked.test_points_not_agreed_are_returned")),
    ("かけ直しの理由に、在籍していない（departed）を受ける（CL-1 の 4-69 の 4-182 の (5)：書かれない理由を受けて使われない道を残す）",
     "src/salescall/core/call_report.py",
     '"later", "other_means", "ask_who"', '"later", "departed", "other_means", "ask_who"',
     ("tests.test_call_report_cl1.Untimed1101.test_departed_and_our_promise_are_not_callback_reasons",)),
    ("止まった判定のAIを、緊急の道で補えるかを見ずに分ける（Jev・d1-ultra が止まっても緊急の道で再開できる。CL-1 の 4-69 の 4-179 (c)(d)）",
     "src/salescall/core/errors.py",
     '        return "ai_stopped" if e.ai in EMERGENCY_COVERS else "judge_stopped"', '        return "ai_stopped"',
     ("tests.test_ai_troubles.Shape.test_three_kinds_by_ai_and_route",
      "tests.test_ai_troubles.Pause.test_a_judge_without_an_emergency_route_cannot_be_reopened_on_it")),
    ("緊急の道が代わりになれるAIに、Anthropic でない判定（Jev）を入れる", "src/salescall/core/errors.py",
     'EMERGENCY_COVERS: frozenset[str] = frozenset({"Claude Sonnet 5.5", "Claude Sonnet 5.5（考える）"})',
     'EMERGENCY_COVERS: frozenset[str] = frozenset({"Claude Sonnet 5.5", "Claude Sonnet 5.5（考える）", "Jev 1.14"})',
     ("tests.test_ai_troubles.Shape.test_three_kinds_by_ai_and_route",)),
    ("取込で、伺う条件の列を読まない", _BI,
     '            {"after": v.get("ask_after"), "answers": _split(v.get("ask_answers")), "hedged": hedged is True}', "            None",
     ("tests.test_business_import.Import.test_ask_if_and_approval_come_in_from_the_question_sheet",)),
    ("取込で、ご承認を伺う列を読まない", "src/salescall/core/business_import.py",
     '                          "approval": _label("approval", v.get("approval"), f"{where}のご承認を伺う", problems),',
     '                          "approval": False,',
     ("tests.test_business_import.Import.test_ask_if_and_approval_come_in_from_the_question_sheet",)),
    ("取込で、最終目的の列を読まない（最終目的の申し出が会話へ渡らず、断られても締めない）", "src/salescall/core/business_import.py",
     '                          "final_goal": _label("final_goal", v.get("final_goal"), f"{where}の最終目的", problems),',
     '                          "final_goal": False,',
     ("tests.test_business_import.Import.test_final_goals_and_how_they_are_met_come_in_from_the_sheets",)),
    ("かける前に会話AIのつながりを開かずにかける（開けないと分かるのは、お客様の電話を鳴らしたあと）", _LINE,
     "                        opened = runner.open(access, actor, got, tenant_id=tenant_id, client_id=client_id, call=call)",
     "                        opened = True",
     ("tests.test_supervisor.Rounds.test_the_conversation_ai_is_opened_before_dialing_and_an_unopened_one_never_rings_the_customer",)),
    ("会話AIのつながりを開けなかった予約を戻さない（予約が残り、期限まで行が止まる）", _LINE,
     "                    if not opened:\n                        access.in_client(actor, tenant_id, client_id, lambda tx: attempts.release(tx, [got.attempt_id]))\n",
     "                    if not opened:\n                        pass\n",
     ("tests.test_supervisor.Rounds.test_the_conversation_ai_is_opened_before_dialing_and_an_unopened_one_never_rings_the_customer",)),
    ("かける前の一歩が落ちたとき、予約を戻さずに外へ出す", _LINE,
     "                    except BaseException:\n                        access.in_client(actor, tenant_id, client_id, lambda tx: attempts.release(tx, [got.attempt_id]))\n",
     "                    except BaseException:\n",
     ("tests.test_supervisor.Rounds.test_the_conversation_ai_is_opened_before_dialing_and_an_unopened_one_never_rings_the_customer",)),
    ("単位を決める処理に、業務の目的を渡さない（CL-1 の決め方は目的と一緒に聞いて測った＝4-69 の 4-184 の (a)）",
     "src/salescall/core/business_import.py",
     "asked=lambda texts: [(purpose, t) for t in texts])", 'asked=lambda texts: [("", t) for t in texts])',
     ("tests.test_business_import.Import.test_units_of_number_questions_are_decided_without_a_person",)),
    ("取込で、最終目的の果たし方を読まない", "src/salescall/core/business_import.py",
     '        **({"goal_mode": _label("goal_mode", basic["goal_mode"], "シート「基本」の最終目的の果たし方", problems)}\n'
     '           if basic.get("goal_mode") else {}),',
     "        **{},",
     ("tests.test_business_import.Import.test_final_goals_and_how_they_are_met_come_in_from_the_sheets",)),
    ("取込で、こちらがすることの言葉を決める処理に意味の言葉を渡さない（向きの取り違えを止められない＝G-72）", _BI,
     '        first.setdefault((q["text"], c), _meanings_of(q, c, meanings))',
     '        first.setdefault((q["text"], c), {})',
     ("tests.test_business_import.Import.test_do_words_are_decided_with_the_meanings",
      "tests.test_business_import.Import.test_away_choices_are_decided_by_the_ai_with_the_meanings")),
    ('伺う条件の言い切っていないときだけ伺うかが真偽でなくても通す', _BS,
     '    if not isinstance(hedged, bool):',
     '    if False:',
     ('tests.test_business_settings.Check.test_a_question_asked_only_after_a_hedged_answer',)),
    ('値を伺う質問の後でも、言い切っていないときだけ伺う質問にできる（会話の側が伺わない）', _BS,
     '    if hedged and src.type not in CHOICE_TYPES:',
     '    if False:',
     ('tests.test_business_settings.Check.test_a_question_asked_only_after_a_hedged_answer',)),
    ('伺う条件の言い切っていないときだけ伺うかを、読んだ条件に入れない', _BS,
     '    return AskIf(after, tuple(answers), hedged)',
     '    return AskIf(after, tuple(answers))',
     ('tests.test_business_settings.Check.test_a_question_asked_only_after_a_hedged_answer',)),
    ('最短の日の営業日の数の範囲を確かめない', _BS,
     '    if not isinstance(n, int) or isinstance(n, bool) or not 0 <= n <= EARLIEST_MAX_DAYS:',
     '    if not isinstance(n, int):',
     ('tests.test_business_settings.Check.test_earliest_day_setting',)),
    ('最短の日の休みの曜日が7つ全部でも通す（最短の日が出ない）', _BS,
     '    elif len(weekdays) == len(calling_hours.DAY_KEYS):',
     '    elif False:',
     ('tests.test_business_settings.Check.test_earliest_day_setting',)),
    ('最短の日の休業日が重なっても通す', _BS,
     '        if len(set(days)) != len(days):',
     '        if False:',
     ('tests.test_business_settings.Check.test_earliest_day_setting',)),
    ('最短の日の休業日の形（YYYY-MM-DD）を確かめない', _BS,
     '                if not isinstance(d, str) or not re.fullmatch(r"\\d{4}-\\d{2}-\\d{2}", d):',
     '                if not isinstance(d, str):',
     ('tests.test_business_settings.Check.test_earliest_day_setting',)),
    ('{最短の日} を書いた質問があっても、最短の日を求めない（会話の側が通話を始めない）', _BS,
     '    if slotted and not has_earliest:',
     '    if False:',
     ('tests.test_business_settings.Check.test_earliest_day_setting',)),
    ('{最短の日} を書いた質問が無くても、最短の日を書ける（使われない値）', _BS,
     '    elif has_earliest and not slotted:',
     '    elif False:',
     ('tests.test_business_settings.Check.test_earliest_day_setting',)),
    ('{最短の日} を質問の文・入る一言のほかに書いても通す（そのまま声になる）', _BS,
     '        if isinstance(v, str) and DAY_SLOT in v:',
     '        if False:',
     ('tests.test_business_settings.Check.test_earliest_day_setting',)),
    ('{最短の日} を質問の文・入る一言に書いても問題にする', _BS,
     'walk({x: y for x, y in q.items() if x not in ("text", "intro")}, f"質問 {q.get(\'id\') or n}")',
     'walk(q, f"質問 {q.get(\'id\') or n}")',
     ('tests.test_business_settings.Check.test_earliest_day_setting',)),
    ('読んだ最短の日を業務設定に入れない', _BS,
     '**responses, client_does=client_does, goal_mode=goal_mode if goal_mode in GOAL_MODES else None, earliest=earliest)',
     '**responses, client_does=client_does, goal_mode=goal_mode if goal_mode in GOAL_MODES else None)',
     ('tests.test_business_settings.Check.test_earliest_day_setting',)),
    ('営業日の数が0でも、電話の日を最短の日にしない', _BS,
     '        if self.business_days == 0 and self._open(d, is_holiday):',
     '        if False:',
     ('tests.test_business_settings.Check.test_earliest_day_counts_business_days',)),
    ('営業日を1日多く数える', _BS,
     '                if count >= self.business_days:',
     '                if count > self.business_days:',
     ('tests.test_business_settings.Check.test_earliest_day_counts_business_days',)),
    ('休みの曜日も営業日に数える', _BS,
     '        return (calling_hours.DAY_KEYS[d.weekday()] not in self.closed_weekdays and d not in self.closed_dates',
     '        return (d not in self.closed_dates',
     ('tests.test_business_settings.Check.test_earliest_day_counts_business_days',)),
    ('休業日も営業日に数える', _BS,
     'not in self.closed_weekdays and d not in self.closed_dates',
     'not in self.closed_weekdays',
     ('tests.test_business_settings.Check.test_earliest_day_counts_business_days',)),
    ('祝日を休みにしない業務でも、祝日を休みにする', _BS,
     '                and not (self.closed_on_holidays and is_holiday(d)))',
     '                and not is_holiday(d))',
     ('tests.test_business_settings.Check.test_earliest_day_counts_business_days',)),
    ('最短の日を探す先が短い（休みの続く業務で見つけられない）', _BS,
     '        for _ in range(_EARLIEST_SEARCH_DAYS):',
     '        for _ in range(30):',
     ('tests.test_business_settings.Check.test_earliest_day_counts_business_days',)),
    ('前の版の伺う条件を、言い切っていないときだけ伺うかの無いまま読む（足す前に置いた版が読めない）', _BS,
     '        if isinstance(out.get("ask_if"), dict) and "hedged" not in out["ask_if"]:',
     '        if False:',
     ('tests.test_business_settings.Versions.test_a_version_saved_before_hedged_conditions_reads_them_as_always_asked',)),
    ('判定へ渡す伺う条件に、言い切っていないときだけ伺うかを入れない', _BS,
     'AskIf(q["ask_if"]["after"], tuple(q["ask_if"]["answers"]), q["ask_if"]["hedged"])',
     'AskIf(q["ask_if"]["after"], tuple(q["ask_if"]["answers"]))',
     ('tests.test_business_settings.Versions.test_a_version_saved_before_hedged_conditions_reads_them_as_always_asked',)),
    ('電話の日を、日本時間でなく時刻の時差のまま決める', _BS,
     '    today = calling_hours.local(at).date()',
     '    today = at.date()',
     ('tests.test_business_settings.Versions.test_day_of_call_gives_the_earliest_day_from_the_calendar',)),
    ('最短の日を、祝日の暦を見ずに数える', _BS,
     'e.day(today, calendar.holiday_checker(tx))',
     'e.day(today, lambda d: False)',
     ('tests.test_business_settings.Versions.test_day_of_call_gives_the_earliest_day_from_the_calendar',)),
    ('言い切っていないときだけ伺う質問を、言い切った答えのあとでも伺っていない（対象外）にしない', "src/salescall/core/judge.py",
     'or (cond.hedged and not src.hedged))):',
     'or False)):',
     ('tests.test_judge.NotAsked.test_a_question_asked_only_after_a_hedged_answer', 'tests.test_settle.NotAskedSettle.test_a_question_asked_only_after_a_hedged_answer_is_not_asked_after_a_definite_one')),
    ('締めで、行の項目の「曖昧」の印を判定に渡さない', _SETTLE,
     'Item(r["state"], r["condition"], r["reason"], r["hedged"])',
     'Item(r["state"], r["condition"], r["reason"])',
     ('tests.test_settle.NotAskedSettle.test_a_question_asked_only_after_a_hedged_answer_is_not_asked_after_a_definite_one',)),
    ('判定の入力の写しに「曖昧」の印を残さない（印が変わっても判定し直さない）', _SETTLE,
     '**({"hedged": True} if i.hedged else {})}',
     '**{}}',
     ('tests.test_settle.NotAskedSettle.test_a_question_asked_only_after_a_hedged_answer_is_not_asked_after_a_definite_one',)),
    ('取込で、言い切っていないときだけ伺うを読まない', _BI,
     '"hedged": hedged is True}',
     '"hedged": False}',
     ('tests.test_business_import.Import.test_hedged_condition_and_earliest_day_come_in',)),
    ('取込で、言い切っていないときだけ伺うだけの行に伺う条件を作らない（伺う条件の無いことを見逃す）', _BI,
     'ask_if = None if not v.get("ask_after") and not v.get("ask_answers") and not hedged else',
     'ask_if = None if not v.get("ask_after") and not v.get("ask_answers") else',
     ('tests.test_business_import.Import.test_hedged_condition_and_earliest_day_come_in',)),
    ('取込で、最短の日を読まない', _BI,
     '        **_earliest(basic, problems),',
     '        **{},',
     ('tests.test_business_import.Import.test_hedged_condition_and_earliest_day_come_in',)),
    ('取込で、休みの曜日を書かない業務を、休みの曜日の無い業務にする', _BI,
     '        else list(business_settings.Earliest.closed_weekdays)',
     '        else []',
     ('tests.test_business_import.Import.test_hedged_condition_and_earliest_day_come_in',)),
    ('取込で、休みの曜日「なし」を読めない', _BI,
     '    weekdays = [] if given == NO_CLOSED_WEEKDAYS else',
     '    weekdays = [] if False else',
     ('tests.test_business_import.Import.test_hedged_condition_and_earliest_day_come_in',)),
    ('取込で、祝日は休みかを書かない業務を、祝日も営業日にする', _BI,
     'if basic.get("closed_on_holidays") else True',
     'if basic.get("closed_on_holidays") else False',
     ('tests.test_business_import.Import.test_hedged_condition_and_earliest_day_come_in',)),
    ('取込で、休業日の「2026/12/30」の形を日にちに直さない', _BI,
     '        dates.append(f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}" if m else d)',
     '        dates.append(d)',
     ('tests.test_business_import.Import.test_hedged_condition_and_earliest_day_come_in',)),
    ('名前を洗い出す台本に、最短の日の場所をそのまま入れる（名前でないものに読みを求めうる）', _BI,
     '    return "\\n".join(p.replace(business_settings.DAY_SLOT, "") for p in parts if isinstance(p, str) and p.strip())',
     '    return "\\n".join(p for p in parts if isinstance(p, str) and p.strip())',
     ('tests.test_business_import.Import.test_hedged_condition_and_earliest_day_come_in',)),
    ('名前を洗い出す台本に、質問に入る一言を入れない（入る一言の中の名前に読みが付かない）', _BI,
     '    parts += [q.get(k) for q in questions for k in ("text", "intro")]',
     '    parts += [q.get(k) for q in questions for k in ("text",)]',
     ('tests.test_business_import.Import.test_do_words_intro_and_client_does_come_in',)),
    ('取込で、日付のセル（「…T00:00:00」）の休業日を日にちに読めない', _BI,
     '(?:T00:00:00)?\\s*$")',
     '\\s*$")',
     ('tests.test_business_import.Import.test_hedged_condition_and_earliest_day_come_in',)),
    ('取込で、最短の日の数が無いのに休みの曜日などを書いても通す', _BI,
     '        if any(basic.get(k) for k in keys):',
     '        if False:',
     ('tests.test_business_import.Import.test_hedged_condition_and_earliest_day_come_in',)),
    ('事実を伺う質問にも、目的から遠ざかる向きを付けられる', _BS,
     '    if away and asks_fact:',
     '    if False:',
     ('tests.test_business_settings.Check.test_away_choices',)),
    ('目的から遠ざかる向きの選択肢が、その質問の選択肢になくても通す', _BS,
     '        unknown = [a for a in away if a not in choices]',
     '        unknown = []',
     ('tests.test_business_settings.Check.test_away_choices',)),
    ('値を聞く質問にも、目的から遠ざかる向きを付けられる', _BS,
     '    if away and type_name not in CHOICE_TYPES:',
     '    if False:',
     ('tests.test_business_settings.Check.test_away_choices',)),
    ('目的から遠ざかる向きを、読んだ質問に入れない', _BS,
     'do_words, away, intro,',
     'do_words, (), intro,',
     ('tests.test_business_settings.Check.test_away_choices',)),
    ('前の版の質問を、目的から遠ざかる向きの無いまま読む（足す前に置いた版が読めない）', _BS,
     '"do_words": {}, "away": [], "intro": None',
     '"do_words": {}, "intro": None',
     ('tests.test_business_settings.Versions.test_a_version_saved_before_asks_fact_is_read_as_fact_questions',)),
    ('2つ目からの質問の文が、つなぎの言葉で始まっても通す（会話の側のつなぎの言葉と重なる）', _BS,
     '            if lead is not None and n > 1:',
     '            if False:',
     ('tests.test_business_settings.Check.test_question_text_does_not_start_with_a_lead_word',)),
    ('最初の質問の文も、つなぎの言葉で始めると止める（頭に何も付かない）', _BS,
     '            if lead is not None and n > 1:',
     '            if lead is not None:',
     ('tests.test_business_settings.Check.test_question_text_does_not_start_with_a_lead_word',)),
    ('つなぎの言葉が文の途中にあっても止める', _BS,
     'q["text"].lstrip().startswith(w)',
     'w in q["text"]',
     ('tests.test_business_settings.Check.test_question_text_does_not_start_with_a_lead_word',)),
    ('取込で、事実を伺う質問にも、目的から遠ざかる向きを AI に聞く', _BI,
     '_WISH_KEYS = ("do_words", "away")',
     '_WISH_KEYS = ("do_words",)',
     ('tests.test_business_import.Import.test_away_choices_are_decided_by_the_ai_with_the_meanings',)),
    ('取込で、入口が目的から遠ざかる向きを持ってきた質問も AI に聞く', _BI,
     '        missing = not given if key != "do_words" else',
     '        missing = True if key != "do_words" else',
     ('tests.test_business_import.Import.test_away_choices_are_decided_by_the_ai_with_the_meanings',)),
    ('取込で、業務設定にすべての選択肢のこちらがすることの言葉があっても AI に聞く', _BI,
     'else not (isinstance(given, dict) and set(choices or ()) <= set(given))',
     'else True',
     ('tests.test_business_import.Import.test_do_words_written_in_the_settings_come_first',)),
    ('取込で、業務設定に書いたこちらがすることの言葉より AI の言葉を先に使う', _BI,
     '        q["do_words"] = {**(ai.get("do_words") or {}).get(_pair_key(q["text"], choices), {}), **given} or None',
     '        q["do_words"] = {**given, **(ai.get("do_words") or {}).get(_pair_key(q["text"], choices), {})} or None',
     ('tests.test_business_import.Import.test_do_words_written_in_the_settings_come_first',)),
    ('取込で、AI の答えの目的から遠ざかる向きの選択肢を確かめずに入れる', _BI,
     '[c for c in choices if c in d] if readable else list(choices)',
     'list(d) if readable else list(choices)',
     ('tests.test_business_import.Import.test_away_choices_are_decided_by_the_ai_with_the_meanings',)),
    ('取込で、読めない AI の答えを、遠ざかる向きの無いことにする（安全側にしない）', _BI,
     '[c for c in choices if c in d] if readable else list(choices)',
     '[c for c in choices if c in d] if readable else []',
     ('tests.test_business_import.Import.test_away_choices_are_decided_by_the_ai_with_the_meanings',)),
    ('取込で、数の合わない AI の答えを、遠ざかる向きの無いことにする（安全側にしない）', _BI,
     'else [None] * len(pairs)',
     'else [()] * len(pairs)',
     ('tests.test_business_import.Import.test_away_choices_are_decided_by_the_ai_with_the_meanings',)),
    ('取込で、選択肢の名前でない並びの答えも読めたことにする', _BI,
     '        readable = isinstance(d, (tuple, list)) and all(isinstance(c, str) for c in d)',
     '        readable = isinstance(d, (tuple, list))',
     ('tests.test_business_import.Import.test_away_choices_are_decided_by_the_ai_with_the_meanings',)),
    ('取込で、目的から遠ざかる向きの AI の答えを入れない', _BI,
     '        q["away"] = (ai.get("away") or {}).get(_pair_key(q["text"], choices)) or None',
     '        q["away"] = None',
     ('tests.test_business_import.Import.test_away_choices_are_decided_by_the_ai_with_the_meanings',)),
    ('取込で、目的から遠ざかる向きを AI に聞かない', _BI,
     '            "away": _decided_away(raw, decide_away, facts, meanings)}',
     '            "away": {}}',
     ('tests.test_business_import.Import.test_away_choices_are_decided_by_the_ai_with_the_meanings',)),
    ('取込で、目的から遠ざかる向きを、事実を伺う質問かが決まる前に聞く', _BI,
     '_decided_away(raw, decide_away, facts, meanings)',
     '_decided_away(raw, decide_away, {}, meanings)',
     ('tests.test_business_import.Import.test_away_choices_are_decided_by_the_ai_with_the_meanings',)),
    ('取込で、目的から遠ざかる向きを決める処理に意味の言葉を渡さない（打ち消しの問いの「はい」を取り違える）', _BI,
     '_decided_away(raw, decide_away, facts, meanings)',
     '_decided_away(raw, decide_away, facts, {})',
     ('tests.test_business_import.Import.test_away_choices_are_decided_by_the_ai_with_the_meanings',)),
    ('ファイルの取込で、目的から遠ざかる向きを決める処理を渡さない', _BI,
     '                decide_do_words=decide_do_words, decide_away=decide_away)',
     '                decide_do_words=decide_do_words)',
     ('tests.test_business_import.Import.test_away_choices_are_decided_by_the_ai_with_the_meanings',)),
    ('取込で、こちらがすることの言葉の列を読まない', _BI,
     '"do_words": _choice_words(v.get("do_words"), f"{where}のこちらがすることの言葉", problems),',
     '"do_words": None,',
     ('tests.test_business_import.Import.test_do_words_written_in_the_settings_come_first',)),
    ('業務設定の確かめで、こちらがすることの言葉の形を見ない', _BS,
     '            do_words = _answer_words(q["do_words"], q["type"], choices, where, problems, replies=False, what="こちらがすることの言葉",\n                                     do=True)',
     '            do_words = _answer_words(q["do_words"], q["type"], choices, where, problems, replies=False, what="こちらがすることの言葉")',
     ('tests.test_business_settings.Check.test_do_words_run_to_the_end_of_the_sentence',)),
    ('取込で、AI のこちらがすることの言葉の形を見ずに受ける', _BI,
     'replies=key == "replies", do=key == "do_words")',
     'replies=key == "replies")',
     ('tests.test_business_import.Import.test_do_words_intro_and_client_does_come_in',)),
    ('前に置いた版の、文の終わりまで持たないこちらがすることの言葉をそのまま読む', _BS,
     '            out["do_words"] = {k: v for k, v in out["do_words"].items() if not isinstance(v, str) or do_words_ok(v)}',
     '            out["do_words"] = dict(out["do_words"])',
     ('tests.test_business_settings.Versions.test_a_version_saved_before_tell_words_and_full_do_words',)),
    ('前に置いた版の、その答えのあとに伝える一言を空として読まない', _BS,
     '"approval": False, "final_goal": False, "declines": [], "tell_words": {}, "ends_on": []}',
     '"approval": False, "final_goal": False, "declines": [], "ends_on": []}',
     ('tests.test_business_settings.Versions.test_a_version_saved_before_tell_words_and_full_do_words',)),
    ('その答えのあとに伝える一言を、宣言した選択肢かを確かめずに受ける', _BS,
     '            tell_words = _answer_words(q["tell_words"], q["type"], choices, where, problems, replies=False,\n                                       what="その答えのあとに伝える一言")',
     '            tell_words = dict(q["tell_words"] or {})',
     ('tests.test_business_settings.Check.test_tell_words_after_the_answer',)),
    ('その答えのあとに伝える一言を業務設定の質問に入れない', _BS,
     '                                q["final_goal"] is True, declines, tell_words, ends_on)',
     '                                q["final_goal"] is True, declines, ends_on=ends_on)',
     ('tests.test_business_settings.Check.test_tell_words_after_the_answer', 'tests.test_business_import.Import.test_tell_words_come_in_and_spoken_words_are_in_the_script')),
    ('取込で、その答えのあとに伝える一言の列を読まない', _BI,
     '                          "tell_words": _choice_words(v.get("tell_words"), f"{where}のその答えのあとに伝える一言", problems) or {},',
     '                          "tell_words": {},',
     ('tests.test_business_import.Import.test_tell_words_come_in_and_spoken_words_are_in_the_script',)),
    ('名前を洗い出す台本に、選択肢ごとの声になる言葉を入れない', _BI,
     '    parts += [w for q in questions for k in SPOKEN_WORDS if isinstance(q.get(k), dict) for w in q[k].values()]',
     '',
     ('tests.test_business_import.Import.test_tell_words_come_in_and_spoken_words_are_in_the_script',)),
    ('名前を洗い出す台本に、意味の言葉を入れない', _BI,
     'SPOKEN_WORDS = ("meanings", "do_words", "tell_words")',
     'SPOKEN_WORDS = ("do_words", "tell_words")',
     ('tests.test_business_import.Import.test_tell_words_come_in_and_spoken_words_are_in_the_script',)),
    ('質問の番号から作る録音の部分の名前との取り違えを確かめない', _BS,
     '    problems += _derived_part_problems(raw)\n',
     '',
     ('tests.test_business_settings.Check.test_question_ids_must_not_look_like_derived_recording_parts', 'tests.test_business_settings.Check.test_earliest_day_setting')),
    ('質問の番号に W1 を使える', _BS,
     '"N1", "N2", "N3", "W1",',
     '"N1", "N2", "N3",',
     ('tests.test_business_settings.Check.test_question_ids_must_not_look_like_derived_recording_parts',)),
    ('質問の番号に C10 を使える', _BS,
     '"C8", "C9", "C10", "M1",',
     '"C8", "C9", "M1",',
     ('tests.test_business_settings.Check.test_question_ids_must_not_look_like_derived_recording_parts',)),
    ('ほかのご希望の方法を確かめずに受ける', 'src/salescall/core/call_report.py',
     '_choice(r.get("means"), OTHER_WISH_MEANS, f"{where}.means", optional=True)',
     'r.get("means")',
     ('tests.test_call_report_cl1.OtherWish1103.test_other_wishes_are_received_and_returned',)),
    ('ほかのご希望の結びを確かめずに受ける', 'src/salescall/core/call_report.py',
     '_choice(r.get("outcome"), OTHER_WISH_OUTCOMES, f"{where}.outcome", optional=True)',
     'r.get("outcome")',
     ('tests.test_call_report_cl1.OtherWish1103.test_other_wishes_are_received_and_returned',)),
    ('判定の中身で、ほかのご希望を依頼者へ返さない', 'src/salescall/core/judge.py',
     '    wishes = inp.report.other_wishes if inp.report is not None else ()',
     '    wishes = ()',
     ('tests.test_call_report_cl1.OtherWish1103.test_other_wishes_are_received_and_returned',)),
    ('前の通話の答えに、言い切っていないときも言葉の欄を空で渡す', 'src/salescall/core/conversation_io.py',
     '_EARLIER_IF_ANY = ("hedged", "wish")',
     '_EARLIER_IF_ANY = ("wish",)',
     ('tests.test_conversation_io.Materials.test_earlier_answers_carry_the_other_wish_and_the_hedged_words',)),
    ('ほかのご希望の結果で、連絡先を伺う途中で切れたのを方法と読まない', 'src/salescall/core/call_report.py',
     '        return self.outcome or ("means" if self.means else None)',
     '        return self.outcome',
     ('tests.test_conversation_io.Materials.test_earlier_answers_carry_the_other_wish_and_the_hedged_words',)),
    ('ほかのご希望の結果で、W1 の答えの前に切れたのも「無い」と読む', 'src/salescall/core/call_report.py',
     '        return self.outcome or ("means" if self.means else None)',
     '        return self.outcome or ("means" if self.means else "none")',
     ('tests.test_conversation_io.Materials.test_earlier_answers_carry_the_other_wish_and_the_hedged_words',)),
    ('取込で、遠ざかる向きを決める処理が無いときは、遠ざかる向きの無いことにする（安全側にしない）', 'src/salescall/core/business_import.py',
     '    if not pairs:\n        return {}\n    got = decide(items) if decide is not None else None',
     '    if not pairs or decide is None:\n        return {}\n    got = decide(items)',
     ('tests.test_business_import.Import.test_away_choices_are_decided_by_the_ai_with_the_meanings',)),
    ('質問の番号どうしで「_」の続きを見ない（日の言い方・伝える一言・ほかのご希望の録音の名前と取り違える）', _BS,
     '    for base in sorted(set(ids) | RESERVED_IDS):',
     '    for base in sorted(RESERVED_IDS):',
     ('tests.test_business_settings.Check.test_question_ids_must_not_look_like_derived_recording_parts', 'tests.test_business_settings.Check.test_earliest_day_setting')),
    ('システムの名前に「_」を続けた質問の番号を通す', _BS,
     '    for base in sorted(set(ids) | RESERVED_IDS):',
     '    for base in sorted(set(ids)):',
     ('tests.test_business_settings.Check.test_question_ids_must_not_look_like_derived_recording_parts',)),
    ('質問の番号の頭が同じなら「_」で続かなくても止める（広く取りすぎる）', _BS,
     '        clash = [x for x in ids if x.startswith(base + DERIVED_JOIN)]',
     '        clash = [x for x in ids if x != base and x.startswith(base)]',
     ('tests.test_business_settings.Check.test_question_ids_must_not_look_like_derived_recording_parts', 'tests.test_business_settings.Check.test_earliest_day_setting')),
    ('こちらがすることの言葉の終わりを確かめない（「…お取り」を通す）', _BS,
     '    return (bool(DO_WORDS_END.search(w)) and not DO_WORDS_DOUBLED.search(w) and not DO_WORDS_MARKS.search(w)\n            and not w.startswith(QUESTION_LEADS))',
     '    return (not DO_WORDS_DOUBLED.search(w) and not DO_WORDS_MARKS.search(w)\n            and not w.startswith(QUESTION_LEADS))',
     ('tests.test_business_settings.Check.test_do_words_run_to_the_end_of_the_sentence', 'tests.test_business_settings.Versions.test_a_version_saved_before_tell_words_and_full_do_words')),
    ('こちらがすることの言葉のへりくだりの重ねを確かめない', _BS,
     '    return (bool(DO_WORDS_END.search(w)) and not DO_WORDS_DOUBLED.search(w) and not DO_WORDS_MARKS.search(w)\n            and not w.startswith(QUESTION_LEADS))',
     '    return (bool(DO_WORDS_END.search(w)) and not DO_WORDS_MARKS.search(w)\n            and not w.startswith(QUESTION_LEADS))',
     ('tests.test_business_settings.Check.test_do_words_run_to_the_end_of_the_sentence',)),
    ('こちらがすることの言葉の記号を確かめない', _BS,
     '    return (bool(DO_WORDS_END.search(w)) and not DO_WORDS_DOUBLED.search(w) and not DO_WORDS_MARKS.search(w)\n            and not w.startswith(QUESTION_LEADS))',
     '    return (bool(DO_WORDS_END.search(w)) and not DO_WORDS_DOUBLED.search(w)\n            and not w.startswith(QUESTION_LEADS))',
     ('tests.test_business_settings.Check.test_do_words_run_to_the_end_of_the_sentence',)),
    ('こちらがすることの言葉の頭のつなぎの言葉を確かめない（「それではそれでは」）', _BS,
     '    return (bool(DO_WORDS_END.search(w)) and not DO_WORDS_DOUBLED.search(w) and not DO_WORDS_MARKS.search(w)\n            and not w.startswith(QUESTION_LEADS))',
     '    return (bool(DO_WORDS_END.search(w)) and not DO_WORDS_DOUBLED.search(w) and not DO_WORDS_MARKS.search(w))',
     ('tests.test_business_settings.Check.test_do_words_run_to_the_end_of_the_sentence',)),
    ('前の通話の答えに、ほかのご希望の結果を渡さない', 'src/salescall/core/conversation_io.py',
     '        wish = next((w.result for w in reversed(report.other_wishes) if w.question == r["question_id"]), None) if report else None',
     '        wish = None',
     ('tests.test_conversation_io.Materials.test_earlier_answers_carry_the_other_wish_and_the_hedged_words',)),
    ('前の通話の答えのほかのご希望を、その質問の最初のもので読む', 'src/salescall/core/conversation_io.py',
     '        wish = next((w.result for w in reversed(report.other_wishes) if w.question == r["question_id"]), None) if report else None',
     '        wish = next((w.result for w in report.other_wishes if w.question == r["question_id"]), None) if report else None',
     ('tests.test_conversation_io.Materials.test_earlier_answers_carry_the_other_wish_and_the_hedged_words',)),
    ('ここで終える答えを業務設定の質問に入れない', _BS,
     '                                q["final_goal"] is True, declines, tell_words, ends_on)',
     '                                q["final_goal"] is True, declines, tell_words)',
     ('tests.test_business_settings.Check.test_ends_on_answers',)),
    ('必須でない質問にも、ここで終える答えを書ける', _BS,
     '    if q["type"] not in CHOICE_TYPES or q["required"] is not True:',
     '    if q["type"] not in CHOICE_TYPES:',
     ('tests.test_business_settings.Check.test_ends_on_answers', 'tests.test_business_import.Import.test_ends_on_comes_in')),
    ('ここで終える答えに「わからない」「答えたくない」を書ける', _BS,
     ') if a not in SYSTEM_CHOICES]\n    unknown = [a for a in raw if a not in allowed]',
     ')]\n    unknown = [a for a in raw if a not in allowed]',
     ('tests.test_business_settings.Check.test_ends_on_answers',)),
    ('前の版の質問を、ここで終える答えの無いまま読む', _BS,
     '"declines": [], "tell_words": {}, "ends_on": []}',
     '"declines": [], "tell_words": {}}',
     ('tests.test_business_settings.Versions.test_a_version_saved_before_ends_on_reads_it_as_empty',)),
    ('分岐の飛ばしの伺う条件に「どれも要らない」を入れる（ほかのご希望を伺わずに次へ進む）', _BS,
     '            keep = [a for a in flow_answers[q.id] if a != NONE_CHOICE and (t := to(q, a)) != END and at[t] <= at[y]',
     '            keep = [a for a in flow_answers[q.id] if (t := to(q, a)) != END and at[t] <= at[y]',
     ('tests.test_business_settings.BranchFlow.test_skips_become_conditions_without_none',)),
    ('分岐の飛ばしの伺う条件を、いちばん遠い元の質問で作る（入れ子の飛ばしを取り違える）', _BS,
     '            q = skippers[-1]\n',
     '            q = skippers[0]\n',
     ('tests.test_business_settings.BranchFlow.test_skips_become_conditions_without_none',)),
    ('分岐の飛ばしで、元の質問を通らずに来る道を見ない（伺うはずの質問を伺わない）', _BS,
     '            if around:\n',
     '            if False:\n',
     ('tests.test_business_settings.BranchFlow.test_forms_the_conversation_cannot_express_are_problems',)),
    ('分岐の飛ばしの伺う条件に、続きのあとで終わる答えを入れる（終えたあとも伺う）', _BS,
     '                    and (a not in ends[q.id] or t == y)]',
     '                    ]',
     ('tests.test_business_settings.BranchFlow.test_a_follow_up_then_end_ends_on_the_earlier_answer',)),
    ('分岐の「→ 終わり」を、目的から遠ざかる向きの答えではここで終える答えにしない（「→ 終わり」から最終目的を推し量る形に戻る）', _BS,
     '        end = [a for a in flow_answers[q.id] if (a in direct[q.id] or a in follow[q.id]) and a not in q.ends_on]',
     '        end = [a for a in flow_answers[q.id] if (a in direct[q.id] or a in follow[q.id]) and a not in q.ends_on and a not in q.away]',
     ('tests.test_business_settings.BranchFlow.test_end_is_a_stop_answer_or_a_goal_refusal',)),
    ('分岐の「→ 終わり」で先へ進むかを決める質問を、必須にしない', _BS,
     'ask_if=ask_if[q.id], required=q.required or bool(end),',
     'ask_if=ask_if[q.id], required=q.required,',
     ('tests.test_business_settings.BranchFlow.test_end_is_a_stop_answer_or_a_goal_refusal',)),
    ('分岐の「わからない」「答えたくない」の行き先を使う（答えが決まらないうちに先へ進まない決まりとぶつかる）', _BS,
     '    flow_answers = {q.id: tuple(a for a in possible_answers(q) if a not in SYSTEM_CHOICES) for q in questions}',
     '    flow_answers = {q.id: tuple(possible_answers(q)) for q in questions}',
     ('tests.test_business_settings.BranchFlow.test_end_is_a_stop_answer_or_a_goal_refusal',)),
    ('分岐の前へ戻る行き先を見ない', _BS,
     '            if t != END and at[t] <= at[q.id]:\n',
     '            if False:\n',
     ('tests.test_business_settings.BranchFlow.test_forms_the_conversation_cannot_express_are_problems',)),
    ('続きのあとで終わる分岐を、行き先がその質問でない答えでも置き換える', _BS,
     ' or any(to(src, b) != q.id for b in c.answers):',
     ':',
     ('tests.test_business_settings.BranchFlow.test_forms_the_conversation_cannot_express_are_problems',)),
    ('理由×扱いの「必須の質問すべて」を、質問ごとの扱いに重ねない', "src/salescall/core/business_rules.py",
     '        per_state = {s: {**(common or {}).get(s, {}), **(own or {}).get(s, {})} for s in ("dont_know", "refused")}',
     '        per_state = {s: dict((own or {}).get(s, {})) for s in ("dont_know", "refused")}',
     ('tests.test_business_settings.BranchFlow.test_handling_for_all_required_reaches_questions_made_required',)),
    ('取込で「全部」の行を、必須の質問すべての鍵にしない', _BI,
     '        qid = business_rules.ALL_REQUIRED if v.get("id") == ALL_QUESTIONS else v.get("id")',
     '        qid = v.get("id")',
     ('tests.test_business_import.Import.test_preview_and_register',)),
    ('取込で、ここで終える答えの列を読まない', _BI,
     '                          "ends_on": _split(v.get("ends_on")) or [],',
     '                          "ends_on": [],',
     ('tests.test_business_import.Import.test_ends_on_comes_in',)),
    ('判定で、ここで終える答えで伺わなかった質問を対象外に数えない', 'src/salescall/core/judge.py',
     '    return frozenset(out | (set(ended.skipped) if ended is not None else set()))',
     '    return frozenset(out)',
     ('tests.test_call_report_cl1.EndedOn1104.test_ended_on_is_received_and_the_skipped_questions_are_not_asked',)),
    ('ここで終える答えの事実を、1通話に2つ以上受ける', 'src/salescall/core/call_report.py',
     '    if not isinstance(rows, list) or len(rows) > 1:',
     '    if not isinstance(rows, list):',
     ('tests.test_call_report_cl1.EndedOn1104.test_ended_on_is_received_and_the_skipped_questions_are_not_asked',)),
    ('前の通話の答えで止めた事実に、根拠の発話があっても受ける', 'src/salescall/core/call_report.py',
     ' or (turn is None and said):',
     ':',
     ('tests.test_call_report_cl1.EndedOn1104.test_ended_on_is_received_and_the_skipped_questions_are_not_asked',)),
    ('伺わなかった質問の並びに、止めた質問そのものがあっても受ける', 'src/salescall/core/call_report.py',
     '\\\n            or question in skipped:',
     ':',
     ('tests.test_call_report_cl1.EndedOn1104.test_ended_on_is_received_and_the_skipped_questions_are_not_asked',)),
    ('質問の番号に、引き止めの一言の録音の部分の名前 busy を使える', _BS,
     '"voicemail", "message", "busy"})',
     '"voicemail", "message"})',
     ('tests.test_business_settings.Check.test_question_ids_must_not_look_like_derived_recording_parts',)),
    ('こちらがすることの言葉・質問の文の頭に、相づち「承知しました」を付けて書ける', _BS,
     '"ありがとうございます", "承知しました", ',
     '"ありがとうございます", ',
     ('tests.test_business_settings.Check.test_question_text_does_not_start_with_a_lead_word',)),
    ('ほかのご希望への答えを、人の言葉の事実に数えない', 'src/salescall/core/call_report.py',
     '"who_unclears", "not_agreed", "other_wishes")',
     '"who_unclears", "not_agreed")',
     ('tests.test_call_report_cl1.OtherWish1103.test_other_wishes_are_received_and_returned',)),
    ('取込で、「選択肢：言葉」の形でない、こちらがすることの言葉を黙って落とす', _BI,
     '            problems.append(f"{where}「{part}」は「選択肢：言葉」の形です")\n',
     '',
     ('tests.test_business_import.Import.test_do_words_written_in_the_settings_come_first',)),
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


def _py_copy(change: tuple[str, str, str] | None = None) -> Path:
    """app の写し（仮の場所。試験・決まりの見張り・表の作りも写す）を作り、change＝(場所, 置き換え元, 置き換え先) があれば、その
    1か所だけを置き換える（置き換え元がちょうど1つでなければ、壊し試しの形がずれているので止める）。"""
    root = Path(tempfile.mkdtemp(prefix="salescall-mut-")) / "app"
    shutil.copytree(APP, root, ignore=shutil.ignore_patterns("__pycache__", ".venv"))
    if change is not None:
        rel, old, new = change
        path = root / rel
        text = path.read_text(encoding="utf-8")
        if text.count(old) != 1:
            shutil.rmtree(root.parent, ignore_errors=True)
            raise AssertionError(f"壊し試しの置き換え元が {rel} に1つだけありません：{old!r}")
        path.write_text(text.replace(old, new), encoding="utf-8")
    return root


def _run_tests_in(root: Path, owner_dsn: str, app_dsn: str, names: tuple[str, ...]) -> bool:
    """写し（root）の部品と試験で、名前の試験だけを別の Python で走らせる。通れば True。"""
    code = ("import sys, os, unittest; sys.path[:0] = [sys.argv[1] + '/src', sys.argv[1]]; "
            "suite = unittest.defaultTestLoader.loadTestsFromNames(sys.argv[2:]); "
            "ok = unittest.TextTestRunner(stream=open(os.devnull, 'w')).run(suite).wasSuccessful(); sys.exit(0 if ok else 1)")
    env = dict(os.environ, SALESCALL_TEST_APP_DSN=app_dsn, SALESCALL_TEST_OWNER_DSN=owner_dsn)
    return subprocess.run([sys.executable, "-c", code, str(root), *names], cwd=root, env=env).returncode == 0


def _missing_tests() -> list[str]:
    """壊し試しが指す試験のうち、見つからない名前（試験の名前を変えた・消した）。⚠️ 見つからない名前は「読み込めない」という
    落ちた試験になり、守りを外しても落ちたと数えていた（2026-10-19 の見直し：11通りが何も確かめていなかった）。"""
    sys.path.insert(0, str(APP))
    missing = []
    for tests in [t for _, _, t in MUTATIONS] + [m[4] for m in PY_MUTATIONS]:
        for name in tests:
            suite = unittest.defaultTestLoader.loadTestsFromName(name)
            if any(type(t).__name__ == "_FailedTest" for t in suite):
                missing.append(name)
    return missing


def _misplaced_py_mutations() -> list[str]:
    """Python の壊し試しのうち、置き換え元が今の部品にちょうど1つない題（部品を書き換えたのに、壊し試しを合わせていない）。⚠️ これまで
    は、その壊し試しの番が来てから止まっていた（2026-10-31：0158 で書き換えた行を、0155 の壊し試しが指したまま、全部の試験の途中で
    止まった）。⇒ 走らせる前に、表の作りの壊し試し（`_edited`。読み込むときに確かめる）と同じに、全部を確かめる。"""
    return [title for title, rel, old, _, _ in PY_MUTATIONS if (APP / rel).read_text(encoding="utf-8").count(old) != 1]


def main() -> int:
    missing = _missing_tests()
    for name in missing:
        print("壊し試しが指す試験が見つからない：", name)
    misplaced = _misplaced_py_mutations()
    for title in misplaced:
        print("壊し試しの置き換え元が、今の部品にちょうど1つない：", title)
    if missing or misplaced:
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
        # Python の判断をわざと外して、その試験が本当に落ちるか（写しで。まず、置き換えない写しで試験が通ることを確かめる＝写したこと
        # そのもので落ちて「見張れている」と出ないように）
        names = tuple(dict.fromkeys(n for m in PY_MUTATIONS for n in m[4]))
        dbname, owner_dsn, app_dsn = _fresh_db(superuser_dsn)
        root = _py_copy()
        try:
            same = _run_tests_in(root, owner_dsn, app_dsn, names)
        finally:
            _drop_db(superuser_dsn, dbname)
            shutil.rmtree(root.parent, ignore_errors=True)
        if not same:
            print("⛔ 置き換えない写しで、Python の壊し試しの試験が通らない（写し方の誤り）")
            return 1
        for title, rel, old, new, tests in PY_MUTATIONS:
            dbname, owner_dsn, app_dsn = _fresh_db(superuser_dsn)
            root = _py_copy((rel, old, new))
            try:
                caught = not _run_tests_in(root, owner_dsn, app_dsn, tests)
            finally:
                _drop_db(superuser_dsn, dbname)
                shutil.rmtree(root.parent, ignore_errors=True)
            print(f"判断を外す：{title} ⇒ {'試験が落ちた（見張れている）' if caught else '⛔ 試験が通ってしまった'}")
            if not caught:
                weak.append(title)
        if weak:
            print("⛔ 守り・判断を外しても落ちない試験がある：", "、".join(weak))
            return 1
        print("守り・判断をわざと外す確かめ：すべて、試験が落ちた")
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
