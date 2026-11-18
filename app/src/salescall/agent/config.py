"""PC の部品の設定（契約者の PC。DESIGN.md 5-5）。読むのは `load` の1か所。持ち主 CL-2。

PC の環境変数から、下の表の名前（SALESCALL_ で始まるもの）だけを読む。**ほかの値は読まない**。**値は画面・記録に出さない**（出すのは
「入っているか」だけ＝`status`）。PC はデータベースにつながないので、データベースの接続は入れない（入れる所が無い）。

| 名前 | 中身 | 要るとき |
|---|---|---|
| SALESCALL_SERVER_URL | SalesCall AI のサーバーの場所（https://…。ログインで使う） | いつも |
| SALESCALL_HUB_URL | サーバーのつなぎ口（wss://…。無ければ SALESCALL_SERVER_URL の https を wss にして /link を足した所） | 場所を変えるとき |
| SALESCALL_LINES | この PC につないだ回線（回線の ID をカンマで区切る。サーバーが、その契約者の回線かを確かめ直す） | 電話をかけるとき |
| SALESCALL_PHONE | 電話の部品を作る関数（`モジュール:名前`。回線の ID を受け取る。電話接続の担当） | 電話をかけるとき |
| SALESCALL_BACKUP_DRIVE | 控え用のハードディスクの一番上（例 E:\\） | 控えを書く PC |
| SALESCALL_BACKUP_KEY | 控えの鍵の元（Proton Pass で作った文字。読むのは link.sealing.load_key の1か所） | 控えを書く PC |
| SALESCALL_UPDATER | プログラムを自動で入れ替える部品を作る関数（インストーラーが入れる。⏳ インストーラーと一緒に作る） | 自動の更新 |
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

from salescall.link.basics import Env

NAMES = ("SALESCALL_SERVER_URL", "SALESCALL_HUB_URL", "SALESCALL_LINES", "SALESCALL_PHONE", "SALESCALL_BACKUP_DRIVE",
         "SALESCALL_BACKUP_KEY", "SALESCALL_UPDATER")
_LOCAL = ("127.0.0.1", "localhost", "::1")


class ConfigMissing(Exception):
    """要る設定が入っていない（名前と入れ方だけを書く。値は書かない）。"""


@dataclass(frozen=True)
class Config:
    server_url: str | None
    hub_url: str
    lines: tuple[UUID, ...]
    phone: str | None
    backup_drive: Path | None
    updater: str | None
    environ: dict[str, str] = field(repr=False, default_factory=dict)   # 控えの鍵は link.sealing.load_key がここから読む


def gather(environ: dict[str, str] | None = None) -> dict[str, str]:
    """環境変数のうち、表の名前のものだけ（ほかの値は読まない）。"""
    return Env(NAMES, environ).values


def load(environ: dict[str, str] | None = None) -> Config:
    env = Env(NAMES, environ)
    maybe = env.get
    server, hub = maybe("SALESCALL_SERVER_URL"), maybe("SALESCALL_HUB_URL")
    if hub is None:
        if server is None or not server.startswith("https://"):
            raise ConfigMissing("設定 SALESCALL_SERVER_URL（https://…）が入っていません（手順：docs/PC_SETUP.md）")
        hub = "wss://" + server[len("https://"):].rstrip("/") + "/link"
    where = urlsplit(hub)
    if not (where.scheme == "wss" or (where.scheme == "ws" and where.hostname in _LOCAL)):
        raise ConfigMissing("サーバーのつなぎ口は wss:// で始まるものだけです（同じ PC の中の試しだけ ws://）")
    try:
        lines = tuple(UUID(x.strip()) for x in (maybe("SALESCALL_LINES") or "").split(",") if x.strip())
    except ValueError:
        raise ConfigMissing("SALESCALL_LINES は回線の ID（例 0a1b…）をカンマで区切って書きます") from None
    drive = maybe("SALESCALL_BACKUP_DRIVE")
    return Config(server_url=server, hub_url=hub, lines=lines, phone=maybe("SALESCALL_PHONE"),
                  backup_drive=Path(drive) if drive else None, updater=maybe("SALESCALL_UPDATER"), environ=env.values)


def status(environ: dict[str, str] | None = None) -> list[str]:
    """どの設定が入っているか（値は出さない）。"""
    return Env(NAMES, environ).status()
