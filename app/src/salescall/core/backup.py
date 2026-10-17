"""データベースの控え（Intent.md 14-1。表の作り 0052）。持ち主 CL-2。

Intent.md 14-1：「毎日自動で取り、Fly.io とは別の場所にも置き、戻せることを試験で確かめ、取れなかったら知らせる（発信禁止の台帳を
失うと、断ったお客様にまたかけるため）」。CL-1 の条件：控えも暗号化する。
- 取る（`take`）：ある時点の見え方を固定し（`access.snapshot`）、その時点の表ごとの行の数を数え、同じ時点を pg_dump で書き出す。
  書き出したものは暗号化する（AES-GCM。鍵は呼ぶ側が渡す＝鍵の値をこの部品は読まない・残さない・出さない）。
- 置く：置き場（`Destination`）2か所以上に置く（Fly.io の中と、Fly.io とは別の場所。⏳ 別の場所をどこにするかはユーザーに確かめる）。
- 戻せるかを確かめる：置いた控えのうち、Fly.io とは別の場所のものを読み戻し、指紋を比べ、復号して、確かめ用の空のデータベースに
  pg_restore で戻し、表ごとの行の数が取ったときと同じかを比べる。
- 記録（backup_runs）：うまくいっても、いかなくても1回ずつ残す。いかなければ、止めずに CL-2 へ修正依頼を出し、管理者に知らせる
  （2026-10-17 ユーザー：「止めずに CL1 か CL2 に修正依頼を出さないといけないだろ。人はそのプロセスをチェックもしくは承認するだけ」）。
- 毎日1回（`due`）：今日（日本時間）の、戻せることまで確かめた控えが無ければ取る。呼ぶのは見守り役（常駐の処理）。
- 記録・知らせ・修正依頼には、お客様の情報・鍵・接続文字列を入れない（行の数と指紋と、エラーの種類だけ）。
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from salescall.core import fix_requests, notify
from salescall.core.access import Access, Actor, Tx, snapshot

MAGIC = b"SALESCALL-BACKUP-1\n"    # 控えのファイルの頭（形が変わったら番号を上げる）
SCHEMAS = ("salescall", "public")   # 数える表の置き場（public には表の作りの記録がある）
SYSTEM = Actor("データベースの控え", "system")


class Destination(Protocol):
    """控えの置き場。name は記録に残す置き場の名前（場所の中身・鍵は入れない）。offsite＝Fly.io とは別の場所か。"""
    name: str
    offsite: bool

    def put(self, file_name: str, data: bytes) -> None: ...
    def get(self, file_name: str) -> bytes: ...


@dataclass(frozen=True)
class LocalDirectory:
    """手元の置き場（Fly.io の置き場の中のフォルダ・試験の一時フォルダ）。"""
    name: str
    path: Path
    offsite: bool = False

    def put(self, file_name: str, data: bytes) -> None:
        self.path.mkdir(parents=True, exist_ok=True)
        tmp = self.path / (file_name + ".part")
        tmp.write_bytes(data)
        tmp.replace(self.path / file_name)          # 書き終えてから名前を付ける（途中のファイルを控えと取り違えない）

    def get(self, file_name: str) -> bytes:
        return (self.path / file_name).read_bytes()


@dataclass(frozen=True)
class Result:
    ok: bool
    file_name: str | None
    error: str | None


class BackupFailed(Exception):
    """控えが取れなかった・置けなかった・戻せなかった（中身は、鍵・接続文字列・お客様の情報を含まない短い説明）。"""


def _counts(tx: Tx) -> dict[str, int]:
    tables = tx.all("SELECT schemaname || '.' || tablename AS t FROM pg_tables WHERE schemaname = ANY(%s) ORDER BY 1",
                    (list(SCHEMAS),))
    return {r["t"]: tx.one(f'SELECT count(*) AS n FROM {_quoted(r["t"])}')["n"] for r in tables}


def _quoted(qualified: str) -> str:
    schema, table = qualified.split(".", 1)
    return f'"{schema}"."{table}"'


def encrypt(data: bytes, key: bytes, file_name: str) -> bytes:
    """控えを暗号化する（AES-GCM。ファイルの名前も合わせて守る＝別のファイルと取り替えられたら復号できない）。"""
    nonce = os.urandom(12)
    return MAGIC + nonce + AESGCM(key).encrypt(nonce, data, file_name.encode())


def decrypt(blob: bytes, key: bytes, file_name: str) -> bytes:
    if not blob.startswith(MAGIC):
        raise BackupFailed("控えのファイルの形が違います")
    nonce, body = blob[len(MAGIC):len(MAGIC) + 12], blob[len(MAGIC) + 12:]
    try:
        return AESGCM(key).decrypt(nonce, body, file_name.encode())
    except Exception:
        raise BackupFailed("控えを復号できません（鍵が違う・ファイルが壊れている・取り替えられた）") from None


def _run(cmd: list[str], what: str) -> None:
    done = subprocess.run(cmd, capture_output=True, text=True)
    if done.returncode != 0:
        # 接続文字列（パスワードを含みうる）は出さない。エラーの最後の行だけ
        last = (done.stderr.strip().splitlines() or ["（説明なし）"])[-1][:300]
        raise BackupFailed(f"{what}に失敗しました：{last}")


def take(app: Access, source_conninfo: str, restore_conninfo: str, destinations: list[Destination], *, key: bytes,
         now: datetime | None = None) -> Result:
    """控えを1回取り、置き、戻せるかを確かめ、記録する。うまくいかなければ、修正依頼と知らせを出して Result(ok=False)。
    source_conninfo＝表の持ち主の接続（すべての行を読む）。restore_conninfo＝確かめ用の空のデータベース（毎回、中身を消して戻す）。"""
    started = datetime.now().astimezone() if now is None else now
    file_name = f"salescall-{started:%Y%m%d-%H%M%S}.dump.enc"
    counts: dict[str, int] | None = None
    blob: bytes | None = None
    placed: list[str] = []
    try:
        if len(key) != 32:
            raise BackupFailed("控えの鍵は32バイトです")
        if len(destinations) < 2 or not any(d.offsite for d in destinations):
            raise BackupFailed("置き場は2か所以上、そのうち1か所は Fly.io とは別の場所が要ります")
        with tempfile.TemporaryDirectory(prefix="salescall-backup-") as work:
            dump = Path(work) / "db.dump"
            with snapshot(source_conninfo) as (snap, tx):
                counts = _counts(tx)
                _run(["pg_dump", "--format=custom", f"--snapshot={snap}", "--no-owner", "--no-acl", "-f", str(dump),
                      "-d", source_conninfo], "書き出し（pg_dump）")
            blob = encrypt(dump.read_bytes(), key, file_name)
        for d in destinations:
            d.put(file_name, blob)
            placed.append(d.name)
        _verify(blob, file_name, [d for d in destinations if d.offsite][0], restore_conninfo, counts, key=key)
    except Exception as e:  # noqa: BLE001 ここで受けて記録する（止めない）
        error = str(e) if isinstance(e, BackupFailed) else f"思いがけない誤り（{type(e).__name__}）"
        _record(app, started, file_name if blob else None, blob, counts, placed, error)
        return Result(False, None, error)
    _record(app, started, file_name, blob, counts, placed, None)
    return Result(True, file_name, None)


def _verify(blob: bytes, file_name: str, offsite: Destination, restore_conninfo: str, counts: dict[str, int], *,
            key: bytes) -> None:
    """Fly.io とは別の場所から読み戻し、指紋を比べ、復号し、確かめ用のデータベースに戻して、行の数を比べる。"""
    back = offsite.get(file_name)
    if hashlib.sha256(back).hexdigest() != hashlib.sha256(blob).hexdigest():
        raise BackupFailed(f"置き場「{offsite.name}」から読み戻した控えが、置いたものと違います")
    with tempfile.TemporaryDirectory(prefix="salescall-restore-") as work:
        dump = Path(work) / "db.dump"
        dump.write_bytes(decrypt(back, key, file_name))
        with snapshot(restore_conninfo) as (_, tx):   # 確かめ用のデータベースの、いまある表を消す（前の確かめの残り）
            drops = [_quoted(f"{r['schemaname']}.{r['tablename']}") for r in tx.all(
                "SELECT schemaname, tablename FROM pg_tables WHERE schemaname = ANY(%s)", (list(SCHEMAS),))]
        _run(["psql", "-q", "-v", "ON_ERROR_STOP=1", "-d", restore_conninfo, "-c",
              "DROP SCHEMA IF EXISTS salescall CASCADE; " + "".join(f"DROP TABLE IF EXISTS {t} CASCADE; " for t in drops
                                                                    if t.startswith('"public"'))],
             "確かめ用のデータベースの片付け")
        _run(["pg_restore", "--no-owner", "--no-acl", "--exit-on-error", "-d", restore_conninfo, str(dump)],
             "戻す（pg_restore）")
    with snapshot(restore_conninfo) as (_, tx):
        got = _counts(tx)
    if got != counts:
        diff = sorted(t for t in set(got) | set(counts) if got.get(t) != counts.get(t))
        raise BackupFailed(f"戻した控えの行の数が、取ったときと違います（{len(diff)} 個の表。例：{', '.join(diff[:3])}）")


def _record(app: Access, started: datetime, file_name: str | None, blob: bytes | None, counts: dict[str, int] | None,
            placed: list[str], error: str | None) -> None:
    def work(tx: Tx) -> None:
        tx.execute("INSERT INTO salescall.backup_runs (started_at, file_name, sha256, size_bytes, table_counts, destinations,"
                   " restore_ok, error) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                   (started, file_name, None if blob is None else hashlib.sha256(blob).hexdigest(),
                    None if blob is None else len(blob), None if counts is None else json.dumps(counts, sort_keys=True), placed,
                    error is None, error))
        if error is not None:
            day = tx.one("SELECT salescall.jst(now())::date AS d")["d"]
            fix_requests.request(tx, "CL-2", "backup_failed", key=f"backup_failed:{day}", summary=error)
            notify.emit(tx, "backup_failed", key=f"backup_failed:{day}", summary="データベースの控えが取れませんでした"
                        "（CL-2 に修正依頼を出しました）。", refs={"error": error}, admin=True, contractor=False)
    app.in_system(SYSTEM, work)


def due(app: Access) -> bool:
    """今日（日本時間）の、戻せることまで確かめた控えが、まだ無いか（見守り役が毎日1回取るときに見る）。"""
    return app.in_system(SYSTEM, lambda tx: tx.one(
        "SELECT NOT EXISTS (SELECT 1 FROM salescall.backup_runs WHERE restore_ok"
        "                   AND salescall.jst(started_at)::date = salescall.jst(now())::date) AS due"))["due"]


def latest(app: Access) -> dict[str, Any] | None:
    """いちばん新しい記録（画面・見守り役が、控えが取れているかを見る）。"""
    return app.in_system(SYSTEM, lambda tx: tx.one(
        "SELECT started_at, file_name, size_bytes, destinations, restore_ok, error FROM salescall.backup_runs"
        " ORDER BY run_id DESC LIMIT 1"))
