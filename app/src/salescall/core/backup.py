"""データベースの控え（Intent.md 14-1。表の作り 0052）。持ち主 CL-2。

Intent.md 14-1：「毎日自動で取り、Fly.io とは別の場所にも置き、戻せることを試験で確かめ、取れなかったら知らせる（発信禁止の台帳を
失うと、断ったお客様にまたかけるため）」。CL-1 の条件：控えも暗号化する。
- 取る（`take`）：ある時点の見え方を固定し（`access.snapshot`）、その時点の表ごとの行の数を数え、同じ時点を pg_dump で書き出す。
  書き出したものは暗号化する（AES-GCM。鍵は呼ぶ側が渡す＝鍵の値をこの部品は読まない・残さない・出さない）。
- 置く：Fly.io とは別の場所を必ず含む置き場（`Destination`）に置く。**サブスク契約までは、PC に接続したハードディスク**
  （`AttachedDrive`。2026-10-17 ユーザー：「クラウドだけでなく、暫定的にPCに接続のハードディスクに保存してください。サブスク契約まで」）。
  クラウド（Fly.io）の側は、データベースの置き場を Fly.io が毎日自動で写す（ボリュームのスナップショット。残す日数は Fly.io の設定）。
  ⇒ この処理は PC で動く（見守り役。**控えは契約者ごと**＝2026-10-18 ユーザー：「費用の見張りと控えは、契約者ごとですよ」）：
  - 契約者の控え（`run_contractor`）：その契約者の PC が、その契約者の依頼者ごとの AI用・提出用を、その PC のハードディスクに置く。
    読むのは、その契約者の取引から見える行だけ（表の持ち主の接続を契約者の PC に置かない）。知らせは、その契約者へ。
  - データベース全体の控え（`run_whole`・`take`）：すべての契約者のデータが入るので、管理者の PC だけ（表の持ち主の接続で読む）。
    データベースを契約者ごとに分けたら（Intent.md 1-4）、その契約者のデータベースの控えとして、その契約者の PC で取る。
- 戻せるかを確かめる：置いた控えのうち、Fly.io とは別の場所のものを読み戻し、指紋を比べ、復号して、確かめ用の空のデータベースに
  pg_restore で戻し、表ごとの行の数が取ったときと同じかを比べる。
- 記録（backup_runs）：うまくいっても、いかなくても1回ずつ残す（契約者の控えは、その契約者の記録）。いかなければ、止めずに CL-2 へ
  修正依頼を出し、管理者（契約者の控えなら、その契約者にも）に知らせる
  （2026-10-17 ユーザー：「止めずに CL1 か CL2 に修正依頼を出さないといけないだろ。人はそのプロセスをチェックもしくは承認するだけ」）。
- 毎日1回（`due`）：今日（日本時間）の、戻せることまで確かめた控えが無ければ取る。呼ぶのは見守り役（常駐の処理）。
- 記録・知らせ・修正依頼には、お客様の情報・鍵・接続文字列を入れない（行の数と指紋と、エラーの種類だけ）。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from salescall.core import fix_requests, notify, settings
from salescall.core.access import Access, Actor, Tx, snapshot, tool_connection

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
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())                    # ディスクに書き終えてから（読み戻しが OS の一時記憶から読めても、書けた証明にならない）
        tmp.replace(self.path / file_name)          # 書き終えてから名前を付ける（途中のファイルを控えと取り違えない）

    def get(self, file_name: str) -> bytes:
        return (self.path / file_name).read_bytes()


DRIVE_MARK = "salescall-backup-drive.txt"   # 控え用のハードディスクの一番上に置く印（中身は問わない）


@dataclass(frozen=True)
class AttachedDrive(LocalDirectory):
    """PC に接続したハードディスク（Fly.io とは別の場所）。ハードディスクの一番上に印のファイル（DRIVE_MARK）を置いておく。
    印が無ければ、つながっていないとみなして置かない（外れたときに、同じ名前のフォルダを PC の中に作って、そこへ置いてしまわない）。"""
    drive_root: Path = Path("/")
    offsite: bool = True

    def put(self, file_name: str, data: bytes) -> None:
        self._attached()
        super().put(file_name, data)

    def get(self, file_name: str) -> bytes:
        self._attached()
        return super().get(file_name)

    def _attached(self) -> None:
        if not (self.drive_root / DRIVE_MARK).is_file():
            raise DriveMissing(f"控え用のハードディスク（{self.name}）がつながっていません")


@dataclass(frozen=True)
class Result:
    ok: bool
    file_name: str | None
    error: str | None


class BackupFailed(Exception):
    """控えが取れなかった・置けなかった・戻せなかった（中身は、鍵・接続文字列・お客様の情報を含まない短い説明）。"""


class PersonNeeded(BackupFailed):
    """直すのが管理者の作業のもの（プログラムの直しではない）：修正依頼ではなく、管理者に `ask` を知らせる。"""
    ask = ""


class DriveMissing(PersonNeeded):
    """控え用のハードディスクがつながっていない。"""
    ask = "控え用のハードディスクがつながっていません。PC にハードディスクをつないでください。"


class KeyMissing(PersonNeeded):
    """控えの鍵が PC に入っていない・短すぎる。"""
    ask = "控えの鍵が PC に入っていません（または短すぎます）。手順書 docs/PC_SETUP.md のとおり、Proton Pass の文字を入れてください。"


def _ask(e: BaseException) -> str | None:
    return e.ask if isinstance(e, PersonNeeded) else None


def _counts(tx: Tx) -> dict[str, int]:
    tables = tx.all("SELECT schemaname || '.' || tablename AS t FROM pg_tables WHERE schemaname = ANY(%s) ORDER BY 1",
                    (list(SCHEMAS),))
    return {r["t"]: tx.one(f'SELECT count(*) AS n FROM {_quoted(r["t"])}')["n"] for r in tables}


def _quoted(qualified: str) -> str:
    schema, table = qualified.split(".", 1)
    return f'"{schema}"."{table}"'


# 控えの鍵（2026-10-17 ユーザー決定 案A：PC と管理者の写しの2か所。写しは Proton Pass で作った＝ユーザー）。
# 鍵の元は Proton Pass で作った文字（20文字以上）。PC の環境変数に入れ、そこから32バイトの鍵を作る（読むのは load_key の1か所）。
KEY_ENV = "SALESCALL_BACKUP_KEY"
KEY_MIN_CHARS = 20
_KEY_SALT = b"salescall-backup-key-1"     # 作り方を変えたら番号を上げる（前の控えは前の作り方でしか戻せない）


def key_from_text(text: str) -> bytes:
    """Proton Pass で作った文字から、控えの鍵（32バイト）を作る。前後の空白・改行は外す（写し間違いを防ぐ）。値は出さない。"""
    text = text.strip()
    if len(text) < KEY_MIN_CHARS:
        raise KeyMissing(f"控えの鍵の元は{KEY_MIN_CHARS}文字以上です（Proton Pass で作った文字を、そのまま写してください）")
    return hashlib.scrypt(text.encode(), salt=_KEY_SALT, n=2 ** 14, r=8, p=1, dklen=32)


def load_key(environ: dict[str, str] | None = None) -> bytes:
    """PC の環境変数から控えの鍵を読む（見守り役が呼ぶ）。無ければ、入れ方を書いた KeyMissing（値は書かない）。"""
    text = (os.environ if environ is None else environ).get(KEY_ENV, "")
    if not text.strip():
        raise KeyMissing(f"控えの鍵が入っていません。PC の環境変数 {KEY_ENV} に、Proton Pass に置いた文字をそのまま入れてください")
    return key_from_text(text)


def key_matches(text: str, environ: dict[str, str] | None = None) -> bool:
    """PC の持ち主（契約者・管理者）が貼った Proton Pass の文字と、PC に入っている文字が、同じ鍵になるか（PC の文字が1文字でも違うと、確かめは通るのに
    Proton Pass の文字では控えを戻せない。見守り役の check-key が呼ぶ。どちらの値も出さない）。"""
    return key_from_text(text) == load_key(environ)


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


# 道具（pg_dump など）の誤りの文は残さない（お客様の行の中身・接続先が入りうる。2026-10-18 の見直しで、pg_restore の誤りの文に
# お客様の名前と番号が入り、記録・修正依頼・知らせに残ることを確かめた）。残すのは、どの段で・どの種類の誤りかだけ
_CAUSES = ((r"could not connect|connection to server|Connection refused|timeout expired|could not translate host",
            "データベースにつながらない"),
           (r"password authentication failed|permission denied|must be owner|no pg_hba", "権限が足りない"),
           (r"No space left|could not write|disk full|Disk quota", "置き場の空きが足りない"),
           (r"server version|version mismatch", "道具の版が、データベースの版と合わない"),
           (r"out of memory", "メモリが足りない"))


def _run(args: list[str], conninfo: str, what: str) -> None:
    dsn, secret = tool_connection(conninfo)    # パスワードは引数でなく環境で渡す
    done = subprocess.run([*args, "-d", dsn], capture_output=True, text=True, env={**os.environ, **secret})
    if done.returncode != 0:
        cause = next((label for pattern, label in _CAUSES if re.search(pattern, done.stderr, re.IGNORECASE)), "種類は分からない")
        raise BackupFailed(f"{what}に失敗しました（{cause}。終了コード {done.returncode}）")


def _identity(tx: Tx) -> tuple[Any, ...]:
    """どのデータベースか（同じサーバーの同じデータベースなら同じ値）。"""
    row = tx.one("SELECT current_database() AS d, pg_postmaster_start_time() AS t")
    return row["d"], row["t"]


def _why(e: Exception) -> str:
    """記録に残す誤りの説明（控えの部品が書いた説明だけ。思いがけない誤りは種類の名前だけ＝中身を残さない）。"""
    return str(e) if isinstance(e, BackupFailed) else f"思いがけない誤り（{type(e).__name__}）"


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
        if not any(d.offsite for d in destinations):
            raise BackupFailed("置き場に、Fly.io とは別の場所（PC に接続したハードディスクなど）が要ります")
        with tempfile.TemporaryDirectory(prefix="salescall-backup-") as work:
            dump = Path(work) / "db.dump"
            with snapshot(source_conninfo) as (snap, tx):
                counts = _counts(tx)
                source_identity = _identity(tx)
                _run(["pg_dump", "--format=custom", f"--snapshot={snap}", "--no-owner", "--no-acl", "-f", str(dump)],
                     source_conninfo, "書き出し（pg_dump）")
            blob = encrypt(dump.read_bytes(), key, file_name)
        for d in destinations:
            d.put(file_name, blob)
            placed.append(d.name)
        _verify(blob, file_name, [d for d in destinations if d.offsite][0], restore_conninfo, counts, key=key,
                source_identity=source_identity)
    except Exception as e:  # noqa: BLE001 ここで受けて記録する（止めない）
        error = _why(e)
        _record(app, started, file_name if blob else None, blob, counts, placed, error, person=_ask(e))
        return Result(False, None, error)
    _record(app, started, file_name, blob, counts, placed, None, person=None)
    return Result(True, file_name, None)


def _verify(blob: bytes, file_name: str, offsite: Destination, restore_conninfo: str, counts: dict[str, int], *,
            key: bytes, source_identity: tuple[Any, ...]) -> None:
    """Fly.io とは別の場所から読み戻し、指紋を比べ、復号し、確かめ用のデータベースに戻して、行の数を比べる。"""
    back = offsite.get(file_name)
    if hashlib.sha256(back).hexdigest() != hashlib.sha256(blob).hexdigest():
        raise BackupFailed(f"置き場「{offsite.name}」から読み戻した控えが、置いたものと違います")
    with tempfile.TemporaryDirectory(prefix="salescall-restore-") as work:
        dump = Path(work) / "db.dump"
        dump.write_bytes(decrypt(back, key, file_name))
        with snapshot(restore_conninfo) as (_, tx):   # 確かめ用のデータベースの、いまある表を消す（前の確かめの残り）
            # ⚠️ 消す前に、確かめ用が本番のデータベースでないことを確かめる（接続先の書き間違いで、発信禁止の台帳を含む本番の表を
            # 控えの処理が消さないように。2026-10-18 の見直し）
            if _identity(tx) == source_identity:
                raise BackupFailed("確かめ用のデータベースが、本番のデータベースと同じです。消さずに止めました（接続先の設定を直す）")
            drops = [_quoted(f"{r['schemaname']}.{r['tablename']}") for r in tx.all(
                "SELECT schemaname, tablename FROM pg_tables WHERE schemaname = ANY(%s)", (list(SCHEMAS),))]
        _run(["psql", "-q", "-v", "ON_ERROR_STOP=1", "-c",
              "DROP SCHEMA IF EXISTS salescall CASCADE; " + "".join(f"DROP TABLE IF EXISTS {t} CASCADE; " for t in drops
                                                                    if t.startswith('"public"'))],
             restore_conninfo, "確かめ用のデータベースの片付け")
        _run(["pg_restore", "--no-owner", "--no-acl", "--exit-on-error", str(dump)], restore_conninfo, "戻す（pg_restore）")
    with snapshot(restore_conninfo) as (_, tx):
        got = _counts(tx)
    if got != counts:
        diff = sorted(t for t in set(got) | set(counts) if got.get(t) != counts.get(t))
        raise BackupFailed(f"戻した控えの行の数が、取ったときと違います（{len(diff)} 個の表。例：{', '.join(diff[:3])}）")


# 控えの種類（記録の kind）と、知らせ・修正依頼に書く名前（1か所）
PARTS = {"system": "データベース全体の控え", "client_ai": "依頼者ごとの AI用の控え", "submission": "提出用の控え",
         "prune": "古い控えの片付け", "drive": "控え用のハードディスクの確かめ", "key": "控えの鍵の確かめ"}
_DAILY = ("system", "client_ai", "prune")   # 1日1回でよい種類（今日うまくいったら、もうしない）
RETRY_AFTER = timedelta(hours=1)            # 失敗したら、同じ種類（・同じ契約者・同じ依頼者）は1時間あけてやり直す
DRIVE_MISSING = "控え用のハードディスクがつながっていません"


def _in(app: Access, tenant_id: UUID | None, client_id: UUID | None, work: Any) -> Any:
    """その記録の範囲の取引（データベース全体＝全体の取引・契約者の控え＝その契約者の取引・依頼者の控え＝その依頼者の取引。0064）。"""
    if client_id is not None:
        return app.in_client(SYSTEM, tenant_id, client_id, work)
    if tenant_id is not None:
        return app.in_tenant(SYSTEM, tenant_id, work)
    return app.in_system(SYSTEM, work)


def _record(app: Access, started: datetime, file_name: str | None, blob: bytes | None, counts: dict[str, int] | None,
            placed: list[str], error: str | None, *, person: str | None, part: str = "system",
            tenant_id: UUID | None = None, client_id: UUID | None = None) -> None:
    """1回ずつ記録する（やり直しは `_may_try` が1時間あけるので、失敗の行は種類・契約者・依頼者ごとに1日24行まで）。
    失敗の知らせのあて先（決めるのはここだけ）：
    - 直すのが PC の前の作業（person＝ハードディスクをつなぐ・鍵を入れる）なら、その PC の持ち主へそれを知らせる（契約者の控え＝
      その契約者・データベース全体の控え＝管理者）。
    - ほかは種類ごとに CL-2 へ修正依頼（同じ不具合は、直したと報告するまで1つ）を出し、管理者へ知らせる（契約者の控えなら、
      その契約者にも。プログラムの誤りは直せるのが管理者だけ＝Intent.md 1-4）。"""
    def work(tx: Tx) -> None:
        tx.execute("INSERT INTO salescall.backup_runs (started_at, file_name, sha256, size_bytes, table_counts, destinations,"
                   " restore_ok, error, kind, tenant_id, client_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                   (started, file_name, None if blob is None else hashlib.sha256(blob).hexdigest(),
                    None if blob is None else len(blob), None if counts is None else json.dumps(counts, sort_keys=True), placed,
                    error is None, error, part, tenant_id, client_id))
        if error is not None:
            day = tx.one("SELECT salescall.jst(now())::date AS d")["d"]
            if person is not None:   # PC の前の作業（プログラムの直しではない）
                summary = person
            else:
                fix_requests.request(tx, "CL-2", "backup_failed", key=f"backup_failed:{part}",
                                     summary=f"{PARTS[part]}：{error}", refs={"client_id": client_id})
                summary = f"{PARTS[part]}が取れませんでした（CL-2 に修正依頼を出しました）。"
            mine = tenant_id is not None
            notify.emit(tx, "backup_failed", key=f"backup_failed:{day}:{'person:' + person if person else part}", summary=summary,
                        refs={"error": error, "client_id": client_id},
                        admin=not mine or person is None, contractor=mine)
    _in(app, tenant_id, client_id, work)


def _may_try(app: Access, part: str, tenant_id: UUID | None = None, client_id: UUID | None = None) -> bool:
    """その種類（・その契約者・その依頼者）を、いまやってよいか（**判断はこの1か所**）：1日1回でよい種類は今日うまくいっていれば、
    しない。失敗したあとは1時間あける（見守り役が数分ごとに呼んでも、重い書き出しを繰り返さない・控えを重ねて取らない）。"""
    return _in(app, tenant_id, None, lambda tx: tx.one(
        "SELECT NOT (%(daily)s AND EXISTS (SELECT 1 FROM salescall.backup_runs WHERE restore_ok AND kind = %(part)s"
        "                  AND tenant_id IS NOT DISTINCT FROM %(t)s AND client_id IS NOT DISTINCT FROM %(c)s"
        "                  AND salescall.jst(started_at)::date = salescall.jst(now())::date))"
        "   AND NOT EXISTS (SELECT 1 FROM salescall.backup_runs WHERE NOT restore_ok AND kind = %(part)s"
        "                  AND tenant_id IS NOT DISTINCT FROM %(t)s AND client_id IS NOT DISTINCT FROM %(c)s"
        "                  AND started_at > now() - %(after)s) AS ok",
        {"daily": part in _DAILY, "part": part, "t": tenant_id, "c": client_id, "after": RETRY_AFTER}))["ok"]


def due(app: Access) -> bool:
    """今日（日本時間）の、戻せることまで確かめたデータベース全体の控えが、まだ無いか（画面・見守り役が見る）。"""
    return app.in_system(SYSTEM, lambda tx: tx.one(
        "SELECT NOT EXISTS (SELECT 1 FROM salescall.backup_runs WHERE restore_ok AND kind = 'system'"
        "                   AND salescall.jst(started_at)::date = salescall.jst(now())::date) AS due"))["due"]


def _drive_ready(app: Access, drive: "Drive", tenant_id: UUID | None) -> bool:
    """ハードディスクがつながっているか。外れていれば、何も置かずに PC の持ち主へ「つないでください」（1時間に1回まで記録）。"""
    if drive.attached():
        return True
    if _may_try(app, "drive", tenant_id):
        _record(app, datetime.now().astimezone(), None, None, None, [], DRIVE_MISSING, person=DriveMissing.ask, part="drive",
                tenant_id=tenant_id)
    return False


def _key(app: Access, environ: dict[str, str], tenant_id: UUID | None) -> bytes | None:
    """PC の鍵を読む。無ければ PC の持ち主へ「鍵を入れてください」（1時間に1回まで記録）。"""
    try:
        return load_key(environ)
    except KeyMissing as e:
        if _may_try(app, "key", tenant_id):
            _record(app, datetime.now().astimezone(), None, None, None, [], str(e), person=e.ask, part="key", tenant_id=tenant_id)
        return None


def latest(app: Access) -> dict[str, Any] | None:
    """いちばん新しい記録（管理者の画面が、控えが取れているかを見る）。"""
    return app.in_system(SYSTEM, lambda tx: tx.one(
        "SELECT started_at, kind, tenant_id, client_id, file_name, size_bytes, destinations, restore_ok, error"
        " FROM salescall.backup_runs ORDER BY run_id DESC LIMIT 1"))


# ═════════ 依頼者ごとの控え（2026-10-17 ユーザー決定。表の作り 0054） ═════════
# ハードディスクの中の形：<ハードディスク>\salescall-backup\<依頼者のフォルダ>\AI・submission・recording ／ データベース全体は
# <ハードディスク>\salescall-backup\system\AI。ユーザー：「E:\salescall-backup\client1\AI」「…\client1\submission」。
BASE = "salescall-backup"
AI, SUBMISSION, RECORDING = "AI", "submission", "recording"
WHOLE = "system"                                                    # データベース全体の控えのフォルダ（依頼者のフォルダには使えない名前）
_DATED = re.compile(r"^salescall-(\d{8})-(\d{6})[.-].*\.enc$")      # 控えのファイルの名前（日時つき。消す決まりはこの名前だけを見る）
_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
# 会社の種類の言葉（フォルダの名前には入れない。2026-10-17 ユーザー：「架空商事-本社」だけでいい）。文字の形をそろえた（NFKC）あとの形で書く
_COMPANY_KINDS = re.compile(
    r"(株式|有限|合同|合資|合名)会社|(一般|公益)(社団|財団)法人|特定非営利活動法人|NPO法人"
    r"|(医療|社会福祉|学校|宗教|弁護士|税理士|行政書士|司法書士|監査|社会保険労務士)法人"
    r"|\((株|有|同|資|名|社|財|医|福|学|宗)\)")
_BREAKS = re.compile(r'(\s|[\\/:*?"<>|\x00-\x1f])+')    # 区切り（空白・Windows で使えない文字）は「-」1つにする
_RESERVED = {"con", "prn", "aux", "nul", WHOLE} | {f"com{i}" for i in range(1, 10)} | {f"lpt{i}" for i in range(1, 10)}


@dataclass(frozen=True)
class Drive:
    """PC に接続したハードディスク（root＝その一番上。例 E:\\）。一番上の印のファイルで、つながっているかを見る。"""
    root: Path

    def attached(self) -> bool:
        return (self.root / DRIVE_MARK).is_file()

    def folder(self, *parts: str) -> AttachedDrive:
        return AttachedDrive("PC に接続したハードディスク（" + "\\".join((BASE, *parts)) + "）", self.root.joinpath(BASE, *parts),
                             drive_root=self.root)


def folder_name_from(name: str) -> str | None:
    """依頼者の名前（依頼者タブの自社情報の会社名）から、フォルダの名前を作る。使えなければ None。
    文字の形をそろえ（全角の英数字・空白・㈱ など）、会社の種類の言葉（株式会社・(株) など）を外し、区切り（空白・Windows で
    使えない文字）を「-」にする（例「株式会社 架空商事:本社」⇒「架空商事-本社」。2026-10-17 ユーザー）。"""
    n = _COMPANY_KINDS.sub(" ", unicodedata.normalize("NFKC", name))
    n = _BREAKS.sub("-", n.strip()).strip("-. ")[:60].rstrip("-. ")
    return None if not n or n.lower() in _RESERVED else n


def client_folder(app: Access, tenant_id: UUID, client_id: UUID) -> str:
    """その依頼者のフォルダの名前（最初に控えを置くときに決め、あとから変えない）。依頼者の名前から作り、使えない・ほかと重なる
    ときは client1・client2…（2026-10-17 ユーザー：「client1」「client2」がデフォルト、自社情報からフォルダ名が自動で）。"""
    def work(tx: Tx) -> str:
        got = tx.one("SELECT folder_name FROM salescall.client_backup_folders WHERE client_id = %s", (client_id,))
        if got is not None:
            return got["folder_name"]
        name = folder_name_from(tx.one("SELECT name FROM salescall.clients WHERE client_id = %s", (client_id,))["name"])
        taken = lambda n: tx.one("SELECT salescall.backup_folder_taken(%s) AS t", (n,))["t"]   # noqa: E731
        if name is None or taken(name):
            name = next(f"client{i}" for i in range(1, 100_000) if not taken(f"client{i}"))
        tx.execute("INSERT INTO salescall.client_backup_folders (tenant_id, client_id, folder_name) VALUES (%s, %s, %s)",
                   (tenant_id, client_id, name))
        return name
    return app.in_client(SYSTEM, tenant_id, client_id, work)


def _client_tables(tx: Tx) -> list[str]:
    """依頼者を持つ表すべて（表の作りの台帳から。読む権限の無い表があれば、読むときに誤りになる＝黙って抜けない）。"""
    return [r["t"] for r in tx.all(
        "SELECT c.relname AS t FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
        " JOIN pg_attribute a ON a.attrelid = c.oid AND a.attname = 'client_id' AND NOT a.attisdropped"
        " WHERE n.nspname = 'salescall' AND c.relkind IN ('r', 'p') ORDER BY 1")]


def _client_export(tx: Tx, client_id: UUID) -> tuple[bytes, dict[str, int]]:
    """その依頼者の行（依頼者を持つ表すべて）を、1行ずつの形で書き出す（AI用の控え。tx＝その依頼者の書かない取引＝1つの時点。ほかの依頼者・
    ほかの契約者の行は、データベースが見せない。表の持ち主の接続は使わない＝契約者の PC に置かない）。"""
    lines, counts = [], {}
    for t in _client_tables(tx):
        rows = tx.all(f'SELECT row_to_json(x)::text AS r FROM salescall."{t}" x WHERE x.client_id = %s ORDER BY 1', (client_id,))
        counts[t] = len(rows)
        lines += ['{"table": ' + json.dumps(t) + ', "row": ' + r["r"] + "}" for r in rows]
    schema = tx.one("SELECT salescall.schema_version() AS n")["n"]
    header = json.dumps({"format": "salescall-client-ai-1", "client_id": str(client_id), "schema": schema, "counts": counts},
                        sort_keys=True)
    return ("\n".join([header, *lines]) + "\n").encode(), counts


def read_client_export(data: bytes) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    """AI用の控え（復号したもの）を読む：見出しと、表ごとの行。"""
    header, *rest = data.decode().splitlines()
    head = json.loads(header)
    tables: dict[str, list[dict[str, Any]]] = {t: [] for t in head["counts"]}
    for line in rest:
        x = json.loads(line)
        tables[x["table"]].append(x["row"])
    return head, tables


def _client_ai(app: Access, drive: Drive, tenant_id: UUID, client_id: UUID, *, key: bytes) -> Result:
    """その依頼者の AI用の控え（その依頼者の取引で、1つの時点の行を書き出す）。"""
    started = datetime.now().astimezone()
    file_name = f"salescall-{started:%Y%m%d-%H%M%S}.ai.enc"
    blob, counts, placed = None, None, []
    try:
        dest = drive.folder(client_folder(app, tenant_id, client_id), AI)
        data, counts = app.in_client(SYSTEM, tenant_id, client_id, lambda tx: _client_export(tx, client_id), read_only=True)
        blob = encrypt(data, key, file_name)
        dest.put(file_name, blob)
        placed.append(dest.name)
        back = dest.get(file_name)          # 読み戻して、指紋・復号・行の数を確かめる（戻せるか）
        if hashlib.sha256(back).hexdigest() != hashlib.sha256(blob).hexdigest():
            raise BackupFailed(f"置き場「{dest.name}」から読み戻した控えが、置いたものと違います")
        head, tables = read_client_export(decrypt(back, key, file_name))
        if head["counts"] != counts or {t: len(r) for t, r in tables.items()} != counts:
            raise BackupFailed("読み戻した AI用の控えの行の数が、取ったときと違います")
    except Exception as e:  # noqa: BLE001 ここで受けて記録する（止めない）
        error = _why(e)
        _record(app, started, file_name if blob else None, blob, counts, placed, error, person=_ask(e),
                part="client_ai", tenant_id=tenant_id, client_id=client_id)
        return Result(False, None, error)
    _record(app, started, file_name, blob, counts, placed, None, person=None, part="client_ai", tenant_id=tenant_id,
            client_id=client_id)
    return Result(True, file_name, None)


def record_submission(tx: Tx, file_name: str, data: bytes) -> int:
    """依頼者へ渡した結果のファイルを、提出用の控えのために置く（出力＝CL-3 が、渡したときに依頼者の取引で1回呼ぶ）。
    契約者の PC の控えの処理がハードディスクへ写したら、中身は消える（Fly.io には残さない＝2026-10-17 ユーザー）。"""
    return tx.one("INSERT INTO salescall.submission_files (tenant_id, client_id, file_name, sha256, content)"
                  " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, %s) RETURNING submission_id",
                  (file_name, hashlib.sha256(data).hexdigest(), data))["submission_id"]


def _clients(app: Access, tenant_id: UUID) -> list[UUID]:
    return app.in_tenant(SYSTEM, tenant_id, lambda tx: [r["client_id"] for r in tx.all(
        "SELECT client_id FROM salescall.clients ORDER BY created_at, client_id")])


def copy_submissions(app: Access, drive: Drive, tenant_id: UUID, *, key: bytes) -> list[Result]:
    """その契約者の依頼者の、まだ写していない提出用のファイルを、ハードディスクへ写す。**見守り役が数分ごとに呼ぶ**（「提出用は
    Fly.io には保存しない」＝2026-10-17 ユーザー。毎日1回だと最長1日 Fly.io に残るため）。ハードディスクが外れていれば写さずに残し
    （消すと失う）、契約者に「つないでください」と知らせる。うまく写せた依頼者の分は返さない（失敗だけを返す）。"""
    if not _drive_ready(app, drive, tenant_id):
        return [Result(False, None, DRIVE_MISSING)]
    failed = []
    def step(client_id: UUID | None, work: Any) -> None:
        try:
            work()
        except Exception as e:  # noqa: BLE001 記録して続ける（止めない）
            _record(app, datetime.now().astimezone(), None, None, None, [], _why(e), person=_ask(e),
                    part="submission", tenant_id=tenant_id, client_id=client_id)
            failed.append(Result(False, None, _why(e)))
    clients: list[UUID] = []
    if _may_try(app, "submission", tenant_id):
        step(None, lambda: clients.extend(_clients(app, tenant_id)))
    for c in clients:
        if _may_try(app, "submission", tenant_id, c):
            step(c, lambda c=c: _copy_submissions(app, drive, tenant_id, c, key=key))
    return failed


def _copy_submissions(app: Access, drive: Drive, tenant_id: UUID, client_id: UUID, *, key: bytes) -> int:
    """まだ写していない提出用のファイルを、ハードディスクへ写す（暗号化・読み戻して確かめてから、Fly.io の中身を消す）。"""
    rows = app.in_client(SYSTEM, tenant_id, client_id, lambda tx: tx.all(
        "SELECT submission_id, delivered_at, file_name, sha256, content FROM salescall.submission_files"
        " WHERE copied_at IS NULL ORDER BY submission_id"))
    if not rows:
        return 0
    dest = drive.folder(client_folder(app, tenant_id, client_id), SUBMISSION)
    for r in rows:
        content = bytes(r["content"])
        if hashlib.sha256(content).hexdigest() != r["sha256"]:
            raise BackupFailed("提出用のファイルの中身が、置いたときと違います")
        name = f"salescall-{r['delivered_at'].astimezone():%Y%m%d-%H%M%S}-{r['submission_id']}-{r['file_name']}.enc"
        blob = encrypt(content, key, name)
        dest.put(name, blob)
        if decrypt(dest.get(name), key, name) != content:
            raise BackupFailed(f"置き場「{dest.name}」から読み戻した提出用の控えが、置いたものと違います")
        app.in_client(SYSTEM, tenant_id, client_id, lambda tx: tx.execute(
            "UPDATE salescall.submission_files SET content = NULL, copied_at = now(), copied_as = %s WHERE submission_id = %s",
            (name, r["submission_id"])))
    return len(rows)


def open_file(path: Path, key: bytes) -> bytes:
    """ハードディスクの控えのファイルを開く（復号する。問い合わせに答えるときなど）。"""
    return decrypt(path.read_bytes(), key, path.name)


def recording_file_name(attempt_id: UUID, started: datetime, part: str) -> str:
    """ハードディスクに置く録音のファイルの名前（録音を写す処理＝電話接続・CL-1 が使う1か所）。日時と通話を名前に入れる
    （古い録音を消す処理が、日時と「測り終わるまで消さない」印を見るため）。part＝その通話の中の録音の名前（例 call）。"""
    if not re.fullmatch(r"[a-z0-9_]{1,30}", part):
        raise ValueError(f"録音の名前は英小文字・数字・_ で書きます（{part!r}）")
    return f"salescall-{started.astimezone():%Y%m%d-%H%M%S}-{attempt_id}-{part}.enc"


_HELD_ID = re.compile(r"-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})-")


def prune(app: Access, drive: Drive, tenant_id: UUID | None, *, now: datetime | None = None) -> int:
    """残す日数を過ぎた控えを消す（名前に日時のある控えのファイルだけ）。tenant_id＝その契約者の依頼者のフォルダ（AI用・提出用＝
    システムの設定、録音＝その契約者の設定）。None＝データベース全体のフォルダ（管理者の PC）。ほかの契約者のフォルダには触れない。
    戻せることまで確かめた控えのうち、フォルダごとに一番新しいものは消さない（控えの失敗が長く続いても、1つは残る）。
    録音は、測り終わるまで消さない印（`recordings.hold`）の付いた通話のものを消さない（2026-10-17 ユーザー 案A）。"""
    now = datetime.now().astimezone() if now is None else now
    def look(tx: Tx) -> tuple[dict[str, dict[str, int]], set[str]]:
        days = {AI: int(settings.system_value(tx, "backup.ai.keep_days_drive")),
                SUBMISSION: int(settings.system_value(tx, "backup.submission.keep_days_drive"))}
        if tenant_id is None:
            folders = {WHOLE: {AI: days[AI]}}
        else:
            days[RECORDING] = int(settings.resolve(tx, "recording.keep_days_drive", tenant_id=tenant_id, client_id=None,
                                                   business_id=None))
            folders = {r["f"]: days for r in tx.all("SELECT folder_name AS f FROM salescall.client_backup_folders")}
        verified = {r["f"] for r in tx.all("SELECT file_name AS f FROM salescall.backup_runs WHERE restore_ok AND kind = %s"
                                           " AND tenant_id IS NOT DISTINCT FROM %s",
                                           ("system" if tenant_id is None else "client_ai", tenant_id))}
        return folders, verified
    folders, verified = _in(app, tenant_id, None, look)
    removed = 0
    for name, keep in folders.items():
        folder = drive.root / BASE / name
        for kind, n in keep.items():
            old = [f for f in ((folder / kind).iterdir() if (folder / kind).is_dir() else ())
                   if (m := _DATED.match(f.name))
                   and datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S").replace(tzinfo=now.tzinfo)
                   < now - timedelta(days=n)]
            if kind == AI and old:   # 消すものがあるときだけ（old があればフォルダはある）
                newest = max((f.name for f in (folder / kind).iterdir() if f.name in verified), default=None)
                old = [f for f in old if f.name != newest]
            if kind == RECORDING and old:
                ids = {f: m.group(1) for f in old if (m := _HELD_ID.search(f.name))}
                held = set(_in(app, tenant_id, None, lambda tx: [str(r["h"]) for r in tx.all(
                    "SELECT salescall.recordings_held(%s::uuid[]) AS h", (sorted(set(ids.values())),))]))
                old = [f for f in old if ids.get(f) not in held]
            for f in old:
                f.unlink()
                removed += 1
    return removed


def _prune_step(app: Access, drive: Drive, tenant_id: UUID | None) -> list[Result]:
    if not _may_try(app, "prune", tenant_id):
        return []
    started = datetime.now().astimezone()
    try:
        prune(app, drive, tenant_id)
    except Exception as e:  # noqa: BLE001 片付けが落ちても（ファイルが使用中など）、記録して続ける
        _record(app, started, None, None, None, [], _why(e), person=_ask(e), part="prune", tenant_id=tenant_id)
        return [Result(False, None, _why(e))]
    _record(app, started, None, None, None, [], None, person=None, part="prune", tenant_id=tenant_id)
    return []


# ═════════ 見守り役が呼ぶ入口（2つ。2026-10-18 ユーザー：「費用の見張りと控えは、契約者ごとですよ」） ═════════
def run_contractor(app: Access, tenant_id: UUID, drive_root: Path, environ: dict[str, str]) -> list[Result]:
    """契約者の控え（その契約者の PC の見守り役が数分ごとに呼んでよい。やるかは種類ごとに `_may_try`）：その契約者の依頼者ごとの
    AI用（1日1回）→ 提出用を写す（数分ごと）→ その契約者のフォルダの古いものを消す（1日1回）。読むのは、その契約者の取引から見える
    その契約者の行だけ（表の持ち主の接続は使わない）。置くのは、その PC のハードディスク・その PC の鍵（契約者が自分の Proton Pass に
    置いた文字）。ハードディスク・鍵が無ければ、何も置かずに契約者へ知らせる。"""
    key = _key(app, environ, tenant_id)
    if key is None:
        return [Result(False, None, PARTS["key"])]
    drive = Drive(drive_root)
    if not _drive_ready(app, drive, tenant_id):
        return [Result(False, None, DRIVE_MISSING)]
    results = []
    try:
        clients = _clients(app, tenant_id) if _may_try(app, "client_ai", tenant_id) else []
    except Exception as e:  # noqa: BLE001 依頼者の一覧が読めない：記録して続ける（止めない）
        _record(app, datetime.now().astimezone(), None, None, None, [], _why(e), person=None, part="client_ai", tenant_id=tenant_id)
        results.append(Result(False, None, _why(e)))
        clients = []
    for c in clients:
        if _may_try(app, "client_ai", tenant_id, c):
            results.append(_client_ai(app, drive, tenant_id, c, key=key))
    results += copy_submissions(app, drive, tenant_id, key=key)
    return results + _prune_step(app, drive, tenant_id)


def run_whole(app: Access, source_conninfo: str, restore_conninfo: str, drive_root: Path,
              environ: dict[str, str]) -> list[Result]:
    """データベース全体の控え（管理者の PC の見守り役が呼ぶ。1日1回、戻せるかまで確かめる）と、そのフォルダの古いものを消す。
    データベース全体には、すべての契約者のデータが入るので、管理者のハードディスクにだけ置く（契約者の PC には置かない。データベースを
    契約者ごとに分けたら、その契約者のデータベースの控えとして、その契約者の PC で取る＝Intent.md 1-4）。"""
    key = _key(app, environ, None)
    if key is None:
        return [Result(False, None, PARTS["key"])]
    drive = Drive(drive_root)
    if not _drive_ready(app, drive, None):
        return [Result(False, None, DRIVE_MISSING)]
    results = []
    if _may_try(app, "system"):
        results.append(take(app, source_conninfo, restore_conninfo, [drive.folder(WHOLE, AI)], key=key))
    return results + _prune_step(app, drive, None)
