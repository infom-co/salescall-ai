"""ログイン（docs/DASHBOARD.md 1章。持ち主 CL-3）。ログインの表・パスワード・印は core の people の1か所（CL-2 4-433）。ここは、
ブラウザの印（クッキー）を読んで、ログインした方を画面の形（context.Person）にするだけ。

- 印は people.login が通ったときだけ返る。ブラウザのクッキー `COOKIE` にしまう（スクリプトから読めない・https だけで送る・ほかのサイトから
  送らない）。要求ごとに people.by_session で、ログインした方を読む（期限が過ぎた・終えた・使えないログインなら None＝ログインの画面へ）。
- 取引は、ログインした方の印（Person.actor＝ログインの番号つき）で開く（データベースが範囲を確かめる）。
- 通らなかった理由は出さない（「ログインIDかパスワードが違います」だけ。ログインIDが在るかを教えない）。続けて誤ったときの止め方は core の1か所。
"""

from __future__ import annotations

from typing import Any

from salescall.core import people
from salescall.core.access import Access
from salescall.web import context
from salescall.web.context import Identify, Person

COOKIE = "salescall_session"
WRONG = "ログインIDかパスワードが違います"
PASSWORD_CHANGED = "パスワードを替えました。ほかの所で開いていたログインも外れています。新しいパスワードでログインしてください。"
NOTE_2FA = "お客様情報を含むことになるため、安全のために二段階認証の設定をお願いします。"   # ✅ 2026-10-18 管理者（そのまま載せる）


def identify(access: Access) -> Identify:
    """ブラウザの印から、ログインした方を読む関数（create_app に渡す。1か所）。"""
    def read(request: Any) -> Person | None:
        p = people.by_session(access, request.cookies.get(COOKIE))
        if p is None:
            return None
        return Person(p.login_id, p.is_admin, p.tenant_id, context.tenant_name(access, p.actor, p.tenant_id), name=p.name,
                      person_id=p.person_id)
    return read
