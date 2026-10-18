"""発信の出口（DESIGN.md 5-3）。持ち主 CL-2。

- 回線の部品の「電話をかける」（`start_call`）を呼んでよいのは、この部品の `dial` だけ（I-1。app/checks が見張る）。
- 予約証が「予約のまま・期限内」のときだけ、1つの書き換えで発信中にし（core/attempts.py の start_dialing）、
  **その取引を確定してから** 回線に電話をかけさせる（確定の前にかけると、取り消された予約で発信しうる）。
  予約証が使えなければ、かけない（False）。停止・発信禁止の登録で取り消された予約も、ここで止まる（5-2-1 の2）。
- 回線の部品が「かけていない」と確かに言える失敗（`CallNotPlaced`）は、試行を「発信に失敗」にする（声は届いていない）。
  それ以外の失敗は、かかったかどうか分からないので「結果不明」（technical_error）にする（すぐかけ直さない。4-2）。
- **回線（スマホ）が、まだ前の通話の途中なら、かけない**（`LineDriver.in_call`。予約を戻す。2026-10-18 の見直し：回線ごとの処理や
  通話を進める部品が落ちると、その試行は終わった扱いになり、データベースの「1回線に進行中は1本」の守りが外れる。ところが
  スマホの通話は PC の処理が終わっても続くので、立ち上げ直した処理が、まだ通話中のスマホで二重にかけうる。回線が空いているかを
  知っているのは回線の部品だけなので、かける直前に必ず聞く＝決まりはこの1か所）。
- 結果は `Outcome`（かけた・予約証が使えない・回線が通話中・かけられなかった）。かけたときだけ真。回線ごとの処理は、回線が通話中・
  かけられなかったときは、その回線を休ませる（かけられなかったことは回線の異常として数える＝続けば回線を止める）。
- 本番のPCの緊急停止の印（core/emergency.py）が効いているあいだは、かけない（予約を戻す）。かけてよいかの判断（門）は増やさない：
  発信の出口の物理的な止め（ネットが切れているあいだに押した停止を、ネットが戻っても守るため）。印が効いているかは
  `emergency.engaged` の1か所が、発信中にする同じ取引の中で決める（画面から解かれた停止の印は、そこで消える）。
"""

from __future__ import annotations

from enum import Enum
from typing import Protocol
from uuid import UUID

from salescall.core import attempts, emergency
from salescall.core.access import Access, Actor


class CallNotPlaced(Exception):
    """回線の部品が、電話をかけていないと確かに言える失敗（例：アプリが番号を受け付けなかった）。"""


class LineDriver(Protocol):
    """回線の部品（電話接続。担当はまだ振られていない）が持つ形。"""

    def in_call(self) -> bool:
        """回線（スマホ）が、いま通話の途中か（呼び出し中を含む）。分からなければ True を返す（かけない側に倒す）。"""
        ...

    def start_call(self, phone_e164: str) -> None: ...


class Outcome(Enum):
    """発信の出口の結果。かけたときだけ真。"""
    DIALED = "dialed"               # かけた
    UNUSABLE = "unusable"           # 予約証が使えない（期限切れ・取り消し済み）・緊急停止：かけていない
    LINE_IN_CALL = "line_in_call"   # 回線がまだ前の通話の途中：かけずに予約を戻した
    NOT_PLACED = "not_placed"       # 回線の部品がかけられなかった（CallNotPlaced）：試行は「発信に失敗」

    def __bool__(self) -> bool:
        return self is Outcome.DIALED


def dial(access: Access, actor: Actor, tenant_id: UUID, client_id: UUID, attempt_id: UUID, driver: LineDriver) -> Outcome:
    """予約証の試行に発信する。結果を返す（かけたときだけ真）。"""
    try:
        busy = driver.in_call()
    except Exception:  # noqa: BLE001 答えられない＝分からない：通話中とみなす（かけない側）
        busy = True
    if busy:   # まだ前の通話の途中：かけずに予約を戻す（予約証が使えるかに関係なく、二重にかけない）
        access.in_client(actor, tenant_id, client_id, lambda tx: attempts.release(tx, [attempt_id]))
        return Outcome.LINE_IN_CALL

    def begin(tx):
        if emergency.engaged(tx) is not None:   # 本番のPCの緊急停止：かけずに予約を戻す（発信中にする直前に見る）
            attempts.release(tx, [attempt_id])
            return None
        if not attempts.start_dialing(tx, attempt_id):
            return None
        return tx.one("SELECT phone_e164 FROM salescall.attempts WHERE attempt_id = %s", (attempt_id,))["phone_e164"]

    phone = access.in_client(actor, tenant_id, client_id, begin)
    if phone is None:
        return Outcome.UNUSABLE
    if emergency.engaged() is not None:   # 発信中にした取引のあいだに押された緊急停止：回線に渡す直前にもう一度見る（かけていない）
        access.in_client(actor, tenant_id, client_id, lambda tx: attempts.move(tx, attempt_id, "call_failed"))
        return Outcome.UNUSABLE
    try:
        driver.start_call(phone)
    except CallNotPlaced:
        access.in_client(actor, tenant_id, client_id, lambda tx: attempts.move(tx, attempt_id, "call_failed"))
        return Outcome.NOT_PLACED
    except BaseException:
        access.in_client(actor, tenant_id, client_id, lambda tx: attempts.move(tx, attempt_id, "technical_error"))
        raise
    return Outcome.DIALED
