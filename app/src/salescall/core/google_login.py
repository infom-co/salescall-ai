"""Google のアカウントでのログイン（表の作り 0272。計画書 14-1。持ち主 CL-2。画面は CL-3）。

✅ 2026-10-18 ユーザー（計画書 14-1）「ログインは ID とパスワードか Google のアカウント」。

- **Google の ID の印（ID トークン）の確かめはこの1か所**（`verify`）：Google の公開の鍵（`CERTS_URL`。`fetch_keys` が取り、期限まで使い回す）で署名（RS256）を
  確かめ、発行元（`ISSUERS`）・宛先（ダッシュボードの Google のクライアント ID＝画面のサーバーの設定から渡す）・期限・メールの確認済みを見る。外の道具は足さない
  （暗号の道具 cryptography で確かめる）。
- ログインした方が、自分の Google のアカウントを結び付ける（`link`。sub＝Google の変わらない番号で結ぶ）。1つの Google のアカウントは1人の方にだけ（データベースが守る）。
- Google でログインした方にも、パスキーを登録していれば2段目が要る（ログインの印を作るのは core/people.start_session の1か所）。
"""

from __future__ import annotations

import base64
import json
import time
import urllib.request
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from salescall.core.access import Access, Refused, Tx

CERTS_URL = "https://www.googleapis.com/oauth2/v3/certs"
ISSUERS = ("https://accounts.google.com", "accounts.google.com")
SKEW_SECONDS = 60          # 時計のずれの余裕（秒）
_cache: dict[str, Any] = {"keys": None, "until": 0.0}


class GoogleRefused(Refused):
    """Google の印を確かめられなかった・結び付けられなかった（画面に出してよい日本語）。"""


def _b64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def fetch_keys(*, opener: Any = urllib.request.urlopen, now: float | None = None) -> dict[str, Any]:
    """Google の公開の鍵（JWKS）。前に取った鍵を、Google が示した期限（Cache-Control の max-age）まで使い回す（1か所）。"""
    t = time.time() if now is None else now
    if _cache["keys"] is not None and t < _cache["until"]:
        return _cache["keys"]
    with opener(CERTS_URL, timeout=10) as r:
        keys = json.loads(r.read().decode())
        age = next((int(p.split("=", 1)[1]) for p in (r.headers.get("Cache-Control") or "").split(",") if p.strip().startswith("max-age=")), 300)
    _cache.update(keys=keys, until=t + age)
    return keys


def verify(id_token: str, *, client_id: str, keys: dict[str, Any], now: float | None = None) -> dict[str, Any]:
    """Google の ID の印を確かめて、中身（sub・email など）を返す（1か所）。通らなければ GoogleRefused。"""
    try:
        head_b, body_b, sig_b = id_token.split(".")
        head, body, sig = json.loads(_b64(head_b)), json.loads(_b64(body_b)), _b64(sig_b)
    except (ValueError, AttributeError):
        raise GoogleRefused("Google のログインを確かめられませんでした") from None
    if head.get("alg") != "RS256":
        raise GoogleRefused("Google のログインを確かめられませんでした")
    jwk = next((k for k in keys.get("keys", ()) if k.get("kid") == head.get("kid") and k.get("kty") == "RSA"), None)
    if jwk is None:
        raise GoogleRefused("Google のログインを確かめられませんでした（鍵が見つかりません）")
    public = rsa.RSAPublicNumbers(int.from_bytes(_b64(jwk["e"]), "big"), int.from_bytes(_b64(jwk["n"]), "big")).public_key()
    try:
        public.verify(sig, f"{head_b}.{body_b}".encode(), padding.PKCS1v15(), hashes.SHA256())
    except InvalidSignature:
        raise GoogleRefused("Google のログインを確かめられませんでした") from None
    t = time.time() if now is None else now
    aud = body.get("aud")
    if body.get("iss") not in ISSUERS or (aud != client_id and not (isinstance(aud, list) and client_id in aud)):
        raise GoogleRefused("Google のログインを確かめられませんでした（宛先が違います）")
    if not isinstance(body.get("exp"), (int, float)) or body["exp"] + SKEW_SECONDS < t or body.get("iat", 0) - SKEW_SECONDS > t:
        raise GoogleRefused("Google のログインの期限が過ぎています。もう一度お願いします")
    if not body.get("sub") or body.get("email_verified") is not True or not body.get("email"):
        raise GoogleRefused("Google のアカウントのメールが確認されていません")
    return body


def _checker() -> Any:
    from salescall.core.people import LOGIN_CHECKER   # ログインの部品が、この部品を読むので、ここで読む（読み合いにしない）
    return LOGIN_CHECKER


def link(app: Access, person: Any, id_token: str, *, client_id: str, keys: dict[str, Any] | None = None) -> str:
    """ログインした方が、自分の Google のアカウントを結び付ける（ほかの方に結び付いていれば GoogleRefused）。結び付けたメールを返す。"""
    claims = verify(id_token, client_id=client_id, keys=keys if keys is not None else fetch_keys())

    def work(tx: Tx) -> str:
        from salescall.core.access import Rejected
        try:
            tx.execute("INSERT INTO salescall.person_google_links (person_id, sub, email, linked) VALUES (%s, %s, %s, true)",
                       (person.person_id, claims["sub"], claims["email"]))
        except Rejected as e:
            if e.constraint == "person_google_one_person":
                raise GoogleRefused("この Google のアカウントは、ほかのログインに結び付いています") from None
            raise
        return claims["email"]
    return app.in_system(_checker(), work)


def unlink(app: Access, person: Any) -> None:
    """自分に結び付いた Google のアカウントを外す（結び付いていなければ何もしない）。"""
    def work(tx: Tx) -> None:
        for row in linked(tx, person.person_id):
            tx.execute("INSERT INTO salescall.person_google_links (person_id, sub, email, linked) VALUES (%s, %s, %s, false)",
                       (person.person_id, row["sub"], row["email"]))
    app.in_system(_checker(), work)


def linked(tx: Tx, person_id: Any) -> list[dict[str, Any]]:
    """その方に、いま結び付いている Google のアカウント（sub・email）。"""
    return tx.all("SELECT l.sub, l.email FROM (SELECT DISTINCT ON (sub) sub, email, person_id, linked FROM salescall.person_google_links"
                  "                       ORDER BY sub, seq DESC) l WHERE l.person_id = %s AND l.linked ORDER BY l.email", (person_id,))


def login(app: Access, id_token: str, *, client_id: str, keys: dict[str, Any] | None = None) -> Any:
    """Google の印でログインする（1か所）。結び付いた方なら印（core/people.Session）、結び付いていない・使えないログインなら None（理由は記録に残す）。
    印が確かめられなければ GoogleRefused。"""
    from salescall.core import people
    claims = verify(id_token, client_id=client_id, keys=keys if keys is not None else fetch_keys())

    def work(tx: Tx) -> Any:
        row = tx.one("SELECT p.person_id, p.tenant_id, p.kind, p.name, p.login_id, salescall.person_active(p.person_id) AS active"
                     " FROM (SELECT DISTINCT ON (sub) sub, person_id, linked FROM salescall.person_google_links WHERE sub = %s ORDER BY sub, seq DESC) l"
                     " JOIN salescall.people p ON p.person_id = l.person_id WHERE l.linked", (claims["sub"],))
        if row is None:
            people.note_login(tx, f"google:{claims['sub']}", None, "google_unlinked")
            return None
        if not row["active"]:
            people.note_login(tx, row["login_id"], row["person_id"], "inactive")
            return None
        people.note_login(tx, row["login_id"], row["person_id"], "google_ok")
        return people.start_session(tx, row)
    return app.in_system(_checker(), work)
