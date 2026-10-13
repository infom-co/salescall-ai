"""表の作り（app/db/migrations）を、順に1回ずつ流す。持ち主 CL-2。

- 表の持ち主の役（salescall_owner）でつないで流す。システムの処理の役（salescall_app）では流さない。
- 一度流した表の作りの中身が書き換わっていたら、止める（流した中身の指紋を記録して比べる）。
  変えるときは、新しい番号のファイルを足す。
- ⚠️ 本番のデータベースへ初めて流すまでは、表の作りのファイルを直してよい（試験のデータベースは毎回作り直すため）。
  初めて流したあとは、足すことしかしない（2026-10-13 の時点で、本番のデータベースへはまだ流していない）。
- 1つのファイルは1つの取引で流す（途中で失敗したら、そのファイルは何も入らない）。
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import psycopg

MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "db" / "migrations"


class MigrationChanged(Exception):
    """一度流した表の作りの中身が、書き換えられている。"""


def migration_files(directory: Path = MIGRATIONS_DIR) -> list[Path]:
    return sorted(directory.glob("[0-9][0-9][0-9][0-9]_*.sql"))


def apply(owner_conninfo: str, directory: Path = MIGRATIONS_DIR) -> list[str]:
    """まだ流していない表の作りを流し、流したファイルの名前を返す。"""
    done: list[str] = []
    with psycopg.connect(owner_conninfo, autocommit=True) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS public.salescall_migrations ("
            " name text PRIMARY KEY, sha256 text NOT NULL, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        applied = dict(conn.execute("SELECT name, sha256 FROM public.salescall_migrations").fetchall())
        for path in migration_files(directory):
            body = path.read_bytes()
            digest = hashlib.sha256(body).hexdigest()
            if path.name in applied:
                if applied[path.name] != digest:
                    raise MigrationChanged(f"{path.name} は流したあとに書き換えられています（新しい番号のファイルを足してください）")
                continue
            with conn.transaction():
                conn.execute(body.decode("utf-8"))
                conn.execute("INSERT INTO public.salescall_migrations (name, sha256) VALUES (%s, %s)", (path.name, digest))
            done.append(path.name)
    return done


if __name__ == "__main__":
    # 接続文字列（合言葉を含みうる）は、命令の引数に書かず、環境変数で渡す（ほかの人から見える場所に出さない）
    dsn = os.environ.get("SALESCALL_OWNER_DSN")
    if not dsn:
        print("環境変数 SALESCALL_OWNER_DSN に、表の持ち主の役でつなぐ接続文字列を入れてください")
        raise SystemExit(2)
    for name in apply(dsn):
        print("流した：", name)
