"""見守り役の設定（PC ごと。DESIGN.md 5-4）。読むのは `load` の1か所。持ち主 CL-2。

PC の環境変数から、下の表の名前（SALESCALL_ で始まるもの）だけを読む。**ほかの値は読まない**：会話AIのキーなどが入ったファイルも
読まない（キーの値を読むのは、キーの選択の処理だけ＝I-32・CL-1 の条件 (g)）。回線ごとの処理にも、表の名前の値だけを渡す。
**値は画面・記録・知らせに出さない**（出すのは「入っているか」だけ＝`status`）。

| 名前 | 中身 | 要るとき |
|---|---|---|
| SALESCALL_DSN | データベースへの接続（アプリの役） | いつも |
| SALESCALL_TENANT_ID | この PC の契約者（1台の PC は1人の契約者の回線だけを動かす。契約者が携帯を用意する＝Intent.md 8-6） | いつも |
| SALESCALL_LINES | この PC につないだ回線（回線の ID をカンマで区切る） | 電話をかけるとき |
| SALESCALL_LINE_DRIVER | 回線の部品を作る関数（`モジュール:名前`。電話接続の担当） | 電話をかけるとき |
| SALESCALL_CALL_RUNNER | 通話を最後まで進める部品を作る関数（会話＝CL-1） | 電話をかけるとき |
| SALESCALL_VERIFIER | 通話後の検証の部品を作る関数（CL-1） | 検証をこの PC でするとき |
| SALESCALL_PROBER | 停止を解く確かめの部品を作る関数（CL-1・電話接続） | 確かめをこの PC でするとき |
| SALESCALL_SYSTEM_JOBS | 1 なら、すべての契約者に共通の仕事（費用の見張り・祝日の暦・控え）もこの PC でする（管理者の PC） | 管理者の PC |
| SALESCALL_OWNER_DSN | データベースへの接続（表の持ち主。控えを取る） | 控えを取る PC |
| SALESCALL_RESTORE_DSN | 戻せるかを確かめる、空のデータベース（本番と別） | 控えを取る PC |
| SALESCALL_BACKUP_DRIVE | 控え用のハードディスクの一番上（例 E:\\） | 控えを取る PC |
| SALESCALL_BACKUP_KEY | 控えの鍵の元（Proton Pass で作った文字。読むのは backup.load_key の1か所） | 控えを取る PC |
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

NAMES = ("SALESCALL_DSN", "SALESCALL_TENANT_ID", "SALESCALL_LINES", "SALESCALL_LINE_DRIVER", "SALESCALL_CALL_RUNNER",
         "SALESCALL_VERIFIER", "SALESCALL_PROBER", "SALESCALL_SYSTEM_JOBS", "SALESCALL_OWNER_DSN", "SALESCALL_RESTORE_DSN",
         "SALESCALL_BACKUP_DRIVE", "SALESCALL_BACKUP_KEY")


class ConfigMissing(Exception):
    """要る設定が入っていない（名前と入れ方だけを書く。値は書かない）。"""


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
    def backups(self) -> bool:
        """この PC で控えを取るか。"""
        return self.system_jobs and bool(self.owner_dsn and self.restore_dsn and self.backup_drive)


def gather(environ: dict[str, str] | None = None) -> dict[str, str]:
    """環境変数のうち、表の名前のものだけ（ほかの値は読まない）。"""
    env = os.environ if environ is None else environ
    return {name: env[name] for name in NAMES if env.get(name, "") != ""}


def load(environ: dict[str, str] | None = None) -> Config:
    env = gather(environ)

    def need(name: str) -> str:
        value = env.get(name, "").strip()
        if not value:
            raise ConfigMissing(f"設定 {name} が入っていません（PC の環境変数に入れてください。手順：docs/PC_SETUP.md）")
        return value

    def maybe(name: str) -> str | None:
        return env.get(name, "").strip() or None

    try:
        tenant = UUID(need("SALESCALL_TENANT_ID"))
        lines = tuple(UUID(x.strip()) for x in (maybe("SALESCALL_LINES") or "").split(",") if x.strip())
    except ValueError:
        raise ConfigMissing("SALESCALL_TENANT_ID・SALESCALL_LINES は ID（例 0a1b…）で書きます") from None
    drive = maybe("SALESCALL_BACKUP_DRIVE")
    return Config(dsn=need("SALESCALL_DSN"), tenant_id=tenant, lines=lines, line_driver=maybe("SALESCALL_LINE_DRIVER"),
                  call_runner=maybe("SALESCALL_CALL_RUNNER"), verifier=maybe("SALESCALL_VERIFIER"),
                  prober=maybe("SALESCALL_PROBER"), system_jobs=maybe("SALESCALL_SYSTEM_JOBS") == "1",
                  owner_dsn=maybe("SALESCALL_OWNER_DSN"), restore_dsn=maybe("SALESCALL_RESTORE_DSN"),
                  backup_drive=Path(drive) if drive else None, environ=env)


def status(environ: dict[str, str] | None = None) -> list[str]:
    """どの設定が入っているか（値は出さない）。"""
    env = gather(environ)
    return [f"{name}：{'入っています' if env.get(name, '').strip() else '入っていません'}" for name in NAMES]
