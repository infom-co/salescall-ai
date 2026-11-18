"""変える操作の形の1か所（docs/DASHBOARD.md 6-3）。

画面から何かを変える操作は、すべて `act` を通す：
- なりすましの送信の確かめ（ほかのサイトから送らせた操作を断る。ログインの印と別の、画面ごとの札＝`form_token`）。
- 理由が要る操作の、空の確かめ（空では core を呼ばない）。
- データベースが断ったとき（`Rejected`）の言い換え、データベースに届かないときの言い換え。
- 画面は判断しない：変えてよいかは core とデータベースが決める。ここは呼んで、結果を言葉にするだけ。

⏳ 二度押しの札（同じ操作を2回送っても1回だけ効く＝docs/DASHBOARD.md 8-1）は、使った札をしまう表が要るので CL-2 に頼んでいる。
それまでは、送っている間はボタンを押せなくし（画面）、core の一意の約束に頼る。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from salescall.core import access

TOKEN_FIELD = "form_token"


def form_token(secret: bytes, login_id: str) -> str:
    """その人の画面に埋める札（ログインした人ごとに決まる。ほかのサイトは作れない）。"""
    nonce = secrets.token_hex(8)
    mac = hmac.new(secret, f"{login_id}:{nonce}".encode(), hashlib.sha256).hexdigest()
    return f"{nonce}.{mac}"


def token_ok(secret: bytes, login_id: str, token: str | None) -> bool:
    if not token or "." not in token:
        return False
    nonce, mac = token.split(".", 1)
    want = hmac.new(secret, f"{login_id}:{nonce}".encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(mac, want)


@dataclass(frozen=True)
class Outcome:
    """操作の結果（画面に出す1行）。ok＝変わった。"""
    ok: bool
    message: str
    value: Any = None


def act(secret: bytes, login_id: str, form: dict[str, Any], do: Callable[[], Any], *,
        done: str, reason_field: str | None = None) -> Outcome:
    """変える操作を1回行う。do＝core を呼ぶ関数（取引の中身は Ctx を通す）。done＝うまくいったときの言葉。
    reason_field＝理由が要る操作の欄の名前（空なら断る）。"""
    if not token_ok(secret, login_id, form.get(TOKEN_FIELD)):
        return Outcome(False, "画面の札が合いません。画面を開き直してから、もう一度押してください。")
    if reason_field is not None and not str(form.get(reason_field) or "").strip():
        return Outcome(False, "理由を書いてください（理由は記録に残ります）。")
    try:
        value = do()
    except access.Rejected as e:
        return Outcome(False, f"受け付けられませんでした：{e.message or '理由が返っていません'}")
    except LookupError:
        # 「無い」と「見てはいけない」を同じ返事にする（core は取引の範囲の外を LookupError で返す。docs/DASHBOARD.md 8-3）
        return Outcome(False, "見つかりません（すでに片付いたか、この範囲の外です）。何も変わっていません。")
    except access.RetryExhausted:
        return Outcome(False, "ほかの操作と重なり、受け付けられませんでした。少し待ってから、もう一度押してください。")
    except Exception as e:
        if access.unreachable(e) or access.busy(e):
            return Outcome(False, "データベースに届きません。何も変わっていません。少し待ってから、もう一度押してください。")
        raise
    return Outcome(True, done, value)
