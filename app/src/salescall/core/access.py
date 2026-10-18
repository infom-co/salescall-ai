"""読み書きの層（DESIGN.md 11章・2章 Q14）。持ち主 CL-2。

データベースへの読み書きは、すべてこの層を通す（会話・画面・出力も。データベースの部品を読んでよいのは、
この部品と、表の作りを流す migrate だけ＝I-31。app/checks が見張る）。

- **取引は、どれも直列化の水準**（ほかの取引と1つずつ順に行ったのと同じ結果になる水準）で行い、
  ほかの取引とぶつかったら、最初からやり直す（DESIGN.md 5-2-1 の2）。やり直すのはこの1か所だけ。
  ⇒ 取引の中の処理（work）は、データベースの読み書きだけをすること（電話・会話AI・ファイルへの書き出しなど、
    やり直すと二重になることをしない）。
- **取引ごとに「誰が・どの契約者・どの依頼者か」をデータベースへ渡す。** データベースは、その行しか
  読ませない・書かせない・作らせない（行ごとに読める範囲を制限する機能）。渡す場所は、この部品の1か所だけ。
- データベースが拒んだ書き込み（一意の約束・組の参照・変えてはいけない記録・読める範囲の制限 など）は、
  `Rejected` にして返す。どの約束で拒まれたかは `constraint` に入る（門が不許可の理由に使う）。
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, TypeVar
from uuid import UUID

import psycopg
import psycopg.conninfo
from psycopg import IsolationLevel, errors
from psycopg.rows import dict_row

T = TypeVar("T")

ACTOR_KINDS = ("operator", "system", "admin")  # 契約者か、システムの処理か、管理者か。データベースの actor_kind と同じ


@dataclass(frozen=True)
class Actor:
    """誰が読み書きするか（利用者の識別名、またはシステムの処理の名前）。記録に残る。"""

    name: str
    kind: str

    def __post_init__(self) -> None:
        if not self.name or not self.name.strip():
            raise ValueError("誰が読み書きするか（name）が空です")
        if self.kind not in ACTOR_KINDS:
            raise ValueError(f"誰が、の種類は {ACTOR_KINDS} のどれかです：{self.kind!r}")


class Rejected(Exception):
    """データベースが書き込みを拒んだ（約束に反する・読める範囲の外・変えてはいけない記録）。"""

    def __init__(self, sqlstate: str | None, constraint: str | None, message: str) -> None:
        super().__init__(message)
        self.sqlstate = sqlstate
        self.constraint = constraint
        self.message = message


class LoginRefused(Exception):
    """データベースが、この接続の合言葉（PC の鍵）を断った（合言葉が違う・止められた・役が無い）。判断は `_login_refused` の1か所。
    契約者の PC の見守り役は、これを受けたらログインし直す（supervisor）。"""


# 合言葉を断られたときは、つなぐ段の誤りで、誤りの種類の番号が付かない（psycopg の OperationalError の文だけ）。データベースの
# 言葉は英語（C）の決まり（DESIGN.md 11章）：合言葉が違う（28P01）・つなげない役（28000）・役が無い（28000）。
_REFUSED_TEXTS = ("password authentication failed", "is not permitted to log in", "does not exist")


def _login_refused(e: Exception) -> bool:
    if not isinstance(e, psycopg.OperationalError) or getattr(e, "sqlstate", None) not in (None, "28P01", "28000"):
        return False
    text = str(e)
    return "password authentication failed" in text or ("role" in text and any(t in text for t in _REFUSED_TEXTS[1:]))


class RetryExhausted(Exception):
    """ほかの取引とのぶつかりが続き、決めた回数やり直しても終わらなかった。"""


# データベースが「約束に反する」として拒んだもの。⇒ Rejected にする
_REJECTIONS = (errors.IntegrityError, errors.InsufficientPrivilege, errors.RaiseException,
               errors.ReadOnlySqlTransaction)   # 書かない取引で書こうとした
# ほかの取引とぶつかったもの。⇒ 取引を最初からやり直す
_CONFLICTS = (errors.SerializationFailure, errors.DeadlockDetected)


class Tx:
    """1つの取引。取引の中の読み書きは、これだけを通す。"""

    def __init__(self, conn: psycopg.Connection[dict[str, Any]]) -> None:
        self._conn = conn

    def execute(self, sql: str, params: tuple[Any, ...] | dict[str, Any] | None = None) -> psycopg.Cursor[dict[str, Any]]:
        try:
            return self._conn.execute(sql, params)
        except _CONFLICTS:
            raise
        except _REJECTIONS as e:
            diag = e.diag
            raise Rejected(e.sqlstate, diag.constraint_name, diag.message_primary or str(e)) from e

    def one(self, sql: str, params: tuple[Any, ...] | dict[str, Any] | None = None) -> dict[str, Any] | None:
        return self.execute(sql, params).fetchone()

    def all(self, sql: str, params: tuple[Any, ...] | dict[str, Any] | None = None) -> list[dict[str, Any]]:
        return self.execute(sql, params).fetchall()

    @contextmanager
    def savepoint(self) -> Iterator[None]:
        """取引の中の一部分だけを取り消せる区切り（拒まれたら、その部分だけ取り消して続ける）。"""
        with self._conn.transaction():
            yield


@contextmanager
def snapshot(conninfo: str) -> Iterator[tuple[str, Tx]]:
    """データベースの控え（core/backup.py）だけが使う：書かない取引で、ある時点の見え方を固定し、その名前（pg_dump の --snapshot に
    渡す）と取引を返す。表の持ち主の接続で使う（行ごとの制限を受けずに、すべての行を数える）。取引は、控えを取り終えるまで開いておく。"""
    with psycopg.connect(conninfo, row_factory=dict_row) as conn:
        conn.isolation_level = IsolationLevel.REPEATABLE_READ
        conn.read_only = True
        with conn.transaction():
            row = conn.execute("SELECT pg_export_snapshot() AS s").fetchone()
            yield row["s"], Tx(conn)


def login_conninfo(base_conninfo: str, role: str, password: str) -> str:
    """契約者の合言葉の接続文字（core/logins.py だけが使う）：base の接続先（場所・データベース）に、その契約者の役と合言葉を入れる。"""
    d = psycopg.conninfo.conninfo_to_dict(base_conninfo)
    # 契約者の PC からの通信は、必ず暗号化し、相手のサーバーを確かめる（鍵と、お客様のデータを運ぶ。同じ機械の中のつなぎ方では効かない）
    d.update(user=role, password=password, sslmode="verify-full")
    return psycopg.conninfo.make_conninfo(**d)


class Provisioner:
    """契約者の PC の鍵（データベースのログインの役）を作る・替える・止める（core/logins.py だけが使う。払い出し専用の役
    salescall_provisioner の接続。DESIGN.md 11章）。この役が呼べるのは、データベースの2つの関数だけ（make_pc_login・stop_pc_logins。
    名前と合言葉の形を、データベースが確かめる＝0084）。合言葉は、平文でデータベースへ送らない（確かめの値だけを送る）。"""

    def __init__(self, conninfo: str) -> None:
        self._conninfo = conninfo

    def set_login(self, role: str, verifier: str) -> None:
        """役が無ければ作り（システムの役の権限を受け継ぐ）、あれば合言葉を替えて、つなげるようにする。"""
        with psycopg.connect(self._conninfo, autocommit=True) as conn:
            conn.execute("SELECT salescall.make_pc_login(%s, %s)", (role, verifier))

    def disable_logins(self, name_or_prefix: str) -> int:
        """名前がそれと同じか、それで始まる（頭の文字の末尾が「_」のとき）役を、つなげないようにし、つないだままの接続も切る。
        ある役の数を返す（もう止まっていても数える＝やり直せる）。"""
        with psycopg.connect(self._conninfo, autocommit=True) as conn:
            return int(conn.execute("SELECT salescall.stop_pc_logins(%s)", (name_or_prefix,)).fetchone()[0])


def tool_connection(conninfo: str) -> tuple[str, dict[str, str]]:
    """データベースの控え（core/backup.py）だけが使う：道具（pg_dump など）に渡す接続先（パスワードを外したもの）と、パスワードを
    入れた環境の追加分。パスワードを引数に入れると、PC のプロセスの一覧から見えるため（2026-10-18 の見直し）。"""
    d = psycopg.conninfo.conninfo_to_dict(conninfo)
    password = d.pop("password", None)
    return psycopg.conninfo.make_conninfo(**d), ({"PGPASSWORD": str(password)} if password else {})


class Access:
    """読み書きの層。取引の始め方は3つだけ（依頼者の範囲・契約者の範囲・システム共通）。"""

    def __init__(self, conninfo: str, *, max_attempts: int = 30) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts は1以上です")
        self._conninfo = conninfo
        self._max_attempts = max_attempts

    def in_client(self, actor: Actor, tenant_id: UUID, client_id: UUID, work: Callable[[Tx], T], *,
                  read_only: bool = False) -> T:
        """1つの依頼者のデータを読み書きする取引。依頼者のデータは、すべてこれで扱う（Q14）。
        read_only＝書かない取引（ある時点の見え方を固定して読むだけ。長く読んでも、ほかの取引をやり直させない。控えの書き出しが使う）。"""
        if tenant_id is None or client_id is None:
            raise ValueError("契約者IDと依頼者IDは必ず要ります")
        return self._run(actor, tenant_id, client_id, work, read_only=read_only)

    def in_tenant(self, actor: Actor, tenant_id: UUID, work: Callable[[Tx], T]) -> T:
        """契約者のデータ（依頼者の一覧・回線・停止）を扱う取引。依頼者のデータの表は、どの行も見えない。"""
        if tenant_id is None:
            raise ValueError("契約者IDは必ず要ります")
        return self._run(actor, tenant_id, None, work)

    def in_system(self, actor: Actor, work: Callable[[Tx], T]) -> T:
        """システム共通の表（全体の停止・祝日の暦・管理者あての知らせ）だけを扱う取引。契約者・依頼者の行は見えない。
        （発信禁止の共通の台帳は、2026-10-16 から契約者ごと＝契約者の取引で扱う。0032）"""
        return self._run(actor, None, None, work)

    def _run(self, actor: Actor, tenant_id: UUID | None, client_id: UUID | None, work: Callable[[Tx], T], *,
             read_only: bool = False) -> T:
        for attempt in range(self._max_attempts):
            try:
                with psycopg.connect(self._conninfo, row_factory=dict_row) as conn:
                    conn.isolation_level = IsolationLevel.REPEATABLE_READ if read_only else IsolationLevel.SERIALIZABLE
                    conn.read_only = read_only
                    with conn.transaction():
                        conn.execute(
                            "SELECT set_config('salescall.tenant_id', %s, true), set_config('salescall.client_id', %s, true),"
                            " set_config('salescall.actor', %s, true), set_config('salescall.actor_kind', %s, true)",
                            ("" if tenant_id is None else str(tenant_id), "" if client_id is None else str(client_id),
                             actor.name, actor.kind),
                        )
                        return work(Tx(conn))
            except _CONFLICTS:
                # ほかの取引とぶつかった ⇒ 少し待って最初からやり直す（待つ長さは少しずつ伸ばし、ばらつかせる）
                time.sleep(min(0.2, 0.002 * (2 ** min(attempt, 7))) * (0.5 + random.random()))
            except psycopg.OperationalError as e:   # ぶつかり（上）より後に見る（ぶつかりも、この種類の一つ）
                if _login_refused(e):
                    raise LoginRefused("データベースが、この接続の合言葉を断りました（PC の鍵が止められた・替えられた）") from None
                raise
        raise RetryExhausted(f"ほかの取引とのぶつかりが {self._max_attempts} 回続きました")
