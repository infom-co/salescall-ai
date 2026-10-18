-- データベースの役（ロール）を作る。データベースの管理者が、最初に1回だけ流す（表の作りより前）。何度流しても同じ結果になる
-- （役の決まりを直したときは、もう一度流す）。
-- salescall_owner：表の持ち主。表の作り（migrations）はこの役で流す。システムの処理はこの役でつながない。
-- salescall_app　：システムの処理がつなぐ役。行ごとに読める人を制限する機能（RLS）を外せない（DESIGN.md 11章）。
--                   消す権限は持たない（変えてはいけない記録を守る。DESIGN.md 3-0 の3）。管理者の PC と画面の機械がつなぐ。
-- salescall_login_maker：契約者の PC の鍵（salescall_t_<契約者の番号>_<PC の番号>。システムの役の権限を受け継ぐ）を作る・替える・
--                   止める権限を持つ役。**ログインできない**。この権限は、表の作り 0084 の2つの関数（salescall.make_pc_login・
--                   salescall.stop_pc_logins）の中でだけ使う（名前が PC の鍵の形・合言葉が確かめの値の形のときだけ。止めたら接続も切る）。
--                   2026-10-18 の見直し：前は払い出し専用の役がこの権限を直接持ち、システムの役の合言葉を替える・名前を自由に付けた
--                   システムの役を作る・自分をシステムの役にする、ができた。
-- salescall_provisioner：払い出し専用の役（core/logins.py。PC がログインしたとき、サーバーが使う）。上の2つの関数を呼べるだけ
--                   （役を作る権限も、システムの役としての権限も持たない）。契約者の PC には置かない。
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
    CREATE ROLE salescall_provisioner LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
  END IF;
END $$;
-- 前の決まり（払い出し専用の役が、役を作る権限とシステムの役の管理権を持っていた）を外す
ALTER ROLE salescall_provisioner NOCREATEROLE;
REVOKE salescall_app FROM salescall_provisioner;
-- 契約者の PC の鍵を、システムの役の権限を受け継ぐ形で作れるように（作る役自身は、受け継がない・なれない）。止めたら接続も切れるように
GRANT salescall_app TO salescall_login_maker WITH ADMIN TRUE, INHERIT FALSE, SET FALSE;
GRANT pg_signal_backend TO salescall_login_maker;
-- 表の持ち主が、表の作りの中で、2つの関数の持ち主を作る役にできるように（持ち主の役は、作る役の権限を受け継がない）
GRANT salescall_login_maker TO salescall_owner WITH INHERIT FALSE, SET TRUE;
