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
- **データベースへのつなぎは、接続先ごとに1つの共有のつなぎ（`_pool`。この処理の中で1つ）から借りて返す**（2026-10-21：取引のたびに
  新しくつなぐと、すべての契約者の仕事・回線ごとの流れ・PC の合図が1つのサーバーに集まったとき、つなぐ手間とデータベースのつなぎの
  上限で止まる）。借りるときに、つなぎが生きているかを確かめる（データベースが立ち上げ直しても、壊れたつなぎを使わない）。取引の
  「誰が・どの契約者・どの依頼者か」は、その取引の中だけの値（`set_config(..., true)`）なので、返したつなぎに残らない。
  空きが無いときは `POOL_WAIT` 秒まで待ち、データベースに届かないときも同じ長さで「届かない」（`psycopg.OperationalError`）。
"""

from __future__ import annotations

import atexit
import random
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from typing import Any, TypeVar
from uuid import UUID

import psycopg
import psycopg.conninfo
from psycopg import IsolationLevel, errors
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool, PoolTimeout

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

    def stream(self, sql: str, params: tuple[Any, ...] | dict[str, Any] | None = None) -> Iterator[dict[str, Any]]:
        """行を1つずつ受け取る（大きな書き出しを、丸ごとメモリに持たない。控えの書き出しが使う）。"""
        try:
            yield from self._conn.cursor().stream(sql, params)
        except _CONFLICTS:
            raise
        except _REJECTIONS as e:
            diag = e.diag
            raise Rejected(e.sqlstate, diag.constraint_name, diag.message_primary or str(e)) from e

    def one(self, sql: str, params: tuple[Any, ...] | dict[str, Any] | None = None) -> dict[str, Any] | None:
        return self.execute(sql, params).fetchone()

    def all(self, sql: str, params: tuple[Any, ...] | dict[str, Any] | None = None) -> list[dict[str, Any]]:
        return self.execute(sql, params).fetchall()

    def today(self) -> date:
        """日本時間の今日（データベースの時計。日本時間の判断は salescall.jst の1か所）。"""
        return self.one("SELECT salescall.jst(now())::date AS d")["d"]

    @contextmanager
    def savepoint(self) -> Iterator[None]:
        """取引の中の一部分だけを取り消せる区切り（拒まれたら、その部分だけ取り消して続ける）。"""
        with self._conn.transaction():
            yield

    @contextmanager
    def as_tenant(self, tenant_id: UUID) -> Iterator[None]:
        """全体の取引（契約者の無い取引）の中で、しばらくのあいだ、その契約者の取引として読み書きする（「誰が」はそのまま。抜けたら全体の
        取引に戻す）。使うのは、管理者がシステムの初めの値を変えたとき、自分の値を持たない契約者ごとに1回ずつ出来事を渡す所だけ
        （core/health.number_set_everywhere。DESIGN.md 17-2 の E14）。契約者・依頼者の取引の中では使えない（ほかの契約者へ移れない）。"""
        cur = self.one("SELECT current_setting('salescall.tenant_id', true) AS t, current_setting('salescall.client_id', true) AS c")
        if (cur["t"] or "") != "" or (cur["c"] or "") != "":
            raise PermissionError("契約者の取引に切り替えられるのは、全体の取引の中だけです")
        self.execute("SELECT set_config('salescall.tenant_id', %s, true)", (str(tenant_id),))
        try:
            yield
        finally:
            self.execute("SELECT set_config('salescall.tenant_id', '', true)")


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


def tool_connection(conninfo: str) -> tuple[str, dict[str, str]]:
    """データベースの控え（core/backup.py）だけが使う：道具（pg_dump など）に渡す接続先（パスワードを外したもの）と、パスワードを
    入れた環境の追加分。パスワードを引数に入れると、その機械のプロセスの一覧から見えるため（2026-10-18 の見直し）。"""
    d = psycopg.conninfo.conninfo_to_dict(conninfo)
    password = d.pop("password", None)
    return psycopg.conninfo.make_conninfo(**d), ({"PGPASSWORD": str(password)} if password else {})


POOL_MAX = 20     # 1つの接続先に、この処理が同時に持つつなぎの上限（多い分は、空くのを待つ。取引は短い）
POOL_WAIT = 10.0  # つなぎの空きを待つ・データベースに届くのを待つ長さ（秒。過ぎたら「届かない」）
_POOLS: dict[str, ConnectionPool] = {}
_POOLS_LOCK = threading.Lock()


def _pool(conninfo: str) -> ConnectionPool:
    """その接続先の共有のつなぎ（この処理の中で1つ。初めて使うときに作る）。"""
    with _POOLS_LOCK:
        got = _POOLS.get(conninfo)
        if got is None:
            got = ConnectionPool(conninfo, min_size=0, max_size=POOL_MAX, timeout=POOL_WAIT, open=True,
                                 check=ConnectionPool.check_connection, name="salescall",
                                 reconnect_timeout=POOL_WAIT, kwargs={
                                     "row_factory": dict_row,
                                     "connect_timeout": int(POOL_WAIT),   # 届かない相手を、OS の長い待ち（数分）まで待たない
                                     # 黙って切れたつなぎ（相手の機械が落ちた・途中の機器が切った）に、数十秒で気づく
                                     "keepalives": 1, "keepalives_idle": 30, "keepalives_interval": 10, "keepalives_count": 3})
            _POOLS[conninfo] = got
        return got


@contextmanager
def _connection(conninfo: str) -> Iterator[psycopg.Connection]:
    """共有のつなぎを1本借りる。空き待ちの時間切れは、つなぎがすべて使われていれば混雑（PoolBusy）、そうでなければ届かない。"""
    pool = _pool(conninfo)
    try:
        conn = pool.getconn()
    except PoolTimeout as e:
        stats = pool.get_stats()
        if stats.get("pool_size", 0) >= POOL_MAX and stats.get("pool_available", 0) == 0:
            raise PoolBusy(f"共有のつなぎ {POOL_MAX} 本がすべて使われていて、{POOL_WAIT:g} 秒で空きませんでした") from e
        raise
    try:
        yield conn
    finally:
        pool.putconn(conn)


@atexit.register
def _close_pools() -> None:
    with _POOLS_LOCK:
        pools = list(_POOLS.values())
        _POOLS.clear()
    for got in pools:
        got.close(timeout=5)


class PoolBusy(psycopg.OperationalError):
    """データベースには届いているが、共有のつなぎがすべて使われていて、POOL_WAIT 秒待っても空かなかった（混雑）。"""


def unreachable(error: BaseException) -> bool:
    """データベースに届かない誤りか（つなぎに行けない・待ち過ぎ・つなぎが切れた。判断はこの1か所）。届かない誤りは、データベースに記録
    できないので、記録しに行って待たない（呼ぶ側）。つなぎの空き待ちの時間切れ（混雑＝`busy`）は届かないに入れない（⛔ 2026-10-22
    の見直し：同じに扱っていたので、つなぎが一瞬埋まっただけで、すべての回線の流れを止め、記録も残さなかった）。"""
    return isinstance(error, psycopg.OperationalError) and not isinstance(error, (_CONFLICTS, PoolBusy))


def conflict(error: BaseException) -> bool:
    """ほかの取引とぶつかった誤りか（取引を最初からやり直すもの。`Access._run` がやり直すのと同じ組）。取引の中で誤りを受け止めて続ける
    所（状態の欄の影＝core/health）は、これだけは受け止めずに返す（やり直しは `Access._run` の1か所）。"""
    return isinstance(error, _CONFLICTS)


def busy(error: BaseException) -> bool:
    """共有のつなぎの空き待ちの時間切れか（混雑。データベースには届いている。判断はこの1か所）。"""
    return isinstance(error, PoolBusy)


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

    @contextmanager
    def exclusive(self, key: str) -> Iterator[bool]:
        """データベースの鍵（この接続先につなぐ、どの処理・どの機械とも、同じ鍵は1つだけ）を、待たずに取りにいく（1か所）。取れたら
        True を渡し、抜けるときに放す。取れなければ False（ほかの処理が持っている）。取引の外の、やり直すと二重になること（電話を鳴らす
        など）を、処理をまたいで1つずつにするのに使う（中央の見守り役の入れ替えで新旧が少し重なっても、同時にしない）。持っているあいだ、
        共有のつなぎを1本使う（取引は開かない）。処理が落ちれば、つなぎが切れてデータベースが放す。"""
        with _connection(self._conninfo) as conn:
            conn.autocommit = True   # 鍵を持つあいだ、取引を開いたままにしない（ほかの取引をやり直させない）
            try:
                got = bool(conn.execute("SELECT pg_try_advisory_lock(hashtextextended(%s, 0)) AS g", (key,)).fetchone()["g"])
                try:
                    yield got
                finally:
                    if got:
                        conn.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (key,))
            finally:
                conn.autocommit = False

    def _run(self, actor: Actor, tenant_id: UUID | None, client_id: UUID | None, work: Callable[[Tx], T], *,
             read_only: bool = False) -> T:
        for attempt in range(self._max_attempts):
            try:
                with _connection(self._conninfo) as conn:
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
        raise RetryExhausted(f"ほかの取引とのぶつかりが {self._max_attempts} 回続きました")
