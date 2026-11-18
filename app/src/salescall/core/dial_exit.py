"""発信の出口（DESIGN.md 5-3）。持ち主 CL-2。

- 回線の部品の「電話をかける」（`start_call`）を呼んでよいのは、この部品の `dial` だけ（I-1。app/checks が見張る）。
- 予約証が「予約のまま・期限内」のときだけ、1つの書き換えで発信中にし（core/attempts.py の start_dialing）、
  **その取引を確定してから** 回線に電話をかけさせる（確定の前にかけると、取り消された予約で発信しうる）。
  予約証が使えなければ、かけない（False）。停止・発信禁止の登録で取り消された予約も、ここで止まる（5-2-1 の2）。
- 回線の部品が「かけようとして、かけられなかった」と確かに言える失敗（`CallNotPlaced`）は、試行を「発信に失敗」にする（声は
  届いていない。どの回数にも数えない＝数え方の1か所 salescall.dial_kind）。**だれのせいかは、ここでもアプリの言い分でも決めない**：
  同じ回線の前後の電話の証拠で決める（状態の表の試行のせいの欄＝DESIGN.md 17章。番号のせい＝番号の欄の決まった順でかけ直し、続けば
  その番号へはかけない／回線のせい＝番号には数えず、回線の欄が休みになる）。
  ⛔ 0129：前は「番号のせい」（NumberRejected）と「回線のせい・分からない」をアプリの言い分で分け、どの番号も受け付けない回線では
  行がすべて終わり、1つの番号だけが続けてかけられないときは回線が止まり続けた（見直し18a の問題2・3）。
  それ以外の失敗は、かかったかどうか分からないので「結果不明」（technical_error）にする（すぐかけ直さない。4-2）。
- **回線（スマホ）が、まだ前の通話の途中なら、かけない**（`LineDriver.in_call`。予約を戻す。2026-10-18 の見直し：回線ごとの処理や
  通話を進める部品が落ちると、その試行は終わった扱いになり、データベースの「1回線に進行中は1本」の守りが外れる。ところが
  スマホの通話はサーバーの回線ごとの流れが終わっても続くので、立ち上げ直した処理が、まだ通話中のスマホで二重にかけうる。回線が空いているかを
  知っているのは回線の部品だけなので、かける直前に必ず聞く＝決まりはこの1か所）。
- 結果は `Outcome`（かけた・予約証が使えない・回線が通話中・かけられなかった・PC の部品がかけようとしなかった）。かけたときだけ真。
  回線ごとの処理は、回線が通話中・PC の部品がかけようとしなかったときは、その回線を休ませる。かけられなかったときは、だれのせいかを
  証拠で見て、回線のせいなら休ませて回線の異常として数える（supervisor/line.py の1か所）。
- **かけたか分からないとき・通話を進める部品が落ちたときは、おわびの録音を流して切ってもらう**（`abandon`。お客様を無言で放置しない）。
- 本番の PC の上で緊急停止が押されている・PC の部品がつながっていない・PC の部品が前の頼みを待つうちに答えの期限を過ぎたときは、かけない（`CallWithheld`。DESIGN.md 5-5・9-2）：試行は
  発信まで進まなかった予約と同じく取り消す（`attempts.withheld`。お客様の電話は鳴っていないので、判定せず、どの数にも入れず、
  行の予定はそのまま。⛔ 2026-10-22 の見直し14：前は「発信に失敗」にして、判定が回数を使い、目的も消えた）。回線の異常には数えない。かけてよいかの判断（門）は増やさない：発信の出口の物理的な止め
  （ネットが切れているあいだに押した停止を、ネットが戻っても守るため）。
"""

from __future__ import annotations

from enum import Enum
from typing import Protocol
from uuid import UUID

from salescall.core import attempts
from salescall.core.access import Access, Actor
from salescall.link.phone import CallNotPlaced, CallWithheld

__all__ = ["CallNotPlaced", "CallWithheld", "LineDriver", "Outcome", "abandon", "dial"]


class LineDriver(Protocol):
    """回線の部品の形（中央の見守り役では、PC の部品へ頼む形＝supervisor.channel。PC の側の電話の部品は salescall.link.phone）。"""

    def in_call(self) -> bool:
        """回線（スマホ）が、いま通話の途中か（呼び出し中を含む）。分からなければ True を返す（かけない側に倒す）。"""
        ...

    def start_call(self, phone_e164: str) -> None: ...

    def apologize_and_hang_up(self) -> None:
        """おわびの録音を流して切る（通話中でなければ、何もしない）。"""
        ...


def abandon(driver: LineDriver) -> None:
    """サーバーの側の誤りで、通話を続けられない（かけたか分からない・通話を進める部品が落ちた）：お客様を無言で放置しないよう、
    おわびの録音を流して切ってもらう（**この判断はこの1か所**。PC とのつながりが切れたときは、PC の部品が自分で同じことをする）。
    頼めなくても止めない（つながりが無ければ、PC の部品の側でおわびして切る）。"""
    try:
        driver.apologize_and_hang_up()
    except Exception:  # noqa: BLE001
        pass


class Outcome(Enum):
    """発信の出口の結果。かけたときだけ真。"""
    DIALED = "dialed"               # かけた
    UNUSABLE = "unusable"           # 予約証が使えない（期限切れ・取り消し済み）：かけていない
    LINE_IN_CALL = "line_in_call"   # 回線がまだ前の通話の途中：かけずに予約を戻した
    NOT_PLACED = "not_placed"       # かけようとして、かけられなかった（CallNotPlaced）：試行は「発信に失敗」（だれのせいかは証拠で）
    WITHHELD = "withheld"           # PC の部品がかけなかった（PC の上で緊急停止・PC がつながっていない）：試行は取り消し

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
        if not attempts.start_dialing(tx, attempt_id):
            return None
        return tx.one("SELECT phone_e164 FROM salescall.attempts WHERE attempt_id = %s", (attempt_id,))["phone_e164"]

    phone = access.in_client(actor, tenant_id, client_id, begin)
    if phone is None:
        return Outcome.UNUSABLE
    try:
        driver.start_call(phone)
    except CallWithheld:   # PC の上で緊急停止が押されている・つながっていない：かけていない（回線の異常ではない。鳴っていない
        # ので、判定せず、どの数にも入れない＝発信まで進まなかった予約と同じ）
        access.in_client(actor, tenant_id, client_id, lambda tx: attempts.withheld(tx, attempt_id))
        return Outcome.WITHHELD
    except CallNotPlaced:    # かけられなかった：「発信に失敗」（どの回数にも数えない。だれのせいかは証拠で＝状態の表の試行のせいの欄）
        access.in_client(actor, tenant_id, client_id, lambda tx: attempts.move(tx, attempt_id, "call_failed"))
        return Outcome.NOT_PLACED
    except BaseException:
        abandon(driver)   # かけたか分からない：かかっていれば、AI のいない電話にしない（記録より先に＝データベースが止まっていても、お客様を待たせない）
        access.in_client(actor, tenant_id, client_id, lambda tx: attempts.move(tx, attempt_id, "technical_error"))
        raise
    return Outcome.DIALED
