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
from datetime import datetime
from typing import Any, TypeVar
from uuid import UUID

from salescall.core.access import Access, Actor, Tx

T = TypeVar("T")


@dataclass(frozen=True)
class Person:
    """ログインした管理者か契約者。どちらかの見分けは、データベースが最後に守る（画面は、出すボタンを決めるためだけに使う）。
    name・person_id＝ログインの表（core の people）の、記録に残る名前とログインの番号。取引は、このログインで開く（データベースが範囲を
    確かめる＝salescall.check_scope。CL-2 4-433）。ログインを確かめるのは web/login の1か所。"""
    login_id: str
    is_admin: bool
    tenant_id: UUID
    tenant_name: str
    name: str = ""
    person_id: UUID | None = None

    @property
    def actor(self) -> Actor:
        return Actor(self.name or self.login_id, "admin" if self.is_admin else "operator", self.person_id)


# ログインを確かめる関数：要求を受け取り、ログインした人（無ければ None）を返す。
Identify = Callable[[Any], "Person | None"]


class NotFound(Exception):
    """見つからない（「無い」と「見てはいけない」を同じ返事にする＝docs/DASHBOARD.md 8-3）。"""


class AdminOnly(Exception):
    """管理者だけの場所に、管理者でない人が来た。"""


class ClientForgotten(Exception):
    """契約が終わり、サーバーからデータを外した依頼者の住所を開いた（core の contract_end。計画書 11-4）。お客様・設定・結果はサーバーに
    無いので、どの画面もこの1枚だけを出す（app.py の handle の1か所）。"""

    def __init__(self, name: str, ended_at: Any) -> None:
        super().__init__(name)
        self.name = name
        self.ended_at = ended_at


@dataclass(frozen=True)
class Ctx:
    """1回の画面の要求の中の、取引の入口。tenant_id＝見ている契約者（管理者が切り替えていれば、その契約者）。"""
    access: Access
    person: Person
    tenant_id: UUID
    tenant_name: str
    group: str | None = None   # 選んだ依頼者のグループ（グループの番号／NO_GROUP＝どのグループにも入っていない依頼者／None＝すべて）

    @property
    def acting_for_other(self) -> bool:
        """管理者が、自分のでない契約者を見ているか（画面の上に帯を出す＝取り違えを防ぐ）。"""
        return self.tenant_id != self.person.tenant_id

    def tenant(self, work: Callable[[Tx], T], *, time_limit: float | None = 10, read_only: bool = False) -> T:
        """見ている契約者の取引（全体の画面）。read_only＝読むだけの画面（書けない取引。CL-2 4-430 の 32）。"""
        if read_only:
            return self.access.in_tenant(self.person.actor, self.tenant_id, work, time_limit=time_limit, read_only=True)
        return _once(lambda w: self.access.in_tenant(self.person.actor, self.tenant_id, w, time_limit=time_limit), work)

    def client(self, client_id: UUID, work: Callable[[Tx], T], *, read_only: bool = False) -> T:
        """住所の依頼者の取引（依頼者の画面）。その契約者の依頼者でなければ NotFound（データベースが行を見せないことで分かる）。"""
        def guarded(tx: Tx) -> T:
            if tx.one("SELECT 1 FROM salescall.clients WHERE client_id = %s", (client_id,)) is None:
                raise NotFound()
            return work(tx)
        if read_only:
            return self.access.in_client(self.person.actor, self.tenant_id, client_id, guarded, read_only=True)
        return _once(lambda w: self.access.in_client(self.person.actor, self.tenant_id, client_id, w), guarded)

    def system(self, work: Callable[[Tx], T]) -> T:
        """管理者の場所の取引（管理者だけ）。"""
        if not self.person.is_admin:
            raise AdminOnly()
        return self.access.in_system(self.person.actor, work)

    def clients(self) -> list[dict[str, Any]]:
        """その契約者の依頼者の一覧（依頼者のタブと探す欄に出す。名前の順）。選んだグループ（`group`）があれば、そのグループの依頼者だけ
        （✅ 2026-11-18 管理者「依頼者が増えた場合は『グループ』を作れるといいね」。グループは core の registry.groups の1か所＝CL-2 4-436）。
        グループはタブの並べ方だけに使う（データの分離・権限には使わない）。無くなったグループを選んでいたら、すべてを並べる。"""
        rows = [r for r in self._all_clients() if not (r["ended"] and r["ended"].forgotten)]   # 外した依頼者はタブに出さない
        if self.group is None:
            return rows
        groups = self.groups()
        if self.group == NO_GROUP:
            inside = {c for g in groups for c in g["clients"]}
            return [r for r in rows if r["client_id"] not in inside]
        chosen = next((g for g in groups if str(g["group_id"]) == self.group), None)
        return rows if chosen is None else [r for r in rows if r["client_id"] in set(chosen["clients"])]

    def _all_clients(self) -> list[dict[str, Any]]:
        """その契約者の依頼者すべて（名前の順）。契約終了の依頼者には、core の contract_end.state の答え（ended）を添える。"""
        def read(tx: Tx) -> list[dict[str, Any]]:
            ended = _contract_ends(tx)
            return [{**r, "ended": ended.get(r["client_id"])}
                    for r in tx.all(f"SELECT {CLIENT_COLUMNS} FROM salescall.clients c ORDER BY c.name, c.client_id")]
        return self.tenant(read, read_only=True)

    def forgotten_clients(self) -> list[dict[str, Any]]:
        """契約が終わり、サーバーから外した依頼者（名前と住所だけ。タブには出さず、ホームに名前の1行で残す）。"""
        return [r for r in self._all_clients() if r["ended"] and r["ended"].forgotten]

    def groups(self) -> list[dict[str, Any]]:
        """その契約者の依頼者のグループ（並び順。core の registry.groups の1か所）。"""
        from salescall.core import registry
        return self.tenant(registry.groups, read_only=True)


# 依頼者を読む形の1か所（タブの一覧も、1つの依頼者も同じ列）。有効かどうかの判断はデータベースの salescall.client_active の1か所
# （門も同じものを見る。計画書 4章 A「登録・編集・無効化」）。無効の依頼者には、新しい電話をかけない。
CLIENT_COLUMNS = "c.client_id, c.name, coalesce(salescall.client_active(c.client_id), true) AS active"


def client(tx: Tx, client_id: UUID) -> dict[str, Any]:
    """依頼者の取引（または契約者の取引）で、住所の依頼者を読む（名前・有効か・無効なら最後に無効にした理由といつ）。
    見えなければ NotFound。"""
    c = tx.one(f"SELECT {CLIENT_COLUMNS}, s.reason AS state_reason, s.set_at AS state_at FROM salescall.clients c"
               " LEFT JOIN LATERAL (SELECT x.reason, x.set_at FROM salescall.client_states x WHERE x.client_id = c.client_id"
               "                    ORDER BY x.seq DESC LIMIT 1) s ON true WHERE c.client_id = %s", (client_id,))
    if c is None:
        raise NotFound()
    st = _contract_ends(tx, client_id).get(client_id)
    if st is not None and st.forgotten:
        raise ClientForgotten(c["name"], st.ended_at)
    c["ended"] = st                     # 契約終了の猶予のあいだ（取り消せる）。外したかの判断は core の contract_end.state の1か所
    # 猶予が過ぎたか（過ぎたのにまだ外していないときだけ、画面に「まだ外していない理由」＝state の blocked を出す。猶予のあいだの blocked は
    # 「猶予のあいだです」で、すぐ前の「それまでは取り消せます」と同じことなので出さない）
    c["grace_over"] = st is not None and st.forget_at is not None and st.forget_at <= datetime.now(st.forget_at.tzinfo)
    return c


def _contract_ends(tx: Tx, client_id: UUID | None = None) -> dict[UUID, Any]:
    """契約終了の依頼者（外したものも）と、その状態（core の contract_end の1か所を読むだけ。決め直さない）。client_id を渡せば、その依頼者だけ。"""
    from salescall.core import contract_end
    ids = contract_end.ended_clients(tx)
    return {c: contract_end.state(tx, c) for c in ids if client_id is None or c == client_id}


def businesses(tx: Tx) -> list[dict[str, Any]]:
    """依頼者の取引で、その依頼者の業務の一覧（業務のタブに出す。作った順）。業務のタブの並びは、ここ1か所で決める。"""
    return tx.all("SELECT business_id, name FROM salescall.businesses ORDER BY created_at, business_id")


def business(tx: Tx, business_id: UUID) -> dict[str, Any]:
    """依頼者の取引で、住所の業務を確かめて読む。その依頼者の業務でなければ NotFound（別の依頼者の業務は、依頼者の取引では見えない）。"""
    b = tx.one("SELECT business_id, name FROM salescall.businesses WHERE business_id = %s", (business_id,))
    if b is None:
        raise NotFound()
    return b


def _once(open_tx: Callable[[Callable[[Tx], T]], T], work: Callable[[Tx], T]) -> T:
    """変える取引の入口の1か所：受けている操作に、まだ使っていない二度押しの札があれば、その取引の最初で使う（web/actions の札の決まり）。
    取引が確定したら、同じ操作の2つ目の取引では使わない。取引が戻れば（断り・やり直し）、札も使っていないまま。"""
    from salescall.web import actions
    token = actions.pending_once()
    if token is None:
        return open_tx(work)

    def guarded(tx: Tx) -> T:
        actions.use_once(tx, token)
        return work(tx)
    out = open_tx(guarded)
    actions.spent_once()
    return out


def tenant_name(access: Access, actor: Actor, tenant_id: UUID) -> str:
    """契約者の名前（ログインを確かめたとき、上の帯に出す。取引を開くのは、この場所の1か所＝web/login は呼ぶだけ）。"""
    row = access.in_tenant(actor, tenant_id, lambda tx: tx.one("SELECT name FROM salescall.tenants WHERE tenant_id = %s", (tenant_id,)),
                           read_only=True)
    return row["name"] if row else ""


def security(access: Access, person_id: UUID) -> dict[str, Any]:
    """ログインと安全の画面の読み物（取引を開くのは、この場所の1か所）：その方のパスキー（名前・登録した時刻）と、結び付いた Google の
    アカウント（メール）。読むのは core の passkeys.active・google_login.linked（ログインの確かめの役で読む部品＝CL-2 4-456・4-457）。"""
    from salescall.core import google_login, passkeys, people
    def read(tx: Tx) -> dict[str, Any]:
        return {"passkeys": [{"passkey_id": k["passkey_id"], "label": k["label"], "added_at": k["added_at"]} for k in passkeys.active(tx, person_id)],
                "google": [g["email"] for g in google_login.linked(tx, person_id)]}
    return access.in_system(people.LOGIN_CHECKER, read)


NO_GROUP = "none"   # グループの選び：どのグループにも入っていない依頼者


def acting(access: Access, person: Person, chosen: UUID | None, group: str | None = None) -> Ctx:
    """見ている契約者を決める1か所。管理者が選んだ契約者（chosen）があり、その契約者が在れば、その契約者。それ以外（管理者でない・
    選んでいない・無い契約者）は、ログインした人の契約者。管理者でない人の選びは、黙って捨てる（契約者は自分の契約者だけ）。"""
    if person.is_admin and chosen is not None and chosen != person.tenant_id:
        row = access.in_tenant(person.actor, chosen, lambda tx: tx.one(
            "SELECT name FROM salescall.tenants WHERE tenant_id = %s", (chosen,)))
        if row is not None:
            return Ctx(access, person, chosen, row["name"], group)   # ほかの契約者のグループの番号なら、当たらずに「すべて」になる
    return Ctx(access, person, person.tenant_id, person.tenant_name, group)

