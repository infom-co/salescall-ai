"""PC の部品の、控えを書く所（DESIGN.md 3-10・5-5）。持ち主 CL-2。

サーバーが書き出した中身を受け取り、**この PC の鍵で暗号化して、PC に接続したハードディスクに書き、読み戻して、元の中身と同じかを
確かめる**（鍵はこの PC と Proton Pass だけ。サーバーは鍵を持たない）。古い控えの片付けも、サーバーが決めた決まり（残す日数・消さない
もの）で、ここがする（ハードディスクの中を見られるのは PC だけ）。

ハードディスクの中の形：<ハードディスク>\\salescall-backup\\<依頼者のフォルダ>\\AI・submission・recording ／ データベース全体は
<ハードディスク>\\salescall-backup\\system\\AI（2026-10-17 ユーザー：「E:\\salescall-backup\\client1\\AI」）。
ハードディスクの一番上に印のファイル（DRIVE_MARK）を置く。印が無ければ「つながっていない」として書かない（外れたときに、同じ名前の
フォルダを PC の中に作って、そこへ書いてしまわない）。
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from salescall.link import sealing
from salescall.link.protocol import JST, RemoteError

BASE = "salescall-backup"
DRIVE_MARK = "salescall-backup-drive.txt"
KINDS = ("AI", "submission", "recording")
_NAME = re.compile(r"^salescall-[0-9A-Za-z._-]{1,200}\.enc$")          # 控えのファイルの名前（サーバーが決める。ほかの名前は書かない）
_FOLDER = re.compile(r"^[^\\/:*?\"<>|\x00-\x1f.][^\\/:*?\"<>|\x00-\x1f]{0,59}$")   # フォルダの名前（Windows で使える・上へ出ない）
_DATED = re.compile(r"^salescall-(\d{8})-(\d{6})[.-].*\.enc$")          # 消す決まりは、名前の日時だけを見る
_HELD_ID = re.compile(r"-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})-")


class DriveMissing(Exception):
    """控え用のハードディスクがつながっていない（PC の前の作業）。"""
    ask = "控え用のハードディスクがつながっていません。PC にハードディスクをつないでください。"


class WriteFailed(Exception):
    """書けなかった・読み戻したら違った（中身は、鍵・お客様の情報を含まない短い説明）。"""


@dataclass(frozen=True)
class Drive:
    root: Path

    def attached(self) -> bool:
        return (self.root / DRIVE_MARK).is_file()

    def place(self, folder: list[str]) -> Path:
        """書く場所（名前を確かめる：上のフォルダへ出ない・決まった種類だけ）。"""
        if len(folder) != 2 or not _FOLDER.match(folder[0]) or folder[1] not in KINDS:
            raise WriteFailed("控えのフォルダの名前が決まりに合いません")
        return self.root.joinpath(BASE, *folder)


def write(drive: Drive, key: bytes, folder: list[str], file_name: str, data: bytes, sha256: str) -> tuple[str, int]:
    """中身を暗号化して書き、読み戻して確かめる。暗号化したファイルの（指紋, 大きさ）を返す。"""
    if not drive.attached():
        raise DriveMissing("控え用のハードディスクがつながっていません")
    if not _NAME.match(file_name):
        raise WriteFailed("控えのファイルの名前が決まりに合いません")
    if hashlib.sha256(data).hexdigest() != sha256:
        raise WriteFailed("受け取った中身が、サーバーが書き出したものと違います（途中で欠けた）")
    blob = sealing.encrypt(data, key, file_name)
    place = drive.place(folder)
    place.mkdir(parents=True, exist_ok=True)
    tmp = place / (file_name + ".part")
    with open(tmp, "wb") as f:
        f.write(blob)
        f.flush()
        os.fsync(f.fileno())                 # ディスクに書き終えてから（読み戻しが OS の一時記憶から読めても、書けた証明にならない）
    tmp.replace(place / file_name)           # 書き終えてから名前を付ける（途中のファイルを控えと取り違えない）
    back = (place / file_name).read_bytes()  # 読み戻して、復号して、元の中身と同じか（戻せるか）
    if hashlib.sha256(sealing.decrypt(back, key, file_name)).hexdigest() != sha256:
        raise WriteFailed("ハードディスクから読み戻した控えが、書いたものと違います")
    return hashlib.sha256(back).hexdigest(), len(back)


def prune(drive: Drive, folders: dict[str, Any], now: datetime | None = None) -> int:
    """残す日数を過ぎた控えを消す（サーバーが決めた決まり。名前に日時のある控えのファイルだけ）。folders＝フォルダの名前 →
    {"days": {種類: 日数}, "verified": [戻せることまで確かめた AI用のファイルの名前], "held": [測り終わるまで消さない通話の番号]}。
    AI用は、戻せることまで確かめたもののうち一番新しいものを消さない（控えの失敗が長く続いても、1つは残る）。録音は、測り終わるまで
    消さない通話のものを消さない。消した数を返す。"""
    if not drive.attached():
        raise DriveMissing("控え用のハードディスクがつながっていません")
    now = datetime.now(JST) if now is None else now.astimezone(JST)
    removed = 0
    for name, rule in folders.items():
        for kind, days in rule.get("days", {}).items():
            place = drive.place([name, kind])
            if not place.is_dir():
                continue
            files = list(place.iterdir())
            old = [f for f in files if (m := _DATED.match(f.name))
                   and datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S").replace(tzinfo=JST)
                   < now - timedelta(days=int(days))]
            if kind == "AI" and old:
                verified = set(rule.get("verified", []))
                newest = max((f.name for f in files if f.name in verified), default=None)
                old = [f for f in old if f.name != newest]
            if kind == "recording" and old:
                held = set(rule.get("held", []))
                old = [f for f in old if not ((m := _HELD_ID.search(f.name)) and m.group(1) in held)]
            for f in old:
                f.unlink()
                removed += 1
    return removed


@dataclass(frozen=True)
class LocalWriter:
    """この PC のハードディスクと鍵に書く（PC の部品が、サーバーの頼みを受けて使う。誤りは、サーバーへ返す形＝RemoteError にする）。"""
    drive: Drive
    environ: dict[str, str]

    def write(self, folder: list[str], file_name: str, data: bytes, sha256: str) -> tuple[str, int]:
        return _remote(lambda: write(self.drive, sealing.load_key(self.environ), folder, file_name, data, sha256))

    def prune(self, folders: dict[str, Any]) -> int:
        return _remote(lambda: prune(self.drive, folders))


def _remote(work: Any) -> Any:
    try:
        return work()
    except (DriveMissing, sealing.KeyMissing) as e:
        raise RemoteError(str(e), ask=e.ask) from None
    except (WriteFailed, sealing.SealError) as e:
        raise RemoteError(str(e)) from None
    except OSError as e:   # ディスクの空き・使用中など（中身は書かない＝種類の名前だけ）
        raise RemoteError(f"ハードディスクに書けません（{type(e).__name__}）") from None
