"""中央の見守り役から見た、つないでいる PC の部品（DESIGN.md 5-5）。持ち主 CL-2。

中央の見守り役（main・jobs・line）は、PC の部品へのつなぎ方（つなぎ口＝channel）を知らない。この形だけを使う（試験は代わりを渡す）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from salescall.core.backup import Writer
from salescall.core.dial_exit import LineDriver


@dataclass(frozen=True)
class Attached:
    """いまつないでいる回線（その契約者の、使っている回線だけ＝データベースが確かめたもの）と、つないでいる PC・そのつなぎの番号
    （つなぎ直せば変わる＝中央の見守り役は、替わったら回線の流れを止めて立ち上げ直す）。"""
    tenant_id: UUID
    pc_id: UUID
    conn: int = 0


class Links(Protocol):
    def lines(self) -> dict[UUID, Attached]:
        """いま、どの回線が、どの契約者のどの PC につながっているか（かけてよい PC の部品の版のものだけ）。"""
        ...

    def driver(self, line_id: UUID, at: Attached) -> LineDriver:
        """そのつなぎの、その回線の電話の部品（PC の部品へ頼む形。通話の声も、これを通る）。**作ったときのつなぎに決まる**（別の PC・
        つなぎ直した新しいつなぎへ、通話の途中の頼み・声を送らない）。"""
        ...

    def writer(self, tenant_id: UUID) -> Writer | None:
        """その契約者の控えを書く PC の部品（つないでいる PC。ハードディスクと鍵がある PC を先に。つないでいる PC が無ければ None）。"""
        ...

    def whole_writer(self) -> Writer | None:
        """データベース全体の控えを書く PC の部品（サーバーの設定で決めた、管理者自身の契約者の PC。つないでいなければ None）。"""
        ...


class NoLinks:
    """PC の部品がつながっていない（つなぎ口を開かない動かし方・試験）。"""

    def lines(self) -> dict[UUID, Attached]:
        return {}

    def driver(self, line_id: UUID, at: Attached) -> LineDriver:
        raise LookupError(f"その回線は、つないでいません：{line_id}")

    def writer(self, tenant_id: UUID) -> Writer | None:
        return None

    def whole_writer(self) -> Writer | None:
        return None
