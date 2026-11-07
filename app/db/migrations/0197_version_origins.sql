-- 0197 版の元・版のつながりをたどる1か所・置き換えられた解決策（2026-11-07 第三者の見直し (2) の直し。書いた人：CL-2。
-- DESIGN.md 3-8-3 の【0197】・15-2 の511）
--
-- (1) 版の元（その版を、どの版から作ったか）を1か所にする。元は2種類：
--     - つながり（version_successions。版を足す＝business_settings.derive。きっかけ standard・solution・override）
--     - 写し（version_copies。この移行で作る。中身をそのまま写した＝business_settings.save_from：以前の版へ戻す・手で前の版に戻す・
--       業務を複製する）
--     写しを version_successions に入れないのは、つながりの先の版は、そろえば自動で実施回に割り当てる（adopt_ready）ため（手で戻した写しを
--     戻す前の版の先にすると、戻した判断が自動で上書きされる）。どちらの表も、1つの版の元は1つだけ。
--     ⛔ 前は、録音の引き継ぎ（core/recordings）が「つながりの1つ前の版」だけを見て、写しの元を知らなかった：録音していない途中の版から
--     足した版・写した版には、録音の要る部分も警告も出ず、録音が2回に分かれた（見直し (2) の2）。品質で止まった版の写しは、止まった版と
--     見なされなかった（見直し (2) の5）。
-- (2) 版のつながり・版の元をたどるのは、ここの関数だけ（⛔ 前は同じ再帰の問い合わせが3か所＝business_settings.successor_tip・adopt_ready・
--     client_solution_line_guard に写されていた。見直し (2) の8）：
--     - version_line(版)＝その版の後の版（近い順。きっかけと何を変えたか）。version_tip(版)＝いちばん後の版（無ければその版）。
--     - version_origins(版)＝その版の元をたどった版（近い順。写しか）。version_copy_root(版)＝写しの元をたどった、中身の同じいちばん元の版。
-- (3) 版のつながりの後の版・写しの版は、その取引で作った版だけ（⛔ 前は、契約者の取引が、どの後の版でも直接つなげられ、つなげると
--     adopt_ready が自動で割り当てた。見直し (2) の8）。版の作られた時刻はデータベースが決める（その取引の時刻）。1つの版の元は1つ・
--     輪にならない（つながりと写しを合わせて）。
-- (4) 依頼者の解決策が、取り込み直した中身（同じ問いの名前の答え・「使わない」）で置き換えられたことを残す（client_solution_replacements。
--     ⛔ 前は残す所が無く、取り込んだ中身が先になった解決策が「流れに入っていない前の解決策」に出続け、知らせ solution_earlier が毎回出て、
--     選べば取り込んだ中身を上書きした。見直し (2) の4）。書くのは取り込み直しの登録（core/business_import.register_shaped）だけ。
-- (5) ✅ 2026-11-07 ユーザー（伺い「契約者が手で前の版に戻して外した解決策を、取り込み直しで自動で入れ直すか」へのお答え）：
--     「契約者が確かめる」＝計画書 6-1 の (3) が (2) より先。手で外した解決策は、取り込み直しでも自動では入れない（core/client_solutions.keep_in
--     に入れない）。「流れに入っていない前の解決策」の一覧（not_in_line）には出し、契約者が選んだもの（add_earlier）だけを入れる。
--     「手で外した」を残す（client_solution_removals。(4) の「置き換えられた」とは別の理由）。当たるのは、管理者か契約者が実施回に版を手で
--     割り当てたとき（business_settings.assign。自動の割り当て・実施回を作るときの最初の割り当ては除く）、前の版の流れ（いちばん後の版）に
--     入っていて、割り当てた版に入っていない解決策（core/client_solutions.removed_by_hand の1か所）。そのあと契約者が選んで足せば
--     （流れの結果に選んだ方が残る＝0194）、外したことは消える（時刻で比べる。流れの結果の時刻もデータベースが決める）。

-- ═════════ (3) 版の作られた時刻 ═════════
CREATE TRIGGER stamp_now BEFORE INSERT ON salescall.business_versions
  FOR EACH ROW EXECUTE FUNCTION salescall.stamp_now('created_at');

-- ═════════ (1) 写しの元 ═════════
CREATE TABLE salescall.version_copies (
  tenant_id   uuid NOT NULL,
  client_id   uuid NOT NULL,
  version_id  uuid PRIMARY KEY,                      -- 写した版（1つの版の写しの元は1つ）
  copied_from uuid NOT NULL,                         -- 写しの元の版（同じ依頼者。業務の複製では、ほかの業務の版）
  recorded_by text NOT NULL CHECK (btrim(recorded_by) <> ''),
  recorded_at timestamptz NOT NULL DEFAULT now(),
  CHECK (version_id <> copied_from),
  FOREIGN KEY (tenant_id, client_id) REFERENCES salescall.clients (tenant_id, client_id),
  FOREIGN KEY (client_id, version_id) REFERENCES salescall.business_versions (client_id, version_id),
  FOREIGN KEY (client_id, copied_from) REFERENCES salescall.business_versions (client_id, version_id)
);
CREATE TRIGGER version_copies_frozen BEFORE UPDATE OR DELETE ON salescall.version_copies
  FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change();
ALTER TABLE salescall.version_copies ENABLE ROW LEVEL SECURITY;
CREATE POLICY client_scope ON salescall.version_copies
  USING (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client())
  WITH CHECK (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client());
GRANT SELECT, INSERT ON salescall.version_copies TO salescall_app;

-- ═════════ (2) たどる関数（ここだけ） ═════════
-- その版のすぐ前の元（つながりの前の版か、写しの元。どちらか1つ＝守りは下）
CREATE FUNCTION salescall.version_origin(p_version uuid) RETURNS TABLE (origin uuid, copied boolean) LANGUAGE sql STABLE
SET search_path = salescall, pg_temp AS $$
  SELECT s.from_version, false FROM salescall.version_successions s WHERE s.to_version = p_version
  UNION ALL
  SELECT c.copied_from, true FROM salescall.version_copies c WHERE c.version_id = p_version
$$;
REVOKE ALL ON FUNCTION salescall.version_origin(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.version_origin(uuid) TO salescall_app;

-- その版の元をたどった版（近い順。depth＝いくつ前か・copied＝そのひとつ後の版が、この版の写しか）
CREATE FUNCTION salescall.version_origins(p_version uuid) RETURNS TABLE (version_id uuid, depth integer, copied boolean)
LANGUAGE sql STABLE SET search_path = salescall, pg_temp AS $$
  WITH RECURSIVE o(v, depth, copied) AS (
    SELECT x.origin, 1, x.copied FROM salescall.version_origin(p_version) x
    UNION ALL
    SELECT x.origin, o.depth + 1, x.copied FROM o CROSS JOIN LATERAL salescall.version_origin(o.v) x)
  SELECT v, depth, copied FROM o ORDER BY depth
$$;
REVOKE ALL ON FUNCTION salescall.version_origins(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.version_origins(uuid) TO salescall_app;

-- 写しの元だけをたどった、中身の同じいちばん元の版（写しでない版は、その版）。2つの版の答えが同じなら、中身の同じ版（品質で止まった版
-- そのものか＝core/quality_stage が読む）
CREATE FUNCTION salescall.version_copy_root(p_version uuid) RETURNS uuid LANGUAGE sql STABLE
SET search_path = salescall, pg_temp AS $$
  WITH RECURSIVE o(v, depth) AS (
    SELECT p_version, 0
    UNION ALL
    SELECT c.copied_from, o.depth + 1 FROM o JOIN salescall.version_copies c ON c.version_id = o.v)
  SELECT v FROM o ORDER BY depth DESC LIMIT 1
$$;
REVOKE ALL ON FUNCTION salescall.version_copy_root(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.version_copy_root(uuid) TO salescall_app;

-- その版の後の版（つながりだけ。近い順。きっかけ・何を変えたか）
CREATE FUNCTION salescall.version_line(p_version uuid) RETURNS TABLE (version_id uuid, depth integer, cause text, note text)
LANGUAGE sql STABLE SET search_path = salescall, pg_temp AS $$
  WITH RECURSIVE l(v, depth, cause, note) AS (
    SELECT s.to_version, 1, s.cause, s.note FROM salescall.version_successions s WHERE s.from_version = p_version
    UNION ALL
    SELECT s.to_version, l.depth + 1, s.cause, s.note FROM salescall.version_successions s JOIN l ON s.from_version = l.v)
  SELECT v, depth, cause, note FROM l ORDER BY depth
$$;
REVOKE ALL ON FUNCTION salescall.version_line(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.version_line(uuid) TO salescall_app;

-- その版のつながりの、いちばん後の版（後の版が無ければ、その版）
CREATE FUNCTION salescall.version_tip(p_version uuid) RETURNS uuid LANGUAGE sql STABLE
SET search_path = salescall, pg_temp AS $$
  SELECT coalesce((SELECT l.version_id FROM salescall.version_line(p_version) l ORDER BY l.depth DESC LIMIT 1), p_version)
$$;
REVOKE ALL ON FUNCTION salescall.version_tip(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.version_tip(uuid) TO salescall_app;

-- その版を、その取引で作ったか（作られた時刻はデータベースが決める＝上の (3)。つながり・写し・置き換えの守りが読む1か所）
CREATE FUNCTION salescall.version_made_now(p_version uuid) RETURNS boolean LANGUAGE sql STABLE
SET search_path = salescall, pg_temp AS $$
  SELECT coalesce((SELECT v.created_at = now() FROM salescall.business_versions v WHERE v.version_id = p_version), false)
$$;
REVOKE ALL ON FUNCTION salescall.version_made_now(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.version_made_now(uuid) TO salescall_app;

-- ═════════ (3) 守り ═════════
-- 版の元を足す守り（つながりと写しの両方が呼ぶ1か所）：その版は、その取引で作った版・まだ元が無い・輪にならない（読んで断るだけ＝STABLE）
CREATE FUNCTION salescall.version_origin_check(p_version uuid, p_origin uuid) RETURNS void LANGUAGE plpgsql STABLE
SET search_path = salescall, pg_temp AS $$
BEGIN
  IF NOT salescall.version_made_now(p_version) THEN
    RAISE EXCEPTION '後の版・写しの版は、その取引で作った版だけです（前からある版を、ほかの版の後につなげない）'
      USING ERRCODE = 'check_violation', CONSTRAINT = 'version_origin_made_now';
  END IF;
  IF EXISTS (SELECT 1 FROM salescall.version_origin(p_version)) THEN
    RAISE EXCEPTION '1つの版の元は1つだけです（つながりか写しの、どちらか）' USING ERRCODE = 'check_violation',
      CONSTRAINT = 'version_origin_one';
  END IF;
  IF p_version = p_origin OR EXISTS (SELECT 1 FROM salescall.version_origins(p_origin) o WHERE o.version_id = p_version) THEN
    RAISE EXCEPTION '版の元が輪になります' USING ERRCODE = 'check_violation', CONSTRAINT = 'version_origin_no_loop';
  END IF;
END $$;
REVOKE ALL ON FUNCTION salescall.version_origin_check(uuid, uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.version_origin_check(uuid, uuid) TO salescall_app;

CREATE FUNCTION salescall.version_copy_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  NEW.recorded_at := now();
  IF NEW.recorded_by IS DISTINCT FROM salescall.cur_actor() THEN
    RAISE EXCEPTION '記録した方は、いま操作している方です' USING ERRCODE = 'check_violation', CONSTRAINT = 'version_copy_recorded_by';
  END IF;
  PERFORM salescall.version_origin_check(NEW.version_id, NEW.copied_from);
  RETURN NEW;
END $$;
CREATE TRIGGER version_copy_guard BEFORE INSERT ON salescall.version_copies
  FOR EACH ROW EXECUTE FUNCTION salescall.version_copy_guard();

CREATE OR REPLACE FUNCTION salescall.version_succession_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  NEW.recorded_at := now();
  IF NEW.recorded_by IS DISTINCT FROM salescall.cur_actor() THEN
    RAISE EXCEPTION '記録した方は、いま操作している方です' USING ERRCODE = 'check_violation', CONSTRAINT = 'version_succession_recorded_by';
  END IF;
  IF (SELECT version_no FROM salescall.business_versions WHERE version_id = NEW.to_version)
     <= (SELECT version_no FROM salescall.business_versions WHERE version_id = NEW.from_version) THEN
    RAISE EXCEPTION '後の版は、前の版より後に作った版だけです（逆向き・輪は作れない）' USING ERRCODE = 'check_violation',
      CONSTRAINT = 'version_succession_forward';
  END IF;
  IF (NEW.cause = 'standard') IS DISTINCT FROM (salescall.cur_actor_kind() = 'system') THEN
    RAISE EXCEPTION '標準の想定問答の版はシステムの処理だけ、解決策・上書きの版は管理者か契約者だけが足せます' USING ERRCODE = 'insufficient_privilege',
      CONSTRAINT = 'version_succession_cause_actor';
  END IF;
  PERFORM salescall.version_origin_check(NEW.to_version, NEW.from_version);
  RETURN NEW;
END $$;

-- 解決策の流れの結果の守り（0194 の形のまま。つながりの先をたどるのは version_line の1か所）
CREATE OR REPLACE FUNCTION salescall.client_solution_line_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.chosen_by IS NOT NULL THEN
    -- 契約者が「流れに入っていない前の解決策」の一覧から選んで足した（core/client_solutions.add_earlier）：選んだのは、いま操作している
    -- 管理者か契約者
    IF NEW.chosen_by IS DISTINCT FROM salescall.cur_actor()
       OR (salescall.cur_actor_kind() IS DISTINCT FROM 'operator' AND salescall.cur_actor_kind() IS DISTINCT FROM 'admin') THEN
      RAISE EXCEPTION '前の解決策を選んで足せるのは、いま操作している管理者か契約者だけです' USING ERRCODE = 'insufficient_privilege',
        CONSTRAINT = 'client_solution_line_chosen_by_person';
    END IF;
  ELSIF NOT EXISTS (SELECT 1 FROM salescall.client_solutions c WHERE c.solution_id = NEW.solution_id
                    AND c.recorded_by = salescall.cur_actor()) THEN
    RAISE EXCEPTION '流れごとの結果を書けるのは、その解決策を足した方だけです' USING ERRCODE = 'check_violation',
      CONSTRAINT = 'client_solution_line_by_recorder';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM salescall.client_solutions c JOIN salescall.tasks t ON t.task_id = c.task_id
                   JOIN salescall.runs r ON r.run_id = t.run_id
                   JOIN salescall.business_versions b ON b.version_id = NEW.base_version AND b.business_id = r.business_id
                 WHERE c.solution_id = NEW.solution_id)
     OR (NEW.version_id IS NOT NULL AND NEW.version_id <> NEW.base_version
         AND NOT EXISTS (SELECT 1 FROM salescall.version_line(NEW.base_version) l WHERE l.version_id = NEW.version_id)) THEN
    RAISE EXCEPTION '流れの版は、その解決策の業務の版で、足した版は足す元の版か、そのつながりの先の版だけです' USING ERRCODE = 'check_violation',
      CONSTRAINT = 'client_solution_line_versions';
  END IF;
  RETURN NEW;
END $$;

-- ═════════ (4) 取り込み直した中身で置き換えられた解決策 ═════════
CREATE TABLE salescall.client_solution_replacements (
  tenant_id   uuid NOT NULL,
  client_id   uuid NOT NULL,
  solution_id uuid PRIMARY KEY REFERENCES salescall.client_solutions (solution_id),
  version_id  uuid NOT NULL,                         -- 置き換えた中身の版（取り込み直して登録した版）
  recorded_by text NOT NULL CHECK (btrim(recorded_by) <> ''),
  recorded_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (tenant_id, client_id) REFERENCES salescall.clients (tenant_id, client_id),
  FOREIGN KEY (client_id, version_id) REFERENCES salescall.business_versions (client_id, version_id)
);
-- 置き換えた版は、その解決策の業務の版で、その取引で作った版（取り込み直しの登録と同じ取引）だけ。記録した方は、いま操作している方
CREATE FUNCTION salescall.client_solution_replacement_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  NEW.recorded_at := now();
  IF NEW.recorded_by IS DISTINCT FROM salescall.cur_actor() THEN
    RAISE EXCEPTION '記録した方は、いま操作している方です' USING ERRCODE = 'check_violation',
      CONSTRAINT = 'client_solution_replacement_recorded_by';
  END IF;
  IF NOT salescall.version_made_now(NEW.version_id) OR NOT EXISTS (
       SELECT 1 FROM salescall.client_solutions c JOIN salescall.tasks t ON t.task_id = c.task_id
         JOIN salescall.runs r ON r.run_id = t.run_id
         JOIN salescall.business_versions b ON b.version_id = NEW.version_id AND b.business_id = r.business_id
       WHERE c.solution_id = NEW.solution_id) THEN
    RAISE EXCEPTION '解決策を置き換えた版は、その解決策の業務の、その取引で取り込み直した版だけです' USING ERRCODE = 'check_violation',
      CONSTRAINT = 'client_solution_replacement_version';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER client_solution_replacement_guard BEFORE INSERT ON salescall.client_solution_replacements
  FOR EACH ROW EXECUTE FUNCTION salescall.client_solution_replacement_guard();
CREATE TRIGGER client_solution_replacements_frozen BEFORE UPDATE OR DELETE ON salescall.client_solution_replacements
  FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change();
ALTER TABLE salescall.client_solution_replacements ENABLE ROW LEVEL SECURITY;
CREATE POLICY client_scope ON salescall.client_solution_replacements
  USING (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client())
  WITH CHECK (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client());
GRANT SELECT, INSERT ON salescall.client_solution_replacements TO salescall_app;

-- ═════════ (5) 手で外した解決策 ═════════
ALTER TABLE salescall.client_solution_lines ADD COLUMN recorded_at timestamptz NOT NULL DEFAULT now();
CREATE TRIGGER stamp_now BEFORE INSERT ON salescall.client_solution_lines
  FOR EACH ROW EXECUTE FUNCTION salescall.stamp_now('recorded_at');

CREATE TABLE salescall.client_solution_removals (
  tenant_id   uuid NOT NULL,
  client_id   uuid NOT NULL,
  seq         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  solution_id uuid NOT NULL REFERENCES salescall.client_solutions (solution_id),
  version_id  uuid NOT NULL,                         -- 手で割り当てた版（その解決策が入っていない版）
  recorded_by text NOT NULL CHECK (btrim(recorded_by) <> ''),
  recorded_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (tenant_id, client_id) REFERENCES salescall.clients (tenant_id, client_id),
  FOREIGN KEY (client_id, version_id) REFERENCES salescall.business_versions (client_id, version_id)
);
CREATE INDEX client_solution_removals_solution ON salescall.client_solution_removals (solution_id, recorded_at DESC);
-- 書けるのは、その取引で、その版をいま操作している管理者か契約者が手で割り当てたとき（run_versions の割り当てる時刻もデータベースが決める
-- ＝0189）だけ。版はその解決策の業務の版
CREATE FUNCTION salescall.client_solution_removal_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  NEW.recorded_at := now();
  IF NEW.recorded_by IS DISTINCT FROM salescall.cur_actor() THEN
    RAISE EXCEPTION '記録した方は、いま操作している方です' USING ERRCODE = 'check_violation',
      CONSTRAINT = 'client_solution_removal_recorded_by';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM salescall.run_versions v WHERE v.version_id = NEW.version_id AND NOT v.auto
                   AND v.assigned_at = now() AND v.assigned_by = salescall.cur_actor())
     OR NOT EXISTS (SELECT 1 FROM salescall.client_solutions c JOIN salescall.tasks t ON t.task_id = c.task_id
                      JOIN salescall.runs r ON r.run_id = t.run_id
                      JOIN salescall.business_versions b ON b.version_id = NEW.version_id AND b.business_id = r.business_id
                    WHERE c.solution_id = NEW.solution_id) THEN
    RAISE EXCEPTION '手で外した解決策を残せるのは、その取引でその業務の版を手で割り当てたときだけです' USING ERRCODE = 'check_violation',
      CONSTRAINT = 'client_solution_removal_by_assignment';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER client_solution_removal_guard BEFORE INSERT ON salescall.client_solution_removals
  FOR EACH ROW EXECUTE FUNCTION salescall.client_solution_removal_guard();
CREATE TRIGGER client_solution_removals_frozen BEFORE UPDATE OR DELETE ON salescall.client_solution_removals
  FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change();
ALTER TABLE salescall.client_solution_removals ENABLE ROW LEVEL SECURITY;
CREATE POLICY client_scope ON salescall.client_solution_removals
  USING (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client())
  WITH CHECK (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client());
GRANT SELECT, INSERT ON salescall.client_solution_removals TO salescall_app;
