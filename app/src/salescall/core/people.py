"""管理者・契約者のログイン（2026-11-19。CL-3 の連絡帳 4-1 の 1・4-1b の 14。表の作り 0251。DESIGN.md 11章・計画書 14-1「認証」）。

- ログイン（people）：管理者・契約者のログイン1つにつき1つ。作れるのは管理者だけ（`create`。全体の取引）。最初の管理者だけは、サーバーを用意する
  ときに、システムの処理がその契約者と一緒に作る（`first_admin`。管理者がまだ1人もいないときだけ＝データベースが守る）。
- 確かめ（`login`）：ログインIDとパスワード。パスワードは元に戻せない形（scrypt）でだけ残す。決めた回数続けて誤ると、決めた長さのあいだ断る。
  確かめた記録は残す（パスワードは残さない）。通れば、ログインの印（ランダムな文字）を1回だけ返し、データベースには指紋だけを残す。
- 画面は、要求ごとに印から管理者・契約者を読み（`by_session`）、`Person.actor` で取引を開く。取引の範囲は、データベースが、そのログインで確かめる
  （salescall.check_scope の1か所＝契約者は自分の契約者の取引だけ。access._run が必ず通す）。
- 管理者・契約者がパスワードを替えたら、前の印はすべて使えなくする（`set_password`）。使えなくしたログイン（契約が終わった など）は、印も確かめも通らない。
⏳ 2段階の確かめ（認証アプリ・パスキー）と Google のアカウントでのログインは、次の段で足す（計画書 14-1 の 2026-10-18 のご決定）。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

from salescall.core import settings
from salescall.core.access import Access, Actor, Refused, Tx

LOGIN_CHECKER = Actor("ログインの確かめ", "system")   # ログインを確かめる取引の「誰が」（全体の取引のシステムの処理）
_SCRYPT = (2 ** 14, 8, 1)                              # scrypt の強さ（n, r, p）


class PasswordRefused(Refused):
    """パスワードが決まりに合わない（何も書いていない）。"""


@dataclass(frozen=True)
class Person:
    """ログインした管理者か契約者。"""
    person_id: UUID
    tenant_id: UUID
    kind: str          # admin／operator
    name: str          # 記録に残る名前
    login_id: str

    @property
    def is_admin(self) -> bool:
        return self.kind == "admin"

    @property
    def actor(self) -> Actor:
        """この方の取引の「誰が」（データベースが、このログインで範囲を確かめる）。"""
        return Actor(self.name, self.kind, self.person_id)


@dataclass(frozen=True)
class Session:
    """ログインが通ったときの答え。token は、このときだけ返す（データベースには指紋だけ）。"""
    token: str
    person: Person


def _login_key(login_id: str) -> str:
    return login_id.strip().lower()


def _hash(password: str, salt: bytes | None = None) -> str:
    """パスワードの写し（元に戻せない形。作るのも比べるのも、ここだけ）。"""
    n, r, p = _SCRYPT
    salt = salt if salt is not None else secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, dklen=64)
    return f"scrypt${n}${r}${p}${salt.hex()}${digest.hex()}"


def _matches(password: str, stored: str) -> bool:
    _, n, r, p, salt, digest = stored.split("$")
    got = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p), dklen=64)
    return hmac.compare_digest(got.hex(), digest)


_DUMMY = _hash("ログインIDが無いときに、時間をそろえるための写し")


def _setting(tx: Tx, key: str) -> Any:
    return settings.resolve(tx, key, tenant_id=None, client_id=None, business_id=None)   # ログインの決まりは全体の値だけ


def _checked_password(tx: Tx, password: str) -> str:
    least = int(_setting(tx, "login.password_min_length"))
    if not isinstance(password, str) or len(password) < least:
        raise PasswordRefused(f"パスワードは{least}文字以上にしてください")
    return _hash(password)


def _put(tx: Tx, *, tenant_id: UUID, kind: str, name: str, login_id: str, password: str | None) -> UUID:
    """ログインを書く1か所（`create`・`first_admin`）。"""
    if not name.strip() or not _login_key(login_id):
        raise Refused("名前とログインIDを入れてください")
    pid = tx.one("INSERT INTO salescall.people (tenant_id, kind, name, login_id, created_by)"
                 " VALUES (%s, %s, %s, %s, salescall.cur_actor()) RETURNING person_id",
                 (tenant_id, kind, name.strip(), _login_key(login_id)))["person_id"]
    if password is not None:
        tx.execute("INSERT INTO salescall.person_passwords (person_id, hash, set_by) VALUES (%s, %s, salescall.cur_actor())",
                   (pid, _checked_password(tx, password)))
    return pid


def create(tx: Tx, *, tenant_id: UUID, kind: str, name: str, login_id: str, password: str | None = None) -> UUID:
    """ログインを作る（全体の取引の管理者だけ＝データベースが守る）。password が無ければ、あとで `set_password` で決める（それまでログインできない）。"""
    return _put(tx, tenant_id=tenant_id, kind=kind, name=name, login_id=login_id, password=password)


def first_admin(tx: Tx, *, tenant_name: str, name: str, login_id: str, password: str) -> tuple[UUID, UUID]:
    """サーバーを用意するときに、最初の管理者を、その契約者と一緒に作る（全体の取引のシステムの処理。管理者がまだ1人もいないときだけ＝
    データベースが守る。計画書 1-4「使い始めの契約者は、管理者自身」）。(契約者, ログイン) を返す。"""
    tid = uuid4()   # 全体の取引からは契約者の行が見えないので、番号はここで決める（作るのは管理者・システムの処理だけ＝0251 の tenants_created_whole）
    tx.execute("INSERT INTO salescall.tenants (tenant_id, name) VALUES (%s, %s)", (tid, tenant_name.strip()))
    return tid, _put(tx, tenant_id=tid, kind="admin", name=name, login_id=login_id, password=password)


def set_password(app: Access, person: Person | None, target: UUID, password: str, *, current: str | None = None) -> None:
    """パスワードを決める・替える（1か所）。本人（person が target）は、いまのパスワード（current）を確かめてから替える。管理者は、ほかの方の
    パスワードを決められる（全体の取引）。替えたら、その方の前のログインの印は、すべて使えなくする（ほかの所で開いたままの画面も）。"""
    if person is not None and person.person_id == target and not person.is_admin:
        row = app.in_system(LOGIN_CHECKER, lambda tx: _lookup(tx, person.login_id))
        if current is None or row is None or row["hash"] is None or not _matches(current, row["hash"]):
            raise PasswordRefused("いまのパスワードが合いません")
        actor = LOGIN_CHECKER
    elif person is not None and person.is_admin:
        actor = person.actor
    else:
        raise PasswordRefused("パスワードを替えられるのは、本人か管理者だけです")

    def work(tx: Tx) -> None:
        tx.execute("INSERT INTO salescall.person_passwords (person_id, hash, set_by) VALUES (%s, %s, salescall.cur_actor())",
                   (target, _checked_password(tx, password)))
    app.in_system(actor, work)
    app.in_system(LOGIN_CHECKER, lambda tx: _end_sessions(tx, target, "password_changed"))


def set_active(tx: Tx, target: UUID, active: bool, *, reason: str) -> None:
    """ログインを使える・使えないにする（全体の取引の管理者だけ＝データベースが守る）。使えないログインは、確かめも印も通らない（取引も
    salescall.check_scope が断る）。"""
    if not reason.strip():
        raise Refused("理由を入れてください")
    tx.execute("INSERT INTO salescall.person_states (person_id, active, reason, set_by) VALUES (%s, %s, %s, salescall.cur_actor())",
               (target, active, reason.strip()))


def _lookup(tx: Tx, login_id: str) -> dict[str, Any] | None:
    return tx.one("SELECT p.person_id, p.tenant_id, p.kind, p.name, p.login_id, salescall.person_active(p.person_id) AS active,"
                  "       (SELECT w.hash FROM salescall.person_passwords w WHERE w.person_id = p.person_id ORDER BY w.seq DESC LIMIT 1) AS hash"
                  " FROM salescall.people p WHERE p.login_id = %s", (_login_key(login_id),))


def _locked(tx: Tx, login_id: str) -> bool:
    """続けて誤った回数が決めた回数を超えて、決めた長さがまだ過ぎていないか（1か所）。数えるのは、いちばん新しい成功のあとの失敗だけ。"""
    most, span = int(_setting(tx, "login.max_failures")), int(_setting(tx, "login.lock_seconds"))
    row = tx.one("SELECT count(*) AS n, max(e.at) AS last FROM salescall.person_login_events e"
                 " WHERE e.login_id = %s AND NOT e.ok AND e.why <> 'locked'"
                 "   AND e.at > coalesce((SELECT max(o.at) FROM salescall.person_login_events o WHERE o.login_id = %s AND o.ok), '-infinity')",
                 (login_id, login_id))
    return row["n"] >= most and row["last"] is not None and tx.one(
        "SELECT now() < %s AS v", (row["last"] + timedelta(seconds=span),))["v"]


def login(app: Access, login_id: str, password: str) -> Session | None:
    """ログインIDとパスワードを確かめる（1か所）。通れば印を返し、通らなければ None（どの理由で通らなかったかは画面に出さない＝ログインIDが
    在るかを教えない。記録には残す）。"""
    key = _login_key(login_id)

    def work(tx: Tx) -> Session | None:
        row = _lookup(tx, key)
        if _locked(tx, key):
            why = "locked"
        elif row is None:
            _matches(password, _DUMMY)   # 在るかを、かかる時間で教えない
            why = "no_such_login"
        elif row["hash"] is None:
            why = "no_password"
        elif not _matches(password, row["hash"]):
            why = "wrong_password"
        elif not row["active"]:
            why = "inactive"
        else:
            why = "ok"
        tx.execute("INSERT INTO salescall.person_login_events (login_id, person_id, ok, why) VALUES (%s, %s, %s, %s)",
                   (key, None if row is None else row["person_id"], why == "ok", why))
        if why != "ok":
            return None
        token = secrets.token_urlsafe(32)
        tx.execute("INSERT INTO salescall.person_sessions (token_sha, person_id, expires_at)"
                   " VALUES (%s, %s, now() + make_interval(secs => %s))",
                   (hashlib.sha256(token.encode()).hexdigest(), row["person_id"], int(_setting(tx, "login.session_seconds"))))
        return Session(token, _person(row))
    return app.in_system(LOGIN_CHECKER, work)


def _person(row: dict[str, Any]) -> Person:
    return Person(row["person_id"], row["tenant_id"], row["kind"], row["name"], row["login_id"])


def by_session(app: Access, token: str | None) -> Person | None:
    """ログインの印から、ログインした方（期限が過ぎた・終えた・使えないログインの印なら None）。画面が要求ごとに呼ぶ1か所。"""
    if not token:
        return None
    row = app.in_system(LOGIN_CHECKER, lambda tx: tx.one(
        "SELECT p.person_id, p.tenant_id, p.kind, p.name, p.login_id FROM salescall.person_sessions s"
        " JOIN salescall.people p ON p.person_id = s.person_id"
        " WHERE s.token_sha = %s AND s.expires_at > now() AND salescall.person_active(p.person_id)"
        "   AND NOT EXISTS (SELECT 1 FROM salescall.person_session_ends e WHERE e.token_sha = s.token_sha)",
        (hashlib.sha256(token.encode()).hexdigest(),)))
    return None if row is None else _person(row)


def logout(app: Access, token: str) -> None:
    """その印を終える（ログアウト）。"""
    sha = hashlib.sha256(token.encode()).hexdigest()
    app.in_system(LOGIN_CHECKER, lambda tx: tx.execute(
        "INSERT INTO salescall.person_session_ends (token_sha, why) SELECT token_sha, 'logout' FROM salescall.person_sessions"
        " WHERE token_sha = %s ON CONFLICT (token_sha) DO NOTHING", (sha,)))


def _end_sessions(tx: Tx, person_id: UUID, why: str) -> None:
    tx.execute("INSERT INTO salescall.person_session_ends (token_sha, why) SELECT s.token_sha, %s FROM salescall.person_sessions s"
               " WHERE s.person_id = %s ON CONFLICT (token_sha) DO NOTHING", (why, person_id))
