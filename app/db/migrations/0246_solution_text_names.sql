-- 0246 依頼者の解決策の文の名前と読みを、文ごとに1回だけ AI に聞いて残す（2026-11-19。書いた人：CL-2。DESIGN.md 3-7・15-2 の879。
--      CL-3 の連絡帳 4-1b の 22）
--
-- ⛔ 前の作り：解決策を足すとき（core/client_solutions._add_line）、流れ（版のつながり）ごとに、足す文の名前を AI に聞き直していた。
--    (1) 同じ文でも、流れごとに名前の読みが変わりえた（AI の答えは聞くたびに変わりうる＝取込の「プレビューで聞いた答えを登録でもそのまま
--        使う」と同じ理由で、聞くのは1回にする）。
--    (2) 契約者が、前の解決策を流れに「入れる」（add_earlier）ときも、また AI に聞く形だった。画面からは名前を洗い出す部品を渡せないので、
--        渡さずに呼ぶと、名前の読みを確かめないまま台本に入った（readings.find が黙って空を返した。CL-3 の 22）。
-- ⇒ 文ごとの名前の洗い出しの答え（solution_text_names）を、その依頼者の中で、文ごとに1つ残す（1か所。書くのは core/client_solutions の
--    `_text_names` だけ）。解決策を足す・前の解決策を入れる、のどれも、残した答えを読む（無いときだけ AI に1回聞いて残す）。名前を洗い出す
--    部品が無く、残した答えも無ければ、黙って足さずに断る（原則10「わからない」を値で埋めない）。

CREATE TABLE salescall.solution_text_names (
  tenant_id    uuid NOT NULL,
  client_id    uuid NOT NULL,
  text         text NOT NULL CHECK (btrim(text) <> ''),             -- 足す文（解決策の文、または置き換えた取り込みの答え）
  names        jsonb NOT NULL CHECK (jsonb_typeof(names) = 'object'),   -- AI が洗い出した名前と読み（core/readings.to_json の形）
  recorded_by  text NOT NULL CHECK (btrim(recorded_by) <> ''),
  recorded_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (client_id, text),
  FOREIGN KEY (tenant_id, client_id) REFERENCES salescall.clients (tenant_id, client_id)
);
CREATE TRIGGER stamp_now BEFORE INSERT ON salescall.solution_text_names FOR EACH ROW EXECUTE FUNCTION salescall.stamp_now('recorded_at');
CREATE TRIGGER solution_text_names_frozen BEFORE UPDATE OR DELETE ON salescall.solution_text_names
  FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change();
ALTER TABLE salescall.solution_text_names ENABLE ROW LEVEL SECURITY;
CREATE POLICY client_scope ON salescall.solution_text_names
  USING (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client())
  WITH CHECK (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client());
GRANT SELECT, INSERT ON salescall.solution_text_names TO salescall_app;
