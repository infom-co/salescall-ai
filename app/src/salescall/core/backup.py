"""データベースの控え（Intent.md 14-1。表の作り 0052）。持ち主 CL-2。

Intent.md 14-1：「毎日自動で取り、Fly.io とは別の場所にも置き、戻せることを試験で確かめ、取れなかったら知らせる（発信禁止の台帳を
失うと、断ったお客様にまたかけるため）」。CL-1 の条件：控えも暗号化する。
- 取る（`take`）：ある時点の見え方を固定し（`access.snapshot`）、その時点の表ごとの行の数を数え、同じ時点を pg_dump で書き出す。
  書き出したものは暗号化する（AES-GCM。鍵は呼ぶ側が渡す＝鍵の値をこの部品は読まない・残さない・出さない）。
- 置く：Fly.io とは別の場所を必ず含む置き場（`Destination`）に置く。**サブスク契約までは、PC に接続したハードディスク**
  （`AttachedDrive`。2026-10-17 ユーザー：「クラウドだけでなく、暫定的にPCに接続のハードディスクに保存してください。サブスク契約まで」）。
  クラウド（Fly.io）の側は、データベースの置き場を Fly.io が毎日自動で写す（ボリュームのスナップショット。残す日数は Fly.io の設定）。
  ⇒ この処理は PC で動く（見守り役と同じ PC。データベースへは表の持ち主の接続で読む）。
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


class DriveMissing(BackupFailed):
    """控え用のハードディスクがつながっていない（直すのは人：ハードディスクをつなぐ。修正依頼ではなく、管理者に知らせる）。"""


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
        if not any(d.offsite for d in destinations):
            raise BackupFailed("置き場に、Fly.io とは別の場所（PC に接続したハードディスクなど）が要ります")
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
        _record(app, started, file_name if blob else None, blob, counts, placed, error, drive=isinstance(e, DriveMissing))
        return Result(False, None, error)
    _record(app, started, file_name, blob, counts, placed, None, drive=False)
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


# 控えの種類（記録の kind）と、知らせ・修正依頼に書く名前（1か所）
PARTS = {"system": "データベース全体の控え", "client_ai": "依頼者ごとの AI用の控え", "submission": "提出用の控え"}


def _record(app: Access, started: datetime, file_name: str | None, blob: bytes | None, counts: dict[str, int] | None,
            placed: list[str], error: str | None, *, drive: bool, part: str = "system", client_id: UUID | None = None) -> None:
    """1回ずつ記録する。失敗は、ハードディスクが外れていれば管理者に「つないでください」、ほかは種類ごとに1日1つ CL-2 へ修正依頼
    （同じ日に別の種類が失敗しても、まとめて消えないように種類で分ける）。"""
    def work(tx: Tx) -> None:
        tx.execute("INSERT INTO salescall.backup_runs (started_at, file_name, sha256, size_bytes, table_counts, destinations,"
                   " restore_ok, error, kind, client_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                   (started, file_name, None if blob is None else hashlib.sha256(blob).hexdigest(),
                    None if blob is None else len(blob), None if counts is None else json.dumps(counts, sort_keys=True), placed,
                    error is None, error, part, client_id))
        if error is not None:
            day = tx.one("SELECT salescall.jst(now())::date AS d")["d"]
            if drive:   # ハードディスクをつなぐのは人の作業（プログラムの直しではない）
                summary = "控え用のハードディスクがつながっていません。PC にハードディスクをつないでください。"
            else:
                fix_requests.request(tx, "CL-2", "backup_failed", key=f"backup_failed:{day}:{part}",
                                     summary=f"{PARTS[part]}：{error}", refs={"client_id": client_id})
                summary = f"{PARTS[part]}が取れませんでした（CL-2 に修正依頼を出しました）。"
            notify.emit(tx, "backup_failed", key=f"backup_failed:{day}:{'drive' if drive else part}", summary=summary,
                        refs={"error": error, "client_id": client_id}, admin=True, contractor=False)
    app.in_system(SYSTEM, work)


def due(app: Access) -> bool:
    """今日（日本時間）の、戻せることまで確かめたデータベース全体の控えが、まだ無いか（見守り役が毎日1回 `run_daily` を呼ぶときに見る）。"""
    return app.in_system(SYSTEM, lambda tx: tx.one(
        "SELECT NOT EXISTS (SELECT 1 FROM salescall.backup_runs WHERE restore_ok AND kind = 'system'"
        "                   AND salescall.jst(started_at)::date = salescall.jst(now())::date) AS due"))["due"]


def latest(app: Access) -> dict[str, Any] | None:
    """いちばん新しい記録（画面・見守り役が、控えが取れているかを見る）。"""
    return app.in_system(SYSTEM, lambda tx: tx.one(
        "SELECT started_at, kind, client_id, file_name, size_bytes, destinations, restore_ok, error FROM salescall.backup_runs"
        " ORDER BY run_id DESC LIMIT 1"))


# ═════════ 依頼者ごとの控え（2026-10-17 ユーザー決定。表の作り 0054） ═════════
# ハードディスクの中の形：<ハードディスク>\salescall-backup\<依頼者のフォルダ>\AI・submission・recording ／ データベース全体は
# <ハードディスク>\salescall-backup\system\AI。ユーザー：「E:\salescall-backup\client1\AI」「…\client1\submission」。
BASE = "salescall-backup"
AI, SUBMISSION, RECORDING = "AI", "submission", "recording"
_DATED = re.compile(r"^salescall-(\d{8})-(\d{6})[.-].*\.enc$")      # 控えのファイルの名前（日時つき。消す決まりはこの名前だけを見る）
_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
# 会社の種類の言葉（フォルダの名前には入れない。2026-10-17 ユーザー：「架空商事-本社」だけでいい）。文字の形をそろえた（NFKC）あとの形で書く
_COMPANY_KINDS = re.compile(
    r"(株式|有限|合同|合資|合名)会社|(一般|公益)(社団|財団)法人|特定非営利活動法人|NPO法人"
    r"|(医療|社会福祉|学校|宗教|弁護士|税理士|行政書士|司法書士|監査|社会保険労務士)法人"
    r"|\((株|有|同|資|名|社|財|医|福|学|宗)\)")
_BREAKS = re.compile(r'(\s|[\\/:*?"<>|\x00-\x1f])+')    # 区切り（空白・Windows で使えない文字）は「-」1つにする
_RESERVED = {"con", "prn", "aux", "nul", "system"} | {f"com{i}" for i in range(1, 10)} | {f"lpt{i}" for i in range(1, 10)}


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
    return [r["t"] for r in tx.all(
        "SELECT c.table_name AS t FROM information_schema.columns c"
        " JOIN pg_tables p ON p.schemaname = c.table_schema AND p.tablename = c.table_name"
        " WHERE c.table_schema = 'salescall' AND c.column_name = 'client_id' ORDER BY 1")]


def _client_export(tx: Tx, client_id: UUID) -> tuple[bytes, dict[str, int]]:
    """その依頼者の行（依頼者を持つ表すべて）を、1行ずつの形で書き出す（AI用の控え。ほかの依頼者の行は入れない）。"""
    lines, counts = [], {}
    for t in _client_tables(tx):
        rows = tx.all(f'SELECT row_to_json(x)::text AS r FROM salescall."{t}" x WHERE x.client_id = %s ORDER BY 1', (client_id,))
        counts[t] = len(rows)
        lines += ['{"table": ' + json.dumps(t) + ', "row": ' + r["r"] + "}" for r in rows]
    schema = tx.one("SELECT max(name) AS n FROM public.salescall_migrations")["n"]
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


def _client_ai(app: Access, source: Tx, drive: Drive, tenant_id: UUID, client_id: UUID, *, key: bytes) -> Result:
    """その依頼者の AI用の控え（source＝表の持ち主の、時点を固定した取引。全部の依頼者を同じ時点で書き出す）。"""
    started = datetime.now().astimezone()
    file_name = f"salescall-{started:%Y%m%d-%H%M%S}.ai.enc"
    blob, counts, placed = None, None, []
    try:
        dest = drive.folder(client_folder(app, tenant_id, client_id), AI)
        data, counts = _client_export(source, client_id)
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
        error = str(e) if isinstance(e, BackupFailed) else f"思いがけない誤り（{type(e).__name__}）"
        _record(app, started, file_name if blob else None, blob, counts, placed, error, drive=isinstance(e, DriveMissing),
                part="client_ai", client_id=client_id)
        return Result(False, None, error)
    _record(app, started, file_name, blob, counts, placed, None, drive=False, part="client_ai", client_id=client_id)
    return Result(True, file_name, None)


def record_submission(tx: Tx, file_name: str, data: bytes) -> int:
    """依頼者へ渡した結果のファイルを、提出用の控えのために置く（出力＝CL-3 が、渡したときに依頼者の取引で1回呼ぶ）。
    PC の控えの処理がハードディスクへ写したら、中身は消える（Fly.io には残さない＝2026-10-17 ユーザー）。"""
    return tx.one("INSERT INTO salescall.submission_files (tenant_id, client_id, file_name, sha256, content)"
                  " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, %s) RETURNING submission_id",
                  (file_name, hashlib.sha256(data).hexdigest(), data))["submission_id"]


def copy_submissions(app: Access, drive: Drive, *, key: bytes) -> list[Result]:
    """まだ写していない提出用のファイルを、すべての依頼者について、ハードディスクへ写す。**見守り役が数分ごとに呼ぶ**（「提出用は
    Fly.io には保存しない」＝2026-10-17 ユーザー。毎日1回だと最長1日 Fly.io に残るため）。ハードディスクが外れていれば写さずに残し
    （消すと失う）、管理者に「つないでください」と知らせる。うまく写せた依頼者の分は返さない（失敗だけを返す）。"""
    waiting = app.in_system(SYSTEM, lambda tx: tx.all("SELECT tenant_id, client_id FROM salescall.submissions_waiting()"))
    failed = []
    for c in waiting:
        try:
            _copy_submissions(app, drive, c["tenant_id"], c["client_id"], key=key)
        except Exception as e:  # noqa: BLE001 記録して続ける（止めない）
            error = str(e) if isinstance(e, BackupFailed) else f"思いがけない誤り（{type(e).__name__}）"
            _record(app, datetime.now().astimezone(), None, None, None, [], error, drive=isinstance(e, DriveMissing),
                    part="submission", client_id=c["client_id"])
            failed.append(Result(False, None, error))
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


def prune(app: Access, drive: Drive, *, now: datetime | None = None) -> int:
    """残す日数を過ぎた控えを消す（名前に日時のある控えのファイルだけ。AI用・提出用＝システムの設定、録音＝契約者ごとの設定）。"""
    now = datetime.now().astimezone() if now is None else now
    days = app.in_system(SYSTEM, lambda tx: {AI: int(settings.system_value(tx, "backup.ai.keep_days_drive")),
                                              SUBMISSION: int(settings.system_value(tx, "backup.submission.keep_days_drive"))})
    removed = 0
    base = drive.root / BASE
    for folder in (base.iterdir() if base.is_dir() else ()):
        keep = dict(days)
        if folder.name != "system":
            who = app.in_system(SYSTEM, lambda tx: tx.one("SELECT salescall.backup_folder_owner(%s) AS t", (folder.name,))["t"])
            if who is not None:
                keep[RECORDING] = int(app.in_tenant(SYSTEM, who, lambda tx: settings.resolve(
                    tx, "recording.keep_days_drive", tenant_id=who, client_id=None, business_id=None)))
        for kind, n in keep.items():
            for f in ((folder / kind).iterdir() if (folder / kind).is_dir() else ()):
                m = _DATED.match(f.name)
                if m and datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S").replace(tzinfo=now.tzinfo) \
                        < now - timedelta(days=n):
                    f.unlink()
                    removed += 1
    return removed


def run_daily(app: Access, source_conninfo: str, restore_conninfo: str, drive: Drive, *, key: bytes) -> list[Result]:
    """毎日1回の控え（PC で見守り役が呼ぶ）：データベース全体（戻せるかを確かめる）→ 依頼者ごとの AI用 → 提出用の残りを写す（ふだんは
    見守り役が数分ごとに `copy_submissions` で写す）→ 古いものを消す。ハードディスクがつながっていなければ、何も置かずに管理者へ「つないでください」と知らせる。"""
    if not drive.attached():
        _record(app, datetime.now().astimezone(), None, None, None, [], "控え用のハードディスクがつながっていません", drive=True)
        return [Result(False, None, "控え用のハードディスクがつながっていません")]
    results = [take(app, source_conninfo, restore_conninfo, [drive.folder("system", AI)], key=key)]
    with snapshot(source_conninfo) as (_, source):
        clients = source.all("SELECT tenant_id, client_id FROM salescall.clients ORDER BY created_at")
        for c in clients:
            results.append(_client_ai(app, source, drive, c["tenant_id"], c["client_id"], key=key))
    results += copy_submissions(app, drive, key=key)
    prune(app, drive)
    return results
