-- 0261 次の予定の印 role_call：その役職の方をお呼びする（2026-11-20。書いた人：CL-2。DESIGN_15-2 の913。CL-1 の連絡帳 4-213・CL-2 の連絡帳 4-445）
--
-- ✅ 2026-11-20 ユーザー「伺い69：エで後任もいないときは、該当の役職の方」。お呼びした方が在籍しておらず、後任の方もいらっしゃらなかったとき、
-- 会話はその役職の方へ切り替える（事実 departed の to_role）。この行のこれからの電話は、はじめから役職の方をお呼びする（後任の方の口 successor＝0114 と同じ考え）。
-- 印を立てるのは next_action.choose_number の1か所。後任の方の口と両方は立てない（役職の方の口は後任の方もいらっしゃらなかった口＝より先の段）。
ALTER TABLE salescall.task_next_actions ADD COLUMN role_call boolean NOT NULL DEFAULT false;
ALTER TABLE salescall.task_next_actions
  ADD CONSTRAINT task_next_actions_role_calls CHECK (NOT role_call OR (kind = 'call' AND NOT successor));
-- 列は表の終わりに足すので、いまの予定の見方は同じ形のまま作り直せる（0219 と同じ）
CREATE OR REPLACE VIEW salescall.current_next_actions WITH (security_invoker = true) AS
  SELECT n.* FROM salescall.task_next_actions n
  WHERE n.seq = (SELECT max(m.seq) FROM salescall.task_next_actions m WHERE m.task_id = n.task_id);
