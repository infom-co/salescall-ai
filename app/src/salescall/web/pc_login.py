"""PC のログインの入口（RFC 8628 の機器のログイン。docs/DASHBOARD.md 1-2。PC の側は agent/pc_login.py＝CL-2）。

PC がログインを始める（`start`）→ 契約者がブラウザでログインし、PC の画面と同じ確かめのコードかを確かめて許可する（`approve`）→
PC が通行証を受け取る（`take`。払い出すのは core の agents.issue だけ・1回だけ）。

待っている間の記録（コード・許可）は、画面のサーバーの手元の記憶にだけ置く（期限 `EXPIRES` 秒。通行証の値は持たない）。
サーバーが入れ替わったら、PC は期限切れとして自動でやり直す（agent/pc_login.until_logged_in）。
⏳ 画面のサーバーを2台以上で動かすなら、待っている間の記録をデータベースに置く形が要る（CL-2 へのお願い 26）。
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

EXPIRES = 600            # 確かめのコードの期限（秒）
INTERVAL = 5             # PC が受け取りを聞き直す間隔（秒）。これより早く聞けば slow_down
_ALPHABET = "BCDFGHJKLMNPQRSTVWXZ"   # 読み違えにくい字だけ（母音・0/O・1/I を除く）


@dataclass
class Pending:
    device_code: str
    user_code: str
    pc_label: str
    replaces: str | None = None
    until: float = field(default_factory=lambda: time.monotonic() + EXPIRES)
    last_poll: float = 0.0
    decision: str | None = None          # None＝待ち／approved／denied
    tenant_id: UUID | None = None
    actor: Any = None                    # 許可した人（core.access.Actor）


_lock = threading.Lock()
_by_device: dict[str, Pending] = {}


def _sweep(now: float) -> None:
    for k in [k for k, p in _by_device.items() if p.until < now]:
        del _by_device[k]


def _user_code() -> str:
    raw = "".join(secrets.choice(_ALPHABET) for _ in range(8))
    return f"{raw[:4]}-{raw[4:]}"


def normalize(code: str) -> str:
    c = "".join(ch for ch in code.upper() if ch.isalnum())
    return f"{c[:4]}-{c[4:]}" if len(c) == 8 else code.strip().upper()


def start(pc_label: str, replaces: str | None = None) -> Pending:
    label = (pc_label or "").strip()[:60] or "名前の無い PC"
    with _lock:
        _sweep(time.monotonic())
        taken = {p.user_code for p in _by_device.values()}
        code = _user_code()
        while code in taken:
            code = _user_code()
        p = Pending(secrets.token_urlsafe(32), code, label, replaces)
        _by_device[p.device_code] = p
        return p


def by_user_code(code: str) -> Pending | None:
    with _lock:
        _sweep(time.monotonic())
        c = normalize(code)
        return next((p for p in _by_device.values() if p.user_code == c and p.decision is None), None)


def decide(code: str, *, approve: bool, tenant_id: UUID, actor: Any) -> Pending | None:
    """契約者が許可する／断る（まだ決めていないコードだけ）。"""
    with _lock:
        _sweep(time.monotonic())
        c = normalize(code)
        p = next((p for p in _by_device.values() if p.user_code == c and p.decision is None), None)
        if p is not None:
            p.decision, p.tenant_id, p.actor = ("approved" if approve else "denied"), tenant_id, actor
        return p


def poll(device_code: str) -> tuple[str, Pending | None]:
    """PC の受け取り：authorization_pending／slow_down／access_denied／expired_token、または ok（許可済み＝ここで手元の記録を消す。1回だけ）。"""
    now = time.monotonic()
    with _lock:
        _sweep(now)
        p = _by_device.get(device_code or "")
        if p is None:
            return "expired_token", None
        if p.decision == "denied":
            del _by_device[device_code]
            return "access_denied", None
        if p.decision is None:
            fast = now - p.last_poll < INTERVAL
            p.last_poll = now
            return ("slow_down" if fast else "authorization_pending"), None
        del _by_device[device_code]
        return "ok", p
