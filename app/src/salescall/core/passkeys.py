"""ログインの2段階の確かめ＝パスキー（表の作り 0271。計画書 14-1。持ち主 CL-2。画面は CL-3）。

✅ 2026-10-18 ユーザー（計画書 14-1）「2段階認証は任意で…確かめにパスキーも使える」。✅ 2026-11-20 ユーザー（「絶対に安全なのは何？」への答えのあと）「ア パスキーだけ」：
2段階の確かめはパスキーだけ（偽のサイトでは使えない・秘密の鍵は端末の外へ出ない・サーバーは公開の鍵だけを持つ）。端末を失くしたときのために、予備のパスキーを別の端末に
登録でき、一度だけ使える予備の合言葉（`RECOVERY_CODES` 個。写しだけを残す）を用意する。

- **WebAuthn の確かめはこの1か所**（外の道具 webauthn を使ってよいのは、ここだけ＝機械が見張る）。問い（チャレンジ）は1回だけ・短い期限（`CHALLENGE_SECONDS`）。
  本人の確かめ（端末の指紋・顔・暗証番号）を必ず求める（user_verification＝required）。
- サイトの住所（`Site`＝パスキーを結び付ける先）は、ダッシュボードのサーバー（画面＝CL-3）が自分の設定から渡す。
- パスキーを登録した方は、パスワードのあと、`verify_login` か `use_recovery_code` を通るまで、ログインの印を使えない（判断は core/people.by_session の1か所）。
- どの表も書き換えない。読み書きは、ログインの確かめ（全体の取引のシステムの処理＝people.LOGIN_CHECKER）だけ。
"""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from webauthn import (generate_authentication_options, generate_registration_options, options_to_json, verify_authentication_response,
                      verify_registration_response)
from webauthn.helpers.structs import (AuthenticatorSelectionCriteria, PublicKeyCredentialDescriptor, ResidentKeyRequirement,
                                      UserVerificationRequirement)

from salescall.core.access import Access, Refused, Tx

CHALLENGE_SECONDS = 300     # 問いの期限（秒。登録・確かめの画面を開いてから、端末で確かめ終えるまで）
RECOVERY_CODES = 10         # 予備の合言葉の数（一度ずつ使える）


class PasskeyRefused(Refused):
    """パスキーの登録・確かめが通らなかった（理由は画面に出してよい日本語。何も書いていない）。"""


@dataclass(frozen=True)
class Site:
    """パスキーを結び付けるサイト（ダッシュボードのサーバーの設定から）：rp_id＝住所の名前（例 app.example.jp）・origin＝https://…・name＝見せる名前。"""
    rp_id: str
    origin: str
    name: str = "SalesCall AI"


def _checker() -> Any:
    from salescall.core.people import LOGIN_CHECKER   # ログインの部品が、この部品を読むので、ここで読む（読み合いにしない）
    return LOGIN_CHECKER


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _challenge(tx: Tx, purpose: str, person_id: UUID, token_sha: str | None = None) -> bytes:
    c = secrets.token_bytes(32)
    tx.execute("INSERT INTO salescall.person_passkey_challenges (challenge, purpose, person_id, token_sha, expires_at)"
               " VALUES (%s, %s, %s, %s, now() + make_interval(secs => %s))", (c, purpose, person_id, token_sha, CHALLENGE_SECONDS))
    return c


def _take_challenge(tx: Tx, purpose: str, person_id: UUID, token_sha: str | None = None) -> bytes:
    """いちばん新しい、期限内でまだ使っていない問いを使う（1回だけ。無ければ PasskeyRefused）。"""
    row = tx.one("SELECT c.challenge FROM salescall.person_passkey_challenges c WHERE c.purpose = %s AND c.person_id = %s"
                 " AND c.token_sha IS NOT DISTINCT FROM %s AND c.expires_at > now()"
                 " AND NOT EXISTS (SELECT 1 FROM salescall.person_passkey_challenge_uses u WHERE u.challenge = c.challenge)"
                 " ORDER BY c.expires_at DESC LIMIT 1", (purpose, person_id, token_sha))
    if row is None:
        raise PasskeyRefused("確かめの期限が過ぎました。もう一度はじめからお願いします")
    tx.execute("INSERT INTO salescall.person_passkey_challenge_uses (challenge) VALUES (%s)", (bytes(row["challenge"]),))
    return bytes(row["challenge"])


def active(tx: Tx, person_id: UUID) -> list[dict[str, Any]]:
    """その方の使えるパスキー（外していないもの。登録の順）：[{passkey_id, credential_id, public_key, label, added_at, sign_count}]。"""
    return tx.all("SELECT k.passkey_id, k.credential_id, k.public_key, k.label, k.added_at,"
                  "       coalesce((SELECT max(u.sign_count) FROM salescall.person_passkey_uses u WHERE u.passkey_id = k.passkey_id), 0) AS sign_count"
                  " FROM salescall.person_passkeys k WHERE k.person_id = %s"
                  " AND NOT EXISTS (SELECT 1 FROM salescall.person_passkey_removals r WHERE r.passkey_id = k.passkey_id)"
                  " ORDER BY k.added_at, k.passkey_id", (person_id,))


def register_options(app: Access, person: Any, site: Site) -> str:
    """パスキーを登録する画面に渡す問い（JSON の文字。ログインした方が自分で登録する）。同じ端末のパスキーは重ねて登録しない（exclude）。"""
    def work(tx: Tx) -> str:
        c = _challenge(tx, "register", person.person_id)
        options = generate_registration_options(
            rp_id=site.rp_id, rp_name=site.name, user_id=person.person_id.bytes, user_name=person.login_id, user_display_name=person.name,
            challenge=c, timeout=CHALLENGE_SECONDS * 1000,
            exclude_credentials=[PublicKeyCredentialDescriptor(id=bytes(k["credential_id"])) for k in active(tx, person.person_id)],
            authenticator_selection=AuthenticatorSelectionCriteria(resident_key=ResidentKeyRequirement.PREFERRED,
                                                                   user_verification=UserVerificationRequirement.REQUIRED))
        return options_to_json(options)
    return app.in_system(_checker(), work)


def register(app: Access, person: Any, site: Site, credential: str | dict[str, Any], label: str) -> tuple[UUID, list[str] | None]:
    """端末が作ったパスキーを確かめて登録する（1か所）。最初のパスキーのときだけ、予備の合言葉を作って返す（このときだけ見せる。写しだけを残す）。
    通らなければ PasskeyRefused。"""
    if not label or not label.strip():
        raise PasskeyRefused("パスキーの名前（「仕事の PC」など）を入れてください")

    def work(tx: Tx) -> tuple[UUID, list[str] | None]:
        c = _take_challenge(tx, "register", person.person_id)
        try:
            got = verify_registration_response(credential=credential, expected_challenge=c, expected_rp_id=site.rp_id,
                                               expected_origin=site.origin, require_user_verification=True)
        except Exception:   # 外の道具の断り（形・署名・住所・本人の確かめ）。中身は画面に出さない
            raise PasskeyRefused("パスキーを確かめられませんでした。もう一度お試しください") from None
        first = not active(tx, person.person_id)
        pid = tx.one("INSERT INTO salescall.person_passkeys (person_id, credential_id, public_key, label) VALUES (%s, %s, %s, %s)"
                     " ON CONFLICT (credential_id) DO NOTHING RETURNING passkey_id",
                     (person.person_id, got.credential_id, got.credential_public_key, label.strip()))
        if pid is None:
            raise PasskeyRefused("このパスキーは、もう登録されています")
        tx.execute("INSERT INTO salescall.person_passkey_uses (passkey_id, sign_count) VALUES (%s, %s)", (pid["passkey_id"], got.sign_count))
        return pid["passkey_id"], (_new_codes(tx, person.person_id) if first else None)
    return app.in_system(_checker(), work)


def remove(app: Access, person: Any, passkey_id: UUID) -> None:
    """自分のパスキーを外す（失くした端末など）。最後の1つを外すと、2段階の確かめは無くなる（任意のため。予備の合言葉も使えなくなる＝次に登録したら作り直す）。"""
    def work(tx: Tx) -> None:
        if tx.one("SELECT 1 AS x FROM salescall.person_passkeys WHERE passkey_id = %s AND person_id = %s", (passkey_id, person.person_id)) is None:
            raise PasskeyRefused("そのパスキーはありません")
        tx.execute("INSERT INTO salescall.person_passkey_removals (passkey_id) VALUES (%s) ON CONFLICT (passkey_id) DO NOTHING", (passkey_id,))
    app.in_system(_checker(), work)


def login_options(app: Access, token: str, site: Site) -> str:
    """2段目を待つログインの印に、パスキーで確かめる問い（JSON の文字）。2段目が要らない・終えた印なら PasskeyRefused。"""
    def work(tx: Tx) -> str:
        s = _waiting(tx, token)
        c = _challenge(tx, "login", s["person_id"], s["token_sha"])
        return options_to_json(generate_authentication_options(
            rp_id=site.rp_id, challenge=c, timeout=CHALLENGE_SECONDS * 1000, user_verification=UserVerificationRequirement.REQUIRED,
            allow_credentials=[PublicKeyCredentialDescriptor(id=bytes(k["credential_id"])) for k in active(tx, s["person_id"])]))
    return app.in_system(_checker(), work)


def verify_login(app: Access, token: str, site: Site, credential: str | dict[str, Any]) -> bool:
    """2段目をパスキーで確かめる（1か所）。通れば、その印を使えるようにして True。通らなければ False（理由は画面に出さない・記録に残す）。"""
    def work(tx: Tx) -> bool:
        s = _waiting(tx, token)
        if _second_locked(tx, s):
            return False
        ok = False
        try:
            c = _take_challenge(tx, "login", s["person_id"], s["token_sha"])
            raw = json.loads(credential) if isinstance(credential, str) else credential
            key = next((k for k in active(tx, s["person_id"]) if _b64(bytes(k["credential_id"])) == raw.get("id")), None)
            if key is not None:
                got = verify_authentication_response(credential=credential, expected_challenge=c, expected_rp_id=site.rp_id,
                                                     expected_origin=site.origin, credential_public_key=bytes(key["public_key"]),
                                                     credential_current_sign_count=key["sign_count"], require_user_verification=True)
                tx.execute("INSERT INTO salescall.person_passkey_uses (passkey_id, sign_count) VALUES (%s, %s)", (key["passkey_id"], got.new_sign_count))
                tx.execute("INSERT INTO salescall.person_session_seconds (token_sha, how) VALUES (%s, 'passkey')", (s["token_sha"],))
                ok = True
        except Exception:   # 外の道具の断り・期限切れ（署名・住所・回数・本人の確かめ）
            ok = False
        _log(tx, s, "passkey_ok" if ok else "passkey_failed")
        return ok
    return app.in_system(_checker(), work)


def use_recovery_code(app: Access, token: str, code: str) -> bool:
    """2段目を予備の合言葉で確かめる（いちばん新しい組の、まだ使っていないもの。一度だけ）。通れば True。2段目を続けて誤ったら、決めた長さのあいだ
    試させない（core/people.locked の1か所。試さずに False）。"""
    def work(tx: Tx) -> bool:
        s = _waiting(tx, token)
        if _second_locked(tx, s):
            return False
        row = tx.one("SELECT c.seq FROM salescall.person_recovery_codes c WHERE c.person_id = %s AND c.code_sha = %s"
                     " AND c.batch = (SELECT x.batch FROM salescall.person_recovery_codes x WHERE x.person_id = c.person_id ORDER BY x.seq DESC LIMIT 1)"
                     " AND NOT EXISTS (SELECT 1 FROM salescall.person_recovery_code_uses u WHERE u.seq = c.seq)",
                     (s["person_id"], _sha(_plain_code(code))))
        if row is not None:
            tx.execute("INSERT INTO salescall.person_recovery_code_uses (seq) VALUES (%s)", (row["seq"],))
            tx.execute("INSERT INTO salescall.person_session_seconds (token_sha, how) VALUES (%s, 'recovery_code')", (s["token_sha"],))
        _log(tx, s, "recovery_code_ok" if row is not None else "recovery_code_failed")
        return row is not None
    return app.in_system(_checker(), work)


def new_recovery_codes(app: Access, person: Any) -> list[str]:
    """予備の合言葉を作り直す（前の組は使えなくなる。このときだけ見せる）。パスキーが無ければ PasskeyRefused。"""
    def work(tx: Tx) -> list[str]:
        if not active(tx, person.person_id):
            raise PasskeyRefused("先にパスキーを登録してください")
        return _new_codes(tx, person.person_id)
    return app.in_system(_checker(), work)


def _new_codes(tx: Tx, person_id: UUID) -> list[str]:
    batch, codes = uuid4(), [secrets.token_hex(5).upper() for _ in range(RECOVERY_CODES)]
    for code in codes:
        tx.execute("INSERT INTO salescall.person_recovery_codes (person_id, batch, code_sha) VALUES (%s, %s, %s)", (person_id, batch, _sha(code)))
    return [f"{c[:5]}-{c[5:]}" for c in codes]


def _plain_code(code: str) -> str:
    """入れられた予備の合言葉をそろえる（区切りの「-」・空白・大文字小文字の違いを見ない）。"""
    return "".join(ch for ch in (code or "") if ch.isalnum()).upper()


def _b64(raw: bytes) -> str:
    import base64
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def needs_second(tx: Tx, person_id: UUID) -> bool:
    """ログインの2段目が要るか（1か所＝パスキーを1つ以上登録している）。ログインの印を作るとき core/people.login が読む。"""
    return bool(active(tx, person_id))


def _waiting(tx: Tx, token: str) -> dict[str, Any]:
    """2段目を待っている、期限内のログインの印（無ければ PasskeyRefused）。"""
    row = tx.one("SELECT s.token_sha, s.person_id FROM salescall.person_sessions s WHERE s.token_sha = %s AND s.expires_at > now() AND s.needs_second"
                 " AND NOT EXISTS (SELECT 1 FROM salescall.person_session_seconds x WHERE x.token_sha = s.token_sha)"
                 " AND NOT EXISTS (SELECT 1 FROM salescall.person_session_ends e WHERE e.token_sha = s.token_sha)",
                 (_sha(token or ""),))
    if row is None:
        raise PasskeyRefused("ログインし直してください")
    return row


def _second_locked(tx: Tx, s: dict[str, Any]) -> bool:
    from salescall.core import people
    login_id = tx.one("SELECT login_id FROM salescall.people WHERE person_id = %s", (s["person_id"],))["login_id"]
    return people.locked(tx, login_id, "second")


def _log(tx: Tx, s: dict[str, Any], why: str) -> None:
    from salescall.core import people   # ログインの部品が、この部品を読むので、ここで読む（読み合いにしない）
    login_id = tx.one("SELECT login_id FROM salescall.people WHERE person_id = %s", (s["person_id"],))["login_id"]
    people.note_login(tx, login_id, s["person_id"], why)   # 記録を書くのはログインの部品の1か所
