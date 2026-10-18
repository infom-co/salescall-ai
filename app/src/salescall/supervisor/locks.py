"""PC の中の印（同じ PC で、同じものが2つ動かないように。DESIGN.md 5-4）。持ち主 CL-2。

印のファイルを、ほかに開けないように押さえる。押さえた処理が落ちれば、OS が外す（落ちたまま押さえ続けることは無い）。
- 見守り役の本体：契約者ごとに1つ（`salescall-supervisor-<契約者>.lock`）。
- 回線ごとの処理：回線ごとに1つ（`salescall-line-<回線>.lock`）。本体は、押さえられているかを `line_in_use_here` で見る。
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any
from uuid import UUID


class AlreadyRunning(Exception):
    """この PC で、同じものがもう動いている。"""


class SingleInstance:
    def __init__(self, path: Path) -> None:
        self.path, self.file = path, None

    def __enter__(self) -> "SingleInstance":
        self.file = open(self.path, "a+b")
        self.file.seek(0)   # どの処理も、同じ位置（頭の1バイト）を押さえる
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            self.file = None
            raise AlreadyRunning(f"この PC で、もう動いています（印：{self.path}）") from None
        return self

    def __exit__(self, *exc: Any) -> None:
        if self.file is not None:
            self.file.close()


def line_lock(line_id: UUID) -> Path:
    return Path(tempfile.gettempdir()) / f"salescall-line-{line_id}.lock"


def line_in_use_here(line_id: UUID) -> bool:
    """この PC で、その回線の処理が動いているか（印が押さえられているか）。"""
    try:
        with SingleInstance(line_lock(line_id)):
            return False
    except AlreadyRunning:
        return True
