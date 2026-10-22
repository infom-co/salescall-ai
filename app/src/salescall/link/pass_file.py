"""PC の通行証をしまう場所と形（PC の部品が読み書きし、移る間の形では、同じ PC で動く中央の見守り役の命令 `pc-pass` も書く。
DESIGN.md 5-5）。持ち主 CL-2。

- しまう場所は、その Windows の利用者のフォルダ（ほかの OS は利用者のフォルダ）。Windows は、その利用者だけが開ける形にしてから
  書く（DPAPI）。ほかの OS は、本人だけが読める許しで書く。書きかけを残さない。画面・記録に出さない。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import UUID

from salescall.link.basics import PcPass


class PassUnreadable(Exception):
    """しまった通行証を読めない・しまえない（理由は日本語。通行証の値は書かない）。"""


def pass_path() -> Path:
    """しまう場所（その Windows の利用者のフォルダ。ほかの OS は利用者のフォルダ）。"""
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "SalesCall" / "pc-pass.bin"
    return Path.home() / ".config" / "salescall" / "pc-pass.bin"


def save(got: PcPass, path: Path | None = None) -> None:
    """通行証をしまう（書きかけを残さない。Windows はその利用者だけが開ける形にして、ほかの OS は本人だけが読める許しで）。"""
    path = path or pass_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = _protect(json.dumps({"tenant_id": str(got.tenant_id), "pc": str(got.pc_id), "token": got.token}).encode("utf-8"))
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    tmp.replace(path)


def load(path: Path | None = None) -> PcPass | None:
    """しまった通行証（無ければ None）。読めない通行証（壊れた・Windows の利用者が違う）も、無いものとする＝ログインし直しの道へ
    （判断はこの1か所。⛔ 2026-10-22 の見直し：前は誤りが外へ出て PC の部品が終わり、次にログオンするまで止まったままだった）。
    新しい通行証をしまえば、上書きされる。"""
    path = path or pass_path()
    if not path.exists():
        return None
    try:
        d = json.loads(_unprotect(path.read_bytes()).decode("utf-8"))
        return PcPass(UUID(d["tenant_id"]), UUID(d["pc"]), str(d["token"]))
    except (PassUnreadable, OSError, ValueError, KeyError, TypeError):
        return None


def logout(path: Path | None = None) -> bool:
    """しまった通行証を消す。消したら True。"""
    path = path or pass_path()
    if not path.exists():
        return False
    path.unlink()
    return True


def _protect(data: bytes) -> bytes:
    """Windows：その利用者だけが開ける形にする（DPAPI）。ほかの OS：そのまま（ファイルの許しで守る）。"""
    return _dpapi(data, protect=True) if os.name == "nt" else data


def _unprotect(data: bytes) -> bytes:
    return _dpapi(data, protect=False) if os.name == "nt" else data


def _dpapi(data: bytes, *, protect: bool) -> bytes:   # pragma: no cover（Windows だけ）
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data, len(data))
    src, dst = Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), Blob()
    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    if not fn(ctypes.byref(src), None, None, None, None, 0, ctypes.byref(dst)):
        raise PassUnreadable("PC の通行証をしまう・読むことができませんでした（Windows の利用者が違う恐れ）")
    try:
        return ctypes.string_at(dst.pbData, dst.cbData)
    finally:
        kernel32.LocalFree(dst.pbData)
