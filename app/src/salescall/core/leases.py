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


def recover(tx: Tx, holder: str | None = None, line_id: UUID | None = None, *,
            except_holder: str | None = None) -> list[dict[str, Any]]:
    """落ちた試行の後始末（取引の契約者の回線）。holder＝その見守り役が借りている回線（起動したとき。line_id を渡せば、その回線
    だけ＝回線の流れが止まったとき）。holder が None なら、借用の期限が切れた回線（前に動いていた見守り役の分）。
    except_holder＝呼んだ見守り役の名前（自分が借りている回線は、期限が切れていても除く＝データベースに届かないあいだに期限が切れても、
    まだ話している自分の通話を終わらせない）。終わらせた数を返す。"""
    return tx.all("SELECT line_id, released, ended FROM salescall.recover_lines(%s, %s, %s)", (holder, line_id, except_holder))


def recover_left(tx: Tx, holder: str, line_id: UUID) -> list[dict[str, Any]]:
    """回線ごとの流れの毎回の頭で呼ぶ：この回線に、前の回の電話（予約・進行中の試行）が残っていれば後始末する（起動・流れが止まったときと
    同じ `recover` の1か所。残っていなければ何もしない＝回線の行を毎回ロックしない）。回の頭では、この流れの電話は1本も進んでいないので、
    残っているのは、終わりの記録が書けずに残った試行（⛔ 見直し26c の 1：その回線は門に「使用中」で断られ続け、二度とかけなかった）。"""
    if tx.one("SELECT salescall.line_has_leftovers(%s) AS left", (line_id,))["left"]:
        return recover(tx, holder, line_id)
    return []


def take_job(tx: Tx, key: str, holder: str, seconds: int) -> bool:
    """決まった間隔の仕事を借りる（システムの処理だけ）。ほかの見守り役が期限の中で借りていれば False。"""
    return bool(tx.one("SELECT salescall.take_job(%s, %s, %s) AS ok", (key, holder, seconds))["ok"])


def return_job(tx: Tx, key: str, holder: str) -> None:
    """仕事を返す（終わったら）。"""
    tx.one("SELECT salescall.return_job(%s, %s) AS ok", (key, holder))
