-- 表の作りを全部流したあとに、データベースの管理者が流す（何度流しても同じ結果）。書いた人：CL-2（2026-10-22 の見直し）
--
-- 前の表の作り（0077・0079・0084・0101・0107）を流すあいだだけ、鍵を作る役（salescall_login_maker）に、役を作る権限・システムの役の
-- 管理権・接続を切る権限を持たせている（db/setup_roles.sql）。流し終えたら外す。⛔ 外していなかったので、表の持ち主はいつでもこの役に
-- なれ、システムの役を持つ、ログインできる役を作れた（0107 の「何の権限も持たない」と食い違っていた）。
-- 次に表の作りを流すときは、db/setup_roles.sql がまた持たせ、流し終えたら、ここがまた外す（python -m salescall.core.migrate の1つの命令）。
REVOKE salescall_login_maker FROM salescall_owner;
REVOKE salescall_app FROM salescall_login_maker CASCADE;
REVOKE pg_signal_backend FROM salescall_login_maker;
ALTER ROLE salescall_login_maker NOLOGIN NOCREATEROLE;
