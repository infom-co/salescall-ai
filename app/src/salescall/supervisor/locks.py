"""PC の中の印（同じ PC で、同じものが2つ動かないように。DESIGN.md 5-4）。持ち主 CL-2。

印のファイルを、ほかに開けないように押さえる。押さえた処理が落ちれば、OS が外す（落ちたまま押さえ続けることは無い）。
印のファイルは、PC 全体で1つの場所に置く（`lock_dir`。Windows はすべての利用者に共通の ProgramData の下。ユーザーごとの一時フォルダに
置くと、1台の PC の2人の Windows の利用者が、それぞれ見守り役を動かせ、同じスマホで二重にかけうる＝2026-10-18 の見直し）。
印のファイルを開けない（ほかの利用者が作り、開く権限が無い）ときも「もう動いている」とみなす（動かさない側に倒す）。
- 見守り役の本体：契約者ごとに1つ（`salescall-supervisor-<契約者>.lock`）。
- 回線ごとの処理：回線ごとに1つ（`salescall-line-<回線>.lock`）。本体は、押さえられているかを `line_in_use_here` で見る。
"""

from __future__ import annotations

import os
import tempfile
import uuid
from pathlib import Path
from typing import Any
from uuid import UUID


class AlreadyRunning(Exception):
    """この PC で、同じものがもう動いている。"""


class SingleInstance:
    def __init__(self, path: Path) -> None:
        self.path, self.file = path, None

    def __enter__(self) -> "SingleInstance":
        try:
            self.file = open(self.path, "a+b")
        except OSError:
            raise AlreadyRunning(f"この PC の印のファイルを開けません（ほかの利用者が動かしている恐れ。印：{self.path}）") from None
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


def lock_dir() -> Path:
    """印のファイルを置く、PC 全体で1つの場所。"""
    if os.name == "nt":
        d = Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "SalesCall"
    else:
        d = Path("/tmp") if Path("/tmp").is_dir() else Path(tempfile.gettempdir())
    d.mkdir(parents=True, exist_ok=True)
    return d


def machine_id() -> str:
    """OS が PC ごとに持つ番号（見守り役の名前に使う。同じ名前の PC を見分ける）。読めなければ、ネットの機器の番号。"""
    if os.name == "nt":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography") as k:
                return str(winreg.QueryValueEx(k, "MachineGuid")[0])
        except OSError:
            pass
    for f in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            got = Path(f).read_text().strip()
            if got:
                return got
        except OSError:
            pass
    return f"{uuid.getnode():012x}"


def line_lock(line_id: UUID) -> Path:
    return lock_dir() / f"salescall-line-{line_id}.lock"


def line_in_use_here(line_id: UUID) -> bool:
    """この PC で、その回線の処理が動いているか（印が押さえられているか）。"""
    try:
        with SingleInstance(line_lock(line_id)):
            return False
    except AlreadyRunning:
        return True
