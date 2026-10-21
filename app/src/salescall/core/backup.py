"""データベースの控え（Intent.md 14-1。表の作り 0052）。持ち主 CL-2。**サーバーの側**（いつ取るか・中身の書き出し・記録・見張り）。

Intent.md 14-1：「毎日自動で取り、Fly.io とは別の場所にも置き、戻せることを試験で確かめ、取れなかったら知らせる（発信禁止の台帳を
失うと、断ったお客様にまたかけるため）」。CL-1 の条件：控えも暗号化する。
- **サーバーと PC の分け方**（2026-10-21 ユーザー承認。DESIGN.md 5-5）：サーバー（中央の見守り役）は中身を書き出し、その契約者の PC の
  部品（`Writer`）へ送る。PC が自分の鍵で暗号化して、PC に接続したハードディスクに書き、読み戻して元と同じかを確かめて返す。
  **サーバーは鍵を持たない**（鍵は、その PC と、その持ち主の Proton Pass の写しだけ＝2026-10-17 ユーザー 案A）。
  サブスク契約までは、PC に接続したハードディスクに置く（2026-10-17 ユーザー：「クラウドだけでなく、暫定的にPCに接続のハードディスクに
  保存してください。サブスク契約まで」）。クラウド（Fly.io）の側は、データベースの置き場を Fly.io が毎日自動で写す。
  - 契約者の控え（`run_contractor`）：その契約者の依頼者ごとの AI用（1日1回）・提出用（数分ごと）を、その契約者の PC へ。読むのは、
    その契約者の取引から見える行だけ。**控えは契約者ごと**（2026-10-18 ユーザー：「費用の見張りと控えは、契約者ごとですよ」）。
  - データベース全体の控え（`run_whole`・`take`）：すべての契約者のデータが入るので、管理者の PC へだけ。サーバーが、確かめ用の空の
    データベースへ戻して、表ごとの行の数が同じか（戻せるか）を確かめてから送る。
- 記録（backup_runs）：うまくいっても、いかなくても1回ずつ残す（契約者の控えは、その契約者の記録）。いかなければ、止めずに CL-2 へ
  修正依頼を出し、管理者（契約者の控えなら、その契約者にも）に知らせる。直すのが PC の前の作業（ハードディスクをつなぐ・鍵を入れる）
  なら、その PC の持ち主へそれを知らせる（PC の部品が `RemoteError.ask` で返す）。
- 毎日1回（`due`）：今日（日本時間）の、戻せることまで確かめた控えが無ければ取る。
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

from salescall.core import fix_requests, notify, settings
from salescall.core.access import Access, Actor, Tx, snapshot, tool_connection
from salescall.link.protocol import JST, RemoteError

SCHEMAS = ("salescall", "public")   # 数える表の置き場（public には表の作りの記録がある）
SYSTEM = Actor("データベースの控え", "system")


class Writer(Protocol):
    """控えを書く先（その PC の部品。中央の見守り役のつなぎ口が、つないでいる PC へ頼む）。誤りは RemoteError（ask があれば PC の前の作業）。"""

    def write(self, folder: list[str], file_name: str, data: bytes, sha256: str) -> tuple[str, int]:
        """暗号化してハードディスクに書き、読み戻して確かめる。暗号化したファイルの（指紋, 大きさ）を返す。"""
        ...

    def prune(self, folders: dict[str, Any]) -> int: ...


@dataclass(frozen=True)
class Result:
    ok: bool
    file_name: str | None
    error: str | None


class BackupFailed(Exception):
    """控えが取れなかった・置けなかった・戻せなかった（中身は、鍵・接続文字列・お客様の情報を含まない短い説明）。"""


def _ask(e: BaseException) -> str | None:
    return e.ask if isinstance(e, RemoteError) else None


def _counts(tx: Tx) -> dict[str, int]:
    tables = tx.all("SELECT schemaname || '.' || tablename AS t FROM pg_tables WHERE schemaname = ANY(%s) ORDER BY 1",
                    (list(SCHEMAS),))
    return {r["t"]: tx.one(f'SELECT count(*) AS n FROM {_quoted(r["t"])}')["n"] for r in tables}


def _quoted(qualified: str) -> str:
    schema, table = qualified.split(".", 1)
    return f'"{schema}"."{table}"'


def _name(started: datetime, rest: str) -> str:
    """控えのファイルの名前（日本時間の日時つき。サーバーの置き場の時間帯に関係なく同じ＝PC の片付けが名前の日時で決める）。"""
    return f"salescall-{started.astimezone(JST):%Y%m%d-%H%M%S}{rest}"


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
    """記録に残す誤りの説明（控えの部品・PC の部品が書いた説明だけ。思いがけない誤りは種類の名前だけ＝中身を残さない）。"""
    return str(e) if isinstance(e, (BackupFailed, RemoteError)) else f"思いがけない誤り（{type(e).__name__}）"


def take(app: Access, source_conninfo: str, restore_conninfo: str, writer: Writer, *, now: datetime | None = None) -> Result:
    """データベース全体の控えを1回取り、戻せるかを確かめ、管理者の PC へ書かせ、記録する。うまくいかなければ、修正依頼と知らせを出して
    Result(ok=False)。source_conninfo＝表の持ち主の接続（すべての行を読む）。restore_conninfo＝確かめ用の空のデータベース（毎回、中身を
    消して戻す）。writer＝管理者の PC の部品。"""
    started = datetime.now().astimezone() if now is None else now
    file_name = _name(started, ".dump.enc")
    counts: dict[str, int] | None = None
    written: tuple[str, int] | None = None
    try:
        with tempfile.TemporaryDirectory(prefix="salescall-backup-") as work:
            dump = Path(work) / "db.dump"
            with snapshot(source_conninfo) as (snap, tx):
                counts = _counts(tx)
                source_identity = _identity(tx)
                _run(["pg_dump", "--format=custom", f"--snapshot={snap}", "--no-owner", "--no-acl", "-f", str(dump)],
                     source_conninfo, "書き出し（pg_dump）")
            _verify(dump, restore_conninfo, counts, source_identity=source_identity)   # 送る前に、戻せることを確かめる
            data = dump.read_bytes()
        written = writer.write([WHOLE, AI], file_name, data, hashlib.sha256(data).hexdigest())
    except Exception as e:  # noqa: BLE001 ここで受けて記録する（止めない）
        error = _why(e)
        _record(app, started, None, None, counts, error, person=_ask(e))
        return Result(False, None, error)
    _record(app, started, file_name, written, counts, None, person=None)
    return Result(True, file_name, None)


def _verify(dump: Path, restore_conninfo: str, counts: dict[str, int], *, source_identity: tuple[Any, ...]) -> None:
    """書き出したものを、確かめ用のデータベースに戻して、行の数を比べる（戻せるか）。PC に書いた控えが、この中身と同じかは、PC が
    読み戻して確かめる（指紋）。"""
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
         "prune": "古い控えの片付け"}
_DAILY = ("system", "client_ai", "prune")   # 1日1回でよい種類（今日うまくいったら、もうしない）
RETRY_AFTER = timedelta(hours=1)            # 失敗したら、同じ種類（・同じ契約者・同じ依頼者）は1時間あけてやり直す


def _in(app: Access, tenant_id: UUID | None, client_id: UUID | None, work: Any) -> Any:
    """その記録の範囲の取引（データベース全体＝全体の取引・契約者の控え＝その契約者の取引・依頼者の控え＝その依頼者の取引。0064）。"""
    if client_id is not None:
        return app.in_client(SYSTEM, tenant_id, client_id, work)
    if tenant_id is not None:
        return app.in_tenant(SYSTEM, tenant_id, work)
    return app.in_system(SYSTEM, work)


def _record(app: Access, started: datetime, file_name: str | None, written: tuple[str, int] | None,
            counts: dict[str, int] | None, error: str | None, *, person: str | None, part: str = "system",
            tenant_id: UUID | None = None, client_id: UUID | None = None) -> None:
    """1回ずつ記録する（やり直しは `_may_try` が1時間あけるので、失敗の行は種類・契約者・依頼者ごとに1日24行まで）。
    失敗の知らせのあて先（決めるのはここだけ）：
    - 直すのが PC の前の作業（person＝ハードディスクをつなぐ・鍵を入れる。PC の部品が返す）なら、その PC の持ち主へそれを知らせる
      （契約者の控え＝その契約者・データベース全体の控え＝管理者）。written＝PC が書いた暗号化したファイルの（指紋, 大きさ）。
    - ほかは種類ごとに CL-2 へ修正依頼（同じ不具合は、直したと報告するまで1つ）を出し、管理者へ知らせる（契約者の控えなら、
      その契約者にも。プログラムの誤りは直せるのが管理者だけ＝Intent.md 1-4）。"""
    def work(tx: Tx) -> None:
        tx.execute("INSERT INTO salescall.backup_runs (started_at, file_name, sha256, size_bytes, table_counts, destinations,"
                   " restore_ok, error, kind, tenant_id, client_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                   (started, file_name, None if written is None else written[0], None if written is None else written[1],
                    None if counts is None else json.dumps(counts, sort_keys=True),
                    [] if written is None else [PLACE], error is None, error, part, tenant_id, client_id))
        if error is not None:
            day = tx.one("SELECT salescall.jst(now())::date AS d")["d"]
            if person is not None:   # PC の前の作業（プログラムの直しではない）
                summary = person
            else:
                fix_requests.request(tx, "CL-2", "backup_failed", key=f"backup_failed:{part}",
                                     summary=f"{PARTS[part]}：{error}", refs={"client_id": client_id})
                summary = f"{PARTS[part]}が取れませんでした（CL-2 に修正依頼を出しました）。"
            mine = tenant_id is not None
            # 知らせの重なりは、日・作業か種類・依頼者ごと（依頼者を入れないと、同じ日の2つ目の依頼者の失敗が黙って落ちた＝2026-10-18 の見直し）
            what = "person:" + person if person else part
            notify.emit(tx, "backup_failed", key=f"backup_failed:{day}:{what}:{client_id or '-'}", summary=summary,
                        refs={"error": error, "client_id": client_id},
                        admin=not mine or person is None, contractor=mine)
    _in(app, tenant_id, client_id, work)


def _done_today(tx: Tx, part: str, tenant_id: UUID | None, client_id: UUID | None) -> bool:
    """その種類（・その契約者・その依頼者）が、今日（日本時間）うまくいったか（**判断はこの1か所**。`_may_try` と `due` が読む）。"""
    return tx.one(
        "SELECT EXISTS (SELECT 1 FROM salescall.backup_runs WHERE restore_ok AND kind = %(part)s"
        "               AND tenant_id IS NOT DISTINCT FROM %(t)s AND client_id IS NOT DISTINCT FROM %(c)s"
        "               AND salescall.jst(started_at)::date = salescall.jst(now())::date) AS done",
        {"part": part, "t": tenant_id, "c": client_id})["done"]


def _may_try(app: Access, part: str, tenant_id: UUID | None = None, client_id: UUID | None = None) -> bool:
    """その種類（・その契約者・その依頼者）を、いまやってよいか（**判断はこの1か所**）：1日1回でよい種類は今日うまくいっていれば、
    しない。失敗したあとは1時間あける（見守り役が数分ごとに呼んでも、重い書き出しを繰り返さない・控えを重ねて取らない）。"""
    def look(tx: Tx) -> bool:
        if part in _DAILY and _done_today(tx, part, tenant_id, client_id):
            return False
        return tx.one(
            "SELECT NOT EXISTS (SELECT 1 FROM salescall.backup_runs WHERE NOT restore_ok AND kind = %(part)s"
            "                   AND tenant_id IS NOT DISTINCT FROM %(t)s AND client_id IS NOT DISTINCT FROM %(c)s"
            "                   AND started_at > now() - %(after)s) AS ok",
            {"part": part, "t": tenant_id, "c": client_id, "after": RETRY_AFTER})["ok"]
    return _in(app, tenant_id, None, look)


def due(app: Access) -> bool:
    """今日（日本時間）の、戻せることまで確かめたデータベース全体の控えが、まだ無いか（画面・見守り役が見る）。"""
    return not app.in_system(SYSTEM, lambda tx: _done_today(tx, "system", None, None))


def latest(app: Access) -> dict[str, Any] | None:
    """いちばん新しい記録（管理者の画面が、控えが取れているかを見る）。"""
    return app.in_system(SYSTEM, lambda tx: tx.one(
        "SELECT started_at, kind, tenant_id, client_id, file_name, size_bytes, destinations, restore_ok, error"
        " FROM salescall.backup_runs ORDER BY run_id DESC LIMIT 1"))


# ═════════ 控えが取れているかの見張り（取る側の設定が無くても気づく。見直し8。DESIGN.md 3-10） ═════════
# 控えは、その契約者の PC（控え用のハードディスクと鍵のあるもの）がつながっているときだけ書ける。PC が止まっている・設定が無いと、
# 失敗の記録も知らせも出ないまま、控えが1つも無くなる（Intent.md 14-1「取れなかったら知らせる」と食い違い）。⇒ 取れたかを、取る仕事と
# 別に、記録（backup_runs）だけから見る。見るのは中央の見守り役で、すべての契約者の分（どの PC が止まっていても見落とさない＝5-5）。
OVERDUE = timedelta(hours=26)   # 1日1回の控えが「取れていない」とみなす長さ（1日に、取る時刻のずれの余裕を足した長さ）
OVERDUE_CLIENT = ("依頼者ごとの AI用の控えが1日以上取れていません。この契約者の PC（PC の部品）が動いていて、控え用のハードディスクと"
                  "鍵の設定（SALESCALL_BACKUP_DRIVE・SALESCALL_BACKUP_KEY）が入っているかを確かめてください（手順：docs/PC_SETUP.md）。")
OVERDUE_WHOLE = ("データベース全体の控えが1日以上取れていません。サーバーの設定（SALESCALL_OWNER_DSN・SALESCALL_RESTORE_DSN・"
                 "SALESCALL_WHOLE_BACKUP_TENANT）と、管理者の PC（PC の部品・控え用のハードディスクと鍵）を確かめてください"
                 "（手順：docs/PC_SETUP.md）。")


def _overdue(tx: Tx, part: str, since: Any) -> bool:
    """その種類の、戻せることまで確かめた控えが、OVERDUE のあいだ1つも無いか（since＝見張りを始めてよい時刻。依頼者を作った
    ばかり・初めて動かしたばかりは数えない）。どの依頼者の分かは取引で決まる（依頼者の取引からは、その依頼者の記録だけが見える。0064）。"""
    return tx.one(
        "SELECT %(since)s < now() - %(late)s AND NOT EXISTS (SELECT 1 FROM salescall.backup_runs WHERE restore_ok"
        "   AND kind = %(part)s AND started_at > now() - %(late)s) AS late",
        {"since": since, "late": OVERDUE, "part": part})["late"]


def watch_client(app: Access, tenant_id: UUID, client_id: UUID) -> bool:
    """その依頼者の AI用の控えが取れているか（見守り役が、設定に関係なく依頼者ごとに呼ぶ＝1つの依頼者の誤りで、ほかの依頼者を見落と
    さない）。取れていなければ、その依頼者の取引で、契約者と管理者に1日1回知らせる（止めない）。知らせたかを返す。"""
    def work(tx: Tx) -> bool:
        since = tx.one("SELECT created_at FROM salescall.clients WHERE client_id = %s", (client_id,))["created_at"]
        if not _overdue(tx, "client_ai", since):
            return False
        day = tx.one("SELECT salescall.jst(now())::date AS d")["d"]
        notify.emit(tx, "backup_failed", key=f"backup_overdue:{day}:client_ai:{client_id}", summary=OVERDUE_CLIENT,
                    refs={"client_id": client_id}, admin=True, contractor=True)
        return True
    return _in(app, tenant_id, client_id, work)


def watch_whole(app: Access) -> bool:
    """データベース全体の控えが取れているか（中央の見守り役が、設定に関係なく呼ぶ。見張るのは、最初の契約者を作ってから）。
    取れていなければ、管理者に1日1回知らせる（止めない）。"""
    def work(tx: Tx) -> bool:
        since = tx.one("SELECT salescall.first_tenant_at() AS t")["t"]   # 全体の取引からは契約者の表を読めない（0102）
        if since is None or not _overdue(tx, "system", since):
            return False
        day = tx.one("SELECT salescall.jst(now())::date AS d")["d"]
        notify.emit(tx, "backup_failed", key=f"backup_overdue:{day}:system", summary=OVERDUE_WHOLE, admin=True, contractor=False)
        return True
    return app.in_system(SYSTEM, work)


# ═════════ 依頼者ごとの控え（2026-10-17 ユーザー決定。表の作り 0054） ═════════
# ハードディスクの中の形（書くのは PC の部品＝salescall/agent/backup.py）：<ハードディスク>\salescall-backup\<依頼者のフォルダ>\AI・
# submission・recording ／ データベース全体は …\system\AI。ユーザー：「E:\salescall-backup\client1\AI」「…\client1\submission」。
AI, SUBMISSION, RECORDING = "AI", "submission", "recording"
WHOLE = "system"                                                    # データベース全体の控えのフォルダ（依頼者のフォルダには使えない名前）
PLACE = "PC に接続したハードディスク"                                # 記録に残す置き場の名前（Fly.io とは別の場所）
# 会社の種類の言葉（フォルダの名前には入れない。2026-10-17 ユーザー：「架空商事-本社」だけでいい）。文字の形をそろえた（NFKC）あとの形で書く
_COMPANY_KINDS = re.compile(
    r"(株式|有限|合同|合資|合名)会社|(一般|公益)(社団|財団)法人|特定非営利活動法人|NPO法人"
    r"|(医療|社会福祉|学校|宗教|弁護士|税理士|行政書士|司法書士|監査|社会保険労務士)法人"
    r"|\((株|有|同|資|名|社|財|医|福|学|宗)\)")
_BREAKS = re.compile(r'(\s|[\\/:*?"<>|\x00-\x1f])+')    # 区切り（空白・Windows で使えない文字）は「-」1つにする
_RESERVED = {"con", "prn", "aux", "nul", WHOLE} | {f"com{i}" for i in range(1, 10)} | {f"lpt{i}" for i in range(1, 10)}


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


def _client_ai(app: Access, writer: Writer, tenant_id: UUID, client_id: UUID) -> Result:
    """その依頼者の AI用の控え（その依頼者の書かない取引で、1つの時点の行を書き出し、その契約者の PC へ書かせる）。"""
    started = datetime.now().astimezone()
    file_name = _name(started, ".ai.enc")
    counts, written = None, None
    try:
        folder = client_folder(app, tenant_id, client_id)
        data, counts = app.in_client(SYSTEM, tenant_id, client_id, lambda tx: _client_export(tx, client_id), read_only=True)
        written = writer.write([folder, AI], file_name, data, hashlib.sha256(data).hexdigest())
    except Exception as e:  # noqa: BLE001 ここで受けて記録する（止めない）
        error = _why(e)
        _record(app, started, None, None, counts, error, person=_ask(e), part="client_ai", tenant_id=tenant_id,
                client_id=client_id)
        return Result(False, None, error)
    _record(app, started, file_name, written, counts, None, person=None, part="client_ai", tenant_id=tenant_id,
            client_id=client_id)
    return Result(True, file_name, None)


def record_submission(tx: Tx, file_name: str, data: bytes) -> int:
    """依頼者へ渡した結果のファイルを、提出用の控えのために置く（出力＝CL-3 が、渡したときに依頼者の取引で1回呼ぶ）。
    契約者の PC の控えの処理がハードディスクへ写したら、中身は消える（Fly.io には残さない＝2026-10-17 ユーザー）。"""
    return tx.one("INSERT INTO salescall.submission_files (tenant_id, client_id, file_name, sha256, content)"
                  " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, %s) RETURNING submission_id",
                  (file_name, hashlib.sha256(data).hexdigest(), data))["submission_id"]


def clients(app: Access, tenant_id: UUID) -> list[UUID]:
    """その契約者の依頼者（作った順。控えと中央の見守り役の契約者ごとの仕事が読む1か所）。"""
    return app.in_tenant(SYSTEM, tenant_id, lambda tx: [r["client_id"] for r in tx.all(
        "SELECT client_id FROM salescall.clients ORDER BY created_at, client_id")])


def copy_submissions(app: Access, writer: Writer, tenant_id: UUID) -> list[Result]:
    """その契約者の依頼者の、まだ写していない提出用のファイルを、その契約者の PC へ写す。**中央の見守り役が数分ごとに呼ぶ**（「提出用は
    Fly.io には保存しない」＝2026-10-17 ユーザー。毎日1回だと最長1日 Fly.io に残るため）。PC が書けなければ（ハードディスクが外れて
    いる など）写さずに残し（消すと失う）、その PC の持ち主へ知らせる。うまく写せた依頼者の分は返さない（失敗だけを返す）。"""
    failed = []
    def step(client_id: UUID | None, work: Any) -> None:
        try:
            work()
        except Exception as e:  # noqa: BLE001 記録して続ける（止めない）
            _record(app, datetime.now().astimezone(), None, None, None, _why(e), person=_ask(e),
                    part="submission", tenant_id=tenant_id, client_id=client_id)
            failed.append(Result(False, None, _why(e)))
    clients: list[UUID] = []
    if _may_try(app, "submission", tenant_id):
        step(None, lambda: clients.extend(clients(app, tenant_id)))
    for c in clients:
        if _may_try(app, "submission", tenant_id, c):
            step(c, lambda c=c: _copy_submissions(app, writer, tenant_id, c))
    return failed


def _copy_submissions(app: Access, writer: Writer, tenant_id: UUID, client_id: UUID) -> int:
    """まだ写していない提出用のファイルを、PC へ写す（PC が暗号化して書き、読み戻して確かめてから、Fly.io の中身を消す）。"""
    rows = app.in_client(SYSTEM, tenant_id, client_id, lambda tx: tx.all(
        "SELECT submission_id, delivered_at, file_name, sha256, content FROM salescall.submission_files"
        " WHERE copied_at IS NULL ORDER BY submission_id"))
    if not rows:
        return 0
    folder = client_folder(app, tenant_id, client_id)
    for r in rows:
        content = bytes(r["content"])
        if hashlib.sha256(content).hexdigest() != r["sha256"]:
            raise BackupFailed("提出用のファイルの中身が、置いたときと違います")
        name = _name(r["delivered_at"], f"-{r['submission_id']}-{_safe(r['file_name'])}.enc")
        writer.write([folder, SUBMISSION], name, content, r["sha256"])
        app.in_client(SYSTEM, tenant_id, client_id, lambda tx: tx.execute(
            "UPDATE salescall.submission_files SET content = NULL, copied_at = now(), copied_as = %s WHERE submission_id = %s",
            (name, r["submission_id"])))
    return len(rows)


def _safe(file_name: str) -> str:
    """提出用のファイルの名前を、控えのファイルの名前に入れられる形に（英数字と「._-」だけ。ほかは「_」）。"""
    return re.sub(r"[^0-9A-Za-z._-]", "_", file_name)[:80] or "file"


def recording_file_name(attempt_id: UUID, started: datetime, part: str) -> str:
    """ハードディスクに置く録音のファイルの名前（録音を写す処理＝電話接続・CL-1 が使う1か所）。日時と通話を名前に入れる
    （古い録音を消す処理が、日時と「測り終わるまで消さない」印を見るため）。part＝その通話の中の録音の名前（例 call）。"""
    if not re.fullmatch(r"[a-z0-9_]{1,30}", part):
        raise ValueError(f"録音の名前は英小文字・数字・_ で書きます（{part!r}）")
    return _name(started, f"-{attempt_id}-{part}.enc")


def prune_rules(app: Access, tenant_id: UUID | None) -> dict[str, Any]:
    """古い控えを消す決まり（PC の部品が、この決まりでハードディスクの中を片付ける）：フォルダごとに、種類ごとの残す日数と、消さない
    もの（戻せることまで確かめた AI用のファイルの名前＝一番新しいものを残す・測り終わるまで消さない通話の番号＝2026-10-17 ユーザー 案A）。
    tenant_id＝その契約者の依頼者のフォルダ（AI用・提出用＝システムの設定、録音＝その契約者の設定）。None＝データベース全体のフォルダ。"""
    def look(tx: Tx) -> dict[str, Any]:
        days = {AI: int(settings.system_value(tx, "backup.ai.keep_days_drive")),
                SUBMISSION: int(settings.system_value(tx, "backup.submission.keep_days_drive"))}
        if tenant_id is None:
            rows = tx.all("SELECT %s AS folder, file_name AS f FROM salescall.backup_runs WHERE restore_ok AND kind = 'system'"
                          " AND file_name IS NOT NULL", (WHOLE,))
            rules: dict[str, Any] = {WHOLE: {"days": {AI: days[AI]}, "verified": [], "held": []}}
        else:
            days[RECORDING] = int(settings.resolve(tx, "recording.keep_days_drive", tenant_id=tenant_id, client_id=None,
                                                   business_id=None))
            held = [str(r["h"]) for r in tx.all("SELECT salescall.recordings_held() AS h")]
            rules = {r["f"]: {"days": dict(days), "verified": [], "held": held}
                     for r in tx.all("SELECT folder_name AS f FROM salescall.client_backup_folders")}
            # 戻せることまで確かめた控え（フォルダとファイルの名前の組。ファイルの名前は時刻だけなので、依頼者が違えば同じ名前もある
            # ＝フォルダごとに見る。2026-10-18 の見直し）
            rows = tx.all("SELECT b.folder_name AS folder, r.file_name AS f FROM salescall.backup_runs r"
                          " JOIN salescall.client_backup_folders b ON b.client_id = r.client_id"
                          " WHERE r.restore_ok AND r.kind = 'client_ai' AND r.tenant_id = %s AND r.file_name IS NOT NULL",
                          (tenant_id,))
        for r in rows:
            if r["folder"] in rules:
                rules[r["folder"]]["verified"].append(r["f"])
        for rule in rules.values():   # PC が残すのは一番新しいもの1つだけ：それだけを渡す（全部を渡すと、日がたつほどやり取りが大きくなる）
            rule["verified"] = [max(rule["verified"])] if rule["verified"] else []
        return rules
    return _in(app, tenant_id, None, look)


def _prune_step(app: Access, writer: Writer, tenant_id: UUID | None) -> list[Result]:
    if not _may_try(app, "prune", tenant_id):
        return []
    started = datetime.now().astimezone()
    try:
        writer.prune(prune_rules(app, tenant_id))
    except Exception as e:  # noqa: BLE001 片付けが落ちても（ファイルが使用中など）、記録して続ける
        _record(app, started, None, None, None, _why(e), person=_ask(e), part="prune", tenant_id=tenant_id)
        return [Result(False, None, _why(e))]
    _record(app, started, None, None, None, None, person=None, part="prune", tenant_id=tenant_id)
    return []


# ═════════ 中央の見守り役が呼ぶ入口（2つ。2026-10-18 ユーザー：「費用の見張りと控えは、契約者ごとですよ」） ═════════
def run_contractor(app: Access, tenant_id: UUID, writer: Writer) -> list[Result]:
    """契約者の控え（中央の見守り役が、その契約者の PC がつながっているあいだ数分ごとに呼んでよい。やるかは種類ごとに `_may_try`）：
    その契約者の依頼者ごとの AI用（1日1回）→ 提出用を写す（数分ごと）→ その契約者のフォルダの古いものを消す（1日1回）。読むのは、
    その契約者の取引から見えるその契約者の行だけ。書くのは、その契約者の PC（その PC のハードディスク・その PC の鍵）。"""
    results = []
    try:
        clients = clients(app, tenant_id) if _may_try(app, "client_ai", tenant_id) else []
    except Exception as e:  # noqa: BLE001 依頼者の一覧が読めない：記録して続ける（止めない）
        _record(app, datetime.now().astimezone(), None, None, None, _why(e), person=None, part="client_ai", tenant_id=tenant_id)
        results.append(Result(False, None, _why(e)))
        clients = []
    for c in clients:
        if _may_try(app, "client_ai", tenant_id, c):
            results.append(_client_ai(app, writer, tenant_id, c))
    results += copy_submissions(app, writer, tenant_id)
    return results + _prune_step(app, writer, tenant_id)


def run_whole(app: Access, source_conninfo: str, restore_conninfo: str, writer: Writer) -> list[Result]:
    """データベース全体の控え（中央の見守り役が呼ぶ。1日1回、戻せるかまで確かめる）と、そのフォルダの古いものを消す。すべての
    契約者のデータが入るので、管理者の PC にだけ書く（writer＝管理者の PC の部品）。"""
    results = []
    if _may_try(app, "system"):
        results.append(take(app, source_conninfo, restore_conninfo, writer))
    return results + _prune_step(app, writer, None)
