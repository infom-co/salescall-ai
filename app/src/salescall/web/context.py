"""取引の始め方の1か所（docs/DASHBOARD.md 6-1）。

画面のどのルートも、データベースへは、ここが開く取引だけを通す（`Access` を直に呼ばない）。
- 依頼者の画面：住所に含めた依頼者で `in_client` を開く（画面が覚えている「いまの依頼者」には頼らない。ブラウザのタブを2つ開いても、
  片方の操作がもう片方の依頼者に効かない＝docs/DASHBOARD.md 8-3 の S-5）。別の契約者の依頼者 ID なら、データベースが行を見せない。
- 全体の画面：ログインした人の契約者で `in_tenant` を開く。
- 管理者の場所：管理者だけ `in_system` を開く（データベースも、管理者・システムの取引でなければ管理者あての行を見せない）。

ログインを確かめる仕組みは、まだ無い（利用者の表を CL-2 に頼んでいる＝docs/DASHBOARD.md 11章）。画面のサーバーは、ログインを確かめる
関数（`Identify`）を外から受け取り、無ければ動かない（確かめずに通す既定の形を作らない）。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar
from uuid import UUID

from salescall.core.access import Access, Actor, Tx

T = TypeVar("T")


@dataclass(frozen=True)
class Person:
    """ログインした管理者か契約者。どちらかの見分けは、データベースが最後に守る（画面は、出すボタンを決めるためだけに使う）。"""
    login_id: str
    is_admin: bool
    tenant_id: UUID
    tenant_name: str

    @property
    def actor(self) -> Actor:
        return Actor(self.login_id, "admin" if self.is_admin else "operator")


# ログインを確かめる関数：要求を受け取り、ログインした人（無ければ None）を返す。
Identify = Callable[[Any], "Person | None"]


class NotFound(Exception):
    """見つからない（「無い」と「見てはいけない」を同じ返事にする＝docs/DASHBOARD.md 8-3）。"""


class AdminOnly(Exception):
    """管理者だけの場所に、管理者でない人が来た。"""


@dataclass(frozen=True)
class Ctx:
    """1回の画面の要求の中の、取引の入口。"""
    access: Access
    person: Person

    def tenant(self, work: Callable[[Tx], T], *, time_limit: float | None = 10) -> T:
        """ログインした人の契約者の取引（全体の画面）。"""
        return self.access.in_tenant(self.person.actor, self.person.tenant_id, work, time_limit=time_limit)

    def client(self, client_id: UUID, work: Callable[[Tx], T], *, read_only: bool = False) -> T:
        """住所の依頼者の取引（依頼者の画面）。その契約者の依頼者でなければ NotFound（データベースが行を見せないことで分かる）。"""
        def guarded(tx: Tx) -> T:
            if tx.one("SELECT 1 FROM salescall.clients WHERE client_id = %s", (client_id,)) is None:
                raise NotFound()
            return work(tx)
        return self.access.in_client(self.person.actor, self.person.tenant_id, client_id, guarded, read_only=read_only)

    def system(self, work: Callable[[Tx], T]) -> T:
        """管理者の場所の取引（管理者だけ）。"""
        if not self.person.is_admin:
            raise AdminOnly()
        return self.access.in_system(self.person.actor, work)

    def clients(self) -> list[dict[str, Any]]:
        """その契約者の依頼者の一覧（選ぶ欄に出す。名前の順）。"""
        return self.tenant(lambda tx: tx.all("SELECT client_id, name FROM salescall.clients ORDER BY name, client_id"))
