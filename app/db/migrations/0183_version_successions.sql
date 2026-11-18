-- 0183 版のつながり（版を足して、そろったら自動で替える1か所。2026-11-02 管理者のご判断。書いた人：CL-2。DESIGN.md 3-8-3 の (5)）
--
-- ✅ 2026-11-02 管理者：伺い2「案1でOK」（文・読み・声がまったく同じ部分は、承認した録音を使い回す）・(h) の流れ「新しい版の録音が
--    承認されるまでは、今の版のまま電話を続けます。承認されたら、その業務の実施回を自動で新しい版に替えます。電話は止めません。」
-- 版を変えるきっかけ（cause）：standard＝管理者が標準の想定問答を新しくした／solution＝依頼者の解決策を想定問答に足した（(h)）／
-- override＝契約者が上書きを変えた。どれも「今の版から、変えたところだけ違う新しい版を作る」1か所（business_settings.derive）が
-- 新しい版を作り、ここにつながりを書く（足していくだけ。1つの版の前の版は1つだけ）。そろった後の版を実施回に割り当てるのは
-- business_settings.adopt_ready の1か所（決まった間隔の仕事。記録は run_versions に「自動」の理由で残る）。

CREATE TABLE salescall.version_successions (
  tenant_id    uuid NOT NULL,
  client_id    uuid NOT NULL,
  seq          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  business_id  uuid NOT NULL,
  from_version uuid NOT NULL UNIQUE,                  -- 1つの版の後の版も1つだけ（つながりは1本の列。derive はいつも列の先から作る）
  to_version   uuid NOT NULL UNIQUE,                  -- 1つの版の前の版は1つだけ
  cause        text NOT NULL CHECK (cause IN ('standard', 'solution', 'override')),
  note         text NOT NULL CHECK (btrim(note) <> ''),
  recorded_by  text NOT NULL CHECK (btrim(recorded_by) <> ''),
  recorded_at  timestamptz NOT NULL DEFAULT now(),
  CHECK (from_version <> to_version),
  FOREIGN KEY (tenant_id, client_id) REFERENCES salescall.clients (tenant_id, client_id),
  FOREIGN KEY (client_id, business_id, from_version) REFERENCES salescall.business_versions (client_id, business_id, version_id),
  FOREIGN KEY (client_id, business_id, to_version) REFERENCES salescall.business_versions (client_id, business_id, version_id)
);
CREATE TRIGGER version_successions_frozen BEFORE UPDATE OR DELETE ON salescall.version_successions
  FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change();
ALTER TABLE salescall.version_successions ENABLE ROW LEVEL SECURITY;
CREATE POLICY client_scope ON salescall.version_successions
  USING (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client())
  WITH CHECK (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client());
GRANT SELECT, INSERT ON salescall.version_successions TO salescall_app;
