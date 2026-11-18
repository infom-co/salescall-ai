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
MAX_PER_LOGIN = 5            # 1つのログインが同時に置いておく数（超えたら、その人の古いものから捨てる。ほかの人のものは捨てない）
MAX_TOTAL = 200 * 1024 * 1024  # 手元の記憶に置く合計の上限（超えたら古いものから捨てる＝サーバーの記憶を守る）
# ⏳ 置き場は画面のサーバー1台の手元の記憶。画面のサーバーを2台以上にするときは、同じ人を同じ台へ送る（Fly.io の設定）か、置き場を
#    データベースの一時の表へ移す（お客様の情報なので、写しを残さない形で）。


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
    """期限（KEEP）を過ぎたものを捨てる（置くときも、取り出すときも）。"""
    for k in [k for k, u in _items.items() if now - u.put_at > KEEP]:
        del _items[k]


def _make_room(login_id: str, adding: int) -> None:
    """置く前に場所をあける：その人の数の上限（その人の古いものから）・合計の大きさの上限（全体の古いものから）。"""
    mine = sorted((k for k, u in _items.items() if u.login_id == login_id), key=lambda k: _items[k].put_at)
    for k in mine[:max(0, len(mine) - MAX_PER_LOGIN + 1)]:
        del _items[k]
    while _items and sum(len(u.data) for u in _items.values()) + adding > MAX_TOTAL:
        del _items[min(_items, key=lambda k: _items[k].put_at)]


def check_size(n: int) -> None:
    """大きさの上限の1か所（読む前＝送られてきた大きさ、置く前＝中身の大きさの両方に使う）。"""
    if n > MAX_BYTES:
        raise TooLarge(f"ファイルが大きすぎます（{MAX_BYTES // (1024 * 1024)}MB まで）")


def put(u: Upload) -> str:
    check_size(len(u.data))
    key = secrets.token_urlsafe(18)
    with _lock:
        _sweep(time.monotonic())
        _make_room(u.login_id, len(u.data))
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
