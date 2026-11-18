-- データベースの役（ロール）を作る。データベースの管理者が、最初に1回だけ流す（表の作りより前）。何度流しても同じ結果になる
-- （役の決まりを直したときは、もう一度流す）。
-- salescall_owner：表の持ち主。表の作り（migrations）はこの役で流す。システムの処理はこの役でつながない。
-- salescall_app　：システムの処理がつなぐ役。行ごとに読める人を制限する機能（RLS）を外せない（DESIGN.md 11章）。
--                   消す権限は持たない（変えてはいけない記録を守る。DESIGN.md 3-0 の3）。サーバー（中央の見守り役・画面の機械）がつなぐ。
-- salescall_login_maker・salescall_provisioner：前の「PC ごとのデータベースの鍵」の仕組み（表の作り 0077・0079・0084）の役。
--                   2026-10-21 から PC はデータベースにつながない（DESIGN.md 5-5）ので、表の作り 0107 が、この2つの役の関数と
--                   権限を外した。前の表の作りを流すために作り続けるだけ（**どちらもログインできない**）。鍵を作る役は、役を作る権限と
--                   システムの役の管理権を持ち続けるが、なれるのは表の持ち主だけ（表の作りの中だけ）。
--                   0107 は、残っている PC の鍵の役（salescall_t_…）のうち消せるものを消す。消せないもの（0084 より前に払い出し専用の
--                   役が作ったもの）が残っても、データベースが受け付けるのはサーバーの役と表の持ち主だけなので（0107 の
--                   salescall.server_session）、つないでも何もできない。人が手で消す手順は要らない（このファイルを流し直したときは、
--                   下で一緒に消す）。
-- ⚠️ 合言葉はここに書かない（git に入れない）。作ったあとに、管理者が ALTER ROLE ... PASSWORD で付ける。
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'salescall_owner') THEN
    CREATE ROLE salescall_owner LOGIN NOSUPERUSER NOBYPASSRLS;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'salescall_app') THEN
    CREATE ROLE salescall_app LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'salescall_login_maker') THEN
    CREATE ROLE salescall_login_maker NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB CREATEROLE;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'salescall_provisioner') THEN
    CREATE ROLE salescall_provisioner NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
  END IF;
END $$;
-- 前の決まり（払い出し専用の役が、ログインでき、役を作る権限とシステムの役の管理権を持っていた）を外す
ALTER ROLE salescall_provisioner NOLOGIN NOCREATEROLE;
REVOKE salescall_app FROM salescall_provisioner CASCADE;   -- 前に払い出し専用の役が作った PC の鍵の役の、システムの役の権限も一緒に外す
DO $$
DECLARE r record;
BEGIN   -- 前の PC の鍵の役（管理者が流すので消せる。つないだままの接続も切る）
  FOR r IN SELECT rolname FROM pg_roles WHERE rolname LIKE 'salescall\_t\_%' LOOP
    PERFORM pg_terminate_backend(a.pid) FROM pg_stat_activity a WHERE a.usename = r.rolname;
    EXECUTE format('DROP ROLE %I', r.rolname);
  END LOOP;
END $$;
-- 前の表の作り（0084・0101）を流すための権限（0107 が、残っている PC の鍵の役を消すときにも使う）
GRANT salescall_app TO salescall_login_maker WITH ADMIN TRUE, INHERIT FALSE, SET FALSE;
GRANT pg_signal_backend TO salescall_login_maker;
-- 表の持ち主が、表の作りの中で、2つの関数の持ち主を作る役にできるように（持ち主の役は、作る役の権限を受け継がない）
GRANT salescall_login_maker TO salescall_owner WITH INHERIT FALSE, SET TRUE;
