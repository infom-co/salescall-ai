-- データベースの役（ロール）を作る。データベースの管理者が、最初に1回だけ流す（表の作りより前）。
-- salescall_owner：表の持ち主。表の作り（migrations）はこの役で流す。システムの処理はこの役でつながない。
-- salescall_app　：システムの処理がつなぐ役。行ごとに読める人を制限する機能（RLS）を外せない（DESIGN.md 11章）。
--                   消す権限は持たない（変えてはいけない記録を守る。DESIGN.md 3-0 の3）。管理者の PC と画面の機械がつなぐ。
-- salescall_provisioner：契約者ごとの合言葉（salescall_t_<契約者の番号>。システムの役の権限を受け継ぐ）を作る・替える・使えなくする
--                   だけの役（core/logins.py。アカウントを作ったとき・入金が済んだときに払い出す＝2026-10-18 ユーザー）。表は読めない・
--                   システムの役としても動けない（受け継がない・なれない）。契約者の PC には置かない。
-- ⚠️ 合言葉はここに書かない（git に入れない）。作ったあとに、管理者が ALTER ROLE ... PASSWORD で付ける。
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'salescall_owner') THEN
    CREATE ROLE salescall_owner LOGIN NOSUPERUSER NOBYPASSRLS;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'salescall_app') THEN
    CREATE ROLE salescall_app LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'salescall_provisioner') THEN
    CREATE ROLE salescall_provisioner LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB CREATEROLE;
  END IF;
END $$;
-- 契約者の合言葉を、システムの役の権限を受け継ぐ形で作れるように（払い出し専用の役自身は、受け継がない・なれない）
GRANT salescall_app TO salescall_provisioner WITH ADMIN TRUE, INHERIT FALSE, SET FALSE;
