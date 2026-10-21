"""控えの暗号化（Intent.md 14-1。DESIGN.md 3-10・5-5）。持ち主 CL-2。**暗号化と鍵の作り方は、この1か所**。

控えの鍵は、PC（その PC の持ち主＝契約者か管理者の環境変数）と、その持ち主の Proton Pass の写しの2か所だけ（2026-10-17 ユーザー 案A・
「proton Passで作成」）。**サーバーは鍵を持たない**：サーバーは中身を書き出して PC の部品へ送り、PC がこの部品で暗号化して
ハードディスクに書く（2026-10-21 ユーザー承認の分け方）。
"""

from __future__ import annotations

import hashlib
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAGIC = b"SALESCALL-BACKUP-1\n"    # 控えのファイルの頭（形が変わったら番号を上げる）
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


def encrypt(data: bytes, key: bytes, file_name: str) -> bytes:
    """控えを暗号化する（AES-GCM。ファイルの名前も合わせて守る＝別のファイルと取り替えられたら復号できない）。"""
    if len(key) != 32:
        raise SealError("控えの鍵は32バイトです")
    nonce = os.urandom(12)
    return MAGIC + nonce + AESGCM(key).encrypt(nonce, data, file_name.encode())


def decrypt(blob: bytes, key: bytes, file_name: str) -> bytes:
    if not blob.startswith(MAGIC):
        raise SealError("控えのファイルの形が違います")
    nonce, body = blob[len(MAGIC):len(MAGIC) + 12], blob[len(MAGIC) + 12:]
    try:
        return AESGCM(key).decrypt(nonce, body, file_name.encode())
    except Exception:
        raise SealError("控えを復号できません（鍵が違う・ファイルが壊れている・取り替えられた）") from None
