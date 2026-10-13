-- データベースの役（ロール）を作る。データベースの管理者が、最初に1回だけ流す（表の作りより前）。
-- salescall_owner：表の持ち主。表の作り（migrations）はこの役で流す。システムの処理はこの役でつながない。
-- salescall_app　：システムの処理がつなぐ役。行ごとに読める人を制限する機能（RLS）を外せない（DESIGN.md 11章）。
--                   消す権限は持たない（変えてはいけない記録を守る。DESIGN.md 3-0 の3）。
-- ⚠️ 合言葉はここに書かない（git に入れない）。作ったあとに、管理者が ALTER ROLE ... PASSWORD で付ける。
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'salescall_owner') THEN
    CREATE ROLE salescall_owner LOGIN NOSUPERUSER NOBYPASSRLS;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'salescall_app') THEN
    CREATE ROLE salescall_app LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
  END IF;
END $$;
