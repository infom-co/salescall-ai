"""回線の名指し（1か所。持ち主 CL-2）：知らせの文・画面が回線を指すときは、名前に発信番号を添える＝「名前（090-1234-5678）」。

✅ CL-3 のお願い 71（2026-11-21）：同じ名前の回線を見分けられない（試しの見本では、どの回線も同じ名前だった）。番号の見せる形は
`phone.PhoneNumber.display`（種類で区切りが決まる番号だけ区切る）の1か所。知らせの文に付けるのは `notify.emit` の1か所（差し込む値に
回線の印 line_id のある知らせ＝回線のことを知らせる文すべて）。呼ぶ側は文に回線の名前を書かない（2か所にしない）。
⛔ それまで：状態の欄の知らせ（「この回線を休ませます」など）・回線が使えない・2台の PC が同じ回線、はどの回線かを文に書かず、
出てもらえた割合の知らせだけが名前だけで書いていた。
"""

from __future__ import annotations

from uuid import UUID

from salescall.core import phone
from salescall.core.access import Tx


def label(name: str, caller_e164: str | None) -> str:
    """回線の名指し：「名前（発信番号）」。番号は見せる形（区切りの決まる番号だけ区切る）。番号が無ければ名前だけ。"""
    if not caller_e164:
        return name
    number = phone.normalize(caller_e164)
    return f"{name}（{number.display or caller_e164}）"


def of(tx: Tx, line_id: UUID) -> str:
    """回線の印から名指しを作る（回線の表の名前と発信番号）。無い回線は LookupError（知らせが指す回線は必ずある）。"""
    row = tx.one("SELECT name, caller_e164 FROM salescall.lines WHERE line_id = %s", (line_id,))
    if row is None:
        raise LookupError(f"回線がありません：{line_id}")
    return label(row["name"], row["caller_e164"])
