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


class RetryExhausted(Exception):
    """ほかの取引とのぶつかりが続き、決めた回数やり直しても終わらなかった。"""


# データベースが「約束に反する」として拒んだもの。⇒ Rejected にする
_REJECTIONS = (errors.IntegrityError, errors.InsufficientPrivilege, errors.RaiseException)
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


class Access:
    """読み書きの層。取引の始め方は3つだけ（依頼者の範囲・契約者の範囲・システム共通）。"""

    def __init__(self, conninfo: str, *, max_attempts: int = 30) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts は1以上です")
        self._conninfo = conninfo
        self._max_attempts = max_attempts

    def in_client(self, actor: Actor, tenant_id: UUID, client_id: UUID, work: Callable[[Tx], T]) -> T:
        """1つの依頼者のデータを読み書きする取引。依頼者のデータは、すべてこれで扱う（Q14）。"""
        if tenant_id is None or client_id is None:
            raise ValueError("契約者IDと依頼者IDは必ず要ります")
        return self._run(actor, tenant_id, client_id, work)

    def in_tenant(self, actor: Actor, tenant_id: UUID, work: Callable[[Tx], T]) -> T:
        """契約者のデータ（依頼者の一覧・回線・停止）を扱う取引。依頼者のデータの表は、どの行も見えない。"""
        if tenant_id is None:
            raise ValueError("契約者IDは必ず要ります")
        return self._run(actor, tenant_id, None, work)

    def in_system(self, actor: Actor, work: Callable[[Tx], T]) -> T:
        """システム共通の表（全体の停止・祝日の暦・管理者あての知らせ）だけを扱う取引。契約者・依頼者の行は見えない。
        （発信禁止の共通の台帳は、2026-10-16 から契約者ごと＝契約者の取引で扱う。0032）"""
        return self._run(actor, None, None, work)

    def _run(self, actor: Actor, tenant_id: UUID | None, client_id: UUID | None, work: Callable[[Tx], T]) -> T:
        for attempt in range(self._max_attempts):
            try:
                with psycopg.connect(self._conninfo, row_factory=dict_row) as conn:
                    conn.isolation_level = IsolationLevel.SERIALIZABLE
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
