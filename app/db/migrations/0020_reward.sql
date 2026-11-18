-- 0020 報酬の決まりと、報酬に対する費用の割合（2026-10-15 ユーザー：「それより重要なのは、報酬に対して、費用が何％かかっているのかと
-- その制限だよ。…報酬より、費用が高いと赤字になる。それが一番の問題です」）。書いた人：CL-2
-- - 報酬の決まり（reward_terms。依頼主ごと・版つき・変更不可）：成果1件あたり・かけた1本あたり・話せた1本あたり・月額・時給の
--   組み合わせ（依頼主によって違う。2026-10-15 ユーザー）。いつから使うかを持ち、あとから足した決まりは、その時刻からの分に使う。
--   書くのは core/reward.py だけ。
-- - 時給の時間＝人が架電作業をしていた時間に当たる、システムがその依頼主の架電をしていた時間（2026-10-15 ユーザー：「通常は、
--   人が架電作業をしていた時間です。人の代わりにAIシステムがやるだけです」）。回線ごとに（1回線＝1人）、その依頼主の発信の
--   予約から終わりまでと、同じ回線で次もその依頼主の発信だったときの短い合間（budget.hourly_gap_max 以内）を足す。
-- - 費用の割合＝今月（日本時間の月）の費用 ÷ 今月の報酬。上限は設定 budget.max_cost_ratio（既定 30％。依頼主ごとに選べる。決定）。
--   上限を超えたら知らせるだけ。赤字（費用 ＞ 報酬）なら、その依頼主を止める（原因 deficit）。判断は core/usage.py の1か所。
--   判断し始める本数は決め打ちにしない：成果・話せた本数のぶれを、budget.judge_confidence の確かさで多めに見込んでも
--   上限・赤字になるときだけ判断する（報酬と費用から逆算される）。
ALTER TYPE salescall.stop_cause ADD VALUE 'deficit';    -- 赤字（今月の費用が今月の報酬を超えた）

CREATE TABLE salescall.reward_terms (
  tenant_id     uuid NOT NULL,
  client_id     uuid NOT NULL,
  terms_id      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  valid_from    timestamptz NOT NULL,
  per_completed_yen numeric NOT NULL DEFAULT 0 CHECK (per_completed_yen >= 0),  -- 成果1件（業務の状態が「完了」になった行）
  per_dial_yen      numeric NOT NULL DEFAULT 0 CHECK (per_dial_yen >= 0),       -- かけた1本（発信した試行）
  per_talk_yen      numeric NOT NULL DEFAULT 0 CHECK (per_talk_yen >= 0),       -- 話せた1本（会話に入った試行）
  monthly_yen       numeric NOT NULL DEFAULT 0 CHECK (monthly_yen >= 0),        -- 月額（その月の、決まりが効いていた時間で日割り）
  hourly_yen        numeric NOT NULL DEFAULT 0 CHECK (hourly_yen >= 0),         -- 時給（架電作業の時間 1時間あたり）
  note          text NOT NULL CHECK (btrim(note) <> ''),       -- 根拠（契約の版・取り決めの日 など。相手の個人情報は入れない）
  set_by        text NOT NULL CHECK (btrim(set_by) <> ''),
  created_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (client_id, valid_from),
  FOREIGN KEY (tenant_id, client_id) REFERENCES salescall.clients (tenant_id, client_id)
);
CREATE TRIGGER reward_terms_frozen BEFORE UPDATE OR DELETE ON salescall.reward_terms
  FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change();
ALTER TABLE salescall.reward_terms ENABLE ROW LEVEL SECURITY;
CREATE POLICY client_scope ON salescall.reward_terms
  USING (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client())
  WITH CHECK (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client());
GRANT SELECT, INSERT ON salescall.reward_terms TO salescall_app;

INSERT INTO salescall.setting_definitions (key, merge_kind, value_type, meaning, design_ref, bound_key, bound_side) VALUES
('budget.max_cost_ratio', 'override', 'ratio', '報酬に対する費用の割合の上限（今月。超えたら知らせるだけ。赤字なら止める）', 'DESIGN.md 12-1-2', NULL, NULL),
('budget.judge_confidence', 'system_only', 'ratio', '割合・赤字を判断するときの確かさ（成果・話せた本数のぶれを、この確かさで多めに見込んでも超えるときだけ判断する）', 'DESIGN.md 12-1-2', NULL, NULL),
('budget.hourly_gap_max', 'system_only', 'seconds', '時給の時間に足す、同じ回線で続けて同じ依頼主にかけたときの合間の上限', 'DESIGN.md 12-1-2', NULL, NULL);
INSERT INTO salescall.setting_values (key, scope, value, status, rationale, review_when, set_by) VALUES
('budget.max_cost_ratio', 'system', '0.30', 'decided', '2026-10-15 ユーザー決定：既定 30％、依頼主ごとに選べる', '依頼主ごとに見直す', 'CL-2'),
('budget.judge_confidence', 'system', '0.95', 'provisional', '統計でふつうに使う確かさ（20回に1回より少ない取り違えにする）。本数を決め打ちにせず、報酬と費用から逆算する（2026-10-15 ユーザー：「計算で逆算したらすぐに出るでしょ」）', '最初の有料の月の実測を見て', 'CL-2'),
('budget.hourly_gap_max', 'system', '300', 'provisional', '同じ回線で5分以内に同じ依頼主の次の発信があれば、続けて作業していた（人も1本ごとに短い合間がある）。門が待たせる時間（gate.denied_retry_wait）と同じ', '最初の時給の依頼主の実測を見て', 'CL-2');

-- 時給の時間の材料（取引の依頼主の分だけ）：試行ごとに、始めた時刻と、数える秒数（予約から終わりまで＋同じ回線の次の発信も
-- この依頼主なら、その合間（上限まで））。同じ回線のほかの依頼主の発信を見て合間を決めるので、表の持ち主の権限で読み、
-- 取引の依頼主の行だけを返す（ほかの依頼主の中身は返さない）。
CREATE FUNCTION salescall.work_seconds(p_from timestamptz, p_to timestamptz, p_gap_max integer)
RETURNS TABLE (at timestamptz, seconds numeric)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  WITH line_calls AS (
    SELECT a.client_id, a.claimed_at, a.ended_at,
           lead(a.client_id) OVER w AS next_client, lead(a.claimed_at) OVER w AS next_at
    FROM salescall.attempts a
    WHERE a.state NOT IN ('claimed', 'released') AND a.ended_at IS NOT NULL
      AND a.line_id IN (SELECT b.line_id FROM salescall.attempts b WHERE b.client_id = salescall.cur_client())
    WINDOW w AS (PARTITION BY a.line_id ORDER BY a.claimed_at)
  )
  SELECT c.claimed_at,
         extract(epoch FROM c.ended_at - greatest(c.claimed_at, p_from))
         + CASE WHEN c.next_client = c.client_id AND c.next_at - c.ended_at <= p_gap_max * interval '1 second'
                THEN extract(epoch FROM least(c.next_at, p_to) - c.ended_at) ELSE 0 END
  FROM line_calls c
  WHERE c.client_id = salescall.cur_client() AND salescall.cur_client() IS NOT NULL
    AND c.ended_at > p_from AND c.claimed_at <= p_to
$$;
REVOKE ALL ON FUNCTION salescall.work_seconds(timestamptz, timestamptz, integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION salescall.work_seconds(timestamptz, timestamptz, integer) TO salescall_app;
