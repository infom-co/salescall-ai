-- 0020 報酬の決まりと、報酬に対する費用の割合（2026-10-15 ユーザー：「それより重要なのは、報酬に対して、費用が何％かかっているのかと
-- その制限だよ。…報酬より、費用が高いと赤字になる。それが一番の問題です」）。書いた人：CL-2
-- - 報酬の決まり（reward_terms。依頼主ごと・版つき・変更不可）：成果1件あたり・かけた1本あたり・話せた1本あたり・月額の組み合わせ
--   （依頼主によって違う。2026-10-15 ユーザー）。いつから使うかを持ち、あとから足した決まりは、その時刻からの分に使う。
--   ⏳ 時給は「何の時間か」をユーザーに確かめ中なので、まだ持たない。書くのは core/reward.py だけ。
-- - 費用の割合＝今月（日本時間の月）の費用 ÷ 今月の報酬。上限は設定 budget.max_cost_ratio（既定 30％。依頼主ごとに選べる。決定）。
--   上限を超えたら知らせるだけ。赤字（費用 ＞ 報酬）なら、その依頼主を止める（原因 deficit）。判断は core/usage.py の1か所。
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
('budget.max_cost_ratio', 'override', 'ratio', '報酬に対する費用の割合の上限（今月。超えたら知らせるだけ。赤字なら止める）', 'DESIGN.md 12-1', NULL, NULL),
('budget.deficit_min_dials', 'override', 'integer', '赤字・費用の割合を判断し始める、今月かけた本数（少ないうちは割合がぶれるので待つ）', 'DESIGN.md 12-1', NULL, NULL);
INSERT INTO salescall.setting_values (key, scope, value, status, rationale, review_when, set_by) VALUES
('budget.max_cost_ratio', 'system', '0.30', 'decided', '2026-10-15 ユーザー決定：既定 30％、依頼主ごとに選べる', '依頼主ごとに見直す', 'CL-2'),
('budget.deficit_min_dials', 'system', '30', 'provisional', '月の初めの数本だけで赤字と決めない。品質の段の最初の段（20件）より少し多い本数を仮に', 'ユーザーに決めていただく', 'CL-2');
