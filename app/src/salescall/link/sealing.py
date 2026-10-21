"""控えの暗号化（Intent.md 14-1。DESIGN.md 3-10・5-5）。持ち主 CL-2。**暗号化と鍵の作り方は、この1か所**。

控えの鍵は、PC（その PC の持ち主＝契約者か管理者の環境変数）と、その持ち主の Proton Pass の写しの2か所だけ（2026-10-17 ユーザー 案A・
「proton Passで作成」）。**サーバーは鍵を持たない**：サーバーは中身を書き出して PC の部品へ送り、PC がこの部品で暗号化して
ハードディスクに書く（2026-10-21 ユーザー承認の分け方）。
"""

from __future__ import annotations

import hashlib
import io
import os
from collections.abc import Iterator
from typing import BinaryIO

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAGIC = b"SALESCALL-BACKUP-1\n"    # 形1の頭（読むだけ。下の「暗号化の形」）
KEY_ENV = "SALESCALL_BACKUP_KEY"
KEY_MIN_CHARS = 20
_KEY_SALT = b"salescall-backup-key-1"     # 作り方を変えたら番号を上げる（前の控えは前の作り方でしか戻せない）


class SealError(Exception):
    """暗号化・復号できない（中身は、鍵・お客様の情報を含まない短い説明）。"""


class KeyMissing(SealError):
    """控えの鍵が PC に入っていない・短すぎる（PC の前の作業：`ask` を、その PC の持ち主へ知らせる）。"""
    ask = "控えの鍵が PC に入っていません（または短すぎます）。手順書 docs/PC_SETUP.md のとおり、Proton Pass の文字を入れてください。"


def key_from_text(text: str) -> bytes:
    """Proton Pass で作った文字から、控えの鍵（32バイト）を作る。前後の空白・改行は外す（写し間違いを防ぐ）。値は出さない。"""
    text = text.strip()
    if len(text) < KEY_MIN_CHARS:
        raise KeyMissing(f"控えの鍵の元は{KEY_MIN_CHARS}文字以上です（Proton Pass で作った文字を、そのまま写してください）")
    return hashlib.scrypt(text.encode(), salt=_KEY_SALT, n=2 ** 14, r=8, p=1, dklen=32)


def load_key(environ: dict[str, str] | None = None) -> bytes:
    """PC の環境変数から控えの鍵を読む。無ければ、入れ方を書いた KeyMissing（値は書かない）。"""
    text = (os.environ if environ is None else environ).get(KEY_ENV, "")
    if not text.strip():
        raise KeyMissing(f"控えの鍵が入っていません。PC の環境変数 {KEY_ENV} に、Proton Pass に置いた文字をそのまま入れてください")
    return key_from_text(text)


def key_matches(text: str, environ: dict[str, str] | None = None) -> bool:
    """PC の持ち主が貼った Proton Pass の文字と、PC に入っている文字が、同じ鍵になるか（PC の文字が1文字でも違うと、確かめは通るのに
    Proton Pass の文字では控えを戻せない。どちらの値も出さない）。"""
    return key_from_text(text) == load_key(environ)


# ── 暗号化の形（判断はこの1か所） ──
# 形2（2026-10-21〜。いま書く形）：控えを丸ごとメモリに持たないよう、決まった大きさ（SEGMENT）ずつ分けて暗号化する。
#   頭＝MAGIC2 ＋ 分ける大きさ（4バイト）＋ でたらめな7バイト。分けた1つずつを AES-GCM で暗号化（守りの印16バイトつき）。
#   使い捨ての番号＝でたらめな7バイト ＋ 何番目か（4バイト）＋ 最後か（1バイト）。置き場所（`label`＝フォルダとファイルの名前）も
#   合わせて守る。
#   ⇒ 途中で切れた（最後の印が無い）・順番を入れ替えた・別のファイルと取り替えた・別の依頼者のフォルダへ移した（同じ秒に取った控えは
#     同じ名前になる）、のどれも復号できない（取り違えた依頼者のデータを戻さない）。
# 形1（〜2026-10-21）：丸ごと1回で暗号化した形（守るのはファイルの名前だけ）。前に書いた控えを戻せるよう、読むことだけはできる。
MAGIC2 = b"SALESCALL-BACKUP-2\n"
SEGMENT = 1 << 20
_HEAD2 = len(MAGIC2) + 4 + 7
_TAG = 16


def label(folder: list[str], file_name: str) -> str:
    """控えの置き場所の名前（ハードディスクの salescall-backup から下の場所。例「client1/AI/salescall-20261021-120000.ai.enc」）。
    暗号化で一緒に守る（判断はこの1か所。戻すときも、置いてあった場所の名前で開く）。"""
    return "/".join([*folder, file_name])


def _nonce(prefix: bytes, index: int, last: bool) -> bytes:
    if index >= 2 ** 32:
        raise SealError("控えが大きすぎます")
    return prefix + index.to_bytes(4, "big") + (b"\x01" if last else b"\x00")


class Sealer:
    """少しずつ受け取って、少しずつ暗号化して書く（形2）。`write` で渡し、`close` で最後の1つを書く（close しないと、復号できない
    ＝途中で切れた控えを、控えと取り違えない）。"""

    def __init__(self, out: BinaryIO, key: bytes, where: str, segment: int = SEGMENT) -> None:
        if len(key) != 32:
            raise SealError("控えの鍵は32バイトです")
        self._out, self._aead, self._aad, self._segment = out, AESGCM(key), where.encode(), segment
        self._prefix, self._index, self._buffer = os.urandom(7), 0, bytearray()
        out.write(MAGIC2 + segment.to_bytes(4, "big") + self._prefix)

    def write(self, data: bytes | memoryview) -> None:
        self._buffer += data
        while len(self._buffer) > self._segment:   # 最後の1つは close まで持つ（最後かどうかは、終わってから分かる）
            self._seal(bytes(self._buffer[:self._segment]), last=False)
            del self._buffer[:self._segment]

    def close(self) -> None:
        self._seal(bytes(self._buffer), last=True)
        self._buffer = bytearray()

    def _seal(self, plain: bytes, *, last: bool) -> None:
        self._out.write(self._aead.encrypt(_nonce(self._prefix, self._index, last), plain, self._aad))
        self._index += 1


def unseal(src: BinaryIO, key: bytes, where: str) -> Iterator[bytes]:
    """暗号化した控えを、少しずつ復号して返す（形2。形1も読める）。where＝置き場所の名前（`label`）。守りの印が合わない・途中で
    切れていれば SealError（返した分は捨てる）。"""
    if len(key) != 32:
        raise SealError("控えの鍵は32バイトです")
    aad, aead = where.encode(), AESGCM(key)
    head = src.read(len(MAGIC2))
    if head == MAGIC:   # 形1：丸ごと（前に書いた控え。守ったのはファイルの名前だけ）
        nonce = src.read(12)
        try:
            yield aead.decrypt(nonce, src.read(), where.rsplit("/", 1)[-1].encode())
        except Exception:
            raise SealError("控えを復号できません（鍵が違う・ファイルが壊れている・取り替えられた）") from None
        return
    if head != MAGIC2:
        raise SealError("控えのファイルの形が違います")
    rest = src.read(_HEAD2 - len(MAGIC2))
    if len(rest) != _HEAD2 - len(MAGIC2):
        raise SealError("控えのファイルが途中で切れています")
    segment, prefix = int.from_bytes(rest[:4], "big"), rest[4:]
    if not 0 < segment <= 64 * SEGMENT:
        raise SealError("控えのファイルの形が違います")
    index, piece = 0, src.read(segment + _TAG)
    while True:
        after = src.read(segment + _TAG)
        last = not after
        try:
            yield aead.decrypt(_nonce(prefix, index, last), piece, aad)
        except Exception:
            raise SealError("控えを復号できません（鍵が違う・ファイルが壊れている・途中で切れている・取り替えられた）") from None
        if last:
            return
        index, piece = index + 1, after


def encrypt(data: bytes, key: bytes, where: str) -> bytes:
    """丸ごと渡す形（小さな中身・試験用。中は Sealer と同じ＝形2）。"""
    out = io.BytesIO()
    sealer = Sealer(out, key, where)
    sealer.write(data)
    sealer.close()
    return out.getvalue()


def decrypt(blob: bytes, key: bytes, where: str) -> bytes:
    """丸ごと戻す形（小さな中身・試験用。中は unseal と同じ）。"""
    return b"".join(unseal(io.BytesIO(blob), key, where))
