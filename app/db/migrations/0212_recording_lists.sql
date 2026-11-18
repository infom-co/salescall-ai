-- 0212 版の録音の部分の一覧（会話の担当の録音の道具が作って渡す）を、版に残す（2026-11-08。書いた人：CL-2。DESIGN.md 3-8-3 の【0212】・
--      2章 Q170〜・15-2 の700〜）
--
-- ✅ 計画書 6-1 (4)（2026-11-06 ユーザー）：「録音が必要な部分がある場合のみ必要な部分だけ録音を実施します。それはダッシュボードにも警告と
--    して出し、それができれば新しい版でも電話がかけられるようになります。」
-- ✅ CL-1 の連絡帳 4-120（CL-2 の連絡帳 4-236 の (d) への答え）：「こちらの録音の道具が、版の部分の一覧（make_ack_clips.script_table）を作り、
--    あなたの needed_parts に頼らず…（部分の名前と業務設定の項目の対応は、こちらの1か所だけが持つ）」。
--
-- ⛔ 前の作りの食い違い：「その版で、どの部分の録音が要るか」を2か所が別々に決めていた。CL-2 は業務設定の項目の違い（想定問答と足した名前）
--    から要る部分を決め（core/recordings._changes）、会話の担当は自分の部分の一覧で録っていた。CL-2 の答えは想定問答のほかの項目が違う版では
--    出せず（要る部分は空・組を作る道も無い）、会話の担当の一覧と名前や文が1つでも違うと complete が断った。
--
-- (1) 一覧（recording_lists と、その部分 recording_list_parts）：その版の業務の録音の組に要る部分の名前・文・名前の読み。作るのは会話の担当の
--     録音の道具（core/recordings.list_parts を呼ぶ）。渡し直しは新しい一覧を足すだけ（いまの一覧は、その版のいちばん新しい一覧＝list_no の
--     大きいもの）。中身は変えない・消さない。部分は、その一覧を作った取引でだけ足せる（あとから一覧に部分を足せない）。
-- (2) その版のいまの一覧（recording_list_of の1か所）：その版の一覧。無ければ、版の元（version_origins）を近い順にたどり、中身（業務設定の中身・
--     業務の決まり・重ねる標準の版）がまったく同じ版の一覧（写し・中身を変えずに足した版は、会話の担当の一覧も同じ＝渡し直していただかない）。
--     中身の違う版に当たったら、そこで止める（その先の一覧は使わない）。

CREATE TABLE salescall.recording_lists (
  tenant_id   uuid NOT NULL,
  client_id   uuid NOT NULL,
  list_id     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  list_no     bigint GENERATED ALWAYS AS IDENTITY UNIQUE,   -- 足した順（いまの一覧は、その版で、いちばん大きいもの。データベースが決める）
  version_id  uuid NOT NULL,
  recorded_by text NOT NULL DEFAULT salescall.cur_actor() CHECK (btrim(recorded_by) <> ''),
  recorded_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, list_id),
  FOREIGN KEY (tenant_id, client_id) REFERENCES salescall.clients (tenant_id, client_id),
  FOREIGN KEY (client_id, version_id) REFERENCES salescall.business_versions (client_id, version_id)
);
CREATE INDEX recording_lists_by_version ON salescall.recording_lists (version_id, list_no);

CREATE TABLE salescall.recording_list_parts (
  tenant_id   uuid NOT NULL,
  client_id   uuid NOT NULL,
  list_id     uuid NOT NULL,
  part        text NOT NULL CHECK (btrim(part) <> ''),               -- 流す単位の名前（会話の側の名前）
  spoken_text text NOT NULL CHECK (btrim(spoken_text) <> ''),        -- 録音の文
  readings    jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(readings) = 'object'),   -- その部分が話す名前の読み（名前 → ひらがな）
  PRIMARY KEY (list_id, part),
  FOREIGN KEY (tenant_id, list_id) REFERENCES salescall.recording_lists (tenant_id, list_id)
);

-- 一覧を書いた方は、いま操作している方。時刻はデータベースが決める
CREATE FUNCTION salescall.recording_list_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  NEW.recorded_at := now();
  IF NEW.recorded_by IS DISTINCT FROM salescall.cur_actor() THEN
    RAISE EXCEPTION '録音の部分の一覧を書いた方は、いま操作している方です' USING ERRCODE = 'check_violation',
      CONSTRAINT = 'recording_list_recorded_by';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER recording_list_guard BEFORE INSERT ON salescall.recording_lists
  FOR EACH ROW EXECUTE FUNCTION salescall.recording_list_guard();

-- 部分は、その一覧を作った取引でだけ足せる（同じ依頼者）
CREATE FUNCTION salescall.recording_list_part_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM salescall.recording_lists l
                 WHERE l.list_id = NEW.list_id AND l.client_id = NEW.client_id AND l.recorded_at = now()) THEN
    RAISE EXCEPTION '録音の部分の一覧に、あとから部分を足せません（渡し直すときは新しい一覧）' USING ERRCODE = 'check_violation',
      CONSTRAINT = 'recording_list_part_made_now';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER recording_list_part_guard BEFORE INSERT ON salescall.recording_list_parts
  FOR EACH ROW EXECUTE FUNCTION salescall.recording_list_part_guard();

CREATE TRIGGER recording_lists_frozen BEFORE UPDATE OR DELETE ON salescall.recording_lists
  FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change();
CREATE TRIGGER recording_list_parts_frozen BEFORE UPDATE OR DELETE ON salescall.recording_list_parts
  FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change();

ALTER TABLE salescall.recording_lists ENABLE ROW LEVEL SECURITY;
CREATE POLICY client_scope ON salescall.recording_lists
  USING (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client())
  WITH CHECK (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client());
ALTER TABLE salescall.recording_list_parts ENABLE ROW LEVEL SECURITY;
CREATE POLICY client_scope ON salescall.recording_list_parts
  USING (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client())
  WITH CHECK (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client());
GRANT SELECT, INSERT ON salescall.recording_lists TO salescall_app;
GRANT SELECT, INSERT ON salescall.recording_list_parts TO salescall_app;

-- ═════════ (2) その版のいまの一覧（ここだけ） ═════════
CREATE FUNCTION salescall.recording_list_of(p_version uuid) RETURNS uuid LANGUAGE sql STABLE
SET search_path = salescall, pg_temp AS $$
  WITH chain AS (
    SELECT p_version AS v, 0 AS depth
    UNION ALL
    SELECT o.version_id, o.depth FROM salescall.version_origins(p_version) o),
  info AS (
    SELECT ch.v, ch.depth, c.content, r.rules, b.standard_id FROM chain ch
    JOIN salescall.business_versions b ON b.version_id = ch.v
    LEFT JOIN salescall.business_contents c ON c.version_id = ch.v LEFT JOIN salescall.business_rules r ON r.version_id = ch.v),
  same AS (   -- その版そのもの、または中身（業務設定の中身・業務の決まり・重ねる標準の版）がまったく同じ元の版（中身の無い版は同じとみない）
    SELECT i.v, i.depth, i.depth = 0 OR coalesce(i.content = me.content AND i.rules IS NOT DISTINCT FROM me.rules
                                                 AND i.standard_id IS NOT DISTINCT FROM me.standard_id, false) AS same
    FROM info i CROSS JOIN (SELECT content, rules, standard_id FROM info WHERE depth = 0) me)
  SELECT l.list_id FROM same s JOIN salescall.recording_lists l ON l.version_id = s.v
  WHERE s.depth < coalesce((SELECT min(x.depth) FROM same x WHERE NOT x.same), 2147483647)
  ORDER BY s.depth, l.list_no DESC
  LIMIT 1
$$;
REVOKE ALL ON FUNCTION salescall.recording_list_of(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.recording_list_of(uuid) TO salescall_app;
