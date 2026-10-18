"""見守り役の設定（PC ごと。DESIGN.md 5-4）。読むのは `load` の1か所。持ち主 CL-2。

PC の環境変数から、下の表の名前（SALESCALL_ で始まるもの）だけを読む。**ほかの値は読まない**：会話AIのキーなどが入ったファイルも
読まない（キーの値を読むのは、キーの選択の処理だけ＝I-32・CL-1 の条件 (g)）。回線ごとの処理にも、表の名前の値だけを渡す。
**値は画面・記録・知らせに出さない**（出すのは「入っているか」だけ＝`status`）。

| 名前 | 中身 | 要るとき |
|---|---|---|
| SALESCALL_DSN | データベースへの接続（システムの役） | 管理者の PC（契約者の PC は入れない：PC のログインでしまった鍵でつなぐ＝pc_login） |
| SALESCALL_TENANT_ID | この PC の契約者（1台の PC は1人の契約者の回線だけを動かす。契約者が携帯を用意する＝Intent.md 8-6） | 管理者の PC（契約者の PC は、しまった鍵の契約者） |
| SALESCALL_SERVER_URL | SalesCall AI のサーバーの場所（https://…。PC のログインで使う） | 契約者の PC（ログインするとき） |
| SALESCALL_LINES | この PC につないだ回線（回線の ID をカンマで区切る） | 電話をかけるとき |
| SALESCALL_LINE_DRIVER | 回線の部品を作る関数（`モジュール:名前`。電話接続の担当） | 電話をかけるとき |
| SALESCALL_CALL_RUNNER | 通話を最後まで進める部品を作る関数（会話＝CL-1） | 電話をかけるとき |
| SALESCALL_VERIFIER | 通話後の検証の部品を作る関数（CL-1） | 検証をこの PC でするとき |
| SALESCALL_PROBER | 停止を解く確かめの部品を作る関数（CL-1・電話接続） | 確かめをこの PC でするとき |
| SALESCALL_SYSTEM_JOBS | 1 なら、すべての契約者に共通の仕事（祝日の暦・全体の停止の確かめ・落ちたままの PC の回線の後始末・データベース全体の控え）もこの PC でする（管理者の PC） | 管理者の PC |
| SALESCALL_OWNER_DSN | データベースへの接続（表の持ち主。データベース全体の控えを取る） | 管理者の PC |
| SALESCALL_RESTORE_DSN | 戻せるかを確かめる、空のデータベース（本番と別） | 管理者の PC |
| SALESCALL_BACKUP_DRIVE | 控え用のハードディスクの一番上（例 E:\\） | いつも（契約者の控え） |
| SALESCALL_BACKUP_KEY | 控えの鍵の元（Proton Pass で作った文字。読むのは backup.load_key の1か所） | いつも（契約者の控え） |
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from salescall.supervisor import pc_login

_STORED = object()   # しまった鍵を読む（試験では代わりを渡す）

NAMES = ("SALESCALL_DSN", "SALESCALL_TENANT_ID", "SALESCALL_SERVER_URL", "SALESCALL_LINES", "SALESCALL_LINE_DRIVER", "SALESCALL_CALL_RUNNER",
         "SALESCALL_VERIFIER", "SALESCALL_PROBER", "SALESCALL_SYSTEM_JOBS", "SALESCALL_OWNER_DSN", "SALESCALL_RESTORE_DSN",
         "SALESCALL_BACKUP_DRIVE", "SALESCALL_BACKUP_KEY")


class ConfigMissing(Exception):
    """要る設定が入っていない（名前と入れ方だけを書く。値は書かない）。"""


class NotLoggedIn(ConfigMissing):
    """契約者の PC が、まだログインしていない（見守り役は、ブラウザでログインのページを開いて待つ＝supervisor.pc_login）。"""


@dataclass(frozen=True)
class Config:
    dsn: str
    tenant_id: UUID
    lines: tuple[UUID, ...]
    line_driver: str | None
    call_runner: str | None
    verifier: str | None
    prober: str | None
    system_jobs: bool
    owner_dsn: str | None
    restore_dsn: str | None
    backup_drive: Path | None
    environ: dict[str, str]          # 読んだ表の名前の値（控えの鍵は backup.load_key がここから読む）

    @property
    def calls(self) -> bool:
        """この PC で電話をかけるか（回線と、回線の部品と、通話を進める部品がそろっているとき）。"""
        return bool(self.lines and self.line_driver and self.call_runner)

    @property
    def whole_backup(self) -> bool:
        """この PC でデータベース全体の控えを取るか（管理者の PC。すべての契約者のデータが入る）。契約者の控え（依頼者ごとの AI用・
        提出用）は、控え用のハードディスクがあれば、どの契約者の PC でも取る（2026-10-18 ユーザー：「控えは、契約者ごとですよ」）。"""
        return self.system_jobs and bool(self.owner_dsn and self.restore_dsn and self.backup_drive)


def gather(environ: dict[str, str] | None = None) -> dict[str, str]:
    """環境変数のうち、表の名前のものだけ（ほかの値は読まない）。"""
    env = os.environ if environ is None else environ
    return {name: env[name] for name in NAMES if env.get(name, "") != ""}


def load(environ: dict[str, str] | None = None, *, pc_key: Any = _STORED) -> Config:
    """設定を読む。データベースへの接続と契約者は、SALESCALL_DSN があればそれ（管理者の PC）、無ければ PC のログインでしまった鍵
    （契約者の PC。`pc_login.load`）。pc_key＝試験で、しまった鍵の代わりを渡すとき。"""
    env = gather(environ)

    def need(name: str) -> str:
        value = env.get(name, "").strip()
        if not value:
            raise ConfigMissing(f"設定 {name} が入っていません（PC の環境変数に入れてください。手順：docs/PC_SETUP.md）")
        return value

    def maybe(name: str) -> str | None:
        return env.get(name, "").strip() or None

    key = None
    if maybe("SALESCALL_DSN") is None:
        key = pc_login.load() if pc_key is _STORED else pc_key
        if key is None:
            raise NotLoggedIn("この PC は、まだ SalesCall AI にログインしていません（見守り役が、ブラウザでログインのページを開きます）")
    try:
        tenant = key.tenant_id if key is not None else UUID(need("SALESCALL_TENANT_ID"))
        if key is not None and maybe("SALESCALL_TENANT_ID") not in (None, str(key.tenant_id)):
            raise ConfigMissing("SALESCALL_TENANT_ID が、この PC でログインした契約者と違います（入れなくて大丈夫です）")
        lines = tuple(UUID(x.strip()) for x in (maybe("SALESCALL_LINES") or "").split(",") if x.strip())
    except ValueError:
        raise ConfigMissing("SALESCALL_TENANT_ID・SALESCALL_LINES は ID（例 0a1b…）で書きます") from None
    drive = maybe("SALESCALL_BACKUP_DRIVE")
    return Config(dsn=key.conninfo if key is not None else need("SALESCALL_DSN"), tenant_id=tenant, lines=lines, line_driver=maybe("SALESCALL_LINE_DRIVER"),
                  call_runner=maybe("SALESCALL_CALL_RUNNER"), verifier=maybe("SALESCALL_VERIFIER"),
                  prober=maybe("SALESCALL_PROBER"), system_jobs=maybe("SALESCALL_SYSTEM_JOBS") == "1",
                  owner_dsn=maybe("SALESCALL_OWNER_DSN"), restore_dsn=maybe("SALESCALL_RESTORE_DSN"),
                  backup_drive=Path(drive) if drive else None, environ=env)


def status(environ: dict[str, str] | None = None) -> list[str]:
    """どの設定が入っているか（値は出さない）と、この PC がログインしているか。"""
    env = gather(environ)
    key = pc_login.load()
    return [f"{name}：{'入っています' if env.get(name, '').strip() else '入っていません'}" for name in NAMES] + [
        f"この PC のログイン：{'済んでいます（契約者 ' + str(key.tenant_id) + '）' if key else 'まだです'}"]
