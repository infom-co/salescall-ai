"""PC の部品の、控えを書く所（DESIGN.md 3-10・5-5）。持ち主 CL-2。

サーバーが書き出した中身を受け取り、**この PC の鍵で暗号化して、PC に接続したハードディスクに書き、読み戻して、元の中身と同じかを
確かめる**（鍵はこの PC と Proton Pass だけ。サーバーは鍵を持たない）。古い控えの片付けも、サーバーが決めた決まり（残す日数・消さない
もの）で、ここがする（ハードディスクの中を見られるのは PC だけ）。

ハードディスクの中の形・名前とフォルダの決まりは `salescall.link.backup_files` の1か所（サーバーと同じ）。
ハードディスクの一番上に印のファイル（DRIVE_MARK）を置く。印が無ければ「つながっていない」として書かない（外れたときに、同じ名前の
フォルダを PC の中に作って、そこへ書いてしまわない）。
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from salescall.link import sealing
from salescall.link.backup_files import AI, BASE, DRIVE_MARK, PART, RECORDING, good_folder, good_name, held_call, taken_at
from salescall.link.jst import JST
from salescall.link.protocol import RemoteError


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
        if not good_folder(folder):
            raise WriteFailed("控えのフォルダの名前が決まりに合いません")
        return self.root.joinpath(BASE, *folder)


class Incoming:
    """1つの控えを、受け取りながら暗号化して書く（丸ごとメモリに持たない。`link.sealing.Sealer`）。書きかけは「.part」の名前で書き、
    `finish` で、受け取った中身がサーバーの書き出したものと同じか（指紋）を確かめ、ディスクに書き終えてから名前を付け、読み戻して復号し、
    元の中身と同じかを確かめる。途中でやめるときは `abort`（書きかけを消す＝控えと取り違えない）。"""

    def __init__(self, drive: Drive, key: bytes, folder: list[str], file_name: str, sha256: str) -> None:
        if not drive.attached():
            raise DriveMissing("控え用のハードディスクがつながっていません")
        if not good_name(file_name):
            raise WriteFailed("控えのファイルの名前が決まりに合いません")
        self.place, self.file_name, self.key, self.expected = drive.place(folder), file_name, key, sha256
        self.where = sealing.label(folder, file_name)   # 置き場所も一緒に守る（別の依頼者のフォルダへ移した控えは開けない）
        self.place.mkdir(parents=True, exist_ok=True)
        self.tmp = self.place / (file_name + PART)
        self._file = open(self.tmp, "wb")
        self._sealer = sealing.Sealer(self._file, key, self.where)
        self._plain = hashlib.sha256()

    def feed(self, data: bytes | memoryview) -> None:
        self._plain.update(data)
        self._sealer.write(data)

    def finish(self) -> tuple[str, int]:
        """書き終える。暗号化したファイルの（指紋, 大きさ）を返す。"""
        if self._plain.hexdigest() != self.expected:
            self.abort()
            raise WriteFailed("受け取った中身が、サーバーが書き出したものと違います（途中で欠けた）")
        self._sealer.close()
        self._file.flush()
        os.fsync(self._file.fileno())        # ディスクに書き終えてから（読み戻しが OS の一時記憶から読めても、書けた証明にならない）
        self._file.close()
        final = self.place / self.file_name
        self.tmp.replace(final)              # 書き終えてから名前を付ける（途中のファイルを控えと取り違えない）
        try:
            enc, plain = hashlib.sha256(), hashlib.sha256()
            with open(final, "rb") as raw:   # 読み戻して、少しずつ復号して、元の中身と同じか（戻せるか）
                for piece in sealing.unseal(_Hashing(raw, enc), self.key, self.where):
                    plain.update(piece)
            if plain.hexdigest() != self.expected:
                raise WriteFailed("ハードディスクから読み戻した控えが、書いたものと違います")
            return enc.hexdigest(), final.stat().st_size
        except BaseException:
            _remove(final)                   # 戻せると確かめられなかった控えを残さない（どんな誤りでも）
            raise

    def abort(self) -> None:
        """書きかけを消す（つながりが切れた・止める ときにも呼ぶので、誤りを上げない。消せなければ、古い書きかけとして prune が消す）。"""
        try:
            if not self._file.closed:
                self._file.close()
        except OSError:
            pass
        _remove(self.tmp)


def _remove(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


class _Hashing:
    """読んだバイトの指紋を取りながら読む（暗号化したファイルの指紋と復号を、1回の読みで）。"""

    def __init__(self, raw: Any, digest: Any) -> None:
        self.raw, self.digest = raw, digest

    def read(self, n: int = -1) -> bytes:
        got = self.raw.read(n)
        self.digest.update(got)
        return got


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
            for f in files:   # 書いている途中で止まった書きかけ（1日より前）を消す（abort が消せなかったもの。書いている途中のものは消さない）
                if f.name.endswith(PART) and datetime.fromtimestamp(f.stat().st_mtime, JST) < now - timedelta(days=1):
                    f.unlink()
                    removed += 1
            old = [f for f in files if (at := taken_at(f.name)) is not None and at < now - timedelta(days=int(days))]
            if kind == AI and old:
                verified = set(rule.get("verified", []))
                newest = max((f.name for f in files if f.name in verified), default=None)
                old = [f for f in old if f.name != newest]
            if kind == RECORDING and old:
                held = set(rule.get("held", []))
                old = [f for f in old if held_call(f.name) not in held]
            for f in old:
                f.unlink()
                removed += 1
    return removed


@dataclass(frozen=True)
class LocalWriter:
    """この PC のハードディスクと鍵に書く（PC の部品が、サーバーの頼みを受けて使う。誤りは、サーバーへ返す形＝RemoteError にする）。"""
    drive: Drive
    environ: dict[str, str]

    def begin(self, folder: list[str], file_name: str, sha256: str) -> Incoming:
        """受け取りながら書き始める（PC の部品が、サーバーから届く中身を少しずつ渡す）。"""
        return as_remote(lambda: Incoming(self.drive, sealing.load_key(self.environ), folder, file_name, sha256))

    def prune(self, folders: dict[str, Any]) -> int:
        return as_remote(lambda: prune(self.drive, folders))


def as_remote(work: Any) -> Any:
    """控えを書く・片付ける処理の誤りを、サーバーへ返す形（RemoteError。PC の前の作業なら ask つき）にする（判断はこの1か所）。"""
    try:
        return work()
    except (DriveMissing, sealing.KeyMissing) as e:
        raise RemoteError(str(e), ask=e.ask) from None
    except (WriteFailed, sealing.SealError) as e:
        raise RemoteError(str(e)) from None
    except OSError as e:   # ディスクの空き・使用中など（中身は書かない＝種類の名前だけ）
        raise RemoteError(f"ハードディスクに書けません（{type(e).__name__}）") from None
