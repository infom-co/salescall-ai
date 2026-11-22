"""画面で物を名指す1か所（持ち主 CL-3）。

- 番号の書き方：そろえるのも区切るのも core の phone の1か所（`PhoneNumber.display`＝区切りが1つに決まる種類だけ「-」で区切る。固定電話などは
  国内の形のまま。そろえられなければ、記録の値のまま）。画面だけの形：依頼者へ渡すファイル・手元の控えは、相手の表と突き合わせやすい国内の形のまま。
- 回線の名指し：名前に発信番号を添える（2026-11-21 の見直し：どの回線も同じ名前だと、気をつけることの画面で見分けられなかった）。形は core の
  `line_names.label` の1か所（知らせの文の頭と画面が同じ形。CL-2 4-470 の (3)）。画面は、名前が無いときの言葉だけを持つ。
"""

from __future__ import annotations

from salescall.core import line_names, phone


def phone_text(e164: str | None) -> str:
    """番号の書き方（ひな形の phone() と、画面のサーバーの文が同じものを使う）。"""
    if not e164:
        return ""
    return phone.normalize(e164).display or e164


def line_label(name: str | None, caller_e164: str | None) -> str:
    """回線の名指し：「名前（発信番号）」（core の line_names.label の1か所）。番号が無ければ名前だけ。"""
    return line_names.label(name or "回線", caller_e164)
