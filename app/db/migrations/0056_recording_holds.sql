-- 0056 測り終わるまで消さない録音（2026-10-17 ユーザー決定 案A。Intent.md 11-4 第2版・16-3。DESIGN.md 3-8。書いた人：CL-2）
--
-- Intent.md 11-4 第2版：「検証モードの期間と、誤判定率を測るための抜き取りの対象は、保存を必須にする」。
-- 録音はハードディスクに2か月（契約者ごとの設定）で消える決まり（2026-10-17）と食い違っていた。
-- ユーザー（2026-10-17）：案A＝検証中の通話と、抜き取りの対象の通話は、測り終わるまで消さない。測り終わったら、ふつうの決まりに戻す。
--
-- (1) 録音の保留（recording_holds）：通話ごと・理由ごとに1行。理由は verification（検証モードの通話。締めのときに付ける）と
--     sample（誤判定率を測るために抜き取った通話。抜き取る処理が付ける）。
-- (2) 外すのは、測り終わったとき1回だけ（どの測定で使い終えたかを書く）。消したり、外した印を戻したりはできない。
-- (3) 古い録音を消す処理は、保留の付いた通話の録音を消さない（どの通話が保留かだけを返す関数。中身は返さない）。

CREATE TABLE salescall.recording_holds (
  tenant_id     uuid NOT NULL,
  client_id     uuid NOT NULL,
  attempt_id    uuid NOT NULL,
  reason        text NOT NULL CHECK (reason IN ('verification', 'sample')),
  held_at       timestamptz NOT NULL DEFAULT now(),
  released_at   timestamptz,
  released_for  text CHECK (released_for IS NULL OR btrim(released_for) <> ''),   -- 使い終えた測定の名前
  PRIMARY KEY (attempt_id, reason),
  CHECK ((released_at IS NULL) = (released_for IS NULL)),
  FOREIGN KEY (client_id, attempt_id) REFERENCES salescall.attempts (client_id, attempt_id)
);
CREATE INDEX recording_holds_open ON salescall.recording_holds (attempt_id) WHERE released_at IS NULL;

-- 変えてよいのは、測り終わって外すときの1回だけ
CREATE FUNCTION salescall.recording_hold_released_once() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' OR OLD.released_at IS NOT NULL OR NEW.released_at IS NULL
     OR (NEW.tenant_id, NEW.client_id, NEW.attempt_id, NEW.reason, NEW.held_at)
        IS DISTINCT FROM (OLD.tenant_id, OLD.client_id, OLD.attempt_id, OLD.reason, OLD.held_at) THEN
    RAISE EXCEPTION '録音の保留は、測り終わったときに1回だけ外せます' USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER recording_hold_released_once BEFORE UPDATE OR DELETE ON salescall.recording_holds
  FOR EACH ROW EXECUTE FUNCTION salescall.recording_hold_released_once();
ALTER TABLE salescall.recording_holds ENABLE ROW LEVEL SECURITY;
CREATE POLICY client_scope ON salescall.recording_holds
  USING (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client())
  WITH CHECK (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client());
GRANT SELECT, INSERT, UPDATE ON salescall.recording_holds TO salescall_app;

-- 渡した通話のうち、保留が付いている（まだ測り終わっていない）もの（古い録音を消す処理が使う。中身は返さない）
CREATE FUNCTION salescall.recordings_held(p_attempts uuid[]) RETURNS SETOF uuid
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  SELECT DISTINCT attempt_id FROM salescall.recording_holds WHERE attempt_id = ANY(p_attempts) AND released_at IS NULL
$$;
REVOKE ALL ON FUNCTION salescall.recordings_held(uuid[]) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.recordings_held(uuid[]) TO salescall_app;
