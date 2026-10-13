-- 0002 「わからない」の状態と、自由な意見の型（2026-10-13。CL-1 の連絡帳 4-3・DESIGN.md 4-4）
-- 書いた人：CL-2。
-- - 項目の状態に dont_know（相手が「わからない」と答えた。「取れた」には入らない）を足す。状態は9つ。
--   ユーザー決定：必須の質問が「わからない」なら、業務は完了ではない（DESIGN.md 7-1）。
-- - 型の一覧に free_text（自由な意見。確かめない型）を足す。名前は CL-1 が決めた（Q19 の持ち主）。
ALTER TYPE salescall.item_state ADD VALUE 'dont_know' AFTER 'accepted';
INSERT INTO salescall.field_types (type_name, readback) VALUES ('free_text', false);
