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
    """いまつないでいる回線（その契約者の、使っている回線だけ＝データベースが確かめたもの）と、つないでいる PC。"""
    tenant_id: UUID
    pc_id: UUID


class Links(Protocol):
    def lines(self) -> dict[UUID, Attached]:
        """いま、どの回線が、どの契約者のどの PC につながっているか（かけてよい PC の部品の版のものだけ）。"""
        ...

    def driver(self, line_id: UUID, pc_id: UUID) -> LineDriver:
        """その PC の、その回線の電話の部品（PC の部品へ頼む形。通話の声も、これを通る）。**作ったときの PC に決まる**（同じ回線を
        別の PC がつなぎ直しても、通話の途中の頼み・声を、新しい PC へ送らない）。"""
        ...

    def writer(self, tenant_id: UUID) -> Writer | None:
        """その契約者の控えを書く PC の部品（つないでいて、控え用のハードディスクと鍵があるもの。無ければ None）。"""
        ...

    def whole_writer(self) -> Writer | None:
        """データベース全体の控えを書く、管理者の PC の部品（サーバーの設定で決めた PC。つないでいなければ None）。"""
        ...


class NoLinks:
    """PC の部品がつながっていない（つなぎ口を開かない動かし方・試験）。"""

    def lines(self) -> dict[UUID, Attached]:
        return {}

    def driver(self, line_id: UUID, pc_id: UUID) -> LineDriver:
        raise LookupError(f"その回線は、つないでいません：{line_id}")

    def writer(self, tenant_id: UUID) -> Writer | None:
        return None

    def whole_writer(self) -> Writer | None:
        return None
