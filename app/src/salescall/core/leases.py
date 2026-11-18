"""回線と仕事の借用・落ちた試行の後始末（見守り役が使う。DESIGN.md 4-2・5-4。表の作り 0063）。持ち主 CL-2。

- **回線の借用**：見守り役が回線を借りて（設定 line.lease 秒の期限つき）、line.lease_renew 秒ごとに延ばす。門は、借りている
  見守り役の予約だけを通す（5-2 の8）。ほかの見守り役が生きて借りている回線は借りられない（データベースの1か所 lease_line）。
- **落ちた試行の後始末**：借用の期限が切れた回線（前に動いていた中央の見守り役が落ちた）と、起動した見守り役が前に借りていた
  回線の、進行中の試行を終わらせる（予約のまま＝取り消し、発信してから＝結果不明）。ほかの見守り役が生きて借りている回線の通話は
  終わらせない。決まった間隔の後始末は、自分が借りている回線を除く（自分の分は、回線の流れを立ち上げる前の後始末だけ）。
  回線を借りるのは中央の見守り役（サーバー）だけ（5-5）。決めるのはデータベースの1か所（recover_lines）。
- **仕事の借用**：決まった間隔の仕事を、2つの見守り役が同時にしないよう、回線と同じ「期限つきの借用」で1つに決める
  （中央の見守り役が入れ替わるときも1つだけが動く。落ちれば期限で次が引き継ぐ）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from salescall.core import settings
from salescall.core.access import Tx


def lease_seconds(tx: Tx) -> int:
    """回線を借りる期限（秒）。"""
    return int(settings.system_value(tx, "line.lease"))


def renew_every(tx: Tx) -> int:
    """回線の借用を延ばす間隔（秒）。"""
    return int(settings.system_value(tx, "line.lease_renew"))


def take_line(tx: Tx, line_id: UUID, holder: str) -> datetime | None:
    """回線を借りる・延ばす（取引の契約者の回線だけ）。借りられたら期限、借りられなければ None。"""
    return tx.one("SELECT salescall.lease_line(%s, %s, %s) AS until", (line_id, holder, lease_seconds(tx)))["until"]


def return_line(tx: Tx, line_id: UUID, holder: str) -> bool:
    """借りていた回線を返す（止めるとき）。"""
    return bool(tx.one("SELECT salescall.return_line(%s, %s) AS ok", (line_id, holder))["ok"])


# 後始末で進行中の試行を終える理由（こちらの不具合の理由＝core/errors.ENDS のうち、後始末だけが使うもの。0238）と根拠の文
LOST = ("worker_lost", "回線の処理・見守り役が、通話を終えないまま止まりました（後始末）")
LEFT = ("storage_stuck", "終わりの記録が書けないまま残った通話です（回線の処理の後始末）")
BROKEN = ("runner_error", "回線の処理が、通話の終わりを書く前に、こちらのプログラムの誤りで落ちて残った通話です（回線の処理の後始末）")


def left_why(error: BaseException | None) -> tuple[str, str]:
    """回線の処理の回が落ちて残った試行を、どの理由で終えるか（1か所。回線の流れが、落ちた誤りを持って次の回の頭へ渡す＝`recover_left`）：データベースが
    書き込みを受けなかった誤り（access.storage_failed の1か所）か、誤りが分からない（流れを立ち上げ直した）なら LEFT
    （storage_stuck。違う行の数に数えない＝伺い5）。ほかの誤り（こちらのプログラムの誤り）は BROKEN（runner_error。違う3行で同じ誤りなら新しい電話を止める
    ＝原則6）。⛔ 2026-11-18 の8回目の見直しまで：どんな誤りで落ちても storage_stuck とし、プログラムの誤りを「違う3行」の数えから外していた。"""
    from salescall.core import access
    if error is None or access.storage_failed(error):
        return LEFT
    return BROKEN


def recover(tx: Tx, holder: str | None = None, line_id: UUID | None = None, *,
            except_holder: str | None = None, why: tuple[str, str] = LOST) -> list[dict[str, Any]]:
    """落ちた試行の後始末（取引の契約者の回線）。holder＝その見守り役が借りている回線（起動したとき。line_id を渡せば、その回線
    だけ＝回線の流れが止まったとき）。holder が None なら、借用の期限が切れた回線（前に動いていた見守り役の分）。
    except_holder＝呼んだ見守り役の名前（自分が借りている回線は、期限が切れていても除く＝データベースに届かないあいだに期限が切れても、
    まだ話している自分の通話を終わらせない）。終わらせた数を返す。
    進行中の試行は、こちらの不具合（why＝理由と根拠の文。初めは回線の処理・見守り役が止まった＝LOST）で終える（データベースの1か所
    recover_lines。番号・回線・行の回数のどれにも数えない＝原則6。⛔ 0238 の前は理由を残さず、お客様が出る前なら回線のせい・番号の
    4回に数えた）。"""
    return tx.all("SELECT line_id, released, ended FROM salescall.recover_lines(%s, %s, %s, %s, %s)",
                  (holder, line_id, except_holder, *why))


def recover_left(tx: Tx, holder: str, line_id: UUID, *, why: tuple[str, str] = LEFT) -> list[dict[str, Any]]:
    """回線ごとの流れの毎回の頭で呼ぶ：この回線に、前の回の電話（予約・進行中の試行）が残っていれば後始末する（起動・流れが止まったときと
    同じ `recover` の1か所。残っていなければ何もしない＝回線の行を毎回ロックしない）。回の頭では、この流れの電話は1本も進んでいないので、
    残っているのは、終わりの記録が書けずに残った試行（⛔ 見直し26c の 1：その回線は門に「使用中」で断られ続け、二度とかけなかった）＝
    データベースへの書き込みが進まなかった・こちらのプログラムの誤りで落ちた（why＝`left_why` の1か所が、前の回の落ちた誤りから決めたもの）。"""
    if tx.one("SELECT salescall.line_has_leftovers(%s) AS left", (line_id,))["left"]:
        return recover(tx, holder, line_id, why=why)
    return []


def take_job(tx: Tx, key: str, holder: str, seconds: int) -> bool:
    """決まった間隔の仕事を借りる（システムの処理だけ）。ほかの見守り役が期限の中で借りていれば False。"""
    return bool(tx.one("SELECT salescall.take_job(%s, %s, %s) AS ok", (key, holder, seconds))["ok"])


def return_job(tx: Tx, key: str, holder: str) -> None:
    """仕事を返す（終わったら）。"""
    tx.one("SELECT salescall.return_job(%s, %s) AS ok", (key, holder))
