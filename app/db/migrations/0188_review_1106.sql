-- 0188 2026-11-06 の見直し（3つに分けた第三者の見直し (A)(C) の直し。書いた人：CL-2）

-- ═════════ (C) 前の電話が今日か：電話が終わった時刻で決める・電話ごとの1か所 ═════════
-- 0186 の same_jst_day に、電話をかけ始めた時刻（claimed_at）を渡していた。23時58分にかけ始めて0時03分に切れた電話のすぐあとの
-- かけ直しで「前回は」と言った（切れたのは今日）。⇒ 電話が終わった時刻（終わっていなければかけ始めた時刻）で決める。お詫びの電話の
-- 文（core/apology._order）と、途中で切れた電話の続きの予約証（core/apology.when）の両方が、この1か所を読む。
CREATE FUNCTION salescall.call_today(p_attempt uuid) RETURNS boolean LANGUAGE sql STABLE
SET search_path = salescall, pg_temp AS $$
  SELECT salescall.same_jst_day(coalesce(a.ended_at, a.claimed_at)) FROM salescall.attempts a WHERE a.attempt_id = p_attempt
$$;
REVOKE ALL ON FUNCTION salescall.call_today(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.call_today(uuid) TO salescall_app;

-- ═════════ (A) 番号の直し：番号の立場は「その時刻」で読む1か所・お客様が指定した番号・サブの守り・同じ実施回の番号 ═════════
-- 2026-11-06 ユーザー（見直しの伺いへのお答え）：「お客様の言った番号へ」（直したあと、新しい番号で話せたお客様が前の番号へかけてと
-- 言ったら、その番号へかける）。
-- (1) お客様が電話で指定した番号の記録（task_designations）。書くのは core/tasks.add_callee_number の1か所（お客様が言った番号を行の
--     番号に足す所＝言われた番号がもう行にあっても書く）。効くのは、いちばん新しい直しより後に指定されたものだけ（直しは契約者の
--     判断なので、直す前の指定より後の直しが勝つ）。読むのは下の task_number_replaced_at の1か所（指定された番号は、前にかける番号
--     だった番号に入らない＝次の動きの current_dial が、いまかける番号へ替えない）。
CREATE TABLE salescall.task_designations (
  tenant_id   uuid NOT NULL,
  client_id   uuid NOT NULL,
  seq         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  task_id     uuid NOT NULL,
  contact_id  uuid NOT NULL,
  attempt_id  uuid NOT NULL,
  recorded_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (tenant_id, client_id) REFERENCES salescall.clients (tenant_id, client_id),
  FOREIGN KEY (client_id, task_id, contact_id) REFERENCES salescall.task_numbers (client_id, task_id, contact_id),
  FOREIGN KEY (client_id, attempt_id, task_id) REFERENCES salescall.attempts (client_id, attempt_id, task_id)
);
CREATE INDEX task_designations_latest ON salescall.task_designations (task_id, seq DESC);
CREATE TRIGGER stamp_now BEFORE INSERT ON salescall.task_designations FOR EACH ROW EXECUTE FUNCTION salescall.stamp_now('recorded_at');
CREATE TRIGGER task_designations_frozen BEFORE UPDATE OR DELETE ON salescall.task_designations
  FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change();
ALTER TABLE salescall.task_designations ENABLE ROW LEVEL SECURITY;
CREATE POLICY client_scope ON salescall.task_designations
  USING (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client())
  WITH CHECK (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client());
GRANT SELECT, INSERT ON salescall.task_designations TO salescall_app;

-- (2) 番号の立場を、その時刻で読む1か所。⛔ 見直し (A) の2：前は「いま」の立場だけで読み、直したあと前の番号で話せた電話が、あとで
--     その番号がサブになると「新しい番号の回数」に数えられ、約束の回の数え直しも効かなかった（話せなかったときと食い違った）。
--     いちばん新しい直しより後で、その時刻まで（含む）に記録したもの。
CREATE FUNCTION salescall.task_sub_contact_at(p_task uuid, p_at timestamptz) RETURNS uuid LANGUAGE sql STABLE
SET search_path = salescall, pg_temp AS $$
  SELECT s.contact_id FROM salescall.task_sub_numbers s
  WHERE s.task_id = p_task AND s.recorded_at >= salescall.task_count_from(p_task) AND s.recorded_at <= p_at
    AND s.contact_id IS DISTINCT FROM salescall.task_dial_contact(p_task)
  ORDER BY s.seq DESC LIMIT 1
$$;
CREATE FUNCTION salescall.task_designated_contact_at(p_task uuid, p_at timestamptz) RETURNS uuid LANGUAGE sql STABLE
SET search_path = salescall, pg_temp AS $$
  SELECT d.contact_id FROM salescall.task_designations d
  WHERE d.task_id = p_task AND d.recorded_at >= salescall.task_count_from(p_task) AND d.recorded_at <= p_at
  ORDER BY d.seq DESC LIMIT 1
$$;
-- 前にかける番号だった番号（その時刻で）：いまかける番号でも、その時刻のサブでも、その時刻にお客様が指定した番号でもない、取り込んだ番号・
-- 前に直した番号
CREATE FUNCTION salescall.task_number_replaced_at(p_task uuid, p_contact uuid, p_at timestamptz) RETURNS boolean LANGUAGE sql STABLE
SET search_path = salescall, pg_temp AS $$
  SELECT p_contact IS DISTINCT FROM salescall.task_dial_contact(p_task)
     AND p_contact IS DISTINCT FROM salescall.task_sub_contact_at(p_task, p_at)
     AND p_contact IS DISTINCT FROM salescall.task_designated_contact_at(p_task, p_at)
     AND (p_contact = (SELECT t.contact_id FROM salescall.tasks t WHERE t.task_id = p_task)
          OR EXISTS (SELECT 1 FROM salescall.task_dial_fixes f WHERE f.task_id = p_task AND f.contact_id = p_contact))
$$;
-- いまの立場は、その時刻＝いまで読む（同じ1か所）
CREATE OR REPLACE FUNCTION salescall.task_number_replaced(p_task uuid, p_contact uuid) RETURNS boolean LANGUAGE sql STABLE
SET search_path = salescall, pg_temp AS $$ SELECT salescall.task_number_replaced_at(p_task, p_contact, now()) $$;
CREATE OR REPLACE FUNCTION salescall.task_sub_contact(p_task uuid) RETURNS uuid LANGUAGE sql STABLE
SET search_path = salescall, pg_temp AS $$ SELECT salescall.task_sub_contact_at(p_task, now()) $$;
REVOKE ALL ON FUNCTION salescall.task_sub_contact_at(uuid, timestamptz), salescall.task_designated_contact_at(uuid, timestamptz),
  salescall.task_number_replaced_at(uuid, uuid, timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.task_sub_contact_at(uuid, timestamptz), salescall.task_designated_contact_at(uuid, timestamptz),
  salescall.task_number_replaced_at(uuid, uuid, timestamptz) TO salescall_app;

-- (3) サブの番号の守り（⛔ 見直し (A) の4：決まりが Python の1か所だけにあり、その通話でかけていない番号・直す前の通話でも書けた）：
--     サブに書けるのは、その通話でかけた番号で、直したあとの通話で、かけたときに前にかける番号だった番号だけ。1つの通話に1つ。時刻は
--     データベースが決める。
CREATE FUNCTION salescall.task_sub_number_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM salescall.attempts a WHERE a.attempt_id = NEW.attempt_id AND a.task_id = NEW.task_id
                   AND a.contact_id = NEW.contact_id AND a.claimed_at >= salescall.task_count_from(a.task_id)
                   AND salescall.task_number_replaced_at(a.task_id, a.contact_id, a.claimed_at)) THEN
    RAISE EXCEPTION 'サブの番号にできるのは、直したあと前にかける番号だった番号へかけた、その通話の番号だけです'
      USING ERRCODE = 'check_violation', CONSTRAINT = 'task_sub_number_of_call';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER task_sub_number_guard BEFORE INSERT ON salescall.task_sub_numbers
  FOR EACH ROW EXECUTE FUNCTION salescall.task_sub_number_guard();
CREATE TRIGGER stamp_now BEFORE INSERT ON salescall.task_sub_numbers FOR EACH ROW EXECUTE FUNCTION salescall.stamp_now('recorded_at');
CREATE UNIQUE INDEX task_sub_numbers_one_per_call ON salescall.task_sub_numbers (attempt_id);

-- (4) 同じ実施回のほかの行が、いまかけている番号へは直せない（⛔ 見直し (A) の3：1つの実施回で同じ連絡先は1行＝設計書 3-1 が、
--     いまかける番号には効いていなかった。同じお客様に2つの行からかかりえた）。
CREATE OR REPLACE FUNCTION salescall.task_dial_fix_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF salescall.cur_actor_kind() IS DISTINCT FROM 'operator' AND salescall.cur_actor_kind() IS DISTINCT FROM 'admin' THEN
    RAISE EXCEPTION 'かける番号を直せるのは、管理者か契約者だけです' USING ERRCODE = 'insufficient_privilege',
      CONSTRAINT = 'task_dial_fix_by_person';
  END IF;
  IF NEW.recorded_by IS DISTINCT FROM salescall.cur_actor() THEN
    RAISE EXCEPTION '記録した方は、いま操作している方です' USING ERRCODE = 'check_violation', CONSTRAINT = 'task_dial_fix_recorded_by';
  END IF;
  IF EXISTS (SELECT 1 FROM salescall.tasks me JOIN salescall.tasks o ON o.run_id = me.run_id AND o.task_id <> me.task_id
             WHERE me.task_id = NEW.task_id
               AND (salescall.task_dial_contact(o.task_id) = NEW.contact_id OR salescall.task_sub_contact(o.task_id) = NEW.contact_id)) THEN
    RAISE EXCEPTION 'その番号は、同じ実施回のほかの行がかけている番号です' USING ERRCODE = 'unique_violation',
      CONSTRAINT = 'task_dial_fix_one_row_per_number';
  END IF;
  RETURN NEW;
END $$;
