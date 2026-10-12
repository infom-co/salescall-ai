"""keybox：APIキーを、GitHub を通して安全に受け渡すための道具（1本だけ）。

しくみ（公開鍵の暗号）：
  - 受け取る側（クラウドの作業環境）が「錠前（公開鍵）」と「鍵（秘密鍵）」の組を作る。
    錠前だけを secrets/recipient_pubkey.pem として GitHub に置く。鍵は作業環境の中から出さない。
  - 送る側（PC）は、錠前で .env の Gemini のキーの行だけを閉じ、secrets/gemini_keys.sealed.json を GitHub に上げる。
    ⇒ 中身は、作業環境の鍵が無いと誰も読めない（GitHub に漏れても読めない）。
  - 受け取る側が鍵で開ける。使い終わったら、開けた中身と鍵を消す（消せば、箱は二度と開かない）。

使い方：
  [作業環境] python tools/keybox.py newkey --private <作業環境の中の場所>
  [PC]       python tools/keybox.py seal                       （C:\\Users\\User\\SalesCall\\.env を読む）
  [作業環境] python tools/keybox.py open --private <鍵> --out <作業環境の中の場所>

暗号の方式：RSA-4096（OAEP・SHA-256）で、使い捨ての AES-256-GCM の鍵を包む。中身は AES-256-GCM で閉じる。
キーの値は、どの場面でも画面に出さない。
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "phase0"))
from common import DEFAULT_ENV_PATH, _KEY_NAME_PATTERN, read_env_file  # noqa: E402  .env の読み方は1か所だけ

PUBKEY_PATH = ROOT / "secrets" / "recipient_pubkey.pem"
SEALED_PATH = ROOT / "secrets" / "gemini_keys.sealed.json"
FORMAT = "salescall-keybox-v1"


def _crypto():
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding, rsa
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        raise SystemExit("cryptography が入っていません：pip install cryptography")
    return hashes, serialization, padding, rsa, AESGCM


def _fingerprint(pub_pem: bytes) -> str:
    return hashlib.sha256(pub_pem.strip()).hexdigest()[:16]


def _oaep(hashes, padding):
    return padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None)


def _inside_repo(path: Path) -> bool:
    try:
        path.resolve().relative_to(ROOT)
        return True
    except ValueError:
        return False


def cmd_newkey(args) -> None:
    hashes, serialization, padding, rsa, AESGCM = _crypto()
    priv_path = Path(args.private)
    if _inside_repo(priv_path):
        raise SystemExit("⛔鍵（秘密鍵）は、リポジトリの外に置いてください（GitHub に上がらないように）")
    if priv_path.exists() and not args.force:
        raise SystemExit(f"{priv_path} はもうあります（作り直すなら --force）")
    key = rsa.generate_private_key(public_exponent=65537, key_size=4096)
    priv_pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    pub_pem = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    priv_path.parent.mkdir(parents=True, exist_ok=True)
    priv_path.write_bytes(priv_pem)
    os.chmod(priv_path, stat.S_IRUSR | stat.S_IWUSR)
    PUBKEY_PATH.parent.mkdir(parents=True, exist_ok=True)
    PUBKEY_PATH.write_bytes(pub_pem)
    print(f"錠前（公開鍵）を作りました：{PUBKEY_PATH}")
    print(f"錠前の指紋：{_fingerprint(pub_pem)}   ← PC で seal するときに、同じ指紋が出ることを確かめる")


def cmd_seal(args) -> None:
    hashes, serialization, padding, rsa, AESGCM = _crypto()
    if not PUBKEY_PATH.is_file():
        raise SystemExit(f"錠前がありません：{PUBKEY_PATH}（git pull してください）")
    pub_pem = PUBKEY_PATH.read_bytes()
    pub = serialization.load_pem_public_key(pub_pem)
    pairs = read_env_file(args.env)
    wanted = set(args.key_name or [])
    picked = [(n, v) for n, v in pairs if v and ((n in wanted) if wanted else bool(_KEY_NAME_PATTERN.search(n)))]
    if not picked:
        raise SystemExit("Gemini のキーの行が見つかりません（--key-name で変数名を指定できます）")
    plaintext = "".join(f"{n}={v}\n" for n, v in picked).encode("utf-8")
    aes_key = AESGCM.generate_key(bit_length=256)
    nonce = os.urandom(12)
    sealed = {
        "format": FORMAT,
        "recipient_fingerprint": _fingerprint(pub_pem),
        "names": [n for n, _ in picked],  # 変数名だけ（値は入れない）
        "wrapped_key": base64.b64encode(pub.encrypt(aes_key, _oaep(hashes, padding))).decode(),
        "nonce": base64.b64encode(nonce).decode(),
        "ciphertext": base64.b64encode(AESGCM(aes_key).encrypt(nonce, plaintext, FORMAT.encode())).decode(),
    }
    out = Path(args.out) if args.out else SEALED_PATH
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(sealed, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"錠前の指紋：{sealed['recipient_fingerprint']}   ← チャットで伝えた指紋と同じか確かめてください")
    print(f"閉じたキー：{len(picked)} 個（変数名：{', '.join(sealed['names'])}）")
    print(f"閉じた箱：{out}")
    print("次に：git add secrets/gemini_keys.sealed.json && git commit -m \"キーの箱\" && git push")


def cmd_open(args) -> None:
    hashes, serialization, padding, rsa, AESGCM = _crypto()
    out = Path(args.out)
    if _inside_repo(out):
        raise SystemExit("⛔開けた中身は、リポジトリの外に置いてください（GitHub に上がらないように）")
    sealed = json.loads(Path(args.sealed).read_text(encoding="utf-8"))
    if sealed.get("format") != FORMAT:
        raise SystemExit("箱の形式が違います")
    key = serialization.load_pem_private_key(Path(args.private).read_bytes(), password=None)
    pub_pem = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    if _fingerprint(pub_pem) != sealed["recipient_fingerprint"]:
        raise SystemExit("⛔この鍵で閉じた箱ではありません（錠前の指紋が違います）")
    aes_key = key.decrypt(base64.b64decode(sealed["wrapped_key"]), _oaep(hashes, padding))
    plaintext = AESGCM(aes_key).decrypt(base64.b64decode(sealed["nonce"]), base64.b64decode(sealed["ciphertext"]), FORMAT.encode())
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(plaintext)
    os.chmod(out, stat.S_IRUSR | stat.S_IWUSR)
    print(f"開けました：{out}（キー {len(sealed['names'])} 個。値は表示しません）")


def main() -> None:
    p = argparse.ArgumentParser(description="APIキーを GitHub 経由で安全に受け渡す")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("newkey", help="[作業環境] 錠前と鍵を作る")
    a.add_argument("--private", required=True, help="鍵（秘密鍵）を置く場所（リポジトリの外）")
    a.add_argument("--force", action="store_true")
    a.set_defaults(fn=cmd_newkey)
    b = sub.add_parser("seal", help="[PC] .env の Gemini のキーを錠前で閉じる")
    b.add_argument("--env", default=os.environ.get("SALESCALL_ENV", DEFAULT_ENV_PATH))
    b.add_argument("--key-name", action="append", help="閉じる変数名（省略時は名前に GEMINI/GOOGLE と KEY を含むもの）")
    b.add_argument("--out", help="箱の置き場所（既定：secrets/gemini_keys.sealed.json）")
    b.set_defaults(fn=cmd_seal)
    c = sub.add_parser("open", help="[作業環境] 箱を開ける")
    c.add_argument("--private", required=True)
    c.add_argument("--sealed", default=str(SEALED_PATH))
    c.add_argument("--out", required=True, help="開けた中身を置く場所（リポジトリの外）")
    c.set_defaults(fn=cmd_open)
    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
