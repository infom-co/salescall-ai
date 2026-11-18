"""取り込むファイルの一時置き場（docs/DASHBOARD.md 5-8）。

ファイルを選んでから登録するまで（列の対応付け → プレビュー → 登録）、同じファイルを使う。ファイルにはお客様の情報が入るので、
**画面のサーバーの手元の記憶にだけ置き、ディスク・データベース・記録には書かない**。置くのは決まった時間だけ（`KEEP`）・決まった数と
大きさまで。取り出せるのは、置いたのと同じログインの人・同じ依頼者・同じ実施回だけ（ほかは「無い」と同じ）。
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

KEEP = 30 * 60               # 置いておく秒数
MAX_BYTES = 10 * 1024 * 1024  # 1つのファイルの大きさの上限
MAX_FILES = 50               # 同時に置いておく数（超えたら古いものから捨てる）


@dataclass
class Upload:
    login_id: str
    client_id: UUID
    run_id: UUID
    file_name: str
    data: bytes
    sheet_name: str | None = None
    encoding: str | None = None
    put_at: float = field(default_factory=time.monotonic)
    mapping: dict[str, str] = field(default_factory=dict)
    preview: dict[str, Any] = field(default_factory=dict)   # 最後のプレビューの指紋・AI の読み・確かめた読み（登録にそのまま渡す）


class TooLarge(ValueError):
    pass


_lock = threading.Lock()
_items: dict[str, Upload] = {}


def _sweep(now: float) -> None:
    for k in [k for k, u in _items.items() if now - u.put_at > KEEP]:
        del _items[k]
    while len(_items) >= MAX_FILES:
        del _items[min(_items, key=lambda k: _items[k].put_at)]


def put(u: Upload) -> str:
    if len(u.data) > MAX_BYTES:
        raise TooLarge(f"ファイルが大きすぎます（{MAX_BYTES // (1024 * 1024)}MB まで）")
    key = secrets.token_urlsafe(18)
    with _lock:
        _sweep(time.monotonic())
        _items[key] = u
    return key


def get(key: str, login_id: str, client_id: UUID, run_id: UUID) -> Upload | None:
    with _lock:
        _sweep(time.monotonic())
        u = _items.get(key)
    if u is None or (u.login_id, u.client_id, u.run_id) != (login_id, client_id, run_id):
        return None
    return u


def drop(key: str) -> None:
    with _lock:
        _items.pop(key, None)
