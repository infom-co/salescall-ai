"""契約者ごとの、PC のデータベースの鍵の払い出し（DESIGN.md 11章。表の作り 0077。表の作りの中では「鍵」と書いている）。
持ち主 CL-2。

2026-10-18 ユーザー：「SalesCall AIのアカウントが払い出され、（恐らく）ストライプで入金が完了するか、アカウントを作成するかしたら、
鍵も払い出されるのがいいんじゃないでしょうか？」「鍵というより、SalesCall AIのログインとパスワードにより、データベースを
判断できないの？サブスク契約で、鍵など聞いたことないけど！」
⇒ **契約者が使うのは、SalesCall AI のログインIDとパスワードだけ**。この鍵は人が覚える・入れるものではなく、PC とデータベースの
あいだで使う機械用の鍵（ふつうのアプリが、ログインしたあと裏で持つ通行証と同じ）。

- 流れ：(1) 契約者が自分の PC で、見守り役の準備の命令を打つと、ブラウザで SalesCall AI のログインのページが開き、確かめのコードが出る
  （`supervisor/pc_login.py`） (2) 契約者は、そのページで好きな方法でログインする（ID とパスワード・Google・パスキー。ログインの
  ページとサーバー＝CL-3） (3) サーバーがログインを確かめ、`issue` を呼んで、その PC の鍵を PC へ渡す。PC は中にしまい、契約者には
  見せない (4) データベースは、つないだ鍵から契約者を決め、ほかの契約者の行・全体の取引・管理者としての動きを断る（0077・0079 の
  `cur_tenant`・`cur_actor_kind`） (5) 同じ PC でログインし直したら、前の鍵を止めて新しい鍵（`replaces`）。PC のログインを外す＝
  `revoke(pc=…)`、契約が終わった＝`revoke`（その契約者のすべての PC。つなげなくする。データは消さない）。
- **鍵は PC ごと**（0079。契約者ごとに1つだと、2台目の PC でログインすると1台目の鍵が使えなくなった）。PC は、パスワードに一切
  触れない（ブラウザでログインするので、Google・パスキーも使える）。
- ログインIDとパスワードそのものを PC に置いて、データベースが確かめる形にはしない（パスワードを PC に置くことになる）。
- 鍵（データベースのログインの役。名前は `role_name`）は、ここで作って **返すのは1回だけ**（どこにも保存しない。記録・知らせ・
  画面に出さない。データベースへも平文では送らない＝確かめの値 `_scram` だけを送る）。払い出した・使えなくした、の記録
  （`tenant_login_events`）は鍵を持たない。
- 払い出してよいのは、全体の取引の管理者か、ログインを確かめるシステムの処理だけ（記録の守り＝データベース）。記録を確定してから
  役を作る（役を作るのは取引の外なので、取引の中ではしない）。止めるときは、止めてから記録する。
- 作る・替える・使えなくするのは、払い出し専用の役（`salescall_provisioner`）の接続だけ（`access.Provisioner`）。この役が呼べるのは、
  名前と合言葉の形をデータベースが確かめる2つの関数だけ（0084。役を作る権限そのものは、ログインできない役だけが持つ）。サーバーにだけ置き、
  契約者の PC・見守り役には置かない。管理者の PC・画面の機械は、前と同じシステムの役でつなぐ。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from salescall.core import access as access_mod
from salescall.core.access import Access, Actor, Provisioner

ROLE_PREFIX = "salescall_t_"     # データベースが、この名前の役を契約者の鍵とみなす（0079 の login_tenant）
_PC = re.compile(r"^[0-9a-f]{8}$")  # PC の番号（払い出すときに作る）
_ITERATIONS = 4096               # 確かめの値を作る繰り返しの数（PostgreSQL の決まりの値）


@dataclass(frozen=True)
class Login:
    """払い出した PC の鍵（返すのは1回だけ。保存しない）。pc＝PC の番号（その PC のログインを外すときに使う）。conninfo＝その PC が
    データベースへつなぐ接続文字（PC の中にしまう）。"""
    tenant_id: UUID
    pc: str
    role: str
    password: str
    conninfo: str

    def __repr__(self) -> str:   # 記録や画面の誤りの表示に、鍵を出さない
        return f"Login(tenant_id={self.tenant_id!r}, pc={self.pc!r}, role={self.role!r}, password=***, conninfo=***)"


def role_name(tenant_id: UUID, pc: str) -> str:
    """その契約者の、その PC の鍵の役の名前（頭の契約者の番号から、データベースが契約者を決める＝0079 の login_tenant）。"""
    if not _PC.match(pc):
        raise ValueError(f"PC の番号の形が違います：{pc!r}")
    return f"{ROLE_PREFIX}{UUID(str(tenant_id))}_{pc}"


def issue(app: Access, actor: Actor, provisioner: Provisioner, tenant_id: UUID, *, base_conninfo: str, pc_label: str,
          replaces: str | None = None) -> Login:
    """その契約者の PC に、鍵を払い出す（PC がログインしたとき。ログインを確かめたサーバーが呼ぶ）。actor＝ログインを確かめる
    システムの処理か管理者（全体の取引）。base_conninfo＝PC からつなぐデータベースの場所（役と鍵は入れない）。pc_label＝契約者に見せる
    PC の名前。replaces＝同じ PC が前に持っていた PC の番号（ログインし直したとき。その鍵は止める）。"""
    pc, password = secrets.token_hex(4), secrets.token_urlsafe(32)
    role, verifier = role_name(tenant_id, pc), _scram(password)
    # 払い出してよい人か・その契約者があるかは、データベースが守る（無ければ Rejected）。記録を確定してから、役を1回だけ作る
    # （役を作るのは取引の外＝取引をやり直すと二重になるので、取引の中ではしない。作れなければ誤りを返し、PC はログインし直す）
    app.in_system(actor, lambda tx: _record(tx, tenant_id, "issued", pc=pc, pc_label=pc_label))
    provisioner.set_login(role, verifier)
    if replaces is not None and _PC.match(replaces):   # 同じ PC の前の鍵（この契約者の鍵だけ。ほかの契約者の番号なら当たらない）
        revoke(app, actor, provisioner, tenant_id, pc=replaces, missing_ok=True)
    return Login(tenant_id, pc, role, password, access_mod.login_conninfo(base_conninfo, role, password))


def revoke(app: Access, actor: Actor, provisioner: Provisioner, tenant_id: UUID, *, pc: str | None = None,
           missing_ok: bool = False) -> int:
    """鍵を使えなくする（つないだままの接続も切る。データは消さない）。pc＝その PC だけ（画面で「この PC のログインを外す」）／None＝
    その契約者のすべての PC（契約が終わったとき）。ある鍵の数を返す。止める鍵が無ければ LookupError（missing_ok なら 0。何も記録しない）。
    記録の取引の中で止める：先に記録を書き、外してよい人か（全体の取引の管理者かシステムの処理）をデータベースに確かめさせてから
    止める（⚠️ 前は止めてから記録したので、外してはいけない人が呼んでも、断られる前に鍵が止まった＝2026-10-19 の見直し）。
    止めるのはやり直しても同じ結果（もう止まった鍵も数える）なので、取引がやり直しになっても、記録できずに終わっても、呼び直せばよい。"""
    def work(tx: Any) -> int:
        _record(tx, tenant_id, "revoked", pc=pc, pc_label=None)
        n = provisioner.disable_logins(role_name(tenant_id, pc) if pc else f"{ROLE_PREFIX}{UUID(str(tenant_id))}_")
        if n == 0:
            raise _NothingToRevoke   # 記録を残さない（取引を戻す）
        return n
    try:
        return app.in_system(actor, work)
    except _NothingToRevoke:
        if missing_ok:
            return 0
        raise LookupError(f"止める鍵がありません：{tenant_id}{'' if pc is None else ' の PC ' + pc}") from None


class _NothingToRevoke(Exception):
    """止める鍵が無かった（revoke の中だけで使う）。"""


def _record(tx: Any, tenant_id: UUID, kind: str, *, pc: str | None, pc_label: str | None) -> None:
    tx.execute("INSERT INTO salescall.tenant_login_events (tenant_id, kind, pc, pc_label, done_by)"
               " VALUES (%s, %s, %s, %s, salescall.cur_actor())", (tenant_id, kind, pc, pc_label))


def _scram(password: str) -> str:
    """鍵の確かめの値（PostgreSQL の SCRAM-SHA-256 の形。RFC 5802・7677）。データベースは、この値で鍵を確かめる
    （鍵そのものは送らない・残らない）。"""
    salt = os.urandom(16)
    salted = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _ITERATIONS)
    client_key = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
    stored_key = hashlib.sha256(client_key).digest()
    server_key = hmac.new(salted, b"Server Key", hashlib.sha256).digest()
    b64 = lambda b: base64.b64encode(b).decode("ascii")   # noqa: E731
    return f"SCRAM-SHA-256${_ITERATIONS}:{b64(salt)}${b64(stored_key)}:{b64(server_key)}"
