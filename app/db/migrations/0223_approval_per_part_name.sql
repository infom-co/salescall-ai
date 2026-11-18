-- 0223 録音の承認を、部分の名前ごとにする（2026-11-15。書いた人：CL-2。DESIGN.md 3-8-3・15-2 の813）
--
-- ✅ 2026-11-15 ユーザー「部分の名前ごとにする（お勧め）」：承認は録音の中身に付く（0172）が、部分の名前を見なかったので、言い方だけ違う同じ文の部分
-- （共通の一言の talk_heard_glad と talk_heard_sad・wait と wait_heavy など）で、録音の道具が音声を取り違えると、聞かずに承認済みになり、悲しい話に明るい
-- 相づちが流れえた。中身の鍵（content_of の1か所）に部分の名前を入れる＝同じ音声でも、部分の名前が違えば聞いて承認する。部分の名前の同じ録り直し・
-- 使い回し（業務の録音の complete・共通の一言の complete_common・名前の録音の台帳）は今までどおり聞かずにそろう。部分の名前が変わったときだけ聞き直しが
-- 増える（0172 の「同じ音を別の質問の番号で流しても、聞いた音は同じ」は外す）。
-- 前に承認した中身は、はじめに承認した組（approved_contents.set_id）の、その中身の部分の名前で、新しい鍵に書き写す（その組で聞いた部分＝承認の画面は
-- 承認していない部分をすべて聞かせた。ほかの組で聞かずにそろった名前の違う部分は、写さない＝聞いていただく）。

CREATE OR REPLACE FUNCTION salescall.content_of(p_set uuid, p_part text) RETURNS text LANGUAGE sql STABLE AS $$
  SELECT md5(r.part || chr(31) || r.spoken_text || chr(31) || r.audio_sha256 || chr(31) ||
             coalesce((SELECT string_agg(x.name_text || '=' || x.reading, '|' ORDER BY x.name_text)
                       FROM salescall.recording_readings x WHERE x.set_id = r.set_id AND x.part = r.part), ''))
  FROM salescall.recordings r WHERE r.set_id = p_set AND r.part = p_part
$$;

CREATE TEMP TABLE content_keys AS
  SELECT k.set_id, k.part, k.content AS old_content, salescall.content_of(k.set_id, k.part) AS new_content
  FROM salescall.part_contents k;

INSERT INTO salescall.approved_contents (tenant_id, client_id, voice_name, content, set_id, approved_at)
SELECT a.tenant_id, a.client_id, a.voice_name, m.new_content, a.set_id, a.approved_at
FROM salescall.approved_contents a JOIN content_keys m ON m.set_id = a.set_id AND m.old_content = a.content
ON CONFLICT DO NOTHING;

DELETE FROM salescall.approved_contents a
WHERE a.content IN (SELECT old_content FROM content_keys) AND a.content NOT IN (SELECT new_content FROM content_keys);

UPDATE salescall.part_contents k SET content = m.new_content
FROM content_keys m WHERE m.set_id = k.set_id AND m.part = k.part;

DROP TABLE content_keys;
