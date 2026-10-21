"""PC ごとの通行証と、PC が動いているかの見張り（DESIGN.md 5-5。表の作り 0105）。持ち主 CL-2。

2026-10-21 ユーザー：「最近はREAなどでプログラムを読み取るのが簡単になっているようなので、読み取られないように対応する必要があります。
その上での最適解を考えてください」⇒ 契約者の PC には電話をつなぐ部分（PC の部品）だけを置き、データベースにつながない。PC は
サーバー（中央の見守り役）にだけつなぎ、サーバーは PC の通行証で、どの契約者のどの PC かを決める。

- **払い出しは `issue` の1か所**（機器のログインが済んだとき、ログインを確かめるシステムの処理か管理者が呼ぶ＝データベースが守る）。
  通行証（でたらめな長い文字）は返すのは1回だけで、どこにも保存しない（データベースには指紋＝`fingerprint` だけ）。同じ PC で
  ログインし直したら、前の通行証を止める（`replaces`）。
- **止めるのは `revoke` の1か所**（画面の「この PC のログインを外す」＝管理者か契約者・契約の終わり。止めた印は1回だけ）。
- **サーバーが通行証から PC を決めるのは `identify` の1か所**（止めた通行証は通らない）。動いている合図は `beat`（つないでいる
  回線は、その契約者の、使わなくなった印の無い回線だけが残る）。
- **PC が止まったら知らせる**（`watch_down`。中央の見守り役の契約者ごとの仕事）：合図が設定 `pc.down_after` より途切れた PC を、
  その契約者と管理者へ知らせる（1回の途切れに1回。止めない＝その PC の回線は借用の期限で門が通さない。PC はいつも動いている
  前提＝Intent.md 14-4）。
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID

from salescall.core import notify, settings
from salescall.core.access import Access, Actor, Tx

DOWN = "PC（電話をつなぐ部品）からの動いている合図が途切れました。その PC の回線では電話をかけていません。PC の電源・ネット・見守り役を確かめてください"


@dataclass(frozen=True)
class Pass:
    """払い出した通行証（返すのは1回だけ。保存しない）。"""
    tenant_id: UUID
    pc_id: UUID
    token: str

    def __repr__(self) -> str:   # 記録や画面の誤りの表示に、通行証を出さない
        return f"Pass(tenant_id={self.tenant_id!r}, pc_id={self.pc_id!r}, token=***)"


def fingerprint(token: str) -> str:
    """通行証の指紋（データベースに残すのは、これだけ）。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue(app: Access, actor: Actor, tenant_id: UUID, *, label: str, replaces: UUID | None = None) -> Pass:
    """その契約者の PC に、通行証を払い出す（機器のログインが済んだとき）。replaces＝同じ PC が前に持っていた PC の番号（止める）。"""
    token = secrets.token_urlsafe(32)

    def work(tx: Tx) -> UUID:
        pc = tx.one("INSERT INTO salescall.pc_agents (tenant_id, label, token_sha256) VALUES (%s, %s, %s) RETURNING pc_id",
                    (tenant_id, label.strip(), fingerprint(token)))["pc_id"]
        if replaces is not None:
            revoke(tx, replaces, missing_ok=True)
        return pc
    return Pass(tenant_id, app.in_tenant(actor, tenant_id, work), token)


def revoke(tx: Tx, pc_id: UUID, *, missing_ok: bool = False) -> bool:
    """その PC の通行証を止める（取引の契約者の PC だけ。もう止めてあれば何もしない）。止めたかを返す。無ければ LookupError。"""
    row = tx.one("UPDATE salescall.pc_agents SET revoked_at = now(), revoked_by = salescall.cur_actor()"
                 " WHERE pc_id = %s AND revoked_at IS NULL RETURNING pc_id", (pc_id,))
    if row is None and not missing_ok and tx.one("SELECT 1 AS x FROM salescall.pc_agents WHERE pc_id = %s", (pc_id,)) is None:
        raise LookupError(f"その PC はありません：{pc_id}")
    return row is not None


def pcs(tx: Tx) -> list[dict[str, Any]]:
    """取引の契約者の PC の一覧（画面の「この PC のログインを外す」。通行証の指紋は返さない）。"""
    return tx.all("SELECT pc_id, label, issued_at, revoked_at, last_seen_at, lines FROM salescall.pc_agents"
                  " ORDER BY issued_at, pc_id")


@dataclass(frozen=True)
class Who:
    tenant_id: UUID
    pc_id: UUID
    label: str


def identify(tx: Tx, token: str) -> Who | None:
    """通行証から、どの契約者のどの PC かを決める（サーバーの全体の取引だけ）。止めた・知らない通行証なら None。"""
    row = tx.one("SELECT pc_id, tenant_id, label FROM salescall.agent_by_token(%s)", (fingerprint(token),))
    return None if row is None else Who(row["tenant_id"], row["pc_id"], row["label"])


def beat(tx: Tx, pc_id: UUID, lines: list[UUID]) -> list[UUID] | None:
    """動いている合図（サーバーの全体の取引だけ）。残った回線（その契約者の、使っている回線だけ）を返す。止めた通行証なら None。"""
    return tx.one("SELECT salescall.agent_beat(%s, %s::uuid[]) AS kept", (pc_id, list(lines)))["kept"]


def watch_down(tx: Tx) -> list[UUID]:
    """取引の契約者の PC のうち、合図が途切れたものを、その契約者と管理者へ知らせる（1回の途切れに1回。合図が一度も無い PC は、
    払い出してから同じ長さで数える）。知らせた PC を返す。"""
    after = timedelta(seconds=int(settings.system_value(tx, "pc.down_after")))
    down = []
    for pc in tx.all("SELECT pc_id, label, coalesce(last_seen_at, issued_at) AS since FROM salescall.pc_agents"
                     " WHERE revoked_at IS NULL AND coalesce(last_seen_at, issued_at) < now() - %s ORDER BY pc_id", (after,)):
        if notify.emit(tx, "pc_down", key=f"pc_down:{pc['pc_id']}:{pc['since'].isoformat()}", summary=DOWN,
                       refs={"pc_id": pc["pc_id"], "pc": pc["label"], "since": pc["since"]}, admin=True, contractor=True):
            down.append(pc["pc_id"])
    return down
