"""契約者ごとのデータベースの合言葉の払い出し（DESIGN.md 11章。表の作り 0077）。持ち主 CL-2。

2026-10-18 ユーザー：「SalesCall AIのアカウントが払い出され、（恐らく）ストライプで入金が完了するか、アカウントを作成するかしたら、
合言葉も払い出されるのがいいんじゃないでしょうか？」

- **契約者ごとに、データベースのログインの役を1つ**（名前は `role_name`）。契約者の PC の見守り役は、この合言葉でつなぐ
  （`SALESCALL_DSN`）。データベースが、つないだ合言葉から契約者を決め、ほかの契約者の行・全体の取引・管理者としての動きを断る
  （0077 の `cur_tenant`・`cur_actor_kind`）。管理者の PC・画面の機械は、前と同じシステムの役でつなぐ。
- **払い出すのは `issue` の1か所**：アカウントを作ったとき・入金が済んだとき（画面とお金の受け取り＝CL-3 が呼ぶ）。同じ契約者に
  もう一度呼べば、合言葉を替える（前の合言葉は使えなくなる）。契約が終わったら `revoke`（つなげなくする。データは消さない）。
- 合言葉は、ここで作って **返すのは1回だけ**（どこにも保存しない。記録・知らせ・画面の履歴に出さない。データベースへも平文では
  送らない＝確かめの値 `_scram` だけを送る）。払い出した・使えなくした、の記録（`tenant_login_events`）は合言葉を持たない。
- 払い出してよいのは、全体の取引の管理者か、アカウントを作るシステムの処理だけ（記録の守り＝データベース）。記録が通ってから、
  役を作る・替える（同じ取引の中。取引をやり直しても、同じ確かめの値なので結果は同じ）。
- 作る・替える・使えなくするのは、払い出し専用の役（`salescall_provisioner`）の接続だけ（`access.Provisioner`）。契約者の PC・
  見守り役には置かない。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from salescall.core import access as access_mod
from salescall.core.access import Access, Actor, Provisioner

ROLE_PREFIX = "salescall_t_"     # データベースが、この名前の役を契約者の合言葉とみなす（0077 の login_tenant）
_ITERATIONS = 4096               # 確かめの値を作る繰り返しの数（PostgreSQL の決まりの値）


@dataclass(frozen=True)
class Login:
    """払い出した合言葉（返すのは1回だけ。保存しない）。conninfo＝契約者の PC の SALESCALL_DSN に入れる接続文字。"""
    tenant_id: UUID
    role: str
    password: str
    conninfo: str

    def __repr__(self) -> str:   # 記録や画面の誤りの表示に、合言葉を出さない
        return f"Login(tenant_id={self.tenant_id!r}, role={self.role!r}, password=***, conninfo=***)"


def role_name(tenant_id: UUID) -> str:
    """その契約者の合言葉の役の名前。"""
    return f"{ROLE_PREFIX}{UUID(str(tenant_id))}"


def issue(app: Access, actor: Actor, provisioner: Provisioner, tenant_id: UUID, *, base_conninfo: str) -> Login:
    """その契約者の合言葉を払い出す（初めて・替える）。actor＝管理者か、アカウントを作るシステムの処理（全体の取引）。
    base_conninfo＝契約者の PC からつなぐデータベースの場所（役と合言葉は入れない）。"""
    role, password = role_name(tenant_id), secrets.token_urlsafe(32)
    verifier = _scram(password)
    def work(tx: Any) -> None:
        _record(tx, tenant_id, "issued")          # 払い出してよい人か・その契約者があるかは、データベースが守る（無ければ Rejected）
        provisioner.set_login(role, verifier)     # 記録が通ってから（同じ確かめの値なので、取引をやり直しても同じ結果）
    app.in_system(actor, work)
    return Login(tenant_id, role, password, access_mod.login_conninfo(base_conninfo, role, password))


def revoke(app: Access, actor: Actor, provisioner: Provisioner, tenant_id: UUID) -> None:
    """その契約者の合言葉を使えなくする（契約が終わったとき。データは消さない）。払い出していなければ LookupError（何も記録しない）。"""
    def work(tx: Any) -> None:
        _record(tx, tenant_id, "revoked")
        if not provisioner.disable_login(role_name(tenant_id)):
            raise LookupError(f"その契約者の合言葉は、払い出していません：{tenant_id}")
    app.in_system(actor, work)


def _record(tx: Any, tenant_id: UUID, kind: str) -> None:
    tx.execute("INSERT INTO salescall.tenant_login_events (tenant_id, kind, done_by) VALUES (%s, %s, salescall.cur_actor())",
               (tenant_id, kind))


def _scram(password: str) -> str:
    """合言葉の確かめの値（PostgreSQL の SCRAM-SHA-256 の形。RFC 5802・7677）。データベースは、この値で合言葉を確かめる
    （合言葉そのものは送らない・残らない）。"""
    salt = os.urandom(16)
    salted = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _ITERATIONS)
    client_key = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
    stored_key = hashlib.sha256(client_key).digest()
    server_key = hmac.new(salted, b"Server Key", hashlib.sha256).digest()
    b64 = lambda b: base64.b64encode(b).decode("ascii")   # noqa: E731
    return f"SCRAM-SHA-256${_ITERATIONS}:{b64(salt)}${b64(stored_key)}:{b64(server_key)}"
