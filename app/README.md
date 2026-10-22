# app — SalesCall AI の本番のコード

持ち場は `docs/TEAM_PLAN.md` 1-4。設計は `Intent.md` と `docs/DESIGN.md`。

| 場所 | 中身 | 持ち主 |
|---|---|---|
| `db/setup_roles.sql` | データベースの役（表の持ち主・システムの処理）を作る。管理者が最初に1回流す | CL-2 |
| `db/migrations/` | 表の作り（番号順に1回ずつ流す。流したものは書き換えない） | CL-2 |
| `src/salescall/core/access.py` | 読み書きの層。データベースへの読み書きは、すべてここを通す（DESIGN.md 11章） | CL-2 |
| `src/salescall/core/migrate.py` | 表の作りを流す処理 | CL-2 |
| `src/salescall/supervisor/` | 中央の見守り役とつなぎ口（サーバーで動く。DESIGN.md 5-4・5-5） | CL-2 |
| `src/salescall/agent/` | PC の部品（契約者の PC で動く。データベースにつながない。手順は `docs/PC_SETUP.md`） | CL-2（電話の部品は電話接続の担当） |
| `src/salescall/link/` | サーバーと PC の両方が読む決まり（やり取りの形・電話の部品の形・控えの暗号化・1つだけの印） | CL-2 |
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
1. 管理者で `db/setup_roles.sql` を流し、表の持ち主（`salescall_owner`）とシステムの処理（`salescall_app`）の2つの役に合言葉を付ける（合言葉は git に入れない。ほかの2つの役は、前の表の作りを流すためだけの、ログインできない役）。
2. `CREATE DATABASE salescall OWNER salescall_owner;`
3. 環境変数 `SALESCALL_OWNER_DSN` に表の持ち主の接続文字列を入れて、`python -m salescall.core.migrate`。管理者の接続文字列も `SALESCALL_ADMIN_DSN` に入れておくと、表の作りの前に 1. の役の準備も流す（何度流しても同じ結果。前の PC の鍵の役が残っていれば消す。新しい版を入れるときも、この1つの命令でよい）。動いているサーバーは止めなくてよい（表を変える取引は、待つのを5秒までにして、待てなければ間をあけて流し直す）。
4. システムの処理は `salescall_app` の役でつなぐ（行ごとの制限を外せない役）。

## 中央の見守り役を動かす（サーバー。DESIGN.md 5-4・5-5）
サーバーができるまでは、管理者の PC で動かす（PC の部品も同じ PC で動かす。PC の部品の設定 `SALESCALL_HUB_URL` に `ws://127.0.0.1:<SALESCALL_LISTEN の番号>/link` を入れる。暗号化しない `ws://` は、同じ PC の中だけ使える。このときは `SALESCALL_LISTEN` を `127.0.0.1:<番号>`＝この PC の中だけにする。そうすると、中央の見守り役が、この PC の PC の部品の通行証を自分で払い出して入れる＝管理者は命令を打たない・値は出さない。契約者は `SALESCALL_ADMIN_TENANT` の契約者。ブラウザでのログインの入口は CL-3 が作るまで無い）。
1. 環境変数を入れる（値は git に入れない。`python -m salescall.supervisor status` で「入っているか」だけを確かめられる）。
   | 名前 | 中身 | 要るとき |
   |---|---|---|
   | `SALESCALL_DSN` | データベースへの接続（`salescall_app` の役） | いつも |
   | `SALESCALL_LISTEN` | PC の部品からのつなぎを待つ場所（例 `0.0.0.0:8765`。外からは、サーバーの入口が暗号化して `/link` で受ける＝CL-3。移る間の形は `127.0.0.1:8765`） | いつも（無ければ動かない） |
   | `SALESCALL_CALL_RUNNER` | 通話を進める部品（`モジュール:名前`。CL-1。回線の ID と、その回線の電話の部品を受け取る） | 電話をかけるとき |
   | `SALESCALL_VERIFIER` | 通話後の検証の部品（CL-1） | 電話をかけるとき（`SALESCALL_CALL_RUNNER` を入れたら必ず。無いと、かけた電話を締められず、行もお客様も待ち続けるので、動かない） |
   | `SALESCALL_PROBER` | 停止を解く確かめの部品（CL-1・電話接続） | 確かめをするとき |
   | `SALESCALL_OWNER_DSN`・`SALESCALL_RESTORE_DSN` | 表の持ち主の接続・戻せるかを確かめる空のデータベース（本番と別） | データベース全体の控え |
   | `SALESCALL_ADMIN_TENANT` | 管理者自身の契約者の番号（データベース全体の控えを書く PC の契約者＝すべての契約者のデータが入るので、この契約者の PC にだけ送る。移る間の形では、この PC の PC の部品の契約者） | データベース全体の控え・移る間の形 |
2. `python -m salescall.supervisor run`（1つの機械に1つだけ。止めるのは Ctrl+C＝いまの通話を最後まで終えてから、回線を返して止まる）。
   - **人が立ち上げ直さない**：思いがけない誤り・データベースやつなぎ口の一時的な不具合では終わらず、間をあけて自分で動き直す（設定の抜け・二重に動かしたときだけ終わる＝直すまで動けないため）。処理そのものが落ちたときに備えて、動かす所の仕組みでも立ち上げ直す：移る間の管理者の PC は Windows の「タスク スケジューラ」でログオン時に動かし、失敗したら再起動する設定にする（`docs/PC_SETUP.md` 5章の7）。サーバー（Fly.io）では、落ちたら立ち上げ直す設定（restart policy）で動かす。
- 会話AI・判定のAIのキーは、中央の見守り役が動く所に置く（読むのは CL-1 のキーの選択の処理だけ。⏳ 契約者のキーをサーバーで預かる形は CL-1 と決める）。控えの鍵は置かない（契約者の PC と Proton Pass だけ）。
