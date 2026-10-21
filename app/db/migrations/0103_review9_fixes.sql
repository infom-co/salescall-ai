-- 0103 見直し9（2026-10-19。ユーザー：「他にも、ここまでの実装とmainに入れる内容で、あなたの想定との食い違いや、不備、修正漏れなどは
-- 起こっていないですか？」で見つけた不備を直す。DESIGN.md 15-2 の219〜。書いた人：CL-2）

-- (1) 0102 の「最初の契約者を作った時刻」は、データベース全体の控えの見張り（管理者の PC・全体の取引）のためのもの。契約者の取引から
--     呼ぶと、ほかの契約者がいつ始めたかが分かってしまっていた。全体の取引（契約者を名乗っていない）からだけ値を返す。
CREATE OR REPLACE FUNCTION salescall.first_tenant_at() RETURNS timestamptz
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = salescall, pg_temp AS $$
  SELECT min(created_at) FROM salescall.tenants WHERE salescall.cur_tenant() IS NULL
$$;

-- (2) 録音の部品の名前は、質問の録音では質問の番号（業務設定は「英数字・「_」「-」の 32 文字まで」）。部品の名前が「-」を
--     受け付けず、「-」のある番号の質問の録音を入れられなかった。質問の番号と同じ文字を受け付ける。
ALTER TABLE salescall.recordings DROP CONSTRAINT recordings_part_check;
ALTER TABLE salescall.recordings ADD CONSTRAINT recordings_part_check CHECK (part ~ '^[A-Za-z0-9_-]{1,64}$');
