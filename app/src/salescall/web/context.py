"""取引の始め方の1か所（docs/DASHBOARD.md 6-1）。

画面のどのルートも、データベースへは、ここが開く取引だけを通す（`Access` を直に呼ばない）。
- 依頼者の画面：住所に含めた依頼者で `in_client` を開く（画面が覚えている「いまの依頼者」には頼らない。ブラウザのタブを2つ開いても、
  片方の操作がもう片方の依頼者に効かない＝docs/DASHBOARD.md 8-3 の S-5）。別の契約者の依頼者 ID なら、データベースが行を見せない。
- 全体の画面：見ている契約者で `in_tenant` を開く。見ている契約者は、ふつうはログインした人の契約者。**管理者は、どの契約者にも
  切り替えて、見て・操作できる**（✅ 2026-11-18 管理者「管理者は全てを見れて操作できる」）。切り替えは `acting` の1か所で決める。
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
    """1回の画面の要求の中の、取引の入口。tenant_id＝見ている契約者（管理者が切り替えていれば、その契約者）。"""
    access: Access
    person: Person
    tenant_id: UUID
    tenant_name: str

    @property
    def acting_for_other(self) -> bool:
        """管理者が、自分のでない契約者を見ているか（画面の上に帯を出す＝取り違えを防ぐ）。"""
        return self.tenant_id != self.person.tenant_id

    def tenant(self, work: Callable[[Tx], T], *, time_limit: float | None = 10) -> T:
        """見ている契約者の取引（全体の画面）。"""
        return self.access.in_tenant(self.person.actor, self.tenant_id, work, time_limit=time_limit)

    def client(self, client_id: UUID, work: Callable[[Tx], T], *, read_only: bool = False) -> T:
        """住所の依頼者の取引（依頼者の画面）。その契約者の依頼者でなければ NotFound（データベースが行を見せないことで分かる）。"""
        def guarded(tx: Tx) -> T:
            if tx.one("SELECT 1 FROM salescall.clients WHERE client_id = %s", (client_id,)) is None:
                raise NotFound()
            return work(tx)
        return self.access.in_client(self.person.actor, self.tenant_id, client_id, guarded, read_only=read_only)

    def system(self, work: Callable[[Tx], T]) -> T:
        """管理者の場所の取引（管理者だけ）。"""
        if not self.person.is_admin:
            raise AdminOnly()
        return self.access.in_system(self.person.actor, work)

    def clients(self) -> list[dict[str, Any]]:
        """その契約者の依頼者の一覧（選ぶ欄に出す。名前の順）。"""
        return self.tenant(lambda tx: tx.all("SELECT client_id, name FROM salescall.clients ORDER BY name, client_id"))


def acting(access: Access, person: Person, chosen: UUID | None) -> Ctx:
    """見ている契約者を決める1か所。管理者が選んだ契約者（chosen）があり、その契約者が在れば、その契約者。それ以外（管理者でない・
    選んでいない・無い契約者）は、ログインした人の契約者。管理者でない人の選びは、黙って捨てる（契約者は自分の契約者だけ）。"""
    if person.is_admin and chosen is not None and chosen != person.tenant_id:
        row = access.in_tenant(person.actor, chosen, lambda tx: tx.one(
            "SELECT name FROM salescall.tenants WHERE tenant_id = %s", (chosen,)))
        if row is not None:
            return Ctx(access, person, chosen, row["name"])
    return Ctx(access, person, person.tenant_id, person.tenant_name)

