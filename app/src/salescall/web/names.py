"""画面で物を名指す1か所（持ち主 CL-3）。

- 番号の書き方：そろえるのは core の phone.normalize の1か所（国内の番号は 0 から。そろえられなければ、そのまま）。
- 回線の名指し：名前に発信番号を添える（2026-11-21 の見直し：どの回線も同じ名前だと、気をつけることの画面で見分けられなかった）。
"""

from __future__ import annotations

from salescall.core import phone


def phone_text(e164: str | None) -> str:
    """番号の書き方（ひな形の phone() と、画面のサーバーの文が同じものを使う）。"""
    if not e164:
        return ""
    return phone.normalize(e164).national or e164


def line_label(name: str | None, caller_e164: str | None) -> str:
    """回線の名指し：「名前（発信番号）」。番号が無ければ名前だけ。"""
    number = phone_text(caller_e164)
    base = name or "回線"
    return f"{base}（{number}）" if number else base
