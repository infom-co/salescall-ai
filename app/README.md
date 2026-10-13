# app — SalesCall AI の本番のコード

持ち場は `docs/TEAM_PLAN.md` 1-4。設計は `Intent.md` と `docs/DESIGN.md`。

| 場所 | 中身 | 持ち主 |
|---|---|---|
| `db/setup_roles.sql` | データベースの役（表の持ち主・システムの処理）を作る。管理者が最初に1回流す | CL-2 |
| `db/migrations/` | 表の作り（番号順に1回ずつ流す。流したものは書き換えない） | CL-2 |
| `src/salescall/core/access.py` | 読み書きの層。データベースへの読み書きは、すべてここを通す（DESIGN.md 11章） | CL-2 |
| `src/salescall/core/migrate.py` | 表の作りを流す処理 | CL-2 |
| `checks/` | 決まりの機械の見張り（DESIGN.md 14章） | CL-2 |
| `tests/` | 試験（架空のデータだけ） | それぞれの持ち場 |
| `pyproject.toml` | 使う部品とその版・Python の版（共通のファイル。足したいときは連絡帳で CL-2 に頼む） | CL-2 |

## 試験を動かす（1つの命令で全部）
```
python -m venv app/.venv
app/.venv/bin/pip install -e app        # Windows は app\.venv\Scripts\pip
app/.venv/bin/python app/run_tests.py
```
1. 決まりの機械の見張り → 2. 試験用のデータベースを立てて表の作りを流す → 3. 試験を全部 → 4. **守りをわざと1つずつ外して、その試験が本当に落ちるか**
- PostgreSQL の部品（initdb・pg_ctl）があれば、使い捨てのデータベースを自分で立てる。無ければ、環境変数 `SALESCALL_TEST_SUPERUSER_DSN` に管理者の接続文字列を入れる。

## データベースの用意（本番・Fly.io）
1. 管理者で `db/setup_roles.sql` を流し、2つの役に合言葉を付ける（合言葉は git に入れない）。
2. `CREATE DATABASE salescall OWNER salescall_owner;`
3. 環境変数 `SALESCALL_OWNER_DSN` に表の持ち主の接続文字列を入れて、`python -m salescall.core.migrate`。
4. システムの処理は `salescall_app` の役でつなぐ（行ごとの制限を外せない役）。
