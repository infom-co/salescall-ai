"""中央の見守り役の設定（サーバー。DESIGN.md 5-4・5-5）。読むのは `load` の1か所。持ち主 CL-2。

サーバーの環境変数から、下の表の名前（SALESCALL_ で始まるもの）だけを読む。**ほかの値は読まない**：会話AIのキーなどが入ったファイルも
読まない（キーの値を読むのは、キーの選択の処理だけ＝I-32・CL-1 の条件 (g)）。**値は画面・記録・知らせに出さない**（出すのは
「入っているか」だけ＝`status`）。契約者の PC の設定は `salescall.agent.config`（PC はデータベースにつながない＝5-5）。

| 名前 | 中身 | 要るとき |
|---|---|---|
| SALESCALL_DSN | データベースへの接続（システムの役） | いつも |
| SALESCALL_LISTEN | PC の部品からのつなぎを待つ場所（例 0.0.0.0:8765。暗号化はサーバーの入口がする） | いつも（無ければ動かない） |
| SALESCALL_CALL_RUNNER | 通話を最後まで進める部品を作る関数（`モジュール:名前`。会話＝CL-1。回線の ID と、その回線の電話の部品を受け取る） | 電話をかけるとき |
| SALESCALL_VERIFIER | 通話後の検証の部品を作る関数（CL-1） | 検証をするとき |
| SALESCALL_PROBER | 停止を解く確かめの部品を作る関数（CL-1・電話接続） | 確かめをするとき |
| SALESCALL_OWNER_DSN | データベースへの接続（表の持ち主。データベース全体の控えの中身を書き出す） | 全体の控え |
| SALESCALL_RESTORE_DSN | 戻せるかを確かめる、空のデータベース（本番と別） | 全体の控え |
| SALESCALL_WHOLE_BACKUP_TENANT | データベース全体の控えを書く PC の契約者（管理者自身の契約者の番号。すべての契約者のデータが入るので、この契約者の PC にだけ送る。PC で決めないのは、ログインし直すと PC の番号が変わるため） | 全体の控え |
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

NAMES = ("SALESCALL_DSN", "SALESCALL_LISTEN", "SALESCALL_CALL_RUNNER", "SALESCALL_VERIFIER", "SALESCALL_PROBER",
         "SALESCALL_OWNER_DSN", "SALESCALL_RESTORE_DSN", "SALESCALL_WHOLE_BACKUP_TENANT")


class ConfigMissing(Exception):
    """要る設定が入っていない（名前と入れ方だけを書く。値は書かない）。"""


@dataclass(frozen=True)
class Config:
    dsn: str = field(repr=False)     # 合言葉を含む（表示に出さない）
    listen: tuple[str, int] | None
    call_runner: str | None
    verifier: str | None
    prober: str | None
    owner_dsn: str | None = field(repr=False)
    restore_dsn: str | None = field(repr=False)
    whole_backup_tenant: UUID | None = None
    environ: dict[str, str] = field(repr=False, default_factory=dict)

    @property
    def calls(self) -> bool:
        """電話をかけるか（通話を進める部品があるとき。かける回線は、つないでいる PC の部品が決める＝5-5）。"""
        return self.call_runner is not None

    @property
    def whole_backup(self) -> bool:
        """データベース全体の控えを取るか（表の持ち主の接続・戻せるかを確かめるデータベース・書く管理者の PC があるとき）。"""
        return bool(self.owner_dsn and self.restore_dsn and self.whole_backup_tenant)


def gather(environ: dict[str, str] | None = None) -> dict[str, str]:
    """環境変数のうち、表の名前のものだけ（ほかの値は読まない）。"""
    env = os.environ if environ is None else environ
    return {name: env[name] for name in NAMES if env.get(name, "") != ""}


def load(environ: dict[str, str] | None = None) -> Config:
    env = gather(environ)

    def maybe(name: str) -> str | None:
        return env.get(name, "").strip() or None

    dsn = maybe("SALESCALL_DSN")
    if dsn is None:
        raise ConfigMissing("設定 SALESCALL_DSN が入っていません（サーバーの環境変数に入れてください。手順：app/README.md）")
    listen = maybe("SALESCALL_LISTEN")
    where: tuple[str, int] | None = None
    if listen is not None:
        host, _, port = listen.rpartition(":")
        if not host or not port.isdigit():
            raise ConfigMissing("SALESCALL_LISTEN は「場所:番号」で書きます（例 0.0.0.0:8765）")
        where = (host, int(port))
    try:
        whole = UUID(maybe("SALESCALL_WHOLE_BACKUP_TENANT")) if maybe("SALESCALL_WHOLE_BACKUP_TENANT") else None
    except ValueError:
        raise ConfigMissing("SALESCALL_WHOLE_BACKUP_TENANT は契約者の番号（例 0a1b…）で書きます") from None
    return Config(dsn=dsn, listen=where, call_runner=maybe("SALESCALL_CALL_RUNNER"), verifier=maybe("SALESCALL_VERIFIER"),
                  prober=maybe("SALESCALL_PROBER"), owner_dsn=maybe("SALESCALL_OWNER_DSN"),
                  restore_dsn=maybe("SALESCALL_RESTORE_DSN"), whole_backup_tenant=whole, environ=env)


def status(environ: dict[str, str] | None = None) -> list[str]:
    """どの設定が入っているか（値は出さない）。"""
    env = gather(environ)
    return [f"{name}：{'入っています' if env.get(name, '').strip() else '入っていません'}" for name in NAMES]
