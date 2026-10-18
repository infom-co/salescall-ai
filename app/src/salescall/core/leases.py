"""回線と仕事の借用・落ちた試行の後始末（見守り役が使う。DESIGN.md 4-2・5-4。表の作り 0063）。持ち主 CL-2。

- **回線の借用**：見守り役が回線を借りて（設定 line.lease 秒の期限つき）、line.lease_renew 秒ごとに延ばす。門は、借りている
  見守り役の予約だけを通す（5-2 の8）。ほかの見守り役が生きて借りている回線は借りられない（データベースの1か所 lease_line）。
- **落ちた試行の後始末**：借用の期限が切れた回線（PC が落ちた・ネットが切れた）と、起動した見守り役が前に借りていた回線の、
  進行中の試行を終わらせる（予約のまま＝取り消し、発信してから＝結果不明）。ほかの見守り役が生きて借りている回線の通話は
  終わらせない。決めるのはデータベースの1か所（recover_lines）。
- **仕事の借用**：決まった間隔の仕事を、2つの見守り役が同時にしないよう、回線と同じ「期限つきの借用」で1つに決める
  （PC が2台あっても1つだけが動く。落ちれば期限で次が引き継ぐ）。
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


def recover(tx: Tx, holder: str | None = None, line_id: UUID | None = None) -> list[dict[str, Any]]:
    """落ちた試行の後始末（取引の契約者の回線）。holder＝その見守り役が借りている回線（起動したとき。line_id を渡せば、その回線
    だけ＝回線の処理が落ちたとき）。holder が None なら、借用の期限が切れた回線（ほかの PC が落ちた分）。終わらせた数を返す。"""
    return tx.all("SELECT line_id, released, ended FROM salescall.recover_lines(%s, %s)", (holder, line_id))


def take_job(tx: Tx, key: str, holder: str, seconds: int) -> bool:
    """決まった間隔の仕事を借りる（システムの処理だけ）。ほかの見守り役が期限の中で借りていれば False。"""
    return bool(tx.one("SELECT salescall.take_job(%s, %s, %s) AS ok", (key, holder, seconds))["ok"])


def return_job(tx: Tx, key: str, holder: str) -> None:
    """仕事を返す（終わったら）。"""
    tx.one("SELECT salescall.return_job(%s, %s) AS ok", (key, holder))
