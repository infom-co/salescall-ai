"""サーバーと PC の部品の両方が使う、小さな決まり（**それぞれ、この1か所**）。持ち主 CL-2。

- `PcPass`：PC の通行証（サーバーが払い出し、PC がしまって、つなぐときに渡す）。
- `Env`：環境変数のうち、決めた名前だけを読む（ほかの値は読まない。値は出さない＝出すのは「入っているか」だけ）。
- `load`：設定の `モジュール:名前` から部品を作る（ほかの担当の部品）。
- `backoff`：続けて失敗するほど、次に試すまでの間を延ばす（上限つき）。
"""

from __future__ import annotations

import importlib
import os
from dataclasses import dataclass
from typing import Any
from uuid import UUID


@dataclass(frozen=True)
class PcPass:
    """PC の通行証（どの契約者のどの PC か＋通行証の文字）。"""
    tenant_id: UUID
    pc_id: UUID
    token: str

    def __repr__(self) -> str:   # 記録や誤りの表示に、通行証を出さない
        return f"PcPass(tenant_id={self.tenant_id!r}, pc_id={self.pc_id!r}, token=***)"


class Env:
    """環境変数のうち、names の名前だけ（空の値は無いものとする）。"""

    def __init__(self, names: tuple[str, ...], environ: dict[str, str] | None = None) -> None:
        env = os.environ if environ is None else environ
        self.names = names
        self.values = {name: env[name] for name in names if env.get(name, "").strip() != ""}

    def get(self, name: str) -> str | None:
        return self.values.get(name, "").strip() or None

    def status(self) -> list[str]:
        """どの設定が入っているか（値は出さない）。"""
        return [f"{name}：{'入っています' if name in self.values else '入っていません'}" for name in self.names]


def load(spec: str, *args: Any) -> Any:
    """設定の `モジュール:名前` から部品を作る（名前は部品を返す関数。args をそのまま渡す）。"""
    module, _, name = spec.partition(":")
    if not module or not name:
        raise ValueError(f"部品は「モジュール:名前」で書きます（{spec!r}）")
    return getattr(importlib.import_module(module), name)(*args)


def backoff(fails: int, first: float, most: float) -> float:
    """fails 回続けて失敗したあと、次に試すまでの秒数（first から倍ずつ。most まで）。"""
    return min(first * 2 ** min(fails, 30), most)
