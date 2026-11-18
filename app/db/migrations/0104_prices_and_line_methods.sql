-- 0104 単価をダッシュボードで変えて保存する形と、回線の方式を一覧から選ぶ形（2026-10-21 ユーザー：意味の判定の単価は「決まれば
-- 入れてください。ダッシュボードでも変更や保存ができるようにしておいてください」・回線の方式は「楽天リンクは楽天リンクでいいんじゃ
-- ないんですか？」）。DESIGN.md 15-2 の225・226。書いた人：CL-2

-- (1) 利用量の台帳に、使ったサービスと枠（無料・有料）を残す。前は単価の版（price_id）からしか分からず、単価がまだ無いとき
--     （量だけ記録した行）は、どのサービス・どの枠の分かが残らなかった（あとで単価を入れても、何の費用か分からない）。
ALTER TABLE salescall.usage_entries ADD COLUMN service text, ADD COLUMN tier text;
ALTER TABLE salescall.usage_entries DISABLE TRIGGER usage_entries_frozen;
UPDATE salescall.usage_entries u SET service = p.service, tier = p.tier FROM salescall.price_versions p WHERE p.price_id = u.price_id;
ALTER TABLE salescall.usage_entries ENABLE TRIGGER usage_entries_frozen;
ALTER TABLE salescall.usage_entries
  ADD CONSTRAINT usage_entries_service CHECK (service IS NOT NULL AND btrim(service) <> '') NOT VALID,
  ADD CONSTRAINT usage_entries_tier CHECK ((source IN ('ai', 'ear', 'judge')) = (tier IS NOT NULL)) NOT VALID;

-- (2) 回線の方式の一覧（すべての契約者に共通。足すのは管理者だけ）。回線を作るときも、回線の単価を入れるときも、この一覧から選ぶ。
--     前は方式を文字で書いていたので、書き方が少し違うだけ（「楽天link」など）で単価が見つからず、費用が空になった。
CREATE TABLE salescall.line_methods (
  method   text PRIMARY KEY CHECK (method ~ '^[a-z][a-z0-9_]{0,31}$'),   -- 中の名前（単価の鍵）
  label    text NOT NULL UNIQUE CHECK (btrim(label) <> ''),              -- 画面に出す名前
  added_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO salescall.line_methods (method, label) VALUES ('rakuten_link', '楽天リンク');
CREATE FUNCTION salescall.line_methods_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF current_user <> 'salescall_owner' AND salescall.cur_actor_kind() IS DISTINCT FROM 'admin' THEN
    RAISE EXCEPTION '回線の方式の一覧に足せるのは管理者だけです' USING ERRCODE = 'insufficient_privilege';
  END IF;
  IF TG_OP <> 'INSERT' THEN
    RAISE EXCEPTION '回線の方式は変えられません・消せません（回線と単価が指しています）' USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER line_methods_guard BEFORE INSERT OR UPDATE OR DELETE ON salescall.line_methods
  FOR EACH ROW EXECUTE FUNCTION salescall.line_methods_guard();
GRANT SELECT, INSERT ON salescall.line_methods TO salescall_app;
ALTER TABLE salescall.lines ADD CONSTRAINT lines_method FOREIGN KEY (method) REFERENCES salescall.line_methods (method);

CREATE FUNCTION salescall.line_price_method() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.source = 'line' AND NOT EXISTS (SELECT 1 FROM salescall.line_methods WHERE method = NEW.service) THEN
    RAISE EXCEPTION '回線の単価は、回線の方式の一覧にある方式に入れます（%）', NEW.service USING ERRCODE = 'foreign_key_violation';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER price_versions_line_method BEFORE INSERT ON salescall.price_versions
  FOR EACH ROW EXECUTE FUNCTION salescall.line_price_method();
