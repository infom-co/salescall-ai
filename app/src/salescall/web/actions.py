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

# 理由の文つきの断り（何も書いていない）の共通の元は core の access.Refused の1か所（CL-2 4-430 の 21）。画面の側の断りも、それを元にする。
# 画面は、これ1つを受けて文をそのまま出す（core が新しい断りを足しても、ここに足し忘れて画面が「誤り」で止まることが無い）。
class Refused(access.Refused):
    """画面の側の理由つきの断り（何も書いていない）。"""


def name_of(form: dict[str, Any], field: str = "name") -> str:
    """作る・直すものの名前（空なら断る。core も空の名前を断るが、その断りは理由の文の種類が無いので、ここで先に断る）。"""
    n = str(form.get(field) or "").strip()
    if not n:
        raise Refused("名前を入れてください")
    return n


def uuid_of(form: dict[str, Any], field: str) -> Any:
    """フォームの ID の欄を読む1か所。形が違えば「見つかりません」と同じ返事（LookupError＝`act` が言い換える）。"""
    from uuid import UUID
    try:
        return UUID(str(form.get(field) or ""))
    except ValueError:
        raise LookupError(field) from None


REFUSALS: tuple[type[Exception], ...] = (access.Refused,)


# データベースの断りの言い換え（1か所）。日本語の理由を書いて断る種類（P0001＝データベースの決まりの RAISE）だけ、その文をそのまま出す。
# ほかの種類（一意の約束・つながりの約束・読める範囲の外 など）の文は、データベースの英語の文なので出さず、種類ごとの言葉にする。
REJECTED_WORDS: dict[str, str] = {
    "23505": "もう済んでいます（同じものが、すでに記録されています）",
    "23503": "つながる記録が無いか、もう使われています",
    "23514": "記録の決まりに合いません",
    "23502": "入れなければならない値が空です",
    "23P01": "同じ時間に重なる記録が、すでにあります",
    "42501": "この範囲では、この操作はできません",
    "25006": "読むだけの画面からは変えられません",
}


def rejected_text(e: access.Rejected) -> str:
    if e.sqlstate == "P0001" and e.message.strip():
        return e.message.strip()
    return REJECTED_WORDS.get(e.sqlstate or "", "記録の決まりに合わないため、受け付けられませんでした")


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


def sign_choice(secret: bytes, login_id: str, tenant_id: Any) -> str:
    """管理者が選んだ契約者の札（その人だけが使える署名つき）。"""
    mac = hmac.new(secret, f"acting:{login_id}:{tenant_id}".encode(), hashlib.sha256).hexdigest()
    return f"{tenant_id}.{mac}"


def read_choice(secret: bytes, login_id: str, value: str | None) -> Any:
    """署名つきの札から、選んだ契約者を読む。合わなければ None。"""
    from uuid import UUID
    if not value or "." not in value:
        return None
    raw, mac = value.split(".", 1)
    want = hmac.new(secret, f"acting:{login_id}:{raw}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(mac, want):
        return None
    try:
        return UUID(raw)
    except ValueError:
        return None


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
        return Outcome(False, f"受け付けられませんでした：{rejected_text(e)}（何も変わっていません）")
    except REFUSALS as e:
        return Outcome(False, f"受け付けられませんでした：{str(e).strip() or '理由が返っていません'}（何も変わっていません）")
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
